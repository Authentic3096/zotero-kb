/* ============================================================================
 * admin-pages/prefs.js —— 「偏好设置」页（插件行为开关；原 Zotero 设置窗格）
 *
 * 2026-10-17：Zotero「设置 → 文献知识库」那个偏好窗格**已整体下线**，设置只留
 * 这一处。哪些东西在别处、为什么不搬过来：
 *   · **模型配置**（provider / model / base_url / api_key）在「运行环境」页 ——
 *     它的唯一权威来源是 `<知识库>\llm-config.json`（插件与引擎共读）。
 *   · **八个路径**也在「运行环境」页（保存由宿主半直接写 kb-location.json）。
 *   · 本页只放**插件行为开关**：它们仍然是 Zotero pref（在 Zotero 进程内直接
 *     生效、没有第二份副本），页面通过宿主路由 `GET|POST /plugin-prefs` 读写。
 *
 * ⚠ 原设置页里的「聊天 / 上下文 / 转换参数」在本版本**不存在**（内容窗格聊天
 *   2026-10-05 已下线），不是漏搬；别把那些控件凭空加回来。
 *
 * 页面约定照抄 admin.js 顶部三条与 env.js 的写法：只用 admin.css 的类、
 * 数据一律 KBAdmin.get/post、一页一文件、注释说清为什么。
 * ==========================================================================*/
(function () {
  "use strict";

  // 兜底层级清单（真实清单由宿主从 KB_LEVELS 给；这里只防 /plugin-prefs 旧版/失败）
  const LEVELS = [
    { value: "outline", text: "分节纲要" },
    { value: "tldr", text: "摘要与要点" },
    { value: "card", text: "完整档案" },
    { value: "fulltext", text: "按页正文" },
    { value: "figures", text: "图注与表格" },
    { value: "weight", text: "权重与经验" },
  ];

  const SYNC_MODES = [
    { value: "none", text: "不同步" },
    { value: "folder", text: "同步到指定目录（坚果云 / OneDrive）" },
    { value: "command", text: "用命令同步（rclone / robocopy）" },
  ];

  function msgOf(e) {
    return (e && e.message) ? e.message : String(e);
  }

  KBAdmin.page({
    id: "prefs",
    title: "偏好设置",
    order: 45,

    async render(host, ctx) {
      const el = ctx.el;
      const state = { values: {}, version: "", zoteroVersion: "", loc: null };
      const refs = {};                    // 名字 → 控件（载入后回填）
      const flash = el("div", { class: "faint", text: "" });
      const setFlash = (text, cls) => {
        flash.className = cls || "faint";
        flash.textContent = text || "";
      };
      host.appendChild(flash);          // 放在最上面：改完任何一项都看得见

      // ================================================================ 动作

      /** 写一项（宿主路由 → Zotero.Prefs.set）。成功后就地更新 state。 */
      async function save(name, val) {
        try {
          await KBAdmin.post("/plugin-prefs", { [name]: val });
          state.values[name] = val;
          setFlash("✓ 已保存", "note note-ok");
          if (name === "kbviewLevel") {
            setFlash("✓ 已保存；下次打开 / 切换侧栏时生效", "note note-ok");
          }
        } catch (e) {
          setFlash("✗ 保存失败：" + msgOf(e), "note note-err");
        }
      }

      function checkbox(name, label, hint) {
        const cb = el("input", { type: "checkbox" });
        cb.addEventListener("change", function () { save(name, cb.checked); });
        refs[name] = cb;
        return el("label", { class: "pref-line" }, [
          cb,
          el("span", { text: label }),
          hint ? el("span", { class: "faint", text: "　" + hint }) : null,
        ]);
      }

      function textInput(name, placeholder) {
        const inp = el("input", { type: "text", class: "prefs-text" });
        inp.placeholder = placeholder || "";
        inp.addEventListener("change", function () { save(name, inp.value.trim()); });
        refs[name] = inp;
        return inp;
      }

      // ================================================================ 卡片 1：知识库行为
      const catIn = textInput("categories", "留空 = 用知识库里现有的分类");
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "知识库行为" }),
          el("span", { class: "faint", text: "这些开关在 Zotero 里立即生效" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "pref-list" }, [
          checkbox("autoProcess", "新文献自动处理",
                   "（自动切片 + 弹出分类建议）"),
          checkbox("acquireAutoClassify", "从 DSH 导入的文献自动分类 / 打标签",
                   "（落库后走一遍模型建议；默认开）"),
          checkbox("acquireConfirm", "导入前先在 Zotero 弹确认框",
                   "（默认关：搜索、候选、挑选都在 DSH 对话里做）"),
          checkbox("createMissing", "分类不存在时自动新建",
                   "（只建顶层分类）"),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [
          el("span", { text: "分类列表" }),
          catIn,
        ]),
        el("div", { class: "faint",
          text: "逗号分隔，例如：分类 B、分类 A、分类器。留空 = 用知识库里现有的分类。" }),
      ]));

      // ================================================================ 卡片 2：侧栏「知识库」
      const levelSel = ctx.select("显示层级", LEVELS, "outline",
                                  function (v) { save("kbviewLevel", v); });
      const fontIn = el("input", { type: "number", min: "10", max: "24",
                                   class: "prefs-num" });
      fontIn.addEventListener("change", function () {
        const n = parseInt(fontIn.value, 10);
        if (isFinite(n)) save("kbviewFont", n);
      });
      refs.kbviewFont = fontIn;
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "侧栏「知识库」" }),
          el("span", { class: "faint", text: "只管我们那一列，不动 Zotero 自己的设置" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [levelSel]),
        el("div", { class: "faint",
          text: "选的那一级如果这篇没有，会依次退到纲要 → 摘要与要点 → 完整档案。" }),
        el("div", { class: "row" }, [
          el("span", { text: "正文字号（px）" }),
          fontIn,
          el("span", { class: "faint", text: "10–24；默认 13" }),
        ]),
      ]));

      // ================================================================ 卡片 3：知识库同步
      const syncEnabled = refs.syncEnabled = el("input", { type: "checkbox" });
      const syncModeSel = ctx.select("方式", SYNC_MODES, "none", function (v) {
        save("syncMode", v);
        syncVisibility(v);
      });
      const syncTarget = textInput("syncTarget",
        "坚果云 / OneDrive 的本地文件夹，或 rclone、robocopy 命令");
      const syncWrap = el("div", { class: "row" }, [
        el("span", { text: "同步目标 / 命令" }),
        syncTarget,
      ]);
      const syncBtn = el("button", {
        class: "btn btn-subtle", text: "立即同步一次",
        title: "把知识库复制到目标目录，或执行同步命令（可能要一会儿）",
        on: { click: function () { syncNow(); } },
      });
      const syncStatus = el("div", { class: "faint", text: "" });
      syncEnabled.addEventListener("change", function () {
        save("syncEnabled", syncEnabled.checked);
      });
      function syncVisibility(mode) {
        const m = mode || syncModeSel.value || "none";
        syncWrap.classList.toggle("hidden", m === "none");
      }
      async function syncNow() {
        const mode = String(syncModeSel.value || "none");
        const target = String(syncTarget.value || "").trim();
        if (mode === "none" || !target) {
          syncStatus.className = "note note-warn";
          syncStatus.textContent = "先选一种同步方式并填目标。";
          return;
        }
        syncBtn.disabled = true;
        syncStatus.className = "faint";
        syncStatus.textContent = "正在同步…（大库可能要几分钟）";
        try {
          const r = await KBAdmin.post("/kb-sync", { mode: mode, target: target },
                                       null, 300000);
          syncStatus.className = "note note-ok";
          syncStatus.textContent = "✓ " + String((r && (r.message || r.output))
            || "同步完成");
        } catch (e) {
          syncStatus.className = "note note-err";
          syncStatus.textContent = "✗ 同步失败：" + msgOf(e);
        } finally {
          syncBtn.disabled = false;
        }
      }
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "知识库同步" }),
          el("span", { class: "faint", text: "可选；默认关闭" }),
        ]),
        el("div", { class: "sep" }),
        el("label", { class: "pref-line" }, [
          syncEnabled,
          el("span", { text: "启用同步" }),
        ]),
        el("div", { class: "row" }, [syncModeSel]),
        syncWrap,
        el("div", { class: "row" }, [syncBtn]),
        syncStatus,
        el("div", { class: "faint",
          text: "Zotero 自带的同步不能用来同步知识库（它只覆盖数据库里有附件条目的"
              + "文件）。这里走「复制到目录」或「执行命令」两条路。" }),
      ]));

      // ================================================================ 卡片 4：知识库位置
      const locLine = el("div", { class: "faint", text: "正在读知识库位置…" });
      const locStatus = el("div", { class: "faint", text: "" });
      const openBtn = el("button", {
        class: "btn btn-subtle", text: "打开这个目录",
        on: { click: function () { openKbDir(); } },
      });
      const migrateBtn = el("button", {
        class: "btn btn-subtle", text: "迁移到 Zotero 数据目录",
        on: { click: function () { askMigrate(); } },
      });
      async function loadLoc() {
        try {
          state.loc = await KBAdmin.get("/kb-location");
          const p = String((state.loc && state.loc.kb_dir) || "");
          locLine.className = p ? "mono prefs-path" : "faint";
          locLine.textContent = p || "（还没读到：引擎没起来？）";
          locLine.title = p;
          migrateBtn.disabled = !(state.loc && state.loc.default_follow);
        } catch (e) {
          locLine.className = "faint";
          locLine.textContent = "（读不到知识库位置：" + msgOf(e) + "）";
        }
      }
      async function openKbDir() {
        const p = String((state.loc && state.loc.kb_dir) || "");
        if (!p) { locStatus.textContent = "还没有可打开的位置。"; return; }
        try {
          await KBAdmin.post("/open-path", { path: p });
        } catch (e) {
          locStatus.className = "note note-err";
          locStatus.textContent = "✗ 打开失败：" + msgOf(e);
        }
      }
      function askMigrate() {
        const target = String((state.loc && state.loc.default_follow) || "");
        const cur = String((state.loc && state.loc.kb_dir) || "");
        if (!target) return;
        KBAdmin.dialog("迁移知识库", el("div", null, [
          el("div", { text: "把知识库迁移到 Zotero 数据目录：" }),
          el("div", { class: "mono prefs-path", text: target }),
          el("div", { class: "faint", text: "当前位置：" + (cur || "（未知）") }),
          el("div", { class: "sep" }),
          el("div", { class: "faint",
            text: "是复制（旧目录保留），会先自动备份；迁移完成后要重启 Zotero 才生效。" }),
        ]), [
          { text: "取消" },
          { text: "开始迁移", primary: true, run: function () { doMigrate(target); } },
        ]);
      }
      async function doMigrate(target) {
        locStatus.className = "faint";
        locStatus.textContent = "迁移中…（可能要几十秒）";
        try {
          const r = await KBAdmin.post("/kb-location", { migrate_to: target },
                                       null, 600000);
          locStatus.className = "note note-ok";
          locStatus.textContent = "✓ 已迁移到 " + String((r && r.kb_dir) || target)
            + "（重启 Zotero 后生效）";
          await loadLoc();
        } catch (e) {
          locStatus.className = "note note-err";
          locStatus.textContent = "✗ 迁移失败：" + msgOf(e);
        }
      }
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "知识库位置" }),
          el("span", { class: "faint", text: "数据在哪（改路径请去「运行环境」页）" }),
        ]),
        el("div", { class: "sep" }),
        locLine,
        el("div", { class: "row" }, [openBtn, migrateBtn]),
        locStatus,
        el("div", { class: "faint",
          text: "默认放在 Zotero 数据目录下的 zotero-kb\\，重装 / 换机只要同步 Zotero 那一份。" }),
      ]));

      // ================================================================ 卡片 5：可选组件引导
      const guideStatus = el("div", { class: "faint", text: "" });
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "可选组件安装引导" }),
          el("span", { class: "faint", text: "MinerU / Ollama 的首次提示" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [
          el("button", {
            class: "btn btn-subtle", text: "下次启动重新检查",
            title: "把「已经问过」的标记清掉；下次启动时如果组件仍缺，会再提示一次",
            on: { click: function () { resetGuides(); } },
          }),
        ]),
        guideStatus,
        el("div", { class: "faint",
          text: "只影响「要不要再提示」；已经装好的组件不会重复安装。" }),
      ]));
      async function resetGuides() {
        try {
          await KBAdmin.post("/plugin-prefs", { resetGuides: true });
          guideStatus.className = "note note-ok";
          guideStatus.textContent = "✓ 已重置，下次启动会重新检查。";
        } catch (e) {
          guideStatus.className = "note note-err";
          guideStatus.textContent = "✗ 重置失败：" + msgOf(e);
        }
      }

      // ================================================================ 卡片 6：高级（排查用）
      const mailIn = textInput("mailboxDir", "留空 = <知识库目录>\\mailbox");
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "高级（排查用）" }),
          el("span", { class: "faint", text: "一般不用改；改错会影响任务通道" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "pref-list" }, [
          checkbox("autoTaskPoll", "允许插件领取「在 Zotero 里执行 JS」的任务",
                   "（自动调试用；默认开）"),
          checkbox("taskPollHttp", "还去消费老的 HTTP 任务队列",
                   "（8765 已退役，默认关；只在手动起服务时用）"),
          checkbox("autoStartService", "Zotero 启动时自动拉起 8765",
                   "（回退能力，默认关）"),
        ]),
        el("div", { class: "row" }, [
          el("span", { text: "任务信箱目录" }),
          mailIn,
        ]),
      ]));

      // ================================================================ 底部：保存提示 / 版本
      host.appendChild(el("div", { class: "faint" }, [
        el("span", { text: "插件版本：" }),
        el("span", { class: "mono", text: "…" }),
        el("span", { text: "　·　Zotero " }),
        el("span", { class: "mono", text: "…" }),
      ]));
      const verBox = host.lastChild;
      const verEl = verBox.children[1];
      const zoteroEl = verBox.children[3];

      // ================================================================ 载入
      function applyValues() {
        const v = state.values || {};
        ["autoProcess", "acquireAutoClassify", "acquireConfirm", "createMissing",
         "syncEnabled", "autoTaskPoll", "taskPollHttp", "autoStartService"]
          .forEach(function (name) {
            if (refs[name]) refs[name].checked = v[name] === true;
          });
        refs.categories.value = v.categories == null ? "" : String(v.categories);
        refs.syncTarget.value = v.syncTarget == null ? "" : String(v.syncTarget);
        refs.mailboxDir.value = v.mailboxDir == null ? "" : String(v.mailboxDir);
        refs.kbviewFont.value = v.kbviewFont == null ? "13" : String(v.kbviewFont);
        levelSel.value = v.kbviewLevel || "outline";
        syncModeSel.value = v.syncMode || "none";
        syncVisibility(syncModeSel.value);
        verEl.textContent = state.version || "（读不到）";
        zoteroEl.textContent = state.zoteroVersion || "（读不到）";
      }

      try {
        const r = await KBAdmin.get("/plugin-prefs");
        state.values = (r && r.values) || {};
        state.version = String((r && r.version) || "");
        state.zoteroVersion = String((r && r.zoteroVersion) || "");
        // 层级清单以宿主为准（KB_LEVELS）；这里只是兜底，别当第二份清单维护
        if (r && Array.isArray(r.levels) && r.levels.length) {
          levelSel.setOptions(r.levels);
        }
        applyValues();
      } catch (e) {
        setFlash("✗ 读设置失败：" + msgOf(e), "note note-err");
      }
      loadLoc();
      ctx.log("偏好设置页：已载入");
    },
  });
})();
