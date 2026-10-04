/**
 * 99-bootstrap.js —— Zotero 要调用的顶层生命周期函数（必须是顶层函数声明）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */

// ============================================================================
// 顶层生命周期函数
//
// Zotero 的 plugins.js 是这样找 bootstrap 方法的：
//     func = scope[method] || Cu.evalInSandbox(`${method};`, scope)
// 它**先看沙箱全局属性**。顶层 `function startup(){}` 会直接成为沙箱全局属性，
// 走第一条路最稳；而把 startup 只挂在 ZoteroKB 对象上时，`scope.startup` 是
// undefined，得靠 Cu.evalInSandbox 兜底 —— 多一层不确定性（本机就遇到过
// "插件 active=true 但 startup 从未执行"，怀疑与这一层有关）。
//
// 所以这里做**双保险**：既暴露顶层函数（Zotero 首选），又保留对象成员写法。
// 顶层函数里同时把加载情况写文件，好判断"到底进没进来"。

function startup(params, reason) {
  try {
    if (typeof ZoteroKB === "undefined" || !ZoteroKB) {
      Zotero.debug("[zotero-kb] 顶层 startup 被调用，但 ZoteroKB 不存在");
      return;
    }
    ZoteroKB.startup(params || {});
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
    try {
      var msg = "startup 顶层包装抛错: " + e
        + "\n" + (e && e.stack ? String(e.stack).slice(0, 800) : "");
      Zotero.File.putContents(
        Zotero.File.pathToFile(
          self.kbDir() + "\\startup-crash.txt"), msg);
    } catch (e3) { /* ignore */ }
  }
}

function shutdown() {
  try {
    if (typeof ZoteroKB !== "undefined" && ZoteroKB) ZoteroKB.shutdown();
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
  }
}

// 窗口钩子：官方文档要求"与窗口相关的 UI 活动（菜单、自定义列等）
// 由 onMainWindowLoad 执行"，否则新开的主窗口不会包含这些改动。
// 这里用来（重新）注册权重列 + 重启轮询 —— 窗口重建时能把它们带回来。
function onMainWindowLoad({ window } = {}) {
  try {
    if (typeof ZoteroKB === "undefined" || !ZoteroKB) return;
    ZoteroKB.onMainWindowLoad(window);
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
  }
}

function onMainWindowUnload({ window } = {}) {
  try {
    if (typeof ZoteroKB === "undefined" || !ZoteroKB) return;
    ZoteroKB.onMainWindowUnload(window);
  } catch (e) {
    try { Zotero.logError(e); } catch (e2) { /* ignore */ }
  }
}

function install() {}
function uninstall() {}
