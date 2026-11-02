"""
CabinWeave 车辆用户手册知识库测试：HTML 正文提取（去掉导航、页眉页脚、脚本样式，标题转 markdown，
有 main / article 时只取其中内容）、文本解码、切块（按标题和空行分段、带"【标题｜小节标题】"前缀、不超长）、
sync_manuals 的新增 / 跳过 / 变更 / 删除以及不影响上传文档、片段不齐时重新导入、目录不存在或为空时不报错、
上传文档的 kind 和切块、检索，以及 search_knowledge_base 检索为空时的提示。

知识库全部注入假 collection：不连接、也不导入 ChromaDB（不会触发 embedding 模型下载），不调用真实模型。
"""
import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.tools import MANUAL_NOT_FOUND, build_rag_tools
from mcp.knowledge_base import (
    CHUNK_SIZE,
    MANUAL_KIND,
    UPLOAD_KIND,
    KnowledgeBase,
    chunk_document,
    decode_text,
    extract_document_text,
    html_to_text,
)


# ── 假 collection ─────────────────────────────────────────────────────────────

class FakeCollection:
    """ChromaDB collection 的内存实现：按 id 存文档和 metadata，where 只支持等值条件。"""

    def __init__(self):
        self.rows = {}           # id → (document, metadata)
        self.add_calls = []
        self.deleted = []

    def count(self):
        return len(self.rows)

    def add(self, ids, documents, metadatas):
        assert len(ids) == len(set(ids)), "同一批里有重复 ID"
        assert not set(ids) & set(self.rows), "写入了已存在的 ID"
        self.add_calls.append(list(ids))
        for doc_id, document, metadata in zip(ids, documents, metadatas):
            self.rows[doc_id] = (document, dict(metadata))

    def get(self, ids=None, where=None, include=None):
        matched = [
            (doc_id, document, metadata)
            for doc_id, (document, metadata) in self.rows.items()
            if (ids is None or doc_id in ids) and all(metadata.get(k) == v for k, v in (where or {}).items())
        ]
        return {
            "ids": [doc_id for doc_id, _, _ in matched],
            "documents": [document for _, document, _ in matched],
            "metadatas": [metadata for _, _, metadata in matched],
        }

    def delete(self, ids=None, where=None):
        for doc_id in ids or []:
            self.deleted.append(doc_id)
            self.rows.pop(doc_id, None)

    def query(self, query_texts, n_results):
        """朴素检索：按查询里的字在文档里出现的个数排序。"""
        chars = set(query_texts[0])
        ranked = sorted(self.rows.values(), key=lambda row: -sum(ch in row[0] for ch in chars))[:n_results]
        return {
            "documents": [[document for document, _ in ranked]],
            "metadatas": [[metadata for _, metadata in ranked]],
            "distances": [[0.2 for _ in ranked]],
        }

    # 测试辅助
    def manual_rows(self, source=None):
        return {
            doc_id: (document, metadata)
            for doc_id, (document, metadata) in self.rows.items()
            if metadata.get("kind") == MANUAL_KIND and (source is None or metadata.get("source") == source)
        }

    def manual_sources(self):
        return sorted({metadata["source"] for _, metadata in self.manual_rows().values()})


@pytest.fixture(autouse=True)
def no_chromadb(monkeypatch):
    """导入 chromadb 会失败：保证测试里的知识库不连接、也不导入 ChromaDB。"""
    monkeypatch.setitem(sys.modules, "chromadb", None)


@pytest.fixture
def tmp_dir():
    """临时手册目录：用标准库 tempfile 创建，测试结束后自动删除。"""
    with tempfile.TemporaryDirectory(prefix="cabinweave-manuals-") as name:
        yield Path(name)


def write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


# ── HTML 正文提取与解码 ──────────────────────────────────────────────────────

MANUAL_HTML = """<!DOCTYPE html>
<html><head><title>站点标题</title><style>.nav { color: red; }</style>
<script>var tracking = 1;</script></head>
<body>
<header><div class="logo">某某汽车官网</div></header>
<nav><a href="/">首页</a> | <a href="/cars">车型</a></nav>
<aside>侧栏推荐</aside>
<main>
  <h1>胎压监测</h1>
  <p>胎压报警灯亮起时，
     请降低车速 &amp; 尽快检查。</p>
  <h2>补气方法</h2>
  <ul><li>停车并挂 P 档</li><li>按标准胎压补气</li></ul>
  <h3>标准胎压</h3>
  <table><tr><th>位置</th><th>胎压</th></tr><tr><td>前轮</td><td>250 kPa</td></tr></table>
  <h4>注意事项</h4>
  <p>第一行<br>第二行</p>
  <noscript>请开启 JavaScript</noscript>
  <script>alert("x")</script>
</main>
<footer>版权所有 © 某某汽车</footer>
</body></html>"""


def test_html_to_text_keeps_main_content_and_converts_headings():
    text = html_to_text(MANUAL_HTML)
    lines = text.split("\n")

    assert "# 胎压监测" in lines
    assert "## 补气方法" in lines
    assert "### 标准胎压" in lines
    assert "#### 注意事项" in lines
    assert "胎压报警灯亮起时， 请降低车速 & 尽快检查。" in lines
    assert "- 停车并挂 P 档" in lines and "- 按标准胎压补气" in lines
    assert "位置 胎压" in lines and "前轮 250 kPa" in lines
    assert "第一行" in lines and "第二行" in lines
    for removed in ("站点标题", "color", "tracking", "某某汽车官网", "首页", "车型", "侧栏推荐",
                    "JavaScript", "alert", "版权所有"):
        assert removed not in text, removed
    assert "\n\n\n" not in text


def test_html_to_text_without_main_uses_whole_body_minus_chrome():
    html = ("<body><header>站点头</header><nav>导航</nav><div><h2>充电</h2><p>插上充电枪。</p></div>"
            "<p>第二段</p><footer>页脚</footer></body>")
    assert html_to_text(html) == "## 充电\n\n插上充电枪。\n\n第二段"


def test_html_to_text_with_article_only_takes_article():
    html = "<body><div>文章外的内容</div><article><h1>冬季用车</h1><p>低温续航会下降。</p></article></body>"
    assert html_to_text(html) == "# 冬季用车\n\n低温续航会下降。"
    # main 里没有文字时退回整篇正文
    assert html_to_text("<body><main> </main><p>正文</p></body>") == "正文"


def test_decode_text_handles_bom_and_gb18030():
    assert decode_text("﻿胎压".encode("utf-8")) == "胎压"
    assert decode_text("胎压监测".encode("gb18030")) == "胎压监测"
    assert decode_text(b"abc") == "abc"


def test_extract_document_text_by_suffix():
    html = "<html><body><nav>导航</nav><p>正文</p></body></html>"
    assert extract_document_text("手册.HTM", html.encode("utf-8")) == "正文"
    assert extract_document_text("手册.html", html.encode("utf-8")) == "正文"
    assert extract_document_text("手册.md", "# 标题\n正文".encode("utf-8")) == "# 标题\n正文"
    assert extract_document_text("手册.txt", "纯文本".encode("gb18030")) == "纯文本"


# ── 切块 ──────────────────────────────────────────────────────────────────────

def test_chunk_document_splits_by_headings_and_adds_prefix():
    text = (
        "# 仪表指示灯\n"
        "本章介绍常见指示灯。\n\n"
        "## 胎压报警\n"
        "胎压低时亮起。\n\n"
        "请尽快补气。\n\n"
        "### 补气方法\n"
        "到加气站补气。\n\n"
        "## 低电量\n"
        "电量低于 15% 时亮起。\n"
    )
    chunks = chunk_document("仪表指示灯", text)
    assert chunks == [
        ("", "【仪表指示灯】本章介绍常见指示灯。"),
        ("胎压报警", "【仪表指示灯｜胎压报警】胎压低时亮起。\n请尽快补气。"),
        ("胎压报警 / 补气方法", "【仪表指示灯｜胎压报警 / 补气方法】到加气站补气。"),
        ("低电量", "【仪表指示灯｜低电量】电量低于 15% 时亮起。"),
    ]


def test_chunk_document_section_keeps_last_two_heading_levels():
    text = "# 驾驶\n## 辅助驾驶\n### 车道保持\n#### 开启方法\n在设置中开启。"
    assert chunk_document("用户手册", text) == [
        ("车道保持 / 开启方法", "【用户手册｜车道保持 / 开启方法】在设置中开启。"),
    ]


@pytest.mark.parametrize("chunk_size", [CHUNK_SIZE, 120])
def test_chunk_document_never_exceeds_chunk_size(chunk_size):
    sentences = "".join(f"第{i}句说明充电口盖的打开方法。" for i in range(80))
    no_punct = "低温" * 400
    text = f"## 充电\n{sentences}\n\n{no_punct}\n\n短段落一\n\n短段落二"
    chunks = chunk_document("充电说明", text, chunk_size=chunk_size)

    prefix = "【充电说明｜充电】"
    assert len(chunks) > 3
    assert all(len(chunk) <= chunk_size for _, chunk in chunks)
    assert all(chunk.startswith(prefix) and section == "充电" for section, chunk in chunks)
    bodies = "".join(chunk[len(prefix):] for _, chunk in chunks).replace("\n", "")
    assert bodies == f"{sentences}{no_punct}短段落一短段落二"


def test_chunk_document_empty_text_and_missing_title():
    assert chunk_document("标题", "") == []
    assert chunk_document("标题", "## 只有标题\n\n   \n") == []
    assert chunk_document("", "正文") == [("", "【未命名文档】正文")]


# ── 手册同步 ──────────────────────────────────────────────────────────────────

def make_manuals(root):
    write(root / "胎压监测.md", "# 胎压监测\n胎压报警灯亮起时请降低车速。\n\n## 补气\n到安全的地方补气。")
    write(root / "MEGA" / "冬季用车.html",
          "<html><body><nav>导航</nav><main><h2>低温续航</h2><p>低温时续航会下降。</p></main></body></html>")
    write(root / "充电.txt", "充电时请先插好充电枪。", encoding="gb18030")
    write(root / "README.md", "# 手册目录说明\n这里放手册。")
    write(root / ".草稿.md", "隐藏文件不导入。")
    write(root / ".cache" / "旧版.md", "隐藏目录不导入。")
    write(root / "说明.pdf", "暂不支持的格式。")


def test_sync_manuals_adds_skips_updates_and_deletes(tmp_dir, caplog):
    root = tmp_dir / "manuals"
    make_manuals(root)
    collection = FakeCollection()
    with caplog.at_level(logging.INFO, logger="mcp.knowledge_base"):
        kb = KnowledgeBase(manuals_dir=root, collection=collection)

    # 新增：3 份手册，README、隐藏文件、PDF 不导入
    assert kb.last_sync == {"added": 3, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0, "chunks": 4}
    assert collection.manual_sources() == ["MEGA/冬季用车.html", "充电.txt", "胎压监测.md"]
    assert "手册同步完成" in caplog.text
    tire = collection.manual_rows("胎压监测.md")
    assert sorted(document for document, _ in tire.values()) == [
        "【胎压监测】胎压报警灯亮起时请降低车速。",
        "【胎压监测｜补气】到安全的地方补气。",
    ]
    _, meta = next(iter(tire.values()))
    assert meta["kind"] == MANUAL_KIND and meta["title"] == "胎压监测" and meta["total_chunks"] == 2
    assert len(meta["source_hash"]) == 64
    winter = [document for document, _ in collection.manual_rows("MEGA/冬季用车.html").values()]
    assert winter == ["【冬季用车｜低温续航】低温时续航会下降。"]
    assert [d for d, _ in collection.manual_rows("充电.txt").values()] == ["【充电】充电时请先插好充电枪。"]

    # 上传的文档不受同步影响
    kb.add_documents([{"title": "临时说明", "content": "上传的内容。"}])
    upload_ids = {doc_id for doc_id, (_, m) in collection.rows.items() if m["kind"] == UPLOAD_KIND}
    assert len(upload_ids) == 1

    # 跳过：内容没变时不写也不删
    add_calls, deleted = len(collection.add_calls), len(collection.deleted)
    stats = kb.sync_manuals(root)
    assert stats == {"added": 0, "updated": 0, "skipped": 3, "deleted": 0, "failed": 0, "chunks": 0}
    assert (len(collection.add_calls), len(collection.deleted)) == (add_calls, deleted)

    # 变更：删掉旧片段再导入，其他手册不动
    old_tire_ids = set(collection.manual_rows("胎压监测.md"))
    others = {doc_id for doc_id in collection.manual_rows() if doc_id not in old_tire_ids}
    write(root / "胎压监测.md", "# 胎压监测\n新版：胎压低于 200 kPa 时报警。")
    stats = kb.sync_manuals(root)
    assert stats == {"added": 0, "updated": 1, "skipped": 2, "deleted": 0, "failed": 0, "chunks": 1}
    new_tire = collection.manual_rows("胎压监测.md")
    assert not old_tire_ids & set(new_tire)
    assert [d for d, _ in new_tire.values()] == ["【胎压监测】新版：胎压低于 200 kPa 时报警。"]
    assert others <= set(collection.manual_rows())

    # 删除：目录里没有的手册删掉其片段
    (root / "充电.txt").unlink()
    stats = kb.sync_manuals(root)
    assert stats == {"added": 0, "updated": 0, "skipped": 2, "deleted": 1, "failed": 0, "chunks": 0}
    assert collection.manual_sources() == ["MEGA/冬季用车.html", "胎压监测.md"]

    # 全程上传的文档都在
    assert upload_ids <= set(collection.rows)


def test_sync_manuals_reimports_source_with_missing_chunks(tmp_dir):
    root = tmp_dir / "manuals"
    write(root / "保养.md", "## 保养周期\n每 2 万公里保养一次。\n\n## 轮胎\n定期检查胎纹。")
    collection = FakeCollection()
    kb = KnowledgeBase(manuals_dir=root, collection=collection)
    assert len(collection.manual_rows("保养.md")) == 2

    collection.delete(ids=[next(iter(collection.manual_rows("保养.md")))])
    stats = kb.sync_manuals(root)
    assert stats["updated"] == 1 and stats["chunks"] == 2
    assert len(collection.manual_rows("保养.md")) == 2


def test_sync_manuals_missing_or_empty_directory_does_not_raise(tmp_dir, caplog):
    collection = FakeCollection()
    with caplog.at_level(logging.INFO, logger="mcp.knowledge_base"):
        kb = KnowledgeBase(manuals_dir=tmp_dir / "不存在", collection=collection)
    assert kb.last_sync == {"added": 0, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0, "chunks": 0}
    assert collection.count() == 0
    assert "不导入" in caplog.text

    empty = tmp_dir / "空目录"
    write(empty / "README.md", "# 说明")
    assert kb.sync_manuals(empty)["added"] == 0
    assert collection.count() == 0


def test_sync_manuals_removes_manuals_when_directory_becomes_empty(tmp_dir):
    root = tmp_dir / "manuals"
    write(root / "充电.md", "充电说明。")
    collection = FakeCollection()
    kb = KnowledgeBase(manuals_dir=root, collection=collection)
    kb.add_documents([{"title": "上传", "content": "上传的内容。"}])
    (root / "充电.md").unlink()

    stats = kb.sync_manuals(root)
    assert stats["deleted"] == 1
    assert collection.manual_rows() == {}
    assert collection.count() == 1


def test_sync_manuals_failed_file_does_not_block_others(tmp_dir):
    root = tmp_dir / "manuals"
    write(root / "空白.md", "  \n\n  ")
    write(root / "仅导航.html", "<body><nav>只有导航</nav></body>")
    write(root / "正常.md", "正常内容。")
    collection = FakeCollection()
    kb = KnowledgeBase(manuals_dir=root, collection=collection)
    assert kb.last_sync["failed"] == 2 and kb.last_sync["added"] == 1
    assert collection.manual_sources() == ["正常.md"]


def test_knowledge_base_without_manuals_dir_does_not_sync():
    collection = FakeCollection()
    kb = KnowledgeBase(collection=collection)
    assert kb.last_sync == {} and kb.manuals_dir is None
    assert collection.add_calls == [] and collection.count() == 0


def test_knowledge_base_without_injected_collection_needs_chromadb():
    # 没有注入 collection 时才导入 chromadb（这里 chromadb 被置为不可导入）
    with pytest.raises(ImportError):
        KnowledgeBase()


# ── 上传文档与检索 ────────────────────────────────────────────────────────────

def test_add_documents_marks_upload_kind_and_uses_prefixed_chunks():
    collection = FakeCollection()
    kb = KnowledgeBase(collection=collection)
    count = kb.add_documents([
        {"title": "冬季用车", "content": "## 低温续航\n低温时续航会下降。"},
        {"title": "冬季用车", "content": "## 低温续航\n低温时续航会下降。"},   # 重复文档不重复写入
        {"content": "没有标题的文档。"},
    ])
    assert count == 2
    rows = sorted(collection.rows.values(), key=lambda row: row[0])
    assert [document for document, _ in rows] == ["【冬季用车｜低温续航】低温时续航会下降。", "【未命名文档】没有标题的文档。"]
    assert all(metadata["kind"] == UPLOAD_KIND for _, metadata in rows)
    assert rows[0][1]["section"] == "低温续航" and rows[0][1]["title"] == "冬季用车"
    assert kb.add_documents([{"title": "空", "content": "  "}]) == 0


def test_search_returns_empty_when_knowledge_base_is_empty_and_items_otherwise(tmp_dir):
    collection = FakeCollection()
    kb = KnowledgeBase(collection=collection)
    assert kb.search("胎压报警怎么办") == []
    assert asyncio.run(kb.search_handler({"query": "胎压"}, None)) == []

    write(tmp_dir / "胎压监测.md", "胎压报警灯亮起时请降低车速。")
    kb.sync_manuals(tmp_dir)
    items = kb.search("胎压报警", top_k=5)
    assert items == [{"title": "胎压监测", "content": "【胎压监测】胎压报警灯亮起时请降低车速。", "score": 0.8, "chunk": 0}]
    assert kb.doc_count == 1


# ── search_knowledge_base 检索为空时的提示 ────────────────────────────────────

class FakeToolManager:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def search_with_rewrite(self, tool_name, query, top_k=5):
        self.calls.append((tool_name, query, top_k))
        return self.result


def run_rag(result, query="胎压报警怎么办"):
    manager = FakeToolManager(result)
    spec = build_rag_tools(manager)["search_knowledge_base"]
    req = SimpleNamespace(message=query)
    return manager, asyncio.run(spec.handler(req, {"query": query}))


def test_search_knowledge_base_empty_result_says_manual_has_nothing():
    assert MANUAL_NOT_FOUND == "手册里没有找到相关内容，建议查看车机中的用户手册或联系售后。"
    manager, result = run_rag(SimpleNamespace(success=False, data=[], error="所有子查询均无结果", reranked=False))
    assert manager.calls == [("knowledge_search", "胎压报警怎么办", 5)]
    assert result["success"] is True and result["found"] is False
    assert result["message"] == MANUAL_NOT_FOUND and result["results"] == []

    _, result = run_rag(SimpleNamespace(success=True, data=[], reranked=False))
    assert result["found"] is False and result["message"] == MANUAL_NOT_FOUND


def test_search_knowledge_base_results_and_errors_unchanged():
    items = [{"title": "胎压监测", "content": "【胎压监测】胎压报警灯亮起时请降低车速。", "score": 0.8}]
    _, result = run_rag(SimpleNamespace(success=True, data=items, reranked=True))
    assert result["success"] is True and result["results"] == items and result["reranked"] is True
    assert "message" not in result

    _, result = run_rag(SimpleNamespace(success=False, data=None, error="工具不存在: knowledge_search"))
    assert result["success"] is False and result["error"] == "工具不存在: knowledge_search"
