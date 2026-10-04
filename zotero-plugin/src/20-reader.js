/* eslint-disable no-undef */
/**
 * 20-reader.js —— **阅读器**里的"选中文字"入口。
 *
 * ## 为什么用官方事件，而不是去挖 reader 的内部
 *
 * Zotero 7+ 给了公开接口：
 *
 *     Zotero.Reader.registerEventListener('renderTextSelectionPopup',
 *         (event) => { const {reader, doc, params, append} = event; … }, pluginID);
 *
 * 选中文字就在 **`params.annotation.text`** 里由 Zotero 递过来，`append()` 可以
 * 往那个弹出框里加自己的按钮 —— 这正是本机装的 Translate for Zotero 的做法
 * （我读了它的源码确认：`addon.data.translate.selectedText = params.annotation.text.trim()`）。
 * Zotero 官方文档块里的示例也是拿 `params.annotation.text` 去翻译。
 *
 * ⚠ 不要退回"翻私有字段"的写法：`reader._iframeWindow.getSelection()` 虽然
 *   也能拿到（reader 是进程内 iframe），但那是下划线私有字段，Zotero 一升级
 *   就可能改名，而且失败时**没有任何提示**。第三个参数 `pluginID` 还能让
 *   Zotero 在插件卸载时自动摘掉监听。
 *
 * ## 这里只做"定位"，不做"检查"
 *
 * 选中 → 点我们的按钮 → 调 `/para-locate` 把文字匹配到段落 → 结果直接显示在
 * 弹出框里，并把这一段记进该文献的会话状态（`armedPara`），用户切回右侧
 * 「本地模型」窗格就能接着走逐段检测。为什么不在弹出框里直接跑检查：
 * 检查结论与"采用哪条修正"要放在能长期看、能勾选的地方（窗格），
 * 弹出框一关就没了。
 */
Object.assign(ZoteroKB, {

  registerReaderEvents: function () {
    var self = ZoteroKB;
    if (!Zotero.Reader || !Zotero.Reader.registerEventListener) {
      try {
        self.writeStatusFile({ readerEvents: "unavailable: Zotero.Reader 不存在" });
      } catch (e) { /* ignore */ }
      return;
    }
    try {
      Zotero.Reader.registerEventListener("renderTextSelectionPopup", (event) => {
        try {
          self.onReaderSelection(event);
        } catch (e) {
          Zotero.debug("[zotero-kb] 选中弹出框处理失败：" + e);
        }
      }, self.id);
      try {
        self.writeStatusFile({ readerEvents: "ok: renderTextSelectionPopup" });
      } catch (e) { /* ignore */ }
    } catch (e) {
      try {
        self.writeStatusFile({ readerEvents: "FAIL: " + e });
      } catch (e2) { /* ignore */ }
    }
  },


  /** 从 reader 事件里认出"这是哪一篇"，并把选中文字存下来。 */
  onReaderSelection: function (event) {
    var self = ZoteroKB;
    const { reader, doc, params, append } = event;
    const text = (((params || {}).annotation || {}).text || "").trim();
    if (!text) return;

    const key = self.readerItemKey(reader);
    // ⚠ 第一次收到选中时，把 params 的字段名写进状态文件 —— 这既是"探针"
    //   （确认 Zotero 这一版到底给了哪些字段，例如有没有 pageIndex），
    //   也免得以后有人凭记忆写字段名。
    if (!self._readerParamsLogged) {
      self._readerParamsLogged = true;
      try {
        self.writeStatusFile({
          readerParams: Object.keys(params || {}).join(","),
          readerAnnotationKeys: Object.keys((params || {}).annotation || {}).join(","),
        });
      } catch (e) { /* ignore */ }
    }
    if (key) {
      const st = self.chatOf(key);
      st.armedPara = {
        text: text,
        pageIndex: (params && (params.pageIndex !== undefined
          ? params.pageIndex : null)),
      };
    }

    // 往弹出框里加我们的按钮（那个框里已经有别的插件的按钮，故意做得小、
    // 带自己的 class，避免和它们抢样式）
    const btn = doc.createElement("button");
    btn.setAttribute("type", "button");
    btn.classList.add("zotero-kb-reader-btn");
    btn.style.cssText = "font-size:11px;padding:2px 6px;margin:2px;cursor:pointer;"
      + "border-radius:3px;border:1px solid rgba(128,128,128,.5);"
      + "background:transparent;color:inherit;";
    btn.textContent = "定位到这一段";
    btn.title = "在文献知识库里找到这段文字对应的段落（不替你猜：多段相像会让你选）";
    const out = doc.createElement("div");
    out.style.cssText = "font-size:11px;opacity:.85;margin:2px;white-space:pre-wrap;";
    btn.addEventListener("click", () => {
      self.readerLocate(btn, out, text, key);
    });
    append(btn);
    append(out);
  },


  /** 从 reader 实例推出"这是哪一篇文献"的 key（附件 → 父条目）。 */
  readerItemKey: function (reader) {
    try {
      const attID = reader && reader.itemID;
      if (!attID) return "";
      const att = Zotero.Items.get(attID);
      if (!att) return "";
      const parent = att.parentItem
        || (att.parentItemID ? Zotero.Items.get(att.parentItemID) : null);
      return (parent && parent.key) || att.key || "";
    } catch (e) {
      return "";
    }
  },


  /** 定位：调 /para-locate，把结果直接显示在弹出框里。 */
  readerLocate: async function (btn, out, text, key) {
    var self = ZoteroKB;
    if (!key) {
      out.textContent = "认不出这是哪一篇文献（附件没有父条目？）";
      return;
    }
    btn.disabled = true;
    out.textContent = "正在匹配…";
    try {
      const res = await self.request("POST", "/para-locate", { key: key, text: text });
      if (!res || !res.ok) {
        out.textContent = "定位失败：" + ((res && res.error) || "未知错误");
        return;
      }
      if (res.best) {
        out.textContent = "定位到 p." + res.best.page + " 第 "
          + (res.best.logical_index + 1) + " 段。\n打开右侧「本地模型」窗格可以继续检查这一段。";
      } else if (res.matches && res.matches.length) {
        // 多候选**列出来让用户选**，不猜
        out.textContent = "有 " + res.matches.length + " 段都像：\n"
          + res.matches.slice(0, 4).map((m) => "· p." + m.page + " 第 "
            + (m.logical_index + 1) + " 段（" + Math.round(m.score * 100) + "%）").join("\n");
      } else {
        out.textContent = "没找到对得上的段落（这段可能不在正文里，例如是页眉/图注）。";
      }
    } catch (e) {
      out.textContent = "定位失败：" + e;
    } finally {
      btn.disabled = false;
    }
  },
});
