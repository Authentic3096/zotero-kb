/* eslint-disable no-undef */
/**
 * 19-itempane.js —— Zotero **右侧内容窗格**里的「本地模型」分区。
 *
 * 它给每篇文献一个本地模型聊天框：按需注入摘要级/全文级上下文提问、
 * 逐段核对正文提取质量、模型想改知识库时走**写入确认**。
 *
 * ## 四条硬规则（用户明确要求，别顺手改掉）
 *
 * 1. **对话不落盘**：消息只在这个对象的 `chatState` 里（内存），退出 Zotero
 *    就没了。落盘的只有"检查进度与结论"（para_check）和"用户确认过的修正"
 *    （fulltext_patch / para_override）—— 否则"续跑"和"改过再改"都做不到。
 *    退出前用 `quit-application-requested` 提醒一次（可勾"下次不再提示"）。
 * 2. **模型只提议，落库要人点**：写入模式先列出改动 → 「确认」→ **二次确认**
 *    （第二次**没有**"下次不再提示"）→ 才 POST /kb-apply（还必须带 confirm=yes）。
 * 3. **结论与客观信号并排显示**：逐段检查的 verdict 旁边永远显示服务端算出来的
 *    符号占比、异域码位、重复度这些客观量。check_chunks.py 记过一次教训：
 *    让模型自己对切片"读着像不像句子"打分，判错 60%，同一模型对同一文本给出
 *    相反结论 —— 所以结论不许当唯一依据。
 * 4. **注入量如实报**：`已注入 N/M 字符`，超预算按页取样时明确写出来。
 *    静默截断会让用户以为"模型看过全文"。
 *
 * ## 为什么 UI 全靠 `doc.createElement` 建
 *
 * `registerSection` 的 `onRender` 给的是 `{doc, body}`：body 就是个普通 DOM
 * 容器，Zotero 不给任何现成控件样式。所以这里自己建 DOM + 行内样式
 * （不引外部 CSS 文件：XUL 文档里加载样式是另一套机制，本项目不想为几个
 * 按钮再加一种失败模式）。文案一律走 ftl 的 `l10nID`（见 locale/ 目录）。
 */
Object.assign(ZoteroKB, {

  // 内容窗格分区的 ID（注销时要用同一个）
  PANE_ID: "zotero-kb-chat",

  // 会话状态：item.key -> {...}。**只在内存**，见文件头第 1 条。
  chatState: {},


  chatOf: function (key) {
    var self = ZoteroKB;
    if (!self.chatState[key]) {
      self.chatState[key] = {
        messages: [],        // [{role, content}]，约定与 OpenAI 一致
        inject: "tldr",      // 上次用的注入方式
        injected: null,      // 上次注入的结果（含 chars/total/truncated/note）
        mode: "chat",        // chat | para | write
        para: null,          // 逐段检查状态机（见 paraUiStart）
        write: null,         // 写入模式待确认的计划
        ui: null,            // 当前 DOM 引用（换条目/重建时替换）
        busy: false,
        armedPara: null,     // 阅读器里选中的文字定位到的段落（20-reader.js 写）
      };
    }
    return self.chatState[key];
  },


  /**
   * 给元素设 ftl 文案。
   *
   * `l10nID` 必须在 locale/*.ftl 里有定义 —— `tools/check_plugin.py` 会静态检查
   * （bootstrap.js 里出现的每个 l10nID 都要在 ftl 里找到）。这里失败时退回显示
   * **id 本身**：至少界面上能看出是哪一条缺了，而不是空白。
   */
  l10n: function (doc, el, id, args) {
    try {
      if (doc && doc.l10n && doc.l10n.setAttributes) {
        doc.l10n.setAttributes(el, id, args || undefined);
        return el;
      }
    } catch (e) { /* 落到下面 */ }
    el.textContent = id;
    return el;
  },


  /** 建一个按钮：行内样式，超小字体（内容窗格本来就窄）。 */
  paneButton: function (doc, l10nID, onClick, opts) {
    var self = ZoteroKB;
    const b = doc.createElement("button");
    b.setAttribute("type", "button");
    b.classList.add("zotero-kb-btn");
    self.l10n(doc, b, l10nID);
    b.style.cssText = [
      "font-size:11px", "padding:2px 6px", "margin:0 4px 4px 0",
      "cursor:pointer", "border-radius:3px",
      "border:1px solid rgba(128,128,128,.5)",
      "background:transparent", "color:inherit",
    ].join(";");
    if (opts && opts.tip) b.title = opts.tip;
    if (onClick) b.addEventListener("click", onClick);
    return b;
  },


  // ================================================================ 注册与生命周期

  registerItemPane: function () {
    var self = ZoteroKB;
    if (!Zotero.ItemPaneManager || !Zotero.ItemPaneManager.registerSection) {
      // 老版本 Zotero 没有这个 API —— 不 throw（否则整个插件启动失败），
      // 只写进状态文件，排查时能看到"为什么没有这个分区"。
      try {
        self.writeStatusFile({ itemPane: "unavailable: Zotero.ItemPaneManager 不存在" });
      } catch (e) { /* ignore */ }
      return;
    }
    const iconHeader = self.rootURI + "toolbar-icon.svg";   // 16×16
    const iconSidenav = self.rootURI + "sidenav-icon.svg";  // 20×20
    self.paneID = Zotero.ItemPaneManager.registerSection({
      paneID: self.PANE_ID,
      pluginID: self.id,
      // ⚠ header / sidenav 的 l10nID 是**必填**（见 pluginAPI 的 schema）：
      //   指向的 id 必须在 locale/*.ftl 里，否则分区标题是空白、且没有报错。
      header: { l10nID: "zotero-kb-pane-header", icon: iconHeader },
      sidenav: { l10nID: "zotero-kb-pane-sidenav", icon: iconSidenav },
      onInit: ({ doc, body }) => self.paneOnInit(doc, body),
      onItemChange: ({ item, tabType, setEnabled }) => {
        // 只有**知识库里**的条目才启用。判据用已有的权重缓存（每几秒刷新），
        // 这样不需要为每次选中条目发一次网络请求。
        const inKb = !!(item && item.isRegularItem && item.isRegularItem()
          && self.weightsCache[item.key]);
        setEnabled(!!inKb);
      },
      onRender: ({ doc, body, item }) => self.paneRender(doc, body, item),
      onDestroy: ({ body }) => self.paneOnDestroy(body),
      sectionButtons: [{
        type: "clear",
        icon: iconHeader,
        l10nID: "zotero-kb-btn-clear",
        onClick: ({ item }) => self.paneClear(item),
      }],
    });
    try {
      self.writeStatusFile({
        itemPane: self.paneID ? ("ok: " + self.paneID) : "registerSection 返回 false",
      });
    } catch (e) { /* ignore */ }
  },


  /**
   * 退出前提醒"对话不会保存"。
   *
   * 为什么用 `quit-application-requested` 而不是在 shutdown 里弹：
   *   · shutdown 阶段已经太晚（模态框可能让退出卡住）；
   *   · 这个观察点**可以取消退出**（设置 `subject.data = true`），
   *     所以用户能"点一下取消、先看看再退"。
   * 复选框走 `Services.prompt.confirmEx` 的第 8/9 个参数（本项目的
   * askOneMeta 里已经用过同一个手法，说明见那段注释）。
   */
  registerQuitGuard: function () {
    var self = ZoteroKB;
    if (!self.quitObserver) {
      self.quitObserver = {
        observe: function (subject) {
          try {
            if (!self.hasUnsavedChat()) return;
            if (!self.getPref(self.PREFS.chatQuitWarn, true)) return;
            const ps = Services.prompt;
            const check = { value: false };
            const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
              + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
            const title = self.l10nText("zotero-kb-quit-title", "退出前提醒");
            const text = self.l10nText("zotero-kb-quit-body",
              "这个窗格里的对话不会被保存，退出 Zotero 就清空了。"
              + "检测进度与已确认的修正会保留。");
            const dont = self.l10nText("zotero-kb-quit-dontask", "下次不再提示");
            const rv = ps.confirmEx(Zotero.getMainWindow(), title, text, flags,
              "退出", "先不退", null, dont, check);
            if (check.value) {
              try { Zotero.Prefs.set(self.PREFS.chatQuitWarn, false); } catch (e) { /* ignore */ }
            }
            if (rv === 1 && subject) subject.data = true;   // 取消退出
          } catch (e) {
            // 退出路径上**绝不能**抛（会让 Zotero 退不干净）；记进状态文件
            try { self.writeStatusFile({ quitGuardError: String(e) }); } catch (e2) { /* ignore */ }
          }
        },
      };
    }
    Services.obs.addObserver(self.quitObserver, "quit-application-requested");
  },


  unregisterQuitGuard: function () {
    var self = ZoteroKB;
    if (!self.quitObserver) return;
    try {
      Services.obs.removeObserver(self.quitObserver, "quit-application-requested");
    } catch (e) { /* ignore */ }
  },


  /** 有没有"还没清空的对话"（决定退出时要不要提醒）。 */
  hasUnsavedChat: function () {
    var self = ZoteroKB;
    for (const key in self.chatState) {
      const st = self.chatState[key];
      if (st && ((st.messages && st.messages.length) || st.mode === "para")) return true;
    }
    return false;
  },


  /**
   * 取一条 ftl 文案（给**弹窗**用）。
   *
   * 弹窗（confirmEx）要的是字符串，而 ftl 是给 DOM 元素设属性的。这里用
   * Fluent 的 `formatValue` 拿字符串；拿不到就退回调用方给的默认中文。
   */
  l10nText: function (id, fallback, args) {
    try {
      const l10n = (Zotero.getMainWindow() || {}).document
        && Zotero.getMainWindow().document.l10n;
      if (l10n && l10n.formatValue) {
        const p = l10n.formatValue(id, args || undefined);
        // Fluent 返回 Promise；弹窗是同步 API，所以这里只能"用缓存/默认值"。
        // 取不到就退回默认中文（**不阻塞退出**）。
        p.then((s) => { ZoteroKB._l10nCache[id] = s; }).catch(() => {});
      }
    } catch (e) { /* ignore */ }
    return (ZoteroKB._l10nCache && ZoteroKB._l10nCache[id]) || fallback;
  },

  _l10nCache: {},


  // ================================================================ 渲染

  paneOnInit: function (doc, body) {
    // 初始化时先不建 UI（onItemChange 才知道是哪一篇；onRender 才给 item）
    body.classList.add("zotero-kb-pane");
  },


  paneOnDestroy: function (body) {
    var self = ZoteroKB;
    // UI 引用清掉：DOM 被 Zotero 销毁后还指着它，下一次 onRender 会写进空气
    for (const key in self.chatState) {
      if (self.chatState[key] && self.chatState[key].ui
          && self.chatState[key].ui.body === body) {
        self.chatState[key].ui = null;
      }
    }
  },


  paneRender: function (doc, body, item) {
    var self = ZoteroKB;
    if (!item) return;
    const st = self.chatOf(item.key);
    body.textContent = "";
    const ui = { doc: doc, body: body, item: item, key: item.key };
    st.ui = ui;

    // ---- 状态行（注入量 / 逐段进度都在这儿）
    const status = doc.createElement("div");
    status.style.cssText = "font-size:11px;opacity:.75;margin:2px 0 6px 0;"
      + "white-space:pre-wrap;";
    ui.status = status;
    body.appendChild(status);

    // ---- 对话区（可滚动）
    const log = doc.createElement("div");
    log.style.cssText = "max-height:320px;min-height:120px;overflow:auto;"
      + "border:1px solid rgba(128,128,128,.28);border-radius:4px;"
      + "padding:6px;margin-bottom:6px;font-size:12px;line-height:1.5;";
    ui.log = log;
    body.appendChild(log);

    // ---- 逐段检查的详情区（模式为 para 时才填）
    const detail = doc.createElement("div");
    detail.style.cssText = "font-size:11px;line-height:1.5;";
    ui.detail = detail;
    body.appendChild(detail);

    // ---- 输入区
    const ta = doc.createElement("textarea");
    ta.rows = 2;
    ta.style.cssText = "width:100%;box-sizing:border-box;font-size:12px;"
      + "resize:vertical;margin-bottom:4px;";
    // ⚠ 这里**不能**走 self.l10n()：那个是给元素设 textContent 的，而
    //   textarea 的 textContent 就是它的**内容** —— 会把提示语变成输入框里
    //   真实存在的文字。placeholder 要走 FTL 的属性注入形式。
    ta.setAttribute("data-l10n-id", "zotero-kb-placeholder-ask");
    ta.setAttribute("data-l10n-attrs", "placeholder");
    ui.input = ta;
    body.appendChild(ta);

    const row = doc.createElement("div");
    ui.row = row;
    body.appendChild(row);

    row.appendChild(self.paneButton(doc, "zotero-kb-btn-send", () => self.paneSend(ui)));
    row.appendChild(self.paneButton(doc, "zotero-kb-btn-inject-tldr",
      () => self.paneInject(ui, "tldr"), { tip: "把摘要级视图放进上下文" }));
    row.appendChild(self.paneButton(doc, "zotero-kb-btn-inject-full",
      () => self.paneInject(ui, "full"), { tip: "把全文放进上下文（超预算按页取样）" }));
    row.appendChild(self.paneButton(doc, "zotero-kb-btn-para",
      () => self.paraStart(ui), { tip: "逐段核对正文提取质量" }));
    row.appendChild(self.paneButton(doc, "zotero-kb-btn-locate",
      () => self.paneLocate(ui), { tip: "把选中/粘贴的文字定位到段落" }));
    // 「整理这次讨论」：模型把对话里"该记的/该改的"整理成建议（**不写库**）。
    // 为什么要一个按钮：/chat 是纯问答，没有"工具调用"这一层，
    //   所以需要一个显式动作把"要不要落库"这件事提出來。
    row.appendChild(self.paneButton(doc, "zotero-kb-btn-propose",
      () => self.panePropose(ui), { tip: "把这次讨论整理成改动建议（确认后才写）" }));

    self.panePaint(ui);
  },


  /** 把内存里的会话状态画出来（切条目、追加消息后都调它）。 */
  panePaint: function (ui) {
    var self = ZoteroKB;
    if (!ui || !ui.log) return;
    const st = self.chatOf(ui.key);
    ui.log.textContent = "";
    if (!st.messages.length) {
      const hint = ui.doc.createElement("div");
      hint.style.opacity = ".6";
      self.l10n(ui.doc, hint, "zotero-kb-placeholder-ask");
      ui.log.appendChild(hint);
    }
    for (const m of st.messages) {
      const d = ui.doc.createElement("div");
      d.style.cssText = "margin:3px 0;white-space:pre-wrap;"
        + (m.role === "user" ? "opacity:.95;" : "opacity:.85;");
      d.textContent = (m.role === "user" ? "我：" : "模型：") + m.content;
      ui.log.appendChild(d);
    }
    ui.log.scrollTop = ui.log.scrollHeight;

    // 状态行：注入量 + 逐段进度
    const bits = [];
    if (st.injected && st.injected.chars) {
      bits.push("已注入 " + st.injected.chars
        + (st.injected.total_chars && st.injected.total_chars !== st.injected.chars
          ? ("/" + st.injected.total_chars) : "") + " 字符"
        + (st.injected.truncated ? "（按页取样）" : ""));
    }
    if (st.para && st.para.plan) {
      bits.push("已查 " + (st.para.plan.checked || 0) + "/"
        + (st.para.plan.total || 0) + " 段");
    }
    if (st.busy) bits.push("正在想…");
    ui.status.textContent = bits.join("　·　");
  },


  // ================================================================ 动作

  paneSend: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const q = (ui.input.value || "").trim();
    if (!q || st.busy) return;
    st.messages.push({ role: "user", content: q });
    ui.input.value = "";
    st.busy = true;
    self.panePaint(ui);

    // 计时器：本地模型一次要几秒到几十秒，不给反馈用户会以为卡住了。
    // ⚠ 用**递归 setTimeout** 而不是 setInterval：Zotero 插件沙箱里
    //   setInterval 可能"创建成功但从不触发"（本机实测：taskPolling=true
    //   而 tickCount=0，任务队列永远 pending；tools/audit_plugin_api.py 把
    //   这条列为硬性问题）。同一句话在本仓库 18-taskpoll.js 里也记着。
    let secs = 0;
    let tickTimer = null;
    let thinking = true;
    const tick = () => {
      if (!thinking) return;
      secs += 1;
      if (ui.status) ui.status.textContent = "正在想…（已 " + secs + " 秒）";
      tickTimer = setTimeout(tick, 1000);
    };
    tickTimer = setTimeout(tick, 1000);
    try {
      const res = await self.request("POST", "/chat", {
        key: ui.key,
        messages: st.messages.slice(-12),      // 只带最近几轮：本地模型上下文小
        inject: st.inject,
      });
      if (res && res.ok) {
        st.messages.push({ role: "assistant", content: res.text || "(空回复)" });
        st.injected = res.injected || st.injected;
      } else {
        st.messages.push({
          role: "assistant",
          content: "（调用失败：" + ((res && res.error) || "未知错误")
            + ((res && res.hint) ? "；" + res.hint : "") + "）",
        });
      }
    } catch (e) {
      st.messages.push({ role: "assistant", content: "（请求失败：" + e + "）" });
    } finally {
      thinking = false;
      if (tickTimer) clearTimeout(tickTimer);
      st.busy = false;
      self.panePaint(ui);
    }
  },


  paneInject: async function (ui, mode) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    st.inject = mode;
    // 注入本身**不调模型**：/chat-context 只把上下文拼出来并回报字符数，
    // 用户立刻能看到"已经装进去了多少"。（原来这里发了一条假的 /chat 请求，
    // 白白跑一次推理 —— 本地 4B 一次好几秒，纯浪费。）
    try {
      const res = await self.request("POST", "/chat-context", {
        key: ui.key, inject: mode,
      });
      st.injected = (res && res.injected) || null;
      if (st.injected && st.injected.note) ui.status.textContent = st.injected.note;
    } catch (e) {
      ui.status.textContent = "注入失败：" + e;
    }
    self.panePaint(ui);
  },


  // ---------------------------------------------------------------- 逐段检测

  paraStart: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    st.mode = "para";
    st.para = { plan: null, queue: [], index: 0, current: null, adopted: [],
                prefetch: null, includeFixed: false, busy: false };
    ui.detail.textContent = "";
    self.panePaint(ui);
    await self.paraLoadPlan(ui);
  },


  paraLoadPlan: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const res = await self.request("POST", "/para-plan", {
      key: ui.key,
      scope: "suspect",
      include_fixed: !!(st.para && st.para.includeFixed),
    });
    if (!res || !res.ok) {
      ui.detail.textContent = "拿不到逐段计划：" + ((res && res.error) || "未知错误");
      return;
    }
    st.para.plan = res;
    st.para.queue = (res.items || []).filter((it) => !it.skip);
    st.para.index = 0;
    self.paraShowCurrent(ui);
  },


  /** 显示当前这一段（有预取就用预取的结果，省一次等待）。 */
  paraShowCurrent: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const P = st.para;
    if (!P) return;
    const item = P.queue[P.index];
    if (!item) {
      ui.detail.textContent = "这一段之后没有待查的段落了。";
      self.paraRenderButtons(ui, { done: true });
      return;
    }
    ui.detail.textContent = "正在检查 p." + item.page + " 第 "
      + (item.logical_index + 1) + " 段（" + item.chars + " 字）…";
    let res = P.prefetch && P.prefetch.hash === item.hash ? P.prefetch.result : null;
    if (!res) {
      res = await self.paraCheckOne(ui, item);
    }
    P.current = { item: item, result: res };
    P.prefetch = null;
    self.paraRender(ui);
    // 预取下一段：用户读这一段的时候，下一段已经在算了
    self.paraPrefetch(ui);
  },


  paraCheckOne: async function (ui, item) {
    var self = ZoteroKB;
    // ⚠ 只发 **hash**，不发正文：`/para-plan` 的返回里本来就不含段落全文
    //   （全库 889 个可疑段、每段几百字，一次全带回来是几百 KB），
    //   服务端自己按 hash 去 fulltext md 里取出这一段与它的前后邻居。
    //   原来这里发的是 `item.text`（undefined → 空串），模型收到的是空段落。
    try {
      return await self.request("POST", "/para-check", {
        key: ui.key,
        hash: item.hash,
      });
    } catch (e) {
      return { ok: false, error: String(e), signals: item.signals || {} };
    }
  },


  paraPrefetch: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const P = st.para;
    if (!P) return;
    const item = P.queue[P.index + 1];
    if (!item) return;
    const res = await self.paraCheckOne(ui, item);
    P.prefetch = { hash: item.hash, result: res };
  },


  /**
   * 画一段的结论：**客观信号与模型结论并排**（文件头第 3 条）。
   *
   * 修的形态有三种，都要求"最小改动"（服务端已经把"整段重写"降级成
   * manual_only，不给可采用项）：
   *   · text  → 替换一小段原文
   *   · join  → 与上一段/下一段合并（"本来是同一段被误拆"）
   *   · 其它  → 只提示，不生成可采用项
   */
  paraRender: function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const P = st.para;
    if (!P || !P.current) return;
    const { item, result } = P.current;
    const doc = ui.doc;
    const box = ui.detail;
    box.textContent = "";

    const head = doc.createElement("div");
    head.style.cssText = "font-weight:600;margin-bottom:2px;";
    head.textContent = "p." + item.page + "　第 " + (item.logical_index + 1)
      + " 段　" + item.chars + " 字";
    box.appendChild(head);

    // 客观信号（先显示 —— 它是用户可以自己核对的那部分）
    const sig = doc.createElement("div");
    sig.style.cssText = "opacity:.75;margin-bottom:4px;";
    const s = (result && result.signals) || item.signals || {};
    sig.textContent = "信号：符号 " + self.pct(s.symbol_ratio)
      + "｜异域 " + self.pct(s.exotic_ratio)
      + "｜私有区 " + self.pct(s.private_ratio)
      + "｜与上段重复 " + self.pct(s.overlap_prev)
      + (s.matrix_marks ? ("｜矩阵括号 " + s.matrix_marks) : "")
      + ((item.why && item.why.length) ? ("\n为什么被挑出来：" + item.why.join("；")) : "");
    sig.style.whiteSpace = "pre-wrap";
    box.appendChild(sig);

    const verdict = doc.createElement("div");
    const v = (result && result.verdict) || (result && result.ok === false ? "失败" : "?");
    verdict.textContent = "模型："
      + ({ ok: "没问题", suspect: "可疑", damaged: "有损伤", unsure: "拿不准" }[v] || v)
      + "　" + ((result && result.kind) || "")
      + ((result && result.reason) ? ("　—　" + result.reason) : "");
    verdict.style.cssText = "margin-bottom:4px;white-space:pre-wrap;";
    if (result && result.error) {
      verdict.textContent += "（" + result.error + "）";
    }
    box.appendChild(verdict);

    // 可采用的修正
    if (result && result.ok && (result.before || result.join_with)) {
      const label = doc.createElement("label");
      label.style.cssText = "display:block;margin:4px 0;";
      const cb = doc.createElement("input");
      cb.setAttribute("type", "checkbox");
      cb.checked = true;                       // 默认采用，但必须用户点头
      cb.style.marginRight = "4px";
      const span = doc.createElement("span");
      if (result.join_with) {
        span.textContent = "把这一段接到"
          + (result.join_with === "prev" ? "上一段" : "下一段") + "（合并）";
      } else {
        span.textContent = "替换：「" + result.before + "」→「" + result.after + "」";
      }
      label.appendChild(cb);
      label.appendChild(span);
      box.appendChild(label);
      P.current.adopt = cb;
    } else if (result && result.manual_only) {
      const note = doc.createElement("div");
      note.style.opacity = ".75";
      note.textContent = "模型想整段重写 —— 这种不给可采用项，请你自己改。";
      box.appendChild(note);
    }

    // 原文（用户要能对照着看）—— 正文由服务端在结果里带回来
    // （插件只知道 hash，这是故意的：见 paraCheckOne 的说明）
    const pre = doc.createElement("pre");
    pre.style.cssText = "max-height:120px;overflow:auto;font-size:10px;"
      + "white-space:pre-wrap;opacity:.8;margin:4px 0;";
    pre.textContent = ((result && result.text) || "").slice(0, 600);
    box.appendChild(pre);

    self.paraRenderButtons(ui, {});
  },


  paraRenderButtons: function (ui, opts) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const doc = ui.doc;
    const bar = doc.createElement("div");
    bar.style.marginTop = "4px";
    const P = st.para || {};
    if (!opts.done) {
      // ⚠ 用户明确要求这里**只有两个按钮**：继续下一段 / 退出逐段检测
      bar.appendChild(self.paneButton(doc, "zotero-kb-btn-para-next",
        () => self.paraNext(ui)));
    }
    bar.appendChild(self.paneButton(doc, "zotero-kb-btn-para-exit",
      () => self.paraExit(ui)));
    const inc = doc.createElement("label");
    inc.style.cssText = "font-size:11px;opacity:.8;display:inline-block;";
    const cb = doc.createElement("input");
    cb.setAttribute("type", "checkbox");
    cb.checked = !!(P && P.includeFixed);
    cb.style.marginRight = "3px";
    cb.addEventListener("change", () => {
      P.includeFixed = cb.checked;
      self.paraLoadPlan(ui);
    });
    const sp = doc.createElement("span");
    self.l10n(doc, sp, "zotero-kb-para-include-fixed");
    inc.appendChild(cb);
    inc.appendChild(sp);
    if (P && P.plan && P.plan.stale) {
      const stl = doc.createElement("div");
      stl.style.cssText = "font-size:10px;opacity:.75;margin-top:2px;";
      self.l10n(doc, stl, "zotero-kb-para-stale", { n: P.plan.stale });
      bar.appendChild(stl);
    }
    bar.appendChild(inc);
    ui.detail.appendChild(bar);
  },


  paraNext: function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const P = st.para;
    if (!P || !P.current) return;
    // 1) 采用勾选过的修正（先攒着，退出时统一走写入确认）
    if (P.current.adopt && P.current.adopt.checked) {
      const r = P.current.result;
      if (r.join_with) {
        P.adopted.push({ kind: "join", p_hash: P.current.item.hash,
                         anchor: (P.current.item.text || "").slice(0, 40),
                         page: P.current.item.page,
                         with_prev: r.join_with === "prev" ? 1 : 0,
                         reason: r.reason || "" });
      } else if (r.before) {
        P.adopted.push({ kind: r.kind || "text", page: P.current.item.page,
                         before: r.before, after: r.after,
                         note: r.reason || "" });
      }
    }
    P.index += 1;
    self.paraShowCurrent(ui);
  },


  /**
   * 退出逐段检测：回到普通对话；**如果攒了修正，就进写入模式**。
   *
   * 为什么在这里成批确认而不是每段弹两次框：一段两下、几十段就是上百次点击；
   * 而且"每段看完再点继续"本身已经是逐条人工过目了 —— 写入那一刻**仍然**
   * 要确认（一次确认 + 二次确认，第二次没有"下次不再提示"）。
   */
  paraExit: function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const P = st.para;
    const adopted = (P && P.adopted) || [];
    st.mode = "chat";
    st.para = null;
    if (ui.detail) ui.detail.textContent = "";
    if (adopted.length) {
      self.enterWriteMode(ui, {
        patches: adopted.filter((a) => a.kind !== "join"),
        joins: adopted.filter((a) => a.kind === "join"),
      }, "逐段检测里采用的 " + adopted.length + " 处修正");
    } else {
      self.panePaint(ui);
    }
  },


  // ---------------------------------------------------------------- 定位

  /**
   * 把"用户选中的一段文字"定位到段落。
   *
   * 来源有两个：阅读器里的选中（20-reader.js 存进 `armedPara`）、
   * 输入框里粘的文字。**多候选就列出来让用户选** —— 猜错定位比不定位更糟。
   */
  paneLocate: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const typed = (ui.input.value || "").trim();
    const text = typed || (st.armedPara && st.armedPara.text) || "";
    if (!text) {
      ui.status.textContent = "先在 PDF 里选中一段文字，或把它粘到上面的输入框里";
      return;
    }
    const res = await self.request("POST", "/para-locate", { key: ui.key, text: text });
    if (!res || !res.ok) {
      ui.status.textContent = (res && res.error) || "定位失败";
      return;
    }
    if (res.best) {
      st.messages.push({
        role: "assistant",
        content: "定位到 p." + res.best.page + " 第 " + (res.best.logical_index + 1)
          + " 段：" + res.best.preview + "…",
      });
    } else if (res.matches && res.matches.length) {
      st.messages.push({
        role: "assistant",
        content: "有 " + res.matches.length + " 段都像，请你选一个：\n"
          + res.matches.map((m) => "· p." + m.page + " 第 "
            + (m.logical_index + 1) + " 段（" + Math.round(m.score * 100)
            + "%）：" + m.preview + "…").join("\n"),
      });
    } else {
      st.messages.push({ role: "assistant", content: "没找到对得上的段落。" });
    }
    self.panePaint(ui);
  },


  // ---------------------------------------------------------------- 写入模式

  /**
   * 进写入模式：把**将要写的东西**摆出来，等用户点确认。
   *
   * 两个入口：逐段检测退出时攒下的修正；或"把这次讨论整理成改动建议"
   * （POST /kb-propose，模型给建议，同样不写库）。
   */
  enterWriteMode: function (ui, plan, why) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    st.mode = "write";
    st.write = { plan: plan, why: why || "" };
    self.paintWrite(ui);
  },


  paintWrite: function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    const doc = ui.doc;
    const box = ui.detail;
    box.textContent = "";
    const w = st.write;
    if (!w) return;
    const plan = w.plan || {};

    const title = doc.createElement("div");
    title.style.cssText = "font-weight:600;margin-bottom:2px;";
    self.l10n(doc, title, "zotero-kb-write-title");
    box.appendChild(title);

    const note = doc.createElement("div");
    note.style.cssText = "opacity:.8;margin-bottom:4px;";
    self.l10n(doc, note, "zotero-kb-write-note");
    box.appendChild(note);

    const ul = doc.createElement("div");
    ul.style.cssText = "max-height:160px;overflow:auto;font-size:11px;";
    const lines = [];
    (plan.experiences || []).forEach((e) => {
      lines.push("经验：" + (e.asked || "").slice(0, 60)
        + "（" + (e.outcome || "unknown") + "）"
        + (e.suspect_unrelated ? "　⚠ 这条没关联任何文献，可能是与文献无关的记录" : ""));
    });
    (plan.weights || []).forEach((x) => {
      lines.push("权重：" + (x.key || ui.key) + "　重点=" + (x.pinned ? "是" : "否")
        + (x.note ? ("　备注：" + x.note) : ""));
    });
    (plan.patches || []).forEach((p) => {
      lines.push("正文修正（p." + (p.page || "?") + "）：「"
        + (p.before || "").slice(0, 40) + "」→「" + (p.after || "").slice(0, 40) + "」");
    });
    (plan.joins || []).forEach((j) => {
      lines.push("段落合并：把 p." + (j.page || "?") + " 的这段接到"
        + (j.with_prev ? "上一段" : "下一段"));
    });
    ul.textContent = lines.length ? lines.join("\n") : "（没有可写的内容）";
    ul.style.whiteSpace = "pre-wrap";
    box.appendChild(ul);

    const bar = doc.createElement("div");
    bar.style.marginTop = "4px";
    bar.appendChild(self.paneButton(doc, "zotero-kb-btn-confirm",
      () => self.confirmWrite(ui)));
    bar.appendChild(self.paneButton(doc, "zotero-kb-btn-cancel", () => {
      st.write = null;
      st.mode = "chat";
      ui.detail.textContent = "";
      self.panePaint(ui);
    }));
    box.appendChild(bar);
  },


  /**
   * 确认写入：**先二次确认**（这一次没有"下次不再提示"—— 用户明确要求），
   * 再 POST /kb-apply（服务端还要求 confirm="yes" 才写）。
   */
  confirmWrite: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    if (!st.write) return;
    const ps = Services.prompt;
    const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
      + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
    const t = self.l10nText("zotero-kb-confirm2-title", "再确认一次");
    const b = self.l10nText("zotero-kb-confirm2-body",
      "以下改动会写进知识库（经验 / 权重 / 正文修正），并立即影响检索排序。确认写入吗？");
    // ⚠ 第 8/9 个参数（复选框）这里**故意不传** —— 用户要求第二次确认
    //   不能再有"下次不再提示"。
    const rv = ps.confirmEx(Zotero.getMainWindow(), t, b, flags,
      "确认写入", "取消", null, null, {});
    if (rv !== 0) return;
    const res = await self.request("POST", "/kb-apply", {
      key: ui.key, plan: st.write.plan, confirm: "yes",
    });
    st.write = null;
    st.mode = "chat";
    ui.detail.textContent = "";
    const ok = res && res.ok;
    st.messages.push({
      role: "assistant",
      content: ok
        ? ("已写入：" + JSON.stringify((res.written || {})).slice(0, 300))
        : ("写入失败：" + ((res && (res.error || JSON.stringify(res.written || {})))
          || "未知错误")),
    });
    self.panePaint(ui);
  },


  /** 把这次讨论整理成改动建议（模型给建议，**不写库**）。 */
  panePropose: async function (ui) {
    var self = ZoteroKB;
    const st = self.chatOf(ui.key);
    if (!st.messages.length) {
      ui.status.textContent = "先聊两句，模型才有东西可整理";
      return;
    }
    st.busy = true;
    self.panePaint(ui);
    try {
      const tail = st.messages.slice(-8)
        .map((m) => (m.role === "user" ? "我：" : "模型：") + m.content).join("\n");
      const res = await self.request("POST", "/kb-propose", {
        key: ui.key, transcript_tail: tail, instruction: "从这次讨论里整理该记的经验与要改的正文",
      });
      if (!res || !res.ok) {
        st.messages.push({ role: "assistant",
          content: "整理失败：" + ((res && res.error) || "未知错误") });
        self.panePaint(ui);
        return;
      }
      self.enterWriteMode(ui, res, "模型从对话里整理出的建议");
    } finally {
      st.busy = false;
      self.panePaint(ui);
    }
  },


  paneClear: function (item) {
    var self = ZoteroKB;
    if (!item || !self.chatState[item.key]) return;
    const st = self.chatState[item.key];
    st.messages = [];
    st.para = null;
    st.write = null;
    st.mode = "chat";
    if (st.ui) {
      if (st.ui.detail) st.ui.detail.textContent = "";
      self.panePaint(st.ui);
    }
  },


  pct: function (v) {
    if (v === undefined || v === null) return "0%";
    return Math.round(Number(v) * 100) + "%";
  },
});
