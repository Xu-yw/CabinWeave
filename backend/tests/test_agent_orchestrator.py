"""
CabinWeave 编排器测试：意图到角色的映射、角色契约、按 tool_scope 过滤的工具白名单、
路由决策（单意图 / 多意图合并 / 安全优先 / 串行顺序）、角色输入包与车辆快照裁剪、
并行与串行执行、角色失败时直接播报失败短句、安全节点、网络兜底和汇总分级；
模拟车辆接入：快照默认取当前用户那辆车的状态、串行时逐个刷新、安全节点的车辆操作、
工具返回的 changes / blocked 汇总进结果和工具调用记录；
真实工具注册：各角色的工具集合符合约定且互不重叠、完整的工具循环改变车辆状态、
被安全规则拒绝的调用进入 blocked_actions、驾驶员偏好传到工具、MAX_TOOL_ROUNDS 限制模型调用轮数；
驾驶员偏好：有偏好时 4 个 LLM 角色的输入包带 driver_preferences 且不含 places，没有偏好时不带，
记忆规整后的偏好经 Request.profile 让 plan_route 解析"回家"，地址不进模型上下文。

全部使用假客户端，不访问网络、不调用真实模型；直接给 Request 传 intent_result 跳过识别器。
"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import agents.agent_orchestrator as orchestrator_module
from agents.agent_orchestrator import (
    AGENT_FAILURE_REPLY,
    DEFERRED_NOTICE,
    INTENT_AGENT,
    MAX_TOOL_ROUNDS,
    NETWORK_FALLBACK_REPLY,
    SNAPSHOT_KEYS,
    AdvisorAgent,
    AgentOrchestrator,
    AgentResponse,
    AgentType,
    CabinAgent,
    NavigationAgent,
    Request,
    ResponseComposer,
    SafetyAgent,
    VehicleControlAgent,
    decide_route,
)
from agents.tools import build_agent_tools, cabin_tools, make_tool
from core.intent_recognizer import IntentCategory, IntentResult, SubTask
from memory.conversation_memory import normalize_driver_profile
from vehicle.service import VehicleService, make_command
from vehicle.state import DEFAULT_STATE, VehicleStateStore

C = IntentCategory
LLM_AGENTS = (VehicleControlAgent, NavigationAgent, AdvisorAgent, CabinAgent)


# ── 假客户端与构造工具 ────────────────────────────────────────────────────────

class ScriptedClient:
    """按顺序返回预设响应的假客户端；元素是异常时抛出，记录每次调用参数。"""

    def __init__(self, *responses, log=None, name=""):
        self.responses = list(responses)
        self.calls = []
        client = self

        class Messages:
            async def create(inner, **kwargs):
                client.calls.append(kwargs)
                if log is not None:
                    log.append(name)
                if not client.responses:
                    raise AssertionError("没有预设响应的意外调用")
                item = client.responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        self.messages = Messages()


def text(content):
    return SimpleNamespace(content=[{"type": "text", "text": content}])


def tool_use(name, args, tool_use_id="toolu_1"):
    return SimpleNamespace(content=[{"type": "tool_use", "id": tool_use_id, "name": name, "input": args}])


def make_result(intent, sub_tasks=(), execution="parallel", llm_status="ok", decided_by="llm"):
    return IntentResult(
        intent=intent,
        confidence_level="high",
        reasoning="test",
        latency_ms=0.0,
        sources={"llm_status": llm_status, "decided_by": decided_by},
        sub_tasks=list(sub_tasks),
        execution=execution,
    )


def make_request(message, intent_result=None, **kwargs):
    return Request(message=message, user_id="u1", conv_id="c1", intent_result=intent_result, **kwargs)


def make_orchestrator(vehicle_service=None, **clients):
    """构造编排器并给每个角色和汇总节点换上假客户端；没指定的用空脚本客户端（被调用就失败）。"""
    orch = AgentOrchestrator(
        api_key="test-key", model="test-model",
        vehicle_service=vehicle_service or VehicleService(VehicleStateStore(default_scenario="parked")),
    )
    installed = {}
    for agent_type, agent in orch._agents.items():
        client = clients.get(agent_type.value) or ScriptedClient()
        installed[agent_type.value] = client
        agent._client = client
    installed["composer"] = clients.get("composer") or ScriptedClient()
    orch._composer._client = installed["composer"]
    return orch, installed


def role_packet(call):
    """从一次模型调用的 messages 里取出角色输入包。"""
    for message in call["messages"]:
        content = message["content"]
        if isinstance(content, str) and content.startswith("[角色输入包]"):
            return json.loads(content.split("\n", 1)[1])
    raise AssertionError("调用里没有角色输入包")


def fake_tool(name, calls=None, result=None):
    """记录调用的假工具。"""

    def handler(req, args):
        if calls is not None:
            calls.append((name, dict(args)))
        return result if result is not None else {"success": True, "tool": name}

    return make_tool(
        name,
        f"测试工具 {name}",
        {"temperature": {"type": "number"}, "query": {"type": "string"}, "distance_km": {"type": "number"}},
        handler,
    )


def service_tool(orch, name, command=None, seen=None):
    """经编排器的车辆服务执行固定命令的假工具；seen 记录调用时请求里的完整车辆状态。"""

    def handler(req, args):
        if seen is not None:
            seen.append(copy.deepcopy(req.vehicle_state))
        if command is None:
            return {"success": True}
        return orch.vehicle.execute(req.user_id, command)

    return make_tool(
        name,
        f"测试工具 {name}",
        {"temperature": {"type": "number"}, "query": {"type": "string"}},
        handler,
    )


FULL_VEHICLE_STATE = {
    "speed_kmh": 40,
    "gear": "D",
    "drive_mode": "comfort",
    "battery_pct": 76,
    "range_km": 380,
    "climate": {"on": True, "temp": 24, "fan_level": 3, "defrost": False},
    "seats": {"seat_heat": {"driver": 0, "passenger": 0}},
    "body": {"doors_locked": True},
    "lights": {"ambient": False, "hazard": False},
    "tire_pressure_kpa": {"fl": 250, "fr": 250, "rl": 248, "rr": 249},
    "warning_lights": [],
    "next_service_km": 3000,
    "outside_temp_c": 10,
    "location": {"city": "上海"},
    "navigation": {"active": False},
    "media": {"playing": False, "volume": 12},
}


# ── 映射与角色契约 ────────────────────────────────────────────────────────────

def test_intent_agent_covers_all_intents_and_matches_design():
    expected = {
        C.CLIMATE_CONTROL: AgentType.VEHICLE_CONTROL,
        C.BODY_CONTROL: AgentType.VEHICLE_CONTROL,
        C.DRIVE_SETTING: AgentType.VEHICLE_CONTROL,
        C.NAVIGATION: AgentType.NAVIGATION,
        C.POI_SEARCH: AgentType.NAVIGATION,
        C.VEHICLE_STATUS: AgentType.ADVISOR,
        C.FAULT_DIAGNOSIS: AgentType.ADVISOR,
        C.MANUAL_QA: AgentType.ADVISOR,
        C.MEDIA_CONTROL: AgentType.CABIN,
        C.PHONE_CALL: AgentType.CABIN,
        C.EMERGENCY_SOS: AgentType.SAFETY,
        C.ROADSIDE_ASSIST: AgentType.SAFETY,
        C.CHITCHAT: AgentType.CABIN,
        C.OTHER: AgentType.CABIN,
    }
    assert set(INTENT_AGENT) == set(IntentCategory)
    assert len(INTENT_AGENT) == 14
    assert INTENT_AGENT == expected
    assert {agent.value for agent in AgentType} == {
        "vehicle_control", "navigation", "advisor", "cabin", "safety",
    }


def test_llm_agent_profiles_are_distinct_and_match_design():
    profiles = [cls.profile for cls in LLM_AGENTS]
    assert len({profile.role for profile in profiles}) == 4
    assert len({profile.mission for profile in profiles}) == 4
    assert len({profile.workflow for profile in profiles}) == 4
    assert len({cls.system_prompt for cls in LLM_AGENTS}) == 4

    assert (VehicleControlAgent.profile.temperature, VehicleControlAgent.profile.max_tokens) == (0.0, 1200)
    assert (NavigationAgent.profile.temperature, NavigationAgent.profile.max_tokens) == (0.1, 1500)
    assert (AdvisorAgent.profile.temperature, AdvisorAgent.profile.max_tokens) == (0.1, 700)
    assert (CabinAgent.profile.temperature, CabinAgent.profile.max_tokens) == (0.4, 1200)

    assert set(VehicleControlAgent.profile.tool_scope) == {
        "set_climate", "set_body", "set_lighting", "set_drive_mode",
    }
    assert set(NavigationAgent.profile.tool_scope) == {"search_poi", "plan_route", "get_route_status"}
    assert set(AdvisorAgent.profile.tool_scope) == {"lookup_warning", "estimate_range", "search_knowledge_base"}
    assert set(CabinAgent.profile.tool_scope) == {
        "control_media", "set_volume", "make_call", "get_weather", "suggest_required_fields",
    }
    # 工具范围互不重叠，RAG 只在用车顾问
    scopes = [set(profile.tool_scope) for profile in profiles]
    assert sum(len(scope) for scope in scopes) == len(set().union(*scopes))
    assert SafetyAgent.uses_llm is False
    assert SafetyAgent.profile.tool_scope == ()


def test_thinking_disabled_except_advisor():
    for cls, thinking in (
        (VehicleControlAgent, False), (NavigationAgent, False), (CabinAgent, False), (AdvisorAgent, True),
    ):
        assert cls.profile.thinking is thinking
        client = ScriptedClient(text("好的。"))
        asyncio.run(cls(client, "test-model").handle(make_request("测试")))
        if thinking:
            assert "extra_body" not in client.calls[0]
        else:
            assert client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_system_prompt_contains_voice_rules():
    agent = VehicleControlAgent(ScriptedClient(), "test-model")
    prompt = agent._build_system_prompt(make_request("空调调到22度"))
    assert "语音播报" in prompt
    assert "不超过 2 句" in prompt
    assert "Markdown" in prompt
    assert "以工具返回为准" in prompt
    assert "只追问一个" in prompt


def test_role_model_can_be_overridden_by_env_and_safety_has_no_model(monkeypatch):
    monkeypatch.setenv("CABINWEAVE_CABIN_MODEL", "fast-model")
    monkeypatch.setenv("CABINWEAVE_SAFETY_MODEL", "should-not-apply")
    orch, _ = make_orchestrator()
    stats = orch.get_stats()
    assert stats["cabin"]["model"] == "fast-model"
    assert stats["vehicle_control"]["model"] == "test-model"
    assert stats["safety"]["model"] is None
    assert set(stats) == {"vehicle_control", "navigation", "advisor", "cabin", "safety"}
    assert "routing_score" not in stats["cabin"]


# ── 工具白名单 ────────────────────────────────────────────────────────────────

def test_tool_whitelist_filters_registry_by_tool_scope():
    orch, _ = make_orchestrator()
    # 清空默认注册的工具，只留下这三个，验证按 tool_scope 过滤
    orch._tool_registry.clear()
    orch.register_tools({
        "set_climate": fake_tool("set_climate"),
        "control_media": fake_tool("control_media"),
        "search_knowledge_base": fake_tool("search_knowledge_base"),
    })
    tools = {agent_type: set(agent.get_tools()) for agent_type, agent in orch._agents.items()}

    assert tools[AgentType.VEHICLE_CONTROL] == {"set_climate"}
    assert "set_climate" not in tools[AgentType.CABIN]
    assert tools[AgentType.CABIN] == {"control_media"}
    assert tools[AgentType.ADVISOR] == {"search_knowledge_base"}
    assert tools[AgentType.NAVIGATION] == set()
    assert tools[AgentType.SAFETY] == set()
    assert [agent_type for agent_type, names in tools.items() if "search_knowledge_base" in names] == [
        AgentType.ADVISOR
    ]
    assert orch.get_stats()["vehicle_control"]["available_tools"] == ["set_climate"]


EXPECTED_ROLE_TOOLS = {
    AgentType.VEHICLE_CONTROL: {"set_climate", "set_body", "set_lighting", "set_drive_mode"},
    AgentType.NAVIGATION: {"search_poi", "plan_route", "get_route_status"},
    AgentType.ADVISOR: {"lookup_warning", "estimate_range", "search_knowledge_base"},
    AgentType.CABIN: {"control_media", "set_volume", "make_call", "get_weather", "suggest_required_fields"},
    AgentType.SAFETY: set(),
}


def test_default_registration_gives_each_role_its_design_tools():
    orch, _ = make_orchestrator()
    tools = {agent_type: set(agent.get_tools()) for agent_type, agent in orch._agents.items()}

    assert tools == EXPECTED_ROLE_TOOLS
    # 互不重叠：车控调不了媒体工具，RAG 只在用车顾问
    assert sum(len(names) for names in tools.values()) == len(set().union(*tools.values())) == 15
    assert set(orch._tool_registry) == set().union(*tools.values())
    assert "control_media" not in tools[AgentType.VEHICLE_CONTROL]
    assert [agent_type for agent_type, names in tools.items() if "search_knowledge_base" in names] == [
        AgentType.ADVISOR
    ]
    for agent_type, names in tools.items():
        assert set(orch.get_stats()[agent_type.value]["available_tools"]) == names

    # 工具绑定的是编排器自己的车辆服务
    set_climate = orch._agents[AgentType.VEHICLE_CONTROL].get_tools()["set_climate"]
    assert set_climate.handler(make_request("打开空调"), {"power": True})["success"] is True
    assert orch.vehicle.snapshot("u1")["climate"]["on"] is True


def test_tool_loop_rejects_tool_outside_whitelist():
    executed = []
    agent = CabinAgent(
        ScriptedClient(tool_use("set_climate", {"temperature": 22}), text("这个我暂时做不到。")),
        "test-model",
    )
    agent.set_tool_registry({
        "set_climate": fake_tool("set_climate", executed),
        "control_media": fake_tool("control_media", executed),
    })
    client = agent._client
    response = asyncio.run(agent.handle(make_request(
        "空调调到22度", assigned_tasks=[SubTask(C.CLIMATE_CONTROL, "空调调到22度")],
    )))

    assert response.success is True
    assert executed == []
    assert response.tools_used == []
    assert response.tool_traces[0]["success"] is False
    assert "白名单" in response.tool_traces[0]["error"]
    assert response.tool_results[0]["tool_name"] == "set_climate"
    assert response.tool_results[0]["result"]["success"] is False
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {"control_media"}
    assert "tool_result" in str(client.calls[1]["messages"])


def test_tool_loop_executes_whitelisted_tool_and_collects_results():
    executed = []
    result = {"success": True, "temperature": 22}
    agent = VehicleControlAgent(
        ScriptedClient(tool_use("set_climate", {"temperature": 22}), text("好的，空调已调到22度。")),
        "test-model",
    )
    agent.set_tool_registry({"set_climate": fake_tool("set_climate", executed, result)})
    response = asyncio.run(agent.handle(make_request("空调调到22度")))

    assert response.content == "好的，空调已调到22度。"
    assert executed == [("set_climate", {"temperature": 22})]
    assert response.tools_used == ["set_climate"]
    assert response.tool_results == [{"tool_name": "set_climate", "result": result}]
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0]["result_success"] is True


def test_agent_failure_reply_is_spoken_style():
    agent = NavigationAgent(ScriptedClient(RuntimeError("provider down")), "test-model")
    response = asyncio.run(agent.handle(make_request("导航回家", assigned_tasks=[SubTask(C.NAVIGATION, "导航回家")])))
    assert response.success is False
    assert response.content == "导航失败，请手动操作或稍后重试。"

    # 没分到任务时用通用致歉
    agent = NavigationAgent(ScriptedClient(RuntimeError("provider down")), "test-model")
    response = asyncio.run(agent.handle(make_request("导航回家")))
    assert response.content == AGENT_FAILURE_REPLY == "抱歉，刚才没处理好，请再说一次。"


@pytest.mark.parametrize("tasks, expected", [
    ([SubTask(C.MEDIA_CONTROL, "放首歌")], "影音操作失败，请手动操作或稍后重试。"),
    ([SubTask(C.CLIMATE_CONTROL, "空调打开"), SubTask(C.BODY_CONTROL, "车窗关上")], "调空调、车身操作失败，请手动操作或稍后重试。"),
    ([SubTask(C.VEHICLE_STATUS, "胎压多少"), SubTask(C.MANUAL_QA, "怎么充电")], "查询车况、查询手册失败，请稍后重试。"),
    ([SubTask(C.CLIMATE_CONTROL, "空调打开"), SubTask(C.CLIMATE_CONTROL, "温度调到22度")], "调空调失败，请手动操作或稍后重试。"),
    ([SubTask(C.CHITCHAT, "你好")], AGENT_FAILURE_REPLY),
    ([], AGENT_FAILURE_REPLY),
])
def test_failure_reply_by_intent(tasks, expected):
    assert orchestrator_module.failure_reply(tasks) == expected


def test_suggest_required_fields_by_intent():
    spec = cabin_tools(VehicleService(VehicleStateStore(default_scenario="parked")))["suggest_required_fields"]

    def fields(**kwargs):
        return spec.handler(make_request("测试", **kwargs), {})["required_fields"]

    assert fields(assigned_tasks=[SubTask(C.NAVIGATION, "导航")]) == ["目的地"]
    assert fields(assigned_tasks=[SubTask(C.PHONE_CALL, "打电话")]) == ["联系人"]
    assert fields(assigned_tasks=[SubTask(C.POI_SEARCH, "找个地方")]) == ["想找的地点类别"]
    assert fields(assigned_tasks=[SubTask(C.CLIMATE_CONTROL, "空调")]) == []
    assert fields(intent=C.OTHER) == ["你想让我做什么"]
    assert fields() == ["你想让我做什么"]


# ── 路由决策 ──────────────────────────────────────────────────────────────────

def test_route_single_intent():
    decision = decide_route(make_result(C.NAVIGATION), "导航回家")
    assert decision.agent_types == [AgentType.NAVIGATION]
    assert decision.assignments == {AgentType.NAVIGATION: [SubTask(C.NAVIGATION, "导航回家")]}
    assert decision.deferred_tasks == []
    assert decision.reason == "intent=navigation, agents=navigation, execution=parallel"


def test_route_climate_and_media_to_vehicle_control_and_cabin():
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调调到22度"), SubTask(C.MEDIA_CONTROL, "再放首周杰伦的歌")]
    decision = decide_route(make_result(C.CLIMATE_CONTROL, tasks), "空调调到22度，再放首周杰伦的歌")
    assert decision.agent_types == [AgentType.VEHICLE_CONTROL, AgentType.CABIN]
    assert decision.multi_agent is True
    assert decision.assignments[AgentType.CABIN] == [tasks[1]]
    assert decision.reason == "intent=climate_control, agents=vehicle_control+cabin, execution=parallel"


def test_route_merges_tasks_for_same_agent():
    tasks = [SubTask(C.BODY_CONTROL, "把车窗关上"), SubTask(C.CLIMATE_CONTROL, "空调打开")]
    decision = decide_route(make_result(C.BODY_CONTROL, tasks), "把车窗关上，空调打开")
    assert decision.assignments == {AgentType.VEHICLE_CONTROL: tasks}
    assert decision.multi_agent is False


def test_route_safety_first_defers_other_tasks():
    tasks = [SubTask(C.MEDIA_CONTROL, "把音乐关了"), SubTask(C.ROADSIDE_ASSIST, "右前轮爆胎了")]
    decision = decide_route(make_result(C.ROADSIDE_ASSIST, tasks), "把音乐关了，右前轮爆胎了")
    assert decision.agent_types == [AgentType.SAFETY]
    assert decision.assignments == {AgentType.SAFETY: [tasks[1]]}
    assert decision.deferred_tasks == [tasks[0]]
    assert "deferred=1" in decision.reason


def test_route_safety_without_sub_tasks_uses_main_intent():
    decision = decide_route(make_result(C.EMERGENCY_SOS), "车着火了")
    assert decision.assignments == {AgentType.SAFETY: [SubTask(C.EMERGENCY_SOS, "车着火了")]}
    assert decision.deferred_tasks == []


def test_route_sequential_keeps_sub_task_order():
    tasks = [SubTask(C.VEHICLE_STATUS, "看看电量够不够"), SubTask(C.NAVIGATION, "够的话就导航去公司")]
    decision = decide_route(make_result(C.VEHICLE_STATUS, tasks, execution="sequential"), "看看电量够不够，够的话就导航去公司")
    assert decision.execution == "sequential"
    assert list(decision.assignments) == [AgentType.ADVISOR, AgentType.NAVIGATION]

    # 主意图不是第一个子任务时，派发顺序仍按子任务首次出现
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调关了"), SubTask(C.MEDIA_CONTROL, "放首歌")]
    decision = decide_route(make_result(C.MEDIA_CONTROL, tasks, execution="sequential"), "空调关了再放首歌")
    assert list(decision.assignments) == [AgentType.VEHICLE_CONTROL, AgentType.CABIN]


# ── 角色输入包 ────────────────────────────────────────────────────────────────

def test_role_packet_fields_and_snapshot_trimmed_by_role():
    req = make_request(
        "空调调到22度，再放首周杰伦的歌",
        assigned_tasks=[SubTask(C.CLIMATE_CONTROL, "空调调到22度")],
        execution_mode="parallel",
        vehicle_state=FULL_VEHICLE_STATE,
    )
    packet = VehicleControlAgent(ScriptedClient(), "test-model")._build_role_packet(req)
    assert set(packet) == {
        "agent_type", "assigned_tasks", "full_utterance", "execution_mode", "previous_results", "vehicle_snapshot",
    }
    assert packet["agent_type"] == "vehicle_control"
    assert packet["assigned_tasks"] == [{"intent": "climate_control", "text": "空调调到22度"}]
    assert packet["full_utterance"] == "空调调到22度，再放首周杰伦的歌"
    assert packet["execution_mode"] == "parallel"
    assert packet["previous_results"] == []

    expected_keys = {
        VehicleControlAgent: {"speed_kmh", "gear", "drive_mode", "climate", "seats", "body", "lights"},
        NavigationAgent: {"location", "navigation", "battery_pct", "range_km"},
        AdvisorAgent: {
            "speed_kmh", "gear", "drive_mode", "battery_pct", "range_km", "tire_pressure_kpa",
            "warning_lights", "next_service_km", "outside_temp_c",
            "climate", "seats", "body", "lights", "media",
        },
        CabinAgent: {"media", "speed_kmh"},
    }
    for cls, keys in expected_keys.items():
        snapshot = cls(ScriptedClient(), "test-model")._build_role_packet(req)["vehicle_snapshot"]
        assert set(snapshot) == keys == set(SNAPSHOT_KEYS[cls.agent_type])
        assert all(snapshot[key] == FULL_VEHICLE_STATE[key] for key in keys)


def test_role_packet_snapshot_empty_without_vehicle_state():
    req = make_request("空调调到22度", assigned_tasks=[SubTask(C.CLIMATE_CONTROL, "空调调到22度")])
    for cls in LLM_AGENTS:
        assert cls(ScriptedClient(), "test-model")._build_role_packet(req)["vehicle_snapshot"] == {}


DRIVER_PROFILE = {
    "preferences": {"climate_temp": 22, "seat_heat": "冬天常开", "music": ["轻音乐"], "drive_mode": "comfort"},
    "places": {"home": "上海南站", "company": "上海虹桥站"},
    "notes": ["周末常去郊区"],
}


def test_role_packet_carries_driver_preferences_without_places_for_all_llm_roles():
    req = make_request(
        "按我习惯的温度来",
        assigned_tasks=[SubTask(C.CLIMATE_CONTROL, "按我习惯的温度来")],
        profile=DRIVER_PROFILE,
    )
    for cls in LLM_AGENTS:
        packet = cls(ScriptedClient(), "test-model")._build_role_packet(req)
        assert packet["driver_preferences"] == {
            "preferences": DRIVER_PROFILE["preferences"],
            "notes": DRIVER_PROFILE["notes"],
        }
        dumped = json.dumps(packet, ensure_ascii=False)
        assert "places" not in dumped and "上海南站" not in dumped and "上海虹桥站" not in dumped
    # 请求里的完整偏好不受影响，工具照样能读到 places
    assert req.profile["places"] == DRIVER_PROFILE["places"]


@pytest.mark.parametrize("profile", [
    {},
    {"places": {"home": "上海南站"}},
    {"preferences": {"climate_temp": 99}, "notes": []},
])
def test_role_packet_has_no_driver_preferences_when_nothing_visible(profile):
    req = make_request("空调调到22度", profile=profile)
    for cls in LLM_AGENTS:
        assert "driver_preferences" not in cls(ScriptedClient(), "test-model")._build_role_packet(req)


def test_driver_preferences_reach_model_and_contracts_mention_them():
    client = ScriptedClient(text("好的，空调已调到22度。"))
    asyncio.run(VehicleControlAgent(client, "test-model").handle(make_request("按我习惯的温度来", profile=DRIVER_PROFILE)))
    call = client.calls[0]
    assert role_packet(call)["driver_preferences"]["preferences"]["climate_temp"] == 22
    assert "上海南站" not in json.dumps(call["messages"], ensure_ascii=False, default=str)

    for cls in LLM_AGENTS:
        assert any("驾驶员偏好" in item for item in cls.profile.input_contract), cls.__name__
    assert "driver_preferences" in VehicleControlAgent.system_prompt
    assert "追问" in VehicleControlAgent.system_prompt


def test_normalized_memory_profile_lets_plan_route_resolve_home_without_leaking_address():
    profile = normalize_driver_profile({
        "notes": ["常走高架"],
        "places": {"home": " 上海南站 ", "school": "某小学"},
    })
    assert profile == {"places": {"home": "上海南站"}, "notes": ["常走高架"]}
    orch, clients = make_orchestrator(navigation=ScriptedClient(
        tool_use("plan_route", {"destination": "回家"}),
        text("好的，开始导航回家。"),
    ))
    result = asyncio.run(orch.run(make_request("导航回家", make_result(C.NAVIGATION), profile=profile)))

    assert result.response == "好的，开始导航回家。"
    assert {"field": "navigation.destination", "old": None, "new": "家"} in result.state_changes
    messages = clients["navigation"].calls[-1]["messages"]
    assert role_packet(clients["navigation"].calls[0])["driver_preferences"] == {"notes": ["常走高架"]}
    tool_result = json.loads(messages[-1]["content"][0]["content"])
    assert tool_result["success"] is True and tool_result["destination"] == "家"
    # 角色输入包和工具结果里都没有家的具体地址
    assert "上海南站" not in json.dumps(messages, ensure_ascii=False, default=str)


# ── 编排执行 ──────────────────────────────────────────────────────────────────

def test_run_parallel_dispatches_each_agent_its_tasks_and_template_joins():
    orch, clients = make_orchestrator(
        vehicle_control=ScriptedClient(text("好的，空调已调到22度。")),
        cabin=ScriptedClient(text("好的，正在播放周杰伦的歌。")),
    )
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调调到22度"), SubTask(C.MEDIA_CONTROL, "放首周杰伦的歌")]
    message = "空调调到22度，再放首周杰伦的歌"
    result = asyncio.run(orch.run(make_request(
        message, make_result(C.CLIMATE_CONTROL, tasks), vehicle_state=FULL_VEHICLE_STATE,
    )))

    assert result.response == "好的，空调已调到22度。正在播放周杰伦的歌。"
    assert clients["composer"].calls == []
    assert result.agent_types == [AgentType.VEHICLE_CONTROL, AgentType.CABIN]
    assert result.agent_type is AgentType.VEHICLE_CONTROL
    assert result.escalated is False
    assert result.execution == "parallel"
    assert result.sub_tasks == [
        {"intent": "climate_control", "text": "空调调到22度"},
        {"intent": "media_control", "text": "放首周杰伦的歌"},
    ]
    assert result.assignments == {
        "vehicle_control": [{"intent": "climate_control", "text": "空调调到22度"}],
        "cabin": [{"intent": "media_control", "text": "放首周杰伦的歌"}],
    }
    assert result.deferred_tasks == []
    assert result.intent_result.intent is C.CLIMATE_CONTROL

    vc_packet = role_packet(clients["vehicle_control"].calls[0])
    assert vc_packet["assigned_tasks"] == [{"intent": "climate_control", "text": "空调调到22度"}]
    assert vc_packet["full_utterance"] == message
    assert set(vc_packet["vehicle_snapshot"]) == set(SNAPSHOT_KEYS[AgentType.VEHICLE_CONTROL])
    cabin_packet = role_packet(clients["cabin"].calls[0])
    assert cabin_packet["assigned_tasks"] == [{"intent": "media_control", "text": "放首周杰伦的歌"}]

    trace = orch.get_tool_trace(result.request_id)
    assert trace["assignments"] == result.assignments


def test_single_agent_with_merged_tasks_returns_reply_directly():
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(text("好的，车窗已关闭，空调已打开。")))
    tasks = [SubTask(C.BODY_CONTROL, "把车窗关上"), SubTask(C.CLIMATE_CONTROL, "空调打开")]
    result = asyncio.run(orch.run(make_request("把车窗关上，空调打开", make_result(C.BODY_CONTROL, tasks))))

    assert result.response == "好的，车窗已关闭，空调已打开。"
    assert len(clients["vehicle_control"].calls) == 1
    assert role_packet(clients["vehicle_control"].calls[0])["assigned_tasks"] == [
        {"intent": "body_control", "text": "把车窗关上"},
        {"intent": "climate_control", "text": "空调打开"},
    ]
    assert clients["composer"].calls == []
    assert result.agent_types == [AgentType.VEHICLE_CONTROL]


def test_run_sequential_passes_previous_results_to_next_agent():
    log = []
    estimate = {"success": True, "remaining_pct": 40, "note": "仅为估算"}
    orch, clients = make_orchestrator(
        advisor=ScriptedClient(
            tool_use("estimate_range", {"distance_km": 30}),
            text("电量够用，到公司大约还剩40%。"),
            log=log, name="advisor",
        ),
        navigation=ScriptedClient(text("好的，已开始导航去公司。"), log=log, name="navigation"),
        composer=ScriptedClient(text("电量够用，已开始导航去公司。"), log=log, name="composer"),
    )
    orch.register_tools({"estimate_range": fake_tool("estimate_range", result=estimate)})
    tasks = [SubTask(C.VEHICLE_STATUS, "看看电量够不够"), SubTask(C.NAVIGATION, "够的话就导航去公司")]
    result = asyncio.run(orch.run(make_request(
        "看看电量够不够，够的话就导航去公司",
        make_result(C.VEHICLE_STATUS, tasks, execution="sequential"),
    )))

    assert log == ["advisor", "advisor", "navigation", "composer"]
    assert role_packet(clients["advisor"].calls[0])["previous_results"] == []
    nav_packet = role_packet(clients["navigation"].calls[0])
    assert nav_packet["execution_mode"] == "sequential"
    assert nav_packet["previous_results"] == [{
        "agent_type": "advisor",
        "content": "电量够用，到公司大约还剩40%。",
        "tool_results": [{"tool_name": "estimate_range", "result": estimate}],
    }]
    assert result.response == "电量够用，已开始导航去公司。"
    assert result.execution == "sequential"
    assert result.tools_used == ["estimate_range"]
    assert list(result.assignments) == ["advisor", "navigation"]


def test_agent_failure_speaks_failure_reply_without_handing_off():
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(RuntimeError("provider down")))
    # "空调调到22度"在快速通道白名单里，这里关掉快速通道测完整链路
    result = asyncio.run(orch.run(make_request("空调调到22度", make_result(C.CLIMATE_CONTROL)), fast_path=False))

    assert result.response == "调空调失败，请手动操作或稍后重试。"
    assert result.agent_type is AgentType.VEHICLE_CONTROL
    assert result.agent_types == [AgentType.VEHICLE_CONTROL]
    assert clients["cabin"].calls == []
    assert orch._agents[AgentType.VEHICLE_CONTROL].stats.success == 0


def test_partial_failure_appends_failure_reply_after_successful_part():
    orch, clients = make_orchestrator(
        vehicle_control=ScriptedClient(text("好的，空调已调到22度。")),
        cabin=ScriptedClient(RuntimeError("provider down")),
    )
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调调到22度"), SubTask(C.MEDIA_CONTROL, "放首周杰伦的歌")]
    result = asyncio.run(orch.run(make_request(
        "空调调到22度，再放首周杰伦的歌", make_result(C.CLIMATE_CONTROL, tasks), vehicle_state=FULL_VEHICLE_STATE,
    )))

    assert result.response == "好的，空调已调到22度。影音操作失败，请手动操作或稍后重试。"
    assert clients["composer"].calls == []
    assert result.agent_types == [AgentType.VEHICLE_CONTROL, AgentType.CABIN]


def test_parallel_agent_exception_becomes_failure_reply(monkeypatch):
    orch, _ = make_orchestrator(vehicle_control=ScriptedClient(text("好的，空调已调到22度。")))

    async def boom(req):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(orch._agents[AgentType.CABIN], "handle", boom)
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调调到22度"), SubTask(C.MEDIA_CONTROL, "放首歌")]
    result = asyncio.run(orch.run(make_request(
        "空调调到22度，再放首歌", make_result(C.CLIMATE_CONTROL, tasks), vehicle_state=FULL_VEHICLE_STATE,
    )))
    assert result.response == "好的，空调已调到22度。影音操作失败，请手动操作或稍后重试。"


def test_safety_node_handles_alone_without_model_and_defers_others():
    orch, clients = make_orchestrator()
    tasks = [SubTask(C.MEDIA_CONTROL, "把音乐关了"), SubTask(C.ROADSIDE_ASSIST, "右前轮爆胎了，帮我叫救援")]
    req = make_request("把音乐关了，右前轮爆胎了，帮我叫救援", make_result(C.ROADSIDE_ASSIST, tasks))
    result = asyncio.run(orch.run(req))

    assert all(client.calls == [] for client in clients.values())
    assert result.escalated is True
    assert result.agent_type is AgentType.SAFETY
    assert f"RSA-{req.request_id}" in result.response
    assert result.response.endswith(DEFERRED_NOTICE)
    assert "已为你打开双闪" in result.response
    assert result.state_changes == [{"field": "lights.hazard", "old": False, "new": True}]
    assert result.assignments == {"safety": [{"intent": "roadside_assist", "text": "右前轮爆胎了，帮我叫救援"}]}
    assert result.deferred_tasks == [{"intent": "media_control", "text": "把音乐关了"}]
    assert result.ticket["ticket_id"] == f"RSA-{req.request_id}"
    assert result.ticket["intent"] == "roadside_assist"
    assert result.ticket["utterance"] == req.message
    assert orch.get_tool_trace(req.request_id)["escalated"] is True


@pytest.mark.parametrize(
    "intent, prefix, keyword",
    [
        (C.EMERGENCY_SOS, "SOS-", "120"),
        (C.ROADSIDE_ASSIST, "RSA-", "警示牌"),
    ],
)
def test_safety_node_templates(intent, prefix, keyword):
    client = ScriptedClient()
    agent = SafetyAgent(client, "test-model")
    req = make_request("测试", intent=intent, assigned_tasks=[SubTask(intent, "测试")])
    response = asyncio.run(agent.handle(req))

    assert client.calls == []
    assert response.success is True
    assert f"{prefix}{req.request_id}" in response.content
    assert keyword in response.content
    assert DEFERRED_NOTICE not in response.content
    assert response.ticket["request_id"] == req.request_id


@pytest.mark.parametrize("llm_status", ["failed", "timeout"])
def test_network_fallback_when_llm_and_keywords_both_fail(llm_status):
    orch, clients = make_orchestrator()
    result = asyncio.run(orch.run(make_request(
        "嗯那个",
        make_result(C.OTHER, llm_status=llm_status, decided_by="none"),
    )))

    assert result.response == NETWORK_FALLBACK_REPLY == "网络不太好，请稍后再试。"
    assert result.agent_type is AgentType.CABIN
    assert all(client.calls == [] for client in clients.values())
    assert all(agent.stats.total == 0 for agent in orch._agents.values())


def test_keyword_fallback_still_routes_normally():
    orch, clients = make_orchestrator(navigation=ScriptedClient(text("好的，正在规划回家的路线。")))
    result = asyncio.run(orch.run(make_request(
        "导航回家",
        make_result(C.NAVIGATION, llm_status="timeout", decided_by="keyword_fallback"),
    )))
    assert result.response == "好的，正在规划回家的路线。"
    assert len(clients["navigation"].calls) == 1


def test_run_calls_recognizer_when_intent_result_missing():
    orch, clients = make_orchestrator(cabin=ScriptedClient(text("你好呀。")))
    seen = []

    class FakeRecognizer:
        async def recognize(self, message, history=None):
            seen.append((message, history))
            return make_result(C.CHITCHAT)

    orch._intent_recognizer = FakeRecognizer()
    req = make_request("你好", history=[{"role": "user", "content": "在吗"}])
    result = asyncio.run(orch.run(req))

    assert seen == [("你好", [{"role": "user", "content": "在吗"}])]
    assert req.intent_result is result.intent_result
    assert req.intent is C.CHITCHAT
    assert result.response == "你好呀。"


# ── 车辆状态 ──────────────────────────────────────────────────────────────────

HAZARD_ON = {"field": "lights.hazard", "old": False, "new": True}
MEDIA_TITLE = "轻音乐相关内容（示例曲库）"


def test_orchestrator_exposes_shared_vehicle_service():
    service = VehicleService(VehicleStateStore())
    orch, _ = make_orchestrator(vehicle_service=service)
    assert orch.vehicle is service
    assert orch._agents[AgentType.SAFETY]._vehicle is service
    assert isinstance(AgentOrchestrator(api_key="test-key", model="test-model").vehicle, VehicleService)


def test_snapshot_comes_from_user_vehicle():
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(text("好的，空调已调到22度。")))
    orch.vehicle.store.update("u1", {"speed_kmh": 40, "gear": "D"})
    req = make_request("空调调到22度", make_result(C.CLIMATE_CONTROL))
    result = asyncio.run(orch.run(req, fast_path=False))

    snapshot = role_packet(clients["vehicle_control"].calls[0])["vehicle_snapshot"]
    assert set(snapshot) == set(SNAPSHOT_KEYS[AgentType.VEHICLE_CONTROL])
    assert (snapshot["speed_kmh"], snapshot["gear"]) == (40, "D")
    assert snapshot["climate"] == DEFAULT_STATE["climate"]


def test_explicit_vehicle_state_is_used_as_is():
    orch, clients = make_orchestrator(navigation=ScriptedClient(text("好的。")))
    orch.vehicle.store.update("u1", {"battery_pct": 30})
    # 调用方显式传入的 vehicle_state 原样使用，不从存储取
    asyncio.run(orch.run(make_request(
        "导航去上海站", make_result(C.NAVIGATION), vehicle_state=FULL_VEHICLE_STATE,
    )))
    assert role_packet(clients["navigation"].calls[0])["vehicle_snapshot"]["battery_pct"] == 76


def _climate_then_media(execution):
    """车控经车辆服务把温度调到 20 度，座舱助手再播放音乐；返回编排器、两次工具调用时的车辆状态和结果。"""
    seen = []
    orch, _ = make_orchestrator(
        vehicle_control=ScriptedClient(tool_use("set_climate", {"temperature": 20}), text("好的，空调已调到20度。")),
        cabin=ScriptedClient(tool_use("control_media", {"query": "轻音乐"}), text("好的，正在播放轻音乐。")),
    )
    orch.register_tools({
        "set_climate": service_tool(orch, "set_climate", make_command("set_temperature", temp=20), seen),
        "control_media": service_tool(
            orch, "control_media", make_command("media_control", op="play", title=MEDIA_TITLE), seen,
        ),
    })
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调调到20度"), SubTask(C.MEDIA_CONTROL, "放点轻音乐")]
    result = asyncio.run(orch.run(make_request(
        "空调调到20度，然后放点轻音乐", make_result(C.CLIMATE_CONTROL, tasks, execution=execution),
    )))
    return orch, seen, result


def test_sequential_agent_sees_state_changed_by_previous_agent():
    orch, seen, result = _climate_then_media("sequential")

    assert seen[0]["climate"]["temp"] == 24      # 车控派发时的快照
    assert seen[1]["climate"]["temp"] == 20      # 座舱助手派发前重新取了快照
    assert seen[1]["climate"]["on"] is True
    assert result.response == "好的，空调已调到20度。正在播放轻音乐。"
    assert result.state_changes == [
        {"field": "climate.on", "old": False, "new": True},
        {"field": "climate.temp", "old": 24, "new": 20},
        {"field": "media.playing", "old": False, "new": True},
        {"field": "media.title", "old": "", "new": MEDIA_TITLE},
    ]
    assert result.blocked_actions == []
    assert orch.vehicle.snapshot("u1")["media"]["title"] == MEDIA_TITLE


def test_parallel_agents_share_snapshot_taken_before_dispatch():
    orch, seen, result = _climate_then_media("parallel")

    assert [state["climate"]["temp"] for state in seen] == [24, 24]
    assert orch.vehicle.snapshot("u1")["climate"]["temp"] == 20
    assert len(result.state_changes) == 4


def test_tool_changes_and_blocked_results_are_collected():
    changes = [{"field": "climate.temp", "old": 24, "new": 22}]
    blocked = {"success": False, "blocked": True, "rule_id": "S01", "reason": "车辆行驶中不能打开后备箱，请停车后再试。"}
    orch, _ = make_orchestrator(vehicle_control=ScriptedClient(
        tool_use("set_climate", {"temperature": 22}),
        tool_use("set_body", {"query": "trunk"}, tool_use_id="toolu_2"),
        text("空调已调到22度。车辆行驶中不能打开后备箱，请停车后再试。"),
    ))
    orch.register_tools({
        "set_climate": fake_tool("set_climate", result={"success": True, "changes": changes, "notice": ""}),
        "set_body": fake_tool("set_body", result=blocked),
    })
    tasks = [SubTask(C.CLIMATE_CONTROL, "空调调到22度"), SubTask(C.BODY_CONTROL, "打开后备箱")]
    req = make_request("空调调到22度，打开后备箱", make_result(C.CLIMATE_CONTROL, tasks))
    result = asyncio.run(orch.run(req))

    assert result.state_changes == changes
    assert result.blocked_actions == [{"tool_name": "set_body", "rule_id": "S01", "reason": blocked["reason"]}]
    trace = orch.get_tool_trace(req.request_id)
    assert trace["state_changes"] == changes
    assert trace["blocked_actions"] == result.blocked_actions


def test_failed_agent_keeps_state_changes_made_before_failure():
    orch, clients = make_orchestrator(
        vehicle_control=ScriptedClient(tool_use("set_climate", {"temperature": 22}), RuntimeError("provider down")),
    )
    orch.register_tools({"set_climate": service_tool(orch, "set_climate", make_command("set_temperature", temp=22))})
    result = asyncio.run(orch.run(make_request("空调调到22度", make_result(C.CLIMATE_CONTROL)), fast_path=False))

    assert result.agent_type is AgentType.VEHICLE_CONTROL
    assert result.response == "调空调失败，请手动操作或稍后重试。"
    assert clients["cabin"].calls == []
    assert result.tools_used == ["set_climate"]
    assert result.state_changes == [
        {"field": "climate.on", "old": False, "new": True},
        {"field": "climate.temp", "old": 24, "new": 22},
    ]


# ── 安全节点的车辆操作 ────────────────────────────────────────────────────────

def run_safety(orch, intent, message):
    req = make_request(message, make_result(intent))
    return req, asyncio.run(orch.run(req))


def test_safety_sos_when_parked_turns_on_hazard_and_unlocks_doors():
    orch, clients = make_orchestrator()
    req, result = run_safety(orch, C.EMERGENCY_SOS, "出车祸了，有人受伤")

    assert all(client.calls == [] for client in clients.values())
    assert result.state_changes == [HAZARD_ON, {"field": "body.doors_locked", "old": True, "new": False}]
    assert result.blocked_actions == []
    assert "已为你打开双闪并解锁车门" in result.response
    assert f"SOS-{req.request_id}" in result.response and "120" in result.response
    state = orch.vehicle.snapshot("u1")
    assert state["lights"]["hazard"] is True and state["body"]["doors_locked"] is False
    assert orch.get_tool_trace(req.request_id)["state_changes"] == result.state_changes


def test_safety_sos_while_driving_turns_on_hazard_but_keeps_doors_locked():
    orch, _ = make_orchestrator()
    orch.vehicle.store.reset("u1", "city_driving")
    _, result = run_safety(orch, C.EMERGENCY_SOS, "车里冒烟了")

    assert result.state_changes == [HAZARD_ON]
    assert result.blocked_actions == [{
        "tool_name": "set_door_lock", "rule_id": "S02", "reason": "车辆行驶中不能解锁车门，请停车后再试。",
    }]
    assert "已为你打开双闪" in result.response
    assert "解锁" not in result.response
    assert orch.vehicle.snapshot("u1")["body"]["doors_locked"] is True


def test_safety_roadside_turns_on_hazard_only():
    orch, _ = make_orchestrator()
    _, result = run_safety(orch, C.ROADSIDE_ASSIST, "车抛锚了，帮我叫救援")

    assert result.state_changes == [HAZARD_ON]
    assert result.blocked_actions == []
    assert "已为你打开双闪" in result.response
    assert "请把车停在安全位置，打开双闪" not in result.response
    assert "警示牌" in result.response
    assert orch.vehicle.snapshot("u1")["body"]["doors_locked"] is True


def test_safety_node_without_vehicle_service_does_not_claim_vehicle_actions():
    agent = SafetyAgent(ScriptedClient(), "test-model")
    req = make_request("车抛锚了", intent=C.ROADSIDE_ASSIST, assigned_tasks=[SubTask(C.ROADSIDE_ASSIST, "车抛锚了")])
    response = asyncio.run(agent.handle(req))

    assert "请把车停在安全位置，打开双闪" in response.content
    assert "已为你" not in response.content
    assert response.state_changes == [] and response.blocked_actions == []


# ── 真实工具注册下的工具循环 ──────────────────────────────────────────────────

def tool_names(call):
    return {tool["name"] for tool in call["tools"]}


def test_vehicle_control_tool_loop_changes_state_and_reports_state_changes():
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(
        tool_use("set_climate", {"power": True, "temperature": 22}),
        text("好的，空调已打开，温度调到22度。"),
    ))
    req = make_request("打开空调，温度调到22度", make_result(C.CLIMATE_CONTROL))
    result = asyncio.run(orch.run(req))

    expected_changes = [
        {"field": "climate.on", "old": False, "new": True},
        {"field": "climate.temp", "old": 24, "new": 22},
    ]
    assert result.response == "好的，空调已打开，温度调到22度。"
    assert result.tools_used == ["set_climate"]
    assert result.state_changes == expected_changes
    assert result.blocked_actions == []
    assert orch.vehicle.snapshot("u1")["climate"] == {"on": True, "temp": 22, "fan_level": 3, "defrost": False}

    calls = clients["vehicle_control"].calls
    assert len(calls) == 2
    assert tool_names(calls[0]) == EXPECTED_ROLE_TOOLS[AgentType.VEHICLE_CONTROL]
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert json.loads(tool_result["content"]) == {"success": True, "changes": expected_changes, "notice": ""}
    trace = orch.get_tool_trace(req.request_id)
    assert trace["state_changes"] == expected_changes
    assert trace["tool_calls"][0]["result_success"] is True


def test_real_blocked_tool_call_enters_blocked_actions():
    orch, _ = make_orchestrator(vehicle_control=ScriptedClient(
        tool_use("set_body", {"part": "trunk", "action": "open"}),
        text("车辆行驶中不能打开后备箱，请停车后再试。"),
    ))
    orch.vehicle.store.reset("u1", "city_driving")
    req = make_request("打开后备箱", make_result(C.BODY_CONTROL))
    result = asyncio.run(orch.run(req, fast_path=False))

    assert result.blocked_actions == [{
        "tool_name": "set_body", "rule_id": "S01", "reason": "车辆行驶中不能打开后备箱，请停车后再试。",
    }]
    assert result.state_changes == []
    assert result.tool_traces[0]["result_success"] is False
    assert orch.vehicle.snapshot("u1")["body"]["trunk_open"] is False
    assert orch.get_tool_trace(req.request_id)["blocked_actions"] == result.blocked_actions


def test_vehicle_control_cannot_call_media_tool_with_real_registry():
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(
        tool_use("control_media", {"action": "play"}),
        text("这个我做不了。"),
    ))
    before = orch.vehicle.snapshot("u1")
    result = asyncio.run(orch.run(make_request("放首歌", make_result(C.CLIMATE_CONTROL))))

    assert "control_media" not in tool_names(clients["vehicle_control"].calls[0])
    assert result.tools_used == []
    assert "白名单" in result.tool_traces[0]["error"]
    assert result.state_changes == []
    assert orch.vehicle.snapshot("u1") == before


def test_navigation_search_then_plan_route_fits_in_max_tool_rounds():
    orch, clients = make_orchestrator(navigation=ScriptedClient(
        tool_use("search_poi", {"category": "charging_station", "require_available": True}),
        tool_use("plan_route", {"destination": "龙华中路快充站（示例）"}, tool_use_id="toolu_2"),
        tool_use("get_route_status", {}, tool_use_id="toolu_3"),
        text("好的，已开始导航去龙华中路快充站，大约4分钟到。"),
    ))
    result = asyncio.run(orch.run(make_request("导航去最近的充电站", make_result(C.POI_SEARCH))))

    assert len(clients["navigation"].calls) == MAX_TOOL_ROUNDS
    assert result.agent_type is AgentType.NAVIGATION
    assert result.response == "好的，已开始导航去龙华中路快充站，大约4分钟到。"
    assert result.tools_used == ["search_poi", "plan_route", "get_route_status"]
    assert result.state_changes == [
        {"field": "navigation.active", "old": False, "new": True},
        {"field": "navigation.destination", "old": None, "new": "龙华中路快充站（示例）"},
        {"field": "navigation.eta_min", "old": None, "new": 4},
    ]
    assert orch.vehicle.snapshot("u1")["navigation"]["destination"] == "龙华中路快充站（示例）"


def test_request_profile_reaches_navigation_tools():
    assert make_request("测试").profile == {}
    orch, _ = make_orchestrator(navigation=ScriptedClient(
        tool_use("plan_route", {"destination": "家"}),
        text("好的，开始导航回家。"),
    ))
    req = make_request("导航回家", make_result(C.NAVIGATION), profile={"places": {"home": "上海南站"}})
    result = asyncio.run(orch.run(req))

    assert {"field": "navigation.destination", "old": None, "new": "家"} in result.state_changes
    assert orch.vehicle.snapshot("u1")["navigation"]["destination"] == "家"


def test_tool_loop_is_limited_by_max_tool_rounds(monkeypatch):
    assert MAX_TOOL_ROUNDS == 4
    service = VehicleService(VehicleStateStore(default_scenario="parked"))

    def run_navigation(rounds):
        client = ScriptedClient(*[tool_use("get_route_status", {}) for _ in range(rounds)])
        agent = NavigationAgent(client, "test-model")
        agent.set_tool_registry(build_agent_tools(service))
        return client, asyncio.run(agent.handle(make_request("还要多久到")))

    client, response = run_navigation(MAX_TOOL_ROUNDS)
    assert response.success is False
    assert response.content == AGENT_FAILURE_REPLY
    assert len(client.calls) == MAX_TOOL_ROUNDS
    assert response.tools_used == ["get_route_status"] * MAX_TOOL_ROUNDS

    # 轮数在运行时读取模块常量
    monkeypatch.setattr(orchestrator_module, "MAX_TOOL_ROUNDS", 2)
    client, response = run_navigation(3)
    assert response.success is False
    assert len(client.calls) == 2


# ── 汇总节点 ──────────────────────────────────────────────────────────────────

def execution_response(agent_type, content, intent=C.CLIMATE_CONTROL):
    return AgentResponse(agent_type, content, True, assigned_tasks=[SubTask(intent, "任务")])


def test_composer_template_joins_execution_results_without_model():
    client = ScriptedClient()
    composer = ResponseComposer(client, "test-model")
    responses = [
        execution_response(AgentType.VEHICLE_CONTROL, "好的，车窗已关闭。", C.BODY_CONTROL),
        execution_response(AgentType.VEHICLE_CONTROL, "好的。空调已打开。"),
        execution_response(AgentType.CABIN, "好的,正在播放周杰伦的歌。", C.MEDIA_CONTROL),
        execution_response(AgentType.CABIN, "好的正在给张三打电话。", C.PHONE_CALL),
    ]
    content = asyncio.run(composer.compose(make_request("测试"), responses))

    assert content == "好的，车窗已关闭。空调已打开。正在播放周杰伦的歌。正在给张三打电话。"
    assert content.count("好的") == 1
    assert client.calls == []


def test_composer_calls_model_when_question_results_present(monkeypatch):
    monkeypatch.delenv("CABINWEAVE_COMPOSER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("CABINWEAVE_COMPOSER_TEMPERATURE", raising=False)
    client = ScriptedClient(text("电量还剩八成，周杰伦的歌也放上了。"))
    composer = ResponseComposer(client, "test-model")
    responses = [
        execution_response(AgentType.ADVISOR, "电量还剩80%，能跑400公里。", C.VEHICLE_STATUS),
        execution_response(AgentType.CABIN, "好的，正在播放周杰伦的歌。", C.MEDIA_CONTROL),
    ]
    content = asyncio.run(composer.compose(make_request("还剩多少电，再放首周杰伦的歌"), responses))

    assert content == "电量还剩八成，周杰伦的歌也放上了。"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["max_tokens"] == 1200
    assert call["temperature"] == 0.1
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}  # 只做合并改写，关闭思考
    prompt = call["messages"][0]["content"]
    assert "不超过 3 句" in prompt
    assert "Markdown" in prompt
    assert "不能说成已完成" in prompt
    assert "安全" in prompt


def test_composer_reads_generation_config_from_env(monkeypatch):
    monkeypatch.setenv("CABINWEAVE_COMPOSER_MAX_TOKENS", "123")
    monkeypatch.setenv("CABINWEAVE_COMPOSER_TEMPERATURE", "0.3")
    client = ScriptedClient(text("合并结果"))
    responses = [
        execution_response(AgentType.ADVISOR, "胎压正常。", C.VEHICLE_STATUS),
        execution_response(AgentType.NAVIGATION, "附近有两个充电站。", C.POI_SEARCH),
    ]
    asyncio.run(ResponseComposer(client, "test-model").compose(make_request("测试"), responses))
    assert client.calls[0]["max_tokens"] == 123
    assert client.calls[0]["temperature"] == 0.3


def test_composer_fallback_joins_in_order_without_labels():
    composer = ResponseComposer(ScriptedClient(RuntimeError("provider down")), "test-model")
    responses = [
        execution_response(AgentType.ADVISOR, "电量还剩80%，能跑400公里", C.VEHICLE_STATUS),
        execution_response(AgentType.CABIN, "好的，正在播放周杰伦的歌。", C.MEDIA_CONTROL),
    ]
    content = asyncio.run(composer.compose(make_request("测试"), responses))

    assert content == "电量还剩80%，能跑400公里。好的，正在播放周杰伦的歌。"
    assert "补充说明" not in content
    assert "\n" not in content


def test_composer_truncated_output_falls_back_to_join():
    # 输出达到 max_tokens 时正文停在半句，不用它，改为按顺序拼接
    truncated = SimpleNamespace(
        content=[{"type": "text", "text": "已开始导航到上海虹桥站。主驾座椅加热已开到"}], stop_reason="max_tokens",
    )
    composer = ResponseComposer(ScriptedClient(truncated), "test-model")
    responses = [
        execution_response(AgentType.VEHICLE_CONTROL, "好的，主驾座椅加热已开到2档。"),
        execution_response(AgentType.NAVIGATION, "已开始导航到上海虹桥站，预计30分钟。", C.NAVIGATION),
    ]
    content = asyncio.run(composer.compose(make_request("打开主驾座椅加热，再导航去虹桥火车站"), responses))

    assert content == "好的，主驾座椅加热已开到2档。已开始导航到上海虹桥站，预计30分钟。"


def test_composer_all_failed_and_partial_failure():
    composer = ResponseComposer(ScriptedClient(), "test-model")
    # 全部失败：失败短句按顺序拼接，相同的致歉只播一次
    failed = [
        AgentResponse(AgentType.VEHICLE_CONTROL, AGENT_FAILURE_REPLY, False),
        AgentResponse(AgentType.CABIN, AGENT_FAILURE_REPLY, False),
    ]
    assert asyncio.run(composer.compose(make_request("测试"), failed)) == "抱歉，刚才没处理好，请再说一次。"

    # 部分失败：成功的放前面，失败短句接在后面；失败结果没有内容时按分到的意图补
    climate_failed = AgentResponse(
        AgentType.VEHICLE_CONTROL, "", False, assigned_tasks=[SubTask(C.CLIMATE_CONTROL, "空调调到22度")],
    )
    mixed = [climate_failed, execution_response(AgentType.CABIN, "好的，正在播放音乐。", C.MEDIA_CONTROL)]
    assert asyncio.run(composer.compose(make_request("测试"), mixed)) == (
        "好的，正在播放音乐。调空调失败，请手动操作或稍后重试。"
    )


def test_agent_receives_only_its_assigned_text_as_instruction():
    client = ScriptedClient(text("好的，正在播放。"))
    req = make_request(
        "空调调到22度，再放首周杰伦的歌",
        assigned_tasks=[SubTask(C.MEDIA_CONTROL, "再放首周杰伦的歌")],
    )
    asyncio.run(CabinAgent(client, "test-model").handle(req))
    messages = client.calls[0]["messages"]
    assert messages[-1] == {"role": "user", "content": "再放首周杰伦的歌"}
    # 完整原话仍在角色输入包里
    assert role_packet(client.calls[0])["full_utterance"] == "空调调到22度，再放首周杰伦的歌"
    # 没有分配子任务时用完整原话
    client = ScriptedClient(text("好的。"))
    asyncio.run(CabinAgent(client, "test-model").handle(make_request("放首歌")))
    assert client.calls[0]["messages"][-1]["content"] == "放首歌"
