"""
CabinWeave 车载意图识别器：关键词层 + LLM 层分层识别

识别流程：
  1. LRU 缓存：清理后的消息前 200 字 + 最近 3 条历史做 md5 作为 key。
  2. 关键词层（同步、纯 Python，毫秒级，先执行）：
     - 紧急短语表（高精度）命中 → 直接判为 emergency_sos，不调用 LLM；
     - 意图词表（含同义词和常见同音错字）按命中数给出候选意图；
     - 按标点和连接词分句，片段落在 2 个以上意图时标记多意图，并生成候选子任务。
  3. LLM 层：prompt 含意图定义、边界规则、每类 2 条示例、最近 3 条历史；关闭思考模式
     （max_tokens 取不开思考所需的三倍，以后打开思考也有余量），
     模型以文本 JSON 输出 intent / reasoning / sub_tasks / execution；超时默认 5 秒。
  4. 决策：紧急短语 > LLM > 关键词兜底。置信度按两层是否一致给出
     （一致 high、不一致 medium、LLM 判为 other 为 low、只有关键词为 low、都没有为 none），
     不使用 LLM 自报的置信数字。

环境变量：
  CABINWEAVE_INTENT_MODEL       非空时覆盖构造参数 model（可单独配一个更快的小模型）
  CABINWEAVE_INTENT_TIMEOUT_S   LLM 超时秒数，默认 5.0
  CABINWEAVE_INTENT_CACHE_SIZE  LRU 缓存容量，默认 1000，0 表示不缓存
构造参数 timeout_s / cache_size 显式传入时优先于环境变量。

构造参数 use_keywords / use_llm 默认都为 True；评测的分层消融（E1）用它们构造
"只关键词"（等同 LLM 不可用）和"只 LLM"（没有紧急直达和关键词兜底，LLM 失败时判为 other）两个对照组。
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anthropic import AsyncAnthropic

from core.llm_utils import THINKING_DISABLED, extract_text_content
from core.text_normalize import remove_spaces, to_halfwidth

logger = logging.getLogger(__name__)


# ── 意图类别 ──────────────────────────────────────────────────────────────────

class IntentCategory(Enum):
    CLIMATE_CONTROL = "climate_control"  # 空调开关、温度、风量、除雾除霜、座椅/方向盘加热通风
    BODY_CONTROL    = "body_control"     # 车窗、天窗遮阳帘、门锁、尾门、后视镜、雨刮、灯光、座椅位置与按摩
    DRIVE_SETTING   = "drive_setting"    # 驾驶模式、能量回收、转向手感、驾驶辅助功能开关
    NAVIGATION      = "navigation"       # 设目的地、回家/去公司、路线偏好、途经点、路况、剩余时间
    POI_SEARCH      = "poi_search"       # 按类别找地点：充电站、停车场、服务区、维修点、餐厅
    VEHICLE_STATUS  = "vehicle_status"   # 电量续航、胎压读数、里程、保养里程、门窗状态
    FAULT_DIAGNOSIS = "fault_diagnosis"  # 故障灯、报警提示、异响、异常现象
    MANUAL_QA       = "manual_qa"        # 功能用法、充电与保养知识、冬季用车
    MEDIA_CONTROL   = "media_control"    # 音乐、电台、有声书、音量、切歌
    PHONE_CALL      = "phone_call"       # 打电话、发消息
    EMERGENCY_SOS   = "emergency_sos"    # 事故、起火冒烟、人员受伤、突发疾病
    ROADSIDE_ASSIST = "roadside_assist"  # 抛锚、亏电趴窝、爆胎、需要拖车
    CHITCHAT        = "chitchat"         # 问候、闲聊、天气等泛信息、情绪表达
    OTHER           = "other"            # 越界（如转人工、投诉）、无意义、缺上下文无法判断


# 安全类意图：主意图选取时优先，路由时只走安全节点
SAFETY_INTENTS: frozenset = frozenset({
    IntentCategory.EMERGENCY_SOS,
    IntentCategory.ROADSIDE_ASSIST,
})

# 意图含义（写进 LLM prompt）
INTENT_DEFINITIONS: Dict[IntentCategory, str] = {
    IntentCategory.CLIMATE_CONTROL: "空调开关、温度、风量、除雾除霜、座椅/方向盘加热通风",
    IntentCategory.BODY_CONTROL:    "车窗、天窗遮阳帘、门锁、尾门、后视镜、雨刮、灯光、座椅位置与按摩",
    IntentCategory.DRIVE_SETTING:   "驾驶模式、能量回收、转向手感、驾驶辅助功能开关",
    IntentCategory.NAVIGATION:      "设目的地、回家/去公司、路线偏好、途经点、路况、剩余时间",
    IntentCategory.POI_SEARCH:      "按类别找地点：充电站、停车场、服务区、维修点、餐厅",
    IntentCategory.VEHICLE_STATUS:  "电量续航、胎压读数、里程、保养里程、门窗状态",
    IntentCategory.FAULT_DIAGNOSIS: "故障灯、报警提示、异响、异常现象",
    IntentCategory.MANUAL_QA:       "功能用法、充电与保养知识、冬季用车",
    IntentCategory.MEDIA_CONTROL:   "音乐、电台、有声书、音量、切歌",
    IntentCategory.PHONE_CALL:      "打电话、发消息",
    IntentCategory.EMERGENCY_SOS:   "事故、起火冒烟、人员受伤、突发疾病",
    IntentCategory.ROADSIDE_ASSIST: "抛锚、亏电趴窝、爆胎、需要拖车",
    IntentCategory.CHITCHAT:        "问候、闲聊、天气等泛信息、情绪表达",
    IntentCategory.OTHER:           "越界（如转人工、投诉）、无意义、缺上下文无法判断",
}

# 易混淆对的标注约定，写进 LLM prompt；评测样本按同一约定标注
BOUNDARY_RULES: Tuple[str, ...] = (
    "座椅、方向盘的加热和通风算 climate_control（热舒适）；座椅位置、按摩算 body_control。",
    "问读数或状态（\"胎压多少\"）算 vehicle_status；出现报警、指示灯、异常现象（\"胎压报警了\"）算 fault_diagnosis。",
    "针对本车当前现象算 fault_diagnosis；一般知识或功能用法算 manual_qa。",
    "要求切换或开关某个驾驶功能算 drive_setting；问功能是什么、怎么用算 manual_qa。",
    "有明确目的地或路线操作算 navigation；按类别找地点算 poi_search（即使说\"导航去最近的充电站\"）。",
    "车辆无法行驶但无人身风险算 roadside_assist；有人身安全风险（事故、起火、受伤、疾病）算 emergency_sos。",
    "要人工、客服、投诉算 other（不提供转人工）；要救援、拖车算 roadside_assist。",
    "有明确社交或信息诉求算 chitchat；判断不出诉求算 other。",
    "一句多指令：主意图安全类优先，否则取第一个诉求；其余诉求另标为子任务。",
)

MAX_SUB_TASKS = 4
_STATUS_TEXT = {"failed": "失败", "timeout": "超时", "skipped": "未启用"}  # llm_status 的中文说明，用于 reasoning

# ── Few-shot 示例 ─────────────────────────────────────────────────────────────
# 每类 2 条，模拟语音识别后的口语文本（含语气词、省略和同音错字，如"空挑"=空调）。
# 注意：这些示例会进入 LLM prompt，不得复用为评测样本，否则评测结果会虚高。
FEW_SHOT_EXAMPLES: Dict[IntentCategory, Tuple[str, str]] = {
    IntentCategory.CLIMATE_CONTROL: ("空挑调到二十二度", "方向盘加热开一下，手都冻僵了"),
    IntentCategory.BODY_CONTROL:    ("副驾那边窗户开一半", "后背箱帮我打开"),
    IntentCategory.DRIVE_SETTING:   ("切到运动模式", "能量回收调成最强的那档"),
    IntentCategory.NAVIGATION:      ("倒航回家", "换条不堵的路，还要多久能到"),
    IntentCategory.POI_SEARCH:      ("附近有没有空闲的超充", "导航去最近的服务区"),
    IntentCategory.VEHICLE_STATUS:  ("现在还剩多少电，能跑多远", "四个轮子胎压多少啊"),
    IntentCategory.FAULT_DIAGNOSIS: ("仪表盘亮了个黄色感叹号是啥情况", "胎压报警了怎么回事"),
    IntentCategory.MANUAL_QA:       ("冬天续航掉得厉害有啥办法", "自适应巡航是干嘛的，咋用"),
    IntentCategory.MEDIA_CONTROL:   ("来首周杰伦的歌", "声音小一点，太吵了"),
    IntentCategory.PHONE_CALL:      ("给老婆打个电话", "发消息给张三说我晚点到"),
    IntentCategory.EMERGENCY_SOS:   ("刚被后车顶了一下，我脖子特别疼", "副驾的人突然没反应了"),
    IntentCategory.ROADSIDE_ASSIST: ("车趴窝了动不了，帮我叫个拖车", "右后轮扎胎了，瘪得厉害"),
    IntentCategory.CHITCHAT:        ("你好呀小助手", "明天上海会下雨吗"),
    IntentCategory.OTHER:           ("嗯那个就是", "帮我订张去北京的机票"),
}

# 多诉求示例：演示 sub_tasks 和 execution 的写法（同样不得复用为评测样本）
MULTI_INTENT_EXAMPLES: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    (
        "空调调到二十二度，再放首周杰伦的歌",
        {
            "intent": "climate_control",
            "reasoning": "两个独立指令：调温和放歌",
            "sub_tasks": [
                {"intent": "climate_control", "text": "空调调到二十二度"},
                {"intent": "media_control", "text": "放首周杰伦的歌"},
            ],
            "execution": "parallel",
        },
    ),
    (
        "看看电量够不够，够的话就导航去公司",
        {
            "intent": "vehicle_status",
            "reasoning": "先查电量，导航依赖查询结果",
            "sub_tasks": [
                {"intent": "vehicle_status", "text": "看看电量够不够"},
                {"intent": "navigation", "text": "够的话就导航去公司"},
            ],
            "execution": "sequential",
        },
    ),
    (
        "把音乐关了，右前轮爆胎了，帮我叫救援",
        {
            "intent": "roadside_assist",
            "reasoning": "安全类优先：爆胎需要救援，另有关音乐指令",
            "sub_tasks": [
                {"intent": "media_control", "text": "把音乐关了"},
                {"intent": "roadside_assist", "text": "右前轮爆胎了，帮我叫救援"},
            ],
            "execution": "parallel",
        },
    ),
)


# ── 关键词层：紧急短语 ────────────────────────────────────────────────────────
# 只放高精度短语，命中即直达安全节点、不调 LLM；
# "救命""着急""要死"这类泛化词会误伤（如"救命，好热"），不放在这里。
EMERGENCY_PHRASES: Tuple[str, ...] = (
    "车着火了", "着火了", "起火了", "电池起火", "冒烟了", "冒黑烟", "电池冒烟",
    "撞车了", "出车祸", "车祸了", "发生车祸", "追尾了", "被追尾", "撞人了", "撞到人了", "翻车了",
    "有人受伤", "受伤了", "流血了", "出血了",
    "晕倒了", "晕过去了", "昏迷了", "喘不上气", "喘不过气", "呼吸困难",
    "胸口疼", "胸口痛", "心脏病犯了", "心脏病发作",
    "掉水里了", "落水了", "被困车里", "困在车里",
)

# 否定或假设语境：出现在短语前面时不算紧急（"差点追尾了""万一着火了怎么办"）
_GUARD_WORDS: Tuple[str, ...] = (
    "没有", "差点", "差一点", "险些", "防止", "避免", "预防", "以防",
    "如果", "要是", "假如", "万一",
)
# 紧挨在短语前面才生效的单字否定（"没冒烟了""不是撞车了"之类）
_GUARD_CHARS = "没不未别"
_GUARD_WINDOW = 6
_PUNCT_RE = re.compile(r"[，,。；;！!？?、]")


# ── 关键词层：意图词表 ────────────────────────────────────────────────────────
# 除 other 外 13 个意图，各含同义词和常见语音识别同音错字（标注"同音"）。
# 匹配前文本统一做：全角转半角、去空格、小写；同一位置按最长词优先，避免
# "座椅加热"同时算进 climate 和 body。
INTENT_KEYWORDS: Dict[IntentCategory, Tuple[str, ...]] = {
    IntentCategory.CLIMATE_CONTROL: (
        "空调", "空挑", "空跳", "控调",  # 后三个为同音错字
        "冷气", "暖气", "暖风", "冷风", "制冷", "制热", "温度", "调温", "升温", "降温",
        "风量", "风速", "风力", "出风口", "吹脸", "吹脚",
        "除雾", "出雾", "除霜", "出霜", "去雾", "起雾",  # 出雾/出霜为同音
        "内循环", "外循环",
        "座椅加热", "坐椅加热", "座椅通风", "坐椅通风", "方向盘加热", "加热",
        "太热", "好热", "有点热", "热死", "闷热", "太冷", "好冷", "有点冷", "冷死",
    ),
    IntentCategory.BODY_CONTROL: (
        "车窗", "车床", "窗户", "开窗", "关窗", "天窗", "遮阳帘", "遮阳板",  # 车床为同音
        "门锁", "锁车", "锁门", "解锁", "车门", "尾门",
        "后备箱", "后背箱", "后备厢", "行李箱",  # 后背箱为同音
        "后视镜", "倒车镜", "反光镜", "雨刮", "雨刷", "雨挂",  # 雨挂为同音
        "大灯", "近光", "远光", "氛围灯", "分为灯", "阅读灯", "双闪", "雾灯",  # 分为灯为同音
        "车灯", "开灯", "关灯", "灯光",
        "座椅", "按摩", "靠背", "腿托",
    ),
    IntentCategory.DRIVE_SETTING: (
        "驾驶模式", "运动模式", "经济模式", "舒适模式", "节能模式", "雪地模式", "标准模式",
        "sport模式", "eco模式",
        "能量回收", "动能回收", "回收等级", "单踏板",
        "转向手感", "方向盘手感", "转向模式", "转向助力",
        "自适应巡航", "定速巡航", "巡航", "车道保持", "车道居中", "车道偏离",
        "辅助驾驶", "智能驾驶", "智驾", "自动驾驶", "领航辅助", "自动泊车",
        "esp", "车身稳定", "自动紧急制动", "aeb", "自动驻车", "陡坡缓降",
    ),
    IntentCategory.NAVIGATION: (
        "导航", "倒航", "导行",  # 后两个为同音
        "回家", "去公司", "到公司", "回公司",
        "路线", "换条路", "路况", "堵不堵", "堵车", "拥堵",
        "避开高速", "不走高速", "高速优先", "走高速", "途经", "途径", "目的地",
        "还有多久", "多久到", "还要多久", "怎么走", "带我去",
        "预计到达", "到达时间", "取消导航", "结束导航", "退出导航",
    ),
    IntentCategory.POI_SEARCH: (
        "充电站", "冲电站", "充电桩", "冲电桩", "超充", "快充站", "换电站",  # 冲电为同音
        "停车场", "停车位", "车位", "服务区",
        "维修点", "维修站", "修理厂", "修车", "4s店",
        "餐厅", "饭店", "餐馆", "吃饭", "美食", "咖啡",
        "厕所", "洗手间", "卫生间", "酒店", "加油站",
        "附近", "最近的", "哪里有", "哪儿有",
    ),
    IntentCategory.VEHICLE_STATUS: (
        "电量", "剩余电量", "还剩多少电", "还有多少电", "多少电", "快没电",
        "续航", "徐航",  # 徐航为同音
        "还能跑多远", "能跑多远", "能跑多少",
        "胎压", "台压",  # 台压为同音
        "里程", "公里数", "保养里程", "下次保养",
        "车门关好", "车窗关好", "门关好", "门窗", "车况", "电池温度", "车内温度",
    ),
    IntentCategory.FAULT_DIAGNOSIS: (
        "故障", "故障灯", "故障码", "报警", "警告灯", "警示灯", "指示灯",
        "亮灯", "灯亮了", "亮黄灯", "黄灯亮", "亮红灯", "红灯亮", "感叹号", "仪表",
        "异响", "响声", "怪声", "咔咔响", "嗡嗡响", "异味", "焦味", "烧焦",
        "抖动", "发抖", "抖得厉害", "跑偏", "漏液", "漏水",
        "刹车软", "胎压低", "胎压报警", "胎压灯", "胎压异常",
        "动力受限", "限功率", "报错", "坏了", "失灵", "不工作",
    ),
    IntentCategory.MANUAL_QA: (
        "怎么用", "咋用", "如何使用", "怎么使用", "使用方法", "什么意思",
        "是干什么的", "干嘛用的", "是干嘛的", "有什么用", "什么功能",
        "怎么设置", "怎么开启", "怎么打开", "怎么关闭", "怎么开",
        "说明书", "用户手册", "手册", "教程", "注意事项", "要注意",
        "充电", "冲电", "快充", "慢充", "预约充电", "充满", "充到",
        "保养", "保养周期", "多久保养", "电池保养",
        "冬季用车", "冬天用车", "冬天续航", "ota", "升级",
    ),
    IntentCategory.MEDIA_CONTROL: (
        "音乐", "放歌", "听歌", "放首", "来首", "来一首", "放一首", "唱首",
        "歌曲", "歌单", "专辑", "的歌",
        "播放", "暂停", "继续播放", "下一首", "上一首", "切歌", "换一首", "换首歌",
        "单曲循环", "随机播放",
        "电台", "广播", "收音机", "fm", "有声书", "听书", "播客", "相声", "评书",
        "音量", "声音大", "声音小", "声音调", "大声", "小声", "静音", "取消静音",
    ),
    IntentCategory.PHONE_CALL: (
        "打电话", "打个电话", "打店话", "拨打", "拨号", "呼叫", "电话",  # 打店话为同音
        "回电话", "接电话", "挂电话", "挂断", "打给",
        "发消息", "发短信", "发信息", "发微信", "回消息", "通讯录", "联系人",
    ),
    IntentCategory.EMERGENCY_SOS: (
        "着火", "起火", "冒烟", "车祸", "撞车", "撞人", "撞到人", "事故", "追尾", "翻车",
        "受伤", "流血", "出血", "晕倒", "昏迷", "晕过去", "心脏病",
        "喘不上气", "喘不过气", "呼吸困难", "胸口疼", "胸口痛",
        "急救", "救护车", "打120", "打110", "打119", "帮我报警", "报警电话", "sos",
        "落水", "被困",
    ),
    IntentCategory.ROADSIDE_ASSIST: (
        "抛锚", "趴窝", "没电了", "亏电", "电耗尽", "开不动", "走不了", "动不了",
        "爆胎", "扎胎", "轮胎破", "漏气", "瘪了",
        "拖车", "救援", "道路救援", "搭电", "无法启动", "启动不了", "掉沟里", "陷进去",
    ),
    IntentCategory.CHITCHAT: (
        "你好", "您好", "嗨", "哈喽", "hello", "早上好", "早安", "晚上好", "晚安",
        "谢谢", "多谢", "辛苦了", "你是谁", "你叫什么", "笑话",
        "天气", "下雨", "下雪", "气温",
        "无聊", "好累", "累了", "心情", "开心", "难过", "烦死了",
        "聊聊", "聊天", "陪我", "拜拜", "再见",
    ),
}

# 命中数相同时的优先级：安全类最先；
# 电话先于地点（"打电话给4S店"），地点先于导航（"导航去充电站"），
# 故障/问答先于车况和车控（"空调坏了""空调怎么用"），闲聊最后。
_KEYWORD_PRIORITY: Tuple[IntentCategory, ...] = (
    IntentCategory.EMERGENCY_SOS,
    IntentCategory.ROADSIDE_ASSIST,
    IntentCategory.FAULT_DIAGNOSIS,
    IntentCategory.PHONE_CALL,
    IntentCategory.POI_SEARCH,
    IntentCategory.NAVIGATION,
    IntentCategory.MANUAL_QA,
    IntentCategory.VEHICLE_STATUS,
    IntentCategory.CLIMATE_CONTROL,
    IntentCategory.BODY_CONTROL,
    IntentCategory.DRIVE_SETTING,
    IntentCategory.MEDIA_CONTROL,
    IntentCategory.CHITCHAT,
)
_PRIORITY_RANK: Dict[IntentCategory, int] = {cat: idx for idx, cat in enumerate(_KEYWORD_PRIORITY)}

# 分句：标点 + 连接词。"还有多久/多少/多远"里的"还有"、"再见"里的"再"不切
CLAUSE_CONNECTORS: Tuple[str, ...] = ("然后", "再", "顺便", "并且", "同时", "还有", "另外", "接着")
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?]|然后|顺便|并且|同时|还有(?!多)|另外|接着|再(?!见)")


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class SubTask:
    """一句多指令拆出来的子任务。"""
    intent: IntentCategory
    text:   str


@dataclass
class IntentResult:
    intent:           IntentCategory
    confidence_level: str                    # "high" / "medium" / "low" / "none"
    reasoning:        str
    latency_ms:       float
    sources:          Dict[str, Any] = field(default_factory=dict)  # 两层各自的判断与最终决策来源
    sub_tasks:        List[SubTask] = field(default_factory=list)   # 为空或至少 2 个
    execution:        str = "parallel"                              # "parallel" / "sequential"


@dataclass
class KeywordCount:
    """意图词表计数结果。"""
    intent: Optional[IntentCategory]
    hits:   List[str] = field(default_factory=list)                      # 命中的全部词（按出现顺序）
    counts: Dict[IntentCategory, int] = field(default_factory=dict)      # 各意图命中数


@dataclass
class KeywordAnalysis:
    """关键词层完整结果。"""
    intent:           Optional[IntentCategory]
    hits:             List[str]
    counts:           Dict[IntentCategory, int]                          # 整句各意图命中数
    emergency_phrase: Optional[str]
    multi_intent:     bool
    sub_tasks:        List[SubTask]


# 未启用关键词层时使用：没有候选、没有紧急短语、不拆分
_NO_KEYWORDS = KeywordAnalysis(
    intent=None, hits=[], counts={}, emergency_phrase=None, multi_intent=False, sub_tasks=[],
)


@dataclass
class _LLMOutcome:
    """LLM 层结果；status 为 ok / failed / timeout / skipped（未启用 LLM 层）。"""
    status:    str
    intent:    Optional[IntentCategory] = None
    reasoning: str = ""
    sub_tasks: List[SubTask] = field(default_factory=list)
    execution: str = "parallel"
    error:     str = ""


class LRUCache:
    """基于 OrderedDict 的 LRU 缓存：命中刷新顺序，超出容量淘汰最久未用的条目。"""

    def __init__(self, capacity: int):
        self.capacity = max(0, int(capacity))
        self._data: "OrderedDict[str, Any]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value: Any) -> None:
        if self.capacity <= 0:
            return
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def keys(self) -> List[str]:
        return list(self._data.keys())

    def clear(self) -> None:
        self._data.clear()

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)


# ── 环境变量 ──────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float, *, positive: bool = False) -> float:
    """读取可选浮点配置；非法值记录 warning 并使用默认值，不阻塞服务启动。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("忽略非法浮点配置 %s=%r", name, raw)
        return default
    if math.isnan(value) or math.isinf(value) or (positive and value <= 0):
        logger.warning("忽略非法浮点配置 %s=%r", name, raw)
        return default
    return value


def _env_int(name: str, default: int, *, non_negative: bool = False) -> int:
    """读取可选整数配置；非法值记录 warning 并使用默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("忽略非法整数配置 %s=%r", name, raw)
        return default
    if non_negative and value < 0:
        logger.warning("忽略非法整数配置 %s=%r", name, raw)
        return default
    return value

# ── 关键词层实现 ──────────────────────────────────────────────────────────────

def clean_text(value: Any) -> str:
    """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.encode("utf-8", errors="ignore").decode("utf-8")


def _normalize_for_match(text: str) -> str:
    """关键词匹配用的归一化：全角转半角、去空格、小写。不转中文数字，保留"下一首"等原词。"""
    return remove_spaces(to_halfwidth(clean_text(text))).lower()


def _build_index(groups: Dict[Any, Tuple[str, ...]]) -> Dict[str, Any]:
    """把"值 → 词列表"展开成"词 → 值"的索引；重复词保留第一个并记录 warning。"""
    index: Dict[str, Any] = {}
    for value, terms in groups.items():
        for term in terms:
            key = _normalize_for_match(term)
            if not key:
                continue
            if key in index and index[key] != value:
                logger.warning("词表重复：%s 已属于 %s，忽略 %s", key, index[key], value)
                continue
            index[key] = value
    return index


_KEYWORD_INDEX: Dict[str, IntentCategory] = _build_index(INTENT_KEYWORDS)

_Span = Tuple[int, int, str, Any]


def _scan_terms(text: str, index: Dict[str, Any]) -> List[_Span]:
    """
    在 text 中查找索引里的词，返回不重叠的匹配（起点、终点、词、值），按出现顺序排列。

    重叠时最长词优先，等长时靠前的优先，例如"座椅加热"只算 climate，不再算 body 的"座椅"。
    """
    candidates: List[_Span] = []
    for term, value in index.items():
        start = text.find(term)
        while start != -1:
            candidates.append((start, start + len(term), term, value))
            start = text.find(term, start + 1)
    candidates.sort(key=lambda span: (-(span[1] - span[0]), span[0]))

    taken = [False] * len(text)
    accepted: List[_Span] = []
    for start, end, term, value in candidates:
        if any(taken[start:end]):
            continue
        for idx in range(start, end):
            taken[idx] = True
        accepted.append((start, end, term, value))
    accepted.sort(key=lambda span: span[0])
    return accepted


def _is_guarded(text: str, pos: int) -> bool:
    """短语前面（同一分句内）有否定或假设词时视为不成立。"""
    window = text[max(0, pos - _GUARD_WINDOW):pos]
    parts = _PUNCT_RE.split(window)
    window = parts[-1] if parts else ""
    if any(word in window for word in _GUARD_WORDS):
        return True
    return bool(window) and window[-1] in _GUARD_CHARS


def find_guarded_phrase(text: str, phrases: Sequence[str]) -> Optional[str]:
    """在归一化文本中查找第一个不处于否定/假设语境的短语，找不到返回 None。"""
    norm = _normalize_for_match(text)
    for phrase in sorted(phrases, key=len, reverse=True):
        start = norm.find(phrase)
        while start != -1:
            if not _is_guarded(norm, start):
                return phrase
            start = norm.find(phrase, start + 1)
    return None


def find_emergency_phrase(text: str) -> Optional[str]:
    """紧急短语检测：命中返回短语本身。"""
    return find_guarded_phrase(text, EMERGENCY_PHRASES)


def count_keywords(text: str) -> KeywordCount:
    """
    意图词表计数：统计每个意图的命中词数，取命中最多的意图。

    命中数相同时按 _KEYWORD_PRIORITY 取优先级高的意图。
    """
    norm = _normalize_for_match(text)
    if not norm:
        return KeywordCount(None)
    spans = _scan_terms(norm, _KEYWORD_INDEX)
    if not spans:
        return KeywordCount(None)

    counts: Dict[IntentCategory, int] = {}
    for _, _, _, intent in spans:
        counts[intent] = counts.get(intent, 0) + 1
    best = max(counts, key=lambda cat: (counts[cat], -_PRIORITY_RANK.get(cat, len(_PRIORITY_RANK))))
    return KeywordCount(best, [span[2] for span in spans], counts)


def split_clauses(text: str) -> List[str]:
    """按标点（，,。；;！!？?）和连接词（然后、再、顺便……）切分，返回非空片段。"""
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(clean_text(text)) if part and part.strip()]


def _is_safety(intent: Optional[IntentCategory]) -> bool:
    return intent in SAFETY_INTENTS


def primary_from_sub_tasks(candidate: Optional[IntentCategory], sub_tasks: List[SubTask]) -> Optional[IntentCategory]:
    """关键词层拆出子任务时的主意图（与 6.2 一致）：安全类优先，否则取第一个诉求。"""
    if not sub_tasks:
        return candidate
    for task in sub_tasks:
        if _is_safety(task.intent):
            return task.intent
    if _is_safety(candidate):
        return candidate
    return sub_tasks[0].intent


def analyze_keywords(text: str) -> KeywordAnalysis:
    """
    关键词层：紧急短语 + 整句词表计数 + 分句多意图检测。

    分句后每个片段单独计数；没有命中的片段并入前一个片段（"把空调打开，调到22度"）。
    闲聊问候片段不单独成组，也并入相邻片段，避免"你好，打开空调"被拆成两个诉求。
    片段落在 2 个以上不同意图时 multi_intent 为真，候选子任务按意图合并
    （同意图片段用"，"拼接），最多 4 个；
    此时候选意图按 6.2 规则取子任务中的第一个安全类，否则取第一个子任务。
    """
    text = clean_text(text)
    emergency = find_emergency_phrase(text)
    overall = count_keywords(text)

    groups: "OrderedDict[IntentCategory, List[str]]" = OrderedDict()
    pending: List[str] = []
    last_intent: Optional[IntentCategory] = None
    for clause in split_clauses(text):
        clause_intent = count_keywords(clause).intent
        if clause_intent is None or clause_intent is IntentCategory.CHITCHAT:
            if last_intent is None:
                pending.append(clause)
            else:
                groups[last_intent].append(clause)
            continue
        if clause_intent not in groups:
            groups[clause_intent] = pending + [clause]
            pending = []
        else:
            groups[clause_intent].append(clause)
        last_intent = clause_intent

    multi_intent = len(groups) >= 2
    sub_tasks: List[SubTask] = []
    intent = overall.intent
    if multi_intent:
        sub_tasks = [SubTask(cat, "，".join(texts)) for cat, texts in groups.items()][:MAX_SUB_TASKS]
        intent = primary_from_sub_tasks(overall.intent, sub_tasks)

    return KeywordAnalysis(
        intent=intent,
        hits=overall.hits,
        counts=overall.counts,
        emergency_phrase=emergency,
        multi_intent=multi_intent,
        sub_tasks=sub_tasks,
    )


# ── LLM 层：prompt、解析与校验 ───────────────────────────────────────────────

_JSON_FORMAT_HINT = (
    '{"intent": "<意图值>", "reasoning": "<一句话说明>", '
    '"sub_tasks": [{"intent": "<意图值>", "text": "<原话片段>"}], "execution": "parallel"}'
)


def build_system_prompt() -> str:
    """组装意图识别的 system prompt（静态内容，便于服务端缓存）。"""
    intent_lines = "\n".join(
        f"- {cat.value}：{INTENT_DEFINITIONS[cat]}" for cat in IntentCategory
    )
    rule_lines = "\n".join(f"{idx}. {rule}" for idx, rule in enumerate(BOUNDARY_RULES, 1))
    example_lines = "\n".join(
        f'"{text}" → {cat.value}' for cat, texts in FEW_SHOT_EXAMPLES.items() for text in texts
    )
    multi_lines = "\n".join(
        f'"{text}" → {json.dumps(output, ensure_ascii=False)}' for text, output in MULTI_INTENT_EXAMPLES
    )
    return f"""你是车载语音助手 CabinWeave 的意图识别模块。
输入来自车内语音识别，可能有口语、省略、语气词和同音错字（例如"空挑"是"空调"、"倒航"是"导航"、"后背箱"是"后备箱"），请按读音和上下文理解真实意图。

## 意图类别（只能从以下 14 个值中选择）
{intent_lines}

## 边界规则
{rule_lines}

## 多个诉求
- 一句话里有两个以上相互独立的诉求时，才输出 sub_tasks：每项写明 intent 和对应的原话片段 text，最多 4 项；只有一个诉求时不要输出 sub_tasks。
- 主意图 intent：子任务里有安全类（emergency_sos、roadside_assist）时取第一个安全类，否则取第一个诉求。
- execution：后一个诉求依赖前一个的结果时为 sequential（如"电量够的话就导航过去"），否则为 parallel。

## 单诉求示例
{example_lines}

## 多诉求示例
{multi_lines}

## 输出
只输出一个 JSON 对象，不要输出其他文字，格式：{_JSON_FORMAT_HINT}
intent 和 sub_tasks 里的 intent 只能取上面 14 个值之一；reasoning 用一句话说明判断依据。"""


def _build_user_prompt(message: str, history: Optional[List[Dict[str, str]]]) -> str:
    """组装 user 消息：最近 3 条历史 + 当前语音识别文本。"""
    lines: List[str] = []
    recent = [item for item in (history or [])[-3:] if isinstance(item, dict)]
    if recent:
        lines.append("最近对话（用于理解省略和指代）：")
        for item in recent:
            role = clean_text(item.get("role", "user"))[:20] or "user"
            content = clean_text(item.get("content", ""))[:200]
            lines.append(f"  {role}: {content}")
    lines.append(f'当前语音识别文本："{clean_text(message)}"')
    return "\n".join(lines)


def parse_llm_payload(content: Any) -> Dict[str, Any]:
    """
    解析 LLM 响应内容：从文本块里截取第一个 "{" 到最后一个 "}" 之间的 JSON（思考块会被忽略）。
    解析失败抛异常。
    """
    blocks = content if isinstance(content, list) else ([content] if content else [])
    raw = extract_text_content(blocks)
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("LLM 输出中没有 JSON")
    data = json.loads(raw[start:end])
    if not isinstance(data, dict):
        raise ValueError("LLM 输出的 JSON 不是对象")
    return data


def _to_intent(value: Any) -> Optional[IntentCategory]:
    """把字符串转为意图枚举，非法值返回 None。"""
    if isinstance(value, IntentCategory):
        return value
    if not isinstance(value, str):
        return None
    try:
        return IntentCategory(value.strip().lower())
    except ValueError:
        return None


def reconcile_primary_intent(intent: IntentCategory, sub_tasks: List[SubTask]) -> IntentCategory:
    """
    按 6.2 规则校正主意图：
      - 子任务里有安全类 → 主意图取第一个安全类子任务；
      - 主意图本身是安全类 → 保持（安全优先，不被非安全子任务覆盖）；
      - 有子任务但主意图不在其中 → 取第一个子任务的意图。
    """
    if not sub_tasks:
        return intent
    for task in sub_tasks:
        if _is_safety(task.intent):
            return task.intent
    if _is_safety(intent):
        return intent
    if intent not in {task.intent for task in sub_tasks}:
        return sub_tasks[0].intent
    return intent


def validate_llm_payload(data: Dict[str, Any]) -> _LLMOutcome:
    """
    校验 LLM 输出：主意图非法按 other；子任务意图非法或文本为空就丢弃，最多保留 4 个，
    不足 2 个则置空；execution 只有在有子任务时才可能为 sequential；最后校正主意图。
    """
    intent = _to_intent(data.get("intent")) or IntentCategory.OTHER
    reasoning = clean_text(data.get("reasoning") or "").strip()[:300]

    sub_tasks: List[SubTask] = []
    raw_tasks = data.get("sub_tasks")
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task_intent = _to_intent(item.get("intent"))
            task_text = clean_text(item.get("text") or "").strip()
            if task_intent is None or not task_text:
                continue
            sub_tasks.append(SubTask(task_intent, task_text))
    sub_tasks = sub_tasks[:MAX_SUB_TASKS]
    if len(sub_tasks) < 2:
        sub_tasks = []

    execution = "sequential" if sub_tasks and data.get("execution") == "sequential" else "parallel"
    intent = reconcile_primary_intent(intent, sub_tasks)
    return _LLMOutcome(status="ok", intent=intent, reasoning=reasoning, sub_tasks=sub_tasks, execution=execution)



# ── 识别器 ────────────────────────────────────────────────────────────────────

class IntentRecognizer:
    """
    车载意图识别器：关键词层 + LLM 层分层识别。

    初始化时不加载任何本地模型；LLM 通过 Anthropic 兼容接口调用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        timeout_s: Optional[float] = None,
        cache_size: Optional[int] = None,
        use_keywords: bool = True,
        use_llm: bool = True,
    ):
        if not (use_keywords or use_llm):
            raise ValueError("关键词层和 LLM 层至少启用一个")
        self.use_keywords = use_keywords
        self.use_llm = use_llm
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)

        # 识别器可单独配模型（如更快的小模型）
        env_model = os.getenv("CABINWEAVE_INTENT_MODEL", "").strip()
        self.model = env_model or model

        if timeout_s is not None and timeout_s > 0:
            self.timeout_s = float(timeout_s)
        else:
            if timeout_s is not None:
                logger.warning("忽略非法 timeout_s=%r，改用环境变量或默认值", timeout_s)
            self.timeout_s = _env_float("CABINWEAVE_INTENT_TIMEOUT_S", 5.0, positive=True)
        capacity = (
            max(0, int(cache_size)) if cache_size is not None
            else _env_int("CABINWEAVE_INTENT_CACHE_SIZE", 1000, non_negative=True)
        )

        self._system_prompt = build_system_prompt()
        self._cache = LRUCache(capacity)
        self.cache_hits = 0
        self.cache_misses = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.cache_misses += 1

        t0 = time.perf_counter()   # 高精度计时（Windows 上 monotonic 精度约 16 ms）
        text = self._clean_text(message)
        kw = analyze_keywords(text) if self.use_keywords else _NO_KEYWORDS
        sources: Dict[str, Any] = {
            "keyword_intent": kw.intent.value if kw.intent else None,
            "keyword_counts": {cat.value: n for cat, n in kw.counts.items()},
            "keyword_hits": list(kw.hits),
            "keyword_sub_tasks": [{"intent": t.intent.value, "text": t.text} for t in kw.sub_tasks],
            "multi_intent_keyword": kw.multi_intent,
            "emergency_phrase": kw.emergency_phrase,
            "llm_intent": None,
            "llm_status": "skipped",
            "llm_error": "",
            "decided_by": "none",
        }

        sub_tasks: List[SubTask] = []
        execution = "parallel"
        if kw.emergency_phrase:
            # 1. 紧急短语直达，不调 LLM
            intent, level = IntentCategory.EMERGENCY_SOS, "high"
            reasoning = f"命中紧急短语「{kw.emergency_phrase}」，直接判为紧急求助"
            sources["decided_by"] = "emergency_rule"
        elif not text.strip():
            # 空输入不调 LLM
            intent, level = IntentCategory.OTHER, "none"
            reasoning = "输入为空"
        else:
            outcome = await self._llm_recognize(text, history) if self.use_llm else _LLMOutcome(status="skipped")
            sources["llm_status"] = outcome.status
            sources["llm_error"] = outcome.error
            if outcome.status == "ok" and outcome.intent is not None:
                # 2. 采用 LLM 结果，置信度看两层是否一致
                intent = outcome.intent
                sub_tasks, execution = outcome.sub_tasks, outcome.execution
                reasoning = outcome.reasoning
                sources["llm_intent"] = intent.value
                sources["decided_by"] = "llm"
                if intent is IntentCategory.OTHER:
                    level = "low"
                elif kw.intent is intent:
                    level = "high"
                else:
                    level = "medium"
            elif kw.intent is not None:
                # 3. LLM 失败、超时或未启用，用关键词候选；多意图时带上关键词拆出的子任务
                intent, level = kw.intent, "low"
                if kw.multi_intent and len(kw.sub_tasks) >= 2:
                    sub_tasks = list(kw.sub_tasks)
                reasoning = f"LLM {_STATUS_TEXT.get(outcome.status, outcome.status)}，采用关键词候选（命中：{'、'.join(kw.hits[:5])}）"
                sources["decided_by"] = "keyword_fallback"
            else:
                # 4. 两层都没有结果，交给座舱助手追问或提示稍后再试
                intent, level = IntentCategory.OTHER, "none"
                reasoning = f"LLM {_STATUS_TEXT.get(outcome.status, outcome.status)}且关键词无候选"
                sources["decided_by"] = "none"

        result = IntentResult(
            intent=intent,
            confidence_level=level,
            reasoning=reasoning,
            latency_ms=(time.perf_counter() - t0) * 1000,
            sources=sources,
            sub_tasks=sub_tasks,
            execution=execution,
        )

        # LLM 失败/超时的兜底结果不缓存，下次同样的话还会重试 LLM
        if sources["llm_status"] not in ("failed", "timeout"):
            self._cache.put(key, result)
        return result

    # ── LLM 层 ────────────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> _LLMOutcome:
        """调用 LLM 识别意图；任何异常都算失败，超时单独标记为 timeout。"""
        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1536,
            "temperature": 0.1,
            "extra_body": THINKING_DISABLED,  # 只输出一个短 JSON，不需要思考过程
            "system": self._system_prompt,
            "messages": [{"role": "user", "content": _build_user_prompt(message, history)}],
        }

        try:
            resp = await asyncio.wait_for(self.client.messages.create(**request), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            logger.warning("意图识别 LLM 超时（%.2fs）", self.timeout_s)
            return _LLMOutcome(status="timeout", error=f"超过 {self.timeout_s:g} 秒")
        except Exception as ex:
            logger.warning("意图识别 LLM 调用失败: %s", ex)
            return _LLMOutcome(status="failed", error=self._short_error(ex))

        try:
            data = parse_llm_payload(getattr(resp, "content", None))
            return validate_llm_payload(data)
        except Exception as ex:
            logger.warning("意图识别 LLM 输出解析失败: %s", ex)
            return _LLMOutcome(status="failed", error=self._short_error(ex))

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload: Dict[str, Any] = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
                if isinstance(item, dict)
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _short_error(ex: Exception) -> str:
        # sources 会随 API 响应返回，只保留异常类型；上游异常全文可能含服务地址，只写日志
        return type(ex).__name__

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        return clean_text(value)

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "capacity": self._cache.capacity,
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
