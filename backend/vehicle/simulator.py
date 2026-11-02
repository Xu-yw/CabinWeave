"""
CabinWeave 车辆模拟控制（供 /vehicle/* 接口和意图评测使用）

直接读写进程内模拟存储（VehicleStateStore），不经过安全规则：用来模拟车速、电量、故障灯，
套用场景预设，或在评测前把车辆重置到指定场景。驾驶员的车控指令仍然只走 VehicleService.execute。
函数都是同步纯 Python，API 路由只负责把 ValueError 转成 400。

一个用户一辆车，车辆状态按 user_id 存取。

操作（返回值都可以直接作为接口响应）：
  read_state(store, uid)                 {"state"}
  patch_state(store, uid, patch)         {"state", "changes"}；按嵌套 dict 局部更新
  apply_scenario(store, uid, scenario)   {"scenario", "state", "changes"}；整车重置为该场景
  reset_vehicle(store, uid)              同上，场景为存储的默认场景（CABINWEAVE_VEHICLE_DEFAULT_SCENARIO）
  list_scenarios(store)                  {"default_scenario", "scenarios": [{"name", "label", "overrides"}]}
  changes 与工具返回的格式一致：[{"field", "old", "new"}]，field 为叶子字段的点分路径。
  未知键、类型不符、取值非法、未知场景都抛 ValueError（中文原因），且状态不变。
"""
import copy
from typing import Any, Dict

from vehicle.state import (
    SCENARIO_LABELS,
    SCENARIOS,
    VehicleStateStore,
    diff_states,
)

# ── 状态操作 ──────────────────────────────────────────────────────────────────

def read_state(store: VehicleStateStore, user_id: str) -> Dict[str, Any]:
    """查看车辆状态；车辆不存在时按默认场景创建。"""
    return {"state": store.snapshot(user_id)}


def patch_state(store: VehicleStateStore, user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """按嵌套 dict 局部修改状态（不经过安全规则），返回修改后的状态和叶子字段变化。"""
    with store.lock:
        before = store.snapshot(user_id)
        after = store.update(user_id, patch)
    return {"state": after, "changes": diff_states(before, after)}


def apply_scenario(store: VehicleStateStore, user_id: str, scenario: str) -> Dict[str, Any]:
    """整车重置为某个场景预设；未知场景抛 ValueError 且状态不变。"""
    if not isinstance(scenario, str) or not scenario.strip():
        raise ValueError(f"场景不能为空，可选 {', '.join(SCENARIOS)}")
    return _reset(store, user_id, scenario.strip().lower())


def reset_vehicle(store: VehicleStateStore, user_id: str) -> Dict[str, Any]:
    """重置为默认场景。"""
    return _reset(store, user_id, store.default_scenario)


def _reset(store: VehicleStateStore, user_id: str, scenario: str) -> Dict[str, Any]:
    with store.lock:
        before = store.snapshot(user_id)
        after = store.reset(user_id, scenario)
    return {
        "scenario": scenario,
        "state": after,
        "changes": diff_states(before, after),
    }


def list_scenarios(store: VehicleStateStore) -> Dict[str, Any]:
    """可选的场景预设（名字、中文名、在默认状态上覆盖的字段）和默认场景。"""
    return {
        "default_scenario": store.default_scenario,
        "scenarios": [
            {"name": name, "label": SCENARIO_LABELS.get(name, name), "overrides": copy.deepcopy(overrides)}
            for name, overrides in SCENARIOS.items()
        ],
    }

