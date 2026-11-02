"""
CabinWeave 车辆安全规则

所有写操作在执行前统一调用 SafetyPolicy.check(state, command)：
  - command 结构与 VehicleService 相同：{"action": "...", "args": {...}}，参数已通过服务层校验
  - 相对调节先换算：按当前状态把 delta 换算成目标值，再过范围规则，裁剪基于换算后的值
  - 返回 SafetyDecision：allowed、adjusted_args（换算、调整后的绝对参数）、notice（可直接播报）、
    rule_id（第一条触发的规则，拒绝时即拒绝的那条）、rule_ids（按顺序列出全部触发的规则）

规则：
  S01 行驶中禁开尾门：打开尾门，且车速 > 0 或档位不在 P → 拒绝
  S02 行驶中禁解锁车门：解锁，且车速 > 0 → 拒绝
  S03 高速限制开度：车速 > 100，车窗或天窗开度 > 30% → 调整为 30% 并说明
  S04 温度范围：16–32℃ 之外 → 裁剪到边界并说明
  S05 档位、音量范围：风量 1–7、座椅加热 0–3、音量 0–30 之外 → 裁剪并说明
  S06 主动安全功能：关闭 ESP、自动紧急制动等 → 一律拒绝，提示到车辆设置中手动操作
  S07 低电量提醒：电量 < 15% 时开启/调高座椅加热或调高风量 → 允许，附带提醒

同时触发多条规则时（如 S05 裁剪 + S07 提醒），notice 按规则顺序拼接，rule_ids 全部列出。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from vehicle.state import SEAT_ZONES, get_path

TEMP_RANGE = (16, 32)
FAN_RANGE = (1, 7)
SEAT_HEAT_RANGE = (0, 3)
VOLUME_RANGE = (0, 30)
HIGHWAY_SPEED_KMH = 100
HIGHWAY_OPEN_LIMIT = 30
LOW_BATTERY_PCT = 15

# 主动安全功能代码 → 播报名称；不在表里的按原文播报
ACTIVE_SAFETY_FEATURES: Dict[str, str] = {
    "esp": "车身稳定系统",
    "aeb": "自动紧急制动",
    "fcw": "前方碰撞预警",
    "lka": "车道保持辅助",
    "bsd": "盲区监测",
}

_SEAT_LABELS = {"driver": "主驾座椅加热", "passenger": "副驾座椅加热"}


@dataclass
class SafetyDecision:
    """一次安全校验的结论。"""
    allowed: bool
    adjusted_args: Dict[str, Any] = field(default_factory=dict)
    notice: str = ""                     # 放行时为调整说明或提醒，拒绝时为拒绝原因
    rule_id: Optional[str] = None        # 第一条触发的规则
    rule_ids: List[str] = field(default_factory=list)


def normalize_number(value: Any) -> Any:
    """数值保留一位小数，整数值转成 int，便于比较和播报。"""
    value = round(float(value), 1)
    return int(value) if value.is_integer() else value


def _fmt(value: Any) -> str:
    return str(normalize_number(value))


def feature_name(feature: Any) -> str:
    """主动安全功能的播报名称。"""
    text = str(feature or "").strip()
    return ACTIVE_SAFETY_FEATURES.get(text.lower(), text or "主动安全功能")


def _range_notice(label: str, unit: str, bound: Any, upper: bool, unchanged: bool) -> str:
    """裁剪说明：裁剪后与当前值相同时说"已经是最高/最低"，否则说"已为你调到"。"""
    edge = "最高" if upper else "最低"
    if unchanged:
        return f"{label}已经是{edge}的{_fmt(bound)}{unit}了。"
    return f"{label}{edge}只能调到{_fmt(bound)}{unit}，已为你调到{_fmt(bound)}{unit}。"


def _clip(value: Any, bounds: Tuple[Any, Any]) -> Tuple[Any, Optional[bool]]:
    """裁剪到区间内；第二个返回值 True 表示碰到上界，False 表示下界，None 表示未裁剪。"""
    low, high = bounds
    if value > high:
        return high, True
    if value < low:
        return low, False
    return value, None


class SafetyPolicy:
    """车辆写操作的安全规则层：LLM 负责理解和规划，能不能执行由这里决定。"""

    def check(self, state: Dict[str, Any], command: Dict[str, Any]) -> SafetyDecision:
        action = str(command.get("action") or "")
        args = self._resolve(state, action, dict(command.get("args") or {}))

        rejection = self._reject(state, action, args)
        if rejection is not None:
            rule_id, reason = rejection
            return SafetyDecision(False, args, reason, rule_id, [rule_id])

        notices: List[Tuple[str, str]] = []
        self._limit_opening(state, action, args, notices)
        self._clip_ranges(state, action, args, notices)
        self._low_battery(state, action, args, notices)
        rule_ids = list(dict.fromkeys(rule for rule, _ in notices))
        notice = "".join(dict.fromkeys(text for _, text in notices))
        return SafetyDecision(True, args, notice, rule_ids[0] if rule_ids else None, rule_ids)

    # ── 相对调节换算 ──────────────────────────────────────────────────────────

    @staticmethod
    def _target(args: Dict[str, Any], key: str, current: Any) -> Any:
        """有绝对值用绝对值，否则按 delta 在当前值上换算。"""
        if key in args:
            return normalize_number(args[key])
        if "delta" in args:
            return normalize_number(current + args["delta"])
        raise ValueError(f"缺少参数 {key} 或 delta")

    def _resolve(self, state: Dict[str, Any], action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """把温度、风量、音量、座椅加热的 delta 换算成绝对值；换算后的参数不再带 delta。"""
        if action == "set_temperature":
            args["temp"] = self._target(args, "temp", get_path(state, "climate.temp", 24))
        elif action == "set_fan":
            args["level"] = self._target(args, "level", get_path(state, "climate.fan_level", 1))
        elif action == "set_volume":
            args["level"] = self._target(args, "level", get_path(state, "media.volume", 0))
        elif action == "set_seat_heat" and "levels" not in args:
            zone = args.get("zone", "driver")
            zones = SEAT_ZONES if zone == "all" else (zone,)
            args["levels"] = {
                name: self._target(args, "level", get_path(state, f"seats.seat_heat.{name}", 0))
                for name in zones
            }
            args.pop("level", None)
        else:
            return args
        args.pop("delta", None)
        return args

    # ── 拒绝类规则：S01、S02、S06 ─────────────────────────────────────────────

    @staticmethod
    def _reject(state: Dict[str, Any], action: str, args: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        speed = get_path(state, "speed_kmh", 0) or 0
        if action == "set_trunk" and args.get("open") is True:
            if speed > 0:
                return "S01", "车辆行驶中不能打开后备箱，请停车后再试。"
            if get_path(state, "gear", "P") != "P":
                return "S01", "当前不在P档，不能打开后备箱，请挂P档后再试。"
        if action == "set_door_lock" and args.get("locked") is False and speed > 0:
            return "S02", "车辆行驶中不能解锁车门，请停车后再试。"
        if action == "set_active_safety" and args.get("on") is False:
            name = feature_name(args.get("feature"))
            return "S06", f"为了行车安全，{name}不能语音关闭，如需调整请到车辆设置中手动操作。"
        return None

    # ── 调整类规则：S03、S04、S05 ─────────────────────────────────────────────

    @staticmethod
    def _limit_opening(
        state: Dict[str, Any], action: str, args: Dict[str, Any], notices: List[Tuple[str, str]],
    ) -> None:
        """S03：高速行驶时车窗、天窗最多开到 30%。"""
        if action not in ("set_window", "set_sunroof"):
            return
        if (get_path(state, "speed_kmh", 0) or 0) <= HIGHWAY_SPEED_KMH:
            return
        if args.get("open_percent", 0) <= HIGHWAY_OPEN_LIMIT:
            return
        args["open_percent"] = HIGHWAY_OPEN_LIMIT
        part = "车窗" if action == "set_window" else "天窗"
        notices.append((
            "S03",
            f"高速行驶中{part}最多只能开到{HIGHWAY_OPEN_LIMIT}%，已为你开到{HIGHWAY_OPEN_LIMIT}%。",
        ))

    def _clip_ranges(
        self, state: Dict[str, Any], action: str, args: Dict[str, Any], notices: List[Tuple[str, str]],
    ) -> None:
        """S04 温度、S05 风量 / 座椅加热 / 音量：超出范围裁剪到边界并说明。"""
        if action == "set_temperature":
            self._clip_single(args, "temp", TEMP_RANGE, get_path(state, "climate.temp"), "S04", "温度", "度", notices)
        elif action == "set_fan":
            self._clip_single(args, "level", FAN_RANGE, get_path(state, "climate.fan_level"), "S05", "风量", "档", notices)
        elif action == "set_volume":
            self._clip_volume(args, get_path(state, "media.volume"), notices)
        elif action == "set_seat_heat":
            self._clip_seat_heat(state, args, notices)

    @staticmethod
    def _clip_single(
        args: Dict[str, Any], key: str, bounds: Tuple[Any, Any], current: Any,
        rule_id: str, label: str, unit: str, notices: List[Tuple[str, str]],
    ) -> None:
        value, upper = _clip(args[key], bounds)
        if upper is None:
            return
        args[key] = value
        notices.append((rule_id, _range_notice(label, unit, value, upper, value == current)))

    @staticmethod
    def _clip_volume(args: Dict[str, Any], current: Any, notices: List[Tuple[str, str]]) -> None:
        value, upper = _clip(args["level"], VOLUME_RANGE)
        if upper is None:
            return
        args["level"] = value
        if upper:
            text = _range_notice("音量", "", value, True, value == current)
        elif value == current:
            text = "音量已经是静音了。"
        else:
            text = "音量最低只能调到0，已为你调成静音。"
        notices.append(("S05", text))

    @staticmethod
    def _clip_seat_heat(state: Dict[str, Any], args: Dict[str, Any], notices: List[Tuple[str, str]]) -> None:
        levels: Dict[str, Any] = args["levels"]
        events: List[Tuple[str, bool, bool]] = []   # (音区, 是否上界, 裁剪后是否与当前相同)
        for zone, level in list(levels.items()):
            value, upper = _clip(level, SEAT_HEAT_RANGE)
            if upper is None:
                continue
            levels[zone] = value
            events.append((zone, upper, value == get_path(state, f"seats.seat_heat.{zone}")))
        if not events:
            return
        # 两个音区裁剪情况相同时合并成一句"座椅加热……"
        if len(events) == len(levels) > 1 and len({event[1:] for event in events}) == 1:
            events = [("all", events[0][1], events[0][2])]
        for zone, upper, unchanged in events:
            label = _SEAT_LABELS.get(zone, "座椅加热")
            if upper:
                text = _range_notice(label, "档", SEAT_HEAT_RANGE[1], True, unchanged)
            elif unchanged:
                text = f"{label}已经是关闭状态。"
            else:
                text = f"{label}最低就是关闭，已为你关闭。"
            notices.append(("S05", text))

    # ── 提醒类规则：S07 ───────────────────────────────────────────────────────

    @staticmethod
    def _low_battery(
        state: Dict[str, Any], action: str, args: Dict[str, Any], notices: List[Tuple[str, str]],
    ) -> None:
        """S07：电量低于 15% 时开启/调高座椅加热或调高风量，放行并提醒。"""
        battery = get_path(state, "battery_pct", 100)
        if battery is None or battery >= LOW_BATTERY_PCT:
            return
        if action == "set_seat_heat":
            raised = any(
                level > (get_path(state, f"seats.seat_heat.{zone}", 0) or 0)
                for zone, level in args["levels"].items()
            )
            what = "座椅加热"
        elif action == "set_fan":
            raised = args["level"] > (get_path(state, "climate.fan_level", 0) or 0)
            what = "调高风量"
        else:
            return
        if raised:
            notices.append(("S07", f"当前电量只剩{_fmt(battery)}%，{what}会增加耗电，建议尽快充电。"))
