/**
 * 05-server.js —— 与本地服务通信：`baseURL` / `request` / `healthCheck`
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 与服务通信

  baseURL: function () {
    return this.getPref(this.PREFS.server, "http://127.0.0.1:8765").replace(/\/+$/, "");
  },


  request: async function (method, path, body) {
    const url = this.baseURL() + path;
    const token = this.getPref(this.PREFS.token, "");
    const options = {
      method: method,
      headers: { "Content-Type": "application/json" },
      responseType: "json",
      timeout: 120000,
      // Zotero 7+ 的 HTTP 封装
      ...(body ? { body: JSON.stringify(body) } : {}),
    };
    if (token) {
      options.headers["X-KB-Token"] = token;
    }
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
        if (token) xhr.setRequestHeader("X-KB-Token", token);
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


  healthCheck: async function () {
    try {
      const info = await this.request("GET", "/health");
      this.serverOk = !!(info && info.ok);
      this.serverInfo = info;
      // 服务会把 token 一起返回（浏览器发起的请求会被服务端拒绝并说明原因），
      // 这里自动存下来 —— 用户就不用手工从文件里复制粘贴 token 了。
      // 本机踩坑：token 没填时 /health 照样通（它不需要 token），
      // 但 /weights、/task 全 401 → 权重列空白、任务队列无人领取，
      // 而 serverOk 却是 true，看着像"连上了"。
      if (info && info.token) {
        const cur = this.getPref(this.PREFS.token, "");
        if (cur !== info.token) {
          try {
            Zotero.Prefs.set(this.PREFS.token, info.token);
            Zotero.debug("[zotero-kb] 已自动写入服务 token");
          } catch (e) { Zotero.debug("[zotero-kb] 写 token 失败：" + e); }
        }
      } else if (info && info.token_withheld) {
        Zotero.debug("[zotero-kb] 服务没返回 token：" + info.token_withheld);
      }
      // 顺便记住服务端报的路径（知识库位置 / 项目根 / Python / Ollama）。
      // 这样插件不用写死任何路径：知识库搬哪、项目放哪，插件就跟到哪。
      // ⚠ 走 rememberServerInfo：它写 `server*` 系列 pref，**不碰**用户
      //   在设置面板里填的那几个键（老代码写同一个键，会把用户填的顶掉）。
      this.rememberServerInfo(info);
      this.serverKbDir = (info && info.kb_dir) || "";
      this.serverProjectRoot = (info && info.project_root) || "";
      Zotero.debug("[zotero-kb] 服务在线：" + JSON.stringify(info));
    } catch (e) {
      this.serverOk = false;
      this.serverInfo = null;
      Zotero.debug("[zotero-kb] 服务不在线：" + e);
    }
    return this.serverOk;
  },
});
