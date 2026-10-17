/* ============================================================================
 * admin-pages/env.js —— 「运行环境」页（八个位置唯一设置面 + 引擎在不在 + 模型怎么配）
 *
 * 对应**已退役（2026-10-14）**的 Tk 面板 tools/panels/tab_env.py。那一页最花力气的是两件事：
 *   ①「项目目录 / Python / Ollama / MinerU / …」这些位置**值从哪来、谁覆盖了谁**；
 *   ②「模型接入」表单（provider / model / base_url / api_key）。
 * 这里照它的口径重画，取数改成问宿主半 → 引擎（字段见开发说明）：
 *   GET  /env-check    八个位置 + 来源 + 被否掉的高优先档 + **纯自动探测结果**
 *   GET  /health       引擎与各位置（顶部那句话与「本机引擎」块复用）
 *   GET  /models       Ollama 本机模型列表
 *   GET  /llm-config   当前模型配置（api_key 只回 has_key / key_tail）
 *   POST /llm-config   保存模型配置（api_key 留空 = 不改）
 *   POST /env-save     **保存运行环境**（宿主半直接写 kb-location.json，不走引擎）
 *   POST /env-pick     **浏览…**（宿主半 nsIFilePicker）
 *   POST /service/restart
 *
 * ⚠ 2026-10-16（用户要求）这一页从"只读展示"变成**唯一设置面**：
 *   Zotero 插件设置页那三行路径输入框已删、`.env` 降级成"救急层"。所以这里
 *   每一项都能改、能浏览、能「恢复自动」，保存**不经过 Python 引擎**——
 *   理由见 kb-location.json 的致命场景：引擎起不来时恰恰最需要能改这些路径。
 *   保存语义（值与自动探测结果相同 → 视为未覆盖）见 collectEnvBody() 的注释。
 *
 * 页面约定照抄 admin.js 顶部三条与 structure.js 的写法：只用 admin.css 的类、
 * 数据一律 KBAdmin.get/post、一页一文件、注释说清为什么。
 * ==========================================================================*/
(function () {
  "use strict";

  // 八个位置在引擎里的键 → 中文名。契约里 items 已带 name，这里只是**兜底**
  // （口径与 offline/schemas.py::ENV_SOURCE_LABELS 同一份）。顺序 = 引擎给的顺序。
  const ENV_LABELS = {
    project_root: "项目目录",
    python: "Python 解释器",
    ollama: "Ollama（可选）",
    mineru: "MinerU（可选）",
    mineru_home: "MinerU HOME",
    kb_dir: "知识库位置",
    zotero_data_dir: "Zotero 数据目录",
    mathtype: "MathType（可选）",
  };

  // 可选组件：找不到也不影响别的功能，状态列要区别对待（别吓唬用户）。
  const OPTIONAL_KEYS = { ollama: 1, mineru: 1, mineru_home: 1, mathtype: 1 };

  // 「自动探测 / 默认」是**机器事实**（灰字）；其余（面板设置 / 环境变量 /
  // 救急 .env / 调用方指定）都是**用户明确设过的**（黑字）。
  const AUTO_SOURCES = { "自动探测": 1, "默认": 1 };
  function isUserSet(source) {
    const s = String(source == null ? "" : source).trim();
    return !!s && !AUTO_SOURCES[s];
  }

  // 每项的"浏览…"按钮文案（跟着 kind 走）
  function pickTitle(it) {
    const name = ENV_LABELS[it.key] || it.key;
    return it.kind === "dir" ? ("选择" + name + "（目录）")
                             : ("选择" + name + "（程序）");
  }

  function msgOf(e) {
    return (e && e.message) ? e.message : String(e);
  }

  KBAdmin.page({
    id: "env",
    title: "运行环境",
    order: 80,

    async render(host, ctx) {
      const el = ctx.el;

      // ------------------------------------------------------ 卡片 1：八个位置（可编辑）
      //
      // 设计（用户 2026-10-16 两轮要求）：
      //   · **唯一设置面就是这里**：Zotero 插件设置页那三个输入框已删，`.env` 降级
      //     成"救急层"。所以每一项都必须能改、能浏览、能恢复自动。
      //   · **输入框永远显示"当前生效的值"**：没手填时填的就是**自动探测结果**
      //     （灰字，提示"这是自动探测到的"）；手填过/环境变量设过 → 黑字。
      //   · 旁边标出来源（自动探测 / 面板设置 / 环境变量 / 救急 .env）。
      //   · ⚠ **保存语义（关键）**：框里的值已经预填成自动探测结果，如果用户点
      //     「保存」却没动过这一项，**不能**把它存成手填 —— 否则自动探测的值被
      //     冻死，换机器/换路径就不跟着变了。判据：**值与自动探测结果相同 →
      //     视为未覆盖（清掉手填项）**；不同 → 存成手填。见 collectEnvBody()。
      const posHost = el("div", null,
                         [el("div", { class: "note note-warn", text: "正在检测八个位置…" })]);
      const srcNote = el("div", { class: "faint", text: "" });
      const envFlash = el("div", { class: "faint", text: "" });
      const redetectBtn = el("button", {
        class: "btn btn-subtle", text: "重新检测",
        on: { click: function () { refreshAll(); } },
      });
      const saveEnvBtn = el("button", {
        class: "btn btn-primary", text: "保存并重新检测",
        title: "把改动写进 kb-location.json（宿主半直接写，不依赖引擎）；"
             + "没动过的那几项不会被存成手填",
        on: { click: function () { saveEnv(); } },
      });
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "运行环境（八个位置）" }),
          el("span", { class: "faint", text: "值从哪来、在不在；就地可改（灰字 = 自动探测，黑字 = 你设过）" }),
          el("span", { class: "grow" }),
          redetectBtn,
          saveEnvBtn,
        ]),
        el("div", { class: "sep" }),
        posHost,
        el("div", { class: "sep" }),
        srcNote,
        envFlash,
        el("div", { class: "faint",
          text: "「保存并重新检测」把改动写进项目的 kb-location.json —— 由 Zotero 插件"
              + "（宿主半）直接写，不经过 Python 引擎，所以引擎因为找不到 Python "
              + "而起不来时你仍然改得回来。「浏览…」同理（走宿主半的系统文件选择框）。" }),
      ]));

      // ------------------------------------------------------ 卡片 2：本机引擎
      const svcHost = el("div", null,
                         [el("div", { class: "note note-warn", text: "正在读引擎状态…" })]);
      const restartNote = el("div", { class: "faint",
        text: "引擎由宿主半按需拉起：正常情况下不需要手动重启；"
            + "「重启引擎」会停掉当前进程，下一次请求自动拉起新的。"
            + "有任务在跑时不会执行。" });
      const restartBtn = el("button", {
        class: "btn btn-subtle", text: "重启引擎",
        on: { click: onRestart },
      });
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "本机引擎" }),
          el("span", { class: "faint", text: "随 Zotero 起落，不常驻、不自启" }),
          el("span", { class: "grow" }),
          restartBtn,
        ]),
        el("div", { class: "sep" }),
        svcHost,
        el("div", { class: "sep" }),
        restartNote,
      ]));

      // ------------------------------------------------------ 卡片 3：模型配置
      //
      // 服务商 / 模型：**自绘下拉**（`ctx.select`），不是原生 <select> ——
      // Zotero 的 <browser> 里原生 select 的弹层出不来（见 admin.js 顶部那段）。
      //
      // ⚠ 服务商**落盘只有两种**（读 offline/judge.py 确认，别自己发明第三种）：
      //   · "ollama" —— 本机跑，走它的 /api/generate；模型留空 = 引擎自动挑；
      //   · "openai" —— **任何** OpenAI 兼容的 /chat/completions（DeepSeek / 通义千问
      //     / Moonshot / 智谱 / Groq / OpenRouter / 硅基流动 / vLLM / LM Studio…），
      //     差别只在 base_url 与模型 id；judge.py 的 KNOWN_BASE_URLS 就是这张表。
      //     写了别的（例如 "deepseek"）会被 `generate()` **当成 ollama** —— 所以
      //     引擎侧 llm.save 加了白名单，页面也不给它入口。
      // 页面上给**三个稳定分组标签**（用户 2026-10-13 的要求：分组别写模型名）：
      //   `ollama` = Ollama（本地） / `openai` = 外部 API（OpenAI 兼容） /
      //   `custom` = 自定义（自建或其它兼容服务）—— `custom` 落盘同样是
      //   provider="openai"，只是 base_url 不是已知服务商地址（见 uiProviderOf）。
      const state = { ui: "ollama", provider: "ollama", model: "", hasKey: false,
                      keyTail: "", services: [], discovered: null, cacheAt: "",
                      filterBare: true, hiddenBare: 0 };

      /** 落盘的 provider（页面的 custom 也要写 openai —— 引擎只认这两类）。 */
      function persistedProvider() {
        return uiProviderSel.value === "ollama" ? "ollama" : "openai";
      }

      /** 已知服务商地址（引擎 llm.config 给的，唯一事实来源在 judge.KNOWN_BASE_URLS）。 */
      function isKnownService(url) {
        const u = String(url || "").replace(/\/+$/, "").toLowerCase();
        if (!u) return true;                      // 留空 = 让引擎按模型名猜，算"外部 API"
        return (state.services || []).some(function (s) {
          return String(s.base_url || "").replace(/\/+$/, "").toLowerCase() === u;
        });
      }

      /** 从落盘的 provider + base_url 反推页面上的分组（先按 id 恢复，不按显示名）。 */
      function uiProviderOf(provider, baseUrl) {
        if (String(provider || "ollama") === "ollama") return "ollama";
        return isKnownService(baseUrl) ? "openai" : "custom";
      }

      const uiProviderSel = ctx.select("服务商", [
        { value: "ollama", text: "Ollama（本地）",
          title: "本机 Ollama（http://127.0.0.1:11434）—— 免费、离线；模型要先 ollama pull" },
        { value: "openai", text: "外部 API（OpenAI 兼容）",
          title: "任何 OpenAI 兼容的 /chat/completions：DeepSeek、通义千问、Moonshot、"
               + "智谱、Groq、OpenRouter、硅基流动…（地址可用已知服务商，也可留空让"
               + "引擎按模型名猜）" },
        { value: "custom", text: "自定义（自建 / 其它兼容服务）",
          title: "vLLM、LM Studio、公司内网网关这类：要自己填 base_url" },
      ], "ollama", function () {
        // 用户手动切服务商：清掉上一家的模型名（免得把 A 家的 id 带到 B 家），
        // 并**重新探测一次**（否则下拉里还挂着上一家的候选 —— 真机踩过）。
        syncProviderUI(true);
        refreshModels(false);
      });
      // 模型：**可输入的下拉**（combobox）—— 候选来自三层探测（见 refreshModels），
      // 列表外的值必须留住（admin.js::select 里"可输入模式不跟着列表回落"那段）。
      const modelSel = ctx.select("模型", [{ value: "", text: "（自动挑选）" }], "",
                                  function (v) { state.model = String(v || ""); },
                                  { editable: true,
                                    placeholder: "留空 = 自动挑，或从候选里挑 / 直接输入",
                                    title: "可以直接输入服务商的模型 id" });
      const refreshModelsBtn = el("button", {
        class: "btn btn-subtle", text: "刷新模型列表",
        title: "重新探测一次（本地走 /api/tags，外部走 {base_url}/models）；"
             + "探测失败会回落内置候选",
        on: { click: function () { refreshModels(true); } },
      });
      const baseUrlIn = el("input", {
        type: "text", size: "32",
        placeholder: "留空 = 按模型名自动猜服务商地址（引擎里那张表）",
      });
      const keyIn = el("input", {
        type: "password", size: "18", placeholder: "留空 = 不改",
      });
      const keyHint = el("span", { class: "faint", text: "" });
      const saveBtn = el("button", {
        class: "btn btn-primary", text: "保存",
        on: { click: onSave },
      });
      const testBtn = el("button", {
        class: "btn", text: "测试连接",
        title: "真发一次最小请求：本地 = 查 Ollama 的模型列表；外部 API = 调一次 "
             + "/chat/completions（模型/地址/Key 有一个不对就会报出来）",
        on: { click: function () { tryConnect(true); } },
      });
      const modelStatus = el("div", { class: "faint", text: "" });
      // 「只列带上下文后缀的模型」（原设置页的更好的模型选择逻辑）——
      // 没有后缀的模型看不出上下文窗口，容易挑错。这是**显示偏好**，不是
      // 模型配置本身（配置仍只存 llm-config.json 一份）。
      const filterCb = el("input", { type: "checkbox" });
      const filterRow = el("label", { class: "pref-line" }, [
        filterCb,
        el("span", { text: "只列带上下文后缀的模型" }),
        el("span", { class: "faint",
                     text: "（-4k / -16k / -24k…；没有后缀的看不出窗口大小）" }),
      ]);
      filterCb.addEventListener("change", function () {
        state.filterBare = filterCb.checked;
        paintModels();
        KBAdmin.post("/plugin-prefs", { filterBareModels: filterCb.checked })
          .catch(function (e) { ctx.log("[模型] 记住过滤偏好失败：" + msgOf(e), "warn"); });
      });
      // 模型一栏的"提示句"（本地/外部两套口径 + 探测结论，见 syncProviderUI/refreshModels）
      const modelHint = el("div", { class: "faint", text: "" });
      const modelNote = el("div", { class: "faint",
        text: "保存到知识库目录下的 llm-config.json —— 引擎与面板的所有模型功能"
            + "（分类建议 / 元数据兜底 / 提示词试跑 / 经验草稿）都用这一份，"
            + "改完立即生效，下一次请求就用新配置。Zotero 插件右键菜单也读这一份。" });

      // 外部 API / 自定义才要的那一行（本地 Ollama 用不到 base_url / api_key）
      const credRow = el("div", { class: "row" }, [
        el("label", { class: "inline", text: "API 地址" }),
        baseUrlIn,
        el("label", { class: "inline", text: "API Key" }),
        keyIn,
        keyHint,
      ]);

      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "模型配置" }),
          el("span", { class: "faint",
                       text: "本地 Ollama，或任何 OpenAI 兼容 API（只是地址与模型 id 不同）" }),
        ]),
        el("div", { class: "sep" }),
        el("div", { class: "row" }, [
          uiProviderSel,
          modelSel,
          refreshModelsBtn,
        ]),
        filterRow,
        modelHint,
        el("div", { class: "sep" }),
        credRow,
        el("div", { class: "row" }, [
          saveBtn, testBtn,
        ]),
        el("div", { class: "sep" }),
        modelStatus,
        modelNote,
      ]));

      // ========================================================== 取数与画图

      /**
       * 每行的编辑状态（重检测时重建）。存"打开时显示的值"是为了实现用户
       * 2026-10-16 的保存语义：**值与自动探测结果相同 → 视为未覆盖**。
       */
      const envEdit = { items: [], inputs: {}, initial: {}, auto: {}, sources: {} };

      /** 状态标记：✓ 存在 / ✗ 找不到；可选组件与"允许暂缺"另给一句不吓人的说明。 */
      function statusCell(r) {
        const ok = r.ok === true;
        const optional = r.optional === true || OPTIONAL_KEYS[r.key] === 1;
        const maybe = r.key === "kb_dir";     // 知识库可能还没建
        const s = el("span", {
          class: (ok ? "tag-ok" : "tag-bad") + " env-st",
          text: ok ? "✓ 存在" : "✗ 找不到",
        });
        s.title = ok
          ? "这个路径存在"
          : (maybe ? "这一项允许指向还不存在的目录（第一次建库会创建它）"
             : optional ? "可选组件：不装 / 找不到都不影响其他功能"
                        : "找不到这个路径，用到它的功能会报错");
        return s;
      }

      /** 来源标签：灰字=机器事实（自动探测/默认），黑字=用户设过的。 */
      function sourceCell(r) {
        const raw = String(r.source == null ? "" : r.source).trim();
        const span = el("span", { class: "faint env-src",
                                  text: raw ? ("来源：" + raw) : "来源：未知" });
        span.title = raw === "自动探测"
          ? "这一项没有手填，值是自动探测出来的（灰字）"
          : (isUserSet(raw) ? "这一项是你明确设过的（黑字）：" + raw
                            : "引擎没报来源（可能是旧版本引擎）");
        if (isUserSet(raw)) span.style.color = "var(--text)";
        return span;
      }

      /**
       * 中段省略：保住头尾，长路径不再把行撑开（用户 2026-10-17 报"非常乱"）。
       * 纯 JS + CSS，不引依赖；完整值一律进 title，鼠标悬停能看到。
       */
      function midPath(raw) {
        const t = String(raw == null ? "" : raw);
        const head = 20, tail = 30;
        if (t.length <= head + tail + 1) return t;
        // 尽量在路径分隔符处切，别把一段目录名劈成两半（"proje…"）；
        // 但总长度仍受控，避免 CSS 再切掉尾巴。
        let h = head;
        const nextSep = t.indexOf("\\", head);
        if (nextSep > 0 && nextSep - head <= 10) h = nextSep + 1;
        let tailStart = t.length - tail;
        const prevSep = t.lastIndexOf("\\", tailStart);
        if (prevSep > 0 && tailStart - prevSep <= 10) tailStart = prevSep + 1;
        if (tailStart <= h) tailStart = h + 1;
        return t.slice(0, h) + "…" + t.slice(tailStart);
      }

      /** 输入框：值 = 当前生效值；灰字表示"这是自动探测到的"。 */
      function envInput(r) {
        const inp = el("input", { type: "text", class: "env-input" });
        inp.value = String(r.path == null ? "" : r.path);
        const auto = String(r.auto == null ? "" : r.auto);
        inp.placeholder = auto ? ("自动探测：" + auto)
                               : "（没有自动探测结果 —— 可手填，或点「浏览…」）";
        const gray = !isUserSet(r.source);
        inp.style.color = gray ? "var(--faint)" : "var(--text)";
        // 用户一动手就变黑字（表示"这一项现在是手填的"）
        inp.addEventListener("input", function () {
          inp.style.color = "var(--text)";
        });
        const full = inp.value || auto || "";
        inp.title = full;
        envEdit.inputs[r.key] = inp;
        envEdit.initial[r.key] = inp.value;
        envEdit.auto[r.key] = auto;
        envEdit.sources[r.key] = String(r.source == null ? "" : r.source).trim();
        return inp;
      }

      /**
       * 路径格：默认显示**中段省略**的等宽文本（整齐、不撑行），点一下才换成
       * 可编辑的输入框（输入框永远存在，只是隐藏 —— 保存逻辑不变）。
       */
      function pathCell(r) {
        const inp = envInput(r);
        const full = inp.value || String(r.auto == null ? "" : r.auto) || "";
        const disp = el("span", {
          class: "env-path",
          text: full ? midPath(full) : "（未设置）",
        });
        disp.title = full || "没有值（灰字是自动探测结果）";
        if (!isUserSet(r.source)) disp.style.color = "var(--faint)";
        inp.classList.add("hidden");
        const paintDisp = function () {
          const v = String(inp.value || "").trim();
          disp.textContent = v ? midPath(v) : "（未设置）";
          disp.title = v || "没有值";
        };
        disp.addEventListener("click", function () {
          disp.classList.add("hidden");
          inp.classList.remove("hidden");
          inp.focus();
          try { inp.select(); } catch (e) { /* ignore */ }
        });
        inp.addEventListener("input", paintDisp);
        inp.addEventListener("blur", function () {
          paintDisp();
          inp.classList.add("hidden");
          disp.classList.remove("hidden");
        });
        inp.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter") inp.blur();
          if (ev.key === "Escape") { disp.classList.remove("hidden"); inp.classList.add("hidden"); }
        });
        // 「浏览…」选完之后要立刻更新上面那行短路径
        inp._kbPathDisp = disp;
        return el("div", { class: "env-path-cell" }, [disp, inp]);
      }

      /** 一行：名字 · 路径 · 来源 · 状态 · 浏览… ·（必要时）恢复自动 */
      function envRow(r) {
        const name = el("span", { class: "env-name",
                                  text: String(r.name || ENV_LABELS[r.key] || r.key) });
        const row = el("div", { class: "env-row" }, [
          name, pathCell(r), sourceCell(r), statusCell(r),
          el("button", {
            class: "btn", text: "浏览…", title: pickTitle(r),
            on: { click: function () { browseEnv(r); } },
          }),
        ]);
        // 「恢复自动」只在**当前来源是面板设置**时有意义（其它档清不掉：
        // 环境变量在系统里、`.env` 在文件里、自动探测本来就没手填）。
        if (envEdit.sources[r.key] === "面板设置") {
          row.appendChild(el("button", {
            class: "btn btn-subtle", text: "恢复自动",
            title: "清掉这一项的手填值，回到自动探测",
            on: { click: function () { restoreAuto(r); } },
          }));
        }
        let note = null;
        if (r.note) note = el("div", { class: "env-note", text: String(r.note) });
        return el("div", { class: "env-item" }, [row, note]);
      }

      function renderPos(d) {
        const items = (d && Array.isArray(d.items)) ? d.items : [];
        envEdit.items = items;
        envEdit.inputs = {};
        envEdit.initial = {};
        envEdit.auto = {};
        envEdit.sources = {};
        posHost.textContent = "";
        if (!items.length) {
          posHost.appendChild(el("div", { class: "note note-warn",
            text: "引擎没有回任何一项（/env-check 回包是空的）。" }));
        } else {
          items.forEach(function (r) { posHost.appendChild(envRow(r)); });
        }

        // 「谁覆盖了谁 / 哪一档被否掉」——只在**真有问题**时才出现，正常时不占视线。
        const bits = [];
        const src = (d && d.sources) || {};
        Object.keys(src).forEach(function (k) {
          const one = src[k] || {};
          (Array.isArray(one.rejected) ? one.rejected : []).forEach(function (b) {
            bits.push((ENV_LABELS[k] || k) + "：" + (b.source || "高优先档")
                      + "里填的 " + (b.value || "（空）") + " 被忽略（"
                      + (b.why || "没写原因") + "）");
          });
          (Array.isArray(one.shadowed) ? one.shadowed : []).forEach(function (b) {
            bits.push((ENV_LABELS[k] || k) + "：" + (b.source || "另一档")
                      + "里也写了 " + (b.value || "（空）")
                      + "，但当前没生效（被更高优先档盖住）");
          });
        });
        const rej = (d && Array.isArray(d.rejected)) ? d.rejected : [];
        rej.forEach(function (b) {
          const line = (b.key ? (ENV_LABELS[b.key] || b.key) + "：" : "")
            + (b.source || "高优先档") + "里填的 " + (b.value || "（空）")
            + " 被忽略（" + (b.why || "没写原因") + "）";
          if (bits.indexOf(line) < 0) bits.push(line);
        });

        if (bits.length) {
          srcNote.className = "note note-warn";
          srcNote.textContent = "⚠ " + bits.join("；");
        } else {
          srcNote.className = "faint";
          srcNote.textContent = "优先级：调用方指定 > 环境变量 > 面板设置（这里保存的值）"
            + " > 自动探测 > 救急 .env > 内置默认。改动只在你点「保存并重新检测」时落盘。";
        }
      }

      // ------------------------------------------------------ 保存 / 浏览 / 恢复自动

      function setFlash(text, cls) {
        envFlash.className = cls || "faint";
        envFlash.textContent = text || "";
      }

      /**
       * 表单 → 请求体（宿主半 `POST /env-save`）。
       *
       * ⚠ **保存语义（用户 2026-10-16 明确要求，别改）**：
       *   输入框里预填的是"当前生效的值"，没手填时它就是自动探测结果。用户点
       *   保存但**没动过**那一项时，绝不能把它存成手填 —— 否则自动探测的值被冻
       *   死，以后换机器/换路径不跟着变。所以：
       *     · 值 == 自动探测结果 → 发**空串**（= 未覆盖，清掉手填项）；
       *     · 值 != 自动探测结果 → 发那个值（= 手填，存进 kb-location.json）；
       *     · 没动过**且**当前来源不是"面板设置" → **不发**这一项（没什么可清的）。
       */
      function collectEnvBody() {
        const body = {};
        let touched = 0;
        envEdit.items.forEach(function (r) {
          const key = r.key;
          const inp = envEdit.inputs[key];
          if (!inp) return;
          const val = String(inp.value == null ? "" : inp.value).trim();
          const init = String(envEdit.initial[key] == null ? "" : envEdit.initial[key]);
          const auto = String(envEdit.auto[key] == null ? "" : envEdit.auto[key]);
          const changed = (val !== init);
          const wasPanel = (envEdit.sources[key] === "面板设置");
          if (!changed && !wasPanel) return;      // 没动过、也没手填过 → 不写
          body[key] = (auto && val === auto) ? "" : val;
          touched++;
        });
        return { body: body, touched: touched };
      }

      async function saveEnv() {
        const got = collectEnvBody();
        if (!got.touched) {
          setFlash("没有需要保存的改动（灰字那几项是自动探测结果，不会被存成手填）。");
          return;
        }
        saveEnvBtn.disabled = true;
        setFlash("正在保存并重新检测…");
        // ⚠ 来源是"环境变量"的那几项：**就算写进 kb-location.json 也不会生效**
        //   （环境变量那一档优先级更高）。这种情况要明说，否则用户会以为白填了。
        const envVarKeys = Object.keys(got.body).filter(function (k) {
          return envEdit.sources[k] === "环境变量";
        });
        try {
          const r = await KBAdmin.post("/env-save", got.body);
          const cleared = (r && Array.isArray(r.cleared)) ? r.cleared.length : 0;
          let msg = "✓ 已保存（" + ((r && r.path) ? r.path : "kb-location.json")
            + (cleared ? ("，" + cleared + " 项恢复自动") : "") + "）";
          if (envVarKeys.length) {
            msg += "　⚠ " + envVarKeys.map(function (k) {
              return (ENV_LABELS[k] || k);
            }).join("、") + " 当前来自环境变量（优先级更高），"
              + "面板里保存的值要等那个环境变量去掉后才生效。";
          }
          setFlash(msg, envVarKeys.length ? "note note-warn" : "note note-ok");
          ctx.log("[运行环境] 已保存 " + got.touched + " 项，"
                  + cleared + " 项恢复自动");
          await refreshAll();
        } catch (e) {
          setFlash("✗ 保存失败：" + msgOf(e), "note note-err");
          ctx.log("[运行环境] 保存失败：" + msgOf(e), "warn");
        } finally {
          saveEnvBtn.disabled = false;
        }
      }

      /** 「浏览…」：走宿主半的 nsIFilePicker（页面里没有系统文件选择框）。 */
      async function browseEnv(r) {
        try {
          const res = await KBAdmin.post("/env-pick",
                                         { kind: r.kind || "file",
                                           title: pickTitle(r) });
          if (!res || !res.path) return;          // 用户取消
          const inp = envEdit.inputs[r.key];
          if (inp) {
            inp.value = res.path;
            inp.style.color = "var(--text)";
            inp.title = res.path;
            if (inp._kbPathDisp) {
              inp._kbPathDisp.textContent = midPath(res.path);
              inp._kbPathDisp.title = res.path;
              inp._kbPathDisp.style.color = "var(--text)";
              inp._kbPathDisp.classList.remove("hidden");
              inp.classList.add("hidden");
            }
          }
          setFlash("已选：" + res.path + " —— 点「保存并重新检测」生效。");
        } catch (e) {
          setFlash("✗ 打开选择框失败：" + msgOf(e), "note note-err");
        }
      }

      /**
       * 「恢复自动」：二次确认后清掉这一项的手填值（宿主半删键）。
       *
       * ⚠ 这是"危险/清零"动作，按项目约定必须先确认，并**把要清掉的值与
       *   接下来会用的自动探测值都摆出来**（用户才知道点了会发生什么）。
       */
      function restoreAuto(r) {
        const name = String(r.name || ENV_LABELS[r.key] || r.key);
        KBAdmin.dialog("恢复自动：" + name, el("div", null, [
          el("div", { text: "把「" + name + "」的手填值清掉，交回自动探测。当前手填的值：" }),
          el("div", { class: "mono", text: String(r.path || "（空）") }),
          el("div", { class: "sep" }),
          el("div", { class: "faint",
            text: "自动探测结果（会被采用）："
                + (r.auto ? String(r.auto) : "（这台机器上没探测到）")
                + "。这一项在 kb-location.json 里的键会被删掉。" }),
        ]), [
          { text: "取消" },
          { text: "恢复自动", primary: true, run: function () { restoreNow(r); } },
        ]);
      }

      async function restoreNow(r) {
        setFlash("正在恢复自动…");
        try {
          const body = {};
          body[r.key] = "";
          const res = await KBAdmin.post("/env-save", body);
          setFlash("✓ 已恢复自动（" + ((res && res.path) ? res.path : "") + "）",
                   "note note-ok");
          await refreshAll();
        } catch (e) {
          setFlash("✗ 恢复自动失败：" + msgOf(e), "note note-err");
        }
      }

      function renderHealth(h) {
        svcHost.textContent = "";
        const rows = [
          ["引擎", (h && h.service) ? String(h.service) : "Python 引擎"],
          ["状态", (h && h.ok === false) ? "离线" : "在线 ✓"],
          ["知识库", h && h.kb_dir],
          ["项目目录", h && h.project_root],
          ["Python", h && h.python],
          ["Ollama", (h && h.ollama) || "（没找到，可选）"],
          ["MinerU", (h && h.mineru) || "（没找到，可选）"],
        ];
        rows.forEach(function (kv) {
          const label = el("span", { class: "subtle", text: kv[0] + "：" });
          const val = String(kv[1] == null ? "" : kv[1]) || "—";
          const v = el("span", { class: "mono", text: val });
          v.title = val;
          svcHost.appendChild(el("div", { class: "row" }, [label, v]));
        });
      }

      // ========================================================== 模型配置：取值与联动
      //
      // 「模型目录」三层兜底（设计依据见开发说明，别在这里发明第四层）：
      //   ① 动态探测：本地 /api/tags、外部 {base_url}/models（引擎 `llm.discover` 做）；
      //   ② 探测失败 → **内置候选**（引擎照抄 DSH 的静态目录，页面要写明"也可手填"）；
      //   ③ 永远留手填入口（模型那一格本来就是可输入的 combobox）。
      // **保存只做语法校验**（非空 / URL 可解析 / id 是可打印 ASCII），
      // "模型名存不存在"一律交给"保存后异步试连"，绝不在保存时挡。

      /** 上下文长度/最大输出：能拿到就显示，拿不到就留空（**宁可空也别猜**）。 */
      function fmtTokens(n) {
        const v = Number(n);
        if (!v) return "";
        if (v >= 1000000 && v % 1000000 === 0) return (v / 1000000) + "M";
        if (v >= 1000) return Math.round(v / 1000) + "k";
        return String(v);
      }

      /** 探测结果的来源 → 人话（页面右上那句状态用）。 */
      function sourceLabel(src) {
        return src === "ollama" ? "本机 /api/tags"
             : src === "remote" ? "{base_url}/models"
             : src === "builtin" ? "内置候选（探测失败）"
             : src === "none" ? "没有候选" : "未探测";
      }

      /** 模型候选（把 state.discovered 画成下拉选项，原设置页那套过滤/排序）。 */
      function modelOptions() {
        const d = state.discovered || {};
        const opts = [];
        if (state.ui === "ollama") {
          opts.push({ value: "", text: "（自动挑选）",
                      title: "留空 = 交回引擎自动挑（优先 qwen3:4b-instruct）" });
        }
        let list = (d.models || []).filter(function (m) {
          return m && String(m.id || "").trim();
        });
        state.hiddenBare = 0;
        if (state.ui === "ollama" && state.filterBare) {
          // 没有 `-Nk` 后缀的模型看不出上下文窗口大小，默认不列；
          // 但**一个带后缀的都没有**时不能把列表清空（回落成全部，页面会说明）。
          const suffix = /-\d+k$/i;
          const suffixed = list.filter(function (m) {
            return suffix.test(String(m.id || ""));
          });
          if (suffixed.length) {
            state.hiddenBare = list.length - suffixed.length;
            list = suffixed;
          }
        }
        // 排序：推荐模型所属的"系"优先 → 同系上下文大的优先 → 其余按名字。
        const ctxOf = function (n) {
          const m = String(n).match(/-(\d+)k$/i);
          return m ? parseInt(m[1], 10) : -1;
        };
        const baseOf = function (n) { return String(n).replace(/-\d+k$/i, ""); };
        const recBase = baseOf(d.recommended || "");
        list = list.slice().sort(function (a, b) {
          const ra = (recBase && baseOf(a.id) === recBase) ? 0 : 1;
          const rb = (recBase && baseOf(b.id) === recBase) ? 0 : 1;
          if (ra !== rb) return ra - rb;
          const ca = ctxOf(a.id), cb = ctxOf(b.id);
          if (ca !== cb) return cb - ca;
          return String(a.id).localeCompare(String(b.id));
        });
        list.forEach(function (m) {
          const id = String(m.id);
          const bits = [];
          if (m.name && m.name !== id) bits.push(String(m.name));
          if (m.context) bits.push("上下文 " + fmtTokens(m.context));
          if (m.note) bits.push(String(m.note));
          opts.push({ value: id,
                      text: id + (bits.length ? "　·　" + bits.join(" · ") : ""),
                      title: id + (bits.length ? "\n" + bits.join("\n") : "") });
        });
        // ⚠ 当前配置里的模型**永远留住**（先按 id 恢复，再谈名字解析）：
        //   目录换了 / 探测失败时也不能把用户存着的 id 弄丢。
        if (state.model && !opts.some(function (o) { return o.value === state.model; })) {
          opts.push({ value: state.model, text: state.model + "（当前配置，候选里没有）",
                      title: "llm-config.json 里写着的模型 id" });
        }
        return opts;
      }

      /** 重画模型下拉 + 状态行 + 提示句（探测结果变了就调它）。 */
      function paintModels() {
        modelSel.setOptions(modelOptions());
        modelSel.value = state.model || "";
        const d = state.discovered || {};
        const n = (d.models || []).length;
        if (!state.discovered) {
          modelStatus.className = "faint";
          modelStatus.textContent = "还没探测模型目录 —— 点右边「刷新模型列表」试试。";
        } else if (d.ok) {
          modelStatus.className = "faint";
          modelStatus.textContent = "探测到 " + n + " 个模型（" + sourceLabel(d.source)
            + "，" + (d.at || "") + (d.cached ? "，缓存" : "") + "）"
            + (state.hiddenBare
               ? "　·　已隐藏 " + state.hiddenBare + " 个没有上下文后缀的"
               : "")
            + (d.base_url ? "　·　" + d.base_url : "");
        } else {
          modelStatus.className = "note note-warn";
          modelStatus.textContent = "探测失败：" + (d.error || "没说明原因")
            + "　—— " + (d.hint || "")
            + (n ? "（下列 " + n + " 个是内置候选，也可以直接手填）" : "");
        }
      }

      /**
       * 服务商/地址变了 → 联动四件事：字段可见性、模型候选、提示句、重新探测。
       * `resetModel` 只在**用户手动切服务商**时为真（免得把 A 家的模型 id 带到 B 家）；
       * 读配置时的联动不能清 —— 那是用户存着的值（先按 id 恢复的落点就在这里）。
       */
      function syncProviderUI(resetModel) {
        const ui = String(uiProviderSel.value || "ollama");
        state.ui = ui;
        state.provider = persistedProvider();
        if (resetModel) state.model = "";
        const isApi = (ui !== "ollama");
        // ⚠ 用 **.hidden 类**而不是 `hidden` 属性：`.row { display:flex }` 会盖过
        //   UA 样式表里的 `[hidden]`（真机踩过：属性设了、行照样显示）。admin.css
        //   里另有一条 `[hidden]{display:none!important}` 兜底，但类名这条不依赖它。
        credRow.classList.toggle("hidden", !isApi);
        filterRow.classList.toggle("hidden", ui !== "ollama");
        keyIn.placeholder = isApi ? "填服务商给的 Key（必填）" : "留空 = 不改";
        modelSel.input.placeholder = ui === "ollama"
          ? "留空 = 自动挑，或填本机模型名（ollama list）"
          : "从候选里挑，或直接输入服务商的模型 id";
        modelHint.textContent = ui === "ollama"
          ? ("本地 Ollama：候选来自本机 /api/tags（比 /v1/models 多给参数量与量化档）。"
             + "默认只列带上下文后缀的模型（可在下面取消勾选）；"
             + "留空 = 引擎自动挑。要用外部 API 就把上面的「服务商」改成"
             + "「外部 API（OpenAI 兼容）」。")
          : ("外部 API 要填「服务商的模型 id」，不是产品名。候选由两层给："
             + "先探测 {base_url}/models（地址留空时按模型名猜服务商），探不到就用"
             + "内置候选；都没有就照服务商文档手填。点「刷新模型列表」可重探，"
             + "点「测试连接」会真发一次请求验证。"
             + (state.services && state.services.length
                ? "引擎认得这些地址：" + state.services.map(function (s) {
                    return s.key;
                  }).join(" / ") + "。"
                : ""));
        paintModels();
        updateKeyHint();
      }

      function updateKeyHint() {
        const isApi = (state.ui !== "ollama");
        if (state.hasKey) {
          keyHint.textContent = "已保存 Key" + (state.keyTail ? "（尾号 " + state.keyTail + "）" : "")
            + "，要换就填新的，留空不改";
        } else {
          keyHint.textContent = isApi
            ? "还没保存 Key —— 外部 API 必填（在服务商后台申请）"
            : "还没保存 Key（本地 Ollama 用不到）";
        }
      }

      /** 探测模型目录（① 动态 → ② 内置候选；`force` = 点「刷新模型列表」）。 */
      async function refreshModels(force) {
        const body = { provider: persistedProvider(),
                       base_url: baseUrlIn.value.trim(),
                       refresh: !!force };
        const typed = keyIn.value.trim();
        if (typed) body.api_key = typed;          // 用户刚输入还没存的 Key 也带上
        if (state.model) body.model = state.model;
        refreshModelsBtn.disabled = true;
        const old = modelStatus.textContent;
        modelStatus.className = "faint";
        modelStatus.textContent = "正在探测模型目录…（本地 /api/tags，外部 {base_url}/models）";
        try {
          const d = await KBAdmin.post("/llm-discover", body, null, 40000);
          state.discovered = d || null;
          state.cacheAt = (d && d.at) || "";
          paintModels();
          ctx.log("[模型目录] " + sourceLabel(d && d.source) + "："
                  + ((d && (d.models || []).length) || 0) + " 个候选"
                  + (d && d.error ? "（" + d.error + "）" : ""));
        } catch (e) {
          state.discovered = { ok: false, source: "none", models: [],
                               error: msgOf(e), hint: "" };
          paintModels();
          ctx.log("[模型目录] 探测失败：" + msgOf(e), "warn");
        } finally {
          refreshModelsBtn.disabled = false;
        }
      }

      function applyConfig(cfg) {
        cfg = cfg || {};
        state.model = String(cfg.model || "");
        state.hasKey = cfg.has_key === true;
        state.keyTail = String(cfg.key_tail || "");
        state.services = Array.isArray(cfg.services) ? cfg.services : [];
        const raw = String(cfg.provider || "ollama");
        const baseUrl = String(cfg.base_url || "");
        // 落盘只有 ollama / openai；认不出的照实显示（保存时引擎白名单会挡住）
        const unknown = (raw !== "ollama" && raw !== "openai"
                         && raw !== "openai-compatible" && raw !== "openai_compatible"
                         && raw !== "api");
        state.ui = unknown ? "openai" : uiProviderOf(raw, baseUrl);
        if (unknown) {
          modelHint.textContent = "配置里的 provider「" + raw + "」引擎不认识"
            + "（只认 ollama / openai）—— 保存会被引擎挡住，请重选一个。";
        }
        const uopts = [
          { value: "ollama", text: "Ollama（本地）", title: "本机 Ollama" },
          { value: "openai", text: "外部 API（OpenAI 兼容）",
            title: "任何 OpenAI 兼容的 /chat/completions" },
          { value: "custom", text: "自定义（自建 / 其它兼容服务）",
            title: "vLLM、LM Studio、内网网关这类：自己填 base_url" },
        ];
        uiProviderSel.setOptions(uopts);
        uiProviderSel.value = state.ui;

        baseUrlIn.value = baseUrl;
        keyIn.value = "";
        syncProviderUI(false);
      }

      // ========================================================== 动作

      async function refreshAll() {
        // 三路各自失败各自报 —— 一路挂了不该把另外两块也变成空白。
        try {
          renderPos(await KBAdmin.get("/env-check"));
        } catch (e) {
          posHost.textContent = "";
          posHost.appendChild(el("div", { class: "note note-err",
            text: "读运行环境失败：" + msgOf(e) }));
        }
        try {
          renderHealth(await KBAdmin.get("/health"));
        } catch (e2) {
          svcHost.textContent = "";
          svcHost.appendChild(el("div", { class: "note note-err",
            text: "读引擎状态失败：" + msgOf(e2) }));
        }
        // 模型这块：先读配置（它有 provider/model/base_url），再探测目录
        // （探测要用到配置里的地址与模型名，顺序反了会拿不到它们）。
        let cfgErr = "";
        try {
          applyConfig(await KBAdmin.get("/llm-config"));
        } catch (e3) {
          cfgErr = "读模型配置失败：" + msgOf(e3);
        }
        if (cfgErr) {
          // 配置读不到比"目录探不到"更严重 —— 这一条要压在上面。
          modelStatus.className = "note note-err";
          modelStatus.textContent = cfgErr;
        } else {
          await refreshModels(false);
        }
      }

      /**
       * 保存前的**语法校验**（不查存在性）。返回一句人话（空串 = 没问题）。
       * 用户 2026-10-13 的要求：保存只做语法校验，模型名存不存在交给"保存后试连"。
       */
      function syntaxProblem() {
        const ui = String(uiProviderSel.value || "ollama");
        const m = String(modelSel.value || "").trim();
        const url = baseUrlIn.value.trim();
        if (ui === "ollama") return "";        // 本地留空 = 自动挑，合法
        if (!m) {
          return "外部 API 必须填模型 id（点「刷新模型列表」从候选里挑，或手填）。";
        }
        if (!/^[\x21-\x7E]+$/.test(m)) {
          return "模型 id 只能用可打印 ASCII（不能有空格、中文或全角字符）："
               + "现在是「" + m + "」。";
        }
        if (url && !/^https?:\/\/[^\s]+$/i.test(url)) {
          return "API 地址不是合法 URL：要以 http:// 或 https:// 开头，且不能有空格"
               + "（现在是「" + url + "」）。";
        }
        return "";
      }

      /** 表单 → 请求体。api_key **只有真填了才发**（留空 = 不改，别把旧 Key 抹掉）。 */
      function formBody() {
        const p = persistedProvider();
        const body = { provider: p, model: String(modelSel.value || "").trim() };
        if (p === "openai") {
          // 本地模式下 base_url 那一行是隐藏的：**不发**，免得把用户配好的外部
          // 地址悄悄清掉（切回外部时还要用）。
          body.base_url = baseUrlIn.value.trim();
        }
        const key = keyIn.value.trim();
        if (key) body.api_key = key;
        return body;
      }

      async function onSave() {
        const problem = syntaxProblem();
        if (problem) {
          // 语法错**直接挡住**（不发保存），就地红字说明；存在性不在这里查。
          modelStatus.className = "note note-err";
          modelStatus.textContent = "没保存：" + problem;
          return;
        }
        saveBtn.disabled = true;
        modelStatus.className = "faint";
        modelStatus.textContent = "正在保存…";
        try {
          await KBAdmin.post("/llm-config", formBody());
          applyConfig(await KBAdmin.get("/llm-config"));
          modelStatus.className = "faint";
          modelStatus.textContent = "已保存（写进知识库目录的 llm-config.json），立即生效。";
          // ⚠ 保存后**异步试连一次、不阻断**（不 await、不禁用表单）：
          //   存在性只能这样查 —— 结果就地显示。
          tryConnect(false);
        } catch (e) {
          modelStatus.className = "note note-err";
          modelStatus.textContent = "保存失败：" + msgOf(e);
        } finally {
          saveBtn.disabled = false;
        }
      }

      /** 试连结果 → 可操作的文案（401/403、404、连不上三种分开说）。 */
      function explainTest(d) {
        const prov = String((d && d.provider) || "");
        const model = String((d && d.model) || "");
        if (d && d.ok) {
          const n = (state.discovered && (state.discovered.models || []).length) || 0;
          return { kind: "ok", text: "✓ 通了：" + (model || "(自动挑)")
            + (d.base_url ? "　·　" + d.base_url : "")
            + (d.ms != null ? "　·　" + d.ms + "ms" : "")
            + (prov === "openai" && d.output ? "　·　模型回：" + d.output : "")
            + (prov === "ollama" && d.models && d.models.length
               ? "　·　本机 " + d.models.length + " 个模型" : "")
            + (prov === "openai" && n ? "　·　候选 " + n + " 个" : "") };
        }
        const err = String((d && d.error) || "没说明原因");
        const hint = String((d && d.hint) || "");
        const code = Number((d && d.status) || 0);
        if (code === 401 || code === 403) {
          return { kind: "err", text: "✗ 认证失败（HTTP " + code + "）：检查 API Key"
            + " —— 去服务商后台重新复制一个（注意别带空格/换行）。" + (hint ? "　" + hint : "") };
        }
        if (code === 404 || /not\s*found|does not exist/i.test(err)) {
          return { kind: "err", text: "✗ 服务商说找不到（HTTP " + (code || "?") + "）："
            + "模型名「" + (model || "(空)") + "」可能拼错，或这个服务商没有它 —— "
            + "点「刷新模型列表」看它的候选。" + (hint ? "　" + hint : "") };
        }
        if (!code) {
          return { kind: "err", text: "✗ 连不上：检查 API 地址（要带 /v1 之类）与网络"
            + " —— " + err + (hint ? "　" + hint : "") };
        }
        return { kind: "err", text: "✗ 没通（HTTP " + code + "）：" + err
          + (hint ? "　" + hint : "") };
      }

      /**
       * 测试连接 / 保存后试连。`manual` = 用户点了按钮（要禁用按钮并在开始前先保存）；
       * 保存后自动跑的那次不动按钮、不阻断（用户 2026-10-13 的要求）。
       */
      async function tryConnect(manual) {
        if (manual) {
          if (syntaxProblem()) { onSave(); return; }
          await KBAdmin.post("/llm-config", formBody()).catch(function () {});
          testBtn.disabled = true;
        }
        modelStatus.className = "faint";
        modelStatus.textContent = "正在真发一次请求（"
          + (state.ui === "ollama" ? "Ollama（本地）" : "外部 API") + "）…";
        try {
          // 90s：引擎侧只等 45s，这里给足余量
          const d = await KBAdmin.post("/llm-test", {}, null, 90000);
          const r = explainTest(d || {});
          modelStatus.className = (r.kind === "ok" ? "note note-ok" : "note note-err");
          modelStatus.textContent = r.text;
        } catch (e) {
          modelStatus.className = "note note-err";
          modelStatus.textContent = "✗ 测试失败：" + msgOf(e);
        } finally {
          if (manual) testBtn.disabled = false;
        }
      }

      function onRestart() {
        KBAdmin.dialog("重启引擎", el("div", null, [
          el("div", { text: "引擎是宿主半按需拉起的子进程，随 Zotero 起落。" }),
          el("div", { class: "sep" }),
          el("div", { class: "faint",
            text: "点「确认」会停掉当前引擎进程；下一次请求（包括本页的刷新）"
                + "会自动拉起一个新进程。有任务正在跑时不会停，会如实告诉你稍后再试。" }),
        ]), [
          { text: "取消" },
          { text: "确认重启", primary: true, run: doRestart },
        ]);
      }

      async function doRestart() {
        restartNote.className = "faint";
        restartNote.textContent = "正在请求…";
        try {
          const r = await KBAdmin.post("/service/restart", {});
          if (r && r.ok === false) {
            restartNote.className = "note note-warn";
            restartNote.textContent = "没有重启：" + String(r.note || "引擎正忙");
            return;
          }
          restartNote.className = "note note-ok";
          restartNote.textContent = "✓ 引擎已重启（旧进程已停，新进程已按需拉起）。";
          await refreshAll();                  // 刷新 == 下一次请求，会拉起新引擎
        } catch (e) {
          restartNote.className = "note note-err";
          restartNote.textContent = "重启请求失败：" + msgOf(e);
        }
      }

      // ========================================================== 起步
      refreshAll();
      // 「只列带后缀的模型」是显示偏好（原设置页那个勾选框），存在 pref 里；
      // 模型配置本身仍然只有 llm-config.json 一份。
      KBAdmin.get("/plugin-prefs").then(function (pref) {
        const v = (pref && pref.values) || {};
        state.filterBare = v.filterBareModels !== false;
        filterCb.checked = state.filterBare;
        paintModels();
      }).catch(function (e) {
        ctx.log("[模型] 读显示偏好失败：" + msgOf(e), "warn");
      });
      ctx.log("运行环境页：开始检测八个位置与引擎状态");
    },
  });
})();
