/**
 * 01-lifecycle.js —— 卸载与关闭：`shutdown` / `install` / `uninstall`
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  shutdown: function () {
    // 同 startup：Zotero 调 shutdown() 时 `this` 同样不可靠，用闭包引用
    var self = ZoteroKB;
    self.alive = false;
    // 关掉可能还开着的进度提示（不留孤儿窗口）
    try { self.closeProgress(); } catch (e) { /* ignore */ }
    // Ollama 收尾：只有"标记文件在"（= 是我们拉起来的）才关。
    // 放在最前面 —— Zotero 关闭时留给 shutdown 的时间有限，先做要紧的。
    try { self.ollamaOnShutdown(); } catch (e) { /* ignore */ }
    // 本地服务同理：只关我们自己拉起的那一个（随弃随关）
    try { self.serverOnShutdown(); } catch (e) { /* ignore */ }
    try {
      if (Zotero.ZoteroKB === self) delete Zotero.ZoteroKB;
    } catch (e) { /* ignore */ }
    try { self.stopTaskPolling(); } catch (e) { /* ignore */ }
    try { self.unregisterWeightColumn(); } catch (e) { /* ignore */ }
    try {
      if (self.notifyIDs && self.notifyIDs.length) {
        self.notifyIDs.forEach((id) => Zotero.Notifier.unregisterObserver(id));
        self.notifyIDs = [];
      }
    } catch (e) { Zotero.logError(e); }
    try {
      if (self.prefObserver) {
        Zotero.Prefs.unregisterObserver(self.prefObserver);
        self.prefObserver = null;
      }
    } catch (e) { Zotero.logError(e); }
    Zotero.debug("[zotero-kb] 已卸载");
  },


  install: function () {},

  uninstall: function () {},

  // ================================================================ 首选项

  PREFS: {
    server: "zotero-kb.server",
    token: "zotero-kb.token",
    model: "zotero-kb.model",
    autoProcess: "zotero-kb.autoProcess",
    categories: "zotero-kb.categories",
    // ---- 模型来源（支持多后端）
    provider: "zotero-kb.provider",     // ollama | openai
    apiModel: "zotero-kb.apiModel",     // 如 deepseek-chat
    apiKey: "zotero-kb.apiKey",
    apiBaseUrl: "zotero-kb.apiBaseUrl",
    // 从 DSH 导入文献前是否先弹确认框（**默认关**）。
    //
    // ⚠ 这里原来默认是 true，被用户否掉了。用户的原话：
    //   "查询文献都是在dsh中做，那下载列表最好也是通过dsh的对话中显示，
    //    这样才是更符合使用习惯的"
    //   —— 搜索、列表、挑选本来就在 DSH 对话里完成，再弹一个 Zotero 模态框
    //   等于把一次连贯的对话中断成两个界面。**确认应该在用户做选择的地方。**
    //   所以：确认与回报都挪到 DSH 对话里，Zotero 这边默认静默入库。
    //   想要老行为的人可以在设置面板把这个勾打开。
    acquireConfirm: "zotero-kb.acquireConfirm",
    // 从 DSH 导入的文献，落库后是否走一遍**本地模型的分类/打标签**流程。
    //
    // 用户明确要这一步（"填进去后走本地大模型自动打标签分类的流程"）。
    // 一次性全部算完、再用**一个**汇总框问（见 askApplyBatch）——
    // 不用每篇弹一次：抓 5 篇弹 5 个模态框，点完"添加"还要连点 5 次。
    acquireAutoClassify: "zotero-kb.acquireAutoClassify",
  },
});
