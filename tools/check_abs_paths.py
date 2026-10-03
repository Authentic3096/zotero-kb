"""检查知识库索引里有没有存**绝对路径**（迁移目录前必须确认）。

    python tools/check_abs_paths.py

为什么要这个：知识库目录要跟着 Zotero 数据目录走（可迁移）。
如果索引里存了写死的绝对路径，迁移后那些字段就全指错了 ——
而且往往不会立刻报错，而是"读不到正文""附件找不到"这类模糊现象。
本机在 WAL 快照那次就吃过"看起来对、其实读的是旧数据"的亏，
所以迁移前先把这个查清楚。
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

ABS_RE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\|^/")


def looks_like_file_path(v: str) -> bool:
    """判断一个字符串是不是**文件路径**，而不是碰巧以 / 开头的内容。

    ⚠ 为什么要这层过滤：第一版只用了 `^/`，结果把正文里的
      `/j. issn. 1672-9730. 2023. 06. 040`（期刊编号）当成了绝对路径 ——
      纯粹误报。文件路径有些别的特征：盘符、反斜杠、常见扩展名、目录分隔。
    """
    v = v.strip()
    if not v:
        return False
    # Windows 盘符路径 / UNC
    if re.match(r"^[A-Za-z]:[\\/]", v) or v.startswith("\\\\"):
        return True
    # Unix 风格：必须像"多段目录"或带已知扩展名，才算路径
    if v.startswith("/"):
        if re.search(r"\.(md|json|db|txt|sqlite|pdf|py|js|log|csv|yml|yaml|xml)$",
                     v, re.I):
            return True
        # /a/b/c 这种多段（且不含空格与中文标点，正文里的引用通常带空格）
        if v.count("/") >= 2 and " " not in v and len(v) < 200:
            return True
    return False


def main() -> int:
    import schemas as S

    db = S.INDEX_DB
    print("=" * 68)
    print("检查索引里的绝对路径（迁移目录前必查）")
    print("=" * 68)
    print(f"  索引：{db}")
    print(f"  知识库目录：{os.path.dirname(db)}")
    print()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tabs = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"  表（{len(tabs)}）：{', '.join(tabs)}")
    print()

    findings = []
    for t in tabs:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for col in cols:
            # 只查文本列
            try:
                rows = conn.execute(
                    f"SELECT rowid, {col} FROM {t} "
                    f"WHERE {col} IS NOT NULL AND typeof({col})='text' LIMIT 400"
                ).fetchall()
            except sqlite3.Error:
                continue
            hits = []
            for r in rows:
                v = str(r[1])
                if looks_like_file_path(v):
                    hits.append((r[0], v))
            if hits:
                findings.append((t, col, len(hits), hits[0][1]))

    if not findings:
        print("  [OK] 没有发现绝对路径 —— 迁移目录是安全的。")
        print("       （正文/清单都是按 key 组织，路径由目录推算）")
    else:
        print(f"  [!!] 发现 {len(findings)} 处存了绝对路径，迁移前要处理：")
        for t, col, n, sample in findings:
            print(f"    · {t}.{col}  ({n}+ 行)")
            print(f"        样例：{sample[:110]}")
        print()
        print("  处理建议：")
        print("    · 如果是'知识库根目录'这类单一值 → 迁移时一并更新即可")
        print("    · 如果是逐行的文件路径 → 迁移脚本里要做前缀替换")
    conn.close()

    # 顺带报一下各子目录规模，便于决定哪些需要一起搬
    kb = os.path.dirname(db)
    print()
    print("  子目录规模：")
    for name in sorted(os.listdir(kb)):
        p = os.path.join(kb, name)
        if not os.path.isdir(p):
            continue
        total = n = 0
        for dp, _dn, fn in os.walk(p):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                    n += 1
                except OSError:
                    pass
        print(f"    {name:20} {total / 1048576:8.1f} MB  {n:5} 文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
