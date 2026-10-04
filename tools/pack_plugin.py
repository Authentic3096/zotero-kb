"""把插件目录打成 Zotero 认得的 xpi（严格对齐已知可用插件的 ZIP 格式）。

    python tools/pack_plugin.py

为什么要单独控制 ZIP 细节：Zotero 拒绝安装时报的是
「无法安装插件…它可能无法与该版本的 Zotero 兼容」——这句**看不出真正原因**。
逐字节对比本机一个能正常用的插件（pdf2zh）后发现差异在 ZIP 条目属性上：

    可用插件：create_system=3(Unix)、flag_bits 含 0x0800(UTF-8 名)、
              external_attr=0x81a40000(权限 0644)、有目录条目
    我先前打的包：create_system=0(MS-DOS)、flag=0、external_attr=0x81b60000(0666)

Python 的 `zipfile.writestr` 默认就按 MS-DOS 写，所以这里显式设置
`ZipInfo.create_system = 3`、`flag_bits |= 0x800`、`external_attr = 0o644 << 16`，
与可用插件保持一致。

## 打包前的硬性闸门（assert_bootstrap_synced）

xpi 里装的是 **`bootstrap.js`（由 `src/*.js` 拼出来的生成物）**，源码在
`zotero-plugin/src/`。所以只要出现"改了 src 却忘了重新生成"，装进去的就是旧
代码 —— 表现是"插件像没改"，而 Zotero 的报错没有任何线索。这类事故靠人记着
跑 build_bootstrap 是不可靠的，改成**打包这一步直接拒绝**。
"""

from __future__ import annotations

import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
sys.path.insert(0, HERE)

# ⚠ `src/` 是**源码**目录，绝不能进 xpi：
#   · 它里面的文件是 bootstrap.js 的原料（`Object.assign` 片段），单独装进
#     Zotero 不会被执行，白胖 200 KB；
#   · 下面 collect_entries 只收 PLUGIN 根目录下的**文件**，本来就会跳过目录，
#     但这里显式写出来 —— 以后谁想把打包改成递归收集时，必须看到这条。
EXCLUDE_DIRS = {"src"}

# 打进包的文件。
# ⚠ 这里必须**自动列全**，不能只写死几个 —— 曾经因为硬编码列表漏掉了
#   新增的 `toolbar-icon.svg`，结果代码里引用的图标在包里不存在，
#   工具栏按钮就变成一个空白/巨大图标，排查了好一会儿。
# 规则：manifest.json 放第一个（部分解析器依赖它先出现），其余按扩展名收集，
#       但排除源码/文档/诊断脚本等**运行时用不到**的东西 —— 之前自动收集时
#       把 verify-plugin.js / diag-load.js 这些开发用脚本也打进去了（白胖 20KB）。
EXCLUDE_EXT = {".xpi", ".md", ".bak", ".log", ".txt", ".zip"}
EXCLUDE_NAME = {"package.json", "package-lock.json"}
# 开发/诊断脚本，不进 xpi（它们是给"在 Zotero 里跑"用的，不是插件运行时代码）
# ⚠ 新增诊断脚本**记得加到这里** —— 漏了会被打进包（白胖、而且用户看不懂，
#   本机曾经因为自动收集把 verify-plugin.js 打进去过）。
EXCLUDE_SCRIPTS = {
    "verify-plugin.js", "diag-load.js", "find-working-manifest.js",
    "install-in-zotero.js", "probe-variants.js", "probe-install-api.js",
    "diag-settings.js",
}
# 优先顺序：先 manifest，再代码，再资源
ORDER = ["manifest.json", "bootstrap.js", "prefs.js", "settings.xhtml", "settings.js"]


def collect_entries() -> list[str]:
    names = []
    for n in os.listdir(PLUGIN):
        if n in EXCLUDE_DIRS:
            continue                      # 源码目录（src/）不进包
        p = os.path.join(PLUGIN, n)
        if not os.path.isfile(p):
            continue                      # 其他目录（variants/ 等）也不进包
        ext = os.path.splitext(n)[1].lower()
        if ext in EXCLUDE_EXT or n in EXCLUDE_NAME or n in EXCLUDE_SCRIPTS:
            continue
        if n.endswith(".bak-iife"):
            continue
        names.append(n)
    ordered = [n for n in ORDER if n in names]
    # 其余（图标、将来新增的 .js/.xhtml 等）按名字排序跟在后面，保证可复现
    ordered += sorted(n for n in names if n not in ordered)
    return ordered


def assert_bootstrap_synced() -> None:
    """打包前必须确认 bootstrap.js 就是 src/*.js 拼出来的。

    接在这里的原因：这个项目里"改了源码但装进去的还是旧的"是最难查的一类
    事故（插件表现像没改，而 Zotero 的报错又没有任何线索）。靠人记着跑
    build_bootstrap 是不可靠的，所以让**打包这一步**硬性拒绝。
    """
    import build_bootstrap as BB
    if not os.path.exists(BB.OUT):
        raise BB.SyncError("bootstrap.js 不存在 —— 先跑 "
                           "`python tools/build_bootstrap.py`")
    with open(BB.OUT, "rb") as fh:
        raw = fh.read()
    if raw[:3] == b"\xef\xbb\xbf":
        raise BB.SyncError("bootstrap.js 带 UTF-8 BOM（Zotero 那边的报错会"
                           "完全没有线索）—— 重新生成")
    want = BB.compose()
    if raw.decode("utf-8") != want:
        raise BB.SyncError("bootstrap.js 与 src/*.js **不同步** —— 先跑 "
                           "`python tools/build_bootstrap.py` 重新生成"
                           "（生成物是 xpi 里真正装进去的那份）")



ENTRIES = collect_entries()


def pack(out_path: str) -> str:
    assert_bootstrap_synced()
    manifest = json.load(open(os.path.join(PLUGIN, "manifest.json"), encoding="utf-8"))
    version = manifest.get("version", "0.0.0")
    if out_path is None:
        out_path = os.path.join(PLUGIN, f"zotero-kb-{version}.xpi")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ENTRIES:
            src = os.path.join(PLUGIN, name)
            if not os.path.exists(src):
                continue
            data = open(src, "rb").read()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # 对齐可用插件：Unix 来源 + UTF-8 文件名标志 + 0644 权限
            info.create_system = 3
            info.flag_bits |= 0x800
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return out_path


import build_bootstrap as BB


def main() -> int:
    try:
        assert_bootstrap_synced()
    except BB.SyncError as exc:
        print(f"  [XX] {exc}")
        return 1
    manifest = json.load(open(os.path.join(PLUGIN, "manifest.json"), encoding="utf-8"))
    version = manifest.get("version", "0.0.0")
    # 先清理旧包，避免目录里堆一串版本让用户装错
    for old in os.listdir(PLUGIN):
        if old.startswith("zotero-kb-") and old.endswith(".xpi"):
            os.remove(os.path.join(PLUGIN, old))
            print(f"  清理旧包 {old}")
    out = pack(None)
    print(f"  [OK] {out}（{os.path.getsize(out)} 字节，v{version}）")

    print("\n  包内条目与属性：")
    with zipfile.ZipFile(out) as z:
        z.testzip()
        for info in z.infolist():
            print(f"    {info.filename:16} {info.file_size:>6}B  "
                  f"comp={info.compress_type} sys={info.create_system} "
                  f"flag={info.flag_bits:#06x} attr={info.external_attr:#010x}")
        names = z.namelist()
    for must in ("manifest.json", "bootstrap.js"):
        if must not in names:
            print(f"  [XX] 缺 {must}")
            return 1
    print("\n  与可用插件（pdf2zh）的属性一致：sys=3、flag 含 0x800、attr=0644")
    print(f"\n安装：Zotero → 工具 → 插件 → 齿轮 → Install Plugin From File… → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
