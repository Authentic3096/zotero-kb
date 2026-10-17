/**
 * 03-processes.js —— Ollama 进程的起停（带 flag 文件，不抢别人的进程）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {
  // ================================================================ Ollama 生命周期

  /**
   * Ollama 的"谁启动、谁负责关"标记文件。
   *
   * 为什么要独立文件：Zotero 插件和 DSH 插件都想管 Ollama。如果共用一个标记，
   * 一边关闭就会把另一边启动的也关掉（比如 DSH 退出时把 Zotero 正在用的关了）。
   * 各记各的，谁启动谁负责收尾，互不干扰。
   *
   * 为什么不用 prefs：prefs 是持久化的，Zotero 崩溃时来不及清标记，
   * 下次启动就会"误以为是自己启动的"而去关一个不是自己开的 Ollama。
   * 每次开机/每次启动都重判 + 用文件标记，能自然自愈。
   */


  /** Ollama 的 API 是否响应（比看进程更可靠：能确认服务真的可用）。 */
  ollamaUp: async function () {
    try {
      const r = await Zotero.HTTP.request(
        "GET", "http://127.0.0.1:11434/api/tags", { timeout: 2500,
                                                   responseType: "text" });
      return !!(r && r.status === 200);
    } catch (e) {
      return false;
    }
  },


  /** 启动 Ollama（先试托盘应用，再试 serve）。 */
  ollamaStart: function () {
    const op = self.ollamaPaths();
    const app = op.app;
    const exe = op.exe;
    const launch = (path, args) => {
      try {
        const { Subprocess } = ChromeUtils.importESModule(
          "resource://gre/modules/Subprocess.sys.mjs");
        Subprocess.call({ command: path, arguments: args || [],
                          stderr: "ignore", stdout: "ignore" })
          .catch(() => {});
        return true;
      } catch (e) {
        // 退回 nsIProcess
        try {
          const nsLocalFile = Components.Constructor(
            "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
          const proc = Components.classes["@mozilla.org/process/util;1"]
            .createInstance(Components.interfaces.nsIProcess);
          proc.init(nsLocalFile(path));
          const a = args || [];
          proc.runw(false, a, a.length);
          return true;
        } catch (e2) {
          Zotero.debug("[zotero-kb] 启动 Ollama 失败：" + e2);
          return false;
        }
      }
    };
    const f = Zotero.File.pathToFile(app);
    if (f.exists()) return launch(app, []);
    const f2 = Zotero.File.pathToFile(exe);
    if (f2.exists()) return launch(exe, ["serve"]);
    Zotero.debug("[zotero-kb] 找不到 ollama：" + app);
    return false;
  },


  /**
   * Zotero 启动时：确保 Ollama 可用。
   * 已经在跑 → 不碰它、不留标记（关 Zotero 时也不动它）。
   * 没在跑   → 拉起来 + 留标记（关 Zotero 时负责关掉）。
   */
  ollamaOnStartup: async function () {
    var self = ZoteroKB;
    try {
      // 先把上次可能残留的标记清掉，避免误判
      try {
        const fp = self.ollamaFlagPath();
        if (await IOUtils.exists(fp)) await IOUtils.remove(fp);
      } catch (e) { /* ignore */ }

      if (await self.ollamaUp()) {
        self.ollamaStartedByMe = false;
        Zotero.debug("[zotero-kb] Ollama 已在运行，不动它（关闭 Zotero 时也不会关它）");
      } else {
        Zotero.debug("[zotero-kb] Ollama 没在跑，由插件拉起");
        self.ollamaStartedByMe = self.ollamaStart();
        if (self.ollamaStartedByMe) {
          // 留标记，shutdown 时据此决定是否关闭
          try {
            await IOUtils.writeUTF8(self.ollamaFlagPath(),
              JSON.stringify({ by: "zotero", at: new Date().toISOString() }));
          } catch (e) { Zotero.debug("[zotero-kb] 写 ollama 标记失败：" + e); }
          // 等它就绪（知识库模型推理要用）
          for (let i = 0; i < 20; i++) {
            await new Promise((r) => setTimeout(r, 500));
            if (await self.ollamaUp()) break;
          }
          Zotero.debug("[zotero-kb] Ollama 就绪：" + (await self.ollamaUp()));
        }
      }
      self.writeStatusFile({ ollamaStartedByMe: !!self.ollamaStartedByMe });
    } catch (e) {
      Zotero.debug("[zotero-kb] Ollama 处理失败：" + e);
    }
  },


  /**
   * Zotero 关闭时：**只关自己启动的那个**。
   * 标记文件在 → 是我拉起来的 → 关掉；标记不在 → 本来就在跑 → 不碰。
   */
  ollamaOnShutdown: function () {
    var self = ZoteroKB;
    try {
      const fp = Zotero.File.pathToFile(self.ollamaFlagPath());
      if (!fp.exists()) return;             // 不是我启动的，绝不动它
    } catch (e) {
      return;
    }
    const kill = (path, args) => {
      try {
        const { Subprocess } = ChromeUtils.importESModule(
          "resource://gre/modules/Subprocess.sys.mjs");
        Subprocess.call({ command: path, arguments: args,
                          stderr: "ignore", stdout: "ignore" })
          .catch(() => {});
        return true;
      } catch (e) {
        try {
          const nsLocalFile = Components.Constructor(
            "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
          const proc = Components.classes["@mozilla.org/process/util;1"]
            .createInstance(Components.interfaces.nsIProcess);
          proc.init(nsLocalFile(path));
          proc.runw(false, args, args.length);
          return true;
        } catch (e2) { return false; }
      }
    };
    // 用 taskkill 结束我们自己启动的那两个进程。
    // 注意：这是"仅当标记存在"才做 —— 用户手动开的 Ollama 没有标记，不会被误关。
    //
    // ⚠ 不要写死 C:\Windows\System32\...（Windows 可能装在别的盘）。
    //   向系统问路径；万一拿不到，退回裸命令名 taskkill.exe
    //   —— 它在 PATH 上（System32 默认在 PATH 里）。
    let taskkill = "taskkill.exe";
    try {
      const sysDir = Services.dirsvc.get("SysD",
                                         Components.interfaces.nsIFile).path;
      taskkill = sysDir + "\\taskkill.exe";
    } catch (e) { /* 用裸命令名 */ }
    kill(taskkill, ["/IM", "ollama app.exe", "/F", "/T"]);
    kill(taskkill, ["/IM", "ollama.exe", "/F", "/T"]);
    try {
      const fp2 = Zotero.File.pathToFile(self.ollamaFlagPath());
      if (fp2.exists()) fp2.remove(false);
    } catch (e) { /* ignore */ }
    Zotero.debug("[zotero-kb] 已关闭由本插件启动的 Ollama");
  },
});
