/**
 * 15-prefpane.js —— 运行状态文件（排错靠它）
 *
 * ⚠ 2026-10-17：原来的「Zotero 设置 → 文献知识库」偏好窗格**已整体下线**，
 *   设置项都搬进了管理面板（`content/admin-pages/prefs.js`）。所以这个文件
 *   里不再有 `registerPrefPane`，只剩 `writeStatusFile`；文件名保留是为了
 *   不打乱 build_bootstrap 的编号顺序。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

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
        model: (self.llmConfig() || {}).model || "",
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
        // W3 线2：任务通道现况。排"派了活没人干"时先看这三个字段 ——
        // mailboxDirPath 为空 = kbDir() 拿不到（那条通道被跳过）；
        // mailboxClaimed 一直不涨 = 插件没在轮询或信箱里没有东西。
        mailboxDirPath: self.mailboxDirPath || (self.mailboxDir ? self.mailboxDir() : ""),
        mailboxClaimed: self.mailboxClaimed || 0,
        mailboxLastTask: self.mailboxLastTask || "",
        mailboxLastClaimAt: self.mailboxLastClaimAt || "",
        taskPollHttp: !!self.getPref("zotero-kb.taskPollHttp", true),
        timerDiag: self.timerDiag || null,
        alive: !!self.alive,
        lastRequestError: self.lastRequestError || "",
        lastRequests: self.lastRequests || [],
        // 「生成分节纲要」的现况与上一批结果。
        // 为什么必须落盘：这项的 UI 只有一个 Zotero 进度窗（而且它**没有**
        // 可查的历史），出问题（"点了没反应"/"列没变"）时，能从 Python 侧
        // 读到"到底跑没跑、跑到第几篇、几篇失败"是唯一的排查入口。
        digestBusy: self.digestActive ? {
          total: self.digestActive.total,
          i: self.digestActive.i,
          currentKey: self.digestActive.currentKey || "",
          currentLabel: self.digestActive.currentLabel || "",
          stopped: !!self.digestActive.stopped,
        } : null,
        lastDigest: self.lastDigest || null,
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
