// 设置面板的交互逻辑。由 bootstrap.js 通过 PreferencePanes 的 scripts 选项加载。
// 用 Zotero.Prefs 读写（与 bootstrap.js 里的键保持一致）。

const KB_PREFS = {
  server: "zotero-kb.server",
  token: "zotero-kb.token",
  model: "zotero-kb.model",
  categories: "zotero-kb.categories",
  autoProcess: "zotero-kb.autoProcess",
  provider: "zotero-kb.provider",
  apiModel: "zotero-kb.apiModel",
  apiKey: "zotero-kb.apiKey",
  apiBaseUrl: "zotero-kb.apiBaseUrl",
  acquireConfirm: "zotero-kb.acquireConfirm",
};

/**
 * 勾选框在"这个 pref 从没被写过"时该显示成什么。
 *
 * 为什么需要这张表：Zotero.Prefs.get() 对没写过的键返回 undefined，
 * 面板得自己决定"没设过"等于勾还是没勾。原来的写法是
 *     el.checked = v !== false && v !== undefined ? !!v : true
 * 它把 undefined 和 **false 一起**推到了 `true` 分支 —— 于是用户把某个
 * 勾去掉、存成 false，下次打开设置面板它又是勾上的（看着像没生效）。
 * 现在按"未设过就用这里的默认值，设过就照实显示"来。
 */
const KB_PREF_DEFAULTS = {
  "zotero-kb.autoProcess": true,
  // 从 DSH 导入文献：**默认不问**。搜索/列表/挑选都在 DSH 对话里做完了，
  // 再弹一个 Zotero 模态框会把一次连贯的对话中断成两个界面（用户否掉过）。
  "zotero-kb.acquireConfirm": false,
  "zotero-kb.syncEnabled": false,
};

/**
 * 把设置面板里的模型配置推给本机知识库服务（写进它的 llm-config.json）。
 *
 * 为什么要有这一步：插件设置是配置的**唯一来源**，但服务端还会被别的入口
 * 调用（管理面板、命令行、定时任务），它们不经过插件。推一次之后，
 * 所有入口都用同一套配置，不会出现"插件用 A、面板用 B"。
 *
 * @param {boolean} silent 静默模式（切换来源时用，不弹状态文字）
 */
async function kbPushModelConfig(doc, silent) {
  const KB = Zotero.ZoteroKB;
  if (!KB) return { ok: false, error: "插件还没就绪" };
  const get = (k) => {
    try { return String(Zotero.Prefs.get(k) || "").trim(); } catch (e) { return ""; }
  };
  const provider = get(KB_PREFS.provider) || "ollama";
  const isApi = provider === "openai";
  const body = {
    __write: true,
    provider,
    base_url: isApi ? get(KB_PREFS.apiBaseUrl) : "",
    api_key: isApi ? get(KB_PREFS.apiKey) : "",
    // Ollama 用 model；API 用 apiModel。服务端只认一个 model 字段，
    // 所以按当前来源挑对应的那个。
    model: isApi ? get(KB_PREFS.apiModel) : get(KB_PREFS.model),
  };
  try {
    const r = await KB.request("POST", "/llm-config", body);
    if (r && r.ok) {
      Zotero.debug("[zotero-kb] 模型配置已同步给服务端：" + provider);
      return { ok: true };
    }
    return { ok: false, error: (r && r.error) || "服务端没接受" };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/**
 * 运行环境区：项目目录 / Python / Ollama。
 *
 * 三个位置**互相独立** —— 这是本项目踩过最久的一个坑：
 *   老代码让「项目根」= 「知识库位置的上一级」，知识库一搬到 Zotero 数据
 *   目录，就算出 D:\Application\ZoteroData\Zotero，那里没有 .venv，
 *   于是管理面板报"找不到 py 环境"。所以现在谁也别推谁，全部显式配置 + 探测。
 */
function kbInitEnvSection(doc, root, save) {
  const KB = Zotero.ZoteroKB;
  const st = doc.getElementById("zotero-kb-env-status");
  const rep = doc.getElementById("zotero-kb-env-report");

  const setStatus = (text, color) => {
    if (!st) return;
    st.textContent = text;
    st.style.color = color || "#888";
  };

  /** 把检测报告渲染成"人话"：每项一行，✓/✗ + 该改哪个框。 */
  const renderReport = (r) => {
    if (!rep) return;
    if (!r || !r.python) {
      rep.innerHTML = "";
      return;
    }
    const line = (ok, label, detail, hint) => {
      const color = ok ? "#0a0" : "#c00";
      const mark = ok ? "✓" : "✗";
      let s = '<div style="color:' + color + '">' + mark + " " + label
        + ' <span style="color:#666">' + (detail || "") + "</span></div>";
      if (!ok && hint) {
        s += '<div style="color:#c60; margin-left:14px;">→ ' + hint + "</div>";
      }
      return s;
    };
    let html = "";
    html += line(r.python.runnable, "Python",
                 (r.python.version || "") + "  " + (r.python.path || "(未找到)"),
                 r.python.hint);
    html += line(r.project_root.has_panel, "项目目录",
                 r.project_root.path || "(未找到)", r.project_root.hint);
    const o = r.ollama || {};
    html += line(!!o.path, "Ollama",
                 (o.path || "(未找到，可选)")
                 + (o.api_up ? "  [服务在跑" + (o.models && o.models.length
                     ? "：" + o.models.join(", ") : "") + "]" : ""),
                 o.hint);
    // 候选列表：用户"装了但没找到"时，靠这个判断是不是装到别处了
    const cands = (r.python.candidates || []);
    if (!r.python.runnable && cands.length) {
      html += '<div style="color:#666; margin-top:4px;">找到这些候选：'
        + cands.slice(0, 5).join("　") + "</div>";
    }
    rep.innerHTML = html;
  };

  /** 保存三个框 → 推给服务端 → 拿回诊断报告。 */
  const saveAndCheck = async (quiet) => {
    if (!KB) { setStatus("✗ 插件还没就绪", "#c00"); return; }
    root.querySelectorAll("[data-pref]").forEach(save);

    const get = (id) => {
      const el = doc.getElementById(id);
      return el ? String(el.value || "").trim() : "";
    };
    const env = {
      project_root: get("zotero-kb-project-root"),
      python: get("zotero-kb-python-exe"),
      ollama: get("zotero-kb-ollama-exe"),
    };

    // ⚠ 存完立刻**读回**比对。
    //   为什么要多这一步：pref 键名如果写错，存下去和读回来是两个不同的键
    //   （本机踩过双前缀），界面上看不出任何异常 —— 用户以为保存了，
    //   插件读的却是空。读回不一致就直接报出来，省掉一轮来回排查。
    const back = readBackPrefs(doc);
    kbDiag("保存并检测：读回 projectRoot=" + JSON.stringify(back.projectRoot)
           + " python=" + JSON.stringify(back.python)
           + " ollama=" + JSON.stringify(back.ollama));
    const mismatch = [];
    if ((env.project_root || "") !== (back.projectRoot || "")) {
      mismatch.push("项目目录");
    }
    if ((env.python || "") !== (back.python || "")) mismatch.push("Python");
    if ((env.ollama || "") !== (back.ollama || "")) mismatch.push("Ollama");
    if (mismatch.length) {
      setStatus("✗ " + mismatch.join("、")
                + " 没能存进配置（pref 键名可能有问题，见 settings-init.log）",
                "#c00");
      return;
    }

    if (!quiet) setStatus("已保存，正在检测…", "#888");
    const res = await KB.kbPushEnvConfig(env);
    if (!res.ok) {
      setStatus("✗ " + (res.error || "服务端没接受（服务在运行吗？）"), "#c00");
      return;
    }
    const r = res.report || (await KB.kbEnvReport());
    renderReport(r);
    if (r && r.python && r.python.runnable && r.project_root.has_panel) {
      setStatus("✓ 已保存，环境可用", "#0a0");
    } else {
      setStatus("✓ 已保存，但环境有问题（看下面的报告）", "#c60");
    }
  };

  // 三个「浏览…」按钮
  const pick = (btnId, inputId, mode) => {
    const btn = doc.getElementById(btnId);
    const input = doc.getElementById(inputId);
    if (!btn || !input || !KB) return;
    btn.addEventListener("click", async () => {
      const picked = mode === "folder"
        ? await KB.pickFolder("选择项目目录（含 offline、online、.venv 的那个）")
        : await KB.pickFile(
            inputId.indexOf("python") >= 0
              ? "选择 python.exe（推荐 .venv\\Scripts\\pythonw.exe）"
              : "选择 ollama.exe",
            "程序", "*.exe");
      if (!picked) return;
      input.value = picked;
      save(input);
      await saveAndCheck();          // 选完立刻验证，用户马上知道对不对
    });
  };
  pick("zotero-kb-browse-root-btn", "zotero-kb-project-root", "folder");
  pick("zotero-kb-browse-python-btn", "zotero-kb-python-exe", "file");
  pick("zotero-kb-browse-ollama-btn", "zotero-kb-ollama-exe", "file");

  const saveBtn = doc.getElementById("zotero-kb-env-save-btn");
  if (saveBtn) saveBtn.addEventListener("click", () => saveAndCheck());

  const openBtn = doc.getElementById("zotero-kb-env-open-panel-btn");
  if (openBtn) {
    openBtn.addEventListener("click", () => {
      try {
        if (KB && KB.openPanel) KB.openPanel();
      } catch (e) { setStatus("✗ " + e, "#c00"); }
    });
  }

  // 打开设置面板时自动检测一次：让用户一进来就看到环境状态，
  // 而不是点了「打开管理面板」才发现有问题。
  setTimeout(() => { saveAndCheck(true); }, 500);
}

/**
 * 读回三个运行环境 pref 的当前值（用于确认"存进去的"和"读出来的"是同一个键）。
 *
 * ⚠ 键名必须是**相对键**（`zotero-kb.X`）：`Zotero.Prefs.get` 会自动补
 *   `extensions.zotero.` 前缀。写成完整键会变成双前缀，读出来永远是空
 *   —— 本机就这么中招过（token 读成空 → 需要认证的接口全 401）。
 */
function readBackPrefs(doc) {
  const out = {};
  const map = {
    projectRoot: "zotero-kb.projectRoot",
    python: "zotero-kb.pythonExe",
    ollama: "zotero-kb.ollamaExe",
    model: "zotero-kb.model",
    token: "zotero-kb.token",
  };
  for (const [k, key] of Object.entries(map)) {
    try {
      const v = Zotero.Prefs.get(key);
      out[k] = (v === undefined || v === null) ? "" : String(v);
    } catch (e) { out[k] = ""; }
  }
  return out;
}

/**
 * 设置面板诊断日志：每一步落盘到 <知识库>\settings-init.log。
 *
 * 为什么要它（用户报了两次"模型下拉出不来 / 版本号是横线"，我两次都只能猜）：
 *   Zotero 的设置面板跑在 `Cu.Sandbox` 里，`Zotero.debug` 的输出平时看不到，
 *   出错又被 catch 吞掉 —— 于是"没生效"这件事完全没法查。
 *   写文件最简单直接：用户打开一次设置，读文件就知道卡在哪一步。
 *
 * ⚠ 每次初始化**覆盖**写（不留陈年旧账），之后逐条 append ——
 *   这样"日志只有前几行"本身就说明卡在哪。
 */
function kbDiagReset() {
  try {
    const KB = Zotero.ZoteroKB;
    if (!KB || !KB.kbDir || !KB.kbDir()) return;
    const p = KB.kbDir() + "\\settings-init.log";
    let env = "";
    try {
      env += "沙箱里有 document："
        + (typeof document !== "undefined" && !!document) + "\n";
      if (typeof document !== "undefined" && document) {
        env += "document.location = " + String(document.location) + "\n";
        env += "document 里能找到面板根节点："
          + !!document.getElementById("zotero-kb-settings") + "\n";
      }
    } catch (e) { env += "读 document 出错：" + e + "\n"; }
    try {
      let n = 0;
      const en = Services.wm.getEnumerator("zotero:pref");
      while (en.hasMoreElements()) { en.getNext(); n++; }
      env += "打开的设置窗口数：" + n + "\n";
    } catch (e) { env += "枚举设置窗口出错：" + e + "\n"; }
    Zotero.File.putContents(
      Zotero.File.pathToFile(p),
      "=== 设置面板初始化 " + new Date().toISOString() + " ===\n"
      + "ZoteroKB.version = " + JSON.stringify(KB.version) + "\n"
      + env);
  } catch (e) { /* ignore */ }
}

function kbDiag(line) {
  try {
    const KB = Zotero.ZoteroKB;
    if (!KB || !KB.kbDir || !KB.kbDir()) return;
    const p = KB.kbDir() + "\\settings-init.log";
    const f = Zotero.File.pathToFile(p);
    const old = f.exists() ? Zotero.File.getContents(f) : "";
    Zotero.File.putContents(f, old + line + "\n");
  } catch (e) { /* ignore */ }
  try { Zotero.debug("[zotero-kb/prefs] " + line); } catch (e) { /* ignore */ }
}

/**
 * 把本机已装的 Ollama 模型填进下拉列表。
 *
 * 用户要求："oll 的本地模型选择也用下拉列表。"
 * 原来是个空白输入框 —— 用户得先自己 `ollama list` 看有哪些模型、
 * 再手打模型名，打错一个字就报"模型不存在"。现在直接列出来点选。
 *
 * 实现要点（踩过的坑）：
 *   · `<html:select>` 用**索引**赋初值，不要用 el.value ——
 *     虽然设 value 在"存在匹配 option"时是对的，但一旦赋了不匹配的值，
 *     某些浏览器会把**任意文本**塞进 value，之后 data-pref 保存时
 *     就把这个脏值写进 pref。
 *   · 拉列表是异步的，所以先用「已保存的值」建一个条目让它显示出来，
 *     拉到真列表后再补齐并恢复选中 —— 否则会有一下显示空白。
 *   · 已保存的值如果**不在**本机列表里（换过机器、模型被删），
 *     也要把它作为一个选项留着（标「已保存，但本机没找到」），
 *     不然用户一打开设置就被静默改成别的模型。
 */
async function kbFillOllamaModels(doc, root) {
  const sel = doc.getElementById("zotero-kb-ollama-model");
  const hint = doc.getElementById("zotero-kb-ollama-model-hint");
  const btn = doc.getElementById("zotero-kb-refresh-models-btn");
  kbDiag("kbFillOllamaModels: 进入；select=" + !!sel + " hint=" + !!hint
         + " btn=" + !!btn);
  if (!sel) {
    kbDiag("  没有 select 元素，直接返回（xhtml 里 id 对不对？）");
    return;
  }
  const KB = Zotero.ZoteroKB;
  kbDiag("  Zotero.ZoteroKB=" + !!KB
         + (KB ? ("  kbDir=" + (KB.kbDir ? KB.kbDir() : "无 kbDir 方法")) : ""));

  const saved = (() => {
    try {
      return String(Zotero.Prefs.get("zotero-kb.model") || "").trim();
    } catch (e) { return ""; }
  })();

  kbDiag("  已保存的模型名=" + JSON.stringify(saved));

  const rebuild = (models, extraNote, recommended) => {
    // 清空重填（保留"自动选"这一项在最前）
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    // ⚠ 用 `doc.createElement("option")` + 直接给属性赋值，
    //   **不要**用 `createElementNS(名字空间, "option")`。
    //   Zotero 把 preference pane 当 HTML 片段插进 document，
    //   `createElement` 会走文档的默认命名空间、生成真正的 HTMLOptionElement；
    //   而 createElementNS 显式指定命名空间时，某些实现生成的是**普通元素**，
    //   下拉框点开是空的（本机实测："下拉仍然点不开"）。
    //   能正常工作的 pdf2zh 也是这么写的：doc.createElement("option")。
    const add = (value, label) => {
      const o = doc.createElement("option");
      o.value = value;
      o.textContent = label;
      sel.appendChild(o);
      return o;
    };
    add("", "自动选（推荐：质量更好的那个）");
    let matchedIdx = 0;
    models.forEach((m, i) => {
      add(m.name, m.name + (m.note ? ("　— " + m.note) : ""));
      if (m.name === saved) matchedIdx = i + 1;      // +1 因为前面有"自动选"
    });
    if (saved && matchedIdx === 0) {
      // 保存的模型本机没有 —— 留着它，并让用户知道
      add(saved, saved + "　— 已保存，但本机没找到");
      matchedIdx = models.length + 1;
    }
    // ⚠ 用索引，不用 sel.value（见函数头说明）
    try { sel.selectedIndex = matchedIdx; } catch (e) { /* ignore */ }
    kbDiag("  rebuild 完成：option 数=" + sel.options.length
           + " selectedIndex=" + sel.selectedIndex
           + " 传入模型数=" + models.length
           + (extraNote ? (" 服务端备注=" + extraNote) : ""));
    return matchedIdx;
  };

  const load = async (verbose) => {
    if (hint) { hint.textContent = "正在读本机已装的模型…"; hint.style.color = "#888"; }
    let data = null;
    try {
      kbDiag("  请求 GET /models …");
      data = await KB.request("GET", "/models");
      kbDiag("  /models 返回：" + JSON.stringify(data).slice(0, 300));
    } catch (e) {
      kbDiag("  /models 抛错：" + e);
      data = null;
    }
    if (!data) {
      // 服务没起来 —— 退化成"只有自动选"的列表，并说清原因
      rebuild([], null, "");
      kbDiag("  服务无响应，已降级显示");
      if (hint) {
        hint.textContent = "✗ 读不到模型列表：本机知识库服务没在响应。"
          + "（Zotero 启动时会自动拉起它；也可以双击 scripts\\4-service.vbs）";
        hint.style.color = "#c00";
      }
      return;
    }
    const models = (data.models || []);
    rebuild(models, data.hint, data.recommended);
    if (!hint) return;
    if (!data.ok) {
      hint.textContent = "✗ " + (data.error || "连不上 Ollama")
        + "　" + (data.hint || "");
      hint.style.color = "#c00";
    } else if (!models.length) {
      hint.textContent = "⚠ Ollama 在跑，但还没装模型。先拉一个："
        + "ollama pull qwen3:4b-instruct";
      hint.style.color = "#c60";
    } else {
      hint.textContent = "本机已装 " + models.length + " 个模型"
        + (data.recommended ? ("；推荐 " + data.recommended) : "")
        + "。要装新的：ollama pull <模型名>";
      hint.style.color = "#888";
    }
    if (verbose) {
      Zotero.debug("[zotero-kb] 模型列表已刷新：" + models.length + " 个");
    }
  };

  if (btn) btn.addEventListener("click", () => load(true));
  // 先用已保存的值垫一下（避免下拉框空着），再去拉真列表
  rebuild([], null, "");
  await load(false);
}

/**
 * 装配设置面板。
 *
 * ⚠ 必须是 **async**：下面要 `await kbFillOllamaModels()` 去本机服务拉模型列表。
 *   普通函数里写 `await` 在运行时会直接 `SyntaxError`，而
 *   `tools/check_js_syntax.py` **查不出来** —— 它把整个文件包成
 *   AsyncFunction 来校验，于是"外层不是 async 却用了 await"这种错被掩盖了。
 *   （本机就这么写错过一次。该工具已补上这一项检查。）
 */
async function kbInitPane(doc) {
  const root = doc.getElementById("zotero-kb-settings");
  kbDiagReset();
  kbDiag("kbInitPane: root=" + !!root);
  if (!root) {
    kbDiag("  找不到 #zotero-kb-settings，直接返回");
    return;
  }

  // 1) 把已存的值填进输入框
  root.querySelectorAll("[data-pref]").forEach((el) => {
    const key = el.getAttribute("data-pref");
    try {
      const v = Zotero.Prefs.get(key);
      if (el.type === "checkbox") {
        // 没设过 → 用声明的默认值；设过 → 照实显示（见 KB_PREF_DEFAULTS 的注释）
        const dflt = KB_PREF_DEFAULTS[key] !== undefined
          ? KB_PREF_DEFAULTS[key] : true;
        el.checked = (v === undefined || v === null) ? dflt : !!v;
      }
      else el.value = v == null ? "" : String(v);
    } catch (e) { /* 未设置过就留空 */ }
  });

  // 2) 改动即保存
  const save = (el) => {
    const key = el.getAttribute("data-pref");
    try {
      if (el.type === "checkbox") Zotero.Prefs.set(key, el.checked);
      else Zotero.Prefs.set(key, el.value.trim());
    } catch (e) { Zotero.debug("[zotero-kb] 保存设置失败：" + e); }
  };
  root.querySelectorAll("[data-pref]").forEach((el) => {
    el.addEventListener("change", () => save(el));
    el.addEventListener("blur", () => save(el));
  });

  // 2b-2) Ollama 模型下拉列表（从本机拉，别让用户手打模型名）
  kbFillOllamaModels(doc, root).catch((e) => Zotero.debug(
    "[zotero-kb] 填模型列表失败：" + e));

  // 2d) 知识库位置：显示 / 打开 / 迁移
  const kbDirEl = doc.getElementById("zotero-kb-kbdir");
  const kbSt = doc.getElementById("zotero-kb-kbdir-status");
  const kbRefresh = doc.getElementById("zotero-kb-refresh-kbdir-btn");
  const kbOpen = doc.getElementById("zotero-kb-open-kbdir-btn");
  const kbMigrate = doc.getElementById("zotero-kb-migrate-btn");

  const showLocation = async (verbose) => {
    if (!kbDirEl) return null;
    const KB = Zotero.ZoteroKB;
    if (!KB) return null;
    try {
      const r = await KB.request("GET", "/kb-location");
      if (r && r.kb_dir) {
        kbDirEl.textContent = r.kb_dir;
        if (verbose && kbSt) {
          kbSt.textContent = "✓ 已从服务端读取";
          kbSt.style.color = "#0a0";
        }
        return r;
      }
      kbDirEl.textContent = "（服务端没返回位置）";
      return null;
    } catch (e) {
      kbDirEl.textContent = "（读不到 —— 服务没启动？）";
      return null;
    }
  };
  showLocation(false);                       // 打开面板就自动读一次

  if (kbRefresh) {
    kbRefresh.addEventListener("click", () => showLocation(true));
  }
  if (kbOpen) {
    kbOpen.addEventListener("click", async () => {
      const KB = Zotero.ZoteroKB;
      try {
        const r = await KB.request("GET", "/kb-location");
        if (r && r.kb_dir) {
          Zotero.File.pathToFile(r.kb_dir).reveal();
        }
      } catch (e) { /* ignore */ }
    });
  }
  if (kbMigrate) {
    kbMigrate.addEventListener("click", async () => {
      const KB = Zotero.ZoteroKB;
      if (!KB) return;
      let info = null;
      try { info = await KB.request("GET", "/kb-location"); } catch (e) { /* ignore */ }
      if (!info || !info.default_follow) {
        if (kbSt) {
          kbSt.textContent = "✗ 读不到建议位置（服务没启动？）";
          kbSt.style.color = "#c00";
        }
        return;
      }
      const target = info.default_follow;
      const ok = Services.prompt.confirm(
        Zotero.getMainWindow(),
        "迁移知识库",
        "把知识库迁移到：\n" + target
        + "\n\n当前位置：" + info.kb_dir
        + "\n\n说明：\n"
        + "· 是**复制**，旧目录会保留（确认没问题后你自己删）\n"
        + "· 会先自动备份一份\n"
        + "· 迁移完成后要**重启知识库服务**才生效\n\n继续吗？");
      if (!ok) return;
      if (kbSt) { kbSt.textContent = "迁移中…（可能要几十秒）"; kbSt.style.color = "#888"; }
      try {
        const r = await KB.request("POST", "/kb-location", { migrate_to: target });
        if (r && r.ok) {
          if (kbSt) {
            kbSt.textContent = "✓ 已迁移到 " + r.kb_dir + "（请重启服务）";
            kbSt.style.color = "#0a0";
          }
          if (kbDirEl) kbDirEl.textContent = r.kb_dir;
        } else {
          if (kbSt) {
            kbSt.textContent = "✗ " + ((r && r.error) || "迁移失败");
            kbSt.style.color = "#c00";
          }
        }
      } catch (e) {
        if (kbSt) { kbSt.textContent = "✗ " + e; kbSt.style.color = "#c00"; }
      }
    });
  }

  // 2e) 同步：显示/隐藏目标输入，以及「立即同步一次」
  const syncMode = doc.getElementById("zotero-kb-sync-mode");
  const syncWrap = doc.getElementById("zotero-kb-sync-target-wrap");
  const syncNow = doc.getElementById("zotero-kb-sync-now-btn");
  const syncNowSt = doc.getElementById("zotero-kb-sync-now-status");
  const syncVisibility = () => {
    if (!syncMode || !syncWrap) return;
    syncWrap.style.display = (syncMode.value || "none") === "none" ? "none" : "block";
  };
  if (syncMode) {
    syncVisibility();
    syncMode.addEventListener("change", () => { save(syncMode); syncVisibility(); });
  }
  if (syncNow) {
    syncNow.addEventListener("click", async () => {
      root.querySelectorAll("[data-pref]").forEach(save);
      const KB = Zotero.ZoteroKB;
      const mode = (syncMode && syncMode.value) || "none";
      let target = "";
      try { target = String(Zotero.Prefs.get("zotero-kb.syncTarget") || ""); }
      catch (e) { /* ignore */ }
      if (mode === "none" || !target.trim()) {
        if (syncNowSt) {
          syncNowSt.textContent = "✗ 先在上面选一种方式并填目标";
          syncNowSt.style.color = "#c00";
        }
        return;
      }
      if (syncNowSt) { syncNowSt.textContent = "同步中…"; syncNowSt.style.color = "#888"; }
      try {
        const r = await KB.request("POST", "/kb-sync", { mode, target });
        if (syncNowSt) {
          syncNowSt.textContent = (r && r.ok ? "✓ " : "✗ ")
            + ((r && (r.message || r.error)) || "未知结果");
          syncNowSt.style.color = r && r.ok ? "#0a0" : "#c00";
        }
      } catch (e) {
        if (syncNowSt) { syncNowSt.textContent = "✗ " + e; syncNowSt.style.color = "#c00"; }
      }
    });
  }

  // 2f) 运行环境（项目目录 / Python / Ollama）
  //
  // 设计要点：三个「浏览…」按钮直接弹系统目录/文件选择框，**不让用户手打路径**
  // —— 手打路径是这类设置最容易出错的地方（反斜杠、拼写、指到父目录）。
  kbInitEnvSection(doc, root, save);

  // 3) 版本号
  //
  // ⚠ 用户反馈"设置中的版本号还是横线，没有显示"。
  //   原来的写法直接读 `Zotero.ZoteroKB.version`，那个值来自 startup 的参数，
  //   一旦插件是通过"运行中重载/更新"起来的、或对象还没挂好，它就是 null
  //   → 显示成"—"。
  //   更可靠的来源是 **AddonManager**（插件管理器显示的就是它）：
  //   它读 manifest，任何加载路径下都准。所以改成"先问 AddonManager，
  //   拿不到再退回对象属性"。
  const verEl = doc.getElementById("zotero-kb-version");
  if (verEl) {
    const setVer = (v) => { if (v) verEl.textContent = String(v); };
    try {
      const me = Zotero.ZoteroKB;
      if (me && me.version) setVer(me.version);
    } catch (e) { /* ignore */ }
    kbDiag("版本号：初值=" + JSON.stringify(verEl.textContent));
    try {
      const { AddonManager } = ChromeUtils.importESModule(
        "resource://gre/modules/AddonManager.sys.mjs");
      const id = (Zotero.ZoteroKB && Zotero.ZoteroKB.id) || "zotero-kb@authentic3096.github.io";
      kbDiag("  AddonManager 导入成功，查 id=" + id);
      Promise.resolve(AddonManager.getAddonByID(id)).then((addon) => {
        kbDiag("  getAddonByID 返回：" + (addon
            ? ("version=" + addon.version) : "null"));
        if (addon && addon.version) setVer(addon.version);
        if (verEl.textContent === "—") {
          // 两条路都拿不到 —— 说清而不是留个横线让人猜
          verEl.textContent = "（读不到，见下方诊断）";
        }
      }).catch(() => {
        if (verEl.textContent === "—") verEl.textContent = "（读不到）";
      });
    } catch (e) {
      Zotero.debug("[zotero-kb] 读插件版本失败：" + e);
      if (verEl.textContent === "—") verEl.textContent = "（读不到）";
    }
  }

  // 4) 测试连接
  const btn = doc.getElementById("zotero-kb-test-btn");
  const status = doc.getElementById("zotero-kb-test-status");
  if (btn && status) {
    btn.addEventListener("click", async () => {
      root.querySelectorAll("[data-pref]").forEach(save);
      status.textContent = "测试中…";
      status.style.color = "#888";
      try {
        const ok = await Zotero.ZoteroKB.healthCheck();
        if (ok) {
          const info = Zotero.ZoteroKB.serverInfo || {};
          status.textContent = "✓ 连接成功（" + (info.service || "zotero-kb")
            + " v" + (info.version || "?") + "）";
          status.style.color = "#0a0";
        } else {
          status.textContent = "✗ 连不上：检查服务是否启动、地址与 token";
          status.style.color = "#c00";
        }
        Zotero.ZoteroKB.writeStatusFile();
      } catch (e) {
        status.textContent = "✗ 出错：" + e;
        status.style.color = "#c00";
      }
    });
  }

  // 5) 诊断信息
  const diag = doc.getElementById("zotero-kb-diag");
  if (diag) {
    try {
      const s = Zotero.ZoteroKB.serverInfo || {};
      diag.textContent = "知识库目录：" + (s.kb_dir || "(未连接)")
        + "\nZotero " + Zotero.version + " / Firefox " + Services.appinfo.platformVersion;
    } catch (e) { /* ignore */ }
  }
}

// Zotero 加载 scripts 时会执行本文件。
//
// ⚠⚠ 这里**绝对不能**用 `Zotero.getMainWindow().document`！
//    面板的 XHTML 被插进的是**设置窗口**（`zotero:pref`）的 document，
//    不是主窗口的 —— 两个不同的 document。
//
//    本机为此白折腾了三轮。这个文件末尾原来写的是：
//        const w = Zotero.getMainWindow();
//        kbInitPane(w.document);
//    于是 `doc.getElementById("zotero-kb-settings")` 永远返回 null，
//    `kbInitPane` 第二行就 return —— **整个初始化从来没跑过**。
//    表现是"模型下拉空白、版本号是横线"，而我把 pref 键名、createElement
//    全改了一遍都没用，因为那些代码根本没被执行。
//
//    诊断日志（settings-init.log）一眼就指出了：
//        kbInitPane: root=false
//          找不到 #zotero-kb-settings，直接返回
//
//    正确做法：`settings.js` 是 Zotero 在**设置窗口的沙箱**里加载的
//    （preferences.js 里 `new Cu.Sandbox(window, {...})` +
//     `Services.scriptloader.loadSubScript(script, pane.scope)`），
//    所以这个文件里的全局 `document` 就是设置窗口的 document。
//    下面还留了 `Services.wm` 兜底，并逐项写诊断日志。
function kbFindPaneDoc() {
  // 1) 沙箱里的 document（正常情况下就是它）
  try {
    if (typeof document !== "undefined" && document
        && document.getElementById("zotero-kb-settings")) {
      return { doc: document, how: "沙箱全局 document" };
    }
  } catch (e) { /* 继续找 */ }
  // 2) 枚举设置窗口（兜底）
  try {
    const en = Services.wm.getEnumerator("zotero:pref");
    while (en.hasMoreElements()) {
      const w = en.getNext();
      if (w && w.document
          && w.document.getElementById("zotero-kb-settings")) {
        return { doc: w.document, how: "Services.wm 找到的设置窗口" };
      }
    }
  } catch (e) { /* 继续 */ }
  return { doc: null, how: "没找到" };
}

if (typeof Zotero !== "undefined") {
  try {
    kbDiagReset();
    let tries = 0;
    const attempt = () => {
      tries++;
      const found = kbFindPaneDoc();
      let inMainWin = "?";
      try {
        const mw = Zotero.getMainWindow();
        inMainWin = String(!!(mw && mw.document
          && mw.document.getElementById("zotero-kb-settings")));
      } catch (e) { /* ignore */ }
      kbDiag("attempt #" + tries + "：" + found.how
             + "；主窗口里有吗=" + inMainWin);
      if (!found.doc) {
        // 面板可能还没插好：300ms 起、每次翻倍，最多约 5 秒
        if (tries < 5) setTimeout(attempt, 300 * tries);
        else kbDiag("放弃：试了 " + tries + " 次都没找到设置窗口");
        return;
      }
      Promise.resolve()
        .then(() => kbInitPane(found.doc))
        .catch((e) => kbDiag("kbInitPane 抛错：" + e));
    };
    setTimeout(attempt, 200);
  } catch (e) {
    try { Zotero.debug("[zotero-kb] 设置面板入口出错：" + e); } catch (e2) {}
  }
}
