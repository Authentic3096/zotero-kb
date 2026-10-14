/* ============================================================================
 * admin-pages/prompts.js —— 「提示词」页（喂给模型的话术：看 / 改 / 还原 / 试跑）
 *
 * 对应**已退役（2026-10-14）**的 Tk 面板 tools/panels/tab_prompts.py（已退役 2026-10-14）。话术原来硬编码在 judge.py /
 * metafill.py / check_chunks.py 里，用户看不到也改不了 —— 这一页把它摆出来，
 * 因为"经验抽得不对"这种问题多半只能靠改一句话解决。
 *
 * 取数（字段见开发说明）：
 *   GET  /prompts         {rows:[{id,title,where,placeholders,system,user,is_custom}]}
 *   POST /prompts         {id, field:"system"|"user", text}  /  {id, action:"reset"}
 *   POST /prompts/probe   {id} → {ok, output, ms}
 *
 * ⚠ 保存是**按字段一条一条发**（契约就长这样）：system 与 user 两次 POST，
 *   哪一次失败都会把失败原因如实说在按钮旁边，不静默。
 * ⚠ 「试跑」可能几十秒，而宿主半的单条请求超时是 30 秒（admin.js::HOST_TIMEOUT）
 *   —— 这是壳的限制，页面只能把"运行中…"显示出来；页面里另外写了这句提示。
 * ⚠ 本页的 render 里刻意**不写顶层 await**：tools/check_js_syntax.py 的"普通函数
 *   里用了 await"那条附加检查有个 bug —— 它把 `noComment`（已删注释的文本）
 *   按**原始**偏移去切函数体，删掉的注释让偏移整体前移，于是窗口会落到 render
 *   自己的 await 上、把无辜的函数报成错（同目录 structure.js 就被误报了 safeShort）。
 *   页面骨架本来就该先画、数据异步补，所以不 await 既对又躲开了这个误报。
 *
 * 页面约定照抄 admin.js 顶部三条与 structure.js 的写法：只用 admin.css 的类、
 * 数据一律 KBAdmin.get/post、一页一文件、注释说清为什么。
 * ==========================================================================*/
(function () {
  "use strict";

  function msgOf(e) {
    return (e && e.message) ? e.message : String(e);
  }

  KBAdmin.page({
    id: "prompts",
    title: "提示词",
    order: 100,

    async render(host, ctx) {
      const el = ctx.el;

      let rows = [];          // /prompts 的整个列表
      let curId = "";         // 当前选中的那条

      // ------------------------------------------------------ 左：条目列表
      //
      // 用原生 select 的 listbox（size>1）而不是自己画一列按钮：选中态由系统给，
      // 不用靠"换个 btn 类假装高亮"，也不占额外代码；列表短（十条上下）刚好够。
      const picker = el("select", { size: "26" });
      picker.addEventListener("change", function () { select(picker.value); });
      const listNote = el("div", { class: "faint", text: "正在读提示词…" });
      // ⚠ 左卡挂 `.pane-list` 而不是让它按内容撑：这个 select 的固有宽度由**最长的
      //   选项文本**决定（真机 310px），两栏的 min-content 之和超过 #view 的宽度时
      //   `.row` 会把右卡整块折到视口外（用户 2026-10-13 报的"右边是空的"）。
      //   宽度与收缩规则都在 admin.css 的 `.split` 一段里。
      const left = el("div", { class: "card pane-list" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "提示词" }),
          el("span", { class: "faint", text: "模型按什么话术干活" }),
        ]),
        el("div", { class: "sep" }),
        picker,
        el("div", { class: "sep" }),
        listNote,
      ]);

      // ------------------------------------------------------ 右：编辑区
      const whereEl = el("div", { class: "faint", text: "← 左边选一条" });
      // cols 别太大：两栏的**min-content 宽度之和**一旦超过内容区，`.row` 就会把
      // 右栏折到下一行（第一版 cols=88 就是这么折的）。真正的兜底是容器上的
      // `.split`（见文件末尾那处 appendChild 与 admin.css），cols 只管文本框
      // 打开时的初始尺寸。
      const sysTa = el("textarea", { rows: "5", cols: "64" });
      const userTa = el("textarea", { rows: "10", cols: "64" });
      // 只读输出区：试跑的结果放这里（用 textarea 而不是 div，是为了长输出
      // 能滚动、还能让用户选中复制 —— 与 Tk 版那个 ScrolledText 同一用途）。
      const outTa = el("textarea", {
        rows: "6", cols: "64", readonly: "readonly",
        placeholder: "点「试跑」看模型怎么回这一条",
      });
      const actNote = el("div", { class: "faint", text: "" });

      const saveBtn = el("button", {
        class: "btn btn-primary", text: "保存",
        on: { click: save },
      });
      const probeBtn = el("button", {
        class: "btn btn-subtle", text: "试跑",
        on: { click: probe },
      });
      const resetBtn = el("button", {
        class: "btn btn-danger", text: "恢复默认",
        on: { click: askReset },
      });

      const right = el("div", { class: "card grow" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "编辑" }),
          el("span", { class: "faint", text: "改完点「保存」，立即生效、不用重启服务" }),
        ]),
        // 「用在哪 / 占位符 / 改没改过」这行很长，**必须独占一行**：塞进上面的
        // `.row` 会把右卡的 min-content 宽度顶大，flex-wrap 就把两栏折成上下。
        el("div", { class: "sep" }),
        whereEl,
        el("div", { class: "faint", text: "system（角色与纪律）" }),
        sysTa,
        el("div", { class: "faint", text: "user（真正的任务；{占位符} 由代码填内容）" }),
        userTa,
        el("div", { class: "row" }, [
          saveBtn, probeBtn, resetBtn,
          el("span", { class: "faint", text: "试跑：拿固定样例调一次模型，可能要几十秒" }),
          el("span", { class: "grow" }),
          actNote,
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "faint", text: "试跑输出（只读）" }),
        outTa,
      ]);

      // 左右并排。`.row` 是 admin.css 里唯一的横向容器（align-items:center），
      // 左边列成 listbox 撑出高度、右边 grow。
      // ⚠ 必须加 `.split`：它把 flex-wrap 改成 nowrap、并让两栏都能收缩 ——
      //   否则左栏被 listbox 的固有宽度撑到 334px 时，右栏会被折到**视口外**
      //   整块看不见（用户 2026-10-13 报的"提示词页显示不正常"，预览里因为假数据
      //   的标题短而看不出来，见 admin.css 的 .split 一段）。
      host.appendChild(el("div", { class: "row split" }, [left, right]));

      // ========================================================== 取数与画图

      async function load() {
        try {
          const d = await KBAdmin.get("/prompts");
          rows = (d && Array.isArray(d.rows)) ? d.rows : [];
          paint();
        } catch (e) {
          listNote.className = "note note-err";
          listNote.textContent = "读提示词失败：" + msgOf(e);
        }
      }

      function paint() {
        listNote.className = "faint";
        picker.textContent = "";
        // listbox 的宽度由**最长的 option 文本**决定（select 没有 width 属性，
        // 而 admin.css 里也没有能给它定宽的类）——所以出处要截短，否则左侧
        // 这一列会把右边挤到换行去。完整出处在下拉选中后显示（见 select()）。
        const shortWhere = function (w) {
          const s = String(w || "");
          return s.length > 10 ? (s.slice(0, 10) + "…") : s;
        };
        rows.forEach(function (r) {
          const title = String(r.title || r.id || "");
          const where = shortWhere(r.where);
          picker.appendChild(el("option", {
            value: String(r.id || ""),
            text: title + (where ? " · " + where : "")
                     + (r.is_custom ? " · 已改" : ""),
          }));
        });
        if (!rows.length) {
          listNote.textContent = "没有可编辑的提示词（引擎回包是空的）。";
          return;
        }
        // 尽量保住用户当前看的那一条（保存后重画列表不能把选择弹回第一条）
        const alive = rows.some(function (r) { return String(r.id) === curId; });
        if (!alive) curId = String(rows[0].id || "");
        picker.value = curId;
        select(curId);
        listNote.textContent = "共 " + rows.length + " 条；带「已改」的是你改过、"
          + "覆盖了代码默认值的那条。";
      }

      function select(id) {
        const r = rows.filter(function (x) { return String(x.id) === String(id); })[0];
        if (!r) return;
        curId = String(r.id || "");
        picker.value = curId;
        const ph = (r.placeholders || []).map(function (p) { return "{" + p + "}"; })
          .join("、") || "（无）";
        whereEl.textContent = "用在哪：" + (r.where || "—")
          + "　·　占位符：" + ph
          + "　·　" + (r.is_custom ? "当前是改过的版本（可「恢复默认」）" : "当前是默认值");
        whereEl.title = whereEl.textContent;
        sysTa.value = (r.system == null) ? "" : String(r.system);
        userTa.value = (r.user == null) ? "" : String(r.user);
        outTa.value = "";
        actNote.className = "faint";
        actNote.textContent = "";
      }

      // ========================================================== 动作

      async function save() {
        if (!curId) return;
        saveBtn.disabled = true;
        actNote.className = "faint";
        actNote.textContent = "正在保存…";
        const id = curId;
        try {
          // 两次 POST：契约就是"一次一个字段"。顺序无关紧要，都被同一个
          // prompts.save 处理（它会校验占位符与 JSON 契约键，不合法就抛）。
          await KBAdmin.post("/prompts", { id: id, field: "system", text: sysTa.value });
          await KBAdmin.post("/prompts", { id: id, field: "user", text: userTa.value });
          await load();               // 重拉一次才知道 is_custom 变了没有
          select(id);                 // 保住选择，别把用户弹回第一条
          actNote.className = "faint";
          actNote.textContent = "已保存 " + id + "（写进 kb/prompts.json，立即生效）";
        } catch (e) {
          actNote.className = "note note-err";
          actNote.textContent = "保存失败：" + msgOf(e);
        } finally {
          saveBtn.disabled = false;
        }
      }

      function askReset() {
        if (!curId) return;
        const r = rows.filter(function (x) { return String(x.id) === curId; })[0] || {};
        KBAdmin.dialog("恢复默认", el("div", null, [
          el("div", { text: "把「" + (r.title || curId) + "」恢复成代码里的默认话术？" }),
          el("div", { class: "faint",
            text: "改动会丢掉（可以再改回来，不是删数据）。" }),
        ]), [
          { text: "取消" },
          { text: "恢复默认", danger: true, run: doReset },
        ]);
      }

      async function doReset() {
        const id = curId;
        try {
          await KBAdmin.post("/prompts", { id: id, action: "reset" });
          await load();
          select(id);
          actNote.className = "faint";
          actNote.textContent = id + " 已恢复默认";
        } catch (e) {
          actNote.className = "note note-err";
          actNote.textContent = "恢复失败：" + msgOf(e);
        }
      }

      async function probe() {
        if (!curId) return;
        const id = curId;
        probeBtn.disabled = true;
        actNote.className = "faint";
        actNote.textContent = "正在试跑 " + id + "…";
        outTa.value = "运行中…（本地模型一次要几秒到几十秒，别关这一页）";
        try {
          const r = await KBAdmin.post("/prompts/probe", { id: id });
          const parts = [];
          if (r && r.ok === false) parts.push("⚠ " + (r.error || "这一条没跑通"));
          parts.push(String((r && r.output) || "（模型没回内容）"));
          if (r && r.ms) parts.push("\n—— 耗时 " + r.ms + " ms");
          outTa.value = parts.join("\n");
          actNote.className = "faint";
          actNote.textContent = (r && r.ok === false) ? "试跑没跑通" : "试跑完成";
        } catch (e) {
          outTa.value = "";
          actNote.className = "note note-err";
          actNote.textContent = "试跑失败：" + msgOf(e);
        } finally {
          probeBtn.disabled = false;
        }
      }

      // ========================================================== 起步
      //
      // 不 await load()：页面骨架已经画好了，列表是**异步补上**的，render 早
      // 返回一步没坏处（load 自己 try/catch，错误显示在左下的 listNote 里）。
      // 顺带的好处：render 里就没有顶层 await，check_js_syntax.py 那个"删注释后
      // 偏移错位"的误报窗口也就抓不到它了（那个 bug 见文件头说明）。
      load();
      ctx.log("提示词页：开始读提示词列表");
    },
  });
})();
