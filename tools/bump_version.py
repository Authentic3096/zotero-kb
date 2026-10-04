"""提升插件版本号并重新打包（一条命令搞定，避免手改 manifest 漏掉别处）。

    python tools/bump_version.py 0.9.6

⚠ 写盘用 `newline="\\n"`：Windows 上文本模式会把 `\\n` 翻成 `\\r\\n`，
  而仓库的 `.gitattributes` 约定是 LF —— 本地文件和仓库副本不一致时，
  `git status` 可能一直是干净的（`text=auto` 会把它规范化掉），
  但别的工具按字节比对时就会觉得"文件被改过"。顺手断言没写 BOM：
  manifest 带 BOM 会让 Zotero 报出没有任何线索的错（见 check_plugin.py）。
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
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    raw = open(MANIFEST, "rb").read()
    assert raw[:3] != b"\xef\xbb\xbf", "manifest 写出了 BOM！"
    assert b"\r" not in raw, "manifest 写出了 CR！"
    print(f"  manifest 版本：{old} → {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
