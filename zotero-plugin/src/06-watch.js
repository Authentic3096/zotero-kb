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
