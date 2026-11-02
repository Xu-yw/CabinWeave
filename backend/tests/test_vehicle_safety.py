"""
CabinWeave 车辆安全规则测试

覆盖 S01–S07 每条规则的拒绝、调整和放行；被拒绝后状态不变；相对调节先按当前状态换算再裁剪；
多条规则同时触发时 notice 都在、rule_ids 全部列出。既直接测 SafetyPolicy.check，也经 VehicleService 端到端测。

全部是纯 Python，不访问网络、不调用模型。
"""
import pytest

from vehicle.safety import SafetyDecision, SafetyPolicy
from vehicle.service import VehicleService, make_command
from vehicle.state import VehicleStateStore, build_state

VID = "test"


@pytest.fixture
def service(monkeypatch):
    monkeypatch.delenv("CABINWEAVE_VEHICLE_DEFAULT_SCENARIO", raising=False)
    return VehicleService(VehicleStateStore())


def use(service, scenario=None, **patch):
    """套用场景并按需局部修改，返回当前快照。"""
    service.store.reset(VID, scenario)
    if patch:
        service.store.update(VID, patch)
    return service.snapshot(VID)


def run(service, action, **args):
    return service.execute(VID, make_command(action, **args))


def assert_blocked_unchanged(service, action, rule_id, reason, **args):
    """被拒绝：返回 blocked 和原因，状态完全不变。"""
    before = service.snapshot(VID)
    result = run(service, action, **args)
    assert result == {"success": False, "blocked": True, "rule_id": rule_id, "reason": reason}
    assert service.snapshot(VID) == before


def check(state, action, **args) -> SafetyDecision:
    return SafetyPolicy().check(state, make_command(action, **args))


# ── S01 行驶中禁开尾门 ────────────────────────────────────────────────────────

def test_s01_blocks_trunk_while_moving(service):
    use(service, "city_driving")
    assert_blocked_unchanged(service, "set_trunk", "S01", "车辆行驶中不能打开后备箱，请停车后再试。", open=True)


def test_s01_blocks_trunk_when_not_in_park(service):
    use(service, gear="D")
    assert_blocked_unchanged(service, "set_trunk", "S01", "当前不在P档，不能打开后备箱，请挂P档后再试。", open=True)


def test_s01_allows_trunk_when_parked_and_closing_while_moving(service):
    use(service)
    assert run(service, "set_trunk", open=True)["changes"] == [{"field": "body.trunk_open", "old": False, "new": True}]
    service.store.update(VID, {"speed_kmh": 30, "gear": "D"})
    result = run(service, "set_trunk", open=False)
    assert result["success"] is True and result["notice"] == ""
    assert service.snapshot(VID)["body"]["trunk_open"] is False


# ── S02 行驶中禁解锁车门 ──────────────────────────────────────────────────────

def test_s02_blocks_unlock_while_moving(service):
    use(service, "highway")
    assert_blocked_unchanged(service, "set_door_lock", "S02", "车辆行驶中不能解锁车门，请停车后再试。", locked=False)


def test_s02_allows_unlock_when_stopped_and_lock_while_moving(service):
    use(service, gear="D")      # 车速为 0，档位不限
    assert run(service, "set_door_lock", locked=False)["changes"] == [
        {"field": "body.doors_locked", "old": True, "new": False}
    ]
    service.store.update(VID, {"speed_kmh": 20})
    assert run(service, "set_door_lock", locked=True)["changes"] == [
        {"field": "body.doors_locked", "old": False, "new": True}
    ]


# ── S03 高速限制开度 ──────────────────────────────────────────────────────────

def test_s03_limits_window_and_sunroof_on_highway(service):
    use(service, "highway")
    result = run(service, "set_window", zone="all", open_percent=80)
    assert result["success"] is True
    assert result["rule_ids"] == ["S03"]
    assert result["notice"] == "高速行驶中车窗最多只能开到30%，已为你开到30%。"
    assert service.snapshot(VID)["body"]["windows"] == {"fl": 30, "fr": 30, "rl": 30, "rr": 30}

    result = run(service, "set_sunroof", open_percent=100)
    assert result["notice"] == "高速行驶中天窗最多只能开到30%，已为你开到30%。"
    assert service.snapshot(VID)["body"]["sunroof"] == 30


def test_s03_allows_small_opening_on_highway_and_any_opening_in_city(service):
    use(service, "highway")
    result = run(service, "set_window", zone="fl", open_percent=20)
    assert result["notice"] == "" and result["rule_ids"] == []
    assert service.snapshot(VID)["body"]["windows"]["fl"] == 20

    use(service, speed_kmh=100, gear="D")       # 恰好 100 不触发
    assert run(service, "set_window", open_percent=80)["notice"] == ""
    assert service.snapshot(VID)["body"]["windows"]["fl"] == 80


# ── S04 温度范围 ──────────────────────────────────────────────────────────────

def test_s04_clips_temperature(service):
    use(service)
    result = run(service, "set_temperature", temp=35)
    assert result["notice"] == "温度最高只能调到32度，已为你调到32度。"
    assert result["rule_ids"] == ["S04"]
    assert service.snapshot(VID)["climate"]["temp"] == 32

    result = run(service, "set_temperature", temp=10)
    assert result["notice"] == "温度最低只能调到16度，已为你调到16度。"
    assert service.snapshot(VID)["climate"]["temp"] == 16

    result = run(service, "set_temperature", temp=22)
    assert result["notice"] == "" and result["rule_ids"] == []


def test_s04_relative_adjustment_resolves_before_clipping(service):
    use(service, climate={"temp": 30})
    result = run(service, "set_temperature", delta=5)      # 30 + 5 = 35 → 32
    assert result["changes"][-1] == {"field": "climate.temp", "old": 30, "new": 32}
    assert result["notice"] == "温度最高只能调到32度，已为你调到32度。"

    result = run(service, "set_temperature", delta=1)      # 已在 32，再调高
    assert result["success"] is True and result["changes"] == []
    assert result["notice"] == "温度已经是最高的32度了。"

    decision = check(build_state(), "set_temperature", delta=-2)
    assert decision.allowed is True
    assert decision.adjusted_args == {"temp": 22}          # 换算后不再带 delta


# ── S05 风量、座椅加热、音量范围 ──────────────────────────────────────────────

def test_s05_clips_fan_level(service):
    use(service)
    result = run(service, "set_fan", level=9)
    assert result["notice"] == "风量最高只能调到7档，已为你调到7档。"
    assert result["rule_ids"] == ["S05"]
    assert service.snapshot(VID)["climate"]["fan_level"] == 7

    result = run(service, "set_fan", delta=-10)            # 7 - 10 → 1
    assert result["notice"] == "风量最低只能调到1档，已为你调到1档。"
    assert service.snapshot(VID)["climate"]["fan_level"] == 1


def test_s05_clips_seat_heat_per_zone(service):
    use(service, seats={"seat_heat": {"driver": 2, "passenger": 0}})
    result = run(service, "set_seat_heat", zone="driver", level=5)
    assert result["notice"] == "主驾座椅加热最高只能调到3档，已为你调到3档。"
    assert service.snapshot(VID)["seats"]["seat_heat"] == {"driver": 3, "passenger": 0}

    # 两个音区按各自当前值换算：主驾 3+2→3（裁剪），副驾 0+2=2（不裁剪）
    result = run(service, "set_seat_heat", zone="all", delta=2)
    assert service.snapshot(VID)["seats"]["seat_heat"] == {"driver": 3, "passenger": 2}
    assert result["notice"] == "主驾座椅加热已经是最高的3档了。"

    result = run(service, "set_seat_heat", zone="all", level=-1)
    assert service.snapshot(VID)["seats"]["seat_heat"] == {"driver": 0, "passenger": 0}
    assert result["notice"] == "座椅加热最低就是关闭，已为你关闭。"


def test_s05_clips_volume(service):
    use(service)
    result = run(service, "set_volume", level=40)
    assert result["notice"] == "音量最高只能调到30，已为你调到30。"
    assert service.snapshot(VID)["media"]["volume"] == 30

    result = run(service, "set_volume", delta=-50)
    assert result["notice"] == "音量最低只能调到0，已为你调成静音。"
    assert service.snapshot(VID)["media"]["volume"] == 0

    result = run(service, "set_volume", level=15)
    assert result["notice"] == "" and service.snapshot(VID)["media"]["volume"] == 15


# ── S06 主动安全功能 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("feature, name", [("esp", "车身稳定系统"), ("aeb", "自动紧急制动"), ("车道保持", "车道保持")])
def test_s06_never_disables_active_safety(service, feature, name):
    use(service)        # 停车时也一律拒绝
    reason = f"为了行车安全，{name}不能语音关闭，如需调整请到车辆设置中手动操作。"
    assert_blocked_unchanged(service, "set_active_safety", "S06", reason, feature=feature, on=False)


def test_s06_enabling_active_safety_changes_nothing(service):
    use(service)
    result = run(service, "set_active_safety", feature="esp", on=True)
    assert result == {"success": True, "changes": [], "notice": "", "rule_ids": []}


# ── S07 低电量提醒 ────────────────────────────────────────────────────────────

def test_s07_reminds_when_turning_on_seat_heat_with_low_battery(service):
    use(service, "low_battery")
    result = run(service, "set_seat_heat", zone="driver", level=2)
    assert result["success"] is True
    assert result["changes"] == [{"field": "seats.seat_heat.driver", "old": 0, "new": 2}]
    assert result["notice"] == "当前电量只剩12%，座椅加热会增加耗电，建议尽快充电。"
    assert result["rule_ids"] == ["S07"]

    # 调低不提醒
    result = run(service, "set_seat_heat", zone="driver", delta=-1)
    assert result["notice"] == "" and result["rule_ids"] == []


def test_s07_reminds_only_when_raising_fan(service):
    use(service, "low_battery")
    result = run(service, "set_fan", delta=1)
    assert result["notice"] == "当前电量只剩12%，调高风量会增加耗电，建议尽快充电。"
    assert service.snapshot(VID)["climate"]["fan_level"] == 4
    assert run(service, "set_fan", level=2)["notice"] == ""
    # 温度、音量不在 S07 范围内
    assert run(service, "set_temperature", temp=26)["notice"] == ""


def test_s07_not_triggered_with_enough_battery(service):
    use(service, battery_pct=15)
    assert run(service, "set_seat_heat", zone="all", level=3)["notice"] == ""
    assert run(service, "set_fan", level=7)["notice"] == ""


# ── 多条规则同时触发 ──────────────────────────────────────────────────────────

def test_multiple_rules_notices_all_present(service):
    use(service, "low_battery")
    result = run(service, "set_seat_heat", zone="all", level=5)
    assert result["success"] is True
    assert result["rule_ids"] == ["S05", "S07"]
    assert result["notice"] == (
        "座椅加热最高只能调到3档，已为你调到3档。"
        "当前电量只剩12%，座椅加热会增加耗电，建议尽快充电。"
    )
    assert service.snapshot(VID)["seats"]["seat_heat"] == {"driver": 3, "passenger": 3}

    result = run(service, "set_fan", delta=10)              # 3 + 10 → 7，同时是调高
    assert result["rule_ids"] == ["S05", "S07"]
    assert "风量最高只能调到7档" in result["notice"] and "调高风量会增加耗电" in result["notice"]


def test_check_returns_decision_fields():
    state = build_state("low_battery")
    decision = check(state, "set_fan", level=9)
    assert isinstance(decision, SafetyDecision)
    assert decision.allowed is True
    assert decision.adjusted_args == {"level": 7}
    assert decision.rule_id == "S05"
    assert decision.rule_ids == ["S05", "S07"]

    blocked = check(build_state("highway"), "set_trunk", open=True)
    assert (blocked.allowed, blocked.rule_id, blocked.rule_ids) == (False, "S01", ["S01"])
    assert blocked.notice == "车辆行驶中不能打开后备箱，请停车后再试。"

    plain = check(build_state(), "set_hazard", on=True)
    assert (plain.allowed, plain.rule_id, plain.rule_ids, plain.notice) == (True, None, [], "")
    assert plain.adjusted_args == {"on": True}
    # check 只做判断，不修改传入的状态
    assert state == build_state("low_battery")
