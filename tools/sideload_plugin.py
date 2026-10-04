"""侧载插件到 Zotero（绕过 UI 安装的兼容性检查）。

    python tools/sideload_plugin.py install    # 装（会先备份关键文件）
    python tools/sideload_plugin.py remove     # 卸（清掉侧载痕迹）
    python tools/sideload_plugin.py status     # 看当前状态

原理来自官方文档（Zotero Plugin Development → Setting Up a Plugin
Development Environment）：

  1. 把一个文件放进 profile 的 `extensions\\`，文件名 = 插件 id；
  2. 删掉 profile `prefs.js` 里的 `extensions.lastAppBuildId` 与
     `extensions.lastAppVersion` —— 这会强制 Zotero 重新扫描 extensions 目录；
  3. 重启 Zotero，插件即被加载。

这条路**绕过了 UI 安装时的那套兼容性判断**（"可能无法与该版本的 Zotero 兼容"
就是卡在那里），所以当 UI 安装被拒、而包本身合法时，用它。

两种放法都支持：
  · **代理文件**（官方推荐给开发者）：文件内容 = 插件源码目录的绝对路径。
    好处是改完源码重启即生效。
  · **xpi 拷贝**：文件名 = 插件 id + `.xpi`，内容就是 xpi。本机已有的 6 个
    插件在 extensions 目录里就是这种形式。

注意：操作前必须**关掉 Zotero**，否则它退出时会把 prefs.js 和 extensions.json
写回去，覆盖我们的改动。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
PROFILE_ROOT = os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                            "Zotero", "Zotero", "Profiles")
BACKUP_ROOT = os.path.join(ROOT, "kb", "profile-backup")
PLUGIN_ID = "zotero-kb@authentic3096.github.io"

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


def find_profile() -> str:
    """找正在用的 profile（含 prefs.js 的那个）。"""
    if not os.path.isdir(PROFILE_ROOT):
        return ""
    cands = [os.path.join(PROFILE_ROOT, d) for d in os.listdir(PROFILE_ROOT)]
    cands = [c for c in cands if os.path.exists(os.path.join(c, "prefs.js"))]
    if not cands:
        return ""
    # 取最近改动的那个（正在用的）
    return max(cands, key=lambda c: os.path.getmtime(os.path.join(c, "prefs.js")))


def zotero_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                             capture_output=True, text=True, timeout=20)
        return "zotero.exe" in (out.stdout or "").lower()
    except Exception:  # noqa: BLE001
        return False


def current_xpi() -> str:
    cands = sorted(glob.glob(os.path.join(PLUGIN, "*.xpi")))
    return cands[-1] if cands else ""


def backup(profile: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(dst, exist_ok=True)
    for name in ("prefs.js", "extensions.json"):
        src = os.path.join(profile, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, name))
            print(f"  已备份 {name}")
    return dst


def strip_lastapp(profile: str) -> list[str]:
    """删掉 prefs.js 里的 extensions.lastAppVersion / lastAppBuildId。

    这两行是 Zotero 用来判断"是否需要重扫扩展"的。删掉后启动会重扫，
    插件就被装上了（官方文档的做法）。
    """
    p = os.path.join(profile, "prefs.js")
    if not os.path.exists(p):
        return []
    lines = open(p, encoding="utf-8").read().splitlines(keepends=True)
    removed, kept = [], []
    for ln in lines:
        if ("extensions.lastAppVersion" in ln
                or "extensions.lastAppBuildId" in ln):
            removed.append(ln.strip())
        else:
            kept.append(ln)
    if removed:
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
    return removed


def cmd_status() -> int:
    print("=" * 66)
    print("插件侧载状态")
    print("=" * 66)
    profile = find_profile()
    if not profile:
        print(f"{BAD}找不到 Zotero profile")
        return 1
    print(f"      profile：{profile}")
    print(f"{'[!!]' if zotero_running() else '[OK]'} Zotero "
          f"{'正在运行（改配置前必须关掉）' if zotero_running() else '未运行'}")

    ext_dir = os.path.join(profile, "extensions")
    print(f"\n[extensions 目录]")
    if os.path.isdir(ext_dir):
        for f in sorted(os.listdir(ext_dir)):
            full = os.path.join(ext_dir, f)
            mark = "  ← 本插件" if "zotero-kb" in f else ""
            print(f"      {f:44} {os.path.getsize(full):>9,}B{mark}")

    # prefs.js 里的 lastApp 键
    pj = os.path.join(profile, "prefs.js")
    hits = []
    if os.path.exists(pj):
        hits = [ln.strip() for ln in open(pj, encoding="utf-8")
                if "extensions.lastApp" in ln]
    print(f"\n[prefs.js 的 lastApp 键]（有这几行=Zotero 不会重扫扩展）")
    for h in hits:
        print(f"      {h}")
    if not hits:
        print("      （已清除 —— 下次启动 Zotero 会重扫 extensions 目录）")

    # 注册表里有没有
    ej = os.path.join(profile, "extensions.json")
    if os.path.exists(ej):
        try:
            raw = "\n".join(ln for ln in open(ej, encoding="utf-8").read().splitlines()
                            if not ln.strip().startswith("//"))
            data = json.loads(raw)
            ids = [a.get("id") for a in data.get("addons", [])]
            print(f"\n[注册表 extensions.json] {len(ids)} 个扩展")
            print(f"      本插件已注册：{PLUGIN_ID in ids}")
        except Exception as exc:  # noqa: BLE001
            print(f"{WARN}读 extensions.json 失败：{exc}")
    return 0


def cmd_install(use_proxy: bool = True) -> int:
    print("=" * 66)
    print("侧载插件到 Zotero")
    print("=" * 66)
    profile = find_profile()
    if not profile:
        print(f"{BAD}找不到 Zotero profile")
        return 1
    xpi = current_xpi()
    if not xpi:
        print(f"{BAD}没有 xpi，先跑 tools\\check_plugin.py 打包")
        return 1
    print(f"      profile：{profile}")
    print(f"      插件包：{os.path.basename(xpi)}")

    if zotero_running():
        print(f"\n{BAD}Zotero 正在运行 —— 请先完全退出 Zotero 再跑本命令。")
        print("      原因：Zotero 退出时会把 prefs.js / extensions.json 写回去，")
        print("            覆盖我们的改动，导致侧载不生效。")
        return 1

    dst = backup(profile)
    print(f"      备份到：{dst}")

    ext_dir = os.path.join(profile, "extensions")
    os.makedirs(ext_dir, exist_ok=True)

    # ⚠ 文件名规则（读 XPIProvider.readAddons 得出，本机踩过）：
    #   · 指针/代理文件：文件名必须**正好等于插件 id**（不带 .xpi）。
    #     代码是 `if (id == entry.leafName && entry.isFile())` → 走 _readLinkFile
    #     读文件内容当目录路径；内容无效就 `entry.remove(true)` 删掉。
    #     我一开始写成 `zotero-kb@authentic3096.github.io.xpi`，于是它被当普通文件、
    #     又不是合法 xpi，直接被清理（实测 xpi 也一起被删）。
    #   · 真 xpi：文件名 = `<id>.xpi`，getExpectedID 会剥掉后缀得到 id。
    if use_proxy:
        target = os.path.join(ext_dir, PLUGIN_ID)          # ← 不带 .xpi
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(PLUGIN)
        print(f"{OK}已放代理文件：{target}")
        print(f"       （文件名 = 插件 id，不带 .xpi —— 这是 XPIProvider 的要求）")
        print(f"       内容 = {PLUGIN}")
    else:
        target = os.path.join(ext_dir, f"{PLUGIN_ID}.xpi")
        shutil.copy2(xpi, target)
        print(f"{OK}已拷贝 xpi 到：{target}")

    # 旧的错命名残留清掉，免得干扰
    stale = os.path.join(ext_dir, f"{PLUGIN_ID}.xpi") if use_proxy else os.path.join(ext_dir, PLUGIN_ID)
    if os.path.exists(stale):
        os.remove(stale)
        print(f"      清理旧残留：{os.path.basename(stale)}")

    removed = strip_lastapp(profile)
    if removed:
        print(f"{OK}已从 prefs.js 删除 {len(removed)} 行，强制 Zotero 重扫扩展：")
        for r in removed:
            print(f"       {r}")
    else:
        print(f"{WARN}prefs.js 里没有 lastApp 键（可能之前已清过）")

    print(f"""
{'=' * 66}
下一步：
  1. 启动 Zotero（它会重扫 extensions 目录并加载插件）
  2. Zotero → 工具 → 插件，应该能看到「Zotero 文献知识库」
  3. 跑 tools\\diagnose_plugin.py 或 tools\\sideload_plugin.py status 确认已注册
  4. 若没出现，看 Zotero → 帮助 → Debug Output Logging 的输出

回退：tools\\sideload_plugin.py remove
      （或把 {dst} 里的 prefs.js 拷回 profile）
{'=' * 66}""")
    return 0


def cmd_remove() -> int:
    print("=" * 66)
    print("移除侧载")
    print("=" * 66)
    profile = find_profile()
    if not profile:
        print(f"{BAD}找不到 profile")
        return 1
    if zotero_running():
        print(f"{BAD}请先完全退出 Zotero。")
        return 1
    ext_dir = os.path.join(profile, "extensions")
    hit = False
    for name in (f"{PLUGIN_ID}.xpi",):
        p = os.path.join(ext_dir, name)
        if os.path.exists(p):
            # 代理文件是文本、xpi 是二进制，都直接删
            os.remove(p)
            print(f"{OK}已删除 {p}")
            hit = True
    # 从 extensions.json 里摘掉（否则 Zotero 会认为它还在但文件没了）
    ej = os.path.join(profile, "extensions.json")
    if os.path.exists(ej):
        try:
            raw = open(ej, encoding="utf-8").read()
            data = json.loads("\n".join(
                ln for ln in raw.splitlines() if not ln.strip().startswith("//")))
            before = len(data.get("addons", []))
            data["addons"] = [a for a in data.get("addons", [])
                              if a.get("id") != PLUGIN_ID]
            after = len(data["addons"])
            if before != after:
                with open(ej, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                print(f"{OK}已从 extensions.json 摘除注册记录")
                hit = True
        except Exception as exc:  # noqa: BLE001
            print(f"{WARN}改 extensions.json 失败：{exc}")
    if not hit:
        print("      没有找到侧载痕迹")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="侧载 Zotero 插件")
    ap.add_argument("action", choices=["install", "remove", "status"],
                    nargs="?", default="status")
    ap.add_argument("--copy-xpi", action="store_true",
                    help="用拷贝 xpi 的方式（默认用代理文件指向源码目录）")
    args = ap.parse_args()
    if args.action == "install":
        return cmd_install(use_proxy=not args.copy_xpi)
    if args.action == "remove":
        return cmd_remove()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
