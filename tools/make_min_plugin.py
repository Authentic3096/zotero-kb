"""打包"最小验证插件"并（可选）解压检查真实包的 bootstrap.js。

    python tools/make_min_plugin.py

目的：当插件"active=true 但什么都不做"时，要区分是**加载阶段**失败还是
**startup 内部**失败。这个最小包只写一个文件，不带任何复杂逻辑：

  · 如果装上它能写出 kb\\minimal-probe.json → 加载链路没问题，
    问题在正式包 bootstrap.js 的内容里
  · 如果它也没反应 → 问题在包结构/加载面（与代码内容无关）

同时把正式包的 bootstrap.js 解压出来，方便直接用 node --check 验证
（排除"打包时内容被改坏"这种可能）。
"""

from __future__ import annotations

import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
MIN_DIR = os.path.join(PLUGIN, "variants-min")
OUT = os.path.join(MIN_DIR, "zotero-kb-min-1.0.0.xpi")

MANIFEST = {
    "manifest_version": 2,
    "name": "Zotero KB Minimal Probe",
    "version": "1.0.0",
    "description": "Minimal probe: writes a file on startup.",
    "author": "zotero-kb",
    "applications": {
        "zotero": {
            "id": "zotero-kb-min@local.dev",
            "update_url": "https://example.com/updates.json",
            "strict_min_version": "6.999",
            "strict_max_version": "10.*",
        }
    },
}


def main() -> int:
    print("=" * 66)
    print("打包最小验证插件")
    print("=" * 66)
    os.makedirs(MIN_DIR, exist_ok=True)

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("manifest.json", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        z.writestr(info, json.dumps(MANIFEST, ensure_ascii=False, indent=2))
        for name in ("bootstrap.js",):
            src = os.path.join(MIN_DIR, name)
            if os.path.exists(src):
                z.write(src, name)
    print(f"  [OK] {OUT}（{os.path.getsize(OUT)} 字节）")
    with zipfile.ZipFile(OUT) as z:
        print("       内容：" + ", ".join(z.namelist()))

    # 顺带验证正式包的 bootstrap.js（排除打包把内容改坏）
    real = os.path.join(PLUGIN, "zotero-kb-0.9.5.xpi")
    if os.path.exists(real):
        print(f"\n正式包 {os.path.basename(real)}：")
        with zipfile.ZipFile(real) as z:
            boot = z.read("bootstrap.js").decode("utf-8", errors="replace")
            man = json.loads(z.read("manifest.json").decode("utf-8"))
        print(f"  manifest version = {man['version']}")
        print(f"  bootstrap.js {len(boot)} 字符")
        print(f"  顶层是 var ZoteroKB = {{...}} 形式："
              f"{'是' if boot.lstrip().startswith('/*') and 'var ZoteroKB = {' in boot else '否'}")
        # 检查是否有顶层 function startup（Zotero 最认这种）
        has_top_func = any(
            ln.startswith("function startup") for ln in boot.splitlines())
        print(f"  有顶层 function startup: {'是' if has_top_func else '否 ← 靠 evalInSandbox 兜底'}")
        # 解出来给 node 检查
        out_js = os.path.join(MIN_DIR, "_real_bootstrap.js")
        open(out_js, "w", encoding="utf-8").write(boot)
        print(f"  已导出：{out_js}")
        print(f"  检查：node --check \"{out_js}\"")

    print("\n装最小包：")
    print("  Zotero → 工具 → 插件 → 齿轮 → Install Plugin From File…")
    print(f"  选 {OUT}")
    print("然后**完全重启 Zotero**，再看 kb\\minimal-probe.json 有没有出现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
