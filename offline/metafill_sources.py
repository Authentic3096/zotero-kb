"""metafill_sources.py —— 元数据补全的「两路首页正文」。

## 单一职责

**只负责"把这篇的首页正文取回来、合并、渲染成给模型/规则看的文本"**。
不做规则抽取（`metafill.py` 负责），不调模型（`metafill.py` 负责）。
所以这个模块可以完全离线单测：喂两个假来源就能验全部合并逻辑。

## 为什么是两路（用户 2026-10-05 的要求）

用户原话：「元数据补全能不能同时读首页和 MinerU 首页，然后本地模型综合一下再给出来。」

| 路 | 从哪来 | 长处 | 短处 |
|---|---|---|---|
| **A：pdf** | `zreader.fulltext_for()`（Zotero 缓存 → 退 PyMuPDF） | 是**原始字面**，卷期页码/DOI 这类"印在纸上的串"最保真 | 页眉页脚混杂、上下标会串行 |
| **B：mineru** | `<kb>/mineru/<KEY>/pages.json`（MinerU 逐页正文） | 版面理解好：页眉页脚已丢、作者行/表格更整齐 | 偶有识别偏差（把 `Vol. 37` 归一化、公式转写错） |

合并策略（**不做"猜"**）：
- 两路归一化后**同一个值** → `agreement="both"`，置信度高（两路互证）；
- 只有一路有 → `agreement="single"`，用它但标明来源；
- 两路**值不同** → `agreement="conflict"`，**不擅自合并**：
  默认按 `PREFERRED_IN_CONFLICT` 取一路（有注释说明理由），另一路进 `alternatives`
  一起交给模型/用户；模型给出结论时按"它选了哪一路"再标注。

⚠ 这一层**不猜**：拿不到就如实说拿不到（`why`），让上层去决定退化方式。
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

HEAD_PAGES = 2          # 只看前两页（与 metafill.HEAD_PAGES 保持一致）
_MAX_CHARS = 6000       # 单路给模型的字符上限（与 metafill 的老行为一致）

# 两路冲突时，各字段优先信哪一路 —— **逐字段定**，不是一刀切。
#
# 根据（写下来免得以后被当成随手写的）：
#   · DOI / volume / issue / pages / date：这些是"印在首页上的字面串"，
#     正则对**原始字符**最可靠；MinerU 会把 `Vol. 37`、`10. 3969 /j. …`
#     这类做归一化或拆分，反而可能改变字面。
#   · creators：多作者行的**版面切分**是关键，MinerU 明显更强
#     （本机实测：两路都能抽出作者时，MinerU 的上下标/顿号处理更整齐）。
PREFERRED_IN_CONFLICT = {
    "DOI": "pdf", "volume": "pdf", "issue": "pdf", "pages": "pdf",
    "date": "pdf", "creators": "mineru",
}
_DEFAULT_PREFERRED = "pdf"

# 归一化用于"两路是否一致"的比较（**不**用于写入值）
_NORM_DROP = re.compile(r"[\s\u3000]+")


def norm_value(field: str, value: str) -> str:
    """把值压成"可比形态"，只用于两路一致性判断。

    ⚠ 刻意做得保守：只去空白、统一连字符与大小写，不做语义换算
      （例如不把 "2025 年 12 月" 变成 "2025-12"）—— 那是上层 `_clean_*` 的事。
      保守的好处：不会把"看起来一样、其实不同"的两个值误判成一致。
    """
    s = str(value or "").strip()
    if field == "pages" or field == "volume" or field == "issue":
        s = s.replace("~", "-").replace("—", "-").replace("–", "-")
        s = _NORM_DROP.sub("", s)
    elif field == "DOI":
        s = s.lower().replace(" ", "")
    elif field == "creators":
        s = _NORM_DROP.sub("", s)
    else:
        s = _NORM_DROP.sub(" ", s)
    return s


def _kb_dir(kb_dir: str = "") -> str:
    if kb_dir:
        return kb_dir
    try:
        import schemas as S
        return S.kb_dir()
    except Exception:      # noqa: BLE001
        return ""


def load_mineru_pages(key: str, kb_dir: str = "") -> tuple[list[str], dict]:
    """取 B 路（MinerU 产物）。返回 `(pages, info)`，失败给空列表 + 原因。

    任何异常都不冒泡：MinerU 是可选组件，它坏了不该影响元数据补全。
    """
    info = {"ok": False, "source": "", "why": "", "dir": ""}
    key = str(key or "").strip()
    if not key:
        info["why"] = "没有 key"
        return ([], info)
    try:
        import mineru as M
        pages = M.load_pages(key, kb_dir)
        adir = M.artifact_dir(key, kb_dir)
        info["dir"] = adir
        if pages:
            meta = M.read_meta(key, kb_dir)
            info.update({"ok": True, "source": f"mineru-{meta.get('tier') or '?'}",
                         "tier": meta.get("tier") or "", "n_pages": len(pages)})
            return (pages, info)
        info["why"] = ("没有 MinerU 产物（没装/没解析过这一篇）"
                       if not os.path.isdir(adir) else "产物里没有可用的逐页文本")
        return ([], info)
    except Exception as exc:      # noqa: BLE001
        info["why"] = f"{type(exc).__name__}: {exc}"
        return ([], info)


def gather(item, *, reader=None, kb_dir: str = "",
           head_pages: int = HEAD_PAGES) -> dict:
    """取 A/B 两路的首页正文。

    返回（**结构固定**，调用方不必判键存在）：
      {
        "pdf":    {"pages": [...], "source": "zotero-cache", "ok": bool, "why": str},
        "mineru": {"pages": [...], "source": "mineru-basic", "ok": bool, "why": str},
        "head_pages": 2,
        "any": bool,          # 至少有一路可用
        "both": bool,         # 两路都有
      }
    """
    out = {
        "head_pages": int(head_pages),
        "pdf": {"pages": [], "source": "", "ok": False, "why": ""},
        "mineru": {"pages": [], "source": "", "ok": False, "why": ""},
    }

    # ---- A 路：PDF 自解析
    try:
        if reader is None:
            import metafill
            reader = metafill._get_reader()
        pages, src = reader.fulltext_for(item, use_model=False)
        pages = [p or "" for p in (pages or [])]
        out["pdf"].update({"pages": pages, "source": src or "",
                           "ok": bool(pages)})
        if not pages:
            out["pdf"]["why"] = "A 路（PDF 自解析）没拿到正文"
    except Exception as exc:      # noqa: BLE001
        out["pdf"]["why"] = f"{type(exc).__name__}: {exc}"

    # ---- B 路：MinerU 产物
    pages_b, info_b = load_mineru_pages(str(getattr(item, "key", "") or ""), kb_dir)
    out["mineru"].update({"pages": [p or "" for p in pages_b],
                          "source": info_b.get("source") or "",
                          "ok": bool(pages_b),
                          "why": "" if pages_b else (info_b.get("why") or "")})
    for k in ("tier", "dir", "n_pages"):
        if k in info_b:
            out["mineru"][k] = info_b[k]

    out["any"] = bool(out["pdf"]["ok"] or out["mineru"]["ok"])
    out["both"] = bool(out["pdf"]["ok"] and out["mineru"]["ok"])
    return out


def head_text(pages: list[str], head_pages: int = HEAD_PAGES,
              max_chars: int = _MAX_CHARS) -> str:
    """前 N 页拼起来并截断（A/B 两路都用它，保证同一套口径）。"""
    text = "\n".join((p or "") for p in (pages or [])[:max(1, int(head_pages))])
    text = text.strip()
    return text[:max_chars] if max_chars else text


_LABEL = {"pdf": "来源 A：PDF 原文字面（Zotero 缓存/PyMuPDF）",
          "mineru": "来源 B：MinerU 版面解析"}


def sources_line(sources: dict) -> str:
    """一行摘要：这次到底拿到哪几路（写进 notes / 结果里，便于排查）。"""
    got = []
    if (sources.get("pdf") or {}).get("ok"):
        got.append("A(pdf:" + ((sources["pdf"].get("source") or "?")[:24]) + ")")
    if (sources.get("mineru") or {}).get("ok"):
        got.append("B(mineru:" + ((sources["mineru"].get("source") or "?")[:24]) + ")")
    return "＋".join(got) if got else "（两路都没拿到正文）"


def candidate_lines(merged: dict) -> list[str]:
    """把合并后的候选渲染成给模型看的几行（供它核对/裁决）。"""
    lines = []
    for field in sorted(merged):
        row = merged[field]
        vals = []
        for v in row.get("values", []):
            tag = "A" if v.get("source") == "pdf" else "B"
            vals.append(f"{v.get('value')}（{tag}）")
        mark = {"both": "两路一致", "single": "单路",
                "conflict": "⚠ 两路不一致"}.get(row.get("agreement"), "")
        lines.append(f"  · {field} = " + " / ".join(vals) + f"   [{mark}]")
    return lines


def prompt_text(sources: dict, merged: dict | None = None,
                head_pages: int = HEAD_PAGES, max_chars: int = _MAX_CHARS) -> str:
    """渲染给模型（与给规则）的合并文本。

    ⚠ **刻意通过 `{text}` 这一个占位符送进去**（`prompts.render` 只做
      `{name}` 替换）：这样用户**已经保存过的旧提示词覆盖**照样能用 ——
      模板不变、payload 变丰富。默认提示词里会说明这个结构（见 prompts.py）。
    """
    blocks: list[str] = []
    for key in ("pdf", "mineru"):
        src = sources.get(key) or {}
        if not src.get("ok"):
            continue
        txt = head_text(src.get("pages") or [], head_pages, max_chars)
        if not txt:
            continue
        blocks.append(f"【{_LABEL[key]}，前 {head_pages} 页】\n{txt}")
    if merged:
        lines = candidate_lines(merged)
        if lines:
            blocks.append("【规则从上面两路正文里抽到的候选（供你核对与裁决；"
                          "以正文里**字面出现过**的为准）】\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _eq(field: str, a: str, b: str) -> bool:
    na, nb = norm_value(field, a), norm_value(field, b)
    if not na or not nb:
        return False
    if field in ("volume", "issue"):
        return na == nb
    if field == "pages":
        return na == nb
    if field == "creators":
        return na == nb
    return na == nb


def merge_candidates(per_source: dict[str, dict]) -> dict:
    """合并两路的规则候选。

    入参形状：`{"pdf": {field: (value, start, end)}, "mineru": {...}}`
    （就是 `metafill._extract_volume_issue_pages` / `_extract_doi` / `_extract_date` /
      `extract_creators` 的产物，按来源分开装）。

    返回：`{field: {"values": [{"value","source","start","end"}…],
                    "agreement": "both"|"single"|"conflict",
                    "primary": {...}, "alternatives": [...]}}`

    ⚠ 多值字段（creators）的"值"是**已渲染成一行摘要的字符串**（`; ` 连接），
      真正的结构化数据留在各自的候选里由上层取用 —— 这里只负责"一致/冲突"的判断。
    """
    out: dict[str, dict] = {}
    fields = sorted({f for src in per_source.values() for f in (src or {})})
    for field in fields:
        vals: list[dict] = []
        for source in ("pdf", "mineru"):
            got = (per_source.get(source) or {}).get(field)
            if not got:
                continue
            value, start, end = got[0], got[1], got[2]
            vals.append({"value": value, "source": source,
                         "start": start, "end": end})
        if not vals:
            continue
        groups: list[str] = []          # 归一化后的不同值
        for v in vals:
            n = norm_value(field, v["value"])
            if n and n not in groups:
                groups.append(n)
        if len(groups) <= 1:
            agreement = "both" if len(vals) > 1 else "single"
            primary = vals[0]
            alts: list[dict] = []
        else:
            agreement = "conflict"
            prefer = PREFERRED_IN_CONFLICT.get(field, _DEFAULT_PREFERRED)
            primary = next((v for v in vals if v["source"] == prefer), vals[0])
            alts = [v for v in vals if v is not primary]
        out[field] = {"values": vals, "agreement": agreement,
                      "primary": primary, "alternatives": alts}
    return out


def agreement_note(field: str, row: dict) -> str:
    """给用户/日志看的一句话（写进 suggestion 的 note）。"""
    if row.get("agreement") == "both":
        return "两路正文一致（更可信）"
    if row.get("agreement") == "single":
        src = row["primary"]["source"]
        return f"只有{'A（PDF 原文）' if src == 'pdf' else 'B（MinerU）'}一路给出"
    pref = row["primary"]["source"]
    other = "、".join(str(a["value"]) for a in row.get("alternatives") or [])
    return (f"⚠ 两路不一致，默认取了"
            f"{'A（PDF 原文）' if pref == 'pdf' else 'B（MinerU）'}的值"
            + (f"；另一路是 {other}" if other else ""))


def why_no_text(sources: dict) -> str:
    """两路都拿不到时，给一句能查的说明。"""
    bits = []
    for key, label in (("pdf", "A（PDF 自解析）"), ("mineru", "B（MinerU）")):
        src = sources.get(key) or {}
        if not src.get("ok"):
            bits.append(f"{label}：{src.get('why') or '不可用'}")
    return "；".join(bits) or "没有可用的正文"
