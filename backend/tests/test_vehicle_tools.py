"""
CabinWeave Agent 工具测试

覆盖 4 个角色的全部专属工具：
  - 车控 set_climate / set_body / set_lighting / set_drive_mode：经车辆服务写状态、changes 正确、
    相对调节、安全规则裁剪 / 调整 / 拒绝（S01、S03、S04、S05、S06、S07）、多条命令的结果合并、
    状态里没有的功能返回 unsupported
  - 导航 search_poi / plan_route / get_route_status：结果确定、写导航状态、需要确认时不写、
    家和公司从驾驶员偏好取、途经点和路线偏好
  - 用车顾问 lookup_warning / estimate_range：别名和模糊匹配、读车辆故障灯、未收录兜底、
    续航估算确定且低温多留余量、建议充电的判断
  - 座舱助手 control_media / set_volume / make_call / get_weather
  - 不合法的参数返回 error 而不是抛异常

全部是纯 Python，不访问网络、不调用模型。
"""
import pytest

from agents.agent_orchestrator import Request
from agents.tools import (
    _merge_results,
    advisor_tools,
    build_agent_tools,
    cabin_tools,
    estimate_arrival,
    navigation_tools,
    vehicle_control_tools,
)
from vehicle import mock_services
from vehicle.service import UNSUPPORTED_REASON, VehicleService
from vehicle.state import DEFAULT_STATE, VehicleStateStore
from vehicle.warning_lights import WARNING_LIGHTS, WARNING_NOT_FOUND, lookup_warning_light

VID = "u1"


@pytest.fixture
def service():
    return VehicleService(VehicleStateStore(default_scenario="parked"))


@pytest.fixture
def tools(service):
    return build_agent_tools(service)


def make_req(**kwargs):
    return Request(message="测试", user_id="u1", conv_id="c1", **kwargs)


def call(tools, name, args=None, req=None):
    return tools[name].handler(req or make_req(), dict(args or {}))


def state(service):
    return service.snapshot(VID)


# ── 工具集合 ──────────────────────────────────────────────────────────────────

def test_role_tool_sets_match_design(service):
    assert set(vehicle_control_tools(service)) == {"set_climate", "set_body", "set_lighting", "set_drive_mode"}
    assert set(navigation_tools(service)) == {"search_poi", "plan_route", "get_route_status"}
    assert set(advisor_tools(service)) == {"lookup_warning", "estimate_range"}
    assert set(cabin_tools(service)) == {
        "control_media", "set_volume", "make_call", "get_weather", "suggest_required_fields",
    }
    tools = build_agent_tools(service)
    assert len(tools) == 14
    for name, spec in tools.items():
        schema = spec.input_schema
        assert spec.name == name
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


def test_tools_operate_on_user_vehicle(tools, service):
    result = call(tools, "set_climate", {"power": True})
    assert result["success"] is True
    assert state(service)["climate"]["on"] is True


def test_merge_results_format_for_multiple_blocked_and_unsupported():
    merged = _merge_results([
        {"success": True, "changes": [{"field": "drive_mode", "old": "comfort", "new": "sport"}],
         "notice": "", "rule_ids": []},
        {"success": False, "blocked": True, "rule_id": "S01", "reason": "原因一。"},
        {"success": False, "blocked": True, "rule_id": "S02", "reason": "原因二。"},
        {"success": False, "unsupported": True, "reason": UNSUPPORTED_REASON, "feature": "雨刮"},
    ])
    assert merged == {
        "success": False,
        "changes": [{"field": "drive_mode", "old": "comfort", "new": "sport"}],
        "notice": "",
        "rule_ids": ["S01", "S02"],
        "blocked": True,
        "rule_id": "S01",
        "reason": "原因一。原因二。",
        "unsupported": True,
        "unsupported_features": ["雨刮"],
    }


# ── set_climate ───────────────────────────────────────────────────────────────

def test_set_climate_writes_state_and_returns_changes(tools, service):
    result = call(tools, "set_climate", {"power": True, "temperature": 22})
    assert result == {
        "success": True,
        "changes": [
            {"field": "climate.on", "old": False, "new": True},
            {"field": "climate.temp", "old": 24, "new": 22},
        ],
        "notice": "",
    }
    assert state(service)["climate"]["temp"] == 22
    assert state(service)["climate"]["on"] is True


def test_set_climate_relative_adjustment(tools, service):
    result = call(tools, "set_climate", {
        "temperature_delta": 2, "fan_delta": 1, "seat_heat_zone": "passenger", "seat_heat_delta": 1,
    })
    assert result["success"] is True
    climate = state(service)["climate"]
    assert (climate["temp"], climate["fan_level"]) == (26, 4)
    assert state(service)["seats"]["seat_heat"] == {"driver": 0, "passenger": 1}
    assert {"field": "climate.temp", "old": 24, "new": 26} in result["changes"]


def test_set_climate_temperature_clipped_with_notice(tools, service):
    result = call(tools, "set_climate", {"temperature": 40})
    assert result["success"] is True
    assert result["changes"][-1] == {"field": "climate.temp", "old": 24, "new": 32}
    assert result["notice"] == "温度最高只能调到32度，已为你调到32度。"
    assert result["rule_ids"] == ["S04"]
    assert state(service)["climate"]["temp"] == 32


def test_set_climate_multiple_settings_merged(tools, service):
    result = call(tools, "set_climate", {
        "power": True, "temperature": 22, "fan_level": 9, "defrost": True,
        "seat_heat_zone": "all", "seat_heat_level": 2,
    })
    assert result["success"] is True
    assert result["changes"] == [
        {"field": "climate.on", "old": False, "new": True},
        {"field": "climate.temp", "old": 24, "new": 22},
        {"field": "climate.fan_level", "old": 3, "new": 7},
        {"field": "climate.defrost", "old": False, "new": True},
        {"field": "seats.seat_heat.driver", "old": 0, "new": 2},
        {"field": "seats.seat_heat.passenger", "old": 0, "new": 2},
    ]
    assert result["notice"] == "风量最高只能调到7档，已为你调到7档。"
    assert result["rule_ids"] == ["S05"]
    assert "blocked" not in result and "unsupported" not in result
    current = state(service)
    assert current["climate"] == {"on": True, "temp": 22, "fan_level": 7, "defrost": True}


def test_set_climate_low_battery_reminders_are_concatenated(tools, service):
    service.store.reset(VID, "low_battery")
    result = call(tools, "set_climate", {"fan_level": 9, "seat_heat_level": 2})
    assert result["success"] is True
    assert result["rule_ids"] == ["S05", "S07"]
    assert result["notice"] == (
        "风量最高只能调到7档，已为你调到7档。"
        "当前电量只剩12%，调高风量会增加耗电，建议尽快充电。"
        "当前电量只剩12%，座椅加热会增加耗电，建议尽快充电。"
    )


def test_set_climate_power_off_and_seat_heat_default_driver(tools, service):
    call(tools, "set_climate", {"power": True})
    result = call(tools, "set_climate", {"power": False, "seat_heat_level": 1})
    assert result["changes"] == [
        {"field": "seats.seat_heat.driver", "old": 0, "new": 1},
        {"field": "climate.on", "old": True, "new": False},
    ]
    assert state(service)["climate"]["on"] is False


@pytest.mark.parametrize("feature, label", [
    ("seat_ventilation", "座椅通风"),
    ("steering_wheel_heat", "方向盘加热"),
    ("seat_massage", "座椅按摩"),
    ("座椅通风", "座椅通风"),
])
def test_set_climate_unsupported_comfort_features(tools, service, feature, label):
    before = state(service)
    result = call(tools, "set_climate", {"comfort_feature": feature})
    assert result["success"] is False
    assert result["unsupported"] is True
    assert result["reason"] == UNSUPPORTED_REASON
    assert result["unsupported_features"] == [label]
    assert result["changes"] == []
    assert state(service) == before


def test_set_climate_partial_unsupported_keeps_supported_changes(tools, service):
    result = call(tools, "set_climate", {"temperature": 22, "comfort_feature": "seat_ventilation"})
    assert result["success"] is False
    assert result["unsupported"] is True
    assert {"field": "climate.temp", "old": 24, "new": 22} in result["changes"]
    assert state(service)["climate"]["temp"] == 22

    rear = call(tools, "set_climate", {"seat_heat_zone": "rear", "seat_heat_level": 2})
    assert rear["unsupported"] is True and rear["unsupported_features"] == ["后排座椅加热"]


@pytest.mark.parametrize("args", [
    {"temperature": 22, "temperature_delta": 1},
    {"fan_level": 3, "fan_delta": 1},
    {"seat_heat_level": 1, "seat_heat_delta": 1},
    {"seat_heat_zone": "trunk", "seat_heat_level": 1},
    {"seat_heat_zone": "driver"},
    {"power": False, "temperature": 22},
    {"power": "yes"},
    {"temperature": "22"},
    {"fan_level": 2.5},
    {},
])
def test_set_climate_invalid_args_return_error(tools, service, args):
    before = state(service)
    result = call(tools, "set_climate", args)
    assert result["success"] is False
    assert result["error"]
    assert state(service) == before


# ── set_body ──────────────────────────────────────────────────────────────────

def test_set_body_trunk_blocked_while_driving(tools, service):
    service.store.reset(VID, "city_driving")
    result = call(tools, "set_body", {"part": "trunk", "action": "open"})
    assert result["success"] is False
    assert result["blocked"] is True
    assert result["rule_id"] == "S01"
    assert result["reason"] == "车辆行驶中不能打开后备箱，请停车后再试。"
    assert result["changes"] == []
    assert state(service)["body"]["trunk_open"] is False


def test_set_body_unlock_blocked_while_driving(tools, service):
    service.store.reset(VID, "city_driving")
    result = call(tools, "set_body", {"part": "door_lock", "action": "unlock"})
    assert (result["blocked"], result["rule_id"]) == (True, "S02")
    assert state(service)["body"]["doors_locked"] is True


def test_set_body_window_limited_to_30_on_highway(tools, service):
    service.store.reset(VID, "highway")
    result = call(tools, "set_body", {"part": "window", "action": "open"})
    assert result["success"] is True
    assert result["changes"] == [{"field": "body.windows.fl", "old": 0, "new": 30}]
    assert result["notice"] == "高速行驶中车窗最多只能开到30%，已为你开到30%。"
    assert result["rule_ids"] == ["S03"]

    # 前排两扇车窗展开成两条命令，重复的说明只保留一次
    both = call(tools, "set_body", {"part": "window", "action": "open", "zone": "front", "open_percent": 80})
    assert [change["field"] for change in both["changes"]] == ["body.windows.fr"]
    assert both["notice"] == "高速行驶中车窗最多只能开到30%，已为你开到30%。"


@pytest.mark.parametrize("zone, opened", [
    (None, {"fl"}),
    ("driver", {"fl"}),
    ("passenger", {"fr"}),
    ("front", {"fl", "fr"}),
    ("rear", {"rl", "rr"}),
    ("left", {"fl", "rl"}),
    ("左后", {"rl"}),
    ("副驾", {"fr"}),
    ("all", {"fl", "fr", "rl", "rr"}),
])
def test_set_body_window_zone_aliases(tools, service, zone, opened):
    args = {"part": "window", "action": "open", "open_percent": 50}
    if zone is not None:
        args["zone"] = zone
    result = call(tools, "set_body", args)
    assert result["success"] is True
    windows = state(service)["body"]["windows"]
    assert {name for name, value in windows.items() if value == 50} == opened
    assert all(value == 0 for name, value in windows.items() if name not in opened)


def test_set_body_sunroof_lock_and_close(tools, service):
    assert call(tools, "set_body", {"part": "sunroof", "action": "open", "open_percent": 50})["changes"] == [
        {"field": "body.sunroof", "old": 0, "new": 50},
    ]
    assert call(tools, "set_body", {"part": "door_lock", "action": "unlock"})["success"] is True
    assert state(service)["body"]["doors_locked"] is False
    call(tools, "set_body", {"part": "车锁", "action": "锁车"})
    assert state(service)["body"]["doors_locked"] is True
    call(tools, "set_body", {"part": "window", "action": "open", "zone": "all"})
    call(tools, "set_body", {"part": "window", "action": "close", "zone": "all"})
    assert set(state(service)["body"]["windows"].values()) == {0}
    assert call(tools, "set_body", {"part": "trunk", "action": "open"})["changes"] == [
        {"field": "body.trunk_open", "old": False, "new": True},
    ]


@pytest.mark.parametrize("part, label", [
    ("wiper", "雨刮"), ("mirror", "后视镜"), ("sunshade", "遮阳帘"), ("雨刮", "雨刮"), ("other", "这项车身功能"),
])
def test_set_body_unsupported_parts(tools, service, part, label):
    before = state(service)
    result = call(tools, "set_body", {"part": part, "action": "open"})
    assert result["success"] is False
    assert result["unsupported"] is True
    assert result["reason"] == UNSUPPORTED_REASON
    assert result["unsupported_features"] == [label]
    assert state(service) == before


@pytest.mark.parametrize("args", [
    {"part": "spoiler", "action": "open"},
    {"part": "door_lock", "action": "open"},
    {"part": "window", "action": "lock"},
    {"part": "window", "action": "open", "zone": "middle"},
    {"part": "window", "action": "open", "open_percent": 150},
    {"part": "window"},
])
def test_set_body_invalid_args_return_error(tools, service, args):
    before = state(service)
    result = call(tools, "set_body", args)
    assert result["success"] is False
    assert result["error"]
    assert state(service) == before


# ── set_lighting ──────────────────────────────────────────────────────────────

def test_set_lighting_ambient_and_hazard(tools, service):
    assert call(tools, "set_lighting", {"type": "ambient", "action": "on"})["changes"] == [
        {"field": "lights.ambient", "old": False, "new": True},
    ]
    call(tools, "set_lighting", {"type": "双闪", "action": "打开"})
    assert state(service)["lights"] == {"ambient": True, "hazard": True}
    call(tools, "set_lighting", {"type": "hazard", "action": "off"})
    assert state(service)["lights"]["hazard"] is False


@pytest.mark.parametrize("light, label", [("headlight", "大灯"), ("reading", "阅读灯"), ("阅读灯", "阅读灯")])
def test_set_lighting_unsupported(tools, service, light, label):
    result = call(tools, "set_lighting", {"type": light, "action": "on"})
    assert result["unsupported"] is True
    assert result["unsupported_features"] == [label]
    assert state(service)["lights"] == DEFAULT_STATE["lights"]


@pytest.mark.parametrize("args", [
    {"type": "ambient", "action": "blink"},
    {"type": "laser", "action": "on"},
    {"type": "ambient"},
])
def test_set_lighting_invalid_args_return_error(tools, args):
    result = call(tools, "set_lighting", args)
    assert result["success"] is False and result["error"]


# ── set_drive_mode ────────────────────────────────────────────────────────────

def test_set_drive_mode_switch(tools, service):
    assert call(tools, "set_drive_mode", {"mode": "sport"})["changes"] == [
        {"field": "drive_mode", "old": "comfort", "new": "sport"},
    ]
    call(tools, "set_drive_mode", {"mode": "经济"})
    assert state(service)["drive_mode"] == "eco"


def test_set_drive_mode_turning_off_esp_is_blocked_by_s06(tools, service):
    before = state(service)
    result = call(tools, "set_drive_mode", {"assist_feature": "esp", "assist_on": False})
    assert result["success"] is False
    assert (result["blocked"], result["rule_id"]) == (True, "S06")
    assert "车身稳定系统不能语音关闭" in result["reason"]
    assert state(service) == before

    lane = call(tools, "set_drive_mode", {"assist_feature": "车道保持", "assist_on": False})
    assert lane["rule_id"] == "S06" and "车道保持辅助" in lane["reason"]

    turn_on = call(tools, "set_drive_mode", {"assist_feature": "aeb", "assist_on": True})
    assert turn_on == {"success": True, "changes": [], "notice": ""}


def test_set_drive_mode_regen_unsupported_and_partial_results(tools, service):
    regen = call(tools, "set_drive_mode", {"regen_level": "强"})
    assert regen["unsupported"] is True
    assert regen["unsupported_features"] == ["能量回收调节"]
    assert regen["reason"] == UNSUPPORTED_REASON

    mixed = call(tools, "set_drive_mode", {"mode": "sport", "assist_feature": "esp", "assist_on": False})
    assert mixed["success"] is False
    assert mixed["changes"] == [{"field": "drive_mode", "old": "comfort", "new": "sport"}]
    assert mixed["blocked"] is True and mixed["rule_id"] == "S06"
    assert state(service)["drive_mode"] == "sport"


@pytest.mark.parametrize("args", [
    {"mode": "race"},
    {"assist_feature": "esp"},
    {"assist_on": False},
    {},
])
def test_set_drive_mode_invalid_args_return_error(tools, service, args):
    result = call(tools, "set_drive_mode", args)
    assert result["success"] is False and result["error"]
    assert state(service)["drive_mode"] == "comfort"


# ── search_poi ────────────────────────────────────────────────────────────────

def test_search_poi_is_deterministic_and_compact(tools):
    args = {"category": "charging_station"}
    first = call(tools, "search_poi", args)
    assert first == call(tools, "search_poi", args)
    assert first["success"] is True
    assert first["results"][0]["name"] == "龙华中路快充站（示例）"
    assert [poi["distance_km"] for poi in first["results"]] == sorted(poi["distance_km"] for poi in first["results"])
    assert set(first["results"][0]) == {
        "name", "category_name", "address", "distance_km", "duration_min",
        "available_piles", "total_piles", "power_kw",
    }


def test_search_poi_sort_and_require_available(tools):
    result = call(tools, "search_poi", {"category": "充电站", "sort_by": "available", "require_available": True})
    names = [poi["name"] for poi in result["results"]]
    assert names[0] == "虹桥枢纽超充站（示例）"
    assert "漕河泾园区充电站（示例）" not in names
    assert all(poi["available_piles"] > 0 for poi in result["results"])

    keyword = call(tools, "search_poi", {"keyword": "停车场"})
    assert keyword["total"] == 3


@pytest.mark.parametrize("args", [{"category": "加油站"}, {"sort_by": "price"}, {"require_available": "yes"}])
def test_search_poi_invalid_args_return_error(tools, args):
    result = call(tools, "search_poi", args)
    assert result["success"] is False and result["error"]


# ── plan_route / get_route_status ─────────────────────────────────────────────

def test_plan_route_success_writes_navigation_state(tools, service):
    result = call(tools, "plan_route", {"destination": "浦东机场"})
    assert result["success"] is True
    assert result["destination"] == "浦东国际机场（示例）"
    assert (result["distance_km"], result["duration_min"]) == (52.0, 58)
    assert result["preference"] == "最快"
    assert result["changes"] == [
        {"field": "navigation.active", "old": False, "new": True},
        {"field": "navigation.destination", "old": None, "new": "浦东国际机场（示例）"},
        {"field": "navigation.eta_min", "old": None, "new": 58},
    ]
    assert state(service)["navigation"] == {"active": True, "destination": "浦东国际机场（示例）", "eta_min": 58}


def test_plan_route_preference_and_waypoints(tools, service):
    fastest = call(tools, "plan_route", {"destination": "浦东机场"})
    avoid = call(tools, "plan_route", {"destination": "浦东机场", "preference": "避开高速"})
    assert avoid["preference"] == "避开高速"
    assert avoid["distance_km"] == round(52.0 * 1.15, 1)
    assert avoid["duration_min"] > fastest["duration_min"]

    args = {"destination": "浦东机场", "waypoints": ["徐汇滨江超充站（示例）"]}
    via, again = call(tools, "plan_route", args), call(tools, "plan_route", args)
    assert (via["distance_km"], via["duration_min"]) == (again["distance_km"], again["duration_min"])
    assert via["waypoints"] == ["徐汇滨江超充站（示例）"]
    # 途经点 2.8 km 在去机场的同一方向上：2.8 + (52 - 2.8) = 52 km，时长按总距离重算
    assert (via["distance_km"], via["duration_min"]) == (52.0, 58)
    assert again["changes"] == []          # 同样的路线再规划一次，导航状态没有变化

    assert call(tools, "plan_route", {"destination": "浦东机场", "preference": "scenic"})["error"]
    assert call(tools, "plan_route", {"destination": "浦东机场", "waypoints": ["a", "b", "c", "d"]})["error"]


def test_plan_route_need_confirmation_does_not_write_state(tools, service):
    before = state(service)
    result = call(tools, "plan_route", {"destination": "机场"})
    assert result["success"] is False
    assert result["need_confirmation"] is True
    assert [item["name"] for item in result["candidates"]] == ["虹桥国际机场（示例）", "浦东国际机场（示例）"]
    assert "changes" not in result

    unknown = call(tools, "plan_route", {"destination": "火星基地"})
    assert unknown["need_confirmation"] is True and unknown["candidates"] == []

    waypoint = call(tools, "plan_route", {"destination": "浦东机场", "waypoints": ["火车站"]})
    assert waypoint["need_confirmation"] is True and waypoint["waypoint"] == "火车站"
    assert state(service) == before


PROFILE = {"places": {"home": "上海南站", "company": "闵行区莘庄工业区某某路88号（示例）"}}


def test_plan_route_home_and_company_from_profile(tools, service):
    home = call(tools, "plan_route", {"destination": "回家"}, make_req(profile=PROFILE))
    assert home["success"] is True
    assert home["destination"] == "家"
    assert home["distance_km"] == 4.8          # 地址能在模拟地图里查到，用地图距离
    assert "address" not in home               # 不把驾驶员的住址放进返回值
    assert state(service)["navigation"]["destination"] == "家"

    company = call(tools, "plan_route", {"destination": "去单位"}, make_req(profile=PROFILE))
    again = call(tools, "plan_route", {"destination": "公司"}, make_req(profile=PROFILE))
    assert company["destination"] == "公司"
    assert 3 <= company["distance_km"] <= 30
    assert (company["distance_km"], company["duration_min"]) == (again["distance_km"], again["duration_min"])
    assert state(service)["navigation"]["destination"] == "公司"


@pytest.mark.parametrize("destination, need", [("回家", "home_address"), ("家", "home_address"), ("公司", "company_address")])
def test_plan_route_without_profile_asks_for_address(tools, service, destination, need):
    before = state(service)
    result = call(tools, "plan_route", {"destination": destination}, make_req(profile={"places": {}}))
    assert result["success"] is False
    assert result["need_info"] == need
    assert "地址" in result["reason"]
    assert state(service) == before


def test_get_route_status_without_and_with_navigation(tools):
    idle = call(tools, "get_route_status")
    assert idle == {"success": True, "active": False, "message": "当前没有在导航。"}

    call(tools, "plan_route", {"destination": "上海站"})
    status = call(tools, "get_route_status")
    assert status == call(tools, "get_route_status")
    assert status["active"] is True
    assert status["destination"] == "上海站（示例）"
    assert status["eta_min"] == 22
    assert status["traffic"] in ("畅通", "缓行", "拥堵")


# ── lookup_warning ────────────────────────────────────────────────────────────

def test_warning_table_covers_common_ev_lights():
    assert len(WARNING_LIGHTS) >= 10
    assert len({item["code"] for item in WARNING_LIGHTS}) == len(WARNING_LIGHTS)
    for item in WARNING_LIGHTS:
        assert set(item) == {"code", "name", "aliases", "color", "severity", "meaning", "advice"}
        assert item["color"] in ("红色", "黄色")
        if item["color"] == "红色":
            assert "停车" in item["advice"]
            assert "救援" in item["advice"] or "售后" in item["advice"]


@pytest.mark.parametrize("signal, name", [
    ("胎压灯", "胎压报警"),
    ("胎压报警了", "胎压报警"),
    ("TPMS", "胎压报警"),
    ("动力电池过然", "动力电池过热"),   # 同音错字，相似度兜底
    ("电池过热了", "动力电池过热"),
    ("动力电池故障", "动力电池故障"),
    ("ESP灯亮了", "ESP故障"),
    ("乌龟灯", "功率受限"),
    ("电量低", "低电量"),
    ("蓄电池电量低", "12V低压蓄电池电量低"),
    ("12V电瓶没电了", "12V低压蓄电池电量低"),
    ("w05", "制动系统故障"),
])
def test_lookup_warning_matches_aliases(tools, signal, name):
    result = call(tools, "lookup_warning", {"signal": signal})
    assert result["success"] is True
    assert result["name"] == name
    assert result["signal"] == signal
    assert {"code", "color", "severity", "meaning", "advice", "note"} <= set(result)


def test_lookup_warning_red_light_advice(tools):
    result = call(tools, "lookup_warning", {"signal": "电池过热"})
    assert (result["color"], result["severity"]) == ("红色", "紧急")
    assert "停车" in result["advice"]
    tpms = call(tools, "lookup_warning", {"signal": "胎压报警"})
    assert tpms["color"] == "黄色"


@pytest.mark.parametrize("signal", ["发动机故障", "故障", "雨刮灯"])
def test_lookup_warning_not_found(tools, signal):
    result = call(tools, "lookup_warning", {"signal": signal})
    assert result == {"success": False, "not_found": True, "signal": signal, "reason": WARNING_NOT_FOUND}
    assert "用户手册" in WARNING_NOT_FOUND and "售后" in WARNING_NOT_FOUND
    assert lookup_warning_light(signal) is None


def test_lookup_warning_without_signal_reads_vehicle_lights(tools, service):
    empty = call(tools, "lookup_warning")
    assert empty["success"] is True and empty["warnings"] == []
    assert empty["message"]

    service.store.reset(VID, "tpms_warning")
    result = call(tools, "lookup_warning")
    assert result["source"] == "vehicle"
    assert [warning["name"] for warning in result["warnings"]] == ["胎压报警"]

    service.store.update(VID, {"warning_lights": ["胎压报警", "未知提示", "电池过热"]})
    ranked = call(tools, "lookup_warning")["warnings"]
    assert [warning.get("name") for warning in ranked] == ["动力电池过热", "胎压报警", None]
    assert ranked[-1] == {"signal": "未知提示", "not_found": True, "reason": WARNING_NOT_FOUND}


# ── estimate_range ────────────────────────────────────────────────────────────

def test_estimate_range_is_deterministic_and_marked_as_estimate(tools, service):
    before = state(service)
    result = call(tools, "estimate_range", {"destination": "浦东机场"})
    assert result == call(tools, "estimate_range", {"destination": "浦东机场"})
    assert result["success"] is True
    assert "仅为估算" in result["note"]
    assert result["destination"] == "浦东国际机场（示例）"
    # 80% / 400 km，52 km：80 × (400 - 52) / 400 = 69.6 → 69
    assert (result["distance_km"], result["arrival_battery_pct"], result["arrival_range_km"]) == (52.0, 69, 348)
    assert (result["reachable"], result["suggest_charging"]) == (True, False)
    assert "low_temp_margin_pct" not in result
    assert state(service) == before


def test_estimate_range_keeps_more_margin_in_low_temperature(tools, service):
    service.store.update(VID, {"range_km": 300})
    mild = call(tools, "estimate_range", {"distance_km": 52})
    service.store.update(VID, {"outside_temp_c": 5})
    cool = call(tools, "estimate_range", {"distance_km": 52})
    service.store.update(VID, {"outside_temp_c": -5})
    cold = call(tools, "estimate_range", {"distance_km": 52})

    assert "low_temp_margin_pct" not in mild
    assert cool["low_temp_margin_pct"] == 10
    assert cold["low_temp_margin_pct"] == 25
    assert mild["arrival_battery_pct"] > cool["arrival_battery_pct"] > cold["arrival_battery_pct"]
    # 80% / 300 km、-5℃：需要 52 × 1.25 = 65 km，80 × 235 / 300 = 62.7 → 62
    assert (cold["arrival_battery_pct"], cold["arrival_range_km"]) == (62, 235)


def test_estimate_range_suggests_charging(tools, service):
    service.store.reset(VID, "low_battery")
    near = call(tools, "estimate_range", {"distance_km": 52})
    assert (near["reachable"], near["arrival_battery_pct"], near["suggest_charging"]) == (True, 1, True)
    assert "充电" in near["advice"]

    far = call(tools, "estimate_range", {"distance_km": 100})
    assert (far["reachable"], far["arrival_battery_pct"], far["arrival_range_km"]) == (False, 0, 0)
    assert far["suggest_charging"] is True


def test_estimate_arrival_threshold_is_15_percent():
    assert estimate_arrival(325, 80, 400, 20)["arrival_battery_pct"] == 15
    assert estimate_arrival(325, 80, 400, 20)["suggest_charging"] is False
    assert estimate_arrival(326, 80, 400, 20)["arrival_battery_pct"] == 14
    assert estimate_arrival(326, 80, 400, 20)["suggest_charging"] is True
    assert estimate_arrival(10, 0, 0, 20)["reachable"] is False


def test_estimate_range_destination_from_profile_and_confirmation(tools):
    home = call(tools, "estimate_range", {"destination": "家"}, make_req(profile=PROFILE))
    assert home["destination"] == "家" and home["distance_km"] == 4.8
    missing = call(tools, "estimate_range", {"destination": "家"})
    assert missing["need_info"] == "home_address"
    ambiguous = call(tools, "estimate_range", {"destination": "机场"})
    assert ambiguous["need_confirmation"] is True and len(ambiguous["candidates"]) == 2


@pytest.mark.parametrize("args", [
    {"destination": "浦东机场", "distance_km": 50},
    {},
    {"distance_km": 0},
    {"distance_km": "50"},
])
def test_estimate_range_invalid_args_return_error(tools, args):
    result = call(tools, "estimate_range", args)
    assert result["success"] is False and result["error"]


# ── control_media / set_volume ────────────────────────────────────────────────

def test_control_media_play_query_pause_and_next(tools, service):
    result = call(tools, "control_media", {"action": "play", "query": "周杰伦"})
    title = mock_services.search_media("周杰伦")["title"]
    assert result["success"] is True
    assert result["changes"] == [
        {"field": "media.playing", "old": False, "new": True},
        {"field": "media.title", "old": "", "new": title},
    ]
    assert (result["playing"], result["title"]) == (True, title)

    paused = call(tools, "control_media", {"action": "暂停"})
    assert paused["changes"] == [{"field": "media.playing", "old": True, "new": False}]
    assert state(service)["media"]["title"] == title
    assert call(tools, "control_media", {"action": "next"})["playing"] is True


def test_control_media_play_without_content_uses_daily_recommendation(tools, service):
    result = call(tools, "control_media", {"action": "play"})
    assert result["title"] == "每日推荐（示例曲库）"
    assert state(service)["media"]["playing"] is True


def test_control_media_invalid_action_returns_error(tools):
    for args in ({"action": "shuffle"}, {}):
        result = call(tools, "control_media", args)
        assert result["success"] is False and result["error"]


def test_set_volume_level_delta_and_clip(tools, service):
    assert call(tools, "set_volume", {"level": 20})["changes"] == [{"field": "media.volume", "old": 12, "new": 20}]
    assert call(tools, "set_volume", {"delta": -5})["volume"] == 15
    clipped = call(tools, "set_volume", {"level": 45})
    assert clipped["volume"] == 30
    assert clipped["notice"] == "音量最高只能调到30，已为你调到30。"
    assert clipped["rule_ids"] == ["S05"]


def test_set_volume_mute_and_unmute(tools, service):
    muted = call(tools, "set_volume", {"mute": True})
    assert muted["changes"] == [{"field": "media.volume", "old": 12, "new": 0}]
    assert muted["volume"] == 0
    unmuted = call(tools, "set_volume", {"mute": False})
    assert unmuted["volume"] == DEFAULT_STATE["media"]["volume"]
    call(tools, "set_volume", {"level": 20})
    assert call(tools, "set_volume", {"mute": False})["changes"] == []
    assert state(service)["media"]["volume"] == 20


@pytest.mark.parametrize("args", [{"level": 10, "mute": True}, {"level": 10, "delta": 2}, {}, {"level": "10"}])
def test_set_volume_invalid_args_return_error(tools, service, args):
    result = call(tools, "set_volume", args)
    assert result["success"] is False and result["error"]
    assert state(service)["media"]["volume"] == 12


# ── make_call / get_weather ───────────────────────────────────────────────────

def test_make_call_unique_match(tools, service):
    before = state(service)
    result = call(tools, "make_call", {"contact": "老婆"})
    assert result["success"] is True
    assert result["simulated"] is True
    assert result["message"] == "正在呼叫 李娜（139****5678）"
    assert state(service) == before


def test_make_call_multiple_matches_and_not_found(tools):
    multiple = call(tools, "make_call", {"contact": "张"})
    assert multiple["success"] is False and multiple["need_confirmation"] is True
    assert {item["name"] for item in multiple["candidates"]} == {"张伟", "张敏"}
    assert set(multiple["candidates"][0]) == {"name", "relation", "phone"}

    missing = call(tools, "make_call", {"contact": "老王"})
    assert missing["success"] is False and missing["not_found"] is True
    assert "老王" in missing["reason"]


def test_get_weather_defaults_to_vehicle_city(tools, service):
    assert call(tools, "get_weather") == mock_services.get_weather("上海")
    service.store.update(VID, {"location": {"city": "杭州"}})
    result = call(tools, "get_weather")
    assert result["city"] == "杭州"
    assert result == mock_services.get_weather("杭州")
    assert call(tools, "get_weather", {"city": "北京", "date": "明天"}) == mock_services.get_weather("北京", "明天")


# ── 不合法参数不抛异常 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, args", [
    ("set_climate", {"seat_heat_zone": 3}),
    ("set_body", {"part": 1, "action": "open"}),
    ("set_lighting", {"type": "ambient", "action": True}),
    ("set_drive_mode", {"mode": "warp"}),
    ("search_poi", {"category": "airport"}),
    ("plan_route", {"destination": "上海站", "waypoints": "上海南站", "preference": "slow"}),
    ("plan_route", {"destination": "上海站", "waypoints": [1, 2]}),
    ("lookup_warning", {"signal": 123}),
    ("estimate_range", {"distance_km": -1}),
    ("control_media", {"action": "rewind"}),
    ("set_volume", {"mute": "yes"}),
    ("make_call", {"contact": 10086}),
    ("get_weather", {"city": 1}),
])
def test_invalid_enum_or_type_returns_error_instead_of_raising(tools, name, args):
    result = call(tools, name, args)
    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["error"]
