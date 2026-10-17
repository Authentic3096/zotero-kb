/**
 * 05-server.js —— 与引擎的桥接（`healthCheck` / `kbItemInfo` / `kbJobGet`）
 *
 * ⚠ 2026-10 起老 HTTP 服务（127.0.0.1:8765）及其全部代码已删除。
 *   插件只走**宿主半的引擎桥**（`kbAdminHostRoute` → 引擎子进程，stdio 无端口）；
 *   这里不再有 `baseURL()` / `request()`，也没有任何 HTTP 兜底。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 引擎桥

  /**
   * 某几条文献在知识库里的状态（`{items:[…]}`）—— **两个调用方共用这一处**：
   *   · `06-watch.js`：监听新条目，判"这条建过库没有"（看 `in_kb` / `n_chunks`）；
   *   · `12-dsh.js`：把文献发给 DSH 时，带上权重/分类/正文字数。
   *
   * 只走引擎桥（`kb.item-info`）；拿不到就返回 null —— 调用方按"拿不到信息"
   * 处理，绝不抛（列表不该因为引擎没起来就画不出来）。
   *
   * @returns Promise<{items:Array}|null>
   */
  kbItemInfo: async function (keys) {
    var self = ZoteroKB;
    const ks = Array.isArray(keys) ? keys.slice() : [keys];
    const want = ks.filter((k) => k).map(String);
    if (!want.length) return null;
    try {
      const r = await self.kbAdminHostRoute("POST", "/item-info", { keys: want });
      if (r && r.items) return r;
      Zotero.debug("[zotero-kb] 引擎桥的 item-info 没给 items");
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥取条目信息失败：" + e);
    }
    return null;
  },


  /**
   * 查一条长任务的状态（`{state, log, result, error}`）。
   *
   * 只走引擎桥（`jobs.get`）；拿不到返回 null（调用方按"查不到"处理，绝不抛）。
   */
  kbJobGet: async function (jobId) {
    var self = ZoteroKB;
    const id = String(jobId || "").trim();
    if (!id) return null;
    try {
      const j = await self.kbAdminHostRoute("GET", "/jobs/" + encodeURIComponent(id));
      if (j) return j;
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥查任务失败：" + e);
    }
    return null;
  },


  /**
   * 插件/引擎是否活着（问引擎桥的 `kb.health`）。
   *
   * 判据只有一条：**桥给到 info = 在线，桥拿不到 = 离线**。桥背后是
   * 宿主半按需拉起的 stdio 引擎（无端口、无 token），所以这里也没有任何
   * HTTP 请求。
   */
  healthCheck: async function () {
    var self = ZoteroKB;
    let engineInfo = null;
    try {
      engineInfo = await self.kbAdminHostRoute("GET", "/health");
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥 /health 不可用：" + e);
    }
    if (engineInfo) {
      this.serverOk = true;
      this.serverInfo = engineInfo;
      this.rememberServerInfo(engineInfo);
      // 拿到 kb_dir 后补一次老配置迁移（启动时 kb_dir 可能还不知道；
      // 迁移本身幂等，成功过就什么都不做）。
      try { this.migrateLlmPrefs(); } catch (e) { /* 不挡启动 */ }
      try { this.migratePathPrefs(); } catch (e2) { /* 同上 */ }
      this.serverKbDir = engineInfo.kb_dir || this.serverKbDir || "";
      this.serverProjectRoot = engineInfo.project_root || this.serverProjectRoot || "";
      Zotero.debug("[zotero-kb] 引擎桥在线（kb_dir=" + this.serverKbDir + "）");
    } else {
      this.serverOk = false;
      this.serverInfo = null;
      Zotero.debug("[zotero-kb] 引擎桥不可用：判为离线");
    }
    return this.serverOk;
  },
});
