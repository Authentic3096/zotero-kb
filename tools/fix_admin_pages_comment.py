"""修掉 9 个页签占位脚本里那处"块注释提前闭合"的语法错误，并给语法检查器补上
`content/admin-pages/` 目录。

    & <venv>\\Scripts\\python.exe tools\\fix_admin_pages_comment.py          # 只打印
    ...  --write                                                             # 真改

背景（2026-10-13）：占位模板的块注释里写了 `.btn*/.note-*` —— 里面的 `*/`
**提前闭合了块注释**，后面那些中文说明变成了裸代码 → 整份脚本语法错误、
静默不注册（页面导航里只剩「知识库结构」一页）。
`check_js_syntax.py` 只扫 `content/*.js`，扫不到 `content/admin-pages/*.js`，
所以这个错误一路打进了 xpi。这里两件一起修。
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # ⚠ tools/ → plugins/zotero-kb/（本会话第三次
#                                       漏这一层了：HERE 是 tools/，不是插件根）
PAGES = os.path.join(ROOT, "zotero-plugin", "content", "admin-pages")
CHECKER = os.path.join(HERE, "check_js_syntax.py")

# 坏的写法 → 好的写法（`*/` 连写必须消失）
BAD_LINES = [
    " *   · 样式只用 admin.css 里的类（.card/.row/.btn*/.grid/.note-*/.num/.mono…），",
    " *     不要再写颜色字面量 —— 那套令牌与 Tk 侧 theme.py 是同名的；",
]
GOOD_LINES = [
    " *   · 样式只用 admin.css 里的类（.card / .row / .btn 系列 / .grid /",
    " *     .note 系列 / .num / .mono …），不要再写颜色字面量 ——",
    " *     那套令牌与 Tk 侧 theme.py 是同名的；",
    " *     ⚠ 注释里不要出现 `*/` 连写（例如 `.btn*/.note-*`）—— 它会把块注释",
    " *       提前闭合，整份脚本语法错误、静默不注册（本模板第一版就踩了）。",
]

SYNTAX_ADD = """        files += sorted(glob.glob(os.path.join(PLUGIN, "content", "*.js")))"""


def main() -> int:
    write = "--write" in sys.argv
    changed = []
    for fn in sorted(os.listdir(PAGES)):
        if not fn.endswith(".js"):
            continue
        path = os.path.join(PAGES, fn)
        s = io.open(path, encoding="utf-8", newline="").read()
        if BAD_LINES[0] not in s:
            continue
        s2 = s.replace(BAD_LINES[0] + "\n" + BAD_LINES[1], "\n".join(GOOD_LINES))
        if s2 == s:
            continue
        changed.append(fn)
        if write:
            io.open(path, "w", encoding="utf-8", newline="").write(s2)
    print(("已修" if write else "将修") + f" {len(changed)} 个页签占位：{changed}")

    # 语法检查器补扫 admin-pages/
    s = io.open(CHECKER, encoding="utf-8", newline="").read()
    if "admin-pages" in s:
        print("语法检查器：已经扫 admin-pages 了，跳过")
    else:
        assert s.count(SYNTAX_ADD) >= 1, "找不到插入锚点，请手工确认"
        add = (SYNTAX_ADD +
               '\n        # ⚠ admin-pages/ 也要扫：2026-10-13 那里 10 个脚本因为块注释里'
               '\n        #   写了 `*/` 连写而全部语法错误，却一路打进了 xpi（本检查器当时'
               '\n        #   只扫 content/*.js）。页面脚本不进 bootstrap，但进包。'
               '\n        files += sorted(glob.glob(os.path.join('
               'PLUGIN, "content", "admin-pages", "*.js")))')
        s2 = s.replace(SYNTAX_ADD, add, 1)
        print("语法检查器：将补上 admin-pages/*.js")
        if write:
            io.open(CHECKER, "w", encoding="utf-8", newline="").write(s2)
    if not write:
        print("\n（只打印；加 --write 真改）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
