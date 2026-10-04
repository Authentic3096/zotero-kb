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
