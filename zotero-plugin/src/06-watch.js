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
