"""
CabinWeave 模拟外部服务

全部是确定性数据，不访问网络，保证演示和评测可复现：
  - 地图：当前位置固定在上海徐汇（示例），约 15 个虚构 POI（充电站、停车场、服务区、维修点、餐厅），
    名称都带"（示例）"；按类别 / 关键词检索，可按距离、空闲桩、功率排序，可只要有空闲桩的充电站；
    按目的地名查路线（距离 km、时长 min）。"家""公司"不在地图里（由驾驶员偏好提供），
    未知或有多个候选的目的地返回 need_confirmation
  - 天气：按城市名（及日期）做 md5 哈希得到固定的天气和温度区间，同一城市每次相同
  - 媒体：不编造真实曲目，只返回"{关键词}相关内容（示例曲库）"这类标题
  - 联系人：几个虚构联系人，号码打码，按名字或称呼查找

返回值都是新的 dict，调用方修改不影响模块内的数据。
"""
import copy
import hashlib
import re
from typing import Any, Dict, List, Optional

SAMPLE_MARK = "（示例）"

# 当前位置（与车辆默认状态的 location 一致）
CURRENT_LOCATION: Dict[str, str] = {"city": "上海", "address": "徐汇区漕溪北路（示例）"}

# 类别代码 → 播报名称
POI_CATEGORIES: Dict[str, str] = {
    "charging_station": "充电站",
    "parking": "停车场",
    "service_area": "服务区",
    "repair": "维修点",
    "restaurant": "餐厅",
}

# 类别的中文叫法 → 类别代码
_CATEGORY_ALIASES: Dict[str, str] = {
    "充电站": "charging_station", "充电桩": "charging_station", "充电": "charging_station", "超充": "charging_station",
    "停车场": "parking", "停车": "parking", "车位": "parking",
    "服务区": "service_area",
    "维修点": "repair", "维修": "repair", "修车": "repair", "售后": "repair", "保养": "repair",
    "餐厅": "restaurant", "饭店": "restaurant", "吃饭": "restaurant", "美食": "restaurant",
}

# 排序方式的别名 → 排序键
_SORT_ALIASES: Dict[str, str] = {
    "distance": "distance", "距离": "distance", "最近": "distance",
    "available": "available", "空闲": "available", "空闲桩": "available",
    "power": "power", "功率": "power", "快充": "power",
}


def _drive_minutes(distance_km: float) -> int:
    """确定性的行驶时长：15 km 以内按市区 30 km/h，超出部分按 80 km/h。"""
    city_km = min(distance_km, 15.0)
    highway_km = max(distance_km - 15.0, 0.0)
    return max(1, round(city_km / 30 * 60 + highway_km / 80 * 60))


def _poi(poi_id: str, name: str, category: str, address: str, distance_km: float, **extra: Any) -> Dict[str, Any]:
    item = {
        "id": poi_id,
        "name": name,
        "category": category,
        "category_name": POI_CATEGORIES[category],
        "address": address,
        "distance_km": distance_km,
        "duration_min": _drive_minutes(distance_km),
    }
    item.update(extra)
    return item


def _charger(poi_id: str, name: str, address: str, distance_km: float,
             available: int, total: int, power_kw: int) -> Dict[str, Any]:
    return _poi(
        poi_id, name, "charging_station", address, distance_km,
        available_piles=available, total_piles=total, power_kw=power_kw,
    )


POIS: List[Dict[str, Any]] = [
    _charger("cs01", "徐汇滨江超充站（示例）", "徐汇区龙腾大道（示例）", 2.8, 6, 12, 250),
    _charger("cs02", "漕河泾园区充电站（示例）", "徐汇区宜山路（示例）", 3.2, 0, 6, 120),
    _charger("cs03", "龙华中路快充站（示例）", "徐汇区龙华中路（示例）", 2.1, 3, 8, 160),
    _charger("cs04", "上海南站充电站（示例）", "徐汇区沪闵路（示例）", 4.6, 10, 20, 120),
    _charger("cs05", "虹桥枢纽超充站（示例）", "闵行区申虹路（示例）", 15.4, 12, 24, 480),
    _poi("pk01", "港汇广场地下停车场（示例）", "parking", "徐汇区虹桥路（示例）", 0.6),
    _poi("pk02", "漕溪路公共停车场（示例）", "parking", "徐汇区漕溪路（示例）", 0.9),
    _poi("pk03", "徐家汇公园停车场（示例）", "parking", "徐汇区肇嘉浜路（示例）", 1.3),
    _poi("sa01", "莘庄服务区（示例）", "service_area", "G60 沪昆高速（示例）", 18.5),
    _poi("sa02", "枫泾服务区（示例）", "service_area", "G60 沪昆高速（示例）", 58.0),
    _poi("rp01", "徐汇新能源汽车服务中心（示例）", "repair", "徐汇区桂平路（示例）", 4.2),
    _poi("rp02", "龙吴路汽车维修点（示例）", "repair", "徐汇区龙吴路（示例）", 6.3),
    _poi("rs01", "漕溪北路家常菜馆（示例）", "restaurant", "徐汇区漕溪北路（示例）", 0.4),
    _poi("rs02", "徐家汇美食广场（示例）", "restaurant", "徐汇区华山路（示例）", 1.0),
    _poi("rs03", "滨江咖啡简餐（示例）", "restaurant", "徐汇区龙腾大道（示例）", 3.0),
]

# 常用目的地：名称、别名、地址、距离
DESTINATIONS: List[Dict[str, Any]] = [
    {"name": "浦东国际机场（示例）", "aliases": ["浦东机场", "浦东国际机场"], "address": "浦东新区（示例）", "distance_km": 52.0},
    {"name": "虹桥国际机场（示例）", "aliases": ["虹桥机场", "虹桥国际机场"], "address": "长宁区（示例）", "distance_km": 14.0},
    {"name": "上海虹桥站（示例）", "aliases": ["虹桥站", "虹桥火车站"], "address": "闵行区（示例）", "distance_km": 15.0},
    {"name": "上海站（示例）", "aliases": ["上海站", "上海火车站"], "address": "静安区（示例）", "distance_km": 11.0},
    {"name": "上海南站（示例）", "aliases": ["上海南站", "南站"], "address": "徐汇区（示例）", "distance_km": 4.8},
    {"name": "陆家嘴中心商场（示例）", "aliases": ["陆家嘴商场", "陆家嘴中心"], "address": "浦东新区（示例）", "distance_km": 12.5},
]

# 由驾驶员偏好提供地址的地点，地图里没有
PROFILE_PLACES = ("家", "回家", "公司", "单位")

# 泛指的目的地叫法 → 需要从中选择的候选
_GENERIC_DESTINATIONS: Dict[str, List[str]] = {
    "机场": ["浦东国际机场（示例）", "虹桥国际机场（示例）"],
    "火车站": ["上海南站（示例）", "上海站（示例）", "上海虹桥站（示例）"],
    "高铁站": ["上海虹桥站（示例）", "上海站（示例）", "上海南站（示例）"],
}

CONTACTS: List[Dict[str, Any]] = [
    {"name": "张伟", "aliases": ["老张", "张总"], "relation": "同事", "phone": "138****1234"},
    {"name": "张敏", "aliases": ["敏敏"], "relation": "朋友", "phone": "135****8642"},
    {"name": "李娜", "aliases": ["老婆", "媳妇", "爱人"], "relation": "家人", "phone": "139****5678"},
    {"name": "王芳", "aliases": ["妈妈", "老妈", "妈"], "relation": "家人", "phone": "137****2468"},
    {"name": "刘洋", "aliases": ["小刘", "刘师傅"], "relation": "朋友", "phone": "136****1357"},
]

_WEATHER_CONDITIONS = ("晴", "多云", "阴", "小雨", "中雨", "雷阵雨", "小雪")


def _plain(text: Any) -> str:
    """去掉"（示例）"标记和空白，用于名称匹配。"""
    value = re.sub(r"\s+", "", str(text or ""))
    return value.replace(SAMPLE_MARK, "").replace("(示例)", "")


# ── 地图 ──────────────────────────────────────────────────────────────────────

def normalize_category(category: Any) -> Optional[str]:
    """类别代码或中文叫法 → 类别代码；认不出返回 None。"""
    text = _plain(category).lower()
    if text in POI_CATEGORIES:
        return text
    return _CATEGORY_ALIASES.get(text)


def search_poi(
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    sort_by: str = "distance",
    require_available: bool = False,
    limit: int = 5,
) -> Dict[str, Any]:
    """
    检索 POI。

    category：类别代码或中文叫法；keyword：在名称、地址、类别名里做子串匹配；
    sort_by：distance（默认）/ available（空闲桩多的在前）/ power（功率大的在前），也认中文叫法；
    require_available：为 True 时去掉没有空闲桩的充电站，其他类别不受影响。
    """
    code = None
    if category:
        code = normalize_category(category)
        if code is None:
            return {"success": False, "error": f"不支持的地点类别：{category}", "results": []}
    sort_key = _SORT_ALIASES.get(_plain(sort_by).lower() or "distance")
    if sort_key is None:
        return {"success": False, "error": f"不支持的排序方式：{sort_by}", "results": []}

    word = _plain(keyword)
    items = []
    for poi in POIS:
        if code and poi["category"] != code:
            continue
        if word and not any(word in _plain(poi[key]) for key in ("name", "address", "category_name")):
            continue
        if require_available and poi["category"] == "charging_station" and poi["available_piles"] <= 0:
            continue
        items.append(poi)

    if sort_key == "available":
        items.sort(key=lambda poi: (-poi.get("available_piles", -1), poi["distance_km"]))
    elif sort_key == "power":
        items.sort(key=lambda poi: (-poi.get("power_kw", -1), poi["distance_km"]))
    else:
        items.sort(key=lambda poi: poi["distance_km"])

    limit = max(1, int(limit or 5))
    return {
        "success": True,
        "origin": dict(CURRENT_LOCATION),
        "category": code,
        "keyword": word,
        "sort_by": sort_key,
        "total": len(items),
        "results": copy.deepcopy(items[:limit]),
    }


def _route(name: str, address: str, distance_km: float) -> Dict[str, Any]:
    return {
        "success": True,
        "origin": dict(CURRENT_LOCATION),
        "destination": name,
        "address": address,
        "distance_km": distance_km,
        "duration_min": _drive_minutes(distance_km),
    }


def _places() -> List[Dict[str, Any]]:
    """可规划路线的地点：常用目的地 + 全部 POI，统一成 name / aliases / address / distance_km。"""
    places = [dict(item) for item in DESTINATIONS]
    places += [
        {"name": poi["name"], "aliases": [], "address": poi["address"], "distance_km": poi["distance_km"]}
        for poi in POIS
    ]
    return places


def plan_route(destination: str) -> Dict[str, Any]:
    """
    按目的地名查路线。

    名称或别名完全一致 → 返回路线；泛指（如"火车站"）或模糊匹配（至少 2 个字）到多个 → need_confirmation
    并给出前 3 个候选；"家""公司"和匹配不到的 → need_confirmation，候选为空。
    """
    query = _plain(destination)
    if not query:
        return {"success": False, "need_confirmation": True, "reason": "请告诉我目的地。", "candidates": []}
    if query in PROFILE_PLACES:
        return {
            "success": False,
            "need_confirmation": True,
            "destination": query,
            "reason": f"地图里没有“{query}”的地址，请先告诉我具体地址。",
            "candidates": [],
        }
    places = _places()

    for place in places:
        if query == _plain(place["name"]) or query in place["aliases"]:
            return _route(place["name"], place["address"], place["distance_km"])

    if query in _GENERIC_DESTINATIONS:
        names = _GENERIC_DESTINATIONS[query]
        matched = [place for place in places if place["name"] in names]
    elif len(query) >= 2:
        matched = [
            place for place in places
            if query in _plain(place["name"]) or any(query in alias for alias in place["aliases"])
        ]
    else:
        matched = []
    if len(matched) == 1:
        place = matched[0]
        return _route(place["name"], place["address"], place["distance_km"])

    matched.sort(key=lambda place: place["distance_km"])
    candidates = [
        {"name": place["name"], "address": place["address"], "distance_km": place["distance_km"]}
        for place in matched[:3]
    ]
    reason = "找到多个地点，请确认要去哪一个。" if candidates else f"没有找到“{query}”，请再确认一下目的地。"
    return {"success": False, "need_confirmation": True, "destination": query, "reason": reason, "candidates": candidates}


# ── 天气 ──────────────────────────────────────────────────────────────────────

def get_weather(city: Optional[str] = None, date: Optional[str] = None) -> Dict[str, Any]:
    """按城市名（指定了非"今天"的日期时连同日期）做 md5 哈希，得到固定的天气和温度区间。"""
    city_name = _plain(city) or CURRENT_LOCATION["city"]
    day = _plain(date) or "今天"
    key = city_name if day == "今天" else f"{city_name}|{day}"
    digest = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)

    low = digest % 26 - 3                       # -3 ~ 22
    high = low + 4 + (digest // 26) % 7         # 比最低温高 4 ~ 10 度
    condition = _WEATHER_CONDITIONS[(digest // 182) % len(_WEATHER_CONDITIONS)]
    if "雪" in condition and low > 2:
        condition = "小雨"
    return {
        "success": True,
        "city": city_name,
        "date": day,
        "condition": condition,
        "temp_low": low,
        "temp_high": high,
        "note": "示例天气数据",
    }


# ── 媒体 ──────────────────────────────────────────────────────────────────────

def search_media(query: Optional[str] = None) -> Dict[str, Any]:
    """按关键词返回示例曲库里的标题，不编造真实曲目。"""
    word = str(query or "").strip()
    title = f"{word}相关内容（示例曲库）" if word else "每日推荐（示例曲库）"
    return {"success": True, "query": word, "title": title}


# ── 联系人 ────────────────────────────────────────────────────────────────────

def find_contact(query: str) -> Dict[str, Any]:
    """
    按名字或称呼查找联系人（号码已打码）。

    名字或称呼完全一致 → 返回该联系人；部分匹配到一个 → 返回该联系人；
    匹配到多个 → need_confirmation 并给出候选；匹配不到 → not_found。
    """
    word = _plain(query)
    if not word:
        return {"success": False, "need_confirmation": True, "reason": "请告诉我要打给谁。", "candidates": []}

    exact = [contact for contact in CONTACTS if word == contact["name"] or word in contact["aliases"]]
    matched = exact or [
        contact for contact in CONTACTS
        if word in contact["name"] or any(word in alias for alias in contact["aliases"])
    ]
    if len(matched) == 1:
        return {"success": True, "contact": copy.deepcopy(matched[0])}
    if matched:
        return {
            "success": False,
            "need_confirmation": True,
            "reason": "通讯录里有多个匹配的联系人，请确认要打给谁。",
            "candidates": copy.deepcopy(matched),
        }
    return {"success": False, "not_found": True, "reason": f"通讯录里没有找到“{word}”。"}
