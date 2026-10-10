/**
 * 17-windowhook.js —— 主窗口加载/卸载钩子（窗口重建时把 UI 带回来）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 窗口钩子

  /**
   * 主窗口加载时的处理。
   *
   * 官方文档明确要求：与窗口相关的 UI 活动（菜单、自定义列等）应由
   * `onMainWindowLoad` 执行，否则新开的主窗口不会包含这些改动
   * （见 Zotero 中文社区的 bootstrap 参考）。
   * 这里把权重列注册和轮询启动都放一份 —— 窗口重建时能把它们带回来。
   */
  onMainWindowLoad: function (win) {
    var self = ZoteroKB;
    // 诊断计数：本机实测启动时这个钩子**不会被调用**（见 startup 里的说明），
    // 留个计数器便于后续一眼看出"窗口钩子到底跑没跑"。
    self.__windowHookCalled = (self.__windowHookCalled || 0) + 1;
    Zotero.debug("[zotero-kb] onMainWindowLoad #" + self.__windowHookCalled);
    self.mainWindow = win || null;
    // ⚠ 每个窗口都要把自己的 ftl 挂上（新开的窗口里 linkset 是新的）——
    //   少了这一步，那个窗口里的 data-l10n-id 全是空白（见 initLocale）。
    try { self.initLocale(win); }
    catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里挂 ftl 失败：" + e); }
    try {
      if (!self.weightColumnKey) self.registerWeightColumn();
    } catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里注册列失败：" + e); }
    // 「已建知识库」「分节纲要」两列同理（窗口重建后列定义要重新交一遍）
    try {
      if (!self.kbColKeys || !self.kbColKeys.length) self.registerKbColumns();
    } catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里注册两列失败：" + e); }
    // 工具栏按钮与条目右键菜单也属于"与窗口相关的 UI"，必须在窗口钩子里做
    try { self.registerToolbarButton(win); }
    catch (e) { Zotero.debug("[zotero-kb] 加工具栏按钮失败：" + e); }
    try { self.registerItemMenu(win); }
    catch (e) { Zotero.debug("[zotero-kb] 加右键菜单失败：" + e); }
    // 窗口重建后定时器可能已经没了，重新拉起来
    try {
      if (!self.taskPolling) self.startTaskPolling();
    } catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里启动轮询失败：" + e); }
  },


  onMainWindowUnload: function (win) {
    var self = ZoteroKB;
    Zotero.debug("[zotero-kb] onMainWindowUnload");
    // 清掉注入到该窗口的 DOM（Zotero 会在窗口关闭时销毁，但多窗口时
    // 明确移除更稳，也避免残留引用）
    try {
      const doc = win && win.document;
      if (doc) {
        const btn = doc.getElementById("zotero-tb-kb-panel");
        if (btn) btn.remove();
        const menu = doc.getElementById("zotero-kb-send-menu");
        if (menu) menu.remove();
      }
    } catch (e) { /* ignore */ }
    self.mainWindow = null;
    // 文档要求：窗口卸载时要停掉计时器，否则可能内存泄漏
    // （只停轮询，列注册保留 —— 下次 onMainWindowLoad 会复用）
    try { self.stopTaskPolling(); } catch (e) { /* ignore */ }
  },
});
