"""知识库的 MCP 服务器：让 DSH 通过工具与资源读取文献。

    python online/server.py            # 由 DSH 通过 dsh-mcp-client 以 stdio 拉起

暴露两类能力：
  · 工具（模型主动调用）：kb_search / kb_item / kb_fulltext /
    kb_experience_add / kb_experience_query / kb_weight_set / kb_reindex / kb_stats
  · 资源（模型按需读取，不占上下文）：
    zotero-kb://item/tldr/{key}        摘要级（几百 token）
    zotero-kb://item/{key}             完整档案
    zotero-kb://item/full/{key}        全文正文
    zotero-kb://collections            分类清单

设计约束（都来自实测）：
  · 启动必须快 —— 嵌入模型与 numpy 都是**懒加载**，只有真用到向量时才付那份成本。
  · 单个工具的返回要克制 —— 全文本默认截断，避免一次把上下文撑爆。
  · 任何一路失败都要降级而不是崩 —— 向量不可用时关键词检索照常工作。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, HERE)

import schemas as S  # noqa: E402
import experience as EXP  # noqa: E402 —— 经验层的唯一写入实现（含权重算术）
import extrafill as XF  # noqa: E402 —— 结构化字段的展示口径在这里（describe）
import kbviews as KBV  # noqa: E402 —— 分级视图（tldr 级的渲染实现在那儿）

try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover
    print(f"需要 mcp 包：{exc}", file=sys.stderr)
    raise

from searcher import KnowledgeBaseMissing, Searcher  # noqa: E402

# 全文返回上限：约 2 万字符足够看清方法细节，又不至于一次吃掉整个上下文
DEFAULT_FULLTEXT_CHARS = 20000
MAX_FULLTEXT_CHARS = 120000

mcp = MCPServer(
    name="zotero-kb",
    title="Zotero 文献知识库",
    version="1.0.0",
    instructions=(
        "本地 Zotero 文献知识库。用法约定：\n"
        "1) 先 kb_search 找相关文献（返回带页码的命中片段与经验权重）；\n"
        "2) 对确实相关的条目用 kb_item 看档案，或读资源 zotero-kb://item/tldr/{key}；\n"
        "3) 只有需要方法细节时才 kb_fulltext 取正文，按页取更省上下文；\n"
        "4) 用某篇的方法做了尝试、或明确判断某篇有效/无效时，调 kb_experience_add 记录"
        "（outcome: effective/ineffective/partial/unknown），下次检索会自动把验证过的文献排前；\n"
        "5) 判断某篇是重点时调 kb_weight_set 打标记。\n"
        "本库是只读的 Zotero 快照，绝不修改你的 Zotero 数据库。"
    ),
)

_searcher: Searcher | None = None


def kb() -> Searcher:
    """懒加载检索器：进程启动时不读索引库，第一次调用才建连接。"""
    global _searcher
    if _searcher is None:
        _searcher = Searcher()
    return _searcher


def kb_error(exc: Exception) -> str:
    """把索引没建好这种情况翻译成给模型看的、可执行的提示。

    直接返回 sqlite 的 "no such table: meta" 对谁都没用 ——
    模型不知道该怎么办，用户也看不懂。所以这里明确告诉它先建索引。
    """
    if isinstance(exc, KnowledgeBaseMissing):
        return jdump({
            "error": "知识库还没有构建（或索引不完整）。",
            "how_to_fix": [
                "在终端跑一次构建：scripts\\1-convert.cmd",
                "或让用户双击该文件；构建只需几秒（首次要下载嵌入模型约 90MB）",
                "构建完成后再调用本工具",
            ],
            "detail": str(exc),
        })
    return jdump({"error": f"{type(exc).__name__}: {exc}"})


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def safe(fn):
    """把工具执行包起来：任何异常都变成可读的 JSON，而不是把堆栈抛给模型。

    MCP 桥接会把未捕获异常当作工具失败报给模型，模型只看到一句
    "tool call failed"，既不知道原因也没法自救。这里统一兜住，
    并且对"索引没建好"给出明确的操作指引。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KnowledgeBaseMissing as exc:
            return kb_error(exc)
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"[zotero-kb] {fn.__name__} 出错：{exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jdump({
                "error": f"{type(exc).__name__}: {exc}",
                "tool": fn.__name__,
                "hint": "服务端日志里有完整堆栈；可先跑 scripts\\3-maintain.cmd check 自检。",
            })

    return wrapper


# ---------------------------------------------------------------- 工具：检索


@mcp.tool(description=(
    "在 Zotero 文献知识库中检索。混合了关键词与语义两路，并按"
    "「经验权重」加权 —— 被验证有效或被标记为重点的文献会自动排前。"
    "同时返回与本次查询相关的历史经验（哪些方法试过、效果如何）。\n"
    "命中里若带 `structured`，说明这篇有知网写入的结构化字段"
    "（中图分类号 / 来源类型 / 收录标签 / 中文译名 / 学位专业）——"
    "英文标题的文献常常有中文译名，可据此确认是不是用户说的那篇。\n"
    "要**按结构化字段筛**就传 source_kind（journal/thesis/conference，"
    "也认中文「期刊论文/学位论文/会议论文」）、publication_tag（如 EI、北大核心、"
    "CSCD）、clc（中图分类号，支持大类前缀如 TP39）。"))
@safe
def kb_search(
    query: str,
    limit: int = 8,
    collection: str = "",
    year_from: int = 0,
    year_to: int = 0,
    with_experience: bool = True,
    source_kind: str = "",
    publication_tag: str = "",
    clc: str = "",
) -> str:
    """检索知识库。

    Args:
        query: 自然语言或关键词，中英文均可。例如「机器学习模型」「预训练补偿 神经网络」。
        limit: 返回条目数上限（默认 8）。
        collection: 限定分类名（可先 kb_search 不带过滤，或看 zotero-kb://collections）。
        year_from: 起始年份（含），0 表示不限。
        year_to: 结束年份（含），0 表示不限。
        with_experience: 是否附带相关历史经验（默认是）。
        source_kind: 按**来源类型**筛：journal / thesis / conference（知网 dbcode 推出，
            只对 extra 里带 dbcode 的条目有效）。
        publication_tag: 按**收录标签**筛，如 EI、北大核心、CSCD、JST（完整标签名匹配）。
        clc: 按**中图分类号**筛，如 TP391、P631；可给大类前缀（TP39 能筛出 TP391.9）。
    """
    s = kb()
    cols = [collection] if collection else None
    result = s.search(
        query, limit=max(1, min(limit, 30)), collections=cols,
        year_from=year_from or None, year_to=year_to or None,
        source_kind=source_kind, publication_tag=publication_tag, clc=clc,
    )
    hits = result["hits"]
    payload = {
        "query": query,
        "returned": len(hits),
        "diagnostics": result["diagnostics"],
        "hits": [
            {
                "key": h["key"],
                # 可读引用名：`作者 年份 · 短标题`，带 [KEY] 的完整标签。
                # 为什么给模型也给这个：item key 是 8 位大写字母数字
                # （22X9PMR6），模型转述给用户时完全认不出是哪篇；
                # 有了 ref/label，模型回答和引用列表都直接可读。
                # ⚠ 它**不唯一**（库里有 3 篇同名文献），所以 key 必须
                #   照旧返回，不能拿 label 当标识。
                "ref": h.get("ref") or h["title"],
                "label": h.get("label") or f"{h['title']}  [{h['key']}]",
                "title": h["title"],
                "year": h["year"],
                "first_author": h["first_author"],
                "type": h["item_type"],
                "collections": h["collections"],
                "weight": h["weight"],
                "snippets": [
                    {"page": sn["page"], "text": sn["text"]} for sn in h["snippets"]
                ],
                # 结构化字段（知网写入的）：有才出现。**放在命中里**的理由见
                # searcher.search 末尾那段说明 —— 它是"这篇是不是我要的"的
                # 判断依据之一（尤其英文文献的中文译名）。
                **({"structured": h["structured"]} if h.get("structured") else {}),
            }
            for h in hits
        ],
    }
    if with_experience:
        keys = [h["key"] for h in hits]
        exp = s.related_experience(query, item_keys=keys, limit=6)
        if exp:
            # 与 kb_experience_query 的组装保持一致：字段用 .get() 兜底，
            # 并且**带上 evidence** —— 检索这条路也要能看到"依据"，
            # 否则模型只能看到结论、无法判断这条经验有多可信。
            payload["related_experience"] = [
                {
                    "when": (e.get("created_at") or "")[:10],
                    "asked": e.get("asked"),
                    "method": e.get("method"),
                    "outcome": e.get("outcome"),
                    "reason": e.get("reason"),
                    "evidence": e.get("evidence"),
                    "items": json.loads(e.get("item_keys") or "[]"),
                }
                for e in exp
            ]
    if not hits:
        diag = result["diagnostics"]
        if diag.get("facet_error"):
            # 结构化字段读不到时**明说**：否则"筛完没有"看起来像"库里没有"，
            # 用户会以为自己的库真的缺这类文献。
            payload["hint"] = (
                "结构化字段过滤没能生效：" + str(diag["facet_error"])
                + "（这不代表库里没有这类文献 —— 去掉 source_kind / "
                  "publication_tag / clc 再搜一次看看）"
            )
        elif diag.get("facet_filter"):
            payload["hint"] = (
                "关键词有命中，但按结构化字段（"
                + "、".join(f"{k}={v}" for k, v in diag["facet_filter"].items())
                + "）筛完一条都不剩。注意：只有 extra 里带知网结构化字段的条目"
                  "才有这些属性，其余条目筛不到不代表它不符合；"
                  "可以去掉该过滤再搜。"
            )
        else:
            payload["hint"] = (
                "没有命中。可以试试：换同义词（如「神经网络」↔「magnetic dipole」）、"
                "去掉限定条件、或用 kb_stats 确认库规模。"
            )
    return jdump(payload)


@mcp.tool(description=(
    "检索文献里的**图注与表格**。当用户问『哪篇里有画…的图』『谁做过…的"
    "对比表』『那张系统的结构图在哪篇』时用它 —— 图注里的文字（图1.1 "
    "神经网络模型、Fig. 5. Electrochemical characterizations）是做不了"
    "全文检索的‘正文’之外的独立信息源。\n\n"
    "返回每张图表所在的条目、页码、图注原文；要**看图表内容**时，"
    "用 kb_fulltext(key, page_from=该页, page_to=该页) 读那一页 —— "
    "图注与表格 Markdown 就渲染在那一页的末尾。"))
@safe
def kb_figures(
    query: str = "",
    key: str = "",
    kind: str = "all",
    limit: int = 40,
) -> str:
    """按图注文字检索图表。

    Args:
        query: 图注关键词（如"神经网络模型"、"Ragone"、"实验装置"）。留空则按 key 列全部。
        key: 限定某篇文献的 key（与 query 二选一或组合）。
        kind: all / image（图） / table（表）。
        limit: 最多返回几条（默认 40）。
    """
    s = kb()
    where, args = [], []
    if key:
        where.append("item_key = ?")
        args.append(key)
    if query:
        # 图注检索走 LIKE：图注短、条数少（本机 31 条/篇量级），
        # 没必要进 FTS；而且 LIKE 支持"图1.1"这种带点的编号查询。
        where.append("(text LIKE ? OR label LIKE ?)")
        args += [f"%{query}%", f"%{query}%"]
    if kind == "image":
        where.append("kind LIKE 'caption-image%'")
    elif kind == "table":
        where.append("(kind = 'table' OR kind LIKE 'caption-table%')")
    sql = "SELECT item_key, page, kind, label, text, n_rows, n_cols FROM figures"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY item_key, page, fig_id LIMIT ?"
    args.append(max(1, min(200, limit)))
    try:
        rows = s.read(sql, tuple(args))
    except Exception as exc:  # noqa: BLE001
        return jdump({"error": f"读 figures 表失败：{exc}",
                      "hint": "老库还没建图注表 —— 跑一次 "
                              "scripts\\1-convert.cmd 重新构建即可。"})
    if not rows:
        return jdump({"hits": [], "note": "没有匹配的图注或表格。",
                      "hint": "图注是构建时从 PDF 抽的；如果刚加的新文献，"
                              "先 kb_reindex 补抽。"})
    # 带上标题，省得调用方再查一次
    out = []
    for r in rows:
        it = s.get_item(r["item_key"]) or {}
        rec = {
            "key": r["item_key"],
            "title": (it.get("title") or "")[:70],
            "page": r["page"],
            "label": r["label"],
            "caption": (r["text"] or "")[:300],
        }
        if r["kind"] == "table":
            rec["kind"] = "table"
            rec["rows"], rec["cols"] = r["n_rows"], r["n_cols"]
        else:
            rec["kind"] = "figure"
        out.append(rec)
    return jdump({
        "hits": out,
        "count": len(out),
        "how_to_view": "看图表内容：kb_fulltext(key, page_from=page, page_to=page)",
    })


@mcp.tool(description=(
    "读一篇文献的完整档案：元数据、摘要、笔记、高亮标注、它的使用经验，"
    "以及**图注与表格清单**（哪个图在第几页、标题是什么）。"
    "若条目带知网写入的结构化字段（中图分类号 / 来源类型 / 收录标签 / "
    "中文译名 / 学位专业 / 期刊级别信息），会一并给出在 `structured` 里。"))
@safe
def kb_item(key: str, include_fulltext_head: bool = False) -> str:
    """按 Zotero item key 读条目档案。

    Args:
        key: 8 位 Zotero key（kb_search 结果里的 key）。
        include_fulltext_head: 是否附带正文开头（默认否，正文请用 kb_fulltext 取）。
    """
    s = kb()
    item = s.get_item(key)
    if not item:
        return jdump({"error": f"知识库里没有 key={key} 的条目。",
                      "hint": "先用 kb_search 检索，或 kb_reindex 补抽该条目。"})
    payload = {
        "key": item["key"],
        "title": item["title"],
        "year": item["year"],
        "first_author": item["first_author"],
        "authors": item["author_line"],
        "type": item["item_type"],
        "venue": item["venue"],
        "doi": item["doi"],
        "collections": item["collections"],
        "tags": item["tags"],
        "abstract": item["abstract"],
        "n_pdfs": item["n_pdfs"],
        "n_notes": item["n_notes"],
        "n_annotations": item["n_annotations"],
        "fulltext_chars": item["fulltext_chars"],
        "fulltext_source": item["fulltext_src"],
        "weight": item["weight"],
        "paper_md": f"kb/papers/{key}.md",
    }
    # ---- 结构化字段（extra 里知网写入的那些）
    #
    # 为什么档案里要有它：中图分类号 / 收录标签（北大核心、EI、CSCD）/
    # 中文译名 / 学位专业这些信息**正文里根本没有**（分类号不会印在 PDF 上），
    # 只有 extra 里有。它们直接决定"这篇是什么层次、是不是用户说的那篇"。
    #
    # 给两份，作用不同：
    #   · structured —— 提纯后的结论（中文名、拆成列表），口径来自 extrafill.describe；
    #   · extra_fields —— extra 里**全部**键值原样照录（含 download / foundation
    #     这类"只原样留存、不做判断"的）。用户/模型要参考原始凭据时看它。
    extra_row = s.extras_for_key(key)
    if extra_row:
        view = XF.describe(extra_row)
        if view:
            payload["structured"] = view
        raw = XF.fields_of(extra_row)
        if raw:
            payload["extra_fields"] = raw
    # 图注与表格（从 PDF 抽的，见 offline/figures.py）。
    # 只给"有哪些图、叫什么、在第几页" —— 需要看内容时用
    # kb_fulltext(page_from=N, page_to=N) 读那一页（图注就渲染在页末）。
    try:
        figs = s.read(
            "SELECT page, kind, label, text, n_rows, n_cols FROM figures "
            "WHERE item_key = ? ORDER BY page, fig_id", (key,))
        if figs:
            payload["n_figures"] = sum(1 for f in figs
                                       if (f["kind"] or "").startswith("caption"))
            payload["n_tables"] = sum(1 for f in figs
                                      if f["kind"] == "table")
            payload["figures"] = [
                {"page": f["page"], "label": f["label"],
                 "caption": (f["text"] or "")[:200],
                 **({"rows": f["n_rows"], "cols": f["n_cols"]}
                    if f["kind"] == "table" else {})}
                for f in figs[:60]
            ]
            if len(figs) > 60:
                payload["figures_note"] = f"（共 {len(figs)} 项，只列前 60）"
    except Exception:  # noqa: BLE001
        pass       # 老库还没有 figures 表时不要因此报错
    notes = s.read(
        "SELECT chunk_id, page, text FROM chunks WHERE item_key=? AND text LIKE '[%笔记]%'",
        (key,),
    )
    if notes:
        payload["notes_preview"] = [n["text"][:1500] for n in notes[:3]]
    anns = s.read(
        "SELECT page, text FROM chunks WHERE item_key=? AND text LIKE '[%高亮]%' LIMIT 30",
        (key,),
    )
    if anns:
        payload["annotations"] = [{"page": a["page"], "text": a["text"][:400]}
                                  for a in anns]
    exp = s.experience_for_item(key)
    if exp:
        payload["experience"] = [
            {"when": e["created_at"][:10], "asked": e["asked"], "method": e["method"],
             "outcome": e["outcome"], "reason": e["reason"], "source": e["source"]}
            for e in exp
        ]
    if include_fulltext_head:
        path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                payload["fulltext_head"] = fh.read(3000)
    return jdump(payload)


@mcp.tool(description=(
    "读一篇文献的正文。可按页范围取，避免一次吃进整篇。"
    "返回带页码锚点的 Markdown，便于引用具体页。"
))
@safe
def kb_fulltext(key: str, page_from: int = 0, page_to: int = 0,
                max_chars: int = DEFAULT_FULLTEXT_CHARS) -> str:
    """取正文。

    Args:
        key: 8 位 Zotero key。
        page_from: 起始页（1 基），0 表示从头。
        page_to: 结束页（含），0 表示到末尾。
        max_chars: 返回字符上限（默认 20000，上限 120000）；超长会截断并说明。
    """
    path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
    if not os.path.exists(path):
        s = kb()
        item = s.get_item(key)
        if not item:
            return jdump({"error": f"没有 key={key} 的条目。"})
        return jdump({
            "error": f"《{item['title']}》没有可读正文。",
            "reason": "该条目是网页类或没有 PDF 附件" if not item["n_pdfs"]
                      else "PDF 没有文字层（扫描件），当前未接 OCR",
            "fallback": "可以看 kb_item 里的摘要与笔记，或读 kb/papers/<key>.md",
        })

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    total = len(text)
    truncated = False
    pages_kept = None

    if page_from or page_to:
        # 按 `## p.N` 切块，只留请求的页范围
        blocks = []
        current = None
        for line in text.split("\n"):
            if line.startswith("## p."):
                if current:
                    blocks.append(current)
                try:
                    page_no = int(line[5:].strip())
                except ValueError:
                    page_no = 0
                current = [page_no, [line]]
            elif current:
                current[1].append(line)
            else:
                current = [0, [line]]
        if current:
            blocks.append(current)
        lo = page_from or 0
        hi = page_to or 10 ** 9
        kept = [b for b in blocks if lo <= b[0] <= hi]
        if kept:
            text = "\n".join("\n".join(b[1]) for b in kept)
            pages_kept = [b[0] for b in kept]

    cap = max(1000, min(int(max_chars or DEFAULT_FULLTEXT_CHARS), MAX_FULLTEXT_CHARS))
    if len(text) > cap:
        text = text[:cap]
        truncated = True

    s = kb()
    item = s.get_item(key)
    return jdump({
        "key": key,
        "title": item["title"] if item else "",
        "pages_returned": pages_kept,
        "chars": len(text),
        "total_chars": total,
        "truncated": truncated,
        "content": text,
        **({"note": f"已截断到 {cap} 字符；需要更多请用 page_from/page_to 分段取。"}
           if truncated else {}),
    })


# ---------------------------------------------------------------- 工具：经验层


@mcp.tool(description=(
    "记录一次使用经验：用某几篇文献的方法做了什么尝试、结果如何、为什么。"
    "这是知识库「越用越准」的关键 —— 记过之后，下次 kb_search 会把验证有效的文献排前，"
    "并把这条经验一并返回，避免重复踩坑。"
))
@safe
def kb_experience_add(
    asked: str,
    outcome: str,
    method: str = "",
    item_keys: str = "",
    context: str = "",
    reason: str = "",
    evidence: str = "",
    tags: str = "",
) -> str:
    """追加一条经验（只追加，不覆盖历史）。

    Args:
        asked: 当时要解决的问题或目标，例如「机器学习模型的传统优化方法怎么选初值」。
        outcome: effective（有效）/ ineffective（无效）/ partial（部分有效）/ unknown（未验证）。
        method: 具体用了什么方法，例如「Levenberg-Marquardt + 多点随机初值」。
        item_keys: 涉及的文献 key，多个用逗号或空格分隔。
        context: 前提条件，例如「样本量 200、随机种子固定、单卡训练」。
        reason: 为什么有效或失败，例如「初值敏感，10 次里 3 次收敛到局部极小」。
        evidence: 指向本次尝试的产物路径或关键数字。
        tags: 标签，逗号分隔，便于以后按标签查。
    """
    valid = {"effective", "ineffective", "partial", "unknown"}
    outcome = (outcome or "").strip().lower()
    if outcome not in valid:
        return jdump({"error": f"outcome 必须是 {sorted(valid)} 之一，收到 {outcome!r}"})
    if not (asked or "").strip():
        return jdump({"error": "asked 不能为空 —— 没有问题的经验以后检索不到。"})

    s = kb()
    keys = EXP.split_list(item_keys)
    # ⚠ 写入与权重计算只有一份实现：offline/experience.py。
    #   原来这里内联了 INSERT + _bump_weights，而 kb_admin.py 里又抄了一份
    #   回滚 SQL —— 三处漂移过一次（"经验说无效、权重还挂着 +1"）。
    try:
        exp_id = EXP.add_experience(
            s, asked=asked, outcome=outcome, method=method, item_keys=keys,
            context=context, reason=reason, evidence=evidence, tags=tags,
            source="dsh", session=os.environ.get("DSH_SESSION_ID", ""),
        )
    except ValueError as exc:
        return jdump({"error": str(exc)})

    return jdump({
        "ok": True,
        "id": exp_id,
        "outcome": outcome,
        "items": keys,
        "weights_after": {k: round(s.weight_of(k), 3) for k in keys},
        "note": "已记入经验层。下次检索该主题时这条经验会一并返回，相关文献排序也会上移。",
    })


# 经验行的字段契约：kb_experience_query 的组装器要读这些列。
# ⚠ 历史坑：searcher 的查询曾「显式列举列名时漏掉 evidence / item_keys」，
#   而写入端是好的 —— 表现为字段恒为 null 或空数组，没有任何报错。
#   新增或修改经验类 SELECT 时，请对照本清单核对。
EXPERIENCE_ROW_FIELDS = (
    "id", "created_at",
    "asked", "context", "method", "outcome", "reason",
    "evidence", "item_keys", "tags", "source",
)


@mcp.tool(description="查历史经验：某个问题 / 某篇文献 / 某个标签下，以前试过什么、效果如何。")
@safe
def kb_experience_query(query: str = "", item_key: str = "", tag: str = "",
                        limit: int = 10) -> str:
    """检索经验层。

    Args:
        query: 自由词，会在问题、方法、原因、标签里找。
        item_key: 只看与这篇文献相关的经验。
        tag: 按标签过滤。
        limit: 返回条数上限。
    """
    s = kb()
    limit = max(1, min(int(limit or 10), 50))
    if item_key:
        rows = s.experience_for_item(item_key, limit=limit)
    elif query:
        rows = s.related_experience(query, limit=limit)
    else:
        sql = "SELECT * FROM experience"
        params: list = []
        if tag:
            sql += " WHERE tags LIKE ?"
            params.append(f"%{tag}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in s.read(sql, tuple(params))]
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or "")]

    # 契约：下面组装器要读的字段，供上面的行来源与本文件的字段清单保持同步。
    # 测试 tests/test_experience_fields.py 会断言各来源都返回这些字段。
    required = set(EXPERIENCE_ROW_FIELDS)
    for r in rows:
        r.setdefault("item_keys", "[]")   # 缺列时给空列表，避免组装器直接崩
    missing = [{k for k in required if k not in r} for r in rows]
    if any(missing):
        return jdump({
            "error": "经验行缺少组装器所需字段",
            "missing": sorted({k for m in missing for k in m}),
            "hint": "多为查询 SELECT 漏列所致；对照 EXPERIENCE_ROW_FIELDS 核对 searcher 的 SELECT。",
        })

    return jdump({
        "count": len(rows),
        "experience": [
            {
                "id": r["id"],
                "when": (r.get("created_at") or "")[:19],
                "asked": r.get("asked"),
                "context": r.get("context"),
                "method": r.get("method"),
                "outcome": r.get("outcome"),
                "reason": r.get("reason"),
                "evidence": r.get("evidence"),
                "items": json.loads(r.get("item_keys") or "[]"),
                "tags": json.loads(r.get("tags") or "[]"),
                "source": r.get("source"),
            }
            for r in rows
        ],
        **({"hint": "还没有经验记录。用 kb_experience_add 记第一条，之后检索会自动带上。"}
           if not rows else {}),
    })


@mcp.tool(description=(
    "调整一篇文献的检索权重：标重点（pinned）、加减分（manual）、写备注。"
    "标重点的文献在检索时会明显上移。"
))
@safe
def kb_weight_set(key: str, pinned: bool | None = None, manual: float | None = None,
                  note: str = "") -> str:
    """设置文献权重。

    Args:
        key: 8 位 Zotero key。
        pinned: True 标为重点；False 取消重点；不传则不变。
        manual: 人工加减分（可为负）；不传则不变。
        note: 为什么标重点，便于以后回看。
    """
    s = kb()
    if not s.get_item(key):
        return jdump({"error": f"知识库里没有 key={key} 的条目。"})
    info = EXP.set_weight(s, key, pinned=pinned, manual=manual, note=note)
    return jdump({"ok": True, "key": key, "pinned": info["pinned"],
                  "manual": info["manual"], "note": info["note"],
                  "weight": round(s.weight_of(key), 3)})


# ---------------------------------------------------------------- 工具：维护


@mcp.tool(description=(
    "补抽知识库：对指定条目（或全部）重新读取 Zotero 并更新索引。"
    "刚在 Zotero 里导入的新文献用这个立刻可用，不必等离线管道重跑。"
))
@safe
def kb_reindex(key: str = "", scope: str = "changed") -> str:
    """增量补抽。

    Args:
        key: 只补这一篇（Zotero key）。留空则按 scope 处理。
        scope: changed（只处理变化过的，默认）/ all（全量，慢）。
    """
    import importlib
    import subprocess

    args = [sys.executable, os.path.join(ROOT, "offline", "convert.py")]
    keys = [k.strip() for k in str(key or "").split(",") if k.strip()]
    if keys:
        # ⚠ 这里原来**丢掉了 key 参数**：不管传没传 key，都跑一遍增量管线，
        #   于是文档里承诺的"只补这一篇"其实做不到（对外表现是"怎么这么慢"）。
        #   convert.py 的 --item 会跳过增量比对、只重建这几条，实测约 1.8s/篇。
        for k in keys:
            args += ["--item", k]
    elif scope == "all":
        args.append("--full")
    args.append("--no-vectors")   # 交互式补抽不阻塞在向量计算上
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              timeout=300, cwd=ROOT)
    except Exception as exc:  # noqa: BLE001
        return jdump({"error": f"补抽失败：{type(exc).__name__}: {exc}"})
    global _searcher
    if _searcher is not None:
        _searcher.close()
        _searcher = None
    tail = (proc.stdout or "").strip().split("\n")[-8:]
    return jdump({
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "scope": ("items:" + ",".join(keys)) if keys else scope,
        "output_tail": tail,
        "note": "补抽完成后检索立即可用（向量需离线管道补建）。",
    })


# ---------------------------------------------------------------- 工具：获取新文献


@mcp.tool(description=(
    "把 DSH 搜出来、**用户已经勾选确认**的文献交给 Zotero 自己抓进库里。"
    "只用 DOI 列表调用；抓取在 Zotero 进程里完成，所以**用户机器当下的网络身份"
    "（校园网出口 IP、机构订阅）天然生效**，元数据走 Zotero 的翻译器、"
    "PDF 走 Zotero 自带的「查找可用的 PDF」；**落库后还会走一遍本地模型的"
    "分类/打标签建议**（Zotero 里一次问一批，这是正常流程不是错误）。"
    "典型流程：web_search 找候选 → 核对 DOI（标题对不上就扔掉）→ 列候选表"
    "（标题做成 https://doi.org/... 链接，用户能点开看原文）→ "
    "ask_user_question(multi_select=True) 让用户勾选 → 本工具只传勾中的那几条。"
    "⚠ 用户勾完就抓，**不要再问第二次**；⚠ 不要替用户决定下哪些；"
    "⚠ 不做付费墙绕过（改 URL 骗计费、影子图书馆、盗用凭证一律不做），"
    "用用户已有的权限可以。"
    "**抓 3 篇以上、或想让用户看到实时进度时用 wait=False**："
    "立刻返回 job_id，之后每隔几秒调 kb_acquire_progress 把进度念给用户听。"
))
@safe
def kb_acquire(dois: str, collection_id: int = 0, find_pdf: bool = True,
               dry_run: bool = False, reindex: bool = True,
               wait: bool = True) -> str:
    """把 DOI 清单交给 Zotero 抓取入库。

    Args:
        dois: DOI 列表，逗号或换行分隔。整条 https://doi.org/... 也认。
        collection_id: 落到哪个 Zotero 分类（collectionID）；0 = 用用户在 Zotero
            界面里**当前选中**的分类（与浏览器连接器行为一致）。
        find_pdf: 是否顺带找可用的 PDF（默认是）。
        dry_run: 只解析元数据、**不落库**。想先确认"抓不抓得到"时用。
        reindex: 入库后自动补抽知识库，使新文献立刻可被 kb_search 搜到
            （默认是；**只在 wait=True 时生效**）。
        wait: True（默认）= 阻塞到抓完再返回，适合 1~2 篇。
            False = 立刻返回 job_id，之后用 `kb_acquire_progress` 查进度，
            适合 3 篇以上、或想让用户看见实时进度时。
    """
    import acquire as AQ

    items = [x for x in re.split(r"[,\n;；、]+", str(dois or "")) if x.strip()]
    if not items:
        return jdump({"error": "dois 是空的。请把用户挑中的 DOI 传进来。"})

    # ---- 异步：立刻返回，让 DSH 能边跑边把进度念给用户听
    if not wait:
        try:
            start = AQ.acquire_async(items, collection_id=collection_id,
                                     find_pdf=find_pdf, dry_run=dry_run)
        except AQ.AcquireError as exc:
            return jdump({"error": str(exc), "how_to_fix": exc.how_to_fix})
        lines = [f"已开始抓取 **{start['total']}** 篇"
                 f"（job_id=`{start['job_id']}`）", ""]
        for i, p in enumerate(start.get("plan") or [], 1):
            lines.append(f"{i}. {p.get('title') or '（标题待 Zotero 解析）'}"
                         f"  `{p['doi']}`")
        lines.append("")
        lines.append(f"接下来每隔 3~6 秒调一次 `kb_acquire_progress`"
                     f"（job_id=\"{start['job_id']}\"），把返回的 progress_line "
                     "念给用户 —— 让他看得见跑到第几篇了。")
        return "\n".join(lines)

    try:
        r = AQ.acquire(items, collection_id=collection_id, find_pdf=find_pdf,
                       dry_run=dry_run)
    except AQ.AcquireError as exc:
        return jdump({"error": str(exc), "how_to_fix": exc.how_to_fix})

    text = AQ.format_report(r)

    keys = [x.get("itemKey") for x in (r.get("results") or []) if x.get("itemKey")]
    if keys and reindex and not dry_run:
        # 一次进程重建这几条（convert.py 的 --item 可重复），比逐篇调快得多
        res = kb_reindex(key=",".join(keys))
        text += "\n\n**知识库补抽**：" + str(res)[:600]
    elif keys and not dry_run:
        text += AQ.reindex_hint(keys)

    return text


@mcp.tool(description=(
    "给一批 DOI 补上「能判断这篇值不值得看」的信息：**摘要、关键词、主题、开放获取状态、"
    "被引数、期刊**。用于 kb_acquire 之前 —— 只报标题+作者+年份，用户没法判断合不合适。"
    "典型用法：搜到候选、核过 DOI 后，把 DOI 一次传进来，再据此列候选表。"
    "数据源 OpenAlex 为主、Crossref / Semantic Scholar 兜底。"
    "⚠ **没有摘要的会如实标 `abstract_source=none`**（不少期刊不存摘要）—— "
    "这时别自己编一段，表里写「该刊未提供摘要」，用关键词补足判断依据。"
))
@safe
def kb_paper_info(dois: str, with_abstract: bool = True) -> str:
    """查一批 DOI 的摘要 / 关键词 / OA / 被引。

    Args:
        dois: DOI 列表，逗号或换行分隔；整条 https://doi.org/... 也认。
        with_abstract: 是否抓摘要（默认是）。关掉只是不填 abstract，
            关键词/主题/OA/被引仍然会给。
    """
    import papermeta as PM

    items = [x for x in re.split(r"[,\n;；、]+", str(dois or "")) if x.strip()]
    if not items:
        return jdump({"error": "dois 是空的"})

    rows = PM.enrich(items, with_abstract=with_abstract)
    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    return jdump({
        "returned": len(rows),
        "ok_count": len(ok),
        "items": rows,
        "failed": bad or None,
        "note": "items 与传入的 DOI **等长同序**，按下标就能对上候选表。",
    })


@mcp.tool(description=(
    "查一次 `kb_acquire(wait=False)` 的抓取进度。返回 progress_line（第几篇/共几篇、"
    "当前在处理哪一篇）以及已完成条目的明细 —— **把它念给用户**，"
    "让他看得见是在跑还是卡住了。"
    "**每 3~6 秒调一次**；`finished=true` 时停止轮询，此时返回里带 `format`"
    "（完整结果报告，直接呈现）与 `keys`；若 `need_reindex` 为真，"
    "再调 `kb_reindex(key=\"k1,k2\")` 让新文献立刻可搜。"
    "job_id 留空 = 查最近一次作业。"
))
@safe
def kb_acquire_progress(job_id: str = "") -> str:
    """查看抓取作业的进度。

    Args:
        job_id: `kb_acquire(wait=False)` 返回的 id；留空取最近一次作业。
    """
    import acquire as AQ

    j = AQ.job_progress(job_id)
    if not j:
        return jdump({"error": "没有这个作业（或者进程重启过，作业记录已丢）。",
                      "how_to_fix": ["重新调 kb_acquire 派发一次。"]})

    state = j.get("state")
    out = {
        "job_id": j.get("job_id"),
        "state": state,
        "finished": state in ("done", "failed"),
        "progress_line": j.get("summary") or "",
        "index": j.get("index"),
        "total": j.get("total"),
        "current_doi": j.get("current_doi") or "",
        "current_title": j.get("current_title") or "",
        "age_s": j.get("age_s"),
        "added": j.get("added", 0),
        "with_pdf": j.get("with_pdf", 0),
    }
    # 已经出结果的那些条目：边跑边报，用户不用等到最后才知道哪篇好了
    done = list(j.get("results") or [])
    if done:
        out["done_items"] = [
            {"title": r.get("title"), "itemKey": r.get("itemKey"),
             "pdf": ("有" if (r.get("pdf") or {}).get("ok") else "没有")}
            for r in done
        ]
    if j.get("failed"):
        out["failed"] = [{"doi": f.get("doi"), "why": f.get("why")}
                         for f in j["failed"]]
    if j.get("duplicates"):
        out["duplicates"] = [{"doi": d.get("doi"), "key": d.get("key")}
                             for d in j["duplicates"]]
    if state == "done":
        # 跑完就把完整报告一起给出去，省得模型再拼一次
        out["format"] = AQ.format_report(j)
        out["keys"] = [r.get("itemKey") for r in done if r.get("itemKey")]
        out["need_reindex"] = bool(out["keys"]) and not j.get("dry_run")
        out["next_step"] = (
            f"调 kb_reindex(key=\"{','.join(out['keys'])}\") 让新文献可搜"
            if out["need_reindex"] else "无需补抽")
    if j.get("error"):
        out["error"] = j["error"]
    return jdump(out)


@mcp.tool(description=(
    "把一个**已经下载到本机、并且确认是 PDF** 的文件，挂到 Zotero 里某个条目上成为附件。"
    "用于兜底路径：kb_acquire 建好了条目但 Zotero 自己没找到 PDF，"
    "而 DSH 又找到了一个直链并下载成功时 —— 用这个把它挂上去，"
    "不要让用户手工拖进 Zotero。"
    "⚠ 文件必须已经过 PDF 文件头校验（tools/fetch-pdf.ps1 会做）；"
    "挂一个网页当 PDF 附件比不挂更糟。"
))
@safe
def kb_attach(item_key: str, path: str, title: str = "") -> str:
    """把本地 PDF 挂到条目的附件里。

    Args:
        item_key: 目标条目的 Zotero key（kb_acquire 的结果里有）。
        path: 本机 PDF 的完整路径。
        title: 附件标题；留空由 Zotero 按文件名生成。
    """
    import acquire as AQ

    try:
        r = AQ.attach_files([{"itemKey": item_key, "path": path, "title": title}])
    except AQ.AcquireError as exc:
        return jdump({"error": str(exc), "how_to_fix": exc.how_to_fix})
    return AQ.format_attach_report(r)


@mcp.tool(description="知识库状态：条目/切片/向量规模、构建时间、经验条数、各后端是否就绪。")
@safe
def kb_stats() -> str:
    """查看知识库状态。"""
    s = kb()
    stats = s.stats()
    # 结构化字段（extra → item_extra）的覆盖情况：让"库里有没有这些数据"
    # 一眼可见 —— 它决定 kb_search 的 source_kind / publication_tag / clc
    # 这三个筛选参数有没有东西可筛。
    stats["structured_extra"] = s.extra_stats()
    stats["kb_dir"] = S.KB_DIR
    stats["manifest"] = S.MANIFEST if os.path.exists(S.MANIFEST) else "(尚未生成)"
    stats["collections"] = s.collections()
    stats["weight_formula"] = (
        "权重 = 1 + 3*重点 + 人工分 + 2*有效 + 1*部分 + 0.5*无效；"
        "最终分 = 融合分 * (1 + ln(权重))"
    )
    return jdump(stats)


def _bump_weights(s: Searcher, keys: list[str], outcome: str) -> None:
    """把一次尝试计入文献的累积战绩。

    ⚠ 实现已抽到 `offline/experience.py:apply_weight_delta` —— "加分"与
      "回滚"必须是同一份算术，否则会出现"经验删了、权重还挂着"的脏状态。
      保留这个薄壳是为了不惊动已有调用点与测试。
    """
    EXP.apply_weight_delta(s, keys, outcome, +1)


def re_split(text: str) -> list[str]:
    """把逗号/空格/分号分隔的字符串拆成列表。

    实现只有一份：`offline/experience.py:split_list`（插件端点、面板、CLI
    都要按同一套规则拆 key/tags，规则漂了就会出现"同一个标签被当成两个"）。
    """
    return EXP.split_list(text)


# ---------------------------------------------------------------- 资源


@mcp.resource("zotero-kb://collections",
              name="知识库分类清单",
              description="所有分类及各自条目数，用于限定检索范围。",
              mime_type="application/json")
def res_collections() -> str:
    return jdump({"collections": kb().collections()})


@mcp.resource("zotero-kb://item/tldr/{key}",
              name="文献要点",
              description="一篇文献的摘要级要点：元数据、摘要、笔记要点、经验权重。",
              mime_type="text/markdown")
def res_item_tldr(key: str) -> str:
    """摘要级资源：模型用它判断"要不要深读"，几百 token 就能覆盖一篇。

    ⚠ 渲染实现**搬到 offline/kbviews.py 了**（`render("tldr", …)`）。
    原因：面板的「打开知识库」和 Zotero 右键菜单也要这一份内容，各写一套
    迟早会漂移 —— 本项目已经为"两份实现"吃过亏。搬的时候是逐字搬的，
    输出与原来完全一致（tests/test_mcp.py 与 tests/test_kbviews.py 都盯着）。
    """
    return KBV.render("tldr", key, kb())


@mcp.resource("zotero-kb://item/{key}",
              name="文献完整档案",
              description="元数据 + 摘要 + 笔记 + 高亮标注 + 使用经验。",
              mime_type="text/markdown")
def res_item(key: str) -> str:
    path = os.path.join(S.PAPERS_DIR, f"{key}.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    return f"# 未找到 {key}\n\n知识库里没有这个条目的档案。用 kb_search 先检索。"


@mcp.resource("zotero-kb://item/full/{key}",
              name="文献正文",
              description="带页码锚点的正文 Markdown。篇幅大，按需读取。",
              mime_type="text/markdown")
def res_item_full(key: str) -> str:
    path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    return (f"# 未找到 {key} 的正文\n\n"
            "该条目可能没有 PDF 附件，或 PDF 没有文字层。可用 kb_item 看摘要与笔记。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    t0 = time.time()
    print(f"[zotero-kb] 启动中（KB={S.KB_DIR}）", file=sys.stderr)
    try:
        stats = Searcher().stats()
        print(f"[zotero-kb] 索引就绪：{stats['items']} 条目 / {stats['chunks']} 切片 / "
              f"{stats['vectors']} 向量；经验 {stats['experience_rows']} 条", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[zotero-kb] 索引检查失败（服务仍启动）：{exc}", file=sys.stderr)
    print(f"[zotero-kb] 初始化完成，用时 {time.time() - t0:.2f}s", file=sys.stderr)
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
