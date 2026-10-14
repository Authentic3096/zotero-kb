/**
 * 02-paths.js —— 路径解析：知识库、项目根、Python、Ollama、桥接信箱都在哪
 * 一律**不写死**：能问服务的就问服务，能探测的就探测
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {
  // ================================================================ 路径解析

  /**
   * 知识库目录：优先用服务端告诉我们的（/health 里带 kb_dir），
   * 其次用上次缓存到 pref 的值，最后才用默认值。
   *
   * 为什么要缓存：插件在服务没起来时也要能工作（比如写启动日志），
   * 那时拿不到 /health，就用上次记住的。
   *
   * 这样别人装的时候**不用改插件源码** —— 服务端在哪，插件就知道在哪。
   */
  kbDir: function () {
    var self = ZoteroKB;
    try {
      const cached = Zotero.Prefs.get("zotero-kb.kbDir");
      if (cached && String(cached).trim()) return String(cached).trim();
    } catch (e) { /* ignore */ }
    // ⚠ 这里**故意不写死绝对路径**（老版本写的是开发机的
    //   D:\DSHplugins\zotero-kb\kb，对别人毫无意义，还会误导排错）。
    //   知识库位置只有两个正当来源：服务端 /health 的 kb_dir，
    //   或用户在设置面板里填的。都没有时返回空，让上层报
    //   "服务没连上，请先在设置里填位置" —— 比指到一个不存在的目录好。
    return "";
  },


  /**
   * 记住服务端告诉我们的运行环境（healthCheck 时调用）。
   *
   * ⚠ 存到**独立于用户设置**的 pref 键里：
   *   `zotero-kb.projectRoot`     = 用户在设置面板里填的（权威）
   *   `zotero-kb.serverProjectRoot` = 服务端报的（兜底）
   *   老版本把两者写进同一个键，结果是"服务端一报就把用户填的覆盖了"。
   */
  rememberServerInfo: function (info) {
    if (!info) return;
    const map = [["serverProjectRoot", info.project_root],
                 ["serverKbDir", info.kb_dir],
                 ["serverPython", info.python],
                 ["serverOllama", info.ollama]];
    for (const [pref, val] of map) {
      if (!val) continue;
      try {
        const key = "zotero-kb." + pref;
        if (Zotero.Prefs.get(key) !== val) {
          Zotero.Prefs.set(key, String(val));
        }
      } catch (e) { /* ignore */ }
    }
    if (info.kb_dir) this.rememberKbDir(info.kb_dir);
  },


  /**
   * 弹「选择文件夹」对话框。返回 Promise<string>（取消则空串）。
   *
   * 为什么不用 Zotero.FilePicker：它是 Zotero 内部模块（filePicker.mjs），
   * 没有挂在 Zotero.* 命名空间下，从插件沙箱里拿不到稳定引用。
   * 直接建 nsIFilePicker 更可靠 —— Zotero 自己的 FilePicker 内部就是
   * `Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker)`。
   * 注意沙箱里没有 `Ci`，要用完整的 Components.interfaces。
   */
  pickFolder: function (title) {
    var self = ZoteroKB;
    return new Promise(function (resolve) {
      try {
        const fp = Components.classes["@mozilla.org/filepicker;1"]
          .createInstance(Components.interfaces.nsIFilePicker);
        // ⚠ 第一个参数要的是 **BrowsingContext**，不是 window。
        //   Zotero 自己的 filePicker.mjs 也是这么传的：
        //     this._fp.init(parentWindow.browsingContext, title, mode)
        //   沙箱里没有 `Ci`，要用完整的 Components.interfaces。
        let bc = null;
        try {
          const win = Zotero.getMainWindow && Zotero.getMainWindow();
          bc = (win && win.browsingContext) || null;
        } catch (e) { /* 拿不到就传 null，picker 会自己找父窗口 */ }
        fp.init(bc, title || "选择文件夹",
                Components.interfaces.nsIFilePicker.modeGetFolder);
        fp.open(function (rv) {
          try {
            if (rv !== Components.interfaces.nsIFilePicker.returnOK || !fp.file) {
              resolve("");
              return;
            }
            resolve(String(fp.file.path || ""));
          } catch (e) {
            resolve("");
          }
        });
      } catch (e) {
        Zotero.debug("[zotero-kb] 选文件夹失败：" + e);
        resolve("");
      }
    });
  },


  /** 弹「选择文件」对话框（选 python.exe / ollama.exe 用）。 */
  pickFile: function (title, filterTitle, filterExt) {
    return new Promise(function (resolve) {
      try {
        const fp = Components.classes["@mozilla.org/filepicker;1"]
          .createInstance(Components.interfaces.nsIFilePicker);
        let bc = null;
        try {
          const win = Zotero.getMainWindow && Zotero.getMainWindow();
          bc = (win && win.browsingContext) || null;
        } catch (e) { /* ignore */ }
        fp.init(bc, title || "选择文件",
                Components.interfaces.nsIFilePicker.modeOpen);
        if (filterExt) {
          fp.appendFilter(filterTitle || "程序", filterExt);
        }
        fp.open(function (rv) {
          try {
            if (rv !== Components.interfaces.nsIFilePicker.returnOK || !fp.file) {
              resolve("");
              return;
            }
            resolve(String(fp.file.path || ""));
          } catch (e) {
            resolve("");
          }
        });
      } catch (e) {
        Zotero.debug("[zotero-kb] 选文件失败：" + e);
        resolve("");
      }
    });
  },


  /**
   * 把运行环境配置推给服务端，让它落盘到 <项目>\kb-location.json。
   *
   * 为什么必须推：Zotero 的 pref 只有插件自己看得到，而**管理面板是独立的
   * Python 进程**，它得知道项目在哪、Python 在哪。服务端落盘后
   * schemas.resolve_* 就能读到，面板启动时自然用对。
   *
   * ⚠ **W3 线2.5（2026-10-13）：这一处故意没迁到引擎桥，因为入参/回包形状都对不上**
   *   （真机取证见开发说明）：
   *     · 老 HTTP `POST /env-config` 收的是**嵌套** `{env:{project_root,…}}`，
   *       回包带 **`report`**；
   *     · 引擎 `env.save` 收的是**扁平**键（`project_root/python/ollama/mineru/figures`），
   *       回包是 `{saved, check}`、**没有 `report`**。
   *   实测：把插件现在这个 `{env:{…}}` 体直接过桥 → 引擎回
   *   `没有要写入的键（project_root / python / ollama / mineru / figures）`。
   *   要迁得先做两件事：① 这里把 `env` **摊平**再发；② 让 `env.save` 的返回里带上
   *   `report`（或让设置页接受"另发一次 env-check"）。②动契约，所以排在收工后做。
   *
   * 2026-10-14：两件事都已在引擎侧补齐（`env.save` 回包带 **`report`**、
   * 新增 **`env.checkRaw`**），所以这里改成**桥优先 → 退回老 HTTP**。
   * **入参必须把 `env` 摊平一层**（老 HTTP 收嵌套 `{env:{…}}`、引擎收扁平
   * `{project_root,…}`）—— 照抄老写法过桥会当场报「没有要写入的键」，
   * 这是本处最容易写错的地方（上一轮实测踩过）。
   */
  kbPushEnvConfig: async function (env) {
    var self = ZoteroKB;
    let r = null;
    try {
      // ⚠ 摊平：`Object.assign({}, env || {})` —— 不是 `{ env: env }`
      r = await self.kbAdminHostRoute("POST", "/env-config",
                                      Object.assign({}, env || {}));
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥写环境配置失败（退回 HTTP）：" + e);
    }
    if (r && r.saved === true) return { ok: true, report: r.report || null };
    try {
      const r2 = await self.request("POST", "/env-config", { env: env });
      return { ok: true, report: (r2 && r2.report) || null };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },


  /**
   * 从服务端取运行环境诊断（设置面板「检测」按钮用）。
   *
   * ⚠ **W3 线2.5（2026-10-13）：这一处也故意没迁** —— 两条通道回的**不是同一个形状**：
   *   · 老 HTTP `GET /env-check` = `env_report()` 原文，含 **`python` / `project_root` /
   *     `ollama` / `runtime_state` / `user_location_file`**；`settings.js` 的
   *     `renderReport()` 读的正是 `r.python.runnable` 与 `r.project_root.has_panel`。
   *   · 引擎 `env.check` 是给**管理面板**重新整形过的：`{ok, items[], sources,
   *     rejected[], env_file, location_file}` —— **没有 `python` / `project_root`
   *     这两个顶层键**（实测 `bridgeHas_consumerFields: false`）。
   *   直接改过去会让设置面板的「检测」报告变空/报错。要迁得先让引擎**多回一份原文**
   *   （新增 `env.checkRaw`，或给 `env.check` 加一个 `raw` 字段）—— 那是扩契约，
   *   按纪律排在计费切换点之后做。
   *
   * 2026-10-14：引擎已新增 **`env.checkRaw`**，所以这里改成**桥优先 → 退回老 HTTP** ——
   * 两条通道回的是**同一个形状**（都是 `env_report()` 原文）。
   * ⚠ 别图省事改成 `env.check`：那个给管理面板用、**没有** `python`/`project_root` 两个顶层键。
   */
  kbEnvReport: async function () {
    var self = ZoteroKB;
    try {
      const r = await self.kbAdminHostRoute("GET", "/env-check-raw");
      if (r && (r.python || r.project_root)) return r;   // 原文形状的自检
      Zotero.debug("[zotero-kb] 引擎桥的诊断原文形状不对（退回 HTTP）");
    } catch (e) {
      Zotero.debug("[zotero-kb] 引擎桥取诊断失败（退回 HTTP）：" + e);
    }
    try {
      return await self.request("GET", "/env-check");
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },


  /** 记住服务端给的知识库位置（healthCheck 时调用）。 */
  rememberKbDir: function (dir) {
    if (!dir) return;
    try {
      const cur = Zotero.Prefs.get("zotero-kb.kbDir");
      if (cur !== dir) {
        Zotero.Prefs.set("zotero-kb.kbDir", String(dir));
        Zotero.debug("[zotero-kb] 知识库位置已记住：" + dir);
      }
    } catch (e) { /* ignore */ }
  },


  /**
   * @deprecated 保留仅为兼容旧调用点。
   * 服务端报的项目根**不再**写进用户设置键（那样会覆盖用户填的值），
   * 现在存到 `serverProjectRoot`，见 rememberServerInfo()。
   */
  rememberProjectRoot: function () {
    // 故意不写任何东西：写用户键会让「用户在设置里填的」被服务端的值顶掉。
  },


  /**
   * 项目根目录（放 .venv / offline / online 的地方）。
   *
   * ⚠ **不能从 kbDir 反推**。知识库默认跟着 Zotero 数据目录走，而代码/venv
   *   还在项目目录里 —— 本机就是反推了一把，结果算出
   *   `D:\Application\ZoteroData\Zotero`，那里没有 .venv，
   *   于是工具栏按钮报"找不到 Python 环境"。
   *   结论：数据位置和代码位置是两个独立的东西，谁也别推谁。
   *
   * 顺序：用户设置 > 服务端 /health > 向上探测标记 > 环境变量 > 空。
   */
  /** 同步判断路径存在（插件里到处能用，不依赖 async）。 */
  _exists: function (p) {
    try {
      return !!p && Zotero.File.pathToFile(p).exists();
    } catch (e) {
      return false;
    }
  },


  /**
   * 这个目录像不像"项目根"（放 .venv / offline / online 的那个）。
   *
   * ⚠ W4（2026-10-14）：判据原来是 `tools\gui.py` 在不在 —— Tk 面板退役后那个文件
   * **已删**，于是四个探测档全部落空、`projectRoot()` 回空串，现象是引擎起不来：
   * 任何走引擎桥的操作都报 `Error: 找不到引擎或 Python`（真机实测，关闭面板窗口后复现）。
   * 现在改成**仍然存在且在语义上更贴切**的标记：`online\engine.py` + `offline\schemas.py`
   * 两个都在才算项目根（只认一个容易误判别人的目录）。
   */
  _looksLikeProjectRoot: function (dir) {
    const d = String(dir || "").trim().replace(/[\\/]+$/, "");
    if (!d) return false;
    return this._exists(d + "\\online\\engine.py")
        && this._exists(d + "\\offline\\schemas.py");
  },

  projectRoot: function () {
    // 1) 用户在设置面板里指定的（唯一权威来源）
    try {
      const v = Zotero.Prefs.get("zotero-kb.projectRoot");
      if (v && this._looksLikeProjectRoot(String(v))) {
        return String(v).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* ignore */ }
    // 2) 服务端上次告诉我们的
    try {
      const v = Zotero.Prefs.get("zotero-kb.serverProjectRoot");
      if (v && this._looksLikeProjectRoot(String(v))) {
        return String(v).trim().replace(/[\\/]+$/, "");
      }
    } catch (e) { /* ignore */ }
    // 3) 环境变量
    try {
      const v = Services.env.get("ZOTERO_KB_ROOT");
      if (v && this._looksLikeProjectRoot(v)) return v;
    } catch (e) { /* ignore */ }
    // 4) 从知识库位置向上找（老布局 <项目>\kb\ 能命中）。
    //    这是探测，不是反推：**验证到才用**。
    try {
      let d = this.kbDir().replace(/[\\/]+$/, "");
      for (let i = 0; i < 4 && d; i++) {
        if (this._looksLikeProjectRoot(d)) return d;
        const up = d.replace(/[\\/][^\\/]+$/, "");
        if (!up || up === d) break;
        d = up;
      }
    } catch (e) { /* ignore */ }
    return "";   // 找不到就返回空，让调用方报"该去哪设置"，不要瞎猜一个
  },


  /** 项目里的 Python 解释器（优先 pythonw，无控制台窗口）。 */
  pythonExe: function () {
    // 0) 用户显式指定的最优先
    try {
      const v = Zotero.Prefs.get("zotero-kb.pythonExe");
      if (v && String(v).trim() && this._exists(String(v).trim())) {
        return String(v).trim();
      }
    } catch (e) { /* ignore */ }
    const root = this.projectRoot();
    const cands = ["\\.venv\\Scripts\\pythonw.exe",
                   "\\.venv\\Scripts\\python.exe"];
    for (const c of cands) {
      if (root && this._exists(root + c)) return root + c;
    }
    // 退回系统 Python：让"没建 venv"也能用（依赖缺了服务端会报清楚）
    try {
      const v = Services.env.get("ZOTERO_KB_PYTHON");
      if (v && this._exists(v)) return v;
    } catch (e) { /* ignore */ }
    return root ? root + cands[0] : "";   // 不存在也返回它，让上层报明确的错
  },


  /** Ollama 可执行文件位置：用户指定 > 标准安装位置。 */
  ollamaPaths: function () {
    // 用户指定优先（装在非默认位置时）
    try {
      const v = Zotero.Prefs.get("zotero-kb.ollamaExe");
      if (v && String(v).trim() && this._exists(String(v).trim())) {
        const p = String(v).trim();
        const dir = p.replace(/[\\/][^\\/]+$/, "");
        return { app: dir + "\\ollama app.exe", exe: p };
      }
    } catch (e) { /* ignore */ }
    let base = "";
    try {
      // 注意：沙箱里没有 `Ci` 这个简写（那是 Mozilla 模块内部的），
      // 要用完整的 Components.interfaces。
      const local = Services.dirsvc.get("LocalAppData",
                                        Components.interfaces.nsIFile).path;
      base = local + "\\Programs\\Ollama";
    } catch (e) {
      // 拿不到 LocalAppData 时才用这个系统级常量位置（不是用户目录的猜测）
      base = "C:\\ProgramData\\Ollama";
      try {
        const pf = Services.dirsvc.get("ProgramFiles",
                                       Components.interfaces.nsIFile).path;
        base = pf + "\\Ollama";
      } catch (e2) { /* ignore */ }
    }
    return { app: base + "\\ollama app.exe", exe: base + "\\ollama.exe" };
  },


  /** DSH 侧收件箱目录（默认在用户主目录下）。 */
  bridgeDirPath: function () {
    let home = "";
    try {
      home = Services.dirsvc.get("Home",
                                 Components.interfaces.nsIFile).path;
    } catch (e) {
      home = "";
    }
    if (!home) {
      // 兜底：Zotero 的 profile 目录一定可写，用它的上一级猜
      try {
        home = Services.dirsvc.get("ProfD",
                                   Components.interfaces.nsIFile).parent.path;
      } catch (e2) { home = "C:\\Users\\Public"; }
    }
    return home + "\\.dsh\\zotero-bridge";
  },


  ollamaFlagPath: function () {
    return this.kbDir() + "\\ollama-owned-by-zotero.flag";
  },


  /**
   * 本地服务（:8765）的归属标记。
   *
   * 和 Ollama 用的是同一套思路：**标记文件在 = 是我们拉起来的**，
   * 关 Zotero 时就顺手关掉；标记不在 = 本来就在跑（用户手动开的、
   * 或开机自启拉起的），绝不去碰它。
   */
  serverFlagPath: function () {
    return this.kbDir() + "\\server-owned-by-zotero.flag";
  },
});
