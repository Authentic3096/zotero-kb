"""逐篇重建 —— 重新解析指定文献的正文、重建切片与向量。

## 为什么需要它

全库重建要 155 秒，而**新文献通常就一两篇**，改一篇时没必要动全库。
更要紧的是：某篇的正文提取坏了（例如源 PDF 字体编码坏导致字符整体偏移），
**不会**改变 `items.version` —— 增量构建会认为"这篇没变化"直接跳过。
所以必须有一个能**强制重建指定条目**的入口。

## 与旧版 repair_chunks 的两点区别（都是判据被推翻后的修正）

1. **判据换了。** 旧版依据"坏切片数"（规则 + 4B 模型逐切片判好坏），
   实测在正常文本上误判约 60% —— 公式矩阵、参考文献、封面、中文正文
   全被打成坏；抽上轮判**好**的切片重判，仍有 25% 被判坏。
   现在用 `check_chunks` 的**文献级**健康度（全是客观量，见那里的说明）。

2. **不再强制走 PyMuPDF。** 旧版一律 `force_pymupdf=True`，当时的理由是
   "Zotero 缓存不可信"。现在 `zreader.fulltext_for` 自己会做缓存体检 +
   灰区模型仲裁 + **字符偏移还原**，绕过它反而可能拿到更差的文本：
   实测那篇偏移文献的 PyMuPDF 结果用 `\\x03` 当空格，比缓存还差，
   而它真正被修好靠的是偏移还原。

实际的重建动作交给 `convert.py --item`（那才是完整流程：正文、图注、
切片、向量、MANIFEST）。本模块只负责"挑哪几篇 + 判断能不能修 + 汇报"。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402

DEFAULT_MODEL = ""          # 兼容旧签名；模型选择现在由 convert 那边决定


def _load_reader():
    import zreader
    return zreader.ZoteroReader()


def _item_by_key(conn: sqlite3.Connection, key: str):
    """取条目（含 PDF 附件）。"""
    reader = _load_reader()
    try:
        row = reader.conn.execute("SELECT itemID FROM items WHERE key=?",
                                  (key,)).fetchone()
        if not row:
            return None, []
        items = reader.load_items([row[0]])
    finally:
        reader.close()
    if not items:
        return None, []
    item = items[0]
    return item, list(getattr(item, "pdfs", []) or [])


def _text_density(pages: list[str]) -> float:
    """每页多少个中文字 —— 判断是不是扫描件。

    ⚠ 不能用"总字数 < 500"当扫描件判据：本机有个 84 页的 PDF
      只提出 186 字（封面 + 目录），却因为"提出来了"被判成可修。
      按**每页密度**看才对：

        · 扫描件（纯图）：0 ~ 几个字/页
        · 正常文字 PDF：几百字/页
    """
    if not pages:
        return 0.0
    import re
    cjk = sum(len(re.findall(r"[\u4e00-\u9fff]", p or "")) for p in pages)
    return cjk / max(1, len(pages))


# 每页中文字少于这个数 → 认为没有可用文字层（扫描件）
SCANNED_DENSITY = 30.0


def _health_of(conn: sqlite3.Connection, key: str) -> tuple[str, list[str]]:
    """从 `item_health` 读这篇的健康结论；没查过就现算一次（纯规则，很快）。"""
    try:
        r = conn.execute("SELECT verdict, reasons FROM item_health "
                         "WHERE item_key=?", (key,)).fetchone()
        if r:
            try:
                return r["verdict"], json.loads(r["reasons"] or "[]")
            except Exception:                                # noqa: BLE001
                return r["verdict"], []
    except sqlite3.OperationalError:
        pass
    return "未检查", []


def inspect(keys: list[str] | None = None, limit: int = 0) -> dict:
    """先看一遍：哪些能修、哪些是扫描件修不了（**不改任何数据**）。

    给面板做"重建前预览"用 —— 用户该先知道会动哪几篇。
    """
    import check_chunks as CC

    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    try:
        CC.ensure_table(conn)
        if not keys:
            rows = conn.execute(
                "SELECT item_key FROM item_health WHERE verdict='bad' "
                "ORDER BY item_key").fetchall()
            keys = [r["item_key"] for r in rows]
        # ⚠ 没有不可信的文献就**直接返回空**，不要"退回全库现算一遍"。
        #   第一版为了"免得用户点了按钮什么都不发生"退回了全库扫描，
        #   结果每篇都要 `_load_reader()`（会把 zotero.sqlite 整个拷一份）
        #   —— 97 篇就是近百次拷贝，点一下按钮要等一两分钟。
        #   面板看到 total=0 会显示"没有需要重建的"，这才是对的反馈。
        if not keys:
            return {"ok": True, "total": 0, "fixable": 0, "items": []}
        if limit > 0:
            keys = keys[:limit]

        out = []
        for k in keys:
            title = ""
            tr = conn.execute("SELECT title FROM items WHERE key=?",
                              (k,)).fetchone()
            if tr:
                title = tr["title"] or ""
            verdict, reasons = _health_of(conn, k)
            if verdict == "ok":
                continue

            item, pdfs = _item_by_key(conn, k)
            if item is None:
                out.append({"key": k, "title": title, "verdict": verdict,
                            "reasons": reasons, "fixable": False,
                            "why": "索引里没有这个条目"})
                continue
            if not pdfs or not any(p.exists for p in pdfs):
                out.append({"key": k, "title": title, "verdict": verdict,
                            "reasons": reasons, "fixable": False,
                            "why": "找不到可读的 PDF 附件（网页剪藏没有 PDF）"})
                continue
            try:
                reader = _load_reader()
                try:
                    pages, src = reader.fulltext_for(item)
                finally:
                    reader.close()
            except Exception as exc:                         # noqa: BLE001
                out.append({"key": k, "title": title, "verdict": verdict,
                            "reasons": reasons, "fixable": False,
                            "why": f"提取失败：{exc}"})
                continue
            chars = sum(len(p) for p in pages)
            dens = _text_density(pages)
            scanned = dens < SCANNED_DENSITY and chars < 3000
            out.append({
                "key": k, "title": title, "verdict": verdict,
                "reasons": reasons,
                "new_chars": chars, "new_pages": len(pages), "source": src,
                "density": round(dens, 1),
                "fixable": not scanned,
                "why": (f"PDF 没有文字层（{dens:.1f} 中文字/页，扫描件）"
                        f"—— 要 OCR 才能救" if scanned
                        else f"重新提取可得 {chars} 字 / {len(pages)} 页"
                             f"（来源 {src or '无'}）"),
            })
    finally:
        conn.close()
    fixable = [x for x in out if x.get("fixable")]
    return {"ok": True, "total": len(out), "fixable": len(fixable),
            "items": out}


def repair(keys: list[str] | None = None, limit: int = 0,
           progress=None, **_ignored) -> dict:
    """真的重建：对能修的条目重新解析 + 重建切片 + 重算向量。

    `**_ignored` 是给旧调用方留的（旧版有 model / use_model_judge /
    judge_model 这几个参数，现在模型选择由 `fulltext_for` 与健康检查
    自己决定，不再从这里传）。

    返回结构与旧版一致（fixed / failed / skipped / bad_before / bad_after /
    details），面板不用改。
    """
    import check_chunks as CC
    import convert

    t0 = time.time()
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    CC.ensure_table(conn)
    if not keys:
        rows = conn.execute("SELECT item_key FROM item_health "
                            "WHERE verdict='bad' ORDER BY item_key").fetchall()
        keys = [r["item_key"] for r in rows]
    conn.close()

    plan = inspect(keys, limit=limit)
    todo = [x["key"] for x in plan["items"] if x.get("fixable")]
    skipped = [x for x in plan["items"] if not x.get("fixable")]

    if not todo:
        return {"ok": True, "fixed": 0, "failed": 0, "skipped": len(skipped),
                "bad_before": len(skipped), "bad_after": len(skipped),
                "elapsed": round(time.time() - t0, 1), "details": [],
                "skipped_detail": skipped,
                "note": "没有可重建的条目" + ("（都不可修）" if skipped else "")}

    if progress:
        progress(0, len(todo), f"重建 {len(todo)} 篇…")

    # ---- 真正的重建：交给 convert 的完整流程（正文、图注、切片、向量、清单）
    ns = argparse.Namespace(
        full=False, no_vectors=False, limit=0,
        embed_model=convert.DEFAULT_EMBED_MODEL,
        item=list(todo),
        figures=False, no_figures=False,
        # 逐篇重建是"用户明确要求修这一篇"，走 deep：两条路都跑 + 模型仲裁，
        # 慢一点没关系（一篇几秒），但要在缓存和 PyMuPDF 之间做对选择。
        cache_check="deep",
    )
    buf = io.StringIO()
    err = ""
    mf: dict = {}
    try:
        with contextlib.redirect_stdout(buf):
            rc, mf = convert.build(ns)
    except Exception as exc:                                 # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"

    log_txt = buf.getvalue()
    if err:
        return {"ok": False, "fixed": 0, "failed": len(todo), "skipped": len(skipped),
                "bad_before": len(todo), "bad_after": len(todo),
                "elapsed": round(time.time() - t0, 1),
                "details": [{"key": k, "ok": False, "why": err} for k in todo],
                "skipped_detail": skipped, "log": log_txt[-2000:]}

    # ---- 验收：重跑健康检查（纯规则，秒级）
    after = CC.scan(use_model=False, item_key="", limit=0)
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    details = []
    still_bad = 0
    for k in todo:
        r = conn.execute("SELECT verdict, reasons FROM item_health "
                         "WHERE item_key=?", (k,)).fetchone()
        v = r["verdict"] if r else "?"
        try:
            rs = json.loads(r["reasons"] or "[]") if r else []
        except Exception:                                    # noqa: BLE001
            rs = []
        if v == "bad":
            still_bad += 1
        src = ""
        sr = conn.execute("SELECT fulltext_src, fulltext_chars FROM items "
                          "WHERE key=?", (k,)).fetchone()
        if sr:
            src = f"{sr['fulltext_src'] or '无'} / {sr['fulltext_chars']} 字"
        details.append({"key": k, "ok": v != "bad", "verdict": v,
                        "reasons": rs, "src": src,
                        "why": "" if v != "bad" else "；".join(rs)[:120]})
    conn.close()

    fixed = [d for d in details if d["ok"]]
    if progress:
        progress(len(todo), len(todo), "完成")
    deshifted = [x["key"] for x in (mf.get("fulltext", {}).get("deshifted") or [])]
    return {
        "ok": True,
        "fixed": len(fixed),
        "failed": len(details) - len(fixed),
        "skipped": len(skipped),
        "bad_before": len(todo),
        "bad_after": still_bad,
        "deshifted": deshifted,
        "elapsed": round(time.time() - t0, 1),
        "details": details,
        "skipped_detail": skipped,
        "note": (f"其中 {len(deshifted)} 篇做了字符偏移还原" if deshifted else ""),
        "log": log_txt[-2000:],
    }


def detail(key: str) -> dict:
    """某一篇的现状 + 重建建议（面板"看明细"用）。"""
    import check_chunks as CC

    d = CC.detail(key)
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    sr = conn.execute("SELECT fulltext_src, fulltext_chars, n_pdfs FROM items "
                      "WHERE key=?", (key,)).fetchone()
    conn.close()
    if sr:
        d["fulltext_src"] = sr["fulltext_src"] or ""
        d["fulltext_chars"] = sr["fulltext_chars"]
        d["n_pdfs"] = sr["n_pdfs"]
    return d


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="逐篇重建：重新解析指定文献的正文、重建切片与向量")
    ap.add_argument("keys", nargs="*", help="条目 key（不传则用健康检查判坏的）")
    ap.add_argument("--limit", type=int, default=0, help="最多重建几篇")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不改数据")
    args = ap.parse_args(argv)

    if args.dry_run:
        p = inspect(args.keys or None, limit=args.limit)
        print("=" * 74)
        print(f"预览：{p['total']} 篇需要重建，其中可修 {p['fixable']} 篇")
        print("=" * 74)
        for x in p["items"][:30]:
            flag = "可修" if x.get("fixable") else "跳过"
            print(f"\n  [{flag}] {x['key']}  {(x.get('title') or '')[:44]}")
            print(f"        结论：{x.get('verdict')}  {'；'.join(x.get('reasons') or [])}")
            print(f"        {x.get('why')}")
        return 0

    def prog(done, total, note):
        print(f"\r  进度 {done}/{total}  {note[:44]:44}", end="")

    r = repair(args.keys or None, limit=args.limit, progress=prog)
    print("\r" + " " * 74 + "\r", end="")
    print("=" * 74)
    print(f"重建完成：成功 {r['fixed']} 篇，仍不可信 {r['failed']} 篇，"
          f"跳过 {r['skipped']} 篇，耗时 {r['elapsed']} 秒")
    print("=" * 74)
    if r.get("note"):
        print(f"  {r['note']}")
    for d in r["details"][:20]:
        mark = "✓" if d["ok"] else "✗"
        print(f"  {mark} {d['key']:12} {d.get('verdict', ''):6} {d.get('src', '')}")
        if not d["ok"] and d.get("why"):
            print(f"      {d['why']}")
    for x in r["skipped_detail"][:10]:
        print(f"  - {x['key']:12} 跳过：{x.get('why')}")
    return 0 if not r["failed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
