/**
 * 19-mineruguide.js —— **可选组件**的首次安装引导（MinerU 与 Ollama 两个）。
 *
 * ## 用户定的规矩（2026-10-05）
 *
 *   · 插件**首次启动**、且**没检测到**某个可选组件时，弹**一次**对话框：
 *     「打开安装引导」/「以后再说」。
 *   · 选「以后再说」就收进角落 —— 入口留在面板「知识库结构」页那些按钮里
 *     （面板那边**只在没装时才显示**，装了按钮就消失）。
 *   · 所以：**无论选哪个都记 `optionalGuideDone`**，不再打扰。
 *   · 已经装齐的用户**一次都不弹**（连 pref 都不写）。
 *
 * 两个组件：
 *   · **MinerU**（PDF 解析增强，装了公式变 LaTeX）
 *   · **Ollama**（本地模型后端，装了分类/摘要能离线跑）—— 用户后加的要求：
 *     「『ollama 程序』能不能也像 minerU 那样做成『ollama（可选）』然后也有
 *     一样逻辑的图形化引导」。
 *
 * ## 为什么先本地看一眼、再问服务
 *
 * MinerU 的默认位置是项目目录下的 `.mineru\.venv\Scripts\mineru-kit.exe`
 * （见 scripts/install-mineru.ps1）；Ollama 的默认位置是
 * `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`。这两个判断**不需要服务在跑**，
 * 一次 `exists` 就够，成本最低、也最不容易误报"没装"。
 * 但也可能装在别处（PATH 上、或用户自定义路径），那只有服务端知道：
 * MinerU 走 `/mineru-check`（`schemas.resolve_mineru()` 六级优先级），
 * Ollama 走 `/health`（里面带 `ollama` 字段，同样是 `resolve_ollama()`）。
 * 服务没起来就按"本地看到什么算什么"处理。
 *
 * ## 为什么不在这里直接弹 Tkinter 向导
 *
 * 插件沙箱里起不了窗口，而且安装要实时日志、要能取消 —— 那些都在面板里
 * （`tools/panels/mineru_guide.py`、`tools/panels/ollama_guide.py`）。
 * 这里只负责"拉面板并把对应窗口打开"，用与「打开知识库」同一条
 * `Subprocess` + `nsIProcess` 兜底链（`panelProcess`）。
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
   * ② 让本地服务有机会起来（`/health` 那次探测在 startup 里）。
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
    // ① MinerU：先看本地文件，再问服务
    const kit = self.mineruKitPath();
    if (!(kit && self._exists && self._exists(kit))) {
      let seen = false;
      try {
        const info = await self.request("GET", "/mineru-check");
        seen = !!(info && info.ok);
      } catch (e) {
        Zotero.debug("[zotero-kb] /mineru-check 不可用（服务没起来？）：" + e);
      }
      if (!seen) missing.push("mineru");
    }
    // ② Ollama：先看本地文件，再问 /health
    const ol = self.ollamaExePath();
    if (!(ol && self._exists && self._exists(ol))) {
      let seen = false;
      try {
        const info = await self.request("GET", "/health");
        seen = !!(info && info.ollama);
      } catch (e) {
        Zotero.debug("[zotero-kb] /health 不可用（服务没起来？）：" + e);
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
   * 返回 0 =「打开安装引导」，1 =「以后再说」。
   *
   * 为什么用 `Services.prompt.confirmEx` 而不是自绘窗口：它是**同步**的、
   * 两个按钮的文案都能自定义，而且不需要额外 xhtml。
   */
  askOptionalGuide: function (missing) {
    const names = (missing || []).map(function (m) {
      return m === "mineru" ? "MinerU（让 PDF 解析更准，公式变 LaTeX）"
                            : "Ollama（本地模型，分类/摘要可离线跑）";
    });
    const body = "知识库有两个**可选**组件还没装：\n\n"
      + names.map(function (n) { return "  · " + n; }).join("\n")
      + "\n\n不装完全不影响现有功能：\n"
      + "  · 不装 MinerU：正文仍用 Zotero 缓存 + PyMuPDF 解析；\n"
      + "  · 不装 Ollama：在面板「运行环境 → 模型接入」里改成用 API（DeepSeek 等）即可。\n\n"
      + "面板里那个安装引导会：说明代价 → 体检磁盘/网络 → 后台安装（可取消）→ 自动填好路径。\n\n"
      + "要不要现在打开安装引导？";
    try {
      const ps = Services.prompt;
      const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
        + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING;
      const win = (Zotero.getMainWindow && Zotero.getMainWindow()) || null;
      return ps.confirmEx(win, "知识库：可选组件（不装也能用）", body, flags,
                          "打开安装引导", "以后再说", null, null, {});
    } catch (e) {
      Zotero.debug("[zotero-kb] 弹可选组件引导失败：" + e);
      return 1;
    }
  },


  /** 拉面板并打开对应组件的安装引导窗口。`which` = "mineru" | "ollama"。 */
  openOptionalGuide: function (which) {
    var self = ZoteroKB;
    const flag = which === "ollama" ? "--ollama-guide" : "--mineru-guide";
    const ok = self.panelProcess
      ? self.panelProcess(["--tab", "struct", flag])
      : false;
    if (!ok) {
      self.notify("打不开安装引导",
        "没能拉起管理面板。可以自己打开面板 →「知识库结构」页 →"
        + "「MinerU 安装引导」/「Ollama 安装引导」，"
        + "或者双击项目里的 scripts\\install-mineru.cmd。",
        null, true);
    }
    return ok;
  },
});
