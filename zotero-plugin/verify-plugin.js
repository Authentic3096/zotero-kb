// ============================================================================
// 插件全功能验证（可在 Zotero 里直接跑，也可由 tools\zotero_js.py 远程派发）。
//
// 用法（二选一）：
//   · Zotero → 工具 → 开发者 → 运行 JavaScript → 粘贴 → Ctrl+R
//   · python tools\zotero_js.py verify     （自动执行并回传结果）
// ============================================================================

const L = [];
const say = (s) => L.push(s);
let pass = 0, fail = 0;
const check = (name, cond, detail) => {
  if (cond) { pass++; say("  PASS  " + name); }
  else { fail++; say("  FAIL  " + name + (detail ? "   → " + detail : "")); }
};

try {
  const KB = Zotero.ZoteroKB;
  say("Zotero " + Zotero.version + " / 插件 "
      + (KB ? KB.version : "(未挂载)"));
  if (!KB) {
    say("");
    say("⚠ Zotero.ZoteroKB 不存在。若刚更新过插件，请**完全重启 Zotero**");
    say("  （bootstrapped 插件在运行中更新不会重跑 startup）。");
    throw new Error("插件对象未挂载");
  }

  // ---------------------------------------------------------------- 1
  say("");
  say("[1] 生命周期与轮询");
  check("startup 完成（对象已挂载）", true);
  // 用 taskPolling / tickCount 判断，不要看 taskTimer：
  // 现在轮询是 setTimeout 递归，句柄在两次回调之间是 null，
  // 拿 taskTimer 判会误报"没在跑"（本机就这么误报过一次）。
  check("任务轮询在跑", !!KB.taskPolling || (KB.tickCount || 0) > 0,
        "taskPolling=" + !!KB.taskPolling + " tickCount=" + (KB.tickCount || 0));
  check("轮询心跳存在", !!KB.lastTickAt, "lastTickAt 为空");
  say("        tickCount=" + (KB.tickCount || 0)
      + "  lastTickAt=" + (KB.lastTickAt || "-")
      + "  taskBusy=" + !!KB.taskBusy + "  alive=" + !!KB.alive);

  // ---------------------------------------------------------------- 2
  say("");
  say("[2] 本地服务与认证");
  const ok = await KB.healthCheck();
  check("服务在线", ok);
  check("token 已自动配置", !!KB.getPref(KB.PREFS.token, ""),
        "token 为空 → /weights 与 /task 会 401");
  try {
    const st = await KB.request("GET", "/status");
    check("/status 可读（token 有效）", !!(st && st.items !== undefined));
    if (st) say("        知识库 " + st.items + " 篇 / " + st.chunks + " 切片");
  } catch (e) { check("/status 可读（token 有效）", false, String(e)); }

  // ---------------------------------------------------------------- 3
  say("");
  say("[3] 权重列");
  check("列已注册", !!KB.weightColumnKey, String(KB.weightColumnKey));
  const cached = Object.keys(KB.weightsCache || {}).length;
  check("权重缓存非空", cached > 0, cached + " 条");
  say("        缓存 " + cached + " 条；样本 "
      + Object.keys(KB.weightsCache || {}).slice(0, 5).join(", "));
  const d = KB.colDiag || {};
  check("dataProvider 被调用过", (d.calls || 0) > 0, "calls=" + (d.calls || 0));
  check("有命中（key 对得上）", (d.hits || 0) > 0,
        "calls=" + (d.calls || 0) + " hits=" + (d.hits || 0)
        + " sample=" + JSON.stringify((d.sample || []).slice(0, 4)));
  say("        providerCalls=" + (d.calls || 0)
      + "  providerHits=" + (d.hits || 0));
  // 用真实条目核对缓存 key 与 item.key 是否同一体系。
  // ⚠ Zotero.Items.getAll 是 **async**（items.js:122 `this.getAll = async function`），
  //   必须 await —— 忘了会拿到 Promise，接着 for...of 就报
  //   "items is not iterable"，.find 报 "is not a function"。
  try {
    const items = (await Zotero.Items.getAll(
      Zotero.Libraries.userLibraryID, true, false)) || [];
    let matched = 0;
    for (const it of items) {
      if (KB.weightsCache[it.key]) matched++;
    }
    check("缓存 key 能匹配到真实条目", matched > 0, "匹配 " + matched + " 条");
    say("        全库 " + items.length + " 条中命中 " + matched + " 条");
  } catch (e) { check("缓存 key 能匹配到真实条目", false, String(e)); }
  // 刷新一次
  try {
    const ok2 = await KB.refreshWeights();
    check("refreshWeights() 可用", ok2);
  } catch (e) { check("refreshWeights() 可用", false, String(e)); }

  // ---------------------------------------------------------------- 4
  say("");
  say("[4] 分类建议（真实调用本地模型）");
  try {
    const items = (await Zotero.Items.getAll(
      Zotero.Libraries.userLibraryID, true, false)) || [];
    const it = items.find((x) => x.getField && x.getField("title"));
    check("取到测试条目", !!it);
    if (it) {
      const meta = KB.buildMeta(it);
      check("buildMeta 有标题", !!meta.title, JSON.stringify(meta).slice(0, 80));
      const res = await KB.request("POST", "/classify",
        { item: meta, categories: [], model: "" });
      check("/classify 返回建议",
            !!(res && !res.error && res.category !== undefined),
            res && res.error ? res.error : JSON.stringify(res).slice(0, 120));
      if (res && !res.error) {
        say("        《" + meta.title.slice(0, 30) + "》→ " + res.category
            + "（置信 " + res.confidence + "）");
      }
    }
  } catch (e) { check("/classify 返回建议", false, String(e)); }

  // ---------------------------------------------------------------- 5
  say("");
  say("[5] 分类与写入能力（只读检查，不改动）");
  try {
    const cols = Zotero.Collections.getByLibrary(
      Zotero.Libraries.userLibraryID, true) || [];
    check("能列出分类", cols.length > 0, cols.length + " 个");
    say("        " + cols.map((c) => c.name).join("、"));
    check("能找到「分类 B」", cols.some((c) => c.name === "分类 B"));
    check("中英重复已消除",
          !cols.some((c) => c.name === "magnetic dipole"
                     || c.name === "attention mechanism"));
  } catch (e) { check("能列出分类", false, String(e)); }

  // ---------------------------------------------------------------- 6
  say("");
  say("[6] 依赖的 Zotero API");
  for (const [n, f] of [
    ["ProgressWindow", () => typeof Zotero.ProgressWindow === "function"],
    ["Prefs", () => !!Zotero.Prefs],
    ["Notifier", () => !!Zotero.Notifier],
    ["ItemTreeManager", () => !!Zotero.ItemTreeManager],
    ["PreferencePanes", () => !!(Zotero.PreferencePanes
                                 && Zotero.PreferencePanes.register)],
    ["HTTP", () => !!Zotero.HTTP],
    ["Services.prompt", () => typeof Services.prompt.confirmEx === "function"],
  ]) check(n, f());
  // 沙箱全局（曾经把 Zotero.setInterval 写错）
  check("setInterval 是沙箱全局", typeof setInterval === "function");
  check("不是 Zotero.setInterval", typeof Zotero.setInterval === "undefined");

  // ---------------------------------------------------------------- 7
  say("");
  say("[7] 监听器与状态文件");
  check("新条目监听已注册",
        Array.isArray(KB.notifyIDs) && KB.notifyIDs.length > 0,
        JSON.stringify(KB.notifyIDs));
  check("autoProcess 开启", KB.getPref(KB.PREFS.autoProcess, true) !== false);
  try {
    KB.writeStatusFile();
    const f = Zotero.File.pathToFile(
      (Zotero.ZoteroKB && Zotero.ZoteroKB.kbDir
                    ? Zotero.ZoteroKB.kbDir() + "\\plugin-status.json"
                    : ""));
    check("状态文件可写", f.exists());
  } catch (e) { check("状态文件可写", false, String(e)); }

} catch (e) {
  fail++;
  say("!! 异常中断: " + e);
}

say("");
say("========================================");
say("通过 " + pass + "　失败 " + fail);
say("========================================");
const text = L.join("\n");
try {
  const pw = new Zotero.ProgressWindow({ closeOnClick: false });
  pw.changeHeadline("插件验证：" + pass + " 通过 / " + fail + " 失败");
  pw.addDescription(text.slice(-1500));
  pw.show();
  pw.startCloseTimer(240000);
} catch (e) { /* ignore */ }
return text;
