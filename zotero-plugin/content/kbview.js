/*
  kbview.js —— 「知识库」侧栏页面的**渲染与交互**（跑在 <browser> 里的 HTML 文档）

  同一份脚本服务两处：条目窗格那一列、Zotero 阅读器右侧那一列。

  与宿主的通信（两端都只用 JSON 字符串，避开跨 principal 传对象那一堆坑）：

    宿主 → 本页：直接调 window.kbvReceive(jsonText)
    本页 → 宿主：window.__kbvHostPost(jsonText)（宿主注入；没有就退回 postMessage）

  消息形状（宿主 → 本页）：
    render {key,title,levels[],level,picked,fellBack,hint,font,empty,emptyWhy,probe,
            fulltext,order[],pages{n:{prefix,blocks[],seps[],suffix}},md,
            imgBase,page,edited[],stale[],busy}
    hint   {text}
    busy   {busy,text}
    font   {font}                 // 面板改了字号 → 已打开的列立刻换（10~20）

  本页 → 宿主的动作：
    {type:"selectLevel", level}
    {type:"reload"}
    {type:"exportDraft"}
    {type:"savePage", page, text}       整页源码模式：直接交整页 markdown
    {type:"saveBlocks", page, parts}    按块编辑：交回 parts（宿主负责拼回整页）
    {type:"restorePage", page}
    {type:"openImage", rel}             点图 → 宿主用系统默认程序打开原图
    {type:"close"}
    {type:"log", message}

  ## 渲染：markdown-it + KaTeX（都 vendored 在 content/vendor/，不放 CDN）

  为什么在**页面里**渲染而不是吃服务端的 .html 孪生（那是 v1.10.0 的做法）：
    · 服务端那套走 latex2mathml，`\begin{aligned}` 会被转成一堆 `&` 与空 fence
      （实测：矩阵/对齐环境基本报废）；KaTeX 原生支持 aligned/矩阵，这是
      「右栏公式是真分式/上下标」这条验收的关键；
    · 逐块编辑要**实时预览**，而服务端 HTML 拿不回来"改了一半"的正文；
    · 页面里有真 document，能跑脚本（XUL 窗格里 innerHTML 注入的 <script> 不执行）。

  KaTeX 的字体在同目录 vendor/katex/fonts/ 下（**必须一起拷**：只带 js/css
  会静默掉字、不报错 —— 派发包 §六 明确点过这条）。
*/
(function () {
  "use strict";

  var els = {};
  var mdRenderer = null;

  var st = {
    busy: false,
    fulltext: false,
    order: [],          // 页顺序 [1,2,...]
    pages: {},          // "3" -> {prefix, blocks[], seps[], suffix}
    edited: {},
    stale: {},
    key: "",
    page: 0,            // 阅读器当前页（宿主推来的，1 起）
    imgBase: "",
    payload: null,
    editor: null        // {page, holder, mode, rows[], source}
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

  function setText(e, text) { if (e) e.textContent = text == null ? "" : String(text); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /** 属性值转义：esc() 不管引号，塞进 title="…" 之前必须再挡一层。 */
  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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

  // ------------------------------------------------------------ 公式

  /**
   * 一段 LaTeX → KaTeX 的 HTML。**转不了就退回等宽原文**（绝不抛、绝不空白）。
   *
   * ⚠ throwOnError 必须是 **true**：给 false 时 KaTeX 会把**整段 LaTeX 原文**
   *   染红塞进一个 `<span class="katex-error">`，而 MinerU 抓坏的长公式动辄上千
   *   字符（本机最长 3 KB），渲染出来是一大片红字 —— 看着像我们排错了。
   *   自己接住 ParseError、退回与 MathML 那边一致的等宽原文，并标
   *   `kbv-tex-fail`。真库实测（1314 个块公式）：3 个是**源 LaTeX 本身坏了**
   *   （环境不配对 / 公式被截断，任何渲染器都救不回来），1 个是 latex2mathml
   *   的局限（嵌套 cases/aligned）而 KaTeX 反而能排。
   */
  function mathHtml(tex, display) {
    var src = String(tex == null ? "" : tex).trim();
    if (!src) return "";
    if (window.katex && window.katex.renderToString) {
      try {
        return window.katex.renderToString(src, {
          displayMode: !!display,
          throwOnError: true,
          strict: "ignore",
          trust: false
        });
      } catch (e) {
        logIt("katex: " + e);
        return '<code class="kbv-tex kbv-tex-fail" title="'
          + escAttr("公式未能排版（下面是原始 LaTeX）："
                    + ((e && e.message) ? e.message : e))
          + '">' + esc(src) + "</code>";
      }
    }
    return '<code class="kbv-tex">' + esc(src) + "</code>";
  }

  // ------------------------------------------------------------ Markdown

  /**
   * 拿到（并缓存）markdown-it 实例，挂上三条自己的规则：
   *   · block  $$…$$ / \[…\] 独占一行 → 块级公式
   *   · inline $$…$$ / $…$ / \(…\) / \[…\] → 行内公式
   *   · image  相对路径 → 前缀 imgBase（宿主给的知识库 resource:// 前缀）
   *
   * ⚠ 行内规则要插在 escape **之前**：否则 \[ 会被 escape 规则先吃掉变成 [。
   * ⚠ 不能只做「先正则抽公式、再喂 markdown-it」：抽出来的占位符会被
   *   emphasis/escape 规则搅坏，而公式里 \* \_ [ ] 全是常见字符。
   */
  function getMd() {
    if (mdRenderer) return mdRenderer;
    if (typeof window.markdownit !== "function") return null;
    var m = window.markdownit({
      html: false,        // 正文是我们自己生成的 md，不放行内 HTML（更安全）
      linkify: false,
      breaks: true,       // 与服务端渲染器一致：段内单换行 → <br>
      typographer: false
    });

    m.block.ruler.before("fence", "kbv_block_math",
      function (state, startLine, endLine, silent) {
        var pos = state.bMarks[startLine] + state.tShift[startLine];
        var max = state.eMarks[startLine];
        var line = state.src.slice(pos, max);
        var content = null, lastLine = startLine;
        var mm = /^\s*\$\$([\s\S]+?)\$\$\s*$/.exec(line)
              || /^\s*\\\[([\s\S]+?)\\\]\s*$/.exec(line);
        if (mm) {
          content = mm[1];
        } else {
          // 跨行的 $$ … $$ / \[ … \]：从这一行一直找到收尾那一行。
          // ⚠ 真库里 94 篇只有 0 篇是跨行的（1314 个公式全在一行内），但一旦
          //   出现跨行，只认单行会让整块退化成普通段落、公式原文裸露 —— 所以补上。
          var t = line.replace(/^\s+/, "");
          var head = t.slice(0, 2);
          var close = (head === "$$") ? "$$" : (head === "\\[" ? "\\]" : "");
          if (close) {
            var rest = t.slice(2);
            var ci = rest.lastIndexOf(close);
            if (ci >= 0) {
              content = rest.slice(0, ci);
            } else {
              var buf = [rest];
              for (var L = startLine + 1; L < endLine; L++) {
                var lt = state.src.slice(state.bMarks[L] + state.tShift[L],
                                        state.eMarks[L]);
                var cj = lt.lastIndexOf(close);
                if (cj >= 0) {
                  buf.push(lt.slice(0, cj));
                  lastLine = L;
                  content = buf.join("\n");
                  break;
                }
                buf.push(lt);
              }
            }
          }
        }
        if (content == null) return false;
        if (silent) return true;
        var token = state.push("kbv_block_math", "math", 0);
        token.content = content;
        token.map = [startLine, lastLine + 1];
        state.line = lastLine + 1;
        return true;
      },
      // ⚠ 必须登记成**段落终止规则**：正文里「公式行紧跟在正文行后面、中间没空行」
      //   时，paragraph 规则会一路把它吞掉，公式就只剩行内排版（挤成一行）。
      //   silent=true 时本规则只回答「这里是不是公式」、不动 state.line —— 这正是
      //   终止规则检查的契约（见 markdown-it ParserBlock 的 terminatorRules）。
      { alt: ["paragraph", "reference", "blockquote", "list"] });
    m.renderer.rules.kbv_block_math = function (tokens, idx) {
      return '<div class="kbv-mathblock">'
        + mathHtml(tokens[idx].content, true) + "</div>\n";
    };

    m.inline.ruler.before("escape", "kbv_inline_math", function (state, silent) {
      var src = state.src, pos = state.pos, max = state.posMax;
      var kind = "", open = 0, close = "";
      if (src.slice(pos, pos + 2) === "$$") {
        kind = "dollar2"; open = 2; close = "$$";
      } else if (src.charAt(pos) === "$") {
        kind = "dollar1"; open = 1; close = "$";
      } else if (src.slice(pos, pos + 2) === "\\[") {
        kind = "bracket"; open = 2; close = "\\]";
      } else if (src.slice(pos, pos + 2) === "\\(") {
        kind = "paren"; open = 2; close = "\\)";
      } else {
        return false;
      }
      var end = src.indexOf(close, pos + open);
      if (end < 0 || end > max) return false;
      var tex = src.slice(pos + open, end);
      if (!tex) return false;
      if (kind === "dollar1" && tex.indexOf("\n") >= 0) return false;
      // ⚠ **无论 silent 与否都要推进 state.pos**。markdown-it 的 state.skipToken()
      //   会用 silent=true 把内联规则过一遍，并在「返回 true 却没推进」时抛
      //   "inline rule didn't increment state.pos"；一旦抛，**整页 md 的渲染
      //   会整体失败**（本机实测：一页里同时有粗体/链接与公式时，整页退化成
      //   一个大 <p> 纯文本 —— 换行被 HTML 折成一个空格、图片与公式全成原文）。
      //   内置规则（backticks / markdown-it-katex）都是这么写的：推进放在
      //   if (!silent) 之外。
      state.pos = end + close.length;
      if (silent) return true;
      var token = state.push("kbv_inline_math", "math", 0);
      token.content = tex;
      token.display = (kind === "bracket");
      return true;
    });
    m.renderer.rules.kbv_inline_math = function (tokens, idx) {
      var t = tokens[idx];
      return mathHtml(t.content, !!t.display);
    };

    var defaultImage = m.renderer.rules.image;
    m.renderer.rules.image = function (tokens, idx, options, env, self) {
      var token = tokens[idx];
      var src = token.attrGet("src") || "";
      // 相对路径（images/xxx.jpg，相对 <kb>/mineru/<key>/）才改写；
      // 已经是协议绝对地址的原地不动（幂等）。
      if (src && !/^[a-z][a-z0-9+.\-]*:|^\/\//i.test(src)) {
        var base = (env && env.imgBase) || "";
        token.attrSet("data-kbv-src", src);
        if (base) {
          token.attrSet("src", base + String(src).replace(/\\/g, "/").replace(/^\.\//, ""));
        }
      }
      token.attrSet("loading", "lazy");
      return defaultImage(tokens, idx, options, env, self);
    };

    mdRenderer = m;
    return m;
  }

  /**
   * 把正文里**少数几个内联 HTML 标签**显示成真的排版。
   *
   * 为什么开这个小口子：md 里本来就有 sub / sup 这类标签（MinerU 把 PDF 里的
   * 上下标原样带出来了，例如 MRD 的下标 i 与 (%)），而 markdown-it 开着 html:false
   * 会把它们转义成可见的原文（用户看到 MRD<sub>i</sub> 那样一串）。
   * **不改成 html:true** —— 这一页是 resource:// 的特权文档，放进来的 HTML 能跑
   * 脚本，而 MinerU 的正文来自任意 PDF；所以只对 `sub/sup/br/i/b/em/strong` 这几个**无属性**标签做精确回填
   * （带属性的写法这一步匹配不上，继续保持转义）。
   */
  function allowInlineTags(html) {
    // ⚠ 只认**无属性**写法：标签名后面最多跟空白与一个 "/"（`<br/>`）。
    //   一旦出现 `=`、引号或别的字符就匹配不上，继续留在转义态 —— 所以这里
    //   绝不能用 `<s.*>` 这类宽松正则（那会把 <script> 放进来）。
    return String(html).replace(
      /&lt;(\/?)(sub|sup|br|i|b|em|strong)(\s*\/?)\s*&gt;/gi, "<$1$2$3>");
  }

  function renderMd(text, env) {
    var m = getMd();
    var src = String(text == null ? "" : text);
    if (!m) return "<p>" + esc(src) + "</p>";
    try {
      return allowInlineTags(m.render(src, env || {}));
    } catch (e) {
      logIt("md render: " + e);
      // ⚠ 整块渲染失败时**逐段退化**：至少保住分段与能渲染的部分，
      //   而不是把整页塞进一个大 <p> —— 那样换行会被 HTML 折成一个空格，
      //   用户看到的是「整页糊成一片、图片与公式全成原文」（2026-10-11 真人反馈）。
      var chunks = src.split(/\n{2,}/);
      var out = [];
      for (var ci = 0; ci < chunks.length; ci++) {
        if (!chunks[ci].trim()) continue;
        try { out.push(allowInlineTags(m.render(chunks[ci], env || {}))); }
        catch (e2) { out.push("<p>" + esc(chunks[ci]) + "</p>"); }
      }
      return out.join("\n") || ("<p>" + esc(src) + "</p>");
    }
  }

  // ------------------------------------------------------------ parts 拼接

  /** parts → 整页 markdown（与宿主的 kbviewJoinBlocks 同一套：纯字符串相加）。 */
  function joinParts(p) {
    if (!p) return "";
    var s = String(p.prefix == null ? "" : p.prefix);
    var bs = p.blocks || [], ss = p.seps || [];
    for (var i = 0; i < bs.length; i++) {
      s += String(bs[i] == null ? "" : bs[i]);
      if (i < ss.length) s += String(ss[i] == null ? "" : ss[i]);
    }
    s += String(p.suffix == null ? "" : p.suffix);
    return s;
  }

  // ------------------------------------------------------------ 页块 / 页内定位

  function pageKey(n) { return String(parseInt(n, 10)); }

  /**
   * 滚到第 n 页那一块并高亮。
   *
   * ⚠ 只在**宿主送来整份内容时**调一次（打开那一列 / 换条目 / 重新读取）——
   *   翻页**不跟随**（2026-10-11 用户拍板：跟随没生效也不想要了，手动翻找不麻烦）。
   *   原来宿主还有一个 `page` 消息专管跟随，已随功能一起去掉。
   */
  function showPage(n, force) {
    var v = els.view;
    if (!v) return;
    var num = parseInt(n, 10);
    if (!num) return;
    var blocks = v.querySelectorAll(".kb-page");
    if (!blocks.length) return;
    var block = v.querySelector('.kb-page[data-kbpage="' + num + '"]');
    if (!block) return;
    if (st.editor && !force) return;
    for (var i = 0; i < blocks.length; i++) blocks[i].classList.remove("kbv-active");
    block.classList.add("kbv-active");
    try {
      var vr = v.getBoundingClientRect();
      var br = block.getBoundingClientRect();
      v.scrollTop += (br.top - vr.top) - 2;
    } catch (e) { logIt("scroll: " + e); }
  }

  function pageBar(page) {
    var bar = el("div", "kbv-page-head");
    var tag = el("span", "kbv-tag", "p." + page);
    if (st.edited[pageKey(page)]) {
      tag.className = "kbv-tag kbv-edited";
      tag.textContent = "p." + page + " · 已修订";
    }
    if (st.stale[pageKey(page)]) {
      tag.textContent += " · 基于旧解析";
      tag.title = "这一页的解析结果变过，你改的是旧版 —— 仍用你的版本，请核对一下";
    }
    var editBtn = el("button", "kbv-mini", st.edited[pageKey(page)] ? "修改这一页" : "改这一页");
    editBtn.type = "button";
    var restoreBtn = el("button", "kbv-mini", "恢复原版");
    restoreBtn.type = "button";
    restoreBtn.disabled = !st.edited[pageKey(page)];
    var status = el("span", "kbv-status", "");
    bar.appendChild(tag);
    bar.appendChild(editBtn);
    bar.appendChild(restoreBtn);
    bar.appendChild(status);

    editBtn.addEventListener("click", function () {
      if (st.busy) return;
      if (st.editor && st.editor.page === page) { closeEditor(); return; }
      openEditor(page, bar, editBtn, restoreBtn, status);
    });
    restoreBtn.addEventListener("click", function () {
      if (st.busy) return;
      setBusy(true, "正在恢复 p." + page + "…");
      post({ type: "restorePage", page: page });
    });
    return bar;
  }

  // ------------------------------------------------------------ 分块编辑

  function closeEditor() {
    var ed = st.editor;
    st.editor = null;
    if (!ed) return;
    try { if (ed.holder && ed.holder.parentNode) ed.holder.parentNode.removeChild(ed.holder); }
    catch (e) { /* ignore */ }
    if (ed.editBtn) ed.editBtn.disabled = false;
    if (ed.restoreBtn) ed.restoreBtn.disabled = !st.edited[pageKey(ed.page)];
    if (ed.status) setText(ed.status, "");
  }

  /**
   * 让左边编辑框的高度 = max(自己的内容高, 右边预览高)。
   *
   * ⚠ 原来左边固定 min-height:56px，比右边预览矮一大截（真人反馈「左边太小」），
   *   而且 MinerU 的块常常很长，固定高度就得在小框里上下滚。
   * ⚠ 也不能只按自己的 scrollHeight 给高度：预览里的**图片是异步加载**的，
   *   图一进来右边就变高、左边还是旧高度（实测差 80px）。所以这里同时看两边，
   *   并在预览的图片 load/error 时再调一次（见 paintPreview）。
   * 置 auto 再读 scrollHeight 是为了让它能**变矮**（高度只增不减会越拉越长）。
   */
  function autoGrow(row) {
    if (!row || !row.ta) return;
    try {
      var ta = row.ta;
      ta.style.height = "auto";
      var h = ta.scrollHeight + 2;
      if (row.prev && row.prev.offsetHeight > h) h = row.prev.offsetHeight;
      ta.style.height = h + "px";
    } catch (e) { /* ignore */ }
  }

  function paintPreview(row) {
    if (!row || !row.prev) return;
    var env = { imgBase: st.imgBase };
    row.prev.innerHTML = renderMd(row.ta.value, env);
    // 预览里的图片是**异步**加载的：加载完右边才真正变高 —— 那时要再对一次高度，
    // 否则左边会停在旧高度（实测差 80px，看起来就是"左边又变小了"）。
    var imgs = row.prev.querySelectorAll("img");
    for (var ii = 0; ii < imgs.length; ii++) {
      imgs[ii].addEventListener("load", function () { autoGrow(row); });
      imgs[ii].addEventListener("error", function () { autoGrow(row); });
    }
    autoGrow(row);
    var dirty = row.ta.value !== row.orig;
    if (dirty) row.wrap.classList.add("kbv-dirty");
    else row.wrap.classList.remove("kbv-dirty");
    return dirty;
  }

  function schedulePreview(row) {
    if (row.timer) clearTimeout(row.timer);
    row.timer = setTimeout(function () { row.timer = null; paintPreview(row); }, 200);
  }

  function buildBlockRows(holder, parts) {
    var rows = [];
    var wrap = el("div", "kbv-blocks");
    each(parts.blocks || [], function (text, i) {
      var rowWrap = el("div", "kbv-block");
      var left = el("div", "kbv-block-cell kbv-block-edit");
      var ta = el("textarea", "kbv-block-text");
      ta.spellcheck = false;
      ta.setAttribute("wrap", "soft");
      ta.value = String(text == null ? "" : text);
      left.appendChild(ta);
      var right = el("div", "kbv-block-cell kbv-block-prev");
      rowWrap.appendChild(left);
      rowWrap.appendChild(right);
      wrap.appendChild(rowWrap);
      var row = { wrap: rowWrap, ta: ta, prev: right, orig: String(text == null ? "" : text), timer: null };
      ta.addEventListener("input", function () {
        autoGrow(row);
        schedulePreview(row);
      });
      ta.addEventListener("keydown", function (ev) {
        // Tab 在块间跳（而不是跳焦点出编辑器）
        if (ev.key === "Tab" && !ev.shiftKey) {
          ev.preventDefault();
          var all = holder.querySelectorAll(".kbv-block-text");
          var idx = Array.prototype.indexOf.call(all, ta);
          if (idx >= 0 && all[idx + 1]) all[idx + 1].focus();
        }
      });
      rows.push(row);
      paintPreview(row);
    });
    return { wrap: wrap, rows: rows };
  }

  /**
   * 打开一页的编辑器。
   *
   * 分块口径（派发包 §3.2）：空行分段 / 独立公式成块 / 表格整块 / 图片行单独一块
   * —— 这些**都由宿主算好**（kbviewSplitBlocks，纯函数、有单测），页面只画。
   * 每块一行：左边等宽小编辑框（自动换行）｜右边该块的 KaTeX 实时预览。
   * 顶部「整页源码」开关给"整段重写"的情形（也是软换行，不再有几万像素的长行）。
   */
  function openEditor(page, bar, editBtn, restoreBtn, status) {
    closeEditor();
    var parts = st.pages[pageKey(page)];
    if (!parts) { setText(status, "这一页没有可编辑的 markdown。"); return; }
    var holder = el("div", "kbv-editor-holder");
    var barEl = el("div", "kbv-editbar");
    var modeBlocks = el("button", "kbv-mini kbv-mode kbv-mode-on", "按块编辑");
    modeBlocks.type = "button";
    var modeSource = el("button", "kbv-mini kbv-mode", "整页源码");
    modeSource.type = "button";
    var saveBtn = el("button", "kbv-mini kbv-save", "保存并重建");
    saveBtn.type = "button";
    var cancelBtn = el("button", "kbv-mini", "取消");
    cancelBtn.type = "button";
    var eHint = el("span", "kbv-status", "左框改、右框实时预览；改动过的块会标黄");
    barEl.appendChild(modeBlocks);
    barEl.appendChild(modeSource);
    barEl.appendChild(saveBtn);
    barEl.appendChild(cancelBtn);
    barEl.appendChild(eHint);

    var built = buildBlockRows(holder, parts);
    var sourceWrap = el("div", "kbv-source-wrap");
    var sourceTa = el("textarea", "kbv-editor");
    sourceTa.spellcheck = false;
    sourceTa.setAttribute("wrap", "soft");
    sourceTa.value = joinParts(parts);
    sourceWrap.appendChild(sourceTa);
    sourceWrap.hidden = true;

    holder.appendChild(barEl);
    holder.appendChild(built.wrap);
    holder.appendChild(sourceWrap);
    if (bar.parentNode) bar.parentNode.insertBefore(holder, bar.nextSibling);

    var ed = {
      page: page, holder: holder, mode: "blocks",
      rows: built.rows, source: sourceTa,
      editBtn: editBtn, restoreBtn: restoreBtn, status: status
    };
    st.editor = ed;

    function setMode(mode) {
      ed.mode = mode;
      var blocks = mode === "blocks";
      built.wrap.hidden = !blocks;
      sourceWrap.hidden = blocks;
      modeBlocks.className = "kbv-mini kbv-mode" + (blocks ? " kbv-mode-on" : "");
      modeSource.className = "kbv-mini kbv-mode" + (blocks ? "" : " kbv-mode-on");
      setText(eHint, blocks
        ? "左框改、右框实时预览；改动过的块会标黄"
        : "整页 markdown 源码（公式与图片都是原文），改完点「保存并重建」");
    }
    modeBlocks.addEventListener("click", function () { setMode("blocks"); });
    modeSource.addEventListener("click", function () { setMode("source"); });

    saveBtn.addEventListener("click", function () {
      if (st.busy) return;
      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      if (ed.mode === "source") {
        setBusy(true, "正在保存 p." + page + "…");
        post({ type: "savePage", page: page, text: sourceTa.value });
      } else {
        var blocks = [];
        each(ed.rows, function (r) { blocks.push(r.ta.value); });
        var out = {
          prefix: parts.prefix == null ? "" : parts.prefix,
          blocks: blocks,
          seps: parts.seps || [],
          suffix: parts.suffix == null ? "" : parts.suffix
        };
        setBusy(true, "正在保存 p." + page + "…");
        post({ type: "saveBlocks", page: page, parts: out });
      }
    });
    cancelBtn.addEventListener("click", function () {
      if (st.busy) return;
      closeEditor();
    });

    editBtn.disabled = true;
    restoreBtn.disabled = true;
    // 插进文档之后才量得出 scrollHeight —— 这里给每个块补一次自动高度
    each(ed.rows, function (rr) { autoGrow(rr); });
    if (built.rows.length) built.rows[0].ta.focus();
    setText(status, "");
  }

  // ------------------------------------------------------------ 正文渲染

  function renderEmpty(msg) {
    var v = els.view;
    v.innerHTML = "";
    v.appendChild(el("div", "kbv-empty", msg.emptyWhy || "知识库里还没有这一篇的任何分级文件。"));
    if (msg.probe) v.appendChild(el("div", "kbv-probe", msg.probe));
    v.appendChild(el("div", "kbv-empty", "先在管理面板「高级」页的「增量更新」建一次索引，"
      + "或用右键菜单「重建本条目知识库」。"));
  }

  function renderContent(msg) {
    var v = els.view;
    v.innerHTML = "";
    var env = { imgBase: st.imgBase };
    if (msg.fulltext && st.order.length) {
      each(st.order, function (n) {
        var parts = st.pages[pageKey(n)];
        var block = el("div", "kb-page");
        block.setAttribute("data-kbpage", String(n));
        block.appendChild(pageBar(n));
        var body = el("div", "kbv-page-body");
        body.innerHTML = renderMd(joinParts(parts), env);
        block.appendChild(body);
        v.appendChild(block);
      });
    } else {
      v.innerHTML = renderMd(msg.md || "", env);
      if (!v.firstChild && (msg.md || "").length) {
        v.appendChild(el("div", "kbv-empty", "这一级的内容没渲染出来（"
          + (msg.textLength || 0) + " 字已读到）—— 请把这一行报给维护者。"));
      }
    }
  }

  function decorateImages() {
    var v = els.view;
    if (!v) return;
    var imgs = v.querySelectorAll("img");
    for (var i = 0; i < imgs.length; i++) {
      (function (img) {
        img.addEventListener("click", function () {
          // 宿主把**原图相对路径**写进了 data-kbv-src；图片本身走
          // resource://zotero-kb-files/… 加载（路径解不回来）——点一下让
          // 宿主用系统默认程序打开原图。
          var rel = String(img.getAttribute("data-kbv-src") || "");
          if (rel) post({ type: "openImage", rel: rel });
        });
      })(imgs[i]);
    }
  }

  function setBusy(busy, text) {
    st.busy = !!busy;
    if (text != null) setText(els.hint, text);
    if (els.btnReload) els.btnReload.disabled = !!busy;
    if (els.btnDraft) els.btnDraft.disabled = !!busy;
  }

  function render(msg) {
    st.payload = msg;
    st.key = msg.key || "";
    st.fulltext = !!msg.fulltext;
    st.order = msg.order || [];
    st.pages = msg.pages || {};
    st.imgBase = msg.imgBase || "";
    st.edited = {};
    st.stale = {};
    each(msg.edited, function (n) { st.edited[pageKey(n)] = true; });
    each(msg.stale, function (n) { st.stale[pageKey(n)] = true; });
    st.page = parseInt(msg.page, 10) || 0;
    // 宿主重推 = 内容变了 → 关掉还开着的编辑器（否则改的东西会被悄悄丢掉）
    closeEditor();

    if (msg.levels) {
      fillSelect(els.level, msg.levels.map(function (lv) {
        return { value: lv.id, label: lv.label };
      }), msg.level);
    }
    if (msg.font) setFontSize(msg.font);
    els.btnDraft.hidden = !st.fulltext;

    if (msg.empty) renderEmpty(msg);
    else { renderContent(msg); decorateImages(); }

    setBusy(!!msg.busy, msg.hint || "");
    if (st.fulltext && st.page) showPage(st.page, true);
  }

  // ------------------------------------------------------------ 接线

  // ------------------------------------------------------------ 公式修正
  //
  // 宿主（src/22-formula.js）取好数据才送进来；这里只画界面：
  //   · 「应用」   → post({type:"formulaApply"})            → 宿主调 POST /formula-apply
  //   · 「MathType」→ post({type:"formulaMathType",latex})   → 宿主调 POST /formula-mathtype
  //     （服务端把 LaTeX 放进剪贴板并启动 MathType，排好后复制回来粘进框里）
  // **不做识别**：LaTeX 由人写。MathType 没装时按钮只置灰、不隐藏。

  var fml = { key: "", page: 0, ordinal: 0, oldLatex: "", cands: [], wired: false };

  function fmlEls() {
    return {
      box: document.getElementById("kbv-fml"),
      where: document.getElementById("kbv-fml-where"),
      img: document.getElementById("kbv-fml-img"),
      tex: document.getElementById("kbv-fml-tex"),
      prev: document.getElementById("kbv-fml-prev"),
      cands: document.getElementById("kbv-fml-cands"),
      cand: document.getElementById("kbv-fml-cand"),
      msg: document.getElementById("kbv-fml-msg"),
      mt: document.getElementById("kbv-fml-mt"),
      mthint: document.getElementById("kbv-fml-mthint"),
      x: document.getElementById("kbv-fml-x"),
      cancel: document.getElementById("kbv-fml-cancel"),
      apply: document.getElementById("kbv-fml-apply")
    };
  }

  function fmlPreview() {
    var e = fmlEls();
    if (!e.tex || !e.prev) return;
    var tex = String(e.tex.value || "");
    if (!tex.trim()) {
      e.prev.innerHTML = '<span class="kbv-fml-empty">（还没写 LaTeX）</span>';
      return;
    }
    e.prev.innerHTML = mathHtml(tex, true);
  }

  function fmlSay(text, kind) {
    var e = fmlEls();
    if (!e.msg) return;
    e.msg.textContent = text || "";
    e.msg.className = "kbv-fml-msg"
      + (kind === "err" ? " kbv-fml-err" : (kind === "ok" ? " kbv-fml-ok" : ""));
  }

  function fmlClose() {
    var e = fmlEls();
    if (e.box) e.box.hidden = true;
  }

  function fmlPick(idx) {
    var e = fmlEls();
    var c = fml.cands[idx];
    if (!c) return;
    fml.ordinal = c.eq_ordinal | 0;
    fml.oldLatex = String(c.latex || "");
    e.tex.value = fml.oldLatex;
    setText(e.where, "第 " + (fml.page + 1) + " 页，第 " + (fml.ordinal + 1) + " 条公式");
    fmlPreview();
  }

  function fmlWire() {
    if (fml.wired) return;
    var e = fmlEls();
    if (!e.box) return;
    fml.wired = true;
    e.tex.addEventListener("input", fmlPreview);
    e.x.addEventListener("click", fmlClose);
    e.cancel.addEventListener("click", fmlClose);
    e.cand.addEventListener("change", function () {
      fmlPick(parseInt(e.cand.value, 10) || 0);
    });
    e.apply.addEventListener("click", function () {
      var tex = String(e.tex.value || "").trim();
      if (!tex) { fmlSay("LaTeX 是空的，先写一条。", "err"); return; }
      e.apply.disabled = true;
      fmlSay("正在写回…");
      post({ type: "formulaApply", key: fml.key, page: fml.page,
             latex: tex, ordinal: fml.ordinal, old_latex: fml.oldLatex });
    });
    e.mt.addEventListener("click", function () {
      fmlSay("正在把 LaTeX 放进剪贴板并打开 MathType…");
      post({ type: "formulaMathType", latex: String(e.tex.value || "") });
    });
  }

  function fmlOpen(payload) {
    if (!payload) return;
    var e = fmlEls();
    if (!e.box) { logIt("formula: 页面里没有编辑区（kbview.html 是不是旧的？）"); return; }
    fmlWire();
    fml.key = String(payload.key || "");
    fml.page = payload.page | 0;
    var m = payload.matched || null;
    fml.oldLatex = m ? String(m.latex || "") : "";
    fml.ordinal = m ? (m.eq_ordinal | 0) : 0;
    fml.cands = payload.candidates || [];

    setText(e.where, "第 " + (fml.page + 1) + " 页"
      + (m ? "，第 " + (fml.ordinal + 1) + " 条公式" : "（没定位到，下面挑一条）"));
    if (payload.preview) { e.img.src = payload.preview; e.img.hidden = false; }
    else { e.img.removeAttribute("src"); e.img.hidden = true; }
    e.tex.value = fml.oldLatex;

    if (fml.cands.length > 1) {
      e.cand.innerHTML = "";
      for (var i = 0; i < fml.cands.length; i++) {
        var c = fml.cands[i];
        var o = document.createElement("option");
        o.value = String(i);
        o.textContent = "第 " + ((c.eq_ordinal | 0) + 1) + " 条："
          + String(c.latex || "").replace(/\s+/g, " ").slice(0, 40);
        if ((c.eq_ordinal | 0) === fml.ordinal) o.selected = true;
        e.cand.appendChild(o);
      }
      e.cands.hidden = false;
    } else {
      e.cands.hidden = true;
    }

    var mt = payload.mathtype || {};
    e.mt.disabled = !mt.installed;          // ⚠ 没装**不隐藏**，只置灰
    e.mthint.textContent = mt.installed
      ? "会在 MathType 里打开：排好后复制，回来粘进上面的框"
      : "本机没装 MathType（装了就能用）";
    e.apply.disabled = false;
    fmlSay(payload.why || "");
    e.box.hidden = false;
    fmlPreview();
    try { e.tex.focus(); } catch (err) { /* ignore */ }
  }

  /** 正文字号：唯一来源是宿主给的 `font`（pref `zotero-kb.kbviewFont`，10~20）。 */
  function setFontSize(px) {
    var v = parseInt(px, 10);
    if (!isFinite(v)) v = 13;
    if (v < 10) v = 10;
    if (v > 20) v = 20;
    document.documentElement.style.setProperty("--kbv-font-size", v + "px");
  }

  function handle(msg) {
    try {
      if (!msg || typeof msg.type !== "string") return;
      if (msg.type === "render") render(msg);
      else if (msg.type === "font") setFontSize(msg.font);
      // ⚠ 原来这里还有一个 `page` 消息（宿主翻页时推当前页、本页滚过去）：
      //   2026-10-11 按页跟随去掉，宿主不再发它，这个分支也就删了。
      else if (msg.type === "hint") setText(els.hint, msg.text);
      else if (msg.type === "busy") setBusy(!!msg.busy, msg.text);
      else if (msg.type === "formula") fmlOpen(msg.payload);
      else if (msg.type === "formulaResult") {
        var fr = fmlEls();
        if (fr.apply) fr.apply.disabled = false;
        if (msg.ok) {
          fmlSay("已写回" + (msg.job ? "，正在重建索引…" : "。"), "ok");
          setTimeout(fmlClose, 900);
        } else {
          fmlSay("写回失败：" + (msg.error || "未知原因"), "err");
        }
      }
      else if (msg.type === "formulaMathTypeResult") {
        if (msg.ok) {
          fmlSay("已把当前 LaTeX 放进剪贴板并打开 MathType："
            + "排好后复制，回来粘进上面的框。", "ok");
        } else {
          fmlSay("MathType 那条路没走通：" + (msg.error || "未知原因"), "err");
        }
      }
    } catch (e) {
      setText(els.hint, "渲染出错：" + e);
      logIt("render error: " + e);
    }
  }

  function wire() {
    els.view = document.getElementById("kbv-view");
    els.hint = document.getElementById("kbv-hint");
    els.level = document.getElementById("kbv-level");
    els.btnDraft = document.getElementById("kbv-draft");
    els.btnReload = document.getElementById("kbv-reload");
    els.btnClose = document.getElementById("kbv-close");

    els.level.addEventListener("change", function () {
      post({ type: "selectLevel", level: els.level.value });
    });
    // 字号没有本地控件：唯一来源是面板「偏好设置 → 侧栏」里的数值（px）。
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

  // 宿主调这个（JSON 字符串，见文件头）
  window.kbvReceive = function (jsonText) {
    var msg = null;
    try { msg = JSON.parse(String(jsonText)); } catch (e) { msg = null; }
    if (msg) handle(msg);
  };

  // 供宿主判断"页面脚本真的跑起来了"
  window.kbvPageReady = true;

  // 把渲染管线露出来：真机排错（"这一页为什么没渲染成 markdown"）与
  // tools/verify_kbview_md.js 的离线回归都靠它。**只读**，不影响界面。
  window.__kbvDebug = { renderMd: renderMd, getMd: getMd, joinParts: joinParts };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
