/* ============================================================================
 * admin-pages/env.js —— 「运行环境」页（四个位置从哪来、服务在不在、模型怎么配）
 *
 * 对应**已退役（2026-10-14）**的 Tk 面板 tools/panels/tab_env.py（已退役 2026-10-14）。那一页最花力气的是两件事：
 *   ①「项目目录 / Python / Ollama / MinerU」这四个位置**值从哪来、谁覆盖了谁**；
 *   ②「模型接入」表单（provider / model / base_url / api_key）。
 * 这里照它的口径重画，取数改成问宿主半 → 引擎（字段见 docs/ADMIN-API-W2.md）：
 *   GET  /env-check    四个位置 + 来源 + 被否掉的高优先档
 *   GET  /health       服务与各位置（顶部那句话与「本机服务」块复用）
 *   GET  /models       Ollama 本机模型列表
 *   GET  /llm-config   当前模型配置（api_key 只回 has_key / key_tail）
 *   POST /llm-config   保存模型配置（api_key 留空 = 不改）
 *   POST /service/restart
 *
 * 与 Tk 版的取舍（为什么少了几样东西）：
 *   · 不做"浏览… / 保存并重新检测"：改这四个位置仍在 Zotero 插件的设置面板里
 *     （两边写的是同一份 kb-location.json）。这一页只负责把它**说清楚**；
 *   · 「重启服务」如实说明：契约里 POST /service/restart 本轮是**占位**返回
 *     （HTTP 服务退场后由宿主半管理生命周期），页面把引擎原话照抄，不假装成功。
 *
 * 页面约定照抄 admin.js 顶部三条与 structure.js 的写法：只用 admin.css 的类、
 * 数据一律 KBAdmin.get/post、一页一文件、注释说清为什么。
 * ==========================================================================*/
(function () {
  "use strict";

  // 四个位置在引擎里的键 → 中文名。契约里 items 已带 name，这里只是**兜底**
  // （口径与 offline/schemas.py::ENV_SOURCE_LABELS 同一份）。
  const ENV_LABELS = {
    project_root: "项目目录",
    python: "Python 解释器",
    ollama: "Ollama（可选）",
    mineru: "MinerU（可选）",
  };

  // 可选组件：找不到也不影响别的功能，状态列要区别对待（别吓唬用户）。
  const OPTIONAL_KEYS = { ollama: 1, mineru: 1 };

  function msgOf(e) {
    return (e && e.message) ? e.message : String(e);
  }

  KBAdmin.page({
    id: "env",
    title: "运行环境",
    order: 40,

    async render(host, ctx) {
      const el = ctx.el;

      // ------------------------------------------------------ 卡片 1：四个位置
      const posHost = el("div", null,
                         [el("div", { class: "note note-warn", text: "正在检测四个位置…" })]);
      const srcNote = el("div", { class: "faint", text: "" });
      const redetectBtn = el("button", {
        class: "btn btn-subtle", text: "重新检测",
        on: { click: function () { refreshAll(); } },
      });
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "四个位置" }),
          el("span", { class: "faint", text: "值从哪来、在不在（路径很长，悬停看全）" }),
          el("span", { class: "grow" }),
          redetectBtn,
        ]),
        el("div", { class: "sep" }),
        posHost,
        el("div", { class: "sep" }),
        srcNote,
      ]));

      // ------------------------------------------------------ 卡片 2：本机服务
      const svcHost = el("div", null,
                         [el("div", { class: "note note-warn", text: "正在读服务状态…" })]);
      const restartNote = el("div", { class: "faint",
        text: "点「重启服务」会请宿主半处理引擎的生命周期。注意：契约里这个接口"
            + "本轮是占位返回（HTTP 服务退场后由宿主半管理生命周期），"
            + "不会真的重启，页面会把引擎的原话照抄出来。" });
      const restartBtn = el("button", {
        class: "btn btn-subtle", text: "重启服务",
        on: { click: onRestart },
      });
      host.appendChild(el("div", { class: "card" }, [
        el("div", { class: "row" }, [
          el("strong", { text: "本机服务" }),
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
                      keyTail: "", services: [], discovered: null, cacheAt: "" };

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
      // 模型一栏的"提示句"（本地/外部两套口径 + 探测结论，见 syncProviderUI/refreshModels）
      const modelHint = el("div", { class: "faint", text: "" });
      const modelNote = el("div", { class: "faint",
        text: "保存到知识库目录下的 llm-config.json —— 服务端与面板的所有模型功能"
            + "（分类建议 / 元数据兜底 / 提示词试跑 / 经验草稿）都用这一份，"
            + "改完立即生效、不用重启服务。Zotero 插件右键菜单用的是插件自己的 pref。" });

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
       * 路径列：格子里只放一段、靠 CSS 省略，完整值进 title。
       *
       * 为什么不做"头尾保留"自己截断：路径长度差很多，硬截会切掉真正有用的尾段
       * （pythonw.exe / ollama.exe 这种）。交给 title 最诚实。
       */
      function pathCell(raw) {
        const t = String(raw == null ? "" : raw);
        const s = el("span", { class: "mono", text: t || "（未找到）" });
        s.title = t || "（未找到）";
        return s;
      }

      /** 状态列：存在 / 找不到；可选组件另给一句不吓人的说明。 */
      function statusCell(r) {
        const ok = r.ok === true;
        const optional = r.optional === true || OPTIONAL_KEYS[r.key] === 1;
        const s = el("span", {
          class: ok ? "tag-ok" : "tag-bad",
          text: ok ? "✓ 存在" : "✗ 找不到",
        });
        s.title = ok
          ? "这个路径存在"
          : (optional ? "可选组件：不装 / 找不到都不影响其他功能"
                      : "找不到这个路径，用到它的功能会报错");
        return s;
      }

      function renderPos(d) {
        const items = (d && Array.isArray(d.items)) ? d.items : [];
        posHost.textContent = "";
        if (!items.length) {
          posHost.appendChild(el("div", { class: "note note-warn",
            text: "引擎没有回任何一项（/env-check 回包是空的）。" }));
        } else {
          const cols = [
            // 表头排序口径见 admin.js::table：项目名 / 来源 / 状态可排；
            // 「路径」是长自由文本（还会被 clip 掐断），**不给排序**。
            { key: "name", label: "项目", sortable: true,
              sortValue: function (r) {
                return String(r.name || ENV_LABELS[r.key] || r.key || "");
              },
              render: function (r) {
                return String(r.name || ENV_LABELS[r.key] || r.key || "");
              } },
            { key: "path", label: "路径",
              render: function (r) { return pathCell(r.path); } },
            { key: "source", label: "来源", sortable: true,
              render: function (r) {
                const s = String(r.source == null ? "" : r.source).trim();
                const span = el("span", { text: s || "未知" });
                span.title = s
                  ? ("这一项最终取自「" + s + "」")
                  : "引擎没报来源（可能是旧版本引擎）";
                return span;
              } },
            { key: "ok", label: "状态", sortable: true,
              // 不可用的排前面（升序 = 先看不正常的），别按中文"正常/缺"的字面排。
              sortValue: function (r) { return r.ok ? 1 : 0; },
              render: function (r) { return statusCell(r); } },
          ];
          posHost.appendChild(KBAdmin.table(cols, items));
        }

        // 「谁覆盖了谁 / 哪一档被否掉」——Tk 面板花最多篇幅讲的就是这句，
        // 这里把它压成一行提示：只在**真有问题**时才出现，正常时不占视线。
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
          srcNote.textContent = "优先级：环境变量 > 运行时设置（面板写的 kb-location.json）"
            + " > .env > 自动探测（调用方显式指定最优先）。留空的项交回下一档。";
        }
      }

      function renderHealth(h) {
        svcHost.textContent = "";
        const rows = [
          ["服务", (h && h.service) ? String(h.service) : "本机引擎（stdio）"],
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
      // 「模型目录」三层兜底（设计依据见 docs/MODEL-SELECTION.md，别在这里发明第四层）：
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

      /** 模型候选（把 state.discovered 画成下拉选项）。 */
      function modelOptions() {
        const d = state.discovered || {};
        const opts = [];
        if (state.ui === "ollama") {
          opts.push({ value: "", text: "（自动挑选）",
                      title: "留空 = 交回引擎自动挑（优先 qwen3:4b-instruct）" });
        }
        (d.models || []).forEach(function (m) {
          const id = String((m && m.id) || "");
          if (!id) return;
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
        keyIn.placeholder = isApi ? "填服务商给的 Key（必填）" : "留空 = 不改";
        modelSel.input.placeholder = ui === "ollama"
          ? "留空 = 自动挑，或填本机模型名（ollama list）"
          : "从候选里挑，或直接输入服务商的模型 id";
        modelHint.textContent = ui === "ollama"
          ? ("本地 Ollama：候选来自本机 /api/tags（比 /v1/models 多给参数量与量化档）。"
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
            text: "读服务状态失败：" + msgOf(e2) }));
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
          return { kind: "err", text: "✗ 服务端说找不到（HTTP " + (code || "?") + "）："
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
        KBAdmin.dialog("重启服务", el("div", null, [
          el("div", { text: "这一页跑在 Zotero 里，服务的生命周期由宿主半（插件）管。" }),
          el("div", { class: "sep" }),
          el("div", { class: "faint",
            text: "契约里 POST /service/restart 本轮是占位返回："
                + "HTTP 服务退场后由宿主半管理生命周期。点「确认」不会真的重启，"
                + "页面会把引擎返回的原话显示出来。" }),
        ]), [
          { text: "取消" },
          { text: "确认", primary: true, run: doRestart },
        ]);
      }

      async function doRestart() {
        restartNote.className = "faint";
        restartNote.textContent = "正在请求…";
        try {
          const r = await KBAdmin.post("/service/restart", {});
          const started = r && r.started === true;
          restartNote.className = started ? "note note-ok" : "note note-warn";
          restartNote.textContent = (started ? "✓ 已请求重启。" : "本轮没有真的重启：")
            + ((r && r.note) || "引擎没有说明原因");
        } catch (e) {
          restartNote.className = "note note-err";
          restartNote.textContent = "重启请求失败：" + msgOf(e);
        }
      }

      // ========================================================== 起步
      refreshAll();
      ctx.log("运行环境页：开始检测四个位置与服务状态");
    },
  });
})();
