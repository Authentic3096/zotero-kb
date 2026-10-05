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
  check("没有注册任何内容窗格分区（窗格已删除）", calls.sections.length === 0,
    JSON.stringify(calls.sections.map((s) => s.paneID)));
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
}

console.log("\n通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
