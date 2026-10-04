"""窗格聊天助手的逻辑测试：注入 / 信号 / 逐段检查 / 定位 / 写入建议。

    python tests/test_chat_endpoints.py

盯五件事：
  ① **注入如实报数**：超预算时按页均匀取样（不是只留开头 —— 只留开头会让
     用户问后面某一节时答"这篇里没有"，那是注入策略造成的假象），
     并且 `chars / total_chars / truncated` 都要对。
  ② **客观信号不许冤枉正常学术文本**：公式、参考文献、表格这些**本来就是
     这样**的内容不能一律标可疑（`check_chunks.py` 记过那次 60% 误判）。
  ③ 逐段检查的契约：模型给的 `before` 必须在原文里找得到；想整段重写的
     一律降级成"建议人工处理"，不生成可采用项。
  ④ 续跑的跳过/重查逻辑：已查过的跳过、正文重建后标 stale 的要重查。
  ⑤ 定位**多候选不许猜**（列出来让用户选）。

模型调用一律用假 `judge.generate`（不需要 Ollama）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("=" * 62)
print("窗格聊天助手（offline/kbchat.py）")
print("=" * 62)

try:
    import judge
    import kbchat as KC
    import paras as P
    import schemas as S
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  导不进 kbchat：{type(exc).__name__}: {exc}")
    sys.exit(0)

# ---------------------------------------------------------------- 假模型
REPLY = {"text": "", "calls": []}


def fake_generate(prompt, system="", **kw):
    REPLY["calls"].append({"prompt": prompt, "system": system, "kw": kw})
    text = REPLY["text"]
    if callable(text):
        text = text(prompt)
    return {"ok": True, "text": text, "model": "fake", "backend": "test"}


real_generate = judge.generate
judge.generate = fake_generate

# ---------------------------------------------------------------- [1] 注入
print("\n[1] 上下文注入：如实报数 + 超预算按页取样")
ctx = KC.build_context("__不存在__", "none")
check("none → 空", ctx["text"] == "" and ctx["chars"] == 0, str(ctx))

# 造一个假知识库：把 views / fulltext 指到临时目录
tmp = tempfile.mkdtemp(prefix="kbchat_")
views, fulls = os.path.join(tmp, "views"), os.path.join(tmp, "fulltext")
os.makedirs(views, exist_ok=True)
os.makedirs(fulls, exist_ok=True)
real_views, real_full = S.VIEWS_DIR, S.FULLTEXT_DIR
S.VIEWS_DIR, S.FULLTEXT_DIR = views, fulls

KEY = "AAAA1111"
with open(os.path.join(views, f"{KEY}.tldr.md"), "w", encoding="utf-8") as fh:
    fh.write("摘要：这是摘要级视图。\n")
pages = ["# 标题", "", "> Zotero key `AAAA1111`｜5 页", ""]
for i in range(1, 6):
    pages += [f"## p.{i}", "", f"第{i}页正文。" + f"内容{i}" * 60, ""]
with open(os.path.join(fulls, f"{KEY}.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(pages))

t = KC.build_context(KEY, "tldr")
check("tldr 注入成功", "摘要级" in t["text"] and t["chars"] > 0, str(t)[:60])
f = KC.build_context(KEY, "full", budget=400)
check("full 超预算 → truncated", f["truncated"] is True, str(f))
check("如实报总数与注入数", f["total_chars"] > f["chars"] > 0,
      f"{f['chars']}/{f['total_chars']}")
check("**取样包含最后一页**（不是只留开头）", "第5页" in f["text"],
      f["text"][-60:])
check("提示语说明是取样", "取样" in f["note"], f["note"])
f2 = KC.build_context(KEY, "full", budget=99999)
check("预算够时不截断", f2["truncated"] is False, str(f2)[:60])
check("缺文件时给可执行提示", "建库" in KC.build_context("NOPE0000", "full")["note"],
      KC.build_context("NOPE0000", "full")["note"])

# ---------------------------------------------------------------- [2] 信号
print("\n[2] 客观信号：不许冤枉正常的学术文本")
normal_zh = ("全张量磁梯度描述的是磁场矢量在三维空间的变化率信息，具有受磁化方向"
             "影响小、能够反映目标体的矢量磁矩信息等优点。在磁感应强度的散度和"
             "旋度为 0 的情况下，全张量磁梯度可以表示成如公式 1 所示的 3x3 矩阵。")
normal_en = ("The 1D structure improves its performance with high mechanical "
             "strength. This limitation can be partially overcome by using "
             "carbon derivatives, as reported in previous studies.")
for label, txt in (("中文正文", normal_zh), ("英文正文", normal_en)):
    sig = KC.signals(txt)
    hot, why = KC.suspect(sig)
    check(f"{label} 不判可疑", not hot, str(why))
refs = "[157] D. Mohanadas, N.H.N. Azman, Y. Sulaiman, A bifunctional asymmetric " \
       "electrochromic supercapacitor, Chemical Engineering Journal, 2021."
sig = KC.signals(refs)
check("参考文献不判可疑（它本来就零碎）", not KC.suspect(sig)[0],
      str(KC.suspect(sig)[1]))

soup = "⎡BX BY BZ / aX0 aX1 aX2⎦ (5) where ∑∫√ ± ×"
hot, why = KC.suspect(KC.signals(soup))
# ⚠ 这一条是回归网：只按"符号占比"判会漏掉它（符号才占十几个百分点），
#   要靠矩阵括号残片 + 字母数字交错才认得出（本机实测漏判过）。
check("符号汤判可疑（矩阵碎片/字母数字交错）",
      hot and any(("矩阵" in w or "交错" in w) for w in why), str(why))
shifted = "&DOFXODWLRQ DQG 6LPXODWLRQ RI WKH PDJQHWLF"
check("位移乱码：异域/符号信号至少能抓到一类",
      isinstance(KC.signals(shifted), dict))
exotic = "中 文 混 入 藏 文 ༀ༁༂༃ 与 天 城 文 अआइई 的 段 落"
hot, why = KC.suspect(KC.signals(exotic))
check("异域码位判可疑", hot and any("异域" in w for w in why), str(why))
table = "| a | b | c |\n| --- | --- | --- |\n| 1 | 2 | 3 |"
hot, why = KC.suspect(KC.signals(table))
check("表格残留判可疑", hot and any("表格" in w for w in why), str(why))

# ---------------------------------------------------------------- [3] 逐段
print("\n[3] 逐段检查：最小修正 + 找不到原文就降级")
conn = S.init_db(os.path.join(tmp, "index.db"))
text = "这是一段正常的中文正文，用来做测试。" * 3
REPLY["text"] = json.dumps({"verdict": "damaged", "kind": "text",
                            "before": "正常的中文", "after": "正常的中文内容",
                            "reason": "测试"}, ensure_ascii=False)
out = KC.check_paragraph(conn, KEY, 1, 2, text)
check("解析出 verdict", out.get("verdict") == "damaged", str(out)[:80])
check("带上了客观信号", isinstance(out.get("signals"), dict), str(out)[:80])
check("before 在原文里 → 保留", out.get("before") == "正常的中文", str(out)[:80])
check("manual_only 为假", out.get("manual_only") is False)

REPLY["text"] = json.dumps({"verdict": "damaged", "kind": "text",
                            "before": "原文里根本没有这句话", "after": "x",
                            "reason": "编的"}, ensure_ascii=False)
out = KC.check_paragraph(conn, KEY, 1, 2, text)
check("before 找不到 → 降级 manual_only", out.get("manual_only") is True,
      str(out)[:80])
check("降级后不给可采用项", out.get("before") == "" and out.get("after") == "",
      str(out)[:80])

REPLY["text"] = json.dumps({"verdict": "damaged", "kind": "text",
                            "before": text, "after": "换个说法", "reason": "重写"},
                           ensure_ascii=False)
out = KC.check_paragraph(conn, KEY, 1, 2, text)
check("想整段重写 → 降级", out.get("manual_only") is True, str(out)[:60])

REPLY["text"] = "模型没按格式回"
out = KC.check_paragraph(conn, KEY, 1, 2, text)
check("非 JSON → ok=False 但有信号", out.get("ok") is False
      and isinstance(out.get("signals"), dict), str(out)[:60])

REPLY["text"] = lambda p: ""
judge.generate = lambda *a, **k: {"ok": False, "error": "连不上", "hint": "启动 Ollama"}
out = KC.check_paragraph(conn, KEY, 1, 2, text)
check("模型失败 → 带 error 与 hint",
      out.get("ok") is False and out.get("hint") == "启动 Ollama", str(out)[:80])
judge.generate = fake_generate

# ---------------------------------------------------------------- [4] 续跑
print("\n[4] 续跑：跳过已查、重建后要重查")
pl = KC.plan(conn, KEY, scope="all")
check("计划有 total/next/items",
      isinstance(pl.get("total"), int) and "next" in pl and "items" in pl,
      str(list(pl)[:8]))
first = pl["next"]
check("第一段没查过 → 不跳过", first and not first["skip"], str(first)[:60])
conn.execute(
    "INSERT INTO para_check(item_key, p_hash, page, para_index, logical_index,"
    " status, signals, note, patch_ids, done_units, checked_at)"
    " VALUES(?,?,?,?,?,'ok','{}','', '[]', 1, 'now')",
    (KEY, first["hash"], first["page"], 0, first["logical_index"]))
conn.commit()
pl2 = KC.plan(conn, KEY, scope="all")
again = [it for it in pl2["items"] if it["hash"] == first["hash"]][0]
check("已查过的段被跳过", again["skip"] is True, str(again)[:60])
check("next 换成了别的段",
      (pl2["next"] or {}).get("hash") != first["hash"], str(pl2["next"])[:60])
conn.execute("UPDATE para_check SET status='stale' WHERE item_key=? AND p_hash=?",
             (KEY, first["hash"]))
conn.commit()
pl3 = KC.plan(conn, KEY, scope="all")
again = [it for it in pl3["items"] if it["hash"] == first["hash"]][0]
check("stale（正文重建过）不再跳过", again["skip"] is False and again["status"] == "stale",
      str(again)[:80])
check("计划里报了 stale 计数", pl3["stale"] >= 1, str(pl3["stale"]))

# ---------------------------------------------------------------- [5] 定位
print("\n[5] 定位：多候选不许猜")
ps = [p for p in P.load_paras(conn, KEY) if p.kind == "prose"]
if ps:
    loc = KC.locate(conn, KEY, ps[0].text[:50])
    check("精确片段 → 唯一命中", bool(loc.get("best")), str(loc)[:80])
loc = KC.locate(conn, KEY, "正文")
check("太短 → 拒绝", loc.get("ok") is False, str(loc)[:60])
loc = KC.locate(conn, KEY, "这段文字在这篇文献里根本不存在" * 3)
check("完全不像 → 没有 best", loc.get("best") is None, str(loc)[:80])

# ---------------------------------------------------------------- [6] 建议
print("\n[6] 写入建议：口径与闸门")
REPLY["text"] = json.dumps({
    "experiences": [{"asked": "方法 A 有效吗", "outcome": "effective",
                     "method": "A", "reason": "r", "item_keys": []},
                    {"asked": "装了插件", "outcome": "unknown", "method": "",
                     "item_keys": ["AAAA1111"]}],
    "weights": [{"key": "AAAA1111", "pinned": True, "note": "自检"}],
    "patches": [{"page": 1, "kind": "text", "before": "正常的中文",
                 "after": "正常的中文（修正）", "note": "测试"}],
    "joins": []}, ensure_ascii=False)
pr = KC.propose(KEY, "讨论片段", "记一下")
check("建议解析出三类", bool(pr.get("experiences") and pr["weights"]
                             and pr["patches"]), str(pr)[:80])
check("没给文献的经验被标 suspect_unrelated",
      pr["experiences"][0]["suspect_unrelated"] is True
      and pr["experiences"][1]["suspect_unrelated"] is False,
      str([e["suspect_unrelated"] for e in pr["experiences"]]))
check("没给文献时兜底关联当前这篇",
      pr["experiences"][0]["item_keys"] == [KEY], str(pr["experiences"][0])[:60])
check("propose 不写库（经验表还是空的）",
      conn.execute("SELECT COUNT(*) n FROM experience").fetchone()["n"] == 0)


class _W:
    """写接口适配器（Searcher 的替身：借 connection 走同样的 SQL）。"""

    def __init__(self, c):
        self.conn = c

    def write(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    def write_returning_id(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def read_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def write_many(self, statements):
        rows = []
        for sql, params in statements:
            cur = self.conn.execute(sql, params)
            if cur.description:
                rows = cur.fetchall()
        self.conn.commit()
        return 0, rows

    def reload_weights(self):
        pass


applied = KC.apply_plan(_W(conn), KEY, pr)
check("apply_plan 写了经验", applied["written"]["experiences"] != [],
      str(applied)[:100])
check("写了权重", "AAAA1111" in applied["written"]["weights"], str(applied)[:100])
check("写了补丁", applied["written"]["patches"] != [], str(applied)[:100])
n_exp = conn.execute("SELECT COUNT(*) n FROM experience").fetchone()["n"]
check("经验真的进库了", n_exp == 2, str(n_exp))
wt = conn.execute("SELECT pinned, note FROM item_weight WHERE item_key=?",
                  ("AAAA1111",)).fetchone()
check("权重真的写进去了", wt and wt["pinned"] == 1, str(dict(wt) if wt else None))
n_patch = conn.execute("SELECT COUNT(*) n FROM fulltext_patch").fetchone()["n"]
check("补丁进库且状态是 applied", n_patch == 1, str(n_patch))

# ---------------------------------------------------------------- [7] 草稿
print("\n[7] 经验草稿：整理但不瞎猜")
REPLY["text"] = json.dumps({
    "asked": "分离求解有效吗", "outcome": "乱写", "method": "位置-磁矩分离",
    "context": "", "reason": "实测误差更小", "evidence": "", "tags": "自检,方法",
    "item_keys": ["不存在的Key"], "similar_hint": "在讲分离求解"},
    ensure_ascii=False)
dr = KC.draft_experience("我用分离求解试了一下，误差小很多", conn=conn)
check("outcome 归一成白名单内", dr["draft"]["outcome"] == "unknown",
      dr["draft"]["outcome"])
check("tags 拆成列表", dr["draft"]["tags"] == ["自检", "方法"], str(dr["draft"]["tags"]))
check("给了 items（带标题，便于用户核对）", isinstance(dr.get("items"), list),
      str(dr.get("items")))
check("草稿不写库", conn.execute("SELECT COUNT(*) n FROM experience").fetchone()["n"] == 2)

judge.generate = real_generate
S.VIEWS_DIR, S.FULLTEXT_DIR = real_views, real_full
conn.close()

print()
print("=" * 62)
print(f"通过 {PASS}，失败 {FAIL}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
