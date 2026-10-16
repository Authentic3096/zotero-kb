/* ============================================================================
 * admin-pages/structure.js —— 「知识库结构」页（库在哪、每个文件夹装什么、能删吗）
 *
 * 这是 W1 的两页之一（另一页是状态数字，和这一页同一张卡片流里）。Tk 面板里的
 * 对应实现是 `tools/panels/tab_struct.py（已退役 2026-10-14）::_build_struct_tab / refresh_struct`
 * —— 列名与口径都照它，只是取数从"直接读磁盘"换成"问宿主半 → 引擎 kb.structure"。
 *
 * 实现约定（照抄 admin.js 顶部那三条）：
 *   · 只画自己这一页，取数一律 `KBAdmin.get/post(...)`（**不要**直接 fetch 8765）；
 *   · 样式只用 admin.css 里的类（.card / .row / .grid / .num / .num-label，
 *     按钮是 .btn 系列、提示条是 .note 系列），**不写颜色字面量** ——
 *     那套令牌与 Tk 侧 theme.py 是同名的；
 *     ⚠ 教训：块注释里**不能**让通配类名（星号）紧跟斜杠 —— 那两个字符会
 *       提前闭合注释。本目录 10 个脚本原来全是这么坏的（整页脚本直接不注册），
 *       所以写注释时一律把类名用「系列」描述，不写通配符紧邻斜杠。
 *   · 一页一文件，改这一页不用动别人。
 *
 * 依赖的引擎字段（2026-10-13 与引擎作者对齐过，见 online/engine.py）：
 *   kb.status    → {items, chunks, vectors, embed_model, built_at,
 *                   experience_rows, weighted_items, vector_ready, fulltext_chars}
 *   kb.structure → {kb_dir, total_size, total_size_text, generated_at,
 *                   rows:[{name, what, count, size, size_text, deletable, note}]}
 *   `deletable` 是**短标签**（绝不删 / 可删 / 看情况），长解释在 `note` 里。
 * ==========================================================================*/
(function () {
  "use strict";

  /**
   * 大整数的人读写法：**千位分隔**，不用"万/亿"。
   *
   * ⚠ 第一版写的是 `(n/10000).toFixed(1) + " 万"`（16,426 → "1.6 万"），用户看了
   *   直接说"那个万看着奇怪"。中文语境里"万"是对的量级词，但**这里是计数不是金额**，
   *   而且和旁边的 K/MB（KB、MB）混在一屏里，量级词反而要换算。改成 16,426 这种
   *   写法：不用心算、和"占用"列的 96.2 MB 风格一致。
   */
  function humanInt(v) {
    const n = Number(v);
    if (!isFinite(n)) return (v == null || v === "") ? "—" : String(v);
    return n.toLocaleString("en-US");
  }

  /**
   * 「能删吗」的短标签。
   *
   * ⚠ 引擎现在给的就是短标签（see engine.py::_SAFE_SHORT），这里只是**兜底**：
   *   老服务端可能给 Tk 面板那种整句（"✗ 绝对不能删，先备份"），按**首符号**判
   *   —— 符号语义来自 tools/panels/common.py（已退役 2026-10-14） 的那份清单，不在页面另立一套。
   */
  const SAFE_BY_CHAR = { "✗": "绝不删", "○": "可删", "✓": "可删",
                         "△": "看情况", "?": "看情况" };
  const SAFE_SHORT = { "绝不删": "绝不删", "可删": "可删", "看情况": "看情况" };
  function safeShort(v) {
    if (v === true) return "可删";
    if (v === false) return "绝不删";
    const t = String(v == null ? "" : v).replace(/\*\*/g, "").trim();
    if (!t) return "看情况";
    if (SAFE_SHORT[t]) return SAFE_SHORT[t];
    return SAFE_BY_CHAR[t.charAt(0)] || "看情况";
  }

  function stripMd(s) {
    return String(s == null ? "" : s).replace(/\*\*/g, "");
  }

  /**
   * 一行的字节数 —— 给「占用」列排序用。
   *
   * ⚠ 排序必须按**字节原值**（"9.9 MB" 排在 "64.0 MB" 后面、GB 与 MB 混着比都是错的），
   *   所以：优先用契约里的 `size`（字节）；**没有就反过来解析 `size_text`**
   *   —— 有的服务端只给人读串（本项目的预览假数据就是这种：除了 index.db
   *   都只有 size_text），解析不出来时返回 NaN（排序时沉底）。
   */
  const SIZE_UNIT = { B: 1, KB: 1024, MB: 1048576, GB: 1073741824 };
  function sizeBytes(r) {
    const n = Number(r && r.size);
    if (isFinite(n) && n > 0) return n;
    const m = /^([0-9][0-9,.]*)\s*(B|KB|MB|GB)?$/i
      .exec(String((r && r.size_text) || "").trim());
    if (!m) return NaN;
    const v = Number(m[1].replace(/,/g, ""));
    if (!isFinite(v)) return NaN;
    return v * (SIZE_UNIT[(m[2] || "B").toUpperCase()] || 1);
  }

  KBAdmin.page({
    id: "structure",
    title: "知识库结构",
    order: 10,

    async render(host, ctx) {
      const el = ctx.el;

      // ---------------------------------------------------------- 状态数字块
      const statRow = el("div", { class: "row" },
                         [el("span", { class: "subtle", text: "正在读取…" })]);
      const statCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "知识库状态" }),
          el("span", { class: "faint", text: "文献 / 切片 / 向量 / 经验" }),
          el("span", { class: "grow" }),
          el("button", {
            class: "btn btn-subtle", text: "刷新",
            on: { click: function () { KBAdmin.show("structure"); } },
          }),
        ]),
        el("div", { class: "sep" }),
        statRow,
      ]);
      host.appendChild(statCard);

      // ---------------------------------------------------------- 结构表
      // `.numtab` 是**本页私有的作用域类**：数值列（内容量 / 占用）整列右对齐的
      // 规则写在 admin.css 里、只挂在它下面，别的页面的表格不受影响。
      const tableHost = el("div", { class: "numtab" },
                           [el("div", { class: "note note-warn", text: "正在读知识库结构…" })]);
      const tableCard = el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "知识库结构" }),
          el("span", { class: "faint",
                       text: "每个文件夹装什么、占多少、能不能删" }),
        ]),
        el("div", { class: "sep" }),
        tableHost,
        el("div", { class: "sep" }),
        el("div", { class: "faint", id: "kb-struct-note", text: "" }),
      ]);
      host.appendChild(tableCard);

      let status = null;
      let struct = null;

      // 两路取数各自失败各自报 —— 一个挂了不该把另一块也变成空白。
      try {
        status = await KBAdmin.get("/kb-status");
      } catch (e) {
        statRow.textContent = "";
        statRow.appendChild(el("div", {
          class: "note note-err",
          text: "读知识库状态失败：" + (e && e.message ? e.message : e),
        }));
      }
      try {
        struct = await KBAdmin.get("/kb-structure");
      } catch (e2) {
        tableHost.textContent = "";
        tableHost.appendChild(el("div", {
          class: "note note-err",
          text: "读知识库结构失败：" + (e2 && e2.message ? e2.message : e2),
        }));
      }

      // ---------------------------------------------------------- 画状态数字
      if (status) {
        statRow.textContent = "";
        const stat = function (label, value, title) {
          const num = el("div", { class: "num", text: String(value) });
          if (title) num.title = title;
          return el("div", { class: "stat" }, [
            num, el("div", { class: "num-label", text: label }),
          ]);
        };
        statRow.appendChild(stat("文献", humanInt(status.items), "items"));
        statRow.appendChild(stat("切片", humanInt(status.chunks), "chunks"));
        statRow.appendChild(stat("向量", humanInt(status.vectors),
                                status.vector_ready === false
                                  ? "还没建向量" : "embeddings"));
        statRow.appendChild(stat("经验", humanInt(status.experience_rows),
                                "经验库里攒下来的尝试记录"));
        statRow.appendChild(stat("正文字数", humanInt(status.fulltext_chars),
                                "全库正文总字符数"));
        statRow.appendChild(stat("标重点", humanInt(status.weighted_items),
                                "被标为重点 / 有人工权重的文献"));
        const meta = el("span", { class: "faint" },
          [String(status.embed_model || "") +
            (status.built_at ? "　·　建库 " + status.built_at : "")]);
        statCard.querySelector(".row").appendChild(meta);
      }

      // ---------------------------------------------------------- 画结构表
      if (!struct) return;
      const rows = Array.isArray(struct) ? struct : (struct.rows || []);

      /**
       * 内容量这一格：**直接显示引擎给的值**。
       *
       * ⚠ 口径已统一（2026-10-16）：引擎对 `index.db` 那一行已经给真实数字
       *   （`N 篇 / M 切片`，见 `online/engine.py::_db_count_text`）。
       *   早先引擎给的是"见上面状态栏"这种指着别处的占位话、由这里去状态块取数
       *   替换 —— 两处口径迟早会漂，所以那段替换逻辑已删。
       */
      const countText = function (r) {
        return String(r.count == null ? "" : r.count);
      };

      /**
       * 「内容量 / 占用」这两列**整列右对齐**（表头也右对齐）—— 对齐交给 CSS。
       *
       * 为什么不在这里拆「数字 + 单位」（2026-10-13 的第二版试过 .n / .u 两个盒子）：
       * 拆开的做法是把"数字定宽右对齐 + 单位紧跟左对齐"的**整块左对齐**，
       * 于是 5.2em 的定宽盒在数字左边留出一段空白（实测 60px），数字看着离表头很远；
       * 更糟的是列宽由 auto 表格布局随窗口分（实测 1240px 下 193px、1600px 下 262px），
       * 列一宽，右半截又空出来 —— 数字块既不贴左也不贴右，浮在列中间，
       * 这就是用户说的"窗口拉宽以后更难看"。**根因是"块没有锚在列的边上"**。
       * 现在把整列（含表头）锚在**列的右内边距**上：值右边缘、表头右边缘同一条线，
       * 列再宽也只是左边空，两边都不漂。数值用 tabular-nums 保证数字等宽。
       * 具体规则在 admin.css 的 `.numtab` 一段（只作用于本页，不影响别的表）。
       */
      const cols = [
        // 表头排序：可排的列按 admin.js::table 注释里的统一口径挑。
        // 「存放内容」是长自由文本、「内容量」是"16,426 篇 / 6,179 切片"这种
        // 复合文本（排它没有意义），两个都**故意不给 sortable**。
        { key: "name", label: "文件 / 文件夹", sortable: true,
          render: function (r) {
            return el("span", { class: "mono", text: String(r.name || "") });
          } },
        { key: "what", label: "存放内容",
          render: function (r) { return stripMd(r.what); } },
        { key: "count", label: "内容量", tdClass: "nowrap",
          render: function (r) { return countText(r); } },
        // 占用的**显示**是 "96.2 MB" 这种人读串，排序必须按**字节原值** ——
        // 拿显示串排会得到 "9.9 MB" 在 "64.0 MB" 后面、GB 与 MB 混着比。
        { key: "size_text", label: "占用", mono: true, sortable: true,
          sortValue: sizeBytes,
          render: function (r) {
            return String(r.size_text || (r.size == null ? "—" : r.size));
          } },
        { key: "deletable", label: "能删吗", sortable: true,
          render: function (r) {
            // 短标签在格子里，长解释（note）进 title 悬停 —— 与 Tk 面板
            // tab_struct.py 的 _safe_short + _bind_struct_tips 同一套做法。
            const short = safeShort(r.deletable);
            const s = el("span", { text: short });
            const note = stripMd(r.note).trim();
            s.title = note || short;
            return s;
          } },
      ];

      tableHost.textContent = "";
      if (!rows.length) {
        tableHost.appendChild(el("div", { class: "note note-warn",
          text: "结构表是空的（知识库目录还没建出来？）" }));
      } else {
        tableHost.appendChild(KBAdmin.table(cols, rows));
      }

      const note = document.getElementById("kb-struct-note");
      if (note) {
        const kb = struct.kb_dir ? String(struct.kb_dir) : "";
        const total = struct.total_size_text
          || (struct.total_size == null ? "" : String(struct.total_size));
        note.textContent = (kb ? ("知识库根目录：" + kb) : "")
          + (total ? ("　·　合计 " + total) : "")
          + (struct.generated_at ? ("　·　统计于 " + struct.generated_at) : "");
        if (kb) note.title = kb;
      }
      ctx.log("知识库结构：" + rows.length + " 行");
    },
  });
})();
