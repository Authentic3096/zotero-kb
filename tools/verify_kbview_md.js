/**
 * verify_kbview_md.js —— 用 node 把**侧栏页面里的 markdown 管线**（vendored
 * markdown-it + KaTeX + 我们自己那几条规则）真跑一遍。
 *
 * ## 为什么值得单来一支（2026-10-11 真人反馈逼出来的）
 *
 * 内联公式规则如果在 silent=true（markdown-it 的 state.skipToken 会用这个模式把
 * 规则表过一遍）时**返回 true 却不推进 state.pos**，markdown-it 会抛
 * "inline rule didn't increment state.pos"。它偏偏是在**图片/链接的方括号标签**
 * 里扫描时触发的（parseLinkLabel → skipToken），所以触发条件是：
 *
 *     图片或链接的标签文本里带公式，例如
 *     ![Fig. 3. in mA· $\mathrm{m}^2)$ for varying layers](images/x.jpg)
 *
 * MinerU 的图注**非常常见**这种形态。一旦抛，页面那层的 try/catch 会把**整页**
 * 退化成一个大 <p> 纯文本（换行被 HTML 折成空格、图片是 ![] 原文、公式是 $$ 原文）；
 * 而同一页点「改这一页」按块渲染又是好的 —— 看起来就像"公式渲染不对"。
 *
 * 所以本脚本的判据**不是"没抛异常"**（异常被页面内部 catch 掉了），
 * 而是**渲染结果的质量**：图片必须是真 <img>、公式必须是 KaTeX。
 *
 * 用法：node tools/verify_kbview_md.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PLUGIN = path.join(__dirname, "..", "zotero-plugin");
const page = fs.readFileSync(path.join(PLUGIN, "content", "kbview.js"), "utf8");
const markdownit = require(path.join(PLUGIN, "content/vendor", "markdown-it", "markdown-it.min.js"));
const katex = require(path.join(PLUGIN, "content/vendor", "katex", "katex.min.js"));

let pass = 0, fail = 0;
const check = (name, cond, detail) => {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + "  " + (detail || "")); }
};

// ---- 够用的假 DOM：页面 IIFE 会 wire() 一次，只碰这些
function fakeEl() {
  return {
    className: "", textContent: "", value: "", hidden: false, disabled: false,
    style: { setProperty() {}, removeProperty() {} },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { return c; }, append() {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, contains() { return false; } },
  };
}
const doc = {
  readyState: "complete",
  documentElement: { style: fakeEl().style },
  createElement: fakeEl,
  createEvent: () => ({ initEvent() {} }),
  getElementById: fakeEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
};
const sandbox = {
  window: { markdownit: markdownit, katex: katex, addEventListener() {} },
  document: doc,
  setTimeout: setTimeout, clearTimeout: clearTimeout, console: console,
};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(page, sandbox, { filename: "kbview.js" });

const api = sandbox.window.__kbvDebug;
check("页面导出了 __kbvDebug.renderMd（本测试与真机排错都用它）",
  !!(api && typeof api.renderMd === "function"));

if (api && typeof api.renderMd === "function") {
  const render = api.renderMd;
  const env = { imgBase: "resource://zotero-kb-files/mineru/ABC/" };

  // ---- ① 回归：图片标签里带公式（旧代码整页退化成纯文本）
  const imgWithMath = "![Fig. 3. (a) Deviation of position (in cm) and magnetic moment "
    + "(in mA· $\\mathrm { m } ^ { 2 } )$ for varying number of hidden layers.](images/page_3_chart_7.jpg)";
  const h1 = render(imgWithMath, env);
  check("[回归] 图片标签文本里带公式时，图片仍渲染成 <img>（旧代码整页退化成纯文本）",
    h1.indexOf("<img") >= 0
    && h1.indexOf("resource://zotero-kb-files/mineru/ABC/images/page_3_chart_7.jpg") >= 0,
    h1.slice(0, 200));

  const linkWithMath = "see [the note about $x^2$ and $y$ here](https://example.test/n)";
  const h2 = render(linkWithMath, env);
  check("[回归] 链接文本里带公式时，链接仍是 <a>",
    h2.indexOf("<a ") >= 0 && h2.indexOf("example.test") >= 0, h2.slice(0, 200));

  // ---- ② 整页形态：粗体 + 链接 + 行内/块级公式 + 图片 + 表格
  const page4 = [
    "**where** $R _ { i }$ denotes the sensor and ref [12].",
    "",
    "MRD<sub>i</sub> (%)",
    "",
    "$$= 1 0 0 \\frac { 1 } { P } \\cdot \\sum _ { p = 1 } ^ { P } \\left( \\frac { \\sqrt { A } - \\sqrt { B } } { \\sqrt { C } } \\right) \\tag{5}$$",
    "",
    "![Fig. 5. chart](images/page_3_chart_14.jpg)",
    "Fig. 5. caption",
    "",
    "| a | b |",
    "| --- | --- |",
    "| 1 | 2 |",
  ].join("\n");
  const h3 = render(page4, env);
  check("整页：分段成多个 <p>（没有整页塞进一个大 <p>）",
    (h3.match(/<p>/g) || []).length >= 3 && h3.indexOf("![Fig. 5.") < 0, h3.slice(0, 200));
  check("整页：块级公式是独立公式块（.katex-display），不是挤在段落里",
    h3.indexOf("kbv-mathblock") >= 0 && h3.indexOf("katex-display") >= 0);
  check("整页：行内公式走 KaTeX", h3.indexOf("katex") >= 0 && h3.indexOf("$R _ { i }$") < 0);
  check("整页：管道表画成 <table>",
    h3.indexOf("<table") >= 0 && h3.indexOf("<th") >= 0 && h3.indexOf("| --- |") < 0);
  check("整页：相对路径图片改写成知识库 resource:// 前缀",
    h3.indexOf("resource://zotero-kb-files/mineru/ABC/images/page_3_chart_14.jpg") >= 0);

  // ---- ③ 公式行紧跟正文行（中间没空行）也要按块级排
  const h4 = render("正文一行\n$$x ^ 2 + y ^ 2 = z ^ 2$$\n再一行", env);
  check("公式行紧跟正文行（无空行）时仍按块级公式排",
    h4.indexOf("kbv-mathblock") >= 0 && h4.indexOf("katex-display") >= 0, h4.slice(0, 200));

  // ---- ④ 跨行 / aligned 公式
  const h5 = render("$$\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}$$", env);
  check("跨行 / aligned 公式能排（KaTeX 支持 aligned，服务端 MathML 不行）",
    h5.indexOf("kbv-mathblock") >= 0 && h5.indexOf("katex-display") >= 0, h5.slice(0, 200));

  // ---- ⑤ 转不了的公式退回原文，不空白、不抛
  const h6 = render("$$\\thisIsNotAMacro { }$$", env);
  check("KaTeX 转不了的公式退回原文（不空白、不抛）",
    h6.indexOf("kbv-tex") >= 0 || h6.indexOf("katex") >= 0, h6.slice(0, 160));

  // ---- ⑥ 内联 HTML 白名单：MinerU 带出来的 sub/sup 要真的当上下标排
  const h8 = render("MRD<sub>i</sub> (%) 与 H<sup>+</sup> 与 <b>粗</b>、<br/> 换行、"
    + "<script>alert(1)</script> 与 <sup onclick=\"x\">S</sup> 与 $x^2$", env);
  check("MinerU 带出的 sub/sup/br/b 渲染成真标签（不是可见的原文）",
    h8.indexOf("<sub>i</sub>") >= 0 && h8.indexOf("<sup>+</sup>") >= 0
    && h8.indexOf("<b>粗</b>") >= 0 && h8.indexOf("<br/>") >= 0
    && h8.indexOf("&lt;sub&gt;") < 0, h8.slice(0, 260));
  check("其余 HTML 仍被转义（不开 html:true —— 特权页面不能跑来自 PDF 的脚本）",
    h8.indexOf("<script>") < 0 && h8.indexOf("&lt;script&gt;") >= 0, h8.slice(0, 320));
  check("白名单标签带属性时继续转义（不能放 <sup onclick=…> 进来）",
    h8.indexOf("<sup onclick") < 0 && h8.indexOf("&lt;sup onclick") >= 0, h8.slice(0, 320));

  // ---- ⑦ 源 LaTeX 本身坏了（真库 1314 个块公式里 3 个是坏的）
  //   ⚠ 不能是一大片红字：KaTeX 原来用 throwOnError:false，会把整段 LaTeX 原文
  //   染红塞进 <span class="katex-error">（最长的 3 KB），看着像我们排错了。
  //   现在自己接住 ParseError，退回等宽原文 + kbv-tex-fail（CSS 里有醒目标记）。
  const h9 = render("$$\\begin{bmatrix} \\begin{array}{c} x \\end{bmatrix}$$", env);
  check("源 LaTeX 坏了时退回等宽原文并标 kbv-tex-fail（不是 katex-error 红字）",
    h9.indexOf("kbv-tex-fail") >= 0 && h9.indexOf("katex-error") < 0, h9.slice(0, 220));

  // ---- ⑧ 带 ## p.N 的整份文件
  const h7 = render("# 标题\n\n> Zotero key ABC｜3 页\n\n## p.1\n\n正文 $x$\n\n## p.2\n\n$$y$$\n", env);
  check("带 ## p.N 的整份文件也能渲染", h7.indexOf("p.1") >= 0 && h7.indexOf("katex") >= 0);
}

console.log("");
console.log("通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
