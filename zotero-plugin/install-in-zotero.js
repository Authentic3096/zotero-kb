// ============================================================================
// 正确姿势安装本插件：显式 import ES 模块拿真正的 AddonManager。
//
// 前两次失败的根因（已从源码确认）：
//   1. `AddonManager.getInstallForFile is not a function`
//      —— Zotero 的"运行 JavaScript"窗口里那个全局 AddonManager 不是
//         resource://gre/modules/AddonManager.sys.mjs 导出的那个对象。
//         真正的 AddonManager 是 `export var AddonManager = {...}`（该文件 3997 行），
//         getInstallForFile 在 4287 行。必须显式 import 才能拿到。
//   2. Zotero 10 的安装还要求 mimetype 与 install source，参数不能省。
//      源码里的签名是：
//         getInstallForFile(aFile, aMimetype, aTelemetryInfo, aUseSystemLocation)
//
// 用法：Zotero → 工具 → 开发者 → 运行 JavaScript，粘全部内容，Ctrl+R
// ============================================================================

const XPI = "D:\\DSHplugins\\zotero-kb\\zotero-plugin\\zotero-kb-0.1.4.xpi";
const MIME = "application/x-xpinstall";
const out = [];
const say = (s) => { out.push(s); Zotero.debug("[kb3] " + s); };

try {
  // ---- 1) 拿真正的 AddonManager（ES 模块导入）
  say("Zotero " + Zotero.version + " / Firefox " + Services.appinfo.platformVersion);

  let AM = null;
  const importTries = [
    ["resource://gre/modules/AddonManager.sys.mjs", "AddonManager"],
    ["resource://gre/modules/addons/AddonManager.sys.mjs", "AddonManager"],
  ];
  for (const [path, name] of importTries) {
    try {
      const mod = ChromeUtils.importESModule(path);
      if (mod && mod[name]) { AM = mod[name]; say("✅ 导入成功: " + path); break; }
    } catch (e) { say("✗ 导入 " + path + " → " + e); }
  }
  if (!AM) { say("❌ 无法导入 AddonManager 模块"); throw new Error("no AddonManager"); }

  say("   typeof getInstallForFile = " + typeof AM.getInstallForFile);
  const fns = [];
  for (const k in AM) {
    try { if (typeof AM[k] === "function" && /install/i.test(k)) fns.push(k); } catch (e) {}
  }
  say("   安装相关方法: " + (fns.join(", ") || "（无）"));

  // ---- 2) 解析 xpi
  const file = Zotero.File.pathToFile(XPI);
  say("xpi 存在=" + file.exists() + " 大小=" + file.fileSize);

  let install = null;
  try {
    install = await AM.getInstallForFile(file, MIME);
  } catch (e) {
    say("✗ getInstallForFile(file, mime) → " + e);
    try {
      install = await AM.getInstallForFile(file);
    } catch (e2) {
      say("✗ getInstallForFile(file) → " + e2);
    }
  }
  if (!install) {
    // 退一步：用 file URI
    try {
      const uri = Services.io.newFileURI(file).spec;
      install = await AM.getInstallForURL(uri, MIME);
      say("（改用 getInstallForURL 成功）");
    } catch (e) {
      say("✗ getInstallForURL → " + e);
    }
  }
  if (!install) { say("❌ 拿不到 install 对象"); throw new Error("no install"); }

  // ---- 3) 关键诊断：把判定信息全打出来
  const a = install.addon || {};
  say("--- 解析结果 ---");
  say("  id=" + a.id + "  name=" + a.name + "  version=" + a.version);
  say("  type=" + a.type + "  loader=" + a.loader +
      "  manifestVersion=" + a.manifestVersion);
  say("  isActive=" + a.isActive + "  appDisabled=" + a.appDisabled +
      "  userDisabled=" + a.userDisabled);
  say("  isCompatible=" + a.isCompatible);
  say("  matchingTargetApplication=" +
      JSON.stringify(a.matchingTargetApplication || null));
  say("  targetApplications=" + JSON.stringify(a.targetApplications || null));
  say("  blocklistState=" + a.blocklistState);
  say("  install.error=" + install.error + "  state=" + install.state);
  try { say("  isCompatibleWith()=" + a.isCompatibleWith()); }
  catch (e) { say("  isCompatibleWith() 抛错: " + e); }

  // ---- 4) 安装
  say("--- 开始安装 ---");
  const done = await new Promise((resolve) => {
    try {
      install.addListener({
        onDownloadFailed: () => resolve("onDownloadFailed"),
        onDownloadCancelled: () => resolve("onDownloadCancelled"),
        onInstallFailed: () => resolve("onInstallFailed error=" + install.error),
        onInstallCancelled: () => resolve("onInstallCancelled"),
        onInstallEnded: (inst, addon) =>
          resolve("onInstallEnded id=" + (addon && addon.id) +
                  " v=" + (addon && addon.version)),
      });
      install.install();
    } catch (e) { resolve("install() 抛错: " + e); }
    setTimeout(() => resolve("超时（10s 无回调）"), 10000);
  });
  say("回调: " + done);

  // ---- 5) 核对
  const got = await AM.getAddonByID("zotero-kb@authentic3096.github.io");
  if (got) {
    say("✅ 已注册: v" + got.version + " isActive=" + got.isActive);
    if (!got.isActive) {
      try { await got.enable(); say("已 enable → isActive=" + got.isActive); }
      catch (e) { say("enable 失败: " + e); }
    }
  } else {
    say("❌ 装完仍未找到（注册表里没有）");
  }
} catch (e) {
  say("❌ 异常: " + e + "\n" + (e && e.stack ? e.stack : ""));
}

const text = out.join("\n");
try {
  const pw = new Zotero.ProgressWindow({ closeOnClick: false });
  pw.changeHeadline("插件安装结果");
  pw.addDescription(text.slice(0, 1200));
  pw.show();
  pw.startCloseTimer(120000);
} catch (e) { /* ignore */ }
return text;
