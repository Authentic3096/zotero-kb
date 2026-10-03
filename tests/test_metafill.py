# ⚠ 与 tests/ 下其它测试一致：凡断言"期望值"的地方，**只用格式中性的合成
#   正文**（编造的刊名/卷期/DOI），不把本机任何一篇文献的标题、DOI、正文
#   写进这个文件。原因见下方说明。
#
#   为什么规则测试也要用合成正文，而不是顺手拿库里的真条目当期望值：
#   一旦把真实条目和它的字段值写成断言，这个文件就成了"用户研究领域的清单"
#   （测试文件会被提交、被分享、被别的会话读到）。所以这里的分工是：
#     · 规则正确性  → 合成正文（可复现、不涉密、换台机器也能跑）
#     · 端到端接通  → 只跑本机库，**只断言格式与不变量**，不写死任何真实值
#
#   文档里的示例同理（README 等给陌生人看的），与测试不一致是故意的。

"""metafill 的自检：规则抽得对不对、只填空的原则有没有被破坏、模型挂了会不会炸。

分三层：
  [A] 纯规则：合成正文 + 断言抽出来的值。不碰 Zotero、不碰模型，跑得飞快。
  [B] suggest() 的行为：用假 item/假 reader 注入正文，验证三条红线
      （只填空、带证据、不写回）和模型不可用时的降级。
  [C] 本机库冒烟：真的扫一遍、真的跑一篇 suggest，只查格式与不变量。
      本机没有 Zotero 库时自动跳过（不让这个测试在别人机器上失败）。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import metafill as M  # noqa: E402

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


# ---------------------------------------------------------------- [A] 纯规则

print("[A] 规则抽取（合成正文，不碰 Zotero / 模型）")

DOI_CASES = [
    # (说明, 正文, 期望抽出的纯 DOI)
    ("裸 DOI", "Article info 10.1234/j.example.2025.001 (c) 2025",
     "10.1234/j.example.2025.001"),
    ("doi: 前缀", "关键词:测试 doi:10.1234/j.example.2025.001 中图分类号:X",
     "10.1234/j.example.2025.001"),
    ("DOI: 前缀带空格", "DOI: 10.1234/j.example.2025.001", "10.1234/j.example.2025.001"),
    ("https://doi.org/ 外壳", "To link: https://doi.org/10.1234/j.example.2025.001",
     "10.1234/j.example.2025.001"),
    ("doi.org/ 无协议", "see doi.org/10.1234/j.example.2025.001 for detail",
     "10.1234/j.example.2025.001"),
    ("DOI 被 PDF 拆出空格", "Doi:10. 1234 / j. example. 2025. 001 收稿日期:2025-06-03",
     "10.1234/j.example.2025.001"),
    ("末尾粘了句号", "见 10.1234/j.example.2025.001. 下一句", "10.1234/j.example.2025.001"),
    ("Digital Object Identifier 写法", "Digital Object Identifier 10.1109/EXAMPLE.2020.3031740",
     "10.1109/EXAMPLE.2020.3031740"),
]
for label, text, want in DOI_CASES:
    got = M._extract_doi(text)
    check(f"DOI / {label}", got is not None and got[0] == want,
          f"得到 {got[0] if got else None!r}，期望 {want!r}")

check("DOI / 抽不到时返回 None", M._extract_doi("本文没有任何标识符") is None)
# 注册号不足 4 位的不算 DOI（避免把 `10.1/xxx` 这种正文片段当 DOI）
check("DOI / 注册号不足 4 位不认", M._extract_doi("版本 10.1/build-2 发布") is None)

# 日期用例：(说明, 正文, 期望值, 期望形态分, 期望日期类型)
#
# ⚠ 第 5 项"日期类型"是本轮策略改动的新要求：投稿/收稿/网络首发日期不再是
#   "宁可留空"，而是**采纳但要标出类型**（published / online_first / submitted）。
#   所以这里同时断言类型 —— 只断言值的话，"把收稿日期当正式出版日期填进去"
#   这个老 bug 会重新变得测不出来。
DATE_CASES = [
    ("中文年月", "某某学报 2025 年 12 月 第 4 期", "2025-12", 3, "published"),
    # 原来写的是 `出版:2025 年 3 月 29 日`。改成 `出版日期:` 是**故意的**：
    # 上一版把 `出版日期` 也列进了"投稿日期丢弃词"，等于把最明确的出版标签丢掉，
    # 本轮一并改正 —— 这条用例现在同时在守这件事。
    ("中文年月日", "出版日期:2025 年 3 月 29 日", "2025-03-29", 2, "published"),
    ("ISO 年月日", "Published 2025-03-29 by X", "2025-03-29", 2, "published"),
    ("斜杠年月", "Published 2025/03", "2025-03", 3, "published"),
    ("英文月份缩写", "IEEE Trans, Dec. 2025, pp. 1-2", "2025-12", 3, "published"),
    ("英文月份全称", "Journal of Testing, June 2024, Vol. 12", "2024-06", 3,
     "published"),
    # ↓ 这条原来是 `("英文日月年", "Received 24 March 2025; accepted 1 April 2025",
    #   None, 0)` —— 旧策略下"只有投稿日期"必须给 None。策略改了之后期望值
    #   必须跟着改：**有值、且类型标成投稿日期**。这不是把测试改成迁就实现，
    #   而是把"用户拍板的新行为"钉住（理由见 metafill 模块头）。
    ("只有英文投稿日期（现在采纳，标 submitted）",
     "Received 24 March 2025; accepted 1 April 2025", "2025-03", 3, "submitted"),
]
for label, text, want, want_score, want_kind in DATE_CASES:
    got = M._extract_date(text)
    check(f"日期 / {label}",
          got is not None and got[0] == want and got[3] == want_score
          and got[4] == want_kind,
          f"得到 {got!r}，期望值 {want!r} 分 {want_score} 类型 {want_kind!r}")

# ---- 本轮策略改动：投稿 / 收稿 / 网络首发日期都采纳，但要标出是哪一种
#     为什么改：date 只用来算"按发表年份的基础权重"（差一两年影响很小），
#     而留空会让整篇文献丢掉初始权重。留空反而更亏。
DATE_FALLBACK_CASES = [
    ("只有收稿日期", "收稿日期: 2025-06-03 某某学报 第 4 期",
     "2025-06-03", "submitted"),
    ("只有投稿日期（中文另一种写法）", "投稿日期：2025-07-15 某某学报",
     "2025-07-15", "submitted"),
    ("只有网络首发日期", "网络首发日期: 2025-08-21 某某学报",
     "2025-08-21", "online_first"),
    ("Available online", "Available online 24 March 2025",
     "2025-03", "online_first"),
    # 没带任何已知标签的日期仍旧算正式出版：期刊刊头上一大半日期是不带标签的，
    # 把它们降级成 unknown 会让排序失去依据（见 classify_date_kind 的说明）。
    ("无标签的刊头日期算正式出版", "某某学报 第 4 期 2025 年 12 月",
     "2025-12", "published"),
]
for label, text, want, want_kind in DATE_FALLBACK_CASES:
    got = M._extract_date(text)
    check(f"日期采纳 / {label}",
          got is not None and got[0] == want and got[4] == want_kind,
          f"得到 {got!r}，期望值 {want!r} 类型 {want_kind!r}")

# 类型判定要取**离日期最近**的那个标签：中文期刊首页常把
# `网络首发日期: … 收稿日期: …` 并排印在同一行，用"谁先出现算谁"的话
# 第二个日期会被前一个标签污染成网络首发。
_adjacent = "网络首发日期: 2025-08-21  收稿日期: 2025-04-14"
check("日期类型 / 并排标签时按最近的算（后面的收稿日期不被前面的首发标签污染）",
      M.classify_date_kind(_adjacent, _adjacent.rindex("2025-04-14")) == "submitted",
      f"得到 {M.classify_date_kind(_adjacent, _adjacent.rindex('2025-04-14'))!r}")
check("日期类型 / 最近的标签是首发就标首发",
      M.classify_date_kind(_adjacent, _adjacent.index("2025-08-21")) == "online_first",
      f"得到 {M.classify_date_kind(_adjacent, _adjacent.index('2025-08-21'))!r}")
check("日期类型 / 中文标签与英文枚举都能归一",
      M._normalize_kind("网络首发") == "online_first"
      and M._normalize_kind("SUBMITTED") == "submitted"
      and M._normalize_kind("published") == "published"
      and M._normalize_kind("看不懂的") == "",
      f"得到 {M._normalize_kind('网络首发')!r} / {M._normalize_kind('看不懂的')!r}")

# 年月（分 3）要压过年月日（分 2）：中文期刊首页常同时印"收稿日期"和出版年月，
# 这条保证选中的是出版年月。这是本模块最容易错的地方，必须有测试守着。
_mixed = "收稿日期:2025-06-03 某某学报 2025 年 12 月 第 4 期"
check("日期 / 出版年月压过收稿日期",
      M._extract_date(_mixed)[0] == "2025-12",
      f"得到 {M._extract_date(_mixed)}")
# 光"选对了值"还不够：类型也要是正式出版，否则界面会提示"这是收稿日期"，
# 用户就会被误导着不敢用这个其实正确的值。
check("日期 / 出版年月压过收稿日期时类型标 published",
      M._extract_date(_mixed)[4] == "published",
      f"得到 {M._extract_date(_mixed)}")

# 只有收稿日期 + 网络首发日期时，**网络首发优先**（它更接近正式出版）。
# 这一条和上面那条合起来说明：类型是"优先级"而不是"过滤器"。
_both_submit = "收稿日期: 2025-04-14 网络首发日期: 2025-08-21"
check("日期 / 收稿与网络首发并存时选网络首发",
      M._extract_date(_both_submit)[:1] == ("2025-08-21",)
      and M._extract_date(_both_submit)[4] == "online_first",
      f"得到 {M._extract_date(_both_submit)}")

VIP_CASES = [
    ("中文刊头", "第 37 卷 第 4 期 2025 年 12 月 第 765-770 页",
     {"volume": "37", "issue": "4", "pages": "765-770"}),
    ("英文刊头", "Vol. 37, No. 4, Dec. 2025, pp. 765-770",
     {"volume": "37", "issue": "4", "pages": "765-770"}),
    ("Vol. 无点", "VOL 37 NO 4 pp 765-770",
     {"volume": "37", "issue": "4", "pages": "765-770"}),
    ("卷(期):页", "Journal of Testing, 37(4): 765-770",
     {"volume": "37", "issue": "4", "pages": "765-770"}),
    ("卷(期):页 有空格", "Journal, 42( 4) :68-83", {"volume": "42", "issue": "4", "pages": "68-83"}),
    ("带年份的引用格式", "Progress in Testing, 2025, 37(12): 1846~1865",
     {"volume": "37", "issue": "12", "pages": "1846-1865"}),
    ("文章编号", "文章编号:1000-0000(2024)03-0296-05",
     {"issue": "3", "pages": "296-300"}),
    ("只有 pp.", "Reprinted, pp. 12-34", {"pages": "12-34"}),
    ("中文页码", "本文见第 765-770 页", {"pages": "765-770"}),
]
for label, text, want in VIP_CASES:
    got = {k: v[0] for k, v in M._extract_volume_issue_pages(text).items()}
    check(f"卷期页 / {label}", got == want, f"得到 {got}，期望 {want}")

# `第 14 卷 第 1 期`：中文刊头最容易抓错的一处 —— "第...期" 的正则要能
# 越过"第 14 卷"里的那个"第"，否则期号会抓成 14。
_cn = {k: v[0] for k, v in M._extract_volume_issue_pages("第 14 卷 第 1 期").items()}
check("卷期页 / 中文卷期不互串", _cn == {"volume": "14", "issue": "1"}, f"得到 {_cn}")

# ---- 假阳性防线：这两条是实测踩到的真 bug，不是假想
_doi_noise = "CN 11-6037/Z 网络首发 DOI:10.1234/j.example.2024.1145 合成标题占位"
check("假阳性 / CN 号里的数字不被拼成日期",
      "2024-11" not in [c[0] for c in M._date_candidates(_doi_noise)],
      f"候选={[c[0] for c in M._date_candidates(_doi_noise)]}")
_url_noise = "To link: https://doi.org/10.1234/j.example.2025.118265"
check("假阳性 / URL 里的数字不被拼成日期",
      M._date_candidates(_url_noise) == [],
      f"候选={M._date_candidates(_url_noise)}")
check("假阳性 / ISSN 号不被当成日期",
      M._date_candidates("ISSN 2096-4188,CN 11-6037/Z") == [],
      f"候选={M._date_candidates('ISSN 2096-4188,CN 11-6037/Z')}")

# 网络首发日期是**合法出版日期**（中文期刊网络首发论文只有它），不能当投稿日期丢掉。
# 旧策略是靠"不收录 `首发` 标签"侥幸做到的；新策略下它是**类型优先级第二档**，
# 所以这条要继续守着，并且要断言它没有被降级成投稿日期。
_online_first = "收稿日期: 2025-04-14 网络首发日期: 2025-08-21 引用格式: 张某某.测试"
check("日期 / 网络首发日期可用（不算投稿日期）",
      M._extract_date(_online_first)[0] == "2025-08-21"
      and M._extract_date(_online_first)[4] == "online_first",
      f"得到 {M._extract_date(_online_first)}")

# ---- 值清理：模型给的脏值要能洗干净
CLEAN_CASES = [
    ("volume", "第 37 卷", "37"),
    ("volume", "Vol. 37", "37"),
    ("issue", "第 4 期", "4"),
    ("issue", "No.4", "4"),
    ("pages", "765–770", "765-770"),
    ("pages", "765~770", "765-770"),
    ("pages", "pp. 765-770", "765-770"),
    ("DOI", "doi: 10.1234/j.example.2025.001", "10.1234/j.example.2025.001"),
    ("DOI", "https://doi.org/10.1234/j.example.2025.001", "10.1234/j.example.2025.001"),
    ("date", "2025 年 12 月", "2025-12"),
    ("date", "Dec. 2025", "2025-12"),
    ("date", "2025-3-9", "2025-03-09"),
]
for field, raw, want in CLEAN_CASES:
    got = M._clean_value(field, raw)
    check(f"清理 / {field} {raw!r}", got == want, f"得到 {got!r}，期望 {want!r}")
check("清理 / 垃圾值返回空串", M._clean_value("volume", "无") == "")
check("清理 / 清不成合法 DOI 就丢弃", M._clean_value("DOI", "不是DOI") == "")


# ---- 作者：姓名拆分是这里最容易出错的一步，逐条钉住
#
# 这一节全是**合成姓名**（不用库里任何真实作者）。判据见 split_name 的说明：
# 拆不准就整名放 lastName，宁可少拆不可拆错。
SPLIT_CASES = [
    # (原始串, 期望 firstName, 期望 lastName, 这条在守什么)
    ("张三", "", "张三", "中文名整名放 lastName（Zotero 里就这么存）"),
    ("欧阳修文", "", "欧阳修文", "复姓中文名同样不拆"),
    ("San Zhang", "San", "Zhang", "西文 First Last"),
    ("Zhang, San", "San", "Zhang", "西文 Last, First"),
    ("ZHANG San", "San", "ZHANG", "全大写词是姓（中文期刊英文作者行的惯例）"),
    ("S. Zhang", "S.", "Zhang", "缩写名保留句点"),
    ("Jean-Luc Picard", "", "Jean-Luc Picard", "带连字符 → 不拆"),
    ("John Smith Jr.", "", "John Smith Jr.", "带后缀 → 不拆（拆了会把 Jr. 当姓）"),
    ("van der Waals", "", "van der Waals", "带姓氏小品词 → 不拆"),
    ("Smith", "", "Smith", "只有一个词 → 不拆"),
    ("MA ZHAN", "", "MA ZHAN", "两个全大写词 → 分不清谁姓，不拆"),
    ("", "", "", "空串不炸"),
]
for raw, want_first, want_last, why in SPLIT_CASES:
    got = M.split_name(raw)
    check(f"姓名拆分 / {raw!r}（{why}）", got == (want_first, want_last),
          f"得到 {got!r}，期望 {(want_first, want_last)!r}")

# 角标与通讯作者标记必须清掉，但**句点不能顺手去掉**（`S.` 的句点是有意义的）
MARKS_CASES = [
    ("张三1", "张三"), ("李四1,2", "李四"), ("王五2*", "王五"),
    ("*赵六", "赵六"), ("†钱七", "钱七"), ("San Zhang", "San Zhang"),
    ("S.", "S."),
]
for raw, want in MARKS_CASES:
    got = M._strip_name_marks(raw)
    check(f"姓名清洗 / {raw!r}", got[0] == want, f"得到 {got!r}，期望 {want!r}")
check("姓名清洗 / 带标记时第二项为 True",
      M._strip_name_marks("张三1")[1] is True
      and M._strip_name_marks("张三")[1] is False)

LINE_CASES = [
    ("中文期刊作者行（带角标与通讯作者标记）", "张三1, 李四2, 王五1*",
     ["张三", "李四", "王五"], False),
    ("英文作者行（and 连接）", "San Zhang, Ming Li, and Wei Wang",
     ["San Zhang", "Ming Li", "Wei Wang"], False),
    ("英文缩写名", "S. Zhang, M. Li", ["S. Zhang", "M. Li"], False),
    ("顿号分隔", "张三、李四", ["张三", "李四"], False),
    ("作者行后面跟着机构（括号里的机构要丢掉）", "张三1, 李四2（某某大学某某学院）",
     ["张三", "李四"], False),
]
for why, line, want_names, want_partial in LINE_CASES:
    got = M._names_in_line(line)
    check(f"作者行 / {why}",
          got is not None and got[0] == want_names and got[1] is want_partial,
          f"得到 {got!r}，期望 {(want_names, want_partial)}")

# "作者没列全"的两种写法都要认出来 —— 这是必须提示用户的信号
for line, want in (("张三, 李四, 等", True), ("张三, 李四等", True),
                   ("San Zhang, Ming Li, et al.", True),
                   ("San Zhang, Ming Li, et al", True),
                   ("张三, 李四", False)):
    got = M._names_in_line(line)
    check(f"作者没列全 / {line!r} → partial={want}",
          got is not None and got[1] is want, f"得到 {got!r}")

# 不像作者行的行必须返回 None（**整行**不接受，不做"取像的那一半"）
for line, why in (
    ("张三1, 某某大学某某学院", "一半姓名一半机构"),
    ("摘要:本文研究了某某问题", "摘要行"),
    ("1. 某某大学某某学院", "机构行"),
    ("某某某某某某某某某某研究进展", "标题行（太长，不像姓名）"),
    ("张三, 李四, 王五, 赵六, 钱七, 孙八, 周九, 吴十, 郑一, 王二, 冯三, 陈四, "
     "褚五, 卫六, 蒋七, 沈八", "超过 15 项（不可能全是作者）"),
):
    check(f"作者行拒绝 / {why}", M._names_in_line(line) is None,
          f"得到 {M._names_in_line(line)!r}")


# ---------------------------------------------------------------- [B] suggest 行为
#
# 用假的 reader / judge 注入，不依赖本机 Zotero 与 Ollama。这样"只填空"、
# "模型挂了不炸"这些红线在任何机器上都能验。
class _FakeItem:
    def __init__(self, key="FAKEKEY1", title="合成标题", item_type="journalArticle",
                 fields=None, authors=None):
        self.key = key
        self.title = title
        self.item_type = item_type
        self.fields = fields or {}
        # `authors` 是 creators 的"当前值"来源（Zotero 的字段表里没有 creators，
        # 作者存在 Item.authors 里）—— 见 metafill._current_values 的说明。
        self.authors = authors or []
        self.pdfs = []


class _FakeAuthor:
    """只实现 metafill 用到的那一个属性：display（`schemas.Author` 上是个 property）。"""

    def __init__(self, display):
        self.display = display


class _FakeReader:
    """只实现 metafill 用到的那一个方法：fulltext_for。"""

    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def fulltext_for(self, item, **kw):
        self.calls += 1
        return list(self._pages), "fake"


class _FakeJudge:
    """假的 judge 模块：generate 返回预置 JSON，用来验"模型抽的值标 low"。"""

    def __init__(self, payload, ok=True):
        import json
        self._text = json.dumps(payload, ensure_ascii=False)
        self._json = json
        self._ok = ok

    def generate(self, prompt, **kw):
        return {"ok": self._ok, "text": self._text, "model": "fake",
                "backend": "fake", "eval_count": 1}

    def parse_json(self, text):
        # ⚠ judge.parse_json 返回的是 (ok, data) 元组，不是 data 本身 ——
        #   假模块必须照着真签名来，否则测的是假接口的行为
        return True, self._json.loads(text)

    def llm_status(self):
        return {"available": True, "provider": "fake", "hint": ""}


_PAGE = ("某某学报 Vol. 37, No. 4, Dec. 2025, pp. 765-770 "
         "DOI: 10.1234/j.example.2025.001 摘要:合成正文用于自检。" + "填" * 200)

_real_reader = M._get_reader
_real_judge = M.judge
M._get_reader = lambda: _FakeReader([_PAGE] * 2)

print("\n[B] suggest() 的行为（假 reader；模型不可用时退化为纯规则）")

# B1 每篇只加载一次 item + 只读一次正文
_fr = _FakeReader([_PAGE] * 2)
M._get_reader = lambda: _fr
M.judge = None                    # 这一节先只验规则：模型关掉
_res = M.suggest_for_item(_FakeItem())
check("只填空 / 5 个空字段都给出了建议", len(_res["suggestions"]) == 5,
      f"得到 {[s['field'] for s in _res['suggestions']]}")
check("只填空 / 每篇只读一次正文", _fr.calls == 1, f"读了 {_fr.calls} 次")
check("规则抽的值标 high",
      all(s["confidence"] == "high" for s in _res["suggestions"]))
check("规则抽的值标 source=rule",
      all(s["source"] == "rule" for s in _res["suggestions"]))
check("每条建议都带页码与原文", all(
    s["evidence"].get("page") and s["evidence"].get("text")
    for s in _res["suggestions"]))
# ⚠ 这里核对的**不是**"归一化后的值出现在片段里"，而是"原文里命中的那段字面
#   文本完整出现在片段里"。两者不是一回事：日期 `Dec. 2025` 归一化后是
#   `2025-12`，拿后者去片段里搜必然搜不到，但证据其实是完整的。
#   真出过一次 bug：窗口算得太窄，把 `pp. 765-770` 从右边切成了 `pp. 765-`，
#   用户拿到的证据里根本没有那个值 —— 这条断言就是为它设的。
check("证据片段里完整包含命中的原文",
      all(s["evidence"].get("_span") in s["evidence"]["text"]
          for s in _res["suggestions"]),
      str([(s["field"], s["evidence"].get("_span"), s["evidence"]["text"][-40:])
           for s in _res["suggestions"]]))

# 这一页的日期是刊头的 `Dec. 2025`（无标签）→ 正式出版。要断言**两件事**：
#   1. 类型字段确实存在（界面按固定结构取值，缺键会 KeyError —— 本项目踩过）；
#   2. 正式出版日期不弹"这不是出版日期"的提示（乱提示比不提示更烦人）。
_date_sug = [s for s in _res["suggestions"] if s["field"] == "date"]
check("日期类型 / 规则建议带 date_kind 且刊头日期算正式出版",
      _date_sug and _date_sug[0].get("date_kind") == "published"
      and _date_sug[0].get("date_kind_label"),
      str(_date_sug[0] if _date_sug else None))
check("日期类型 / 正式出版日期不加多余提示",
      _date_sug and not _date_sug[0]["evidence"].get("note"),
      str(_date_sug[0]["evidence"] if _date_sug else None))

# B2 已经有值的字段绝不能出现在 suggestions 里
_have = _FakeItem(fields={"date": "2025-12", "volume": "37"})
_res2 = M.suggest_for_item(_have)
_sug_fields = {s["field"] for s in _res2["suggestions"]}
check("只填空 / 已有值的字段不出现在建议里",
      "date" not in _sug_fields and "volume" not in _sug_fields, str(_sug_fields))
check("只填空 / 已有值的字段进 skipped 且写明原因",
      any(s["field"] == "date" and "已有值" in s["why"] for s in _res2["skipped"]),
      str(_res2["skipped"]))
check("只填空 / 已有值时仍补其它空字段",
      {"issue", "pages", "DOI"} <= _sug_fields, str(_sug_fields))

# B3 不修改任何数据：跑完 item.fields 必须原样
_before = dict(_have.fields)
M.suggest_for_item(_have)
check("不写回 / item.fields 未被改动", _have.fields == _before,
      f"{_before} -> {_have.fields}")

# B4 全部字段都有值 → 不读正文、不给建议
#    ⚠ `creators` 也算一个字段：它的"有值"体现在 item.authors 上，
#      所以要把作者一起造出来，才算真的"全部有值"。
_full = _FakeItem(fields={"date": "2025-12", "DOI": "10.1234/j.example.2025.001",
                          "volume": "37", "issue": "4", "pages": "765-770"},
                  authors=[_FakeAuthor("张三"), _FakeAuthor("李四")])
_fr2 = _FakeReader([_PAGE])
M._get_reader = lambda: _fr2
_res3 = M.suggest_for_item(_full)
check("全部已有值时不给建议", _res3["suggestions"] == [])
check("全部已有值时不去读正文（省一次 PDF 解析）", _fr2.calls == 0,
      f"读了 {_fr2.calls} 次")
check("全部已有值时仍然 ok", _res3["ok"] is True)

# B5 模型路径：值标 low，清洗照样生效，证据要能对上原文
#
# 这一页**故意不含** Vol./No./卷/期 这些规则能认的标记（只有孤立一个"第 3 期"，
# 规则不认），逼着走模型分支。
M._get_reader = lambda: _FakeReader(
    ["某某大学学报 第 3 期 摘要:合成正文。" + "填" * 250] * 2)
M.judge = _FakeJudge({
    "date": "2025-12", "DOI": "10.1234/j.example.2025.001",
    "volume": "第 37 卷", "issue": "第 3 期", "pages": "pp. 765-770",
    "evidence": {"date": "某某大学学报", "DOI": "某某大学学报",
                 "volume": "某某大学学报", "issue": "某某大学学报",
                 "pages": "某某大学学报"}})
_res4 = M.suggest_for_item(_FakeItem())
_by_field = {s["field"]: s for s in _res4["suggestions"]}
check("模型抽的值标 low",
      all(s["confidence"] == "low" for s in _res4["suggestions"]
          if s["source"] == "model"),
      str([(s["field"], s["source"], s["confidence"]) for s in _res4["suggestions"]]))
check("模型抽的值 source=model",
      any(s["source"] == "model" for s in _res4["suggestions"]),
      str(_res4["suggestions"]))
check("模型建议也带原文证据",
      all(s["evidence"].get("text") for s in _res4["suggestions"]),
      str([(s["field"], s["evidence"]) for s in _res4["suggestions"]]))
# 模型给的值照样要清洗：`第 37 卷` → `37`、`pp. 765-770` → `765-770`
check("模型给的脏值会被清洗成 Zotero 形态",
      _by_field.get("volume", {}).get("value") == "37"
      and _by_field.get("pages", {}).get("value") == "765-770"
      and _by_field.get("issue", {}).get("value") == "3",
      str({f: _by_field.get(f, {}).get("value")
           for f in ("volume", "issue", "pages")}))

# 模型编不出处的证据 → 标 verified=False，让用户知道要自己核
M.judge = _FakeJudge({
    "date": "", "DOI": "", "volume": "37", "issue": "",
    "pages": "", "evidence": {"volume": "这段原文根本不在正文里哦"}})
_res5 = M.suggest_for_item(_FakeItem())
_vol = [s for s in _res5["suggestions"] if s["field"] == "volume"]
check("模型编的原文会被标 verified=False",
      _vol and _vol[0]["evidence"].get("verified") is False,
      str(_vol[0]["evidence"]) if _vol else "没抽到 volume")

# B6 模型不可用 / 抛异常 → 必须优雅退化成纯规则，不能抛
#
# ⚠ 先换回标准合成页：上面 B5 为了逼出模型分支换了另一页（那页没有
#   Vol./pp. 这些规则能认的标记）。忘了换回来的话，这里的期望值会莫名其妙
#   变成 1 条 —— 自检真的这么错过一次。
M._get_reader = lambda: _FakeReader([_PAGE] * 2)
M.judge = None
_res6 = M.suggest_for_item(_FakeItem())
check("模型不可用时不抛异常且规则结果照常返回",
      _res6["ok"] is True and len(_res6["suggestions"]) == 5,
      f"ok={_res6['ok']} n={len(_res6['suggestions'])} "
      f"{[s['field'] for s in _res6['suggestions']]}")
check("模型不可用时状态写明用了没有",
      _res6["model"]["used"] is False, str(_res6.get("model")))
check("模型不可用时状态里写明原因（给用户一句人话）",
      _res6["model"]["used"] is False and _res6["model"].get("reason"),
      str(_res6["model"]))

# B6b 日期类型要一路传到建议里 —— 这是本轮策略改动的**验收点**：
#     采纳了投稿日期，就必须让用户在弹窗里看出"这是收稿日期，不是出版日期"。
#     值传对了但类型没传，等于把判断责任推给一个看不见的理由。
_ONLY_SUBMIT_PAGE = ("某某学报 第 4 期 收稿日期: 2025-06-03 摘要:合成正文。"
                     + "填" * 260)
M._get_reader = lambda: _FakeReader([_ONLY_SUBMIT_PAGE] * 2)
M.judge = None
_res6b = M.suggest_for_item(_FakeItem())
_d6 = [s for s in _res6b["suggestions"] if s["field"] == "date"]
check("日期类型 / 只有收稿日期时照样给建议（不再宁可留空）",
      _d6 and _d6[0]["value"] == "2025-06-03", str(_d6[0] if _d6 else None))
check("日期类型 / 收稿日期标成 submitted 并带中文说明",
      _d6 and _d6[0].get("date_kind") == "submitted"
      and "收稿" in (_d6[0].get("date_kind_label") or ""),
      str(_d6[0] if _d6 else None))
check("日期类型 / 证据里也写清这是哪种日期（顺带展示证据的地方能看到）",
      _d6 and "收稿" in (_d6[0]["evidence"].get("note") or ""),
      str(_d6[0]["evidence"] if _d6 else None))

# B6c 模型兜底路径的日期类型。这里刻意用一页**规则抽不出日期**的正文
#     （正文里没有任何能匹配的日期形态），逼着走模型分支。
_MODEL_PAGE = "某某大学学报 收稿日期（详见版权页） 摘要:合成正文。" + "填" * 260
M._get_reader = lambda: _FakeReader([_MODEL_PAGE] * 2)

# 模型自称 published，但它给的证据原文里写着"收稿日期" —— 类型要按**证据**算。
# 为什么以证据为准：证据是它在正文里指出来的那段字（属于原文级信息），
# 自我归类只是它的说法，小模型在这类标签上并不可靠。
M.judge = _FakeJudge({
    "date": "2025-12", "date_kind": "published",
    "DOI": "", "volume": "", "issue": "", "pages": "",
    "evidence": {"date": "收稿日期（详见版权页）"}})
_res6c = M.suggest_for_item(_FakeItem())
_d6c = [s for s in _res6c["suggestions"] if s["field"] == "date"]
check("日期类型 / 模型路径：证据原文优先于模型自述的类型",
      _d6c and _d6c[0].get("date_kind") == "submitted",
      str(_d6c[0] if _d6c else None))

# 证据里看不出标签时，退到模型声明的类型（中文标签也要认）
M.judge = _FakeJudge({
    "date": "2025-12", "date_kind": "网络首发",
    "DOI": "", "volume": "", "issue": "", "pages": "",
    "evidence": {"date": "某某大学学报"}})
_res6d = M.suggest_for_item(_FakeItem())
_d6d = [s for s in _res6d["suggestions"] if s["field"] == "date"]
check("日期类型 / 模型路径：证据没标签时用模型声明的类型（中文标签也认）",
      _d6d and _d6d[0].get("date_kind") == "online_first",
      str(_d6d[0] if _d6d else None))

# 模型既没给标签、证据也看不出 → unknown（**不猜**成正式出版）
M.judge = _FakeJudge({
    "date": "2025-12", "date_kind": "",
    "DOI": "", "volume": "", "issue": "", "pages": "",
    "evidence": {"date": "某某大学学报"}})
_res6e = M.suggest_for_item(_FakeItem())
_d6e = [s for s in _res6e["suggestions"] if s["field"] == "date"]
check("日期类型 / 模型路径：判不出来时标 unknown（不猜）",
      _d6e and _d6e[0].get("date_kind") == "unknown",
      str(_d6e[0] if _d6e else None))
check("日期类型 / 模型给的 date_kind 不会被当成一个待补字段冒出来",
      all(s["field"] in M.FIELDS for s in _res6e["suggestions"]),
      str([s["field"] for s in _res6e["suggestions"]]))

# ---- B6f 作者（creators）：本轮新增的字段，四类形态都要有端到端用例
#
# 页面都是合成的。作者行在版式上就在标题下面，所以合成页也照这个版式来。
print("\n[B2] creators（作者）的抽取与拒绝")

# (1) 中文期刊作者行：逗号分隔 + 机构角标 + 通讯作者标记
_CN_PAGE = ("某某某某某某方法研究\n"
            "张三1, 李四2, 王五1*\n"
            "1. 某某大学某某学院　2. 某某研究所\n"
            "摘要:合成正文用于自检。" + "填" * 220)
M._get_reader = lambda: _FakeReader([_CN_PAGE] * 2)
M.judge = None
_res_cn = M.suggest_for_item(_FakeItem(title="某某某某某某方法研究"))
_cn = [s for s in _res_cn["suggestions"] if s["field"] == "creators"]
check("作者 / 中文期刊作者行能抽到（规则、high）",
      _cn and _cn[0]["source"] == "rule" and _cn[0]["confidence"] == "high",
      str(_cn[0] if _cn else [s["field"] for s in _res_cn["suggestions"]]))
check("作者 / 中文姓名整名放 lastName、firstName 留空",
      _cn and [c["lastName"] for c in _cn[0]["values"]] == ["张三", "李四", "王五"]
      and all(c["firstName"] == "" for c in _cn[0]["values"]),
      str(_cn[0]["values"] if _cn else None))
check("作者 / 角标与通讯作者标记被清掉",
      _cn and all(c["lastName"] in ("张三", "李四", "王五") for c in _cn[0]["values"]),
      str(_cn[0]["values"] if _cn else None))
check("作者 / value 是给人看的字符串、values 是给写回用的列表",
      _cn and isinstance(_cn[0]["value"], str) and _cn[0]["value"] == "张三; 李四; 王五"
      and isinstance(_cn[0]["values"], list) and len(_cn[0]["values"]) == 3
      and all(c["creatorType"] == "author" for c in _cn[0]["values"]),
      str(_cn[0] if _cn else None))
check("作者 / 证据是作者那一行的原文（能看出名字从哪来）",
      _cn and _cn[0]["evidence"].get("page") == 1
      and "张三" in _cn[0]["evidence"].get("text", ""),
      str(_cn[0]["evidence"] if _cn else None))
check("作者 / 没列全时不加 partial 与 warning（列全了就别说没列全）",
      _cn and not _cn[0].get("partial") and not _cn[0].get("warning"),
      str(_cn[0] if _cn else None))

# (1a) 中英两行同时印 → 选**中文行**（用户拍板"优先中文行"）
#
# 版式刻意造成"英文行的原始分**更高**"，这样才真的在验新判据：
#   · 中文行在标题下一行，标题只有 4 个字 → 拿不到"上一行像标题"的 +1 → 原始分 3
#   · 英文行在中文作者行下面，上一行够长        → 拿到 +1              → 原始分 4
# 没有「优先中文行」时英文行 4 > 中文行 3，会选中英文行；
# 加了 +2 之后中文行 5 > 4，选中的是中文行。
_BOTH_LANG_PAGE = ("某某研究\n"
                   "张三1, 李四2\n"
                   "San Zhang1, Ming Li2\n"
                   "摘要:合成正文用于自检。" + "填" * 210)
_both_lines = M._head_lines(M.head_text([_BOTH_LANG_PAGE] * 2, M.HEAD_PAGES))
_cn_i = next(i for i, (l, _s, _e) in enumerate(_both_lines) if "张三" in l)
_en_i = next(i for i, (l, _s, _e) in enumerate(_both_lines) if "San Zhang" in l)
check("作者优先中文 / 场景前置条件：中文行在前、英文行在后",
      _cn_i < _en_i, f"中文行 {_cn_i}，英文行 {_en_i}")
_cr_both = M.extract_creators([_BOTH_LANG_PAGE] * 2, title="某某研究")
check("作者优先中文 / 中英两行都在时选中文行",
      _cr_both is not None
      and [c["lastName"] for c in _cr_both["creators"]] == ["张三", "李四"],
      str(_cr_both))
# 光断言"选到了中文行"还不够 —— 得证明它**是靠偏好赢的**，不是本来分就高：
# `score` 是"够不够格"的依据，中文行的 score=3 是**低于**英文行那 4 分的，
# 赢在 cjk_names 带来的 +2。
check("作者优先中文 / 中文行原始分低于英文行（确实是靠偏好 +2 赢的）",
      _cr_both is not None and _cr_both["score"] == 3
      and _cr_both["cjk_names"] is True,
      str(_cr_both))
M._get_reader = lambda: _FakeReader([_BOTH_LANG_PAGE] * 2)
M.judge = None
_res_both = M.suggest_for_item(_FakeItem(title="某某研究"))
_cb = [s for s in _res_both["suggestions"] if s["field"] == "creators"]
check("作者优先中文 / 端到端建议里也是中文行",
      _cb and [c["lastName"] for c in _cb[0]["values"]] == ["张三", "李四"],
      str(_cb[0]["values"] if _cb else None))

# (1b) 只有英文行 → **照旧能抽**（防止"优先中文"把英文文献搞坏）。
#      判据是加分而不是"英文减分"，所以纯英文文献里英文行是唯一候选，
#      按原分数照样过门槛 —— 这两条就是守这件事的。
_EN_ONLY_PAGE = ("A Study of Something Synthetic\n"
                 "San Zhang, Ming Li\n"
                 "Abstract: synthetic text for self-check. " + "x" * 210)
M._get_reader = lambda: _FakeReader([_EN_ONLY_PAGE] * 2)
_res_eo = M.suggest_for_item(_FakeItem(
    item_type="journalArticle", title="A Study of Something Synthetic"))
_eo = [s for s in _res_eo["suggestions"] if s["field"] == "creators"]
check("作者优先中文 / 只有英文行时照旧抽到（纯英文文献不受影响）",
      _eo and [c["lastName"] for c in _eo[0]["values"]] == ["Zhang", "Li"],
      str(_eo[0]["values"] if _eo else None))
# 英文作者行**旁边**有中文机构行时，不能被误判成中文行 ——
# 判据取的是"姓名含汉字"，不是"整行含汉字"（见 RE_NAME_CJK 的说明）。
_MIXED_PAGE = ("A Study of Something Synthetic\n"
               "San Zhang1, Ming Li2\n"
               "1. 某某大学某某学院\n"
               "Abstract: synthetic text for self-check. " + "x" * 210)
_cr_mixed = M.extract_creators([_MIXED_PAGE] * 2,
                               title="A Study of Something Synthetic")
check("作者优先中文 / 英文行旁边的中文机构不会把它算成中文行",
      _cr_mixed is not None and _cr_mixed["cjk_names"] is False
      and [c["lastName"] for c in _cr_mixed["creators"]] == ["Zhang", "Li"],
      str(_cr_mixed))

# (2) 中文学位论文封面：带标签的作者行 + 西文姓名拆分
_CN_THESIS_PAGE = ("作者姓名: San Zhang\n"
                   "指导教师: Ming Li 教授\n"
                   "学科专业: 某专业\n"
                   "摘要:合成正文。" + "填" * 230)
M._get_reader = lambda: _FakeReader([_CN_THESIS_PAGE] * 2)
_res_th = M.suggest_for_item(_FakeItem(item_type="thesis", title="某学位论文"))
_th = [s for s in _res_th["suggestions"] if s["field"] == "creators"]
check("作者 / 带标签的作者行（作者姓名:）能抽到",
      _th and _th[0]["n_creators"] == 1, str(_th[0] if _th else None))
check("作者 / 西文姓名拆成 firstName/lastName",
      _th and _th[0]["values"] == [{"lastName": "Zhang", "firstName": "San",
                                    "creatorType": "author"}],
      str(_th[0]["values"] if _th else None))
check("作者 / 证据片段里带着标签（用户一眼看出这是从哪抄的）",
      _th and "作者姓名" in _th[0]["evidence"].get("text", ""),
      str(_th[0]["evidence"] if _th else None))
check("作者 / 指导教师那一行不会被当成作者行",
      _th and _th[0]["n_creators"] == 1, str(_th[0]["values"] if _th else None))

# (2b) 英文学位论文封面：`by` 单独占一行，作者名在下一行（单个姓名）
_EN_THESIS_PAGE = ("A Study of Something Synthetic\n"
                   "by\n"
                   "San Zhang\n"
                   "A dissertation submitted for the degree of Doctor\n"
                   "Abstract: synthetic text for self-check. " + "x" * 200)
M._get_reader = lambda: _FakeReader([_EN_THESIS_PAGE] * 2)
_res_en = M.suggest_for_item(_FakeItem(item_type="thesis",
                                       title="A Study of Something Synthetic"))
_en = [s for s in _res_en["suggestions"] if s["field"] == "creators"]
check("作者 / 英文学位论文封面（by 单独一行）能抽到单作者",
      _en and _en[0]["n_creators"] == 1, str(_en[0] if _en else None))
check("作者 / 英文学位论文的姓名拆成 firstName/lastName",
      _en and _en[0]["values"] == [{"lastName": "Zhang", "firstName": "San",
                                    "creatorType": "author"}],
      str(_en[0]["values"] if _en else None))
check("作者 / 英文封面里没有标签的单独姓名行不会被误当作者",
      # `A dissertation submitted for the degree of Doctor` 这行必然被拒
      _en and _en[0]["n_creators"] == 1, str(_en[0] if _en else None))

# (3) et al.：只列了前几位 → 必须明确提示
_ETAL_PAGE = ("某某某某某方法研究\n"
              "San Zhang, Ming Li, Wei Wang, et al.\n"
              "摘要:合成正文用于自检。" + "填" * 220)
M._get_reader = lambda: _FakeReader([_ETAL_PAGE] * 2)
_res_et = M.suggest_for_item(_FakeItem(title="某某某某某方法研究"))
_et = [s for s in _res_et["suggestions"] if s["field"] == "creators"]
check("作者 / et al. 时只抄列出来的那几位",
      _et and _et[0]["n_creators"] == 3, str(_et[0] if _et else None))
check("作者 / et al. 时标 partial 并给出人话 warning",
      _et and _et[0].get("partial") is True
      and _et[0].get("warning") and "实际作者可能更多" in _et[0]["warning"],
      str(_et[0] if _et else None))
check("作者 / et al. 的提示也写进证据说明里",
      _et and "实际作者可能更多" in (_et[0]["evidence"].get("note") or ""),
      str(_et[0]["evidence"] if _et else None))
check("作者 / et al. 本身不会被当成一位作者",
      _et and all("et al" not in c["lastName"].lower() for c in _et[0]["values"]),
      str(_et[0]["values"] if _et else None))

# (4) 已有任何一位作者 → **整条**不给建议（不做"补第 N 位"式的合并）
M._get_reader = lambda: _FakeReader([_CN_PAGE] * 2)
_res_have = M.suggest_for_item(_FakeItem(authors=[_FakeAuthor("张三")],
                                         title="某某某某某某方法研究"))
_have_fields = {s["field"] for s in _res_have["suggestions"]}
check("作者 / 已有作者时整条不给建议（哪怕正文里能抽到 3 位）",
      "creators" not in _have_fields, str(_have_fields))
check("作者 / 已有作者时进 skipped 并写明「整条跳过」",
      any(s["field"] == "creators" and "整条跳过" in s["why"]
          for s in _res_have["skipped"]),
      str([s for s in _res_have["skipped"] if s["field"] == "creators"]))
check("作者 / 已有作者不影响其它空字段照常补",
      {"date", "DOI", "volume", "issue", "pages"} & _have_fields
      or len(_res_have["skipped"]) >= 1,
      str(_res_have["skipped"]))

# (5) 标题本身长得像姓名列表时，不能被当成作者行
_TITLE_LIKE_PAGE = ("张三, 李四\n王五, 赵六\n摘要:合成正文。" + "填" * 240)
M._get_reader = lambda: _FakeReader([_TITLE_LIKE_PAGE] * 2)
_res_tl = M.suggest_for_item(_FakeItem(title="张三, 李四"))
_tl = [s for s in _res_tl["suggestions"] if s["field"] == "creators"]
check("作者 / 标题与作者行同形时按标题排除（取下面那行）",
      _tl and [c["lastName"] for c in _tl[0]["values"]] == ["王五", "赵六"],
      str(_tl[0]["values"] if _tl else None))

# (6) 模型兜底：规则抓不到作者时问模型。
#     这一页把唯一一行姓名放到了第 30 行之后，规则的位置加分拿不到，
#     总分低于门槛 → 规则放弃，交给模型。
_MODEL_CREATOR_PAGE = ("某某某某某某某方法研究\n"
                       + "填充行用于占位。\n" * 32
                       + "San Zhang\n"
                       + "摘要:合成正文。" + "填" * 150)
M._get_reader = lambda: _FakeReader([_MODEL_CREATOR_PAGE] * 2)
M.judge = _FakeJudge({
    "date": "", "DOI": "", "volume": "", "issue": "", "pages": "",
    "creators": [{"lastName": "Zhang", "firstName": "San"},
                 {"name": "Ming Li"}],
    "creators_partial": True,
    "evidence": {"creators": "这段原文根本不在正文里"}})
_res_mc = M.suggest_for_item(_FakeItem(title="某某某某某某某方法研究"))
_mc = [s for s in _res_mc["suggestions"] if s["field"] == "creators"]
check("作者 / 规则抓不到时走模型兜底（标 model / low）",
      _mc and _mc[0]["source"] == "model" and _mc[0]["confidence"] == "low",
      str(_mc[0] if _mc else [s["field"] for s in _res_mc["suggestions"]]))
check("作者 / 模型给的整名（只有 name 字段）也会被拆",
      _mc and [c["lastName"] for c in _mc[0]["values"]] == ["Zhang", "Li"],
      str(_mc[0]["values"] if _mc else None))
check("作者 / 模型声明没列全时也带 warning",
      _mc and _mc[0].get("partial") is True and _mc[0].get("warning"),
      str(_mc[0] if _mc else None))
check("作者 / 模型路径也有原文证据（这里故意定位不到 → verified=False）",
      _mc and _mc[0]["evidence"].get("verified") is False,
      str(_mc[0]["evidence"] if _mc else None))

# (7) 新增字段不能破坏其余 5 个字段的返回结构：它们的 value 仍是字符串、且没有 values
check("作者 / 其余 5 个字段的返回结构没变（value 仍是字符串）",
      all(isinstance(s["value"], str) and "values" not in s
          for s in _res6e["suggestions"] if s["field"] != "creators"),
      str([(s["field"], type(s["value"]).__name__) for s in _res6e["suggestions"]]))
check("作者 / 建议里出现的字段都在 FIELDS 里（含 creators）",
      all(s["field"] in M.FIELDS for s in _res_cn["suggestions"]),
      str([s["field"] for s in _res_cn["suggestions"]]))

# ⚠ 换回标准合成页：下面是"模型抛异常"与 use_model=False 的用例，它们的期望值
#   都建立在 `_PAGE` 上。忘了换回来会像 B5 那次一样让断言莫名其妙地失败。
M._get_reader = lambda: _FakeReader([_PAGE] * 2)
M.judge = None


class _BoomJudge:
    def generate(self, *a, **kw):
        raise RuntimeError("模拟模型服务炸了")

    def llm_status(self):
        return {"available": True}

    def parse_json(self, text):
        raise RuntimeError("boom")


M.judge = _BoomJudge()
_res7 = M.suggest_for_item(_FakeItem())
check("模型抛异常时不冒泡、规则结果仍返回",
      _res7["ok"] is True and len(_res7["suggestions"]) == 5,
      f"ok={_res7['ok']} n={len(_res7['suggestions'])}")

# B7 use_model=False → 完全不碰模型（连 llm_status 都不该调）
class _NoTouchJudge:
    def __getattr__(self, name):
        raise AssertionError(f"use_model=False 却访问了 judge.{name}")


M.judge = _NoTouchJudge()
_res8 = M.suggest_for_item(_FakeItem(), use_model=False)
check("use_model=False 时完全不碰模型", _res8["ok"] is True,
      str(_res8.get("error")))

# B8 空 key / 找不到 key → 返回结构化错误而不是抛异常
M.judge = _real_judge
check("空 key 返回结构化错误", M.suggest("")["ok"] is False)
M._get_reader = lambda: (_ for _ in ()).throw(RuntimeError("模拟库打不开"))
check("库打不开时 suggest 不抛异常", M.suggest("FAKEKEY1")["ok"] is False,
      str(M.suggest("FAKEKEY1")))

M.judge = _real_judge
M._get_reader = _real_reader


# ---------------------------------------------------------------- [C] 本机库冒烟

print("\n[C] 本机库冒烟（只断言格式与不变量，不写死任何真实值）")
try:
    _scan = M.scan()
except Exception as exc:  # noqa: BLE001
    _scan = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

if not _scan.get("ok") or not _scan.get("total"):
    print(f"  （跳过：本机 Zotero 库不可用 —— {_scan.get('error') or '没有条目'}）")
else:
    check("scan 返回 ok", _scan["ok"] is True)
    check("scan 统计了条目总数", _scan["total"] > 0, str(_scan["total"]))
    check("scan 的每个字段计数不超过总数",
          all(v <= _scan["total"] for v in _scan["per_field"].values()),
          str(_scan["per_field"]))
    check("scan 明细里的缺失字段都合法",
          all(set(r["missing"]) <= set(M.FIELDS) for r in _scan["rows"]))
    check("scan 明细里没有'一个都不缺'的条目",
          all(r["missing"] for r in _scan["rows"]))

    # 真跑一篇：挑"缺字段且正文可读"的第一篇；结果只查格式，不看具体值
    _rows = [r for r in _scan["rows"] if r["n_pdfs"]]
    if _rows:
        _key = _rows[0]["key"]
        _sug = M.suggest(_key, use_model=False)
        check("suggest 返回 ok 且 key 对得上",
              _sug["ok"] is True and _sug["key"] == _key, str(_sug.get("error")))
        check("suggest 只吐出合法字段（current/skipped 都不越界）",
              all(s["field"] in M.FIELDS for s in _sug["skipped"]))
        _sfields = [s["field"] for s in _sug["suggestions"]]
        check("建议的字段都在允许范围内",
              set(_sfields) <= set(M.FIELDS), str(_sfields))
        check("建议的字段不重复", len(_sfields) == len(set(_sfields)), str(_sfields))
        check("建议的字段都不是'当前已有值'的字段",
              not (set(_sfields) & {f for f, v in _sug["current"].items() if v}),
              str(_sug["current"]))
        check("每条建议都有页码(>=1)与原文",
              all(s["evidence"].get("page", 0) >= 1
                  and len(s["evidence"].get("text", "")) >= 4
                  for s in _sug["suggestions"]),
              str([s["evidence"] for s in _sug["suggestions"]]))
        check("规则值标 high、模型值标 low",
              all((s["confidence"] == "high") == (s["source"] == "rule")
                  for s in _sug["suggestions"]),
              str([(s["field"], s["source"], s["confidence"])
                   for s in _sug["suggestions"]]))
        check("suggest 不返回 item 的可写句柄（只给建议）",
              "item" not in _sug)
        print(f"  （冒烟用的是 {_key}：建议 {len(_sug['suggestions'])} 条，"
              f"跳过 {len(_sug['skipped'])} 项 —— 具体值不在此打印，避免把"
              f"文献内容写进测试输出）")
    else:
        print("  （跳过 suggest 冒烟：缺字段的条目都没有 PDF）")

M.close()

print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
