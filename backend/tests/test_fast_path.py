"""
CabinWeave 快速通道测试

覆盖：
  - 白名单命中：18 条规则的说法（含中文数字、口头词"请帮我…吧"、语序变体、句末标点）对应的
    规则 ID、工具和参数，以及规则到意图的映射
  - 不命中：连接词、疑问（含问号）、否定、指代、超长、中间带标点、带歌手的播放、解锁、导航、白名单外；
    拒绝判断函数返回的原因
  - 模板播报：普通成功、相对调节说最终值、值没变时同一模板、S04 裁剪、S03 高速限制开度、
    S01 行驶中开后备箱被拒、S07 低电量提醒；工具返回 error / unsupported / 异常时回落为未命中
  - 开关：CABINWEAVE_FAST_PATH
  - 编排器接入：run() 命中快速通道时不调模型、fast_path=False 不走快速通道、未命中走完整链路、
    通道统计、工具调用记录带 route 和 fast_rule、异步 handler

全部使用假客户端和进程内车辆服务，不访问网络、不调用真实模型。
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from agents.tools import make_tool
from core.fast_path import FAST_PATH_ENV, RULES, FastMatch, FastPath, reject_reason
from core.intent_recognizer import IntentCategory, IntentResult
from vehicle.service import VehicleService
from vehicle.state import VehicleStateStore

C = IntentCategory
VID = "u1"


# ── 假客户端与构造工具 ────────────────────────────────────────────────────────

class ScriptedClient:
    """按顺序返回预设响应的假客户端；没有预设响应时被调用就失败，记录每次调用参数。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        client = self

        class Messages:
            async def create(inner, **kwargs):
                client.calls.append(kwargs)
                if not client.responses:
                    raise AssertionError("没有预设响应的意外调用")
                return client.responses.pop(0)

        self.messages = Messages()


def text(content):
    return SimpleNamespace(content=[{"type": "text", "text": content}])


def tool_use(name, args):
    return SimpleNamespace(content=[{"type": "tool_use", "id": "toolu_1", "name": name, "input": args}])


def make_result(intent, llm_status="ok", decided_by="llm"):
    return IntentResult(
        intent=intent,
        confidence_level="high",
        reasoning="test",
        latency_ms=0.0,
        sources={"llm_status": llm_status, "decided_by": decided_by},
    )


def make_request(message, intent_result=None, **kwargs):
    return Request(message=message, user_id="u1", conv_id="c1", intent_result=intent_result, **kwargs)


def make_orchestrator(**clients):
    """构造编排器，给每个角色、汇总节点和意图识别器换上假客户端；没指定的用空脚本客户端（被调用就失败）。"""
    orch = AgentOrchestrator(
        api_key="test-key", model="test-model",
        vehicle_service=VehicleService(VehicleStateStore(default_scenario="parked")),
    )
    installed = {}
    for agent_type, agent in orch._agents.items():
        client = clients.get(agent_type.value) or ScriptedClient()
        installed[agent_type.value] = client
        agent._client = client
    installed["composer"] = orch._composer._client = ScriptedClient()
    installed["recognizer"] = orch._intent_recognizer.client = ScriptedClient()
    return orch, installed


@pytest.fixture(autouse=True)
def default_switch(monkeypatch):
    """每个用例默认不受外部环境变量影响：快速通道开启。"""
    monkeypatch.delenv(FAST_PATH_ENV, raising=False)


@pytest.fixture
def fast():
    return FastPath(enabled=True)


@pytest.fixture
def orch():
    return make_orchestrator()[0]


def try_fast(orch, message):
    return asyncio.run(orch.try_fast_path(make_request(message)))


def state(orch):
    return orch.vehicle.snapshot(VID)


# ── 白名单命中 ────────────────────────────────────────────────────────────────

WINDOW = {"part": "window"}

HITS = [
    # F01 空调开关
    ("打开空调", "F01", "set_climate", {"power": True}),
    ("开空调", "F01", "set_climate", {"power": True}),
    ("空调关了", "F01", "set_climate", {"power": False}),
    ("请帮我把空调打开吧。", "F01", "set_climate", {"power": True}),
    ("关掉空调！", "F01", "set_climate", {"power": False}),
    # F02 设定温度
    ("温度调到二十二度", "F02", "set_climate", {"temperature": 22}),
    ("空调调到22度", "F02", "set_climate", {"temperature": 22}),
    ("温度设为二十二点五度", "F02", "set_climate", {"temperature": 22.5}),
    ("把空调温度调到 26 度", "F02", "set_climate", {"temperature": 26}),
    # F03 相对温度
    ("温度调高两度", "F03", "set_climate", {"temperature_delta": 2}),
    ("温度调低一点", "F03", "set_climate", {"temperature_delta": -1}),
    ("空调调高两度", "F03", "set_climate", {"temperature_delta": 2}),
    ("温度高一点", "F03", "set_climate", {"temperature_delta": 1}),
    ("调高温度", "F03", "set_climate", {"temperature_delta": 1}),
    # F04 设定风量
    ("风量调到三档", "F04", "set_climate", {"fan_level": 3}),
    ("把风速调到5挡", "F04", "set_climate", {"fan_level": 5}),
    # F05 相对风量
    ("风大一点", "F05", "set_climate", {"fan_delta": 1}),
    ("风量调小一点", "F05", "set_climate", {"fan_delta": -1}),
    ("风量加大", "F05", "set_climate", {"fan_delta": 1}),
    ("减小风量", "F05", "set_climate", {"fan_delta": -1}),
    # F06 除雾
    ("打开前挡除雾", "F06", "set_climate", {"defrost": True}),
    ("关闭前挡除雾", "F06", "set_climate", {"defrost": False}),
    ("打开除雾", "F06", "set_climate", {"defrost": True}),
    # F07 座椅加热开关
    ("打开主驾座椅加热", "F07", "set_climate", {"seat_heat_zone": "driver", "seat_heat_level": 2}),
    ("关闭副驾座椅加热", "F07", "set_climate", {"seat_heat_zone": "passenger", "seat_heat_level": 0}),
    ("打开座椅加热", "F07", "set_climate", {"seat_heat_zone": "driver", "seat_heat_level": 2}),
    ("主副驾座椅加热打开", "F07", "set_climate", {"seat_heat_zone": "all", "seat_heat_level": 2}),
    # F08 座椅加热档位
    ("座椅加热调到两档", "F08", "set_climate", {"seat_heat_zone": "driver", "seat_heat_level": 2}),
    ("副驾座椅加热调到三档", "F08", "set_climate", {"seat_heat_zone": "passenger", "seat_heat_level": 3}),
    # F09 车窗
    ("打开主驾车窗", "F09", "set_body", {**WINDOW, "zone": "fl", "action": "open"}),
    ("关闭所有车窗", "F09", "set_body", {**WINDOW, "zone": "all", "action": "close"}),
    ("把副驾驶车窗关上", "F09", "set_body", {**WINDOW, "zone": "fr", "action": "close"}),
    ("车窗开一半", "F09", "set_body", {**WINDOW, "zone": "fl", "action": "open", "open_percent": 50}),
    ("车窗开到百分之五十", "F09", "set_body", {**WINDOW, "zone": "fl", "action": "open", "open_percent": 50}),
    ("右后车窗开到30%", "F09", "set_body", {**WINDOW, "zone": "rr", "action": "open", "open_percent": 30}),
    ("打开后排车窗", "F09", "set_body", {**WINDOW, "zone": "rear", "action": "open"}),
    # F10 天窗
    ("打开天窗", "F10", "set_body", {"part": "sunroof", "action": "open"}),
    ("天窗关了", "F10", "set_body", {"part": "sunroof", "action": "close"}),
    # F11 后备箱
    ("打开后备箱", "F11", "set_body", {"part": "trunk", "action": "open"}),
    ("关闭尾门", "F11", "set_body", {"part": "trunk", "action": "close"}),
    # F12 锁车
    ("锁车", "F12", "set_body", {"part": "door_lock", "action": "lock"}),
    ("锁门", "F12", "set_body", {"part": "door_lock", "action": "lock"}),
    # F13 氛围灯
    ("打开氛围灯", "F13", "set_lighting", {"type": "ambient", "action": "on"}),
    ("关闭氛围灯", "F13", "set_lighting", {"type": "ambient", "action": "off"}),
    # F14 双闪
    ("打开双闪", "F14", "set_lighting", {"type": "hazard", "action": "on"}),
    ("双闪关掉", "F14", "set_lighting", {"type": "hazard", "action": "off"}),
    # F15 播放控制
    ("播放", "F15", "control_media", {"action": "play"}),
    ("继续播放", "F15", "control_media", {"action": "play"}),
    ("暂停", "F15", "control_media", {"action": "pause"}),
    ("下一首", "F15", "control_media", {"action": "next"}),
    ("上一首", "F15", "control_media", {"action": "previous"}),
    ("切歌", "F15", "control_media", {"action": "next"}),
    # F16 设定音量
    ("音量调到十五", "F16", "set_volume", {"level": 15}),
    ("把音量调到15", "F16", "set_volume", {"level": 15}),
    # F17 相对音量
    ("声音大一点", "F17", "set_volume", {"delta": 3}),
    ("音量调小一点", "F17", "set_volume", {"delta": -3}),
    ("大声点", "F17", "set_volume", {"delta": 3}),
    ("小声一点", "F17", "set_volume", {"delta": -3}),
    # F18 静音
    ("静音", "F18", "set_volume", {"mute": True}),
    ("取消静音", "F18", "set_volume", {"mute": False}),
]


@pytest.mark.parametrize("message, rule_id, tool_name, args", HITS)
def test_whitelist_hits(fast, message, rule_id, tool_name, args):
    match = fast.match(message)
    assert match is not None, message
    assert (match.rule_id, match.tool_name, match.args) == (rule_id, tool_name, args)


def test_rules_are_numbered_in_order_and_map_to_intents():
    assert [rule.rule_id for rule in RULES] == [f"F{index:02d}" for index in range(1, 19)]
    intents = {rule.rule_id: rule.intent for rule in RULES}
    assert {rule_id for rule_id, intent in intents.items() if intent is C.CLIMATE_CONTROL} == {
        "F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08",
    }
    assert {rule_id for rule_id, intent in intents.items() if intent is C.BODY_CONTROL} == {
        "F09", "F10", "F11", "F12", "F13", "F14",
    }
    assert {rule_id for rule_id, intent in intents.items() if intent is C.MEDIA_CONTROL} == {
        "F15", "F16", "F17", "F18",
    }
    # 每条规则都在命中表里出现过
    assert {hit[1] for hit in HITS} == set(intents)


# ── 不命中 ────────────────────────────────────────────────────────────────────

MISSES = [
    # 连接词
    "打开空调然后关闭车窗", "打开空调再放首歌", "打开空调和天窗", "顺便打开空调",
    # 疑问
    "空调开了吗", "空调开着呢", "空调怎么开", "温度调到几度", "现在温度多少", "打开空调？", "打开空调?",
    # 否定
    "不要开空调", "别开空调", "没开空调", "请勿打开后备箱",
    # 指代 / 省略
    "把它打开", "打开这个", "刚才那个关了", "再高一点", "温度还是低一点",
    # 超长
    "请帮我把主驾驶的座椅加热打开并调到最高档位",
    # 中间带标点
    "打开空调，关闭车窗", "打开空调、天窗", "温度调到22度;风量调到3档",
    # 带歌手或歌名的播放
    "播放周杰伦的歌", "播放晴天",
    # 解锁
    "解锁车门", "解锁", "开锁",
    # 导航、电话
    "导航回家", "导航去公司", "打电话给张三",
    # 白名单外和可能是抱怨的说法
    "打开雨刮", "切到运动模式", "空调", "温度高", "风大", "声音大", "大声", "放首歌",
    # 参数不合理、空输入
    "车窗开到150%", "", "   ", "吧",
]


@pytest.mark.parametrize("message", MISSES)
def test_not_whitelisted(fast, message):
    assert fast.match(message) is None


@pytest.mark.parametrize(
    "message, expected",
    [
        ("打开空调然后关闭车窗", "连接词"),
        ("打开空调还有天窗", "连接词"),
        ("空调开了吗", "疑问词"),
        ("空调怎么开", "疑问词"),
        ("别开空调", "否定词"),
        ("把它打开", "指代"),
        ("温度还是低一点", "指代"),
        ("打开空调？", "问号"),
        ("打开空调，关闭车窗", "标点"),
        ("请帮我把主驾驶的座椅加热打开并调到最高档位", "超过20字"),
        ("", "空输入"),
    ],
)
def test_reject_reason(message, expected):
    reason = reject_reason(message)
    assert reason is not None and expected in reason


@pytest.mark.parametrize("message", ["打开空调。", "请帮我把空调打开吧！", "温度设为22.5度", "播放周杰伦的歌"])
def test_reject_reason_passes(message):
    # 拒绝判断只管第 1–4 步，不判断是否在白名单里
    assert reject_reason(message) is None


def test_length_limit_counts_original_text_without_spaces(fast):
    # 口头词去掉后很短，但原文超过 20 字也不命中
    twenty = "麻烦你请帮我把主驾驶座椅加热调到三档吧呀"
    assert len(twenty) == 20
    assert fast.match(twenty).rule_id == "F08"
    assert fast.match(twenty + "啊") is None
    assert fast.match("座椅 加热 调到 两档") is not None


# ── 模板播报 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message, reply",
    [
        ("打开空调", "好的，空调已打开。"),
        ("温度调到二十二度", "好的，空调已调到22度。"),
        ("温度设为二十二点五度", "好的，空调已调到22.5度。"),
        ("风大一点", "好的，风量已调到4档。"),
        ("风量调到五档", "好的，风量已调到5档。"),
        ("打开前挡除雾", "好的，前挡除雾已打开。"),
        ("打开座椅加热", "好的，主驾座椅加热已调到2档。"),
        ("关闭副驾座椅加热", "好的，副驾座椅加热已关闭。"),
        ("副驾座椅加热调到三档", "好的，副驾座椅加热已调到3档。"),
        ("车窗开一半", "好的，主驾车窗已开到50%。"),
        ("打开后排车窗", "好的，后排车窗已打开。"),
        ("打开天窗", "好的，天窗已打开。"),
        ("打开后备箱", "好的，后备箱已打开。"),
        ("锁车", "好的，车门已上锁。"),
        ("打开氛围灯", "好的，氛围灯已打开。"),
        ("打开双闪", "好的，双闪已打开。"),
        ("播放", "好的，正在播放每日推荐（示例曲库）。"),
        ("暂停", "好的，已暂停播放。"),
        ("下一首", "好的，已切到下一首。"),
        ("上一首", "好的，已切到上一首。"),
        ("音量调到十五", "好的，音量已调到15。"),
        ("声音大一点", "好的，音量已调到15。"),
        ("静音", "好的，已静音。"),
        ("取消静音", "好的，已取消静音，音量为12。"),
    ],
)
def test_template_reply_on_success(orch, message, reply):
    result = try_fast(orch, message)
    assert result is not None, message
    assert result.response == reply
    assert result.route == "fast"
    assert result.blocked_actions == []


def test_relative_temperature_speaks_final_value(orch):
    result = try_fast(orch, "温度调高两度")
    assert result.response == "好的，空调已调到26度。"
    assert state(orch)["climate"]["temp"] == 26
    assert {"field": "climate.temp", "old": 24, "new": 26} in result.state_changes

    result = try_fast(orch, "温度调低一点")
    assert result.response == "好的，空调已调到25度。"
    assert state(orch)["climate"]["temp"] == 25


def test_unchanged_value_uses_same_template(orch):
    # 停车场景默认已上锁、空调关闭：值没变也用同一模板
    result = try_fast(orch, "锁车")
    assert result.response == "好的，车门已上锁。"
    assert result.state_changes == []

    result = try_fast(orch, "关闭空调")
    assert result.response == "好的，空调已关闭。"
    assert result.state_changes == []


def test_s04_clipped_temperature_speaks_notice_only(orch):
    result = try_fast(orch, "温度调到35度")
    assert result.response == "温度最高只能调到32度，已为你调到32度。"
    assert state(orch)["climate"]["temp"] == 32
    assert result.blocked_actions == []


def test_s05_clipped_volume_speaks_notice_only(orch):
    result = try_fast(orch, "音量调到一百")
    assert result.response == "音量最高只能调到30，已为你调到30。"
    assert state(orch)["media"]["volume"] == 30


def test_s03_highway_open_all_windows_limited_and_close_normal(orch):
    orch.vehicle.store.reset(VID, "highway")
    result = try_fast(orch, "打开所有车窗")
    assert result.response == "高速行驶中车窗最多只能开到30%，已为你开到30%。"
    assert state(orch)["body"]["windows"] == {"fl": 30, "fr": 30, "rl": 30, "rr": 30}

    result = try_fast(orch, "关闭所有车窗")
    assert result.response == "好的，所有车窗已关闭。"
    assert state(orch)["body"]["windows"] == {"fl": 0, "fr": 0, "rl": 0, "rr": 0}


def test_s01_open_trunk_while_driving_is_blocked(orch):
    orch.vehicle.store.reset(VID, "city_driving")
    result = try_fast(orch, "打开后备箱")
    reason = "车辆行驶中不能打开后备箱，请停车后再试。"

    assert result.response == reason
    assert (result.route, result.fast_rule) == ("fast", "F11")
    assert result.state_changes == []
    assert result.blocked_actions == [{"tool_name": "set_body", "rule_id": "S01", "reason": reason}]
    assert result.tool_traces[0]["result_success"] is False
    assert state(orch)["body"]["trunk_open"] is False
    assert orch.get_tool_trace(result.request_id)["blocked_actions"] == result.blocked_actions


def test_s07_low_battery_seat_heat_appends_reminder(orch):
    orch.vehicle.store.reset(VID, "low_battery")
    result = try_fast(orch, "打开座椅加热")
    assert result.response == "好的，主驾座椅加热已调到2档。当前电量只剩12%，座椅加热会增加耗电，建议尽快充电。"
    assert state(orch)["seats"]["seat_heat"]["driver"] == 2


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "error": "参数错误"},
        {"success": False, "unsupported": True, "reason": "暂不支持这个功能"},
        {"success": False},
        "not a dict",
    ],
)
def test_reply_returns_none_for_results_that_cannot_be_templated(result):
    match = FastMatch("F01", "空调开关", "set_climate", {"power": True}, C.CLIMATE_CONTROL, "空调")
    assert FastPath.reply(match, result, {}) is None


def test_reply_reads_changes_when_state_missing():
    match = FastMatch("F02", "设定温度", "set_climate", {"temperature": 22}, C.CLIMATE_CONTROL, "空调")
    result = {"success": True, "changes": [{"field": "climate.temp", "old": 24, "new": 22}], "notice": ""}
    assert FastPath.reply(match, result) == "好的，空调已调到22度。"


# ── 工具结果回落 ──────────────────────────────────────────────────────────────

def climate_tool(result=None, error=None):
    """替换注册表里的 set_climate：返回固定结果，或抛异常。"""

    def handler(req, args):
        if error is not None:
            raise error
        return result

    return make_tool("set_climate", "测试工具", {"power": {"type": "boolean"}}, handler)


@pytest.mark.parametrize(
    "tool",
    [
        climate_tool({"success": False, "error": "参数错误"}),
        climate_tool({"success": False, "unsupported": True, "reason": "暂不支持这个功能"}),
        climate_tool(error=RuntimeError("tool down")),
    ],
)
def test_tool_error_or_unsupported_falls_back_to_full_chain(tool):
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(text("好的，空调已打开。")))
    orch.register_tools({"set_climate": tool})

    assert try_fast(orch, "打开空调") is None
    assert orch.get_route_stats()["fast"] == 0

    result = asyncio.run(orch.run(make_request("打开空调", make_result(C.CLIMATE_CONTROL))))
    assert result.route == "slow"
    assert result.response == "好的，空调已打开。"
    assert len(clients["vehicle_control"].calls) == 1


def test_async_tool_handler_is_awaited():
    orch, _ = make_orchestrator()

    async def handler(req, args):
        return orch.vehicle.execute(req.user_id, {"action": "set_climate_power", "args": {"on": args["power"]}})

    orch.register_tools({"set_climate": make_tool("set_climate", "测试工具", {"power": {"type": "boolean"}}, handler)})
    result = try_fast(orch, "打开空调")
    assert result.response == "好的，空调已打开。"
    assert state(orch)["climate"]["on"] is True


# ── 开关 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "No", " off "])
def test_switch_off(monkeypatch, value):
    monkeypatch.setenv(FAST_PATH_ENV, value)
    fast = FastPath()
    assert fast.enabled is False
    assert fast.match("打开空调") is None

    orch, _ = make_orchestrator()
    assert try_fast(orch, "打开空调") is None
    assert orch.get_route_stats()["enabled"] is False


@pytest.mark.parametrize("value", ["on", "ON", "1", "true", ""])
def test_switch_on(monkeypatch, value):
    monkeypatch.setenv(FAST_PATH_ENV, value)
    assert FastPath().enabled is True
    assert FastPath().match("打开空调").rule_id == "F01"


def test_default_is_on():
    assert FastPath().enabled is True


def test_switch_off_run_goes_full_chain(monkeypatch):
    monkeypatch.setenv(FAST_PATH_ENV, "off")
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(
        tool_use("set_climate", {"power": True}), text("好的，空调已打开。"),
    ))
    result = asyncio.run(orch.run(make_request("打开空调", make_result(C.CLIMATE_CONTROL))))
    assert result.route == "slow"
    assert len(clients["vehicle_control"].calls) == 2
    assert state(orch)["climate"]["on"] is True


# ── 编排器接入 ────────────────────────────────────────────────────────────────

AGENT_TRACE_KEYS = {
    "agent_type", "tool_name", "tool_use_id", "input", "success", "result_success",
    "latency_ms", "cached", "reranked", "error",
}


def test_run_hits_fast_path_without_calling_any_model():
    orch, clients = make_orchestrator()
    req = make_request("请帮我打开空调吧")
    result = asyncio.run(orch.run(req))

    assert all(client.calls == [] for client in clients.values())
    assert all(agent.stats.total == 0 for agent in orch._agents.values())
    assert result.response == "好的，空调已打开。"
    assert (result.route, result.fast_rule) == ("fast", "F01")
    assert result.intent is C.CLIMATE_CONTROL
    assert result.agent_type is AgentType.VEHICLE_CONTROL
    assert result.agent_types == [AgentType.VEHICLE_CONTROL]
    assert result.escalated is False
    assert result.tools_used == ["set_climate"]
    assert result.sub_tasks == []
    assert result.routing_reason == "route=fast, rule=F01"
    assert result.state_changes == [{"field": "climate.on", "old": False, "new": True}]
    assert result.blocked_actions == []
    assert result.latency_ms >= 0

    intent_result = result.intent_result
    assert req.intent_result is intent_result and req.intent is C.CLIMATE_CONTROL
    assert intent_result.intent is C.CLIMATE_CONTROL
    assert intent_result.confidence_level == "high"
    assert intent_result.sources == {"decided_by": "fast_path", "fast_rule": "F01", "llm_status": "skipped"}
    assert "F01" in intent_result.reasoning

    trace = result.tool_traces[0]
    assert set(trace) == AGENT_TRACE_KEYS
    assert trace["agent_type"] == "vehicle_control"
    assert trace["tool_name"] == "set_climate"
    assert trace["input"] == {"power": True}
    assert (trace["success"], trace["result_success"], trace["error"]) == (True, True, "")

    recorded = orch.get_tool_trace(result.request_id)
    assert (recorded["route"], recorded["fast_rule"]) == ("fast", "F01")
    assert recorded["tool_calls"] == result.tool_traces
    assert recorded["state_changes"] == result.state_changes


def test_media_rule_routes_to_cabin():
    orch, _ = make_orchestrator()
    result = asyncio.run(orch.run(make_request("下一首")))
    assert result.agent_type is AgentType.CABIN
    assert result.intent is C.MEDIA_CONTROL
    assert result.tool_traces[0]["agent_type"] == "cabin"


def test_run_with_fast_path_false_uses_full_chain():
    orch, clients = make_orchestrator(vehicle_control=ScriptedClient(
        tool_use("set_climate", {"power": True}), text("好的，空调已打开。"),
    ))
    result = asyncio.run(orch.run(make_request("打开空调", make_result(C.CLIMATE_CONTROL)), fast_path=False))

    assert (result.route, result.fast_rule) == ("slow", None)
    assert len(clients["vehicle_control"].calls) == 2
    assert result.response == "好的，空调已打开。"
    assert orch.get_tool_trace(result.request_id)["route"] == "slow"
    assert orch.get_route_stats()["fast"] == 0


def test_miss_goes_full_chain():
    orch, clients = make_orchestrator(navigation=ScriptedClient(text("好的，正在规划回家的路线。")))
    result = asyncio.run(orch.run(make_request("导航回家", make_result(C.NAVIGATION))))

    assert (result.route, result.fast_rule) == ("slow", None)
    assert result.response == "好的，正在规划回家的路线。"
    assert len(clients["navigation"].calls) == 1
    trace = orch.get_tool_trace(result.request_id)
    assert (trace["route"], trace["fast_rule"]) == ("slow", None)


def test_route_stats_count_fast_and_slow():
    orch, _ = make_orchestrator(navigation=ScriptedClient(text("好的，正在规划回家的路线。")))
    assert orch.get_route_stats() == {
        "fast": 0, "slow": 0, "total": 0, "fast_ratio": 0.0, "rules": {}, "enabled": True,
    }

    for message in ("打开空调", "温度调到22度", "关闭空调"):
        assert asyncio.run(orch.run(make_request(message))).route == "fast"
    asyncio.run(orch.run(make_request("导航回家", make_result(C.NAVIGATION))))
    # 网络兜底回复同样计入完整链路
    asyncio.run(orch.run(make_request("嗯那个就是", make_result(C.OTHER, llm_status="failed", decided_by="none"))))
    # 未命中的 try_fast_path 不计数
    assert try_fast(orch, "导航回家") is None

    assert orch.get_route_stats() == {
        "fast": 3, "slow": 2, "total": 5, "fast_ratio": 0.6, "rules": {"F01": 2, "F02": 1}, "enabled": True,
    }


def test_fast_path_can_be_toggled_at_runtime():
    orch, _ = make_orchestrator()
    orch.fast_path.enabled = False
    assert try_fast(orch, "打开空调") is None
    assert orch.get_route_stats()["enabled"] is False
    orch.fast_path.enabled = True
    assert try_fast(orch, "打开空调").fast_rule == "F01"
