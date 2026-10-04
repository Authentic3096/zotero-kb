/**
 * 14-menus.js —— 工具栏按钮与条目右键菜单（菜单项都是每次弹出时现建）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 工具栏按钮 / 右键菜单

  /**
   * 在主窗口工具栏加一个按钮，点击打开知识库管理面板。
   *
   * 位置与写法照抄本机"沉浸式翻译"插件（它就是这么加按钮的）：
   *   document.getElementById("zotero-tb-note-add").after(btn)
   * 即插在「新建笔记」按钮后面，是 Zotero 主工具栏里的位置。
   *
   * 面板本身是 Python/Tkinter 程序（tools\gui.py）—— 插件不重写界面，
   * 直接把它拉起来即可（PyMuPDF、向量、模型都在 Python 侧，界面重写不划算）。
   */
  registerToolbarButton: function (win) {
    var self = ZoteroKB;
    try {
      const doc = (win && win.document)
        || (Zotero.getMainWindow && Zotero.getMainWindow().document);
      if (!doc) return;
      const BTN_ID = "zotero-tb-kb-panel";
      const old = doc.getElementById(BTN_ID);
      if (old) old.remove();                 // 窗口重建时先清掉旧的，避免重复

      const btn = doc.createXULElement
        ? doc.createXULElement("toolbarbutton")
        : doc.createElement("toolbarbutton");
      btn.id = BTN_ID;
      btn.setAttribute("class", "zotero-tb-button");
      btn.setAttribute("tooltiptext", "打开文献知识库管理面板");
      // ⚠ 用**工具栏专用**的小图标（toolbar-icon.svg，16×16）。
      // 之前图省事直接用了 icon.svg —— 那是 96×96 的大图标（给插件管理器/设置
      // 面板用的），list-style-image 会按原尺寸渲染，结果工具栏里一个巨大的图标。
      // 同时显式写 width/height，防止 Zotero 的按钮样式反向把它拉大。
      btn.setAttribute("style",
        "list-style-image: url(" + self.rootURI + "toolbar-icon.svg);"
        + " width: 28px; height: 28px;"
        + " -moz-context-properties: fill, fill-opacity;"
        + " fill: currentColor;");
      btn.addEventListener("click", () => { self.openPanel(); });

      // 插到「新建笔记」按钮后面（沉浸式翻译用的就是这个锚点）
      const anchor = doc.getElementById("zotero-tb-note-add");
      if (anchor && anchor.parentNode) anchor.after(btn);
      else {
        // 锚点找不到时的兜底：塞进集合树工具栏
        const tb = doc.getElementById("zotero-collections-toolbar")
          || doc.getElementById("zotero-toolbar-collection-tree");
        if (tb) tb.appendChild(btn);
        else Zotero.debug("[zotero-kb] 找不到工具栏锚点，按钮未添加");
      }
      Zotero.debug("[zotero-kb] 工具栏按钮已添加");
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /**
   * 带控制台的 Python —— 跑命令行脚本、要拿输出时用它。
   *
   * ⚠ `pythonExe()` 优先给 **pythonw.exe**（无窗口，适合启动 GUI），
   *   但 **pythonw 下 stdout 是 None** —— 脚本里的 `print` 会直接抛
   *   `AttributeError: 'NoneType' object has no attribute 'write'`。
   *   而 `convert.py` 全靠 print 报告进度和结果（`log()` 就是 print），
   *   所以跑脚本必须换回同一目录下的 python.exe。
   */
  pythonConsoleExe: function () {
    var self = ZoteroKB;
    const py = self.pythonExe();
    if (!py) return "";
    const alt = py.replace(/pythonw\.exe$/i, "python.exe");
    if (alt !== py && self._exists(alt)) return alt;
    return py;
  },


  /**
   * 跑一段 Python 并等它结束。返回 `{code, out}`（`out` 恒为空串，见下）。
   *
   * ⚠⚠ **不要用 `stdout: "pipe"` 去读输出** —— 本机实测踩过：
   *   `Subprocess.call` 给的 `proc.stdout` 是 **XPCOM 的 `nsIAsyncInputStream`**，
   *   不是 Web Stream，**没有 `getReader()`**。写成 `stream.getReader()` 会抛
   *   `stream.getReader is not a function`，用户点「重建知识库条目」时看到的就是
   *   这个报错（进度窗弹出即失败）。
   *
   *   现在用 `"ignore"`（等价于 /dev/null）：子进程照常 `print`，
   *   既不会因管道写满而卡死，也不需要读流。
   *
   *   **要看结果就读 MANIFEST.json**（见 `manifestSummary`）——
   *   那本来就是构建流程写给程序看的权威产物，比解析 stdout 可靠得多。
   */
  runPython: async function (args) {
    var self = ZoteroKB;
    const py = self.pythonConsoleExe();
    const root = self.projectRoot();
    if (!py) {
      return { code: -1, out: "找不到 Python（见 设置 → 文献知识库 → 运行环境）" };
    }
    if (!root) return { code: -1, out: "找不到项目目录" };
    const { Subprocess } = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs");
    const proc = await Subprocess.call({
      command: py,
      arguments: ["-X", "utf8"].concat(args),
      workdir: root,
      stdout: "ignore", stderr: "ignore",
    });
    const { exitCode } = await proc.wait();
    return { code: exitCode, out: "" };
  },


  /**
   * 从 `MANIFEST.json` 读这次构建的关键结果（**不要解析 stdout**）。
   *
   * 为什么读文件而不是抓进程输出：见 `runPython` 的说明。而且 MANIFEST 里
   * 记的是**全库实况**（`fulltext_total`），比一段可能被截断的 stdout 完整。
   */
  manifestSummary: async function () {
    var self = ZoteroKB;
    try {
      const txt = await IOUtils.readUTF8(self.kbDir() + "\\MANIFEST.json");
      const mf = JSON.parse(txt);
      const lines = [];
      const tot = mf.fulltext_total || {};
      const parts = Object.keys(tot).map((k) => k + " " + tot[k]);
      if (parts.length) lines.push("正文来源：" + parts.join("｜"));
      const fx = ((mf.fulltext || {}).deshifted) || [];
      // ⚠ 用**全库实况**计数，不要用 `fulltext.deshifted` ——
      //   后者是"本批次"的统计，增量重建时恒为 0，用户就永远看不到
      //   "库里其实有几篇是靠字符偏移还原救回来的"（实测踩到：
      //   全库实况里 `zotero-cache+fix+29` 有 1 篇，而本批次是 0）。
      let nfix = 0;
      for (const k of Object.keys(tot)) {
        if (k.indexOf("+fix") >= 0) nfix += tot[k];
      }
      if (nfix) lines.push("其中 " + nfix + " 篇的正文是字符偏移还原后的");
      if (fx.length) lines.push("本次新还原 " + fx.length + " 篇");
      const skip = ((mf.warnings || {}).skipped_no_pdf) || [];
      if (skip.length) lines.push("跳过（无 PDF 附件）" + skip.length + " 条");
      const c = mf.counts || {};
      lines.push("共 " + (c.chunks || 0) + " 个切片 / " + (c.items_in_kb || 0) + " 条文献");
      return lines.join("\n");
    } catch (e) {
      return "（读 MANIFEST 失败：" + e + "）";
    }
  },


  /** 这一条在 Zotero 里有没有 PDF 附件。 */
  itemHasPdf: function (item) {
    try {
      if (!item || !item.isRegularItem || !item.isRegularItem()) return false;
      return item.getAttachments().some((id) => {
        const a = Zotero.Items.get(id);
        return !!(a && a.isPDFAttachment && a.isPDFAttachment());
      });
    } catch (e) {
      return false;
    }
  },


  /** 条目的显示名（弹窗里列清单用）。 */
  itemLabel: function (item) {
    try {
      return String(item.getField("title") || item.key || "").slice(0, 60);
    } catch (e) {
      return String((item && item.key) || "");
    }
  },


  /**
   * 需要用户点「确定」的模态提示。
   *
   * 为什么不用 `notify`（进度窗）：进度窗 6 秒自动消失，"这篇为什么
   * 没进知识库"这种信息用户得看清 —— 而且它是要用户去 Zotero 里
   * 动手补 PDF 的，一闪而过等于没说。
   */
  alertDialog: function (title, text) {
    var self = ZoteroKB;
    try {
      Services.prompt.alert(null, title, text);
    } catch (e) {
      // 拿不到窗口时退回进度窗 —— 至少别把信息吞掉
      self.notify(title, text, null, true);
    }
  },


  /**
   * 逐篇重建知识库条目。**没有 PDF 附件的跳过并弹窗告知。**
   *
   * 后端是 `offline/convert.py --item <key>`（说明见那个文件）——
   * 它会走完整流程：缓存体检 → 灰区模型仲裁 → 字符偏移还原 →
   * 切片 → 向量 → 清单。
   */
  rebuildItems: async function (items) {
    var self = ZoteroKB;
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    const todo = [], noPdf = [];
    for (const it of list) {
      if (self.itemHasPdf(it)) todo.push(it);
      else noPdf.push(it);
    }
    if (noPdf.length) {
      self.alertDialog(
        "没有 PDF 附件，已跳过",
        "下面这些条目在 Zotero 里没有 PDF 附件，知识库不处理它们：\n\n"
        + noPdf.map((x) => "· " + self.itemLabel(x)).join("\n")
        + "\n\n想让它们进知识库：先给条目挂上 PDF（把 PDF 拖到条目上即可），"
        + "再重建一次。");
    }
    if (!todo.length) return;

    const script = self.projectRoot() + "\\offline\\convert.py";
    const args = [script];
    for (const it of todo) args.push("--item", it.key);

    self.notify("正在重建知识库条目…",
                todo.length + " 篇，请稍候（单篇几秒）", null, false);
    let r;
    try {
      r = await self.runPython(args);
    } catch (e) {
      self.alertDialog("重建失败", String((e && e.message) || e));
      return;
    }
    if (r.code === 0) {
      // 关键结果从 MANIFEST.json 读 —— **不解析 stdout**（见 runPython 的说明）
      const summary = await self.manifestSummary();
      self.notify("重建完成",
                  todo.length + " 篇已更新\n" + summary, null, false);
      // 重建可能改变切片数/新增文献，让权重缓存跟上
      try { await self.refreshWeights(); } catch (e) { /* ignore */ }
    } else {
      self.alertDialog(
        "重建失败（退出码 " + r.code + "）",
        "可能的原因看这两处：\n"
        + "· 管理面板「解析健康」页的日志\n"
        + "· " + self.kbDir() + "\\logs\\ 下的日志\n"
        + (r.out ? ("\n" + String(r.out).slice(-800)) : ""));
    }
  },


  /** 启动管理面板（Python/Tkinter）。已开着就不重复启动。 */
  openPanel: function () {
    var self = ZoteroKB;
    try {
      const root = self.projectRoot();
      const py = self.pythonExe();
      const gui = root ? root + "\\tools\\gui.py" : "";
      // 报错时把"我找的是哪、为什么、该改什么"一次说清，
      // 而不是只说"找不到 Python 环境"（那样用户不知道该动哪一个框）。
      if (!root || !self._exists(gui)) {
        self.notify("找不到项目目录",
          "我找的是：" + (root || "（没找到）")
          + "\n它下面应该有 tools\\gui.py，但没找到。"
          + "\n\n请在 设置 → 文献知识库 → 运行环境 里，"
          + "把「项目目录」指到有 offline、online、.venv 的那个文件夹。",
          null, true);
        return;
      }
      if (!self._exists(py)) {
        self.notify("找不到 Python 环境",
          "我找的是：" + (py || "（没找到）")
          + "\n项目目录：" + root
          + "\n\n请在 设置 → 文献知识库 → 运行环境 里点「浏览…」，"
          + "选到 .venv\\Scripts\\pythonw.exe；"
          + "\n如果还没建过环境，先按 README 的「安装」一节建一次。",
          null, true);
        return;
      }
      // 已在运行就不重复开（GUI 自己也有单实例判断，这里再挡一层）
      try {
        const { Subprocess } = ChromeUtils.importESModule(
          "resource://gre/modules/Subprocess.sys.mjs");
        Subprocess.call({
          command: py, arguments: ["-X", "utf8", gui],
          workdir: self.projectRoot(),
          stderr: "ignore", stdout: "ignore",
        }).catch((e) => Zotero.debug("[zotero-kb] 面板进程：" + e));
        self.notify("正在打开管理面板", "如果没出现，看任务栏（面板可能已在运行）",
                    null, false);
      } catch (e) {
        // Subprocess 不可用时退回 nsIProcess
        const nsLocalFile = Components.Constructor(
          "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
        const proc = Components.classes["@mozilla.org/process/util;1"]
          .createInstance(Components.interfaces.nsIProcess);
        proc.init(nsLocalFile(py));
        const args = ["-X", "utf8", gui];
        proc.runw(false, args, args.length);
        self.notify("正在打开管理面板", "", null, false);
      }
    } catch (e) {
      Zotero.logError(e);
      self.notify("打开面板失败", String(e), null, true);
    }
  },


  /**
   * 给条目右键菜单加「发送到 DSH」。
   *
   * 用 popupshowing 事件在**每次弹出时现建**菜单项，这样：
   *   · 选中项数/类型变化时能实时反应（单选/多选）
   *   · 不需要维护菜单项的增删
   * 这是 Zotero 插件加条目标右键菜单最稳的做法（MenuManager 在 10 上
   * 对 collectionTreeRow 的上下文有过破坏性变更，见官方 Zotero 10 说明）。
   */
  registerItemMenu: function (win) {
    var self = ZoteroKB;
    try {
      const doc = (win && win.document)
        || (Zotero.getMainWindow && Zotero.getMainWindow().document);
      if (!doc) return;
      const popup = doc.getElementById("zotero-itemmenu");
      if (!popup) {
        Zotero.debug("[zotero-kb] 找不到 zotero-itemmenu，右键菜单未加");
        return;
      }
      // 避免重复绑定
      if (popup.__kbBound) return;
      popup.__kbBound = true;
      popup.addEventListener("popupshowing", (event) => {
        // ⚠⚠ 关键防护：`popupshowing` **会冒泡**。
        // 不加这个判断的话，鼠标悬停展开**我自己的子菜单**时，子菜单的
        // popupshowing 会冒泡到这里，于是把自己的 menu 删掉重建 ——
        // 表现就是"二级菜单点不开"（刚展开就被自己拆了）。
        // 本机实测踩过：截图里菜单项在，但一悬停就没了。
        // 只处理真正来自 zotero-itemmenu 本身的事件。
        if (event && event.target && event.target !== popup) return;
        try {
          // 清理上一次建的几样东西：五个顶层菜单项 + 上下两个分隔符。
          // 漏掉的话每弹一次右键菜单就会多留一份（会看出来越用越多）。
          // ⚠ 新建的顶层项**必须同时**在这里登记 id，否则下一轮就重复了。
          for (const id of ["zotero-kb-send-menu",
                            "zotero-kb-classify-item",
                            "zotero-kb-pin-item",
                            "zotero-kb-metafill-item",
                            "zotero-kb-rebuild-item",
                            "zotero-kb-sep-before",
                            "zotero-kb-sep-after"]) {
            const old = doc.getElementById(id);
            if (old) old.remove();
          }
          // 取当前选中的条目。
          // Zotero 10 对 ZoteroPane 有些改动，所以这里多路兜底，
          // 任一路能拿到就不至于让菜单消失。
          let items = [];
          try {
            const pane = (typeof Zotero.getActiveZoteroPane === "function"
                          && Zotero.getActiveZoteroPane())
              || (win && win.ZoteroPane) || null;
            if (pane && typeof pane.getSelectedItems === "function") {
              items = pane.getSelectedItems() || [];
            }
          } catch (e) { /* 落到下面 */ }
          if (!items.length) {
            try {
              const w = (win && win.document && win) || Zotero.getMainWindow();
              const sel = w && w.document
                && w.document.getElementById("zotero-items-tree");
              if (sel && sel.view && sel.view.getSelectedItems) {
                items = sel.view.getSelectedItems() || [];
              }
            } catch (e) { /* 再落到下面 */ }
          }
          const real = items.filter((it) => it && !it.isAttachment()
            && !it.isNote() && !it.isAnnotation());
          if (!real.length) return;

          const menu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          menu.id = "zotero-kb-send-menu";
          menu.setAttribute("label", "发送到 DSH");
          // 对齐其他插件（jasminum / pdf2zh）的样子：menu-iconic + image。
          // 没有这两样时，菜单项在 Zotero 10 里跟内置项长得不一样（没图标、
          // 行高偏小），一眼就能看出是"外来的"。
          menu.setAttribute("class", "menu-iconic");
          menu.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          const mp = doc.createXULElement
            ? doc.createXULElement("menupopup") : doc.createElement("menupopup");
          menu.appendChild(mp);
          // ⚠ 这个辅助必须带 parent 参数（`mkIn`），而 `mk` 只是它的"绑定到
          //   发送子菜单"版本。原因：原来 `mk` 里写死了 `mp.appendChild(mi)`，
          //   于是**所有**用它建的项都落进了「发送到 DSH」子菜单 ——
          //   上一轮加的「重建知识库条目（这一篇）」就是这么被塞进子菜单的
          //   （它跟"发送"毫无关系，本该和「标为重点」并列在顶层）。
          //   这一轮要把补全/重建两项放到顶层，所以拆一层；`mk` 行为完全不变，
          //   提示逻辑也只有一份（本项目吃过"两份实现必然漂移"的亏）。
          const mkIn = (parent, label, fn, opts) => {
            opts = opts || {};
            const mi = doc.createXULElement
              ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
            mi.setAttribute("label", label);
            if (opts.iconic !== false) mi.setAttribute("class", "menuitem-iconic");
            if (opts.image) mi.setAttribute("image", opts.image);
            if (opts.tooltip) mi.setAttribute("tooltiptext", opts.tooltip);
            mi.addEventListener("command", () => {
              // 立刻给可见反馈 —— 之前只有 6 秒的进度窗，而真正发送要等
              // DSH 侧回复（最长可达 2 分钟），看起来就像"点了没反应"。
              //
              // ⚠ 这句原本写死成"已开始发送到 DSH…"，对分类建议 / 标重点 /
              //   重建这些**不发送**的项就是错的话术。改成可覆盖：
              //   `opts.ready` 传空串表示"这一项自己会弹提示，别插嘴"。
              const ready = opts.ready === undefined
                ? "已开始发送到 DSH…" : opts.ready;
              if (ready) {
                try { self.notify(ready, label, null, false); }
                catch (e) { /* ignore */ }
              }
              try { fn(); } catch (e) { Zotero.logError(e); }
            });
            parent.appendChild(mi);
            return mi;
          };
          const mk = (label, fn, opts) => mkIn(mp, label, fn, opts);
          const mkSep = () => {
            const s = doc.createXULElement
              ? doc.createXULElement("menuseparator") : doc.createElement("menuseparator");
            mp.appendChild(s);
          };

          mk("新建对话…", () => { self.sendToDSH(real, { create: true }); },
             { iconic: false, tooltip: "在 DSH 里开一个新对话，把文献路径发过去" });
          // ⚠ 这一行只是分组标题，**不能用 disabled 的 menuitem** ——
          // 本机实测：menupopup 里只要有 disabled 的 menuitem，子菜单就点不开。
          // 改成不可聚焦的普通 menuitem（去掉 disabled，改为 label 元素语义）。
          const choose = mk("选择已有对话：", () => {},
             { iconic: false, tooltip: "展开下面列表挑一个对话" });
          choose.setAttribute("disabled", "false");
          choose.setAttribute("tabindex", "-1");
          choose.setAttribute("style", "font-weight:600; opacity:0.75;");

          self.listDSHSessions().then((rows) => {
            if (!rows || !rows.length) {
              mkSep();
              mk("（读不到对话列表，DSH 在运行吗？）", () => {}, { iconic: false });
              return;
            }
            mkSep();
            for (const s of rows.slice(0, 15)) {
              const t = (s.title || "").trim() || "(未命名)";
              mk("　" + t.slice(0, 38) + "　·　" + s.id.slice(-6),
                 () => { self.sendToDSH(real, { sessionId: s.id }); },
                 { tooltip: "发送到：" + t + "\n" + s.id });
            }
          }).catch(() => {});

          // ---- 紧挨着的第二项：「分类建议」（用户要求放右键菜单、与发送到 DSH 并列）
          //
          // 为什么放在右键而不是面板里（用户的原话："单篇的分类建议也可以不
          // 在面板，可以在右键的菜单看，正好可以和之前的发送到 dsh 并列"）：
          //   · 在 Zotero 里看到某篇觉得"它该归哪儿"，那一刻就想问 —— 右键是
          //     最短路径；去管理面板还得先找出这篇的 key。
          //   · 弹窗里可以「应用 / 调整 / 跳过」，调整就是跟模型对话改分类。
          const classify = doc.createXULElement
            ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
          classify.id = "zotero-kb-classify-item";
          classify.setAttribute("class", "menuitem-iconic");
          classify.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          classify.setAttribute(
            "label", real.length > 1
              ? ("分类建议（本地模型）· " + real.length + " 篇")
              : "分类建议（本地模型）");
          classify.setAttribute(
            "tooltiptext",
            "让本地模型判断这篇该归到哪个分类，可一键应用；\n"
            + "不满意可以「调整」——直接告诉模型哪里不对，它会重判。");
          classify.addEventListener("command", () => {
            // 多选时逐篇串行问 —— 一次弹一堆对话框会叠在一起，
            // 而且模型请求并发打过去也容易把本地 Ollama 拖住。
            const runOne = (i) => {
              if (i >= real.length) return;
              try {
                self.notify("正在判断分类…",
                            (i + 1) + "/" + real.length, null, false);
              } catch (e) { /* ignore */ }
              Promise.resolve(self.suggestFor(real[i]))
                .then(() => runOne(i + 1))
                .catch((e) => {
                  Zotero.logError(e);
                  runOne(i + 1);
                });
            };
            try { runOne(0); } catch (e) { Zotero.logError(e); }
          });

          // ---- 第三项：标重点 / 取消重点（用户要求"右键菜单没有标重点"）
          //
          // 标签按当前状态动态给：已标重点的显示「取消重点」，
          // 否则显示「标为重点」。状态从权重缓存里查（启动/刷新时拉的），
          // 所以不用发网络请求 —— 右键菜单是同步构建的，等不了网络。
          const pinItem = doc.createXULElement
            ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
          pinItem.id = "zotero-kb-pin-item";
          pinItem.setAttribute("class", "menuitem-iconic");
          pinItem.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          const isPinned = real.every((it) => {
            const w = self.weightsCache && self.weightsCache[it.key];
            return !!(w && w.pinned);
          });
          pinItem.setAttribute(
            "label", (isPinned ? "取消重点" : "标为重点")
              + (real.length > 1 ? ("· " + real.length + " 篇") : ""));
          pinItem.setAttribute(
            "tooltiptext",
            "重点文献在检索时会明显往前排（权重 ×4）。\n"
            + "这是「哪些文献对我重要」的手工标记，"
            + "和「用过哪些方法」的经验是两回事。");
          pinItem.addEventListener("command", () => {
            self.setPinned(real, !isPinned).catch((e) => Zotero.logError(e));
          });

          // ---- 第四项：重建知识库条目（这一篇）
          //
          // 为什么需要逐篇重建：
          //   · 全量重建要 2 分多钟，而新加/改动通常只有一两篇；
          //   · **更要紧的是** —— 某篇的正文提取坏了（例如源 PDF 字体编码坏，
          //     整篇字符偏移：`&DOFXODWLRQ` 本该是 `Calculation`）
          //     **不会改变条目版本号**，增量构建会认为"这篇没变化"直接跳过。
          //     没有强制单篇重建的入口，用户就只能全量重建。
          //
          // ⚠ 它原来是用 `mk(...)` 建的，而 mk 把项塞进「发送到 DSH」子菜单 ——
          //   跟"发送"毫无关系，用户按上一轮的要求期待的是"在「标重点」之后"。
          //   这里改成 `mkIn(popup, ...)` 放到顶层，并给它一个 id 供每次重建菜单
          //   时清理（不给 id 的话每弹一次右键就多留一份，本项目踩过）。
          const rebuild = mkIn(popup, "重建知识库条目（这一篇）", () => {
            self.rebuildItems(real).catch((e) => Zotero.logError(e));
          }, {
            image: self.rootURI + "toolbar-icon.svg",
            ready: "",          // 它自己会弹进度/结果，不要那句"发送到 DSH"
            tooltip: "重新解析这一篇的正文、切片与向量。\n"
              + "正文乱码、字符错位时用它修（源 PDF 字体编码坏时也修得了）。\n"
              + "单篇只要几秒；全量重建要 2 分钟。\n"
              + "没有 PDF 附件的条目会被跳过，并弹窗告诉你。",
          });
          rebuild.id = "zotero-kb-rebuild-item";

          // ---- 第五项：补全元数据（本地模型）
          //
          // 为什么放右键（用户要求"放在「标为重点」附近"）：
          //   · 用 Zotero 抓文献识别失败很常见（date/DOI/卷/期/页码留空），
          //     而这些值**就印在 PDF 首页**；在 Zotero 里看到"这篇字段是空的"
          //     那一瞬间，右键是最短路径（同「分类建议」的理由）。
          //   · 与分类建议不同的是：这一项**一定会写 Zotero 库**，所以服务端
          //     只出建议，写回必须由用户在弹窗里勾选确认（见 applyMeta）。
          const metaFill = mkIn(popup,
            "补全元数据（本地模型）"
              + (real.length > 1 ? ("· " + real.length + " 篇") : ""),
            () => {
              // ⚠ 单篇 / 多选**走两条不同的路**（用户 2026-10-03 拍板的形态）：
              //   · 只选一篇 → 保留原来的"逐条确认"弹窗。一篇时用户要的是
              //     "把证据逐条看一遍再决定"，一次全采用反而把证据藏起来了；
              //   · 选了两篇及以上 → 先汇总一份清单、一次确认、然后批量写
              //     （就是「一键采用」）。逐篇弹一堆确认框在实际用起来很烦，
              //     而且用户很难判断"一共会改多少个字段"。
              if (real.length >= 2) {
                Promise.resolve(self.metaFillMany(real))
                  .catch((e) => {
                    Zotero.logError(e);
                    self.alertDialog("批量补全失败", String((e && e.message) || e));
                  });
                return;
              }
              // 单篇时逐篇串行（照分类建议的 runOne）。
              // 这里的循环在 real.length>=2 时已经不会被走到（上面提前 return），
              // 保留成循环是因为"只选一篇"和"将来放宽分流阈值"都能直接用。
              const runOne = (i) => {
                if (i >= real.length) return;
                try {
                  self.notify("正在读 PDF 首页找元数据…",
                              (i + 1) + "/" + real.length, null, false);
                } catch (e) { /* ignore */ }
                Promise.resolve(self.metaFillFor(real[i]))
                  .then(() => runOne(i + 1))
                  .catch((e) => {
                    Zotero.logError(e);
                    runOne(i + 1);
                  });
              };
              try { runOne(0); } catch (e) { Zotero.logError(e); }
            },
            {
              image: self.rootURI + "toolbar-icon.svg",
              ready: "",        // 它自己会弹进度/确认窗，不要那句"发送到 DSH"
              tooltip: "从这篇的 PDF 首页里找缺的元数据（日期 / DOI / 卷 / 期 / 页码）。\n"
                + "每条建议都带原文证据与来源（规则 / 模型）与置信度。\n"
                + "只填空、不覆盖，**绝不写作者和标题**；\n"
                + "写进 Zotero 之前要你先在弹窗里确认。",
            });
          metaFill.id = "zotero-kb-metafill-item";


          // ---- 放到"插件菜单组的最上面"（不依赖任何其他插件）。
          //
          // 定位原理（读 Zotero 源码 `zoteroPane.js` 的 buildItemContextMenu 得到）：
          //   · 内置项由它管理，用**索引**访问：`menu.childNodes[m.reindexItem]`
          //     → 说明内置项只被隐藏、**不会被删除**，顺序固定。
          //   · 它的硬编码内置列表**最后一项是 `reindexItem`**（「重建条目索引」）。
          //   · 各插件菜单是自己在末尾 append 的。
          //
          // ⚠ 这里必须用 reindexItem 这个**确定锚点**，不能靠"哪一项看起来像内置"。
          //   本机踩过：我原来把"无 id 的 menuseparator"也当内置（因为 Zotero 的
          //   内置分隔符确实没有 id），结果**沉浸式翻译加的那个无 id 分隔符**
          //   被误判成内置项，菜单就被插到了 PDF2zh 之后（用户看到位置没变）。
          const AFTER_LABELS = ["重建条目索引", "Reindex Item"];
          const AFTER_IDS = ["zotero-menuitem-reindex"];
          let anchor = null;
          for (const c of popup.children) {
            if (!c || c.nodeType !== 1) continue;
            if ((c.id && AFTER_IDS.includes(c.id))
                || AFTER_LABELS.includes(c.getAttribute("label") || "")) {
              anchor = c;
            }
          }
          Zotero.debug("[zotero-kb] 定位锚点："
            + (anchor ? (anchor.id || anchor.getAttribute("label")) : "未找到"));

          // 上下各加一个分隔符，把这个菜单和别的插件/内置项隔开。
          // 分隔符也带 id，方便下次重建时一起清掉（否则每弹一次菜单就多两个）。
          // 注意：名字别跟上面那个 mkSep()（子菜单分组用）重了，
          // 两者签名不同（一个带 id 一个不带），重名容易埋雷。
          const mkMarkedSep = (id) => {
            const s = doc.createXULElement
              ? doc.createXULElement("menuseparator") : doc.createElement("menuseparator");
            s.id = id;
            return s;
          };

          // ⚠ 相邻已经有分隔符时就别再加了 —— 否则会出现"两个分隔符叠在一起"。
          // 本机实测：jasminum 在自己的菜单前加了个分隔符，正好紧跟在我的下面，
          // 我再加一个就成了连续两条横线（用户截图反馈）。
          // 判据要跳过纯空白文本节点（XUL 源码里的换行），否则永远"后面没东西"。
          const isRealSep = (el) =>
            !!el && el.nodeType === 1 && el.tagName === "menuseparator";
          const prevReal = (el) => {
            let n = el.previousSibling;
            while (n && n.nodeType === 3 && !n.data.trim()) n = n.previousSibling;
            return n;
          };
          const nextReal = (el) => {
            let n = el.nextSibling;
            while (n && n.nodeType === 3 && !n.data.trim()) n = n.nextSibling;
            return n;
          };

          if (anchor && anchor.parentNode === popup) {
            // 上方：锚点后面本来就有分隔符就不用加
            if (!isRealSep(nextReal(anchor))) anchor.after(mkMarkedSep("zotero-kb-sep-before"));
            // 插到"锚点之后、所有相邻分隔符之后"，保证紧跟分隔符下面。
            // 顺序：发送到 DSH（menu）→ 分类建议 → 标重点 → 补全元数据 → 重建。
            let ref = anchor;
            while (isRealSep(nextReal(ref))) ref = nextReal(ref);
            ref.after(menu);
            menu.after(classify);
            classify.after(pinItem);
            pinItem.after(metaFill);
            metaFill.after(rebuild);
            // 下方：下一个真实元素已经是分隔符就不用加
            if (!isRealSep(nextReal(rebuild))) rebuild.after(mkMarkedSep("zotero-kb-sep-after"));
            Zotero.debug("[zotero-kb] 菜单已插到「重建条目索引」之后（按需补分隔符）");
          } else {
            // 兜底：锚点找不到（Zotero 改了菜单结构）就放在最前面，
            // 至少保证"在插件组之前"，不会夹在别的插件中间
            const before = mkMarkedSep("zotero-kb-sep-before");
            popup.insertBefore(before, popup.firstChild);
            before.after(menu);
            menu.after(classify);
            classify.after(pinItem);
            pinItem.after(metaFill);
            metaFill.after(rebuild);
            if (!isRealSep(nextReal(rebuild))) rebuild.after(mkMarkedSep("zotero-kb-sep-after"));
            Zotero.debug("[zotero-kb] 找不到内置锚点，菜单放到最前面");
          }
        } catch (e) {
          Zotero.logError(e);
        }
      });
      Zotero.debug("[zotero-kb] 条目右键菜单已绑定");
    } catch (e) {
      Zotero.logError(e);
    }
  },
});
