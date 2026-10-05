/**
 * 15-prefpane.js —— 设置面板注册与运行状态文件（排错靠它）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 设置面板

  /**
   * 注册设置面板。用 Zotero 官方的 PreferencePanes（Zotero 7+）。
   *
   * 为什么不用之前那套：旧实现去 `document.getElementById("zotero-prefpane-advanced")`
   * 注入 DOM，而 Zotero 7+ 的设置窗口根本不是那个结构 —— 那个元素不存在，
   * 而且 `addToWindow` 也不在 Zotero 的插件生命周期里、压根不会被调用。
   * 结果就是插件装上了却没有可用的设置界面（用户没法填服务地址/token）。
   *
   * 现在把 settings.xhtml 交给 Zotero 自己渲染，交互逻辑放在 settings.js 里
   * （通过 scripts 选项加载，避免内联脚本被 CSP 拦掉）。
   *
   * ⚠ 三个必须遵守的点（都踩过或读源码确认过）：
   *
   * 1. `register()` 返回 **Promise**，不 await 的话注册失败会被完全吞掉 ——
   *    表现就是"设置里看不到这个面板"而没有任何提示。所以这里接住它。
   *
   * 2. 参数名是 `label`，**没有 `rawLabel`** —— 看 Zotero 的
   *    preferencePanes.js：`rawLabel: options.label || (await getName(...))`。
   *    传 rawLabel 是无效的（旧代码传了，无害但误导）。
   *
   * 3. `src` 指向的 XHTML **不能带 `<?xml ...?>` 声明**。
   *    Zotero 是把文件内容当字符串嵌进 `<div>` 再解析的
   *    （preferences.js 的 _parseXHTMLToFragment），声明在 <div> 内部
   *    就是非法 XML → 整个面板点进去一片空白。
   *    工具：`python tools\check_prefpane.py` 专门回归这一条。
   */
  registerPrefPane: function () {
    var self = ZoteroKB;
    try {
      if (!Zotero.PreferencePanes || !Zotero.PreferencePanes.register) {
        Zotero.debug("[zotero-kb] 这个 Zotero 版本没有 PreferencePanes，跳过设置面板");
        return;
      }
      // 幂等：Zotero 会在插件 shutdown 时自动注销本插件的 pane，但运行中
      // 被重载时可能残留，重复 register 同一个 id 会抛错（下面接住即可）。
      const src = self.rootURI + "settings.xhtml";
      const p = Zotero.PreferencePanes.register({
        pluginID: self.id,
        id: "zotero-kb-prefpane",
        label: "文献知识库",
        image: self.rootURI + "icon.svg",
        src: src,
        scripts: [self.rootURI + "settings.js"],
      });
      // register 是 async 的：成功 resolve 面板 id，失败 reject。
      if (p && typeof p.then === "function") {
        p.then((id) => {
          Zotero.debug("[zotero-kb] 设置面板已注册，id=" + id);
        }).catch((e) => {
          // "already registered" 是重启/重载时的正常情况，其余要报出来
          const msg = String((e && e.message) || e);
          if (msg.includes("already registered")) {
            Zotero.debug("[zotero-kb] 设置面板已存在（重载时正常）：" + msg);
          } else {
            Zotero.logError(new Error("[zotero-kb] 注册设置面板失败：" + msg));
          }
        });
      }
      Zotero.debug("[zotero-kb] 设置面板注册请求已发出：" + src);
    } catch (e) {
      Zotero.logError(new Error("[zotero-kb] 注册设置面板抛错：" + e));
    }
  },


  /** 把运行状态写到文件，方便从 Python 侧和排障时查看。
   *
   * ⚠ 每次写都把调用方给的 `extra` **累积**在 `_statusExtra` 里再一起写。
   *   原因是这条链上有很多调用方：startup 写完 `startupSteps`，几秒后
   *   任务轮询每隔 2 秒再写一次（不带 extra）—— 而本函数是**整文件覆盖**，
   *   于是那些诊断字段（startupSteps / itemPane / locale / readerEvents…）
   *   只存在了几秒钟就没了。用户报"分区正文空白"时要查的恰恰是那几个字段，
   *   结果一个都看不到（2026-10-05 亲历）。
   */
  writeStatusFile: function (extra) {
    var self = ZoteroKB;
    try {
      const path = self.kbDir() + "\\plugin-status.json";
      const cacheKeys = Object.keys(self.weightsCache || {});
      const data = {
        pluginVersion: self.version,
        zoteroVersion: Zotero.version,
        platformVersion: Services.appinfo.platformVersion,
        server: self.getPref(self.PREFS.server, ""),
        model: self.getPref(self.PREFS.model, ""),
        autoProcess: !!self.getPref(self.PREFS.autoProcess, true),
        serverOk: self.serverOk,
        serverInfo: self.serverInfo,
        weightColumnRegistered: !!self.weightColumnKey,
        weightColumnKey: self.weightColumnKey || "",
        weightsCached: cacheKeys.length,
        // 缓存里前几个 key —— 用来核对"缓存里的 key"和"条目 key"对不对得上
        weightsSampleKeys: cacheKeys.slice(0, 5),
        // 列渲染统计：dataProvider 被调用了几次、命中几次。
        // 如果 providerCalls 是 0，说明列压根没渲染；
        // 如果调用很多但命中 0，说明 key 对不上。
        providerCalls: self.colDiag ? self.colDiag.calls : 0,
        providerHits: self.colDiag ? self.colDiag.hits : 0,
        providerSample: self.colDiag ? (self.colDiag.sample || []) : [],
        taskPolling: !!self.taskPolling,
        taskBusy: !!self.taskBusy,
        tickCount: self.tickCount || 0,
        lastTickAt: self.lastTickAt || "",
        timerDiag: self.timerDiag || null,
        alive: !!self.alive,
        lastRequestError: self.lastRequestError || "",
        lastRequests: self.lastRequests || [],
        wroteAt: new Date().toISOString(),
      };
      if (extra) {
        self._statusExtra = Object.assign(self._statusExtra || {}, extra);
      }
      Object.assign(data, self._statusExtra || {});
      const file = Zotero.File.pathToFile(path);
      Zotero.File.putContents(file, JSON.stringify(data, null, 2));
    } catch (e) {
      Zotero.debug("[zotero-kb] 写状态文件失败：" + e);
    }
  },
});
