"""
CabinWeave 快速通道

纯规则、不调模型：简单的车控 / 媒体指令整句命中白名单时，由编排器调用和 Agent 同一个工具函数
（同样过安全规则），再按工具结果套模板播报，全程 0 次模型调用。原则是宁可漏判，不能误判，
拿不准的说法一律交回完整链路。

匹配流程（FastPath.match）：
  1. 原文去掉空白后超过 20 字 → 不命中
  2. 全角转半角后含问号 → 不命中
  3. normalize_command 归一化（全角转半角、去空格、小写、去口头词、中文数字转阿拉伯数字），
     再去掉首尾标点（。.！!~，,、）；中间仍有标点或符号（如"打开空调，关闭车窗"）→ 不命中，
     夹在两个数字之间的小数点除外
  4. 含拒绝词（REJECT_WORDS：连接词、疑问、否定、指代/省略），任意出现即不命中
  5. 剩下的文本必须整句匹配（fullmatch）白名单里的某条正则，允许常见语序变体
     （打开空调 / 空调打开 / 把空调打开 / 开空调）和"把 / 将"前缀；按规则顺序取第一条命中的
  第 1–4 步由 reject_reason 给出拒绝原因，可单独测试。

白名单（规则 ID：说法示例 → 工具与参数 → 意图）：
  F01 空调开关：打开/关闭空调、开空调、空调关了 → set_climate power → climate_control
  F02 设定温度：温度调到22度、空调调到22度、温度设为22.5度 → set_climate temperature → climate_control
  F03 相对温度：温度调高2度、温度调低一点、空调调高两度、调高温度
      → set_climate temperature_delta（"一点"或不说幅度按 1 度）→ climate_control
  F04 设定风量：风量调到3档 → set_climate fan_level → climate_control
  F05 相对风量：风大一点、风量调小一点、风量加大 → set_climate fan_delta（±1）→ climate_control
  F06 除雾：打开/关闭前挡除雾、打开除雾 → set_climate defrost → climate_control
  F07 座椅加热开关：打开主驾座椅加热、关闭副驾座椅加热、打开座椅加热
      → set_climate seat_heat_zone + seat_heat_level（打开为 2 档、关闭为 0；不说位置为主驾，
        "主副驾 / 前排"为 all）→ climate_control
  F08 座椅加热档位：座椅加热调到2档、副驾座椅加热调到3档 → set_climate seat_heat_zone + seat_heat_level
      → climate_control
  F09 车窗：打开/关闭主驾车窗、关闭所有车窗、车窗开一半、车窗开到50%、打开后排车窗
      → set_body part=window + action + zone（不说位置为 fl 主驾）+ open_percent（"一半"为 50，
        超过 100 不命中）→ body_control
  F10 天窗：打开/关闭天窗 → set_body part=sunroof + action → body_control
  F11 后备箱：打开/关闭后备箱（尾门）→ set_body part=trunk + action → body_control
  F12 锁车：锁车、锁门 → set_body part=door_lock action=lock（解锁不进快速通道）→ body_control
  F13 氛围灯：打开/关闭氛围灯 → set_lighting type=ambient + action → body_control
  F14 双闪：打开/关闭双闪 → set_lighting type=hazard + action → body_control
  F15 播放控制：播放、继续播放、暂停、下一首、上一首、切歌 → control_media action（不带 query）→ media_control
  F16 设定音量：音量调到15 → set_volume level → media_control
  F17 相对音量：声音大一点、音量调小一点、大声点 → set_volume delta（±3）→ media_control
  F18 静音：静音、取消静音 → set_volume mute → media_control
  "温度高""风大""声音大"这类可能是抱怨的说法，只有带"一点 / 点 / 些"或"调高 / 调大"等动词时才算调节。
  带歌名或歌手的播放、解锁、导航、电话等白名单外指令一律不命中。

播报（FastPath.reply，按工具执行结果生成）：
  - 工具返回 error 或 unsupported → 返回 None，调用方视为未命中，回到完整链路
  - 有 blocked → 播报拒绝原因
  - 成功且触发调整类规则 S03 / S04 / S05 → 只播报 notice（notice 已说明调整后的结果）
  - 其他成功 → "好的，…"模板，后面接 notice（S07 提醒或空）；数值取执行后的实际值（先看 changes，
    没变化时读执行后的车辆状态），相对调节也说最终值；值没变（已经是该状态）时用同一模板

环境变量：
  CABINWEAVE_FAST_PATH   on / off，默认 on；off、0、false、no（不区分大小写）为关闭
"""
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from core.intent_recognizer import IntentCategory
from core.text_normalize import normalize_command, remove_spaces, to_halfwidth
from vehicle.state import get_path

logger = logging.getLogger(__name__)

FAST_PATH_ENV = "CABINWEAVE_FAST_PATH"
_OFF_VALUES = frozenset({"off", "0", "false", "no"})

# 原文去掉空白后的最大字数
MAX_CHARS = 20
# 归一化后去掉的首尾标点
_EDGE_PUNCT = "。.！!~，,、"
# 归一化后只允许汉字、字母、数字、%、℃；小数点只允许夹在两个数字之间
_DISALLOWED_RE = re.compile(r"[^一-鿿a-z0-9%℃.]|(?<!\d)\.|\.(?!\d)")

# 拒绝词：任意出现即不命中（宁可漏判）
REJECT_WORDS: Dict[str, Tuple[str, ...]] = {
    "连接词": ("然后", "再", "顺便", "并且", "同时", "和", "跟", "还有", "以及", "接着", "之后"),
    "疑问词": ("吗", "呢", "么", "怎么", "为什么", "多少", "几", "什么"),
    "否定词": ("不", "别", "没", "勿"),
    "指代/省略": ("它", "这个", "那个", "刚才", "还"),
}

# 调整类安全规则：触发时 notice 已说明调整后的结果，只播报 notice
ADJUST_RULES = frozenset({"S03", "S04", "S05"})


def fast_path_enabled() -> bool:
    """读取 CABINWEAVE_FAST_PATH：默认开启，off / 0 / false / no 为关闭。"""
    return os.getenv(FAST_PATH_ENV, "on").strip().lower() not in _OFF_VALUES


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class FastMatch:
    """一次快速通道命中：调用哪个工具、传什么参数，以及生成播报需要的信息。"""
    rule_id:   str                 # 规则 ID，如 F02
    name:      str                 # 规则类别，如"设定温度"
    tool_name: str                 # 工具注册表里的名字
    args:      Dict[str, Any]      # 工具参数
    intent:    IntentCategory      # 对应意图，编排器据此确定 agent_type
    label:     str = ""            # 播报里的对象名，如"主驾座椅加热""所有车窗"
    text:      str = ""            # 归一化后参与整句匹配的文本


class _Values:
    """播报取值：先取本次 changes 里的新值，没变化时读执行后的车辆状态。"""

    def __init__(self, result: Dict[str, Any], state: Optional[Dict[str, Any]]):
        self.result = result
        self._state = state or {}
        self._changes = {
            change["field"]: change.get("new")
            for change in result.get("changes") or []
            if isinstance(change, dict) and "field" in change
        }

    def get(self, path: str, default: Any = None) -> Any:
        if path in self._changes:
            return self._changes[path]
        return get_path(self._state, path, default)


Builder = Callable[[Dict[str, Optional[str]]], Optional[Tuple[Dict[str, Any], str]]]
Speaker = Callable[[FastMatch, _Values], str]


@dataclass(frozen=True)
class _Rule:
    """白名单规则：正则命中后由 build 生成（工具参数, 播报对象名），成功执行后由 speak 生成播报。"""
    rule_id:   str
    name:      str
    tool_name: str
    intent:    IntentCategory
    patterns:  Tuple["re.Pattern[str]", ...]
    build:     Builder        # 返回 None 表示不命中（如开度超过 100）
    speak:     Speaker        # 成功且未被调整时的"好的，…"模板


# ── 正则片段 ──────────────────────────────────────────────────────────────────

_PREFIX = r"(?:把|将)?"
_OPEN = r"打开|开启|开"
_CLOSE = r"关闭|关掉|关上|关了|关"
_SET = r"(?:调到|调至|调成|设为|设到|设成|设置为|设置到|设置成|开到)"
_LITTLE = r"(?:一点|一些|点)"
_LEVEL_UNIT = r"(?:档|挡|级)"


def _alt(words: Iterable[str]) -> str:
    """词表 → 正则分支，长词在前。"""
    return "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))


def _toggle(obj: str) -> Tuple["re.Pattern[str]", ...]:
    """开关类说法：动词在前（打开空调、关掉空调）或对象在前（空调打开、把空调关了）。"""
    verb = rf"(?:(?P<open>{_OPEN})|(?P<close>{_CLOSE}))"
    return re.compile(verb + obj), re.compile(_PREFIX + obj + verb)


_TEMP_OBJ = r"(?:空调温度|空调|温度)"
_TEMP_UNIT = r"(?:度|℃|摄氏度)"
_FAN_OBJ = r"(?:空调风量|风量|风速)"
_VOL_OBJ = r"(?:媒体音量|音乐音量|音量|声音)"

_SEAT_ZONES = {"主驾驶": "driver", "主驾": "driver", "副驾驶": "passenger", "副驾": "passenger",
               "主副驾": "all", "前排": "all"}
_SEAT_LABELS = {"driver": "主驾座椅加热", "passenger": "副驾座椅加热", "all": "主副驾座椅加热"}
_SEAT_OBJ = rf"(?P<zone>{_alt(_SEAT_ZONES)})?座椅加热"

# 车窗说法 → set_body 的 zone；不说位置时为 fl（主驾）
_WINDOW_ZONES = {
    "主驾驶": "fl", "主驾": "fl", "左前": "fl",
    "副驾驶": "fr", "副驾": "fr", "右前": "fr",
    "左后": "rl", "右后": "rr",
    "前排": "front", "后排": "rear",
    "左侧": "left", "左边": "left", "右侧": "right", "右边": "right",
    "所有": "all", "全部": "all", "全车": "all",
}
_WINDOW_LABELS = {
    "fl": "主驾车窗", "fr": "副驾车窗", "rl": "左后车窗", "rr": "右后车窗",
    "front": "前排车窗", "rear": "后排车窗", "left": "左侧车窗", "right": "右侧车窗", "all": "所有车窗",
}
# 播报开度时读的车窗：一个说法对应多扇时读第一扇（同一次命令开度相同）
_WINDOW_READ = {"front": "fl", "rear": "rl", "left": "fl", "right": "fr", "all": "fl"}
_WINDOW_OBJ = rf"(?P<zone>{_alt(_WINDOW_ZONES)})?(?:车窗|窗户)"


# ── 参数构造 ──────────────────────────────────────────────────────────────────

def _is_open(groups: Dict[str, Optional[str]]) -> bool:
    return groups.get("open") is not None


def _number(text: str) -> Any:
    """数字文本 → int 或 float（整数值转 int）。"""
    value = float(text)
    return int(value) if value.is_integer() else value


def _fmt(value: Any) -> str:
    """播报用的数值：整数值的 float 去掉小数点。"""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _sign(groups: Dict[str, Optional[str]]) -> int:
    return 1 if groups.get("up") is not None else -1


def _build_temp_delta(groups: Dict[str, Optional[str]]) -> Optional[Tuple[Dict[str, Any], str]]:
    amount = _number(groups["amt"]) if groups.get("amt") else 1
    if amount == 0:
        return None
    return {"temperature_delta": _sign(groups) * amount}, "空调"


def _build_seat(groups: Dict[str, Optional[str]], level: int) -> Tuple[Dict[str, Any], str]:
    zone = _SEAT_ZONES.get(groups.get("zone") or "", "driver")
    return {"seat_heat_zone": zone, "seat_heat_level": level}, _SEAT_LABELS[zone]


def _build_window(groups: Dict[str, Optional[str]]) -> Optional[Tuple[Dict[str, Any], str]]:
    zone = _WINDOW_ZONES.get(groups.get("zone") or "", "fl")
    args: Dict[str, Any] = {"part": "window", "zone": zone}
    if groups.get("pct") is not None or groups.get("half") is not None:
        percent = 50 if groups.get("half") else int(groups["pct"])
        if percent > 100:
            return None
        args.update(action="open", open_percent=percent)
    else:
        args["action"] = "open" if _is_open(groups) else "close"
    return args, _WINDOW_LABELS[zone]


def _build_media(groups: Dict[str, Optional[str]]) -> Tuple[Dict[str, Any], str]:
    action = next(name for name in ("play", "pause", "next", "previous") if groups.get(name) is not None)
    return {"action": action}, "媒体"


# ── 播报模板 ──────────────────────────────────────────────────────────────────

def _on_off(on: bool) -> str:
    return "已打开" if on else "已关闭"


def _opening(percent: Any) -> str:
    """开度播报：0 为已关闭，100 为已打开，其余说开到多少。"""
    if percent == 0:
        return "已关闭"
    if percent == 100:
        return "已打开"
    return f"已开到{_fmt(percent)}%"


def _speak_temperature(match: FastMatch, values: _Values) -> str:
    return f"好的，空调已调到{_fmt(values.get('climate.temp'))}度。"


def _speak_fan(match: FastMatch, values: _Values) -> str:
    return f"好的，风量已调到{_fmt(values.get('climate.fan_level'))}档。"


def _speak_seat(match: FastMatch, values: _Values) -> str:
    zone = match.args["seat_heat_zone"]
    level = values.get(f"seats.seat_heat.{'driver' if zone == 'all' else zone}")
    if level == 0:
        return f"好的，{match.label}已关闭。"
    return f"好的，{match.label}已调到{_fmt(level)}档。"


def _speak_window(match: FastMatch, values: _Values) -> str:
    zone = match.args["zone"]
    percent = values.get(f"body.windows.{_WINDOW_READ.get(zone, zone)}")
    return f"好的，{match.label}{_opening(percent)}。"


def _speak_media(match: FastMatch, values: _Values) -> str:
    action = match.args["action"]
    if action == "pause":
        return "好的，已暂停播放。"
    if action == "next":
        return "好的，已切到下一首。"
    if action == "previous":
        return "好的，已切到上一首。"
    title = values.result.get("title") or values.get("media.title")
    return f"好的，正在播放{title}。" if title else "好的，已开始播放。"


def _volume(values: _Values) -> Any:
    return values.get("media.volume", values.result.get("volume"))


def _speak_volume(match: FastMatch, values: _Values) -> str:
    volume = _volume(values)
    return "好的，已静音。" if volume == 0 else f"好的，音量已调到{_fmt(volume)}。"


def _speak_mute(match: FastMatch, values: _Values) -> str:
    if match.args["mute"]:
        return "好的，已静音。"
    return f"好的，已取消静音，音量为{_fmt(_volume(values))}。"


# ── 白名单 ────────────────────────────────────────────────────────────────────

C = IntentCategory

RULES: Tuple[_Rule, ...] = (
    _Rule(
        "F01", "空调开关", "set_climate", C.CLIMATE_CONTROL,
        _toggle("空调"),
        lambda g: ({"power": _is_open(g)}, "空调"),
        lambda m, v: f"好的，空调{_on_off(m.args['power'])}。",
    ),
    _Rule(
        "F02", "设定温度", "set_climate", C.CLIMATE_CONTROL,
        (re.compile(rf"{_PREFIX}{_TEMP_OBJ}{_SET}(?P<num>\d+(?:\.\d+)?){_TEMP_UNIT}?"),),
        lambda g: ({"temperature": _number(g["num"])}, "空调"),
        _speak_temperature,
    ),
    _Rule(
        "F03", "相对温度", "set_climate", C.CLIMATE_CONTROL,
        (
            # 带动词：温度调高、温度调低一点、空调调高2度
            re.compile(
                rf"{_PREFIX}{_TEMP_OBJ}(?:(?P<up>调高|升高)|(?P<down>调低|降低))"
                rf"(?:(?P<amt>\d+(?:\.\d+)?){_TEMP_UNIT}|{_LITTLE})?"
            ),
            # 不带动词时必须说"一点 / 点 / 些"：温度高一点（"温度高"可能是抱怨，不算）
            re.compile(rf"{_PREFIX}{_TEMP_OBJ}(?:(?P<up>高)|(?P<down>低)){_LITTLE}"),
            # 动词在前：调高温度
            re.compile(r"(?:(?P<up>调高|升高)|(?P<down>调低|降低))(?:空调温度|温度)"),
        ),
        _build_temp_delta,
        _speak_temperature,
    ),
    _Rule(
        "F04", "设定风量", "set_climate", C.CLIMATE_CONTROL,
        (re.compile(rf"{_PREFIX}{_FAN_OBJ}{_SET}(?P<num>\d+){_LEVEL_UNIT}?"),),
        lambda g: ({"fan_level": int(g["num"])}, "风量"),
        _speak_fan,
    ),
    _Rule(
        "F05", "相对风量", "set_climate", C.CLIMATE_CONTROL,
        (
            # 带动词：风量加大、风量调小一点
            re.compile(
                rf"{_PREFIX}(?:{_FAN_OBJ}|风)(?:(?P<up>调大|开大|加大|增大)|(?P<down>调小|开小|减小)){_LITTLE}?"
            ),
            # 不带动词时必须说"一点 / 点 / 些"：风大一点（"风大"可能是抱怨，不算）
            re.compile(rf"{_PREFIX}(?:{_FAN_OBJ}|风)(?:(?P<up>大)|(?P<down>小)){_LITTLE}"),
            # 动词在前：加大风量
            re.compile(rf"(?:(?P<up>调大|开大|加大|增大)|(?P<down>调小|开小|减小)){_FAN_OBJ}"),
        ),
        lambda g: ({"fan_delta": _sign(g)}, "风量"),
        _speak_fan,
    ),
    _Rule(
        "F06", "除雾", "set_climate", C.CLIMATE_CONTROL,
        _toggle(r"(?:前挡风玻璃|前挡风|前风挡|前挡)?(?:除雾|除霜)"),
        lambda g: ({"defrost": _is_open(g)}, "前挡除雾"),
        lambda m, v: f"好的，前挡除雾{_on_off(m.args['defrost'])}。",
    ),
    _Rule(
        "F07", "座椅加热开关", "set_climate", C.CLIMATE_CONTROL,
        _toggle(_SEAT_OBJ),
        lambda g: _build_seat(g, 2 if _is_open(g) else 0),
        _speak_seat,
    ),
    _Rule(
        "F08", "座椅加热档位", "set_climate", C.CLIMATE_CONTROL,
        (re.compile(rf"{_PREFIX}{_SEAT_OBJ}{_SET}(?P<num>\d+){_LEVEL_UNIT}?"),),
        lambda g: _build_seat(g, int(g["num"])),
        _speak_seat,
    ),
    _Rule(
        "F09", "车窗", "set_body", C.BODY_CONTROL,
        _toggle(_WINDOW_OBJ) + (
            # 开一半 / 开到50%
            re.compile(rf"{_PREFIX}{_WINDOW_OBJ}(?:打开|开)(?:到|至)?(?:(?P<pct>\d+)%|(?P<half>一半))"),
        ),
        _build_window,
        _speak_window,
    ),
    _Rule(
        "F10", "天窗", "set_body", C.BODY_CONTROL,
        _toggle("天窗"),
        lambda g: ({"part": "sunroof", "action": "open" if _is_open(g) else "close"}, "天窗"),
        lambda m, v: f"好的，天窗{_opening(v.get('body.sunroof'))}。",
    ),
    _Rule(
        "F11", "后备箱", "set_body", C.BODY_CONTROL,
        _toggle(r"(?P<part>后备箱|后备厢|尾门)"),
        lambda g: (
            {"part": "trunk", "action": "open" if _is_open(g) else "close"},
            "尾门" if g.get("part") == "尾门" else "后备箱",
        ),
        lambda m, v: f"好的，{m.label}{_on_off(m.args['action'] == 'open')}。",
    ),
    _Rule(
        "F12", "锁车", "set_body", C.BODY_CONTROL,
        (re.compile(r"锁车|锁门|锁车门|锁上车门|(?:把|将)?车门锁上|车门上锁"),),
        lambda g: ({"part": "door_lock", "action": "lock"}, "车门"),
        lambda m, v: "好的，车门已上锁。",
    ),
    _Rule(
        "F13", "氛围灯", "set_lighting", C.BODY_CONTROL,
        _toggle("氛围灯"),
        lambda g: ({"type": "ambient", "action": "on" if _is_open(g) else "off"}, "氛围灯"),
        lambda m, v: f"好的，氛围灯{_on_off(m.args['action'] == 'on')}。",
    ),
    _Rule(
        "F14", "双闪", "set_lighting", C.BODY_CONTROL,
        _toggle(r"(?:双闪灯|双闪|危险报警灯)"),
        lambda g: ({"type": "hazard", "action": "on" if _is_open(g) else "off"}, "双闪"),
        lambda m, v: f"好的，双闪{_on_off(m.args['action'] == 'on')}。",
    ),
    _Rule(
        "F15", "播放控制", "control_media", C.MEDIA_CONTROL,
        # 归一化会把"下一首"转成"下1首"，两种写法都认
        (re.compile(
            r"(?P<play>播放|继续播放|恢复播放|开始播放|播放音乐|继续播放音乐)"
            r"|(?P<pause>暂停|暂停播放|暂停音乐|停止播放)"
            r"|(?P<next>下[1一]首|下[1一]曲|切歌|换[1一]首|换首歌)"
            r"|(?P<previous>上[1一]首|上[1一]曲)"
        ),),
        _build_media,
        _speak_media,
    ),
    _Rule(
        "F16", "设定音量", "set_volume", C.MEDIA_CONTROL,
        (re.compile(rf"{_PREFIX}{_VOL_OBJ}{_SET}(?P<num>\d+)格?"),),
        lambda g: ({"level": int(g["num"])}, "音量"),
        _speak_volume,
    ),
    _Rule(
        "F17", "相对音量", "set_volume", C.MEDIA_CONTROL,
        (
            # 带动词：音量调小一点、声音调大
            re.compile(
                rf"{_PREFIX}{_VOL_OBJ}(?:(?P<up>调大|开大|加大|增大|调高|提高)"
                rf"|(?P<down>调小|开小|减小|调低|降低)){_LITTLE}?"
            ),
            # 不带动词时必须说"一点 / 点 / 些"：声音大一点（"声音大"可能是抱怨，不算）
            re.compile(rf"{_PREFIX}{_VOL_OBJ}(?:(?P<up>大|高)|(?P<down>小|低)){_LITTLE}"),
            # 动词在前：调大音量
            re.compile(rf"(?:(?P<up>调大|加大|增大|调高|提高)|(?P<down>调小|减小|调低|降低)){_VOL_OBJ}"),
            # 大声点、小点声
            re.compile(rf"(?P<up>大声{_LITTLE}|大点声)|(?P<down>小声{_LITTLE}|小点声)"),
        ),
        lambda g: ({"delta": 3 * _sign(g)}, "音量"),
        _speak_volume,
    ),
    _Rule(
        "F18", "静音", "set_volume", C.MEDIA_CONTROL,
        (re.compile(r"(?P<mute>静音|开启静音|打开静音)|(?P<unmute>取消静音|关闭静音|解除静音)"),),
        lambda g: ({"mute": g.get("mute") is not None}, "音量"),
        _speak_mute,
    ),
)

_RULES_BY_ID: Dict[str, _Rule] = {rule.rule_id: rule for rule in RULES}


# ── 拒绝判断 ──────────────────────────────────────────────────────────────────

def _screen(message: Any) -> Tuple[Optional[str], str]:
    """匹配流程第 1–4 步：返回（拒绝原因或 None, 归一化并去掉首尾标点后的文本）。"""
    raw = remove_spaces(str(message or ""))
    if not raw:
        return "空输入", ""
    if len(raw) > MAX_CHARS:
        return f"超过{MAX_CHARS}字", ""
    if "?" in to_halfwidth(raw):
        return "含问号", ""
    text = normalize_command(raw).strip(_EDGE_PUNCT)
    if not text:
        return "只有口头词或标点", ""
    if _DISALLOWED_RE.search(text):
        return "中间有标点或符号", text
    for category, words in REJECT_WORDS.items():
        for word in sorted(words, key=len, reverse=True):
            if word in text:
                return f"含{category}「{word}」", text
    return None, text


def reject_reason(message: Any) -> Optional[str]:
    """拒绝判断（匹配流程第 1–4 步）：返回拒绝原因，通过时返回 None。不判断是否在白名单里。"""
    return _screen(message)[0]


# ── 快速通道 ──────────────────────────────────────────────────────────────────

class FastPath:
    """快速通道：match 判断是否命中白名单，reply 按工具执行结果生成播报。"""

    def __init__(self, enabled: Optional[bool] = None):
        # 不传时读环境变量；运行中也可以直接改 enabled
        self.enabled = fast_path_enabled() if enabled is None else bool(enabled)

    def match(self, message: Any) -> Optional[FastMatch]:
        """整句命中白名单时返回 FastMatch；未开启、被拒绝或不在白名单里时返回 None。"""
        if not self.enabled:
            return None
        reason, text = _screen(message)
        if reason is not None:
            logger.debug("快速通道未命中（%s）：%s", reason, message)
            return None
        for rule in RULES:
            for pattern in rule.patterns:
                found = pattern.fullmatch(text)
                if found is None:
                    continue
                built = rule.build(found.groupdict())
                if built is None:
                    logger.debug("快速通道规则 %s 的参数不合理，不命中：%s", rule.rule_id, message)
                    return None
                args, label = built
                return FastMatch(
                    rule_id=rule.rule_id,
                    name=rule.name,
                    tool_name=rule.tool_name,
                    args=args,
                    intent=rule.intent,
                    label=label,
                    text=text,
                )
        logger.debug("快速通道未命中（不在白名单）：%s", message)
        return None

    @staticmethod
    def reply(match: FastMatch, result: Any, state: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        按工具结果生成播报；state 为执行后的车辆状态。

        返回 None 表示不能模板播报（error、unsupported 或结果格式不对），调用方应回到完整链路。
        """
        if not isinstance(result, dict):
            return None
        if result.get("error") or result.get("unsupported") is True:
            return None
        if result.get("blocked") is True:
            return str(result.get("reason") or "").strip() or None
        if result.get("success") is not True:
            return None
        notice = str(result.get("notice") or "").strip()
        if notice and ADJUST_RULES.intersection(result.get("rule_ids") or []):
            return notice
        rule = _RULES_BY_ID.get(match.rule_id)
        if rule is None:
            return None
        return rule.speak(match, _Values(result, state)) + notice
