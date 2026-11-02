"""
CabinWeave 车辆服务接口（模拟车辆的抽象边界）

现在接进程内的模拟存储；将来接真实车控服务时只替换这一层。agents/tools.py 里的车控、媒体、导航工具
都是 VehicleService.execute 的薄封装。

命令结构：{"action": "<动作名>", "args": {...}}，可用 make_command(action, **args) 构造。

命令词表（括号内为取值；"二选一"表示两个参数必须且只能给一个）：
  set_climate_power   on（bool）                                      空调开关
  set_temperature     temp（数值）或 delta（数值），二选一              温度；设温度时同时打开空调
  set_fan             level（整数）或 delta（整数），二选一              风量；调风量时同时打开空调
  set_defrost         on（bool）                                      前挡除雾
  set_seat_heat       zone（driver/passenger/all，默认 driver），
                      level（整数）或 delta（整数），二选一              座椅加热
  set_window          zone（fl/fr/rl/rr/all，默认 fl），open_percent（0–100 整数）  车窗开度
  set_sunroof         open_percent（0–100 整数）                      天窗开度
  set_trunk           open（bool）                                    尾门开关
  set_door_lock       locked（bool）                                  车门上锁 / 解锁
  set_ambient_light   on（bool）                                      氛围灯
  set_hazard          on（bool）                                      双闪
  set_drive_mode      mode（eco/comfort/sport）                       驾驶模式
  set_volume          level（整数）或 delta（整数），二选一              音量
  media_control       op（play/pause/next/previous），title（可选文本）  媒体播放状态
  set_navigation      destination（文本），eta_min（可选，非负整数）     设置导航
  cancel_navigation   无参数                                          取消导航
  set_active_safety   feature（文本，如 esp/aeb），on（bool）           主动安全功能开关；状态里没有对应字段，
                                                                      关闭一律被 S06 拒绝，开启不改状态

执行流程（execute 是同步函数，"读-改-写"在存储锁内完成，中间没有 await）：
  校验命令和参数 → 取状态 → SafetyPolicy.check → 被拒绝时状态不变 → 放行时按 adjusted_args 写入 → 返回变化

返回值：
  放行    {"success": True, "changes": [{"field", "old", "new"}], "notice": "...", "rule_ids": [...]}
          值没变的字段不进 changes；没有说明时 notice 为空字符串
  拒绝    {"success": False, "blocked": True, "rule_id": "S01", "reason": "..."}
  不支持  {"success": False, "unsupported": True, "reason": "暂不支持这个功能"}
  参数错  {"success": False, "error": "..."}
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from vehicle.safety import SafetyPolicy, normalize_number
from vehicle.state import (
    DRIVE_MODES,
    SEAT_ZONES,
    WINDOW_ZONES,
    VehicleStateStore,
    get_path,
    same_value,
    set_path,
)

UNSUPPORTED_REASON = "暂不支持这个功能"


class CommandError(ValueError):
    """命令或参数不合法。"""


def make_command(action: str, **args: Any) -> Dict[str, Any]:
    """构造车辆命令。"""
    return {"action": action, "args": args}


# ── 命令词表 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Arg:
    """单个参数的约束。"""
    kind: str                            # bool / number / int / str / enum
    required: bool = False
    choices: Tuple[str, ...] = ()
    default: Any = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None


Writes = List[Tuple[str, Any]]


@dataclass(frozen=True)
class _CommandSpec:
    """动作的参数约束，以及放行后要写入的字段（点分路径 → 新值）。"""
    writes: Callable[[Dict[str, Any]], Writes]
    args: Dict[str, _Arg] = field(default_factory=dict)
    one_of: Tuple[str, ...] = ()         # 这几个参数必须且只能给一个


def _window_writes(args: Dict[str, Any]) -> Writes:
    zones = WINDOW_ZONES if args["zone"] == "all" else (args["zone"],)
    return [(f"body.windows.{zone}", args["open_percent"]) for zone in zones]


def _media_writes(args: Dict[str, Any]) -> Writes:
    playing = args["op"] != "pause"
    writes: Writes = [("media.playing", playing)]
    if playing and args.get("title"):
        writes.append(("media.title", args["title"]))
    return writes


_ON = {"on": _Arg("bool", required=True)}
_PERCENT = _Arg("int", required=True, min_value=0, max_value=100)

COMMANDS: Dict[str, _CommandSpec] = {
    "set_climate_power": _CommandSpec(lambda a: [("climate.on", a["on"])], _ON),
    "set_temperature": _CommandSpec(
        lambda a: [("climate.on", True), ("climate.temp", a["temp"])],
        {"temp": _Arg("number"), "delta": _Arg("number")},
        one_of=("temp", "delta"),
    ),
    "set_fan": _CommandSpec(
        lambda a: [("climate.on", True), ("climate.fan_level", a["level"])],
        {"level": _Arg("int"), "delta": _Arg("int")},
        one_of=("level", "delta"),
    ),
    "set_defrost": _CommandSpec(lambda a: [("climate.defrost", a["on"])], _ON),
    "set_seat_heat": _CommandSpec(
        lambda a: [(f"seats.seat_heat.{zone}", level) for zone, level in a["levels"].items()],
        {
            "zone": _Arg("enum", choices=SEAT_ZONES + ("all",), default="driver"),
            "level": _Arg("int"),
            "delta": _Arg("int"),
        },
        one_of=("level", "delta"),
    ),
    "set_window": _CommandSpec(
        _window_writes,
        {"zone": _Arg("enum", choices=WINDOW_ZONES + ("all",), default="fl"), "open_percent": _PERCENT},
    ),
    "set_sunroof": _CommandSpec(lambda a: [("body.sunroof", a["open_percent"])], {"open_percent": _PERCENT}),
    "set_trunk": _CommandSpec(lambda a: [("body.trunk_open", a["open"])], {"open": _Arg("bool", required=True)}),
    "set_door_lock": _CommandSpec(
        lambda a: [("body.doors_locked", a["locked"])], {"locked": _Arg("bool", required=True)},
    ),
    "set_ambient_light": _CommandSpec(lambda a: [("lights.ambient", a["on"])], _ON),
    "set_hazard": _CommandSpec(lambda a: [("lights.hazard", a["on"])], _ON),
    "set_drive_mode": _CommandSpec(
        lambda a: [("drive_mode", a["mode"])], {"mode": _Arg("enum", required=True, choices=DRIVE_MODES)},
    ),
    "set_volume": _CommandSpec(
        lambda a: [("media.volume", a["level"])],
        {"level": _Arg("int"), "delta": _Arg("int")},
        one_of=("level", "delta"),
    ),
    "media_control": _CommandSpec(
        _media_writes,
        {
            "op": _Arg("enum", required=True, choices=("play", "pause", "next", "previous")),
            "title": _Arg("str"),
        },
    ),
    "set_navigation": _CommandSpec(
        lambda a: [
            ("navigation.active", True),
            ("navigation.destination", a["destination"]),
            ("navigation.eta_min", a.get("eta_min")),
        ],
        {"destination": _Arg("str", required=True), "eta_min": _Arg("int", min_value=0)},
    ),
    "cancel_navigation": _CommandSpec(
        lambda a: [
            ("navigation.active", False),
            ("navigation.destination", None),
            ("navigation.eta_min", None),
        ],
    ),
    "set_active_safety": _CommandSpec(
        lambda a: [],
        {"feature": _Arg("str", required=True), "on": _Arg("bool", required=True)},
    ),
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce(name: str, spec: _Arg, value: Any) -> Any:
    """按参数约束校验并规整取值。"""
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise CommandError(f"参数 {name} 必须是布尔值")
        return value
    if spec.kind in ("number", "int"):
        if not _is_number(value):
            raise CommandError(f"参数 {name} 必须是数值")
        if spec.kind == "int":
            if not float(value).is_integer():
                raise CommandError(f"参数 {name} 必须是整数")
            value = int(value)
        else:
            value = normalize_number(value)
        if spec.min_value is not None and value < spec.min_value:
            raise CommandError(f"参数 {name} 不能小于 {spec.min_value}")
        if spec.max_value is not None and value > spec.max_value:
            raise CommandError(f"参数 {name} 不能大于 {spec.max_value}")
        return value
    if not isinstance(value, str):
        raise CommandError(f"参数 {name} 必须是文本")
    value = value.strip()
    if spec.kind == "enum":
        value = value.lower()
        if value not in spec.choices:
            raise CommandError(f"参数 {name} 只能是 {'/'.join(spec.choices)}")
    elif spec.required and not value:
        raise CommandError(f"参数 {name} 不能为空")
    return value


def _validate(spec: _CommandSpec, args: Dict[str, Any]) -> Dict[str, Any]:
    """校验参数：不认识的参数、缺必填、二选一不满足、类型或取值不对都抛 CommandError。"""
    unknown = sorted(set(args) - set(spec.args))
    if unknown:
        raise CommandError(f"不支持的参数: {', '.join(unknown)}")
    # 可选参数传 null 视同没传
    args = {key: value for key, value in args.items() if value is not None or spec.args[key].required}
    if spec.one_of and sum(key in args for key in spec.one_of) != 1:
        raise CommandError(f"{' 和 '.join(spec.one_of)} 需要且只能给一个")
    result: Dict[str, Any] = {}
    for name, arg in spec.args.items():
        if name in args:
            result[name] = _coerce(name, arg, args[name])
        elif arg.required:
            raise CommandError(f"缺少参数: {name}")
        elif arg.default is not None:
            result[name] = arg.default
    return result


# ── 车辆服务 ──────────────────────────────────────────────────────────────────

class VehicleService:
    """车辆服务接口：同步执行车辆命令，写操作统一先过安全规则。"""

    def __init__(self, store: Optional[VehicleStateStore] = None, policy: Optional[SafetyPolicy] = None):
        self.store = store if store is not None else VehicleStateStore()
        self.policy = policy if policy is not None else SafetyPolicy()

    def snapshot(self, user_id: str) -> Dict[str, Any]:
        """车辆状态的深拷贝。"""
        return self.store.snapshot(user_id)

    def execute(self, user_id: str, command: Dict[str, Any]) -> Dict[str, Any]:
        """执行一条车辆命令；不支持、参数错误和被拒绝时都不改状态。"""
        if not isinstance(command, dict) or not isinstance(command.get("action"), str):
            return {"success": False, "error": "命令格式错误，需要 action 字段"}
        action = command["action"].strip()
        spec = COMMANDS.get(action)
        if spec is None:
            return {"success": False, "unsupported": True, "reason": UNSUPPORTED_REASON}
        raw_args = command.get("args") or {}
        if not isinstance(raw_args, dict):
            return {"success": False, "error": "args 必须是对象"}

        try:
            args = _validate(spec, raw_args)
            with self.store.lock:
                state = self.store.get(user_id)
                decision = self.policy.check(state, {"action": action, "args": args})
                if not decision.allowed:
                    return {
                        "success": False,
                        "blocked": True,
                        "rule_id": decision.rule_id,
                        "reason": decision.notice,
                    }
                changes: List[Dict[str, Any]] = []
                for path, value in spec.writes(decision.adjusted_args):
                    old = get_path(state, path)
                    if same_value(old, value):
                        continue
                    set_path(state, path, value)
                    changes.append({"field": path, "old": old, "new": value})
        except ValueError as ex:
            return {"success": False, "error": str(ex)}

        return {
            "success": True,
            "changes": changes,
            "notice": decision.notice,
            "rule_ids": list(decision.rule_ids),
        }
