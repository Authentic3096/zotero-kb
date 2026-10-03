// 设置面板诊断 —— 在 Zotero 主窗口里运行
//
//   Zotero → 工具 → 开发者 → 运行 JavaScript
//   粘贴本文件全部内容 → Ctrl+R 运行 → 把输出的 [KB-DIAG] 行发回来
//
// 先打开 设置 → 文献知识库，再运行它。

(async () => {
  const log = (s) => { try { Zotero.debug("[KB-DIAG] " + s); } catch (e) {} };
  const out = [];
  const say = (s) => { out.push(s); };

  // ---------- 0. 磁盘上的包 vs 实际加载的版本（最关键的一项）----------
  //
  // ⚠ 为什么放最前面：Zotero **运行中替换 profile 里的 xpi 不会被重读**，
  //   所以"改了代码没生效"十有八九是"Zotero 还在跑旧代码"。
  //   把两个版本号并排打出来，一眼就能分辨是"代码没生效"还是"代码有问题"。
  say("=== 0. 版本对照（先看这个）===");
  let loadedVer = "(未知)";
  try {
    const { AddonManager } = ChromeUtils.importESModule(
      "resource://gre/modules/AddonManager.sys.mjs");
    const a = await AddonManager.getAddonByID("zotero-kb@authentic3096.github.io");
    loadedVer = a ? a.version : "(AddonManager 里没有这个插件)";
    say("Zotero 实际加载的版本: " + loadedVer);
    if (a) {
      say("  插件 active: " + a.isActive);
      say("  包路径: " + (a.getResourceURI ? a.getResourceURI("").spec : "?"));
    }
  } catch (e) { say("AddonManager 出错: " + e); }
  try {
    const KBv = Zotero.ZoteroKB;
    say("ZoteroKB.version（startup 参数）: "
        + JSON.stringify(KBv ? KBv.version : null));
  } catch (e) { /* ignore */ }

  // 读磁盘上那个 xpi 的 manifest，看它是哪个版本
  try {
    const profD = Services.dirsvc.get("ProfD", Components.interfaces.nsIFile).path;
    const xpi = profD + "\\extensions\\zotero-kb@authentic3096.github.io.xpi";
    const f = Zotero.File.pathToFile(xpi);
    if (!f.exists()) {
      say("磁盘上的 xpi: 不存在（" + xpi + "）");
    } else {
      const { Subprocess } = ChromeUtils.importESModule(
        "resource://gre/modules/Subprocess.sys.mjs");
      // 用系统 tar 读 zip 里的 manifest.json（Win10+ 自带，不用引额外库）
      let ver = "(读不出)";
      try {
        const p = await Subprocess.call({
          command: "C:\\Windows\\System32\\tar.exe",
          arguments: ["-xOf", xpi, "manifest.json"],
          stdout: "pipe", stderr: "ignore",
        });
        let s = "";
        let chunk;
        while ((chunk = await p.stdout.readString())) s += chunk;
        await p.wait();
        const m = /"version"\s*:\s*"([^"]+)"/.exec(s);
        if (m) ver = m[1];
      } catch (e) { ver = "(tar 读失败: " + e + ")"; }
      say("磁盘上 xpi 的版本: " + ver);
      if (ver !== "(读不出)" && loadedVer !== "(未知)"
          && ver !== loadedVer) {
        say("");
        say("!! 两者不一致 —— Zotero 加载的是内存里的旧代码。");
        say("!! 解决：**完全退出 Zotero 再打开**（关窗口不算）。");
      } else if (ver === loadedVer) {
        say("  两者一致 ✓");
      }
    }
  } catch (e) { say("查磁盘 xpi 出错: " + e); }

  // ---------- 1. 插件本体 ----------
  say("");
  say("=== 1. 插件对象 ===");
  try {
    const KB = Zotero.ZoteroKB;
    say("Zotero.ZoteroKB 存在: " + !!KB);
    if (KB) {
      say("  .version = " + JSON.stringify(KB.version));
      say("  .id = " + JSON.stringify(KB.id));
      say("  .rootURI = " + KB.rootURI);
      say("  serverOk = " + KB.serverOk);
      const n = Object.keys(KB.weightsCache || {}).length;
      say("  weightsCache 条数 = " + n + "（0 说明没拉到权重）");
      say("  有 redrawItemTree 方法: "
          + (typeof KB.redrawItemTree === "function")
          + "（false = 加载的还是旧代码）");
      say("  有 setPinned 方法: " + (typeof KB.setPinned === "function"));
      // 权重缓存里前几条，方便核对星号该不该显示
      const sample = Object.entries(KB.weightsCache || {}).slice(0, 3);
      for (const [k, v] of sample) {
        say("    " + k + " → pinned=" + v.pinned + " weight=" + v.weight);
      }
    }
  } catch (e) { say("读 ZoteroKB 出错: " + e); }

  // ---------- 2. 本地服务 ----------
  say("");
  say("=== 2. 本机服务 ===");
  const KB2 = Zotero.ZoteroKB;
  if (KB2) {
    try {
      const h = await KB2.request("GET", "/health");
      say("/health ok=" + (h && h.ok) + " 版本=" + (h && h.version)
          + " kb_dir=" + (h && h.kb_dir));
    } catch (e) { say("/health 失败: " + e); }
    try {
      const m = await KB2.request("GET", "/models");
      say("/models ok=" + (m && m.ok)
          + " 模型数=" + ((m && m.models) || []).length
          + " 推荐=" + (m && m.recommended));
      if (m && m.error) say("  /models 报错: " + m.error + " / " + (m.hint || ""));
    } catch (e) { say("/models 失败: " + e); }
  } else {
    say("拿不到 ZoteroKB，跳过");
  }

  // ---------- 3. 设置面板的 DOM ----------
  say("");
  say("=== 3. 设置面板 DOM ===");
  let doc = null;
  try {
    const wins = [];
    const en = Services.wm.getEnumerator("zotero:pref");
    while (en.hasMoreElements()) wins.push(en.getNext());
    say("打开的设置窗口数: " + wins.length);
    if (wins.length) doc = wins[0].document;
  } catch (e) { say("枚举设置窗口出错: " + e); }

  if (!doc) {
    say("!! 没有打开的设置窗口 —— 请先打开 设置 → 文献知识库，再运行本脚本");
  } else {
    const pane = doc.getElementById("zotero-kb-settings");
    say("面板根节点存在: " + !!pane);
    if (pane) {
      say("面板可见: " + !pane.hidden
          + "  子节点数: " + pane.children.length);
    }

    // 版本号
    const ver = doc.getElementById("zotero-kb-version");
    say("版本号元素: " + (ver ? JSON.stringify(ver.textContent) : "(不存在)"));

    // 模型下拉
    const sel = doc.getElementById("zotero-kb-ollama-model");
    if (!sel) {
      say("!! 模型下拉元素不存在（xhtml 里没有？）");
    } else {
      say("模型下拉: 标签=" + sel.tagName
          + " 命名空间=" + sel.namespaceURI
          + " option 数=" + sel.options.length
          + " 当前值=" + JSON.stringify(sel.value));
      for (let i = 0; i < Math.min(sel.options.length, 6); i++) {
        const o = sel.options[i];
        say("   option[" + i + "] ns=" + o.namespaceURI
            + " value=" + JSON.stringify(o.value)
            + " text=" + JSON.stringify(o.textContent));
      }
      if (sel.options.length === 0) {
        say("!! option 是空的 —— 填充代码没跑到，或者 createElement 有问题");
        // 再探一次：确认"能不能往里插 option"这件事本身是否可行
        try {
          const probe2 = doc.createElement("option");
          probe2.value = "__probe2__";
          probe2.textContent = "探针2";
          sel.appendChild(probe2);
          const nAfter = sel.options.length;
          say("   → 插入后 options.length = " + nAfter
              + (nAfter > 0 ? "（插得进去，说明问题在填充流程没走到）"
                            : "（连插都插不进去，说明这个 select 不是真 HTML select）"));
          probe2.remove();
        } catch (e) { say("   → 插入抛错: " + e); }
        // 看 settings.js 到底有没有被加载
        //
        // ⚠ 这里本来想用 `Zotero.PreferencePanes.getScope(id)` 去拿面板沙箱，
        //   但读了 preferencePanes.js 才发现**它没有 getScope**（只有
        //   register / unregister / pluginPanes / builtInPanes）。
        //   跟"ItemTreeManager.refresh"是同一类错误：凭印象写 API，然后
        //   被 try/catch 吞掉。所以改成"先检查方法在不在再用"。
        try {
          const pp = Zotero.PreferencePanes;
          say("   PreferencePanes 有 getScope 吗: "
              + (typeof pp.getScope === "function"));
          const panes = (pp && pp.pluginPanes) || [];
          const mine = panes.filter((p) => p.pluginID === "zotero-kb@authentic3096.github.io");
          say("   已注册的本插件面板: " + mine.length + " 个"
              + (mine.length ? ("　id=" + mine[0].id
                                + " loaded=" + mine[0].loaded
                                + " src=" + mine[0].src) : ""));
        } catch (e) { say("   查面板注册出错: " + e); }
      }
      // 手动试一次插入，看能不能成功
      try {
        const probe = doc.createElement("option");
        probe.value = "__probe__";
        probe.textContent = "探针";
        sel.appendChild(probe);
        say("手动插入 option 结果: options.length=" + sel.options.length
            + "（能到 " + (sel.options.length > 0 ? "1+" : "0") + " 就说明插入可行）");
        probe.remove();
      } catch (e) { say("手动插入 option 失败: " + e); }
      // 触发一次下拉（有些实现需要它才渲染）
      try {
        say("select 的 onchange 已绑定: " + !!sel.onchange);
        say("select.disabled: " + sel.disabled + "  hidden: " + sel.hidden);
      } catch (e) { /* ignore */ }
    }

    // 外接 API 那块应该没了
    const api = doc.getElementById("zotero-kb-group-api");
    say("外接 API 那块还在吗: " + (api ? "在（没删干净）" : "已移除 ✓"));
    const provSel = doc.getElementById("zotero-kb-provider");
    say("模型来源下拉还在吗: " + (provSel ? "在（没删干净）" : "已移除 ✓"));

    // 运行环境区
    const envRep = doc.getElementById("zotero-kb-env-report");
    say("运行环境报告区: " + (envRep
        ? ("有内容=" + (envRep.textContent || "").trim().length + " 字")
        : "不存在"));
  }

  // ---------- 4. 权重缓存与右键菜单 ----------
  say("");
  say("=== 4. 权重与菜单 ===");
  try {
    const win = Zotero.getMainWindow();
    const popup = win.document.getElementById("zotero-itemmenu");
    if (popup) {
      for (const id of ["zotero-kb-send-menu", "zotero-kb-classify-item",
                        "zotero-kb-pin-item"]) {
        const el = win.document.getElementById(id);
        say(id + ": " + (el
            ? ("存在，label=" + JSON.stringify(el.getAttribute("label")))
            : "不在（右键菜单没弹过，或代码没跑到）"));
      }
      say("popup 绑过 popupshowing: " + !!popup.__kbBound);
    } else {
      say("找不到 zotero-itemmenu");
    }
  } catch (e) { say("查菜单出错: " + e); }

  // ---------- 输出 ----------
  const text = out.join("\n");
  try { Zotero.debug(text); } catch (e) {}
  // 同时写一份到知识库目录，方便直接读文件
  try {
    const KB3 = Zotero.ZoteroKB;
    if (KB3 && KB3.kbDir && KB3.kbDir()) {
      const p = KB3.kbDir() + "\\settings-diag.txt";
      await Zotero.File.putContentsAsync(p, text);
      say("");
      say("已写入: " + p);
    }
  } catch (e) { say("写诊断文件失败: " + e); }
  return out.join("\n");
})();
