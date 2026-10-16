"""构建知识库：读 Zotero → 写 Markdown → 切片 → 建索引 → 算向量 → 写 MANIFEST。

被 convert.py（命令行入口）调用，也可以直接 import 用于测试。

对经验层（experience / item_weight）**只读不改**：重建知识库永远不会丢掉你的经验。
"""

from __future__ import annotations
# --- 让本模块可以独立运行（不依赖调用者先配好 sys.path）---
# 本项目模块平铺在 offline/ 与 online/ 下，用 `from schemas import ...` 这种
# 平铺方式互相导入，因此要求对应目录在 sys.path 里。调用者通常配好了，但
# **直接运行本文件**时没有"调用者"，就会 ModuleNotFoundError。
# 本机踩过同类问题：只把 offline/ 加进 sys.path 时，「列出全部经验」会报
#   ModuleNotFoundError: No module named 'searcher'（它在 online/ 下）。
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.dirname(_HERE)
for _sub in ("offline", "online"):
    _p = _os.path.join(_ROOT, _sub)
    if _os.path.isdir(_p) and _p not in _sys.path:
        _sys.path.append(_p)
# 清掉临时名，别污染本模块的命名空间
del _os, _sys, _HERE, _ROOT, _sub, _p
# --- 路径设置结束 ---

import json
import math
import os
import re
import time

import schemas as S
from zreader import ZoteroReader, clean_text, html_to_markdown, split_pages  # noqa: F401


# 切片参数：800-1200 字符、重叠 150。学术段落大多 300-900 字符，
# 这个粒度既能被向量模型吃下（e5 上限 512 token），又保留足够上下文。
CHUNK_TARGET = 1000
CHUNK_OVERLAP = 150
CHUNK_MIN = 120

# 父子切块（2026-10-11 口径，设计方案附录 J.4）：
#   父块（level=0）= 节/章的整节全文，≤3000 字（超了按段落拆「（续 N）」），
#     只作「命中子块时返回上下文」的载体，**不进 FTS、不建向量**；
#   子块（level=1）= 节内 300 字、重叠 60，**FTS 与向量都建在这一层**。
# 堆缩点：父块能吃下整节（信息全），子块短（关键词路精度高、向量不过饱和），
# 两头的好处都要 —— 见附录 J.4 为什么推翻「FTS 建在父块」。
# 子块大小与重叠：实验开关 ZOTERO_KB_CHILD_TARGET（默认 300）。
# ⚠ 这两个是**构建期**参数：改了必须全量重建，且要重算向量（重切会换 chunk_id）。
CHILD_TARGET = int(os.environ.get("ZOTERO_KB_CHILD_TARGET") or 300) or 300
CHILD_OVERLAP = int(os.environ.get("ZOTERO_KB_CHILD_OVERLAP") or 60)
CHILD_MIN = 20
PARENT_MAX = 3000


def _pfts() -> bool:
    """K300+PFTS 档：**父块也建一份 FTS**（ZOTERO_KB_PFTS=1，默认关）。

    动机：关键词路偏长块（覆盖度高），父块进 FTS 把这份优势用「另一路加分」补回来，
    同时保住子块的高精度。父块命中**不直接返回**，只给该篇加分（见 searcher.search）。
    """
    return str(os.environ.get("ZOTERO_KB_PFTS") or "").strip().lower() \
        in ("1", "true", "yes", "on")


# ⚠ 时间戳**只有一份实现**：`schemas.now_iso()`。
#   本模块原来自己写了一份（2026-10-15 清理），而它写进 `items.built_at` 的值
#   与 experience / edits / trash 的时间戳是同一列口径 —— 分开写迟早错配。
#   （保留这个名字是因为 convert.py 在调 `C.now_iso()`。）
now_iso = S.now_iso


# ---------------------------------------------------------------- 切片


def split_into_sentences(para: str) -> list[str]:
    """把段落切成句子。

    坑：中文句号后面**没有空格**（"……方案。本文以……"），
    所以不能用 `(?<=[。！？.!?])\\s+` —— 那样整个中文段落会被当成一句、
    退化成几千字符的巨块，切片就白做了。这里用零宽断言只看标点，
    再丢掉切出来的空白片段。
    """
    parts = re.split(r"(?<=[。！？；.!?;])\s*", para)
    return [p.strip() for p in parts if p and p.strip()]


def join_sentences(left: str, right: str) -> str:
    """拼句子时按前一句的收尾字符决定要不要空格：

    中文标点后不加（加了反而难看），英文标点后加一个。
    """
    if not left:
        return right
    if not right:
        return left
    if left[-1] in "。！？；、，）】》":
        return left + right
    return f"{left} {right}"


def chunk_text(text: str, target: int = CHUNK_TARGET, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落边界切片，保留重叠。

    先把文本按空行切成段落，再贪心堆积到 target 附近。单个超长段落
    按句子（中英文标点都认）再切一刀，避免出现几千字符的巨块。
    """
    if not text or len(text) < CHUNK_MIN:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    # 超长段落先按句子拆
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= target:
            pieces.append(para)
            continue
        buf = ""
        for sent in split_into_sentences(para):
            if len(buf) + len(sent) + 1 <= target:
                buf = join_sentences(buf, sent)
            else:
                if buf:
                    pieces.append(buf)
                buf = sent
        if buf:
            pieces.append(buf)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + len(piece) + 1 <= target:
            current = join_sentences(current, piece)
        else:
            chunks.append(current)
            # 重叠：把上一块尾部接到新块开头，保证跨块语义不丢
            tail = current[-overlap:] if len(current) > overlap else current
            current = join_sentences(tail, piece)
    if current:
        chunks.append(current)

    return [c for c in (c.strip() for c in chunks) if len(c) >= CHUNK_MIN]


# ---------------------------------------------------------------- 分节（父子切块）

RE_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
RE_CHAP = re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*[章节]")
# 开头的编号：1 / 1.2 / 1.2.3 / 1.2.3.4 …（后面必须跟空白或标点，免得把
# 「3D 打印」这种开头的正文当成标题）
RE_NUM_LEAD = re.compile(r"^(\d+(?:\.\d+)*)(?=\s|[.、,，:：)）(（]|$)")
RE_SENT_END = re.compile(r"[。！？；!?;]\s*")
# 目录行：一串点线/省略号后跟页码（"... 5" / "……9" / "· · · 12"）
RE_TOC_LINE = re.compile(r"\.{2,}\s*\d+\s*$|…{2,}\s*\d*\s*$|[·．]{2,}")

# 节的语义类别（附录 F.4：参考文献/致谢/附录/目录默认不进检索；综述章加权）
KIND_RULES = (
    # ⚠ 中文标题常被排版成「参 考 文 献」（带空格）—— 实测 9 篇的参考文献
    #   因此被当成正文索引了进去（`参 考 文 献` 匹配不上 `参考文献`）。
    (re.compile(r"参\s*考\s*文\s*献|references?", re.I), "refs"),
    (re.compile(r"致\s*谢|acknowledg", re.I), "ack"),
    (re.compile(r"攻读学位期间|科研成果|发表论文|成果清单"), "ack"),
    (re.compile(r"附\s*录|appendix", re.I), "appendix"),
    (re.compile(r"目\s*录|table of contents", re.I), "toc"),
    (re.compile(r"研究现状|综述|related work|literature review|国内外.{0,6}(进展|现状)", re.I),
     "review"),
)
# 这些类别里的「标题行」多半是参考文献条目/成果清单，别再分节（否则几百个假节）
FLAT_KINDS = ("refs", "ack", "toc")
# 只有这些类别会被子节**继承**（review 不继承：它会顺着传染整篇正文）
INHERIT_KINDS = ("refs", "ack", "toc", "appendix")
# 默认不进 FTS/向量（开关：ZOTERO_KB_INDEX_NONBODY=1）
NONBODY_KINDS = ("toc", "refs", "ack", "appendix")


def heading_level(title: str) -> int:
    """从标题**编号**推层级：第N章/1 -> 1、1.1 -> 2、1.1.1 -> 3、其余 0（继承）。

    ⚠ 不能用 MinerU 的 `paragraph_title.level` —— 本机实测 94 篇 1756 个标题的
    level **全是 2**，照它推会把「0 引言」与「1.1 …」判成同级（附录 J.4）。
    """
    t = str(title or "").strip()
    if RE_CHAP.match(t):
        return 1
    m = RE_NUM_LEAD.match(t)
    if not m:
        return 0
    # 1 -> 1、1.2 -> 2、1.2.3 -> 3；四层以上按 3 级算（再深就没有父块负担了）
    parts = m.group(1).count(".") + 1
    return min(3, parts)


def section_kind_of(title: str, inherit: str = "body") -> str:
    """标题 -> section_kind；匹配不到就继承上一节（子节跟着「参考文献」走）。"""
    t = str(title or "")
    for rx, kind in KIND_RULES:
        if rx.search(t):
            return kind
    return inherit or "body"


def _index_nonbody() -> bool:
    """是否把 refs/ack/appendix/toc 也建进 FTS 与向量（默认关）。"""
    return str(os.environ.get("ZOTERO_KB_INDEX_NONBODY") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def sectionize_pages(pages: list, item_type: str = "") -> list:
    """把**有效正文**（逐页 markdown，已过修订层）分节。

    返回**扁平**的节列表（顺序 = 原文顺序），每节：
        {title, level, kind, page_from, page_to, text, parent}
    `parent` 是父节在列表里的下标（-1 = 顶层）。

    为什么吃 pages 而不是 structured_content：修订层改的是 pages 这一层，
    绕回 structured_content 等于把用户的修订架空（附录 J.4 明确要求）。
    `item_type` 目前只作记录 —— 学位论文的「章 -> 节」与期刊的「节 -> 小节」
    在编号规则下是同一件事（章/节都是 level<=2 的父块）。
    """
    secs: list = []
    stack: list = []          # [(level, index)]
    cur = [-1]

    def open_sec(title, level, kind):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else -1
        # 类别继承只认**结构性非正文**的父节（参考文献/致谢/目录/附录）。
        # ⚠ 别让它从"上一节"顺着传下去：目录里冒出来的一个 review 小节会把
        #   后面整篇正文都染成 review（本机实测：学位论文第 2 章全被判成综述）。
        if (kind or "body") == "body" and parent >= 0 \
                and secs[parent]["kind"] in INHERIT_KINDS:
            kind = secs[parent]["kind"]
        idx = len(secs)
        secs.append({"title": str(title or ""), "level": int(level),
                     "kind": kind or "body", "page_from": 0, "page_to": 0,
                     "text": "", "parent": parent})
        stack.append((int(level), idx))
        cur[0] = idx

    prev_level = [1]

    def append_body(raw, pno):
        if cur[0] < 0:
            open_sec("", 1, "body")
            prev_level[0] = 1
        s = secs[cur[0]]
        s["text"] = (s["text"] + "\n" + raw) if s["text"] else raw
        if not s["page_from"]:
            s["page_from"] = pno
        s["page_to"] = pno

    for pno, page_text in enumerate(pages or [], start=1):
        for raw in str(page_text or "").splitlines():
            m = RE_HEAD.match(raw)
            if not m:
                append_body(raw, pno)
                continue
            title = m.group(2).strip()
            kind_here = section_kind_of(title, "")
            lvl = heading_level(title)
            if lvl <= 0:
                # 无编号标题：**继承上一节的层级**（规范：其余继承上一节）
                lvl = prev_level[0]
            # 处在「扁平区」（目录/参考文献/致谢）里时：比它更深的标题一律当正文
            # —— 那些是目录行 / 文献条目，不是节。点线+页码的目录行更是无条件当正文。
            # 只有不深于扁平区、且不像目录行的标题才走出扁平区（例：目录后的「第1章 绪论」）。
            flat_level = None
            for _sl, _si in reversed(stack):
                if secs[_si]["kind"] in FLAT_KINDS:
                    flat_level = _sl
                    break
            if flat_level is not None \
                    and (lvl > flat_level or RE_TOC_LINE.search(raw)):
                append_body(raw, pno)
                continue
            prev_level[0] = lvl
            open_sec(title, lvl, kind_here)
    # 丢掉完全空的节（只有标题、没有正文、也没有子节）
    keep = []
    for i, s in enumerate(secs):
        has_child = any(x["parent"] == i for x in secs)
        if str(s["text"]).strip() or has_child:
            keep.append(i)
    remap = {old: new for new, old in enumerate(keep)}
    out = []
    for old in keep:
        s = dict(secs[old])
        p = s.get("parent", -1)
        s["parent"] = remap.get(p, -1)
        out.append(s)
    return out


def _path_of(secs: list, si: int) -> str:
    """节的 section_path：祖先标题用 > 连起来（"第2章 > 2.3 反演方法"）。"""
    names = []
    j = si
    seen = set()
    while 0 <= j < len(secs) and j not in seen:
        seen.add(j)
        t = str(secs[j].get("title") or "").strip()
        if t:
            names.append(t)
        j = secs[j].get("parent", -1)
    return " > ".join(reversed(names))


def _indexable(kind: str, on: bool = False) -> bool:
    return bool(on) or (kind or "body") not in NONBODY_KINDS


def _subtree(secs: list, si: int):
    """一节及其子孙的全文（父块正文）＋ {节索引: 在全文里的偏移}。"""
    subs: dict = {}
    for i, s in enumerate(secs):
        p = s.get("parent", -1)
        if p >= 0:
            subs.setdefault(p, []).append(i)
    buf: list = []
    offs: dict = {}
    pos = [0]

    def walk(i):
        offs[i] = pos[0]
        t = str(secs[i].get("text") or "").strip()
        if t:
            if buf:
                buf.append("\n\n")
                pos[0] += 2
            offs[i] = pos[0]
            buf.append(t)
            pos[0] += len(t)
        for j in subs.get(i, []):
            walk(j)

    walk(si)
    return "".join(buf), offs


def _split_parent(text: str, limit: int = PARENT_MAX) -> list:
    """父块正文按段落拆到 ≤limit 字，返回 [(起始偏移, 文本)]。"""
    t = str(text or "")
    if len(t) <= limit:
        return [(0, t)]
    parts: list = []
    cur = ""
    cur_start = 0
    pos = 0
    for chunk in re.split(r"(\n\s*\n)", t):
        if cur and len(cur) + len(chunk) > limit:
            parts.append((cur_start, cur.rstrip()))
            cur = ""
            cur_start = pos
        cur += chunk
        pos += len(chunk)
    if cur.strip():
        parts.append((cur_start, cur.rstrip()))
    return parts or [(0, t)]


def _cut_offsets(text: str, target: int = CHILD_TARGET,
                 overlap: int = CHILD_OVERLAP) -> list:
    """把一节正文切成子块，返回 [(起始偏移, 文本)]。

    与老的 chunk_text 的区别：**带偏移**（父块拆成多段时要按偏移归到那一段），
    另外按句子边界收口（中文句号后没有空格，零宽看标点）。
    """
    t = str(text or "")
    if not t.strip():
        return []
    out: list = []
    n = len(t)
    i = 0
    while i < n:
        end = min(n, i + target)
        if end < n:
            m = RE_SENT_END.search(t, max(end - 1, i))
            if m and m.end() <= i + target + overlap:
                end = m.end()
        piece = t[i:end].strip()
        if piece and len(piece) >= CHILD_MIN:
            out.append((i, piece))
        if end >= n:
            break
        nxt = end - overlap
        i = nxt if nxt > i else i + 1
    if not out and t.strip():
        out = [(0, t.strip())]
    return out


# ---------------------------------------------------------------- L2 清洗（切片/向量专用）
#
# 三层文本（设计方案附录 J.1；2026-10-12 补 L2）：
#   L0 MinerU 原版（mineru/<key>/，不动）
#     -> L1 fulltext/<key>.md（给人看、也给 AI 全文读，**保留引文上标与图表**）
#     -> L2 = clean_for_index(L1)（**只用来切片与向量化**）
#
# L2 是纯函数、不落盘、不可编辑：L1 是契约、L2 是 L1 的推导 —— 一旦落成
# 文件，用户改 L1 的修订层时 L2 就跟不上，等于要维护两份可编辑稿。
#
# 实测收益（2026-10-12，本机 94 篇；**如实记，别夸大**）：
#   · 可索引子块 11284 里含上/下标的 1534 块（13.6%），平均每块 0.283 个，
#     标签只占字符 1.23%（46092 / 3750710）—— 剥上标是**小收益**；
#   · 真正的大头是第 3 条**页首期刊版权块**（一篇 p.1 开头能有 8~十几天这类行）。
# 清洗规则（按序判：先方括号 -> 再纯数字 -> 最后看前一个字符）：
#   1 删**引文型** <sup>：内容是纯数字 / 数字列表 / 方括号（含全角），且前一个
#     字符**不是**拉丁字母、**不是**数字、**不是** >。实测：汉字后的 1622 个
#     全是 [1] 这类引文；拉丁字母后 308 个是 m² 单位、> 后 71 个是
#     x<sub>i</sub><sup>2</sup> 这类数学 —— 两档都要留。
#   2 删正文里的 [n] / [n-m] / [n,m] 编号（含全角）。**必须先给
#     $$…$$ 公式上占位符** —— 公式里 \begin{bmatrix} 的方括号极多。
#   3 删页首期刊版权块那类**独立行**（http:// / E-mail / Tel / ISSN /
#     收稿日期 / 基金项目 / 中图分类号 / 文献标志码 / 文章编号 / DOI:）。
# **明确不动**：<sub>（513 个全是数学下标）、单位与数学上标、摘要/关键词/正文/
#   图注、公式、## p.N 锚点（它本来就不在 pages 里、切片从来不含）。
# **不要**把清洗写进 fulltext_markdown() —— 那样人看的稿子也被改了，就白分层了。

# 引文型 <sup> 的内容：纯数字 / 数字列表 / 方括号（含全角）。
RE_SUP = re.compile(r"<sup>(.*?)</sup>", re.I | re.S)
RE_SUP_NUM = re.compile(r"\d+(?:\s*[-–—,，、]\s*\d+)*")
# 正文里的 [n] / [n-m] / [n,m]（含全角）；**紧跟** ( 的是 md 链接标签，不能删。
# ⚠ 负向前瞻里不要写 \s* —— 实测 [50] (Eq. (3)) 会被当成链接而漏删（markdown
#   链接不允许 ] 与 ( 之间有空格）。
RE_CITE_NUM = re.compile(r"\[(\d+(?:\s*[-–—,，、]\s*\d+)*)\](?!\()")
RE_CITE_NUM_FW = re.compile(r"［(\d+(?:\s*[-–—,，、]\s*\d+)*)］")
# $$…$$ 公式块（非贪婪、跨行）；处理前先换成占位符。
RE_EQ_BLOCK = re.compile(r"\$\$.*?\$\$", re.S)
# 规则 1 保留下来的 <sup>（m²、x_i² 这类单位/数学）也是保护区：规则 2 不该
# 钻进去删 [35]（否则 5<sup>[35]</sup> 会变成 <sup></sup> 这种空标签残渣）。
RE_SUP_SPAN = re.compile(r"<sup>.*?</sup>", re.I | re.S)
_L2_EQ_PH = "\x00l2eq%d\x00"
# 页首期刊版权块：以这些开头的独立行。拉丁前缀加 \b，免得把 Telescope 当 Tel。
RE_COPY_LINE = re.compile(
    r"^(?:https?://|E-?mail\b|Tel\b|ISSN\b|DOI\s*[:：]"
    r"|收稿日期|基金项目|中图分类号|文献标志码|文章编号)")


def _sup_is_citation(content: str) -> bool:
    """<sup> 的内容是不是「引文型」：纯数字 / 数字列表 / 方括号（含全角）。"""
    c = str(content or "").strip()
    if not c:
        return False
    inner = c
    if len(c) >= 2 and ((c[0] == "[" and c[-1] == "]")
                        or (c[0] == "［" and c[-1] == "］")):
        inner = c[1:-1]
    return bool(RE_SUP_NUM.fullmatch(inner))


def _sup_prev_protects(prev: str) -> bool:
    """前一个字符是不是「数学/单位」的护栏：拉丁字母、数字、>。

    > 保护的是 x<sub>i</sub><sup>2</sup> 这种复合下标（标签结束符）。
    注意**只认 ASCII 拉丁字母** —— 汉字 isalpha() 也为真，若用它会把
    1622 个汉字后的引文全留下。
    """
    if not prev:
        return False
    if prev == ">":
        return True
    if prev in "0123456789":
        return True
    return prev.isascii() and prev.isalpha()


def _drop_citation_sups(text: str) -> str:
    if "<sup" not in text.lower():
        return text
    out: list[str] = []
    pos = 0
    for m in RE_SUP.finditer(text):
        out.append(text[pos:m.start()])
        prev = text[m.start() - 1] if m.start() > 0 else ""
        if _sup_is_citation(m.group(1)) and not _sup_prev_protects(prev):
            pass                       # 连标签带内容一起删
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _drop_citation_numbers(text: str) -> str:
    """删正文里的 [n]/[n-m]/[n,m] 编号。

    **两块保护区**：`$$…$$` 公式（\begin{bmatrix} 的方括号极多）与规则 1 保留下来的
    `<sup>…</sup>`（m² / x_i²）。做法都是先换占位符、处理完再换回。
    """
    blocks: list[str] = []

    def _stash(m):
        blocks.append(m.group(0))
        return _L2_EQ_PH % (len(blocks) - 1)

    tmp = RE_EQ_BLOCK.sub(_stash, text)
    tmp = RE_SUP_SPAN.sub(_stash, tmp)
    tmp = RE_CITE_NUM.sub("", tmp)
    tmp = RE_CITE_NUM_FW.sub("", tmp)
    for i, b in enumerate(blocks):
        tmp = tmp.replace(_L2_EQ_PH % i, b)
    return tmp


def _drop_copy_lines(text: str) -> str:
    if "http" not in text and "\n" not in text:
        return text
    lines = text.split("\n")
    keep = [ln for ln in lines if not RE_COPY_LINE.match(ln.strip())]
    return "\n".join(keep)


def clean_for_index(text: str) -> str:
    """把 L1 正文清洗成 **L2**（只用于切片与向量化）。纯函数、幂等。

    见本节顶部注释；规则 1/2/3 按序执行（1 必须在 2 之前：<sup>[1]</sup>
    要先被当成引文上标整体删掉，否则规则 2 先删掉 [1] 就只剩空标签）。
    """
    if not text:
        return str(text or "")
    s = _drop_citation_sups(str(text))
    s = _drop_citation_numbers(s)
    s = _drop_copy_lines(s)
    return s


DOC_MAX_CHARS = 1200


def doc_chunk(item: S.Item, summary: str = "") -> dict | None:
    """全篇层（depth=2）的块：给「整篇在讲什么」这类宽问题一个能命中的载体。

    文本**优先用本地模型写好的摘要与关键词**（也就是 views/<key>.tldr.md 的产物：
    meta 表 ai_summary:<key>，由 judge.py summarize 写、kbviews 渲染），没有就退回
    Zotero 的摘要；两者都没有就**不建** —— 拿正文硬凑会变成第二份全文，反而稀释检索。

    它与 level 正交：level=1（可索引的子块），但 depth=2。检索侧默认
    （ZOTERO_KB_DEPTH 这个开关关着）会把它从候选里过滤掉，所以默认行为一个字不变。
    """
    body = str(summary or "").strip() or str(
        getattr(item, "abstract", "") or "").strip()
    if not body:
        return None
    title = str(getattr(item, "title", "") or "").strip()
    head = "；".join(x for x in (
        str(getattr(item, "author_line", "") or "").strip(),
        str(getattr(item, "year", "") or "").strip()) if x)
    text = title + (("（" + head + "）") if head else "") + "\n" + body
    pages = getattr(item, "fulltext_pages", None) or []
    return {
        "level": 1, "depth": 2, "kind": "fulltext", "parent_ref": "",
        "page": 1, "page_from": 1, "page_to": len(pages),
        "text": text[:DOC_MAX_CHARS], "section_path": "", "section_kind": "doc",
        "indexable": True,
    }


def collect_chunks(item: S.Item, pages: list[str],
                   doc_summary: str = "") -> list[dict]:
    """把一篇文献切成**父子两层**（2026-10-11 口径，设计方案附录 J.4）。

    · 父块（level=0）：节/章的**整节全文**，≤3000 字（超了按段落拆「（续 N）」）；
      只作「命中子块时返回上下文」的载体，**不进 FTS、不建向量**；
    · 子块（level=1）：节内 300 字、重叠 60，**FTS 与向量都建在这一层**，
      文本前缀挂 section_path（"第2章 > 2.3 反演方法"）；
    · 标注与笔记只有一层（level=1、无父块）；
    · toc / refs / ack / appendix 存库但不进 FTS/向量（开关 ZOTERO_KB_INDEX_NONBODY=1）。

    返回顺序：**全部父块在前、子块在后** —— 调用方按顺序插入即可保证
    `parent_id` 指向已存在的行（父块先插）。父块带 `ref`，子块带 `parent_ref`。
    """
    secs = sectionize_pages(pages, str(getattr(item, "item_type", "") or ""))
    # L2 清洗（2026-10-12）：正文（fulltext）在**切片之前**过一遍 clean_for_index，
    # 父块与子块吃到同一份清洗后的文本，parent_ref 的偏移也天然一致。
    # **标注与笔记不清洗** —— 那是人工文本，一个字都不动（它们在下面单独成块）。
    # 为什么改 secs 而不是改 pages：fulltext 落盘吃的是 pages（L1），清洗只该
    # 作用在「要被切片」的这一份上 —— 这样 L1 是契约、L2 是推导，天然一致。
    for _sec in secs:
        _sec["text"] = clean_for_index(str(_sec.get("text") or ""))
    index_nonbody = _index_nonbody()
    parents: list = []
    children: list = []
    store: dict = {}
    ref_seq = [0]

    def new_ref():
        ref_seq[0] += 1
        return "r%d" % ref_seq[0]

    def parent_ref_for(si, off):
        """把「某节内偏移 off」映射到最近的 ≤2 级祖先父块的某一段。"""
        j = si
        while j >= 0 and secs[j]["level"] > 2:
            j = secs[j].get("parent", -1)
        if j < 0 or j not in store:
            return ""
        info = store[j]
        base = info["offs"].get(si, 0)
        off = base + off
        ref = info["parts"][0][1] if info["parts"] else ""
        for start, r in info["parts"]:
            if start <= off:
                ref = r
            else:
                break
        return ref

    for si, sec in enumerate(secs):
        kind = sec.get("kind") or "body"
        text = str(sec.get("text") or "").strip()
        if sec["level"] <= 2:
            subtree, offs = _subtree(secs, si)
            parts = _split_parent(subtree)
            refs = []
            for pi, (start, ptext) in enumerate(parts):
                title = sec.get("title") or "（无标题节）"
                if len(parts) > 1:
                    title = "%s（续 %d）" % (title, pi + 1)
                ref = new_ref()
                parents.append({
                    "ref": ref, "level": 0, "depth": 1, "kind": "fulltext", "title": title,
                    "page": sec.get("page_from") or 0,
                    "page_from": sec.get("page_from") or 0,
                    "page_to": sec.get("page_to") or 0,
                    "text": ptext, "section_path": _path_of(secs, si),
                    "section_kind": kind, "indexable": False,
                    "fts": _pfts(),
                })
                refs.append((start, ref))
            store[si] = {"parts": refs, "offs": offs}
        if text:
            path = _path_of(secs, si)
            ok = _indexable(kind, index_nonbody)
            for off, piece in _cut_offsets(text):
                children.append({
                    "level": 1, "depth": 0, "kind": "fulltext", "parent_ref": parent_ref_for(si, off),
                    "page": sec.get("page_from") or 0,
                    "page_from": sec.get("page_from") or 0,
                    "page_to": sec.get("page_to") or 0,
                    "text": (path + "\n" + piece) if path else piece,
                    "section_path": path, "section_kind": kind, "indexable": ok,
                })

    # 标注：一条一个块（通常很短，但信号强）；只有一层
    for ann in item.annotations:
        text = ann.text.strip()
        if len(text) < 8:
            continue
        comment = f"（批注：{ann.comment}）" if ann.comment else ""
        page = int(ann.page) if ann.page.isdigit() else 0
        children.append({
            "level": 1, "depth": 0, "kind": "annotation", "parent_ref": "",
            "page": page, "page_from": page, "page_to": page,
            "text": f"[{item.title} · 第 {ann.page} 页 高亮]{comment}\n{text}",
            "section_path": "", "section_kind": "", "indexable": True,
        })

    # 笔记：段落级切块，只有一层
    for note in item.notes:
        body = note.markdown.strip()
        if not body:
            continue
        for piece in chunk_text(body, target=700, overlap=100):
            children.append({
                "level": 1, "depth": 0, "kind": "note", "parent_ref": "",
                "page": 0, "page_from": 0, "page_to": 0,
                "text": f"[{item.title} · 笔记]\n{piece}",
                "section_path": "", "section_kind": "", "indexable": True,
            })

    # 全篇层（depth=2）：整篇的摘要级块（见 doc_chunk）。摘要文本由调用方给
    # （convert.py 读 meta 的 ai_summary），没给就用 item.abstract 兜底。
    _doc = doc_chunk(item, doc_summary)
    if _doc:
        children.append(_doc)

    out = parents + children
    for seq, chunk in enumerate(out):
        chunk["seq"] = seq
    return out
# ---------------------------------------------------------------- Markdown


def item_markdown(item: S.Item, pages: list[str], source: str, conn=None) -> str:
    """一篇文献的完整档案 Markdown。"""
    lines: list[str] = []
    lines.append(f"# {item.title or '(无标题)'}")
    lines.append("")
    lines.append(f"- Zotero key：`{item.key}`")
    lines.append(f"- 类型：{item.item_type}")
    if item.authors:
        lines.append(f"- 作者：{item.author_line}")
    if item.year:
        lines.append(f"- 年份：{item.year}")
    if item.venue:
        lines.append(f"- 出处：{item.venue}")
    if item.doi:
        lines.append(f"- DOI：`{item.doi}`")
    if item.fields.get("url"):
        lines.append(f"- 链接：{item.fields['url']}")
    if item.collections:
        lines.append(f"- 分类：{' / '.join(item.collections)}")
    if item.tags:
        lines.append(f"- 标签：{'、'.join(item.tags)}")
    lines.append(f"- 全文：{len(pages)} 页 / {sum(len(p) for p in pages):,} 字符"
                 + (f"（来源：{source}）" if source else "（无正文）"))
    if item.pdfs:
        lines.append(f"- PDF 附件：{len(item.pdfs)} 个")
        for pdf in item.pdfs:
            mark = "✓" if pdf.exists else "✗ 文件缺失"
            lines.append(f"  - {mark} `{os.path.basename(pdf.path)}`")
    lines.append("")

    # 经验层：有经验就展示，让读的人先看到"这篇被用过没有"
    if conn is not None and has_experience(conn, item.key):
        lines.append("## 使用经验")
        lines.append("")
        lines.append(render_experience(conn, item.key))
        lines.append("")

    if item.abstract:
        lines.append("## 摘要")
        lines.append("")
        lines.append(item.abstract.strip())
        lines.append("")

    if item.notes:
        lines.append(f"## 笔记（{len(item.notes)} 条）")
        lines.append("")
        for idx, note in enumerate(item.notes, start=1):
            lines.append(f"### 笔记 {idx}（`{note.key}`）")
            lines.append("")
            lines.append(note.markdown or "*(空)*")
            if note.image_refs:
                lines.append("")
                lines.append(f"> 该笔记含 {note.image_refs} 张内嵌截图，未搬运；在 Zotero 中查看。")
            lines.append("")

    if item.annotations:
        lines.append(f"## 高亮与批注（{len(item.annotations)} 条）")
        lines.append("")
        for ann in item.annotations:
            head = f"- **第 {ann.page} 页**" if ann.page else "- "
            lines.append(f"{head} `{ann.type}`")
            if ann.text:
                lines.append(f"  > {ann.text}")
            if ann.comment:
                lines.append(f"  > 批注：{ann.comment}")
        lines.append("")

    if pages:
        lines.append(f"## 正文（{len(pages)} 页）")
        lines.append("")
        lines.append(f"> 完整正文见 `fulltext/{item.key}.md`；此处只列每页首段，方便快速定位。")
        lines.append("")
        for page_no, page_text in enumerate(pages, start=1):
            head = clean_text(page_text)[:200].replace("\n", " ")
            lines.append(f"### p.{page_no}")
            lines.append("")
            lines.append(f"{head}…" if len(page_text) > 200 else head)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def figures_for_page(figs: dict | None, page_no: int) -> str:
    """把某一页的图注与表格渲染成 Markdown（没有就返回空串）。

    ⚠ 图注与表格是**按页**挂在正文里的（不是单独一份文件）：
      用户要的是"需要读图时让大模型自己去读" —— 图注就在那一页的正文
      末尾，AI 读到第 12 页就看见"这里有个图叫『神经网络模型』"，
      想深究就继续看上下文，不用额外查一次。
    """
    if not figs:
        return ""
    out: list[str] = []
    caps = [c for c in (figs.get("captions") or [])
            if c.get("page") == page_no]
    tabs = [t for t in (figs.get("tables") or [])
            if t.get("page") == page_no]
    if caps:
        out.append("**本页图注**")
        for c in caps:
            tag = "图" if c.get("kind") == "image" else "表"
            # 已经是"图1.1 xxx"的形式就直接用，避免重复编号
            txt = c.get("text") or ""
            out.append(f"- {tag}：{txt}")
        out.append("")
    for i, t in enumerate(tabs, start=1):
        out.append(f"**本页表格 {i}**（{t.get('rows')}×{t.get('cols')}）")
        out.append("")
        out.append((t.get("markdown") or "").rstrip())
        out.append("")
    return "\n".join(out)


def fulltext_markdown(item: S.Item, pages: list[str],
                      figs: dict | None = None) -> str:
    """正文 Markdown：每页一个 `## p.N` 锚点，在线侧按页切分返回。

    `figs` 形如 `{"captions": [...], "tables": [...]}`（见 offline/figures.py）。
    传了就按页把图注与表格插在正文后面。
    """
    lines = [f"# {item.title or item.key}", ""]
    lines.append(f"> Zotero key `{item.key}`｜{len(pages)} 页")
    lines.append("")
    for page_no, page_text in enumerate(pages, start=1):
        lines.append(f"## p.{page_no}")
        lines.append("")
        lines.append(page_text.strip() or "*(本页无可提取文字)*")
        lines.append("")
        extra = figures_for_page(figs, page_no)
        if extra:
            lines.append(extra)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 经验层读取


def has_experience(conn, item_key: str) -> bool:
    if conn is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM experience WHERE item_keys LIKE ? LIMIT 1", (f"%{item_key}%",)
    ).fetchone()
    return row is not None


def render_experience(conn, item_key: str) -> str:
    """把一篇文献的相关经验渲染成 Markdown。"""
    rows = conn.execute(
        """
        SELECT created_at, asked, method, outcome, reason, evidence, source
        FROM experience WHERE item_keys LIKE ?
        ORDER BY created_at DESC LIMIT 20
        """,
        (f"%{item_key}%",),
    ).fetchall()
    weight = conn.execute(
        "SELECT w.*, i.year FROM item_weight w "
        "LEFT JOIN items i ON i.key = w.item_key WHERE w.item_key = ?",
        (item_key,),
    ).fetchone()

    lines: list[str] = []
    if weight:
        lines.append(
            f"权重 {S.weight_multiplier(weight, S.recency_base(weight['year'])):.2f}"
            f"（尝试 {weight['attempts']} 次｜有效 {weight['effective']}"
            f"｜无效 {weight['ineffective']}｜部分 {weight['partial']}"
            f"{'｜★重点' if weight['pinned'] else ''}）"
        )
        lines.append("")
    label = {"effective": "✅ 有效", "ineffective": "❌ 无效",
             "partial": "◐ 部分有效", "unknown": "❓ 未验证"}
    for row in rows:
        lines.append(f"- {row['created_at'][:10]} {label.get(row['outcome'], row['outcome'])}"
                     f"｜{row['method'] or '(未记方法)'}")
        if row["asked"]:
            lines.append(f"  - 问题：{row['asked']}")
        if row["reason"]:
            lines.append(f"  - 原因：{row['reason']}")
        if row["evidence"]:
            lines.append(f"  - 依据：{row['evidence']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 向量


def embed_texts(texts: list[str], model_name: str, cache_dir: str, batch: int = 32):
    """算嵌入向量。

    故意做成"失败就返回 None"：向量只是增强，关键词检索不依赖它，
    模型下载不下来时整条管道仍然可用。
    """
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError as exc:
        print(f"  [向量] 跳过：缺少依赖（{exc}）")
        return None

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        vectors = list(model.embed(texts, batch_size=batch))
    except Exception as exc:  # noqa: BLE001
        print(f"  [向量] 失败：{type(exc).__name__}: {exc}")
        print("  [向量] 提示：需要联网下载模型；失败不影响关键词检索。")
        return None
    return np.asarray(vectors, dtype="float32")
