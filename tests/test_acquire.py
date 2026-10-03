# ⚠ 与 tests/ 下其它测试一致：**只用合成的 DOI 与标题**（编造的、不指向真实文献），
#   不把本机任何一篇文献的 DOI、标题、条目 key 写进这个文件。
#   原因见 test_metafill.py 文件头：测试文件会被提交、被分享、被别的会话读到，
#   一旦写进真实值，它就成了"用户研究领域的清单"。
#
#   所以这里的分工是：
#     · 纯函数（归一化/版本/措辞/报告格式）→ 合成输入，可复现、换台机器也能跑
#     · 与 Zotero 的接线           → 不在这里测（那要真 Zotero）。
#                                    用 tools/check_acquire.py 分段自检。

"""acquire 的自检：DOI 归一化、载荷清洗、版本闸、结果措辞、报告格式。

分四层：
  [A] 归一化：各种写法（裸 DOI / doi.org URL / 前缀 / 尾标点）与垃圾输入。
  [B] 载荷清洗：批内去重、非法项要**报出来而不是静默吞掉**、条数上限。
  [C] 版本闸：0.23 < 0.24 ≤ 0.24.1 这类比较，以及"没设过"的处理。
  [D] 措辞与报告：取消 / 无可存 / 干跑 / 正常四种分支各说各的话，
      **不能把"没有可存的"说成"用户取消了"**。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import acquire as AQ  # noqa: E402

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


# 合成 DOI：形状合法，但不指向任何真实文献
D1 = "10.1234/abcd.5678"
D2 = "10.5678/efgh-9012"


print("=" * 66)
print("[A] DOI 归一化")
print("=" * 66)

cases = [
    (D1, D1, "裸 DOI 原样"),
    (f"https://doi.org/{D1}", D1, "doi.org 前缀要去掉"),
    (f"http://dx.doi.org/{D1}", D1, "dx.doi.org 前缀要去掉"),
    (f"DOI: {D1}", D1, "DOI: 前缀要去掉"),
    (f"doi:{D1}", D1, "小写 doi: 前缀也要去掉"),
    (f"  {D1}  ", D1, "首尾空白要去掉"),
    (f"{D1}.", D1, "尾部的句号要去掉"),
    (f"{D1},", D1, "尾部的逗号要去掉"),
    (f"({D1})", D1, "包裹的括号要去掉"),
    (f"见 https://doi.org/{D1} 这篇", D1, "夹在句子里也能抠出来"),
    ("", "", "空串 → 空"),
    ("   ", "", "全空白 → 空"),
    ("没有 DOI 的一句话", "", "没有 DOI 形状 → 空"),
    ("10.123/abc", "", "前缀位数不够（要 4~9 位）→ 空"),
]
for raw, want, label in cases:
    got = AQ.normalize_doi(raw)
    check(label, got == want, f"输入 {raw!r} 得 {got!r}，期望 {want!r}")

# 大小写：DOI 大小写不敏感，但归一化不该擅自改大小写（同一篇的不同写法要能靠
# lower() 比对 —— 去重那一步就是这么做的，见 normalize_items）
check("不擅自改大小写", AQ.normalize_doi("10.1234/AbCd") == "10.1234/AbCd")


print()
print("=" * 66)
print("[B] 载荷清洗")
print("=" * 66)

good, bad = AQ.normalize_items([D1, f"https://doi.org/{D2}", "这不是 DOI"])
check("合法项都进来", len(good) == 2, f"得 {good}")
check("非法项报出来而不是吞掉", bad == ["这不是 DOI"], f"得 {bad}")

good, bad = AQ.normalize_items([D1, D1.upper(), f"doi:{D1}"])
check("批内重复只留一条", len(good) == 1, f"得 {len(good)} 条：{good}")
check("批内重复不算'非法'", bad == [], f"得 {bad}")

good, bad = AQ.normalize_items([{"doi": D1, "title": "某标题"}])
check("字典形态也认", len(good) == 1 and good[0]["doi"] == D1, f"得 {good}")
check("字典形态保留标题", good[0]["title"] == "某标题")

good, bad = AQ.normalize_items([])
check("空输入 → 空结果", good == [] and bad == [])
check("None 也不炸", AQ.normalize_items(None) == ([], []))

# 条数上限：要在**发网络请求之前**就挡住
try:
    AQ.acquire([f"10.1234/x{i}" for i in range(AQ.MAX_ITEMS + 1)])
    check("超量被挡住", False, "居然没抛 AcquireError")
except AQ.AcquireError as exc:
    check("超量被挡住", "最多" in str(exc), f"信息是 {exc}")
    check("超量给出分批建议", any("批" in h for h in exc.how_to_fix))
except Exception as exc:  # noqa: BLE001
    check("超量被挡住", False, f"抛了别的异常 {type(exc).__name__}: {exc}")


print()
print("=" * 66)
print("[C] 版本闸")
print("=" * 66)

ver_cases = [
    ("0.24.0", False, "刚好等于门槛 → 放行"),
    ("0.24.1", False, "高于门槛 → 放行"),
    ("0.25.0", False, "次版本更高 → 放行"),
    ("1.0.0", False, "主版本更高 → 放行"),
    ("0.23.0", True, "低于门槛 → 拦"),
    ("0.9.5", True, "更老 → 拦"),
    ("", True, "空版本（没设过）→ 拦"),
    ("0.24.0-beta.1", False, "带后缀 → 取数字部分，放行"),
    ("0.24", False, "只有两段 → 补 0，放行"),
]
for v, should_block, label in ver_cases:
    blocked = AQ._parse_version(v) < AQ.MIN_PLUGIN_VERSION
    check(label, blocked == should_block,
          f"{v!r} 解析成 {AQ._parse_version(v)}，期望 blocked={should_block}")

check("门槛是 0.24.0", AQ.MIN_PLUGIN_VERSION == (0, 24, 0),
      f"得 {AQ.MIN_PLUGIN_VERSION}")


print()
print("=" * 66)
print("[D] 措辞与报告格式")
print("=" * 66)

# 四个分支必须说四句不同的话 —— 尤其"取消"和"没得存"不能混
s_cancel = AQ.summarize({"cancelled": True})
s_nothing = AQ.summarize({"nothing_to_do": True})
s_dry = AQ.summarize({"dry_run": True, "added": 2})
check("取消：说的是取消", "取消" in s_cancel, s_cancel)
check("没得存：不说是取消", "取消" not in s_nothing, s_nothing)
check("没得存：说的是没得存", "没有可入库" in s_nothing, s_nothing)
check("干跑：说明没写库", "没" in s_dry and ("写入" in s_dry or "没有" in s_dry), s_dry)
check("三种分支措辞互不相同",
      len({s_cancel, s_nothing, s_dry}) == 3, f"{s_cancel} / {s_nothing} / {s_dry}")

r = {
    "ok": True, "dry_run": False, "elapsed_s": 9.9, "task_id": "t1",
    "collection": {"id": 7, "name": "某分类"},
    "added": 2, "with_pdf": 1,
    "results": [
        {"doi": D1, "title": "某标题一", "itemKey": "AAAA1111", "status": "added",
         "pdf": {"ok": True, "attachmentTitle": "Full Text PDF"}},
        {"doi": D2, "title": "某标题二", "itemKey": "BBBB2222", "status": "added",
         "pdf": {"ok": False, "noFileFound": True, "tried": ["doi", "oa"]}},
    ],
    "failed": [{"doi": "10.9999/nope", "why": "no_translator"}],
    "duplicates": [{"doi": "10.1111/dup", "title": "某重复", "key": "CCCC3333"}],
    "dropped": ["一句不是 DOI 的话"],
}
rep = AQ.format_report(r)
for frag, label in [
    ("某分类", "报告里有目标分类名"),
    ("AAAA1111", "报告里有成功的条目 key"),
    ("没找到可用的", "没下到 PDF 的如实标注"),
    ("试过：doi/oa", "报告里带了 Zotero 真正试过的 resolver"),
    ("没有能识别这个 DOI 的翻译器", "失败原因说人话"),
    ("库里已有的", "去重的那类单独成段"),
    ("不是合法 DOI", "被丢掉的原文要报出来"),
    ("浏览器连接器", "没 PDF 时给出下一步建议"),
]:
    check(label, frag in rep, f"报告里找不到 {frag!r}")

check("报告是 Markdown 表格", "|---|" in rep)
check("报告不含真实条目 key 格式之外的臆造",
      "DDDD4444" not in rep)

# 插件侧的 note 必须原样带出来 —— 用户看不到它就会以为"自动分类坏了"。
# （这条是踩出来的：note 的渲染代码写好了，但 MCP 子进程是长驻的、
#   没重启，于是真机回报里它根本没出现。纯函数测试能钉住渲染这一半。）
rep_note = AQ.format_report({
    "ok": True, "added": 1, "with_pdf": 0, "dry_run": False,
    "note": "这批跳过了逐篇的自动分类弹窗。要归类用右键。",
    "results": [{"doi": D1, "title": "某标题", "itemKey": "AAAA1111",
                 "status": "added", "pdf": {"ok": False, "noFileFound": True}}],
    "failed": [], "duplicates": [], "dropped": [],
})
check("插件的 note 出现在报告里", "跳过了逐篇的自动分类弹窗" in rep_note,
      rep_note[:200])
check("note 带引用块标记（与正文分开）", "> 这批跳过了" in rep_note, rep_note[:200])
check("没有 note 时不产生空引用块",
      "> \n" not in AQ.format_report({
          "ok": True, "added": 0, "with_pdf": 0, "results": [],
          "failed": [], "duplicates": [], "dropped": [],
          "summary": "x"}))

rep_dry = AQ.format_report({
    "ok": True, "dry_run": True, "added": 1, "with_pdf": 0,
    "results": [{"doi": D1, "title": "某标题", "byline": "Zhang 2024",
                 "venue": "某刊，第 1 卷", "status": "dry", "pdf": None}],
    "failed": [], "duplicates": [], "dropped": [],
})
check("干跑报告明说尚未写入", "尚未写入" in rep_dry, rep_dry[:200])
check("干跑报告不带'浏览器连接器'那段",
      "浏览器连接器" not in rep_dry, "干跑又没建条目，不该劝人去抓")

# summarize 正常分支：数字要如实，不能把"没下到"说成"下到了"
s_ok = AQ.summarize({"added": 3, "with_pdf": 1,
                     "failed": [{"why": "no_translator"}],
                     "duplicates": [{"doi": D1}]})
check("正常：报入库数", "3 篇" in s_ok, s_ok)
check("正常：报带 PDF 数", "1 篇" in s_ok, s_ok)
check("正常：报跳过数", "1 篇" in s_ok and "已有" in s_ok, s_ok)


print()
print("=" * 66)
print("[E] 挂接报告")
print("=" * 66)

ra = AQ.format_attach_report({"results": [
    {"itemKey": "AAAA1111", "attachmentKey": "ZZZZ9999",
     "status": "attached", "path": "x.pdf"},
    {"itemKey": "BBBB2222", "status": "error", "path": "y.pdf",
     "error": "不是 PDF（文件头是 \"<htm\"）"},
]})
check("挂接报告报成功数", "挂上 1 个" in ra, ra)
check("挂接报告报失败原因原文", "不是 PDF" in ra, ra)
check("挂接报告提醒重抽才有正文", "kb_reindex" in ra, ra)

# PDF 那一列的四种形态都要说对 —— 真机踩过一次：
# Zotero 的 addFileFromURLs **一条路都没走通时可能既不抛异常也不给
# noFileFound**（直接返回假值），那种情况下只有 `tried` 能证明"确实试过"。
# 原来这一支落进了 else，被写成"—（未尝试）"，而同一行又列着"试过：doi/url/oa/oa"，
# 自相矛盾。
def _pdf_cell(pdf_obj):
    rep = AQ.format_report({
        "ok": True, "added": 1, "with_pdf": 0, "elapsed_s": 1, "task_id": "t",
        "results": [{"doi": D1, "title": "某标题", "itemKey": "AAAA1111",
                     "status": "added", "pdf": pdf_obj}],
        "failed": [], "duplicates": [], "dropped": [],
    })
    for line in rep.splitlines():
        if "AAAA1111" in line:
            return line
    return ""


check("带上 PDF → 说已带",
      "✅ 已带" in _pdf_cell({"ok": True, "attachmentTitle": "Full Text PDF"}),
      _pdf_cell({"ok": True, "attachmentTitle": "Full Text PDF"}))
check("试过但没找到 → 说没找到，**不说未尝试**",
      "没找到可用的" in _pdf_cell({"ok": False, "tried": ["doi", "url", "oa", "oa"]})
      and "未尝试" not in _pdf_cell({"ok": False, "tried": ["doi"]}),
      _pdf_cell({"ok": False, "tried": ["doi", "url", "oa", "oa"]}))
check("明确报 noFileFound → 也说没找到",
      "没找到可用的" in _pdf_cell({"ok": False, "noFileFound": True, "tried": ["doi"]}),
      _pdf_cell({"ok": False, "noFileFound": True, "tried": ["doi"]}))
check("有真错误 → 把错误原文带出来",
      "⚠" in _pdf_cell({"ok": False, "error": "boom", "tried": ["doi"]}),
      _pdf_cell({"ok": False, "error": "boom", "tried": ["doi"]}))
check("确实没尝试（find_pdf=False）→ 才写未尝试",
      "未尝试" in _pdf_cell(None), _pdf_cell(None))

try:
    AQ.attach_files([])
    check("空清单被挡住", False, "居然没抛")
except AQ.AcquireError as exc:
    check("空清单被挡住", "没有可挂的文件" in str(exc), str(exc))

try:
    AQ.attach_files([{"itemKey": "AAAA1111", "path": "D:\\不存在的目录\\x.pdf"}])
    check("文件不存在被挡住", False, "居然没抛")
except AQ.AcquireError as exc:
    check("文件不存在被挡住", "不存在" in str(exc), str(exc))

try:
    AQ.attach_files([{"itemKey": "", "path": "x.pdf"}])
    check("缺 itemKey 被挡住", False, "居然没抛")
except AQ.AcquireError as exc:
    check("缺 itemKey 被挡住", "没有可挂的文件" in str(exc), str(exc))


print()
print("=" * 66)
print("[F] 异步作业（DSH 里看实时进度的机制）")
print("=" * 66)

# 空 job_id / 不存在的 job：要明确说"没有"，而不是抛异常或返回半个 dict
check("不存在的 job 返回空 dict", AQ.job_progress("根本没有这个") == {},
      f"得 {AQ.job_progress('根本没有这个')}")
check("还没派过作业时 job_id='' 也返回空", AQ.job_progress("") == {})

PLAN = [{"doi": "10.1234/a", "title": "甲"},
        {"doi": "10.1234/b", "title": "乙"},
        {"doi": "10.1234/c", "title": "丙"}]
jid = AQ._job_new(3, PLAN, False)
snap = AQ.job_progress(jid)
check("新作业是 pending", snap["state"] == "pending", snap["state"])
check("新作业 index=0", snap["index"] == 0)
check("新作业带回 plan（DSH 立刻能念清单）", snap["plan"] == PLAN, snap["plan"])
check("pending 的进度行说等领取", "等 Zotero 领取" in snap["summary"], snap["summary"])

AQ._job_update(jid, state="running", index=1,
               current_doi="10.1234/b", current_title="乙")
s = AQ.job_progress(jid)
check("running 报出第几篇/共几篇", "1/3" in s["summary"], s["summary"])
check("running 报出正在处理哪个 DOI", "10.1234/b" in s["summary"], s["summary"])
check("updated 会随时间推进", s["updated"] >= s["started"])

# 边跑边给结果：用户不必等到最后才知道哪篇好了
AQ._job_update(jid, results=[{"status": "added", "title": "甲",
                              "itemKey": "AAAA1111", "pdf": {"ok": True}}])
s = AQ.job_progress(jid)
check("跑的过程中就能看到已完成条目", len(s["results"]) == 1, s["results"])

AQ._job_update(jid, state="done", index=3, added=2, with_pdf=1,
               results=[{"status": "added", "title": "甲", "itemKey": "AAAA1111",
                         "pdf": {"ok": True}},
                        {"status": "added", "title": "乙", "itemKey": "BBBB2222",
                         "pdf": {"ok": False, "noFileFound": True}}],
               duplicates=[{"doi": "10.1234/c", "key": "CCCC3333"}],
               collection={"id": 8, "name": "某分类"})
s = AQ.job_progress(jid)
check("done 的进度行说完成几篇", "完成：2/3" in s["summary"], s["summary"])
check("done 的进度行说带了几篇 PDF", "1 篇带 PDF" in s["summary"], s["summary"])
check("done 的进度行把'库里已有'也说清楚", "库里已有" in s["summary"], s["summary"])

# 一篇都没成功时，进度行不能只说"完成：0/1 篇入库" —— 那听着像成功了
jid_bad = AQ._job_new(1, [], False)
AQ._job_update(jid_bad, state="done", index=1, added=0, with_pdf=0,
               failed=[{"doi": "10.1234/x", "why": "error",
                        "detail": "任务派出去 30 秒了还没被领取"}])
sb = AQ.job_progress(jid_bad)["summary"]
check("全失败时进度行明确说'没成功'", "没成功" in sb, sb)
check("全失败时不出现'0/1 篇入库'就收尾的假成功措辞",
      "0/1 篇入库" not in sb or "没成功" in sb, sb)
rep_done = AQ.format_report(s)
check("作业结果能直接喂 format_report（共用口径）",
      "某分类" in rep_done and "AAAA1111" in rep_done, rep_done[:160])
check("作业报告不印出 'None'（elapsed_s 缺失时用 age_s）",
      "None" not in rep_done, rep_done[-120:])
check("作业报告末尾标的是作业号", "作业 " + jid in rep_done, rep_done[-120:])

# 快照必须是副本：拿到之后作业继续更新，不能把已给出的快照改掉
before = AQ.job_progress(jid)["results"]
AQ._job_update(jid, results=[])
check("进度快照是副本（作业后续变化不影响已取到的快照）",
      len(before) == 2, f"得 {len(before)}")

# 作业表不会无限增长
for i in range(AQ._JOB_KEEP + 5):
    AQ._job_new(1, [], False)
with AQ._JOBS_LOCK:
    n = len(AQ._JOBS)
check(f"作业表上限 {AQ._JOB_KEEP} 条（不会无限长）", n <= AQ._JOB_KEEP, f"得 {n}")


print()
print("=" * 66)
print("[G] Zotero 存活探针（把「没开」和「插件没轮询」分开报）")
print("=" * 66)

# 为什么不看插件状态文件就够了：那个文件**每 30 秒才写一次**，Zotero 刚关掉的
# 那段时间里它看起来还是新的 —— 实测因此放行了一个注定失败的任务，
# 干等 30 秒后才报"插件没在轮询"，而真正的原因只是 Zotero 没开。
# 23119 是 Zotero 自己起的连接器端口，开没开一试便知。
_alive = AQ._zotero_alive()
check("_zotero_alive() 返回布尔值（不抛异常）",
      isinstance(_alive, bool), f"得 {type(_alive).__name__}: {_alive!r}")
print(f"        （本机当前：Zotero {'在运行' if _alive else '没运行'}）")

check("探针用的是 23119（不是知识库的 8765）",
      "23119" in AQ._zotero_alive.__code__.co_consts.__str__(),
      "常量里没看到 23119")
check("探针有超时（Zotero 没开时不能挂住）",
      AQ._zotero_alive.__code__.co_consts.__str__().find("4") >= 0
      or "timeout" in AQ._zotero_alive.__doc__,
      "看不到超时设置")


print()
print("=" * 66)
print(f"通过 {PASS}　失败 {FAIL}")
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
