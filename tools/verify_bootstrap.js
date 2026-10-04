/**
 * 用 Node 的 vm 把真机 bootstrap.js 跑起来，断言：
 *   ① 插件对象能建起来、startup 十步全过；
 *   ② `ItemPaneManager.registerSection` 被调用，且 **header/sidenav 的 l10nID
 *      与图标都在**（这两项是必填，缺了就是"分区标题空白、无报错"）；
 *   ③ `Reader.registerEventListener('renderTextSelectionPopup', …, pluginID)`
 *      被注册（公开接口，第三个参数用于卸载时自动摘）；
 *   ④ `quit-application-requested` 被观察（退出提醒）。
 *
 * 为什么需要它：真机验证要重启 Zotero，而插件里"名字写错/字段名写错"这类问题
 * 在 Zotero 里**没有任何报错**，只表现为"按钮点了没反应"。这个桩环境能把
 * 结构与参数先钉一遍，真机只需要确认"看得见、点得动"。
 *
 * 用法：node tools/verify_bootstrap.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PLUGIN = path.join(__dirname, "..", "zotero-plugin");
const SRC = fs.readFileSync(path.join(PLUGIN, "bootstrap.js"), "utf8");

let pass = 0, fail = 0;
const check = (name, cond, detail) => {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + "  " + (detail || "")); }
};

const calls = {
  sections: [], readers: [], observers: [], prefs: {}, status: [], notes: 0,
  http: [],
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
  querySelector: () => null,
  querySelectorAll: () => [],
  l10n: { setAttributes(el, id) { el.setAttribute("data-l10n-id", id); } },
  documentElement: makeEl(),
  getElementById: () => null,
};

const Zotero = {
  debug: () => {},
  logError: () => {},
  getMainWindow: () => ({ document: doc, ZoteroPane: {} }),
  Prefs: {
    get: (k) => (k in calls.prefs ? calls.prefs[k] : undefined),
    set: (k, v) => { calls.prefs[k] = v; },
    registerObserver: () => {},
    unregisterObserver: () => {},
  },
  Notifier: { registerObserver: () => 1, unregisterObserver: () => {} },
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
  check("startup 十步全过（含 registerItemPane/ReaderEvents/QuitGuard）",
    steps.indexOf("FAIL") < 0
    && steps.indexOf("registerItemPane=ok") >= 0
    && steps.indexOf("registerReaderEvents=ok") >= 0
    && steps.indexOf("registerQuitGuard=ok") >= 0, steps);

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
    } catch (e) {
      check("onRender 能建出界面", false, String(e));
    }
  }
}

console.log("\n通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
