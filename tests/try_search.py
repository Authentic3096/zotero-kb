# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""快速自检：查询改写 + 检索，直接在命令行看结果。

    python tests/try_search.py "<你库里确实有的主题>"
    python tests/try_search.py "卷积神经网络" --limit 5
    python tests/try_search.py "<另一个主题>" --no-vector
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

from query import build_match, keywords  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--show-rewrite", action="store_true")
    args = parser.parse_args()

    print(f"查询：{args.query!r}")
    print(f"FTS5 改写：{build_match(args.query)}")
    print(f"关键词：{keywords(args.query)}")
    print("-" * 70)

    try:
        from searcher import Searcher
    except Exception as exc:  # noqa: BLE001
        print(f"searcher 不可用：{type(exc).__name__}: {exc}")
        print("（可能缺 numpy —— 先跑单测确认纯逻辑，装好依赖再试检索）")
        return 1

    s = Searcher()
    print(f"库规模：{s.stats()}")
    print("-" * 70)
    result = s.search(args.query, limit=args.limit)
    diag = result["diagnostics"]
    print(f"候选：关键词 {diag['fts_candidates']} 条，"
          f"向量 {diag['vector_candidates']} 条（使用={diag['vector_used']}）")
    print(f"过滤后：{diag['after_filter']} 个条目；返回 {len(result['hits'])} 条")
    print("=" * 70)
    scored = result.get("scored_chunks", {})
    for i, hit in enumerate(result["hits"], start=1):
        print(f"{i}. [{hit['key']}] {hit['title']}")
        print(f"   {hit['year']} | {hit['first_author']} | {'/'.join(hit['collections'])}"
              f" | 分 {hit['score']:.4f} | 权重 {hit['weight']}")
        if scored.get(hit["key"]):
            print(f"   参与打分的块：{scored[hit['key']]}")
        for sn in hit["snippets"]:
            page = f"p.{sn['page']}" if sn["page"] else "笔记/标注"
            print(f"   · {page}: {sn['text'][:150]}")
        print()
    exp = s.related_experience(args.query, limit=3)
    if exp:
        print("-" * 70)
        print(f"相关经验 {len(exp)} 条：")
        for e in exp:
            print(f"  [{e['outcome']}] {e['method']} — {(e['reason'] or '')[:80]}")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
