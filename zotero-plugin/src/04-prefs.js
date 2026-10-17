/**
 * 04-prefs.js —— 首选项：默认值、读取、变更监听
 *
 * ## 配置的权威来源（2026-10-17 定稿，别再开第二份）
 *
 *   · **模型配置**：`<知识库>\llm-config.json`（`offline/judge.py` 读写，管理面板
 *     「运行环境」页编辑）。插件里的 `zotero-kb.model / provider / apiModel /
 *     apiKey / apiBaseUrl` 五个 pref 已**退役**：启动时一次性搬进 llm-config.json
 *     （见 migrateLlmPrefs），搬完就清掉。插件自己（分类建议、右键菜单状态）也读
 *     这个文件，不再读 pref —— 否则"面板里改了、插件还是旧的"。
 *   · **运行环境**：项目的 `kb-location.json`（宿主半的 `POST /env-save` 直接写，
 *     面板「运行环境」页编辑）。插件起引擎时读的 `projectRoot / pythonExe /
 *     ollamaExe` 三个 pref 只是它的**写穿镜像**（见 23-admin.js::KB_ADMIN_ENV_PREFS），
 *     不是设置面。老用户 profile 里的这三个 pref 会一次性补进 kb-location.json
 *     （见 migratePathPrefs），免得出现"插件用 pref、Python 用自动探测"的分叉。
 *   · **插件行为开关**（自动处理 / 分类列表 / 侧栏显示 / 同步 / 任务通道）仍然
 *     是 Zotero pref —— 它们在 Zotero 进程内直接生效，没有第二个副本。管理面板
 *     的「偏好设置」页通过宿主路由 `GET|POST /plugin-prefs` 读写它们。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  registerPrefs: function () {
    // 默认值可以在这里设，也可以走 prefs.js 前缀注册。
    // 这里用 Zotero.Prefs.set 兜底（只在未设置时写），避免依赖 defaults 文件。
    const defaults = {};
    defaults[this.PREFS.autoProcess] = true;
    defaults[this.PREFS.categories] = "";   // 空=用知识库现有的
    defaults[this.PREFS.acquireConfirm] = false;
    defaults[this.PREFS.acquireAutoClassify] = true;
    // MinerU 首次安装引导是否已经问过（问过就不再弹，见 19-mineruguide.js）
    defaults[this.PREFS.mineruGuideDone] = false;
    defaults[this.PREFS.optionalGuideDone] = false;
    // chatQuitWarn / chatNumCtx 已废弃：内容窗格聊天整块下线了，不要再加回来。
    for (const [key, value] of Object.entries(defaults)) {
      try {
        if (Zotero.Prefs.get(key) === undefined) {
          Zotero.Prefs.set(key, value);
        }
      } catch (e) { /* 个别键可能未被允许，忽略 */ }
    }
    // 老配置的迁移（幂等；拿不到知识库目录时下次启动再试）
    try { this.migrateLlmPrefs(); } catch (e) { /* 迁移失败不挡启动 */ }
    try { this.migratePathPrefs(); } catch (e) { /* 同上 */ }
  },


  getPref: function (key, fallback) {
    try {
      const v = Zotero.Prefs.get(key);
      return (v === undefined || v === null || v === "") ? fallback : v;
    } catch (e) {
      return fallback;
    }
  },


  // ================================================================ 模型配置

  /** `<知识库>\llm-config.json` 的路径（拿不到知识库目录时返回空串）。 */
  llmConfigPath: function () {
    var self = ZoteroKB;
    try {
      const dir = String(self.kbDir() || "").replace(/[\\/]+$/, "");
      return dir ? (dir + "\\llm-config.json") : "";
    } catch (e) {
      return "";
    }
  },


  /**
   * 读模型配置（**同步**，每次现读 —— 面板改完立即生效，不用重启）。
   *
   * 返回 `{provider, model, base_url, api_key, ollama_host}`；文件不存在 / 损坏
   * 都返回 `{}`（调用方按"没配"处理，不要抛）。
   */
  llmConfig: function () {
    var self = ZoteroKB;
    const p = self.llmConfigPath();
    if (!p) return {};
    try {
      const f = Zotero.File.pathToFile(p);
      if (!f.exists()) return {};
      const data = JSON.parse(String(Zotero.File.getContents(f) || "{}"));
      return (data && typeof data === "object" && !Array.isArray(data)) ? data : {};
    } catch (e) {
      Zotero.debug("[zotero-kb] 读 llm-config.json 失败（当作没配）：" + e);
      return {};
    }
  },


  /**
   * 把老版本的模型 pref 一次性搬进 `llm-config.json`，成功后清掉 pref。
   *
   * 为什么必须搬：老用户的 `zotero-kb.model` 是**唯一**记着他选了哪个模型的地方；
   * 新面板只认 llm-config.json。不搬的话升级后模型选择会静默变回"自动挑"。
   * 为什么搬完要清：两个地方都存 = 必然会漂移（本项目吃过太多次）。清之前先确认
   * 文件写成功；写不进去（知识库目录还不存在等）就保留 pref，下次启动再试。
   */
  migrateLlmPrefs: function () {
    var self = ZoteroKB;
    const get = (k) => {
      try { return String(Zotero.Prefs.get("zotero-kb." + k) || "").trim(); }
      catch (e) { return ""; }
    };
    const old = { provider: get("provider"), model: get("model"),
                  apiModel: get("apiModel"), apiKey: get("apiKey"),
                  apiBaseUrl: get("apiBaseUrl") };
    const anyOld = !!(old.provider || old.model || old.apiModel
                      || old.apiKey || old.apiBaseUrl);
    const p = self.llmConfigPath();
    if (!p) return false;                     // 知识库目录还不知道 → 下次再试
    const cur = self.llmConfig();
    // 新用户（没有旧 pref、也没有配置文件）：不凭空造一个文件
    if (!anyOld && !Object.keys(cur).length) return false;
    const useApi = String(cur.provider || old.provider || "ollama") === "openai";
    const out = {
      provider: String(cur.provider || old.provider || "ollama"),
      model: String(cur.model || (useApi ? old.apiModel : old.model) || ""),
      base_url: String(cur.base_url || (useApi ? old.apiBaseUrl : "") || ""),
      api_key: String(cur.api_key || (useApi ? old.apiKey : "") || ""),
      ollama_host: String(cur.ollama_host || ""),
    };
    const changed = ["provider", "model", "base_url", "api_key", "ollama_host"]
      .some((k) => String(cur[k] == null ? "" : cur[k]) !== out[k]);
    if (changed) {
      const err = self.kbAdminWriteJsonFile
        ? self.kbAdminWriteJsonFile(p, out)
        : "写配置的宿主函数不可用";
      if (err) {
        Zotero.debug("[zotero-kb] 迁移模型配置失败（保留旧 pref）：" + err);
        return false;
      }
    }
    if (!anyOld) return !!changed;
    // 写成功（或没有需要写的）→ 清掉退役的 pref
    for (const k of ["model", "provider", "apiModel", "apiKey", "apiBaseUrl"]) {
      try { Zotero.Prefs.clear("zotero-kb." + k); }
      catch (e) {
        try { Zotero.Prefs.set("zotero-kb." + k, ""); } catch (e2) { /* ignore */ }
      }
    }
    Zotero.debug("[zotero-kb] 模型 pref 已迁移到 " + p);
    return true;
  },


  // ================================================================ 运行环境

  /**
   * 把老用户 profile 里的三个路径 pref 一次性补进 `kb-location.json`。
   *
   * 三个 pref（projectRoot / pythonExe / ollamaExe）是老设置页的输入框写的；
   * 现在**权威来源**是 kb-location.json，pref 只是写穿镜像。老 profile 里可能只有
   * pref、没有 kb-location.json 的对应键 —— 那样插件起引擎用 pref，而 Python 那边
   * 按自动探测/别的档位算，两个进程对"项目在哪"可能给出不同答案。
   *
   * 只补**文件里还没有的**键，绝不覆盖（面板里明确设过的值优先）。
   */
  migratePathPrefs: function () {
    var self = ZoteroKB;
    const root = (() => {
      try { return String(Zotero.Prefs.get("zotero-kb.projectRoot") || "").trim(); }
      catch (e) { return ""; }
    })();
    if (!root || !self._looksLikeProjectRoot(root)) return false;
    const file = root.replace(/[\\/]+$/, "") + "\\kb-location.json";
    const cfg = self.kbAdminReadJsonFile ? self.kbAdminReadJsonFile(file) : {};
    const env = (cfg.env && typeof cfg.env === "object" && !Array.isArray(cfg.env))
      ? Object.assign({}, cfg.env) : {};
    const pairs = [
      ["project_root", "project_root", root],
      ["python", "python_exe",
       (() => { try { return String(Zotero.Prefs.get("zotero-kb.pythonExe") || "").trim(); } catch (e) { return ""; } })()],
      ["ollama", "ollama_exe",
       (() => { try { return String(Zotero.Prefs.get("zotero-kb.ollamaExe") || "").trim(); } catch (e) { return ""; } })()],
    ];
    let changed = false;
    for (const [key, flat, val] of pairs) {
      if (!val) continue;
      if (cfg[flat] || env[key]) continue;     // 已有值 → 不动
      cfg[flat] = val;
      env[key] = val;
      changed = true;
    }
    if (!changed) return false;
    cfg.env = env;
    const err = self.kbAdminWriteJsonFile
      ? self.kbAdminWriteJsonFile(file, cfg)
      : "写配置的宿主函数不可用";
    if (err) {
      Zotero.debug("[zotero-kb] 迁移路径 pref 失败：" + err);
      return false;
    }
    Zotero.debug("[zotero-kb] 路径 pref 已补进 " + file);
    return true;
  },

});
