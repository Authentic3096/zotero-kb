"""从 PDF 正文里补全缺失的元数据字段（只给建议，不写回任何东西）。

    python offline/metafill.py <KEY>          # 看某篇文献的补全建议
    python offline/metafill.py --scan         # 扫描全库，列出缺字段的文献
    python offline/metafill.py <KEY> --no-model   # 只跑规则，不调本地模型

## 为什么要有这个模块

用 Zotero 抓文献时识别失败很常见：日期、卷、期、页码、DOI 留空。这些值其实
**就印在 PDF 首页**，只是 Zotero 没抓到。人工翻 PDF 一页页抄太慢，所以让程序
读首页自己找。

## 三条设计红线（用户定的，改代码时不要绕过）

1. **只填空，不覆盖** —— 只对"当前为空"的字段给建议。已有值的一律不出现在
   `suggestions` 里，而是进 `skipped` 并写明原因。为什么：程序猜错时覆盖掉
   用户手工填对的值，比不填更糟 —— 而且用户不会立刻发现。
2. **每条建议必须带原文证据** —— 页码 + 原文片段。用户要能一眼核对
   "这个值是从哪来的"。没有证据的价值等于让用户盲信，所以模型给的值也必须
   给出它依据的原文（并校验这段原文真的在正文里，见 `_verify_evidence`）。
3. **只给建议，不写回** —— 本模块不碰 Zotero 数据库，也不碰知识库的 index.db。
   写回是后续模块的事；这样这个模块可以随便跑、随便试，不会有副作用。

## 抽取策略：规则优先，模型兜底

字段的"可规则化程度"差别很大，所以不是一刀切：

| 字段     | 主要手段 | 原因 |
|----------|----------|------|
| DOI      | 纯正则   | 格式是全球统一的（`10.` + 注册机构号 + `/` + 后缀），正则几乎不会错，调模型纯属浪费 |
| date     | 正则为主 | 常见形态有限（`2025 年 12 月`、`2025-03-29`、`Dec. 2025`…），但**噪声最多**（收稿日期、网络首发日期、参考文献里的年份），见下面的"日期为什么难" |
| volume   | 正则为主 | 期刊首页有固定套路（`第 37 卷`、`Vol. 37`、`37(4)`） |
| issue    | 正则为主 | 同上（`第 4 期`、`No. 4`、`37(4)`） |
| pages    | 正则为主 | `pp. 765-770`、`37(4): 765-770`、`文章编号:…(2024)03-0296-05` 都能推出来 |
| creators | 正则为主 | 作者行在版式上位置固定（标题下方、封面标签后），但**姓名形态自由度最大** —— 规则拿的是"整行是不是一串姓名"，姓名本身的拆分另有一套保守判据，见"作者为什么单独处理" |

规则抽到的值标 `confidence="high"`，模型抽到的标 `"low"`。为什么不给模型也标
high：小模型做抽取会**编**（本机实测：它会把参考文献里的 DOI 当成这篇的 DOI），
用户必须能一眼看出哪个值可以信、哪个需要自己核一眼。规则是从固定格式里抠出来的，
出处明确，所以给 high。

### 日期为什么难；以及为什么"投稿日期也采纳"（策略在本轮改过）

中文期刊首页上同时印着好几个日期：`收稿日期:2025-06-03`、`改回日期:2025-07-15`、
`网络首发日期:2025-08-21` 以及真正的出版日期。只按"第一个能匹配的日期"取，
会把**收稿日期**当成出版日期填进去 —— 这个错很隐蔽（值本身是个合法日期，
看起来没问题）。

**采纳策略（用户拍板，本轮改）**：`date` 的最终用途是给文献算**按发表年份的
基础权重**（`schemas.recency_base`，0.95~1.05 那个区间）。年份差一两年，权重
只差不到 1%；而**留空会让整篇文献丢掉初始权重**（库里绝大多数文献的权重因此
一直硬编码成 1.0，见 `schemas.raw_weight` 的说明）—— 后者的代价大得多。

所以：**投稿日期 / 收稿日期 / 网络首发日期 / 在线发表日期一律采纳**，
不再"只有投稿日期就宁可留空"。上一版的做法是把带这些标签的候选**直接丢掉**，
那是按"date 必须是正式出版日期"的假设写的；现在假设变了，判据也要跟着变。

但"采纳"不等于"不加区分" —— 建议里必须能看出这是哪种日期，用户在弹窗里
才判断得出要不要用：

| `date_kind`      | 含义 | 典型标签 |
|------------------|------|----------|
| `published`      | 正式出版日期 | `2025 年 12 月`、`Vol. 37, No. 4, Dec. 2025` |
| `online_first`   | 网络首发 / 在线发表 / 优先出版 | `网络首发日期`、`Available online`、`Published online` |
| `submitted`      | 收稿 / 投稿 / 修回 / 录用 | `收稿日期`、`Received`、`Revised`、`accepted` |
| `unknown`        | 看不出标签（模型给的值常是这种） | —— |

`date_kind_label` 是配套的中文说明，界面直接显示它就行，不必自己翻译枚举。

候选排序（**先看类型，再看形态**）：

1. 把候选**全都收集起来**，每条都记下它前面 20 个字符的上下文；
2. **按类型排优先级**：正式出版 > 网络首发/在线发表 > 收稿/投稿/修回。
   这一步原来是"带投稿标签的直接丢掉"，现在改成**降级而不是丢弃** ——
   有正式出版日期时仍旧选它，只有投稿日期时也能给用户一个可用的值；
3. 同类型内按"出版日期的典型程度"打分：`2025 年 12 月` / `Dec. 2025` 这种
   **年月**最像出版信息，`2025-03-29` 这种精确到日的反而更可能是投稿/上线日期；
4. 都相同再按位置：靠前的像刊头，靠后的像参考文献。

这样"出版年月 + 收稿日期"同时存在时，选中的仍是出版年月。

### 作者（creators）为什么单独处理

作者是这个模块里**唯一的多值字段**，也是唯一"错一个字符就毁掉整条引文"的字段。
所以它的规则和其余 5 个字段不同，三条硬约束：

1. **只填空，而且是"整条跳过"式的填空** —— 条目**已有任何一位作者**就整条不给建议。
   为什么不支持"补第 3 个作者"：现有作者与新抽出的作者怎么合并、顺序怎么排，
   没有可靠依据（原文可能只列了前几位，也可能条目里的是别的版本）。
   合并错了比不补更糟，而且用户不容易发现。
2. **姓名拆分一律保守**：拆不准就**整名放 `lastName`**、`firstName` 留空。
   Zotero 里 `lastName` 放整名照样正常显示、引用样式也能正确处理
   （中文名本来就是这么存的，fieldMode=1）；而**拆错**的代价大得多：
   `Zhang San` 拆成 first=Zhang / last=San 之后，引文会变成 `San, Z.`，
   用户不逐条核对根本发现不了。
3. **必须标出"作者没列全"**：原文出现 `et al.` / `等` 时，只抄**列出来的**那几位，
   并在建议里带 `partial=true` 与一句人话 `warning`，让用户自己决定要不要采纳。

返回值形态（**和其余 5 个字段的返回结构不兼容，是刻意设计的**）：

```python
{"field": "creators", "value": "San Zhang; Ming Li",   # 字符串，给人看
 "values": [{"lastName": "Zhang", "firstName": "San", "creatorType": "author"}, …],
 "n_creators": 2, "partial": False, "warning": "", …}
```

为什么 `value` 仍然是**字符串**、多值走新键 `values`：

  · 插件里已有的 `metaLine` 是直接把 `sug.value` 拼进字符串的
    （`sug.field + " → " + sug.value`）。若让 `value` 有时是数组，JS 会把它
    **隐式转成 `张三,李四`** —— 看起来完全正常，接线方不会发现自己的代码没适配。
    这个项目最怕的就是"值看着合法、语义已经错了"。
  · 写回作者用的是 `item.setCreators([...])`，它要的是**有序的对象列表**；
    给字符串的话接线方还得再解析一次，多一次出错机会。
  · 所以两者都给：`value` 保人读与旧结构兼容，`values` 专供写回。
  · `values` 里用 **Zotero 自己的字段名**（`firstName` / `lastName` /
    `creatorType`）—— 写回时不必翻译，也就没有"翻译错把姓和名调了个个儿"的机会。

## 数据从哪来（确认过的接口，不要凭印象改）

- 正文：`zreader.ZoteroReader.fulltext_for(item)` → `(pages, source)`。
  只看**前两页**：元数据都在首页/版权页，往后翻只会引入参考文献里的噪声
  （参考文献里有大把别人的卷期页码和 DOI）。
- 当前值：`schemas.Item.fields`（dict，键是 Zotero 的字段名 `date` / `DOI` /
  `volume` / `issue` / `pages`）。**不能**从 index.db 读 —— 那张 items 表只有
  `year` 和 `doi` 两列，没有 volume / issue / pages / date；反过来，能从
  `item.fields` 读就不能只信索引库，否则会出现"索引库里 doi 是空的、但 Zotero
  里其实有"这种假缺失。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402
import zreader  # noqa: E402

# judge 只是"可选后端"。放在 try 里是因为：这个模块的规则部分不依赖模型，
# 而 judge 会 import urllib/sqlite 等一堆东西。万一将来 judge 的依赖出问题
# （或缺文件），整个 metafill 也不该跟着挂 —— 退化成纯规则即可。
try:
    import judge  # noqa: E402
except Exception:  # noqa: BLE001
    judge = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 常量

# 可补的字段范围。
#
# ⚠ `creators` 是**唯一的多值字段**，返回值与其余 5 个不同（多了 `values`，
#   见模块头"作者为什么单独处理"）。把它放进这个元组是为了让"字段合法性"
#   在 CLI 的 --field 校验、scan 的缺失统计、以及测试里的
#   `set(fields) <= set(FIELDS)` 这些地方**只有一处口径**。
#   它的当前值不从 `item.fields` 读，而是从 `item.authors` 读（见 _current_values）。
#
# 仍然刻意不做 title / abstract / 期刊名 —— 那几个字段靠"格式"抽不准，
# 得靠外部数据源（Crossref/知网）比对，不是这个模块的活。
FIELDS = ("date", "DOI", "volume", "issue", "pages", "creators")

# 只看前两页：首页有刊头，版权页/英文摘要页有补充信息，再往后全是正文和
# 参考文献，噪声远大于收益。
HEAD_PAGES = 2

# 每条证据片段最多截多长。给用户核对的，太长反而看不清；160 字足够覆盖
# 一整行刊头。
EVIDENCE_MAX = 160

# 证据片段里命中值**前面**留多少字的上下文。中文刊头很短，40 字足够看到
# "第 37 卷" 这类前置标签；留太多会挤掉值后面的内容。
EVIDENCE_CTX = 40

# 正文里如果连这点字符都没有，说明这不是能读的文本（扫描件、或者 PDF 解析
# 失败），直接放弃，别浪费模型调用。
MIN_TEXT_CHARS = 200


# ---------------------------------------------------------------- 正文与证据


def _is_cjk(ch: str) -> bool:
    return "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef"


def head_text(pages: list[str], limit: int = HEAD_PAGES) -> str:
    """把前几页拼成一段文本，页与页之间用换行隔开。

    为什么要拼而不是逐页处理：刊头信息经常**跨页断开**（中文期刊的卷期在首页、
    收稿日期在版权页），逐页各自匹配会漏。页码归属交给 `_locate` 处理。
    """
    return "\n".join((p or "") for p in (pages or [])[:limit])


def _locate(pages: list[str], start: int, limit: int = HEAD_PAGES) -> int:
    """把"拼接文本里的偏移"换算成"第几页"（1 起，给人看的页码）。"""
    pos = 0
    for i, page in enumerate((pages or [])[:limit]):
        end = pos + len(page or "") + 1        # +1 是 head_text 里补的那个换行
        if start < end:
            return i + 1
        pos = end
    return 1


def build_evidence(pages: list[str], start: int, end: int,
                   limit: int = HEAD_PAGES) -> dict:
    """从正文里裁一段证据出来（页码 + 原文片段）。

    为什么证据要带上下文、而不是只给命中的那几个字符：用户核对时要判断的是
    "这个值是这篇文献的卷，还是参考文献里别人的卷"。只给 `37` 看不出区别，
    给 `第 37 卷 第 4 期 2025 年 12 月` 才能一眼确认。
    """
    text = head_text(pages, limit)
    # 窗口分配：命中值前面给固定的一小段上下文，后面尽量多给。
    #
    # 为什么不是"命中值居中"（这是改了两轮的地方）：把值放在片段正中，
    # 等于只给它一半额度；额度不够时右（或左）边界就会**把值本身切掉**，
    # 用户拿到的"证据"里根本没有那个值 —— 自检里真的抓到了这个。
    # 现在的序：先保证"值完整在窗口里"，再谈上下文，最后用上限兜住。
    s = max(0, start - EVIDENCE_CTX)
    e = min(len(text), start + EVIDENCE_MAX)
    if e < end:                       # 值被右边界切到了，整体往左挪
        e = min(len(text), end)
        s = max(0, e - EVIDENCE_MAX)
    if e - s > EVIDENCE_MAX:          # 左边界兜底（start 很靠前时 s 不会为负）
        e = s + EVIDENCE_MAX
    snippet = text[s:e].strip()
    # 换行会让"原文片段"看起来断开，接成一行更接近阅读观感。
    # 只把换行折成空格，不动原有的空格 —— 中文文本里 `第 37 卷` 这种
    # 空格是 PDF 提取出来的排版痕迹，改掉反而和原文对不上。
    snippet = re.sub(r"\s*\n\s*", " ", snippet).strip()
    # 两头不要切在词中间：`otor Learning Systems` 这种片段用户一看就皱眉，
    # 而且会怀疑"是不是程序看错了行"。只裁两端，不重排中间的字（保证
    # 打印出来的片段在原文里逐字可查）。
    snippet = _align_edges(snippet, text[start:end])
    if not snippet:
        snippet = text[start:end].strip()
    return {"page": _locate(pages, start, limit), "text": snippet,
            # `_span` 是"这个值在原文里命中的那段字面文本"，供自检核对用：
            # 值可能是**归一化**过的（`Dec. 2025` → `2025-12`），所以不能拿
            # 值本身去片段里搜 —— 搜不到不代表证据是错的。真正该保证的是
            # "命中的那段原文完整出现在片段里"，这一条由 tests 守着。
            # 下划线开头表示内部字段，CLI/上层不必展示。
            "_span": text[start:end]}


def _is_word_char(ch: str) -> bool:
    """是不是"词内字符"（用于判断片段两端有没有切在半个单词上）。

    ⚠ 只认拉丁字母数字下划线，**不认 CJK**。踩过的坑：一开始写成
    `ch.isalnum()`，而 `年`/`月` 这些汉字 `isalnum()` 也是 True，于是
    "命中值后面跟着 `年`" 被当成"切在词中间"，整个右裁剪逻辑就不触发了
    （实测片段照旧从半个单词开始）。
    """
    return ch.isascii() and (ch.isalnum() or ch in "._-")


def _align_edges(snippet: str, span: str) -> str:
    """把片段两端的半个单词裁掉。

    为什么值得写这段：证据是给**人**核对的，片段两头是半个单词时（本机实测
    出现过 `otor Learning Systems 张三 …`），用户第一反应是"这程序读串行了"，
    对整条建议的信任就掉了。成本只是几行裁剪，收益是证据看起来是"原文"。

    ⚠ 只在两端裁剪，**不动中间的字符**：片段必须在原文里逐字可查，
    否则用户拿它去 PDF 里搜会搜不到，反而更可疑。
    """
    if not snippet:
        return snippet
    # 命中值在片段里的位置（strip 过可能有偏移，所以用 find 定位而不是算）
    anchor = snippet.find(span) if span else -1
    if anchor < 0:
        anchor = 0
    # 左边：片段正好从半个单词中间开始时，往左走到词头（不会越过锚点）
    if anchor > 0 and _is_word_char(snippet[anchor - 1]) \
            and _is_word_char(snippet[0]):
        i = 0
        while i < anchor and _is_word_char(snippet[i]):
            i += 1
        snippet = snippet[i:].lstrip()
        anchor = snippet.find(span) if span else 0
        if anchor < 0:
            anchor = 0
    # 右边：同理走到词尾。判据是"空格前的最后一个字符"和"空格后的第一个
    # 字符"都是词内字符 —— 即这个空格是词间空格，不是排版空格。
    tail = anchor + max(1, len(span))
    if 0 < tail < len(snippet) - 1 and not snippet[tail].isspace() \
            and _is_word_char(snippet[tail - 1]) and _is_word_char(snippet[tail]):
        i = 0
        while i < tail and not snippet[i].isspace():
            i += 1
        snippet = (snippet[:i].rstrip() + " " + snippet[tail:]).rstrip()
    return snippet


def _ctx(text: str, start: int, width: int = 20) -> str:
    """取命中位置之前的上下文（小写化），用于"这个词是不是投稿日期标签"的判断。"""
    return text[max(0, start - width):start].lower()


# ---------------------------------------------------------------- 规则抽取

# DOI 的标准形态。为什么不用更严格的校验（比如要求后缀含数字）：实测有一批
# 中文期刊 DOI 的**后缀是纯字母数字混排、不含任何点号**（形如
# `10.1234/abcd-ef230237`），规则再严就会漏。反倒是"排除误匹配"更值得做，
# 见 _extract_doi 里的清理。
RE_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")

# 带空格的 DOI。为什么要单列一条：PDF 提取会把 DOI 拆出空格，本机实测见过
# `10. 1234 / j. abcd. 1004. 9533` 这种（每个点号后面都多了空格）。
# 规则是"先按上面的严格形态找，找不到才退到这条"，避免这条宽松规则把
# 正文里正常的数字串吃进来。
RE_DOI_SPACED = re.compile(r"10\s*\.\s*\d{4,9}\s*/\s*[A-Za-z0-9][-._;()/:\sA-Za-z0-9]{2,}")

# 卷期页码的几种固定套路。分组顺序在不同套路里含义不同，所以分开写、
# 每条单独处理 —— 硬凑成一个大正则，将来加一种格式就会牵连其它几条。
RE_VOL_EN = re.compile(r"[Vv]ol\.?\s*(\d{1,4})(?!\d)", re.I)
RE_NO_EN = re.compile(r"\b(?:No|Issue)\.?\s*(\d{1,3})(?!\d)", re.I)
RE_CN_VOL = re.compile(r"第\s*(\d{1,4})\s*卷")
# 中文"第 N 期"：不能写成 `第\s*(\d+)\s*期` 就完 —— `第 14 卷` 里也有"第"，
# 宽松写法会在 "第 14 卷 第 1 期" 里抓错。这里强制要求"第"前面不是
# "卷"（回顾一位）且数字后面紧跟"期"。
RE_CN_NO = re.compile(r"(?<!卷)\s?第\s*(\d{1,3})\s*期")
# `37(4): 765-770` / `42( 4) :68-83`：卷(期): 起页-止页
RE_VOL_ISSUE_PAGES = re.compile(
    r"\b(\d{1,4})\s*\(\s*(\d{1,3})\s*\)\s*[:：]\s*(\d{1,5})\s*[-–—~～]\s*(\d{1,5})\b")
# `2025, 37(12): 1846~1865`（中文期刊英文摘要里的引用格式，带年份）
RE_CITE_YEAR = re.compile(
    r"\b(?:19|20)\d{2}\s*,\s*(\d{1,4})\s*\(\s*(\d{1,3})\s*\)\s*[:：]\s*"
    r"(\d{1,5})\s*[-–—~～]\s*(\d{1,5})\b")
RE_PP = re.compile(r"\b(?:pp?|pages?)\s*\.?\s*(\d{1,5})\s*[-–—~～]\s*(\d{1,5})\b",
                   re.I)
RE_PAGE_CN = re.compile(r"第\s*(\d{1,5})\s*[-–—~～]\s*(\d{1,5})\s*页")
# `文章编号:1673-1379(2024)03-0296-05`：最后两段是"起始页-页数"，
# 这是中文期刊最可靠的页码来源之一（没有它就只能靠卷期页码套路）。
RE_ARTICLE_NO = re.compile(
    r"文章编号\s*[:：]?\s*\d{4}\s*[-–—]\s*\d{4}\s*\(\s*(\d{4})\s*\)\s*"
    r"(\d{1,3})\s*[-–—]\s*(\d{4,5})\s*[-–—]\s*(\d{2})")

# 日期候选。同样按形态分开，便于给不同形态不同的可信度。
# 年月日 / 年月：中文写作 `2025 年 12 月 29 日`，西文写作 `2025-12-29`、`2025/03`
RE_YMD = re.compile(
    r"\b((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*(?:[-/.月]\s*(\d{1,2})\s*日?)?")
# 英文月份名在前：`Dec. 2025`、`Jan. 2025`
RE_MON_Y = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+"
    r"((?:19|20)\d{2})\b", re.I)
# 英文日期完整形态：`29 March 2025`
RE_D_MON_Y = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+"
    r"((?:19|20)\d{2})\b", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# ---- 日期"类型"标签
#
# 为什么要给日期贴类型，而不是像上一版那样把非出版日期直接丢掉：
#   `date` 只用来算"按发表年份的基础权重"（schemas.recency_base，0.95~1.05）。
#   年份差一两年权重差不到 1%，而**留空会让整篇文献丢掉初始权重**，代价更大。
#   所以投稿/收稿/网络首发/在线发表日期**都采纳**，但必须让用户看得出这是哪种，
#   他才判断得了要不要用（用户原话："即使投稿时间也没关系，因为时间不会差太多"）。
DATE_KIND_PUBLISHED = "published"
DATE_KIND_ONLINE_FIRST = "online_first"
DATE_KIND_SUBMITTED = "submitted"
DATE_KIND_UNKNOWN = "unknown"

# 排序优先级：数字小的更可能是"真正的出版日期"，同类型内再比形态分。
# 单独列出来是为了让"为什么正式出版排最前"这件事有唯一一处可改。
DATE_KIND_RANK = {
    DATE_KIND_PUBLISHED: 0,
    DATE_KIND_ONLINE_FIRST: 1,
    DATE_KIND_SUBMITTED: 2,
    DATE_KIND_UNKNOWN: 3,
}

# 给界面直接显示的中文说明。放在服务端而不是插件里：
# 插件只需要原样显示 `date_kind_label`，不必自己维护一份枚举→中文的映射
# （两处映射迟早会漂移，而这里改了插件不用跟着发版）。
DATE_KIND_LABELS = {
    DATE_KIND_PUBLISHED: "正式出版日期",
    DATE_KIND_ONLINE_FIRST: "网络首发/在线发表日期（不是正式出版日期）",
    DATE_KIND_SUBMITTED: "收稿/投稿/修回日期（不是正式出版日期）",
    DATE_KIND_UNKNOWN: "日期类型未能判断",
}

# 网络首发 / 在线发表 / 优先出版的标签词。这些**是**可用的出版信息
# （中文期刊的网络首发论文往往只有它、没有正式卷期），所以归到第二档。
ONLINE_MARKERS = (
    "网络首发", "网络出版", "网络优先", "在线发表", "在线出版", "在线首发",
    "优先出版", "优先数字出版", "预出版", "上线",
    "available online", "published online", "first published", "online first",
    "advance online", "early access", "in press", "epub ahead",
)

# 投稿/修回/录用类日期的标签词。归第三档：**能采纳**，但在有别的日期时排在后面。
#
# ⚠ 上一版把 `出版日期` 也塞进了这个"丢弃词"表里 —— 那是错的：`出版日期:2025-03-29`
#   显然是最明确的出版标签，被当成投稿日期丢掉属于自伤。本轮一并改正
#   （不带标签的日期本来就默认算正式出版，见 classify 的兜底分支）。
SUBMIT_MARKERS = (
    "收稿", "投稿", "来稿", "修回", "改回", "录用", "接受", "定稿", "审稿",
    "received", "revised", "accepted", "submitted", "article received",
)

# 判断类型时往前看多长一段上下文。20 字是实测够用的长度：
# 中文日期标签（`收稿日期:`、`网络首发日期:`）都在 5~7 字内，英文标签
# （`Available online `、`Published online `）在 20 字内。
DATE_CTX = 20

# 类型标签 → (优先级更高的先检查)。顺序无关紧要（下面按"离日期最近"判），
# 但为了让可读性一致，依旧按 ONLINE 在前、SUBMIT 在后排列。
_KIND_MARKERS = (
    (DATE_KIND_ONLINE_FIRST, ONLINE_MARKERS),
    (DATE_KIND_SUBMITTED, SUBMIT_MARKERS),
)


def kind_from_text(text: str) -> str:
    """从一小段文字里判断日期类型，看不出标签返回空串（**不是** unknown）。

    为什么要单独一个函数、而不是写进 `classify_date_kind`：
    模型的证据是一小段原文，没有"命中位置"这个概念，只能整段找标签。
    两个入口共用同一套标签词，才不会出现"规则认 `网络首发`、模型不认"的漂移。

    ⚠ 取**离日期最近**的那个标签（`rfind` 取最靠右的一次出现），不是"谁先出现算谁"：
      中文期刊首页常把 `收稿日期: 2025-04-14  网络首发日期: 2025-08-21` 并排印在
      一行，用"先出现算谁"的话第二个日期会被前一个标签污染成收稿日期。
      离得最近的那个才是这个日期的标签。
    """
    low = (text or "").lower()
    best_pos, best_kind = -1, ""
    for kind, markers in _KIND_MARKERS:
        for mark in markers:
            pos = low.rfind(mark)
            if pos > best_pos:
                best_pos, best_kind = pos, kind
    return best_kind


def classify_date_kind(text: str, start: int) -> str:
    """判断命中在 `start` 处的日期是哪种日期（正式出版 / 网络首发 / 收稿）。

    没有已知标签时**默认算正式出版**：期刊首页上绝大多数不带标签的日期
    （刊头 `2025 年 12 月`、`Vol. 37, No. 4, Dec. 2025`）就是出版日期，
    把它们降级成 unknown 反而会让排序失去依据。
    """
    return kind_from_text(_ctx(text, start, DATE_CTX)) or DATE_KIND_PUBLISHED


def date_kind_note(kind: str) -> str:
    """给 `evidence.note` 准备一句人话提示；正式出版日期不需要提示，返回空串。"""
    if kind == DATE_KIND_ONLINE_FIRST:
        return ("这是网络首发/在线发表日期，不是正式出版日期。"
                "按发表年份算权重时两者差不了多少，可以放心用。")
    if kind == DATE_KIND_SUBMITTED:
        return ("这是收稿/投稿/修回日期，不是正式出版日期。"
                "年份一般只差一两年，用来算初始权重没问题；"
                "如果这篇有正式出版日期，优先填那个。")
    if kind == DATE_KIND_UNKNOWN:
        return "没能从上下文判断这是哪种日期，请自行核对 PDF 再决定用不用。"
    return ""

# 一眼假的年份：这些数字形态上像日期，其实是期刊号/期号/IP 之类
RE_BAD_YEAR = re.compile(r"(?:ISSN|CN|ISBN|No\.?\s*|Vol\.?\s*)\s*$", re.I)

# 日期抽取前要先"挖掉"的区域：DOI、URL、CN/ISSN/ISBN 号。
#
# 为什么必须有这一步（本机实测的两个真实假阳性，DOI 用等长占位符改写）：
#   · `CN 11-6037/Z` 里的 `11` 和前面 DOI 里的年份 `2024`，被正则拼成了
#     `2024-11` —— 一个**看起来完全合法的出版年月**，凭空造出来的；
#   · `https://doi.org/10.1234/j.abcd.2025.118265` 里的 `2025` 和
#     `118265` 的片段，被拼成了 `2025-11`。
#   这类错误最麻烦：值本身格式正确，用户不逐字核对证据根本发现不了。
#   所以不是"尽量别匹配"，而是**先把这些区域从候选搜索里排除掉**。
RE_MASK = re.compile(
    r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"          # DOI
    r"|https?://\S+"                              # URL
    r"|\b(?:ISSN|ISBN)\s*[\dXx-]{6,}"             # ISSN / ISBN
    r"|\bCN\s*\d{1,2}\s*[-–]\s*\d{3,4}\s*/\s*[A-Za-z]"   # CN 号
)


def _masked_spans(text: str) -> list[tuple[int, int]]:
    """算出"不该拿来当日期证据"的字符区间（已排序）。"""
    return [(m.start(), m.end()) for m in RE_MASK.finditer(text)]


def _overlaps(spans: list[tuple[int, int]], s: int, e: int) -> bool:
    """区间 [s, e) 是否和任何保护区重叠。空 spans 时直接返回 False。"""
    for a, b in spans:
        if s < b and e > a:
            return True
        if a >= e:            # spans 有序，后面的更靠右，不用再比
            break
    return False



def _clean_doi(raw: str) -> str:
    """把 DOI 文本清理成纯 DOI。

    要处理三件事（都是实测见过的形态）：
      · `doi:10.xxxx/yyy`、`DOI: 10.xxxx/yyy` —— 前缀要去掉；
      · `https://doi.org/10.xxxx/yyy`、`doi.org/10.xxxx/yyy` —— URL 外壳要去掉；
      · PDF 提取在点号后插了空格 —— 要把空格去掉（DOI 本身不含空格）。
    末尾的标点（`.`、`,`、`)`）也常被一起匹配进来，需要剪掉；但**不能无脑剪**：
    DOI 后缀里合法地可以出现 `)` 和 `.`，所以只在"剪掉后仍然像个 DOI"时才剪。
    """
    s = (raw or "").strip()
    # 去掉 URL / 前缀外壳（大小写不敏感，可能有空格）。
    # 顺序要紧：先剥 `https://doi.org/` 这种外壳，再剥裸的 `doi:` 前缀 ——
    # 反过来的话 `doi:10.xxxx` 会被剥成 `10.xxxx`，没问题；但
    # `https://doi.org/10.xxxx` 先撞上 `doi:` 规则就会剩下一截 `org/10.xxxx`。
    # 所以 URL 形态必须排在前面，而且它的正则里同时吃掉 `https://` 和 `doi.org/`。
    s = re.sub(r"^\s*(?:https?://)?(?:dx\.)?doi\.org\s*/\s*", "", s, flags=re.I)
    s = re.sub(r"^\s*doi\s*[:：]\s*", "", s, flags=re.I)
    # 去掉所有空白（DOI 里不可能有空格）；中英文全角空格一并处理
    s = re.sub(r"[\s\u3000]+", "", s)
    # 剪掉结尾的标点，直到它是个合法 DOI 为止
    while s and s[-1] in ".,;:)]}>,、。；：）】":
        s = s[:-1]
    return s


def _valid_doi(doi: str) -> bool:
    """粗校验：只要求"10. + 4~9 位注册号 + / + 至少 3 个后缀字符"。

    为什么不查 DOI 手册做完整校验：这一步的目的是**过滤误匹配**（比如
    `10.1109/EXAMPLE.2020.3031740` 后面粘上的正文），不是做数据质检。
    校验收紧的代价是漏掉真实 DOI，而漏掉的用户看不出来。
    """
    return bool(re.fullmatch(r"10\.\d{4,9}/[A-Za-z0-9][-._;()/:A-Za-z0-9]{2,}", doi))


def _extract_doi(text: str) -> tuple[str, int, int] | None:
    """从正文里抽 DOI，返回 (doi, 起, 止)；抽不到返回 None。"""
    # 先严格后宽松：严格形态在绝大多数文献上都能命中，只有 PDF 把 DOI 拆开了
    # 才需要退到宽松形态。
    for rx in (RE_DOI, RE_DOI_SPACED):
        for m in rx.finditer(text):
            cand = _clean_doi(m.group(0))
            if _valid_doi(cand):
                # 证据要指向**原文的真实跨度**，不是清理后的字符串 ——
                # 用户核对时看到的是 PDF 里的样子。
                return cand, m.start(), m.end()
    return None


def _extract_date(text: str) -> tuple[str, int, int, int, str] | None:
    """抽日期，返回 (归一化日期, 起, 止, 形态分, 日期类型)；抽不到返回 None。

    形态分与日期类型（`published` / `online_first` / `submitted`）的含义在
    `_date_candidates` 与 `DATE_KIND_*` 常量处解释。
    返回的是**排序最靠前**的那个候选，而不是第一个匹配到的。

    ⚠ 返回值是 5 元组，第 4 位仍是形态分（`cands[0][3]`）—— 老调用方不用改；
      第 5 位是新增的日期类型。测试里直接按下标取，不要写 `a, b, c, d = ...`。
    """
    cands = _date_candidates(text)
    return cands[0] if cands else None


def _date_candidates(text: str) -> list[tuple[str, int, int, int, str]]:
    """收集所有日期候选，按"最该被当成出版日期的程度"降序排。

    元组是 (值, 起, 止, 形态分, 日期类型)。

    排序键：
      1. **日期类型**（`DATE_KIND_RANK`）：正式出版 > 网络首发/在线发表 >
         收稿/投稿/修回。这一步本轮从"丢弃非出版日期"改成"给它们降级" ——
         有正式出版日期时照旧选它，**只有**投稿日期时不再留空。
      2. **形态分**（同类型内比）：
         3 分：年月（`2025 年 12 月`、`Dec. 2025`）—— 期刊出版信息最常见的写法
         2 分：年月日（`2025-03-29`、`29 March 2025`）—— 也可能是投稿/上线日期
         （1 分"只有年份"目前没有规则会产生：`RE_YMD` 要求必须带月份，
          单独一个年份太容易撞上参考文献，索性不收集。）
      3. **位置**：靠前的像刊头，靠后的像参考文献。

    为什么年月反而排在年月日前面：本机实测中文期刊首页几乎都印
    `2025 年 12 月` 这种出版年月，而精确到日的往往是"收稿日期/网络首发"。
    两个都在时，选年月更接近真相。
    """
    out: list[tuple[str, int, int, int, str]] = []
    # 先算出要排除的区域（DOI/URL/CN/ISSN）—— 见 RE_MASK 的说明，
    # 不排除的话会凭空拼出 `2024-11` 这种假出版日期。
    masked = _masked_spans(text)

    def push(value: str, s: int, e: int, score: int) -> None:
        if not value:
            return
        # 落在 DOI/URL/CN 号里的"日期"不是日期，直接丢
        if _overlaps(masked, s, e):
            return
        # 投稿/网络首发标签**不再丢弃**，只用来定类型（见 classify_date_kind）：
        # 这类日期现在是"可采纳、但优先级低"，不是"不可用"。
        kind = classify_date_kind(text, s)
        # `ISSN1004-2903 CN 11-2982/P` 里的数字不该被当成日期
        if RE_BAD_YEAR.search(_ctx(text, s)):
            return
        year = int(value[:4])
        if not (1900 <= year <= 2100):
            return
        out.append((value, s, e, score, kind))

    for m in RE_YMD.finditer(text):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if mo and 1 <= int(mo) <= 12:
            if d and 1 <= int(d) <= 31:
                push(f"{y}-{int(mo):02d}-{int(d):02d}", m.start(), m.end(), 2)
            else:
                push(f"{y}-{int(mo):02d}", m.start(), m.end(), 3)
    for m in RE_MON_Y.finditer(text):
        mo = MONTHS.get(m.group(1).lower()[:3], 0)
        if mo:
            push(f"{m.group(2)}-{mo:02d}", m.start(), m.end(), 3)
    for m in RE_D_MON_Y.finditer(text):
        mo = MONTHS.get(m.group(2).lower()[:3], 0)
        if mo and 1 <= int(m.group(1)) <= 31:
            push(f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}",
                 m.start(), m.end(), 2)

    # 排序：先比类型，再比形态分，最后比位置
    out.sort(key=lambda t: (DATE_KIND_RANK[t[4]], -t[3], t[1]))
    # 同值去重（同一期号可能在页眉页脚各印一次）。排序后取第一条，
    # 留下的就是**类型最好、位置最靠前**的那处证据 —— 所以同一串日期
    # 既被印在"收稿日期"后面又被印在"网络首发日期"后面时，留下的是更好的那个类型。
    seen: set[str] = set()
    uniq = []
    for item in out:
        if item[0] in seen:
            continue
        seen.add(item[0])
        uniq.append(item)
    return uniq


def _pages_str(a: str | int, b: str | int) -> str:
    """页码格式化：`765-770`。Zotero 的 pages 字段就是这个形态。"""
    return f"{a}-{b}"


def _extract_volume_issue_pages(text: str) -> dict:
    """抽卷 / 期 / 页码，返回 {字段: (值, 起, 止)}（抽不到的字段不出现）。

    为什么要一个函数一起抽、而不是三个独立函数各扫一遍：
    `37(4): 765-770` 这一处文本同时给出三个值，三个函数各扫会导致**证据
    指向同一行却互不知道**，而且中文"第 14 卷 第 1 期"里"第...期"的匹配
    依赖"卷"的匹配结果（见 RE_CN_NO 的负向回顾）。合在一起抽更省事也更准。
    """
    found: dict[str, tuple[str, int, int]] = {}

    def put(field: str, value: str, s: int, e: int) -> None:
        # 先到先得：同一种格式通常只在刊头出现一次，第一次出现的更可信
        # （后面的可能是参考文献里别人的卷期）。
        if field not in found and value:
            found[field] = (value, s, e)

    # ---- 1) 中文"第 N 卷 第 M 期"
    for m in RE_CN_VOL.finditer(text):
        put("volume", m.group(1), m.start(), m.end())
        break
    for m in RE_CN_NO.finditer(text):
        put("issue", m.group(1), m.start(), m.end())
        break

    # ---- 2) 英文 `Vol. 37, No. 4`
    for m in RE_VOL_EN.finditer(text):
        put("volume", m.group(1), m.start(), m.end())
        break
    for m in RE_NO_EN.finditer(text):
        put("issue", m.group(1), m.start(), m.end())
        break

    # ---- 3) `37(4): 765-770`（卷期页一次给全，最省事的一种）
    m = RE_VOL_ISSUE_PAGES.search(text)
    if m:
        put("volume", m.group(1), m.start(1), m.end(1))
        put("issue", m.group(2), m.start(2), m.end(2))
        put("pages", _pages_str(m.group(3), m.group(4)),
            m.start(3), m.end(4))

    # ---- 4) 带年份的引用格式 `2025, 37(12): 1846~1865`
    m = RE_CITE_YEAR.search(text)
    if m:
        put("volume", m.group(1), m.start(1), m.end(1))
        put("issue", m.group(2), m.start(2), m.end(2))
        put("pages", _pages_str(m.group(3), m.group(4)),
            m.start(3), m.end(4))

    # ---- 5) `pp. 765-770` / `第 765-770 页`
    if "pages" not in found:
        m = RE_PP.search(text) or RE_PAGE_CN.search(text)
        if m:
            put("pages", _pages_str(m.group(1), m.group(2)),
                m.start(), m.end())

    # ---- 6) 文章编号 `…(2024)03-0296-05` → 起页 296、共 5 页 → 296-300
    #
    # 为什么这个要放在最后（优先级最低）：它是**推算**出来的，不是原文直接
    # 写的页码区间。推算就会错（比如页码跨了位数、或者编号里编码的不是页数），
    # 所以只要前面几种直接格式命中过，就不用它。
    m = RE_ARTICLE_NO.search(text)
    if m and "pages" not in found:
        start_page, count = int(m.group(3)), int(m.group(4))
        if count > 0:
            put("pages", _pages_str(start_page, start_page + count - 1),
                m.start(), m.end())
        # 文章编号里的期号也能用（`(2024)03-…` → 期 3），但卷抽不到
        put("issue", str(int(m.group(2))), m.start(), m.end())

    return found


# ---------------------------------------------------------------- 作者抽取
#
# 概览（详细理由见模块头"作者为什么单独处理"）：
#   1. 先找**带标签的作者行**（学位论文封面 `作者姓名: 张三`）—— 几乎不会错，分最高；
#   2. 再在正文前若干行里找**像一串姓名**的行（期刊首页标题下方那行）；
#   3. 找到后把整行切成姓名项，逐项清洗（角标 / 通讯作者标记 / et al.）；
#   4. 拆分姓名时一律保守：拆不准就整名放 lastName。

# 只在正文的前这么多行里找无标签的作者行。
# 为什么要有这个限制：作者行在版式上就在标题下方，而正文深处任何一行
# 两个汉字、三个汉字都可能"像姓名"（小标题、页眉、目录项）。靠位置先砍掉
# 绝大部分假阳性，比堆关键词表可靠。
AUTHOR_SCAN_LINES = 30

# 作者行的标签。判据是"标签后面紧跟冒号"，所以 `作者简介:` 不会被 `作者` 命中
# （它后面跟的是"简"），不用再单独排除 —— 这一条是踩过的坑：宽松匹配会把
# 作者简介整段当成作者行。
AUTHOR_LABELS = (
    "研究生姓名", "学位申请人", "作者姓名", "第一作者", "通讯作者", "作者",
    "姓名", "authors", "author",
)

# 一眼不是作者行的行内关键词。
# 为什么要按**行**排除而不是按整页：作者行的上一行常常是标题、下一行常常是
# 机构，按整页排除会把作者行一起排掉。
AUTHOR_LINE_REJECT = (
    "摘要", "abstract", "关键词", "keywords", "中图分类号", "文献标识码",
    "文章编号", "doi", "http", "@", "基金", "收稿", "录用", "修回",
    "引用格式", "简介", "大学", "学院", "研究院", "研究所", "实验室",
    "编辑部", "出版社", "导师", "指导教师", "学科专业", "专业名称",
    "学位", "答辩", "提交日期", "vol.", "no.", "pp.", "issn", "isbn",
    "目录", "目次", "版权", "主编", "教授", "研究员", "申请号", "学号",
)

# 姓名的形态。
#   · 中文名 2~8 个汉字，允许 `·`（少数民族姓名里常见）；
#     单字姓名不认 —— 一个汉字"像姓名"的概率太高（"李"、"张"都可能是别的东西）。
#   · 西文名 1~3 个词，每个词以字母开头（允许 ' 和 -），至少一个词首字母大写
#     （用来排除整行小写的正文片段）。带连字符的复姓**能通过校验**，
#     但拆分时会整名放 lastName（见 split_name）。
RE_CN_NAME = re.compile(r"^[\u4e00-\u9fff·]{2,8}$")
RE_LATIN_WORD = re.compile(r"^[A-Za-z][A-Za-z'’\-]*\.?$")

# 姓氏前缀/小品词。带这些词的西文名（`van der Waals`）不能按"最后一个词是姓"
# 拆 —— 那会拆成 first=`van der` / last=`Waals`。宁可不拆。
NAME_PARTICLES = frozenset((
    "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos",
    "la", "le", "bin", "ibn", "ter", "ten", "st", "st.",
))

# 学位/贵族后缀：`Jr.` `Sr.` `III`。带后缀时整名放 lastName —— 拆了会把后缀当姓。
RE_NAME_SUFFIX = re.compile(r"(?:^|\s)(?:jr|sr|ii|iii|iv)\.?$", re.I)

# 姓名项的切分：逗号/分号/顿号，以及英文列表里的 ` and ` / ` & `。
# ⚠ ` and ` 必须要求两侧有空白，否则会把 `Anderson` 从中间切开。
RE_AUTHOR_SPLIT = re.compile(r"\s*[,，;；、]\s*|\s+(?:and|&)\s+", re.I)

# 行尾的"作者没列全"标记：`,` `等` / `, et al.`。
# 中文期刊和英文学术文献各有各的写法，两种都要认。
RE_PARTIAL_TAIL = re.compile(
    r"(?:[,，;；、]\s*)?(?:et\s*al\.?|etc\.?|等)\s*$", re.I)

# "这一行列的是中文姓名"的判据。
#
# ⚠ 判的是**姓名本身**含汉字，不是"整行含汉字"：中文期刊的英文作者行里也常混着
#   CJK（机构名、"通讯作者"字样），按整行判会把它误判成中文行。
#   姓名含汉字才真正说明"这一串是中文姓名"。
RE_NAME_CJK = re.compile(r"[\u4e00-\u9fff]")

# 「优先中文行」的加分（用户拍板：中文期刊首页常同时印中文作者行和英文作者行，
# 要的是**中文那一行** —— 库里已有的中文期刊条目，作者存的也是中文名）。
#
# 权重为什么定 2（现有量级只有 1/2/3）：
#   · 定 1 压不过"英文行多一个角标"（角标 +2 是期刊作者行的典型排版特征），
#     用户要的"优先中文"就落不了地；
#   · 定 3 等于把"姓名是汉字"抬到与"带明确标签（`作者姓名:` / `by`）"同级，
#     一条只有版式旁证的中文行会盖过带明确标签的英文行 —— 那等于让**语言
#     压过版式证据**，本末倒置；
#   · 定 2 的语义正好是"和『姓名带角标』『作者没列全』一样，属于一条**旁证**"。
#
# ⚠ 它只进**排序**，不进"够不够格"的门槛（见 extract_creators 里 score 与 rank
#   分开写）：否则一条躺在正文深处、本来证据不足的中文行会因为"含汉字"这一个
#   理由被够格，保守策略就被悄悄放松了。
#
# ⚠ 它是**加分**不是"英文行减分"，所以**不存在**"没有中文行就不给建议"：
#   纯英文文献里英文行是唯一的候选，它照样按原分数过门槛。
CJK_NAME_BONUS = 2


def _is_name_like(name: str) -> bool:
    """这个词像不像一个姓名（中文整名或西文 1~3 个词）。"""
    if not name:
        return False
    if RE_CN_NAME.fullmatch(name):
        return True
    words = name.split()
    if not 1 <= len(words) <= 3:
        return False
    if not all(RE_LATIN_WORD.fullmatch(w) for w in words):
        return False
    # 至少要有一个词首字母大写：全小写的多半是正文片段而不是姓名
    return any(w[0].isupper() for w in words)


def _strip_name_marks(tok: str) -> tuple[str, bool]:
    """去掉姓名上的角标与通讯作者标记，返回 (干净姓名, 原本是否带标记)。

    实现顺序有讲究（实测的形态）：
      · `张三1`、`李四1,2`、`王五2*` —— 角标在被切分后可能已经是
        `1,2` 或 `2*` 这种混合尾巴，所以用"一次扫掉尾部所有角标字符"
        而不是"去掉最后一个数字"；
      · `*张三`、`†李四` —— 标记也可能在名字前面（通讯作者常这么标）；
      · **不能顺手去掉句点**：`S.` 这种缩写名的句点是有意义的，
        去掉了就分不清它是缩写还是姓。
    """
    s = (tok or "").strip()
    marked = False
    s2 = re.sub(r"^[\*\u2020\u2021\u00a7\u00b6\u2217#]+", "", s).strip()
    marked = marked or (s2 != s)
    s = s2
    s3 = re.sub(r"[\s\d,，;；\*\u2020\u2021\u00a7\u00b6\u2217#]+$", "", s).strip()
    marked = marked or (s3 != s)
    return s3, marked


def split_name(raw: str) -> tuple[str, str]:
    """把一个姓名拆成 `(firstName, lastName)`；拆不准就整名放 lastName。

    为什么"拆不准"一律整名放 lastName，而不是干脆不给这条建议：
    Zotero 的 lastName 放整名照样能正常显示、引用样式也能正确处理
    （中文名本来就是这么存的）；而**拆错**的代价大得多 ——
    `Zhang San` 拆成 first=Zhang / last=San 之后，引文里会变成 `San, Z.`，
    用户不逐条核对根本发现不了。宁可少拆，不可拆错。

    判定顺序（每一步都对应一种"拆了大概率错"的形态）：
      1. 含 CJK            → 整名放 lastName（Zotero 里中文名就是这样存的）
      2. 带 Jr./Sr./III    → 整名（拆了会把后缀当姓）
      3. 带连字符          → 整名（复姓与复名的分界没有可靠依据）
      4. 带姓氏小品词      → 整名（`van der Waals` 不能按"最后一词是姓"拆）
      5. 含 `Last, First`  → 逗号前是姓（这是**唯一**能确定的西文形态）
      6. 只有一个词        → 整名（纯姓、或纯缩写，无从拆）
      7. 全大写词 ≥2 个    → 整名（分不清哪个是姓）
      8. 恰好一个全大写词  → 它是姓（中文期刊英文作者行的惯例：`ZHANG San`）
      9. 词数 ≥ 4          → 整名（多半混进了机构或备注）
     10. 其余              → 按西文惯例：最后一个词是姓
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    # 1) 中文（含 `·` 的少数民族姓名）：不拆
    if any("\u4e00" <= ch <= "\u9fff" for ch in s):
        return "", s
    # 2) 后缀
    if RE_NAME_SUFFIX.search(s):
        return "", s
    # 3) 连字符
    if "-" in s or "\u2010" in s or "\u2011" in s:
        return "", s
    words = s.split()
    # 4) 姓氏小品词
    if any(w.lower().rstrip(".") in NAME_PARTICLES for w in words):
        return "", s
    # 5) `Last, First`
    if "," in s:
        last, _, first = s.partition(",")
        last, first = last.strip(), first.strip()
        if last and first:
            return first, last
        return "", s
    # 6) 只有一个词
    if len(words) < 2:
        return "", s
    caps = [w for w in words if len(w.rstrip(".")) >= 2 and w.isupper()]
    # 7) 全大写词 ≥2 个 → 分不清
    if len(caps) >= 2:
        return "", s
    # 8) 恰好一个全大写词 → 它是姓
    if len(caps) == 1:
        last = caps[0]
        first = " ".join(w for w in words if w != last)
        return first, last
    # 9) 词太多
    if len(words) >= 4:
        return "", s
    # 10) 默认西文序
    return " ".join(words[:-1]), words[-1]


def _names_in_line(line: str) -> tuple[list[str], bool, bool] | None:
    """把一行文本切成姓名列表；不像"一串姓名"就返回 None。

    返回 `(姓名列表, 是否作者没列全, 是否带角标/通讯作者标记)`。

    为什么要求"**每一个**切出来的项都像姓名"才接受整行：
    一半像姓名一半像机构的行（`张三1, 某某大学某某学院`）如果只取像的那一半，
    就等于在猜"哪一半才是作者"。整行不接受、交给模型或用户，代价小得多。
    """
    s = (line or "").strip()
    if not s:
        return None
    # 机构角标/通讯作者说明常写在括号里，从第一个括号处截断
    # （`张三1, 李四2（某某大学）` → `张三1, 李四2`）。
    #
    # ⚠ 截断必须在"排除关键词"检查**之前**做，否则 `张三1, 李四2（某某大学）`
    #   会因为括号里的"大学"被整行拒掉 —— 而它恰恰是最典型的中文期刊作者行。
    #   截断后为空说明整行都在括号里，那就不像作者行。
    head = re.split(r"[（(\[【]", s, 1)[0].strip()
    if not head or len(head) > EVIDENCE_MAX:
        # 超过证据长度上限的行不可能是作者行（作者行很短），
        # 而且证据片段会装不下它 —— 见 build_evidence 的窗口上限。
        return None
    s = head
    low = s.lower()
    if any(bad in low for bad in AUTHOR_LINE_REJECT):
        return None
    # 行尾的"作者没列全"标记
    partial = False
    m = RE_PARTIAL_TAIL.search(s)
    if m:
        partial = True
        s = s[:m.start()].strip()
    if not s:
        return None
    names: list[str] = []
    marked_any = False
    for tok in RE_AUTHOR_SPLIT.split(s):
        # 按逗号切完以后，英文列表最后一项可能还带着 `and`（`A, B, and C` 里
        # 逗号先被匹配上）。不剥掉的话会多出一位叫 "and Wei Wang" 的作者。
        # `and` 后面必须有空白才算连接词，所以 `Anderson` 不会被误伤。
        tok = re.sub(r"^(?:and|&)\s+", "", (tok or "").strip(), flags=re.I)
        if not tok:
            continue
        name, marked = _strip_name_marks(tok)
        marked_any = marked_any or marked
        if not name:
            continue
        # `王五等`（"等"直接贴在最后一个名字后面）：剥掉并标"没列全"
        if name.endswith("等") and RE_CN_NAME.fullmatch(name[:-1] or ""):
            partial = True
            name = name[:-1]
        if not _is_name_like(name):
            return None          # 有一项不像姓名 → 整行不作数
        names.append(name)
    if not names or len(names) > 15:
        return None
    return names, partial, marked_any


def _labeled_author_value(line: str) -> str | None:
    """`作者姓名: 张三` 这类带标签的行 → 标签后的内容；不是这种行返回 None。

    ⚠ 标签后面**必须紧跟冒号**才算：`作者简介: 张三，1980 年生…` 里
      `作者` 后面跟的是"简"，所以不会命中 —— 这是刻意的，作者的简介
      不是作者列表（本机踩过：宽松匹配会把整段简介当姓名）。
    """
    s = (line or "").strip()
    for label in AUTHOR_LABELS:
        m = re.match(rf"{re.escape(label)}\s*[:：]\s*(.+)$", s, re.I)
        if m:
            return m.group(1).strip()
    return None


def _head_lines(text: str) -> list[tuple[str, int, int]]:
    """把拼接后的正文按行切开，带上每行在原文里的 (起, 止) 偏移。

    为什么要带偏移：证据要指回**原文的位置**（`build_evidence` 按偏移裁片段），
    只留行号的话用户核对时还得自己数行。
    """
    out: list[tuple[str, int, int]] = []
    pos = 0
    for line in (text or "").split("\n"):
        out.append((line, pos, pos + len(line)))
        pos += len(line) + 1
    return out


def _same_as_title(line: str, title: str) -> bool:
    """这一行是不是条目的标题本身（标题绝不能被当成作者行）。"""
    t = re.sub(r"\s+", "", title or "")
    if not t:
        return False
    s = re.sub(r"\s+", "", line or "")
    return bool(s) and (s == t or s.startswith(t))


def extract_creators(pages: list[str], title: str = "") -> dict | None:
    """从首页/版权页抽作者名单；抽不到返回 None。

    返回 `{"creators": [{"lastName", "firstName", "creatorType"}], "partial": bool,
    "start": int, "end": int, "labeled": bool, "unsplit": int, "score": int,
    "cjk_names": bool}`。

    打分（分数最高的一行胜出，同分取靠前的）：
      +3 带明确标签（`作者姓名:` / 研究生姓名: / 英文封面里单独一行的 `by`）
         —— 学位论文封面那种，几乎不会错
      +2 行尾有"作者没列全"标记（`等` / `et al.`）—— 说明这行确实在列作者
      +2 姓名带角标数字或 `*` —— 期刊作者行的典型特征（关联机构用）
      +1 出现在正文前 12 行内
      +1 上一行像标题（长度 ≥ 6）—— 作者行就在标题下方

    **排序分 = 上面的分数 +「优先中文行」的加分**（姓名含汉字 +2）。两者分开的理由：
    "含汉字"只说明**想要哪一种写法**，不说明"这行到底是不是作者行" —— 拿它去够
    门槛会把保守策略放松。所以够不够格只看前者，语言偏好只在都够格时决定选谁。
    结果就是：中英两行都在时选中文行；只有英文行时它照旧过门槛被选中。
    """
    text = head_text(pages, HEAD_PAGES)
    lines = _head_lines(text)
    best: dict | None = None
    best_rank = 0
    for i, (line, start, end) in enumerate(lines):
        if i >= AUTHOR_SCAN_LINES:
            break
        stripped = line.strip()
        if not stripped or _same_as_title(stripped, title):
            continue
        label_value = _labeled_author_value(stripped)
        body = label_value if label_value is not None else stripped
        got = _names_in_line(body)
        if got is None:
            continue
        names, partial, marked = got
        # 上一行：既用于"像不像标题"的加分，也用于认英文封面那种
        # `by` 单独占一行的版式（
        #     by
        #     San Zhang
        # ）—— 那个 `by` 就是**标签**，有它的时候单个姓名也算证据充分。
        prev_text = ""
        for j in range(i - 1, -1, -1):
            prev = lines[j][0].strip()
            if prev:
                prev_text = prev
                break
        by_label = prev_text.strip().strip(":：").lower() == "by"
        labeled = label_value is not None or by_label
        # 无标签行里**孤零零一个姓名**、又没有任何角标/省略标记时，证据太弱：
        # 中文标题（`某某研究`）本身就长得像姓名，一个汉字的差别就会把标题
        # 当成作者。宁可漏（交给模型/用户），也不要错。
        # 带标签的行（含 `by`）不受这条限制 —— 单作者学位论文就是这么写的。
        if not labeled and len(names) == 1 and not (partial or marked):
            continue
        score = 0
        if labeled:
            score += 3
        if partial:
            score += 2
        if marked:
            score += 2
        if i < 12:
            score += 1
        if len(prev_text) >= 6:      # 上一行够长 → 像标题
            score += 1
        # 「优先中文行」只进排序分，不进 score（门槛判的是 score）——
        # 详见 CJK_NAME_BONUS 的说明：语言偏好决定"选谁"，不决定"够不够格"。
        cjk_names = any(RE_NAME_CJK.search(n) for n in names)
        rank = score + (CJK_NAME_BONUS if cjk_names else 0)
        if rank > best_rank:
            best_rank = rank
            best = {"names": names, "partial": partial, "start": start,
                    # 证据跨度按上限截断：作者行后面常跟着一长串机构，
                    # 整行可能超过证据窗口，截断后 `_span` 仍然完整落在片段里。
                    "end": min(end, start + EVIDENCE_MAX),
                    "labeled": labeled, "score": score, "rank": rank,
                    "cjk_names": cjk_names}
    # 最低分数线**只看 score**（不看排序分）：一条加分都没有的行说明"没有任何
    # 旁证说它是作者行"，那就宁可不给建议（保守策略）。带标签的行拿 3 分必然过线。
    if best is None or best["score"] < 2:
        return None
    creators = []
    unsplit = 0
    for name in best["names"]:
        first, last = split_name(name)
        if not first and not last:
            continue
        if not first:
            unsplit += 1
        creators.append({"lastName": last, "firstName": first,
                         "creatorType": "author"})
    if not creators:
        return None
    return {"creators": creators, "partial": bool(best["partial"]),
            "start": best["start"], "end": best["end"],
            "labeled": best["labeled"], "unsplit": unsplit,
            # `score`（够不够格的依据）与 `cjk_names`（选中这行的原因之一）
            # 一并返回：出问题时能直接看出"是证据强，还是靠语言偏好赢的"。
            "score": best["score"], "cjk_names": best["cjk_names"]}


def _creator_note(cr: dict, source: str = "rule", verified=None) -> str:
    """给 creators 建议配的一句人话说明（会写进 `evidence.note`）。

    为什么要说这么多：作者是"错一个字符就毁掉引文"的字段，
    用户必须知道**这一条是怎么来的**才敢勾选。没把握的地方更要说出来。
    """
    parts: list[str] = []
    if source == "model":
        # 模型路径的措辞要更重：它给的值本身就是 low，而且证据还可能定位不到
        parts.append("这是模型从正文里认出来的作者。"
                     + ("它指的原文没能在正文里定位到，请**务必**对照 PDF 核对。"
                        if verified is False else
                        "请对照 PDF 核对它指的那段原文。"))
    elif cr.get("labeled"):
        parts.append("取自文献里带标签的作者行（如「作者姓名:」）。")
    else:
        parts.append("取自正文首页标题下方的作者行，请对照 PDF 核一遍。")
    n = len(cr.get("creators") or [])
    if cr.get("unsplit"):
        parts.append(f"其中 {cr['unsplit']} 位姓名没能可靠拆分，"
                     f"整名放在 lastName（引用样式可能不完美，但不会拆错）。")
    if cr.get("partial"):
        parts.append(f"⚠ 原文只列了前 {n} 位作者（et al./等），实际作者可能更多；"
                     f"要不要采纳请自行判断。")
    return "".join(parts)


# ---------------------------------------------------------------- 模型兜底

MODEL_SYSTEM = (
    "你是文献元数据抽取助手。只从给定文字里抄出信息，不要推测、不要补全、"
    "不要用外部知识。文字里没有的字段一律留空字符串。输出 JSON。"
)

# 提示词里反复强调"没有就留空"，是因为小模型在这类任务上最大的风险是**编**：
# 它见过太多文献，会凭"这类文章一般发在哪年哪卷"填一个看起来合理的值。
# 作者上这个风险更高：它会凭"这个领域常引谁"编出一串人名。
#
# `{creator_hint}` / `{creator_rules}` 只在**规则没搞定作者**时才填内容。
# 为什么要按需填而不是永远问：`_model_suggest` 的原则是"少问就少编" ——
# 不问的字段模型没有机会瞎给值，提示词短一点小模型的注意力也更集中。
MODEL_PROMPT = """从下面这段文献正文（首页节选）里，抄出这些元数据字段的值：
date（日期）、date_kind（日期类型）、DOI、volume（卷）、issue（期）、
pages（起止页码）{creator_hint}。

【正文】
{text}

【要求】
1. 只抄正文里**字面出现过**的内容；找不到的字段必须留空字符串 ""，绝对不要猜。
2. date 优先抄**正式出版日期**（如 "2025 年 12 月" → "2025-12"、"Dec. 2025" → "2025-12"）。
   如果正文里**只有**收稿日期 / 网络首发日期 / 在线发表日期，**也要抄出来、不要留空** ——
   这些日期照样能用（date 最终只用来算按发表年份的基础权重，差一两年影响很小，
   留空反而丢掉整篇的初始权重）。
3. date_kind 填上一步那个日期的类型：正式出版填 "published"，
   网络首发/在线发表/优先出版填 "online_first"，收稿/投稿/修回/录用填 "submitted"，
   实在看不出填 "unknown"。
4. DOI 只给纯净形态（不要 "doi:" 前缀，不要 URL），例如 "10.1234/abc.2025.001"。
5. pages 给 "起页-止页" 形态，例如 "765-770"。
6. volume / issue 只给数字，不要带 "Vol." "第" "期" 这些字。
7. evidence 字段填**正文里支持该值的那一小段原文**（照抄，20-60 字），
   找不到出处的字段就不要给值。{creator_rules}
只输出 JSON：
{{"date": "", "date_kind": "", "DOI": "", "volume": "", "issue": "", "pages": "",
  "creators": [], "creators_partial": false,
  "evidence": {{"date": "", "DOI": "", "volume": "", "issue": "", "pages": "",
                "creators": ""}}}}"""

# 只追加在作者需要模型兜底时的提示词片段
CREATOR_HINT = "、creators（作者列表）"
CREATOR_RULES = """
8. creators 填**作者姓名列表**（按正文里的先后顺序），每一项是一个对象：
   {"lastName": "姓", "firstName": "名"}。规则：
   · 中文姓名**不要拆**，整个姓名放 lastName，firstName 留空字符串 ""；
   · 西文姓名按 "名 姓" 拆（"San Zhang" → firstName="San", lastName="Zhang"）；
     拿不准怎么拆就整个姓名放 lastName、firstName 留空；
   · 去掉姓名上的角标数字（"张三1" → "张三"）与 * † ‡ 这类通讯作者标记；
   · 没有列全时（正文里出现 "et al." 或 "等"），只抄**列出来的**那几位，
     并把 creators_partial 填 true。
9. creators 在正文里找不到就填空数组 []，**绝对不要**凭印象补全作者名单 ——
   编出来的作者比没有作者更糟，用户很可能不会逐条核对。
10. evidence 里的 creators 填**作者那一行的原文**（照抄）。"""


def model_available() -> tuple[bool, str]:
    """本地模型是否可用，返回 (可用, 说明)。

    为什么要单独探一次：不可用时**必须优雅退化成纯规则**（用户可能根本没装
    Ollama）。这里只探测不抛异常 —— judge 内部已经把网络错误都吃掉了，
    但保险起见还是包一层。
    """
    if judge is None:
        return False, "judge 模块不可用"
    try:
        st = judge.llm_status()
        return bool(st.get("available")), str(st.get("hint") or st.get("provider") or "")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _normalize_kind(raw: str) -> str:
    """把模型回的日期类型归一成内部枚举；认不出返回空串。

    模型可能回英文枚举（`online_first`），也可能回中文标签（`网络首发`）——
    两种都要认：前者是提示词要求的，后者是小模型更顺手的写法。
    认不出的**不猜**，返回空串让调用方退回"从证据原文判类型"。
    """
    t = (raw or "").strip().lower()
    if not t:
        return ""
    if t in DATE_KIND_RANK:
        return t
    return kind_from_text(raw)


def _parse_model_creators(raw) -> list[dict]:
    """把模型回的 `creators` 归一成 `[{"lastName", "firstName", "creatorType"}]`。

    小模型在这件事上会给出三种形态，全都得认（认不出就当没给，不猜）：
      · `[{"lastName": "张", "firstName": "三"}, …]` —— 提示词要求的形态；
      · `["张三", "李四"]` —— 只给整名；
      · `"张三, 李四"` —— 干脆给一个字符串。
    后两种都要过 `split_name` 拆一遍（拆不准就整名放 lastName）。

    ⚠ 每一项里模型可能把关**键名**写成 `last`/`first`/`name`。同样要认：
      模型输出的键名不稳定是常态，只认一种写法等于把大部分结果丢掉。
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for it in items:
        first = last = ""
        if isinstance(it, dict):
            first = str(it.get("firstName") or it.get("first") or "").strip()
            last = str(it.get("lastName") or it.get("last") or "").strip()
            if not first and not last:
                # 只给了 `name` 这种合并写法 → 走拆分
                whole = str(it.get("name") or "").strip()
                if not whole:
                    continue
                first, last = split_name(_strip_name_marks(whole)[0])
        else:
            whole = str(it or "").strip()
            if not whole:
                continue
            first, last = split_name(_strip_name_marks(whole)[0])
        # 模型给的姓名同样要洗角标与通讯作者标记（它常把 `张三1` 原样抄回来）
        first = _strip_name_marks(first)[0]
        last = _strip_name_marks(last)[0]
        if not first and not last:
            continue
        if not last:                 # 模型把整名塞进了 firstName → 纠正回 lastName
            last, first = first, ""
        out.append({"lastName": last, "firstName": first,
                    "creatorType": "author"})
    if len(out) > 15:                # 一个人不可能 15 位以上还只印在首页作者行上
        return []
    return out


def _truthy(value) -> bool:
    """模型回的布尔字段可能是 `true` / `"true"` / `"是"` / `1`，统一判一下。"""
    if isinstance(value, bool):
        return value
    s = str(value or "").strip().lower()
    return s in ("1", "true", "yes", "y", "是", "true.", "t")


def _model_suggest(pages: list[str], need: list[str], model: str = ""
                   ) -> tuple[dict[str, tuple[str, str]], dict]:
    """调本地模型抽缺失字段，返回 `({字段: (值, 证据文本)}, 附带信息)`。

    need 是"规则没搞定、且当前为空"的字段 —— 只问这些，不问全部。为什么：
      · 少问就少编（模型对没问的字段没有机会瞎给值）；
      · 提示词短一点，小模型的注意力也更集中；
      · 规则已经确定正确的字段没必要再让模型"复核"一遍 —— 它复核错了更麻烦。

    ⚠ `creators` 特殊：它是多值字段，值不是字符串而是对象列表，
      所以**不进**第一个返回值（那个 dict 的值会被调用方按 `raw, ev = got[field]`
      解包成两个字符串），而是走第二项 `meta["creators"]`。
      附带信息里还有 `date_kind`（模型声明的日期类型）与 `creators_partial`。
      为什么附带信息不塞进第一个 dict：那个 dict 的键必须**全是待补字段**，
      调用方按 `need` 遍历它；混进 `date_kind` 会被当成待补字段，
      凭空冒出一条 `field="date_kind"` 的建议。
    """
    if judge is None or not need:
        return {}, {}
    text = head_text(pages, HEAD_PAGES)[:6000]
    if len(text.strip()) < MIN_TEXT_CHARS:
        return {}, {}
    want_creators = "creators" in need
    prompt = MODEL_PROMPT.format(
        text=text,
        creator_hint=CREATOR_HINT if want_creators else "",
        creator_rules=CREATOR_RULES if want_creators else "",
    )
    try:
        res = judge.generate(prompt, system=MODEL_SYSTEM, model=model,
                             json_mode=True, temperature=0.0)
    except Exception:  # noqa: BLE001  —— 模型层任何异常都不该冒泡给调用方
        return {}, {}
    if not isinstance(res, dict) or not res.get("ok"):
        return {}, {}
    try:
        good, data = judge.parse_json(res.get("text") or "")
    except Exception:  # noqa: BLE001
        return {}, {}
    if not good or not isinstance(data, dict):
        return {}, {}
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    out: dict[str, tuple[str, str]] = {}
    for field in need:
        if field == "creators":
            continue          # 多值字段，走 meta（见上面的说明）
        raw = data.get(field)
        if raw is None:
            continue
        val = str(raw).strip()
        if not val or val.lower() in ("null", "none", "n/a", "-"):
            continue
        ev = str((evidence or {}).get(field) or "").strip()
        out[field] = (val, ev)
    meta = {"date_kind": _normalize_kind(str(data.get("date_kind") or "")),
            "creators": [], "creators_evidence": "", "creators_partial": False}
    if want_creators:
        meta["creators"] = _parse_model_creators(data.get("creators"))
        meta["creators_evidence"] = str((evidence or {}).get("creators") or "").strip()
        # "没列全"取"模型自己声明的"与"证据原文里能看到 et al./等"的**或**：
        # 只要有一处说明作者没列全，就该提示用户 —— 漏提示的代价是
        # 用户以为作者齐了，而多提示只是让他多看一眼。
        meta["creators_partial"] = bool(
            _truthy(data.get("creators_partial"))
            or bool(RE_PARTIAL_TAIL.search(meta["creators_evidence"])))
    return out, meta


def _verify_evidence(pages: list[str], ev: str) -> dict:
    """核对模型给的证据是否真的在正文里，返回 evidence dict。

    为什么要校验：模型给的"证据"也可能是编的（本机实测见过它把参考文献里的
    一行抄来当证据）。证据是用户唯一能核对的东西 —— 证据本身是假的，
    这条建议就失去了全部意义。所以：能在正文里找到就带页码，找不到就明确
    标 `verified=False`，让用户知道"这条只能自己看 PDF 确认"。
    """
    text = head_text(pages, HEAD_PAGES)
    if ev:
        # 去掉空白再比：正文里的换行/空格和模型复述的不一定一致
        flat = re.sub(r"\s+", "", text)
        needle = re.sub(r"\s+", "", ev)
        # 长证据按前 24 字定位（模型常把中间几个字抄错，全串比对太脆）
        probe = needle[:24] if len(needle) > 24 else needle
        idx = flat.find(probe) if probe else -1
        if idx != -1:
            # 把扁平化后的偏移换算回原文偏移：逐字符数回去
            count, pos = 0, 0
            for pos, ch in enumerate(text):
                if count >= idx:
                    break
                if not ch.isspace():
                    count += 1
            return {"page": _locate(pages, pos, HEAD_PAGES),
                    "text": ev[:EVIDENCE_MAX], "verified": True}
        return {"page": 0, "text": ev[:EVIDENCE_MAX], "verified": False,
                "note": "模型给的这段原文没能在正文里定位到，请自行核对 PDF"}
    return {"page": 0, "text": "", "verified": False,
            "note": "模型没有给出原文出处，请自行核对 PDF"}


# ---------------------------------------------------------------- 值清理

def _clean_value(field: str, value: str) -> str:
    """把值清理成 Zotero 里该有的形态；清不出可用值就返回空串。

    为什么要有这一步：模型给的常是 `第 37 卷`、`Vol. 37`、`37(4)` 这种
    带修饰的写法，直接写进去 Zotero 会显示成一团乱。规则抽的值本来就干净，
    过一遍也只是顺手（幂等）。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if field == "DOI":
        v = _clean_doi(v)
        return v if _valid_doi(v) else ""
    if field == "volume":
        m = re.search(r"(\d{1,4})", v)
        return m.group(1) if m else ""
    if field == "issue":
        m = re.search(r"(\d{1,3})", v)
        return m.group(1) if m else ""
    if field == "pages":
        # 允许 `765-770`、`765–770`、`765~770`、`765-70`（末页缩写）
        m = re.search(r"(\d{1,5})\s*[-–—~～]\s*(\d{1,5})", v)
        if m:
            return _pages_str(m.group(1), m.group(2))
        m = re.search(r"^\s*(\d{1,5})\s*$", v)
        return m.group(1) if m else ""
    if field == "date":
        return _clean_date(v)
    return v


def _clean_date(value: str) -> str:
    """把日期归一成 `YYYY` / `YYYY-MM` / `YYYY-MM-DD`。

    为什么不留原文（比如 `2025 年 12 月`）：Zotero 的 date 字段有它自己的
    解析规则，`2025 年 12 月` 在中文界面能认、切到英文界面就散架；
    ISO 形态两边都认，而且排序正常。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"\d{4}", v):
        return v
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", v)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), m.group(3)
        if not 1 <= mo <= 12:
            return y
        if d and 1 <= int(d) <= 31:
            return f"{y}-{mo:02d}-{int(d):02d}"
        return f"{y}-{mo:02d}"
    m = RE_MON_Y.search(v)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3], 0)
        if mo:
            return f"{m.group(2)}-{mo:02d}"
    m = RE_D_MON_Y.search(v)
    if m:
        mo = MONTHS.get(m.group(2).lower()[:3], 0)
        if mo and 1 <= int(m.group(1)) <= 31:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    # 最后退一步：值是一句话里的日期（例如模型回了 "Published 2025-03"）。
    # ⚠ 这里**不能**递归调回 _clean_date(m.group(0))：`RE_YMD` 只是
    #   `search`（不是 fullmatch），匹配到的子串再喂回来会再次匹配到自己，
    #   变成无限递归 —— 本机实测直接 RecursionError 崩掉。改成用已匹配的
    #   分组自己拼值。
    m = RE_YMD.search(v)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if mo and 1 <= int(mo) <= 12:
            if d and 1 <= int(d) <= 31:
                return f"{y}-{int(mo):02d}-{int(d):02d}"
            return f"{y}-{int(mo):02d}"
        return y
    return ""


# ---------------------------------------------------------------- 建议构造

def _attach_date_kind(sug: dict, kind: str) -> None:
    """给 date 建议标上"这是哪种日期"（非 date 字段直接返回）。

    为什么要标（而不是只给个值）：date 现在会采纳投稿/网络首发日期，
    用户必须在弹窗里看得出这一点 —— 否则他没法判断"要不要把这个值写进 Zotero"。
    只给值不给类型，等于把判断责任推给一个看不见的理由。

    为什么同时写两处：
      · `sug["date_kind"]` / `date_kind_label` —— 给界面用的稳定字段（机器读）；
      · `evidence["note"]` —— 给任何"顺带展示证据"的地方用（人读）。
        界面 `metaLine` 已经在渲染 evidence 的 text/page，加 note 后
        展示层迟早会带上它，不必等插件改版。
    """
    if sug.get("field") != "date":
        return
    k = kind or DATE_KIND_UNKNOWN
    if k not in DATE_KIND_LABELS:
        k = DATE_KIND_UNKNOWN
    sug["date_kind"] = k
    sug["date_kind_label"] = DATE_KIND_LABELS[k]
    note = date_kind_note(k)
    if note and isinstance(sug.get("evidence"), dict):
        sug["evidence"]["note"] = note


def _creator_suggestion(cr: dict, evidence: dict, source: str,
                        confidence: str) -> dict:
    """把抽到的作者组成一条建议（形态见模块头"作者为什么单独处理"）。

    `value` 给人读（字符串，和其余 5 个字段同型），`values` 给写回用
    （有序对象列表，字段名就是 Zotero 的 `firstName`/`lastName`/`creatorType`）。
    """
    creators = cr.get("creators") or []

    def _display(c: dict) -> str:
        # 西文名按"名 姓"显示更接近原文；中文名的 firstName 是空的，只显示 lastName
        return (f"{c['firstName']} {c['lastName']}".strip()
                if c.get("firstName") else c.get("lastName", ""))

    sug = {
        "field": "creators", "current": "",
        "value": "; ".join(_display(c) for c in creators),
        "values": creators,
        "n_creators": len(creators),
        "source": source, "confidence": confidence,
        "evidence": evidence,
    }
    if cr.get("partial"):
        # 单独给一个布尔 + 一句人话：界面不该自己去数"要不要提示"，
        # 也不该自己拼这句话（服务端说一次，所有展示端一致）。
        sug["partial"] = True
        sug["warning"] = (f"原文只列了前 {len(creators)} 位作者（et al./等），"
                          f"实际作者可能更多。")
    note = _creator_note(cr, source=source,
                         verified=(evidence or {}).get("verified"))
    if note and isinstance(sug.get("evidence"), dict):
        sug["evidence"]["note"] = note
    return sug


# ---------------------------------------------------------------- 主逻辑

def _current_values(item) -> dict:
    """读条目"当前值"。

    ⚠ 必须从 `item.fields` 读，**不能**从 index.db 的 items 表读 ——
    那张表只有 `year` 和 `doi` 两列，没有 volume / issue / pages / date。
    从索引库读会得出"这些都缺"的错误结论，然后建议一堆用户其实已经有的值。

    ⚠ `creators` 是**唯一的例外**：Zotero 的字段表里没有它，
    作者存在 `item.authors` 里（`schemas.Item.authors`，已解析好的 Author 列表）。
    这里返回作者的显示串，作用有两个：
      · 让 `missing` 判断正确（有任何一位作者就算"已有值"）；
      · 让 skipped 的原因能写成"已有 3 位作者"，用户一眼看懂为什么不补。
    """
    fields = getattr(item, "fields", None) or {}
    out = {f: str(fields.get(f) or "").strip() for f in FIELDS if f != "creators"}
    authors = [a for a in (getattr(item, "authors", None) or [])
               if str(getattr(a, "display", "") or "").strip()]
    out["creators"] = "; ".join(a.display for a in authors)
    return out


def suggest_for_item(item, model: str = "", use_model: bool = True) -> dict:
    """给一个已加载的 Item 生成补全建议（**不修改任何数据**）。

    调用方已经加载过 item 时用它，省一次 Zotero 查询。
    """
    key = str(getattr(item, "key", "") or "")
    title = str(getattr(item, "title", "") or "")
    current = _current_values(item)

    # 只处理"当前为空"的字段。已有值的一律进 skipped 并说明原因 ——
    # 这不是可有可无的装饰：用户看到"什么都没建议"时得知道是"程序没找到"
    # 还是"本来就有值"，否则会以为工具没工作。
    missing = [f for f in FIELDS if not current[f]]
    skipped = []
    for f in FIELDS:
        if not current[f]:
            continue
        if f == "creators":
            # 作者的跳过原因要单独写：用户看到"已有值 '张三; 李四'"也能懂，
            # 但说清"**整条**不动、不会去补第 3 位"更重要 —— 否则他会以为
            # 工具漏了后面几位作者（见模块头"只填空，而且是整条跳过式的填空"）。
            n = len([x for x in current[f].split(";") if x.strip()])
            skipped.append({
                "field": f,
                "why": f"已有 {n} 位作者，按「作者整条跳过」原则不动"
                       f"（不做「补第 N 位」式的合并）"})
            continue
        skipped.append({"field": f, "why": f"已有值 {current[f]!r}，按只填空原则不动"})

    result: dict = {
        "ok": True, "key": key, "title": title,
        "item_type": str(getattr(item, "item_type", "") or ""),
        "suggestions": [], "skipped": skipped,
        "current": current, "notes": [],
        # 模型状态**一开始就占位**：如果规则把 5 个字段全搞定了，下面那段
        # "模型兜底"根本不会执行，键就会缺席 —— 调用方（插件/CLI）按固定
        # 结构取值时会 KeyError。自检里抓到过一次。宁可给个明确的状态。
        "model": {"used": False, "reason": "规则已完成，无需调用模型"},
    }

    if not missing:
        result["notes"].append("这几个字段都已有值，无可补")
        return result

    # ---- 取正文（只看前两页）
    pages: list[str] = []
    source = ""
    try:
        reader = _get_reader()
        pages, source = reader.fulltext_for(item, use_model=False)
    except Exception as exc:  # noqa: BLE001
        # 读正文失败不该让整个模块挂掉：这条建议就当"没找到"，
        # 把原因写进 notes 让用户知道是坏了还是真没有。
        result["notes"].append(f"读取正文失败：{type(exc).__name__}: {exc}")
    pages = pages or []
    result["fulltext_source"] = source
    result["fulltext_pages"] = len(pages)

    if not head_text(pages).strip():
        result["notes"].append("没有可用的 PDF 正文（缺少 PDF、或扫描件提不出文字），"
                              "只能靠规则/模型之外的办法补字段")
        return result
    if len(head_text(pages).strip()) < MIN_TEXT_CHARS:
        result["notes"].append("首页正文过短，判据不足，未做抽取")
        return result

    text = head_text(pages, HEAD_PAGES)
    hits: dict[str, tuple[str, int, int]] = {}
    # 字段的"附带信息"。为什么不让 hits 里有的存三元组、有的存四元组：
    # 下面按 `value, s, e = hits[field]` 解包，元组长短不一就会当场 ValueError。
    # 分开存之后，加新的附带信息（比如将来给 pages 标"这是推算出来的"）
    # 不需要动 hits 的结构。
    hit_meta: dict[str, dict] = {}

    # ---- 第一步：规则（DOI / 卷 / 期 / 页，以及能规则化的日期）
    found = _extract_volume_issue_pages(text)
    for field, trip in found.items():
        if field in missing:
            hits[field] = trip
    if "DOI" in missing:
        doi = _extract_doi(text)
        if doi:
            hits["DOI"] = doi
    if "date" in missing:
        d = _extract_date(text)
        if d:
            # d 是 (值, 起, 止, 形态分, 日期类型)：前三个进 hits，
            # 类型单独记 —— 界面要靠它显示"这是网络首发日期，不是正式出版日期"。
            hits["date"] = (d[0], d[1], d[2])
            hit_meta["date"] = {"date_kind": d[4]}

    for field in FIELDS:
        if field in hits:
            value, s, e = hits[field]
            sug = {
                "field": field, "current": "", "value": value,
                "source": "rule", "confidence": "high",
                "evidence": build_evidence(pages, s, e),
            }
            _attach_date_kind(sug, hit_meta.get(field, {}).get("date_kind", ""))
            result["suggestions"].append(sug)

    # ---- 第二步：作者（creators）
    #
    # 为什么它不走上面那条 hits 路：hits 的值是"一个字符串 + 一处偏移"，
    # 而作者是**有序多值**，还要逐名拆分。硬塞进 hits 只能塞一个拼接串，
    # 后面每个读 hits 的地方都要再解析一次（多一次出错机会）。
    creators_done = False
    if "creators" in missing:
        cr = extract_creators(pages, title=title)
        if cr:
            result["suggestions"].append(_creator_suggestion(
                cr, build_evidence(pages, cr["start"], cr["end"]),
                source="rule", confidence="high"))
            creators_done = True

    # ---- 第三步：模型兜底（只问规则没搞定的字段）
    still = [f for f in missing if f not in hits]
    if creators_done and "creators" in still:
        still.remove("creators")
    if still and use_model:
        avail, hint = model_available()
        if not avail:
            # 优雅退化：不抛异常，只说明为什么没有模型建议
            result["model"] = {"used": False, "reason": hint or "本地模型不可用"}
            result["notes"].append(
                f"本地模型不可用（{hint or '未启动'}），已退化为纯规则抽取")
        else:
            got, model_meta = _model_suggest(pages, still, model=model)
            result["model"] = {"used": True, "backend": "judge",
                               "fields_asked": still}
            for field in still:
                if field not in got:
                    continue
                raw, ev = got[field]
                value = _clean_value(field, raw)
                if not value:
                    continue
                # 值要和"当前值"比一下：模型可能把已有的值又抄一遍，
                # 但那个字段本来就不在 missing 里，所以这里不用管。
                sug = {
                    "field": field, "current": "", "value": value,
                    "source": "model", "confidence": "low",   # 模型会错，标 low
                    "evidence": _verify_evidence(pages, ev),
                }
                if field == "date":
                    # 类型优先**从它给的证据原文判**，而不是信它自己声明的
                    # `date_kind`：证据是它在正文里指出的那段字，属于"原文级"
                    # 信息；自我归类只是它的说法，小模型在这类标签上并不可靠。
                    # 证据里看不出标签时才退到它声明的类型，最后才是 unknown。
                    kind = kind_from_text(ev) or model_meta.get("date_kind", "") \
                        or DATE_KIND_UNKNOWN
                    _attach_date_kind(sug, kind)
                result["suggestions"].append(sug)
            # 作者由模型抽出时走这里（`got` 里没有它 —— 多值字段见 _model_suggest）。
            # 为什么证据也照"验证过的原文"走：模型说"作者是这几位"时，
            # 用户唯一能核对的就是它指的那行原文；原文定位不到就标 verified=False。
            if "creators" in still and model_meta.get("creators"):
                cr = {"creators": model_meta["creators"],
                      "partial": bool(model_meta.get("creators_partial")),
                      "unsplit": len([c for c in model_meta["creators"]
                                      if not c.get("firstName")])}
                result["suggestions"].append(_creator_suggestion(
                    cr, _verify_evidence(pages, model_meta.get("creators_evidence", "")),
                    source="model", confidence="low"))
    elif still:
        result["model"] = {"used": False, "reason": "use_model=False"}

    # ---- 收尾：没抽到的字段说一声
    got_fields = {s["field"] for s in result["suggestions"]}
    for field in missing:
        if field not in got_fields:
            result["skipped"].append(
                {"field": field, "why": "当前为空，但正文首页里没找到可靠依据"})
    return result


def suggest(item_key: str, model: str = "", use_model: bool = True) -> dict:
    """给一篇文献（按 Zotero key）生成补全建议（**不修改任何数据**）。"""
    key = (item_key or "").strip()
    if not key:
        return {"ok": False, "error": "没有给 key", "key": "", "title": "",
                "suggestions": [], "skipped": []}
    try:
        reader = _get_reader()
        items = reader.load_items()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "key": key, "title": "",
                "error": f"读 Zotero 库失败：{type(exc).__name__}: {exc}",
                "suggestions": [], "skipped": []}
    for it in items:
        if it.key == key:
            return suggest_for_item(it, model=model, use_model=use_model)
    return {"ok": False, "key": key, "title": "",
            "error": f"Zotero 库里没有 key={key} 的条目",
            "suggestions": [], "skipped": []}


# ZoteroReader 每建一次都要开 sqlite 连接，批量扫描时复用同一个实例。
# 为什么做成模块级懒加载而不是全局变量：import 这个模块的代码（比如测试）
# 未必会调用 suggest，不该在 import 时就打开 Zotero 数据库。
_READER = None


def _get_reader():
    global _READER
    if _READER is None:
        _READER = zreader.ZoteroReader()
    return _READER


def close() -> None:
    """释放 Zotero 连接（长驻进程用；CLI 退出时不必显式调）。"""
    global _READER
    if _READER is not None:
        try:
            _READER.close()
        except Exception:  # noqa: BLE001
            pass
        _READER = None


# ---------------------------------------------------------------- 全库扫描


def missing_items(fields: tuple[str, ...] = FIELDS) -> list[dict]:
    """列出全库里缺这些字段的条目（**只读**）。

    数据源是 **Zotero 库**而不是索引库：索引库的 items 表没有 volume /
    issue / pages / date 这几列，用它判断会得出"全都缺"的错结论。
    """
    rows: list[dict] = []
    reader = _get_reader()
    for it in reader.load_items():
        cur = _current_values(it)
        lack = [f for f in fields if not cur[f]]
        if not lack:
            continue
        rows.append({
            "key": it.key,
            "title": it.title,
            "item_type": it.item_type,
            "missing": lack,
            "n_pdfs": len(getattr(it, "pdfs", []) or []),
        })
    # 缺得多的排前面（更值得先补），同数量按 key 稳定排序
    rows.sort(key=lambda r: (-len(r["missing"]), r["key"]))
    return rows


def scan(fields: tuple[str, ...] = FIELDS) -> dict:
    """扫描全库，返回缺字段统计 + 明细（**只读**）。"""
    try:
        rows = missing_items(fields)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"扫描失败：{type(exc).__name__}: {exc}",
                "rows": [], "per_field": {}, "total": 0, "missing_total": 0}
    per_field = Counter()
    for r in rows:
        for f in r["missing"]:
            per_field[f] += 1
    try:
        total = len(_get_reader().load_items())
    except Exception:  # noqa: BLE001
        total = 0
    return {"ok": True, "rows": rows, "per_field": dict(per_field),
            "total": total, "missing_total": len(rows), "fields": list(fields)}


# ---------------------------------------------------------------- CLI 输出

def _fmt_suggestion(s: dict) -> str:
    ev = s.get("evidence") or {}
    page = ev.get("page") or 0
    mark = {"high": "高", "low": "低"}.get(s.get("confidence"), "?")
    src = {"rule": "规则", "model": "模型"}.get(s.get("source"), "?")
    lines = [f"  {s['field']:8} {s['value']!r}"
             f"    [{src} / {mark}]"
             + (f"   来源 p.{page}" if page else "   来源：模型未给出处")]
    # 日期类型单独一行：`2025-08-21` 这种值本身看不出是首发还是正式出版，
    # 抄进 Zotero 之前必须让用户看见（见 DATE_KIND_LABELS 的说明）。
    if s.get("field") == "date" and s.get("date_kind"):
        lines.append(f"           日期类型：{s.get('date_kind_label') or s['date_kind']}")
    # 作者的名单要逐位列出来：`value` 那一行是给人快速看的，
    # 但用户真正要核对的是"姓和名有没有被拆对"，所以把 values 也打出来。
    if s.get("field") == "creators":
        for i, c in enumerate(s.get("values") or [], 1):
            lines.append(f"           {i}. 姓={c.get('lastName', '')!r}"
                         f"  名={c.get('firstName', '')!r}")
        if s.get("partial"):
            lines.append(f"           ⚠ {s.get('warning', '')}")
    if ev.get("text"):
        lines.append(f"           原文：{ev['text']}")
    if ev.get("verified") is False and s.get("source") == "model":
        lines.append("           ⚠ 这段原文没能在正文里定位到，请自行核对 PDF")
    return "\n".join(lines)


def cmd_suggest(key: str, model: str, use_model: bool, as_json: bool) -> int:
    res = suggest(key, model=model, use_model=use_model)
    if as_json:
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        print(f"[XX] {res.get('error')}")
        return 1
    print("=" * 66)
    print(f"{res['key']}  {res.get('title') or '(无标题)'}")
    print(f"类型：{res.get('item_type') or '?'}"
          f"　正文：{res.get('fulltext_pages', 0)} 页"
          f"（来源 {res.get('fulltext_source') or '无'}）")
    print("=" * 66)
    if res["suggestions"]:
        print(f"\n建议 {len(res['suggestions'])} 条（值 + 原文证据，人工核对后再决定用不用）：")
        for s in res["suggestions"]:
            print(_fmt_suggestion(s))
    else:
        print("\n没有可补的字段")
    if res.get("skipped"):
        print("\n跳过 / 没找到：")
        for s in res["skipped"]:
            print(f"  {s['field']:8} {s['why']}")
    if res.get("model"):
        m = res["model"]
        print(f"\n模型：{'用了' if m.get('used') else '没用'}"
              + (f"（{m.get('reason', '')}）" if not m.get("used") else ""))
    for n in res.get("notes") or []:
        print(f"  注：{n}")
    print("\n⚠ 这只是建议，本模块不写 Zotero、也不写知识库。")
    return 0


def cmd_scan(fields: tuple[str, ...], show: int, as_json: bool) -> int:
    data = scan(fields)
    if not data.get("ok"):
        print(f"[XX] {data.get('error')}")
        return 1
    if as_json:
        import json
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    rows = data["rows"]
    print("=" * 66)
    print(f"全库共 {data['total']} 篇，其中 {data['missing_total']} 篇缺以下字段"
          f"（字段范围：{'、'.join(fields)}）")
    print("=" * 66)
    print("\n各字段缺失篇数：")
    for f in fields:
        print(f"  {f:8} {data['per_field'].get(f, 0):>4} 篇")
    if not rows:
        print("\n没有缺字段的文献。")
        return 0
    print(f"\n缺失明细（按缺得多的排前，最多列 {show} 条）：")
    for r in rows[:show]:
        print(f"  {r['key']}  缺 {len(r['missing'])}：{'、'.join(r['missing'])}"
              f"   PDF {r['n_pdfs']}")
        print(f"      《{r['title'] or '(无标题)'}》")
    if len(rows) > show:
        print(f"  …另有 {len(rows) - show} 条（--show N 可多列）")
    print("\n看某一篇的具体建议：python offline/metafill.py <KEY>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从 PDF 正文里补全缺失的元数据字段（只给建议，不写回）")
    ap.add_argument("key", nargs="?", default="", help="Zotero 条目 key")
    ap.add_argument("--scan", action="store_true", help="扫描全库缺字段情况")
    ap.add_argument("--field", default="", help="只处理某个字段（仅 --scan 用）")
    ap.add_argument("--no-model", action="store_true", help="只跑规则，不调本地模型")
    ap.add_argument("--model", default="", help="指定模型名（默认由 judge 配置决定）")
    ap.add_argument("--show", type=int, default=25, help="--scan 明细列几条")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="输出结构化 JSON（供上层调用/接线用）")
    args = ap.parse_args()

    fields = FIELDS
    if args.field:
        want = [f.strip() for f in re.split(r"[,\s]+", args.field) if f.strip()]
        bad = [f for f in want if f not in FIELDS]
        if bad:
            print(f"[XX] 不支持的字段：{'、'.join(bad)}"
                  f"（只能在这些里选：{'、'.join(FIELDS)}）")
            return 2
        fields = tuple(want)

    try:
        if args.scan:
            return cmd_scan(fields, args.show, args.as_json)
        if not args.key:
            ap.print_help()
            return 2
        return cmd_suggest(args.key, args.model, not args.no_model, args.as_json)
    finally:
        close()


if __name__ == "__main__":
    sys.exit(main())
