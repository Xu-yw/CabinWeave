"""
CabinWeave 意图识别评测

样本：backend/evaluation/意图样本.json，58 条（单意图 48、多意图 10），load_samples / parse_samples 负责格式校验。

两组对比实验：
  E1 分层消融：只关键词 / 只 LLM / 关键词 + LLM 三个识别器跑全部样本，只测识别器本身。
  E2 快速通道开关：
    ① 意图层面：关闭时就是 E1 的"关键词 + LLM"组；开启时被快速通道命中的样本改用规则对应的意图。
    ② 端到端：标 fast_path 的样本各用一辆套用 parked 场景的新车，调用编排器 run(req, fast_path=True/False)，
       比较耗时和两次执行的状态变化。

指标只算总体指标：
  主意图准确率、多意图完全匹配率（子意图集合和执行方式都一致）、快速通道覆盖率与误命中率、
  延迟 P50 / P95（最近秩法）、状态一致率。
调用模型的组按 repeats 重复，准确率取各遍均值，延迟按全部遍的耗时一起算分位数；
只关键词和快速通道匹配结果确定，只跑 1 遍。

本模块只放评测逻辑和报告生成，入口见 运行评测.py；全部串行执行，避免并发影响延迟。
"""
import hashlib
import json
import math
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agents.agent_orchestrator import Request
from core.fast_path import FastPath
from core.intent_recognizer import MAX_SUB_TASKS, IntentCategory
from vehicle.simulator import apply_scenario

SAMPLES_PATH = Path(__file__).resolve().parent / "意图样本.json"
REPORT_DIR = Path(__file__).resolve().parent / "报告"   # 正式报告，GET /eval/report 读取其中的 report.json
E2_SCENARIO = "parked"
EXECUTIONS = ("parallel", "sequential")
_INTENTS = {c.value for c in IntentCategory}

# E1 三个对照组：键名 → 报告里的中文名
E1_CONFIGS = {
    "keyword_only": "只关键词",
    "llm_only": "只 LLM",
    "layered": "关键词 + LLM",
}


# ── 样本 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sample:
    id: str
    text: str
    intent: str
    sub_tasks: Tuple[Tuple[str, str], ...]   # 多意图样本的 (intent, text)，单意图为空
    execution: str
    fast_path: bool
    note: str = ""

    @property
    def is_multi(self) -> bool:
        return bool(self.sub_tasks)

    @property
    def expected_intents(self) -> frozenset:
        """标注的意图集合：多意图为子意图集合，单意图为主意图。"""
        return frozenset(i for i, _ in self.sub_tasks) if self.sub_tasks else frozenset({self.intent})


def load_samples(path: Path = SAMPLES_PATH) -> List[Sample]:
    """读取并校验样本文件。"""
    return parse_samples(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_samples(raw: Any) -> List[Sample]:
    """校验样本数组；任何一条不合法都抛 ValueError（带样本 id）。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("样本文件应为非空数组")
    samples, seen = [], set()
    for idx, item in enumerate(raw):
        sid = item.get("id") if isinstance(item, dict) else None
        label = sid or f"第 {idx + 1} 条"
        try:
            samples.append(_parse_sample(item))
        except (TypeError, ValueError, KeyError) as ex:
            raise ValueError(f"样本 {label}：{ex}") from None
        if sid in seen:
            raise ValueError(f"样本 id 重复：{sid}")
        seen.add(sid)
    return samples


def _parse_sample(item: Dict[str, Any]) -> Sample:
    sid, text, intent = item["id"], item["text"], item["intent"]
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("id 不能为空")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 不能为空")
    if intent not in _INTENTS:
        raise ValueError(f"未知意图 {intent!r}")
    execution = item.get("execution", "parallel")
    if execution not in EXECUTIONS:
        raise ValueError(f"execution 只能是 {' / '.join(EXECUTIONS)}")
    fast_path = item.get("fast_path", False)
    if not isinstance(fast_path, bool):
        raise ValueError("fast_path 应为布尔值")

    sub_tasks = []
    for task in item.get("sub_tasks") or []:
        if task.get("intent") not in _INTENTS:
            raise ValueError(f"子任务意图未知 {task.get('intent')!r}")
        if not str(task.get("text", "")).strip():
            raise ValueError("子任务 text 不能为空")
        sub_tasks.append((task["intent"], task["text"]))
    if sub_tasks:
        if not 2 <= len(sub_tasks) <= MAX_SUB_TASKS:
            raise ValueError(f"多意图样本应有 2–{MAX_SUB_TASKS} 个子任务")
        if len({i for i, _ in sub_tasks}) < 2:
            raise ValueError("多意图样本的子任务至少涉及 2 个意图")
        if intent not in {i for i, _ in sub_tasks}:
            raise ValueError("主意图应是子任务意图之一")
        if fast_path:
            raise ValueError("多意图样本不应标 fast_path")
    elif execution != "parallel":
        raise ValueError("单意图样本的 execution 只能是 parallel")
    return Sample(sid, text, intent, tuple(sub_tasks), execution, fast_path, item.get("note", ""))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── 指标 ──────────────────────────────────────────────────────────────────────

def intent_accuracy(samples: Sequence[Sample], preds: Dict[str, Dict[str, Any]]) -> float:
    """主意图准确率。preds 按样本 id 索引，每项至少含 intent。"""
    correct = sum(preds[s.id]["intent"] == s.intent for s in samples)
    return correct / len(samples) if samples else 0.0


def multi_match_rate(samples: Sequence[Sample], preds: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """多意图完全匹配率：子意图集合和执行方式都与标注一致；没有多意图样本时为 None。"""
    multi = [s for s in samples if s.is_multi]
    if not multi:
        return None
    ok = sum(
        frozenset(preds[s.id]["intents"]) == s.expected_intents and preds[s.id]["execution"] == s.execution
        for s in multi
    )
    return ok / len(multi)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """最近秩法分位数：排序后取第 ceil(p × n) 个；空序列为 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(values: Sequence[float]) -> Dict[str, Optional[float]]:
    p50, p95 = percentile(values, 50), percentile(values, 95)
    return {
        "p50_ms": round(p50, 2) if p50 is not None else None,
        "p95_ms": round(p95, 2) if p95 is not None else None,
        "count": len(values),
    }


def fast_path_rates(samples: Sequence[Sample], hits: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """快速通道覆盖率与误命中率；hits 为样本 id → 命中的规则 ID（未命中为 None）。"""
    should = [s for s in samples if s.fast_path]
    should_not = [s for s in samples if not s.fast_path]
    missed = [s.id for s in should if not hits.get(s.id)]
    false_hits = [s.id for s in should_not if hits.get(s.id)]
    return {
        "coverage": (len(should) - len(missed)) / len(should) if should else None,
        "false_trigger": len(false_hits) / len(should_not) if should_not else None,
        "fast_path_samples": len(should),
        "missed": missed,
        "false_hits": false_hits,
    }


def changes_signature(changes: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """state_changes 归一成 字段 → 新值（JSON 串），用于比较两次执行的结果。"""
    return {c.get("field"): json.dumps(c.get("new"), ensure_ascii=False, sort_keys=True) for c in changes}


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), 4) if values else None


# ── E1 分层消融 ───────────────────────────────────────────────────────────────

def prediction(result: Any) -> Dict[str, Any]:
    """IntentResult → 评测用的预测记录。intents 为预测的意图集合（有子任务时取子任务意图）。"""
    intent = result.intent.value
    intents = sorted({t.intent.value for t in result.sub_tasks}) if result.sub_tasks else [intent]
    sources = result.sources or {}
    return {
        "intent": intent,
        "intents": intents,
        "execution": result.execution,
        "latency_ms": round(result.latency_ms, 2),
        "decided_by": sources.get("decided_by"),
        "llm_status": sources.get("llm_status"),
    }


async def run_recognizer(recognizer: Any, samples: Sequence[Sample]) -> Dict[str, Dict[str, Any]]:
    """识别器逐条串行识别，返回 样本 id → 预测记录。"""
    preds = {}
    for sample in samples:
        preds[sample.id] = prediction(await recognizer.recognize(sample.text))
    return preds


def summarize_runs(samples: Sequence[Sample], runs: List[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """多遍结果汇总：准确率取均值，延迟合并后算分位数，并列出错例（任意一遍错就列出）。"""
    per_run = [
        {"intent_accuracy": round(intent_accuracy(samples, r), 4), "multi_match": multi_match_rate(samples, r)}
        for r in runs
    ]
    latencies = [r[s.id]["latency_ms"] for r in runs for s in samples]
    llm_failures = sum(r[s.id]["llm_status"] in ("failed", "timeout") for r in runs for s in samples)

    intent_errors, multi_errors = [], []
    for s in samples:
        preds = [r[s.id] for r in runs]
        if any(p["intent"] != s.intent for p in preds):
            intent_errors.append({
                "id": s.id, "text": s.text, "expected": s.intent,
                "predicted": [p["intent"] for p in preds],
            })
        if s.is_multi and any(
            frozenset(p["intents"]) != s.expected_intents or p["execution"] != s.execution for p in preds
        ):
            multi_errors.append({
                "id": s.id, "text": s.text,
                "expected": {"intents": sorted(s.expected_intents), "execution": s.execution},
                "predicted": [{"intents": p["intents"], "execution": p["execution"]} for p in preds],
            })
    return {
        "intent_accuracy": _mean([m["intent_accuracy"] for m in per_run]),
        "multi_match": _mean([m["multi_match"] for m in per_run]),
        "latency": latency_summary(latencies),
        "repeats": len(runs),
        "llm_failures": llm_failures,
        "per_run": per_run,
        "intent_errors": intent_errors,
        "multi_errors": multi_errors,
    }


async def run_e1(
    samples: Sequence[Sample],
    recognizers: Dict[str, Any],
    repeats: int = 3,
    log: Any = print,
) -> Dict[str, Any]:
    """E1：每个识别器跑 repeats 遍（不调 LLM 的只跑 1 遍）。返回 配置 → {"summary", "runs"}。"""
    out = {}
    for name, recognizer in recognizers.items():
        times = repeats if getattr(recognizer, "use_llm", True) else 1
        runs = []
        for i in range(times):
            log(f"E1 {E1_CONFIGS.get(name, name)}：第 {i + 1}/{times} 遍")
            runs.append(await run_recognizer(recognizer, samples))
        out[name] = {"summary": summarize_runs(samples, runs), "runs": runs}
    return out


# ── E2 快速通道 ───────────────────────────────────────────────────────────────

def fast_path_hits(samples: Sequence[Sample], fast_path: FastPath) -> Dict[str, Optional[Dict[str, str]]]:
    """只做规则匹配（不执行工具）：样本 id → {"rule", "intent"}，未命中为 None。"""
    hits = {}
    for s in samples:
        match = fast_path.match(s.text)
        hits[s.id] = {"rule": match.rule_id, "intent": match.intent.value} if match else None
    return hits


def run_e2_intent(
    samples: Sequence[Sample],
    layered_runs: List[Dict[str, Dict[str, Any]]],
    hits: Dict[str, Optional[Dict[str, str]]],
) -> Dict[str, Any]:
    """E2①：关闭即 E1"关键词 + LLM"组；开启时命中样本改用规则意图（单意图、并行）。"""
    on_runs = []
    for run in layered_runs:
        merged = dict(run)
        for sid, hit in hits.items():
            if hit:
                merged[sid] = {**run[sid], "intent": hit["intent"], "intents": [hit["intent"]], "execution": "parallel"}
        on_runs.append(merged)
    rates = fast_path_rates(samples, {sid: (hit or {}).get("rule") for sid, hit in hits.items()})
    return {
        "off": {"intent_accuracy": _mean([intent_accuracy(samples, r) for r in layered_runs])},
        "on": {"intent_accuracy": _mean([intent_accuracy(samples, r) for r in on_runs])},
        **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in rates.items()},
        "hits": {sid: hit["rule"] for sid, hit in hits.items() if hit},
    }


async def run_e2_end_to_end(
    samples: Sequence[Sample],
    orchestrator: Any,
    repeats: int = 3,
    log: Any = print,
) -> Dict[str, Any]:
    """
    E2②：标 fast_path 的样本，每遍快速通道开、关各执行一次。
    每次换一个用户（eval:{id}:{on|off}:{遍}），也就是一辆新车，并套用 parked 场景；耗时只算 orchestrator.run。
    """
    targets = [s for s in samples if s.fast_path]
    records: List[Dict[str, Any]] = []
    for i in range(repeats):
        log(f"E2 端到端：第 {i + 1}/{repeats} 遍，{len(targets)} 条")
        for s in targets:
            for mode in ("off", "on"):
                user_id = f"eval:{s.id}:{mode}:{i + 1}"
                apply_scenario(orchestrator.vehicle.store, user_id, E2_SCENARIO)
                req = Request(message=s.text, user_id=user_id, conv_id=str(uuid.uuid4()))
                t0 = time.perf_counter()
                result = await orchestrator.run(req, fast_path=(mode == "on"))
                latency = (time.perf_counter() - t0) * 1000
                records.append({
                    "id": s.id, "repeat": i + 1, "mode": mode,
                    "route": result.route, "fast_rule": result.fast_rule,
                    "intent": result.intent.value if result.intent else None,
                    "latency_ms": round(latency, 2),
                    "response": result.response,
                    "state_changes": result.state_changes,
                    "blocked_actions": result.blocked_actions,
                })

    by_key = {(r["id"], r["repeat"], r["mode"]): r for r in records}
    pairs = [(by_key[(s.id, i + 1, "on")], by_key[(s.id, i + 1, "off")]) for i in range(repeats) for s in targets]
    consistent = [changes_signature(on["state_changes"]) == changes_signature(off["state_changes"]) for on, off in pairs]
    inconsistent = sorted({on["id"] for (on, _), same in zip(pairs, consistent) if not same})
    return {
        "samples": len(targets),
        "repeats": repeats,
        "scenario": E2_SCENARIO,
        "on": latency_summary([r["latency_ms"] for r in records if r["mode"] == "on"]),
        "off": latency_summary([r["latency_ms"] for r in records if r["mode"] == "off"]),
        "on_fast_ratio": round(
            sum(r["route"] == "fast" for r in records if r["mode"] == "on") / max(1, len(pairs)), 4
        ),
        "state_consistency": round(sum(consistent) / len(consistent), 4) if consistent else None,
        "inconsistent": inconsistent,
        "records": records,
    }


# ── 报告 ──────────────────────────────────────────────────────────────────────

def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _ms(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f} s"
    return f"{value:.2f} ms" if value < 1 else f"{value:.1f} ms"   # 关键词层、快速通道在 1 ms 以内


def build_markdown(report: Dict[str, Any]) -> str:
    """把 report.json 的内容整理成可读的 Markdown（两张实验表 + 错例）。"""
    ds, cfg, e1, e2 = report["dataset"], report["config"], report["e1"], report["e2"]
    git = report.get("git") or {}
    lines = [
        "# CabinWeave 意图识别评测报告",
        "",
        f"- 时间：{report['generated_at']}",
        f"- 样本：{ds['total']} 条（多意图 {ds['multi']} 条，应走快速通道 {ds['fast_path']} 条），SHA-256 `{ds['sha256'][:12]}`",
        f"- 模型：意图识别 `{cfg['intent_model']}`，其余角色 `{cfg['model']}`；调用模型的组各跑 {cfg['repeats']} 遍取均值",
        f"- 代码：`{git.get('commit', '未知')[:10]}`{'（有未提交改动）' if git.get('dirty') else ''}",
        "",
        "## E1 分层消融",
        "",
        "| 组 | 主意图准确率 | 多意图完全匹配率 | 延迟 P50 | 延迟 P95 |",
        "|---|---|---|---|---|",
    ]
    for key, label in E1_CONFIGS.items():
        if key in e1:
            s = e1[key]["summary"]
            lines.append(
                f"| {label} | {_pct(s['intent_accuracy'])} | {_pct(s['multi_match'])} "
                f"| {_ms(s['latency']['p50_ms'])} | {_ms(s['latency']['p95_ms'])} |"
            )
    failures = {E1_CONFIGS[k]: e1[k]["summary"]["llm_failures"] for k in E1_CONFIGS if k in e1}
    if any(failures.values()):
        lines += ["", "LLM 失败或超时次数：" + "，".join(f"{k} {v}" for k, v in failures.items() if v)]

    intent_part = e2["intent"]
    lines += [
        "",
        "## E2 快速通道",
        "",
        "| 快速通道 | 主意图准确率 | 端到端延迟 P50 | 端到端延迟 P95 |",
        "|---|---|---|---|",
    ]
    e2e = e2.get("end_to_end")
    for mode, label in (("on", "开"), ("off", "关")):
        p50 = _ms(e2e[mode]["p50_ms"]) if e2e else "—"
        p95 = _ms(e2e[mode]["p95_ms"]) if e2e else "—"
        lines.append(f"| {label} | {_pct(intent_part[mode]['intent_accuracy'])} | {p50} | {p95} |")
    lines += [
        "",
        f"- 快速通道覆盖率：{_pct(intent_part['coverage'])}（未命中：{'、'.join(intent_part['missed']) or '无'}）",
        f"- 快速通道误命中率：{_pct(intent_part['false_trigger'])}（误命中：{'、'.join(intent_part['false_hits']) or '无'}）",
    ]
    if e2e:
        lines += [
            f"- 状态一致率：{_pct(e2e['state_consistency'])}（不一致：{'、'.join(e2e['inconsistent']) or '无'}）",
            f"- 端到端样本 {e2e['samples']} 条，每遍开、关各一次，共 {e2e['repeats']} 遍；车辆场景 {e2e['scenario']}；"
            "延迟只算编排器，不含 HTTP 和记忆读写",
        ]

    lines += ["", "## 错例", ""]
    any_error = False
    for key, label in E1_CONFIGS.items():
        if key not in e1:
            continue
        summary = e1[key]["summary"]
        for err in summary["intent_errors"]:
            any_error = True
            counts = Counter(err["predicted"])
            got = "、".join(f"{k}×{v}" if len(err["predicted"]) > 1 else k for k, v in counts.items())
            lines.append(f"- [{label}] {err['id']}「{err['text']}」主意图应为 {err['expected']}，预测 {got}")
        for err in summary["multi_errors"]:
            any_error = True
            exp = err["expected"]
            got = "；".join(f"{'+'.join(p['intents'])}（{p['execution']}）" for p in err["predicted"])
            lines.append(
                f"- [{label}] {err['id']}「{err['text']}」应拆为 {'+'.join(exp['intents'])}（{exp['execution']}），预测 {got}"
            )
    if not any_error:
        lines.append("无")
    return "\n".join(lines) + "\n"
