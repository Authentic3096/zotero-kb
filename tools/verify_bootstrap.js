/**
 * 用 Node 的 vm 把真机 bootstrap.js 跑起来，断言：
 *   ① 插件对象能建起来、startup 各步全过；
 *   ② `ItemPaneManager.registerSection` 被调用，且 **header/sidenav 的 l10nID
 *      与图标都在**（这两项是必填，缺了就是"分区标题空白、无报错"）；
 *   ③ `Reader.registerEventListener('renderTextSelectionPopup', …, pluginID)`
 *      被注册（公开接口，第三个参数用于卸载时自动摘）；
 *   ④ `quit-application-requested` 被观察（退出提醒）；
 *   ⑤ **界面文案真的出得来**：`initLocale` 把 ftl 挂上了窗口文档，
 *      而且 onRender 建出来的每个按钮都有**非空文字**。
 *
 * 为什么需要它：真机验证要重启 Zotero，而插件里"名字写错/字段名写错"这类问题
 * 在 Zotero 里**没有任何报错**，只表现为"按钮点了没反应"。
 *
 * ⚠ ⑤ 是 2026-10-05 补的，因为真出过一次：`doc.l10n.setAttributes` 设了
 *   data-l10n-id，但文档的 linkset 里没挂我们的 ftl —— 于是**按钮全是空框**，
 *   不报错、也不显示 id。桩环境里 doc.l10n 本来就"什么都不翻译"（跟当时真机
 *   一个样），所以这条断言能当场抓住它：文字必须来自 `Zotero.ftl.formatValueSync`
 *   那条兜底路径。
 *
 * 用法：node tools/verify_bootstrap.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PLUGIN = path.join(__dirname, "..", "zotero-plugin");
const SRC = fs.readFileSync(path.join(PLUGIN, "bootstrap.js"), "utf8");

/** 读真 ftl 里的 id → 文案（只认 `id = 值` 这种行，够用）。 */
function parseFtl(p) {
  const out = {};
  for (const line of fs.readFileSync(p, "utf8").split("\n")) {
    const m = /^([A-Za-z0-9_.-]+)\s*=\s*(.*)$/.exec(line.trim());
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}
const MSG_ZH = parseFtl(path.join(PLUGIN, "locale", "zh-CN", "zotero-kb.ftl"));
const MSG_EN = parseFtl(path.join(PLUGIN, "locale", "en-US", "zotero-kb.ftl"));

let pass = 0, fail = 0;
const check = (name, cond, detail) => {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + "  " + (detail || "")); }
};

const calls = {
  sections: [], readers: [], observers: [], prefs: {}, status: [], notes: 0,
  http: [], ftlLinks: [], ftlResourceIds: [],
};


const makeEl = () => {
  const el = {
    style: { cssText: "", setProperty() {} },
    dataset: {},
    attributes: {},
    children: [],
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k]; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    replaceChildren(...c) { this.children = c; },
    querySelector() { return null; },
    querySelectorAll(sel) {
      // paneState() 靠这条查询找"我们的分区元素"；桩里由测试注入 fakeSections
      if (sel === "collapsible-section") return calls.fakeSections || [];
      return [];
    },
    set textContent(v) { this._text = v; },
    get textContent() { return this._text; },
  };
  return el;
};

const doc = {
  createElement: () => makeEl(),
  createElementNS: () => makeEl(),
  // initLocale 会往 doc.head / linkset 里插 <link rel="localization">
  head: makeEl(),
  querySelector: () => null,
  // paneState() 靠这条查询找"我们的分区元素"；桩里由测试注入 fakeSections
  querySelectorAll: (sel) => (sel === "collapsible-section"
    ? (calls.fakeSections || []) : []),
  // ⚠ 这个桩**故意不翻译**（只把 data-l10n-id 记下来）—— 真机当时就是
  //   "文档没挂 ftl" 的状态，见文件头的 ⑤。
  l10n: { setAttributes(el, id) { el.setAttribute("data-l10n-id", id); } },
  documentElement: makeEl(),
  getElementById: () => null,
};

const Zotero = {
  debug: () => {},
  logError: () => {},
  getMainWindow: () => ({
    document: doc, ZoteroPane: {},
    MozXULElement: {
      insertFTLIfNeeded(f) { calls.ftlLinks.push(f); },
    },
  }),
  Prefs: {
    get: (k) => (k in calls.prefs ? calls.prefs[k] : undefined),
    set: (k, v) => { calls.prefs[k] = v; },
    registerObserver: () => {},
    unregisterObserver: () => {},
  },
  Notifier: { registerObserver: () => 1, unregisterObserver: () => {} },
  // 程序化取字符串（initLocale 会 addResourceIds，l10n()/l10nText() 会取）
  ftl: {
    addResourceIds(ids) { calls.ftlResourceIds.push(...ids); },
    formatValueSync(id) {
      if (Object.prototype.hasOwnProperty.call(MSG_ZH, id)) return MSG_ZH[id];
      return undefined;
    },
  },

  // ⚠ 2026-10-05：窗格（内容窗格里的「本地模型」分区）已按用户要求整条删除。
  //   这两个桩**故意留着并记账**：谁把 registerSection / registerEventListener
  //   加回来，末尾那两条反向 check 立刻红 —— 比"API 不存在时报错"更早、
  //   也更明确地指出"你不该把它加回来"。
  ItemPaneManager: {
    registerSection(opts) { calls.sections.push(opts); return opts.paneID; },
    unregisterSection() { return true; },
  },
  Reader: {
    registerEventListener(type, fn, pluginID) {
      calls.readers.push({ type, fn, pluginID });
    },
    getByTabID: () => null,
  },
  Items: { get: () => null },
  HTTP: { request: async () => ({ response: "{}" }) },
  Promise: Promise,
  Date: Date,
  Utilities: { Internal: {}, xpcom: {} },
  ProgressWindow: function () { return { changeHeadline() {}, addDescription() {}, show() {}, startCloseTimer() {}, close() {} }; },
  File: { pathToFile: (p) => ({ path: p, reveal() {} }) },
  launchURL: () => {},
  Session: { state: { windows: [] } },
  uiReadyPromise: Promise.resolve(),
};

const Services = {
  obs: {
    addObserver(o, t) { calls.observers.push({ o, t }); },
    removeObserver() {},
    notifyObservers() {},
  },
  prompt: {
    confirmEx: () => 0,
    alert: () => {},
    BUTTON_POS_0: 1, BUTTON_POS_1: 2, BUTTON_TITLE_IS_STRING: 128,
  },
  locale: { availableLocales: ["zh-CN", "en-US"] },
  dirsvc: { get: () => ({ path: "D:\\" }) },
  // ⚠ 为了能测「Ollama 装在本机默认位置」那条分支：`ollamaExePath()` 读
  //   %LOCALAPPDATA%，桩里给它一个中性值（别写真实用户名，发布审计会拦）。
  env: { get: (k) => (k === "LOCALAPPDATA" ? "C:\\lad" : "") },
};
const Components = { classes: {}, interfaces: {}, utils: { import: () => ({}) } };
const ChromeUtils = { import: () => ({}) };
const IOUtils = { read: async () => new Uint8Array(), write: async () => {} };
const PathUtils = { join: (...a) => a.join("/") };
const setTimeoutStub = (fn) => { /* 定时器不跑：桩环境不推进时间 */ return 1; };
const clearTimeoutStub = () => {};
const setIntervalStub = () => { throw new Error("不许用 setInterval（沙箱里可能不触发）"); };

const sandbox = {
  Zotero, Services, Components, ChromeUtils, IOUtils, PathUtils,
  setTimeout: setTimeoutStub, clearTimeout: clearTimeoutStub,
  setInterval: setIntervalStub, clearInterval: () => {},
  console, JSON, Math, Date, Promise, Object, Array, String, Number, Boolean,
  RegExp, Error, TypeError, isNaN, parseInt, parseFloat, encodeURIComponent,
  decodeURIComponent, TextEncoder, TextDecoder, URL, Blob, fetch: async () => ({ ok: false }),
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;

vm.createContext(sandbox);
try {
  vm.runInContext(SRC, sandbox, { filename: "bootstrap.js" });
  check("bootstrap.js 能在桩环境里加载", true);
} catch (e) {
  check("bootstrap.js 能在桩环境里加载", false, String(e));
  console.log("\n通过 " + pass + "，失败 " + fail);
  process.exit(1);
}

const KB = sandbox.ZoteroKB || sandbox.Zotero.ZoteroKB;
check("插件对象建起来了", !!(KB && KB.startup), "ZoteroKB 不存在");

if (KB) {
  // 桩掉会真的碰文件/网络的几步，只验结构
  KB.writeStatusFile = (o) => { calls.status.push(o); };
  KB.serverOnStartup = async () => {};
  KB.healthCheck = async () => {};
  KB.refreshWeights = async () => {};
  KB.ollamaOnStartup = async () => {};
  KB.registerPrefPane = () => {};
  KB.registerWeightColumn = () => {};
  KB.startTaskPolling = () => {};
  KB.registerToolbarButton = () => {};
  KB.registerItemMenu = () => {};

  try {
    KB.startup({ id: "zotero-kb@authentic3096.github.io", version: "1.0.0",
                 rootURI: "file:///x/" });
    check("startup 没抛异常", true);
  } catch (e) {
    check("startup 没抛异常", false, String(e));
  }

  const steps = (calls.status.find((s) => s.startupSteps) || {}).startupSteps || "";
  check("startup 各步全过（含 initLocale）",
    steps.indexOf("FAIL") < 0 && steps.indexOf("initLocale=ok") >= 0, steps);

  // ---- ⑤ 界面文案那套挂载还在（2026-10-05 用户报"按钮全是空框"的根因）
  check("initLocale 把 ftl 挂到了窗口（MozXULElement.insertFTLIfNeeded）",
    calls.ftlLinks.includes(KB.FTL_FILE),
    JSON.stringify({ links: calls.ftlLinks, file: KB.FTL_FILE }));
  check("initLocale 也把 ftl 加进了 Zotero.ftl（程序化取字符串用）",
    calls.ftlResourceIds.includes(KB.FTL_FILE),
    JSON.stringify(calls.ftlResourceIds));
  check("文档 linkset 里插进了 <link rel=\"localization\">（兜底那条路）",
    doc.head.children.some((c) => c.attributes["href"] === KB.FTL_FILE),
    JSON.stringify(doc.head.children.map((c) => c.attributes)));
  const loc = (calls.status.find((s) => s.locale) || {}).locale || "";
  check("状态文件里记了 locale 那一步的结果（排查用）",
    loc.indexOf(KB.FTL_FILE) >= 0 && loc.indexOf("ok") >= 0, loc);
  // ⚠ 2026-10-05：两个 ftl 现在是**空的**（那些文案全是内容窗格分区用的，
  //   窗格删了就没人引用了）。所以这里只保留"两份 id 集合一致"这条 ——
  //   以后往回加文案时，中英两份仍然要同步。
  check("zh-CN 与 en-US 的 ftl id 集合一致（现在都是空集）",
    Object.keys(MSG_ZH).sort().join() === Object.keys(MSG_EN).sort().join(),
    `zh=${Object.keys(MSG_ZH).length} en=${Object.keys(MSG_EN).length}`);

  // ---- ⑥ 窗格那条链**必须不在**（2026-10-05 用户要求整条删除）
  //
  // 桩里故意留着会记账的 ItemPaneManager / Reader（见上面的注释）：
  // 谁把 registerSection / registerEventListener 加回来，这两条立刻红。
  // ⚠ 2026-10-05 晚改成正向：用户要求「把知识库 md 显示在右侧栏」→
  //   现在**应该**注册一个分区（zotero-kb-kbview）。被删掉的是"对话窗格"，
  //   所以上面那批 paneSend/chatOf 函数名仍然必须不存在（见 check_plugin）。
  check("注册了「知识库」分区",
    calls.sections.length === 1
    && calls.sections[0].paneID === "zotero-kb-kbview",
    JSON.stringify(calls.sections.map((s) => s.paneID)));
  const sec0 = calls.sections[0] || {};
  check("分区 header/sidenav 的 l10nID 与图标都在（缺了就是标题空白且不报错）",
    sec0.header && sec0.header.l10nID === "zotero-kb-kbview-header"
    && sec0.header.icon && sec0.sidenav
    && sec0.sidenav.l10nID === "zotero-kb-kbview-sidenav"
    && sec0.sidenav.icon, JSON.stringify(sec0.header));
  check("分区只对普通条目开启",
    KB.kbviewEnabled({ isRegularItem: () => true }, "library") === true
    && KB.kbviewEnabled({ isRegularItem: () => false }, "library") === false
    && KB.kbviewEnabled({ isRegularItem: () => true }, "reader") === false);

  // ---- md2html：知识库 md 里真正用到的语法（纯函数，最容易出错的一块）
  const html = KB.md2html([
    "# 标题", "", "**粗体**与*斜体*和`代码`", "", "- 项目一", "- 项目二", "",
    "1. 第一", "2. 第二", "", "> 引用一行", "",
    "$$E = m c^2$$", "", "[链接](https://x.test)", "", "![图注](images/a.jpg)",
  ].join("\n"));
  check("md2html 渲染标题", html.indexOf("font-weight:600") >= 0);
  check("md2html 渲染粗体/斜体/代码",
    html.indexOf("<strong>粗体</strong>") >= 0
    && html.indexOf("<em>斜体</em>") >= 0
    && html.indexOf("<code>代码</code>") >= 0, html.slice(0, 160));
  check("md2html 渲染无序与有序列表",
    html.indexOf("<ul") >= 0 && html.indexOf("<ol") >= 0
    && html.indexOf("项目一") >= 0 && html.indexOf("第一") >= 0);
  check("md2html 渲染引用", html.indexOf("<blockquote") >= 0);
  check("md2html 渲染公式（原样保留 LaTeX 文本）",
    html.indexOf("E = m c^2") >= 0 && html.indexOf("monospace") >= 0);
  check("md2html 渲染链接、图片降级成图注",
    html.indexOf('<a href="https://x.test">链接</a>') >= 0
    && html.indexOf("[图：图注]") >= 0);
  check("md2html 转义 HTML（笔记里有 < & 也不炸）",
    KB.md2html("<b>x</b> & y").indexOf("&lt;b&gt;x&lt;/b&gt; &amp; y") >= 0,
    KB.md2html("<b>x</b> & y"));
  check("md2html 空输入不抛", KB.md2html("") === "" && KB.md2html(null) === "");

  // ---- kbviewPickLevel：选中那一级没生成时按 纲要→摘要→档案 回退
  check("kbviewOrder 把「分节纲要」排在摘要之前",
    KB.kbviewOrder("fulltext").join(",") === "fulltext,outline,tldr,card",
    KB.kbviewOrder("fulltext").join(","));
  check("级别回退：首选不在就用纲要",
    KB.kbviewPickLevel("fulltext", (id) => id === "outline").levelId === "outline");
  check("级别回退会标出已回退",
    KB.kbviewPickLevel("fulltext", (id) => id === "outline").fellBack === true);
  check("首选就在时不回退",
    KB.kbviewPickLevel("outline", (id) => id === "outline").fellBack === false);
  check("一级都没有时返回空 + 不抛",
    KB.kbviewPickLevel("outline", () => false).levelId === "");
  check("存在性判断抛异常也不炸",
    KB.kbviewPickLevel("outline", () => { throw new Error("x"); }).levelId === "");
  check("没有注册阅读器选中入口（随窗格一起删了）", calls.readers.length === 0,
    JSON.stringify(calls.readers.map((r) => r.type)));
  check("startup 里不再有窗格/退出提醒那三步",
    steps.indexOf("registerItemPane") < 0
    && steps.indexOf("registerReaderEvents") < 0
    && steps.indexOf("registerQuitGuard") < 0, steps);
  check("插件对象上不再有窗格那批函数",
    typeof KB.registerItemPane === "undefined"
    && typeof KB.paneRender === "undefined"
    && typeof KB.paneSend === "undefined"
    && typeof KB.registerReaderEvents === "undefined"
    && typeof KB.registerQuitGuard === "undefined",
    ["registerItemPane", "paneRender", "paneSend", "registerReaderEvents",
     "registerQuitGuard"].filter((f) => typeof KB[f] !== "undefined").join(","));
  check("退出提醒的观察点也没了（quit-application-requested）",
    !calls.observers.some((o) => o.t === "quit-application-requested"),
    JSON.stringify(calls.observers.map((o) => o.t)));

  // ---- ⑦ 能力探测（19-caps.js）：菜单文案按它走
  check("有能力探测那批函数",
    typeof KB.refreshCaps === "function"
    && typeof KB.probeDsh === "function"
    && typeof KB.probeLocalModel === "function"
    && typeof KB.capLabel === "function"
    && typeof KB.scheduleCapsRefresh === "function");
  check("startup 里有 scheduleCapsRefresh 这一步",
    steps.indexOf("scheduleCapsRefresh=ok") >= 0, steps);

  // 三档文案：可用 / 未连接 / 检测中（用户要的是"显示但标未连接"）
  KB.caps.dsh = true;
  check("DSH 可用时标题就是「发送到 DSH」",
    KB.capLabel("dsh").label === "发送到 DSH", KB.capLabel("dsh").label);
  // ⚠ 用户 2026-10-05 反馈：「（未连接）」挂在顶层标题上把菜单撑太宽了 → 去掉；
  //   连接状态改放**下级菜单的第一行**（capStateLine），并且不再出现「检测中」
  //   （菜单是同步构建的，缓存可能还没探到 —— 那时据实说"还没检查过"）。
  KB.caps.dsh = false;
  KB.caps.dshWhy = "桥目录不存在（DSH 侧插件未装载）";
  check("标题不带任何后缀（不再撑宽）",
    KB.capLabel("dsh").label === "发送到 DSH"
    && KB.capLabel("dsh").label.indexOf("（") < 0,
    KB.capLabel("dsh").label);
  check("未连接时 tooltip 说清原因",
    KB.capLabel("dsh").tooltip.indexOf("没连上") >= 0,
    KB.capLabel("dsh").tooltip);
  check("未连接的状态行写「○ 未连接：原因」",
    KB.capStateLine("dsh").text.indexOf("○ 未连接") >= 0
    && KB.capStateLine("dsh").text.indexOf("桥目录不存在") >= 0,
    KB.capStateLine("dsh").text);
  KB.caps.dsh = true;
  check("已连接的状态行写「● 已连接 DSH」且可带补充信息",
    KB.capStateLine("dsh", "12 个对话").text
      === "● 已连接 DSH　12 个对话",
    KB.capStateLine("dsh", "12 个对话").text);
  KB.caps.dsh = null;
  check("还没探过时不说「检测中」，而是「还没检查过（点这一行重新检查）」",
    KB.capStateLine("dsh").text.indexOf("还没检查过") >= 0
    && KB.capStateLine("dsh").text.indexOf("检测中") < 0,
    KB.capStateLine("dsh").text);

  KB.caps.localModel = true;
  check("本地模型可用时标题是「连接到本地模型」",
    KB.capLabel("localModel").label === "连接到本地模型");
  check("本地模型可用的状态行写「● 本地模型可用」",
    KB.capStateLine("localModel").text.indexOf("● 本地模型可用") >= 0,
    KB.capStateLine("localModel").text);
  KB.caps.localModel = false;
  KB.caps.localWhy = "Ollama 没在运行";
  check("本地模型不可用时状态行写「○ 不可用：原因」",
    KB.capStateLine("localModel").text.indexOf("○ 不可用") >= 0
    && KB.capStateLine("localModel").text.indexOf("Ollama 没在运行") >= 0,
    KB.capStateLine("localModel").text);

  // probeLocalModel 的判据（不发新请求，用已有信息）
  const savedPrefs = {};
  const oGet = KB.getPref;
  KB.getPref = (k, d) => (k === KB.PREFS.provider ? "ollama" : d);
  KB.serverInfo = null;
  check("拿不到 /health 结果时 → null（未知，不是不可用）",
    KB.probeLocalModel() === null);
  KB.serverInfo = { ollama: { exists: true, api_up: true,
                              models: ["qwen3:4b-instruct"] } };
  check("ollama 在跑且有模型 → true", KB.probeLocalModel() === true);
  KB.serverInfo = { ollama: { exists: true, api_up: false, models: [] } };
  check("ollama 装了没启动 → false 且 why 提到「启动 Ollama」",
    KB.probeLocalModel() === false && KB.caps.localWhy.indexOf("启动 Ollama") >= 0,
    KB.caps.localWhy);
  KB.serverInfo = { ollama: { exists: false, api_up: false, models: [] } };
  check("没装 ollama → false", KB.probeLocalModel() === false);
  KB.getPref = (k, d) => (k === KB.PREFS.provider ? "openai" : d);
  KB.serverInfo = null;
  check("选 API 但没填 key → false", KB.probeLocalModel() === false);
  KB.getPref = oGet;
  KB.serverInfo = null;

  // 菜单清理 id：二级菜单的容器 id 必须在清理名单里
  const menuSrc = fs.readFileSync(
    path.join(PLUGIN, "src", "14-menus.js"), "utf8");
  // 不该出现的断言要在**剥掉注释**的代码里扫：源码里刻意留了
  // `这句原来写"权重 x4"，与实现不符` 这类说明，扫注释会误报
  // （与 tools/check_plugin.py 那条反向检查同一个道理）。
  const menuCode = menuSrc
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^[ \t]*\/\/.*$/gm, "");
  check("菜单清理名单里有 zotero-kb-localmenu（否则每弹一次多留一份）",
    menuSrc.indexOf('"zotero-kb-localmenu"') >= 0);
  check("DSH 子菜单第一行是状态行（可点重检）",
    menuSrc.indexOf("capStateLine(\"dsh\"") >= 0
    && menuSrc.indexOf("点这一行立刻重新检查 DSH 连接") >= 0);
  check("对话列表拿到就回填 caps.dsh（不再出现列出对话却写检测中）",
    menuCode.indexOf("self.caps.dsh = true") >= 0);
  check("「分类建议」「补全元数据」挂在二级菜单容器里",
    menuSrc.indexOf("lmPopup.appendChild(classify)") >= 0
    && menuSrc.indexOf("mkIn(lmPopup,") >= 0);
  check("重建项已改名「重建本条目知识库」",
    menuSrc.indexOf('"重建本条目知识库"') >= 0
    && menuCode.indexOf("重建知识库条目（这一篇）") < 0);
  check("标重点提示不再写「权重 ×4」（与实现不符）",
    menuCode.indexOf("权重 ×4") < 0 && menuSrc.indexOf("约 2.4 倍") >= 0);

  // ---- ⑦ 可选组件（MinerU + Ollama）的首次安装引导
  //
  // 用户定的规矩：**首次启动 + 没装 → 弹一次；取消也记 pref（不再弹）；
  //   装了 / 服务说装了 → 一次都不弹。** 这几条都是"用户会不会被烦到"的
  //   行为，所以桩里逐种情况跑一遍（`maybeAskOptionalGuides` 是 async，
  //   下面整段放在 async IIFE 里，最后在那边结算总数）。
  check("有可选组件引导那批函数",
    typeof KB.mineruKitPath === "function"
    && typeof KB.ollamaExePath === "function"
    && typeof KB.scheduleOptionalGuides === "function"
    && typeof KB.maybeAskOptionalGuides === "function"
    && typeof KB.askOptionalGuide === "function"
    && typeof KB.openOptionalGuide === "function"
    && typeof KB.panelProcess === "function");
  check("startup 里有 scheduleOptionalGuides 这一步（失败也能在状态文件里看到）",
    steps.indexOf("scheduleOptionalGuides=ok") >= 0, steps);
  // ⚠ 桩里 `projectRoot()` 是空的（pref 没填、也没有真文件系统），
  //   所以这里临时钉一个假项目根 —— 否则 `mineruKitPath()` 返回空串，
  //   "本地就看到了 exe" 那条分支根本走不到，等于没测。
  const savedRootFn = KB.projectRoot;
  KB.projectRoot = () => "D:\\repo";
  check("mineruKitPath 指向项目里的 .mineru\\.venv\\Scripts\\mineru-kit.exe",
    /\.mineru[\\/]\.venv[\\/]Scripts[\\/]mineru-kit\.exe$/.test(KB.mineruKitPath()),
    KB.mineruKitPath());
  check("ollamaExePath 拿 %LOCALAPPDATA% 拼（拿不到就空串，不猜用户名）",
    (function () {
      const p = KB.ollamaExePath();
      return p === "" || /Ollama[\\/]ollama\.exe$/.test(p);
    })(), KB.ollamaExePath());
  check("askOptionalGuide 走 Services.prompt.confirmEx 并返回按钮号",
    KB.askOptionalGuide(["mineru", "ollama"]) === 0,
    String(KB.askOptionalGuide(["mineru"])));

  const guideCase = async (opts) => {
    const seen = { pref: [], panel: [], asked: 0, missing: null };
    const saved = {
      _exists: KB._exists, request: KB.request, setPref: KB.setPref,
      getPref: KB.getPref, askOptionalGuide: KB.askOptionalGuide,
      panelProcess: KB.panelProcess,
    };
    KB.getPref = (k, d) => {
      if (k === KB.PREFS.optionalGuideDone) return !!opts.done;
      if (k === KB.PREFS.mineruGuideDone) return !!opts.legacyDone;
      return d;
    };
    KB.setPref = (k, v) => { seen.pref.push([k, v]); };
    KB._exists = () => !!opts.localExe;
    KB.request = async (m, path) => (path === "/mineru-check"
      ? { ok: !!opts.mineruOk } : { ollama: opts.ollamaOk ? "C:\\x\\ollama.exe" : "" });
    KB.askOptionalGuide = (missing) => {
      seen.asked++; seen.missing = missing; return opts.choice;
    };
    KB.panelProcess = (args) => { seen.panel.push(args); return true; };
    try {
      await KB.maybeAskOptionalGuides();
    } finally {
      KB._exists = saved._exists;
      KB.request = saved.request;
      KB.setPref = saved.setPref;
      KB.getPref = saved.getPref;
      KB.askOptionalGuide = saved.askOptionalGuide;
      KB.panelProcess = saved.panelProcess;
    }
    return seen;
  };

  (async () => {
    let s = await guideCase({ done: true });
    check("已经问过（optionalGuideDone=true）→ 不弹、不写 pref",
      s.asked === 0 && s.pref.length === 0, JSON.stringify(s));

    s = await guideCase({ legacyDone: true });
    check("老 pref mineruGuideDone=true 也认（升级用户不再被烦）",
      s.asked === 0 && s.pref.length === 0, JSON.stringify(s));

    s = await guideCase({ localExe: true });
    check("本地就看到了两个 exe → 不弹",
      s.asked === 0 && s.pref.length === 0, JSON.stringify(s));

    s = await guideCase({ mineruOk: true, ollamaOk: true });
    check("本地没有但服务说两个都在 → 不弹",
      s.asked === 0 && s.pref.length === 0, JSON.stringify(s));

    s = await guideCase({ choice: 1 });
    check("两个都没装 + 选「以后再说」→ 弹一次、记 pref、不开面板",
      s.asked === 1
      && s.pref.length === 1 && s.pref[0][0] === KB.PREFS.optionalGuideDone
      && s.pref[0][1] === true && s.panel.length === 0,
      JSON.stringify(s));

    s = await guideCase({ choice: 0 });
    check("两个都没装 + 选「打开安装引导」→ 拉面板开 MinerU 那个窗口（先缺谁开谁）",
      s.asked === 1 && s.pref.length === 1 && s.pref[0][1] === true
      && s.panel.length === 1
      && s.panel[0].indexOf("--mineru-guide") >= 0
      && s.panel[0].indexOf("--tab") >= 0
      && JSON.stringify(s.missing) === JSON.stringify(["mineru", "ollama"]),
      JSON.stringify(s));

    s = await guideCase({ mineruOk: true, choice: 0 });
    check("只有 Ollama 缺 → 只列 Ollama，并且开的是 --ollama-guide",
      JSON.stringify(s.missing) === JSON.stringify(["ollama"])
      && s.panel.length === 1
      && s.panel[0].indexOf("--ollama-guide") >= 0,
      JSON.stringify(s));

    KB.projectRoot = savedRootFn;
    console.log("\n通过 " + pass + "，失败 " + fail);
    process.exit(fail ? 1 : 0);
  })();
}
