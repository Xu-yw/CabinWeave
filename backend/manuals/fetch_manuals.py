"""
CabinWeave 手册抓取脚本：从理想汽车官网帮助中心抓取纯电车型的公开页面，转成 Markdown 存到本目录。

理想汽车的完整《用户手册》在车机里，官网没有公开版本；这里抓取的是官网帮助中心按车型公开的
用车须知、道路救援须知、充电服务、家用充电桩、车辆售后指南。只取正文（div#article），
h1–h4 转成二级标题，图片和脚本丢掉。三款车型内容完全相同的页面合并成一份"理想纯电车型-…"，
避免检索时返回重复片段。

页面版权归理想汽车，抓取结果只放在本地（本目录除 README.md 和本脚本外都在 .gitignore 里），
后端启动时由知识库按目录同步导入（见 mcp/knowledge_base.py）。

用法（在 backend 目录下）：
  python manuals/fetch_manuals.py            # 输出到本脚本所在目录
  python manuals/fetch_manuals.py <输出目录>
只打印每个文件的字数和小节数，不打印正文。只用标准库。
"""
import re
import sys
import time
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

# 车型 → 帮助中心车型 ID
MODELS = {"理想i6": "5453", "理想i8": "5433", "理想MEGA": "5355"}
# 车型 → {页面名: 页面 ID}
PAGES = {
    "理想i6": {"用车须知": "5468", "道路救援须知": "5467", "充电服务": "5469", "家用充电桩": "5465", "车辆售后指南": "5466"},
    "理想i8": {"用车须知": "5449", "道路救援须知": "5448", "充电服务": "5450", "家用充电桩": "5446", "车辆售后指南": "5447"},
    "理想MEGA": {"用车须知": "5375", "道路救援须知": "5374", "充电服务": "5376", "家用充电桩": "5372", "车辆售后指南": "5373"},
}
URL = "https://www.lixiang.com/help/support/{model_id}/{page_id}.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NOTICE = "版权归理想汽车所有，仅供本地检索使用。"

_BLOCK = {"p", "div", "li", "center", "tr", "table", "ul", "ol", "section"}
_HEAD = {"h1", "h2", "h3", "h4"}
_SKIP = {"script", "style", "svg", "noscript"}
_VOID = {"br", "img", "hr", "input", "meta", "link"}
_MODEL_RE = re.compile("|".join(re.escape(name) for name in MODELS))


class ArticleParser(HTMLParser):
    """只取 id="article" 元素内的文字：h1–h4 转成二级标题，块级元素换行。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0        # 进入 article 后的嵌套深度，0 表示不在 article 内
        self._skip = 0
        self._in_head = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if self._depth == 0:
            if dict(attrs).get("id") == "article":
                self._depth = 1
            return
        if tag in _VOID:
            if tag == "br":
                self._parts.append("\n")
            return
        self._depth += 1
        if tag in _SKIP:
            self._skip += 1
        elif tag in _HEAD:
            self._parts.append("\n\n## ")
            self._in_head = True
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if self._depth == 0 or tag in _VOID:
            return
        self._depth -= 1
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _HEAD:
            self._parts.append("\n\n")
            self._in_head = False
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._depth and not self._skip:
            self._parts.append(data.replace("\n", " ") if self._in_head else data)

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).replace("\xa0", " ").splitlines()]
        lines = [line for line in lines if line != "##"]          # 去掉空标题
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def fetch_body(url: str) -> str:
    """抓取一个页面，返回正文 Markdown。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    parser = ArticleParser()
    parser.feed(html)
    return parser.text()


def write_doc(path: Path, title: str, source: str, body: str) -> None:
    path.write_text(f"# {title}\n\n> 来源：理想汽车官网帮助中心 {source}。{NOTICE}\n\n{body}\n", encoding="utf-8")
    sections = body.count("\n## ") + body.startswith("## ")
    print(f"{path.name}\t{len(body)} 字\t{sections} 个小节", flush=True)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    # 1. 抓取全部页面：{页面名: {车型: (URL, 正文)}}
    fetched = {}
    for model, pages in PAGES.items():
        for name, page_id in pages.items():
            url = URL.format(model_id=MODELS[model], page_id=page_id)
            fetched.setdefault(name, {})[model] = (url, fetch_body(url))
            time.sleep(1)

    # 2. 三款车型正文相同（车型名换掉后一致）的合并成一份，其余按车型各存一份
    for name, by_model in fetched.items():
        bodies = {_MODEL_RE.sub("理想汽车", body) for _, body in by_model.values()}
        if len(bodies) == 1:
            urls = "、".join(url for url, _ in by_model.values())
            models = "、".join(model.replace("理想", "") for model in by_model)
            write_doc(out / f"理想纯电车型-{name}.md", f"理想纯电车型（{models}）{name}",
                      f"{urls}（{today} 抓取，各车型内容相同）", bodies.pop())
            continue
        for model, (url, body) in by_model.items():
            write_doc(out / f"{model}-{name}.md", f"{model} {name}", f"{url}（{today} 抓取）", body)


if __name__ == "__main__":
    main()
