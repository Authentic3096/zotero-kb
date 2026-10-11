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
    // 「导出草稿」：把整份**有效正文**写成 edits\<key>.draft.md，给外部编辑器改。
    // 只在「按页正文」这一级显示（别的级别没有逐页正文）。存回时服务端逐页比对，
    // 只有真变了的页进修订层 —— 所以重解析不会把整篇冻在旧稿上。
    const draftBtn = self.kbviewEl(doc, "button");
    draftBtn.textContent = "导出草稿";
    draftBtn.setAttribute("style", "padding:1px 6px; flex:0 0 auto; white-space:nowrap;");
    draftBtn.setAttribute("title", "导出整份正文为 edits 下的草稿文件（供外部编辑器修改）");
    draftBtn.style.display = "none";
    draftBtn.addEventListener("click", async () => {
      draftBtn.disabled = true;
      hint.textContent = "正在导出草稿…";
      try {
        const r = await self.request("POST", "/doc-edit",
                                     { key: item.key, action: "draft" });
        if (r && r.path) {
          hint.textContent = "草稿已写出：" + r.path;
          try { self.openKbPath(r.path, "正文草稿"); } catch (e) { /* 打不开也不影响导出 */ }
        } else {
          hint.textContent = "导出失败：" + JSON.stringify(r == null ? {} : r).slice(0, 120);
        }
      } catch (e) {
        hint.textContent = "导出失败：" + ((e && e.message) ? e.message : e);
      }
      hint.setAttribute("title", hint.textContent);
      draftBtn.disabled = false;
    });
    bar.append(sel, fontSel, draftBtn, btn, hint);
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
      draftBtn.style.display = picked.levelId === "fulltext" ? "" : "none";
      const lvLabel = (byId[picked.levelId] || {}).label || picked.levelId;
      const hintText = lvLabel
        + (picked.fellBack ? "（默认那一级还没生成，已回退）" : "")
        + "　" + text.length + " 字"
        + (isHtml ? "　公式已渲染" : "　公式为原文文本");
      hint.textContent = hintText;
      hint.setAttribute("title", hintText);   // 截断了也能悬停看全
      // 图片路径是相对 <kb>/mineru/<key>/ 的（kb 文件里不写本机路径）——
      // 注入前改写成绝对 file://，否则窗格里全是裂图。
      let html = isHtml ? text : self.md2html(text);
      html = self.kbviewFixImgs(html, item.key);
      self.kbviewSetHtml(doc, view, html
        || "<p style='opacity:0.75'>（这一级是空的）</p>");
      // 注入后如果容器是空的（极少数解析失败），说明白，别让用户对着空白猜
      if (!view.firstChild) {
        self.kbviewSetHtml(doc, view,
          "<p style='opacity:0.75'>这一级的内容没渲染出来（"
          + text.length + " 字已读到）—— 请把这一行报给维护者。</p>");
      }
      // 「按页正文」这一级：每页挂「改这一页」+ 图片缩略图/点击打开原图。
      // 只在正文这一级做（别的级别没有 ## p.N 页锚点）。
      // ⚠ 渲染路径必须**同步**返回（用户反馈"菜单要立刻弹出来"）：这里只接线，
      //   真正的网络请求都在用户点按钮之后。
      if (picked.levelId === "fulltext") {
        self.kbviewWirePages(doc, view, item, path, function () {
          return paint(sel.value);
        }).catch(function (e) {
          Zotero.debug("[zotero-kb] 正文编辑接线失败：" + e);
        });
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
   * 把正文 markdown 按 `## p.N` 切成 {页码: 该页 markdown}（**纯函数**，桩测试直接测）。
   *
   * 为什么在插件侧再切一次：服务端渲染好的 HTML 里拿不回 markdown 源文本，而
   * 「改这一页」要编辑的正是 markdown（图片行、公式、表格都是 markdown）。
   */
  kbviewSplitMd: function (md) {
    var out = {};
    var cur = null;
    var buf = [];
    var lines = String(md == null ? "" : md).split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var m = lines[i].match(/^##\s*p\.(\d+)\s*$/);
      if (m) {
        if (cur !== null) out[cur] = buf.join("\n").replace(/^\n+|\n+$/g, "");
        cur = parseInt(m[1], 10);
        buf = [];
      } else if (cur !== null) {
        buf.push(lines[i]);
      }
    }
    if (cur !== null) out[cur] = buf.join("\n").replace(/^\n+|\n+$/g, "");
    return out;
  },


  /** 轮询一个 job 直到结束（最多约 6 分钟）。返回 job 对象，拿不到返回 {}。 */
  kbviewWaitJob: async function (jobId, onTick) {
    var self = ZoteroKB;
    if (!jobId) return {};
    for (var i = 0; i < 240; i++) {
      var job = {};
      try { job = await self.request("GET", "/jobs/" + encodeURIComponent(jobId)); }
      catch (e) { return {}; }
      if (onTick) onTick(job);
      if (job && job.state && job.state !== "running") return job;
      await new Promise(function (r) { setTimeout(r, 1500); });
    }
    return { state: "timeout" };
  },


  /**
   * 保存/恢复一页正文：POST /doc-edit -> 等 job -> 返回是否成功。
   *
   * 保存之后服务端会**重切重向量**（用户口径"改完立即"），所以这里必须等它跑完
   * 再重绘，否则窗格读到的还是旧内容。
   */
  kbviewRunDocEdit: async function (body, onStatus) {
    var self = ZoteroKB;
    var note = onStatus || function () {};
    note("正在重建切片…");
    var res = null;
    try {
      res = await self.request("POST", "/doc-edit", body);
    } catch (e) {
      note("请求失败：" + ((e && e.message) ? e.message : e));
      return false;
    }
    if (!res || !res.job) {
      note("服务没有返回任务：" + JSON.stringify(res == null ? {} : res).slice(0, 120));
      return false;
    }
    var done = await self.kbviewWaitJob(res.job, function (j) {
      var lg = (j && j.log) || [];
      note("正在重建切片… " + String(lg[lg.length - 1] || "").slice(0, 60));
    });
    if (done && done.state === "done") { note("已重建，正在刷新…"); return true; }
    note("重建失败：" + ((done && done.error) || "未知"));
    return false;
  },


  /**
   * 「按页正文」接线：给每个 `## p.N` 页块挂上「改这一页 / 恢复原版」+ 图片交互。
   *
   * 页块由 mdhtml / md2html 渲染成 `<div class="kb-page" data-kbpage="N">`，
   * 所以这里只要 query 一遍 DOM 就能拿到每页；markdown 源文本另外从 `.md` 读。
   * 全过程只读，不阻塞渲染。
   */
  kbviewWirePages: async function (doc, view, item, mdPath, repaint) {
    var self = ZoteroKB;
    var els = [];
    try { els = Array.from(view.querySelectorAll(".kb-page")); }
    catch (e) { return; }
    if (!els.length) return;
    // 图片：限高 + 点击打开原图（不引 lightbox 依赖）
    try {
      Array.from(view.querySelectorAll("img")).forEach(function (img) {
        img.setAttribute("style", "max-width:100%; max-height:180px;"
          + " height:auto; display:block; margin:4px 0; cursor:zoom-in;");
        img.addEventListener("click", function () {
          var src = String(img.getAttribute("src") || "");
          var path = src.indexOf("file:///") === 0
            ? decodeURIComponent(src.slice(8)).replace(/\//g, "\\") : "";
          if (path) self.openKbPath(path, "正文原图");
        });
      });
    } catch (e) { /* 图片接线失败不影响编辑 */ }

    var mdText = "";
    try { mdText = await IOUtils.readUTF8(mdPath); } catch (e) { mdText = ""; }
    var mdPages = self.kbviewSplitMd(mdText);
    var meta = {};
    try {
      meta = await self.request("GET", "/doc-edit?key=" + encodeURIComponent(item.key));
    } catch (e) { meta = {}; }
    var edited = {};
    (meta && meta.edited || []).forEach(function (n) { edited[n] = true; });
    var stale = {};
    (meta && meta.stale || []).forEach(function (n) { stale[n] = true; });
    els.forEach(function (el) {
      var n = parseInt(el.getAttribute("data-kbpage"), 10);
      if (!n) return;
      self.kbviewDecoratePage(doc, el, item, n, mdPages[n] || "",
                              !!edited[n], !!stale[n], repaint);
    });
  },


  /** 给一个页块加工具条与编辑态（工具条在页块内部顶部）。 */
  kbviewDecoratePage: function (doc, el, item, page, mdText, edited, stale, repaint) {
    var self = ZoteroKB;
    var busy = false;
    var bar = self.kbviewEl(doc, "div");
    bar.setAttribute("style", "display:flex; gap:6px; align-items:center;"
      + " flex-wrap:wrap; border-bottom:1px dashed rgba(127,127,127,0.4);"
      + " margin:2px 0 4px; padding-bottom:2px; font-size:11px;");
    var tag = self.kbviewEl(doc, "span");
    tag.textContent = "p." + page
      + (edited ? " · 已修订" : "") + (stale ? " · 基于旧解析" : "");
    tag.setAttribute("style", (edited ? "background:rgba(255,196,0,0.35);"
      + " padding:0 4px; border-radius:3px;" : "opacity:0.7;"));
    if (stale) tag.setAttribute("title", "这一页的解析结果变过，你改的是旧版 —— 仍用你的版本，请核对一下");
    var editBtn = self.kbviewEl(doc, "button");
    editBtn.textContent = edited ? "修改这一页" : "改这一页";
    editBtn.setAttribute("style", "padding:0 5px; font-size:11px;");
    var restoreBtn = self.kbviewEl(doc, "button");
    restoreBtn.textContent = "恢复原版";
    restoreBtn.setAttribute("style", "padding:0 5px; font-size:11px;");
    restoreBtn.disabled = !edited;
    var status = self.kbviewEl(doc, "span");
    status.setAttribute("style", "opacity:0.7;");
    bar.append(tag, editBtn, restoreBtn, status);
    el.insertBefore(bar, el.firstChild);

    var setBusy = function (b) {
      busy = b;
      editBtn.disabled = b;
      restoreBtn.disabled = b || !edited;
    };
    var submit = function (body) {
      if (busy) return;
      setBusy(true);
      self.kbviewRunDocEdit(body, function (msg) { status.textContent = msg; })
        .then(function (ok) {
          if (ok) {
            repaint().catch(function () {});
          } else {
            setBusy(false);
          }
        })
        .catch(function (e) {
          status.textContent = "失败：" + ((e && e.message) ? e.message : e);
          setBusy(false);
        });
    };
    var openEditor = function () {
      if (busy) return;
      if (el.querySelector(".zotero-kb-page-edit")) return;
      var ta = self.kbviewEl(doc, "textarea");
      ta.className = "zotero-kb-page-edit";
      ta.setAttribute("style", "width:100%; min-height:220px; font-family:monospace;"
        + " font-size:12px; white-space:pre-wrap; box-sizing:border-box;");
      ta.value = mdText;
      var save = self.kbviewEl(doc, "button");
      save.textContent = "保存";
      var cancel = self.kbviewEl(doc, "button");
      cancel.textContent = "取消";
      var row = self.kbviewEl(doc, "div");
      row.setAttribute("style", "display:flex; gap:6px; margin:4px 0;");
      row.append(save, cancel);
      var holder = self.kbviewEl(doc, "div");
      holder.append(ta, row);
      el.appendChild(holder);
      save.addEventListener("click", function () {
        submit({ key: item.key, page: page, text: ta.value });
      });
      cancel.addEventListener("click", function () {
        repaint().catch(function () {});
      });
    };
    editBtn.addEventListener("click", openEditor);
    restoreBtn.addEventListener("click", function () {
      submit({ key: item.key, page: page, action: "restore" });
    });
  },


  /**   * 把 `<img src="相对路径">` 改写成能加载的绝对 `file://`。
   *
   * kb 文件里图片是 `images/xxx.jpg`（相对 `<kb>/mineru/<key>/`），这样知识库
   * 整体搬家不用重写任何文件。窗格是特权 chrome 文档，`file://` 一般可加载；
   * 绝对/协议相对的 src 原地不动（幂等），没有 kbDir 时原样返回（宁可裂图，
   * 也不要拼出半个坏 URL）。
   */
  kbviewFixImgs: function (html, key) {
    var self = ZoteroKB;
    var s = String(html == null ? "" : html);
    if (s.indexOf("<img") < 0) return s;
    var base = "";
    try {
      var kb = self.kbDir();
      if (kb && key) {
        base = String(kb).replace(/[\\/]+$/, "") + "\\mineru\\" + key + "\\";
      }
    } catch (e) { base = ""; }
    if (!base) return s;
    var uriOf;
    try {
      uriOf = function (rel) {
        var abs = base + String(rel).replace(/\//g, "\\");
        return Services.io.newFileURI(Zotero.File.pathToFile(abs)).spec;
      };
    } catch (e) { return s; }
    return s.replace(/(<img\b[^>]*?\bsrc=")([^"]*)(")/g,
      function (m, pre, src, post) {
        // 已经是绝对 URL（http:/file:/data:/…）、协议相对、或锚点 → 不动
        if (!src || /^(?:[a-z][a-z0-9+.\-]*:|\/\/|#)/i.test(src)) return m;
        try { return pre + uriOf(src) + post; } catch (e) { return m; }
      });
  },


  /**
   * 最小 markdown → HTML（**纯函数**，桩测试直接测它）。
   *
   * 覆盖知识库 md 里真正用到的语法：`#` 标题、`-`/数字列表、`>` 引用、
   * `**粗**`、`*斜*`、`` `代码` ``、`[文字](链接)`、`$$公式$$`、`![图注](图片)`、
   * **管道表**（MinerU 的表格块就是它）、空行分段。**不做嵌套列表**。
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
      // 图片：渲染成真的 <img>（用户要"侧边栏能看正文并渲染里面的图表"）。
      // 路径**保持相对**（kb 文件里就是 images/xxx.jpg），由 kbviewFixImgs 在注入前
      // 改写成绝对 file:// —— 知识库搬家不用重写文件。自闭合，窗格可能按 XML 解析。
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g,
               '<img src="$2" alt="$1" style="max-width:100%; height:auto;'
               + ' display:block; margin:4px 0;" />')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

    const out = [];
    let list = "";
    let buf = [];
    let pageOpen = false;
    const flushBuf = () => {
      if (buf.length) {
        out.push("<p>" + buf.join("<br>") + "</p>");
        buf = [];
      }
    };
    const closeList = () => {
      if (list) { out.push("</" + list + ">"); list = ""; }
    };
    const cell = (tag, text) => "<" + tag + " style=\"border:1px solid"
      + " rgba(127,127,127,0.45); padding:2px 5px; text-align:left;"
      + " vertical-align:top;\">" + inline(text) + "</" + tag + ">";
    const splitRow = (line) => {
      let s = String(line).trim();
      if (s.charAt(0) === "|") s = s.slice(1);
      if (s.charAt(s.length - 1) === "|") s = s.slice(0, -1);
      return s.split("|").map((c) => c.trim());
    };
    const lines = String(md == null ? "" : md).split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const rawI = lines[i];
      // 管道表：本行 |...| + 下一行 |---| → 画成 <table>（MinerU 的表格块就是它）
      if (/^\s*\|.*\|\s*$/.test(rawI)
          && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?/.test(lines[i + 1])) {
        flushBuf(); closeList();
        const rows = [splitRow(rawI)];
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        i--;
        const head = rows[0] || [];
        let html = "<table style=\"border-collapse:collapse; margin:6px 0;"
          + " font-size:0.95em; max-width:100%;\"><thead><tr>"
          + head.map((c) => cell("th", c)).join("") + "</tr></thead>";
        if (rows.length > 1) {
          html += "<tbody>";
          for (const r of rows.slice(1)) {
            const cells = r.slice(0, head.length);
            while (cells.length < head.length) cells.push("");
            html += "<tr>" + cells.map((c) => cell("td", c)).join("") + "</tr>";
          }
          html += "</tbody>";
        }
        out.push(html + "</table>");
        continue;
      }
      const raw = rawI;
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { flushBuf(); closeList(); continue; }
      let m;
      // 页码锚点：把 ## p.N 包成 <div class="kb-page" data-kbpage="N">，
      // 窗格靠它把「按页正文」切成可编辑的页块（与 mdhtml.render 同一套 DOM）。
      if ((m = line.match(/^(#{1,6})\s+p\.(\d+)\s*$/))) {
        flushBuf(); closeList();
        if (pageOpen) out.push("</div>");
        out.push('<div class="kb-page" data-kbpage="' + m[2] + '">');
        pageOpen = true;
        const pLvl = m[1].length;
        const pSize = { 1: "1.35em", 2: "1.18em", 3: "1.06em" }[pLvl] || "1em";
        out.push('<div style="font-weight:600; font-size:' + pSize
                 + '; margin:6px 0 2px;">p.' + m[2] + "</div>");
        continue;
      }
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
    if (pageOpen) out.push("</div>");
    return out.join("\n");
  },
});
