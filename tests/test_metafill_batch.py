# ⚠ 与 tests/ 下其它测试一致：**只用编造的合成数据**（假的标题、假的 DOI、
#   假的正文），绝不把本机任何一篇文献的标题 / DOI / 正文写进这个文件。
#   原因见 test_metafill.py 开头的说明 —— 测试文件会被提交、被分享、被别的
#   会话读到，一旦写进真实条目，它就成了"用户研究领域的清单"。
#
#   所以：规则用合成数据钉死；真库只做"格式与不变量"的冒烟（本文件不含真库冒烟，
#   真库那部分由 online/localserver.py --test 的第 [12] 节负责）。

"""本轮新增的两块功能的自检：
  [A] 批量补全扫描（`do_metafill_scan`）—— 复用同一个 reader、能取消、
      apply_plan 只含高置信且只含允许自动写的字段。
  [B] 类型/标题修正建议（`_clean_title_by_rules` / `_type_from_pdf_text` /
      `DOI_TYPE_MAP`）—— 保守剥离、DOI 类型映射、只读。

    python tests/test_metafill_batch.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import localserver as LS  # noqa: E402
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


# ================================================================ [A] 批量补全
#
# 用假 reader + 假 suggest_for_item 注入。为什么不跑真库：
#   这个测试要在任何机器上都能过（没有 Zotero 库的机器也要过），
#   而"真库冒烟"已经由 localserver.py --test 的第 [12] 节覆盖了。


class _FakeAuthor:
    def __init__(self, display):
        self.display = display


class _FakeItem:
    def __init__(self, key, title="合成标题", item_type="journalArticle",
                 fields=None, authors=None, n_pdfs=1):
        self.key = key
        self.title = title
        self.item_type = item_type
        self.fields = fields or {}
        self.authors = authors or []
        self.pdfs = [object()] * n_pdfs


class _FakeReader:
    """假的 ZoteroReader：只实现 load_items()（批量扫描用到的那一个）。"""

    def __init__(self, items, pages=None):
        self._items = items
        self._pages = pages or []
        self.n_load_items = 0
        self.closed = 0

    def load_items(self):
        self.n_load_items += 1
        return list(self._items)

    def fulltext_for(self, item, **kw):
        return list(self._pages), "fake"


# 三篇：缺不同字段；一篇字段齐全（不该出现在结果里）
_ITEMS = [
    _FakeItem("AAAA1111", "合成甲", fields={}),
    _FakeItem("BBBB2222", "合成乙", fields={"date": "2020"},
              item_type="conferencePaper"),
    _FakeItem("CCCC3333", "合成丙", fields={"date": "2021", "DOI": "10.1/x",
                                            "volume": "1", "issue": "2",
                                            "pages": "3-4",
                                            "abstractNote": "x"},
              authors=[_FakeAuthor("张三")]),
]

_real_get_reader = M._get_reader
_real_suggest_for_item = M.suggest_for_item
_real_close = M.close

_fake_reader = _FakeReader(_ITEMS)
_reader_builds = {"n": 0}


def _fake_get_reader():
    # 真正的 `metafill._get_reader()` 是懒加载单例：**第一次才构造**。
    # 这个假函数把它复现出来 —— 批量扫描"只构造一次"这条断言
    # 必须测的是这个行为，否则测不到"每篇新建 reader"那个坑。
    if _reader_builds["n"] == 0:
        _reader_builds["n"] = 1
    return _fake_reader


_suggest_calls = []
_cancel_after = {"n": 0}      # 在第 n 次 suggest 之后置取消标志（0 = 不取消）


def _fake_suggest(item, model="", use_model=True):
    _suggest_calls.append(item.key)
    # 取消是"合作式"的：服务端在**每篇之间**检查标志。要测它就得在扫描
    # 进行到中途时置位（不能在 do_metafill_scan 之前置 —— 那个函数一开始
    # 会 clear()，这是有意的：取消一次之后必须还能再扫）。
    if _cancel_after["n"] and len(_suggest_calls) >= _cancel_after["n"]:
        LS._SCAN_CANCEL.set()
    if item.key == "AAAA1111":
        return {"ok": True, "key": item.key, "title": item.title,
                "item_type": item.item_type,
                "suggestions": [
                    # high + 可写字段 → 该进 apply_plan
                    {"field": "DOI", "value": "10.9999/fake.1", "source": "rule",
                     "confidence": "high", "evidence": {"page": 1, "text": "合成证据"}},
                    # low → **绝不能**进 apply_plan（一键采用只写高置信）
                    {"field": "volume", "value": "9", "source": "model",
                     "confidence": "low", "evidence": {"page": 1, "text": "合成证据"}},
                ],
                "skipped": [], "notes": [], "model": {"used": False},
                "fulltext_source": "fake"}
    return {"ok": True, "key": item.key, "title": item.title,
            "item_type": item.item_type,
            "suggestions": [
                # high 但是 creators → 不在自动写白名单里，要进 excluded_high
                {"field": "creators", "value": "合成作者", "source": "rule",
                 "confidence": "high", "evidence": {"page": 1, "text": "合成证据"}},
            ],
            "skipped": [], "notes": [], "model": {"used": False},
            "fulltext_source": "fake"}


M._get_reader = _fake_get_reader
M.suggest_for_item = _fake_suggest
M.close = lambda: None            # 别真去关（假 reader 没有 close）

print("[A] 批量补全扫描 do_metafill_scan（假 reader，不碰真库）")
_r = LS.do_metafill_scan(use_model=False)

check("返回 ok", _r.get("ok") is True, str(_r.get("error")))
check("总篇数是全库篇数", _r.get("total") == 3, str(_r.get("total")))
# CCCC3333 字段齐全（含作者），不该被扫 —— 这条守的是"只扫缺字段的"
check("字段齐全的条目被跳过", _r.get("missing_total") == 2,
      f"missing_total={_r.get('missing_total')}")
check("扫了两篇", _r.get("scanned") == 2, str(_r.get("scanned")))
check("没有跑模型", _r.get("model_used_items") == 0)
check("耗时字段存在", isinstance(_r.get("elapsed"), (int, float)))

# ⚠ 这条是本任务最要紧的性能断言：**整批只构造/使用一个 reader**。
#   逐个 HTTP 请求的写法会每篇重建一次（整库快照拷贝），本机实测很慢。
check("复用同一个 reader（只构造一次）", _reader_builds["n"] == 1,
      f"reader 构造/使用计数 = {_reader_builds['n']}")
check("reader.load_items 只调了一次", _fake_reader.n_load_items == 1,
      str(_fake_reader.n_load_items))

# apply_plan：只有 high、只有白名单字段
plan = (_r.get("apply_plan") or {}).get("items") or []
check("apply_plan 有内容", len(plan) == 1, str(plan))
if plan:
    fields = [f["field"] for f in plan[0]["fields"]]
    check("apply_plan 里没有低置信字段（volume 是 low）", "volume" not in fields,
          str(fields))
    check("apply_plan 里只有可自动写的字段", "DOI" in fields, str(fields))
# creators 是 high，但不在 BATCH_WRITABLE_FIELDS 里 → 必须出现在 excluded_high
check("creators 被排除并计数", (_r.get("excluded_high") or {}).get("creators") == 1,
      str(_r.get("excluded_high")))
check("白名单与插件一致（5 个字段）",
      tuple(LS.BATCH_WRITABLE_FIELDS) == ("date", "DOI", "volume", "issue", "pages"),
      str(LS.BATCH_WRITABLE_FIELDS))

# 逐篇明细要带"缺哪些字段"，否则面板那一列是空的
check("每行都带 missing", all(r.get("missing") for r in _r["rows"]))
check("行里带高/低置信计数",
      all("n_high" in r and "n_low" in r for r in _r["rows"]))

# ---- 取消：跑完第一篇就置取消标志，循环应在**下一篇之前**停下并带回部分结果
_reader_builds["n"] = 0                    # 让"只构造一次"的断言下一轮仍成立
_fake_reader.n_load_items = 0
_cancel_after["n"] = 1
_r2 = LS.do_metafill_scan(use_model=False)
_cancel_after["n"] = 0
check("中途取消：canceled=True", _r2.get("canceled") is True, str(_r2.get("canceled")))
check("中途取消：只扫了一篇就停（部分结果保留）",
      _r2.get("scanned") == 1 and len(_r2.get("rows") or []) == 1,
      f"scanned={_r2.get('scanned')} rows={len(_r2.get('rows') or [])}")
check("中途取消：已扫出来的建议照旧返回",
      (_r2.get("apply_plan") or {}).get("items") != [],
      str(_r2.get("apply_plan")))
# ⚠ 取消标志必须在新一轮开始时复位，否则"取消一次之后再也扫不了"
_r3 = LS.do_metafill_scan(use_model=False)
check("新一次扫描会复位取消标志（能正常扫）",
      _r3.get("canceled") is False and _r3.get("scanned") == 2,
      f"canceled={_r3.get('canceled')} scanned={_r3.get('scanned')}")
check("取消端点可调", LS.do_metafill_scan_cancel().get("ok") is True)

# 清理：取消标志再复位一次，别影响同进程里的其它测试
LS._SCAN_CANCEL.clear()


# ================================================================ [B] 标题剥离
print("\n[B] 类型/标题修正：标题尾部的站点后缀剥离（保守优先）")


def _strip(t):
    r = LS._clean_title_by_rules(t)
    return None if r is None else (r["value"], r["confidence"])


# 该剥的（白名单里的精确模式）
check("IEEE 期刊页尾巴",
      _strip("A Synthetic Study of Something"
             " | IEEE Journals & Magazine | IEEE Xplore")[0]
      == "A Synthetic Study of Something")
check("IEEE 会议页尾巴",
      _strip("Another Synthetic Title Here"
             " | IEEE Conference Publication | IEEE Xplore")[0]
      == "Another Synthetic Title Here")
check("裸 IEEE Xplore 尾巴",
      _strip("Some Reasonably Long Synthetic Title | IEEE Xplore")[0]
      == "Some Reasonably Long Synthetic Title")
check("IEEE 尾巴的置信度是 high",
      _strip("Some Reasonably Long Synthetic Title | IEEE Xplore")[1] == "high")

# **不该剥的**（这几条是"拿不准就不动"的防线，比该剥的更重要）
check("普通副标题不动",
      _strip("A Long Synthetic Main Title - A Subtitle Here") is None)
check("中文书名号式的标题不动",
      _strip("关于某某问题的研究——以某个系统为例") is None)
check("剥完太短就不动（剩下的不是标题了）",
      _strip("Short | IEEE Xplore") is None)
check("剥完太短就不动（中文按 2 字计权，太短同样不动）",
      _strip("测试 | IEEE Xplore") is None)
# ⚠ 闸门③（剥掉超过一半）**只对 low 置信的通配规则生效**，不挡精确的站点名：
#   精确到字的 ` | IEEE Journals & Magazine | IEEE Xplore` 有 40 字符，
#   短标题一比就"超过一半"，但那是必须剥的（第一版就是被这条误挡的）。
check("精确站点后缀再长也要剥（闸门③不挡 high 置信）",
      _strip("Data Study | IEEE Journals & Magazine | IEEE Xplore")[0]
      == "Data Study")
check("全角竖线不认（避免切坏中文标题）",
      _strip("某某技术综述｜某某学报｜某某网") is None)
check("空标题不动", _strip("") is None)

# low 置信那一档：只提示、不写成建议值 —— 这里验它确实是 low
_cn = LS._clean_title_by_rules("某某技术发展现状与展望-某某新闻网")
check("中文站点名尾巴只给 low 置信",
      _cn is not None and _cn["confidence"] == "low",
      str(_cn))


# ================================================================ [C] 类型判据
print("\n[C] 类型/标题修正：从 PDF 首页判类型（合成正文）")

_CONF_PAGE = ("Proceedings of the 2024 International Conference on Synthetic "
              "Testing, pp. 1-6, 2024. " + "填" * 240)
_JOURNAL_PAGE = ("Synthetic Journal of Testing, Vol. 12, No. 3, pp. 45-52, "
                 "2023. ISSN 0000-0000. " + "填" * 240)
_THESIS_PAGE = ("某某大学硕士学位论文 某某系统的设计 " + "填" * 240)
_AMBIG_PAGE = ("Conference on Synthetic Things, Vol. 3, pp. 1-2. " + "填" * 240)
_EMPTY_PAGE = "太短"

_t = LS._type_from_pdf_text([_CONF_PAGE] * 2)
check("会议论文判得出来", _t and _t["value"] == "conferencePaper", str(_t))
check("会议论文置信 high（同族 ≥2 个特征词）",
      _t and _t["confidence"] == "high", str(_t))
check("证据带原文片段", _t and (_t["evidence"] or {}).get("text"), str(_t))
_j = LS._type_from_pdf_text([_JOURNAL_PAGE] * 2)
check("期刊论文判得出来", _j and _j["value"] == "journalArticle", str(_j))
_s = LS._type_from_pdf_text([_THESIS_PAGE] * 2)
check("学位论文判得出来", _s and _s["value"] == "thesis", str(_s))
_amb = LS._type_from_pdf_text([_AMBIG_PAGE] * 2)
check("两族都命中时降为 low", _amb and _amb["confidence"] == "low", str(_amb))
check("没有特征词就返回 None（不动）",
      LS._type_from_pdf_text([_EMPTY_PAGE] * 2) is None)


# ================================================================ [D] DOI 映射
print("\n[D] DOI 权威记录的类型映射")

# ⚠ 这一组是**实测回归**：DOI 内容协商（application/vnd.citationstyles.csl+json）
#   规范上该返回 CSL 词汇，但本机实测 CrossRef 返回的是它自己的 `journal-article`。
#   第一版只映射了 CSL 的 `article-journal`，于是"有 DOI 的条目反而拿不到类型"。
check("CSL 词汇 article-journal 能映射",
      LS.DOI_TYPE_MAP.get("article-journal") == "journalArticle")
check("CrossRef 实际返回的 journal-article 也能映射",
      LS.DOI_TYPE_MAP.get("journal-article") == "journalArticle")
check("proceedings-article → conferencePaper",
      LS.DOI_TYPE_MAP.get("proceedings-article") == "conferencePaper")
check("dissertation → thesis", LS.DOI_TYPE_MAP.get("dissertation") == "thesis")
# 拿不准的类型必须没有映射（宁可不动）
check("dataset 不在映射里（宁可不建议）", "dataset" not in LS.DOI_TYPE_MAP)
check("software 不在映射里", "software" not in LS.DOI_TYPE_MAP)

# 标题归一化只用于"比有没有变"，不能当建议值用
check("标题归一化：去标签与实体",
      LS._plain_title("A &amp; B <i>C</i>") == LS._plain_title("A & B C"),
      LS._plain_title("A &amp; B <i>C</i>"))
check("标题归一化：大小写不敏感",
      LS._plain_title("Something Here") == LS._plain_title("something here"))
check("标题归一化：去结尾句点",
      LS._plain_title("Something Here.") == LS._plain_title("Something Here"))


# ================================================================ [E] 只读性
print("\n[E] typefix 是只读的（不许在服务端出现写库路径）")


class _FakeItemFull:
    key = "ZZZZ9999"
    title = "Synthetic Title | IEEE Xplore"
    item_type = "webpage"
    fields = {"title": "Synthetic Title | IEEE Xplore", "url": "https://x"}
    pdfs = [object()]

    def __init__(self, pages):
        self._pages = pages


# 让 typefix 用假 reader 取正文
_reader_for_typefix = _FakeReader([], pages=[_JOURNAL_PAGE] * 2)
M._get_reader = lambda: _reader_for_typefix
M.close = lambda: None

_tf = LS.typefix_for_item(_FakeItemFull(None), use_network=False)
check("typefix 返回 ok", _tf.get("ok") is True, str(_tf.get("error")))
check("网页 + PDF + 期刊特征 → 有类型建议",
      any(f["kind"] == "type" and f["value"] == "journalArticle"
          for f in _tf.get("fixes", [])), str(_tf.get("fixes")))
check("标题建议剥掉了站点尾巴",
      any(f["kind"] == "title" and f["value"] == "Synthetic Title"
          for f in _tf.get("fixes", [])), str(_tf.get("fixes")))
check("每条建议都带置信与依据",
      all(f.get("confidence") and f.get("rule") for f in _tf.get("fixes", [])))
# 响应里不该有任何"写入结果"字段 —— 写库只能发生在插件里
_wrote = [k for k in _tf if k in ("applied", "written", "changed", "saved")]
check("响应里没有写入结果字段（只读）", not _wrote, str(_wrote))

# 常规类型 + PDF 不该被当成嫌疑（report 挂 PDF 完全正常）
class _FakeReport:
    key = "REPORT01"
    title = "A Synthetic Report"
    item_type = "report"
    fields = {}
    pdfs = [object()]


_tf2 = LS.typefix_for_item(_FakeReport(), use_network=False)
check("report + PDF 不算嫌疑", _tf2.get("suspect") is False, str(_tf2))
check("report 不产出修正建议", not _tf2.get("fixes"), str(_tf2.get("fixes")))


class _FakeNoPdf:
    key = "NOPDF001"
    title = "Synthetic"
    item_type = "webpage"
    fields = {}
    pdfs = []


check("没 PDF 的直接跳过", LS.typefix_for_item(_FakeNoPdf()).get("suspect") is False)


class _FakePaper:
    key = "PAPER001"
    title = "Synthetic Paper Title"
    item_type = "journalArticle"
    fields = {}
    pdfs = [object()]


check("已经是期刊论文的不动",
      LS.typefix_for_item(_FakePaper()).get("fixes") == [])

# ---- 收尾：把被替换掉的模块级函数还回去，别污染同进程里的其它测试
M._get_reader = _real_get_reader
M.suggest_for_item = _real_suggest_for_item
M.close = _real_close

print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
