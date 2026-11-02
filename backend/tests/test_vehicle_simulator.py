"""
CabinWeave 车辆模拟控制测试

覆盖：局部修改状态返回叶子字段变化，未知键或非法值报错且状态不变；套用场景、未知场景；重置为默认场景；场景列表；
diff_states 的叶子比较。

全部是纯 Python，不依赖 FastAPI，不访问网络、不调用模型。
"""
import pytest

from vehicle.simulator import (
    apply_scenario,
    list_scenarios,
    patch_state,
    read_state,
    reset_vehicle,
)
from vehicle.state import SCENARIO_LABELS, SCENARIOS, VehicleStateStore, build_state, diff_states

VID = "u1001"


@pytest.fixture
def store():
    return VehicleStateStore(default_scenario="parked")


# ── 查看与局部修改 ────────────────────────────────────────────────────────────

def test_read_state_creates_default(store):
    result = read_state(store, VID)
    assert result == {"state": build_state("parked")}
    # 返回的是快照，改它不影响存储
    result["state"]["speed_kmh"] = 99
    assert store.snapshot(VID)["speed_kmh"] == 0


def test_patch_state_returns_leaf_changes(store):
    result = patch_state(store, VID, {
        "speed_kmh": 60,
        "climate": {"temp": 24, "on": True},        # temp 与原值相同，不进 changes
        "tire_pressure_kpa": {"fl": 180},
        "warning_lights": ["胎压报警"],
    })
    assert result["changes"] == [
        {"field": "speed_kmh", "old": 0, "new": 60},
        {"field": "climate.on", "old": False, "new": True},
        {"field": "tire_pressure_kpa.fl", "old": 250, "new": 180},
        {"field": "warning_lights", "old": [], "new": ["胎压报警"]},
    ]
    assert result["state"] == store.snapshot(VID)
    assert result["state"]["tire_pressure_kpa"] == {"fl": 180, "fr": 250, "rl": 250, "rr": 250}


def test_patch_state_empty_patch(store):
    result = patch_state(store, VID, {})
    assert result["changes"] == []
    assert result["state"] == build_state("parked")


@pytest.mark.parametrize("patch, reason", [
    ({"foo": 1}, "未知的车辆状态字段: foo"),
    ({"climate": {"bar": 1}}, "未知的车辆状态字段: climate.bar"),
    ({"gear": "X"}, "gear 只能是"),
    ({"battery_pct": 101}, "battery_pct 必须在 0–100 之间"),
    ({"speed_kmh": -1}, "speed_kmh 不能小于 0"),
    ({"speed_kmh": "fast"}, "类型应与原值一致"),
    ({"climate": 1}, "是分组"),
    ({"speed_kmh": 60, "drive_mode": "turbo"}, "drive_mode 只能是"),
])
def test_patch_state_rejects_and_keeps_state(store, patch, reason):
    patch_state(store, VID, {"battery_pct": 50})
    before = store.snapshot(VID)
    with pytest.raises(ValueError, match=reason):
        patch_state(store, VID, patch)
    assert store.snapshot(VID) == before


def test_patch_state_isolated_by_vehicle(store):
    patch_state(store, "a", {"speed_kmh": 30})
    assert store.snapshot("b")["speed_kmh"] == 0


# ── 场景与重置 ────────────────────────────────────────────────────────────────

def test_apply_scenario(store):
    result = apply_scenario(store, VID, " Highway ")
    assert result["scenario"] == "highway"
    assert result["state"] == build_state("highway") == store.snapshot(VID)
    assert result["changes"] == [
        {"field": "speed_kmh", "old": 0, "new": 110},
        {"field": "gear", "old": "P", "new": "D"},
    ]


def test_apply_scenario_replaces_whole_state(store):
    patch_state(store, VID, {"climate": {"on": True}, "battery_pct": 30})
    result = apply_scenario(store, VID, "low_battery")
    assert result["state"] == build_state("low_battery")
    fields = {change["field"] for change in result["changes"]}
    assert {"climate.on", "battery_pct", "range_km"} <= fields


@pytest.mark.parametrize("scenario", ["moon", "", "   ", None])
def test_apply_scenario_rejects_unknown(store, scenario):
    patch_state(store, VID, {"speed_kmh": 20})
    before = store.snapshot(VID)
    with pytest.raises(ValueError):
        apply_scenario(store, VID, scenario)
    assert store.snapshot(VID) == before


def test_reset_vehicle_uses_default_scenario(store):
    patch_state(store, VID, {"speed_kmh": 80, "gear": "D"})
    result = reset_vehicle(store, VID)
    assert result["scenario"] == "parked"
    assert result["state"] == build_state("parked") == store.snapshot(VID)
    assert result["changes"] == [
        {"field": "speed_kmh", "old": 80, "new": 0},
        {"field": "gear", "old": "D", "new": "P"},
    ]


def test_reset_vehicle_with_configured_default():
    winter = VehicleStateStore(default_scenario="winter")
    apply_scenario(winter, VID, "highway")
    result = reset_vehicle(winter, VID)
    assert result["scenario"] == "winter"
    assert result["state"] == build_state("winter")


def test_list_scenarios(store):
    result = list_scenarios(store)
    assert result["default_scenario"] == "parked"
    assert [item["name"] for item in result["scenarios"]] == list(SCENARIOS)
    for item in result["scenarios"]:
        assert item["label"] == SCENARIO_LABELS[item["name"]]
        assert item["overrides"] == SCENARIOS[item["name"]]
    # 返回的是副本
    result["scenarios"][-1]["overrides"]["range_km"] = 1
    assert SCENARIOS["winter"]["range_km"] == 300


def test_scenario_labels_cover_all_scenarios():
    assert set(SCENARIO_LABELS) == set(SCENARIOS)
    assert all(label.strip() for label in SCENARIO_LABELS.values())


# ── diff_states ───────────────────────────────────────────────────────────────

def test_diff_states_leaf_paths():
    old = {"a": 1, "b": {"c": True, "d": [1]}, "e": None}
    new = {"a": 1, "b": {"c": 1, "d": [1, 2]}, "e": "x"}
    assert diff_states(old, new) == [
        {"field": "b.c", "old": True, "new": 1},          # 布尔与数值不混为一谈
        {"field": "b.d", "old": [1], "new": [1, 2]},      # 列表整体算一个叶子
        {"field": "e", "old": None, "new": "x"},
    ]
    assert diff_states(new, new) == []

