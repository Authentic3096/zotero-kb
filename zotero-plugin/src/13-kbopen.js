/**
 * 13-kbopen.js —— 打开知识库（分级）：级别清单、定位文件、交给系统打开
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 *
 * 为什么要有它（用户 2026-10-05 的要求）：
 *   "打开知识库时先打开一个构建的列表，里面也是文献，然后点进某篇文献，
 *    里面还有不同级别的知识库内容，再打开某个具体的知识库才是打开具体的
 *    md 文件"；右键菜单也一样。
 *
 * 知识库目录里是 `papers/22X9PMR6.md` —— **文件名是 Zotero 的 key，人认不出
 * 是哪篇**。所以"打开某一篇的某一层"这件事必须由插件/面板替他做掉：
 * 右键 → 打开知识库 → 选级别 → 打开那个 md。
 *
 * ⚠ KB_LEVELS 必须与 **offline/kbviews.py 的 LEVELS** 一致（id / 标签 /
 *   相对路径模板三条）。它是跨语言复制的一份常量，所以有
 *   tools/check_kb_levels.py 盯着两边 —— 手工同步的约定最后一定会漂移。
 */
Object.assign(ZoteroKB, {

  /**
   * 知识库的级别清单（镜像 offline/kbviews.py 的 LEVELS）。
   *
   * rel 里的 `{key}` 会被替换成 Zotero 条目 key；分隔符统一用正斜杠，
   * 拼本地路径时再转成反斜杠（这样两边写起来一样，不会一个 `\` 一个 `/`）。
   */
  KB_LEVELS: [
    { id: "tldr", label: "摘要与要点", rel: "views/{key}.tldr.md",
      what: "元数据 + 结构化字段 + 摘要 + 笔记要点 + 使用经验，几百 token" },
    // 中间层：比摘要详细、比全文短（由 offline/digest.py 生成）
  { id: "outline", label: "分节纲要", rel: "views/{key}.outline.md",
    what: "按章节给「这一节在做什么 + 关键点/参数/结论」，每节带页码范围" },
  { id: "card", label: "完整档案", rel: "papers/{key}.md",
      what: "元数据 + 摘要 + 每页首段 + 笔记与高亮标注" },
    { id: "fulltext", label: "按页正文", rel: "fulltext/{key}.md",
      what: "带 ## p.N 页码锚点的正文" },
    { id: "figures", label: "图注与表格", rel: "views/{key}.figures.md",
      what: "散在各页末尾的图注与表格，汇总成一份" },
    { id: "weight", label: "权重与经验", rel: "views/{key}.weight.md",
      what: "检索权重、重点标记、人工加减分，以及这篇的全部使用经验" },
  ],

  /**
   * 某一篇的全部级别，带**文件在不在**。
   *
   * 为什么必须同步判断存在性：右键菜单是**同步**构建的（弹出来那一刻不能等
   * 网络、也不能等 Python），所以只能靠"文件在不在"决定显示什么。
   * 这也正是分级视图要在构建时生成、而不是"用的时候现生成"的原因。
   */
  kbLevels: function (key) {
    var self = ZoteroKB;
    var root = String(self.kbDir() || "").replace(/[\\/]+$/, "");
    return self.KB_LEVELS.map(function (lv) {
      var rel = String(lv.rel).replace(/\{key\}/g, key).replace(/\//g, "\\");
      var path = root + "\\" + rel;
      var exists = false;
      try { exists = !!self._exists(path); } catch (e) { /* 只当作"没有" */ }
      return { id: lv.id, label: lv.label, what: lv.what,
               path: path, exists: exists };
    });
  },

  /** 一篇还差哪几层没生成（决定菜单项的文案）。 */
  kbLevelsMissing: function (key) {
    var self = ZoteroKB;
    var miss = [];
    try {
      self.kbLevels(key).forEach(function (lv) {
        if (!lv.exists) miss.push(lv.label);
      });
    } catch (e) {
      return self.KB_LEVELS.map(function (lv) { return lv.label; });
    }
    return miss;
  },

  /**
   * 打开一个本地文件。按"从好到差"依次试，试到哪个算哪个。
   *
   * 为什么写成链式而不是挑一个：沙箱里能用的 API 随 Zotero 版本变，而这些
   * 入口的可用性没法静态确认。链式的好处是**最坏情况仍然有确定可用的兜底**
   * （`reveal()` 已在 settings.js 用过；再往前还有"交给面板的 Python 调
   * os.startfile"，那条是本机面板一直在用的）。用哪一条会写进 debug 日志。
   */
  openKbPath: function (path, what) {
    var self = ZoteroKB;
    var tried = [];
    var label = what || "知识库文件";

    // ① Zotero 自己的"打开外链/文件"入口
    try {
      if (typeof Zotero.launchURL === "function") {
        Zotero.launchURL(
          Services.io.newFileURI(Zotero.File.pathToFile(path)).spec);
        Zotero.debug("[zotero-kb] 打开知识库用 Zotero.launchURL：" + path);
        return "launchURL";
      }
      tried.push("Zotero.launchURL 不存在");
    } catch (e) {
      tried.push("Zotero.launchURL：" + e);
    }

    // ② 外部协议服务（把 file: URI 交给系统默认程序）
    try {
      const uri = Services.io.newFileURI(Zotero.File.pathToFile(path));
      const ext = Components.classes[
        "@mozilla.org/uriloader/external-protocol-service;1"
      ].getService(Components.interfaces.nsIExternalProtocolService);
      ext.loadURI(uri);
      Zotero.debug("[zotero-kb] 打开知识库用 externalProtocolService：" + path);
      return "externalProtocolService";
    } catch (e) {
      tried.push("externalProtocolService：" + e);
    }

    // ③（W4 2026-10-14 已删）原来还有一级"交给 Tk 面板的 Python 用 os.startfile 打开"——
    //    Tk 面板退役、`tools\gui.py（已退役 2026-10-14）` 已删，那一级必然失败；留着只会多一次无用的
    //    pythonw 往返、并把真实原因盖住。所以直接进第 ③ 级（原第 ④ 级）：
    // ④ 保底：在文件管理器里**选中**它（这条一定存在）。用户双击一下就能看，
    //    比"点了没反应"好得多。
    try {
      Zotero.File.pathToFile(path).reveal();
      Zotero.debug("[zotero-kb] 只能 reveal：" + path);
      return "reveal";
    } catch (e) {
      tried.push("reveal：" + e);
    }

    self.alertDialog("打不开「" + label + "」",
      "文件：\n" + path + "\n\n"
      + "四条路都试过了：\n" + tried.map(function (t) {
        return "  · " + t;
      }).join("\n")
      + "\n\n可以先在「" + self.kbDir() + "」里手动打开它；"
      + "如果这份文件本来就不该在，去面板点「补齐知识库分级文件」。");
    return "";
  },

  // ⚠ W4（2026-10-14）：原来这里有两个函数（起一个 Tk 面板进程 / 用它代劳开文件），
  //   它们是"起一个 Tk 面板（tools\gui.py（已退役 2026-10-14））进程并让它代劳"的兜底链。Tk 面板退役后
  //   `tools\gui.py（已退役 2026-10-14）` 已删，这两个函数**只会返回 false**（还会掩盖真实原因），
  //   所以整段删掉（连带 19-mineruguide.js 里那个调用点）。

  /**
   * 打开某一篇的某个级别。文件不在时**不静默失败** —— 明确告诉用户
   * 这一层还没生成、以及怎么生成。
   *
   * 为什么不在这里"现生成"：生成要在 Python 进程里读库（几百毫秒起，
   * 还要等子进程），而右键菜单点下去要立刻有反应。所以分工是：
   * 插件负责打开，生成交给构建/面板的「补齐知识库分级文件」。
   */
  openKbLevel: function (key, levelId) {
    var self = ZoteroKB;
    const row = (self.kbLevels(key) || []).filter(function (lv) {
      return lv.id === levelId;
    })[0];
    if (!row) return "";
    if (!row.exists) {
      self.notify("这一层还没生成", "「" + row.label + "」"
        + "（" + row.path + "）\n\n"
        + "去面板的「高级」页点「补齐知识库分级文件」，"
        + "或跑一次「手动更新（增量）」。", null, true);
      return "";
    }
    return self.openKbPath(row.path, row.label);
  },

  /** 在文件管理器里显示某一篇的分级视图目录（找不到就退到知识库根）。 */
  openKbFolder: function (key) {
    var self = ZoteroKB;
    const dir = String(self.kbDir() || "");
    var target = dir;
    try {
      const rows = self.kbLevels(key) || [];
      for (const lv of rows) {
        if (lv.exists) { target = lv.path.replace(/[\\/][^\\/]+$/, ""); break; }
      }
    } catch (e) { /* 用知识库根目录 */ }
    try {
      Zotero.File.pathToFile(target).reveal();
      return "reveal";
    } catch (e) {
      self.notify("打不开目录", String(target) + "\n" + e, null, true);
      return "";
    }
  },
});
