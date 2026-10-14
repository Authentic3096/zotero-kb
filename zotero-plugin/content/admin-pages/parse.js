/* ============================================================================
 * admin-pages/parse.js —— 「PDF 解析」页（MinerU 可选组件的状态与补跑）
 *
 * Tk 面板里的对应实现是 `tools/panels/tab_parse.py（已退役 2026-10-14）`（状态表 / 只补缺失 /
 * 全库重解析 / 单篇解析 / 删产物 / 打开产物目录）。本页把"看得见 + 能补跑"
 * 这两件事搬进 Zotero，布局按"简约干净"重做：顶部一行动作 + 一行探针状态 +
 * 一张表。交互在 2026-10-13 按用户要求改成**分节纲要那一套**：
 *   · 表格**行里不放任何按钮**（一行必然一行高，也不会再出现按钮被挤成两行）；
 *   · **点一行 = 选中它**（单选替换、行底色高亮），表头上方那两个按钮作用在选中的
 *     那一篇上（`解析这一篇` / `删除产物`）；
 *   · **没选中时两个按钮置灰**，原因写进 `title`（照 outline.js::updateActions 的写法）；
 *   · `全库重解析` / `只补缺失` / `刷新状态` 是页面级动作，与有没有选中无关，原位不动。
 *
 * 数据来源照 docs/ADMIN-API-W2B.md 的 PDF 解析那一组：
 *   GET  /mineru-status  → mineru.status → {probe:{ok,exe,why},
 *        rows:[{key,title,tier,fulltext_src,pages,figures,elapsed,
 *               artifacts_size,built_at}], total}
 *   POST /mineru/parse   {tier:"missing"|"reparse", key?} → 长任务 {job:{id}}
 *        （派发即返回，之后由 ctx.runTask 轮询 GET /jobs/<id>）
 *   POST /mineru/clear   {key} → {cleared, freed_bytes}
 *
 * 三个取舍（为什么这么做）：
 *   · 「产物占用」优先用行里的 size_text（人读字符串，与 Tk 面板同一口径）；
 *     引擎若只给 artifacts_size（字节），页面自己折成 MB —— 两种都认，
 *     免得引擎那边选哪种形状都要回来改这一页。
 *   · 单篇的「解析这一篇」固定发 tier="reparse"：契约里 tier 只剩 missing /
 *     reparse 两种（Tk 的解析器下拉已并入引擎默认档），而**用户点了按钮就该真解析**
 *     —— 发 missing 时已解析的那一篇会被跳过，看着像"点了没反应"。
 *   · 不做「打开产物目录」：W2B 契约里没有这个端点，而这一页跑在 Zotero 的
 *     browser 里、碰不到文件系统（要经宿主半），按 admin.js 的约定不自己发明通道。
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

  /** 字节数 → 人读（KB / MB / GB）；用于引擎只给 artifacts_size 的场合。 */
  function bytesText(b) {
    const n = Number(b);
    if (!isFinite(n) || n <= 0) return "—";
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  /** 一篇的产物占用：size_text 优先（人读字符串），没有才把字节折成 MB。 */
  function sizeTextOf(r) {
    const row = r || {};
    if (row.size_text) return String(row.size_text);
    const b = Number(row.artifacts_size);
    if (isFinite(b) && b > 0) return bytesText(b);
    return "—";
  }

  /** 拆出"数字 + 单位"（"17.8 MB" → ["17.8","MB"]），拆不出来给 null。 */
  function splitNumUnit(text) {
    const m = /^([0-9][0-9,.]*)\s*(.*)$/.exec(String(text == null ? "" : text).trim());
    return m ? [m[1], m[2]] : null;
  }

  /**
   * 产物占用的**字节数**（只给表头排序用）。
   *
   * ⚠ 排序要按字节原值，不能拿 "64.0 MB" 这种显示串比（"9.9 MB" 会排在后面）。
   *   契约给的是 `artifacts_size`（字节），优先用它；没有就把 `size_text`
   *   反解回去（结构与 parse 各一份，一页一文件、不 import）；反解不出来给 NaN
   *   （排序时沉底）。
   */
  const SIZE_UNIT = { B: 1, KB: 1024, MB: 1048576, GB: 1073741824 };
  function sizeBytesOf(r) {
    const n = Number(r && r.artifacts_size);
    if (isFinite(n) && n > 0) return n;
    const parts = splitNumUnit((r && r.size_text) || "");
    if (!parts) return NaN;
    const v = Number(parts[0].replace(/,/g, ""));
    if (!isFinite(v)) return NaN;
    return v * (SIZE_UNIT[String(parts[1]).trim().toUpperCase()] || 1);
  }

  /** 秒 → "21.4s"。引擎可能给数字，也可能给已经带单位的字符串。 */
  function fmtSecs(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    return n.toFixed(1) + "s";
  }

  /** ISO 时间 → "2026-10-12 09:14"（精确到分钟就够）。 */
  function fmtTime(v) {
    const s = String(v == null ? "" : v).trim();
    return s ? s.replace("T", " ").slice(0, 16) : "—";
  }

  KBAdmin.page({
    id: "parse",
    title: "PDF 解析",
    order: 50,

    async render(host, ctx) {
      const el = ctx.el;
      const dialog = ctx.dialog;
      const runTask = ctx.runTask;
      // 页面级状态（切走再切回来会重新 render，不沿用上一次的残影）。
      // `selKey` = 当前选中的那一篇（行底色高亮、上方两个按钮作用在它身上）。
      const state = { rows: [], probe: null, total: 0, busy: false, selKey: "" };

      /** 数字在定宽盒里右对齐、单位紧跟其后左对齐（admin.css 的 n / u 两档）。 */
      function cellNumUnit(text) {
        const t = String(text == null ? "" : text);
        const parts = splitNumUnit(t);
        if (!parts) return el("span", { text: t });
        return el("span", null, [
          el("span", { class: "n", text: parts[0] }),
          parts[1] ? el("span", { class: "u", text: parts[1] }) : null,
        ]);
      }

      /** 纯计数的数值格（页数 / 图）：没有单位，只把数字放进定宽盒子里对齐。 */
      function numCell(v) {
        const n = Number(v);
        if (v == null || v === "" || !isFinite(n)) {
          return el("span", { class: "faint", text: "—" });
        }
        return el("span", { class: "n", text: String(n) });
      }

      // ------------------------------------------------------ 探针状态行（页面第一行）
      //
      // ⚠ 未装 MinerU 必须**如实且不吓人**：它是可选组件，不是"坏了"。
      //   库默认的正文走 Zotero 缓存 + PyMuPDF，与没装 MinerU 之前逐字节一致。
      const probeLine = el("div", { class: "note note-warn", text: "正在探测 MinerU…" });

      // ------------------------------------------------------ 顶部动作
      const refreshBtn = el("button", {
        class: "btn btn-subtle", text: "刷新状态",
        title: "重读每篇的正文来源与解析产物（只读索引库与产物目录，不解析、秒级）",
        on: { click: function () { load(); } },
      });
      const missingBtn = el("button", {
        class: "btn btn-primary", text: "只补缺失",
        title: "只解析还没有 MinerU 产物的那批；已解析且 PDF 没变的会按指纹跳过"
             + "（几秒一篇）。档位一律 basic（这一页没有档位下拉）。",
        on: { click: function () { runParse("missing", "", "只补缺失"); } },
      });
      const allBtn = el("button", {
        class: "btn btn-danger", text: "全库重解析",
        title: "把全库正文都换成 MinerU 解析的结果（几十分钟，会二次确认）；"
             + "档位一律 basic；PDF 没变、档位没变的那批按指纹跳过。",
        on: { click: askAll },
      });

      const note = el("div", { class: "note hidden", text: "" });

      const toolCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "PDF 解析" }),
          el("span", { class: "faint",
                       text: "MinerU 是可选组件：装了正文更干净、公式变 LaTeX；不装也能读" }),
          el("span", { class: "grow" }),
          refreshBtn, missingBtn, allBtn,
        ]),
        el("div", { class: "sep" }),
        probeLine,
        note,
      ]);
      host.appendChild(toolCard);

      // ------------------------------------------------------ 状态表
      //
      // 交互照「分节纲要」页（用户 2026-10-13：「像之前一样按钮放在上面然后选中再操作」）：
      // 行里零按钮、点行 = 选中，下面这两个按钮作用在选中的那一篇上。
      const selInfo = el("span", { class: "faint", text: "未选中（先点一行）" });
      const parseSelBtn = el("button", {
        class: "btn btn-subtle", text: "解析这一篇",
        title: "先在下面点一行选中它",
        on: { click: function () { doParseSelected(); } },
      });
      const clearSelBtn = el("button", {
        class: "btn btn-danger", text: "删除产物",
        title: "先在下面点一行选中它",
        on: { click: function () { askClear(); } },
      });

      const statsLine = el("div", { class: "subtle", text: "正在读解析状态…" });
      const tableHost = el("div", null,
                           [el("div", { class: "note note-warn", text: "正在读解析状态…" })]);
      // ⚠ 这一行（已选中 + 两个操作按钮）挂 `.stickrow`：滚到表格中段时它**钉在
      //   滚动口顶端**，不必滚回顶上才能操作选中的那一篇（用户 2026-10-13 报的
      //   「往下滚动时这两颗按钮能不能像表头一样固定在上面」）。
      //   表头 th 的 top 由 admin.css 的 `.sticktab` 让出这一行的高度。
      const selRow = el("div", { class: "row stickrow" }, [
        el("strong", { text: "解析状态" }),
        selInfo,
        el("span", { class: "grow" }),
        parseSelBtn, clearSelBtn,
      ]);
      // `.sticktab` 给这一卡里的表头让出操作行的高度；`.parsetab` 只管这一页表格的
      // 表头 nowrap 与四个数值列的整列右对齐（两条规则都只在本页生效）。
      const tableCard = el("div", { class: "card sticktab parsetab" }, [
        selRow,
        el("div", { class: "sep" }),
        statsLine,
        el("div", { class: "sep" }),
        tableHost,
        el("div", { class: "sep" }),
        el("div", { class: "faint", text:
          "「解析档位」是产物用的 MinerU 档位；「正文来源」是这一篇实际读的正文"
          + "（mineru 开头的才是走了 MinerU）。产物放在 <知识库> 下的 mineru <key> 目录里，"
          + "删掉只是下次要重新解析，正文、切片、向量都不动。" }),
      ]);
      host.appendChild(tableCard);

      // ------------------------------------------------------ 小工具
      function setNote(text, kind) {
        note.className = "note" + (kind ? " note-" + kind : "") + (text ? "" : " hidden");
        note.textContent = text || "";
      }

      function setBusy(on) {
        state.busy = !!on;
        refreshBtn.disabled = state.busy;
        missingBtn.disabled = state.busy;
        allBtn.disabled = state.busy;
        updateActions();
      }

      /** 当前选中的那一行（选中态只在内存里，重读状态后按 key 对回来）。 */
      function selected() {
        const k = state.selKey;
        if (!k) return null;
        return state.rows.filter(function (r) { return r.key === k; })[0] || null;
      }

      /**
       * 选中变化后更新上方两个按钮：**没选中就禁用**，并把"为什么不能点"写进
       * title（与 outline.js / experience.js 同一套口径 —— 只看到一块灰而不知道
       * 原因是最糟的）。
       *
       * 可用性规则（本页自己定的，写进报告）：
       *   · 解析这一篇 —— 选中且没有任务在跑即可。**已解析的会改叫「重新解析」并
       *                  二次确认**（见 doParseSelected）：它不是纯粹的 no-op。
       *   · 删除产物   —— 同上，但**这一篇得有产物可删**才算"有得删"：产物来自
       *                  size_text / artifacts_size / built_at 三者任一（与上面
       *                  统计行"有产物"的判据同源，别两处写法漂）。
       */
      function updateActions() {
        const r = selected();
        const busy = state.busy;
        const has = !!r;
        const nothing = "先在下面点一行选中它（点行就是选中）";
        // 「有产物」的判据与「删除产物」「统计行」同源（三取或），别两处写法漂。
        const b = has ? Number(r.artifacts_size) : 0;
        const hasArtifact = has && !!(r.size_text || (isFinite(b) && b > 0) || r.built_at);
        const tier = has ? String(r.tier || "").trim() : "";

        // ⚠ 文案要说实话（2026-10-13 用户问「已经解析过的再点会怎样」）。读到的链路是
        //   `kb_admin.py mineru parse --key K --tier basic --rebuild`：**不带 --force**，
        //   所以 mineru.parse_item(force=False) 按指纹判 —— 同档位（basic）且 PDF
        //   （size+mtime）没变 → **秒回跳过、产物原样**（只顺手重建切片与向量）；
        //   其余情况（原来不是 basic / PDF 改过 / 压根没产物）才会真跑并覆盖产物，
        //   而且一律按 basic 生成 —— 原本 standard/flash 的会被改成 basic，必须说出来。
        parseSelBtn.disabled = busy || !has;
        // 已解析 → 「重新解析」（与未解析的"解析这一篇"一眼分得开）
        parseSelBtn.textContent = hasArtifact ? "重新解析" : "解析这一篇";
        parseSelBtn.title = !has ? nothing
          : (busy ? "有任务正在跑，等它结束"
                  : (hasArtifact
                     ? ("已解析（当前 " + (tier || "档位没记") + " 档）。点它 = 重新解析：\n"
                        + "· 同档位（basic）且 PDF 没变 → 秒回跳过，产物不动，只重建切片与向量；\n"
                        + "· PDF 改过、或要换成 basic 档 → 真的重跑一遍并覆盖产物"
                        + "（几秒到几分钟）。\n"
                        + "⚠ 这一页一律按 basic 档解析；原本不是 basic 的会被改成 basic。")
                     : ("这一篇还没有 MinerU 产物：按 basic 档解析（几秒到几分钟），"
                        + "完了顺手重建这一篇的切片与向量。")));

        clearSelBtn.disabled = busy || !has || !hasArtifact;
        clearSelBtn.title = !has ? nothing
          : (busy ? "有任务正在跑，等它结束"
                  : (!hasArtifact ? "这一篇没有解析产物，没有可删的东西"
                                  : "删掉这一篇的 MinerU 产物（会二次确认；下次构建会重新解析）"));

        selInfo.textContent = has
          ? ("已选中：" + String(r.key) + " · "
             + String(r.title || "（无标题）").slice(0, 46))
          : "未选中（先点一行）";
        selInfo.title = has
          ? (String(r.key) + "　" + String(r.title || "")
             + "\n档位：" + (String(r.tier || "").trim() || "（无产物）")
             + " · 占用 " + sizeTextOf(r)
             + "\n解析于 " + fmtTime(r.built_at))
          : "点表格里任意一行选中它，上方两个操作按钮就会亮起来";
      }

      /** 把选中高亮刷到 DOM 上（排序/重建 tbody 后由 rowClass 自动恢复）。 */
      function applySelToDom() {
        const trs = tableHost.querySelectorAll("tbody tr");
        for (let i = 0; i < trs.length; i++) {
          const k = trs[i].getAttribute("data-key") || "";
          trs[i].classList.toggle("selected", !!k && k === state.selKey);
        }
      }

      /** 点行 = 单选替换（不取消：误点一下就让工具栏变灰很恼人）。 */
      function pick(r) {
        if (state.busy) return;
        state.selKey = String(r.key || "");
        applySelToDom();
        updateActions();
        ctx.log("[PDF 解析] 选中 " + state.selKey + "：" + String(r.title || ""));
      }

      /**
       * 表头上方那颗「解析这一篇 / 重新解析」：作用在选中的那一篇上。
       *
       * ⚠ 已解析的那一篇再点一次**不是纯粹的 no-op**（可能真重跑 + 覆盖产物），
       *   所以改文案为「重新解析」之后再**加一层二次确认**（2026-10-13 用户问
       *   "已经解析过的再点会发生什么" —— 界面得把两种结果都先说清）。
       *   未解析的（第一次解析）不弹确认：那本来就是用户要的，没有歧义。
       */
      function doParseSelected() {
        const r = selected();
        if (!r) return;
        const key = String(r.key || "");
        const b = Number(r.artifacts_size);
        const hasArtifact = !!(r.size_text || (isFinite(b) && b > 0) || r.built_at);
        if (!hasArtifact) {
          runParse("reparse", key, "解析 " + key);
          return;
        }
        const tier = String(r.tier || "").trim() || "（没记档位）";
        dialog("重新解析 " + key, el("div", null, [
          el("div", { text: "这一篇已经有 MinerU 产物（" + tier + " 档）。点「重新解析」会：" }),
          el("div", { class: "faint",
            text: "· 同档位（basic）且 PDF 没改过 → 秒回跳过，产物原样，只重建切片与向量；" }),
          el("div", { class: "faint",
            text: "· PDF 改过、或这一篇原来不是 basic 档 → 真的重跑一遍并覆盖产物"
                + "（几秒到几分钟；解析档位会变成 basic）。" }),
          el("div", { class: "sep" }),
          el("div", { class: "faint",
            text: "只想补还没解析过的篇目，请用上面的「只补缺失」；"
                + "想删掉产物请用「删除产物」。" }),
        ]), [
          { text: "取消" },
          { text: "重新解析", danger: true,
            run: function () { runParse("reparse", key, "重新解析 " + key); } },
        ]);
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

      // ------------------------------------------------------ 表格列
      //
      // ⚠ 列宽是这一页最容易翻车的地方（7 列挤在 1100px 里，总宽刚刚够）：
      //   · 页数 / 图 / 产物占用用 n 档（数字在定宽盒里右对齐，admin.css 那一套，
      //     见 structure.js 的同一做法）；
      //   · 产物占用 / 解析时间这两列挂 c.mono，td 因此拿到 admin.css 的
      //     td.mono（等宽 + **white-space: nowrap**）。nowrap 是关键：没有它
      //     「64.0 MB」会在数字与单位之间折行，整张表就又高又空。
      //   · 文献列不放 key：那 8 位大写要占 60px，而 60px 正是「解析时间」表头
      //     被折成两行的差额（口径照 outline.js：键名只进悬停）。
      //   · **四个数值列（页数 / 图 / 产物占用 / 解析时间）整列右对齐**、短表头
      //     nowrap：这两条写在 admin.css 的 `.parsetab` 一段（只挂本页）。
      //     为什么不能只靠 n 档：n 是"定宽盒里右对齐"，盒子左边仍与表头左边对齐，
      //     于是表头贴列左、数字贴盒右、差着大半列宽（用户 2026-10-13 报的
      //     「页数和图这两列表头内容也不对齐」）。整列右对齐后两者右边缘重合。
      //   · **行里不放任何按钮**（2026-10-13 用户要求照「分节纲要」那套改）：
      //     点行 = 选中，两个操作按钮在表头上方。原来行内的"解析这一篇 / 删除产物"
      //     会在窄列里被 flex 折成上下两行，整张表又高又空。
      const cols = [
        // 表头排序：七列**全部可排**（都是有序语义的短值）。数值列一律给
        // `sortValue` 返回**数字原值**，别拿显示串排 —— "9.9 MB" 会排到
        // "64.0 MB" 后面、"21.4s" 与 "1.2s" 也会乱（口径见 admin.js::table）。
        { key: "title", label: "文献", sortable: true,
          sortValue: function (r) { return String(r.title || ""); },
          render: function (r) {
            // key（8 位大写）不摆进格子里：它在这一屏是噪音，但占了 60px ——
            // 八列 1100px 就这么多，那 60px 正是「解析时间」表头被折行的原因。
            // 口径照 outline.js 的先例：**键名只进悬停**（删产物时弹窗里也会写全）。
            const s = el("span", { text: short(String(r.title || "（无标题）"), 30) });
            s.title = String(r.key || "") + "　" + String(r.title || "");
            return s;
          } },
        { key: "tier", label: "解析档位", sortable: true,
          sortValue: function (r) { return String(r.tier || ""); },
          render: function (r) {
            const t = String(r.tier == null ? "" : r.tier).trim();
            return t ? el("span", { class: "mono", text: t })
                     : el("span", { class: "faint", text: "—" });
          } },
        { key: "fulltext_src", label: "正文来源", sortable: true,
          sortValue: function (r) { return String(r.fulltext_src || ""); },
          render: function (r) {
            const src = String(r.fulltext_src == null ? "" : r.fulltext_src).trim();
            if (!src) return el("span", { class: "faint", text: "(无全文)" });
            // mineru 开头的一律显示成 "MinerU"：具体哪一档已经在左边那列里了，
            // 把 mineru-basic 原样摆出来既占宽又把同一件事说两遍（原值进悬停）。
            const shown = (src.indexOf("mineru") === 0) ? "MinerU" : src;
            const s = el("span", { class: "mono", text: short(shown, 14) });
            s.title = src;
            return s;
          } },
        { key: "pages", label: "页数", sortable: true,
          sortValue: function (r) { return Number(r.pages); },
          render: function (r) { return numCell(r.pages); } },
        { key: "figures", label: "图", sortable: true,
          sortValue: function (r) { return Number(r.figures); },
          render: function (r) { return numCell(r.figures); } },
        { key: "size_text", label: "产物占用", mono: true, sortable: true,
          sortValue: sizeBytesOf,
          render: function (r) { return cellNumUnit(sizeTextOf(r)); } },
        { key: "elapsed", label: "解析时间", mono: true, sortable: true,
          sortValue: function (r) { return Number(r.elapsed); },
          render: function (r) {
            const s = el("span", { text: fmtSecs(r.elapsed) });
            const at = fmtTime(r.built_at);
            s.title = (at === "—") ? "引擎没报解析时间" : ("解析于 " + at);
            return s;
          } },
      ];

      // ------------------------------------------------------ 读状态
      async function load() {
        statsLine.textContent = "正在读解析状态…";
        tableHost.textContent = "";
        tableHost.appendChild(el("div", { class: "note note-warn",
                                          text: "正在读解析状态…" }));
        try {
          const d = await ctx.get("/mineru-status");
          const rows = Array.isArray(d) ? d : ((d && d.rows) || []);
          state.rows = rows;
          state.probe = (d && d.probe) || null;
          state.total = (d && d.total != null) ? Number(d.total) : rows.length;
          // 选中项还在新状态里就留着（刷新/解析完不该把手上选的那篇弄丢）。
          if (!rows.some(function (r) { return r.key === state.selKey; })) {
            state.selKey = "";
          }
          drawProbe(state.probe);
          drawTable(rows);
          ctx.log("PDF 解析：" + rows.length + " 行");
        } catch (e) {
          state.rows = [];
          state.selKey = "";
          statsLine.textContent = "";
          probeLine.className = "note note-err";
          probeLine.textContent = "读解析状态失败：" + msgOf(e);
          tableHost.textContent = "";
          tableHost.appendChild(el("div", { class: "note note-err",
            text: "读解析状态失败：" + msgOf(e) + "（引擎没起来时也会这样）" }));
          updateActions();
        }
      }

      /** 探针状态行：装了就说在哪，没装就说清"可选、不影响读正文"。 */
      function drawProbe(p) {
        const ok = !!(p && p.ok);
        const exe = String((p && p.exe) || "");
        const why = String((p && p.why) || "");
        if (ok) {
          probeLine.className = "note note-ok";
          probeLine.textContent = "MinerU：已装 ✓" + (exe ? "（" + exe + "）" : "")
            + "　·　全库可用档位见下表的「解析档位」。";
          probeLine.title = exe;
        } else {
          probeLine.className = "note note-warn";
          probeLine.textContent = "MinerU：没装，或装了但没能用起来"
            + (why ? "（" + why + "）" : "（引擎没说明原因）")
            + "　·　这是可选组件，没装不影响读正文：正文继续走 Zotero 缓存 + PyMuPDF，"
            + "与以前逐字节一致；想要公式变 LaTeX、表格更稳再装它。";
          probeLine.title = why || "MinerU 没装";
        }
      }

      /** 画状态表 + 一行汇总（汇总是按**当前这批行**算的，用户看到的表就是依据）。 */
      function drawTable(rows) {
        tableHost.textContent = "";
        let nMineru = 0, nArtifact = 0, nMissing = 0, nPages = 0, bytes = 0;
        rows.forEach(function (r) {
          const src = String(r.fulltext_src || "");
          if (src.indexOf("mineru") === 0) nMineru += 1;
          const b = Number(r.artifacts_size);
          const hasArtifact = !!(r.size_text || (isFinite(b) && b > 0) || r.built_at);
          if (hasArtifact) nArtifact += 1; else nMissing += 1;
          const p = Number(r.pages);
          if (isFinite(p) && p > 0) nPages += p;
          if (isFinite(b) && b > 0) bytes += b;
        });
        statsLine.textContent = "共 " + (state.total || rows.length) + " 篇"
          + "　·　正文来自 MinerU " + nMineru + " 篇"
          + "　·　有产物 " + nArtifact + " 篇"
          + "　·　待解析 " + nMissing + " 篇"
          + (nPages ? "　·　共 " + nPages.toLocaleString("en-US") + " 页" : "")
          + (bytes ? "　·　产物合计 " + bytesText(bytes) : "");
        if (!rows.length) {
          tableHost.appendChild(el("div", { class: "note note-warn",
            text: "库里还没有条目（或者引擎回包是空的）。" }));
          updateActions();
          return;
        }
        tableHost.appendChild(ctx.table(cols, rows, {
          rowKey: function (r) { return r.key; },
          // 选中态交给 rowClass：排序/重建 tbody 后靠它自动回到那一行。
          rowClass: function (r) { return r.key === state.selKey ? "selected" : ""; },
          onRowClick: function (r) { pick(r); },
        }));
        updateActions();
      }

      // ------------------------------------------------------ 解析（长任务）
      /**
       * 派发一次解析并跟到结束。
       * `tier` 只有 missing（只补没有产物的）与 reparse（重跑）两种；
       * `key` 给了就只动那一篇。
       */
      async function runParse(tier, key, label) {
        if (state.busy) return;
        setBusy(true);
        setNote(label + "：已派发，等引擎…（单篇几秒到一分钟，全库几十分钟；"
                + "关掉这一页任务也会继续）", "ok");
        try {
          const job = await runTask({
            submit: function () {
              const body = { tier: tier };
              if (key) body.key = key;
              return ctx.post("/mineru/parse", body);
            },
            interval: 1500,
            onTick: function (j) { setNote(label + "：" + tickText(j), "ok"); },
          });
          const res = (job && job.result) || {};
          const extra = (res.parsed != null) ? ("（本次解析 " + res.parsed + " 篇）") : "";
          setNote(label + "：完成" + extra, "ok");
          ctx.logAuto("[PDF 解析] " + label + " 完成" + extra);
          await load();
        } catch (e) {
          setNote(label + "：失败 —— " + msgOf(e), "err");
          ctx.log("[PDF 解析] " + label + " 失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      /**
       * 全库重解析先确认：这是这一页最重的一击（几十分钟 + 重建切片与向量）。
       * 文案要给出"为什么慢"和"其实该先点哪个"，用户才知道自己在等什么。
       */
      function askAll() {
        if (state.busy) return;
        dialog("全库重解析", el("div", null, [
          el("div", { text: "会把全库正文都换成 MinerU 解析的结果（档位一律 basic），"
                          + "并重建切片与向量。" }),
          el("div", { class: "faint",
            text: "几十篇要几十分钟；PDF 没变、档位没变的那批会按指纹跳过（几秒一篇）。" }),
          el("div", { class: "sep" }),
          el("div", { class: "faint", text: "只是想补上还没解析的那批，请点「只补缺失」。" }),
        ]), [
          { text: "取消" },
          { text: "开始重解析", danger: true,
            run: function () { runParse("reparse", "", "全库重解析"); } },
        ]);
      }

      // ------------------------------------------------------ 删产物
      // 作用在**选中的那一篇**上（按钮在表头上方，见 updateActions 的禁用口径）。
      function askClear() {
        if (state.busy) return;
        const r = selected();
        if (!r) return;
        const key = String(r.key || "");
        // 删之前先把"能腾出多少"说清楚 —— 这一列的数字就是为此存在的。
        const size = sizeTextOf(r);
        dialog("删除解析产物", el("div", null, [
          el("div", { text: "删掉 " + key + " 的 MinerU 解析产物"
            + "（<知识库> 下 mineru 目录里的 " + key + " 这一层）。" }),
          el("div", { class: "faint",
            text: "预计释放 " + size + "。正文、切片、向量都不受影响；"
              + "下次构建（或点「解析这一篇」）会重新解析它。" }),
        ]), [
          { text: "取消" },
          { text: "删除产物", danger: true, run: function () { doClear(r); } },
        ]);
      }

      async function doClear(r) {
        if (state.busy) return;
        const key = String(r.key || "");
        setBusy(true);
        setNote("正在删除 " + key + " 的解析产物…", "ok");
        try {
          const res = await ctx.post("/mineru/clear", { key: key });
          const ok = !!(res && res.cleared);
          const freed = Number((res && res.freed_bytes) || 0);
          setNote(ok
            ? ("已删除 " + key + " 的产物" + (freed > 0 ? "，释放 " + bytesText(freed) : "")
               + "（下次构建会重新解析）")
            : (key + "：没有找到可删的产物（可能已经删过了）"), ok ? "ok" : "warn");
          ctx.logAuto("[PDF 解析] 删除 " + key + " 的产物："
            + (ok ? ("已删" + (freed > 0 ? "，" + bytesText(freed) : "")) : "没有可删的"));
          await load();
        } catch (e) {
          setNote("删除产物失败：" + msgOf(e), "err");
          ctx.log("[PDF 解析] 删除 " + key + " 产物失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      // ------------------------------------------------------ 起步
      //
      // 不 await load()：页面骨架已经画好，表是异步补上的（load 自己 try/catch，
      // 失败会写进探针行与表格位）。
      setNote("");
      updateActions();     // 起手就把两个按钮置灰（此时还没选中任何一行）
      load();
      ctx.log("PDF 解析页：开始读 MinerU 状态");
    },
  });
})();
