/**
 * 03-processes.js —— 本地服务与 Ollama 进程的起停（带 flag 文件，不抢别人的进程）
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


  /** 本地服务是否响应（问 /health，不需要 token）。 */
  serverUp: async function () {
    // ⚠ 这是一次**真连 8765** 的探测。它已**不是任何自动路径的一部分**：
    //   调用方只有 serverOnStartup（严格 opt-in，见那里），以及手动诊断。
    try {
      const r = await Zotero.HTTP.request(
        "GET", "http://127.0.0.1:8765/health",
        { timeout: 2500, responseType: "text" });
      return !!(r && r.status === 200);
    } catch (e) {
      return false;
    }
  },


  /** 拉起本地服务（不经控制台窗口）。 */
  serverStart: function () {
    var self = ZoteroKB;
    const py = self.pythonConsoleExe();
    const root = self.projectRoot();
    if (!py || !root) {
      Zotero.debug("[zotero-kb] 拉不起本地服务：找不到 Python 或项目目录");
      return false;
    }
    // 用 pythonw.exe（无窗口）—— 服务是后台常驻的，弹个黑框很吓人。
    const pyw = py.replace(/python\.exe$/i, "pythonw.exe");
    const exe = self._exists(pyw) ? pyw : py;
    const script = root + "\\online\\localserver.py";
    try {
      const { Subprocess } = ChromeUtils.importESModule(
        "resource://gre/modules/Subprocess.sys.mjs");
      Subprocess.call({ command: exe, arguments: ["-X", "utf8", script],
                        workdir: root, stderr: "ignore", stdout: "ignore" })
        .catch(() => {});
      return true;
    } catch (e) {
      try {
        const nsLocalFile = Components.Constructor(
          "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
        const proc = Components.classes["@mozilla.org/process/util;1"]
          .createInstance(Components.interfaces.nsIProcess);
        proc.init(nsLocalFile(exe));
        const a = ["-X", "utf8", script];
        proc.runw(false, a, a.length);
        return true;
      } catch (e2) {
        Zotero.debug("[zotero-kb] 启动本地服务失败：" + e2);
        return false;
      }
    }
  },


  /**
   * Zotero 启动时：**只在用户显式要求时才**拉起本地服务（严格 opt-in）。
   *
   * ⚠⚠ 2026-10-16（用户拍板"HTTP 噪音一起清"）：这一整组（serverUp /
   *   serverStart / serverOnStartup / serverOnShutdown）**保留**，但从"默认自动"
   *   改成"**只有显式置 `zotero-kb.autoStartService=true` 才走**"。
   *   为什么保留而不是整组删：它是一条**回退能力**（有人仍需旧 HTTP 通道时，
   *   手动入口是 `scripts\4-service.vbs`，或把这个 pref 设回 true）；
   *   为什么默认必须 false：8765 已退役、插件已全部走引擎桥/文件信箱，
   *   默认 true 会让**每次启动都去探/拉起一个没人用的服务**（就是"HTTP 噪音"）。
   *
   * 判据（pref 为 false 时的**唯一行为**）：立刻返回、**一个网络请求都不发**
   * —— 这条早退必须在任何 serverUp()/serverStart() 之前（现在就是）。
   *
   * 触发者清单（改这里前先看）：
   *   · `00-core.js` 启动流程 → 调本函数；
   *   · `01-lifecycle.js` shutdown → 调 `serverOnShutdown()`（它只在自己留过
   *     标记文件时才发 `/shutdown`，没启动过就纯本地检查、不发请求）；
   *   · 只有这两个，没有按钮、没有定时器。
   */
  serverOnStartup: async function () {
    var self = ZoteroKB;
    try {
      // W3 线3（2026-10-14）：自动拉起本地服务是一个开关。
      // ⚠ 2026-10-16 起**默认值改成 false**（原为 true）：不开这个开关时，
      //   启动流程**完全不碰 8765**。用户手动起服务用 scripts\4-service.vbs。
      if (!self.getPref("zotero-kb.autoStartService", false)) {
        Zotero.debug("[zotero-kb] 本地服务自动拉起未开启"
                     + "（pref zotero-kb.autoStartService 默认 false）"
                     + "—— 不碰 8765；要旧 HTTP 通道请手动跑 scripts\\4-service.vbs");
        self.serverStartedByMe = false;
        self.writeStatusFile({ serverStartedByMe: false, serverAutoStart: false });
        return;
      }
      // 清掉上次可能残留的标记，避免误判成"是我们拉起的"
      try {
        const fp = self.serverFlagPath();
        if (await IOUtils.exists(fp)) await IOUtils.remove(fp);
      } catch (e) { /* ignore */ }

      if (await self.serverUp()) {
        self.serverStartedByMe = false;
        Zotero.debug("[zotero-kb] 本地服务已在运行，不动它（关 Zotero 时也不关）");
      } else {
        Zotero.debug("[zotero-kb] 本地服务没在跑，由插件拉起");
        self.serverStartedByMe = self.serverStart();
        if (self.serverStartedByMe) {
          try {
            await IOUtils.writeUTF8(self.serverFlagPath(),
              JSON.stringify({ by: "zotero", at: new Date().toISOString() }));
          } catch (e) { Zotero.debug("[zotero-kb] 写服务标记失败：" + e); }
          // 等它就绪 —— 权重列、任务轮询都要用它
          for (let i = 0; i < 20; i++) {
            await new Promise((r) => setTimeout(r, 500));
            if (await self.serverUp()) break;
          }
          Zotero.debug("[zotero-kb] 本地服务就绪：" + (await self.serverUp()));
        }
      }
      self.writeStatusFile({ serverStartedByMe: !!self.serverStartedByMe });
    } catch (e) {
      Zotero.debug("[zotero-kb] 本地服务处理失败：" + e);
    }
  },


  /**
   * Zotero 关闭时：**只关自己启动的那个**（随弃随关）。
   *
   * ⚠ 2026-10-16：`autoStartService` 默认 false 之后，插件平时**不会**启动服务，
   *   也就不会留下标记文件 —— 所以这里通常**第一句就 return**（无网络请求）。
   *   只有当初真的走 serverOnStartup 拉起过（用户显式开了那个 pref）才会有标记。
   *
   * 关法是 `POST /shutdown` 让服务自己退出，**不是** taskkill ——
   * 服务跑在 pythonw.exe 里，而 pythonw 是极常见的进程名，
   * `taskkill /IM pythonw.exe` 会把管理面板、用户自己的 Python 脚本
   * 一起杀掉。这个坑不能踩。
   *
   * 同步实现：Zotero 关闭时留给 shutdown() 的时间有限，等不了 async。
   * 用同步的 XHR 把请求发出去（服务收到就置位退出），不等回复。
   */
  serverOnShutdown: function () {
    var self = ZoteroKB;
    try {
      const fp = Zotero.File.pathToFile(self.serverFlagPath());
      if (!fp.exists()) return;             // 不是我们启动的，绝不动它
    } catch (e) {
      return;
    }
    try {
      const url = "http://127.0.0.1:8765/shutdown";
      const req = new XMLHttpRequest();
      req.open("POST", url, false);         // false = 同步，关闭流程里必须这样
      req.setRequestHeader("Content-Type", "application/json");
      req.send("{}");
      Zotero.debug("[zotero-kb] 已请求本地服务退出，HTTP " + req.status);
    } catch (e) {
      Zotero.debug("[zotero-kb] 请求服务退出失败：" + e);
    }
    try {
      const fp2 = Zotero.File.pathToFile(self.serverFlagPath());
      if (fp2.exists()) fp2.remove(false);
    } catch (e) { /* ignore */ }
  },


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
