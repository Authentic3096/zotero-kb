"""把插件 JS 脚本从 IIFE 包装改成裸函数体，让返回值能真正传出来。

    python tools/fix_js_wrapper.py [--dry]

背景（踩过的坑）：
    `tools/zotero_js.py` 执行脚本的方式是
        new AsyncFunction("Zotero", "Services", "ChromeUtils", code)
    也就是把**整个文件**当函数体。但如果文件是这种结构：

        (async () => {
          ...
          return text;      // ← 这个 return 属于 IIFE
        })();               // ← 最后一句是表达式语句，值为 undefined

    那么 AsyncFunction 的返回值永远是 undefined —— 任务报告"成功"、
    但 result 是空的（实测 ms=2，说明根本没跑多少代码）。
    手动粘贴到 Zotero 的"运行 JavaScript"窗口时看不到这个问题，
    因为那边靠 ProgressWindow 显示结果。

    改法：去掉头尾的 IIFE 包装，让代码直接成为 AsyncFunction 的函数体，
    顶层的 `return text` 就能正常返回。
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")

TARGETS = ["verify-plugin.js", "diag-load.js", "find-working-manifest.js",
           "install-in-zotero.js", "probe-variants.js", "probe-install-api.js"]


def unwrap(src: str) -> tuple[str, bool]:
    """去掉 `(async () => {` … `})();` 包装。返回 (新内容, 是否改了)。"""
    lines = src.splitlines()
    # 找开头：允许前面有注释
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        if re.match(r"^\(async\s*\(\s*\)\s*=>\s*\{\s*$", s):
            start = i
        break
    if start is None:
        return src, False
    # 找结尾：最后一个 `})();` 或 `})()`
    end = None
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if re.match(r"^\}\)\(\)\s*;?\s*$", s):
            end = i
            break
    if end is None or end <= start:
        return src, False
    body = lines[start + 1:end]
    # 去掉公共缩进（IIFE 内通常缩进 2 格）
    out_lines = []
    for ln in body:
        out_lines.append(ln[2:] if ln.startswith("  ") else ln)
    new = "\n".join(lines[:start] + out_lines + lines[end + 1:])
    if not new.endswith("\n"):
        new += "\n"
    return new, True


def main() -> int:
    dry = "--dry" in sys.argv
    print("=" * 64)
    print("去掉 JS 脚本的 IIFE 包装（让返回值能传出来）")
    print("=" * 64)
    changed = 0
    for name in TARGETS:
        p = os.path.join(PLUGIN, name)
        if not os.path.exists(p):
            continue
        src = open(p, encoding="utf-8").read()
        new, ok = unwrap(src)
        if not ok:
            print(f"  [--] {name}：没有 IIFE 包装（无需改）")
            continue
        # 语法自检交给 node（调用方之后会跑），这里只做括号配平粗检
        if new.count("{") != new.count("}"):
            print(f"  [XX] {name}：改完括号不平（{{={new.count('{')} }}={new.count('}')}），跳过")
            continue
        if dry:
            print(f"  [dry] {name}：会去掉包装（{len(src)} → {len(new)} 字符）")
        else:
            bak = p + ".bak-iife"
            open(bak, "w", encoding="utf-8").write(src)
            open(p, "w", encoding="utf-8").write(new)
            print(f"  [OK] {name}：已去包装（{len(src)} → {len(new)} 字符），备份 {os.path.basename(bak)}")
        changed += 1
    print(f"\n共处理 {changed} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
