"""提示词注册表：把所有"喂给模型的固定话术"集中到一处，用户可看、可改、可还原。

## 为什么

改之前这些话术**硬编码**在 5 个地方，用户看不到、改不了：

    judge.py:495  TAG_SYSTEM / SUMMARY_SYSTEM / EXTRACT_SYSTEM
    judge.py      tag_item / summarize_item / extract_experience 里内联的 user 模板
    metafill.py   MODEL_SYSTEM / MODEL_PROMPT
    check_chunks.py  ITEM_PROMPT

其中 `EXTRACT_SYSTEM`（从对话里抽经验）**用户明确说抽得不对** —— 抽出来一堆
跟文献无关的工具链调试记录。这类口径问题只能靠"用户能自己改一句话"解决，
不能每次都等改代码。

## 覆盖文件与默认值

    kb/prompts.json     用户改动（只存改过的字段，没改的跟着默认走）
    本文件的 SPECS      默认值 —— **与搬进来之前的字面量逐字相同**

"逐字相同"有回归网兜着：`tests/test_prompts.py` 拿 `_prompt_goldens.json`
（搬家前从真机抓的 5 次真实调用）逐字比对，改了字就红。

## 渲染方式：`{占位符}` 替换，**不是** `str.format`

这几条提示词里**本来就含 JSON 示例**（`{"tags": [...]}`）。用 `str.format`
就得把每个大括号写成 `{{`，用户看到的默认文本会是一堆双括号 —— 既难读，
用户自己写 `{` 时也会当场炸。所以 `render()` 只做
`text.replace("{" + name + "}", value)`，大括号该是什么样就是什么样。

## 校验（"改坏了在保存时被拒"，而不是在调用时炸）

保存时检查两件事：
  1. **占位符还在**：`{text}` 这类必须保留，否则模型收到的提示词里没有正文；
  2. **契约键还在**：`keys` 列的 JSON 字段名必须还出现在文本里 ——
     它们是解析输出时的依据，删掉就可能静默解析失败。
"""

from __future__ import annotations

import json
import os

import schemas as S


def path() -> str:
    """覆盖文件的位置（**惰性计算**：知识库目录可以中途切走）。"""
    return os.path.join(os.path.dirname(S.INDEX_DB), "prompts.json")


# ---------------------------------------------------------------- 默认话术
#
# ⚠ 每个 system / user 都必须是**搬家前那份字面量的逐字副本**。
#   想改口径请改这里（那会同时改变默认值），或者让用户在面板里覆盖。

SPECS: dict[str, dict] = {
    "tag": {
        "title": "给文献打标签",
        "where": "自动打标签；面板「AI」页；kb_item 的标签建议",
        "placeholders": ["title", "abstract"],
        "keys": ["tags", "topic"],
        "system": (
            "你是文献管理助手。只根据给定的标题与摘要输出标签，不要编造内容，"
            "不要解释。输出 JSON。"
        ),
        "user": (
            "请为下面这篇文献给出 3-6 个中文标签（主题词或方法名，不要泛词如"
            "「研究」「论文」）。\n"
            '输出 JSON：{"tags": ["标签1", "标签2"], "topic": "一句话主题（不超过 20 字）"}\n\n'
            "标题：{title}\n\n摘要：{abstract}"
        ),
    },
    "summary": {
        "title": "写文献要点",
        "where": "面板「AI」页的要点；部分视图的摘要来源",
        "placeholders": ["title", "text"],
        "keys": ["points", "method", "keywords"],
        "system": (
            "你是文献阅读助手。用中文写要点，只依据给定文字，不要引入外部知识，"
            "不要评价好坏。输出 JSON。"
        ),
        "user": (
            "用 2-3 句中文概括下面这篇文献做了什么、用了什么方法、结论是什么。\n"
            '输出 JSON：{"points": ["句1", "句2"], "method": "核心方法名", '
            '"keywords": ["关键词"]}\n\n'
            "标题：{title}\n\n正文节选：\n{text}"
        ),
    },
    "extract": {
        "title": "从对话里抽经验（learn.py 用）",
        "where": "offline/learn.py 扫会话记录时逐段调用",
        "placeholders": ["text"],
        "keys": ["found", "items", "asked", "method", "outcome", "reason"],
        "system": (
            "你是技术记录整理助手。从给定对话片段里提取「用过什么方法、结果如何」的事实。"
            "严格只提取文字里明确写出的内容；没写的一律留空，绝不推测。"
            "outcome 只能取 effective / ineffective / partial / unknown 之一；"
            "文字没有明确结论时必须用 unknown。输出 JSON。"
        ),
        "user": (
            "从下面的对话片段中提取「用过什么方法、结果如何」的记录。\n\n"
            "输出 JSON：\n"
            "{\n"
            '  "found": true/false,          // 片段里有没有可提取的经验\n'
            '  "items": [{\n'
            '    "asked": "要解决的问题（没写就留空字符串）",\n'
            '    "method": "用了什么方法（没写就留空）",\n'
            '    "outcome": "effective|ineffective|partial|unknown",\n'
            '    "reason": "为什么有效/失败（没写就留空）",\n'
            '    "context": "前提条件（没写就留空）",\n'
            '    "item_refs": ["提到的文献标题或编号片段"],\n'
            '    "evidence": "引用原文里支持这个结论的短句（不超过 60 字）",\n'
            '    "confidence": 0.0-1.0        // 你对这条提取有多确信\n'
            "  }]\n"
            "}\n\n"
            "重要：只提取文字里明确写出的结论。凡是没写的一律留空。"
            "宁可 found=false，也不要推测。\n\n"
            "对话片段：\n{text}"
        ),
    },
    "metafill": {
        "title": "从首页正文补元数据",
        "where": "面板「元数据」页的模型兜底（规则没搞定、且字段为空时才问）",
        "placeholders": ["text", "creator_hint", "creator_rules"],
        "keys": ["date", "date_kind", "DOI", "volume", "issue", "pages", "evidence"],
        "system": (
            "你是文献元数据抽取助手。只从给定文字里抄出信息，不要推测、不要补全、"
            "不要用外部知识。文字里没有的字段一律留空字符串。输出 JSON。"
        ),
        # ⚠ 这里的 JSON 示例是**单**大括号：搬家前它走 str.format（模板里写 {{），
        #   现在走占位符替换，所以模板里就写单括号、输出完全一致。
        "user": """从下面这段文献正文（首页节选）里，抄出这些元数据字段的值：
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
{"date": "", "date_kind": "", "DOI": "", "volume": "", "issue": "", "pages": "",
  "creators": [], "creators_partial": false,
  "evidence": {"date": "", "DOI": "", "volume": "", "issue": "", "pages": "",
                "creators": ""}}""",
    },
    "chat": {
        "title": "窗格聊天（按需注入上下文后的问答）",
        "where": "Zotero 右侧内容窗格「本地模型」分区的普通对话",
        "placeholders": ["context", "history", "question"],
        "keys": [],
        "system": (
            "你是这篇文献的阅读助手，只在用户给的上下文范围内回答。"
            "上下文没写的内容不要编；不确定就直说「这篇里没有」。"
            "回答用中文、简洁、直接给结论，不要客套。"
            "如果用户的问题提示某个结论应当记进经验库或修正正文，"
            "**不要自行写入** —— 只用一句话说明你建议记什么、改哪里，"
            "由界面带着用户确认。"
        ),
        "user": """【这篇文献的上下文】
{context}

【已经聊过的】（可能为空）
{history}

【用户现在问】
{question}""",
    },
    "para": {
        "title": "逐段检查（提取损坏 / 边界 / 顺序 / 碎片）",
        "where": "窗格里「全文级段落检测」逐段调用；面板「解析健康」复核也用它",
        "placeholders": ["page", "signals", "prev_tail", "next_head", "text"],
        "keys": ["verdict", "kind", "reason"],
        # ⚠ 这一条的每一条判据都必须**可核对**：界面会把客观信号与结论并排显示。
        #   历史上吃过一次亏：让模型对每个切片"读着像不像句子"打分，
        #   结果判错 60%，同一模型对同一文本给出相反结论（见 check_chunks.py 开头）。
        "system": (
            "你在核对一篇论文的**正文提取质量**。一次只看一段，逐条判断下面四件事：\n"
            "1. 提取损坏：有没有乱码、符号汤（如矩阵碎片 `⎡BX BY BZ⎦`）、英文断词、"
            "页眉页脚混入正文？\n"
            "2. 段落边界：这一段和上一段/下一段是不是**本来同一段**被拆开了"
            "（典型是跨页，或上一段末尾是小写/逗号）？或者这一段里是不是**粘了"
            "两段**（中间有明显的另一段开头）？\n"
            "3. 阅读顺序：双栏排版有没有被读成交错（句子在栏宽处硬切、"
            "上下句接不上、页眉页码夹在段中间）？\n"
            "4. 碎片混入：公式、表格碎片、参考文献碎片是不是被当成正文了？\n\n"
            "只给**最小修正**：能替换几个字就别重写整段。"
            "拿不准时 verdict 给 ok 或 unsure —— 宁可放过，也不要把正常的"
            "学术文本（公式、参考文献、表格、封面）判成坏的。\n"
            "输出 JSON。"
        ),
        "user": """页码：p.{page}

【服务端算出来的客观信号】（不依赖模型，可与你的结论对照）
{signals}

【上一段末尾】
{prev_tail}

【本段】
{text}

【下一段开头】
{next_head}

输出 JSON：
{{"verdict": "ok|suspect|damaged|unsure",
  "kind": "text|boundary|order|fragment|none",
  "join_with": "prev|next|",
  "before": "要被替换掉的那一小段原文（没有就空串）",
  "after": "替换成什么（没有就空串）",
  "reason": "一句话理由（要能对应上面某条信号）"}}""",
    },
    "propose": {
        "title": "把对话里「该记的东西」整理成写入建议",
        "where": "窗格的写入模式（用户确认后才落库）",
        "placeholders": ["instruction", "key", "transcript"],
        "keys": ["experiences", "weights", "patches"],
        # ⚠ 这是"经验库会被污染"的那条路径，口径必须收得很紧：
        #   经验库参与**检索加权**，工具链/工程类的记录进错地方会让排序变脏。
        "system": (
            "你在把一段关于某篇文献的讨论，整理成**可以落库的改动建议**。"
            "三条硬口径：\n"
            "1. 经验只记**与文献内容或研究方法有关**的尝试与结论"
            "（用了什么方法、结果如何、为什么）。"
            "工程/工具链/配置/安装/打包这类「关于知识库自己怎么搭」的内容"
            "**一律不算经验**，不要放进 experiences。\n"
            "2. 只记讨论里**明确写出**的结论；没写的留空，绝不推测。"
            "outcome 只能 effective / ineffective / partial / unknown，"
            "没明确结论就用 unknown。\n"
            "3. 正文修正只给**最小改动**（before 必须是原文里能逐一找到的片段）。"
            "找不到确切原文就不要给 patches。\n"
            "输出 JSON，没有内容的数组给空数组。"
        ),
        "user": """文献 key：{key}
用户的意图：{instruction}

【讨论片段】
{transcript}

输出 JSON：
{{"experiences": [{{"asked": "", "outcome": "unknown", "method": "",
                 "reason": "", "context": "", "evidence": "", "tags": [],
                 "item_keys": []}}],
  "weights": [{{"key": "", "pinned": null, "manual": null, "note": ""}}],
  "patches": [{{"page": 0, "kind": "text", "before": "", "after": "",
               "note": ""}}],
  "joins": [{{"page": 0, "para_index": 0, "with_prev": 1, "reason": ""}}]}}""",
    },
    "draft": {
        "title": "把用户的大白话整理成一条经验",
        "where": "面板「经验库 → 修改/增添经验」（有本地模型时）",
        "placeholders": ["text"],
        "keys": ["asked", "outcome", "method"],
        "system": (
            "用户在口述一条**文献研究经验**，你把它整理成结构化的一条。"
            "只整理，不添加用户没说的内容；字段说不清就留空字符串。"
            "outcome 只能 effective / ineffective / partial / unknown，"
            "用户没说效果就用 unknown。"
            "item_keys 只填用户明确提到的文献（8 位 Zotero key），"
            "拿不准就留空数组 —— 关联错的文献会让权重加到别的论文上。"
            "另外给出你判断的「这条像不像已有经验」是给用户看的建议，"
            "不要替用户决定是新增还是修改。输出 JSON。"
        ),
        "user": """用户口述：
{text}

输出 JSON：
{{"asked": "", "outcome": "unknown", "method": "", "context": "",
  "reason": "", "evidence": "", "tags": [], "item_keys": [],
  "similar_hint": "一句话：这条大概是在讲什么，便于用户确认"}}""",
    },
    "chunks": {
        "title": "判「整篇提取是否失败」（解析健康灰区）",
        "where": "offline/check_chunks.py 的灰区判定（一次只看一篇）",
        "placeholders": ["fragments"],
        # 契约键：模型必须回 failed / reason
        "keys": ["failed", "reason"],
        # ⚠ 这里的 JSON 示例是**双**大括号：搬家前它用 `.replace()` 渲染，
        #   双括号是会原样出现在提示词里的（历史上的小瑕疵），
        #   为了"逐字不变"这里保持原样。
        "user": """下面是**同一篇论文**正文的若干片段（从全文不同位置均匀抽取）。

请判断：这篇论文的正文提取是否**整体失败**了？

只有下面几种情况算「整体失败」：
- **字符整体错乱**：英文字母像被整体位移过（`&DOFXODWLRQ` 本该是 `Calculation`）
- **编码错乱**：混入藏文、天城文等不该出现的字符
- **中文全丢**：中文文献里中文几乎没了，只剩数字、标点、孤立字母

⚠⚠ 下面这些**都不算失败**，不要判失败：
- 公式、矩阵、符号、上下标（论文里本来就有）
- 参考文献列表（本来就零碎、编号多）
- 表格内容（本来就是孤立单词和数字）
- 封面、目录、页眉页脚、基金信息
- 某一段的开头或结尾不完整（切片/分页造成的）

拿不准时判**没失败** —— 宁可放过，也不要误伤正常文献。

{fragments}

只输出 JSON：{{"failed": true 或 false, "reason": "一句话理由"}}""",
    },
}

# 给界面用：这一条提示词"用在哪"
FIELDS = ("system", "user")


def default(pid: str, field_name: str = "system") -> str:
    """注册表里的默认话术。"""
    spec = SPECS.get(pid) or {}
    return str(spec.get(field_name) or "")


def load() -> dict:
    """默认值 + 用户覆盖（每次调用都读，改了立即生效，不用重启服务）。"""
    out = {pid: {f: default(pid, f) for f in FIELDS} for pid in SPECS}
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for pid, fields in (data.get("overrides") or {}).items():
        if pid not in out or not isinstance(fields, dict):
            continue
        for f in FIELDS:
            if isinstance(fields.get(f), str) and fields[f].strip():
                out[pid][f] = fields[f]
    return out


def get(pid: str, field_name: str = "system") -> str:
    return str((load().get(pid) or {}).get(field_name) or "")


def render(pid: str, **kwargs) -> str:
    """把 `{占位符}` 换成实际内容（**不用 str.format**，见模块头说明）。"""
    text = get(pid, "user")
    for name, value in kwargs.items():
        text = text.replace("{" + name + "}", "" if value is None else str(value))
    return text


def is_custom(pid: str, field_name: str = "") -> bool:
    """这一条（或这条的某个字段）是否被用户改过。"""
    cur = load()
    names = [field_name] if field_name else list(FIELDS)
    return any(cur.get(pid, {}).get(f) != default(pid, f) for f in names)


def validate(pid: str, field_name: str, text: str) -> str:
    """返回错误信息（空串=通过）。保存前调用，别让改坏的话术到运行时才炸。"""
    if pid not in SPECS:
        return f"没有这一条提示词：{pid}"
    if field_name not in FIELDS:
        return f"字段只能是 {list(FIELDS)}"
    if not (text or "").strip():
        return "不能存空提示词"
    spec = SPECS[pid]
    if field_name == "user":
        missing = [p for p in spec.get("placeholders", []) if "{" + p + "}" not in text]
        if missing:
            return ("缺少占位符 " + "、".join("{" + m + "}" for m in missing)
                    + " —— 少了它模型就收不到那部分内容（会显得「模型变笨了」，"
                      "而报错却在别处）")
    for key in spec.get("keys", []):
        if key not in text and field_name == "user":
            return (f"契约键 `{key}` 不见了 —— 输出解析靠它，"
                    f"删掉就可能静默解析失败")
    return ""


def save(pid: str, field_name: str, text: str) -> str:
    """保存一条覆盖。校验不过抛 ValueError。返回文件路径。"""
    err = validate(pid, field_name, text)
    if err:
        raise ValueError(err)
    data = {"version": 1, "overrides": {}}
    try:
        with open(path(), encoding="utf-8") as fh:
            old = json.load(fh)
        if isinstance(old, dict) and isinstance(old.get("overrides"), dict):
            data["overrides"] = old["overrides"]
    except (OSError, json.JSONDecodeError):
        pass
    data["overrides"].setdefault(pid, {})[field_name] = text
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    return path()


def reset(pid: str = "", field_name: str = "") -> str:
    """还原默认：给 pid 就只还原它，留空还原全部。"""
    data = {"version": 1, "overrides": {}}
    try:
        with open(path(), encoding="utf-8") as fh:
            old = json.load(fh)
        if isinstance(old, dict) and isinstance(old.get("overrides"), dict):
            data["overrides"] = old["overrides"]
    except (OSError, json.JSONDecodeError):
        pass
    if not pid:
        data["overrides"] = {}
    elif field_name:
        data["overrides"].get(pid, {}).pop(field_name, None)
        if not data["overrides"].get(pid):
            data["overrides"].pop(pid, None)
    else:
        data["overrides"].pop(pid, None)
    if data["overrides"]:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        with open(path(), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    elif os.path.exists(path()):
        os.remove(path())
    return path()


def status() -> list[dict]:
    """给面板「提示词」页用的清单。"""
    out = []
    for pid, spec in SPECS.items():
        out.append({
            "id": pid,
            "title": spec.get("title", pid),
            "where": spec.get("where", ""),
            "placeholders": list(spec.get("placeholders", [])),
            "keys": list(spec.get("keys", [])),
            "custom": [f for f in FIELDS if is_custom(pid, f)],
            "system": get(pid, "system"),
            "user": get(pid, "user"),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    """`python offline/prompts.py list|show <id>|reset [id]`：命令行看一眼。"""
    import argparse
    ap = argparse.ArgumentParser(description="提示词注册表")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["list", "show", "reset"])
    ap.add_argument("pid", nargs="?", default="")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        print(f"  覆盖文件：{path()}"
              f"{'（存在）' if os.path.exists(path()) else '（尚未创建）'}\n")
        for row in status():
            mark = ("  ✎ " + ",".join(row["custom"])) if row["custom"] else ""
            print(f"  [{row['id']}] {row['title']}{mark}")
            print(f"        用在哪：{row['where']}")
            print(f"        占位符：{', '.join(row['placeholders']) or '（无）'}"
                  f"｜契约键：{', '.join(row['keys'])}")
        return 0
    if args.cmd == "show":
        if args.pid not in SPECS:
            print(f"  没有这一条：{args.pid}（可选：{', '.join(SPECS)}）")
            return 1
        for f in FIELDS:
            print(f"  ---- {args.pid}.{f} ----")
            print(get(args.pid, f))
            print()
        return 0
    reset(args.pid)
    print(f"  已还原{('「' + args.pid + '」') if args.pid else '全部'}为默认")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
