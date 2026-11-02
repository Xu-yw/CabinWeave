"""
CabinWeave 意图识别测试：文本归一化、关键词层、LLM 输出解析与校验、
分层决策（紧急短语 / LLM / 关键词兜底）、置信度、层开关（评测消融用）和 LRU 缓存。

全部使用假客户端，不访问网络、不调用真实模型。
"""
import asyncio
import dataclasses
import json
import logging
from types import SimpleNamespace

import pytest

from core.intent_recognizer import (
    EMERGENCY_PHRASES,
    FEW_SHOT_EXAMPLES,
    INTENT_KEYWORDS,
    IntentCategory,
    IntentRecognizer,
    IntentResult,
    LRUCache,
    SAFETY_INTENTS,
    SubTask,
    analyze_keywords,
    build_system_prompt,
    find_emergency_phrase,
    parse_llm_payload,
    count_keywords,
    validate_llm_payload,
)
from core.text_normalize import (
    chinese_to_arabic,
    normalize_command,
    remove_fillers,
    remove_spaces,
    to_halfwidth,
)


# ── 测试工具 ──────────────────────────────────────────────────────────────────

class FakeClient:
    """假 LLM 客户端：记录调用参数，可返回固定响应、抛出异常或模拟慢响应。"""

    def __init__(self, response=None, error=None, delay=0.0):
        self.response = response
        self.error = error
        self.delay = delay
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def text_response(text):
    """模拟思考模型的响应：先是思考块，再是文本块。"""
    return SimpleNamespace(content=[
        SimpleNamespace(type="thinking", thinking="先判断意图类别"),
        SimpleNamespace(type="text", text=text),
    ])


def json_response(**payload):
    """模拟以文本 JSON 输出识别结果的响应。"""
    return text_response(json.dumps(payload, ensure_ascii=False))


def make_recognizer(client, **kwargs):
    recognizer = IntentRecognizer(api_key="test-key", model="test-model", **kwargs)
    recognizer.client = client
    return recognizer


def recognize(recognizer, message, history=None):
    return asyncio.run(recognizer.recognize(message, history=history))


@pytest.fixture(autouse=True)
def _clear_intent_env(monkeypatch):
    """隔离外部环境变量，保证测试结果稳定。"""
    for name in (
        "CABINWEAVE_INTENT_MODEL",
        "CABINWEAVE_INTENT_TIMEOUT_S",
        "CABINWEAVE_INTENT_CACHE_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)


# ── 接口契约 ──────────────────────────────────────────────────────────────────

def test_intent_categories_contract():
    assert len(IntentCategory) == 14
    assert SAFETY_INTENTS == {IntentCategory.EMERGENCY_SOS, IntentCategory.ROADSIDE_ASSIST}


def test_intent_result_fields_contract():
    names = [f.name for f in dataclasses.fields(IntentResult)]
    assert names == [
        "intent", "confidence_level",
        "reasoning", "latency_ms", "sources", "sub_tasks", "execution",
    ]


# ── 文本归一化 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("二十二", "22"),
        ("空调调到二十二度", "空调调到22度"),
        ("座椅加热调到两档", "座椅加热调到2档"),
        ("调高俩度", "调高2度"),
        ("十五", "15"),
        ("一百二十", "120"),
        ("一百二", "120"),
        ("一百零五", "105"),
        ("二十二点五度", "22.5度"),
        ("车窗开到百分之三十", "车窗开到30%"),
        ("两点半出发", "2点半出发"),
        # 不该转换的说法保持原样
        ("车窗开一半", "车窗开一半"),
        ("等一下", "等一下"),
        ("统一", "统一"),
        ("调高一点", "调高一点"),
        ("调高两三度", "调高两三度"),
    ],
)
def test_chinese_to_arabic(text, expected):
    assert chinese_to_arabic(text) == expected


def test_remove_fillers_and_width_helpers():
    assert remove_fillers("请帮我把空调打开一下吧") == "把空调打开"
    assert remove_fillers("麻烦给我放首歌呀") == "放首歌"
    assert remove_fillers("开窗呗啊") == "开窗"
    # 保护词不被误删
    assert remove_fillers("申请去酒吧啊") == "申请去酒吧"
    assert to_halfwidth("ＡＢＣ１２３，！") == "ABC123,!"
    assert remove_spaces("空调 调到\t22度") == "空调调到22度"
    assert normalize_command("请帮我把空调调到 二十二度 吧") == "把空调调到22度"


# ── 关键词层 ──────────────────────────────────────────────────────────────────

KEYWORD_SAMPLES = {
    IntentCategory.CLIMATE_CONTROL: "空调调到二十二度",
    IntentCategory.BODY_CONTROL: "把副驾车窗打开",
    IntentCategory.DRIVE_SETTING: "切换到运动模式",
    IntentCategory.NAVIGATION: "导航回家",
    IntentCategory.POI_SEARCH: "附近有没有充电站",
    IntentCategory.VEHICLE_STATUS: "还剩多少电",
    IntentCategory.FAULT_DIAGNOSIS: "仪表报了个故障码P0A80",
    IntentCategory.MANUAL_QA: "冬天用车要注意什么",
    IntentCategory.MEDIA_CONTROL: "来首周杰伦的歌",
    IntentCategory.PHONE_CALL: "给老婆打个电话",
    IntentCategory.EMERGENCY_SOS: "前面出了车祸",
    IntentCategory.ROADSIDE_ASSIST: "车趴窝了要拖车",
    IntentCategory.CHITCHAT: "你好呀",
}


def test_every_intent_except_other_is_covered_by_keywords():
    assert set(KEYWORD_SAMPLES) == set(IntentCategory) - {IntentCategory.OTHER}
    assert set(INTENT_KEYWORDS) == set(IntentCategory) - {IntentCategory.OTHER}
    for intent, sample in KEYWORD_SAMPLES.items():
        assert count_keywords(sample).intent is intent, sample


def test_keyword_lexicon_has_no_cross_intent_duplicates():
    owner = {}
    for intent, terms in INTENT_KEYWORDS.items():
        for term in terms:
            key = term.lower()
            assert owner.setdefault(key, intent) is intent, f"{term} 同时属于 {owner[key]} 和 {intent}"


def test_keyword_layer_boundary_rules_and_homophones():
    # 2.2：按类别找地点算 poi_search，即使说"导航去"
    assert count_keywords("导航去最近的充电站").intent is IntentCategory.POI_SEARCH
    # 座椅加热算 climate（最长匹配，不算 body 的"座椅"）
    assert count_keywords("座椅加热调到两档").intent is IntentCategory.CLIMATE_CONTROL
    # 胎压读数 vs 胎压报警
    assert count_keywords("胎压多少").intent is IntentCategory.VEHICLE_STATUS
    assert count_keywords("胎压报警了").intent is IntentCategory.FAULT_DIAGNOSIS
    # 同音错字
    assert count_keywords("空挑开一下").intent is IntentCategory.CLIMATE_CONTROL
    assert count_keywords("倒航去机场").intent is IntentCategory.NAVIGATION


def test_keyword_layer_detects_multi_intent_clauses():
    analysis = analyze_keywords("空调调到22度然后导航回家")
    assert analysis.multi_intent is True
    assert [(t.intent, t.text) for t in analysis.sub_tasks] == [
        (IntentCategory.CLIMATE_CONTROL, "空调调到22度"),
        (IntentCategory.NAVIGATION, "导航回家"),
    ]
    # 同一 Agent 下的不同意图也拆开，路由时再合并派发
    assert [(t.intent, t.text) for t in analyze_keywords("打开车窗，再把空调打开").sub_tasks] == [
        (IntentCategory.BODY_CONTROL, "打开车窗"),
        (IntentCategory.CLIMATE_CONTROL, "把空调打开"),
    ]
    # 同意图片段合并，问候前缀并入相邻片段，都不算多意图
    assert analyze_keywords("把空调打开，座椅加热也开一下").multi_intent is False
    assert analyze_keywords("你好，打开空调").multi_intent is False


def test_emergency_phrases_are_precise():
    assert find_emergency_phrase("车着火了，快帮帮我") == "车着火了"
    assert find_emergency_phrase("不好了车着火了") == "车着火了"
    assert find_emergency_phrase("后面有人受伤") == "有人受伤"
    # 否定和假设语境不算
    assert find_emergency_phrase("差点追尾了") is None
    assert find_emergency_phrase("万一车子着火了怎么办") is None
    assert find_emergency_phrase("没有冒烟了") is None
    # 泛化词不在紧急短语表里
    for word in ("救命", "着急", "要死"):
        assert word not in EMERGENCY_PHRASES
    assert find_emergency_phrase("救命，好热") is None


# ── 分层决策 ──────────────────────────────────────────────────────────────────

def test_emergency_phrase_skips_llm():
    client = FakeClient(response=json_response(intent="chitchat", reasoning="不应被调用"))
    result = recognize(make_recognizer(client), "车着火了，快帮帮我")

    assert result.intent is IntentCategory.EMERGENCY_SOS
    assert result.confidence_level == "high"
    assert result.sources["decided_by"] == "emergency_rule"
    assert result.sources["llm_status"] == "skipped"
    assert client.calls == []


def test_help_me_its_hot_stays_climate_control():
    client = FakeClient(response=json_response(intent="climate_control", reasoning="嫌热，需要降温"))
    result = recognize(make_recognizer(client), "救命，好热")

    assert result.intent is IntentCategory.CLIMATE_CONTROL
    assert result.confidence_level == "high"
    assert len(client.calls) == 1


def test_llm_request_uses_text_json_output():
    client = FakeClient(response=json_response(intent="navigation", reasoning="回家"))
    history = [{"role": "user", "content": f"第{i}句"} for i in range(5)]
    result = recognize(make_recognizer(client), "导航回家", history=history)

    assert result.intent is IntentCategory.NAVIGATION
    assert result.sources["llm_status"] == "ok"
    assert result.sources["llm_intent"] == "navigation"
    assert result.sources["decided_by"] == "llm"

    call = client.calls[0]
    assert "tools" not in call and "tool_choice" not in call
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 1536
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}  # 关闭思考模式
    assert "只输出一个 JSON 对象" in call["system"]
    assert "空挑调到二十二度" in call["system"]  # few-shot 示例确实拼进了 prompt
    user_text = call["messages"][0]["content"]
    assert "第4句" in user_text and "第2句" in user_text and "第1句" not in user_text  # 只带最近 3 条


def test_json_parsing_ignores_thinking_and_extra_text():
    # 思考块被忽略；JSON 前后带了多余文字也能截取
    client = FakeClient(response=text_response('好的。{"intent": "media_control", "reasoning": "放歌"} 以上'))
    result = recognize(make_recognizer(client), "来首歌")
    assert result.intent is IntentCategory.MEDIA_CONTROL
    assert result.sources["decided_by"] == "llm"

    # dict 形式的内容块同样能解析
    data = parse_llm_payload([{"type": "text", "text": '{"intent": "chitchat", "reasoning": "问候"}'}])
    assert data["intent"] == "chitchat"


def test_unparseable_llm_output_counts_as_failure():
    client = FakeClient(response=text_response("我觉得是空调相关"))
    result = recognize(make_recognizer(client), "空调调到二十二度")
    assert result.sources["llm_status"] == "failed"
    assert result.sources["decided_by"] == "keyword_fallback"
    assert result.intent is IntentCategory.CLIMATE_CONTROL


def test_llm_timeout_falls_back_to_keywords():
    client = FakeClient(response=json_response(intent="media_control", reasoning="太慢了"), delay=0.5)
    result = recognize(make_recognizer(client, timeout_s=0.05), "空调调到二十二度")

    assert result.sources["llm_status"] == "timeout"
    assert result.sources["decided_by"] == "keyword_fallback"
    assert result.intent is IntentCategory.CLIMATE_CONTROL
    assert result.confidence_level == "low"


def test_llm_error_splits_multi_intent_by_keywords_and_is_not_cached():
    client = FakeClient(error=RuntimeError("provider down"))
    recognizer = make_recognizer(client)
    result = recognize(recognizer, "空调调到22度然后导航回家")

    assert result.sources["llm_status"] == "failed"
    assert result.sources["decided_by"] == "keyword_fallback"
    assert result.sources["multi_intent_keyword"] is True
    assert result.sources["keyword_counts"] == {"climate_control": 1, "navigation": 2}
    assert [(t.intent, t.text) for t in result.sub_tasks] == [
        (IntentCategory.CLIMATE_CONTROL, "空调调到22度"),
        (IntentCategory.NAVIGATION, "导航回家"),
    ]
    assert result.execution == "parallel"
    assert result.intent is IntentCategory.CLIMATE_CONTROL  # 非安全类取第一个诉求
    assert result.confidence_level == "low"

    # 失败结果不缓存，下一次仍会重试 LLM
    recognize(recognizer, "空调调到22度然后导航回家")
    assert len(client.calls) == 2


def test_llm_error_without_keywords_returns_other_none():
    result = recognize(make_recognizer(FakeClient(error=RuntimeError("down"))), "嗯那个就是")
    assert result.intent is IntentCategory.OTHER
    assert result.confidence_level == "none"
    assert result.sources["decided_by"] == "none"


def test_confidence_reflects_layer_agreement():
    agree = recognize(make_recognizer(FakeClient(json_response(intent="climate_control", reasoning="开空调"))), "打开空调")
    assert agree.confidence_level == "high"
    assert agree.sources["keyword_intent"] == "climate_control"

    disagree = recognize(make_recognizer(FakeClient(json_response(intent="body_control", reasoning="?"))), "打开空调")
    assert disagree.intent is IntentCategory.BODY_CONTROL
    assert disagree.confidence_level == "medium"

    llm_only = recognize(make_recognizer(FakeClient(json_response(intent="navigation", reasoning="想去海边"))), "我想去看看海")
    assert llm_only.sources["keyword_intent"] is None
    assert llm_only.confidence_level == "medium"

    unsure = recognize(make_recognizer(FakeClient(json_response(intent="other", reasoning="听不懂"))), "打开空调")
    assert unsure.confidence_level == "low"


# ── 层开关（评测 E1 消融用）───────────────────────────────────────────────────

def test_keywords_only_skips_llm():
    client = FakeClient(response=json_response(intent="navigation", reasoning="x"))
    recognizer = make_recognizer(client, use_llm=False, cache_size=0)
    result = recognize(recognizer, "把空调打开")
    assert client.calls == []
    assert result.intent is IntentCategory.CLIMATE_CONTROL
    assert result.sources["llm_status"] == "skipped"
    assert result.sources["decided_by"] == "keyword_fallback"
    assert "未启用" in result.reasoning
    # 紧急短语照样直达
    assert recognize(recognizer, "车着火了").sources["decided_by"] == "emergency_rule"


def test_llm_only_has_no_emergency_shortcut_or_fallback():
    client = FakeClient(response=json_response(intent="emergency_sos", reasoning="着火"))
    recognizer = make_recognizer(client, use_keywords=False, cache_size=0)
    result = recognize(recognizer, "车着火了")
    assert len(client.calls) == 1
    assert result.intent is IntentCategory.EMERGENCY_SOS
    assert result.sources["decided_by"] == "llm"
    assert result.sources["keyword_intent"] is None and result.sources["emergency_phrase"] is None

    failing = make_recognizer(FakeClient(error=RuntimeError("down")), use_keywords=False, cache_size=0)
    result = recognize(failing, "把空调打开")
    assert result.intent is IntentCategory.OTHER
    assert result.sources["decided_by"] == "none"


def test_at_least_one_layer_required():
    with pytest.raises(ValueError):
        IntentRecognizer(api_key="k", model="m", use_keywords=False, use_llm=False)


# ── 子任务校验 ────────────────────────────────────────────────────────────────

def test_sub_task_validation_drops_invalid_and_requires_two():
    outcome = validate_llm_payload({
        "intent": "climate_control",
        "reasoning": "x",
        "sub_tasks": [
            {"intent": "climate_control", "text": "空调调到22度"},
            {"intent": "teleport", "text": "瞬移回家"},
            {"intent": "media_control", "text": "   "},
        ],
        "execution": "sequential",
    })
    assert outcome.sub_tasks == []
    assert outcome.execution == "parallel"
    assert outcome.intent is IntentCategory.CLIMATE_CONTROL

    outcome = validate_llm_payload({
        "intent": "not_a_category",
        "reasoning": "x",
        "sub_tasks": [
            {"intent": "climate_control", "text": "空调调低"},
            {"intent": "bogus", "text": "x"},
            {"intent": "media_control", "text": "放首歌"},
        ],
        "execution": "sequential",
    })
    assert [t.intent for t in outcome.sub_tasks] == [IntentCategory.CLIMATE_CONTROL, IntentCategory.MEDIA_CONTROL]
    assert outcome.execution == "sequential"
    # 主意图非法按 other，再按"不在子任务中取第一个"校正
    assert outcome.intent is IntentCategory.CLIMATE_CONTROL


def test_sub_task_limit_and_primary_reconciliation():
    tasks = [{"intent": "media_control", "text": f"第{i}件事"} for i in range(6)]
    outcome = validate_llm_payload({"intent": "media_control", "reasoning": "x", "sub_tasks": tasks})
    assert len(outcome.sub_tasks) == 4

    outcome = validate_llm_payload({
        "intent": "chitchat",
        "reasoning": "x",
        "sub_tasks": [
            {"intent": "climate_control", "text": "空调关了"},
            {"intent": "media_control", "text": "放首歌"},
        ],
    })
    assert outcome.intent is IntentCategory.CLIMATE_CONTROL


def test_safety_sub_task_becomes_primary_intent():
    client = FakeClient(json_response(
        intent="media_control",
        reasoning="关音乐和叫救援",
        sub_tasks=[
            {"intent": "media_control", "text": "把音乐关了"},
            {"intent": "roadside_assist", "text": "右前轮爆胎了帮我叫救援"},
        ],
        execution="parallel",
    ))
    result = recognize(make_recognizer(client), "把音乐关了，右前轮爆胎了帮我叫救援")

    assert result.intent is IntentCategory.ROADSIDE_ASSIST
    assert [t.intent for t in result.sub_tasks] == [IntentCategory.MEDIA_CONTROL, IntentCategory.ROADSIDE_ASSIST]
    assert all(isinstance(t, SubTask) for t in result.sub_tasks)


# ── 缓存 ──────────────────────────────────────────────────────────────────────

def test_lru_cache_refreshes_on_hit_and_evicts_least_recent():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # 命中后 a 变成最近使用
    cache.put("c", 3)           # 淘汰最久未用的 b
    assert "b" not in cache
    assert cache.keys() == ["a", "c"]
    assert len(cache) == 2


def test_recognizer_uses_true_lru_cache():
    client = FakeClient(json_response(intent="climate_control", reasoning="ok"))
    recognizer = make_recognizer(client, cache_size=2)

    recognize(recognizer, "打开空调")    # miss，调用 1
    recognize(recognizer, "关闭空调")    # miss，调用 2
    recognize(recognizer, "打开空调")    # hit，刷新顺序
    recognize(recognizer, "空调调高")    # miss，调用 3，淘汰"关闭空调"
    recognize(recognizer, "打开空调")    # hit
    recognize(recognizer, "关闭空调")    # miss，调用 4

    assert len(client.calls) == 4
    stats = recognizer.cache_stats
    assert stats["hits"] == 2
    assert stats["misses"] == 4
    assert stats["size"] == 2

    # 历史不同则缓存 key 不同
    recognize(recognizer, "打开空调", history=[{"role": "user", "content": "好冷"}])
    assert len(client.calls) == 5


# ── Prompt 与配置 ─────────────────────────────────────────────────────────────

def test_few_shot_examples_two_per_intent_and_in_prompt():
    assert set(FEW_SHOT_EXAMPLES) == set(IntentCategory)
    prompt = build_system_prompt()
    for intent, examples in FEW_SHOT_EXAMPLES.items():
        assert len(examples) == 2
        assert all(isinstance(text, str) and text.strip() for text in examples)
        assert intent.value in prompt
        for text in examples:
            assert text in prompt
    assert "语音识别" in prompt and "同音错字" in prompt
    assert "sequential" in prompt and "最多 4" in prompt


def test_env_config_and_explicit_args_priority(monkeypatch, caplog):
    monkeypatch.setenv("CABINWEAVE_INTENT_MODEL", "fast-model")
    monkeypatch.setenv("CABINWEAVE_INTENT_TIMEOUT_S", "abc")
    monkeypatch.setenv("CABINWEAVE_INTENT_CACHE_SIZE", "5")

    with caplog.at_level(logging.WARNING, logger="core.intent_recognizer"):
        recognizer = IntentRecognizer(api_key="test-key", model="main-model")
    assert recognizer.model == "fast-model"
    assert recognizer.timeout_s == 5.0
    assert any("CABINWEAVE_INTENT_TIMEOUT_S" in record.getMessage() for record in caplog.records)
    assert recognizer.cache_stats["capacity"] == 5

    recognizer = IntentRecognizer(api_key="test-key", model="main-model", timeout_s=1.5, cache_size=7)
    assert recognizer.timeout_s == 1.5
    assert recognizer.cache_stats["capacity"] == 7
