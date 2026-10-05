"""digest.py —— **分节纲要**：全文与摘要之间的"中间层"。

## 为什么要有它（用户 2026-10-05 的原话）

「还是知识库层级的问题，全文太长，现有的摘要还太短，怎么加中间的一层级」

现在的三级确实断层太大：

| 层 | 体量 | 够不够用 |
|---|---|---|
| `views/<KEY>.tldr.md`（摘要级） | 几百字 | **太短**：知道"讲什么"，但不知道"有哪几节、每节做了什么" |
| `views/<KEY>.outline.md`（**本模块，分节纲要**） | 论文 1~2k 字、学位论文 4~12k 字 | 中间层：按章节给"这一节在做什么 + 关键点/参数/结论" |
| `fulltext/<KEY>.md`（全文） | 论文 1~2 万字、学位论文 10~20 万字 | **太长**：整篇塞进上下文不现实 |

用法（**不引入任何新的排序机制**，用户已明确"检索分层算了吧"）：
模型/人先读纲要 → 看到"第 3 节 方法（p.5–p.9）" → 需要细看就用 `kb_fulltext`
把那几页取回来。纲要里每一节都带**页码范围**，就是为了这一步。

## 切节：从 MinerU 的按页结构里切（不是从 md 里猜）

`<kb>/mineru/<KEY>/structured_content.json` 的 `pages[].blocks[]` 里，
MinerU 已经标好了 `doc_title` / `paragraph_title`（章节标题）——
所以切节用**它**，比"从 markdown 里找标题行"可靠得多。

三种兜底：
  · 完全没有标题块 → 按字符窗口切（每 ~4000 字一节，标题写「第 N 段」）；
  · 太短的节（< 400 字）→ 并进下一节（避免几十个"参考文献"一句话节）；
  · 太长的节（> 9000 字）→ 按段落再切，标题加「（续 N）」。

## 增量：按**节指纹**缓存

节的指纹 = `sha1(标题 + 节内正文[:2000])[:6]`。正文没变的节直接复用上次的摘要
（写在 `meta` 表的 `ai_outline:<KEY>` 里）—— 一篇 90 页的学位论文有 20~40 节，
全量重跑要十几分钟，改一章只重跑那一章才有实用价值。

## 落盘（与其它层的关系）

  · `meta.ai_outline:<KEY>`  —— 结构化 JSON（供面板/MCP/增量）
  · `views/<KEY>.outline.md` —— 给人读的纲要（kbviews 的 `outline` 级别）

⚠ **刻意不入索引**：纲要切片与正文切片混在一路排序里，会让"检索结果"变成
"纲要摘抄"，而用户当前阶段要的是"能按需读中间层"，不是"改排序"。等哪天要
把纲要也做成可检索内容，再单独讨论（那才是真正的分层检索）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import prompts as PR  # noqa: E402
import schemas as S  # noqa: E402

META_PREFIX = "ai_outline:"
MIN_SECTION_CHARS = 120        # 比这短才算「碎片」，并进上一节（见下面的注释）
MAX_SECTION_CHARS = 9000       # 比这长就再切（按段落）
WINDOW_CHARS = 4000            # 没有任何标题时的兜底窗口
SECTION_INPUT_CHARS = 5000     # 每节最多喂给模型多少字
MAX_SECTIONS = 24              # 每篇最多这么多个节（超了就合并相邻节）

# 这些节在纲要里不展开（参考文献动辄 3000~10000 字，问模型纯属浪费）：
# 保留一行占位让读者知道"它在原文里有"，但**不调模型**。
SKIP_TITLE_RE = re.compile(
    r"(参考文献|references?|致\s*谢|acknowledg|附录|appendix|"
    r"作者简介|攻读学位期间|发表论文|index)", re.I)


# ================================================================ 切节（纯函数）

def _blocks(structured: dict) -> list[tuple[int, dict]]:
    """摊平成 `[(页码 1-based, 块), …]`，顺手丢掉页眉页脚页码块。"""
    import mineru as M
    out: list[tuple[int, dict]] = []
    for i, pg in enumerate((structured or {}).get("pages") or []):
        if not isinstance(pg, dict):
            continue
        page = i + 1
        for b in (pg.get("blocks") or []):
            if M.block_type(b) in M.DROP_BLOCK_TYPES:
                continue
            out.append((page, b))
    return out


def sectionize(structured: dict) -> list[dict]:
    """把 MinerU 的按页结构切成"节"。

    返回 `[{title, level, page_from, page_to, text, chars, fp}, …]`（顺序=原文顺序）。
    `title` 一定是非空字符串（兜底会写「第 N 段」/「正文」）。
    """
    import mineru as M
    items = _blocks(structured)
    secs: list[dict] = []
    cur = {"title": "", "level": 0, "page_from": 0, "page_to": 0,
           "parts": []}

    def flush():
        nonlocal cur
        if cur["parts"]:
            text = "\n\n".join(p for p in cur["parts"] if p)
            if text.strip():
                secs.append({"title": cur["title"], "level": cur["level"],
                             "page_from": cur["page_from"],
                             "page_to": cur["page_to"], "text": text})
        cur = {"title": "", "level": 0, "page_from": 0, "page_to": 0, "parts": []}

    for page, b in items:
        typ = M.block_type(b)
        if typ in ("doc_title", "title", "paragraph_title", "section_title",
                   "heading"):
            # 标题块：先把上一节收尾，再开新节
            flush()
            title = str(b.get("content") or "").strip()
            try:
                lvl = int(b.get("level") or 1)
            except Exception:      # noqa: BLE001
                lvl = 1
            cur["title"] = title
            cur["level"] = lvl
            cur["page_from"] = page
            cur["page_to"] = page
            continue
        txt = M.render_block(b)
        if not txt.strip():
            continue
        if not cur["page_from"]:
            cur["page_from"] = page
        cur["page_to"] = page
        cur["parts"].append(txt)
    flush()

    # 一篇里一个**带标题**的节都没有 → 按窗口切。
    #
    # ⚠ 判据不能只写 `if not secs`：正文开头（第一个标题之前）总会攒出一个
    #   空标题的节，于是"没有标题"这件事被它掩盖了 —— 本机实测 6 页 9010 字的
    #   无标题文档因此只切出 1 节（本该按 4000 字窗口切 3 节）。
    if not secs or not any((s.get("title") or "").strip() for s in secs):
        return _windowize(items)

    # 碎片（极短、通常是标题下只跟了一两行的残片）并进**上一节**。
    #
    # ⚠ 这里的阈值刻意定得很小（120 字）而不是"短于 400 字就并"：
    #   本机实测过一版 400 字阈值，结果「1 引言」（363 字）被吃进了上一节，
    #   纲要里那一节就消失了 —— 纲要的价值恰恰在**保住原文的节结构**。
    #   真正的防膨胀交给下面的 MAX_SECTIONS 合并（那是安全阀，不是常规路径）。
    # 开头的碎片没有"上一节"可并 → 先并进下一节（见下）
    if (len(secs) > 1 and len(secs[0]["text"]) < MIN_SECTION_CHARS
            and not (secs[0].get("title") or "").strip()):
        secs[1] = dict(secs[1],
                       text=secs[0]["text"] + "\n\n" + secs[1]["text"],
                       page_from=min(secs[0]["page_from"], secs[1]["page_from"]))
        secs = secs[1:]
    merged: list[dict] = []
    for s in secs:
        if (merged and len(s["text"]) < MIN_SECTION_CHARS
                and len(merged[-1]["text"]) + len(s["text"]) < MAX_SECTION_CHARS):
            prev = merged[-1]
            prev["text"] = prev["text"] + "\n\n" + s["text"]
            prev["page_to"] = max(prev["page_to"], s["page_to"])
            continue
        merged.append(dict(s))
    # 太长的节再切（按段落，标题加「（续 N）」）
    out: list[dict] = []
    for s in merged:
        out += _split_long(s)
    # 节数上限：超了就两两合并相邻节（保住页码范围与篇幅）
    while len(out) > MAX_SECTIONS:
        new: list[dict] = []
        i = 0
        while i < len(out):
            if i + 1 < len(out) and len(out[i]["text"]) + len(out[i + 1]["text"]) \
                    < MAX_SECTION_CHARS:
                a, b = out[i], out[i + 1]
                new.append({"title": a["title"] or b["title"],
                            "level": min(a["level"] or 9, b["level"] or 9),
                            "page_from": min(a["page_from"], b["page_from"]),
                            "page_to": max(a["page_to"], b["page_to"]),
                            "text": a["text"] + "\n\n" + b["text"]})
                i += 2
            else:
                new.append(out[i])
                i += 1
        if len(new) == len(out):
            break                      # 合不动了（每节都很大），就此打住
        out = new
    for n, s in enumerate(out, 1):
        s["title"] = s["title"] or f"第 {n} 段"
        s["index"] = n
        s["chars"] = len(s["text"])
        s["skip"] = bool(SKIP_TITLE_RE.search(s["title"]))
        s["fp"] = section_fp(s)
    return out


def _windowize(items: list[tuple[int, dict]]) -> list[dict]:
    """没有任何标题时的兜底：按字符窗口切。"""
    import mineru as M
    secs: list[dict] = []
    buf: list[str] = []
    page_from = page_to = 0
    n = 0

    def flush():
        nonlocal buf, page_from, page_to, n
        text = "\n\n".join(buf).strip()
        if text:
            n += 1
            secs.append({"title": f"第 {n} 段", "level": 9,
                         "page_from": page_from, "page_to": page_to,
                         "text": text})
        buf = []
        page_from = page_to = 0

    for page, b in items:
        txt = M.render_block(b)
        if not txt.strip():
            continue
        if not page_from:
            page_from = page
        page_to = page
        buf.append(txt)
        if sum(len(x) for x in buf) >= WINDOW_CHARS:
            flush()
    flush()
    return secs


def _split_long(s: dict) -> list[dict]:
    """一节太长 → 按段落（空行）切成若干"（续 N）"节。"""
    if len(s["text"]) <= MAX_SECTION_CHARS:
        return [s]
    paras = [p for p in re.split(r"\n\s*\n", s["text"]) if p.strip()]
    parts: list[list[str]] = [[]]
    for p in paras:
        if sum(len(x) for x in parts[-1]) + len(p) > MAX_SECTION_CHARS and parts[-1]:
            parts.append([])
        parts[-1].append(p)
    out = []
    for i, chunk in enumerate(parts, 1):
        title = s["title"] if i == 1 else f"{s['title']}（续 {i}）"
        out.append({"title": title, "level": s["level"],
                    "page_from": s["page_from"], "page_to": s["page_to"],
                    "text": "\n\n".join(chunk)})
    return out


def section_fp(s: dict) -> str:
    """节的指纹（标题 + 正文前 2000 字）—— 增量重跑用。"""
    raw = (str(s.get("title") or "") + "|" + str(s.get("text") or "")[:2000])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


# ================================================================ 读缓存 / 生成

def load_outline(key: str, s=None) -> dict:
    """读 `<kb>` 里这一篇的纲要（JSON）。没有就返回 `{}`。"""
    try:
        s = s or S.connect(S.INDEX_DB)
        row = s.execute("SELECT v FROM meta WHERE k = ?",
                        (META_PREFIX + key,)).fetchone()
        if not row:
            return {}
        raw = row["v"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:      # noqa: BLE001
        return {}


def save_outline(key: str, data: dict, s=None) -> None:
    """写纲要（覆盖写：整篇一起写，避免半截状态）。"""
    own = s is None
    s = s or S.connect(S.INDEX_DB)
    try:
        s.execute("INSERT INTO meta(k, v) VALUES(?, ?) "
                  "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                  (META_PREFIX + key, json.dumps(data, ensure_ascii=False)))
        s.commit()
    finally:
        if own:
            s.close()


def _ask_section(title: str, sec: dict, model: str = "") -> dict:
    """让模型给一节写 2~4 句 + 关键点。任何异常都返回 `{}`（调用方跳过这节）。"""
    try:
        import judge
    except Exception:      # noqa: BLE001
        return {}
    text = str(sec.get("text") or "")[:SECTION_INPUT_CHARS]
    if len(text.strip()) < 80:
        return {}
    prompt = PR.render("digest", title=title, section=sec.get("title") or "",
                       text=text)
    try:
        res = judge.generate(prompt, system=PR.get("digest", "system"),
                             model=model, json_mode=True, temperature=0.0)
    except Exception:      # noqa: BLE001
        return {}
    if not isinstance(res, dict) or not res.get("ok"):
        return {}
    try:
        good, data = judge.parse_json(res.get("text") or "")
    except Exception:      # noqa: BLE001
        return {}
    if not good or not isinstance(data, dict):
        return {}
    summary = str(data.get("summary") or "").strip()
    points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
    if not summary and not points:
        return {}
    return {"summary": summary[:600], "points": points[:6],
            "model": res.get("model") or ""}


def build(key: str, kb_dir: str = "", force: bool = False, model: str = "",
          limit_sections: int = 0, log=None) -> dict:
    """给一篇生成（或增量更新）分节纲要。

    返回 `{ok, key, n_sections, cached, asked, seconds, why, outline}`；
    任何情况都返回 dict（不抛）。`limit_sections>0` 时只跑前 N 节（先看效果用）。
    """
    import time
    t0 = time.time()

    def note(msg):
        if log:
            try:
                log(msg)
            except Exception:      # noqa: BLE001
                pass

    import mineru as M
    kb_dir = kb_dir or M._kb_dir()
    adir = M.artifact_dir(key, kb_dir)
    struct_path = os.path.join(adir, "structured_content.json")
    out = {"ok": False, "key": key, "n_sections": 0, "cached": 0, "asked": 0,
           "seconds": 0.0, "why": "", "outline": {}}
    if not os.path.isfile(struct_path):
        out["why"] = ("没有 MinerU 产物（先解析这一篇：面板「PDF 解析」页或 "
                      "tools\\kb_admin.py mineru parse --key " + key + "）")
        return out
    try:
        with io.open(struct_path, encoding="utf-8") as fh:
            structured = json.load(fh)
    except Exception as exc:      # noqa: BLE001
        out["why"] = f"读 structured_content.json 失败：{exc}"
        return out

    secs = sectionize(structured)
    if not secs:
        out["why"] = "这篇没有可切的正文（MinerU 产物里没有文本块？）"
        return out
    old = load_outline(key)
    old_by_fp = {s.get("fp"): s for s in (old.get("sections") or [])
                 if isinstance(s, dict)}
    title = ""
    try:
        row = S.connect(S.INDEX_DB).execute(
            "SELECT title FROM items WHERE key = ?", (key,)).fetchone()
        title = (row["title"] if row else "") or ""
    except Exception:      # noqa: BLE001
        title = ""

    kept: list[dict] = []
    if limit_sections:
        secs = secs[:limit_sections]
    for i, sec in enumerate(secs, 1):
        if sec.get("skip"):
            kept.append(dict(sec, summary="（这一节不展开：参考文献/致谢/附录类）",
                             points=[], model="", cached=True, tried=True))
            out["cached"] += 1
            continue
        prev = old_by_fp.get(sec["fp"])
        # ⚠ 判据里必须带 `tried`：有些节本来就产出不了要点（正文过短、
        #   或模型这次没给结果）。只认"有 summary"的话，这些节**每次构建都会被
        #   重问一遍** —— 本机实测：第二次构建 4 节里仍有 1 节被重问。
        if prev and not force and (prev.get("tried")
                                   or prev.get("summary") or prev.get("points")):
            kept.append(dict(sec, summary=prev.get("summary") or "",
                             points=prev.get("points") or [],
                             model=prev.get("model") or "", cached=True,
                             tried=True))
            out["cached"] += 1
            continue
        got = _ask_section(title, sec, model=model)
        out["asked"] += 1
        kept.append(dict(sec, summary=got.get("summary") or "",
                         points=got.get("points") or [],
                         model=got.get("model") or "", cached=False,
                         tried=True))
        note(f"    [{i}/{len(secs)}] {sec['title'][:40]} → "
             f"{'ok' if got else '（模型没给结果，留空）'}")

    data = {"ok": True, "key": key, "title": title,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sections": kept,
            "n_sections": len(kept)}
    if not limit_sections:
        save_outline(key, data)
        # 顺手把「打开知识库 → 分节纲要」那一份也写出来：用户的动作是
        # "点生成 → 打开看"，中间不该再要求他跑一次 maintain.py views。
        try:
            vpath = os.path.join(S.VIEWS_DIR, f"{key}.outline.md")
            os.makedirs(S.VIEWS_DIR, exist_ok=True)
            with io.open(vpath, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render(key, data))
            try:
                import mdhtml
                mdhtml.write_html(vpath)
            except Exception:      # noqa: BLE001
                pass
        except Exception as exc:      # noqa: BLE001
            note(f"    [!!] 写 views/{key}.outline.md 失败：{exc}")
        # 顺手把"节标题 + 一句话"写进 papers/<KEY>.md（没笔记时档案才不笼统）
        try:
            sync_card(key, data)
        except Exception as exc:      # noqa: BLE001
            note(f"    [!!] 同步 papers/{key}.md 的纲要概览失败：{exc}")
    out.update({"ok": True, "n_sections": len(kept),
                "seconds": round(time.time() - t0, 1), "outline": data,
                "why": f"{len(kept)} 节（新跑 {out['asked']}、复用 {out['cached']}）"})
    return out


# ================================================================ 渲染（人读）

def render(key: str, data: dict | None = None) -> str:
    """把纲要渲染成 markdown（kbviews 的 `outline` 级别用它）。"""
    data = data if data is not None else load_outline(key)
    if not data or not data.get("sections"):
        return ""
    lines = [f"# {data.get('title') or key} — 分节纲要", ""]
    lines.append("> 这是「中间层」：比摘要详细、比全文短。"
                 "想细看某一节，用 `kb_fulltext(page_from=起, page_to=止)` 取那几页。")
    lines.append("")
    for i, s in enumerate(data.get("sections") or [], 1):
        head = s.get("title") or f"第 {i} 段"
        rng = f"p.{s.get('page_from')}–{s.get('page_to')}"
        lines.append(f"## {i}. {head}（{rng}）")
        lines.append("")
        if s.get("summary"):
            lines.append(str(s["summary"]))
            lines.append("")
        for p in (s.get("points") or []):
            lines.append(f"- {p}")
        if s.get("points"):
            lines.append("")
    lines.append(f"> 共 {len(data.get('sections') or [])} 节；"
                 f"生成于 {data.get('at') or '?'}（模型可能出错，重要结论请回原文核对）")
    return "\n".join(lines)


OUTLINE_BEGIN = "<!-- OUTLINE:BEGIN -->"
OUTLINE_END = "<!-- OUTLINE:END -->"


def _one_liner(text: str, limit: int = 90) -> str:
    """把一句话压成一行（去掉换行、超长截断）—— 给档案里的概览用。"""
    s = " ".join(str(text or "").split())
    return (s[:limit] + "…") if len(s) > limit else s


def sync_card(key: str, data: dict | None = None) -> bool:
    """把纲要概览写进 `papers/<KEY>.md`（幂等：只替换两个标记之间的内容）。

    为什么要这一步：有笔记的文献，档案本身就能"浏览大致做了什么"；没有笔记的
    只有元数据 + 每页首段，太笼统。把每节标题 + 一句话放进档案，档案这一级才
    真的能当中间层用（用户 2026-10-05 指出的问题）。
    """
    data = data if data is not None else load_outline(key)
    secs = (data or {}).get("sections") or []
    path = os.path.join(S.PAPERS_DIR, f"{key}.md")
    if not os.path.isfile(path):
        return False
    try:
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    if not secs:
        block = ""
    else:
        lines = ["## 分节纲要（节标题 + 页码范围；详细要点见「打开知识库 → 分节纲要」）", ""]
        for i, s in enumerate(secs, 1):
            head = s.get("title") or f"第 {i} 段"
            rng = f"p.{s.get('page_from')}–{s.get('page_to')}"
            one = _one_liner(s.get("summary") or "")
            lines.append(f"- {i}. {head}（{rng}）" + (f"：{one}" if one else ""))
        lines.append("")
        block = OUTLINE_BEGIN + "\n" + "\n".join(lines) + OUTLINE_END + "\n"
    # 幂等：已有标记就只换标记之间；没有就追加到文件末尾
    i = text.find(OUTLINE_BEGIN)
    j = text.find(OUTLINE_END)
    if i >= 0 and j > i:
        new_text = text[:i] + block + text[j + len(OUTLINE_END) + 1:]
    elif block:
        new_text = text.rstrip("\n") + "\n\n" + block
    else:
        return False
    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    """`python offline/digest.py <KEY> [--force] [--limit N] [--missing] [--dry]`"""
    import argparse
    ap = argparse.ArgumentParser(description="分节纲要（全文与摘要之间的中间层）")
    ap.add_argument("key", nargs="?", default="")
    ap.add_argument("--force", action="store_true", help="忽略节的指纹，全部重跑")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 节（先看效果）")
    ap.add_argument("--model", default="")
    ap.add_argument("--missing", action="store_true", help="给还没有纲要的文献补")
    ap.add_argument("--dry", action="store_true", help="只打印会做哪些，不调模型")
    ap.add_argument("--sync", action="store_true",
                    help="不调模型：把**已有**纲要的概览回填进 papers/*.md（补历史用）")
    args = ap.parse_args(argv)

    if args.sync:
        # 补历史：纲要已经在库里，只是当时还没写"档案里的概览"这一步
        conn = S.connect(S.INDEX_DB)
        ks = [r["k"][len(META_PREFIX):] for r in conn.execute(
            "SELECT k FROM meta WHERE k LIKE ?", (META_PREFIX + "%",))]
        n = sum(1 for k in ks if sync_card(k))
        print(f"  已把纲要概览写进 {n} / {len(ks)} 篇的 papers/*.md")
        # 顺带补 .html（公式渲染成 MathML）：后台任务跑的时候还没有这一步
        try:
            import mdhtml
            vd = S.VIEWS_DIR
            m = sum(1 for k in ks if mdhtml.write_html(
                os.path.join(vd, f"{k}.outline.md")))
            print(f"  已补 {m} 个 views/*.outline.html（公式渲染）")
        except Exception as exc:      # noqa: BLE001
            print(f"  （补 html 失败：{exc}）")
        return 0

    keys = []
    if args.missing:
        conn = S.connect(S.INDEX_DB)
        have = {r["k"][len(META_PREFIX):] for r in conn.execute(
            "SELECT k FROM meta WHERE k LIKE ?", (META_PREFIX + "%",))}
        keys = [r["key"] for r in conn.execute("SELECT key FROM items ORDER BY key")
                if r["key"] not in have]
    elif args.key:
        keys = [args.key.strip()]
    if not keys:
        print("要给哪一篇？用法：python offline/digest.py <KEY>（或 --missing）")
        return 2

    import mineru as M
    for k in keys:
        secs = []
        p = os.path.join(M.artifact_dir(k), "structured_content.json")
        if os.path.isfile(p):
            secs = sectionize(json.load(io.open(p, encoding="utf-8")))
        if args.dry:
            print(f"  {k}: {len(secs)} 节")
            for s in secs[:12]:
                print(f"    - {s['title'][:50]}（p.{s['page_from']}–{s['page_to']}，"
                      f"{s['chars']} 字）")
            continue
        r = build(k, force=args.force, model=args.model,
                  limit_sections=args.limit,
                  log=lambda m: print(m, flush=True))
        print(f"  {k}: {'成功' if r['ok'] else '失败'}　{r['why']}"
              f"　{r['seconds']} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
