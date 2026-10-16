/* ============================================================================
 * admin-pages/advanced.js —— 「高级」页（检索 / 权重 / 维护 / 分类重整 / 插件与升级）
 *
 * 对应**已退役（2026-10-14）**的 Tk 面板 tools/panels/tab_advanced.py（已退役 2026-10-14）。功能照它，**布局不照它**：
 * Tk 版把二十来个按钮塞进几个 LabelFrame，网页这版按「一组一件事」拆成小卡片，
 * 每张卡片最多 5 个按钮（用户明确要「简约干净」——一行摆满等于没有重点）。
 *
 * 依赖的引擎方法（契约见开发说明，2026-10-13 冻结）：
 *   POST /search       {query, limit?} → {rows:[{key,title,score,snippet}]}      60s
 *   POST /weight       {key, pinned?, manual?, note?} → {ok, weight}           默认
 *   GET  /weight-list  {limit?} → {rows:[{key,title,year,weight,pinned,note}]}   60s
 *        ⚠ 页面固定发 limit=0（引擎不截断）= 取全量，再做客户端分页（见权重榜那段）。
 *   POST /maintain     {task:"stats"|"views"|"sync-skill"} → {task, ok, output} 120s
 *   POST /zotero-sync  {action:check|authorize|plan|apply|verify}
 *                      → {action, ok, output}                                  180s
 *   POST /plugin       {action:build|sideload-status|sideload-install|sideload-remove}
 *                      → {action, ok, output}                                  300s
 *   POST /zotero-upgrade {action:check|backup|verify} → {action, ok, output}    300s
 *
 * ⚠ 超时必须显式给：admin.js 的默认超时是 30s，而这些动作是真起子进程
 *   （打包 xpi / 备份 sqlite / 跑分类重整），30s 会在半路把**还在跑**的请求
 *   判成失败。所以每条路径都按契约表把超时写死在调用点。
 *
 * ⚠ 会改数据的动作一律 btn-danger + 二次确认：`zotero-sync apply`（真改 Zotero
 *   分类）、`plugin build / sideload-install / sideload-remove`（开发用）。
 *   只读的 `sideload-status` 不做确认 —— 看一个状态还要点确认是折磨用户。
 *
 * `output` 一律按原样文本进底部只读区（契约三条硬口径第 1 条）：不解析成
 * 结构化字段（子进程输出格式会漂移，解析必然坏）。
 *
 * 三条硬约束（与 structure.js / experience.js 完全一致）：
 *   · 只画自己这一页，取数一律 KBAdmin.get/post，不直接 fetch 本机服务；
 *   · 样式只用 admin.css 的类，不写颜色字面量、不写内联 style；
 *   · 一页一个文件、不 import。
 *
 * ⚠ 盘点报告核实过：`tools/diagnose_plugin.py` 与 `tools/zotero_js.py` 两个文件
 *   **都不存在**（Tk 面板里那两个按钮点了必然报「找不到文件」）——本页**不做**
 *   这两个按钮，别照着 Tk 版的按钮清单抄。
 * ==========================================================================*/
(function () {
  "use strict";

  function msgOf(e) { return (e && e.message) ? e.message : String(e); }

  /** 长文本掐短：格子里只留关键词，全文放 title 悬停（用户要「简约」）。 */
  function clip(s, n) {
    const t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  /** 数值格：有限数就保留 3 位，其余原样（score / weight 可能是字符串）。 */
  function numText(v, digits) {
    const n = Number(v);
    return isFinite(n) ? n.toFixed(digits == null ? 3 : digits) : String(v == null ? "" : v);
  }

  /** 二次确认（会改数据的动作先过它，语义与 experience.js / suggest.js 一致）。 */
  function confirmBox(ctx, title, body, okText) {
    return new Promise(function (resolve) {
      let done = false;
      const dlg = ctx.dialog(title, body, [
        { text: "取消", run: function () { done = true; resolve(false); return true; } },
        { text: okText, danger: true,
          run: function () { done = true; resolve(true); return true; } },
      ]);
      dlg.addEventListener("close", function () { if (!done) resolve(false); });
    });
  }

  /** 「按钮 + 一行用途」的动作行（用户要求：每行说明用途）。 */
  function actionRow(el, btn, desc) {
    return el("div", { class: "row" }, [
      btn,
      el("span", { class: "faint", text: desc }),
    ]);
  }

  KBAdmin.page({
    id: "advanced",
    title: "高级",
    order: 90,

    async render(host, ctx) {
      const el = ctx.el;
      // 索引重建是长任务，走壳里的 runTask（提交 → 轮询 /jobs/<id>）。
      const runTask = ctx.runTask;

      // ============================================================ 底部只读输出区
      // 先建出来（所有动作都往这里写原样文本）。
      const outHost = el("div", null,
        [el("div", { class: "faint",
                     text: "（子进程的原样输出会显示在这里）" })]);

      /**
       * 把一段原样文本放进只读区。
       *
       * ⚠ 逐行塞独立 div 而不是用 pre：pre 自带 UA 的 margin，而这里不许写内联
       *   style 去压它；一行一个 div 既保住换行、又完全落在已有类上。
       */
      function showOut(text, kind) {
        outHost.textContent = "";
        let cls = "mono";
        if (kind === "err") cls = "mono note-err";
        else if (kind === "ok") cls = "mono note-ok";
        const box = el("div", { class: cls });
        String(text == null ? "" : text).split(/\r?\n/).forEach(function (ln) {
          box.appendChild(el("div", { text: ln === "" ? " " : ln }));
        });
        outHost.appendChild(box);
      }

      // ============================================================ 卡片：检索
      const qInput = el("input", { type: "text", size: "40",
                                   placeholder: "检索词（中英各试一次命中率更高）" });
      const btnSearch = el("button", { class: "btn btn-primary", text: "搜索",
        title: "在知识库全文里检索（关键词 + 向量两路）" });
      const searchHost = el("div", null, [el("div", { class: "faint",
        text: "输入关键词后点「搜索」。" })]);

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "检索" }),
          el("span", { class: "faint", text: "在知识库里找文献，结果带 key 便于复制" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [qInput, btnSearch]),
        searchHost,
      ]));

      // ============================================================ 卡片：标重点
      const wKey = el("input", { type: "text", size: "14",
                                 placeholder: "文献 key（8 位）" });
      const wPin = el("input", { type: "checkbox" });
      const wNote = el("input", { type: "text", size: "30",
                                  placeholder: "备注（可选）" });
      const btnWeight = el("button", { class: "btn btn-primary", text: "应用",
        title: "勾选「标为重点」则权重乘数约 1.0 → 2.4 倍；不勾就是取消重点" });

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "标重点" }),
          el("span", { class: "faint",
            text: "调某一篇的检索权重：标为重点后它在检索里明显靠前" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [
          wKey,
          el("label", { class: "inline" }, [wPin, el("span", { text: " 标为重点" })]),
          wNote,
          btnWeight,
        ]),
      ]));

      // ============================================================ 卡片：权重榜
      //
      // 「每屏」= 自绘下拉（`ctx.select`），不是原生 <select>：Zotero 的 <browser>
      // 里原生 select 的弹层出不来的（见 admin.js 顶部那段）。
      //
      // 分页（2026-10-13 用户："我一共 12 篇，改显示 10 篇以后，没有下一页功能，
      //   多余的直接看不见了"）：用**客户端分页** —— 一次把全量取回来（引擎
      //   `advanced.weightList` 的 limit 传 0 = 不截断），"每屏"只在本地切页。
      //   为什么不用引擎加 offset：本机一共 12 条、真实场景也就几十上百条，全量取回
      //   毫无压力；而给引擎加 offset 要动 online/engine.py + 契约 + 桩测试，为一个
      //   翻页按钮不值得。目的只有一个：**多余的一条都不许看不见**。
      const wState = { rows: [], page: 1, size: 20, sortKey: "", sortDir: "asc" };
      const wLimit = ctx.select("每屏", [10, 20, 50, 100].map(function (n) {
        return { value: String(n), text: String(n) + " 条" };
      }), "20", function (v) {
        // 改每屏 = 换一种每页装多少条 → **回到第 1 页**（否则停在一个可能不存在的页上）。
        wState.size = Number(v) || 20;
        wState.page = 1;
        paintWeights();
      });
      wLimit.title = "每页显示多少条；超出的用下面的「上一页 / 下一页」翻";
      const btnWReload = el("button", { class: "btn btn-subtle", text: "刷新",
        title: "重新读权重榜" });
      const wPrev = el("button", { type: "button", class: "btn btn-subtle",
                                   text: "上一页" });
      const wNext = el("button", { type: "button", class: "btn btn-subtle",
                                   text: "下一页" });
      const wPageInfo = el("span", { class: "faint", text: "" });
      const wHost = el("div", null,
        [el("div", { class: "note note-warn", text: "正在读权重榜…" })]);

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "权重榜" }),
          el("span", { class: "faint", text: "被加权 / 标重点过的文献" }),
          el("span", { class: "grow" }),
          wLimit, btnWReload,
        ]),
        el("div", { class: "sep" }),
        wHost,
      ]));

      // ============================================================ 卡片：索引重建
      //
      // 这两件事原来是命令行（`scripts\1-convert.cmd`）；面板补上入口，插件里
      // 几处提示（「手动更新（增量）」）也才有对应的按钮可指。
      //
      // ⚠ 它们是**长任务**：`POST /reindex` 派发即返回 `{job:{id}}`，之后由
      //   ctx.runTask 轮询 `/jobs/<id>` —— 与 PDF 解析页同一套，进度打到底部日志。
      const incrBtn = el("button", {
        class: "btn btn-subtle", text: "增量更新",
        title: "只处理变过的条目（秒级）。新加了文献、或改了元数据之后点它。",
      });
      const fullBtn = el("button", {
        class: "btn btn-danger", text: "全部重建",
        title: "全库重跑：重新解析正文、重切块、重算向量（100 篇约 1~3 分钟）。"
             + "会二次确认。",
      });

      const rebuildKids = [
        el("div", { class: "row" }, [
          el("strong", { text: "索引重建" }),
          el("span", { class: "faint",
                       text: "改了 Zotero 库之后让知识库跟上；进度在底部日志里" }),
        ]),
        el("div", { class: "sep" }),
        actionRow(el, incrBtn, "只处理变过的条目，秒级；日常新加文献后用这个"),
        actionRow(el, fullBtn, "全库重跑（解析 / 切片 / 向量），会二次确认"),
      ];
      host.appendChild(el("div", { class: "card" }, rebuildKids));

      // ============================================================ 卡片：维护动作
      const MAINTAIN = [
        { task: "stats", text: "统计",
          desc: "知识库各表有多少行（条目 / 切片 / 向量 / 经验）。" },
        { task: "check", text: "环境自检",
          desc: "查依赖 / 索引 / Zotero 本地 API / Ollama 是否就绪（几秒）。" },
        { task: "backup", text: "备份索引",
          desc: "把 index.db（含经验层与权重）复制一份到知识库的 backups\\ 下。" },
        { task: "reconcile", text: "对账巡检",
          desc: "只读：核对知识库与 Zotero 的差异（归档 / 磁盘残留 / 失效引用），不改数据。" },
        { task: "views", text: "补齐分级文件",
          desc: "给每篇补「摘要与要点 / 图注与表格 / 权重与经验」——"
              + "侧栏对照页和右键菜单读的就是它。" },
        { task: "sync-skill", text: "重载技能到 DSH",
          desc: "改了 SKILL.md 之后同步到 DSH 技能目录（复制，不会自动同步）。" },
      ];

      const maintKids = [
        el("div", { class: "row" }, [
          el("strong", { text: "维护动作" }),
          el("span", { class: "faint", text: "一次性的整理动作，输出在下面的只读区" }),
        ]),
        el("div", { class: "sep" }),
      ];
      MAINTAIN.forEach(function (a) {
        const b = el("button", { class: "btn btn-subtle", text: a.text,
                                 title: a.desc });
        b.addEventListener("click", function () { doMaintain(a.task); });
        maintKids.push(actionRow(el, b, a.desc));
      });
      host.appendChild(el("div", { class: "card" }, maintKids));

      // ============================================================ 卡片：分类重整
      // 五步是有顺序的（探测 → 授权 → 干跑 → 执行 → 核对），按钮文字里带序号。
      const SYNC = [
        { action: "check", text: "1 探测写入能力", desc: "看能不能连上 Zotero。" },
        { action: "authorize", text: "2 申请授权", desc: "Zotero 会弹窗，要允许。" },
        { action: "plan", text: "3 干跑（只看要动什么）", desc: "不修改任何东西。" },
        { action: "apply", text: "4 执行重整", desc: "真的改 Zotero 里的分类。先干跑。",
          danger: true },
        { action: "verify", text: "5 核对结果", desc: "改完检查一遍。" },
      ];

      const syncKids = [
        el("div", { class: "row" }, [
          el("strong", { text: "分类重整" }),
          el("span", { class: "faint",
            text: "按这五步改 Zotero 里的分类归属 —— 只有第 4 步会真的改" }),
        ]),
        el("div", { class: "sep" }),
      ];
      SYNC.forEach(function (a) {
        const b = el("button", {
          class: a.danger ? "btn btn-danger" : "btn btn-subtle",
          text: a.text, title: a.desc });
        b.addEventListener("click", function () {
          doZoteroSync(a.action, a.danger, a.text);
        });
        syncKids.push(actionRow(el, b, a.desc));
      });
      host.appendChild(el("div", { class: "card" }, syncKids));

      // ============================================================ 卡片：插件
      const PLUGIN = [
        { action: "build", text: "打包插件 xpi", danger: true,
          desc: "改过插件代码后重新打包（开发用，会重建 xpi）。" },
        { action: "sideload-status", text: "侧载状态", danger: false,
          desc: "看当前侧载情况（只读，不改任何东西）。" },
        { action: "sideload-install", text: "侧载安装", danger: true,
          desc: "UI 装不上时的备用安装方式（开发用，要 Zotero 关着）。" },
        { action: "sideload-remove", text: "卸载侧载", danger: true,
          desc: "撤销侧载（开发用）。" },
      ];

      const pluginKids = [
        el("div", { class: "row" }, [
          el("strong", { text: "插件" }),
          el("span", { class: "faint", text: "只在重装 / 开发插件时需要" }),
        ]),
        el("div", { class: "sep" }),
      ];
      PLUGIN.forEach(function (a) {
        const b = el("button", {
          class: a.danger ? "btn btn-danger" : "btn btn-subtle",
          text: a.text, title: a.desc });
        b.addEventListener("click", function () {
          doPlugin(a.action, a.danger, a.text);
        });
        pluginKids.push(actionRow(el, b, a.desc));
      });
      host.appendChild(el("div", { class: "card" }, pluginKids));

      // ============================================================ 卡片：Zotero 升级
      const UPGRADE = [
        { action: "check", text: "升级前检查", desc: "查兼容性。" },
        { action: "backup", text: "备份数据", desc: "备份 zotero.sqlite。" },
        { action: "verify", text: "升级后核对", desc: "确认数据没丢。" },
      ];

      const upKids = [
        el("div", { class: "row" }, [
          el("strong", { text: "Zotero 升级" }),
          el("span", { class: "faint", text: "升级 Zotero 前后各跑一次" }),
        ]),
        el("div", { class: "sep" }),
      ];
      UPGRADE.forEach(function (a) {
        const b = el("button", { class: "btn btn-subtle", text: a.text,
                                 title: a.desc });
        b.addEventListener("click", function () { doUpgrade(a.action, a.text); });
        upKids.push(actionRow(el, b, a.desc));
      });
      host.appendChild(el("div", { class: "card" }, upKids));

      // ============================================================ 卡片：输出（放最后）
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "输出" }),
          el("span", { class: "faint",
            text: "子进程的原样文本 —— 不解析成字段（解析必然漂移）" }),
        ]),
        el("div", { class: "sep" }),
        outHost,
      ]));

      // ============================================================ 动作实现

      /** 取子进程原样文本（返回里没 output 就退化成 JSON，至少不显示空白）。 */
      function outputOf(r, fallback) {
        if (r && r.output != null) return String(r.output);
        if (r == null) return fallback || "（没有回包）";
        return JSON.stringify(r);
      }

      async function doSearch() {
        const q = qInput.value.trim();
        if (!q) {
          showOut("请输入检索词（中英各试一次命中率更高）。", "err");
          return;
        }
        searchHost.textContent = "";
        searchHost.appendChild(el("div", { class: "note note-warn",
          text: "正在检索…" }));
        try {
          const r = await ctx.post("/search", { query: q, limit: 12 }, null, 60000);
          const rows = (r && r.rows) || (Array.isArray(r) ? r : []);
          renderSearch(rows, q);
          ctx.logAuto("检索「" + q + "」：" + rows.length + " 条");
        } catch (e) {
          searchHost.textContent = "";
          searchHost.appendChild(el("div", { class: "note note-err",
            text: "检索失败：" + msgOf(e) }));
          ctx.logAuto("检索失败：" + msgOf(e));
        }
      }

      function renderSearch(rows, q) {
        searchHost.textContent = "";
        if (!rows.length) {
          searchHost.appendChild(el("div", { class: "note note-warn",
            text: "「" + q + "」没有命中。试试换同义词，中英各一次。" }));
          return;
        }
        const cols = [
          // 表头排序口径见 admin.js::table：key / 标题 / 得分可排；
          // 「命中片段」是长自由文本，**不给排序**。
          { key: "key", label: "key", mono: true, sortable: true },
          { key: "title", label: "标题", sortable: true,
            sortValue: function (r) { return String(r.title || ""); },
            render: function (r) {
              const t = String(r.title || "");
              const s = el("span", { text: clip(t, 46) || "（无标题）" });
              s.title = t;
              return s;
            } },
          { key: "score", label: "得分", sortable: true,
            sortValue: function (r) { return Number(r.score); },
            render: function (r) {
              return el("span", { class: "mono", text: numText(r.score, 3) });
            } },
          { key: "snippet", label: "命中片段", render: function (r) {
              const t = String(r.snippet || "");
              const s = el("span", { text: clip(t, 56) || "—" });
              s.title = t;
              return s;
            } },
        ];
        searchHost.appendChild(ctx.table(cols, rows));
        searchHost.appendChild(el("div", { class: "faint",
          text: "命中 " + rows.length + " 条（按相关度排序）。" }));
      }

      async function doWeight() {
        const key = wKey.value.trim();
        if (!key) {
          showOut("请先填文献 key（8 位，比如 A97QALZN）。", "err");
          return;
        }
        const pinned = !!wPin.checked;
        const note = wNote.value.trim();
        const body = { key: key, pinned: pinned };
        if (note) body.note = note;
        showOut((pinned ? "标为重点 " : "取消重点 ") + key + "：请求已发出…");
        try {
          const r = await ctx.post("/weight", body);
          showOut((pinned ? "已标为重点 " : "已取消重点 ") + key + "\n"
            + "引擎回包：" + JSON.stringify((r && r.weight) || r || {}), "ok");
          ctx.logAuto((pinned ? "已标为重点 " : "已取消重点 ") + key);
          loadWeights();
        } catch (e) {
          showOut("改权重失败：" + msgOf(e), "err");
          ctx.logAuto("改权重失败：" + msgOf(e));
        }
      }

      /** 总页数（至少 1 页：一条都没有时也显示"第 1/1 页"）。 */
      function pageCount() {
        return Math.max(1, Math.ceil(wState.rows.length / wState.size));
      }

      // ---- 权重榜的表头排序（**客户端分页**的表要特殊处理）-------------------
      //
      // ⚠ 这一张表是本地切页的（引擎一次给全量、页面按"每屏"切），而 `ctx.table`
      //   只对**传给它的这批行**排序 —— 直接给 `sortable` 的话点表头只重排当前
      //   这一页，看着像"排错了"。所以：排序时先自己把**全量**排好、回到第 1 页
      //   重画，再把 `sort` 传给 ctx.table（它按同一列重排当前页，等于幂等）。
      //   这就是 admin.js::table 的 `onSort` 存在的理由。
      const W_SORT = {
        key: function (r) { return String(r.key || ""); },
        title: function (r) { return String(r.title || ""); },
        year: function (r) { return Number(r.year) || 0; },
        weight: function (r) { return Number(r.weight) || 0; },
        pinned: function (r) { return r.pinned ? 1 : 0; },
      };
      function sortWeightRows(key, dir) {
        const f = W_SORT[key];
        if (!f) return;
        const sign = dir === "asc" ? 1 : -1;
        wState.rows.sort(function (a, b) {
          const va = f(a), vb = f(b);
          if (typeof va === "number" && typeof vb === "number") {
            return sign * (va - vb);
          }
          return sign * String(va).localeCompare(String(vb), "zh",
                                                 { numeric: true, sensitivity: "base" });
        });
      }

      /** 页码夹紧到 [1, 总页数]（改每屏 / 刷新后都可能越界）。 */
      function clampPage() {
        wState.page = Math.min(Math.max(1, wState.page), pageCount());
      }

      async function loadWeights() {
        wHost.textContent = "";
        wHost.appendChild(el("div", { class: "note note-warn",
          text: "正在读权重榜…" }));
        try {
          // ⚠ limit=0 = 不要引擎截断（客户端自己切页），路径带上它
          //   （预览桩是按**完整路径含查询串**取假数据的）。
          const r = await ctx.get("/weight-list?limit=0", null, 60000);
          wState.rows = (r && r.rows) || (Array.isArray(r) ? r : []);
          clampPage();
          paintWeights();
          ctx.logAuto("权重榜：" + wState.rows.length + " 条（第 "
            + wState.page + "/" + pageCount() + " 页）");
        } catch (e) {
          wHost.textContent = "";
          wHost.appendChild(el("div", { class: "note note-err",
            text: "读权重榜失败：" + msgOf(e) }));
          ctx.logAuto("读权重榜失败：" + msgOf(e));
        }
      }

      /** 画当前这一页 + 下面的分页行（全量在 wState.rows 里，切页不发请求）。 */
      function paintWeights() {
        clampPage();
        const all = wState.rows;
        const n = pageCount();
        // 边界：第 1 页没有上一页、最后一页没有下一页 —— 只禁用，不隐藏
        // （按钮消失会让人以为功能没做；置灰 + title 说清原因才是本项目的口径）。
        const rows = all.slice((wState.page - 1) * wState.size,
                               (wState.page - 1) * wState.size + wState.size);

        wHost.textContent = "";
        if (!all.length) {
          wHost.appendChild(el("div", { class: "note note-warn",
            text: "还没有被加权 / 标重点的文献。" }));
          return;
        }
        const cols = [
          // 表头排序口径见 admin.js::table：key / 标题 / 年份 / 权重 / 重点可排；
          // 「备注」是自由文本，**不给排序**。分页表的全量排序见 sortWeightRows。
          { key: "key", label: "key", mono: true, sortable: true },
          { key: "title", label: "标题", sortable: true,
            render: function (r) {
              const t = String(r.title || "");
              const s = el("span", { text: clip(t, 40) || "（无标题）" });
              s.title = t;
              return s;
            } },
          { key: "year", label: "年份", sortable: true,
            sortValue: function (r) { return Number(r.year); } },
          { key: "weight", label: "权重", sortable: true,
            sortValue: function (r) { return Number(r.weight); },
            render: function (r) {
              return el("span", { class: "mono", text: numText(r.weight, 2) });
            } },
          { key: "pinned", label: "重点", sortable: true,
            sortValue: function (r) { return r.pinned ? 1 : 0; },
            render: function (r) {
              return r.pinned
                ? el("span", { class: "tag-ok", text: "重点" })
                : el("span", { class: "faint", text: "—" });
            } },
          { key: "note", label: "备注", render: function (r) {
              const t = String(r.note || "");
              const s = el("span", { text: clip(t, 24) || "—" });
              s.title = t;
              return s;
            } },
        ];
        wHost.appendChild(ctx.table(cols, rows, {
          sort: wState.sortKey ? { key: wState.sortKey, dir: wState.sortDir } : null,
          onSort: function (key, dir) {
            // ⚠ 必须判"真的变了"才动：否则重画 → onSort → 重画会无限打转
            //   （页面重画时会把同一个 sort 再传回来，构造那一刻也会调一次）。
            if (!key || (key === wState.sortKey && dir === wState.sortDir)) return;
            wState.sortKey = key;
            wState.sortDir = dir;
            sortWeightRows(key, dir);
            wState.page = 1;        // 换了排序顺序，页码没有意义了
            paintWeights();
          },
        }));

        wPrev.disabled = wState.page <= 1;
        wNext.disabled = wState.page >= n;
        wPrev.title = wPrev.disabled ? "已经是第 1 页" : "看上一页";
        wNext.title = wNext.disabled ? ("已经是最后一页（共 " + n + " 页）") : "看下一页";
        wPageInfo.textContent = "第 " + wState.page + "/" + n + " 页"
          + "　·　共 " + all.length + " 条"
          + (rows.length !== all.length ? "（这一页 " + rows.length + " 条）" : "");
        wPageInfo.title = "共 " + all.length + " 条，每屏 " + wState.size + " 条";
        wHost.appendChild(el("div", { class: "row" }, [
          wPageInfo,
          el("span", { class: "grow" }),
          wPrev, wNext,
        ]));
      }

      /** 维护动作的中文名（只给日志与提示用，别拿它当分支条件）。 */
      const MAINTAIN_LABEL = {
        stats: "统计", check: "环境自检", backup: "备份索引",
        reconcile: "对账巡检", views: "补齐分级文件", "sync-skill": "重载技能",
        collections: "分类分布", "judge-status": "模型状态",
      };

      async function doMaintain(task) {
        const label = MAINTAIN_LABEL[task] || task;
        showOut(label + "：已发出，等子进程跑完（最多 120s）…");
        try {
          const r = await ctx.post("/maintain", { task: task }, null, 120000);
          const ok = !(r && r.ok === false);
          showOut(label + "：\n" + outputOf(r), ok ? "ok" : "err");
          ctx.logAuto("维护动作「" + label + "」"
                      + (ok ? "完成" : "回包报失败（见上方输出）"));
          // ⚠ 不在这里调 KBAdmin.show("structure") 去"刷新"状态页：那是切页用的，
          //   壳对已渲染过的页是直出缓存（`show()` 见到 `done` 就返回），只会把用户
          //   切走、并不会刷新数据。要看最新数字，到「知识库结构」页按它自己的「刷新」。
        } catch (e) {
          showOut(label + "失败：" + msgOf(e), "err");
          ctx.logAuto("维护动作「" + label + "」失败：" + msgOf(e));
        }
      }

      /**
       * 索引重建（长任务）：`POST /reindex` 派发即返回 `{job:{id}}`，
       * 之后轮询 `/jobs/<id>`；进度进底部日志。
       */
      async function doReindex(full) {
        const label = full ? "全部重建" : "增量更新";
        if (full) {
          const body = el("div", null, [
            el("div", { text: "会重新解析全库正文、重切块、重算向量。" }),
            el("div", { class: "faint",
              text: "100 篇大约 1~3 分钟；跑之前建议先「备份索引」。"
                  + "PDF 没变、档位没变的那批会按指纹跳过。" }),
          ]);
          const ok = await confirmBox(ctx, "确认「全部重建」？", body, "开始重建");
          if (!ok) { showOut("已取消「全部重建」。"); return; }
        }
        showOut(label + "：已派发，等引擎…（进度见底部日志；关掉这一页任务也会继续）");
        ctx.logAuto(label + "：已派发");
        try {
          const job = await runTask({
            submit: function () {
              return ctx.post("/reindex", full ? { full: true } : {});
            },
            interval: 1500,
            onTick: function (j) {
              const logs = Array.isArray(j.log) ? j.log : [];
              const last = logs.length ? String(logs[logs.length - 1]) : "";
              ctx.log("[" + label + "] "
                      + (String(j.message || last || "运行中…"))
                      + (j.percent == null || j.percent === ""
                         ? "" : ("（" + j.percent + "%）")));
            },
          });
          const res = (job && job.result) || job || {};
          showOut(label + "：完成\n" + outputOf(res, JSON.stringify(res)), "ok");
          ctx.logAuto(label + " 完成");
          // 重建会改条目 / 切片数与权重榜，顺手刷新一次权重榜。
          loadWeights();
        } catch (e) {
          showOut(label + "失败：" + msgOf(e), "err");
          ctx.logAuto(label + " 失败：" + msgOf(e));
        }
      }

      async function doZoteroSync(action, needConfirm, label) {
        if (needConfirm) {
          const body = el("div", null, [
            el("div", { class: "mono", text: action }),
            el("div", { class: "faint",
              text: "这一步会真的修改 Zotero 库里的分类归属。建议先跑第 3 步「干跑」。" }),
          ]);
          const ok = await confirmBox(ctx, "确认执行「" + label + "」？", body, "执行");
          if (!ok) { showOut("已取消「" + label + "」。"); return; }
        }
        showOut("分类重整 " + action + "：已发出，等子进程跑完（最多 180s）…");
        try {
          const r = await ctx.post("/zotero-sync", { action: action }, null, 180000);
          showOut("分类重整 " + action + "：\n" + outputOf(r), "ok");
          ctx.logAuto("分类重整 " + action + " 完成");
        } catch (e) {
          showOut("分类重整 " + action + " 失败：" + msgOf(e), "err");
          ctx.logAuto("分类重整 " + action + " 失败：" + msgOf(e));
        }
      }

      async function doPlugin(action, needConfirm, label) {
        if (needConfirm) {
          const body = el("div", null, [
            el("div", { class: "mono", text: action }),
            el("div", { class: "faint",
              text: "这是开发用的动作：会重新打包插件或改本机的侧载状态。" }),
          ]);
          const ok = await confirmBox(ctx, "确认执行「" + label + "」？", body, "执行");
          if (!ok) { showOut("已取消「" + label + "」。"); return; }
        }
        showOut("插件动作 " + action + "：已发出，等子进程跑完（最多 300s）…");
        try {
          const r = await ctx.post("/plugin", { action: action }, null, 300000);
          showOut("插件动作 " + action + "：\n" + outputOf(r), "ok");
          ctx.logAuto("插件动作 " + action + " 完成");
        } catch (e) {
          showOut("插件动作 " + action + " 失败：" + msgOf(e), "err");
          ctx.logAuto("插件动作 " + action + " 失败：" + msgOf(e));
        }
      }

      async function doUpgrade(action, label) {
        showOut("Zotero 升级 " + action + "：已发出，等子进程跑完（最多 300s）…");
        try {
          const r = await ctx.post("/zotero-upgrade", { action: action }, null, 300000);
          showOut("Zotero 升级 " + action + "：\n" + outputOf(r), "ok");
          ctx.logAuto("Zotero 升级 " + action + "（" + label + "）完成");
        } catch (e) {
          showOut("Zotero 升级 " + action + " 失败：" + msgOf(e), "err");
          ctx.logAuto("Zotero 升级 " + action + " 失败：" + msgOf(e));
        }
      }

      // ============================================================ 接线
      btnSearch.addEventListener("click", function () { doSearch(); });
      qInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") doSearch();
      });
      btnWeight.addEventListener("click", function () { doWeight(); });
      incrBtn.addEventListener("click", function () { doReindex(false); });
      fullBtn.addEventListener("click", function () { doReindex(true); });
      btnWReload.addEventListener("click", function () { loadWeights(); });
      wPrev.addEventListener("click", function () {
        wState.page -= 1;          // clampPage 会把越界夹回来
        paintWeights();
      });
      wNext.addEventListener("click", function () {
        wState.page += 1;
        paintWeights();
      });
      // ⚠ 「每屏」的变更走 ctx.select 的 onChange（在那处传的匿名函数里直接
      //   回到第 1 页重画），**不再需要** change 监听 —— 自绘下拉没有 change 事件。

      // ⚠ 不用 await（见 experience.js / suggest.js 的同一段注释：检查器的
      //   「非 async 函数里用 await」那一条有索引漂移的假阳性）。return 语义一样。
      return loadWeights();
    },
  });
})();
