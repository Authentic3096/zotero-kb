/**
 * 19-caps.js —— 「能力探测」：DSH 连上了没、本地模型可用不可用。
 *
 * ## 为什么单独一个文件
 *
 * 右键菜单要按"当前有没有这个能力"改文案（用户 2026-10-05 的要求：
 * 「右键文献的菜单能实现不填死吗，dsh 没接到就不显示，本地模型（或者接外部 api）
 * 没读取到也不显示对应的两条」→ 后来拍板成**显示但标「未连接」**）。
 * 而"怎么判断有没有"这件事不该散在菜单代码里（菜单是同步构建的，
 * 探测是异步的、还会失败），所以：
 *
 *   · 本文件只负责**探测 + 缓存 + 给出文案**（纯函数 `capLabel` 便于桩测试）；
 *   · `14-menus.js` 只读缓存（`caps.dsh` / `caps.localModel`），不自己探测。
 *
 * ## 三种状态，不是两种
 *
 * `true` = 确认可用；`false` = 确认不可用；`null` = **还没探到 / 探不出来**。
 * 为什么要留 `null`：本机常年"Ollama 装了但没启动"、"DSH 装着但没开"，
 * 把"未知"当成"不可用"会让用户在最需要入口的时候看不到任何提示。
 * 所以文案分三档：正常 / （未连接）/ （检测中）。
 *
 * ## 探测的成本与"别乱丢文件"
 *
 * DSH 那条是真的往桥的 `requests/` 里投一个 `list-sessions` 请求（几千字节），
 * 所以：① 只在启动后与超过 TTL 时探；② **上次超时后的 5 分钟内不再探**
 * （否则 DSH 没开时会每 30 秒往那个目录里堆一个没人处理的请求文件）；
 * ③ 超时后**把自己的那个请求文件删掉**（只删自己刚写的那个，绝不碰别的）。
 */
Object.assign(ZoteroKB, {

  /** 能力缓存：`true` / `false` / `null`（未知）。菜单只读它。 */
  caps: { dsh: null, dshWhy: "", localModel: null, localWhy: "", at: 0,
          dshTimeoutAt: 0 },

  CAP_TTL_MS: 30000,            // 缓存有效期
  DSH_TIMEOUT_BACKOFF_MS: 300000,   // 上次探测超时后的冷却（5 分钟）


  /**
   * 探一次能力（异步，**不抛**）。`force=true` 时忽略 TTL。
   *
   * ⚠ 菜单是同步构建的，所以这里**绝不能**被菜单直接 await —— 菜单读 `caps`，
   *   这里在后台更新它（并在更新后不做任何界面重绘：下次弹出菜单自然是新的）。
   */
  refreshCaps: function (force) {
    var self = ZoteroKB;
    const now = Date.now();
    if (!force && self.caps.at && (now - self.caps.at) < self.CAP_TTL_MS) {
      return Promise.resolve(self.caps);
    }
    self.caps.at = now;
    return Promise.all([
      self.probeDsh(force).catch(function () { return null; }),
      Promise.resolve(self.probeLocalModel()),
    ]).then(function (r) {
      self.caps.dsh = r[0] === null ? self.caps.dsh : !!r[0];
      self.caps.localModel = r[1];
      return self.caps;
    });
  },


  /**
   * DSH 桥是否活着。返回 `true` / `false`（拿不到就是 false，并把原因写进 caps）。
   *
   * 判据分两步（先便宜后昂贵）：
   *   ① 桥目录 `<home>\.dsh\zotero-bridge\requests` 存在吗 —— 不存在说明
   *      DSH 侧插件从没装载过（一条 `IOUtils.exists` 就够，零成本）；
   *   ② 投一个 `list-sessions` 请求，等 3 秒 —— 有回复才叫"接上了"。
   */
  probeDsh: async function (force) {
    var self = ZoteroKB;
    const root = self.bridgeDir();
    const reqDir = root + "\\requests";
    const resDir = root + "\\results";
    if (!self.bridgeDirPath || !root) {
      self.caps.dshWhy = "拿不到桥目录（~/.dsh/zotero-bridge）";
      return false;
    }
    let exists = false;
    try {
      exists = await IOUtils.exists(reqDir);
    } catch (e) {
      exists = false;
    }
    if (!exists) {
      self.caps.dshWhy = "桥目录不存在（DSH 侧插件未装载）";
      return false;
    }
    // 上个探测刚超时过 → 冷却期内不再打扰那个目录
    if (!force && self.caps.dshTimeoutAt
        && (Date.now() - self.caps.dshTimeoutAt) < self.DSH_TIMEOUT_BACKOFF_MS) {
      self.caps.dshWhy = "上次探测超时（冷却中，请确认 DSH 在运行）";
      return false;
    }
    const id = "z" + Date.now().toString(36)
      + Math.random().toString(36).slice(2, 7);
    const dst = reqDir + "\\" + id + ".json";
    const resFile = resDir + "\\" + id + ".json";
    try {
      await IOUtils.writeUTF8(dst, JSON.stringify(
        { id: id, kind: "list-sessions", at: new Date().toISOString() }, null, 1));
    } catch (e) {
      self.caps.dshWhy = "投递请求失败：" + e;
      return false;
    }
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      await new Promise(function (r) { setTimeout(r, 250); });
      try {
        if (await IOUtils.exists(resFile)) {
          self.caps.dshWhy = "";
          self.caps.dshTimeoutAt = 0;
          return true;
        }
      } catch (e) { /* 下一轮再试 */ }
    }
    // 超时：把自己刚写的请求文件删掉（只删自己这个，别碰 DSH 的目录）
    self.caps.dshTimeoutAt = Date.now();
    self.caps.dshWhy = "投了请求但 3 秒没回复（DSH 没在运行？）";
    try {
      await IOUtils.remove(dst, { ignoreAbsent: true });
    } catch (e) { /* 删不掉也无妨 */ }
    return false;
  },


  /**
   * 本地模型可用不可用。返回 `true` / `false` / `null`（未知）。
   *
   * 判据用**已经拿到的**信息，不再发新请求：
   *   · 插件 pref 里选了 `openai` → 有 apiKey（和 baseUrl）就算可用；
   *   · 选了 `ollama` → 看 `serverInfo.ollama`（`/health` 报的：文件在不在 +
   *     API 通不通 + 有哪些模型）→ `api_up && models.length` 才算可用；
   *   · 拿不到 `/health` 的结果 → `null`（未知），界面上写"检测中"。
   */
  probeLocalModel: function () {
    var self = ZoteroKB;
    let provider = "";
    try {
      provider = String(self.getPref(self.PREFS.provider, "") || "").trim();
    } catch (e) { provider = ""; }
    if (!provider) provider = "ollama";
    if (provider === "openai") {
      const key = String(self.getPref(self.PREFS.apiKey, "") || "").trim();
      const base = String(self.getPref(self.PREFS.apiBaseUrl, "") || "").trim();
      self.caps.localWhy = key ? "" : "选了 API 但没填 API Key（运行环境→模型接入）";
      return !!key && !!base;
    }
    const info = (self.serverInfo && self.serverInfo.ollama) || null;
    if (!info) {
      self.caps.localWhy = "还没拿到本地服务状态（点面板「服务状态」看一眼）";
      return null;
    }
    if (!info.exists) {
      self.caps.localWhy = "没找到 Ollama（可选组件；也可改用 API 模型）";
      return false;
    }
    if (!info.api_up) {
      self.caps.localWhy = "Ollama 没在运行（面板「运行环境」页点「启动 Ollama」）";
      return false;
    }
    const models = info.models || [];
    if (!models.length) {
      self.caps.localWhy = "Ollama 在跑，但一个模型都没有（ollama pull qwen3:4b-instruct）";
      return false;
    }
    self.caps.localWhy = "";
    return true;
  },


  /**
   * 菜单文案（**纯函数**，桩环境直接测）：按能力状态给标题与提示。
   *
   * `kind` = "dsh" | "localModel"；返回 `{label, tooltip}`。
   * 三档：正常 → 原名；确认不可用 → 附「（未连接）」+ 怎么办；未知 → 「（检测中）」。
   */
  capLabel: function (kind) {
    var self = ZoteroKB;
    const st = (kind === "dsh") ? self.caps.dsh : self.caps.localModel;
    if (kind === "dsh") {
      // ⚠ 顶层标题**不带后缀**（用户 2026-10-05：「现在动态的加（未连接）
      //   导致太宽了，去掉吧」）。连接状态改用 `capStateLine()` 放进**下级菜单**。
      return { label: "发送到 DSH",
               tooltip: "把这篇的路径发到 DSH 里（新建对话或选已有对话）"
                 + (st === false ? ("\n\n⚠ 现在没连上：" + (self.caps.dshWhy || "")) : "") };
    }
    return { label: "连接到本地模型",
             tooltip: "用本地模型做「分类建议」和「补全元数据」"
               + (st === false
                  ? ("\n\n⚠ 现在不可用：" + (self.caps.localWhy || "")) : "") };
  },


  /**
   * 下级菜单里的**状态行**文案（纯函数，可桩测）。
   *
   * 为什么放这里而不是标题上：用户 2026-10-05 反馈"标题加（未连接）太宽"，
   * 要求"在悬浮的下级菜单显示链接没链接"。顺带解决另一个问题：菜单是同步构建的，
   * 缓存里可能是 `null`（还没探到）—— 那时**不写"检测中"**，而是据实说
   * "还没检查"（点了就是检查），免得出现"对话都列出来了却写检测中"的矛盾。
   *
   * `extra` 用来塞"这次真的看到的"信息（例如对话条数 / 模型名）。
   */
  capStateLine: function (kind, extra) {
    var self = ZoteroKB;
    const st = (kind === "dsh") ? self.caps.dsh : self.caps.localModel;
    const why = (kind === "dsh") ? self.caps.dshWhy : self.caps.localWhy;
    const tail = extra ? ("　" + extra) : "";
    if (st === true) {
      return { text: (kind === "dsh" ? "● 已连接 DSH" : "● 本地模型可用") + tail,
               ok: true };
    }
    if (st === false) {
      return { text: (kind === "dsh" ? "○ 未连接：" : "○ 不可用：")
                     + (why || "原因未知"), ok: false };
    }
    return { text: (kind === "dsh" ? "○ 还没检查过连接" : "○ 还没检查过本地模型")
                   + "（点这一行重新检查）", ok: false };
  },


  /** 排一次周期探测（用 setTimeout 链，不用 setInterval —— 沙箱里后者可能不触发）。 */
  scheduleCapsRefresh: function () {
    var self = ZoteroKB;
    const tick = function () {
      self.refreshCaps(false).catch(function () { /* 探测失败不是错误 */ });
      setTimeout(tick, 60000);
    };
    setTimeout(tick, 12000);      // 启动 12 秒后第一次（等 healthCheck 先跑完）
  },
});
