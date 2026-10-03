"""查询解析与改写。

一个关键事实：SQLite FTS5 的 unicode61 分词器把连续中文当成**一个 token**，
所以查询「偶极子」永远匹配不到正文里的「机器学习模型模型」。
解决办法是把中文查询拆成单字、用 AND 连接 —— 这与 Zotero 自己建索引的方式一致
（它的 fulltextWords 表里同样是单字），实测「偶极」「分类器」这类两字词都能召回。
"""

from __future__ import annotations

import re

# 全角转半角 + 常见数学符号统一，避免"用户打全角、正文是半角"这种假阴性
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ（）－",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz()-",
)

# FTS5 里对中文有用的最小单位：CJK 统一表意文字
CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def normalize(text: str) -> str:
    """全角转半角、压空白。"""
    return re.sub(r"\s+", " ", (text or "").translate(_FULLWIDTH)).strip()


def tokenize(query: str) -> list[tuple[str, str]]:
    """把查询拆成 (类型, 词元) 列表。类型：cjk / word / quoted。

    引号里的整段保持不动 —— 用户写 "多神经网络" 是想精确匹配。
    """
    query = normalize(query)
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch in '"\u201c\u201d':
            # 找到配对的引号
            closing = '"' if ch == '"' else "\u201d"
            end = query.find(closing, i + 1)
            if end == -1:
                end = n
            phrase = query[i + 1:end].strip()
            if phrase:
                tokens.append(("quoted", phrase))
            i = end + 1
            continue
        if re.match(f"[{CJK}]", ch):
            j = i
            while j < n and re.match(f"[{CJK}]", query[j]):
                j += 1
            # 连续中文按**单字**展开（FTS5 里它们各自是独立 token）
            for k in range(i, j):
                tokens.append(("cjk", query[k]))
            i = j
            continue
        if ch.isalnum() or ch in "-_.":
            j = i
            while j < n and (query[j].isalnum() or query[j] in "-_."):
                j += 1
            tokens.append(("word", query[i:j]))
            i = j
            continue
        i += 1
    return tokens


def _quote(token: str) -> str:
    """把词元包成 FTS5 字符串字面量。

    纯 ASCII 标识符（英文单词、数字）不需要引号，且加引号会显得啰嗦 ——
    但 CJK 单字**必须**加引号，否则 FTS5 的语法解析会把它们当操作符。
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return token
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", token):
        return token
    return '"' + token.replace('"', '""') + '"'


def match_terms(query: str) -> list[str]:
    """把查询拆成**可以单独去 FTS5 匹配**的最小单位。

    中文一个字算一个单位（因为 FTS5 里它们本来就是独立 token），
    英文一个单词算一个单位，引号短语拆成其中的 CJK 单字。
    检索层用这些单位做 OR 召回 + 覆盖率打分，而不是硬 AND ——
    「机器学习模型」这种长中文查询若要求每个字都出现，
    会把大量只讲了「神经网络」的好文献挡在门外。
    """
    out: list[str] = []
    for kind, text in tokenize(query):
        if kind == "quoted":
            for ch in text:
                if re.match(f"[{CJK}]", ch) and ch not in out:
                    out.append(ch)
            if not any(re.match(f"[{CJK}]", c) for c in text) and text not in out:
                out.append(text)
        elif text not in out:
            out.append(text)
    return out


def build_match(query: str, mode: str = "and") -> str:
    """生成 FTS5 的 MATCH 表达式。

    mode='and'：所有词元都要出现（精确，用于"必须都命中"的场景）
    mode='or' ：任一词元出现即可（默认检索走这条路，靠覆盖率打分排序）
    """
    tokens = tokenize(query)
    if not tokens:
        return ""
    parts: list[str] = []
    for kind, text in tokens:
        if kind == "quoted":
            # 引号短语：内部若是中文，按字拆开（FTS5 的短语匹配对中文无效）
            sub = [c for c in text if re.match(f"[{CJK}]", c)]
            if sub:
                parts.append("(" + " AND ".join(_quote(c) for c in sub) + ")")
            else:
                parts.append(_quote(text))
        else:
            parts.append(_quote(text))
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join(parts)


def cjk_rewrite(query: str) -> str:
    """给人看的改写结果，用于测试与排障。"""
    return build_match(query)


def build_match_phrases(query: str) -> str:
    """召回表达式：把每个 CJK 连续段当作**短语**（相邻）来查。

    「机器学习模型」→ `"机器学习模型"`，只在正文里真的连续写出这些字时才命中。
    FTS5 把中文切成单字 token，短语查询即"这些单字相邻出现"，正是要的语义。
    英文词直接列入（OR）。
    """
    parts: list[str] = []
    for seg in re.findall(f"[{CJK}]+", normalize(query)):
        parts.append(_quote(seg))
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{0,30}", normalize(query)):
        parts.append(word)
    return " OR ".join(parts)


def cjk_ngrams(seg: str, max_len: int = 4) -> list[str]:
    """一个 CJK 段的 1..max_len 长度子串，长优先。

    用于覆盖率打分：块里命中「反演」（连续两个字）比只命中「反」更相关。
    """
    out: list[str] = []
    n = len(seg)
    for size in range(min(max_len, n), 0, -1):
        for start in range(n - size + 1):
            gram = seg[start:start + size]
            if gram not in out:
                out.append(gram)
    return out


def coverage_units(query: str) -> list[tuple[str, float]]:
    """覆盖率打分单位：(子串, 权重)。

    权重取子串长度的平方 —— 命中「反演」比命中孤零零的「反」值钱得多。
    英文单词整体算一个单位，权重同为长度的平方。
    实测这一条就能把「无关领域无损检测」（只含"反"字）压到后面。
    """
    weighted: dict[str, float] = {}
    for seg in re.findall(f"[{CJK}]+", normalize(query)):
        for gram in cjk_ngrams(seg):
            weight = float(len(gram) ** 2)
            if weighted.get(gram, 0.0) < weight:
                weighted[gram] = weight
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{0,30}", normalize(query)):
        weight = float(len(word) ** 2)
        if weighted.get(word.lower(), 0.0) < weight:
            weighted[word.lower()] = weight
    return sorted(weighted.items(), key=lambda kv: (-kv[1], kv[0]))


def keywords(query: str) -> list[str]:
    """只取词元本身（去重、保序），用于在正文里做片段高亮与 LIKE 兜底。"""
    seen: list[str] = []
    for _kind, text in tokenize(query):
        if text not in seen:
            seen.append(text)
    return seen
