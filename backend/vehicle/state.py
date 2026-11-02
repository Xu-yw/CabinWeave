"""
CabinWeave 车辆状态与进程内存储

状态用普通 dict 表示，便于 JSON 序列化和按角色裁剪快照。字段只保留工具会写、安全规则会查、
用车顾问会问、场景预设会设的部分：
  - 顶层字段：speed_kmh、gear（P/R/N/D）、drive_mode（eco/comfort/sport）、battery_pct、range_km、
    tire_pressure_kpa（四轮）、warning_lights、next_service_km、outside_temp_c
  - 嵌套分组：climate、seats、body、lights、location、navigation、media

存储：
  - VehicleStateStore 按 user_id 隔离，进程内 dict；一个用户一辆车，新对话不重置车辆
  - get 返回内部状态（供 VehicleService 修改），snapshot 返回深拷贝
  - reset 恢复成新车状态或套用场景预设；update 按嵌套 dict 局部更新，只允许已存在的键
  - 带一把可重入锁：车辆服务的"读-改-写"在锁内完成，同步接口跑在线程池里时也不会互相覆盖

场景预设（在默认状态上覆盖，SCENARIO_LABELS 为中文名）：
  parked 停车、city_driving 城市道路行驶、highway 高速行驶、low_battery 低电量、tpms_warning 胎压报警、winter 冬季低温。

状态比较：diff_states 逐个叶子字段比较两份状态，返回和工具一致的 [{"field", "old", "new"}]；
same_value 比较单个值时布尔与数值不混为一谈。

环境变量：
  CABINWEAVE_VEHICLE_DEFAULT_SCENARIO   新车使用的场景，默认 parked；非法值记 warning 后用 parked
"""
import copy
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SCENARIO = "parked"
DEFAULT_SCENARIO_ENV = "CABINWEAVE_VEHICLE_DEFAULT_SCENARIO"

# 音区：座椅加热分主驾、副驾；车窗分左前、右前、左后、右后
SEAT_ZONES = ("driver", "passenger")
WINDOW_ZONES = ("fl", "fr", "rl", "rr")
GEARS = ("P", "R", "N", "D")
DRIVE_MODES = ("eco", "comfort", "sport")

# 默认状态，即 parked 场景
DEFAULT_STATE: Dict[str, Any] = {
    "speed_kmh": 0,
    "gear": "P",
    "drive_mode": "comfort",
    "battery_pct": 80,
    "range_km": 400,
    "climate": {"on": False, "temp": 24, "fan_level": 3, "defrost": False},
    "seats": {"seat_heat": {"driver": 0, "passenger": 0}},
    "body": {
        "windows": {"fl": 0, "fr": 0, "rl": 0, "rr": 0},
        "sunroof": 0,
        "doors_locked": True,
        "trunk_open": False,
    },
    "lights": {"ambient": False, "hazard": False},
    "tire_pressure_kpa": {"fl": 250, "fr": 250, "rl": 250, "rr": 250},
    "warning_lights": [],
    "next_service_km": 5000,
    "outside_temp_c": 20,
    "location": {"city": "上海", "address": "徐汇区漕溪北路（示例）"},
    "navigation": {"active": False, "destination": None, "eta_min": None},
    "media": {"playing": False, "title": "", "volume": 12},
}

# 场景预设：在默认状态上按嵌套 dict 覆盖
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "parked": {},
    "city_driving": {"speed_kmh": 40, "gear": "D"},
    "highway": {"speed_kmh": 110, "gear": "D"},
    # 默认 80% 对应 400 km，按同样比例 12% 约 60 km
    "low_battery": {"battery_pct": 12, "range_km": 60},
    "tpms_warning": {
        "tire_pressure_kpa": {"fl": 180},
        "warning_lights": ["胎压报警"],
    },
    # 低温下续航按约 75% 折算
    "winter": {"outside_temp_c": -5, "range_km": 300},
}

# 场景预设的中文名，和 SCENARIOS 一一对应
SCENARIO_LABELS: Dict[str, str] = {
    "parked": "停车",
    "city_driving": "城市道路行驶",
    "highway": "高速行驶",
    "low_battery": "低电量",
    "tpms_warning": "胎压报警",
    "winter": "冬季低温",
}

# 允许为空的叶子字段：清空导航时写回 None
_NULLABLE_FIELDS = frozenset({"navigation.destination", "navigation.eta_min"})


def get_path(state: Dict[str, Any], path: str, default: Any = None) -> Any:
    """按点分路径读取字段，如 "climate.temp"；路径不存在时返回 default。"""
    node: Any = state
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def set_path(state: Dict[str, Any], path: str, value: Any) -> None:
    """按点分路径写入已存在的字段；路径不存在时抛 KeyError。"""
    keys = path.split(".")
    node: Any = state
    for key in keys[:-1]:
        node = node[key]
    if not isinstance(node, dict) or keys[-1] not in node:
        raise KeyError(path)
    node[keys[-1]] = value


def _is_number(value: Any) -> bool:
    """int / float 算数值，bool 不算。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def same_value(old: Any, new: Any) -> bool:
    """比较新旧值；布尔与数值不混为一谈（True == 1 在 Python 里成立）。"""
    if isinstance(old, bool) or isinstance(new, bool):
        return old is new
    return old == new


def diff_states(old: Dict[str, Any], new: Dict[str, Any], prefix: str = "") -> List[Dict[str, Any]]:
    """
    逐个叶子字段比较两份状态，返回 [{"field", "old", "new"}]，field 为点分路径（如 "climate.temp"）。

    按 new 的键顺序输出；嵌套分组逐层展开，列表（如 warning_lights）整体算一个叶子；值没变的字段不出现。
    """
    changes: List[Dict[str, Any]] = []
    for key, new_value in new.items():
        path = f"{prefix}{key}"
        old_value = old.get(key) if isinstance(old, dict) else None
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            changes.extend(diff_states(old_value, new_value, prefix=f"{path}."))
        elif not same_value(old_value, new_value):
            changes.append({"field": path, "old": copy.deepcopy(old_value), "new": copy.deepcopy(new_value)})
    return changes


def _check_leaf(path: str, old: Any, new: Any) -> None:
    """叶子字段的类型与取值校验；不合法时抛 ValueError。"""
    if path in _NULLABLE_FIELDS:
        if new is not None and not isinstance(new, str) and not _is_number(new):
            raise ValueError(f"{path} 只能是文本、数值或 null")
        return
    if isinstance(old, bool):
        ok = isinstance(new, bool)
    elif _is_number(old):
        ok = _is_number(new)
    elif isinstance(old, str):
        ok = isinstance(new, str)
    elif isinstance(old, list):
        ok = isinstance(new, list)
    else:
        ok = True
    if not ok:
        raise ValueError(f"{path} 的类型应与原值一致（{type(old).__name__}）")
    if path == "gear" and new not in GEARS:
        raise ValueError(f"gear 只能是 {'/'.join(GEARS)}")
    if path == "drive_mode" and new not in DRIVE_MODES:
        raise ValueError(f"drive_mode 只能是 {'/'.join(DRIVE_MODES)}")
    if path in ("speed_kmh", "range_km") and new < 0:
        raise ValueError(f"{path} 不能小于 0")
    if path == "battery_pct" and not 0 <= new <= 100:
        raise ValueError("battery_pct 必须在 0–100 之间")


def _merge(target: Dict[str, Any], patch: Dict[str, Any], prefix: str = "") -> None:
    """按嵌套 dict 把 patch 合并进 target：只允许已存在的键，分组只能按对象更新。"""
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if key not in target:
            raise ValueError(f"未知的车辆状态字段: {path}")
        current = target[key]
        if isinstance(current, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{path} 是分组，需要按对象更新")
            _merge(current, value, prefix=f"{path}.")
        elif isinstance(value, dict):
            raise ValueError(f"{path} 不是分组，不能按对象更新")
        else:
            _check_leaf(path, current, value)
            target[key] = copy.deepcopy(value)


def _normalize_scenario(scenario: Any) -> str:
    return str(scenario or "").strip().lower()


def build_state(scenario: str = DEFAULT_SCENARIO) -> Dict[str, Any]:
    """生成某个场景的完整状态（新的 dict）；未知场景抛 ValueError。"""
    name = _normalize_scenario(scenario)
    if name not in SCENARIOS:
        raise ValueError(f"未知的场景预设: {scenario}，可选 {', '.join(SCENARIOS)}")
    state = copy.deepcopy(DEFAULT_STATE)
    _merge(state, SCENARIOS[name])
    return state


def _scenario_from_env() -> str:
    """读取新车默认场景；非法值记 warning 后用 parked，不阻塞服务启动。"""
    raw = os.getenv(DEFAULT_SCENARIO_ENV, "")
    name = _normalize_scenario(raw)
    if not name:
        return DEFAULT_SCENARIO
    if name not in SCENARIOS:
        logger.warning("忽略非法场景配置 %s=%r，使用 %s", DEFAULT_SCENARIO_ENV, raw, DEFAULT_SCENARIO)
        return DEFAULT_SCENARIO
    return name


class VehicleStateStore:
    """
    进程内车辆状态存储，按 user_id 隔离（一个用户一辆车）。

    多实例部署时可换成 Redis 实现，保持 get / snapshot / reset / update 接口不变。
    """

    def __init__(self, default_scenario: Optional[str] = None):
        if default_scenario is None:
            self._default_scenario = _scenario_from_env()
        else:
            name = _normalize_scenario(default_scenario)
            if name not in SCENARIOS:
                raise ValueError(f"未知的场景预设: {default_scenario}")
            self._default_scenario = name
        self._states: Dict[str, Dict[str, Any]] = {}
        # 车辆服务在锁内完成"读-改-写"，可重入以便服务内部再调 get
        self.lock = threading.RLock()

    @property
    def default_scenario(self) -> str:
        """新车和不带场景的 reset 使用的场景。"""
        return self._default_scenario

    @staticmethod
    def _check_user(user_id: str) -> str:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id 不能为空")
        return user_id

    def get(self, user_id: str) -> Dict[str, Any]:
        """返回内部状态（供车辆服务修改）；不存在时按默认场景创建。"""
        user_id = self._check_user(user_id)
        with self.lock:
            state = self._states.get(user_id)
            if state is None:
                state = build_state(self._default_scenario)
                self._states[user_id] = state
            return state

    def snapshot(self, user_id: str) -> Dict[str, Any]:
        """返回深拷贝，调用方修改不影响存储。"""
        with self.lock:
            return copy.deepcopy(self.get(user_id))

    def reset(self, user_id: str, scenario: Optional[str] = None) -> Dict[str, Any]:
        """恢复成新车状态（默认场景），或套用指定场景；未知场景抛 ValueError。返回新状态的快照。"""
        new_state = build_state(self._default_scenario if scenario is None else scenario)
        with self.lock:
            state = self.get(user_id)
            state.clear()
            state.update(new_state)
            return copy.deepcopy(state)

    def update(self, user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        按嵌套 dict 局部更新，用于模拟车速、电量、故障灯等。

        只允许已存在的键，未知键、类型不符或取值非法时抛 ValueError，且整次更新不生效。
        返回更新后的快照。
        """
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是对象")
        with self.lock:
            state = self.get(user_id)
            updated = copy.deepcopy(state)
            _merge(updated, patch)
            state.clear()
            state.update(updated)
            return copy.deepcopy(state)
