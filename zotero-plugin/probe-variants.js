// ============================================================================
// 逐个试装 manifest 变体，找出"解析失败 error=-3"的真凶字段。
//
// 前置：先跑 python tools\make_variants.py 生成变体
// 用法：Zotero → 工具 → 开发者 → 运行 JavaScript，粘全部内容，Ctrl+R
//
// 每个变体只做**解析**（getInstallForFile），不真装 —— 这样快且无副作用。
// 解析成功（install.addon 有 id）的变体，再真装那一个。
// ============================================================================

const DIR = "D:\\DSHplugins\\zotero-kb\\zotero-plugin\\variants";
const out = [];
const say = (s) => { out.push(s); Zotero.debug("[kbv] " + s); };

try {
  const { AddonManager } = ChromeUtils.importESModule(
    "resource://gre/modules/AddonManager.sys.mjs");

  const names = ["v0-current", "v1-homepage", "v2-official",
                 "v3-official-noupdate", "v4-bss", "v5-ascii",
                 "v6-noicons", "v7-pngicon"];

  say("变体解析测试（error=0 且 addon.id 有值 = 成功）");
  say("========================================");

  let winner = null;
  for (const n of names) {
    const path = DIR + "\\" + n + ".xpi";
    const file = Zotero.File.pathToFile(path);
    if (!file.exists()) { say(n + ": 文件不存在"); continue; }
    let line = n.padEnd(22);
    try {
      const inst = await AddonManager.getInstallForFile(
        file, "application/x-xpinstall");
      if (!inst) { say(line + " → install 为空"); continue; }
      const err = inst.error;
      const addon = inst.addon;
      const okId = addon && addon.id;
      line += " error=" + err + " state=" + inst.state +
              " addon=" + (addon ? ("id=" + okId + " v=" + addon.version) : "null");
      if (err === 0 && okId) {
        line += "   ✅ 可解析";
        if (!winner) winner = { name: n, inst: inst };
      }
      say(line);
    } catch (e) {
      say(line + " → 抛错: " + e);
    }
  }

  // 对第一个可解析的变体真装
  if (winner) {
    say("");
    say("=== 试装可解析的变体: " + winner.name + " ===");
    const inst = winner.inst;
    const done = await new Promise((resolve) => {
      try {
        inst.addListener({
          onInstallFailed: () => resolve("onInstallFailed err=" + inst.error),
          onInstallEnded: (i, a) => resolve("onInstallEnded id=" + (a && a.id)),
        });
        inst.install();
      } catch (e) { resolve("install() 抛错: " + e); }
      setTimeout(() => resolve("超时"), 12000);
    });
    say("回调: " + done);
    const got = await AddonManager.getAddonByID("zotero-kb@authentic3096.github.io");
    say("装后查询: " + (got ? ("✅ 已注册 v" + got.version +
        " isActive=" + got.isActive) : "❌ 仍未注册"));
  } else {
    say("");
    say("❌ 所有变体都无法解析 —— 说明问题不在 manifest 字段，");
    say("   而在包结构或 bootstrap.js 本身。下一步查后者。");
  }
} catch (e) {
  say("❌ 异常: " + e);
}

const text = out.join("\n");
try {
  const pw = new Zotero.ProgressWindow({ closeOnClick: false });
  pw.changeHeadline("变体解析测试结果");
  pw.addDescription(text.slice(0, 1400));
  pw.show();
  pw.startCloseTimer(180000);
} catch (e) { /* ignore */ }
return text;
