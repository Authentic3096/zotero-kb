// ============================================================================
// 一次运行，自动试装全部候选，找出能成功解析的那一个。
//
// 前置（可选，用于对照）：python tools\make_variants.py
// 用法：Zotero → 工具 → 开发者 → 运行 JavaScript，粘全部内容，Ctrl+R
//
// 会依次尝试：
//   · 正式包 zotero-kb-0.1.5.xpi（已改用 browser_specific_settings）
//   · variants\ 下的 8 个对照变体（每个只差一个字段）
// 对每个包报告 install.error / state / addon.id，然后真装**第一个成功的**。
//
// 关键背景（已从 Firefox schema 与 Zotero 源码确认）：
//   manifest 里 `applications` 是废弃字段（schema: "please use
//   'browser_specific_settings'"，max_manifest_version: 2），
//   而 Zotero 注释说 "as of MV3, only browser_specific_settings is accepted"。
//   用 applications 时 getInstallForFile 直接返回 error=-3（CORRUPT_FILE）。
// ============================================================================

// 从 Zotero 拿插件自己的目录（早期排查装载机制时用来反复试 manifest 的）

// ⚠ 整体包进 async IIFE：脚本里要用 await（取插件目录、装包），
//   而 Zotero 的"运行 JavaScript"窗口是在顶层求值的 —— 顶层 await
//   会直接语法错误。包起来之后 await 与末尾的 return 都合法。
(async () => {
  // 从 Zotero 拿插件自己的目录。
  // ⚠ 不要写死路径（那是开发机的），也不要写成
  //   `const PLUGIN = (await (async () => {...})());` 这种一行套娃 ——
  //   括号极容易数错（本机数错过一次，多一个 `)` 直接语法错误）。
  //   拆成"先定义、再 await 调用"两步，一眼能看出括号对不对。
  const getPluginDir = async () => {
    const { AddonManager: AM } = ChromeUtils.importESModule(
      "resource://gre/modules/AddonManager.sys.mjs");
    const a = await AM.getAddonByID("zotero-kb@authentic3096.github.io");
    return (a && a.getResourceURI) ? a.getResourceURI("").spec : "";
  };
  const PLUGIN = await getPluginDir();
    const out = [];
    const say = (s) => { out.push(s); Zotero.debug("[kbx] " + s); };
    const ID = "zotero-kb@authentic3096.github.io";

    try {
      const { AddonManager } = ChromeUtils.importESModule(
        "resource://gre/modules/AddonManager.sys.mjs");
      say("Zotero " + Zotero.version + " / Firefox " + Services.appinfo.platformVersion);

      // 候选清单：正式包在前
      const cands = [{ name: "0.1.5-official", path: PLUGIN + "\\zotero-kb-0.1.5.xpi" }];
      const variants = ["v0-current", "v1-homepage", "v2-official",
                        "v3-official-noupdate", "v4-bss", "v5-ascii",
                        "v6-noicons", "v7-pngicon"];
      for (const v of variants) {
        cands.push({ name: v, path: PLUGIN + "\\variants\\" + v + ".xpi" });
      }

      say("");
      say("名称                   error  state  addon");
      say("--------------------------------------------------");

      let winner = null;
      for (const c of cands) {
        const file = Zotero.File.pathToFile(c.path);
        if (!file.exists()) { say(c.name.padEnd(22) + " 文件不存在"); continue; }
        let line = c.name.padEnd(22);
        try {
          const inst = await AddonManager.getInstallForFile(
            file, "application/x-xpinstall");
          if (!inst) { say(line + " install=null"); continue; }
          const addon = inst.addon;
          const id = addon && addon.id;
          line += " " + String(inst.error).padStart(5) + "  " +
                  String(inst.state).padStart(5) + "  " +
                  (addon ? ("id=" + id + " v=" + addon.version) : "null");
          if (inst.error === 0 && id) {
            line += "   ✅";
            if (!winner) winner = { name: c.name, inst: inst, path: c.path };
          }
          say(line);
        } catch (e) {
          say(line + " 抛错: " + e);
        }
      }

      if (winner) {
        say("");
        say("=== 试装第一个可解析的包: " + winner.name + " ===");
        say("    " + winner.path);
        const inst = winner.inst;
        const done = await new Promise((resolve) => {
          try {
            inst.addListener({
              onInstallFailed: () => resolve("onInstallFailed err=" + inst.error),
              onInstallCancelled: () => resolve("onInstallCancelled"),
              onInstallEnded: (i, a) => resolve("onInstallEnded id=" + (a && a.id)),
            });
            inst.install();
          } catch (e) { resolve("install() 抛错: " + e); }
          setTimeout(() => resolve("超时"), 15000);
        });
        say("回调: " + done);

        const got = await AddonManager.getAddonByID(ID);
        if (got) {
          say("✅ 已注册: v" + got.version + " isActive=" + got.isActive);
          if (!got.isActive) {
            try { await got.enable(); say("已 enable → isActive=" + got.isActive); }
            catch (e) { say("enable 失败: " + e); }
          }
          say("");
          say("==> 成功！请把上面这段发给助手，我把正式包改成这个变体的写法。");
        } else {
          say("❌ 装了但注册表里没有");
        }
      } else {
        say("");
        say("❌ 全部包都无法解析 —— 问题不在 manifest 字段。");
        say("   下一步应查包结构或 bootstrap.js（它会在 addon 建立后立即执行，");
        say("   抛错也会被报成加载失败）。");
      }
    } catch (e) {
      say("❌ 异常: " + e);
    }

    const text = out.join("\n");
    try {
      const pw = new Zotero.ProgressWindow({ closeOnClick: false });
      pw.changeHeadline("插件试装结果");
      pw.addDescription(text.slice(0, 1500));
      pw.show();
      pw.startCloseTimer(180000);
    } catch (e) { /* ignore */ }
    return text;
})();
