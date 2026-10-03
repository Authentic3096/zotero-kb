"""构建知识库：读 Zotero → 写 Markdown → 切片 → 建索引 → 算向量 → 写 MANIFEST。

被 convert.py（命令行入口）调用，也可以直接 import 用于测试。

对经验层（experience / item_weight）**只读不改**：重建知识库永远不会丢掉你的经验。
"""

from __future__ import annotations
# --- 让本模块可以独立运行（不依赖调用者先配好 sys.path）---
# 本项目模块平铺在 offline/ 与 online/ 下，用 `from schemas import ...` 这种
# 平铺方式互相导入，因此要求对应目录在 sys.path 里。调用者通常配好了，但
# **直接运行本文件**时没有"调用者"，就会 ModuleNotFoundError。
# 本机踩过同类问题：gui.py 只加了 offline/，面板点「列出全部经验」报
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
from datetime import datetime, timezone

import schemas as S
from zreader import ZoteroReader, clean_text, html_to_markdown, split_pages  # noqa: F401


# 切片参数：800-1200 字符、重叠 150。学术段落大多 300-900 字符，
# 这个粒度既能被向量模型吃下（e5 上限 512 token），又保留足够上下文。
CHUNK_TARGET = 1000
CHUNK_OVERLAP = 150
CHUNK_MIN = 120


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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


def collect_chunks(item: S.Item, pages: list[str]) -> list[dict]:
    """把一篇文献切成块，带页码溯源信息。

    来源优先级：正文 → 用户标注 → 用户笔记。
    标注和笔记是**人工高信号**，单独成块并标记来源，检索时更容易被看重。
    """
    out: list[dict] = []

    # 正文
    for page_no, page_text in enumerate(pages, start=1):
        for piece in chunk_text(page_text):
            out.append({"page": page_no, "kind": "fulltext", "text": piece})

    # 标注：一条一个块（通常很短，但信号强）
    for ann in item.annotations:
        text = ann.text.strip()
        if len(text) < 8:
            continue
        comment = f"（批注：{ann.comment}）" if ann.comment else ""
        page = int(ann.page) if ann.page.isdigit() else 0
        out.append({
            "page": page,
            "kind": "annotation",
            "text": f"[{item.title} · 第 {ann.page} 页 高亮]{comment}\n{text}",
        })

    # 笔记：段落级切块
    for note in item.notes:
        body = note.markdown.strip()
        if not body:
            continue
        for piece in chunk_text(body, target=700, overlap=100):
            out.append({"page": 0, "kind": "note", "text": f"[{item.title} · 笔记]\n{piece}"})

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
