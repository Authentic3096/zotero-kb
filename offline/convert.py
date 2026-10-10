"""命令行入口：把 Zotero 库构建成知识库。

    python offline/convert.py                 # 增量（只处理变化过的条目）
    python offline/convert.py --full          # 全量重建
    python offline/convert.py --no-vectors    # 跳过向量，只要关键词检索
    python offline/convert.py --limit 5       # 先试跑 5 条

它会做：
    1. 建/更新 kb/index.db 的表结构（幂等）
    2. 每条文献：写 kb/papers/<key>.md 与 kb/fulltext/<key>.md
    3. 切片 → 写 chunks、chunks_fts、embeddings
    4. 写 kb/MANIFEST.json 供在线侧与人工核对

经验层（experience / item_weight）只读不动 —— 重建知识库不丢经验。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402
import converter as C  # noqa: E402
import extrafill as XF  # noqa: E402
# MinerU 是**可选组件**：`import mineru` 只是导入这个探测/解析包装模块
# （纯标准库），没装 MinerU 也能 import —— 真正的可用性由 probe() 判。
import keys as K  # noqa: E402 —— 合并映射与 resolve_key 的唯一实现（见该模块头部）
import mineru as M  # noqa: E402
import trash as TR  # noqa: E402 —— 归档/还原/删除三动作的唯一实现（见该模块头部）
from zreader import ZoteroReader  # noqa: E402

DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"


def log(msg: str) -> None:
    print(msg, flush=True)


def build(args: argparse.Namespace) -> tuple[int, dict]:
    """跑一次构建。返回 `(退出码, MANIFEST 字典)`。

    为什么返回 manifest 而不只是退出码：`repair_chunks.repair()`（逐篇重建）
    要把"这一批的统计"汇报给面板，而 manifest 里本来就有这些数
    （正文来源、字符偏移还原了几篇、警告）。旧版只返回 int，
    调用方只能再去读 MANIFEST 文件 —— 那是同一份数据读两遍，还多一次 IO。
    """
    t_start = time.time()
    log("=" * 72)
    log("Zotero → 知识库 构建开始")
    log("=" * 72)

    conn = S.init_db()
    reader = ZoteroReader()

    # ---- 结构化字段（extra）的表：**循环外建一次**
    #
    # `item_extra` 由 `offline/extrafill.py` 自建自用（和 check_chunks 的
    # `item_health` 同一个模式）。这里只负责"保证它在"，逐条只写行 ——
    # 101 条每条重跑一遍 DDL 是白费（所以 extrafill.sync_item 默认不建表）。
    #
    # 建表失败不让整个构建失败：结构化字段是**附加**信息，
    # 拿不到它照样能把文献收进知识库。失败只报一次，而不是每条报一遍。
    extra_on = True
    try:
        XF.ensure_schema(conn)
    except Exception as exc:  # noqa: BLE001
        extra_on = False
        log(f"  [!!] 结构化字段（extra）建表失败，本次构建跳过它："
            f"{type(exc).__name__}: {exc}")

    try:
        all_ids = reader.top_level_items()
        log(f"条目总数（已排除附件/笔记/标注/回收站）：{len(all_ids)}")

        # ---- 老库一次性回填结构化字段（见 _backfill_extra_once 的说明）
        _backfill_extra_once(conn, reader, all_ids, extra_on)

        # ---- 删除对账：Zotero 侧变了、知识库这边还没跟上的条目。
        #
        # 两段式（用户 2026-10-09 定稿，方案附录 H.1）：
        #   · 进了回收站        → **归档**到 kb/trash/<key>/（不是直接删）
        #   · 回收站里也彻底删  → 清掉归档、墓碑改 kind='deleted'
        #   · 从回收站还原回来  → 按 rows.json 搬回来、复原数据库行
        #
        # 为什么放在增量判断**之前**：这三种条目都不在 all_ids 里（回收站条目被
        # zreader 排除在外），增量判断压根看不到它们，于是永远不处理。用户删了一篇
        # 文献、`kb_search` 却还搜得到 —— 这违背他对"删除"的预期。
        # **全量模式也要对账**（它同样只遍历 all_ids）。
        #
        # ⚠ 只在"确实读到了库"时才处理：万一 reader 因为权限/锁返回了空列表，
        #   贸然按空集对账会把整个知识库归档掉。所以加一条保险。
        if all_ids:
            items_now = reader.load_items(all_ids)
            live_keys = {it.key for it in items_now}
            # 还原时要把「Zotero 当前 version」写进还原行，否则刚还原的条目
            # 会被增量当成变过的条目重抽一遍（见 trash.restore 的说明）。
            live_versions = {it.key: it.version for it in items_now}
            rep = _reconcile_deletions(conn, live_keys, reader.trashed_keys,
                                       live_versions)
            for label, keys in (("归档（Zotero 回收站）", rep["archived"]),
                               ("删除（回收站里已彻底删）", rep["deleted"]),
                               ("还原（从回收站恢复）", rep["restored"])):
                if keys:
                    log(f"{label} {len(keys)} 条：" + "、".join(sorted(keys)[:8])
                        + ("…" if len(keys) > 8 else ""))
            for why in rep.get("degraded") or []:
                log(f"  [!!] 还原降级为重建（id 已被别的文献复用）：{why}")
                log("       产物已搬回原位；数据库行一概不复原，交给本次增量重抽"
                    "（详见知识库 logs\\trash.log）")
        else:
            log("  [!!] 一条 Zotero 条目都没读到 —— 跳过删除对账（防误删整个知识库）")

        # ---- 合并映射：把 Zotero 的 dc:replaces 落进 item_merge（只增不改）。
        #
        # 放在这里的理由：挨着删除对账，两者都是"知识库跟上 Zotero 的生命周期变化"，
        # 而且 reader 已经开着（快照只拷一次库）。**只写映射表** —— relink 图谱数据、
        # 合并权重都还没做（见方案 G.4 第 2/4 步与 keys.py 头部"不做的事"），
        # 所以这一步只让 resolve_key() 能查到答案，不改变任何一条文献的去留。
        if all_ids:
            mg = K.sync_merges(conn, reader)
            if mg.get("error"):
                log(f"  [!!] 合并映射同步失败（不影响构建）：{mg['error']}")
            elif mg["inserted"]:
                log(f"  合并映射：新增 {mg['inserted']} 条（共 {mg['total']} 条，"
                    f"待处理 {K.pending_count(conn)} 条）")

        # ---------------------------------------------------------- 增量判断
        # 比对「version + 分类标签指纹」两项：
        #   · version     —— 标题/作者等元数据变了会动它
        #   · collections_sig —— 分类归属或标签变了才会动它
        #     （Zotero 改分类/标签**不更新 items.version**，只比 version 会漏掉）
        # 解析器（定义在这里：下面增量判断与逐条处理都要用）
        want_parser = str(getattr(args, "parser", "") or "zotero").strip().lower()

        known: dict[str, tuple[int, str, str]] = {}
        for row in conn.execute(
                "SELECT key, version, built_at, collections_sig, fulltext_src "
                "FROM items"):
            known[row["key"]] = (row["version"], row["collections_sig"] or "",
                                 row["fulltext_src"] or "")

        ids = all_ids
        if not args.full:
            fresh: list[int] = []
            reasons = {"new": 0, "version": 0, "collections": 0, "parser": 0}
            for item in reader.load_items(all_ids):
                prev = known.get(item.key)
                if prev is None:
                    fresh.append(item.item_id)
                    reasons["new"] += 1
                    continue
                if prev[0] != item.version:
                    fresh.append(item.item_id)
                    reasons["version"] += 1
                    continue
                sig = S.collections_sig(item.collection_keys, item.tags)
                if prev[1] != sig:
                    fresh.append(item.item_id)
                    reasons["collections"] += 1
                    continue
                # ---- 解析器换了也要重跑
                #
                # ⚠ 不判这一条的话，"把解析器从 zotero 换成 basic"会**什么都不做**：
                #   条目的 version / 分类都没变，增量逻辑认为它没变化 —— 用户会以为
                #   命令没生效（本机在别的地方吃过同样的亏：改了设置却没有任何反应）。
                #   判据用 items.fulltext_src 的前缀（`mineru-basic` / `mineru-standard`…），
                #   所以**换档位**（basic → standard）也会触发重跑。
                want_src = f"mineru-{want_parser}"
                if want_parser != "zotero":
                    if not (prev[2] or "").startswith(f"mineru-{want_parser}"):
                        fresh.append(item.item_id)
                        reasons["parser"] += 1
                        continue
            skipped = len(all_ids) - len(fresh)
            log(f"增量模式：{len(fresh)} 条需要处理，{skipped} 条未变化（跳过）")
            if fresh:
                log(f"  其中 新条目 {reasons['new']}｜元数据变动 {reasons['version']}"
                    f"｜分类/标签变动 {reasons['collections']}"
                    f"｜解析器/档位待切换 {reasons['parser']}")
            ids = fresh

        # 指定条目（逐篇重建用）：**跳过增量判断**，无论变没变都重建。
        # 这是 Zotero 右键「重建这一篇」的入口 —— 元数据没动但正文提取坏了时，
        # 增量判断会认为"没变化"直接跳过，所以必须能强制。
        want_keys = {k.strip() for k in (getattr(args, "item", None) or [])
                     if k.strip()}
        if want_keys:
            picked = [it for it in reader.load_items(all_ids)
                      if it.key in want_keys]
            ids = [it.item_id for it in picked]
            missing = want_keys - {it.key for it in picked}
            log(f"--item 生效：重建 {len(ids)} 条"
                + (f"（库里没有：{', '.join(sorted(missing))}）" if missing else ""))

        if args.limit:
            ids = ids[: args.limit]
            log(f"--limit 生效：只处理前 {len(ids)} 条")

        if not ids:
            log("没有需要处理的条目。若要强制重建请加 --full")
        items = reader.load_items(ids)
        log(f"开始处理 {len(items)} 条…")

        # ---------------------------------------------------------- 逐条处理
        errors: list[dict] = []
        stats = {
            "fulltext_from_cache": 0,
            "fulltext_from_pymupdf": 0,
            "fulltext_missing": [],
            "missing_pdf_files": [],
            "no_pdf": [],
            "deshifted": [],          # 字符偏移被自动还原的（见 zreader._deshift_fix）
            "fulltext_from_mineru": 0,
            "mineru_cached": 0,       # 命中文档指纹、没重跑的
            "mineru_failed": [],      # MinerU 没出结果、回落成 Zotero/PyMuPDF 的
            "cache_bad": [],          # 缓存被判坏、换了源的
            "skipped_no_pdf": [],     # 没有 PDF 附件、跳过正文解析的
            # ---- 结构化字段（extra → item_extra 表）
            "extra_rows": 0,          # 本次写入/更新的行数
            "extra_cleared": 0,       # 本次删掉的陈旧行（extra 变空了的条目）
            "extra_items": 0,         # 本次有结构化字段的条目数
            "extra_errors": [],       # 写失败的那些（只记，不中断）
        }
        all_chunks: list[dict] = []
        built_at = C.now_iso()

        # 缓存校验模式：命令行 > 环境变量 > 默认 auto（见 zreader.cache_model_mode）
        mode = getattr(args, "cache_check", "") or ""
        if mode == "off":
            ft_kw = {"use_model": False, "arbitrate": False}
        elif mode == "deep":
            ft_kw = {"use_model": True, "arbitrate": True}
        elif mode == "auto":
            ft_kw = {"use_model": True, "arbitrate": False}
        else:
            ft_kw = {}

        for idx, item in enumerate(items, start=1):
            try:
                # ---- 没有 PDF 附件：**整个条目不要**（用户明确要求）
                #
                # 这类条目是网页剪藏、纯条目、只有笔记的条目 —— 不是要精读的
                # 文献。保留元数据行会让知识库里混进一堆没有正文的空壳
                # （搜得到标题、点进去没内容），不如干脆不收。
                #
                # ⚠ 用户对某篇记过的**经验不会删**（那是用户自己的数据）；
                #   但条目不在库里之后，那些经验也就不会再被检索到。
                if not item.pdfs:
                    stats["skipped_no_pdf"].append(
                        {"key": item.key, "title": item.title[:80],
                         "why": "没有 PDF 附件"})
                    _purge_item(conn, item)
                    conn.commit()
                    continue

                # ---- 正文来源：MinerU（可选）→ 失败按篇回落
                #
                # ⚠ 回落必须**按篇**做，不能"一次失败就整轮降级"：95 篇里混着
                #   扫描件、加密 PDF、损坏文件，任何一篇都可能失败，而其余篇
                #   仍然应该拿到 MinerU 的好结果。回落了在 stats 里留痕，
                #   报告会列出来（用户才知道哪几篇没走上新解析器）。
                mineru_used = False
                if want_parser != "zotero":
                    pages, source = M.fulltext_for(
                        item, tier=want_parser, kb_dir=S.kb_dir(),
                        force=bool(getattr(args, "force_parse", False)),
                        timeout=float(getattr(args, "parse_timeout", 0) or 0)
                        or M.PARSE_TIMEOUT, log=log)
                    if pages:
                        mineru_used = True
                        stats["fulltext_from_mineru"] += 1
                        if "+cached" in source:
                            stats["mineru_cached"] += 1
                    else:
                        stats["mineru_failed"].append(
                            {"key": item.key, "title": item.title[:80],
                             "why": "MinerU 没出结果，已回落"})
                        pages, source = reader.fulltext_for(item, **ft_kw)
                        source = source + "+fallback"
                else:
                    pages, source = reader.fulltext_for(item, **ft_kw)
                # ⚠ 必须把正文回填到 item 上。`Item.fulltext_chars` 是
                # `sum(len(p) for p in fulltext_pages)`，而 fulltext_pages 默认是空列表 ——
                # 只把正文留在局部变量 `pages` 里的话，`_upsert_item` 写进库的
                # `fulltext_chars` 恒为 0（实测 100 条全是 0），
                # 连带 kb_item / 资源 tldr 显示"全文 0 字符"，虽然正文文件其实是好的。
                item.fulltext_pages = pages
                # ⚠ 来源标记可能带后缀（`zotero-cache+fix+29` = 缓存文本经过
                #   字符偏移还原），所以只能用 startswith 判断，不能 ==。
                if source.startswith("mineru-"):
                    pass          # 上面已经计数（fulltext_from_mineru）
                elif source.startswith("zotero-cache"):
                    stats["fulltext_from_cache"] += 1
                    if reader._last_cache_verdict in ("bad", "unsure"):
                        stats["cache_bad"].append(
                            {"key": item.key, "verdict": reader._last_cache_verdict,
                             "why": reader._last_cache_note})
                elif source.startswith("pymupdf"):
                    stats["fulltext_from_pymupdf"] += 1
                elif not source.startswith("mineru-"):
                    stats["fulltext_missing"].append(
                        {"key": item.key, "title": item.title[:80]}
                    )
                if "+fix" in source:
                    stats["deshifted"].append({"key": item.key, "src": source})

                for pdf in item.pdfs:
                    if not pdf.exists:
                        stats["missing_pdf_files"].append(
                            {"key": item.key, "path": pdf.path}
                        )
                if not item.pdfs:
                    stats["no_pdf"].append({"key": item.key, "title": item.title[:80]})

                # ---- 图注与表格（从 PDF 抽，纯结构解析、不调模型）
                #
                # 为什么不在这里塞进 chunks：图注与表格是**结构化**的
                # （有编号、有行列），混进正文切片会被切碎；而且表格
                # Markdown 往往很长，会挤掉正文。所以单独存 figures 表，
                # 并在 fulltext 的**那一页末尾**渲染出来（见
                # converter.figures_for_page）—— AI 读到那页就能看见
                # "这里有个图叫什么"，需要深究时自己去看。
                figs = {"captions": [], "tables": []}
                # 开关三级：命令行 > 环境变量 > kb-location.json > 默认开
                _want = None
                if getattr(args, "figures", False):
                    _want = True
                elif getattr(args, "no_figures", False):
                    _want = False
                FG = None
                try:
                    import figures as _FG
                    if S.figures_enabled(_want):
                        FG = _FG
                    else:
                        log("    （按设置跳过图注与表格提取）")
                except Exception as exc:  # noqa: BLE001
                    log(f"    [!!] 载入 figures 模块失败：{exc}")
                try:
                    for pdf in (item.pdfs if FG else []):
                        if pdf.exists and pdf.path.lower().endswith(".pdf"):
                            fr = FG.extract_document(pdf.path)
                            figs["captions"] += fr.get("captions") or []
                            # ⚠ 表格只留一路：正文来自 MinerU 时，表格已经以 HTML
                            #   形式进了逐页正文（见 mineru.render_block），这里再收
                            #   一份就等于同一张表在正文里出现两次。正文走 Zotero
                            #   缓存 / PyMuPDF 的篇才需要它来补表格。
                            if str(source or "").startswith("mineru-"):
                                log("    （正文来自 MinerU：表格已在正文里，"
                                    "跳过 PyMuPDF 表格抽取）")
                            else:
                                figs["tables"] += fr.get("tables") or []
                            break      # 只取第一个可用 PDF
                except Exception as exc:  # noqa: BLE001
                    log(f"    [!!] {item.key} 抽图注/表格失败：{exc}")
                if figs["captions"] or figs["tables"]:
                    stats["figures"] = stats.get("figures", 0) + len(
                        figs["captions"])
                    stats["tables"] = stats.get("tables", 0) + len(
                        figs["tables"])

                # Markdown 落盘
                os.makedirs(S.PAPERS_DIR, exist_ok=True)
                os.makedirs(S.FULLTEXT_DIR, exist_ok=True)
                with open(item.paper_path(), "w", encoding="utf-8") as fh:
                    fh.write(C.item_markdown(item, pages, source, conn))
                if pages:
                    with open(item.fulltext_path(), "w", encoding="utf-8") as fh:
                        fh.write(C.fulltext_markdown(item, pages, figs))

                # 结论层写库（写前先删旧 chunk，保留经验层不动）
                _upsert_item(conn, item, source, built_at)

                # ---- extra 里的结构化字段（知网写入的 CLC/dbcode/中文译名…）
                #
                # 位置有硬约束：必须在 `_upsert_item` **之后**。`item_extra`
                # 有 `FOREIGN KEY(item_key) REFERENCES items(key)`
                # （schemas.connect 开了 `PRAGMA foreign_keys=ON`），
                # 条目行还不存在时写它就是外键违约，整条会失败。
                #
                # **删除侧不用在这里接线**：条目被 `_purge_item` 删掉时，
                # `ON DELETE CASCADE` 会带走这一行（tests/test_extrafill.py
                # 里有一条断言专门盯着"条目删了结构化行有没有跟着走"）。
                #
                # 失败只记一行、不中断这一条：结构化字段是附加信息，
                # 解析出问题不该让这篇文献进不了知识库（和图注/表格一个态度）。
                if extra_on:
                    try:
                        ex = XF.sync_item(conn, item, commit=False)
                        stats["extra_rows"] += ex["rows"]
                        stats["extra_cleared"] += ex["cleared"]
                        if ex["rows"]:
                            stats["extra_items"] += 1
                    except Exception as exc:  # noqa: BLE001
                        stats["extra_errors"].append(
                            {"key": item.key,
                             "error": f"{type(exc).__name__}: {exc}"})
                        log(f"    [!!] {item.key} 结构化字段写库失败：{exc}")

                # 图注与表格写库（先删后写，和 chunks 同一套模式）
                conn.execute("DELETE FROM figures WHERE item_key = ?",
                             (item.key,))
                for c in figs["captions"]:
                    conn.execute(
                        "INSERT INTO figures(item_key, page, kind, label, text,"
                        " built_at) VALUES(?,?,?,?,?,?)",
                        (item.key, c.get("page"), "caption-" + str(
                            c.get("kind") or "image"),
                         c.get("label") or "", c.get("text") or "", built_at))
                for t in figs["tables"]:
                    conn.execute(
                        "INSERT INTO figures(item_key, page, kind, label, text,"
                        " n_rows, n_cols, built_at) VALUES(?,?,?,?,?,?,?,?)",
                        (item.key, t.get("page"), "table", "",
                         t.get("markdown") or "", t.get("rows"),
                         t.get("cols"), built_at))
                conn.execute(
                    "UPDATE items SET n_figures = ?, n_tables = ? WHERE key = ?",
                    (len(figs["captions"]), len(figs["tables"]), item.key))

                conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN "
                    "(SELECT chunk_id FROM chunks WHERE item_key = ?)",
                    (item.key,),
                )
                conn.execute("DELETE FROM chunks WHERE item_key = ?", (item.key,))

                for chunk in C.collect_chunks(item, pages):
                    cur = conn.execute(
                        "INSERT INTO chunks(item_key, page, seq, text, n_chars) "
                        "VALUES(?,?,?,?,?)",
                        (item.key, chunk["page"], chunk["seq"], chunk["text"],
                         len(chunk["text"])),
                    )
                    chunk_id = cur.lastrowid
                    chunk["chunk_id"] = chunk_id
                    chunk["item_key"] = item.key
                    # FTS 与 chunks 用同一个 rowid，随写随建，避免"先全删后全建"
                    conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)",
                                 (chunk_id, chunk["text"]))
                    all_chunks.append(chunk)

                if idx % 20 == 0 or idx == len(items):
                    log(f"  [{idx}/{len(items)}] 已处理；累计切片 {len(all_chunks)}")
            except Exception as exc:  # noqa: BLE001
                errors.append({"key": item.key, "title": item.title[:80],
                               "error": f"{type(exc).__name__}: {exc}"})
                log(f"  ! {item.key} 处理失败：{type(exc).__name__}: {exc}")
            conn.commit()

        # ---------------------------------------------------------- 词元稀有度
        # 从**全量 chunks** 统计，不是只统计本次处理的 —— 否则增量跑一次
        # 就会把词元表缩成"只剩这一批"的样子，词汇稀有度全错。
        log("统计词元文档频率（供检索做稀有度加权）…")
        conn.execute("DELETE FROM term_df")
        _build_term_df(conn)
        conn.commit()

        # ---------------------------------------------------------- 向量
        # 只补**缺向量**的切片：增量时不用把全库重算一遍，也避免重复下载模型。
        vector_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if args.no_vectors:
            log("--no-vectors：跳过向量计算")
        else:
            pending = conn.execute(
                """
                SELECT c.chunk_id, c.text FROM chunks c
                LEFT JOIN embeddings e ON e.chunk_id = c.chunk_id
                WHERE e.chunk_id IS NULL
                """
            ).fetchall()
            if not pending:
                log(f"向量已齐全（{vector_count} 个），无需重算")
            else:
                log(f"算向量：{len(pending)} 个待补（模型 {args.embed_model}，"
                    f"首次会下载模型）…")
                t_vec = time.time()
                vectors = C.embed_texts(
                    [r["text"] for r in pending],
                    model_name=args.embed_model,
                    cache_dir=os.path.join(S.CACHE_DIR, "models"),
                )
                if vectors is not None:
                    dim = int(vectors.shape[1])
                    conn.executemany(
                        "INSERT OR REPLACE INTO embeddings(chunk_id, dim, vector, model) "
                        "VALUES(?,?,?,?)",
                        [
                            (r["chunk_id"], dim, vectors[i].tobytes(), args.embed_model)
                            for i, r in enumerate(pending)
                        ],
                    )
                    conn.commit()
                    # 换模型意味着旧向量作废：把不一致的清掉，下次重算。
                    #
                    # ⚠ 这里必须判"真正的换模型"，不能用 `model <> 当前模型` 一刀切。
                    # 踩过的坑：本库曾经在 **`meta.embed_model` 为空**的状态下存在
                    # 3706 个向量（某次 `--no-vectors` 构建把 meta 覆盖成了空串）。
                    # 那种状态下 `model <> 'BAAI/bge-small-zh-v1.5'` 成立 ——
                    # 等于把**全部**向量当成"旧模型的"删掉，重建期间检索的向量路
                    # 直接归零（kb_search 的 vector_used 变 false），要等 65 秒重算。
                    # 现在改成：只有 meta 里明确记着"另一个模型"时才清理。
                    prev_model = S.get_meta(conn, "embed_model", "")
                    if prev_model and prev_model != args.embed_model:
                        stale = conn.execute(
                            "SELECT COUNT(*) FROM embeddings WHERE model <> ?",
                            (args.embed_model,),
                        ).fetchone()[0]
                        if stale:
                            conn.execute("DELETE FROM embeddings WHERE model <> ?",
                                         (args.embed_model,))
                            conn.commit()
                            log(f"  清掉 {stale} 个旧模型的向量"
                                f"（{prev_model} → {args.embed_model}）")
                    else:
                        mismatched = conn.execute(
                            "SELECT COUNT(*) FROM embeddings WHERE model <> ?",
                            (args.embed_model,),
                        ).fetchone()[0]
                        if mismatched:
                            # meta 没记录但库里确有别的模型 —— 只提示，不擅自删
                            log(f"  [注意] 库里有 {mismatched} 个非 {args.embed_model} "
                                f"的向量（meta.embed_model={prev_model!r}），"
                                f"未自动清理；确认要换模型时用 --full 重建")
                    vector_count = conn.execute(
                        "SELECT COUNT(*) FROM embeddings").fetchone()[0]
                    S.set_meta(conn, "embed_model", args.embed_model)
                    S.set_meta(conn, "embed_dim", dim)
                    log(f"  向量完成：本批 {len(pending)} 个，维度 {dim}，"
                        f"累计 {vector_count}，耗时 {time.time() - t_vec:.1f}s")
                else:
                    log("  向量失败（不阻塞）：关键词检索仍然可用")

        # 无论是否算了新向量，元信息都要落到库里（否则 kb_stats 会显示"未建向量"）
        if not args.no_vectors:
            rows = conn.execute(
                "SELECT model, dim, COUNT(*) AS n FROM embeddings GROUP BY model, dim "
                "ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if rows:
                S.set_meta(conn, "embed_model", rows["model"])
                S.set_meta(conn, "embed_dim", rows["dim"])
        conn.commit()

        # ---------------------------------------------------------- 分级视图
        # ⚠ 位置有硬约束：必须在**这一批全部提交之后**。视图要读 items /
        #   figures / experience / item_weight 四张表（见 offline/kbviews.py），
        #   而它们是在这一轮里逐条写的 —— 提前生成会拿到半截数据。
        #
        # 为什么放在构建里而不是"用的时候现生成"：Zotero 右键菜单是**同步**
        # 构建的（弹菜单那一刻不能等网络/等 Python），它只能靠"文件在不在"
        # 来决定显示什么。所以文件必须先存在。
        #
        # 失败只记一行、不让构建失败：它是派生物，检索不依赖它。
        try:
            import kbviews as KV
            t_view = time.time()
            res = KV.write_many([it.key for it in items], log=log)
            log(f"  分级视图：{res['written']}/{res['keys']} 篇，"
                f"耗时 {time.time() - t_view:.1f}s")
            if res["failed"]:
                log(f"    [!!] {len(res['failed'])} 篇失败（不阻塞）："
                    f"{res['failed'][:2]}")
        except Exception as exc:  # noqa: BLE001
            log(f"    [!!] 分级视图没生成（不阻塞，检索不受影响）：{exc}")

        # ---------------------------------------------------------- 收尾统计
        total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        S.set_meta(conn, "built_at", built_at)
        S.set_meta(conn, "last_run_items", len(items))
        S.set_meta(conn, "kb_version", "1")
        conn.commit()

        manifest = {
            "built_at": built_at,
            "kb_version": 1,
            "zotero_db": S.ZOTERO_DB,
            "counts": {
                "items_in_kb": total_items,
                "items_this_run": len(items),
                "chunks": total_chunks,
                "vectors": vector_count,
                "experience_rows": conn.execute(
                    "SELECT COUNT(*) FROM experience"
                ).fetchone()[0],
            },
            # 本批次的来源分布（增量跑完时这里多数是 0，属正常）
            # 本次用的解析器（写进 MANIFEST：否则"库里这批正文到底是哪来的"
            # 只能靠 items.fulltext_src 猜）
            "parser": want_parser,
            "fulltext": {
                "parser": want_parser,
                "from_mineru": stats["fulltext_from_mineru"],
                "mineru_cached": stats["mineru_cached"],
                "mineru_fell_back": stats["mineru_failed"],
                "from_zotero_cache": stats["fulltext_from_cache"],
                "from_pymupdf": stats["fulltext_from_pymupdf"],
                "missing": stats["fulltext_missing"],
                # 字符偏移被自动还原的（源 PDF 字体编码坏，换源也修不了，
                # 只能位移还原 —— 见 zreader._deshift_fix）
                "deshifted": stats["deshifted"],
                # 缓存被判坏/灰区、因此换了源或走了仲裁的
                "cache_rejected": stats["cache_bad"],
            },
            # 全库的**实况**分布：增量模式下上面那个是本批次的，容易误读成"全文全丢了"
            # （实测：跑完一次 0 条增量后，from_zotero_cache 显示 0，看着像索引坏了）。
            # 这里从库里直接统计，任何模式下都能反映真实情况。
            "fulltext_total": {
                row["fulltext_src"] or "(无全文)": row["n"]
                for row in conn.execute(
                    "SELECT fulltext_src, COUNT(*) AS n FROM items GROUP BY 1"
                )
            },
            "fulltext_chars_total": conn.execute(
                "SELECT SUM(fulltext_chars) FROM items"
            ).fetchone()[0] or 0,
            # 结构化字段（extra → item_extra）的落库情况。
            #
            # 为什么单独报：这些字段（中图分类号 / 来源库 / 中文译名 / 收录标签…）
            # 是**附加**信息，不进 items 表，检索和展示侧另走一次查询读它。
            # 构建时不报出来，用户就没有任何地方能看出"这次到底带上了没有"。
            # `rows_total` 是**全库**的，增量构建里 rows_this_run 是 0 属正常
            # （没变化的条目会被跳过，它们的行本来就在）。
            "structured_extra": {
                "rows_this_run": stats["extra_rows"],
                "cleared_this_run": stats["extra_cleared"],
                "items_this_run": stats["extra_items"],
                "rows_total": _count_item_extra(conn) if extra_on else None,
                "errors": stats["extra_errors"],
            },
            "warnings": {
                "missing_pdf_files": stats["missing_pdf_files"],
                "items_without_pdf": stats["no_pdf"],
                # 本次跳过正文解析的条目（没有 PDF 附件）
                "skipped_no_pdf": stats["skipped_no_pdf"],
                "errors": errors,
            },
            "collections": reader.collection_tree(),
            # 从库里读实际模型名，不要写 `args.embed_model if vector_count else ""`。
            # 那样在 `--no-vectors` 构建时会把空串写进 meta，制造出
            # "meta 说没建向量、库里实际有 3706 个"的危险状态
            # （下一轮构建的 stale 清理会因此误删全部向量，见上面那段注释）。
            "embed_model": S.get_meta(conn, "embed_model", ""),
            "seconds": round(time.time() - t_start, 1),
        }
        with open(S.MANIFEST, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)

    finally:
        reader.close()
        conn.close()

    # ---------------------------------------------------------- 报告
    log("")
    log("=" * 72)
    log("构建完成")
    log("=" * 72)
    log(f"  知识库条目：{manifest['counts']['items_in_kb']}"
        f"（本次处理 {manifest['counts']['items_this_run']}）")
    log(f"  切片：{manifest['counts']['chunks']}　向量：{manifest['counts']['vectors']}")
    # 解析器与来源分布：**必须报 MinerU**，否则跑了半天看不出这次到底用了谁
    # （用户的原话是"MinerU 的效果好的出人预料"，报告里得能看出它有没有生效）
    ft = manifest["fulltext"]
    parser = manifest.get("parser") or "zotero"
    log(f"  解析器：{parser}"
        + ("（MinerU 可选组件；失败会按篇回落）" if parser != "zotero" else
           "（Zotero 缓存 + PyMuPDF，默认）"))
    if parser != "zotero" or ft.get("from_mineru"):
        log(f"  MinerU：本次 {ft.get('from_mineru', 0)} 篇"
            f"（其中命中文档指纹、没重跑的 {ft.get('mineru_cached', 0)} 篇）"
            f"，回落 {len(ft.get('mineru_fell_back') or [])} 篇")
        for x in (ft.get("mineru_fell_back") or [])[:5]:
            log(f"      {x['key']}  {x['why']}  {x['title'][:40]}")
    log(f"  全文来源：Zotero 缓存 {ft['from_zotero_cache']} 篇，"
        f"PyMuPDF {ft['from_pymupdf']} 篇，"
        f"缺失 {len(ft['missing'])} 篇")
    se = manifest.get("structured_extra") or {}
    if se:
        total = se.get("rows_total")
        log(f"  结构化字段（extra）：本次写入 {se['rows_this_run']} 行、"
            f"清空删除 {se['cleared_this_run']} 行；全库共 "
            f"{'—' if total is None else total} 行")
        if se.get("errors"):
            log(f"  ⚠ 结构化字段写失败的条目：{len(se['errors'])} 条")
    if manifest["fulltext"].get("deshifted"):
        d = manifest["fulltext"]["deshifted"]
        log(f"  ⚙ 字符偏移已自动还原：{len(d)} 篇"
            f"（源 PDF 字体编码坏，换源修不了）")
        for x in d[:5]:
            log(f"      {x['key']}  {x['src']}")
    if manifest["warnings"]["missing_pdf_files"]:
        log(f"  ⚠ 磁盘上找不到的 PDF 附件：{len(manifest['warnings']['missing_pdf_files'])} 个")
    skipped = manifest["warnings"].get("skipped_no_pdf") or []
    if skipped:
        log("")
        log("  " + "─" * 66)
        log(f"  ⚠ 有 {len(skipped)} 条没有 PDF 附件，**没有收进知识库**：")
        for x in skipped[:12]:
            log(f"      {x['key']}  {(x.get('title') or '')[:52]}")
        if len(skipped) > 12:
            log(f"      …还有 {len(skipped) - 12} 条")
        log("      想让它们进知识库：在 Zotero 里给条目挂上 PDF，再重建一次。")
        log("  " + "─" * 66)
        log("")
    if manifest["warnings"]["errors"]:
        log(f"  ⚠ 处理失败的条目：{len(manifest['warnings']['errors'])} 条")
    log(f"  耗时 {manifest['seconds']} 秒　清单：{S.MANIFEST}")

    # 顺手重建「给人看的」文献清单 INDEX.md。
    # 为什么放在这里：它是索引的派生物，重建索引后不刷新就会过期
    # （用户点开看到的是上次的条目，以为新文献没进来）。
    # ⚠ 用 try 包住：这只是个便利功能，失败了不该让整个构建算失败。
    try:
        sys.path.insert(0, os.path.join(S.KB_ROOT, "tools"))
        import make_index  # noqa: PLC0415
        out = make_index.write_index()
        log(f"  文献清单：{out}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ 文献清单生成失败（不影响索引）：{type(exc).__name__}: {exc}")
    log("")

    return (0 if not manifest["warnings"]["errors"] else 1), manifest


def _backfill_extra_once(conn, reader, all_ids, extra_on: bool) -> None:
    """老库第一次跑新版时，把 `item_extra` 一次性补齐（每个库只跑一次）。

    为什么需要这一步：逐条接线**只覆盖"本次要重建的条目"**，而已经建好的库
    在增量模式下经常是"0 条需要处理" —— 表会一直空着，检索侧的筛选与展示
    就永远没数据，功能看起来像"没接上"（而且不报错，最难发现）。

    什么时候跑：表是空的、知识库里**有**条目、且 meta 里没记过"已回填"。
    跑完在 meta 里记一笔，之后的构建不再重复扫。

    ⚠ 只同步**知识库里已有的条目**（`items` 表里的 key）：没有 PDF、
    已被 `_purge_item` 清掉的条目在这张表里没有对应行，硬写会违外键。

    ⚠ 失败不中断构建：结构化字段是附加信息，拿不到它照样能把文献收进知识库。
    """
    if not extra_on or S.get_meta(conn, "extra_backfilled", ""):
        return
    try:
        if not conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            return                       # 空库：等第一次真正建库时再说
        if _count_item_extra(conn):
            return                       # 已经有数据了，不需要回填
        kb_keys = {r["key"] for r in conn.execute("SELECT key FROM items")}
        items = [it for it in reader.load_items(all_ids) if it.key in kb_keys]
        ex = XF.sync_items(conn, items, commit=True, ensure=False)
        S.set_meta(conn, "extra_backfilled", "1")
        log(f"  结构化字段（extra）：老库首次回填 {ex['rows']} 行"
            f"（扫了 {ex['items']} 篇）")
    except Exception as exc:  # noqa: BLE001
        log(f"  [!!] 结构化字段回填失败（不影响构建）：{type(exc).__name__}: {exc}")


def _count_item_extra(conn) -> int | None:
    """数一下 `item_extra` 有多少行；表不存在时返回 None（不抛）。

    为什么要单独一个函数：老库（还没跑过新版本构建）里根本没这张表，
    `SELECT COUNT(*)` 会抛 `no such table`。构建的收尾统计**不该**因为
    一个附加表而失败 —— 但也不能假装它是 0（0 行和"没有这张表"是两件事）。
    """
    try:
        return conn.execute("SELECT COUNT(*) FROM item_extra").fetchone()[0]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 删除对账
#
# 三个动作（归档 / 删除 / 还原）的**实现在 offline/trash.py**，这里只是给对账
# 逻辑起名字（全局纪律 7：同一件事只允许一份实现，别处只能调它）。
# 保留这三个薄封装是为了让下面 `_reconcile_deletions` 读起来就是"三支动作"，
# **别再往它们里面加 SQL 或文件操作**。


def _archive_key(conn, key: str) -> dict:
    """阶段①：进回收站 → 归档到 kb/trash/<key>/（含视图/档案/正文/MinerU 产物）。"""
    return TR.archive(conn, key)


def _delete_archived(conn, key: str) -> dict:
    """阶段②：回收站里彻底删 → 清掉归档目录，墓碑改 kind='deleted'。"""
    return TR.delete_archived(conn, key)


def _restore_key(conn, key: str, version: int | None = None) -> dict:
    """阶段③：从回收站还原 → 搬回产物 + 按 rows.json 复原数据库行 + 清墓碑。"""
    return TR.restore(conn, key, version=version)


def _reconcile_deletions(conn, live_keys: set[str], trashed_keys: set[str],
                         live_versions: dict | None = None) -> dict:
    """把知识库与 Zotero 的"条目集合"对齐（两段式删除，见方案附录 H.1）。

    三个集合的分工（H.1 的检测信号）：
        live_keys     在 Zotero 的 items 里、且不在 deletedItems  → 正常
        trashed_keys  在 items 里、且在 deletedItems             → 归档（阶段①）
        两处都没有                                               → 删除（阶段②）

    四支判断：
      · 库里有这条、Zotero 里在回收站            → `_archive_key()`
      · 库里有这条、Zotero 里两处都没有          → `_delete_archived()`
      · 库里没有、live 且有 kind='trashed' 墓碑  → `_restore_key()`
      · 归档目录还在、Zotero 里两处都没有        → `_delete_archived()`
        （库里已经没有它的行，只需清目录 + 改墓碑）

    返回值里的 `degraded` **不是第五支判断**，而是阶段③的降级记录（T0-11）：
    还原时探到快照里的 id 已被别的文献复用，就整条放弃直插、只把产物搬回原位，
    数据库行交给紧随其后的增量重抽。这里只负责把它报出来（原因在 trash.py 的
    `_id_conflicts`）。

    ⚠ 这是"既有缺陷的修补"的延续：原来 `_purge_orphans` 只在"条目还在、但 PDF
      没了"时清理，而"用户在 Zotero 里把整条删了"没有任何路径会处理它 —— 增量判断
      只比对 `items.version`，条目都不在了自然也没得比，于是孤儿永远留在库里，
      `kb_search` 还能搜到一篇**已经被删掉的文献**。**删了就该搜不到**。
    """
    archived: list[str] = []
    deleted: list[str] = []
    restored: list[str] = []
    degraded: list[str] = []        # 还原时探到 id 被复用、只能重建的（T0-11）

    kb_keys = {row["key"] for row in conn.execute("SELECT key FROM items")}
    tombstones: dict[str, str] = {}
    try:
        tombstones = {row["key"]: row["kind"] for row in conn.execute(
            "SELECT key, kind FROM item_tombstone")}
    except sqlite3.Error:
        pass        # 老库还没补上墓碑表（init_db/connect 会补），按"没有墓碑"处理

    # 阶段③：从回收站还原回来了（key 回到 live，且留着 kind='trashed' 的墓碑）
    for key, kind in sorted(tombstones.items()):
        if kind == "trashed" and key in live_keys:
            result = _restore_key(conn, key, (live_versions or {}).get(key))
            if result["ok"]:
                restored.append(key)
                # 探到 id 已被别人复用（T0-11）：数据库行**没有**复原，只能等增量重抽。
                # 记下来给构建收尾报一声 —— 这件事必须看得见，静默降级等于没修。
                if result.get("degraded"):
                    degraded.append(
                        f"{key}（{result.get('note') or 'id 已被复用'}）")
            else:
                # 失败必须报出来：这里原来**没有 else** —— 一次失败的还原连日志都不留，
                # 条目就留在归档里、每轮重试、每轮静默失败（本机在 items.item_id 撞
                # UNIQUE 那条路径上实测到过：`IntegrityError: UNIQUE constraint
                # failed: items.item_id`）。
                log(f"  [!!] 还原 {key} 失败：{result['why']}")

    handled: set[str] = set()
    for key in sorted(kb_keys):
        if key in live_keys or key in handled:
            continue
        if key in trashed_keys:
            result = _archive_key(conn, key)
            if result["ok"]:
                archived.append(key)
            else:
                log(f"  [!!] 归档 {key} 失败：{result['why']}")
        else:
            result = _delete_archived(conn, key)
            if result["ok"]:
                deleted.append(key)
            else:
                log(f"  [!!] 删除 {key} 失败：{result['why']}")
        handled.add(key)

    # 阶段②的另一半：库里已经没有行，但 kb/trash 下还留着目录
    for key in TR.archived_keys():
        if key in live_keys or key in trashed_keys or key in handled:
            continue
        result = _delete_archived(conn, key)
        if result["ok"]:
            deleted.append(key)
    return {"archived": archived, "deleted": deleted, "restored": restored,
            "degraded": degraded}


def _purge_item(conn, item) -> None:
    """条目**还在 Zotero 里**、只是没有 PDF 附件了：把它从知识库里摘掉。

    给"没有 PDF 附件的条目"用（用户要求：没有 PDF 的就不要它）。
    **必须主动清**：用户删掉 PDF 附件之后，这些派生数据不会自己消失 ——
    增量判断只看 `items.version`，而删附件不一定改它。不清的话用户会看到
    "我明明没有 PDF，怎么还能搜到"。

    ⚠ 这一条**不归档、不留墓碑**（走 `trash.discard_live`）：它没有被删，
      还在 Zotero 里；留了 kind='trashed' 的墓碑，下一轮对账就会把它当成
      "从回收站还原"搬回来 —— 删掉 PDF 的条目会每轮复活一次。

    ⚠ 不动 `experience` / `item_weight`：那是**用户自己记的数据**
    （用过什么方法、效果如何、标没标重点），不属于"构建产物"。
    重建知识库从来不该丢经验，删除也一样。**对账也不动它们** ——
    用户可能给一篇已删文献记过经验，那是有价值的记录。
    """
    TR.discard_live(conn, item.key)


def _upsert_item(conn, item, source: str, built_at: str) -> None:
    conn.execute(
        """
        INSERT INTO items(item_id, key, item_type, title, year, first_author, author_line,
                          venue, doi, abstract, tags, collections, n_pdfs, n_notes,
                          n_annotations, fulltext_chars, fulltext_src, date_added, version,
                          built_at, collections_sig)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET
            item_type=excluded.item_type, title=excluded.title, year=excluded.year,
            first_author=excluded.first_author, author_line=excluded.author_line,
            venue=excluded.venue, doi=excluded.doi, abstract=excluded.abstract,
            tags=excluded.tags, collections=excluded.collections, n_pdfs=excluded.n_pdfs,
            n_notes=excluded.n_notes, n_annotations=excluded.n_annotations,
            fulltext_chars=excluded.fulltext_chars, fulltext_src=excluded.fulltext_src,
            date_added=excluded.date_added, version=excluded.version,
            built_at=excluded.built_at, collections_sig=excluded.collections_sig
        """,
        (
            item.item_id, item.key, item.item_type, item.title, item.year,
            item.first_author, item.author_line, item.venue, item.doi, item.abstract,
            json.dumps(item.tags, ensure_ascii=False),
            json.dumps(item.collections, ensure_ascii=False),
            len(item.pdfs), len(item.notes), len(item.annotations),
            item.fulltext_chars, source, item.date_added, item.version, built_at,
            # 指纹与上面 collections/tags 用同一份数据算，避免两处不一致
            S.collections_sig(item.collection_keys, item.tags),
        ),
    )


def _build_term_df(conn) -> None:
    """统计每个词元出现在多少个切片里（document frequency），**从全量 chunks 读**。

    统计单位刻意与检索侧保持一致：
      · CJK 逐字统计（FTS5 unicode61 下中文就是单字 token）
      · ASCII 按词统计
    只保留 df >= 2 的词元 —— df=1 的词元最稀有，但数量很多（生僻字、编号），
    全存进来表格会虚胖。检索侧对查不到的词元按最大权重处理，效果等价。

    注意：必须扫全量而不是只扫本次处理的条目，否则增量跑一次
    就会把词元稀有度算成"只在这一批里"的样子，排序直接失真。
    """
    cjk_re = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    word_re = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,30}")
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT text FROM chunks"):
        text = row["text"]
        seen = set(cjk_re.findall(text))
        seen.update(w.lower() for w in word_re.findall(text))
        for term in seen:
            counts[term] = counts.get(term, 0) + 1
    rows = [(t, n) for t, n in counts.items() if n >= 2]
    conn.executemany("INSERT OR REPLACE INTO term_df(term, df) VALUES(?,?)", rows)
    conn.commit()
    log(f"  词元表：{len(rows)} 项（出现于 ≥2 个切片）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 Zotero 库构建成知识库")
    parser.add_argument("--full", action="store_true", help="全量重建（默认增量）")
    parser.add_argument("--no-vectors", action="store_true", help="跳过向量计算")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    # 逐篇重建：跳过增量判断强制重建指定条目（Zotero 右键菜单用）
    parser.add_argument("--item", action="append", default=[], metavar="KEY",
                        help="只重建指定条目（可重复；跳过增量判断，无论变没变都重建）")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL,
                        help=f"嵌入模型（默认 {DEFAULT_EMBED_MODEL}）")
    # 图注与表格提取的开关（默认开）。抽它们要读 PDF，会给构建多加
    # 约 60 秒（101 篇实测 178 秒里的那部分）。
    parser.add_argument("--no-figures", action="store_true",
                        help="跳过图注与表格提取（省约 1 分钟）")
    parser.add_argument("--figures", action="store_true",
                        help="强制提取图注与表格（覆盖配置里的关闭）")
    # 入库解析校验：缓存文本 vs PyMuPDF 的取舍策略（见 zreader.cache_model_mode）
    parser.add_argument(
        "--parser", choices=["zotero", "flash", "basic", "standard", "advanced"],
        default="zotero",
        help="正文来源：zotero（默认，Zotero 缓存 + PyMuPDF）或 MinerU 档位"
             "（flash/basic/standard/advanced，需先装 MinerU）")
    parser.add_argument(
        "--force-parse", action="store_true",
        help="MinerU 重解析时忽略已有产物（默认按 PDF+档位指纹跳过）")
    parser.add_argument(
        "--parse-timeout", type=float, default=0.0,
        help="单篇 MinerU 解析的超时秒数（0 = 用默认 3600）")
    parser.add_argument("--cache-check", choices=["off", "auto", "deep"], default="",
                        help="正文来源校验：off 只用规则；auto 灰区调本地模型仲裁"
                             "（默认）；deep 每篇都跑两条路再让模型选（最慢，最准）")
    args = parser.parse_args(argv)
    rc, _manifest = build(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
