"""Zotero 右侧内容窗格里那个本地模型助手：注入上下文 → 问答 / 逐段检查 / 写入建议。

## 三块功能，一个原则

    build_context + chat      按需注入这篇的上下文再提问
    plan + check_paragraph    逐段核对正文提取质量（可中断、可续跑）
    propose + apply_plan      把讨论里"该记的"整理成改动建议，**用户确认后**才写

那个原则是：**模型只提议，落库要人点**。而且模型的结论永远与
服务端算出来的**客观信号并排显示**。

## 为什么信号必须自己算

`check_chunks.py` 开头记着一次失败：让模型对每个切片"读着像不像句子"打分，
判坏 60%；做对照实验时，**同一模型对同一文本给出了相反结论**。所以这里
不把判定权交给模型：符号占比、异域码位占比、单字碎片率、词间空格缺失率、
与上一段的重复度这些都由代码算出来，与模型的 verdict 一起给用户看 ——
用户一眼就能判断模型是不是在胡说。

## 对话不落盘

对话内容由插件内存持有、退出即清（用户明确要求）。本模块**不存对话**；
只落盘两样东西：检查进度与结论（`para_check`）、确认过的修正
（`fulltext_patch` / `para_override`）—— 否则"续跑"和"改过再改"都做不到。
"""

from __future__ import annotations

import os
import re

import experience as EXP
import paras as P
import prompts as PR
import schemas as S

# ---------------------------------------------------------------- 注入


def read_view(key: str, level: str = "tldr") -> str:
    path = os.path.join(S.VIEWS_DIR, f"{key}.{level}.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return ""


def _sample_pages(text: str, budget: int) -> str:
    """超预算时**按页均匀取样**，而不是只留开头。

    只留开头的话，用户问后面某一节时模型手里根本没有那段，会答"这篇里没有"
    —— 那是"注入策略"造成的假象，不是文献里真没有。

    ⚠ 每页分到的额度要**先扣掉页标题的开销**再平均，否则"每页至少 200 字"
      这条下限会让总量超预算，最后的一次 `[:budget]` 截断又把后几页全丢掉 ——
      正是这个函数想避免的那个毛病（实测：预算 400、5 页时只剩前 3 页）。
    """
    parts = re.split(r"^(## p\.[0-9]+)\s*$", text, flags=re.M)
    if len(parts) < 3:
        return text[:budget]
    pages = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    overhead = sum(len(head) + 4 for head, _ in pages)
    per = max(40, (budget - overhead) // max(1, len(pages)))
    out = []
    for head, body in pages:
        out.append(head)
        out.append(body.strip()[:per])
    return "\n\n".join(out)[:budget]


def build_context(key: str, inject: str = "tldr", budget: int = 6000) -> dict:
    """按需注入上下文。返回 `{text, mode, chars, total_chars, truncated, note}`。

    ⚠ **如实报数**：`chars / total_chars / truncated` 会显示在界面上。
      静默截断会让用户误以为"模型看过全文"。
    """
    mode = (inject or "none").strip().lower()
    if mode in ("", "none", "off"):
        return {"text": "", "mode": "none", "chars": 0, "total_chars": 0,
                "truncated": False, "note": "未注入"}
    if mode == "tldr":
        text = read_view(key, "tldr")
        if not text:
            return {"text": "", "mode": "tldr", "chars": 0, "total_chars": 0,
                    "truncated": False,
                    "note": "这篇还没有摘要级视图（先在面板建库/补视图）"}
        text = f"【摘要级视图（views/{key}.tldr.md）】\n{text}"
        return {"text": text, "mode": "tldr", "chars": len(text),
                "total_chars": len(text), "truncated": False,
                "note": f"已注入摘要级 {len(text)} 字符"}
    # full
    path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
    if not os.path.exists(path):
        return {"text": "", "mode": "full", "chars": 0, "total_chars": 0,
                "truncated": False, "note": f"没有 fulltext/{key}.md（先在面板建库）"}
    with open(path, encoding="utf-8") as fh:
        full = fh.read()
    if len(full) <= budget:
        text = f"【全文（fulltext/{key}.md）】\n{full}"
        return {"text": text, "mode": "full", "chars": len(full),
                "total_chars": len(full), "truncated": False,
                "note": f"已注入全文 {len(full)} 字符"}
    got = _sample_pages(full, budget)
    text = f"【全文节选（按页均匀取样；原文 {len(full)} 字符）】\n{got}"
    return {"text": text, "mode": "full", "chars": len(got),
            "total_chars": len(full), "truncated": True,
            "note": (f"全文 {len(full)} 字符超预算，按页均匀取样注入 {len(got)} 字符"
                     f"（约 {int(len(got) / 1.6)} token）—— 只留开头会让"
                     f"后面几节的问题答成「这篇里没有」")}


# ---------------------------------------------------------------- 聊天


def _history_text(messages: list, keep: int = 6) -> str:
    """把消息列表压成一段文字（Ollama 走单 prompt，没有 chat 结构）。

    只留最近 keep 轮：本地模型上下文小，旧轮次挤掉的是文献本身。
    """
    lines = []
    for m in (messages or [])[-keep:]:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        who = {"user": "我", "assistant": "助手", "system": "说明"}.get(role, role)
        lines.append(f"{who}：{content}")
    return "\n".join(lines)


def chat(key: str, messages: list, inject: str = "tldr", model: str = "",
         num_ctx: int = 16000, budget: int = 6000) -> dict:
    """带上这篇的上下文回答用户的问题。"""
    import judge

    ctx = build_context(key, inject, budget=budget)
    msgs = list(messages or [])
    question = ""
    for m in reversed(msgs):
        if str(m.get("role")) == "user" and str(m.get("content") or "").strip():
            question = str(m["content"]).strip()
            break
    history = _history_text(msgs[:-1] if msgs else [])
    prompt = PR.render("chat", context=ctx["text"] or "（无上下文）",
                       history=history or "（还没聊过）", question=question)
    res = judge.generate(prompt, system=PR.get("chat", "system"), model=model,
                         json_mode=False, temperature=0.3, timeout=300.0,
                         num_ctx=num_ctx)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "模型调用失败",
                "hint": res.get("hint") or "", "injected": ctx}
    return {"ok": True, "text": (res.get("text") or "").strip(),
            "model": res.get("model") or "", "injected": ctx}


# ---------------------------------------------------------------- 客观信号

# 数学符号（公式碎片最直接的证据）
MATH_RE = re.compile(r"[∑∫∮√±×÷≈≠≤≥∂∇∞∏∐⟨⟩⌈⌉⌊⌋⎡⎤⎣⎦⎢⎥⁄∕·⋅⊗⊕→←↔]")
# 矩阵/行列式的括号残片（`⎡BX BY BZ / aX0 aX1 aX2⎦` 这种符号汤的来源）
MATRIX_RE = re.compile(r"[⎡⎢⎣⎤⎥⎦]")
# 字母数字紧邻（`aX0` `X1` `1D`）：正文里偶尔有一两个，成片出现就是变量/下标
LETDIG_RE = re.compile(r"[A-Za-z][0-9]|[0-9][A-Za-z]")
# 异域码位：藏文/天城文/阿拉伯/希伯来/孟加拉/泰文/西里尔…
# 中英文学术 PDF 里出现这些基本可以断定字符映射错了（见 schemas 的说明）
EXOTIC_RE = re.compile(r"[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f"
                       r"\u0980-\u09ff\u0e00-\u0e7f\u0f00-\u0fff]")
PRIVATE_RE = re.compile(r"[\ue000-\uf8ff]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
DIGIT_RE = re.compile(r"[0-9]")
# 表格/公式残留
TABLEISH_RE = re.compile(r"[|｜]\s*[-—:]{2,}|\|\s*\w+\s*\|")
SENT_END_RE = re.compile(r"[。．！？；!?;.…][\"”’)\]]*$")


def _ratio(n: int, total: int) -> float:
    return round(n / total, 3) if total else 0.0


def signals(text: str, *, prev_tail: str = "", next_head: str = "",
            kind: str = "prose") -> dict:
    """服务端算的客观信号 —— 与模型结论并排显示，用户可核对。"""
    t = (text or "").strip()
    n = len(t)
    latin_words = re.findall(r"[A-Za-z]{2,}", t)
    # 词间空格缺失：英文单词被拼在一起（`Fluxgateerrormagneticfield`）
    camel = len(re.findall(r"[a-z]{3}[A-Z][a-z]", t))
    # 碎片度：⚠ 这条路走过三次都没走通，最后**只留中文字间空格**这一条：
    #   1) 数"被分隔符夹住的单字"绝对个数 → 正常正文大面积误报（占全部标记 75%）；
    #   2) 改成"单字词占比" → 中文没有词边界，混着算必然失真；
    #   3) 只看拉丁词的单字母占比 → 中英混排的学术文本里缩写和人名首字母
    #      （`Barrie W. Leach`、`SQUID`、`CS-3`）到处都是，照样误报。
    #   而那三类想抓的"碎片"，其实已经被符号/私有区/矩阵/表格这几条覆盖了。
    latin_tokens = re.findall(r"[A-Za-z]+", t)
    cjk_spaced = len(re.findall(r"[\u4e00-\u9fff][ \t][\u4e00-\u9fff]", t))
    dup = 0.0
    if prev_tail and t:
        a, b = set(prev_tail[-120:]), set(t[:120])
        if a and b:
            dup = round(len(a & b) / max(1, len(a | b)), 3)
    return {
        "chars": n,
        "kind": kind,
        "cjk_ratio": _ratio(len(CJK_RE.findall(t)), n),
        "latin_ratio": _ratio(len(LATIN_RE.findall(t)), n),
        "digit_ratio": _ratio(len(DIGIT_RE.findall(t)), n),
        "symbol_ratio": _ratio(len(MATH_RE.findall(t)), n),
        "matrix_marks": len(MATRIX_RE.findall(t)),
        "letdig_pairs": len(LETDIG_RE.findall(t)),
        "exotic_ratio": _ratio(len(EXOTIC_RE.findall(t)), n),
        "private_ratio": _ratio(len(PRIVATE_RE.findall(t)), n),
        "camel_joins": camel,
        "latin_tokens": len(latin_tokens),
        "cjk_spaced": cjk_spaced,
        "table_marks": len(TABLEISH_RE.findall(t)),
        "ends_sentence": bool(SENT_END_RE.search(t)),
        "starts_lower": bool(t[:1].islower()),
        "overlap_prev": dup,
        "words": len(latin_words),
    }


def suspect(sig: dict) -> tuple[bool, list]:
    """按客观信号粗判"这一段值得让模型看一眼"。

    ⚠ 这是**预筛**，不是判定：目的是别把全库 9 千段全喂给模型（既慢又只会
      招来一堆假报警 —— 上一版逐切片判好坏的教训）。信号可疑 → 交给模型；
      信号正常 → 跳过。
    """
    why = []
    if sig["symbol_ratio"] >= 0.30:
        why.append(f"符号占比 {sig['symbol_ratio']:.0%}")
    # ⚠ 光看"符号占比"会漏掉真实的那类符号汤：`⎡BX BY BZ / aX0 aX1 aX2⎦ (5) where`
    #   里大多是字母和数字，符号只占十几个百分点（本机实测漏判过）。
    #   矩阵括号残片 + 字母数字成片交错才是它的特征。
    if sig.get("matrix_marks", 0) >= 2:
        why.append(f"矩阵括号残片 {sig['matrix_marks']} 处（疑似公式被拆散）")
    if (sig.get("letdig_pairs", 0) >= 3 and sig["cjk_ratio"] == 0
            and sig["chars"] < 200 and sig["digit_ratio"] >= 0.12):
        why.append(f"字母数字交错 {sig['letdig_pairs']} 处且无中文（疑似公式碎片）")
    if sig.get("cjk_spaced", 0) >= 5:
        why.append(f"中文字间被插空格 {sig['cjk_spaced']} 处")
    if sig["exotic_ratio"] >= 0.02:
        why.append(f"异域码位 {sig['exotic_ratio']:.0%}")
    if sig["private_ratio"] >= 0.02:
        why.append(f"私有区码位 {sig['private_ratio']:.0%}")
    if sig["table_marks"]:
        why.append(f"表格残留 {sig['table_marks']} 处")
    if sig["camel_joins"] >= 3:
        why.append(f"英文词粘连 {sig['camel_joins']} 处")
    if sig["overlap_prev"] >= 0.55:
        why.append(f"与上段重复 {sig['overlap_prev']:.0%}")
    if not sig["ends_sentence"] and not sig["starts_lower"] and sig["chars"] < 40:
        why.append("短且没有句末标点")
    return bool(why), why


# ---------------------------------------------------------------- 逐段计划


def _patch_marks(conn, key: str) -> dict:
    """这一段是不是被改过：查 `fulltext_patch` / `para_override`（按锚点内容）。"""
    out = {"patches": {}, "overrides": set()}
    try:
        for row in conn.execute(
                "SELECT patch_id, anchor, status FROM fulltext_patch "
                "WHERE item_key=? AND status IN ('applied','proposed')", (key,)):
            out["patches"][str(row["anchor"])[:40]] = str(row["status"])
    except Exception:      # noqa: BLE001
        pass
    try:
        for row in conn.execute(
                "SELECT p_hash, kind FROM para_override WHERE item_key=?", (key,)):
            out["overrides"].add((str(row["p_hash"]), str(row["kind"])))
    except Exception:      # noqa: BLE001
        pass
    return out


def _progress(conn, key: str) -> dict:
    try:
        rows = list(conn.execute(
            "SELECT p_hash, status, done_units FROM para_check WHERE item_key=?",
            (key,)))
        return {str(r["p_hash"]): (str(r["status"]), int(r["done_units"] or 0))
                for r in rows}
    except Exception:      # noqa: BLE001
        return {}


def plan(conn, key: str, scope: str = "suspect", include_fixed: bool = False) -> dict:
    """算出"从哪一段开始、还有多少要做"（续跑用）。

    进度与修正都按**段落指纹**对齐：正文重建后段号会漂，指纹不会；
    对不上的（内容已变）标 `stale`，界面要说"正文重建过，N 段需重查"。
    """
    paras = P.load_paras(conn, key)
    if not paras:
        return {"ok": False, "error": f"没有 {key} 的 fulltext（先在面板建库）",
                "total": 0, "items": []}
    marks = _patch_marks(conn, key)
    prog = _progress(conn, key)
    hashes = {p.hash for p in paras}
    # 库里记着的进度，若指纹在现在的正文里已经找不到 —— 这段多半被修正/重建
    # 改掉了，它的进度已经失效。要报出来，不能静默丢（否则用户以为"都查过了"）。
    orphan = sum(1 for h in prog if h not in hashes)
    items = []
    stale = 0
    for p in paras:
        if p.kind not in ("prose",):
            continue
        sig = signals(p.text)
        hot, why = suspect(sig)
        if scope != "all" and not hot:
            continue
        st, done = prog.get(p.hash, ("", 0))
        overridden = p.overridden in ("join_prev", "split_at")
        if st == "stale":
            stale += 1
        fixed = (st == "fixed") or overridden
        # 跳过规则：查过且正文没变的跳过；改过的段默认也跳过
        # （「连已修复的段也一起看」= include_fixed；单段「重新检查」由界面直接发起）
        skip = ((st in ("ok", "skipped", "fixed") and not overridden)
                or (fixed and not include_fixed))
        items.append({
            "logical_index": p.logical_index, "page": p.page, "hash": p.hash,
            "chars": p.n_chars, "evidence": p.evidence, "signals": sig,
            "why": why, "status": st, "done_units": done,
            "skip": bool(skip), "fixed": bool(fixed),
        })
    nxt = next((it for it in items if not it["skip"]), None)
    checked = sum(1 for it in items if it["status"] in ("ok", "skipped", "fixed"))
    notes = [f"共 {len(items)} 个待查段，已查 {checked}"]
    if stale:
        notes.append(f"{stale} 段因正文重建需重查")
    if orphan:
        notes.append(f"{orphan} 段进度已对不上现在的正文")
    return {"ok": True, "key": key, "scope": scope, "total": len(items),
            "checked": checked, "stale": stale, "orphan": orphan,
            "next": nxt, "items": items, "note": "；".join(notes)}


def check_paragraph(conn, key: str, page: int, logical_index: int, text: str,
                    *, prev_tail: str = "", next_head: str = "",
                    model: str = "", signals_in: dict | None = None,
                    p_hash: str = "") -> dict:
    """让模型核对一段；结论与客观信号一起返回（界面并排显示）。

    ## 两种调用方式

    · 给 `p_hash`（**插件走这条**）：服务端自己按指纹去 fulltext md 里取出
      这一段与它的前后邻居。为什么不让插件把正文传上来：`/para-plan` 的返回
      里不含段落全文（889 个可疑段各几百字，一次全带回来是几百 KB），
      插件只有 hash。**插件发 hash、服务端取正文**，职责也更清楚。
    · 直接给 `text`（**面板走这条**：面板本来就是从 md 里读的）。

    ⚠ 曾经踩过：插件按"计划里的 item"取 `item.text` —— 计划里根本没有这个
      字段，于是模型收到的是**空段落**，而它照样一本正经地回了一个 verdict。
    """
    import judge

    if p_hash and not text:
        found = None
        paras = P.load_paras(conn, key)
        for i, p in enumerate(paras):
            if p.hash == p_hash:
                found = (i, p)
                break
        if not found:
            return {"ok": False, "error": f"这篇的正文里找不到指纹 {p_hash}"
                    "（正文可能重建过，请重新拉一次逐段计划）", "signals": {}}
        i, p = found
        text = p.text
        page = p.page
        logical_index = p.logical_index
        prose = [x for x in paras if x.kind == "prose"]
        try:
            j = prose.index(p)
        except ValueError:
            j = -1
        if j > 0:
            prev_tail = prose[j - 1].text[-300:]
        if 0 <= j < len(prose) - 1:
            next_head = prose[j + 1].text[:300]

    sig = signals(text, prev_tail=prev_tail, next_head=next_head)
    hot, why = suspect(sig)
    sig["suspect_why"] = why
    prompt = PR.render(
        "para", page=page,
        signals="；".join(f"{k}={v}" for k, v in sig.items()
                          if k not in ("suspect_why", "kind")) or "（无）",
        prev_tail=(prev_tail or "（这一页的第一段）")[-300:],
        text=text,
        next_head=(next_head or "（这一页的最后一段）")[:300])
    res = judge.generate(prompt, system=PR.get("para", "system"), model=model,
                         json_mode=True, temperature=0.0, timeout=240.0)
    out = {"ok": False, "signals": sig, "page": page, "text": text,
           "logical_index": logical_index, "hash": P.para_hash(text)}
    if not res.get("ok"):
        out["error"] = res.get("error") or "模型调用失败"
        out["hint"] = res.get("hint") or ""
        return out
    good, data = judge.parse_json(res.get("text") or "")
    if not good or not isinstance(data, dict):
        out["error"] = "模型输出不是 JSON"
        out["raw"] = str(res.get("text"))[:300]
        return out
    verdict = str(data.get("verdict") or "unsure").strip().lower()
    if verdict not in ("ok", "suspect", "damaged", "unsure"):
        verdict = "unsure"
    before = str(data.get("before") or "").strip()
    after = str(data.get("after") or "").strip()
    # 只接受"最小修正"：模型想整段重写时（before 覆盖了整段的大部分）
    # 一律降级成"建议人工处理"，不生成可采用项（见模块头说明）。
    # ⚠ 门槛必须是**相对这一段**的：先写成 `max(120, len*0.5)`，
    #   而短段（几十字）里"整段重写"根本够不到 120，规则等于失效（实测漏过）。
    limit = max(40, len(text) * 0.8)
    big = bool(before) and (len(before) > limit or not before_in_text(before, text))
    out.update({
        "ok": True, "verdict": verdict,
        "kind": str(data.get("kind") or "none").strip().lower(),
        "join_with": str(data.get("join_with") or "").strip().lower(),
        "before": "" if big else before,
        "after": "" if big else after,
        "reason": str(data.get("reason") or "")[:400],
        "manual_only": big,
        "model": res.get("model") or "",
    })
    return out


def before_in_text(before: str, text: str) -> bool:
    """模型给的 `before` 必须在原文里找得到（忽略空白）。

    找不到 = 它在编原文。这种"修正"落进补丁表只会让锚点永远不命中。
    """
    norm = lambda s: re.sub(r"\s+", "", s or "")
    return bool(before) and norm(before) in norm(text)


# ---------------------------------------------------------------- 定位


def _norm_for_match(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[·．.…\-–—_]+", "", s)
    return s


def locate(conn, key: str, text: str, limit: int = 5) -> dict:
    """把"用户选中的一段文字"定位到段落。

    ⚠ **唯一命中才给 best**：多个候选就都列出来让用户选。猜错定位比不定位
      更糟 —— 用户以为在改 A 段，实际改了 B 段。
    """
    import difflib

    target = _norm_for_match(text)
    if len(target) < 4:
        return {"ok": False, "error": "选中文字太短，认不出是哪一段",
                "matches": []}
    paras = P.load_paras(conn, key)
    scored = []
    for p in paras:
        body = _norm_for_match(p.text)
        if not body:
            continue
        # 先看有没有"包含"关系（选中的就是原文片段时最快最准）
        if target in body:
            score = 0.99
        else:
            head = target[:60]
            score = max(difflib.SequenceMatcher(None, head, body[:max(60, len(head))]).ratio(),
                        difflib.SequenceMatcher(None, target[:120], body[:400]).ratio())
        if score >= 0.45:
            scored.append((round(score, 3), p))
    scored.sort(key=lambda x: (-x[0], x[1].logical_index))
    matches = [{"logical_index": p.logical_index, "page": p.page, "score": s,
                "chars": p.n_chars, "kind": p.kind,
                "preview": p.text[:80]} for s, p in scored[:limit]]
    best = matches[0] if (scored and scored[0][0] >= 0.6
                          and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.12)
                          ) else None
    return {"ok": True, "matches": matches, "best": best,
            "ambiguous": bool(matches) and not best,
            "note": ("定位到 1 段" if best else
                     ("有多段都像，请你选一个（不替你猜）" if matches
                      else "没找到对得上的段落"))}


# ---------------------------------------------------------------- 写入建议


def propose(key: str, transcript_tail: str, instruction: str = "",
            model: str = "") -> dict:
    """把讨论整理成改动建议（**不落库**；落库要用户点确认）。"""
    import judge

    prompt = PR.render("propose", key=key, instruction=instruction or "（未说明）",
                       transcript=(transcript_tail or "")[-4000:])
    res = judge.generate(prompt, system=PR.get("propose", "system"), model=model,
                         json_mode=True, temperature=0.0, timeout=300.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "模型调用失败"}
    good, data = judge.parse_json(res.get("text") or "")
    if not good or not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON",
                "raw": str(res.get("text"))[:300]}
    exps = [e for e in (data.get("experiences") or []) if isinstance(e, dict)]
    wts = [w for w in (data.get("weights") or []) if isinstance(w, dict)]
    patches = [p for p in (data.get("patches") or []) if isinstance(p, dict)]
    joins = [j for j in (data.get("joins") or []) if isinstance(j, dict)]
    # 口径收紧：模型没给出任何关联文献时，标成"可能与文献无关"让界面默认不勾
    # （经验库参与检索加权，工具链/工程类的记录进去会让排序变脏）
    for e in exps:
        raw_keys = EXP.split_list(e.get("item_keys"))
        e["item_keys"] = raw_keys or [key]
        e["suspect_unrelated"] = not raw_keys
    return {"ok": True, "experiences": exps, "weights": wts,
            "patches": patches, "joins": joins, "model": res.get("model") or "",
            "note": "这些只是建议。确认前不会写进知识库。"}


def apply_plan(s, key: str, plan_data: dict, source: str = "zotero-chat") -> dict:
    """**用户确认后**才调这里。写三类：经验 / 权重 / 全文补丁（含段落合并）。"""
    written = {"experiences": [], "weights": [], "patches": [], "joins": [],
               "errors": []}
    for e in plan_data.get("experiences") or []:
        try:
            eid = EXP.add_experience(
                s, asked=str(e.get("asked") or ""),
                outcome=str(e.get("outcome") or "unknown"),
                method=str(e.get("method") or ""),
                item_keys=EXP.split_list(e.get("item_keys")) or [key],
                context=str(e.get("context") or ""),
                reason=str(e.get("reason") or ""),
                evidence=str(e.get("evidence") or ""),
                tags=e.get("tags") or [], source=source)
            written["experiences"].append(eid)
        except (ValueError, Exception) as exc:      # noqa: BLE001
            written["errors"].append(f"经验没写进去：{exc}")
    for w in plan_data.get("weights") or []:
        k = str(w.get("key") or key).strip()
        try:
            EXP.set_weight(s, k, pinned=w.get("pinned"), manual=w.get("manual"),
                           note=str(w.get("note") or ""))
            written["weights"].append(k)
        except Exception as exc:                    # noqa: BLE001
            written["errors"].append(f"权重没写进去（{k}）：{exc}")
    for pt in plan_data.get("patches") or []:
        before = str(pt.get("before") or "").strip()
        if not before:
            continue
        try:
            s.write(
                """INSERT INTO fulltext_patch(item_key, page, kind, anchor,
                       replacement, note, source, status, created_at)
                   VALUES(?,?,?,?,?,?,?,'applied',?)""",
                (key, int(pt.get("page") or 0), str(pt.get("kind") or "text"),
                 before, str(pt.get("after") or ""), str(pt.get("note") or ""),
                 source, EXP.now_iso()))
            written["patches"].append(before[:40])
        except Exception as exc:                    # noqa: BLE001
            written["errors"].append(f"补丁没写进去：{exc}")
    for j in plan_data.get("joins") or []:
        try:
            P.add_override(s if hasattr(s, "execute") else s.conn, key, "join_prev",
                           str(j.get("p_hash") or ""),
                           anchor=str(j.get("anchor") or ""),
                           reason=str(j.get("reason") or "窗格确认"),
                           source=source)
            written["joins"].append(str(j.get("p_hash") or ""))
        except Exception as exc:                    # noqa: BLE001
            written["errors"].append(f"段落合并没写进去：{exc}")
    ok = bool(written["experiences"] or written["weights"] or written["patches"]
              or written["joins"])
    return {"ok": ok, "written": written,
            "note": "已写入" if ok else "没有任何可写的内容"}


def draft_experience(text: str, ref_id: int = 0, model: str = "",
                     conn=None) -> dict:
    """把用户的大白话整理成一条经验草稿（**不落库**）。

    另外给出：相似的已有经验（用户点"其实是改这条"）、提到的文献候选
    （**显示标题**，因为关联错文献＝权重加到别的论文上）。
    """
    import judge

    res = judge.generate(PR.render("draft", text=text),
                         system=PR.get("draft", "system"), model=model,
                         json_mode=True, temperature=0.1, timeout=240.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "模型调用失败"}
    good, data = judge.parse_json(res.get("text") or "")
    if not good or not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON",
                "raw": str(res.get("text"))[:300]}
    draft = {
        "asked": str(data.get("asked") or "")[:400],
        "outcome": str(data.get("outcome") or "unknown").strip().lower(),
        "method": str(data.get("method") or "")[:300],
        "context": str(data.get("context") or "")[:300],
        "reason": str(data.get("reason") or "")[:600],
        "evidence": str(data.get("evidence") or "")[:200],
        "tags": EXP.split_list(data.get("tags")),
        "item_keys": EXP.split_list(data.get("item_keys")),
        "similar_hint": str(data.get("similar_hint") or "")[:200],
    }
    if draft["outcome"] not in EXP.OUTCOMES:
        draft["outcome"] = "unknown"
    similar, items = [], []
    if conn is not None:
        try:
            for row in conn.execute(
                    "SELECT id, asked, outcome, item_keys FROM experience "
                    "ORDER BY id DESC LIMIT 80"):
                if str(row["asked"])[:12] and str(row["asked"])[:12] in text:
                    similar.append({"id": row["id"], "asked": str(row["asked"])[:80],
                                    "outcome": row["outcome"]})
        except Exception:      # noqa: BLE001
            pass
        try:
            import learn                      # 复用已有的"文本→文献 key"匹配
            items = learn.match_items(text, conn)
        except Exception:      # noqa: BLE001
            items = []
    titles = []
    if conn is not None and (draft["item_keys"] or items):
        keys = list(dict.fromkeys(draft["item_keys"] + items))
        for k in keys:
            row = None
            try:
                row = conn.execute("SELECT title FROM items WHERE key=?",
                                   (k,)).fetchone()
            except Exception:      # noqa: BLE001
                row = None
            titles.append({"key": k, "title": (row["title"] if row else "")[:80]})
        draft["item_keys"] = [t["key"] for t in titles]
    return {"ok": True, "draft": draft, "similar": similar[:5],
            "items": titles, "ref_id": int(ref_id or 0),
            "model": res.get("model") or "",
            "note": "这是草稿。确认后才写库（写库时会重算权重）。"}
