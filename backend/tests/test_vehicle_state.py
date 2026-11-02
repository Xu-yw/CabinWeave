"""
CabinWeave 车辆状态、车辆服务与模拟外部服务测试

覆盖：默认状态字段符合约定；按 user_id 隔离（一个用户一辆车）；snapshot 是深拷贝；6 个场景预设；
reset / update（含未知键、类型不符时报错且不生效）；环境变量指定默认场景；
VehicleService 的 changes 只含变化字段、未知 action 返回 unsupported、参数不合法返回 error；
模拟地图、路线、天气、媒体、联系人的结果确定且符合约定。

全部是纯 Python，不访问网络、不调用模型。
"""
import logging
import re

import pytest

from agents.agent_orchestrator import SNAPSHOT_KEYS
from vehicle import mock_services
from vehicle.service import COMMANDS, UNSUPPORTED_REASON, VehicleService, make_command
from vehicle.state import (
    DEFAULT_STATE,
    SCENARIOS,
    VehicleStateStore,
    build_state,
)

EXPECTED_DEFAULT = {
    "speed_kmh": 0, "gear": "P", "drive_mode": "comfort",
    "battery_pct": 80, "range_km": 400,
    "climate": {"on": False, "temp": 24, "fan_level": 3, "defrost": False},
    "seats": {"seat_heat": {"driver": 0, "passenger": 0}},
    "body": {"windows": {"fl": 0, "fr": 0, "rl": 0, "rr": 0}, "sunroof": 0, "doors_locked": True, "trunk_open": False},
    "lights": {"ambient": False, "hazard": False},
    "tire_pressure_kpa": {"fl": 250, "fr": 250, "rl": 250, "rr": 250},
    "warning_lights": [],
    "next_service_km": 5000,
    "outside_temp_c": 20,
    "location": {"city": "上海", "address": "徐汇区漕溪北路（示例）"},
    "navigation": {"active": False, "destination": None, "eta_min": None},
    "media": {"playing": False, "title": "", "volume": 12},
}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("CABINWEAVE_VEHICLE_DEFAULT_SCENARIO", raising=False)
    return VehicleStateStore()


@pytest.fixture
def service(store):
    return VehicleService(store)


# ── 默认状态与快照 ────────────────────────────────────────────────────────────

def test_default_state_matches_design():
    assert DEFAULT_STATE == EXPECTED_DEFAULT
    assert build_state("parked") == EXPECTED_DEFAULT
    # 车辆快照按角色裁剪的顶层键与状态字段一一对齐
    snapshot_keys = set().union(*(set(keys) for keys in SNAPSHOT_KEYS.values()))
    assert snapshot_keys == set(DEFAULT_STATE)


def test_get_creates_default_vehicle_and_returns_internal_state(store):
    state = store.get("a")
    assert state == EXPECTED_DEFAULT
    state["speed_kmh"] = 30
    assert store.get("a")["speed_kmh"] == 30
    with pytest.raises(ValueError):
        store.get("")


def test_vehicles_are_isolated_by_id(store):
    store.update("a", {"speed_kmh": 60, "climate": {"temp": 20}})
    assert store.snapshot("a")["speed_kmh"] == 60
    assert store.snapshot("b") == EXPECTED_DEFAULT


def test_snapshot_is_deep_copy(store):
    snapshot = store.snapshot("a")
    snapshot["climate"]["temp"] = 30
    snapshot["warning_lights"].append("胎压报警")
    assert store.snapshot("a") == EXPECTED_DEFAULT


# ── 场景预设与 reset ──────────────────────────────────────────────────────────

def test_six_scenarios():
    assert set(SCENARIOS) == {"parked", "city_driving", "highway", "low_battery", "tpms_warning", "winter"}
    city = build_state("city_driving")
    assert (city["speed_kmh"], city["gear"]) == (40, "D")
    highway = build_state("highway")
    assert highway["speed_kmh"] == 110 and highway["gear"] == "D"
    low = build_state("low_battery")
    assert low["battery_pct"] == 12 and low["range_km"] < EXPECTED_DEFAULT["range_km"]
    tpms = build_state("tpms_warning")
    assert tpms["tire_pressure_kpa"]["fl"] < 200
    assert all(tpms["tire_pressure_kpa"][wheel] == 250 for wheel in ("fr", "rl", "rr"))
    assert "胎压报警" in tpms["warning_lights"]
    winter = build_state("winter")
    assert winter["outside_temp_c"] == -5 and winter["range_km"] < EXPECTED_DEFAULT["range_km"]
    # 场景之间互不影响，默认状态不被修改
    assert DEFAULT_STATE == EXPECTED_DEFAULT


def test_reset_to_default_or_scenario(store):
    store.update("a", {"speed_kmh": 50, "body": {"windows": {"fl": 100}}})
    assert store.reset("a", "highway")["speed_kmh"] == 110
    assert store.snapshot("a")["body"]["windows"]["fl"] == 0
    assert store.reset("a") == EXPECTED_DEFAULT
    with pytest.raises(ValueError):
        store.reset("a", "moon_base")


def test_reset_keeps_internal_reference_valid(store):
    state = store.get("a")
    store.reset("a", "winter")
    assert state["outside_temp_c"] == -5


def test_default_scenario_from_env(monkeypatch, caplog):
    monkeypatch.setenv("CABINWEAVE_VEHICLE_DEFAULT_SCENARIO", "highway")
    store = VehicleStateStore()
    assert store.default_scenario == "highway"
    assert store.snapshot("new")["speed_kmh"] == 110
    assert store.reset("new", "parked")["speed_kmh"] == 0
    assert store.reset("new")["speed_kmh"] == 110

    monkeypatch.setenv("CABINWEAVE_VEHICLE_DEFAULT_SCENARIO", "flying")
    with caplog.at_level(logging.WARNING, logger="vehicle.state"):
        store = VehicleStateStore()
    assert store.default_scenario == "parked"
    assert store.snapshot("new") == EXPECTED_DEFAULT
    assert any("CABINWEAVE_VEHICLE_DEFAULT_SCENARIO" in record.getMessage() for record in caplog.records)

    with pytest.raises(ValueError):
        VehicleStateStore(default_scenario="flying")


# ── update ────────────────────────────────────────────────────────────────────

def test_update_merges_nested_patch(store):
    snapshot = store.update("a", {
        "speed_kmh": 35,
        "gear": "D",
        "battery_pct": 9,
        "tire_pressure_kpa": {"rr": 190},
        "warning_lights": ["胎压报警"],
        "navigation": {"destination": "上海站（示例）"},
    })
    assert snapshot["speed_kmh"] == 35
    assert snapshot["tire_pressure_kpa"] == {"fl": 250, "fr": 250, "rl": 250, "rr": 190}
    assert snapshot["warning_lights"] == ["胎压报警"]
    assert snapshot["navigation"]["destination"] == "上海站（示例）"
    assert store.snapshot("a") == snapshot


@pytest.mark.parametrize(
    "patch",
    [
        {"wiper": True},                                # 未知顶层键
        {"climate": {"temp_driver": 22}},               # 未知嵌套键
        {"speed_kmh": 40, "seats": {"ventilation": 1}}, # 有一个未知键时整次不生效
        {"climate": 22},                                # 分组不能按标量更新
        {"speed_kmh": {"value": 40}},                   # 标量不能按对象更新
        {"speed_kmh": "fast"},                          # 类型不符
        {"lights": {"hazard": 1}},                      # 布尔字段不接受数值
        {"gear": "d"},                                  # 档位只能是 P/R/N/D
        {"drive_mode": "race"},
        {"battery_pct": 120},
        {"speed_kmh": -5},
    ],
)
def test_update_rejects_invalid_patch_without_partial_apply(store, patch):
    with pytest.raises(ValueError):
        store.update("a", patch)
    assert store.snapshot("a") == EXPECTED_DEFAULT


# ── VehicleService ────────────────────────────────────────────────────────────

def test_execute_returns_only_changed_fields(service):
    result = service.execute("a", make_command("set_temperature", temp=22))
    assert result == {
        "success": True,
        "changes": [
            {"field": "climate.on", "old": False, "new": True},
            {"field": "climate.temp", "old": 24, "new": 22},
        ],
        "notice": "",
        "rule_ids": [],
    }
    # 同样的命令再执行一次：值没变，不产生 changes
    again = service.execute("a", make_command("set_temperature", temp=22))
    assert again["success"] is True and again["changes"] == []
    assert service.snapshot("a")["climate"] == {"on": True, "temp": 22, "fan_level": 3, "defrost": False}


def test_execute_covers_command_vocabulary(service):
    def run(action, **args):
        result = service.execute("a", make_command(action, **args))
        assert result["success"] is True, result
        return {change["field"]: change["new"] for change in result["changes"]}

    assert run("set_climate_power", on=True) == {"climate.on": True}
    assert run("set_temperature", delta=-2) == {"climate.temp": 22}
    assert run("set_fan", delta=2) == {"climate.fan_level": 5}
    assert run("set_defrost", on=True) == {"climate.defrost": True}
    assert run("set_seat_heat", level=2) == {"seats.seat_heat.driver": 2}
    assert run("set_seat_heat", zone="all", delta=1) == {"seats.seat_heat.driver": 3, "seats.seat_heat.passenger": 1}
    assert run("set_window", open_percent=50) == {"body.windows.fl": 50}
    assert run("set_window", zone="all", open_percent=20) == {
        "body.windows.fl": 20, "body.windows.fr": 20, "body.windows.rl": 20, "body.windows.rr": 20,
    }
    assert run("set_sunroof", open_percent=40) == {"body.sunroof": 40}
    assert run("set_trunk", open=True) == {"body.trunk_open": True}
    assert run("set_door_lock", locked=False) == {"body.doors_locked": False}
    assert run("set_ambient_light", on=True) == {"lights.ambient": True}
    assert run("set_hazard", on=True) == {"lights.hazard": True}
    assert run("set_drive_mode", mode="sport") == {"drive_mode": "sport"}
    assert run("set_volume", delta=3) == {"media.volume": 15}
    assert run("media_control", op="play", title="轻音乐相关内容（示例曲库）") == {
        "media.playing": True, "media.title": "轻音乐相关内容（示例曲库）",
    }
    assert run("media_control", op="pause") == {"media.playing": False}
    assert run("media_control", op="next") == {"media.playing": True}
    assert run("set_navigation", destination="上海站（示例）", eta_min=25) == {
        "navigation.active": True, "navigation.destination": "上海站（示例）", "navigation.eta_min": 25,
    }
    assert run("cancel_navigation") == {
        "navigation.active": False, "navigation.destination": None, "navigation.eta_min": None,
    }
    assert run("set_active_safety", feature="esp", on=True) == {}
    assert set(COMMANDS) == {
        "set_climate_power", "set_temperature", "set_fan", "set_defrost", "set_seat_heat", "set_window",
        "set_sunroof", "set_trunk", "set_door_lock", "set_ambient_light", "set_hazard", "set_drive_mode",
        "set_volume", "media_control", "set_navigation", "cancel_navigation", "set_active_safety",
    }


def test_unknown_action_is_unsupported_and_state_unchanged(service):
    result = service.execute("a", make_command("set_wiper", on=True))
    assert result == {"success": False, "unsupported": True, "reason": UNSUPPORTED_REASON}
    assert UNSUPPORTED_REASON == "暂不支持这个功能"
    assert service.snapshot("a") == EXPECTED_DEFAULT


@pytest.mark.parametrize(
    "command",
    [
        "set_hazard",                                                  # 不是 dict
        {"args": {"on": True}},                                        # 缺 action
        {"action": "set_hazard", "args": ["on"]},                      # args 不是对象
        make_command("set_hazard"),                                    # 缺必填参数
        make_command("set_hazard", on="yes"),                          # 类型不对
        make_command("set_temperature"),                               # temp / delta 都没给
        make_command("set_temperature", temp=22, delta=1),             # temp / delta 都给了
        make_command("set_fan", level=2.5),                            # 风量必须是整数
        make_command("set_window", zone="front", open_percent=50),     # 音区不对
        make_command("set_window", open_percent=120),                  # 开度超出 0–100
        make_command("set_drive_mode", mode="race"),
        make_command("set_navigation", destination="  "),
        make_command("set_hazard", on=True, color="red"),              # 不认识的参数
    ],
)
def test_invalid_command_returns_error_and_state_unchanged(service, command):
    result = service.execute("a", command)
    assert result["success"] is False
    assert result.get("error")
    assert service.snapshot("a") == EXPECTED_DEFAULT


def test_execute_rejects_empty_user_id(service):
    result = service.execute("", make_command("set_hazard", on=True))
    assert result["success"] is False and "user_id" in result["error"]


# ── 模拟外部服务 ──────────────────────────────────────────────────────────────

def test_poi_dataset_follows_conventions():
    pois = mock_services.POIS
    assert 12 <= len(pois) <= 18
    assert {poi["category"] for poi in pois} == {
        "charging_station", "parking", "service_area", "repair", "restaurant",
    }
    assert all(poi["name"].endswith("（示例）") for poi in pois)
    assert all(poi["distance_km"] > 0 and poi["duration_min"] >= 1 for poi in pois)
    for poi in pois:
        if poi["category"] == "charging_station":
            assert 0 <= poi["available_piles"] <= poi["total_piles"]
            assert poi["power_kw"] > 0
    assert all(item["name"].endswith("（示例）") for item in mock_services.DESTINATIONS)


def test_search_poi_by_category_sorted_by_distance():
    result = mock_services.search_poi(category="充电站")
    assert result["success"] is True
    assert result["category"] == "charging_station"
    distances = [poi["distance_km"] for poi in result["results"]]
    assert distances == sorted(distances)
    assert all(poi["category"] == "charging_station" for poi in result["results"])
    assert mock_services.search_poi(category="charging_station") == result
    # 返回的是拷贝，调用方修改不影响数据
    result["results"][0]["name"] = "改掉"
    assert mock_services.search_poi(category="充电站")["results"][0]["name"].endswith("（示例）")


def test_search_poi_filters_and_sorts():
    all_chargers = mock_services.search_poi(category="charging_station", limit=20)["results"]
    assert any(poi["available_piles"] == 0 for poi in all_chargers)
    available = mock_services.search_poi(category="charging_station", require_available=True, limit=20)["results"]
    assert available and all(poi["available_piles"] > 0 for poi in available)
    assert len(available) == len(all_chargers) - 1

    by_power = mock_services.search_poi(category="charging_station", sort_by="power", limit=20)["results"]
    assert [poi["power_kw"] for poi in by_power] == sorted((poi["power_kw"] for poi in by_power), reverse=True)
    by_free = mock_services.search_poi(category="充电站", sort_by="空闲桩", limit=20)["results"]
    assert by_free[0]["available_piles"] == max(poi["available_piles"] for poi in all_chargers)

    keyword = mock_services.search_poi(keyword="服务区")
    assert keyword["total"] == 2 and all("服务区" in poi["name"] for poi in keyword["results"])
    assert mock_services.search_poi(category="加油站")["success"] is False


def test_plan_route_known_alias_ambiguous_and_unknown():
    route = mock_services.plan_route("浦东机场")
    assert route["success"] is True
    assert route["destination"] == "浦东国际机场（示例）"
    assert route["distance_km"] > 0 and route["duration_min"] > 0
    assert mock_services.plan_route("浦东国际机场（示例）") == route

    poi_route = mock_services.plan_route("徐汇滨江超充站（示例）")
    assert poi_route["success"] is True and poi_route["distance_km"] == 2.8

    ambiguous = mock_services.plan_route("火车站")
    assert ambiguous["success"] is False and ambiguous["need_confirmation"] is True
    assert 2 <= len(ambiguous["candidates"]) <= 3
    assert all(item["name"].endswith("（示例）") for item in ambiguous["candidates"])

    for place in ("家", "公司", "火星基地"):
        unknown = mock_services.plan_route(place)
        assert unknown["success"] is False and unknown["need_confirmation"] is True
        assert unknown["candidates"] == []


def test_weather_is_deterministic_per_city():
    shanghai = mock_services.get_weather("上海")
    assert shanghai == mock_services.get_weather("上海")
    assert shanghai == mock_services.get_weather()          # 默认当前城市
    assert shanghai["temp_low"] < shanghai["temp_high"]
    assert shanghai["condition"]
    beijing = mock_services.get_weather("北京")
    assert beijing == mock_services.get_weather("北京")
    assert mock_services.get_weather("北京", "明天") == mock_services.get_weather("北京", "明天")


def test_media_titles_are_sample_only():
    assert mock_services.search_media("周杰伦")["title"] == "周杰伦相关内容（示例曲库）"
    assert mock_services.search_media("")["title"].endswith("（示例曲库）")


def test_contacts_are_masked_and_searchable():
    assert all(re.fullmatch(r"1\d{2}\*{4}\d{4}", contact["phone"]) for contact in mock_services.CONTACTS)
    assert mock_services.find_contact("李娜")["contact"]["name"] == "李娜"
    assert mock_services.find_contact("老婆")["contact"]["name"] == "李娜"
    assert mock_services.find_contact("妈妈")["contact"]["name"] == "王芳"
    ambiguous = mock_services.find_contact("张")
    assert ambiguous["need_confirmation"] is True and len(ambiguous["candidates"]) == 2
    missing = mock_services.find_contact("赵六")
    assert missing["success"] is False and missing["not_found"] is True
    # 返回拷贝，修改不影响通讯录
    found = mock_services.find_contact("李娜")["contact"]
    found["phone"] = "13800001111"
    assert mock_services.find_contact("李娜")["contact"]["phone"] == "139****5678"
