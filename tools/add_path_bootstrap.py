"""给叶子模块插入"自包含的路径设置"，让它们能独立运行。

    python tools/add_path_bootstrap.py [--dry]

背景（本机踩的坑）：
    项目模块平铺在 `offline\\` 与 `online\\` 下，用 `from schemas import ...`
    这种平铺方式互相导入，所以要求对应目录在 sys.path 里。调用者通常配好了，
    但**直接运行某个文件**时（`python offline\\converter.py`）没有"调用者"，
    就会 ModuleNotFoundError。同类问题已经造成过一次真实故障：
    `tools\\gui.py` 只加了 offline\\，面板点「列出全部经验」报
    `No module named 'searcher'`（searcher 在 online\\ 下）。

⚠ 两个必须避开的坑（第一次做的时候全踩了）：
    1. **别用 shell 内联 Python 传含反斜杠的代码** —— heredoc 会把 `\\` 吃掉，
       注释里的 `online\\` 变成 `online`，生成的代码直接语法错误。
       → 所以这件事写成脚本文件（就是本文件）。
    2. **插入位置不能用"最后一个 import 语句之后"** —— 多行 import
       （`from query import (a,\\n b)`）会被从中间劈开。
       → 用 AST 找"第一个真实语句"的行号，插在它前面。

用法：默认处理 TARGETS 里的文件；加 --dry 只看会改成什么样。
"""

from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TARGETS = [
    "offline/converter.py",
    "offline/zreader.py",
    "online/searcher.py",
]

MARK_START = "# --- 让本模块可以独立运行（不依赖调用者先配好 sys.path）---"
MARK_END = "# --- 路径设置结束 ---"

# 用普通字符串拼接生成，避免源码里出现转义混乱
SNIPPET = "\n".join([
    MARK_START,
    "# 本项目模块平铺在 offline/ 与 online/ 下，用 `from schemas import ...` 这种",
    "# 平铺方式互相导入，因此要求对应目录在 sys.path 里。调用者通常配好了，但",
    "# **直接运行本文件**时没有\"调用者\"，就会 ModuleNotFoundError。",
    "# 本机踩过同类问题：gui.py 只加了 offline/，面板点「列出全部经验」报",
    "#   ModuleNotFoundError: No module named 'searcher'（它在 online/ 下）。",
    "import os as _os",
    "import sys as _sys",
    "",
    "_HERE = _os.path.dirname(_os.path.abspath(__file__))",
    "_ROOT = _os.path.dirname(_HERE)",
    "for _sub in (\"offline\", \"online\"):",
    "    _p = _os.path.join(_ROOT, _sub)",
    "    if _os.path.isdir(_p) and _p not in _sys.path:",
    "        _sys.path.append(_p)",
    "# 清掉临时名，别污染本模块的命名空间",
    "del _os, _sys, _HERE, _ROOT, _sub, _p",
    MARK_END,
    "",
])


def first_stmt_line(src: str) -> int:
    """返回可以安全插入的行号（1-based）；注释/文档字符串/`__future__` 都跳过。

    用 AST 而不是正则，是因为多行 import / 多行 docstring 用正则很容易切错。

    ⚠ 两个必须让开的开头元素（本机第一个版本就栽在第二个上）：
      · 模块 docstring —— 插到它前面会把文档挤到中间
      · `from __future__ import ...` —— **必须在文件最开头**，
        插到它前面会报 `SyntaxError: from __future__ imports must occur at
        the beginning of the file`。注意：`ast.parse()` **不会**报这个错
        （它只在编译阶段检查），所以光靠"改完能 parse"是拦不住的。
    """
    tree = ast.parse(src)
    if not tree.body:
        return len(src.splitlines()) + 1
    at = tree.body[0].lineno
    for node in tree.body:
        is_doc = (isinstance(node, ast.Expr)
                  and isinstance(node.value, ast.Constant)
                  and isinstance(node.value.value, str))
        is_future = (isinstance(node, ast.ImportFrom)
                     and node.module == "__future__")
        if is_doc or is_future:
            at = getattr(node, "end_lineno", node.lineno) + 1
            continue
        break
    return at


def main() -> int:
    dry = "--dry" in sys.argv
    print("=" * 66)
    print("给叶子模块加自包含路径设置")
    print("=" * 66)
    changed = 0
    for rel in TARGETS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"  [--] {rel} 不存在")
            continue
        src = open(path, encoding="utf-8").read()
        if MARK_START in src:
            print(f"  [--] {rel} 已有标记，跳过")
            continue
        try:
            at = first_stmt_line(src)
        except SyntaxError as exc:
            print(f"  [XX] {rel} 解析失败：{exc}")
            continue
        lines = src.splitlines(keepends=True)
        idx = at - 1                      # 插在这一行之前
        new = "".join(lines[:idx]) + SNIPPET + "".join(lines[idx:])
        # 语法自检：改完必须还能解析
        try:
            ast.parse(new)
        except SyntaxError as exc:
            print(f"  [XX] {rel} 改完语法错误，跳过：{exc}")
            continue
        if dry:
            print(f"  [dry] {rel}：会插在第 {at} 行前")
        else:
            open(path, "w", encoding="utf-8").write(new)
            print(f"  [OK] {rel}：已插在第 {at} 行前（{len(src)} → {len(new)} 字符）")
        changed += 1
    print(f"\n共处理 {changed} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
