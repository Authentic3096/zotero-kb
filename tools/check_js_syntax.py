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
//   ⚠ 2026-10-13 重写（修一次误报）：旧版是"删掉注释后在清理过的文本里按
//     原始下标找 await"，两个错：
//       ① 注释是 **replace 掉**的，长度变短 → 原始下标切到别处去。本机实测
//          content/admin-pages/structure.js：`async render()` 里的 await
//          被算到了上面的 `safeShort` 头上（那个函数里根本没有 await）；
//       ② 它只处理 `async function` 与 `async (...) =>`，漏了
//          `async 名字() {}` 这种**方法简写**。
//   新做法：**交给解析器判**，不再自己搜 await ——
//       ① 用"长度不变"的方式把注释/字符串/正则挖成空格（大括号配平必须落在
//          真实的结构字符上，`"}"` 这种字符串会让范围算错）；
//       ② 每个 `function 名字(...) {`（非 async）按大括号配平求出**它自己的**
//          body 范围；
//       ③ 把这段**原始源码**丢给 `new Function(...)` —— 非 async 函数体里出现
//          裸 await 时 V8 当场抛 SyntaxError，而嵌套在 async 方法/箭头里的
//          await 合法、不会抛。这是"是不是 await 的错"由解析器说了算，
//          不靠任何正则猜名字或猜归属。
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

/** 前一个非空白字符（判断 `/` 是除号还是正则字面量用）。 */
function prevSignificant(code, i) {
  for (let k = i - 1; k >= 0; k--) {
    if (!/\s/.test(code[k])) return code[k];
  }
  return '';
}

/**
 * 把注释 / 字符串 / 模板串 / 正则字面量**原地**挖成空格。
 * 长度与换行位置都不变 —— 这样"按括号配平求函数体范围"才拿得到真实下标。
 */
function blankNonCode(code) {
  const chars = code.split('');
  const n = code.length;
  const blank = function (a, b) {
    for (let i = a; i < b; i++) { if (chars[i] !== '\n') chars[i] = ' '; }
  };
  let i = 0;
  while (i < n) {
    const c = code[i];
    const c2 = code[i + 1];
    if (c === '/' && c2 === '/') {                    // 行注释
      let j = i;
      while (j < n && code[j] !== '\n') j++;
      blank(i, j); i = j; continue;
    }
    if (c === '/' && c2 === '*') {                    // 块注释
      let j = i + 2;
      while (j < n && !(code[j] === '*' && code[j + 1] === '/')) j++;
      j = Math.min(n, j + 2);
      blank(i, j); i = j; continue;
    }
    if (c === '"' || c === "'" || c === '`') {        // 字符串 / 模板串
      let j = i + 1;
      while (j < n) {
        if (code[j] === '\\') { j += 2; continue; }
        if (code[j] === c) { j++; break; }
        j++;
      }
      j = Math.min(n, j);
      blank(i, j); i = j; continue;
    }
    if (c === '/') {                                  // 可能是正则字面量
      const p = prevSignificant(code, i);
      if (p === '' || '([{,;:=!&|?+-*%^~<>'.indexOf(p) >= 0) {
        let j = i + 1, inClass = false, done = false;
        while (j < n) {
          const d = code[j];
          if (d === '\\') { j += 2; continue; }
          if (d === '[') inClass = true;
          else if (d === ']') inClass = false;
          else if (d === '/' && !inClass) { j++; done = true; break; }
          else if (d === '\n') break;                 // 保守：正则不跨行
          j++;
        }
        if (done) { blank(i, Math.min(n, j)); i = Math.min(n, j); continue; }
      }
    }
    i++;
  }
  return chars.join('');
}

try {
  // ⚠ masked 与 code **等长**（挖空 = 换空格），所以下标两边通用。
  const masked = blankNonCode(code);
  const re = /(^|\n)[ \t]*(async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{/g;
  let m;
  while ((m = re.exec(masked)) !== null) {
    if (m[2]) continue;                       // 已经是 async，跳过
    const name = m[3];
    const open = m.index + m[0].length - 1;   // 指向 '{'
    const [a, b] = bodyRange(masked, open);
    // 取**原始源码**里这一段（从 `function` 起到配平的 `}`），让解析器判它
    const src = code.slice(m.index + m[1].length, b);
    try {
      new Function('Zotero', 'Services', 'ChromeUtils', src);
    } catch (e) {
      // 只有"await 用错位置"这一类才报；别的语法错由上面的整体校验负责，
      // 这里重复报会给同一个问题两条互相矛盾的说明。
      if (/await/.test(e.message)) {
        problems.push('非 async 函数 `' + name + '` 里用了 await'
          + '（运行时会 SyntaxError，但上面的整体校验查不出来）');
      }
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
        # ⚠ 两层都要扫：插件运行时的源码在 src/ 下（bootstrap.js 是它拼出来的
        #   生成物），根目录那层放的是 manifest / 设置面板 / 诊断脚本。
        #   只扫根目录的话，`src/*.js` 里的语法错要等到打包+装进 Zotero 才发现。
        files = sorted(glob.glob(os.path.join(PLUGIN, "*.js")))
        files += sorted(glob.glob(os.path.join(PLUGIN, "src", "*.js")))
        # ⚠ content/*.js 是"知识库"自建列那个页面里的脚本（自建列 2026-10-11）——
        #   它**不是** AsyncFunction 的源码，但用同一套 AsyncFunction 方式解析
        #   一样能过（只是包一层 async 函数体），语法错照样抓得到。
        files += sorted(glob.glob(os.path.join(PLUGIN, "content", "*.js")))
        # ⚠ admin-pages/ 也要扫：2026-10-13 那里 10 个脚本因为块注释里
        #   写了 `*/` 连写而全部语法错误，却一路打进了 xpi（本检查器当时
        #   只扫 content/*.js）。页面脚本不进 bootstrap，但进包。
        files += sorted(glob.glob(os.path.join(PLUGIN, "content", "admin-pages", "*.js")))
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
        label = os.path.relpath(f, PLUGIN).replace(os.sep, "/")
        print(f"  {'[OK]' if ok else '[XX]'} {label:28} {msg}")
        if not ok:
            bad += 1
    print()
    print(f"共 {len(files)} 个文件，{bad} 个有问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
