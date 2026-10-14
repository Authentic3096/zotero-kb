/* ============================================================================
 * admin.js —— 知识库**管理页面**的壳（2026-10-13 把 Tk 面板搬进 Zotero）
 *
 * 为什么这么做：Tk 面板每次打开都要付"起一个 Python 进程 + Tk 初始化"约 0.5s
 * 的冷启动税（实测：Python 91ms + import tkinter 45ms + Tk() 345ms），再加建控件
 * 的 1.5s。搬进 Zotero 的 XHTML 页面后**没有独立进程**，打开即页面，用 CSS 做
 * "简约干净"也比 Tk 容易。设计令牌与 `tools/panels/theme.py（已退役 2026-10-14）` 同一套（见 admin.css）。
 *
 * ## 三条架构约定（改之前先读）
 *
 * 1. **页面不直接 fetch 127.0.0.1:8765**。服务端**故意**在带 Origin 的请求上不给
 *    token（防恶意网页偷 token），而本机服务是唯一的数据来源。所以：页面 →
 *    `postMessage` 给**宿主半** → 宿主半用 `Zotero.HTTP`（带 token）去取 → 回传。
 *    这与侧栏列（`content/kbview.js` ↔ `src/20-kbview.js`）**同一套模式**，
 *    别自己发明第二条通道。
 * 2. **一个页面一个文件**：`content/admin-pages/<id>.js`，各自调
 *    `KBAdmin.page({...})` 注册。壳只负责导航/取数/进度/日志，页面只管画自己的。
 *    这样多个人（或 agent）并行改不同页不会撞车。
 * 3. **长任务走 `/jobs/<id>` 轮询**（服务既有约定），不要自己去猜进度。
 *    用 `KBAdmin.runTask(...)` 一把梭：提交 → 轮询 → 画进度条 → 完成/失败回调。
 * ==========================================================================*/
(function () {
  "use strict";

  const PAGES = [];              // 注册进来的页面（按菜单顺序）
  const byId = {};
  let pending = new Map();       // seq → {resolve, reject, timer}
  let seq = 0;

  // ---------------------------------------------------------------- 桥接
  //
  // 页面 ↔ 宿主半。消息形状固定：
  //   页面 → 宿主：{ __kba: 1, seq, op, args }
  //   宿主 → 页面：{ __kba: 1, seq, ok, data | error }
  // 宿主半的实现见 `zotero-plugin/src/23-admin.js`（新文件）。
  const HOST_TIMEOUT = 30000;
  /**
   * 调宿主半。
   * `timeoutMs` 可覆盖默认 30s —— **慢动作必须显式给**：例如「试跑提示词」
   * （`POST /prompts/probe`，模型要跑几十秒）用默认值会在半路被判超时，
   * 而请求其实还在跑（2026-10-13 env/prompts 页的作者发现）。
   */
  function call(op, args, onProgress, timeoutMs) {
    const waitMs = timeoutMs || HOST_TIMEOUT;
    return new Promise((resolve, reject) => {
      const id = ++seq;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("宿主半没有回应（" + op + " 超过 " +
                         (waitMs / 1000) + "s）"));
      }, waitMs);
      pending.set(id, { resolve, reject, timer, onProgress });
      try {
        window.parent.postMessage({ __kba: 1, seq: id, op: op, args: args || {} },
                                 "*");
      } catch (e) {
        clearTimeout(timer);
        pending.delete(id);
        reject(e);
      }
    });
  }

  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || d.__kba !== 1 || !d.seq) return;
    const slot = pending.get(d.seq);
    if (!slot) return;
    // ⚠ 进度事件：同一 seq 会来**多条**，它既不是成功也不是失败 ——
    //   第一版只判 ok/data，于是 `{event:"progress"}` 走 else 分支把**整个请求
    //   reject 掉**（长任务永远"失败"，进度也显示不出来）。这里必须先拦下：
    //   不清超时、不删挂起项，只把 data 交给回调。
    if (d.event === "progress") {
      try {
        if (typeof slot.onProgress === "function") slot.onProgress(d.data || {});
      } catch (e) { /* 回调自己抛错不能影响请求 */ }
      return;
    }
    clearTimeout(slot.timer);
    pending.delete(d.seq);
    if (d.ok) slot.resolve(d.data);
    else slot.reject(new Error(d.error || "宿主半返回失败"));
  });

  // ⚠ 第 4 个参数是超时（毫秒），**慢动作必须给**（见 call 的注释）。
  const api = (method, path, body, onProgress, timeoutMs) =>
    call("http", { method, path, body }, onProgress, timeoutMs);
  const get = (path, onProgress, timeoutMs) =>
    api("GET", path, null, onProgress, timeoutMs);
  const post = (path, body, onProgress, timeoutMs) =>
    api("POST", path, body || {}, onProgress, timeoutMs);

  // ---------------------------------------------------------------- 日志
  const logEl = () => document.getElementById("log");
  function logLine(text, kind) {
    const el = logEl();
    if (!el) return;
    const div = document.createElement("div");
    if (kind) div.className = kind;
    div.textContent = text;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
    while (el.childElementCount > 500) el.removeChild(el.firstChild);
  }
  function log(text, kind) { logLine("[" + stamp() + "] " + text, kind); }
  function stamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }
  /** 按内容判断级别（与 Tk 侧 base.py 的日志着色同一套判据）。 */
  function logAuto(text) {
    const bad = /\[XX\]|Error|错误|失败|✗|Traceback|不能|拒绝/;
    const warn = /⚠|注意|缺|未|跳过/;
    log(text, bad.test(text) ? "err" : (warn.test(text) ? "warn" : ""));
  }

  // ---------------------------------------------------------------- 长任务
  /**
   * 提交一个长任务并轮询到结束。
   *   runTask({submit: () => post("/jobs", {...}), path: (id) => "/jobs/" + id,
   *            onTick: (st) => …, interval: 1000})
   * 服务端既有约定：POST /jobs 返回 {job: {id}}；GET /jobs/<id> 返回
   * {job: {state, percent, message, result, error}}（字段名以服务端为准，
   * 这里做了宽松取值，服务端字段若有出入只改这一处）。
   */
  async function runTask(opts) {
    const hook = opts.onTick || (() => {});
    let id = opts.id;
    if (!id) {
      const res = await opts.submit();
      id = (res && res.job && res.job.id) || (res && res.id) || res;
      id = String(id).replace(/^"|"$/g, "");
    }
    const path = opts.path ? opts.path(id) : "/jobs/" + id;
    const iv = opts.interval || 1000;
    for (;;) {
      const st = await get(path);
      const job = (st && st.job) || st || {};
      hook(job);
      const state = String(job.state || job.status || "");
      if (state === "done" || state === "ok" || state === "finished") {
        return job;
      }
      if (state === "failed" || state === "error" || state === "aborted") {
        throw new Error(job.error || job.message || "任务失败");
      }
      await sleep(iv);
    }
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------------------------------------------------------------- 导航
  function page(id) { return byId[id]; }

  /**
   * 页面容器：**所有渲染过的页都常驻在 #view 里，切页只切显隐**。
   *
   * 用户 2026-10-13 两次反馈，这里是第二版：
   *   ① 「加载过的数据应该缓存起来，关闭面板才清空」；
   *   ② 「切出再切入还是会有明显的迟滞感，就像重新读取了一遍；反复点当前页还会
   *      出现重复的『开始读 MinerU 状态』日志」。
   *
   * 第一版是"切走时把 DOM 从 #view 摘下来、切回来再挂回去" —— 那有两个毛病：
   *   · **摘挂会强制重排/重绘整棵子树**（PDF 解析页 94 行表格尤其明显），
   *     观感就是"又读了一遍"；
   *   · 页面**还在渲染中**时 `pageCache` 里还没有东西，于是再点一次（或切回切出）
   *     会**从头再渲染一遍** —— 日志里那一串重复的"开始读…"就是这么来的。
   * 现在改成：
   *   · 每页一个 `box` 常驻 `#view`，切页只 `hidden = true/false` —— 不进 DOM 树
   *     增删，没有重排代价；内层滚动位置也天然保留；
   *   · `promise` 记住"这一页正在渲染"，**重复点同一页/切回来不会重复发起**；
   *   · 渲染成功标记 `done`，此后永久直出（页面自己的「刷新」按钮才是更新入口）。
   *
   * ⚠ 这些都活在页面 JS 内存里，**面板窗口一关就随 window 一起没了** —— 正是用户
   *   要的"关闭面板才清空"，不需要持久化，也不需要显式 clear。
   * ⚠ 渲染**失败**时把盒子和错误条留下给用户看，但**清掉 done/promise**，所以下次
   *   切回来会重试（否则那份坏页面会一直被当成"已缓存"）。
   */
  const pageState = new Map();      // id -> { box, promise, done }

  function ensureBox(id) {
    let st = pageState.get(id);
    if (st) return st;
    const box = document.createElement("div");
    box.className = "page-body";
    box.hidden = true;
    const view = document.getElementById("view");
    if (view) view.appendChild(box);
    st = { box: box, promise: null, done: false };
    pageState.set(id, st);
    return st;
  }

  async function show(id) {
    const p = byId[id];
    if (!p) return;
    document.querySelectorAll("#nav button").forEach((b) => {
      if (b.dataset.page === id) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    document.title = p.title + " · 知识库";
    const h1 = document.querySelector("#topbar h1");
    if (h1) h1.textContent = p.title;

    // 切换显隐（不摘不挂）：先把所有页藏起来，再亮这一页。
    for (const st of pageState.values()) st.box.hidden = true;
    const st = ensureBox(id);
    st.box.hidden = false;

    if (st.done || st.promise) return st.promise;   // 已渲染 / 正在渲染 → 直出

    st.promise = Promise.resolve()
      .then(() => {
        // ⚠ `ctx` 上要把**工具函数本身**摊平（`ctx.el` 是函数、不是装着函数的对象）：
        //   原来写的是 `el: helpers`（一个对象），页面里 `const { el } = ctx` 之后
        //   `el(...)` 直接抛 "el is not a function" —— 整页渲染失败，而外层 catch
        //   只显示一行红字，看着像"页面没做"（2026-10-13 预览回路抓到）。
        return p.render(st.box, Object.assign({}, helpers, {
          api, get, post, call, runTask, log, logAuto, page,
        }));
      })
      .then(() => { st.done = true; })
      .catch((e) => {
        const note = document.createElement("div");
        note.className = "note note-err";
        note.textContent = "这一页渲染失败：" + (e && e.message ? e.message : e);
        st.box.appendChild(note);
        log("页面 " + id + " 渲染失败：" + e, "err");
      })
      .then(() => { st.promise = null; });
    return st.promise;
  }

  // ---------------------------------------------------------------- 自绘控件
  //
  // ⚠ **为什么这里要自己画下拉**：Zotero 的 <browser> 里**原生 <select> 的弹层
  //   出不来**（2026-10-13 真机实测：DOM/CSS/命中全正常、窗口级 XUL 弹层也正常，
  //   但那个面板浏览器的 select 弹层就是不显示；证据见交接报告）。于是：
  //     · 选项 ≤ 4 个 → 并排按钮组（`.btn-group`，选中态 aria-pressed + .is-on）；
  //     · 选项多（模型列表、分类列表）→ 这里的自绘下拉 `select()`。
  //   **页面里不许再出现原生 `<select>`**。

  /**
   * 弹层骨架：一个按钮 + 一个浮层，只负责"开关 / 点外面关 / Esc 关"三件事。
   * `select()` 与 `popmenu()` 都建在它上面 —— 各写一份必然漂。
   */
  function attachPopup(btn, pop) {
    function close() {
      if (pop.classList.contains("hidden")) return;
      pop.classList.add("hidden");
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("click", onDoc, true);
      document.removeEventListener("keydown", onKey, true);
    }
    function open() {
      if (!pop.classList.contains("hidden")) return;
      pop.classList.remove("hidden");
      btn.setAttribute("aria-expanded", "true");
      // ⚠ 用**捕获**阶段：浮层里的按钮自己 stopPropagation 之外，其余任何地方
      //   点一下都要关（包括点另一个自绘下拉）—— 冒泡阶段会被页面别处的
      //   处理器抢先 stopPropagation 而漏关。
      document.addEventListener("click", onDoc, true);
      document.addEventListener("keydown", onKey, true);
    }
    function onDoc(ev) {
      if (pop.contains(ev.target) || btn === ev.target || btn.contains(ev.target)) return;
      close();
    }
    function onKey(ev) {
      if (ev.key !== "Escape") return;
      ev.stopPropagation();
      close();
      btn.focus();
    }
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (pop.classList.contains("hidden")) open();
      else close();
    });
    return { open: open, close: close, isOpen: () => !pop.classList.contains("hidden") };
  }

  /**
   * 自绘下拉 / 可输入下拉（combobox）。形状就是
   * `ctx.select(label, options, value, onChange, opts)`：
   *   const sel = select("服务商", [{value:"ollama", text:"本地 Ollama"}], 当前值, fn);
   *   host.appendChild(sel);      // 它本身就是个 <div>，直接挂
   *   sel.value                   // 读 / 写当前值（可输入模式下读的是输入框里的实时值）
   *   sel.setOptions(list)        // 换选项（当前值还在新列表里就保留，否则见下）
   *   sel.setEditable(true)       // 切成"可输入"：多一个文本框，能填列表里没有的值
   *   sel.setDisabled(true)       // 禁用（按钮置灰、点不开）
   *   sel.getOptions()            // 当前选项（副本）
   *   sel.input                   // 那个 <input>（可输入模式才有意义）
   *
   * `options` 每项是 {value, text, title?}（也接受裸字符串）；`label` 画成
   * 按钮里的前缀（"服务商：ollama"），不传就只显示值。
   * `opts` = {editable, placeholder, title}。
   *
   * ⚠ 两种模式下"当前值不在新列表里"的处理**故意不同**：
   *   · 普通下拉：落到第一项（列表是全部合法取值）；
   *   · 可输入下拉：**保留用户打进去的字符串** —— 外部 API 的 model id 本来就不在
   *     任何本地列表里，跟着列表刷新被清掉是最致命的（用户白填）。
   */
  function select(label, options, value, onChange, opts) {
    const o = opts || {};
    const node = el("div", { class: "sel" });
    const cap = el("span", { class: "sel-cap" });
    const val = el("span", { class: "sel-val" });
    const btn = el("button", { type: "button", class: "sel-btn",
                               "aria-haspopup": "listbox",
                               "aria-expanded": "false" });
    btn.appendChild(cap);
    btn.appendChild(val);
    btn.appendChild(el("span", { class: "sel-caret", text: "▾" }));
    // 可输入模式用的文本框：与"值显示"共用同一个值，但**不**跟着每次按键重画
    //（重画会把人家的光标顶到末尾，打字体验立刻废掉）。
    const box = el("input", { type: "text", class: "sel-input hidden" });
    if (o.placeholder) box.placeholder = String(o.placeholder);
    const pop = el("div", { class: "sel-pop hidden", role: "listbox" });
    node.appendChild(box);
    node.appendChild(btn);
    node.appendChild(pop);
    const api = attachPopup(btn, pop);

    let list = [];
    let cur = value == null ? "" : String(value);
    let off = false;
    let edit = !!o.editable;

    function textOf(v) {
      for (const it of list) {
        if (it.value === String(v)) return it.text;
      }
      return String(v == null ? "" : v);
    }
    function paint() {
      cap.textContent = label ? (String(label) + "：") : "";
      cap.classList.toggle("hidden", !label || edit);
      val.textContent = textOf(cur);
      val.title = textOf(cur);
      val.classList.toggle("hidden", edit);
      box.classList.toggle("hidden", !edit);
      if (edit && document.activeElement !== box) box.value = cur;
      // 可输入模式下按钮只剩个箭头，title 要说清它还能展开候选
      btn.title = edit ? "展开候选（也可以直接在左边输入）" : (o.title || "");
      box.title = edit ? (o.title || "") : "";
      btn.disabled = off;
      box.disabled = off;
      btn.setAttribute("aria-disabled", off ? "true" : "false");
      pop.textContent = "";
      list.forEach((it) => {
        const item = el("button", { type: "button", class: "sel-opt", role: "option",
                                    text: it.text });
        if (it.title) item.title = it.title;
        if (it.value === cur) {
          item.classList.add("is-on");
          item.setAttribute("aria-selected", "true");
        }
        item.addEventListener("click", (ev) => {
          ev.stopPropagation();
          cur = it.value;
          api.close();
          paint();
          if (typeof onChange === "function") onChange(cur);
        });
        pop.appendChild(item);
      });
    }
    function setOptions(list2) {
      list = (list2 || []).map((it) => {
        if (it && typeof it === "object") {
          return { value: String(it.value == null ? "" : it.value),
                   text: String(it.text == null ? it.value : it.text),
                   title: it.title == null ? "" : String(it.title) };
        }
        return { value: String(it), text: String(it), title: "" };
      });
      if (!list.some((it) => it.value === cur) && !edit) {
        cur = list.length ? list[0].value : "";
      }
      paint();
    }
    // ⚠ 只在"值真的会变"的两种时机调 onChange：选一项、或输入框里改了内容。
    //   打字时**不重画**（见 box 上的注释）。
    box.addEventListener("input", () => {
      cur = box.value;
      if (typeof onChange === "function") onChange(cur);
    });
    box.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); api.close(); }
    });
    Object.defineProperty(node, "value", {
      get: () => (edit ? box.value : cur),
      set: (v) => {
        cur = v == null ? "" : String(v);
        box.value = cur;
        paint();
      },
    });
    node.input = box;
    node.setOptions = setOptions;
    node.setEditable = (on) => { edit = !!on; paint(); };
    node.isEditable = () => edit;
    node.setDisabled = (on) => { off = !!on; paint(); };
    node.getOptions = () => list.slice();
    setOptions(options);
    return node;
  }

  // ---------------------------------------------------------------- 小工具
  const el = (tag, attrs, kids) => {
    const n = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        if (k === "text") n.textContent = attrs[k];
        else if (k === "class") n.className = attrs[k];
        else if (k === "html") n.innerHTML = attrs[k];
        else if (k === "on") { for (const ev of Object.keys(attrs.on)) n.addEventListener(ev, attrs.on[ev]); }
        else n.setAttribute(k, attrs[k]);
      }
    }
    for (const c of (kids || [])) {
      if (c == null) continue;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return n;
  };
  let dialogEl = null;
  function dialog(title, body, actions) {
    if (dialogEl) dialogEl.remove();
    dialogEl = el("dialog");
    dialogEl.appendChild(el("h2", { text: title }));
    dialogEl.appendChild(typeof body === "string"
      ? el("div", { text: body }) : body);
    const bar = el("div", { class: "actions" });
    (actions || [{ text: "关闭" }]).forEach((a) => {
      bar.appendChild(el("button", {
        class: "btn " + (a.primary ? "btn-primary" : (a.danger ? "btn-danger" : "")),
        text: a.text,
        on: { click: () => { if (!a.run || a.run() !== false) dialogEl.close(); } },
      }));
    });
    dialogEl.appendChild(bar);
    document.body.appendChild(dialogEl);
    dialogEl.showModal();
    return dialogEl;
  }
  /**
   * 表格：`columns` = [{key, label, width, align, mono, tdClass, render(row),
   *                    sortable, sortValue(row)}]
   *
   * `opts`：
   *   · `onRowClick(row, tr)` —— 点行回调；
   *   · `sort: {key, dir}`    —— 初始按哪一列排（列要有 `sortable: true`）；
   *   · `rowKey(row)`         —— 给每行挂 `data-key`（页面据此找回自己的行，
   *                             排序重建 tbody 之后也找得到）；
   *   · `rowClass(row)`       —— 每次重画都重新算的行 class（选中态靠它跨排序保留）；
   *   · `onSort(key, dir)`    —— 排序变化时通知页面（**只给客户端分页的表用**：
   *                             那种表要把**全量**重排再切页，见 advanced.js 权重榜）。
   *
   * 排序能力**只加在这一处**（页面别自己造一套）：表头挂 `sortable: true` 就
   * 可点、再点一次升降切换，指示符是表头里的 ▲ / ▼ + `aria-sort`，比较用
   * `compare()`（数字按数比、其余按 `localeCompare("zh", {numeric})`）。
   *
   * ## 表头排序的统一口径（2026-10-14，各页照这一份做，别各写各的）
   *
   * 1. **可排序** = 值是"有序语义"、用户会想按它排的列：
   *      · 标识类：文献 / 标题 / key；
   *      · 数值类：节数 / 页数 / 图 / 大小 / 权重 / 得分 / 坏切片 / 疑似 / 跳过……
   *        这类列必须给 `sortValue(row)` 返回**数字原值**，别把格式化后的字符串
   *        拿去排（"9.9 MB" 会排到 "64.0 MB" 后面）；
   *      · 时间类：日期 / 生成时间 / 解析时间（ISO 串本身可排）；
   *      · 枚举类：效果 / 来源 / 档位 / 能删吗 / 重点 / 纲要文件 / 状态。
   * 2. **不可排序** = 下面这几类（不给 `sortable`，表头**不加** cursor 与箭头，
   *    点了什么都不发生）：
   *      · 长自由文本：说明 / 备注 / 命中片段 / 问题·摘要 / 存放内容 —— 排它没有意义；
   *      · 复合文本：一格拼两种信息的列（结构页的「内容量」= "16,426 篇 / 6,179 切片"）；
   *      · 非数据列：操作列、勾选列、详情字段表的标签 / 值列；
   *      · **客户端分页的表**（高级页的权重榜）：默认要靠 `onSort` 把**全量**重排
   *        再切页；不做这一步的分页表干脆不给排序 —— 只排当前一页会看着像排错了。
   * 3. 默认排序：**不额外排**（保留引擎给的自然顺序），只有「分节纲要」页显式指定
   *    按文献升序（它的自然顺序是年份倒序，看不出排过）。点过表头才按那一列排。
   */
  function table(columns, rows, opts) {
    const wrap = el("div");
    const list = (rows || []).slice();
    const o = opts || {};
    let sortKey = (o.sort && o.sort.key) || "";
    let sortDir = (o.sort && o.sort.dir) || "asc";
    const head = el("tr");
    const tbody = el("tbody");

    const cellOf = (c, r) => {
      const v = c.render ? c.render(r) : (r[c.key] == null ? "" : String(r[c.key]));
      const cls = [c.mono ? "mono" : "", c.tdClass || ""].filter(Boolean).join(" ");
      const td = cls ? el("td", { class: cls }) : el("td");
      if (v && v.nodeType) td.appendChild(v); else td.textContent = v;
      if (c.align === "right") td.style.textAlign = "right";
      return td;
    };
    const paintBody = () => {
      tbody.textContent = "";
      list.forEach((r) => {
        const tr = el("tr");
        columns.forEach((c) => tr.appendChild(cellOf(c, r)));
        if (o.rowKey) tr.setAttribute("data-key", String(o.rowKey(r)));
        if (o.rowClass) {
          const rc = o.rowClass(r);
          if (rc) tr.className = rc;
        }
        if (o.onRowClick) tr.addEventListener("click", () => o.onRowClick(r, tr));
        tbody.appendChild(tr);
      });
    };
    const valueOf = (c, r) => {
      const v = c.sortValue ? c.sortValue(r) : r[c.key];
      if (v == null) return "";
      return Array.isArray(v) ? v.join("、") : v;
    };
    /**
     * 比较两个取值。
     *
     * ⚠ 数字列**必须按数比**。原来一律 `String(v)` 再 `localeCompare(..., {numeric})`：
     *   那个 `numeric` 只把「连续数字段」当数字，**小数点不是分隔符** —— 于是
     *   "0.5" 会被判成小于 "0.25"（5 < 25），权重 / 得分 / 秒数 / MB 这些小数列
     *   全排错，而且箭头看着"能排"、谁都想不到是排错了。现在两边都是 number
     *   就按数比（NaN 沉底），一边数字一边非数字时数字在前（"—" 这类空值沉底）。
     */
    const compare = (a, b) => {
      const na = typeof a === "number", nb = typeof b === "number";
      if (na && nb) {
        const fa = isFinite(a), fb = isFinite(b);
        if (!fa && !fb) return 0;
        if (!fa) return 1;
        if (!fb) return -1;
        return a - b;
      }
      if (na !== nb) return na ? -1 : 1;
      return String(a).localeCompare(String(b), "zh",
                                     { numeric: true, sensitivity: "base" });
    };
    const paintHead = () => {
      head.textContent = "";
      columns.forEach((c) => {
        const th = el("th", { text: c.label });
        if (c.sortable) {
          th.classList.add("th-sort");
          th.title = "点击按这一列排序（再点一次升降切换）";
          th.setAttribute("aria-sort", c.key === sortKey
            ? (sortDir === "asc" ? "ascending" : "descending") : "none");
          if (c.key === sortKey) {
            th.appendChild(el("span", { class: "arrow",
                                        text: sortDir === "asc" ? " ▲" : " ▼" }));
          }
          th.addEventListener("click", () => {
            if (sortKey === c.key) sortDir = (sortDir === "asc" ? "desc" : "asc");
            else { sortKey = c.key; sortDir = "asc"; }
            applySort();
          });
        }
        head.appendChild(th);
      });
    };
    const applySort = () => {
      const col = columns.filter((c) => c.key === sortKey)[0];
      if (col) {
        const sign = sortDir === "asc" ? 1 : -1;
        list.sort((a, b) => sign * compare(valueOf(col, a), valueOf(col, b)));
      }
      // ⚠ 顺序要在重画 body **之前**定下来：选中态是 rowClass 在重画时体现的。
      paintHead();
      paintBody();
      // 排序变了通知页面（客户端分页的表靠它重排**全量**再切页）。
      // ⚠ 页面必须在回调里判「真的变了」才动，否则重画 → 回调 → 重画会打转。
      if (typeof o.onSort === "function") o.onSort(sortKey, sortDir);
    };
    // ⚠ 先把 head/tbody 插进 DOM 再排：`applySort` 会调 `paintHead/paintBody`，
    //   它们只改元素内容，与元素在不在文档里无关，但这样结构一次性就位、少一次重排。
    wrap.appendChild(el("table", { class: "grid" }, [el("thead", null, [head]), tbody]));
    applySort();
    return wrap;
  }
  const helpers = { el, table, dialog, log, logAuto, select };

  /** 注册一个页面。`render(host, ctx)` 里画自己的东西。 */
  function registerPage(spec) {
    if (!spec || !spec.id) return;
    byId[spec.id] = spec;
    PAGES.push(spec);
  }

  // ---------------------------------------------------------------- 启动
  async function boot() {
    const nav = document.getElementById("nav");
    const brand = el("div", { class: "brand", text: "Zotero 文献知识库" });
    nav.appendChild(brand);
    PAGES.sort((a, b) => (a.order == null ? 100 : a.order) -
                         (b.order == null ? 100 : b.order));
    PAGES.forEach((p) => {
      nav.appendChild(el("button", {
        "data-page": p.id, text: p.title,
        on: { click: () => { location.hash = "#" + p.id; show(p.id); } },
      }));
    });
    el._pages = PAGES;

    // 顶部右侧：一句话状态（复用 /health，失败就如实说）
    const status = document.querySelector("#topbar .hint");
    get("/health").then((h) => {
      if (!status) return;
      const kb = (h && h.kb_dir) || "";
      status.textContent = "服务正常 · " + kb;
      status.title = kb;
    }).catch((e) => {
      if (status) status.textContent = "本机服务没连上：" + e.message;
      log("健康检查失败：" + e.message, "err");
    });

    const want = (location.hash || "").replace(/^#/, "");
    await show(byId[want] ? want : (PAGES[0] && PAGES[0].id));
  }

  window.KBAdmin = {
    page: registerPage, api, get, post, call, runTask, log, logAuto,
    show, el, table, dialog, select, boot, PAGES,
  };
  document.addEventListener("DOMContentLoaded", () => {
    // 各页脚本（admin-pages/*.js）在自己的文件里注册；等它们都执行完再 boot
    const t = setInterval(() => {
      if (document.readyState === "complete") {
        clearInterval(t);
        boot();
      }
    }, 30);
  });
})();
