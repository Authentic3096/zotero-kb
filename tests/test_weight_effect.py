# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""验收：经验权重是否真的改变检索排序。

这是整个知识库最关键的一条 —— 如果"标重点/记经验"不能让相关文献上移，
那经验层就只是个笔记本，谈不上"越用越准"。

做法：清掉自检留下的数据 → 跑一次基线查询 → 给排序靠后的相关文献标重点
+ 记一条"有效"经验 → 再跑同一查询 → 对比排名变化。
完成后恢复原状（不留下测试数据）。
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import kbquery  # noqa: E402
import schemas as S  # noqa: E402
from searcher import Searcher  # noqa: E402

# 检索词运行时从库里取（别写死用户的领域词，原因见 tests/kbquery.py）
QUERY = kbquery.sample_query() or "model"


def snapshot(searcher: Searcher, label: str, target: str) -> list[tuple[int, str, float, float]]:
    result = searcher.search(QUERY, limit=8)
    rows = []
    print(f"\n--- {label} ---")
    for i, hit in enumerate(result["hits"], start=1):
        rows.append((i, hit["key"], hit["score"], hit["weight"]))
        mark = "  <<< 目标" if hit["key"] == target else ""
        print(f"  {i}. [{hit['key']}] {hit['title'][:44]}")
        print(f"      分 {hit['score']:.5f}  权重 {hit['weight']}{mark}")
    return rows


def rank_of(rows, key: str) -> int:
    for i, k, _s, _w in rows:
        if k == key:
            return i
    return 99


def main() -> int:
    conn = S.connect(S.INDEX_DB)

    # 0. 清掉此前自检/试跑留下的数据，保证基线干净
    cleaned = conn.execute(
        "DELETE FROM experience WHERE source = 'dsh' AND (asked LIKE '%自检%' "
        "OR tags LIKE '%自检%')"
    ).rowcount
    conn.execute("DELETE FROM item_weight WHERE note = '自检标记'")
    conn.commit()
    print(f"清理测试数据：经验 {cleaned} 条")

    searcher = Searcher()
    baseline = searcher.search(QUERY, limit=8)
    if len(baseline["hits"]) < 4:
        print(f"[XX] 查询 {QUERY!r} 结果太少（{len(baseline['hits'])} 条），"
              "换个更常见的查询词")
        return 1

    # 目标选**排名中段**的那篇：这样权重生效才有可观察的位移空间。
    # 选最后一名的话它本来就在末尾附近，上移空间不足会误判成"没生效"。
    target_index = min(3, len(baseline["hits"]) - 1)
    TARGET = baseline["hits"][target_index]["key"]
    target_title = baseline["hits"][target_index]["title"]
    print(f"\n目标：{[TARGET]} {target_title[:48]}")
    print(f"（基线排名第 {target_index + 1}，给它标重点 + 记一条有效经验，"
          f"看是否前移）")

    before = snapshot(searcher, f"基线（{QUERY}）", TARGET)
    rank_before = rank_of(before, TARGET)

    # 1. 记一条"有效"经验 + 标重点
    conn.execute(
        """
        INSERT INTO experience(created_at, asked, context, item_keys, method,
                               outcome, reason, evidence, tags, source)
        VALUES(datetime('now'), ?, ?, ?, ?, 'effective', ?, '', ?, 'dsh')
        """,
        (f"验收测试：{QUERY} 场景下该方法是否可用", "验收",
         json.dumps([TARGET]), "按该篇的建模思路处理",
         "验收用：确认权重会让它上移", json.dumps(["验收"], ensure_ascii=False)),
    )
    conn.execute(
        """
        INSERT INTO item_weight(item_key, attempts, effective, ineffective, partial,
                                pinned, manual, note, updated_at)
        VALUES(?, 1, 1, 0, 0, 1, 0, '验收标记', datetime('now'))
        ON CONFLICT(item_key) DO UPDATE SET
            attempts = attempts + 1, effective = effective + 1,
            pinned = 1, note = '验收标记', updated_at = datetime('now')
        """,
        (TARGET,),
    )
    conn.commit()
    searcher.reload_weights()

    after = snapshot(searcher, "记经验 + 标重点之后", TARGET)
    rank_after = rank_of(after, TARGET)
    print(f"\n目标 {TARGET}：第 {rank_before} → 第 {rank_after}")

    # 2. 判定
    print("\n" + "=" * 62)
    ok = rank_after < rank_before
    weight_before = 1.0
    weight_after = next((w for _i, k, _s, w in after if k == TARGET), 1.0)
    print(f"权重：{weight_before} → {weight_after}")
    print(f"排名：{rank_before if rank_before < 99 else '未进前8'}"
          f" → {rank_after if rank_after < 99 else '未进前8'}")
    print("结论：" + ("经验权重生效，相关文献上移 ✅" if ok else "❌ 未生效，需要检查"))
    print("=" * 62)

    # 3. 恢复原状：不把验收数据留在库里
    conn.execute("DELETE FROM experience WHERE tags LIKE '%验收%'")
    conn.execute("UPDATE item_weight SET pinned = 0, attempts = attempts - 1, "
                 "effective = effective - 1, note = '' WHERE note = '验收标记'")
    conn.execute("DELETE FROM item_weight WHERE attempts <= 0 AND pinned = 0 "
                 "AND effective <= 0 AND ineffective <= 0 AND partial <= 0")
    conn.commit()
    conn.close()
    print("\n已恢复原状（验收数据已清除）")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
