/* ============================================================================
 * admin-pages/experience.js —— 「经验库」页（经验的增删改查、体检、待确认清单）
 *
 * 对应**已退役（2026-10-14）**的 Tk 面板 tools/panels/tab_experience.py（已退役 2026-10-14）（列表 + 详情 + 两个弹窗，
 * 弹窗源码在 tools/panels/exp_editor.py（已退役 2026-10-14））。功能照它，**布局不照它** ——
 * 用户对界面的一贯要求是「简约干净」：
 *   · 顶部只留一行动作，按钮按内容收着、不拉满整行；
 *   · 一个列表 + 一个详情（Tk 版早年那两个"显示经验的窗口"就合并过一次了）；
 *   · 不做拖拽调列宽（Tk 版有，网页这版按用户要求不做）。
 *
 * ## 依赖的引擎方法（契约见开发说明，2026-10-13 冻结）
 *
 *   GET  /experience?mode=all|query|checkup|pending&q=&limit=
 *        → {rows:[{id,date,effect,source,refs:[{key,label}],question,summary,checkup?}],
 *           total, mode}
 *   GET  /experience/<id>
 *        → {id,date,effect,source,session,question,method,reason,refs,tags,item_keys}
 *   POST /experience         {id?,question,method?,reason?,effect,source?,refs?,tags?}
 *        → {id,created}
 *   POST /experience/delete  {ids:[…]} → {deleted:n, rolled_back_weights:m}
 *   POST /pending/mark       {ids:[…], status:"adopted"|"dropped"} → {marked:n}
 *
 * 另外用到 `GET /papers`（批次 B 的契约，见 suggest.js）：编辑弹窗里的
 * 「选择文献…」多选弹窗（本文件 pickPapers）从它取候选列表 —— 网页版复刻
 * Tk 的 PaperPicker（多选那一路），见本文件里那段注释。
 *
 * ⚠ 两处**要引擎侧对齐**的约定（写在最显眼的地方）：
 *   1. 保存时 `refs` 传的是 **key 字符串数组**（例如 ["A97QALZN"]）；
 *      列表 / 详情**返回**的 refs 是 [{key,label}] —— 一来一回形状不同，
 *      别按同一个形状解析（Tk 侧存库那列叫 item_keys，也是裸 key 列表）。
 *   2. `pending.mark` 传 adopted 时，引擎要**同时**做两件事：把条目写进经验库、
 *      并按关联文献计权重。Tk 版是面板自己 add_experience + 改 pending.jsonl；
 *      页面不许写业务，所以合并进这一个方法。传 dropped 只标状态、不写库。
 *
 * ## 「选择会话」这条线契约里没有
 *
 * W2 只冻结了上面 5 条，而 Tk 版有「选择会话…」（挑要扫的对话，再让本地模型
 * 从里面抽经验）。本页按同样的命名风格新开两条，**引擎与宿主半要一起加**：
 *   GET  /sessions → {rows:[{id,title,when,size_kb,scanned,scanned_n}]}
 *   POST /learn    {sessions:[…]} → {job:{id}}  ← 长任务，之后轮询 GET /jobs/<id>
 *
 * ## 三条硬约束（照抄 admin.js 顶部那三条）
 *   · 只画自己这一页，取数一律 KBAdmin.get/post（页面不发网络请求，
 *     宿主半按路由表转成引擎方法）；
 *   · 样式只用 admin.css 的类，不写颜色字面量、不写内联 style；
 *   · 一页一个文件、不 import。
 * ==========================================================================*/
(function () {
  "use strict";

  // 效果枚举 → 中文 / 语义色（与 Tk 侧 tab_experience.EXP_OUTCOME_CN 同一口径）。
  // ⚠ 契约里列表字段叫 effect、库里那一列叫 outcome —— 两个都认（见 effectValue），
  //   免得因为一处命名差异让"效果"整列显示成空。
  const EFFECT_CN = { effective: "有效", ineffective: "无效",
                      partial: "部分", unknown: "未知" };
  const EFFECT_TAG = { effective: "tag-ok", ineffective: "tag-bad",
                       partial: "tag-warn" };

  const MODE_TITLE = { all: "列出全部", query: "关键词查询",
                       checkup: "经验体检", pending: "待确认清单" };

  // 「效果」列的排序权重：按语义排（有效 → 部分 → 未知 → 无效），不是按中文拼音。
  const EFFECT_ORDER = { effective: 0, partial: 1, unknown: 2, ineffective: 3 };

  // 说明只留一行（原文收进 title 悬停）—— Tk 版那三行灰字常驻太占版面。
  const INFO = "经验是知识库「越用越准」的唯一机制：记下「有效 / 无效 / 部分」，"
             + "下次检索会把验证过的文献排在前面。";
  const INFO_WHY = "只记「与文献内容或研究方法有关」的尝试。\n\n"
    + "工具链 / 配置 / 安装 / 调试这类「关于知识库自己怎么搭」的踩坑不进这里 —— "
    + "它们不关联任何一篇文献，混进来会把检索排序弄脏。\n\n"
    + "「经验体检」就是按这条判据把可疑条目标出来（只标注，不自动删）。";

  function msgOf(e) { return (e && e.message) ? e.message : String(e); }

  /** 列表 / 详情里的"效果"取值：契约叫 effect，库里叫 outcome，两个都认。 */
  function effectValue(r) {
    const v = (r && r.effect != null) ? r.effect : (r && r.outcome);
    return String(v == null ? "" : v);
  }

  function effectCN(v) { return EFFECT_CN[v] || (v ? v : "—"); }

  /**
   * 关联文献的人读文本：`refs` 可能是 [{key,label}]（契约）或裸 key 字符串，
   * 两种都摊平。名字一律用引擎（experience.item_refs）给的 label ——
   * 已删 / 已合并的标记就在 label 里，页面不自己拼。
   */
  function refsLabels(r) {
    const refs = (r && r.refs) || [];
    return refs.map(function (x) {
      if (x == null) return "";
      if (typeof x === "string") return x;
      return String(x.label || x.key || "");
    }).filter(Boolean);
  }

  function refsText(r) { return refsLabels(r).join("、"); }

  /** 长文本掐短：格子里只留关键词，全文放 title 悬停（用户明确要"简约"）。 */
  function clip(s, n) {
    const t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  /**
   * 「选择关联文献」多选弹窗 —— 网页版复刻
   * tools/panels/paper_picker.py（已退役 2026-10-14）::PaperPicker 的**多选**那一路（用户 2026-10-13
   * 的原话：「经验关联文献，原来可以拉出文献列表去勾选……现在没了」）。
   *
   * 为什么不直接用 ctx.dialog：壳里的 dialog() 只维护**一个** dialogEl，开第二个
   *   会把前一个 remove() 掉 —— 而这里正是从「修改经验…」的编辑弹窗里**再开一层**，
   *   用 ctx.dialog 会把用户填了一半的表单连同弹窗一起删掉。所以自己建一个
   *   <dialog> 元素（showModal 天然支持嵌套的 top layer）。样式全部来自 admin.css
   *   的 `dialog` / `.actions` / `.row` / `table.grid` 规则，不写内联 style。
   *
   * 与 PaperPicker 对齐的几条口径（都是 Tk 侧实测踩出来的）：
   *   · 勾选状态只存在 sel 这个 Set 里 —— 搜索/重画把整张表重建，从"行上画的是什么"
   *     反推勾选，一次过滤就全丢了；
   *   · **已选置顶**（稳定排序）只在打开 / 搜索 / 清除时生效；切换勾选**不重排**
   *     —— 勾一下就让那行跳到最前，下一次点击会落到别的行上（Tk 侧的连点踩坑）；
   *   · 已选但不在列表里的 key（已删除 / 已合并的文献）补一行占位，**不能悄悄丢**：
   *     存库时权重按 key 逐条重算，丢了会连着把给它的分数一起扣掉；
   *   · 返回**完整集合**（空数组合法），顺序按候选池顺序，不受此刻搜索到哪一行
   *     —— 与 PaperPicker._picked_keys 同一口径。
   *
   * 2026-10-14 加**分类多选筛选**（用户：「文献列表的文献太多……能不能在分类那里
   * 下拉，让用户选择哪个或者哪几个分类」）：控件是共享零件 KBAdmin.multiSelect
   *（与「分类建议」页同一个），候选从候选池聚合、**不发新请求**；0 个 = 全部、
   * 多个 = 并集；与搜索框叠加（交集）。占位行不受它影响（见 filtered 里的说明）。
   *
   * 返回 Promise<{keys:[…], names:{key:label}} | null>，null = 取消（一个都不动）。
   * 取数只走 ctx.get("/papers")（契约见开发说明），不自己开通道。
   */
  function pickPapers(ctx, selected) {
    const el = ctx.el;
    const sel = new Set((selected || []).map(String));
    let pool = [];      // 候选池（/papers 的行 + 已选但不在列表里的占位行）
    let shown = [];     // 当前显示顺序（勾选时不重排，见上面的说明）
    let cats = [];      // 分类筛选（0 个 = 全部；多个 = 并集）

    const status = el("span", { class: "faint", text: "正在读取文献列表…" });
    const qInput = el("input", { type: "search", size: "30",
      placeholder: "搜标题 / 作者 / 年份 / 分类 / key" });
    const btnClear = el("button", { class: "btn btn-subtle", text: "清除" });
    const listHost = el("div", null,
      [el("div", { class: "note note-warn", text: "正在读文献列表…" })]);

    // 分类多选筛选：与「分类建议」页用的是**同一份**共享零件（KBAdmin.multiSelect，
    // 定义在 suggest.js —— admin-pages 之间不能 import，所以挂在壳上）。
    // ⚠ 候选从 /papers 的行里聚合（每行自带 collections），**不发新请求**。
    const catSel = KBAdmin.multiSelect("分类", [], cats, function (vals) {
      cats = vals;
      rebuild();
    });
    // 库里一个分类都没有时整行藏起来（否则只剩一个空控件）。
    const catRow = el("div", { class: "row hidden" }, [
      catSel,
      el("span", { class: "faint", text: "不选 = 全部；选多个 = 取并集" }),
      el("span", { class: "grow" }),
    ]);

    const dlg = el("dialog");
    dlg.appendChild(el("h2", { text: "选择关联文献（可多选）" }));

    /** 一篇的人读名字（照 suggest.js::paperLabel 的写法，与引擎给的 label 同一格式）。 */
    function nameOf(r) {
      if (r.placeholder) return r.key + "（不在当前文献列表）";
      return (r.first_author || "（无作者）") + " "
        + (r.year == null ? "" : String(r.year)) + " · "
        + clip(r.title, 40) + " [" + r.key + "]";
    }

    function orderRows(rows) {
      // 已选优先。Array.sort 稳定，所以两组内部都保持候选池顺序
      //（= /papers 的年份倒序），排序不会把组内顺序打乱。
      return rows.slice().sort(function (a, b) {
        return (sel.has(a.key) ? 0 : 1) - (sel.has(b.key) ? 0 : 1);
      });
    }

    function filtered() {
      const q = qInput.value.trim().toLowerCase();
      const want = {};
      cats.forEach(function (c) { want[c] = 1; });
      let out = pool;
      if (cats.length) {
        // 占位行（已选、但已删除 / 已合并不在当前列表里的文献）**不受分类筛选
        // 影响**：它是"手上勾着的那几个"，藏起来就没法再取消勾选了。
        out = out.filter(function (r) {
          return r.placeholder
            || (r.collections || []).some(function (c) { return want[c]; });
        });
      }
      if (!q) return out.slice();
      // 作者 / 标题 / 年份 / 分类 / key 都能搜 —— 想按哪种找都行（照 PaperPicker._filter）。
      return out.filter(function (r) {
        const hay = [r.title, r.first_author, r.year, r.key,
                     (r.collections || []).join(" ")].join(" ").toLowerCase();
        return hay.indexOf(q) >= 0;
      });
    }

    function updateStatus() {
      status.textContent = "已选 " + sel.size + " 篇　·　列表 "
        + shown.length + "/" + pool.length + " 篇"
        + (cats.length ? "（筛 " + cats.length + " 个分类）" : "")
        + (qInput.value.trim() ? "（已过滤）" : "");
    }

    function paint() {
      listHost.textContent = "";
      if (!shown.length) {
        listHost.appendChild(el("div", { class: "note note-warn",
          text: pool.length ? "没有匹配的文献 —— 换个关键词试试。"
                            : "知识库索引里还没有文献。" }));
        updateStatus();
        return;
      }
      // ⚠ 这张表**故意不给表头排序**（与其他表的统一口径不同，理由写在这里）：
      //   它有一层"已选置顶"的自定义顺序（orderRows），而每次勾选都会
      //   `paint()` 整表重画 —— 点表头排完再点一行，顺序立刻被打回去，
      //   看着像"排了没用"。要按分类找文献用上面的多选筛选。
      // ⚠ 第 2 列会被 admin.css 的 `td:nth-child(2)` 规则当成「名字 + 说明」那种
      //   长列掐成一行 —— 标题放第 2 列正合适（本来就该掐短 + title 悬停看全文）。
      const cols = [
        { key: "pick", label: "选", render: function (r) {
            const on = sel.has(r.key);
            const s = el("span", { text: on ? "☑" : "☐" });
            s.className = on ? "tag-ok" : "faint";
            return s;
          } },
        { key: "title", label: "标题", render: function (r) {
            const t = String(r.title || "") || "（无标题）";
            const s = el("span", { text: t });
            if (r.placeholder) s.className = "faint";
            s.title = t;
            return s;
          } },
        { key: "author_year", label: "年份 · 作者", render: function (r) {
            return r.placeholder ? "—" : (String(r.year == null ? "" : r.year)
              + " · " + String(r.first_author || "（无作者）"));
          } },
        { key: "collections", label: "分类", render: function (r) {
            const full = (r.collections || []).join("、");
            const s = el("span", { text: clip(full, 18) || "（未分类）" });
            s.title = full || "（未分类）";
            return s;
          } },
        { key: "key", label: "key", mono: true },
      ];
      listHost.appendChild(ctx.table(cols, shown, {
        rowKey: function (r) { return r.key; },
        rowClass: function (r) { return sel.has(r.key) ? "selected" : ""; },
        onRowClick: function (r) { toggle(r.key); },
      }));
      updateStatus();
    }

    function rebuild() {
      shown = orderRows(filtered());
      paint();
    }

    function toggle(key) {
      if (sel.has(key)) sel.delete(key);
      else sel.add(key);
      // ⚠ **不重排**（见上面的说明）：只把这一屏按现在的顺序重画一遍。
      paint();
    }

    function load() {
      ctx.get("/papers").then(function (res) {
        const rows = (res && res.rows) || (Array.isArray(res) ? res : []);
        pool = rows.map(function (r) {
          return {
            key: String((r && r.key) || ""),
            title: String((r && r.title) || ""),
            first_author: String((r && r.first_author) || ""),
            year: (r && r.year == null) ? "" : r.year,
            collections: (r && Array.isArray(r.collections)) ? r.collections : [],
            placeholder: false,
          };
        }).filter(function (r) { return !!r.key; });
        const have = {};
        pool.forEach(function (r) { have[r.key] = 1; });
        sel.forEach(function (k) {
          if (have[k]) return;
          // 已删除 / 已合并的文献：照 Tk 的 _extra_rows 补一行占位，
          // 看得见、能取消勾选（不勾就不会出现在返回的集合里）。
          pool.push({ key: k, title: "（这篇不在当前文献列表里：可能已删除或已合并）",
                      first_author: "（不在列表）", year: "", collections: [],
                      placeholder: true });
        });
        // 分类候选从候选池聚合（一个请求都不多发）；选中的分类若这次没有了，
        // setOptions 会把它剔掉 —— 否则会筛出一张空表还说不出为什么。
        const opts = KBAdmin.collectionOptions(pool);
        catSel.setOptions(opts);
        catRow.classList.toggle("hidden", !opts.length);
        rebuild();
      }).catch(function (e) {
        listHost.textContent = "";
        listHost.appendChild(el("div", { class: "note note-err",
          text: "读文献列表失败：" + msgOf(e) }));
        status.textContent = "";
      });
    }

    // 搜索框**随打随筛**（Tk 是 KeyRelease）；回车也收一次，防输入法不触发 input。
    qInput.addEventListener("input", function () { rebuild(); });
    qInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); rebuild(); }
    });
    btnClear.addEventListener("click", function () { qInput.value = ""; rebuild(); });

    // ⚠ 这三段（标题 / 正文 / 动作）**必须分段**：94 篇的列表会把弹窗撑得比屏幕还高，
    //   而 `<dialog>` 的 `overflow:auto` 是让**整个**对话框滚动 —— 底部的
    //   「取消 / 确定」就被列表顶到可视区之外（用户 2026-10-13 真机报的正是这条：
    //   "勾选完了也没有个确认按钮"）。`.dlg-body` 把正文包成**一个**子元素，
    //   配合 admin.css 的 `dialog[open]` 三段式布局，只有它自己滚、标题与动作行常驻。
    dlg.appendChild(el("div", { class: "dlg-body" }, [
      el("div", { class: "row" }, [
        el("span", { class: "subtle", text: "搜索" }), qInput, btnClear,
        el("span", { class: "grow" }), status,
      ]),
      catRow,
      el("div", { class: "sep" }),
      listHost,
      el("div", { class: "faint",
        text: "点一行勾选 / 取消；已选的排在前面（切换勾选不会让行跳动）。"
            + "确定后取回勾上的全部文献；按 Esc = 取消，一个都不动。" }),
    ]));

    return new Promise(function (resolve) {
      let done = false;
      function dropNode() { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); }
      function finish(v) {
        if (done) return;
        done = true;
        try { dlg.close(); } catch (e) { /* 已经关了 */ }
        dropNode();
        resolve(v);
      }
      /** 勾上的全部 key，顺序按**候选池**（不受此刻搜索到哪一行影响）。 */
      function picked() {
        const keys = [];
        const names = {};
        pool.forEach(function (r) {
          if (!sel.has(r.key)) return;
          keys.push(r.key);
          names[r.key] = nameOf(r);
        });
        return { keys: keys, names: names };
      }
      dlg.appendChild(el("div", { class: "actions" }, [
        el("button", { type: "button", class: "btn", text: "取消",
                       on: { click: function () { finish(null); } } }),
        el("button", { type: "button", class: "btn btn-primary", text: "确定",
                       on: { click: function () { finish(picked()); } } }),
      ]));
      // Esc / 点遮罩关窗 = 不改动关联（与 PaperPicker 的"取消不回调"同一口径）。
      dlg.addEventListener("close", function () {
        if (done) { dropNode(); return; }
        done = true;
        dropNode();
        resolve(null);
      });
      document.body.appendChild(dlg);
      dlg.showModal();
      load();
    });
  }

  KBAdmin.page({
    id: "experience",
    title: "经验库",
    order: 40,

    async render(host, ctx) {
      const el = ctx.el;

      // 这一页的全部状态都在这个闭包里（一页一文件，不往全局挂东西）：
      //   mode/q    当前视图 —— 动作后按**同一个视图**刷新，不跳回"全部"
      //   rows      当前列表数据
      //   sel       选中的 id（字符串；按住 Ctrl 点可多选）
      //   trById    id → 行元素（切选中样式用）
      //   detailSeq 详情请求序号：点得快时防止**旧响应覆盖新详情**
      const state = { mode: "all", q: "", rows: [], sel: new Set(),
                      trById: {}, detailSeq: 0 };

      // ---------------------------------------------------------- 顶部一行动作
      const qInput = el("input", { type: "text", size: "26",
                                   placeholder: "关键词（问题 / 方法 / 文献）" });
      const btnQuery = el("button", {
        class: "btn btn-subtle", text: "查询",
        title: "按关键词找相关经验（在框里按回车也一样）" });
      const btnAll = el("button", {
        class: "btn btn-subtle", text: "列出全部",
        title: "库里所有经验（点一行在下面看全文）" });
      const btnCheckup = el("button", {
        class: "btn btn-subtle", text: "经验体检",
        title: "把「没有关联文献 / 像是工具链记录」的标出来（只标注，不自动删）" });
      // ⚠ Tk 版还有「待确认清单」—— 采纳 / 丢弃两个动作只在那个视图里有意义，
      //   所以要有一个入口。用户给的那一行动作里没写它，但不给入口那两个按钮
      //   就是死的，故与"经验体检"并列为同一个视图切换。
      const btnPending = el("button", {
        class: "btn btn-subtle", text: "待确认清单",
        title: "模型从会话里抽出来、还没确认的条目" });
      // ⚠ 2026-10-13 用户要求：把「修改」与「增添」**拆成两个按钮**。
      //   原来只有一个 btnEdit，按"有没有选中行"在两种语义间切换（选中=改那条、
      //   没选=新增）—— 用户看不出这次点下去到底是改还是增，所以拆开：
      //     · 修改经验…  只在**恰好选中一条**时可用（待确认视图里不可用：那些条目
      //       还没写进经验库，只能采纳 / 丢弃）。没选中时**禁用**，绝不默默变成
      //       "新增" —— 那正是用户要拆开的原因。
      //     · 增添经验…  永远开一张**空表单**，与有没有选中无关。
      //   档位：增添 = btn-primary（主操作、蓝底实心）；修改 = btn-subtle（次级、
      //   改的是已有条目、不产生新数据）。两者一眼区分；这两个都不是破坏性动作，
      //   所以都不用 btn-danger（那是「删除选中」的档位）。
      const btnEdit = el("button", {
        class: "btn btn-subtle", text: "修改经验…",
        title: "改当前选中的那一条 —— 先在下面列表里点一行选中它" });
      const btnAdd = el("button", {
        class: "btn btn-primary", text: "增添经验…",
        title: "新记一条经验（打开的是空表单，跟有没有选中无关）" });
      const btnSessions = el("button", {
        class: "btn btn-subtle", text: "选择会话…",
        title: "挑要扫的对话，让模型从里面抽经验" });

      const infoLine = el("span", { class: "faint", text: INFO });
      infoLine.title = INFO_WHY;

      // 按钮**不 stetch**（不用 grow）：按文字宽度收着，主操作一眼找得到。
      //
      // ⚠ 这一行放**与选中行无关**的动作：查询类（关键词 / 查询 / 列出全部 /
      //   经验体检 / 待确认清单）+「增添经验…」「选择会话…」。
      //   2026-10-13 用户第二遍修正：「增添经验和选择会话这两个应该留在关键词
      //   搜索那一行，这两个和选中的经验无关，不用钉在上面」—— 上一轮把它们
      //   一起挪进下面的 sticky 行是过头了：**只有作用于选中行的**才该跟着钉住。
      //   布局左半截是"筛什么/看什么"，grow 之后是"记一条 / 挑对话"这两个
      //   和选中无关的动作，与下面的 sticky 行一眼分得开。
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("span", { class: "subtle", text: "关键词" }),
          qInput, btnQuery, btnAll, btnCheckup, btnPending,
          el("span", { class: "grow" }),
          btnAdd, btnSessions,
        ]),
        el("div", { class: "row" }, [infoLine]),
      ]));

      // ---------------------------------------------------------- 列表卡
      const countLabel = el("span", { class: "faint", text: "正在读取…" });
      const btnDelete = el("button", {
        class: "btn btn-danger", text: "删除选中（会回滚权重）",
        title: "删除会同时把它给文献加过的权重减回去（不回滚会留下"
             + "「没有经验却权重很高」的脏状态）" });
      const btnAdopt = el("button", {
        class: "btn btn-primary", text: "采纳入库",
        title: "把待确认的条目写进经验库，并计入关联文献的权重" });
      const btnDrop = el("button", {
        class: "btn btn-danger", text: "丢弃",
        title: "只把待确认条目标成「已丢弃」，不写经验库" });

      const tableHost = el("div", null,
                           [el("div", { class: "note note-warn", text: "正在读经验…" })]);
      const msgHost = el("div", null, [el("div", { class: "faint", text: "" })]);

      // ⚠ 这一行（经验 + 列出 N 条 + 与**选中行**相关的操作）挂 `.stickrow`：
      //   滚到列表中段时它钉在滚动口顶端，选中/改/删都不必滚回顶上
      //   （用户 2026-10-13：「也固定在上面不被翻过去」）。表头 th 的 top 由
      //   admin.css 的 `.sticktab` 让出这一行的高度。
      //   只留"作用于选中行"的：修改 / 删除（+ 待确认视图才有的采纳 / 丢弃）；
      //   与选中无关的「增添经验…」「选择会话…」回到上面那条工具条（用户第二遍要求）。
      //   顺序：修改在最前、破坏性的删除最右。
      const opsRow = el("div", { class: "row stickrow" }, [
        el("strong", { text: "经验" }),
        countLabel,
        el("span", { class: "grow" }),
        btnEdit, btnDelete, btnAdopt, btnDrop,
      ]);

      // `.exptab` 是本页列表卡私有的作用域类：短列（id 日期 效果 来源 体检）
      // nowrap + 收窄列宽、两个长文本列让位，规则在 admin.css 里（只作用于本页）。
      // `.sticktab` 是「操作行 + 表头两层 sticky」那套，两者互不干扰。
      host.appendChild(el("div", { class: "card sticktab exptab" }, [
        opsRow,
        el("div", { class: "sep" }),
        tableHost,
        el("div", { class: "sep" }),
        msgHost,
      ]));

      // ---------------------------------------------------------- 详情卡
      const detailHead = el("span", { class: "faint", text: "" });
      const detailHost = el("div", null,
        [el("div", { class: "faint",
                     text: "点上面一行看这条经验的全文；按住 Ctrl 点可多选。" })]);

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [el("strong", { text: "详情" }), detailHead]),
        detailHost,
      ]));

      // ================================================================ 小工具

      /** 卡片底部那一行状态（动作结果 / 错误），取代 Tk 的 messagebox 提示。 */
      function say(text, kind) {
        msgHost.textContent = "";
        if (!text) return;
        msgHost.appendChild(el("div", {
          class: kind === "err" ? "note note-err"
               : kind === "warn" ? "note note-warn"
               : kind === "ok" ? "note note-ok" : "faint",
          text: text,
        }));
      }

      /** 选中的 id → 交给引擎的类型（纯数字转 int，其余原样 —— 待确认的 id 是字符串）。 */
      function toIds() {
        return Array.from(state.sel).map(function (s) {
          return /^\d+$/.test(s) ? Number(s) : s;
        });
      }

      function clearSel() {
        // ⚠ 按 DOM 上的 `.selected` 清，**不要**按 state.trById 里存的行元素清：
        //   点表头排序之后 tbody 是整块重建的，旧引用指向的是已经从文档里摘掉
        //   的节点 —— 清它们等于什么都没清，新表上的底色会留着。（选中态的
        //   唯一真相是 state.sel，行的底色由 rowClass 每次重画时算。）
        tableHost.querySelectorAll("tbody tr.selected").forEach(function (tr) {
          tr.classList.remove("selected");
        });
        state.sel = new Set();
      }

      /** 选中集合变了之后，按钮的可用状态与计数一起更新。 */
      function updateActions() {
        const n = state.sel.size;
        const pending = state.mode === "pending";
        // ⚠ 2026-10-13 用户报「删除选中那个按钮的描述最后怎么有个数字1？」——
        //   旧文案是「删除选中（会回滚权重）· 1」，那个 1 是选中条数，却挂在括号
        //   后面孤零零一个数字，谁也不知道它是什么。现在把数字写进句子里
        //   （"删除选中 2 条（会回滚权重）"），并让**0 条时不出现 0**：
        //   按钮直接禁用、把"为什么不能点"写进 title（与「修改经验…」同一套做法）。
        if (pending) {
          // 待确认的条目还没入库，删除对它无效（要用采纳 / 丢弃）。
          btnDelete.textContent = "删除选中（会回滚权重）";
          btnDelete.disabled = true;
          btnDelete.title = "待确认的条目还没写进经验库，删不了 —— "
                          + "请用「采纳入库」或「丢弃」";
        } else if (n) {
          btnDelete.textContent = "删除选中 " + n + " 条（会回滚权重）";
          btnDelete.disabled = false;
          btnDelete.title = "把这 " + n + " 条经验删掉，并把它给文献加过的权重减回去"
                          + "（不回滚会留下「没有经验却权重很高」的脏状态）";
        } else {
          btnDelete.textContent = "删除选中（会回滚权重）";
          btnDelete.disabled = true;
          btnDelete.title = "还没选中任何条目 —— 先在下面列表里点一行"
                          + "（按住 Ctrl 可多选），这里会变成「删除选中 N 条」";
        }
        btnAdopt.classList.toggle("hidden", !pending);
        btnDrop.classList.toggle("hidden", !pending);
        btnAdopt.disabled = (!pending) || (!n);
        btnDrop.disabled = (!pending) || (!n);

        // 「修改经验…」只在**恰好选中一条**时可点：0 条无从改起，多条不知改哪条；
        // 待确认视图里那些条目还没入库（只能采纳 / 丢弃，不走修改）。
        // ⚠ 这里是**禁用**，不是"没选中就当新增" —— 那种上下文切换正是拆分要消灭的。
        //   同时把不能点的原因写进 title，免得只看到一块灰、不知道为什么。
        const canEdit = (!pending) && (n === 1);
        btnEdit.disabled = !canEdit;
        btnEdit.title = pending
          ? "待确认的条目还没写进经验库 —— 请用右边的「采纳入库」或「丢弃」，不能直接改"
          : canEdit
            ? ("修改选中的 #" + Array.from(state.sel)[0])
            : (n > 1
                ? ("一次只能改一条：现在选中了 " + n + " 条（按住 Ctrl 点可以去掉多余的）")
                : "先在下面列表里点一行选中它，再点这里；要新记一条请点「增添经验…」");
        // 「增添经验…」永远可点（它跟选中状态无关，不需要跟着变）。
      }

      /**
       * 二次确认。（破坏性动作一律先过这里。）
       *
       * ⚠ 为什么要自己包一层而不是直接用 ctx.dialog 的 actions：那两个按钮的
       *   run 会被当成"做完就关"，而这里要的是"用户点了确认之后我才去发请求"，
       *   所以用 Promise 把结果交回调用方。Esc 关窗会走 close 事件兜底 resolve(false)，
       *   免得留下一个永远不落地的 Promise。
       */
      function confirmBox(title, body, okText, danger) {
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

      /** 列表里的长文本格子：关键词 + title 悬停看全文。 */
      function cellClip(text, n, fullTitle, clipClass) {
        const s = el("span", { text: clip(text, n) || "—",
                               class: clipClass || null });
        if (fullTitle) s.title = fullTitle;
        return s;
      }

      // ================================================================ 列表

      function emptyHint(mode) {
        if (mode === "pending") {
          return "待确认清单是空的。先点右上「选择会话…」挑要扫的对话，"
               + "扫出来的条目会出现在这里。";
        }
        if (mode === "checkup") {
          return "体检没发现可疑条目 —— 库里没有「没有关联文献」或"
               + "「像是工具链记录」的经验。";
        }
        if (mode === "query") return "没找到含这个关键词的经验。";
        return "库里的经验是空的 —— 点「增添经验…」记一条。";
      }

      /**
       * 取一个视图的数据并重画列表 + 详情。
       *
       * ⚠ 参数一律放 query 串：`GET /experience?mode=…`（宿主半的表里就是
       *   `GET /experience` 这一条，带参路径只有 /experience/<id>）。
       */
      async function load(mode, q) {
        state.mode = mode;
        state.q = q || "";
        clearSel();
        state.trById = {};
        updateActions();
        countLabel.textContent = "正在读取…";
        tableHost.textContent = "";
        tableHost.appendChild(el("div", { class: "note note-warn", text: "正在读经验…" }));
        detailHost.textContent = "";
        detailHead.textContent = "";

        const path = "/experience?mode=" + encodeURIComponent(mode)
                   + (state.q ? ("&q=" + encodeURIComponent(state.q)) : "");

        let res = null;
        try {
          res = await ctx.get(path);
        } catch (e) {
          tableHost.textContent = "";
          tableHost.appendChild(el("div", { class: "note note-err",
            text: "读经验失败：" + msgOf(e) }));
          countLabel.textContent = "";
          say("读「" + (MODE_TITLE[mode] || mode) + "」失败：" + msgOf(e), "err");
          ctx.logAuto("读经验失败（" + mode + "）：" + msgOf(e));
          return;
        }

        // 契约是 {rows:[…]}；裸数组也认（老引擎 / 退化路径）。
        const rows = (res && res.rows)
          || (Array.isArray(res) ? res : []);
        state.rows = rows;

        const total = (res && res.total != null) ? res.total : rows.length;
        setCount(mode, rows.length, total);

        renderRows(rows);
        ctx.logAuto("经验（" + (MODE_TITLE[mode] || mode) + "）："
                    + rows.length + " 条");
        if (!rows.length) {
          detailHost.textContent = "";
          detailHost.appendChild(el("div", { class: "note note-warn",
                                             text: emptyHint(mode) }));
        }
      }

      /**
       * 计数那一行。
       *
       * ⚠ 必须把「这一屏显示了 N 条」与「库里共 M 条」分开说：查询视图是**相关度
       *   排序 + 截断**的（Tk 版 limit=60），N < M 是正常的，不能让人以为库丢了东西。
       */
      function setCount(mode, shown, total) {
        countLabel.textContent = (MODE_TITLE[mode] || mode) + " · " + shown + " 条"
          + (total !== shown ? ("（库里共 " + total + "）") : "");
      }

      function renderRows(rows) {
        tableHost.textContent = "";
        if (!rows.length) {
          tableHost.appendChild(el("div", { class: "note note-warn",
                                            text: emptyHint(state.mode) }));
          updateActions();
          return;
        }

        const cols = [
          // 表头排序：哪些列可排 / 哪些不可排，口径写在 admin.js::table 的注释里。
          // 这一页可排的是 id / 日期 / 效果 / 来源 / 体检（有序语义的短列）；
          // 「文献」与「问题 · 摘要」是长自由文本，排它没有意义，故不给 sortable。
          { key: "id", label: "id", mono: true, sortable: true,
            sortValue: function (r) { return Number(r.id); } },
          { key: "date", label: "日期", sortable: true },
          { key: "effect", label: "效果", sortable: true,
            sortValue: function (r) {
              const n = EFFECT_ORDER[effectValue(r)];
              return n == null ? 9 : n;
            },
            render: function (r) {
              const v = effectValue(r);
              const cn = effectCN(v);
              const s = el("span", { text: cn });
              // 只给"有语义"的三档上色；unknown 不给色，免得好几条都是红的。
              if (EFFECT_TAG[v]) s.className = EFFECT_TAG[v];
              else s.className = "faint";
              return s;
            } },
          { key: "source", label: "来源", sortable: true },
          { key: "refs", label: "文献", render: function (r) {
              const labels = refsLabels(r);
              if (!labels.length) {
                return cellClip("（无）", 26,
                  "这条没有关联文献：不会被检索加权，也不会出现在任何一篇的档案里。",
                  "faint");
              }
              // ⚠ label 是「作者 年份 · 短标题 [KEY]」这种长串（30 字上下），
              //   一篇就把格子占满 —— 直接掐断会**完全看不出关联了几篇**
              //   （Tk 版那一列显示的就是篇数）。所以：放得下就按用户要求用
              //   顿号连；放不下就「第一篇 + 等 N 篇」，完整清单照样在 title 里。
              // `.clip`（单行 + 省略号，页作用域里收到 18em）保证这一行不被折成
              //   两三行 —— 2026-10-14 用户报的「列表还会折行」就是长列折行。
              const full = labels.join("、");
              const budget = 30;
              const text = (full.length > budget && labels.length > 1)
                ? (clip(labels[0], budget - 8) + " 等 " + labels.length + " 篇")
                : full;
              return cellClip(text, budget, full, "clip");
            } },
          // 「问题 · 摘要」：格子里只留关键词，全文（含摘要）放 title 悬停。
          { key: "question", label: "问题 · 摘要", render: function (r) {
              const q = String(r.question == null ? "" : r.question);
              const sum = String(r.summary == null ? "" : r.summary);
              const full = q + (sum ? (q ? "\n" : "") + "摘要：" + sum : "");
              return cellClip(q || sum, 38, full || "（这一条没写问题 / 摘要）",
                              "clip");
            } },
          { key: "checkup", label: "体检", sortable: true,
            render: function (r) {
              let c = r.checkup;
              if (Array.isArray(c)) c = c.join("；");
              c = String(c == null ? "" : c).trim();
              if (!c) return el("span", { class: "faint", text: "—" });
              // 体检原因可能很长：格子里留关键词（`.clip` 在 9em 处截断），
              // 悬停看完整判据。
              return cellClip(c, 24, c, "clip tag-warn");
            } },
        ];

        // ⚠ 多选靠 Ctrl / Shift：`ctx.table` 的行回调只给 (row, tr)，**拿不到
        //   事件**。所以在包裹元素上挂一个**捕获阶段**的监听，先把这一次点击的
        //   修饰键记下来；冒泡阶段 ctx.table 的回调再读那个变量。
        let mods = false;
        const tbl = ctx.table(cols, rows, {
          onRowClick: function (r, tr) { pick(r, tr, mods); },
          // ⚠ rowKey / rowClass 是**表头可排序之后必须有的**：点表头会整块重建
          //   tbody，靠 rowClass 才能在重画时把选中底色带回那一行（否则排一下
          //   选中态就没了，看着像"点丢了"）。
          rowKey: function (r) { return r.id; },
          rowClass: function (r) {
            return state.sel.has(String(r.id)) ? "selected" : "";
          },
        });
        tbl.addEventListener("click", function (ev) {
          mods = !!(ev.ctrlKey || ev.metaKey || ev.shiftKey);
        }, true);
        tableHost.appendChild(tbl);

        // 行元素按顺序取回来（ctx.table 不把它们交出来），供选中样式与恢复用。
        const trs = tableHost.querySelectorAll("tbody tr");
        rows.forEach(function (r, i) {
          if (trs[i]) state.trById[String(r.id)] = trs[i];
        });

        // 与 Tk 版一致：填完自动选中第一条（否则详情区一直空着等用户点）。
        const first = rows[0];
        const ftr = state.trById[String(first.id)];
        if (ftr) { state.sel.add(String(first.id)); ftr.classList.add("selected"); }
        showDetail(first);
        updateActions();
      }

      /**
       * 点行：默认**单选替换**，按住 Ctrl / Cmd / Shift 才是加/减一项
       *（与 Tk 的 extended 选择一致）。点谁看谁 —— 哪怕刚把它取消选中。
       */
      function pick(r, tr, mods) {
        const id = String(r.id);
        if (!mods) {
          clearSel();
          state.trById[id] = tr;
          state.sel.add(id);
          tr.classList.add("selected");
        } else if (state.sel.has(id)) {
          state.sel.delete(id);
          tr.classList.remove("selected");
        } else {
          state.trById[id] = tr;
          state.sel.add(id);
          tr.classList.add("selected");
        }
        showDetail(r);
        updateActions();
      }

      // ================================================================ 详情

      /**
       * 画某一行的详情。
       *
       * 列表行按契约只有 question/summary，方法 / 原因 / 会话要另取
       * `GET /experience/<id>`。做法是**先用行里的字段画一版**（点下去立刻有东西），
       * 再取全文替换 —— 行里已经带了全文（method + reason + session 都在）就
       * 省掉这一趟往返。待确认的条目没有详情接口，行里就是全部。
       */
      async function showDetail(row) {
        const id = String(row.id);
        renderDetail(row);
        if (state.mode === "pending") return;
        const complete = (row.method != null) && (row.reason != null)
                      && (row.session != null);
        if (complete) return;

        const token = ++state.detailSeq;
        try {
          const full = await ctx.get("/experience/" + encodeURIComponent(id));
          if (token !== state.detailSeq) return;      // 用户已经点了别的行
          renderDetail(Object.assign({}, row, full || {}));
        } catch (e) {
          if (token !== state.detailSeq) return;
          renderDetail(row);
          detailHost.appendChild(el("div", { class: "note note-err",
            text: "取全文失败（" + msgOf(e) + "），上面只显示列表里带的字段。" }));
        }
      }

      /**
       * 两列字段式详情：标签**右对齐** + 值。
       *
       * ⚠ 为什么是三列（左边多一个空格子）：admin.css 有一条
       *   `table.grid td:nth-child(2)` 的掐断规则（单行 + 省略号，给
       *   「名字 + 说明」那种表用的）。值列要是落在第 2 格，"方法 / 原因"
       *   这两个最长的格子就会被截成一行 —— 把标签放第 2 格（它本来就短）、
       *   值放第 3 格，正好躲开那条规则。
       */
      function renderDetail(r) {
        detailHost.textContent = "";
        detailHead.textContent = "#" + String(r.id == null ? "?" : r.id)
          + (r.date ? ("　·　" + String(r.date)) : "");

        // 体检原因（体检视图那几条才有）单独一行提示，不混进字段表。
        let why = r.checkup;
        if (Array.isArray(why)) why = why.join("；");
        why = String(why == null ? "" : why).trim();
        if (why) {
          detailHost.appendChild(el("div", { class: "note note-warn",
                                             text: "体检认为可疑：" + why }));
        }

        const fields = [
          ["日期", r.date],
          ["来源", r.source],
          ["会话", r.session || "（无）"],
          ["效果", effectCN(effectValue(r))],
          ["文献", refsText(r) || "（无 —— 不会被检索加权）"],
          ["问题", r.question],
          ["方法", r.method],
          ["原因", r.reason],
        ];
        let tags = r.tags;
        if (Array.isArray(tags)) tags = tags.join("、");
        if (tags) fields.push(["标签", tags]);

        const rows = fields.map(function (f) {
          const v = (f[1] == null) ? "" : String(f[1]);
          return { label: f[0], value: v === "" ? "—" : v };
        });

        detailHost.appendChild(ctx.table([
          { key: "_pad", label: "" },
          { key: "label", label: "", align: "right",
            render: function (x) { return el("span", { class: "faint", text: x.label }); } },
          { key: "value", label: "" },
        ], rows));
      }

      // ================================================================ 动作

      /** 删除选中（**会回滚权重**，必须二次确认）。 */
      async function doDelete() {
        const ids = toIds();
        if (!ids.length) {
          say("先在列表里选中要删的条目（点一行；按住 Ctrl 可多选）。", "warn");
          return;
        }
        const body = el("div", null, [
          el("div", { class: "mono", text: ids.slice(0, 20).map(function (i) {
            return "#" + i; }).join("、") + (ids.length > 20 ? " …" : "") }),
          el("div", { class: "faint",
            text: "删除会同时把它给文献加过的权重减回去 —— 不回滚会留下"
                + "「没有经验却权重很高」的脏状态。删完编号会重排成从 1 开始连续。" }),
        ]);
        const ok = await confirmBox("删除这 " + ids.length + " 条经验？", body,
                                    "删除", true);
        if (!ok) return;
        try {
          const res = await ctx.post("/experience/delete", { ids: ids });
          const n = (res && res.deleted != null) ? res.deleted : ids.length;
          const m = (res && res.rolled_back_weights != null)
            ? res.rolled_back_weights : "?";
          ctx.logAuto("已删除 " + n + " 条经验（回滚了 " + m
                      + " 篇文献的权重），编号已重排");
          say("已删除 " + n + " 条（回滚权重 " + m + " 篇）。", "ok");
        } catch (e) {
          ctx.logAuto("删除失败：" + msgOf(e));
          say("删除失败：" + msgOf(e), "err");
        }
        load(state.mode, state.q);
      }

      /** 采纳 / 丢弃待确认条目（只对"待确认清单"视图有效）。 */
      async function doMark(status) {
        const adopt = (status === "adopted");
        const ids = toIds();
        if (!ids.length) {
          say("先选中待确认的条目（点一行；按住 Ctrl 可多选）。", "warn");
          return;
        }
        const body = el("div", null, [
          el("div", { class: "mono", text: ids.slice(0, 20).map(function (i) {
            return "#" + i; }).join("、") }),
          el("div", { class: "faint", text: adopt
            ? "入库会按关联文献计入权重（引擎在 pending.mark 里一并完成）。"
            : "只把这几条标成「已丢弃」，不写经验库。" }),
        ]);
        const ok = await confirmBox(
          adopt ? ("采纳并入库这 " + ids.length + " 条？")
                : ("丢弃这 " + ids.length + " 条？"),
          body, adopt ? "采纳入库" : "丢弃", !adopt);
        if (!ok) return;
        try {
          const res = await ctx.post("/pending/mark", { ids: ids, status: status });
          const n = (res && res.marked != null) ? res.marked : ids.length;
          ctx.logAuto((adopt ? "已采纳入库 " : "已丢弃 ") + n + " 条待确认条目");
          say((adopt ? "已采纳 " : "已丢弃 ") + n + " 条。", adopt ? "ok" : "");
        } catch (e) {
          ctx.logAuto((adopt ? "采纳失败：" : "丢弃失败：") + msgOf(e));
          say((adopt ? "采纳失败：" : "丢弃失败：") + msgOf(e), "err");
        }
        load("pending", "");
      }

      /** 逗号 / 分号 / 空白分隔 → 列表（与 Tk 侧 split_list 同一套切法）。 */
      function splitList(text) {
        return String(text == null ? "" : text)
          .split(/[,，;；\s]+/)
          .map(function (s) { return s.trim(); })
          .filter(Boolean);
      }

      // ---- 修改 / 增添经验（两个入口共用这一个编辑器） ---------------------

      /** 当前选中的那一条；不是"恰好一条"（或处于待确认视图）时为 null。 */
      function selectedRow() {
        if (state.mode === "pending" || state.sel.size !== 1) return null;
        const id = Array.from(state.sel)[0];
        return state.rows.find(function (x) { return String(x.id) === id; }) || null;
      }

      /**
       * 「修改经验…」：**只**改当前选中的那一条。
       *
       * ⚠ 正常路径下没选中时按钮是禁用的、根本点不到；这里再兜一次底，并把
       *   "为什么不能改"说明白 —— 拆分的目的就是不让它偷偷变成"新增"。
       */
      function doEdit() {
        const row = selectedRow();
        if (!row) {
          say(state.mode === "pending"
            ? "待确认的条目还没写进经验库，改不了 —— 请用上面的「采纳入库」或「丢弃」。"
            : "先在列表里点一行选中它（只选一条）再点「修改经验…」；"
              + "要新记一条请点「增添经验…」。", "warn");
          return;
        }
        openEditor(row);
      }

      /**
       * 打开编辑器。
       *
       *   openEditor(null)  → **增添**：表单全空，与当前选中无关；
       *   openEditor(row)   → **修改**：把 row 填进表单（先取全文）。
       *
       * 语义由调用方定好再进来 —— 编辑器自己**不看选中状态**（旧版那句
       * "没选中就新增"就是写在这里的，也是用户要拆掉的地方）。
       */
      async function openEditor(row) {
        // 列表行可能只有摘要 —— 编辑要全文，先取一次（失败就用行里的字段，不拦着用户改）。
        let full = row;
        if (row) {
          try {
            const got = await ctx.get("/experience/" + encodeURIComponent(row.id));
            full = Object.assign({}, row, got || {});
          } catch (e) {
            ctx.logAuto("读经验 #" + row.id + " 全文失败，用列表里的字段编辑："
                        + msgOf(e));
          }
        }

        // 表单控件（HTML 属性定宽：不许写内联 style）。
        const fEffect = el("select", null, Object.keys(EFFECT_CN).map(function (v) {
          return el("option", { value: v, text: EFFECT_CN[v] });
        }));
        const fQuestion = el("textarea", { rows: "3", cols: "58",
          placeholder: "一句话说清这次尝试要解决什么（这一句会参与检索匹配）" });
        const fMethod = el("textarea", { rows: "3", cols: "58",
          placeholder: "用了什么方法（照哪篇文献 / 哪个公式）" });
        const fReason = el("textarea", { rows: "3", cols: "58",
          placeholder: "为什么有效 / 为什么失败" });
        const fTags = el("input", { type: "text", size: "58",
          placeholder: "逗号分隔，可空" });
        // ---- 关联文献：从列表勾选（2026-10-13 用户要求，替换原来的"填 key"输入框）
        //   refKeys  要写回引擎的 **key 字符串数组**（见文件头约定 1：保存 payload 的
        //            refs 是 ["KEY",…]，不是 [{key,label}]）；
        //   refLabels  只给人看（引擎的 label，或从 /papers 拼出来的"作者 年份 · 标题"）。
        let refKeys = [];
        const refLabels = {};
        const chipsHost = el("div", { class: "row" });
        const refStatus = el("span", { class: "faint", text: "" });
        const btnPickRefs = el("button", {
          class: "btn btn-subtle", text: "选择文献…",
          title: "打开知识库的文献列表，勾选关联文献（可搜索、已选的排在前面）" });
        const errBox = el("div");

        // `full` 非空 = 修改（把那条填进表单）；为空 = **增添**，表单保持全空
        //（只把"效果"落回 unknown —— 那是个下拉框，不给默认值就成了未选状态）。
        if (full) {
          fEffect.value = effectValue(full) || "unknown";
          fQuestion.value = String(full.question == null ? "" : full.question);
          fMethod.value = String(full.method == null ? "" : full.method);
          fReason.value = String(full.reason == null ? "" : full.reason);
          let tags = full.tags;
          if (Array.isArray(tags)) tags = tags.join(", ");
          fTags.value = String(tags == null ? "" : tags);
          // ⚠ 关联文献**不在这里**填：控件是"选择文献…"按钮 + 已选清单，
          //   初值取自 item_keys（库里的裸 key 列表，顺序权威）+ refs（补 label）。
        } else {
          fEffect.value = "unknown";
        }

        /**
         * 关联文献的初值。
         *
         * ⚠ 引擎的两处形状不一样（文件头约定 1）：`item_keys` 是裸 key 数组、
         *   `refs` 是 [{key,label}]。**两个都认**：顺序优先用 item_keys（那是库里
         *   存的原序），label 只从 refs 取 —— 老引擎可能只给其中一个。
         */
        (function initRefs() {
          if (!full) return;
          (Array.isArray(full.refs) ? full.refs : []).forEach(function (x) {
            if (x && typeof x === "object" && x.key) {
              refLabels[String(x.key)] = String(x.label || x.key);
            }
          });
          const seen = {};
          const push = function (k) {
            k = String(k == null ? "" : k).trim();
            if (!k || seen[k]) return;
            seen[k] = 1;
            refKeys.push(k);
          };
          if (Array.isArray(full.item_keys) && full.item_keys.length) {
            full.item_keys.forEach(push);
          }
          (Array.isArray(full.refs) ? full.refs : []).forEach(function (x) {
            push(typeof x === "string" ? x : (x && x.key));
          });
        })();

        /** 把"已选清单"画出来（label/chip，可单个移除）。 */
        function paintRefs() {
          chipsHost.textContent = "";
          if (!refKeys.length) {
            chipsHost.appendChild(el("span", { class: "faint",
              text: "（未关联 —— 不会被检索加权，也不会出现在任何一篇的档案里）" }));
          } else {
            refKeys.forEach(function (k) {
              const label = refLabels[k] || k;
              const chip = el("span", { class: "note note-ok" }, [
                el("span", { text: clip(label, 34), title: label }),
                el("button", { type: "button", class: "btn btn-subtle",
                  text: "✕", title: "取消「" + label + "」的关联",
                  on: { click: function () { removeRef(k); } } }),
              ]);
              chip.title = k;
              chipsHost.appendChild(chip);
            });
          }
          refStatus.textContent = refKeys.length
            ? ("已选 " + refKeys.length + " 篇") : "还没选";
        }

        function removeRef(key) {
          refKeys = refKeys.filter(function (k) { return k !== key; });
          paintRefs();
        }

        btnPickRefs.addEventListener("click", function () {
          // 这个弹窗是**嵌套的**（从编辑弹窗里再开一层），详见 pickPapers 的说明。
          pickPapers(ctx, refKeys).then(function (res) {
            if (!res) return;                  // 取消：一个都不动
            refKeys = res.keys.slice();
            Object.keys(res.names).forEach(function (k) {
              // 已有 label（引擎给的）优先保留 —— 它带着"已删除 / 已合并"的标记。
              if (!refLabels[k]) refLabels[k] = res.names[k];
            });
            paintRefs();
          });
        });

        paintRefs();

        function fieldRow(label, node, tip) {
          const lab = el("span", { class: "faint", text: label });
          if (tip) lab.title = tip;
          return el("div", { class: "row" }, [lab, node]);
        }

        const body = el("div", null, [
          el("div", { class: "faint",
            text: "关联文献从文献列表里勾选 —— 选错会把权重记到别的文献上。" }),
          fieldRow("效果", fEffect, "有效 / 无效 / 部分 / 未知（未知 = 还没验证）"),
          fieldRow("问题/目标", fQuestion, "必填"),
          fieldRow("用了什么方法", fMethod),
          fieldRow("为什么有效/失败", fReason),
          fieldRow("标签", fTags, "逗号分隔"),
          el("div", { class: "row" }, [
            (function () {
              const lab = el("span", { class: "faint", text: "关联文献" });
              lab.title = "勾上的文献会按这条经验计权重（存库时是裸 key 列表）";
              return lab;
            })(),
            btnPickRefs, refStatus,
          ]),
          chipsHost,
          errBox,
        ]);

        const dlg = ctx.dialog(row ? ("修改经验 #" + row.id) : "增添经验", body, [
          { text: "取消" },
          // ⚠ `run` 返回 false = 先别关窗：保存是异步的，出错要留在窗里给人看
          //   （ctx.dialog 的语义是"run 没返回 false 就立刻关"）。
          { text: "保存", primary: true, run: function () { doSave(); return false; } },
        ]);

        function formError(msg) {
          errBox.textContent = "";
          errBox.appendChild(el("div", { class: "note note-err", text: msg }));
        }

        async function doSave() {
          const question = fQuestion.value.trim();
          if (!question) {
            formError("「问题/目标」不能空 —— 它是以后检索时要匹配的那句话。");
            return;
          }
          const payload = {
            question: question,
            method: fMethod.value.trim(),
            reason: fReason.value.trim(),
            effect: fEffect.value,
            // ⚠ refs 仍是**字符串 key 数组**（见文件头约定 1）—— 弹窗给回的
            //   {keys,names} 里只取 keys，names 仅用于画界面。
            refs: refKeys.slice(),
            tags: splitList(fTags.value),
          };
          if (row) payload.id = row.id;
          errBox.textContent = "";
          try {
            const res = await ctx.post("/experience", payload);
            const eid = (res && res.id != null) ? res.id : (row ? row.id : "?");
            ctx.logAuto((row ? "已修改经验 #" : "已新增经验 #") + eid
                        + "（权重已按关联文献重算）");
            dlg.close();
            load(state.mode, state.q);
          } catch (e) {
            formError("保存失败：" + msgOf(e));
            ctx.logAuto("保存经验失败：" + msgOf(e));
          }
        }
      }

      // ---- 选择会话（扫哪些对话） ------------------------------------------

      async function openSessions() {
        let res = null;
        try {
          res = await ctx.get("/sessions");
        } catch (e) {
          ctx.logAuto("读会话列表失败：" + msgOf(e));
          ctx.dialog("选择会话", "读不到会话列表：" + msgOf(e), [{ text: "关闭" }]);
          return;
        }
        const rows = (res && res.rows) || (Array.isArray(res) ? res : []);
        const picked = new Set();

        const status = el("div", { class: "faint",
          text: rows.length
            ? "点一行勾选 / 取消 —— 抽取只在你勾的会话里做。"
            : "一个会话都没找到。" });
        const listHost = el("div");
        if (rows.length) {
          listHost.appendChild(ctx.table([
            { key: "when", label: "时间", mono: true, sortable: true },
            { key: "size_kb", label: "大小", sortable: true,
              sortValue: function (r) { return Number(r.size_kb); },
              render: function (r) {
                return (r.size_kb == null) ? "—" : String(r.size_kb) + " KB"; } },
            { key: "title", label: "会话", sortable: true,
              sortValue: function (r) { return String(r.title || r.id || ""); },
              render: function (r) {
                // 标题可能很长（也可能是空 → 退回会话 id），同「问题·摘要」那套处理。
                const t = String(r.title || r.id || "");
                return cellClip(t, 46, t + (r.id ? ("\n" + r.id) : ""), "clip");
              } },
            { key: "scanned", label: "扫过吗", sortable: true,
              sortValue: function (r) { return r.scanned ? 1 : 0; },
              render: function (r) {
                return r.scanned
                  ? ("已扫到第 " + (r.scanned_n == null ? "?" : r.scanned_n) + " 条")
                  : "没扫过"; } },
          ], rows, {
            // 勾选态存 picked 集合；rowClass 让它在表头排序重建 tbody 之后回来。
            rowKey: function (r) { return r.id; },
            rowClass: function (r) {
              return picked.has(String(r.id)) ? "selected" : "";
            },
            onRowClick: function (r, tr) {
              const id = String(r.id);
              if (picked.has(id)) { picked.delete(id); tr.classList.remove("selected"); }
              else { picked.add(id); tr.classList.add("selected"); }
              status.textContent = "已勾 " + picked.size + " 个会话 —— "
                                 + "抽取只在你勾的会话里做。";
            },
          }));
        } else {
          listHost.appendChild(el("div", { class: "note note-warn",
                                           text: "没找到可扫的会话记录。" }));
        }

        const dlg = ctx.dialog("选择会话（只扫勾中的）",
          el("div", null, [status, el("div", { class: "sep" }), listHost]), [
            { text: "关闭" },
            { text: "扫描勾选的会话", primary: true,
              run: function () { doScan(); return false; } },
          ]);

        function doScan() {
          const ids = Array.from(picked);
          if (!ids.length) {
            status.textContent = "一个都没勾 —— 先点几行。";
            return;
          }
          dlg.close();
          ctx.logAuto("补经验：开始扫 " + ids.length + " 个会话");
          // 长任务：POST /learn 回 {job:{id}}，之后由壳轮询 GET /jobs/<id>。
          ctx.runTask({
            submit: function () { return ctx.post("/learn", { sessions: ids }); },
            onTick: function (job) {
              const m = job && (job.message || job.state);
              if (m) ctx.logAuto("扫会话：" + m);
            },
          }).then(function () {
            ctx.logAuto("补经验：扫完 " + ids.length + " 个会话，待确认清单已更新");
            say("扫完 " + ids.length + " 个会话，下面是新的待确认清单。", "ok");
            load("pending", "");
          }).catch(function (e) {
            ctx.logAuto("补经验失败：" + msgOf(e));
            say("补经验失败：" + msgOf(e), "err");
          });
        }
      }

      // ================================================================ 接线

      function runQuery() {
        const q = qInput.value.trim();
        load(q ? "query" : "all", q);
      }
      btnQuery.addEventListener("click", runQuery);
      qInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") runQuery();
      });
      btnAll.addEventListener("click", function () { load("all", ""); });
      btnCheckup.addEventListener("click", function () { load("checkup", ""); });
      btnPending.addEventListener("click", function () { load("pending", ""); });
      btnDelete.addEventListener("click", function () { doDelete(); });
      btnAdopt.addEventListener("click", function () { doMark("adopted"); });
      btnDrop.addEventListener("click", function () { doMark("dropped"); });
      // 两个入口各自把"改哪条 / 新增"定死：修改走 doEdit（先校验选中），
      // 增添直接传 null 开空表单 —— 编辑器不再自己看选中状态（见 openEditor）。
      btnEdit.addEventListener("click", function () { doEdit(); });
      btnAdd.addEventListener("click", function () { openEditor(null); });
      btnSessions.addEventListener("click", function () { openSessions(); });

      updateActions();
      // ⚠ 这里**不用 await**：`render` 是对象字面量里的 async 方法，而
      //   tools/check_js_syntax.py 的「非 async 函数里用了 await」那一条只认
      //   `async function 名字(...)`，不认方法简写 —— 它把 code 上量出来的函数
      //   范围拿去切**注释删短之后**的文本（索引漂移），于是 render 尾部这个
      //   await 会被算到文件里最后一个普通函数的账上，报一条假阳性。
      //   直接 `return` 这个 Promise 语义完全一样（壳那边本来就 await render），
      //   顺带绕开那个坑；真要在这里 await，就先把检查器的索引 bug 修掉。
      return load("all", "");
    },
  });
})();
