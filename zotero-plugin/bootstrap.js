// ⚠ 本文件由 tools/build_bootstrap.py 从 zotero-plugin/src/*.js 生成，
//    不要直接改这里 —— 改 src/ 下的源文件，再跑：
//        python tools/build_bootstrap.py
//    改完没重新生成的话，打包前会被拒绝（tools/pack_plugin.py 会校验）。
//    源码共 23 个文件，清单在 build_bootstrap.py 的 SRC_ORDER。

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
      // ⚠ 第一件事就是把 ftl 挂到窗口文档上（见 initLocale 的说明）——
      //   后面的设置面板/内容窗格分区都要靠它把 l10nID 变成文字。
      step("initLocale", () => self.initLocale());
      step("registerPrefs", () => self.registerPrefs());
      step("registerPrefObserver", () => self.registerPrefObserver());
      step("registerPrefPane", () => self.registerPrefPane());
      step("registerNotifier", () => self.registerNotifier());
      step("registerWeightColumn", () => self.registerWeightColumn());
      step("startTaskPolling", () => self.startTaskPolling());
      // MinerU（可选组件）的首次安装引导：**延迟 8 秒**跑，且只在"没装过 +
      // 没问过"时才弹一次（见 19-mineruguide.js）。放在这里是为了让它跟别的
      // 启动步骤一样有名字、失败也能在状态文件里看到（它自己不会抛）。
      step("scheduleOptionalGuides", () => self.scheduleOptionalGuides());
      // 能力探测：DSH 接上了没、本地模型可用不可用。右键菜单只读它的缓存
      // （菜单是同步构建的，等不了网络），所以这一步只负责"排上周期探测"。
      step("scheduleCapsRefresh", () => self.scheduleCapsRefresh());
      // 右侧栏「知识库」分区（20-kbview.js）：只读展示 kb/ 里的 md。
      // ⚠ 与已删掉的「窗格本地模型对话」不同 —— 那个是交互（调模型），这个是只读。
      step("registerKbViewSection", () => self.registerKbViewSection());
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
      try { self.unregisterKbViewSection(); } catch (e) { /* ignore */ }
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
    server: "zotero-kb.server",
    token: "zotero-kb.token",
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
    // 右侧栏「知识库」分区显示哪一级（2026-10-05 新增，见 20-kbview.js）。
    // 默认「分节纲要」—— 那一层就是为"对着 PDF 读"设计的（每节带页码范围）。
    kbviewLevel: "zotero-kb.kbviewLevel",
    // 右侧栏「知识库」分区的字号（px）。**只作用于我们这一块**（容器上的内联
    // font-size），不动 Zotero 的任何默认设置。
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
   */
  kbPushEnvConfig: async function (env) {
    var self = ZoteroKB;
    try {
      const r = await self.request("POST", "/env-config", { env: env });
      return { ok: true, report: (r && r.report) || null };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },


  /** 从服务端取运行环境诊断（设置面板「检测」按钮用）。 */
  kbEnvReport: async function () {
    var self = ZoteroKB;
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
   * 项目根目录（放 .venv / tools\gui.py 的地方）。
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


  projectRoot: function () {
    // 1) 用户在设置面板里指定的（唯一权威来源）
    try {
      const v = Zotero.Prefs.get("zotero-kb.projectRoot");
      if (v && String(v).trim()
          && this._exists(String(v).trim().replace(/[\\/]+$/, "") + "\\tools\\gui.py")) {
        return String(v).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* ignore */ }
    // 2) 服务端上次告诉我们的
    try {
      const v = Zotero.Prefs.get("zotero-kb.serverProjectRoot");
      if (v && String(v).trim()
          && this._exists(String(v).trim().replace(/[\\/]+$/, "") + "\\tools\\gui.py")) {
        return String(v).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* ignore */ }
    // 3) 环境变量
    try {
      const v = Services.env.get("ZOTERO_KB_ROOT");
      if (v && this._exists(v + "\\tools\\gui.py")) return v;
    } catch (e) { /* ignore */ }
    // 4) 从知识库位置向上找（老布局 <项目>\kb\ 能命中）。
    //    这是探测，不是反推：**验证到才用**。
    try {
      let d = this.kbDir().replace(/[\\/]+$/, "");
      for (let i = 0; i < 4 && d; i++) {
        if (this._exists(d + "\\tools\\gui.py")) return d;
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
      const tok = self.getPref(self.PREFS.token, "");
      const req = new XMLHttpRequest();
      req.open("POST", url, false);         // false = 同步，关闭流程里必须这样
      req.setRequestHeader("Content-Type", "application/json");
      if (tok) req.setRequestHeader("X-KB-Token", tok);
      req.send(JSON.stringify({ token: tok }));
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
    defaults[this.PREFS.server] = "http://127.0.0.1:8765";
    defaults[this.PREFS.token] = "";
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

// ===== src/05-server.js =====
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
        this.notify("知识库服务没启动",
          "请先启动本地服务（scripts\\0-panel.vbs 里能开，或跑 python online\\localserver.py）",
          null, true);
        return;
      }
      const key = item.key;
      const info = await this.request("POST", "/item-info", { keys: [key] });
      const entry = (info.items || [])[0] || {};
      const needsIndex = !entry.in_kb || !entry.n_chunks;

      if (needsIndex) {
        this.notify("正在把新文献送进知识库…", item.getField("title"), null, true);
        const res = await this.request("POST", "/reindex", { keys: [key] });
        const jobId = res && res.job;
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


  waitJob: async function (jobId, maxSeconds) {
    const deadline = Date.now() + (maxSeconds || 30) * 1000;
    while (Date.now() < deadline) {
      // 自己用 setTimeout 包一个延时：
      // 别用 Zotero.Promise.delay —— 插件沙箱的 Zotero 命名空间里不一定有
      // （这里已经踩过一次 Zotero.setInterval / Zotero.setTimeout 不存在的坑）。
      await new Promise((r) => setTimeout(r, 1000));
      try {
        const job = await this.request("GET", "/jobs/" + jobId);
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
      const res = await this.request("POST", "/classify", payload);
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
                  "看起来本地服务或模型没响应（面板「运行环境」可自检）",
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
        const r = await self.request("POST", "/weight",
                                     { key: k, pinned: !!pinned });
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
        tf = await this.request("POST", "/typefix",
                                { key: item.key, use_network: true });
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
      res = await this.request("POST", "/metafill",
                               { key: item.key, use_model: true });
    } catch (e) {
      this.notify("补全建议失败", String((e && e.message) || e), null, true);
      return;
    }
    if (!res || res.ok === false) {
      // 服务端把失败原因原样带回来了（它内部 try/except 过），这里照实显示。
      this.alertDialog(
        "补全建议失败",
        String((res && res.error) || "服务端没有返回可用的结果")
        + "\n\n如果提示是「连不上服务」，先确认本地服务在跑："
        + "管理面板（scripts\\0-panel.vbs）里能开，或重启 Zotero 让它自动拉起。");
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
        res = await self.request("POST", "/metafill",
                                 { key: it.key, use_model: false });
      } catch (e) {
        res = { ok: false, error: String((e && e.message) || e) };
      }
      results.push({ item: it, res: res });
      // 顺手记下"类型/标题可能被网页污染"的条目 —— 但**不批量改**，
      // 只在最后的回报里提醒用户单独去处理（见函数头第 3 条）。
      if (self.itemIsWebSaved(it)) {
        try {
          const tf = await self.request("POST", "/typefix",
                                        { key: it.key, use_network: false });
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
        const r = await self.request("POST", "/item-info", { keys });
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

    // ③ 交给面板的 Python 打开（os.startfile —— 面板「文献管理器中查看」就是
    //    这么干的，本机确定可用）。代价是要起一个短命的 pythonw 进程。
    try {
      if (self.openKbViaPanel(path)) return "panel";
      tried.push("面板：找不到 Python 或 tools\\gui.py");
    } catch (e) {
      tried.push("面板：" + e);
    }

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

  /**
   * 起一个管理面板进程（`tools\gui.py` + 任意参数）。返回 true/false。
   *
   * 为什么抽出来：面板不止一个"带参数启动"的用途 —— 打开某个 md
   * （`--open <path>`）与打开 MinerU 安装引导（`--tab struct --mineru-guide`）
   * 走的是同一条链。分开写两份的话，Subprocess / nsIProcess 那套兜底
   * 迟早只有一处被修。
   */
  panelProcess: function (extraArgs) {
    var self = ZoteroKB;
    const root = self.projectRoot();
    const py = self.pythonExe();
    const gui = root ? root + "\\tools\\gui.py" : "";
    if (!py || !root || !self._exists(gui)) return false;
    const args = ["-X", "utf8", gui].concat(extraArgs || []);
    try {
      const { Subprocess } = ChromeUtils.importESModule(
        "resource://gre/modules/Subprocess.sys.mjs");
      Subprocess.call({
        command: py, arguments: args, workdir: root,
        stderr: "ignore", stdout: "ignore",
      }).catch(function (e) {
        Zotero.debug("[zotero-kb] 面板进程：" + e);
      });
      return true;
    } catch (e) {
      // Subprocess 不可用时退回 nsIProcess（与 openPanel 同一套兜底）
      try {
        const nsLocalFile = Components.Constructor(
          "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
        const proc = Components.classes["@mozilla.org/process/util;1"]
          .createInstance(Components.interfaces.nsIProcess);
        proc.init(nsLocalFile(py));
        proc.runw(false, args, args.length);
        return true;
      } catch (e2) {
        Zotero.debug("[zotero-kb] 面板进程也起不来：" + e2);
        return false;
      }
    }
  },


  /**
   * 用面板的 Python 打开一个文件（`tools/gui.py --open <path>`）。
   *
   * 为什么不直接在这里调 shell：插件沙箱里没有可靠的"用默认程序打开"接口，
   * 而面板那边 `os.startfile` 一直在用、确定可用。让它代劳最省事。
   */
  openKbViaPanel: function (path) {
    return this.panelProcess(["--open", path]);
  },

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
   * 面板本身是 Python/Tkinter 程序（tools\gui.py）—— 插件不重写界面，
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


  /** 启动管理面板（Python/Tkinter）。已开着就不重复启动。 */
  openPanel: function () {
    var self = ZoteroKB;
    try {
      const root = self.projectRoot();
      const py = self.pythonExe();
      const gui = root ? root + "\\tools\\gui.py" : "";
      // 报错时把"我找的是哪、为什么、该改什么"一次说清，
      // 而不是只说"找不到 Python 环境"（那样用户不知道该动哪一个框）。
      if (!root || !self._exists(gui)) {
        self.notify("找不到项目目录",
          "我找的是：" + (root || "（没找到）")
          + "\n它下面应该有 tools\\gui.py，但没找到。"
          + "\n\n请在 设置 → 文献知识库 → 运行环境 里，"
          + "把「项目目录」指到有 offline、online、.venv 的那个文件夹。",
          null, true);
        return;
      }
      if (!self._exists(py)) {
        self.notify("找不到 Python 环境",
          "我找的是：" + (py || "（没找到）")
          + "\n项目目录：" + root
          + "\n\n请在 设置 → 文献知识库 → 运行环境 里点「浏览…」，"
          + "选到 .venv\\Scripts\\pythonw.exe；"
          + "\n如果还没建过环境，先按 README 的「安装」一节建一次。",
          null, true);
        return;
      }
      // 已在运行就不重复开（GUI 自己也有单实例判断，这里再挡一层）
      try {
        const { Subprocess } = ChromeUtils.importESModule(
          "resource://gre/modules/Subprocess.sys.mjs");
        Subprocess.call({
          command: py, arguments: ["-X", "utf8", gui],
          workdir: self.projectRoot(),
          stderr: "ignore", stdout: "ignore",
        }).catch((e) => Zotero.debug("[zotero-kb] 面板进程：" + e));
        self.notify("正在打开管理面板", "如果没出现，看任务栏（面板可能已在运行）",
                    null, false);
      } catch (e) {
        // Subprocess 不可用时退回 nsIProcess
        const nsLocalFile = Components.Constructor(
          "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
        const proc = Components.classes["@mozilla.org/process/util;1"]
          .createInstance(Components.interfaces.nsIProcess);
        proc.init(nsLocalFile(py));
        const args = ["-X", "utf8", gui];
        proc.runw(false, args, args.length);
        self.notify("正在打开管理面板", "", null, false);
      }
    } catch (e) {
      Zotero.logError(e);
      self.notify("打开面板失败", String(e), null, true);
    }
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


  /** 拉取全量权重映射填进内存缓存（供同步的 dataProvider 查表）。 */
  refreshWeights: async function () {
    try {
      let res = await this.request("GET", "/weights");
      if (!res || !res.weights) {
        // 拿不到（服务刚重启、token 变了、服务刚被拉起还没就绪）→
        // 补问一次 /health，它会顺手把新 token 同步进 pref，再试一次。
        await this.healthCheck();
        res = await this.request("GET", "/weights");
      }
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
      Zotero.debug("[zotero-kb] 拉权重失败：" + e);
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
    self.mainWindow = null;
    // 文档要求：窗口卸载时要停掉计时器，否则可能内存泄漏
    // （只停轮询，列注册保留 —— 下次 onMainWindowLoad 会复用）
    try { self.stopTaskPolling(); } catch (e) { /* ignore */ }
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

  // ================================================================ 任务轮询

  /**
   * 轮询本地服务领取"在 Zotero 里执行 JS"的任务。
   *
   * 为什么做这个：调试插件时想在 Zotero 里跑一段 JS（诊断、刷新权重、验证功能），
   * 原本只能"打开 运行JavaScript 窗口 → 复制 → 粘贴 → Ctrl+R"，一次几十秒。
   * 有了轮询，Python 侧派任务、插件执行、结果回传，全自动。
   *
   * 安全：走的还是本地服务的 token 认证通道（服务只监听 127.0.0.1）。
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
        const res = await self.request("GET", "/task");
        const task = res && res.task;
        if (task) {
          Zotero.debug("[zotero-kb] 领取任务 " + task.id + "（" + task.kind + "）");
          await self.runTask(task);
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
        if (tick % 15 === 0) {
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
    try {
      await self.request("POST", "/task/result", {
        id: task.id, ok: ok, error: error,
        result: { value: self.safeStringify(result), ms: ms,
                  pluginVersion: self.version },
      });
    } catch (e) {
      Zotero.debug("[zotero-kb] 回报任务结果失败：" + e);
    }
    Zotero.debug("[zotero-kb] 任务 " + task.id + (ok ? " 完成" : " 失败")
                 + "（" + ms + "ms）");
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
    return Promise.all([
      self.probeDsh(force).catch(function () { return null; }),
      Promise.resolve(self.probeLocalModel()),
    ]).then(function (r) {
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
    const info = (self.serverInfo && self.serverInfo.ollama) || null;
    if (!info) {
      self.caps.localWhy = "还没拿到本地服务状态（点面板「服务状态」看一眼）";
      return null;
    }
    if (!info.exists) {
      self.caps.localWhy = "没找到 Ollama（可选组件；也可改用 API 模型）";
      return false;
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
             tooltip: "用本地模型做「分类建议」和「补全元数据」"
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
 * ## 为什么不在这里直接弹 Tkinter 向导
 *
 * 插件沙箱里起不了窗口，而且安装要实时日志、要能取消 —— 那些都在面板里
 * （`tools/panels/mineru_guide.py`、`tools/panels/ollama_guide.py`）。
 * 这里只负责"拉面板并把对应窗口打开"，用与「打开知识库」同一条
 * `Subprocess` + `nsIProcess` 兜底链（`panelProcess`）。
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
      try {
        const info = await self.request("GET", "/mineru-check");
        seen = !!(info && info.ok);
      } catch (e) {
        Zotero.debug("[zotero-kb] /mineru-check 不可用（服务没起来？）：" + e);
      }
      if (!seen) missing.push("mineru");
    }
    // ② Ollama：先看本地文件，再问 /health
    const ol = self.ollamaExePath();
    if (!(ol && self._exists && self._exists(ol))) {
      let seen = false;
      try {
        const info = await self.request("GET", "/health");
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


  /** 拉面板并打开对应组件的安装引导窗口。`which` = "mineru" | "ollama"。 */
  openOptionalGuide: function (which) {
    var self = ZoteroKB;
    const flag = which === "ollama" ? "--ollama-guide" : "--mineru-guide";
    const ok = self.panelProcess
      ? self.panelProcess(["--tab", "struct", flag])
      : false;
    if (!ok) {
      self.notify("打不开安装引导",
        "没能拉起管理面板。可以自己打开面板 →「知识库结构」页 →"
        + "「MinerU 安装引导」/「Ollama 安装引导」，"
        + "或者双击项目里的 scripts\\install-mineru.cmd。",
        null, true);
    }
    return ok;
  },
});

// ===== src/20-kbview.js =====
/**
 * 20-kbview.js —— 条目右侧栏的「知识库」分区：把 kb/ 里的 markdown 显示出来。
 *
 * 用户 2026-10-05 的原话：「zotero 的文献右侧栏是可以正确显示 md 格式的，能不能把
 * 知识库中的 md 在这里显示，方便和原文对照？」（要对着 PDF 读纲要那一层）
 *
 * ## ⚠ 与已经删掉的「窗格本地模型对话」（19-itempane.js）的区别
 *
 *   · 那个是**交互**：提问 → 调本地模型 → 多轮讨论。用户判定"没什么用而且 bug 多"，
 *     整条链（窗格 + 7 个端点 + kbchat/paras + 逐段检查页）都删了。
 *   · 这个是**只读展示**：读 `kb/` 里的 md 文件 → 渲染成 HTML。不发请求、不写数据、
 *     模型挂了也照样能看。
 *
 * 所以 `Zotero.ItemPaneManager` 这个 API 名字重新出现是**有意的**；
 * `tools/check_plugin.py` 的反向清单里只保留"对话窗格"那批函数名（paneSend/chatOf…）。
 *
 * ## l10n 的两个坑（上一轮实测踩过，写下来免得再踩）
 *
 *   ① `header` / `sidenav` 的 `l10nID` 是**必填**，而且 FTL 里必须写成
 *      **只有名字、值留空** 的条目（`zotero-kb-kbview-header =`）。写错（比如给它
 *      加个值）会让 Zotero 的 `translateFragment` 把节点内容抹掉 —— 结果是
 *      "分区标题空白、`onRender` 拿到的 body 是 null"，而且**不报错**。
 *   ② zh-CN 与 en-US 两个 ftl 都要加，键名一致。
 *
 * ## 为什么正文不在这里生成
 *
 * 纲要（`views/<KEY>.outline.md`）是 Python 侧按节调模型生成的（一篇学位论文要几分钟）。
 * 窗格只**读现成的**：没有就显示"这一级还没生成" + 生成入口提示。
 */
Object.assign(ZoteroKB, {

  KBVIEW_ID: "zotero-kb-kbview",
  kbviewPaneID: null,

  /**
   * XHTML 命名空间。
   *
   * ⚠ 这个常量是**修 bug 用的**（2026-10-05 实测）：Zotero 主窗口是 XUL 文档，
   *   `doc.createElement("div")` 建出来的元素在 **XUL 命名空间**里 —— 给它设
   *   `innerHTML` 时按 **XML** 解析：片段里只要有一个没闭合的标签（比如 `<br>`），
   *   **整段内容会被丢掉**，而且不报错（用户看到的正是"提示行显示读了 34414 字、
   *   正文一片空白"）。用 `createElementNS(XHTML, …)` 建元素就会按 HTML 解析。
   */
  KB_XHTML: "http://www.w3.org/1999/xhtml",

  /**
   * 把一段 HTML **片段**灌进容器（对两种解析器都稳）。
   *
   * ⚠ 为什么不用 `el.innerHTML = html`（2026-10-05 实测）：
   *   主窗口是 XUL 文档。往 XHTML 命名空间的元素设 innerHTML 会按 HTML 解析、
   *   往 XUL 命名空间的元素设则按 **XML** 解析 —— 后者遇到一点不合规（未闭合的
   *   `<br/>`、XML 不认识的实体、MathML 里的边角）就**整段丢弃且不报错**
   *   （现象：提示行写着"读了 34414 字"、正文一片空白；用户反馈「每页正文完全
   *   打不开，每一篇都是」，而带 32 个 `<math>` 的正文与 0 个 `<math>` 的摘要
   *   表现不同，正好对上）。
   * 现在统一：DOMParser 按 text/html 解析 → importNode 到本容器。
   * HTML 解析是**宽容**的，片段里有什么都能落地；解析不了也只会少几个节点，
   * 不会整段消失。真出异常时把异常文字显示出来（下次一眼看到原因）。
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
          // ⚠⚠ 这里**绝不能**写成 `while (root.firstChild) { frag.appendChild(
          //   doc.importNode(root.firstChild, true)) }` —— `importNode` 返回的是
          //   **副本**、不会移除原节点，于是 `firstChild` 永远为真 → **死循环**
          //   疯狂追加节点 → 内存吃爆 → **Zotero 卡死并崩溃**（2026-10-05 实测，
          //   用户装机后"打开就卡住然后崩了"）。
          //   一次 import 整棵 wrapper 最简、最不可能出错。
          el.appendChild(doc.importNode(root, true));
          return true;
        }
      }
    } catch (e) {
      // 落到下面的兜底
      try {
        el.textContent = "（渲染出错：" + e + "）";
      } catch (e2) { /* ignore */ }
      return false;
    }
    try {
      el.innerHTML = String(text == null ? "" : text);
      return true;
    } catch (e) {
      try {
        el.textContent = "（渲染出错：" + e + "）";
      } catch (e2) { /* ignore */ }
      return false;
    }
  },

  /** 建一个 XHTML 命名空间的元素（窗格里凡是会装 HTML 的都该用它）。 */
  kbviewEl: function (doc, tag) {
    return doc.createElementNS(ZoteroKB.KB_XHTML, tag);
  },

  /** 注册分区（缺 ItemPaneManager 就如实记状态，不抛）。 */
  registerKbViewSection: function () {
    var self = ZoteroKB;
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
        try {
          setEnabled(self.kbviewEnabled(item, tabType));
        } catch (e) { /* ignore */ }
      },
      onRender: ({ doc, body, item }) => self.kbviewRender(doc, body, item),
    });
    self.writeStatusFile({
      kbViewPane: self.kbviewPaneID ? ("ok: " + self.kbviewPaneID)
                                    : "registerSection 返回 false",
    });
    return !!self.kbviewPaneID;
  },


  /** 卸载（插件 disable/卸载时调；不调会留下一个打不开的空分区）。 */
  unregisterKbViewSection: function () {
    var self = ZoteroKB;
    if (self.kbviewPaneID && Zotero.ItemPaneManager
        && Zotero.ItemPaneManager.unregisterSection) {
      Zotero.ItemPaneManager.unregisterSection(self.kbviewPaneID);
    }
    self.kbviewPaneID = null;
  },


  /**
   * 把分区的**可见标题**设成「知识库预览」。
   *
   * 为什么要手动做这一步：`header.l10nID` 指向的 FTL 条目是**属性形态**
   * （只有 `.label`、值为空）—— Fluent 只会把文案写进元素的 `label` 属性，
   * 所以必须给分区元素加 `data-l10n-attrs="label"`；在 Fluent 翻完之前，
   * 再用普通条目 `zotero-kb-kbview-title` 兜一次，免得标题短暂空白
   * （旧窗格代码里验证过的写法，见 git 历史 19-itempane.js 的 setupSectionHeader）。
   */
  setupKbViewHeader: function (body) {
    var self = ZoteroKB;
    try {
      let sec = body && body.closest ? body.closest("collapsible-section") : null;
      if (!sec && body && body.parentElement && body.parentElement.closest) {
        sec = body.parentElement.closest("collapsible-section");
      }
      if (!sec) return false;
      if (sec.getAttribute("data-l10n-attrs") !== "label") {
        sec.setAttribute("data-l10n-attrs", "label");
      }
      if (!sec.getAttribute("label")) {
        let msg = "";
        try {
          msg = Zotero.ftl && Zotero.ftl.formatValueSync
            && Zotero.ftl.formatValueSync("zotero-kb-kbview-title");
        } catch (e) { /* ignore */ }
        sec.setAttribute("label", msg || "知识库预览");
      }
      return true;
    } catch (e) {
      return false;
    }
  },

  /** 只有普通条目（有 pdf/元数据的那些）才显示这个分区。 */
  kbviewEnabled: function (item, tabType) {
    if (!item) return false;
    // ⚠ 2026-10-05：这里原来只认 `library`（照抄了被删掉的对话窗格），
    //   于是**打开 PDF 的阅读器页签里分区整个消失**（用户截图反馈：
    //   "在文献打开后就没有插件图标了"）。而"对着 PDF 读纲要"恰恰是最常用的场景，
    //   所以 reader 必须开。未知页签类型也放行（有 item 就能显示）。
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

  /** 字号（px，只作用于本分区）。12~18，默认 13。 */
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
      if (v >= 10 && v <= 24) {
        Zotero.Prefs.set(self.PREFS.kbviewFont, v);
      }
    } catch (e) { /* ignore */ }
  },

  setKbviewLevel: function (id) {
    var self = ZoteroKB;
    // ⚠ 写 pref 用 Zotero.Prefs.set：插件里**没有** setPref 这个包装
    //   （第一版我照着读的那半边想当然写了，真机一渲染就"不是函数"）。
    try {
      Zotero.Prefs.set(self.PREFS.kbviewLevel, String(id || ""));
    } catch (e) { /* ignore */ }
  },


  /** 某个级别的 md 在磁盘上的绝对路径（与 Python 侧 kbviews.level_path 同一套拼接）。 */
  kbviewPath: function (levelId, key) {
    // ⚠ 别自己拼路径：`kbLevels(key)` 已经按同一套规则算好 `path`（并且带 exists）。
    //   第一版我写了 `self.kbDirPath()` —— 插件里根本没有这个名字（是 `kbDir()`），
    //   真机一渲染就会抛。
    const lv = (ZoteroKB.kbLevels(key) || [])
      .find((x) => x.id === levelId);
    return lv ? lv.path : "";
  },


  /**
   * 决定"这次读哪一级"：先看用户选的那一级，文件不在就依次退到
   * 分节纲要 → 摘要与要点 → 完整档案，都读不到就返回 `{levelId:"", why:…}`。
   *
   * `existsFn(id) => bool` 由调用方注入（真机读磁盘、测试传假函数）——
   * 逻辑是纯的，所以能单测。
   */
  kbviewOrder: function (prefer) {
    // 首选 → 分节纲要（中间层，最合"对着原文读"）→ 摘要级 → 完整档案
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


  /** 渲染分区：一行工具条（级别下拉 + 重新读取）+ markdown 正文。 */
  kbviewRender: async function (doc, body, item) {
    var self = ZoteroKB;
    if (!doc || !body) return;
    self.setupKbViewHeader(body);      // 可见标题「知识库预览」
    // 清掉上一次的内容（Zotero 会复用 body）
    try {
      const old = body.querySelector(".zotero-kb-kbview");
      if (old) old.remove();
    } catch (e) { /* ignore */ }

    const wrap = self.kbviewEl(doc, "div");
    wrap.className = "zotero-kb-kbview";
    // 字号：只在本容器上设（不动 Zotero 的默认设置）。
    // 限高 + 内部滚动：正文再长也只在我们这一块里滚，不去挤 Zotero 自己的分区
    //（用户反馈"我的插件窗口总被上面的挤位置"）。
    // ⚠ 滚动条要加在**正文容器**上，不能加在 wrap 上 ——
    //   加在 wrap 上会把工具条（级别/字号/重新读取/提示）一起滚上去
    //   （用户反馈：「滚动会把完整档案、字号都滚上去，这个应该保持在顶部」）。
    //   结构：wrap 是竖向 flex + 限高，bar 固定，view 自己滚。
    wrap.setAttribute("style",
      "padding:4px 6px; font-size:" + self.kbviewFont() + "px; line-height:1.55;"
      + " max-height:85vh; display:flex; flex-direction:column; overflow:hidden;");

    // 工具条：**单行不换行**（用户反馈：提示文字换行后"重新读取"被挤到第二行）。
    // 做法：select/按钮 nowrap，提示占剩余宽度并省略号截断（min-width:0 是关键，
    // flex 子项默认 min-width:auto 不会收缩）。
    const bar = self.kbviewEl(doc, "div");
    bar.setAttribute("style", "display:flex; gap:6px; align-items:center;"
      + " flex-wrap:nowrap; margin-bottom:4px; flex:0 0 auto;");
    const sel = self.kbviewEl(doc, "select");
    // ⚠ 原来写了 `max-width:7.5em` —— 窗格里 select 的字号继承自 Zotero（比 12px 大），
    //   加上下拉箭头就截断成「摘要与要」（用户截图反馈）。改成按内容自适应。
    sel.setAttribute("style", "flex:0 0 auto; white-space:nowrap;");
    for (const lv of (self.kbLevels() || [])) {
      const op = self.kbviewEl(doc, "option");
      op.setAttribute("value", lv.id);
      op.textContent = lv.label;
      sel.appendChild(op);
    }
    sel.value = self.kbviewLevel();
    // 字号下拉（小/标准/较大/大）—— 只改这一块的字号
    const fontSel = self.kbviewEl(doc, "select");
    fontSel.setAttribute("style", "flex:0 0 auto; white-space:nowrap;");
    for (const opt of [[12, "小"], [13, "标准"], [15, "较大"], [17, "大"],
                       [20, "特大"]]) {
      const o = self.kbviewEl(doc, "option");
      o.setAttribute("value", String(opt[0]));
      o.textContent = opt[1];
      fontSel.appendChild(o);
    }
    fontSel.value = String(self.kbviewFont());
    fontSel.addEventListener("change", () => {
      self.setKbviewFont(fontSel.value);
      wrap.setAttribute("style",
        "padding:4px 6px; font-size:" + self.kbviewFont() + "px; line-height:1.55;"
        + " max-height:85vh; display:flex; flex-direction:column; overflow:hidden;");
    });

    const btn = self.kbviewEl(doc, "button");
    btn.textContent = "重新读取";
    btn.setAttribute("style", "padding:1px 6px; flex:0 0 auto; white-space:nowrap;");
    const hint = self.kbviewEl(doc, "span");
    hint.setAttribute("style", "opacity:0.65; font-size:11px; flex:1 1 auto;"
      + " min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;");
    hint.setAttribute("title", "");
    bar.append(sel, fontSel, btn, hint);
    wrap.append(bar);

    const view = self.kbviewEl(doc, "div");
    view.className = "zotero-kb-kbview-body";
    // 只有正文滚动（min-height:0 是 flex 子项能收缩的关键）
    view.setAttribute("style", "flex:1 1 auto; min-height:0; overflow:auto;");
    wrap.append(view);
    body.append(wrap);

    const paint = async (prefer) => {
      hint.textContent = "读取中…";
      // 存在性直接问 `kbLevels(key)`（它内部用同步 `_exists`，且路径已经算好），
      // 再交给纯函数 `kbviewPickLevel` 决策 —— 真机与测试走同一条规则。
      const levels = self.kbLevels(item.key);
      const byId = {};
      for (const lv of levels) byId[lv.id] = lv;
      const picked = self.kbviewPickLevel(
        prefer, (id) => !!(byId[id] && byId[id].exists));
      if (!picked.levelId) {
        view.textContent = "";
        const p = self.kbviewEl(doc, "div");
        p.setAttribute("style", "opacity:0.75;");
        // 把"知识库目录 + 试过的路径"一并显示：用户反馈"有的文献打不开"时，
        // 这一行就能看出是"没建索引"还是"知识库位置没定下来"（省一轮来回）。
        p.textContent = "知识库里还没有这一篇的任何分级文件。"
          + "先在管理面板「手动更新」建一次索引，"
          + "或用右键菜单「重建本条目知识库」。";
        const probe = self.kbviewEl(doc, "div");
        probe.setAttribute("style", "opacity:0.6; font-size:11px; margin-top:4px;"
          + " word-break:break-all;");
        const kb = self.kbDir();
        probe.textContent = (kb ? ("知识库目录：" + kb + "；")
                                : "知识库目录未确定（面板「运行环境」→ 服务状态）；")
          + "试过：" + (byId[self.kbviewLevel()] || {}).path;
        view.append(probe);
        view.append(p);
        hint.textContent = "";
        return;
      }
      const path = (byId[picked.levelId] || {}).path
        || self.kbviewPath(picked.levelId, item.key);
      // 优先读同名 `.html`（LaTeX 已渲染成 MathML，Firefox 原生显示公式）；
      // 没有才退回 md，由 md2html 现场渲染（那时公式是原文文本）。
      const htmlPath = path.replace(/\.md$/i, ".html");
      let text = "";
      let isHtml = false;
      try {
        if (self._exists(htmlPath)) {
          text = await IOUtils.readUTF8(htmlPath);
          isHtml = true;
        }
      } catch (e) { /* 读不到就走 md */ }
      if (!text) {
        try {
          text = await IOUtils.readUTF8(path);
        } catch (e) {
          text = "";
        }
      }
      const lvLabel = (byId[picked.levelId] || {}).label || picked.levelId;
      const hintText = lvLabel
        + (picked.fellBack ? "（默认那一级还没生成，已回退）" : "")
        + "　" + text.length + " 字"
        + (isHtml ? "　公式已渲染" : "　公式为原文文本");
      hint.textContent = hintText;
      hint.setAttribute("title", hintText);   // 截断了也能悬停看全
      self.kbviewSetHtml(doc, view, (isHtml ? text : self.md2html(text))
        || "<p style='opacity:0.75'>（这一级是空的）</p>");
      // 注入后如果容器是空的（极少数解析失败），说明白，别让用户对着空白猜
      if (!view.firstChild) {
        self.kbviewSetHtml(doc, view,
          "<p style='opacity:0.75'>这一级的内容没渲染出来（"
          + text.length + " 字已读到）—— 请把这一行报给维护者。</p>");
      }
    };

    sel.addEventListener("change", () => {
      self.setKbviewLevel(sel.value);
      paint(sel.value).catch(() => {});
    });
    btn.addEventListener("command", () => { paint(sel.value).catch(() => {}); });
    btn.addEventListener("click", () => { paint(sel.value).catch(() => {}); });

    await paint(self.kbviewLevel());
  },


  /**
   * 最小 markdown → HTML（**纯函数**，桩测试直接测它）。
   *
   * 覆盖知识库 md 里真正用到的语法：`#` 标题、`-`/数字列表、`>` 引用、
   * `**粗**`、`*斜*`、`` `代码` ``、`[文字](链接)`、`$$公式$$`、`![图注](图片)`、
   * 空行分段。**不做表格/嵌套列表** —— 我们的视图里没有。
   *
   * ⚠ 先转义 HTML 再套标记：md 是我们自己生成的，但笔记原文里有 `<`、`&`
   *   （用户笔记里就有），不转义会让窗格渲染坏掉甚至注入。
   */
  md2html: function (md) {
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s)
      // 行内公式：先处理，免得里面的 * 和 _ 被当成强调
      .replace(/\$\$([^$]+)\$\$/g,
               '<code style="background:rgba(127,127,127,0.15);'
               + ' padding:0 3px; border-radius:3px;">$1</code>')
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      // 图片：窗格里不加载本地图片（路径是 KB 相对路径），只留图注
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, "[图：$1]")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

    const out = [];
    let list = "";
    let buf = [];
    const flushBuf = () => {
      if (buf.length) {
        out.push("<p>" + buf.join("<br>") + "</p>");
        buf = [];
      }
    };
    const closeList = () => {
      if (list) { out.push("</" + list + ">"); list = ""; }
    };
    const lines = String(md == null ? "" : md).split(/\r?\n/);
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { flushBuf(); closeList(); continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushBuf(); closeList();
        const lvl = m[1].length;
        // em：跟随容器字号（窗格有字号调节，写死 px 会盖住它）
        const size = { 1: "1.35em", 2: "1.18em", 3: "1.06em" }[lvl] || "1em";
        out.push(`<div style="font-weight:600; font-size:${size}; margin:6px 0 2px;">`
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
      // 独立成行的公式块：$$…$$ 或 \[…\]
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
    return out.join("\n");
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
