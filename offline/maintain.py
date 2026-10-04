"""维护：环境自检、重建、备份、统计。

    python offline/maintain.py check      # 环境自检（依赖 / 索引 / Zotero / Ollama）
    python offline/maintain.py stats      # 库规模与经验统计
    python offline/maintain.py backup     # 备份 index.db 与经验层
    python offline/maintain.py reindex-fst   # 只重建 FTS 与词元表（不动向量）
    python offline/maintain.py vacuum     # 整理数据库

自检是给"换机器 / 隔了几个月回来"用的：它会明确指出哪一环断了、怎么修。
"""

from __future__ import annotations

import argparse
import json
import os
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


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库维护")
    parser.add_argument("action",
                        choices=["check", "stats", "backup", "reindex-fst",
                                 "vacuum", "views"],
                        nargs="?", default="check")
    parser.add_argument("--key", default="",
                        help="views 用：只给这一篇补（默认全部）")
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
    return vacuum()


if __name__ == "__main__":
    sys.exit(main())
