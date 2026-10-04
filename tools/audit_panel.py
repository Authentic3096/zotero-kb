"""管理面板体检：检查每个按钮背后的命令、文件、参数是否真的可用。

    python tools/audit_panel.py

为什么要它：面板的按钮是"点了才报错"，而有些错误（文件不存在、子命令
拼错、参数名不对）完全可以在点之前就查出来。本机已经栽过两次：
    · 「列出全部经验」→ ModuleNotFoundError（gui.py 漏加 online\\ 路径）
    · 「生成分类建议」→ 弹"还没做"（在等一个从未写过的 offline/taxonomy.py）
两次都是"按钮在那儿但背后是空的"。这个脚本专门盯这类问题。

检查方式（不真跑长任务，只做静态可达性验证）：
  1. 面板里每个 `self.run(...)` / `self.run_async(...)` 的命令
     → 脚本文件存在吗？子命令在该脚本的 argparse 里吗？
  2. 每个 `self.do_xxx` 回调 → 方法真的定义了吗？
  3. 内联的 `[...]` 命令参数里的路径 → 存在吗？
"""

from __future__ import annotations

import ast
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GUI = os.path.join(HERE, "gui.py")
PANELS = os.path.join(HERE, "panels")

PASS = FAIL = 0
PROBLEMS: list[str] = []


def panel_files() -> list[str]:
    """面板的全部源码：入口 + panels/ 下的页签模块。

    ⚠ 必须**都扫**：拆成包以后 `do_*` 回调散在各个 mixin 里，只看 gui.py
      的话每个按钮都会被判成"没有定义"（那是最典型的"审计器改一半"）。
    """
    return [GUI] + sorted(glob.glob(os.path.join(PANELS, "*.py")))


def panel_source() -> str:
    """把面板源码拼成一份文本，供正则/ast 检查。

    为什么要删 `from __future__ import ...`：拼起来的文本里，它出现在中间就是
    SyntaxError（那条 import 必须在文件开头），会让整个 ast.parse 崩掉。
    它对本脚本要查的东西没有任何影响。
    """
    parts = []
    for path in panel_files():
        text = open(path, encoding="utf-8").read()
        text = re.sub(r"^from __future__ import .*$", "", text, flags=re.M)
        parts.append(f"# ===== {os.path.relpath(path, ROOT)} =====\n{text}")
    return "\n".join(parts)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        PROBLEMS.append(f"{name}  {detail}")
        print(f"  FAIL  {name}  {detail}")


def subcommands_of(path: str) -> set[str]:
    """读某个 CLI 脚本的 argparse 子命令。"""
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return set()
    return set(re.findall(r'add_parser\(\s*["\']([^"\']+)["\']', src))


def main() -> int:
    print("=" * 70)
    print("管理面板体检")
    print("=" * 70)
    src = panel_source()
    files = panel_files()
    print(f"  扫描 {len(files)} 个文件："
          + "、".join(os.path.relpath(f, ROOT) for f in files))
    tree = ast.parse(src)

    # ---------------- 1. 回调方法都存在吗
    print("\n[1] 按钮回调是否都有定义")
    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
    called = set(re.findall(r"self\.(do_\w+)", src))
    missing = sorted(c for c in called if c not in defined)
    check(f"{len(called)} 个 do_* 回调都有定义", not missing,
          f"缺 {missing}" if missing else "")

    # ---------------- 2. 每个 CLI 调用：脚本存在 + 子命令存在
    print("\n[2] 面板调用的 CLI 脚本与子命令")
    # 收集所有形如 [os.path.join(ROOT, "a", "b.py"), "subcmd", ...] 的调用
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        elts = node.elts
        if len(elts) < 2:
            continue
        first, second = elts[0], elts[1]
        # 第一个元素是 os.path.join(ROOT, ...) 或者 Path(...)
        if not isinstance(first, ast.Call):
            continue
        if not (isinstance(first.func, ast.Attribute)
                and first.func.attr == "join"):
            continue
        # 拼出相对路径
        parts = []
        for a in first.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                parts.append(a.value)
            elif isinstance(a, ast.Name) and a.id in ("ROOT", "HERE"):
                continue
        if not parts:
            continue
        rel = os.path.join(*parts)
        if not isinstance(second, ast.Constant) or not isinstance(second.value, str):
            continue
        calls.append((rel, second.value, node.lineno))

    seen = set()
    for rel, sub, lineno in calls:
        key = (rel, sub)
        if key in seen:
            continue
        seen.add(key)
        full = os.path.join(ROOT, rel)
        if not os.path.exists(full):
            check(f"{rel} 存在（行 {lineno}）", False, "文件不存在")
            continue
        subs = subcommands_of(full)
        if not subs:
            # 没有 argparse 的脚本（如 taxonomy.py 那种）——只要求文件在
            check(f"{rel} 存在（行 {lineno}）", True)
            continue
        ok = sub in subs
        check(f"{rel} 支持子命令 {sub}", ok,
              f"实际支持 {sorted(subs)}" if not ok else "")
    if not calls:
        print("  （没有解析到 CLI 调用，检查正则是否失效）")

    # ---------------- 3. 关键能力是否都有入口
    print("\n[3] 关键能力是否有面板入口")
    expectations = [
        ("列出全部经验", r"do_exp_all"),
        ("查询经验", r"do_exp_list"),
        ("待确认清单", r"do_pending"),
        ("从会话记录补经验", r"do_learn"),
        ("标为重点", r"do_weight"),
        ("搜索", r"do_search"),
        ("检查模型服务", r"do_ai_status"),
        ("打标签", r"do_ai_tag"),
        ("生成要点", r"do_ai_summary"),
        ("生成分类建议", r"do_taxonomy"),
        ("单篇分类建议", r"do_taxonomy_one"),
        ("列出分类", r"do_coll_list"),
        ("分类分布", r"do_coll_stats"),
        ("Zotero 写入能力", r"do_write_check"),
        ("重载技能", r"do_sync_skill"),
        ("升级前检查", r"do_upgrade_check"),
        ("备份", r"do_upgrade_backup"),
        ("升级后核对", r"do_upgrade_verify"),
        ("启动服务", r"do_service_start"),
        ("服务状态", r"do_service_status"),
        ("复制 token", r"do_copy_token"),
        ("打包插件", r"do_build_plugin"),
        ("侧载状态", r"do_sideload_status"),
        ("诊断安装", r"do_diagnose"),
        ("分类重整五步", r"do_sync_check"),
    ]
    for label, pat in expectations:
        check(f"有入口：{label}", re.search(pat, src) is not None, "面板里找不到")

    # ---------------- 4. 已知的"空实现"模式
    print("\n[4] 是否残留'还没做'之类的空实现")
    # ⚠ 只看**代码**，不看字符串/注释 —— 否则文档里写「之前点了只弹"还没做"」
    #   这种说明文字也会被判成问题（本机第一次跑就这样误报了一条）。
    bad_patterns = [
        (r'还没做', "弹窗写着「还没做」"),
        (r'还没实现', "弹窗写着「还没实现」"),
        (r'raise NotImplementedError', "未实现"),
    ]
    # 收集所有字符串字面量与注释的行号，检查时排除
    skip_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for ln in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
                skip_lines.add(ln)
    for i, ln in enumerate(src.splitlines(), 1):
        if ln.lstrip().startswith("#"):
            skip_lines.add(i)

    for pat, desc in bad_patterns:
        hits = [f"行 {i+1}" for i, ln in enumerate(src.splitlines(), 1)
                if re.search(pat, ln) and i not in skip_lines]
        check(f"没有「{desc}」（只查代码，不查字符串/注释）", not hits,
              f"{hits[:3]}" if hits else "")

    # ---------------- 5. 面板依赖的模块都能导入
    print("\n[5] 面板依赖的模块能否导入")
    sys.path.insert(0, HERE)
    sys.path.insert(0, os.path.join(ROOT, "offline"))
    sys.path.insert(0, os.path.join(ROOT, "online"))
    for mod in ("schemas", "searcher", "converter", "query"):
        try:
            __import__(mod)
            check(f"能导入 {mod}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"能导入 {mod}", False, f"{type(exc).__name__}: {exc}")
    check("judge 有 classify_item（面板与插件共用）",
          "def classify_item(" in open(os.path.join(ROOT, "offline", "judge.py"),
                                       encoding="utf-8").read())

    print("\n" + "=" * 70)
    print(f"通过 {PASS}　失败 {FAIL}")
    if PROBLEMS:
        print("\n需要处理：")
        for p in PROBLEMS:
            print(f"  · {p}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
