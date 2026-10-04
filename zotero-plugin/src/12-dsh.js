/**
 * 12-dsh.js —— 发到 DSH：文件信箱通道（不走 HTTP，见 ARCHITECTURE.md）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 发到 DSH

  /** 桥接信箱目录（与 DSH 侧插件约定，见 ARCHITECTURE.md）。 */
  bridgeDir: function () {
    return this.bridgeDirPath();
  },


  /**
   * 给 DSH 侧收件箱投一个请求文件，等它写回结果。
   *
   * 为什么用文件而不是 HTTP：DSH desktop profile 里**没有 webServer 服务**
   * （插件会卡在 pending），而它的 /api 是"受信任+已认证"专用通道
   * （本机外部程序一律 401）。详见 ARCHITECTURE.md 第三、四节。
   *
   * 原子写：先写 .json.tmp 再改名 —— DSH 侧轮询时不会读到半截文件。
   */
  bridgeRequest: async function (req, waitMs) {
    var self = ZoteroKB;
    const root = self.bridgeDir();
    const id = "z" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    req.id = id;
    const reqDir = root + "\\requests";
    const resDir = root + "\\results";
    const tmp = reqDir + "\\" + id + ".json.tmp";
    const dst = reqDir + "\\" + id + ".json";
    const resFile = resDir + "\\" + id + ".json";

    try {
      // 目录不存在说明 DSH 侧插件没装载 —— 直接给出可操作的提示
      if (!(await IOUtils.exists(reqDir))) {
        return { ok: false, error: "收件箱目录不存在（DSH 侧插件未装载）",
                 hint: "请确认 dsh-bundle-zotero-bridge 已装并重启过 DSH" };
      }
      await IOUtils.writeUTF8(tmp, JSON.stringify(req, null, 1));
      await IOUtils.move(tmp, dst, { noOverwrite: false });
    } catch (e) {
      return { ok: false, error: "投递失败：" + e };
    }

    const deadline = Date.now() + (waitMs || 60000);
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 500));
      try {
        if (await IOUtils.exists(resFile)) {
          const text = await IOUtils.readUTF8(resFile);
          try { return JSON.parse(text); }
          catch (e) { /* 还没写完，下轮再读 */ }
        }
      } catch (e) { /* 忽略，继续等 */ }
    }
    return { ok: false, error: "等待 DSH 回复超时（" + ((waitMs || 60000) / 1000) + "s）",
             hint: "检查 DSH 是否在运行、插件是否已装载" };
  },


  /** 拉取 DSH 的对话列表（只读）。 */
  listDSHSessions: async function () {
    var self = ZoteroKB;
    try {
      const res = await self.bridgeRequest({ kind: "list-sessions" }, 20000);
      return (res && res.ok && res.sessions) ? res.sessions : [];
    } catch (e) {
      return [];
    }
  },


  /**
   * 把选中的文献发到 DSH 对话。
   *
   * ⚠ 只发"解析后的存放路径"，不发内容本身 ——
   * 知识库已把文献解析好放在 `kb\papers\<key>.md`（摘要/笔记）与
   * `kb\fulltext\<key>.md`（按页正文），DSH 有 read 工具能直接读文件。
   * 把正文塞进消息只是白烧 token、占满上下文。
   * 轻量信息（权重/分类）仍带上：它们是"知识库的结论"，不值几个 token。
   */
  sendToDSH: async function (items, opts) {
    var self = ZoteroKB;
    opts = opts || {};
    // 前置校验：不要把明显无效的请求发出去白等 2 分钟
    if (!opts.create && !String(opts.sessionId || "").trim()) {
      self.notify("发送失败", "没有指定对话（请从列表里选一个，或选「新建对话…」）",
                  null, true);
      return;
    }
    if (!items || !items.length) {
      self.notify("发送失败", "没有选中文献", null, true);
      return;
    }
    try {
      self.notify("正在整理文献路径…", items.length + " 篇", null, false);

      const keys = items.map((it) => it.key);
      let info = {};
      // 知识库根路径从本地服务取，而不是写死 —— 这样知识库以后
      // 迁到别处（比如 Zotero 数据目录）这里不用改。
      let kbDir = self.kbDir();      // 缓存值兜底，下面再用 /health 校正
      try {
        const r = await self.request("POST", "/item-info", { keys });
        for (const x of (r && r.items) || []) info[x.key] = x;
        const st = await self.request("GET", "/health");
        if (st && st.kb_dir) kbDir = String(st.kb_dir).replace(/[\\/]+$/, "");
      } catch (e) {
        Zotero.debug("[zotero-kb] 取知识库信息失败（用默认路径）：" + e);
      }

      // ⚠ 只发**解析后的存放路径**，不发内容本身。
      // 知识库已把文献解析好放在 papers\<key>.md（摘要/笔记/元数据）与
      // fulltext\<key>.md（按页正文）；DSH 有 read 工具能直接读文件。
      // 把正文塞进消息只是白烧 token、占满上下文。
      const L = [];
      L.push("【Zotero 文献】已按知识库路径发来，共 " + items.length + " 篇");
      L.push("知识库根目录：" + kbDir);
      L.push("");
      for (const it of items) {
        const k = it.key;
        const meta = self.buildMeta(it);
        const kb = info[k] || {};
        L.push("- " + (meta.title || "(无标题)")
          + (meta.year ? "（" + meta.year + "）" : "") + "  [" + k + "]");
        if (kb.in_kb) {
          const bits = [];
          if (kb.weight && Math.abs(kb.weight - 1) > 0.005) {
            bits.push("权重 " + kb.weight + "（被使用经验加权过）");
          }
          if (kb.collections && kb.collections.length) {
            bits.push("分类 " + kb.collections.join("、"));
          }
          if (kb.tags && kb.tags.length) {
            bits.push("标签 " + kb.tags.slice(0, 6).join("、"));
          }
          if (bits.length) L.push("    知识库：" + bits.join("｜"));
          L.push("    摘要与笔记：" + kbDir + "\\papers\\" + k + ".md");
          if (kb.fulltext_chars > 0) {
            L.push("    按页正文：" + kbDir + "\\fulltext\\" + k + ".md"
              + "（" + kb.fulltext_chars + " 字符）");
          }
        } else {
          L.push("    （这篇还没进知识库，只有 Zotero 元数据）");
        }
      }
      L.push("");
      L.push("---");
      L.push("请按上面的路径读取（要细节读「按页正文」，要概览读「摘要与笔记」）；"
        + "也可直接用 kb_search / kb_item / kb_fulltext 工具按 key 取。"
        + "读完后结合这些文献回答我接下来的问题。");

      const text = L.join("\n");

      // 用 newProgress/closeProgress 统一管理 —— 新提示出现时旧的自动关掉，
      // 屏幕上永远只有一条（否则"整理路径→已开始发送→已发送"会叠三层）。
      // 结果提示不自动消失（不调 startCloseTimer），由用户点击关闭。
      try {
        self.newProgress("正在发送到 DSH…",
          items.length + " 篇文献（只发路径）· "
          + (opts.create ? "新建对话" : "指定对话")
          + "\n等待 DSH 确认，请稍候…");
      } catch (e) { /* ignore */ }

      const res = await self.bridgeRequest(
        opts.create
          ? { kind: "create-and-send",
              title: "Zotero 文献：" + String(items[0].getField("title") || "").slice(0, 28),
              text }
          : { kind: "send", sessionId: opts.sessionId, text },
        120000);

      try {
        if (res && res.ok) {
          // 成功：不自动关闭，留给你看清楚；点一下窗口即关闭（Zotero 进度窗
          // 没有 X 按钮，closeOnClick 就是它的关闭方式，所以文案里写明）
          const pw = self.newProgress("✅ 已发送到 DSH",
            items.length + " 篇 · " + text.length + " 字符（只发路径）\n"
            + (res.sessionId ? "对话 " + String(res.sessionId).slice(-12) : "")
            + "\n\n去 DSH 里打开对应对话即可。\n"
            + "（点击本窗口任意处关闭）");
          // 兜底：不点也自己消失，避免一直留在屏幕上
          try { pw.startCloseTimer(60000); } catch (e) { /* ignore */ }
        } else {
          const why = String((res && (res.error
            || JSON.stringify(res.notes || res.tries))) || "未知错误").slice(0, 300);
          const pw = self.newProgress("❌ 发送失败", why
            + "\n\n（点击本窗口任意处关闭）");
          try { pw.startCloseTimer(60000); } catch (e) { /* ignore */ }
        }
      } catch (e) { /* 进度窗失败不影响主流程 */ }

      if (res && res.ok) {
        Zotero.debug("[zotero-kb] 已发送到 DSH：" + res.sessionId);
      } else {
        Zotero.logError(new Error("[zotero-kb] sendToDSH 失败："
          + JSON.stringify(res).slice(0, 400)));
      }
    } catch (e) {
      Zotero.logError(e);
      self.notify("发送出错", String(e), null, true);
    }
  },
});
