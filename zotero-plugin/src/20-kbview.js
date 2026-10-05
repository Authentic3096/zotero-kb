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
    if (tabType && tabType !== "library") return false;
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
    try {
      self.setPref(self.PREFS.kbviewLevel, String(id || ""));
    } catch (e) { /* ignore */ }
  },


  /** 某个级别的 md 在磁盘上的绝对路径（与 Python 侧 kbviews.level_path 同一套拼接）。 */
  kbviewPath: function (levelId, key) {
    var self = ZoteroKB;
    const kb = self.kbDirPath();
    const lv = (self.kbLevels() || []).find((x) => x.id === levelId);
    if (!kb || !lv || !key) return "";
    return kb.replace(/[\\/]+$/, "") + "\\"
      + String(lv.rel).replace("{key}", key).replace(/\//g, "\\");
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

    const wrap = doc.createElement("div");
    wrap.className = "zotero-kb-kbview";
    wrap.setAttribute("style", "padding:4px 6px; font-size:12px; line-height:1.5;");

    const bar = doc.createElement("div");
    bar.setAttribute("style", "display:flex; gap:6px; align-items:center;"
      + " margin-bottom:4px;");
    const sel = doc.createElement("select");
    sel.setAttribute("style", "max-width:60%;");
    for (const lv of (self.kbLevels() || [])) {
      const op = doc.createElement("option");
      op.setAttribute("value", lv.id);
      op.textContent = lv.label;
      sel.appendChild(op);
    }
    sel.value = self.kbviewLevel();
    const btn = doc.createElement("button");
    btn.textContent = "重新读取";
    btn.setAttribute("style", "padding:1px 6px;");
    const hint = doc.createElement("span");
    hint.setAttribute("style", "opacity:0.65; font-size:11px;");
    bar.append(sel, btn, hint);
    wrap.append(bar);

    const view = doc.createElement("div");
    view.className = "zotero-kb-kbview-body";
    wrap.append(view);
    body.append(wrap);

    const paint = async (prefer) => {
      hint.textContent = "读取中…";
      // 存在性：**逐个 await**（真机上 IOUtils.exists 是异步的），再交给纯函数决策。
      // ⚠ 别在这里做同步假判（第一版我图省事返回 true，"级别回退"当场失效）。
      const existMap = {};
      for (const id of self.kbviewOrder(prefer)) {
        try {
          existMap[id] = await IOUtils.exists(self.kbviewPath(id, item.key));
        } catch (e) {
          existMap[id] = false;
        }
      }
      const picked = self.kbviewPickLevel(prefer, (id) => !!existMap[id]);
      if (!picked.levelId) {
        view.textContent = "";
        const p = doc.createElement("div");
        p.setAttribute("style", "opacity:0.75;");
        p.textContent = "知识库里还没有这一篇的任何分级文件。"
          + "先在管理面板「手动更新」建一次索引，"
          + "或用右键菜单「重建本条目知识库」。";
        view.append(p);
        hint.textContent = "";
        return;
      }
      const path = self.kbviewPath(picked.levelId, item.key);
      let text = "";
      try {
        text = await IOUtils.readUTF8(path);
      } catch (e) {
        text = "";
      }
      const lvLabel = ((self.kbLevels() || [])
        .find((x) => x.id === picked.levelId) || {}).label || picked.levelId;
      hint.textContent = lvLabel + (picked.fellBack ? "（默认那一级还没生成，已回退）" : "")
        + "　" + text.length + " 字";
      view.innerHTML = self.md2html(text) ||
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
