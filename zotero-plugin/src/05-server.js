/**
 * 05-server.js —— 与本地服务通信：`baseURL` / `request` / `healthCheck`
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 与服务通信

  /**
   * 老的 HTTP 通道地址（**固定值**，不再有首选项）。
   *
   * 8765 服务已退役、插件平时不碰它；这个地址只剩一个用途：万一有人按回退说明
   * （`scripts\4-service.vbs`）手动把它起起来，`kbItemInfo` / `kbJobGet` 的兜底
   * 分支还认得它。所以这里写死字面量，**不再从 pref 读**（那个 pref 已删）。
   */
  baseURL: function () {
    return "http://127.0.0.1:8765";
  },


  request: async function (method, path, body) {
    const url = this.baseURL() + path;
    const options = {
      method: method,
      headers: { "Content-Type": "application/json" },
      responseType: "json",
      timeout: 120000,
      // Zotero 7+ 的 HTTP 封装
      ...(body ? { body: JSON.stringify(body) } : {}),
    };
    // 记下最近几次请求的结果 —— catch 里只 Zotero.debug 的话，
    // 用户和我都看不到失败原因（本机踩过：权重缓存一直是 0，
    // 但没有任何地方说明为什么）。写进状态文件才查得动。
    var self = ZoteroKB;
    const note = (ok, detail) => {
      try {
        self.lastRequests = self.lastRequests || [];
        self.lastRequests.push({
          at: new Date().toISOString().slice(11, 19),
          method: method, path: path, ok: ok,
          detail: String(detail == null ? "" : detail).slice(0, 400),
        });
        if (self.lastRequests.length > 12) self.lastRequests.shift();
        if (!ok) self.lastRequestError = method + " " + path + " → " + detail;
      } catch (e) { /* ignore */ }
    };
    let result;
    try {
      if (Zotero.HTTP && Zotero.HTTP.request) {
        const xhr = await Zotero.HTTP.request(method, url, options);
        result = typeof xhr.response === "string"
          ? JSON.parse(xhr.response || "{}")
          : xhr.response;
        note(true, "HTTP ok, responseType=" + typeof xhr.response);
        return result;
      }
      // 老式 XHR 兜底
      result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open(method, url, true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.timeout = 120000;
        xhr.onload = () => {
          try { resolve(JSON.parse(xhr.responseText || "{}")); }
          catch (e) { reject(e); }
        };
        xhr.onerror = () => reject(new Error("请求失败"));
        xhr.ontimeout = () => reject(new Error("请求超时"));
        xhr.send(body ? JSON.stringify(body) : null);
      });
      note(true, "XHR fallback ok");
      return result;
    } catch (e) {
      note(false, (e && e.message) ? e.message : String(e));
      throw e;
    }
  },


  /**
   * 某几条文献在知识库里的状态（`{items:[…]}`）—— **两个调用方共用这一处**：
   *   · `06-watch.js`：监听新条目，判"这条建过库没有"（看 `in_kb` / `n_chunks`）；
   *   · `12-dsh.js`：把文献发给 DSH 时，带上权重/分类/正文字数。
   *
   * W3 线2.5（2026-10-13）：**优先走宿主半的引擎桥**（`kb.itemInfo`），拿不到再退回
   * 老 HTTP —— 过渡期两条都必须能用。桥不需要那个常驻 8765，也不需要 token。
   *
   * 为什么把两条通道收在这一个函数里：以前是两个调用点各写一遍
   * `request("POST", "/item-info", …)`，迁桥时若各改各的，很容易只改一处
   * （另一处继续走 HTTP 而没人发现）。
   *
   * @returns Promise<{items:Array}|null> —— 两条通道都失败时返回 null
   *          （调用方按"拿不到信息"处理，绝不抛）。
   */
  kbItemInfo: async function (keys) {
    var self = ZoteroKB;
    const ks = Array.isArray(keys) ? keys.slice() : [keys];
    const want = ks.filter((k) => k).map(String);
    if (!want.length) return null;
    try {
      const r = await self.kbAdminHostRoute("POST", "/item-info", { keys: want });
      if (r && r.items) return r;
      Zotero.debug("[zotero-kb] 引擎桥的 item-info 没给 items（退回 HTTP）");
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥取条目信息失败（退回 HTTP）：" + e);
    }
    try {
      return await self.request("POST", "/item-info", { keys: want });
    } catch (e) {
      Zotero.debug("[zotero-kb] 取条目信息失败：" + e);
      return null;
    }
  },


  /**
   * 查一条长任务的状态（`{state, log, result, error}`）。
   *
   * W3 线2.5（2026-10-14）：桥优先（`jobs.get`，与 HTTP `GET /jobs/<id>` **同形** ——
   * 两边都走 `LS.do_jobs_get`），拿不到再退回老 HTTP；**两条都失败返回 null**
   * （调用方按"查不到"处理，绝不抛）。
   *
   * ⚠ 收成一个共用函数的原因：`/jobs/<id>` 原来是好几处各写一遍 `request("GET", …)`，
   *   迁桥时很容易只改一处（另一处继续走 HTTP 而没人发现）。
   */
  kbJobGet: async function (jobId) {
    var self = ZoteroKB;
    const id = String(jobId || "").trim();
    if (!id) return null;
    try {
      const j = await self.kbAdminHostRoute("GET", "/jobs/" + encodeURIComponent(id));
      if (j) return j;
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥查任务失败（退回 HTTP）：" + e);
    }
    try {
      return await self.request("GET", "/jobs/" + encodeURIComponent(id));
    } catch (e) {
      Zotero.debug("[zotero-kb] 查任务失败：" + e);
      return null;
    }
  },


  /**
   * 插件/引擎是否活着（问引擎桥的 `kb.health`，**不碰 8765**）。
   *
   * 判据是引擎桥给的 `kb.health`：它给 kb_dir / project_root / python / ollama，
   * 够定位知识库与运行环境（stdio 通道没有"浏览器偷 token"这回事，所以它本来
   * 就不含 token）。
   *
   * ⚠ 2026-10-16：**删掉了"老 HTTP `/health` 兜底"**。那段兜底打的是
   *   127.0.0.1:8765 —— 服务早就退役了，所以它每次启动/每次体检**必然失败**，
   *   唯一的产出就是 `plugin-status.json` 里的 `lastRequestError`（用户看到的
   *   "HTTP 噪音"）。现在判定只有一条：**桥给到 info = 在线，桥拿不到 = 离线**。
   *   非要把老服务起起来的人另有入口（`scripts\4-service.vbs`），但插件不再探它。
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
      // 桥也拿不到 → 离线。**不**再去问老 HTTP（8765 已退役，问了只会失败）。
      this.serverOk = false;
      this.serverInfo = null;
      Zotero.debug("[zotero-kb] 引擎桥不可用：判为离线");
    }
    return this.serverOk;
  },
});
