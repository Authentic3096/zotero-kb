/**
 * 01-lifecycle.js —— 卸载与关闭：`shutdown` / `install` / `uninstall`
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  shutdown: function () {
    // 同 startup：Zotero 调 shutdown() 时 `this` 同样不可靠，用闭包引用
    var self = ZoteroKB;
    self.alive = false;
    // 关掉可能还开着的进度提示（不留孤儿窗口）
    try { self.closeProgress(); } catch (e) { /* ignore */ }
    // Ollama 收尾：只有"标记文件在"（= 是我们拉起来的）才关。
    // 放在最前面 —— Zotero 关闭时留给 shutdown 的时间有限，先做要紧的。
    try { self.ollamaOnShutdown(); } catch (e) { /* ignore */ }
    // 本地服务同理：只关我们自己拉起的那一个（随弃随关）
    try { self.serverOnShutdown(); } catch (e) { /* ignore */ }
    try {
      if (Zotero.ZoteroKB === self) delete Zotero.ZoteroKB;
    } catch (e) { /* ignore */ }
    try { self.stopTaskPolling(); } catch (e) { /* ignore */ }
    try { self.unregisterWeightColumn(); } catch (e) { /* ignore */ }
    try { self.unregisterKbColumns(); } catch (e) { /* ignore */ }
      try { self.unregisterKbViewSection(); } catch (e) { /* ignore */ }
    // ⚠ 2026-10-05：内容窗格分区（ItemPaneManager）、退出提醒（quit guard）、
    //   以及"对话不落盘所以清掉 chatState"这三件事随窗格一起删了。
    //   阅读器的监听本来也不需要手动摘（registerEventListener 传了 pluginID）。
    try {
      if (self.notifyIDs && self.notifyIDs.length) {
        self.notifyIDs.forEach((id) => Zotero.Notifier.unregisterObserver(id));
        self.notifyIDs = [];
      }
    } catch (e) { Zotero.logError(e); }
    try {
      if (self.prefObserver) {
        Zotero.Prefs.unregisterObserver(self.prefObserver);
        self.prefObserver = null;
      }
    } catch (e) { Zotero.logError(e); }
    Zotero.debug("[zotero-kb] 已卸载");
  },


  install: function () {},

  uninstall: function () {},


  // ================================================================ 界面文案（ftl）

  /**
   * 把插件自带的 ftl 挂到窗口文档上。**这一步不能省。**
   *
   * Zotero 只做了一半：它把插件 `locale/<语言>/*.ftl` 读进 `L10nRegistry` 的
   * `zotero-plugins` 源（`plugins.js` 的 `registerLocales`，在 `startup` 之前跑），
   * 但**文档必须自己声明要用这个资源** —— 主窗口的 `linkset` 里只有
   * `zotero.ftl` / `reader.ftl` 那几条。少了这一步，我们设的
   * `data-l10n-id` 谁都不认识：元素**保持空白**（不报错、也不显示 id），
   * 表现就是"内容窗格里一排按钮全是空框"。
   *
   * 2026-10-05 实测（用户截图报的正是这个）：删了 `initLocale` 之后，
   * 分区能出现、状态行有字（那是代码里的中文字符串），但 **6 个按钮全空**、
   * 分区标题与输入框 placeholder 也空。
   *
   * 两个动作：
   *   ① `MozXULElement.insertFTLIfNeeded(文件名)` —— 往文档的 linkset 里加一条
   *      `<link rel="localization" href="zotero-kb.ftl">`（Firefox 的标准做法，
   *      本机 5 个能正常显示文案的插件都是这么干的）；拿不到 MozXULElement
   *      时**自己插那条 link**（那段代码就是它的实现，只是多一层保险）。
   *   ② `Zotero.ftl.addResourceIds([文件名])` —— 让**程序化**取字符串也行
   *      （`l10nText()` 给 confirmEx 弹窗用；那是同步 API，只能走这条路）。
   *
   * 不抛错：文案是装饰，缺了也不该让插件启动失败（只是界面难看）。
   */
  initLocale: function (win) {
    var self = ZoteroKB;
    const file = self.FTL_FILE;
    const result = { file: file, steps: [] };
    try {
      // ② 程序化取字符串（弹窗用）
      try {
        if (Zotero.ftl && Zotero.ftl.addResourceIds) {
          Zotero.ftl.addResourceIds([file]);
          result.steps.push("ftl.addResourceIds=ok");
        }
      } catch (e) {
        result.steps.push("ftl.addResourceIds=FAIL(" + e + ")");
      }

      const w = win || (Zotero.getMainWindow && Zotero.getMainWindow());
      if (!w || !w.document) {
        result.steps.push("no-window");
        self.writeStatusFile({ locale: JSON.stringify(result) });
        return result;
      }
      const doc = w.document;
      // ① 文档级：让 data-l10n-id 真的被翻译
      try {
        if (w.MozXULElement && w.MozXULElement.insertFTLIfNeeded) {
          w.MozXULElement.insertFTLIfNeeded(file);
          result.steps.push("insertFTLIfNeeded=ok");
        } else {
          result.steps.push("insertFTLIfNeeded=missing");
        }
      } catch (e) {
        result.steps.push("insertFTLIfNeeded=FAIL(" + e + ")");
      }
      // 兜底：自己插那条 link（与 insertFTLIfNeeded 的实现一致，幂等）
      try {
        const container = doc.head || doc.querySelector("linkset");
        if (container) {
          let have = false;
          for (const l of container.querySelectorAll("link")) {
            if (l.getAttribute("href") === file) { have = true; break; }
          }
          if (!have) {
            const link = doc.createElementNS(
              "http://www.w3.org/1999/xhtml", "link");
            link.setAttribute("rel", "localization");
            link.setAttribute("href", file);
            container.appendChild(link);
            result.steps.push("manual-link=ok");
          } else {
            result.steps.push("manual-link=already");
          }
        } else {
          result.steps.push("manual-link=no-container");
        }
      } catch (e) {
        result.steps.push("manual-link=FAIL(" + e + ")");
      }
      self.writeStatusFile({ locale: JSON.stringify(result) });
    } catch (e) {
      Zotero.debug("[zotero-kb] initLocale 失败：" + e);
    }
    return result;
  },

  // ================================================================ 首选项

  PREFS: {
    server: "zotero-kb.server",
    token: "zotero-kb.token",
    model: "zotero-kb.model",
    autoProcess: "zotero-kb.autoProcess",
    categories: "zotero-kb.categories",
    // ---- 模型来源（支持多后端）
    provider: "zotero-kb.provider",     // ollama | openai
    apiModel: "zotero-kb.apiModel",     // 如 deepseek-chat
    apiKey: "zotero-kb.apiKey",
    apiBaseUrl: "zotero-kb.apiBaseUrl",
    // 从 DSH 导入文献前是否先弹确认框（**默认关**）。
    //
    // ⚠ 这里原来默认是 true，被用户否掉了。用户的原话：
    //   "查询文献都是在dsh中做，那下载列表最好也是通过dsh的对话中显示，
    //    这样才是更符合使用习惯的"
    //   —— 搜索、列表、挑选本来就在 DSH 对话里完成，再弹一个 Zotero 模态框
    //   等于把一次连贯的对话中断成两个界面。**确认应该在用户做选择的地方。**
    //   所以：确认与回报都挪到 DSH 对话里，Zotero 这边默认静默入库。
    //   想要老行为的人可以在设置面板把这个勾打开。
    acquireConfirm: "zotero-kb.acquireConfirm",
    // 从 DSH 导入的文献，落库后是否走一遍**本地模型的分类/打标签**流程。
    //
    // 用户明确要这一步（"填进去后走本地大模型自动打标签分类的流程"）。
    // 一次性全部算完、再用**一个**汇总框问（见 askApplyBatch）——
    // 不用每篇弹一次：抓 5 篇弹 5 个模态框，点完"添加"还要连点 5 次。
    acquireAutoClassify: "zotero-kb.acquireAutoClassify",
    // ---- MinerU（可选组件）的首次安装引导
    //
    // 「首次启动且没检测到 MinerU 时弹一次，之后不再打扰」—— 用户定的规矩。
    // 所以**无论用户选「打开安装引导」还是「以后再说」都会把它置为 true**；
    // 装了 MinerU 的用户一次都不弹（这个 pref 也不会被写）。
    // 面板那侧还有入口：「知识库结构」页那个只在未安装时出现的按钮。
    mineruGuideDone: "zotero-kb.mineruGuideDone",
    // 可选组件（MinerU + Ollama）的安装引导是否已经问过。
    // ⚠ 旧版本只有 mineruGuideDone；升级上来的用户那条 pref 还是 true，
    //   所以 19-mineruguide.js 里**两个都认**，不会二次打扰。
    optionalGuideDone: "zotero-kb.optionalGuideDone",
    // 右侧栏「知识库」分区显示哪一级（2026-10-05 新增，见 20-kbview.js）。
    // 默认「分节纲要」—— 那一层就是为"对着 PDF 读"设计的（每节带页码范围）。
    kbviewLevel: "zotero-kb.kbviewLevel",
    // 右侧栏「知识库」分区的字号（px）。**只作用于我们这一块**（容器上的内联
    // font-size），不动 Zotero 的任何默认设置。
    kbviewFont: "zotero-kb.kbviewFont",
    // ⚠ 2026-10-05：`chatQuitWarn` / `chatNumCtx` 两个 pref 随内容窗格聊天
    //   一起删了（见 00-core.js 里那段说明）。prefs.js 里的默认值也一并删了。
  },
});
