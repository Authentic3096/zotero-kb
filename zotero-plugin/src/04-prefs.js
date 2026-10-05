/**
 * 04-prefs.js —— 首选项：默认值、读取、变更监听
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  registerPrefs: function () {
    // 默认值可以在这里设，也可以走 prefs.js 前缀注册。
    // 这里用 Zotero.Prefs.set 兜底（只在未设置时写），避免依赖 defaults 文件。
    const defaults = {};
    defaults[this.PREFS.server] = "http://127.0.0.1:8765";
    defaults[this.PREFS.token] = "";
    defaults[this.PREFS.model] = "";
    defaults[this.PREFS.autoProcess] = true;
    defaults[this.PREFS.categories] = "";   // 空=用知识库现有的
    defaults[this.PREFS.acquireConfirm] = false;
    defaults[this.PREFS.acquireAutoClassify] = true;
    // ⚠ 2026-10-05：chatQuitWarn / chatNumCtx 随「内容窗格聊天」一起删了
    //   （见 00-core.js 的 startup 里那段说明）。prefs.js 的默认值也删了。
    for (const [key, value] of Object.entries(defaults)) {
      try {
        if (Zotero.Prefs.get(key) === undefined) {
          Zotero.Prefs.set(key, value);
        }
      } catch (e) { /* 个别键可能未被允许，忽略 */ }
    }
  },


  getPref: function (key, fallback) {
    try {
      const v = Zotero.Prefs.get(key);
      return (v === undefined || v === null || v === "") ? fallback : v;
    } catch (e) {
      return fallback;
    }
  },


  registerPrefObserver: function () {
    const self = this;
    this.prefObserver = {
      observe: function (_subject, _topic, pref) {
        if (pref === self.PREFS.server || pref === self.PREFS.token) {
          self.healthCheck().catch(() => {});
        }
      },
    };
    Zotero.Prefs.registerObserver(this.PREFS.server, this.prefObserver);
    Zotero.Prefs.registerObserver(this.PREFS.token, this.prefObserver);
  },
});
