/* ============================================================================
 * admin-pages/suggest.js —— 「分类建议」页（选一篇 → 生成要点 / 纲要 / 分类建议 → 采纳）
 *
 * 对应**已退役（2026-10-14）**的 Tk 面板 tools/panels/tab_ai.py（已退役 2026-10-14）。功能照它，布局**不照它**：
 * Tk 版把「选文献」做成一个独立弹窗（列宽可拖、表头可排序），网页这版没有
 * 独立窗口那一层 —— 直接把文献列表放在页面上，选中哪一行就是对哪一篇动作，
 * 少一次弹窗往返（用户要「简约干净」）。
 *
 * 依赖的引擎方法（契约见开发说明，2026-10-13 冻结）：
 *   GET  /papers           → {rows:[{key,title,year,first_author,collections[],
 *                                     fulltext_chars,in_kb}], total}
 *   POST /summarize        {key} → {key, summary, ms}                      120s
 *   POST /digest           {key} → 长任务 {job:{id}}（之后轮询 /jobs/<id>）
 *   POST /classify         {key, feedback?, previous?}
 *                          → {key, category, tags[], reason, raw?}         120s
 *   POST /apply-suggestion {key, category, tags[]} → {ok, applied}         120s
 *   POST /maintain         {task:"collections"} → {task, ok, output}       120s
 *
 * 三条硬约束（与 structure.js / experience.js 完全一致）：
 *   · 只画自己这一页，取数一律 KBAdmin.get/post（页面不发网络请求，
 *     宿主半按路由表转成引擎方法）；
 *   · 样式只用 admin.css 的类，不写颜色字面量、不写内联 style；
 *   · 一页一个文件、不 import。
 *
 * ⚠「应用建议」是**唯一会改用户 Zotero 的动作**：按钮用 btn-danger，
 *   点下去先弹二次确认，确认框里必须把「分类 = …、标签 = …」摊开写清 ——
 *   用户要有机会在写库前看清到底写什么。Tk 版的 askyesno 也是这个口径。
 * ==========================================================================*/
(function () {
  "use strict";

  function msgOf(e) { return (e && e.message) ? e.message : String(e); }

  /** 长文本掐短：格子里只留关键词，全文放 title 悬停（用户要「简约」）。 */
  function clip(s, n) {
    const t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  /* ==========================================================================
   * 共享零件：文献列表的「分类多选筛选」（全项目**唯一**一份实现）
   *
   * 2026-10-14 用户：「文献列表的文献太多……无论 dsh 部分还是 zotero 管理面板的
   * 部分，文献列表能不能在分类那里下拉，让用户选择哪个或者哪几个分类。」
   *
   * 为什么挂到 KBAdmin 上、而不是写进 admin.js：admin.js 里的 `select()` 是
   * **单选**的（它给分节纲要页用），而这里要的是多选；两个页面（本页 +
   * experience.js 的「选择关联文献」弹窗）都要用，admin-pages/*.js 之间又不能
   * import（见文件头的三条硬约束），所以只写一份挂到壳上。
   * ⚠ **改这一处就是改两处**：experience.js 里用的是 `KBAdmin.multiSelect`。
   *
   * ⚠ 只用 admin.css 已有的 `.btn-group` / `.sel*` 类，不新增样式；也绝不用原生
   *   `<select>` / `<select multiple>` —— Zotero 的 <browser> 里它的弹层出不来
   *   （真机实测，见 admin.js 顶部说明），而且多选在原生控件里还要按住 Ctrl。
   * ========================================================================*/

  /**
   * `collectionOptions(rows)` —— 从文献行里聚合出分类选项（按篇数降序、同数按拼音）。
   *
   * `/papers` 的每行本来就带 `collections[]`，所以**一个请求都不多发**。
   * 返回 `[{value, text:"名字（n）", title}, …]`，直接喂 multiSelect。
   */
  function collectionOptions(rows) {
    const n = {};
    (rows || []).forEach(function (r) {
      const cs = (r && r.collections) || [];
      for (let i = 0; i < cs.length; i++) {
        const name = String(cs[i] || "");
        if (name) n[name] = (n[name] || 0) + 1;
      }
    });
    return Object.keys(n).map(function (name) {
      return { value: name, text: name + "（" + n[name] + "）",
               title: "Zotero 分类：" + name + "，共 " + n[name] + " 篇" };
    }).sort(function (a, b) {
      if (n[b.value] !== n[a.value]) return n[b.value] - n[a.value];
      return a.value.localeCompare(b.value, "zh", { numeric: true, sensitivity: "base" });
    });
  }

  /**
   * `multiSelect(label, options, selected, onChange, opts)` —— 分类多选控件。
   *
   * 形状与 admin.js::select 对齐（label 画在按钮里的前缀），但值是**数组**：
   *   · `selected` 空数组 = 不筛（全部）；多个 = **并集**（"在这几个分类里任一个就算"）；
   *   · 选项 ≤ 4 个 → `.btn-group` 并排按钮（与分节纲要页的状态筛选同一套），
   *     更长 → `.sel` 自绘下拉（多选版：点一项**不关**浮层，要能连着勾几个）；
   *   · `onChange(vals)` 拿到的永远是**新数组**；`node.getSelected()` 同理。
   * 另外给 `setOptions(list)` / `setDisabled(on)`（外部换数据、跟长任务一起置灰）。
   *
   * 与 admin.js::select 的差别只有"多选必须的"那几条：点里面不关、选项带 ☑/☐、
   * 另加一个「全部分类（清除选择）」；展开/收起的口径照抄 attachPopup
   *（捕获阶段点外面关、Esc 关并回焦、点按钮切开关 —— 那份实现是私有的）。
   */
  function multiSelect(label, options, selected, onChange, opts) {
    const el = KBAdmin.el;
    const o = opts || {};
    const node = el("span");
    let list = [];
    let cur = (selected || []).map(String);
    let off = false;
    let openState = false;      // 展开态（重画时要还原，勾一项得看得见）
    let domBtn = null;          // 下拉模式的那个按钮（按钮组模式为 null）
    let domPop = null;

    function fire() { if (typeof onChange === "function") onChange(cur.slice()); }

    function toggle(v) {
      const i = cur.indexOf(v);
      if (i >= 0) cur.splice(i, 1); else cur.push(v);
      paint();
      fire();
    }

    /** 按钮上显示什么：0 个 = "全部分类"，否则列出被选中的分类名（顿号连）。 */
    function labelText() {
      if (!cur.length) return o.allText || "全部分类";
      return cur.map(function (v) {
        const it = list.filter(function (x) { return x.value === v; })[0];
        return it ? String(it.text).replace(/（\d+）$/, "") : String(v);
      }).join("、");
    }

    // ---- 展开 / 收起（照 admin.js::attachPopup 的口径重写一份，那份是私有的）
    function detach() {
      document.removeEventListener("click", onDoc, true);
      document.removeEventListener("keydown", onKey, true);
    }
    function setOpen(on) {
      openState = !!on;
      if (!domPop) return;
      domPop.classList.toggle("hidden", !openState);
      if (domBtn) domBtn.setAttribute("aria-expanded", openState ? "true" : "false");
      if (openState) {
        document.addEventListener("click", onDoc, true);
        document.addEventListener("keydown", onKey, true);
      } else {
        detach();
      }
    }
    function onDoc(ev) {
      if (!domPop) return;
      if (domPop.contains(ev.target) || domBtn === ev.target
          || (domBtn && domBtn.contains(ev.target))) return;
      setOpen(false);
    }
    function onKey(ev) {
      if (ev.key !== "Escape") return;
      ev.stopPropagation();
      setOpen(false);
      if (domBtn) domBtn.focus();
    }

    function paint() {
      detach();
      node.textContent = "";
      domBtn = null;
      domPop = null;
      if (label) node.appendChild(el("span", { class: "sel-cap",
                                               text: String(label) + "：" }));

      if (list.length && list.length <= 4) {
        // 选项 ≤ 4 个用按钮组：点一下就开关，比下拉少一次点击。
        const group = el("div", { class: "btn-group" });
        list.forEach(function (it) {
          const on = cur.indexOf(it.value) >= 0;
          const b = el("button", { type: "button", class: "btn btn-subtle",
                                   text: it.text });
          b.title = it.title || "";
          b.setAttribute("aria-pressed", on ? "true" : "false");
          b.classList.toggle("is-on", on);
          b.disabled = off;
          b.addEventListener("click", function () { toggle(it.value); });
          group.appendChild(b);
        });
        node.appendChild(group);
        openState = false;
        return;
      }

      // 长列表用自绘下拉（.sel 那套；admin.js 那份是单选，这是多选版）。
      const wrap = el("div", { class: "sel" });
      const btn = el("button", { type: "button", class: "sel-btn",
                                 "aria-haspopup": "listbox",
                                 "aria-expanded": "false" });
      const txt = labelText();
      btn.appendChild(el("span", { class: "sel-val", text: txt }));
      btn.appendChild(el("span", { class: "sel-caret", text: "▾" }));
      btn.disabled = off;
      btn.title = String(o.title || "按 Zotero 分类筛选（可多选）：点一项勾上 / 取消，"
                      + "0 个 = 不过滤，多个 = 取并集");
      wrap.appendChild(btn);
      const pop = el("div", { class: "sel-pop hidden", role: "listbox" });
      wrap.appendChild(pop);
      domBtn = btn;
      domPop = pop;
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        setOpen(!openState);
      });

      // 第一项固定是「全部分类」= 清空选择（0 个选中时它就是当前态）。
      const all = el("button", { type: "button", class: "sel-opt", role: "option",
                                 text: "全部分类（清除选择）" });
      all.classList.toggle("is-on", !cur.length);
      all.setAttribute("aria-selected", cur.length ? "false" : "true");
      all.addEventListener("click", function (ev) {
        ev.stopPropagation();       // 点里面**不关**浮层
        cur = [];
        paint();
        fire();
      });
      pop.appendChild(all);
      if (!list.length) {
        pop.appendChild(el("div", { class: "sel-opt",
                                    text: "（还没有可选的分类）" }));
      }
      list.forEach(function (it) {
        const on = cur.indexOf(it.value) >= 0;
        const b = el("button", { type: "button", class: "sel-opt", role: "option",
                                 text: (on ? "☑ " : "☐ ") + it.text });
        if (it.title) b.title = it.title;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.addEventListener("click", function (ev) {
          ev.stopPropagation();
          toggle(it.value);
        });
        pop.appendChild(b);
      });
      node.appendChild(wrap);
      if (openState) setOpen(true);   // 重画前是开着的就还开着
    }

    node.getSelected = function () { return cur.slice(); };
    node.setOptions = function (list2) {
      list = (list2 || []).map(function (it) {
        if (it && typeof it === "object") {
          return { value: String(it.value == null ? "" : it.value),
                   text: String(it.text == null ? it.value : it.text),
                   title: it.title == null ? "" : String(it.title) };
        }
        return { value: String(it), text: String(it), title: "" };
      });
      // 只保留还存在的取值：选项没了却还拿它筛，结果必然是空表。
      cur = cur.filter(function (v) {
        return list.some(function (it) { return it.value === v; });
      });
      paint();
    };
    node.setDisabled = function (on) { off = !!on; paint(); };
    node.setOptions(options);
    return node;
  }

  KBAdmin.multiSelect = multiSelect;
  KBAdmin.collectionOptions = collectionOptions;

  /**
   * 二次确认。（会改 Zotero 的动作一律先过这里。）
   *
   * ⚠ 自己包一层而不是直接用 ctx.dialog 的 actions：那两个按钮的 run 会被
   *   当成「做完就关」，而这里要的是「用户点了确认之后我才去发请求」，
   *   所以用 Promise 把结果交回调用方。Esc / 点遮罩关窗走 close 事件兜底
   *   resolve(false)，免得留下一个永远不落地的 Promise（与 experience.js 同）。
   */
  function confirmBox(ctx, title, body, okText, danger) {
    return new Promise(function (resolve) {
      let done = false;
      const dlg = ctx.dialog(title, body, [
        { text: "取消", run: function () { done = true; resolve(false); return true; } },
        { text: okText, primary: !danger, danger: !!danger,
          run: function () { done = true; resolve(true); return true; } },
      ]);
      dlg.addEventListener("close", function () { if (!done) resolve(false); });
    });
  }

  KBAdmin.page({
    id: "suggest",
    title: "分类建议",
    order: 30,

    async render(host, ctx) {
      const el = ctx.el;

      // 这一页的状态都在这个闭包里（一页一文件，不往全局挂东西）：
      //   selKey   当前选中的文献 key
      //   sugg     最近一次 /classify 的结果（「应用建议」用它）
      //   papers   从 /papers 读回的全量行（分类筛选在**本地**做，不再发请求）
      //   cats     当前选中的分类（0 个 = 全部；多个 = 并集）
      const state = { selKey: "", sugg: null, papers: [], cats: [] };

      // ================================================================ 卡片一：文献列表
      const countLabel = el("span", { class: "faint", text: "正在读取…" });
      const btnReload = el("button", {
        class: "btn btn-subtle", text: "刷新列表",
        title: "从知识库重新读文献列表",
      });
      const btnColls = el("button", {
        class: "btn btn-subtle", text: "只看分类分布",
        title: "不调模型，只统计每个分类有多少篇（结果在下面的输出区）",
      });
      const tableHost = el("div", null,
        [el("div", { class: "note note-warn", text: "正在读文献列表…" })]);
      const selLabel = el("span", { class: "subtle", text: "（还没选）" });

      // 分类筛选：**0 个选中 = 全部**，选多个取并集。控件是共享零件
      // `KBAdmin.multiSelect`（选项 ≤4 个自动退成按钮组，长列表是自绘下拉）——
      // 与经验库「选择关联文献」弹窗用的是同一份实现，别在这里另造一套。
      // ⚠ 筛选在**本地**做（/papers 一次就给全量行），所以筛选不发请求、即时生效。
      const catFilter = KBAdmin.multiSelect("分类", [], state.cats, function (vals) {
        state.cats = vals;
        renderPapers();
      });
      // 库里一个分类都没有时整行藏起来（不然只有一个「全部分类」的空控件）。
      const catRow = el("div", { class: "row hidden" }, [
        catFilter,
        el("span", { class: "faint", text: "0 个 = 全部；选多个 = 取并集" }),
        el("span", { class: "grow" }),
      ]);

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "分类建议" }),
          el("span", { class: "faint",
            text: "点一行选一篇，再让它写要点 / 纲要 / 分类建议；采纳前不会改你的 Zotero" }),
          el("span", { class: "grow" }),
          btnReload, btnColls,
        ]),
        el("div", { class: "sep" }),
        catRow,
        tableHost,
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [
          el("span", { class: "faint", text: "选中的文献：" }),
          selLabel,
          el("span", { class: "grow" }),
          countLabel,
        ]),
      ]));

      // ================================================================ 卡片二：对选中这一篇
      const actLabel = el("span", { class: "faint", text: "先在列表里点一篇文献。" });
      // 「调整建议」不单独占一个按钮：它就是**带一句 feedback 的**分类建议
      // （Tk 版 do_taxonomy_talk 最后也是转去 do_taxonomy_one），这里用一个输入框
      // 表达同一件事，少一个按钮、少一次点击。
      const fbInput = el("input", {
        type: "text", size: "42",
        placeholder: "调整意见（可选）：例如「这篇应归到分类 A」「这是设备类不是方法类」",
      });
      const btnSum = el("button", {
        class: "btn btn-primary", text: "生成要点",
        title: "给选中的这一篇写一段摘要要点（几百字，调一次模型）",
      });
      const btnDigest = el("button", {
        class: "btn btn-primary", text: "生成纲要",
        title: "按章节给要点 + 页码范围（比摘要详细、比全文短；一篇学位论文要 1~2 分钟）",
      });
      const btnCls = el("button", {
        class: "btn btn-primary", text: "分类建议",
        title: "让模型判断该归哪类、打什么标签；输入框里写了字就按那句话重判",
      });
      const btnApply = el("button", {
        class: "btn btn-danger", text: "应用建议",
        title: "把最近一次分类建议的分类 / 标签写回 Zotero（会二次确认）",
      });

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "对这一篇" }),
          actLabel,
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [btnSum, btnDigest, btnCls, btnApply]),
        el("div", { class: "row" }, [
          el("span", { class: "faint", text: "跟模型说一句" }),
          fbInput,
        ]),
        el("div", { class: "row" }, [
          el("span", { class: "faint",
            text: "「生成要点 / 纲要 / 分类建议」只调你配置的模型（本地 Ollama 或外部 API），"
                + "不动 Zotero；只有「应用建议」会写回你的 Zotero 库。" }),
        ]),
      ]));

      // ================================================================ 卡片三：输出
      const outHost = el("div", null,
        [el("div", { class: "faint", text: "（动作的结果或错误会显示在这里）" })]);

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "输出" }),
          el("span", { class: "faint", text: "最近一次动作的原样结果" }),
        ]),
        el("div", { class: "sep" }),
        outHost,
      ]));

      // ================================================================ 小工具

      /**
       * 输出区。等宽（.mono）+ 只读 —— 引擎回的是**原样文本**（子进程 / 模型输出），
       * 不解析成结构化字段（解析必然漂移，见契约三条硬口径第 1 条）。
       *
       * ⚠ 逐行塞进独立 div 而不是用 pre：pre 自带 UA 的 margin，而这里不许写
       *   内联 style 去压它；一行一个 div 既保住换行、又完全落在已有类上。
       */
      function showOut(text, kind) {
        outHost.textContent = "";
        let cls = "mono";
        if (kind === "err") cls = "mono note-err";
        else if (kind === "ok") cls = "mono note-ok";
        const box = el("div", { class: cls });
        const lines = String(text == null ? "" : text).split(/\r?\n/);
        lines.forEach(function (ln) {
          // 空行给一个空格，否则 div 高度塌成 0、看不出换行。
          box.appendChild(el("div", { text: ln === "" ? " " : ln }));
        });
        outHost.appendChild(box);
      }

      function setAct(text, kind) {
        actLabel.textContent = text;
        actLabel.className = kind === "err" ? "note note-err" : "faint";
      }

      /** 选中的那一篇的人读标题（作者 年份 · 标题 [KEY]）。 */
      function paperLabel(r) {
        return (r.first_author || "（无作者）") + " "
          + (r.year == null ? "" : String(r.year)) + " · "
          + clip(r.title, 60) + "  [" + String(r.key || "") + "]";
      }

      function updateActions() {
        const on = !!state.selKey;
        [btnSum, btnDigest, btnCls, btnApply].forEach(function (b) { b.disabled = !on; });
      }

      /** 选中一行（单选；点谁看谁）。 */
      function pick(r, tr) {
        tableHost.querySelectorAll("tbody tr").forEach(function (t) {
          t.classList.remove("selected");
        });
        if (tr) tr.classList.add("selected");
        state.selKey = String(r.key || "");
        // 换了文献，上一轮的建议就作废 —— 否则「应用建议」可能把 A 的分类写到 B 上。
        state.sugg = null;
        const lab = paperLabel(r);
        selLabel.textContent = lab;
        selLabel.title = String(r.title || "");
        setAct("已选中 " + state.selKey);
        updateActions();
      }

      /** 取不到选中文献时统一提示（四个动作都先过它）。 */
      function needKey() {
        if (!state.selKey) {
          showOut("先在列表里点一篇文献，再点这个按钮。", "err");
          setAct("还没选文献", "err");
          return "";
        }
        return state.selKey;
      }

      // ================================================================ 文献列表

      /**
       * 当前分类筛选下的行。
       * **0 个分类 = 全部**；多个 = **并集**（"在这几个分类里任一个就算"）。
       * 一篇归到多个分类时只算一行（filter 不做去重，本来也不会重复）。
       */
      function filteredPapers() {
        const all = state.papers || [];
        if (!state.cats.length) return all.slice();
        const want = {};
        state.cats.forEach(function (c) { want[c] = 1; });
        return all.filter(function (r) {
          return (r.collections || []).some(function (c) { return want[c]; });
        });
      }

      function renderPapers() {
        const rows = filteredPapers();
        tableHost.textContent = "";
        if (!state.papers.length) {
          tableHost.appendChild(el("div", { class: "note note-warn",
            text: "知识库索引里还没有文献 —— 先去「高级」页点一次「增量更新」建索引。" }));
          countLabel.textContent = "";
          updateActions();
          return;
        }
        if (rows.length) drawTable(rows);
        else {
          tableHost.appendChild(el("div", { class: "note note-warn",
            text: "选中的这些分类下一篇都没有 —— 换一个分类，或点「全部分类」清除筛选。" }));
        }

        // 当前选中的那一篇还在筛出来的表里就留着（切分类不该把手上选的那篇弄丢，
        // 也不该白白作废已经生成的建议）；不在就退回"未选中"，再自动选中第一条
        // —— 与经验页一致，否则右边四个按钮全灰、看着像「这一页坏了」。
        const keep = !!state.selKey && rows.some(function (r) {
          return String(r.key) === state.selKey;
        });
        if (!keep) {
          state.selKey = "";
          state.sugg = null;
          selLabel.textContent = "（还没选）";
          selLabel.title = "";
          setAct("先在列表里点一篇文献。");
        }
        const trs = tableHost.querySelectorAll("tbody tr");
        if (!keep && rows[0] && trs[0]) {
          pick(rows[0], trs[0]);
        } else {
          updateActions();
        }

        // 计数行（原来在 loadPapers 里；筛选在本地做之后归到这儿才对得上）。
        const nAll = state.papers.length;
        countLabel.textContent = (state.cats.length
            ? ("筛 " + state.cats.length + " 个分类：" + rows.length + " 篇")
            : ("这一屏 " + rows.length + " 篇"))
          + (nAll !== rows.length ? "（库里共 " + nAll + "）" : "");
      }

      function drawTable(rows) {
        // ⚠ 第 2 列会被 admin.css 的 `td:nth-child(2)` 规则当成「名字 + 说明」那种
        //   长列掐成一行（单行 + 省略号）。所以把**短**的「年份 · 作者」放第 2 列、
        //   最长的「标题」放第 3 列自己掐并挂 title —— 与结构表/经验表同一套做法。
        const cols = [
          // 表头排序口径见 admin.js::table：四列都可排（都是有序语义的短值）。
          { key: "key", label: "key", mono: true, sortable: true },
          { key: "author_year", label: "年份 · 作者", sortable: true,
            sortValue: function (r) {
              // 先按年份（数字）再看作者 —— 直接排显示串的话 "2026 · …" 会与
              // "（无作者）" 混着比。数字与文本混排由 compare() 处理（数字在前）。
              const y = Number(r.year);
              return isFinite(y) ? y : String(r.first_author || "");
            },
            render: function (r) {
              return String(r.year == null ? "" : r.year) + " · "
                + String(r.first_author || "（无作者）");
            } },
          { key: "title", label: "标题", sortable: true,
            sortValue: function (r) { return String(r.title || ""); },
            render: function (r) {
              const t = String(r.title || "");
              const s = el("span", { text: clip(t, 58) || "（无标题）" });
              s.title = t;
              return s;
            } },
          { key: "collections", label: "分类", sortable: true,
            sortValue: function (r) {
              return (Array.isArray(r.collections) ? r.collections : []).join("、");
            },
            render: function (r) {
              const arr = Array.isArray(r.collections) ? r.collections : [];
              const full = arr.join("、");
              const s = el("span", { text: clip(full, 24) || "（未分类）" });
              s.title = full || "（未分类）";
              return s;
            } },
        ];

        // 选中态交给 rowKey/rowClass：切分类会整表重建，靠它才能在重画后回到那一行。
        tableHost.appendChild(ctx.table(cols, rows, {
          rowKey: function (r) { return r.key; },
          rowClass: function (r) {
            return String(r.key) === state.selKey ? "selected" : "";
          },
          onRowClick: function (r, tr) { pick(r, tr); },
        }));
      }

      async function loadPapers() {
        countLabel.textContent = "正在读取…";
        tableHost.textContent = "";
        tableHost.appendChild(el("div", { class: "note note-warn",
          text: "正在读文献列表…" }));
        let res = null;
        try {
          res = await ctx.get("/papers");
        } catch (e) {
          tableHost.textContent = "";
          tableHost.appendChild(el("div", { class: "note note-err",
            text: "读文献列表失败：" + msgOf(e) }));
          countLabel.textContent = "";
          showOut("读文献列表失败：" + msgOf(e), "err");
          ctx.logAuto("读文献列表失败：" + msgOf(e));
          return;
        }
        const rows = (res && res.rows) || (Array.isArray(res) ? res : []);
        const total = (res && res.total != null) ? res.total : rows.length;
        state.papers = rows;
        // 分类候选**从这批行里聚合**（每行自带 collections）—— 一个请求都不多发。
        // 当前选中的分类若这次数据里没有了，setOptions 会把它剔掉（否则筛出空表）。
        const opts = KBAdmin.collectionOptions(rows);
        catFilter.setOptions(opts);
        catRow.classList.toggle("hidden", !opts.length);
        renderPapers();
        ctx.logAuto("文献列表：" + rows.length + "/" + total + " 篇 · 分类 "
                    + opts.length + " 个");
      }

      // ================================================================ 四个动作

      /** 生成要点：POST /summarize（120s，模型要跑一会儿）。 */
      async function doSummarize() {
        const key = needKey();
        if (!key) return;
        setAct("正在生成要点（" + key + "）…");
        showOut("生成要点 " + key + "：请求已发出…");
        try {
          const r = await ctx.post("/summarize", { key: key }, null, 120000);
          const s = String((r && r.summary) || "");
          showOut("生成要点 " + key
            + (r && r.ms != null ? "（" + r.ms + " ms）" : "") + "：\n" + s,
            s ? "ok" : "err");
          setAct("要点生成完成（" + s.length + " 字）");
          ctx.logAuto("生成要点 " + key + "：" + s.length + " 字");
        } catch (e) {
          showOut("生成要点失败：" + msgOf(e), "err");
          setAct("生成要点失败", "err");
          ctx.logAuto("生成要点失败：" + msgOf(e));
        }
      }

      /**
       * 生成纲要：复用 POST /digest，但它是**长任务**（按节调模型，一篇学位论文
       * 要 1~2 分钟）—— 派发即返回 {job:{id}}，之后由壳轮询 /jobs/<id>。
       * 不要在这里自己 setInterval 猜进度（契约三条硬口径：进度靠轮询 job.log）。
       */
      async function doDigest() {
        const key = needKey();
        if (!key) return;
        setAct("正在生成纲要（长任务，按节调模型）…");
        showOut("生成纲要 " + key + "：已提交，正在轮询进度…");
        try {
          const job = await ctx.runTask({
            submit: function () { return ctx.post("/digest", { key: key }); },
            onTick: function (j) {
              const m = j && (j.message || j.state);
              if (m) setAct("纲要：" + m);
            },
          });
          const result = (job && job.result) || {};
          const text = result.output || result.text
            || (Array.isArray(result.sections)
                ? ("纲要：" + result.sections.length + " 节") : "");
          showOut("生成纲要 " + key + " 完成"
            + (text ? ("\n" + text) : "（完成，没带回文本）"), "ok");
          setAct("纲要生成完成");
          ctx.logAuto("生成纲要 " + key + " 完成");
        } catch (e) {
          showOut("生成纲要失败：" + msgOf(e), "err");
          setAct("生成纲要失败", "err");
          ctx.logAuto("生成纲要失败：" + msgOf(e));
        }
      }

      /**
       * 分类建议：POST /classify（120s）。
       *
       * ⚠ 带上上一轮结果（previous）—— Tk 版「调整建议」就是把上一轮结论 +
       *   用户那句话一起发回去重判；这里同样，只是输入框常驻、少一个按钮。
       */
      async function doClassify() {
        const key = needKey();
        if (!key) return;
        const fb = fbInput.value.trim();
        const body = { key: key };
        if (fb) body.feedback = fb;
        if (state.sugg && String(state.sugg.key || "") === key) {
          body.previous = state.sugg;
        }
        setAct("正在让模型判断分类" + (fb ? "（按你这句话重判）" : "") + "…");
        showOut("分类建议 " + key + "：请求已发出…");
        try {
          const r = await ctx.post("/classify", body, null, 120000);
          state.sugg = r || null;
          const tags = ((r && r.tags) || []);
          showOut("分类建议 " + key + (fb ? "（已带你的调整意见）" : "") + "\n"
            + "分类：" + ((r && r.category) || "（没给）") + "\n"
            + "标签：" + (tags.length ? tags.join("、") : "（没给）") + "\n"
            + "理由：" + ((r && r.reason) || "（没给）"), "ok");
          setAct("建议已生成 —— 点「应用建议」才会写回 Zotero");
          ctx.logAuto("分类建议 " + key + "："
            + ((r && r.category) || "（无分类）"));
        } catch (e) {
          showOut("分类建议失败：" + msgOf(e), "err");
          setAct("分类建议失败", "err");
          ctx.logAuto("分类建议失败：" + msgOf(e));
        }
      }

      /**
       * 应用建议：POST /apply-suggestion —— **真的写用户的 Zotero**。
       * 二次确认里必须把「分类 = …、标签 = …」摊开，用户要先看清写什么。
       */
      async function doApply() {
        const s = state.sugg;
        if (!s || !s.key) {
          showOut("还没有可应用的分类建议 —— 先点「分类建议」生成一个。", "err");
          setAct("还没有建议", "err");
          return;
        }
        const cat = String(s.category || "");
        const tags = Array.isArray(s.tags) ? s.tags : [];
        if (!cat && !tags.length) {
          showOut("这条建议里既没有分类也没有标签，没什么可应用。", "err");
          return;
        }
        const body = el("div", null, [
          el("div", { class: "mono", text: String(s.key) }),
          el("div", { text: "分类 = " + (cat || "（不改）") }),
          el("div", { text: "标签 = " + (tags.length ? tags.join("、") : "（不改）") }),
          el("div", { class: "faint",
            text: "这会真的修改你的 Zotero 库，并同步到 zotero.org / 坚果云。" }),
        ]);
        const ok = await confirmBox(ctx, "确认应用到 Zotero？", body,
                                    "写入 Zotero", true);
        if (!ok) { setAct("已取消，没有写 Zotero"); return; }
        setAct("正在写回 Zotero…");
        showOut("应用建议 " + s.key + "：请求已发出…");
        try {
          const r = await ctx.post("/apply-suggestion",
            { key: s.key, category: cat, tags: tags }, null, 120000);
          const applied = (r && r.applied) || r || {};
          showOut("已应用 " + s.key + "：\n"
            + "分类 → " + (cat || "（不改）") + "\n"
            + "标签 → " + (tags.length ? tags.join("、") : "（不改）")
            + "\n引擎回包：" + JSON.stringify(applied), "ok");
          setAct("已写回 Zotero");
          ctx.logAuto("已应用分类建议 " + s.key);
        } catch (e) {
          showOut("应用建议失败：" + msgOf(e), "err");
          setAct("应用失败", "err");
          ctx.logAuto("应用分类建议失败：" + msgOf(e));
        }
      }

      /** 只看分类分布：POST /maintain {task:"collections"}（不调模型，只统计）。 */
      async function doCollections() {
        setAct("正在统计分类分布…");
        showOut("统计分类分布：请求已发出…");
        try {
          const r = await ctx.post("/maintain",
            { task: "collections" }, null, 120000);
          const out = (r && r.output != null) ? String(r.output)
                    : JSON.stringify(r || {});
          showOut("分类分布\n" + out, "ok");
          setAct("分类分布统计完成");
          ctx.logAuto("分类分布统计完成");
        } catch (e) {
          showOut("统计分类分布失败：" + msgOf(e), "err");
          setAct("统计失败", "err");
          ctx.logAuto("统计分类分布失败：" + msgOf(e));
        }
      }

      // ================================================================ 接线
      btnReload.addEventListener("click", function () { loadPapers(); });
      btnColls.addEventListener("click", function () { doCollections(); });
      btnSum.addEventListener("click", function () { doSummarize(); });
      btnDigest.addEventListener("click", function () { doDigest(); });
      btnCls.addEventListener("click", function () { doClassify(); });
      btnApply.addEventListener("click", function () { doApply(); });
      fbInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") doClassify();
      });

      updateActions();
      // ⚠ 这里**不用 await**：tools/check_js_syntax.py 的「非 async 函数里用了
      //   await」那一条有索引漂移的假阳性（范围在 code 上量、却去切删短注释后的
      //   文本，见 experience.js 的同一段注释）。`return` 这个 Promise 语义一样，
      //   顺带绕开它。
      return loadPapers();
    },
  });
})();
