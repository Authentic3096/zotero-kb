/**
 * 20-kbview.js —— 条目右侧栏的「知识库」：把 kb/ 里的分级文件在一列里显示出来。
 *
 * ## 2026-10-11 改版（本文件的大变化，看这里就够了）
 *
 * 用户看了 `leike0813/zotero-agents` 的 ACP Chat 之后拍板"仿照他的做"：
 * **我们这一列要能独占条目窗格，而不是像 registerSection 那样挤在分区列最下面**
 * （Zotero 的 itemPane 是一列纵向堆叠，rail 只做 scrollToPane / pin，做不到独占）。
 *
 * 于是：
 *   · **不再用 `ItemPaneManager.registerSection`** —— 自己往
 *     `#zotero-item-pane` 里插一列，并把原生的分区列（`#zotero-item-pane-content`）
 *     暂时 `hidden` 起来，视觉上独占；关闭时逐项还原（先存再改，见 kbviewSaveNative）。
 *   · **列里放的是 XUL `<browser>`**，指向插件自带的 `content/kbview.html`。
 *     正文在那份**普通 HTML 文档**里渲染，终于能正常写 CSS
 *     （overflow-wrap:anywhere / max-width:100% / MathML / 宽表格滚动）。
 *     老实现往 XUL 文档里塞 innerHTML，按 **XML** 解析 —— 一个没闭合的 `<br>`
 *     就让整段内容被静默丢弃（2026-10-05 实测："读了 34414 字、正文一片空白"）。
 *   · 宿主与页面之间用 **JSON 字符串**通信（见 kbviewSend / kbviewAttachBridge）：
 *     宿主→页面直接调 `contentWindow.kbvReceive(json)`；页面→宿主调宿主注入的
 *     `__kbvHostPost(json)`。都用字符串，避开跨 principal 传对象那一堆坑。
 *
 * ⚠ **装/卸必须对称**：Zotero 会复用主窗口，漏 `remove()` 就会出现两份。
 * ⚠ **藏原生列之前先存原状态**（hidden / collapsed / width / splitter state），
 *   关闭时逐项还原 —— 别把用户的分区列弄没了。
 * ⚠ 这三个 id 都是**内部 id**，换 Zotero 版本可能变 → 一律"取不到就降级"：
 *   退回老的 `registerSection` 模式（只读）并如实写状态文件，别让面板整个消失。
 *
 * 数据分工：**宿主取数据**（IOUtils / kbDir / kbLevels / doc-edit 都在这儿），
 * **页面只负责渲染与交互**。页面里不重复实现权限逻辑。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成 bootstrap.js。
 */
Object.assign(ZoteroKB, {

  KBVIEW_ID: "zotero-kb-kbview",
  kbviewPaneID: null,        // 只读降级模式（registerSection）返回的 id
  kbviewHosts: null,         // Map<window, host>，见 kbviewMap

  /**
   * 我们自己的 `resource://` 包名。
   *
   * ⚠ 这一条是**真机实测逼出来的**（2026-10-11，Zotero 10.0.6 / Firefox 140）：
   *   列里那个 `<browser>` 如果直接加载 `rootURI + "content/kbview.html"`
   *   （rootURI 是 `jar:file:///…xpi!/`），会出现两难：
   *     · 带 `maychangeremoteness="true"`（参考实现这么写）→ 浏览器变成**远程进程**，
   *       页面能加载，但 `browser.contentWindow` 是 **null** —— 宿主根本碰不到页面；
   *     · 去掉那个属性 → 留在父进程、`contentWindow` 有了，**但 jar: 地址压根不导航**
   *       （实测 `currentURI` 永远 about:blank）。
   *   Zotero 自己的 reader 是这么干的（`xpcom/reader.js:1992`）：
   *     `createXULElement('browser')` + `type="content"` + src = **`resource://zotero/`**，
   *     然后 `this._iframe.contentWindow` 直接用。也就是说**resource:// 地址才会在
   *     父进程里加载**。所以插件启动时注册一个自己的 resource 包（插件本来就有权
   *     注册包），页面从 `resource://zotero-kb/…` 走。
   */
  KB_RES_KEY: "zotero-kb",

  /**
   * 知识库目录的 resource 包名（**图片走它**）。
   *
   * ⚠ 为什么不用 `file:///`（老做法）：真机实测，`resource://` 的页面加载
   *   `file://` 图片会被**安全策略拦掉** —— `naturalWidth` 永远是 0、
   *   `complete` 永远 false（"图片全都加载不出来"）。改成再注册一个
   *   `resource://zotero-kb-files/` → <知识库目录>，图片全都能加载
   *   （实测 418×293 / complete=true）。
   */
  KB_FILES_RES: "zotero-kb-files",
  _kbvFilesRoot: "",
  _kbvFilesOK: false,

  /** 知识库目录的 resource 前缀（懒注册；kbDir 变了会重注册）。 */
  kbviewFilesURI: function () {
    var self = ZoteroKB;
    try {
      const kb = String(self.kbDir() || "").replace(/[\\/]+$/, "");
      if (!kb) return "";
      if (self._kbvFilesRoot === kb && self._kbvFilesOK) {
        return "resource://" + self.KB_FILES_RES + "/";
      }
      const res = Services.io.getProtocolHandler("resource")
        .QueryInterface(Components.interfaces.nsIResProtocolHandler);
      res.setSubstitution(self.KB_FILES_RES,
                          Services.io.newFileURI(Zotero.File.pathToFile(kb)));
      self._kbvFilesRoot = kb;
      self._kbvFilesOK = true;
      return "resource://" + self.KB_FILES_RES + "/";
    } catch (e) {
      self._kbvFilesOK = false;
      Zotero.debug("[zotero-kb] 注册知识库 resource 包失败：" + e);
      return "";
    }
  },

  /** 注册 `resource://zotero-kb/` → 插件根目录（startup 时调；失败要能看见）。 */
  registerKbResource: function () {
    var self = ZoteroKB;
    try {
      const res = Services.io.getProtocolHandler("resource")
        .QueryInterface(Components.interfaces.nsIResProtocolHandler);
      res.setSubstitution(self.KB_RES_KEY, Services.io.newURI(self.rootURI, null, null));
      self.writeStatusFile({ kbViewResource: "ok: resource://" + self.KB_RES_KEY + "/" });
      return true;
    } catch (e) {
      self.writeStatusFile({ kbViewResource: "FAIL: " + e });
      Zotero.debug("[zotero-kb] 注册 resource 包失败：" + e);
      return false;
    }
  },

  /** 注销（shutdown 时调）—— 留着会让下次启用指向旧包。 */
  unregisterKbResource: function () {
    var self = ZoteroKB;
    try {
      const res = Services.io.getProtocolHandler("resource")
        .QueryInterface(Components.interfaces.nsIResProtocolHandler);
      res.setSubstitution(self.KB_RES_KEY, null);
      res.setSubstitution(self.KB_FILES_RES, null);
      self._kbvFilesRoot = "";
      self._kbvFilesOK = false;
    } catch (e) { /* ignore */ }
  },

  /**
   * XHTML 命名空间。
   *
   * ⚠ 这个常量是**修 bug 用的**（2026-10-05 实测）：Zotero 主窗口是 XUL 文档，
   *   `doc.createElement("div")` 建出来的元素在 **XUL 命名空间**里 —— 给它设
   *   `innerHTML` 时按 **XML** 解析：片段里只要有一个没闭合的标签（比如 `<br>`），
   *   **整段内容会被丢掉**，而且不报错（用户看到的正是"读了 34414 字、正文一片空白"）。
   * 现在自建列走 `<browser>` + 普通 HTML 文档（那条路不存在这个坑），
   * 这个常量与 kbviewSetHtml 只留给**降级模式**用。
   */
  KB_XHTML: "http://www.w3.org/1999/xhtml",

  /** 建一个 XHTML 命名空间的元素（降级模式里装 HTML 的容器要用它）。 */
  kbviewEl: function (doc, tag) {
    return doc.createElementNS(ZoteroKB.KB_XHTML, tag);
  },

  /**
   * 把一段 HTML **片段**灌进容器（对两种解析器都稳）。**降级模式专用。**
   * ⚠ 绝不能写成 `while (root.firstChild) { el.appendChild(doc.importNode(
   *   root.firstChild, true)) }` —— importNode 返回**副本**、不移除原节点，
   *   于是 firstChild 永远为真 → 死循环 → 内存吃爆 → Zotero 卡死崩溃（实测过）。
   *   一次 import 整棵 wrapper 最简也最不可能出错。
   */
  kbviewSetHtml: function (doc, el, text) {
    try {
      if (typeof DOMParser === "function") {
        const parsed = new DOMParser().parseFromString(
          "<div id='kbroot'>" + String(text == null ? "" : text) + "</div>",
          "text/html");
        const root = parsed.getElementById("kbroot");
        if (root) {
          el.textContent = "";
          el.appendChild(doc.importNode(root, true));
          return true;
        }
      }
    } catch (e) {
      try { el.textContent = "（渲染出错：" + e + "）"; } catch (e2) { /* ignore */ }
      return false;
    }
    try {
      el.innerHTML = String(text == null ? "" : text);
      return true;
    } catch (e) {
      try { el.textContent = "（渲染出错：" + e + "）"; } catch (e2) { /* ignore */ }
      return false;
    }
  },


  // ================================================================ 基础设置

  /** 界面文案（按钮 tooltip）：优先走 ftl，取不到就用中文兜底。 */
  kbviewLabel: function () {
    try {
      const msg = Zotero.ftl && Zotero.ftl.formatValueSync
        && Zotero.ftl.formatValueSync("zotero-kb-kbview-title");
      if (msg && String(msg).trim()) return String(msg);
    } catch (e) { /* 落回中文 */ }
    return "知识库预览";
  },

  /** 只有普通条目（有 pdf/元数据的那些）才显示这一列。 */
  kbviewEnabled: function (item, tabType) {
    if (!item) return false;
    if (tabType === "reader-unloaded") return false;
    try {
      return typeof item.isRegularItem === "function" ? !!item.isRegularItem() : true;
    } catch (e) {
      return false;
    }
  },

  /** 用户选过的级别（pref），默认「分节纲要」—— 它就是给"对着原文读"用的那一层。 */
  kbviewLevel: function () {
    var self = ZoteroKB;
    try {
      const v = String(self.getPref(self.PREFS.kbviewLevel, "") || "").trim();
      if (v) return v;
    } catch (e) { /* ignore */ }
    return "outline";
  },

  /** 字号（px，只作用于这一列）。12~20，默认 13。 */
  kbviewFont: function () {
    var self = ZoteroKB;
    try {
      const v = parseInt(self.getPref(self.PREFS.kbviewFont, 13), 10);
      if (v >= 10 && v <= 24) return v;
    } catch (e) { /* ignore */ }
    return 13;
  },

  setKbviewFont: function (px) {
    var self = ZoteroKB;
    try {
      const v = parseInt(px, 10);
      // ⚠ 写 pref 用 Zotero.Prefs.set：插件里**没有** setPref 这个包装
      if (v >= 10 && v <= 24) Zotero.Prefs.set(self.PREFS.kbviewFont, v);
    } catch (e) { /* ignore */ }
  },

  setKbviewLevel: function (id) {
    var self = ZoteroKB;
    try { Zotero.Prefs.set(self.PREFS.kbviewLevel, String(id || "")); }
    catch (e) { /* ignore */ }
  },


  // ⚠ 这里原来还有一套「窄 / 标准 / 宽」档位（kbviewWidth / setKbviewWidth /
  //   kbviewWidthPx），实现是**改原生日志窗格的 width**。用户 2026-10-11 明确不要：
  //   "覆盖在原侧边栏上，同宽窄能拉动，我也想要一样的" ——
  //   那就是说**不要碰窗格宽度**：我们只是借这条原生窗格显示，宽度归原生的
  //   splitter（#zotero-items-splitter / #zotero-context-splitter）管，
  //   用户拖它，我们的列（flex:1）跟着变。所以那套档位整段删掉了，别再加回来。
  // ================================================================ 分级与文件

  /** 某个级别的 md 在磁盘上的绝对路径（与 Python 侧 kbviews.level_path 同一套拼接）。 */
  kbviewPath: function (levelId, key) {
    const lv = (ZoteroKB.kbLevels(key) || []).find((x) => x.id === levelId);
    return lv ? lv.path : "";
  },

  /**
   * 决定"这次读哪一级"：先看用户选的那一级，文件不在就依次退到
   * 分节纲要 → 摘要与要点 → 完整档案，都读不到就返回 `{levelId:""}`。
   * `existsFn(id) => bool` 由调用方注入 —— 逻辑是纯的，所以能单测。
   */
  kbviewOrder: function (prefer) {
    const order = [];
    for (const id of [prefer, "outline", "tldr", "card"]) {
      if (id && order.indexOf(id) < 0) order.push(id);
    }
    return order;
  },

  kbviewPickLevel: function (prefer, existsFn) {
    const order = ZoteroKB.kbviewOrder(prefer);
    for (const id of order) {
      let ok = false;
      try { ok = !!existsFn(id); } catch (e) { ok = false; }
      if (ok) return { levelId: id, fellBack: id !== prefer };
    }
    return { levelId: "", fellBack: false };
  },


  // ================================================================ 自建列：宿主状态

  /**
   * 每个主窗口一份宿主状态。`Map<window, host>`：
   *   host = { win, lib, read, timer, timerArmed, lastKey, onMessage }
   *   dock = { kind, roots, button, container, browser, saved, open, ready }
   * 用 Map 而不是挂在 window 上：卸载时能枚举干净，也不往 Zotero 的对象上加属性。
   */
  kbviewMap: function () {
    var self = ZoteroKB;
    if (!self.kbviewHosts) {
      try { self.kbviewHosts = new Map(); } catch (e) { self.kbviewHosts = null; }
    }
    return self.kbviewHosts;
  },

  kbviewHost: function (win, create) {
    var self = ZoteroKB;
    const m = self.kbviewMap();
    if (!m || !win) return null;
    let h = m.get(win) || null;
    if (!h && create) {
      h = { win: win, lib: null, read: null, timer: null, timerArmed: false,
            lastKey: "", lastPage: 0, onMessage: null };
      m.set(win, h);
    }
    return h;
  },

  /** 一列的 DOM 落点。取不到（Zotero 内部结构变了）返回 null → 上层降级。 */
  kbviewRoots: function (win, kind) {
    const doc = win && win.document;
    if (!doc) return null;
    const byId = (id) => { try { return doc.getElementById(id); } catch (e) { return null; } };
    if (kind === "reader") {
      const cp = byId("zotero-context-pane");
      const inner = byId("zotero-context-pane-inner");
      const sn = byId("zotero-context-pane-sidenav");
      if (!cp || !inner || !sn) return null;
      const parent = inner.parentElement;
      if (!parent) return null;
      return { parent: parent, anchor: inner, sidenav: sn, deck: inner, pane: cp };
    }
    const pane = byId("zotero-item-pane");
    const deck = byId("zotero-item-pane-content");
    const sidenav = byId("zotero-view-item-sidenav");
    if (!pane || !deck || !sidenav) return null;
    // 参考实现是 insertBefore(container, sidenav) —— 那只在"sidenav 是 pane 的直接
    // 子元素"时成立。这里先验证这条前提，不成立就降级（宁可不装，也不乱插）。
    if (sidenav.parentElement !== pane) return null;
    return { parent: pane, anchor: sidenav, sidenav: sidenav, deck: deck, pane: pane };
  },

  /** 列里那个页面的地址（走我们自己注册的 resource 包，理由见 KB_RES_KEY）。 */
  kbviewPageURL: function () {
    var self = ZoteroKB;
    const base = "resource://" + self.KB_RES_KEY + "/";
    return base + "content/kbview.html";
  },


  // ================================================================ 自建列：装 / 卸

  /** 装一个窗口里的一列（幂等）。装不上返回 false，由 kbviewMount 决定是否降级。 */
  kbviewMountPane: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, true);
    if (!host) return false;
    const slot = kind === "reader" ? "read" : "lib";
    if (host[slot]) return true;
    const roots = self.kbviewRoots(win, kind);
    if (!roots) return false;
    const url = self.kbviewPageURL();
    if (!url) return false;
    const doc = win.document;
    const btn = self.kbviewBuildButton(doc, win, kind);
    const box = self.kbviewBuildContainer(doc, win, kind);
    const br = self.kbviewBuildBrowser(doc, win, kind, url);
    box.appendChild(br);
    try {
      roots.sidenav.appendChild(btn);                 // 天然在最下面
      roots.parent.insertBefore(box, roots.anchor);    // 排在原生分区列之前
    } catch (e) {
      // 插一半失败：把已经插进去的清掉，别留残骸
      try { btn.remove(); } catch (e2) { /* ignore */ }
      try { box.remove(); } catch (e3) { /* ignore */ }
      throw e;
    }
    host[slot] = { kind: kind, win: win, roots: roots, button: btn, container: box,
                   browser: br, saved: null, open: false, ready: false };
    self.writeStatusFile({ kbViewColumn: "ok: 自建列已装（" + kind + "）" });
    Zotero.debug("[zotero-kb] 自建列已装：" + kind);
    return true;
  },

  /** 装一个窗口的全部 UI（主入口；onMainWindowLoad 与 startup 各调一次）。 */
  kbviewMount: function (win) {
    var self = ZoteroKB;
    if (!win || !win.document) return false;
    const host = self.kbviewHost(win, true);
    if (!host) return false;
    let ok = false;
    try { ok = self.kbviewMountPane(win, "library"); }
    catch (e) { Zotero.debug("[zotero-kb] 装 library 列失败：" + e); ok = false; }
    if (!ok) {
      // 取不到落点 → 退回 registerSection（只读），并如实写状态文件。
      self.kbviewDegrade("找不到 #zotero-item-pane / #zotero-view-item-sidenav / "
                         + "#zotero-item-pane-content（Zotero 内部结构可能变了）");
      return false;
    }
    // 阅读器那一列（#zotero-context-pane）是**尽力而为**：装不上不影响主功能，
    // 也**不**因此降级（否则会出现"rail 上一个入口、分区列里还多一块"）。
    try {
      self.kbviewMountPane(win, "reader");
    } catch (e2) {
      Zotero.debug("[zotero-kb] 阅读器那列没装成（不影响条目库）：" + e2);
    }
    if (!host.onMessage) self.kbviewOnWindowMessage(win);
    // 阅读器侧的两个全局东西（幂等；放在这里 = 有主窗口就一定装上）：
    //   · Notifier 的 pageChange —— 翻页时立刻把右边滚到同一页（见 kbviewOnNotify）
    //   · 阅读器工具栏的「对照」按钮（见 kbviewRegisterReaderToolbar）
    try { self.kbviewRegisterNotifier(); }
    catch (e3) { Zotero.debug("[zotero-kb] 注册侧栏 Notifier 失败：" + e3); }
    try { self.kbviewRegisterReaderToolbar(); }
    catch (e4) { Zotero.debug("[zotero-kb] 注册阅读器工具栏按钮失败：" + e4); }
    // 阅读器右键菜单的「知识库：修正公式」（22-formula.js）。同样幂等，
    // 失败只写状态文件，不影响别的功能。
    try { self.kbFormulaInit(); }
    catch (e4b) { Zotero.debug("[zotero-kb] 注册阅读器右键菜单失败：" + e4b); }
    // ⚠ 会话恢复出来的阅读器**早于**我们注册 renderToolbar，收不到那个一次性
    //   事件 —— 所以注册完再"补注入"一次（见 kbviewInjectToolbarButton）。
    try { self.kbviewLateDispatch(function () { self.kbviewEnsureReaderToolbar(); }, 1500); }
    catch (e5) { /* ignore */ }
    // 那份页面**不在启动期加载**（实测 863ms，见 kbviewEnsurePage 的说明）：
    // 等窗口起来、空闲下来再挂。用户要是此刻就点开，kbviewOpen 里会立刻挂上。
    try {
      self.kbviewLateDispatch(function () { self.kbviewEnsurePage(win, "library"); }, 2500);
      self.kbviewLateDispatch(function () { self.kbviewEnsurePage(win, "reader"); }, 3500);
    } catch (e6) { /* ignore */ }
    return true;
  },

  /** 卸一个窗口的全部 UI（对称：按钮、列、监听、定时器都要清）。 */
  kbviewUnmount: function (win) {
    var self = ZoteroKB;
    if (!win) return;
    const host = self.kbviewHost(win, false);
    self.kbviewStopTimer(win);
    if (host) {
      const slots = ["lib", "read"];
      for (const s of slots) {
        const dock = host[s];
        if (!dock) continue;
        try { if (dock.open) self.kbviewRestoreNative(win, dock); }
        catch (e) { Zotero.debug("[zotero-kb] 还原原生列失败：" + e); }
        try { if (dock.button && dock.button.remove) dock.button.remove(); } catch (e2) { /* ignore */ }
        try { if (dock.container && dock.container.remove) dock.container.remove(); } catch (e3) { /* ignore */ }
        dock.open = false;
        host[s] = null;
      }
      if (host.onMessage && win.removeEventListener) {
        try { win.removeEventListener("message", host.onMessage, true); } catch (e4) { /* ignore */ }
      }
      host.onMessage = null;
    }
    const m = self.kbviewMap();
    if (m) { try { m.delete(win); } catch (e5) { /* ignore */ } }
    Zotero.debug("[zotero-kb] 自建列已卸");
  },

  /** 卸载插件时把所有窗口的列都清掉。 */
  kbviewUnmountAll: function () {
    var self = ZoteroKB;
    const m = self.kbviewMap();
    if (!m) return;
    const wins = [];
    try { m.forEach(function (v, k) { wins.push(k); }); } catch (e) { return; }
    for (const w of wins) {
      try { self.kbviewUnmount(w); } catch (e2) { /* ignore */ }
    }
    try { m.clear(); } catch (e3) { /* ignore */ }
  },


  // ================================================================ 自建列：DOM 构造

  /** rail 上那个按钮：class="btn" 让它继承 Zotero 自己的 rail 样式（28×28 / 悬停）。 */
  kbviewBuildButton: function (doc, win, kind) {
    var self = ZoteroKB;
    let btn = null;
    try { btn = doc.createXULElement ? doc.createXULElement("toolbarbutton") : null; }
    catch (e) { btn = null; }
    if (!btn) btn = doc.createElement("toolbarbutton");
    btn.setAttribute("id", "zotero-kb-kbview-entry-" + kind);
    // ⚠ 绝不加 data-pane：ItemPaneSidenav#render() 会遍历 .btn[data-pane] 并按
    //   "已注册的分区"隐藏/显示 —— 我们的按钮没注册过，会被它连父元素一起 hidden
    //   （父元素就是 rail 本身，等于把整条 rail 藏掉）。
    btn.setAttribute("class", "btn zotero-kb-kbview-entry");
    btn.setAttribute("custom", "true");
    btn.setAttribute("tooltiptext", self.kbviewLabel());
    btn.setAttribute("aria-label", self.kbviewLabel());
    btn.setAttribute("aria-pressed", "false");
    btn.setAttribute("tabindex", "0");
    btn.setAttribute("role", "button");
    const icon = self.rootURI == null ? "" : String(self.rootURI) + "sidenav-icon.svg";
    try {
      // 图标用 context-fill（跟随主题）；rail 的 .btn 已经设了
      // background-repeat/position 与 -moz-context-properties，这里只补图与填充色。
      if (icon) btn.style.setProperty("background-image", 'url("' + icon + '")');
      btn.style.setProperty("-moz-context-properties", "fill,stroke");
      btn.style.setProperty("fill", "currentColor");
    } catch (e2) { /* 图标是装饰，失败不影响功能 */ }
    btn.addEventListener("command", function () {
      try { ZoteroKB.kbviewToggle(win, kind); }
      catch (e3) { Zotero.debug("[zotero-kb] 切换知识库侧栏失败：" + e3); }
    });
    return btn;
  },

  /** 我们那一列的容器（默认藏起来，打开时才 display:flex）。 */
  kbviewBuildContainer: function (doc, win, kind) {
    let box = null;
    try { box = doc.createXULElement ? doc.createXULElement("vbox") : null; }
    catch (e) { box = null; }
    if (!box) box = doc.createElement("vbox");
    box.setAttribute("id", "zotero-kb-kbview-column-" + kind);
    box.setAttribute("class", "zotero-kb-kbview-column");
    box.setAttribute("flex", "1");
    box.setAttribute("style",
      "display:none; flex:1 1 auto; min-width:0; min-height:0; height:100%;"
      + " overflow:hidden; flex-direction:column; background:transparent;");
    return box;
  },

  /**
   * 列里的内容区：XUL `<browser type="content">`（参考实现也是这么做的）。
   * 为什么不用 `<iframe>`：browser 是 Zotero 里加载自带页面的既定做法
   * （Zotero 自己的 reader 就是 `createXULElement('browser')` + resource:// 地址），
   * 真机实测 `<iframe>`（XUL 与 HTML 两种）在这个 XUL 窗口里**根本不导航**
   * （`contentDocument` 永远停在 about:blank）。
   */
  kbviewBuildBrowser: function (doc, win, kind, url) {
    var self = ZoteroKB;
    let el = null;
    try { el = doc.createXULElement ? doc.createXULElement("browser") : null; }
    catch (e) { el = null; }
    if (!el) { try { el = doc.createElement("browser"); } catch (e2) { el = null; } }
    if (!el) return null;
    el.setAttribute("id", "zotero-kb-kbview-frame-" + kind);
    el.setAttribute("class", "zotero-kb-kbview-frame");
    el.setAttribute("disableglobalhistory", "true");
    // ⚠ 这里**故意没有** `maychangeremoteness="true"`（参考实现有）：
    //   真机实测加了它这个 browser 会变成**远程进程**，contentWindow 变 null、
    //   宿主与页面彻底失联。去掉它 + src 走 resource:// 才有 contentWindow。
    //   详见 KB_RES_KEY 那段说明。
    el.setAttribute("type", "content");
    el.setAttribute("flex", "1");
    // ⚠ 2026-10-13：**这里不设 src**，只把它记下来 —— 那份页面实测要 863ms
    //   （kbview.html + markdown-it + katex + 21 个 woff2 字体），而用户报的
    //   "启动时插件加载有点慢"就是这个：建 <browser> 时立刻设 src = 把这 863ms
    //   摊进了 Zotero 的启动期。改由 kbviewEnsurePage() 在**窗口空闲后**再挂，
    //   或者用户真点开时立刻挂（见 kbviewOpen / kbviewMount 里的调度）。
    el.setAttribute("data-kbview-src", url);
    try {
      el.style.setProperty("width", "100%");
      el.style.setProperty("height", "100%");
      el.style.setProperty("flex", "1 1 auto");
      el.style.setProperty("min-height", "0");
      el.style.setProperty("min-width", "0");
      el.style.setProperty("display", "block");
      el.style.setProperty("border", "0");
    } catch (e3) { /* ignore */ }
    el.addEventListener("load", function () {
      // 计时：从**挂 src 那一刻**到页面 load（含 vendor 与字体）。
      // 分段写，任何一段读不到都不影响其它信息落盘（第一版把整段包一个 try，
      // contentWindow 还没就绪时于是一条都不写，白装了仪表）。
      const t0 = el.__kbvSrcAt || Date.now();
      try {
        self.writeStatusFile({ kbviewPageLoadMs: Date.now() - t0 });
      } catch (eTiming) { /* ignore */ }
      try {
        const cw = el.contentWindow;
        const perf = cw && cw.performance;
        if (perf) {
          const nav = perf.getEntriesByType("navigation")[0];
          const rs = perf.getEntriesByType("resource");
          let bytes = 0, resMs = 0;
          for (const r of rs) { bytes += (r.transferSize || 0); resMs += r.duration; }
          self.writeStatusFile({
            kbviewPageDomComplete: nav ? Math.round(nav.domComplete) : null,
            kbviewPageResources: rs.length,
            kbviewPageResourcesMs: Math.round(resMs),
            kbviewPageBytes: bytes,
          });
        }
      } catch (ePerf) { /* 拿不到 performance 就算了，上面那行耗时已经够用 */ }
      try { self.kbviewAttachBridge(win, kind); }
      catch (e4) { Zotero.debug("[zotero-kb] 侧栏页面桥接失败：" + e4); }
    });
    return el;
  },


  // ================================================================ 自建列：开 / 关

  /** 把原生列的原状态**先存下来**（关闭时逐项还原，这是"自插 DOM"的第一大坑）。 */
  kbviewSaveNative: function (win, dock) {
    if (!dock || dock.saved) return;
    const r = dock.roots;
    // ⚠ **不存窗格宽度**：宽度归原生的 splitter（用户自己拖），我们从头到尾
    //   没碰过它 —— 存了再还原会把用户在我们打开期间拖出来的宽度顶回去。
    const saved = {
      deckHidden: null, deckCollapsed: null, paneCollapsed: null,
      sidenavHidden: null, splitterState: null, contextCollapsed: null,
    };
    try {
      if (r.deck) {
        saved.deckHidden = r.deck.getAttribute("hidden");
        saved.deckCollapsed = r.deck.getAttribute("collapsed");
      }
      if (r.pane) saved.paneCollapsed = r.pane.getAttribute("collapsed");
      if (r.sidenav) saved.sidenavHidden = r.sidenav.getAttribute("hidden");
      const sp = win.document.getElementById(dock.kind === "reader"
        ? "zotero-context-splitter" : "zotero-items-splitter");
      if (sp) saved.splitterState = sp.getAttribute("state");
      if (dock.kind === "reader" && win.ZoteroContextPane) {
        saved.contextCollapsed = !!win.ZoteroContextPane.collapsed;
      }
    } catch (e2) { /* 存不下就尽量少动原生 DOM */ }
    dock.saved = saved;
  },

  /** 逐项还原（不是"删掉就算了"）。 */
  kbviewRestoreNative: function (win, dock) {
    if (!dock || !dock.saved) return;
    const s = dock.saved;
    const r = dock.roots;
    const put = function (el, name, val) {
      if (!el) return;
      try {
        if (val == null) el.removeAttribute(name);
        else el.setAttribute(name, val);
      } catch (e) { /* ignore */ }
    };
    put(r.deck, "hidden", s.deckHidden);
    put(r.deck, "collapsed", s.deckCollapsed);
    put(r.pane, "collapsed", s.paneCollapsed);
    put(r.sidenav, "hidden", s.sidenavHidden);
    try {
      const sp = win.document.getElementById(dock.kind === "reader"
        ? "zotero-context-splitter" : "zotero-items-splitter");
      if (sp && s.splitterState != null) sp.setAttribute("state", s.splitterState);
    } catch (e2) { /* ignore */ }
    try {
      if (dock.kind === "reader" && win.ZoteroContextPane
          && s.contextCollapsed != null) {
        win.ZoteroContextPane.collapsed = !!s.contextCollapsed;
      }
    } catch (e3) { /* ignore */ }
    dock.saved = null;
  },

  /** 打开前先把原生窗格撑开（收起状态下 rail 自己也看不见，谈不上点按钮）。 */
  kbviewExpandPane: function (win, dock) {
    const r = dock.roots;
    try {
      if (dock.kind === "reader") {
        if (win.ZoteroContextPane) win.ZoteroContextPane.collapsed = false;
        if (r.pane && r.pane.getAttribute("collapsed") === "true") {
          r.pane.setAttribute("collapsed", "false");
        }
      } else if (r.pane && r.pane.getAttribute("collapsed") === "true") {
        const sp = win.document.getElementById("zotero-items-splitter");
        if (sp) sp.setAttribute("state", "open");
        r.pane.setAttribute("collapsed", "false");
      }
      if (r.sidenav && r.sidenav.getAttribute("hidden") != null) {
        r.sidenav.removeAttribute("hidden");
      }
    } catch (e) { /* ignore */ }
  },

  kbviewIsOpen: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const dock = kind === "reader" ? host.read : host.lib;
    return !!(dock && dock.open);
  },

  /** 读一列里那个 <browser>（没有就 null）。 */
  kbviewBrowserOf: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    const slot = kind === "reader" ? "read" : "lib";
    const dock = host && host[slot];
    return (dock && dock.browser) || null;
  },

  /**
   * 把那一列的页面**挂上（首次调用时才真加载）**。幂等。
   *
   * 为什么要有这一步（2026-10-13，用户报"启动时插件加载有点慢"）：
   * 实测插件自己的同步启动只占 **21ms**，而那份页面（kbview.html +
   * markdown-it 121KB + katex 269KB + 21 个 woff2 字体）要 **863ms** ——
   * 原来在建 <browser> 时就 `setAttribute("src", url)`，于是这 863ms 被摊进了
   * Zotero 的启动期（新建 docshell + 解析 40 万字符 vendor JS 都在抢 CPU/IO）。
   * 现在只把 url 记在 `data-kbview-src` 上，由这里按需挂：
   *   · 窗口起来后空闲 ~2.5s（kbviewMount 里调度）—— 用户还没看的时候后台补上；
   *   · 用户真去点「知识库」时立刻挂（kbviewOpen 里调）—— 不让人干等。
   * 打开流程本来就有 400ms 轮询兜底（kbviewStartTimer），晚挂不影响推送。
   */
  kbviewEnsurePage: function (win, kind) {
    var self = ZoteroKB;
    const br = self.kbviewBrowserOf(win, kind);
    if (!br) return false;
    try {
      if (br.getAttribute("src")) return true;          // 已经挂过
      const url = br.getAttribute("data-kbview-src");
      if (!url) return false;
      br.__kbvSrcAt = Date.now();                       // 给 load 里的计时用
      br.setAttribute("src", url);
      return true;
    } catch (e) {
      Zotero.debug("[zotero-kb] 挂侧栏页面失败：" + e);
      return false;
    }
  },

  kbviewOpen: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, true);
    if (!host) return false;
    // 两列都占地方，**不能同时开**（派发包 §一）。kbviewToggle 已经处理了
    // 点按钮那条路；这里再兜一次，防止别的代码路径直接调 kbviewOpen。
    const other = kind === "reader" ? "library" : "reader";
    if (self.kbviewIsOpen(win, other)) self.kbviewClose(win, other);
    const slot = kind === "reader" ? "read" : "lib";
    if (!host[slot]) {
      let ok = false;
      try { ok = self.kbviewMountPane(win, kind); } catch (e) { ok = false; }
      if (!ok) {
        if (kind === "reader") {
          self.writeStatusFile({ kbViewColumn: "reader 列不可用（不影响条目库那一列）" });
        } else {
          self.kbviewDegrade("装不上条目库那一列");
        }
        return false;
      }
    }
    const dock = host[slot];
    if (!dock) return false;
    if (dock.open) return true;
    self.kbviewSaveNative(win, dock);
    self.kbviewExpandPane(win, dock);
    try { dock.container.style.display = "flex"; } catch (e2) { /* ignore */ }
    // 把原生的分区列让开 —— "独占一页"就是靠这一句
    try { dock.roots.deck.setAttribute("hidden", "true"); } catch (e3) { /* ignore */ }
    try {
      dock.button.setAttribute("aria-pressed", "true");
      dock.button.setAttribute("data-kbview-open", "true");
    } catch (e4) { /* ignore */ }
    dock.open = true;
    host.lastKey = "?!reset";          // 强制下一拍重推内容
    // 用户真点开了：**立刻**把页面挂上（还没挂的话），别让人对着空白干等
    self.kbviewEnsurePage(win, kind);
    self.kbviewStartTimer(win);
    // 立刻试一次挂桥（页面可能已经就绪），失败就交给上面的轮询
    try { self.kbviewAttachBridge(win, kind); } catch (e5) { /* 轮询会再试 */ }
    self.kbviewPush(win, kind, null);
    self.writeStatusFile({ kbViewColumn: "open: " + kind });
    return true;
  },

  kbviewClose: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const slot = kind === "reader" ? "read" : "lib";
    const dock = host[slot];
    if (!dock || !dock.open) return false;
    dock.open = false;
    try { dock.container.style.display = "none"; } catch (e) { /* ignore */ }
    self.kbviewRestoreNative(win, dock);
    try {
      dock.button.setAttribute("aria-pressed", "false");
      dock.button.removeAttribute("data-kbview-open");
    } catch (e2) { /* ignore */ }
    if (!self.kbviewIsOpen(win, "library") && !self.kbviewIsOpen(win, "reader")) {
      self.kbviewStopTimer(win);
    }
    self.writeStatusFile({ kbViewColumn: "closed: " + kind });
    return true;
  },

  /** rail 按钮 / 开关的统一入口：开就关、关就开（同一时刻只开一列）。 */
  kbviewToggle: function (win, kind) {
    var self = ZoteroKB;
    const other = kind === "reader" ? "library" : "reader";
    if (self.kbviewIsOpen(win, kind)) return self.kbviewClose(win, kind);
    if (self.kbviewIsOpen(win, other)) self.kbviewClose(win, other);
    return self.kbviewOpen(win, kind);
  },


  // ================================================================ 自建列：与页面通信

  /**
   * 把回调挂到页面窗口上（页面用 window.__kbvHostPost(jsonText) 回来）。
   *
   * ⚠ 跨 principal 的两个要点：
   *   ① 挂到 **wrappedJSObject** 上，页面才看得见（挂在 Xray wrapper 上是给自己看的）；
   *   ② 只传 **JSON 字符串**，不传对象 —— 传对象要过 Xray，是最容易"权限被拒"的一步。
   */
  kbviewAttachBridge: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const dock = kind === "reader" ? host.read : host.lib;
    if (!dock || !dock.browser) return false;
    if (dock.ready) return true;
    const api = function (jsonText) {
      try { ZoteroKB.kbviewOnHostCall(win, kind, jsonText); }
      catch (e) { Zotero.debug("[zotero-kb] 侧栏回调出错：" + e); }
    };
    let n = 0;
    try {
      const w = dock.browser.contentWindow;
      if (w) {
        try { w.__kbvHostPost = api; n++; } catch (e) { /* Xray wrapper 上挂了没用 */ }
        try {
          if (w.wrappedJSObject) { w.wrappedJSObject.__kbvHostPost = api; n++; }
        } catch (e2) { /* ignore */ }
      }
    } catch (e3) { /* 页面还没就绪 */ }
    dock.ready = n > 0;
    if (dock.open) self.kbviewPush(win, kind, null);
    return n > 0;
  },

  /** 把一条消息送进页面（优先直接调，页面里也挂了 postMessage 兜底）。 */
  kbviewSend: function (dock, msg) {
    if (!dock || !dock.browser) return false;
    const full = Object.assign({ __kbv: true }, msg);
    let text = "";
    try { text = JSON.stringify(full); } catch (e) { return false; }
    const wins = [];
    try {
      const w = dock.browser.contentWindow;
      if (w) {
        // ⚠ **wrappedJSObject 优先**：页面是 resource:// 的特权文档，直接读
        //   `contentWindow.kbvReceive` 会被 Xray 挡住（typeof 一直是 "undefined"），
        //   真身只能从 wrappedJSObject 拿 —— 真机实测（2026-10-11）。
        try { if (w.wrappedJSObject) wins.push(w.wrappedJSObject); } catch (e2) { /* ignore */ }
        wins.push(w);
      }
    } catch (e3) { /* ignore */ }
    for (const w of wins) {
      try {
        if (typeof w.kbvReceive === "function") { w.kbvReceive(text); return true; }
      } catch (e4) { /* 试下一个 */ }
    }
    try {
      if (wins.length && wins[0].postMessage) { wins[0].postMessage(full, "*"); return true; }
    } catch (e5) { /* ignore */ }
    return false;
  },

  /** 顺手也听一下主窗口的 message 事件（页面里那条 postMessage 兜底路能用上）。 */
  kbviewOnWindowMessage: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host || host.onMessage) return;
    host.onMessage = function (event) {
      try {
        const d = event && event.data;
        if (!d || typeof d !== "object" || d.__kbv !== true) return;
        const lib = host.lib;
        const rd = host.read;
        let kind = "";
        if (lib && lib.browser && event.source === lib.browser.contentWindow) kind = "library";
        else if (rd && rd.browser && event.source === rd.browser.contentWindow) kind = "reader";
        if (!kind) return;
        self.kbviewOnHostCall(win, kind, JSON.stringify(d));
      } catch (e) { /* ignore */ }
    };
    try { win.addEventListener("message", host.onMessage, true); }
    catch (e2) { Zotero.debug("[zotero-kb] 挂 message 监听失败：" + e2); }
  },

  /** 页面发回来的一条消息。 */
  kbviewOnHostCall: function (win, kind, jsonText) {
    var self = ZoteroKB;
    let msg = null;
    try { msg = JSON.parse(String(jsonText)); } catch (e) { msg = null; }
    if (!msg || typeof msg.type !== "string") return;
    const t = msg.type;
    if (t === "close") { self.kbviewClose(win, kind); return; }
    if (t === "reload") { self.kbviewPush(win, kind, null); return; }
    if (t === "selectLevel") {
      self.setKbviewLevel(msg.level);
      self.kbviewPush(win, kind, msg.level);
      return;
    }
    if (t === "setFont") {
      self.setKbviewFont(msg.font);
      self.kbviewPush(win, kind, null);
      return;
    }
    if (t === "openPath") {
      try { self.openKbPath(msg.path, "知识库文件"); }
      catch (e) { Zotero.debug("[zotero-kb] 打开文件失败：" + e); }
      return;
    }
    if (t === "log") {
      Zotero.debug("[zotero-kb] 侧栏页面：" + msg.message);
      return;
    }
    if (t === "exportDraft") { self.kbviewExportDraft(win, kind); return; }
    if (t === "savePage") { self.kbviewSavePage(win, kind, msg.page, msg.text); return; }
    // 按块编辑：页面把每块的文本交回来，由**宿主**用它原来那份 parts 拼回整页
    // （拼法是纯函数 kbviewJoinBlocks，桩测试直接测；页面不重复实现一遍）。
    if (t === "saveBlocks") {
      let text = "";
      try { text = self.kbviewJoinBlocks(msg.parts); } catch (e) { text = ""; }
      if (!text) {
        self.kbviewHint(win, kind, "拼不回整页正文（parts 不完整），没有保存。");
        return;
      }
      self.kbviewSavePage(win, kind, msg.page, text);
      return;
    }
    if (t === "openImage") { self.kbviewOpenImage(win, kind, msg.rel); return; }
    if (t === "restorePage") { self.kbviewSavePage(win, kind, msg.page, null); return; }
    // 公式纠错（22-formula.js）：页面只负责界面，请求与写回都在宿主这一侧。
    if (t === "formulaApply") { self.kbFormulaApply(win, kind, msg); return; }
    if (t === "formulaMathType") { self.kbFormulaMathType(win, kind, msg); return; }
  },

  /** 给某个窗口的一列送一句提示（只更新状态行，不重画正文）。 */
  kbviewHint: function (win, kind, text) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    const dock = kind === "reader" ? host.read : host.lib;
    if (dock) self.kbviewSend(dock, { type: "hint", text: String(text == null ? "" : text) });
  },


  // ================================================================ 自建列：内容

  /**
   * 当前该显示哪一篇。
   *
   * 原来这一步是 registerSection 的 onItemChange 回调给的；自建列没有这个回调，
   * 所以自己取：阅读器页签取当前附件 → 用它的**父条目**（知识库按父条目的 key 建），
   * 条目库页签取列表里唯一选中的那一条。
   *
   * ⚠ 取法必须和 Zotero 自己的 item-details 一致（它也是 parentID ? parent : item，
   *   见 elements/contextPane.js 的 _addItemContext），否则会出现"选区在附件上、
   *   我们查的是附件 key，永远没有知识库"。
   */
  kbviewCurrentItem: function (win) {
    if (!win) return null;
    let item = null;
    try {
      const tabs = win.Zotero_Tabs;
      const selType = tabs ? String(tabs.selectedType || "") : "";
      if (selType === "reader" && Zotero.Reader && Zotero.Reader.getByTabID) {
        let reader = null;
        try { reader = Zotero.Reader.getByTabID(tabs.selectedID); } catch (e) { reader = null; }
        const id = reader && reader.itemID;
        if (id) item = Zotero.Items.get(id);
      }
    } catch (e2) { item = null; }
    if (!item) {
      try {
        const pane = win.ZoteroPane;
        const sel = pane && pane.getSelectedItems ? pane.getSelectedItems() : null;
        if (sel && sel.length === 1) item = sel[0];
      } catch (e3) { item = null; }
    }
    if (!item) return null;
    try {
      if (item.parentID) {
        const parent = Zotero.Items.get(item.parentID);
        if (parent) item = parent;
      }
    } catch (e4) { /* 用原条目 */ }
    return item;
  },

  /**
   * 组装给页面的一份内容（宿主把该读的文件都读好，页面只管画）。
   * 返回纯数据（可 JSON 化）。
   *
   * ⚠ 2026-10-11 起**默认不再读 `.html` 孪生**：正文与公式都在页面里用
   *   markdown-it + KaTeX 渲染（配方与理由见 content/kbview.js 文件头）。
   *   只有**降级模式**（退回 ItemPaneManager 分区，跑在主窗口的 XUL 文档里，
   *   innerHTML 注入的 <script> 不执行）才要服务端那份带 MathML 的 HTML ——
   *   那时调用方传 opts.wantHtml。
   */
  kbviewPayloadForItem: async function (item, prefer, opts) {
    var self = ZoteroKB;
    const wantHtml = !!(opts && opts.wantHtml);
    let levels = [];
    try { levels = self.kbLevels() || []; } catch (e) { levels = []; }
    const out = {
      key: "", title: "",
      levels: levels.map(function (lv) { return { id: lv.id, label: lv.label }; }),
      level: self.kbviewLevel(), picked: "", fellBack: false, hint: "",
      md: "", pages: {}, order: [], imgBase: "",
      html: "", textLength: 0, fulltext: false,
      page: (opts && opts.page) || 0,
      edited: [], stale: [],
      empty: false, emptyWhy: "", probe: "",
      font: self.kbviewFont(), busy: false,
    };
    if (!item) {
      out.empty = true;
      out.emptyWhy = "先在左边选中一篇文献（一次只能选一篇）。";
      return out;
    }
    try { out.key = String(item.key || ""); } catch (e) { out.key = ""; }
    try { out.title = String((item.getField && item.getField("title")) || ""); }
    catch (e2) { out.title = ""; }
    if (!self.kbviewEnabled(item, "library")) {
      out.empty = true;
      out.emptyWhy = "这一条不是普通文献条目（附件 / 笔记没有知识库）。";
      return out;
    }
    let list = [];
    try { list = self.kbLevels(out.key) || []; } catch (e3) { list = []; }
    const byId = {};
    for (const lv of list) byId[lv.id] = lv;
    const picked = self.kbviewPickLevel(
      prefer || out.level, function (id) { return !!(byId[id] && byId[id].exists); });
    out.picked = picked.levelId;
    out.fellBack = !!picked.fellBack;
    if (!picked.levelId) {
      out.empty = true;
      out.emptyWhy = "知识库里还没有这一篇的任何分级文件。";
      let kb = "";
      try { kb = String(self.kbDir() || ""); } catch (e4) { kb = ""; }
      out.probe = (kb ? ("知识库目录：" + kb + "；")
                      : "知识库目录未确定（面板「运行环境」→ 服务状态）；")
        + "试过：" + ((byId[out.level] || {}).path || "");
      return out;
    }
    const path = (byId[picked.levelId] || {}).path
      || self.kbviewPath(picked.levelId, out.key);
    // 读 **markdown** 原文（页面自己渲染）。降级模式才另外读一次 .html 孪生。
    let text = "";
    try { text = await IOUtils.readUTF8(path); } catch (e6) { text = ""; }
    out.md = text;
    out.textLength = text.length;
    out.imgBase = self.kbviewImgBase(out.key);
    out.fulltext = picked.levelId === "fulltext";
    if (wantHtml) {
      // 降级模式：页面脚本跑不了（主窗口 XUL 文档里的 innerHTML 不执行 <script>），
      // 只能吃服务端渲染好的 MathML 孪生 —— 那是 mdhtml.py 的产物。
      let html = "";
      try { html = await IOUtils.readUTF8(String(path).replace(/\.md$/i, ".html")); }
      catch (e7) { html = ""; }
      if (!html) html = self.md2html(text);
      out.html = self.kbviewFixImgs(html, out.key);
    }
    const label = (byId[picked.levelId] || {}).label || picked.levelId;
    out.hint = label
      + (picked.fellBack ? "（默认那一级还没生成，已回退）" : "")
      + "　" + text.length + " 字";
    if (out.fulltext) {
      // 逐页正文：先按 "## p.N" 切页，每页再按块切开（空行分段 / 独立公式 /
      // 表格整块 / 图片行单独一块 —— 派发包 §3.2 的口径）。
      // 分块是**纯函数**（kbviewSplitBlocks），桩测试直接测它。
      let byPage = {};
      try { byPage = self.kbviewSplitMd(text); } catch (e8) { byPage = {}; }
      const pages = {};
      const order = [];
      for (const kk of Object.keys(byPage)) {
        const n = parseInt(kk, 10);
        if (!n) continue;
        pages[String(n)] = self.kbviewSplitBlocks(byPage[kk]);
        order.push(n);
      }
      order.sort(function (a, b) { return a - b; });
      out.pages = pages;
      out.order = order;
      out.md = "";                      // 逐页时整份 md 不必再传（省一半体积）
      let meta = {};
      // W3 线2.5（2026-10-14）：桥优先（`doc.get` = 与 HTTP `GET /doc-edit` **同一份**
      // 只读状态），拿不到再退回老 HTTP —— 失败了都一样当空（页面照样渲染）。
      try {
        try {
          meta = await self.kbAdminHostRoute("GET",
            "/doc-edit?key=" + encodeURIComponent(out.key));
        } catch (e0) {
          meta = await self.request("GET", "/doc-edit?key=" + encodeURIComponent(out.key));
        }
      } catch (e9) { meta = {}; }
      out.edited = (meta && meta.edited) || [];
      out.stale = (meta && meta.stale) || [];
    }
    return out;
  },

  /** 取当前条目 → 组装 → 发给页面。 */
  kbviewPush: async function (win, kind, prefer) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const dock = kind === "reader" ? host.read : host.lib;
    if (!dock) return false;
    let payload = null;
    let opts = null;
    if (kind === "reader") {
      // 阅读器那一列要"打开就停在正在读的那一页"
      const info = self.kbviewReaderInfo(win);
      opts = { page: info ? info.page : 0 };
      if (opts.page) host.lastPage = opts.page;
    }
    try { payload = await self.kbviewPayloadForItem(self.kbviewCurrentItem(win), prefer, opts); }
    catch (e) {
      Zotero.debug("[zotero-kb] 组装侧栏内容失败：" + e);
      return false;
    }
    payload.type = "render";
    return self.kbviewSend(dock, payload);
  },

  /** 重新取当前条目 → 组装 → 发给页面（所有打开着的列都发）。 */
  kbviewPushOpen: async function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    if (host.lib && host.lib.open) await self.kbviewPush(win, "library", null);
    if (host.read && host.read.open) await self.kbviewPush(win, "reader", null);
  },

  /** 轮询看"当前条目变了没"（自建列没有 onItemChange 回调，只能自己盯）。 */
  kbviewRefresh: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    const open = (host.lib && host.lib.open) || (host.read && host.read.open);
    if (!open) { self.kbviewStopTimer(win); return; }
    // 桥没挂上就再试一次。
    // ⚠ 为什么不靠 `<browser>` 的 load 事件（kbviewBuildBrowser 里挂了那个监听）：
    //   真机实测（Zotero 10.0.6 / Firefox 140）**XUL `<browser>` 不触发 load 事件**
    //   —— 页面加载得好好的（contentWindow.kbvPageReady === true），但
    //   dock.ready 一直是 false，于是页面上的按钮（切级别 / 收起 / 改这一页）
    //   一个都不管用。改成"打开期间每拍补挂一次"，挂上就停。
    if (host.lib && host.lib.open && !host.lib.ready) {
      try { self.kbviewAttachBridge(win, "library"); } catch (e0) { /* 下一拍再试 */ }
    }
    if (host.read && host.read.open && !host.read.ready) {
      try { self.kbviewAttachBridge(win, "reader"); } catch (e1) { /* 下一拍再试 */ }
    }
    // ⚠ 这里**不再**做「按页跟随」（2026-10-11 用户拍板：跟随没生效也不用要了，
    //   手动翻找并不麻烦）—— 功能与代码一起去掉，见 kbviewFanoutPage 的注释。
    //   原来这里是 400ms 一轮的兜底重推（主路径是 Notifier 的 file/pageChange）。
    //   顺带：那也是一处**周期性重渲染**，是"渲染偶尔抽搐"最可疑的来源。
    if (host.read && host.read.open && !self.kbviewHasReader(win)) {
      // 阅读器标签全关了就把这一列收起来 —— 否则状态上"还开着"，
      // 定时器空转，用户再打开时会觉得"没关干净"（派发包 §五 第 6 条）。
      try { self.kbviewClose(win, "reader"); } catch (e6) { /* ignore */ }
    }
    const item = self.kbviewCurrentItem(win);
    let key = "";
    try { key = item && item.key ? String(item.key) : ""; } catch (e) { key = ""; }
    if (key === host.lastKey) return;
    host.lastKey = key;
    self.kbviewPushOpen(win).catch(function (e2) {
      Zotero.debug("[zotero-kb] 推送侧栏内容失败：" + e2);
    });
  },

  /**
   * 延时调度。
   * ⚠ 这个沙箱里 setInterval / 链式 setTimeout **不可靠**（见 18-taskpoll.js 的
   *   实测），所以统一用 Services.tm 的主线程定时器，setTimeout 只作兜底。
   */
  kbviewLateDispatch: function (fn, ms) {
    try {
      const tm = Services.tm;
      const target = tm.mainThread
        || (tm.currentThread && tm.currentThread.QueryInterface(
              Components.interfaces.nsIThread));
      if (target && tm.newTimer) {
        let qi;
        try { qi = ChromeUtils.generateQI(["nsITimerCallback"]); }
        catch (e) { qi = function () { return this; }; }
        const timer = tm.newTimer({
          observe: function () { try { fn(); } catch (e2) { Zotero.debug("" + e2); } },
          QueryInterface: qi,
        }, ms);
        if (timer) return timer;
      }
    } catch (e3) { /* 落回 setTimeout */ }
    try { return setTimeout(fn, ms); } catch (e4) { return null; }
  },

  kbviewStartTimer: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host || host.timerArmed) return;
    host.timerArmed = true;
    const tick = function () {
      if (!host.timerArmed) return;
      try { self.kbviewRefresh(win); }
      catch (e) { Zotero.debug("[zotero-kb] 侧栏轮询出错：" + e); }
      if (host.timerArmed) host.timer = self.kbviewLateDispatch(tick, 400);
    };
    host.timer = self.kbviewLateDispatch(tick, 400);
  },

  kbviewStopTimer: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    host.timerArmed = false;
    try { if (host.timer && host.timer.cancel) host.timer.cancel(); } catch (e) { /* ignore */ }
    host.timer = null;
  },


  // ================================================================ 自建列：页内动作

  /** 「导出草稿」：把整份有效正文写成 edits/<key>.draft.md（只在「按页正文」显示）。 */
  kbviewExportDraft: async function (win, kind) {
    var self = ZoteroKB;
    const item = self.kbviewCurrentItem(win);
    if (!item) return false;
    self.kbviewHint(win, kind, "正在导出草稿…");
    try {
      // W3 线2.5：桥优先（`doc.save` = HTTP `POST /doc-edit` 同一份实现），失败退回老 HTTP。
      let r = null;
      try {
        r = await self.kbAdminHostRoute("POST", "/doc-edit",
                                        { key: item.key, action: "draft" });
      } catch (e0) {
        Zotero.debug("[zotero-kb] 引擎桥导草稿失败（退回 HTTP）：" + e0);
        r = await self.request("POST", "/doc-edit", { key: item.key, action: "draft" });
      }
      if (r && r.path) {
        self.kbviewHint(win, kind, "草稿已写出：" + r.path);
        try { self.openKbPath(r.path, "正文草稿"); } catch (e) { /* 打不开不影响导出 */ }
        return true;
      }
      self.kbviewHint(win, kind, "导出失败：" + JSON.stringify(r == null ? {} : r).slice(0, 120));
    } catch (e2) {
      self.kbviewHint(win, kind, "导出失败：" + ((e2 && e2.message) ? e2.message : e2));
    }
    return false;
  },

  // ⚠ 2026-10-11 用户反馈后**删掉了 kbviewOpenDoc**（以及页面上的「对照页」按钮）：
  //   阅读器右侧那一列已经能直接对着 PDF 看（同一个窗口、同一份正文），再跳一次
  //   浏览器反而多一步。
  // ⚠ **2026-10-12 用户拍板：浏览器兜底页整组删掉**（派发包《元数据与摘要与分层》§五）——
  //   online/docpage.{py,html} 与 GET /doc、/doc-text、/doc-raw、/doc-page.png、
  //   /kb-image 全部删除；**只保留 POST/GET /doc-edit**（正文修订层，本文件的
  //   kbviewSavePage 与条目窗格列的「改这一页」都靠它）。

  /** 保存 / 恢复一页正文（text == null 表示恢复原版）。 */
  kbviewSavePage: async function (win, kind, page, text) {
    var self = ZoteroKB;
    const item = self.kbviewCurrentItem(win);
    if (!item) return false;
    const body = { key: item.key, page: page };
    if (text == null) body.action = "restore";
    else body.text = String(text);
    const ok = await self.kbviewRunDocEdit(body, function (m) {
      self.kbviewHint(win, kind, m);
    });
    await self.kbviewPush(win, kind, null);
    return ok;
  },


  // ================================================================ 降级：老的「分区」模式（只读）

  /**
   * 退回 `ItemPaneManager.registerSection`。
   *
   * 什么时候走到这里：自建列找不到 `#zotero-item-pane` / `#zotero-view-item-sidenav`
   * / `#zotero-item-pane-content`（换 Zotero 版本、内部 id 变了）。
   * 这一档**只读**：能看分级内容，但没有"改这一页 / 导出草稿 / 独占一页"那些交互
   * —— 总比面板整个消失强，而且状态文件里会如实写下来（"非静默降级"）。
   */
  kbviewDegrade: function (why) {
    var self = ZoteroKB;
    self.writeStatusFile({ kbViewColumn: "degraded: " + why });
    Zotero.debug("[zotero-kb] 自建列不可用，退回分区模式：" + why);
    try { self.registerKbViewSection(); } catch (e) { /* ignore */ }
  },

  /** 注册分区（降级模式）。缺 ItemPaneManager 就如实记状态，不抛。 */
  registerKbViewSection: function () {
    var self = ZoteroKB;
    if (self.kbviewPaneID) return true;
    if (!Zotero.ItemPaneManager || !Zotero.ItemPaneManager.registerSection) {
      self.writeStatusFile({ kbViewPane: "unavailable: ItemPaneManager 不存在" });
      return false;
    }
    self.kbviewPaneID = Zotero.ItemPaneManager.registerSection({
      paneID: self.KBVIEW_ID,
      pluginID: self.id,
      // ⚠ header/sidenav 的 l10nID 必填，且 FTL 里必须是"只有名字"的条目
      header: { l10nID: "zotero-kb-kbview-header",
                icon: self.rootURI + "toolbar-icon.svg" },
      sidenav: { l10nID: "zotero-kb-kbview-sidenav",
                 icon: self.rootURI + "sidenav-icon.svg" },
      onItemChange: ({ item, tabType, setEnabled }) => {
        try { setEnabled(self.kbviewEnabled(item, tabType)); }
        catch (e) { /* ignore */ }
      },
      onRender: ({ doc, body, item }) => self.kbviewRender(doc, body, item),
    });
    self.writeStatusFile({
      kbViewPane: self.kbviewPaneID
        ? ("ok: " + self.kbviewPaneID + "（降级模式）")
        : "registerSection 返回 false",
    });
    return !!self.kbviewPaneID;
  },

  /** 卸载降级分区（插件 disable/卸载时调；不调会留下一个打不开的空分区）。 */
  unregisterKbViewSection: function () {
    var self = ZoteroKB;
    if (self.kbviewPaneID && Zotero.ItemPaneManager
        && Zotero.ItemPaneManager.unregisterSection) {
      Zotero.ItemPaneManager.unregisterSection(self.kbviewPaneID);
    }
    self.kbviewPaneID = null;
  },

  /** 降级模式的分区渲染：一行提示 + 级别下拉 + 正文（只读）。 */
  kbviewRender: async function (doc, body, item) {
    var self = ZoteroKB;
    if (!doc || !body) return;
    try {
      const old = body.querySelector(".zotero-kb-kbview");
      if (old) old.remove();
    } catch (e) { /* ignore */ }
    const wrap = self.kbviewEl(doc, "div");
    wrap.className = "zotero-kb-kbview";
    wrap.setAttribute("style",
      "padding:4px 6px; font-size:" + self.kbviewFont() + "px; line-height:1.55;"
      + " max-height:85vh; display:flex; flex-direction:column; overflow:hidden;");
    const bar = self.kbviewEl(doc, "div");
    bar.setAttribute("style", "display:flex; gap:6px; align-items:center;"
      + " flex-wrap:nowrap; margin-bottom:4px; flex:0 0 auto;");
    const sel = self.kbviewEl(doc, "select");
    sel.setAttribute("style", "flex:0 0 auto; white-space:nowrap;");
    for (const lv of (self.kbLevels() || [])) {
      const op = self.kbviewEl(doc, "option");
      op.setAttribute("value", lv.id);
      op.textContent = lv.label;
      sel.appendChild(op);
    }
    sel.value = self.kbviewLevel();
    const btn = self.kbviewEl(doc, "button");
    btn.textContent = "重新读取";
    btn.setAttribute("style", "padding:1px 6px; flex:0 0 auto; white-space:nowrap;");
    const hint = self.kbviewEl(doc, "span");
    hint.setAttribute("style", "opacity:0.65; font-size:11px; flex:1 1 auto;"
      + " min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;");
    bar.append(sel, btn, hint);
    wrap.append(bar);
    const view = self.kbviewEl(doc, "div");
    view.setAttribute("style", "flex:1 1 auto; min-height:0; overflow:auto;");
    wrap.append(view);
    body.append(wrap);
    const paint = async function (prefer) {
      hint.textContent = "读取中…";
      let payload = null;
      // 降级模式：跑不了页面脚本，只能吃服务端渲染好的 MathML 孪生
      try { payload = await self.kbviewPayloadForItem(item, prefer, { wantHtml: true }); }
      catch (e) { payload = null; }
      if (!payload) { hint.textContent = "读取失败"; return; }
      hint.textContent = payload.empty
        ? "（这一篇还没有知识库内容）" : String(payload.hint || "");
      hint.setAttribute("title", hint.textContent);
      if (payload.empty) {
        self.kbviewSetHtml(doc, view, "<p style='opacity:0.75'>"
          + String(payload.emptyWhy || "") + "</p>");
        return;
      }
      self.kbviewSetHtml(doc, view, payload.html
        || "<p style='opacity:0.75'>（这一级是空的）</p>");
    };
    sel.addEventListener("change", () => {
      self.setKbviewLevel(sel.value);
      paint(sel.value).catch(() => {});
    });
    btn.addEventListener("command", () => { paint(sel.value).catch(() => {}); });
    btn.addEventListener("click", () => { paint(sel.value).catch(() => {}); });
    await paint(self.kbviewLevel());
  },


  // ================================================================ 阅读器那一列：取当前页（**不再跟随**）

  /**
   * 阅读器当前页（**1 起**，对齐知识库的 p.N）。取不到返回 0。
   *
   * 两条来源：
   *   ① reader._state.primaryViewState.pageIndex —— **实时**的内部状态，翻页当场就变；
   *   ② items 表的「最后阅读页」—— 阅读器每次翻页都会写它（reader.js 的
   *      _setState：先 setAttachmentLastPageIndex，再
   *      Zotero.Notifier.trigger('pageChange','file',itemID)），事件到的那一刻它已是新值。
   * **① 优先**：② 里留着"上次退出时的页码"，先问它会拿到陈旧的页。
   * 两处都是 **0 起**的，这里 +1。
   *
   * ⚠ 2026-10-11 起**只用于打开那一列时的初始页**（翻页**不再**跟随）：
   *   派发包《去跟随与上下标》§一 要求保留「取当前页」的能力 —— 打开阅读器列的
   *   那一刻用它定位一次就够了。原来还挂过 Notifier 的 file/pageChange 做跟随，
   *   已随跟随一起去掉（实测 reader.js::_setState 确实会 trigger 它，能跟随，
   *   但用户拍板不要 —— 手翻一次正文并不麻烦，而周期性重推最像"抽搐"的来源）。
   */
  kbviewReaderPage: function (reader) {
    if (!reader) return 0;
    let idx = NaN;
    try {
      const s = reader._state;
      if (s && s.primaryViewState
          && Number.isFinite(s.primaryViewState.pageIndex)) {
        idx = s.primaryViewState.pageIndex;
      }
    } catch (e) { /* 走兜底 */ }
    if (!Number.isFinite(idx) || idx < 0) {
      try {
        const item = Zotero.Items.get(reader.itemID);
        if (item && item.getAttachmentLastPageIndex) {
          idx = item.getAttachmentLastPageIndex();
        }
      } catch (e2) { /* ignore */ }
    }
    if (!Number.isFinite(idx) || idx < 0) return 0;
    return Math.floor(idx) + 1;
  },

  /** 当前选中的阅读器标签（不在阅读器页签时返回 null）。 */
  kbviewReaderInfo: function (win) {
    if (!win) return null;
    let reader = null;
    try {
      const tabs = win.Zotero_Tabs;
      if (!tabs || String(tabs.selectedType || "") !== "reader") return null;
      reader = (Zotero.Reader && Zotero.Reader.getByTabID)
        ? Zotero.Reader.getByTabID(tabs.selectedID) : null;
    } catch (e) { reader = null; }
    if (!reader) return null;
    return { reader: reader, itemID: reader.itemID,
             page: ZoteroKB.kbviewReaderPage(reader) };
  },

  /** 这个窗口里还有没有阅读器？（关干净后要把我们那一列收起来。） */
  kbviewHasReader: function (win) {
    try {
      const tabs = win && win.Zotero_Tabs;
      if (tabs && String(tabs.selectedType || "") === "reader") return true;
      const list = tabs && tabs._tabs;
      if (list && list.length) {
        for (const t of list) { if (t && String(t.type) === "reader") return true; }
        return false;
      }
    } catch (e) { /* 走下面的兜底 */ }
    try {
      const rs = Zotero.Reader && Zotero.Reader._readers;
      if (rs && rs.length) {
        for (const r of rs) { if (r && r.itemID && !r._isTabClosed) return true; }
        return false;
      }
    } catch (e2) { /* ignore */ }
    return true;   // 问不到就当「还有」—— 宁可多留一会儿，也不要乱关
  },

  /**
   * Notifier：翻页时立刻把右边滚到同一页；阅读器标签关光了就收起我们那一列。
   *
   * 注册 id 推到 self.notifyIDs，shutdown 里统一注销（既有机制）。
   */
  kbviewRegisterNotifier: function () {
    var self = ZoteroKB;
    if (!Zotero.Notifier || !Zotero.Notifier.registerObserver) return false;
    if (self._kbvNotifierID
        && (self.notifyIDs || []).indexOf(self._kbvNotifierID) >= 0) {
      return true;             // 已经注册过（幂等；kbviewMount 每个窗口都会调）
    }
    self._kbvNotifierID = null;
    try {
      // ⚠ 只要 tab（选中/新开标签 → 补阅读器工具栏的「对照」按钮）。
      //   file 原来是给 pageChange（按页跟随）用的，2026-10-11 去跟随之后不再需要。
      self._kbvNotifierID = Zotero.Notifier.registerObserver({
        notify: function (event, type, ids) {
          try { ZoteroKB.kbviewOnNotify(event, type, ids); }
          catch (e) { Zotero.debug("[zotero-kb] 侧栏通知处理失败：" + e); }
        },
      }, ["tab"], self.id + "-kbview");
      if (self._kbvNotifierID) {
        if (!self.notifyIDs) self.notifyIDs = [];
        if (self.notifyIDs.indexOf(self._kbvNotifierID) < 0) {
          self.notifyIDs.push(self._kbvNotifierID);
        }
      }
      return !!self._kbvNotifierID;
    } catch (e) {
      Zotero.debug("[zotero-kb] 注册侧栏 Notifier 失败：" + e);
      self._kbvNotifierID = null;
      return false;
    }
  },

  kbviewOnNotify: function (event, type, ids) {
    var self = ZoteroKB;
    // ⚠ 这里**不再**处理 file/pageChange：按页跟随已去掉（2026-10-11 用户拍板），
    //   翻页时右边那一列不再重推、不再重渲染。留下的只有「补工具栏按钮」一条 ——
    //   会话恢复出来的阅读器是在我们注册 renderToolbar **之前** mount 的，
    //   那个一次性事件不会再发，只能靠标签事件补挂（见 kbviewInjectToolbarButton）。
    if (type === "tab" && (event === "select" || event === "add")) {
      self.kbviewEnsureReaderToolbar();
    }
  },

  /**
   * 一次性把「当前页」推给**已经打开**的阅读器列。
   *
   * ⚠ 2026-10-11 起**没有自动调用点**了：按页跟随去掉之后，原来两条自动路径
   *   （宿主侧 400ms 轮询重推、Notifier 的 file/pageChange）都删了。
   *   函数本身**保留**（派发包《去跟随与上下标》§一 明确要求"别整体删掉"）：
   *   它是唯一一处「把当前页一次性推给已打开的列」的逻辑，条目窗格那一列与
   *   将来真要做的「跳到这一页」按钮都直接调它；真删了这条能力就彻底没有了。
   */
  kbviewFanoutPage: function (ids) {
    var self = ZoteroKB;
    const m = self.kbviewMap();
    if (!m) return;
    const hosts = [];
    try { m.forEach(function (v) { hosts.push(v); }); } catch (e) { return; }
    for (const host of hosts) {
      const dock = host.read;
      if (!dock || !dock.open) continue;
      try {
        const info = self.kbviewReaderInfo(host.win);
        if (!info || ids.indexOf(info.itemID) < 0) continue;
        if (!info.page || info.page === host.lastPage) continue;
        host.lastPage = info.page;
        self.kbviewSend(dock, { type: "page", page: info.page });
      } catch (e2) { /* 一个窗口出错不影响别的 */ }
    }
  },

  // ================================================================ 阅读器工具栏的「对照」按钮

  /**
   * 注册阅读器工具栏的「对照」按钮（**一键进入/退出对照模式**，派发包 §二）。
   *
   * Zotero.Reader.registerEventListener('renderToolbar', handler, pluginID)：
   * 阅读器每次渲染工具栏都会发这个事件，handler 拿到
   * {type, doc, params, append, reader}，**必须同步** append。
   * pluginID 传了之后，插件停用时 Zotero 会自己把它摘掉（不用手动反注册）。
   */
  kbviewRegisterReaderToolbar: function () {
    var self = ZoteroKB;
    if (self._kbvReaderToolbar) return true;
    if (!Zotero.Reader || typeof Zotero.Reader.registerEventListener !== "function") {
      return false;
    }
    self._kbvReaderToolbar = function (event) {
      try { ZoteroKB.kbviewAddToolbarButton(event); }
      catch (e) { Zotero.debug("[zotero-kb] 加阅读器「对照」按钮失败：" + e); }
    };
    try {
      Zotero.Reader.registerEventListener("renderToolbar", self._kbvReaderToolbar, self.id);
      self.writeStatusFile({ kbViewReaderToolbar: "ok" });
      return true;
    } catch (e) {
      self._kbvReaderToolbar = null;
      self.writeStatusFile({ kbViewReaderToolbar: "FAIL: " + e });
      return false;
    }
  },

  /**
   * 事件回调里往阅读器工具栏塞按钮。
   *
   * ⚠ **必须同步创建并 append**：阅读器那边 append 之后再调用就会抛
   *   "Append must be called directly and synchronously in the event"
   *   （见 resource/reader/reader.js 的 CustomSections）。
   * ⚠ 每次渲染事件都会来、工具栏的 custom-sections 每次都被 replaceChildren 清空，
   *   所以这里**每次都新建**一个（不是「已存在就跳过」）。
   */
  kbviewAddToolbarButton: function (event) {
    var self = ZoteroKB;
    const doc = event && event.doc;
    if (!doc || !doc.createElement || typeof event.append !== "function") return false;
    let win = null;
    try { win = Zotero.getMainWindow(); } catch (e) { win = null; }
    const btn = doc.createElement("button");
    btn.setAttribute("id", "zotero-kb-kbview-toolbar-button");
    btn.setAttribute("class", "toolbar-button");
    btn.setAttribute("title", "对照：右侧显示这一页的知识库正文（一键开 / 关）");
    btn.textContent = "对照";
    if (win && self.kbviewIsOpen(win, "reader")) {
      btn.setAttribute("aria-pressed", "true");
      btn.setAttribute("data-kbview-open", "true");
    }
    btn.addEventListener("click", function () {
      try {
        let w = null;
        try { w = Zotero.getMainWindow(); } catch (e2) { w = null; }
        self.kbviewToggle(w || win, "reader");
      } catch (e3) { Zotero.debug("[zotero-kb] 「对照」按钮出错：" + e3); }
    });
    event.append(btn);
    return true;
  },

  /**
   * 直接往阅读器工具栏**注入**一个「对照」按钮（幂等）。
   *
   * ⚠ 为什么需要它（真机实测，2026-10-11）：renderToolbar 是**一次性**的
   *   （React 那个 CustomSections 只在 mount 时发一次），而 Zotero 会**在启动时
   *   恢复会话里的阅读器标签** —— 那些阅读器可能在插件注册监听之前就挂好了，
   *   于是我们永远收不到事件，工具栏上就没有按钮（现象：阅读器右侧那一列只能
   *   靠 rail 图标开，工具栏上找不到入口）。
   *   这里绕过事件、直接把按钮塞进 React 那个 .custom-sections 容器；
   *   下次阅读器重渲染时会清掉，但那一刻 renderToolbar 会来，又会补上。
   */
  kbviewInjectToolbarButton: function (reader) {
    var self = ZoteroKB;
    const w = reader && reader._iframeWindow;
    if (!w) return false;
    let doc = null;
    try { doc = w.document; } catch (e) { return false; }
    if (!doc) return false;
    if (doc.getElementById("zotero-kb-kbview-toolbar-button")) return true;
    const host = doc.querySelector(".custom-sections");
    if (!host) return false;                 // 工具栏还没渲染 → 等下一次
    const btn = doc.createElement("button");
    btn.setAttribute("id", "zotero-kb-kbview-toolbar-button");
    btn.setAttribute("class", "toolbar-button");
    btn.setAttribute("title", "对照：右侧显示这一页的知识库正文（一键开 / 关）");
    btn.textContent = "对照";
    if (self.kbviewIsOpen(Zotero.getMainWindow(), "reader")) {
      btn.setAttribute("aria-pressed", "true");
    }
    btn.addEventListener("click", function () {
      try { self.kbviewToggle(Zotero.getMainWindow(), "reader"); }
      catch (e3) { Zotero.debug("[zotero-kb] 「对照」按钮出错：" + e3); }
    });
    const section = doc.createElement("div");
    section.setAttribute("class", "section");
    section.appendChild(btn);
    try { host.appendChild(section); } catch (e4) { return false; }
    return true;
  },

  /** 把「对照」按钮补到**所有已经挂好的**阅读器工具栏上（幂等）。 */
  kbviewEnsureReaderToolbar: function () {
    var self = ZoteroKB;
    let n = 0;
    try {
      const rs = (Zotero.Reader && Zotero.Reader._readers) || [];
      for (const r of rs) {
        try { if (self.kbviewInjectToolbarButton(r)) n++; }
        catch (e) { /* 单个阅读器失败不影响别的 */ }
      }
    } catch (e2) { /* ignore */ }
    return n;
  },

  // ================================================================ 图片与分块（纯函数）

  /**
   * 知识库图片的 resource:// 前缀（<kb>/mineru/<key>/ 下的相对路径拼在它后面）。
   *
   * ⚠ 为什么走 resource 而不是 file://（老做法）：resource:// 的页面加载
   *   file:// 图片会被安全策略**拦掉**，图片全空白且不报错（见 KB_FILES_RES）。
   */
  kbviewImgBase: function (key) {
    var self = ZoteroKB;
    try {
      const base = self.kbviewFilesURI();
      if (!base || !key) return "";
      return base + "mineru/" + String(key) + "/";
    } catch (e) { return ""; }
  },

  /** 阅读器栏里点了一张图 → 用系统默认程序打开 <kb>/mineru/<key>/<rel>。 */
  kbviewOpenImage: function (win, kind, rel) {
    var self = ZoteroKB;
    const item = self.kbviewCurrentItem(win);
    if (!item) return false;
    let kb = "";
    try { kb = String(self.kbDir() || "").replace(/[\\/]+$/, ""); } catch (e) { kb = ""; }
    const r = String(rel == null ? "" : rel).replace(/\\/g, "/").replace(/^\.\//, "");
    if (!kb || !r) return false;
    // 只允许相对路径，且不许穿越目录（与 docpage.image_path 同一口径）
    if (/^[a-z][a-z0-9+.\-]*:/i.test(r) || r.charAt(0) === "/" || r.indexOf("..") >= 0) {
      return false;
    }
    const abs = kb + "\\mineru\\" + String(item.key) + "\\" + r.replace(/\//g, "\\");
    try {
      self.openKbPath(abs, "图片原图");
      return true;
    } catch (e2) {
      Zotero.debug("[zotero-kb] 打开原图失败：" + e2);
      return false;
    }
  },

  // 管道表 / 独立公式 / 独立图片行（与 mdhtml.py 同一套判据）
  KBV_RE_TABLE_ROW: /^\s*\|.*\|\s*$/,
  KBV_RE_TABLE_SEP: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
  KBV_RE_LINE_MATH: /^\s*(?:\$\$.+\$\$|\\\[.+\\\])\s*$/,
  KBV_RE_LINE_IMG: /^\s*!\[[^\]]*\]\([^)]+\)\s*$/,

  /**
   * 把一页 markdown 切成**块**（派发包 §3.2 的口径），并保留块之间的原文分隔符 ——
   * **一字不改地拼回去**是硬要求（kbviewJoinBlocks(parts) === 原文，桩测试钉住）：
   *   · 空行分段；独立成行的 $$…$$ / \[…\] 自成一「块」；
   *   · 管道表整块；独占一行的图片行单独一块；段内单换行仍属同一块。
   *
   * 返回 {prefix, blocks[], seps[], suffix}，拼法就是**纯字符串相加**：
   *   prefix + blocks[0] + seps[0] + blocks[1] + … + suffix
   * seps[i] 是 blocks[i] 与 blocks[i+1] 之间的原文（含换行；空行也在里面）。
   */
  kbviewSplitBlocks: function (md) {
    var self = ZoteroKB;
    const s = String(md == null ? "" : md).replace(/\r\n?/g, "\n");
    const lines = s.split("\n");
    const n = lines.length;
    const off = new Array(n);
    let acc = 0;
    for (let i = 0; i < n; i++) { off[i] = acc; acc += lines[i].length + 1; }
    const endOf = function (i) { return off[i] + lines[i].length; };
    const blank = function (i) { return !lines[i].trim(); };
    const lineMath = function (i) { return self.KBV_RE_LINE_MATH.test(lines[i]); };
    const lineImg = function (i) { return self.KBV_RE_LINE_IMG.test(lines[i]); };
    const tableAt = function (i) {
      return i + 1 < n && self.KBV_RE_TABLE_ROW.test(lines[i])
        && self.KBV_RE_TABLE_SEP.test(lines[i + 1]);
    };
    // 先算出每个块覆盖哪些行（[a, b)：左闭右开、互不重叠、按顺序）
    const region = [];
    let k = 0;
    while (k < n) {
      if (blank(k)) { k++; continue; }
      let b;
      if (lineMath(k)) {
        b = k + 1;
      } else if (tableAt(k)) {
        b = k + 2;
        while (b < n && self.KBV_RE_TABLE_ROW.test(lines[b])) b++;
      } else if (lineImg(k)) {
        b = k + 1;
      } else {
        b = k + 1;
        while (b < n && !blank(b) && !lineMath(b) && !lineImg(b) && !tableAt(b)) b++;
      }
      region.push([k, b]);
      k = b;
    }
    if (!region.length) return { prefix: s, blocks: [], seps: [], suffix: "" };
    const prefix = s.slice(0, off[region[0][0]]);
    const blocks = [];
    const seps = [];
    for (let r = 0; r < region.length; r++) {
      const a = region[r][0], bb = region[r][1];
      blocks.push(s.slice(off[a], endOf(bb - 1)));
      if (r + 1 < region.length) {
        seps.push(s.slice(endOf(bb - 1), off[region[r + 1][0]]));
      }
    }
    const lastR = region[region.length - 1];
    const suffix = s.slice(endOf(lastR[1] - 1));
    return { prefix: prefix, blocks: blocks, seps: seps, suffix: suffix };
  },

  /** kbviewSplitBlocks 的逆（纯字符串相加）。 */
  kbviewJoinBlocks: function (parts) {
    if (!parts) return "";
    let s = String(parts.prefix == null ? "" : parts.prefix);
    const blocks = parts.blocks || [];
    const seps = parts.seps || [];
    for (let i = 0; i < blocks.length; i++) {
      s += String(blocks[i] == null ? "" : blocks[i]);
      if (i < seps.length) s += String(seps[i] == null ? "" : seps[i]);
    }
    s += String(parts.suffix == null ? "" : parts.suffix);
    return s;
  },


  // ================================================================ 纯函数与文件工具（自建列 / 降级模式共用）

  /**
   * 把正文 markdown 按 "## p.N" 切成 {页码: 该页 markdown}（**纯函数**，桩测试直接测）。
   * 为什么在插件侧再切一次：服务端渲染好的 HTML 里拿不回 markdown 源文本，而
   * 「改这一页」要编辑的正是 markdown（图片行、公式、表格都是 markdown）。
   */
  kbviewSplitMd: function (md) {
    var out = {};
    var cur = null;
    var buf = [];
    var lines = String(md == null ? "" : md).split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var m = lines[i].match(/^##\s*p\.(\d+)\s*$/);
      if (m) {
        if (cur !== null) out[cur] = buf.join("\n").replace(/^\n+|\n+$/g, "");
        cur = parseInt(m[1], 10);
        buf = [];
      } else if (cur !== null) {
        buf.push(lines[i]);
      }
    }
    if (cur !== null) out[cur] = buf.join("\n").replace(/^\n+|\n+$/g, "");
    return out;
  },

  /** 轮询一个 job 直到结束（最多约 6 分钟）。返回 job 对象，拿不到返回 {}。 */
  kbviewWaitJob: async function (jobId, onTick) {
    var self = ZoteroKB;
    if (!jobId) return {};
    for (var i = 0; i < 240; i++) {
      var job = {};
      try { job = await self.request("GET", "/jobs/" + encodeURIComponent(jobId)); }
      catch (e) { return {}; }
      if (onTick) onTick(job);
      if (job && job.state && job.state !== "running") return job;
      await new Promise(function (r) { setTimeout(r, 1500); });
    }
    return { state: "timeout" };
  },

  /**
   * 保存/恢复一页正文：POST /doc-edit -> 等 job -> 返回是否成功。
   * 保存之后服务端会**重切重向量**（用户口径"改完立即"），所以这里必须等它跑完
   * 再重绘，否则页面读到的还是旧内容。
   */
  kbviewRunDocEdit: async function (body, onStatus) {
    var self = ZoteroKB;
    var note = onStatus || function () {};
    note("正在重建切片…");
    var res = null;
    // W3 线2.5（2026-10-14）：桥优先（`doc.save`，**写路径**：保存某一页的修订并派一次
    // 单篇重建 —— 与 HTTP `POST /doc-edit` 同一份实现），失败退回老 HTTP。
    try {
      try { res = await self.kbAdminHostRoute("POST", "/doc-edit", body); }
      catch (e0) {
        Zotero.debug("[zotero-kb] 引擎桥保存正文失败（退回 HTTP）：" + e0);
        res = await self.request("POST", "/doc-edit", body);
      }
    } catch (e) {
      note("请求失败：" + ((e && e.message) ? e.message : e));
      return false;
    }
    if (!res || !res.job) {
      note("服务没有返回任务：" + JSON.stringify(res == null ? {} : res).slice(0, 120));
      return false;
    }
    var done = await self.kbviewWaitJob(res.job, function (j) {
      var lg = (j && j.log) || [];
      note("正在重建切片… " + String(lg[lg.length - 1] || "").slice(0, 60));
    });
    if (done && done.state === "done") { note("已重建，正在刷新…"); return true; }
    note("重建失败：" + ((done && done.error) || "未知"));
    return false;
  },

  /**
   * 把 `<img src="相对路径">` 改写成能加载的 `resource://zotero-kb-files/…`，
   * 并把**绝对路径**写进 `data-kbv-path`（页面上点图"看原图"仍交给系统默认程序）。
   *
   * kb 文件里图片是 `images/xxx.jpg`（相对 `<kb>/mineru/<key>/`），这样知识库
   * 整体搬家不用重写任何文件；绝对/协议相对的 src 原地不动（幂等），
   * 拿不到 kbDir 时原样返回（宁可裂图，也不要拼出半个坏 URL）。
   *
   * ⚠ 这里**不能**用 `file://`：resource:// 的页面加载 file:// 图片会被安全策略
   *   拦掉，表现是图片全都空白（naturalWidth=0）—— 真机实测踩到，见 KB_FILES_RES。
   */
  kbviewFixImgs: function (html, key) {
    var self = ZoteroKB;
    var s = String(html == null ? "" : html);
    if (s.indexOf("<img") < 0) return s;
    var base = "";
    var dir = "";
    try {
      const kb = String(self.kbDir() || "").replace(/[\\/]+$/, "");
      if (kb && key) {
        dir = kb + "\\mineru\\" + key + "\\";
        base = self.kbviewFilesURI();
      }
    } catch (e) { base = ""; dir = ""; }
    if (!base || !dir) return s;
    return s.replace(/(<img\b[^>]*?)\bsrc="([^"]*)"/g,
      function (m, pre, src) {
        if (!src || /^(?:[a-z][a-z0-9+.\-]*:|\/\/|#)/i.test(src)) return m;
        const rel = String(src).replace(/\\/g, "/").replace(/^\.\//, "");
        const abs = dir + rel.replace(/\//g, "\\");
        return pre + 'src="' + base + "mineru/" + key + "/" + rel
          + '" data-kbv-path="' + abs.replace(/"/g, "&quot;") + '"';
      });
  },

  /**
   * 最小 markdown → HTML（**纯函数**，桩测试直接测它）。
   * 覆盖知识库 md 里真正用到的语法：# 标题、-/数字列表、> 引用、**粗**、*斜*、
   * 反引号代码、[文字](链接)、$$公式$$、![图注](图片)、管道表、空行分段。
   * ⚠ 先转义 HTML 再套标记：md 是我们自己生成的，但笔记原文里有 <、&。
   */
  md2html: function (md) {
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s)
      .replace(/\$\$([^$]+)\$\$/g,
               '<code style="background:rgba(127,127,127,0.15);'
               + ' padding:0 3px; border-radius:3px;">$1</code>')
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g,
               '<img src="$2" alt="$1" style="max-width:100%; height:auto;'
               + ' display:block; margin:4px 0;" />')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

    const out = [];
    let list = "";
    let buf = [];
    let pageOpen = false;
    const flushBuf = () => {
      if (buf.length) { out.push("<p>" + buf.join("<br>") + "</p>"); buf = []; }
    };
    const closeList = () => {
      if (list) { out.push("</" + list + ">"); list = ""; }
    };
    const cell = (tag, text) => "<" + tag + " style=\"border:1px solid"
      + " rgba(127,127,127,0.45); padding:2px 5px; text-align:left;"
      + " vertical-align:top;\">" + inline(text) + "</" + tag + ">";
    const splitRow = (line) => {
      let s = String(line).trim();
      if (s.charAt(0) === "|") s = s.slice(1);
      if (s.charAt(s.length - 1) === "|") s = s.slice(0, -1);
      return s.split("|").map((c) => c.trim());
    };
    const lines = String(md == null ? "" : md).split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const rawI = lines[i];
      if (/^\s*\|.*\|\s*$/.test(rawI)
          && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?/.test(lines[i + 1])) {
        flushBuf(); closeList();
        const rows = [splitRow(rawI)];
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        i--;
        const head = rows[0] || [];
        let html = "<table style=\"border-collapse:collapse; margin:6px 0;"
          + " font-size:0.95em; max-width:100%;\"><thead><tr>"
          + head.map((c) => cell("th", c)).join("") + "</tr></thead>";
        if (rows.length > 1) {
          html += "<tbody>";
          for (const r of rows.slice(1)) {
            const cells = r.slice(0, head.length);
            while (cells.length < head.length) cells.push("");
            html += "<tr>" + cells.map((c) => cell("td", c)).join("") + "</tr>";
          }
          html += "</tbody>";
        }
        out.push(html + "</table>");
        continue;
      }
      const raw = rawI;
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { flushBuf(); closeList(); continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+p\.(\d+)\s*$/))) {
        flushBuf(); closeList();
        if (pageOpen) out.push("</div>");
        out.push('<div class="kb-page" data-kbpage="' + m[2] + '">');
        pageOpen = true;
        const pLvl = m[1].length;
        const pSize = { 1: "1.35em", 2: "1.18em", 3: "1.06em" }[pLvl] || "1em";
        out.push('<div style="font-weight:600; font-size:' + pSize
                 + '; margin:6px 0 2px;">p.' + m[2] + "</div>");
        continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushBuf(); closeList();
        const lvl = m[1].length;
        const size = { 1: "1.35em", 2: "1.18em", 3: "1.06em" }[lvl] || "1em";
        out.push('<div style="font-weight:600; font-size:' + size + '; margin:6px 0 2px;">'
                 + inline(m[2]) + "</div>");
        continue;
      }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
        flushBuf();
        if (list !== "ul") { closeList(); out.push("<ul style='margin:2px 0 2px 16px;'>"); list = "ul"; }
        out.push("<li>" + inline(m[1]) + "</li>");
        continue;
      }
      if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
        flushBuf();
        if (list !== "ol") { closeList(); out.push("<ol style='margin:2px 0 2px 18px;'>"); list = "ol"; }
        out.push("<li>" + inline(m[2]) + "</li>");
        continue;
      }
      if ((m = line.match(/^>\s?(.*)$/))) {
        flushBuf(); closeList();
        out.push("<blockquote style='margin:4px 0; padding:1px 6px;"
                 + " border-left:3px solid rgba(127,127,127,0.5); opacity:0.85;'>"
                 + inline(m[1]) + "</blockquote>");
        continue;
      }
      if ((m = line.match(/^\s*\$\$(.+)\$\$\s*$/) || line.match(/^\s*\\\[(.+)\\\]\s*$/))) {
        flushBuf(); closeList();
        out.push("<div style='margin:4px 0; padding:3px 6px; font-family:monospace;"
                 + " background:rgba(127,127,127,0.12); border-radius:3px;"
                 + " overflow-x:auto;'>" + esc(m[1]) + "</div>");
        continue;
      }
      buf.push(inline(line));
    }
    flushBuf(); closeList();
    if (pageOpen) out.push("</div>");
    return out.join("\n");
  },
});
