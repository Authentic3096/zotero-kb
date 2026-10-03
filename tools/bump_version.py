"""提升插件版本号并重新打包（一条命令搞定，避免手改 manifest 漏掉别处）。

    python tools/bump_version.py 0.9.6
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, "zotero-plugin", "manifest.json")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：bump_version.py <新版本号>")
        return 1
    new = sys.argv[1].strip()
    m = json.load(open(MANIFEST, encoding="utf-8"))
    old = m.get("version")
    m["version"] = new
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"  manifest 版本：{old} → {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
