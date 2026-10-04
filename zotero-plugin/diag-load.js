// ============================================================================
// 极简诊断：插件到底加载了没有？bootstrap.js 执行过没有？
//
// 用法：Zotero → 工具 → 开发者 → 运行 JavaScript，粘全部，Ctrl+R
// 输出很短，直接贴给我即可。
// ============================================================================

const L = [];
const say = (s) => L.push(s);

try {
  say("Zotero " + Zotero.version);

  // 1) 插件对象挂上没有（bootstrap.js 有没有跑到 startup）
  say("Zotero.ZoteroKB = " + (Zotero.ZoteroKB ? "有 ✅" : "无 ❌"));

  // 2) Zotero.Plugins 上有哪些方法（找列举已加载插件的入口）
  const fns = [];
  for (const k in Zotero.Plugins) {
    try { if (typeof Zotero.Plugins[k] === "function") fns.push(k); } catch (e) {}
  }
  say("Zotero.Plugins 方法: " + fns.sort().join(", "));

  // 3) 用几种可能的方式找我们的插件
  const tries = [];
  try { if (Zotero.Plugins.getAll) {
    const all = Zotero.Plugins.getAll();
    tries.push("getAll() → " + (Array.isArray(all) ? all.length : typeof all));
    if (Array.isArray(all)) {
      const mine = all.find((a) => String(a && a.id).indexOf("zotero-kb") >= 0);
      say("  我们的插件: " + (mine ? ("找到 v" + mine.version
          + " active=" + mine.isActive) : "不在列表里"));
      say("  列表: " + all.map((a) => a && a.id).join(", "));
    }
  } } catch (e) { tries.push("getAll 抛错: " + e); }
  try { if (Zotero.Plugins._plugins) {
    const ks = Object.keys(Zotero.Plugins._plugins);
    tries.push("_plugins → " + ks.length + " 个");
    say("  _plugins: " + ks.join(", "));
  } } catch (e) { tries.push("_plugins 抛错: " + e); }
  for (const t of tries) say("  " + t);

  // 4) AddonManager 用 ES 模块导入（可信的那个），查插件状态
  try {
    const { AddonManager } = ChromeUtils.importESModule(
      "resource://gre/modules/AddonManager.sys.mjs");
    const all = await AddonManager.getAllAddons();
    const mine = all.find((a) => String(a.id).indexOf("zotero-kb") >= 0);
    if (mine) {
      say("AddonManager: v" + mine.version
          + " type=" + mine.type
          + " isActive=" + mine.isActive
          + " appDisabled=" + mine.appDisabled
          + " userDisabled=" + mine.userDisabled
          + " blocklistState=" + mine.blocklistState);
      say("  path=" + (mine.path || "?"));
      // 关键：isCompatible / matchingTargetApplication
      try { say("  isCompatible=" + mine.isCompatible
                + " matchingTargetApp="
                + JSON.stringify(mine.matchingTargetApplication || null)); }
      catch (e) { say("  读 isCompatible 抛错: " + e); }
    } else {
      say("AddonManager: 找不到 zotero-kb");
    }
  } catch (e) { say("AddonManager 出错: " + e); }

  // 5) 手动读一次 bootstrap.js，确认文件内容是新版（含 Zotero.ZoteroKB 挂载）
  //
  // ⚠ 这里原来写死了 `C:/Users/<用户名>/AppData/.../yf7pgbex.default/...`
  //   —— 既泄露用户名，换台机器也跑不了。改成从 AddonManager 拿插件自己的
  //   resource URI（它本来就是这么加载插件的，见 plugins.js 的 _loadScope）。
  try {
    const { AddonManager: AM1 } = ChromeUtils.importESModule(
      "resource://gre/modules/AddonManager.sys.mjs");
    const a1 = await AM1.getAddonByID("zotero-kb@authentic3096.github.io");
    const base = (a1 && a1.getResourceURI) ? a1.getResourceURI("").spec : "";
    say("插件 resource URI: " + (base || "(取不到)"));
    if (!base) throw new Error("拿不到 resource URI，跳过这一步");
    const txt = await Zotero.File.getContentsFromURLAsync(base + "bootstrap.js");
    say("包内 bootstrap.js: " + txt.length + " 字符");
    say("  含 'Zotero.ZoteroKB = this': "
        + (txt.indexOf("Zotero.ZoteroKB = this") >= 0 ? "是 ✅" : "否 ❌"));
    say("  含 'registerWeightColumn': "
        + (txt.indexOf("registerWeightColumn") >= 0 ? "是 ✅" : "否 ❌"));
  } catch (e) { say("读包内 bootstrap.js 失败: " + e); }

  // 5b) ★ 关键：Zotero 是用 addon.getResourceURI() + 'bootstrap.js' 去加载的
  //     （见 plugins.js 的 _loadScope）。如果这个 URI 取不到文件，
  //     scope 就是空的 → 报 "missing bootstrap method 'startup'"。
  //     日志已经证实我们正撞在这个现象上，所以这里直接把它验出来。
  try {
    const { AddonManager: AM2 } = ChromeUtils.importESModule(
      "resource://gre/modules/AddonManager.sys.mjs");
    const addon = await AM2.getAddonByID("zotero-kb@authentic3096.github.io");
    if (addon) {
      const resURI = addon.getResourceURI();
      say("★ addon.getResourceURI() = " + (resURI ? resURI.spec : "(null)"));
      const bootURI = (resURI ? resURI.spec : "") + "bootstrap.js";
      say("★ 拼出的 bootstrap URI = " + bootURI);
      // 试着读它 —— 这一步模拟 Zotero 的 loadSubScript
      try {
        const t2 = await Zotero.File.getContentsFromURLAsync(bootURI);
        say("★ 能读到 bootstrap.js：" + t2.length + " 字符 "
            + (t2.length > 100 ? "✅" : "❌ 内容异常"));
      } catch (e) {
        say("★ ❌ 读不到：这就解释了 'missing bootstrap method'！→ " + e);
      }
      // 也看看 addon 的类型/loader —— plugins.js 只处理 type == 'extension'
      say("★ addon.type = " + addon.type + "  loader = " + addon.loader
          + "  isActive = " + addon.isActive);
    } else {
      say("★ AddonManager 找不到插件");
    }
  } catch (e) { say("★ 检查 getResourceURI 失败: " + e); }

  // 5c) 当前 Zotero.Plugins 内部状态
  try {
    const scopeMap = Zotero.Plugins._scopes || Zotero.Plugins.scopes;
    say("★ Zotero.Plugins._scopes = "
        + (scopeMap ? ("存在，键：" + Array.from(scopeMap.keys
          ? scopeMap.keys() : Object.keys(scopeMap)).join(", ")) : "取不到"));
  } catch (e) { say("★ 读 _scopes 失败: " + e); }

  // 6) 首选项是否写进去了
  for (const k of ["server", "token", "autoProcess"]) {
    try {
      say("pref " + k + " = " + JSON.stringify(
        Zotero.Prefs.get("extensions.zotero-kb." + k)));
    } catch (e) { say("pref " + k + " 读取失败: " + e); }
  }

  // 7) 如果插件对象没挂上，主动 disable→enable 强制重跑 bootstrap 生命周期。
  //    这样不用反复重启 Zotero，而且能直接看到 startup 里的真实报错。
  if (!Zotero.ZoteroKB) {
    say("");
    say("[7] 尝试强制重载插件（disable → enable）…");
    try {
      const { AddonManager } = ChromeUtils.importESModule(
        "resource://gre/modules/AddonManager.sys.mjs");
      const addon = await AddonManager.getAddonByID("zotero-kb@authentic3096.github.io");
      if (!addon) {
        say("  ❌ 找不到插件，无法重载");
      } else {
        say("  当前: v" + addon.version + " isActive=" + addon.isActive);
        await addon.disable();
        say("  已 disable");
        await new Promise((r) => setTimeout(r, 800));
        await addon.enable();
        say("  已 enable");
        await new Promise((r) => setTimeout(r, 1200));
        say("  重载后 Zotero.ZoteroKB = "
            + (Zotero.ZoteroKB ? "有 ✅（说明 startup 能跑通）" : "仍无 ❌"));
        if (Zotero.ZoteroKB) {
          say("  version=" + Zotero.ZoteroKB.version
              + " 权重列=" + Zotero.ZoteroKB.weightColumnKey);
        }
      }
    } catch (e) {
      say("  重载失败: " + e);
    }
    // 顺便看看状态文件里有没有记下 startup 的失败
    try {
      const f = Zotero.File.pathToFile(
        (Zotero.ZoteroKB && Zotero.ZoteroKB.kbDir
      ? Zotero.ZoteroKB.kbDir() + "\\plugin-status.json" : ""));
      if (f.exists()) {
        say("");
        say("[8] 状态文件内容（含 startup 失败原因）:");
        say(Zotero.File.getContents(f).slice(0, 900));
      } else {
        say("");
        say("[8] 状态文件不存在 —— 说明 startup() 完全没被执行过");
      }
    } catch (e) { say("读状态文件失败: " + e); }
  }

} catch (e) {
  say("外层异常: " + e);
}

const text = L.join("\n");
try {
  const pw = new Zotero.ProgressWindow({ closeOnClick: false });
  pw.changeHeadline("插件加载诊断");
  pw.addDescription(text.slice(0, 1400));
  pw.show();
  pw.startCloseTimer(180000);
} catch (e) {}
return text;
