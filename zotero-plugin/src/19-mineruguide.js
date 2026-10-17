/**
 * 19-mineruguide.js —— **可选组件**的首次安装引导（MinerU 与 Ollama 两个）。
 *
 * ## 规矩
 *
 *   · 插件**首次启动**、且**没检测到**某个可选组件时，弹**一次**对话框：
 *     「打开面板去装」/「以后再说」。
 *   · 「打开面板去装」打开管理面板；真正的安装按钮在
 *     「偏好设置 → 可选组件安装引导」（MinerU 开始安装 / Ollama 拉取推荐模型）。
 *     这里不再重复安装说明（两处必然漂移）。
 *   · 无论选哪个都记 `optionalGuideDone`，不再打扰；已经装齐的用户一次都不弹。
 *
 * ## 为什么先本地看一眼、再问服务
 *
 * MinerU 的默认位置是项目目录下的 `.mineru\.venv\Scripts\mineru-kit.exe`
 * （见 scripts/install-mineru.ps1）；Ollama 的默认位置是
 * `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`。这两个判断**不需要服务在跑**，
 * 一次 `exists` 就够，成本最低、也最不容易误报"没装"。
 * 但也可能装在别处（PATH 上、或用户自定义路径），那只有引擎知道：
 * MinerU 走引擎桥的 `/mineru-check`（`schemas.resolve_mineru()` 六级优先级），
 * Ollama 走引擎桥的 `/health`（里面带 `ollama` 字段，同样是 `resolve_ollama()`）。
 * 引擎没起来就按"本地看到什么算什么"处理。
 * ⚠ 2026-10-16：这两条原来各有一条"老 HTTP（8765）兜底"，已删 —— 这段代码跑在
 *   启动流程里，而 8765 已退役，兜底必然失败、只剩噪音。
 *
 * ## 本文件只做"首启引导"
 *
 * 真正的安装动作在管理面板「偏好设置 → 可选组件安装引导」：MinerU 跑项目里的
 * `scripts\install-mineru.ps1`、Ollama 打开官网下载页 / `ollama pull`。
 * 这里只负责**告诉用户去哪**并打开面板，不再各写一份安装说明。
 */
Object.assign(ZoteroKB, {

  /** MinerU 默认安装在项目内的位置（与 install-mineru.ps1 的约定一致）。 */
  mineruKitPath: function () {
    const root = this.projectRoot ? this.projectRoot() : "";
    return root ? root + "\\.mineru\\.venv\\Scripts\\mineru-kit.exe" : "";
  },


  /** Ollama 默认安装位置（按用户安装，不需要管理员）。 */
  ollamaExePath: function () {
    // 用 %LOCALAPPDATA% 拼（Ollama 官方安装器就装在那里）。拿不到就返回空 ——
    // 这是"快速看一眼"用的，真正的判定还有服务端 `/health` 兜底，
    // 所以宁可不猜也不写死某个用户名的路径。
    try {
      const env = Services.env || Components.classes[
        "@mozilla.org/process/environment;1"]
        .getService(Components.interfaces.nsIEnvironment);
      const lad = env.get("LOCALAPPDATA") || "";
      if (lad) return lad + "\\Programs\\Ollama\\ollama.exe";
    } catch (e) { /* ignore */ }
    return "";
  },


  /**
   * 排一次首启引导检查。
   *
   * 延迟 8 秒：① 让主窗口、设置面板先就绪（首屏别抢时间）；
   * ② 让引擎有机会起来（healthCheck 那次引擎桥探测在 startup 里 ——
   *    2026-10-16 起它**不碰老 HTTP 8765**，这里也不再等那个服务）。
   * 用 `setTimeout` 而不是窗口钩子 —— 本机实测 `onMainWindowLoad` 在
   * **Zotero 启动时已存在的那个窗口**上不会被调用（见 00-core.js 的说明）。
   */
  scheduleOptionalGuides: function () {
    var self = ZoteroKB;
    setTimeout(function () {
      try { self.maybeAskOptionalGuides(); } catch (e) {
        Zotero.debug("[zotero-kb] 可选组件引导检查失败：" + e);
      }
    }, 8000);
  },


  /** 首启检查：都装了什么都不做；缺哪个就弹一次（然后就再也不弹）。 */
  maybeAskOptionalGuides: async function () {
    var self = ZoteroKB;
    // 新 pref 优先；老的 mineruGuideDone 也认（升级上来的用户不会被再烦一次）
    if (self.getPref(self.PREFS.optionalGuideDone, false)
        || self.getPref(self.PREFS.mineruGuideDone, false)) return;

    const missing = [];
    // ① MinerU：先看本地文件，再问引擎桥
    const kit = self.mineruKitPath();
    if (!(kit && self._exists && self._exists(kit))) {
      let seen = false;
      // ⚠ 2026-10-16（用户拍板"HTTP 噪音一起清"）：**删掉老 HTTP `/mineru-check`
      //   兜底**。这条探测跑在**启动流程**里，而 8765 已退役 —— 兜底必然失败，
      //   只剩"每次启动多一次连接错误"。桥（`mineru.checkRaw` = `mineru.probe()`
      //   原文）拿不到就算没看到，不再去够老服务。
      try {
        const info = await self.kbAdminHostRoute("GET", "/mineru-check");
        seen = !!(info && info.ok);
      } catch (e) {
        Zotero.debug("[zotero-kb] 引擎桥查 MinerU 不可用：" + e);
      }
      if (!seen) missing.push("mineru");
    }
    // ② Ollama：先看本地文件，再问引擎桥的 /health
    const ol = self.ollamaExePath();
    if (!(ol && self._exists && self._exists(ol))) {
      let seen = false;
      try {
        // ⚠ 2026-10-16：同 ①，**删掉老 HTTP /health 兜底**（启动路径不许碰 8765）。
        const info = await self.kbAdminHostRoute("GET", "/health");
        seen = !!(info && info.ollama);
      } catch (e) {
        Zotero.debug("[zotero-kb] 引擎桥 /health 不可用：" + e);
      }
      if (!seen) missing.push("ollama");
    }
    if (!missing.length) return;

    // ③ 只弹一次：选什么都要记，免得每次启动都烦人
    // ⚠ 这里原来写 `self.setPref(...)` —— 插件里**没有**这个包装函数（写 pref 用
  //   `Zotero.Prefs.set`），而它又在 try/catch 里，于是静默失败：
  //   后果是「引导只弹一次」的 pref 从来没写进去，缺 MinerU/Ollama 的用户
  //   **每次启动都会被弹一次**。2026-10-05 由新的成员引用检查扫出来。
  try { Zotero.Prefs.set(self.PREFS.optionalGuideDone, true); }
  catch (e) { /* ignore */ }
    if (self.askOptionalGuide(missing) === 0) self.openOptionalGuide(missing[0]);
    return missing;
  },


  /**
   * 弹对话框。`missing` 是缺的组件名数组（"mineru" / "ollama"）。
   * 返回 0 =「打开面板去装」，1 =「以后再说」。
   *
   * 为什么用 `Services.prompt.confirmEx` 而不是自绘窗口：它是**同步**的、
   * 两个按钮的文案都能自定义，而且不需要额外 xhtml。
   */
  askOptionalGuide: function (missing) {
    const names = (missing || []).map(function (m) {
      return m === "mineru" ? "MinerU（让 PDF 解析更准，公式变 LaTeX）"
                            : "Ollama（本地模型，分类/摘要/大纲可离线跑）";
    });
    const body = "知识库有两个**可选**组件还没装：\n\n"
      + names.map(function (n) { return "  · " + n; }).join("\n")
      + "\n\n不装完全不影响现有功能：\n"
      + "  · 不装 MinerU：正文仍用 Zotero 缓存 + PyMuPDF 解析；\n"
      + "  · 不装 Ollama：在面板「运行环境 → 模型配置」里改成用 API（DeepSeek 等）即可。\n\n"
      + "要打开管理面板去装吗？\n"
      + "  面板 →「偏好设置」→「可选组件安装引导」：\n"
      + "  · MinerU 有「开始安装」（装到项目内 .mineru\\）；\n"
      + "  · Ollama 有「打开官网下载页」和「拉取推荐模型」。";
    try {
      const ps = Services.prompt;
      const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
        + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
      const win = (Zotero.getMainWindow && Zotero.getMainWindow()) || null;
      return ps.confirmEx(win, "知识库：可选组件（不装也能用）", body, flags,
                          "打开面板去装", "以后再说", null, null, {});
    } catch (e) {
      Zotero.debug("[zotero-kb] 弹可选组件引导失败：" + e);
      return 1;
    }
  },


  /**
   * 「打开面板去装」：打开管理面板（安装按钮在「偏好设置」页）。
   *
   * ⚠ 这里不重复安装步骤 —— 面板里那两块是唯一实现（MinerU 跑
   *   scripts\install-mineru.ps1；Ollama 打开官网 / ollama pull）。
   * 返回 false 让调用方知道"没有自动安装"。
   */
  openOptionalGuide: function (which) {
    var self = ZoteroKB;
    try { if (self.openPanel) self.openPanel(); } catch (e) { /* 面板打不开也不影响提示 */ }
    try {
      self.notify("装 " + (which === "ollama" ? "Ollama" : "MinerU") + "（可选组件）",
        "已打开管理面板：到「偏好设置 → 可选组件安装引导」点安装。\n"
        + "（MinerU 会装到项目内 .mineru\\；Ollama 会打开官网下载页 / 拉取推荐模型。）",
        null, false);
    } catch (e) { /* 提示失败不影响调用方 */ }
    return false;
  },
});
