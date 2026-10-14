// ⚠ 本文件由 tools/build_bootstrap.py 从 zotero-plugin/src/*.js 生成，
//    不要直接改这里 —— 改 src/ 下的源文件，再跑：
//        python tools/build_bootstrap.py
//    改完没重新生成的话，打包前会被拒绝（tools/pack_plugin.py 会校验）。
//    源码共 26 个文件，清单在 build_bootstrap.py 的 SRC_ORDER。

// ===== src/00-core.js =====
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

  // 界面文案的 ftl 文件名（locale/<语言>/<这个名字>）。
  // ⚠ Zotero 会把插件 `locale/<语言>/*.ftl` 读进 L10nRegistry 的
  //   `zotero-plugins` 源，但**文档还得自己把它挂上** —— 少了 initLocale 那一步，
  //   `data-l10n-id` 谁都不认识，界面上的表现就是"按钮全是空框"
  //   （2026-10-05 用户截图报的正是这个）。
  FTL_FILE: "zotero-kb.ftl",

  // 运行时状态
  serverOk: false,
  serverInfo: null,
  notifyIDs: [],
  prefObserver: null,
  pending: {},          // itemID -> true，避免同一条重复处理
  weightsCache: {},     // item.key -> {weight, pinned, attempts, ...}（权重列用）
  weightColumnKey: null,// ItemTreeManager 返回的列 dataKey（注销时要）
  kbColKeys: null,      // 「已建知识库」「分节纲要」两列的 dataKey（注销时要）
  colStatusCache: {},   // item.key -> {in_kb, outline_sections}（21-kbcols.js 用）
  colStatusReady: false,// 这份缓存是不是**完整**的一份 —— 只有完整，
                        //    "映射里查不到"才能解释成"这篇还没建库"（见 refreshKbCols）
  colStatusSig: "",     // 上次列状态的内容指纹（没变就不重画列表）
  digestActive: null,   // 正在跑的「生成分节纲要」那一批（见 14-menus.js）
  lastDigest: null,     // 上一批纲要生成的结果摘要（写进状态文件供排查）
  taskTimer: null,      // 任务轮询的 setTimeout 句柄
  _statusExtra: {},     // 状态文件的"累积字段"（见 writeStatusFile 的说明）
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
    //
    // 2026-10-13：每一步**同时记毫秒**（用户报「启动时插件加载有点慢」）。
    // 不记数字就只能猜 —— 这份 timing 每个阶段都会写进 plugin-status.json，
    // 下次启动完直接读它就知道慢在哪个 step（见 tools/read_startup_timing.py）。
    // ⚠ 只读不写进 Zotero 自己的日志：那里的东西重启就没了。
    const steps = [];
    const tBoot = Date.now();
    const timing = {};
    const step = (name, fn) => {
      const a = Date.now();
      try {
        fn();
        timing[name] = Date.now() - a;
        steps.push(name + "=ok");
      } catch (e) {
        timing[name] = Date.now() - a;
        steps.push(name + "=FAIL(" + e + ")");
        throw new Error(name + " 失败: " + e);
      }
    };

    try {
      // ⚠ 第一件事就是把 ftl 挂到窗口文档上（见 initLocale 的说明）——
      //   后面的设置面板/内容窗格分区都要靠它把 l10nID 变成文字。
      step("initLocale", () => self.initLocale());
      // 注册我们自己的 resource 包：自建列里的 <browser> 必须从 resource:// 加载
      // 才能在父进程里跑、宿主才拿得到 contentWindow（真机实测，见 KB_RES_KEY）。
      step("registerKbResource", () => self.registerKbResource());
      step("registerPrefs", () => self.registerPrefs());
      step("registerPrefPane", () => self.registerPrefPane());
      step("registerNotifier", () => self.registerNotifier());
      step("registerWeightColumn", () => self.registerWeightColumn());
      // 文献列表的另外两列（21-kbcols.js）：与权重列同一套做法 ——
      // 注册本身只是把列定义交出去，数据由 refreshKbCols 异步填。
      step("registerKbColumns", () => self.registerKbColumns());
      step("startTaskPolling", () => self.startTaskPolling());
      // MinerU（可选组件）的首次安装引导：**延迟 8 秒**跑，且只在"没装过 +
      // 没问过"时才弹一次（见 19-mineruguide.js）。放在这里是为了让它跟别的
      // 启动步骤一样有名字、失败也能在状态文件里看到（它自己不会抛）。
      step("scheduleOptionalGuides", () => self.scheduleOptionalGuides());
      // 能力探测：DSH 接上了没、本地模型可用不可用。右键菜单只读它的缓存
      // （菜单是同步构建的，等不了网络），所以这一步只负责"排上周期探测"。
      step("scheduleCapsRefresh", () => self.scheduleCapsRefresh());
      // 右侧栏「知识库」那一列（20-kbview.js）：**自建列**，不再是 registerSection。
      // 装不上（找不到 Zotero 的内部 DOM）时它会自己退回分区模式并把原因写进状态文件
      // —— 见 kbviewDegrade。真正的装载在下面的 scheduleWindowUI 里做（要有窗口）。
      step("scheduleKbViewColumn", () => {
        const applyCol = () => {
          try {
            const win = Zotero.getMainWindow && Zotero.getMainWindow();
            if (!win || !win.document) return false;
            const a = Date.now();
            self.kbviewMount(win);
            // 这一条最容易慢：它要建一个 <browser> 元素、里面那份页面还要
            // 加载 markdown-it + katex + 21 个字体（见 kbviewMountPane 里的 load 计时）
            timing.kbviewMount = Date.now() - a;
            return true;
          } catch (e) {
            Zotero.debug("[zotero-kb] 装知识库侧栏失败：" + e);
            return false;
          }
        };
        if (applyCol()) return;
        let n = 0;
        const retry = () => {
          if (applyCol() || ++n >= 20) return;
          setTimeout(retry, 500);
        };
        setTimeout(retry, 500);
      });
      // ⚠ 2026-10-05：这里原来还有三步 —— registerItemPane / registerReaderEvents
      //   / registerQuitGuard（内容窗格里的「本地模型」分区、阅读器选中入口、
      //   退出时提醒"对话不保存"）。用户判断那个窗格"没什么用而且 bug 多"，
      //   要求整条链删掉，于是 19-itempane.js / 20-reader.js 与这三个 step
      //   都不再存在。**别照着旧文档或旧提交把它们加回来。**

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
            const a = Date.now();
            self.registerToolbarButton(win);
            timing.registerToolbarButton = Date.now() - a;
            const b = Date.now();
            self.registerItemMenu(win);
            timing.registerItemMenu = Date.now() - b;
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

      timing.startupSyncMs = Date.now() - tBoot;
      self.writeStatusFile({ startupSteps: steps.join(" "), startupOk: true,
                             startupTiming: JSON.stringify(timing) });
      Zotero.debug("[zotero-kb] 已加载 v" + version + "  " + steps.join(" "));

      // 本地服务生命周期（**随用随取，随弃随关**）：
      //   已经在跑（用户手动开的 / 开机自启拉起的）→ 不碰它，关 Zotero 时也不关
      //   没在跑 → 插件拉起来 + 留标记，关 Zotero 时由插件关掉
      //
      // ⚠ 必须**排在 healthCheck/refreshWeights 之前**：那两个都要服务在跑
      //   才有意义（服务没起时 refreshWeights 只会失败一次，然后要等到
      //   下一个轮询周期才重试 —— 用户看到的就是"刚开 Zotero 权重列是空的"）。
      // 这三步都是网络/磁盘活，**不阻塞** Zotero 启动，但要计时 ——
      // 它们决定的是"多久之后权重列/两列才第一次有内容"。
      const timed = (name, fn) => () => {
        const a = Date.now();
        return Promise.resolve().then(fn).then(
          (r) => { timing[name] = Date.now() - a; return r; },
          (e) => { timing[name] = Date.now() - a; throw e; });
      };
      self.serverOnStartup()
        .then(timed("healthCheck", () => self.healthCheck()))
        .then(timed("refreshWeights", () => self.refreshWeights()))
        // 两列的数据同一次启动里也拉一份（不拉的话，要等轮询那 30 秒
        // 才会第一次出现内容 —— 用户看到的是"列是空的"）。
        .then(timed("refreshKbCols", () => self.refreshKbCols()))
        .then(() => self.writeStatusFile({
          startupSteps: steps.join(" "),
          startupOk: true,
          startupTiming: JSON.stringify(timing),
          startupAsyncMs: Date.now() - tBoot,
        }))
        .catch((e) => {
          try {
            self.writeStatusFile({ startupSteps: steps.join(" "),
                                   startupOk: true, asyncError: String(e),
                                   startupTiming: JSON.stringify(timing),
                                   startupAsyncMs: Date.now() - tBoot });
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

// ===== src/01-lifecycle.js =====
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
    try { self.unregisterKbColumns(); } catch (e) { /* ignore */ }
      // 自建列：**卸必须对称** —— 按钮、列、监听、定时器都要清干净，
    // 否则下次启用会出现两份（Zotero 会复用主窗口）。
    try { self.kbviewUnmountAll(); } catch (e) { /* ignore */ }
    // 只读降级模式留下的分区
    try { self.unregisterKbViewSection(); } catch (e) { /* ignore */ }
    // 我们注册的 resource 包：留着会让下次启用指向旧包
    try { self.unregisterKbResource(); } catch (e) { /* ignore */ }
    // ⚠ 2026-10-05：内容窗格分区（ItemPaneManager）、退出提醒（quit guard）、
    //   以及"对话不落盘所以清掉 chatState"这三件事随窗格一起删了。
    //   阅读器的监听本来也不需要手动摘（registerEventListener 传了 pluginID）。
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


  // ================================================================ 界面文案（ftl）

  /**
   * 把插件自带的 ftl 挂到窗口文档上。**这一步不能省。**
   *
   * Zotero 只做了一半：它把插件 `locale/<语言>/*.ftl` 读进 `L10nRegistry` 的
   * `zotero-plugins` 源（`plugins.js` 的 `registerLocales`，在 `startup` 之前跑），
   * 但**文档必须自己声明要用这个资源** —— 主窗口的 `linkset` 里只有
   * `zotero.ftl` / `reader.ftl` 那几条。少了这一步，我们设的
   * `data-l10n-id` 谁都不认识：元素**保持空白**（不报错、也不显示 id），
   * 表现就是"内容窗格里一排按钮全是空框"。
   *
   * 2026-10-05 实测（用户截图报的正是这个）：删了 `initLocale` 之后，
   * 分区能出现、状态行有字（那是代码里的中文字符串），但 **6 个按钮全空**、
   * 分区标题与输入框 placeholder 也空。
   *
   * 两个动作：
   *   ① `MozXULElement.insertFTLIfNeeded(文件名)` —— 往文档的 linkset 里加一条
   *      `<link rel="localization" href="zotero-kb.ftl">`（Firefox 的标准做法，
   *      本机 5 个能正常显示文案的插件都是这么干的）；拿不到 MozXULElement
   *      时**自己插那条 link**（那段代码就是它的实现，只是多一层保险）。
   *   ② `Zotero.ftl.addResourceIds([文件名])` —— 让**程序化**取字符串也行
   *      （`l10nText()` 给 confirmEx 弹窗用；那是同步 API，只能走这条路）。
   *
   * 不抛错：文案是装饰，缺了也不该让插件启动失败（只是界面难看）。
   */
  initLocale: function (win) {
    var self = ZoteroKB;
    const file = self.FTL_FILE;
    const result = { file: file, steps: [] };
    try {
      // ② 程序化取字符串（弹窗用）
      try {
        if (Zotero.ftl && Zotero.ftl.addResourceIds) {
          Zotero.ftl.addResourceIds([file]);
          result.steps.push("ftl.addResourceIds=ok");
        }
      } catch (e) {
        result.steps.push("ftl.addResourceIds=FAIL(" + e + ")");
      }

      const w = win || (Zotero.getMainWindow && Zotero.getMainWindow());
      if (!w || !w.document) {
        result.steps.push("no-window");
        self.writeStatusFile({ locale: JSON.stringify(result) });
        return result;
      }
      const doc = w.document;
      // ① 文档级：让 data-l10n-id 真的被翻译
      try {
        if (w.MozXULElement && w.MozXULElement.insertFTLIfNeeded) {
          w.MozXULElement.insertFTLIfNeeded(file);
          result.steps.push("insertFTLIfNeeded=ok");
        } else {
          result.steps.push("insertFTLIfNeeded=missing");
        }
      } catch (e) {
        result.steps.push("insertFTLIfNeeded=FAIL(" + e + ")");
      }
      // 兜底：自己插那条 link（与 insertFTLIfNeeded 的实现一致，幂等）
      try {
        const container = doc.head || doc.querySelector("linkset");
        if (container) {
          let have = false;
          for (const l of container.querySelectorAll("link")) {
            if (l.getAttribute("href") === file) { have = true; break; }
          }
          if (!have) {
            const link = doc.createElementNS(
              "http://www.w3.org/1999/xhtml", "link");
            link.setAttribute("rel", "localization");
            link.setAttribute("href", file);
            container.appendChild(link);
            result.steps.push("manual-link=ok");
          } else {
            result.steps.push("manual-link=already");
          }
        } else {
          result.steps.push("manual-link=no-container");
        }
      } catch (e) {
        result.steps.push("manual-link=FAIL(" + e + ")");
      }
      self.writeStatusFile({ locale: JSON.stringify(result) });
    } catch (e) {
      Zotero.debug("[zotero-kb] initLocale 失败：" + e);
    }
    return result;
  },

  // ================================================================ 首选项

  PREFS: {
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
    // ---- MinerU（可选组件）的首次安装引导
    //
    // 「首次启动且没检测到 MinerU 时弹一次，之后不再打扰」—— 用户定的规矩。
    // 所以**无论用户选「打开安装引导」还是「以后再说」都会把它置为 true**；
    // 装了 MinerU 的用户一次都不弹（这个 pref 也不会被写）。
    // 面板那侧还有入口：「知识库结构」页那个只在未安装时出现的按钮。
    mineruGuideDone: "zotero-kb.mineruGuideDone",
    // 可选组件（MinerU + Ollama）的安装引导是否已经问过。
    // ⚠ 旧版本只有 mineruGuideDone；升级上来的用户那条 pref 还是 true，
    //   所以 19-mineruguide.js 里**两个都认**，不会二次打扰。
    optionalGuideDone: "zotero-kb.optionalGuideDone",
    // 右侧栏「知识库」那一列显示哪一级（2026-10-05 新增，见 20-kbview.js）。
    // 默认「分节纲要」—— 那一层就是为"对着 PDF 读"设计的（每节带页码范围）。
    kbviewLevel: "zotero-kb.kbviewLevel",
    // 那一列的字号（px）。**只作用在我们这一列**，不动 Zotero 的任何默认设置。
    //
    // ⚠ 这里原来还有一个 `kbviewWidth`（窄/标准/宽档位），2026-10-11 删了：
    //   用户要的是"覆盖在原侧边栏上、同宽窄能拉动"，也就是**宽度归原生 splitter**，
    //   我们不去改窗格宽度。见 20-kbview.js 里那段注释。
    kbviewFont: "zotero-kb.kbviewFont",
    // ⚠ 2026-10-05：`chatQuitWarn` / `chatNumCtx` 两个 pref 随内容窗格聊天
    //   一起删了（见 00-core.js 里那段说明）。prefs.js 里的默认值也一并删了。
  },
});

// ===== src/02-paths.js =====
/**
 * 02-paths.js —— 路径解析：知识库、项目根、Python、Ollama、桥接信箱都在哪
 * 一律**不写死**：能问服务的就问服务，能探测的就探测
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {
  // ================================================================ 路径解析

  /**
   * 知识库目录：优先用服务端告诉我们的（/health 里带 kb_dir），
   * 其次用上次缓存到 pref 的值，最后才用默认值。
   *
   * 为什么要缓存：插件在服务没起来时也要能工作（比如写启动日志），
   * 那时拿不到 /health，就用上次记住的。
   *
   * 这样别人装的时候**不用改插件源码** —— 服务端在哪，插件就知道在哪。
   */
  kbDir: function () {
    var self = ZoteroKB;
    try {
      const cached = Zotero.Prefs.get("zotero-kb.kbDir");
      if (cached && String(cached).trim()) return String(cached).trim();
    } catch (e) { /* ignore */ }
    // ⚠ 这里**故意不写死绝对路径**（老版本写的是开发机的
    //   D:\DSHplugins\zotero-kb\kb，对别人毫无意义，还会误导排错）。
    //   知识库位置只有两个正当来源：服务端 /health 的 kb_dir，
    //   或用户在设置面板里填的。都没有时返回空，让上层报
    //   "服务没连上，请先在设置里填位置" —— 比指到一个不存在的目录好。
    return "";
  },


  /**
   * 记住服务端告诉我们的运行环境（healthCheck 时调用）。
   *
   * ⚠ 存到**独立于用户设置**的 pref 键里：
   *   `zotero-kb.projectRoot`     = 用户在设置面板里填的（权威）
   *   `zotero-kb.serverProjectRoot` = 服务端报的（兜底）
   *   老版本把两者写进同一个键，结果是"服务端一报就把用户填的覆盖了"。
   */
  rememberServerInfo: function (info) {
    if (!info) return;
    const map = [["serverProjectRoot", info.project_root],
                 ["serverKbDir", info.kb_dir],
                 ["serverPython", info.python],
                 ["serverOllama", info.ollama]];
    for (const [pref, val] of map) {
      if (!val) continue;
      try {
        const key = "zotero-kb." + pref;
        if (Zotero.Prefs.get(key) !== val) {
          Zotero.Prefs.set(key, String(val));
        }
      } catch (e) { /* ignore */ }
    }
    if (info.kb_dir) this.rememberKbDir(info.kb_dir);
  },


  /**
   * 弹「选择文件夹」对话框。返回 Promise<string>（取消则空串）。
   *
   * 为什么不用 Zotero.FilePicker：它是 Zotero 内部模块（filePicker.mjs），
   * 没有挂在 Zotero.* 命名空间下，从插件沙箱里拿不到稳定引用。
   * 直接建 nsIFilePicker 更可靠 —— Zotero 自己的 FilePicker 内部就是
   * `Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker)`。
   * 注意沙箱里没有 `Ci`，要用完整的 Components.interfaces。
   */
  pickFolder: function (title) {
    var self = ZoteroKB;
    return new Promise(function (resolve) {
      try {
        const fp = Components.classes["@mozilla.org/filepicker;1"]
          .createInstance(Components.interfaces.nsIFilePicker);
        // ⚠ 第一个参数要的是 **BrowsingContext**，不是 window。
        //   Zotero 自己的 filePicker.mjs 也是这么传的：
        //     this._fp.init(parentWindow.browsingContext, title, mode)
        //   沙箱里没有 `Ci`，要用完整的 Components.interfaces。
        let bc = null;
        try {
          const win = Zotero.getMainWindow && Zotero.getMainWindow();
          bc = (win && win.browsingContext) || null;
        } catch (e) { /* 拿不到就传 null，picker 会自己找父窗口 */ }
        fp.init(bc, title || "选择文件夹",
                Components.interfaces.nsIFilePicker.modeGetFolder);
        fp.open(function (rv) {
          try {
            if (rv !== Components.interfaces.nsIFilePicker.returnOK || !fp.file) {
              resolve("");
              return;
            }
            resolve(String(fp.file.path || ""));
          } catch (e) {
            resolve("");
          }
        });
      } catch (e) {
        Zotero.debug("[zotero-kb] 选文件夹失败：" + e);
        resolve("");
      }
    });
  },


  /** 弹「选择文件」对话框（选 python.exe / ollama.exe 用）。 */
  pickFile: function (title, filterTitle, filterExt) {
    return new Promise(function (resolve) {
      try {
        const fp = Components.classes["@mozilla.org/filepicker;1"]
          .createInstance(Components.interfaces.nsIFilePicker);
        let bc = null;
        try {
          const win = Zotero.getMainWindow && Zotero.getMainWindow();
          bc = (win && win.browsingContext) || null;
        } catch (e) { /* ignore */ }
        fp.init(bc, title || "选择文件",
                Components.interfaces.nsIFilePicker.modeOpen);
        if (filterExt) {
          fp.appendFilter(filterTitle || "程序", filterExt);
        }
        fp.open(function (rv) {
          try {
            if (rv !== Components.interfaces.nsIFilePicker.returnOK || !fp.file) {
              resolve("");
              return;
            }
            resolve(String(fp.file.path || ""));
          } catch (e) {
            resolve("");
          }
        });
      } catch (e) {
        Zotero.debug("[zotero-kb] 选文件失败：" + e);
        resolve("");
      }
    });
  },


  /**
   * 把运行环境配置推给服务端，让它落盘到 <项目>\kb-location.json。
   *
   * 为什么必须推：Zotero 的 pref 只有插件自己看得到，而**管理面板是独立的
   * Python 进程**，它得知道项目在哪、Python 在哪。服务端落盘后
   * schemas.resolve_* 就能读到，面板启动时自然用对。
   *
   * ⚠ **W3 线2.5（2026-10-13）：这一处故意没迁到引擎桥，因为入参/回包形状都对不上**
   *   （真机取证见开发说明）：
   *     · 老 HTTP `POST /env-config` 收的是**嵌套** `{env:{project_root,…}}`，
   *       回包带 **`report`**；
   *     · 引擎 `env.save` 收的是**扁平**键（`project_root/python/ollama/mineru/figures`），
   *       回包是 `{saved, check}`、**没有 `report`**。
   *   实测：把插件现在这个 `{env:{…}}` 体直接过桥 → 引擎回
   *   `没有要写入的键（project_root / python / ollama / mineru / figures）`。
   *   要迁得先做两件事：① 这里把 `env` **摊平**再发；② 让 `env.save` 的返回里带上
   *   `report`（或让设置页接受"另发一次 env-check"）。②动契约，所以排在收工后做。
   *
   * 2026-10-14：两件事都已在引擎侧补齐（`env.save` 回包带 **`report`**、
   * 新增 **`env.checkRaw`**），所以这里改成**桥优先 → 退回老 HTTP**。
   * **入参必须把 `env` 摊平一层**（老 HTTP 收嵌套 `{env:{…}}`、引擎收扁平
   * `{project_root,…}`）—— 照抄老写法过桥会当场报「没有要写入的键」，
   * 这是本处最容易写错的地方（上一轮实测踩过）。
   */
  kbPushEnvConfig: async function (env) {
    var self = ZoteroKB;
    let r = null;
    try {
      // ⚠ 摊平：`Object.assign({}, env || {})` —— 不是 `{ env: env }`
      r = await self.kbAdminHostRoute("POST", "/env-config",
                                      Object.assign({}, env || {}));
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥写环境配置失败（退回 HTTP）：" + e);
    }
    if (r && r.saved === true) return { ok: true, report: r.report || null };
    try {
      const r2 = await self.request("POST", "/env-config", { env: env });
      return { ok: true, report: (r2 && r2.report) || null };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },


  /**
   * 从服务端取运行环境诊断（设置面板「检测」按钮用）。
   *
   * ⚠ **W3 线2.5（2026-10-13）：这一处也故意没迁** —— 两条通道回的**不是同一个形状**：
   *   · 老 HTTP `GET /env-check` = `env_report()` 原文，含 **`python` / `project_root` /
   *     `ollama` / `runtime_state` / `user_location_file`**；`settings.js` 的
   *     `renderReport()` 读的正是 `r.python.runnable` 与 `r.project_root.has_panel`。
   *   · 引擎 `env.check` 是给**管理面板**重新整形过的：`{ok, items[], sources,
   *     rejected[], env_file, location_file}` —— **没有 `python` / `project_root`
   *     这两个顶层键**（实测 `bridgeHas_consumerFields: false`）。
   *   直接改过去会让设置面板的「检测」报告变空/报错。要迁得先让引擎**多回一份原文**
   *   （新增 `env.checkRaw`，或给 `env.check` 加一个 `raw` 字段）—— 那是扩契约，
   *   按纪律排在计费切换点之后做。
   *
   * 2026-10-14：引擎已新增 **`env.checkRaw`**，所以这里改成**桥优先 → 退回老 HTTP** ——
   * 两条通道回的是**同一个形状**（都是 `env_report()` 原文）。
   * ⚠ 别图省事改成 `env.check`：那个给管理面板用、**没有** `python`/`project_root` 两个顶层键。
   */
  kbEnvReport: async function () {
    var self = ZoteroKB;
    try {
      const r = await self.kbAdminHostRoute("GET", "/env-check-raw");
      if (r && (r.python || r.project_root)) return r;   // 原文形状的自检
      Zotero.debug("[zotero-kb] 引擎桥的诊断原文形状不对（退回 HTTP）");
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥取诊断失败（退回 HTTP）：" + e);
    }
    try {
      return await self.request("GET", "/env-check");
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },


  /** 记住服务端给的知识库位置（healthCheck 时调用）。 */
  rememberKbDir: function (dir) {
    if (!dir) return;
    try {
      const cur = Zotero.Prefs.get("zotero-kb.kbDir");
      if (cur !== dir) {
        Zotero.Prefs.set("zotero-kb.kbDir", String(dir));
        Zotero.debug("[zotero-kb] 知识库位置已记住：" + dir);
      }
    } catch (e) { /* ignore */ }
  },


  /**
   * @deprecated 保留仅为兼容旧调用点。
   * 服务端报的项目根**不再**写进用户设置键（那样会覆盖用户填的值），
   * 现在存到 `serverProjectRoot`，见 rememberServerInfo()。
   */
  rememberProjectRoot: function () {
    // 故意不写任何东西：写用户键会让「用户在设置里填的」被服务端的值顶掉。
  },


  /**
   * 项目根目录（放 .venv / offline / online 的地方）。
   *
   * ⚠ **不能从 kbDir 反推**。知识库默认跟着 Zotero 数据目录走，而代码/venv
   *   还在项目目录里 —— 本机就是反推了一把，结果算出
   *   `D:\Application\ZoteroData\Zotero`，那里没有 .venv，
   *   于是工具栏按钮报"找不到 Python 环境"。
   *   结论：数据位置和代码位置是两个独立的东西，谁也别推谁。
   *
   * 顺序：用户设置 > 服务端 /health > 向上探测标记 > 环境变量 > 空。
   */
  /** 同步判断路径存在（插件里到处能用，不依赖 async）。 */
  _exists: function (p) {
    try {
      return !!p && Zotero.File.pathToFile(p).exists();
    } catch (e) {
      return false;
    }
  },


  /**
   * 这个目录像不像"项目根"（放 .venv / offline / online 的那个）。
   *
   * ⚠ W4（2026-10-14）：判据原来是 `tools\gui.py` 在不在 —— Tk 面板退役后那个文件
   * **已删**，于是四个探测档全部落空、`projectRoot()` 回空串，现象是引擎起不来：
   * 任何走引擎桥的操作都报 `Error: 找不到引擎或 Python`（真机实测，关闭面板窗口后复现）。
   * 现在改成**仍然存在且在语义上更贴切**的标记：`online\engine.py` + `offline\schemas.py`
   * 两个都在才算项目根（只认一个容易误判别人的目录）。
   */
  _looksLikeProjectRoot: function (dir) {
    const d = String(dir || "").trim().replace(/[\\/]+$/, "");
    if (!d) return false;
    return this._exists(d + "\\online\\engine.py")
        && this._exists(d + "\\offline\\schemas.py");
  },

  projectRoot: function () {
    // 1) 用户在设置面板里指定的（唯一权威来源）
    try {
      const v = Zotero.Prefs.get("zotero-kb.projectRoot");
      if (v && this._looksLikeProjectRoot(String(v))) {
        return String(v).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* ignore */ }
    // 2) 服务端上次告诉我们的
    try {
      const v = Zotero.Prefs.get("zotero-kb.serverProjectRoot");
      if (v && this._looksLikeProjectRoot(String(v))) {
        return String(v).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* ignore */ }
    // 3) 环境变量
    try {
      const v = Services.env.get("ZOTERO_KB_ROOT");
      if (v && this._looksLikeProjectRoot(v)) return v;
    } catch (e) { /* ignore */ }
    // 4) 从知识库位置向上找（老布局 <项目>\kb\ 能命中）。
    //    这是探测，不是反推：**验证到才用**。
    try {
      let d = this.kbDir().replace(/[\\/]+$/, "");
      for (let i = 0; i < 4 && d; i++) {
        if (this._looksLikeProjectRoot(d)) return d;
        const up = d.replace(/[\\/][^\\/]+$/, "");
        if (!up || up === d) break;
        d = up;
      }
    } catch (e) { /* ignore */ }
    return "";   // 找不到就返回空，让调用方报"该去哪设置"，不要瞎猜一个
  },


  /** 项目里的 Python 解释器（优先 pythonw，无控制台窗口）。 */
  pythonExe: function () {
    // 0) 用户显式指定的最优先
    try {
      const v = Zotero.Prefs.get("zotero-kb.pythonExe");
      if (v && String(v).trim() && this._exists(String(v).trim())) {
        return String(v).trim();
      }
    } catch (e) { /* ignore */ }
    const root = this.projectRoot();
    const cands = ["\\.venv\\Scripts\\pythonw.exe",
                   "\\.venv\\Scripts\\python.exe"];
    for (const c of cands) {
      if (root && this._exists(root + c)) return root + c;
    }
    // 退回系统 Python：让"没建 venv"也能用（依赖缺了服务端会报清楚）
    try {
      const v = Services.env.get("ZOTERO_KB_PYTHON");
      if (v && this._exists(v)) return v;
    } catch (e) { /* ignore */ }
    return root ? root + cands[0] : "";   // 不存在也返回它，让上层报明确的错
  },


  /** Ollama 可执行文件位置：用户指定 > 标准安装位置。 */
  ollamaPaths: function () {
    // 用户指定优先（装在非默认位置时）
    try {
      const v = Zotero.Prefs.get("zotero-kb.ollamaExe");
      if (v && String(v).trim() && this._exists(String(v).trim())) {
        const p = String(v).trim();
        const dir = p.replace(/[\\/][^\\/]+$/, "");
        return { app: dir + "\\ollama app.exe", exe: p };
      }
    } catch (e) { /* ignore */ }
    let base = "";
    try {
      // 注意：沙箱里没有 `Ci` 这个简写（那是 Mozilla 模块内部的），
      // 要用完整的 Components.interfaces。
      const local = Services.dirsvc.get("LocalAppData",
                                        Components.interfaces.nsIFile).path;
      base = local + "\\Programs\\Ollama";
    } catch (e) {
      // 拿不到 LocalAppData 时才用这个系统级常量位置（不是用户目录的猜测）
      base = "C:\\ProgramData\\Ollama";
      try {
        const pf = Services.dirsvc.get("ProgramFiles",
                                       Components.interfaces.nsIFile).path;
        base = pf + "\\Ollama";
      } catch (e2) { /* ignore */ }
    }
    return { app: base + "\\ollama app.exe", exe: base + "\\ollama.exe" };
  },


  /** DSH 侧收件箱目录（默认在用户主目录下）。 */
  bridgeDirPath: function () {
    let home = "";
    try {
      home = Services.dirsvc.get("Home",
                                 Components.interfaces.nsIFile).path;
    } catch (e) {
      home = "";
    }
    if (!home) {
      // 兜底：Zotero 的 profile 目录一定可写，用它的上一级猜
      try {
        home = Services.dirsvc.get("ProfD",
                                   Components.interfaces.nsIFile).parent.path;
      } catch (e2) { home = "C:\\Users\\Public"; }
    }
    return home + "\\.dsh\\zotero-bridge";
  },


  ollamaFlagPath: function () {
    return this.kbDir() + "\\ollama-owned-by-zotero.flag";
  },


  /**
   * 本地服务（:8765）的归属标记。
   *
   * 和 Ollama 用的是同一套思路：**标记文件在 = 是我们拉起来的**，
   * 关 Zotero 时就顺手关掉；标记不在 = 本来就在跑（用户手动开的、
   * 或开机自启拉起的），绝不去碰它。
   */
  serverFlagPath: function () {
    return this.kbDir() + "\\server-owned-by-zotero.flag";
  },
});

// ===== src/03-processes.js =====
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
   * Zotero 启动时：确保本地服务可用（**随用随取**）。
   *
   * 已经在跑 → 不碰它、不留标记（关 Zotero 时也不动它）。
   * 没在跑   → 拉起来 + 留标记（关 Zotero 时负责关掉）。
   *
   * 为什么做成"跟随 Zotero"而不是让用户开机自启：
   *   · 服务只在 Zotero 用得上（插件要它查权重/领任务），不看书时白占内存；
   *   · 用户不用记着"先开服务再用 Zotero"——本机实测这个坑踩过好几次，
   *     现象是"分类建议失败""权重列空白"，而真实原因只是服务没跑。
   *   · 已经装了开机自启的人也不会被影响：那时服务本来就在跑，
   *     走的是"不碰它"分支。
   */
  serverOnStartup: async function () {
    var self = ZoteroKB;
    try {
      // W3 线3（2026-10-14）：**自动拉起本地服务变成一个开关**
      // （pref `zotero-kb.autoStartService`，默认 true = 老行为）。
      // 为什么要它：用户拍板停掉 8765 之后，如果这条自动拉起还在，Zotero 一重启
      // 它就把服务又拉起来了 —— 那样"停服务"是假的。这里只关"自动拉起"这一个动作，
      // **别的功能一个都不动**（插件此时已全部走引擎桥/文件信箱，不依赖该服务）。
      if (!self.getPref("zotero-kb.autoStartService", true)) {
        Zotero.debug("[zotero-kb] 本地服务自动拉起已关闭"
                     + "（pref zotero-kb.autoStartService=false）—— 不再启动 8765");
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

// ===== src/04-prefs.js =====
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
    defaults[this.PREFS.model] = "";
    defaults[this.PREFS.autoProcess] = true;
    defaults[this.PREFS.categories] = "";   // 空=用知识库现有的
    defaults[this.PREFS.acquireConfirm] = false;
    defaults[this.PREFS.acquireAutoClassify] = true;
    // MinerU 首次安装引导是否已经问过（问过就不再弹，见 19-mineruguide.js）
    defaults[this.PREFS.mineruGuideDone] = false;
    defaults[this.PREFS.optionalGuideDone] = false;
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

});

// ===== src/05-server.js =====
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
   * 判据是 `kb.health`：它给 kb_dir / project_root / python / ollama，够定位知识库
   * 与运行环境（stdio 通道没有"浏览器偷 token"这回事，所以它本来就不含 token）。
   * 引擎桥拿不到时，仍会退回老 HTTP `/health` —— 那只在有人手动起了 8765 才会成功，
   * 属于回退路径，不是常态。
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
      this.serverKbDir = engineInfo.kb_dir || this.serverKbDir || "";
      this.serverProjectRoot = engineInfo.project_root || this.serverProjectRoot || "";
      Zotero.debug("[zotero-kb] 引擎桥在线（kb_dir=" + this.serverKbDir + "）");
    }
    // 桥不可用时，老 HTTP 仍是兜底（8765 平时是停的，所以这条路通常直接失败）
    try {
      const info = await this.request("GET", "/health");
      if (!engineInfo) {
        this.serverOk = !!(info && info.ok);
        this.serverInfo = info;
        this.rememberServerInfo(info);
        this.serverKbDir = (info && info.kb_dir) || "";
        this.serverProjectRoot = (info && info.project_root) || "";
        Zotero.debug("[zotero-kb] 老 HTTP 服务在线（桥不可用，用它兜底）");
      }
    } catch (e) {
      if (!engineInfo) {
        this.serverOk = false;
        this.serverInfo = null;
      }
      Zotero.debug("[zotero-kb] 老 HTTP /health 不在（过渡期正常）：" + e);
    }
    return this.serverOk;
  },
});

// ===== src/06-watch.js =====
/**
 * 06-watch.js —— 新条目监听：条目保存时自动送进知识库（切片 + 索引 + 向量）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 新条目监听

  registerNotifier: function () {
    const self = this;
    const callback = {
      notify: function (event, type, ids, extraData) {
        try {
          self.onNotify(event, type, ids, extraData);
        } catch (e) { Zotero.logError(e); }
      },
    };
    // 只关心条目；附件新加时会以 item 事件出现
    this.notifyIDs.push(Zotero.Notifier.registerObserver(
      callback, ["item"], "zotero-kb", 1
    ));
  },


  onNotify: function (event, type, ids, extraData) {
    // ⚠ 这里必须用闭包里的 self：
    //   · 本函数经 Zotero.Notifier 回调进来，`this` 不一定是我们这个对象；
    //   · 下面 setTimeout 的回调里 `this` 更是会变。
    // 之前写成 this.getPref / this.handleNewItem，会在真正有新增文献时静默失效。
    var self = ZoteroKB;
    if (type !== "item" || event !== "add") return;
    if (!self.getPref(self.PREFS.autoProcess, true)) return;
    // add 事件里 ids 就是新条目
    for (const id of ids) {
      const item = Zotero.Items.get(id);
      if (!item || item.isAttachment() || item.isNote() || item.isAnnotation()) continue;
      // 稍等片刻：条目刚 add 时附件可能还在写入，延迟一下再看。
      // 用沙箱全局的 setTimeout（不是 Zotero.setTimeout —— 那个不存在）。
      setTimeout(() => {
        self.handleNewItem(item).catch((e) => Zotero.logError(e));
      }, 2500);
    }
  },


  /** 新文献的主流程：查知识库状态 → 需要则送切片 → 要分类建议 → 询问用户 */
  handleNewItem: async function (item) {
    if (!this.alive || !item || !item.id) return;
    // ---- 从 DSH 批量导入的那些条目：**跳过一次自动分类**
    //
    // 为什么要跳：`notifyNewItems` 是**逐条**触发分类建议弹窗的，而 acquire
    // 一次可能建十几条 —— 用户点完"添加到 Zotero"之后会被十几个分类框连击。
    // 而且每条还会各起一次 /reindex 子进程，而 kb_acquire 结束时会统一补抽一遍，
    // 纯属重复劳动。
    //
    // ⚠ 只跳**一次**：分类建议这个能力本身没取消，用户随时可以右键
    //   「分类建议（本地模型）」补上。同理，这里也顺带省掉了那次多余的 reindex。
    const marked = this.acquiredIDs && this.acquiredIDs[item.id];
    if (marked) {
      delete this.acquiredIDs[item.id];
      // 时间闸：万一 Notifier 的延时回调没跑起来，标记也会自己过期，
      // 不会把这一条**以后**手工添加时的分类建议也一起吃掉。
      if (Date.now() - marked < 120000) {
        Zotero.debug("[zotero-kb] " + item.key
                     + " 来自 DSH 导入，跳过一次自动分类建议");
        return;
      }
    }
    if (this.pending[item.id]) return;
    this.pending[item.id] = true;
    try {
      if (!(await this.healthCheck())) {
        this.notify("知识库引擎没起来",
          "先在 Zotero 里打开管理面板：工具菜单 →「知识库管理…」→「运行环境」页，"
          + "那里能自检并按提示安装（Python 环境、Ollama 都在那一页）。\n"
          + "插件不依赖任何常驻的本地服务。",
          null, true);
        return;
      }
      const key = item.key;
      // W3 线2.5：走共用的 kbItemInfo（桥优先、退回 HTTP，见 05-server.js）。
      // ⚠ 它可能回 null（两条通道都失败）—— 那时按"拿不到信息"处理：**当成需要
      //   建索引**（宁可多派一次增量建库，也不要漏掉新文献）。
      const info = await this.kbItemInfo([key]);
      const entry = ((info && info.items) || [])[0] || {};
      const needsIndex = !info || !entry.in_kb || !entry.n_chunks;

      if (needsIndex) {
        this.notify("正在把新文献送进知识库…", item.getField("title"), null, true);
        const jobId = await this.reindexOne(key);
        if (jobId) {
          await this.waitJob(jobId, 30);
        }
      }

      // 无论是否新建索引，都给一次分类建议
      await this.suggestFor(item);
    } finally {
      this.pending[item.id] = false;
    }
  },


  /**
   * 给一篇/几篇**派一次建索引**，返回 job id（拿不到就返回空串）。
   *
   * W3 线2.5（2026-10-13）：**优先走宿主半的引擎桥**（`kb.reindex`），拿不到再退回
   * 老 HTTP —— 过渡期两条都必须能用，桥不需要那个常驻 8765。
   *
   * ⚠ **两条通道的"派发回执"形状不一样，这里必须归一化**（踩点）：
   *   · 桥 `kb.reindex` → `{job:{id:"job-…"}, id:"job-…"}`（契约形状，job 是对象）
   *   · HTTP `POST /reindex` → `{job:"job-…", state:"running"}`（job 是**字符串**）
   *   直接照抄老写法 `res.job` 走桥会拿到一个对象，再拿去拼
   *   `GET /jobs/<object>` 就是一条注定失败的请求（而且报错看不出是这个原因）。
   */
  reindexOne: async function (keys) {
    var self = ZoteroKB;
    let res = null;
    try {
      res = await self.kbAdminHostRoute("POST", "/reindex", { keys: keys });
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥派建索引失败（退回 HTTP）：" + e);
    }
    const flat = (r) => (r && ((r.job && r.job.id) || r.job || r.id)) || "";
    if (!flat(res)) {
      try {
        res = await self.request("POST", "/reindex", { keys: keys });
      } catch (e) {
        Zotero.debug("[zotero-kb] 派建索引失败：" + e);
        return "";
      }
    }
    return String(flat(res) || "");
  },


  waitJob: async function (jobId, maxSeconds) {
    var self = ZoteroKB;
    const deadline = Date.now() + (maxSeconds || 30) * 1000;
    while (Date.now() < deadline) {
      // 自己用 setTimeout 包一个延时：
      // 别用 Zotero.Promise.delay —— 插件沙箱的 Zotero 命名空间里不一定有
      // （这里已经踩过一次 Zotero.setInterval / Zotero.setTimeout 不存在的坑）。
      await new Promise((r) => setTimeout(r, 1000));
      try {
        let job = null;
        // W3 线2.5：桥优先（`jobs.get` 与 HTTP `GET /jobs/<id>` **同形**：
        // 都走 LS.do_jobs_get），拿不到再退回 HTTP。
        try {
          job = await self.kbAdminHostRoute("GET", "/jobs/" + jobId);
        } catch (e) {
          job = await self.request("GET", "/jobs/" + jobId);
        }
        if (job && (job.state === "done" || job.state === "failed")) return job;
      } catch (e) {
        return null;
      }
    }
    return null;
  },
});

// ===== src/07-classify.js =====
/**
 * 07-classify.js —— 分类建议：让本地模型判断该归到哪个分类，可一键应用或对话调整
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 分类建议

  buildMeta: function (item) {
    let tags = [];
    try { tags = item.getTags().map((t) => t.tag); } catch (e) { tags = []; }
    let abstract = "";
    try { abstract = item.getField("abstractNote") || ""; } catch (e) { abstract = ""; }
    return {
      key: item.key,
      title: item.getField("title") || "",
      abstract: abstract,
      tags: tags,
      itemType: Zotero.ItemTypes.getName(item.itemTypeID),
      year: (item.getField("date") || "").slice(0, 4),
    };
  },


  configuredCategories: function () {
    // 设置里可以填「分类名,分类名」手动指定；留空则用知识库现有的
    const raw = this.getPref(this.PREFS.categories, "");
    if (!raw) return [];
    return String(raw).split(/[,，\n]/).map((s) => s.trim())
      .filter(Boolean).map((name) => ({ name: name }));
  },


  /**
   * 只问模型要分类建议，**不弹任何窗**。拿不到就返回 null。
   *
   * 为什么要从 `suggestFor` 里拆出来：批量导入（从 DSH 抓进来几篇）时，
   * 逐篇弹窗会变成"抓 5 篇弹 5 个框"。拆开之后可以先**全部问完**，
   * 再用**一个**汇总框让用户一次决定（见 askApplyBatch）。
   */
  classifyOnly: async function (item, feedback, previous) {
    const meta = this.buildMeta(item);
    if (!meta.title && !meta.abstract) return null;
    // 把模型配置一起发过去 —— 这样"配置在插件设置里"对服务端也成立。
    // 服务端会落盘（下次就按这套走），所以管理面板等入口也自动用同一套。
    // 注意按 provider 挑 model：ollama 用 model，API 用 apiModel。
    const isApi = (this.getPref(this.PREFS.provider, "ollama") === "openai");
    const payload = {
      item: meta,
      categories: this.configuredCategories(),
      model: isApi ? this.getPref(this.PREFS.apiModel, "")
                   : this.getPref(this.PREFS.model, ""),
      provider: this.getPref(this.PREFS.provider, "ollama"),
      base_url: isApi ? this.getPref(this.PREFS.apiBaseUrl, "") : "",
      api_key: isApi ? this.getPref(this.PREFS.apiKey, "") : "",
    };
    // "跟模型对话调整"：把上一轮结论和用户的意见一起带回去。
    // ⚠ 只带上一轮的 category/reason/round，不带 tags —— 免得模型
    //   把上一轮的标签原样抄回来，看着像"没听懂调整"。
    if (feedback) {
      payload.feedback = String(feedback).slice(0, 500);
      payload.previous = {
        category: (previous && previous.category) || "",
        reason: (previous && previous.reason) || "",
        round: (previous && previous.round) || 1,
      };
    }
    try {
      // ⚠ 这一处曾经**故意留在老 HTTP**（2026-10-13）：引擎原来的 `ai.classify` 只认
      //   **key**、由它从索引库读元数据，而这里要分类的是**还没入库的新文献**
      //   （元数据只能在 Zotero 这边现取），且要带**用户在这个弹窗里选的**模型配置。
      //   那是**两个能力**，不能互相顶替（硬迁会对新文献报 `索引库里没有 key=…`）。
      //
      // 2026-10-14：引擎侧已按同一契约补齐 `classify.item`（`POST /classify-item`，
      //   见 online/engine.py::_m_classify_item：收**内联** item + 调用方给的模型配置，
      //   复用 `LS.do_classify_one`），所以这里改成**桥优先 → 退回老 HTTP**。
      //   入参就是本函数上面造好的 `payload` —— 它**本来就是扁平的**，
      //   不用像 `/env-config` 那样再摊平一层。
      let res = null;
      try {
        res = await self.kbAdminHostRoute("POST", "/classify-item", payload);
      } catch (e) {
        Zotero.debug("[zotero-kb] 引擎桥分类失败（退回 HTTP）：" + e);
      }
      if (!res || res.error) {
        try {
          res = await self.request("POST", "/classify", payload);
        } catch (e) {
          Zotero.debug("[zotero-kb] 分类请求失败：" + e);
          return null;
        }
      }
      if (!res || res.error) return null;
      return res;
    } catch (e) {
      Zotero.debug("[zotero-kb] 分类请求失败：" + e);
      return null;
    }
  },


  suggestFor: async function (item, feedback, previous) {
    const meta = this.buildMeta(item);
    if (!meta.title && !meta.abstract) return;
    const res = await this.classifyOnly(item, feedback, previous);
    if (!res) {
      this.notify("分类建议失败",
                  "看起来模型没响应（面板「运行环境」页可自检）",
                  null, true);
      return;
    }
    this.askApply(item, res, feedback);
  },


  /**
   * 一批新文献的**汇总**分类确认（从 DSH 导入时用）。
   *
   * 为什么要有它：逐篇弹窗在批量导入时是灾难（抓 5 篇弹 5 个模态框，
   * 用户在点完"添加"之后还要连着点 5 次）。这里把建议**全部算完再问一次**。
   *
   * pairs: [{item, sug}]；sug 为 null 表示那篇没拿到建议。
   * 返回：{applied, skipped, done}
   */
  askApplyBatch: async function (pairs) {
    var self = ZoteroKB;
    const list = (pairs || []).filter((x) => x && x.item);
    if (!list.length) return { applied: 0, skipped: 0, done: true };

    const lines = ["本地模型给这 " + list.length + " 篇的建议：", ""];
    list.forEach((x, i) => {
      const title = (x.item.getField("title") || "(无标题)").slice(0, 48);
      if (!x.sug) {
        lines.push("  " + (i + 1) + ". 《" + title + "》");
        lines.push("       ⚠ 没拿到建议（模型或服务没响应）");
        return;
      }
      const conf = Math.round((x.sug.confidence || 0) * 100);
      lines.push("  " + (i + 1) + ". 《" + title + "》");
      lines.push("       → " + (x.sug.category || "（无合适分类）")
                 + "　置信 " + conf + "%");
      if (x.sug.tags && x.sug.tags.length) {
        lines.push("       标签：" + x.sug.tags.join("、"));
      }
    });
    lines.push("");
    lines.push("「全部应用」= 各自归入上面的分类并打标签；"
               + "「逐篇确认…」= 一篇篇问你（能看理由、也能让模型重判）；"
               + "「跳过」= 都不动，条目照常留在 Zotero 里。");

    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_2 * ps.BUTTON_TITLE_IS_STRING;
    // ⚠ confirmEx 只有 9 个参数，第 9 个必须是对象（见 askOneMeta 的说明）
    const choice = ps.confirmEx(
      win, "文献知识库 · 分类建议（本批 " + list.length + " 篇）",
      lines.join("\n"), flags,
      "全部应用", "逐篇确认…", "跳过", null, {}
    );

    if (choice === 0) {
      let applied = 0;
      for (const x of list) {
        if (!x.sug) continue;
        try {
          await self.applySuggestion(x.item, x.sug.category, x.sug.tags, true);
          applied++;
        } catch (e) {
          Zotero.logError(e);
        }
      }
      self.notify("已应用分类建议", applied + " 篇已归入分类并打标签", null, false);
      return { applied, skipped: list.length - applied, done: true };
    }
    if (choice === 1) {
      // 逐篇：复用原有的单篇弹窗（它带"调整…"能与模型对话）
      for (const x of list) {
        if (x.sug) self.askApply(x.item, x.sug, "");
        else await self.suggestFor(x.item);
      }
      return { applied: -1, skipped: 0, done: true, oneByOne: true };
    }
    return { applied: 0, skipped: list.length, done: true };
  },


  /**
   * 标重点 / 取消重点（右键菜单用）。
   *
   * 用户要求："右键菜单没有标重点。" —— 原来标重点只能去管理面板的
   * 「高级 → 手动调权重」填 key，而用户在 Zotero 里看到某篇的那一瞬间
   * 才是最短路径。
   *
   * 写完后**立刻刷新权重缓存**并让列表重画 —— 否则用户点了"标为重点"
   * 但权重列的星号要等下次轮询（最多 20 秒）才出现，看着像没生效。
   */
  setPinned: async function (items, pinned) {
    var self = ZoteroKB;
    if (!items || !items.length) return;
    const keys = items.map((it) => it.key).filter(Boolean);
    if (!keys.length) return;
    let okCount = 0, lastWeight = null, err = "";
    for (const k of keys) {
      try {
        // W3 线2.5（2026-10-13）：**优先走宿主半的引擎桥**（`kb.weight`，复用
        // LS.do_set_weight —— 与 HTTP /weight **同一份实现**），拿不到再退回老 HTTP。
        // 两条通道的回包形状实测同形（`{ok, key, pinned, manual, note, weight, …}`），
        // 所以这里不需要像 /reindex 那样归一化。
        let r = null;
        try {
          r = await self.kbAdminHostRoute("POST", "/weight",
                                          { key: k, pinned: !!pinned });
        } catch (e) {
          Zotero.debug("[zotero-kb] 引擎桥写权重失败（退回 HTTP）：" + e);
        }
        if (!r || r.ok !== true) {
          r = await self.request("POST", "/weight", { key: k, pinned: !!pinned });
        }
        if (r && r.ok) {
          okCount++;
          lastWeight = r.weight;
        } else {
          err = (r && r.error) || "服务端没接受";
        }
      } catch (e) {
        err = String(e);
      }
    }
    // 刷新缓存 + 重画（不用等轮询）
    try { await self.refreshWeights(); } catch (e) { /* ignore */ }
    if (okCount) {
      const what = pinned ? "已标为重点" : "已取消重点";
      self.notify(
        what + "（" + okCount + " 篇）",
        (lastWeight != null ? ("现在权重 " + lastWeight
                               + "（检索时会往前排）") : "")
        + (okCount < keys.length ? ("　✗ " + (keys.length - okCount)
                                    + " 篇失败：" + err) : ""),
        null, false);
    } else {
      self.notify("操作失败", err || "服务端没有响应", null, true);
    }
  },


  /**
   * 弹窗：显示推荐 → 可「调整」让模型重来 → 满意了「应用」。
   *
   * 为什么要可调整（用户明确要求"应该能跟本地模型对话来调整分类"）：
   *   模型只看标题+摘要，用户可能知道更多（比如这篇其实属于哪个项目）。
   *   只说一句"这更像注意力机制那类"就能让它改，比让用户自己去翻分类列表快。
   *
   * 为什么用 `Services.prompt.confirmEx` 而不是自建窗口：
   *   cancelable 的循环需要"弹窗→拿输入→再弹窗"，confirmEx + prompt 这套
   *   系统对话框已经够用，且不引入新的窗口/生命周期管理（本机在这上面
   *   栽过：ProgressWindow 没有关闭按钮，自建 XUL 窗口要处理父子与卸载）。
   */
  askApply: function (item, suggestion, feedback) {
    const category = suggestion.category || "";
    const tags = suggestion.tags || [];
    const conf = Math.round((suggestion.confidence || 0) * 100);
    const rnd = suggestion.round || (feedback ? 2 : 1);
    const title = (item.getField("title") || "").slice(0, 60);

    const lines = ["《" + title + "》", ""];
    if (rnd > 1) {
      lines.push("（第 " + rnd + " 轮建议，已参考你的意见）");
      lines.push("");
    }
    lines.push("推荐分类：" + (category || "（无合适分类）")
               + "   置信 " + conf + "%");
    if (suggestion.reason) lines.push("理由：" + suggestion.reason);
    if (tags.length) lines.push("推荐标签：" + tags.join("、"));
    if (suggestion.raw_category && suggestion.raw_category !== category) {
      lines.push("");
      lines.push("（模型原本说「" + suggestion.raw_category
                 + "」，不在已知分类里，已忽略）");
    }
    lines.push("");
    lines.push("「应用」= 归入该分类并打标签；「调整」= 告诉模型哪里不对，让它重来。");

    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_2 * ps.BUTTON_TITLE_IS_STRING;
    const choice = ps.confirmEx(
      win, "文献知识库 · 分类建议（按「调整」可与模型对话）",
      lines.join("\n"), flags,
      "应用", "调整…", "跳过", null, {}
    );

    if (choice === 0) {
      this.applySuggestion(item, category, tags, true)
        .catch((e) => Zotero.logError(e));
      return;
    }
    if (choice === 1) {
      // 让用户说一句哪里不对。默认值给他上一轮的建议做参考。
      const input = { value: "" };
      const ok = ps.prompt(
        win, "告诉模型怎么调整",
        "说一句你的判断，例如：\n"
        + "  · 这篇应该归到「分类 A」\n"
        + "  · 这是设备类，不是方法类\n"
        + "  · 分类对，但标签应该是 A、B\n"
        + "（留空就直接重来一次）",
        input, null, {});
      if (!ok) return;                       // 用户取消了
      const fb = String(input.value || "").trim();
      this.notify("正在按你的意见重新判断…",
                  fb ? ("意见：" + fb.slice(0, 40)) : "（未填意见，重来一次）",
                  null, false);
      // 用 setTimeout 跳出当前弹窗栈再发请求 —— 不然新弹窗可能被
      // 刚关掉的对话框抢焦点（实测在 confirmEx 回调里同步弹下一个会闪）。
      const self = this;
      setTimeout(function () {
        self.suggestFor(item, fb || "（用户未说明具体意见，请重新判断）",
                        suggestion).catch((e) => Zotero.logError(e));
      }, 120);
      return;
    }
    // choice === 2：跳过，什么都不做
  },


  /** 真正写回 Zotero：分类（归属到 collections）+ 标签 */
  applySuggestion: async function (item, categoryName, tags, addTags) {
    const done = [];
    try {
      if (categoryName) {
        const col = await this.ensureCollection(categoryName);
        if (col) {
          item.addToCollection(col.id);
          done.push("已归入「" + categoryName + "」");
        }
      }
      if (addTags && tags && tags.length) {
        const existing = new Set(item.getTags().map((t) => t.tag));
        const toAdd = tags.filter((t) => !existing.has(t));
        toAdd.forEach((t) => item.addTag(t, 0));
        if (toAdd.length) done.push("已加标签：" + toAdd.join("、"));
      }
      if (done.length) {
        await item.saveTx();
        this.notify("已应用分类建议", done.join("；"), null, false);
      }
    } catch (e) {
      Zotero.logError(e);
      this.notify("写回失败", String(e), null, true);
    }
  },


  /** 找到或创建分类。只建顶层（用户要求最多两层），不嵌套。 */
  ensureCollection: async function (name) {
    const libraryID = Zotero.Libraries.userLibraryID;
    let col = null;
    try {
      // 先按名字精确找。
      // ⚠ 用 Array.from 包一层：getByLibrary 的返回不一定就是 Array
      //   （可能是类数组/Map），`|| []` 挡不住"它不是数组"的情况，
      //   直接 .find 会抛错 → ensureCollection 返回 null → 分类写不进去。
      const raw = Zotero.Collections.getByLibrary(libraryID, true);
      const all = raw ? Array.from(raw) : [];
      col = all.find((c) => c && c.name === name) || null;
    } catch (e) {
      Zotero.debug("[zotero-kb] 查分类失败：" + e);
      col = null;
    }
    if (col) return col;
    if (!this.getPref("zotero-kb.createMissing", true)) return null;
    try {
      col = new Zotero.Collection();
      col.libraryID = libraryID;
      col.name = name;
      await col.saveTx();
      Zotero.debug("[zotero-kb] 新建分类：" + name);
      return col;
    } catch (e) {
      Zotero.logError(e);
      return null;
    }
  },
});

// ===== src/08-metafill.js =====
/**
 * 08-metafill.js —— 元数据补全（单篇）：从 PDF 首页找缺的字段，逐条带证据确认
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 元数据补全

  /**
   * 从 PDF 首页给**一篇**文献要"元数据补全建议"。
   *
   * 数据流：插件 → `POST /metafill {key}` → `offline/metafill.py`
   *   （规则优先、模型兜底）→ 建议数组（每条带 字段/建议值/来源/置信/原文证据）。
   *
   * ⚠ 服务端返回的 `suggestions` **只含"当前为空 + 找到了值"的字段**。已有值的
   *   字段进 `skipped` 并写明原因（"只填空不覆盖"是模块的红线，不是这里判断的）。
   *   所以"没有建议"≠"工具没工作" —— 必须把 skipped / notes 讲给用户听，
   *   否则用户只会看到什么都没发生（见 askApplyMeta 里的空结果分支）。
   *
   * ⚠ 服务端**只读**：写回全在这个文件里，而且只在用户于弹窗里勾选之后。
   *   这里也**没有**任何自动写回的调用点 —— 不要给这个函数加"静默应用"的分支。
   */
  metaFillFor: async function (item) {
    if (!item || !item.key) return;
    // ---- 先看"类型/标题有没有被网页污染"
    //
    // 为什么放在**取字段建议之前**（顺序有实际后果，不是随便排的）：
    //   改类型会改变"哪些字段合法"。把 `webpage` 改成 `journalArticle` 之后，
    //   卷/期/页码才成为可写字段。先补字段再改类型，那几条会被
    //   `applyMeta` 的字段合法性检查挡掉，用户看到"明明有建议却写不进去"。
    //
    // 为什么用"本地初判 + 命中才发请求"：绝大多数条目根本不是网页存来的，
    //   对它们这一步零开销（不联网、不多一次往返），右键流程和以前完全一样。
    let tf = null;
    if (this.itemIsWebSaved(item)) {
      try {
        self.notify("正在检查条目类型与标题…", "这一篇是网页类型但挂着 PDF", null, false);
      } catch (e) { /* ignore */ }
      try {
        // W3 线2.5（2026-10-14）：桥优先（`meta.typefixOne` = HTTP `POST /typefix`
        // **同一份同步实现**，只出建议、不写库），失败退回老 HTTP。
        try {
          tf = await self.kbAdminHostRoute("POST", "/typefix",
                                           { key: item.key, use_network: true });
        } catch (e0) {
          Zotero.debug("[zotero-kb] 引擎桥类型检查失败（退回 HTTP）：" + e0);
          tf = await self.request("POST", "/typefix",
                                  { key: item.key, use_network: true });
        }
      } catch (e) {
        // 检查失败不能挡住原本的补全功能 —— 记一行，继续走下面的字段建议
        Zotero.debug("[zotero-kb] 类型/标题检查失败：" + e);
        tf = null;
      }
    }
    let typeFixDone = false;
    if (tf && tf.ok !== false && !tf.error && (tf.fixes || []).length) {
      if (this.askApplyTypeFix(item, tf)) {
        try {
          const rep = await this.applyTypeFix(item, tf);
          this.reportTypeFix(rep);
          typeFixDone = true;
        } catch (e) {
          Zotero.logError(e);
          this.alertDialog("类型/标题修正失败", String((e && e.message) || e));
        }
      }
    } else if (tf && tf.ok !== false && (tf.signals || []).length) {
      // 有嫌疑但给不出可靠建议 —— 也要说一声，别让用户以为"什么都没发生"
      const lines = ["《" + this.itemLabel(item) + "》"];
      lines.push("");
      lines.push("这条看起来是「先保存网页、后来挂上 PDF」，但拿不准该怎么改：");
      (tf.signals || []).forEach((s) => lines.push("· " + s));
      if ((tf.notes || []).length) {
        lines.push("");
        (tf.notes || []).forEach((n) => lines.push("· " + n));
      }
      lines.push("");
      lines.push("没有把握的改动一律不做 —— 你可以在 Zotero 右侧的信息栏里手工改。");
      this.alertDialog("类型/标题可能有污染（未自动改）", lines.join("\n"));
    }

    let res;
    try {
      // 这个请求可能真的要调本地模型（规则没抽到字段时），所以可能几十秒；
      // request() 的超时是 120 秒。等的时候弹的是进度窗（非模态），不卡界面。
      // W3 线2.5（2026-10-14）：桥优先（`meta.fillOne` = HTTP `POST /metafill`
      // **同一份同步实现**，只出建议、不写库），失败退回老 HTTP。
      try {
        res = await self.kbAdminHostRoute("POST", "/metafill",
                                          { key: item.key, use_model: true });
      } catch (e0) {
        Zotero.debug("[zotero-kb] 引擎桥补元数据失败（退回 HTTP）：" + e0);
        res = await self.request("POST", "/metafill",
                                 { key: item.key, use_model: true });
      }
    } catch (e) {
      this.notify("补全建议失败", String((e && e.message) || e), null, true);
      return;
    }
    if (!res || res.ok === false) {
      // 服务端把失败原因原样带回来了（它内部 try/except 过），这里照实显示。
      this.alertDialog(
        "补全建议失败",
        String((res && res.error) || "服务端没有返回可用的结果")
        + "\n\n先看一眼运行环境：工具菜单 →「知识库管理…」→「运行环境」页，"
        + "那里能看到引擎、Python 与 Ollama 的状态，缺什么就按提示装上。");
      return;
    }
    if (typeFixDone) {
      // 改过类型/标题之后，服务端给的这一份建议是**按旧类型**算的；
      // 字段的合法性在写回时（applyMeta）会按新类型再判一次，所以照用没问题，
      // 只要在弹窗里说明白，用户就不会奇怪"为什么它读到的类型是旧的"。
      res.notes = (res.notes || []).concat(
        ["注意：上面刚改过类型/标题，这份字段建议是按改动前算的；"
         + "写入时会按新类型再校验一次字段是否合法。"]);
    }
    this.askApplyMeta(item, res);
  },


  /**
   * 建议的"行文本"：字段 → 建议值　〔来源·置信〕＋ 原文证据 ＋ 补充说明。
   *
   * 为什么把 source / confidence 翻成中文再显示：它们是**决定信不信这条**的
   * 关键信息（规则是从固定格式里抠的、模型会编），给用户看 `rule|model`、
   * `high|low` 等于让他先学一套内部枚举。
   *
   * 为什么要显示证据是否"定位到了"：模型给的建议有 `verified:false` 这一档
   * （它复述的原文没能在正文里找到）。那是最需要用户自己看一眼 PDF 的情况，
   * 不能跟规则抽出来的一视同仁。
   *
   * 值的显示做了一处特判：`creators` 的 `value` 可能为空而 `values` 有内容
   * （服务端两个都发，但**写回只认 values**）。这里兜一下，避免出现
   * "creators →"后面空空如也、用户以为没抽到作者。
   */
  metaLine: function (sug, idx) {
    const src = sug.source === "model" ? "模型" : "规则";
    const conf = sug.confidence === "low" ? "低" : "高";
    const ev = sug.evidence || {};
    let shown = sug.value;
    if (sug.field === "creators" && !String(shown == null ? "" : shown).trim()) {
      shown = this.creatorNames(sug).join("; ");
    }
    let line = (idx != null ? (idx + ". ") : "")
      + sug.field + "　→　" + shown
      + "　〔" + src + "·" + conf + "置信〕";
    // ⚠ 双源标注（2026-10-05 起服务端可能给 sources/agreement）：
    //   两路一致 = 更可信；两路不一致 = 这条是"按来源优先表取的一路"，
    //   另一路的值必须给用户看见 —— 否则他会以为工具只找到这一个值。
    //   老服务端不给这几个键时，这段整块跳过（向后兼容）。
    if (Array.isArray(sug.sources) && sug.sources.length) {
      const tag = { both: "两路一致", single: "单路",
                    conflict: "⚠ 两路不一致", "model-picked": "模型选了这一路" }
        [sug.agreement] || "";
      const names = sug.sources.map(function (s) {
        return s === "pdf" ? "PDF 原文" : (s === "mineru" ? "MinerU" : s);
      }).join("＋");
      if (tag) line += "　[" + tag + "：" + names + "]";
    }
    if (Array.isArray(sug.alternatives) && sug.alternatives.length) {
      line += "\n     另一路给出的是："
        + sug.alternatives.map(function (a) {
          const n = a.source === "pdf" ? "PDF 原文" : "MinerU";
          return String(a.value) + "（" + n + "）";
        }).join(" / ");
    }
    // ⚠ pages 的低置信建议单独提示：本机实测模型会把"该页页码"（例如 564）
    //   当成页码范围写进来 —— 值本身是合法数字，不提示的话很难发现。
    if (sug.field === "pages" && sug.confidence === "low") {
      line += "\n     ⚠ 可能是单页页码，请核对";
    }
    if (ev.text) {
      line += "\n     证据：" + (ev.page ? ("PDF 第 " + ev.page + " 页") : "页码未定位")
        + "　「" + String(ev.text) + "」";
      if (ev.verified === false) {
        line += "\n     ⚠ 这段原文没能在正文里定位到，请自行核对 PDF";
      }
    } else {
      line += "\n     证据：（没有给出原文出处）";
    }
    // 值之外还必须让用户看见的东西（作者名单 / 名字没列全的警示 / 日期类型 /
    // 证据附注）—— 见 metaNotes 的说明。
    this.metaNotes(sug).forEach((n) => { line += "\n     " + n; });
    return line;
  },


  /**
   * 服务端在建议里附带的"值之外的信息"，逐条列出来（没有就是空数组）。
   *
   * 四类（都是**服务端算好的**，客户端只显示）：
   *   · 作者名单 —— `values` 是写回用的有序对象列表，逐行显示才对得上；
   *     只看 `value` 那个拼接串的话，用户核对不出"姓和名有没有被拆对"，
   *     而这正是作者这个字段唯一会出错的地方；
   *   · `partial === true` 的警示 —— 原文只列了前 N 位（et al./等），
   *     实际作者可能更多。漏掉这条，用户会以为"作者齐了"；
   *   · `date_kind` —— 日期可能是收稿/网络首发日期（不是正式出版日期）。
   *     不提示的话，用户没法判断要不要把这个值写进 Zotero；
   *   · `evidence.note` —— 服务端给的补充说明（例如"其中 2 位姓名没能
   *     可靠拆分，整名放在 lastName"）。
   *
   * 为什么单独一个函数、而不是写在 metaLine 里：弹窗有**两处** ——
   * 汇总列表（metaLine）和逐条确认框（askOneMeta）。真正拍板"这条要不要写"
   * 的是逐条框，那里看不到这些信息等于让用户盲签。两处共用一份实现，
   * 才不会出现"汇总里写了、确认框里没有"。
   *
   * ⚠ 文案一律**用服务端给的**（warning / date_kind_label / note），
   * 客户端不自己拼 —— 拼一份就会两处漂移（服务端改了插件不知道）。
   */
  metaNotes: function (sug) {
    const out = [];
    if (!sug) return out;
    if (sug.field === "creators") {
      const names = this.creatorNames(sug);
      if (names.length) {
        out.push("作者共 " + names.length + " 位：");
        names.forEach((n, i) => out.push("　" + (i + 1) + ". " + n));
      } else {
        out.push("⚠ 建议里没有可用的作者姓名，这一条不会被写入");
      }
      if (sug.partial === true) {
        out.push("⚠ " + (sug.warning
          || ("原文只列了前 " + names.length + " 位作者（et al./等），"
              + "实际作者可能更多。")));
      }
    }
    // 日期类型：published（正式出版）不用提示，其余都要说清楚是哪种日期。
    // ⚠ 判据用 `date_kind` 而不是"有没有 label" —— 老服务端可能只给 label。
    if (sug.field === "date" && sug.date_kind && sug.date_kind !== "published") {
      out.push("日期类型：" + (sug.date_kind_label || sug.date_kind));
    }
    const note = (sug.evidence || {}).note;
    if (note) out.push(String(note));
    return out;
  },


  /**
   * 从建议里取出**给人看**的作者姓名列表。
   *
   * 拼法是 `firstName lastName`（西文名的自然顺序，和服务端 `value` 的拼法
   * 一致）；中文名的 `firstName` 是空的，这时只显示 `lastName`，
   * 不留多余空格 —— 用户核对的就是"这两半有没有被拆错"。
   *
   * 优先 `values`；没有（老版本服务端只发 value）才退回切开 `value` 那个串。
   */
  creatorNames: function (sug) {
    const out = [];
    const vals = (sug && sug.values) || null;
    if (Array.isArray(vals)) {
      vals.forEach((c) => {
        if (!c) return;
        const name = [String(c.firstName == null ? "" : c.firstName).trim(),
                      String(c.lastName == null ? "" : c.lastName).trim()]
          .filter((s) => s).join(" ");
        if (name) out.push(name);
      });
    }
    if (out.length) return out;
    return String((sug && sug.value) || "").split(/[;；]/)
      .map((s) => s.trim()).filter((s) => s);
  },


  /**
   * 从建议里取出**能交给 `item.setCreators()`** 的数组（没有就返回空数组）。
   *
   * 形态是**读过 Zotero 源码 + 真机实测**确认的（Zotero 10.0.5 的
   * `chrome/content/zotero/xpcom/data/item.js`，第 1450 行）：
   *   `setCreators(data, options = {})`，data = `[{lastName, firstName, creatorType}]`；
   *   `creatorType` **必填**（缺了抛 "Creator data must include a valid
   *   'creatorType' or 'creatorTypeID' property"，实测过），值可以是类型名
   *   （"author"）或类型 ID。
   *
   * ⚠ 只认 `values`：`value` 是一根给人看的拼接串（`张三; San Zhang`），
   * 从它反推"哪一段是姓、哪一段是名"只能靠猜 —— 猜错的后果是把引文毁掉，
   * 而且用户不容易发现。所以没有 values 就**不写**，如实报出来。
   *
   * 为什么这一层还要再校验一次（服务端已经给过结构化的 values 了）：
   * 这里是**唯一**真正动用户数据的地方，宁可多挡一道。空姓名直接丢掉
   * （空 lastName+空 firstName 在 Zotero 里是一条空作者行，会显示成"(无)"）。
   */
  creatorValues: function (sug) {
    const out = [];
    const vals = (sug && sug.values) || null;
    if (!Array.isArray(vals)) return out;
    vals.forEach((c) => {
      if (!c) return;
      const last = String(c.lastName == null ? "" : c.lastName).trim();
      const first = String(c.firstName == null ? "" : c.firstName).trim();
      if (!last && !first) return;
      out.push({
        lastName: last,
        firstName: first,
        // 缺 creatorType 时补 author：服务端本来就只出 author，
        // 这里补的是"万一将来服务端少给一个键"的兜底。
        creatorType: String(c.creatorType || "author"),
      });
    });
    return out;
  },


  /**
   * "一条建议都没有"时的说明文本。
   *
   * 为什么非要写这一段（而不是静默什么都不弹）：用户点了菜单却什么都没看到，
   * 只会以为功能坏了。而实际原因好几种 —— 字段本来就有值、正文里确实找不到、
   * 没有 PDF 正文、模型没启动 —— 这几种的处理方式完全不同，必须说清是哪一种。
   */
  metaEmptyText: function (item, res) {
    const lines = [];
    lines.push("《" + this.itemLabel(item) + "》"
               + (res.item_type ? ("　类型：" + res.item_type) : ""));
    lines.push("正文来源：" + (res.fulltext_source || "没有可用正文")
               + (res.fulltext_pages != null ? ("　读到 " + res.fulltext_pages + " 页") : ""));
    // 双源（2026-10-05）：这句能一眼看出"这次到底看了几路"，以及另一路为什么没用上
    // —— 用户报障时最常见的问题就是"为什么没抽到"（老服务端不给 sources 时跳过）。
    if (res.sources && res.sources.line) {
      lines.push("看了哪几路：" + res.sources.line
                 + (res.sources.mineru && res.sources.mineru.ok === false
                    && res.sources.mineru.why
                    ? ("　（MinerU："
                       + String(res.sources.mineru.why).slice(0, 40) + "）") : ""));
    }
    lines.push("");
    lines.push("这几个字段在 PDF 首页里都没找到可靠依据，"
               + "所以这次没有任何可补的值：");
    lines.push("");
    for (const k of (res.skipped || [])) {
      lines.push("· " + k.field + "：" + k.why);
    }
    if (!(res.skipped || []).length) lines.push("·（服务端没有给出逐字段说明）");
    const notes = res.notes || [];
    if (notes.length) {
      lines.push("");
      lines.push("补充说明：");
      notes.forEach((n) => lines.push("· " + n));
    }
    const m = res.model || {};
    lines.push("");
    lines.push("模型：" + (m.used ? ("用过了（" + (m.backend || "本地模型") + "）")
                                 : ("没用到 —— " + (m.reason || "未知原因"))));
    return lines.join("\n");
  },


  /**
   * 弹窗：先给"总览"（照分类建议那套 confirmEx 的样式），再按用户选择走。
   *
   * 三条路：
   *   · 全部应用 —— 默认全勾选的等价操作，一次点完；
   *   · 逐条确认… —— 一条一个对话框，**每条一个勾选框、默认勾上**，
   *     用户取消勾选或点「跳过这条」就不写它；
   *   · 跳过 —— 什么都不写。
   *
   * ⚠ 写入只可能由用户在这里（或逐条框里）点出来 —— 没有任何自动路径。
   */
  askApplyMeta: function (item, res) {
    const sugs = res.suggestions || [];
    if (!sugs.length) {
      // 一条都没有也必须开口（用户要的"不要静默什么都不显示"）
      this.alertDialog("这篇没有可补的字段", this.metaEmptyText(item, res));
      return;
    }

    const lines = ["《" + this.itemLabel(item) + "》", ""];
    lines.push("PDF 首页里找到 " + sugs.length + " 个字段可以补：");
    lines.push("");
    sugs.forEach((s, i) => lines.push(this.metaLine(s, i + 1)));
    const skipped = res.skipped || [];
    if (skipped.length) {
      lines.push("");
      lines.push("没有出现在上面的字段：");
      skipped.forEach((k) => lines.push("· " + k.field + "：" + k.why));
    }
    lines.push("");
    lines.push("「全部应用」= 按上面全部写入；「逐条确认」= 一条条勾选"
               + "（默认都勾上，可以取消）；「跳过」= 什么都不写。");
    lines.push("只填空、不覆盖。作者（creators）只在「这条目一位作者都没有」时"
               + "才写，而且是**整份名单**；标题永远不写。");

    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_2 * ps.BUTTON_TITLE_IS_STRING;
    // 按钮 0（默认）= 全部应用：用户要的语义就是"默认全勾选、可取消"。
    // 最后一个参数是确认框的复选框（这里是 null = 不要复选框），照分类建议的写法。
    const choice = ps.confirmEx(
      win, "文献知识库 · 元数据补全建议",
      lines.join("\n"), flags,
      "全部应用", "逐条确认…", "跳过", null, {}
    );
    if (choice === 2) return;                       // 跳过
    if (choice === 1) {
      // 逐条：confirmEx 是**同步**的，所以这里用一个普通 for 循环一条条问就行；
      // 真正异步的只有最后的写回（applyMeta）。
      // `declined` 专门收集"用户没要的"（取消勾选 / 跳过 / 中断后剩下的）——
      // 只用来在最后的回报里列清楚，那几条**绝不会**被写。
      // 为什么非要收集：不传的话回报只会说"已写入 2 项"，用户看到
      // 自己取消过的那条不在结果里，会以为是程序漏了（dry-run 里抓到的）。
      const picked = [], declined = [];
      for (let i = 0; i < sugs.length; i++) {
        const d = this.askOneMeta(sugs[i], i, sugs.length);
        if (d === "stop") {
          for (let j = i; j < sugs.length; j++) declined.push(sugs[j]);
          break;
        }
        if (d === "yes") picked.push(sugs[i]);
        else declined.push(sugs[i]);
      }
      if (!picked.length) {
        this.notify("没有勾选任何字段", "已取消，什么都没写", null, false);
        return;
      }
      this.applyMeta(item, picked, declined).catch((e) => Zotero.logError(e));
      return;
    }
    // choice === 0：全部应用
    this.applyMeta(item, sugs).catch((e) => Zotero.logError(e));
  },


  /**
   * 逐条确认用的单字段对话框：**一个勾选框，默认勾上**。
   *
   * 为什么用 `Services.prompt.confirmEx` 的第 8/9 个参数而不是自建 XUL 窗口：
   *   那两个参数就是 XPCOM 询问框的"复选框文字 + 勾选状态"（第 9 个传
   *   `{value:bool}`，返回后 `value` 里是用户的最终状态）。**这是读过定义确认的**：
   *   Zotero 自己的 `chrome/content/zotero/xpcom/prompt.js` 里
   *   `Zotero.Prompt.confirm()` 就是把 checkLabel/checkbox 原样透传给 confirmEx，
   *   Zotero 的 Mendeley 导入提示、连接器版本提示都用它。
   *   自建 XUL 窗口要处理父子与卸载，本项目在这上面栽过 —— 不引入。
   *
   * 返回："yes"=这条要写 / "no"=这条跳过 / "stop"=后面的都不用了。
   */
  askOneMeta: function (sug, i, n) {
    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const ev = sug.evidence || {};
    const lines = [];
    lines.push("字段：" + sug.field + "　（当前值：空）");
    lines.push("建议值：" + sug.value);
    lines.push("来源：" + (sug.source === "model" ? "模型（本地小模型，会出错）" : "规则（从固定格式里抽取）")
               + "　　置信：" + (sug.confidence === "low" ? "低" : "高"));
    // ⚠ 用户实测反馈过的那一类：模型把"该页页码"当成页码范围。
    if (sug.field === "pages" && sug.confidence === "low") {
      lines.push("⚠ 这个值可能是【单页页码】，不是页码范围 —— 请核对 PDF 再决定。");
    }
    lines.push("");
    if (ev.text) {
      lines.push("原文证据（" + (ev.page ? ("PDF 第 " + ev.page + " 页") : "页码未定位") + "）：");
      lines.push("「" + String(ev.text) + "」");
      if (ev.verified === false) {
        lines.push("⚠ 模型复述的这段原文没能在正文里定位到，请自行核对 PDF。");
      }
    } else {
      lines.push("（服务端没给出原文证据 —— 这条没有依据，建议核对后再决定）");
    }
    // 值之外的补充信息（作者名单 / 名字没列全的警示 / 日期类型 / 证据附注）：
    // 逐条确认框是用户**真正拍板**的地方，这里看不到等于让他盲签。
    // 与汇总列表（metaLine）共用 metaNotes，两处才不会不一致。
    const notes = this.metaNotes(sug);
    if (notes.length) {
      lines.push("");
      notes.forEach((n) => lines.push(n));
    }
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_2 * ps.BUTTON_TITLE_IS_STRING;
    const checkbox = { value: true };        // 默认勾选（用户要的"默认全勾上"）
    const choice = ps.confirmEx(
      win, "元数据补全 · 第 " + (i + 1) + "/" + n + " 条",
      lines.join("\n"), flags,
      "确定", "跳过这条", "后面的都不用了",
      "应用这一条（取消勾选 = 跳过）", checkbox
    );
    if (choice === 2) return "stop";
    if (choice === 1) return "no";
    if (!checkbox.value) return "no";       // 勾被取消了 → 等同于跳过
    return "yes";
  },


  /**
   * 把用户勾选的字段写回 Zotero（**唯一**的写入点）。
   *
   * 四条硬规矩（用户定的，改代码时不要绕过）：
   *   1. **只允许这几个字段**：date / DOI / volume / issue / pages，外加
   *      **creators（作者）**。标题**永远不写**。
   *      —— 作者原先也在这张禁令里（"留到后面看效果再说"）；本轮经用户同意
   *      开放，但只在这个"逐条确认"的流程里开，而且只填空（见第 2 条）。
   *      ⚠ 批量自动写入（`BATCH_ALLOWED_FIELDS`）**没有**跟着放开作者，
   *        原因写在那个常量上：批量是一次确认写 N 篇，作者却需要逐条看证据。
   *   2. **只填空**：写之前**再查一次**当前值。建议是"生成时为空"的，但用户可能
   *      在这中间手工补上了；此时一律跳过并说明。**没有覆盖这条路**（弹窗里
   *      也没有"覆盖"这个勾选项），所以"已有值"的字段永远不会被改掉。
   *      作者这一条**尤其**要复查：metafill 只在"一位作者都没有"时才建议，
   *      而这里是真正动数据的地方（作者的复查用 `getCreators()`，见下）。
   *   3. **只写用户勾过的**：picked 就是用户在弹窗里勾的那几条；declined 是用户
   *      明确不要的那几条（只进回报，**永远不写**）。
   *      这个函数只应该被 askApplyMeta 调用 —— 别在别处调用它。
   *   4. 写入只有 `item.setField(field, value)`（5 个单值字段）
   *      或 `item.setCreators(list)`（作者，见那个分支）＋ `await item.saveTx()`。
   *
   * 为什么不自己刷新条目列表：本项目踩过 `ItemTreeManager.refresh` 不存在，
   * 而 `saveTx()` 的写入通知会驱动 Zotero 自己重画，不需要我们插手。
   */
  applyMeta: async function (item, picked, declined) {
    const ALLOWED = ["date", "DOI", "volume", "issue", "pages", "creators"];
    const done = [], skipped = [], failed = [], notChosen = [];
    let changed = false;
    // 用户取消掉的那几条：只列出来，不做任何写入
    for (const sug of (declined || [])) {
      const f = String((sug && sug.field) || "");
      if (f) notChosen.push(f);
    }
    for (const sug of (picked || [])) {
      const field = String((sug && sug.field) || "");
      const value = String((sug && sug.value) || "");
      if (ALLOWED.indexOf(field) < 0) {
        failed.push(field + "：不在允许写入的字段里，已拒绝");
        continue;
      }
      // ================= 分支：作者（creators）=================
      //
      // 为什么作者**不能**和下面 5 个字段走同一条路（三条都是读过源码 +
      // 真机实测确认的，不是照印象写的）：
      //   · `creators` 根本不是 `setField` 认的字段 —— 实测
      //     `item.getField('creators')` **恒返回空串**（`Zotero.ItemFields.getID`
      //     查不到它），所以那条"写前复查当前值"的代码对作者**等于没查**；
      //   · 作者读写在 `item.getCreators()` / `item.setCreators(list)` 上
      //     （Zotero 10.0.5 的 item.js 第 1361 / 1450 行）；
      //   · `setCreators` 是**整表替换**语义：传进去的数组就是最终名单，
      //     多出来的旧作者会被删掉（实测：先写 2 位、再写 1 位，库里只剩 1 位）。
      //     ⚠ 对这个场景安全（服务端只在"一位作者都没有"时才建议），
      //     但**绝不能拿它做"追加"** —— 追加必须写成
      //     `setCreators([...getCreators(), ...新增])`，本函数不做追加。
      if (field === "creators") {
        const list = this.creatorValues(sug);
        if (!list.length) {
          // 没有结构化的 values 就不写：从 `value` 那根拼接串反推姓名是猜，
          // 猜错的代价是引文被毁掉（见 creatorValues 的说明）。
          failed.push("creators：建议里没有结构化的作者数据（values），"
                      + "不猜着写；请在 Zotero 里手工填");
          continue;
        }
        // 写前复查（只填空）—— **必须用 getCreators()**：
        // `getField('creators')` 恒为空，用它复查等于没查。
        // 只数"有名字的"：Zotero 允许存在一条空的作者行（编辑时留下的），
        // 把那条当成"已有作者"会让这一条永远补不上。
        let have = [];
        try { have = item.getCreators() || []; } catch (e) { have = []; }
        const named = have.filter((c) => c
          && (String(c.lastName || "").trim() || String(c.firstName || "").trim()));
        if (named.length) {
          skipped.push("creators：已有 " + named.length
                       + " 位作者，按只填空原则未覆盖（作者是整份名单，"
                       + "不做部分合并）");
          continue;
        }
        try {
          item.setCreators(list);
          done.push("creators = " + list.map(
            (c) => [c.firstName, c.lastName].filter((s) => s).join(" ")).join("、"));
          changed = true;
        } catch (e) {
          failed.push("creators：" + ((e && e.message) || e));
        }
        continue;
      }
      if (!value) {
        failed.push(field + "：建议值是空的，跳过");
        continue;
      }
      // 这个条目类型有没有这个字段 —— 没有的话 setField 会抛（"期刊"有 volume，
      // 而"网页""学位论文"就没有）。提前挡掉，别让用户看到一串英文异常。
      //
      // ⚠ 这不是理论问题：实测库里 `thesis` / `webpage` 两种类型**只有** date 与
      //   DOI（`itemTypeFields` 查出来的），而 metafill 是"只要当前为空就给建议"，
      //   所以它照样会给这两种类型提 volume/issue/pages（模型还常给个页码）。
      //   Zotero 存不了就是存不了 —— 如实说，并写清是哪种类型没有，别让用户
      //   以为"写失败"是程序坏了。
      try {
        const fid = Zotero.ItemFields.getID(field);
        if (!fid || !Zotero.ItemFields.isValidForType(fid, item.itemTypeID)) {
          let typeName = "";
          try { typeName = Zotero.ItemTypes.getName(item.itemTypeID) || ""; }
          catch (e) { typeName = ""; }
          failed.push(field + "：这个条目类型"
            + (typeName ? ("（" + typeName + "）") : "") + "没有该字段，Zotero 存不了");
          continue;
        }
      } catch (e) {
        failed.push(field + "：校验字段时出错（" + ((e && e.message) || e) + "）");
        continue;
      }
      // 写前复查（只填空）
      let cur = "";
      try { cur = String(item.getField(field) || "").trim(); } catch (e) { cur = ""; }
      if (cur) {
        skipped.push(field + "：已有值「" + cur + "」，按只填空原则未覆盖");
        continue;
      }
      try {
        item.setField(field, value);
        done.push(field + " = " + value);
        changed = true;
      } catch (e) {
        failed.push(field + "：" + ((e && e.message) || e));
      }
    }

    if (changed) {
      try {
        await item.saveTx();
      } catch (e) {
        // saveTx 失败 = 一条都没落库（值只在内存里）。这时不能报"成功"，
        // 否则用户以为写进去了，下次打开 Zotero 发现还是空的。
        Zotero.logError(e);
        this.alertDialog(
          "写回失败（什么都没写进 Zotero）",
          String((e && e.message) || e)
          + "\n\n刚才赋的值没有保存；重新打开这篇条目就会恢复原样。");
        return;
      }
    }

    // 回报：成功几项 / 跳过几项 / 失败几项 / 用户取消几项，以及**每项的实际结果**
    const body = [];
    if (done.length) {
      body.push("✔ 已写入 " + done.length + " 项：\n　" + done.join("\n　"));
    }
    if (skipped.length) {
      body.push("－ 跳过 " + skipped.length + " 项：\n　" + skipped.join("\n　"));
    }
    if (notChosen.length) {
      // 单独列出来：用户取消的不是"失败"，也不该看起来像被漏掉了
      body.push("－ 你取消了 " + notChosen.length + " 项（没有写入）：\n　"
                + notChosen.join("、"));
    }
    if (failed.length) {
      body.push("✗ 失败 " + failed.length + " 项：\n　" + failed.join("\n　"));
    }
    if (!done.length && !skipped.length && !failed.length && !notChosen.length) {
      body.push("（没有要处理的字段）");
    }
    body.push("提示：字段改了以后知识库里的索引不会自动跟着变；"
              + "要让新值进检索/权重，右键「重建知识库条目（这一篇）」。");
    this.alertDialog(
      changed ? ("元数据已写入 " + done.length + " 项") : "元数据没有写入任何项",
      "《" + this.itemLabel(item) + "》\n\n" + body.join("\n\n"));
  },
});

// ===== src/09-metabatch.js =====
/**
 * 09-metabatch.js —— 批量补全 / 批量写回 / 类型与标题修正
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 批量写回（面板发起）

  /**
   * 确保条目的 itemData 已经加载。
   *
   * ⚠ **这是真 Zotero 里实测出来的坑，不这么做会当场抛异常**：
   *   `Zotero.Items.get(id)` 有可能返回一个**已缓存但数据未加载**的对象
   *   （对象缓存里有它，但 itemData=false）。这时 `item.getField('title')`
   *   直接抛 `UnloadedDataException: Item data not loaded and field 'title'
   *   not set`，而 `setType()` 内部会 `_requireData('itemData')`，同样抛。
   *   实测：用 `Zotero.Items.unload(id)` 把条目从缓存卸掉再 `get` 回来，
   *   就是这个状态（探针脚本 probe3 的输出为证）。
   *
   * ⚠ 而且 `Zotero.Items.getAsync(id)` **救不了**：它的实现是
   *   `if (this._objectCache[id]) { toReturn.push(this._objectCache[id]) }`
   *   —— 缓存里有就直接还给你，不会补加载。唯一可靠的是显式 loadDataType。
   *
   * 加载顺序有讲究：`primaryData` 是其它类型的先决条件（dataObject.js 的
   * `_requireData` 里对其他类型会先递归要求 primaryData）。
   */
  ensureItemData: async function (item) {
    for (const dt of ["primaryData", "itemData", "creators"]) {
      try {
        if (item._loaded && item._loaded[dt]) continue;
      } catch (e) { /* _loaded 不可读就直接尝试加载 */ }
      try {
        await item.loadDataType(dt);
      } catch (e) {
        Zotero.debug("[zotero-kb] 加载 " + dt + " 失败（继续尝试）：" + e);
      }
    }
    return item;
  },

  /**
   * 批量写回允许的字段。
   *
   * ⚠ **故意与 `applyMeta` 的 ALLOWED 不一致**（那里多了 `creators`）：
   *   · 逐条流程：用户一条条看见建议值、证据、作者名单与"没列全"的警示，
   *     再一条条勾 —— 作者可以在这里写（仍是只填空、整份名单）；
   *   · 批量流程：**一次确认写 N 篇**，看的是汇总清单（标题 + `字段 = 值`），
   *     没有逐条证据可核。作者是唯一"错一个字符就毁掉一条引文"的字段，
   *     放进来等于让用户在没细看的情况下批量改作者。
   * 所以批量仍然只写这 5 个单值字段；要补作者就单独右键那一条走逐条流程。
   * 两端的分工在界面上是**明说**的（`buildMetaPlan` 会把 creators 记进
   * "没有列进来的"，用户看得见，不会以为是漏了）。
   */
  BATCH_ALLOWED_FIELDS: ["date", "DOI", "volume", "issue", "pages"],


  /**
   * 【面板「一键采用」的执行端 —— 见下方 metaFillMany 的说明】
   *
   * 计划（plan）形态：
   *   {items: [{key, title, fields: [{field, value}]}]}
   * 返回：逐篇的 成功/跳过/失败 明细 + 总计。
   *
   * 四道红线（**和用户逐条确认的 applyMeta 同源，但白名单更窄**）：
   *   ① 只写白名单字段（日期/DOI/卷/期/页码）—— 作者与标题不在这里写
   *      （作者本轮开给了逐条流程，理由见 BATCH_ALLOWED_FIELDS 的注释）；
   *   ② 只填空 —— 写前**再查一次**当前值，已有值一律跳过并说明；
   *   ③ 字段对当前条目类型不合法就不写（说清是哪种类型没有，别抛英文异常）；
   *   ④ 逐篇回报成功/跳过/失败，直接显示给用户，不做"看起来成功"的模糊话。
   *
   * 确认发生在调用方（`metaFillMany` 的汇总确认框）。这个函数只负责执行。
   */
  applyMetaBatch: async function (plan) {
    var self = ZoteroKB;
    const items = (plan && plan.items) || [];
    const out = { total_items: items.length, written: 0, skipped: 0,
                  failed: 0, not_found: 0, details: [] };
    const libID = Zotero.Libraries.userLibraryID;
    for (const spec of items) {
      const key = String((spec && spec.key) || "");
      const row = { key: key, written: [], skipped: [], failed: [], saved: false };
      let item = null;
      try {
        item = Zotero.Items.getByLibraryAndKey(libID, key);
      } catch (e) {
        item = null;
      }
      if (!item) {
        row.failed.push("库里找不到这个条目（可能刚被删掉/换库了）");
        out.not_found++;
        out.details.push(row);
        continue;
      }
      try {
        await self.ensureItemData(item);
      } catch (e) {
        row.failed.push("读条目数据失败：" + ((e && e.message) || e));
        out.failed++;
        out.details.push(row);
        continue;
      }
      for (const f of (spec.fields || [])) {
        const field = String((f && f.field) || "");
        const value = String((f && f.value) || "").trim();
        if (self.BATCH_ALLOWED_FIELDS.indexOf(field) < 0) {
          row.failed.push(field + "：不在允许自动写入的字段里（"
                          + self.BATCH_ALLOWED_FIELDS.join("/") + "），已拒绝");
          continue;
        }
        if (!value) {
          row.failed.push(field + "：建议值是空的");
          continue;
        }
        // 这个条目类型有没有这个字段（"网页"类型就没有 volume/pages，
        // setField 会抛英文异常，用户看不懂）
        let fid = 0;
        try { fid = Zotero.ItemFields.getID(field); } catch (e) { fid = 0; }
        if (!fid) {
          row.failed.push(field + "：Zotero 里没有这个字段");
          continue;
        }
        try {
          if (!Zotero.ItemFields.isValidForType(fid, item.itemTypeID)) {
            let typeName = "";
            try { typeName = Zotero.ItemTypes.getName(item.itemTypeID) || ""; }
            catch (e) { typeName = ""; }
            row.failed.push(field + "：条目类型"
              + (typeName ? ("（" + typeName + "）") : "") + "没有这个字段");
            continue;
          }
        } catch (e) {
          row.failed.push(field + "：校验字段时出错（" + ((e && e.message) || e) + "）");
          continue;
        }
        // 写前复查（只填空）：建议是"生成时为空"的，用户可能在这中间手工补上了
        let cur = "";
        try { cur = String(item.getField(field) || "").trim(); }
        catch (e) { cur = ""; }
        if (cur) {
          row.skipped.push(field + "：已有值「" + cur.slice(0, 40) + "」，按只填空未覆盖");
          continue;
        }
        try {
          item.setField(field, value);
          row.written.push(field + " = " + value);
        } catch (e) {
          row.failed.push(field + "：" + ((e && e.message) || e));
        }
      }
      if (row.written.length) {
        try {
          await item.saveTx();
          row.saved = true;
          out.written += row.written.length;
        } catch (e) {
          // saveTx 失败 = 这一篇一个字段都没落库。这时绝不能报"成功"，
          // 否则用户以为写进去了，下次打开 Zotero 发现还是空的。
          Zotero.logError(e);
          row.failed.push("保存失败（这一篇一个字段都没写进去）："
                          + ((e && e.message) || e));
          row.written = [];
        }
      }
      out.skipped += row.skipped.length;
      out.failed += row.failed.length;
      out.details.push(row);
    }
    Zotero.debug("[zotero-kb] 批量写回结束：写入 " + out.written
                 + " 项，跳过 " + out.skipped + " 项，失败 " + out.failed
                 + " 项，找不到 " + out.not_found + " 篇");
    return out;
  },


  // ================================================================ 批量补全（多选）

  /**
   * 多选时的「一键采用」：**先汇总一份清单、一次确认、再批量写**。
   *
   * 形态是用户拍板的（2026-10-03），两条理由：
   *   ① 写库留在**插件里** —— 插件就在 Zotero 进程内，`item.setField + saveTx`
   *      是官方写法。让管理面板（另一个 Python 进程）发任务过来，要多一套
   *      "派发 → 领取 → 回执 → 超时"的状态机，成本高、容易做成半成品。
   *   ② 现有的右键菜单本来就是**多选逐篇串行**（见菜单里那个 runOne），
   *      把"逐篇弹窗"换成"先汇总、一次确认"改动面最小。
   *
   * 单篇（只选了一条）**不走这里** —— 一篇时用户要逐条核对证据，
   * 一次全采用反而看不到证据（分流在菜单的 handler 里，见那里的注释）。
   *
   * ⚠ 批量**只写 `confidence === "high"` 的字段建议**：
   *   · 低置信（本地小模型给的）一条都不写、也不列进清单；
   *   · 作者（creators）与标题**不在这个函数的白名单里**（见
   *     BATCH_ALLOWED_FIELDS）—— 本轮把作者开给了**逐条**流程，
   *     批量仍然不开：一次确认写 N 篇时没有逐条证据可核，而作者是
   *     "错一个字符就毁掉一条引文"的字段；
   *   · 类型/标题修正**绝不进批量**：改类型是改身份，必须逐条看
   *     "现在是什么 → 要改成什么"（见 askApplyTypeFix）。批量里遇到
   *     这种条目只**记下来提醒**，不替用户改。
   *
   * ⚠ 批量取建议时**不调本地模型**（use_model:false）：批量只写高置信，
   *   而模型给的永远是低置信 —— 调它等于白等十几分钟占着 GPU，一条也用不上。
   */
  metaFillMany: async function (items) {
    var self = ZoteroKB;
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    self.notify("正在读 PDF 首页找元数据…",
                list.length + " 篇（纯规则，几秒）", null, false);

    const results = [];
    const suspects = [];
    for (let i = 0; i < list.length; i++) {
      const it = list[i];
      let res = null;
      try {
        // W3 线2.5（2026-10-14）：桥优先（`meta.fillOne`），失败退回老 HTTP。
        try {
          res = await self.kbAdminHostRoute("POST", "/metafill",
                                           { key: it.key, use_model: false });
        } catch (e0) {
          res = await self.request("POST", "/metafill",
                                   { key: it.key, use_model: false });
        }
      } catch (e) {
        res = { ok: false, error: String((e && e.message) || e) };
      }
      results.push({ item: it, res: res });
      // 顺手记下"类型/标题可能被网页污染"的条目 —— 但**不批量改**，
      // 只在最后的回报里提醒用户单独去处理（见函数头第 3 条）。
      if (self.itemIsWebSaved(it)) {
        try {
          // W3 线2.5：桥优先（`meta.typefixOne`），失败退回老 HTTP。
          let tf = null;
          try {
            tf = await self.kbAdminHostRoute("POST", "/typefix",
                                            { key: it.key, use_network: false });
          } catch (e0) {
            tf = await self.request("POST", "/typefix",
                                    { key: it.key, use_network: false });
          }
          if (tf && tf.ok !== false && (tf.fixes || []).length) {
            suspects.push({ item: it, tf: tf });
          }
        } catch (e) { /* 检查失败不影响补字段这件事 */ }
      }
    }

    // ---- 汇总：只要高置信 + 白名单字段
    const built = self.buildMetaPlan(results);

    if (!built.plan.items.length) {
      const lines = ["这 " + list.length + " 篇里没有可自动写入的高置信建议。", ""];
      if (built.extras.low) {
        lines.push("有 " + built.extras.low + " 条低置信建议（本地小模型给的）——"
                   + "按规矩不自动写，要自己核对。");
      }
      if (Object.keys(built.extras.otherFields).length) {
        lines.push("另有：" + Object.keys(built.extras.otherFields).map(
          (k) => k + "（" + built.extras.otherFields[k] + " 条）").join("、")
          + " —— 这些字段不在自动写入的白名单里。");
      }
      if (suspects.length) {
        lines.push("");
        lines.push("另外有 " + suspects.length + " 篇的类型/标题可能被网页污染，"
                   + "那类改动必须逐条确认：请单独右键那一条。");
      }
      self.alertDialog("没有可自动写入的建议", lines.join("\n"));
      return;
    }

    const choice = self.askApplyMetaBatch(built.plan, built.extras, list.length);
    if (choice === 2) {
      self.notify("已取消", "什么都没有写", null, false);
      return;
    }
    if (choice === 1) {
      // 「逐篇确认」：回到**单篇那套弹窗**（用户要逐条看证据、逐条勾选）。
      // ⚠ 说明白这一轮没调模型：所以这里只有规则建议，没有模型建议。
      for (const r of results) {
        if (r.res && r.res.ok !== false) self.askApplyMeta(r.item, r.res);
      }
      return;
    }
    const nFields = built.plan.items.reduce((a, x) => a + x.fields.length, 0);
    self.notify("正在写入 Zotero…", nFields + " 个字段", null, false);
    let out;
    try {
      out = await self.applyMetaBatch(built.plan);
    } catch (e) {
      Zotero.logError(e);
      self.alertDialog("批量写入失败", String((e && e.message) || e)
                       + "\n\n没有确认写入成功的字段请当作没写。");
      return;
    }
    self.reportMetaBatch(out, built.extras);
  },


  /**
   * 把逐篇的 `/metafill` 结果汇总成"批量写入计划" + "没写进去的那些为什么"。
   *
   * 抽成独立函数有两个理由：
   *   ① metaFillMany 的流程本身已经够长，汇总规则夹在中间看不出来；
   *   ② **它能在真 Zotero 里被单独验证** —— 弹窗那部分（confirmEx）没法自动化，
   *      但"只收高置信 / 只收白名单字段 / 低置信要计数"这些**规则**可以，
   *      用合成数据直接调它就行（见本轮实测脚本）。
   *
   * 入参 `results`：`[{item, res}]`，res 是服务端 /metafill 的响应。
   * 返回 `{plan, extras}`；plan 直接就是 applyMetaBatch 要的入参。
   */
  buildMetaPlan: function (results) {
    var self = ZoteroKB;
    const plan = { items: [] };
    const extras = { low: 0, otherFields: {}, noneCount: 0, suspects: [] };
    for (const r of (results || [])) {
      const item = r && r.item;
      const res = r && r.res;
      if (!item) continue;
      if (!res || res.ok === false) { extras.noneCount++; continue; }
      const high = [];
      for (const s of (res.suggestions || [])) {
        const field = String((s && s.field) || "");
        if (s.confidence !== "high") { extras.low++; continue; }
        if (self.BATCH_ALLOWED_FIELDS.indexOf(field) < 0) {
          extras.otherFields[field] = (extras.otherFields[field] || 0) + 1;
          continue;
        }
        if (!String(s.value || "").trim()) continue;
        high.push({ field: field, value: String(s.value) });
      }
      if (high.length) {
        plan.items.push({ key: item.key, title: item.title, fields: high });
      } else {
        extras.noneCount++;
      }
    }
    return { plan: plan, extras: extras };
  },


  /**
   * 批量写入的**汇总确认框**。
   *
   * 三个按钮为什么是这三个（用户给了两个候选组合，这里选后者并说明理由）：
   *   · 「全部采用」—— 主操作，默认按钮；
   *   · 「逐篇确认…」—— 退回到单篇那套弹窗。这一档比"只采用高置信"有用得多：
   *     清单里**本来就只有高置信**（低置信连列都没列），所以"只采用高置信"
   *     按下去和"全部采用"是同一件事，等于一个没有区别的按钮；而"逐篇确认"
   *     给了不放心的人一条能逐条看证据的路（这正是单篇流程的价值）；
   *   · 「取消」—— 什么都不写。
   *
   * 返回 0=全部采用 / 1=逐篇确认 / 2=取消。
   */
  askApplyMetaBatch: function (plan, extras, totalItems) {
    const nFields = plan.items.reduce((a, x) => a + x.fields.length, 0);
    const lines = [];
    lines.push("将写入 " + plan.items.length + " 篇 / " + nFields + " 个字段"
               + "（只含高置信建议）。");
    lines.push("");
    for (const it of plan.items) {
      lines.push("· " + String(it.title || it.key).slice(0, 46)
                 + "　[" + it.key + "]");
      for (const f of it.fields) {
        lines.push("　　" + f.field + " = " + String(f.value).slice(0, 60));
      }
    }
    lines.push("");
    const notes = [];
    if (extras && extras.low) {
      notes.push("· 有 " + extras.low + " 条「低置信」建议（本地小模型给的）"
                 + "没有列进来，也不会写。");
    }
    const of = (extras && extras.otherFields) || {};
    if (Object.keys(of).length) {
      notes.push("· 另有 " + Object.keys(of).map(
        (k) => k + "（" + of[k] + " 条）").join("、")
        + "：不在批量自动写入的白名单里。作者（creators）要在"
        + "「逐条确认」里写（每条都看得见姓名与「有没有列全」），"
        + "标题一律不写。");
    }
    if (extras && extras.noneCount) {
      notes.push("· 有 " + extras.noneCount + " 篇没有任何可写的高置信建议。");
    }
    if (extras && (extras.suspects || []).length) {
      notes.push("· ⚠ 另有 " + extras.suspects.length + " 篇的「类型/标题」"
                 + "可能被网页污染（例如先存了网页、后来挂上 PDF）。"
                 + "这类改动必须逐条看「现在是什么 → 要改成什么」再定，"
                 + "「不会」在这批里改 —— 请单独右键那一条。");
    }
    if (notes.length) { lines.push("没有列进来的："); lines.push(...notes); lines.push(""); }
    lines.push("规则仍然是「只填空」：写之前会再查一次当前值，"
               + "已有值的一律跳过、不覆盖。");
    lines.push("");
    lines.push("「全部采用」= 按上面一次写完；「逐篇确认」= 回到一条条勾的弹窗"
               + "（这一轮没调本地模型，所以逐篇窗口里只有规则建议）；"
               + "「取消」= 什么都不写。");

    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_2 * ps.BUTTON_TITLE_IS_STRING;
    return ps.confirmEx(
      win, "文献知识库 · 批量采用高置信建议",
      lines.join("\n"), flags,
      "全部采用", "逐篇确认…", "取消", null, {}
    );
  },


  /** 批量写入的结果回报（成功/跳过/失败 + 没写进去的那些为什么）。 */
  reportMetaBatch: function (out, extras) {
    const body = [];
    const doneItems = (out.details || []).filter((d) => (d.written || []).length);
    body.push("✔ 写入 " + out.written + " 项，涉及 " + doneItems.length + " 篇");
    if (out.skipped) body.push("－ 跳过 " + out.skipped + " 项（已有值，按只填空）");
    if (out.failed) body.push("✗ 失败 " + out.failed + " 项");
    if (out.not_found) body.push("✗ 有 " + out.not_found + " 篇在库里找不到了");
    body.push("");
    for (const d of (out.details || [])) {
      const mark = (d.written || []).length ? "✔" : ((d.skipped || []).length ? "－" : "✗");
      body.push(mark + " " + d.key
                + "　写入 " + (d.written || []).length
                + " / 跳过 " + (d.skipped || []).length
                + " / 失败 " + (d.failed || []).length);
      for (const x of (d.written || []).slice(0, 8)) body.push("　　" + x);
      for (const x of (d.skipped || []).slice(0, 4)) body.push("　　－ " + x);
      for (const x of (d.failed || []).slice(0, 4)) body.push("　　✗ " + x);
    }
    const of = (extras && extras.otherFields) || {};
    if ((extras && extras.low) || Object.keys(of).length) {
      body.push("");
      body.push("没有写进去的（这是设计如此，不是漏了）：");
      if (extras && extras.low) {
        body.push("· " + extras.low + " 条低置信建议（模型给的，会编）");
      }
      for (const k of Object.keys(of)) {
        body.push("· " + k + "（" + of[k] + " 条）：不在自动写入白名单里");
      }
    }
    if (!out.written) {
      body.push("");
      body.push("⚠ 这次一个字段都没写进去。如果「失败」里都是"
                + "「条目类型没有该字段」，说明这些条目需要先改类型"
                + "（在列表里看它们的类型是不是「网页」）。");
    }
    body.push("");
    body.push("提示：字段改了以后知识库索引不会自动跟着变；"
              + "要让新值进检索，右键「重建知识库条目（这一篇）」。");
    this.alertDialog(
      out.written ? ("已批量写入 " + out.written + " 项") : "批量写入没有成功",
      body.join("\n"));
  },

  // ================================================================ 类型/标题修正
  /** 常规文献类型（这些类型不该被当成"网页存了、后来挂 PDF"）。 */
  PAPER_TYPES: ["journalArticle", "conferencePaper", "thesis", "book",
                "bookSection", "report", "preprint", "manuscript",
                "newspaperArticle", "magazineArticle", "patent", "standard",
                "document", "presentation", "conferencePaper"],

  /** 浏览器"保存网页"最常产生的类型（只有这几种才值得怀疑"它其实是论文"）。 */
  WEBISH_TYPES: ["webpage", "blogPost", "forumPost"],


  /**
   * 本地初判：这条像不像"先存了网页、后来挂上 PDF"。
   *
   * 为什么这一步放在插件里而不是等服务器回答：它只用**本地即时可得**的信息
   * （条目类型 + 有没有 PDF 附件），比一次 HTTP 往返快得多。绝大多数条目
   * 这一步就返回 false，于是普通条目右键「补全元数据」的流程**一点没变**
   * （不发多余的请求、不弹多余的窗）—— 这是"不给正常路径加成本"的设计。
   */
  itemIsWebSaved: function (item) {
    try {
      if (!item || !item.isRegularItem || !item.isRegularItem()) return false;
      const t = Zotero.ItemTypes.getName(item.itemTypeID) || "";
      if (this.WEBISH_TYPES.indexOf(t) < 0) return false;
      return this.itemHasPdf(item);
    } catch (e) {
      return false;
    }
  },


  /**
   * 类型/标题修正的确认框。
   *
   * ⚠ 与 applyMeta 的弹窗**分开**是有意的，不是偷懒：
   *   · 两个操作的语义完全不同 —— 一个是"填空"，一个是"改身份"（类型/标题）；
   *   · applyMeta 有四条用户定过的红线（只写白名单字段、只填空、不覆盖、
   *     标题永远不写），把 setType/标题塞进同一个函数会把那几条红线搅浑；
   *   · 改类型还得让用户看见"会丢哪些字段"，那是 applyMeta 没有的概念。
   *   但入口仍然是同一个右键菜单项 —— 用户不用多学一个菜单。
   *
   * 返回 true = 用户要求执行；false = 跳过。
   */
  askApplyTypeFix: function (item, tf) {
    const lines = [];
    const fixes = tf.fixes || [];
    lines.push("《" + this.itemLabel(item) + "》");
    lines.push("");
    lines.push("这条在 Zotero 里是「" + (tf.item_type || "未知")
               + "」，却挂着 PDF —— 看起来是「先保存了网页、后来才把 PDF 拖进来」，");
    lines.push("所以类型和标题都还带着网页的样子。检测到这些可以修正：");
    lines.push("");
    fixes.forEach((f, i) => {
      if (f.kind === "type") {
        lines.push((i + 1) + ". 类型：" + f.current + "  →  " + f.value
                   + "　〔" + f.source + "〕");
      } else {
        lines.push((i + 1) + ". 标题：现在　" + f.current);
        lines.push("   　　　　改成　" + f.value);
        lines.push("   　　　　〔" + f.source + "〕");
      }
      if (f.rule) lines.push("   依据：" + f.rule);
      const ev = f.evidence || {};
      if (ev.text) lines.push("   证据：" + String(ev.text).slice(0, 200));
      lines.push("");
    });
    const drop = tf.will_drop || [];
    if (drop.length) {
      // ⚠ 这一条是 Zotero 自己的行为（读了 item.js 的 setType 与 itemBox.js
      //   的改动确认过）：改类型时，**新类型没有的字段会被清空**。
      //   实测网页 → 期刊论文时，`网站名` 会被 Zotero 自动搬到 `期刊名`，
      //   别的网页独有字段会消失。用户必须在写之前看到这件事。
      lines.push("⚠ 改类型会清掉这些字段（新类型没有它们）：");
      lines.push("　　" + drop.join("、"));
      lines.push("（Zotero 会把能对应上的字段搬到新类型，例如"
                 + "「网站名」→「期刊名」——如果那是站点名，改完请手工清掉）");
      lines.push("");
    }
    lines.push("类型/标题和日期卷期不同：它们决定这篇在列表里「长什么样」，");
    lines.push("改错了不好发现，所以这里「不会自动做」，要你点确认。");
    lines.push("");
    lines.push("「修正」= 按上面写入；「跳过」= 只补字段、不动类型和标题。");

    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
    const choice = ps.confirmEx(
      win, "文献知识库 · 类型/标题修正建议",
      lines.join("\n"), flags,
      "修正", "跳过（只补字段）", null, null, {}
    );
    return choice === 0;
  },


  /**
   * 应用类型/标题修正（**用户确认之后**才调用）。
   *
   * 改类型的真实行为（**在真 Zotero 里实测过，不是猜的**，见探针脚本）：
   *   · `item.setType(itemTypeID)` 是同步方法，签名 `(itemTypeID, loadIn)`；
   *   · 它**不会**把"新类型也有的字段"清掉（实测 title/DOI/date/url/abstractNote
   *     全部保留）；真正被清掉的是"新类型没有的字段" —— 而且它还会做**基字段
   *     搬运**：实测 `网站名(websiteTitle)` 被搬进了 `期刊名(publicationTitle)`；
   *   · 作者里"新类型不认的创作者类型"会被改成新类型的主创作者类型；
   *   · 内部会 `this._requireData('itemData')`，数据没加载时**抛异常**
   *     （所以必须先 ensureItemData）。
   * Zotero 自己的"改变条目类型"菜单（itemBox.js）就是：先算
   * `getFieldsNotInType()` 提示用户，再 `setType()` + `saveTx()`。
   *
   * 本函数照抄那套，并按用户的要求多做两件事：
   *   ① **先把所有字段值取出来**，改完类型再把"新类型仍然合法"的写回去
   *      —— 这样即使 Zotero 将来改了清字段的策略，也不会丢值；
   *   ② 改完**重新从库里读一遍**类型与标题验证，结果如实回报（不信内存里的值）。
   */
  applyTypeFix: async function (item, tf) {
    var self = ZoteroKB;
    const key = item.key;
    const fixes = tf.fixes || [];
    const typeFix = fixes.find((f) => f.kind === "type") || null;
    const titleFix = fixes.find((f) => f.kind === "title") || null;
    const report = { key: key, changed: [], skipped: [], failed: [],
                     dropped: [], verified: null };

    await self.ensureItemData(item);

    // ---- ① 先备份所有字段值（改类型之前）
    let backup = {};
    try {
      for (const name of (Zotero.ItemFields.getItemTypeFields(item.itemTypeID)
                          .map((fid) => Zotero.ItemFields.getName(fid)) || [])) {
        try {
          const v = item.getField(name);
          if (v !== "" && v !== null && v !== undefined && v !== false) {
            backup[name] = String(v);
          }
        } catch (e) { /* 该类型没有这个字段 */ }
      }
    } catch (e) {
      report.failed.push("备份字段失败：" + ((e && e.message) || e));
    }

    // ---- ② 改类型
    let newTypeName = "";
    if (typeFix) {
      newTypeName = String(typeFix.value || "");
      const tid = Zotero.ItemTypes.getID(newTypeName);
      if (!tid) {
        report.failed.push("Zotero 里没有「" + newTypeName + "」这个类型");
      } else {
        // 权威的"会丢哪些字段"由 Zotero 现算（服务端给的只是预览）
        let willDrop = [];
        try { willDrop = item.getFieldsNotInType(tid) || []; }
        catch (e) { willDrop = []; }
        try {
          item.setType(tid);
          report.changed.push("类型 " + tf.item_type + " → " + newTypeName);
          report.dropped = willDrop.map((fid) => {
            try { return Zotero.ItemFields.getName(fid); } catch (e) { return String(fid); }
          });
        } catch (e) {
          report.failed.push("改类型失败：" + ((e && e.message) || e));
        }
      }
    }

    // ---- ③ 把备份里"仍然合法"的字段写回（用户要求的保险）
    if (report.changed.length) {
      let restored = 0;
      for (const name of Object.keys(backup)) {
        let fid = 0;
        try { fid = Zotero.ItemFields.getID(name); } catch (e) { fid = 0; }
        if (!fid) continue;
        try {
          if (!Zotero.ItemFields.isValidForType(fid, item.itemTypeID)) continue;
          if (String(item.getField(name) || "").trim()) continue;   // 已经有值就别覆盖
          item.setField(name, backup[name]);
          restored++;
        } catch (e) { /* 单字段失败不影响其它 */ }
      }
      if (restored) report.changed.push("回写了 " + restored + " 个仍然适用的字段值");
    }

    // ---- ④ 改标题（**只在这条是"标题被污染"的建议上**；不做任何别的标题改动）
    if (titleFix) {
      const want = String(titleFix.value || "").trim();
      let cur = "";
      try { cur = String(item.getField("title") || "").trim(); } catch (e) { cur = ""; }
      if (!want) {
        report.skipped.push("标题：建议值是空的");
      } else if (want === cur) {
        report.skipped.push("标题：已经和它一样了");
      } else {
        try {
          item.setField("title", want);
          report.changed.push("标题已改（原 " + cur.length + " 字 → 新 "
                              + want.length + " 字）");
        } catch (e) {
          report.failed.push("改标题失败：" + ((e && e.message) || e));
        }
      }
    }

    // ---- ⑤ 保存
    if (report.changed.length) {
      try {
        // undoAction 用的是 Zotero 自己的那个字符串（itemBox.js 改类型时同款），
        // 这样用户在 Zotero 里按 Ctrl+Z 能撤销，不用手工改回去。
        await item.saveTx({ undoAction: "undo-action-change-type" });
      } catch (e) {
        Zotero.logError(e);
        report.failed.push("保存失败（类型/标题都没写进去）："
                           + ((e && e.message) || e));
        report.changed = [];
      }
    }

    // ---- ⑥ 验证：**重新从库里读**一遍，不信内存里的值
    try {
      const fresh = await Zotero.Items.getAsync(item.id);
      if (fresh) {
        await self.ensureItemData(fresh);
        report.verified = {
          itemType: Zotero.ItemTypes.getName(fresh.itemTypeID),
          titleLen: String(fresh.getField("title") || "").length,
          titleHasSiteSuffix: /\|\s*IEEE\s+Xplore\s*$/i.test(
            String(fresh.getField("title") || "")),
          publicationTitle: String(fresh.getField("publicationTitle") || "").slice(0, 60),
        };
      }
    } catch (e) {
      report.verified = { error: String((e && e.message) || e) };
    }
    Zotero.debug("[zotero-kb] 类型/标题修正完成：" + JSON.stringify(report));
    return report;
  },


  /** 把 applyTypeFix 的结果讲给用户听（成功几项 / 跳过几项 / 失败几项 + 验证）。 */
  reportTypeFix: function (report) {
    const body = [];
    if (report.changed.length) {
      body.push("✔ 已改 " + report.changed.length + " 项：\n　"
                + report.changed.join("\n　"));
    }
    if (report.skipped.length) {
      body.push("－ 跳过 " + report.skipped.length + " 项：\n　"
                + report.skipped.join("\n　"));
    }
    if (report.failed.length) {
      body.push("✗ 失败 " + report.failed.length + " 项：\n　"
                + report.failed.join("\n　"));
    }
    if (report.dropped && report.dropped.length) {
      body.push("改类型时 Zotero 清掉了这些字段：\n　" + report.dropped.join("、"));
    }
    const v = report.verified || {};
    if (v.error) {
      body.push("⚠ 改完重新读库验证失败：" + v.error);
    } else if (v.itemType) {
      body.push("改完重新读了一遍库，确认现在是：\n　类型 = " + v.itemType
                + "　标题 " + v.titleLen + " 字"
                + (v.titleHasSiteSuffix ? "（⚠ 标题尾部仍有站点后缀！）"
                                        : "（标题尾部没有站点后缀了）")
                + (v.publicationTitle
                   ? ("\n　期刊名 = " + v.publicationTitle
                      + "（如果这是站点名，请手工清掉）") : ""));
    }
    if (!body.length) body.push("（什么都没改）");
    body.push("提示：改了以后知识库索引不会自动跟着变，"
              + "要让新值进检索，右键「重建知识库条目（这一篇）」。");
    this.alertDialog(
      report.changed.length ? ("类型/标题已修正 " + report.changed.length + " 项")
                            : "类型/标题没有改动",
      body.join("\n\n"));
  },

  // ================================================================ 获取文献（DSH 搜好，Zotero 自己抓）

  /** 一次最多收几篇 —— 防止上游发来一个几百条的列表把 Zotero 卡住。 */
  ACQUIRE_MAX: 20,

  /**
   * 从 DSH 导入的条目 id → 时间戳。`handleNewItem` 见到就跳过一次自动分类，
   * 免得一次导入十几篇就弹十几个分类框（理由写在 handleNewItem 里）。
   * 用时间戳是为了让标记能自己过期，不会永久吞掉以后手工添加时的分类建议。
   */
  acquiredIDs: {},
});

// ===== src/10-acquire.js =====
/**
 * 10-acquire.js —— 获取文献：DSH 搜好并挑好，由 Zotero 自己抓进库
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  /**
   * DSH 侧搜好、用户挑过之后，把 DOI 列表交给 **Zotero 自己的抓取链路**入库。
   *
   * 为什么这件事必须放在插件里，而不是在 Python 侧下载 PDF：
   *   ① **网络身份**：请求是从 Zotero 进程发出的，用的是用户机器**当下**的
   *      网络环境 —— 校园网出口 IP、机构订阅、出版社的会话全都天然生效。
   *      Python 侧去下就把这层身份丢了，所以那条路现在只当兜底。
   *   ② **`addAvailableFile` 是 Zotero 自带的"查找可用的 PDF"**（实测
   *      `Zotero.Attachments.addAvailableFile` 是 function）。它自带一整套
   *      resolver：DOI 落地页 → item 的 url → 开放获取源（Unpaywall）→ PMC
   *      → 用户自定义 resolver。自己写爬虫不可能比它全。
   *   ③ **元数据交给 Zotero 的翻译器生态**（本机实测走 "DOI Content
   *      Negotiation"），比解析搜索结果网页可靠得多。
   *
   * ⚠ **边界（不要放宽）**：这里用的是**用户已有的访问权限**，不是
   *   "绕过付费墙"。改 URL 骗计费、影子图书馆、盗用凭证 —— 一律不做。
   *   走不通就如实回报"没找到"，让用户自己用浏览器连接器抓。
   *
   * 任务形态：`kind = "acquire"`，`code` 是一段 JSON（见 parseAcquirePayload）。
   */
  runAcquire: async function (rawPayload) {
    var self = ZoteroKB;
    const t0 = Date.now();

    let p;
    try {
      p = self.parseAcquirePayload(rawPayload);
    } catch (e) {
      return { ok: false, error: "载荷解析失败：" + e };
    }
    if (!p.items.length) return { ok: false, error: "没有可用的 DOI" };
    if (p.items.length > self.ACQUIRE_MAX) {
      return { ok: false,
               error: "一次最多 " + self.ACQUIRE_MAX + " 篇，收到 " + p.items.length + " 篇。"
                      + "请分批。" };
    }

    const libraryID = Zotero.Libraries.userLibraryID;
    const col = self.acquireTargetCollection(p.collectionID);

    // 清掉过期的导入标记（正常情况下 handleNewItem 会自己删；这里兜住
    // "autoProcess 关着、Notifier 压根没回调"那种情况，免得对象无限长大）。
    const now0 = Date.now();
    for (const k of Object.keys(self.acquiredIDs || {})) {
      if (now0 - self.acquiredIDs[k] > 120000) delete self.acquiredIDs[k];
    }

    const report = {
      ok: true,
      collection: col ? { id: col.id, name: col.name } : null,
      findPdf: p.findPdf,
      dryRun: p.dryRun,
      results: [],
      warnings: [],
    };

    self.notify("正在向 Zotero 抓取文献…",
                p.items.length + " 篇，正在解析元数据。", null, false);

    // ---- 第 1 步：去重 + 按 DOI 抓元数据（**只翻译不保存**）
    //
    // 为什么先"预览"一遍再问用户：① 能在**不写任何东西**的前提下知道
    // Zotero 到底认不认这些 DOI（认不出就别让用户白点一次"添加"）；
    // ② Zotero 抓回来的才是权威元数据，比搜索结果里的标题可靠。
    // 实测 `translate({libraryID:false})` 返回的是普通对象（不是 Zotero.Item），
    // 正好可以直接喂给 `item.fromJSON()` —— 于是**只联网一次**就够。
    const doiIndex = await self.doiIndex(libraryID);
    const plan = { good: [], bad: [], dup: [] };

    for (const it of p.items) {
      const hit = doiIndex[String(it.doi).toLowerCase()];
      if (hit) {
        plan.dup.push({ doi: it.doi, title: it.title, key: hit });
        continue;
      }
      let r;
      try {
        r = await self.resolveByDoi(it.doi);
      } catch (e) {
        r = { ok: false, why: "error", detail: String(e) };
      }
      if (r.ok) {
        plan.good.push({ doi: it.doi, src_title: it.title, json: r.json,
                         translators: r.translators });
      } else {
        plan.bad.push({ doi: it.doi, title: it.title, why: r.why,
                        detail: r.detail || "", translators: r.translators || [] });
      }
    }

    // ---- 干跑：到这儿就停。**一个字节都不写库。**
    //      "先看看抓不抓得到"是用户很可能要的动作，必须真的安全 ——
    //      原来这里会直接落库（只是不弹确认框），那是错的。
    if (p.dryRun) {
      report.dryRun = true;
      report.added = plan.good.length;
      report.results = plan.good.map((g) => {
        const d = self.describeAcquireJson(g.json);
        return { doi: g.doi, title: d.title, byline: d.byline, venue: d.venue,
                 status: "dry", itemKey: "", pdf: null,
                 translators: g.translators };
      });
      report.duplicates = plan.dup;
      report.failed = plan.bad.map((b) => ({ doi: b.doi, why: b.why,
                                             detail: b.detail }));
      report.ms = Date.now() - t0;
      report.note = "干跑：没有写入 Zotero。";
      self.closeProgress();
      return report;
    }

    // ---- 第 2 步：过一遍（**默认不问** —— 确认在 DSH 对话里做，见 PREFS 的注释）
    const ask = self.getPref(self.PREFS.acquireConfirm, false);
    let go = true;
    if (ask) {
      const ans = self.askAcquire(plan, col);
      go = ans.go;
      if (ans.remember) {
        try { Zotero.Prefs.set(self.PREFS.acquireConfirm, false); } catch (e) { /* ignore */ }
      }
    }
    if (!go) {
      report.ok = false;
      // ⚠ 要区分"用户点了取消"和"压根没有可存的" —— 后者是 alertDialog
      //   （没有按钮可选），报成"用户取消了"会让用户莫名其妙：
      //   他根本没看到过确认框。DSH 那边按这两个字段说不同的话。
      if (!plan.good.length) {
        report.nothingToDo = true;
        report.error = "没有可入库的条目（全部重复或抓不到元数据）";
      } else {
        report.cancelled = true;
        report.error = "用户取消了";
      }
      report.duplicates = plan.dup;
      report.failed = plan.bad.map((b) => ({ doi: b.doi, why: b.why,
                                             detail: b.detail }));
      report.ms = Date.now() - t0;
      self.closeProgress();
      return report;
    }

    // ---- 第 3 步：落库 + 找 PDF
    const pw = self.acquireProgressStart(plan.good.length);
    const savedItems = [];          // 用来在最后统一走一遍分类
    for (let i = 0; i < plan.good.length; i++) {
      const g = plan.good[i];
      const desc = self.describeAcquireJson(g.json);
      self.acquireProgressSet(pw, i, plan.good.length, "正在入库：" + desc.title);
      const row = { doi: g.doi, title: desc.title,
                    status: "added", itemKey: "", pdf: null };
      try {
        const item = await self.saveAcquired(g.json, col ? col.id : 0, libraryID);
        row.itemKey = item.key;
        row.itemID = item.id;
        savedItems.push(item);
        // 打标记：Notifier 会在 ~2.5 秒后回调到 handleNewItem，
        // 那时这里已经跑完了，所以标记必须**留在对象上**（不能只用一个
        // 布尔开关在 finally 里清掉 —— 会在回调前就失效）。
        // 目的是**压住逐篇的自动分类**，因为下面会统一走一次批量的
        // （见第 4 步）—— 不然 5 篇就是 5 个弹窗。
        self.acquiredIDs[item.id] = Date.now();
        if (p.findPdf) {
          row.pdf = await self.findPdfForAcquired(item, p.methods);
        }
      } catch (e) {
        row.status = "error";
        row.error = String(e);
        Zotero.logError(e);
      }
      report.results.push(row);
    }

    // ---- 第 4 步：走一遍本地大模型的分类 / 打标签（**一个汇总框问一次**）
    //
    // 用户要的流程："填进去后走本地大模型自动打标签分类"。
    // 放在找完 PDF 之后：分类要看标题+摘要，与 PDF 无关，但放最后能让
    // 进度窗先把"几篇入库"报出来，用户不至于对着一个不动的框等模型。
    if (self.getPref(self.PREFS.acquireAutoClassify, true) && savedItems.length) {
      self.acquireProgressSet(pw, savedItems.length, savedItems.length,
                              "正在让本地模型判断分类…");
      const pairs = [];
      for (const it of savedItems) {
        let sug = null;
        try {
          sug = await self.classifyOnly(it);
        } catch (e) {
          Zotero.logError(e);
        }
        pairs.push({ item: it, sug });
      }
      try {
        report.classify = await self.askApplyBatch(pairs);
      } catch (e) {
        report.classify = { error: String(e) };
        Zotero.logError(e);
      }
    }

    report.duplicates = plan.dup;
    report.failed = plan.bad.map((b) => ({ doi: b.doi, why: b.why,
                                           detail: b.detail }));
    report.ms = Date.now() - t0;
    report.added = report.results.filter((r) => r.status === "added").length;
    report.withPdf = report.results.filter((r) => r.pdf && r.pdf.ok).length;
    report.classifySkipped = false;
    report.note = "落库后已走一遍本地模型的分类建议（见 Zotero 里的汇总框）。";
    // ⚠ 进度窗的收尾必须放在**统计算完之后** —— 原来写在前面，
    //   于是窗口上显示的是 "undefined 篇已入库"（写的时候 report.added
    //   还没赋值）。这种错很典型：函数调用的位置比它读的数据早一行。
    self.acquireProgressDone(pw, report);
    return report;
  },


  /**
   * 解析 DSH 侧发来的载荷。
   * 形状：{ items:[{doi,title}], collectionID?:number, findPdf?:bool,
   *         methods?:string[], dryRun?:bool }
   * 也接受 items 里直接放字符串（就是 DOI）。
   */
  parseAcquirePayload: function (raw) {
    const p = (typeof raw === "string") ? JSON.parse(raw) : (raw || {});
    const items = [];
    for (const it of (Array.isArray(p.items) ? p.items : [])) {
      const rawDoi = (typeof it === "string") ? it : (it && it.doi);
      const doi = ZoteroKB.cleanDoi(rawDoi);
      if (doi) {
        items.push({ doi,
                     title: (it && typeof it === "object" && it.title) || "" });
      }
    }
    return {
      items,
      collectionID: Number(p.collectionID) || 0,
      findPdf: p.findPdf === undefined ? true : !!p.findPdf,
      methods: Array.isArray(p.methods) && p.methods.length ? p.methods : null,
      dryRun: !!p.dryRun,
    };
  },


  /** DOI 归一化。优先用 Zotero 自己的 cleanDOI（它认得 10.xxxx/... 的各种写法）。 */
  cleanDoi: function (s) {
    const raw = String(s == null ? "" : s).trim();
    if (!raw) return "";
    try {
      const c = Zotero.Utilities.cleanDOI(raw);
      if (c) return c;
    } catch (e) { /* 没有这个方法就退回下面 */ }
    const m = raw.match(/10\.\d{4,9}\/[^\s"'<>]+/);
    return m ? m[0].replace(/[.,;)]+$/, "") : "";
  },


  /** 全库 DOI → item.key 映射，用来去重。821 条实测 2ms。 */
  doiIndex: async function (libraryID) {
    // ⚠ Zotero.Items.getAll() 返回的是 **Promise**（实测 ctor=Promise），必须 await。
    const all = await Zotero.Items.getAll(libraryID, false);
    const map = {};
    for (const it of all) {
      try {
        if (it.isRegularItem && !it.isRegularItem()) continue;
        const d = it.getField("DOI");
        if (d) map[String(d).toLowerCase()] = it.key;
      } catch (e) {
        // 半加载条目 getField 会抛 UnloadedDataException —— 跳过，
        // 不能让它把整个去重过程带崩（这个坑本项目踩过）。
      }
    }
    return map;
  },


  /**
   * 按 DOI 抓元数据，**只翻译不保存**。
   *
   * `libraryID: false` 是 Zotero 自己的"预览"路径（translate.js 里对
   * `_libraryID == false` 的分支）。实测返回的是普通对象而不是 Zotero.Item，
   * 所以这里不能用 getField()，只能读 JSON 字段。
   */
  resolveByDoi: async function (doi) {
    const tr = new Zotero.Translate.Search();
    tr.setIdentifier({ DOI: doi });
    const tls = await tr.getTranslators();
    const names = (tls || []).map((t) => (t && t.label) || "?").slice(0, 3);
    if (!tls || !tls.length) {
      return { ok: false, why: "no_translator", translators: names };
    }
    tr.setTranslator(tls);
    let items;
    try {
      items = await tr.translate({ libraryID: false });
    } catch (e) {
      return { ok: false, why: "translate_error", detail: String(e),
               translators: names };
    }
    if (!items || !items.length) {
      return { ok: false, why: "no_result", translators: names };
    }
    const json = items[0];
    // 兜底：万一翻译器没带回 DOI，用我们自己的，否则后面 addAvailableFile
    // 的 DOI resolver 就没了（getFileResolvers 只认条目上的 DOI/url 字段）。
    if (json && typeof json === "object" && !json.DOI) json.DOI = doi;
    return { ok: true, json, translators: names };
  },


  /** 预览对象 → 一句话摘要（对话框和回报都用它）。 */
  describeAcquireJson: function (json) {
    const j = json || {};
    const c = (j.creators || [])[0] || {};
    const who = c.lastName || c.name || c.firstName || "?";
    const yr = String(j.date || "").match(/\d{4}/);
    const bits = [];
    if (j.publicationTitle) bits.push(String(j.publicationTitle));
    if (j.volume) bits.push("第 " + j.volume + " 卷");
    if (j.issue) bits.push("第 " + j.issue + " 期");
    if (j.pages) bits.push(j.pages);
    return {
      title: String(j.title || "(无标题)"),
      byline: who + (yr ? " " + yr[0] : ""),
      venue: bits.join("，"),
    };
  },


  /** 目标分类：显式给了就用给的，否则用 Zotero 里**当前选中**的分类。 */
  acquireTargetCollection: function (collectionID) {
    try {
      if (collectionID) {
        const c = Zotero.Collections.get(collectionID);
        if (c) return c;
      }
    } catch (e) { /* 掉到下面用选中的 */ }
    try {
      const pane = Zotero.getActiveZoteroPane();
      const sel = pane && pane.getSelectedCollection && pane.getSelectedCollection();
      if (sel) return sel;
    } catch (e) { /* 没有主窗口（如后台测试）就算没选中 */ }
    return null;
  },


  /**
   * 存一篇（**这是本文件里除"元数据补全"之外唯一的写库路径**）。
   *
   * 为什么能直接复用预览 JSON：实测 `item.fromJSON(预览对象)` 能把
   * 标题/DOI/年份/卷期页/26 位作者全部正确落进条目（探针验证过，
   * 且 `saved:false` 说明构造阶段不落库）。这样整条链路只联网一次。
   */
  saveAcquired: async function (json, collectionID, libraryID) {
    const item = new Zotero.Item();
    await item.fromJSON(json);
    item.libraryID = libraryID || Zotero.Libraries.userLibraryID;
    if (collectionID) item.setCollections([collectionID]);
    await item.saveTx();
    return item;
  },


  /**
   * 给刚入库的条目找 PDF —— 走 Zotero 自带的 resolver 链。
   *
   * ⚠ 这里**故意不用 `addAvailableFile`**，而用底层的 `addFileFromURLs`
   *   + `getFileResolvers`：前者只把 `options.methods` 传下去，拿不到
   *   "最后是哪条路成功的"；后者能通过 `onAccessMethodStart` 收到回调，
   *   于是一篇没下到时，我们能告诉用户**试过哪几条路**，而不是干巴巴一句
   *   "没找到"。
   *
   * ⚠ 另一个实测坑：一条路都没走通时，`downloadFirstAvailableFile`
   *   返回的是 `false`，而 `addFileFromURLs` 直接对它做对象解构
   *   （`let {title, mimeType, url} = await ...`）→ **抛 TypeError**，
   *   不是返回 false。所以"没找到"在这里表现为异常，必须按异常接住。
   */
  findPdfForAcquired: async function (item, methods) {
    const tried = [];
    const out = { ok: false, tried: [], error: "" };
    try {
      const resolvers = Zotero.Attachments.getFileResolvers(item, methods || undefined);
      out.resolvers = resolvers.length;
      const att = await Zotero.Attachments.addFileFromURLs(item, resolvers, {
        onAccessMethodStart: (m) => { tried.push(m); },
      });
      out.tried = tried;
      if (att) {
        out.ok = true;
        out.attachmentKey = att.key;
        out.attachmentTitle = att.getField("title") || "";
        try { out.bytes = att.attachmentSize || 0; } catch (e) { /* ignore */ }
      }
      return out;
    } catch (e) {
      out.tried = tried;
      out.error = String(e);
      // 解构失败 = Zotero 自己的"一条路都没走通"信号，不是真错误
      if (/destructur|not iterable|of undefined|of null/i.test(String(e))) {
        out.noFileFound = true;
        out.error = "";
      }
      return out;
    }
  },


  /**
   * 抓取前的确认框（**默认不弹**，见 PREFS.acquireConfirm 的注释）。
   *
   * 为什么默认关掉：搜索、列候选、挑选本来就在 **DSH 对话**里完成，
   * 再弹一个 Zotero 模态框等于把一次连贯的对话中断成两个界面 ——
   * 用户明确否掉了这个设计（"确认应该在用户做选择的地方"）。
   * 现在确认与回报都在 DSH 里；这里保留实现，给想要"落盘前再看一眼"的人。
   */
  askAcquire: function (plan, col) {
    var self = ZoteroKB;
    const ps = Services.prompt;
    const win = Zotero.getMainWindow();
    const lines = [];

    lines.push("DSH 那边挑好了 " + plan.good.length + " 篇，要存进 Zotero：");
    lines.push("");
    plan.good.slice(0, 12).forEach((g, i) => {
      const d = self.describeAcquireJson(g.json);
      lines.push("  " + (i + 1) + ". " + d.byline + " · " + d.title);
      if (d.venue) lines.push("        " + d.venue);
    });
    if (plan.good.length > 12) {
      lines.push("  …还有 " + (plan.good.length - 12) + " 篇");
    }

    if (plan.dup.length) {
      lines.push("");
      lines.push("ℹ 下面 " + plan.dup.length + " 篇库里已经有了，会跳过：");
      plan.dup.slice(0, 6).forEach((d) => {
        lines.push("  · " + d.doi + (d.title ? "（" + d.title + "）" : ""));
      });
      if (plan.dup.length > 6) lines.push("  …还有 " + (plan.dup.length - 6) + " 篇");
    }
    if (plan.bad.length) {
      lines.push("");
      lines.push("⚠ 下面 " + plan.bad.length + " 篇 Zotero 抓不到元数据，会跳过：");
      plan.bad.slice(0, 6).forEach((b) => {
        lines.push("  · " + b.doi + "（" + self.acquireWhyText(b.why) + "）");
      });
      if (plan.bad.length > 6) lines.push("  …还有 " + (plan.bad.length - 6) + " 篇");
    }

    lines.push("");
    lines.push("存入分类：" + (col ? col.name : "（我的文库根目录）"));
    lines.push("存好后会为每篇查找可用的 PDF —— 用的是你机器**当下**的网络"
               + "身份，校园网/机构订阅都算数。");
    if (!plan.good.length) {
      self.alertDialog("没有可存的条目", lines.join("\n"));
      return { go: false, remember: false };
    }

    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
    const checkbox = { value: false };
    // ⚠ confirmEx 只有 **9 个参数**（见 nsIPromptService.idl）：
    //     parent, dialogTitle, text, buttonFlags,
    //     button0Title, button1Title, button2Title, checkMsg, checkValue
    //   第 9 个 checkValue 是 `inout boolean`，**必须是对象**（{value:bool}）。
    //   这里曾经多写了一个 `null`（把 button2Title 和 checkMsg 之间多插了一个），
    //   于是 checkValue 收到 null → 真机报
    //     NS_ERROR_XPC_NEED_OUT_OBJECT: 'Out' argument must be an object arg 8
    //   对照本文件里已有的 askOneMeta（它的写法是对的）就不会犯这个错。
    //   改这段时数一遍参数个数：**只有两个按钮时也要把 button2Title 写出来（null）**。
    const choice = ps.confirmEx(
      win, "从 DSH 导入文献",
      lines.join("\n"), flags,
      "添加到 Zotero", "取消", null,
      "以后不再询问，直接入库（可在插件设置里重新打开）", checkbox
    );
    return { go: choice === 0, remember: choice === 0 && !!checkbox.value };
  },


  /** 失败原因说人话（对话框与回报共用，避免两处不一致）。 */
  acquireWhyText: function (why) {
    switch (why) {
      case "no_translator": return "Zotero 没有能识别这个 DOI 的翻译器";
      case "no_result": return "翻译器认了，但没返回任何条目";
      case "translate_error": return "翻译时报错";
      case "error": return "出错了";
      default: return why || "未知原因";
    }
  },


  // ---- 抓取进度窗（拿不到就静默降级，不能因为提示挂了把抓取带崩）

  acquireProgressStart: function (n) {
    try {
      const pw = new Zotero.ProgressWindow({ closeOnClick: true });
      pw.changeHeadline("正在从 DSH 导入文献");
      // ⚠ 先建进度行、再 show()。反过来的话，万一 ItemProgress 不可用，
      //   屏幕上会留下一个**空白**的进度窗（比不显示更糟）。
      pw._kbLine = new pw.ItemProgress("", "准备中…");
      pw._kbLine.setProgress(0);
      pw.show();
      return pw;
    } catch (e) {
      Zotero.debug("[zotero-kb] 进度窗不可用，静默继续：" + e);
      return null;
    }
  },


  acquireProgressSet: function (pw, i, n, text) {
    if (!pw || !pw._kbLine) return;
    try {
      pw._kbLine.setProgress(Math.round((i / Math.max(n, 1)) * 100));
      pw._kbLine.setText(text.slice(0, 90));
    } catch (e) { /* ignore */ }
  },


  acquireProgressDone: function (pw, report) {
    if (!pw || !pw._kbLine) return;
    try {
      pw._kbLine.setProgress(100);
      const bits = [report.added + " 篇已入库"];
      if (report.withPdf) bits.push(report.withPdf + " 篇带 PDF");
      if (report.failed.length) bits.push(report.failed.length + " 篇抓不到元数据");
      if (report.duplicates.length) bits.push(report.duplicates.length + " 篇库里已有");
      pw._kbLine.setText(bits.join("，"));
      pw.startCloseTimer(8000);
    } catch (e) { /* ignore */ }
  },


  // ---------------------------------------------------------------- 兜底：把下好的 PDF 挂上去

  /**
   * 任务 kind = "attach"：把本机一个**已经下好、验过是 PDF** 的文件挂到某条目上。
   *
   * 为什么要有这一步：`kb_acquire` 走 Zotero 自带 resolver 找不到 PDF 时，
   * DSH 侧还能自己搜一个直链下下来。但"下到本地"本身不是目的 ——
   * 目的是**进 Zotero**。没有这一步，用户还是得手工拖，等于白做。
   *
   * 载荷（JSON 字符串）：
   *   { files: [ { itemKey: "ABCD1234", path: "D:\\...\\x.pdf", title: "" } ] }
   *
   * ⚠ 只挂**新建的条目**，不碰用户已有的条目 —— 与 acquire 同一条边界。
   *   而且同样走 `acquireConfirm`（见调用处），不静默改用户的东西。
   */
  runAttach: async function (rawPayload) {
    var self = ZoteroKB;
    let p;
    try {
      p = (typeof rawPayload === "string") ? JSON.parse(rawPayload) : (rawPayload || {});
    } catch (e) {
      return { ok: false, error: "载荷解析失败：" + e };
    }
    const files = Array.isArray(p.files) ? p.files : [];
    if (!files.length) return { ok: false, error: "没有要挂的文件" };

    const libraryID = Zotero.Libraries.userLibraryID;
    const out = { ok: true, results: [] };
    for (const f of files) {
      const row = { itemKey: f.itemKey || "", path: f.path || "",
                    status: "attached", attachmentKey: "", error: "" };
      try {
        const item = Zotero.Items.getByLibraryAndKey(libraryID, String(f.itemKey || ""));
        if (!item) throw new Error("库里没有 key=" + f.itemKey + " 的条目");
        if (!(await IOUtils.exists(f.path))) throw new Error("文件不存在：" + f.path);

        // ⚠ 落盘的东西**必须自己再验一次文件头**才敢挂：出版社/CDN 返回
        //   "HTTP 200 + 一个 HTML 验证页"是常态。DSH 侧的脚本已经验过一道，
        //   这里再验一道 —— 挂一个网页当 PDF 附件，比不挂更坏。
        const head = await IOUtils.read(f.path, { maxBytes: 1024 });
        const magic = String.fromCharCode.apply(null, Array.from(head.slice(0, 5)));
        if (magic.indexOf("%PDF") !== 0) {
          throw new Error("不是 PDF（文件头是 " + JSON.stringify(magic) + "）");
        }

        const att = await Zotero.Attachments.importFromFile({
          file: f.path,
          libraryID: libraryID,
          parentItemID: item.id,
          title: f.title || undefined,
          contentType: "application/pdf",
        });
        row.attachmentKey = att ? att.key : "";
      } catch (e) {
        row.status = "error";
        row.error = String(e);
      }
      out.results.push(row);
    }
    out.attached = out.results.filter((r) => r.status === "attached").length;
    return out;
  },
});

// ===== src/11-notify.js =====
/**
 * 11-notify.js —— 提示：进度窗与通知（ProgressWindow 的使用纪律都在这）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 提示

  /**
   * 关掉当前那条进度提示。
   *
   * 为什么要做实例管理：每次提示都 `new Zotero.ProgressWindow()` 的话，
   * "正在整理路径…"、"已开始发送…"、"已发送到 DSH" 三条会**叠在屏幕上**，
   * 用户看到一堆窗口。所以统一只保留一条：新的要显示前，先把上一条关掉。
   *
   * 说明：Zotero 的 ProgressWindow **没有关闭按钮**（API 只有
   * show / close / startCloseTimer / closeAll），它是"点击即关闭"样式。
   * 所以"关闭的 X"用等效方式实现：closeOnClick + 文案里写明可点击关闭。
   */
  closeProgress: function () {
    var self = ZoteroKB;
    try {
      if (self._progressWin) {
        self._progressWin.close();
      }
    } catch (e) { /* 已经关了就忽略 */ }
    self._progressWin = null;
  },


  /** 建一条新的进度提示（会自动关掉上一条）。 */
  newProgress: function (headline, text) {
    var self = ZoteroKB;
    self.closeProgress();
    const pw = new Zotero.ProgressWindow({ closeOnClick: true });
    try { pw.changeHeadline(headline); } catch (e) { /* ignore */ }
    if (text) { try { pw.addDescription(text); } catch (e) { /* ignore */ } }
    try { pw.show(); } catch (e) { /* ignore */ }
    self._progressWin = pw;
    return pw;
  },


  notify: function (title, text, _win, isError) {
    var self = ZoteroKB;
    try {
      const progressWin = self.newProgress(title, text || "");
      progressWin.startCloseTimer(isError ? 12000 : 6000);
    } catch (e) {
      // 进度窗不可用时退回调试日志，至少留下痕迹
      Zotero.debug("[zotero-kb] " + title + " — " + text);
    }
  },
});

// ===== src/12-dsh.js =====
/**
 * 12-dsh.js —— 发到 DSH：文件信箱通道（不走 HTTP，见 ARCHITECTURE.md）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 发到 DSH

  /** 桥接信箱目录（与 DSH 侧插件约定，见 ARCHITECTURE.md）。 */
  bridgeDir: function () {
    return this.bridgeDirPath();
  },


  /**
   * 给 DSH 侧收件箱投一个请求文件，等它写回结果。
   *
   * 为什么用文件而不是 HTTP：DSH desktop profile 里**没有 webServer 服务**
   * （插件会卡在 pending），而它的 /api 是"受信任+已认证"专用通道
   * （本机外部程序一律 401）。详见 ARCHITECTURE.md 第三、四节。
   *
   * 原子写：先写 .json.tmp 再改名 —— DSH 侧轮询时不会读到半截文件。
   */
  bridgeRequest: async function (req, waitMs) {
    var self = ZoteroKB;
    const root = self.bridgeDir();
    const id = "z" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    req.id = id;
    const reqDir = root + "\\requests";
    const resDir = root + "\\results";
    const tmp = reqDir + "\\" + id + ".json.tmp";
    const dst = reqDir + "\\" + id + ".json";
    const resFile = resDir + "\\" + id + ".json";

    try {
      // 目录不存在说明 DSH 侧插件没装载 —— 直接给出可操作的提示
      if (!(await IOUtils.exists(reqDir))) {
        return { ok: false, error: "收件箱目录不存在（DSH 侧插件未装载）",
                 hint: "请确认 dsh-bundle-zotero-bridge 已装并重启过 DSH" };
      }
      await IOUtils.writeUTF8(tmp, JSON.stringify(req, null, 1));
      await IOUtils.move(tmp, dst, { noOverwrite: false });
    } catch (e) {
      return { ok: false, error: "投递失败：" + e };
    }

    const deadline = Date.now() + (waitMs || 60000);
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 500));
      try {
        if (await IOUtils.exists(resFile)) {
          const text = await IOUtils.readUTF8(resFile);
          try { return JSON.parse(text); }
          catch (e) { /* 还没写完，下轮再读 */ }
        }
      } catch (e) { /* 忽略，继续等 */ }
    }
    return { ok: false, error: "等待 DSH 回复超时（" + ((waitMs || 60000) / 1000) + "s）",
             hint: "检查 DSH 是否在运行、插件是否已装载" };
  },


  /** 拉取 DSH 的对话列表（只读）。 */
  listDSHSessions: async function () {
    var self = ZoteroKB;
    try {
      const res = await self.bridgeRequest({ kind: "list-sessions" }, 20000);
      return (res && res.ok && res.sessions) ? res.sessions : [];
    } catch (e) {
      return [];
    }
  },


  /**
   * 把选中的文献发到 DSH 对话。
   *
   * ⚠ 只发"解析后的存放路径"，不发内容本身 ——
   * 知识库已把文献解析好放在 `kb\papers\<key>.md`（摘要/笔记）与
   * `kb\fulltext\<key>.md`（按页正文），DSH 有 read 工具能直接读文件。
   * 把正文塞进消息只是白烧 token、占满上下文。
   * 轻量信息（权重/分类）仍带上：它们是"知识库的结论"，不值几个 token。
   */
  sendToDSH: async function (items, opts) {
    var self = ZoteroKB;
    opts = opts || {};
    // 前置校验：不要把明显无效的请求发出去白等 2 分钟
    if (!opts.create && !String(opts.sessionId || "").trim()) {
      self.notify("发送失败", "没有指定对话（请从列表里选一个，或选「新建对话…」）",
                  null, true);
      return;
    }
    if (!items || !items.length) {
      self.notify("发送失败", "没有选中文献", null, true);
      return;
    }
    try {
      self.notify("正在整理文献路径…", items.length + " 篇", null, false);

      const keys = items.map((it) => it.key);
      let info = {};
      // 知识库根路径从本地服务取，而不是写死 —— 这样知识库以后
      // 迁到别处（比如 Zotero 数据目录）这里不用改。
      let kbDir = self.kbDir();      // 缓存值兜底，下面再用 /health 校正
      try {
        // W3 线2.5：走**共用**的 kbItemInfo（桥优先、退回 HTTP；见 05-server.js）。
        // ⚠ 它可能回 null（两条通道都失败）——那时只有 Zotero 元数据可发，
        //   下面的 `info[k]` 查不到就落到「这篇还没进知识库」那句，行为可接受。
        const r = await self.kbItemInfo(keys);
        for (const x of (r && r.items) || []) info[x.key] = x;
        const st = await self.request("GET", "/health");
        if (st && st.kb_dir) kbDir = String(st.kb_dir).replace(/[\\/]+$/, "");
      } catch (e) {
        Zotero.debug("[zotero-kb] 取知识库信息失败（用默认路径）：" + e);
      }

      // ⚠ 只发**解析后的存放路径**，不发内容本身。
      // 知识库已把文献解析好放在 papers\<key>.md（摘要/笔记/元数据）与
      // fulltext\<key>.md（按页正文）；DSH 有 read 工具能直接读文件。
      // 把正文塞进消息只是白烧 token、占满上下文。
      const L = [];
      L.push("【Zotero 文献】已按知识库路径发来，共 " + items.length + " 篇");
      L.push("知识库根目录：" + kbDir);
      L.push("");
      for (const it of items) {
        const k = it.key;
        const meta = self.buildMeta(it);
        const kb = info[k] || {};
        L.push("- " + (meta.title || "(无标题)")
          + (meta.year ? "（" + meta.year + "）" : "") + "  [" + k + "]");
        if (kb.in_kb) {
          const bits = [];
          if (kb.weight && Math.abs(kb.weight - 1) > 0.005) {
            bits.push("权重 " + kb.weight + "（被使用经验加权过）");
          }
          if (kb.collections && kb.collections.length) {
            bits.push("分类 " + kb.collections.join("、"));
          }
          if (kb.tags && kb.tags.length) {
            bits.push("标签 " + kb.tags.slice(0, 6).join("、"));
          }
          if (bits.length) L.push("    知识库：" + bits.join("｜"));
          L.push("    摘要与笔记：" + kbDir + "\\papers\\" + k + ".md");
          if (kb.fulltext_chars > 0) {
            L.push("    按页正文：" + kbDir + "\\fulltext\\" + k + ".md"
              + "（" + kb.fulltext_chars + " 字符）");
          }
        } else {
          L.push("    （这篇还没进知识库，只有 Zotero 元数据）");
        }
      }
      L.push("");
      L.push("---");
      L.push("请按上面的路径读取（要细节读「按页正文」，要概览读「摘要与笔记」）；"
        + "也可直接用 kb_search / kb_item / kb_fulltext 工具按 key 取。"
        + "读完后结合这些文献回答我接下来的问题。");

      const text = L.join("\n");

      // 用 newProgress/closeProgress 统一管理 —— 新提示出现时旧的自动关掉，
      // 屏幕上永远只有一条（否则"整理路径→已开始发送→已发送"会叠三层）。
      // 结果提示不自动消失（不调 startCloseTimer），由用户点击关闭。
      try {
        self.newProgress("正在发送到 DSH…",
          items.length + " 篇文献（只发路径）· "
          + (opts.create ? "新建对话" : "指定对话")
          + "\n等待 DSH 确认，请稍候…");
      } catch (e) { /* ignore */ }

      const res = await self.bridgeRequest(
        opts.create
          ? { kind: "create-and-send",
              title: "Zotero 文献：" + String(items[0].getField("title") || "").slice(0, 28),
              text }
          : { kind: "send", sessionId: opts.sessionId, text },
        120000);

      try {
        if (res && res.ok) {
          // 成功：不自动关闭，留给你看清楚；点一下窗口即关闭（Zotero 进度窗
          // 没有 X 按钮，closeOnClick 就是它的关闭方式，所以文案里写明）
          const pw = self.newProgress("✅ 已发送到 DSH",
            items.length + " 篇 · " + text.length + " 字符（只发路径）\n"
            + (res.sessionId ? "对话 " + String(res.sessionId).slice(-12) : "")
            + "\n\n去 DSH 里打开对应对话即可。\n"
            + "（点击本窗口任意处关闭）");
          // 兜底：不点也自己消失，避免一直留在屏幕上
          try { pw.startCloseTimer(60000); } catch (e) { /* ignore */ }
        } else {
          const why = String((res && (res.error
            || JSON.stringify(res.notes || res.tries))) || "未知错误").slice(0, 300);
          const pw = self.newProgress("❌ 发送失败", why
            + "\n\n（点击本窗口任意处关闭）");
          try { pw.startCloseTimer(60000); } catch (e) { /* ignore */ }
        }
      } catch (e) { /* 进度窗失败不影响主流程 */ }

      if (res && res.ok) {
        Zotero.debug("[zotero-kb] 已发送到 DSH：" + res.sessionId);
      } else {
        Zotero.logError(new Error("[zotero-kb] sendToDSH 失败："
          + JSON.stringify(res).slice(0, 400)));
      }
    } catch (e) {
      Zotero.logError(e);
      self.notify("发送出错", String(e), null, true);
    }
  },
});

// ===== src/13-kbopen.js =====
/**
 * 13-kbopen.js —— 打开知识库（分级）：级别清单、定位文件、交给系统打开
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 *
 * 为什么要有它（用户 2026-10-05 的要求）：
 *   "打开知识库时先打开一个构建的列表，里面也是文献，然后点进某篇文献，
 *    里面还有不同级别的知识库内容，再打开某个具体的知识库才是打开具体的
 *    md 文件"；右键菜单也一样。
 *
 * 知识库目录里是 `papers/22X9PMR6.md` —— **文件名是 Zotero 的 key，人认不出
 * 是哪篇**。所以"打开某一篇的某一层"这件事必须由插件/面板替他做掉：
 * 右键 → 打开知识库 → 选级别 → 打开那个 md。
 *
 * ⚠ KB_LEVELS 必须与 **offline/kbviews.py 的 LEVELS** 一致（id / 标签 /
 *   相对路径模板三条）。它是跨语言复制的一份常量，所以有
 *   tools/check_kb_levels.py 盯着两边 —— 手工同步的约定最后一定会漂移。
 */
Object.assign(ZoteroKB, {

  /**
   * 知识库的级别清单（镜像 offline/kbviews.py 的 LEVELS）。
   *
   * rel 里的 `{key}` 会被替换成 Zotero 条目 key；分隔符统一用正斜杠，
   * 拼本地路径时再转成反斜杠（这样两边写起来一样，不会一个 `\` 一个 `/`）。
   */
  KB_LEVELS: [
    { id: "tldr", label: "摘要与要点", rel: "views/{key}.tldr.md",
      what: "元数据 + 结构化字段 + 摘要 + 笔记要点 + 使用经验，几百 token" },
    // 中间层：比摘要详细、比全文短（由 offline/digest.py 生成）
  { id: "outline", label: "分节纲要", rel: "views/{key}.outline.md",
    what: "按章节给「这一节在做什么 + 关键点/参数/结论」，每节带页码范围" },
  { id: "card", label: "完整档案", rel: "papers/{key}.md",
      what: "元数据 + 摘要 + 每页首段 + 笔记与高亮标注" },
    { id: "fulltext", label: "按页正文", rel: "fulltext/{key}.md",
      what: "带 ## p.N 页码锚点的正文" },
    { id: "figures", label: "图注与表格", rel: "views/{key}.figures.md",
      what: "散在各页末尾的图注与表格，汇总成一份" },
    { id: "weight", label: "权重与经验", rel: "views/{key}.weight.md",
      what: "检索权重、重点标记、人工加减分，以及这篇的全部使用经验" },
  ],

  /**
   * 某一篇的全部级别，带**文件在不在**。
   *
   * 为什么必须同步判断存在性：右键菜单是**同步**构建的（弹出来那一刻不能等
   * 网络、也不能等 Python），所以只能靠"文件在不在"决定显示什么。
   * 这也正是分级视图要在构建时生成、而不是"用的时候现生成"的原因。
   */
  kbLevels: function (key) {
    var self = ZoteroKB;
    var root = String(self.kbDir() || "").replace(/[\\/]+$/, "");
    return self.KB_LEVELS.map(function (lv) {
      var rel = String(lv.rel).replace(/\{key\}/g, key).replace(/\//g, "\\");
      var path = root + "\\" + rel;
      var exists = false;
      try { exists = !!self._exists(path); } catch (e) { /* 只当作"没有" */ }
      return { id: lv.id, label: lv.label, what: lv.what,
               path: path, exists: exists };
    });
  },

  /** 一篇还差哪几层没生成（决定菜单项的文案）。 */
  kbLevelsMissing: function (key) {
    var self = ZoteroKB;
    var miss = [];
    try {
      self.kbLevels(key).forEach(function (lv) {
        if (!lv.exists) miss.push(lv.label);
      });
    } catch (e) {
      return self.KB_LEVELS.map(function (lv) { return lv.label; });
    }
    return miss;
  },

  /**
   * 打开一个本地文件。按"从好到差"依次试，试到哪个算哪个。
   *
   * 为什么写成链式而不是挑一个：沙箱里能用的 API 随 Zotero 版本变，而这些
   * 入口的可用性没法静态确认。链式的好处是**最坏情况仍然有确定可用的兜底**
   * （`reveal()` 已在 settings.js 用过；再往前还有"交给面板的 Python 调
   * os.startfile"，那条是本机面板一直在用的）。用哪一条会写进 debug 日志。
   */
  openKbPath: function (path, what) {
    var self = ZoteroKB;
    var tried = [];
    var label = what || "知识库文件";

    // ① Zotero 自己的"打开外链/文件"入口
    try {
      if (typeof Zotero.launchURL === "function") {
        Zotero.launchURL(
          Services.io.newFileURI(Zotero.File.pathToFile(path)).spec);
        Zotero.debug("[zotero-kb] 打开知识库用 Zotero.launchURL：" + path);
        return "launchURL";
      }
      tried.push("Zotero.launchURL 不存在");
    } catch (e) {
      tried.push("Zotero.launchURL：" + e);
    }

    // ② 外部协议服务（把 file: URI 交给系统默认程序）
    try {
      const uri = Services.io.newFileURI(Zotero.File.pathToFile(path));
      const ext = Components.classes[
        "@mozilla.org/uriloader/external-protocol-service;1"
      ].getService(Components.interfaces.nsIExternalProtocolService);
      ext.loadURI(uri);
      Zotero.debug("[zotero-kb] 打开知识库用 externalProtocolService：" + path);
      return "externalProtocolService";
    } catch (e) {
      tried.push("externalProtocolService：" + e);
    }

    // ③（W4 2026-10-14 已删）原来还有一级"交给 Tk 面板的 Python 用 os.startfile 打开"——
    //    Tk 面板退役、`tools\gui.py（已退役 2026-10-14）` 已删，那一级必然失败；留着只会多一次无用的
    //    pythonw 往返、并把真实原因盖住。所以直接进第 ③ 级（原第 ④ 级）：
    // ④ 保底：在文件管理器里**选中**它（这条一定存在）。用户双击一下就能看，
    //    比"点了没反应"好得多。
    try {
      Zotero.File.pathToFile(path).reveal();
      Zotero.debug("[zotero-kb] 只能 reveal：" + path);
      return "reveal";
    } catch (e) {
      tried.push("reveal：" + e);
    }

    self.alertDialog("打不开「" + label + "」",
      "文件：\n" + path + "\n\n"
      + "四条路都试过了：\n" + tried.map(function (t) {
        return "  · " + t;
      }).join("\n")
      + "\n\n可以先在「" + self.kbDir() + "」里手动打开它；"
      + "如果这份文件本来就不该在，去面板点「补齐知识库分级文件」。");
    return "";
  },

  // ⚠ W4（2026-10-14）：原来这里有两个函数（起一个 Tk 面板进程 / 用它代劳开文件），
  //   它们是"起一个 Tk 面板（tools\gui.py（已退役 2026-10-14））进程并让它代劳"的兜底链。Tk 面板退役后
  //   `tools\gui.py（已退役 2026-10-14）` 已删，这两个函数**只会返回 false**（还会掩盖真实原因），
  //   所以整段删掉（连带 19-mineruguide.js 里那个调用点）。

  /**
   * 打开某一篇的某个级别。文件不在时**不静默失败** —— 明确告诉用户
   * 这一层还没生成、以及怎么生成。
   *
   * 为什么不在这里"现生成"：生成要在 Python 进程里读库（几百毫秒起，
   * 还要等子进程），而右键菜单点下去要立刻有反应。所以分工是：
   * 插件负责打开，生成交给构建/面板的「补齐知识库分级文件」。
   */
  openKbLevel: function (key, levelId) {
    var self = ZoteroKB;
    const row = (self.kbLevels(key) || []).filter(function (lv) {
      return lv.id === levelId;
    })[0];
    if (!row) return "";
    if (!row.exists) {
      self.notify("这一层还没生成", "「" + row.label + "」"
        + "（" + row.path + "）\n\n"
        + "去面板的「高级」页点「补齐知识库分级文件」，"
        + "或跑一次「手动更新（增量）」。", null, true);
      return "";
    }
    return self.openKbPath(row.path, row.label);
  },

  /** 在文件管理器里显示某一篇的分级视图目录（找不到就退到知识库根）。 */
  openKbFolder: function (key) {
    var self = ZoteroKB;
    const dir = String(self.kbDir() || "");
    var target = dir;
    try {
      const rows = self.kbLevels(key) || [];
      for (const lv of rows) {
        if (lv.exists) { target = lv.path.replace(/[\\/][^\\/]+$/, ""); break; }
      }
    } catch (e) { /* 用知识库根目录 */ }
    try {
      Zotero.File.pathToFile(target).reveal();
      return "reveal";
    } catch (e) {
      self.notify("打不开目录", String(target) + "\n" + e, null, true);
      return "";
    }
  },
});

// ===== src/14-menus.js =====
/**
 * 14-menus.js —— 工具栏按钮与条目右键菜单（菜单项都是每次弹出时现建）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 工具栏按钮 / 右键菜单

  /**
   * 在主窗口工具栏加一个按钮，点击打开知识库管理面板。
   *
   * 位置与写法照抄本机"沉浸式翻译"插件（它就是这么加按钮的）：
   *   document.getElementById("zotero-tb-note-add").after(btn)
   * 即插在「新建笔记」按钮后面，是 Zotero 主工具栏里的位置。
   *
   * W4（2026-10-14）之后面板是 **Zotero 内的新面板**（`content/admin.html` + `23-admin.js`）。
 * 下面说的是退役前的样子：面板曾是 Python/Tkinter 程序（tools\gui.py，已删）—— 插件不重写界面，
   * 直接把它拉起来即可（PyMuPDF、向量、模型都在 Python 侧，界面重写不划算）。
   */
  registerToolbarButton: function (win) {
    var self = ZoteroKB;
    try {
      const doc = (win && win.document)
        || (Zotero.getMainWindow && Zotero.getMainWindow().document);
      if (!doc) return;
      const BTN_ID = "zotero-tb-kb-panel";
      const old = doc.getElementById(BTN_ID);
      if (old) old.remove();                 // 窗口重建时先清掉旧的，避免重复

      const btn = doc.createXULElement
        ? doc.createXULElement("toolbarbutton")
        : doc.createElement("toolbarbutton");
      btn.id = BTN_ID;
      btn.setAttribute("class", "zotero-tb-button");
      btn.setAttribute("tooltiptext", "打开文献知识库管理面板");
      // ⚠ 用**工具栏专用**的小图标（toolbar-icon.svg，16×16）。
      // 之前图省事直接用了 icon.svg —— 那是 96×96 的大图标（给插件管理器/设置
      // 面板用的），list-style-image 会按原尺寸渲染，结果工具栏里一个巨大的图标。
      // 同时显式写 width/height，防止 Zotero 的按钮样式反向把它拉大。
      btn.setAttribute("style",
        "list-style-image: url(" + self.rootURI + "toolbar-icon.svg);"
        + " width: 28px; height: 28px;"
        + " -moz-context-properties: fill, fill-opacity;"
        + " fill: currentColor;");
      btn.addEventListener("click", () => { self.openPanel(); });

      // 插到「新建笔记」按钮后面（沉浸式翻译用的就是这个锚点）
      const anchor = doc.getElementById("zotero-tb-note-add");
      if (anchor && anchor.parentNode) anchor.after(btn);
      else {
        // 锚点找不到时的兜底：塞进集合树工具栏
        const tb = doc.getElementById("zotero-collections-toolbar")
          || doc.getElementById("zotero-toolbar-collection-tree");
        if (tb) tb.appendChild(btn);
        else Zotero.debug("[zotero-kb] 找不到工具栏锚点，按钮未添加");
      }
      Zotero.debug("[zotero-kb] 工具栏按钮已添加");
      // 工具菜单里的「知识库管理…」也在这里顺手注册一次。
      //
      // 为什么挂在 registerToolbarButton 里、而不是新开一个 startup step：
      //   · 窗口相关的 UI 必须在"启动时已存在的那个窗口"和"之后每个新窗口"
      //     各注册一次，而 registerToolbarButton 恰好就是**唯一**一个两条路都会
      //     走到的调用点（`00-core.js::scheduleWindowUI` + `17-windowhook.js`
      //     ::onMainWindowLoad）；
      //   · 本波只许动 23-admin / 14-menus / admin-pages/structure 三个文件，
      //     去 00-core 里加一步会越界。
      self.registerAdminMenu(win, doc);
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /**
   * 工具菜单里的「知识库管理…」——在 Zotero 里打开管理页面（W1 的新入口）。
   *
   * 为什么是**工具菜单**而不是再放一个工具栏按钮：工具栏那个按钮是"把旧 Tk 面板
   * 拉起来"（W3 之前必须还能用，见 ADMIN-PORT-PLAN 的不变量 1）；新面板更适合放在
   * 「工具」里与 JSON API 之类并列，不占工具栏那一排的位置。
   *
   * 幂等：重复注册先删旧的（窗口重建时 registerToolbarButton 会被再调一次），
   * 点开时由 `23-admin.js::kbAdminOpen` 保证"已经开着就聚焦"。
   */
  registerAdminMenu: function (win, docArg) {
    var self = ZoteroKB;
    try {
      const doc = docArg || (win && win.document)
        || (Zotero.getMainWindow && Zotero.getMainWindow().document);
      if (!doc) return;
      const popup = doc.getElementById("menu_ToolsPopup");
      if (!popup) {
        Zotero.debug("[zotero-kb] 找不到 menu_ToolsPopup，工具菜单项未加");
        return;
      }
      const ITEM_ID = "zotero-kb-admin-menu";
      const SEP_ID = "zotero-kb-admin-sep";
      for (const id of [ITEM_ID, SEP_ID]) {
        const old = doc.getElementById(id);
        if (old && old.remove) old.remove();
      }
      const mk = function (tag) {
        try {
          return doc.createXULElement ? doc.createXULElement(tag)
            : doc.createElement(tag);
        } catch (e) { return doc.createElement(tag); }
      };
      const mi = mk("menuitem");
      mi.id = ITEM_ID;
      // 与条目右键菜单里的项同一套外观（menu-iconic + image），不然一眼就看出
      // 是"外来的"（本文件上面记过同样的教训）。
      mi.setAttribute("class", "menuitem-iconic");
      if (self.rootURI) mi.setAttribute("image", self.rootURI + "toolbar-icon.svg");
      mi.setAttribute("label", "知识库管理…");
      mi.setAttribute("tooltiptext",
        "在 Zotero 里打开知识库管理页面（状态、知识库结构）。\n"
        + "无端口：引擎是本插件拉起的子进程，Zotero 退出时一起收掉。");
      mi.addEventListener("command", function () {
        try { self.kbAdminOpen(); } catch (e) { Zotero.logError(e); }
      });
      const sep = mk("menuseparator");
      sep.id = SEP_ID;
      // 放到最上面，并用分隔符与 Zotero 自带项隔开。
      popup.insertBefore(mi, popup.firstChild);
      if (mi.after) mi.after(sep);
      else popup.insertBefore(sep, mi.nextSibling);
      Zotero.debug("[zotero-kb] 工具菜单项「知识库管理…」已添加");
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /**
   * 带控制台的 Python —— 跑命令行脚本、要拿输出时用它。
   *
   * ⚠ `pythonExe()` 优先给 **pythonw.exe**（无窗口，适合启动 GUI），
   *   但 **pythonw 下 stdout 是 None** —— 脚本里的 `print` 会直接抛
   *   `AttributeError: 'NoneType' object has no attribute 'write'`。
   *   而 `convert.py` 全靠 print 报告进度和结果（`log()` 就是 print），
   *   所以跑脚本必须换回同一目录下的 python.exe。
   */
  pythonConsoleExe: function () {
    var self = ZoteroKB;
    const py = self.pythonExe();
    if (!py) return "";
    const alt = py.replace(/pythonw\.exe$/i, "python.exe");
    if (alt !== py && self._exists(alt)) return alt;
    return py;
  },


  /**
   * 跑一段 Python 并等它结束。返回 `{code, out}`（`out` 恒为空串，见下）。
   *
   * ⚠⚠ **不要用 `stdout: "pipe"` 去读输出** —— 本机实测踩过：
   *   `Subprocess.call` 给的 `proc.stdout` 是 **XPCOM 的 `nsIAsyncInputStream`**，
   *   不是 Web Stream，**没有 `getReader()`**。写成 `stream.getReader()` 会抛
   *   `stream.getReader is not a function`，用户点「重建知识库条目」时看到的就是
   *   这个报错（进度窗弹出即失败）。
   *
   *   现在用 `"ignore"`（等价于 /dev/null）：子进程照常 `print`，
   *   既不会因管道写满而卡死，也不需要读流。
   *
   *   **要看结果就读 MANIFEST.json**（见 `manifestSummary`）——
   *   那本来就是构建流程写给程序看的权威产物，比解析 stdout 可靠得多。
   */
  runPython: async function (args) {
    var self = ZoteroKB;
    const py = self.pythonConsoleExe();
    const root = self.projectRoot();
    if (!py) {
      return { code: -1, out: "找不到 Python（见 设置 → 文献知识库 → 运行环境）" };
    }
    if (!root) return { code: -1, out: "找不到项目目录" };
    const { Subprocess } = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs");
    const proc = await Subprocess.call({
      command: py,
      arguments: ["-X", "utf8"].concat(args),
      workdir: root,
      stdout: "ignore", stderr: "ignore",
    });
    const { exitCode } = await proc.wait();
    return { code: exitCode, out: "" };
  },


  /**
   * 从 `MANIFEST.json` 读这次构建的关键结果（**不要解析 stdout**）。
   *
   * 为什么读文件而不是抓进程输出：见 `runPython` 的说明。而且 MANIFEST 里
   * 记的是**全库实况**（`fulltext_total`），比一段可能被截断的 stdout 完整。
   */
  manifestSummary: async function () {
    var self = ZoteroKB;
    try {
      const txt = await IOUtils.readUTF8(self.kbDir() + "\\MANIFEST.json");
      const mf = JSON.parse(txt);
      const lines = [];
      const tot = mf.fulltext_total || {};
      const parts = Object.keys(tot).map((k) => k + " " + tot[k]);
      if (parts.length) lines.push("正文来源：" + parts.join("｜"));
      const fx = ((mf.fulltext || {}).deshifted) || [];
      // ⚠ 用**全库实况**计数，不要用 `fulltext.deshifted` ——
      //   后者是"本批次"的统计，增量重建时恒为 0，用户就永远看不到
      //   "库里其实有几篇是靠字符偏移还原救回来的"（实测踩到：
      //   全库实况里 `zotero-cache+fix+29` 有 1 篇，而本批次是 0）。
      let nfix = 0;
      for (const k of Object.keys(tot)) {
        if (k.indexOf("+fix") >= 0) nfix += tot[k];
      }
      if (nfix) lines.push("其中 " + nfix + " 篇的正文是字符偏移还原后的");
      if (fx.length) lines.push("本次新还原 " + fx.length + " 篇");
      const skip = ((mf.warnings || {}).skipped_no_pdf) || [];
      if (skip.length) lines.push("跳过（无 PDF 附件）" + skip.length + " 条");
      const c = mf.counts || {};
      lines.push("共 " + (c.chunks || 0) + " 个切片 / " + (c.items_in_kb || 0) + " 条文献");
      return lines.join("\n");
    } catch (e) {
      return "（读 MANIFEST 失败：" + e + "）";
    }
  },


  /** 这一条在 Zotero 里有没有 PDF 附件。 */
  itemHasPdf: function (item) {
    try {
      if (!item || !item.isRegularItem || !item.isRegularItem()) return false;
      return item.getAttachments().some((id) => {
        const a = Zotero.Items.get(id);
        return !!(a && a.isPDFAttachment && a.isPDFAttachment());
      });
    } catch (e) {
      return false;
    }
  },


  /** 条目的显示名（弹窗里列清单用）。 */
  itemLabel: function (item) {
    try {
      return String(item.getField("title") || item.key || "").slice(0, 60);
    } catch (e) {
      return String((item && item.key) || "");
    }
  },


  /**
   * 需要用户点「确定」的模态提示。
   *
   * 为什么不用 `notify`（进度窗）：进度窗 6 秒自动消失，"这篇为什么
   * 没进知识库"这种信息用户得看清 —— 而且它是要用户去 Zotero 里
   * 动手补 PDF 的，一闪而过等于没说。
   */
  alertDialog: function (title, text) {
    var self = ZoteroKB;
    try {
      Services.prompt.alert(null, title, text);
    } catch (e) {
      // 拿不到窗口时退回进度窗 —— 至少别把信息吞掉
      self.notify(title, text, null, true);
    }
  },


  /**
   * 逐篇重建知识库条目。**没有 PDF 附件的跳过并弹窗告知。**
   *
   * 后端是 `offline/convert.py --item <key>`（说明见那个文件）——
   * 它会走完整流程：缓存体检 → 灰区模型仲裁 → 字符偏移还原 →
   * 切片 → 向量 → 清单。
   */
  rebuildItems: async function (items) {
    var self = ZoteroKB;
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    const todo = [], noPdf = [];
    for (const it of list) {
      if (self.itemHasPdf(it)) todo.push(it);
      else noPdf.push(it);
    }
    if (noPdf.length) {
      self.alertDialog(
        "没有 PDF 附件，已跳过",
        "下面这些条目在 Zotero 里没有 PDF 附件，知识库不处理它们：\n\n"
        + noPdf.map((x) => "· " + self.itemLabel(x)).join("\n")
        + "\n\n想让它们进知识库：先给条目挂上 PDF（把 PDF 拖到条目上即可），"
        + "再重建一次。");
    }
    if (!todo.length) return;

    const script = self.projectRoot() + "\\offline\\convert.py";
    const args = [script];
    for (const it of todo) args.push("--item", it.key);

    self.notify("正在重建知识库条目…",
                todo.length + " 篇，请稍候（单篇几秒）", null, false);
    let r;
    try {
      r = await self.runPython(args);
    } catch (e) {
      self.alertDialog("重建失败", String((e && e.message) || e));
      return;
    }
    if (r.code === 0) {
      // 关键结果从 MANIFEST.json 读 —— **不解析 stdout**（见 runPython 的说明）
      const summary = await self.manifestSummary();
      self.notify("重建完成",
                  todo.length + " 篇已更新\n" + summary, null, false);
      // 重建可能改变切片数/新增文献，让权重缓存跟上
      try { await self.refreshWeights(); } catch (e) { /* ignore */ }
    } else {
      self.alertDialog(
        "重建失败（退出码 " + r.code + "）",
        "可能的原因看这两处：\n"
        + "· 管理面板「损坏查询」页的日志\n"
        + "· " + self.kbDir() + "\\logs\\ 下的日志\n"
        + (r.out ? ("\n" + String(r.out).slice(-800)) : ""));
    }
  },


  // ================================================================ 分节纲要

  /**
   * 这一篇生成过「分节纲要」没有（右键菜单据此决定跳不跳）。
   *
   * 判据是**文件在不在**（views/<KEY>.outline.md），不是索引库里的行 ——
   * 纲要刻意不入索引（见 offline/digest.py 开头那段），所以文件是唯一权威来源。
   * 走 kbLevels 而不是自己拼路径：路径模板只有 KB_LEVELS 一份，再拼一次
   * 就是第二份实现，早晚漂移（本项目吃过"两份实现必然漂移"的亏）。
   *
   * ⚠ 判不出来（知识库位置未知等）返回 false：交给服务端自己按节指纹复用，
   *   那是诚实的兜底 —— 不能把"我不知道"说成"已经有纲要了"。
   */
  hasOutline: function (key) {
    var self = ZoteroKB;
    try {
      const lv = (self.kbLevels(key) || []).find((x) => x.id === "outline");
      return !!(lv && lv.exists);
    } catch (e) {
      return false;
    }
  },


  /**
   * 给选中的条目生成「分节纲要」（条目右键菜单 → 生成分节纲要）。
   *
   * 后端是服务端的 `POST /digest`（一个异步 job）→ `python offline/digest.py <KEY>`。
   * 为什么不在插件里跑 Python：venv、MinerU 产物、模型全在服务端管 ——
   * 与「重建本条目知识库」同一套分工（见上面 runPython 的说明）。
   *
   * 三条硬规则（都来自本机的实测条件）：
   *   ① **串行**：纲要要一节一节调本地模型，本机是 4B 小模型 + 单 GPU，
   *      并发提交只会让两篇都变慢，还可能把 Ollama 拖住；
   *   ② **失败不中断整批**：一篇没解析过 PDF 的文献不该让后面几篇都不跑 ——
   *      逐篇记结果，最后一起汇总（"失败 K 篇"连原因一起给）；
   *   ③ **已有纲要的跳过**：省在学位论文上最明显（读 MinerU 产物 + 重渲染
   *      + 写回档案都不便宜），而且用户的语义就是"我只要还没有的那些"。
   *      真要重跑已有的一篇：管理面板「AI」页的「生成纲要」按钮（那条路
   *      也是 force=False 的增量，已经跑过的节不会重问模型）。
   *
   * ⚠ 进度窗只有 Zotero.ProgressWindow 一个入口，而它**没有"改文字"的 API**
   *   （只有 changeHeadline / addDescription），所以"更新进度"只能是"关掉旧的、
   *   开一个新的"。为了不闪，只在"换篇"或"日志变了且距上次 ≥4 秒"时重画。
   */
  generateOutlines: async function (items) {
    var self = ZoteroKB;
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    // 同一时刻只跑一批：再点一次是"提示"而不是"叠第二批"
    // （叠起来两批会同时打 Ollama，进度窗也会互相顶掉）。
    if (self.digestActive) {
      const cur = self.digestActive;
      self.alertDialog(
        "已经有一批在跑",
        "正在生成：「" + (cur.currentLabel || cur.currentKey || "") + "」"
        + "（" + (cur.i + 1) + "/" + cur.total + "）\n\n"
        + "要停就再点一次右键菜单里的「停止生成纲要」。");
      return;
    }
    try {
      if (!(await self.healthCheck())) {
        self.notify("知识库引擎没起来",
          "生成纲要要调模型。\n"
          + "先在 工具菜单 →「知识库管理…」→「运行环境」页把环境弄好（缺什么那页会提示）。",
          null, true);
        return;
      }
    } catch (e) {
      // 探活失败也要往下走：真正的失败原因由 POST /digest 给（比这里编一句准）
      Zotero.debug("[zotero-kb] 生成纲要前的探活失败：" + e);
    }

    const todo = [], skipped = [];
    for (const it of list) {
      if (self.hasOutline(it.key)) skipped.push(it);
      else todo.push(it);
    }
    if (!todo.length) {
      self.alertDialog("都已经有分节纲要了",
        "选中的 " + list.length + " 篇都有纲要，这次没有要生成的。\n\n"
        + "想重新生成某一篇：管理面板 →「AI」页 → 选中这篇 →「生成纲要」"
        + "（已有的节按指纹复用，不会重复烧模型）。");
      return;
    }

    const st = self.digestActive = {
      total: todo.length, i: 0, currentKey: "", currentLabel: "",
      generated: [], failed: [],
      skipped: skipped, skippedByServer: [],
      stopped: false, startedAt: Date.now(), lastPaint: 0, lastLog: "",
    };
    Zotero.debug("[zotero-kb] 开始生成分节纲要：" + todo.length + " 篇（已跳过 "
                 + skipped.length + " 篇）");

    const paint = (headline, text, force) => {
      const now = Date.now();
      if (!force && now - (st.lastPaint || 0) < 4000) return;
      st.lastPaint = now;
      try { self.newProgress(headline, text); } catch (e) { /* ignore */ }
    };

    /** 跑一篇：提交 → 轮询 job → 分类结果。**自己吞掉异常**（失败不中断整批）。 */
    const runOne = async (it, idx) => {
      st.i = idx;
      st.currentKey = it.key;
      st.currentLabel = self.itemLabel(it);
      const head = "正在生成分节纲要（" + (idx + 1) + "/" + st.total + "）";
      st.lastPaint = 0;                  // 换篇必须重画一次
      paint(head, st.currentLabel + "\n" + it.key, true);
      let job = null;
      try {
        const res = await self.request("POST", "/digest", { key: it.key });
        const jobId = res && res.job;
        if (!jobId) throw new Error((res && res.error) || "服务端没返回 job id");
        // 学位论文几分钟，给到 30 分钟（真超时也只是"这一篇算失败"，
        // 不中断后面的；服务端那个 job 仍在跑，下一次会命中的它的结果）。
        const deadline = Date.now() + 30 * 60 * 1000;
        while (Date.now() < deadline) {
          if (st.stopped) {
            // 用户点了停止：**每一轮都发一次**取消请求。服务端是"到下一节
            // 边界才收工"，而这一篇可能正卡在一节几分钟的模型调用里，
            // 只发一次不够稳（服务端可能刚好还没进循环）。
            // W3 线2.5（2026-10-14）：桥优先（`kb.digestCancel` 与 HTTP
            // `POST /digest/cancel` **置的是同一个取消标记**），失败退回老 HTTP。
            try {
              await self.kbAdminHostRoute("POST", "/digest/cancel", {});
            } catch (e0) {
              self.request("POST", "/digest/cancel", {}).catch(() => {});
            }
          }
          await new Promise((r) => setTimeout(r, 2000));
          // W3 线2.5：轮询也走桥（`jobs.get` 与 HTTP 同形），失败退回老 HTTP。
          job = await self.kbJobGet(jobId);
          if (!job) break;
          const log = job.log || [];
          const tail = log.length ? String(log[log.length - 1]).trim() : "";
          if (tail) st.lastLog = tail;
          paint(head, st.currentLabel + "\n" + it.key
            + "\n\n已跑 " + Math.round((Date.now() - st.startedAt) / 1000) + " 秒"
            + (st.lastLog ? ("\n" + st.lastLog) : ""), false);
          if (job.state === "done" || job.state === "failed") break;
        }
      } catch (e) {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String((e && e.message) || e) });
        return;
      }
      if (!job || (job.state !== "done" && job.state !== "failed")) {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: "等服务端超时（30 分钟）" });
        return;
      }
      if (job.state === "failed") {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String(job.error || "任务失败") });
        return;
      }
      // ---- 分类这一篇的结果
      // 四种都出现过，所以逐个点名（别用 truthy 一把抓）：
      //   · 真生成了几节；
      //   · 服务端说"一节都没问"（全是节指纹复用）= 跳过；
      //   · 被用户停掉（cancelled）；
      //   · 失败（ok=false，why 里是"没有 MinerU 产物"这类可行动的原因）。
      const r = job.result || {};
      if (r.cancelled) {
        // ⚠ 服务端给的 why 已经是以「已取消（…）」开头的整句，别再包一层
        //   （本机验收第一版就是这么写出「已取消（已取消（4/8 节已跑完…））」的）。
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String(r.why || "已取消") });
      } else if (r.ok) {
        // 「跳过」= **本来就有纲要、这次一节都没问模型**（asked=0 且文件本来就在）。
        // ⚠ 只判 asked=0 是不够的：本机实测有 48 条「meta 里有纲要、但
        //   views/<key>.outline.md 不在」的条目（2026-10-05 那批只落了 meta），
        //   它们这次会把**文件补出来** —— 那是实打实的产出，不是跳过。
        if (r.skipped && r.existed_before) {
          st.skippedByServer.push({ key: it.key, n: r.n_sections || 0 });
        } else {
          st.generated.push({ key: it.key, label: self.itemLabel(it),
                              n: r.n_sections || 0, asked: r.asked || 0,
                              sec: r.seconds || 0 });
        }
      } else {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String(r.why || "未知原因") });
      }
    };

    try {
      for (let i = 0; i < todo.length; i++) {
        if (st.stopped) break;
        await runOne(todo[i], i);
      }
    } catch (e) {
      Zotero.logError(e);
    } finally {
      self.digestActive = null;        // 菜单里的「停止」项随之消失
    }

    // ---- 汇总一句，口径就是用户要的原话：
    //      「已生成 N 篇、跳过 M 篇（已有纲要）、失败 K 篇」
    const nSkip = st.skipped.length + st.skippedByServer.length;
    const parts = ["已生成 " + st.generated.length + " 篇",
                   "跳过 " + nSkip + " 篇（已有纲要）",
                   "失败 " + st.failed.length + " 篇"];
    const secs = Math.round((Date.now() - st.startedAt) / 1000);
    const lines = [parts.join("、") + "　·　共 " + secs + " 秒"];
    // 明细只列前 6 条：进度窗太长了会顶到屏幕上边（它没有滚动条）。
    const detail = [];
    for (const g of st.generated.slice(0, 6)) {
      detail.push("✓ " + (g.label || g.key) + "　" + g.n + " 节"
                  + (g.asked
                     ? ("（新跑 " + g.asked + " 节，" + g.sec + " 秒）")
                     : "（全部复用已有内容，没调模型）"));
    }
    for (const s of st.skipped.slice(0, 3)) {
      detail.push("– 跳过：" + (s.getField ? self.itemLabel(s) : s.key));
    }
    for (const f2 of st.failed.slice(0, 6)) {
      detail.push("✗ " + (f2.label || f2.key) + "\n　　"
                  + String(f2.why).split("\n")[0].slice(0, 90));
    }
    if (st.generated.length + st.failed.length > 12) {
      detail.push("（明细只列前几条，完整结果在知识库的 plugin-status.json）");
    }
    if (st.stopped) detail.unshift("⏹ 中途停止（已经跑完的几篇是有效的）");
    st.summary = lines.concat(detail).join("\n");
    self.lastDigest = {
      at: new Date().toISOString().slice(11, 19), seconds: secs,
      generated: st.generated.length, skipped: nSkip, failed: st.failed.length,
      stopped: !!st.stopped,
      failedKeys: st.failed.map((x) => x.key),
      failedWhy: st.failed.slice(0, 5).map((x) => x.why.slice(0, 120)),
      summary: st.summary,
    };
    try {
      self.notify(st.failed.length ? "分节纲要：有没跑成的" : "分节纲要生成完成",
                  st.summary, null, !!st.failed.length);
    } catch (e) { /* ignore */ }
    // 两列立刻跟上 —— 不必等 15 个 tick（约 30 秒）那次轮询。
    // ⚠ 这只是"提前一下"：轮询没变、列的数据源没变，所以即使这句失败
    //   也不会让列永远停在 ✗。
    try { await self.refreshKbCols(); } catch (e) { /* ignore */ }
  },


  /** 停止正在跑的那一批纲要生成（右键菜单里的「停止生成纲要」）。
   *
   * ⚠ 这不是"杀线程"，而是**置一个标记**：服务端在每节开始前看一眼
   *   （见 localserver.do_digest 的 should_stop）。已经在跑的那一次模型
   *   调用会跑完，所以点完可能还要等十几秒才真的停 —— 提示里要说清，
   *   否则用户以为没点着。
   */
  stopDigest: function () {
    var self = ZoteroKB;
    const st = self.digestActive;
    if (!st) {
      self.notify("现在没有在跑的纲要生成", "", null, true);
      return;
    }
    if (st.stopped) {
      self.notify("已经在停了", "当前这一篇会在这一节跑完后停下。", null, false);
      return;
    }
    st.stopped = true;
    // 主循环里还会反复发（一篇可能正卡在一节长调用里），这里先发一次求快。
    // W3 线2.5：桥优先，失败退回老 HTTP（两处都得改，只改一处会留下隐形 HTTP 依赖）。
    self.kbAdminHostRoute("POST", "/digest/cancel", {}).catch((e0) => {
      Zotero.debug("[zotero-kb] 引擎桥停止纲要失败（退回 HTTP）：" + e0);
      self.request("POST", "/digest/cancel", {}).catch((e) => {
        Zotero.debug("[zotero-kb] 请求停止纲要失败：" + e);
      });
    });
    self.notify("正在停止生成纲要…",
      "当前这一篇会在这一节的模型调用跑完后停下；\n"
      + "已经跑完的几篇不受影响。", null, false);
  },


  /**
   * 打开管理面板。
   *
   * W4（2026-10-14）：**Tk 面板已退役**（`tools\gui.py（已退役 2026-10-14）` 与 `tools\panels\*` 已删），
   * 这里改成打开 **Zotero 内的新面板**（`23-admin.js::kbAdminOpen`；工具菜单里那个
   * 「知识库管理…」走的是同一个入口）。
   *
   * 为什么保留这个函数名：工具栏按钮、条目右键「打开知识库管理面板」、设置页
   * **三处都调它** —— 一处改，三处都不再有死按钮（点了没反应比没有更糟）。
   * 之所以不连这三处入口一起删：它们对用户是有用的（"打开管理面板"这件事没变，
   * 只是面板换了实现），删了反而少一个正经入口。
   */
  openPanel: function () {
    var self = ZoteroKB;
    if (typeof self.kbAdminOpen === "function") {
      try { return self.kbAdminOpen(); }
      catch (e) { Zotero.debug("[zotero-kb] 打开管理面板失败：" + e); }
    }
    Zotero.debug("[zotero-kb] kbAdminOpen 不可用（23-admin.js 没加载？）");
  },


  /**
   * 给条目右键菜单加「发送到 DSH」。
   *
   * 用 popupshowing 事件在**每次弹出时现建**菜单项，这样：
   *   · 选中项数/类型变化时能实时反应（单选/多选）
   *   · 不需要维护菜单项的增删
   * 这是 Zotero 插件加条目标右键菜单最稳的做法（MenuManager 在 10 上
   * 对 collectionTreeRow 的上下文有过破坏性变更，见官方 Zotero 10 说明）。
   */
  registerItemMenu: function (win) {
    var self = ZoteroKB;
    try {
      const doc = (win && win.document)
        || (Zotero.getMainWindow && Zotero.getMainWindow().document);
      if (!doc) return;
      const popup = doc.getElementById("zotero-itemmenu");
      if (!popup) {
        Zotero.debug("[zotero-kb] 找不到 zotero-itemmenu，右键菜单未加");
        return;
      }
      // 避免重复绑定
      if (popup.__kbBound) return;
      popup.__kbBound = true;
      popup.addEventListener("popupshowing", (event) => {
        // ⚠⚠ 关键防护：`popupshowing` **会冒泡**。
        // 不加这个判断的话，鼠标悬停展开**我自己的子菜单**时，子菜单的
        // popupshowing 会冒泡到这里，于是把自己的 menu 删掉重建 ——
        // 表现就是"二级菜单点不开"（刚展开就被自己拆了）。
        // 本机实测踩过：截图里菜单项在，但一悬停就没了。
        // 只处理真正来自 zotero-itemmenu 本身的事件。
        if (event && event.target && event.target !== popup) return;
        try {
          // 清理上一次建的几样东西：六个顶层菜单项 + 上下两个分隔符。
          // 漏掉的话每弹一次右键菜单就会多留一份（会看出来越用越多）。
          // ⚠ 新建的顶层项**必须同时**在这里登记 id，否则下一轮就重复了。
          for (const id of ["zotero-kb-send-menu",
                            "zotero-kb-localmenu",
                            "zotero-kb-classify-item",
                            "zotero-kb-pin-item",
                            "zotero-kb-metafill-item",
                            "zotero-kb-outline-item",
                            "zotero-kb-digest-cancel",
                            "zotero-kb-rebuild-item",
                            "zotero-kb-open-menu",
                            "zotero-kb-sep-before",
                            "zotero-kb-sep-after"]) {
            const old = doc.getElementById(id);
            if (old) old.remove();
          }
          // 取当前选中的条目。
          // Zotero 10 对 ZoteroPane 有些改动，所以这里多路兜底，
          // 任一路能拿到就不至于让菜单消失。
          let items = [];
          try {
            const pane = (typeof Zotero.getActiveZoteroPane === "function"
                          && Zotero.getActiveZoteroPane())
              || (win && win.ZoteroPane) || null;
            if (pane && typeof pane.getSelectedItems === "function") {
              items = pane.getSelectedItems() || [];
            }
          } catch (e) { /* 落到下面 */ }
          if (!items.length) {
            try {
              const w = (win && win.document && win) || Zotero.getMainWindow();
              const sel = w && w.document
                && w.document.getElementById("zotero-items-tree");
              if (sel && sel.view && sel.view.getSelectedItems) {
                items = sel.view.getSelectedItems() || [];
              }
            } catch (e) { /* 再落到下面 */ }
          }
          const real = items.filter((it) => it && !it.isAttachment()
            && !it.isNote() && !it.isAnnotation());
          if (!real.length) return;

          // ---- 标题按"能力状态"给（19-caps.js 探测，菜单只读缓存）
          //
          // 用户 2026-10-05：「右键文献的菜单能实现不填死吗，dsh 没接到就不显示」，
          // 随后拍板成**显示但标「未连接」**（藏起来反而让人找不到入口去修）。
          const dshCap = self.capLabel ? self.capLabel("dsh")
            : { label: "发送到 DSH", tooltip: "" };
          const lmCap = self.capLabel ? self.capLabel("localModel")
            : { label: "连接到本地模型", tooltip: "" };
          const menu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          menu.id = "zotero-kb-send-menu";
          menu.setAttribute("label", dshCap.label);
          if (dshCap.tooltip) menu.setAttribute("tooltiptext", dshCap.tooltip);
          // 对齐其他插件（jasminum / pdf2zh）的样子：menu-iconic + image。
          // 没有这两样时，菜单项在 Zotero 10 里跟内置项长得不一样（没图标、
          // 行高偏小），一眼就能看出是"外来的"。
          menu.setAttribute("class", "menu-iconic");
          menu.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          const mp = doc.createXULElement
            ? doc.createXULElement("menupopup") : doc.createElement("menupopup");
          menu.appendChild(mp);
          // ⚠ 这个辅助必须带 parent 参数（`mkIn`），而 `mk` 只是它的"绑定到
          //   发送子菜单"版本。原因：原来 `mk` 里写死了 `mp.appendChild(mi)`，
          //   于是**所有**用它建的项都落进了「发送到 DSH」子菜单 ——
          //   上一轮加的「重建知识库条目（这一篇）」就是这么被塞进子菜单的
          //   （它跟"发送"毫无关系，本该和「标为重点」并列在顶层）。
          //   这一轮要把补全/重建两项放到顶层，所以拆一层；`mk` 行为完全不变，
          //   提示逻辑也只有一份（本项目吃过"两份实现必然漂移"的亏）。
          const mkIn = (parent, label, fn, opts) => {
            opts = opts || {};
            const mi = doc.createXULElement
              ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
            mi.setAttribute("label", label);
            if (opts.iconic !== false) mi.setAttribute("class", "menuitem-iconic");
            if (opts.image) mi.setAttribute("image", opts.image);
            if (opts.tooltip) mi.setAttribute("tooltiptext", opts.tooltip);
            mi.addEventListener("command", () => {
              // 立刻给可见反馈 —— 之前只有 6 秒的进度窗，而真正发送要等
              // DSH 侧回复（最长可达 2 分钟），看起来就像"点了没反应"。
              //
              // ⚠ 这句原本写死成"已开始发送到 DSH…"，对分类建议 / 标重点 /
              //   重建这些**不发送**的项就是错的话术。改成可覆盖：
              //   `opts.ready` 传空串表示"这一项自己会弹提示，别插嘴"。
              const ready = opts.ready === undefined
                ? "已开始发送到 DSH…" : opts.ready;
              if (ready) {
                try { self.notify(ready, label, null, false); }
                catch (e) { /* ignore */ }
              }
              try { fn(); } catch (e) { Zotero.logError(e); }
            });
            parent.appendChild(mi);
            return mi;
          };
          const mk = (label, fn, opts) => mkIn(mp, label, fn, opts);
          const mkSep = () => {
            const s = doc.createXULElement
              ? doc.createXULElement("menuseparator") : doc.createElement("menuseparator");
            mp.appendChild(s);
          };

          // ---- 子菜单第一行：连接状态（用户要求"在悬浮的下级菜单显示链接没链接"）
          //
          // 为什么这一行要能点：`caps` 可能还没探过（菜单是同步构建的），
          // 点它就是"现在探一次"。点了之后不重建菜单（下次打开自然更新）。
          const dshState = self.capStateLine
            ? self.capStateLine("dsh", "") : { text: "", ok: false };
          mk(dshState.text, () => {
            self.refreshCaps(true).then(() => {
              self.notify("DSH 连接检查",
                (self.capLabel ? self.capLabel("dsh").tooltip : "")
                + "\n\n当前：" + (self.capStateLine
                    ? self.capStateLine("dsh").text : ""), null, true);
            }).catch(() => {});
          }, { iconic: false, ready: "",
               tooltip: "点这一行立刻重新检查 DSH 连接" });

          mk("新建对话…", () => { self.sendToDSH(real, { create: true }); },
             { iconic: false, tooltip: "在 DSH 里开一个新对话，把文献路径发过去" });
          // ⚠ 这一行只是分组标题，**不能用 disabled 的 menuitem** ——
          // 本机实测：menupopup 里只要有 disabled 的 menuitem，子菜单就点不开。
          // 改成不可聚焦的普通 menuitem（去掉 disabled，改为 label 元素语义）。
          const choose = mk("选择已有对话：", () => {},
             { iconic: false, tooltip: "展开下面列表挑一个对话" });
          choose.setAttribute("disabled", "false");
          choose.setAttribute("tabindex", "-1");
          choose.setAttribute("style", "font-weight:600; opacity:0.75;");

          self.listDSHSessions().then((rows) => {
            // ⚠ 用**这次真拿到的结果**回填能力缓存：原来只靠"周期探测"，
            //   于是出现"对话列表都列出来了、标题却写（检测中）"的矛盾
            //   （用户 2026-10-05 截图反馈）。列表能拿到 = DSH 连上了。
            if (rows && rows.length) {
              self.caps.dsh = true;
              self.caps.dshWhy = "";
              self.caps.dshTimeoutAt = 0;
            } else if (self.caps.dsh === null) {
              self.caps.dsh = false;
              self.caps.dshWhy = "能投递但没拿到对话列表（DSH 侧插件没响应？）";
            }
            if (!rows || !rows.length) {
              mkSep();
              mk("（读不到对话列表，DSH 在运行吗？）", () => {}, { iconic: false });
              return;
            }
            mkSep();
            for (const s of rows.slice(0, 15)) {
              const t = (s.title || "").trim() || "(未命名)";
              mk("　" + t.slice(0, 38) + "　·　" + s.id.slice(-6),
                 () => { self.sendToDSH(real, { sessionId: s.id }); },
                 { tooltip: "发送到：" + t + "\n" + s.id });
            }
          }).catch(() => {});

          // ---- 紧挨着的第二项：「分类建议」（用户要求放右键菜单、与发送到 DSH 并列）
          //
          // 为什么放在右键而不是面板里（用户的原话："单篇的分类建议也可以不
          // 在面板，可以在右键的菜单看，正好可以和之前的发送到 dsh 并列"）：
          //   · 在 Zotero 里看到某篇觉得"它该归哪儿"，那一刻就想问 —— 右键是
          //     最短路径；去管理面板还得先找出这篇的 key。
          //   · 弹窗里可以「应用 / 调整 / 跳过」，调整就是跟模型对话改分类。
          // ----「连接到本地模型」二级菜单（用户要求：原来那两条合并到这里）
          //
          // 为什么要合并：两条（分类建议 / 补全元数据）都是"要本地模型"的动作，
          // 平铺在顶层既占地方、又让"模型没连上"这件事没有统一的落点 ——
          // 合并之后标题本身就能写「（未连接）」，提示也只有一份。
          const lmMenu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          lmMenu.id = "zotero-kb-localmenu";
          lmMenu.setAttribute("class", "menu-iconic");
          lmMenu.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          lmMenu.setAttribute("label", lmCap.label);
          lmMenu.setAttribute("tooltiptext", lmCap.tooltip || "");
          const lmPopup = doc.createXULElement
            ? doc.createXULElement("menupopup") : doc.createElement("menupopup");
          lmMenu.appendChild(lmPopup);
          // ⚠ 子菜单里**不能**放 disabled 的 menuitem（会让整个子菜单点不开，
          //   见本文件下面「选择已有对话」那段的教训）。所以"未连接"用一条
          //   普通 menuitem 说明，点了只提示、不做动作。
          {
            // 本地模型子菜单的第一行同样是状态行（与 DSH 一致）；
            // 点击会给"怎么办"的两条路，而不是只报错。
            const lmState = self.capStateLine
              ? self.capStateLine("localModel", "") : { text: "", ok: false };
            mkIn(lmPopup, lmState.text, () => {
              self.refreshCaps(true).then(() => {
                self.notify("本地模型检查",
                  (self.caps.localWhy || "可用的")
                  + "\n\n① 面板「运行环境」→「启动 Ollama」；"
                  + "\n② 或在「模型接入」里改用 API 模型（填地址与 Key）。",
                  null, true);
              }).catch(() => {});
            }, { ready: "", tooltip: lmCap.tooltip || "" });
          }

          const classify = doc.createXULElement
            ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
          classify.id = "zotero-kb-classify-item";
          classify.setAttribute("class", "menuitem-iconic");
          classify.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          classify.setAttribute(
            "label", real.length > 1
              ? ("分类建议 · " + real.length + " 篇")
              : "分类建议");
          classify.setAttribute(
            "tooltiptext",
            "让本地模型判断这篇该归到哪个分类，可一键应用；\n"
            + "不满意可以「调整」——直接告诉模型哪里不对，它会重判。");
          classify.addEventListener("command", () => {
            // 多选时逐篇串行问 —— 一次弹一堆对话框会叠在一起，
            // 而且模型请求并发打过去也容易把本地 Ollama 拖住。
            const runOne = (i) => {
              if (i >= real.length) return;
              try {
                self.notify("正在判断分类…",
                            (i + 1) + "/" + real.length, null, false);
              } catch (e) { /* ignore */ }
              Promise.resolve(self.suggestFor(real[i]))
                .then(() => runOne(i + 1))
                .catch((e) => {
                  Zotero.logError(e);
                  runOne(i + 1);
                });
            };
            try { runOne(0); } catch (e) { Zotero.logError(e); }
          });

          // ⚠ 必须显式挂进「连接到本地模型」子菜单：原来它是靠插入时那句
          //   `menu.after(classify)` 蹭进 popup 的；改成二级菜单后那句没了，
          //   漏了这一行就会"分类建议"整项消失（菜单里不报错，很难查）。
          lmPopup.appendChild(classify);

          // ---- 第三项：标重点 / 取消重点（用户要求"右键菜单没有标重点"）
          //
          // 标签按当前状态动态给：已标重点的显示「取消重点」，
          // 否则显示「标为重点」。状态从权重缓存里查（启动/刷新时拉的），
          // 所以不用发网络请求 —— 右键菜单是同步构建的，等不了网络。
          const pinItem = doc.createXULElement
            ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
          pinItem.id = "zotero-kb-pin-item";
          pinItem.setAttribute("class", "menuitem-iconic");
          pinItem.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          const isPinned = real.every((it) => {
            const w = self.weightsCache && self.weightsCache[it.key];
            return !!(w && w.pinned);
          });
          pinItem.setAttribute(
            "label", (isPinned ? "取消重点" : "标为重点")
              + (real.length > 1 ? ("· " + real.length + " 篇") : ""));
          // ⚠ 这句原来写"权重 ×4"，与实现不符（用户 2026-10-05 指出）。
          //   真实口径：raw = 年份基础(0.95~1.05) + 3.0×重点 + 人工分 + 2.0×有效…
          //   然后**取 1+ln(raw)**；基础权重 1 时标重点 = 1+ln(4) ≈ 2.4 倍，
          //   已有经验时 raw 更大、相对增幅更小（所以写"约"）。
          pinItem.setAttribute(
            "tooltiptext",
            "重点文献在检索时会明显往前排：权重乘数从约 1.0 提到约 2.4 倍"
            + "（算法是 1+ln(年份基础分 + 3×重点 + 经验加分)）。\n"
            + "这是「哪些文献对我重要」的手工标记，"
            + "和「用过哪些方法」的经验是两回事。");
          pinItem.addEventListener("command", () => {
            self.setPinned(real, !isPinned).catch((e) => Zotero.logError(e));
          });

          // ---- 第四项：重建知识库条目（这一篇）
          //
          // 为什么需要逐篇重建：
          //   · 全量重建要 2 分多钟，而新加/改动通常只有一两篇；
          //   · **更要紧的是** —— 某篇的正文提取坏了（例如源 PDF 字体编码坏，
          //     整篇字符偏移：`&DOFXODWLRQ` 本该是 `Calculation`）
          //     **不会改变条目版本号**，增量构建会认为"这篇没变化"直接跳过。
          //     没有强制单篇重建的入口，用户就只能全量重建。
          //
          // ⚠ 它原来是用 `mk(...)` 建的，而 mk 把项塞进「发送到 DSH」子菜单 ——
          //   跟"发送"毫无关系，用户按上一轮的要求期待的是"在「标重点」之后"。
          //   这里改成 `mkIn(popup, ...)` 放到顶层，并给它一个 id 供每次重建菜单
          //   时清理（不给 id 的话每弹一次右键就多留一份，本项目踩过）。
          const rebuild = mkIn(popup, "重建本条目知识库", () => {
            self.rebuildItems(real).catch((e) => Zotero.logError(e));
          }, {
            image: self.rootURI + "toolbar-icon.svg",
            ready: "",          // 它自己会弹进度/结果，不要那句"发送到 DSH"
            tooltip: "重新解析这一篇的正文、切片与向量。\n"
              + "正文乱码、字符错位时用它修（源 PDF 字体编码坏时也修得了）。\n"
              + "单篇只要几秒；全量重建要 2 分钟。\n"
              + "没有 PDF 附件的条目会被跳过，并弹窗告诉你。",
          });
          rebuild.id = "zotero-kb-rebuild-item";

          // ---- 第五项：补全元数据（本地模型）
          //
          // 为什么放右键（用户要求"放在「标为重点」附近"）：
          //   · 用 Zotero 抓文献识别失败很常见（date/DOI/卷/期/页码留空），
          //     而这些值**就印在 PDF 首页**；在 Zotero 里看到"这篇字段是空的"
          //     那一瞬间，右键是最短路径（同「分类建议」的理由）。
          //   · 与分类建议不同的是：这一项**一定会写 Zotero 库**，所以服务端
          //     只出建议，写回必须由用户在弹窗里勾选确认（见 applyMeta）。
          const metaFill = mkIn(lmPopup,
            "补全元数据"
              + (real.length > 1 ? ("· " + real.length + " 篇") : ""),
            () => {
              // ⚠ 单篇 / 多选**走两条不同的路**（用户 2026-10-03 拍板的形态）：
              //   · 只选一篇 → 保留原来的"逐条确认"弹窗。一篇时用户要的是
              //     "把证据逐条看一遍再决定"，一次全采用反而把证据藏起来了；
              //   · 选了两篇及以上 → 先汇总一份清单、一次确认、然后批量写
              //     （就是「一键采用」）。逐篇弹一堆确认框在实际用起来很烦，
              //     而且用户很难判断"一共会改多少个字段"。
              if (real.length >= 2) {
                Promise.resolve(self.metaFillMany(real))
                  .catch((e) => {
                    Zotero.logError(e);
                    self.alertDialog("批量补全失败", String((e && e.message) || e));
                  });
                return;
              }
              // 单篇时逐篇串行（照分类建议的 runOne）。
              // 这里的循环在 real.length>=2 时已经不会被走到（上面提前 return），
              // 保留成循环是因为"只选一篇"和"将来放宽分流阈值"都能直接用。
              const runOne = (i) => {
                if (i >= real.length) return;
                try {
                  self.notify("正在读 PDF 首页找元数据…",
                              (i + 1) + "/" + real.length, null, false);
                } catch (e) { /* ignore */ }
                Promise.resolve(self.metaFillFor(real[i]))
                  .then(() => runOne(i + 1))
                  .catch((e) => {
                    Zotero.logError(e);
                    runOne(i + 1);
                  });
              };
              try { runOne(0); } catch (e) { Zotero.logError(e); }
            },
            {
              image: self.rootURI + "toolbar-icon.svg",
              ready: "",        // 它自己会弹进度/确认窗，不要那句"发送到 DSH"
              tooltip: "从这篇的 PDF 首页里找缺的元数据（日期 / DOI / 卷 / 期 / 页码）。\n"
                + "每条建议都带原文证据与来源（规则 / 模型）与置信度。\n"
                + "只填空、不覆盖，**绝不写作者和标题**；\n"
                + "写进 Zotero 之前要你先在弹窗里确认。",
            });
          metaFill.id = "zotero-kb-metafill-item";

          // ---- 第七项：生成分节纲要（本地模型）
          //
          // 为什么放右键（用户 2026-10-10 的要求）：纲要原来只能在管理面板的
          //   「AI」页点按钮生成；而在 Zotero 列表里看到「分节纲要」那一列是 ✗
          //   的那一刻，右键是最短路径（同「分类建议」「补全元数据」）。
          //
          // ⚠ 不能用 disabled 表达"这篇已经有纲要了" —— menupopup 里只要有一个
          //   disabled 的 menuitem，整个子菜单就点不开（本文件里记了两次的教训）。
          //   所以标题永远可点，跳过逻辑放进命令里，跑完再汇总告诉用户跳过了几篇。
          const missingN = real.filter((it) => !self.hasOutline(it.key)).length;
          const outlineItem = mkIn(lmPopup,
            "生成分节纲要"
              + (real.length > 1
                 ? ("· " + real.length + " 篇"
                    + (missingN < real.length
                       ? ("（跳过已有 " + (real.length - missingN) + "）") : ""))
                 : (missingN ? "" : "（已有，会跳过）")),
            () => {
              Promise.resolve(self.generateOutlines(real))
                .catch((e) => Zotero.logError(e));
            },
            {
              image: self.rootURI + "toolbar-icon.svg",
              ready: "",       // 它自己会弹进度/结果，不要那句"发送到 DSH"
              tooltip: "按章节给这篇写「这一节在做什么 + 关键点/参数/结论」，"
                + "每节带页码范围。\n"
                + "要调本地模型：论文几十秒、学位论文几分钟，逐篇串行跑。\n"
                + "已经有纲要的会跳过；跑完「分节纲要」这一列会变成 N 节。\n"
                + "跑的过程中可以在本菜单里点「停止生成纲要」。",
            });
          outlineItem.id = "zotero-kb-outline-item";

          // 停止项：**只在真有一批在跑时出现**。
          // 用"出现 / 不出现"而不是 disabled —— 见上面的老坑。
          if (self.digestActive) {
            const stopItem = mkIn(lmPopup,
              "停止生成纲要（" + (self.digestActive.i + 1) + "/"
                + self.digestActive.total + "）",
              () => { self.stopDigest(); },
              { image: self.rootURI + "toolbar-icon.svg", ready: "",
                tooltip: "当前这一篇会在这一节的模型调用跑完后停下；\n"
                  + "已经跑完的那几篇不受影响。" });
            stopItem.id = "zotero-kb-digest-cancel";
          }

          // ---- 第六项：打开知识库（分级）
          //
          // 为什么是**二级菜单**（用户 2026-10-05 的要求）：知识库目录里是
          // `papers/22X9PMR6.md` —— 文件名是 Zotero 的 key，人认不出是哪篇；
          // 让用户自己去目录里翻，等于把"找文件"又还给了他。所以：
          // 右键这一篇 → 打开知识库 → 选级别 → 直接打开那个 md。
          //
          // 多选时以**第一项**为准：级别是"某一篇的某个层面"，
          // 多选没有"共同的级别文件"这种东西。
          const first = real[0];
          const levels = self.kbLevels(first.key);
          const missing = levels.filter((lv) => !lv.exists)
            .map((lv) => lv.label);

          const openMenu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          openMenu.id = "zotero-kb-open-menu";
          openMenu.setAttribute("class", "menu-iconic");
          openMenu.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          openMenu.setAttribute(
            "label", "打开知识库" + (real.length > 1 ? "（第一项）" : ""));
          openMenu.setAttribute(
            "tooltiptext",
            "直接打开这一篇的某个层面，不用去知识库目录里按 key 找：\n"
            + "　" + levels.map((lv) => lv.label).join(" / ") + "\n"
            + (real.length > 1 ? "⚠ 一次选了多篇时，以第一项为准。\n" : "")
            + (missing.length
               ? ("⚠ 还没生成：" + missing.join("、")
                  + "\n　去面板的「高级」页点「补齐知识库分级文件」。")
               : "（这一篇的五个层面都已生成）"));

          const op = doc.createXULElement
            ? doc.createXULElement("menupopup") : doc.createElement("menupopup");
          openMenu.appendChild(op);
          for (const lv of levels) {
            const mi = doc.createXULElement
              ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
            mi.setAttribute("class", "menuitem-iconic");
            // ⚠ 缺文件的级别**不能**设 disabled —— 本机实测（见上面
            //   「选择已有对话」那一项的注释）：menupopup 里只要有 disabled 的
            //   menuitem，整个子菜单就点不开。改成"文案里说明 + 点了给提示"。
            mi.setAttribute("label",
              lv.label + (lv.exists ? "" : "（还没生成）"));
            mi.setAttribute("tooltiptext", lv.what + "\n\n" + lv.path);
            mi.addEventListener("command", () => {
              try { self.openKbLevel(first.key, lv.id); }
              catch (e) { Zotero.logError(e); }
            });
            op.appendChild(mi);
          }
          op.appendChild(doc.createXULElement
            ? doc.createXULElement("menuseparator")
            : doc.createElement("menuseparator"));
          const revealItem = mkIn(op, "在文件管理器里显示", () => {
            self.openKbFolder(first.key);
          }, { ready: "", tooltip: "在资源管理器里选中这一篇的知识库文件" });
          revealItem.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          const panelItem = mkIn(op, "打开知识库管理面板", () => {
            self.openPanel();
          }, { ready: "", tooltip: "要做「补齐分级文件」这类批量操作时用它" });
          panelItem.setAttribute("image", self.rootURI + "toolbar-icon.svg");


          // ---- 放到"插件菜单组的最上面"（不依赖任何其他插件）。
          //
          // 定位原理（读 Zotero 源码 `zoteroPane.js` 的 buildItemContextMenu 得到）：
          //   · 内置项由它管理，用**索引**访问：`menu.childNodes[m.reindexItem]`
          //     → 说明内置项只被隐藏、**不会被删除**，顺序固定。
          //   · 它的硬编码内置列表**最后一项是 `reindexItem`**（「重建条目索引」）。
          //   · 各插件菜单是自己在末尾 append 的。
          //
          // ⚠ 这里必须用 reindexItem 这个**确定锚点**，不能靠"哪一项看起来像内置"。
          //   本机踩过：我原来把"无 id 的 menuseparator"也当内置（因为 Zotero 的
          //   内置分隔符确实没有 id），结果**沉浸式翻译加的那个无 id 分隔符**
          //   被误判成内置项，菜单就被插到了 PDF2zh 之后（用户看到位置没变）。
          const AFTER_LABELS = ["重建条目索引", "Reindex Item"];
          const AFTER_IDS = ["zotero-menuitem-reindex"];
          let anchor = null;
          for (const c of popup.children) {
            if (!c || c.nodeType !== 1) continue;
            if ((c.id && AFTER_IDS.includes(c.id))
                || AFTER_LABELS.includes(c.getAttribute("label") || "")) {
              anchor = c;
            }
          }
          Zotero.debug("[zotero-kb] 定位锚点："
            + (anchor ? (anchor.id || anchor.getAttribute("label")) : "未找到"));

          // 上下各加一个分隔符，把这个菜单和别的插件/内置项隔开。
          // 分隔符也带 id，方便下次重建时一起清掉（否则每弹一次菜单就多两个）。
          // 注意：名字别跟上面那个 mkSep()（子菜单分组用）重了，
          // 两者签名不同（一个带 id 一个不带），重名容易埋雷。
          const mkMarkedSep = (id) => {
            const s = doc.createXULElement
              ? doc.createXULElement("menuseparator") : doc.createElement("menuseparator");
            s.id = id;
            return s;
          };

          // ⚠ 相邻已经有分隔符时就别再加了 —— 否则会出现"两个分隔符叠在一起"。
          // 本机实测：jasminum 在自己的菜单前加了个分隔符，正好紧跟在我的下面，
          // 我再加一个就成了连续两条横线（用户截图反馈）。
          // 判据要跳过纯空白文本节点（XUL 源码里的换行），否则永远"后面没东西"。
          const isRealSep = (el) =>
            !!el && el.nodeType === 1 && el.tagName === "menuseparator";
          const prevReal = (el) => {
            let n = el.previousSibling;
            while (n && n.nodeType === 3 && !n.data.trim()) n = n.previousSibling;
            return n;
          };
          const nextReal = (el) => {
            let n = el.nextSibling;
            while (n && n.nodeType === 3 && !n.data.trim()) n = n.nextSibling;
            return n;
          };

          if (anchor && anchor.parentNode === popup) {
            // 上方：锚点后面本来就有分隔符就不用加
            if (!isRealSep(nextReal(anchor))) anchor.after(mkMarkedSep("zotero-kb-sep-before"));
            // 插到"锚点之后、所有相邻分隔符之后"，保证紧跟分隔符下面。
            // 顺序：发送到 DSH（menu）→ 分类建议 → 标重点 → 补全元数据 →
            //       重建 → 打开知识库（分级）
            let ref = anchor;
            while (isRealSep(nextReal(ref))) ref = nextReal(ref);
            ref.after(menu);
            menu.after(lmMenu);
            lmMenu.after(pinItem);
            pinItem.after(rebuild);
            rebuild.after(openMenu);
            // 下方：下一个真实元素已经是分隔符就不用加
            if (!isRealSep(nextReal(openMenu))) openMenu.after(mkMarkedSep("zotero-kb-sep-after"));
            Zotero.debug("[zotero-kb] 菜单已插到「重建条目索引」之后（按需补分隔符）");
          } else {
            // 兜底：锚点找不到（Zotero 改了菜单结构）就放在最前面，
            // 至少保证"在插件组之前"，不会夹在别的插件中间
            const before = mkMarkedSep("zotero-kb-sep-before");
            popup.insertBefore(before, popup.firstChild);
            before.after(menu);
            menu.after(lmMenu);
            lmMenu.after(pinItem);
            pinItem.after(rebuild);
            rebuild.after(openMenu);
            if (!isRealSep(nextReal(openMenu))) openMenu.after(mkMarkedSep("zotero-kb-sep-after"));
            Zotero.debug("[zotero-kb] 找不到内置锚点，菜单放到最前面");
          }
        } catch (e) {
          Zotero.logError(e);
        }
      });
      Zotero.debug("[zotero-kb] 条目右键菜单已绑定");
    } catch (e) {
      Zotero.logError(e);
    }
  },
});

// ===== src/15-prefpane.js =====
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
   * 结果就是插件装上了却没有可用的设置界面（用户没法填项目目录/解释器）。
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

// ===== src/16-weightcol.js =====
/**
 * 16-weightcol.js —— 文献列表里的「知识库权重」自定义列
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 权重列

  /**
   * 在文献列表里加一列「知识库权重」。
   *
   * 关键约束：ItemTree 的 `dataProvider` 是**同步**的、每一行都会调用 ——
   * 不能在里面发网络请求（几百行会打爆本地服务）。所以做法是：
   *   · 启动时（以及手动刷新时）拉一次 `/weights` 全量映射放进内存；
   *   · dataProvider 只做字典查表，O(1)。
   *
   * 显示规则：
   *   · 不在知识库里的条目 → 显示 "—"
   *   · 在库、权重为 1.0（没被经验加权）→ 显示 "1.0"，不加粗
   *   · 权重 > 1（有经验/被标重点）→ 加粗显示，★ 表示标了重点
   * 这样一眼能看出"哪些文献是被验证过的"。
   */
  registerWeightColumn: function () {
    try {
      if (!Zotero.ItemTreeManager || !Zotero.ItemTreeManager.registerColumn) {
        Zotero.debug("[zotero-kb] 这个 Zotero 版本没有 ItemTreeManager，跳过权重列");
        return;
      }
      const self = ZoteroKB;
      // 诊断计数：搞清"列显示了但没值"到底是
      //   A. dataProvider 压根没被调用（providerCalls=0），还是
      //   B. 被调用了但 key 对不上（calls 多、hits 0）
      self.colDiag = { calls: 0, hits: 0, sample: [] };

      const key = Zotero.ItemTreeManager.registerColumn({
        dataKey: "kbWeight",
        label: "知识库权重",
        pluginID: self.id,
        // 只加到主列表（不加到 feed 等）
        enabledTreeIDs: ["main"],
        // 用 flex 而不是固定 width —— Zotero 内置列绝大多数是 flex: 1
        // （见 itemTreeColumns.js：title flex:4、firstCreator flex:1、date flex:1 …），
        // 只有极少数窄列用固定 width。之前我用 width:"96" 会让列在
        // 虚拟表格里定位不自然（用户反馈"不像正常加进去的列"）。
        flex: 1,
        minWidth: 70,
        sortable: true,
        zoteroPersist: ["width", "hidden", "sortDirection"],
        dataProvider: function (item) {
          try {
            const d = self.colDiag;
            if (d) d.calls++;
            if (!item) return "";
            const k = item.key;          // Zotero 条目的 8 位 key，缓存以它做键
            if (!k) return "";
            const w = self.weightsCache[k];
            if (!w) {
              if (d && d.sample.length < 5) d.sample.push("miss:" + k);
              return "";
            }
            if (d) {
              d.hits++;
              if (d.sample.length < 5) d.sample.push("hit:" + k + "=" + w.weight);
            }
            const num = (typeof w.weight === "number") ? w.weight : 1;
            if (Math.abs(num - 1) < 0.005 && !w.pinned) return "1.0";
            // ⚠ 星号放在**数字后面**并留一个空格。
            //   原来写成 "★" + 数字（"★2.61"），用户反馈：
            //   "重点标记也在右键中菜单中显示标记，用的星在权重后面空一点位置显示"
            //   —— 数字在前更符合"权重值 + 一个标记"的读法，
            //   而且列表里所有数值左对齐，数字都在同一列上，扫起来整齐。
            return num.toFixed(2) + (w.pinned ? "  ★" : "");
          } catch (e) {
            return "";
          }
        },
        renderCell: function (index, data, column, isFirstColumn, doc) {
          // ⚠ 必须复刻 Zotero 的标准单元格结构，否则列会"不像正常的列"
          //   （本机踩过：自己造了个裸 <span>，丢了 Zotero 的单元格样式）：
          //     · className 必须含 `cell`（样式/对齐/内边距挂在这个类上）
          //     · 用传入的 document（别用全局 document）
          //     · 空值也要返回元素，不能返回 null
          //   标准实现见 Zotero 的 components/virtualized-table.js：
          //     let span = document.createElement('span');
          //     span.className = `cell ${column.className}`;
          //     span.textContent = data;
          //     if (dir) span.dir = dir;
          const doc2 = doc || (Zotero.getMainWindow ? Zotero.getMainWindow().document
                                                    : document);
          const span = doc2.createElement("span");
          // 自定义列的 column.className 可能是 undefined，兜个底
          span.className = "cell " + ((column && column.className) || "kb-weight");
          span.textContent = data == null ? "" : data;
          if (!data) return span;        // 不在知识库：留空，但保留标准结构
          span.style.fontWeight = "600";
          span.style.fontVariantNumeric = "tabular-nums";
          // 颜色按权重分级 —— 一眼看出"哪些文献是被验证过的"
          //   重点（橙）> 3.0（红）> 2.0（蓝）> 1.0 以上（青）> 1.0（灰）
          // ⚠ 星号现在在**数字后面**（"2.61  ★"），所以判重点要用 contains
          //   而不是 startsWith —— 改格式时这里最容易漏（漏了后果是
          //   重点文献不再显示橙色，看着像"标记丢了"）。
          const pinned = String(data).indexOf("★") >= 0;
          const num = parseFloat(String(data).replace(/[^\d.]/g, "")) || 1;
          let color;
          if (pinned) color = "#e8890c";          // 橙色：标过的重点
          else if (num >= 3.0) color = "#c0392b"; // 红：经验加权最多
          else if (num >= 2.0) color = "#1f6feb"; // 蓝：有明确经验
          else if (num > 1.005) color = "#0e8a7d";// 青：轻微加权
          else color = "#9aa0a6";                 // 灰：没被经验加权
          span.style.color = color;
          span.title = "知识库权重 " + data
            + "（>1 表示被使用经验加权过；★ 是标为重点）";
          return span;
        },
      });
      if (key) {
        this.weightColumnKey = key;
        Zotero.debug("[zotero-kb] 权重列已注册：" + key);
      } else {
        Zotero.debug("[zotero-kb] 权重列注册失败（可能已存在）");
      }
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /**
   * 让文献列表重画（权重变化后星号/颜色要立刻更新）。
   *
   * ⚠ 这里踩过一个**静默失效**的坑（用户报"标了重点但不显示星、权重也没变"）：
   *   原来写的是
   *       if (Zotero.ItemTreeManager && Zotero.ItemTreeManager.refresh) {
   *         Zotero.ItemTreeManager.refresh();
   *       }
   *   而 `Zotero.ItemTreeManager` **根本没有 `refresh` 方法** —— 它只有
   *   `registerColumn` / `unregisterColumn` / `refreshColumns` /
   *   `getCustomColumns` / `isCustomColumn` / `getCustomCellData`
   *   （读 omni.ja 的 xpcom/pluginAPI/itemTreeManager.js 确认）。
   *   于是那个 `if` **永远为假**，列表从来没重画过 ——
   *   而外层还有 `catch {}`，所以连个报错都没有。
   *
   * 正确的重画入口是**虚拟化表格自己**：
   *   `ZoteroPane.itemsView.tree.invalidate()`
   * （Zotero 内部就是这么干的，见 itemTree.js 的 `this.tree.invalidate()`）。
   * 它是"重画当前可见行"、不重新查库，正好适合"数据没变、只是显示值变了"。
   */
  redrawItemTree: function () {
    const done = [];
    try {
      const pane = (typeof Zotero.getActiveZoteroPane === "function"
                    && Zotero.getActiveZoteroPane())
        || (Zotero.getMainWindow && Zotero.getMainWindow().ZoteroPane);
      const tree = pane && pane.itemsView && pane.itemsView.tree;
      if (tree && typeof tree.invalidate === "function") {
        tree.invalidate();
        done.push("tree.invalidate");
      }
      // 更彻底一层：有些情况下（列刚注册、列表还没建）需要整体刷新。
      // 它是 async 的，这里不 await —— 重画是"尽力而为"，不该挡住调用方。
      if (pane && pane.itemsView
          && typeof pane.itemsView.refreshAndMaintainSelection === "function") {
        Promise.resolve(pane.itemsView.refreshAndMaintainSelection())
          .catch(() => {});
        done.push("refreshAndMaintainSelection");
      }
    } catch (e) {
      Zotero.debug("[zotero-kb] 重画列表失败：" + e);
    }
    // 列定义也刷一下：万一权重列是这次才注册上的（启动竞态）
    try {
      if (Zotero.ItemTreeManager
          && typeof Zotero.ItemTreeManager.refreshColumns === "function") {
        Zotero.ItemTreeManager.refreshColumns();
        done.push("refreshColumns");
      }
    } catch (e) { /* ignore */ }
    Zotero.debug("[zotero-kb] 已请求重画列表：" + (done.join(" + ") || "无可用入口"));
    return done;
  },


  /**
   * 拉取全量权重映射填进内存缓存（供同步的 dataProvider 查表）。
   *
   * W3 线2.5（2026-10-13）：**优先走宿主半的引擎桥**（`kbAdminHostRoute("GET","/weights")`
   * → 引擎 `kb.weightsMap`），拿不到就**退回老 HTTP** —— 过渡期两条都必须能用。
   * 为什么先桥后 HTTP：桥不需要那个常驻 8765 服务（引擎按需拉起、随 Zotero 退出），
   * 这正是"去掉常驻服务"要的效果；而 HTTP 兜底保证在引擎起不来时列表还有数。
   * ⚠ 引擎那边**必须**是全量映射（每篇都有基础权重）。用 `advanced.weightList`
   *   （只列有经验的几篇）会让其余全空白 —— 老实现就踩过这个坑。
   */
  refreshWeights: async function () {
    var self = ZoteroKB;
    let res = null;
    try {
      res = await self.kbAdminHostRoute("GET", "/weights");
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥取权重失败（退回 HTTP）：" + e);
    }
    if (!res || !res.weights) {
      try {
        res = await this.request("GET", "/weights");
        if (!res || !res.weights) {
          // 拿不到（引擎刚起、还没就绪）→ 补问一次 healthCheck（问引擎桥的 kb.health），
          // 再试一次。
          await this.healthCheck();
          res = await this.request("GET", "/weights");
        }
      } catch (e) {
        Zotero.debug("[zotero-kb] 拉权重失败：" + e);
      }
    }
    try {
      if (res && res.weights) {
        // ⚠ **内容没变就不重画**。权重现在是每几秒拉一次，若无条件
        //   `redrawItemTree()`，列表会周期性重画 —— 用户正在滚动或
        //   多选时会被打断，看着像"列表在抖"。
        const sig = JSON.stringify(res.weights);
        this.weightsCache = res.weights;
        if (sig === this.weightsSig) return true;
        this.weightsSig = sig;
        Zotero.debug("[zotero-kb] 权重缓存已更新："
                     + Object.keys(res.weights).length + " 条");
        this.redrawItemTree();
        return true;
      }
    } catch (e) {
      Zotero.debug("[zotero-kb] 权重缓存更新失败：" + e);
    }
    return false;
  },


  /** 注销权重列（插件卸载/禁用时调用，避免留下悬空列）。 */
  unregisterWeightColumn: function () {
    try {
      if (this.weightColumnKey && Zotero.ItemTreeManager
          && Zotero.ItemTreeManager.unregisterColumn) {
        Zotero.ItemTreeManager.unregisterColumn(this.weightColumnKey);
        Zotero.debug("[zotero-kb] 权重列已注销");
      }
    } catch (e) { Zotero.logError(e); }
    this.weightColumnKey = null;
  },
});

// ===== src/17-windowhook.js =====
/**
 * 17-windowhook.js —— 主窗口加载/卸载钩子（窗口重建时把 UI 带回来）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 窗口钩子

  /**
   * 主窗口加载时的处理。
   *
   * 官方文档明确要求：与窗口相关的 UI 活动（菜单、自定义列等）应由
   * `onMainWindowLoad` 执行，否则新开的主窗口不会包含这些改动
   * （见 Zotero 中文社区的 bootstrap 参考）。
   * 这里把权重列注册和轮询启动都放一份 —— 窗口重建时能把它们带回来。
   */
  onMainWindowLoad: function (win) {
    var self = ZoteroKB;
    // 诊断计数：本机实测启动时这个钩子**不会被调用**（见 startup 里的说明），
    // 留个计数器便于后续一眼看出"窗口钩子到底跑没跑"。
    self.__windowHookCalled = (self.__windowHookCalled || 0) + 1;
    Zotero.debug("[zotero-kb] onMainWindowLoad #" + self.__windowHookCalled);
    self.mainWindow = win || null;
    // ⚠ 每个窗口都要把自己的 ftl 挂上（新开的窗口里 linkset 是新的）——
    //   少了这一步，那个窗口里的 data-l10n-id 全是空白（见 initLocale）。
    try { self.initLocale(win); }
    catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里挂 ftl 失败：" + e); }
    try {
      if (!self.weightColumnKey) self.registerWeightColumn();
    } catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里注册列失败：" + e); }
    // 「已建知识库」「分节纲要」两列同理（窗口重建后列定义要重新交一遍）
    try {
      if (!self.kbColKeys || !self.kbColKeys.length) self.registerKbColumns();
    } catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里注册两列失败：" + e); }
    // 「知识库」自建列（20-kbview.js）也属于"与窗口相关的 UI"：
    // 每个主窗口各装一份（rail 按钮 + 列 + 页面）。多窗口互不干扰。
    try { self.kbviewMount(win); }
    catch (e2) { Zotero.debug("[zotero-kb] 窗口钩子里装知识库侧栏失败：" + e2); }
    // 工具栏按钮与条目右键菜单也属于"与窗口相关的 UI"，必须在窗口钩子里做
    try { self.registerToolbarButton(win); }
    catch (e) { Zotero.debug("[zotero-kb] 加工具栏按钮失败：" + e); }
    try { self.registerItemMenu(win); }
    catch (e) { Zotero.debug("[zotero-kb] 加右键菜单失败：" + e); }
    // 窗口重建后定时器可能已经没了，重新拉起来
    try {
      if (!self.taskPolling) self.startTaskPolling();
    } catch (e) { Zotero.debug("[zotero-kb] 窗口钩子里启动轮询失败：" + e); }
  },


  onMainWindowUnload: function (win) {
    var self = ZoteroKB;
    Zotero.debug("[zotero-kb] onMainWindowUnload");
    // 清掉注入到该窗口的 DOM（Zotero 会在窗口关闭时销毁，但多窗口时
    // 明确移除更稳，也避免残留引用）
    try {
      const doc = win && win.document;
      if (doc) {
        const btn = doc.getElementById("zotero-tb-kb-panel");
        if (btn) btn.remove();
        const menu = doc.getElementById("zotero-kb-send-menu");
        if (menu) menu.remove();
      }
    } catch (e) { /* ignore */ }
    // 自建列要卸干净（按钮 / 列 / 定时器）—— 窗口关闭后那些 DOM 就没人管了
    try { self.kbviewUnmount(win); } catch (e2) { /* ignore */ }
    self.mainWindow = null;
    // 文档要求：窗口卸载时要停掉计时器，否则可能内存泄漏。
    // ⚠ 只有在**一个主窗口都不剩**时才停 —— 原来是无条件停，于是"关掉第二个窗口"
    //   会把任务轮询一起停掉（而主窗口还在跑），后续 DSH 派活再也没人接。
    //   实测踩到：关掉第二个窗口后，派出去的任务一直 pending 直到超时。
    try {
      const left = Zotero.getMainWindows ? Zotero.getMainWindows().length : 0;
      if (!left) self.stopTaskPolling();
    } catch (e) {
      try { self.stopTaskPolling(); } catch (e2) { /* ignore */ }
    }
  },
});

// ===== src/18-taskpoll.js =====
/**
 * 18-taskpoll.js —— 任务轮询与内建命令（DSH 侧派活、这边执行）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 任务信箱（W3 线2）

  /**
   * 文件信箱根目录（`<知识库>\mailbox`）。
   *
   * 为什么要有这条通道：派活原来走常驻 HTTP 服务 `127.0.0.1:8765` 的 `POST /task`，
   * W3 要把那个常驻进程送走 —— 改成"文件即消息"：派发方往 `requests\` 写一个 JSON，
   * 这边轮询领取（**移动**到 `claimed\`，rename 原子 → 只可能被执行一次），
   * 跑完把结果写进 `results\<id>.json`。与 `zotero-bridge` 的 FLAG_DIR 同一套路。
   *
   * 好处：没有端口、没有 token、**服务没跑也能干活**（Zotero 关着时任务静静躺在
   * requests\ 里，与老 HTTP 队列的行为一样）。
   *
   * 覆盖顺序：pref `zotero-kb.mailboxDir` > `<知识库>\mailbox`。
   * 知识库目录拿不到（kbDir() 为空，且没人填过 pref）时返回空串 —— 上层就跳过这条
   * 通道，不去凭空造目录。
   */
  mailboxDir: function () {
    var self = ZoteroKB;
    try {
      const explicit = Zotero.Prefs.get("zotero-kb.mailboxDir");
      if (explicit && String(explicit).trim()) {
        return String(explicit).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* pref 拿不到就当没填 */ }
    const kb = String((self.kbDir && self.kbDir()) || "").replace(/[\\/]+$/, "");
    return kb ? (kb + "\\mailbox") : "";
  },


  /** 保证 mailbox\ 下三个子目录都在。IOUtils 不可用时抛（上层会记下原因）。 */
  mailboxEnsure: async function (root) {
    for (const sub of ["requests", "claimed", "results"]) {
      await IOUtils.makeDirectory(root + "\\" + sub,
                                  { ignoreExisting: true, createAncestors: true });
    }
    return true;
  },


  /**
   * 领取一个任务（没有就返回 null）。
   *
   * 三条刻意的行为：
   *   · **先 move 再执行** —— move 失败（被派发方撤回了、或已经被别的循环领走）
   *     就跳过这条文件，绝不执行；
   *   · **坏 JSON 直接跳过**（半截文件下次再说，不要因为它崩掉整个轮询）；
   *   · 领取时顺手每小时清一次过期结果（`results\` 是慢性垃圾）。
   */
  pollTaskMailbox: async function () {
    var self = ZoteroKB;
    const root = self.mailboxDir();
    if (!root) return null;
    if (typeof IOUtils === "undefined" || !IOUtils.getChildren) return null;
    await self.mailboxEnsure(root);
    self.mailboxDirPath = root;
    let paths = [];
    try {
      paths = await IOUtils.getChildren(root + "\\requests");
    } catch (e) {
      Zotero.debug("[zotero-kb] 读任务信箱失败：" + e);
      return null;
    }
    // 排序保证先来先做（文件名里带毫秒时间戳）；同一次只领一个，
    // 与老 HTTP 队列"一次领一条"一致，免得两条 acquire 同时跑。
    paths = (paths || []).map(String).filter((p) => p.endsWith(".json")).sort();
    for (const p of paths) {
      let task = null;
      try {
        task = JSON.parse(await IOUtils.readUTF8(p));
      } catch (e) { continue; }                     // 半截/坏文件：下轮再试
      if (!task || !task.id) continue;
      const claimed = root + "\\claimed\\" + task.id + ".json";
      try {
        await IOUtils.move(p, claimed);
      } catch (e) {
        continue;                                   // 被撤回了 / 已被领走
      }
      task.__via = "mailbox";
      task.__mailbox = root;
      task.__claimed = claimed;
      task.__started = new Date().toISOString();
      self.mailboxClaimed = (self.mailboxClaimed || 0) + 1;
      self.mailboxLastTask = task.id + " (" + task.kind + ")";
      self.mailboxLastClaimAt = task.__started;
      return task;
    }
    return null;
  },


  /** 把结果写回信箱（原子写：先 .tmp 再 move，读者永远看不到半截文件）。 */
  reportTaskMailbox: async function (task, ok, error, result) {
    const root = String(task && task.__mailbox || "");
    if (!root) return false;
    const out = {
      id: task.id,
      kind: task.kind || "",
      state: ok ? "done" : "failed",
      ok: !!ok,
      created: task.created || "",
      started: task.__started || "",
      finished: new Date().toISOString(),
      error: error || "",
      result: result == null ? null : result,
      fetched_by: "plugin",
      channel: "mailbox",
    };
    const tmp = root + "\\results\\" + task.id + ".json.tmp";
    await IOUtils.writeUTF8(tmp, JSON.stringify(out, null, 2));
    await IOUtils.move(tmp, root + "\\results\\" + task.id + ".json");
    return true;
  },


  /** 清掉过期的 results / claimed 残留（默认 7 天）。返回删了几个。 */
  mailboxCleanup: async function (maxAgeMs) {
    var self = ZoteroKB;
    const root = self.mailboxDir();
    if (!root || typeof IOUtils === "undefined") return 0;
    const age = Number(maxAgeMs) || (7 * 24 * 3600 * 1000);
    const now = Date.now();
    let removed = 0;
    for (const sub of ["results", "claimed"]) {
      let paths = [];
      try { paths = await IOUtils.getChildren(root + "\\" + sub); } catch (e) { continue; }
      for (const p of (paths || []).map(String)) {
        try {
          const st = await IOUtils.stat(p);
          if (now - Number(st.lastModified || 0) > age) {
            await IOUtils.remove(p);
            removed += 1;
          }
        } catch (e) { /* 单个文件失败不影响别的 */ }
      }
    }
    return removed;
  },


  // ================================================================ 任务轮询

  /**
   * 轮询领取"在 Zotero 里执行 JS"的任务。
   *
   * 为什么做这个：调试插件时想在 Zotero 里跑一段 JS（诊断、刷新权重、验证功能），
   * 原本只能"打开 运行JavaScript 窗口 → 复制 → 粘贴 → Ctrl+R"，一次几十秒。
   * 有了轮询，Python 侧派任务、插件执行、结果回传，全自动。
   *
   * ⚠ **两条队列都消费**（2026-10-13 W3 线2）：
   *   ① **文件信箱**（`<知识库>\mailbox`，新的、首选）—— 不需要服务在跑；
   *   ② **老的 HTTP 队列**（`GET /task`）—— 保留为兜底，直到确认没有调用方。
   *   同一个任务**只会被写进其中一处**，所以不存在双执行；先看信箱、信箱空
   *   再看 HTTP，两处都空就是没事做。想停掉 HTTP 那条就把 prefs 里的
   *   `zotero-kb.taskPollHttp` 设为 false。
   *
   * 安全：老通道走服务 token；新通道只认本机文件系统里的那个目录（没有端口）。
   * 不想开这个能力就把 prefs 里的 autoTaskPoll 设为 false。
   *
   * ⚠ 定时器的坑（本机实测，花了很久）：
   *   `setInterval` 在这个沙箱里**创建成功但回调从不触发**
   *   （心跳数据：taskPolling=true 而 tickCount=0）。
   *   所以改用 **setTimeout 递归**，并且启动时立刻先跑一次 ——
   *   即使定时器机制有问题，第一次也一定能执行（链路能验证）。
   */
  startTaskPolling: function () {
    var self = ZoteroKB;
    if (self.taskPolling) return;
    if (!self.getPref("zotero-kb.autoTaskPoll", true)) {
      Zotero.debug("[zotero-kb] 任务轮询已禁用（prefs: autoTaskPoll=false）");
      return;
    }

    // ---- 定时器自诊断：用 setTimeout 验证 setTimeout 自己能不能用
    // 上几轮排查时完全看不出"定时器到底跑没跑"，只能靠猜。这里直接把结论落盘。
    const diag = {
      setIntervalType: typeof setInterval,
      setTimeoutType: typeof setTimeout,
      clearTimeoutType: typeof clearTimeout,
      setTimeoutSelfTest: "pending",
    };
    self.timerDiag = diag;
    try {
      setTimeout(function () {
        diag.setTimeoutSelfTest = "ok";
        self.timerDiag = diag;
        self.writeStatusFile();
      }, 50);
    } catch (e) {
      diag.setTimeoutSelfTest = "throw: " + e;
      self.timerDiag = diag;
    }

    self.taskPolling = true;
    self.tickCount = 0;
    const INTERVAL = 2000;
    var tick = 0;

    /**
     * 延时调度。
     *
     * 本机实测（心跳为证）：这个沙箱里
     *   · 单次 `setTimeout(fn, 50)` **能**执行（timerDiag.setTimeoutSelfTest="ok"）
     *   · 但**链式递归**的 setTimeout 不触发第二次
     *     （tickCount 卡在 1，而 taskBusy 已回到 false，说明第一次完整跑完）
     * 所以主路径换成 `Services.tm` 的主线程事件目标 ——
     * `Services` 是沙箱里明确提供的特权 API，比沙箱自己的定时器可靠；
     * setTimeout 作为兜底。哪条路生效会记进 status 的 delayBy 字段，
     * 这样"到底谁来调度"是可观测的，不用猜。
     */
    const lateDispatch = (fn, ms) => {
      const d = self.timerDiag || {};
      // 1) Services.tm（主线程事件目标 + nsITimer）
      try {
        const tm = Services.tm;
        const target = tm.mainThread
          || (tm.currentThread && tm.currentThread.QueryInterface(
                Components.interfaces.nsIThread));
        if (target && tm.newTimer) {
          // QueryInterface 声明成 nsITimerCallback，nsITimer 需要它
          let qi;
          try {
            qi = ChromeUtils.generateQI(["nsITimerCallback"]);
          } catch (e) {
            qi = function () { return this; };
          }
          const timer = tm.newTimer({
            observe: function () {
              d.delayBy = "Services.tm";
              d.delayFireCount = (d.delayFireCount || 0) + 1;
              self.timerDiag = d;
              try { fn(); } catch (e) { Zotero.debug("[zotero-kb] 调度回调抛错：" + e); }
            },
            QueryInterface: qi,
          }, ms);
          if (timer) {
            d.lastDelayScheduled = "Services.tm@" + ms;
            self.timerDiag = d;
            return true;
          }
        }
      } catch (e) {
        d.tmError = String(e).slice(0, 200);
        self.timerDiag = d;
      }
      // 2) 沙箱 setTimeout 兜底
      try {
        setTimeout(() => {
          d.delayBy = "setTimeout";
          d.delayFireCount = (d.delayFireCount || 0) + 1;
          self.timerDiag = d;
          try { fn(); } catch (e) { Zotero.debug("[zotero-kb] 调度回调抛错：" + e); }
        }, ms);
        d.lastDelayScheduled = "setTimeout@" + ms;
        self.timerDiag = d;
        return true;
      } catch (e) {
        d.setTimeoutError = String(e).slice(0, 200);
        self.timerDiag = d;
      }
      return false;
    };

    const scheduleNext = (ms) => {
      if (!self.alive || !self.taskPolling) return;
      if (!lateDispatch(() => runOnce(), ms)) {
        // 两条路都不可用：不再静默停掉，记下来（避免"轮询悄悄死了"）
        const d = self.timerDiag || {};
        d.scheduleFailed = true;
        self.timerDiag = d;
        Zotero.debug("[zotero-kb] 无法排下一次轮询（Services.tm 与 setTimeout 都失败）");
      }
    };

    const runOnce = async () => {
      if (!self.alive || !self.taskPolling) return;
      tick++;
      self.tickCount = tick;
      self.lastTickAt = new Date().toISOString().slice(11, 19);
      // 先排下一次：即使本次抛错也不会断掉循环
      scheduleNext(INTERVAL);
      if (self.taskBusy) return;
      self.taskBusy = true;
      try {
        // ① 新通道：文件信箱（不需要服务在跑）。领取一个就执行一个。
        let task = null;
        try {
          task = await self.pollTaskMailbox();
        } catch (e) {
          Zotero.debug("[zotero-kb] 任务信箱轮询失败：" + e);
        }
        // ② 老通道：HTTP 队列（兜底；没有调用方了就把它关掉）。
        if (!task && self.getPref("zotero-kb.taskPollHttp", true)) {
          try {
            const res = await self.request("GET", "/task");
            if (res && res.task) { task = res.task; task.__via = "http"; }
          } catch (e) {
            // 服务没跑是最常见的情况（W3 之后本来就会没跑），不必刷日志
          }
        }
        if (task) {
          Zotero.debug("[zotero-kb] 领取任务 " + task.id + "（" + task.kind
                       + "，via=" + task.__via + "）");
          await self.runTask(task);
        }
        // 结果文件是慢性垃圾：每小时清一次过期的（不能跟着 2 秒的轮询做）。
        if (tick % 1800 === 1) {
          try {
            const n = await self.mailboxCleanup();
            if (n) Zotero.debug("[zotero-kb] 清掉 " + n + " 个过期任务文件");
          } catch (e) { /* 清理失败不影响轮询 */ }
        }
        // 权重刷新：每 2 个 tick（INTERVAL=2000ms → 约 4 秒）。
        //
        // 为什么要缩短：原来 30 秒一次，用户在别处（DSH 记经验、管理面板
        // 改权重）动了权重后，列表里的数字要等半分钟才跟上，像"没生效"。
        // 本地 HTTP + 2KB 数据，4 秒一次的开销可以忽略；重画有内容比对兜底
        // （见 refreshWeights），没变化不会打断列表。
        //
        // 状态文件仍然 30 秒写一次 —— 那是磁盘 IO，没必要跟着变快。
        if (tick % 2 === 0) {
          await self.refreshWeights();
        }
        // 两列（已建库 / 纲要）变得**慢得多**：建一次库、生成一次纲要才变，
        // 所以 15 个 tick（INTERVAL=2000ms → 约 30 秒）拉一次就够。
        // 和权重一样，内容没变不会重画（见 refreshKbCols 的内容比对）。
        if (tick % 15 === 0) {
          await self.refreshKbCols();
          self.writeStatusFile();
        }
      } catch (e) {
        Zotero.debug("[zotero-kb] 任务轮询失败：" + e);
      } finally {
        self.taskBusy = false;
      }
    };

    // 立刻跑一次（不依赖定时器）
    runOnce();
    Zotero.debug("[zotero-kb] 任务轮询已启动（setTimeout 递归，每 "
                 + INTERVAL + "ms）");
  },


  stopTaskPolling: function () {
    var self = ZoteroKB;
    self.taskPolling = false;
    if (self.taskTimer) {
      try { clearTimeout(self.taskTimer); } catch (e) { /* ignore */ }
      self.taskTimer = null;
    }
    Zotero.debug("[zotero-kb] 任务轮询已停止（tickCount="
                 + (self.tickCount || 0) + "）");
  },


  /** 执行一个任务并把结果回报给服务。 */
  runTask: async function (task) {
    var self = ZoteroKB;
    const started = Date.now();
    let ok = false;
    let result = null;
    let error = "";
    try {
      if (task.kind === "cmd") {
        result = await self.runBuiltinCommand(task.code);
      } else if (task.kind === "acquire") {
        // DSH 搜好、用户挑过的 DOI 列表 → 交给 Zotero 自己的抓取链路入库。
        // 载荷是 JSON 字符串；实现在 runAcquire（那里写清了为什么放在插件里）。
        // ⚠ 这是**唯一**会由外部（DSH）触发写库的任务类型，所以它自带一个
        //   确认框（`zotero-kb.acquireConfirm`），别的 kind 都不写库。
        result = await self.runAcquire(task.code);
      } else if (task.kind === "attach") {
        // 兜底路径：DSH 侧用脚本下好的 PDF，挂到刚建的条目上。
        result = await self.runAttach(task.code);
      } else {
        // 用 AsyncFunction 包一层，这样任务里的顶层 await 也能用
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
        const fn = new AsyncFunction("Zotero", "Services", "ChromeUtils",
                                     '"use strict";\n' + task.code);
        result = await fn(Zotero, Services, ChromeUtils);
      }
      ok = true;
    } catch (e) {
      error = String(e) + (e && e.stack ? "\n" + String(e.stack).slice(0, 600) : "");
      Zotero.logError(e);
    }
    const ms = Date.now() - started;
    // 回报结果：在哪领的就回哪去（信箱任务绝不能往 HTTP 回，反之亦然 ——
    // 回错地方派发方永远等不到结果，而且看起来像"任务丢了"）。
    try {
      if (task.__via === "mailbox") {
        await self.reportTaskMailbox(task, ok, error,
          { value: self.safeStringify(result), ms: ms, pluginVersion: self.version });
      } else {
        await self.request("POST", "/task/result", {
          id: task.id, ok: ok, error: error,
          result: { value: self.safeStringify(result), ms: ms,
                    pluginVersion: self.version },
        });
      }
    } catch (e) {
      Zotero.debug("[zotero-kb] 回报任务结果失败：" + e);
    }
    Zotero.debug("[zotero-kb] 任务 " + task.id + (ok ? " 完成" : " 失败")
                 + "（" + ms + "ms，via=" + task.__via + "）");
    return ok;
  },


  /** 内建命令（不必每次传 JS 源码过去）。 */
  runBuiltinCommand: async function (name) {
    var self = ZoteroKB;
    switch (name) {
      case "status":
        return {
          version: self.version,
          zoteroVersion: Zotero.version,
          serverOk: self.serverOk,
          weightColumnKey: self.weightColumnKey,
          weightsCached: Object.keys(self.weightsCache || {}).length,
          kbColKeys: self.kbColKeys || [],
          colStatusCached: Object.keys(self.colStatusCache || {}).length,
          colStatusReady: !!self.colStatusReady,
          notifyIDs: self.notifyIDs || [],
          taskPolling: !!self.taskTimer,
        };
      case "refreshWeights":
        return { ok: await self.refreshWeights(),
                 cached: Object.keys(self.weightsCache || {}).length };
      case "healthCheck":
        return { ok: await self.healthCheck(), info: self.serverInfo };
      case "reloadWeightsCache":
        self.weightsCache = {};
        return { ok: await self.refreshWeights() };
      case "refreshKbCols":
        // 两列（已建库 / 纲要）的缓存重拉一次 —— DSH 那边刚建完库/生成完
        // 纲要时用它让列表立刻反映出来，不必等 30 秒的轮询。
        return { ok: await self.refreshKbCols(),
                 cached: Object.keys(self.colStatusCache || {}).length,
                 ready: !!self.colStatusReady };
      // ⚠ 这里**故意没有**"批量写回"这类内建命令：
      //   改**已有条目**的字段只应该由用户在 Zotero 界面里的动作触发
      //   （右键菜单 → 确认框）。批量写回的实现是 `applyMetaBatch`，
      //   它只由 `metaFillMany` 调 —— 而 metaFillMany 只由右键菜单调。
      //
      //   唯一的例外是 `kind: "acquire"`（**新建**条目，不动已有数据）：
      //   DSH 那边搜好、用户在对话里挑过之后，由任务队列触发。它自带一个
      //   确认框（`zotero-kb.acquireConfirm`），见 runAcquire。
      default:
        throw new Error("未知内建命令: " + name);
    }
  },


  /** 把任意值转成可 JSON 化的字符串（避免循环引用把回报打挂）。 */
  safeStringify: function (v) {
    if (typeof v === "string") return v;
    try {
      return JSON.stringify(v);
    } catch (e) {
      try { return String(v); } catch (e2) { return "(无法序列化)"; }
    }
  },
});

// ===== src/19-caps.js =====
/**
 * 19-caps.js —— 「能力探测」：DSH 连上了没、本地模型可用不可用。
 *
 * ## 为什么单独一个文件
 *
 * 右键菜单要按"当前有没有这个能力"改文案（用户 2026-10-05 的要求：
 * 「右键文献的菜单能实现不填死吗，dsh 没接到就不显示，本地模型（或者接外部 api）
 * 没读取到也不显示对应的两条」→ 后来拍板成**显示但标「未连接」**）。
 * 而"怎么判断有没有"这件事不该散在菜单代码里（菜单是同步构建的，
 * 探测是异步的、还会失败），所以：
 *
 *   · 本文件只负责**探测 + 缓存 + 给出文案**（纯函数 `capLabel` 便于桩测试）；
 *   · `14-menus.js` 只读缓存（`caps.dsh` / `caps.localModel`），不自己探测。
 *
 * ## 三种状态，不是两种
 *
 * `true` = 确认可用；`false` = 确认不可用；`null` = **还没探到 / 探不出来**。
 * 为什么要留 `null`：本机常年"Ollama 装了但没启动"、"DSH 装着但没开"，
 * 把"未知"当成"不可用"会让用户在最需要入口的时候看不到任何提示。
 * 所以文案分三档：正常 / （未连接）/ （检测中）。
 *
 * ## 探测的成本与"别乱丢文件"
 *
 * DSH 那条是真的往桥的 `requests/` 里投一个 `list-sessions` 请求（几千字节），
 * 所以：① 只在启动后与超过 TTL 时探；② **上次超时后的 5 分钟内不再探**
 * （否则 DSH 没开时会每 30 秒往那个目录里堆一个没人处理的请求文件）；
 * ③ 超时后**把自己的那个请求文件删掉**（只删自己刚写的那个，绝不碰别的）。
 */
Object.assign(ZoteroKB, {

  /** 能力缓存：`true` / `false` / `null`（未知）。菜单只读它。 */
  caps: { dsh: null, dshWhy: "", localModel: null, localWhy: "", at: 0,
          dshTimeoutAt: 0 },

  CAP_TTL_MS: 30000,            // 缓存有效期
  DSH_TIMEOUT_BACKOFF_MS: 300000,   // 上次探测超时后的冷却（5 分钟）


  /**
   * 探一次能力（异步，**不抛**）。`force=true` 时忽略 TTL。
   *
   * ⚠ 菜单是同步构建的，所以这里**绝不能**被菜单直接 await —— 菜单读 `caps`，
   *   这里在后台更新它（并在更新后不做任何界面重绘：下次弹出菜单自然是新的）。
   */
  refreshCaps: function (force) {
    var self = ZoteroKB;
    const now = Date.now();
    if (!force && self.caps.at && (now - self.caps.at) < self.CAP_TTL_MS) {
      return Promise.resolve(self.caps);
    }
    self.caps.at = now;
    // ⚠ 先刷一次 /health：`probeLocalModel` 读的是 `serverInfo`，而它只在
    //   startup（以及权重列 401 重试）时取过 —— 不在这里刷新的话，
    //   「先开 Zotero、再开 Ollama」永远等不到状态变对（本机 2026-10-10
    //   实测：caps.localWhy 一直停在"没找到 Ollama"）。/health 是本机一次
    //   HTTP，很便宜；失败也不抛（healthCheck 自己 try/catch）。
    return Promise.resolve(
      self.healthCheck ? self.healthCheck() : null
    ).catch(function () { return null; }).then(function () {
      return Promise.all([
        self.probeDsh(force).catch(function () { return null; }),
        Promise.resolve(self.probeLocalModel()),
      ]);
    }).then(function (r) {
      self.caps.dsh = r[0] === null ? self.caps.dsh : !!r[0];
      self.caps.localModel = r[1];
      return self.caps;
    });
  },


  /**
   * DSH 桥是否活着。返回 `true` / `false`（拿不到就是 false，并把原因写进 caps）。
   *
   * 判据分两步（先便宜后昂贵）：
   *   ① 桥目录 `<home>\.dsh\zotero-bridge\requests` 存在吗 —— 不存在说明
   *      DSH 侧插件从没装载过（一条 `IOUtils.exists` 就够，零成本）；
   *   ② 投一个 `list-sessions` 请求，等 3 秒 —— 有回复才叫"接上了"。
   */
  probeDsh: async function (force) {
    var self = ZoteroKB;
    const root = self.bridgeDir();
    const reqDir = root + "\\requests";
    const resDir = root + "\\results";
    if (!self.bridgeDirPath || !root) {
      self.caps.dshWhy = "拿不到桥目录（~/.dsh/zotero-bridge）";
      return false;
    }
    let exists = false;
    try {
      exists = await IOUtils.exists(reqDir);
    } catch (e) {
      exists = false;
    }
    if (!exists) {
      self.caps.dshWhy = "桥目录不存在（DSH 侧插件未装载）";
      return false;
    }
    // 上个探测刚超时过 → 冷却期内不再打扰那个目录
    if (!force && self.caps.dshTimeoutAt
        && (Date.now() - self.caps.dshTimeoutAt) < self.DSH_TIMEOUT_BACKOFF_MS) {
      self.caps.dshWhy = "上次探测超时（冷却中，请确认 DSH 在运行）";
      return false;
    }
    const id = "z" + Date.now().toString(36)
      + Math.random().toString(36).slice(2, 7);
    const dst = reqDir + "\\" + id + ".json";
    const resFile = resDir + "\\" + id + ".json";
    try {
      await IOUtils.writeUTF8(dst, JSON.stringify(
        { id: id, kind: "list-sessions", at: new Date().toISOString() }, null, 1));
    } catch (e) {
      self.caps.dshWhy = "投递请求失败：" + e;
      return false;
    }
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      await new Promise(function (r) { setTimeout(r, 250); });
      try {
        if (await IOUtils.exists(resFile)) {
          self.caps.dshWhy = "";
          self.caps.dshTimeoutAt = 0;
          return true;
        }
      } catch (e) { /* 下一轮再试 */ }
    }
    // 超时：把自己刚写的请求文件删掉（只删自己这个，别碰 DSH 的目录）
    self.caps.dshTimeoutAt = Date.now();
    self.caps.dshWhy = "投了请求但 3 秒没回复（DSH 没在运行？）";
    try {
      await IOUtils.remove(dst, { ignoreAbsent: true });
    } catch (e) { /* 删不掉也无妨 */ }
    return false;
  },


  /**
   * 本地模型可用不可用。返回 `true` / `false` / `null`（未知）。
   *
   * 判据用**已经拿到的**信息，不再发新请求：
   *   · 插件 pref 里选了 `openai` → 有 apiKey（和 baseUrl）就算可用；
   *   · 选了 `ollama` → 看 `serverInfo.ollama`（`/health` 报的：文件在不在 +
   *     API 通不通 + 有哪些模型）→ `api_up && models.length` 才算可用；
   *   · 拿不到 `/health` 的结果 → `null`（未知），界面上写"检测中"。
   */
  probeLocalModel: function () {
    var self = ZoteroKB;
    let provider = "";
    try {
      provider = String(self.getPref(self.PREFS.provider, "") || "").trim();
    } catch (e) { provider = ""; }
    if (!provider) provider = "ollama";
    if (provider === "openai") {
      const key = String(self.getPref(self.PREFS.apiKey, "") || "").trim();
      const base = String(self.getPref(self.PREFS.apiBaseUrl, "") || "").trim();
      self.caps.localWhy = key ? "" : "选了 API 但没填 API Key（运行环境→模型接入）";
      return !!key && !!base;
    }
    let info = null;
    try {
      // ⚠ 优先读**结构化**的 `ollama_info`（服务端 2026-10-10 新增）。
      //   在那之前 `/health` 只回报 `ollama`（一个**路径字符串**），
      //   而下面的判据按 `info.exists / info.api_up` 读 —— 于是 `info.exists`
      //   恒为 undefined、永远走"没找到 Ollama"：明明连上了也显示「未连接」
      //   （用户 2026-10-10 报的正是这个）。
      const si = self.serverInfo || {};
      info = si.ollama_info || null;
      if (!info && typeof si.ollama === "string" && si.ollama) {
        // 老服务：只证明"装过"，不知道 API 通不通、有哪些模型 → 按未知处理
        info = { path: si.ollama, exists: true, api_up: null, models: null };
      }
      if (!info && si.ollama && typeof si.ollama === "object") {
        info = si.ollama;    // 兼容"将来 ollama 直接给结构"的服务端
      }
    } catch (e) { info = null; }
    if (!info) {
      self.caps.localWhy = "还没拿到运行环境状态（点面板「运行环境」看一眼）";
      return null;
    }
    if (!info.exists) {
      self.caps.localWhy = "没找到 Ollama（可选组件；也可改用 API 模型）";
      return false;
    }
    if (typeof info.api_up !== "boolean") {
      self.caps.localWhy = "只拿到 Ollama 路径、拿不到运行状态（服务端版本较旧？）";
      return null;
    }
    if (!info.api_up) {
      self.caps.localWhy = "Ollama 没在运行（面板「运行环境」页点「启动 Ollama」）";
      return false;
    }
    const models = info.models || [];
    if (!models.length) {
      self.caps.localWhy = "Ollama 在跑，但一个模型都没有（ollama pull qwen3:4b-instruct）";
      return false;
    }
    self.caps.localWhy = "";
    return true;
  },


  /**
   * 菜单文案（**纯函数**，桩环境直接测）：按能力状态给标题与提示。
   *
   * `kind` = "dsh" | "localModel"；返回 `{label, tooltip}`。
   * 三档：正常 → 原名；确认不可用 → 附「（未连接）」+ 怎么办；未知 → 「（检测中）」。
   */
  capLabel: function (kind) {
    var self = ZoteroKB;
    const st = (kind === "dsh") ? self.caps.dsh : self.caps.localModel;
    if (kind === "dsh") {
      // ⚠ 顶层标题**不带后缀**（用户 2026-10-05：「现在动态的加（未连接）
      //   导致太宽了，去掉吧」）。连接状态改用 `capStateLine()` 放进**下级菜单**。
      return { label: "发送到 DSH",
               tooltip: "把这篇的路径发到 DSH 里（新建对话或选已有对话）"
                 + (st === false ? ("\n\n⚠ 现在没连上：" + (self.caps.dshWhy || "")) : "") };
    }
    return { label: "连接到本地模型",
             tooltip: "用本地模型做「分类建议」「补全元数据」和「生成分节纲要」"
               + (st === false
                  ? ("\n\n⚠ 现在不可用：" + (self.caps.localWhy || "")) : "") };
  },


  /**
   * 下级菜单里的**状态行**文案（纯函数，可桩测）。
   *
   * 为什么放这里而不是标题上：用户 2026-10-05 反馈"标题加（未连接）太宽"，
   * 要求"在悬浮的下级菜单显示链接没链接"。顺带解决另一个问题：菜单是同步构建的，
   * 缓存里可能是 `null`（还没探到）—— 那时**不写"检测中"**，而是据实说
   * "还没检查"（点了就是检查），免得出现"对话都列出来了却写检测中"的矛盾。
   *
   * `extra` 用来塞"这次真的看到的"信息（例如对话条数 / 模型名）。
   */
  capStateLine: function (kind, extra) {
    var self = ZoteroKB;
    const st = (kind === "dsh") ? self.caps.dsh : self.caps.localModel;
    const why = (kind === "dsh") ? self.caps.dshWhy : self.caps.localWhy;
    const tail = extra ? ("　" + extra) : "";
    if (st === true) {
      return { text: (kind === "dsh" ? "● 已连接 DSH" : "● 本地模型可用") + tail,
               ok: true };
    }
    if (st === false) {
      return { text: (kind === "dsh" ? "○ 未连接：" : "○ 不可用：")
                     + (why || "原因未知"), ok: false };
    }
    return { text: (kind === "dsh" ? "○ 还没检查过连接" : "○ 还没检查过本地模型")
                   + "（点这一行重新检查）", ok: false };
  },


  /** 排一次周期探测（用 setTimeout 链，不用 setInterval —— 沙箱里后者可能不触发）。 */
  scheduleCapsRefresh: function () {
    var self = ZoteroKB;
    const tick = function () {
      self.refreshCaps(false).catch(function () { /* 探测失败不是错误 */ });
      setTimeout(tick, 60000);
    };
    setTimeout(tick, 12000);      // 启动 12 秒后第一次（等 healthCheck 先跑完）
  },
});

// ===== src/19-mineruguide.js =====
/**
 * 19-mineruguide.js —— **可选组件**的首次安装引导（MinerU 与 Ollama 两个）。
 *
 * ## 用户定的规矩（2026-10-05）
 *
 *   · 插件**首次启动**、且**没检测到**某个可选组件时，弹**一次**对话框：
 *     「打开安装引导」/「以后再说」。
 *   · 选「以后再说」就收进角落 —— 入口留在面板「知识库结构」页那些按钮里
 *     （面板那边**只在没装时才显示**，装了按钮就消失）。
 *   · 所以：**无论选哪个都记 `optionalGuideDone`**，不再打扰。
 *   · 已经装齐的用户**一次都不弹**（连 pref 都不写）。
 *
 * 两个组件：
 *   · **MinerU**（PDF 解析增强，装了公式变 LaTeX）
 *   · **Ollama**（本地模型后端，装了分类/摘要能离线跑）—— 用户后加的要求：
 *     「『ollama 程序』能不能也像 minerU 那样做成『ollama（可选）』然后也有
 *     一样逻辑的图形化引导」。
 *
 * ## 为什么先本地看一眼、再问服务
 *
 * MinerU 的默认位置是项目目录下的 `.mineru\.venv\Scripts\mineru-kit.exe`
 * （见 scripts/install-mineru.ps1）；Ollama 的默认位置是
 * `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`。这两个判断**不需要服务在跑**，
 * 一次 `exists` 就够，成本最低、也最不容易误报"没装"。
 * 但也可能装在别处（PATH 上、或用户自定义路径），那只有服务端知道：
 * MinerU 走 `/mineru-check`（`schemas.resolve_mineru()` 六级优先级），
 * Ollama 走 `/health`（里面带 `ollama` 字段，同样是 `resolve_ollama()`）。
 * 服务没起来就按"本地看到什么算什么"处理。
 *
 * ## 为什么引导在 Zotero 里做（W4 2026-10-14 改写）
 *
 * 原来这一步是"拉起 Tk 面板的安装向导"（向导逻辑原本在 `tools/panels/*_guide.py`，
 * 那套代码已随 Tk 面板一起退役 2026-10-14）。
 * Tk 面板退役后那套代码已删，MinerU 的入口统一到**新面板的「PDF 解析」页**
 * （Ollama 在「运行环境」页）；本文件保留的是"首页首次引导对话框"。
 */
Object.assign(ZoteroKB, {

  /** MinerU 默认安装在项目内的位置（与 install-mineru.ps1 的约定一致）。 */
  mineruKitPath: function () {
    const root = this.projectRoot ? this.projectRoot() : "";
    return root ? root + "\\.mineru\\.venv\\Scripts\\mineru-kit.exe" : "";
  },


  /** Ollama 默认安装位置（按用户安装，不需要管理员）。 */
  ollamaExePath: function () {
    // 用 %LOCALAPPDATA% 拼（Ollama 官方安装器就装在那里）。拿不到就返回空 ——
    // 这是"快速看一眼"用的，真正的判定还有服务端 `/health` 兜底，
    // 所以宁可不猜也不写死某个用户名的路径。
    try {
      const env = Services.env || Components.classes[
        "@mozilla.org/process/environment;1"]
        .getService(Components.interfaces.nsIEnvironment);
      const lad = env.get("LOCALAPPDATA") || "";
      if (lad) return lad + "\\Programs\\Ollama\\ollama.exe";
    } catch (e) { /* ignore */ }
    return "";
  },


  /**
   * 排一次首启引导检查。
   *
   * 延迟 8 秒：① 让主窗口、设置面板先就绪（首屏别抢时间）；
   * ② 让本地服务有机会起来（`/health` 那次探测在 startup 里）。
   * 用 `setTimeout` 而不是窗口钩子 —— 本机实测 `onMainWindowLoad` 在
   * **Zotero 启动时已存在的那个窗口**上不会被调用（见 00-core.js 的说明）。
   */
  scheduleOptionalGuides: function () {
    var self = ZoteroKB;
    setTimeout(function () {
      try { self.maybeAskOptionalGuides(); } catch (e) {
        Zotero.debug("[zotero-kb] 可选组件引导检查失败：" + e);
      }
    }, 8000);
  },


  /** 首启检查：都装了什么都不做；缺哪个就弹一次（然后就再也不弹）。 */
  maybeAskOptionalGuides: async function () {
    var self = ZoteroKB;
    // 新 pref 优先；老的 mineruGuideDone 也认（升级上来的用户不会被再烦一次）
    if (self.getPref(self.PREFS.optionalGuideDone, false)
        || self.getPref(self.PREFS.mineruGuideDone, false)) return;

    const missing = [];
    // ① MinerU：先看本地文件，再问服务
    const kit = self.mineruKitPath();
    if (!(kit && self._exists && self._exists(kit))) {
      let seen = false;
      // W3 线2.5（2026-10-14）：桥优先（`mineru.checkRaw` = `mineru.probe()` **原文**，
      // 与 HTTP `/mineru-check` 同一份），拿不到再退回老 HTTP。
      try {
        let info = null;
        try { info = await self.kbAdminHostRoute("GET", "/mineru-check"); }
        catch (e0) {
          Zotero.debug("[zotero-kb] 引擎桥查 MinerU 失败（退回 HTTP）：" + e0);
          info = await self.request("GET", "/mineru-check");
        }
        seen = !!(info && info.ok);
      } catch (e) {
        Zotero.debug("[zotero-kb] 查 MinerU 不可用（引擎与老服务都没起来？）：" + e);
      }
      if (!seen) missing.push("mineru");
    }
    // ② Ollama：先看本地文件，再问 /health
    const ol = self.ollamaExePath();
    if (!(ol && self._exists && self._exists(ol))) {
      let seen = false;
      try {
        // W3 线2.5：桥优先（`kb.health` 里带 ollama 路径），退回老 HTTP。
        let info = null;
        try { info = await self.kbAdminHostRoute("GET", "/health"); }
        catch (e0) { info = await self.request("GET", "/health"); }
        seen = !!(info && info.ollama);
      } catch (e) {
        Zotero.debug("[zotero-kb] /health 不可用（服务没起来？）：" + e);
      }
      if (!seen) missing.push("ollama");
    }
    if (!missing.length) return;

    // ③ 只弹一次：选什么都要记，免得每次启动都烦人
    // ⚠ 这里原来写 `self.setPref(...)` —— 插件里**没有**这个包装函数（写 pref 用
  //   `Zotero.Prefs.set`），而它又在 try/catch 里，于是静默失败：
  //   后果是「引导只弹一次」的 pref 从来没写进去，缺 MinerU/Ollama 的用户
  //   **每次启动都会被弹一次**。2026-10-05 由新的成员引用检查扫出来。
  try { Zotero.Prefs.set(self.PREFS.optionalGuideDone, true); }
  catch (e) { /* ignore */ }
    if (self.askOptionalGuide(missing) === 0) self.openOptionalGuide(missing[0]);
    return missing;
  },


  /**
   * 弹对话框。`missing` 是缺的组件名数组（"mineru" / "ollama"）。
   * 返回 0 =「打开安装引导」，1 =「以后再说」。
   *
   * 为什么用 `Services.prompt.confirmEx` 而不是自绘窗口：它是**同步**的、
   * 两个按钮的文案都能自定义，而且不需要额外 xhtml。
   */
  askOptionalGuide: function (missing) {
    const names = (missing || []).map(function (m) {
      return m === "mineru" ? "MinerU（让 PDF 解析更准，公式变 LaTeX）"
                            : "Ollama（本地模型，分类/摘要可离线跑）";
    });
    const body = "知识库有两个**可选**组件还没装：\n\n"
      + names.map(function (n) { return "  · " + n; }).join("\n")
      + "\n\n不装完全不影响现有功能：\n"
      + "  · 不装 MinerU：正文仍用 Zotero 缓存 + PyMuPDF 解析；\n"
      + "  · 不装 Ollama：在面板「运行环境 → 模型接入」里改成用 API（DeepSeek 等）即可。\n\n"
      + "面板里那个安装引导会：说明代价 → 体检磁盘/网络 → 后台安装（可取消）→ 自动填好路径。\n\n"
      + "要不要现在打开安装引导？";
    try {
      const ps = Services.prompt;
      const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
        + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
      const win = (Zotero.getMainWindow && Zotero.getMainWindow()) || null;
      return ps.confirmEx(win, "知识库：可选组件（不装也能用）", body, flags,
                          "打开安装引导", "以后再说", null, null, {});
    } catch (e) {
      Zotero.debug("[zotero-kb] 弹可选组件引导失败：" + e);
      return 1;
    }
  },


  /**
   * 打开对应组件的安装引导。`which` = "mineru" | "ollama"。
   *
   * W4（2026-10-14）：原来这里是"拉起 Tk 面板的引导窗口"（起面板进程 + 传 --mineru-guide）。Tk 面板退役后那条路没了，改成**引导用户去新面板的对应页**
   * （新面板第 5 页「PDF 解析」就是 MinerU 的入口；Ollama 在「运行环境」页）。
   * 返回 false 让调用方知道"没自动打开"，但**给的是能照做的指引**，不是死路。
   */
  openOptionalGuide: function (which) {
    var self = ZoteroKB;
    const where = which === "ollama"
      ? "「运行环境」页（本地模型那一栏）"
      : "「PDF 解析」页（MinerU 那一栏）";
    try {
      self.notify("安装引导在管理面板里",
        "打开：工具菜单 →「知识库管理…」→ " + where + "。\n"
        + "（也可以直接双击项目里的 "
        + (which === "ollama" ? "（Ollama 用系统安装包）" : "scripts\\install-mineru.cmd")
        + "）", null, false);
    } catch (e) { /* 提示失败不影响调用方 */ }
    return false;
  },
});

// ===== src/20-kbview.js =====
/**
 * 20-kbview.js —— 条目右侧栏的「知识库」：把 kb/ 里的分级文件在一列里显示出来。
 *
 * ## 2026-10-11 改版（本文件的大变化，看这里就够了）
 *
 * 用户看了 `leike0813/zotero-agents` 的 ACP Chat 之后拍板"仿照他的做"：
 * **我们这一列要能独占条目窗格，而不是像 registerSection 那样挤在分区列最下面**
 * （Zotero 的 itemPane 是一列纵向堆叠，rail 只做 scrollToPane / pin，做不到独占）。
 *
 * 于是：
 *   · **不再用 `ItemPaneManager.registerSection`** —— 自己往
 *     `#zotero-item-pane` 里插一列，并把原生的分区列（`#zotero-item-pane-content`）
 *     暂时 `hidden` 起来，视觉上独占；关闭时逐项还原（先存再改，见 kbviewSaveNative）。
 *   · **列里放的是 XUL `<browser>`**，指向插件自带的 `content/kbview.html`。
 *     正文在那份**普通 HTML 文档**里渲染，终于能正常写 CSS
 *     （overflow-wrap:anywhere / max-width:100% / MathML / 宽表格滚动）。
 *     老实现往 XUL 文档里塞 innerHTML，按 **XML** 解析 —— 一个没闭合的 `<br>`
 *     就让整段内容被静默丢弃（2026-10-05 实测："读了 34414 字、正文一片空白"）。
 *   · 宿主与页面之间用 **JSON 字符串**通信（见 kbviewSend / kbviewAttachBridge）：
 *     宿主→页面直接调 `contentWindow.kbvReceive(json)`；页面→宿主调宿主注入的
 *     `__kbvHostPost(json)`。都用字符串，避开跨 principal 传对象那一堆坑。
 *
 * ⚠ **装/卸必须对称**：Zotero 会复用主窗口，漏 `remove()` 就会出现两份。
 * ⚠ **藏原生列之前先存原状态**（hidden / collapsed / width / splitter state），
 *   关闭时逐项还原 —— 别把用户的分区列弄没了。
 * ⚠ 这三个 id 都是**内部 id**，换 Zotero 版本可能变 → 一律"取不到就降级"：
 *   退回老的 `registerSection` 模式（只读）并如实写状态文件，别让面板整个消失。
 *
 * 数据分工：**宿主取数据**（IOUtils / kbDir / kbLevels / doc-edit 都在这儿），
 * **页面只负责渲染与交互**。页面里不重复实现权限逻辑。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成 bootstrap.js。
 */
Object.assign(ZoteroKB, {

  KBVIEW_ID: "zotero-kb-kbview",
  kbviewPaneID: null,        // 只读降级模式（registerSection）返回的 id
  kbviewHosts: null,         // Map<window, host>，见 kbviewMap

  /**
   * 我们自己的 `resource://` 包名。
   *
   * ⚠ 这一条是**真机实测逼出来的**（2026-10-11，Zotero 10.0.6 / Firefox 140）：
   *   列里那个 `<browser>` 如果直接加载 `rootURI + "content/kbview.html"`
   *   （rootURI 是 `jar:file:///…xpi!/`），会出现两难：
   *     · 带 `maychangeremoteness="true"`（参考实现这么写）→ 浏览器变成**远程进程**，
   *       页面能加载，但 `browser.contentWindow` 是 **null** —— 宿主根本碰不到页面；
   *     · 去掉那个属性 → 留在父进程、`contentWindow` 有了，**但 jar: 地址压根不导航**
   *       （实测 `currentURI` 永远 about:blank）。
   *   Zotero 自己的 reader 是这么干的（`xpcom/reader.js:1992`）：
   *     `createXULElement('browser')` + `type="content"` + src = **`resource://zotero/`**，
   *     然后 `this._iframe.contentWindow` 直接用。也就是说**resource:// 地址才会在
   *     父进程里加载**。所以插件启动时注册一个自己的 resource 包（插件本来就有权
   *     注册包），页面从 `resource://zotero-kb/…` 走。
   */
  KB_RES_KEY: "zotero-kb",

  /**
   * 知识库目录的 resource 包名（**图片走它**）。
   *
   * ⚠ 为什么不用 `file:///`（老做法）：真机实测，`resource://` 的页面加载
   *   `file://` 图片会被**安全策略拦掉** —— `naturalWidth` 永远是 0、
   *   `complete` 永远 false（"图片全都加载不出来"）。改成再注册一个
   *   `resource://zotero-kb-files/` → <知识库目录>，图片全都能加载
   *   （实测 418×293 / complete=true）。
   */
  KB_FILES_RES: "zotero-kb-files",
  _kbvFilesRoot: "",
  _kbvFilesOK: false,

  /** 知识库目录的 resource 前缀（懒注册；kbDir 变了会重注册）。 */
  kbviewFilesURI: function () {
    var self = ZoteroKB;
    try {
      const kb = String(self.kbDir() || "").replace(/[\\/]+$/, "");
      if (!kb) return "";
      if (self._kbvFilesRoot === kb && self._kbvFilesOK) {
        return "resource://" + self.KB_FILES_RES + "/";
      }
      const res = Services.io.getProtocolHandler("resource")
        .QueryInterface(Components.interfaces.nsIResProtocolHandler);
      res.setSubstitution(self.KB_FILES_RES,
                          Services.io.newFileURI(Zotero.File.pathToFile(kb)));
      self._kbvFilesRoot = kb;
      self._kbvFilesOK = true;
      return "resource://" + self.KB_FILES_RES + "/";
    } catch (e) {
      self._kbvFilesOK = false;
      Zotero.debug("[zotero-kb] 注册知识库 resource 包失败：" + e);
      return "";
    }
  },

  /** 注册 `resource://zotero-kb/` → 插件根目录（startup 时调；失败要能看见）。 */
  registerKbResource: function () {
    var self = ZoteroKB;
    try {
      const res = Services.io.getProtocolHandler("resource")
        .QueryInterface(Components.interfaces.nsIResProtocolHandler);
      res.setSubstitution(self.KB_RES_KEY, Services.io.newURI(self.rootURI, null, null));
      self.writeStatusFile({ kbViewResource: "ok: resource://" + self.KB_RES_KEY + "/" });
      return true;
    } catch (e) {
      self.writeStatusFile({ kbViewResource: "FAIL: " + e });
      Zotero.debug("[zotero-kb] 注册 resource 包失败：" + e);
      return false;
    }
  },

  /** 注销（shutdown 时调）—— 留着会让下次启用指向旧包。 */
  unregisterKbResource: function () {
    var self = ZoteroKB;
    try {
      const res = Services.io.getProtocolHandler("resource")
        .QueryInterface(Components.interfaces.nsIResProtocolHandler);
      res.setSubstitution(self.KB_RES_KEY, null);
      res.setSubstitution(self.KB_FILES_RES, null);
      self._kbvFilesRoot = "";
      self._kbvFilesOK = false;
    } catch (e) { /* ignore */ }
  },

  /**
   * XHTML 命名空间。
   *
   * ⚠ 这个常量是**修 bug 用的**（2026-10-05 实测）：Zotero 主窗口是 XUL 文档，
   *   `doc.createElement("div")` 建出来的元素在 **XUL 命名空间**里 —— 给它设
   *   `innerHTML` 时按 **XML** 解析：片段里只要有一个没闭合的标签（比如 `<br>`），
   *   **整段内容会被丢掉**，而且不报错（用户看到的正是"读了 34414 字、正文一片空白"）。
   * 现在自建列走 `<browser>` + 普通 HTML 文档（那条路不存在这个坑），
   * 这个常量与 kbviewSetHtml 只留给**降级模式**用。
   */
  KB_XHTML: "http://www.w3.org/1999/xhtml",

  /** 建一个 XHTML 命名空间的元素（降级模式里装 HTML 的容器要用它）。 */
  kbviewEl: function (doc, tag) {
    return doc.createElementNS(ZoteroKB.KB_XHTML, tag);
  },

  /**
   * 把一段 HTML **片段**灌进容器（对两种解析器都稳）。**降级模式专用。**
   * ⚠ 绝不能写成 `while (root.firstChild) { el.appendChild(doc.importNode(
   *   root.firstChild, true)) }` —— importNode 返回**副本**、不移除原节点，
   *   于是 firstChild 永远为真 → 死循环 → 内存吃爆 → Zotero 卡死崩溃（实测过）。
   *   一次 import 整棵 wrapper 最简也最不可能出错。
   */
  kbviewSetHtml: function (doc, el, text) {
    try {
      if (typeof DOMParser === "function") {
        const parsed = new DOMParser().parseFromString(
          "<div id='kbroot'>" + String(text == null ? "" : text) + "</div>",
          "text/html");
        const root = parsed.getElementById("kbroot");
        if (root) {
          el.textContent = "";
          el.appendChild(doc.importNode(root, true));
          return true;
        }
      }
    } catch (e) {
      try { el.textContent = "（渲染出错：" + e + "）"; } catch (e2) { /* ignore */ }
      return false;
    }
    try {
      el.innerHTML = String(text == null ? "" : text);
      return true;
    } catch (e) {
      try { el.textContent = "（渲染出错：" + e + "）"; } catch (e2) { /* ignore */ }
      return false;
    }
  },


  // ================================================================ 基础设置

  /** 界面文案（按钮 tooltip）：优先走 ftl，取不到就用中文兜底。 */
  kbviewLabel: function () {
    try {
      const msg = Zotero.ftl && Zotero.ftl.formatValueSync
        && Zotero.ftl.formatValueSync("zotero-kb-kbview-title");
      if (msg && String(msg).trim()) return String(msg);
    } catch (e) { /* 落回中文 */ }
    return "知识库预览";
  },

  /** 只有普通条目（有 pdf/元数据的那些）才显示这一列。 */
  kbviewEnabled: function (item, tabType) {
    if (!item) return false;
    if (tabType === "reader-unloaded") return false;
    try {
      return typeof item.isRegularItem === "function" ? !!item.isRegularItem() : true;
    } catch (e) {
      return false;
    }
  },

  /** 用户选过的级别（pref），默认「分节纲要」—— 它就是给"对着原文读"用的那一层。 */
  kbviewLevel: function () {
    var self = ZoteroKB;
    try {
      const v = String(self.getPref(self.PREFS.kbviewLevel, "") || "").trim();
      if (v) return v;
    } catch (e) { /* ignore */ }
    return "outline";
  },

  /** 字号（px，只作用于这一列）。12~20，默认 13。 */
  kbviewFont: function () {
    var self = ZoteroKB;
    try {
      const v = parseInt(self.getPref(self.PREFS.kbviewFont, 13), 10);
      if (v >= 10 && v <= 24) return v;
    } catch (e) { /* ignore */ }
    return 13;
  },

  setKbviewFont: function (px) {
    var self = ZoteroKB;
    try {
      const v = parseInt(px, 10);
      // ⚠ 写 pref 用 Zotero.Prefs.set：插件里**没有** setPref 这个包装
      if (v >= 10 && v <= 24) Zotero.Prefs.set(self.PREFS.kbviewFont, v);
    } catch (e) { /* ignore */ }
  },

  setKbviewLevel: function (id) {
    var self = ZoteroKB;
    try { Zotero.Prefs.set(self.PREFS.kbviewLevel, String(id || "")); }
    catch (e) { /* ignore */ }
  },


  // ⚠ 这里原来还有一套「窄 / 标准 / 宽」档位（kbviewWidth / setKbviewWidth /
  //   kbviewWidthPx），实现是**改原生日志窗格的 width**。用户 2026-10-11 明确不要：
  //   "覆盖在原侧边栏上，同宽窄能拉动，我也想要一样的" ——
  //   那就是说**不要碰窗格宽度**：我们只是借这条原生窗格显示，宽度归原生的
  //   splitter（#zotero-items-splitter / #zotero-context-splitter）管，
  //   用户拖它，我们的列（flex:1）跟着变。所以那套档位整段删掉了，别再加回来。
  // ================================================================ 分级与文件

  /** 某个级别的 md 在磁盘上的绝对路径（与 Python 侧 kbviews.level_path 同一套拼接）。 */
  kbviewPath: function (levelId, key) {
    const lv = (ZoteroKB.kbLevels(key) || []).find((x) => x.id === levelId);
    return lv ? lv.path : "";
  },

  /**
   * 决定"这次读哪一级"：先看用户选的那一级，文件不在就依次退到
   * 分节纲要 → 摘要与要点 → 完整档案，都读不到就返回 `{levelId:""}`。
   * `existsFn(id) => bool` 由调用方注入 —— 逻辑是纯的，所以能单测。
   */
  kbviewOrder: function (prefer) {
    const order = [];
    for (const id of [prefer, "outline", "tldr", "card"]) {
      if (id && order.indexOf(id) < 0) order.push(id);
    }
    return order;
  },

  kbviewPickLevel: function (prefer, existsFn) {
    const order = ZoteroKB.kbviewOrder(prefer);
    for (const id of order) {
      let ok = false;
      try { ok = !!existsFn(id); } catch (e) { ok = false; }
      if (ok) return { levelId: id, fellBack: id !== prefer };
    }
    return { levelId: "", fellBack: false };
  },


  // ================================================================ 自建列：宿主状态

  /**
   * 每个主窗口一份宿主状态。`Map<window, host>`：
   *   host = { win, lib, read, timer, timerArmed, lastKey, onMessage }
   *   dock = { kind, roots, button, container, browser, saved, open, ready }
   * 用 Map 而不是挂在 window 上：卸载时能枚举干净，也不往 Zotero 的对象上加属性。
   */
  kbviewMap: function () {
    var self = ZoteroKB;
    if (!self.kbviewHosts) {
      try { self.kbviewHosts = new Map(); } catch (e) { self.kbviewHosts = null; }
    }
    return self.kbviewHosts;
  },

  kbviewHost: function (win, create) {
    var self = ZoteroKB;
    const m = self.kbviewMap();
    if (!m || !win) return null;
    let h = m.get(win) || null;
    if (!h && create) {
      h = { win: win, lib: null, read: null, timer: null, timerArmed: false,
            lastKey: "", lastPage: 0, onMessage: null };
      m.set(win, h);
    }
    return h;
  },

  /** 一列的 DOM 落点。取不到（Zotero 内部结构变了）返回 null → 上层降级。 */
  kbviewRoots: function (win, kind) {
    const doc = win && win.document;
    if (!doc) return null;
    const byId = (id) => { try { return doc.getElementById(id); } catch (e) { return null; } };
    if (kind === "reader") {
      const cp = byId("zotero-context-pane");
      const inner = byId("zotero-context-pane-inner");
      const sn = byId("zotero-context-pane-sidenav");
      if (!cp || !inner || !sn) return null;
      const parent = inner.parentElement;
      if (!parent) return null;
      return { parent: parent, anchor: inner, sidenav: sn, deck: inner, pane: cp };
    }
    const pane = byId("zotero-item-pane");
    const deck = byId("zotero-item-pane-content");
    const sidenav = byId("zotero-view-item-sidenav");
    if (!pane || !deck || !sidenav) return null;
    // 参考实现是 insertBefore(container, sidenav) —— 那只在"sidenav 是 pane 的直接
    // 子元素"时成立。这里先验证这条前提，不成立就降级（宁可不装，也不乱插）。
    if (sidenav.parentElement !== pane) return null;
    return { parent: pane, anchor: sidenav, sidenav: sidenav, deck: deck, pane: pane };
  },

  /** 列里那个页面的地址（走我们自己注册的 resource 包，理由见 KB_RES_KEY）。 */
  kbviewPageURL: function () {
    var self = ZoteroKB;
    const base = "resource://" + self.KB_RES_KEY + "/";
    return base + "content/kbview.html";
  },


  // ================================================================ 自建列：装 / 卸

  /** 装一个窗口里的一列（幂等）。装不上返回 false，由 kbviewMount 决定是否降级。 */
  kbviewMountPane: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, true);
    if (!host) return false;
    const slot = kind === "reader" ? "read" : "lib";
    if (host[slot]) return true;
    const roots = self.kbviewRoots(win, kind);
    if (!roots) return false;
    const url = self.kbviewPageURL();
    if (!url) return false;
    const doc = win.document;
    const btn = self.kbviewBuildButton(doc, win, kind);
    const box = self.kbviewBuildContainer(doc, win, kind);
    const br = self.kbviewBuildBrowser(doc, win, kind, url);
    box.appendChild(br);
    try {
      roots.sidenav.appendChild(btn);                 // 天然在最下面
      roots.parent.insertBefore(box, roots.anchor);    // 排在原生分区列之前
    } catch (e) {
      // 插一半失败：把已经插进去的清掉，别留残骸
      try { btn.remove(); } catch (e2) { /* ignore */ }
      try { box.remove(); } catch (e3) { /* ignore */ }
      throw e;
    }
    host[slot] = { kind: kind, win: win, roots: roots, button: btn, container: box,
                   browser: br, saved: null, open: false, ready: false };
    self.writeStatusFile({ kbViewColumn: "ok: 自建列已装（" + kind + "）" });
    Zotero.debug("[zotero-kb] 自建列已装：" + kind);
    return true;
  },

  /** 装一个窗口的全部 UI（主入口；onMainWindowLoad 与 startup 各调一次）。 */
  kbviewMount: function (win) {
    var self = ZoteroKB;
    if (!win || !win.document) return false;
    const host = self.kbviewHost(win, true);
    if (!host) return false;
    let ok = false;
    try { ok = self.kbviewMountPane(win, "library"); }
    catch (e) { Zotero.debug("[zotero-kb] 装 library 列失败：" + e); ok = false; }
    if (!ok) {
      // 取不到落点 → 退回 registerSection（只读），并如实写状态文件。
      self.kbviewDegrade("找不到 #zotero-item-pane / #zotero-view-item-sidenav / "
                         + "#zotero-item-pane-content（Zotero 内部结构可能变了）");
      return false;
    }
    // 阅读器那一列（#zotero-context-pane）是**尽力而为**：装不上不影响主功能，
    // 也**不**因此降级（否则会出现"rail 上一个入口、分区列里还多一块"）。
    try {
      self.kbviewMountPane(win, "reader");
    } catch (e2) {
      Zotero.debug("[zotero-kb] 阅读器那列没装成（不影响条目库）：" + e2);
    }
    if (!host.onMessage) self.kbviewOnWindowMessage(win);
    // 阅读器侧的两个全局东西（幂等；放在这里 = 有主窗口就一定装上）：
    //   · Notifier 的 pageChange —— 翻页时立刻把右边滚到同一页（见 kbviewOnNotify）
    //   · 阅读器工具栏的「对照」按钮（见 kbviewRegisterReaderToolbar）
    try { self.kbviewRegisterNotifier(); }
    catch (e3) { Zotero.debug("[zotero-kb] 注册侧栏 Notifier 失败：" + e3); }
    try { self.kbviewRegisterReaderToolbar(); }
    catch (e4) { Zotero.debug("[zotero-kb] 注册阅读器工具栏按钮失败：" + e4); }
    // 阅读器右键菜单的「知识库：修正公式」（22-formula.js）。同样幂等，
    // 失败只写状态文件，不影响别的功能。
    try { self.kbFormulaInit(); }
    catch (e4b) { Zotero.debug("[zotero-kb] 注册阅读器右键菜单失败：" + e4b); }
    // ⚠ 会话恢复出来的阅读器**早于**我们注册 renderToolbar，收不到那个一次性
    //   事件 —— 所以注册完再"补注入"一次（见 kbviewInjectToolbarButton）。
    try { self.kbviewLateDispatch(function () { self.kbviewEnsureReaderToolbar(); }, 1500); }
    catch (e5) { /* ignore */ }
    // 那份页面**不在启动期加载**（实测 863ms，见 kbviewEnsurePage 的说明）：
    // 等窗口起来、空闲下来再挂。用户要是此刻就点开，kbviewOpen 里会立刻挂上。
    try {
      self.kbviewLateDispatch(function () { self.kbviewEnsurePage(win, "library"); }, 2500);
      self.kbviewLateDispatch(function () { self.kbviewEnsurePage(win, "reader"); }, 3500);
    } catch (e6) { /* ignore */ }
    return true;
  },

  /** 卸一个窗口的全部 UI（对称：按钮、列、监听、定时器都要清）。 */
  kbviewUnmount: function (win) {
    var self = ZoteroKB;
    if (!win) return;
    const host = self.kbviewHost(win, false);
    self.kbviewStopTimer(win);
    if (host) {
      const slots = ["lib", "read"];
      for (const s of slots) {
        const dock = host[s];
        if (!dock) continue;
        try { if (dock.open) self.kbviewRestoreNative(win, dock); }
        catch (e) { Zotero.debug("[zotero-kb] 还原原生列失败：" + e); }
        try { if (dock.button && dock.button.remove) dock.button.remove(); } catch (e2) { /* ignore */ }
        try { if (dock.container && dock.container.remove) dock.container.remove(); } catch (e3) { /* ignore */ }
        dock.open = false;
        host[s] = null;
      }
      if (host.onMessage && win.removeEventListener) {
        try { win.removeEventListener("message", host.onMessage, true); } catch (e4) { /* ignore */ }
      }
      host.onMessage = null;
    }
    const m = self.kbviewMap();
    if (m) { try { m.delete(win); } catch (e5) { /* ignore */ } }
    Zotero.debug("[zotero-kb] 自建列已卸");
  },

  /** 卸载插件时把所有窗口的列都清掉。 */
  kbviewUnmountAll: function () {
    var self = ZoteroKB;
    const m = self.kbviewMap();
    if (!m) return;
    const wins = [];
    try { m.forEach(function (v, k) { wins.push(k); }); } catch (e) { return; }
    for (const w of wins) {
      try { self.kbviewUnmount(w); } catch (e2) { /* ignore */ }
    }
    try { m.clear(); } catch (e3) { /* ignore */ }
  },


  // ================================================================ 自建列：DOM 构造

  /** rail 上那个按钮：class="btn" 让它继承 Zotero 自己的 rail 样式（28×28 / 悬停）。 */
  kbviewBuildButton: function (doc, win, kind) {
    var self = ZoteroKB;
    let btn = null;
    try { btn = doc.createXULElement ? doc.createXULElement("toolbarbutton") : null; }
    catch (e) { btn = null; }
    if (!btn) btn = doc.createElement("toolbarbutton");
    btn.setAttribute("id", "zotero-kb-kbview-entry-" + kind);
    // ⚠ 绝不加 data-pane：ItemPaneSidenav#render() 会遍历 .btn[data-pane] 并按
    //   "已注册的分区"隐藏/显示 —— 我们的按钮没注册过，会被它连父元素一起 hidden
    //   （父元素就是 rail 本身，等于把整条 rail 藏掉）。
    btn.setAttribute("class", "btn zotero-kb-kbview-entry");
    btn.setAttribute("custom", "true");
    btn.setAttribute("tooltiptext", self.kbviewLabel());
    btn.setAttribute("aria-label", self.kbviewLabel());
    btn.setAttribute("aria-pressed", "false");
    btn.setAttribute("tabindex", "0");
    btn.setAttribute("role", "button");
    const icon = self.rootURI == null ? "" : String(self.rootURI) + "sidenav-icon.svg";
    try {
      // 图标用 context-fill（跟随主题）；rail 的 .btn 已经设了
      // background-repeat/position 与 -moz-context-properties，这里只补图与填充色。
      if (icon) btn.style.setProperty("background-image", 'url("' + icon + '")');
      btn.style.setProperty("-moz-context-properties", "fill,stroke");
      btn.style.setProperty("fill", "currentColor");
    } catch (e2) { /* 图标是装饰，失败不影响功能 */ }
    btn.addEventListener("command", function () {
      try { ZoteroKB.kbviewToggle(win, kind); }
      catch (e3) { Zotero.debug("[zotero-kb] 切换知识库侧栏失败：" + e3); }
    });
    return btn;
  },

  /** 我们那一列的容器（默认藏起来，打开时才 display:flex）。 */
  kbviewBuildContainer: function (doc, win, kind) {
    let box = null;
    try { box = doc.createXULElement ? doc.createXULElement("vbox") : null; }
    catch (e) { box = null; }
    if (!box) box = doc.createElement("vbox");
    box.setAttribute("id", "zotero-kb-kbview-column-" + kind);
    box.setAttribute("class", "zotero-kb-kbview-column");
    box.setAttribute("flex", "1");
    box.setAttribute("style",
      "display:none; flex:1 1 auto; min-width:0; min-height:0; height:100%;"
      + " overflow:hidden; flex-direction:column; background:transparent;");
    return box;
  },

  /**
   * 列里的内容区：XUL `<browser type="content">`（参考实现也是这么做的）。
   * 为什么不用 `<iframe>`：browser 是 Zotero 里加载自带页面的既定做法
   * （Zotero 自己的 reader 就是 `createXULElement('browser')` + resource:// 地址），
   * 真机实测 `<iframe>`（XUL 与 HTML 两种）在这个 XUL 窗口里**根本不导航**
   * （`contentDocument` 永远停在 about:blank）。
   */
  kbviewBuildBrowser: function (doc, win, kind, url) {
    var self = ZoteroKB;
    let el = null;
    try { el = doc.createXULElement ? doc.createXULElement("browser") : null; }
    catch (e) { el = null; }
    if (!el) { try { el = doc.createElement("browser"); } catch (e2) { el = null; } }
    if (!el) return null;
    el.setAttribute("id", "zotero-kb-kbview-frame-" + kind);
    el.setAttribute("class", "zotero-kb-kbview-frame");
    el.setAttribute("disableglobalhistory", "true");
    // ⚠ 这里**故意没有** `maychangeremoteness="true"`（参考实现有）：
    //   真机实测加了它这个 browser 会变成**远程进程**，contentWindow 变 null、
    //   宿主与页面彻底失联。去掉它 + src 走 resource:// 才有 contentWindow。
    //   详见 KB_RES_KEY 那段说明。
    el.setAttribute("type", "content");
    el.setAttribute("flex", "1");
    // ⚠ 2026-10-13：**这里不设 src**，只把它记下来 —— 那份页面实测要 863ms
    //   （kbview.html + markdown-it + katex + 21 个 woff2 字体），而用户报的
    //   "启动时插件加载有点慢"就是这个：建 <browser> 时立刻设 src = 把这 863ms
    //   摊进了 Zotero 的启动期。改由 kbviewEnsurePage() 在**窗口空闲后**再挂，
    //   或者用户真点开时立刻挂（见 kbviewOpen / kbviewMount 里的调度）。
    el.setAttribute("data-kbview-src", url);
    try {
      el.style.setProperty("width", "100%");
      el.style.setProperty("height", "100%");
      el.style.setProperty("flex", "1 1 auto");
      el.style.setProperty("min-height", "0");
      el.style.setProperty("min-width", "0");
      el.style.setProperty("display", "block");
      el.style.setProperty("border", "0");
    } catch (e3) { /* ignore */ }
    el.addEventListener("load", function () {
      // 计时：从**挂 src 那一刻**到页面 load（含 vendor 与字体）。
      // 分段写，任何一段读不到都不影响其它信息落盘（第一版把整段包一个 try，
      // contentWindow 还没就绪时于是一条都不写，白装了仪表）。
      const t0 = el.__kbvSrcAt || Date.now();
      try {
        self.writeStatusFile({ kbviewPageLoadMs: Date.now() - t0 });
      } catch (eTiming) { /* ignore */ }
      try {
        const cw = el.contentWindow;
        const perf = cw && cw.performance;
        if (perf) {
          const nav = perf.getEntriesByType("navigation")[0];
          const rs = perf.getEntriesByType("resource");
          let bytes = 0, resMs = 0;
          for (const r of rs) { bytes += (r.transferSize || 0); resMs += r.duration; }
          self.writeStatusFile({
            kbviewPageDomComplete: nav ? Math.round(nav.domComplete) : null,
            kbviewPageResources: rs.length,
            kbviewPageResourcesMs: Math.round(resMs),
            kbviewPageBytes: bytes,
          });
        }
      } catch (ePerf) { /* 拿不到 performance 就算了，上面那行耗时已经够用 */ }
      try { self.kbviewAttachBridge(win, kind); }
      catch (e4) { Zotero.debug("[zotero-kb] 侧栏页面桥接失败：" + e4); }
    });
    return el;
  },


  // ================================================================ 自建列：开 / 关

  /** 把原生列的原状态**先存下来**（关闭时逐项还原，这是"自插 DOM"的第一大坑）。 */
  kbviewSaveNative: function (win, dock) {
    if (!dock || dock.saved) return;
    const r = dock.roots;
    // ⚠ **不存窗格宽度**：宽度归原生的 splitter（用户自己拖），我们从头到尾
    //   没碰过它 —— 存了再还原会把用户在我们打开期间拖出来的宽度顶回去。
    const saved = {
      deckHidden: null, deckCollapsed: null, paneCollapsed: null,
      sidenavHidden: null, splitterState: null, contextCollapsed: null,
    };
    try {
      if (r.deck) {
        saved.deckHidden = r.deck.getAttribute("hidden");
        saved.deckCollapsed = r.deck.getAttribute("collapsed");
      }
      if (r.pane) saved.paneCollapsed = r.pane.getAttribute("collapsed");
      if (r.sidenav) saved.sidenavHidden = r.sidenav.getAttribute("hidden");
      const sp = win.document.getElementById(dock.kind === "reader"
        ? "zotero-context-splitter" : "zotero-items-splitter");
      if (sp) saved.splitterState = sp.getAttribute("state");
      if (dock.kind === "reader" && win.ZoteroContextPane) {
        saved.contextCollapsed = !!win.ZoteroContextPane.collapsed;
      }
    } catch (e2) { /* 存不下就尽量少动原生 DOM */ }
    dock.saved = saved;
  },

  /** 逐项还原（不是"删掉就算了"）。 */
  kbviewRestoreNative: function (win, dock) {
    if (!dock || !dock.saved) return;
    const s = dock.saved;
    const r = dock.roots;
    const put = function (el, name, val) {
      if (!el) return;
      try {
        if (val == null) el.removeAttribute(name);
        else el.setAttribute(name, val);
      } catch (e) { /* ignore */ }
    };
    put(r.deck, "hidden", s.deckHidden);
    put(r.deck, "collapsed", s.deckCollapsed);
    put(r.pane, "collapsed", s.paneCollapsed);
    put(r.sidenav, "hidden", s.sidenavHidden);
    try {
      const sp = win.document.getElementById(dock.kind === "reader"
        ? "zotero-context-splitter" : "zotero-items-splitter");
      if (sp && s.splitterState != null) sp.setAttribute("state", s.splitterState);
    } catch (e2) { /* ignore */ }
    try {
      if (dock.kind === "reader" && win.ZoteroContextPane
          && s.contextCollapsed != null) {
        win.ZoteroContextPane.collapsed = !!s.contextCollapsed;
      }
    } catch (e3) { /* ignore */ }
    dock.saved = null;
  },

  /** 打开前先把原生窗格撑开（收起状态下 rail 自己也看不见，谈不上点按钮）。 */
  kbviewExpandPane: function (win, dock) {
    const r = dock.roots;
    try {
      if (dock.kind === "reader") {
        if (win.ZoteroContextPane) win.ZoteroContextPane.collapsed = false;
        if (r.pane && r.pane.getAttribute("collapsed") === "true") {
          r.pane.setAttribute("collapsed", "false");
        }
      } else if (r.pane && r.pane.getAttribute("collapsed") === "true") {
        const sp = win.document.getElementById("zotero-items-splitter");
        if (sp) sp.setAttribute("state", "open");
        r.pane.setAttribute("collapsed", "false");
      }
      if (r.sidenav && r.sidenav.getAttribute("hidden") != null) {
        r.sidenav.removeAttribute("hidden");
      }
    } catch (e) { /* ignore */ }
  },

  kbviewIsOpen: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const dock = kind === "reader" ? host.read : host.lib;
    return !!(dock && dock.open);
  },

  /** 读一列里那个 <browser>（没有就 null）。 */
  kbviewBrowserOf: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    const slot = kind === "reader" ? "read" : "lib";
    const dock = host && host[slot];
    return (dock && dock.browser) || null;
  },

  /**
   * 把那一列的页面**挂上（首次调用时才真加载）**。幂等。
   *
   * 为什么要有这一步（2026-10-13，用户报"启动时插件加载有点慢"）：
   * 实测插件自己的同步启动只占 **21ms**，而那份页面（kbview.html +
   * markdown-it 121KB + katex 269KB + 21 个 woff2 字体）要 **863ms** ——
   * 原来在建 <browser> 时就 `setAttribute("src", url)`，于是这 863ms 被摊进了
   * Zotero 的启动期（新建 docshell + 解析 40 万字符 vendor JS 都在抢 CPU/IO）。
   * 现在只把 url 记在 `data-kbview-src` 上，由这里按需挂：
   *   · 窗口起来后空闲 ~2.5s（kbviewMount 里调度）—— 用户还没看的时候后台补上；
   *   · 用户真去点「知识库」时立刻挂（kbviewOpen 里调）—— 不让人干等。
   * 打开流程本来就有 400ms 轮询兜底（kbviewStartTimer），晚挂不影响推送。
   */
  kbviewEnsurePage: function (win, kind) {
    var self = ZoteroKB;
    const br = self.kbviewBrowserOf(win, kind);
    if (!br) return false;
    try {
      if (br.getAttribute("src")) return true;          // 已经挂过
      const url = br.getAttribute("data-kbview-src");
      if (!url) return false;
      br.__kbvSrcAt = Date.now();                       // 给 load 里的计时用
      br.setAttribute("src", url);
      return true;
    } catch (e) {
      Zotero.debug("[zotero-kb] 挂侧栏页面失败：" + e);
      return false;
    }
  },

  kbviewOpen: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, true);
    if (!host) return false;
    // 两列都占地方，**不能同时开**（派发包 §一）。kbviewToggle 已经处理了
    // 点按钮那条路；这里再兜一次，防止别的代码路径直接调 kbviewOpen。
    const other = kind === "reader" ? "library" : "reader";
    if (self.kbviewIsOpen(win, other)) self.kbviewClose(win, other);
    const slot = kind === "reader" ? "read" : "lib";
    if (!host[slot]) {
      let ok = false;
      try { ok = self.kbviewMountPane(win, kind); } catch (e) { ok = false; }
      if (!ok) {
        if (kind === "reader") {
          self.writeStatusFile({ kbViewColumn: "reader 列不可用（不影响条目库那一列）" });
        } else {
          self.kbviewDegrade("装不上条目库那一列");
        }
        return false;
      }
    }
    const dock = host[slot];
    if (!dock) return false;
    if (dock.open) return true;
    self.kbviewSaveNative(win, dock);
    self.kbviewExpandPane(win, dock);
    try { dock.container.style.display = "flex"; } catch (e2) { /* ignore */ }
    // 把原生的分区列让开 —— "独占一页"就是靠这一句
    try { dock.roots.deck.setAttribute("hidden", "true"); } catch (e3) { /* ignore */ }
    try {
      dock.button.setAttribute("aria-pressed", "true");
      dock.button.setAttribute("data-kbview-open", "true");
    } catch (e4) { /* ignore */ }
    dock.open = true;
    host.lastKey = "?!reset";          // 强制下一拍重推内容
    // 用户真点开了：**立刻**把页面挂上（还没挂的话），别让人对着空白干等
    self.kbviewEnsurePage(win, kind);
    self.kbviewStartTimer(win);
    // 立刻试一次挂桥（页面可能已经就绪），失败就交给上面的轮询
    try { self.kbviewAttachBridge(win, kind); } catch (e5) { /* 轮询会再试 */ }
    self.kbviewPush(win, kind, null);
    self.writeStatusFile({ kbViewColumn: "open: " + kind });
    return true;
  },

  kbviewClose: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const slot = kind === "reader" ? "read" : "lib";
    const dock = host[slot];
    if (!dock || !dock.open) return false;
    dock.open = false;
    try { dock.container.style.display = "none"; } catch (e) { /* ignore */ }
    self.kbviewRestoreNative(win, dock);
    try {
      dock.button.setAttribute("aria-pressed", "false");
      dock.button.removeAttribute("data-kbview-open");
    } catch (e2) { /* ignore */ }
    if (!self.kbviewIsOpen(win, "library") && !self.kbviewIsOpen(win, "reader")) {
      self.kbviewStopTimer(win);
    }
    self.writeStatusFile({ kbViewColumn: "closed: " + kind });
    return true;
  },

  /** rail 按钮 / 开关的统一入口：开就关、关就开（同一时刻只开一列）。 */
  kbviewToggle: function (win, kind) {
    var self = ZoteroKB;
    const other = kind === "reader" ? "library" : "reader";
    if (self.kbviewIsOpen(win, kind)) return self.kbviewClose(win, kind);
    if (self.kbviewIsOpen(win, other)) self.kbviewClose(win, other);
    return self.kbviewOpen(win, kind);
  },


  // ================================================================ 自建列：与页面通信

  /**
   * 把回调挂到页面窗口上（页面用 window.__kbvHostPost(jsonText) 回来）。
   *
   * ⚠ 跨 principal 的两个要点：
   *   ① 挂到 **wrappedJSObject** 上，页面才看得见（挂在 Xray wrapper 上是给自己看的）；
   *   ② 只传 **JSON 字符串**，不传对象 —— 传对象要过 Xray，是最容易"权限被拒"的一步。
   */
  kbviewAttachBridge: function (win, kind) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const dock = kind === "reader" ? host.read : host.lib;
    if (!dock || !dock.browser) return false;
    if (dock.ready) return true;
    const api = function (jsonText) {
      try { ZoteroKB.kbviewOnHostCall(win, kind, jsonText); }
      catch (e) { Zotero.debug("[zotero-kb] 侧栏回调出错：" + e); }
    };
    let n = 0;
    try {
      const w = dock.browser.contentWindow;
      if (w) {
        try { w.__kbvHostPost = api; n++; } catch (e) { /* Xray wrapper 上挂了没用 */ }
        try {
          if (w.wrappedJSObject) { w.wrappedJSObject.__kbvHostPost = api; n++; }
        } catch (e2) { /* ignore */ }
      }
    } catch (e3) { /* 页面还没就绪 */ }
    dock.ready = n > 0;
    if (dock.open) self.kbviewPush(win, kind, null);
    return n > 0;
  },

  /** 把一条消息送进页面（优先直接调，页面里也挂了 postMessage 兜底）。 */
  kbviewSend: function (dock, msg) {
    if (!dock || !dock.browser) return false;
    const full = Object.assign({ __kbv: true }, msg);
    let text = "";
    try { text = JSON.stringify(full); } catch (e) { return false; }
    const wins = [];
    try {
      const w = dock.browser.contentWindow;
      if (w) {
        // ⚠ **wrappedJSObject 优先**：页面是 resource:// 的特权文档，直接读
        //   `contentWindow.kbvReceive` 会被 Xray 挡住（typeof 一直是 "undefined"），
        //   真身只能从 wrappedJSObject 拿 —— 真机实测（2026-10-11）。
        try { if (w.wrappedJSObject) wins.push(w.wrappedJSObject); } catch (e2) { /* ignore */ }
        wins.push(w);
      }
    } catch (e3) { /* ignore */ }
    for (const w of wins) {
      try {
        if (typeof w.kbvReceive === "function") { w.kbvReceive(text); return true; }
      } catch (e4) { /* 试下一个 */ }
    }
    try {
      if (wins.length && wins[0].postMessage) { wins[0].postMessage(full, "*"); return true; }
    } catch (e5) { /* ignore */ }
    return false;
  },

  /** 顺手也听一下主窗口的 message 事件（页面里那条 postMessage 兜底路能用上）。 */
  kbviewOnWindowMessage: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host || host.onMessage) return;
    host.onMessage = function (event) {
      try {
        const d = event && event.data;
        if (!d || typeof d !== "object" || d.__kbv !== true) return;
        const lib = host.lib;
        const rd = host.read;
        let kind = "";
        if (lib && lib.browser && event.source === lib.browser.contentWindow) kind = "library";
        else if (rd && rd.browser && event.source === rd.browser.contentWindow) kind = "reader";
        if (!kind) return;
        self.kbviewOnHostCall(win, kind, JSON.stringify(d));
      } catch (e) { /* ignore */ }
    };
    try { win.addEventListener("message", host.onMessage, true); }
    catch (e2) { Zotero.debug("[zotero-kb] 挂 message 监听失败：" + e2); }
  },

  /** 页面发回来的一条消息。 */
  kbviewOnHostCall: function (win, kind, jsonText) {
    var self = ZoteroKB;
    let msg = null;
    try { msg = JSON.parse(String(jsonText)); } catch (e) { msg = null; }
    if (!msg || typeof msg.type !== "string") return;
    const t = msg.type;
    if (t === "close") { self.kbviewClose(win, kind); return; }
    if (t === "reload") { self.kbviewPush(win, kind, null); return; }
    if (t === "selectLevel") {
      self.setKbviewLevel(msg.level);
      self.kbviewPush(win, kind, msg.level);
      return;
    }
    if (t === "setFont") {
      self.setKbviewFont(msg.font);
      self.kbviewPush(win, kind, null);
      return;
    }
    if (t === "openPath") {
      try { self.openKbPath(msg.path, "知识库文件"); }
      catch (e) { Zotero.debug("[zotero-kb] 打开文件失败：" + e); }
      return;
    }
    if (t === "log") {
      Zotero.debug("[zotero-kb] 侧栏页面：" + msg.message);
      return;
    }
    if (t === "exportDraft") { self.kbviewExportDraft(win, kind); return; }
    if (t === "savePage") { self.kbviewSavePage(win, kind, msg.page, msg.text); return; }
    // 按块编辑：页面把每块的文本交回来，由**宿主**用它原来那份 parts 拼回整页
    // （拼法是纯函数 kbviewJoinBlocks，桩测试直接测；页面不重复实现一遍）。
    if (t === "saveBlocks") {
      let text = "";
      try { text = self.kbviewJoinBlocks(msg.parts); } catch (e) { text = ""; }
      if (!text) {
        self.kbviewHint(win, kind, "拼不回整页正文（parts 不完整），没有保存。");
        return;
      }
      self.kbviewSavePage(win, kind, msg.page, text);
      return;
    }
    if (t === "openImage") { self.kbviewOpenImage(win, kind, msg.rel); return; }
    if (t === "restorePage") { self.kbviewSavePage(win, kind, msg.page, null); return; }
    // 公式纠错（22-formula.js）：页面只负责界面，请求与写回都在宿主这一侧。
    if (t === "formulaApply") { self.kbFormulaApply(win, kind, msg); return; }
    if (t === "formulaMathType") { self.kbFormulaMathType(win, kind, msg); return; }
  },

  /** 给某个窗口的一列送一句提示（只更新状态行，不重画正文）。 */
  kbviewHint: function (win, kind, text) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    const dock = kind === "reader" ? host.read : host.lib;
    if (dock) self.kbviewSend(dock, { type: "hint", text: String(text == null ? "" : text) });
  },


  // ================================================================ 自建列：内容

  /**
   * 当前该显示哪一篇。
   *
   * 原来这一步是 registerSection 的 onItemChange 回调给的；自建列没有这个回调，
   * 所以自己取：阅读器页签取当前附件 → 用它的**父条目**（知识库按父条目的 key 建），
   * 条目库页签取列表里唯一选中的那一条。
   *
   * ⚠ 取法必须和 Zotero 自己的 item-details 一致（它也是 parentID ? parent : item，
   *   见 elements/contextPane.js 的 _addItemContext），否则会出现"选区在附件上、
   *   我们查的是附件 key，永远没有知识库"。
   */
  kbviewCurrentItem: function (win) {
    if (!win) return null;
    let item = null;
    try {
      const tabs = win.Zotero_Tabs;
      const selType = tabs ? String(tabs.selectedType || "") : "";
      if (selType === "reader" && Zotero.Reader && Zotero.Reader.getByTabID) {
        let reader = null;
        try { reader = Zotero.Reader.getByTabID(tabs.selectedID); } catch (e) { reader = null; }
        const id = reader && reader.itemID;
        if (id) item = Zotero.Items.get(id);
      }
    } catch (e2) { item = null; }
    if (!item) {
      try {
        const pane = win.ZoteroPane;
        const sel = pane && pane.getSelectedItems ? pane.getSelectedItems() : null;
        if (sel && sel.length === 1) item = sel[0];
      } catch (e3) { item = null; }
    }
    if (!item) return null;
    try {
      if (item.parentID) {
        const parent = Zotero.Items.get(item.parentID);
        if (parent) item = parent;
      }
    } catch (e4) { /* 用原条目 */ }
    return item;
  },

  /**
   * 组装给页面的一份内容（宿主把该读的文件都读好，页面只管画）。
   * 返回纯数据（可 JSON 化）。
   *
   * ⚠ 2026-10-11 起**默认不再读 `.html` 孪生**：正文与公式都在页面里用
   *   markdown-it + KaTeX 渲染（配方与理由见 content/kbview.js 文件头）。
   *   只有**降级模式**（退回 ItemPaneManager 分区，跑在主窗口的 XUL 文档里，
   *   innerHTML 注入的 <script> 不执行）才要服务端那份带 MathML 的 HTML ——
   *   那时调用方传 opts.wantHtml。
   */
  kbviewPayloadForItem: async function (item, prefer, opts) {
    var self = ZoteroKB;
    const wantHtml = !!(opts && opts.wantHtml);
    let levels = [];
    try { levels = self.kbLevels() || []; } catch (e) { levels = []; }
    const out = {
      key: "", title: "",
      levels: levels.map(function (lv) { return { id: lv.id, label: lv.label }; }),
      level: self.kbviewLevel(), picked: "", fellBack: false, hint: "",
      md: "", pages: {}, order: [], imgBase: "",
      html: "", textLength: 0, fulltext: false,
      page: (opts && opts.page) || 0,
      edited: [], stale: [],
      empty: false, emptyWhy: "", probe: "",
      font: self.kbviewFont(), busy: false,
    };
    if (!item) {
      out.empty = true;
      out.emptyWhy = "先在左边选中一篇文献（一次只能选一篇）。";
      return out;
    }
    try { out.key = String(item.key || ""); } catch (e) { out.key = ""; }
    try { out.title = String((item.getField && item.getField("title")) || ""); }
    catch (e2) { out.title = ""; }
    if (!self.kbviewEnabled(item, "library")) {
      out.empty = true;
      out.emptyWhy = "这一条不是普通文献条目（附件 / 笔记没有知识库）。";
      return out;
    }
    let list = [];
    try { list = self.kbLevels(out.key) || []; } catch (e3) { list = []; }
    const byId = {};
    for (const lv of list) byId[lv.id] = lv;
    const picked = self.kbviewPickLevel(
      prefer || out.level, function (id) { return !!(byId[id] && byId[id].exists); });
    out.picked = picked.levelId;
    out.fellBack = !!picked.fellBack;
    if (!picked.levelId) {
      out.empty = true;
      out.emptyWhy = "知识库里还没有这一篇的任何分级文件。";
      let kb = "";
      try { kb = String(self.kbDir() || ""); } catch (e4) { kb = ""; }
      out.probe = (kb ? ("知识库目录：" + kb + "；")
                      : "知识库目录未确定（面板「运行环境」→ 服务状态）；")
        + "试过：" + ((byId[out.level] || {}).path || "");
      return out;
    }
    const path = (byId[picked.levelId] || {}).path
      || self.kbviewPath(picked.levelId, out.key);
    // 读 **markdown** 原文（页面自己渲染）。降级模式才另外读一次 .html 孪生。
    let text = "";
    try { text = await IOUtils.readUTF8(path); } catch (e6) { text = ""; }
    out.md = text;
    out.textLength = text.length;
    out.imgBase = self.kbviewImgBase(out.key);
    out.fulltext = picked.levelId === "fulltext";
    if (wantHtml) {
      // 降级模式：页面脚本跑不了（主窗口 XUL 文档里的 innerHTML 不执行 <script>），
      // 只能吃服务端渲染好的 MathML 孪生 —— 那是 mdhtml.py 的产物。
      let html = "";
      try { html = await IOUtils.readUTF8(String(path).replace(/\.md$/i, ".html")); }
      catch (e7) { html = ""; }
      if (!html) html = self.md2html(text);
      out.html = self.kbviewFixImgs(html, out.key);
    }
    const label = (byId[picked.levelId] || {}).label || picked.levelId;
    out.hint = label
      + (picked.fellBack ? "（默认那一级还没生成，已回退）" : "")
      + "　" + text.length + " 字";
    if (out.fulltext) {
      // 逐页正文：先按 "## p.N" 切页，每页再按块切开（空行分段 / 独立公式 /
      // 表格整块 / 图片行单独一块 —— 派发包 §3.2 的口径）。
      // 分块是**纯函数**（kbviewSplitBlocks），桩测试直接测它。
      let byPage = {};
      try { byPage = self.kbviewSplitMd(text); } catch (e8) { byPage = {}; }
      const pages = {};
      const order = [];
      for (const kk of Object.keys(byPage)) {
        const n = parseInt(kk, 10);
        if (!n) continue;
        pages[String(n)] = self.kbviewSplitBlocks(byPage[kk]);
        order.push(n);
      }
      order.sort(function (a, b) { return a - b; });
      out.pages = pages;
      out.order = order;
      out.md = "";                      // 逐页时整份 md 不必再传（省一半体积）
      let meta = {};
      // W3 线2.5（2026-10-14）：桥优先（`doc.get` = 与 HTTP `GET /doc-edit` **同一份**
      // 只读状态），拿不到再退回老 HTTP —— 失败了都一样当空（页面照样渲染）。
      try {
        try {
          meta = await self.kbAdminHostRoute("GET",
            "/doc-edit?key=" + encodeURIComponent(out.key));
        } catch (e0) {
          meta = await self.request("GET", "/doc-edit?key=" + encodeURIComponent(out.key));
        }
      } catch (e9) { meta = {}; }
      out.edited = (meta && meta.edited) || [];
      out.stale = (meta && meta.stale) || [];
    }
    return out;
  },

  /** 取当前条目 → 组装 → 发给页面。 */
  kbviewPush: async function (win, kind, prefer) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return false;
    const dock = kind === "reader" ? host.read : host.lib;
    if (!dock) return false;
    let payload = null;
    let opts = null;
    if (kind === "reader") {
      // 阅读器那一列要"打开就停在正在读的那一页"
      const info = self.kbviewReaderInfo(win);
      opts = { page: info ? info.page : 0 };
      if (opts.page) host.lastPage = opts.page;
    }
    try { payload = await self.kbviewPayloadForItem(self.kbviewCurrentItem(win), prefer, opts); }
    catch (e) {
      Zotero.debug("[zotero-kb] 组装侧栏内容失败：" + e);
      return false;
    }
    payload.type = "render";
    return self.kbviewSend(dock, payload);
  },

  /** 重新取当前条目 → 组装 → 发给页面（所有打开着的列都发）。 */
  kbviewPushOpen: async function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    if (host.lib && host.lib.open) await self.kbviewPush(win, "library", null);
    if (host.read && host.read.open) await self.kbviewPush(win, "reader", null);
  },

  /** 轮询看"当前条目变了没"（自建列没有 onItemChange 回调，只能自己盯）。 */
  kbviewRefresh: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    const open = (host.lib && host.lib.open) || (host.read && host.read.open);
    if (!open) { self.kbviewStopTimer(win); return; }
    // 桥没挂上就再试一次。
    // ⚠ 为什么不靠 `<browser>` 的 load 事件（kbviewBuildBrowser 里挂了那个监听）：
    //   真机实测（Zotero 10.0.6 / Firefox 140）**XUL `<browser>` 不触发 load 事件**
    //   —— 页面加载得好好的（contentWindow.kbvPageReady === true），但
    //   dock.ready 一直是 false，于是页面上的按钮（切级别 / 收起 / 改这一页）
    //   一个都不管用。改成"打开期间每拍补挂一次"，挂上就停。
    if (host.lib && host.lib.open && !host.lib.ready) {
      try { self.kbviewAttachBridge(win, "library"); } catch (e0) { /* 下一拍再试 */ }
    }
    if (host.read && host.read.open && !host.read.ready) {
      try { self.kbviewAttachBridge(win, "reader"); } catch (e1) { /* 下一拍再试 */ }
    }
    // ⚠ 这里**不再**做「按页跟随」（2026-10-11 用户拍板：跟随没生效也不用要了，
    //   手动翻找并不麻烦）—— 功能与代码一起去掉，见 kbviewFanoutPage 的注释。
    //   原来这里是 400ms 一轮的兜底重推（主路径是 Notifier 的 file/pageChange）。
    //   顺带：那也是一处**周期性重渲染**，是"渲染偶尔抽搐"最可疑的来源。
    if (host.read && host.read.open && !self.kbviewHasReader(win)) {
      // 阅读器标签全关了就把这一列收起来 —— 否则状态上"还开着"，
      // 定时器空转，用户再打开时会觉得"没关干净"（派发包 §五 第 6 条）。
      try { self.kbviewClose(win, "reader"); } catch (e6) { /* ignore */ }
    }
    const item = self.kbviewCurrentItem(win);
    let key = "";
    try { key = item && item.key ? String(item.key) : ""; } catch (e) { key = ""; }
    if (key === host.lastKey) return;
    host.lastKey = key;
    self.kbviewPushOpen(win).catch(function (e2) {
      Zotero.debug("[zotero-kb] 推送侧栏内容失败：" + e2);
    });
  },

  /**
   * 延时调度。
   * ⚠ 这个沙箱里 setInterval / 链式 setTimeout **不可靠**（见 18-taskpoll.js 的
   *   实测），所以统一用 Services.tm 的主线程定时器，setTimeout 只作兜底。
   */
  kbviewLateDispatch: function (fn, ms) {
    try {
      const tm = Services.tm;
      const target = tm.mainThread
        || (tm.currentThread && tm.currentThread.QueryInterface(
              Components.interfaces.nsIThread));
      if (target && tm.newTimer) {
        let qi;
        try { qi = ChromeUtils.generateQI(["nsITimerCallback"]); }
        catch (e) { qi = function () { return this; }; }
        const timer = tm.newTimer({
          observe: function () { try { fn(); } catch (e2) { Zotero.debug("" + e2); } },
          QueryInterface: qi,
        }, ms);
        if (timer) return timer;
      }
    } catch (e3) { /* 落回 setTimeout */ }
    try { return setTimeout(fn, ms); } catch (e4) { return null; }
  },

  kbviewStartTimer: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host || host.timerArmed) return;
    host.timerArmed = true;
    const tick = function () {
      if (!host.timerArmed) return;
      try { self.kbviewRefresh(win); }
      catch (e) { Zotero.debug("[zotero-kb] 侧栏轮询出错：" + e); }
      if (host.timerArmed) host.timer = self.kbviewLateDispatch(tick, 400);
    };
    host.timer = self.kbviewLateDispatch(tick, 400);
  },

  kbviewStopTimer: function (win) {
    var self = ZoteroKB;
    const host = self.kbviewHost(win, false);
    if (!host) return;
    host.timerArmed = false;
    try { if (host.timer && host.timer.cancel) host.timer.cancel(); } catch (e) { /* ignore */ }
    host.timer = null;
  },


  // ================================================================ 自建列：页内动作

  /** 「导出草稿」：把整份有效正文写成 edits/<key>.draft.md（只在「按页正文」显示）。 */
  kbviewExportDraft: async function (win, kind) {
    var self = ZoteroKB;
    const item = self.kbviewCurrentItem(win);
    if (!item) return false;
    self.kbviewHint(win, kind, "正在导出草稿…");
    try {
      // W3 线2.5：桥优先（`doc.save` = HTTP `POST /doc-edit` 同一份实现），失败退回老 HTTP。
      let r = null;
      try {
        r = await self.kbAdminHostRoute("POST", "/doc-edit",
                                        { key: item.key, action: "draft" });
      } catch (e0) {
        Zotero.debug("[zotero-kb] 引擎桥导草稿失败（退回 HTTP）：" + e0);
        r = await self.request("POST", "/doc-edit", { key: item.key, action: "draft" });
      }
      if (r && r.path) {
        self.kbviewHint(win, kind, "草稿已写出：" + r.path);
        try { self.openKbPath(r.path, "正文草稿"); } catch (e) { /* 打不开不影响导出 */ }
        return true;
      }
      self.kbviewHint(win, kind, "导出失败：" + JSON.stringify(r == null ? {} : r).slice(0, 120));
    } catch (e2) {
      self.kbviewHint(win, kind, "导出失败：" + ((e2 && e2.message) ? e2.message : e2));
    }
    return false;
  },

  // ⚠ 2026-10-11 用户反馈后**删掉了 kbviewOpenDoc**（以及页面上的「对照页」按钮）：
  //   阅读器右侧那一列已经能直接对着 PDF 看（同一个窗口、同一份正文），再跳一次
  //   浏览器反而多一步。
  // ⚠ **2026-10-12 用户拍板：浏览器兜底页整组删掉**（派发包《元数据与摘要与分层》§五）——
  //   online/docpage.{py,html} 与 GET /doc、/doc-text、/doc-raw、/doc-page.png、
  //   /kb-image 全部删除；**只保留 POST/GET /doc-edit**（正文修订层，本文件的
  //   kbviewSavePage 与条目窗格列的「改这一页」都靠它）。

  /** 保存 / 恢复一页正文（text == null 表示恢复原版）。 */
  kbviewSavePage: async function (win, kind, page, text) {
    var self = ZoteroKB;
    const item = self.kbviewCurrentItem(win);
    if (!item) return false;
    const body = { key: item.key, page: page };
    if (text == null) body.action = "restore";
    else body.text = String(text);
    const ok = await self.kbviewRunDocEdit(body, function (m) {
      self.kbviewHint(win, kind, m);
    });
    await self.kbviewPush(win, kind, null);
    return ok;
  },


  // ================================================================ 降级：老的「分区」模式（只读）

  /**
   * 退回 `ItemPaneManager.registerSection`。
   *
   * 什么时候走到这里：自建列找不到 `#zotero-item-pane` / `#zotero-view-item-sidenav`
   * / `#zotero-item-pane-content`（换 Zotero 版本、内部 id 变了）。
   * 这一档**只读**：能看分级内容，但没有"改这一页 / 导出草稿 / 独占一页"那些交互
   * —— 总比面板整个消失强，而且状态文件里会如实写下来（"非静默降级"）。
   */
  kbviewDegrade: function (why) {
    var self = ZoteroKB;
    self.writeStatusFile({ kbViewColumn: "degraded: " + why });
    Zotero.debug("[zotero-kb] 自建列不可用，退回分区模式：" + why);
    try { self.registerKbViewSection(); } catch (e) { /* ignore */ }
  },

  /** 注册分区（降级模式）。缺 ItemPaneManager 就如实记状态，不抛。 */
  registerKbViewSection: function () {
    var self = ZoteroKB;
    if (self.kbviewPaneID) return true;
    if (!Zotero.ItemPaneManager || !Zotero.ItemPaneManager.registerSection) {
      self.writeStatusFile({ kbViewPane: "unavailable: ItemPaneManager 不存在" });
      return false;
    }
    self.kbviewPaneID = Zotero.ItemPaneManager.registerSection({
      paneID: self.KBVIEW_ID,
      pluginID: self.id,
      // ⚠ header/sidenav 的 l10nID 必填，且 FTL 里必须是"只有名字"的条目
      header: { l10nID: "zotero-kb-kbview-header",
                icon: self.rootURI + "toolbar-icon.svg" },
      sidenav: { l10nID: "zotero-kb-kbview-sidenav",
                 icon: self.rootURI + "sidenav-icon.svg" },
      onItemChange: ({ item, tabType, setEnabled }) => {
        try { setEnabled(self.kbviewEnabled(item, tabType)); }
        catch (e) { /* ignore */ }
      },
      onRender: ({ doc, body, item }) => self.kbviewRender(doc, body, item),
    });
    self.writeStatusFile({
      kbViewPane: self.kbviewPaneID
        ? ("ok: " + self.kbviewPaneID + "（降级模式）")
        : "registerSection 返回 false",
    });
    return !!self.kbviewPaneID;
  },

  /** 卸载降级分区（插件 disable/卸载时调；不调会留下一个打不开的空分区）。 */
  unregisterKbViewSection: function () {
    var self = ZoteroKB;
    if (self.kbviewPaneID && Zotero.ItemPaneManager
        && Zotero.ItemPaneManager.unregisterSection) {
      Zotero.ItemPaneManager.unregisterSection(self.kbviewPaneID);
    }
    self.kbviewPaneID = null;
  },

  /** 降级模式的分区渲染：一行提示 + 级别下拉 + 正文（只读）。 */
  kbviewRender: async function (doc, body, item) {
    var self = ZoteroKB;
    if (!doc || !body) return;
    try {
      const old = body.querySelector(".zotero-kb-kbview");
      if (old) old.remove();
    } catch (e) { /* ignore */ }
    const wrap = self.kbviewEl(doc, "div");
    wrap.className = "zotero-kb-kbview";
    wrap.setAttribute("style",
      "padding:4px 6px; font-size:" + self.kbviewFont() + "px; line-height:1.55;"
      + " max-height:85vh; display:flex; flex-direction:column; overflow:hidden;");
    const bar = self.kbviewEl(doc, "div");
    bar.setAttribute("style", "display:flex; gap:6px; align-items:center;"
      + " flex-wrap:nowrap; margin-bottom:4px; flex:0 0 auto;");
    const sel = self.kbviewEl(doc, "select");
    sel.setAttribute("style", "flex:0 0 auto; white-space:nowrap;");
    for (const lv of (self.kbLevels() || [])) {
      const op = self.kbviewEl(doc, "option");
      op.setAttribute("value", lv.id);
      op.textContent = lv.label;
      sel.appendChild(op);
    }
    sel.value = self.kbviewLevel();
    const btn = self.kbviewEl(doc, "button");
    btn.textContent = "重新读取";
    btn.setAttribute("style", "padding:1px 6px; flex:0 0 auto; white-space:nowrap;");
    const hint = self.kbviewEl(doc, "span");
    hint.setAttribute("style", "opacity:0.65; font-size:11px; flex:1 1 auto;"
      + " min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;");
    bar.append(sel, btn, hint);
    wrap.append(bar);
    const view = self.kbviewEl(doc, "div");
    view.setAttribute("style", "flex:1 1 auto; min-height:0; overflow:auto;");
    wrap.append(view);
    body.append(wrap);
    const paint = async function (prefer) {
      hint.textContent = "读取中…";
      let payload = null;
      // 降级模式：跑不了页面脚本，只能吃服务端渲染好的 MathML 孪生
      try { payload = await self.kbviewPayloadForItem(item, prefer, { wantHtml: true }); }
      catch (e) { payload = null; }
      if (!payload) { hint.textContent = "读取失败"; return; }
      hint.textContent = payload.empty
        ? "（这一篇还没有知识库内容）" : String(payload.hint || "");
      hint.setAttribute("title", hint.textContent);
      if (payload.empty) {
        self.kbviewSetHtml(doc, view, "<p style='opacity:0.75'>"
          + String(payload.emptyWhy || "") + "</p>");
        return;
      }
      self.kbviewSetHtml(doc, view, payload.html
        || "<p style='opacity:0.75'>（这一级是空的）</p>");
    };
    sel.addEventListener("change", () => {
      self.setKbviewLevel(sel.value);
      paint(sel.value).catch(() => {});
    });
    btn.addEventListener("command", () => { paint(sel.value).catch(() => {}); });
    btn.addEventListener("click", () => { paint(sel.value).catch(() => {}); });
    await paint(self.kbviewLevel());
  },


  // ================================================================ 阅读器那一列：取当前页（**不再跟随**）

  /**
   * 阅读器当前页（**1 起**，对齐知识库的 p.N）。取不到返回 0。
   *
   * 两条来源：
   *   ① reader._state.primaryViewState.pageIndex —— **实时**的内部状态，翻页当场就变；
   *   ② items 表的「最后阅读页」—— 阅读器每次翻页都会写它（reader.js 的
   *      _setState：先 setAttachmentLastPageIndex，再
   *      Zotero.Notifier.trigger('pageChange','file',itemID)），事件到的那一刻它已是新值。
   * **① 优先**：② 里留着"上次退出时的页码"，先问它会拿到陈旧的页。
   * 两处都是 **0 起**的，这里 +1。
   *
   * ⚠ 2026-10-11 起**只用于打开那一列时的初始页**（翻页**不再**跟随）：
   *   派发包《去跟随与上下标》§一 要求保留「取当前页」的能力 —— 打开阅读器列的
   *   那一刻用它定位一次就够了。原来还挂过 Notifier 的 file/pageChange 做跟随，
   *   已随跟随一起去掉（实测 reader.js::_setState 确实会 trigger 它，能跟随，
   *   但用户拍板不要 —— 手翻一次正文并不麻烦，而周期性重推最像"抽搐"的来源）。
   */
  kbviewReaderPage: function (reader) {
    if (!reader) return 0;
    let idx = NaN;
    try {
      const s = reader._state;
      if (s && s.primaryViewState
          && Number.isFinite(s.primaryViewState.pageIndex)) {
        idx = s.primaryViewState.pageIndex;
      }
    } catch (e) { /* 走兜底 */ }
    if (!Number.isFinite(idx) || idx < 0) {
      try {
        const item = Zotero.Items.get(reader.itemID);
        if (item && item.getAttachmentLastPageIndex) {
          idx = item.getAttachmentLastPageIndex();
        }
      } catch (e2) { /* ignore */ }
    }
    if (!Number.isFinite(idx) || idx < 0) return 0;
    return Math.floor(idx) + 1;
  },

  /** 当前选中的阅读器标签（不在阅读器页签时返回 null）。 */
  kbviewReaderInfo: function (win) {
    if (!win) return null;
    let reader = null;
    try {
      const tabs = win.Zotero_Tabs;
      if (!tabs || String(tabs.selectedType || "") !== "reader") return null;
      reader = (Zotero.Reader && Zotero.Reader.getByTabID)
        ? Zotero.Reader.getByTabID(tabs.selectedID) : null;
    } catch (e) { reader = null; }
    if (!reader) return null;
    return { reader: reader, itemID: reader.itemID,
             page: ZoteroKB.kbviewReaderPage(reader) };
  },

  /** 这个窗口里还有没有阅读器？（关干净后要把我们那一列收起来。） */
  kbviewHasReader: function (win) {
    try {
      const tabs = win && win.Zotero_Tabs;
      if (tabs && String(tabs.selectedType || "") === "reader") return true;
      const list = tabs && tabs._tabs;
      if (list && list.length) {
        for (const t of list) { if (t && String(t.type) === "reader") return true; }
        return false;
      }
    } catch (e) { /* 走下面的兜底 */ }
    try {
      const rs = Zotero.Reader && Zotero.Reader._readers;
      if (rs && rs.length) {
        for (const r of rs) { if (r && r.itemID && !r._isTabClosed) return true; }
        return false;
      }
    } catch (e2) { /* ignore */ }
    return true;   // 问不到就当「还有」—— 宁可多留一会儿，也不要乱关
  },

  /**
   * Notifier：翻页时立刻把右边滚到同一页；阅读器标签关光了就收起我们那一列。
   *
   * 注册 id 推到 self.notifyIDs，shutdown 里统一注销（既有机制）。
   */
  kbviewRegisterNotifier: function () {
    var self = ZoteroKB;
    if (!Zotero.Notifier || !Zotero.Notifier.registerObserver) return false;
    if (self._kbvNotifierID
        && (self.notifyIDs || []).indexOf(self._kbvNotifierID) >= 0) {
      return true;             // 已经注册过（幂等；kbviewMount 每个窗口都会调）
    }
    self._kbvNotifierID = null;
    try {
      // ⚠ 只要 tab（选中/新开标签 → 补阅读器工具栏的「对照」按钮）。
      //   file 原来是给 pageChange（按页跟随）用的，2026-10-11 去跟随之后不再需要。
      self._kbvNotifierID = Zotero.Notifier.registerObserver({
        notify: function (event, type, ids) {
          try { ZoteroKB.kbviewOnNotify(event, type, ids); }
          catch (e) { Zotero.debug("[zotero-kb] 侧栏通知处理失败：" + e); }
        },
      }, ["tab"], self.id + "-kbview");
      if (self._kbvNotifierID) {
        if (!self.notifyIDs) self.notifyIDs = [];
        if (self.notifyIDs.indexOf(self._kbvNotifierID) < 0) {
          self.notifyIDs.push(self._kbvNotifierID);
        }
      }
      return !!self._kbvNotifierID;
    } catch (e) {
      Zotero.debug("[zotero-kb] 注册侧栏 Notifier 失败：" + e);
      self._kbvNotifierID = null;
      return false;
    }
  },

  kbviewOnNotify: function (event, type, ids) {
    var self = ZoteroKB;
    // ⚠ 这里**不再**处理 file/pageChange：按页跟随已去掉（2026-10-11 用户拍板），
    //   翻页时右边那一列不再重推、不再重渲染。留下的只有「补工具栏按钮」一条 ——
    //   会话恢复出来的阅读器是在我们注册 renderToolbar **之前** mount 的，
    //   那个一次性事件不会再发，只能靠标签事件补挂（见 kbviewInjectToolbarButton）。
    if (type === "tab" && (event === "select" || event === "add")) {
      self.kbviewEnsureReaderToolbar();
    }
  },

  /**
   * 一次性把「当前页」推给**已经打开**的阅读器列。
   *
   * ⚠ 2026-10-11 起**没有自动调用点**了：按页跟随去掉之后，原来两条自动路径
   *   （宿主侧 400ms 轮询重推、Notifier 的 file/pageChange）都删了。
   *   函数本身**保留**（派发包《去跟随与上下标》§一 明确要求"别整体删掉"）：
   *   它是唯一一处「把当前页一次性推给已打开的列」的逻辑，条目窗格那一列与
   *   将来真要做的「跳到这一页」按钮都直接调它；真删了这条能力就彻底没有了。
   */
  kbviewFanoutPage: function (ids) {
    var self = ZoteroKB;
    const m = self.kbviewMap();
    if (!m) return;
    const hosts = [];
    try { m.forEach(function (v) { hosts.push(v); }); } catch (e) { return; }
    for (const host of hosts) {
      const dock = host.read;
      if (!dock || !dock.open) continue;
      try {
        const info = self.kbviewReaderInfo(host.win);
        if (!info || ids.indexOf(info.itemID) < 0) continue;
        if (!info.page || info.page === host.lastPage) continue;
        host.lastPage = info.page;
        self.kbviewSend(dock, { type: "page", page: info.page });
      } catch (e2) { /* 一个窗口出错不影响别的 */ }
    }
  },

  // ================================================================ 阅读器工具栏的「对照」按钮

  /**
   * 注册阅读器工具栏的「对照」按钮（**一键进入/退出对照模式**，派发包 §二）。
   *
   * Zotero.Reader.registerEventListener('renderToolbar', handler, pluginID)：
   * 阅读器每次渲染工具栏都会发这个事件，handler 拿到
   * {type, doc, params, append, reader}，**必须同步** append。
   * pluginID 传了之后，插件停用时 Zotero 会自己把它摘掉（不用手动反注册）。
   */
  kbviewRegisterReaderToolbar: function () {
    var self = ZoteroKB;
    if (self._kbvReaderToolbar) return true;
    if (!Zotero.Reader || typeof Zotero.Reader.registerEventListener !== "function") {
      return false;
    }
    self._kbvReaderToolbar = function (event) {
      try { ZoteroKB.kbviewAddToolbarButton(event); }
      catch (e) { Zotero.debug("[zotero-kb] 加阅读器「对照」按钮失败：" + e); }
    };
    try {
      Zotero.Reader.registerEventListener("renderToolbar", self._kbvReaderToolbar, self.id);
      self.writeStatusFile({ kbViewReaderToolbar: "ok" });
      return true;
    } catch (e) {
      self._kbvReaderToolbar = null;
      self.writeStatusFile({ kbViewReaderToolbar: "FAIL: " + e });
      return false;
    }
  },

  /**
   * 事件回调里往阅读器工具栏塞按钮。
   *
   * ⚠ **必须同步创建并 append**：阅读器那边 append 之后再调用就会抛
   *   "Append must be called directly and synchronously in the event"
   *   （见 resource/reader/reader.js 的 CustomSections）。
   * ⚠ 每次渲染事件都会来、工具栏的 custom-sections 每次都被 replaceChildren 清空，
   *   所以这里**每次都新建**一个（不是「已存在就跳过」）。
   */
  kbviewAddToolbarButton: function (event) {
    var self = ZoteroKB;
    const doc = event && event.doc;
    if (!doc || !doc.createElement || typeof event.append !== "function") return false;
    let win = null;
    try { win = Zotero.getMainWindow(); } catch (e) { win = null; }
    const btn = doc.createElement("button");
    btn.setAttribute("id", "zotero-kb-kbview-toolbar-button");
    btn.setAttribute("class", "toolbar-button");
    btn.setAttribute("title", "对照：右侧显示这一页的知识库正文（一键开 / 关）");
    btn.textContent = "对照";
    if (win && self.kbviewIsOpen(win, "reader")) {
      btn.setAttribute("aria-pressed", "true");
      btn.setAttribute("data-kbview-open", "true");
    }
    btn.addEventListener("click", function () {
      try {
        let w = null;
        try { w = Zotero.getMainWindow(); } catch (e2) { w = null; }
        self.kbviewToggle(w || win, "reader");
      } catch (e3) { Zotero.debug("[zotero-kb] 「对照」按钮出错：" + e3); }
    });
    event.append(btn);
    return true;
  },

  /**
   * 直接往阅读器工具栏**注入**一个「对照」按钮（幂等）。
   *
   * ⚠ 为什么需要它（真机实测，2026-10-11）：renderToolbar 是**一次性**的
   *   （React 那个 CustomSections 只在 mount 时发一次），而 Zotero 会**在启动时
   *   恢复会话里的阅读器标签** —— 那些阅读器可能在插件注册监听之前就挂好了，
   *   于是我们永远收不到事件，工具栏上就没有按钮（现象：阅读器右侧那一列只能
   *   靠 rail 图标开，工具栏上找不到入口）。
   *   这里绕过事件、直接把按钮塞进 React 那个 .custom-sections 容器；
   *   下次阅读器重渲染时会清掉，但那一刻 renderToolbar 会来，又会补上。
   */
  kbviewInjectToolbarButton: function (reader) {
    var self = ZoteroKB;
    const w = reader && reader._iframeWindow;
    if (!w) return false;
    let doc = null;
    try { doc = w.document; } catch (e) { return false; }
    if (!doc) return false;
    if (doc.getElementById("zotero-kb-kbview-toolbar-button")) return true;
    const host = doc.querySelector(".custom-sections");
    if (!host) return false;                 // 工具栏还没渲染 → 等下一次
    const btn = doc.createElement("button");
    btn.setAttribute("id", "zotero-kb-kbview-toolbar-button");
    btn.setAttribute("class", "toolbar-button");
    btn.setAttribute("title", "对照：右侧显示这一页的知识库正文（一键开 / 关）");
    btn.textContent = "对照";
    if (self.kbviewIsOpen(Zotero.getMainWindow(), "reader")) {
      btn.setAttribute("aria-pressed", "true");
    }
    btn.addEventListener("click", function () {
      try { self.kbviewToggle(Zotero.getMainWindow(), "reader"); }
      catch (e3) { Zotero.debug("[zotero-kb] 「对照」按钮出错：" + e3); }
    });
    const section = doc.createElement("div");
    section.setAttribute("class", "section");
    section.appendChild(btn);
    try { host.appendChild(section); } catch (e4) { return false; }
    return true;
  },

  /** 把「对照」按钮补到**所有已经挂好的**阅读器工具栏上（幂等）。 */
  kbviewEnsureReaderToolbar: function () {
    var self = ZoteroKB;
    let n = 0;
    try {
      const rs = (Zotero.Reader && Zotero.Reader._readers) || [];
      for (const r of rs) {
        try { if (self.kbviewInjectToolbarButton(r)) n++; }
        catch (e) { /* 单个阅读器失败不影响别的 */ }
      }
    } catch (e2) { /* ignore */ }
    return n;
  },

  // ================================================================ 图片与分块（纯函数）

  /**
   * 知识库图片的 resource:// 前缀（<kb>/mineru/<key>/ 下的相对路径拼在它后面）。
   *
   * ⚠ 为什么走 resource 而不是 file://（老做法）：resource:// 的页面加载
   *   file:// 图片会被安全策略**拦掉**，图片全空白且不报错（见 KB_FILES_RES）。
   */
  kbviewImgBase: function (key) {
    var self = ZoteroKB;
    try {
      const base = self.kbviewFilesURI();
      if (!base || !key) return "";
      return base + "mineru/" + String(key) + "/";
    } catch (e) { return ""; }
  },

  /** 阅读器栏里点了一张图 → 用系统默认程序打开 <kb>/mineru/<key>/<rel>。 */
  kbviewOpenImage: function (win, kind, rel) {
    var self = ZoteroKB;
    const item = self.kbviewCurrentItem(win);
    if (!item) return false;
    let kb = "";
    try { kb = String(self.kbDir() || "").replace(/[\\/]+$/, ""); } catch (e) { kb = ""; }
    const r = String(rel == null ? "" : rel).replace(/\\/g, "/").replace(/^\.\//, "");
    if (!kb || !r) return false;
    // 只允许相对路径，且不许穿越目录（与 docpage.image_path 同一口径）
    if (/^[a-z][a-z0-9+.\-]*:/i.test(r) || r.charAt(0) === "/" || r.indexOf("..") >= 0) {
      return false;
    }
    const abs = kb + "\\mineru\\" + String(item.key) + "\\" + r.replace(/\//g, "\\");
    try {
      self.openKbPath(abs, "图片原图");
      return true;
    } catch (e2) {
      Zotero.debug("[zotero-kb] 打开原图失败：" + e2);
      return false;
    }
  },

  // 管道表 / 独立公式 / 独立图片行（与 mdhtml.py 同一套判据）
  KBV_RE_TABLE_ROW: /^\s*\|.*\|\s*$/,
  KBV_RE_TABLE_SEP: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
  KBV_RE_LINE_MATH: /^\s*(?:\$\$.+\$\$|\\\[.+\\\])\s*$/,
  KBV_RE_LINE_IMG: /^\s*!\[[^\]]*\]\([^)]+\)\s*$/,

  /**
   * 把一页 markdown 切成**块**（派发包 §3.2 的口径），并保留块之间的原文分隔符 ——
   * **一字不改地拼回去**是硬要求（kbviewJoinBlocks(parts) === 原文，桩测试钉住）：
   *   · 空行分段；独立成行的 $$…$$ / \[…\] 自成一「块」；
   *   · 管道表整块；独占一行的图片行单独一块；段内单换行仍属同一块。
   *
   * 返回 {prefix, blocks[], seps[], suffix}，拼法就是**纯字符串相加**：
   *   prefix + blocks[0] + seps[0] + blocks[1] + … + suffix
   * seps[i] 是 blocks[i] 与 blocks[i+1] 之间的原文（含换行；空行也在里面）。
   */
  kbviewSplitBlocks: function (md) {
    var self = ZoteroKB;
    const s = String(md == null ? "" : md).replace(/\r\n?/g, "\n");
    const lines = s.split("\n");
    const n = lines.length;
    const off = new Array(n);
    let acc = 0;
    for (let i = 0; i < n; i++) { off[i] = acc; acc += lines[i].length + 1; }
    const endOf = function (i) { return off[i] + lines[i].length; };
    const blank = function (i) { return !lines[i].trim(); };
    const lineMath = function (i) { return self.KBV_RE_LINE_MATH.test(lines[i]); };
    const lineImg = function (i) { return self.KBV_RE_LINE_IMG.test(lines[i]); };
    const tableAt = function (i) {
      return i + 1 < n && self.KBV_RE_TABLE_ROW.test(lines[i])
        && self.KBV_RE_TABLE_SEP.test(lines[i + 1]);
    };
    // 先算出每个块覆盖哪些行（[a, b)：左闭右开、互不重叠、按顺序）
    const region = [];
    let k = 0;
    while (k < n) {
      if (blank(k)) { k++; continue; }
      let b;
      if (lineMath(k)) {
        b = k + 1;
      } else if (tableAt(k)) {
        b = k + 2;
        while (b < n && self.KBV_RE_TABLE_ROW.test(lines[b])) b++;
      } else if (lineImg(k)) {
        b = k + 1;
      } else {
        b = k + 1;
        while (b < n && !blank(b) && !lineMath(b) && !lineImg(b) && !tableAt(b)) b++;
      }
      region.push([k, b]);
      k = b;
    }
    if (!region.length) return { prefix: s, blocks: [], seps: [], suffix: "" };
    const prefix = s.slice(0, off[region[0][0]]);
    const blocks = [];
    const seps = [];
    for (let r = 0; r < region.length; r++) {
      const a = region[r][0], bb = region[r][1];
      blocks.push(s.slice(off[a], endOf(bb - 1)));
      if (r + 1 < region.length) {
        seps.push(s.slice(endOf(bb - 1), off[region[r + 1][0]]));
      }
    }
    const lastR = region[region.length - 1];
    const suffix = s.slice(endOf(lastR[1] - 1));
    return { prefix: prefix, blocks: blocks, seps: seps, suffix: suffix };
  },

  /** kbviewSplitBlocks 的逆（纯字符串相加）。 */
  kbviewJoinBlocks: function (parts) {
    if (!parts) return "";
    let s = String(parts.prefix == null ? "" : parts.prefix);
    const blocks = parts.blocks || [];
    const seps = parts.seps || [];
    for (let i = 0; i < blocks.length; i++) {
      s += String(blocks[i] == null ? "" : blocks[i]);
      if (i < seps.length) s += String(seps[i] == null ? "" : seps[i]);
    }
    s += String(parts.suffix == null ? "" : parts.suffix);
    return s;
  },


  // ================================================================ 纯函数与文件工具（自建列 / 降级模式共用）

  /**
   * 把正文 markdown 按 "## p.N" 切成 {页码: 该页 markdown}（**纯函数**，桩测试直接测）。
   * 为什么在插件侧再切一次：服务端渲染好的 HTML 里拿不回 markdown 源文本，而
   * 「改这一页」要编辑的正是 markdown（图片行、公式、表格都是 markdown）。
   */
  kbviewSplitMd: function (md) {
    var out = {};
    var cur = null;
    var buf = [];
    var lines = String(md == null ? "" : md).split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var m = lines[i].match(/^##\s*p\.(\d+)\s*$/);
      if (m) {
        if (cur !== null) out[cur] = buf.join("\n").replace(/^\n+|\n+$/g, "");
        cur = parseInt(m[1], 10);
        buf = [];
      } else if (cur !== null) {
        buf.push(lines[i]);
      }
    }
    if (cur !== null) out[cur] = buf.join("\n").replace(/^\n+|\n+$/g, "");
    return out;
  },

  /** 轮询一个 job 直到结束（最多约 6 分钟）。返回 job 对象，拿不到返回 {}。 */
  kbviewWaitJob: async function (jobId, onTick) {
    var self = ZoteroKB;
    if (!jobId) return {};
    for (var i = 0; i < 240; i++) {
      var job = {};
      try { job = await self.request("GET", "/jobs/" + encodeURIComponent(jobId)); }
      catch (e) { return {}; }
      if (onTick) onTick(job);
      if (job && job.state && job.state !== "running") return job;
      await new Promise(function (r) { setTimeout(r, 1500); });
    }
    return { state: "timeout" };
  },

  /**
   * 保存/恢复一页正文：POST /doc-edit -> 等 job -> 返回是否成功。
   * 保存之后服务端会**重切重向量**（用户口径"改完立即"），所以这里必须等它跑完
   * 再重绘，否则页面读到的还是旧内容。
   */
  kbviewRunDocEdit: async function (body, onStatus) {
    var self = ZoteroKB;
    var note = onStatus || function () {};
    note("正在重建切片…");
    var res = null;
    // W3 线2.5（2026-10-14）：桥优先（`doc.save`，**写路径**：保存某一页的修订并派一次
    // 单篇重建 —— 与 HTTP `POST /doc-edit` 同一份实现），失败退回老 HTTP。
    try {
      try { res = await self.kbAdminHostRoute("POST", "/doc-edit", body); }
      catch (e0) {
        Zotero.debug("[zotero-kb] 引擎桥保存正文失败（退回 HTTP）：" + e0);
        res = await self.request("POST", "/doc-edit", body);
      }
    } catch (e) {
      note("请求失败：" + ((e && e.message) ? e.message : e));
      return false;
    }
    if (!res || !res.job) {
      note("服务没有返回任务：" + JSON.stringify(res == null ? {} : res).slice(0, 120));
      return false;
    }
    var done = await self.kbviewWaitJob(res.job, function (j) {
      var lg = (j && j.log) || [];
      note("正在重建切片… " + String(lg[lg.length - 1] || "").slice(0, 60));
    });
    if (done && done.state === "done") { note("已重建，正在刷新…"); return true; }
    note("重建失败：" + ((done && done.error) || "未知"));
    return false;
  },

  /**
   * 把 `<img src="相对路径">` 改写成能加载的 `resource://zotero-kb-files/…`，
   * 并把**绝对路径**写进 `data-kbv-path`（页面上点图"看原图"仍交给系统默认程序）。
   *
   * kb 文件里图片是 `images/xxx.jpg`（相对 `<kb>/mineru/<key>/`），这样知识库
   * 整体搬家不用重写任何文件；绝对/协议相对的 src 原地不动（幂等），
   * 拿不到 kbDir 时原样返回（宁可裂图，也不要拼出半个坏 URL）。
   *
   * ⚠ 这里**不能**用 `file://`：resource:// 的页面加载 file:// 图片会被安全策略
   *   拦掉，表现是图片全都空白（naturalWidth=0）—— 真机实测踩到，见 KB_FILES_RES。
   */
  kbviewFixImgs: function (html, key) {
    var self = ZoteroKB;
    var s = String(html == null ? "" : html);
    if (s.indexOf("<img") < 0) return s;
    var base = "";
    var dir = "";
    try {
      const kb = String(self.kbDir() || "").replace(/[\\/]+$/, "");
      if (kb && key) {
        dir = kb + "\\mineru\\" + key + "\\";
        base = self.kbviewFilesURI();
      }
    } catch (e) { base = ""; dir = ""; }
    if (!base || !dir) return s;
    return s.replace(/(<img\b[^>]*?)\bsrc="([^"]*)"/g,
      function (m, pre, src) {
        if (!src || /^(?:[a-z][a-z0-9+.\-]*:|\/\/|#)/i.test(src)) return m;
        const rel = String(src).replace(/\\/g, "/").replace(/^\.\//, "");
        const abs = dir + rel.replace(/\//g, "\\");
        return pre + 'src="' + base + "mineru/" + key + "/" + rel
          + '" data-kbv-path="' + abs.replace(/"/g, "&quot;") + '"';
      });
  },

  /**
   * 最小 markdown → HTML（**纯函数**，桩测试直接测它）。
   * 覆盖知识库 md 里真正用到的语法：# 标题、-/数字列表、> 引用、**粗**、*斜*、
   * 反引号代码、[文字](链接)、$$公式$$、![图注](图片)、管道表、空行分段。
   * ⚠ 先转义 HTML 再套标记：md 是我们自己生成的，但笔记原文里有 <、&。
   */
  md2html: function (md) {
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s)
      .replace(/\$\$([^$]+)\$\$/g,
               '<code style="background:rgba(127,127,127,0.15);'
               + ' padding:0 3px; border-radius:3px;">$1</code>')
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g,
               '<img src="$2" alt="$1" style="max-width:100%; height:auto;'
               + ' display:block; margin:4px 0;" />')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

    const out = [];
    let list = "";
    let buf = [];
    let pageOpen = false;
    const flushBuf = () => {
      if (buf.length) { out.push("<p>" + buf.join("<br>") + "</p>"); buf = []; }
    };
    const closeList = () => {
      if (list) { out.push("</" + list + ">"); list = ""; }
    };
    const cell = (tag, text) => "<" + tag + " style=\"border:1px solid"
      + " rgba(127,127,127,0.45); padding:2px 5px; text-align:left;"
      + " vertical-align:top;\">" + inline(text) + "</" + tag + ">";
    const splitRow = (line) => {
      let s = String(line).trim();
      if (s.charAt(0) === "|") s = s.slice(1);
      if (s.charAt(s.length - 1) === "|") s = s.slice(0, -1);
      return s.split("|").map((c) => c.trim());
    };
    const lines = String(md == null ? "" : md).split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const rawI = lines[i];
      if (/^\s*\|.*\|\s*$/.test(rawI)
          && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?/.test(lines[i + 1])) {
        flushBuf(); closeList();
        const rows = [splitRow(rawI)];
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        i--;
        const head = rows[0] || [];
        let html = "<table style=\"border-collapse:collapse; margin:6px 0;"
          + " font-size:0.95em; max-width:100%;\"><thead><tr>"
          + head.map((c) => cell("th", c)).join("") + "</tr></thead>";
        if (rows.length > 1) {
          html += "<tbody>";
          for (const r of rows.slice(1)) {
            const cells = r.slice(0, head.length);
            while (cells.length < head.length) cells.push("");
            html += "<tr>" + cells.map((c) => cell("td", c)).join("") + "</tr>";
          }
          html += "</tbody>";
        }
        out.push(html + "</table>");
        continue;
      }
      const raw = rawI;
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { flushBuf(); closeList(); continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+p\.(\d+)\s*$/))) {
        flushBuf(); closeList();
        if (pageOpen) out.push("</div>");
        out.push('<div class="kb-page" data-kbpage="' + m[2] + '">');
        pageOpen = true;
        const pLvl = m[1].length;
        const pSize = { 1: "1.35em", 2: "1.18em", 3: "1.06em" }[pLvl] || "1em";
        out.push('<div style="font-weight:600; font-size:' + pSize
                 + '; margin:6px 0 2px;">p.' + m[2] + "</div>");
        continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushBuf(); closeList();
        const lvl = m[1].length;
        const size = { 1: "1.35em", 2: "1.18em", 3: "1.06em" }[lvl] || "1em";
        out.push('<div style="font-weight:600; font-size:' + size + '; margin:6px 0 2px;">'
                 + inline(m[2]) + "</div>");
        continue;
      }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
        flushBuf();
        if (list !== "ul") { closeList(); out.push("<ul style='margin:2px 0 2px 16px;'>"); list = "ul"; }
        out.push("<li>" + inline(m[1]) + "</li>");
        continue;
      }
      if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
        flushBuf();
        if (list !== "ol") { closeList(); out.push("<ol style='margin:2px 0 2px 18px;'>"); list = "ol"; }
        out.push("<li>" + inline(m[2]) + "</li>");
        continue;
      }
      if ((m = line.match(/^>\s?(.*)$/))) {
        flushBuf(); closeList();
        out.push("<blockquote style='margin:4px 0; padding:1px 6px;"
                 + " border-left:3px solid rgba(127,127,127,0.5); opacity:0.85;'>"
                 + inline(m[1]) + "</blockquote>");
        continue;
      }
      if ((m = line.match(/^\s*\$\$(.+)\$\$\s*$/) || line.match(/^\s*\\\[(.+)\\\]\s*$/))) {
        flushBuf(); closeList();
        out.push("<div style='margin:4px 0; padding:3px 6px; font-family:monospace;"
                 + " background:rgba(127,127,127,0.12); border-radius:3px;"
                 + " overflow-x:auto;'>" + esc(m[1]) + "</div>");
        continue;
      }
      buf.push(inline(line));
    }
    flushBuf(); closeList();
    if (pageOpen) out.push("</div>");
    return out.join("\n");
  },
});

// ===== src/21-kbcols.js =====
/**
 * 21-kbcols.js —— 文献列表里的两列：「已建知识库」「分节纲要」
 *
 * 用户的需求（原话）："在 Zotero 的文献列表里一眼看出这篇建过知识库没有、
 * 有没有分节纲要，方便管理（现在只能靠面板一页页翻）"。
 *
 * 数据来源：本地服务 `GET /col-status`（**一次全量**）。
 * ⚠ 插件里**不读** index.db / views 目录 —— token、知识库路径、迁移全在服务端，
 *   插件只认 HTTP（与 16-weightcol.js 同一套分工）。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ============================================================ 两列：建库 / 纲要

  /**
   * 拉一次全量列状态放进内存缓存。
   *
   * 为什么必须"全量 + 缓存"：ItemTree 的 dataProvider 是**同步**的、每一行
   * 都会被调用 —— 不可能在里面发网络请求（几百行会把本地服务打爆）。
   * 所以这里一次把 key → {in_kb, outline_sections} 拉进来，
   * dataProvider 只查表（见 kbColValue）。
   *
   * 拿不到服务时**静默降级**：不弹窗、不抛错（文献列表是高频 UI），
   * 只 Zotero.debug 一句，并且**保留上一次的数据** —— 服务重启几秒钟，
   * 不该让两列一起变空。
   *
   * W3 线2.5（2026-10-13）：**优先走宿主半的引擎桥**
   * （`kbAdminHostRoute("GET","/col-status")` → 引擎 `kb.colStatus`），拿不到再退回
   * 老 HTTP —— 过渡期两条都必须能用。桥不需要常驻 8765（引擎随 Zotero 起停）。
   * ⚠ 引擎那边 `outline_sections` 必须走 `meta.ai_outline:`（读端口径），
   *   不是数 `views/*.outline.md` 文件个数：本机 94 篇有 meta、只有 50 个文件，
   *   按文件数会把 44 篇有纲要的显示成 ✗。
   */
  refreshKbCols: async function () {
    var self = ZoteroKB;
    try {
      let res = null;
      try {
        res = await self.kbAdminHostRoute("GET", "/col-status");
      } catch (e) {
        Zotero.debug("[zotero-kb] 引擎桥取列状态失败（退回 HTTP）：" + e);
      }
      if (!res || !res.ok || !res.items) {
        // 桥不可用 / 桥没给数据 → 走老 HTTP 兜一次（过渡期两条都活着）。
        res = await self.request("GET", "/col-status");
      }
      if (!res || !res.ok || !res.items) {
        Zotero.debug("[zotero-kb] 拉列状态失败（保持原样）："
                     + ((res && (res.error || res.hint)) || "响应里没有 items"));
        return false;
      }
      const sig = JSON.stringify(res.items);
      self.colStatusCache = res.items;
      // ready 的含义是"手上这份缓存是**完整**的"：只有完整的一份，
      // "映射里查不到"才能解释成"这篇还没建库"。没 ready 时一律留空，
      // 免得把"服务没数据"画成"每条都没建库"（那是两句完全不同的话）。
      self.colStatusReady = true;
      if (sig === self.colStatusSig) return true;   // 内容没变就不重画（同 refreshWeights）
      self.colStatusSig = sig;
      Zotero.debug("[zotero-kb] 列状态已更新：" + Object.keys(res.items).length
                   + " 条（有纲要 " + (res.with_outline || 0) + " 条）");
      this.redrawItemTree();
      return true;
    } catch (e) {
      Zotero.debug("[zotero-kb] 拉列状态异常（保持原样）：" + e);
      return false;
    }
  },


  /**
   * 注册两列。列定义照 16-weightcol.js 那套写：
   *   · `flex: 1` 而不是固定 width（内置列绝大多数是 flex，固定宽度会让列
   *     在虚拟表格里定位不自然 —— 本机踩过，用户原话"不像正常加进去的列"）；
   *   · zoteroPersist 带上 width / hidden / sortDirection。
   *
   * ⚠ 关于 `sortable`：16 里写了 `sortable: true`，但**Zotero 不读这个字段**
   *   —— 列定义的 schema（xpcom/pluginAPI/itemTreeManager.js 的
   *   optionTypeDefinition）里根本没有它；表头点击走的是虚拟表格自己的
   *   `_handleHeaderMouseUp → onColumnSort`（components/virtualized-table.js），
   *   对**所有**列都开着（只有 staticColumns 那一档例外）。
   *   所以这里不写它：排序照样能用，而多写一个没人读的字段，只会让后来的人
   *   以为"排序是靠它开的"。
   */
  registerKbColumns: function () {
    try {
      if (!Zotero.ItemTreeManager || !Zotero.ItemTreeManager.registerColumn) {
        Zotero.debug("[zotero-kb] 这个 Zotero 版本没有 ItemTreeManager，跳过两列");
        return;
      }
      const self = ZoteroKB;
      self.kbColKeys = [];
      const add = (opt) => {
        try {
          const k = Zotero.ItemTreeManager.registerColumn(opt);
          if (k) self.kbColKeys.push(k);
          else Zotero.debug("[zotero-kb] 列没注册上（可能已存在）：" + opt.dataKey);
        } catch (e) {
          // 单列失败不该影响另一列，更不该影响插件启动
          Zotero.debug("[zotero-kb] 注册列 " + opt.dataKey + " 失败：" + e);
        }
      };

      const common = {
        pluginID: self.id,
        enabledTreeIDs: ["main"],            // 只加到主列表（不加到 feed 等）
        // ⚠ 新列**必须**声明 defaultIn，否则注册成功却"不出现"（本机实测踩到）：
        //   虚拟表格 _getColumns() 里，只要**任何**一列带 defaultIn
        //   （内置的 title / firstCreator / hasAttachment 都带），
        //   新列就会被算成 hidden=true：
        //       if (hasDefaultIn && this.collectionTreeRows.length)
        //         column.hidden = !(column.defaultIn && matchesViewType(...))
        //   现象是：列注册了（kbColKeys 有值、`_columns` 里也有、表头 DOM 里却
        //   没有它），列表上完全看不见 —— 只能自己去右键"列"菜单里勾出来。
        //   ["*"] = 所有视图类型都默认显示（_matchesViewType 对 '*' 直接返回 true），
        //   与内置 title / hasAttachment 的写法一致。
        //   用户之后自己在列菜单里取消勾选，那个选择会存进 profile 的
        //   treePrefs.json，并在 _getColumns 里覆盖这一行 —— 不会被我们顶掉。
        //   （ItemTreeManager 会对 defaultIn 打一句"已废弃"的 debug 提示，但它与
        //    enabledTreeIDs 是两件事：后者管"进哪些列表"，前者管"默认显示不显示"
        //    —— 内置列至今仍在用它。）
        defaultIn: ["*"],
        flex: 1,
        minWidth: 70,
        zoteroPersist: ["width", "hidden", "sortDirection"],
      };
      add(Object.assign({}, common, {
        dataKey: "kbBuilt",
        label: "已建知识库",
        dataProvider: function (item) { return self.kbColValue(item, "built"); },
        renderCell: function (index, data, column, isFirstColumn, doc) {
          return self.kbColCell(doc, column, data, "built");
        },
      }));
      add(Object.assign({}, common, {
        dataKey: "kbOutline",
        label: "分节纲要",
        dataProvider: function (item) { return self.kbColValue(item, "outline"); },
        renderCell: function (index, data, column, isFirstColumn, doc) {
          return self.kbColCell(doc, column, data, "outline");
        },
      }));
      Zotero.debug("[zotero-kb] 已注册知识库列：" + (self.kbColKeys.join(", ") || "无"));
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /** 注销两列（插件卸载 / 禁用时调用，避免留下悬空列）。 */
  unregisterKbColumns: function () {
    var self = ZoteroKB;
    try {
      if (self.kbColKeys && self.kbColKeys.length && Zotero.ItemTreeManager
          && Zotero.ItemTreeManager.unregisterColumn) {
        self.kbColKeys.forEach((k) => {
          try { Zotero.ItemTreeManager.unregisterColumn(k); }
          catch (e) { /* 单列失败也要把另一列摘掉 */ }
        });
        Zotero.debug("[zotero-kb] 知识库两列已注销");
      }
    } catch (e) { Zotero.logError(e); }
    self.kbColKeys = null;
  },


  /**
   * 某一列某一行该显示什么（dataProvider 的实现，**同步、只查表**）。
   *
   * which = "built"   → "✓" / "✗"
   * which = "outline" → 补零的节数（如 "012"，见下面的说明）/ "✗"
   * 一次都没拉到过服务 → ""（留空）
   */
  kbColValue: function (item, which) {
    var self = ZoteroKB;
    try {
      if (!item || !item.key) return "";
      // 从没成功拉到过（服务没跑 / 知识库还没建）→ **留空**。
      // 不能画成"✗"：那等于替用户断言"这篇没建库"，而事实是"我不知道"。
      if (!self.colStatusReady) return "";
      const c = self.colStatusCache[item.key];
      const inKb = !!(c && c.in_kb);          // 完整映射里查不到 = 还没建库
      if (which === "built") return inKb ? "✓" : "✗";
      const n = (c && typeof c.outline_sections === "number")
        ? c.outline_sections : 0;
      if (!inKb || n <= 0) return "✗";
      // ⚠ 节数**补零**再交给 Zotero，否则排序是错的：
      //   自定义列的排序是"把 dataProvider 的字符串按本地化 collation 比"
      //   （itemTree.js 的 _compareField → _sortCollation.compareString），
      //   不补零时 "13" < "4"，点一下表头会看到 13 节排在 4 节前面。
      //   补零只影响排序键；给人看的文字在 kbColText 里把前导零去掉。
      return String(n).padStart(3, "0");
    } catch (e) {
      return "";
    }
  },


  /** 把列值变成给人看的文字（"012" → "12 节"）。 */
  kbColText: function (data, which) {
    if (data == null || data === "") return "";
    if (which !== "outline") return String(data);
    if (String(data) === "✗") return "✗";
    const n = parseInt(String(data).replace(/^0+/, ""), 10);
    return isNaN(n) ? String(data) : (n + " 节");
  },


  /**
   * renderCell：复刻 Zotero 的标准单元格结构（否则列会"不像正常的列"，
   * 本机踩过 —— 见 16-weightcol.js 里那段说明）：
   *   · className 必须含 `cell`（样式/对齐/内边距挂在这个类上）；
   *   · 用传进来的 document（拿不到才退回主窗口的）；
   *   · 空值也要返回元素，不能返回 null。
   *
   * ⚠ 文字外面**多套了一层 span**，这是为了提示能显示出来：
   *   虚拟表格的 _handleMouseOver（components/virtualized-table.js）会给
   *   "第一个子节点是文本"的单元格**清掉 title**（文字没被截断就
   *   removeAttribute('title')）。套一层之后，鼠标悬停在文字上时它清的是
   *   内层（内层没设 title），外层这个 title 才留得住。
   */
  kbColCell: function (doc, column, data, which) {
    var self = ZoteroKB;
    let doc2 = doc;
    if (!doc2) {
      try {
        doc2 = (Zotero.getMainWindow && Zotero.getMainWindow().document) || document;
      } catch (e) { doc2 = document; }
    }
    const span = doc2.createElement("span");
    // 自定义列的 column.className 可能是 undefined，兜个底
    span.className = "cell " + ((column && column.className) || "kb-col");
    const text = doc2.createElement("span");
    const shown = self.kbColText(data, which);
    text.textContent = shown;
    span.appendChild(text);
    if (!shown) return span;        // 没数据：留空，但保留标准结构
    span.style.fontVariantNumeric = "tabular-nums";
    if (which === "built") {
      const on = String(data).indexOf("✓") >= 0;
      span.style.color = on ? "#1a7f37" : "#9aa0a6";
      span.title = on ? "这篇已经在知识库里（切片、索引都在）"
                      : "这篇还不在知识库里";
    } else {
      const n = parseInt(String(data).replace(/^0+/, ""), 10);
      const has = !isNaN(n) && n > 0;
      span.style.color = has ? "#1f6feb" : "#9aa0a6";
      span.title = has
        ? "已生成 " + n + " 节分节纲要（中间层：比摘要详细、比全文短）"
        : "还没生成分节纲要 —— 在文献上右键 →「连接到本地模型」→"
          + "「生成分节纲要」（管理面板的「AI」页也有）";
    }
    return span;
  },
});

// ===== src/22-formula.js =====
// ===== 公式就地纠错：阅读器右键菜单 → 定位 → 阅读器列里的编辑区 → 回写 =====
//
// 设计口径（2026-10-12 用户拍板）：
//   · **不做 OCR**（本地公式识别模型试过，pix2tex 明显劣于 MinerU、PP-FormulaNet
//     只与 MinerU 打平，按预登记判据停手不落地）；
//   · 编辑方式用 **MathType 剪贴板往返**（会 MathType 的人零学习成本）；
//     未装 MathType 时按钮**不隐藏、只置灰**；
//   · 写回走既有的唯一写路径（服务端 POST /formula-apply → 修订层 + 重切重向量）。
//
// 两个右键事件的分工（真机探针实测，见 20-kbview.js 的同名注释）：
//   · createViewContextMenu        params={x,y,overlay,position}
//       position={pageIndex,rects} 是**点击处在页面上的位置**（左下原点 PDF 点）
//       —— 所以右键公式**不必先画标注**；
//   · createAnnotationContextMenu  params={ids,x,y,view}
//       ids 是标注 item key → 从 annotationPosition 拿 pageIndex/rects。
//   两者互斥：右键已有标注时**只发** annotation 那个。

ZoteroKB.kbFormulaInit = function () {
  var self = ZoteroKB;
  if (self._kbFormulaMenus) return true;
  if (!Zotero.Reader || typeof Zotero.Reader.registerEventListener !== "function") {
    return false;
  }
  self._kbFormulaMenus = function (event) {
    try { self.kbFormulaAddMenuItem(event); }
    catch (e) { Zotero.debug("[zotero-kb] 加阅读器右键菜单失败：" + e); }
  };
  try {
    Zotero.Reader.registerEventListener(
      "createViewContextMenu", self._kbFormulaMenus, self.id);
    Zotero.Reader.registerEventListener(
      "createAnnotationContextMenu", self._kbFormulaMenus, self.id);
    self.writeStatusFile({ kbFormulaMenus: "ok" });
    return true;
  } catch (e) {
    self._kbFormulaMenus = null;
    self.writeStatusFile({ kbFormulaMenus: "FAIL: " + e });
    return false;
  }
};

/** 往右键菜单里塞一项。⚠ 必须**同步** append（同工具栏那条规矩）。 */
ZoteroKB.kbFormulaAddMenuItem = function (event) {
  var self = ZoteroKB;
  if (!event || typeof event.append !== "function") return false;
  // 菜单名保持固定：不管能不能定位，先给入口，点了再由服务端说"没找到公式"。
  event.append({
    label: "知识库：修正公式",
    onCommand: function () { self.kbFormulaStart(event); },
  });
  return true;
};

/**
 * 从事件里**同步**取出「哪一篇、哪一页、哪块区域」。
 *
 * ⚠ 条目 key 必须取**顶层条目**：阅读器的 reader.itemID 是**附件**，
 *   附件 key 在知识库里永远查不到（本机踩过）。
 */
ZoteroKB.kbFormulaTargetInfo = function (event) {
  const reader = event && event.reader;
  const p = (event && event.params) || {};
  let key = "";
  try {
    const id = reader && reader.itemID;
    let item = id ? Zotero.Items.get(id) : null;
    if (item && item.parentID) item = Zotero.Items.get(item.parentID) || item;
    key = item && item.key ? item.key : "";
  } catch (e) { key = ""; }
  let page = 0;
  let rects = null;
  if (p.position && typeof p.position.pageIndex === "number") {
    page = p.position.pageIndex;
    rects = p.position.rects || null;
  }
  if (p.ids && p.ids.length) {
    try {
      const it = Zotero.Items.get(p.ids[0]);
      const pos = it && it.annotationPosition
        ? JSON.parse(it.annotationPosition) : null;
      if (pos) {
        if (typeof pos.pageIndex === "number") page = pos.pageIndex;
        rects = pos.rects || rects;
      }
    } catch (e2) { /* 用点击位置那份 */ }
  }
  return { key: key, page: page, rects: rects };
};

ZoteroKB.kbFormulaAlert = function (text) {
  var self = ZoteroKB;
  self.writeStatusFile({ kbFormulaLast: String(text).slice(0, 300) });
  let win = null;
  try { win = Zotero.getMainWindow(); } catch (e) { win = null; }
  try {
    Services.prompt.alert(win, "知识库：修正公式", String(text));
    return;
  } catch (e2) { /* 退下一档 */ }
  try { if (win && win.alert) win.alert(String(text)); } catch (e3) { /* 只能写状态文件 */ }
};

/** 右键 → 定位 → 打开阅读器列 → 把 payload 送进页面。 */
ZoteroKB.kbFormulaStart = function (event) {
  var self = ZoteroKB;
  const info = self.kbFormulaTargetInfo(event);
  let win = null;
  try { win = Zotero.getMainWindow(); } catch (e) { win = null; }
  if (!win) return;
  if (!info.key) {
    self.kbFormulaAlert("这一条不在知识库里（取不到顶层条目 key）。\n"
      + "先给这篇建知识库，再来改公式。");
    return;
  }
  let url = "/formula-target?key=" + encodeURIComponent(info.key)
    + "&page=" + Math.max(0, info.page | 0);
  if (info.rects && info.rects.length) {
    url += "&rects=" + encodeURIComponent(JSON.stringify(info.rects));
  }
  self.writeStatusFile({ kbFormulaLast: "定位 " + info.key + " p" + info.page });
  // W3 线2.5（2026-10-14）：桥优先（`formula.target`，与 HTTP `GET /formula-target`
  // 同一份实现：rects 走查询串、是 JSON 字符串），拿不到再退回老 HTTP。
  const ask = (async function () {
    try { return await self.kbAdminHostRoute("GET", url); }
    catch (e0) {
      Zotero.debug("[zotero-kb] 引擎桥定位公式失败（退回 HTTP）：" + e0);
      return await self.request("GET", url);
    }
  })();
  ask.then(function (res) {
    if (!res || res.ok !== true) {
      self.kbFormulaAlert("没找到可改的公式："
        + ((res && (res.error || res.why)) || "本机服务没返回原因"));
      return;
    }
    if (!self.kbviewIsOpen(win, "reader")) self.kbviewOpen(win, "reader");
    self.kbFormulaSendWhenReady(win, res, 0);
  }).catch(function (e2) {
    self.kbFormulaAlert("连本机服务失败：" + e2);
  });
};

/** 页面可能还没就绪，轮询着送（最多 ~10 秒）。 */
ZoteroKB.kbFormulaSendWhenReady = function (win, payload, tries) {
  var self = ZoteroKB;
  const host = self.kbviewHost(win, false);
  const dock = host && host.read;
  let sent = false;
  try { sent = dock ? self.kbviewSend(dock, { type: "formula", payload: payload }) : false; }
  catch (e) { sent = false; }
  if (sent) {
    self.writeStatusFile({ kbFormulaLast: "已送页面（" + (payload.key || "") + "）" });
    return;
  }
  if (tries > 40) {
    self.kbFormulaAlert("阅读器那一列没能就绪，没打开编辑区。\n"
      + "可以先在阅读器工具栏点一下「对照」，再右键公式。");
    return;
  }
  try {
    win.setTimeout(function () {
      self.kbFormulaSendWhenReady(win, payload, tries + 1);
    }, 250);
  } catch (e2) { /* 窗口没了就算了 */ }
};

/** 页面点了「应用」。 */
ZoteroKB.kbFormulaApply = function (win, kind, msg) {
  var self = ZoteroKB;
  const host = self.kbviewHost(win, false);
  const dock = host ? (kind === "reader" ? host.read : host.lib) : null;
  const back = function (payload) {
    if (dock) self.kbviewSend(dock, payload);
  };
  // W3 线2.5（2026-10-14）：桥优先（`formula.apply`，**写路径**：写回该页第 ordinal 条
  // $$…$$ 并复用 doc.save 的重切重向量 —— 与 HTTP `POST /formula-apply` 同一份实现）。
  const payload = { key: msg.key, page: msg.page, latex: msg.latex,
                    ordinal: msg.ordinal, old_latex: msg.old_latex };
  const send = (async function () {
    try { return await self.kbAdminHostRoute("POST", "/formula-apply", payload); }
    catch (e0) {
      Zotero.debug("[zotero-kb] 引擎桥写回公式失败（退回 HTTP）：" + e0);
      return await self.request("POST", "/formula-apply", payload);
    }
  })();
  send.then(function (res) {
    if (!res || res.error) {
      back({ type: "formulaResult", ok: false,
             error: (res && res.error) || "写回失败" });
      return;
    }
    back({ type: "formulaResult", ok: true, job: res.job || "" });
    self.kbFormulaWaitJob(win, kind, res.job || "", 0);
  }).catch(function (e) {
    back({ type: "formulaResult", ok: false, error: String(e) });
  });
};

/** 页面点了「用 MathType 编辑」：服务端放剪贴板 + 启动 MathType。 */
ZoteroKB.kbFormulaMathType = function (win, kind, msg) {
  var self = ZoteroKB;
  const host = self.kbviewHost(win, false);
  const dock = host ? (kind === "reader" ? host.read : host.lib) : null;
  // W3 线2.5（2026-10-14）：桥优先（`formula.mathtype`：本机放剪贴板 + 起 MathType），
  // 失败退回老 HTTP。`dry_run` 只在自检里用，正常路径不传。
  const askMt = (async function () {
    try { return await self.kbAdminHostRoute("POST", "/formula-mathtype", { latex: msg.latex || "" }); }
    catch (e0) {
      Zotero.debug("[zotero-kb] 引擎桥起 MathType 失败（退回 HTTP）：" + e0);
      return await self.request("POST", "/formula-mathtype", { latex: msg.latex || "" });
    }
  })();
  askMt
    .then(function (res) {
      if (dock) {
        self.kbviewSend(dock, {
          type: "formulaMathTypeResult",
          ok: !!(res && res.ok), copied: !!(res && res.copied),
          launched: !!(res && res.launched),
          error: (res && res.why) || "",
        });
      }
    }).catch(function (e) {
      if (dock) {
        self.kbviewSend(dock, { type: "formulaMathTypeResult",
                                ok: false, error: String(e) });
      }
    });
};

/** 重建是异步任务，做完再刷新右侧正文。 */
ZoteroKB.kbFormulaWaitJob = function (win, kind, jobId, tries) {
  var self = ZoteroKB;
  if (!jobId) { self.kbviewPush(win, kind, null); return; }
  // W3 线2.5：轮询也走桥（`jobs.get`，与 HTTP `GET /jobs/<id>` 同形），失败退回 HTTP。
  const poll = (async function () {
    try { return await self.kbAdminHostRoute("GET", "/jobs/" + encodeURIComponent(jobId)); }
    catch (e0) {
      return await self.request("GET", "/jobs/" + encodeURIComponent(jobId));
    }
  })();
  poll.then(function (j) {
    const st = String((j && j.state) || "");
    if (st === "done" || st === "failed" || st === "cancelled" || tries > 120) {
      self.kbviewPush(win, kind, null);
      return;
    }
    win.setTimeout(function () {
      self.kbFormulaWaitJob(win, kind, jobId, tries + 1);
    }, 1000);
  }).catch(function () {
    win.setTimeout(function () {
      self.kbFormulaWaitJob(win, kind, jobId, tries + 1);
    }, 1500);
  });
};

// ===== src/23-admin.js =====
/**
 * 23-admin.js —— 管理面板搬进 Zotero 的**宿主半**（范围 B 第一波，W1）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 *
 * ## 这一层干什么
 *
 * 一份前端（`content/admin.html` + `admin.js` + `admin-pages/*.js`）两个入口，
 * 本文件只负责 **Zotero 内的那个入口**：
 *   ① 按需拉起**无端口**的引擎子进程（`online/engine.py`，stdin/stdout 上的 NDJSON-RPC）；
 *   ② 把页面的 `postMessage` 转成引擎 RPC，再把结果转回页面；
 *   ③ 工具菜单里的一项「知识库管理…」→ 带 `<browser>` 的 Zotero 窗口。
 *
 * 设计契约见开发说明（尤其 §1/§4/§6/§7）。**不监听任何 TCP** ——
 * 页面 ↔ 宿主 ↔ 引擎全在父子进程的管道里，没有 token、没有 `Origin` 例外。
 *
 * ## ⚠⚠ 页面层的三个硬结论（2026-10-13 在**活着的 Zotero** 里用 dispatch_js 逐条实测）
 *
 * 这三条是本文件两次真机返工换来的，**改这一层之前先读完**：
 *
 * ① **页面必须跑在父进程里。** `Zotero.openInViewer(管理页面地址)` 是错的：
 *    basicViewer 自带的 `<browser>` 带 **`maychangeremoteness="true"`**，于是
 *    `resource://zotero-kb/content/admin.html` 被切进**独立进程**——
 *    实测 `b.isRemoteBrowser=true`、`b.contentWindow=null`、`b.contentDocument=null`。
 *    整套桥（装钩子 / 读 `wrappedJSObject` / 回包）都要求同进程，所以什么都做不了
 *    （上一版拿到的 `contentWindow` 只是导航前的 about:blank，钩子装在了空窗口上）。
 *   正解：`openInViewer("about:blank#zotero-kb-admin")` 开一个**壳窗口**，然后
 *   用 `kbAdminMountFrame` 在它里面**自建**一个 `<browser>`（**不设** maychangeremoteness）
 *    把页面加载进去 —— 实测 `isRemoteBrowser=false`、`contentWindow` 正常、页面能渲染。
 *    对照：`20-kbview.js` 自建的 browser 本来就没有那个属性，一直是同进程的。
 *
 * ② **这类 `<browser>` 的内容文档 `window.parent === window`（实测 `parentIsSelf:true`，
 *    且 `browsingContext.isTopLevel:false`）**。而 `content/admin.js::call()` 用的是
 *    `window.parent.postMessage(...)` —— 于是请求**先被页面自己的 message 监听吃到**：
 *    那个 seq 从 `pending` 里被删、请求里没有 `ok`，当场
 *    `reject(new Error("宿主半返回失败"))`（用户看到的就是这一句，不是 30 秒超时那句）。
 *    正解：在页面 window 上**替换 `postMessage`**（`kbAdminInstallHookOn`）——
 *    `{__kba:1, op}` 形状的请求直接交给宿主、**不再派发**；其余原样放行。
 *    （`content/kbview.js` 第 76 行 `window.parent !== window` 的判据在这里永远是 false，
 *    所以它页面→宿主走的是宿主注入的 `__kbvHostPost`，不是 postMessage。）
 *
 * ③ **回包必须用 WindowProxy 上的内建 `postMessage`，不能用 `wrappedJSObject` 上那个。**
 *    实测从 chrome 用后者发对象会 `DataCloneError: The object could not be cloned.`
 *    （Xray 包装过的 chrome 对象不可结构化克隆）；用 `browser.contentWindow.postMessage`
 *    （WindowProxy 的 WebIDL 方法）则正常。另外：页面里 `window.KBAdmin` 这类
 *    **expando 在 Xray 上看不到**（`cw.KBAdmin === undefined`），必须走
 *    `cw.wrappedJSObject.KBAdmin` —— 探针里读页面对象时记住这一条。
 *
 * 页面的窗口一律**按 URL 认**（`kbAdminEnsureDocObserver`：观察 `document-element-inserted`，
 * 在页面脚本跑之前就把钩子装上），不再靠 `querySelector("browser")` 猜是哪一个。
 *
 * ## ⚠ 关键探明结论：Zotero 的 Subprocess **拿得到 stdout 管道**（不是"只能拿退出码"）
 *
 * 计划文档 §7 风险 1 与 `14-menus.js::runPython` 的注释都写着"Subprocess 只能拿退出码、
 * stdout 是 ignore，所以 stdio 引擎可能做不了"。**直接读已装 Zotero 的实现**后确认：
 * 那句是从"`proc.stdout` 不是 Web Stream（没有 getReader）"推出来的，而**前提是错的**：
 *
 *   · `omni.ja!/modules/Subprocess.sys.mjs`（Zotero 10 / Gecko 140）只处理
 *     `options.stderr`，**根本没有 `options.stdout` 这个选项** ——
 *     传 `stdout: "ignore"` 是被**静默忽略**的，不会改变任何行为。
 *   · 真正的创建在 `omni.ja!/modules/subprocess/subprocess_win.worker.js::initPipes({stderr})`：
 *     `their_pipes[0] = pipe(false)`（子进程 stdin，我们写）与
 *     `their_pipes[1] = pipe(true)`（子进程 stdout，**我们读**）**永远都建管道**，
 *     `stderr` 这个选项只决定第三条（`"pipe"` 单独管道 / `"stdout"` 并进 stdout /
 *     其它则继承父进程的 stderr）。
 *   · 因此 `Subprocess.call(...)` 的返回值上 `proc.stdout` 是一个 **`InputPipe`**，
 *     有 `read() / readString() / readJSON() / readUint32()`（见 `subprocess_common.sys.mjs`
 *     的 `BaseProcess` 构造函数：`fds[1] !== undefined` 就挂 `this.stdout`）。
 *
 * 结论：**管道可用，stdio 引擎这条路成立**。原来那句"没有 getReader 所以读不了"只说明
 * 不能用 Web Stream 的写法，正确的读法是 `await proc.stdout.readString()`（流式解码，
 * 读到 EOF 返回空串）。本项目实测也确认 `pythonw.exe` 在**双管道**下 `sys.stdin/stdout`
 * 都不是 None（`print(repr(sys.stdout is None))` → `False`）。
 *
 * ## ⚠ 传输路径：只剩两条（2026-10-13 W2 删掉中间的"结果写文件"那条）
 *
 *   ① **长驻管道**（首选）：`kbAdminStartEngine()` 拉起一次，之后每次请求写一行 NDJSON；
 *   ② **短进程 + 单行管道**（退化）：拿不到管道时，一个请求起一次引擎、写一行、读一行。
 *
 *   原来还有一条夹在中间的 ③"短进程 + 结果写文件"（契约形态
 *   `--once --request <json> --response <json>`）。**它已删除**，理由：
 *     · `engine.py` 从来没实现过 `--once` —— 实测那条命令退出码 0、**不写响应文件**，
 *       所以它 100% 会抛"引擎没写出响应文件"，是个**永远不成功的尝试**；
 *     · 每次都要多一次临时文件往返，却不比 ② 多任何能力（同样是一个请求一个进程）；
 *     · 而 ① 与 ② **都已在真机实测可用**（见上面那段探明结论）。
 *   留一条必然失败的路径只会让真正的故障被它盖住（报错指向"没写响应文件"，
 *   而根因可能在别处），所以连同它的注释一起删掉，不留提醒性的死代码。
 *
 * ## 页面协议（`content/admin.js` 已按这个写好，**不要改前端**）
 *
 *   页面 → 宿主：`{__kba:1, seq, op, args}`（`admin.js::call`）
 *   宿主 → 页面：`{__kba:1, seq, ok, data|error}`
 *   `op` 目前只有一个：`"http"`，`args = {method, path, body}`，由 `KB_ADMIN_ROUTES`
 *   映射成引擎方法（未知路径回 `ok:false` 并说明白，不静默）。
 *
 *   `KB_ADMIN_ROUTES` 的**当前覆盖**（W1 状态/结构 + W2 批次 A 五页 + 批次 B 四页）：
 *
 *     W1   GET /health · GET /status · GET /kb-status · GET /kb-structure ·
 *          GET /jobs · GET /jobs/<id> · POST /reindex · POST /digest
 *     W2A  经验库：GET /experience · GET /experience/<id> · POST /experience ·
 *          POST /experience/delete · POST /pending/mark ·
 *          GET /sessions · POST /learn
 *          分节纲要：GET /outline-list · POST /outline/render · POST /outline/delete
 *          元数据：  POST /metafill-scan · POST /metafill-scan-cancel ·
 *          POST /typefix-scan
 *          运行环境：GET /env-check · POST /env-config · GET|POST /llm-config ·
 *          GET /models · POST /llm-test（真发一次请求的「测试连接」）·
 *          POST /service/restart
 *          提示词：  GET /prompts · POST /prompts · POST /prompts/probe
 *     W2B  分类建议：GET /papers · POST /summarize · POST /classify ·
 *          POST /apply-suggestion · POST /maintain
 *          PDF 解析：GET /mineru-status · POST /mineru/parse · POST /mineru/clear
 *          损坏查询：GET /quality-stats · POST /quality-scan ·
 *          POST /quality-repair · GET /quality-detail · GET|POST /config/figures
 *          高级：    POST /search · POST /weight · GET /weight-list ·
 *          GET /collections · POST /zotero-sync · POST /plugin ·
 *          POST /zotero-upgrade
 *
 *   契约（方法名/参数/返回形状）见开发说明（批次 A）与
 *   开发说明（批次 B）—— **两边都要改**：
 *   引擎加 method、这里加一行。`GET /quality-detail?key=` 这类查询串由精确
 *   匹配统一抽参数，不用另写带参分支。
 *
 * ⚠ **进度事件：页面壳已经能收，宿主这一侧还没转发**（2026-10-13 更新）。
 *   `content/admin.js` 现在先判 `d.event === "progress"` 再走 `onProgress`，
 *   不会再把同 seq 的进度当失败响应打掉。**但本文件仍只把进度记进日志与
 *   状态文件**（见 `kbAdminForwardProgress` 的注释：转发要把页面 seq 与 browser
 *   一路带进 pending 槽，本轮没做）。长任务页面走 `runTask` 轮询 `/jobs/<id>`，
 *   进度从 `job.log` 里照样拿得到 —— 而且长任务现在**一律"派发即返回 + 轮询"**
 *   （`kb.digest` / `kb.reindex` 也已改成这个形状，见 engine.py::_job_dispatch），
 *   所以"实时逐行"本来就不需要：这是**有意不做**，别当成漏了。
 *
 * ⚠ **超时**：页面 `get/post` 的第 4 个参数只约束**页面自己**的等待时间，
 *   管不到宿主。宿主侧默认 180s（`timeoutMs`），慢方法用 `KB_ADMIN_TIMEOUTS`
 *   单独放宽（目前只有 `prompts.probe` → 300s：它内部模型超时就是 240s，
 *   用 180s 会被宿主提前掐掉）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 路由表
  //
  // 页面只会说"HTTP 语义"（路径 + 方法），这里换成引擎方法名。
  // 为什么不把这张表塞进函数体：**换引擎方法时只改这一处**，而且"哪些端点
  // W1 已经做了"一眼可见（未知路径会被明确拒绝，不是静默 404）。
  //
  // `from` 决定参数从哪来：query 串（GET）还是请求体（POST）。
  KB_ADMIN_ROUTES: {
    // 页面顶部那句话（admin.js::boot 用 /health）与状态卡
    "GET /health": { method: "kb.health", from: "query" },
    "GET /status": { method: "kb.status", from: "query" },
    // ⚠ 状态页用的是 `/kb-status`（Tk 面板的口径），与上面的 /status 同义。
    //   两个都留着：老前端/新前端都不用改。
    "GET /kb-status": { method: "kb.status", from: "query" },
    "GET /kb-structure": { method: "kb.structure", from: "query" },
    "GET /jobs": { method: "jobs.list", from: "query" },
    "POST /reindex": { method: "kb.reindex", from: "body" },
    "POST /digest": { method: "kb.digest", from: "body" },
    // W3 线2.5（2026-10-14）：右键菜单「停止生成纲要」/ 侧栏正文修订 / 公式纠错。
    // ⚠ 这几条都是"老 HTTP 已有、引擎侧新补"的一对一映射，与老 `/formula-*` 并存。
    "POST /digest/cancel": { method: "kb.digestCancel", from: "body" },
    "GET /doc-edit": { method: "doc.get", from: "query" },
    "POST /doc-edit": { method: "doc.save", from: "body" },
    "GET /formula-target": { method: "formula.target", from: "query" },
    "POST /formula-apply": { method: "formula.apply", from: "body" },
    "POST /formula-mathtype": { method: "formula.mathtype", from: "body" },
    // `GET /jobs/<id>` 是**带参路径**，在 kbAdminMapRoute 里单独匹配（见那里）。

    // ---------------------------------------------------------- W2 批次 A
    //
    // 方法名与开发说明里的那张表一一对应（契约冻结，不要自己发明）。
    // 经验库（`GET /experience/<id>` 也是带参路径，同样在 kbAdminMapRoute 里匹配）
    "GET /experience": { method: "experience.list", from: "query" },
    "POST /experience": { method: "experience.save", from: "body" },
    "POST /experience/delete": { method: "experience.delete", from: "body" },
    "POST /pending/mark": { method: "pending.mark", from: "body" },
    // 「选择会话…」弹窗 + 从勾选的会话里抽经验（后者是长任务，走 /jobs/<id>）
    "GET /sessions": { method: "sessions.list", from: "query" },
    "POST /learn": { method: "learn.scan", from: "body" },
    // 分节纲要（`/digest` 已在上面，长任务走 /jobs/<id> 轮询）
    "GET /outline-list": { method: "outline.list", from: "query" },
    "POST /outline/render": { method: "outline.render", from: "body" },
    "POST /outline/delete": { method: "outline.delete", from: "body" },
    // 元数据（补全 / 类型污染）
    "POST /metafill-scan": { method: "meta.fillScan", from: "body" },
    "POST /metafill-scan-cancel": { method: "meta.fillCancel", from: "body" },
    "POST /typefix-scan": { method: "meta.typeScan", from: "body" },
    // 运行环境（/health 复用上面那条）
    "GET /env-check": { method: "env.check", from: "query" },
    // W3 线2.5（2026-10-14）：插件设置面板要的是**诊断原文**（= env_report() 那个形状，
    // 带 python / project_root / runtime_state 顶层键）；管理面板仍用上面那个重整形过的。
    "GET /env-check-raw": { method: "env.checkRaw", from: "query" },
    // W3 线2.5：**内联元数据**的分类建议（新文献还没入库时用）。
    // ⚠ 与 `POST /classify`（→ ai.classify，认 key）是**两个能力**，并存不冲突。
    "POST /classify-item": { method: "classify.item", from: "body" },
    "POST /env-config": { method: "env.save", from: "body" },
    "GET /llm-config": { method: "llm.config", from: "query" },
    "POST /llm-config": { method: "llm.save", from: "body" },
    "GET /models": { method: "llm.models", from: "query" },
    // 「测试连接」：**真发一次请求**（本地查 ollama list；外部 API 真调一次
    // /chat/completions）—— 慢，超时在 KB_ADMIN_TIMEOUTS 里单独放宽到 90s。
    "POST /llm-test": { method: "llm.test", from: "body" },
    // 「模型目录」三层兜底：本机 /api/tags、外部 /models、失败回落内置候选。
    // 引擎侧自己只等 12s（`discover_llm` 的 timeout 参数），这里给 30s。
    "POST /llm-discover": { method: "llm.discover", from: "body" },
    "POST /service/restart": { method: "service.restart", from: "body" },
    // 提示词
    "GET /prompts": { method: "prompts.list", from: "query" },
    "POST /prompts": { method: "prompts.save", from: "body" },
    "POST /prompts/probe": { method: "prompts.probe", from: "body" },

    // ---------------------------------------------------------- W2 批次 B
    //
    // 方法名与开发说明里的那张表一一对应（契约冻结，不要自己发明）。
    // `GET /quality-detail?key=`、`GET /mineru-status` 这些**查询串已被上面的
    // 精确匹配统一抽成 params**（kbAdminMapRoute 会解析 `?a=b` 再交给引擎），
    // 所以不需要另写带参匹配分支。
    // 分类建议
    "GET /papers": { method: "papers.list", from: "query" },
    "POST /summarize": { method: "ai.summarize", from: "body" },
    "POST /classify": { method: "ai.classify", from: "body" },
    "POST /apply-suggestion": { method: "ai.apply", from: "body" },
    "POST /maintain": { method: "maintain.run", from: "body" },
    // PDF 解析
    "GET /mineru-status": { method: "mineru.status", from: "query" },
    // W3 线2.5（2026-10-14）：安装引导要的**原文**（带 exe/version/vlm_engine/cuda/gpu…），
    // 与上面那个给面板的汇总形状**不是同一个东西**。
    "GET /mineru-check": { method: "mineru.checkRaw", from: "query" },
    // 单篇**同步**的元数据补全 / 类型修正（与 /metafill-scan、/typefix-scan 的长任务分工不同）。
    "POST /metafill": { method: "meta.fillOne", from: "body" },
    "POST /typefix": { method: "meta.typefixOne", from: "body" },
    "POST /mineru/parse": { method: "mineru.parse", from: "body" },
    "POST /mineru/clear": { method: "mineru.clear", from: "body" },
    // 损坏查询
    "GET /quality-stats": { method: "quality.stats", from: "query" },
    "POST /quality-scan": { method: "quality.scan", from: "body" },
    "POST /quality-repair": { method: "quality.repair", from: "body" },
    "GET /quality-detail": { method: "quality.detail", from: "query" },
    // 图注/表格开关：**GET 读当前值**（页面那个勾选框必须显示真值，不能瞎勾），
    // POST 写 —— 同一个路径两种方法，与 /llm-config 的写法一致。
    "GET /config/figures": { method: "config.figuresGet", from: "query" },
    "POST /config/figures": { method: "config.figures", from: "body" },
    // 高级（`POST /search`、`POST /weight` 批次 A 没有，本轮补上）
    "POST /search": { method: "kb.search", from: "body" },
    "POST /weight": { method: "kb.weight", from: "body" },
    // W3 线2.5：插件自身的**列表权重列**走这条（`GET /weights` = 全量映射，每篇都有数）。
    // ⚠ 别拿下面的 `/weight-list`（advanced.weightList）顶替：那个是"权重榜"，
    //   只列 `item_weight` 里有行的几篇，拿它填列会让其余全空白（历史真实踩坑）。
    "GET /weights": { method: "kb.weightsMap", from: "query" },
    // W3 线2.5：文献列表那两列（已建库 / 纲要节数）。
    // ⚠ 节数口径 = `meta.ai_outline:<key>`（读端看的），**不是数 views 里的文件个数**。
    "GET /col-status": { method: "kb.colStatus", from: "query" },
    // W3 线2.5：某几条文献在库状态（06-watch 监听 / 12-dsh 发文献给 DSH 共用）。
    "POST /item-info": { method: "kb.itemInfo", from: "body" },
    "GET /weight-list": { method: "advanced.weightList", from: "query" },
    "GET /collections": { method: "advanced.collections", from: "query" },
    "POST /zotero-sync": { method: "advanced.zoteroSync", from: "body" },
    "POST /plugin": { method: "advanced.plugin", from: "body" },
    "POST /zotero-upgrade": { method: "advanced.upgrade", from: "body" },
  },

  // ================================================================ 引擎：状态与生命周期

  /**
   * 引擎的运行时状态（**挂在一个对象里**，不要往 self 上撒几十个字段）。
   *
   *   proc        当前管道引擎的句柄（null = 没起 / 已退）
   *   mode        "pipe"（长驻）/"file"（短进程）/"none"（起不来）
   *   nextId      NDJSON-RPC 的请求 id（与页面的 seq 是两套编号，别混）
   *   pending     Map<id, {resolve,reject,timer,method}> —— 等响应的请求
   *   buffer      stdout 的**行缓冲**（chunk 不保证按行切）
   *   errTail     stderr 的尾巴（几千字符，"引擎为什么崩了"就靠它）
   *   failCount   pipe 引擎连续挂了几次（≥3 就固定走退化路径，别反复重启）
   *   forceFile   是否固定走退化路径
   *   timeoutMs   单条 RPC 的默认超时（180s）。**慢方法用它不够** —— 见
   *              `KB_ADMIN_TIMEOUTS` / `kbAdminTimeoutFor()`（按方法名放宽）。
   */
  kbAdminEngineState: function () {
    var self = ZoteroKB;
    if (!self._kbAdminEngine) {
      self._kbAdminEngine = {
        proc: null, mode: "none", starting: null, dead: false,
        nextId: 0, pending: null, buffer: "", errTail: "",
        failCount: 0, forceFile: false,
        timeoutMs: 180000, startedAt: 0, reason: "", lastProgress: "",
      };
    }
    if (!self._kbAdminEngine.pending) {
      try { self._kbAdminEngine.pending = new Map(); }
      catch (e) { self._kbAdminEngine.pending = null; }
    }
    return self._kbAdminEngine;
  },

  /**
   * 引擎的可执行文件。
   *
   * ⚠ 这里**故意用 python.exe 而不是 pythonw.exe**（`pythonConsoleExe()` 会把
   *   `pythonw.exe` 换成同目录的 `python.exe`）：
   *   · Zotero 的 Subprocess 本来就是 `CREATE_NO_WINDOW` 启动（见
   *     `subprocess_win.worker.js` 的 `processFlags`），**不会弹控制台** ——
   *     pythonw 唯一的那点好处在这里根本用不上；
   *   · 而引擎作者在 `online/engine.py` 开头明确写了"不要用 pythonw 启动"
   *     （它担心 `sys.stdout is None` 时协议直接废掉）。本机实测双管道下
   *     pythonw 的 stdin/stdout 其实不是 None，但既然没有收益，就不去赌这一点。
   */
  kbAdminEngineExe: function () {
    var self = ZoteroKB;
    try {
      const exe = self.pythonConsoleExe();
      if (exe) return exe;
    } catch (e) { /* 落到 pythonExe */ }
    return self.pythonExe();
  },

  /** 引擎脚本的绝对路径（项目根下 online/engine.py）。找不到返回空串。 */
  kbAdminEngineScript: function () {
    var self = ZoteroKB;
    const root = self.projectRoot();
    if (!root) return "";
    const p = root + "\\online\\engine.py";
    return self._exists(p) ? p : "";
  },

  /**
   * Zotero 退出时把引擎收掉（**不常驻**是用户的第一诉求之一）。
   *
   * 为什么自己挂 `quit-application` 而不改 `01-lifecycle.js::shutdown`：
   *   · 这一波只许动 23-admin / 14-menus / structure 三个文件；
   *   · `quit-application` 才是"真的要退了"（`quit-application-requested` 是可以
   *     被别的观察者取消的**询问**，`verify_bootstrap.js` 也明确不许再挂那个 topic）。
   *
   * 引擎自己是"stdin EOF 就退出"（engine.py::main），所以先关 stdin 让它体面收工，
   * 再兜一个 kill（关不掉也不能留孤儿进程）。
   */
  kbAdminEnsureQuitHook: function () {
    var self = ZoteroKB;
    if (self.kbAdminQuitObs) return;
    try {
      const obs = {
        observe: function (subject, topic) {
          if (topic !== "quit-application") return;
          try { ZoteroKB.kbAdminStop(); } catch (e) { /* 退出流程里不敢抛 */ }
        },
      };
      Services.obs.addObserver(obs, "quit-application");
      self.kbAdminQuitObs = obs;
    } catch (e) {
      Zotero.debug("[zotero-kb] 挂 quit-application 观察者失败：" + e);
    }
  },

  /** 收掉引擎进程（幂等）。返回"这次真的收了一个没有"。 */
  kbAdminStop: function () {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const proc = st.proc;
    st.proc = null;
    st.dead = true;
    if (!proc) return false;
    // ① 关 stdin：engine.py 的主循环读不到行就正常退出（体面路径）
    try {
      if (proc.stdin && typeof proc.stdin.close === "function") proc.stdin.close();
    } catch (e) { /* ignore */ }
    // ② 兜底：给它 200ms 收尾，然后强制结束（Windows 上是 TerminateProcess）
    try {
      if (proc.kill) proc.kill(200);
    } catch (e2) { Zotero.debug("[zotero-kb] 杀引擎失败：" + e2); }
    Zotero.debug("[zotero-kb] 引擎已收掉");
    return true;
  },

  /**
   * 按需拉起引擎（**幂等**：已经在跑就返回同一个句柄；正在起就复用那个 Promise）。
   *
   * 返回 Promise<proc|null>。null 表示"管道这条路走不通"，调用方应当走退化路径
   * （`kbAdminRpcOnce`），而不是把整页打不开。
   */
  kbAdminStartEngine: function () {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    if (st.proc && !st.dead) return Promise.resolve(st.proc);
    if (st.starting) return st.starting;
    st.starting = (async function () {
      try {
        const exe = self.kbAdminEngineExe();
        const script = self.kbAdminEngineScript();
        const root = self.projectRoot();
        if (!exe || !self._exists(exe) || !script || !root) {
          st.reason = "找不到引擎或 Python（项目目录/venv 还没配好？）";
          st.mode = "none";
          self.writeStatusFile({ kbAdminEngine: "FAIL: " + st.reason });
          return null;
        }
        const { Subprocess } = ChromeUtils.importESModule(
          "resource://gre/modules/Subprocess.sys.mjs");
        // ⚠ 不用传 stdout：Zotero 这版**没有** options.stdout，stdin/stdout 永远
        //   会建成管道（探明结论见文件头）。stderr 显式要 "pipe" 是为了把引擎日志
        //   拿到手（排错全靠它），并在这里起一个读泵——**不读的话 64KB 管道写满，
        //   引擎会卡死在写日志上**（这是管道编程最容易埋的雷）。
        const proc = await Subprocess.call({
          command: exe,
          arguments: ["-X", "utf8", script],
          workdir: root,
          environment: { PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
          environmentAppend: true,        // 追加到 Zotero 自己的环境上（别把 PATH 冲掉）
          stderr: "pipe",
        });
        if (!proc || !proc.stdout
            || typeof proc.stdout.readString !== "function") {
          st.reason = "拿不到 stdout 管道（proc.stdout 不是 InputPipe）";
          st.mode = "file";
          try { if (proc && proc.kill) proc.kill(0); } catch (e2) { /* ignore */ }
          self.writeStatusFile({ kbAdminEngine: "file-fallback: " + st.reason });
          return null;
        }
        st.proc = proc;
        st.mode = "pipe";
        st.dead = false;
        st.buffer = "";
        st.errTail = "";
        st.startedAt = Date.now();
        st.reason = "";
        self.kbAdminReadLoop(proc);
        self.kbAdminReadErrLoop(proc);
        self.kbAdminEnsureQuitHook();
        self.writeStatusFile({ kbAdminEngine: "pipe: pid=" + (proc.pid || "?")
                              + " exe=" + exe });
        return proc;
      } catch (e) {
        st.reason = String((e && e.message) || e);
        st.mode = "file";
        self.writeStatusFile({ kbAdminEngine: "FAIL→file: " + st.reason });
        return null;
      } finally {
        st.starting = null;
      }
    })();
    return st.starting;
  },

  /** 管道断了 / 引擎退了：把等在身上的请求全部拒绝，并决定要不要转退化路径。 */
  kbAdminEngineGone: function (proc, why) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    if (st.proc && st.proc !== proc) return;   // 已经被别人换掉了
    st.proc = null;
    st.dead = true;
    st.failCount = (st.failCount || 0) + 1;
    // 连挂三次就别再重启了：改走短进程（每次都冷启动，但至少能用）
    if (st.failCount >= 3) st.forceFile = true;
    const err = new Error("引擎进程没了（" + why + "）"
      + (st.errTail ? ("\n引擎日志尾巴：\n" + String(st.errTail).slice(-600)) : ""));
    const pend = st.pending;
    if (pend) {
      const ids = [];
      try { pend.forEach(function (v, k) { ids.push(k); }); } catch (e) { /* ignore */ }
      for (const id of ids) {
        const slot = pend.get(id);
        if (!slot) continue;
        pend.delete(id);
        try { if (slot.timer) clearTimeout(slot.timer); } catch (e2) { /* ignore */ }
        try { slot.reject(err); } catch (e3) { /* ignore */ }
      }
    }
    self.writeStatusFile({ kbAdminEngine: "gone: " + why
                          + "（failCount=" + st.failCount + "）" });
  },

  /**
   * 读 stdout 的行泵。
   *
   * ⚠ 用 `readString()`（InputPipe 的流式解码）：chunk 不保证按行切，
   *   所以必须自己维护行缓冲（st.buffer），拿到完整一行才解析。
   */
  kbAdminReadLoop: function (proc) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const consume = function () {
      let idx;
      while ((idx = st.buffer.indexOf("\n")) >= 0) {
        const line = st.buffer.slice(0, idx).replace(/\r$/, "");
        st.buffer = st.buffer.slice(idx + 1);
        if (line.trim()) self.kbAdminOnEngineLine(line);
      }
    };
    const pump = function () {
      Promise.resolve(proc.stdout.readString()).then(function (chunk) {
        if (chunk) {
          st.buffer += chunk;
          consume();
          pump();
          return;
        }
        // EOF：引擎退出了。残留的最后一行（没有换行结尾）也要处理掉。
        if (st.buffer.trim()) {
          const tail = st.buffer.replace(/\r$/, "");
          st.buffer = "";
          self.kbAdminOnEngineLine(tail);
        }
        self.kbAdminEngineGone(proc, "stdout 结束");
      }, function (e) {
        self.kbAdminEngineGone(proc, "读 stdout 失败：" + e);
      });
    };
    pump();
  },

  /** 把 stderr 的尾巴攒起来（引擎日志就是排错现场）。 */
  kbAdminReadErrLoop: function (proc) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    if (!proc.stderr || typeof proc.stderr.readString !== "function") return;
    const pump = function () {
      Promise.resolve(proc.stderr.readString()).then(function (chunk) {
        if (!chunk) return;
        st.errTail = (st.errTail + chunk).slice(-6000);
        // 只把前若干字符送进 Zotero 调试日志（整段可能很长）
        Zotero.debug("[zotero-kb] engine: " + String(chunk).slice(0, 1500));
        pump();
      }, function (e) {
        Zotero.debug("[zotero-kb] 读引擎 stderr 失败：" + e);
      });
    };
    pump();
  },

  /** 一条来自引擎的 NDJSON 行：要么是最终响应，要么是 progress 事件。 */
  kbAdminOnEngineLine: function (line) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    let msg = null;
    try { msg = JSON.parse(line); } catch (e) {
      Zotero.debug("[zotero-kb] 引擎输出了非 JSON 的一行：" + String(line).slice(0, 240));
      return;
    }
    if (!msg || msg.id == null) return;      // 协议占位（例如坏 JSON 的 id:null 回执）
    const pend = st.pending;
    if (!pend) return;
    const slot = pend.get(msg.id);
    if (!slot) return;
    if (msg.event) { self.kbAdminForwardProgress(slot, msg); return; }
    if (slot.timer) clearTimeout(slot.timer);
    pend.delete(msg.id);
    if (msg.ok) {
      try { slot.resolve(msg.data); }
      catch (e) { Zotero.debug("[zotero-kb] 处理引擎响应失败：" + e); }
    } else {
      slot.reject(new Error(String(msg.error || "引擎返回失败")));
    }
  },

  /**
   * 进度事件（`{event:"progress", data:{job,percent,message}}`）。
   *
   * ⚠ **目前只落到 Zotero 日志 + 状态文件，没有 postMessage 给页面** —— 但
   *   `content/admin.js` **已经改好**能收进度了（它先判 `d.event === "progress"`
   *   就交给 `onProgress`，不会再把同 seq 的进度当失败响应打掉）。所以差的只是
   *   宿主这一侧的转发，而转发需要知道该进度属于**页面的哪个 seq**：
   *     · 现在的 `slot` 只存了 `method`/`resolve`/`reject`，没有 seq，也没有 browser；
   *     · 要接的话得把 `d.seq`（`kbAdminOnPageMessage` 里有）与 browser 一路
   *       带进 `kbAdminRpc` → `kbAdminRpcPipe` 的 pending 槽，再在这里
   *       `self.kbAdminPost(browser, {__kba:1, seq, event:"progress", data})`。
   *   本轮（W2 批次 A 的引擎方法）**没做这一步**，而且**用户已明确这轮不做**：
   *   长任务一律"派发即返回 + 轮询 `/jobs/<id>`"，`job.log` 里本来就有每一行
   *   进度，页面 `runTask` 够用；阻塞式（W1 形状）已经不存在了，所以"实时逐行"
   *   就更不需要。**这是有意的决定，不是遗漏**（引擎 `_job_dispatch` 里也写了同一句）。
   */
  kbAdminForwardProgress: function (slot, msg) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const data = (msg && msg.data) || {};
    const text = String(data.message || data.percent || "");
    st.lastProgress = text.slice(0, 200);
    Zotero.debug("[zotero-kb] 引擎进度（" + (slot.method || "?") + "）：" + text);
    try {
      self.writeStatusFile({ kbAdminProgress: text.slice(0, 200) });
    } catch (e) { /* 进度写不进去不该影响主流程 */ }
  },

  // ================================================================ 引擎：RPC

  /**
   * 慢方法 → 宿主侧等多久（毫秒）。默认用 state.timeoutMs（180s）。
   *
   * 为什么要这张表：页面 `get/post` 的第 4 个参数（见 content/admin.js 的 call）
   * **只约束页面自己的等待**，它连 postMessage 都不带过去 —— 宿主这一侧如果
   * 还是 180s，`prompts.probe` 这种内部模型超时 240s 的请求会被宿主先掐掉，
   * 页面拿到的是"引擎 180 秒没回响应"，而它其实还在跑。
   *
   * ⚠ 只放**确实超过 180s** 的方法；其余别加（等太久只会让真卡住时更难发现）。
   *   · prompts.probe 真调模型，offline/prompts.py 自己给的是 240s → 300s。
   *   · advanced.plugin / advanced.upgrade（批次 B）会重打 xpi / 备份
   *     zotero.sqlite，契约给的就是 300s → 300s。
   *   · advanced.zoteroSync（批次 B）契约给的正好是 180s，写进来是**显式**
   *     对齐（值等于默认，不会放宽也不会收紧）。
   */
  KB_ADMIN_TIMEOUTS: {
    "prompts.probe": 300000,
    "advanced.plugin": 300000,
    "advanced.upgrade": 300000,
    "advanced.zoteroSync": 180000,
    // 引擎侧只等 45s（`llm.test` 的 timeout 参数），这里给 90s 留出余量：
    // 页面那侧的等待（第 4 个参数）也给了 90000，两边别互相掐。
    "llm.test": 90000,
    // 模型目录探测：引擎侧最多 12s（params.timeout），这里给 30s。
    "llm.discover": 30000,
  },

  /** 这条 RPC 宿主侧等多久。 */
  kbAdminTimeoutFor: function (method) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const per = self.KB_ADMIN_TIMEOUTS[String(method)];
    return per || st.timeoutMs || 180000;
  },

  /** 造一条引擎请求（id 自己递增，与页面的 seq 无关）。 */
  kbAdminNewRequest: function (method, params) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    st.nextId = (st.nextId || 0) + 1;
    return { id: st.nextId, method: String(method), params: params || {} };
  },

  /**
   * 发一条 RPC。第一优先是**长驻管道**，拿不到管道就退到"每次请求起一次短进程"。
   * 返回 Promise<data>（失败 reject，message 是人能看懂的）。
   */
  kbAdminRpc: function (engineMethod, params) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const req = self.kbAdminNewRequest(engineMethod, params);
    if (st.forceFile) return self.kbAdminRpcOnce(req);
    return self.kbAdminStartEngine().then(function (proc) {
      if (!proc) return self.kbAdminRpcOnce(req);
      return self.kbAdminRpcPipe(proc, req).catch(function (e) {
        // 引擎正好在这次请求中途挂了：**纯读**方法用短进程重试一遍（用户不该
        // 因为一次进程崩溃就重开页面）；但**会派活 / 会写库**的方法重试一次
        // 可能做两遍（跑两遍模型、多写一条经验），所以直接把错误抛上去。
        // ⚠ 这张名单要跟着"有副作用的方法"一起长大（W2 批次 A 又加了三个）。
        if (!st.dead) throw e;
        if (req.method === "kb.reindex" || req.method === "kb.digest"
            || req.method === "meta.fillScan" || req.method === "learn.scan"
            || req.method === "experience.save") throw e;
        Zotero.debug("[zotero-kb] 管道引擎挂了，用短进程重试一次：" + req.method);
        return self.kbAdminRpcOnce(req);
      });
    });
  },

  /** 管道模式：写一行请求，等同 id 的最终响应。 */
  kbAdminRpcPipe: function (proc, req) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    return new Promise(function (resolve, reject) {
      const pend = st.pending;
      if (!pend) { reject(new Error("内部状态丢了（pending）")); return; }
      let timer = null;
      if (typeof setTimeout === "function") {
        const waitMs = self.kbAdminTimeoutFor(req.method);
        timer = setTimeout(function () {
          pend.delete(req.id);
          reject(new Error("引擎 " + Math.round(waitMs / 1000)
            + " 秒没回响应（" + req.method + "）"));
        }, waitMs);
      }
      pend.set(req.id, { resolve: resolve, reject: reject, timer: timer,
                         method: req.method });
      let line = "";
      try { line = JSON.stringify(req) + "\n"; }
      catch (e) {
        if (timer) clearTimeout(timer);
        pend.delete(req.id);
        reject(e);
        return;
      }
      try {
        const w = proc.stdin.write(line);
        // OutputPipe.write 返回 Promise；失败了要接住，否则是未处理拒绝。
        if (w && typeof w.then === "function") {
          w.then(null, function (e) {
            Zotero.debug("[zotero-kb] 写引擎 stdin 失败：" + e);
          });
        }
      } catch (e2) {
        if (timer) clearTimeout(timer);
        pend.delete(req.id);
        reject(e2);
      }
    });
  },

  /**
   * 退化路径入口：拿不到长驻管道时，**短进程 + 单行管道**兜底。
   *
   * ⚠ 这里原来有"两层"：先试"短进程 + 结果写文件"（契约形态
   *   `--once --request <json> --response <json>`），失败了才落到单行管道。
   *   **那一层已删除**（2026-10-13 W2）：`engine.py` 从来没实现 `--once`，
   *   实测那条命令退出码 0、**不写响应文件**，所以第一层 100% 抛"引擎没写出
   *   响应文件"—— 它除了每次多一次临时文件往返、还把真正的故障信息盖住之外，
   *   没有任何作用。留下短进程管道这条（真机实测可用），退化链更短也更好查。
   */
  kbAdminRpcOnce: async function (req) {
    var self = ZoteroKB;
    return await self.kbAdminRpcOncePipe(req);
  },

  /**
   * 短进程 + 单行管道：起一个引擎、写一行请求、读到同 id 的响应、关掉 stdin 让它退出。
   * 这是**今天就能用**的退化形态（引擎不需要任何新参数）。
   */
  kbAdminRpcOncePipe: async function (req) {
    var self = ZoteroKB;
    const exe = self.kbAdminEngineExe();
    const script = self.kbAdminEngineScript();
    const root = self.projectRoot();
    if (!exe || !script || !root) throw new Error("找不到引擎或 Python");
    const { Subprocess } = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs");
    const proc = await Subprocess.call({
      command: exe,
      arguments: ["-X", "utf8", script],
      workdir: root,
      environment: { PYTHONIOENCODING: "utf-8" },
      environmentAppend: true,
      stderr: "pipe",
    });
    if (!proc || !proc.stdout || typeof proc.stdout.readString !== "function") {
      try { if (proc && proc.kill) proc.kill(0); } catch (e) { /* ignore */ }
      throw new Error("短进程也拿不到 stdout 管道");
    }
    const errBox = { text: "" };
    self.kbAdminDrain(proc.stderr, errBox);
    try {
      const w = proc.stdin.write(JSON.stringify(req) + "\n");
      if (w && typeof w.then === "function") w.then(null, function () {});
    } catch (e2) {
      try { proc.kill(0); } catch (e3) { /* ignore */ }
      throw e2;
    }
    const waitMs = self.kbAdminTimeoutFor(req.method);
    const deadline = Date.now() + waitMs;
    let buf = "";
    for (;;) {
      if (Date.now() > deadline) {
        try { proc.kill(0); } catch (e4) { /* ignore */ }
        throw new Error("短进程 " + Math.round(waitMs / 1000)
          + " 秒没回响应（" + req.method + "）"
          + (errBox.text ? ("\n引擎日志：\n" + String(errBox.text).slice(-600)) : ""));
      }
      let chunk = "";
      try { chunk = await proc.stdout.readString(); }
      catch (e5) { chunk = ""; }
      if (!chunk) break;                       // EOF
      buf += chunk;
      let idx;
      let hit = null;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).replace(/\r$/, "");
        buf = buf.slice(idx + 1);
        let m = null;
        try { m = JSON.parse(line); } catch (e6) { m = null; }
        if (m && m.id === req.id) { hit = m; break; }
      }
      if (hit) {
        try {
          if (proc.stdin && proc.stdin.close) proc.stdin.close();
        } catch (e7) { /* ignore */ }
        try { proc.kill(200); } catch (e8) { /* ignore */ }
        if (hit.ok) return hit.data;
        throw new Error(String(hit.error || "引擎返回失败"));
      }
    }
    try { proc.kill(0); } catch (e9) { /* ignore */ }
    throw new Error("短进程在响应前就退出了（" + req.method + "）"
      + (errBox.text ? ("\n引擎日志：\n" + String(errBox.text).slice(-600)) : ""));
  },

  /** 把一条管道读到 EOF，内容攒进 box.text（用于短进程的 stderr/诊断）。 */
  kbAdminDrain: function (pipe, box) {
    if (!pipe || typeof pipe.readString !== "function") return;
    const pump = function () {
      Promise.resolve(pipe.readString()).then(function (chunk) {
        if (!chunk) return;
        box.text = (box.text + chunk).slice(-6000);
        pump();
      }, function (e) {
        Zotero.debug("[zotero-kb] 读管道失败：" + e);
      });
    };
    pump();
  },

  // ⓘ 原来这里还有一个 `kbAdminTempDir()`（给"结果写文件"那条路径准备临时目录）。
  //   那条路径 2026-10-13 已删除（engine.py 从来没有 `--once`），临时目录也就
  //   没有使用方了 —— 一并删掉，不留没人调的成员。

  // ================================================================ 路由与派发

  /** `a=1&b=2` → `{a:"1", b:"2"}`（自己写：沙箱里 URLSearchParams 不保证有）。 */
  kbAdminParseQuery: function (query) {
    const out = {};
    const text = String(query == null ? "" : query);
    if (!text) return out;
    for (const part of text.split("&")) {
      if (!part) continue;
      const eq = part.indexOf("=");
      const k = eq < 0 ? part : part.slice(0, eq);
      const v = eq < 0 ? "" : part.slice(eq + 1);
      let key = k, val = v;
      try { key = decodeURIComponent(k); } catch (e) { /* 用原文 */ }
      try { val = decodeURIComponent(v.replace(/\+/g, " ")); } catch (e2) { /* 用原文 */ }
      out[key] = val;
    }
    return out;
  },

  /**
   * 页面说的"HTTP 请求" → 引擎方法 + 参数。返回 `{method, params}` 或 null（未知）。
   *
   * ⚠ 未知路径**不猜**：调用方会回一条"W1 还没有这条映射"的 `ok:false`，
   *   页面上看得见、日志里也看得见（比静默 404 好）。
   */
  kbAdminMapRoute: function (method, path, body) {
    var self = ZoteroKB;
    const m = String(method || "GET").toUpperCase();
    const raw = String(path == null ? "" : path);
    const qi = raw.indexOf("?");
    const query = qi < 0 ? "" : raw.slice(qi + 1);
    let only = qi < 0 ? raw : raw.slice(0, qi);
    if (only && only.charAt(0) !== "/") only = "/" + only;
    const hit = self.KB_ADMIN_ROUTES[m + " " + only];
    if (hit) {
      if (hit.from === "body") {
        return { method: hit.method,
                 params: (body && typeof body === "object") ? body : {} };
      }
      return { method: hit.method, params: self.kbAdminParseQuery(query) };
    }
    // 带参路径：GET /jobs/<id> → jobs.get（id 从**路径**取，不从 query）
    const jobMatch = /^\/jobs\/([^\/]+)$/.exec(only);
    if (jobMatch && m === "GET") {
      let id = jobMatch[1];
      try { id = decodeURIComponent(id); } catch (e) { /* 用原文 */ }
      const params = self.kbAdminParseQuery(query);
      params.id = id;
      return { method: "jobs.get", params: params };
    }
    // 带参路径：GET /experience/<id> → experience.get（同上，id 从路径取）。
    // ⚠ 必须在 `GET /experience`（精确匹配）之后 —— 精确匹配先命中就走了，
    //   这条只接 `/experience/12` 这种；顺序写反不会出错（键名不同），
    //   但读起来容易误解，所以留这句。
    const expMatch = /^\/experience\/([^\/]+)$/.exec(only);
    if (expMatch && m === "GET") {
      let id = expMatch[1];
      try { id = decodeURIComponent(id); } catch (e) { /* 用原文 */ }
      return { method: "experience.get", params: { id: id } };
    }
    return null;
  },

  /** 页面的一条 `op:"http"` 请求 → `{ok, data|error}`。 */
  kbAdminHandleRequest: async function (browser, msg) {
    var self = ZoteroKB;
    if (msg.op !== "http") {
      return { ok: false, error: "宿主半不认识这个 op：" + String(msg.op) };
    }
    const args = msg.args || {};
    const route = self.kbAdminMapRoute(args.method, args.path, args.body);
    if (!route) {
      return { ok: false,
               error: "W1 还没有这条路径的映射：" + String(args.method || "GET")
                 + " " + String(args.path || "")
                 + "（映射表在 src/23-admin.js 的 KB_ADMIN_ROUTES）" };
    }
    try {
      const data = await self.kbAdminRpc(route.method, route.params);
      return { ok: true, data: data };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },

  // ================================================================ 页面窗口与桥

  /** 管理页面的地址（必须走 resource://，理由见 20-kbview.js 的 KB_RES_KEY）。 */
  kbAdminPageURL: function () {
    var self = ZoteroKB;
    const key = self.KB_RES_KEY || "zotero-kb";
    const rel = "content/admin.html";
    return "resource://" + key + "/" + rel;
  },

  /** 已经开着的管理窗口（Zotero 用 `viewerOriginalURI` 认，见 Zotero.openInViewer）。 */
  kbAdminFindWindow: function (url) {
    try {
      const e = Services.wm.getEnumerator("zotero:basicViewer");
      while (e.hasMoreElements()) {
        const w = e.getNext();
        try { if (w.viewerOriginalURI === url) return w; } catch (err) { /* 下一个 */ }
      }
    } catch (e2) { /* 拿不到窗口枚举就算了 */ }
    return null;
  },

  /**
   * 我们那个"壳窗口"的地址（**不是**管理页面的地址）。
   *
   * ⚠ 为什么不让 `Zotero.openInViewer` 直接加载管理页面 —— 第一版就是这么做的，也因此
   *   彻底失败。真机实测（2026-10-13，Zotero 10.0.6 / Gecko 140）：
   *     basicViewer 自带的那个 `<browser>` 带 **`maychangeremoteness="true"`**，
   *     于是 `resource://zotero-kb/content/admin.html` 被切到**独立进程**：
   *       `b.isRemoteBrowser = true`、`b.contentWindow = null`、`b.contentDocument = null`，
   *     而我们的整套桥（装 postMessage 钩子 / 读 `wrappedJSObject` / 回包）全都要求页面
   *     **在父进程里**。所以之前 `contentWindow` 只能读到导航前的 about:blank，
   *     钩子装在了那个空窗口上（`kbAdminProbe` 里 `url:"about:blank"` 就是这么来的）。
   *   对照：侧栏 `20-kbview.js` 自建的那个 `<browser>` **没有** maychangeremoteness，
   *   真机实测 `isRemoteBrowser=false`、`contentWindow` 正常 —— 同一台机器、同一个 scheme。
   *   因此：壳窗口只加载 about:blank，管理页面由**我们自己建的 browser** 加载
   *   （见 kbAdminMountFrame，里面绝不设 maychangeremoteness）。
   */
  kbAdminShellURI: function () { return "about:blank#zotero-kb-admin"; },

  /** 已经开着的壳窗口（按 `viewerOriginalURI` 认，与 Zotero 自己的幂等口径一致）。 */
  kbAdminFindShellWindow: function () {
    var self = ZoteroKB;
    const want = self.kbAdminShellURI();
    try {
      const e = Services.wm.getEnumerator("zotero:basicViewer");
      while (e.hasMoreElements()) {
        const w = e.getNext();
        try { if (String(w.viewerOriginalURI) === want) return w; } catch (err) { /* 下一个 */ }
      }
    } catch (e2) { /* 拿不到窗口枚举就算了 */ }
    return null;
  },

  /**
   * 在壳窗口里建**我们自己的** `<browser>` 并加载管理页面（幂等）。
   * 建的时候**绝不设 maychangeremoteness**（理由见 kbAdminShellURI）。返回那个 browser。
   */
  kbAdminMountFrame: function (win) {
    var self = ZoteroKB;
    if (!win) return null;
    let doc = null;
    try { doc = win.document; } catch (e) { doc = null; }
    if (!doc) return null;
    let frame = null;
    try { frame = doc.getElementById("zotero-kb-admin-frame"); } catch (e2) { frame = null; }
    if (frame && frame.getAttribute && frame.getAttribute("src")) return frame;
    if (!frame) {
      let host = null;
      try {
        host = doc.getElementById("appcontent") || doc.getElementById("browser")
          || doc.documentElement;
      } catch (e3) { host = null; }
      if (!host) return null;
      // 壳窗口自带那个 browser（里面是 about:blank#…）藏起来，别两页叠着
      try {
        const stock = doc.querySelector("browser");
        if (stock) {
          stock.setAttribute("hidden", "true");
          stock.setAttribute("collapsed", "true");
        }
      } catch (e4) { /* ignore */ }
      try {
        frame = doc.createXULElement
          ? doc.createXULElement("browser") : doc.createElement("browser");
      } catch (e5) { frame = null; }
      if (!frame) return null;
      frame.id = "zotero-kb-admin-frame";
      frame.setAttribute("type", "content");
      frame.setAttribute("remote", "false");
      // ⚠⚠ 这里**刻意没有** maychangeremoteness：设了它页面会跑进独立进程，
      //     宿主拿不到 contentWindow（真机实测 isRemoteBrowser=true / cw=null）。
      frame.setAttribute("disableglobalhistory", "true");
      frame.setAttribute("flex", "1");
      try {
        frame.style.setProperty("width", "100%");
        frame.style.setProperty("height", "100%");
        frame.style.setProperty("min-width", "0");
        frame.style.setProperty("min-height", "0");
      } catch (e6) { /* ignore */ }
      try { host.appendChild(frame); } catch (e7) { return null; }
      try { win.document.title = "知识库 · 管理"; } catch (e8) { /* ignore */ }
    }
    try {
      if (!frame.getAttribute("src")) frame.setAttribute("src", self.kbAdminPageURL());
    } catch (e9) { /* ignore */ }
    return frame;
  },

  /** 窗口里那个装页面的 `<browser>`（basicViewer 自带一个）。 */
  kbAdminBrowserOf: function (win) {
    try {
      const doc = win && win.document;
      if (!doc) return null;
      if (doc.querySelector) {
        const b = doc.querySelector("browser");
        if (b) return b;
      }
      const root = doc.documentElement;
      if (root && root.getElementsByTagName) {
        return root.getElementsByTagName("browser")[0] || null;
      }
    } catch (e) { /* ignore */ }
    return null;
  },

  /** 两个 window 是不是同一个（event.source 与目标窗口的比法，含 Xray 兜底）。 */
  kbAdminSameWindow: function (target, source) {
    if (!target || !source) return false;
    if (source === target) return true;
    try {
      if (source.wrappedJSObject && target.wrappedJSObject) {
        return source.wrappedJSObject === target.wrappedJSObject;
      }
    } catch (e) { /* Xray 挡了就算了 */ }
    return false;
  },

  /** 给页面发一条消息。**必须走 WindowProxy 上的内建 postMessage**（见下）。 */
  kbAdminPostTo: function (w, obj) {
    if (!w) return false;
    // ① WindowProxy 上的内建 postMessage —— 真机实测**只有这条路不抛 DataCloneError**。
    //    为什么：chrome 里的对象经 Xray 交给 content 时，用 wrappedJSObject 上那个
    //    `postMessage` 会报 "The object could not be cloned."（真机实测原话）；
    //    而 WindowProxy 上的这个是 WebIDL 方法，参数会被正常结构化克隆。
    try { w.postMessage(obj, "*"); return true; } catch (e) { /* 落兜底 */ }
    // ② 兜底：wrappedJSObject 上的（少数窗口形态下只有它可达）
    try {
      const p = w.wrappedJSObject;
      if (p && p !== w) { p.postMessage(obj, "*"); return true; }
    } catch (e2) { /* ignore */ }
    Zotero.debug("[zotero-kb] 给管理页面发消息失败（两种 postMessage 都不行）");
    return false;
  },

  /** 兼容旧签名：第一个参数可以是 window，也可以是 `<browser>`。 */
  kbAdminPost: function (browserOrWindow, obj) {
    let w = browserOrWindow;
    try {
      if (browserOrWindow && browserOrWindow.contentWindow) {
        w = browserOrWindow.contentWindow;
      }
    } catch (e) { /* 当 window 用 */ }
    return ZoteroKB.kbAdminPostTo(w, obj);
  },

  /**
   * 把页面的一条请求派发给引擎，并按**同一个 seq** 回包。
   * 三条通道（postMessage 钩子 / 页面窗口监听 / 父窗口监听）共用这一个出口。
   *
   * `target` = **执行 admin.html 的那个 window**（回包就发给它）。
   */
  kbAdminDispatch: function (target, d, channel) {
    var self = ZoteroKB;
    const seq = d.seq;
    const reply = function (res) {
      const full = { __kba: 1, seq: seq };
      if (res && res.ok) { full.ok = true; full.data = res.data; }
      else {
        full.ok = false;
        full.error = String((res && res.error) || "宿主半处理失败");
      }
      // 记下"这次是哪条通道、哪个 seq、哪个路径通的"—— 真机排查一眼就知道走了哪条路。
      try {
        self.writeStatusFile({
          kbAdminChannel: String(channel || "") + " seq=" + seq
            + " op=" + String((d.args && d.args.path) || ""),
        });
      } catch (e) { /* ignore */ }
      if (!self.kbAdminPostTo(target, full)) {
        Zotero.debug("[zotero-kb] 回不了管理页面（窗口没了？）");
      }
    };
    return Promise.resolve()
      .then(function () { return self.kbAdminHandleRequest(target, d); })
      .then(function (res) {
        reply(res || { ok: false, error: "宿主半没有返回结果" });
      }, function (e) {
        reply({ ok: false, error: String((e && e.message) || e) });
      });
  },

  /**
   * 桥的核心：**在页面自己的 window 上**装钩子/监听，并按 URL 认出"哪个 window 是
   * 管理页面"。
   *
   * ## 为什么必须装钩子（`parentIsSelf` 已由真机证实）
   *
   * `content/admin.js::call()` 用 `window.parent.postMessage(...)` 发请求；真机
   * `kbAdminProbe.parentIsSelf = true` —— 内容文档是**顶层**浏览上下文，
   * `window.parent === window`，于是请求会**先被页面自己的 message 监听吃到**：
   * 那个 seq 从 `pending` 里被删掉、因为请求里没有 `ok`，当场
   * `reject(new Error("宿主半返回失败"))`（用户看到的就是这一句，不是 30 秒超时）；
   * 宿主半后来那份真实回包永远被丢弃。
   *
   * 所以把 `{__kba:1, op}` 形状的**请求**直接交给宿主处理，并且**不再往下派发**
   * （页面自己的监听因此收不到它）；其余消息（回包 `ok/error`、progress `event`、
   * 页面里任何别的 postMessage）原样放行 —— 回包必须走原函数才能到页面监听。
   *
   * ## 上一版为什么失败（真机证据，别重犯）
   *
   * 上一版从 `Zotero.openInViewer()` 的窗口里 `document.querySelector("browser")`
   * 取 `contentWindow`，真机上拿到的是**导航前的 about:blank**
   * （`kbAdminProbe` = `{"parentIsSelf":true,"hasKBAdmin":false,"url":"about:blank"}`），
   * 钩子装在了那个空窗口上，页面真正的请求没人接（`kbAdminChannel` 一直空）。
   * 现在窗口一律**按 URL 认**（`kbAdminOnDocInserted` 在文档创建时 /  `kbAdminScanAdminWindows`
   * 在已加载时），不再依赖 browser 元素的层次。
   */
  /** 某个 window 现在的地址（读不到给空串，不抛）。 */
  kbAdminHrefOf: function (w) {
    try { return String((w && w.location && w.location.href) || ""); }
    catch (e) { return ""; }
  },

  /** 这个地址是不是我们的管理页面。 */
  kbAdminIsAdminHref: function (href) {
    var self = ZoteroKB;
    const want = self.kbAdminPageURL();
    return !!want && String(href || "").indexOf(want) >= 0;
  },

  /**
   * 在**指定 window** 的 `postMessage` 上装一道分派钩子（防自回环，见文件头的说明）。
   *
   * 与上一版的区别：参数从 `<browser>` 换成**window 本身** —— 上一版从
   * `browser.contentWindow` 取窗口，真机上取得的是**导航前的 about:blank**
   * （`kbAdminProbe` 实测 `url:"about:blank", hasKBAdmin:false`），钩子装到了错的
   * 窗口上，所以什么也没接住。现在窗口由 URL 认（见 kbAdminOnDocInserted /
   * kbAdminScanAdminWindows），装的一定是"正在执行 admin.html 的那个 window"。
   */
  kbAdminInstallHookOn: function (w) {
    var self = ZoteroKB;
    if (!w) return false;
    let page = w;
    try { if (w.wrappedJSObject) page = w.wrappedJSObject; } catch (e) { /* 用原对象 */ }
    if (!page) return false;
    let already = false;
    try { already = page.__kbAdminHooked === true; } catch (e2) { already = false; }
    if (already) return true;
    let orig = null;
    try { orig = page.postMessage; } catch (e3) { orig = null; }
    if (typeof orig !== "function") return false;
    const hook = function (data) {
      try {
        if (data && typeof data === "object" && data.__kba === 1
            && typeof data.seq === "number"
            && data.op !== undefined
            && data.ok === undefined && data.error === undefined
            && data.event === undefined) {
          // 页面 → 宿主的**请求**：自己处理，不再派发（否则会被页面自己的监听吃掉）。
          ZoteroKB.kbAdminDispatch(w, data, "hook");
          return;
        }
      } catch (e) { /* 判断失败就当普通消息放行 */ }
      return orig.apply(this, arguments);
    };
    try { page.postMessage = hook; }
    catch (e4) {
      Zotero.debug("[zotero-kb] 装管理页面 postMessage 钩子失败：" + e4);
      return false;
    }
    try { page.__kbAdminHooked = true; } catch (e5) { /* 标记丢了下次再装，无害 */ }
    try {
      self.writeStatusFile({
        kbAdminPageHook: "installed@" + self.kbAdminHrefOf(w).slice(0, 80),
      });
    } catch (e6) { /* ignore */ }
    return true;
  },

  /**
   * 对一个**确认是管理页面**的 window 做完整武装：钩子 + 两处 message 监听 + 诊断。
   *
   *   ① 钩子 —— 顶层上下文（`window.parent === window`）时请求会回环到页面自己，
   *      只有它能拦下（真机 `parentIsSelf:true` 已证实这是真实形态）。
   *   ② 页面 window 上的监听 —— 上面那条路的兜底。
   *   ③ **父窗口**上的监听 —— 若内容文档其实是嵌套上下文，请求会落在 `window.parent`
   *      （那个 chrome 窗口）上，那里也必须有人听。
   * 三条同时装，多装零成本（不是请求形状就忽略），换来"哪种层次都能通"。
   */
  kbAdminArmPageWindow: function (w, via) {
    var self = ZoteroKB;
    if (!w) return false;
    let page = w;
    try { if (w.wrappedJSObject) page = w.wrappedJSObject; } catch (e) { /* 用原对象 */ }

    // ① 钩子
    let hooked = false;
    try { hooked = self.kbAdminInstallHookOn(w); } catch (e1) { /* ignore */ }

    // ② 页面 window 监听
    try {
      let bound = false;
      try { bound = page && page.__kbAdminMsgBound === true; } catch (e2) { bound = false; }
      if (!bound) {
        const onMsg = function (event) {
          try { self.kbAdminOnPageMessage(w, event, via || "page"); }
          catch (e3) { Zotero.debug("[zotero-kb] 处理页面消息失败：" + e3); }
        };
        w.addEventListener("message", onMsg, true);
        try { if (page) page.__kbAdminMsgBound = true; } catch (e4) { /* ignore */ }
      }
    } catch (e5) { Zotero.debug("[zotero-kb] 挂页面 message 监听失败：" + e5); }

    // ③ 父窗口监听（嵌套上下文时请求落在那里）
    try {
      const p = w.parent;
      if (p && p !== w) {
        let pj = p;
        try { if (p.wrappedJSObject) pj = p.wrappedJSObject; } catch (e6) { /* ignore */ }
        let pbound = false;
        try { pbound = pj && pj.__kbAdminParentBound === true; } catch (e7) { pbound = false; }
        if (!pbound) {
          const onMsg2 = function (event) {
            try { self.kbAdminOnPageMessage(w, event, "parent"); }
            catch (e8) { Zotero.debug("[zotero-kb] 处理父窗口消息失败：" + e8); }
          };
          p.addEventListener("message", onMsg2, true);
          try { if (pj) pj.__kbAdminParentBound = true; } catch (e9) { /* ignore */ }
        }
      }
    } catch (e10) { /* 拿不到 parent 就算了 */ }

    // ④ 诊断：页面在哪、parent 是谁、是不是顶层 —— 只在内容变化时写
    let probe = null;
    try {
      probe = {
        via: String(via || ""),
        url: self.kbAdminHrefOf(w).slice(0, 80),
        parentIsSelf: (w.parent === w),
        parentHref: (w.parent && w.parent !== w)
          ? self.kbAdminHrefOf(w.parent).slice(0, 60) : "",
        hasKBAdmin: (function () {
          try { return !!w.KBAdmin; } catch (e11) { return "ERR"; }
        })(),
        ready: (function () {
          try { return String(w.document && w.document.readyState); } catch (e12) { return "ERR"; }
        })(),
      };
    } catch (e13) { probe = { via: String(via || ""), error: String(e13) }; }
    let text = "";
    try { text = JSON.stringify(probe); } catch (e14) { text = String(probe); }
    if (self._kbAdminProbeWrote !== text) {
      self._kbAdminProbeWrote = text;
      try { self.writeStatusFile({ kbAdminProbe: text }); } catch (e15) { /* ignore */ }
    }
    return hooked;
  },

  /**
   * 观察"文档被创建"与"内容全局被创建"，**用 URL 认出管理页面**并在它的脚本
   * **跑起来之前**把桥装上。
   *
   * 为什么换成这条路（上一版为什么失败，有真机证据）：
   *   上一版靠 `Zotero.openInViewer` 返回的窗口 + `document.querySelector("browser")`
   *   去拿窗口，真机上拿到的是**导航前的 about:blank**（`kbAdminProbe` =
   *   `{"parentIsSelf":true,"hasKBAdmin":false,"docReady":"complete","url":"about:blank"}`），
   *   所以钩子和监听全装在了那个空窗口上，页面真正的请求没人接（`kbAdminChannel` 一直空）。
   *   用 URL 认窗口就没有"是哪一个"的不确定性 —— 页面在哪，钩子就在哪。
   *
   * ⚠ `document-element-inserted` 的 subject 是 **document**；它在脚本执行**之前**
   *   触发，所以能赶在 `admin.js` 的 boot()（complete 后 30ms）之前装上钩子。
   *   另外补一个 `content-document-global-created`（subject 是 window）作保险。
   */
  kbAdminEnsureDocObserver: function () {
    var self = ZoteroKB;
    if (self.kbAdminDocObs) return true;
    try {
      const obs = {
        observe: function (subject, topic) {
          try {
            if (topic === "document-element-inserted") {
              const doc = subject;
              let href = "";
              try {
                href = String((doc && doc.location && doc.location.href)
                               || (doc && doc.documentURI) || "");
              } catch (e) { href = ""; }
              if (!ZoteroKB.kbAdminIsAdminHref(href)) return;
              let w = null;
              try { w = doc.defaultView; } catch (e2) { w = null; }
              if (w) ZoteroKB.kbAdminArmPageWindow(w, "doc");
              return;
            }
            if (topic === "content-document-global-created") {
              const w = subject;
              if (ZoteroKB.kbAdminIsAdminHref(ZoteroKB.kbAdminHrefOf(w))) {
                ZoteroKB.kbAdminArmPageWindow(w, "global");
              }
            }
          } catch (e3) { /* 观察者里绝不抛 */ }
        },
      };
      Services.obs.addObserver(obs, "document-element-inserted");
      Services.obs.addObserver(obs, "content-document-global-created");
      self.kbAdminDocObs = obs;
      Zotero.debug("[zotero-kb] 管理页面文档观察者已挂");
      return true;
    } catch (e) {
      Zotero.debug("[zotero-kb] 挂文档观察者失败：" + e);
      return false;
    }
  },

  /**
   * 扫一遍所有 `zotero:basicViewer` 窗口里的**每一个** `<browser>`，把其中"正在跑
   * 管理页面"的那个武装起来（观察者只对**之后**创建的文档有效，页面若已经加载好，
   * 只能靠这里补上）。
   *
   * 同时把"这个窗口里到底有哪些 browser、各自的 currentURI / browsingContext /
   * contentWindow 在哪"记进状态文件（`kbAdminBrowsers`）—— 上一轮就是缺这份现场，
   * 只能靠猜 `querySelector("browser")` 那一个。
   */
  kbAdminScanAdminWindows: function () {
    var self = ZoteroKB;
    let armed = 0;
    const seen = [];
    try {
      const e = Services.wm.getEnumerator("zotero:basicViewer");
      while (e.hasMoreElements()) {
        const win = e.getNext();
        const rec = { winURI: "", browsers: [] };
        try { rec.winURI = String(win.viewerOriginalURI || "").slice(0, 70); }
        catch (e2) { rec.winURI = "?"; }
        let browsers = [];
        try {
          browsers = Array.from(
            win.document.documentElement.getElementsByTagName("browser"));
        } catch (e3) { browsers = []; }
        try {
          const more = win.document.querySelectorAll
            ? win.document.querySelectorAll("browser") : [];
          for (const b of Array.from(more)) {
            if (browsers.indexOf(b) < 0) browsers.push(b);
          }
        } catch (e4) { /* ignore */ }
        for (const b of browsers) {
          let cw = null, curi = "", href = "", bcuri = "", isTop = null;
          try { cw = b.contentWindow; } catch (e5) { cw = null; }
          try { curi = b.currentURI ? String(b.currentURI.spec) : ""; } catch (e6) { curi = ""; }
          try { href = cw ? String(cw.location.href) : ""; } catch (e7) { href = ""; }
          try {
            const bc = b.browsingContext;
            bcuri = (bc && bc.currentURI) ? String(bc.currentURI.spec) : "";
            isTop = bc ? !!bc.isTopLevel : null;
          } catch (e8) { bcuri = "ERR"; }
          rec.browsers.push({
            id: (function () { try { return String(b.id || ""); } catch (e9) { return "?"; } })(),
            curi: curi.slice(0, 70), bc: String(bcuri).slice(0, 70),
            href: href.slice(0, 70), hasCw: !!cw, isTop: isTop,
          });
          if (cw && self.kbAdminIsAdminHref(href)) {
            try {
              self.kbAdminArmPageWindow(cw, "scan");
              armed++;
            } catch (e10) { Zotero.debug("[zotero-kb] 武装页面窗口失败：" + e10); }
          }
        }
        seen.push(rec);
      }
    } catch (e11) { /* 拿不到窗口枚举就算了 */ }
    // 一个都没武装上时留下现场（只在变化时写，避免 20ms 轮询刷盘）
    if (!armed && seen.length) {
      let text = "";
      try { text = JSON.stringify(seen); } catch (e12) { text = String(seen); }
      if (self._kbAdminBrowsersWrote !== text) {
        self._kbAdminBrowsersWrote = text;
        try { self.writeStatusFile({ kbAdminBrowsers: text }); } catch (e13) { /* ignore */ }
      }
    }
    return armed;
  },

  /**
   * 兼容旧名字：现在它是"扫所有 basicViewer 窗口、按 URL 认页面并武装它"。
   * 保留这个名字是因为 `kbAdminStartBridgeWatch` 与验证脚本仍在调它。
   */
  kbAdminAttachWindow: function (win) {
    return ZoteroKB.kbAdminScanAdminWindows();
  },

  /**
   * 反复补挂桥（**本波的关键修复**，与 `20-kbview.js` 的 400ms 补挂同一套路）。
   *
   * 为什么不能只挂一次：刚 openInViewer 时 `contentWindow` 还是 about:blank；页面一
   * 导航内层 window 就换了，挂上去的监听与钩子全部失效；而 XUL `<browser>` 不触发
   * load 事件，没有可靠的"页面好了"事件可依赖；偏偏页面 boot() 在 complete 后 30ms
   * 就发第一批请求。所以：**头 2 秒每 20ms 补一次**（赶 boot），之后每 250ms 补到
   * 约 12 秒（扛重载/晚到的导航），然后停（不留常驻定时器）。
   */
  kbAdminStartBridgeWatch: function (win) {
    var self = ZoteroKB;
    if (!win) return;
    if (win.__kbAdminWatch) return;          // 一个窗口只起一条链
    win.__kbAdminWatch = true;
    let ticks = 0;
    const tick = function () {
      ticks++;
      try {
        if (win.closed) return;
        // ① 壳窗口的文档还没好就先把我们的 browser 建出来（幂等）
        self.kbAdminMountFrame(win);
        // ② 扫所有 basicViewer 窗口，把"在跑管理页面的那个 window"武装起来
        self.kbAdminAttachWindow(win);
      } catch (e) { /* 下一拍再试 */ }
      if (ticks >= 140) return;
      const delay = ticks <= 100 ? 20 : 250; // 前 2 秒密，之后疏
      if (typeof setTimeout === "function") setTimeout(tick, delay);
    };
    if (typeof setTimeout === "function") setTimeout(tick, 0);
  },

  /**
   * 页面来的一条消息。只处理**请求形状**的（`op` 有值），回包/其它一律忽略 ——
   * 否则我们自己的回包会被再派发一次，变成无限回环。
   */
  kbAdminOnPageMessage: function (target, event, channel) {
    var self = ZoteroKB;
    let d = null;
    try { d = event && event.data; } catch (e) { d = null; }
    if (!d || typeof d !== "object" || d.__kba !== 1) return;
    if (d.op === undefined) return;
    // 来源校验：比对不上只**记日志**，不丢消息 —— 监听点本身只有这个页面的窗口（或它
    // 的父窗口）能说话；Xray 下同一性比不出来是常事，宁可多处理，也不要再出现
    // "请求被静默丢掉"这种事（这一轮就是被静默丢消息坑的）。
    if (!self.kbAdminSameWindow(target, event && event.source)) {
      Zotero.debug("[zotero-kb] 管理页面消息来源比对不上（监听点唯一，继续处理）");
      try { self.writeStatusFile({ kbAdminSrcCheck: "mismatch-but-handled" }); }
      catch (e2) { /* ignore */ }
    }
    self.kbAdminDispatch(target, d, channel || "msg");
  },

  /**
   * 打开管理页面（工具菜单的入口）。**幂等**：已经开着就聚焦它。
   *
   * 为什么用 `Zotero.openInViewer()` 而不是自己 openDialog 一个窗口：
   *   · 它就是 Zotero 自己那条"带 `<browser>` 的窗口"的路子（工具 → 插件用的也是它），
   *     `<browser type="content" remote="false">` 在父进程里加载，宿主拿得到
   *     `contentWindow` —— 这正是我们挂桥需要的（jar: 地址则根本不导航，见 20-kbview.js）；
   *   · 它自带幂等（按 `viewerOriginalURI` 找已开窗口并聚焦）；
   *   · 不用新建任何 chrome 文件（本波只许改三个文件）。
   */
  kbAdminOpen: function () {
    var self = ZoteroKB;
    const url = self.kbAdminPageURL();
    if (!url) {
      self.alertDialog("打不开管理页面", "页面地址没算出来（resource 包没注册？）");
      return null;
    }
    // ⚠ **先挂文档观察者**：管理页面的文档一被创建（脚本还没跑）就按 URL 认出来、
    //   当场把钩子装上（见 kbAdminEnsureDocObserver）。这是"页面在哪、钩子在哪"的
    //   确定性做法；下面那些"扫 browser / 定时补挂"都是它的兜底。
    try { self.kbAdminEnsureDocObserver(); }
    catch (e0) { Zotero.debug("[zotero-kb] 挂文档观察者失败：" + e0); }

    // 壳窗口已经开着 → 聚焦 + 补建 frame + 起观察链
    let win = self.kbAdminFindShellWindow();
    if (win) {
      try { win.focus(); } catch (e1) { /* ignore */ }
      try { self.kbAdminMountFrame(win); }
      catch (e2) { Zotero.debug("[zotero-kb] 补建管理页面 frame 失败：" + e2); }
      self.kbAdminStartBridgeWatch(win);
      return win;
    }
    if (typeof Zotero.openInViewer !== "function") {
      self.alertDialog("打不开管理页面",
        "这个 Zotero 版本没有 Zotero.openInViewer()，装不了带 <browser> 的窗口。\n"
        + "可以先用工具栏按钮打开旧的 Tk 面板。");
      return null;
    }
    const shellURI = self.kbAdminShellURI();
    win = null;
    try {
      // ⚠ 只让它加载 about:blank（**不能**直接加载管理页面，理由见 kbAdminShellURI：
      //   壳窗口自带的 browser 有 maychangeremoteness，会把页面切到独立进程）
      win = Zotero.openInViewer(shellURI, {
        onLoad: function () {
          try {
            self.kbAdminSizeWindow(win);
            self.kbAdminMountFrame(win);
            self.kbAdminStartBridgeWatch(win);
          } catch (e) { Zotero.debug("[zotero-kb] 壳窗口就绪后建 frame 失败：" + e); }
        },
      });
    } catch (e3) {
      self.alertDialog("打开管理页面失败", String((e3 && e3.message) || e3));
      return null;
    }
    // ⚠ 新窗口把默认尺寸调宽（用户 2026-10-13："默认宽度把主页列表挤成两行"）。
    //   放在"新窗口"这条路上：已经开着的窗口在上面就 return 了 —— 用户自己调过的
    //   尺寸不会被我们每次打开都推翻。
    try { self.kbAdminSizeWindow(win); } catch (e5) { /* ignore */ }
    // 壳窗口的文档可能还没解析完 → 立刻试一次，剩下的交给观察链
    try { self.kbAdminMountFrame(win); } catch (e4) { /* ignore */ }
    self.kbAdminStartBridgeWatch(win);
    return win;
  },

  /**
   * 管理窗口的**默认尺寸与最小尺寸**（2026-10-13 用户两轮要求："再宽一点"）。
   *
   * 为什么这么做（读 Zotero 10.0.6 的 `app/omni.ja` 查实，不是猜的）：
   *   · `Zotero.openInViewer(uri, opts)` **没有尺寸参数** —— 它内部就是
   *     `ww.openWindow(null, "chrome://zotero/content/standalone/basicViewer.xhtml",
   *     null, "chrome,dialog=yes,resizable,centerscreen,menubar,scrollbars", arg)`，
   *     尺寸完全由那份 XUL 与平台默认决定；
   *   · `basicViewer.xhtml` 的根元素只写了 `style="min-width:1000px; min-height:700px"`
   *     与 `persist="screenX screenY width height sizemode"`（用户调过的尺寸会被记住），
   *     所以**默认就是最小**：1000×700（实测页面 innerWidth=1000）。
   *     而结构页在 1000~1194px 这个宽度段里「能删吗」表头会被折成三行。
   * 做法（第二版，用户说第一版的 1300 还是"挤"）：
   *   ① 把根元素的 `min-width/min-height` 抬到 **1400×820** —— 用户再也拖不到更窄；
   *   ② `resizeTo()` 到**屏幕可视区的 90%**（`screen.availWidth/availHeight`，
   *      不超过屏幕），拿不到屏幕尺寸时兜底 1600×950。
   * 调用点只在**新建窗口**时（见 kbAdminOpen），不动用户自己调过的尺寸。
   */
  kbAdminSizeWindow: function (win) {
    if (!win || !win.document || !win.document.documentElement) return false;
    const MIN_W = 1400, MIN_H = 820;
    const FALLBACK_W = 1600, FALLBACK_H = 950;      // 取不到屏幕尺寸时的兜底
    try {
      const root = win.document.documentElement;
      root.style.setProperty("min-width", MIN_W + "px");
      root.style.setProperty("min-height", MIN_H + "px");
    } catch (e) { /* 设不上最小尺寸也不影响下面的 resizeTo */ }
    try {
      const scr = win.screen || {};
      const availW = Number(scr.availWidth) || 0;
      const availH = Number(scr.availHeight) || 0;
      // 先按可视区 90% 取，再夹在 [MIN, 可视区] 之间。屏幕比 MIN 还小时只能顶到
      // MIN（min-width 已经是 1400，窗口再小会自相矛盾），这种情况如实大一点。
      const wantW = Math.round((availW || FALLBACK_W) * 0.9);
      const wantH = Math.round((availH || FALLBACK_H) * 0.9);
      const w = Math.max(MIN_W, Math.min(wantW, availW || FALLBACK_W));
      const h = Math.max(MIN_H, Math.min(wantH, availH || FALLBACK_H));
      if (typeof win.resizeTo === "function") win.resizeTo(w, h);
      // 原来那 1000 宽是 centerscreen 放好的；变宽之后顺手放回屏幕中间
      if (typeof win.moveTo === "function" && availW && availH) {
        win.moveTo(Math.max(0, Math.round((availW - w) / 2)),
                   Math.max(0, Math.round((availH - h) / 2)));
      }
      return true;
    } catch (e2) {
      Zotero.debug("[zotero-kb] 调整管理窗口尺寸失败：" + e2);
      return false;
    }
  },

  /**
   * **给插件内部其它模块用的引擎调用面**（W3 线2.5 的入口）。
   *
   * 为什么要有它：面板走"页面 → postMessage → kbAdminHandleRequest"，但那需要
   * 一个打开的页面窗口。`src` 里其它模块（权重列、分类、元数据、侧栏 doc-edit、
   * 纲要菜单…）**不是**在页面里跑的，它们要的是"给一条 HTTP 语义的路径（或引擎
   * 方法名），把数据拿回来"。于是这里给两层：
   *
   *   · `kbAdminHostRoute(method, path, body)` —— 认 HTTP 语义（与
   *     `KB_ADMIN_ROUTES` 同一张表），返回引擎的 `data`；未知路径**明确报错**
   *     （不猜），错误信息里带上路径，方便"这条还没迁"一眼看出来。
   *   · `kbAdminHostRpc(engineMethod, params)` —— 直接调引擎方法（已经知道方法名时用）。
   *
   * 两者都**按需拉起引擎**（`kbAdminRpc` 内部会 `kbAdminStartEngine()`，所以
   * 面板没打开也照样能用；拿不到长驻管道时自动退化成"短进程 + 单行管道"）。
   * 返回 Promise；失败 reject 一个 message 人能看懂的 Error —— 调用方**必须**
   * 接住，别让异常冒到 UI 线程（本项目的纪律：降级可以，抛出去不行）。
   */
  kbAdminHostRpc: function (engineMethod, params) {
    var self = ZoteroKB;
    return self.kbAdminRpc(engineMethod, params);
  },

  kbAdminHostRoute: function (method, path, body) {
    var self = ZoteroKB;
    const route = self.kbAdminMapRoute(method, path, body);
    if (!route) {
      return Promise.reject(new Error(
        "这条路径还没有引擎映射：" + String(method || "GET") + " " + String(path || "")
        + "（要迁就加进 src/23-admin.js 的 KB_ADMIN_ROUTES，并在 online/engine.py 补方法）"));
    }
    return self.kbAdminRpc(route.method, route.params);
  },

  // ================================================================ 门面
  //
  // 计划文档里把宿主半写成 `KBAdminHost.ensureEngine()`。内部实现是一批
  // `ZoteroKB.kbAdminXxx` 成员（好被 check_plugin 的成员引用检查扫到、
  // 也好在桩测试里单独调），这里给一个薄门面把这个名字对上。

  KBAdminHost: null,

  kbAdminHost: function () {
    var self = ZoteroKB;
    if (!self.KBAdminHost) {
      self.KBAdminHost = {
        ensureEngine: function () { return ZoteroKB.kbAdminStartEngine(); },
        rpc: function (method, params) {
          return ZoteroKB.kbAdminRpc(method, params);
        },
        // 插件内部模块用的那两个（见上面的注释）：HTTP 语义 + 引擎方法名
        route: function (method, path, body) {
          return ZoteroKB.kbAdminHostRoute(method, path, body);
        },
        call: function (engineMethod, params) {
          return ZoteroKB.kbAdminHostRpc(engineMethod, params);
        },
        stop: function () { return ZoteroKB.kbAdminStop(); },
        open: function () { return ZoteroKB.kbAdminOpen(); },
        state: function () { return ZoteroKB.kbAdminEngineState(); },
      };
    }
    return self.KBAdminHost;
  },
});

// ===== src/99-bootstrap.js =====
/**
 * 99-bootstrap.js —— Zotero 要调用的顶层生命周期函数（必须是顶层函数声明）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */

// ============================================================================
// 顶层生命周期函数
//
// Zotero 的 plugins.js 是这样找 bootstrap 方法的：
//     func = scope[method] || Cu.evalInSandbox(`${method};`, scope)
// 它**先看沙箱全局属性**。顶层 `function startup(){}` 会直接成为沙箱全局属性，
// 走第一条路最稳；而把 startup 只挂在 ZoteroKB 对象上时，`scope.startup` 是
// undefined，得靠 Cu.evalInSandbox 兜底 —— 多一层不确定性（本机就遇到过
// "插件 active=true 但 startup 从未执行"，怀疑与这一层有关）。
//
// 所以这里做**双保险**：既暴露顶层函数（Zotero 首选），又保留对象成员写法。
// 顶层函数里同时把加载情况写文件，好判断"到底进没进来"。

function startup(params, reason) {
  try {
    if (typeof ZoteroKB === "undefined" || !ZoteroKB) {
      Zotero.debug("[zotero-kb] 顶层 startup 被调用，但 ZoteroKB 不存在");
      return;
    }
    ZoteroKB.startup(params || {});
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
    try {
      var msg = "startup 顶层包装抛错: " + e
        + "\n" + (e && e.stack ? String(e.stack).slice(0, 800) : "");
      Zotero.File.putContents(
        Zotero.File.pathToFile(
          self.kbDir() + "\\startup-crash.txt"), msg);
    } catch (e3) { /* ignore */ }
  }
}

function shutdown() {
  try {
    if (typeof ZoteroKB !== "undefined" && ZoteroKB) ZoteroKB.shutdown();
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
  }
}

// 窗口钩子：官方文档要求"与窗口相关的 UI 活动（菜单、自定义列等）
// 由 onMainWindowLoad 执行"，否则新开的主窗口不会包含这些改动。
// 这里用来（重新）注册权重列 + 重启轮询 —— 窗口重建时能把它们带回来。
function onMainWindowLoad({ window } = {}) {
  try {
    if (typeof ZoteroKB === "undefined" || !ZoteroKB) return;
    ZoteroKB.onMainWindowLoad(window);
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
  }
}

function onMainWindowUnload({ window } = {}) {
  try {
    if (typeof ZoteroKB === "undefined" || !ZoteroKB) return;
    ZoteroKB.onMainWindowUnload(window);
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
  }
}

function install() {}
function uninstall() {}
