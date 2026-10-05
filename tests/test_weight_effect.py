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
    #
    # ⚠ 判据不能只看"名次有没有前进"：名次是**库内容相关**的。
    #   本轮全库正文换成 MinerU 之后，目标那篇的基础分掉到 0.0029，乘 3.568 是
    #   0.01046，仍低于当时第三名的 0.01090 —— 名次没动，但**权重明明乘对了**
    #   （0.01046 = 0.0029 × 3.568）。所以改成两条：
    #     ① 加权后分数必须≈基础分 × 权重（权重真的作用在分数上）；
    #     ② 若"上一名的分数 / 目标基础分 < 权重"，那么名次**必须**前进
    #        （纯算术：乘完之后它一定超过上一名）。条件不成立就不要求位移。
    print("\n" + "=" * 62)
    # ⚠ 权重不是"从 1.0 起跳"：它是**发表年份基础权重 + 经验加成**（本机实测目标
    #   那篇本来是 3.07，标重点后 3.568）。所以倍数要拿**两次搜索各自报的权重**来比
    #   —— 第一版把 weight_before 硬编码成 1.0，于是"分数只涨 1.16 倍"被判成没生效，
    #   而 3.568 / 3.07 ≈ 1.16 恰恰说明乘对了。
    score_before = next((s for _i, k, s, _w in before if k == TARGET), 0.0)
    score_after = next((s for _i, k, s, _w in after if k == TARGET), 0.0)
    weight_before = next((w for _i, k, _s, w in before if k == TARGET), 1.0) or 1.0
    weight_after = next((w for _i, k, _s, w in after if k == TARGET), 1.0) or 1.0
    factor = weight_after / weight_before
    ratio = (score_after / score_before) if score_before else 0.0
    ok_score = score_before > 0 and score_after > score_before
    ok_ratio = abs(ratio - factor) <= max(0.05, factor * 0.25)
    above = [s for _i, _k, s, _w in before if s > score_before]
    must_move = bool(above) and (min(above) / score_before) < factor \
        if score_before else False
    ok_rank = (not must_move) or (rank_after < rank_before)
    ok = ok_score and ok_ratio and ok_rank
    print(f"权重：{weight_before:.3f} → {weight_after:.3f}（倍数 {factor:.2f}）")
    print(f"分数：{score_before:.5f} → {score_after:.5f}（倍数 {ratio:.2f}）")
    moved = "（算术上必须前进）" if must_move else "（算术上不必前进）"
    print(f"排名：{rank_before if rank_before < 99 else '未进前8'}"
          f" → {rank_after if rank_after < 99 else '未进前8'}" + moved)
    tail = "（分数按权重放大" + ("，且该前进时前进了）" if must_move else "）")
    print("结论：" + ("经验权重生效" + tail + " ✅" if ok else "❌ 未生效，需要检查"))
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
