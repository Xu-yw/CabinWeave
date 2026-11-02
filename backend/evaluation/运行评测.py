"""
CabinWeave 意图识别评测入口

在仓库根目录运行：
  python backend/evaluation/运行评测.py [--repeats 3] [--samples 路径] [--skip-e2e]

- 读取仓库根目录 .env 的模型配置（ANTHROPIC_API_KEY、ANTHROPIC_BASE_URL、ANTHROPIC_MODEL，
  意图识别可用 CABINWEAVE_INTENT_MODEL 单独配模型），.env 优先于进程里已有的同名环境变量，
  避免误用外层工具注入的配置；不需要启动 Docker、Redis 或 ChromaDB。
- 识别器和编排器内部的识别器都关闭缓存，快速通道强制开启；全部串行执行。
- 结果写到 backend/evaluation/结果/<时间戳>/：report.json（配置、样本哈希、git 提交、指标、逐条预测）
  和 报告.md。正式一轮复制到 backend/evaluation/报告/ 后提交。
--skip-e2e 跳过 E2 端到端部分（只跑识别器，调试用）。
"""
import argparse
import asyncio
import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parent
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path: pathlib.Path, override: bool = False) -> None:
        """没装 python-dotenv 时的简单读取：逐行 KEY=VALUE，跳过注释。"""
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#") and (override or key.strip() not in os.environ):
                os.environ[key.strip()] = value.strip().strip("\"'")

load_dotenv(ROOT / ".env", override=True)
os.environ["CABINWEAVE_INTENT_CACHE_SIZE"] = "0"   # 编排器内部的识别器同样不缓存，重复遍才是真实调用

from agents.agent_orchestrator import AgentOrchestrator
from core.intent_recognizer import IntentRecognizer
from core.skill_loader import SkillManager
from evaluation.evaluator import (
    E2_SCENARIO,
    SAMPLES_PATH,
    build_markdown,
    fast_path_hits,
    file_sha256,
    load_samples,
    run_e1,
    run_e2_end_to_end,
    run_e2_intent,
)


def _git_info() -> dict:
    """当前提交和工作区是否有未提交改动；git 不可用时返回空值。"""
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "", "dirty": None}


def _model_cfg() -> dict:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise SystemExit("未设置 ANTHROPIC_API_KEY（读取的是仓库根目录 .env）")
    cfg = {"api_key": key, "model": os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip()}
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


async def main(args: argparse.Namespace) -> pathlib.Path:
    samples_path = pathlib.Path(args.samples)
    samples = load_samples(samples_path)
    cfg = _model_cfg()

    def recognizer(**layers) -> IntentRecognizer:
        return IntentRecognizer(api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
                                cache_size=0, **layers)

    recognizers = {
        "keyword_only": recognizer(use_llm=False),
        "llm_only": recognizer(use_keywords=False),
        "layered": recognizer(),
    }
    layered = recognizers["layered"]

    e1 = await run_e1(samples, recognizers, repeats=args.repeats)

    orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager(),
    )
    orchestrator.fast_path.enabled = True
    hits = fast_path_hits(samples, orchestrator.fast_path)
    e2 = {"intent": run_e2_intent(samples, e1["layered"]["runs"], hits)}
    if not args.skip_e2e:
        e2["end_to_end"] = await run_e2_end_to_end(samples, orchestrator, repeats=args.repeats)

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset": {
            "path": samples_path.resolve().relative_to(ROOT).as_posix()
            if samples_path.resolve().is_relative_to(ROOT) else str(samples_path),
            "sha256": file_sha256(samples_path),
            "total": len(samples),
            "multi": sum(s.is_multi for s in samples),
            "fast_path": sum(s.fast_path for s in samples),
        },
        "config": {
            "repeats": args.repeats,
            "intent_model": layered.model,
            "model": cfg["model"],
            "intent_timeout_s": layered.timeout_s,
            "e2_scenario": E2_SCENARIO,
        },
        "git": _git_info(),
        "e1": e1,
        "e2": e2,
    }

    out_dir = HERE / "结果" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = build_markdown(report)
    (out_dir / "报告.md").write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    print(f"结果已写入 {out_dir}")
    return out_dir


def _skill_manager() -> SkillManager:
    manager = SkillManager(
        root_dir=os.getenv("CABINWEAVE_SKILLS_DIR", str(BACKEND / "skills")),
        max_prompt_chars=int(os.getenv("CABINWEAVE_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    manager.load()
    return manager


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台默认 GBK，统一按 UTF-8 输出
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="CabinWeave 意图识别评测（E1 分层消融、E2 快速通道开关）")
    parser.add_argument("--repeats", type=int, default=3, help="调用模型的组重复遍数，默认 3")
    parser.add_argument("--samples", default=str(SAMPLES_PATH), help="样本文件，默认 backend/evaluation/意图样本.json")
    parser.add_argument("--skip-e2e", action="store_true", help="跳过 E2 端到端部分")
    parsed = parser.parse_args()
    if parsed.repeats < 1:
        parser.error("--repeats 至少为 1")
    asyncio.run(main(parsed))
