"""CabinWeave Agent 工具定义与实现。

编排器维护工具注册表（名字 → AgentToolSpec），各 Agent 按自己的 tool_scope 过滤可见工具。
本文件按角色提供工具集合，需要车辆服务的工具由构造函数绑定 VehicleService：
  - vehicle_control_tools(service)  车控：set_climate、set_body、set_lighting、set_drive_mode
  - navigation_tools(service)       导航：search_poi、plan_route、get_route_status
  - advisor_tools(service)          用车顾问：lookup_warning、estimate_range（search_knowledge_base 由 build_rag_tools 提供）
  - cabin_tools(service)            座舱助手：control_media、set_volume、make_call、get_weather、suggest_required_fields
  - build_agent_tools(service)      以上四组的合集，编排器构造时注册
  - build_rag_tools(tool_manager)   search_knowledge_base：检索车辆用户手册；检索为空时返回 found=False
                                    和 MANUAL_NOT_FOUND 提示

约定：
  - handler 签名统一为 (req, args)，快速通道可以直接调用同一个函数
  - 一个用户一辆车，车辆状态按 req.user_id 存取
  - 写车辆状态一律经 VehicleService.execute，同样过安全规则；一次工具调用可能对应多条命令，依次执行后合并成一个返回值
  - 参数不合法（枚举值、互斥参数、缺参数、类型）返回 {"success": False, "error": "..."}，不抛异常；
    枚举值同时接受标准值和常见中文叫法
  - 状态里没有的功能（雨刮、后视镜、遮阳帘、大灯、阅读灯、座椅通风、方向盘加热、座椅按摩、能量回收等）
    也有参数可传，调用时返回 unsupported，避免模型没有工具可调时假装执行成功
  - 返回值给模型看，保持紧凑，不带整份车辆状态

写操作的合并返回值（_merge_results）：
  success               全部命令都成功才为 True
  changes               全部状态变化 [{"field", "old", "new"}]，编排器据此汇总 state_changes
  notice                放行命令的调整说明和提醒，按顺序拼接（去重），可直接播报
  rule_ids              触发的全部安全规则（含拒绝的），按顺序去重；没有时不出现
  blocked / rule_id / reason
                        有命令被安全规则拒绝时 blocked 为 True，rule_id 为第一条拒绝的规则，
                        reason 为全部拒绝原因拼接（去重）；编排器据此汇总 blocked_actions
  unsupported / unsupported_features
                        有不支持的功能时 unsupported 为 True，unsupported_features 列出功能名；
                        没有被拒绝的命令时 reason 为"暂不支持这个功能"
  error                 参数错误说明，多条用"；"连接

续航估算（estimate_range）的算法见 estimate_arrival。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from functools import partial, wraps
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, TYPE_CHECKING, Union

from vehicle import mock_services
from vehicle.safety import LOW_BATTERY_PCT
from vehicle.service import UNSUPPORTED_REASON, VehicleService, make_command
from vehicle.state import DEFAULT_STATE, get_path
from vehicle.warning_lights import SEVERITY_ORDER, WARNING_NOT_FOUND, WARNING_NOTE, lookup_warning_light

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可见工具的定义和执行函数。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """创建带 JSON Schema 的 Agent 工具。"""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


# ── 参数解析 ──────────────────────────────────────────────────────────────────

class _ArgError(ValueError):
    """工具参数不合法；由 _guard 转成 {"success": False, "error": ...}。"""


def _guard(func: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """参数不合法时返回 error，不抛异常。"""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            return func(*args, **kwargs)
        except _ArgError as ex:
            return {"success": False, "error": str(ex)}

    return wrapper


def _key(text: Any) -> str:
    """枚举值和别名的比较键：去掉空白、转小写。"""
    return "".join(str(text or "").split()).lower()


def _alias_map(table: Dict[str, Iterable[str]]) -> Dict[str, str]:
    """{标准值: 别名列表} → {比较键: 标准值}，标准值本身也收进去。"""
    mapping: Dict[str, str] = {}
    for code, aliases in table.items():
        mapping[_key(code)] = code
        for alias in aliases:
            mapping[_key(alias)] = code
    return mapping


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _opt_bool(args: Dict[str, Any], key: str) -> Optional[bool]:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _ArgError(f"参数 {key} 必须是布尔值")
    return value


def _opt_number(args: Dict[str, Any], key: str) -> Optional[float]:
    value = args.get(key)
    if value is None:
        return None
    if not _is_number(value):
        raise _ArgError(f"参数 {key} 必须是数值")
    return value


def _opt_int(args: Dict[str, Any], key: str) -> Optional[int]:
    value = _opt_number(args, key)
    if value is None:
        return None
    if not float(value).is_integer():
        raise _ArgError(f"参数 {key} 必须是整数")
    return int(value)


def _opt_text(args: Dict[str, Any], key: str) -> Optional[str]:
    """文本参数；空串视同没传。"""
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ArgError(f"参数 {key} 必须是文本")
    return value.strip() or None


def _opt_text_list(args: Dict[str, Any], key: str) -> List[str]:
    """文本列表参数；单个文本按一项处理，空项去掉。"""
    value = args.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _ArgError(f"参数 {key} 必须是文本列表")
    return [item.strip() for item in value if item.strip()]


def _choice(value: Optional[str], aliases: Dict[str, str], key: str, allowed: Sequence[str], what: str = "") -> Optional[str]:
    """枚举值或中文叫法 → 标准值；认不出时报错并列出可选值。"""
    if value is None:
        return None
    code = aliases.get(_key(value))
    if code is None:
        raise _ArgError(f"{what}参数 {key} 只能是 {'/'.join(allowed)}")
    return code


def _one_of(values: Dict[str, Any]) -> None:
    """互斥参数：最多给一个。"""
    given = [name for name, value in values.items() if value is not None]
    if len(given) > 1:
        raise _ArgError(f"{' 和 '.join(given)} 只能给一个")


def _stable_hash(text: str) -> int:
    """确定性的哈希（md5），同一文本每次结果相同。"""
    return int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)


# ── 执行与合并 ────────────────────────────────────────────────────────────────

def _unsupported(feature: str) -> Dict[str, Any]:
    return {"success": False, "unsupported": True, "reason": UNSUPPORTED_REASON, "feature": feature}


def _dedupe(items: Iterable[Any]) -> List[Any]:
    return list(dict.fromkeys(item for item in items if item))


def _merge_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把多条命令的执行结果合并成一个工具返回值，字段含义见模块说明。"""
    changes: List[Dict[str, Any]] = []
    notices: List[str] = []
    rule_ids: List[str] = []
    blocked_rules: List[str] = []
    reasons: List[str] = []
    features: List[str] = []
    unsupported = False
    errors: List[str] = []
    for result in results:
        changes.extend(dict(change) for change in result.get("changes") or [] if isinstance(change, dict))
        if result.get("blocked") is True:
            blocked_rules.append(str(result.get("rule_id") or ""))
            rule_ids.append(str(result.get("rule_id") or ""))
            reasons.append(str(result.get("reason") or ""))
        elif result.get("unsupported") is True:
            unsupported = True
            features.append(str(result.get("feature") or ""))
        elif result.get("success") is True:
            notices.append(str(result.get("notice") or ""))
            rule_ids.extend(str(rule) for rule in result.get("rule_ids") or [])
        elif result.get("error"):
            errors.append(str(result["error"]))

    merged: Dict[str, Any] = {
        "success": bool(results) and all(result.get("success") is True for result in results),
        "changes": changes,
        "notice": "".join(_dedupe(notices)),
    }
    if _dedupe(rule_ids):
        merged["rule_ids"] = _dedupe(rule_ids)
    if blocked_rules:
        merged["blocked"] = True
        merged["rule_id"] = blocked_rules[0]
        merged["reason"] = "".join(_dedupe(reasons))
    if unsupported:
        merged["unsupported"] = True
        if _dedupe(features):
            merged["unsupported_features"] = _dedupe(features)
        merged.setdefault("reason", UNSUPPORTED_REASON)
    if errors:
        merged["error"] = "；".join(_dedupe(errors))
    return merged


def _execute(
    service: VehicleService, req: Request, commands: Sequence[Dict[str, Any]], unsupported: Sequence[str] = (),
) -> Dict[str, Any]:
    """依次经车辆服务执行命令（每条都过安全规则），再把不支持的功能并进结果。"""
    results = [service.execute(req.user_id, command) for command in commands]
    results += [_unsupported(feature) for feature in unsupported]
    return _merge_results(results)


# ── 车控：set_climate ─────────────────────────────────────────────────────────

SEAT_HEAT_ZONES = ("driver", "passenger", "all", "rear")
_SEAT_ZONE_ALIASES = _alias_map({
    "driver": ("主驾", "主驾驶", "驾驶位", "左前"),
    "passenger": ("副驾", "副驾驶", "右前"),
    "all": ("both", "front", "前排", "全部", "所有"),
    "rear": ("back", "后排"),
})
# 状态里没有的空调舒适功能 → 播报名称
_COMFORT_FEATURES = {
    "seat_ventilation": "座椅通风",
    "steering_wheel_heat": "方向盘加热",
    "seat_massage": "座椅按摩",
    "other": "这项空调功能",
}
_COMFORT_ALIASES = _alias_map({
    "seat_ventilation": ("座椅通风", "通风"),
    "steering_wheel_heat": ("方向盘加热",),
    "seat_massage": ("座椅按摩", "按摩"),
    "other": (),
})


@_guard
def set_climate(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    空调与座椅加热：power / temperature 或 temperature_delta / fan_level 或 fan_delta / defrost /
    seat_heat_zone + seat_heat_level 或 seat_heat_delta；comfort_feature 返回不支持。

    命令顺序：打开空调 → 温度 → 风量 → 除雾 → 座椅加热 → 关闭空调；关闭空调时不能同时调温度或风量。
    """
    power = _opt_bool(args, "power")
    temp, temp_delta = _opt_number(args, "temperature"), _opt_number(args, "temperature_delta")
    fan, fan_delta = _opt_int(args, "fan_level"), _opt_int(args, "fan_delta")
    defrost = _opt_bool(args, "defrost")
    zone = _choice(_opt_text(args, "seat_heat_zone"), _SEAT_ZONE_ALIASES, "seat_heat_zone", SEAT_HEAT_ZONES)
    seat_level, seat_delta = _opt_int(args, "seat_heat_level"), _opt_int(args, "seat_heat_delta")
    comfort = _opt_text(args, "comfort_feature")
    _one_of({"temperature": temp, "temperature_delta": temp_delta})
    _one_of({"fan_level": fan, "fan_delta": fan_delta})
    _one_of({"seat_heat_level": seat_level, "seat_heat_delta": seat_delta})
    if power is False and any(value is not None for value in (temp, temp_delta, fan, fan_delta)):
        raise _ArgError("关闭空调时不能同时调温度或风量")

    commands: List[Dict[str, Any]] = []
    unsupported: List[str] = []
    if power is True:
        commands.append(make_command("set_climate_power", on=True))
    if temp is not None:
        commands.append(make_command("set_temperature", temp=temp))
    elif temp_delta is not None:
        commands.append(make_command("set_temperature", delta=temp_delta))
    if fan is not None:
        commands.append(make_command("set_fan", level=fan))
    elif fan_delta is not None:
        commands.append(make_command("set_fan", delta=fan_delta))
    if defrost is not None:
        commands.append(make_command("set_defrost", on=defrost))
    if zone == "rear":
        unsupported.append("后排座椅加热")
    elif seat_level is not None or seat_delta is not None:
        amount = {"level": seat_level} if seat_level is not None else {"delta": seat_delta}
        commands.append(make_command("set_seat_heat", zone=zone or "driver", **amount))
    elif zone is not None:
        raise _ArgError("设置座椅加热需要 seat_heat_level 或 seat_heat_delta")
    if comfort:
        unsupported.append(_COMFORT_FEATURES.get(_COMFORT_ALIASES.get(_key(comfort), ""), comfort))
    if power is False:
        commands.append(make_command("set_climate_power", on=False))
    if not commands and not unsupported:
        raise _ArgError("至少需要一个参数")
    return _execute(service, req, commands, unsupported)


# ── 车控：set_body ────────────────────────────────────────────────────────────

BODY_PARTS = ("window", "sunroof", "trunk", "door_lock", "wiper", "mirror", "sunshade", "seat", "other")
_BODY_PART_ALIASES = _alias_map({
    "window": ("车窗", "窗户", "窗"),
    "sunroof": ("天窗",),
    "trunk": ("尾门", "后备箱", "后备厢", "tailgate"),
    "door_lock": ("门锁", "车锁", "车门", "中控锁", "door", "lock"),
    "wiper": ("雨刮", "雨刷", "雨刮器"),
    "mirror": ("后视镜", "外后视镜"),
    "sunshade": ("遮阳帘", "天窗遮阳帘"),
    "seat": ("座椅", "座椅位置", "seat_position"),
    "other": (),
})
_UNSUPPORTED_BODY = {
    "wiper": "雨刮",
    "mirror": "后视镜",
    "sunshade": "遮阳帘",
    "seat": "座椅位置调节",
    "other": "这项车身功能",
}
_BODY_LABELS = {"window": "车窗", "sunroof": "天窗", "trunk": "尾门", "door_lock": "门锁"}
_OPEN_CLOSE = _alias_map({"open": ("打开", "开", "开启", "降下"), "close": ("关闭", "关", "关上", "升起")})
_LOCK_UNLOCK = _alias_map({"lock": ("上锁", "锁", "锁车", "落锁"), "unlock": ("解锁", "开锁")})

WINDOW_ZONES = ("fl", "fr", "rl", "rr", "front", "rear", "left", "right", "all", "driver", "passenger")
_WINDOW_ZONE_ALIASES = _alias_map({
    "fl": ("driver", "主驾", "主驾驶", "左前"),
    "fr": ("passenger", "副驾", "副驾驶", "右前"),
    "rl": ("左后",),
    "rr": ("右后",),
    "front": ("前排",),
    "rear": ("后排",),
    "left": ("左侧", "左边"),
    "right": ("右侧", "右边"),
    "all": ("全部", "所有", "全车", "四门"),
})
# 一个说法对应多扇车窗时展开成多条 set_window 命令
_WINDOW_ZONE_TARGETS = {"front": ("fl", "fr"), "rear": ("rl", "rr"), "left": ("fl", "rl"), "right": ("fr", "rr")}


@_guard
def set_body(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    车窗、天窗、尾门、门锁：part + action（+ 车窗的 zone，车窗和天窗的 open_percent）。

    车窗不说位置时默认主驾；open_percent 不给时打开为 100、关闭为 0；
    雨刮、后视镜、遮阳帘、座椅位置等返回不支持。
    """
    part = _choice(_opt_text(args, "part"), _BODY_PART_ALIASES, "part", BODY_PARTS)
    if part is None:
        raise _ArgError("缺少参数 part")
    if part in _UNSUPPORTED_BODY:
        return _execute(service, req, [], [_UNSUPPORTED_BODY[part]])

    action_text = _opt_text(args, "action")
    if action_text is None:
        raise _ArgError("缺少参数 action")
    label = _BODY_LABELS[part]
    if part == "door_lock":
        action = _choice(action_text, _LOCK_UNLOCK, "action", ("lock", "unlock"), what=f"{label}的")
        return _execute(service, req, [make_command("set_door_lock", locked=action == "lock")])

    action = _choice(action_text, _OPEN_CLOSE, "action", ("open", "close"), what=f"{label}的")
    if part == "trunk":
        return _execute(service, req, [make_command("set_trunk", open=action == "open")])

    percent = _opt_int(args, "open_percent")
    target = percent if percent is not None else (100 if action == "open" else 0)
    if part == "sunroof":
        return _execute(service, req, [make_command("set_sunroof", open_percent=target)])

    zone = _choice(_opt_text(args, "zone"), _WINDOW_ZONE_ALIASES, "zone", WINDOW_ZONES) or "fl"
    zones = _WINDOW_ZONE_TARGETS.get(zone, (zone,))
    return _execute(service, req, [make_command("set_window", zone=name, open_percent=target) for name in zones])


# ── 车控：set_lighting ────────────────────────────────────────────────────────

LIGHT_TYPES = ("ambient", "hazard", "headlight", "reading", "fog", "other")
_LIGHT_TYPE_ALIASES = _alias_map({
    "ambient": ("氛围灯",),
    "hazard": ("双闪", "危险报警灯", "警示灯"),
    "headlight": ("大灯", "前大灯", "近光灯", "远光灯", "headlights"),
    "reading": ("阅读灯", "顶灯", "车内灯", "reading_light"),
    "fog": ("雾灯", "fog_light"),
    "other": (),
})
_UNSUPPORTED_LIGHTS = {"headlight": "大灯", "reading": "阅读灯", "fog": "雾灯", "other": "这项灯光功能"}
_ON_OFF = _alias_map({"on": ("打开", "开", "开启", "open"), "off": ("关闭", "关", "关上", "close")})


@_guard
def set_lighting(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """氛围灯、双闪：type + action（on/off）；大灯、阅读灯、雾灯等返回不支持。"""
    light = _choice(_opt_text(args, "type"), _LIGHT_TYPE_ALIASES, "type", LIGHT_TYPES)
    if light is None:
        raise _ArgError("缺少参数 type")
    if light in _UNSUPPORTED_LIGHTS:
        return _execute(service, req, [], [_UNSUPPORTED_LIGHTS[light]])
    action = _choice(_opt_text(args, "action"), _ON_OFF, "action", ("on", "off"))
    if action is None:
        raise _ArgError("缺少参数 action")
    name = "set_ambient_light" if light == "ambient" else "set_hazard"
    return _execute(service, req, [make_command(name, on=action == "on")])


# ── 车控：set_drive_mode ──────────────────────────────────────────────────────

DRIVE_MODES = ("eco", "comfort", "sport")
_DRIVE_MODE_ALIASES = _alias_map({
    "eco": ("经济", "节能", "省电", "经济模式", "节能模式"),
    "comfort": ("舒适", "标准", "舒适模式", "标准模式", "normal"),
    "sport": ("运动", "运动模式"),
})
# 驾驶辅助 / 主动安全功能的叫法 → 安全规则认识的代码；不在表里的按原文交给车辆服务
_ASSIST_FEATURE_ALIASES = _alias_map({
    "esp": ("esc", "车身稳定", "车身稳定系统", "车身稳定控制"),
    "aeb": ("自动紧急制动", "紧急制动", "自动刹车", "主动刹车"),
    "fcw": ("前方碰撞预警", "碰撞预警", "前碰预警"),
    "lka": ("车道保持", "车道保持辅助", "lkas"),
    "bsd": ("盲区监测", "盲点监测", "bsm"),
})


@_guard
def set_drive_mode(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    驾驶模式与驾驶辅助：mode；assist_feature + assist_on（经 set_active_safety，关闭会被 S06 拒绝）；
    regen_level（能量回收）、steering_mode（转向手感）返回不支持。
    """
    mode = _choice(_opt_text(args, "mode"), _DRIVE_MODE_ALIASES, "mode", DRIVE_MODES)
    feature = _opt_text(args, "assist_feature")
    assist_on = _opt_bool(args, "assist_on")
    if feature is not None and assist_on is None:
        raise _ArgError("设置驾驶辅助功能需要同时给 assist_on")
    if assist_on is not None and feature is None:
        raise _ArgError("assist_on 需要和 assist_feature 一起给")

    commands: List[Dict[str, Any]] = []
    if mode is not None:
        commands.append(make_command("set_drive_mode", mode=mode))
    if feature is not None:
        code = _ASSIST_FEATURE_ALIASES.get(_key(feature), feature)
        commands.append(make_command("set_active_safety", feature=code, on=assist_on))
    unsupported: List[str] = []
    if args.get("regen_level") not in (None, ""):
        unsupported.append("能量回收调节")
    if args.get("steering_mode") not in (None, ""):
        unsupported.append("转向手感调节")
    if not commands and not unsupported:
        raise _ArgError("至少需要一个参数")
    return _execute(service, req, commands, unsupported)


# ── 导航：search_poi / plan_route / get_route_status ─────────────────────────

POI_CATEGORIES = tuple(mock_services.POI_CATEGORIES)
POI_SORTS = ("distance", "available", "power")
_POI_FIELDS = ("name", "category_name", "address", "distance_km", "duration_min",
               "available_piles", "total_piles", "power_kw")

ROUTE_PREFERENCES = ("fastest", "avoid_highway", "avoid_toll")
_PREFERENCE_ALIASES = _alias_map({
    "fastest": ("最快", "时间最短", "躲避拥堵", "默认"),
    "avoid_highway": ("避开高速", "不走高速", "躲避高速"),
    "avoid_toll": ("避开收费", "不走收费", "少收费", "免费"),
})
# 路线偏好的模拟效果：(播报名称, 距离系数, 时长系数)
_PREFERENCE_EFFECTS = {
    "fastest": ("最快", 1.0, 1.0),
    "avoid_highway": ("避开高速", 1.15, 1.3),
    "avoid_toll": ("避开收费", 1.05, 1.1),
}
MAX_WAYPOINTS = 3

# 驾驶员偏好里的地点（profile["places"]["home" / "company"]）
_PROFILE_PLACES = {
    "home": ("家", {"家", "回家", "我家", "家里", "到家", "回我家", "home"}),
    "company": ("公司", {"公司", "去公司", "回公司", "我公司", "单位", "去单位", "回单位", "上班", "去上班",
                         "company", "office", "work"}),
}

_TRAFFIC_LEVELS = ("畅通", "缓行", "拥堵")


def _drive_minutes(distance_km: float) -> int:
    """行驶时长，与模拟地图同一口径：15 km 以内按市区 30 km/h，超出部分按 80 km/h。"""
    city_km = min(distance_km, 15.0)
    highway_km = max(distance_km - 15.0, 0.0)
    return max(1, round(city_km / 30 * 60 + highway_km / 80 * 60))


def _profile_place_kind(name: str) -> Optional[str]:
    """"家""回家""公司""去单位"等说法 → home / company；其他返回 None。"""
    key = _key(name)
    for kind, (_, aliases) in _PROFILE_PLACES.items():
        if key in aliases:
            return kind
    return None


def _profile_address(req: Request, kind: str) -> Optional[str]:
    """从驾驶员偏好取家或公司的地址；值可以是文本，也可以是带 address 的对象。"""
    profile = getattr(req, "profile", None) or {}
    places = profile.get("places") if isinstance(profile, dict) else None
    value = places.get(kind) if isinstance(places, dict) else None
    if isinstance(value, dict):
        value = value.get("address") or value.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve_place(req: Request, name: str) -> Dict[str, Any]:
    """
    把目的地说法解析成地点：{"success": True, "destination", "distance_km", "duration_min"}（地图地点另带 address）。

    家、公司从驾驶员偏好取地址：地址能在模拟地图里查到时用地图的距离，查不到时按地址哈希出 3–30 km 的固定距离；
    返回结果不带具体地址。偏好里没有时返回 need_info；其他目的地查模拟地图，需要确认时原样返回候选。
    """
    kind = _profile_place_kind(name)
    if kind is not None:
        label = _PROFILE_PLACES[kind][0]
        address = _profile_address(req, kind)
        if address is None:
            return {
                "success": False,
                "need_info": f"{kind}_address",
                "reason": f"还不知道你{label}的地址，请告诉我具体地址。",
            }
        route = mock_services.plan_route(address)
        if route.get("success"):
            distance, duration = route["distance_km"], route["duration_min"]
        else:
            distance = float(3 + _stable_hash(address) % 28)
            duration = _drive_minutes(distance)
        return {"success": True, "destination": label, "distance_km": distance, "duration_min": duration}

    route = mock_services.plan_route(name)
    if not route.get("success"):
        keys = ("success", "need_confirmation", "destination", "reason", "candidates")
        return {key: route[key] for key in keys if key in route}
    return {
        "success": True,
        "destination": route["destination"],
        "address": route["address"],
        "distance_km": route["distance_km"],
        "duration_min": route["duration_min"],
    }


def _route_distance(points_km: Sequence[float]) -> float:
    """
    经过途经点的总距离（模拟）：各点只有到当前位置的距离，近似看成在一条线上，
    相邻两点之间按距离差计算，每段至少 1 km。
    """
    total, previous = 0.0, 0.0
    for point in points_km:
        total += max(abs(point - previous), 1.0)
        previous = point
    return round(total, 1)


@_guard
def search_poi(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """按类别或关键词检索地点（模拟地图），结果只保留播报需要的字段。"""
    category = _opt_text(args, "category")
    keyword = _opt_text(args, "keyword")
    sort_by = _opt_text(args, "sort_by") or "distance"
    require_available = _opt_bool(args, "require_available") or False
    result = mock_services.search_poi(
        category=category, keyword=keyword, sort_by=sort_by, require_available=require_available,
    )
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "地点检索失败")}
    return {
        "success": True,
        "category": result["category"],
        "sort_by": result["sort_by"],
        "total": result["total"],
        "results": [{key: poi[key] for key in _POI_FIELDS if key in poi} for poi in result["results"]],
    }


@_guard
def plan_route(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    规划路线并设置导航：destination、preference、waypoints。

    目的地或途经点需要确认 / 需要驾驶员提供地址时原样返回，不写导航状态；
    规划成功后经车辆服务 set_navigation 写入目的地和预计时长。
    偏好是模拟效果：避开高速距离 ×1.15、时长 ×1.3，避开收费距离 ×1.05、时长 ×1.1。
    """
    destination = _opt_text(args, "destination")
    if destination is None:
        raise _ArgError("缺少参数 destination")
    preference = _choice(
        _opt_text(args, "preference"), _PREFERENCE_ALIASES, "preference", ROUTE_PREFERENCES,
    ) or "fastest"
    waypoints = _opt_text_list(args, "waypoints")
    if len(waypoints) > MAX_WAYPOINTS:
        raise _ArgError(f"途经点最多 {MAX_WAYPOINTS} 个")

    target = _resolve_place(req, destination)
    if not target["success"]:
        return target
    stops = []
    for name in waypoints:
        stop = _resolve_place(req, name)
        if not stop["success"]:
            return dict(stop, waypoint=name)
        stops.append(stop)

    distance, duration = target["distance_km"], target["duration_min"]
    if stops:
        distance = _route_distance([stop["distance_km"] for stop in stops] + [distance])
        duration = _drive_minutes(distance)
    label, distance_factor, time_factor = _PREFERENCE_EFFECTS[preference]
    distance = round(distance * distance_factor, 1)
    duration = max(1, round(duration * time_factor))

    written = _execute(
        service, req, [make_command("set_navigation", destination=target["destination"], eta_min=duration)],
    )
    result: Dict[str, Any] = {"success": written["success"], "destination": target["destination"]}
    if target.get("address"):
        result["address"] = target["address"]
    result.update({"distance_km": distance, "duration_min": duration, "preference": label})
    if stops:
        result["waypoints"] = [stop["destination"] for stop in stops]
    result.update({key: value for key, value in written.items() if key != "success"})
    return result


def get_route_status(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """当前导航状态，外加按目的地哈希出的固定模拟路况；没在导航时如实返回。"""
    navigation = service.snapshot(req.user_id).get("navigation") or {}
    if not navigation.get("active"):
        return {"success": True, "active": False, "message": "当前没有在导航。"}
    destination = str(navigation.get("destination") or "")
    return {
        "success": True,
        "active": True,
        "destination": destination,
        "eta_min": navigation.get("eta_min"),
        "traffic": _TRAFFIC_LEVELS[_stable_hash(destination) % len(_TRAFFIC_LEVELS)],
        "note": "示例路况",
    }


# ── 用车顾问：lookup_warning / estimate_range ─────────────────────────────────

RANGE_NOTE = "仅为估算，实际续航受车速、路况、空调和载重影响。"


@_guard
def lookup_warning(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    查故障灯表：signal 为故障灯名称、别名或示例编码；signal 为空时查车辆当前亮起的全部故障灯
    （按严重度从高到低排列）；查不到返回"未收录"。
    """
    signal = _opt_text(args, "signal")
    if signal is not None:
        item = lookup_warning_light(signal)
        if item is None:
            return {"success": False, "not_found": True, "signal": signal, "reason": WARNING_NOT_FOUND}
        return {"success": True, "signal": signal, **item, "note": WARNING_NOTE}

    lights = service.snapshot(req.user_id).get("warning_lights") or []
    if not lights:
        return {"success": True, "source": "vehicle", "warnings": [], "message": "当前仪表没有亮起的故障灯。"}
    warnings = []
    for name in lights:
        item = lookup_warning_light(name)
        if item is None:
            warnings.append({"signal": name, "not_found": True, "reason": WARNING_NOT_FOUND})
        else:
            warnings.append({"signal": name, **item})
    rank = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
    warnings.sort(key=lambda warning: rank.get(warning.get("severity"), len(rank)))
    return {"success": True, "source": "vehicle", "warnings": warnings, "note": WARNING_NOTE}


def low_temp_margin(outside_temp_c: Optional[float]) -> float:
    """低温余量：0℃ 及以下多留 25%，0–10℃ 多留 10%，10℃ 及以上不额外留。"""
    if outside_temp_c is None:
        return 0.0
    if outside_temp_c <= 0:
        return 0.25
    if outside_temp_c < 10:
        return 0.10
    return 0.0


def estimate_arrival(
    distance_km: float, battery_pct: float, range_km: float, outside_temp_c: Optional[float],
) -> Dict[str, Any]:
    """
    确定性的到达电量估算：
      1. 需要的续航 = 距离 × (1 + 低温余量)
      2. 到达后剩余续航 = 当前续航 - 需要的续航
      3. 到达后剩余电量 = 当前电量 × 剩余续航 / 当前续航（续航按当前电量线性折算）
      4. 剩余电量、剩余续航向下取整，最低为 0；剩余续航不大于 0 视为到不了
      5. 到不了，或到达后电量低于 15%（与安全规则 S07 的低电量阈值一致）时建议途中充电
    """
    margin = low_temp_margin(outside_temp_c)
    needed_km = distance_km * (1 + margin)
    left_km = range_km - needed_km
    left_pct = battery_pct * left_km / range_km if range_km > 0 else -1.0
    reachable = left_km > 0 and battery_pct > 0
    arrival_pct = max(0, math.floor(left_pct)) if reachable else 0
    arrival_range = max(0, math.floor(left_km)) if reachable else 0
    suggest_charging = not reachable or arrival_pct < LOW_BATTERY_PCT
    if not reachable:
        advice = "按当前电量可能到不了，建议先就近充电。"
    elif suggest_charging:
        advice = f"到达后电量只剩约{arrival_pct}%，建议途中充电。"
    else:
        advice = "电量够用，途中不需要充电。"

    result: Dict[str, Any] = {"distance_km": round(distance_km, 1)}
    if margin > 0:
        result["low_temp_margin_pct"] = round(margin * 100)
    result.update({
        "arrival_battery_pct": arrival_pct,
        "arrival_range_km": arrival_range,
        "reachable": reachable,
        "suggest_charging": suggest_charging,
        "advice": advice,
    })
    return result


@_guard
def estimate_range(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    估算到达后的剩余电量和续航：destination 或 distance_km 二选一。

    destination 经模拟地图取距离（家、公司同样从驾驶员偏好取），需要确认或需要地址时原样返回；
    按车辆当前电量、续航和室外温度计算，算法见 estimate_arrival，结果标注"仅为估算"。
    """
    destination = _opt_text(args, "destination")
    distance = _opt_number(args, "distance_km")
    _one_of({"destination": destination, "distance_km": distance})
    if destination is None and distance is None:
        raise _ArgError("需要 destination 或 distance_km 其中一个")

    place_name = None
    if destination is not None:
        place = _resolve_place(req, destination)
        if not place["success"]:
            return place
        distance, place_name = place["distance_km"], place["destination"]
    if distance <= 0:
        raise _ArgError("distance_km 必须大于 0")

    state = service.snapshot(req.user_id)
    estimate = estimate_arrival(
        float(distance),
        float(state.get("battery_pct") or 0),
        float(state.get("range_km") or 0),
        state.get("outside_temp_c"),
    )
    result: Dict[str, Any] = {"success": True, "note": RANGE_NOTE}
    if place_name:
        result["destination"] = place_name
    result.update(estimate)
    return result


# ── 座舱助手：control_media / set_volume / make_call / get_weather ────────────

MEDIA_ACTIONS = ("play", "pause", "next", "previous")
_MEDIA_ACTION_ALIASES = _alias_map({
    "play": ("播放", "继续播放", "继续", "resume"),
    "pause": ("暂停", "停止", "停", "stop"),
    "next": ("下一首", "下一曲", "切歌", "换一首", "skip"),
    "previous": ("上一首", "上一曲", "prev"),
})
# 取消静音时没有记录静音前的音量，恢复到默认音量
_DEFAULT_VOLUME = DEFAULT_STATE["media"]["volume"]


@_guard
def control_media(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    播放控制：action（play/pause/next/previous）+ 可选 query。

    有 query 时用示例曲库生成标题；直接播放且当前没有内容时播放"每日推荐（示例曲库）"。
    经车辆服务 media_control 写入，返回值附带当前播放状态和标题。
    """
    op = _choice(_opt_text(args, "action"), _MEDIA_ACTION_ALIASES, "action", MEDIA_ACTIONS)
    if op is None:
        raise _ArgError("缺少参数 action")
    query = _opt_text(args, "query")

    command_args: Dict[str, Any] = {"op": op}
    if op != "pause":
        if query:
            command_args["title"] = mock_services.search_media(query)["title"]
        elif op == "play" and not get_path(service.snapshot(req.user_id), "media.title"):
            command_args["title"] = mock_services.search_media()["title"]
    result = _execute(service, req, [make_command("media_control", **command_args)])
    media = service.snapshot(req.user_id).get("media") or {}
    result["playing"] = media.get("playing")
    result["title"] = media.get("title")
    return result


@_guard
def set_volume(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    音量：level / delta / mute 三选一。静音即调到 0；取消静音时当前音量为 0 则恢复到默认音量，
    否则保持不变。范围由安全规则 S05 裁剪到 0–30，返回值附带调整后的音量。
    """
    level, delta, mute = _opt_int(args, "level"), _opt_int(args, "delta"), _opt_bool(args, "mute")
    given = [value for value in (level, delta, mute) if value is not None]
    if len(given) != 1:
        raise _ArgError("level、delta、mute 需要且只能给一个")

    if mute is True:
        command = make_command("set_volume", level=0)
    elif mute is False:
        current = get_path(service.snapshot(req.user_id), "media.volume", 0) or 0
        command = make_command("set_volume", level=current if current > 0 else _DEFAULT_VOLUME)
    elif level is not None:
        command = make_command("set_volume", level=level)
    else:
        command = make_command("set_volume", delta=delta)
    result = _execute(service, req, [command])
    result["volume"] = get_path(service.snapshot(req.user_id), "media.volume")
    return result


@_guard
def make_call(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    模拟拨打电话（不真实拨号、不写车辆状态）：按名字或称呼查通讯录。

    唯一匹配返回"正在呼叫 某某（打码号码）"；多个匹配返回候选，让模型追问；查不到如实返回。
    """
    contact = _opt_text(args, "contact") or ""
    found = mock_services.find_contact(contact)
    if found.get("success"):
        person = found["contact"]
        return {
            "success": True,
            "simulated": True,
            "contact": person["name"],
            "relation": person["relation"],
            "phone": person["phone"],
            "message": f"正在呼叫 {person['name']}（{person['phone']}）",
        }
    if found.get("need_confirmation"):
        return {
            "success": False,
            "need_confirmation": True,
            "reason": found.get("reason", ""),
            "candidates": [
                {"name": person["name"], "relation": person["relation"], "phone": person["phone"]}
                for person in found.get("candidates") or []
            ],
        }
    return {"success": False, "not_found": True, "reason": found.get("reason", "通讯录里没有找到这个联系人。")}


@_guard
def get_weather(service: VehicleService, req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """天气（示例数据）：city 默认取车辆当前所在城市，date 默认今天。"""
    city = _opt_text(args, "city") or get_path(service.snapshot(req.user_id), "location.city")
    return mock_services.get_weather(city, _opt_text(args, "date"))


# 各意图下需要向驾驶员追问的关键信息；未列出的意图不需要追问
_REQUIRED_FIELDS: Dict[str, List[str]] = {
    "navigation": ["目的地"],
    "phone_call": ["联系人"],
    "poi_search": ["想找的地点类别"],
    "other": ["你想让我做什么"],
}


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """座舱助手工具：按分到的子任务意图（没有子任务时用主意图）给出需要追问的关键信息。"""
    intents = [task.intent.value for task in req.assigned_tasks]
    if not intents:
        intents = [req.intent.value if req.intent else "other"]
    fields: List[str] = []
    for intent in intents:
        for name in _REQUIRED_FIELDS.get(intent, []):
            if name not in fields:
                fields.append(name)
    return {"intents": intents, "required_fields": fields}


# ── 按角色构造工具集合 ────────────────────────────────────────────────────────

_WRITE_RESULT_HINT = "以返回为准：changes 是实际变化，notice 是需要播报的调整说明，blocked 表示被安全规则拒绝，unsupported 表示暂不支持。"


def vehicle_control_tools(service: VehicleService) -> Dict[str, AgentToolSpec]:
    """车控的工具集合：空调与座椅加热、车身、灯光、驾驶模式。"""
    return {
        "set_climate": make_tool(
            "set_climate",
            "调节空调和座椅加热：空调开关、温度、风量、前挡除雾、主副驾座椅加热，可以一次设置多项。"
            "座椅通风、方向盘加热、座椅按摩等用 comfort_feature 传入，会返回暂不支持。" + _WRITE_RESULT_HINT,
            {
                "power": {"type": "boolean", "description": "空调开关：true 打开，false 关闭"},
                "temperature": {"type": "number", "description": "目标温度（℃），范围 16–32，超出会被裁剪"},
                "temperature_delta": {
                    "type": "number", "description": "温度相对调节，调高两度为 2，调低一度为 -1；与 temperature 二选一",
                },
                "fan_level": {"type": "integer", "description": "目标风量档位，1–7"},
                "fan_delta": {"type": "integer", "description": "风量相对调节，风大一点为 1；与 fan_level 二选一"},
                "defrost": {"type": "boolean", "description": "前挡除雾：true 打开，false 关闭"},
                "seat_heat_zone": {
                    "type": "string",
                    "enum": list(SEAT_HEAT_ZONES),
                    "description": "座椅加热位置：driver 主驾（默认）、passenger 副驾、all 主副驾；rear 后排暂不支持",
                },
                "seat_heat_level": {"type": "integer", "description": "座椅加热档位 0–3，0 为关闭"},
                "seat_heat_delta": {"type": "integer", "description": "座椅加热相对调节；与 seat_heat_level 二选一"},
                "comfort_feature": {
                    "type": "string",
                    "enum": list(_COMFORT_FEATURES),
                    "description": "状态里没有的舒适功能：seat_ventilation 座椅通风、steering_wheel_heat 方向盘加热、"
                                   "seat_massage 座椅按摩、other 其他；传入后返回暂不支持",
                },
            },
            partial(set_climate, service),
        ),
        "set_body": make_tool(
            "set_body",
            "控制车窗、天窗、尾门和门锁。雨刮、后视镜、遮阳帘、座椅位置等也用这个工具，会返回暂不支持。"
            + _WRITE_RESULT_HINT,
            {
                "part": {
                    "type": "string",
                    "enum": list(BODY_PARTS),
                    "description": "部件：window 车窗、sunroof 天窗、trunk 尾门、door_lock 门锁；"
                                   "wiper 雨刮、mirror 后视镜、sunshade 遮阳帘、seat 座椅位置、other 其他（这些暂不支持）",
                },
                "action": {
                    "type": "string",
                    "enum": ["open", "close", "lock", "unlock"],
                    "description": "车窗、天窗、尾门用 open/close，门锁用 lock/unlock",
                },
                "zone": {
                    "type": "string",
                    "enum": list(WINDOW_ZONES),
                    "description": "车窗位置（只对车窗有效）：fl 左前、fr 右前、rl 左后、rr 右后、front 前排、rear 后排、"
                                   "left 左侧、right 右侧、all 全部、driver 主驾、passenger 副驾；不传默认主驾",
                },
                "open_percent": {
                    "type": "integer",
                    "description": "车窗或天窗开度 0–100，开一半为 50；不传时打开为 100、关闭为 0",
                },
            },
            partial(set_body, service),
            required=["part", "action"],
        ),
        "set_lighting": make_tool(
            "set_lighting",
            "开关氛围灯和双闪。大灯、阅读灯、雾灯等也用这个工具，会返回暂不支持。" + _WRITE_RESULT_HINT,
            {
                "type": {
                    "type": "string",
                    "enum": list(LIGHT_TYPES),
                    "description": "灯光：ambient 氛围灯、hazard 双闪；headlight 大灯、reading 阅读灯、fog 雾灯、"
                                   "other 其他灯光或调节（这些暂不支持）",
                },
                "action": {"type": "string", "enum": ["on", "off"], "description": "on 打开，off 关闭"},
            },
            partial(set_lighting, service),
            required=["type", "action"],
        ),
        "set_drive_mode": make_tool(
            "set_drive_mode",
            "切换驾驶模式，开关驾驶辅助和主动安全功能；为了行车安全，主动安全功能不能语音关闭，会被拒绝。"
            "能量回收、转向手感暂不支持，传入会返回不支持。" + _WRITE_RESULT_HINT,
            {
                "mode": {
                    "type": "string",
                    "enum": list(DRIVE_MODES),
                    "description": "驾驶模式：eco 经济、comfort 舒适、sport 运动",
                },
                "assist_feature": {
                    "type": "string",
                    "description": "驾驶辅助或主动安全功能：esp 车身稳定、aeb 自动紧急制动、fcw 前方碰撞预警、"
                                   "lka 车道保持、bsd 盲区监测，也可以直接写中文名",
                },
                "assist_on": {"type": "boolean", "description": "与 assist_feature 一起给：true 开启，false 关闭"},
                "regen_level": {"type": "string", "description": "能量回收强度（如强、中、弱），暂不支持"},
                "steering_mode": {"type": "string", "description": "转向手感（如轻、标准、重），暂不支持"},
            },
            partial(set_drive_mode, service),
        ),
    }


def navigation_tools(service: VehicleService) -> Dict[str, AgentToolSpec]:
    """导航的工具集合：地点检索、路线规划、导航状态。"""
    return {
        "search_poi": make_tool(
            "search_poi",
            "按类别或关键词查找附近地点（充电站、停车场、服务区、维修点、餐厅），返回最多 5 个结果；"
            "充电站带空闲桩数、总桩数和功率。",
            {
                "category": {
                    "type": "string",
                    "enum": list(POI_CATEGORIES),
                    "description": "类别：charging_station 充电站、parking 停车场、service_area 服务区、"
                                   "repair 维修点、restaurant 餐厅",
                },
                "keyword": {"type": "string", "description": "名称或地址里的关键词"},
                "sort_by": {
                    "type": "string",
                    "enum": list(POI_SORTS),
                    "description": "排序：distance 距离最近（默认）、available 空闲桩最多、power 功率最大",
                },
                "require_available": {"type": "boolean", "description": "true 时只要有空闲桩的充电站"},
            },
            search_poi,
        ),
        "plan_route": make_tool(
            "plan_route",
            "规划路线并开始导航，返回距离和预计时长。目的地有多个候选或找不到时返回 need_confirmation 和候选，"
            "不会开始导航；回家、去公司时地址从驾驶员偏好里取，没有时返回 need_info，请驾驶员提供地址。",
            {
                "destination": {"type": "string", "description": "目的地名称；回家、去公司直接写“家”或“公司”"},
                "preference": {
                    "type": "string",
                    "enum": list(ROUTE_PREFERENCES),
                    "description": "路线偏好：fastest 最快（默认）、avoid_highway 避开高速、avoid_toll 避开收费",
                },
                "waypoints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"途经点名称，按顺序，最多 {MAX_WAYPOINTS} 个",
                },
            },
            partial(plan_route, service),
            required=["destination"],
        ),
        "get_route_status": make_tool(
            "get_route_status",
            "查询当前导航：是否在导航、目的地、预计剩余时间和路况（示例数据）。",
            {},
            partial(get_route_status, service),
        ),
    }


def advisor_tools(service: VehicleService) -> Dict[str, AgentToolSpec]:
    """用车顾问的工具集合：故障灯查询、续航估算；用户手册检索由 build_rag_tools 提供。"""
    return {
        "lookup_warning": make_tool(
            "lookup_warning",
            "查询故障灯或报警提示的含义、颜色、严重度和建议动作（通用说明）。不传 signal 时查询车辆当前亮起的全部故障灯。",
            {
                "signal": {
                    "type": "string",
                    "description": "故障灯名称、报警提示或故障码，如“胎压报警”“电池过热”“ESP 故障”",
                },
            },
            partial(lookup_warning, service),
        ),
        "estimate_range": make_tool(
            "estimate_range",
            "按当前电量、续航和室外温度估算到达后的剩余电量和续航，并判断是否建议途中充电；结果仅为估算。"
            "destination 和 distance_km 二选一。",
            {
                "destination": {"type": "string", "description": "目的地名称；家、公司直接写“家”或“公司”"},
                "distance_km": {"type": "number", "description": "行驶距离（km）"},
            },
            partial(estimate_range, service),
        ),
    }


def cabin_tools(service: VehicleService) -> Dict[str, AgentToolSpec]:
    """座舱助手的工具集合：媒体、音量、电话（模拟）、天气、追问所需信息。"""
    return {
        "control_media": make_tool(
            "control_media",
            "控制音乐和电台播放：播放、暂停、下一首、上一首；播放某类内容时传 query（示例曲库）。返回当前播放的标题。",
            {
                "action": {
                    "type": "string",
                    "enum": list(MEDIA_ACTIONS),
                    "description": "play 播放、pause 暂停、next 下一首、previous 上一首",
                },
                "query": {"type": "string", "description": "想听的内容，如歌手、歌曲类型、电台、有声书"},
            },
            partial(control_media, service),
            required=["action"],
        ),
        "set_volume": make_tool(
            "set_volume",
            "调节媒体音量（0–30，超出会被裁剪）：level、delta、mute 三选一。返回调整后的音量。",
            {
                "level": {"type": "integer", "description": "目标音量 0–30"},
                "delta": {"type": "integer", "description": "相对调节，大一点为 3，小一点为 -3"},
                "mute": {"type": "boolean", "description": "true 静音（音量调到 0），false 取消静音"},
            },
            partial(set_volume, service),
        ),
        "make_call": make_tool(
            "make_call",
            "给通讯录联系人打电话（模拟，不真实拨号）。多个匹配时返回候选，请驾驶员确认；找不到时如实告知。",
            {"contact": {"type": "string", "description": "联系人名字或称呼，如“张伟”“老婆”"}},
            make_call,
            required=["contact"],
        ),
        "get_weather": make_tool(
            "get_weather",
            "查询天气（示例数据）。",
            {
                "city": {"type": "string", "description": "城市名，不传时用车辆当前所在城市"},
                "date": {"type": "string", "description": "日期，如今天、明天、周六；不传为今天"},
            },
            partial(get_weather, service),
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "根据当前指令的意图，给出下一轮需要向驾驶员追问的关键信息（如目的地、联系人）。",
            {},
            suggest_required_fields,
        ),
    }


def build_agent_tools(service: VehicleService) -> Dict[str, AgentToolSpec]:
    """四个 LLM 角色的全部专属工具（不含 RAG）；编排器构造时注册。"""
    tools: Dict[str, AgentToolSpec] = {}
    for factory in (vehicle_control_tools, navigation_tools, advisor_tools, cabin_tools):
        tools.update(factory(service))
    return tools


MANUAL_NOT_FOUND = "手册里没有找到相关内容，建议查看车机中的用户手册或联系售后。"


def build_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """构建检索车辆用户手册的 RAG 工具。"""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = int(args.get("top_k", 5) or 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        # 检索为空（知识库没有手册或没有相关片段）：search_with_rewrite 返回空列表
        data = getattr(result, "data", None)
        if isinstance(data, list) and not data:
            return {
                "success": True,
                "query": query,
                "top_k": top_k,
                "found": False,
                "message": MANUAL_NOT_FOUND,
                "results": [],
                "reranked": False,
            }
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", "知识库检索失败"),
                "results": [],
                "reranked": False,
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "检索车辆用户手册，返回最相关的文档片段；用于功能用法、充电与保养知识、指示灯说明等问题。",
            {
                "query": {"type": "string", "description": "驾驶员的问题或检索关键词"},
                "top_k": {"type": "integer", "description": "返回结果条数"},
            },
            search_knowledge_base,
            required=["query"],
        )
    }
