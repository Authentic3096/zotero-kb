"""测一次 Zotero 写操作，把请求/响应细节全打出来。

    python tools/zotero_write_test.py

为什么单独做：批量重整时 27/28 篇都报 `401 Invalid or expired API key`，
但读操作（列表、条目）都正常 —— 说明 key 这一层有问题，而批量跑一遍代价大、
错误信息又被循环淹没了。这里只动**一篇**，把每次请求的 method/path/状态/响应头
都打出来，好定位是 key 没生效、还是缺了某个必需的请求头。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, HERE)

import zotero_sync as Z  # noqa: E402

OK, BAD = "  [OK]  ", "  [XX]  "


def main() -> int:
    api = Z.ZoteroAPI()
    print("=" * 68)
    print("Zotero 写操作单次测试")
    print("=" * 68)

    info = api.probe()
    print(f"  端点：{api.base}")
    print(f"  服务可达：{info.get('reachable')}  HTTP {info.get('status')}")
    print(f"  Zotero-Server-ID：{info.get('server_id')!r}")
    print(f"  API 版本：{info.get('api_version')}")
    print(f"  授权端点状态：{info.get('authorize_status')}")
    print(f"  已保存 key：{'有（%d 字符）' % len(api.key) if api.key else '没有'}")
    if not api.key:
        print(f"{BAD}没有 key，先跑 zotero_sync.py authorize")
        return 1

    # 1) 读操作（对照：不需要 key 也应该能过）
    print("\n[1] 只读：列分类")
    try:
        cols = api.collections()
        print(f"{OK}读到 {len(cols)} 个分类")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读失败：{exc}")
        return 1

    # 2) 挑一个有内容的分类，取第一篇来试
    target_col = next((c for c in cols
                       if c["data"]["name"] == "稀疏线性最小二乘法"), None)
    if not target_col:
        target_col = cols[0]
    col_key = target_col["key"]
    col_name = target_col["data"]["name"]
    items = api.items_in_collection(col_key)
    if not items:
        print(f"{BAD}分类「{col_name}」里没有条目，换个分类试不行；请手动指定")
        return 1
    item = items[0]
    item_key = item["key"]
    version = item.get("version")
    print(f"\n[2] 目标：分类「{col_name}」里的条目 {item_key}（version={version}）")
    print(f"      标题：{(item.get('data', {}).get('title') or '')[:60]}")
    print(f"      当前分类：{item.get('data', {}).get('collections')}")

    # 3) 拿一个别的分类 key 作为"移动目标"
    other = next((c for c in cols if c["key"] != col_key), None)
    if not other:
        print(f"{BAD}只有一个分类，无法测移动")
        return 1
    print(f"      移动目标分类：{other['data']['name']}（{other['key']}）")

    # 4) 手工发 PATCH，把细节全打出来
    print("\n[3] PATCH 请求明细")
    url = f"{api.base}/users/0/items/{item_key}"
    body = json.dumps({"collections": [col_key, other["key"]]}).encode()
    headers = {
        "Content-Type": "application/json",
        "Zotero-API-Version": "3",
        "User-Agent": "zotero-kb/1.0 (local tool)",
        "Zotero-Allowed-Request": "1",
        "Host": "127.0.0.1:23119",
        "Authorization": f"Bearer {api.key}",
        "If-Unmodified-Since-Version": str(version),
    }
    if api.server_id:
        headers["Zotero-Server-ID"] = api.server_id
    print(f"  PATCH {url}")
    for k, v in headers.items():
        shown = v if k != "Authorization" else f"Bearer {v[7:15]}…（共 {len(v) - 7} 字符）"
        print(f"    {k}: {shown}")
    req = urllib.request.Request(url, data=body, method="PATCH", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            print(f"{OK}HTTP {resp.status}")
            print(f"      响应头 Zotero-API-Version="
                  f"{resp.headers.get('Zotero-API-Version')}")
            print(f"      Last-Modified-Version="
                  f"{resp.headers.get('Last-Modified-Version')}")
            print(f"      响应体：{raw[:200]}")
            print("\n结论：**写操作可以工作**。刚才批量失败可能是 key 过期或竞态。")
            print("      现在可以跑：python tools/zotero_sync.py apply")
            return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"{BAD}HTTP {exc.code}")
        print(f"      响应体：{raw[:300]}")
        print("      响应头：")
        for k, v in (exc.headers or {}).items():
            print(f"        {k}: {v}")
        if exc.code == 401:
            print("\n诊断：401 = key 未通过校验。可能原因：")
            print("  · authorize 拿到的是**一次性** key，写成功一次后就失效")
            print("  · 或每次写操作都需要重新 authorize")
            print("  · 或需要额外的 Authorization 格式（如 'Bearer' 之外的形式）")
        elif exc.code == 428:
            print("\n诊断：428 = 缺前置条件头（If-Unmodified-Since-Version 或 Server-ID）")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
