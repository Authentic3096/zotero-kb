/* ============================================================================
 * admin-pages/quality.js —— 「损坏查询」页（坏切片、谁不可信、能不能修）
 *
 * 为什么值得单独一页：PDF 提取失败**从界面上看不出来** —— 检索命中了、片段也显示了，
 * 但内容是 `HVHDUFK DQG 7HVW`（其实是 "Research and Test" 每个字母 -3）或
 * `ऍնླbັຩቔູ`（混进藏文码位）这种整段乱码。规则看不出来，得让模型判。
 *
 * Tk 面板里的对应实现是 `tools/panels/tab_quality.py（已退役 2026-10-14）`（刷新统计 / 检查损坏切片 /
 * 只跑规则 / 重建不可信的 / 看某篇明细）。本页覆盖同样的功能，布局按"简约干净"重做：
 * 顶部一行动作 + 一行统计 + 一张表 + 底下一块只读明细，点行即看明细。
 *
 * 数据来源照 docs/ADMIN-API-W2B.md 的损坏查询那一组：
 *   GET  /quality-stats   → quality.stats  → {chunks, figures, item_health,
 *                            rows:[{key,title,n_bad,n_suspect}]}
 *   POST /quality-scan    {use_model?}     → 长任务 {job:{id}}；
 *                            job.result = {items:[{key,title,issue,detail}], summary}
 *   POST /quality-repair  {keys?[]}        → 长任务 {job:{id}}
 *   GET  /quality-detail  ?key=<key>       → {key, title,
 *                            rows:[{page,kind,detail,snippet}]}
 *   长任务都是"派发即返回"，之后由 ctx.runTask 轮询 GET /jobs/<id>。
 *
 * 四个取舍（为什么这么做）：
 *   · 明细区用**只读 textarea** 而不是 div：长 snippet 能滚动、还能让用户选中复制
 *     （Tk 版那个 ScrolledText 就是同一用途），给 class="mono" 让它等宽。
 *     页面里**不写内联样式**（约定），等宽只靠 admin.css 已有的 mono 档。
 *   · 「重建不可信」把当前统计里"有问题的篇"的 key 显式传下去：用户点之前看到的
 *     名单，就是将要处理的名单。统计里一篇都没有时不传 keys，交给引擎自己判。
 *   · 不生成摘要文本以外的任何解析：契约里 job.result.summary 是**不透明**的，
 *     这里只认几个常见键（bad / unclear / fixed / failed / skipped / elapsed），
 *     认不出来就照实说"引擎没给可读的摘要"，不假装看懂了。
 *   · 不做"全库重建后体检"（Tk 版那个一条龙按钮）：W2B 契约里没有对应端点，
 *     而它本质是"重建 + 修复"两件事，本页只做契约给的那两件。
 *
 * 页面约定照抄 admin.js 顶部三条与 structure.js 的写法：只用 admin.css 的类、
 * 数据一律 KBAdmin.get/post、一页一文件、注释说清为什么。
 * 提醒：块注释里不要写"星号紧接斜杠"那两个字符，也不能让通配类名紧邻斜杠 ——
 *   它们会把注释提前闭合（本目录 10 个脚本原来全是这么坏的）。类名一律用
 *   "某某系列"描述。
 * ==========================================================================*/
(function () {
  "use strict";

  function msgOf(e) {
    return (e && e.message) ? e.message : String(e);
  }

  /** 单行截断：格子里只留关键词，全文进 title 悬停。 */
  function short(text, n) {
    const s = String(text == null ? "" : text);
    return s.length > n ? (s.slice(0, n) + "…") : s;
  }

  /** 千位分隔（不用"万/亿"：这里是计数，和旁边的条数混在一屏时换算反而添乱）。 */
  function humanInt(v) {
    const n = Number(v);
    if (!isFinite(n)) return (v == null || v === "") ? "—" : String(v);
    return n.toLocaleString("en-US");
  }

  KBAdmin.page({
    id: "quality",
    title: "损坏查询",
    order: 60,

    async render(host, ctx) {
      const el = ctx.el;
      const dialog = ctx.dialog;
      const runTask = ctx.runTask;
      // 页面级状态（切走再切回来会重新 render，不沿用上一次的残影）。
      const state = { rows: [], busy: false, selKey: "", selTr: null };

      // ------------------------------------------------------ 顶部：动作行
      //
      // 档位照契约三档：「检查损坏切片」是这一页的日常入口 → 主；
      // 「重建不可信」会改本地正文数据 → 破坏色 + 二次确认。
      const ruleCk = el("input", { type: "checkbox" });
      const ruleLabel = el("label", { class: "inline" },
        [ruleCk, el("span", { text: "　只跑规则（不用模型）" })]);
      ruleLabel.title = "勾上就只用字符/编码规则扫（秒级、不调模型）；"
        + "不勾会再让本地模型逐条判一遍（全库几分钟，但乱码识别准得多）";

      const scanBtn = el("button", {
        class: "btn btn-primary", text: "检查损坏切片",
        title: "扫全库切片，判「字符错乱 / 编码错乱 / 只剩符号」这类能检索到却读不出东西的正文",
        on: { click: function () { runScan(); } },
      });
      const repairBtn = el("button", {
        class: "btn btn-danger", text: "重建不可信",
        title: "绕开 Zotero 的文本缓存、直接用 PyMuPDF 重新提取被判不可信的正文（会二次确认）",
        on: { click: function () { askRepair(); } },
      });
      const note = el("div", { class: "note hidden", text: "" });

      const toolCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "损坏查询" }),
          el("span", { class: "faint",
                       text: "这类切片能检索到、却读不出东西，还会污染向量" }),
          el("span", { class: "grow" }),
          scanBtn, ruleLabel, repairBtn,
        ]),
        el("div", { class: "sep" }),
        note,
      ]);
      host.appendChild(toolCard);

      // ------------------------------------------------------ 统计 + 结果表
      const statRow = el("div", { class: "row" },
        [el("span", { class: "subtle", text: "正在读统计…" })]);
      const statsNote = el("div", { class: "faint", text: "" });
      const tableHost = el("div", null,
        [el("div", { class: "note note-warn", text: "正在读统计…" })]);
      const tableCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "有问题的切片" }),
          el("span", { class: "faint", text: "按篇聚合；点一行看那一篇的明细" }),
          el("span", { class: "grow" }),
        ]),
        el("div", { class: "sep" }),
        statRow,
        el("div", { class: "sep" }),
        tableHost,
        el("div", { class: "sep" }),
        statsNote,
      ]);
      host.appendChild(tableCard);

      // ------------------------------------------------------ 明细（只读）
      const detailHint = el("div", { class: "faint",
        text: "看明细需要先选中一行 —— 点上面表格里的一行。" });
      const detailTa = el("textarea", {
        class: "mono", rows: "14", cols: "64", readonly: "readonly",
        placeholder: "点上面表格里的一行，这里会显示那一篇的坏切片逐条明细",
      });
      const detailCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "明细" }),
          el("span", { class: "faint", text: "只读，可选中复制" }),
        ]),
        el("div", { class: "sep" }),
        detailHint,
        detailTa,
      ]);
      host.appendChild(detailCard);

      // ------------------------------------------------------ 小工具
      function setNote(text, kind) {
        note.className = "note" + (kind ? " note-" + kind : "") + (text ? "" : " hidden");
        note.textContent = text || "";
      }

      function setBusy(on) {
        state.busy = !!on;
        scanBtn.disabled = state.busy;
        repairBtn.disabled = state.busy;
        ruleCk.disabled = state.busy;
      }

      /** 从 job 状态里挑一句最有用的话（message 优先，其次日志尾行）。 */
      function tickText(job) {
        const j = job || {};
        const logs = Array.isArray(j.log) ? j.log : [];
        const last = logs.length ? String(logs[logs.length - 1]) : "";
        const msg = String(j.message || "");
        const pct = (j.percent == null || j.percent === "") ? "" : "（" + j.percent + "%）";
        return (msg || last || "运行中…") + pct;
      }

      /**
       * summary 在契约里是**不透明**的：只认几个常见键，认不出就照实说。
       * （宁可说"引擎没给可读的摘要"，也不能把对象 JSON 塞进用户眼前。）
       */
      function summaryText(s) {
        if (!s) return "引擎没给摘要";
        if (typeof s === "string") return s;
        const parts = [];
        if (s.total != null) parts.push("共 " + s.total + " 篇");
        if (s.bad != null) parts.push("不可信 " + s.bad + " 篇");
        if (s.unsure != null) parts.push("待人工确认 " + s.unsure + " 篇");
        if (s.fixed != null) parts.push("修好 " + s.fixed + " 篇");
        if (s.failed != null) parts.push("仍不可信 " + s.failed + " 篇");
        if (s.skipped != null) parts.push("跳过 " + s.skipped + " 篇");
        if (s.elapsed != null) parts.push("耗时 " + s.elapsed + " 秒");
        if (s.text) parts.push(String(s.text));
        return parts.length ? parts.join("　·　") : "引擎没给可读的摘要";
      }

      // ------------------------------------------------------ 结果表
      // 三列都可排（口径见 admin.js::table）：文献按标题、两个计数按数字原值。
      const cols = [
        { key: "title", label: "文献", sortable: true,
          sortValue: function (r) { return String(r.title || ""); },
          render: function (r) {
            const s = el("span", null, [
              el("span", { class: "mono", text: String(r.key || "") }),
              el("span", { class: "faint", text: "　" }),
              el("span", { text: short(String(r.title || "（无标题）"), 40) }),
            ]);
            s.title = String(r.key || "") + "　" + String(r.title || "");
            return s;
          } },
        { key: "n_bad", label: "坏切片", sortable: true,
          sortValue: function (r) { return Number(r.n_bad || 0); },
          render: function (r) {
            const n = Number(r.n_bad || 0);
            // 不可信的那一栏才是"要动手"的：给它危险色；0 用淡色留白，别满屏刺眼。
            // n 档负责数字右边缘对齐（admin.css 里那个定宽盒），tag-bad 只是上色。
            if (!n) return el("span", { class: "faint", text: "0" });
            return el("span", { class: "n tag-bad", text: String(n) });
          } },
        { key: "n_suspect", label: "疑似", sortable: true,
          sortValue: function (r) { return Number(r.n_suspect || 0); },
          render: function (r) {
            const n = Number(r.n_suspect || 0);
            if (!n) return el("span", { class: "faint", text: "0" });
            return el("span", { class: "n tag-warn", text: String(n) });
          } },
      ];

      // ------------------------------------------------------ 读统计
      async function load() {
        statsNote.textContent = "正在读统计…";
        tableHost.textContent = "";
        tableHost.appendChild(el("div", { class: "note note-warn", text: "正在读统计…" }));
        try {
          const d = await ctx.get("/quality-stats");
          const rows = (d && Array.isArray(d.rows)) ? d.rows : [];
          state.rows = rows;
          drawStats(d, rows);
          drawTable(rows);
          restoreSelection();
          ctx.log("损坏查询：" + rows.length + " 篇有问题的切片");
        } catch (e) {
          state.rows = [];
          statRow.textContent = "";
          statRow.appendChild(el("div", { class: "note note-err",
            text: "读统计失败：" + msgOf(e) }));
          tableHost.textContent = "";
          tableHost.appendChild(el("div", { class: "note note-err",
            text: "读统计失败：" + msgOf(e) + "（引擎没起来时也会这样）" }));
        }
      }

      /** 统计行：切片 / 图注 / 有问题的篇数（照任务口径），其余压进底下那行说明。 */
      function drawStats(d, rows) {
        const chunks = (d && d.chunks != null) ? d.chunks : "";
        const figs = (d && d.figures != null) ? d.figures : "";
        const checked = (d && d.item_health != null) ? Number(d.item_health) : 0;
        let bad = 0, sus = 0;
        rows.forEach(function (r) {
          bad += Number(r.n_bad || 0);
          sus += Number(r.n_suspect || 0);
        });

        const stat = function (label, value, title) {
          const num = el("div", { class: "num", text: String(value) });
          if (title) num.title = title;
          return el("div", { class: "stat" }, [
            num, el("div", { class: "num-label", text: label }),
          ]);
        };
        statRow.textContent = "";
        statRow.appendChild(stat("切片", humanInt(chunks), "索引库 chunks 表里的切片总数"));
        statRow.appendChild(stat("图注", humanInt(figs), "图注与表格（figures 表）的条数"));
        statRow.appendChild(stat("有问题的篇数", humanInt(rows.length),
          "至少有一条坏切片或疑似切片的文献"));

        if (!checked) {
          statsNote.textContent = "还没体检过 —— 点「检查损坏切片」跑一次"
            + "（勾上「只跑规则」是秒级，不勾会调本地模型、全库几分钟）。";
        } else if (!rows.length) {
          statsNote.textContent = "✓ 已体检 " + humanInt(checked) + " 篇，"
            + "没有查出不可信或疑似的正文提取。";
        } else {
          statsNote.textContent = "已体检 " + humanInt(checked) + " 篇"
            + "　·　坏切片 " + humanInt(bad) + " 条"
            + "　·　疑似 " + humanInt(sus) + " 条"
            + "　·　点一行看明细，「重建不可信」会绕开 Zotero 缓存重新提取。";
        }
      }

      function drawTable(rows) {
        tableHost.textContent = "";
        if (!rows.length) {
          tableHost.appendChild(el("div", { class: "note note-ok",
            text: "没有查出坏切片（或者还没体检过）。" }));
          return;
        }
        tableHost.appendChild(ctx.table(cols, rows, {
          onRowClick: function (r, tr) { pick(r, tr); },
        }));
      }

      /** 重画表之后把选中态找回来（否则扫一遍回来高亮就丢了，像没选中）。 */
      function restoreSelection() {
        state.selTr = null;
        if (!state.selKey) return;
        const trs = tableHost.querySelectorAll("tbody tr");
        for (let i = 0; i < state.rows.length && i < trs.length; i++) {
          if (String(state.rows[i].key || "") === state.selKey) {
            trs[i].classList.add("selected");
            state.selTr = trs[i];
            return;
          }
        }
      }

      function pick(r, tr) {
        if (state.selTr && state.selTr.classList) state.selTr.classList.remove("selected");
        state.selTr = tr || null;
        if (tr && tr.classList) tr.classList.add("selected");
        state.selKey = String(r.key || "");
        showDetail(state.selKey, String(r.title || ""));
      }

      // ------------------------------------------------------ 某篇的明细
      async function showDetail(key, title) {
        if (!key) return;
        detailHint.className = "faint";
        detailHint.textContent = "正在读 " + key + " 的明细…";
        detailTa.value = "";
        try {
          const d = await ctx.get("/quality-detail?key=" + encodeURIComponent(key));
          const metas = (d && Array.isArray(d.rows)) ? d.rows : [];
          const lines = [];
          lines.push(String((d && d.key) || key) + "　"
            + String((d && d.title) || title || ""));
          lines.push("");
          if (!metas.length) {
            lines.push("（引擎没有回明细 —— 这一篇可能已经修好，"
              + "或者体检记录里只有汇总。）");
          }
          metas.forEach(function (m, i) {
            const where = (m.page == null || m.page === "") ? "" : ("第 " + m.page + " 页");
            const kind = String(m.kind || "");
            const head = [where, kind].filter(Boolean).join(" · ");
            lines.push("--- " + (i + 1) + "/" + metas.length + " ---"
              + (head ? "  " + head : ""));
            if (m.detail) lines.push("　" + String(m.detail));
            if (m.snippet) lines.push("　" + String(m.snippet).replace(/\n/g, "\n　"));
            lines.push("");
          });
          detailTa.value = lines.join("\n");
          detailHint.textContent = String((d && d.key) || key) + "："
            + metas.length + " 条明细（只读，可选中复制）";
        } catch (e) {
          detailHint.className = "note note-err";
          detailHint.textContent = "读明细失败：" + msgOf(e);
          detailTa.value = "";
        }
      }

      // ------------------------------------------------------ 检查（长任务）
      async function runScan() {
        if (state.busy) return;
        const useModel = !ruleCk.checked;
        const label = useModel ? "检查损坏切片（规则 + 本地模型）"
                               : "检查损坏切片（只跑规则）";
        setBusy(true);
        setNote(label + "：已派发，等引擎…（告一段落前别关这一页；进度看这行）", "ok");
        try {
          const job = await runTask({
            submit: function () {
              return ctx.post("/quality-scan", { use_model: useModel });
            },
            interval: 1500,
            onTick: function (j) { setNote(label + "：" + tickText(j), "ok"); },
          });
          const res = (job && job.result) || {};
          const items = Array.isArray(res.items) ? res.items : [];
          const line = summaryText(res.summary)
            + (items.length ? "　·　可能有问题的 " + items.length + " 篇" : "");
          setNote(label + "：完成 —— " + line, "ok");
          ctx.logAuto("[损坏查询] " + label + " 完成：" + line);
          await load();
        } catch (e) {
          setNote(label + "：失败 —— " + msgOf(e), "err");
          ctx.log("[损坏查询] " + label + " 失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      // ------------------------------------------------------ 重建不可信（长任务）
      function askRepair() {
        if (state.busy) return;
        const keys = state.rows.map(function (r) { return String(r.key || ""); })
                               .filter(Boolean);
        let bad = 0, sus = 0;
        state.rows.forEach(function (r) {
          bad += Number(r.n_bad || 0);
          sus += Number(r.n_suspect || 0);
        });
        dialog("重建不可信正文", el("div", null, [
          el("div", { text: keys.length
            ? ("会重新提取当前统计里这 " + keys.length + " 篇的正文"
               + "（坏切片 " + bad + " 条、疑似 " + sus + " 条）。")
            : "统计里一篇有问题的都没有；点下去引擎会自己再筛一遍，通常什么也不做。" }),
          el("div", { class: "faint",
            text: "做法是绕开 Zotero 的文本缓存、直接用 PyMuPDF 读 PDF，然后重建切片与向量。" }),
          el("div", { class: "faint",
            text: "扫描件（没有文字层）修不了，会跳过并在日志里说明原因。" }),
        ]), [
          { text: "取消" },
          { text: "开始重建", danger: true, run: function () { doRepair(keys); } },
        ]);
      }

      async function doRepair(keys) {
        if (state.busy) return;
        setBusy(true);
        setNote("重建不可信：已派发，等引擎…（逐篇重建，看这行的日志）", "ok");
        try {
          const body = {};
          // 空数组不传：让引擎自己决定处理范围（免得把"什么都不做"写死在这一页）。
          if (keys && keys.length) body.keys = keys;
          const job = await runTask({
            submit: function () { return ctx.post("/quality-repair", body); },
            interval: 1500,
            onTick: function (j) { setNote("重建不可信：" + tickText(j), "ok"); },
          });
          const res = (job && job.result) || {};
          const line = summaryText(res.summary || res);
          setNote("重建不可信：完成 —— " + line, "ok");
          ctx.logAuto("[损坏查询] 重建不可信完成：" + line);
          await load();
          // 明细跟着刷新：修过的那些条目，明细里的问题应该少了或没了。
          if (state.selKey) await showDetail(state.selKey, "");
        } catch (e) {
          setNote("重建不可信：失败 —— " + msgOf(e), "err");
          ctx.log("[损坏查询] 重建不可信失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      // ------------------------------------------------------ 起步
      //
      // 不 await load()：页面骨架已经画好，统计与表是异步补上的（load 自己
      // try/catch，失败写进统计位与表格位）。
      setNote("");
      load();
      ctx.log("损坏查询页：开始读统计（检查不自动跑）");
    },
  });
})();
