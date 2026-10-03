"""检查"给 AsyncFunction 执行的插件脚本"的语法。

    python tools/check_js_syntax.py [文件...]

为什么不能直接用 `node --check`：
    这些脚本不是普通 JS 文件，它们的运行方式是
        new AsyncFunction("Zotero", "Services", "ChromeUtils", code)
    也就是**整个文件当作 async 函数的函数体**。所以
      · 顶层 `await`  合法（AsyncFunction 天然 async）
      · 顶层 `return` 合法（它就是函数体）
    而 `node --check` 按 CommonJS 脚本解析，会把上面两个都报成语法错误
    —— 本机就在这里被误导过一次（以为脚本坏了，其实是检查方式不对）。

    正确做法：用 AsyncFunction 构造一遍。
    构造函数**只校验语法、不执行**，所以不会真的跑插件代码。
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")

# 用 node 来构造 AsyncFunction 做语法校验（node 是权威解析器）
CHECKER = r"""
const fs = require('fs');
// node -e 的 argv 布局：[execPath, ...额外参数]，没有脚本路径那一项。
// 所以取 argv[1]（而不是 [2]）—— 曾经写成 [2] 导致 readFileSync(undefined)。
const file = process.argv[1];
if (!file) { console.log('ERR 没有传文件路径'); process.exit(1); }
const code = fs.readFileSync(file, 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const problems = [];
try {
  // 只构造，不调用 —— 仅校验语法
  new AsyncFunction('Zotero', 'Services', 'ChromeUtils', code);
} catch (e) {
  problems.push(e.name + ': ' + e.message);
}
// ⚠ 补一层检查：把整个文件包成 AsyncFunction 会**掩盖**一类错 ——
//   "普通函数里用了 await"。那些函数在文件里是合法的（因为顶层 await
//   被允许），但它们自己不是 async，运行到那一行才会 SyntaxError。
//   本机就这么写错过一次（kbInitPane 里加了 await 但没加 async，
//   上面的检查报"语法正确"）。
//
//   判定必须**只剪掉嵌套的 async 函数体**再找 await —— 第一版没剪干净，
//   把 `async (quiet) => { ... await ... }` 里的 await 误报成外层的。
function bodyRange(code, openBraceIdx) {
  // 从 '{' 开始按括号配平，返回 [start, end)（end 指向收尾 '}' 之后）
  let depth = 0;
  for (let i = openBraceIdx; i < code.length; i++) {
    const c = code[i];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return [openBraceIdx, i + 1];
    }
  }
  return [openBraceIdx, code.length];
}

function stripAsyncFunctions(code) {
  // 把 async function / async (...) => {...} 的函数体挖成等长空格，
  // 这样外层再搜 await 就不会命中内层的。
  const chars = code.split('');
  const blank = (a, b) => { for (let i = a; i < b; i++) {
    if (chars[i] !== '\n') chars[i] = ' '; } };

  // 1) async function name(...) { ... }
  let re1 = /\basync\s+function\s*\w*\s*\([^)]*\)\s*\{/g, m1;
  while ((m1 = re1.exec(code)) !== null) {
    const open = m1.index + m1[0].length - 1;      // 指向 '{'
    const [a, b] = bodyRange(code, open);
    blank(a, b);
  }
  // 2) async (...) => { ... }   以及 async x => { ... }
  let re2 = /\basync\s*(\([^)]*\)|\w+)\s*=>\s*\{/g, m2;
  while ((m2 = re2.exec(code)) !== null) {
    const open = m2.index + m2[0].length - 1;
    const [a, b] = bodyRange(code, open);
    blank(a, b);
  }
  return chars.join('');
}

try {
  const cleaned = stripAsyncFunctions(code);
  // 把注释也去掉，免得注释里提到 await 就误报
  const noComment = cleaned
    .replace(/\/\/[^\n]*/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const re = /(^|\n)\s*(async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{/g;
  let m;
  while ((m = re.exec(code)) !== null) {
    if (m[2]) continue;                       // 已经是 async，跳过
    const name = m[3];
    const open = m.index + m[0].length - 1;
    const [a, b] = bodyRange(code, open);
    // 在这个函数的**原始**范围里，用清理过的文本判断本层有没有 await
    const raw = code.slice(a, b);
    const clean = noComment.slice(a, b);
    if (/\bawait\b/.test(clean)) {
      problems.push('非 async 函数 `' + name + '` 里用了 await'
        + '（运行时会 SyntaxError，但上面的整体校验查不出来）');
    }
  }
} catch (e) {
  problems.push('附加检查本身出错（不影响主结论）：' + e.message);
}
if (problems.length) {
  console.log('ERR ' + problems.join(' ｜ '));
  process.exit(1);
}
console.log('OK');
"""


def node_exe() -> str:
    for cand in (r"C:\Program Files\nodejs\node.exe", "node"):
        if cand == "node" or os.path.exists(cand):
            return cand
    return "node"


def check(path: str) -> tuple[bool, str]:
    node = node_exe()
    try:
        out = subprocess.run([node, "-e", CHECKER, path],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        msg = (out.stdout or "").strip()
        if msg.startswith("OK"):
            return True, "语法正确"
        return False, (msg or out.stderr or "").strip()[:300]
    except Exception as exc:  # noqa: BLE001
        return False, f"调用 node 失败：{exc}"


NODE = shutil.which("node") or "node"


def main() -> int:
    args = sys.argv[1:]
    if args:
        files = [a if os.path.isabs(a) else os.path.join(PLUGIN, a) for a in args]
    else:
        files = sorted(glob.glob(os.path.join(PLUGIN, "*.js")))
        files = [f for f in files if not f.endswith(".bak-iife")]

    print("=" * 64)
    print("插件脚本语法检查")
    print("  · 语法：把整套源码包进 AsyncFunction 解析（Zotero 就是这么加载它的）")
    print("  · 语义：额外查「非 async 函数里用了 await」—— 语法能过、跑起来才炸")
    print("    （Zotero 的「运行 JavaScript」窗口把代码当 async 函数体跑，")
    print("      所以那一类诊断脚本里的**顶层 await 是合法的**，不算问题）")
    print("=" * 64)
    bad = 0
    for f in files:
        if not os.path.exists(f):
            print(f"  [--] {os.path.basename(f)} 不存在")
            continue
        ok, msg = check(f)
        print(f"  {'[OK]' if ok else '[XX]'} {os.path.basename(f):28} {msg}")
        if not ok:
            bad += 1
    print()
    print(f"共 {len(files)} 个文件，{bad} 个有问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
