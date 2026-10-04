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
