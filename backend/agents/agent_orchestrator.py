"""
CabinWeave 车载多 Agent 路由与编排

五个角色：
  - vehicle_control 车控：空调、座椅、车身、灯光、驾驶模式
  - navigation      导航：目的地、路线、按类别找地点
  - advisor         用车顾问：车况、故障提示、用车知识（唯一挂 RAG 的角色）
  - cabin           座舱助手：影音、电话、闲聊、追问澄清
  - safety          安全节点：不调模型，处理紧急求助、道路救援；经车辆服务开双闪、静止时解锁车门

快慢双通道：
  ① 快速通道：run() 入口先调 try_fast_path，core/fast_path.py 的规则整句命中简单车控 / 媒体指令时，
     调用注册表里同名工具的 handler（和 Agent 用的是同一个函数，同样过安全规则），按工具结果模板播报，
     0 次模型调用，结果 route="fast"、fast_rule 为命中的规则 ID；工具返回 error / unsupported 时回到完整链路
  ② 完整链路：意图识别 → 路由决策 → 执行 → 汇总，结果 route="slow"（含网络兜底回复）
  API 层在读取记忆之前已经试过快速通道时，调用 run(req, fast_path=False) 避免重复尝试；
  get_route_stats() 返回两条通道的请求数、快速通道占比和各规则命中次数

路由（只看意图识别结果，不区分来自 LLM 还是关键词兜底）：
  1. 安全优先：主意图或任一子任务是安全类 → 只派给安全节点，其余子任务暂缓并在回复里说明
  2. 意图映射：INTENT_AGENT 把 14 个意图映射到角色，子任务按映射分给各角色，
     同一角色的多个子任务合并成一次派发
  3. 失败：角色处理失败时直接播报按分到的意图生成的失败短句（failure_reply），不转交其他角色
  LLM 失败且关键词层也没有候选时，不调任何模型，直接回复"网络不太好，请稍后再试。"

执行：
  - parallel：各角色同时执行（asyncio.gather）
  - sequential：按子任务顺序逐个执行，前面角色的回复和工具结果放进后一个的 previous_results
  - 每个角色拿到自己的角色输入包：分到的子任务、完整原话、执行方式、上游结果、按角色裁剪的车辆快照，
    有驾驶员偏好时再加 driver_preferences（只含 preferences 和 notes，家和公司地址只由工具从 Request.profile 读取）
  - 工具白名单：编排器维护工具注册表，各角色只能看到和调用自己 tool_scope 内的工具；
    构造时注册全部角色工具（agents/tools.py 的 build_agent_tools，绑定 self.vehicle）和 RAG 工具
  - 工具循环：每次处理最多 MAX_TOOL_ROUNDS 次模型调用

车辆状态：
  - 编排器持有 VehicleService（self.vehicle），一个用户一辆车，车辆状态按 user_id 存取
  - 调用方没传 vehicle_state 时从车辆服务取快照：并行时各角色用同一时刻的快照，
    串行时每个角色派发前重新取一次（上一个角色可能改了状态）；调用方显式传入的保持原样
  - 状态变化汇总：工具返回 changes 时并入 state_changes，返回 blocked 时并入 blocked_actions，
    安全节点和快速通道的车辆操作同样汇总；角色失败时保留它失败前已产生的变化

思考模式：车控、导航、座舱助手关闭（请求带 thinking: disabled），用车顾问保留。

汇总分级（只有一个角色时直接返回它的回复，不经过汇总节点）：
  - 参与汇总的子任务全是执行类 → 模板拼接，不调模型
  - 含问答类 → 调模型合并成不超过 3 句的播报
  - 模型失败 → 按顺序确定性拼接
  - 失败的角色不交给模型改写，失败短句按顺序接在成功部分后面

环境变量：
  CABINWEAVE_FAST_PATH                 快速通道开关 on / off，默认 on（见 core/fast_path.py）
  CABINWEAVE_{AGENT_TYPE 大写}_MODEL   按角色覆盖模型，如 CABINWEAVE_CABIN_MODEL、CABINWEAVE_ADVISOR_MODEL
  CABINWEAVE_COMPOSER_MAX_TOKENS       汇总节点 max_tokens，默认 400
  CABINWEAVE_COMPOSER_TEMPERATURE      汇总节点温度，默认 0.1
  CABINWEAVE_TOOL_TRACE_MAX            内存中保留的请求级工具调用记录条数，默认 200
"""
import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from anthropic import AsyncAnthropic

from agents.tools import AgentToolSpec, build_agent_tools, build_rag_tools
from core.fast_path import FastMatch, FastPath
from core.intent_recognizer import (
    SAFETY_INTENTS,
    IntentCategory,
    IntentRecognizer,
    IntentResult,
    SubTask,
)
from core.llm_utils import THINKING_DISABLED, extract_text_content
from memory.conversation_memory import prompt_driver_preferences
from vehicle.service import VehicleService, make_command

logger = logging.getLogger(__name__)


# ── 角色与路由表 ──────────────────────────────────────────────────────────────

class AgentType(Enum):
    VEHICLE_CONTROL = "vehicle_control"  # 车控
    NAVIGATION      = "navigation"       # 导航
    ADVISOR         = "advisor"          # 用车顾问
    CABIN           = "cabin"            # 座舱助手
    SAFETY          = "safety"           # 安全节点（不调模型）


# 意图 → 角色
INTENT_AGENT: Dict[IntentCategory, AgentType] = {
    IntentCategory.CLIMATE_CONTROL: AgentType.VEHICLE_CONTROL,
    IntentCategory.BODY_CONTROL:    AgentType.VEHICLE_CONTROL,
    IntentCategory.DRIVE_SETTING:   AgentType.VEHICLE_CONTROL,
    IntentCategory.NAVIGATION:      AgentType.NAVIGATION,
    IntentCategory.POI_SEARCH:      AgentType.NAVIGATION,
    IntentCategory.VEHICLE_STATUS:  AgentType.ADVISOR,
    IntentCategory.FAULT_DIAGNOSIS: AgentType.ADVISOR,
    IntentCategory.MANUAL_QA:       AgentType.ADVISOR,
    IntentCategory.MEDIA_CONTROL:   AgentType.CABIN,
    IntentCategory.PHONE_CALL:      AgentType.CABIN,
    IntentCategory.EMERGENCY_SOS:   AgentType.SAFETY,
    IntentCategory.ROADSIDE_ASSIST: AgentType.SAFETY,
    IntentCategory.CHITCHAT:        AgentType.CABIN,
    IntentCategory.OTHER:           AgentType.CABIN,
}

# 执行类意图：参与汇总的子任务全是这类时模板拼接，不调模型
EXECUTION_INTENTS: frozenset = frozenset({
    IntentCategory.CLIMATE_CONTROL,
    IntentCategory.BODY_CONTROL,
    IntentCategory.DRIVE_SETTING,
    IntentCategory.MEDIA_CONTROL,
    IntentCategory.PHONE_CALL,
})

# 车辆快照按角色裁剪的顶层键；vehicle_state 里没有的键跳过
SNAPSHOT_KEYS: Dict[AgentType, Tuple[str, ...]] = {
    AgentType.VEHICLE_CONTROL: ("speed_kmh", "gear", "drive_mode", "climate", "seats", "body", "lights"),
    AgentType.NAVIGATION: ("location", "navigation", "battery_pct", "range_km"),
    AgentType.ADVISOR: (
        "speed_kmh", "gear", "drive_mode", "battery_pct", "range_km", "tire_pressure_kpa",
        "warning_lights", "next_service_km", "outside_temp_c",
        "climate", "seats", "body", "lights", "media",
    ),
    AgentType.CABIN: ("media", "speed_kmh"),
}


# 单次处理的最大模型调用轮数：导航可能先 search_poi 再 plan_route，最后再出文字回复，留一轮余量
MAX_TOOL_ROUNDS = 4

NETWORK_FALLBACK_REPLY = "网络不太好，请稍后再试。"
AGENT_FAILURE_REPLY = "抱歉，刚才没处理好，请再说一次。"
DEFERRED_NOTICE = "其他指令先暂缓，处理完再说。"

# 角色失败时播报的动作名；chitchat、other 和安全类没有动作名，失败时用 AGENT_FAILURE_REPLY
FAILURE_ACTIONS: Dict[IntentCategory, str] = {
    IntentCategory.CLIMATE_CONTROL: "调空调",
    IntentCategory.BODY_CONTROL:    "车身操作",
    IntentCategory.DRIVE_SETTING:   "驾驶设置",
    IntentCategory.NAVIGATION:      "导航",
    IntentCategory.POI_SEARCH:      "搜索地点",
    IntentCategory.MEDIA_CONTROL:   "影音操作",
    IntentCategory.PHONE_CALL:      "打电话",
    IntentCategory.VEHICLE_STATUS:  "查询车况",
    IntentCategory.FAULT_DIAGNOSIS: "查询故障",
    IntentCategory.MANUAL_QA:       "查询手册",
}
# 查询类失败时只提示稍后重试，不提手动操作
_QUERY_INTENTS: frozenset = frozenset({
    IntentCategory.VEHICLE_STATUS,
    IntentCategory.FAULT_DIAGNOSIS,
    IntentCategory.MANUAL_QA,
})

# 所有 LLM 角色共用的播报要求
VOICE_RULES: Tuple[str, ...] = (
    "回复会通过语音播报给驾驶员：一般不超过 2 句，口语化，不用列表、编号和 Markdown。",
    "执行结果以工具返回为准；操作被拒绝或失败时，直接说明原因，不能说成已完成。",
    "缺少关键信息时只追问一个最关键的问题。",
    "只处理角色输入包 assigned_tasks 里分给你的任务；其他部分由别的模块处理，不要提及，也不要说不归你管。",
    "不要声称执行了没有工具结果支撑的操作或查询。",
)

# 模板拼接时去掉的开头应答词（长的在前）
_ACK_PREFIXES: Tuple[str, ...] = ("好的，", "好的,", "好的。", "好的")
# 句末标点：拼接时前一段没有句末标点就补一个句号
_SENTENCE_END = "。！？!?….；;"


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentProfile:
    """角色契约：写进 system prompt，并决定工具白名单和生成参数。"""

    role: str
    mission: str
    workflow: Tuple[str, ...]
    input_contract: Tuple[str, ...]
    output_contract: Tuple[str, ...]
    tool_scope: Tuple[str, ...] = ()
    model: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 1024
    thinking: bool = True          # False 时请求带 thinking: disabled


def _env_float(name: str, default: float) -> float:
    """读取可选浮点配置；错误配置不应阻塞服务启动。"""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("忽略非法浮点配置 %s=%r", name, os.getenv(name))
        return default


def _env_int(name: str, default: int) -> int:
    """读取可选整数配置；错误配置不应阻塞服务启动。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("忽略非法整数配置 %s=%r", name, os.getenv(name))
        return default


def _clean_text(value: str) -> str:
    """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
    return (value or "").encode("utf-8", errors="ignore").decode("utf-8")


def failure_reply(tasks: Iterable[SubTask]) -> str:
    """
    角色失败时的播报，不调模型：按分到的意图取动作名，多个用"、"连接。
    执行类："调空调失败，请手动操作或稍后重试。"；全是查询类："查询车况失败，请稍后重试。"
    没有动作名（闲聊、other 或没分到任务）时用 AGENT_FAILURE_REPLY。
    """
    intents = [task.intent for task in tasks if task.intent in FAILURE_ACTIONS]
    if not intents:
        return AGENT_FAILURE_REPLY
    actions = "、".join(dict.fromkeys(FAILURE_ACTIONS[intent] for intent in intents))
    hint = "请稍后重试" if all(intent in _QUERY_INTENTS for intent in intents) else "请手动操作或稍后重试"
    return f"{actions}失败，{hint}。"


def _tasks_to_dicts(tasks: Iterable[SubTask]) -> List[Dict[str, str]]:
    """子任务列表转成 [{"intent", "text"}]，用于输入包、结果和 API 响应。"""
    return [{"intent": task.intent.value, "text": task.text} for task in tasks]


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    latency_ms:  float = 0.0
    tools_used:  List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)  # [{"tool_name", "result"}]，即工具返回值
    assigned_tasks: List[SubTask] = field(default_factory=list)       # 本次处理的子任务
    ticket: Optional[Dict[str, Any]] = None                           # 安全节点生成的求助单 / 救援单
    state_changes: List[Dict[str, Any]] = field(default_factory=list)    # [{"field", "old", "new"}]
    blocked_actions: List[Dict[str, Any]] = field(default_factory=list)  # [{"tool_name", "rule_id", "reason"}]


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    intent:      Optional[IntentCategory] = None        # 主意图，run() 中由 intent_result 写入
    intent_result: Optional[IntentResult] = None        # 为空时 run() 调用识别器
    vehicle_state: Dict[str, Any] = field(default_factory=dict)  # 车辆状态，按角色裁剪成快照；为空时 run() 从车辆服务取
    profile:     Dict[str, Any] = field(default_factory=dict)    # 驾驶员偏好：places 只供工具读取，preferences、notes 进角色输入包
    assigned_tasks: List[SubTask] = field(default_factory=list)  # 分给当前 Agent 的子任务
    execution_mode: str = "parallel"                             # "parallel" / "sequential"
    previous_results: List[Dict[str, Any]] = field(default_factory=list)  # 串行时上游 Agent 的结果
    deferred_tasks: List[SubTask] = field(default_factory=list)  # 安全优先时暂缓的子任务
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False   # 本次由安全节点处理
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)  # 参与处理的角色，按子任务首次出现的顺序
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    routing_reason: str = ""
    intent_result: Optional[IntentResult] = None
    sub_tasks: List[Dict[str, str]] = field(default_factory=list)               # [{"intent", "text"}]
    execution: str = "parallel"
    assignments: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)  # agent_type 值 → 子任务
    deferred_tasks: List[Dict[str, str]] = field(default_factory=list)
    ticket: Optional[Dict[str, Any]] = None
    state_changes: List[Dict[str, Any]] = field(default_factory=list)    # 本次请求的车辆状态变化
    blocked_actions: List[Dict[str, Any]] = field(default_factory=list)  # 被安全规则拒绝的操作
    route: str = "slow"                  # "fast" 快速通道 / "slow" 完整链路
    fast_rule: Optional[str] = None      # 快速通道命中的规则 ID，如 F02


@dataclass
class RoutingDecision:
    """一次请求的结构化路由决策。"""
    assignments: Dict[AgentType, List[SubTask]] = field(default_factory=dict)  # 按子任务首次出现的顺序
    execution: str = "parallel"
    deferred_tasks: List[SubTask] = field(default_factory=list)
    reason: str = ""

    @property
    def agent_types(self) -> List[AgentType]:
        return list(self.assignments)

    @property
    def multi_agent(self) -> bool:
        return len(self.assignments) > 1


@dataclass
class _ToolRun:
    """一次 handle 调用内的工具记录；放在局部变量里，同一 Agent 被并发调用时互不覆盖。"""
    tools_used: List[str] = field(default_factory=list)
    traces: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    state_changes: List[Dict[str, Any]] = field(default_factory=list)
    blocked_actions: List[Dict[str, Any]] = field(default_factory=list)


def collect_vehicle_effects(run: _ToolRun, tool_name: str, result: Any) -> None:
    """工具返回 changes 时并入状态变化，返回 blocked 时记下被拒绝的操作；Agent 工具循环、安全节点和快速通道共用。"""
    if not isinstance(result, dict):
        return
    changes = result.get("changes")
    if isinstance(changes, list):
        run.state_changes.extend(dict(change) for change in changes if isinstance(change, dict))
    if result.get("blocked") is True:
        run.blocked_actions.append({
            "tool_name": tool_name,
            "rule_id": result.get("rule_id"),
            "reason": str(result.get("reason", "") or ""),
        })


# ── 路由决策 ──────────────────────────────────────────────────────────────────

def decide_route(intent_result: IntentResult, message: str) -> RoutingDecision:
    """
    生成路由决策（规则见 docs/项目介绍/02-Agent角色与路由.md）。

    没有子任务时，整句作为主意图的唯一子任务；
    规则 1：主意图或任一子任务是安全类 → 只派给安全节点，非安全类子任务放进 deferred_tasks；
    规则 2：子任务按 INTENT_AGENT 分到各角色，同一角色合并，按子任务首次出现的顺序排列。
    """
    intent = intent_result.intent
    tasks = list(intent_result.sub_tasks) or [SubTask(intent, message)]
    execution = "sequential" if intent_result.execution == "sequential" else "parallel"

    if intent in SAFETY_INTENTS or any(task.intent in SAFETY_INTENTS for task in tasks):
        safety_tasks = [task for task in tasks if task.intent in SAFETY_INTENTS]
        deferred = [task for task in tasks if task.intent not in SAFETY_INTENTS]
        if not safety_tasks:
            # 主意图是安全类但子任务里没有安全类：整句交给安全节点
            safety_tasks = [SubTask(intent, message)]
        decision = RoutingDecision(
            assignments={AgentType.SAFETY: safety_tasks},
            execution=execution,
            deferred_tasks=deferred,
        )
    else:
        assignments: Dict[AgentType, List[SubTask]] = {}
        for task in tasks:
            assignments.setdefault(INTENT_AGENT.get(task.intent, AgentType.CABIN), []).append(task)
        decision = RoutingDecision(assignments=assignments, execution=execution)

    agents = "+".join(agent.value for agent in decision.agent_types)
    decision.reason = f"intent={intent.value}, agents={agents}, execution={decision.execution}"
    if decision.deferred_tasks:
        decision.reason += f", deferred={len(decision.deferred_tasks)}"
    return decision


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 工具循环、角色契约和统计。"""

    agent_type: AgentType
    system_prompt: str = ""
    profile: AgentProfile
    uses_llm: bool = True

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        skill_manager: Optional[Any] = None,
        profile: Optional[AgentProfile] = None,
    ):
        self._client = client
        self.profile = profile or self.profile
        self._model  = self.profile.model or model
        self._skill_manager = skill_manager
        self.stats   = AgentStats()
        self._tool_registry: Dict[str, AgentToolSpec] = {}

    def set_tool_registry(self, registry: Optional[Dict[str, AgentToolSpec]]) -> None:
        """接收编排器下发的工具注册表；真正可见的工具由 get_tools 按 tool_scope 过滤。"""
        self._tool_registry = dict(registry or {})

    def get_tools(self) -> Dict[str, AgentToolSpec]:
        """返回该角色真实可调用的工具白名单：注册表中名字在 tool_scope 内的工具。"""
        return {
            name: self._tool_registry[name]
            for name in self.profile.tool_scope
            if name in self._tool_registry
        }

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        run = _ToolRun()
        try:
            content = await self._call_llm(req, run)
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                tools_used=list(run.tools_used),
                tool_traces=list(run.traces),
                tool_results=list(run.results),
                assigned_tasks=list(req.assigned_tasks),
                state_changes=list(run.state_changes),
                blocked_actions=list(run.blocked_actions),
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content=failure_reply(req.assigned_tasks),
                success=False,
                latency_ms=ms,
                tools_used=list(run.tools_used),
                tool_traces=list(run.traces),
                tool_results=list(run.results),
                assigned_tasks=list(req.assigned_tasks),
                state_changes=list(run.state_changes),
                blocked_actions=list(run.blocked_actions),
            )

    async def _call_llm(self, req: Request, run: _ToolRun) -> str:
        messages: List[Dict[str, Any]] = []
        if req.context:
            messages.append({"role": "user", "content": f"[背景信息]\n{_clean_text(req.context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        packet = json.dumps(self._build_role_packet(req), ensure_ascii=False, default=str)
        messages.append({"role": "user", "content": f"[角色输入包]\n{_clean_text(packet)}"})
        messages.append({"role": "assistant", "content": "好的，我只处理分给我的任务。"})
        messages.append({"role": "user", "content": _clean_text(self._task_text(req))})

        tools = self.get_tools()
        system = self._build_system_prompt(req)
        for _ in range(MAX_TOOL_ROUNDS):
            request_kwargs: Dict[str, Any] = {
                "model": self._model,
                "max_tokens": self.profile.max_tokens,
                "temperature": self.profile.temperature,
                "system": system,
                "messages": messages,
            }
            if not self.profile.thinking:
                request_kwargs["extra_body"] = THINKING_DISABLED
            if tools:
                request_kwargs["tools"] = [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "input_schema": spec.input_schema,
                    }
                    for spec in tools.values()
                ]
            resp = await self._client.messages.create(**request_kwargs)
            tool_uses = [block for block in (resp.content or []) if self._block_type(block) == "tool_use"]
            if not tool_uses:
                return extract_text_content(resp.content)

            messages.append({"role": "assistant", "content": resp.content})
            tool_result_blocks = []
            for block in tool_uses:
                name = self._block_value(block, "name")
                tool_use_id = self._block_value(block, "id")
                args = self._block_value(block, "input") or {}
                spec = tools.get(name)
                tool_t0 = time.monotonic()
                call_success = True
                result_success: Optional[bool] = None
                error_text = ""
                if spec is None:
                    # 不在白名单的工具不执行，只把错误回传给模型
                    call_success = False
                    result: Any = {"success": False, "error": f"工具不在 {self.agent_type.value} Agent 白名单中"}
                    error_text = result["error"]
                else:
                    try:
                        self._validate_tool_input(spec, args)
                        result = spec.handler(req, args)
                        if inspect.isawaitable(result):
                            result = await result
                        run.tools_used.append(name)
                        if isinstance(result, dict) and "success" in result:
                            result_success = bool(result.get("success"))
                    except Exception as ex:
                        call_success = False
                        logger.warning("Agent 工具 %s 执行失败: %s", name, ex)
                        error_text = str(ex)
                        result = {"success": False, "error": error_text}
                tool_latency_ms = (time.monotonic() - tool_t0) * 1000
                if not error_text and isinstance(result, dict):
                    error_text = str(result.get("error", "") or "")
                self._collect_vehicle_effects(run, name, result)
                run.traces.append(
                    {
                        "agent_type": self.agent_type.value,
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                        "input": dict(args) if isinstance(args, dict) else args,
                        "success": call_success,
                        "result_success": result_success,
                        "latency_ms": round(tool_latency_ms, 1),
                        "cached": bool(result.get("cached")) if isinstance(result, dict) else False,
                        "reranked": bool(result.get("reranked")) if isinstance(result, dict) else False,
                        "error": error_text,
                    }
                )
                run.results.append({"tool_name": name, "result": result})
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
            messages.append({"role": "user", "content": tool_result_blocks})

        raise RuntimeError(f"{self.agent_type.value} 工具调用超过最大轮数")

    _collect_vehicle_effects = staticmethod(collect_vehicle_effects)

    @staticmethod
    def _block_type(block: Any) -> Optional[str]:
        if isinstance(block, dict):
            return block.get("type")
        return getattr(block, "type", None)

    @staticmethod
    def _block_value(block: Any, key: str) -> Any:
        if isinstance(block, dict):
            return block.get(key)
        return getattr(block, key, None)

    @staticmethod
    def _validate_tool_input(spec: AgentToolSpec, args: Any) -> None:
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        schema = spec.input_schema
        for field_name in schema.get("required", []):
            if field_name not in args:
                raise ValueError(f"缺少必需参数: {field_name}")
        properties = schema.get("properties", {})
        unknown = set(args) - set(properties)
        if unknown and schema.get("additionalProperties") is False:
            raise ValueError(f"不允许的工具参数: {', '.join(sorted(unknown))}")
        type_map = {"string": str, "number": (int, float), "integer": int, "boolean": bool}
        for key, value in args.items():
            expected = properties.get(key, {}).get("type")
            if expected in type_map and not isinstance(value, type_map[expected]):
                raise ValueError(f"参数 {key} 类型错误，期望 {expected}")

    def _build_system_prompt(self, req: Request) -> str:
        """把角色契约、统一的播报要求和动态 Skills 拼入 system prompt。"""
        tool_names = "、".join(self.get_tools()) or "无"
        rules = "\n".join(f"- {rule}" for rule in VOICE_RULES)
        profile_prompt = (
            f"\n\n[角色契约]\n"
            f"角色：{self.profile.role}\n"
            f"职责：{self.profile.mission}\n"
            f"处理流程：{' -> '.join(self.profile.workflow)}\n"
            f"可用输入：{'；'.join(self.profile.input_contract)}\n"
            f"输出要求：{'；'.join(self.profile.output_contract)}\n"
            f"可用工具：{tool_names}\n"
            f"\n[播报要求]\n{rules}"
        )
        base_prompt = f"{self.system_prompt}{profile_prompt}"
        if self._skill_manager is None:
            return base_prompt
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return base_prompt
        return f"{base_prompt}\n\n[动态 Skills]\n{skill_prompt}"

    @staticmethod
    def _task_text(req: Request) -> str:
        """本轮交给模型的指令：只放分给本角色的原话片段，完整原话留在角色输入包里作参考。"""
        texts = [task.text for task in req.assigned_tasks if task.text]
        return "；".join(texts) if texts else req.message

    def _build_role_packet(self, req: Request) -> Dict[str, Any]:
        """
        角色输入包：分给本角色的子任务、完整原话、执行方式、上游结果和车辆快照；
        驾驶员偏好非空时加 driver_preferences，只含 preferences 和 notes，不含 places。
        """
        packet: Dict[str, Any] = {
            "agent_type": self.agent_type.value,
            "assigned_tasks": _tasks_to_dicts(req.assigned_tasks),
            "full_utterance": req.message,
            "execution_mode": req.execution_mode,
            "previous_results": list(req.previous_results),
            "vehicle_snapshot": self._vehicle_snapshot(req),
        }
        preferences = prompt_driver_preferences(req.profile)
        if preferences:
            packet["driver_preferences"] = preferences
        return packet

    def _vehicle_snapshot(self, req: Request) -> Dict[str, Any]:
        """按 SNAPSHOT_KEYS 从车辆状态里裁剪本角色需要的顶层键。"""
        state = req.vehicle_state or {}
        return {key: state[key] for key in SNAPSHOT_KEYS.get(self.agent_type, ()) if key in state}


class VehicleControlAgent(BaseAgent):
    agent_type = AgentType.VEHICLE_CONTROL
    profile = AgentProfile(
        role="车控",
        mission="解析控制对象、动作、参数和音区，调用控制工具执行，并按工具结果播报。",
        workflow=("解析控制对象和动作", "确定参数和音区", "调用控制工具", "按工具结果播报"),
        input_contract=(
            "分给自己的子任务",
            "完整原话",
            "车辆快照：车速、档位、空调、座椅、车身、灯光",
            "驾驶员偏好（driver_preferences，如习惯的温度、座椅加热、驾驶模式）与近期对话",
        ),
        output_contract=("一句话播报执行结果", "被拒绝或被调整时说明原因", "缺参数时只追问一个"),
        tool_scope=("set_climate", "set_body", "set_lighting", "set_drive_mode"),
        temperature=0.0,
        max_tokens=400,
        thinking=False,
    )
    system_prompt = (
        "你是车载语音助手 CabinWeave 的车控模块，负责空调、座椅加热、车窗车门、灯光和驾驶模式等车辆控制。"
        "根据分给你的指令确定控制对象、动作、参数和音区（主驾、副驾、后排），调用控制工具执行，并按工具返回的结果播报。"
        "车辆快照里已有的状态直接使用，不需要再查询。"
        "驾驶员说“按我习惯的温度”“老样子”这类话时，参考角色输入包里的 driver_preferences；没有相关偏好就追问具体数值。"
    )


class NavigationAgent(BaseAgent):
    agent_type = AgentType.NAVIGATION
    profile = AgentProfile(
        role="导航",
        mission="确认目的地或地点类别，检索地点或规划路线，有多个候选时播报前 3 个让驾驶员选。",
        workflow=("确认目的地或地点类别", "检索地点或规划路线", "多候选时播报前 3 个", "播报路线或候选结果"),
        input_contract=(
            "分给自己的子任务",
            "完整原话",
            "车辆快照：位置、导航状态、电量续航",
            "驾驶员偏好（driver_preferences，不含地址；家和公司的地址由 plan_route 从偏好里读取）与近期对话",
        ),
        output_contract=("播报目的地、路线或候选地点", "多候选时只播报前 3 个并请驾驶员选择", "目的地不明确时只追问一个"),
        tool_scope=("search_poi", "plan_route", "get_route_status"),
        temperature=0.1,
        max_tokens=500,
        thinking=False,
    )
    system_prompt = (
        "你是车载语音助手 CabinWeave 的导航模块，负责设置目的地、规划路线、查询路况和剩余时间，"
        "以及按类别找地点（充电站、停车场、服务区、维修点、餐厅）。"
        "目的地不明确时先确认；有多个候选时只播报前 3 个，让驾驶员选择。"
    )


class AdvisorAgent(BaseAgent):
    agent_type = AgentType.ADVISOR
    profile = AgentProfile(
        role="用车顾问",
        mission="区分车况、故障、知识类问题，查车辆快照、故障表或用户手册，按严重度给建议。",
        workflow=("区分车况、故障、知识类问题", "车况看快照，故障查故障表，知识查手册", "按严重度给建议"),
        input_contract=(
            "分给自己的子任务",
            "完整原话",
            "车辆快照：行驶、电量续航、胎压、故障灯、保养里程、室外温度、空调、座椅、车身、灯光、媒体",
            "用户手册检索结果",
            "驾驶员偏好（driver_preferences）与近期对话",
        ),
        output_contract=("先给结论再给建议", "可能影响行车安全时先提醒安全停车", "不下最终诊断结论"),
        tool_scope=("lookup_warning", "estimate_range", "search_knowledge_base"),
        temperature=0.1,
        max_tokens=700,
    )
    system_prompt = (
        "你是车载语音助手 CabinWeave 的用车顾问，负责回答车况、故障提示和用车知识问题。"
        "车况类问题直接看车辆快照；故障灯和报警提示先查故障表；功能用法、充电和保养知识查用户手册。"
        "按严重程度给建议，可能影响行车安全时先提醒安全停车；不要下最终的维修诊断结论。"
    )


class CabinAgent(BaseAgent):
    agent_type = AgentType.CABIN
    profile = AgentProfile(
        role="座舱助手",
        mission="处理影音、电话、天气和闲聊，听不明白时追问澄清。",
        workflow=("判断是影音、电话、闲聊还是需要澄清", "调用对应工具或直接回答", "缺信息时只追问一个"),
        input_contract=(
            "分给自己的子任务",
            "完整原话",
            "车辆快照：媒体状态、车速",
            "驾驶员偏好（driver_preferences，如喜欢的音乐）与近期对话",
        ),
        output_contract=("简短口语化回复", "做不到的操作如实说明", "听不明白时只追问一个"),
        tool_scope=("control_media", "set_volume", "make_call", "get_weather", "suggest_required_fields"),
        temperature=0.4,
        max_tokens=400,
        thinking=False,
    )
    system_prompt = (
        "你是车载语音助手 CabinWeave 的座舱助手，负责音乐电台、音量、打电话发消息、天气和日常闲聊。"
        "驾驶员的话听不明白或缺少关键信息时，用一句话追问。"
        "没有对应工具的操作如实说明暂时做不到，不要假装已经执行。"
    )


class SafetyAgent(BaseAgent):
    """
    安全节点：不调模型，按安全类意图操作车辆、生成求助单 / 救援单并播报固定话术。

    车辆操作经 VehicleService 执行，同样过安全规则：紧急求助打开双闪，车速为 0 时解锁车门
    （行驶中被 S02 拒绝，不解锁）；道路救援打开双闪。播报按实际执行结果说。
    没有绑定车辆服务时不操作车辆，播报改为提醒驾驶员自己打开双闪。
    生产环境可在这里对接救援平台或 Webhook。
    """

    agent_type = AgentType.SAFETY
    uses_llm = False
    profile = AgentProfile(
        role="安全节点",
        mission=(
            "不调模型，处理紧急求助和道路救援：经车辆服务开双闪、静止时解锁车门，"
            "生成求助单或救援单，并按实际执行结果播报安全提示。"
        ),
        workflow=(
            "确定安全类意图",
            "执行车辆操作（开双闪、静止时解锁车门）",
            "生成单号",
            "按执行结果播报安全提示",
            "说明其他指令暂缓",
        ),
        input_contract=("分给自己的安全类子任务", "完整原话", "车辆 ID", "暂缓的其他指令"),
        output_contract=("单号", "车辆操作的实际结果", "安全提示", "有暂缓指令时说明暂缓"),
        temperature=0.0,
    )

    # 意图 → (单号前缀, 单据类型)
    _TICKETS: Dict[IntentCategory, Tuple[str, str]] = {
        IntentCategory.EMERGENCY_SOS: ("SOS", "emergency"),
        IntentCategory.ROADSIDE_ASSIST: ("RSA", "roadside"),
    }

    _vehicle: Optional[VehicleService] = None

    def bind_vehicle_service(self, service: Optional[VehicleService]) -> None:
        """绑定车辆服务；编排器创建时调用。"""
        self._vehicle = service

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        intent = self._safety_intent(req)
        prefix, ticket_type = self._TICKETS[intent]
        ticket_id = f"{prefix}-{req.request_id}"

        effects = _ToolRun()
        hazard_on = unlocked = unlock_blocked = False
        if intent in (IntentCategory.EMERGENCY_SOS, IntentCategory.ROADSIDE_ASSIST):
            hazard_on = self._operate(req, effects, make_command("set_hazard", on=True)).get("success") is True
        if intent is IntentCategory.EMERGENCY_SOS:
            unlock = self._operate(req, effects, make_command("set_door_lock", locked=False))
            unlocked = unlock.get("success") is True
            unlock_blocked = unlock.get("blocked") is True

        content = self._compose(intent, ticket_id, hazard_on, unlocked, unlock_blocked)
        if req.deferred_tasks:
            content += DEFERRED_NOTICE
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "intent": intent.value,
            "request_id": req.request_id,
            "utterance": req.message,
        }
        logger.warning("请求 %s 进入安全节点: intent=%s ticket=%s", req.request_id, intent.value, ticket_id)
        ms = (time.monotonic() - t0) * 1000
        self.stats.success += 1
        self.stats.total_ms += ms
        return AgentResponse(
            agent_type=self.agent_type,
            content=content,
            success=True,
            latency_ms=ms,
            assigned_tasks=list(req.assigned_tasks),
            ticket=ticket,
            state_changes=list(effects.state_changes),
            blocked_actions=list(effects.blocked_actions),
        )

    def _operate(self, req: Request, effects: _ToolRun, command: Dict[str, Any]) -> Dict[str, Any]:
        """经车辆服务执行一条命令，并记下状态变化或被拒绝的操作；没有车辆服务或执行异常时返回空 dict。"""
        if self._vehicle is None:
            return {}
        try:
            result = self._vehicle.execute(req.user_id, command)
        except Exception as ex:
            logger.error("安全节点车辆操作 %s 失败: %s", command.get("action"), ex)
            return {}
        self._collect_vehicle_effects(effects, command["action"], result)
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _compose(
        intent: IntentCategory, ticket_id: str, hazard_on: bool, unlocked: bool, unlock_blocked: bool,
    ) -> str:
        """按车辆操作的实际结果拼播报：做成了才说"已为你……"，被拒绝的不说成已完成。"""
        if intent is IntentCategory.ROADSIDE_ASSIST:
            guide = "已为你打开双闪，请把车停在安全位置，" if hazard_on else "请把车停在安全位置，打开双闪，"
            return (
                f"已生成道路救援单 {ticket_id}，救援人员会尽快联系你。"
                f"{guide}人员撤到护栏外，并在车后方放置警示牌。"
            )
        done = [label for label, ok in (("打开双闪", hazard_on), ("解锁车门", unlocked)) if ok]
        vehicle = f"已为你{'并'.join(done)}" if done else ""
        if unlock_blocked:
            vehicle = f"{vehicle}，请尽快在安全的地方停车" if vehicle else "请尽快在安全的地方停车"
        if vehicle:
            vehicle += "。"
        return (
            f"已发起紧急求助，求助单号 {ticket_id}。{vehicle}"
            "请先确认人员安全，有人受伤立即拨打 120，发生事故请拨打 122。"
        )

    @staticmethod
    def _safety_intent(req: Request) -> IntentCategory:
        """取分到的第一个安全类意图；都没有时退回主意图，再按紧急求助处理（安全优先）。"""
        for task in req.assigned_tasks:
            if task.intent in SAFETY_INTENTS:
                return task.intent
        if req.intent in SAFETY_INTENTS:
            return req.intent
        return IntentCategory.EMERGENCY_SOS


# ── 汇总节点 ──────────────────────────────────────────────────────────────────

def _strip_ack(text: str) -> str:
    """去掉开头的"好的"类应答词。"""
    text = text.strip()
    for prefix in _ACK_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].lstrip()
    return text


def _join_sentences(parts: Iterable[str]) -> str:
    """按顺序直接连接多段回复；前一段没有句末标点时补一个句号，避免播报连成一句。"""
    joined = ""
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        if joined and joined[-1] not in _SENTENCE_END:
            joined += "。"
        joined += text
    return joined


class ResponseComposer:
    """
    多 Agent 汇总节点：成功的部分执行类模板拼接、含问答类调模型合并、模型失败时确定性拼接；
    失败的角色不交给模型改写，失败短句按顺序接在后面。
    """

    def __init__(self, client: AsyncAnthropic, model: str):
        self._client = client
        self._model = model

    async def compose(self, req: Request, responses: List[AgentResponse]) -> str:
        successful = [response for response in responses if response.success and response.content.strip()]
        failures = list(dict.fromkeys(   # 去重，避免同一句致歉播两遍
            response.content.strip() if not response.success and response.content.strip()
            else failure_reply(response.assigned_tasks)
            for response in responses if response not in successful
        ))
        if not successful:
            return _join_sentences(failures) or AGENT_FAILURE_REPLY
        return _join_sentences([await self._compose_successful(req, successful)] + failures)

    async def _compose_successful(self, req: Request, successful: List[AgentResponse]) -> str:
        """合并成功角色的回复。"""
        if len(successful) == 1:
            return successful[0].content.strip()

        if self._all_execution(successful):
            # 第一条原样保留，后面每条去掉开头的"好的"再接上
            return _join_sentences(
                [successful[0].content] + [_strip_ack(response.content) for response in successful[1:]]
            )

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=_env_int("CABINWEAVE_COMPOSER_MAX_TOKENS", 400),
                temperature=_env_float("CABINWEAVE_COMPOSER_TEMPERATURE", 0.1),
                messages=[{"role": "user", "content": self._build_prompt(req, successful)}],
            )
            content = extract_text_content(response.content).strip()
            if content:
                return content
        except Exception as ex:
            logger.warning("汇总节点调用模型失败，使用确定性拼接: %s", ex)

        return _join_sentences(response.content for response in successful)

    @staticmethod
    def _all_execution(responses: List[AgentResponse]) -> bool:
        """每条回复都有子任务，且子任务全是执行类。"""
        return all(
            response.assigned_tasks
            and all(task.intent in EXECUTION_INTENTS for task in response.assigned_tasks)
            for response in responses
        )

    @staticmethod
    def _build_prompt(req: Request, responses: List[AgentResponse]) -> str:
        sections = []
        for idx, response in enumerate(responses, 1):
            tasks = "；".join(f"{task.text}（{task.intent.value}）" for task in response.assigned_tasks) or "未标注"
            lines = [f"[{idx}] 处理的指令：{tasks}", f"回复：{response.content.strip()}"]
            if response.tool_results:
                tool_text = json.dumps(response.tool_results, ensure_ascii=False, default=str)
                lines.append(f"工具结果：{tool_text[:800]}")
            sections.append("\n".join(lines))
        prompt = (
            "你是车载语音助手 CabinWeave 的汇总节点，负责把几段处理结果合并成一条播报给驾驶员的回复。\n"
            "要求：\n"
            "- 不超过 3 句，口语化，不用列表、编号和 Markdown，不要提及 Agent 或内部处理过程；\n"
            "- 执行结果以工具结果为准，被拒绝或失败的操作不能说成已完成；\n"
            "- 涉及安全的信息放在最前面；\n"
            "- 去掉重复的应答词和重复内容，不要补充结果里没有的信息。\n"
            "只输出播报内容。\n\n"
            f"驾驶员原话：{req.message}\n"
            "处理结果：\n"
        ) + "\n\n".join(sections)
        return _clean_text(prompt)


# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    车载多 Agent 编排器。

    run() 流程：快速通道（try_fast_path，命中直接返回）→ 意图识别 → 路由决策（decide_route）
    → 并行或串行执行 → 汇总 → 记录工具调用、累计通道统计。
    车辆服务通过 self.vehicle 暴露，可由外部传入，便于测试和 API 层共享同一个实例；
    快速通道通过 self.fast_path 暴露，enabled 可在运行中切换。
    """

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
        rag_tool_manager: Optional[Any] = None,
        vehicle_service: Optional[VehicleService] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._intent_recognizer = IntentRecognizer(api_key=api_key, base_url=base_url, model=model)
        self._skill_manager = skill_manager
        self._composer = ResponseComposer(client, model)
        self._tool_registry: Dict[str, AgentToolSpec] = {}
        self._recent_tool_traces = deque(maxlen=_env_int("CABINWEAVE_TOOL_TRACE_MAX", 200))
        # 车辆服务（内含 VehicleStateStore 和 SafetyPolicy）
        self.vehicle = vehicle_service if vehicle_service is not None else VehicleService()
        # 快速通道（开关读 CABINWEAVE_FAST_PATH）与通道统计
        self.fast_path = FastPath()
        self._route_counts: Dict[str, int] = {"fast": 0, "slow": 0}
        self._fast_rule_counts: Dict[str, int] = {}

        # 每个角色一个实例
        self._agents: Dict[AgentType, BaseAgent] = {
            AgentType.VEHICLE_CONTROL: self._make_agent(VehicleControlAgent, client, model, skill_manager),
            AgentType.NAVIGATION: self._make_agent(NavigationAgent, client, model, skill_manager),
            AgentType.ADVISOR: self._make_agent(AdvisorAgent, client, model, skill_manager),
            AgentType.CABIN: self._make_agent(CabinAgent, client, model, skill_manager),
            AgentType.SAFETY: self._make_agent(SafetyAgent, client, model, skill_manager),
        }
        safety = self._agents[AgentType.SAFETY]
        if isinstance(safety, SafetyAgent):
            safety.bind_vehicle_service(self.vehicle)
        self.register_tools(build_rag_tools(rag_tool_manager))
        self.register_tools(build_agent_tools(self.vehicle))

    @staticmethod
    def _make_agent(
        agent_cls: type[BaseAgent],
        client: AsyncAnthropic,
        default_model: str,
        skill_manager: Optional[Any],
    ) -> BaseAgent:
        """按角色创建 Agent，并允许用环境变量覆盖该角色的模型。

        座舱助手可配更快的模型，用车顾问可配更强的模型；安全节点不调用模型，不读模型配置。
        """
        if not agent_cls.uses_llm:
            return agent_cls(client, default_model, skill_manager)
        profile = agent_cls.profile
        env_name = f"CABINWEAVE_{agent_cls.agent_type.value.upper()}_MODEL"
        model = os.getenv(env_name, "").strip() or profile.model
        configured_profile = replace(profile, model=model) if model else profile
        return agent_cls(client, default_model, skill_manager, profile=configured_profile)

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agent in self._agents.values():
            agent._skill_manager = skill_manager

    def register_tools(self, tools: Optional[Dict[str, AgentToolSpec]]) -> None:
        """把工具合并进注册表并下发给各 Agent；各 Agent 只能看到 tool_scope 内的工具。"""
        self._tool_registry.update(tools or {})
        for agent in self._agents.values():
            agent.set_tool_registry(self._tool_registry)

    def _record_tool_trace(self, result: OrchestratorResult) -> None:
        trace = {
            "request_id": result.request_id,
            "timestamp": datetime.now().isoformat(),
            "intent": result.intent.value if result.intent else None,
            "agent_types": [agent.value for agent in result.agent_types],
            "assignments": result.assignments,
            "tools_used": list(result.tools_used),
            "tool_calls": list(result.tool_traces),
            "escalated": result.escalated,
            "ticket": result.ticket,
            "state_changes": list(result.state_changes),
            "blocked_actions": list(result.blocked_actions),
            "latency_ms": round(result.latency_ms, 1),
            "route": result.route,
            "fast_rule": result.fast_rule,
        }
        self._recent_tool_traces.append(trace)

    def _finish(self, result: OrchestratorResult) -> OrchestratorResult:
        """请求结束：记录工具调用，按 route 累计通道统计。"""
        self._record_tool_trace(result)
        if result.route == "fast":
            self._route_counts["fast"] += 1
            if result.fast_rule:
                self._fast_rule_counts[result.fast_rule] = self._fast_rule_counts.get(result.fast_rule, 0) + 1
        else:
            self._route_counts["slow"] += 1
        return result

    def get_tool_trace(self, request_id: str) -> Optional[Dict[str, Any]]:
        for trace in reversed(self._recent_tool_traces):
            if trace.get("request_id") == request_id:
                return trace
        return None

    def get_recent_tool_traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self._recent_tool_traces:
            return []
        limit = max(1, min(int(limit or 20), len(self._recent_tool_traces)))
        return list(reversed(list(self._recent_tool_traces)[-limit:]))

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def try_fast_path(self, req: Request) -> Optional[OrchestratorResult]:
        """
        快速通道：规则命中白名单指令时，调用注册表里同名工具的 handler
        （和 Agent 用的是同一个函数，同样过安全规则），按工具结果模板播报，不调模型。

        返回 route="fast" 的结果；未开启、未命中、工具不在注册表、工具执行异常或返回 error / unsupported 时
        返回 None，由调用方走完整链路。命中时同样记录工具调用并计入通道统计。
        """
        t0 = time.monotonic()
        match = self.fast_path.match(req.message)
        if match is None:
            return None
        spec = self._tool_registry.get(match.tool_name)
        if spec is None:
            logger.warning("快速通道规则 %s 的工具 %s 未注册，回到完整链路", match.rule_id, match.tool_name)
            return None
        match_ms = (time.monotonic() - t0) * 1000

        # 1. 调用和 Agent 同一个工具函数（handler 可能同步，也可能返回 awaitable）
        args = dict(match.args)
        tool_t0 = time.monotonic()
        try:
            result = spec.handler(req, args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as ex:
            logger.warning("快速通道工具 %s 执行异常，回到完整链路: %s", match.tool_name, ex)
            return None
        tool_latency_ms = (time.monotonic() - tool_t0) * 1000

        # 2. 模板播报；error / unsupported 等不能模板播报的结果回到完整链路
        reply = self.fast_path.reply(match, result, self.vehicle.snapshot(req.user_id))
        if reply is None:
            logger.debug("快速通道规则 %s 的工具结果不能模板播报，回到完整链路: %s", match.rule_id, result)
            return None

        agent_type = INTENT_AGENT.get(match.intent, AgentType.CABIN)
        effects = _ToolRun()
        collect_vehicle_effects(effects, match.tool_name, result)
        trace = {
            "agent_type": agent_type.value,
            "tool_name": match.tool_name,
            "tool_use_id": f"fast-{req.request_id}",   # 没有模型的 tool_use，用请求 ID 占位
            "input": args,
            "success": True,
            "result_success": bool(result.get("success")) if isinstance(result, dict) and "success" in result else None,
            "latency_ms": round(tool_latency_ms, 1),
            "cached": False,
            "reranked": False,
            "error": "",
        }
        req.intent_result = self._fast_intent_result(match, match_ms)
        req.intent = match.intent
        return self._finish(OrchestratorResult(
            request_id=req.request_id,
            response=reply,
            agent_type=agent_type,
            intent=match.intent,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[agent_type],
            tools_used=[match.tool_name],
            tool_traces=[trace],
            routing_reason=f"route=fast, rule={match.rule_id}",
            intent_result=req.intent_result,
            state_changes=list(effects.state_changes),
            blocked_actions=list(effects.blocked_actions),
            route="fast",
            fast_rule=match.rule_id,
        ))

    @staticmethod
    def _fast_intent_result(match: FastMatch, latency_ms: float) -> IntentResult:
        """快速通道命中时的识别结果：置信度 high，决策来源为 fast_path，LLM 未调用。"""
        return IntentResult(
            intent=match.intent,
            confidence_level="high",
            reasoning=f"快速通道命中规则 {match.rule_id}（{match.name}）",
            latency_ms=latency_ms,
            sources={"decided_by": "fast_path", "fast_rule": match.rule_id, "llm_status": "skipped"},
        )

    async def run(self, req: Request, *, fast_path: bool = True) -> OrchestratorResult:
        """
        处理一次请求：
          快速通道（fast_path 为 True 时先试，命中直接返回）
          → 意图识别 → 路由决策 → 执行（并行/串行）→ 汇总 → 返回结果
        调用方已经试过快速通道时（如 API 层在读取记忆之前）传 fast_path=False。
        """
        t0 = time.monotonic()

        # 0. 快速通道：规则命中简单车控 / 媒体指令时直接执行，不调模型
        if fast_path:
            fast_result = await self.try_fast_path(req)
            if fast_result is not None:
                return fast_result

        # 1. 意图识别（调用方已传入识别结果则跳过）
        if req.intent_result is None:
            req.intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
        req.intent = req.intent_result.intent

        # 2. LLM 失败且关键词层也没有候选：不调任何模型，直接回复
        if self._network_unavailable(req.intent_result):
            sources = req.intent_result.sources or {}
            result = OrchestratorResult(
                request_id=req.request_id,
                response=NETWORK_FALLBACK_REPLY,
                agent_type=AgentType.CABIN,
                intent=req.intent,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.CABIN],
                routing_reason=(
                    f"intent={req.intent.value}, llm_status={sources.get('llm_status')}, "
                    "decided_by=none, 直接回复网络提示"
                ),
                intent_result=req.intent_result,
            )
            return self._finish(result)

        # 3. 路由决策并执行
        decision = decide_route(req.intent_result, req.message)
        if decision.execution == "sequential" and len(decision.assignments) > 1:
            responses = await self._run_sequential(req, decision)
        else:
            responses = await self._run_parallel(req, decision)

        # 4. 单 Agent 直接返回（失败时就是它的失败短句）；多 Agent 经汇总节点
        if decision.multi_agent:
            content = await self._composer.compose(req, responses)
        else:
            content = responses[0].content

        result = OrchestratorResult(
            request_id=req.request_id,
            response=content,
            agent_type=decision.agent_types[0],
            intent=req.intent,
            escalated=AgentType.SAFETY in decision.assignments,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=decision.agent_types,
            tools_used=list(dict.fromkeys(
                tool_name for response in responses for tool_name in response.tools_used
            )),
            tool_traces=[trace for response in responses for trace in response.tool_traces],
            routing_reason=decision.reason,
            intent_result=req.intent_result,
            sub_tasks=_tasks_to_dicts(req.intent_result.sub_tasks),
            execution=decision.execution,
            assignments={
                assigned_type.value: _tasks_to_dicts(tasks)
                for assigned_type, tasks in decision.assignments.items()
            },
            deferred_tasks=_tasks_to_dicts(decision.deferred_tasks),
            ticket=next((response.ticket for response in responses if response.ticket), None),
            state_changes=[change for response in responses for change in response.state_changes],
            blocked_actions=[blocked for response in responses for blocked in response.blocked_actions],
        )
        if result.escalated:
            logger.warning("请求 %s 由安全节点处理: %s", req.request_id, decision.reason)
        return self._finish(result)

    @staticmethod
    def _network_unavailable(intent_result: IntentResult) -> bool:
        """LLM 失败或超时，且关键词层没有候选。"""
        sources = intent_result.sources or {}
        return sources.get("llm_status") in ("failed", "timeout") and sources.get("decided_by") == "none"

    def _vehicle_state(self, req: Request) -> Dict[str, Any]:
        """调用方显式传了 vehicle_state 就原样使用，否则从车辆服务取当前快照。"""
        if req.vehicle_state:
            return req.vehicle_state
        return self.vehicle.snapshot(req.user_id)

    @staticmethod
    def _agent_request(
        req: Request,
        decision: RoutingDecision,
        tasks: List[SubTask],
        previous: List[Dict[str, Any]],
        vehicle_state: Dict[str, Any],
    ) -> Request:
        """为单个 Agent 复制一份请求，带上分给它的子任务、执行方式、上游结果和车辆快照。"""
        return replace(
            req,
            assigned_tasks=list(tasks),
            execution_mode=decision.execution,
            previous_results=list(previous),
            deferred_tasks=list(decision.deferred_tasks),
            vehicle_state=vehicle_state,
        )

    async def _run_parallel(self, req: Request, decision: RoutingDecision) -> List[AgentResponse]:
        """
        各 Agent 同时执行，使用同一时刻的车辆快照，结果按 assignments 顺序返回；
        某个 Agent 抛出异常时，换成它的失败短句，不从结果里丢掉。
        """
        vehicle_state = self._vehicle_state(req)
        tasks = [
            self._execute(
                self._agent_request(req, decision, assigned, req.previous_results, vehicle_state), agent_type,
            )
            for agent_type, assigned in decision.assignments.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        responses: List[AgentResponse] = []
        for (agent_type, assigned), result in zip(decision.assignments.items(), results):
            if isinstance(result, AgentResponse):
                responses.append(result)
                continue
            logger.error("请求 %s 的 %s 执行异常: %s", req.request_id, agent_type.value, result)
            responses.append(AgentResponse(
                agent_type=agent_type, content=failure_reply(assigned), success=False, assigned_tasks=list(assigned),
            ))
        return responses

    async def _run_sequential(self, req: Request, decision: RoutingDecision) -> List[AgentResponse]:
        """
        按 assignments 顺序逐个执行，前面 Agent 的回复和工具结果追加进后一个的 previous_results；
        每个 Agent 派发前重新取车辆快照，看到的是上一个 Agent 改过之后的状态。
        """
        responses: List[AgentResponse] = []
        previous = list(req.previous_results)
        for agent_type, assigned in decision.assignments.items():
            agent_req = self._agent_request(req, decision, assigned, previous, self._vehicle_state(req))
            response = await self._execute(agent_req, agent_type)
            responses.append(response)
            previous.append({
                "agent_type": response.agent_type.value,
                "content": response.content,
                "tool_results": list(response.tool_results),
            })
        return responses

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """
        交给对应角色执行；失败时直接返回它的失败短句，不转交其他角色。
        失败前已执行的工具调用和车辆状态变化保留在返回里。
        """
        agent = self._agents.get(agent_type)
        if agent is None:
            return AgentResponse(
                agent_type=agent_type,
                content=failure_reply(req.assigned_tasks),
                success=False,
                assigned_tasks=list(req.assigned_tasks),
            )
        return await agent.handle(req)

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            agent_type.value: {
                "total":        agent.stats.total,
                "success_rate": round(agent.stats.success_rate, 3),
                "avg_ms":       round(agent.stats.avg_ms, 1),
                "role": agent.profile.role,
                "workflow": list(agent.profile.workflow),
                "tool_scope": list(agent.profile.tool_scope),
                "available_tools": list(agent.get_tools()),
                "model": agent._model if agent.uses_llm else None,
            }
            for agent_type, agent in self._agents.items()
        }

    def get_route_stats(self) -> Dict[str, Any]:
        """快慢通道统计：两条通道的请求数、快速通道占比、各规则命中次数（按规则 ID 排序）和开关状态。"""
        fast, slow = self._route_counts["fast"], self._route_counts["slow"]
        total = fast + slow
        return {
            "fast": fast,
            "slow": slow,
            "total": total,
            "fast_ratio": round(fast / total, 3) if total else 0.0,
            "rules": dict(sorted(self._fast_rule_counts.items())),
            "enabled": self.fast_path.enabled,
        }
