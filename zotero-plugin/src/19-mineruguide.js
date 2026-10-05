/**
 * 19-mineruguide.js —— MinerU（可选组件）的首次安装引导。
 *
 * ## 用户定的规矩（2026-10-05）
 *
 *   · 插件**首次启动**、且**没检测到** MinerU 时，弹**一次**对话框：
 *     「打开安装引导」/「以后再说」。
 *   · 选「以后再说」就收进角落 —— 入口留在面板「知识库结构」页那个按钮里
 *     （面板那边**只在没装时才显示**，装了按钮就消失）。
 *   · 所以：**无论选哪个都记 `mineruGuideDone`**，不再打扰。
 *   · 已经装了 MinerU 的用户**一次都不弹**（连 pref 都不写）。
 *
 * ## 为什么先本地看一眼、再问服务
 *
 * 装 MinerU 的默认位置是项目目录下的 `.mineru\.venv\Scripts\mineru-kit.exe`
 * （见 scripts/install-mineru.ps1）。这个判断**不需要服务在跑**，一次
 * `exists` 就够，成本最低、也最不容易误报"没装"。
 * 但也可能是用户自己 pip 装在别处（或者在 PATH 上），那只有服务端
 * `/mineru-check`（走 schemas.resolve_mineru 的六级优先级）知道 ——
 * 所以本地没找到时再问一次服务，服务没起来就按"没装"处理（提示用户去装）。
 *
 * ## 为什么不在这里直接弹 Tkinter 向导
 *
 * 插件沙箱里起不了窗口，而且安装要实时日志、要能取消 —— 那些都在面板里
 * （`tools/panels/mineru_guide.py`）。这里只负责"拉面板并把窗口打开"，
 * 用与「打开知识库」同一条 `Subprocess` + `nsIProcess` 兜底链。
 */
Object.assign(ZoteroKB, {

  /** 默认安装在项目内的位置（与 install-mineru.ps1 的约定一致）。 */
  mineruKitPath: function () {
    const root = this.projectRoot ? this.projectRoot() : "";
    return root ? root + "\\.mineru\\.venv\\Scripts\\mineru-kit.exe" : "";
  },


  /**
   * 排一次首启引导检查。
   *
   * 延迟 8 秒：① 让主窗口、设置面板先就绪（首屏别抢时间）；
   * ② 让本地服务有机会起来（`/health` 那次探测在 startup 里）。
   * 用 `setTimeout` 而不是窗口钩子 —— 本机实测 `onMainWindowLoad` 在
   * **Zotero 启动时已存在的那个窗口**上不会被调用（见 00-core.js 的说明）。
   */
  scheduleMineruGuide: function () {
    var self = ZoteroKB;
    setTimeout(function () {
      try { self.maybeAskMineruGuide(); } catch (e) {
        Zotero.debug("[zotero-kb] MinerU 引导检查失败：" + e);
      }
    }, 8000);
  },


  /** 首启检查：装了就什么都不做；没装就弹一次（然后就再也不弹）。 */
  maybeAskMineruGuide: async function () {
    var self = ZoteroKB;
    if (self.getPref(self.PREFS.mineruGuideDone, false)) return;

    // ① 本地快速判断（不依赖服务）
    const kit = self.mineruKitPath();
    if (kit && self._exists && self._exists(kit)) return;

    // ② 问服务（可能装在别的路径）；服务没起来就问不到，按"没装"处理
    let installed = false;
    try {
      const info = await self.request("GET", "/mineru-check");
      installed = !!(info && info.ok);
    } catch (e) {
      Zotero.debug("[zotero-kb] /mineru-check 不可用（服务没起来？）：" + e);
    }
    if (installed) return;

    // ③ 只弹一次：选什么都要记，免得每次启动都烦人
    try { self.setPref(self.PREFS.mineruGuideDone, true); } catch (e) { /* ignore */ }
    if (self.askMineruGuide() === 0) self.openMineruGuide();
  },


  /**
   * 弹对话框。返回 0 =「打开安装引导」，1 =「以后再说」。
   *
   * 为什么用 `Services.prompt.confirmEx` 而不是自绘窗口：它是**同步**的、
   * 三个按钮的文案都能自定义，而且不需要额外 xhtml —— 设置面板那套复用不了
   * （那个是 Zotero 的 pane 容器，得挂在设置里才出现）。
   */
  askMineruGuide: function () {
    var self = ZoteroKB;
    // ⚠ 不要在界面文案里写 markdown 的 `**`：Tk 与 Zotero 的对话框都不渲染它，
    //   用户看到的就是一串星号（用户提过这个毛病，本机也踩过）。
    const body = "MinerU 是「可选」的 PDF 解析增强：装了它，正文更干净、"
      + "公式会变成 LaTeX（能进检索与摘要）、表格与扫描件更稳。\n\n"
      + "不装完全不影响现有功能（知识库仍用 Zotero 缓存文本 + PyMuPDF）。\n"
      + "装它要下 5~9 GB、首次 5~20 分钟；装到项目目录下的 .mineru\\，"
      + "不写系统 PATH、不动注册表，删目录即卸载。\n\n"
      + "要不要现在打开安装引导？";
    try {
      const ps = Services.prompt;
      const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
        + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
      const win = (Zotero.getMainWindow && Zotero.getMainWindow()) || null;
      const btn = ps.confirmEx(
        win, "知识库：可选装 MinerU（能让 PDF 解析更准）", body, flags,
        "打开安装引导", "以后再说", null, null, {});
      return btn;
    } catch (e) {
      Zotero.debug("[zotero-kb] 弹 MinerU 引导失败：" + e);
      return 1;
    }
  },


  /** 拉面板并直接打开「MinerU 安装引导」窗口。 */
  openMineruGuide: function () {
    var self = ZoteroKB;
    const ok = self.panelProcess
      ? self.panelProcess(["--tab", "struct", "--mineru-guide"])
      : false;
    if (!ok) {
      self.notify("打不开安装引导",
        "没能拉起管理面板。可以自己打开面板 →「知识库结构」页 →"
        + "「MinerU 安装引导」，或者双击项目里的 scripts\\install-mineru.cmd。",
        null, true);
    }
    return ok;
  },
});
