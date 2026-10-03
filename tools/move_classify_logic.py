"""把分类建议算法从 localserver 抽到 judge.py（服务端与 CLI 共用一份）。

    python tools/move_classify_logic.py --dry
    python tools/move_classify_logic.py

背景（用户反馈）：
    管理面板点「生成分类建议」弹的是"还没做" —— 因为 do_taxonomy() 在等一个
    `offline/taxonomy.py`，而那个文件从来没写过。但**服务端早就有完整实现**
    （localserver.do_classify_one，few-shot + JSON 模式，插件正在用）。

    与其在面板里另写一份（两份提示词一定会漂），不如把它抽到 judge.py：
        judge.classify_item(...)   ← localserver 与 CLI 共用
        judge.py classify          ← 面板直接调这个

    这样口径统一、模型配置也统一（服务端读插件设置传来的 model）。

为什么用脚本做这次搬移：
    这类"跨文件 relocation"用 shell 内联 Python 很容易踩转义/切错位置的坑
    （本机在加路径设置时就栽过两次）。写成脚本可干跑、可自检语法。
"""

from __future__ import annotations

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

JUDGE = os.path.join(ROOT, "offline", "judge.py")
SERVER = os.path.join(ROOT, "online", "localserver.py")

# 插到 judge.py 的这个函数之前
ANCHOR = "def cmd_status() -> int:"

NEW_CODE = '''def list_categories() -> list[dict]:
    """从知识库现有条目聚合分类（名字 + 篇数 + 标题样例）。

    样例是 few-shot 的关键：让模型看到"这个库里什么文献归到哪"的实际口径，
    比只给分类名准得多。
    """
    import json as _json

    import schemas as S

    conn = S.connect(S.INDEX_DB)
    try:
        mapping: dict[str, list[str]] = {}
        for row in conn.execute("SELECT title, collections FROM items"):
            for name in _json.loads(row["collections"] or "[]"):
                mapping.setdefault(name, []).append(row["title"])
        return [{"name": n, "count": len(t), "sample": t[:4]}
                for n, t in sorted(mapping.items())]
    finally:
        conn.close()


def classify_item(item_meta: dict, categories: list[dict] | None = None,
                  model: str = "") -> dict:
    """给一篇文献推荐分类与标签（**插件与服务端共用这一份**）。

    设计约束（来自用户）：
      · 每篇文献只归到**一个**分类（不做多归属）——提示词明确要求单选；
      · 分类最多两层，但只推荐**已有分类**，不擅自造新分类；
      · 分类列表明细可由调用方传入（插件可配置），没传就用知识库现有的。

    ⚠ 这份逻辑原本只写在 `online/localserver.py` 的 do_classify_one 里。
       抽出到这里是因为管理面板也要用 —— 两份实现必然漂移，提示词一改就不同步。
       改动时请只改这里，不要在两处各写一份。
    """
    ok, _models = ollama_available()
    if not ok:
        return {"error": "本地模型服务不可用", "hint": "启动 Ollama 后重试"}
    model = model or pick_model()

    if not categories:
        categories = list_categories()

    cat_lines = []
    for c in categories[:40]:
        samples = "；".join(str(x)[:44] for x in (c.get("sample") or [])[:4])
        cat_lines.append(f"- {c['name']}（{c.get('count', '?')} 篇）"
                         + (f"\\n    样例：{samples}" if samples else ""))

    title = item_meta.get("title") or "(无标题)"
    abstract = (item_meta.get("abstract") or "")[:1200]
    tags = item_meta.get("tags") or []
    prompt = (
        "任务：给一篇新加入文献库的文献，从**已有分类里选一个**最合适的。\\n\\n"
        "【已有分类】\\n" + "\\n".join(cat_lines) + "\\n\\n"
        "【待分类文献】\\n"
        f"标题：{title}\\n"
        f"摘要：{abstract or '(无摘要)'}\\n"
        + (f"已有标签：{', '.join(tags[:12])}\\n" if tags else "")
        + "\\n要求：\\n"
        "1. category 必须是上面列出的分类名之一，**原样照抄**，不要改写、不要造新分类。\\n"
        "2. 只选**一个**最合适的。若都不合适，category 用空字符串并说明。\\n"
        "3. 另外给 3-6 个中文标签（可复用「已有标签」里合适的）。\\n"
        '输出 JSON：{"category": "分类名", "confidence": 0.0-1.0, '
        '"reason": "为什么归到这里（一句）", "tags": ["标签"]}'
    )
    res = generate(prompt, model=model, timeout=240)
    if not res.get("ok"):
        return {"error": f"模型调用失败：{res.get('error')}"}
    good, data = parse_json(res["text"])
    if not good or not isinstance(data, dict):
        return {"error": "模型输出无法解析", "raw": str(res["text"])[:400]}

    valid_names = {c["name"] for c in categories}
    pick = str(data.get("category") or "").strip()
    # 模型偶尔会改写分类名（加书名号、去空格）。做一次宽松匹配兜住，
    # 但只接受"唯一命中" —— 含糊就不认，宁可不给建议，也不塞进错的分类。
    resolved = pick if pick in valid_names else ""
    if pick and not resolved:
        loose = [n for n in valid_names
                 if pick.strip("《》「」\\"' ") == n
                 or pick.strip("《》「」\\"' ") in n
                 or n in pick]
        if len(loose) == 1:
            resolved = loose[0]

    tags_out = data.get("tags") or []
    if isinstance(tags_out, str):
        tags_out = [t.strip() for t in tags_out.replace("，", ",").split(",")
                    if t.strip()]
    return {
        "category": resolved,
        "raw_category": pick,
        "confidence": float(data.get("confidence") or 0.0),
        "reason": str(data.get("reason") or "")[:300],
        "tags": [str(t) for t in tags_out][:8],
        "model": model,
        "known_categories": sorted(valid_names),
    }


def cmd_classify(limit: int, key: str, use_model: bool) -> int:
    """命令行：给文献生成分类建议（管理面板「生成分类建议」走这里）。

    use_model=False 时只打印确定性的分类分布，不调模型（快、离线可用）。
    """
    import schemas as S

    conn = S.connect(S.INDEX_DB)
    try:
        if key:
            rows = conn.execute(
                "SELECT key, title, abstract, tags, collections FROM items "
                "WHERE key = ?", (key,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, title, abstract, tags, collections FROM items "
                "WHERE title <> '' ORDER BY key LIMIT ?", (limit,)).fetchall()
        if not rows:
            print("索引里没有可处理的条目")
            return 1

        cats = list_categories()
        print(f"知识库现有 {len(cats)} 个分类：")
        for c in cats:
            print(f"  {c['name']:26} {c['count']:>4} 篇")
        print()

        if not use_model:
            print("（--no-model：只列分类分布，未调用模型）")
            # 顺手报一下"还没归类"的条目有多少，便于决定要不要跑模型
            n_none = conn.execute(
                "SELECT COUNT(*) FROM items WHERE collections IS NULL "
                "OR collections IN ('', '[]')").fetchone()[0]
            print(f"未归类条目：{n_none} 篇")
            return 0

        ok, models = ollama_available()
        if not ok:
            print("  [XX] 本地模型服务不可用。先启动 Ollama。")
            return 1
        model = pick_model()
        print(f"用模型：{model}（可选：{', '.join(models[:5])}）")
        print(f"给 {len(rows)} 篇生成建议（每篇一次调用，可能要等一会儿）…\\n")

        import json as _json

        for row in rows:
            cur = _json.loads(row["collections"] or "[]")
            meta = {
                "title": row["title"],
                "abstract": row["abstract"] or "",
                "tags": _json.loads(row["tags"] or "[]"),
            }
            res = classify_item(meta, cats)
            head = f"  {row['key']}  {str(row['title'])[:40]}"
            if res.get("error"):
                print(f"{head}\\n      [XX] {res['error']}")
                continue
            cur_s = "、".join(cur) or "（未归类）"
            cat = res["category"] or "（无合适分类）"
            mark = "=" if (res["category"] and res["category"] in cur) else "→"
            print(f"{head}")
            print(f"      现在：{cur_s}")
            print(f"      建议：{cat}   置信 {res['confidence']:.2f}   {mark}")
            if res.get("raw_category") and res["raw_category"] != res["category"]:
                print(f"      （模型原话「{res['raw_category']}」已按已知分类归一）")
            if res.get("reason"):
                print(f"      理由：{res['reason']}")
            if res.get("tags"):
                print(f"      标签：{'、'.join(res['tags'])}")
        print("\\n提示：这只是建议，不会自动改动 Zotero。"
              "要真正归类请用插件的「一键应用」，或跑 zotero_sync.py。")
        return 0
    finally:
        conn.close()


'''


def main() -> int:
    dry = "--dry" in sys.argv
    jsrc = open(JUDGE, encoding="utf-8").read()

    if "def classify_item(" in jsrc:
        print("  [--] judge.py 已有 classify_item，跳过插入")
    else:
        i = jsrc.find(ANCHOR)
        if i < 0:
            print(f"  [XX] judge.py 里找不到锚点：{ANCHOR}")
            return 1
        new = jsrc[:i] + NEW_CODE + jsrc[i:]
        try:
            ast.parse(new)
        except SyntaxError as exc:
            print(f"  [XX] 插入后 judge.py 语法错误：{exc}")
            return 1
        if dry:
            print(f"  [dry] 会在 judge.py 第 {jsrc[:i].count(chr(10)) + 1} 行前插入"
                  f"（{len(NEW_CODE)} 字符）")
        else:
            open(JUDGE, "w", encoding="utf-8").write(new)
            print(f"  [OK] judge.py 已插入 classify_item / list_categories / cmd_classify")

    # ---- 2. CLI 子命令
    jsrc = open(JUDGE, encoding="utf-8").read()
    if 'add_parser("classify")' in jsrc:
        print("  [--] CLI 已有 classify 子命令")
    else:
        old_cli = '''    p_ext = sub.add_parser("extract")
    p_ext.add_argument("--file", required=True)'''
        new_cli = '''    p_ext = sub.add_parser("extract")
    p_ext.add_argument("--file", required=True)
    p_cls = sub.add_parser("classify")
    p_cls.add_argument("--limit", type=int, default=10)
    p_cls.add_argument("--key", default="")
    p_cls.add_argument("--no-model", action="store_true",
                       help="只列分类分布，不调模型")'''
        if old_cli not in jsrc:
            print("  [XX] judge.py 的 CLI 段落没匹配上")
            return 1
        jsrc = jsrc.replace(old_cli, new_cli)
        old_disp = '''    if args.cmd == "summarize":
        return cmd_summarize(args.key, args.write)
    return cmd_extract(args.file)'''
        new_disp = '''    if args.cmd == "summarize":
        return cmd_summarize(args.key, args.write)
    if args.cmd == "classify":
        return cmd_classify(args.limit, args.key, not args.no_model)
    return cmd_extract(args.file)'''
        if old_disp not in jsrc:
            print("  [XX] judge.py 的分发段落没匹配上")
            return 1
        jsrc = jsrc.replace(old_disp, new_disp)
        try:
            ast.parse(jsrc)
        except SyntaxError as exc:
            print(f"  [XX] 改 CLI 后语法错误：{exc}")
            return 1
        if not dry:
            open(JUDGE, "w", encoding="utf-8").write(jsrc)
            print("  [OK] judge.py 已加 classify 子命令")

    # ---- 3. localserver 改为复用
    ssrc = open(SERVER, encoding="utf-8").read()
    if "judge.classify_item" in ssrc:
        print("  [--] localserver 已在复用 judge.classify_item")
    else:
        # 找 do_classify_one 的函数体范围（到下一个顶层 def 之前）
        m = re.search(r"def do_classify_one\(.*?\n(?=def )", ssrc, re.S)
        if not m:
            print("  [XX] 找不到 do_classify_one")
            return 1
        new_fn = '''def do_classify_one(item_meta: dict, categories: list[dict],
                    model: str = "") -> dict:
    """给**一篇新文献**推荐分类与标签（插件弹窗用）。

    ⚠ 算法已抽到 `judge.classify_item` —— 管理面板的 CLI 也调那一份。
       这里只做转发，**不要**再往这里写提示词逻辑（两份必然漂移）。
    """
    import judge

    return judge.classify_item(item_meta, categories, model)


'''
        ssrc = ssrc[:m.start()] + new_fn + ssrc[m.end():]
        try:
            ast.parse(ssrc)
        except SyntaxError as exc:
            print(f"  [XX] 改 localserver 后语法错误：{exc}")
            return 1
        if not dry:
            open(SERVER, "w", encoding="utf-8").write(ssrc)
            print("  [OK] localserver.do_classify_one 已改为转发 judge.classify_item")

    print("\n完成。验证：")
    print("  .venv\\Scripts\\python.exe offline\\judge.py classify --limit 3 --no-model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
