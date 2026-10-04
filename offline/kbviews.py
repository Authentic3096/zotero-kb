"""知识库的**分级视图**：同一篇文献，按"要读多深"分成几个级别。

## 为什么要这一层

知识库根目录点开是一堆 `papers/22X9PMR6.md`、`fulltext/23E4GXUX.md` ——
**文件名是 key，人认不出是哪篇**；要打开某一篇的某个层面，只能先记 key、
再去目录里翻。所以这里把"一篇文献有几层可看"变成**数据模型**：

    LEVELS = tldr（摘要与要点）/ card（完整档案）/ fulltext（按页正文）
             / figures（图注与表格）/ weight（权重与经验）

三处消费同一份定义，不再各写一套：
  · 面板的「打开知识库…」：文献列表 → 级别列表 → `os.startfile` 打开那个 md
  · Zotero 项的右键「打开知识库」二级菜单（插件侧有一份 `KB_LEVELS` 常量，
    由 tools/check_kb_levels.py 盯着两边一致）
  · MCP 资源 `zotero-kb://item/tldr/{key}`（它的渲染实现就搬到了本模块的
    `render("tldr", …)`，避免"两份实现必然漂移"）

## 哪些是文件、哪些要生成

    card / fulltext      现成文件（构建时写的），指过去就行，**不复制内容**
    tldr / figures / weight   由本模块生成到 views/<key>.<级别>.md

生成时机：`offline/convert.py` 在一批条目**全部提交之后**统一生成（那时
items/figures/experience 都落库了）；想单独补齐用
`python offline/maintain.py views`。views/ 与 papers/、fulltext/ 同待遇 ——
**是可重建的派生物，删了跑一次构建就回来**。
"""

from __future__ import annotations

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402
import extrafill as XF  # noqa: E402

# ---------------------------------------------------------------- 级别定义
#
# ⚠ 这份清单是**跨语言的唯一事实来源**：插件侧 zotero-plugin/src/13-kbopen.js
#   里的 KB_LEVELS 必须是它的镜像，由 tools/check_kb_levels.py 校验
#   （id / 标签 / 相对路径模板三者一致）。
#
# rel 里的 {key} 会被替换成 Zotero 的条目 key。
LEVELS = [
    {
        "id": "tldr",
        "label": "摘要与要点",
        "rel": "views/{key}.tldr.md",
        "what": "元数据 + 结构化字段 + 摘要 + 笔记要点 + 使用经验，几百 token"
                "就能判断这篇要不要深读",
    },
    {
        "id": "card",
        "label": "完整档案",
        "rel": "papers/{key}.md",
        "what": "元数据 + 摘要 + 每页首段 + 笔记与高亮标注",
    },
    {
        "id": "fulltext",
        "label": "按页正文",
        "rel": "fulltext/{key}.md",
        "what": "带 ## p.N 页码锚点的正文（图注与表格渲染在各页末尾）",
    },
    {
        "id": "figures",
        "label": "图注与表格",
        "rel": "views/{key}.figures.md",
        "what": "把散在各页末尾的图注与表格汇总成一份，方便一眼找图和表",
    },
    {
        "id": "weight",
        "label": "权重与经验",
        "rel": "views/{key}.weight.md",
        "what": "检索权重、重点标记、人工加减分，以及这篇的全部使用经验",
    },
]

# 需要本模块写文件的级别（card / fulltext 是构建时已写好的现成文件）
GENERATED_IDS = ("tldr", "figures", "weight")

_LEVEL_BY_ID = {lv["id"]: lv for lv in LEVELS}


def level(level_id: str) -> dict:
    """按 id 取级别定义（拿不到就 KeyError —— 内部调用不该静默）。"""
    return _LEVEL_BY_ID[level_id]


def level_path(level_id: str, key: str) -> str:
    """某个级别对应的文件绝对路径。"""
    return os.path.join(S.KB_DIR, level(level_id)["rel"].replace(
        "\\", "/").replace("{key}", key).replace("/", os.sep))


def level_rows(key: str) -> list[dict]:
    """给界面用的级别清单：标签、说明、路径、在不在、多大。

    只报"文件在不在"，**不承诺内容是最新的** —— 它是派生物，重建即回来。
    """
    rows = []
    for lv in LEVELS:
        path = level_path(lv["id"], key)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        rows.append({
            "id": lv["id"],
            "label": lv["label"],
            "what": lv["what"],
            "path": path,
            "exists": os.path.exists(path),
            "size": size,
        })
    return rows


# ---------------------------------------------------------------- 取 Searcher


def open_searcher():
    """开一个 Searcher（结论层读它，保证权重/经验的数字与检索一致）。

    为什么**延迟**导入：`online/` 不在离线管道的 sys.path 上，模块级 import
    会让 `offline/convert.py` 依赖 online 目录。这里按需把 online 加进来再导。
    """
    online = os.path.join(S.KB_ROOT, "online")
    if online not in sys.path:
        sys.path.insert(0, online)
    from searcher import Searcher  # noqa: PLC0415
    return Searcher()


# ---------------------------------------------------------------- 渲染


def render(level_id: str, key: str, s=None) -> str:
    """把某个级别渲染成 Markdown 文本。

    s 传了就复用调用方的 Searcher（MCP 服务器那边一次构造长期复用），
    不传就自己开一个、用完关掉。
    """
    if level_id in ("card", "fulltext"):
        return _render_file(level_id, key)
    if level_id not in GENERATED_IDS:
        raise KeyError(f"不认识的级别：{level_id}")
    own = s is None
    if own:
        s = open_searcher()
    try:
        item = s.get_item(key)
        if not item:
            # ⚠ tldr 这一级的"找不到"文案必须与搬过来的旧实现**逐字一致**：
            #   它是 MCP 资源，输出形状变了要重新训练模型的用法习惯。
            if level_id == "tldr":
                return (f"# 未找到 {key}\n\n"
                        f"知识库里没有这个 key。用 kb_search 先检索。")
            return _missing(key, "知识库里没有这个条目")
        if level_id == "tldr":
            return _render_tldr(key, item, s)
        if level_id == "figures":
            return _render_figures(key, item, s)
        return _render_weight(key, item, s)
    finally:
        if own:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def _missing(key: str, why: str) -> str:
    return (f"# 未找到 {key}\n\n{why}。\n\n"
            f"先在面板点「手动更新」，或在 Zotero 里右键这一篇"
            f"「重建知识库条目（这一篇）」。\n")


def _render_file(level_id: str, key: str) -> str:
    lv = level(level_id)
    path = level_path(level_id, key)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    return _missing(key, f"「{lv['label']}」还没生成（{path}）")


def _render_tldr(key: str, item: dict, s) -> str:
    """摘要级：模型用它判断"要不要深读"，几百 token 就能覆盖一篇。

    ⚠ 这段逻辑**原来在 online/server.py 的 res_item_tldr 里**，为了不让
      "MCP 资源"和"视图文件"变成两份实现（迟早会漂移），整段搬到这里，
      server.py 改成调 `render("tldr", …)`。搬的时候是逐字搬的。
    """
    lines = [f"# {item['title']}", ""]
    if item["author_line"]:
        lines.append(f"**作者**：{item['author_line']}")
    if item["year"]:
        lines.append(f"**年份**：{item['year']}")
    if item["venue"]:
        lines.append(f"**出处**：{item['venue']}")
    if item["collections"]:
        lines.append(f"**分类**：{' / '.join(item['collections'])}")
    lines.append(f"**标签**：{'、'.join(item['tags']) or '（无）'}")
    lines.append(f"**权重**：{item['weight']}｜全文 {item['fulltext_chars']:,} 字符"
                 f"｜标注 {item['n_annotations']} 条｜笔记 {item['n_notes']} 条")
    # 结构化字段（知网写入的那些）：一行装下，摘要级资源不占多少 token，
    # 但"中文译名 / 中图分类号 / 收录标签"往往正是"要不要深读"的决定因素。
    view = XF.describe(s.extras_for_key(key))
    if view:
        lines.append("**结构化字段**：" + "；".join(
            f"{k} {'/'.join(v) if isinstance(v, list) else v}"
            for k, v in view.items()))
    lines.append("")
    if item["abstract"]:
        lines.append("## 摘要")
        lines.append("")
        lines.append(item["abstract"][:2000])
        lines.append("")
    exp = s.experience_for_item(key, limit=5)
    if exp:
        lines.append("## 使用经验")
        lines.append("")
        lines += _experience_lines(exp)
        lines.append("")
    notes = s.read(
        "SELECT text FROM chunks WHERE item_key=? AND text LIKE '[%笔记]%' LIMIT 2", (key,)
    )
    if notes:
        lines.append("## 我的笔记（要点）")
        lines.append("")
        lines.append(notes[0]["text"][:2500])
        lines.append("")
    lines.append(f"> 完整档案：`kb/papers/{key}.md`；"
                 f"正文用 kb_fulltext 或资源 `zotero-kb://item/full/{key}`")
    return "\n".join(lines)


def _figure_rows(s, key: str):
    """这篇的图注与表格行；老库还没有 figures 表时返回 None。

    返回 None 与返回 [] 是**两件不同的事**：None 是"这张表都还没有"
    （要提示去构建），[] 是"这篇确实没有图注"。
    """
    try:
        return s.read(
            "SELECT page, kind, label, text, n_rows, n_cols FROM figures "
            "WHERE item_key = ? ORDER BY COALESCE(page, 0), fig_id", (key,))
    except Exception:  # noqa: BLE001
        return None


def _render_figures(key: str, item: dict, s) -> str:
    """图注与表格：把散在各页末尾的它们汇总成一份。

    正文里它们是**按页**挂在 `## p.N` 后面的（见 converter.figures_for_page），
    一篇 148k 字符的正文里翻一张表很痛苦，所以单独给一份按页汇总的。
    """
    rows = _figure_rows(s, key)
    if rows is None:
        return (f"# {item['title']}\n\n本库还没有图注表。\n\n"
                f"跑一次「手动更新」会从 PDF 里抽图注与表格。\n")
    lines = [f"# {item['title']}", "",
             f"> Zotero key `{key}`｜图注与表格共 {len(rows)} 项", ""]
    if not rows:
        lines += ["这篇没有抽到图注或表格。", "",
                  "可能是扫描版 PDF（没有文字层），或构建时用了「跳过图注与表格」。"]
        return "\n".join(lines)
    cur_page = object()
    for r in rows:
        page = r["page"]
        if page != cur_page:
            lines += [f"## p.{page if page is not None else '?'}", ""]
            cur_page = page
        kind = r["kind"] or ""
        tag = "表" if (kind == "table" or kind.startswith("caption-table")) else "图"
        label = (r["label"] or "").strip()
        # ⚠ label 里**本来就带**「图1」/「表2」这种前缀（抽的时候一起抓下来的），
        #   再拼一次就成了「图 图1」。所以先看它自己开头是不是那个字。
        if label and re.match(r"^(图|表|fig|figure|tab|table)", label, re.I):
            head = f"**{label}**"
        elif label:
            head = f"**{tag} {label}**"
        else:
            head = f"**{tag}**"
        if kind == "table" and r["n_rows"] and r["n_cols"]:
            head += f"（{r['n_rows']}×{r['n_cols']}）"
        lines += [head, "", (r["text"] or "").strip(), ""]
    return "\n".join(lines)


def _render_weight(key: str, item: dict, s) -> str:
    """权重与经验：这篇在检索里凭什么排前面，以及试过什么。"""
    row = s.read_one("SELECT * FROM item_weight WHERE item_key = ?", (key,))
    exp = s.experience_for_item(key, limit=100)
    lines = [f"# {item['title']}", "",
             f"> Zotero key `{key}`｜检索权重 **{item['weight']}**"
             f"（权重越高，kb_search 时越靠前）", ""]
    if row:
        lines += ["## 权重明细", "",
                  "| 项 | 值 |", "|:---|---:|",
                  f"| 有效经验 | {row['effective'] or 0} 次 |",
                  f"| 无效经验 | {row['ineffective'] or 0} 次 |",
                  f"| 部分有效 | {row['partial'] or 0} 次 |",
                  f"| 重点标记 | {'是' if row['pinned'] else '否'} |",
                  f"| 人工加减分 | {row['manual'] or 0:+.2f} |",
                  f"| 尝试总数 | {row['attempts'] or 0} 次 |",
                  f"| 最后更新 | {(row['updated_at'] or '')[:19]} |", ""]
        if row["note"]:
            lines += [f"**为什么标重点**：{row['note']}", ""]
    else:
        lines += ["这篇还没有任何经验记录或人工加减分，"
                  "权重只由发表年份决定（≈1.0）。", ""]
    lines += [f"## 使用经验（{len(exp)} 条）", ""]
    if exp:
        lines += _experience_lines(exp)
    else:
        lines += ["还没有记过。用 kb_experience_add 记一次"
                  "（「用某篇的方法试过、结果如何」），检索时它就会往前排。"]
    lines += ["", "> 经验是**攒出来的**：重建索引不会动它，"
                  "但也只有记下来才有用。"]
    return "\n".join(lines)


def _experience_lines(exp: list) -> list[str]:
    """经验条目的渲染口径（tldr 级与 weight 级共用一份）。"""
    label = {"effective": "✅ 有效", "ineffective": "❌ 无效",
             "partial": "◐ 部分", "unknown": "❓ 未验证"}
    out = []
    for e in exp:
        out.append(f"- {e['created_at'][:10]} "
                   f"{label.get(e['outcome'], e['outcome'])}"
                   f"｜{e['method'] or ''}")
        if e["reason"]:
            out.append(f"  - {e['reason']}")
    return out


# ---------------------------------------------------------------- 落盘


def write_levels(key: str, s=None) -> list[str]:
    """把这篇的生成型级别写到 views/。返回写成功的路径列表。

    `figures` 没内容时**不写文件**（而不是写一份空文件）—— 界面靠"文件在不在"
    判断，空文件会让人以为有图表。
    """
    os.makedirs(S.VIEWS_DIR, exist_ok=True)
    own = s is None
    if own:
        s = open_searcher()
    written = []
    try:
        for level_id in GENERATED_IDS:
            # `figures` 没内容时**不写文件**（而不是写一份空文件）—— 界面靠
            # "文件在不在"判断，空文件会让人以为这篇有图有表。
            # 判据直接查表，不靠"渲染出来的文字里有没有某句话"（那种判据一改
            # 文案就失效，而且失效时是静默的）。
            if level_id == "figures" and not (_figure_rows(s, key) or []):
                continue
            text = render(level_id, key, s)
            path = level_path(level_id, key)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            written.append(path)
    finally:
        if own:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
    return written


def write_many(keys, log=None) -> dict:
    """给一批 key 生成级别文件（构建收尾、maintain.py views 都走这里）。

    只开一个 Searcher 复用；单篇失败不影响别的（视图是派生物，
    生成不出来不该让整个构建失败）。
    """
    keys = [k for k in dict.fromkeys(keys) if k]
    res = {"keys": len(keys), "written": 0, "failed": []}
    if not keys:
        return res
    s = open_searcher()
    try:
        for i, key in enumerate(keys, 1):
            try:
                write_levels(key, s)
                res["written"] += 1
            except Exception as exc:  # noqa: BLE001
                res["failed"].append(f"{key}: {type(exc).__name__}: {exc}")
            if log and (i % 20 == 0 or i == len(keys)):
                log(f"    分级视图 {i}/{len(keys)}")
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    return res


def all_keys(s=None) -> list[str]:
    """库里全部条目的 key（maintain.py views 用）。"""
    own = s is None
    if own:
        s = open_searcher()
    try:
        return [r["key"] for r in s.read("SELECT key FROM items ORDER BY key")]
    finally:
        if own:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def purge(key: str) -> list[str]:
    """删掉某篇的全部生成型视图（条目被彻底移除时调用）。"""
    gone = []
    for path in glob.glob(os.path.join(S.VIEWS_DIR, f"{key}.*.md")):
        try:
            os.remove(path)
            gone.append(path)
        except OSError:
            pass
    return gone
