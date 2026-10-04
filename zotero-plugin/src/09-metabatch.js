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
