"""
CabinWeave 驾驶员偏好测试：normalize_driver_profile 的规整规则（合法值、类型错误、
notes 去重和上限、敏感号码）、merge_driver_profile 的合并、给模型看的偏好不含 places、
上下文的"[驾驶员偏好]"标签和可读中文、偏好提炼 prompt 的内容，以及 update_profile 用假客户端和假存储
走完"提炼 → 规整 → 合并 → 存库"、get_profile 读出时同样规整；偏好提炼、对话摘要、摘要合并关闭思考，
摘要输出被 max_tokens 截断时走兜底。

全部用假数据，不连接 Redis、ChromaDB，不调用真实模型；MemoryManager 用 __new__ 创建后手动装上假依赖。
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from memory.conversation_memory import (
    MAX_PROFILE_NOTES,
    MemoryContext,
    MemoryManager,
    Message,
    MsgRole,
    build_profile_prompt,
    merge_driver_profile,
    normalize_driver_profile,
    prompt_driver_preferences,
)

VALID_PROFILE = {
    "preferences": {"climate_temp": 22, "seat_heat": "冬天常开", "music": ["轻音乐"], "drive_mode": "comfort"},
    "places": {"home": "上海市徐汇区漕溪北路 100 号", "company": "上海市闵行区申虹路 8 号"},
    "notes": ["周末常带孩子去郊区"],
}


# ── normalize_driver_profile ─────────────────────────────────────────────────

def test_normalize_keeps_valid_profile_and_cleans_values():
    assert normalize_driver_profile(VALID_PROFILE) == VALID_PROFILE

    raw = {
        "preferences": {"climate_temp": 22.0, "drive_mode": " SPORT ", "music": [" 轻音乐 ", "轻音乐", "周杰伦"]},
        "places": {"home": "  上海南站  "},
        "notes": [" 喜欢开窗 "],
    }
    assert normalize_driver_profile(raw) == {
        "preferences": {"climate_temp": 22, "drive_mode": "sport", "music": ["轻音乐", "周杰伦"]},
        "places": {"home": "上海南站"},
        "notes": ["喜欢开窗"],
    }
    assert normalize_driver_profile({"preferences": {"climate_temp": 22.5}}) == {"preferences": {"climate_temp": 22.5}}


@pytest.mark.parametrize("data", [None, [], "偏好", 42, {}])
def test_normalize_non_dict_or_empty_returns_empty(data):
    assert normalize_driver_profile(data) == {}


def test_normalize_drops_values_that_fail_type_checks():
    raw = {
        "preferences": {
            "climate_temp": "22",          # 字符串不算数字
            "drive_mode": "turbo",         # 不在 eco / comfort / sport 里
            "music": "轻音乐",              # 不是列表
            "seat_heat": 2,                # 不是字符串
            "fragrance": "淡香",            # 其他键：字符串保留
            "volume": 12,                  # 其他键：数字保留
            "auto_hold": True,             # 其他键：布尔保留
            "radio": ["交通台", 3, "交通台"],  # 其他键：字符串列表，非字符串和重复项丢掉
            "nested": {"a": 1},            # 其他键：嵌套对象丢掉
            "blank": "  ",
            "none": None,
            "": "无名键",
        },
        "places": {"home": "", "company": 123, "school": "某小学"},
        "notes": "不是列表",
    }
    assert normalize_driver_profile(raw) == {
        "preferences": {"fragrance": "淡香", "volume": 12, "auto_hold": True, "radio": ["交通台"]},
    }


@pytest.mark.parametrize("temp", [15, 33, 40, True, float("nan"), float("inf")])
def test_normalize_climate_temp_must_be_number_within_16_to_32(temp):
    assert normalize_driver_profile({"preferences": {"climate_temp": temp}}) == {}


def test_normalize_music_list_without_strings_is_dropped():
    assert normalize_driver_profile({"preferences": {"music": [1, None, ""]}}) == {}


def test_normalize_preferences_must_be_object():
    data = {"preferences": ["喜欢22度"], "notes": ["喜欢22度", "冬天常开座椅加热"]}
    assert normalize_driver_profile(data) == {"notes": ["喜欢22度", "冬天常开座椅加热"]}


def test_normalize_notes_are_deduplicated_and_capped():
    notes = [f"习惯{i}" for i in range(12)]
    notes.insert(3, "习惯0")
    result = normalize_driver_profile({"notes": notes + [1, None, " "]})
    assert MAX_PROFILE_NOTES == 10
    assert result == {"notes": [f"习惯{i}" for i in range(10)]}


def test_normalize_drops_strings_with_sensitive_numbers():
    raw = {
        "preferences": {"seat_heat": "冬天常开", "contact": "老婆电话13812345678"},
        "places": {"home": "漕溪北路1388弄", "company": "前台 021-64871234 转 13812345678"},
        "notes": ["身份证11010519491231002X", "银行卡6222021234567890123", "喜欢开窗"],
    }
    assert normalize_driver_profile(raw) == {
        "preferences": {"seat_heat": "冬天常开"},
        "places": {"home": "漕溪北路1388弄"},
        "notes": ["喜欢开窗"],
    }


# ── 合并与给模型看的部分 ──────────────────────────────────────────────────────

def test_merge_keeps_unmentioned_values_and_overrides_mentioned_ones():
    current = {
        "preferences": {"climate_temp": 22, "seat_heat": "冬天常开"},
        "places": {"home": "上海南站"},
        "notes": ["周末常去郊区"],
    }
    update = {"preferences": {"climate_temp": 24, "music": ["轻音乐"], "seat_heat": 3}, "places": {"company": "虹桥站"}}
    assert merge_driver_profile(current, update) == {
        "preferences": {"climate_temp": 24, "seat_heat": "冬天常开", "music": ["轻音乐"]},
        "places": {"home": "上海南站", "company": "虹桥站"},
        "notes": ["周末常去郊区"],
    }
    assert merge_driver_profile(current, {"notes": ["喜欢开窗"]})["notes"] == ["喜欢开窗"]
    assert merge_driver_profile({}, {}) == {}
    assert merge_driver_profile(current, "无法解析") == normalize_driver_profile(current)


def test_prompt_driver_preferences_excludes_places():
    assert prompt_driver_preferences(VALID_PROFILE) == {
        "preferences": VALID_PROFILE["preferences"],
        "notes": VALID_PROFILE["notes"],
    }
    assert prompt_driver_preferences({"places": {"home": "上海南站"}}) == {}
    assert prompt_driver_preferences({"preferences": {"climate_temp": 99}, "notes": []}) == {}
    assert prompt_driver_preferences(None) == {}


# ── MemoryContext.to_prompt_text ─────────────────────────────────────────────

def make_context(profile, **kwargs):
    return MemoryContext(
        recent_messages=kwargs.get("recent", []),
        relevant_history=kwargs.get("history", []),
        user_profile=profile,
        summary=kwargs.get("summary", ""),
    )


def test_to_prompt_text_uses_driver_label_readable_chinese_and_no_places():
    recent = [Message(role=MsgRole.USER, content="空调调到22度")]
    text = make_context(VALID_PROFILE, recent=recent, summary="驾驶员在调空调").to_prompt_text()

    assert "[驾驶员偏好]" in text
    assert "冬天常开" in text and "轻音乐" in text and "周末常带孩子去郊区" in text
    assert "\\u" not in text
    assert "places" not in text and "漕溪北路" not in text and "申虹路" not in text
    section = text.split("[驾驶员偏好]\n", 1)[1].split("\n\n", 1)[0]
    assert json.loads(section) == prompt_driver_preferences(VALID_PROFILE)
    assert "[会话摘要]" in text and "user: 空调调到22度" in text


def test_to_prompt_text_without_visible_preferences_has_no_section():
    assert "[驾驶员偏好]" not in make_context({}).to_prompt_text()
    assert "[驾驶员偏好]" not in make_context({"places": {"home": "上海南站"}}).to_prompt_text()


def test_to_prompt_text_removes_lone_surrogates():
    text = make_context({"notes": ["喜欢\ud83d轻音乐"], "preferences": {"seat_heat": "常开\udc00"}}).to_prompt_text()
    text.encode("utf-8")  # 不抛 UnicodeEncodeError
    assert "喜欢轻音乐" in text and "常开" in text


# ── 偏好提炼 prompt ───────────────────────────────────────────────────────────

def test_build_profile_prompt_describes_driver_preferences():
    prompt = build_profile_prompt("user: 我一般开22度\nassistant: 好的。", VALID_PROFILE)

    for word in ("驾驶员偏好", "climate_temp", "seat_heat", "music", "drive_mode", "places", "home", "company",
                 "notes", "一次性指令", "这次调到26度", "我一般开22度", "电话号码", "证件号", "合并", "保留"):
        assert word in prompt, word
    assert "user: 我一般开22度" in prompt
    # 已有偏好用可读中文传给模型（含 places，供模型合并）
    assert "冬天常开" in prompt and "漕溪北路" in prompt and "\\u" not in prompt


def test_build_profile_prompt_normalizes_current_profile():
    prompt = build_profile_prompt("user: 你好", {"preferences": {"climate_temp": 50}, "notes": ["喜欢22度", "喜欢22度"]})
    # 越界的温度被丢掉，重复的备注只留一条
    assert '已有偏好：\n{"notes": ["喜欢22度"]}\n' in prompt


# ── update_profile / get_profile（假客户端、假 Redis、假 collection）────────────

class FakeRedis:
    """只实现工作记忆读取用到的 lrange / get；列表最新消息在前，与 lpush 一致。"""

    def __init__(self, messages):
        self.items = [
            json.dumps({"role": role, "content": content, "ts": datetime(2026, 10, 1).isoformat(), "metadata": {}})
            for role, content in reversed(messages)
        ]

    async def lrange(self, key, start, end):
        return self.items[start:end + 1]

    async def get(self, key):
        return None


class FakeProfileStore:
    """偏好 collection 的假实现：按 id 或 metadata 等值条件读取，记录写入。"""

    def __init__(self, docs=None):
        self.docs = {}
        self.metas = {}
        self.added = []
        for doc_id, (document, metadata) in (docs or {}).items():
            self.docs[doc_id] = document
            self.metas[doc_id] = metadata

    def _result(self, ids):
        return {"ids": ids, "documents": [self.docs[i] for i in ids], "metadatas": [self.metas[i] for i in ids]}

    def get(self, ids=None, where=None):
        if ids is not None:
            return self._result([i for i in ids if i in self.docs])
        return self._result([
            i for i, meta in self.metas.items() if all(meta.get(k) == v for k, v in (where or {}).items())
        ])

    def delete(self, ids):
        for doc_id in ids:
            self.docs.pop(doc_id, None)
            self.metas.pop(doc_id, None)

    def add(self, ids, documents, metadatas):
        self.added.append({"ids": ids, "documents": documents, "metadatas": metadatas})
        for doc_id, document, metadata in zip(ids, documents, metadatas):
            self.docs[doc_id] = document
            self.metas[doc_id] = metadata


class ScriptedClient:
    """按顺序返回预设文本（或完整响应对象）的假客户端，记录每次调用参数。"""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []
        client = self

        class Messages:
            async def create(inner, **kwargs):
                client.calls.append(kwargs)
                item = client.texts.pop(0)
                if isinstance(item, SimpleNamespace):
                    return item
                return SimpleNamespace(content=[{"type": "text", "text": item}])

        self.messages = Messages()


def truncated(text):
    """输出达到 max_tokens 被截断的响应。"""
    return SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason="max_tokens")


THINKING_OFF = {"thinking": {"type": "disabled"}}


def make_manager(messages, client, store):
    manager = MemoryManager.__new__(MemoryManager)
    manager._client = client
    manager._model = "test-model"
    manager._redis = FakeRedis(messages)
    manager._profile = store
    return manager


DIALOGUE = [
    ("user", "我一般开22度，这次先调到26度"),
    ("assistant", "好的，温度已调到26度。"),
    ("user", "我公司在上海市闵行区申虹路 8 号"),
]


def test_update_profile_sends_new_prompt_and_stores_normalized_merged_profile():
    existing = {"preferences": {"seat_heat": "冬天常开"}, "places": {"home": "上海南站"}, "notes": ["周末常去郊区"]}
    store = FakeProfileStore({
        "user_profile:u1": (json.dumps(existing, ensure_ascii=False), {"user_id": "u1"}),
    })
    reply = (
        "好的，更新后的偏好如下：\n"
        '{"preferences": {"climate_temp": 22, "drive_mode": "COMFORT", "seat_heat": 2, "music": ["轻音乐", 5]},'
        ' "places": {"company": "上海市闵行区申虹路 8 号", "gym": "某健身房"},'
        ' "notes": ["周末常去郊区", "电话 13812345678"],'
        ' "entities": {"产品": ["MEGA"]}}'
    )
    client = ScriptedClient(reply)
    asyncio.run(make_manager(DIALOGUE, client, store).update_profile("u1", "c1"))

    assert client.calls[0]["extra_body"] == THINKING_OFF
    assert client.calls[0]["max_tokens"] == 1536
    prompt = client.calls[0]["messages"][0]["content"]
    assert "驾驶员偏好" in prompt and "一次性指令" in prompt and "places" in prompt
    assert "user: 我一般开22度，这次先调到26度" in prompt
    assert "冬天常开" in prompt and "\\u" not in prompt
    assert "entities" not in prompt

    assert len(store.added) == 1
    stored = json.loads(store.docs["user_profile:u1"])
    assert stored == {
        "preferences": {"seat_heat": "冬天常开", "climate_temp": 22, "drive_mode": "comfort", "music": ["轻音乐"]},
        "places": {"home": "上海南站", "company": "上海市闵行区申虹路 8 号"},
        "notes": ["周末常去郊区"],
    }
    assert store.metas["user_profile:u1"]["user_id"] == "u1"
    assert "申虹路" in store.added[0]["documents"][0]  # 存库是可读中文


@pytest.mark.parametrize("reply", ["{}", "没有新的偏好", '{"preferences": {"climate_temp": 99}}'])
def test_update_profile_does_not_overwrite_when_nothing_new(reply):
    existing = {"preferences": {"climate_temp": 22}}
    store = FakeProfileStore({"user_profile:u1": (json.dumps(existing), {"user_id": "u1"})})
    asyncio.run(make_manager(DIALOGUE, ScriptedClient(reply), store).update_profile("u1", "c1"))
    assert store.added == []
    assert json.loads(store.docs["user_profile:u1"]) == existing


def test_update_profile_without_messages_does_not_call_model():
    client = ScriptedClient()
    asyncio.run(make_manager([], client, FakeProfileStore()).update_profile("u1", "c1"))
    assert client.calls == []


def test_get_profile_normalizes_stored_documents():
    stored = {"preferences": {"climate_temp": 22, "drive_mode": "turbo"}, "notes": ["喜欢22度", "喜欢22度"]}
    store = FakeProfileStore({"user_profile:u1": (json.dumps(stored, ensure_ascii=False), {"user_id": "u1"})})
    manager = make_manager([], ScriptedClient(), store)
    assert asyncio.run(manager.get_profile("u1")) == {"preferences": {"climate_temp": 22}, "notes": ["喜欢22度"]}

    # 没有固定 id 的文档时按 user_id 取最新一条，同样规整
    store = FakeProfileStore({
        "old": (json.dumps({"preferences": {"climate_temp": 20}}), {"user_id": "u2", "updated_at": "2026-01-01"}),
        "new": (json.dumps({"preferences": {"climate_temp": 50, "music": ["轻音乐"]}}),
                {"user_id": "u2", "updated_at": "2026-02-01"}),
    })
    manager = make_manager([], ScriptedClient(), store)
    assert asyncio.run(manager.get_profile("u2")) == {"preferences": {"music": ["轻音乐"]}}


# ── 摘要（关闭思考、截断兜底）──────────────────────────────────────────────────

def test_merge_summary_disables_thinking_and_falls_back_when_truncated():
    client = ScriptedClient("合并后的摘要", truncated("合并后的摘"))
    manager = make_manager([], client, FakeProfileStore())

    assert asyncio.run(manager._merge_summary("旧摘要", "新摘要")) == "合并后的摘要"
    assert client.calls[0]["extra_body"] == THINKING_OFF
    assert client.calls[0]["max_tokens"] == 768

    # 输出被截断：不用半截结果，回退为旧摘要 + 新摘要
    assert asyncio.run(manager._merge_summary("旧摘要", "新摘要")) == "旧摘要\n新摘要"


class SummaryRedis:
    """压缩用到的 get / setex / delete / lpush / expire，记录写入的摘要和保留的消息。"""

    def __init__(self):
        self.summary = None
        self.pushed = []

    async def get(self, key):
        return None

    async def setex(self, key, ttl, value):
        self.summary = value

    async def delete(self, key):
        self.pushed.clear()

    async def lpush(self, key, value):
        self.pushed.append(value)

    async def expire(self, key, ttl):
        pass


def test_compress_disables_thinking_and_uses_fallback_summary_when_truncated():
    client = ScriptedClient(truncated("驾驶员想去"))
    manager = make_manager([], client, FakeProfileStore())
    manager._redis = SummaryRedis()
    messages = [Message(MsgRole.USER, f"第{i}条") for i in range(MemoryManager.COMPRESS_AT)]
    episodic = []

    async def working_memory(user_id, conv_id):
        return messages

    async def store_episodic(user_id, conv_id, text, summary):
        episodic.append(summary)

    manager._get_working_memory = working_memory
    manager._store_episodic = store_episodic
    asyncio.run(manager._compress("u1", "c1"))

    assert client.calls[0]["extra_body"] == THINKING_OFF
    assert client.calls[0]["max_tokens"] == 768
    fallback = f"对话包含 {MemoryManager.COMPRESS_AT - 5} 条消息（摘要生成失败）"
    assert episodic == [fallback]
    assert manager._redis.summary == fallback  # 没有旧摘要时直接用新摘要，不再调模型合并
    assert len(client.calls) == 1
    assert len(manager._redis.pushed) == 5
