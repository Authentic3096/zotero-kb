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
