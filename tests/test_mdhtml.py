"""mdhtml（LaTeX → MathML，给 Zotero 右侧栏渲染公式）的单测。

    python tests/test_mdhtml.py

为什么值得单测：公式转错了用户看到的是一堆乱码或空白，而"生成时"没人会盯着看；
这里用**知识库里真实出现过的公式形态**（分式、根号、求和、矩阵、aligned、
\boldsymbol、上下标）做判据，并保证"转不了也不抛、原样显示"。
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


REAL = [
    (r"E = m c^2", "简单式子"),
    (r"\sigma = \sqrt{\frac{\sum |B_r - B|}{n}}", "根号+分式+求和"),
    (r"B_{px} = \sum_{i=1}^{N} a_{xi} M_{xi}", "上下标+求和"),
    (r"\boldsymbol{G}=\left[\begin{matrix}a_{x11} & a_{y11}\end{matrix}\right]", "矩阵+bold"),
    (r"\left\{ \begin{aligned} a_{xi} &= \frac{\mu_{0}}{4\pi} \end{aligned} \right.",
     "aligned 方程组"),
    (r"\alpha + \beta \gamma \Delta \mu_0", "希腊字母"),
]


def test_latex():
    print("\n[1] LaTeX → MathML：知识库里真实出现过的公式形态")
    import mdhtml as H
    for tex, what in REAL:
        m = H.latex_to_mathml(tex)
        ok = m.startswith("<math") and "</math>" in m
        check(f"{what} 转成了 MathML", ok, m[:80])
    check("block 模式带 display=block",
          'display="block"' in H.latex_to_mathml("E=mc^2", "block"))
    check("inline 模式带 display=inline",
          'display="inline"' in H.latex_to_mathml("E=mc^2", "inline"))
    check("同一个式子走缓存（两次结果同一对象内容）",
          H.latex_to_mathml("E=mc^2") == H.latex_to_mathml("E=mc^2"))


def test_fallback():
    print("\n[2] 转不了也不能抛：退回原样显示")
    import mdhtml as H
    bad = r"\thisIsNotALaTeXCommand{{{"
    out = H.latex_to_mathml(bad)
    check("坏公式不抛且有输出", bool(out))
    check("坏公式退回 <code> 原文",
          "<code" in out and "thisIsNotALaTeXCommand" in out, out[:90])
    check("空串也不抛", bool(H.latex_to_mathml("")))


def test_render():
    print("\n[3] markdown → HTML：标题/列表/引用/转义/公式")
    import mdhtml as H
    md = "\n".join([
        "# 标题一", "", "**粗**与*斜*和`代码`", "",
        "- 项一", "- 项二 $x^2$", "", "1. 甲", "2. 乙", "",
        "> 引用", "", "$$\\sigma = \\sqrt{2}$$", "",
        "段落里有 <b>标签</b> & 符号", "", "[链](https://x.test) ![图](a.jpg)",
    ])
    out = H.render(md)
    check("标题渲染", "font-weight:600" in out)
    check("粗/斜/代码", all(k in out for k in ("<strong>粗</strong>", "<em>斜</em>",
                                             "<code>代码</code>")))
    check("无序与有序列表", "<ul" in out and "<ol" in out and "项一" in out)
    check("引用", "<blockquote" in out)
    check("行内公式渲染成 MathML", "$x^2$" not in out and "<math" in out)
    check("独立公式块渲染成 MathML 且是 block",
          'display="block"' in out and "\\sqrt" not in out)
    check("HTML 被转义（<b> 不当标签）",
          "&lt;b&gt;标签&lt;/b&gt;" in out and "&amp;" in out, out[-160:])
    check("链接保留、图片降级成图注",
          '<a href="https://x.test">链</a>' in out and "[图：图]" in out)
    check("空输入不抛", H.render("") == "" and H.render(None) == "")


def test_write():
    print("\n[4] write_html：写同名 .html、跳过与上限")
    import mdhtml as H
    tmp = tempfile.mkdtemp(prefix="kbhtml-")
    try:
        md = os.path.join(tmp, "A.outline.md")
        with io.open(md, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 纲要\n\n- 公式 $E=mc^2$\n")
        dst = H.write_html(md)
        check("写出了同名 .html", dst.endswith("A.outline.html")
              and os.path.isfile(dst), dst)
        html = io.open(dst, encoding="utf-8").read()
        check("html 里有 MathML 与标题", "<math" in html and "纲要" in html)
        check("文件不存在时返回空串", H.write_html(os.path.join(tmp, "无.md")) == "")
        # 病态文件（公式数超上限）不写
        huge = os.path.join(tmp, "B.md")
        with io.open(huge, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("$$x$$\n" * (H.MAX_FORMULAS + 10))
        check("公式数超上限时跳过（不让窗格卡住）", H.write_html(huge) == "")
        check("html_path_for 只换后缀",
              H.html_path_for("a/b/c.md") == "a/b/c.html")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_latex()
    test_fallback()
    test_render()
    test_write()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
