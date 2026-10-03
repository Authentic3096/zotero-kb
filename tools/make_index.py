"""生成知识库的「文献清单」INDEX.md —— 给人看的目录。

    python tools/make_index.py

为什么要有它：知识库里的文件是按 Zotero key 命名的（22X9PMR6.md），
唯一但完全认不出是哪篇。检索时靠 kb_search 就够了，但用户想
"看看库里都有什么"时没有入口 —— papers/ 目录点开是一堆 8 位代号。

设计取舍
    · 用 **Markdown 表格**（用户明确要求）：表格能对齐，扫一眼就看完，
      比列表省一半行数。
    · 表格里放 key，但**不把它当主角** —— 人看的是作者年份标题，
      key 放最后一列，需要精确指定时再复制。
    · 按年份倒序分组：新文献在最上面，符合"最近在看什么"的直觉。
    · ⚠ 不放进 fulltext：这个文件是给人看的，AI 不需要读它
      （AI 走 kb_search，更快也更省上下文）。
"""

from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

TYPE_CN = {
    "journalArticle": "期刊",
    "conferencePaper": "会议",
    "thesis": "学位论文",
    "book": "书",
    "bookSection": "书章",
    "report": "报告",
    "patent": "专利",
    "preprint": "预印本",
    "webpage": "网页",
    "newspaperArticle": "报纸",
    "magazineArticle": "杂志",
    "manuscript": "手稿",
    "document": "文档",
    "standard": "标准",
    "dataset": "数据集",
}


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def build(db: str | None = None) -> str:
    """返回 INDEX.md 的内容（不写盘，方便测试）。"""
    db = db or S.INDEX_DB
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT key, title, year, first_author, item_type, fulltext_chars, "
        "n_annotations, n_notes, collections FROM items"))
    try:
        built = S.get_meta(conn, "built_at", "")
        model = S.get_meta(conn, "embed_model", "")
        n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        n_vec = conn.execute(
            "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
        n_exp = conn.execute(
            "SELECT COUNT(*) AS n FROM experience").fetchone()["n"]
    except sqlite3.Error:
        built = model = ""
        n_chunks = n_vec = n_exp = 0
    conn.close()

    L: list[str] = []
    L.append("# 文献清单")
    L.append("")
    L.append(f"共 **{len(rows)}** 篇 ｜ 切片 {n_chunks} ｜ 向量 {n_vec} ｜ "
             f"经验 {n_exp} 条"
             + (f" ｜ 嵌入模型 {model}" if model else "")
             + (f" ｜ 建于 {built[:16].replace('T', ' ')}" if built else ""))
    L.append("")
    L.append("> 这份清单是给人看的目录，方便一眼认出哪篇是哪篇。")
    L.append("> AI 检索走 `kb_search`，不需要读这个文件。")
    L.append(">")
    L.append("> **KEY** 列是 Zotero 的条目编号 —— 想精确指定某一篇")
    L.append("> （比如在 Zotero 里右键「发送到 DSH」、或调权重）时用它。")
    L.append("")

    # 按年分组，年新的在前；无年份的归到最后
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        y = str(r["year"] or "").strip() or "（无年份）"
        buckets.setdefault(y, []).append(r)

    def year_key(y: str):
        digits = "".join(c for c in y if c.isdigit())
        return (y == "（无年份）", -int(digits) if digits else 0, y)

    total = len(rows)
    for y in sorted(buckets, key=year_key):
        group = sorted(buckets[y], key=lambda r: (r["first_author"] or "zzzz"))
        L.append(f"## {y}　（{len(group)} 篇）")
        L.append("")
        L.append("| 文献 | 类型 | 全文 | KEY |")
        L.append("|:---|:---:|:---:|:---|")
        for r in group:
            ref = S.human_ref(r["title"], r["first_author"], r["year"],
                              limit=46)
            # 表格里的竖线会破坏列，必须转义
            ref = ref.replace("|", "\\|")
            typ = TYPE_CN.get(r["item_type"] or "", r["item_type"] or "—")
            ft = r["fulltext_chars"] or 0
            if ft > 200:
                mark = f"✓ {ft // 1000}k" if ft >= 1000 else f"✓ {ft}"
            else:
                mark = "—"
            bits = []
            if r["n_annotations"]:
                bits.append(f"{r['n_annotations']} 标注")
            if r["n_notes"]:
                bits.append(f"{r['n_notes']} 笔记")
            if bits:
                typ += f"（{'、'.join(bits)}）"
            L.append(f"| {ref} | {typ} | {mark} | `{r['key']}` |")
        L.append("")

    L.append("---")
    L.append("")
    L.append("## 这份清单怎么来的")
    L.append("")
    L.append("由 `tools/make_index.py` 从索引库生成，"
             "「更新索引」之后会自动重建。")
    L.append("想手动重建：`python tools/make_index.py`")
    L.append("")
    return "\n".join(L) + "\n"


def write_index(db: str | None = None, out: str | None = None) -> str:
    """生成并写盘，返回写入路径。"""
    text = build(db)
    out = out or os.path.join(S.KB_DIR, "INDEX.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out


def main() -> int:
    try:
        out = write_index()
    except FileNotFoundError:
        print(f"[XX] 找不到索引库：{S.INDEX_DB}")
        print("     先跑一次「更新索引」（scripts\\1-convert.cmd）")
        return 1
    size = os.path.getsize(out)
    print(f"[OK] 已生成 {out}（{size} 字节）")
    # 顺便报几个数，方便核对
    import re
    text = open(out, encoding="utf-8").read()
    n = len(re.findall(r"^\| .*\| `[A-Z0-9]{8}` \|$", text, re.M))
    print(f"     清单里 {n} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
