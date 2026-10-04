"""检查打包出来的 xpi 里有没有**写死的本机路径**。

    python tools/check_xpi_paths.py

为什么需要：这个项目要公开发布，xpi 里如果残留开发机的路径
（D:\\DSHplugins\\...），别人装上去就会指向不存在的目录，而且很难查。
本机已经因为"从知识库位置反推项目根"出过一次这种事故（面板报
"找不到 py 环境"），所以这件事值得自动化盯着。

判定规则（区分"写死"和"合理"）：

  算写死 ✗：
    · 出现开发机特有的目录名：DSHplugins / ZoteroData / 具体用户名
    · 形如 X:\\具体路径\\具体路径 的完整绝对路径

  不算写死 ✓（这些是正常做法）：
    · Services.dirsvc.get("LocalAppData")  —— 向系统问路径
    · %LOCALAPPDATA% / %APPDATA%           —— 环境变量
    · C:\\Users\\Public                     —— Windows 系统常量，
                                              不是"某个用户的目录"
    · 注释与文档里的举例

用法：在打包之后跑一遍（check_plugin.py 的最后一步也可以调它）。
"""

from __future__ import annotations

import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")

# 开发机/用户特有的痕迹 —— 这些一出现就是写死。
#
# ⚠ 这套模式是**共享的**：`tools/audit_release.py` 的全仓库扫描直接 import 它，
#   不再各写一份。2026-10-04 之前两处各有一份，而 DSHplugins / ZoteroData
#   只写在这一份里 —— 于是全仓库扫描漏掉了它们，`skills/` 里写死的
#   `D:\DSHplugins\...` 从首个提交起就躺在公开仓库里，一直没人发现。
#
#   另外：Nutstore 只认「盘符 + 路径」形式（`X:\Nutstore`），不认裸词 ——
#   否则 `nutstore:zotero-kb` 这种 rclone 远端名会被误报。
SENSITIVE = re.compile(
    r"(DSHplugins|ZoteroData|[A-Za-z]:[\\/]+Nutstore|"
    + re.escape(os.path.expanduser("~")) + r")",
    re.I)

# 完整的 Windows 绝对路径（盘符 + 至少两级目录）
ABS_PATH = re.compile(r"[A-Za-z]:\\\\?[^\"'\s,;)]{4,}")

# 合理的动态取值/系统常量，命中就不算写死
BENIGN = re.compile(
    r"(dirsvc|getenv|expanduser|%LOCALAPPDATA%|%APPDATA%|%USERPROFILE%|"
    r"C:\\\\Users\\\\Public|C:\\\\ProgramData|"
    r"Zotero\.File\.pathToFile|PathUtils\.)")

# 注释行（// 或 * 或 # 开头）—— 文档里的举例不算
COMMENT = re.compile(r"^\s*(//|\*|/\*|#)")


def strip_code(text: str) -> list[tuple[int, str]]:
    """返回 [(行号, 行内容)]，跳过注释行。"""
    out = []
    for i, ln in enumerate(text.split("\n"), 1):
        if COMMENT.match(ln):
            continue
        out.append((i, ln))
    return out


def check_xpi(path: str) -> tuple[int, list[str]]:
    """返回 (问题数, 问题描述列表)。"""
    if not os.path.isfile(path):
        return 1, [f"找不到 {path}"]
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith((".js", ".json", ".xhtml", ".html", ".css")):
                continue
            try:
                text = z.read(name).decode("utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in strip_code(text):
                if not ABS_PATH.search(line) and not SENSITIVE.search(line):
                    continue
                if BENIGN.search(line):
                    continue      # 动态取值 / 系统常量 / 环境变量
                problems.append(f"{name}:{lineno}  {line.strip()[:100]}")
    return len(problems), problems


def main() -> int:
    print("=" * 70)
    print("xpi 写死路径检查（打包出来的东西，不是源码）")
    print("=" * 70)
    xpis = sorted(
        (os.path.join(PLUGIN, n) for n in os.listdir(PLUGIN)
         if n.endswith(".xpi")),
        key=os.path.getmtime, reverse=True)
    if not xpis:
        print("  [XX] 还没打包过 xpi，先跑 tools\\check_plugin.py")
        return 1
    target = xpis[0]
    print(f"  检查：{os.path.relpath(target, ROOT)}")

    n, problems = check_xpi(target)
    print()
    if problems:
        print(f"  发现 {n} 处疑似写死的本机路径：")
        for p in problems:
            print(f"    {p}")
        print()
        print("  ⚠ 这些会让别人装上后指向不存在的目录。")
        print("    正常做法：Services.dirsvc / PathUtils / 环境变量 / 用户配置。")
        return 1

    print("  [OK] 包内没有写死的本机路径")
    print("  （Services.dirsvc、%LOCALAPPDATA%、C:\\Users\\Public 等"
          "动态取值/系统常量不算）")
    with zipfile.ZipFile(target) as z:
        print(f"\n  包内 {len(z.namelist())} 个文件："
              + ", ".join(z.namelist()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
