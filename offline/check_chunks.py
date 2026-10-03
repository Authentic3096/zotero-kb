"""文献级解析健康检查 —— 看看每篇入库的正文提取**整体**是否可信。

## 为什么是文献级，而不是切片级（一次被数据推翻的重设计）

第一版是"逐切片判好坏"：3681 个切片，规则 + 4B 模型跑一遍，判坏 2223 个（60%）。
逐层排查后发现**判据本身不成立**：

  · 判坏的"坏切片"里大量是**正常正文**：
      中文正文 `合 Adam 优化器,对某物理量进行高效且精确的求解`
      封面     `某学位论文标题 / 作者姓名:某某某`
      参考文献 `[20] G. Author, "Performance measures in ...`
      公式矩阵 `⎡BX BY BZ / aX0 aX1 aX2⎦ (5) where`

    ⚠ 上面四条是**格式示例**（把真实词换成了占位词）。这里原来放的是本机
      库里真实切片的原文，而本文件是要公开的 —— 真实标题 + 作者姓名写在
      注释里，等于把用户的研究方向和一篇具体文献的作者一起写出来了。
      加/改这类示例时，一律用占位词。
  · 对照实验（同一批切片、同一模型、**只换取样方式**）：
    上轮判**好**的 90 个切片重判下来仍有 25% 被判坏
    —— 同一模型对同一文本给出相反结论，说明判定本身不稳定，跟取样无关
  · 判据里"表格碎渣""只剩符号"这类条目，本来就会把公式、参考文献、表格
    这些**正常学术文本形态**全打成坏
  · 判坏率按来源看完全一样（zotero-cache 60% / pymupdf 61%）
    —— 不是"某一类来源坏了"，是判据坏了

反过来看**真正坏的**那一篇（源 PDF 字体没有可用的 ToUnicode 映射，
提取出来每个字符偏移 +29：`&DOFXODWLRQ DQG 6LPXODWLRQ`），
它的坏是**整篇**的 —— 全篇都坏。全库 97 篇有正文的文献里，
客观判据下只有这 1 篇有问题。

结论：**提取失败是文献级现象**，判定就该在文献级做（97 次而不是 3681 次），
判据也该用客观量，不用"读着像不像句子"这种主观标准。

## 判据（全部可计算、可复现）

    empty      正文过短（< 200 字）—— 扫描件没有文本层
    exotic     异域码位占比 > 10%（藏文/天城文…）—— 字体映射错
    cjk_lost   非外文文献但中文 < 0.5% —— 中文全丢
    shifted    字符偏移，且**入库时没被还原掉**（见 zreader._deshift_fix）
    common     外文文献但常见词率 < 12% —— 读不成句，交模型看
    ok         其余

前四条由 `zreader.cache_verdict` + `schemas.shift_fix` 提供，都是确定性的；
只有第五条（灰区）才调本地模型，而且**一次只看一篇**（整篇均匀取样 4 段），
问一个二选一的问题。

## 结果落在哪

`item_health` 表（一篇一行）。`searcher` 读它给整篇降权
—— 一篇的提取若不可信，它的每个切片都不可信。

⚠ 旧的 `chunk_quality` 表会被删掉：它那 7420 行全是上面那套误判的产物，
留着只会继续误导排序。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime

import schemas as S

# 模型的取样：整篇均匀取 4 段，每段 500 字符。
# 为什么不看开头：论文开头是标题页/摘要，代表不了正文；而且开头常有
# 卷期页码这类零碎内容，容易把模型带偏。
SAMPLE_N = 4
SAMPLE_SIZE = 500

# 外文文献常见词率低于此值 → 灰区，交模型
COMMON_SUSPECT = 0.12
# 正文短于此值直接判不可用
MIN_CHARS = 200


# ---------------------------------------------------------------- 判据

def item_health(text: str) -> tuple[str, list[str]]:
    """一篇文献的解析健康度。返回 (verdict, reasons)，verdict: ok|bad|unsure。

    ⚠ 判据只抓"整篇提取失败"，**不碰**"这一段看着零碎" ——
      公式、表格、参考文献、封面零碎是正常的。
    """
    from zreader import cache_verdict

    t = text or ""
    st = S.char_stats(t)
    if st["total"] < MIN_CHARS:
        return "bad", [f"正文过短（有效字符 {st['total']}）"]

    # 前四条确定性判据复用缓存体检那套 —— 它判的本来就是"这段文本像不像
    # 正常提取结果"，与文本来自缓存还是 PyMuPDF 无关。
    v, why = cache_verdict([t])
    if v == "bad":
        return "bad", [why or "缓存体检判坏"]

    # 字符偏移：入库时 `zreader._deshift_fix` 应该已经还原过了。
    # 这里还能测出来，说明当时没救成功（或那一步被关掉了）→ 该重建。
    sh, _ = S.shift_fix(t)
    if sh:
        return "bad", [f"字符偏移 {sh:+d} 未还原（源 PDF 字体编码坏，可重建修复）"]

    if v == "unsure":
        return "unsure", [why or "缓存体检落在灰区"]

    # 外文文献但读不成句 —— 这一条规则不敢定，交模型
    ratio, n = S.en_common_ratio(t)
    if st["latin_ratio"] >= S.SHIFT_MIN_LATIN and n >= S.SHIFT_MIN_WORDS:
        if ratio < COMMON_SUSPECT:
            return "unsure", [f"常见词率仅 {ratio:.1%}（外文文献，规则不敢定）"]
    return "ok", []


def _sample(text: str, n: int = SAMPLE_N, size: int = SAMPLE_SIZE) -> str:
    """从整篇均匀取 n 段 —— 比只看开头有代表性得多。"""
    t = text or ""
    if len(t) <= n * size:
        return t
    step = len(t) // n
    return "\n\n".join(f"[片段 {i + 1}]\n{t[i * step:i * step + size]}"
                       for i in range(n))


ITEM_PROMPT = """下面是**同一篇论文**正文的若干片段（从全文不同位置均匀抽取）。

请判断：这篇论文的正文提取是否**整体失败**了？

只有下面几种情况算「整体失败」：
- **字符整体错乱**：英文字母像被整体位移过（`&DOFXODWLRQ` 本该是 `Calculation`）
- **编码错乱**：混入藏文、天城文等不该出现的字符
- **中文全丢**：中文文献里中文几乎没了，只剩数字、标点、孤立字母

⚠⚠ 下面这些**都不算失败**，不要判失败：
- 公式、矩阵、符号、上下标（论文里本来就有）
- 参考文献列表（本来就零碎、编号多）
- 表格内容（本来就是孤立单词和数字）
- 封面、目录、页眉页脚、基金信息
- 某一段的开头或结尾不完整（切片/分页造成的）

拿不准时判**没失败** —— 宁可放过，也不要误伤正常文献。

{fragments}

只输出 JSON：{{"failed": true 或 false, "reason": "一句话理由"}}"""


def judge_item(text: str, model: str = "") -> tuple[bool, str]:
    """让模型判"整篇提取是否失败"。返回 (failed, reason)。

    ⚠ `judge.generate` 返回 **dict**（含 ok/text/model/backend/eval_count），
      不是字符串 —— 这个坑踩过不止一次。
    """
    import judge

    r = judge.generate(ITEM_PROMPT.replace("{fragments}", _sample(text)),
                       model=model, json_mode=True, temperature=0.0)
    if not isinstance(r, dict) or not r.get("ok"):
        err = (r or {}).get("error") if isinstance(r, dict) else "返回值不是 dict"
        return False, f"模型调用失败：{err}"
    ok, data = judge.parse_json(r.get("text") or "")
    if not ok or not isinstance(data, dict):
        return False, f"模型输出不是 JSON：{str(r.get('text'))[:60]}"
    failed = data.get("failed")
    if isinstance(failed, str):
        failed = failed.strip().lower() in ("true", "1", "yes", "是")
    return bool(failed), str(data.get("reason") or "")


# ---------------------------------------------------------------- 落库

def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS item_health (
            item_key   TEXT PRIMARY KEY,
            verdict    TEXT,      -- ok | bad | unsure
            reasons    TEXT,      -- JSON 数组
            n_chars    INTEGER,
            checked_at TEXT,
            checker    TEXT       -- rule | model | rule+model
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ih_verdict "
                 "ON item_health(verdict)")
    conn.commit()


def drop_legacy(conn: sqlite3.Connection) -> int:
    """删掉旧的 `chunk_quality` 表。返回删掉的行数（0 = 本来就没有）。

    为什么要删而不是留着：那 7420 行是"逐切片判好坏"那套误判的产物
    （判坏的 2223 个切片里大量是正常正文）。留着它，任何读它的代码
    （旧的 repair_chunks / 面板）都会继续按错误结论行事。
    """
    try:
        n = conn.execute("SELECT COUNT(*) FROM chunk_quality").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    conn.execute("DROP TABLE chunk_quality")
    conn.commit()
    return int(n or 0)


def _item_text(conn: sqlite3.Connection, key: str) -> str:
    """从**已入库的 chunks** 拼出全文 —— 审计的对象就是入库的东西。"""
    return "".join(r["text"] for r in conn.execute(
        "SELECT text FROM chunks WHERE item_key=? ORDER BY page, seq", (key,)))


def scan(use_model: bool = True, item_key: str = "", limit: int = 0,
         progress=None, model: str = "") -> dict:
    """扫一遍文献级健康度。返回统计。

    `progress`: 可选回调 `(done, total, note)`，给面板显示进度用。

    ⚠ 全库只要几秒（97 篇），所以没有"增量"这个概念 —— 每次全扫，
      结果按 item_key 覆盖写。这也是它比旧切片级检查好的地方之一
      （旧的 3681 个切片要跑 7.5 分钟，才不得不做增量）。
    """
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    legacy = drop_legacy(conn)
    # 清掉**已经没有正文**的条目的体检行：用户删掉 PDF 附件后，构建会
    # 跳过它并清掉它的切片，但 `item_health` 里那一行不会自己消失 ——
    # 留着会让面板显示一个点进去查不到正文的"不可信文献"。
    stale = conn.execute(
        "DELETE FROM item_health WHERE item_key NOT IN "
        "(SELECT DISTINCT item_key FROM chunks)").rowcount
    if stale:
        conn.commit()

    sql = ("SELECT key, title FROM items WHERE EXISTS "
           "(SELECT 1 FROM chunks c WHERE c.item_key = items.key)")
    args: list = []
    if item_key:
        sql += " AND key = ?"
        args.append(item_key)
    sql += " ORDER BY key"
    rows = conn.execute(sql, args).fetchall()
    if limit > 0:
        rows = rows[:limit]
    total = len(rows)
    if not total:
        conn.close()
        return {"ok": True, "total": 0, "bad": 0, "unsure": 0, "elapsed": 0.0,
                "legacy_dropped": legacy, "stale_dropped": stale,
                "note": "没有可检查的文献（库是空的？）"}

    t0 = time.time()
    counts = {"ok": 0, "bad": 0, "unsure": 0}
    bad_items: list[dict] = []
    model_note = ""
    now = datetime.now().isoformat(timespec="seconds")

    for i, r in enumerate(rows, start=1):
        key = r["key"]
        text = _item_text(conn, key)
        verdict, reasons = item_health(text)
        checker = "rule"
        if verdict == "unsure" and use_model:
            failed, why = judge_item(text, model=model)
            if why.startswith("模型调用失败") or why.startswith("模型输出"):
                # 模型挂了：不假装查过 —— 保持 unsure，把原因记下来
                model_note = why
                reasons.append(why)
            else:
                checker = "rule+model"
                if failed:
                    verdict = "bad"
                else:
                    verdict = "ok"
                    reasons = []
                if why:
                    reasons.append(f"模型：{why}")
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "bad":
            bad_items.append({"key": key, "title": r["title"] or "",
                              "reasons": reasons})
        conn.execute(
            "INSERT OR REPLACE INTO item_health"
            "(item_key, verdict, reasons, n_chars, checked_at, checker)"
            " VALUES(?,?,?,?,?,?)",
            (key, verdict, json.dumps(reasons, ensure_ascii=False),
             len(text), now, checker))
        conn.commit()
        if progress:
            progress(i, total, f"{(r['title'] or key)[:36]}")

    conn.close()
    elapsed = time.time() - t0
    return {
        "ok": True, "total": total, "bad": counts["bad"],
        "unsure": counts["unsure"], "healthy": counts["ok"],
        "elapsed": round(elapsed, 1), "model_note": model_note,
        "legacy_dropped": legacy, "stale_dropped": stale, "items": bad_items,
        "per_sec": round(total / max(0.01, elapsed), 1),
    }


def report(limit: int = 200) -> dict:
    """按文献汇总健康度，供面板/工具展示。"""
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    try:
        ensure_table(conn)
        rows = conn.execute(
            "SELECT h.item_key, h.verdict, h.reasons, h.n_chars, h.checker,"
            "       i.title "
            "FROM item_health h LEFT JOIN items i ON i.key = h.item_key "
            "WHERE h.verdict <> 'ok' ORDER BY h.verdict DESC, h.item_key "
            "LIMIT ?", (limit,)).fetchall()
        total_bad = conn.execute(
            "SELECT COUNT(*) FROM item_health WHERE verdict='bad'").fetchone()[0]
        total_chk = conn.execute(
            "SELECT COUNT(*) FROM item_health").fetchone()[0]
        checked_at = conn.execute(
            "SELECT MAX(checked_at) FROM item_health").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        return {"ok": False, "error": "还没跑过检查"}
    conn.close()
    items = []
    for r in rows:
        try:
            reasons = json.loads(r["reasons"] or "[]")
        except Exception:                                    # noqa: BLE001
            reasons = []
        items.append({"key": r["item_key"], "title": r["title"] or "",
                      "verdict": r["verdict"], "reasons": reasons,
                      "n_chars": r["n_chars"], "checker": r["checker"]})
    return {"ok": True, "total_bad": total_bad, "total_checked": total_chk,
            "checked_at": checked_at, "items": items}


def detail(item_key: str, limit: int = 3) -> dict:
    """某篇的健康度明细（带正文片段，供人工判断要不要重建）。"""
    conn = sqlite3.connect(S.INDEX_DB)
    conn.row_factory = sqlite3.Row
    try:
        ensure_table(conn)
        r = conn.execute(
            "SELECT h.*, i.title FROM item_health h "
            "LEFT JOIN items i ON i.key = h.item_key WHERE h.item_key = ?",
            (item_key,)).fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return {"ok": False, "error": "还没跑过检查"}
    if not r:
        conn.close()
        return {"ok": False, "error": f"{item_key} 不在检查结果里"}
    try:
        reasons = json.loads(r["reasons"] or "[]")
    except Exception:                                        # noqa: BLE001
        reasons = []
    text = _item_text(conn, item_key)
    conn.close()
    return {"ok": True, "item_key": item_key, "title": r["title"] or "",
            "verdict": r["verdict"], "reasons": reasons,
            "n_chars": r["n_chars"], "checker": r["checker"],
            "samples": [text[i * 400:i * 400 + 400] for i in range(limit)]
            if text else []}


def bad_item_keys() -> set[str]:
    """提取不可信的文献 key 集合 —— `searcher` 给它们整篇降权用。"""
    try:
        conn = sqlite3.connect(S.INDEX_DB)
        rows = conn.execute(
            "SELECT item_key FROM item_health WHERE verdict='bad'").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="文献级解析健康检查（入库的正文提取整体是否可信）")
    ap.add_argument("--rule-only", action="store_true",
                    help="只用规则，不调模型（秒级）")
    ap.add_argument("--item", default="", help="只查某篇（条目 key）")
    ap.add_argument("--limit", type=int, default=0, help="最多查多少篇")
    ap.add_argument("--model", default="", help="指定模型")
    ap.add_argument("--report", action="store_true", help="只看已有结果")
    ap.add_argument("--detail", default="", help="看某篇的明细")
    args = ap.parse_args(argv)

    if args.report:
        r = report()
        if not r.get("ok"):
            print(f"  {r.get('error')}")
            return 1
        print("=" * 74)
        print("解析健康报告")
        print(f"  已检查 {r['total_checked']} 篇，其中不可信 {r['total_bad']} 篇")
        print(f"  最近检查：{r['checked_at']}")
        print("=" * 74)
        if not r["items"]:
            print("  ✓ 全部健康")
            return 0
        for it in r["items"][:40]:
            print(f"\n  [{it['verdict']}] {it['key']}  {it['title'][:44]}")
            for x in it["reasons"]:
                print(f"        · {x}")
        return 0

    if args.detail:
        d = detail(args.detail)
        if not d.get("ok"):
            print(f"  {d.get('error')}")
            return 1
        print(f"  {d['item_key']}  {d['title'][:50]}")
        print(f"  结论：{d['verdict']}（{d['checker']}）  {d['n_chars']} 字")
        for x in d["reasons"]:
            print(f"    · {x}")
        for i, s in enumerate(d["samples"]):
            print(f"\n  --- 片段 {i + 1} ---\n  {s[:300]!r}")
        return 0

    print("=" * 74)
    print("解析健康检查" + ("（仅规则）" if args.rule_only else "（规则 + 本地模型）"))
    print("=" * 74)
    if not args.rule_only:
        try:
            import judge
            m = args.model or judge.load_llm_config().get("model") or ""
            m = m or judge.pick_model()
            print(f"  模型：{m or '(Ollama 没跑，将只用规则)'}")
            if not m:
                args.rule_only = True
        except Exception as exc:                             # noqa: BLE001
            print(f"  ⚠ 载入 judge 失败，退回纯规则：{exc}")
            args.rule_only = True
    t0 = time.time()

    def prog(done, total, note):
        print(f"\r  进度 {done}/{total}  {note[:44]:44}", end="")

    res = scan(use_model=not args.rule_only, item_key=args.item,
               limit=args.limit,
               progress=prog if not args.rule_only else None,
               model=args.model)
    print("\r" + " " * 74 + "\r", end="")
    if res.get("legacy_dropped"):
        print(f"  已清理旧的 chunk_quality 表（{res['legacy_dropped']} 行误判数据）")
    print(f"  检查 {res['total']} 篇，耗时 {res['elapsed']} 秒"
          f"（{res.get('per_sec', 0)} 篇/秒）")
    print(f"  健康 {res.get('healthy', 0)}｜不可信 {res['bad']}"
          f"｜待定 {res.get('unsure', 0)}")
    if res.get("model_note"):
        print(f"  ⚠ {res['model_note']}")
    if res.get("items"):
        print("\n  不可信的文献：")
        for it in res["items"][:20]:
            print(f"    {it['key']:12} {(it['title'] or '')[:40]}")
            for x in it["reasons"][:2]:
                print(f"                 · {x}")
        print(f"\n  看明细：python offline\\check_chunks.py "
              f"--detail {res['items'][0]['key']}")
    print(f"  （总耗时 {time.time() - t0:.1f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
