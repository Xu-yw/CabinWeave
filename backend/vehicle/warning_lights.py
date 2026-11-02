"""
CabinWeave 纯电车型故障灯表（lookup_warning 工具的数据）

确定性的通用说明，覆盖 11 项常见的仪表故障灯和报警提示；不对应任何品牌的官方说明，
编码 W01–W11 也只是示例编码，具体以车辆用户手册为准。

每项包含：
  code      示例编码
  name      名称（与车辆状态 warning_lights 里的写法一致，如"胎压报警"）
  aliases   常见叫法，用于匹配
  color     红色 / 黄色
  severity  紧急 / 严重 / 注意
  meaning   含义
  advice    建议动作：红色尽快在安全的地方停车，联系道路救援或售后；黄色可以谨慎行驶，尽快检修

查找规则（lookup_warning_light）：
  1. 去掉空白和标点、英文转小写后，与编码完全一致的优先
  2. 名称或别名是输入的子串（如"胎压报警了"包含"胎压报警"），或输入是名称或别名的子串（至少 2 个字），
     取匹配长度最长的一项；长度相同时前一种优先
  3. 都没有时用 difflib 做相似度兜底（阈值 0.75），处理语音识别的同音错字
  查不到返回 None。
"""
import copy
import difflib
import re
from typing import Any, Dict, List, Optional

WARNING_NOT_FOUND = "未收录这个故障提示，建议查看车辆用户手册或联系售后。"
WARNING_NOTE = "以上是通用说明，具体以车辆用户手册为准。"

# 严重度从高到低，用于排序
SEVERITY_ORDER = ("紧急", "严重", "注意")

# 太宽泛的输入不做反向匹配，避免"故障"命中第一项
_GENERIC_WORDS = frozenset({"故障", "报警", "警告", "故障灯", "报警灯", "警告灯", "指示灯", "系统"})

_SIMILARITY_CUTOFF = 0.75

_RED_ADVICE_TAIL = "联系道路救援或售后。"

WARNING_LIGHTS: List[Dict[str, Any]] = [
    {
        "code": "W01",
        "name": "胎压报警",
        "aliases": ["胎压", "胎压灯", "胎压低", "胎压异常", "胎压监测", "轮胎压力", "轮胎气压", "轮胎亏气", "tpms"],
        "color": "黄色",
        "severity": "注意",
        "meaning": "一个或多个轮胎气压偏低，或胎压监测系统异常。",
        "advice": "降低车速，避免急刹和急转，尽快到安全的地方检查胎压并补气；胎压持续下降或轮胎明显瘪了，请停车并联系道路救援。",
    },
    {
        "code": "W02",
        "name": "动力电池故障",
        "aliases": ["电池故障", "高压电池故障", "电池包故障", "电池系统故障", "动力电池异常", "bms故障"],
        "color": "红色",
        "severity": "严重",
        "meaning": "动力电池系统检测到故障，车辆可能限制功率，严重时无法继续行驶。",
        "advice": "尽快在安全的地方停车，不要继续长途行驶，" + _RED_ADVICE_TAIL,
    },
    {
        "code": "W03",
        "name": "动力电池过热",
        "aliases": ["电池过热", "电池温度过高", "电池温度高", "电池高温", "动力电池高温"],
        "color": "红色",
        "severity": "紧急",
        "meaning": "动力电池温度过高，存在热失控风险。",
        "advice": "立即在安全的地方停车并下电，人员下车远离车辆；如有冒烟、异味或异响，立即远离并拨打 119，再联系道路救援。",
    },
    {
        "code": "W04",
        "name": "电驱系统故障",
        "aliases": ["电机故障", "电驱故障", "驱动电机故障", "电机控制器故障", "驱动系统故障", "动力系统故障", "电机过热"],
        "color": "红色",
        "severity": "严重",
        "meaning": "驱动电机或电机控制器故障，车辆可能动力受限或失去动力。",
        "advice": "打开双闪，尽快在安全的地方停车，" + _RED_ADVICE_TAIL,
    },
    {
        "code": "W05",
        "name": "制动系统故障",
        "aliases": ["制动故障", "刹车故障", "刹车系统故障", "刹车故障灯", "制动警告灯", "制动液位低", "刹车油不足"],
        "color": "红色",
        "severity": "严重",
        "meaning": "制动液位过低或制动系统故障，制动效果可能变差。",
        "advice": "降低车速，与前车保持更远的距离，尽快在安全的地方停车，不要继续行驶，" + _RED_ADVICE_TAIL,
    },
    {
        "code": "W06",
        "name": "安全气囊故障",
        "aliases": ["气囊故障", "安全气囊灯", "气囊灯", "安全气囊", "srs"],
        "color": "红色",
        "severity": "严重",
        "meaning": "安全气囊系统故障，发生碰撞时气囊可能无法正常弹出。",
        "advice": "尽快在安全的地方停车检查，" + _RED_ADVICE_TAIL + "修好之前谨慎驾驶。",
    },
    {
        "code": "W07",
        "name": "ESP故障",
        "aliases": ["esp", "esc", "车身稳定", "车身稳定系统故障", "车身稳定故障", "防侧滑", "侧滑灯"],
        "color": "黄色",
        "severity": "注意",
        "meaning": "车身稳定系统故障或已关闭，湿滑路面和紧急避让时车辆更容易打滑。",
        "advice": "降低车速，避免急加速和急转向，尽快到售后检修。",
    },
    {
        "code": "W08",
        "name": "12V低压蓄电池电量低",
        "aliases": ["12v", "12v电池", "12v蓄电池", "低压蓄电池", "低压电池", "蓄电池", "小电瓶", "电瓶",
                    "蓄电池电量低", "电瓶没电", "电瓶亏电"],
        "color": "黄色",
        "severity": "注意",
        "meaning": "12V 低压蓄电池电量低或供电异常，严重时车辆可能无法上电。",
        "advice": "减少停车时的用电，尽快到售后检修或更换低压蓄电池。",
    },
    {
        "code": "W09",
        "name": "低电量",
        "aliases": ["电量低", "电量不足", "电量过低", "低电量提醒", "快没电", "续航不足"],
        "color": "黄色",
        "severity": "注意",
        "meaning": "动力电池剩余电量偏低。",
        "advice": "尽快就近充电，关闭不必要的用电设备，避免急加速和高速行驶。",
    },
    {
        "code": "W10",
        "name": "充电系统故障",
        "aliases": ["充电故障", "充不进电", "充不上电", "无法充电", "充电系统", "车载充电机故障", "充电口故障", "obc故障"],
        "color": "黄色",
        "severity": "注意",
        "meaning": "充电系统或车载充电机异常，可能无法正常充电。",
        "advice": "停止充电，检查充电枪是否插好，换一个充电桩再试；仍然充不进去，请尽快联系售后检修。",
    },
    {
        "code": "W11",
        "name": "功率受限",
        "aliases": ["乌龟灯", "动力受限", "动力限制", "限功率", "功率限制"],
        "color": "黄色",
        "severity": "注意",
        "meaning": "动力输出被限制，常见原因是电量过低，或动力电池温度过低、过高。",
        "advice": "降低车速，尽快充电或到安全的地方停车检查；持续亮起请联系售后。",
    },
]


def _normalize(text: Any) -> str:
    """去掉空白和标点，英文转小写，用于匹配。"""
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def _public(item: Dict[str, Any]) -> Dict[str, Any]:
    """对外返回的字段（不含别名），新的 dict。"""
    return {key: copy.deepcopy(value) for key, value in item.items() if key != "aliases"}


def lookup_warning_light(signal: Any) -> Optional[Dict[str, Any]]:
    """按故障灯名称、别名或示例编码查找，规则见模块说明；查不到返回 None。"""
    text = _normalize(signal)
    if not text:
        return None

    for item in WARNING_LIGHTS:
        if text == item["code"].lower():
            return _public(item)

    # 得分为（匹配长度, 是否正向匹配）：长度相同时，别名包含在输入里的优先于输入包含在别名里的
    best: Optional[Dict[str, Any]] = None
    best_score = (0, 0)
    reverse_allowed = len(text) >= 2 and text not in _GENERIC_WORDS
    for item in WARNING_LIGHTS:
        for alias in [item["name"]] + item["aliases"]:
            key = _normalize(alias)
            if key in text:
                score = (len(key), 1)
            elif reverse_allowed and text in key:
                score = (len(text), 0)
            else:
                continue
            if score > best_score:
                best, best_score = item, score
    if best is not None:
        return _public(best)

    keys: Dict[str, Dict[str, Any]] = {}
    for item in WARNING_LIGHTS:
        for alias in [item["name"]] + item["aliases"]:
            keys.setdefault(_normalize(alias), item)
    close = difflib.get_close_matches(text, list(keys), n=1, cutoff=_SIMILARITY_CUTOFF)
    return _public(keys[close[0]]) if close else None
