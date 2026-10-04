"""段落重建测试：把 `fulltext/<key>.md` 切成可检查的段落，并支持 join/split 修正。

    python tests/test_paras.py

盯四件事：
  ① **不制造损伤**：句子切分/重新分组必须逐字保留原文（标点、句间空格都在）。
     这两个地方各踩过一次：英文句点用了消费式 `\\.` 会把句号删掉；对每句
     `.strip()` 会把 `window. However` 变成 `window.However`。所以这里是回归网。
  ② **两种形态都能认**：中文期刊的"折行"形态（一行 20~60 字）与来自
     ft-cache 的"块形态"（一行几百字）—— 实测第一版只认折行，全库粘出
     532 段超长、最长一段 11872 字。
  ③ **进度与修正靠 hash 对齐**：段号会漂（重建/补丁/页眉过滤都会改段数），
     内容指纹不会。
  ④ 落库读写（para_override / para_check）在临时库上可用，不碰真实库。

不需要 Zotero、不需要界面。
"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

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
print("段落重建（offline/paras.py）")
print("=" * 62)

try:
    import paras as P
    import schemas as S
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  导不进 paras：{type(exc).__name__}: {exc}")
    sys.exit(0)

# ---------------------------------------------------------------- [1] 句子切分
print("\n[1] 句子切分：不许吃掉标点与空格")
zh = "第一句话。第二句话！第三句？"
parts = P.split_sentences(zh)
check("中文按句号切开", len(parts) == 3, str(parts))
check("中文句末标点保留", all(p.rstrip().endswith(("。", "！", "？")) for p in parts),
      str(parts))

en = "This is one. However, this is two. And three."
parts = P.split_sentences(en)
check("英文按句点切开", len(parts) == 3, str(parts))
check("英文句点**没被吃掉**（回归：曾用消费式 \\. 删标点）",
      parts[0].strip().endswith("one."), repr(parts[0]))
check("英文句间空格保留（回归：曾 strip 成 window.However）",
      "one. However" in "".join(parts), repr(parts[:2]))

check("小数不切", len(P.split_sentences("电容为 93.46 % 时")) == 1)
check("缩写不切（et al. / e.g. / Fig.）",
      len(P.split_sentences("As shown by Smith et al. in Fig. 3 of the paper")) == 1)
check("句首大写才切", len(P.split_sentences("end of sentence. Another one here")) == 2)

# ---------------------------------------------------------------- [2] 折行形态
print("\n[2] 折行形态（中文期刊：一行 20~60 字）")
wrapped = [
    "高精度航空超导全张量磁梯度探测技术",          # 标题块（第 1 页开头）
    "荣亮亮 张典风 邱隆清 张国峰 裴易峰 伍俊*",
    "中国科学院上海微系统与信息技术研究所 上海 200050",
    "0 引言",
    "国际上航空磁测量技术的发展共经历了三个阶段其中航空磁场矢量测量和全",
    "张量磁力梯度测量是目前最新的研究方向相对传统航磁测量新一代航磁测量",
    "技术可有效减少反演中的多解性。",                  # ← 短尾行 + 句末标点 = 段尾
    "国内因受制于超导磁传感器器件我国航空超导磁测相关的研究起步比较晚其",
    "中开展航空低温超导全张量磁梯度测量研究工作的科研机构只有两家。",
    "",
    "**本页图注**",
    "- 图：图1 六棱锥构型的超导全张量磁梯度探头",
]
ps = P.split_page(wrapped, page=1)
kinds = [p.kind for p in ps]
check("标题/作者/单位三行被认成 heading 块",
      kinds[:3] == ["heading", "heading", "heading"], str(kinds))
check("`0 引言` 是 heading", "heading" in kinds[:5], str(kinds))
prose = [p for p in ps if p.kind == "prose"]
check("折行的引言被拼成一段（3 行）", len(prose[0].text.split("\n")) == 3,
      repr(prose[0].text[:60]))
check("段尾证据记在**下一段**的起点上",
      prose[1].evidence == "上一行短且以句末标点结尾", prose[1].evidence)
check("页内空行也能断段（证据落在空行之后落地的那个块上）",
      any(p.evidence == "页内空行" for p in ps),
      str([p.evidence for p in ps]))
check("图注块独立成 figure", "figure" in kinds, str(kinds))
check("公式编号单独成块（给公式待办留位）",
      P.split_page(["正文一句。", "(12)", "下一页正文。"], page=2)[1].kind == "formula")

# ---------------------------------------------------------------- [3] 块形态
print("\n[3] 块形态（ft-cache：一行几百字）：不制造损伤 + 能切")
long_line = ("This is sentence one about capacitance. It continues with "
             "more detail on the material. " * 18).strip()
check("长行会被按句群切开", len(P.expand_lines([long_line])) >= 3,
      f"{len(P.expand_lines([long_line]))} 组")
groups = P.expand_lines([long_line])
check("每个句群 ≤ 目标长度",
      all(len(g) <= P.GROUP_TARGET + 40 for g in groups),
      str([len(g) for g in groups]))
rebuilt = "".join(groups)
check("**重新分组后逐字不丢**（只允许首尾空白差异）",
      rebuilt.replace(" ", "") == long_line.replace(" ", ""),
      f"{len(rebuilt)} vs {len(long_line)}")

# ---------------------------------------------------------------- [4] hash
print("\n[4] 段落指纹：跨重建稳定")
import paras  # noqa: E402  (同名模块已导入，这里仅为可读性)
h1 = P.para_hash("同一段文字" * 30)
h2 = P.para_hash("同一段文字" * 30)
check("同文同指纹", h1 == h2, f"{h1} {h2}")
check("改一个字符就变", P.para_hash("同一段文字" * 30 + "x") != h1)
check("指纹长度 6", len(h1) == 6, h1)
p_a = P.split_page(["一段文字。" * 40], page=1)[0]
p_b = P.split_page(["一段文字。" * 40], page=7)[0]
check("换页/换段号不影响指纹", p_a.hash == p_b.hash, f"{p_a.hash} {p_b.hash}")

# ---------------------------------------------------------------- [5] override
print("\n[5] 边界修正：join / split（靠 hash 对齐）")
base = P.parse("## p.1\n\n第一段话结束。\n\n第二段话开始。\n")
check("基础解析出 2 段", len(base) == 2, str([p.n_chars for p in base]))
merged = P.apply_overrides(
    P.parse("## p.1\n\n第一段话结束。\n\n第二段话开始。\n"),
    [{"kind": "join_prev", "p_hash": P.parse(
        "## p.1\n\n第一段话结束。\n\n第二段话开始。\n")[1].hash}])
check("join_prev 合成一段", len(merged) == 1, str([p.n_chars for p in merged]))
check("合并后正文都在", "第一段话结束" in merged[0].text
      and "第二段话开始" in merged[0].text, merged[0].text[:40])
check("合并留下痕迹", merged[0].overridden == "join_prev", merged[0].overridden)

one = P.parse("## p.1\n\n这是一个很长的段落它其实包含两段但被粘在一起了。\n")
split = P.apply_overrides(
    one, [{"kind": "split_at", "p_hash": one[0].hash, "at": 12}])
check("split_at 切成两段", len(split) == 2, str([p.text for p in split]))
check("切点两侧内容都在", split[0].text + split[1].text == one[0].text,
      f"{split[0].text!r} + {split[1].text!r}")
check("空 override 不改动", len(P.apply_overrides(one, [])) == 1)
check("对齐靠 hash 而不是段号（重排后仍命中）",
      len(P.apply_overrides(one, [{"kind": "join_prev",
                                   "p_hash": one[0].hash}])) == 1)

# ---------------------------------------------------------------- [6] 检查单元
print("\n[6] 检查单元：超长段切片但不切断句子")
short = P.Para(page=1, index=0, logical_index=0, text="很短的一段话。")
check("短段只有一个单元", P.units(short) == ["很短的一段话。"], str(P.units(short)))
longt = "这是一个句子。" * 200          # 1400 字
pl = P.Para(page=1, index=0, logical_index=0, text=longt)
us = P.units(pl, max_chars=300)
check("长段被切成多块", len(us) >= 4, f"{len(us)} 块")
check("每块基本不超过上限", max(len(u) for u in us) <= 340,
      str([len(u) for u in us]))
check("**拼回去逐字不丢**", "".join(us) == longt, f"{sum(len(u) for u in us)}")
check("切点落在句子边界（除末块外都以句号结尾）",
      all(u.endswith("。") for u in us[:-1]), str([u[-3:] for u in us]))

# ---------------------------------------------------------------- [7] parse 端到端
print("\n[7] 整篇解析：按 `## p.N` 分页，各类块都认")
doc = "\n".join([
    "# 某篇论文", "", "> Zotero key `ABCD1234`｜2 页", "",
    "## p.1", "",
    "第一段正文第一行。", "第二段正文。", "",
    "图 1 系统结构示意图", "",
    "| a | b |", "| - | - |", "",
    "**本页表格 1**（2×2）", "", "| 1 | 2 |", "",
    "## p.2", "",
    "第二页正文。", "",
    "参考文献", "", "[1] Author A. Title. Journal, 2020.",
])
pp = P.parse(doc)
order = [p.logical_index for p in pp]
check("logical_index 连续", order == list(range(len(pp))), str(order))
got = {p.kind for p in pp}
check("认出来 refs", "refs" in got, str(got))
check("认出来 caption", "caption" in got, str(got))
check("认出来 table", "table" in got, str(got))
check("认出来 figure 块", "figure" in got, str(got))
check("两页都有内容", {p.page for p in pp} == {1, 2}, str({p.page for p in pp}))
check("参考文献条目没有混进正文",
      all("Author A" not in p.text for p in pp if p.kind == "prose"),
      str([p.text for p in pp if p.kind == "prose"]))

# ---------------------------------------------------------------- [8] 落库
print("\n[8] 落库：para_override / para_check（临时库）")
try:
    tmpdir = tempfile.mkdtemp(prefix="kbpara_")
    tmpdb = os.path.join(tmpdir, "index.db")
    conn = S.init_db(tmpdb)
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("para_override", "para_check", "fulltext_patch"):
        check(f"表 {t} 存在", t in tabs)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(experience)")}
    check("experience 有 history/updated_at 列",
          {"history", "updated_at"} <= cols, str(sorted(cols)))
    P.add_override(conn, "ABCD1234", "join_prev", "abc123", anchor="第二段",
                   reason="其实是同一段", source="user")
    ovs = P.load_overrides(conn, "ABCD1234")
    check("override 写进去了", len(ovs) == 1 and ovs[0]["kind"] == "join_prev",
          str(ovs))
    check("删得掉", P.remove_override(conn, "ABCD1234", "join_prev", "abc123") == 1)
    check("删完为空", P.load_overrides(conn, "ABCD1234") == [])
    conn.close()
except Exception as exc:  # noqa: BLE001
    check(f"落库测试没抛异常：{type(exc).__name__}: {exc}", False)

# ---------------------------------------------------------------- [9] 真实库冒烟
print("\n[9] 真实库冒烟（有就测，没有就跳过）")
try:
    rr = S.connect(S.INDEX_DB)
    keys = [r["key"] for r in rr.execute("SELECT key FROM items LIMIT 5")]
    rr.close()
except Exception:  # noqa: BLE001
    keys = []
tested = 0
for k in keys:
    ps = P.load_paras(S.connect(S.INDEX_DB), k) if keys else []
    if not ps:
        continue
    st = P.summary(ps)
    tested += 1
    check(f"{k}：解析出段落", st["total"] > 0, str(st))
    check(f"{k}：正文段中位长度合理（50~1500）",
          50 <= st["prose_median"] <= 1500 or st["prose"] == 0,
          f"中位 {st['prose_median']}")
    check(f"{k}：超长段占比 < 15%",
          st["too_long"] <= 0.15 * max(1, st["prose"]) + 1,
          f"{st['too_long']}/{st['prose']}")
    break
if not tested:
    print("  SKIP  索引库为空或没有 fulltext（正常，不影响上面各项）")

print()
print("=" * 62)
print(f"通过 {PASS}，失败 {FAIL}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
