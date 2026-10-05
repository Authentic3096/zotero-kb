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

    // ③ 交给面板的 Python 打开（os.startfile —— 面板「文献管理器中查看」就是
    //    这么干的，本机确定可用）。代价是要起一个短命的 pythonw 进程。
    try {
      if (self.openKbViaPanel(path)) return "panel";
      tried.push("面板：找不到 Python 或 tools\\gui.py");
    } catch (e) {
      tried.push("面板：" + e);
    }

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

  /**
   * 起一个管理面板进程（`tools\gui.py` + 任意参数）。返回 true/false。
   *
   * 为什么抽出来：面板不止一个"带参数启动"的用途 —— 打开某个 md
   * （`--open <path>`）与打开 MinerU 安装引导（`--tab struct --mineru-guide`）
   * 走的是同一条链。分开写两份的话，Subprocess / nsIProcess 那套兜底
   * 迟早只有一处被修。
   */
  panelProcess: function (extraArgs) {
    var self = ZoteroKB;
    const root = self.projectRoot();
    const py = self.pythonExe();
    const gui = root ? root + "\\tools\\gui.py" : "";
    if (!py || !root || !self._exists(gui)) return false;
    const args = ["-X", "utf8", gui].concat(extraArgs || []);
    try {
      const { Subprocess } = ChromeUtils.importESModule(
        "resource://gre/modules/Subprocess.sys.mjs");
      Subprocess.call({
        command: py, arguments: args, workdir: root,
        stderr: "ignore", stdout: "ignore",
      }).catch(function (e) {
        Zotero.debug("[zotero-kb] 面板进程：" + e);
      });
      return true;
    } catch (e) {
      // Subprocess 不可用时退回 nsIProcess（与 openPanel 同一套兜底）
      try {
        const nsLocalFile = Components.Constructor(
          "@mozilla.org/file/local;1", "nsIFile", "initWithPath");
        const proc = Components.classes["@mozilla.org/process/util;1"]
          .createInstance(Components.interfaces.nsIProcess);
        proc.init(nsLocalFile(py));
        proc.runw(false, args, args.length);
        return true;
      } catch (e2) {
        Zotero.debug("[zotero-kb] 面板进程也起不来：" + e2);
        return false;
      }
    }
  },


  /**
   * 用面板的 Python 打开一个文件（`tools/gui.py --open <path>`）。
   *
   * 为什么不直接在这里调 shell：插件沙箱里没有可靠的"用默认程序打开"接口，
   * 而面板那边 `os.startfile` 一直在用、确定可用。让它代劳最省事。
   */
  openKbViaPanel: function (path) {
    return this.panelProcess(["--open", path]);
  },

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
