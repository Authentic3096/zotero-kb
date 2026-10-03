"""插件诊断：把"无法安装插件…可能无法与该版本的 Zotero 兼容"的真实原因找出来。

    python tools/diagnose_plugin.py

Zotero 那句报错是**笼统**的（manifest 读不了、版本范围不匹配、图标缺失、
ZIP 结构异常、profile 里已有同名残留……都报同一句）。所以这里逐项验证，
并去读 Zotero 自己的日志找确切原因。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
PLUGIN = os.path.join(ROOT, "zotero-plugin")

ZOTERO_DIR = r"D:\Application\Zotero"
PROFILE_ROOT = os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                            "Zotero", "Zotero", "Profiles")

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


def local_zotero_version() -> str:
    exe = os.path.join(ZOTERO_DIR, "zotero.exe")
    if not os.path.exists(exe):
        return "?"
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=20)
        return (out.stdout or "").strip() or "?"
    except Exception:  # noqa: BLE001
        return "?"


def ver_tuple(v: str) -> tuple:
    out = []
    for p in re.split(r"[._-]", v or ""):
        if p == "*":
            out.append(float("inf"))
        elif p.isdigit():
            out.append(int(p))
        elif p:
            out.append(p)
    return tuple(out)


def cmp_ver(a: str, b: str) -> int:
    ta, tb = ver_tuple(a), ver_tuple(b)
    for x, y in zip(ta, tb):
        if x == y:
            continue
        if isinstance(x, str) or isinstance(y, str):
            return -1 if str(x) < str(y) else 1
        return -1 if x < y else 1
    return (len(ta) > len(tb)) - (len(ta) < len(tb))


def find_xpi() -> str:
    cands = sorted(glob.glob(os.path.join(PLUGIN, "*.xpi")))
    return cands[-1] if cands else ""


def find_profiles() -> list[str]:
    if not os.path.isdir(PROFILE_ROOT):
        return []
    return [os.path.join(PROFILE_ROOT, d) for d in os.listdir(PROFILE_ROOT)
            if os.path.isdir(os.path.join(PROFILE_ROOT, d))]


def main() -> int:
    print("=" * 70)
    print("Zotero 插件安装诊断")
    print("=" * 70)
    problems: list[str] = []

    zv = local_zotero_version()
    print(f"\n[1] 环境")
    print(f"      本机 Zotero：{zv}")
    xpi = find_xpi()
    if not xpi:
        print(f"{BAD}找不到 xpi，先跑 tools\\check_plugin.py 打包")
        return 1
    print(f"      待装插件：{os.path.basename(xpi)}（{os.path.getsize(xpi)} 字节）")
    if zv == "?":
        problems.append("读不到 Zotero 版本")

    print(f"\n[2] 包内容与 ZIP 结构")
    try:
        with zipfile.ZipFile(xpi) as z:
            bad = z.testzip()
            names = z.namelist()
            infos = z.infolist()
    except zipfile.BadZipFile as exc:
        print(f"{BAD}不是合法 ZIP：{exc}")
        return 1
    print(f"{OK if not bad else BAD}ZIP 完整性：{'好' if not bad else bad}")
    if bad:
        problems.append("ZIP 内容损坏")
    has_top_dir = not any(n == "manifest.json" for n in names)
    print(f"{OK if not has_top_dir else BAD}manifest.json 在包根目录：{not has_top_dir}")
    if has_top_dir:
        problems.append("manifest.json 不在根目录（被目录包裹了，装不上）")
    for must in ("manifest.json", "bootstrap.js"):
        hit = must in names
        print(f"{OK if hit else BAD}{must} 存在：{hit}")
        if not hit:
            problems.append(f"包里缺 {must}")
    for info in infos[:6]:
        print(f"        {info.filename:16} sys={info.create_system} "
              f"attr={info.external_attr:#010x} flag={info.flag_bits:#06x}")

    print(f"\n[3] manifest 合法性")
    try:
        with zipfile.ZipFile(xpi) as z:
            raw = z.read("manifest.json")
        if raw[:3] == b"\xef\xbb\xbf":
            print(f"{BAD}manifest.json 有 BOM —— 会解析失败")
            problems.append("manifest 有 BOM")
        else:
            print(f"{OK}无 BOM")
        m = json.loads(raw.decode("utf-8"))
        print(f"{OK}JSON 可解析")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}manifest 读不了：{exc}")
        problems.append("manifest 不可解析")
        m = {}

    app = (m.get("applications") or m.get("browser_specific_settings") or {}).get("zotero") or {}
    pid = str(app.get("id", ""))
    if "@" in pid and "." in pid.split("@")[-1]:
        print(f"{OK}插件 id 形式规范：{pid}")
    else:
        print(f"{WARN}插件 id 不太规范：{pid!r}（建议形如 name@some.domain）")
        problems.append(f"插件 id 形式可疑：{pid!r}")
    if m.get("manifest_version") != 2:
        print(f"{BAD}manifest_version 应为 2，实际 {m.get('manifest_version')}")
        problems.append("manifest_version 不是 2")

    lo = str(app.get("strict_min_version", ""))
    hi = str(app.get("strict_max_version", ""))
    print(f"\n[4] 版本范围 vs 本机 Zotero")
    print(f"      声明：{lo} ~ {hi}    本机：{zv}")
    if not lo or not hi:
        print(f"{BAD}缺 strict_min_version 或 strict_max_version")
        problems.append("缺版本范围字段")
    else:
        ok_min = cmp_ver(zv, lo) >= 0
        if hi.endswith(".*"):
            base = hi[:-2]
            ok_max = zv == base or zv.startswith(base + ".")
        else:
            ok_max = cmp_ver(zv, hi) <= 0
        print(f"{OK if ok_min else BAD}min 检查：Zotero({zv}) >= {lo} → {ok_min}")
        print(f"{OK if ok_max else BAD}max 检查：Zotero({zv}) <= {hi} → {ok_max}")
        if not (ok_min and ok_max):
            problems.append(f"版本范围不覆盖 {zv}")

    # 图标
    icons = m.get("icons") or {}
    for size, rel in icons.items():
        print(f"{OK if rel in names else BAD}图标 {size} → {rel}"
              f"{'' if rel in names else '（**文件不在包里**）'}")
        if rel not in names:
            problems.append(f"图标 {rel} 不在包里")

    print(f"\n[5] profile 里是否已有同名残留（会干扰安装）")
    for prof in find_profiles():
        hit = []
        for pat in ("extensions", ""):
            d = os.path.join(prof, pat) if pat else prof
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if "zotero-kb" in f.lower():
                    hit.append(os.path.join(pat, f) if pat else f)
        if hit:
            print(f"{WARN}{os.path.basename(prof)}：{hit}")
            problems.append(f"{os.path.basename(prof)} 里有 zotero-kb 残留，"
                            f"先在 Zotero 里卸载旧版再装")
        else:
            print(f"{OK}{os.path.basename(prof)}：无残留")

    print(f"\n[6] Zotero 日志里的安装错误")
    found_log = False
    for prof in find_profiles():
        for name in ("zotero.log", "zotero.debug.log"):
            p = os.path.join(prof, name)
            if not os.path.exists(p):
                continue
            found_log = True
            try:
                lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            hits = [ln for ln in lines[-4000:]
                    if re.search(r"zotero-kb|plugin.*install|install.*plugin|"
                                 r"incompatible|not compatible|Failed to (load|install)",
                                 ln, re.I)]
            print(f"      {name}（{len(lines)} 行）命中 {len(hits)} 条：")
            for ln in hits[-12:]:
                print(f"        {ln.strip()[:150]}")
    if not found_log:
        print(f"{WARN}没有日志文件 —— Zotero 默认不写日志。")
        print("      要看真实原因，请开一次调试日志：")
        print("        Zotero → 帮助 → Debug Output Logging → Enable")
        print("        再试一次安装（失败后）")
        print("        然后 Zotero → 帮助 → Debug Output Logging → View Output")
        print("        （或再跑一次本脚本，它会自动读日志）")

    print("\n" + "=" * 70)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    else:
        print("从包本身看**没有发现问题** —— 包是合法的、版本范围也覆盖本机。")
        print("那问题多半在 Zotero 侧的加载阶段（bootstrap 执行或 profile 状态）。")
        print("请按上面 [6] 开启调试日志后再试一次，把日志内容发我。")
    print("=" * 70)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
