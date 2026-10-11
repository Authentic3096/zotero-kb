/*
  kbview.js —— 「知识库」独占侧栏页面的**渲染与交互**（跑在 <browser> 里的 HTML 文档）

  与宿主的通信（两端都只用 JSON 字符串，避开跨 principal 传对象那一堆坑）：

    宿主 → 本页：直接调 window.kbvReceive(jsonText)
    本页 → 宿主：window.__kbvHostPost(jsonText)（宿主注入；没有就退回 postMessage）

  消息形状（type 字段区分）：
    render  {key,title,levels[],level,picked,fellBack,hint,html,textLength,isHtml,
             fulltext,pages{},edited[],stale[],empty,emptyWhy,probe,font,width}
    hint    {text}
    busy    {busy, text}

  本页 → 宿主的动作：
    {type:"selectLevel", level}
    {type:"reload"}
    {type:"exportDraft"}
    {type:"savePage", page, text}
    {type:"restorePage", page}
    {type:"setFont", font}
    {type:"openPath", path}
    {type:"close"}
    {type:"log", message}
*/
(function () {
  "use strict";

  var els = {};
  var st = {
    busy: false,
    fulltext: false,
    pages: {},          // 页码 -> 该页 markdown 源文本
    edited: {},
    stale: {},
    editing: {},        // 已经在编辑中的页码（避免重复插入编辑器）
    key: "",
    payload: null
  };

  // ------------------------------------------------------------ 与宿主通信

  function post(msg) {
    var text;
    try { text = JSON.stringify(msg); } catch (e) { return; }
    var sent = false;
    try {
      if (typeof window.__kbvHostPost === "function") {
        window.__kbvHostPost(text);
        sent = true;
      }
    } catch (e) { /* 落回下面那条路 */ }
    if (!sent) {
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage(msg, "*");
        }
      } catch (e2) { /* 两条路都不通就只能放弃 */ }
    }
  }

  function logIt(message) {
    post({ type: "log", message: String(message) });
  }

  // ------------------------------------------------------------ 小工具

  function setText(el, text) { if (el) el.textContent = text == null ? "" : String(text); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }

  function each(list, fn) {
    for (var i = 0; i < (list ? list.length : 0); i++) fn(list[i], i);
  }

  function fillSelect(sel, items, value) {
    if (!sel) return;
    sel.innerHTML = "";
    each(items, function (it) {
      var o = document.createElement("option");
      o.value = String(it.value);
      o.textContent = String(it.label);
      sel.appendChild(o);
    });
    if (value != null) sel.value = String(value);
  }

  // ------------------------------------------------------------ 正文渲染

  function clearPageEditors() {
    st.editing = {};
    var v = els.view;
    if (!v) return;
    var holders = v.querySelectorAll(".kbv-editor-holder");
    for (var i = 0; i < holders.length; i++) {
      if (holders[i].parentNode) holders[i].parentNode.removeChild(holders[i]);
    }
    var bs = v.querySelectorAll(".kbv-mini");
    for (var j = 0; j < bs.length; j++) bs[j].disabled = false;
  }

  function decorateImages() {
    var v = els.view;
    if (!v) return;
    var imgs = v.querySelectorAll("img");
    for (var i = 0; i < imgs.length; i++) {
      (function (img) {
        img.addEventListener("click", function () {
          // 宿主在改写 src 时把**绝对路径**写进了 data-kbv-path（图本身走
          // resource://zotero-kb-files/… 加载，路径解不回来）——点一下用系统
          // 默认程序打开原图。
          var p = String(img.getAttribute("data-kbv-path") || "");
          if (p) post({ type: "openPath", path: p });
        });
      })(imgs[i]);
    }
  }

  function pageBar(page) {
    var bar = el("div", "kbv-page-head");
    var tag = el("span", "kbv-tag", "p." + page);
    if (st.edited[page]) {
      tag.className = "kbv-tag kbv-edited";
      tag.textContent = "p." + page + " · 已修订";
    }
    if (st.stale[page]) {
      tag.textContent += " · 基于旧解析";
      tag.title = "这一页的解析结果变过，你改的是旧版 —— 仍用你的版本，请核对一下";
    }
    var editBtn = el("button", "kbv-mini", st.edited[page] ? "修改这一页" : "改这一页");
    editBtn.type = "button";
    var restoreBtn = el("button", "kbv-mini", "恢复原版");
    restoreBtn.type = "button";
    restoreBtn.disabled = !st.edited[page];
    var status = el("span", "kbv-status", "");
    bar.appendChild(tag);
    bar.appendChild(editBtn);
    bar.appendChild(restoreBtn);
    bar.appendChild(status);

    editBtn.addEventListener("click", function () {
      if (st.busy) return;
      toggleEditor(page, bar, editBtn, restoreBtn, status);
    });
    restoreBtn.addEventListener("click", function () {
      if (st.busy) return;
      setBusy(true, "正在恢复 p." + page + "…");
      post({ type: "restorePage", page: page });
    });
    return bar;
  }

  function toggleEditor(page, bar, editBtn, restoreBtn, status) {
    var host = bar.parentNode;
    if (!host) return;
    var existed = host.querySelector(".kbv-editor-holder");
    if (existed) { host.removeChild(existed); return; }
    var holder = el("div", "kbv-editor-holder");
    var ta = el("textarea", "kbv-editor");
    ta.value = st.pages[page] == null ? "" : String(st.pages[page]);
    var row = el("div", "kbv-editor-row");
    var save = el("button", "kbv-mini", "保存");
    save.type = "button";
    var cancel = el("button", "kbv-mini", "取消");
    cancel.type = "button";
    row.appendChild(save);
    row.appendChild(cancel);
    holder.appendChild(ta);
    holder.appendChild(row);
    bar.parentNode.insertBefore(holder, bar.nextSibling);
    ta.focus();
    save.addEventListener("click", function () {
      if (st.busy) return;
      save.disabled = true;
      cancel.disabled = true;
      setBusy(true, "正在保存 p." + page + "…");
      post({ type: "savePage", page: page, text: ta.value });
    });
    cancel.addEventListener("click", function () {
      if (holder.parentNode) holder.parentNode.removeChild(holder);
    });
  }

  function decoratePages() {
    var v = els.view;
    if (!v) return;
    // ⚠ 这里必须是服务端渲染出来的 `.kb-page`（见 offline/mdhtml 与 20-kbview.js 的
    //   md2html），**不是**我们自己的 `.kbv-*` 命名空间。第一版写成 `.kbv-page`，
    //   结果 4 个页块一个工具条都没挂上（真机实测：pages=4 / heads=0）。
    var blocks = v.querySelectorAll(".kb-page");
    for (var i = 0; i < blocks.length; i++) {
      (function (block) {
        var n = parseInt(block.getAttribute("data-kbpage"), 10);
        if (!n) return;
        block.insertBefore(pageBar(n), block.firstChild);
      })(blocks[i]);
    }
  }

  // ------------------------------------------------------------ 消息处理

  function setBusy(busy, text) {
    st.busy = !!busy;
    if (text != null) setText(els.hint, text);
    var buttons = [els.btnReload, els.btnDraft, els.btnClose];
    each(buttons, function (b) { if (b) b.disabled = !!busy && b !== els.btnClose; });
  }

  function renderEmpty(msg) {
    var v = els.view;
    v.innerHTML = "";
    v.appendChild(el("div", "kbv-empty", msg.emptyWhy || "知识库里还没有这一篇的任何分级文件。"));
    if (msg.probe) v.appendChild(el("div", "kbv-probe", msg.probe));
    v.appendChild(el("div", "kbv-empty", "先在管理面板「手动更新」建一次索引，"
      + "或用右键菜单「重建本条目知识库」。"));
  }

  function renderContent(msg) {
    var v = els.view;
    clearPageEditors();
    v.innerHTML = msg.html || "";
    if (!v.firstChild) {
      v.appendChild(el("div", "kbv-empty", "这一级的内容没渲染出来（"
        + (msg.textLength || 0) + " 字已读到）—— 请把这一行报给维护者。"));
      return;
    }
    decorateImages();
    if (msg.fulltext) decoratePages();
  }

  function render(msg) {
    st.payload = msg;
    st.key = msg.key || "";
    st.fulltext = !!msg.fulltext;
    st.pages = msg.pages || {};
    st.edited = {};
    st.stale = {};
    each(msg.edited, function (n) { st.edited[n] = true; });
    each(msg.stale, function (n) { st.stale[n] = true; });

    if (msg.levels) {
      fillSelect(els.level, msg.levels.map(function (lv) {
        return { value: lv.id, label: lv.label };
      }), msg.level);
    }
    if (msg.font) {
      els.font.value = String(msg.font);
      document.documentElement.style.setProperty("--kbv-font-size", String(msg.font) + "px");
    }
    els.btnDraft.hidden = !st.fulltext;

    if (msg.empty) renderEmpty(msg);
    else renderContent(msg);

    setBusy(!!msg.busy, msg.hint || "");
    if (!msg.busy) setText(els.hint, msg.hint || "");
  }

  // ------------------------------------------------------------ 接线

  function wire() {
    els.view = document.getElementById("kbv-view");
    els.hint = document.getElementById("kbv-hint");
    els.level = document.getElementById("kbv-level");
    els.font = document.getElementById("kbv-font");
    els.btnDraft = document.getElementById("kbv-draft");
    els.btnReload = document.getElementById("kbv-reload");
    els.btnClose = document.getElementById("kbv-close");

    els.level.addEventListener("change", function () {
      post({ type: "selectLevel", level: els.level.value });
    });
    els.font.addEventListener("change", function () {
      var px = parseInt(els.font.value, 10) || 13;
      document.documentElement.style.setProperty("--kbv-font-size", px + "px");
      post({ type: "setFont", font: px });
    });
    els.btnDraft.addEventListener("click", function () { post({ type: "exportDraft" }); });
    els.btnReload.addEventListener("click", function () { post({ type: "reload" }); });
    els.btnClose.addEventListener("click", function () { post({ type: "close" }); });

    // 兜底：宿主若走 postMessage 那条路
    window.addEventListener("message", function (ev) {
      var d = ev && ev.data;
      if (!d || typeof d !== "object" || typeof d.type !== "string") return;
      if (d.kb !== "zotero-kb" && !d.__kbv) return;
      handle(d);
    });
  }

  function handle(msg) {
    try {
      if (!msg || typeof msg.type !== "string") return;
      if (msg.type === "render") render(msg);
      else if (msg.type === "hint") setText(els.hint, msg.text);
      else if (msg.type === "busy") setBusy(!!msg.busy, msg.text);
    } catch (e) {
      setText(els.hint, "渲染出错：" + e);
      logIt("render error: " + e);
    }
  }

  // 宿主调这个（JSON 字符串，见文件头）
  window.kbvReceive = function (jsonText) {
    var msg = null;
    try { msg = JSON.parse(String(jsonText)); } catch (e) { msg = null; }
    if (msg) handle(msg);
  };

  // 供宿主判断"页面脚本真的跑起来了"
  window.kbvPageReady = true;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
