"""生成 Zotero 插件的自动更新清单 `updates.json`。

    python tools/make_updates_json.py --tag v0.25.0 --repo Authentic3096/zotero-kb

## 为什么必须有这个脚本

插件 manifest 里的 `update_url` 指向**仓库 main 分支上的 updates.json**，
Zotero 靠它判断"有没有新版本"。每发一次版，这个文件必须跟着更新 ——
否则用户永远停在旧版，而且**不会有任何报错**（这是最坏的一种失败）。

## 它防的是什么

两个 URL 必须严格对得上，否则 Zotero 拿到清单却下不到包：

    update_link = https://github.com/<repo>/releases/download/<tag>/<xpi 文件名>

手写这三者（tag、附件名、清单里的路径）迟早会不一致。所以这里**全部由
manifest 里的 version 推导**，并且：

  · tag 与 manifest 版本不一致 → 直接退出码非零（宁可发不出去，也不要发一个错的）
  · 生成的 update_link 指向的附件名 = pack_plugin.py 实际产出的文件名

`--check` 只比对不写入，可以挂进 CI 当守门员。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, "zotero-plugin", "manifest.json")
OUT = os.path.join(ROOT, "updates.json")
# 与 manifest 里 applications.zotero 的版本范围保持一致，否则 Zotero 会忽略这条更新
DEFAULT_REPO = os.environ.get("GITHUB_REPOSITORY", "Authentic3096/zotero-kb")


def read_manifest() -> dict:
    # 用 utf-8-sig 读：万一 manifest 被写进了 BOM，这里也读得下去。
    # ⚠ 但**读得下去不等于没问题** —— manifest 带 BOM 可能让 Zotero 拒绝加载，
    #   所以 BOM 由 tools/check_plugin.py 专门查并报错，这里只是别让它以
    #   "Unexpected UTF-8 BOM" 这种没头没尾的报错形式冒出来。
    with open(MANIFEST, encoding="utf-8-sig") as fh:
        return json.load(fh)


def build(tag: str, repo: str) -> dict:
    m = read_manifest()
    version = m["version"]
    zot = (m.get("applications") or {}).get("zotero") or {}
    addon_id = zot.get("id")
    if not addon_id:
        raise SystemExit(f"[XX] manifest 里没有 applications.zotero.id")
    # pack_plugin.py 的产物名就是这个形状；两者必须一致
    asset = f"zotero-kb-{version}.xpi"
    return {
        "addons": {
            addon_id: {
                "updates": [
                    {
                        "version": version,
                        "update_link": f"https://github.com/{repo}"
                                       f"/releases/download/{tag}/{asset}",
                        "applications": {
                            "zotero": {
                                "strict_min_version":
                                    zot.get("strict_min_version", "6.999"),
                                "strict_max_version":
                                    zot.get("strict_max_version", "10.*"),
                            }
                        },
                    }
                ]
            }
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 updates.json")
    ap.add_argument("--tag", default="", help="发布用的 git tag，如 v0.25.0")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    ap.add_argument("--check", action="store_true",
                    help="只比对现有 updates.json 是否就是将要生成的内容")
    args = ap.parse_args()

    m = read_manifest()
    version = m["version"]
    tag = args.tag or f"v{version}"

    # ⚠ 守门员：tag 和 manifest 版本对不上是最常见的发布事故
    if tag.lstrip("v") != version:
        print(f"[XX] tag 与 manifest 版本不一致：tag={tag}  manifest={version}")
        print("     两者必须一致，否则清单里的 update_link 会指向不存在的附件。")
        return 1

    data = build(tag, args.repo)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not os.path.exists(OUT):
            print(f"[XX] {OUT} 不存在")
            return 1
        with open(OUT, encoding="utf-8") as fh:
            cur = fh.read()
        if cur.strip() != text.strip():
            print("[XX] updates.json 与当前版本不一致：")
            print("     现有：", cur.replace("\n", " ")[:160])
            print("     应为：", text.replace("\n", " ")[:160])
            return 1
        print(f"[OK] updates.json 与 v{version} 一致")
        return 0

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    link = data["addons"][(m.get("applications") or {})["zotero"]["id"]][
        "updates"][0]["update_link"]
    print(f"[OK] 写入 updates.json：v{version}")
    print(f"     update_link = {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
