/* ============================================================================
 * admin-pages/outline.js —— 「分节纲要」页（全库纲要清单 + 生成 / 强制重做 / 删除）
 *
 * Tk 面板里的对应实现是 `tools/panels/tab_outline.py（已退役 2026-10-14）`。本页覆盖同样的功能，
 * 但交互在 2026-10-13 按用户要求改成**经验库那一套**：
 *   · 表格**行里不放任何按钮**（一行必然一行高，也不会再出现"删除被挤到第二行"）；
 *   · **点一行 = 选中它**（单选替换，行底色高亮），表格上方那排按钮作用在选中的那一篇；
 *   · **没选中时按钮置灰**，原因写在 `title` 里（照 experience.js::updateActions 的写法）。
 * 页面上再没有原生 `<select>`：状态筛选是 btn-group，分类筛选是自绘下拉（见 admin.js）。
 *
 * 数据来源见开发说明（批次 A / B）（页面只说 HTTP 语义，路径由宿主半
 * src/23-admin.js 的映射表翻成引擎方法）：
 *   GET  /outline-list?filter=all|missing|orphan[&collection=<分类名>]
 *        → outline.list → {rows:[{key,title,year,sections,has_file,orphan,built_at,
 *                                collections[]}], filter, collection}
 *   GET  /collections   → advanced.collections → {rows:[{name,n}]}（填分类下拉）
 *   POST /outline/render {keys:[…]} → {rendered, skipped}
 *   POST /outline/delete {key}      → {deleted}
 *   POST /digest         {key,force}→ 长任务（回 {job:{id}}，之后轮询 GET /jobs/<id>）
 *
 * ## 分类筛选是**多选**（0 个 = 全部，多个 = 并集）—— 2026-10-14
 *
 * 用户：「分节纲要的列表，没做按分类显示，只有按分类排序。」原来这里是
 * `ctx.select` 的**单选**下拉（请求带 `&collection=<一个分类>`，确实生效但只能挑一个）。
 * 现在换成与「分类建议」页 /「经验库」选择文献弹窗**同一份**共享零件
 * `KBAdmin.multiSelect`（定义在 suggest.js；admin-pages 之间不能 import，所以挂在壳上）。
 *
 * ⚠ 引擎的 `outline.list` 只认**一个** `collection` 参数（精确匹配 items.collections
 *   里的一个名字，见 online/engine.py::_m_outline_list），而服务端那一侧这次不动
 *   （本次任务范围只有 content/），所以**并集在客户端做**：
 *     ① 请求只带 `filter`（不带 collection）→ 拿到该状态下的**全部**行；
 *     ② 按勾中的分类做并集过滤（一篇归到多个分类时只算一行）。
 *   好处是"0 / 1 / N 个分类"三态同一套代码；代价是全库行数（现在 94）都过一遍 ——
 *   比多发几次请求便宜得多。
 *   真机判据：全部 94 → 勾 2 个分类后 37（用户给的例子）。
 *   ⚠ 原来那个「分类多于 30 个才出现的文本过滤框」没有了：multiSelect 的选项列表
 *     不支持"输入关键字缩小范围"，硬加会与它自己的"选项没了就剔掉选中值"打架
 *     （正是筛选值被悄悄清掉那种坑）。本库现在 6 个分类，用不上；真多到要过滤时
 *     再给 multiSelect 加选项过滤，别在这里另写一套。
 *
 * 判据以**索引库 meta 为准**（读端 kbviews / MCP 读的也是它）：`sections` 来自
 * meta.ai_outline，`has_file` 只是 views 下那份"给人读的"渲染产物在不在。
 * 有纲要却没文件**不影响检索**，所以缺文件只提醒、不当作失败；孤儿（有文件无纲要）
 * 只报警、不静默删（Tk 侧就是这么定的）。
 * ==========================================================================*/
(function () {
  "use strict";

  /** ISO 时间 → "2026-10-09 21:14"。表格里精确到分钟就够（秒是噪音）。 */
  function fmtTime(v) {
    const s = String(v == null ? "" : v).trim();
    if (!s) return "—";
    return s.replace("T", " ").slice(0, 16);
  }

  /** 多行文案塞进弹窗：dialog 的 body 是 HTML，字符串里的换行会被折掉。 */
  function msgLines(el, arr) {
    return el("div", null, (arr || []).map(function (t) {
      return el("div", { text: String(t) });
    }));
  }

  /** 一篇的"分类"文案（多个合集用顿号连；空就是未分类）。 */
  function catText(r) {
    const cs = (r && r.collections) || [];
    return cs.length ? cs.join("、") : "";
  }

  KBAdmin.page({
    id: "outline",
    title: "分节纲要",
    order: 80,

    async render(host, ctx) {
      const el = ctx.el;
      const dialog = ctx.dialog;
      const runTask = ctx.runTask;
      // 页面级状态。⚠ 不用模块级变量：一次会话里切走再切回来会重新 render，
      //   状态必须每次从零起，不然会画到上一次的残影上。
      //   `collections`（数组）就是分类多选筛选：0 个 = 全部、多个 = 并集。
      const state = { filter: "all", collections: [], rows: [],
                      busy: false, selKey: "" };
      // 读清单的序号：用户连点筛选/刷新时会有多条请求在飞，**只认最后一条的回包**
      // （否则先发后到的旧结果会把新筛选的表盖回去，看着像"筛选没生效"）。
      let loadSeq = 0;
      let allCats = [];              // /collections 的原始行 [{name,n}]

      // ============================================================ 顶部：筛选
      //
      // 状态筛选只有三项、且要一眼看出"现在筛的是哪一种" —— 用按钮组而不是下拉
      //（选项 ≤ 4 个，按钮组比下拉少一次点击，也不碰原生 select 那个坑）。
      const STATUS = [
        { value: "all", text: "全部", title: "全部条目" },
        { value: "missing", text: "只有缺失",
          title: "只有缺失 = 有纲要、却缺 views 下的渲染文件" },
        { value: "orphan", text: "孤儿",
          title: "孤儿 = 有 views 渲染文件、索引库里却没有纲要" },
      ];
      const statusGroup = el("div", { class: "btn-group" });
      const statusBtns = [];
      STATUS.forEach(function (o) {
        const b = el("button", { type: "button", class: "btn btn-subtle",
                                 text: o.text });
        b.title = o.title;
        b.addEventListener("click", function () { load(o.value); });
        statusGroup.appendChild(b);
        statusBtns.push({ opt: o, btn: b });
      });
      function paintStatus() {
        statusBtns.forEach(function (it) {
          const on = it.opt.value === state.filter;
          // 选中态双写：aria-pressed 给读屏，.is-on 给样式（见 admin.css）。
          it.btn.setAttribute("aria-pressed", on ? "true" : "false");
          it.btn.classList.toggle("is-on", on);
        });
      }

      // 分类筛选：**多选**（0 个 = 全部，多个 = 并集）。控件是与「分类建议」页 /
      // 「经验库」选择文献弹窗同一份共享零件 `KBAdmin.multiSelect`（定义在
      // suggest.js，挂到壳上 —— admin-pages 之间不能 import）。选项 ≤ 4 个时它
      // 自己退化成按钮组，更长是自绘下拉（多选版：点一项不关浮层）。
      const catSel = KBAdmin.multiSelect("分类", [], state.collections,
        function (vals) {
          state.collections = vals;      // 并集过滤要的数组（空 = 全部）
          load(state.filter);
        });
      catSel.title = "按 Zotero 分类筛选（可多选）：0 个 = 全部，多个 = 并集；"
                   + "与上面的状态筛选叠加（两个条件是交集）";

      const refreshBtn = el("button", {
        class: "btn btn-subtle", text: "刷新",
        title: "重新读纲要清单（只读索引库与 views 目录，不调模型）",
        on: { click: function () { load(state.filter); } },
      });

      const fillBtn = el("button", {
        class: "btn btn-primary", text: "只补缺失",
        title: "把当前清单里「有纲要、缺渲染文件」的条目一次补齐"
             + "（复用分级视图渲染，不调模型、秒级）",
        on: { click: function () { askFill(); } },
      });

      const note = el("div", { class: "note hidden", text: "" });

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "筛选" }),
          el("span", { class: "faint",
                       text: "按状态与 Zotero 分类缩小清单（两个条件叠加）" }),
          el("span", { class: "grow" }),
          refreshBtn,
          fillBtn,
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [
          el("span", { class: "faint", text: "状态" }),
          statusGroup,
          catSel,
        ]),
        el("div", { class: "sep" }),
        note,
      ]));

      // ============================================================ 操作 + 清单
      //
      // 按钮在上、作用在"选中的那一篇"（经验库同款交互）。行里不放按钮 ——
      // 一行高是硬要求（用户 2026-10-13 的截图里「生成/强制重做/删除」被挤成两行）。
      // ⚠ selInfo 挂 `.clip`：它随选中的标题变长，而这一行现在是 `.stickrow`
      //   （nowrap + 固定高度）—— 没有截断就会把右边三颗按钮挤出去。
      const selInfo = el("span", { class: "faint clip", text: "未选中" });

      const genBtn = el("button", { type: "button", class: "btn btn-subtle",
                                    text: "生成" });
      const rebuildBtn = el("button", { type: "button", class: "btn btn-danger",
                                        text: "强制重做" });
      const delBtn = el("button", { type: "button", class: "btn btn-danger",
                                    text: "删除" });
      genBtn.addEventListener("click", function () {
        const r = selected(); if (r) runDigest(r, false);
      });
      rebuildBtn.addEventListener("click", function () {
        const r = selected(); if (r) askRebuild(r);
      });
      delBtn.addEventListener("click", function () {
        const r = selected(); if (r) askDelete(r);
      });

      const statsLine = el("div", { class: "subtle", text: "正在读纲要清单…" });
      const tableHost = el("div", null,
                           [el("div", { class: "note note-warn", text: "正在读纲要清单…" })]);
      // `.outlinetab` 是本页私有的作用域类：只用来钉「节数」那一列（整列右对齐 +
      // 收紧宽度），规则写在 admin.css 里，别的页面一格不动。
      // `.sticktab` 是「操作行 + 表头两层 sticky」那套（见 admin.css）：这一页
      // 与选中行有关的操作行（生成 / 强制重做 / 删除）也要钉在滚动口顶端。
      //
      // ⚠ 只有**与当前选中行有关**的操作才钉。上面那张「筛选」卡（状态 / 分类 /
      //   刷新 / 只补缺失）是**页面级条件**，与选中哪一行无关，**故意不钉** ——
      //   与经验库同口径：关键词那一行不钉、作用于选中行的那一行才钉；两个都钉
      //   会白吃掉视口顶部两行（用户的实际窗口只有 ~560px 高的可视区）。
      const opsRow = el("div", { class: "row stickrow" }, [
        el("strong", { text: "纲要清单" }),
        selInfo,
        el("span", { class: "grow" }),
        genBtn, rebuildBtn, delBtn,
      ]);
      const tableCard = el("div", { class: "card outlinetab sticktab" }, [
        opsRow,
        el("div", { class: "sep" }),
        statsLine,
        el("div", { class: "sep" }),
        tableHost,
        el("div", { class: "sep" }),
        el("div", { class: "faint", text:
          "「纲要状态」按索引库里 meta.ai_outline 算（检索读的就是它）；views 下的 " +
          ".outline.md 只是给人读的渲染产物 —— 缺它不影响功能，点「只补缺失」可秒级补齐。" +
          "「纲要文件」列：有 = views 下那份 .outline.md 在；无 = 有纲要但缺文件；" +
          "孤儿 = 有文件、索引库里却没有纲要。" }),
      ]);
      host.appendChild(tableCard);

      // ============================================================ 小工具
      function setNote(text, kind) {
        note.className = "note" + (kind ? " note-" + kind : "") + (text ? "" : " hidden");
        note.textContent = text || "";
      }

      function setBusy(on) {
        state.busy = !!on;
        refreshBtn.disabled = state.busy;
        fillBtn.disabled = state.busy;
        catSel.setDisabled(state.busy);
        statusBtns.forEach(function (it) { it.btn.disabled = state.busy; });
        updateActions();
      }

      /** 当前选中的那一行（选中态只在内存里，清单重读后按 key 对回来）。 */
      function selected() {
        const k = state.selKey;
        if (!k) return null;
        return state.rows.filter(function (r) { return r.key === k; })[0] || null;
      }

      /**
       * 选中变化后更新上方那排按钮：**没选中就禁用**，并把"为什么不能点"写进
       * title（与 experience.js::updateActions 同一套口径 —— 只看到一块灰而不知道
       * 原因是最糟的）。
       *
       * 可用性规则（本页自己定的，写在报告里）：
       *   · 生成   —— 选中即可。引擎按**节指纹**增量复用；孤儿行（有文件没纲要）
       *              点它就是新建纲要，也放行。（实话版说明见 genBtn.title）
       *   · 强制重做 —— 只在**这篇真的有纲要**时可点：没有纲要时它等价于"生成"，
       *              用左边那个就好（重做会白跑一遍模型）。
       *   · 删除   —— 只在**有东西可删**时可点（有纲要 或 有渲染文件任一）。
       */
      function updateActions() {
        const r = selected();
        const busy = state.busy;
        const has = !!r;
        const n = has ? Number(r.sections || 0) : 0;
        const nothing = "先在上面点一行选中它（点行就是选中）";
        const needMineru = "（引擎要 MinerU 产物；没有它会直接失败并提示先解析这一篇）";

        // ⚠ 文案要说实话（2026-10-13 用户问「已经生成过再点『生成』会怎样」）。
        //   链路：POST /digest {key, force:false} → engine._m_digest →
        //   localserver.start_digest_job(key, force=False) → do_digest →
        //   digest.build(force=False)：节指纹 = sha1(节标题 + 节内正文前 2000 字)。
        //   **指纹没变的节直接复用上次的摘要、一个模型调用都不发**；只有变了/新增的
        //   节才补问。所以"已有纲要"时这颗按钮多半是秒回 —— 这件事必须写在按钮上，
        //   否则用户会以为点下去会重跑（整篇重问是右边那颗「强制重做」）。
        genBtn.disabled = busy || !has;
        // 已入库的纲要 → 按钮上直接标"已有则跳过"；没纲要 / 孤儿 → 老实叫"生成"。
        genBtn.textContent = (has && n) ? "生成（已有则跳过）" : "生成";
        genBtn.title = !has ? nothing
          : (busy ? "有任务正在跑，等它结束"
             : (r.orphan
                ? ("这一行是孤儿（views 下有渲染文件、索引库里没有纲要）：点它会新建纲要"
                   + "并重写那份文件。" + needMineru)
                : (n
                   ? ("这一篇已有 " + n + " 节纲要：正文没变的节按指纹复用上次摘要"
                      + "（不调模型、秒回）；只有正文变了或新增的节才补问模型。"
                      + "要整篇每一节都重问一遍，请用右边的「强制重做」。")
                   : ("这一篇还没有纲要：逐节问模型生成（一篇学位论文要几分钟），"
                      + "随时切页可取消。" + needMineru))));

        rebuildBtn.disabled = busy || !has || !n;
        rebuildBtn.title = !has ? nothing
          : (busy ? "有任务正在跑，等它结束"
             : (!n ? "这一篇还没有纲要 —— 用左边的「生成」；重做是忽略已有节的指纹、整篇重跑"
                   : ("忽略节的指纹，整篇每一节都重新问一遍模型（学位论文要几分钟）；"
                      + "它一定会真的调模型，不像「生成」那样按指纹秒回。")));

        delBtn.disabled = busy || !has || !(n || r.has_file);
        delBtn.title = !has ? nothing
          : (busy ? "有任务正在跑，等它结束"
             : (!(n || r.has_file) ? "这一篇既没有纲要、也没有渲染文件，没有可删的东西"
                                   : "删掉索引里的纲要与 views 下的渲染文件（会二次确认）"));

        selInfo.textContent = has
          ? ("已选中：" + String(r.key) + " · " + String(r.title || "（无标题）").slice(0, 46))
          : "未选中（先点一行）";
        selInfo.title = has
          ? (String(r.key) + "　" + String(r.title || "")
             + "\n分类：" + (catText(r) || "（未分类）")
             + "\n纲要 " + n + " 节 · "
             + (r.has_file ? "有渲染文件" : "无渲染文件"))
          : "点表格里任意一行选中它，上方三个操作按钮就会亮起来";
      }

      /** 把选中高亮刷到 DOM 上（点行时用；排序重建 tbody 后由 rowClass 自动恢复）。 */
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
        state.selKey = r.key;
        applySelToDom();
        updateActions();
        ctx.log("[分节纲要] 选中 " + r.key + "：" + String(r.title || ""));
      }

      // ============================================================ 分类多选
      //
      // 选项来自 `GET /collections`（全库分类，含"有分类但一篇纲要都没有"的），
      // 文案与排序口径照 suggest.js::collectionOptions：**按篇数降序、同数按拼音**
      // —— 两个地方给同一份数据摆出两种顺序会显得像数据不一致。
      // ⚠ multiSelect 自己带「全部分类（清除选择）」那一项，这里**不要**再造一个
      //   `value: ""` 的选项：它的选中值就是数组本身，空数组即全部。
      function buildCatOptions() {
        const opts = allCats.slice().sort(function (a, b) {
          if (b.n !== a.n) return b.n - a.n;
          return a.name.localeCompare(b.name, "zh",
                                      { numeric: true, sensitivity: "base" });
        }).map(function (c) {
          return { value: c.name, text: c.name + "（" + c.n + "）",
                   title: "Zotero 分类：" + c.name + "，共 " + c.n + " 篇" };
        });
        catSel.setOptions(opts);
      }

      async function loadCats() {
        try {
          const d = await ctx.get("/collections");
          allCats = ((d && d.rows) || []).map(function (c) {
            return { name: String(c.name || ""), n: Number(c.n || 0) };
          }).filter(function (c) { return c.name; });
          buildCatOptions();
          ctx.log("分类清单：" + allCats.length + " 个（可多选，0 个 = 全部）");
        } catch (e) {
          // 拿不到分类不该让整页摆烂：多选控件只剩"全部分类"，清单照读。
          ctx.log("读分类清单失败（分类筛选退化成全部）：" + e, "warn");
        }
      }

      // ============================================================ 表格列
      //
      // 「文献」把年份并进标题（契约给的就是 year · title 两块），完整 key 也进
      // title 悬停 —— 键名（8 位大写）在这张表里是噪音，但排查时要能拿到。
      // 长文本一律"关键词 + title 悬停"（.clip），保证每行一行高。
      const cols = [
        { key: "title", label: "文献", sortable: true, tdClass: "nowrap",
          render: function (r) {
            const s = el("span", { class: "clip" }, [
              r.year ? el("span", { class: "faint", text: String(r.year) + " · " }) : null,
              el("span", { text: String(r.title || "（无标题）") }),
            ]);
            s.title = String(r.key || "") + "　" + String(r.title || "");
            return s;
          } },
        // ⚠ 这一列**不用 `.n` 那个定宽盒**：`.n` 带 `min-width:5.2em`（那是给
        //   "数字 + 单位"两段式用的），一个两位数字会被它撑到 86px 宽（真机实测），
        //   既"太宽"又让表头（靠左）与数字（靠右）差 47px。整列对齐与列宽都交给
        //   admin.css 的 `.outlinetab`（表头也右对齐、宽度钉在 5em）。
        { key: "sections", label: "节数", sortable: true, tdClass: "nowrap",
          render: function (r) {
            const n = Number(r.sections || 0);
            if (!n) return el("span", { class: "faint", text: "—" });
            return el("span", { text: String(n) });
          } },
        { key: "has_file", label: "纲要文件", sortable: true, tdClass: "nowrap",
          // 枚举列按**语义**给次序（有文件 → 缺文件 → 孤儿 → 还没纲要），
          // 别按中文拼音排 —— 那会得到"孤儿/无/有"这种看不出道理的顺序。
          sortValue: function (r) {
            if (r.orphan) return 2;
            if (!Number(r.sections || 0)) return 3;
            return r.has_file ? 0 : 1;
          },
          render: function (r) {
            // 文案压到"有 / 无 / 孤儿"三个字以内 —— 原来的"已渲染"在窄列里会被
            // 折成"已渲/染"两行（用户 2026-10-13 报的正是这条）。颜色语义沿用
            // admin.css 的 .tag-*（ok / warn / bad），说明写进 title。
            if (r.orphan) {
              const s = el("span", { class: "tag-bad", text: "孤儿" });
              s.title = "views 下有渲染文件、索引库里却没有纲要（删过 meta 或留了旧文件）";
              return s;
            }
            if (!Number(r.sections || 0)) {
              const s = el("span", { class: "faint", text: "—" });
              s.title = "这一篇还没有纲要（先用上方的「生成」）";
              return s;
            }
            if (r.has_file) {
              const s = el("span", { class: "tag-ok", text: "有" });
              s.title = "views 下有 .outline.md（给人读的那份渲染产物）";
              return s;
            }
            const w = el("span", { class: "tag-warn", text: "无" });
            w.title = "有纲要、缺 views 下的 .outline.md；点上方「只补缺失」补齐";
            return w;
          } },
        { key: "collections", label: "分类", sortable: true, tdClass: "nowrap",
          sortValue: function (r) { return catText(r); },
          render: function (r) {
            const txt = catText(r);
            if (!txt) {
              const s = el("span", { class: "faint", text: "—" });
              s.title = "这篇没有归到任何 Zotero 分类";
              return s;
            }
            const s = el("span", { class: "clip", text: txt });
            s.title = txt + "（Zotero 分类，多个用顿号连）";
            return s;
          } },
        { key: "built_at", label: "生成时间", mono: true, sortable: true,
          tdClass: "nowrap",
          render: function (r) {
            const s = el("span", { text: fmtTime(r.built_at) });
            if (r.built_at) s.title = String(r.built_at);
            return s;
          } },
      ];

      // ============================================================ 读清单
      async function load(filter) {
        state.filter = filter || "all";
        paintStatus();
        const my = ++loadSeq;
        statsLine.textContent = "正在读纲要清单…";
        tableHost.textContent = "";
        tableHost.appendChild(el("div", { class: "note note-warn",
                                          text: "正在读纲要清单…" }));
        try {
          // ⚠ 请求**不带** collection：引擎的 `outline.list` 只认一个分类名，
          //   多选是并集，所以拿全量回来自己筛（见文件头的说明）。路径不带
          //   查询串里没有的那一段，预览桩也是按整串匹配的（preview-data/outline.json）。
          const url = "/outline-list?filter=" + encodeURIComponent(state.filter);
          const data = await ctx.get(url);
          if (my !== loadSeq) return;        // 已经有更新的一次读取了，丢掉这条
          const all = Array.isArray(data) ? data : ((data && data.rows) || []);
          const rows = filterByCats(all);
          state.rows = rows;
          // 选中项还在新清单里就留着（切筛选/刷新不该把手上选的那篇弄丢）。
          if (!rows.some(function (r) { return r.key === state.selKey; })) {
            state.selKey = "";
          }
          drawTable(rows);
          ctx.log("纲要清单：" + rows.length + " 行（" + scopeName() + "）"
                  + (state.collections.length ? ("　·　全量 " + all.length) : ""));
        } catch (e) {
          if (my !== loadSeq) return;
          state.rows = [];
          state.selKey = "";
          statsLine.textContent = "";
          tableHost.textContent = "";
          tableHost.appendChild(el("div", {
            class: "note note-err",
            text: "读纲要清单失败：" + (e && e.message ? e.message : e),
          }));
          updateActions();
        }
      }

      /**
       * 分类多选的**并集**过滤（0 个 = 全部）。
       *
       * 一篇归到多个分类时只算一行（filter 天然不会重复）；分类名精确匹配
       * `collections[]` 里的一个名字 —— 与引擎 `_m_outline_list` 的口径一致
       * （服务端是 `coll in r["collections"]`）。
       */
      function filterByCats(rows) {
        if (!state.collections.length) return rows.slice();
        const want = {};
        state.collections.forEach(function (c) { want[c] = 1; });
        return rows.filter(function (r) {
          return (r.collections || []).some(function (c) { return want[c]; });
        });
      }

      /** 当前筛选的口径文案（统计行与日志共用一份，避免两处写法漂）。 */
      function scopeName() {
        const s = state.filter === "missing" ? "只有缺失"
             : (state.filter === "orphan" ? "孤儿" : "全部");
        return state.collections.length
          ? (s + " · 分类=" + state.collections.join("/")
             + "（" + state.collections.length + " 个取并集）")
          : s;
      }

      function drawTable(rows) {
        tableHost.textContent = "";
        // 统计按**当前返回的这批**算：筛选后就是"筛出来的这些里有几篇有纲要"。
        let nHave = 0, nFile = 0, nOrph = 0;
        rows.forEach(function (r) {
          if (Number(r.sections || 0)) nHave += 1;
          if (r.has_file) nFile += 1;
          if (r.orphan) nOrph += 1;
        });
        statsLine.textContent = "清单 " + rows.length + " 行（" + scopeName() + "）"
          + "　·　有纲要 " + nHave + " 篇"
          + "　·　有渲染文件 " + nFile + " 份"
          + (nOrph ? "　·　⚠ 孤儿 " + nOrph + " 个" : "");
        if (!rows.length) {
          tableHost.appendChild(el("div", { class: "note note-warn",
            text: "这个筛选下没有条目。" }));
          updateActions();
          return;
        }
        tableHost.appendChild(ctx.table(cols, rows, {
          // 默认按标题升序：这样表头的排序箭头一眼可见（点它切降序，点别的列换列）。
          sort: { key: "title", dir: "asc" },
          rowKey: function (r) { return r.key; },
          // 选中态交给 rowClass：排序会重建 tbody，靠它才能在重画后自动回到那一行。
          rowClass: function (r) { return r.key === state.selKey ? "selected" : ""; },
          onRowClick: function (r) { pick(r); },
        }));
        updateActions();
      }

      // ============================================================ 生成 / 强制重做
      //
      // 长任务：POST /digest → {job:{id}} → ctx.runTask 轮询 /jobs/<id>。
      // 进度只显示"最后一行日志 / message"（引擎的 percent 恒 null，见 ADMIN-API-W2）。
      async function runDigest(r, force) {
        if (state.busy) return;
        setBusy(true);
        const what = (force ? "强制重做 " : "生成 ") + String(r.key);
        setNote(what + "：已派发，等引擎…", "ok");
        try {
          const job = await runTask({
            submit: function () {
              return ctx.post("/digest", { key: r.key, force: !!force });
            },
            interval: 1200,
            onTick: function (j) { setNote(what + "：" + tickText(j), "ok"); },
          });
          const res = (job && job.result) || {};
          // ⚠ 两种"假成功"原先都被这段抹平了，必须分开说（2026-10-13）：
          //   ① digest.build 找不到 MinerU 产物时**正常返回**（ok:false + why），
          //      任务状态是 done —— 旧代码照样报"完成"，把原因吞掉了；
          //   ② `existed_before` 只说明"跑之前文库里有纲要"，**不等于这次真跑了模型**：
          //      真正说明干没干活的是 asked（新问了几节）/ cached（按指纹复用几节）。
          if (res.ok === false) {
            const why = String(res.why || "引擎没说明原因");
            setNote(what + "：没跑成 —— " + why, "err");
            ctx.log("[分节纲要] " + what + " 没跑成：" + why, "err");
            await load(state.filter);
            return;
          }
          const asked = Number(res.asked || 0);
          const cached = Number(res.cached || 0);
          const tail = res.cancelled
            ? "（已取消，这一篇没有落盘）"
            : (asked
               ? ("（新问 " + asked + " 节、复用 " + cached + " 节）")
               : (cached
                  ? ("（" + cached + " 节全部命中指纹：一个模型都没问、秒回；"
                     + "要整篇重问请用「强制重做」）")
                  : "（没有可跑的节）"));
          setNote(what + "：完成" + tail, "ok");
          ctx.log("[分节纲要] " + what + " 完成" + tail, "ok");
          await load(state.filter);
        } catch (e) {
          setNote(what + "：失败 —— " + (e && e.message ? e.message : e), "err");
          ctx.log("[分节纲要] " + what + " 失败：" + e, "err");
        } finally {
          setBusy(false);
        }
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

      function askRebuild(r) {
        if (state.busy) return;
        dialog("强制重做纲要",
          msgLines(el, [
            String(r.key) + "　" + String(r.title || ""),
            "",
            "会忽略已有纲要、整篇重新问一遍模型（一篇学位论文要几分钟，还要占着本地模型）。",
            "只是补没跑过的节请用「生成」。继续？",
          ]),
          [{ text: "取消" },
           { text: "强制重做", danger: true,
             run: function () { runDigest(r, true); } }]);
      }

      // ============================================================ 删除
      function askDelete(r) {
        if (state.busy) return;
        dialog("确认删除纲要",
          msgLines(el, [
            "会删掉 " + String(r.key) + " 的分节纲要：",
            "　· 索引库 meta.ai_outline（" + Number(r.sections || 0) + " 节）",
            "　· views 下的 .outline.md 与 .html 孪生",
            r.orphan ? "（这一行是孤儿：索引库里没有纲要，只会清掉那份残留文件）" : "",
            "",
            "正文、切片、向量、经验都不动。删了要重新按节调模型才能回来。继续？",
          ]),
          [{ text: "取消" },
           { text: "删除", danger: true, run: function () { doDelete(r); } }]);
      }

      async function doDelete(r) {
        if (state.busy) return;
        setBusy(true);
        setNote("正在删除 " + String(r.key) + " 的纲要…", "ok");
        try {
          const res = await ctx.post("/outline/delete", { key: r.key });
          const ok = res && res.deleted;
          setNote(ok ? ("已删除 " + String(r.key) + " 的纲要")
                     : (String(r.key) + "：没有找到可删的纲要（可能已经删过了）"),
                  ok ? "ok" : "warn");
          ctx.log("[分节纲要] 删除 " + r.key + "：" + (res && res.deleted), "ok");
          await load(state.filter);
        } catch (e) {
          setNote("删除失败：" + (e && e.message ? e.message : e), "err");
          ctx.log("[分节纲要] 删除 " + r.key + " 失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      // ============================================================ 只补缺失（批量补渲染）
      //
      // 数据源用**当前这批行**（而不是自己再读一遍）：用户看到的表就是将要处理的对象。
      function missingRows() {
        return state.rows.filter(function (r) {
          return Number(r.sections || 0) > 0 && !r.has_file;
        });
      }

      function askFill() {
        if (state.busy) return;
        const missing = missingRows();
        if (!missing.length) {
          setNote("当前清单里没有缺渲染文件的条目，不用补。", "warn");
          return;
        }
        dialog("补渲染纲要文件",
          msgLines(el, [
            "有 " + missing.length + " 篇有纲要、却没有 views 下的 .outline.md。",
            "",
            "会按索引库里的纲要重新渲染这些文件（复用分级视图渲染，不调模型、秒级完成）。",
            "继续？",
          ]),
          [{ text: "取消" },
           { text: "开始补渲染", primary: true, run: function () { doFill(missing); } }]);
      }

      async function doFill(missing) {
        if (state.busy) return;
        setBusy(true);
        setNote("正在补渲染 " + missing.length + " 篇的纲要文件…", "ok");
        try {
          const res = await ctx.post("/outline/render",
            { keys: missing.map(function (r) { return r.key; }) });
          const n = (res && res.rendered != null) ? res.rendered : "?";
          const skipped = (res && res.skipped) || [];
          setNote("补渲染完成：成功 " + n + " / " + missing.length
                  + (skipped.length ? "　·　跳过 " + skipped.length + " 篇" : ""),
                  "ok");
          if (skipped.length) {
            // 跳过原因要看得见（通常是那篇其实没有纲要 / 文件已存在）。
            ctx.log("[分节纲要] 补渲染跳过：" + skipped.slice(0, 5).map(function (s) {
              return String(s.key) + "（" + String(s.reason || "未说明") + "）";
            }).join("；"), "warn");
          }
          ctx.log("[分节纲要] 补渲染完成：成功 " + n + " / " + missing.length, "ok");
          await load(state.filter);
        } catch (e) {
          setNote("补渲染失败：" + (e && e.message ? e.message : e), "err");
          ctx.log("[分节纲要] 补渲染失败：" + e, "err");
        } finally {
          setBusy(false);
        }
      }

      // ============================================================ 起步
      setNote("");
      paintStatus();
      updateActions();
      await loadCats();
      await load("all");
    },
  });
})();
