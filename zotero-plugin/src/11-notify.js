/**
 * 11-notify.js —— 提示：进度窗与通知（ProgressWindow 的使用纪律都在这）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 提示

  /**
   * 关掉当前那条进度提示。
   *
   * 为什么要做实例管理：每次提示都 `new Zotero.ProgressWindow()` 的话，
   * "正在整理路径…"、"已开始发送…"、"已发送到 DSH" 三条会**叠在屏幕上**，
   * 用户看到一堆窗口。所以统一只保留一条：新的要显示前，先把上一条关掉。
   *
   * 说明：Zotero 的 ProgressWindow **没有关闭按钮**（API 只有
   * show / close / startCloseTimer / closeAll），它是"点击即关闭"样式。
   * 所以"关闭的 X"用等效方式实现：closeOnClick + 文案里写明可点击关闭。
   */
  closeProgress: function () {
    var self = ZoteroKB;
    try {
      if (self._progressWin) {
        self._progressWin.close();
      }
    } catch (e) { /* 已经关了就忽略 */ }
    self._progressWin = null;
  },


  /** 建一条新的进度提示（会自动关掉上一条）。 */
  newProgress: function (headline, text) {
    var self = ZoteroKB;
    self.closeProgress();
    const pw = new Zotero.ProgressWindow({ closeOnClick: true });
    try { pw.changeHeadline(headline); } catch (e) { /* ignore */ }
    if (text) { try { pw.addDescription(text); } catch (e) { /* ignore */ } }
    try { pw.show(); } catch (e) { /* ignore */ }
    self._progressWin = pw;
    return pw;
  },


  notify: function (title, text, _win, isError) {
    var self = ZoteroKB;
    try {
      const progressWin = self.newProgress(title, text || "");
      progressWin.startCloseTimer(isError ? 12000 : 6000);
    } catch (e) {
      // 进度窗不可用时退回调试日志，至少留下痕迹
      Zotero.debug("[zotero-kb] " + title + " — " + text);
    }
  },
});
