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
  columns: [],       // ItemTreeManager.registerColumn 收到的列定义（T0-8 的两列）
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
  // 自定义列（16-weightcol.js / 21-kbcols.js）：把注册进来的列定义记下来。
  // ⚠ 真机里 registerColumn 返回的 dataKey 会带 pluginID 前缀，桩里按原样
  //   返回即可 —— 下面的断言只看列定义本身，不看前缀。
  // ⚠ 权重列那一步在桩里是**被替换掉的**（见下面 KB.registerWeightColumn），
  //   所以这里记到的是 T0-8 新加的两列。
  ItemTreeManager: {
    registerColumn(opts) { calls.columns.push(opts); return opts.dataKey; },
    unregisterColumn() { return true; },
    refreshColumns() {},
    isCustomColumn: () => false,
    getCustomCellData: () => "",
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
  // 图片相对路径 → file:// （20-kbview.js 的 kbviewFixImgs 用它；桩里给个够用的实现）
  io: {
    newFileURI: (f) => ({ spec: "file:///" + String(f.path).replace(/\\/g, "/") }),
    newURI: (u) => ({ spec: String(u) }),
    // 自建列要注册 resource 包（resource://zotero-kb/ 与 …-files/），桩里给个空实现
    getProtocolHandler: () => ({
      QueryInterface() { return this; },
      setSubstitution() {},
      resolveURI: (u) => ({ spec: String(u.spec) }),
    }),
  },
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

  // ---- ⑧ 文献列表两列（T0-8）：「已建知识库」「分节纲要」
  //
  // 这两列的数据全部来自本地服务的 GET /col-status。桩里把缓存直接填好，
  // 逐条断言**取值规则**（✓/✗、补零的节数、拿不到服务时留空）——
  // 真机上"列是空的"是没有任何报错的，只能在这里钉死。
  const colBuilt = calls.columns.find((c) => c.dataKey === "kbBuilt");
  const colOutline = calls.columns.find((c) => c.dataKey === "kbOutline");
  const dp = (col, key) =>
    (col && col.dataProvider ? col.dataProvider({ key }) : "<没有这一列>");
  check("startup 里有 registerKbColumns 这一步",
    steps.indexOf("registerKbColumns=ok") >= 0, steps);
  check("注册了 kbBuilt / kbOutline 两列",
    !!colBuilt && !!colOutline,
    JSON.stringify(calls.columns.map((c) => c.dataKey)));
  check("两列的 label / enabledTreeIDs / flex / minWidth 都对",
    !!colBuilt && !!colOutline
    && colBuilt.label === "已建知识库" && colOutline.label === "分节纲要"
    && colBuilt.enabledTreeIDs.join() === "main"
    && colOutline.enabledTreeIDs.join() === "main"
    && colBuilt.flex === 1 && colOutline.flex === 1
    && colBuilt.minWidth > 0,
    JSON.stringify([colBuilt, colOutline].map((c) => c && [c.label, c.flex])));
  KB.colStatusCache = {
    ABCD1234: { in_kb: true, outline_sections: 12 },
    IJKL9012: { in_kb: true, outline_sections: 4 },
    EFGH5678: { in_kb: true, outline_sections: 0 },
    NOPE0001: { in_kb: false, outline_sections: 0 },
  };
  KB.colStatusReady = true;
  check("已建知识库列：在库 ✓ / 不在库 ✗",
    dp(colBuilt, "ABCD1234") === "✓" && dp(colBuilt, "NOPE0001") === "✗",
    dp(colBuilt, "ABCD1234") + dp(colBuilt, "NOPE0001"));
  check("分节纲要列：有纲要给节数 / 没有给 ✗",
    dp(colOutline, "ABCD1234") === "012" && dp(colOutline, "EFGH5678") === "✗",
    dp(colOutline, "ABCD1234") + dp(colOutline, "EFGH5678"));
  check("节数补零 → 字典序等于数值序（4 节排在 12 节前）",
    dp(colOutline, "IJKL9012") === "004"
    && dp(colOutline, "IJKL9012") < dp(colOutline, "ABCD1234"),
    dp(colOutline, "IJKL9012") + " vs " + dp(colOutline, "ABCD1234"));
  check("给人看的文案去掉补零、带上「节」",
    KB.kbColText("012", "outline") === "12 节"
    && KB.kbColText("004", "outline") === "4 节"
    && KB.kbColText("✗", "outline") === "✗"
    && KB.kbColText("✓", "built") === "✓",
    KB.kbColText("012", "outline"));
  KB.colStatusReady = false;
  check("一次都没拉到服务时两列留空（不把「没数据」画成「没建库」）",
    dp(colBuilt, "ABCD1234") === "" && dp(colOutline, "ABCD1234") === "");
  check("没有 item / 没有 key 时不抛",
    !!colBuilt && colBuilt.dataProvider(null) === ""
    && colBuilt.dataProvider({}) === "");
  KB.colStatusReady = true;
  // 渲染结构：className 含 cell + 文字在**内层** span 里 ——
  // 外层这个 title 不会被虚拟表格的 _handleMouseOver 清掉，就靠这一层
  // （见 21-kbcols.js 的 kbColCell 说明）。
  const cell = colOutline && colOutline.renderCell(0, "012", { className: "kb-x" });
  check("renderCell 复刻标准单元格结构（class 含 cell、文字在内层 span）",
    !!cell && /(^|\s)cell(\s|$)/.test(cell.className)
    && cell.children.length === 1
    && cell.children[0].textContent === "12 节",
    cell ? cell.className + " / "
      + JSON.stringify(cell.children.map((c) => c.textContent)) : "没有 renderCell");
  check("renderCell 在外层给了 title（悬停提示）",
    !!cell && typeof cell.title === "string" && cell.title.indexOf("纲要") >= 0,
    cell ? String(cell.title) : "");
  check("这两列没有用 htmlLabel 之类没人读的字段去冒充表头 tooltip",
    !!colBuilt && !!colOutline
    && colBuilt.htmlLabel === undefined && colOutline.htmlLabel === undefined);
  // ⚠ 这条是**真机踩出来的回归**：不写 defaultIn 时，列注册成功、_columns 里也有，
  //   但列表上就是看不见（被 _getColumns 算成 hidden）—— 用户只能自己去右键
  //   "列"菜单里把它勾出来。桩环境抓不到"看得见看不见"，所以在这里把字段钉死。
  check("两列都带 defaultIn（否则注册了也不会显示）",
    !!colBuilt && !!colOutline
    && String(colBuilt.defaultIn) === "*" && String(colOutline.defaultIn) === "*",
    JSON.stringify([colBuilt && colBuilt.defaultIn,
                    colOutline && colOutline.defaultIn]));

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
  // ---- ⑥b 2026-10-11：自建列（不再用 registerSection）
  //
  // 桩环境里 Zotero.getMainWindow().document 没有主窗口那份 DOM，所以 startup 时
  // 会自动**降级**回分区模式 —— 下面这组断言钉的正是那条兜底路（"取不到就不让
  // 面板消失"）。真正的自建列在后面的 fake DOM 里单独测。
  check("取不到主窗口 DOM 时降级注册了「知识库」分区",
    calls.sections.length === 1
    && calls.sections[0].paneID === "zotero-kb-kbview",
    JSON.stringify(calls.sections.map((s) => s.paneID)));
  const sec0 = calls.sections[0] || {};
  check("降级分区的 header/sidenav l10nID 与图标都在（缺了就是标题空白且不报错）",
    sec0.header && sec0.header.l10nID === "zotero-kb-kbview-header"
    && sec0.header.icon && sec0.sidenav
    && sec0.sidenav.l10nID === "zotero-kb-kbview-sidenav"
    && sec0.sidenav.icon, JSON.stringify(sec0.header));
  check("状态文件里如实记了降级原因（不是静默降级）",
    (calls.status.some((s) => String(s.kbViewColumn || "").indexOf("degraded") >= 0)),
    JSON.stringify(calls.status.filter((s) => s.kbViewColumn).slice(-2)));
  // ⚠ 阅读器也要认（用户："在文献打开后就没有插件图标了"）——
  //   "对着 PDF 读纲要"正是最常用的场景。
  check("分区在条目库与阅读器页签都开、非普通条目不开",
    KB.kbviewEnabled({ isRegularItem: () => true }, "library") === true
    && KB.kbviewEnabled({ isRegularItem: () => true }, "reader") === true
    && KB.kbviewEnabled({ isRegularItem: () => false }, "library") === false
    && KB.kbviewEnabled({ isRegularItem: () => true }, "reader-unloaded") === false
    && KB.kbviewEnabled(null, "library") === false);

  // ---- ⑥c 自建列本体：用一份假 DOM 把"装 → 开 → 关 → 卸"整条走一遍
  //
  // 为什么值得专门做一份假 DOM：真机验证要重启 Zotero，而自建列最容易出的两类
  // 事故（**漏 remove 导致两份**、**关了列没还原原生分区列**）在静态代码里看不出来。
  const fake = (function () {
    const ids = {};
    const camel = (k) => String(k).replace(/-([a-z])/g, (m, c) => c.toUpperCase());
    const mkStyle = () => ({
      setProperty(k, v) { this[camel(k)] = v; },
      // 真的 CSSStyleDeclaration 会把 style="a:b;c:d" 属性解析成属性访问；
      // 假 DOM 不模拟这一步的话，"列默认 display:none" 这类断言会假失败。
      _apply(text) {
        String(text).split(";").forEach((part) => {
          const i = part.indexOf(":");
          if (i < 0) return;
          const n = part.slice(0, i).trim();
          if (n) this[camel(n)] = part.slice(i + 1).trim();
        });
      },
    });
    const mkEl = (tag) => {
      const el = {
        tagName: tag, children: [], parentElement: null, attributes: {},
        style: mkStyle(), listeners: {},
        classList: { add() {}, remove() {}, contains: () => false },
        setAttribute(k, v) {
          this.attributes[k] = String(v);
          if (k === "id") ids[String(v)] = this;
          if (k === "style") this.style._apply(String(v));
        },
        getAttribute(k) {
          return Object.prototype.hasOwnProperty.call(this.attributes, k)
            ? this.attributes[k] : null;
        },
        removeAttribute(k) { delete this.attributes[k]; },
        hasAttribute(k) {
          return Object.prototype.hasOwnProperty.call(this.attributes, k);
        },
        addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
        removeEventListener() {},
        fire(t) { (this.listeners[t] || []).forEach((fn) => fn({ target: this })); },
        appendChild(c) {
          if (c.parentElement) c.parentElement.removeChild(c);
          this.children.push(c); c.parentElement = this; return c;
        },
        insertBefore(c, ref) {
          if (c.parentElement) c.parentElement.removeChild(c);
          const i = ref ? this.children.indexOf(ref) : -1;
          if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
          c.parentElement = this; return c;
        },
        removeChild(c) {
          const i = this.children.indexOf(c);
          if (i >= 0) { this.children.splice(i, 1); c.parentElement = null; }
          return c;
        },
        remove() { if (this.parentElement) this.parentElement.removeChild(this); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        get firstChild() { return this.children[0] || null; },
        set textContent(v) { this._text = v; this.children = []; },
        get textContent() { return this._text; },
      };
      return el;
    };
    const doc = {
      createXULElement: (t) => mkEl(t),
      createElement: (t) => mkEl(t),
      createElementNS: (ns, t) => mkEl(t),
      getElementById: (id) => ids[id] || null,
      querySelector: () => null,
      head: mkEl("head"),
      documentElement: mkEl("htmlElement"),
    };
    // <item-pane id="zotero-item-pane">
    //   <deck id="zotero-item-pane-content" selectedIndex="0"/>
    //   <item-pane-sidenav id="zotero-view-item-sidenav"/>
    const pane = mkEl("item-pane");
    pane.setAttribute("id", "zotero-item-pane");
    const deck = mkEl("deck");
    deck.setAttribute("id", "zotero-item-pane-content");
    deck.setAttribute("selectedIndex", "0");
    const sidenav = mkEl("item-pane-sidenav");
    sidenav.setAttribute("id", "zotero-view-item-sidenav");
    pane.appendChild(deck);
    pane.appendChild(sidenav);
    const win = {
      document: doc,
      listeners: {},
      addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
      removeEventListener() {},
      Zotero_Tabs: { selectedType: "library", selectedID: "t1" },
      ZoteroPane: { getSelectedItems: () => [fakeItem] },
      innerWidth: 1400,
    };
    const fakeItem = {
      key: "ABCD1234", libraryID: 1, parentID: null,
      isRegularItem: () => true,
      getField: (f) => (f === "title" ? "测试文献" : ""),
    };
    return { win, doc, pane, deck, sidenav, ids, fakeItem };
  })();

  check("自建列能装上（返回 true）", KB.kbviewMount(fake.win) === true);
  const host = KB.kbviewHost(fake.win, false);
  check("rail 上多了我们的按钮（且不放在 .inherit-flex 里、不带 data-pane）",
    !!host && !!host.lib && host.lib.button.parentElement === fake.sidenav
    && host.lib.button.getAttribute("class").indexOf("btn") >= 0
    && host.lib.button.getAttribute("data-pane") === null
    && host.lib.button.getAttribute("id") === "zotero-kb-kbview-entry-library",
    JSON.stringify(fake.sidenav.children.map((c) => c.getAttribute("id"))));
  check("按钮的图标走 rootURI（sidenav-icon.svg）",
    !!host && String(host.lib.button.style.backgroundImage || "").indexOf("sidenav-icon.svg") >= 0,
    host && host.lib.button.style.backgroundImage);
  check("我们那一列插在原生分区列与 rail 之间（不是塞进分区列里）",
    !!host && host.lib.container.parentElement === fake.pane
    && fake.pane.children.indexOf(host.lib.container) === 1
    && fake.pane.children.indexOf(fake.sidenav) === 2
    && fake.pane.children.indexOf(fake.deck) === 0,
    JSON.stringify(fake.pane.children.map((c) => c.getAttribute("id"))));
  check("列默认是藏起来的（display:none）",
    !!host && host.lib.container.style.display === "none");
  // 2026-10-13 契约变了：装列时**不立刻加载**那份页面（实测 863ms，用户报的
  // "启动慢"就是它）——url 先记在 data-kbview-src 上，等窗口空闲或用户点开时
  // 才由 kbviewEnsurePage() 挂到 src。所以下面分三步验：
  //   ① 装列时 src 为空、data-kbview-src 指向我们的 content/kbview.html；
  //   ② kbviewEnsurePage 一调，src 真的变成它；
  //   ③ 再调一次是幂等的（不会重复挂）。
  check("列里装的是 <browser>，页面地址指向插件的 content/kbview.html（先不加载）",
    !!host && host.lib.browser.tagName === "browser"
    && !host.lib.browser.getAttribute("src")
    && String(host.lib.browser.getAttribute("data-kbview-src"))
         .indexOf("content/kbview.html") >= 0,
    host && JSON.stringify({
      src: host.lib.browser.getAttribute("src"),
      pending: host.lib.browser.getAttribute("data-kbview-src"),
    }));
  check("kbviewEnsurePage() 会把 src 挂上（就是上面那个地址）",
    KB.kbviewEnsurePage(fake.win, "library")
    && String(host.lib.browser.getAttribute("src"))
         .indexOf("content/kbview.html") >= 0,
    host && host.lib.browser.getAttribute("src"));
  check("kbviewEnsurePage() 幂等（第二次调不重复挂、仍返回 true）",
    KB.kbviewEnsurePage(fake.win, "library") === true
    && String(host.lib.browser.getAttribute("src"))
         .indexOf("content/kbview.html") >= 0);

  // 打开：原生分区列让开 + 按钮按下 + 轮询起来
  KB.kbviewOpen(fake.win, "library");
  check("打开后原生分区列被藏起来（hidden=true）",
    fake.deck.getAttribute("hidden") === "true");
  check("打开后我们那一列显示出来（display:flex）",
    host.lib.container.style.display === "flex");
  check("打开后按钮标成按下（aria-pressed=true）",
    host.lib.button.getAttribute("aria-pressed") === "true"
    && host.lib.button.getAttribute("data-kbview-open") === "true");
  check("打开后条目切换轮询起来了（timerArmed=true）",
    host.timerArmed === true);
  check("打开时先存下了原生列的原状态（hidden 原值是 null）",
    !!host.lib.saved && host.lib.saved.deckHidden === null);

  // rail 按钮点一下 = 切换（command 事件）
  fake.sidenav.children[0].fire("command");
  check("再点一下 rail 按钮 → 收起（原生分区列还原）",
    fake.deck.getAttribute("hidden") === null
    && host.lib.container.style.display === "none"
    && host.lib.button.getAttribute("aria-pressed") === "false"
    && host.timerArmed === false,
    JSON.stringify({ hidden: fake.deck.getAttribute("hidden"),
                     disp: host.lib.container.style.display,
                     armed: host.timerArmed }));

  // 原状态是「本来就 hidden」时，关掉之后必须还是 hidden（别替用户改状态）
  fake.deck.setAttribute("hidden", "true");
  KB.kbviewOpen(fake.win, "library");
  KB.kbviewClose(fake.win, "library");
  check("原生列原本就 hidden → 关掉后仍然 hidden（逐项还原，不是一律清掉）",
    fake.deck.getAttribute("hidden") === "true",
    String(fake.deck.getAttribute("hidden")));
  fake.deck.removeAttribute("hidden");

  // 多窗口：同一份逻辑对第二个窗口要各装一份
  check("kbviewCurrentItem 认「当前选中的那一条」",
    (function () {
      const it = KB.kbviewCurrentItem(fake.win);
      return it && it.key === "ABCD1234";
    })());
  check("kbviewCurrentItem 在附件上取**父条目**（知识库按父条目 key 建）",
    (function () {
      const savedGet = Zotero.Items.get;
      Zotero.Items.get = (id) => (id === 77
        ? { key: "PARENT01", libraryID: 1, parentID: null, isRegularItem: () => true }
        : null);
      const savedSel = fake.win.ZoteroPane.getSelectedItems;
      fake.win.ZoteroPane.getSelectedItems = () => [
        { key: "ATTACH01", libraryID: 1, parentID: 77, isRegularItem: () => false }];
      const it = KB.kbviewCurrentItem(fake.win);
      fake.win.ZoteroPane.getSelectedItems = savedSel;
      Zotero.Items.get = savedGet;
      return it && it.key === "PARENT01";
    })());

  // 内容组装（async，断言放到文件末尾那个 async IIFE 里结算）
  const payloadNoItem = KB.kbviewPayloadForItem(null, null);
  const payloadNoFiles = KB.kbviewPayloadForItem(fake.fakeItem, null);
  check("kbviewPayloadForItem 返回 thenable（async）",
    !!payloadNoItem && typeof payloadNoItem.then === "function");

  // 卸载：按钮与列都要**一个不剩**（Zotero 会复用主窗口，漏了就是两份）
  KB.kbviewUnmount(fake.win);
  check("卸载后按钮与列都没了（DOM 干净）",
    fake.sidenav.children.length === 0
    && fake.pane.children.length === 2
    && fake.pane.children[0] === fake.deck
    && fake.pane.children[1] === fake.sidenav,
    JSON.stringify({ sidenav: fake.sidenav.children.length,
                     pane: fake.pane.children.length }));
  check("卸载后宿主状态也清了（Map 里没有这个窗口）",
    KB.kbviewHost(fake.win, false) === null);
  check("卸载后原生分区列的 hidden 是干净的",
    fake.deck.getAttribute("hidden") === null);

  // ---- ⑥d 源码与资源层面的断言（这几条是"文件漏打/名字写错"的保险）
  const kvSrc = fs.readFileSync(
    path.join(PLUGIN, "src", "20-kbview.js"), "utf8");
  check("自建列用的是 insertBefore(container, sidenav)，不是往分区列里塞",
    kvSrc.indexOf("insertBefore(box, roots.anchor)") >= 0
    && kvSrc.indexOf("sidenav.appendChild(btn)") >= 0);
  check("藏原生列之前先存原状态（kbviewSaveNative / kbviewRestoreNative）",
    kvSrc.indexOf("kbviewSaveNative") >= 0
    && kvSrc.indexOf("kbviewRestoreNative") >= 0
    && kvSrc.indexOf('put(r.deck, "hidden", s.deckHidden)') >= 0);
  check("页面→宿主只传 JSON 字符串（跨 principal 不传对象）",
    kvSrc.indexOf("__kbvHostPost") >= 0
    && kvSrc.indexOf("wrappedJSObject") >= 0
    && kvSrc.indexOf("kbvReceive") >= 0);
  check("定时器走 Services.tm（沙箱的 setTimeout 不可靠）",
    kvSrc.indexOf("Services.tm") >= 0 && kvSrc.indexOf("newTimer") >= 0);
  check("正文默认不读 .html 孪生（页面里用 markdown-it + KaTeX 渲染），只有降级模式要它",
    // 2026-10-11 改版：服务端 latex2mathml 渲染不了 aligned 环境，页面改吃 md
    kvSrc.indexOf("wantHtml") >= 0
    && kvSrc.indexOf("kbviewImgBase") >= 0
    && kvSrc.indexOf("公式已渲染") < 0);
  const pageHtml = fs.readFileSync(
    path.join(PLUGIN, "content", "kbview.html"), "utf8");
  check("content/kbview.html 外链了自己的 css/js（不内联，避开 CSP）",
    pageHtml.indexOf('href="kbview.css"') >= 0
    && pageHtml.indexOf('src="kbview.js"') >= 0
    && pageHtml.indexOf("<script>") < 0
    && pageHtml.indexOf("<style") < 0);
  check("content/ 三件套都在（漏打就是'列装上了、里面一片空白'）",
    fs.existsSync(path.join(PLUGIN, "content", "kbview.html"))
    && fs.existsSync(path.join(PLUGIN, "content", "kbview.css"))
    && fs.existsSync(path.join(PLUGIN, "content", "kbview.js")));
  const pageJs = fs.readFileSync(
    path.join(PLUGIN, "content", "kbview.js"), "utf8");
  check("侧栏页面把正文按 .kb-page 分块并给出「改这一页 / 恢复原版」",
    // ⚠ 选择器必须是**服务端的** .kb-page：第一版写成 .kbv-page（自己的命名空间），
    //   真机上只表现为"4 个页块一个工具条都没有"，静态看不出来。
    pageJs.indexOf('querySelectorAll(".kb-page")') >= 0
    && pageJs.indexOf("改这一页") >= 0
    && pageJs.indexOf("恢复原版") >= 0,
    pageJs.indexOf('querySelectorAll(".kb-page")'));
  const pageCss = fs.readFileSync(
    path.join(PLUGIN, "content", "kbview.css"), "utf8");
  check("侧栏样式表给 .kb-page 写了页块样式（同样的类名）",
    // ⚠ 这里查的是 "`.kb-page {`" 这个**带花括号**的完整选择器：光查 "`.kbv-page`"
    //   会被我们自己的 "`.kbv-page-head`（工具条）" 误判成"用错类名"。
    pageCss.indexOf(".kb-page {") >= 0
    && pageCss.indexOf(".kb-page:first-of-type") >= 0);
  check("点图看原图：页面把相对路径交回宿主（data-kbv-src → openImage）",
    pageJs.indexOf("data-kbv-src") >= 0
    && pageJs.indexOf("openImage") >= 0
    && kvSrc.indexOf("kbviewOpenImage") >= 0
    && kvSrc.indexOf("indexOf(\"..\") >= 0") >= 0);
  check("侧栏页面导出了 kbvReceive（宿主直接调它送内容）",
    pageJs.indexOf("window.kbvReceive") >= 0);

  // ---- 逐块编辑 / 初始页定位 / 阅读器工具栏（派发包 §二 §三）
  check("编辑器：左等宽编辑框 + 右该块实时预览 + 改动过的块标黄",
    pageJs.indexOf("kbv-block-text") >= 0
    && pageJs.indexOf("kbv-block-prev") >= 0
    && pageJs.indexOf("kbv-dirty") >= 0
    && pageCss.indexOf(".kbv-block.kbv-dirty") >= 0);
  check("编辑器有「整页源码」开关，且一律软换行（不再有几万像素的长行）",
    pageJs.indexOf("整页源码") >= 0
    && pageJs.indexOf('"soft"') >= 0
    && pageCss.indexOf("white-space: pre-wrap") >= 0);
  check("打开那一列就停在正在读的那一页（**初始页**，不是跟随）",
    pageJs.indexOf("showPage") >= 0
    && pageJs.indexOf('data-kbpage="') >= 0
    && kvSrc.indexOf("kbviewReaderPage") >= 0
    && kvSrc.indexOf("kbviewReaderInfo") >= 0);
  check("按页跟随已彻底去掉：宿主没有自动重推的调用点、页面不跟",
    // 2026-10-11 用户拍板（派发包《去跟随与上下标》§一）：跟随"没生效也不用要了"。
    // 顺带：那也是一处周期性重渲染，是"渲染偶尔抽搐"最可疑的来源。
    // ⚠ 判据不是"再没有 type:\"page\" 这个字符串" —— 保留下来的
    //   kbviewFanoutPage（一次性推当前页）内部还在用这个消息；要钉住的是
    //   **没有自动调用点**：没有 pageChange 分支、没有 poll 那一拍的重推。
    kvSrc.indexOf("self.kbviewFanoutPage(") < 0
    && kvSrc.indexOf('event === "pageChange"') < 0
    && kvSrc.indexOf("host.lastPage = page;") < 0
    && pageJs.indexOf("followPage") < 0
    && pageJs.indexOf('msg.type === "page"') < 0);
  check("kbviewFanoutPage 与 Notifier 保留（派发包 §一：别整体删掉）",
    kvSrc.indexOf("kbviewFanoutPage: function") >= 0
    && kvSrc.indexOf('}, ["tab"], self.id + "-kbview")') >= 0
    && kvSrc.indexOf("kbviewRegisterNotifier") >= 0);
  check("页工具条吸顶：每页的「p.n / 改这一页 / 恢复原版」粘在顶边（要有不透明背景）",
    pageCss.indexOf("position: sticky") >= 0
    && pageCss.indexOf("border-bottom: 1px solid var(--kbv-line)") >= 0
    && pageCss.indexOf("background: var(--kbv-bg)") >= 0
    // ⚠ 条上方不能留缝：sticky 的 top 是从滚动口内容盒量起的，必须抵消
    //   .kbv-view 的 padding-top，而这两处共用 --kbv-view-pad（改一处即可）
    && pageCss.indexOf("padding: var(--kbv-view-pad) 8px 24px") >= 0
    && pageCss.indexOf("top: calc(-1 * var(--kbv-view-pad))") >= 0
    && pageCss.indexOf("padding: var(--kbv-view-pad) 0 4px") >= 0);
  check("排不了的公式退回等宽原文并标 kbv-tex-fail（不是一大片红字）",
    pageJs.indexOf("kbv-tex-fail") >= 0
    && pageJs.indexOf("throwOnError: true") >= 0
    && pageCss.indexOf(".kbv-tex-fail") >= 0);
  check("阅读器工具栏「对照」按钮走 renderToolbar（必须同步 append）",
    kvSrc.indexOf('registerEventListener("renderToolbar"') >= 0
    && kvSrc.indexOf("kbviewAddToolbarButton") >= 0
    && kvSrc.indexOf("synchronously") >= 0);
  check("两列不能同时开（kbviewOpen 里也兜一次）",
    kvSrc.indexOf("if (self.kbviewIsOpen(win, other)) self.kbviewClose(win, other);") >= 0);

  // ---- 分块：块之间保留原文分隔符，拼回去必须**一字不差**
  const pageMd = [
    "# 标题", "", "第一段", "续行", "", "$$E = m c^2$$", "", "结语",
    "", "| a | b |", "| --- | --- |", "| 1 | 2 |", "", "![图](images/a.jpg)", "尾行",
  ].join("\n");
  const blk = KB.kbviewSplitBlocks(pageMd);
  check("kbviewSplitBlocks 能一字不改地拼回去（kbviewJoinBlocks）",
    KB.kbviewJoinBlocks(blk) === pageMd, JSON.stringify(blk));
  check("kbviewSplitBlocks 把独立公式 / 表格 / 图片行各自切成一块",
    blk.blocks.indexOf("$$E = m c^2$$") >= 0
    && blk.blocks.indexOf("| a | b |\n| --- | --- |\n| 1 | 2 |") >= 0
    && blk.blocks.indexOf("![图](images/a.jpg)") >= 0,
    JSON.stringify(blk.blocks));
  check("kbviewSplitBlocks 段内单换行仍属同一块",
    blk.blocks.indexOf("第一段\n续行") >= 0, JSON.stringify(blk.blocks));
  check("kbviewSplitBlocks 空输入不抛，且拼得回来",
    KB.kbviewJoinBlocks(KB.kbviewSplitBlocks("")) === ""
    && KB.kbviewJoinBlocks(KB.kbviewSplitBlocks(null)) === "");
  check("kbviewJoinBlocks 对缺字段的 parts 也不抛",
    KB.kbviewJoinBlocks({ blocks: ["a"] }) === "a"
    && KB.kbviewJoinBlocks(null) === "");
  const realParts = KB.kbviewSplitBlocks(
    "# H\n\n文字。\n\n$$x^2$$\n\n| a |\n| --- |\n| 1 |\n\n尾\n");
  check("真形态样例（末尾换行 + 多空行）也能一字不改地往返",
    KB.kbviewJoinBlocks(realParts) ===
      "# H\n\n文字。\n\n$$x^2$$\n\n| a |\n| --- |\n| 1 |\n\n尾\n");

  // ---- vendored 第三方库：漏了就是"公式不渲染，而且不报错"
  const vdir = path.join(PLUGIN, "content", "vendor");
  for (const rel of ["markdown-it/markdown-it.min.js",
                     "katex/katex.min.js", "katex/katex.min.css",
                     "katex/fonts/KaTeX_Main-Regular.woff2"]) {
    check("vendored 第三方库在包里：" + rel,
      fs.existsSync(path.join(vdir, rel)));
  }
  let licOk = true;
  for (const n of ["markdown-it", "katex"]) {
    if (!fs.existsSync(path.join(vdir, n, "LICENSE"))) licOk = false;
  }
  check("vendored 第三方库都带了 LICENSE（MIT，发布面要求）", licOk);
  check("页面从 vendor/ 引 KaTeX 与 markdown-it（不放 CDN，离线可用）",
    pageHtml.indexOf('href="vendor/katex/katex.min.css"') >= 0
    && pageHtml.indexOf('src="vendor/markdown-it/markdown-it.min.js"') >= 0
    && pageHtml.indexOf('src="vendor/katex/katex.min.js"') >= 0
    && pageHtml.indexOf("cdn.jsdelivr") < 0
    && pageHtml.indexOf("unpkg") < 0);
  check("打包器会递归收 content/vendor/**（否则 KaTeX 进不了包）",
    fs.readFileSync(path.join(__dirname, "..", "tools", "pack_plugin.py"), "utf8")
      .indexOf('RECURSE_DIRS = ("locale", "content")') >= 0);

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
  check("md2html 渲染链接",
    html.indexOf('<a href="https://x.test">链接</a>') >= 0);
  check("md2html 把图片渲染成 <img>（相对路径不被改写）",
    html.indexOf('<img src="images/a.jpg" alt="图注"') >= 0, html.slice(-220));
  const tbl = KB.md2html(["| a | b |", "| --- | --- |", "| 1 | 2 |"].join("\n"));
  check("md2html 把管道表渲染成 <table>",
    tbl.indexOf("<table") >= 0 && tbl.indexOf("<th") >= 0
    && tbl.indexOf("<td") >= 0 && tbl.indexOf("| --- |") < 0, tbl);
  const oldKbDir = KB.kbDir;
  KB.kbDir = () => "C:\\kb";
  const fixedImg = KB.kbviewFixImgs('<img src="images/a.jpg">', "ABC");
  check("kbviewFixImgs 把相对 src 改写成 resource://zotero-kb-files/（含 mineru/<key>）",
    // ⚠ 不能是 file://：resource:// 页面加载 file:// 图片会被安全策略拦掉
    //   （真机实测图片全空白）
    fixedImg.indexOf("resource://zotero-kb-files/mineru/ABC/images/a.jpg") >= 0,
    fixedImg);
  check("kbviewFixImgs 同时把绝对路径写进 data-kbv-path（点图看原图靠它）",
    fixedImg.indexOf('data-kbv-path="C:\\kb\\mineru\\ABC\\images\\a.jpg"') >= 0,
    fixedImg);
  check("kbviewFixImgs 不动绝对 URL",
    KB.kbviewFixImgs('<img src="http://x/a.jpg">', "ABC")
      === '<img src="http://x/a.jpg">');
  KB.kbDir = () => "";
  check("kbviewFixImgs 没有 kbDir 时原样返回（宁可裂图，不拼坏 URL）",
    KB.kbviewFixImgs('<img src="images/a.jpg">', "ABC")
      === '<img src="images/a.jpg">');
  KB.kbDir = oldKbDir;
  check("md2html 转义 HTML（笔记里有 < & 也不炸）",
    KB.md2html("<b>x</b> & y").indexOf("&lt;b&gt;x&lt;/b&gt; &amp; y") >= 0,
    KB.md2html("<b>x</b> & y"));
  const paged = KB.md2html(["# T", "", "## p.1", "aaa", "", "## p.2", "bbb"].join("\n"));
  check("md2html 把 ## p.N 包成可编辑的页块",
    paged.indexOf('<div class="kb-page" data-kbpage="1">') >= 0
    && paged.indexOf('<div class="kb-page" data-kbpage="2">') >= 0, paged);
  const split = KB.kbviewSplitMd(["## p.1", "线一", "线二", "", "## p.2", "乙"].join("\n"));
  check("kbviewSplitMd 把正文按 ## p.N 切成 markdown",
    split[1] === "线一\n线二" && split[2] === "乙", JSON.stringify(split));
  check("kbviewSplitMd 没分节时返回空对象",
    JSON.stringify(KB.kbviewSplitMd("没有页码锚点")) === "{}");
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
  // 阅读器那边注册的事件**只允许这三种**：
  //   · renderToolbar                     —— 工具栏「对照」按钮（2026-10-11）
  //   · createViewContextMenu             —— 右键页面/公式「知识库：修正公式」
  //   · createAnnotationContextMenu       —— 右键已有标注（同上一项，2026-10-12）
  //     （两个都要：右键公式**不必先画标注**，见 src/22-formula.js 文件头）
  // 旧的"选中入口"（renderTextSelectionPopup 那批）随窗格一起删了，不许被加回来。
  const READER_EVENTS_OK = ["renderToolbar", "createViewContextMenu",
                            "createAnnotationContextMenu"];
  const badReaderEvents = calls.readers
    .filter((r) => READER_EVENTS_OK.indexOf(r.type) < 0)
    .map((r) => r.type);
  check("阅读器只注册 工具栏 + 两个右键菜单（旧的选中入口没被加回来）",
    calls.readers.length >= 1 && badReaderEvents.length === 0,
    JSON.stringify(calls.readers.map((r) => r.type)));
  check("两个右键菜单事件都注册了（右键公式不必先建标注）",
    calls.readers.some((r) => r.type === "createViewContextMenu")
    && calls.readers.some((r) => r.type === "createAnnotationContextMenu"),
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

  // ---- 2026-10-10 修复：/health 的 ollama 是**路径字符串**（面板拿它填输入框），
  //   插件要的结构化状态在新增的 ollama_info 里。修复前 probeLocalModel 直接读
  //   `serverInfo.ollama`（字符串）→ `info.exists` 恒 undefined → 明明连上也写
  //   「没找到 Ollama」。下面四条把新契约钉住。
  KB.serverInfo = {
    ollama: "C:\\x\\ollama.exe",
    ollama_info: { path: "C:\\x\\ollama.exe", exists: true,
                   api_up: true, models: ["qwen3:4b-instruct"] },
  };
  check("优先读 ollama_info：已连上 → true",
    KB.probeLocalModel() === true, KB.caps.localWhy);
  KB.serverInfo = {
    ollama: "C:\\x\\ollama.exe",
    ollama_info: { path: "C:\\x\\ollama.exe", exists: true,
                   api_up: false, models: [] },
  };
  check("ollama_info 说没在跑 → false 且原因具体",
    KB.probeLocalModel() === false
    && KB.caps.localWhy.indexOf("启动 Ollama") >= 0, KB.caps.localWhy);
  KB.serverInfo = { ollama: "C:\\x\\ollama.exe" };   // 老服务：只有路径字符串
  check("只有路径字符串（老服务）→ null 未知，不再误报「没找到 Ollama」",
    KB.probeLocalModel() === null
    && KB.caps.localWhy.indexOf("拿不到运行状态") >= 0, KB.caps.localWhy);
  KB.serverInfo = { ollama: { exists: true, api_up: true,
                             models: ["qwen3:4b-instruct"] } };
  check("兼容「ollama 直接给结构」的服务端 → true",
    KB.probeLocalModel() === true);
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
    && typeof KB.openOptionalGuide === "function");
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
    // W4（2026-10-14）：`panel` 留着当"有没有起过面板进程"的哨兵（现在应当**永远是空**）；
    //   新增 `notices` 收集通知文案 —— 退役后的正确行为是"指路新面板"，要能断言到。
    const seen = { pref: [], panel: [], notices: [], asked: 0, missing: null };
    // ⚠ 这里原来还存了 `setPref: KB.setPref` 并在下面**伪造**了它 ——
    //   而真机上 `self.setPref` 根本不存在（写 pref 用 Zotero.Prefs.set）。
    //   桩凭想象提供了一个函数，于是 19-mineruguide.js 里那处
    //   `self.setPref(...)` 静默失败一直没被发现（2026-10-05 修的）。
    //   现在监听真的 `Zotero.Prefs.set`，桩与真机口径一致。
    const saved = {
      _exists: KB._exists, request: KB.request,
      getPref: KB.getPref, askOptionalGuide: KB.askOptionalGuide,
      notify: KB.notify, prefsSet: Zotero.Prefs.set,
    };
    KB.getPref = (k, d) => {
      if (k === KB.PREFS.optionalGuideDone) return !!opts.done;
      if (k === KB.PREFS.mineruGuideDone) return !!opts.legacyDone;
      return d;
    };
    Zotero.Prefs.set = (k, v) => { seen.pref.push([k, v]); calls.prefs[k] = v; };
    KB._exists = () => !!opts.localExe;
    KB.request = async (m, path) => (path === "/mineru-check"
      ? { ok: !!opts.mineruOk } : { ollama: opts.ollamaOk ? "C:\\x\\ollama.exe" : "" });
    KB.askOptionalGuide = (missing) => {
      seen.asked++; seen.missing = missing; return opts.choice;
    };
    // 哨兵：现在**没有任何代码**会去起面板进程；如果哪天又有人加回来，seen.panel 会非空。
    KB.notify = (title, body) => { seen.notices.push(String(title) + "|" + String(body)); };
    try {
      await KB.maybeAskOptionalGuides();
    } finally {
      KB._exists = saved._exists;
      KB.request = saved.request;
      Zotero.Prefs.set = saved.prefsSet;
      KB.getPref = saved.getPref;
      KB.askOptionalGuide = saved.askOptionalGuide;
      KB.notify = saved.notify;
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
    // 2026-10-16：图形化安装向导随 Tk 面板退役后，「看安装方法」**不再拉任何进程、
    // 也不再指向面板里那个不存在的安装器** —— 改成直接给两条真能照做的路
    // （MinerU 走 scripts\install-mineru.cmd；Ollama 走官网安装器）。
    // 断言新意图：① 仍然弹一次并记 pref（用户不会被反复问）② 先缺谁就先说谁
    // ③ 通知里给的是**真的安装路径**，不是"去面板点安装引导"。
    check("两个都没装 + 选「看安装方法」→ 给出真安装路径（不再指面板向导），先缺谁开谁",
      s.asked === 1 && s.pref.length === 1 && s.pref[0][1] === true
      && s.panel.length === 0
      && JSON.stringify(s.missing) === JSON.stringify(["mineru", "ollama"])
      && s.notices.some((n) => n.indexOf("install-mineru.cmd") >= 0),
      JSON.stringify(s));

    s = await guideCase({ mineruOk: true, choice: 0 });
    check("只有 Ollama 缺 → 只列 Ollama，指的是官网安装器",
      JSON.stringify(s.missing) === JSON.stringify(["ollama"])
      && s.panel.length === 0
      && s.notices.some((n) => n.indexOf("ollama.com") >= 0),
      JSON.stringify(s));

    // ---- ⑥c 的两条 async 断言（放在这里结算，print 前）
    const p0 = await payloadNoItem;
    check("没有任何条目时给出空状态 + 提示「先选中一篇」",
      !!p0 && p0.empty === true && p0.emptyWhy.indexOf("选中一篇") >= 0,
      JSON.stringify(p0));
    const p1 = await payloadNoFiles;
    check("这一篇没有任何分级文件时给空状态 + 诊断行（不抛、不空白）",
      !!p1 && p1.empty === true && p1.key === "ABCD1234"
      && p1.emptyWhy.indexOf("分级文件") >= 0 && typeof p1.probe === "string",
      JSON.stringify(p1 && { empty: p1.empty, why: p1.emptyWhy }));
    check("payload 带上了级别清单（下拉框靠它）",
      !!p1 && Array.isArray(p1.levels) && p1.levels.length >= 4
      && typeof p1.font === "number",
      JSON.stringify(p1 && p1.levels));

    KB.projectRoot = savedRootFn;
  // ============ W4（2026-10-14）：Tk 面板退役的回归闸门 ============
// 原意图是"面板入口必须在"；现在面板换成 Zotero 内的新面板，所以把断言改成
// **"入口仍指向管理面板" + "不再有任何指向 Tk 的路径"**（改断言，不是删断言）。
{
  console.log("\n[W4] Tk 面板退役");
  const src = fs.readFileSync(path.join(PLUGIN, "src", "14-menus.js"), "utf8");
  const openSrc = fs.readFileSync(path.join(PLUGIN, "src", "13-kbopen.js"), "utf8");
  const minersrc = fs.readFileSync(path.join(PLUGIN, "src", "19-mineruguide.js"), "utf8");
  check("openPanel() 改成打开**新面板**（kbAdminOpen），不再拉 Tk",
    /openPanel:\s*function[\s\S]{0,600}?kbAdminOpen/.test(src) && !/tools\\\\gui\.py/.test(src),
    "openPanel 里应调 kbAdminOpen、且不再出现 tools\\gui.py");
  check("13-kbopen.js 不再有 panelProcess / openKbViaPanel（Tk 兜底链已删）",
    !/panelProcess|openKbViaPanel/.test(openSrc), "");
  check("19-mineruguide.js 不再调 panelProcess，也不再指向面板里的安装向导",
    !/panelProcess/.test(minersrc)
    && /install-mineru\.cmd/.test(minersrc)
    && /ollama\.com/.test(minersrc)
    && !/面板里那个安装引导/.test(minersrc)
    && !/安装引导在管理面板里/.test(minersrc), "");
  check("工具栏按钮与条目右键仍指向 openPanel()（入口没删，只是换实现）",
    /registerToolbarButton/.test(src) && /self\.openPanel\(\)/.test(src), "");
  // 磁盘断言：退役件必须没了；保留件必须还在（4-service.vbs 是线3 的回退路径）
  const root = path.join(PLUGIN, "..");
  check("tools/gui.py 已删除（W4）", !fs.existsSync(path.join(root, "tools", "gui.py")));
  check("tools/panels 目录已删除（W4）", !fs.existsSync(path.join(root, "tools", "panels")));
  check("scripts/0-panel.vbs 已删除（W4）",
    !fs.existsSync(path.join(root, "scripts", "0-panel.vbs")));
  // ⚠ W4 真机踩到的回归：projectRoot() 原来靠"tools\gui.py 在不在"探测项目根，
  //   删掉 gui.py 后四个探测档全落空 → 引擎起不来（真机现象：`找不到引擎或 Python`）。
  //   这条把它钉住：判据必须是**仍然存在**的标记文件。
  const p2 = fs.readFileSync(path.join(PLUGIN, "src", "02-paths.js"), "utf8");
  check("projectRoot() 不再用 tools\\gui.py 当判据（W4 回归闸门）",
    // ① 有新的"项目根标记"函数；② 里面用 online/engine.py；
    // ③ **代码里不再有** `_exists(… + "…gui.py")` 这种判据（注释里提历史没关系）。
    /_looksLikeProjectRoot/.test(p2)
    && /online[\\\/]engine\.py/.test(p2)
    && !/_exists\([^)]*gui\.py/.test(p2));
  check("localserver 的项目根标记也换成 online\\engine.py（键名 panel/has_panel 保留）",
    (function () {
      const ls = fs.readFileSync(path.join(root, "online", "localserver.py"), "utf8");
      return /os\.path\.join\(root, "online", "engine\.py"\)/.test(ls)
             && !/os\.path\.join\(root, "tools", "gui\.py"\)/.test(ls);
    })(), "");
  check("scripts/4-service.vbs **仍在**（线3 的回退路径，不许删）",
    fs.existsSync(path.join(root, "scripts", "4-service.vbs")));
}


  console.log("\n通过 " + pass + "，失败 " + fail);
    
process.exit(fail ? 1 : 0);
  })();
}
