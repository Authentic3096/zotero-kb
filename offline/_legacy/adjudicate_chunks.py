"""对"规则判不准"的条目，用模型决定要不要替换。

## 为什么需要这一步

修复流程里"新文本是否更好"的判据，规则判**不准**：

  · `ZNAKFIM2` 用 PyMuPDF 重抽后是**正常中文**
    （`第3章误差鲁棒的校准系数估计算法 ... (3-26) 令代价函数J 为`），
    但公式、图表标注被 PyMuPDF 提取成一堆碎片
    （`k\nk\nk\nP\nK A P`），规则的"碎片/符号为主"判据就把它算成坏片了
    → 坏片**比例** 20%→44%，被误判成"没改善"。
  · 而真正该跳过的扫描件是**一个字都提不出来**的。

规则分不清"公式碎片（有价值，该保留）"和"乱码（该跳过）"，
**模型分得清**（本机实测单条判定 9/9 全对）。

## 做法

对候选条目：
  1. 用 `force_pymupdf` 重新提取
  2. **问模型**："新提取的这段内容比旧的更可用吗？"——给两边各一段样例
  3. 模型说"新更好"就替换，否则保持原样

## ⚠ 这一轮踩的坑（都记下来）

1. 判据先用了**坏片数量** —— Zotero 缓存的乱码"规则看着不坏"
   （它只是乱，不短），于是"新的 49 > 原来的 24"被误判成没改善。
2. 改成**坏片比例** —— 又被公式碎片拉高，同样误判。
3. 根因是**规则本身分不清公式碎片与乱码** —— 该问模型的时候不要硬凑规则。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import schemas as S
import repair_chunks as R

PROMPT = """下面同一篇文献有两份文本，来自同一个 PDF 的两种提取方式。

【A：Zotero 的文本缓存】
{old}

【B：用 PyMuPDF 重新提取】
{new}

问：**哪一份更可用**（更适合做全文检索）？

判断要点：
- 如果一份是**乱码**（字符错位、混入不该出现的字符、读不出句子），另一份正常 → 选正常的那份
- 如果一份**只有一个封面/几行字**（扫描件提取不出正文），另一份有完整正文 → 选完整的那份
- 如果两份都正常，只是 B 里公式/图表被拆成了碎片 → **仍然算正常**
  （公式碎片含有效信息，检索能用），此时选 B（它内容更全）
- 如果两份都不可用（都是乱码，或都提不出正文）→ 选 none

只输出 JSON：{{"better": "A" 或 "B" 或 "none", "why": "一句话"}}"""


def _sample(pages: list[str], n: int = 3) -> str:
    """从正文里取几页做样例（跳过封面那种没信息量的页）。"""
    if not pages:
        return "(空)"
    body = [p for p in pages if len(p or "") > 200] or pages
    step = max(1, len(body) // n)
    picked = [body[i] for i in range(0, len(body), step)][:n]
    return "\n---\n".join((p or "")[:500] for p in picked)


def decide(old_pages: list[str], new_pages: list[str],
           model: str = "") -> tuple[str, str]:
    """问模型：新提取的比旧的好吗？返回 (better, why)。

    ⚠ `judge.generate` 返回 dict、要配 `json_mode=True` + `judge.parse_json`。
      这里第一版写乱了（同一个 prompt 赋值两次、参数名还混用
      `item.fulltext_pages`），重写清楚：两个样例都从参数来。
    """
    import judge

    prompt = (PROMPT.replace("{old}", _sample(old_pages))
                    .replace("{new}", _sample(new_pages)))
    r = judge.generate(prompt, model=model, json_mode=True, temperature=0.0)
    if not isinstance(r, dict) or not r.get("ok"):
        return "none", f"模型调用失败：{(r or {}).get('error')}"
    ok, data = judge.parse_json(r.get("text") or "")
    if not ok or not isinstance(data, dict):
        return "none", f"模型没返回 JSON：{str(r.get('text'))[:60]}"
    return (str(data.get("better") or "none").upper(),
            str(data.get("why") or "")[:80])


def run(keys: list[str] | None = None, limit: int = 0,
        model: str = "", progress=None) -> dict:
    """对候选条目用模型裁决 + 替换。"""
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    R.ensure_table(conn)
    if not keys:
        rows = conn.execute(
            "SELECT item_key FROM chunk_quality WHERE verdict='bad' "
            "GROUP BY item_key ORDER BY COUNT(*) DESC").fetchall()
        keys = [r["item_key"] for r in rows]
    if limit > 0:
        keys = keys[:limit]

    plan = R.inspect(keys)
    cands = [x for x in plan["items"]
             if x.get("fixable") and x.get("bad_now", 0) > 0]
    results = []
    t0 = __import__("time").time()
    import converter as C
    import numpy as np
    from fastembed import TextEmbedding

    embedder = None
    for i, c in enumerate(cands):
        key = c["key"]
        if progress:
            progress(i, len(cands), f"裁决 {key}…")
        item, pdfs = R._item_by_key(conn, key)
        if item is None:
            continue
        # 旧内容 = 库里现在的切片
        old_rows = conn.execute(
            "SELECT text FROM chunks WHERE item_key=? ORDER BY seq LIMIT 20",
            (key,)).fetchall()
        old_pages = [r["text"] for r in old_rows]
        # 新内容
        reader = R._load_reader()
        try:
            new_pages, src = reader.fulltext_for(item, force_pymupdf=True)
        except Exception as exc:  # noqa: BLE001
            results.append({"key": key, "action": "跳过", "why": str(exc)[:60]})
            continue
        if not new_pages:
            results.append({"key": key, "action": "跳过",
                            "why": "提不出正文"})
            continue
        item.fulltext_pages = new_pages
        better, why = decide(old_pages, new_pages, model)
        if better != "B":
            results.append({"key": key, "action": "保持原样",
                            "why": f"模型选 {better}：{why}"})
            continue
        # ---- 替换
        new_chunks = list(C.collect_chunks(item, new_pages))
        if not new_chunks:
            results.append({"key": key, "action": "跳过", "why": "切不出切片"})
            continue
        if embedder is None:
            embedder = TextEmbedding(R.DEFAULT_MODEL)
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                         "(SELECT chunk_id FROM chunks WHERE item_key=?)",
                         (key,))
            conn.execute("DELETE FROM embeddings WHERE chunk_id IN "
                         "(SELECT chunk_id FROM chunks WHERE item_key=?)",
                         (key,))
            conn.execute("DELETE FROM chunk_quality WHERE item_key=?", (key,))
            conn.execute("DELETE FROM chunks WHERE item_key=?", (key,))
            ids = []
            for ch in new_chunks:
                cur = conn.execute(
                    "INSERT INTO chunks(item_key,page,seq,text,n_chars) "
                    "VALUES(?,?,?,?,?)",
                    (key, ch["page"], ch["seq"], ch["text"], len(ch["text"])))
                ids.append(cur.lastrowid)
                conn.execute(
                    "INSERT INTO chunks_fts(rowid,text) VALUES(?,?)",
                    (cur.lastrowid, ch["text"]))
            conn.execute("UPDATE items SET fulltext_chars=?, fulltext_src=?, "
                         "built_at=datetime('now') WHERE key=?",
                         (sum(len(p) for p in new_pages), "pymupdf", key))
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            results.append({"key": key, "action": "失败", "why": str(exc)[:60]})
            continue
        vecs = list(embedder.embed([ch["text"] for ch in new_chunks]))
        dim = len(vecs[0]) if vecs else 0
        for cid, v in zip(ids, vecs):
            arr = np.asarray(v, dtype=np.float32)
            conn.execute("INSERT OR REPLACE INTO embeddings"
                         "(chunk_id,dim,vector,model) VALUES(?,?,?,?)",
                         (cid, dim, arr.tobytes(), R.DEFAULT_MODEL))
        conn.commit()
        # 重写 Markdown（kb_fulltext 读的是文件，不是库）
        try:
            with open(item.fulltext_path(), "w", encoding="utf-8") as fh:
                fh.write(C.fulltext_markdown(item, new_pages))
            with open(item.paper_path(), "w", encoding="utf-8") as fh:
                fh.write(C.item_markdown(item, new_pages, "pymupdf", conn))
        except Exception:  # noqa: BLE001
            pass
        bad_before = c.get("bad_now", 0)
        results.append({"key": key, "action": "已替换",
                        "why": why, "bad_before": bad_before,
                        "chunks": len(new_chunks),
                        "title": (c.get("title") or "")[:40]})
    if progress:
        progress(len(cands), len(cands), "完成")
    conn.close()
    replaced = [r for r in results if r["action"] == "已替换"]
    return {"ok": True, "candidates": len(cands),
            "replaced": len(replaced), "results": results,
            "elapsed": round(__import__("time").time() - t0, 1)}


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="用模型裁决切片要不要替换")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--item", default="")
    ap.add_argument("--model", default="")
    args = ap.parse_args(argv)
    keys = [args.item] if args.item else None
    print("=" * 74)
    print("模型裁决：重新提取的内容该不该替换（规则判不准的情形）")
    print("=" * 74)

    def prog(done, total, note):
        print(f"\r  {done}/{total} {note[:50]:50}", end="")

    r = run(keys, args.limit, args.model, progress=prog)
    print("\r" + " " * 78 + "\r", end="")
    print(f"  候选 {r['candidates']} 篇，替换 {r['replaced']} 篇，"
          f"耗时 {r['elapsed']} 秒\n")
    for x in r["results"]:
        mark = {"已替换": "✓", "保持原样": "—", "跳过": "·",
                "失败": "✗"}.get(x["action"], "?")
        print(f"  {mark} {x['key']:12} {x['action']:6} "
              f"{(x.get('title') or '')[:26]:28} {x.get('why', '')[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
