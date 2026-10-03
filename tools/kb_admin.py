"""经验层管理：查看、清理测试数据、导出。经验是攒出来的，要能看清、能备份。

    python tools/kb_admin.py list                 # 列出全部经验与权重
    python tools/kb_admin.py list --pending       # 只看待确认清单里还没处理的
    python tools/kb_admin.py clean --test-data    # 清掉自检/验收留下的记录
    python tools/kb_admin.py clean --id 3         # 删掉某一条经验（会同时修正权重）
    python tools/kb_admin.py export backup.json   # 导出经验与权重（换机器时带走）
    python tools/kb_admin.py import backup.json   # 导入（按内容去重）

为什么要有 `clean`：验收测试会往经验表写记录，写进去就会影响检索排序。
测试完必须能干净地把它们拿掉，并且**同时回滚 item_weight** ——
只删 experience 不回滚权重，会留下"没有经验却权重很高"的脏状态。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

TEST_MARKERS = ("%自检%", "%验收%", "%测试%")


def cmd_list(args: argparse.Namespace) -> int:
    conn = S.connect(S.INDEX_DB)
    if args.pending:
        path = os.path.join(S.INBOX_DIR, "pending.jsonl")
        if not os.path.exists(path):
            print("待确认清单是空的（先跑 offline/learn.py scan）")
            return 0
        rows = []
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        pending = [r for r in rows if r.get("status") == "pending"]
        print(f"待确认 {len(pending)} 条（共 {len(rows)} 行）\n")
        for row in pending:
            print(f"[{row['id']}] [{row['outcome']}] 置信 {row.get('confidence', 0):.2f}")
            print(f"   问题：{row['asked'][:90]}")
            print(f"   方法：{row.get('method', '')[:90]}")
            print(f"   原因：{row.get('reason', '')[:120]}")
            print()
        conn.close()
        return 0

    exp = conn.execute(
        "SELECT id, created_at, asked, method, outcome, reason, item_keys, tags, source "
        "FROM experience ORDER BY id"
    ).fetchall()
    print(f"=== 经验 {len(exp)} 条 ===")
    for r in exp:
        keys = json.loads(r["item_keys"] or "[]")
        print(f"#{r['id']} [{r['outcome']}] {r['created_at'][:10]} "
              f"({r['source']}) {r['asked'][:60]}")
        if r["method"]:
            print(f"     方法：{r['method'][:90]}")
        if r["reason"]:
            print(f"     原因：{r['reason'][:100]}")
        if keys:
            print(f"     文献：{', '.join(keys)}")

    weights = conn.execute(
        "SELECT * FROM item_weight ORDER BY (3*pinned + manual + 2*effective "
        "+ ineffective) DESC"
    ).fetchall()
    print(f"\n=== 权重 {len(weights)} 行 ===")
    for r in weights:
        title = conn.execute("SELECT title, year FROM items WHERE key = ?",
                             (r["item_key"],)).fetchone()
        mult = S.weight_multiplier(
            r, S.recency_base(title["year"] if title else None)
        )
        print(f"{r['item_key']}  乘数 {mult:.2f}  尝试 {r['attempts']} "
              f"有效 {r['effective']} 无效 {r['ineffective']} 部分 {r['partial']}"
              f"{' ★重点' if r['pinned'] else ''}  {(title['title'] if title else '')[:36]}")
        if r["note"]:
            print(f"     备注：{r['note']}")
    conn.close()
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    conn = S.connect(S.INDEX_DB)
    if args.id:
        row = conn.execute("SELECT * FROM experience WHERE id = ?", (args.id,)).fetchone()
        if not row:
            print(f"没有 id={args.id} 的经验")
            return 1
        print(f"删除 #{args.id} [{row['outcome']}] {row['asked'][:60]}")
        # 回滚这条经验对权重的贡献
        delta = {"effective": (1, 0, 0), "ineffective": (0, 1, 0),
                 "partial": (0, 0, 1)}.get(row["outcome"], (0, 0, 0))
        for key in json.loads(row["item_keys"] or "[]"):
            conn.execute(
                """
                UPDATE item_weight SET
                    attempts = MAX(0, attempts - 1),
                    effective = MAX(0, effective - ?),
                    ineffective = MAX(0, ineffective - ?),
                    partial = MAX(0, partial - ?),
                    updated_at = datetime('now')
                WHERE item_key = ?
                """,
                (delta[0], delta[1], delta[2], key),
            )
        conn.execute("DELETE FROM experience WHERE id = ?", (args.id,))
        conn.commit()
        print("已删除，并回滚了对应权重")
        conn.close()
        return 0

    if not (args.test_data or args.orphan_weights):
        print("要做什么？--test-data 清测试经验，--orphan-weights 清无经验支撑的权重残留，"
              "或 --id N 删某一条")
        conn.close()
        return 1

    where = " OR ".join(["asked LIKE ?"] * len(TEST_MARKERS)
                        + ["tags LIKE ?"] * len(TEST_MARKERS))
    params = list(TEST_MARKERS) * 2
    rows = conn.execute(
        f"SELECT id, asked, outcome, item_keys FROM experience WHERE {where}", params
    ).fetchall()
    # ⚠ 注意：这里**不能提前 return**。曾经在"没有找到测试数据"时直接返回，
    # 于是后面的孤儿权重清理永远走不到 —— 而"经验已删、权重还挂着"正是
    # 最需要清理的情形（实测漏过一次）。
    if rows:
        print(f"将删除 {len(rows)} 条测试经验并回滚权重：")
        for row in rows:
            print(f"  #{row['id']} [{row['outcome']}] {row['asked'][:60]}")
            delta = {"effective": (1, 0, 0), "ineffective": (0, 1, 0),
                     "partial": (0, 0, 1)}.get(row["outcome"], (0, 0, 0))
            for key in json.loads(row["item_keys"] or "[]"):
                conn.execute(
                    """
                    UPDATE item_weight SET
                        attempts = MAX(0, attempts - 1),
                        effective = MAX(0, effective - ?),
                        ineffective = MAX(0, ineffective - ?),
                        partial = MAX(0, partial - ?),
                        updated_at = datetime('now')
                    WHERE item_key = ?
                    """,
                    (delta[0], delta[1], delta[2], key),
                )
        conn.execute(f"DELETE FROM experience WHERE {where}", params)
    else:
        print("没有找到测试经验"
              + ("（继续检查权重残留…）" if args.orphan_weights else ""))

    if args.orphan_weights:
        _clean_orphan_weights(conn)

    # 已经全零的行直接删掉，省得 list 里一堆空壳
    conn.execute("DELETE FROM item_weight WHERE attempts <= 0 AND pinned = 0 "
                 "AND effective <= 0 AND ineffective <= 0 AND partial <= 0 "
                 "AND manual = 0")
    conn.commit()
    print("已清理。剩下经验：",
          conn.execute("SELECT COUNT(*) FROM experience").fetchone()[0], "条",
          "｜权重行：",
          conn.execute("SELECT COUNT(*) FROM item_weight").fetchone()[0])
    if not args.orphan_weights:
        n_orphan = _count_orphan_weights(conn)
        if n_orphan:
            print(f"\n⚠ 还有 {n_orphan} 行权重没有经验支撑（多为测试标记残留），"
                  f"会继续影响排序。")
            print("   清理：python tools/kb_admin.py clean --orphan-weights")
    conn.close()
    return 0


def _orphan_rows(conn) -> list:
    """找出"有权重标记、却没有任何经验引用"的文献。

    判据说明：pinned / manual / note 这三个字段只有人工操作
    （kb_weight_set）会写，经验写入（kb_experience_add）不动它们。
    所以"有这些标记但没有经验引用"= 残留。
    """
    rows = conn.execute(
        """
        SELECT w.item_key, w.attempts, w.effective, w.ineffective, w.partial,
               w.pinned, w.manual, w.note
        FROM item_weight w
        WHERE (w.pinned <> 0 OR w.manual <> 0
               OR (w.note IS NOT NULL AND w.note <> ''))
        """
    ).fetchall()
    out = []
    for row in rows:
        n_exp = conn.execute(
            "SELECT COUNT(*) AS n FROM experience WHERE item_keys LIKE ?",
            (f"%{row['item_key']}%",),
        ).fetchone()["n"]
        if n_exp == 0:
            out.append(row)
    return out


def _count_orphan_weights(conn) -> int:
    return len(_orphan_rows(conn))


def _clean_orphan_weights(conn) -> int:
    """清零残留权重。这类残留是测试数据影响排序的最后一条路径。"""
    rows = _orphan_rows(conn)
    for row in rows:
        print(f"  清除无经验支撑的权重残留：{row['item_key']}"
              f"（pinned={row['pinned']} manual={row['manual']} "
              f"note={row['note']!r}）")
        conn.execute(
            "UPDATE item_weight SET pinned = 0, manual = 0, note = '', "
            "attempts = 0, effective = 0, ineffective = 0, partial = 0, "
            "updated_at = datetime('now') WHERE item_key = ?",
            (row["item_key"],),
        )
    if rows:
        print(f"  共清零 {len(rows)} 行残留权重")
    conn.commit()
    return len(rows)


def cmd_export(args: argparse.Namespace) -> int:
    conn = S.connect(S.INDEX_DB)
    data = {
        "version": 1,
        "exported_from": S.INDEX_DB,
        "experience": [dict(r) for r in conn.execute("SELECT * FROM experience")],
        "item_weight": [dict(r) for r in conn.execute("SELECT * FROM item_weight")],
    }
    with open(args.path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print(f"已导出到 {args.path}：经验 {len(data['experience'])} 条，"
          f"权重 {len(data['item_weight'])} 行")
    conn.close()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    if not os.path.exists(args.path):
        print(f"找不到 {args.path}")
        return 1
    data = json.load(open(args.path, encoding="utf-8"))
    conn = S.connect(S.INDEX_DB)
    added = 0
    for row in data.get("experience", []):
        # 按 (asked, method, outcome) 去重，避免重复导入把权重刷高
        exists = conn.execute(
            "SELECT 1 FROM experience WHERE asked = ? AND ifnull(method,'') = ? "
            "AND outcome = ?",
            (row.get("asked"), row.get("method") or "", row.get("outcome")),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO experience(created_at, asked, context, item_keys, method,
                                   outcome, reason, evidence, tags, source, session)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (row.get("created_at"), row.get("asked"), row.get("context"),
             row.get("item_keys"), row.get("method"), row.get("outcome"),
             row.get("reason"), row.get("evidence"), row.get("tags"),
             row.get("source"), row.get("session")),
        )
        added += 1
    conn.commit()
    print(f"导入完成：新增经验 {added} 条（重复的已跳过）")
    print("提示：item_weight 未自动合并，如需完整恢复权重，请手工核对。")
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="经验层管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--pending", action="store_true")

    p_clean = sub.add_parser("clean")
    p_clean.add_argument("--test-data", action="store_true",
                         help="删除自检/验收/测试经验，并回滚其权重")
    p_clean.add_argument("--orphan-weights", action="store_true",
                         help="清零「有 pinned/人工分/备注、却没有任何经验引用」的权重残留")
    p_clean.add_argument("--id", type=int, default=0, help="删除指定 id 的经验")

    p_exp = sub.add_parser("export")
    p_exp.add_argument("path")
    p_imp = sub.add_parser("import")
    p_imp.add_argument("path")

    args = parser.parse_args()
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "clean":
        return cmd_clean(args)
    if args.cmd == "export":
        return cmd_export(args)
    return cmd_import(args)


if __name__ == "__main__":
    sys.exit(main())
