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
   * ⚠ 只有**文件信箱**一条队列（2026-10 起）：`<知识库>\mailbox` —— 没有端口、
   *   没有 token、服务没跑也能干活。老的 HTTP 队列（GET /task）已随 8765 一起删除。
   *
   * 安全：只认本机文件系统里的那个目录；不想开这个能力就把 pref
   * `autoTaskPoll` 设为 false。
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
        // 只有文件信箱一条通道（领取一个就执行一个）。
        let task = null;
        try {
          task = await self.pollTaskMailbox();
        } catch (e) {
          Zotero.debug("[zotero-kb] 任务信箱轮询失败：" + e);
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
    // 结果只回文件信箱（任务就是从那里领的）。
    try {
      await self.reportTaskMailbox(task, ok, error,
        { value: self.safeStringify(result), ms: ms, pluginVersion: self.version });
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
