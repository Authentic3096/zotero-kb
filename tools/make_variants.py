"""生成 manifest 变体 xpi，用于二分定位"解析失败(error=-3)"的真因。

    python tools/make_variants.py

背景：`AddonManager.getInstallForFile()` 对本插件返回
`install.error = -3 (ERROR_CORRUPT_FILE)`、`install.addon = undefined`，
即 **manifest 解析阶段就失败**了。而本机三个可用插件的包结构（manifest.json +
bootstrap.js + prefs.js，无 chrome.manifest）和我们完全一样，所以差异一定在
manifest 内容里。

与其继续猜，这里生成一组**每次只差一个字段**的 xpi，逐个丢进 Zotero 测，
看哪个能装上 —— 一次就能定位到底是哪个字段的问题。

用法：跑本脚本，然后按 Zotero → 工具 → 开发者 → 运行 JavaScript，
      运行 probe-variants.js（会自动逐个试装并打印结果）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
OUT = os.path.join(PLUGIN, "variants")

BASE = {
    "manifest_version": 2,
    "name": "Zotero 文献知识库",
    "version": "0.9.0",
    "description": "把新加入的文献自动送进本地文献知识库（切片+索引），并用本地模型推荐分类与标签，一键应用。",
    "author": "zotero-kb",
    "icons": {"48": "icon.svg", "96": "icon.svg"},
    "applications": {"zotero": {"id": "zotero-kb@authentic3096.github.io"}},
}


def variant(name: str, mutate) -> dict:
    m = json.loads(json.dumps(BASE))       # deep copy
    mutate(m)
    return {"name": name, "manifest": m}


VARIANTS = [
    # 当前版本（对照：预期失败）
    variant("v0-current", lambda m: None),
    # 加上可用插件都有的 homepage_url
    variant("v1-homepage", lambda m: m.update(
        {"homepage_url": "https://github.com/zotero-kb/zotero-kb"})),
    # 权威来源：社区标准写法（与官方示例 make-it-red 一致）
    variant("v2-official", lambda m: m.update({
        "homepage_url": "https://github.com/zotero-kb/zotero-kb",
    }) or m["applications"]["zotero"].update({
        "update_url": "https://example.com/updates.json",
        "strict_min_version": "6.999",
        "strict_max_version": "10.*",
    })),
    # 官方示例的原文（去掉 update_url）
    variant("v3-official-noupdate", lambda m: m.update({
        "homepage_url": "https://github.com/zotero/make-it-red",
    }) or m["applications"]["zotero"].update({
        "strict_min_version": "6.999",
        "strict_max_version": "10.*",
    })),
    # 用 browser_specific_settings（MV3 的写法）
    variant("v4-bss", lambda m: (
        m.pop("applications", None),
        m.update({"browser_specific_settings": {
            "zotero": {"id": "zotero-kb@authentic3096.github.io",
                       "strict_min_version": "6.999",
                       "strict_max_version": "10.*"}}})
    ) and None),
    # 纯英文 name/description（排除中文引起的解析问题）
    variant("v5-ascii", lambda m: m.update({
        "name": "Zotero KB",
        "description": "Local literature knowledge base integration.",
        "author": "zotero-kb",
        "homepage_url": "https://github.com/zotero-kb/zotero-kb",
    }) or m["applications"]["zotero"].update({
        "strict_min_version": "6.999", "strict_max_version": "10.*"})),
    # 不带 icons（排除图标文件问题）
    variant("v6-noicons", lambda m: m.pop("icons", None)),
    # 用 PNG 图标而不是 SVG
    variant("v7-pngicon", lambda m: m.update(
        {"icons": {"48": "icon.png", "96": "icon.png"},
         "homepage_url": "https://github.com/zotero-kb/zotero-kb"})),
]


def build(spec: dict) -> str:
    os.makedirs(OUT, exist_ok=True)
    name = spec["name"]
    path = os.path.join(OUT, f"{name}.xpi")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # manifest 第一个写（与可用插件一致）
        info = zipfile.ZipInfo("manifest.json", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        z.writestr(info, json.dumps(spec["manifest"], ensure_ascii=False, indent=2))
        # bootstrap.js 用真的（如果它能加载就算成功）
        for f in ("bootstrap.js", "prefs.js", "icon.svg", "icon.png"):
            src = os.path.join(PLUGIN, f)
            if os.path.exists(src):
                z.write(src, f)
    return path


def main() -> int:
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT, exist_ok=True)
    print("=" * 70)
    print("生成 manifest 变体（用于定位解析失败的真因）")
    print("=" * 70)
    listing = []
    for spec in VARIANTS:
        p = build(spec)
        size = os.path.getsize(p)
        print(f"  {spec['name']:24} {size:>6}B  {p}")
        listing.append({"name": spec["name"], "path": p,
                        "id": (spec["manifest"].get("applications", {})
                               .get("zotero", {}).get("id")
                               or spec["manifest"].get("browser_specific_settings", {})
                               .get("zotero", {}).get("id"))})
    with open(os.path.join(OUT, "variants.json"), "w", encoding="utf-8") as fh:
        json.dump(listing, fh, ensure_ascii=False, indent=1)
    print(f"\n共 {len(listing)} 个变体 → {OUT}")
    print("\n下一步：在 Zotero 里运行 zotero-plugin\\probe-variants.js")
    print("（它会逐个试装并打印每个变体的 install.error，从而定位真凶字段）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
