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
    querySelectorAll() { return []; },
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
  querySelectorAll: () => [],
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
  check("startup 各步全过（含 initLocale/registerItemPane/ReaderEvents/QuitGuard）",
    steps.indexOf("FAIL") < 0
    && steps.indexOf("initLocale=ok") >= 0
    && steps.indexOf("registerItemPane=ok") >= 0
    && steps.indexOf("registerReaderEvents=ok") >= 0
    && steps.indexOf("registerQuitGuard=ok") >= 0, steps);

  // ---- ⑤ 界面文案真的出得来（2026-10-05 用户报"按钮全是空框"）
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
  check("ftl 里确实有每条 id 的中文（zh-CN 与 en-US 的 id 集合一致）",
    Object.keys(MSG_ZH).length > 20
    && Object.keys(MSG_ZH).sort().join() === Object.keys(MSG_EN).sort().join(),
    `zh=${Object.keys(MSG_ZH).length} en=${Object.keys(MSG_EN).length}`);

  const sec = calls.sections[0];
  check("注册了内容窗格分区", !!sec, "registerSection 没被调用");
  if (sec) {
    check("paneID 与 PANE_ID 一致", sec.paneID === KB.PANE_ID, sec.paneID);
    check("pluginID 是插件 id", sec.pluginID === "zotero-kb@authentic3096.github.io",
      sec.pluginID);
    check("header 有 l10nID 与 icon",
      !!(sec.header && sec.header.l10nID && sec.header.icon),
      JSON.stringify(sec.header));
    check("sidenav 有 l10nID 与 icon（20×20 那版）",
      !!(sec.sidenav && sec.sidenav.l10nID
        && /sidenav-icon\.svg$/.test(sec.sidenav.icon)),
      JSON.stringify(sec.sidenav));
    check("onRender / onItemChange 都是函数",
      typeof sec.onRender === "function" && typeof sec.onItemChange === "function");
    check("sectionButtons 至少一个（清空对话）",
      Array.isArray(sec.sectionButtons) && sec.sectionButtons.length > 0
      && typeof sec.sectionButtons[0].onClick === "function");
  }

  const rd = calls.readers[0];
  check("注册了阅读器选中事件", !!rd && rd.type === "renderTextSelectionPopup",
    JSON.stringify(calls.readers.map((r) => r.type)));
  check("注册时带了 pluginID（卸载时自动摘）",
    !!rd && rd.pluginID === "zotero-kb@authentic3096.github.io",
    rd && rd.pluginID);

  check("观察了 quit-application-requested（退出提醒）",
    calls.observers.some((o) => o.t === "quit-application-requested"),
    JSON.stringify(calls.observers.map((o) => o.t)));

  // onRender 真跑一遍：桩 doc/body，看它建了哪些控件、有没有抛
  if (sec && sec.onRender) {
    const body = makeEl();
    try {
      sec.onRender({ doc, body, item: { key: "AAAA1111", isRegularItem: () => true } });
      check("onRender 能建出界面", body.children.length >= 4,
        "子元素 " + body.children.length);
      const kinds = body.children.map((c) => c.attributes["data-l10n-id"] || c.tagName || "div");
      check("输入框用了 FTL 的 placeholder 属性形式",
        body.children.some((c) => c.attributes["data-l10n-attrs"] === "placeholder"),
        JSON.stringify(kinds));
      check("输入框的文案没有被写成内容（placeholder 那个坑）",
        body.children.every((c) => !/placeholder-ask/.test(c._text || "")),
        "textarea 的 textContent/textContent 里出现了提示语");

      // ⚠ 这条就是用户截图报的那个毛病：桩环境的文档**不翻译**，
      //   所以按钮必须有非空文字（来自 l10n() 的同步兜底），
      //   而且文字要真的取自 ftl（不是 raw id）。
      const buttons = [];
      const collect = (el) => {
        if (el.tagName === "button" || el.attributes["data-l10n-id"]) {
          buttons.push(el);
        }
        (el.children || []).forEach(collect);
      };
      body.children.forEach(collect);
      const labels = buttons
        .filter((b) => /^zotero-kb-btn-/.test(b.attributes["data-l10n-id"] || ""))
        .map((b) => String(b._text || ""));
      check("每个按钮都有非空文字（不允许出现「空框按钮」）",
        labels.length >= 5 && labels.every((t) => t.trim().length > 0),
        JSON.stringify(labels));
      check("按钮文字取自 ftl（不是 raw id）",
        labels.every((t) => !/^zotero-kb-/.test(t)),
        JSON.stringify(labels));
      const ids = buttons
        .map((b) => b.attributes["data-l10n-id"])
        .filter((x) => /^zotero-kb-/.test(x || ""));
      const missing = ids.filter((id) => !MSG_ZH[id]);
      check("用到的每个 l10n id 都在 ftl 里有中文", missing.length === 0,
        JSON.stringify(missing));
    } catch (e) {
      check("onRender 能建出界面", false, String(e));
    }
  }
}

console.log("\n通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
