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
    // 清掉上一次的内容（Zotero 会复用 body）
    try {
      const old = body.querySelector(".zotero-kb-kbview");
      if (old) old.remove();
    } catch (e) { /* ignore */ }

    const wrap = self.kbviewEl(doc, "div");
    wrap.className = "zotero-kb-kbview";
    wrap.setAttribute("style", "padding:4px 6px; font-size:12px; line-height:1.5;");

    // 工具条：**单行不换行**（用户反馈：提示文字换行后"重新读取"被挤到第二行）。
    // 做法：select/按钮 nowrap，提示占剩余宽度并省略号截断（min-width:0 是关键，
    // flex 子项默认 min-width:auto 不会收缩）。
    const bar = self.kbviewEl(doc, "div");
    bar.setAttribute("style", "display:flex; gap:6px; align-items:center;"
      + " flex-wrap:nowrap; margin-bottom:4px;");
    const sel = self.kbviewEl(doc, "select");
    sel.setAttribute("style", "max-width:7.5em; flex:0 0 auto; white-space:nowrap;");
    for (const lv of (self.kbLevels() || [])) {
      const op = self.kbviewEl(doc, "option");
      op.setAttribute("value", lv.id);
      op.textContent = lv.label;
      sel.appendChild(op);
    }
    sel.value = self.kbviewLevel();
    const btn = self.kbviewEl(doc, "button");
    btn.textContent = "重新读取";
    btn.setAttribute("style", "padding:1px 6px; flex:0 0 auto; white-space:nowrap;");
    const hint = self.kbviewEl(doc, "span");
    hint.setAttribute("style", "opacity:0.65; font-size:11px; flex:1 1 auto;"
      + " min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;");
    hint.setAttribute("title", "");
    bar.append(sel, btn, hint);
    wrap.append(bar);

    const view = self.kbviewEl(doc, "div");
    view.className = "zotero-kb-kbview-body";
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
      const lvLabel = (byId[picked.levelId] || {}).label || picked.levelId;
      const hintText = lvLabel
        + (picked.fellBack ? "（默认那一级还没生成，已回退）" : "")
        + "　" + text.length + " 字"
        + (isHtml ? "　公式已渲染" : "　公式为原文文本");
      hint.textContent = hintText;
      hint.setAttribute("title", hintText);   // 截断了也能悬停看全
      view.innerHTML = (isHtml ? text : self.md2html(text)) ||
        "<p style='opacity:0.75'>（这一级是空的）</p>";
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
   * 最小 markdown → HTML（**纯函数**，桩测试直接测它）。
   *
   * 覆盖知识库 md 里真正用到的语法：`#` 标题、`-`/数字列表、`>` 引用、
   * `**粗**`、`*斜*`、`` `代码` ``、`[文字](链接)`、`$$公式$$`、`![图注](图片)`、
   * 空行分段。**不做表格/嵌套列表** —— 我们的视图里没有。
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
      // 图片：窗格里不加载本地图片（路径是 KB 相对路径），只留图注
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, "[图：$1]")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

    const out = [];
    let list = "";
    let buf = [];
    const flushBuf = () => {
      if (buf.length) {
        out.push("<p>" + buf.join("<br>") + "</p>");
        buf = [];
      }
    };
    const closeList = () => {
      if (list) { out.push("</" + list + ">"); list = ""; }
    };
    const lines = String(md == null ? "" : md).split(/\r?\n/);
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { flushBuf(); closeList(); continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushBuf(); closeList();
        const lvl = m[1].length;
        const size = { 1: "15px", 2: "13.5px", 3: "12.5px" }[lvl] || "12px";
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
    return out.join("\n");
  },
});
