"""开/关 Zotero 调试日志，并抓取启动时的插件加载错误。

    python tools/zotero_debug.py on      # 打开调试日志（改 prefs.js，需先关 Zotero）
    python tools/zotero_debug.py off     # 关掉
    python tools/zotero_debug.py read    # 读日志，挑出插件相关的报错

背景：Zotero 默认**不写** zotero.log，而且 UI 那句
"无法安装插件…它可能无法与该版本的 Zotero 兼容" 是笼统文案，真正原因
（比如 `Add-on X is not compatible with application version. add-on
minVersion: … maxVersion: …`）只在调试日志里。

本机实测发现的关键事实（都来自读 Zotero 自己的代码 omni.ja）：
  · `XPIProvider.readAddons()`：extensions 目录里**文件名等于插件 id** 的文件
    会被当作"指针文件"读取内容；内容不是有效目录路径就**直接删掉**。
    所以代理文件的文件名**不能带 .xpi**（我一开始写成 `id.xpi`，被删了）。
  · `getExpectedID()`：`<id>.xpi` 会剥掉后缀当作插件 id —— 真 xpi 用这个命名。
  · `XPIDatabase` 里有 "Rebuilding add-ons database from installed extensions"，
    删 `extensions.lastAppBuildId` 会触发重建。
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROFILE_ROOT = os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                            "Zotero", "Zotero", "Profiles")
BACKUP_ROOT = os.path.join(ROOT, "kb", "profile-backup")

# Zotero 的调试相关首选项
DEBUG_PREFS = {
    "extensions.logging.enabled": "true",       # 记录扩展管理器日志
    "zotero.debug.log": "true",                 # Zotero 自己的调试日志
    "zotero.debug.store": "true",               # 存到文件
}


def find_profile() -> str:
    if not os.path.isdir(PROFILE_ROOT):
        return ""
    cands = [os.path.join(PROFILE_ROOT, d) for d in os.listdir(PROFILE_ROOT)
             if os.path.exists(os.path.join(PROFILE_ROOT, d, "prefs.js"))]
    return max(cands, key=lambda c: os.path.getmtime(os.path.join(c, "prefs.js"))) if cands else ""


def zotero_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                             capture_output=True, text=True, timeout=20)
        return "zotero.exe" in (out.stdout or "").lower()
    except Exception:  # noqa: BLE001
        return False


def set_prefs(profile: str, prefs: dict) -> None:
    """在 prefs.js 里增删 user_pref 行（Zotero 退出后才会重写，所以要它先关）。"""
    p = os.path.join(profile, "prefs.js")
    lines = open(p, encoding="utf-8").read().splitlines(keepends=True)
    keys = set(prefs)
    kept = [ln for ln in lines
            if not any(f'user_pref("{k}"' in ln for k in keys)]
    for k, v in prefs.items():
        if v is None:
            continue
        if isinstance(v, bool):
            kept.append(f'user_pref("{k}", {str(v).lower()});\n')
        elif isinstance(v, int):
            kept.append(f'user_pref("{k}", {v});\n')
        else:
            kept.append(f'user_pref("{k}", "{v}");\n')
    with open(p, "w", encoding="utf-8") as fh:
        fh.writelines(kept)


def cmd_on() -> int:
    profile = find_profile()
    if not profile:
        print("找不到 profile")
        return 1
    if zotero_running():
        print("请先完全退出 Zotero 再执行（它会重写 prefs.js）")
        return 1
    p = os.path.join(profile, "prefs.js")
    dst = os.path.join(BACKUP_ROOT, "prefs-before-debug.js")
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    open(dst, "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
    set_prefs(profile, {**DEBUG_PREFS, "extensions.lastAppVersion": None,
                        "extensions.lastAppBuildId": None})
    print(f"已打开调试日志，并清掉 lastApp 键（强制重扫）")
    print(f"prefs.js 备份：{dst}")
    print("\n现在启动 Zotero，等它完全起来（约 30 秒），然后跑：")
    print("  python tools/zotero_debug.py read")
    return 0


def cmd_off() -> int:
    profile = find_profile()
    if not profile:
        return 1
    if zotero_running():
        print("请先完全退出 Zotero")
        return 1
    set_prefs(profile, {k: None for k in DEBUG_PREFS})
    print("已关闭调试日志")
    return 0


def find_logs() -> list[str]:
    out: list[str] = []
    for base in (PROFILE_ROOT,
                 os.path.join(os.path.expanduser("~"), "AppData", "Local", "Zotero")):
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".log"):
                    out.append(os.path.join(root, f))
    return out


def cmd_read() -> int:
    print("=" * 70)
    print("Zotero 调试日志里的插件相关记录")
    print("=" * 70)
    logs = find_logs()
    if not logs:
        print("没找到任何 .log 文件。")
        print("先跑 zotero_debug.py on → 启动 Zotero → 再跑本命令")
        return 1
    pat = re.compile(
        r"zotero-kb|addon|add-on|extension|XPI|install|compatib|"
        r"minVersion|maxVersion|scanned|Ignoring file|Deleting|failed",
        re.I)
    total = 0
    for path in logs:
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        hits = [ln for ln in lines[-8000:] if pat.search(ln)]
        # 优先显示明确提到我们插件的
        ours = [ln for ln in hits if "zotero-kb" in ln.lower()]
        if not hits:
            continue
        print(f"\n--- {path}（{len(lines)} 行，命中 {len(hits)}）---")
        if ours:
            print(f"  ★ 提到 zotero-kb 的 {len(ours)} 条：")
            for ln in ours[-25:]:
                print(f"    {ln.strip()[:170]}")
            total += len(ours)
        errs = [ln for ln in hits if re.search(
            r"not compatible|incompatible|Failed to|failed to|error|Ignoring file|Deleting",
            ln, re.I)]
        if errs:
            print(f"  其他可疑 {len(errs)} 条（末 12）：")
            for ln in errs[-12:]:
                print(f"    {ln.strip()[:170]}")
    if total == 0:
        print("\n日志里没有提到 zotero-kb —— 说明 Zotero 压根没把它当候选插件读进去，")
        print("或者日志没抓到那一段。把上面显示的内容发我。")
    print("=" * 70)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["on", "off", "read"])
    args = ap.parse_args()
    return {"on": cmd_on, "off": cmd_off, "read": cmd_read}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
