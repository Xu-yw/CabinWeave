"""
CabinWeave 车辆用户手册知识库—— 基于 ChromaDB 的 RAG 检索。

功能：
  1. 手册同步：sync_manuals(dir) 把本地手册目录同步进知识库，KnowledgeBase 初始化时调用
  2. 文档导入：add_documents 导入经 /knowledge/add、/knowledge/upload 上传的文档（metadata kind="upload"）
  3. 语义检索：search 返回最相关的片段，作为 MCP 工具 knowledge_search 的 handler

手册目录：
  - 默认 backend/manuals/（容器内 /app/manuals，由 docker compose 只读挂载），环境变量 CABINWEAVE_MANUALS_DIR
    覆盖，由 api/main.py 读取后传入；手册版权归车企，目录内容不进 git，也不打进镜像
  - 递归扫描 .md、.txt、.html / .htm；隐藏文件、隐藏目录和 README.md 跳过
  - 文本先按 UTF-8 解码，失败再试 GB18030，仍失败时按 UTF-8 忽略非法字节（decode_text）
  - HTML 用标准库 html.parser 提取正文（html_to_text）：去掉 script、style、nav、header、footer、noscript
    以及 head、template、svg；块级元素换行，h1–h4 转成 markdown 标题，列表项前加"- "；
    文档里有 main 或 article 时只取其中的内容

同步规则（sync_manuals）：
  - 每个文件按内容算 SHA-256；片段 metadata 记 kind="manual"、source（相对手册目录的路径）、source_hash、
    title（文件名去扩展名）、section（小节标题）、chunk_index、total_chunks
  - 库里已有同 source、同哈希且片段齐全 → 跳过；哈希变了或片段不齐 → 删掉该 source 的旧片段再导入；
    目录里已经没有的 source → 删掉其片段
  - 只动 kind="manual" 的片段，上传的文档不受影响
  - 目录不存在或没有可导入的文件时不导入，记 info 日志（库里原有的手册片段同样按"目录里已没有"删除），知识库可以为空
  - 单个文件读取失败、没有正文或写入失败时记 warning、计入 failed，不影响其他文件；
    返回统计 {"added", "updated", "skipped", "deleted", "failed", "chunks"} 并记 info 日志

切块（chunk_document，手册和上传文档共用）：
  1. 按 markdown 标题（# 到 ######）分节，节内按空行分段
  2. 同一节的段落依次合并，前缀加正文不超过 CHUNK_SIZE（500 字）；超长段落按句末标点和换行切开，单句仍超长时硬切
  3. 每块前面加"【标题｜小节标题】"：小节标题取所在位置最近两级标题，用" / "连接，与文档标题相同的标题不重复；
     不在任何小节里时为"【标题】"；标题和小节标题各自最多 40 字

已知局限：
  - ChromaDB 默认 embedding（all-MiniLM-L6-v2）以英文语料为主，对中文语义检索效果一般；片段前缀里的
    标题和小节名能提高命中率，tool_manager 的查询改写和 LLM 重排也能弥补一部分

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 驾驶员偏好）
  - 这里用于存储手册和上传文档（RAG 检索）
  两者是不同的 collection，互不干扰。构造时可以注入 collection（测试用），此时不连接、也不导入 ChromaDB。
"""
import asyncio
import hashlib
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MANUAL_KIND = "manual"
UPLOAD_KIND = "upload"
MANUAL_SUFFIXES = (".md", ".txt", ".html", ".htm")
HTML_SUFFIXES = (".html", ".htm")
CHUNK_SIZE = 500

_TITLE_MAX = 40          # 前缀里标题、小节标题各自的最大长度
_SECTION_LEVELS = 2      # 小节标题取最近几级标题
_BATCH_SIZE = 100        # 单次写入或删除 ChromaDB 的片段数
_UNTITLED = "未命名文档"


# ── 文本解码与 HTML 正文提取 ──────────────────────────────────────────────────

def decode_text(data: bytes) -> str:
    """按 UTF-8（可带 BOM）解码，失败再试 GB18030，仍失败时按 UTF-8 忽略非法字节。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


# 整段跳过的元素：导航、页眉页脚、脚本样式，以及 head、template、svg 这类不含正文的元素
_SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript", "head", "template", "svg"})
# 存在时只取其中内容的正文容器
_CONTENT_TAGS = frozenset({"main", "article"})
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
# 前后空一行的块级元素
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "main", "article", "aside", "ul", "ol", "dl", "table", "blockquote", "pre",
    "figure", "form", "fieldset", "address", "details", "summary", "caption", "h5", "h6",
})
# 只在开头换行的元素
_LINE_TAGS = frozenset({"br", "tr", "dt", "dd", "figcaption", "hr"})
_CELL_TAGS = frozenset({"td", "th"})


class _TextExtractor(HTMLParser):
    """收集 HTML 文本：full 为整篇正文，content 只收 main / article 里的内容。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0           # 处在几层跳过元素里
        self._content = 0        # 处在几层 main / article 里
        self._pre = 0            # 处在几层 pre 里（保留原有空白）
        self.has_content = False
        self.full: List[str] = []
        self.content: List[str] = []

    def _emit(self, text: str) -> None:
        if self._skip:
            return
        self.full.append(text)
        if self._content:
            self.content.append(text)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _CONTENT_TAGS:
            self._content += 1
            if not self._skip:
                self.has_content = True
        if tag == "pre":
            self._pre += 1
        if tag in _HEADING_LEVELS:
            self._emit("\n\n" + "#" * _HEADING_LEVELS[tag] + " ")
        elif tag in _BLOCK_TAGS:
            self._emit("\n\n")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in _LINE_TAGS:
            self._emit("\n")
        elif tag in _CELL_TAGS:
            self._emit(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _HEADING_LEVELS or tag in _BLOCK_TAGS:
            self._emit("\n\n")
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
        if tag in _CONTENT_TAGS:
            self._content = max(0, self._content - 1)

    def handle_data(self, data: str) -> None:
        if not self._pre:
            data = re.sub(r"\s+", " ", data)
        self._emit(data)


def _tidy_lines(text: str) -> str:
    """去掉每行首尾空白、空的标题和列表项，连续空行合并成一个。"""
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^(#{1,6})\s+", r"\1 ", line.strip())
        if re.fullmatch(r"#{1,6}|-", line):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """提取 HTML 正文，规则见模块说明；有 main / article 且其中有文字时只返回其中的内容。"""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    if parser.has_content:
        content = _tidy_lines("".join(parser.content))
        if content:
            return content
    return _tidy_lines("".join(parser.full))


def extract_document_text(filename: str, data: bytes) -> str:
    """按扩展名取文件正文：.html / .htm 提取 HTML 正文，其他按纯文本解码。"""
    text = decode_text(data)
    if Path(filename).suffix.lower() in HTML_SUFFIXES:
        return html_to_text(text)
    return text


# ── 切块 ──────────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
# 切句：句末标点或换行之后
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")


def _short(text: str, limit: int = _TITLE_MAX) -> str:
    """合并空白并截断到 limit 字。"""
    return " ".join(str(text or "").split())[:limit]


def _sections(text: str, doc_title: str) -> List[Tuple[str, List[str]]]:
    """按 markdown 标题分节，返回 [(小节标题, 段落列表)]；没有正文的小节不返回。"""
    sections: List[Tuple[str, List[str]]] = []
    stack: List[Tuple[int, str]] = []    # 当前所在的标题路径 [(级别, 标题)]
    current = ""
    lines: List[str] = []

    def flush() -> None:
        paragraphs = []
        for block in re.split(r"\n\s*\n", "\n".join(lines)):
            paragraph = "\n".join(line.strip() for line in block.split("\n") if line.strip())
            if paragraph:
                paragraphs.append(paragraph)
        if paragraphs:
            sections.append((current, paragraphs))
        lines.clear()

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _HEADING_RE.match(line.strip())
        if match is None:
            lines.append(line)
            continue
        flush()
        level, heading = len(match.group(1)), _short(match.group(2), limit=200)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        names = [name for _, name in stack if name and name != doc_title]
        current = _short(" / ".join(names[-_SECTION_LEVELS:]))
    flush()
    return sections


def _split_long(text: str, limit: int) -> List[str]:
    """把超长段落按句末标点和换行切成不超过 limit 字的几段，单句超长时硬切。"""
    pieces: List[str] = []
    current = ""
    for sentence in _SENTENCE_RE.split(text):
        if not sentence:
            continue
        while len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(current) + len(sentence) <= limit:
            current += sentence
        else:
            pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _pack(paragraphs: List[str], limit: int) -> List[str]:
    """同一节的段落依次合并成不超过 limit 字的正文，段落之间换行。"""
    bodies: List[str] = []
    current = ""
    for paragraph in paragraphs:
        for piece in ([paragraph] if len(paragraph) <= limit else _split_long(paragraph, limit)):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= limit:
                current += "\n" + piece
            else:
                bodies.append(current)
                current = piece
    if current:
        bodies.append(current)
    return bodies


def chunk_document(title: str, text: str, chunk_size: int = CHUNK_SIZE) -> List[Tuple[str, str]]:
    """
    把一篇文档切成 [(小节标题, 片段)]，规则见模块说明。

    片段已带"【标题｜小节标题】"前缀，前缀加正文不超过 chunk_size；正文为空时返回空列表。
    """
    raw_title = " ".join(str(title or "").split())
    doc_title = _short(raw_title) or _UNTITLED
    chunks: List[Tuple[str, str]] = []
    for section, paragraphs in _sections(text or "", raw_title):
        prefix = f"【{doc_title}｜{section}】" if section else f"【{doc_title}】"
        limit = max(chunk_size - len(prefix), 1)
        chunks.extend((section, prefix + body) for body in _pack(paragraphs, limit))
    return chunks


# ── 知识库 ────────────────────────────────────────────────────────────────────

class KnowledgeBase:
    """
    基于 ChromaDB 的车辆用户手册知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "knowledge_base"
    COLLECTION_DESCRIPTION = "CabinWeave 车辆用户手册知识库"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        manuals_dir: Optional[Any] = None,
        collection: Optional[Any] = None,
    ):
        self._use_server = False
        if collection is not None:
            # 注入的 collection（测试用）：不连接、也不导入 ChromaDB
            self._collection = collection
        else:
            self._collection = self._open_collection(chroma_host, chroma_port, chroma_path)

        # 手册目录：给了就按目录同步一次
        self.manuals_dir: Optional[Path] = Path(manuals_dir).expanduser() if manuals_dir else None
        self.last_sync: Dict[str, int] = {}
        if self.manuals_dir is not None:
            self.last_sync = self.sync_manuals(self.manuals_dir)

    def _open_collection(self, chroma_host: str, chroma_port: int, chroma_path: str) -> Any:
        """连接 ChromaDB 并取得知识库 collection：优先独立服务，连不上降级为本地嵌入式。"""
        import chromadb

        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 不传 embedding_function，使用 ChromaDB 默认的 all-MiniLM-L6-v2：在客户端计算，首次使用会下载模型
        # （Docker 镜像构建时已预下载）
        return self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": self.COLLECTION_DESCRIPTION},
        )

    # ── 手册同步 ──────────────────────────────────────────────────────────────

    def sync_manuals(self, manuals_dir: Any) -> Dict[str, int]:
        """按手册目录同步 kind="manual" 的片段，规则见模块说明；返回新增 / 变更 / 跳过 / 删除 / 失败的文件数和写入片段数。"""
        root = Path(manuals_dir).expanduser()
        stats = {"added": 0, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0, "chunks": 0}
        files = self._scan_manuals(root)
        if not files:
            logger.info(f"手册目录 {root} 不存在或没有可导入的手册（支持 {' / '.join(MANUAL_SUFFIXES)}），不导入")
        existing = self._manual_index()

        for source, path in files.items():
            try:
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                old = existing.get(source)
                if old is not None and old["hashes"] == {digest} and old["totals"] == {len(old["ids"])}:
                    stats["skipped"] += 1
                    continue

                chunks = chunk_document(path.stem, extract_document_text(path.name, data))
                if old is not None:
                    self._delete_ids(old["ids"])
                if not chunks:
                    stats["failed"] += 1
                    logger.warning(f"手册 {source} 没有可导入的正文")
                    continue
                self._add_batches(
                    [self._manual_chunk_id(source, digest, index) for index in range(len(chunks))],
                    [chunk for _, chunk in chunks],
                    [
                        {
                            "kind": MANUAL_KIND,
                            "source": source,
                            "source_hash": digest,
                            "title": path.stem,
                            "section": section,
                            "chunk_index": index,
                            "total_chunks": len(chunks),
                        }
                        for index, (section, _) in enumerate(chunks)
                    ],
                )
                stats["updated" if old is not None else "added"] += 1
                stats["chunks"] += len(chunks)
            except Exception as ex:
                stats["failed"] += 1
                logger.warning(f"手册 {source} 导入失败: {ex}")

        for source, old in existing.items():
            if source in files:
                continue
            try:
                self._delete_ids(old["ids"])
                stats["deleted"] += 1
            except Exception as ex:
                stats["failed"] += 1
                logger.warning(f"删除手册 {source} 的片段失败: {ex}")

        logger.info(
            f"手册同步完成（{root}）：新增 {stats['added']}、变更 {stats['updated']}、跳过 {stats['skipped']}、"
            f"删除 {stats['deleted']}、失败 {stats['failed']}，写入 {stats['chunks']} 个片段"
        )
        return stats

    @staticmethod
    def _scan_manuals(root: Path) -> Dict[str, Path]:
        """递归列出可导入的手册文件：{相对路径（/ 分隔）: 文件路径}；目录不存在时返回空 dict。"""
        if not root.is_dir():
            return {}
        files: Dict[str, Path] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MANUAL_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts) or path.name.lower() == "readme.md":
                continue
            files[relative.as_posix()] = path
        return files

    def _manual_index(self) -> Dict[str, Dict[str, Any]]:
        """库里已有的手册片段，按 source 分组：{source: {"ids": [...], "hashes": {...}, "totals": {...}}}。"""
        result = self._collection.get(where={"kind": MANUAL_KIND}, include=["metadatas"])
        index: Dict[str, Dict[str, Any]] = {}
        for doc_id, meta in zip(result.get("ids") or [], result.get("metadatas") or []):
            meta = meta or {}
            entry = index.setdefault(str(meta.get("source") or ""), {"ids": [], "hashes": set(), "totals": set()})
            entry["ids"].append(doc_id)
            entry["hashes"].add(meta.get("source_hash"))
            entry["totals"].add(meta.get("total_chunks"))
        return index

    @staticmethod
    def _manual_chunk_id(source: str, digest: str, index: int) -> str:
        return hashlib.md5(f"{MANUAL_KIND}:{source}:{digest}:{index}".encode("utf-8")).hexdigest()

    def _add_batches(self, ids: List[str], documents: List[str], metadatas: List[Dict[str, Any]]) -> None:
        """分批写入，ChromaDB 会自动生成 Embedding。"""
        for start in range(0, len(ids), _BATCH_SIZE):
            end = start + _BATCH_SIZE
            self._collection.add(ids=ids[start:end], documents=documents[start:end], metadatas=metadatas[start:end])

    def _delete_ids(self, ids: List[str]) -> None:
        for start in range(0, len(ids), _BATCH_SIZE):
            self._collection.delete(ids=ids[start:start + _BATCH_SIZE])

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入上传的文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        按 chunk_document 切块（带标题前缀，每片不超过 500 字），metadata 记 kind="upload"。
        """
        ids, docs, metas = [], [], []
        seen = set()

        for doc in documents:
            title = str(doc.get("title") or "").strip()
            content = str(doc.get("content") or "")
            chunks = chunk_document(title, content)

            for i, (section, chunk) in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk}".encode("utf-8")).hexdigest()
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({
                    "kind": UPLOAD_KIND,
                    "title": title,
                    "section": section,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                })

        if ids:
            self._add_batches(ids, docs, metas)
            logger.info(f"知识库导入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段；知识库为空时返回空列表。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        count = self._collection.count()
        if count == 0:
            return []
        results = self._collection.query(
            query_texts=[query],
            n_results=max(1, min(top_k, count)),
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                meta = meta or {}
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)
