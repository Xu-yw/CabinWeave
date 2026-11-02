"""
CabinWeave 多轮对话记忆

三级记忆架构：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（ChromaDB）—— 跨会话的历史对话，按语义相似度检索
  3. 驾驶员偏好（ChromaDB）—— 从对话中提炼的稳定偏好、家和公司地址、其他习惯

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - Embedding 由 ChromaDB 内置模型生成，不调用外部 Embedding API
  - chromadb、redis 在 MemoryManager 构造时才导入，偏好规整、上下文格式化等纯函数不依赖它们

驾驶员偏好格式：
  {"preferences": {"climate_temp": 22, "seat_heat": "冬天常开", "music": ["轻音乐"], "drive_mode": "comfort"},
   "places": {"home": "…", "company": "…"},
   "notes": ["…"]}
  - update_profile 用 LLM 从最近对话提炼（build_profile_prompt），结果规整后与已有偏好合并再存库；
    get_profile 读出后同样规整
  - normalize_driver_profile 规整规则：
      preferences  只接受 dict；已知键做类型校验，不合格的丢掉：climate_temp 为 16–32 的数字，
                   drive_mode 为 eco / comfort / sport，music 为字符串列表，seat_heat 为字符串；
                   其他键只保留标量或字符串列表
      places       只保留 home、company 两个键，值为非空字符串
      notes        字符串列表，去重保序，最多 MAX_PROFILE_NOTES 条
      敏感信息     含连续 11 位以上数字（电话号码、证件号、银行卡号）的字符串一律丢掉
    空的字段不出现在结果里
  - merge_driver_profile：preferences、places 按键合并（新值覆盖，没提到的保留），notes 有新的就用新的
  - 给模型看的只有 preferences 和 notes（prompt_driver_preferences），places 只由工具从 Request.profile 读取，
    上下文、角色输入包和播报里都不出现具体地址
"""
import hashlib
import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content
from vehicle.safety import TEMP_RANGE
from vehicle.state import DRIVE_MODES

logger = logging.getLogger(__name__)


# ── 驾驶员偏好─────────────────────────────────────────────────────

PROFILE_PLACE_KEYS: Tuple[str, ...] = ("home", "company")
# 给模型看的字段；places 不在其中
PROFILE_PROMPT_KEYS: Tuple[str, ...] = ("preferences", "notes")
MAX_PROFILE_NOTES = 10

# 疑似电话号码、证件号、银行卡号：连续 11 位以上数字，或 17 位数字加 X
_SENSITIVE_RE = re.compile(r"\d{11,}|\d{17}[xX]")


def _clean_str(value: Any) -> str:
    """去掉非法代理字符和首尾空白；不是字符串时返回空串。"""
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", errors="ignore").decode("utf-8").strip()


def _safe_str(value: Any) -> str:
    """可以存进偏好的字符串：非空且不含疑似敏感号码；不合格时返回空串。"""
    text = _clean_str(value)
    return "" if _SENSITIVE_RE.search(text) else text


def _str_list(value: Any) -> List[str]:
    """字符串列表：非字符串、空串和敏感项丢掉，去重保序；不是列表时返回空列表。"""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(text for text in (_safe_str(item) for item in value) if text))


def _is_number(value: Any) -> bool:
    """有限的 int / float，布尔值不算数字。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_preference(name: str, value: Any) -> Any:
    """校验单个偏好值，返回规整后的值；不合格返回 None。"""
    if name == "climate_temp":
        if _is_number(value) and TEMP_RANGE[0] <= value <= TEMP_RANGE[1]:
            return int(value) if float(value).is_integer() else round(float(value), 1)
        return None
    if name == "drive_mode":
        mode = _clean_str(value).lower()
        return mode if mode in DRIVE_MODES else None
    if name == "music":
        return _str_list(value) or None
    if name == "seat_heat":
        return _safe_str(value) or None
    # 其他键：只保留标量或字符串列表
    if isinstance(value, bool) or _is_number(value):
        return value
    if isinstance(value, str):
        return _safe_str(value) or None
    if isinstance(value, list):
        return _str_list(value) or None
    return None


def normalize_driver_profile(data: Any) -> Dict[str, Any]:
    """规整驾驶员偏好，规则见模块说明；不是 dict 时返回空 dict。"""
    if not isinstance(data, dict):
        return {}
    raw_preferences = data.get("preferences")
    preferences: Dict[str, Any] = {}
    if isinstance(raw_preferences, dict):
        for key, value in raw_preferences.items():
            name = _clean_str(key)
            checked = _check_preference(name, value) if name else None
            if checked is not None:
                preferences[name] = checked
    notes = _str_list(data.get("notes"))[:MAX_PROFILE_NOTES]

    raw_places = data.get("places")
    places: Dict[str, str] = {}
    if isinstance(raw_places, dict):
        for key in PROFILE_PLACE_KEYS:
            address = _safe_str(raw_places.get(key))
            if address:
                places[key] = address

    profile: Dict[str, Any] = {}
    if preferences:
        profile["preferences"] = preferences
    if places:
        profile["places"] = places
    if notes:
        profile["notes"] = notes
    return profile


def merge_driver_profile(current: Any, update: Any) -> Dict[str, Any]:
    """
    把新提炼的偏好合并进已有偏好（两边都先规整）：preferences、places 按键合并，新值覆盖，
    新结果没提到的保留；notes 新结果非空时用新的，否则保留已有的。
    """
    old, new = normalize_driver_profile(current), normalize_driver_profile(update)
    return normalize_driver_profile({
        "preferences": {**old.get("preferences", {}), **new.get("preferences", {})},
        "places": {**old.get("places", {}), **new.get("places", {})},
        "notes": new.get("notes") or old.get("notes", []),
    })


def prompt_driver_preferences(profile: Any) -> Dict[str, Any]:
    """给模型看的驾驶员偏好：规整后只取 preferences 和 notes，不含 places。"""
    normalized = normalize_driver_profile(profile)
    return {key: normalized[key] for key in PROFILE_PROMPT_KEYS if key in normalized}


def build_profile_prompt(dialogue: str, current_profile: Any) -> str:
    """驾驶员偏好提炼 prompt：对话 + 已有偏好（规整后，含 places，供模型合并）。"""
    current = json.dumps(normalize_driver_profile(current_profile), ensure_ascii=False)
    return f"""你负责维护车载语音助手的驾驶员偏好。根据下面的对话和已有偏好，输出更新后的完整偏好。
对话里 user 是驾驶员，assistant 是语音助手。

记录规则：
1. 只记驾驶员明确表达或在对话中反复出现的稳定偏好；一次性指令不算。例如"这次调到26度"不记，"我一般开22度"记为 climate_temp=22。
2. places 只在驾驶员明确说出家或公司的地址时记录：home 是家，company 是公司；没说地址就不要填写，也不要猜。
3. 不记电话号码、证件号、银行卡号、车牌号等敏感信息。
4. 和已有偏好合并：新对话明确推翻的才修改，没有被推翻的原样保留。
5. 助手说的话只作参考，不能当成驾驶员的偏好。

字段说明：
- preferences.climate_temp：习惯的空调温度，16 到 32 之间的数字
- preferences.seat_heat：座椅加热习惯，简短描述，如"冬天常开"
- preferences.music：喜欢的音乐类型或歌手，字符串列表
- preferences.drive_mode：习惯的驾驶模式，只能是 eco、comfort、sport 之一
- preferences 里也可以加其他稳定习惯，值用字符串、数字或字符串列表
- places.home、places.company：家、公司的地址
- notes：放不进上面字段的其他稳定习惯，每条一句话，最多 {MAX_PROFILE_NOTES} 条，重要的在前

对话：
{dialogue}

已有偏好：
{current}

只输出一个 JSON 对象，结构为 {{"preferences": {{}}, "places": {{}}, "notes": []}}，按字段说明填写；
对话和已有偏好里都没有依据的字段不要填。"""


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    user_profile:     Dict[str, Any]  # 驾驶员偏好（已规整，含 places），/chat 原样传给 Request.profile
    summary:          str             # 当前会话摘要（压缩后）

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本；驾驶员偏好只输出 preferences 和 notes，不输出 places。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        preferences = prompt_driver_preferences(self.user_profile)
        if preferences:
            parts.append(f"[驾驶员偏好]\n{self._clean(json.dumps(preferences, ensure_ascii=False))}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages[-8:]:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和驾驶员偏好存 ChromaDB（持久化）。
    """

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    SUMMARY_MAX_CHARS = 800
    PROFILE_DOC_PREFIX = "user_profile:"

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
    ):
        # 构造时才导入，模块里的纯函数不依赖 chromadb、redis
        import chromadb
        import redis.asyncio as redis

        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._model  = model

        self._redis = redis.from_url(redis_url, decode_responses=True)

        # ChromaDB：优先连接独立服务（docker compose 模式），连不上则降级为本地嵌入式
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            chroma = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            chroma.heartbeat()  # 测试连接
            logger.info(f"ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"ChromaDB 服务不可用，使用本地嵌入式模式: {chroma_path}")
            chroma = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 情景记忆：存储历史对话片段
        self._episodic = chroma.get_or_create_collection("episodic")
        # 驾驶员偏好：每个用户一条文档，内容为规整后的偏好 JSON
        self._profile  = chroma.get_or_create_collection("user_profile")

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """将一条消息写入工作记忆，超阈值时自动压缩。"""
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        # 追加到 Redis 列表（左推，最新在前）
        await self._redis.lpush(key, json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        }))
        await self._redis.expire(key, 86400)  # 24h TTL

        # 超过压缩阈值时触发压缩
        if await self._redis.llen(key) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        """
        从当前工作记忆中提炼驾驶员偏好并更新存库。

        LLM 按 build_profile_prompt 提炼，结果经 normalize_driver_profile 规整、merge_driver_profile
        与已有偏好合并后存入 ChromaDB（内置 embedding，不依赖外部 API）；合并结果为空或没有变化时不写库。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        current_profile = await self.get_profile(user_id)

        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        prompt = self._safe_text(build_profile_prompt(text, current_profile))

        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=512, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            profile_data = merge_driver_profile(current_profile, json.loads(raw[s:e]))
            if not profile_data or profile_data == current_profile:
                logger.debug(f"驾驶员偏好没有变化: {user_id}")
                return

            doc_id = self._profile_doc_id(user_id)
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))

            try:
                await asyncio.to_thread(self._profile.delete, ids=[doc_id])
            except Exception:
                pass

            # 直接传 documents，让 ChromaDB 内置模型生成 embedding（不依赖 Voyage API）
            await asyncio.to_thread(
                self._profile.add,
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[{
                    "user_id": user_id,
                    "conv_id": conv_id,
                    "updated_at": datetime.now().isoformat(),
                }],
            )
            logger.info(f"驾驶员偏好已更新: {user_id}")
        except Exception as ex:
            logger.warning(f"更新驾驶员偏好失败: {ex}")

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 1. 工作记忆（当前会话最近消息）
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history = await self._search_episodic(
            user_id,
            conv_id,
            query or (recent[-1].content if recent else ""),
        )

        # 3. 驾驶员偏好（已规整）
        profile = await self.get_profile(user_id)

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        工作记忆压缩：
          1. 用 LLM 对旧消息生成摘要
          2. 摘要存 Redis（覆盖旧摘要）
          3. 旧消息存入情景记忆（ChromaDB）供跨会话检索
          4. 工作记忆只保留最近 5 条
        """
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return

        to_compress = messages[:-5]   # 保留最近 5 条
        keep        = messages[-5:]

        # LLM 摘要
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"用 2-3 句话总结以下对话的关键信息：\n{text}")
        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(extract_text_content(resp.content)).strip()
        except Exception:
            summary = f"对话包含 {len(to_compress)} 条消息（摘要生成失败）"

        # 存摘要到 Redis
        skey = self._summary_key(user_id, conv_id)
        old_summary = await self._redis.get(skey) or ""
        new_summary = await self._merge_summary(old_summary, summary)
        await self._redis.setex(skey, 86400, new_summary)

        # 旧消息存入情景记忆
        await self._store_episodic(user_id, conv_id, text, summary)

        # 重置工作记忆为最近 5 条
        key = self._wm_key(user_id, conv_id)
        await self._redis.delete(key)
        for m in keep:  # 按旧到新逐条左推，保持 Redis 中最新消息在前
            await self._redis.lpush(key, json.dumps({
                "role": m.role.value, "content": m.content,
                "ts": m.timestamp.isoformat(), "metadata": m.metadata,
            }))
        await self._redis.expire(key, 86400)
        logger.info(f"工作记忆压缩完成: {user_id}/{conv_id}，摘要 {len(summary)} 字")

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        key  = self._wm_key(user_id, conv_id)
        raws = await self._redis.lrange(key, 0, self.WORKING_MAX - 1)
        msgs = []
        for raw in reversed(raws):  # Redis lpush 最新在前，reversed 还原时序
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, conv_id: str, query: str) -> List[str]:
        """语义检索情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            results = await self._query_episodic(
                query_text,
                n_results=self.HISTORY_TOP_K,
                where={"user_id": self._safe_text(user_id), "conv_id": self._safe_text(conv_id)},
            )
            docs = self._extract_docs(results)
            if len(docs) < self.HISTORY_TOP_K:
                fallback = await self._query_episodic(
                    query_text,
                    n_results=self.HISTORY_TOP_K,
                    where={"user_id": self._safe_text(user_id)},
                )
                docs.extend(self._extract_docs(fallback))
            return self._dedupe_texts(docs)[: self.HISTORY_TOP_K]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """将压缩后的对话片段存入情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            # 直接传 documents，ChromaDB 内置模型自动生成 embedding
            await asyncio.to_thread(
                self._episodic.add,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")

    async def get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取驾驶员偏好（取最新一条），读出后经 normalize_driver_profile 规整。"""
        try:
            doc_id = self._profile_doc_id(user_id)
            direct = await asyncio.to_thread(self._profile.get, ids=[doc_id])
            if direct.get("documents"):
                return normalize_driver_profile(json.loads(direct["documents"][0]))

            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id})
            return normalize_driver_profile(self._latest_profile_from_results(results))
        except Exception:
            pass
        return {}

    async def close(self) -> None:
        """关闭异步 Redis 连接。"""
        await self._redis.aclose()

    @staticmethod
    def _wm_key(user_id: str, conv_id: str) -> str:
        return f"wm:{user_id}:{conv_id}"

    @staticmethod
    def _summary_key(user_id: str, conv_id: str) -> str:
        return f"summary:{user_id}:{conv_id}"

    @classmethod
    def _profile_doc_id(cls, user_id: str) -> str:
        return f"{cls.PROFILE_DOC_PREFIX}{user_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """转成 ChromaDB 可接受的普通 UTF-8 字符串。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """递归清洗 metadata，避免 Redis/ChromaDB 后续读写遇到非法 UTF-8。"""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value

    async def _query_episodic(
        self,
        query_text: str,
        n_results: int,
        where: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._episodic.query,
            query_texts=[query_text],
            n_results=n_results,
            where=where,
        )

    @staticmethod
    def _extract_docs(results: Dict[str, Any]) -> List[str]:
        docs = results.get("documents") or []
        if not docs:
            return []
        first = docs[0] if isinstance(docs[0], list) else docs
        return [doc for doc in first if isinstance(doc, str) and doc.strip()]

    @staticmethod
    def _dedupe_texts(values: List[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for value in values:
            text = value.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            deduped.append(text)
        return deduped

    async def _merge_summary(self, old_summary: str, new_summary: str) -> str:
        old_summary = self._safe_text(old_summary).strip()
        new_summary = self._safe_text(new_summary).strip()
        if not old_summary:
            return new_summary[: self.SUMMARY_MAX_CHARS]
        if not new_summary:
            return old_summary[: self.SUMMARY_MAX_CHARS]

        prompt = self._safe_text(
            f"""你是对话摘要器。请把下面两段摘要合并为一段不超过 {self.SUMMARY_MAX_CHARS} 个中文字符的摘要。
保留：用户偏好、关键实体、待办事项、约束条件、未解决问题。
只输出摘要正文，不要编号，不要解释。

旧摘要:
{old_summary}

新增摘要:
{new_summary}
"""
        )
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            merged = self._safe_text(extract_text_content(resp.content)).strip()
            if merged:
                return merged[: self.SUMMARY_MAX_CHARS]
        except Exception as ex:
            logger.warning(f"合并摘要失败，回退为截断拼接: {ex}")

        merged = self._safe_text(f"{old_summary}\n{new_summary}").strip()
        return merged[-self.SUMMARY_MAX_CHARS :]

    @staticmethod
    def _latest_profile_from_results(results: Dict[str, Any]) -> Dict[str, Any]:
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        if not documents:
            return {}

        candidates: List[tuple[str, Dict[str, Any], str]] = []
        for idx, doc in enumerate(documents):
            if not doc:
                continue
            metadata = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
            ts = str(metadata.get("updated_at") or metadata.get("ts") or "")
            candidates.append((ts, metadata, doc))

        if not candidates:
            return {}

        candidates.sort(key=lambda item: item[0], reverse=True)
        latest_doc = candidates[0][2]
        try:
            return json.loads(latest_doc)
        except Exception:
            return {}
