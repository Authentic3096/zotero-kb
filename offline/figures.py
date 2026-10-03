"""从 PDF 里提取图注与表格 —— 让图表进入知识库。

## 为什么不需要视觉模型

用户要的是两件事，**都不用"看懂"图**：

  1. **列出图注** —— 图注本来就是 PDF 里的**文字**（`图1.1 欧洲空间局
     （ESA）模拟的地磁场示意图[4]`），PyMuPDF 直接读得到。
     有了图注，检索"那张画了补偿系统结构的图"就能命中，
     而且 `kb_fulltext` 里能看到"这里有个图，标题是……"。
  2. **表格存成 Markdown** —— `page.find_tables()` + `Table.to_markdown()`
     就够了，纯结构解析。

所以本模块**全程不调模型**（快、零额外依赖）。
真要看图内容时，让大模型自己按图注所在的页去调 `kb_fulltext(page_from=…)`
—— 用户的原话："需要读图就让大模型在读知识库时去自己读（必要的话）"。

## 为什么这个方案更稳

本机实测过视觉模型（`moondream`、`qwen2.5vl:3b`）：
· `moondream` 输出垃圾（`1. 三个图表 2. 三个图表 3. 三个图表`）
· `qwen2.5vl:3b` 对本机这些页一律回答"无图表"

而**图注提取一次就对了**。能用确定性方法拿到的信息，
不该交给概率模型去猜。

## 图注怎么认

中文论文：`图1.1` `图 3-2` `圖2`；英文：`Fig. 3` `Figure 4a` `Table 2`
表格：`表1` `表 2-3` `Table 1`

⚠ 关键点：**图注编号必须按出现顺序**，而且要把跨行折断的图注接起来
（PDF 里图注经常折成 2~3 行）。
"""
from __future__ import annotations

import re

# 图注：图/圖/Fig./Figure  + 编号 + 后跟说明文字
CAP_RE = re.compile(
    r"^\s*((?:图|圖|Fig\.?|Figure|FIGURE)\s*[\d]+(?:[-–—.．]\d+)*[a-zA-Z]?"
    r"[\s:：.、,，-]*[^\n]{0,200})",
    re.I)
# 表注同理
TAB_CAP_RE = re.compile(
    r"^\s*((?:表|Table|TABLE)\s*[\d]+(?:[-–—.．]\d+)*[a-zA-Z]?"
    r"[\s:：.、,，-]*[^\n]{0,200})",
    re.I)

# 明显的非图注（页眉页脚、目录行、只有编号没说明）
NOT_CAPTION = re.compile(
    r"(目录|contents|^\s*$|^\s*[\d\-–—.．]+\s*$|"
    r"图\s*[\d.]+\s*$|表\s*[\d.]+\s*$)", re.I)

# 表格提取质量的粗判：太碎/太小的多半是误检（版面像表格但不是）
MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2
MIN_CELL_CHARS = 1


def _clean(s: str) -> str:
    """清理 PDF 文本里的排版噪声。

    ⚠ 两个**相反**方向的坑，都踩过：

    1. 中文之间被插了空格（`中 国 电 机 工 程 学 报`）→ 要去掉。
    2. 英文的空格被**换行吃掉**（`get_text("text")` 把一行的词拼起来，
       行内空格在有些 PDF 里丢失）→ 变成
       `Fluxgateerrormagneticfielddatacompensationresults`，得补回来。

    第 2 条的判据（保守，宁可少补不可错补）：
    只在**连续 3 个以上小写字母后面直接跟大写字母**时插空格
    （`gateerror` → `gate error` 这种 camel 断层），
    以及**小写紧跟大写**（`resultsFluxgate`）。
    `pdf`、`T-L`、`3pT`、`Fig.1` 这类不受影响。
    """
    s = (s or "").strip()
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)   # 控制字符
    s = re.sub(r"[\uf000-\uf8ff\ue000-\uf8ff]", "", s)    # 私有区（字体残缺）
    s = re.sub(r"\s+", " ", s)
    # 中文字之间的空格
    s = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", s)
    # 英文空格被吃掉：小写+大写边界（连着 3 个以上小写才动手，避免
    # 把 `T-L`、`pH`、`mRNA` 这类本来就该连着的拆开）
    s = re.sub(r"(?<=[a-z]{3})(?=[A-Z][a-z])", " ", s)
    # 数字与字母之间被吃掉（`Fig.1Fluxgate`、`1000HzBandwidth`）
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z]{2})", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# 章节号开头（`1.2 国内外研究现状`）—— 图注续行不会长这样
SECTION_HEAD = re.compile(r"^\s*\d+(\.\d+)+\s*[\u4e00-\u9fff]")


def _looks_complete(s: str) -> bool:
    """第一行是不是已经是一条完整图注？

    ⚠ 这个判断是**必须**的。第一版只判"下一行像不像续行"，结果把
      正文吞了进来：

          图1.1 某某机构模拟的某物理场示意图[4] 某类探测是一种
          重要的探测技术手段，是利用某平台搭载某传感器……

      （示例里的词是**占位词**，不是本机库里的真实内容 —— 本文件要公开。）

      图注几乎总是**单独一行**（含编号、标题、可能带 [4] 这样的引用），
      而正文是成句的。所以先看第一行是否自足，自足就别往后接。
    """
    t = s.strip()
    if len(t) >= 22:                       # 够长了，多半完整
        return True
    if re.search(r"[\]\)）】]$", t):        # 以引用/括号收尾
        return True
    return False


def _merge_wrapped(lines: list[str], idx: int) -> tuple[str, int]:
    """把折行的图注接起来（**保守**：拿不准就不接）。

    PDF 里确有折行的图注，例如
        `图3.12 不同补偿算法在实测数据上的\n误差对比曲线`
    但更多时候下一行就是正文。所以只在**明确像续行**时才接：

      · 下一行不以句号结尾，且长度较短（< 40 字）→ 像标题碎片
      · 且不以下列词开头（那些是正文的连接词/指代）
      · 且不是章节号开头（`1.2 国内外研究现状`）
      · 且下一行本身不是新的图注/表注
    """
    first = lines[idx].strip()
    if _looks_complete(first):
        return _clean(first), idx + 1

    out = first
    j = idx + 1
    while j < len(lines) and j - idx <= 2:
        nxt = lines[j].strip()
        if not nxt:
            break
        if CAP_RE.match(nxt) or TAB_CAP_RE.match(nxt):
            break
        # 章节号开头 → 正文
        if SECTION_HEAD.match(nxt):
            break
        # 正文连接词/指代
        if re.match(r"^(如|从图|由图|见图|参见|可见|因此|所以|然而|但是|而|其|该|这|"
                    r"是|为|在|对|与|和|由于|本文|本研究|综上|由此|故|即|则)", nxt):
            break
        # ⚠ 以句号/分号收尾 → 一定是完整的句子，不可能是图注续行
        #   （图注可以折行，但折行处不会正好是个句号）
        if re.search(r"[。；;！？!?]$", nxt):
            break
        # 够长（≥ 20 字）→ 多半是正文句子；图注续行一般是短碎片
        if len(nxt) >= 20:
            break
        out += nxt
        j += 1
    return _clean(out), j


def extract_page_figures(page) -> list[dict]:
    """抽一页里的图注与表注。返回 [{kind, label, text, y}]。"""
    out: list[dict] = []
    try:
        txt = page.get_text("text") or ""
    except Exception:  # noqa: BLE001
        return out
    lines = txt.split("\n")
    for i, ln in enumerate(lines):
        raw = ln.strip()
        if not raw or len(raw) < 4:
            continue
        for kind, rx in (("image", CAP_RE), ("table", TAB_CAP_RE)):
            m = rx.match(raw)
            if not m:
                continue
            if NOT_CAPTION.search(raw):
                continue
            merged, _ = _merge_wrapped(lines, i)
            if len(merged) < 6:          # 只有编号、没说明 → 不算
                continue
            label = re.match(
                r"((?:图|圖|Fig\.?|Figure|表|Table)\s*[\d]+(?:[-–—.．]\d+)*[a-zA-Z]?)",
                merged, re.I)
            out.append({
                "kind": kind,
                "label": (label.group(1) if label else "").strip(),
                "text": merged[:400],
            })
            break
    # 同一页可能重复命中（同一行被两个正则匹配），按文本去重
    seen, uniq = set(), []
    for x in out:
        k = x["text"][:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


def extract_page_tables(page, max_tables: int = 3) -> list[dict]:
    """抽一页里的表格并转 Markdown。

    ⚠ `find_tables()` 会误检（把版面块当成表）。所以过滤：
      行列数太小、或几乎所有单元格都是空的，都丢掉。
    """
    out: list[dict] = []
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001
        return out
    for t in (getattr(found, "tables", None) or [])[:max_tables]:
        try:
            data = t.extract()
        except Exception:  # noqa: BLE001
            continue
        if not data or len(data) < MIN_TABLE_ROWS:
            continue
        rows = [[_clean(str(c)) if c is not None else "" for c in row]
                for row in data]
        cols = max((len(r) for r in rows), default=0)
        if cols < MIN_TABLE_COLS:
            continue
        filled = sum(1 for r in rows for c in r if len(c) >= MIN_CELL_CHARS)
        total = max(1, sum(len(r) for r in rows))
        if filled / total < 0.3:          # 大部分是空的 → 误检
            continue
        # 补平每行列数（Markdown 要求）
        rows = [r + [""] * (cols - len(r)) for r in rows]
        head, body = rows[0], rows[1:]
        md = ("| " + " | ".join(head) + " |\n"
              + "|" + "|".join([" --- "] * cols) + "|\n")
        for r in body:
            md += "| " + " | ".join(r) + " |\n"
        out.append({"markdown": md, "rows": len(rows), "cols": cols,
                    "bbox": list(getattr(t, "bbox", ())) or None})
    return out


def extract_document(pdf_path: str, max_pages: int = 0,
                     progress=None) -> dict:
    """抽整份 PDF：图注 + 表注 + 表格 Markdown。

    返回 {"captions": [...], "tables": [...], "pages": n}
      captions: [{"page", "kind", "label", "text"}]
      tables:   [{"page", "markdown", "rows", "cols"}]
    """
    import pymupdf

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:  # noqa: BLE001
        return {"captions": [], "tables": [], "pages": 0,
                "error": f"打不开：{exc}"}

    captions: list[dict] = []
    tables: list[dict] = []
    n = len(doc)
    if max_pages > 0:
        n = min(n, max_pages)
    for pno in range(n):
        page = doc[pno]
        for c in extract_page_figures(page):
            c["page"] = pno + 1
            captions.append(c)
        for t in extract_page_tables(page):
            t["page"] = pno + 1
            tables.append(t)
        if progress and (pno % 10 == 0):
            progress(pno + 1, n)
    doc.close()
    return {"captions": captions, "tables": tables, "pages": n}


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    """命令行试试效果（给定 PDF 路径）。

        python offline\\figures.py "D:\\path\\to.pdf" [--tables-only]
    """
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description="抽 PDF 的图注与表格")
    ap.add_argument("pdf", help="PDF 路径")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--tables-only", action="store_true")
    ap.add_argument("--captions-only", action="store_true")
    ap.add_argument("--dump-tables", action="store_true",
                    help="把表格 Markdown 打印出来")
    args = ap.parse_args(argv)

    if not os.path.exists(args.pdf):
        print(f"  找不到 {args.pdf}")
        return 1
    print("=" * 74)
    print(f"抽图注与表格：{os.path.basename(args.pdf)}")
    print("=" * 74)

    def prog(done, total):
        print(f"\r  扫到第 {done}/{total} 页…", end="")

    r = extract_document(args.pdf, args.max_pages, progress=prog)
    print("\r" + " " * 40 + "\r", end="")
    if r.get("error"):
        print(f"  ✗ {r['error']}")
        return 1
    caps, tabs = r["captions"], r["tables"]
    print(f"  {r['pages']} 页 → 图注 {len(caps)} 条，表格 {len(tabs)} 个\n")

    if not args.tables_only:
        print("  【图注与表注】")
        for c in caps[:30]:
            tag = "图" if c["kind"] == "image" else "表"
            print(f"    p{c['page']:<4}{tag}  {c['text'][:88]}")
        if len(caps) > 30:
            print(f"    … 还有 {len(caps) - 30} 条")
    if not args.captions_only:
        print(f"\n  【表格】{'（用 --dump-tables 看内容）' if not args.dump_tables else ''}")
        for t in tabs[:20]:
            print(f"    p{t['page']:<4}{t['rows']}×{t['cols']}")
            if args.dump_tables:
                for ln in t["markdown"].split("\n")[:8]:
                    print(f"        {ln[:100]}")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main(_sys.argv[1:]))
