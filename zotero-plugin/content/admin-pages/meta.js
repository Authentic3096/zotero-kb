/* ============================================================================
 * admin-pages/meta.js —— 「元数据」页（批量补元数据的扫描与核对）
 *
 * Tk 面板里的对应实现是 `tools/panels/tab_meta.py（已退役 2026-10-14）`（一键补全 / 模型补全 / 取消 /
 * 检查类型与标题污染 / 看某篇的建议明细）。本页覆盖同样的功能，布局按"简约"重做：
 * 顶上一行按钮、一行统计、一张结果表，点某一行看明细（不再另放一个"看明细"按钮）。
 *
 * 数据来源照 docs/ADMIN-API-W2.md 的批次 A：
 *   POST /metafill-scan        {use_model?,limit?}
 *        → meta.fillScan  → {rows:[{key,title,high,low,skipped,notes}], summary}
 *   POST /metafill-scan-cancel {}
 *        → meta.fillCancel → {cancelled:bool}
 *   POST /typefix-scan         {use_network?}
 *        → meta.typeScan  → {rows:[{key,title,issue,suggest}]}
 * 长任务形态（回 {job:{id}}）也接得住：`submit()` 里判到 job 就轮询 /jobs/<id>，
 * 拿到 job.result 当最终数据 —— 契约把"长任务"写成可选，两种形状都实现一次，
 * 免得引擎那边选哪种都要回来改页面。
 *
 * ⚠ 这一页**只读**：不写 Zotero、不写索引库（写回元数据统一留在 Zotero 插件里，
 *   插件在 Zotero 进程内能直接 item.setField + saveTx；面板是另一个进程，写不了库。
 *   这是用户拍板的形态，见 tab_meta.py 的注释）。
 * ⚠ 扫描**不自动跑**：一页打开就扫全库 PDF 属于"替用户做决定"，而且很吵；
 *   打开时只画表头与一句提示，等用户点按钮。
 * ==========================================================================*/
(function () {
  "use strict";

  /** 高/低置信建议的条数：契约给数组，Tk 那边的旧形状给 n_high/n_low，都认。 */
  function countOf(r, name) {
    const n = r["n_" + name];
    if (n != null) return Number(n) || 0;
    return Array.isArray(r[name]) ? r[name].length : 0;
  }

  /** notes 可能是数组也可能是字符串（老形状），统一成字符串数组。 */
  function notesOf(r) {
    if (Array.isArray(r.notes)) return r.notes.map(String);
    return r.notes ? [String(r.notes)] : [];
  }

  /** 单行截断（表格"说明"列只留关键词，全文放 title 悬停）。 */
  function short(text, n) {
    const s = String(text == null ? "" : text);
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  /** 多行文案塞进弹窗（dialog 的 body 是 HTML，字符串里的换行会被折掉）。 */
  function msgLines(el, arr) {
    return el("div", null, (arr || []).map(function (t) {
      return el("div", { text: String(t) });
    }));
  }

  KBAdmin.page({
    id: "meta",
    title: "元数据",
    order: 70,

    async render(host, ctx) {
      const el = ctx.el;
      const dialog = ctx.dialog;
      const runTask = ctx.runTask;
      // 页面级状态（切页回来会重新 render，不复用上一次的残影）。
      const state = { rows: [], busy: false, data: null };

      // ------------------------------------------------------------ 顶部：操作行
      //
      // 档位照契约：日常入口（纯规则、秒级）→ btn-primary；会调模型、慢十几分钟的
      // 那档保持中性、并且**先弹确认**；「取消」只在忙的时候能点 → btn-subtle + disabled。
      const ruleBtn = el("button", {
        class: "btn btn-primary", text: "一键补全（不用模型）",
        title: "对全库缺字段的文献扫一遍 PDF 首页，抽出日期/DOI/卷/期/页码/作者列成表。只读、秒级（纯规则）",
        on: { click: function () { scan("rule"); } },
      });
      const modelBtn = el("button", {
        class: "btn", text: "用模型补全",
        title: "对每篇缺字段的文献调一次本地小模型（慢，十几分钟），结果都算「低置信」，要自己核对",
        on: { click: function () { askModel(); } },
      });
      const cancelBtn = el("button", {
        class: "btn btn-subtle", text: "取消", disabled: "disabled",
        title: "请求服务端在当前这一篇算完后停下（已扫出来的部分会保留）",
        on: { click: function () { doCancel(); } },
      });
      const typeBtn = el("button", {
        class: "btn", text: "检查类型/标题污染",
        title: "找「类型不像正经文献、却挂着 PDF」的条目（常见于先存网页、后来挂 PDF），只报不改",
        on: { click: function () { doTypefix(); } },
      });

      const note = el("div", { class: "note hidden", text: "" });

      const toolCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "元数据补全" }),
          el("span", { class: "faint",
                       text: "只读扫描：不写 Zotero、不写索引库；写回统一在 Zotero 插件里做" }),
          el("span", { class: "grow" }),
          ruleBtn, modelBtn, cancelBtn, typeBtn,
        ]),
        el("div", { class: "sep" }),
        note,
      ]);
      host.appendChild(toolCard);

      // ------------------------------------------------------------ 结果卡片
      const summaryLine = el("div", { class: "subtle",
        text: "还没扫过 —— 点「一键补全（不用模型）」跑一次（只读、秒级）" });
      const tableHost = el("div");
      const tableCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "扫描结果" }),
          el("span", { class: "faint", text: "点一行看每条的取值与证据" }),
          el("span", { class: "grow" }),
          summaryLine,
        ]),
        el("div", { class: "sep" }),
        tableHost,
        el("div", { class: "sep" }),
        el("div", { class: "faint", text:
          "「高置信」是能从 PDF 首页固定格式里抽出来的（规则）；" +
          "「低置信」是本地小模型给的（会编，必须自己核对）。" +
          "「一键采用」在 Zotero 里：多选文献 → 右键 → 补全元数据。" }),
      ]);
      host.appendChild(tableCard);

      // ------------------------------------------------------------ 小工具
      function setNote(text, kind) {
        note.className = "note" + (kind ? " note-" + kind : "") + (text ? "" : " hidden");
        note.textContent = text || "";
      }

      function setBusy(on) {
        state.busy = !!on;
        ruleBtn.disabled = state.busy;
        modelBtn.disabled = state.busy;
        typeBtn.disabled = state.busy;
        // 取消反过来：忙的时候才能点（没在跑时取消没有意义）。
        cancelBtn.disabled = !state.busy;
      }

      // ------------------------------------------------------------ 结果表
      // 表头排序口径见 admin.js::table：文献按标题、两个计数列按数字原值（可排）；
      // 「说明」是长自由文本（拼了缺字段 / 类型 / 正文来源 / 备注），**不给排序**。
      const cols = [
        { key: "title", label: "文献", sortable: true,
          sortValue: function (r) { return String(r.title || r.item_type || ""); },
          render: function (r) {
            const s = el("span", null, [
              el("span", { class: "mono", text: String(r.key || "") }),
              el("span", { class: "faint", text: "　" }),
              el("span", { text: short(String(r.title || r.item_type || "（无标题）"), 46) }),
            ]);
            s.title = String(r.key || "") + "　" + String(r.title || "");
            return s;
          } },
        { key: "suggest", label: "建议（高 · 低）", sortable: true,
          sortValue: function (r) { return countOf(r, "high"); },
          render: function (r) {
            const h = countOf(r, "high"), l = countOf(r, "low");
            return el("span", null, [
              el("span", { class: h ? "tag-ok" : "faint", text: "高 " + h }),
              el("span", { class: "faint", text: "　·　" }),
              el("span", { class: l ? "tag-warn" : "faint", text: "低 " + l }),
            ]);
          } },
        { key: "skipped", label: "跳过", sortable: true,
          sortValue: function (r) { return countOf(r, "skipped"); },
          render: function (r) {
            const n = countOf(r, "skipped");
            if (!n) return el("span", { class: "faint", text: "—" });
            return el("span", { class: "n", text: String(n) });
          } },
        { key: "notes", label: "说明",
          render: function (r) {
            // 格子里只留关键词，全文进 title 悬停（契约的硬约束 3）。
            const bits = [];
            if (r.error) bits.push("出错");
            if (Array.isArray(r.missing) && r.missing.length) {
              bits.push("缺 " + r.missing.join("/"));
            }
            const notes = notesOf(r);
            if (notes.length) bits.push(notes[0]);
            if (!bits.length) bits.push("—");
            const s = el("span", {
              class: r.error ? "tag-bad" : null,
              text: short(bits.join("　·　"), 30),
            });
            s.title = [
              r.item_type ? ("类型：" + r.item_type) : "",
              r.fulltext_source ? ("正文来源：" + r.fulltext_source) : "",
              notes.join("\n"),
              r.error ? ("出错：" + r.error) : "",
            ].filter(Boolean).join("\n");
            return s;
          } },
      ];

      function drawRows(rows) {
        tableHost.textContent = "";
        if (!rows.length) {
          tableHost.appendChild(el("div", { class: "note note-warn",
            text: "这次扫描没有缺字段的条目（或者结果是空的）。" }));
          return;
        }
        tableHost.appendChild(ctx.table(cols, rows, {
          onRowClick: function (r) { showDetail(r); },
        }));
      }

      /** 打开时的空状态：先把表头画出来，让"结果长什么样"一眼可见。 */
      function drawEmpty() {
        tableHost.textContent = "";
        tableHost.appendChild(el("div", { class: "note note-warn",
          text: "还没有扫描结果 —— 点上方「一键补全（不用模型）」跑一次（只读、秒级）。" }));
        tableHost.appendChild(ctx.table(cols, [], {}));
      }

      // ------------------------------------------------------------ 统计行
      function summaryText(data) {
        const d = data || {};
        const rows = Array.isArray(d.rows) ? d.rows : [];
        let hi = 0, lo = 0, sk = 0;
        rows.forEach(function (r) {
          hi += countOf(r, "high");
          lo += countOf(r, "low");
          sk += countOf(r, "skipped");
        });
        const parts = [];
        if (d.total != null) parts.push("全库 " + d.total + " 篇");
        if (d.missing_total != null) parts.push("缺字段 " + d.missing_total + " 篇");
        if (d.scanned != null) parts.push("本次扫了 " + d.scanned + " 篇");
        parts.push("本页 " + rows.length + " 篇");
        parts.push("高置信 " + hi + " 条");
        parts.push("低置信 " + lo + " 条");
        if (sk) parts.push("跳过 " + sk + " 条");
        if (d.elapsed != null) parts.push("耗时 " + d.elapsed + " 秒");
        if (d.canceled) parts.push("⚠ 被取消，只有部分结果");
        return parts.join("　·　");
      }

      // ------------------------------------------------------------ 提交（长/短任务两种形状都接）
      /**
       * 提交一个扫描。
       *   · 短任务形态：直接回 {rows, summary}（Tk 面板用的就是这种）→ 原样返回；
       *   · 长任务形态：回 {job:{id}} → 轮询 /jobs/<id>，拿 job.result 当数据。
       * 这样引擎那边选哪种实现，这一页都不用改。
       */
      async function submit(path, body, label) {
        const first = await ctx.post(path, body);
        const jobId = first && first.job && first.job.id;
        if (!jobId) return first;
        const job = await runTask({
          id: jobId,
          interval: 1000,
          onTick: function (j) { setNote(label + "：" + tickText(j), "ok"); },
        });
        return (job && job.result) || job || {};
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

      // ------------------------------------------------------------ 一键补全
      async function scan(kind) {
        if (state.busy) return;
        const label = kind === "model" ? "用本地模型补全"
                                       : "一键补全（不用模型）";
        setBusy(true);
        setNote(label + "：扫描中…（可点「取消」，已扫出来的部分会保留）", "ok");
        try {
          const data = await submit("/metafill-scan",
                                    { use_model: kind === "model" }, label);
          state.data = data;
          state.rows = (data && data.rows) || [];
          drawRows(state.rows);
          summaryLine.textContent = summaryText(data);
          const line = label + "：完成 —— " + summaryText(data);
          setNote(line, (data && data.canceled) ? "warn" : "ok");
          ctx.log("[元数据] " + line, "ok");
        } catch (e) {
          setNote(label + "：失败 —— " + (e && e.message ? e.message : e)
                  + "（引擎/本地服务没起来时也会这样）", "err");
          ctx.log("[元数据] " + label + " 失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      /**
       * 「用模型补全」先确认再做：几十篇 × 每篇几秒 = 十几分钟，
       * 用户必须知道自己在等什么（照面板里其它长任务的规矩，先问再做）。
       */
      function askModel() {
        if (state.busy) return;
        dialog("用本地模型补全",
          msgLines(el, [
            "会对每一篇缺字段的文献都调一次本地小模型，几十篇大约要十几分钟",
            "（纯规则那档只要几秒）。",
            "",
            "模型给的都算「低置信」，要你自己核对。继续？",
          ]),
          [{ text: "取消" },
           { text: "开始扫描", primary: true, run: function () { scan("model"); } }]);
      }

      // ------------------------------------------------------------ 取消
      /**
       * 取消要**发到服务端**，不是本地忽略结果：扫描是服务端在跑，本地忽略的话
       * 那几十篇它照样跑完、照样占着数据库快照 —— 用户以为停了，其实没停。
       * 服务端在每篇之间检查取消标志（与 Tk 面板同一条路）。
       */
      async function doCancel() {
        try {
          const res = await ctx.post("/metafill-scan-cancel", {});
          const ok = res && res.cancelled;
          setNote("已发出取消请求（会在当前这一篇算完后停下）"
                  + (ok ? "" : "　·　服务端说当时没有在跑的任务"), "warn");
          ctx.log("[元数据] 取消请求：" + (ok ? "服务端已接收" : "服务端说没有在跑的任务"), "warn");
        } catch (e) {
          setNote("取消请求失败：" + (e && e.message ? e.message : e), "err");
          ctx.log("[元数据] 取消请求失败：" + e, "err");
        }
      }

      // ------------------------------------------------------------ 类型/标题污染
      async function doTypefix() {
        if (state.busy) return;
        setBusy(true);
        setNote("检查类型/标题污染：正在找「网页类型却挂着 PDF」的条目…", "ok");
        try {
          const data = await submit("/typefix-scan", { use_network: false },
                                    "检查类型/标题污染");
          const rows = (data && data.rows) || [];
          const head = "检查类型/标题污染：查出 " + rows.length + " 篇有嫌疑"
            + (data && data.checked != null ? "（共查了 " + data.checked + " 篇）" : "");
          setNote(head, rows.length ? "warn" : "ok");
          ctx.log("[类型修正] " + head, rows.length ? "warn" : "ok");
          showTypefix(rows);
        } catch (e) {
          setNote("类型/标题污染检查失败：" + (e && e.message ? e.message : e), "err");
          ctx.log("[类型修正] 检查失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      /** 类型污染的结果用弹窗（形状与主表不同，别硬塞进四列表里）。 */
      function showTypefix(rows) {
        const body = el("div", null, [
          el("div", { class: "faint", text:
            "这些条目「类型不像正经文献、却挂着 PDF」 —— 最常见的原因是先用浏览器" +
            "「保存网页」存下来、后来才把 PDF 拖上去，于是类型与标题还是网页的样子。" }),
          el("div", { class: "faint", text:
            "这一页不会替你改：改类型是敏感操作，请到 Zotero 里右键那一条走" +
            "「补全元数据（本地模型）」，它会先给你看「现在是什么 → 要改成什么」再写。" }),
          el("div", { class: "sep" }),
        ]);
        if (!rows.length) {
          body.appendChild(el("div", { class: "note note-ok",
            text: "没有发现嫌疑条目。" }));
        } else {
          body.appendChild(ctx.table([
            // 只有「文献」可排：「问题 / 建议」是自由文本，排它没有意义。
            { key: "title", label: "文献", sortable: true,
              sortValue: function (r) { return String(r.title || ""); },
              render: function (r) {
                const s = el("span", { text: short(String(r.title || "（无标题）"), 60) });
                s.title = String(r.key || "") + "　" + String(r.title || "");
                return s;
              } },
            { key: "issue", label: "问题",
              render: function (r) {
                return el("span", { text: short(String(r.issue || "—"), 40) });
              } },
            { key: "suggest", label: "建议",
              render: function (r) {
                const s = el("span", { text: short(String(r.suggest || "—"), 40) });
                s.title = String(r.suggest || "");
                return s;
              } },
          ], rows));
        }
        dialog("类型/标题污染检查", body, [{ text: "关闭", primary: true }]);
      }

      // ------------------------------------------------------------ 某篇的明细
      function showDetail(r) {
        const lines = [
          String(r.key || "") + "　" + short(String(r.title || ""), 70),
          "类型：" + String(r.item_type || "—")
            + "　正文来源：" + String(r.fulltext_source || "无"),
          "缺的字段：" + ((Array.isArray(r.missing) && r.missing.length)
                           ? r.missing.join("、") : "（没给）"),
        ];
        const highs = Array.isArray(r.high) ? r.high : [];
        const lows = Array.isArray(r.low) ? r.low : [];
        if (highs.length) {
          lines.push("");
          lines.push("== 高置信建议（规则抽取；自动写回只会写这些）==");
          highs.forEach(function (h) {
            lines.push("　" + String(h.field || "?") + " = " + String(h.value == null ? "" : h.value));
            if (h.evidence_text) lines.push("　　　证据：" + short(String(h.evidence_text), 160));
          });
        }
        if (lows.length) {
          lines.push("");
          lines.push("== 低置信建议（本地小模型给的，会编，必须自己核对）==");
          lows.forEach(function (h) {
            lines.push("　" + String(h.field || "?") + " = " + String(h.value == null ? "" : h.value));
            if (h.evidence_text) lines.push("　　　证据：" + short(String(h.evidence_text), 160));
          });
        }
        const skipped = Array.isArray(r.skipped) ? r.skipped : [];
        if (skipped.length) {
          lines.push("");
          lines.push("== 没建议的字段（为什么）==");
          skipped.forEach(function (s) {
            lines.push("　" + String(s.field || "?") + "：" + String(s.why || "未说明"));
          });
        }
        const notes = notesOf(r);
        if (notes.length) {
          lines.push("");
          lines.push("== 说明 ==");
          notes.forEach(function (n) { lines.push("　· " + n); });
        }
        if (r.error) {
          lines.push("");
          lines.push("⚠ 这一篇出错了：" + String(r.error));
        }
        dialog("元数据建议明细 · " + String(r.key || ""),
               msgLines(el, lines), [{ text: "关闭", primary: true }]);
      }

      // ------------------------------------------------------------ 起步
      setNote("");
      drawEmpty();
      ctx.log("元数据页：还没有扫描结果（扫描不自动跑）");
    },
  });
})();
