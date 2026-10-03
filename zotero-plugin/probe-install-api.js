// ============================================================================
// 探明 Zotero 10 里可用的插件安装 API，并尝试安装本插件。
//
// 背景：前一个脚本用 `AddonManager.getInstallForFile()` 报
//   TypeError: AddonManager.getInstallForFile is not a function
// 说明这个方法在 Zotero 10（Firefox 140 基座）里已经不存在或改名了。
// 本脚本先把 AddonManager 上真实存在的方法列出来，再逐个尝试可用的安装入口，
// 每一步都打印结果 —— 这样一次就能定位。
//
// 用法：Zotero → 工具 → 开发者 → 运行 JavaScript，粘全部内容，Ctrl+R
// ============================================================================

const XPI = "D:\\DSHplugins\\zotero-kb\\zotero-plugin\\zotero-kb-0.1.4.xpi";
const out = [];
const say = (s) => { out.push(s); Zotero.debug("[kb2] " + s); };

try {
  say("Zotero " + Zotero.version + " / Firefox 基座 " + Services.appinfo.platformVersion);

  // ---- 1) AddonManager 上到底有什么方法
  const names = [];
  for (const k in AddonManager) {
    try {
      if (typeof AddonManager[k] === "function") names.push(k);
    } catch (e) { /* ignore */ }
  }
  say("--- AddonManager 的函数（共 " + names.length + "）---");
  say(names.sort().join(", "));

  // 也看看原型链（有些实现挂在 prototype 上）
  try {
    const proto = Object.getPrototypeOf(AddonManager);
    const pnames = [];
    for (const k of Object.getOwnPropertyNames(proto)) {
      if (typeof AddonManager[k] === "function") pnames.push(k);
    }
    if (pnames.length) say("原型链方法: " + pnames.sort().join(", "));
  } catch (e) { /* ignore */ }

  // ---- 2) 真的存在哪些 install 相关入口
  const cands = names.filter((n) => /install|Install/.test(n));
  say("--- 与安装相关的方法 ---");
  say(cands.length ? cands.join(", ") : "（没有）");

  // ---- 3) 逐个尝试
  const file = Zotero.File.pathToFile(XPI);
  say("--- xpi 存在: " + file.exists() + " 大小: " + file.fileSize + " ---");

  let install = null;
  const tries = [
    ["getInstallForFile", () => AddonManager.getInstallForFile(file)],
    ["getInstallForFile(path,type)", () => AddonManager.getInstallForFile(file.path, "application/x-xpinstall")],
    ["getInstallForURL", () => AddonManager.getInstallForURL(
      Services.io.newFileURI(file).spec, "application/x-xpinstall")],
    ["getInstallForURL(2)", () => AddonManager.getInstallForURL(
      Services.io.newFileURI(file).spec)],
  ];
  for (const [label, fn] of tries) {
    try {
      const r = await fn();
      if (r) {
        install = r;
        say("✅ 可用入口: " + label);
        const a = r.addon || {};
        say("   id=" + a.id + " name=" + a.name + " version=" + a.version +
            " type=" + a.type);
        say("   isActive=" + a.isActive + " appDisabled=" + a.appDisabled +
            " isCompatible=" + a.isCompatible);
        say("   matchingTargetApplication=" +
            JSON.stringify(a.matchingTargetApplication || null));
        say("   install.error=" + r.error + " state=" + r.state);
        break;
      } else {
        say("… " + label + " 返回空");
      }
    } catch (e) {
      say("✗ " + label + " → " + e);
    }
  }

  // ---- 4) 执行安装
  if (install) {
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
                    " version=" + (addon && addon.version)),
        });
        install.install();
      } catch (e) {
        resolve("install() 抛错: " + e);
      }
      setTimeout(() => resolve("超时（10s 无回调）"), 10000);
    });
    say("安装回调: " + done);

    const got = await AddonManager.getAddonByID("zotero-kb@authentic3096.github.io");
    say("装后查询: " + (got
      ? ("✅ 找到 version=" + got.version + " isActive=" + got.isActive)
      : "❌ 仍未找到"));
    if (got && !got.isActive) {
      try { await got.enable(); say("已 enable，isActive=" + got.isActive); }
      catch (e) { say("enable 失败: " + e); }
    }
  } else {
    say("❌ 所有已知安装入口都不可用。");
    say("   备选：用 Zotero.Plugins 的内部导入，或走 extensions.json 手工注册。");
  }

  // ---- 5) 顺带看看 Zotero 自己的插件管理入口
  say("--- Zotero.Plugins 上的方法 ---");
  const zp = [];
  for (const k in Zotero.Plugins) {
    try { if (typeof Zotero.Plugins[k] === "function") zp.push(k); } catch (e) {}
  }
  say(zp.sort().join(", "));

} catch (e) {
  say("❌ 异常: " + e + "\n" + (e && e.stack ? e.stack : ""));
}

const text = out.join("\n");
try {
  const pw = new Zotero.ProgressWindow({ closeOnClick: false });
  pw.changeHeadline("插件安装 API 探查结果");
  pw.addDescription(text.slice(0, 1200));
  pw.show();
  pw.startCloseTimer(120000);
} catch (e) { /* ignore */ }
return text;
