"""提示词里的 LaTeX 转义不能被 Python 吃掉（独立小测试，不依赖任何补丁脚本）。

    python tests/test_prompt_escapes.py

背景：2026-10-05 我在 `digest` 提示词里写了单反斜杠的 `\\frac` / `\\boldsymbol`
（本文件里要写双反斜杠才对，见下），Python 把 `\\f`（换页）、`\\b`（退格）
**当转义吃掉** → 渲染成 `rac{`、`oldsymbol{`，正好把"照抄 LaTeX"这条教歪，
而且**不报错**（只多一条 SyntaxWarning）。所以这里用 chr() 拼控制字符来判，
不看日志、只看最终值。
"""

from __future__ import annotations

import os
import sys

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


B = chr(92)          # 反斜杠
CTRL = ((chr(12), "换页"), (chr(8), "退格"), (chr(11), "纵向制表"))


def main() -> int:
    import prompts as PR

    print("[1] 全部提示词里没有被 Python 吃出来的控制字符")
    bad = []
    for pid in PR.SPECS:
        for field in ("system", "user"):
            val = PR.get(pid, field) or ""
            for ch, name in CTRL:
                if ch in val:
                    bad.append(f"{pid}.{field} 含{name}字符")
    check("没有控制字符", not bad, str(bad))

    print("\n[2] digest 提示词里是单反斜杠的 LaTeX 形态")
    du = PR.get("digest", "user")
    for token in (B + "sum", B + "frac", B + "sqrt", B + "boldsymbol", B + "mu_0"):
        check(f"含 {token}", token in du, du[:80])
    check("没有把 frac 吃掉成 rac{", (B + "frac{") in du and " rac{" not in du)

    print("\n[3] 明确要求把公式写进 $…$ / $$…$$")
    check("提到 LaTeX", "LaTeX" in du)
    check("给了 $…$ 的例子", "$" in du and du.count("$") >= 4)
    check("明确不要用纯文本 Σ/√ 近似",
          "Σ" in du or "√" in du, du[-200:])

    print("\n[4] 校验能过（占位符与契约键都在）")
    check("validate(digest) 通过", PR.validate("digest", "user", du) == "",
          PR.validate("digest", "user", du))
    check("渲染时占位符被替换", "{" + "text" + "}" not in
          PR.render("digest", title="T", section="S", text="正文"))

    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
