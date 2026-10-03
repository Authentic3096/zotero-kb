# ⚠ 与 tests/ 下其它测试一致：**只用合成的摘要/关键词/DOI**（编造的），
#   不把本机任何一篇文献的标题、摘要、关键词写进这个文件。

"""papermeta 的自检：倒排索引还原、JATS 剥离、摘要截断、DOI 归一。

不发网络：`enrich()` 要联网，这里只测它的**纯函数零件**。
联网那一半用 `python online/papermeta.py <doi>` 手工自查。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "online"))

import papermeta as PM  # noqa: E402

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


print("=" * 66)
print("[A] OpenAlex 倒排索引还原")
print("=" * 66)

# OpenAlex 把摘要存成 {词: [位置...]}，要还原成正文
inv = {"We": [0], "study": [1], "reproducibility.": [2], "It": [3], "works.": [4]}
check("连续位置还原成原句",
      PM._inv_to_text(inv) == "We study reproducibility. It works.",
      repr(PM._inv_to_text(inv)))

# 位置**乱序**也要按位置排回来（dict 的遍历顺序不保证是阅读顺序）
inv2 = {"world": [1], "Hello": [0], "again": [2]}
check("按位置排序而不是按键序",
      PM._inv_to_text(inv2) == "Hello world again",
      repr(PM._inv_to_text(inv2)))

# 同一个词出现多次 → 一个词对多个位置
inv3 = {"a": [0, 2], "b": [1]}
check("一个词多个位置",
      PM._inv_to_text(inv3) == "a b a", repr(PM._inv_to_text(inv3)))

# 空洞：位置 1 缺号，不该插空格变出双空格
inv4 = {"x": [0], "y": [2]}
check("位置有缺号时不补出多余空格",
      PM._inv_to_text(inv4) == "x y", repr(PM._inv_to_text(inv4)))

check("空/None 返回空串",
      PM._inv_to_text(None) == "" and PM._inv_to_text({}) == "")
check("形态不对（值不是 list）不炸",
      PM._inv_to_text({"a": "坏数据"}) == "",
      repr(PM._inv_to_text({"a": "坏数据"})))
check("位置里混进非整数不炸",
      PM._inv_to_text({"a": [0, "x", None, 1]}) == "a a",
      repr(PM._inv_to_text({"a": [0, "x", None, 1]})))


print()
print("=" * 66)
print("[B] Crossref 的 JATS 剥离")
print("=" * 66)

check("剥掉标签",
      PM._strip_jats("<jats:p>Some <b>bold</b> text</jats:p>") == "Some bold text",
      repr(PM._strip_jats("<jats:p>Some <b>bold</b> text</jats:p>")))
check("实体还原",
      PM._strip_jats("a &lt; b &amp;&amp; c &gt; d") == "a < b && c > d",
      repr(PM._strip_jats("a &lt; b &amp;&amp; c &gt; d")))
check("压掉多余空白与换行",
      PM._strip_jats("a\n\n   b\t\tc") == "a b c",
      repr(PM._strip_jats("a\n\n   b\t\tc")))
check("空输入返回空", PM._strip_jats("") == "" and PM._strip_jats(None) == "")


print()
print("=" * 66)
print("[C] 摘要截断（候选表里不能塞整篇摘要）")
print("=" * 66)

short = "很短的一句。"
check("短摘要原样返回", PM._clip(short) == short)

long_en = ("This is a sentence. " * 60).strip()
c = PM._clip(long_en, 200)
check("超长会被截断", len(c) <= 210, f"得 {len(c)}")
# ⚠ 断在**句末**时不补省略号（补了会变成 "…sentence.…"，很难看）。
#   所以断言的是"要么补了省略号、要么确实断在句末"，而不是"一定以省略号结尾"。
check("截断处有交代（省略号或完整句末）",
      c.endswith("…") or c.endswith("."), repr(c[-20:]))
check("尽量断在句末（不在词中间硬切）",
      c.rstrip("…").endswith(".") or " " in c[-15:], repr(c[-30:]))
# 最重要的一条：截出来的**必须原文的前缀**，绝不能是改写/拼接出来的
check("截断结果是原文的子串（不许改写）",
      long_en.startswith(c.rstrip("…").rstrip()), repr(c[-30:]))

long_cn = ("这是一句话。" * 200)
c2 = PM._clip(long_cn, 120)
check("中文也按句号断", c2.endswith("。") or c2.endswith("…"), repr(c2[-20:]))
check("截断后长度受控", len(c2) <= 130, f"得 {len(c2)}")

check("空摘要返回空", PM._clip("") == "" and PM._clip(None) == "")


print()
print("=" * 66)
print("[D] DOI 归一化")
print("=" * 66)

cases = [
    ("10.1234/abcd", "10.1234/abcd", "裸 DOI"),
    ("https://doi.org/10.1234/ABCD", "10.1234/abcd", "URL 前缀去掉 + 转小写"),
    ("doi:10.1234/AbCd", "10.1234/abcd", "doi: 前缀 + 转小写"),
    ("  10.1234/abcd  ", "10.1234/abcd", "首尾空白"),
    ("", "", "空"),
]
for raw, want, label in cases:
    got = PM._norm_doi(raw)
    check(label, got == want, f"{raw!r} → {got!r}，期望 {want!r}")

# 归一化是为了**去重**：同一篇的不同写法必须落到同一个键
check("同一篇的三种写法归一后相同",
      len({PM._norm_doi("10.1234/AbC"), PM._norm_doi("https://doi.org/10.1234/abc"),
           PM._norm_doi("DOI:10.1234/abc")}) == 1)


print()
print("=" * 66)
print("[E] enrich 的输入清洗（不发网络的那部分）")
print("=" * 66)

check("空输入返回空列表", PM.enrich([]) == [] and PM.enrich(None) == [])
# 全是空/垃圾 → 没有可查的 DOI → 直接空返回，不该发请求
check("全是空串时不发请求直接返回", PM.enrich(["", "   "]) == [])

# 返回必须与输入**等长同序**，否则候选表按下标就对不上了
rows = PM.enrich(["", "  "])
check("无有效 DOI 时结果是空（调用方据此报错而不是当成功）", rows == [], rows)


print()
print("=" * 66)
print(f"通过 {PASS}　失败 {FAIL}")
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
