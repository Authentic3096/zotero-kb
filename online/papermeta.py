"""给 DOI 补上"能判断这篇值不值得看"的信息：摘要、关键词、主题、OA、被引。

**为什么要单独有这个模块**：DSH 列候选表时，光有"标题 + 作者 + 年份"用户没法判断
合不合适 —— 他得看到这篇**讲了什么**。所以候选表要带摘要和关键词。

## 数据源与优先级（都实测过）

| 源 | 给什么 | 备注 |
|---|---|---|
| **OpenAlex**（主） | 摘要（倒排索引还原）、关键词、主题、OA 状态与直链、被引数 | 免 key；支持一次查 50 个 DOI（filter 里用竖线连接），比逐个查快得多 |
| **Crossref**（兜底摘要） | `abstract`（JATS XML，要剥标签） | 不少出版社不上传摘要，常为空 |
| **Semantic Scholar**（再兜底） | `abstract` / `tldr` | 限流较紧，只在前面都没有时才问 |

## 一条纪律：**没有摘要就说没有，不要编**

实测 HDSR（Harvard Data Science Review）这类刊**三方都没有摘要**。
这时如实返回 `abstract=""` + `abstract_source="none"`，由调用方在表里写
"（该刊未提供摘要）"，并用关键词/主题补足判断依据。**不要拿标题去"总结"一段摘要**。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "ZoteroAcquire/1.2 (DSH skill; metadata lookup)"
# OpenAlex 的 polite pool：带上邮箱会被优先服务、限流更宽
MAILTO = "zotero.acquire.skill@gmail.com"

TIMEOUT = 40.0
ABSTRACT_CHARS = 700          # 候选表里够读就行；要全文另有 kb_fulltext


def _get_json(url: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _inv_to_text(inv) -> str:
    """OpenAlex 的摘要存成**倒排索引**（词 → 位置列表），要还原成正文。

    位置可能不连续（有的条目被抽掉了），所以按位置排序后直接拼，
    缺号不补 —— 补空格会引入不存在的换行。
    """
    if not isinstance(inv, dict) or not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        if not isinstance(idxs, list):
            continue
        for i in idxs:
            if isinstance(i, int):
                pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _strip_jats(s: str) -> str:
    """Crossref 的 abstract 是 JATS XML 片段，剥标签 + 压空白。"""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&apos;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def _clip(s: str, n: int = ABSTRACT_CHARS) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    # 尽量断在句末，读起来不像被砍断的
    cut = s[:n]
    for p in ("。", ". ", "；", "; "):
        i = cut.rfind(p)
        if i > n * 0.6:
            return cut[: i + len(p)].strip()
    return cut.rstrip() + "…"


def _norm_doi(d: str) -> str:
    return str(d or "").strip().lower().lstrip("https://doi.org/").lstrip("doi:")


# ---------------------------------------------------------------- OpenAlex 批量


def _openalex_batch(dois: list[str]) -> dict[str, dict]:
    """一次查一批（OpenAlex 的 filter 支持 `|` 连接，上限 50）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        f = "doi:" + "|".join(chunk)
        url = ("https://api.openalex.org/works?filter=" + urllib.parse.quote(f, safe=":|")
               + f"&per-page=50&mailto={MAILTO}")
        try:
            data = _get_json(url)
        except Exception:  # noqa: BLE001
            continue
        for w in data.get("results") or []:
            d = _norm_doi(w.get("doi") or "")
            if d:
                out[d] = w
        if i + 50 < len(dois):
            time.sleep(0.3)
    return out


def _from_openalex(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    oa = w.get("open_access") or {}
    authors = []
    for a in (w.get("authorships") or [])[:20]:
        nm = ((a.get("author") or {}).get("display_name") or "").strip()
        if nm:
            authors.append(nm)
    best = w.get("best_oa_location") or {}
    return {
        "title": (w.get("title") or w.get("display_name") or "").strip(),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "year": w.get("publication_year") or "",
        "venue": (src.get("display_name") or "").strip(),
        "type": w.get("type") or "",
        "cited_by": w.get("cited_by_count") or 0,
        "oa_status": oa.get("oa_status") or "",
        "oa_url": best.get("pdf_url") or oa.get("oa_url") or "",
        "landing": loc.get("landing_page_url") or "",
        "keywords": [k.get("display_name") for k in (w.get("keywords") or [])
                     if k.get("display_name")][:12],
        "topics": [t.get("display_name") for t in (w.get("topics") or [])
                   if t.get("display_name")][:4],
        "abstract": _inv_to_text(w.get("abstract_inverted_index")),
        "abstract_source": "openalex",
    }


# ---------------------------------------------------------------- 摘要兜底


def _crossref_abstract(doi: str) -> str:
    try:
        m = _get_json("https://api.crossref.org/works/"
                      + urllib.parse.quote(doi))["message"]
    except Exception:  # noqa: BLE001
        return ""
    return _strip_jats(m.get("abstract") or "")


def _s2_abstract(doi: str) -> str:
    try:
        w = _get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:"
                      + urllib.parse.quote(doi) + "?fields=abstract,tldr")
    except Exception:  # noqa: BLE001
        return ""
    return (w.get("abstract") or (w.get("tldr") or {}).get("text") or "").strip()


def _crossref_core(doi: str) -> dict:
    """Crossref 的元数据（OpenAlex 没收录时兜底）。"""
    try:
        m = _get_json("https://api.crossref.org/works/"
                      + urllib.parse.quote(doi))["message"]
    except Exception:  # noqa: BLE001
        return {}
    authors = []
    for a in (m.get("author") or [])[:20]:
        nm = " ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
        if nm:
            authors.append(nm)
    yr = ""
    for k in ("published-print", "published-online", "issued", "created"):
        dp = ((m.get(k) or {}).get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            yr = dp[0]
            break
    return {
        "title": (m.get("title") or [""])[0].strip(),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "year": yr,
        "venue": (m.get("container-title") or [""])[0].strip(),
        "type": m.get("type") or "",
        "abstract": _strip_jats(m.get("abstract") or ""),
        "abstract_source": "crossref" if m.get("abstract") else "none",
    }


# ---------------------------------------------------------------- 对外接口


def enrich(dois, with_abstract: bool = True) -> list[dict]:
    """把 DOI 列表补成"能判断值不值得看"的档案。

    Args:
        dois: DOI 字符串列表（各种写法都认）。
        with_abstract: 是否抓摘要。**关掉只是不填 abstract 字段，
            关键词/主题/OA/被引仍然会给** —— 那些才是判断的主要依据。

    Returns:
        与输入**等长、同序**的列表（这样调用方按下标就能对上候选表）。
        每项含 `ok`；ok=False 时只有 `doi` 与 `error`。
    """
    norm = [_norm_doi(d) for d in (dois or [])]
    uniq = [d for d in dict.fromkeys(norm) if d]
    if not uniq:
        return []

    oa_map = _openalex_batch(uniq)

    out: list[dict] = []
    for d in norm:
        if not d:
            out.append({"doi": "", "ok": False, "error": "空 DOI"})
            continue
        rec = None
        w = oa_map.get(d)
        if w:
            rec = _from_openalex(w)
        else:
            rec = _crossref_core(d)
            if rec:
                rec.setdefault("cited_by", 0)
                rec.setdefault("oa_status", "")
                rec.setdefault("oa_url", "")
                rec.setdefault("landing", "")
                rec.setdefault("keywords", [])
                rec.setdefault("topics", [])
        if not rec or not rec.get("title"):
            out.append({"doi": d, "ok": False,
                        "error": "OpenAlex 与 Crossref 都查不到这个 DOI"})
            continue

        # 摘要兜底链：OpenAlex → Crossref → Semantic Scholar
        if with_abstract and not rec.get("abstract"):
            ab = _crossref_abstract(d)
            if ab:
                rec["abstract"], rec["abstract_source"] = ab, "crossref"
            else:
                ab = _s2_abstract(d)
                if ab:
                    rec["abstract"], rec["abstract_source"] = ab, "semanticscholar"
        if not rec.get("abstract"):
            rec["abstract_source"] = "none"
        rec["abstract"] = _clip(rec.get("abstract") or "")
        rec["doi"] = d
        rec["ok"] = True
        out.append(rec)
    return out


# ---------------------------------------------------------------- CLI（自查用）


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="给 DOI 补摘要/关键词/OA/被引")
    ap.add_argument("dois", nargs="+")
    ap.add_argument("--no-abstract", action="store_true")
    args = ap.parse_args()

    for r in enrich(args.dois, with_abstract=not args.no_abstract):
        if not r.get("ok"):
            print(f"✗ {r.get('doi')}  {r.get('error')}")
            continue
        print(f"● {r['title']}")
        print(f"  {r.get('first_author')} {r.get('year')} · {r.get('venue')}"
              f" · 被引 {r.get('cited_by')} · OA={r.get('oa_status') or '否'}")
        if r.get("keywords"):
            print("  关键词：", "、".join(r["keywords"]))
        if r.get("topics"):
            print("  主题：", "、".join(r["topics"]))
        print(f"  摘要（{r.get('abstract_source')}，{len(r.get('abstract') or '')} 字）：",
              (r.get("abstract") or "（该刊未提供摘要）")[:300])
        if r.get("oa_url"):
            print("  OA 直链：", r["oa_url"])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
