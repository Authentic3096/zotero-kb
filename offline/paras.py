"""把 `fulltext/<key>.md` 切成**可检查的段落**，并支持用户/模型修正边界。

## 为什么需要专门一个模块（本机实测的结论）

正文 md 里**没有段落边界**。我逐篇统计过全库 95 篇 / 1439 页：

    · 页内完全没有空行的 10 篇；"有空行"的 85 篇 —— 但那些空行几乎都只是
      图注/表格块前的那一个，正文段落之间**没有**空行。
    · 行首**没有缩进**（提取时被 clean_text 抹平），所以中文论文惯用的
      "首行缩进两字"这条最强信号也不可用。
    · 我本来指望 PyMuPDF 的 block 边界 == 段落，**也不是**：
      `get_text("dict")` 在这些 PDF 上把**每一行**都当独立 block
      （实测：88 行 → 90 个 block；一个 4 行段落 → 4 个 block）。

结论：段落边界只能**重建**，而且不可能 100% 正确。所以本模块的定位不是
"给出正确答案"，而是：

  1. **只在有证据的地方切**，并把"为什么在这里切"记进 `evidence`（可核对，
     不是黑箱 —— 这是本项目对模型输出的统一纪律）；
  2. 允许**用户/模型修正**（`para_override` 表：join / split 两个方向）；
  3. 段落太长时按句末标点切成**检查单元**（`units()`）：段落身份不变，
     只是控制每次送进模型的上下文长度。

## 两级粒度（为什么不是一级）

    段落 para   —— 语义单位。要能"这一段和上一段其实是一段"（join）、
                   "这一段其实是两段粘在一起"（split）。进度按它记。
    检查单元 unit —— 调用模型的粒度。超长段落按句末标点切成 ≤900 字的块，
                  避免一段 3000 字把上下文撑爆。进度里记"做到第几个 unit"。

## 规则（按证据强度排序）

    blank      页内空行（有的页是真空行，直接就是边界，最可信）
    空行后/页首 天然边界
    标题         `2 航空超导全张量磁梯度探测系统` / `3.1 静态试验` /
                `第 3 章` / `摘要` / `参考文献`（独立成段，kind=heading）
    图注表注     `图 2 …` / `Fig. 3 …` / `表1 …`（独立成段，kind=caption）
    图表块      md 里已有的 `**本页图注**` / `**本页表格 N**` 块（kind=figure）
    表格行      以 `|` 开头的行（kind=table）
    段尾短行    上一行**明显短于中位行长**且以句末标点结尾 → 那是一个段落的
                收尾，下一行起新段。这是正文页最主要的边界证据。
    页眉残渣    页首 1~2 行命中页眉模式（`·1092·` / `第34 卷` / `Vol.` /
                `Proceedings of`）→ 独立成块 kind=header，不并进正文。
    英文断词    上一行以 `-` 结尾、下一行以小写开头 → **同一段**，并去掉连字符。

⚠ 宁可"少切"（把两段粘成一段）也不要"多切"：多切出来的碎段会让模型提一堆
  没意义的 join，而少切的那一段在界面上**一眼就能看出来**（一段 2000 字），
  用 split 修一次就够。所以默认规则是"没有证据就不切"。

## hash 为什么用内容而不是段号

段号会漂：正文重建、页眉过滤、补丁应用都会改变段数。而 `hash = sha1(段首
200 字 + 段长)[:6]` 只要这段文字没变就不变 —— 进度（`para_check`）与修正
（`fulltext_patch`）都靠它对齐。这也是"打标记"能跨重建活下来的原因。
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field

import schemas as S

# ---------------------------------------------------------------- 行分类

# 章节标题（中文）：`2 xxx` / `3.1 xxx` / `第 3 章 xxx` / `摘 要`
HEADING_CN = re.compile(
    r"^(?:[0-9]{1,2}(?:\.[0-9]{1,2}){0,3}\s*[\u4e00-\u9fff]"
    r"|第\s*[0-9一二三四五六七八九十]+\s*[章节篇]"
    r"|摘\s*要|关\s*键\s*词|参考文献|致\s*谢|结\s*论|引\s*言|目\s*录)"
)
# 章节标题（英文）：`3.1 Introduction` / `Abstract` / `References`
HEADING_EN = re.compile(
    r"^(?:[0-9]{1,2}(?:\.[0-9]{1,2}){0,3}\s+[A-Z]"
    r"|Abstract|Keywords|References|Introduction|Conclusion|Acknowledg)",
)
# 图注/表注：以图号开头（中英文）
CAPTION = re.compile(r"^\s*(?:图|表)\s*[0-9]|^\s*(?:Fig\.?|Figure|Table|Tab\.?)\s*[0-9]",
                     re.I)
# 页眉残渣：页码装饰、卷期、"Proceedings of"、DOI
HEADERISH = re.compile(
    r"·\s*[0-9]{1,5}\s*·|第\s*[0-9]{1,3}\s*卷|Vol\.?\s*[0-9]|Proceedings of"
    r"|ISSN|DOI[：:]|文章编号|收稿日期",
    re.I)
# 句末标点（硬）与句中标点
# ⚠ 第一版漏了英文句点 `.` —— 结果**英文论文几乎不切段**（EBMFCYGT 一篇就
#   粘出 39 段超长、最长 9004 字）。中文的 `。` 和英文的 `.` 都要在。
SENT_END = re.compile(r"[。．！？；!?;.…][\"”’)\]]*$")
CLAUSE_END = re.compile(r"[，,、：:][\"”’)\]]*$")
# 项目符号开头
BULLET = re.compile(r"^(?:[-–—•·]|\([0-9]+\)|（[0-9]+）|[①-⑩])\s*")
# md 里已有的块标记
FIGBLOCK = re.compile(r"^\*\*本页(?:图注|表格)")
TABLE_ROW = re.compile(r"^\s*\|")
NO_TEXT = re.compile(r"^\*\(本页无可提取文字\)\*$")
# 单独成行的公式编号（`(1)` / `（12）`）
FORMULA_ONLY = re.compile(r"^[（(]\s*[0-9]{1,3}\s*[)）]$")
# 我们自己插入的标记行（`<!-- kb-fix:8f3a2b -->` / `<!-- kb-join:… -->`）。
# ⚠ 必须认出来：否则它会被当正文并进上一段，AI 读全时也会把工程注释当论文内容。
MARK_LINE = re.compile(r"^\s*<!--\s*kb-")
# 参考文献条目：`[157] D. Mohanadas, …`
REFS_LINE = re.compile(r"^\s*\[\s*[0-9]{1,3}\s*\]")
# 目录行：`1.1 研究背景 ………… 12`
TOC_LINE = re.compile(r"[.·]{4,}\s*[0-9]{1,4}\s*$")
# 参考文献区块的起始标题
REFS_HEADING = re.compile(r"^\s*(?:参\s*考\s*文\s*献|References|REFERENCES|Bibliography)\s*$")
# "一段就是一行"的门槛：超过它就别再跟邻行粘 —— 它自己已经是一段
# （Zotero 的 ft-cache 就是按段落给 `\n` 的，实测本库有整行 6000 字的参考文献）
LONG_LINE = 100
# 超过它的行**先按句子重新分组**再进主循环。
# 为什么要这步：英文那几篇是"块形态"（一行 = 400~1500 字的整块，块边界随意断），
# 实测最长一段 9004 字就是这么粘出来的。块内部的句末标点是唯一可用的切点。
PRE_SPLIT = 200
# 重新分组的目标长度：一段几百字才好导航（也正好落在中文学术段落的量级上）
GROUP_TARGET = 600

# 句末切分。⚠ 英文的句点必须用 **lookbehind**（`(?<=\.)`）而不是 `\.`：
#   `re.split` 会把**匹配到的分隔符删掉**，用消费式的 `\.` 就等于把每个句子
#   末尾的句号吃掉了 —— 于是切出来的每"句"都不以标点结尾，后面的分句逻辑
#   全部失效（实测：823 字的行切出 6 句，句句没句号）。这种"自己制造半句话"
#   正是本项目要修的那类损伤，不能自己来一遍。
#   小数（`93.46`）不用单独排除：后面要求 `\s+[A-Z(0-9]`，小数点后是数字，不会命中。
_ABBR = (r"(?<!\bet al\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bFig\.)(?<!\bEq\.)"
         r"(?<!\bRef\.)(?<!\bvs\.)(?<!\betc\.)(?<!\bNo\.)(?<!\bcf\.)")
SENT_SPLIT = re.compile(
    r"(?<=[。．！？；])"
    r"|(?<=\.)" + _ABBR + r"(?=\s+[A-Z(\[0-9])"
)


def split_sentences(text: str) -> list[str]:
    """把一段文字按句子切开（中英混排都认）。

    ⚠ **不要 strip 每一句**：英文句间的空格就在切点处，strip 掉再拼回去会
      写出 `window.However` 这种损伤 —— 我们自己不能制造"提取损伤"。
      句子之间的原始间距要原样保留，只在最外层（成组落地时）strip。
    """
    return [p for p in SENT_SPLIT.split(text) if p and p.strip()]


def sentence_groups(text: str, target: int = GROUP_TARGET) -> list[str]:
    """把长行按句子贪心分组，每组 ≤ target 字（尽量不把句子切断）。

    这是"块形态"页面唯一能做的事：块边界是随意断的，段落边界已经不在文本
    里了；能恢复的最细粒度就是"句群"。

    ⚠ 拼接时**不加空格也不删空格**：句子之间的间距原样保留（见
      `split_sentences` 的说明）。只在成组落地时 strip 外边缘。
    """
    out: list[str] = []
    buf = ""
    for s in split_sentences(text):
        if buf and len(buf) + len(s) > target:
            out.append(buf.strip())
            buf = s
        else:
            buf += s
    if buf.strip():
        out.append(buf.strip())
    return out


def expand_lines(lines: list[str]) -> list[str]:
    """把过长的行先按句群拆开（短行原样返回）。"""
    out: list[str] = []
    for line in lines:
        if len(line.strip()) >= PRE_SPLIT:
            out.extend(sentence_groups(line.strip()))
        else:
            out.append(line)
    return out


def para_hash(text: str) -> str:
    """段落指纹：段首 200 字 + 段长。文字没变 → 指纹不变（跨重建稳定）。"""
    key = (text[:200] + "|" + str(len(text))).encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:6]


@dataclass
class Para:
    """一个重建出来的段落。"""

    page: int                 # 1 基页码
    index: int                # 页内序号（0 基）
    logical_index: int        # 全文序号（0 基）
    text: str
    kind: str = "prose"       # prose|heading|caption|figure|table|header|block
    evidence: str = ""        # 它为什么成为新段（可核对）
    joined_from: list = field(default_factory=list)   # 由哪些页内段合并而来
    overridden: str = ""      # 被哪条 para_override 修正过（join/split）

    @property
    def hash(self) -> str:
        return para_hash(self.text)

    @property
    def anchor(self) -> str:
        return self.text[:40]

    @property
    def n_chars(self) -> int:
        return len(self.text)


def _classify(line: str, *, first_of_page: bool, in_refs: bool = False,
              page: int = 0, line_no: int = 99) -> str:
    s = line.strip()
    if not s:
        return "blank"
    if NO_TEXT.match(s):
        return "block"
    if MARK_LINE.match(s):
        return "mark"
    if FIGBLOCK.match(s):
        return "figure"
    # 单独成行的公式编号/公式碎片（`(1)`、`(12)`）——不当正文。
    # 这一条同时给"公式转 LaTeX"那条线留了位置（见工作档案待办）。
    if FORMULA_ONLY.match(s):
        return "formula"
    if TABLE_ROW.match(s):
        return "table"
    if REFS_HEADING.match(s):
        return "heading"
    # 参考文献区：标题之后的所有行都算 refs（不然一条条都会被当正文，
    # 实测最长的"段落"就是这么来的 —— 参考文献被粘成 11872 字）
    if in_refs or REFS_LINE.match(s):
        return "refs"
    if TOC_LINE.search(s):
        return "toc"
    if first_of_page and HEADERISH.search(s):
        return "header"
    # 第 1 页开头几行的"短行、无任何标点"—— 这是标题/作者/单位块
    # （实测：`高精度航空超导全张量磁梯度探测技术` + 作者 + 单位三行会被
    #  粘成一段 64 字的"正文"，看着很别扭）。只限第 1 页前 3 行，避免把
    #  正文页首那句"续上一页"的残句误判成标题。
    if page == 1 and line_no <= 2 and len(s) <= 60 \
            and not SENT_END.search(s) and not CLAUSE_END.search(s):
        return "heading"
    if HEADING_CN.match(s) or HEADING_EN.match(s):
        # `1.1.2 透射电子显微镜(TEM)` 这种也算；但正文里"1997 年基于…"不会命中
        # （要求数字后紧跟中文且整行不长，否则一句正文也可能以数字开头）
        if len(s) <= 60 and not SENT_END.search(s):
            return "heading"
    if CAPTION.match(s):
        return "caption"
    return "prose"


# 独立成块的 kind（不参与"行→段"的粘连判断）
_BLOCKY = ("heading", "caption", "figure", "table", "block", "header",
           "refs", "toc", "formula", "mark")


def split_page(lines: list[str], page: int, start_index: int = 0,
               *, full_pct: int = 75, gap_ratio: float = 0.8) -> list[Para]:
    """把一页的行重建为段落。纯函数，便于测试与复用。

    ## 两种形态都要认（这是第一版最大的错）

    本库的正文来自两个地方，行的粒度**不一样**：
      · PyMuPDF/`ft-cache` 的**折行**形态：一行 = PDF 的一行（20~60 字），
        段落要靠"上一行短且以句末标点收尾"才认得出边界；
      · **一段就是一行**形态（ft-cache 按段落给 `\\n`）：一行可能就是 300 字，
        甚至整页参考文献合成一行 6000 字。
    第一版只按折行规则办事（用中位行长当"整行宽度"），于是"一段就是一行"
    的那些页面把整页粘成一段 —— 全库粘出 532 段超长、最长 11872 字。

    所以现在的判据是**逐行**的，不设全局模式：
      (A) `prev` 以句末标点结尾 且 `prev` 短于整行宽度 → 折行形态的段尾
      (B) `prev` 以句末标点结尾 且 `prev` 长度 ≥ `LONG_LINE` → 一段就是一行
      (C) 本行自己像新块开头（标题/图注/表格/参考文献/目录/项目符号）

    ## 阈值是量出来的

      · `full_pct`：用第 75 百分位的行长当"整行宽度"。**不能用中位数** ——
        页面上有段落尾行、标题、图注、表格行，中位数会被短行拉低，于是尾行
        看起来"不短"，段落又粘起来了。
      · `gap_ratio`：尾行短于整行宽度的这个比例才算段尾。中文两端对齐排版里
        只有**段落最后一行**会明显不满；行尾是 `。` 但接近满行的是**行内断句**，
        不能切（切了就是"误拆"，正是我们要修的那种毛病）。
    """
    out: list[Para] = []
    # 先把"块形态"的长行按句群拆开（短行不受影响）—— 否则整块会被当成一段，
    # 实测就是这么粘出 9004 字的。
    lines = expand_lines(lines)
    body_lens = [len(l.strip()) for l in lines if l.strip()]
    if body_lens:
        srt = sorted(body_lens)
        full = srt[min(len(srt) - 1, int(len(srt) * full_pct / 100))]
        short_line = max(8, int(full * gap_ratio))
    else:
        full, short_line = 0, 0

    # 形态判定：`full`（整行宽度）特别大 → 这一页是**一段就是一行**（来源按段落
    # 给了 `\n`）。此时"行尾是句末标点"就足以断段，不必再看长度。
    # 门槛取 150：英文两端对齐的**折行**最多不过 ~110 字符，中文折行 20~60；
    # 超过 150 的行只可能是"一段一行"那种块。
    loose = full >= 150

    cur: list[str] = []          # 当前段落的行
    cur_ev = ""                  # **这一段为什么从这里开始**（可核对的证据）
    pending_ev = ""              # 刚断过，下一段开始时要记的理由
    in_refs = False
    in_figure = False            # 是否在 `**本页图注**` / `**本页表格 N**` 块里

    def flush() -> None:
        """收束当前段落。evidence 记的是"它为什么从这里开始"。

        ⚠ 不碰 `pending_ev`：那是给**下一个**真正落地的段落/块用的。
          第一版在这里把 pending_ev 吃掉了，于是"空行之后的图注块"把
          "页内空行"这条证据吞了，后面那段正文只剩"首段"。
        """
        nonlocal cur, cur_ev
        text = "\n".join(cur).strip()
        cur = []
        if text:
            out.append(Para(page=page, index=start_index + len(out),
                            logical_index=0, text=text, evidence=cur_ev))
        cur_ev = ""

    def emit(text: str, kind: str, ev: str) -> None:
        out.append(Para(page=page, index=start_index + len(out),
                        logical_index=0, text=text, kind=kind, evidence=ev))

    for i, raw in enumerate(lines):
        line = raw.strip()
        kind = _classify(line, first_of_page=(i <= 1), in_refs=in_refs,
                         page=page, line_no=i)
        if kind == "heading" and REFS_HEADING.match(line):
            in_refs = True
        # ⚠ 图注/表格块要**延续**：`**本页图注**` 后面那些 `- 图：…` 行本身
        #   不匹配图注正则（以 `- ` 开头），第一版就漏成了"正文" ——
        #   于是预筛里 35% 的可疑段有一大半是图注（实测），完全是自找的噪声。
        if in_figure and kind in ("prose", "caption"):
            kind = "figure"
        if kind == "figure":
            in_figure = True
        elif kind in ("heading", "blank"):
            in_figure = False

        if kind == "blank":
            flush()
            pending_ev = "页内空行"
            continue
        if kind in _BLOCKY:
            flush()
            emit(line, kind, pending_ev or f"{kind} 行")
            pending_ev = ""
            continue

        # ---- 普通正文行
        if not cur:
            cur = [line]
            cur_ev = pending_ev or ("页首" if i == 0 else "首段")
            pending_ev = ""
            continue

        prev = cur[-1]
        cont = True                      # 默认**继续**（宁可少切）
        why = ""
        if prev.endswith("-") and line[:1].islower():
            # 英文断词跨行：接上并去掉连字符
            cur[-1] = prev[:-1] + line
            continue
        if SENT_END.search(prev):
            if loose:
                # 一段一行形态：行尾是句末标点就是段尾
                cont = False
                why = "整行（≥%d 字）以句末标点结尾" % full
            elif short_line and len(prev) <= short_line:
                cont = False
                why = "上一行短且以句末标点结尾"          # (A) 折行形态段尾
            elif len(prev) >= LONG_LINE:
                cont = False
                why = "上一行是整段（≥%d 字）" % LONG_LINE  # (B) 一段就是一行
        if cont and BULLET.match(line):
            cont = False
            why = "项目符号开头"                            # (C)
        if cont:
            cur.append(line)
        else:
            flush()
            cur = [line]
            cur_ev = why

    flush()
    for k, p in enumerate(out):
        p.logical_index = k
    return out


def parse(text: str) -> list[Para]:
    """解析整篇 fulltext md → 段落列表（未应用 override）。"""
    # 按 `## p.N` 切页；第一段是标题区（`# 标题` + `> 元信息`），不是正文
    parts = re.split(r"^## p\.([0-9]+)\s*$", text, flags=re.M)
    paras: list[Para] = []
    # parts = [前言, 页号1, 正文1, 页号2, 正文2, ...]
    for i in range(1, len(parts) - 1, 2):
        pno = int(parts[i])
        body = parts[i + 1]
        lines = body.split("\n")
        # 去掉块首尾的空行，但**页内空行要保留**（它是边界证据）
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        page_paras = split_page(lines, page=pno, start_index=0)
        paras.extend(page_paras)
    for k, p in enumerate(paras):
        p.logical_index = k
    return paras


# ---------------------------------------------------------------- 检查单元


def units(para: Para, max_chars: int = 900) -> list[str]:
    """把段落切成**检查单元**（调用模型的粒度）。

    只有超长段落才会被切，切点落在句子边界（复用 `sentence_groups`），
    避免把一句话切断 —— 切断会让模型看到半句话，正是我们想让模型帮我们
    修的那类损伤，自己制造一遍就没意义了。
    """
    text = para.text
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    return sentence_groups(text, target=max_chars)


# ---------------------------------------------------------------- 修正（join/split）


def apply_overrides(paras: list[Para], overrides: list[dict]) -> list[Para]:
    """按 `para_override` 修正段落边界。

    override 形如：`{"kind": "join_prev"|"split_at", "p_hash": "…", "at": 位置}`
      · `join_prev` 挂在这一段的 hash 上：把它**并入上一段**
      · `split_at` 挂在这一段的 hash 上：在 `at`（字符偏移）处把它切成两段
    ⚠ 对齐用 **hash**（内容指纹）而不是段号：正文重建后段号会漂。
    ⚠ **纯函数**：入参的 Para 不会被改动（内部先复制）。第一版是原地改的，
      调用方拿着同一批对象再比对时就会看到"文本已经被切过"的错觉。
    """
    paras = [_clone(p) for p in paras]
    if not overrides:
        return _renumber(paras)

    by_hash: dict[str, list[dict]] = {}
    for ov in overrides:
        by_hash.setdefault(str(ov.get("p_hash") or ""), []).append(ov)

    out: list[Para] = []
    for p in paras:
        ovs = by_hash.get(p.hash, [])
        joins = [o for o in ovs if o.get("kind") == "join_prev"]
        splits = [o for o in ovs if o.get("kind") == "split_at"]

        if joins and out:
            prev = out[-1]
            prev.text = prev.text.rstrip() + "\n" + p.text.strip()
            prev.joined_from = list(prev.joined_from) + [p.index] + list(p.joined_from)
            prev.overridden = "join_prev"
            # 合并后可能还要在合并结果上切
            target = prev
        else:
            target = p
            out.append(target)

        for sp in splits:
            try:
                at = int(sp.get("at") or 0)
            except (TypeError, ValueError):
                continue
            t = target.text
            if 0 < at < len(t):
                first, second = t[:at].strip(), t[at:].strip()
                if first and second:
                    target.text = first
                    target.overridden = "split_at"
                    idx = out.index(target) + 1
                    out.insert(idx, Para(
                        page=target.page, index=target.index,
                        logical_index=0, text=second,
                        kind=target.kind, evidence="用户/模型切分",
                        overridden="split_at"))
    return _renumber(out)


def _renumber(paras: list[Para]) -> list[Para]:
    for k, p in enumerate(paras):
        p.logical_index = k
    return paras


def _clone(p: Para) -> Para:
    """复制一个 Para（含 joined_from 列表），让 apply_overrides 保持纯函数。"""
    return Para(page=p.page, index=p.index, logical_index=p.logical_index,
                text=p.text, kind=p.kind, evidence=p.evidence,
                joined_from=list(p.joined_from), overridden=p.overridden)


# ---------------------------------------------------------------- 落库读写


def load_overrides(conn, key: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT kind, p_hash, at, anchor, reason, source, created_at "
            "FROM para_override WHERE item_key = ? ORDER BY override_id", (key,))
        return [dict(r) for r in rows]
    except Exception:                                    # noqa: BLE001
        return []          # 老库还没这张表


def add_override(conn, key: str, kind: str, p_hash: str, *,
                 at: int = 0, anchor: str = "", reason: str = "",
                 source: str = "user") -> int:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO para_override(item_key, kind, p_hash, at, anchor, reason,"
        " source, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (key, kind, p_hash, int(at), anchor, reason, source, now))
    conn.commit()
    return 0


def remove_override(conn, key: str, kind: str, p_hash: str) -> int:
    cur = conn.execute(
        "DELETE FROM para_override WHERE item_key=? AND kind=? AND p_hash=?",
        (key, kind, p_hash))
    conn.commit()
    return cur.rowcount


def load_paras(conn, key: str) -> list[Para]:
    """读某篇的段落（已应用修正）。找不到文件时返回空列表。"""
    import os
    path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return apply_overrides(parse(fh.read()), load_overrides(conn, key))


# ---------------------------------------------------------------- 自检/统计


def summary(paras: list[Para]) -> dict:
    """给界面与自检用的段落统计（也是判断重建质量是否可接受的基线）。

    `too_long` 是**重建质量的客观量化**：>1200 字的正文段几乎一定是多段被
    粘在一起（中文学术段落一般 200~600 字）。第一版全库 532 段，改造后
    应当大幅下降 —— 剩下的那些就是真要交给模型/用户 join/split 的。
    """
    prose = [p for p in paras if p.kind == "prose"]
    lens = [p.n_chars for p in prose] or [0]
    long_ones = [p for p in prose if p.n_chars > 1200]
    return {
        "total": len(paras),
        "prose": len(prose),
        "heading": sum(1 for p in paras if p.kind == "heading"),
        "caption": sum(1 for p in paras if p.kind == "caption"),
        "figure": sum(1 for p in paras if p.kind == "figure"),
        "table": sum(1 for p in paras if p.kind == "table"),
        "header": sum(1 for p in paras if p.kind == "header"),
        "refs": sum(1 for p in paras if p.kind == "refs"),
        "toc": sum(1 for p in paras if p.kind == "toc"),
        "prose_chars": sum(lens),
        "prose_median": int(statistics.median(lens)) if lens else 0,
        "prose_max": max(lens) if lens else 0,
        "too_long": len(long_ones),      # > 1200 字：多半是多段被粘在一起
    }


def main(argv: list[str] | None = None) -> int:
    """`python offline/paras.py <KEY> [--dump N]`：看看某篇切出来什么样。"""
    import argparse
    import os
    ap = argparse.ArgumentParser(description="段落重建自检")
    ap.add_argument("key", nargs="?", default="", help="Zotero 条目 key")
    ap.add_argument("--dump", type=int, default=0, help="打印前 N 段全文")
    ap.add_argument("--all", action="store_true", help="跑全库统计")
    args = ap.parse_args(argv)

    conn = S.connect(S.INDEX_DB)
    if args.all or not args.key:
        keys = [r["key"] for r in conn.execute("SELECT key FROM items ORDER BY key")]
        agg = {"total": 0, "prose": 0, "too_long": 0}
        worst = []
        for k in keys:
            paras = load_paras(conn, k)
            if not paras:
                continue
            st = summary(paras)
            for f in agg:
                agg[f] += st[f]
            if st["too_long"]:
                worst.append((st["too_long"], k, st["prose"], st["prose_max"]))
        print(f"  全库 {len(keys)} 篇：段落 {agg['total']}（正文 {agg['prose']}），"
              f"其中超长（>1200 字，疑似多段粘连）{agg['too_long']} 段")
        worst.sort(reverse=True)
        for n, k, prose, mx in worst[:10]:
            print(f"    {k}  超长 {n} 段 / 共 {prose} 段，最长 {mx} 字")
        conn.close()
        return 0

    paras = load_paras(conn, args.key)
    if not paras:
        print(f"  没有 {args.key} 的 fulltext md（或库为空）")
        conn.close()
        return 1
    st = summary(paras)
    print(f"  {args.key}：段 {st['total']}（正文 {st['prose']}）"
          f"  标题 {st['heading']} 图注 {st['caption']} 图表块 {st['figure']}"
          f" 表格 {st['table']} 页眉 {st['header']}")
    print(f"  正文段长：中位 {st['prose_median']} 字 / 最长 {st['prose_max']} 字"
          f" / 超长 {st['too_long']} 段")
    for p in paras[: (args.dump or 0)]:
        print(f"\n  [{p.logical_index}] p.{p.page} {p.kind} "
              f"hash={p.hash} 证据={p.evidence}")
        print("    " + p.text.replace("\n", "\n    ")[:600])
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
