"""mdhtml.py —— 把知识库的 markdown 渲染成**带 MathML 的 HTML**（给 Zotero 右侧栏用）。

## 为什么公式在 Python 侧渲染，而不是在窗格里跑 JS

用户问：「zotero 有没有能在侧边栏显示 md 同时渲染公式的插件，如果没有就要我们自己解决
（vscode 我也是安装了插件，如果自己解决可以参照那个插件）」——VS Code 那类插件靠
**JS 跑 KaTeX/MathJax**。这条路在 Zotero 条目窗格里走不通：

  · 窗格 body 是主窗口的普通 DOM 节点，`innerHTML` 注入的 `<script>` **不会执行**
    （HTML 规范如此，Zotero 还有 CSP），所以没有地方跑 KaTeX；
  · 引 KaTeX 还要带一套字体文件与 CSS，而 CSS 也得注入到主窗口（能用，但字体加载
    与 CSP 都是新的未知）。

**可行的路**：Zotero 基于 Firefox，**原生支持 MathML**。所以我们在生成时把
`$$…$$` 用 `latex2mathml` 转成 MathML 写进 HTML 旁边（`<同名>.html`），窗格读 HTML 直接
`innerHTML` —— 不需要 JS、不需要字体包、完全离线。

## 用法

  · `mdhtml.write_html("…/views/ABC.tldr.md")`  → 写 `…/views/ABC.tldr.html`
  · 窗格优先读 `.html`，没有才退回"纯文本 md + 自己渲染"（那时公式是原文文本）
  · 回填历史：`python offline/mdhtml.py --all`（不重跑模型，只重渲染）
"""

from __future__ import annotations

import html as _html
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

MAX_FORMULAS = 4000          # 单文件最多渲染多少个公式（防病态文件）
MAX_HTML_BYTES = 6 * 1024 * 1024   # 单文件 HTML 上限，超了就**不写**（窗格退回 md）

_math_cache: dict[tuple[str, str], str] = {}

RE_BLOCK_MATH = re.compile(r"\$\$(.+?)\$\$", re.S)
RE_BRACKET_MATH = re.compile(r"\\\[(.+?)\\\]", re.S)
# 行内单美元：`$x^2$`（我们自己的视图只用 `$$`，但用户笔记/别的 md 里会出现）
RE_INLINE_MATH = re.compile(r"(?<!\$)\$([^$\n]+)\$(?!\$)")


def latex_to_mathml(tex: str, display: str = "inline") -> str:
    """LaTeX → MathML 字符串。**失败就退回** `<code>` 原文（绝不抛）。"""
    key = (display, tex)
    if key in _math_cache:
        return _math_cache[key]
    out = ""
    try:
        import latex2mathml.converter as C
        out = C.convert(tex, display=display)
    except Exception:      # noqa: BLE001
        out = ""
    if not out or "<math" not in out:
        # 转不了就原样显示（用户至少能看到公式长什么样，而不是一片空白）
        out = ('<code style="background:rgba(127,127,127,0.15); padding:0 3px;'
               ' border-radius:3px;">' + _html.escape(tex) + "</code>")
    _math_cache[key] = out
    return out


def _inline(text: str) -> str:
    """行内元素：公式 → MathML，粗/斜/代码/链接/图片。**先转义再套标记**。"""
    s = text

    # 公式先抽出来（占位），免得里面的 `*` `_` `[` 被当成 markdown
    slots: list[str] = []

    def stash(m, disp):
        slots.append(latex_to_mathml(m.group(1).strip(), disp))
        return "\x00M%d\x00" % (len(slots) - 1)

    s = RE_BLOCK_MATH.sub(lambda m: stash(m, "inline"), s)
    s = RE_BRACKET_MATH.sub(lambda m: stash(m, "block"), s)
    # 行内单美元放最后：`$$…$$` 已经被换成占位符，剩下的 `$…$` 才是行内公式
    s = RE_INLINE_MATH.sub(lambda m: stash(m, "inline"), s)

    s = _html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(^|[^*])\*([^*\n]+)\*", r"\1<em>\2</em>", s)
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[图：\1]", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)

    # 占位换回 MathML（此时已转义完，MathML 是可信的我们自己生成的标签）
    for i, frag in enumerate(slots):
        s = s.replace("\x00M%d\x00" % i, frag)
    return s


def render(md_text: str) -> str:
    """把知识库的 markdown 渲染成 HTML **片段**（不含 html/head，直接 innerHTML）。"""
    out: list[str] = []
    list_kind = ""
    buf: list[str] = []

    def flush_buf():
        nonlocal buf
        if buf:
            out.append("<p>" + "<br>".join(buf) + "</p>")
            buf = []

    def close_list():
        nonlocal list_kind
        if list_kind:
            out.append("</%s>" % list_kind)
            list_kind = ""

    for raw in str(md_text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_buf()
            close_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_buf(); close_list()
            lvl = len(m.group(1))
            size = {1: "15px", 2: "13.5px", 3: "12.5px"}.get(lvl, "12px")
            out.append('<div style="font-weight:600; font-size:%s; margin:6px 0 2px;">%s</div>'
                       % (size, _inline(m.group(2))))
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            flush_buf()
            if list_kind != "ul":
                close_list()
                out.append('<ul style="margin:2px 0 2px 16px;">')
                list_kind = "ul"
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if m:
            flush_buf()
            if list_kind != "ol":
                close_list()
                out.append('<ol style="margin:2px 0 2px 18px;">')
                list_kind = "ol"
            out.append("<li>" + _inline(m.group(2)) + "</li>")
            continue
        m = re.match(r"^>\s?(.*)$", line)
        if m:
            flush_buf(); close_list()
            out.append('<blockquote style="margin:4px 0; padding:1px 6px;'
                       ' border-left:3px solid rgba(127,127,127,0.5); opacity:0.85;">'
                       + _inline(m.group(1)) + "</blockquote>")
            continue
        # 独立成行的公式块：$$…$$ / \[…\] → display=block
        m = re.match(r"^\s*\$\$(.+)\$\$\s*$", line) or re.match(r"^\s*\\\[(.+)\\\]\s*$", line)
        if m:
            flush_buf(); close_list()
            out.append('<div style="margin:6px 0; overflow-x:auto;">'
                       + latex_to_mathml(m.group(1).strip(), "block") + "</div>")
            continue
        buf.append(_inline(line))
    flush_buf(); close_list()
    return "\n".join(out)


def html_path_for(md_path: str) -> str:
    return re.sub(r"\.md$", ".html", md_path)


def write_html(md_path: str, html_path: str = "") -> str:
    """给一份知识库 md 写同名 `.html`。返回写出的路径；跳过/失败返回空串。"""
    md_path = str(md_path or "")
    if not md_path or not os.path.isfile(md_path):
        return ""
    try:
        with io.open(md_path, encoding="utf-8") as fh:
            md = fh.read()
    except OSError:
        return ""
    if md.count("$$") // 2 > MAX_FORMULAS:
        return ""                     # 病态文件：宁可不给 HTML，也别卡住窗格
    frag = render(md)
    if len(frag.encode("utf-8")) > MAX_HTML_BYTES:
        return ""
    dst = html_path or html_path_for(md_path)
    try:
        with io.open(dst, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(frag)
    except OSError:
        return ""
    return dst


def _kb_md_files():
    import schemas as S
    for d in (S.VIEWS_DIR, S.PAPERS_DIR, S.FULLTEXT_DIR):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md"):
                yield os.path.join(d, fn)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="知识库 md → 带 MathML 的 html（窗格公式渲染）")
    ap.add_argument("path", nargs="?", default="", help="单个 md 文件；不给就配 --all")
    ap.add_argument("--all", action="store_true", help="把 kb/ 里所有 md 都渲染一遍")
    ap.add_argument("--force", action="store_true", help="已存在也重写")
    args = ap.parse_args(argv)

    files = []
    if args.all:
        files = list(_kb_md_files())
    elif args.path:
        files = [args.path]
    if not files:
        print("要给一个 md 路径，或用 --all")
        return 2

    n_ok = n_skip = n_fail = 0
    for p in files:
        dst = html_path_for(p)
        if not args.force and os.path.isfile(dst) \
                and os.path.getmtime(dst) >= os.path.getmtime(p):
            n_skip += 1
            continue
        out = write_html(p)
        if out:
            n_ok += 1
        else:
            n_fail += 1
    print(f"  渲染完成：写 {n_ok} 个、跳过 {n_skip} 个（已是最新）、失败 {n_fail} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
