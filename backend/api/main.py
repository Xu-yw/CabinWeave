"""
CabinWeave 智能座舱多 Agent 交互系统 — FastAPI 入口与命令行

启动时打印小熊饼干图案。所有核心组件在 lifespan 中初始化，通过环境变量配置（完整列表见仓库根目录 .env.example）。

接口：
  POST /chat               主对话：快速通道 → 记忆读取 → orchestrator.run(fast_path=False) → 记忆写入；
                           一个用户一辆车（按 user_id 定位），响应带所走通道、子任务、
                           状态变化、被拒绝的操作和执行后的完整车辆状态
  GET  /vehicle/state      查看车辆状态
  POST /vehicle/state      局部修改状态，模拟车速、电量、故障灯等（不经过安全规则），返回叶子字段变化
  GET  /vehicle/scenarios  可选场景预设和默认场景
  POST /vehicle/scenario   套用场景预设
  POST /vehicle/reset      重置为默认场景
                           车辆接口的逻辑在 vehicle/simulator.py，路由只做参数转换；
                           user_id 为空、未知字段、取值非法或未知场景返回 400
  GET  /profile            某个用户规整后的驾驶员偏好 {preferences, places, notes}
  GET  /eval/report        意图评测的正式报告（evaluation/报告/report.json），没有时 404
  其他：/health、/skills、/skills/reload、/monitor、/metrics、/trace/*、/search、/knowledge/*
  服务未就绪时返回 503。

命令行（python api/main.py --cli）：流程与 /chat 一致，每轮回复后输出所走通道，
有状态变化或被拒绝的操作时再各输出一行。

本文件直接读取的环境变量：
  ANTHROPIC_API_KEY、ANTHROPIC_BASE_URL、ANTHROPIC_MODEL      模型凭据与默认模型
  CABINWEAVE_SKILLS_DIR                Skills 目录，默认 backend/skills
  CABINWEAVE_SKILLS_MAX_PROMPT_CHARS   一次请求注入的 Skill 合计字符上限，默认 5000
  CABINWEAVE_MANUALS_DIR               车辆用户手册目录，默认 backend/manuals
  REDIS_URL、CHROMA_HOST、CHROMA_PORT、CHROMA_PERSIST_DIRECTORY   记忆与知识库存储
  PROMETHEUS_PORT、MONITOR_INTERVAL、ALERT_WEBHOOK_URL            监控
  LOG_LEVEL、API_HOST、API_PORT、APP_ENV                          日志与直接运行时的监听配置
"""
import asyncio
import json
import logging
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from vehicle import simulator

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   CabinWeave  v2.0   ║
   ║  车载多 Agent 助手   ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_skill_manager = None

def _anthropic_cfg() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _skill_manager

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from agents.tools import build_rag_tools
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("CABINWEAVE_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("CABINWEAVE_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
    )

    # 记忆管理器（Redis 工作记忆 + ChromaDB 情景记忆/驾驶员偏好）
    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )
    # 车辆用户手册：启动时按目录同步（新增、变更、删除），目录不存在或为空时知识库可以为空
    manuals_dir = os.getenv("CABINWEAVE_MANUALS_DIR", str(pathlib.Path(_ROOT) / "manuals"))
    kb = KnowledgeBase(
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        manuals_dir=manuals_dir,
    )
    logger.info(f"知识库已加载: {await kb.doc_count_async()} 个文档片段")

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "手册检索降级结果",
            "content": f"车辆用户手册检索暂时不可用，没能查到“{query}”的相关内容，请以车辆用户手册为准。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="检索车辆用户手册（基于 ChromaDB 向量检索）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))
    if _orchestrator is not None:
        _orchestrator.register_tools(build_rag_tools(_tool_manager))

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    logger.info("CabinWeave 已就绪")
    yield

    await _monitor.stop()
    if _memory is not None:
        await _memory.close()
    logger.info("CabinWeave 已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="CabinWeave 智能座舱多 Agent 交互系统",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str
    user_id:     str = "anonymous"
    conv_id:     Optional[str] = None


class ChatResponse(BaseModel):
    conv_id:     str
    request_id:  str = ""
    response:    str
    intent:      str
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)   # 参与处理的角色，按子任务顺序
    tools_used: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    intent_confidence_level: str = "none"
    intent_sources: Dict[str, Any] = Field(default_factory=dict)
    sub_tasks: List[Dict[str, str]] = Field(default_factory=list)
    execution: str = "parallel"
    assignments: Dict[str, List[Dict[str, str]]] = Field(default_factory=dict)
    route: str = "slow"                  # "fast" 快速通道 / "slow" 完整链路
    fast_rule: Optional[str] = None      # 快速通道命中的规则 ID
    state_changes: List[Dict[str, Any]] = Field(default_factory=list)    # [{"field", "old", "new"}]
    blocked_actions: List[Dict[str, Any]] = Field(default_factory=list)  # [{"tool_name", "rule_id", "reason"}]
    vehicle_state: Dict[str, Any] = Field(default_factory=dict)          # 执行后的完整车辆状态


class VehicleTarget(BaseModel):
    """车辆接口的目标车辆：一个用户一辆车，按 user_id 定位。"""
    user_id: str = "anonymous"


class VehicleStatePatchInput(VehicleTarget):
    """局部修改车辆状态，patch 按嵌套对象写，如 {"speed_kmh": 60, "warning_lights": ["胎压报警"]}。"""
    patch: Dict[str, Any]


class VehicleScenarioInput(VehicleTarget):
    """套用场景预设，可选值见 GET /vehicle/scenarios。"""
    scenario: str


class ToolTraceResponse(BaseModel):
    request_id: str
    found: bool
    trace: Dict[str, Any] = Field(default_factory=dict)


class RecentToolTracesResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills():
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    主对话接口：
      ① 快速通道：读取记忆之前先试，命中时执行并模板播报，跳过记忆检索和偏好提炼，
         只把用户和助手两条消息写入对话记录（下一轮"再低一点"仍有上下文）
      ② 完整链路：记忆读取 → 意图识别 → Agent 路由 → 执行 → 记忆写入 → 异步提炼驾驶员偏好；
         规整后的驾驶员偏好（含家和公司地址）经 Request.profile 传给工具，编排器不再重复尝试快速通道
    车辆按 user_id 定位，两条通道操作同一辆车；user_id 为空时返回 400。
    响应里的 route（fast / slow）和 fast_rule 标明走了哪条通道，state_changes、blocked_actions 是本轮的
    状态变化和被安全规则拒绝的操作，vehicle_state 是执行后的完整车辆状态。
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    from agents.agent_orchestrator import Request as OrcReq
    from memory.conversation_memory import MsgRole

    if not req.user_id.strip():
        raise HTTPException(400, "user_id 不能为空")
    conv_id = req.conv_id or str(uuid.uuid4())

    # 1. 快速通道：命中时只写对话记录，不读记忆、不提炼偏好
    fast_result = await _orchestrator.try_fast_path(OrcReq(
        message=req.message, user_id=req.user_id, conv_id=conv_id,
    ))
    if fast_result is not None:
        await _memory.add_message(req.user_id, conv_id, MsgRole.USER, req.message)
        await _memory.add_message(req.user_id, conv_id, MsgRole.ASSISTANT, fast_result.response)
        return _chat_response(conv_id, fast_result, _orchestrator.vehicle.snapshot(req.user_id))

    # 2. 读取记忆上下文
    mem_ctx = await _memory.get_context(req.user_id, conv_id, query=req.message)

    # 3. 构建编排请求（含对话历史，用于意图识别上下文）
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    full_context = mem_ctx.to_prompt_text()

    orch_req = OrcReq(
        message=req.message,
        user_id=req.user_id,
        conv_id=conv_id,
        context=full_context,
        history=history,
        profile=mem_ctx.user_profile,
    )

    # 4. 执行完整链路（上面已经试过快速通道）
    result = await _orchestrator.run(orch_req, fast_path=False)

    # 5. 写入记忆
    await _memory.add_message(req.user_id, conv_id, MsgRole.USER, req.message)
    await _memory.add_message(req.user_id, conv_id, MsgRole.ASSISTANT, result.response)

    # 6. 异步提炼驾驶员偏好（不阻塞响应）
    asyncio.create_task(_memory.update_profile(req.user_id, conv_id))

    return _chat_response(conv_id, result, _orchestrator.vehicle.snapshot(req.user_id))


def _chat_response(conv_id: str, result: Any, vehicle_state: Dict[str, Any]) -> ChatResponse:
    """把编排结果（快速通道或完整链路）和执行后的车辆状态转成 /chat 的响应。"""
    intent_result = result.intent_result
    return ChatResponse(
        conv_id=conv_id,
        request_id=result.request_id,
        response=result.response,
        intent=result.intent.value if result.intent else "other",
        agent_type=result.agent_type.value,
        agent_types=[agent_type.value for agent_type in result.agent_types],
        tools_used=result.tools_used,
        routing_reason=result.routing_reason,
        escalated=result.escalated,
        latency_ms=round(result.latency_ms, 1),
        knowledge_used="search_knowledge_base" in result.tools_used,
        intent_confidence_level=intent_result.confidence_level if intent_result else "none",
        intent_sources=intent_result.sources if intent_result else {},
        sub_tasks=result.sub_tasks,
        execution=result.execution,
        assignments=result.assignments,
        route=result.route,
        fast_rule=result.fast_rule,
        state_changes=list(result.state_changes),
        blocked_actions=list(result.blocked_actions),
        vehicle_state=vehicle_state,
    )


# ── 车辆（模拟控制，逻辑在 vehicle/simulator.py）──────────────────────────────
def _vehicle_store():
    """编排器持有的车辆模拟存储；服务未就绪时 503。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return _orchestrator.vehicle.store


def _vehicle_call(op: Callable[..., Any], *args: Any) -> Any:
    """调用 vehicle/simulator.py 的函数，把 ValueError（中文原因）转成 400。"""
    try:
        return op(*args)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/vehicle/state", tags=["车辆"])
async def get_vehicle_state(user_id: str = "anonymous"):
    """查看车辆状态，返回 {"state"}。"""
    return _vehicle_call(simulator.read_state, _vehicle_store(), user_id)


@app.post("/vehicle/state", tags=["车辆"])
async def patch_vehicle_state(body: VehicleStatePatchInput):
    """
    局部修改车辆状态，用来模拟车速、电量、故障灯等；不经过安全规则。

    返回 {"state", "changes"}，changes 为叶子字段变化 [{"field", "old", "new"}]；
    未知字段或取值非法时返回 400，状态不变。
    """
    return _vehicle_call(simulator.patch_state, _vehicle_store(), body.user_id, body.patch)


@app.get("/vehicle/scenarios", tags=["车辆"])
async def list_vehicle_scenarios():
    """可选的场景预设和默认场景，返回 {"default_scenario", "scenarios": [{"name", "label", "overrides"}]}。"""
    return simulator.list_scenarios(_vehicle_store())


@app.post("/vehicle/scenario", tags=["车辆"])
async def apply_vehicle_scenario(body: VehicleScenarioInput):
    """整车重置为场景预设，返回 {"scenario", "state", "changes"}；未知场景返回 400。"""
    return _vehicle_call(simulator.apply_scenario, _vehicle_store(), body.user_id, body.scenario)


@app.post("/vehicle/reset", tags=["车辆"])
async def reset_vehicle(body: Optional[VehicleTarget] = None):
    """重置为默认场景（CABINWEAVE_VEHICLE_DEFAULT_SCENARIO），返回格式同 /vehicle/scenario。"""
    body = body or VehicleTarget()
    return _vehicle_call(simulator.reset_vehicle, _vehicle_store(), body.user_id)


@app.get("/profile", tags=["记忆"])
async def driver_profile(user_id: str = "anonymous"):
    """查看驾驶员偏好（规整后的 preferences、places、notes，没有时为空对象）。"""
    if _memory is None:
        raise HTTPException(503, "服务未就绪")
    return {"user_id": user_id, "profile": await _memory.get_profile(user_id)}


@app.get("/eval/report", tags=["评测"])
async def eval_report():
    """意图评测的正式报告，由 evaluation/运行评测.py 生成后复制到 evaluation/报告/。"""
    from evaluation.evaluator import REPORT_DIR

    path = REPORT_DIR / "report.json"
    if not path.exists():
        raise HTTPException(404, "还没有正式评测报告")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/trace/tool/{request_id}", response_model=ToolTraceResponse)
async def get_tool_trace(request_id: str):
    """查看某次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    trace = _orchestrator.get_tool_trace(request_id)
    return ToolTraceResponse(
        request_id=request_id,
        found=trace is not None,
        trace=trace or {},
    )


@app.get("/trace/tools", response_model=RecentToolTracesResponse)
async def list_recent_tool_traces(limit: int = 20):
    """查看最近 N 次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return RecentToolTracesResponse(items=_orchestrator.get_recent_tool_traces(limit=limit))


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {"query": query, "results": result.data, "reranked": result.reranked}


class DocInput(BaseModel):
    """单篇文档输入。"""
    title:   str
    content: str


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput):
    """
    批量导入文档到知识库。

    文档按 markdown 标题和空行切片（每片不超过 500 字，带"【标题｜小节标题】"前缀）并存入 ChromaDB，
    ChromaDB 内置 Embedding 模型自动向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "冬季用车", "content": "## 低温续航\\n低温环境下电池活性下降，续航会比常温时短..."},
        {"title": "胎压监测", "content": "胎压报警灯亮起时，请降低车速并尽快检查胎压..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    count = await kb.add_documents_async([{"title": d.title, "content": d.content} for d in body.documents])
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.html` / `.htm`：提取正文（与手册导入同一个提取函数）后作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    from mcp.knowledge_base import decode_text, extract_document_text

    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    filename = file.filename or "unknown"

    if filename.lower().endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(decode_text(content))
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md / html：整个文件作为一篇文档，HTML 先提取正文
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": extract_document_text(filename, content)}]

    count = await kb.add_documents_async(docs)
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {"total_chunks": await kb.doc_count_async()}


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
def _cli_value(value: Any) -> str:
    """状态值按 JSON 写法输出（true / null / "文本"），和接口返回的一致。"""
    return json.dumps(value, ensure_ascii=False)


def _turn_summary(result: Any) -> List[str]:
    """CLI 每轮回复后的附加行：所走通道；有状态变化、被拒绝的操作时各加一行。"""
    route = f"快速通道 {result.fast_rule or ''}".strip() if result.route == "fast" else "完整链路"
    lines = [f"通道：{route}"]
    if result.state_changes:
        lines.append("状态变化：" + "；".join(
            f"{change.get('field')} {_cli_value(change.get('old'))} → {_cli_value(change.get('new'))}"
            for change in result.state_changes
        ))
    if result.blocked_actions:
        items = []
        for blocked in result.blocked_actions:
            head = " ".join(str(part) for part in (blocked.get("rule_id"), blocked.get("reason")) if part)
            items.append(f"{head}（{blocked.get('tool_name')}）")
        lines.append("被拒绝：" + "；".join(items))
    return lines


async def _cli():
    """
    命令行对话，流程与 /chat 一致：先试快速通道（命中时只写对话记录，不读记忆、不提炼偏好），
    未命中再读记忆、带驾驶员偏好调用 run(req, fast_path=False)，之后后台提炼偏好；退出前等提炼任务结束。
    user_id 固定为 cli_user，车辆状态也按它存取。
    """
    print(BANNER)
    print("CabinWeave CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("CABINWEAVE_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("CABINWEAVE_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())
    profile_tasks: set = set()   # 后台偏好提炼任务

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        # 1. 快速通道：命中时只写对话记录
        result = await orch.try_fast_path(Request(message=msg, user_id=user_id, conv_id=conv_id))
        fast = result is not None
        if not fast:
            # 2. 完整链路：读取记忆 → 编排器（上面已经试过快速通道）
            ctx = await mem.get_context(user_id, conv_id, query=msg)
            history = [
                {"role": m.role.value, "content": m.content}
                for m in ctx.recent_messages[-5:]
            ] if ctx.recent_messages else None
            req = Request(
                message=msg, user_id=user_id, conv_id=conv_id,
                context=ctx.to_prompt_text(), history=history, profile=ctx.user_profile,
            )
            result = await orch.run(req, fast_path=False)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)
        if not fast:
            task = asyncio.create_task(mem.update_profile(user_id, conv_id))
            profile_tasks.add(task)
            task.add_done_callback(profile_tasks.discard)

        print(f"\nCabinWeave [{result.agent_type.value}]: {result.response}")
        for line in _turn_summary(result):
            print(f"  {line}")
        print()

    if profile_tasks:
        await asyncio.gather(*profile_tasks, return_exceptions=True)
    await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
