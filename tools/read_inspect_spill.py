"""从 Cordis Inspect 的 spill 文件里提取指定 Service 的方法签名。

    python tools/read_inspect_spill.py <spill文件> sessions sessionController ...

为什么用脚本：Inspect 的输出很长会被截断到 spill 文件，直接用 shell 里内联
Python 处理会遇到引号转义地狱（本机踩过好几次）。写成文件最稳。
"""

from __future__ import annotations

import json
import re
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    path, keys = sys.argv[1], sys.argv[2:]
    raw = open(path, encoding="utf-8", errors="replace").read()

    # 输出可能是 JSON，也可能是带行号的文本；两种都试
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            start = raw.index("{")
            data = json.loads(raw[start:])
        except (ValueError, json.JSONDecodeError):
            data = None

    services = []
    if isinstance(data, dict):
        # 常见形状：{ data: { services: [...] } }
        for path_keys in (("data", "services"), ("services",),
                          ("data", "data", "services")):
            cur = data
            for k in path_keys:
                cur = cur.get(k) if isinstance(cur, dict) else None
            if isinstance(cur, list):
                services = cur
                break
    if not services:
        # 退回正则粗提
        services = [{"key": k, "methods": []} for k in keys]

    wanted = {k.lower(): k for k in keys}
    hit = 0
    for svc in services:
        key = str(svc.get("key", ""))
        if key.lower() not in wanted:
            continue
        hit += 1
        print("=" * 68)
        print(f"Service: {key}")
        desc = str(svc.get("description", "")).strip()
        if desc:
            print(f"  {desc[:220]}")
        print("-" * 68)
        for m in svc.get("methods", []) or []:
            sig = str(m.get("signature", "")).replace("\n", " ")
            print(f"  {re.sub(r' +', ' ', sig)[:160]}")
        print()
    if not hit:
        print("没找到这些 Service。可用的 key：")
        for svc in services:
            print("  ", svc.get("key"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
