/* eslint-disable no-undef */
/**
 * Zotero 文献知识库插件（bootstrapped extension，Zotero 7+ 与 10 通用）。
 *
 * 它做四件事：
 *   1. 新条目保存时，自动把它送进本地知识库（切片 + 索引 + 向量）；
 *   2. 用本地模型给这篇文献推荐**一个**分类和几个标签（每篇只归一类）；
 *   3. 弹窗让用户**一键应用**（分类、标签直接写回 Zotero）；
 *   4. 设置面板里能配本地服务地址、token、模型 —— 便于分享复用。
 *
 * 为什么不用 Zotero 的"本地 API"：那需要用户在 设置→高级 里勾选允许通信，
 * 而且 Zotero 10 还要额外授权拿 key。插件跑在 Zotero 进程内，能**直接**
 * 用 Zotero API 读写库，少一层依赖、也更稳（升级到 10 后本地 API 一度 403，
 * 但插件这条路不受影响）。
 *
 * 与本地服务的分工：
 *   插件只负责"Zotero 侧"的监听与写入；所有解析、切片、模型推理都在本地
 *   Python 服务（online/localserver.py，端口 8765）里做 —— 因为 PyMuPDF、
 *   嵌入模型、Ollama 都在那边，插件里重写一遍不现实。
 */


/**
 * 00-core.js —— 插件对象与启动：运行时状态字段、`startup`
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
var ZoteroKB = {
  id: null,
  version: null,
  rootURI: null,
  alive: false,

  // 运行时状态
  serverOk: false,
  serverInfo: null,
  notifyIDs: [],
  prefObserver: null,
  pending: {},          // itemID -> true，避免同一条重复处理
  weightsCache: {},     // item.key -> {weight, pinned, attempts, ...}（权重列用）
  weightColumnKey: null,// ItemTreeManager 返回的列 dataKey（注销时要）
  taskTimer: null,      // 任务轮询的 setTimeout 句柄
  taskPolling: false,   // 轮询开关（比 taskTimer 更能表达"是否在轮询"）
  taskBusy: false,      // 防止上一轮还没跑完就再来一轮
  tickCount: 0,         // 轮询心跳：触发次数（排查"定时器没跑"用）
  lastTickAt: "",       // 最后一次心跳时间
  timerDiag: null,      // 定时器可用性自诊断结果
  mainWindow: null,     // onMainWindowLoad 传进来的主窗口


  // ================================================================ 生命周期

  startup: function ({ id, version, rootURI } = {}) {
    // ⚠ 全程用 `self` 而不是 `this`。
    // 原因（实测踩坑）：Zotero 调 startup() 时 `this` 的绑定不一定是我们这个对象，
    // 一旦 `this.id = id` 抛错，就发生在"挂载 Zotero.ZoteroKB"之前 ——
    // 结果是插件看起来完全没加载（Zotero.ZoteroKB 是 undefined），
    // 而错误又被 catch 吞掉，极难排查。用闭包里的 ZoteroKB 引用最稳。
    var self = ZoteroKB;

    // 最先挂载：哪怕后面任何一步抛错，插件对象也已经可见，
    // 验证脚本至少能判断"插件活着"、并读到写下来的失败原因。
    try { Zotero.ZoteroKB = self; } catch (e) { /* 只读时忽略 */ }

    self.id = id;
    self.version = version;
    self.rootURI = rootURI;
    self.alive = true;

    // 分步记录：哪一步失败一目了然（以前整段一个 try，出错完全看不出来）
    const steps = [];
    const step = (name, fn) => {
      try {
        fn();
        steps.push(name + "=ok");
      } catch (e) {
        steps.push(name + "=FAIL(" + e + ")");
        throw new Error(name + " 失败: " + e);
      }
    };

    try {
      step("registerPrefs", () => self.registerPrefs());
      step("registerPrefObserver", () => self.registerPrefObserver());
      step("registerPrefPane", () => self.registerPrefPane());
      step("registerNotifier", () => self.registerNotifier());
      step("registerWeightColumn", () => self.registerWeightColumn());
      step("startTaskPolling", () => self.startTaskPolling());

      // ⚠ 工具栏按钮与右键菜单**必须在这里也注册一次**，不能只靠 onMainWindowLoad。
      // 实测（用任务队列在 Zotero 内查证）：
      //     windowHookCalled = 0     ← onMainWindowLoad 在启动时**从未被调用**
      //     manualCall = "ok"        ← 但手动调用立刻成功
      // 原因：`onMainWindowLoad` 只在插件启动**之后**新开的窗口才触发，
      // 而 Zotero 启动时主窗口已经存在了 —— 那个窗口不会走这个钩子。
      // 官方文档建议"UI 放窗口钩子"是针对多窗口场景，但没覆盖"启动时已存在的窗口"。
      // 所以：startup 里给已存在的窗口补一次，onMainWindowLoad 负责后续新窗口。
      step("scheduleWindowUI", () => {
        const applyUI = () => {
          try {
            const win = Zotero.getMainWindow && Zotero.getMainWindow();
            if (!win) return false;
            self.registerToolbarButton(win);
            self.registerItemMenu(win);
            return true;
          } catch (e) {
            Zotero.debug("[zotero-kb] 注册窗口 UI 失败：" + e);
            return false;
          }
        };
        // 立即试一次（多数情况主窗口已就绪）
        if (applyUI()) return;
        // 没就绪就重试几次（主窗口可能还在构建）
        let n = 0;
        const retry = () => {
          if (applyUI() || ++n >= 20) return;
          setTimeout(retry, 500);
        };
        setTimeout(retry, 500);
      });

      self.writeStatusFile({ startupSteps: steps.join(" "), startupOk: true });
      Zotero.debug("[zotero-kb] 已加载 v" + version + "  " + steps.join(" "));

      // 本地服务生命周期（**随用随取，随弃随关**）：
      //   已经在跑（用户手动开的 / 开机自启拉起的）→ 不碰它，关 Zotero 时也不关
      //   没在跑 → 插件拉起来 + 留标记，关 Zotero 时由插件关掉
      //
      // ⚠ 必须**排在 healthCheck/refreshWeights 之前**：那两个都要服务在跑
      //   才有意义（服务没起时 refreshWeights 只会失败一次，然后要等到
      //   下一个轮询周期才重试 —— 用户看到的就是"刚开 Zotero 权重列是空的"）。
      self.serverOnStartup()
        .then(() => self.healthCheck())
        .then(() => self.refreshWeights())
        .then(() => self.writeStatusFile({ startupSteps: steps.join(" "),
                                           startupOk: true }))
        .catch((e) => {
          try {
            self.writeStatusFile({ startupSteps: steps.join(" "),
                                   startupOk: true, asyncError: String(e) });
          } catch (e2) { /* ignore */ }
        });

      // Ollama 生命周期（异步，不阻塞插件启动）：
      //   已经在跑 → 不碰它（关 Zotero 时也不关）
      //   没在跑   → 拉起来 + 留标记（关 Zotero 时负责关掉）
      self.ollamaOnStartup().catch((e) => {
        Zotero.debug("[zotero-kb] ollamaOnStartup 失败：" + e);
      });
    } catch (e) {
      Zotero.logError(e);
      // 关键：把失败写进文件。Zotero 的调试日志重启后就没了，
      // 而用户看到的现象只是"插件不工作"，没有文件可查就无从下手。
      try {
        self.writeStatusFile({
          startupOk: false,
          startupSteps: steps.join(" "),
          startupError: String(e),
          stack: (e && e.stack) ? String(e.stack).slice(0, 800) : "",
        });
      } catch (e2) { /* 连写文件都失败，只能靠 debug 日志 */ }
    }
  },
};
