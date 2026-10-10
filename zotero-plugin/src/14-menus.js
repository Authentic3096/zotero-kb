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
        + "· 管理面板「损坏查询」页的日志\n"
        + "· " + self.kbDir() + "\\logs\\ 下的日志\n"
        + (r.out ? ("\n" + String(r.out).slice(-800)) : ""));
    }
  },


  // ================================================================ 分节纲要

  /**
   * 这一篇生成过「分节纲要」没有（右键菜单据此决定跳不跳）。
   *
   * 判据是**文件在不在**（views/<KEY>.outline.md），不是索引库里的行 ——
   * 纲要刻意不入索引（见 offline/digest.py 开头那段），所以文件是唯一权威来源。
   * 走 kbLevels 而不是自己拼路径：路径模板只有 KB_LEVELS 一份，再拼一次
   * 就是第二份实现，早晚漂移（本项目吃过"两份实现必然漂移"的亏）。
   *
   * ⚠ 判不出来（知识库位置未知等）返回 false：交给服务端自己按节指纹复用，
   *   那是诚实的兜底 —— 不能把"我不知道"说成"已经有纲要了"。
   */
  hasOutline: function (key) {
    var self = ZoteroKB;
    try {
      const lv = (self.kbLevels(key) || []).find((x) => x.id === "outline");
      return !!(lv && lv.exists);
    } catch (e) {
      return false;
    }
  },


  /**
   * 给选中的条目生成「分节纲要」（条目右键菜单 → 生成分节纲要）。
   *
   * 后端是服务端的 `POST /digest`（一个异步 job）→ `python offline/digest.py <KEY>`。
   * 为什么不在插件里跑 Python：venv、MinerU 产物、模型全在服务端管 ——
   * 与「重建本条目知识库」同一套分工（见上面 runPython 的说明）。
   *
   * 三条硬规则（都来自本机的实测条件）：
   *   ① **串行**：纲要要一节一节调本地模型，本机是 4B 小模型 + 单 GPU，
   *      并发提交只会让两篇都变慢，还可能把 Ollama 拖住；
   *   ② **失败不中断整批**：一篇没解析过 PDF 的文献不该让后面几篇都不跑 ——
   *      逐篇记结果，最后一起汇总（"失败 K 篇"连原因一起给）；
   *   ③ **已有纲要的跳过**：省在学位论文上最明显（读 MinerU 产物 + 重渲染
   *      + 写回档案都不便宜），而且用户的语义就是"我只要还没有的那些"。
   *      真要重跑已有的一篇：管理面板「AI」页的「生成纲要」按钮（那条路
   *      也是 force=False 的增量，已经跑过的节不会重问模型）。
   *
   * ⚠ 进度窗只有 Zotero.ProgressWindow 一个入口，而它**没有"改文字"的 API**
   *   （只有 changeHeadline / addDescription），所以"更新进度"只能是"关掉旧的、
   *   开一个新的"。为了不闪，只在"换篇"或"日志变了且距上次 ≥4 秒"时重画。
   */
  generateOutlines: async function (items) {
    var self = ZoteroKB;
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    // 同一时刻只跑一批：再点一次是"提示"而不是"叠第二批"
    // （叠起来两批会同时打 Ollama，进度窗也会互相顶掉）。
    if (self.digestActive) {
      const cur = self.digestActive;
      self.alertDialog(
        "已经有一批在跑",
        "正在生成：「" + (cur.currentLabel || cur.currentKey || "") + "」"
        + "（" + (cur.i + 1) + "/" + cur.total + "）\n\n"
        + "要停就再点一次右键菜单里的「停止生成纲要」。");
      return;
    }
    try {
      if (!(await self.healthCheck())) {
        self.notify("知识库服务没启动",
          "纲要要由本地服务调模型生成。\n"
          + "先把它起起来：管理面板的「运行环境」页，或跑 scripts\\0-panel.vbs。",
          null, true);
        return;
      }
    } catch (e) {
      // 探活失败也要往下走：真正的失败原因由 POST /digest 给（比这里编一句准）
      Zotero.debug("[zotero-kb] 生成纲要前的探活失败：" + e);
    }

    const todo = [], skipped = [];
    for (const it of list) {
      if (self.hasOutline(it.key)) skipped.push(it);
      else todo.push(it);
    }
    if (!todo.length) {
      self.alertDialog("都已经有分节纲要了",
        "选中的 " + list.length + " 篇都有纲要，这次没有要生成的。\n\n"
        + "想重新生成某一篇：管理面板 →「AI」页 → 选中这篇 →「生成纲要」"
        + "（已有的节按指纹复用，不会重复烧模型）。");
      return;
    }

    const st = self.digestActive = {
      total: todo.length, i: 0, currentKey: "", currentLabel: "",
      generated: [], failed: [],
      skipped: skipped, skippedByServer: [],
      stopped: false, startedAt: Date.now(), lastPaint: 0, lastLog: "",
    };
    Zotero.debug("[zotero-kb] 开始生成分节纲要：" + todo.length + " 篇（已跳过 "
                 + skipped.length + " 篇）");

    const paint = (headline, text, force) => {
      const now = Date.now();
      if (!force && now - (st.lastPaint || 0) < 4000) return;
      st.lastPaint = now;
      try { self.newProgress(headline, text); } catch (e) { /* ignore */ }
    };

    /** 跑一篇：提交 → 轮询 job → 分类结果。**自己吞掉异常**（失败不中断整批）。 */
    const runOne = async (it, idx) => {
      st.i = idx;
      st.currentKey = it.key;
      st.currentLabel = self.itemLabel(it);
      const head = "正在生成分节纲要（" + (idx + 1) + "/" + st.total + "）";
      st.lastPaint = 0;                  // 换篇必须重画一次
      paint(head, st.currentLabel + "\n" + it.key, true);
      let job = null;
      try {
        const res = await self.request("POST", "/digest", { key: it.key });
        const jobId = res && res.job;
        if (!jobId) throw new Error((res && res.error) || "服务端没返回 job id");
        // 学位论文几分钟，给到 30 分钟（真超时也只是"这一篇算失败"，
        // 不中断后面的；服务端那个 job 仍在跑，下一次会命中的它的结果）。
        const deadline = Date.now() + 30 * 60 * 1000;
        while (Date.now() < deadline) {
          if (st.stopped) {
            // 用户点了停止：**每一轮都发一次**取消请求。服务端是"到下一节
            // 边界才收工"，而这一篇可能正卡在一节几分钟的模型调用里，
            // 只发一次不够稳（服务端可能刚好还没进循环）。
            self.request("POST", "/digest/cancel", {}).catch(() => {});
          }
          await new Promise((r) => setTimeout(r, 2000));
          job = await self.request("GET", "/jobs/" + jobId);
          if (!job) break;
          const log = job.log || [];
          const tail = log.length ? String(log[log.length - 1]).trim() : "";
          if (tail) st.lastLog = tail;
          paint(head, st.currentLabel + "\n" + it.key
            + "\n\n已跑 " + Math.round((Date.now() - st.startedAt) / 1000) + " 秒"
            + (st.lastLog ? ("\n" + st.lastLog) : ""), false);
          if (job.state === "done" || job.state === "failed") break;
        }
      } catch (e) {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String((e && e.message) || e) });
        return;
      }
      if (!job || (job.state !== "done" && job.state !== "failed")) {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: "等服务端超时（30 分钟）" });
        return;
      }
      if (job.state === "failed") {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String(job.error || "任务失败") });
        return;
      }
      // ---- 分类这一篇的结果
      // 四种都出现过，所以逐个点名（别用 truthy 一把抓）：
      //   · 真生成了几节；
      //   · 服务端说"一节都没问"（全是节指纹复用）= 跳过；
      //   · 被用户停掉（cancelled）；
      //   · 失败（ok=false，why 里是"没有 MinerU 产物"这类可行动的原因）。
      const r = job.result || {};
      if (r.cancelled) {
        // ⚠ 服务端给的 why 已经是以「已取消（…）」开头的整句，别再包一层
        //   （本机验收第一版就是这么写出「已取消（已取消（4/8 节已跑完…））」的）。
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String(r.why || "已取消") });
      } else if (r.ok) {
        // 「跳过」= **本来就有纲要、这次一节都没问模型**（asked=0 且文件本来就在）。
        // ⚠ 只判 asked=0 是不够的：本机实测有 48 条「meta 里有纲要、但
        //   views/<key>.outline.md 不在」的条目（2026-10-05 那批只落了 meta），
        //   它们这次会把**文件补出来** —— 那是实打实的产出，不是跳过。
        if (r.skipped && r.existed_before) {
          st.skippedByServer.push({ key: it.key, n: r.n_sections || 0 });
        } else {
          st.generated.push({ key: it.key, label: self.itemLabel(it),
                              n: r.n_sections || 0, asked: r.asked || 0,
                              sec: r.seconds || 0 });
        }
      } else {
        st.failed.push({ key: it.key, label: self.itemLabel(it),
                         why: String(r.why || "未知原因") });
      }
    };

    try {
      for (let i = 0; i < todo.length; i++) {
        if (st.stopped) break;
        await runOne(todo[i], i);
      }
    } catch (e) {
      Zotero.logError(e);
    } finally {
      self.digestActive = null;        // 菜单里的「停止」项随之消失
    }

    // ---- 汇总一句，口径就是用户要的原话：
    //      「已生成 N 篇、跳过 M 篇（已有纲要）、失败 K 篇」
    const nSkip = st.skipped.length + st.skippedByServer.length;
    const parts = ["已生成 " + st.generated.length + " 篇",
                   "跳过 " + nSkip + " 篇（已有纲要）",
                   "失败 " + st.failed.length + " 篇"];
    const secs = Math.round((Date.now() - st.startedAt) / 1000);
    const lines = [parts.join("、") + "　·　共 " + secs + " 秒"];
    // 明细只列前 6 条：进度窗太长了会顶到屏幕上边（它没有滚动条）。
    const detail = [];
    for (const g of st.generated.slice(0, 6)) {
      detail.push("✓ " + (g.label || g.key) + "　" + g.n + " 节"
                  + (g.asked
                     ? ("（新跑 " + g.asked + " 节，" + g.sec + " 秒）")
                     : "（全部复用已有内容，没调模型）"));
    }
    for (const s of st.skipped.slice(0, 3)) {
      detail.push("– 跳过：" + (s.getField ? self.itemLabel(s) : s.key));
    }
    for (const f2 of st.failed.slice(0, 6)) {
      detail.push("✗ " + (f2.label || f2.key) + "\n　　"
                  + String(f2.why).split("\n")[0].slice(0, 90));
    }
    if (st.generated.length + st.failed.length > 12) {
      detail.push("（明细只列前几条，完整结果在知识库的 plugin-status.json）");
    }
    if (st.stopped) detail.unshift("⏹ 中途停止（已经跑完的几篇是有效的）");
    st.summary = lines.concat(detail).join("\n");
    self.lastDigest = {
      at: new Date().toISOString().slice(11, 19), seconds: secs,
      generated: st.generated.length, skipped: nSkip, failed: st.failed.length,
      stopped: !!st.stopped,
      failedKeys: st.failed.map((x) => x.key),
      failedWhy: st.failed.slice(0, 5).map((x) => x.why.slice(0, 120)),
      summary: st.summary,
    };
    try {
      self.notify(st.failed.length ? "分节纲要：有没跑成的" : "分节纲要生成完成",
                  st.summary, null, !!st.failed.length);
    } catch (e) { /* ignore */ }
    // 两列立刻跟上 —— 不必等 15 个 tick（约 30 秒）那次轮询。
    // ⚠ 这只是"提前一下"：轮询没变、列的数据源没变，所以即使这句失败
    //   也不会让列永远停在 ✗。
    try { await self.refreshKbCols(); } catch (e) { /* ignore */ }
  },


  /** 停止正在跑的那一批纲要生成（右键菜单里的「停止生成纲要」）。
   *
   * ⚠ 这不是"杀线程"，而是**置一个标记**：服务端在每节开始前看一眼
   *   （见 localserver.do_digest 的 should_stop）。已经在跑的那一次模型
   *   调用会跑完，所以点完可能还要等十几秒才真的停 —— 提示里要说清，
   *   否则用户以为没点着。
   */
  stopDigest: function () {
    var self = ZoteroKB;
    const st = self.digestActive;
    if (!st) {
      self.notify("现在没有在跑的纲要生成", "", null, true);
      return;
    }
    if (st.stopped) {
      self.notify("已经在停了", "当前这一篇会在这一节跑完后停下。", null, false);
      return;
    }
    st.stopped = true;
    // 主循环里还会反复发（一篇可能正卡在一节长调用里），这里先发一次求快。
    self.request("POST", "/digest/cancel", {}).catch((e) => {
      Zotero.debug("[zotero-kb] 请求停止纲要失败：" + e);
    });
    self.notify("正在停止生成纲要…",
      "当前这一篇会在这一节的模型调用跑完后停下；\n"
      + "已经跑完的几篇不受影响。", null, false);
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
          // 清理上一次建的几样东西：六个顶层菜单项 + 上下两个分隔符。
          // 漏掉的话每弹一次右键菜单就会多留一份（会看出来越用越多）。
          // ⚠ 新建的顶层项**必须同时**在这里登记 id，否则下一轮就重复了。
          for (const id of ["zotero-kb-send-menu",
                            "zotero-kb-localmenu",
                            "zotero-kb-classify-item",
                            "zotero-kb-pin-item",
                            "zotero-kb-metafill-item",
                            "zotero-kb-outline-item",
                            "zotero-kb-digest-cancel",
                            "zotero-kb-rebuild-item",
                            "zotero-kb-open-menu",
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

          // ---- 标题按"能力状态"给（19-caps.js 探测，菜单只读缓存）
          //
          // 用户 2026-10-05：「右键文献的菜单能实现不填死吗，dsh 没接到就不显示」，
          // 随后拍板成**显示但标「未连接」**（藏起来反而让人找不到入口去修）。
          const dshCap = self.capLabel ? self.capLabel("dsh")
            : { label: "发送到 DSH", tooltip: "" };
          const lmCap = self.capLabel ? self.capLabel("localModel")
            : { label: "连接到本地模型", tooltip: "" };
          const menu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          menu.id = "zotero-kb-send-menu";
          menu.setAttribute("label", dshCap.label);
          if (dshCap.tooltip) menu.setAttribute("tooltiptext", dshCap.tooltip);
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

          // ---- 子菜单第一行：连接状态（用户要求"在悬浮的下级菜单显示链接没链接"）
          //
          // 为什么这一行要能点：`caps` 可能还没探过（菜单是同步构建的），
          // 点它就是"现在探一次"。点了之后不重建菜单（下次打开自然更新）。
          const dshState = self.capStateLine
            ? self.capStateLine("dsh", "") : { text: "", ok: false };
          mk(dshState.text, () => {
            self.refreshCaps(true).then(() => {
              self.notify("DSH 连接检查",
                (self.capLabel ? self.capLabel("dsh").tooltip : "")
                + "\n\n当前：" + (self.capStateLine
                    ? self.capStateLine("dsh").text : ""), null, true);
            }).catch(() => {});
          }, { iconic: false, ready: "",
               tooltip: "点这一行立刻重新检查 DSH 连接" });

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
            // ⚠ 用**这次真拿到的结果**回填能力缓存：原来只靠"周期探测"，
            //   于是出现"对话列表都列出来了、标题却写（检测中）"的矛盾
            //   （用户 2026-10-05 截图反馈）。列表能拿到 = DSH 连上了。
            if (rows && rows.length) {
              self.caps.dsh = true;
              self.caps.dshWhy = "";
              self.caps.dshTimeoutAt = 0;
            } else if (self.caps.dsh === null) {
              self.caps.dsh = false;
              self.caps.dshWhy = "能投递但没拿到对话列表（DSH 侧插件没响应？）";
            }
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
          // ----「连接到本地模型」二级菜单（用户要求：原来那两条合并到这里）
          //
          // 为什么要合并：两条（分类建议 / 补全元数据）都是"要本地模型"的动作，
          // 平铺在顶层既占地方、又让"模型没连上"这件事没有统一的落点 ——
          // 合并之后标题本身就能写「（未连接）」，提示也只有一份。
          const lmMenu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          lmMenu.id = "zotero-kb-localmenu";
          lmMenu.setAttribute("class", "menu-iconic");
          lmMenu.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          lmMenu.setAttribute("label", lmCap.label);
          lmMenu.setAttribute("tooltiptext", lmCap.tooltip || "");
          const lmPopup = doc.createXULElement
            ? doc.createXULElement("menupopup") : doc.createElement("menupopup");
          lmMenu.appendChild(lmPopup);
          // ⚠ 子菜单里**不能**放 disabled 的 menuitem（会让整个子菜单点不开，
          //   见本文件下面「选择已有对话」那段的教训）。所以"未连接"用一条
          //   普通 menuitem 说明，点了只提示、不做动作。
          {
            // 本地模型子菜单的第一行同样是状态行（与 DSH 一致）；
            // 点击会给"怎么办"的两条路，而不是只报错。
            const lmState = self.capStateLine
              ? self.capStateLine("localModel", "") : { text: "", ok: false };
            mkIn(lmPopup, lmState.text, () => {
              self.refreshCaps(true).then(() => {
                self.notify("本地模型检查",
                  (self.caps.localWhy || "可用的")
                  + "\n\n① 面板「运行环境」→「启动 Ollama」；"
                  + "\n② 或在「模型接入」里改用 API 模型（填地址与 Key）。",
                  null, true);
              }).catch(() => {});
            }, { ready: "", tooltip: lmCap.tooltip || "" });
          }

          const classify = doc.createXULElement
            ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
          classify.id = "zotero-kb-classify-item";
          classify.setAttribute("class", "menuitem-iconic");
          classify.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          classify.setAttribute(
            "label", real.length > 1
              ? ("分类建议 · " + real.length + " 篇")
              : "分类建议");
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

          // ⚠ 必须显式挂进「连接到本地模型」子菜单：原来它是靠插入时那句
          //   `menu.after(classify)` 蹭进 popup 的；改成二级菜单后那句没了，
          //   漏了这一行就会"分类建议"整项消失（菜单里不报错，很难查）。
          lmPopup.appendChild(classify);

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
          // ⚠ 这句原来写"权重 ×4"，与实现不符（用户 2026-10-05 指出）。
          //   真实口径：raw = 年份基础(0.95~1.05) + 3.0×重点 + 人工分 + 2.0×有效…
          //   然后**取 1+ln(raw)**；基础权重 1 时标重点 = 1+ln(4) ≈ 2.4 倍，
          //   已有经验时 raw 更大、相对增幅更小（所以写"约"）。
          pinItem.setAttribute(
            "tooltiptext",
            "重点文献在检索时会明显往前排：权重乘数从约 1.0 提到约 2.4 倍"
            + "（算法是 1+ln(年份基础分 + 3×重点 + 经验加分)）。\n"
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
          const rebuild = mkIn(popup, "重建本条目知识库", () => {
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
          const metaFill = mkIn(lmPopup,
            "补全元数据"
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

          // ---- 第七项：生成分节纲要（本地模型）
          //
          // 为什么放右键（用户 2026-10-10 的要求）：纲要原来只能在管理面板的
          //   「AI」页点按钮生成；而在 Zotero 列表里看到「分节纲要」那一列是 ✗
          //   的那一刻，右键是最短路径（同「分类建议」「补全元数据」）。
          //
          // ⚠ 不能用 disabled 表达"这篇已经有纲要了" —— menupopup 里只要有一个
          //   disabled 的 menuitem，整个子菜单就点不开（本文件里记了两次的教训）。
          //   所以标题永远可点，跳过逻辑放进命令里，跑完再汇总告诉用户跳过了几篇。
          const missingN = real.filter((it) => !self.hasOutline(it.key)).length;
          const outlineItem = mkIn(lmPopup,
            "生成分节纲要"
              + (real.length > 1
                 ? ("· " + real.length + " 篇"
                    + (missingN < real.length
                       ? ("（跳过已有 " + (real.length - missingN) + "）") : ""))
                 : (missingN ? "" : "（已有，会跳过）")),
            () => {
              Promise.resolve(self.generateOutlines(real))
                .catch((e) => Zotero.logError(e));
            },
            {
              image: self.rootURI + "toolbar-icon.svg",
              ready: "",       // 它自己会弹进度/结果，不要那句"发送到 DSH"
              tooltip: "按章节给这篇写「这一节在做什么 + 关键点/参数/结论」，"
                + "每节带页码范围。\n"
                + "要调本地模型：论文几十秒、学位论文几分钟，逐篇串行跑。\n"
                + "已经有纲要的会跳过；跑完「分节纲要」这一列会变成 N 节。\n"
                + "跑的过程中可以在本菜单里点「停止生成纲要」。",
            });
          outlineItem.id = "zotero-kb-outline-item";

          // 停止项：**只在真有一批在跑时出现**。
          // 用"出现 / 不出现"而不是 disabled —— 见上面的老坑。
          if (self.digestActive) {
            const stopItem = mkIn(lmPopup,
              "停止生成纲要（" + (self.digestActive.i + 1) + "/"
                + self.digestActive.total + "）",
              () => { self.stopDigest(); },
              { image: self.rootURI + "toolbar-icon.svg", ready: "",
                tooltip: "当前这一篇会在这一节的模型调用跑完后停下；\n"
                  + "已经跑完的那几篇不受影响。" });
            stopItem.id = "zotero-kb-digest-cancel";
          }

          // ---- 第六项：打开知识库（分级）
          //
          // 为什么是**二级菜单**（用户 2026-10-05 的要求）：知识库目录里是
          // `papers/22X9PMR6.md` —— 文件名是 Zotero 的 key，人认不出是哪篇；
          // 让用户自己去目录里翻，等于把"找文件"又还给了他。所以：
          // 右键这一篇 → 打开知识库 → 选级别 → 直接打开那个 md。
          //
          // 多选时以**第一项**为准：级别是"某一篇的某个层面"，
          // 多选没有"共同的级别文件"这种东西。
          const first = real[0];
          const levels = self.kbLevels(first.key);
          const missing = levels.filter((lv) => !lv.exists)
            .map((lv) => lv.label);

          const openMenu = doc.createXULElement
            ? doc.createXULElement("menu") : doc.createElement("menu");
          openMenu.id = "zotero-kb-open-menu";
          openMenu.setAttribute("class", "menu-iconic");
          openMenu.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          openMenu.setAttribute(
            "label", "打开知识库" + (real.length > 1 ? "（第一项）" : ""));
          openMenu.setAttribute(
            "tooltiptext",
            "直接打开这一篇的某个层面，不用去知识库目录里按 key 找：\n"
            + "　" + levels.map((lv) => lv.label).join(" / ") + "\n"
            + (real.length > 1 ? "⚠ 一次选了多篇时，以第一项为准。\n" : "")
            + (missing.length
               ? ("⚠ 还没生成：" + missing.join("、")
                  + "\n　去面板的「高级」页点「补齐知识库分级文件」。")
               : "（这一篇的五个层面都已生成）"));

          const op = doc.createXULElement
            ? doc.createXULElement("menupopup") : doc.createElement("menupopup");
          openMenu.appendChild(op);
          for (const lv of levels) {
            const mi = doc.createXULElement
              ? doc.createXULElement("menuitem") : doc.createElement("menuitem");
            mi.setAttribute("class", "menuitem-iconic");
            // ⚠ 缺文件的级别**不能**设 disabled —— 本机实测（见上面
            //   「选择已有对话」那一项的注释）：menupopup 里只要有 disabled 的
            //   menuitem，整个子菜单就点不开。改成"文案里说明 + 点了给提示"。
            mi.setAttribute("label",
              lv.label + (lv.exists ? "" : "（还没生成）"));
            mi.setAttribute("tooltiptext", lv.what + "\n\n" + lv.path);
            mi.addEventListener("command", () => {
              try { self.openKbLevel(first.key, lv.id); }
              catch (e) { Zotero.logError(e); }
            });
            op.appendChild(mi);
          }
          op.appendChild(doc.createXULElement
            ? doc.createXULElement("menuseparator")
            : doc.createElement("menuseparator"));
          const revealItem = mkIn(op, "在文件管理器里显示", () => {
            self.openKbFolder(first.key);
          }, { ready: "", tooltip: "在资源管理器里选中这一篇的知识库文件" });
          revealItem.setAttribute("image", self.rootURI + "toolbar-icon.svg");
          const panelItem = mkIn(op, "打开知识库管理面板", () => {
            self.openPanel();
          }, { ready: "", tooltip: "要做「补齐分级文件」这类批量操作时用它" });
          panelItem.setAttribute("image", self.rootURI + "toolbar-icon.svg");


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
            // 顺序：发送到 DSH（menu）→ 分类建议 → 标重点 → 补全元数据 →
            //       重建 → 打开知识库（分级）
            let ref = anchor;
            while (isRealSep(nextReal(ref))) ref = nextReal(ref);
            ref.after(menu);
            menu.after(lmMenu);
            lmMenu.after(pinItem);
            pinItem.after(rebuild);
            rebuild.after(openMenu);
            // 下方：下一个真实元素已经是分隔符就不用加
            if (!isRealSep(nextReal(openMenu))) openMenu.after(mkMarkedSep("zotero-kb-sep-after"));
            Zotero.debug("[zotero-kb] 菜单已插到「重建条目索引」之后（按需补分隔符）");
          } else {
            // 兜底：锚点找不到（Zotero 改了菜单结构）就放在最前面，
            // 至少保证"在插件组之前"，不会夹在别的插件中间
            const before = mkMarkedSep("zotero-kb-sep-before");
            popup.insertBefore(before, popup.firstChild);
            before.after(menu);
            menu.after(lmMenu);
            lmMenu.after(pinItem);
            pinItem.after(rebuild);
            rebuild.after(openMenu);
            if (!isRealSep(nextReal(openMenu))) openMenu.after(mkMarkedSep("zotero-kb-sep-after"));
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
