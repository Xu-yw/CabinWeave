"""
CabinWeave 意图识别评测测试

覆盖：正式样本文件的数量与分组；样本校验（重复 id、未知意图、子任务不足、主意图不在子任务里等）；
各项指标（主意图准确率、多意图完全匹配率、最近秩分位数、快速通道覆盖率与误命中率、状态变化比较）；
E1 重复遍数（不调 LLM 的组只跑 1 遍）；E2 意图层面用规则意图替换命中样本；
E2 端到端的车辆准备、开关两种调用和状态一致率；Markdown 报告生成。

全部使用假识别器、假编排器，不访问网络、不调用模型。
"""
import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from core.fast_path import FastPath
from core.intent_recognizer import IntentCategory, SubTask
from evaluation.evaluator import (
    E1_CONFIGS,
    SAMPLES_PATH,
    build_markdown,
    changes_signature,
    fast_path_rates,
    fast_path_hits,
    intent_accuracy,
    load_samples,
    multi_match_rate,
    parse_samples,
    percentile,
    run_e1,
    run_e2_end_to_end,
    run_e2_intent,
)
from vehicle.service import VehicleService


def single(sid, text, intent, fast=False):
    return {"id": sid, "text": text, "intent": intent, "sub_tasks": [], "execution": "parallel", "fast_path": fast}


def multi(sid, text, tasks, execution="parallel"):
    return {
        "id": sid, "text": text, "intent": tasks[0][0], "execution": execution, "fast_path": False,
        "sub_tasks": [{"intent": i, "text": t} for i, t in tasks],
    }


def pred(intent, intents=None, execution="parallel", latency=1.0, status="ok"):
    return {"intent": intent, "intents": intents or [intent], "execution": execution,
            "latency_ms": latency, "decided_by": "llm", "llm_status": status}


# ── 正式样本 ──────────────────────────────────────────────────────────────────

def test_official_samples_shape():
    samples = load_samples(SAMPLES_PATH)
    assert len(samples) == 58
    assert sum(s.is_multi for s in samples) == 10
    counts = Counter(s.intent for s in samples if not s.is_multi)
    assert set(counts) == {c.value for c in IntentCategory}
    assert min(counts.values()) >= 2
    sizes = Counter(len(s.sub_tasks) for s in samples if s.is_multi)
    assert sizes == {2: 7, 3: 3}
    assert sum(s.execution == "sequential" for s in samples) == 3
    assert 10 <= sum(s.fast_path for s in samples) <= 14


# ── 样本校验 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("items, reason", [
    ([single("A", "打开空调", "climate_control"), single("A", "下一首", "media_control")], "重复"),
    ([single("A", "打开空调", "aircon")], "未知意图"),
    ([single("A", " ", "climate_control")], "text"),
    ([multi("A", "开空调再放歌", [("climate_control", "开空调")])], "2–"),
    ([multi("A", "开空调关车窗", [("climate_control", "开空调"), ("climate_control", "调温度")])], "2 个意图"),
    ([{**multi("A", "开空调放歌", [("climate_control", "开空调"), ("media_control", "放歌")]), "intent": "navigation"}], "主意图"),
    ([{**single("A", "打开空调", "climate_control"), "execution": "sequential"}], "parallel"),
    ([{**single("A", "打开空调", "climate_control"), "fast_path": "yes"}], "布尔"),
])
def test_invalid_samples_rejected(items, reason):
    with pytest.raises(ValueError, match=reason):
        parse_samples(items)


# ── 指标 ──────────────────────────────────────────────────────────────────────

def test_accuracy_and_multi_match():
    samples = parse_samples([
        single("A", "打开空调", "climate_control"),
        single("B", "下一首", "media_control"),
        multi("C", "搜充电站再导航过去", [("poi_search", "搜充电站"), ("navigation", "导航过去")], "sequential"),
        multi("D", "开空调放歌", [("climate_control", "开空调"), ("media_control", "放歌")]),
    ])
    preds = {
        "A": pred("climate_control"),
        "B": pred("phone_call"),
        "C": pred("poi_search", ["navigation", "poi_search"], "sequential"),
        "D": pred("climate_control", ["climate_control", "media_control"], "sequential"),  # 执行方式错
    }
    assert intent_accuracy(samples, preds) == 0.75
    assert multi_match_rate(samples, preds) == 0.5
    assert multi_match_rate(samples[:2], preds) is None


def test_percentile_nearest_rank():
    values = list(range(1, 21))          # 1..20
    assert percentile(values, 50) == 10
    assert percentile(values, 95) == 19
    assert percentile([5], 95) == 5
    assert percentile([], 50) is None


def test_fast_path_rates():
    samples = parse_samples([
        single("A", "打开空调", "climate_control", fast=True),
        single("B", "下一首", "media_control", fast=True),
        single("C", "空调开着吗", "vehicle_status"),
        single("D", "导航回家", "navigation"),
    ])
    rates = fast_path_rates(samples, {"A": "F01", "B": None, "C": "F01", "D": None})
    assert rates["coverage"] == 0.5 and rates["missed"] == ["B"]
    assert rates["false_trigger"] == 0.5 and rates["false_hits"] == ["C"]


def test_changes_signature_ignores_old_values_and_order():
    a = [{"field": "climate.on", "old": False, "new": True}, {"field": "climate.temp", "old": 24, "new": 22}]
    b = [{"field": "climate.temp", "old": 26, "new": 22}, {"field": "climate.on", "old": False, "new": True}]
    assert changes_signature(a) == changes_signature(b)
    assert changes_signature(a) != changes_signature(a[:1])


# ── E1 ────────────────────────────────────────────────────────────────────────

class FakeRecognizer:
    """按文本返回固定意图的假识别器。"""

    def __init__(self, mapping, use_llm=True):
        self.mapping = mapping
        self.use_llm = use_llm
        self.calls = 0

    async def recognize(self, message, history=None):
        self.calls += 1
        intents = self.mapping[message]
        sub_tasks = [SubTask(IntentCategory(i), message) for i in intents] if len(intents) > 1 else []
        return SimpleNamespace(
            intent=IntentCategory(intents[0]), sub_tasks=sub_tasks, execution="parallel", latency_ms=2.0,
            sources={"decided_by": "llm", "llm_status": "ok" if self.use_llm else "skipped"},
        )


def test_run_e1_repeats_only_llm_configs():
    samples = parse_samples([
        single("A", "打开空调", "climate_control"),
        multi("B", "开空调放歌", [("climate_control", "开空调"), ("media_control", "放歌")]),
    ])
    keyword = FakeRecognizer({"打开空调": ["climate_control"], "开空调放歌": ["climate_control"]}, use_llm=False)
    layered = FakeRecognizer({"打开空调": ["navigation"], "开空调放歌": ["climate_control", "media_control"]})
    out = asyncio.run(run_e1(samples, {"keyword_only": keyword, "layered": layered}, repeats=3, log=lambda _: None))

    assert keyword.calls == 2 and layered.calls == 6
    assert out["keyword_only"]["summary"]["repeats"] == 1
    assert out["keyword_only"]["summary"]["intent_accuracy"] == 1.0
    assert out["keyword_only"]["summary"]["multi_match"] == 0.0
    assert out["layered"]["summary"]["intent_accuracy"] == 0.5
    assert out["layered"]["summary"]["multi_match"] == 1.0
    assert out["layered"]["summary"]["intent_errors"][0]["predicted"] == ["navigation"] * 3
    assert out["layered"]["summary"]["latency"] == {"p50_ms": 2.0, "p95_ms": 2.0, "count": 6}


# ── E2 ────────────────────────────────────────────────────────────────────────

def test_run_e2_intent_replaces_hit_predictions():
    samples = parse_samples([
        single("A", "打开空调", "climate_control", fast=True),
        single("B", "空调开着吗", "vehicle_status"),
    ])
    hits = fast_path_hits(samples, FastPath())
    assert hits["A"] == {"rule": "F01", "intent": "climate_control"} and hits["B"] is None

    layered_runs = [{"A": pred("body_control"), "B": pred("vehicle_status")}]
    out = run_e2_intent(samples, layered_runs, hits)
    assert out["off"]["intent_accuracy"] == 0.5
    assert out["on"]["intent_accuracy"] == 1.0
    assert out["coverage"] == 1.0 and out["false_trigger"] == 0.0
    assert out["hits"] == {"A": "F01"}


class FakeOrchestrator:
    """快速通道开时把空调温度设为 22，关时设为 23，用来制造一次状态不一致。"""

    def __init__(self):
        self.vehicle = VehicleService()
        self.calls = []

    async def run(self, req, *, fast_path=True):
        self.calls.append((req.user_id, fast_path))
        assert self.vehicle.store.snapshot(req.user_id)["speed_kmh"] == 0   # 已套用 parked 场景
        new = 22 if fast_path or "空调" not in req.message else 23
        return SimpleNamespace(
            route="fast" if fast_path else "slow", fast_rule="F02" if fast_path else None,
            intent=IntentCategory.CLIMATE_CONTROL, response="好的",
            state_changes=[{"field": "climate.temp", "old": 24, "new": new}], blocked_actions=[],
        )


def test_run_e2_end_to_end():
    samples = parse_samples([
        single("A", "空调调到22度", "climate_control", fast=True),
        single("B", "温度调到22度", "climate_control", fast=True),
        single("C", "导航回家", "navigation"),
    ])
    orch = FakeOrchestrator()
    out = asyncio.run(run_e2_end_to_end(samples, orch, repeats=2, log=lambda _: None))

    assert len(orch.calls) == 8                      # 2 条 × 2 遍 × 开关
    assert {uid for uid, _ in orch.calls} >= {"eval:A:on:1", "eval:A:off:1", "eval:B:off:2"}
    assert out["samples"] == 2 and out["on"]["count"] == 4 and out["off"]["count"] == 4
    assert out["state_consistency"] == 0.5 and out["inconsistent"] == ["A"]
    assert out["on_fast_ratio"] == 1.0


def test_build_markdown_contains_tables():
    summary = {
        "intent_accuracy": 0.9, "multi_match": 0.8, "latency": {"p50_ms": 1200.0, "p95_ms": 2500.0, "count": 3},
        "repeats": 3, "llm_failures": 1, "per_run": [],
        "intent_errors": [{"id": "S01", "text": "x", "expected": "climate_control", "predicted": ["body_control"] * 3}],
        "multi_errors": [],
    }
    report = {
        "generated_at": "2026-11-01T12:00:00+08:00",
        "dataset": {"total": 60, "multi": 10, "fast_path": 11, "sha256": "ab" * 32},
        "config": {"repeats": 3, "intent_model": "m1", "model": "m2"},
        "git": {"commit": "1234567890abc", "dirty": True},
        "e1": {key: {"summary": summary} for key in E1_CONFIGS},
        "e2": {
            "intent": {"on": {"intent_accuracy": 0.92}, "off": {"intent_accuracy": 0.9},
                       "coverage": 0.9, "false_trigger": 0.0, "missed": ["S09"], "false_hits": []},
            "end_to_end": {"on": {"p50_ms": 3.5, "p95_ms": 8.0}, "off": {"p50_ms": 3500.0, "p95_ms": 5000.0},
                           "state_consistency": 1.0, "inconsistent": [], "samples": 11, "repeats": 3,
                           "scenario": "parked"},
        },
    }
    md = build_markdown(report)
    assert "| 关键词 + LLM | 90.0% | 80.0% | 1.20 s | 2.50 s |" in md
    assert "| 开 | 92.0% | 3.5 ms | 8.0 ms |" in md
    assert "未命中：S09" in md and "（有未提交改动）" in md
    assert "S01「x」主意图应为 climate_control，预测 body_control×3" in md
