"""维护：环境自检、重建、备份、统计。

    python offline/maintain.py check      # 环境自检（依赖 / 索引 / Zotero / Ollama）
    python offline/maintain.py stats      # 库规模与经验统计
    python offline/maintain.py backup     # 备份 index.db 与经验层
    python offline/maintain.py reindex-fst   # 只重建 FTS 与词元表（不动向量）
    python offline/maintain.py vacuum     # 整理数据库
    python offline/maintain.py reconcile  # 与 Zotero 对账巡检（**默认只读**）
    python offline/maintain.py reconcile --apply 1   # 只处理第 1 项，逐条确认
    python offline/maintain.py reconcile --apply --yes   # 全部可动项，免确认（慎用）

自检是给"换机器 / 隔了几个月回来"用的：它会明确指出哪一环断了、怎么修。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402

OK = "  [OK]  "
WARN = "  [!!]  "
BAD = "  [XX]  "


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- 自检


def check() -> int:
    problems: list[str] = []
    core_broken = False     # 只有核心链路断了才返回非零，可选增强未就绪不算故障
    print("=" * 68)
    print("文献知识库 环境自检")
    print("=" * 68)

    # 1. Python 依赖
    print("\n[1] Python 依赖")
    deps = {
        "mcp": "MCP 服务器（必需）",
        "pymupdf": "PDF 解析（离线兜底，可选）",
        "numpy": "向量检索（必需）",
        "fastembed": "嵌入模型（可选，缺了只剩关键词检索）",
        "zstandard": "读 DSH 会话记录（可选）",
    }
    for mod, why in deps.items():
        try:
            __import__(mod)
            print(f"{OK}{mod:12} {why}")
        except ImportError:
            level = WARN if mod in ("pymupdf", "fastembed", "zstandard") else BAD
            print(f"{level}{mod:12} 缺失 —— {why}")
            if level == BAD:
                problems.append(f"缺少必需依赖 {mod}：pip install {mod}")
                core_broken = True

    # 2. Zotero 数据
    print("\n[2] Zotero 数据")
    if os.path.exists(S.ZOTERO_DB):
        size_mb = os.path.getsize(S.ZOTERO_DB) / 1024 / 1024
        mtime = datetime.fromtimestamp(os.path.getmtime(S.ZOTERO_DB)).strftime("%Y-%m-%d %H:%M")
        print(f"{OK}库文件 {S.ZOTERO_DB}（{size_mb:.1f} MB，最后修改 {mtime}）")
    else:
        print(f"{BAD}找不到 {S.ZOTERO_DB}")
        problems.append("Zotero 数据目录路径不对，检查 schemas.py 的 ZOTERO_DATA_DIR")
        core_broken = True
    if os.path.isdir(S.ZOTERO_STORAGE):
        n = len([d for d in os.listdir(S.ZOTERO_STORAGE)
                 if os.path.isdir(os.path.join(S.ZOTERO_STORAGE, d))])
        print(f"{OK}附件目录有 {n} 个子目录")
    else:
        print(f"{BAD}找不到附件目录 {S.ZOTERO_STORAGE}")
        problems.append("Zotero storage 目录不存在")

    # 3. 索引库
    print("\n[3] 索引库")
    if not os.path.exists(S.INDEX_DB):
        print(f"{BAD}还没有索引：{S.INDEX_DB}")
        problems.append("先跑一次 convert.py 建索引")
        core_broken = True
    else:
        conn = S.connect(S.INDEX_DB)
        items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        vecs = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
        terms = conn.execute("SELECT COUNT(*) AS n FROM term_df").fetchone()["n"]
        exp = conn.execute("SELECT COUNT(*) AS n FROM experience").fetchone()["n"]
        print(f"{OK}条目 {items}｜切片 {chunks}｜向量 {vecs}｜词元 {terms}｜经验 {exp}")
        if items == 0:
            problems.append("索引里没有条目，重新构建")
        if chunks and vecs == 0:
            print(f"{WARN}没有向量 —— 语义检索不可用（关键词检索仍可用）")
            problems.append("补向量：convert.py（不加 --no-vectors）")
        # FTS 与 chunks 是否一一对应（漏了会导致"检索不到明明该命中的内容"）
        fts = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
        if fts != chunks:
            print(f"{BAD}FTS 行数 {fts} 与切片数 {chunks} 不一致")
            problems.append("FTS 与 chunks 不同步：跑 maintain.py reindex-fst")
            core_broken = True
        else:
            print(f"{OK}FTS 与切片一一对应")
        # 测试数据残留：会抬高某些文献的权重、并把"自检经验"带进检索结果。
        # 两个会写数据的测试现在都自清理了，这里兜底检查一次。
        test_exp = conn.execute(
            "SELECT COUNT(*) AS n FROM experience "
            "WHERE asked LIKE '%自检%' OR asked LIKE '%验收%' OR tags LIKE '%测试%'"
        ).fetchone()["n"]
        orphan = conn.execute(
            "SELECT COUNT(*) AS n FROM item_weight w "
            "WHERE (w.pinned <> 0 OR w.manual <> 0 OR (w.note IS NOT NULL AND w.note <> '')) "
            "AND NOT EXISTS (SELECT 1 FROM experience e "
            "                WHERE e.item_keys LIKE '%' || w.item_key || '%')"
        ).fetchone()["n"]
        if test_exp or orphan:
            print(f"{WARN}测试数据残留：自检经验 {test_exp} 条、无经验支撑的权重 {orphan} 行")
            print("       它们会继续影响检索排序。清理：")
            print("         tools\\kb_admin.py clean --test-data")
            print("         tools\\kb_admin.py clean --orphan-weights")
            problems.append("清理测试数据残留")
        else:
            print(f"{OK}经验层无测试残留")
        # 全文登记是否完整（曾因没回填 fulltext_pages 而全部为 0）
        ft_zero = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE fulltext_chars = 0"
        ).fetchone()["n"]
        ft_sum = conn.execute(
            "SELECT SUM(fulltext_chars) AS s FROM items"
        ).fetchone()["s"] or 0
        if items and ft_zero == items:
            print(f"{BAD}所有条目的 fulltext_chars 都是 0 —— 正文没被登记"
                  f"（索引可能坏，或又是没回填 fulltext_pages）")
            problems.append("跑一次 convert.py --full 重建（修复 fulltext_chars）")
        else:
            print(f"{OK}正文已登记：{items - ft_zero} 条有正文，共 {ft_sum:,} 字符")
        built = S.get_meta(conn, "built_at", "")
        zot_mtime = datetime.fromtimestamp(os.path.getmtime(S.ZOTERO_DB)).isoformat(
            timespec="seconds")
        print(f"    索引构建于 {built or '(未知)'}")
        print(f"    Zotero 库改于 {zot_mtime}")
        if built and built < zot_mtime:
            print(f"{WARN}Zotero 库比索引新 —— 可能有新文献没进库")
            problems.append("跑一次 convert.py 增量更新")
        conn.close()

    # 4. Zotero 本地 API（可选，只影响"新条目元数据"来源）
    print("\n[4] Zotero 本地 API（可选）")
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:23119/api/", timeout=3) as resp:
            print(f"{OK}可用（HTTP {resp.status}）")
    except Exception:  # noqa: BLE001
        print(f"{WARN}未响应。要么 Zotero 没开，要么没勾选"
              "「设置 → 高级 → 允许本机上的其他应用程序与 Zotero 通信」。")
        print("       这不影响检索与读全文（知识库自带全文）。")

    # 5. Ollama（可选，用于打标签/摘要/抽经验）
    print("\n[5] 本地小模型 Ollama（可选）")
    # 先探 API：服务活着比"命令在不在 PATH"更能说明问题。
    # Windows 上 Ollama 装到 %LOCALAPPDATA%\Programs\Ollama，但**不一定进 PATH**，
    # 只查命令会误报"没找到"（实测踩过）。
    api_alive, models = False, []
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        api_alive = True
        models = [m.get("name", "") for m in data.get("models", [])]
    except Exception:  # noqa: BLE001
        api_alive = False

    ollama = shutil.which("ollama")
    if not ollama:
        guess = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                             "Ollama", "ollama.exe")
        if os.path.exists(guess):
            ollama = guess
            print(f"{OK}ollama 在 {guess}（未加入 PATH，不影响使用）")
    if api_alive:
        print(f"{OK}服务在跑（127.0.0.1:11434）")
        if models:
            print(f"{OK}已装模型：{', '.join(models)}")
            if any("4b" in m.lower() for m in models):
                print(f"{OK}有 4B 模型（抽经验/写摘要的首选）")
            else:
                print(f"{WARN}没看到 4B 模型，抽经验质量会打折："
                      f"ollama pull qwen3:4b-instruct")
        else:
            print(f"{WARN}还没拉模型：ollama pull qwen3:4b-instruct")
    elif ollama:
        print(f"{WARN}找到命令但服务没响应（{ollama}）。启动 Ollama 后再试。")
        print("       注：检索与读全文不依赖它。")
    else:
        print(f"{WARN}没装 Ollama（不影响检索，只影响打标签/摘要/抽经验）")

    # 6. DSH 接入
    print("\n[6] DSH 接入")
    prof_dir = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop")
    pkg = os.path.join(prof_dir, "package.json")
    patch = os.path.join(prof_dir, "cordis.patch.yml")
    bundle_ok = False
    if os.path.exists(pkg):
        try:
            data = json.load(open(pkg, encoding="utf-8"))
            bundles = (data.get("dsh", {}).get("profile", {}).get("bundles") or [])
            deps = data.get("dependencies") or {}
            if "dsh-bundle-zotero-kb" in bundles:
                bundle_ok = True
                print(f"{OK}profile 的 bundles 里有 dsh-bundle-zotero-kb")
                print(f"     依赖声明：{deps.get('dsh-bundle-zotero-kb', '(缺失)')}")
            else:
                print(f"{BAD}profile 的 bundles 里没有 dsh-bundle-zotero-kb")
                print("     修法：dsh plugin --profile desktop add "
                      f"\"link:{S.PROJECT_ROOT.replace(os.sep, '/')}/bundle\"")
                problems.append("把 bundle 装进 desktop profile")
                core_broken = True
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD}读 package.json 失败：{exc}")
            problems.append("desktop profile 的 package.json 可能损坏")
            core_broken = True
    else:
        print(f"{BAD}找不到 {pkg}")
        problems.append("desktop profile 目录不对")
        core_broken = True

    # bundle 自己的 patch 必须存在，否则插件树装配时会报 not found
    bundle_patch = os.path.join(S.KB_ROOT, "bundle", "cordis.patch.yml")
    if os.path.exists(bundle_patch):
        text = open(bundle_patch, encoding="utf-8").read()
        if "- insert:" in text:
            print(f"{OK}bundle 的 patch 用了 insert 语法")
        else:
            print(f"{BAD}bundle 的 patch 没有 `- insert:` —— 新增条目必须用它，"
                  f"直接写 `- id:` 会报 entry not found")
            problems.append("修 bundle/cordis.patch.yml 的语法")
            core_broken = True
    else:
        print(f"{BAD}找不到 bundle 的 patch：{bundle_patch}")
        problems.append("bundle 目录不完整")
        core_broken = True

    # profile 自己的 patch 不该出现 zotero 条目（那是覆盖层，写了会报错）
    if os.path.exists(patch):
        text = open(patch, encoding="utf-8").read()
        if "zotero" in text:
            print(f"{BAD}profile 的 cordis.patch.yml 里出现了 zotero 条目 —— "
                  f"那是覆盖层，写不存在的新增条目会让 profile 报错，必须删掉")
            problems.append("从 cordis.patch.yml 移除 zotero 条目")
            core_broken = True
        if text.startswith("\ufeff"):
            print(f"{BAD}配置文件带 BOM —— 会让整个 profile 不可选，必须去掉")
            problems.append("去掉 cordis.patch.yml 的 BOM")
            core_broken = True
        if not text.startswith("\ufeff") and "zotero" not in text:
            print(f"{OK}cordis.patch.yml 干净（无 BOM、无越界条目）")

    # 7. 技能文件
    skill = os.path.join(os.path.expanduser("~"), ".dsh", "skills", "zotero-kb", "SKILL.md")
    if os.path.exists(skill):
        print(f"{OK}技能已安装：{skill}")
    else:
        print(f"{WARN}技能未安装（不影响工具调用，只少了使用约定）")
        problems.append("跑 scripts/sync-skill.ps1 安装技能")

    print("\n" + "=" * 68)
    if problems:
        print(f"发现 {len(problems)} 个待处理项：")
        for i, p in enumerate(problems, start=1):
            print(f"  {i}. {p}")
    else:
        # 核心链路（依赖 / Zotero 数据 / 索引 / DSH 接入）都通过；
        # [4] Zotero 本地 API 与 [5] Ollama 是可选增强，未就绪不算故障
        print("核心链路全部正常（[4][5] 是可选项，未就绪不影响检索与读全文）。")
    print("=" * 68)
    return 1 if core_broken else 0


# ---------------------------------------------------------------- 统计


def stats() -> int:
    conn = S.connect(S.INDEX_DB)
    print("=" * 68)
    print("知识库统计")
    print("=" * 68)
    for label, sql in (
        ("条目", "SELECT COUNT(*) FROM items"),
        ("切片", "SELECT COUNT(*) FROM chunks"),
        ("向量", "SELECT COUNT(*) FROM embeddings"),
        ("词元", "SELECT COUNT(*) FROM term_df"),
        ("经验", "SELECT COUNT(*) FROM experience"),
        ("有经验的文献", "SELECT COUNT(*) FROM item_weight WHERE attempts > 0"),
        ("标为重点", "SELECT COUNT(*) FROM item_weight WHERE pinned = 1"),
    ):
        print(f"  {label:14} {conn.execute(sql).fetchone()[0]}")

    print("\n  全文来源分布：")
    for row in conn.execute(
        "SELECT fulltext_src, COUNT(*) n FROM items GROUP BY 1 ORDER BY n DESC"
    ):
        print(f"    {row['fulltext_src'] or '(无全文)':16} {row['n']}")

    print("\n  条目最多的分类（前 8）：")
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT collections FROM items"):
        for name in json.loads(row["collections"] or "[]"):
            counts[name] = counts.get(name, 0) + 1
    for name, n in sorted(counts.items(), key=lambda x: -x[1])[:8]:
        print(f"    {name:24} {n}")

    print("\n  经验结果分布：")
    for row in conn.execute(
        "SELECT outcome, COUNT(*) n FROM experience GROUP BY 1 ORDER BY n DESC"
    ):
        print(f"    {row['outcome']:14} {row['n']}")

    print("\n  权重最高的文献（前 5）：")
    for row in conn.execute(
        """
        SELECT w.item_key, w.attempts, w.effective, w.ineffective, w.pinned, i.title
        FROM item_weight w LEFT JOIN items i ON i.key = w.item_key
        ORDER BY (3*w.pinned + w.manual + 2*w.effective + 0.5*w.ineffective) DESC
        LIMIT 5
        """
    ):
        print(f"    {row['item_key']} 尝试{row['attempts']} 有效{row['effective']} "
              f"无效{row['ineffective']}{' ★' if row['pinned'] else ''}  "
              f"{(row['title'] or '')[:34]}")
    conn.close()
    return 0


# ---------------------------------------------------------------- FTS 重建


def reindex_fst() -> int:
    """只重建 FTS 与词元表，不动 chunks 与向量。用于修 FTS 与切片不同步。"""
    print("重建 FTS 与词元表…")
    conn = S.connect(S.INDEX_DB)
    conn.execute("DELETE FROM chunks_fts")
    rows = conn.execute("SELECT chunk_id, text FROM chunks").fetchall()
    conn.executemany("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)",
                     [(r["chunk_id"], r["text"]) for r in rows])
    conn.execute("DELETE FROM term_df")

    import re
    cjk_re = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    word_re = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,30}")
    counts: dict[str, int] = {}
    for row in rows:
        seen = set(cjk_re.findall(row["text"]))
        seen.update(w.lower() for w in word_re.findall(row["text"]))
        for term in seen:
            counts[term] = counts.get(term, 0) + 1
    terms = [(t, n) for t, n in counts.items() if n >= 2]
    conn.executemany("INSERT OR REPLACE INTO term_df(term, df) VALUES(?,?)", terms)
    conn.commit()
    print(f"  完成：FTS {len(rows)} 行，词元 {len(terms)} 项")
    conn.close()
    return 0


# ---------------------------------------------------------------- 备份


def backup() -> int:
    """备份索引库（含经验层）。经验是攒出来的，丢不起。"""
    dst_dir = os.path.join(S.KB_DIR, "backups")
    os.makedirs(dst_dir, exist_ok=True)
    stamp = now_stamp()
    dst = os.path.join(dst_dir, f"index-{stamp}.db")
    if not os.path.exists(S.INDEX_DB):
        print("还没有索引库，无需备份")
        return 1
    # 用 SQLite 自己的备份 API，保证一致性（WAL 下直接拷文件可能拿到半截状态）
    src = sqlite3.connect(S.INDEX_DB)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    size_mb = os.path.getsize(dst) / 1024 / 1024
    print(f"已备份到 {dst}（{size_mb:.1f} MB）")

    # 只保留最近 10 份
    files = sorted(
        (f for f in os.listdir(dst_dir) if f.startswith("index-") and f.endswith(".db")),
        reverse=True,
    )
    for old in files[10:]:
        os.remove(os.path.join(dst_dir, old))
        print(f"  清掉旧备份 {old}")
    return 0


def views(key: str = "") -> int:
    """补齐每篇文献的**分级视图**（views/*.md，见 offline/kbviews.py）。

    为什么需要单独一个入口：视图是构建时生成的，但下面两种情况不会触发构建
    —— ① 升级到这个版本时库里已有的条目；② 只改了经验/权重（那不动正文）。
    有了它就不用为了补几个小文件跑一次 2 分钟的全量重建。
    """
    import kbviews as KV

    keys = [key] if key else KV.all_keys()
    if not keys:
        print("索引里没有条目。先点「更新索引（增量）」。")
        return 1
    print(f"生成分级视图：{len(keys)} 篇 → {S.VIEWS_DIR}")
    res = KV.write_many(keys, log=lambda m: print(m))
    print(f"完成：{res['written']}/{res['keys']} 篇")
    if res["failed"]:
        print(f"失败 {len(res['failed'])} 篇：")
        for f in res["failed"][:10]:
            print(f"  {f}")
        return 1
    # 顺手报一下三个级别各有多少篇有文件 —— 「图注与表格」本来就允许缺席
    for lv in KV.LEVELS:
        if lv["id"] not in KV.GENERATED_IDS:
            continue
        n = sum(1 for k in keys
                if os.path.exists(KV.level_path(lv["id"], k)))
        print(f"  {lv['label']}：{n}/{len(keys)} 篇"
              + ("（没有图注的文献本来就不会有这一份）"
                 if lv["id"] == "figures" else ""))
    return 0


def vacuum() -> int:
    conn = S.connect(S.INDEX_DB)
    before = os.path.getsize(S.INDEX_DB) / 1024 / 1024
    conn.execute("VACUUM")
    conn.close()
    after = os.path.getsize(S.INDEX_DB) / 1024 / 1024
    print(f"整理完成：{before:.1f} MB → {after:.1f} MB")
    return 0


# ---------------------------------------------------------------- 对账巡检
#
# 目标：把"知识库 vs Zotero 的差异"变成一条**只读报告**。
# 默认什么也不动；--apply 才动，而且**逐条二次确认**。
#
# ⚠ 本命令**不重复实现**删除/归档/还原 —— 那些是 offline/trash.py 的事
#   （convert.py 的删除对账也是调它）。这里只做发现、统计、展示；要动数据时
#   调 trash 的既有函数。经验层的失效引用**永远只提示**（那是用户手记）。

RECONCILE_TITLES = {
    1: "KB 有、Zotero 没有（真删 / 待清理）",
    2: "未处理的合并映射（item_merge 里 applied_at 为空）",
    3: "经验里的失效引用（只提示，绝不自动改）",
    4: "磁盘残留（没有对应 items 行的产物）",
    5: "kb/trash/ 归档占用（可回收空间）",
}
# --apply 真正能动的项：2 属后续任务（relink），3 是用户手记 —— 这两项永不改
RECONCILE_APPLICABLE = (1, 4, 5)
# Zotero 条目 key 的形状（8 位大写字母数字）。用它筛残留文件，避免误伤
# "README.md" 这种不是条目的东西。
_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")


def _human(n: int) -> str:
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1024 ** 2)
    if n >= 1024:
        return "%.0f KB" % (n / 1024)
    return "%d B" % n


def _walk_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _preview(seq, n: int = 8) -> str:
    items = [str(x) for x in seq]
    if not items:
        return "（无）"
    head = "、".join(items[:n])
    return head + ("……" if len(items) > n else "")


def _trash_mod():
    """offline/trash.py —— 归档/还原/彻底删除的**唯一实现**（别自己再写一套）。"""
    import trash as TR       # noqa: PLC0415
    return TR


def _zotero_keys():
    """Zotero 侧的条目集合：(live, trashed, err)。

    读不到（没装 Zotero / 数据目录不对 / 快照失败）时两个集合都是 None、err 非空
    —— 调用方据此把第 1 项标成"跳过"，而不是把整库当成"Zotero 里没有"。
    """
    rd = None
    try:
        import zreader
        rd = zreader.ZoteroReader()
        ids = rd.top_level_items()
        live = {it.key for it in rd.load_items(ids)}
        trashed = set(rd.trashed_keys or ())
        return live, trashed, ""
    except Exception as exc:        # noqa: BLE001 —— 可选依赖/数据缺失都按"读不到"
        return None, None, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if rd is not None:
            try:
                rd.close()
            except Exception:       # noqa: BLE001
                pass


def _disk_orphans(live: set) -> list:
    """没有对应 items 行的磁盘产物：views/<key>.*、papers/<key>.*、
    fulltext/<key>.*、mineru/<key>/。只认**像 Zotero key** 的名字。"""
    # ⚠ 用 S.kb_dir()（= dirname(INDEX_DB)）而不是模块常量 S.KB_DIR：
    #   它是"知识库在哪"的唯一事实定义，而且 trash.py 也按它找落点 ——
    #   两处用不同的口径，测试里改了 INDEX_DB 就会去扫**真实**知识库。
    kb = S.kb_dir()
    out: list = []
    for sub in ("views", "papers", "fulltext"):
        for path in sorted(glob.glob(os.path.join(kb, sub, "*"))):
            if not os.path.isfile(path):
                continue
            key = os.path.basename(path).split(".")[0]
            if key in live or not _KEY_RE.match(key):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            out.append({"key": key, "kind": sub, "path": path, "size": size})
    mdir = os.path.join(kb, "mineru")
    if os.path.isdir(mdir):
        for name in sorted(os.listdir(mdir)):
            path = os.path.join(mdir, name)
            if name in live or not os.path.isdir(path) or not _KEY_RE.match(name):
                continue
            out.append({"key": name, "kind": "mineru", "path": path,
                        "size": _walk_size(path)})
    return out


def reconcile_snapshot(conn) -> dict:
    """只读快照：trash 体积/条数 + 磁盘残留（面板那一行与第 4/5 项共用一份口径）。"""
    TR = _trash_mod()
    live = {r["key"] for r in conn.execute("SELECT key FROM items")}
    orphans = _disk_orphans(live)
    trash_keys = TR.archived_keys()
    return {
        "trash_keys": trash_keys,
        "trash_size": TR.trash_size(),
        "orphans": orphans,
        "orphan_keys": sorted({o["key"] for o in orphans}),
        "orphan_size": sum(o["size"] for o in orphans),
    }


def reconcile_summary_line(conn) -> str:
    """面板「知识库结构」页那一行：trash 体积 + 残留条数（永不抛）。"""
    try:
        snap = reconcile_snapshot(conn)
        return ("对账：kb\\trash\\ 归档 %d 条、共 %s　·　磁盘残留 %d 个 key / %d 个文件、共 %s"
                % (len(snap["trash_keys"]), _human(snap["trash_size"]),
                   len(snap["orphan_keys"]), len(snap["orphans"]),
                   _human(snap["orphan_size"])))
    except Exception as exc:        # noqa: BLE001 —— 面板显示不该因一项统计而崩
        return "对账：统计失败（%s: %s）" % (type(exc).__name__, exc)


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    """逐条二次确认。**非交互终端一律按"否"**（要真做必须显式 --yes）。"""
    if assume_yes:
        return True
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            print("      （不是交互终端：未确认，跳过这一条。"
                  "确实要执行请加 --yes）")
            return False
        ans = input("      %s [y/N] " % prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "是")


def _parse_apply(spec) -> set:
    """--apply 的取值：all/空 = 全部可动项；否则按逗号取项号。"""
    if spec is None:
        return set()
    text = str(spec).strip().lower()
    if text in ("", "all", "*"):
        return set(RECONCILE_APPLICABLE)
    out: set = set()
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def reconcile(apply=None, assume_yes: bool = False, as_json: bool = False) -> int:
    """与 Zotero 对账巡检。默认**只读**；apply 里列出的项才动，且逐条确认。

    五项见 RECONCILE_TITLES。第 2 项（合并映射的 relink）属后续任务、第 3 项
    （经验失效引用）是用户手记 —— 这两项**永不被 --apply 改动**，只报出来。
    """
    import experience as EX
    import keys as K
    TR = _trash_mod()

    report: dict = {"items": {}, "applied": [], "zotero_error": ""}
    conn = S.connect(S.INDEX_DB)
    try:
        kb_keys = {r["key"] for r in conn.execute("SELECT key FROM items")}
        live, trashed, zerr = _zotero_keys()
        report["zotero_error"] = zerr

        if not as_json:
            print("=" * 68)
            print("知识库 ↔ Zotero 对账巡检（默认只读；--apply 才会动数据）")
            print("=" * 68)
            print("  知识库条目：%d　Zotero：%s"
                  % (len(kb_keys),
                     ("读不到（%s）" % zerr) if zerr else "在线 %d 条" % len(live)))

        # ---- 1) KB 有、Zotero 没有
        to_delete = to_archive = []
        if zerr:
            report["items"]["1"] = {"error": zerr}
            if not as_json:
                print("\n  [1] %s" % RECONCILE_TITLES[1])
                print("      读不到 Zotero：%s" % zerr)
                print("      这一项只能跳过（Zotero 没装 / 数据目录不对时是正常的）")
        else:
            to_delete = sorted(kb_keys - live - trashed)
            to_archive = sorted(kb_keys & trashed)
            report["items"]["1"] = {"to_delete": to_delete,
                                    "to_archive": to_archive}
            if not as_json:
                print("\n  [1] %s" % RECONCILE_TITLES[1])
                print("      待彻底删除（Zotero 两处都没有）：%d 条" % len(to_delete))
                print("        %s" % _preview(to_delete))
                print("      待归档（Zotero 回收站里）：%d 条" % len(to_archive))
                print("        %s" % _preview(to_archive))

        # ---- 2) 未处理的合并映射
        rows = K.merge_rows(conn)
        pending = [r for r in rows if not r.get("applied_at")]
        report["items"]["2"] = {"total": len(rows), "pending": len(pending),
                                "rows": ["%s→%s" % (r["old_key"], r["new_key"])
                                         for r in pending]}
        if not as_json:
            print("\n  [2] %s" % RECONCILE_TITLES[2])
            print("      映射表共 %d 条，其中待处理 %d 条" % (len(rows), len(pending)))
            for r in pending[:10]:
                print("        %s → %s（记于 %s）"
                      % (r["old_key"], r["new_key"], r["seen_at"]))
            print("      （relink 与权重合并属后续任务；本命令只报，不动它）")

        # ---- 3) 经验里的失效引用（只提示）
        dead: list = []
        n_exp = 0
        for row in conn.execute("SELECT id, item_keys FROM experience ORDER BY id"):
            n_exp += 1
            ref = EX.item_refs(row["item_keys"], conn)
            for it in ref["items"]:
                if it["status"] != EX.REF_OK and it["status"] not in (EX.REF_CYCLE,
                                                                      EX.REF_ERROR):
                    dead.append({"exp": row["id"], "key": it["key"],
                                 "status": it["status"], "text": it["text"]})
        bad_exps = sorted({d["exp"] for d in dead})
        report["items"]["3"] = {"experiences": n_exp, "dead_keys": len(dead),
                                "bad_experiences": bad_exps,
                                "dead": dead}
        if not as_json:
            print("\n  [3] %s" % RECONCILE_TITLES[3])
            print("      经验 %d 条，其中 %d 条挂着失效引用（共 %d 个 key）"
                  % (n_exp, len(bad_exps), len(dead)))
            for d in dead[:12]:
                print("        #%s  %s" % (d["exp"], d["text"]))
            print("      （那是用户手记，只提示、绝不自动改）")

        # ---- 4/5) 磁盘残留与 trash 占用（面板那一行用同一个快照）
        snap = reconcile_snapshot(conn)
        orphans = snap["orphans"]
        report["items"]["4"] = {"count": len(orphans),
                                "size": snap["orphan_size"],
                                "keys": snap["orphan_keys"],
                                "by_kind": sorted({o["kind"] for o in orphans})}
        if not as_json:
            print("\n  [4] %s" % RECONCILE_TITLES[4])
            print("      残留 %d 个文件/目录，共 %s"
                  % (len(orphans), _human(snap["orphan_size"])))
            by_kind: dict = {}
            for o in orphans:
                by_kind.setdefault(o["kind"], []).append(o)
            for kind in sorted(by_kind):
                items = by_kind[kind]
                keys = sorted({i["key"] for i in items})
                print("        %-9s %d 个文件 / %d 个 key / %s　例：%s"
                      % (kind, len(items), len(keys),
                         _human(sum(i["size"] for i in items)),
                         _preview(keys, 6)))
            if not orphans:
                print("        （没有残留）")

        trash_keys = snap["trash_keys"]
        recyclable = []
        if live is not None:
            recyclable = [k for k in trash_keys if k not in live and k not in trashed]
        report["items"]["5"] = {"count": len(trash_keys), "size": snap["trash_size"],
                                "keys": trash_keys, "recyclable": recyclable}
        if not as_json:
            print("\n  [5] %s" % RECONCILE_TITLES[5])
            print("      归档 %d 条，共 %s" % (len(trash_keys),
                                              _human(snap["trash_size"])))
            for k in trash_keys[:20]:
                kind = (TR.tombstone(conn, k) or {}).get("kind") or "(无墓碑)"
                print("        %s  %s  墓碑 kind=%s"
                      % (k, _human(TR.archived_size(k)), kind))
            if not trash_keys:
                print("        （没有归档）")
            elif live is None:
                print("      读不到 Zotero，分不清哪些还能还原 —— 一律不动")
            else:
                print("      其中可回收（Zotero 里已没有这条）：%d 条 / %s"
                      % (len(recyclable),
                         _human(sum(TR.archived_size(k) for k in recyclable))))
                print("      其余 %d 条仍在 Zotero 里（进过回收站），随时可能还原，保留"
                      % (len(trash_keys) - len(recyclable)))

        # ---- --apply：按项执行，逐条确认
        want = _parse_apply(apply)
        if not want:
            if not as_json:
                print("\n" + "=" * 68)
                print("  只读报告结束（没有改动任何东西）。")
                print("  要处理某一项：--apply <项号>（逐条确认）或 --apply --yes")
                print("=" * 68)
            if as_json:
                print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0

        if not as_json:
            print("\n" + "-" * 68)

        for n in sorted(want):
            if n == 2 or n == 3:
                if not as_json:
                    extra = ("经验是用户手记，只提示 —— 本命令永不改它" if n == 3
                             else "relink 与权重合并属后续任务，本命令不动它")
                    print("  [项 %d] %s：%s" % (n, RECONCILE_TITLES.get(n), extra))
                continue
            if n == 1:
                if zerr:
                    if not as_json:
                        print("  [项 1] 读不到 Zotero，跳过")
                    continue
                acts = [("delete", k) for k in to_delete] \
                    + [("archive", k) for k in to_archive]
                if not acts:
                    if not as_json:
                        print("  [项 1] 没有要处理的条目")
                    continue
                if not as_json:
                    print("  [项 1] 有 %d 条要处理（逐条确认）：" % len(acts))
                for how, key in acts:
                    word = ("归档到 kb\\trash\\<key>\\（可从回收站还原）"
                            if how == "archive" else
                            "彻底删除（清归档目录、墓碑改 deleted）")
                    if not _confirm("%s：%s？" % (key, word), assume_yes):
                        if not as_json:
                            print("      跳过 %s" % key)
                        continue
                    res = (TR.archive(conn, key) if how == "archive"
                           else TR.delete_archived(conn, key))
                    ok = bool(res.get("ok"))
                    if not as_json:
                        print("      %s %s %s %s" % ("OK " if ok else "XX ", key,
                                                     how,
                                                     "" if ok else res.get("why")))
                    if ok:
                        report["applied"].append({"item": 1, "key": key,
                                                  "action": how})
                continue
            if n == 4:
                keys = snap["orphan_keys"]
                if not keys:
                    if not as_json:
                        print("  [项 4] 没有磁盘残留")
                    continue
                if not as_json:
                    print("  [项 4] 有 %d 个残留 key 要清理（逐条确认）：" % len(keys))
                for key in keys:
                    if not _confirm("%s：删除它的残留产物（views/papers/fulltext/"
                                    "mineru，不动别的东西）？" % key, assume_yes):
                        if not as_json:
                            print("      跳过 %s" % key)
                        continue
                    # 复用"条目还在、只是没有 PDF"的那条清理路径：
                    # 只删派生文件 + 派生行，**不留墓碑**（这些 key 本来就不在库里）
                    res = TR.discard_live(conn, key)
                    ok = bool(res.get("ok"))
                    if not as_json:
                        print("      %s %s %s" % ("OK " if ok else "XX ", key,
                                                  "" if ok else res.get("why")))
                    if ok:
                        report["applied"].append({"item": 4, "key": key,
                                                  "action": "discard"})
                continue
            if n == 5:
                if live is None:
                    if not as_json:
                        print("  [项 5] 读不到 Zotero，无法判断哪些还能还原 —— 不动")
                    continue
                if not recyclable:
                    if not as_json:
                        print("  [项 5] 没有可回收的归档（其余都还可能需要还原）")
                    continue
                if not as_json:
                    print("  [项 5] 有 %d 条可回收（Zotero 里已没有这条）：" %
                          len(recyclable))
                for key in recyclable:
                    if not _confirm("清理归档 %s（%s）？Zotero 里已彻底没有它，"
                                    "清掉不影响还原"
                                    % (key, _human(TR.archived_size(key))),
                                    assume_yes):
                        if not as_json:
                            print("      跳过 %s" % key)
                        continue
                    res = TR.delete_archived(conn, key)
                    ok = bool(res.get("ok"))
                    if not as_json:
                        print("      %s %s 释放 %s" % ("OK " if ok else "XX ", key,
                                                      _human(res.get("freed") or 0)))
                    if ok:
                        report["applied"].append({"item": 5, "key": key,
                                                  "action": "purge"})
                continue
            if not as_json:
                print("  [项 %d] 没有这一项（可动的是 %s）"
                      % (n, "、".join(str(x) for x in RECONCILE_APPLICABLE)))

        if not as_json:
            print("-" * 68)
            if report["applied"]:
                print("  已处理 %d 项：" % len(report["applied"]))
                for a in report["applied"]:
                    print("    项%s  %s  %s" % (a["item"], a["key"], a["action"]))
            else:
                print("  这一次没有改动任何数据。")
        if as_json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库维护")
    parser.add_argument("action",
                        choices=["check", "stats", "backup", "reindex-fst",
                                 "vacuum", "views", "reconcile"],
                        nargs="?", default="check")
    parser.add_argument("--key", default="",
                        help="views 用：只给这一篇补（默认全部）")
    parser.add_argument("--apply", nargs="?", const="all", default=None,
                        metavar="N",
                        help="reconcile 用：处理第 N 项（可逗号分隔，如 1,4）；"
                             "不带值 = 全部可动项。默认只读")
    parser.add_argument("--yes", action="store_true",
                        help="reconcile 用：跳过逐条确认（慎用；破坏性动作）")
    parser.add_argument("--json", action="store_true",
                        help="reconcile 用：输出 JSON（供面板/脚本消费）")
    args = parser.parse_args()
    if args.action == "check":
        return check()
    if args.action == "stats":
        return stats()
    if args.action == "backup":
        return backup()
    if args.action == "reindex-fst":
        return reindex_fst()
    if args.action == "views":
        return views(args.key)
    if args.action == "reconcile":
        return reconcile(apply=args.apply, assume_yes=args.yes, as_json=args.json)
    return vacuum()


if __name__ == "__main__":
    sys.exit(main())
