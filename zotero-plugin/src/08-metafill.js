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
