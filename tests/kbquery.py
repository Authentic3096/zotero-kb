"""从知识库里**现取**检索词/标题片段，供测试使用。

## 为什么需要这个

测试必须真的搜到东西（断言依赖命中），所以最容易的写法是把几个"确实存在
于库里"的词硬编码进去。但那些词**就是用户的研究领域** —— 一旦随仓库公开，
等于把人家在做什么一并公布了。

所以改成：**运行时从库里挑**。取一条真实标题里的一段中文当查询词，
既保证能命中（标题自己当然含这段），又不在代码里留下任何领域词汇。

    from kbquery import sample_query, sample_title, chinese_runs

    q = sample_query()          # 一个能命中库的查询词
    t = sample_title()          # 一条真实标题（要造 fixture 时用）
    chinese_runs("气候变化模型")   # ['气候变化模型']

⚠ 库里没东西时返回空串，调用方要能容忍（测试里跳过或退化）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(ROOT, "offline"), os.path.join(ROOT, "online")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CJK = re.compile(r"[\u4e00-\u9fff]{2,}")


def chinese_runs(text: str) -> list[str]:
    """抽出文本里的连续中文片段（长度 ≥2）。"""
    return CJK.findall(text or "")


def _connect():
    try:
        import schemas as S  # noqa: PLC0415
        db = os.path.join(S.KB_DIR, "index.db")
    except Exception:                                        # noqa: BLE001
        return None
    if not os.path.exists(db):
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def sample_title(offset: int = 0):
    """取一条真实标题（按 key 排序，offset 用来取不同的几条）。没有就返回 ''。"""
    conn = _connect()
    if conn is None:
        return ""
    try:
        rows = conn.execute(
            "SELECT title FROM items WHERE length(title) > 8 ORDER BY key"
        ).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if not rows:
        return ""
    return rows[offset % len(rows)][0] or ""


def sample_item(offset: int = 0):
    """取一条真实文献的 `(key, title)`。没有就返回 `("", "")`。

    为什么要它：断言要**跟着库内容走** —— 比如"标重点后这篇更靠前"，
    被测的 key、查询词、期望在结果里出现的片段，必须取自**同一篇**，
    否则库里内容一变（本轮全库正文换成了 MinerU）测试就误报。
    """
    conn = _connect()
    if conn is None:
        return ("", "")
    try:
        row = conn.execute(
            "SELECT key, title FROM items WHERE length(title) > 8 ORDER BY key "
            "LIMIT 1 OFFSET ?", (offset,)).fetchone()
    except sqlite3.Error:
        return ("", "")
    finally:
        conn.close()
    if not row:
        return ("", "")
    return (row[0] or "", row[1] or "")


def sample_titles(n: int = 24) -> list[str]:
    """取 n 条真实标题（**一次连接**，别在循环里反复开库）。"""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT title FROM items WHERE length(title) > 8 ORDER BY key LIMIT ?",
            (n,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def sample_query(offset: int = 0, min_len: int = 4) -> str:
    """取一个**能命中库**的查询词：真实标题里的一段中文。

    从标题中段取，避开开头常见的"研究/基于"这类泛词。
    """
    title = sample_title(offset)
    runs = [r for r in chinese_runs(title) if len(r) >= min_len]
    if not runs:
        runs = chinese_runs(title)
    if not runs:
        return ""
    longest = max(runs, key=len)
    # 太长就截中间一段（查询词短一点更像真实使用）
    if len(longest) > 8:
        start = max(0, (len(longest) - 6) // 2)
        return longest[start:start + 6]
    return longest


def sample_queries(n: int = 6) -> list[str]:
    """取 n 个互不相同的查询词（不够就有几个算几个）。"""
    out: list[str] = []
    for i in range(n * 4):
        q = sample_query(offset=i)
        if q and q not in out:
            out.append(q)
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    print("示例查询词：", sample_queries(6))
    print("示例标题  ：", sample_title()[:60])
