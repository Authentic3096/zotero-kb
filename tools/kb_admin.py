"""经验层管理：查看、编辑、增添、清理测试数据、导出。经验是攒出来的，要能看清、能备份。

    python tools/kb_admin.py list                 # 列出全部经验与权重
    python tools/kb_admin.py list --pending       # 只看待确认清单里还没处理的
    python tools/kb_admin.py add --asked "…" --outcome effective --method "…" --keys A,B
    python tools/kb_admin.py edit 12 --outcome ineffective --reason "…"
    python tools/kb_admin.py clean --test-data    # 清掉自检/验收留下的记录
    python tools/kb_admin.py clean --id 3         # 删掉某一条经验（会同时修正权重）
    python tools/kb_admin.py renumber             # 把经验 id 重排成 1..N 连续
    python tools/kb_admin.py export backup.json   # 导出经验与权重（换机器时带走）
    python tools/kb_admin.py import backup.json   # 导入（按内容去重）

## 权重算术只有一份

`add` / `edit` / `clean` 都会改权重，而"加一次"与"回滚一次"必须是同一份
算术 —— 否则会出现"经验删了、权重还挂着"的脏状态（实测漏过一次：删经验不
回滚权重，排序就继续被幽灵分数影响）。这一份实现在
`offline/experience.py`，本文件只负责把参数喂进去。

⚠ `import` 是唯一的例外：它是**批量恢复**路径，重建 experience 行但**不合并
item_weight**（导出文件里那份权重是当时的快照，直接合并会重复计分）。工具
末尾会明确提示这一点。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import experience as EXP  # noqa: E402
import schemas as S  # noqa: E402

TEST_MARKERS = ("%自检%", "%验收%", "%测试%")


def _writer(conn) -> EXP.ConnWriter:
    """CLI 是单线程的，裸连接直接包一层就行（MCP 侧必须传 Searcher）。"""
    return EXP.ConnWriter(conn)


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
    w = _writer(conn)
    if args.id:
        row = conn.execute("SELECT * FROM experience WHERE id = ?", (args.id,)).fetchone()
        if not row:
            print(f"没有 id={args.id} 的经验")
            return 1
        print(f"删除 #{args.id} [{row['outcome']}] {row['asked'][:60]}")
        # 回滚权重与删除走同一份实现（experience.py）；删完 id 自动重排成 1..N
        EXP.delete_experience(w, args.id)
        print("已删除，并回滚了对应权重（id 已重排成 1..N 连续）")
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
        # ⚠ 一次批量删（`delete_experiences`），**不要**循环调 delete_experience：
        #   每删一条都会重排编号，下一条的 id 可能已经指到别的行上了。
        EXP.delete_experiences(w, [row["id"] for row in rows])
        print(f"  已删除 {len(rows)} 条，编号已重排成 1..N")
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


def cmd_renumber(args: argparse.Namespace) -> int:
    """把经验 id 重排成 1..N 连续（老库删过条目、编号有空洞时用一次）。"""
    conn = S.connect(S.INDEX_DB)
    w = _writer(conn)
    res = EXP.renumber(w)
    if res["moved"]:
        print(f"已重排：{res['before']} → {res['after']}")
    else:
        print(f"编号本来就是连续的：{res['before']}")
    print(f"现在共 {len(EXP.all_ids(w))} 条经验")
    conn.close()
    return 0


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


def cmd_add(args: argparse.Namespace) -> int:
    """手记一条经验（面板「修改/增添经验」在没有本地模型时走的就是这条路）。"""
    conn = S.connect(S.INDEX_DB)
    w = _writer(conn)
    try:
        exp_id = EXP.add_experience(
            w, asked=args.asked, outcome=args.outcome, method=args.method,
            item_keys=args.keys, context=args.context, reason=args.reason,
            evidence=args.evidence, tags=args.tags, source="user",
        )
    except ValueError as exc:
        print(f"  ✗ {exc}")
        conn.close()
        return 1
    row = EXP.get_experience(w, exp_id)
    keys = EXP.loads(row["item_keys"], [])
    print(f"  已新增 #{exp_id} [{row['outcome']}] {row['asked'][:60]}")
    if keys:
        print("  关联文献：" + ", ".join(keys) + "（权重已计入）")
    else:
        print("  ⚠ 没有关联文献：这条经验不会被检索加权，也不会出现在任何一篇的档案里")
    conn.close()
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """改一条经验：先回滚旧权重 → 改行（旧值进 history）→ 应用新权重。"""
    conn = S.connect(S.INDEX_DB)
    w = _writer(conn)
    fields = {}
    for name, value in (("asked", args.asked), ("outcome", args.outcome),
                        ("method", args.method), ("context", args.context),
                        ("reason", args.reason), ("evidence", args.evidence),
                        ("tags", args.tags), ("item_keys", args.keys)):
        if value is not None:
            fields[name] = value
    if not fields:
        print("  没有要改的字段（用 --asked/--outcome/--method/--reason/--keys… 指定）")
        conn.close()
        return 1
    try:
        info = EXP.update_experience(w, args.id, reason_suffix="CLI 编辑", **fields)
    except ValueError as exc:
        print(f"  ✗ {exc}")
        conn.close()
        return 1
    print(f"  已修改 #{args.id}：")
    for name, diff in (info.get("changed") or {}).items():
        print(f"    {name}：{str(diff['from'])[:60]!r} → {str(diff['to'])[:60]!r}")
    if not info.get("changed"):
        print("    （内容没变）")
    print("  权重已按新旧 outcome / 关联文献重算；旧值留在 history 里")
    if info.get("item_keys"):
        print("  关联文献：" + ", ".join(info["item_keys"]))
    else:
        print("  ⚠ 现在没有关联文献了：这条经验不会再参与加权")
    conn.close()
    return 0


def cmd_drop_legacy_tables(args: argparse.Namespace) -> int:
    """删掉已废弃的三张表（逐段检查层）。

    ## 为什么要单独一个命令、还要 --yes

    这三张表（`para_check` / `para_override` / `fulltext_patch`）装的是
    **用户确认过的劳动成果**（哪段查过、改了哪里），删了不可逆。
    2026-10-05 用户拍板：「删就都删了」—— 逐段检测那条链（窗格入口、
    七个端点、kbchat/paras、面板复核页）全没了，留着表也没人能产生或查看它。
    所以给这个命令，而不是在 `connect()` 里悄悄 DROP：
      · 默认**只预览**（列出表名与行数），`--yes` 才真删；
      · 真删前**自动备份 `index.db`** 到 `kb/backups/`（`--no-backup` 可跳过）；
      · 备份失败就不删（宁可留着表，也不能在没有退路的情况下销毁数据）。
    """
    legacy = ("para_check", "para_override", "fulltext_patch")
    conn = S.connect(S.INDEX_DB)
    try:
        have = {}
        for t in legacy:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            if not row:
                continue
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            have[t] = n
        if not have:
            print("这三张表都不在库里（新库不会再建）。什么都不用做。")
            return 0
        print("将要删除的表：")
        for t, n in have.items():
            print(f"  {t:18s} {n} 行")
        if not args.yes:
            print("\n这是**预览**（没删任何东西）。要真删：")
            print("  python tools/kb_admin.py drop-legacy-tables --yes")
            return 0

        if not args.no_backup:
            backup = _backup_index_db()
            if not backup:
                print("备份失败 —— 不删。检查 kb/backups 是否可写，"
                      "或确认不要备份时加 --no-backup。")
                return 2
            print(f"已备份：{backup}")

        for t in have:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        print("已删除：" + "、".join(have))
        print("（经验层、权重、切片、向量都没动）")
        return 0
    finally:
        conn.close()


def _backup_index_db() -> str:
    """把 index.db 备份到 kb/backups/，返回路径；失败返回空串。"""
    import sqlite3
    from datetime import datetime as _dt

    src = S.INDEX_DB
    if not os.path.exists(src):
        return ""
    out_dir = os.path.join(os.path.dirname(src), "backups")
    try:
        os.makedirs(out_dir, exist_ok=True)
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(out_dir, f"index-before-drop-{stamp}.db")
        # 用 sqlite 自己的 backup API：WAL 模式下直接拷文件可能缺数据
        with sqlite3.connect(src) as src_conn:
            with sqlite3.connect(dst) as dst_conn:
                src_conn.backup(dst_conn)
        return dst
    except Exception as exc:      # noqa: BLE001
        print(f"备份出错：{type(exc).__name__}: {exc}")
        return ""


def _mineru_keys(only_missing: bool, tier: str) -> list[str]:
    """挑出要解析的 key：`only_missing` 时只挑"还没有该档产物"的。"""
    import sqlite3
    import schemas as S
    import mineru as M
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT key, fulltext_src FROM items ORDER BY key").fetchall()
    conn.close()
    out = []
    for r in rows:
        if not only_missing:
            out.append(r["key"])
            continue
        st = M.status_for(r["key"])
        # ⚠ 三条取或，缺一条就会出现"产物在、库里还是老正文"的不一致：
        #   · 产物缺失 / 档位不同 → 要解析；
        #   · **库里来源还不是 mineru-<档位>** → 要重建。本机 ZNAKFIM2 就是这么漏的：
        #     上一轮被杀掉的那次运行写了产物、重建那一步没跑到，这次"补缺失"
        #     看它有产物就跳过 —— 结果库里留着 Zotero 缓存的老正文。
        src = str(r["fulltext_src"] or "")
        if (not st["parsed"] or st["tier"] != tier
                or not src.startswith(f"mineru-{tier}")):
            out.append(r["key"])
    return out


def cmd_mineru(args: argparse.Namespace) -> int:
    """MinerU 的四个动作（面板「PDF 解析」页就是调它）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "offline"))
    import mineru as M

    if args.sub == "status":
        return _mineru_status(args)
    if args.sub == "clear":
        n = M.clear_artifacts("" if args.all else args.key)
        print(f"删掉 {n} 篇的解析产物"
              + ("" if args.all else f"（{args.key}）"))
        return 0
    if args.sub == "parse":
        if not args.key:
            print("[XX] parse 需要 --key")
            return 2
        res = M.parse_item(args.key, _pdf_of(args.key), tier=args.tier,
                           force=args.force,
                           log=lambda m: print(m, flush=True))
        print(f"{args.key}：{'成功' if res['ok'] else '失败'}　"
              f"{res.get('n_pages', 0)} 页 / {res.get('n_images', 0)} 图　"
              f"{res.get('seconds', 0)}s　{res.get('why', '')}")
        if res["ok"] and getattr(args, "rebuild", False):
            print("—— 顺手重建这一篇（切片与向量）——")
            return _convert_keys([args.key], args.tier, force=False)
        return 0 if res["ok"] else 1
    # missing / reparse：**批量解析**（一次加载模型）再重建这些篇
    #
    # ⚠ 为什么先批量解析、而不是让 convert 在循环里逐篇调 MinerU：
    #   每次 `mineru-kit parse` 都要重新加载模型（本机实测约 2 分钟），
    #   逐篇跑 93 篇 ≈ 3 小时；目录批量 2 篇只要 14.5 秒。面板「全库重解析」
    #   走的就是这条，否则用户会以为程序卡死了。
    #   批量解析会把产物与指纹写好；随后 convert 命中指纹、只做切片与向量。
    keys = _mineru_keys(args.sub == "missing", args.tier)
    if args.limit:
        keys = keys[: args.limit]
    if not keys:
        print("没有需要解析的条目（都已经是这个档位了）。")
        return 0
    pairs = []
    for k in keys:
        pdf = _pdf_of(k)
        if pdf:
            pairs.append((k, pdf))
        else:
            print(f"  [!!] {k} 没有可用的 PDF 附件，跳过")
    print(f"{'补缺失' if args.sub == 'missing' else '全库重解析'}："
          f"{len(pairs)} 篇，档位 {args.tier}")
    print("（PDF 没变、档位没变的那批会按指纹跳过；批量解析模型只加载一次）")
    res = M.parse_many(pairs, tier=args.tier, force=bool(args.force),
                       stream=True,   # 批量要几十分钟，日志必须实时（面板也看它）
                       log=lambda m: print(m, flush=True))
    print(f"批量解析结果：成功 {res['n_ok']}　命中指纹跳过 {res['n_cached']}　"
          f"失败 {res['n_failed']}　共 {res['seconds']} 秒")
    for k, why in res["failed"][:20]:
        print(f"  ✗ {k}：{why}")
    done = [k for k, _p in pairs
            if res["results"].get(k, {}).get("ok")]
    if not done:
        print("没有解析成功的条目，构建这一步跳过。")
        return 1 if res["n_failed"] else 0
    print(f"—— 重建这 {len(done)} 篇的切片与向量 ——")
    return _convert_keys(done, args.tier, force=False)


def _pdf_of(key: str) -> str:
    """从 Zotero 库里取这一篇的 PDF 路径。"""
    import zreader
    r = zreader.ZoteroReader()
    try:
        for it in r.load_items(r.top_level_items()):
            if it.key == key:
                return next((p.path for p in it.pdfs if os.path.isfile(p.path)), "")
        return ""
    finally:
        r.close()


def _convert_keys(keys: list[str], tier: str, force: bool = False) -> int:
    """包一层 convert.py：只重建这些 key，且用指定档位。

    ⚠ 三件事都与"别再写第二份实现"有关：
      · 直接调 `convert.build(ns)`（与 `repair_chunks.py` 同一个用法），
        不拼命令行 —— 免得两个入口的默认值漂移；
      · 造 Namespace 时**字段照 repair_chunks 那份抄全**：convert.build 里
        `args.figures` / `args.no_figures` / `args.embed_model` 都要读，
        少一个就 AttributeError（第一版就漏了 embed_model）；
      · `cache_check="off"`：走 MinerU 时"Zotero 缓存 vs PyMuPDF"那套用不上，
        开着只是白跑两条路。
    """
    import argparse as ap
    import convert as CV
    ns = ap.Namespace(
        full=False, no_vectors=False, limit=0,
        embed_model=CV.DEFAULT_EMBED_MODEL,
        item=list(keys), figures=False, no_figures=False,
        parser=tier, force_parse=force, parse_timeout=0.0,
        cache_check="off",
    )
    rc, _manifest = CV.build(ns)
    return rc


def _mineru_status(args: argparse.Namespace) -> int:
    import sqlite3
    import schemas as S
    import mineru as M
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT key, title, fulltext_src FROM items "
                        "ORDER BY title").fetchall()
    conn.close()
    n_mineru = n_parsed = n_stale = 0
    total = 0
    for r in rows:
        st = M.status_for(r["key"])
        if str(r["fulltext_src"] or "").startswith("mineru-"):
            n_mineru += 1
        if st["parsed"]:
            n_parsed += 1
        if st["stale"]:
            n_stale += 1
        for dp, _dn, fn in os.walk(st["dir"]):
            for n in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, n))
                except OSError:
                    pass
    info = M.probe()
    print(f"MinerU：{M.summary_line(info)}")
    print(f"共 {len(rows)} 篇：正文来源是 MinerU 的 {n_mineru} 篇，"
          f"有解析产物的 {n_parsed} 篇，指纹过期 {n_stale} 篇；"
          f"产物占用 {round(total / 1048576, 1)} MB")
    if args.verbose:
        for r in rows:
            st = M.status_for(r["key"])
            mark = "✓" if st["parsed"] else "·"
            print(f"  {mark} {r['key']}  {str(r['title'])[:52]:52s} "
                  f"{r['fulltext_src'] or '(无全文)':16s} "
                  f"{st['tier'] or '-':8s} {st['pages']}页 {st['seconds']}s"
                  + ("  ⚠指纹过期" if st["stale"] else "")
                  + (f"  错误：{st['last_error'][:40]}" if st["last_error"] else ""))
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

    sub.add_parser("renumber", help="把经验 id 重排成 1..N 连续")

    p_exp = sub.add_parser("export")
    p_exp.add_argument("path")
    p_imp = sub.add_parser("import")
    p_imp.add_argument("path")

    p_add = sub.add_parser("add", help="手记一条经验")
    p_add.add_argument("--asked", required=True, help="当时要解决的问题（必填）")
    p_add.add_argument("--outcome", required=True, choices=list(EXP.OUTCOMES))
    p_add.add_argument("--method", default="")
    p_add.add_argument("--keys", default="", help="关联文献 key，逗号/空格分隔")
    p_add.add_argument("--context", default="")
    p_add.add_argument("--reason", default="")
    p_add.add_argument("--evidence", default="")
    p_add.add_argument("--tags", default="")

    p_edit = sub.add_parser("edit", help="改一条经验（会重算权重，旧值留档）")
    p_edit.add_argument("id", type=int)
    p_edit.add_argument("--asked")
    p_edit.add_argument("--outcome", choices=list(EXP.OUTCOMES))
    p_edit.add_argument("--method")
    p_edit.add_argument("--keys", help="新的关联文献（逗号/空格分隔）")
    p_edit.add_argument("--context")
    p_edit.add_argument("--reason")
    p_edit.add_argument("--evidence")
    p_edit.add_argument("--tags")

    p_drop = sub.add_parser(
        "drop-legacy-tables",
        help="删掉已废弃的三张表（逐段检查层）；默认只预览，--yes 才真删")
    p_drop.add_argument("--yes", action="store_true",
                        help="真的 DROP（不传就只打印将要删什么）")
    p_drop.add_argument("--no-backup", action="store_true",
                        help="跳过删除前的 index.db 备份（不推荐）")

    p_min = sub.add_parser("mineru", help="MinerU 解析：状态 / 单篇 / 补缺失 / 全库重解析")
    msub = p_min.add_subparsers(dest="sub", required=True)
    m_st = msub.add_parser("status", help="看全库解析状态")
    m_st.add_argument("-v", "--verbose", action="store_true",
                      help="逐篇列出来")
    m_parse = msub.add_parser("parse", help="解析一篇")
    m_parse.add_argument("--key", required=True)
    m_parse.add_argument("--tier", default="basic")
    m_parse.add_argument("--force", action="store_true", help="忽略指纹，强制重解析")
    m_parse.add_argument("--rebuild", action="store_true",
                         help="解析完顺手重建这一篇的切片与向量")
    m_clear = msub.add_parser("clear", help="删解析产物")
    m_clear.add_argument("--key", default="")
    m_clear.add_argument("--all", action="store_true")
    for name, helptext in (("missing", "只补还没有产物的"),
                           ("reparse", "全库换成这个档位")):
        p = msub.add_parser(name, help=helptext)
        p.add_argument("--tier", default="basic")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--force", action="store_true",
                       help="忽略指纹，全部重解析（几十分钟）")

    args = parser.parse_args()
    if args.cmd == "mineru":
        return cmd_mineru(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "clean":
        return cmd_clean(args)
    if args.cmd == "renumber":
        return cmd_renumber(args)
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "edit":
        return cmd_edit(args)
    if args.cmd == "drop-legacy-tables":
        return cmd_drop_legacy_tables(args)
    if args.cmd == "export":
        return cmd_export(args)
    return cmd_import(args)


if __name__ == "__main__":
    sys.exit(main())
