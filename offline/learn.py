"""从 DSH 会话记录里补经验：小模型读对话 → 抽结构化经验 → 等你确认。

    python offline/learn.py scan                  # 扫全部会话，产出待确认清单
    python offline/learn.py scan --since-days 7   # 只看最近 7 天
    python offline/learn.py scan --session <id>   # 只看某个会话
    python offline/learn.py show                  # 看待确认清单里的内容
    python offline/learn.py mark <id> --approve   # 标为已采纳（会提示怎么入库）
    python offline/learn.py mark <id> --reject    # 标为丢弃

为什么要有这一步：当场记（kb_experience_add）靠 DSH 主动调用，会漏；
事后扫一遍能把漏掉的结论补回来。但小模型会编，所以它写的东西
**只进待确认区**，绝不允许直接进经验表污染检索排序。

产出：kb/inbox/pending.jsonl（每行一条经验 + 审核状态）
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402
import judge  # noqa: E402

PENDING = os.path.join(S.INBOX_DIR, "pending.jsonl")
SEEN = os.path.join(S.INBOX_DIR, "scanned.json")

# 粗筛关键词：只有提到这些词的片段才值得送模型，省时间也省显存。
# 太宽会把整场对话都送去，太窄会漏掉"结论写得含蓄"的片段。
SIGNALS = [
    "有效", "无效", "失败", "成功", "收敛", "发散", "误差", "精度",
    "试了", "试过", "尝试", "结果", "对比", "改进", "问题", "原因是",
    "不work", "不行", "有用", "没用", "效果", "提升", "下降",
    "effective", "ineffective", "failed", "worked", "improved", "worse",
    "better", "converge", "diverge", "error",
]

# 第二道过滤：片段必须"像是在用文献做事"才抽取。
# 为什么需要这道：实测把"本项目怎么搭的"这类工程对话也抽成了经验
# （内容没错，但不是"文献使用方法"的经验，混进来会让经验层变脏）。
DOC_WORK_HINTS = [
    "文献", "论文", "文章", "这篇", "那篇", "作者", "DOI", "摘要",
    "Zotero", "zotero", "知识库", "kb_search", "kb_item", "kb_fulltext",
    "引用", "参考文献", "标注", "高亮", "笔记",
]

# 知识库工具名：调用过这些工具，基本可以确定这段在跟文献打交道
KB_TOOLS = {
    "kb_search", "kb_item", "kb_fulltext", "kb_experience_add",
    "kb_experience_query", "kb_weight_set", "kb_reindex", "kb_stats",
    "mcp__zotero-kb__kb_search", "mcp__zotero-kb__kb_item",
    "mcp__zotero-kb__kb_fulltext",
}


# ---------------------------------------------------------------- 读转录


def load_positions() -> dict:
    if os.path.exists(SEEN):
        try:
            return json.load(open(SEEN, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_positions(data: dict) -> None:
    os.makedirs(S.INBOX_DIR, exist_ok=True)
    with open(SEEN, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def read_transcript(path: str) -> list[dict]:
    """解压并解析一个会话转录，返回事件列表。"""
    try:
        import zstandard
    except ImportError:
        print("  [!!] 缺少 zstandard，无法读会话记录：pip install zstandard")
        return []
    with open(path, "rb") as fh:
        raw = zstandard.ZstdDecompressor().stream_reader(fh).read()
    events = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _text_of(content) -> str:
    """把消息的 content 数组拼成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # reasoning 也留着：结论常常先出现在思考里
        if block.get("type") in ("text", "reasoning"):
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def extract_segments(events: list[dict], start_seq: int = 0) -> list[dict]:
    """把事件流切成"片段"：以用户提问开头，聚合到下一个用户提问之前。

    这样每个片段是一个完整的问题-过程-结论单元，模型判断起来最省事。
    """
    segments: list[dict] = []
    current: dict | None = None

    for ev in events:
        kind = ev.get("type", "")
        seq = ev.get("seq", 0)
        if seq <= start_seq:
            continue
        if kind == "user/message":
            source = (ev.get("data") or {}).get("source") or {}
            # 跳过系统注入（审批策略变更、记忆注入之类），它们不是用户真实提问
            if source.get("kind") in ("user-approval", "plugin:openviking-memory",
                                      "system"):
                continue
            text = _text_of((ev.get("data") or {}).get("content"))
            if not text.strip():
                continue
            if current:
                segments.append(current)
            current = {"start_seq": seq, "asked": text.strip()[:800],
                       "texts": [], "tools": []}
            continue

        if current is None:
            continue

        if kind == "assistant/message":
            text = _text_of(((ev.get("data") or {}).get("message") or {}).get("content"))
            if text.strip():
                current["texts"].append(text.strip())
        elif kind == "tool/call":
            name = (ev.get("data") or {}).get("name")
            if name:
                current["tools"].append(str(name))
        elif kind == "session/end-seed":
            break

    if current:
        segments.append(current)
    return segments


def segment_text(seg: dict, max_chars: int = 8000) -> str:
    """把片段渲染成给模型看的文本，首尾都留（结论常在末尾）。"""
    body = "\n\n".join(seg["texts"])
    header = f"【用户问题】{seg['asked']}\n"
    if seg["tools"]:
        header += f"【用过的工具】{', '.join(sorted(set(seg['tools'])))}\n"
    budget = max_chars - len(header)
    if len(body) > budget:
        head = body[: budget // 2]
        tail = body[-(budget // 2):]
        body = f"{head}\n\n……（中间省略 {len(body) - budget} 字）……\n\n{tail}"
    return header + "\n" + body


def looks_promising(text: str) -> bool:
    """第一道：出现经验信号词（是不是在报结果）。"""
    lowered = text.lower()
    return any(sig.lower() in lowered for sig in SIGNALS)


def is_doc_work(seg: dict, text: str) -> bool:
    """第二道：这段是不是在"用文献做事"。

    命中任一条即可：
      1. 调用了知识库工具（kb_* / mcp__zotero-kb__*）
      2. 文本里有文献工作相关的词（文献、论文、标注、引用…）

    为什么要这道：没有它，纯工程的对话（比如"我是怎么搭这个知识库的"）
    也会被抽成"经验"，内容虽然不假，但混进"文献使用方法经验"里会让
    检索带出的相关经验跑偏。
    """
    for tool in seg.get("tools") or []:
        if tool in KB_TOOLS or tool.startswith("mcp__zotero-kb__"):
            return True
    return any(hint in text for hint in DOC_WORK_HINTS)


# 主题词表不再手工维护 —— 见 library_domain()：
# 原来写死了一张"领域词"清单，结果库里真实存在的某些主题文献被当成"非主题"
# 而漏匹配（清单永远列不全，而且那是**按某一台机器的库**写的）。
# 现在改成从全部标题自动学出高频 2 字片段作为主题词。
#
# ⚠ 这一段原来把本机库里的真实标题与领域词写进了注释。本文件要公开 ——
#   举例一律用占位说法，别把用户的方向写进源码。


def _title_probes(title: str, min_len: int = 8) -> list[str]:
    """从标题里切出可用于匹配的字符串。

    为什么要做"滑动窗口"：对话里提一篇文献通常**不会逐字照抄标题**，
    而是说「基于某某传感器的某某技术研究那篇」或者只说中间一截。
    早期版本只拿"最长的连续汉字段"去匹配，实测带标点断开的标题
    （形如《某某架构与全场景方案…》）就匹配不上。

    ⚠ 步长必须是 1：曾用步长 4 采样，结果一个 12 字的标题
    正好落在采样空隙里，匹配不上 —— 真实对话里的说法
    落在哪个位置是随机的，采样就会漏。

    min_len 由调用方按"标题是否属于库的主题范围"决定（见 match_items）：
    属于主题的可以用 8 字探针（召回优先），边缘主题的必须 12 字（精度优先）。
    """
    probes: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", title):
        if len(seg) < min_len:
            continue
        # 长段用大窗口，短段用 min_len
        for size in (16, 12):
            if len(seg) >= size:
                for start in range(len(seg) - size + 1):
                    probes.append(seg[start:start + size])
        if len(seg) >= min_len:
            for start in range(len(seg) - min_len + 1):
                probes.append(seg[start:start + min_len])
    for seg in re.findall(r"[A-Za-z][A-Za-z\-]+(?: [A-Za-z\-]+){3,}", title):
        probes.append(seg)
    # 去重并按长度降序（长探针更不容易误匹配，优先用）
    return sorted(set(probes), key=len, reverse=True)[:80]


def library_domain(min_count: int = 3) -> set[str]:
    """从库里的标题**自动学出"主题词表"**，而不是手工维护一张领域词清单。

    为什么自动学：手工清单必然漏。最初写死了几个具体学科的词，
    结果库里真实存在的《海洋能发电技术的发展现状与前景》被当成"非主题"，
    该匹配的文献匹配不上。改成"在 ≥min_count 个标题里出现过的 2 字片段"
    作为主题词 —— 同一个主题的文献标题会自然共享词（比如同一领域标题会共享的实词，
    注意力机制…），而技术名词（目录结构、配置文件）不会出现在多个标题里。
    """
    conn = S.connect(S.INDEX_DB)
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT title FROM items"):
        title = row["title"] or ""
        segs = re.findall(r"[\u4e00-\u9fff]{2,}", title)
        seen: set[str] = set()
        for seg in segs:
            for i in range(len(seg) - 1):
                seen.add(seg[i:i + 2])
        for gram in seen:
            counts[gram] = counts.get(gram, 0) + 1
    conn.close()
    return {gram for gram, n in counts.items() if n >= min_count}


_DOMAIN_CACHE: set[str] | None = None


def match_items(text: str, conn, domain: set[str] | None = None) -> list[str]:
    """把片段里提到的文献匹配到 Zotero key。

    门槛**按标题是否属于库的主题范围自适应**：
      · 主题内的标题：探针 8 字 → 召回优先（对话里常只说一截）
      · 边缘标题：探针 12 字 → 精度优先（避免"看起来像标题"的技术名词误匹配）

    再加一道硬门槛：探针必须有一截（≥8 字）出现在对话文本里。

    宁可漏（item_refs 里仍会留下原文说的"张宇那篇"），也不要错 ——
    错的 key 会让经验挂到不相干的文献上，反过来污染检索排序。
    """
    global _DOMAIN_CACHE
    if domain is None:
        if _DOMAIN_CACHE is None:
            _DOMAIN_CACHE = library_domain()
        domain = _DOMAIN_CACHE

    hits: list[str] = []
    for row in conn.execute("SELECT key, title FROM items WHERE length(title) > 8"):
        title = row["title"]
        in_domain = any(gram in title for gram in domain)
        min_len = 8 if in_domain else 12
        for probe in _title_probes(title, min_len=min_len):
            if probe in text:
                if row["key"] not in hits:
                    hits.append(row["key"])
                break
    return hits[:5]


# ---------------------------------------------------------------- 主流程


def cmd_scan(args: argparse.Namespace) -> int:
    conn = S.connect(S.INDEX_DB)
    ok, _models = judge.ollama_available()
    if not ok:
        print("[XX] Ollama 不可用，无法抽取经验。")
        print("     先启动 Ollama 并 `ollama pull qwen3:4b-instruct`；")
        print("     或者跳过这一步，在对话里让 DSH 用 kb_experience_add 当场记。")
        conn.close()
        return 1
    model = judge.pick_model()
    print(f"用模型：{model}\n")

    positions = load_positions()
    files = glob.glob(os.path.join(S.DSH_SESSIONS, "**", "session.v*.jsonl.zstd"),
                      recursive=True)
    if args.session:
        files = [f for f in files if args.session in f]
    if args.since_days:
        cutoff = time.time() - args.since_days * 86400
        files = [f for f in files if os.path.getmtime(f) >= cutoff]
    # 最近改动的先处理
    files.sort(key=os.path.getmtime, reverse=True)
    print(f"扫描 {len(files)} 个会话文件\n")

    new_rows: list[dict] = []
    processed = 0
    for path in files:
        session_id = os.path.basename(os.path.dirname(path))
        events = read_transcript(path)
        if not events:
            continue
        start = int(positions.get(session_id, 0))
        segments = extract_segments(events, start_seq=start)
        if not segments:
            continue
        max_seq = max(seg["start_seq"] for seg in segments)
        # 两道过滤：先看是不是在报结果，再看是不是在用文献做事
        candidates = []
        skipped_not_doc = 0
        for seg in segments:
            text = segment_text(seg)
            if not looks_promising(text):
                continue
            if not is_doc_work(seg, text):
                skipped_not_doc += 1
                continue
            candidates.append(seg)
        note = f"，跳过 {skipped_not_doc} 段非文献工作" if skipped_not_doc else ""
        print(f"{session_id[:28]}  片段 {len(segments)}（候选 {len(candidates)}{note}）")
        for seg in candidates:
            if args.limit and processed >= args.limit:
                print(f"    （已达 --limit {args.limit}，本轮到此为止；"
                      f"已处理的位置会记住，下次接着往后扫）")
                # 注意：这里不推进 positions，下次仍从这条开始
                positions.pop(session_id, None)
                save_positions(positions)
                _flush_pending(new_rows)
                _print_next_steps(len(new_rows))
                conn.close()
                return 0
            processed += 1
            text = segment_text(seg)
            result = judge.extract_experience(text, model=model)
            if not result["ok"]:
                print(f"    [XX] 抽取失败：{result['error']}")
                continue
            if not result["found"]:
                continue
            keys = match_items(text, conn)
            for item in result["items"]:
                new_rows.append({
                    "id": f"{session_id[-8:]}-{seg['start_seq']}-{len(new_rows)}",
                    "status": "pending",
                    "source": "llm",
                    "session": session_id,
                    "when": datetime.now(timezone.utc).astimezone().isoformat(
                        timespec="seconds"),
                    "asked": item["asked"] or seg["asked"][:200],
                    "method": item["method"],
                    "outcome": item["outcome"],
                    "reason": item["reason"],
                    "context": item["context"],
                    "items": keys,
                    "item_refs": item["item_refs"],
                    "evidence": item["evidence"],
                    "confidence": item["confidence"],
                })
                print(f"    + [{item['outcome']}] {(item['method'] or '(未写方法)')[:44]}"
                      f"  涉及 {keys or item['item_refs'] or '?'}")
        positions[session_id] = max_seq

    _flush_pending(new_rows)
    save_positions(positions)
    conn.close()
    _print_next_steps(len(new_rows))
    return 0


def _flush_pending(rows: list[dict]) -> None:
    """把新抽出的经验追加到待确认清单。"""
    if not rows:
        return
    os.makedirs(S.INBOX_DIR, exist_ok=True)
    with open(PENDING, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _print_next_steps(count: int) -> None:
    print(f"\n{'=' * 62}")
    print(f"新增待确认经验 {count} 条 → {PENDING}")
    if count:
        print("\n下一步：")
        print("  1. python offline/learn.py show          # 逐条看，判断小模型有没有编")
        print("  2. 把确实成立的条目在对话里说一声，")
        print("     让 DSH 用 kb_experience_add 入库（这样经验表只有人工确认过的内容）")
        print("  3. python offline/learn.py mark <id> --reject   # 丢弃明显错的")
    print("=" * 62)


def _read_pending() -> list[dict]:
    if not os.path.exists(PENDING):
        return []
    rows = []
    for line in open(PENDING, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write_pending(rows: list[dict]) -> None:
    with open(PENDING, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def cmd_show(args: argparse.Namespace) -> int:
    rows = _read_pending()
    if not rows:
        print(f"待确认清单是空的（{PENDING}）")
        print("先跑 python offline/learn.py scan 扫一遍会话记录。")
        return 0
    label = {"effective": "✅ 有效", "ineffective": "❌ 无效",
             "partial": "◐ 部分", "unknown": "❓ 未验证"}
    pending = [r for r in rows if r.get("status") == "pending"]
    other = len(rows) - len(pending)
    print(f"待确认 {len(pending)} 条（另有 {other} 条已处理）\n")
    for row in pending:
        conf = row.get("confidence", 0)
        flag = "  ⚠低置信" if conf and conf < 0.6 else ""
        print(f"[{row['id']}] {label.get(row['outcome'], row['outcome'])}"
              f"  置信 {conf:.2f}{flag}")
        print(f"   问题：{row['asked'][:100]}")
        if row["method"]:
            print(f"   方法：{row['method']}")
        if row["reason"]:
            print(f"   原因：{row['reason'][:160]}")
        if row["context"]:
            print(f"   条件：{row['context'][:100]}")
        if row["items"]:
            print(f"   涉及：{', '.join(row['items'])}")
        elif row["item_refs"]:
            print(f"   提到：{', '.join(row['item_refs'])}（未匹配到库中条目）")
        if row["evidence"]:
            print(f"   依据：{row['evidence'][:120]}")
        print(f"   来源：{row['session'][-12:]} seq={row['id'].split('-')[1]}"
              f"  入库：kb_experience_add  asked=... outcome={row['outcome']}")
        print()
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    rows = _read_pending()
    if not rows:
        print("待确认清单是空的")
        return 1
    hit = None
    for row in rows:
        if row["id"] == args.id:
            hit = row
            break
    if not hit:
        print(f"没找到 id={args.id}")
        return 1
    hit["status"] = "approved" if args.approve else "rejected"
    _write_pending(rows)
    print(f"{args.id} → {hit['status']}")
    if args.approve:
        print("\n现在把这条交给 DSH 入库（它调 kb_experience_add）：")
        print(f'  asked="{hit["asked"][:80]}"')
        print(f'  outcome="{hit["outcome"]}"')
        print(f'  method="{hit["method"][:80]}"')
        print(f'  item_keys="{",".join(hit["items"])}"')
        print(f'  reason="{hit["reason"][:100]}"')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="从会话记录补经验")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--since-days", type=int, default=0)
    p_scan.add_argument("--session", default="")
    p_scan.add_argument("--limit", type=int, default=0,
                        help="最多处理多少个候选片段（先小量试跑）")

    sub.add_parser("show")

    p_mark = sub.add_parser("mark")
    p_mark.add_argument("id")
    group = p_mark.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")

    args = parser.parse_args()
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "show":
        return cmd_show(args)
    return cmd_mark(args)


if __name__ == "__main__":
    sys.exit(main())
