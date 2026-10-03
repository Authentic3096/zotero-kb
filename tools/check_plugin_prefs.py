"""打印插件的 prefs 与本地服务 token 的对照，快速判断"是不是 token 没填"。

    python tools/check_plugin_prefs.py
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402


def find_profile() -> str:
    """找 Zotero 的 profile 目录（自动挑有 prefs.js 的那个）。

    ⚠ 不要写死 `C:\\Users\\<用户名>\\...` —— 既泄露用户名，别人机器上也跑不了。
      从 %APPDATA% 拼即可。
    """
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    base = os.path.join(appdata, "Zotero", "Zotero", "Profiles")
    try:
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if os.path.isfile(os.path.join(p, "prefs.js")):
                return p
    except OSError:
        pass
    return base


PROFILE = find_profile()
PREFS = os.path.join(PROFILE, "prefs.js")
TOKEN = os.path.join(S.KB_DIR, "service-token.txt")

KEYS = ["server", "token", "model", "autoProcess", "autoTaskPoll", "createMissing"]

# 插件在 prefs.js 里的实际键名前缀。
#
# ⚠ `Zotero.Prefs.get("zotero-kb.X")` 会自动补 `extensions.zotero.`
#   （omni.ja 的 config.mjs：PREF_BRANCH = 'extensions.zotero.'），
#   所以落到 prefs.js 的键是 `extensions.zotero.zotero-kb.X`。
#   本机踩过双前缀的坑（代码里写完整键 → 变成
#   `extensions.zotero.extensions.zotero-kb.X` → 插件读不到自己的配置），
#   所以三种前缀都看一眼，并报出命中的是哪种 —— 那是判断
#   "键名有没有写错"最直接的证据。
PREFIX_NEW = "extensions.zotero.zotero-kb."
PREFIX_OLD = "extensions.zotero-kb."
PREFIX_LEGACY_BAD = "extensions.zotero.extensions.zotero-kb."


def read_pref(src: str, key: str) -> str:
    """读一个 pref。按 新键 → 老键 → 双前缀（错误） 顺序找。"""
    for prefix in (PREFIX_NEW, PREFIX_OLD, PREFIX_LEGACY_BAD):
        pat = 'user_pref("' + prefix + key + '", '
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(pat):
                rest = s[len(pat):]
                if rest.endswith(");"):
                    rest = rest[:-2]
                return rest.strip()
    return "(未设置)"


def which_prefix(src: str) -> str:
    """报告 prefs.js 里用的是哪种前缀。"""
    if PREFIX_LEGACY_BAD in src:
        return "双前缀 —— 错误！插件读不到，见 README 的 pref 键说明"
    if PREFIX_NEW in src:
        return "正确（extensions.zotero.zotero-kb.*）"
    if PREFIX_OLD in src:
        return "老的单前缀（extensions.zotero-kb.*）—— 插件读不到"
    return "没有任何 zotero-kb 的键"


def main() -> int:
    print("=" * 66)
    print("插件 prefs 与服务 token 对照")
    print("=" * 66)
    print(f"  profile：{PROFILE}")
    if not os.path.exists(PREFS):
        print(f"  [XX] 找不到 {PREFS}")
        return 1
    src = open(PREFS, encoding="utf-8", errors="replace").read()
    pref_kind = which_prefix(src)
    print(f"  pref 前缀：{pref_kind}")
    if pref_kind.startswith("双前缀") or pref_kind.startswith("老的单前缀"):
        print("  ⚠ 键名不对 —— 请升级插件到 0.19+ 并重启 Zotero，"
              "老键可以跑 tools\\migrate_prefs.py 迁过来")

    print("\n[插件 prefs]")
    vals = {}
    for k in KEYS:
        v = read_pref(src, k)
        vals[k] = v
        print(f"  zotero-kb.{k:14} = {v}")

    svc = ""
    if os.path.exists(TOKEN):
        svc = open(TOKEN, encoding="utf-8").read().strip()
    print(f"\n[服务 token]\n  {svc or '(读不到)'}")

    # 关键判定：/health 不需要 token，所以 serverOk=true 不能证明 token 对；
    # 而 /weights、/task 都需要 token —— token 没填就会 401，现象正是
    # "列显示了但没有值 / 任务一直 pending"。
    plug = vals.get("token", "")
    plug = plug.strip().strip('"')
    print("\n[判定]")
    if not plug:
        print("  [XX] 插件里**没有填 token** —— 这就是权重列没值的原因：")
        print("       /health 不需要 token（所以 serverOk=true，看着像连上了），")
        print("       但 /weights 和 /task 都需要 → 全部 401。")
        print("\n  修法（二选一）：")
        print("   · Zotero → 设置 → 文献知识库 → 把下面这串 token 粘进去：")
        print(f"       {svc}")
        print("   · 或跑：python tools\\set_plugin_prefs.py   （我来自动写）")
        return 1
    if plug != svc:
        print(f"  [XX] token 不一致：")
        print(f"       插件里 = {plug}")
        print(f"       服务端 = {svc}")
        return 1
    print("  [OK] token 一致，服务也在线。若列仍无值，看 plugin-status.json")
    print("       里的 providerCalls / providerHits 与 lastRequestError。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
