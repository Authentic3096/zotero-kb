/**
 * 23-admin.js —— 管理面板搬进 Zotero 的**宿主半**（范围 B 第一波，W1）
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 *
 * ## 这一层干什么
 *
 * 一份前端（`content/admin.html` + `admin.js` + `admin-pages/*.js`）两个入口，
 * 本文件只负责 **Zotero 内的那个入口**：
 *   ① 按需拉起**无端口**的引擎子进程（`online/engine.py`，stdin/stdout 上的 NDJSON-RPC）；
 *   ② 把页面的 `postMessage` 转成引擎 RPC，再把结果转回页面；
 *   ③ 工具菜单里的一项「知识库管理…」→ 带 `<browser>` 的 Zotero 窗口。
 *
 * 设计契约见开发说明（尤其 §1/§4/§6/§7）。**不监听任何 TCP** ——
 * 页面 ↔ 宿主 ↔ 引擎全在父子进程的管道里，没有 token、没有 `Origin` 例外。
 *
 * ## ⚠⚠ 页面层的三个硬结论（2026-10-13 在**活着的 Zotero** 里用 dispatch_js 逐条实测）
 *
 * 这三条是本文件两次真机返工换来的，**改这一层之前先读完**：
 *
 * ① **页面必须跑在父进程里。** `Zotero.openInViewer(管理页面地址)` 是错的：
 *    basicViewer 自带的 `<browser>` 带 **`maychangeremoteness="true"`**，于是
 *    `resource://zotero-kb/content/admin.html` 被切进**独立进程**——
 *    实测 `b.isRemoteBrowser=true`、`b.contentWindow=null`、`b.contentDocument=null`。
 *    整套桥（装钩子 / 读 `wrappedJSObject` / 回包）都要求同进程，所以什么都做不了
 *    （上一版拿到的 `contentWindow` 只是导航前的 about:blank，钩子装在了空窗口上）。
 *   正解：`openInViewer("about:blank#zotero-kb-admin")` 开一个**壳窗口**，然后
 *   用 `kbAdminMountFrame` 在它里面**自建**一个 `<browser>`（**不设** maychangeremoteness）
 *    把页面加载进去 —— 实测 `isRemoteBrowser=false`、`contentWindow` 正常、页面能渲染。
 *    对照：`20-kbview.js` 自建的 browser 本来就没有那个属性，一直是同进程的。
 *
 * ② **这类 `<browser>` 的内容文档 `window.parent === window`（实测 `parentIsSelf:true`，
 *    且 `browsingContext.isTopLevel:false`）**。而 `content/admin.js::call()` 用的是
 *    `window.parent.postMessage(...)` —— 于是请求**先被页面自己的 message 监听吃到**：
 *    那个 seq 从 `pending` 里被删、请求里没有 `ok`，当场
 *    `reject(new Error("宿主半返回失败"))`（用户看到的就是这一句，不是 30 秒超时那句）。
 *    正解：在页面 window 上**替换 `postMessage`**（`kbAdminInstallHookOn`）——
 *    `{__kba:1, op}` 形状的请求直接交给宿主、**不再派发**；其余原样放行。
 *    （`content/kbview.js` 第 76 行 `window.parent !== window` 的判据在这里永远是 false，
 *    所以它页面→宿主走的是宿主注入的 `__kbvHostPost`，不是 postMessage。）
 *
 * ③ **回包必须用 WindowProxy 上的内建 `postMessage`，不能用 `wrappedJSObject` 上那个。**
 *    实测从 chrome 用后者发对象会 `DataCloneError: The object could not be cloned.`
 *    （Xray 包装过的 chrome 对象不可结构化克隆）；用 `browser.contentWindow.postMessage`
 *    （WindowProxy 的 WebIDL 方法）则正常。另外：页面里 `window.KBAdmin` 这类
 *    **expando 在 Xray 上看不到**（`cw.KBAdmin === undefined`），必须走
 *    `cw.wrappedJSObject.KBAdmin` —— 探针里读页面对象时记住这一条。
 *
 * 页面的窗口一律**按 URL 认**（`kbAdminEnsureDocObserver`：观察 `document-element-inserted`，
 * 在页面脚本跑之前就把钩子装上），不再靠 `querySelector("browser")` 猜是哪一个。
 *
 * ## ⚠ 关键探明结论：Zotero 的 Subprocess **拿得到 stdout 管道**（不是"只能拿退出码"）
 *
 * 计划文档 §7 风险 1 与 `14-menus.js::runPython` 的注释都写着"Subprocess 只能拿退出码、
 * stdout 是 ignore，所以 stdio 引擎可能做不了"。**直接读已装 Zotero 的实现**后确认：
 * 那句是从"`proc.stdout` 不是 Web Stream（没有 getReader）"推出来的，而**前提是错的**：
 *
 *   · `omni.ja!/modules/Subprocess.sys.mjs`（Zotero 10 / Gecko 140）只处理
 *     `options.stderr`，**根本没有 `options.stdout` 这个选项** ——
 *     传 `stdout: "ignore"` 是被**静默忽略**的，不会改变任何行为。
 *   · 真正的创建在 `omni.ja!/modules/subprocess/subprocess_win.worker.js::initPipes({stderr})`：
 *     `their_pipes[0] = pipe(false)`（子进程 stdin，我们写）与
 *     `their_pipes[1] = pipe(true)`（子进程 stdout，**我们读**）**永远都建管道**，
 *     `stderr` 这个选项只决定第三条（`"pipe"` 单独管道 / `"stdout"` 并进 stdout /
 *     其它则继承父进程的 stderr）。
 *   · 因此 `Subprocess.call(...)` 的返回值上 `proc.stdout` 是一个 **`InputPipe`**，
 *     有 `read() / readString() / readJSON() / readUint32()`（见 `subprocess_common.sys.mjs`
 *     的 `BaseProcess` 构造函数：`fds[1] !== undefined` 就挂 `this.stdout`）。
 *
 * 结论：**管道可用，stdio 引擎这条路成立**。原来那句"没有 getReader 所以读不了"只说明
 * 不能用 Web Stream 的写法，正确的读法是 `await proc.stdout.readString()`（流式解码，
 * 读到 EOF 返回空串）。本项目实测也确认 `pythonw.exe` 在**双管道**下 `sys.stdin/stdout`
 * 都不是 None（`print(repr(sys.stdout is None))` → `False`）。
 *
 * ## ⚠ 传输路径：只剩两条（2026-10-13 W2 删掉中间的"结果写文件"那条）
 *
 *   ① **长驻管道**（首选）：`kbAdminStartEngine()` 拉起一次，之后每次请求写一行 NDJSON；
 *   ② **短进程 + 单行管道**（退化）：拿不到管道时，一个请求起一次引擎、写一行、读一行。
 *
 *   原来还有一条夹在中间的 ③"短进程 + 结果写文件"（契约形态
 *   `--once --request <json> --response <json>`）。**它已删除**，理由：
 *     · `engine.py` 从来没实现过 `--once` —— 实测那条命令退出码 0、**不写响应文件**，
 *       所以它 100% 会抛"引擎没写出响应文件"，是个**永远不成功的尝试**；
 *     · 每次都要多一次临时文件往返，却不比 ② 多任何能力（同样是一个请求一个进程）；
 *     · 而 ① 与 ② **都已在真机实测可用**（见上面那段探明结论）。
 *   留一条必然失败的路径只会让真正的故障被它盖住（报错指向"没写响应文件"，
 *   而根因可能在别处），所以连同它的注释一起删掉，不留提醒性的死代码。
 *
 * ## 页面协议（`content/admin.js` 已按这个写好，**不要改前端**）
 *
 *   页面 → 宿主：`{__kba:1, seq, op, args}`（`admin.js::call`）
 *   宿主 → 页面：`{__kba:1, seq, ok, data|error}`
 *   `op` 目前只有一个：`"http"`，`args = {method, path, body}`，由 `KB_ADMIN_ROUTES`
 *   映射成引擎方法（未知路径回 `ok:false` 并说明白，不静默）。
 *
 *   `KB_ADMIN_ROUTES` 的**当前覆盖**（W1 状态/结构 + W2 批次 A 五页 + 批次 B 四页）：
 *
 *     W1   GET /health · GET /status · GET /kb-status · GET /kb-structure ·
 *          GET /jobs · GET /jobs/<id> · POST /reindex · POST /digest
 *     W2A  经验库：GET /experience · GET /experience/<id> · POST /experience ·
 *          POST /experience/delete · POST /pending/mark ·
 *          GET /sessions · POST /learn
 *          分节纲要：GET /outline-list · POST /outline/render · POST /outline/delete
 *          元数据：  POST /metafill-scan · POST /metafill-scan-cancel ·
 *          POST /typefix-scan
 *          运行环境：GET /env-check · POST /env-config · GET|POST /llm-config ·
 *          GET /models · POST /llm-test（真发一次请求的「测试连接」）·
 *          POST /service/restart
 *          提示词：  GET /prompts · POST /prompts · POST /prompts/probe
 *     W2B  分类建议：GET /papers · POST /summarize · POST /classify ·
 *          POST /apply-suggestion · POST /maintain
 *          PDF 解析：GET /mineru-status · POST /mineru/parse · POST /mineru/clear
 *          损坏查询：GET /quality-stats · POST /quality-scan ·
 *          POST /quality-repair · GET /quality-detail · GET|POST /config/figures
 *          高级：    POST /search · POST /weight · GET /weight-list ·
 *          GET /collections · POST /zotero-sync · POST /plugin ·
 *          POST /zotero-upgrade
 *
 *   契约（方法名/参数/返回形状）见开发说明（批次 A）与
 *   开发说明（批次 B）—— **两边都要改**：
 *   引擎加 method、这里加一行。`GET /quality-detail?key=` 这类查询串由精确
 *   匹配统一抽参数，不用另写带参分支。
 *
 * ⚠ **进度事件：页面壳已经能收，宿主这一侧还没转发**（2026-10-13 更新）。
 *   `content/admin.js` 现在先判 `d.event === "progress"` 再走 `onProgress`，
 *   不会再把同 seq 的进度当失败响应打掉。**但本文件仍只把进度记进日志与
 *   状态文件**（见 `kbAdminForwardProgress` 的注释：转发要把页面 seq 与 browser
 *   一路带进 pending 槽，本轮没做）。长任务页面走 `runTask` 轮询 `/jobs/<id>`，
 *   进度从 `job.log` 里照样拿得到 —— 而且长任务现在**一律"派发即返回 + 轮询"**
 *   （`kb.digest` / `kb.reindex` 也已改成这个形状，见 engine.py::_job_dispatch），
 *   所以"实时逐行"本来就不需要：这是**有意不做**，别当成漏了。
 *
 * ⚠ **超时**：页面 `get/post` 的第 4 个参数只约束**页面自己**的等待时间，
 *   管不到宿主。宿主侧默认 180s（`timeoutMs`），慢方法用 `KB_ADMIN_TIMEOUTS`
 *   单独放宽（目前只有 `prompts.probe` → 300s：它内部模型超时就是 240s，
 *   用 180s 会被宿主提前掐掉）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 路由表
  //
  // 页面只会说"HTTP 语义"（路径 + 方法），这里换成引擎方法名。
  // 为什么不把这张表塞进函数体：**换引擎方法时只改这一处**，而且"哪些端点
  // W1 已经做了"一眼可见（未知路径会被明确拒绝，不是静默 404）。
  //
  // `from` 决定参数从哪来：query 串（GET）还是请求体（POST）。
  KB_ADMIN_ROUTES: {
    // 页面顶部那句话（admin.js::boot 用 /health）与状态卡
    "GET /health": { method: "kb.health", from: "query" },
    // 知识库位置：老 HTTP 服务停着也要能用（原来只有 8765 上那条）
    "GET /kb-location": { method: "kb.location", from: "query" },
    "POST /kb-location": { method: "kb.locationMigrate", from: "body" },
    "GET /status": { method: "kb.status", from: "query" },
    // ⚠ 状态页用的是 `/kb-status`（Tk 面板的口径），与上面的 /status 同义。
    //   两个都留着：老前端/新前端都不用改。
    "GET /kb-status": { method: "kb.status", from: "query" },
    "GET /kb-structure": { method: "kb.structure", from: "query" },
    "GET /jobs": { method: "jobs.list", from: "query" },
    "POST /reindex": { method: "kb.reindex", from: "body" },
    "POST /digest": { method: "kb.digest", from: "body" },
    // W3 线2.5（2026-10-14）：右键菜单「停止生成纲要」/ 侧栏正文修订 / 公式纠错。
    // ⚠ 这几条都是"老 HTTP 已有、引擎侧新补"的一对一映射，与老 `/formula-*` 并存。
    "POST /digest/cancel": { method: "kb.digestCancel", from: "body" },
    "GET /doc-edit": { method: "doc.get", from: "query" },
    "POST /doc-edit": { method: "doc.save", from: "body" },
    "GET /formula-target": { method: "formula.target", from: "query" },
    "POST /formula-apply": { method: "formula.apply", from: "body" },
    "POST /formula-mathtype": { method: "formula.mathtype", from: "body" },
    // `GET /jobs/<id>` 是**带参路径**，在 kbAdminMapRoute 里单独匹配（见那里）。

    // ---------------------------------------------------------- W2 批次 A
    //
    // 方法名与开发说明里的那张表一一对应（契约冻结，不要自己发明）。
    // 经验库（`GET /experience/<id>` 也是带参路径，同样在 kbAdminMapRoute 里匹配）
    "GET /experience": { method: "experience.list", from: "query" },
    "POST /experience": { method: "experience.save", from: "body" },
    "POST /experience/delete": { method: "experience.delete", from: "body" },
    "POST /pending/mark": { method: "pending.mark", from: "body" },
    // 「选择会话…」弹窗 + 从勾选的会话里抽经验（后者是长任务，走 /jobs/<id>）
    "GET /sessions": { method: "sessions.list", from: "query" },
    "POST /learn": { method: "learn.scan", from: "body" },
    // 分节纲要（`/digest` 已在上面，长任务走 /jobs/<id> 轮询）
    "GET /outline-list": { method: "outline.list", from: "query" },
    "POST /outline/render": { method: "outline.render", from: "body" },
    "POST /outline/delete": { method: "outline.delete", from: "body" },
    // 元数据（补全 / 类型污染）
    "POST /metafill-scan": { method: "meta.fillScan", from: "body" },
    "POST /metafill-scan-cancel": { method: "meta.fillCancel", from: "body" },
    "POST /typefix-scan": { method: "meta.typeScan", from: "body" },
    // 运行环境（/health 复用上面那条）
    "GET /env-check": { method: "env.check", from: "query" },
    // W3 线2.5（2026-10-14）：插件设置面板要的是**诊断原文**（= env_report() 那个形状，
    // 带 python / project_root / runtime_state 顶层键）；管理面板仍用上面那个重整形过的。
    "GET /env-check-raw": { method: "env.checkRaw", from: "query" },
    // W3 线2.5：**内联元数据**的分类建议（新文献还没入库时用）。
    // ⚠ 与 `POST /classify`（→ ai.classify，认 key）是**两个能力**，并存不冲突。
    "POST /classify-item": { method: "classify.item", from: "body" },
    "POST /env-config": { method: "env.save", from: "body" },
    "GET /llm-config": { method: "llm.config", from: "query" },
    "POST /llm-config": { method: "llm.save", from: "body" },
    "GET /models": { method: "llm.models", from: "query" },
    // 「测试连接」：**真发一次请求**（本地查 ollama list；外部 API 真调一次
    // /chat/completions）—— 慢，超时在 KB_ADMIN_TIMEOUTS 里单独放宽到 90s。
    "POST /llm-test": { method: "llm.test", from: "body" },
    // 「模型目录」三层兜底：本机 /api/tags、外部 /models、失败回落内置候选。
    // 引擎侧自己只等 12s（`discover_llm` 的 timeout 参数），这里给 30s。
    "POST /llm-discover": { method: "llm.discover", from: "body" },
    "POST /service/restart": { method: "service.restart", from: "body" },
    // 提示词
    "GET /prompts": { method: "prompts.list", from: "query" },
    "POST /prompts": { method: "prompts.save", from: "body" },
    "POST /prompts/probe": { method: "prompts.probe", from: "body" },

    // ---------------------------------------------------------- W2 批次 B
    //
    // 方法名与开发说明里的那张表一一对应（契约冻结，不要自己发明）。
    // `GET /quality-detail?key=`、`GET /mineru-status` 这些**查询串已被上面的
    // 精确匹配统一抽成 params**（kbAdminMapRoute 会解析 `?a=b` 再交给引擎），
    // 所以不需要另写带参匹配分支。
    // 分类建议
    "GET /papers": { method: "papers.list", from: "query" },
    "POST /summarize": { method: "ai.summarize", from: "body" },
    "POST /classify": { method: "ai.classify", from: "body" },
    "POST /apply-suggestion": { method: "ai.apply", from: "body" },
    "POST /maintain": { method: "maintain.run", from: "body" },
    // PDF 解析
    "GET /mineru-status": { method: "mineru.status", from: "query" },
    // W3 线2.5（2026-10-14）：安装引导要的**原文**（带 exe/version/vlm_engine/cuda/gpu…），
    // 与上面那个给面板的汇总形状**不是同一个东西**。
    "GET /mineru-check": { method: "mineru.checkRaw", from: "query" },
    // 单篇**同步**的元数据补全 / 类型修正（与 /metafill-scan、/typefix-scan 的长任务分工不同）。
    "POST /metafill": { method: "meta.fillOne", from: "body" },
    "POST /typefix": { method: "meta.typefixOne", from: "body" },
    "POST /mineru/parse": { method: "mineru.parse", from: "body" },
    "POST /mineru/clear": { method: "mineru.clear", from: "body" },
    // 损坏查询
    "GET /quality-stats": { method: "quality.stats", from: "query" },
    "POST /quality-scan": { method: "quality.scan", from: "body" },
    "POST /quality-repair": { method: "quality.repair", from: "body" },
    "GET /quality-detail": { method: "quality.detail", from: "query" },
    // 图注/表格开关：**GET 读当前值**（页面那个勾选框必须显示真值，不能瞎勾），
    // POST 写 —— 同一个路径两种方法，与 /llm-config 的写法一致。
    "GET /config/figures": { method: "config.figuresGet", from: "query" },
    "POST /config/figures": { method: "config.figures", from: "body" },
    // 高级（`POST /search`、`POST /weight` 批次 A 没有，本轮补上）
    "POST /search": { method: "kb.search", from: "body" },
    "POST /weight": { method: "kb.weight", from: "body" },
    // W3 线2.5：插件自身的**列表权重列**走这条（`GET /weights` = 全量映射，每篇都有数）。
    // ⚠ 别拿下面的 `/weight-list`（advanced.weightList）顶替：那个是"权重榜"，
    //   只列 `item_weight` 里有行的几篇，拿它填列会让其余全空白（历史真实踩坑）。
    "GET /weights": { method: "kb.weightsMap", from: "query" },
    // W3 线2.5：文献列表那两列（已建库 / 纲要节数）。
    // ⚠ 节数口径 = `meta.ai_outline:<key>`（读端看的），**不是数 views 里的文件个数**。
    "GET /col-status": { method: "kb.colStatus", from: "query" },
    // W3 线2.5：某几条文献在库状态（06-watch 监听 / 12-dsh 发文献给 DSH 共用）。
    "POST /item-info": { method: "kb.itemInfo", from: "body" },
    "GET /weight-list": { method: "advanced.weightList", from: "query" },
    "GET /collections": { method: "advanced.collections", from: "query" },
    "POST /zotero-sync": { method: "advanced.zoteroSync", from: "body" },
    "POST /plugin": { method: "advanced.plugin", from: "body" },
    "POST /zotero-upgrade": { method: "advanced.upgrade", from: "body" },
  },

  // ================================================================ 引擎：状态与生命周期

  /**
   * 引擎的运行时状态（**挂在一个对象里**，不要往 self 上撒几十个字段）。
   *
   *   proc        当前管道引擎的句柄（null = 没起 / 已退）
   *   mode        "pipe"（长驻）/"file"（短进程）/"none"（起不来）
   *   nextId      NDJSON-RPC 的请求 id（与页面的 seq 是两套编号，别混）
   *   pending     Map<id, {resolve,reject,timer,method}> —— 等响应的请求
   *   buffer      stdout 的**行缓冲**（chunk 不保证按行切）
   *   errTail     stderr 的尾巴（几千字符，"引擎为什么崩了"就靠它）
   *   failCount   pipe 引擎连续挂了几次（≥3 就固定走退化路径，别反复重启）
   *   forceFile   是否固定走退化路径
   *   timeoutMs   单条 RPC 的默认超时（180s）。**慢方法用它不够** —— 见
   *              `KB_ADMIN_TIMEOUTS` / `kbAdminTimeoutFor()`（按方法名放宽）。
   */
  kbAdminEngineState: function () {
    var self = ZoteroKB;
    if (!self._kbAdminEngine) {
      self._kbAdminEngine = {
        proc: null, mode: "none", starting: null, dead: false,
        nextId: 0, pending: null, buffer: "", errTail: "",
        failCount: 0, forceFile: false,
        timeoutMs: 180000, startedAt: 0, reason: "", lastProgress: "",
      };
    }
    if (!self._kbAdminEngine.pending) {
      try { self._kbAdminEngine.pending = new Map(); }
      catch (e) { self._kbAdminEngine.pending = null; }
    }
    return self._kbAdminEngine;
  },

  /**
   * 引擎的可执行文件。
   *
   * ⚠ 这里**故意用 python.exe 而不是 pythonw.exe**（`pythonConsoleExe()` 会把
   *   `pythonw.exe` 换成同目录的 `python.exe`）：
   *   · Zotero 的 Subprocess 本来就是 `CREATE_NO_WINDOW` 启动（见
   *     `subprocess_win.worker.js` 的 `processFlags`），**不会弹控制台** ——
   *     pythonw 唯一的那点好处在这里根本用不上；
   *   · 而引擎作者在 `online/engine.py` 开头明确写了"不要用 pythonw 启动"
   *     （它担心 `sys.stdout is None` 时协议直接废掉）。本机实测双管道下
   *     pythonw 的 stdin/stdout 其实不是 None，但既然没有收益，就不去赌这一点。
   */
  kbAdminEngineExe: function () {
    var self = ZoteroKB;
    try {
      const exe = self.pythonConsoleExe();
      if (exe) return exe;
    } catch (e) { /* 落到 pythonExe */ }
    return self.pythonExe();
  },

  /** 引擎脚本的绝对路径（项目根下 online/engine.py）。找不到返回空串。 */
  kbAdminEngineScript: function () {
    var self = ZoteroKB;
    const root = self.projectRoot();
    if (!root) return "";
    const p = root + "\\online\\engine.py";
    return self._exists(p) ? p : "";
  },

  /**
   * Zotero 退出时把引擎收掉（**不常驻**是用户的第一诉求之一）。
   *
   * 为什么自己挂 `quit-application` 而不改 `01-lifecycle.js::shutdown`：
   *   · 这一波只许动 23-admin / 14-menus / structure 三个文件；
   *   · `quit-application` 才是"真的要退了"（`quit-application-requested` 是可以
   *     被别的观察者取消的**询问**，`verify_bootstrap.js` 也明确不许再挂那个 topic）。
   *
   * 引擎自己是"stdin EOF 就退出"（engine.py::main），所以先关 stdin 让它体面收工，
   * 再兜一个 kill（关不掉也不能留孤儿进程）。
   */
  kbAdminEnsureQuitHook: function () {
    var self = ZoteroKB;
    if (self.kbAdminQuitObs) return;
    try {
      const obs = {
        observe: function (subject, topic) {
          if (topic !== "quit-application") return;
          try { ZoteroKB.kbAdminStop(); } catch (e) { /* 退出流程里不敢抛 */ }
        },
      };
      Services.obs.addObserver(obs, "quit-application");
      self.kbAdminQuitObs = obs;
    } catch (e) {
      Zotero.debug("[zotero-kb] 挂 quit-application 观察者失败：" + e);
    }
  },

  /** 收掉引擎进程（幂等）。返回"这次真的收了一个没有"。 */
  kbAdminStop: function () {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const proc = st.proc;
    st.proc = null;
    st.dead = true;
    if (!proc) return false;
    // ① 关 stdin：engine.py 的主循环读不到行就正常退出（体面路径）
    try {
      if (proc.stdin && typeof proc.stdin.close === "function") proc.stdin.close();
    } catch (e) { /* ignore */ }
    // ② 兜底：给它 200ms 收尾，然后强制结束（Windows 上是 TerminateProcess）
    try {
      if (proc.kill) proc.kill(200);
    } catch (e2) { Zotero.debug("[zotero-kb] 杀引擎失败：" + e2); }
    Zotero.debug("[zotero-kb] 引擎已收掉");
    return true;
  },

  /**
   * 按需拉起引擎（**幂等**：已经在跑就返回同一个句柄；正在起就复用那个 Promise）。
   *
   * 返回 Promise<proc|null>。null 表示"管道这条路走不通"，调用方应当走退化路径
   * （`kbAdminRpcOnce`），而不是把整页打不开。
   */
  kbAdminStartEngine: function () {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    if (st.proc && !st.dead) return Promise.resolve(st.proc);
    if (st.starting) return st.starting;
    st.starting = (async function () {
      try {
        const exe = self.kbAdminEngineExe();
        const script = self.kbAdminEngineScript();
        const root = self.projectRoot();
        if (!exe || !self._exists(exe) || !script || !root) {
          st.reason = "找不到引擎或 Python（项目目录/venv 还没配好？）";
          st.mode = "none";
          self.writeStatusFile({ kbAdminEngine: "FAIL: " + st.reason });
          return null;
        }
        const { Subprocess } = ChromeUtils.importESModule(
          "resource://gre/modules/Subprocess.sys.mjs");
        // ⚠ 不用传 stdout：Zotero 这版**没有** options.stdout，stdin/stdout 永远
        //   会建成管道（探明结论见文件头）。stderr 显式要 "pipe" 是为了把引擎日志
        //   拿到手（排错全靠它），并在这里起一个读泵——**不读的话 64KB 管道写满，
        //   引擎会卡死在写日志上**（这是管道编程最容易埋的雷）。
        const proc = await Subprocess.call({
          command: exe,
          arguments: ["-X", "utf8", script],
          workdir: root,
          environment: { PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
          environmentAppend: true,        // 追加到 Zotero 自己的环境上（别把 PATH 冲掉）
          stderr: "pipe",
        });
        if (!proc || !proc.stdout
            || typeof proc.stdout.readString !== "function") {
          st.reason = "拿不到 stdout 管道（proc.stdout 不是 InputPipe）";
          st.mode = "file";
          try { if (proc && proc.kill) proc.kill(0); } catch (e2) { /* ignore */ }
          self.writeStatusFile({ kbAdminEngine: "file-fallback: " + st.reason });
          return null;
        }
        st.proc = proc;
        st.mode = "pipe";
        st.dead = false;
        st.buffer = "";
        st.errTail = "";
        st.startedAt = Date.now();
        st.reason = "";
        self.kbAdminReadLoop(proc);
        self.kbAdminReadErrLoop(proc);
        self.kbAdminEnsureQuitHook();
        self.writeStatusFile({ kbAdminEngine: "pipe: pid=" + (proc.pid || "?")
                              + " exe=" + exe });
        return proc;
      } catch (e) {
        st.reason = String((e && e.message) || e);
        st.mode = "file";
        self.writeStatusFile({ kbAdminEngine: "FAIL→file: " + st.reason });
        return null;
      } finally {
        st.starting = null;
      }
    })();
    return st.starting;
  },

  /** 管道断了 / 引擎退了：把等在身上的请求全部拒绝，并决定要不要转退化路径。 */
  kbAdminEngineGone: function (proc, why) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    if (st.proc && st.proc !== proc) return;   // 已经被别人换掉了
    st.proc = null;
    st.dead = true;
    st.failCount = (st.failCount || 0) + 1;
    // 连挂三次就别再重启了：改走短进程（每次都冷启动，但至少能用）
    if (st.failCount >= 3) st.forceFile = true;
    const err = new Error("引擎进程没了（" + why + "）"
      + (st.errTail ? ("\n引擎日志尾巴：\n" + String(st.errTail).slice(-600)) : ""));
    const pend = st.pending;
    if (pend) {
      const ids = [];
      try { pend.forEach(function (v, k) { ids.push(k); }); } catch (e) { /* ignore */ }
      for (const id of ids) {
        const slot = pend.get(id);
        if (!slot) continue;
        pend.delete(id);
        try { if (slot.timer) clearTimeout(slot.timer); } catch (e2) { /* ignore */ }
        try { slot.reject(err); } catch (e3) { /* ignore */ }
      }
    }
    self.writeStatusFile({ kbAdminEngine: "gone: " + why
                          + "（failCount=" + st.failCount + "）" });
  },

  /**
   * 读 stdout 的行泵。
   *
   * ⚠ 用 `readString()`（InputPipe 的流式解码）：chunk 不保证按行切，
   *   所以必须自己维护行缓冲（st.buffer），拿到完整一行才解析。
   */
  kbAdminReadLoop: function (proc) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const consume = function () {
      let idx;
      while ((idx = st.buffer.indexOf("\n")) >= 0) {
        const line = st.buffer.slice(0, idx).replace(/\r$/, "");
        st.buffer = st.buffer.slice(idx + 1);
        if (line.trim()) self.kbAdminOnEngineLine(line);
      }
    };
    const pump = function () {
      Promise.resolve(proc.stdout.readString()).then(function (chunk) {
        if (chunk) {
          st.buffer += chunk;
          consume();
          pump();
          return;
        }
        // EOF：引擎退出了。残留的最后一行（没有换行结尾）也要处理掉。
        if (st.buffer.trim()) {
          const tail = st.buffer.replace(/\r$/, "");
          st.buffer = "";
          self.kbAdminOnEngineLine(tail);
        }
        self.kbAdminEngineGone(proc, "stdout 结束");
      }, function (e) {
        self.kbAdminEngineGone(proc, "读 stdout 失败：" + e);
      });
    };
    pump();
  },

  /** 把 stderr 的尾巴攒起来（引擎日志就是排错现场）。 */
  kbAdminReadErrLoop: function (proc) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    if (!proc.stderr || typeof proc.stderr.readString !== "function") return;
    const pump = function () {
      Promise.resolve(proc.stderr.readString()).then(function (chunk) {
        if (!chunk) return;
        st.errTail = (st.errTail + chunk).slice(-6000);
        // 只把前若干字符送进 Zotero 调试日志（整段可能很长）
        Zotero.debug("[zotero-kb] engine: " + String(chunk).slice(0, 1500));
        pump();
      }, function (e) {
        Zotero.debug("[zotero-kb] 读引擎 stderr 失败：" + e);
      });
    };
    pump();
  },

  /** 一条来自引擎的 NDJSON 行：要么是最终响应，要么是 progress 事件。 */
  kbAdminOnEngineLine: function (line) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    let msg = null;
    try { msg = JSON.parse(line); } catch (e) {
      Zotero.debug("[zotero-kb] 引擎输出了非 JSON 的一行：" + String(line).slice(0, 240));
      return;
    }
    if (!msg || msg.id == null) return;      // 协议占位（例如坏 JSON 的 id:null 回执）
    const pend = st.pending;
    if (!pend) return;
    const slot = pend.get(msg.id);
    if (!slot) return;
    if (msg.event) { self.kbAdminForwardProgress(slot, msg); return; }
    if (slot.timer) clearTimeout(slot.timer);
    pend.delete(msg.id);
    if (msg.ok) {
      try { slot.resolve(msg.data); }
      catch (e) { Zotero.debug("[zotero-kb] 处理引擎响应失败：" + e); }
    } else {
      slot.reject(new Error(String(msg.error || "引擎返回失败")));
    }
  },

  /**
   * 进度事件（`{event:"progress", data:{job,percent,message}}`）。
   *
   * ⚠ **目前只落到 Zotero 日志 + 状态文件，没有 postMessage 给页面** —— 但
   *   `content/admin.js` **已经改好**能收进度了（它先判 `d.event === "progress"`
   *   就交给 `onProgress`，不会再把同 seq 的进度当失败响应打掉）。所以差的只是
   *   宿主这一侧的转发，而转发需要知道该进度属于**页面的哪个 seq**：
   *     · 现在的 `slot` 只存了 `method`/`resolve`/`reject`，没有 seq，也没有 browser；
   *     · 要接的话得把 `d.seq`（`kbAdminOnPageMessage` 里有）与 browser 一路
   *       带进 `kbAdminRpc` → `kbAdminRpcPipe` 的 pending 槽，再在这里
   *       `self.kbAdminPost(browser, {__kba:1, seq, event:"progress", data})`。
   *   本轮（W2 批次 A 的引擎方法）**没做这一步**，而且**用户已明确这轮不做**：
   *   长任务一律"派发即返回 + 轮询 `/jobs/<id>`"，`job.log` 里本来就有每一行
   *   进度，页面 `runTask` 够用；阻塞式（W1 形状）已经不存在了，所以"实时逐行"
   *   就更不需要。**这是有意的决定，不是遗漏**（引擎 `_job_dispatch` 里也写了同一句）。
   */
  kbAdminForwardProgress: function (slot, msg) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const data = (msg && msg.data) || {};
    const text = String(data.message || data.percent || "");
    st.lastProgress = text.slice(0, 200);
    Zotero.debug("[zotero-kb] 引擎进度（" + (slot.method || "?") + "）：" + text);
    try {
      self.writeStatusFile({ kbAdminProgress: text.slice(0, 200) });
    } catch (e) { /* 进度写不进去不该影响主流程 */ }
  },

  // ================================================================ 引擎：RPC

  /**
   * 慢方法 → 宿主侧等多久（毫秒）。默认用 state.timeoutMs（180s）。
   *
   * 为什么要这张表：页面 `get/post` 的第 4 个参数（见 content/admin.js 的 call）
   * **只约束页面自己的等待**，它连 postMessage 都不带过去 —— 宿主这一侧如果
   * 还是 180s，`prompts.probe` 这种内部模型超时 240s 的请求会被宿主先掐掉，
   * 页面拿到的是"引擎 180 秒没回响应"，而它其实还在跑。
   *
   * ⚠ 只放**确实超过 180s** 的方法；其余别加（等太久只会让真卡住时更难发现）。
   *   · prompts.probe 真调模型，offline/prompts.py 自己给的是 240s → 300s。
   *   · advanced.plugin / advanced.upgrade（批次 B）会重打 xpi / 备份
   *     zotero.sqlite，契约给的就是 300s → 300s。
   *   · advanced.zoteroSync（批次 B）契约给的正好是 180s，写进来是**显式**
   *     对齐（值等于默认，不会放宽也不会收紧）。
   */
  KB_ADMIN_TIMEOUTS: {
    "prompts.probe": 300000,
    "advanced.plugin": 300000,
    "advanced.upgrade": 300000,
    "advanced.zoteroSync": 180000,
    // 引擎侧只等 45s（`llm.test` 的 timeout 参数），这里给 90s 留出余量：
    // 页面那侧的等待（第 4 个参数）也给了 90000，两边别互相掐。
    "llm.test": 90000,
    // 模型目录探测：引擎侧最多 12s（params.timeout），这里给 30s。
    "llm.discover": 30000,
  },

  /** 这条 RPC 宿主侧等多久。 */
  kbAdminTimeoutFor: function (method) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const per = self.KB_ADMIN_TIMEOUTS[String(method)];
    return per || st.timeoutMs || 180000;
  },

  /** 造一条引擎请求（id 自己递增，与页面的 seq 无关）。 */
  kbAdminNewRequest: function (method, params) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    st.nextId = (st.nextId || 0) + 1;
    return { id: st.nextId, method: String(method), params: params || {} };
  },

  /**
   * 发一条 RPC。第一优先是**长驻管道**，拿不到管道就退到"每次请求起一次短进程"。
   * 返回 Promise<data>（失败 reject，message 是人能看懂的）。
   */
  kbAdminRpc: function (engineMethod, params) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    const req = self.kbAdminNewRequest(engineMethod, params);
    if (st.forceFile) return self.kbAdminRpcOnce(req);
    return self.kbAdminStartEngine().then(function (proc) {
      if (!proc) return self.kbAdminRpcOnce(req);
      return self.kbAdminRpcPipe(proc, req).catch(function (e) {
        // 引擎正好在这次请求中途挂了：**纯读**方法用短进程重试一遍（用户不该
        // 因为一次进程崩溃就重开页面）；但**会派活 / 会写库**的方法重试一次
        // 可能做两遍（跑两遍模型、多写一条经验），所以直接把错误抛上去。
        // ⚠ 这张名单要跟着"有副作用的方法"一起长大（W2 批次 A 又加了三个）。
        if (!st.dead) throw e;
        if (req.method === "kb.reindex" || req.method === "kb.digest"
            || req.method === "meta.fillScan" || req.method === "learn.scan"
            || req.method === "experience.save") throw e;
        Zotero.debug("[zotero-kb] 管道引擎挂了，用短进程重试一次：" + req.method);
        return self.kbAdminRpcOnce(req);
      });
    });
  },

  /** 管道模式：写一行请求，等同 id 的最终响应。 */
  kbAdminRpcPipe: function (proc, req) {
    var self = ZoteroKB;
    const st = self.kbAdminEngineState();
    return new Promise(function (resolve, reject) {
      const pend = st.pending;
      if (!pend) { reject(new Error("内部状态丢了（pending）")); return; }
      let timer = null;
      if (typeof setTimeout === "function") {
        const waitMs = self.kbAdminTimeoutFor(req.method);
        timer = setTimeout(function () {
          pend.delete(req.id);
          reject(new Error("引擎 " + Math.round(waitMs / 1000)
            + " 秒没回响应（" + req.method + "）"));
        }, waitMs);
      }
      pend.set(req.id, { resolve: resolve, reject: reject, timer: timer,
                         method: req.method });
      let line = "";
      try { line = JSON.stringify(req) + "\n"; }
      catch (e) {
        if (timer) clearTimeout(timer);
        pend.delete(req.id);
        reject(e);
        return;
      }
      try {
        const w = proc.stdin.write(line);
        // OutputPipe.write 返回 Promise；失败了要接住，否则是未处理拒绝。
        if (w && typeof w.then === "function") {
          w.then(null, function (e) {
            Zotero.debug("[zotero-kb] 写引擎 stdin 失败：" + e);
          });
        }
      } catch (e2) {
        if (timer) clearTimeout(timer);
        pend.delete(req.id);
        reject(e2);
      }
    });
  },

  /**
   * 退化路径入口：拿不到长驻管道时，**短进程 + 单行管道**兜底。
   *
   * ⚠ 这里原来有"两层"：先试"短进程 + 结果写文件"（契约形态
   *   `--once --request <json> --response <json>`），失败了才落到单行管道。
   *   **那一层已删除**（2026-10-13 W2）：`engine.py` 从来没实现 `--once`，
   *   实测那条命令退出码 0、**不写响应文件**，所以第一层 100% 抛"引擎没写出
   *   响应文件"—— 它除了每次多一次临时文件往返、还把真正的故障信息盖住之外，
   *   没有任何作用。留下短进程管道这条（真机实测可用），退化链更短也更好查。
   */
  kbAdminRpcOnce: async function (req) {
    var self = ZoteroKB;
    return await self.kbAdminRpcOncePipe(req);
  },

  /**
   * 短进程 + 单行管道：起一个引擎、写一行请求、读到同 id 的响应、关掉 stdin 让它退出。
   * 这是**今天就能用**的退化形态（引擎不需要任何新参数）。
   */
  kbAdminRpcOncePipe: async function (req) {
    var self = ZoteroKB;
    const exe = self.kbAdminEngineExe();
    const script = self.kbAdminEngineScript();
    const root = self.projectRoot();
    if (!exe || !script || !root) throw new Error("找不到引擎或 Python");
    const { Subprocess } = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs");
    const proc = await Subprocess.call({
      command: exe,
      arguments: ["-X", "utf8", script],
      workdir: root,
      environment: { PYTHONIOENCODING: "utf-8" },
      environmentAppend: true,
      stderr: "pipe",
    });
    if (!proc || !proc.stdout || typeof proc.stdout.readString !== "function") {
      try { if (proc && proc.kill) proc.kill(0); } catch (e) { /* ignore */ }
      throw new Error("短进程也拿不到 stdout 管道");
    }
    const errBox = { text: "" };
    self.kbAdminDrain(proc.stderr, errBox);
    try {
      const w = proc.stdin.write(JSON.stringify(req) + "\n");
      if (w && typeof w.then === "function") w.then(null, function () {});
    } catch (e2) {
      try { proc.kill(0); } catch (e3) { /* ignore */ }
      throw e2;
    }
    const waitMs = self.kbAdminTimeoutFor(req.method);
    const deadline = Date.now() + waitMs;
    let buf = "";
    for (;;) {
      if (Date.now() > deadline) {
        try { proc.kill(0); } catch (e4) { /* ignore */ }
        throw new Error("短进程 " + Math.round(waitMs / 1000)
          + " 秒没回响应（" + req.method + "）"
          + (errBox.text ? ("\n引擎日志：\n" + String(errBox.text).slice(-600)) : ""));
      }
      let chunk = "";
      try { chunk = await proc.stdout.readString(); }
      catch (e5) { chunk = ""; }
      if (!chunk) break;                       // EOF
      buf += chunk;
      let idx;
      let hit = null;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).replace(/\r$/, "");
        buf = buf.slice(idx + 1);
        let m = null;
        try { m = JSON.parse(line); } catch (e6) { m = null; }
        if (m && m.id === req.id) { hit = m; break; }
      }
      if (hit) {
        try {
          if (proc.stdin && proc.stdin.close) proc.stdin.close();
        } catch (e7) { /* ignore */ }
        try { proc.kill(200); } catch (e8) { /* ignore */ }
        if (hit.ok) return hit.data;
        throw new Error(String(hit.error || "引擎返回失败"));
      }
    }
    try { proc.kill(0); } catch (e9) { /* ignore */ }
    throw new Error("短进程在响应前就退出了（" + req.method + "）"
      + (errBox.text ? ("\n引擎日志：\n" + String(errBox.text).slice(-600)) : ""));
  },

  /** 把一条管道读到 EOF，内容攒进 box.text（用于短进程的 stderr/诊断）。 */
  kbAdminDrain: function (pipe, box) {
    if (!pipe || typeof pipe.readString !== "function") return;
    const pump = function () {
      Promise.resolve(pipe.readString()).then(function (chunk) {
        if (!chunk) return;
        box.text = (box.text + chunk).slice(-6000);
        pump();
      }, function (e) {
        Zotero.debug("[zotero-kb] 读管道失败：" + e);
      });
    };
    pump();
  },

  // ⓘ 原来这里还有一个 `kbAdminTempDir()`（给"结果写文件"那条路径准备临时目录）。
  //   那条路径 2026-10-13 已删除（engine.py 从来没有 `--once`），临时目录也就
  //   没有使用方了 —— 一并删掉，不留没人调的成员。

  // ================================================================ 路由与派发

  /** `a=1&b=2` → `{a:"1", b:"2"}`（自己写：沙箱里 URLSearchParams 不保证有）。 */
  kbAdminParseQuery: function (query) {
    const out = {};
    const text = String(query == null ? "" : query);
    if (!text) return out;
    for (const part of text.split("&")) {
      if (!part) continue;
      const eq = part.indexOf("=");
      const k = eq < 0 ? part : part.slice(0, eq);
      const v = eq < 0 ? "" : part.slice(eq + 1);
      let key = k, val = v;
      try { key = decodeURIComponent(k); } catch (e) { /* 用原文 */ }
      try { val = decodeURIComponent(v.replace(/\+/g, " ")); } catch (e2) { /* 用原文 */ }
      out[key] = val;
    }
    return out;
  },

  /**
   * 页面说的"HTTP 请求" → 引擎方法 + 参数。返回 `{method, params}` 或 null（未知）。
   *
   * ⚠ 未知路径**不猜**：调用方会回一条"W1 还没有这条映射"的 `ok:false`，
   *   页面上看得见、日志里也看得见（比静默 404 好）。
   */
  kbAdminMapRoute: function (method, path, body) {
    var self = ZoteroKB;
    const m = String(method || "GET").toUpperCase();
    const raw = String(path == null ? "" : path);
    const qi = raw.indexOf("?");
    const query = qi < 0 ? "" : raw.slice(qi + 1);
    let only = qi < 0 ? raw : raw.slice(0, qi);
    if (only && only.charAt(0) !== "/") only = "/" + only;
    const hit = self.KB_ADMIN_ROUTES[m + " " + only];
    if (hit) {
      if (hit.from === "body") {
        return { method: hit.method,
                 params: (body && typeof body === "object") ? body : {} };
      }
      return { method: hit.method, params: self.kbAdminParseQuery(query) };
    }
    // 带参路径：GET /jobs/<id> → jobs.get（id 从**路径**取，不从 query）
    const jobMatch = /^\/jobs\/([^\/]+)$/.exec(only);
    if (jobMatch && m === "GET") {
      let id = jobMatch[1];
      try { id = decodeURIComponent(id); } catch (e) { /* 用原文 */ }
      const params = self.kbAdminParseQuery(query);
      params.id = id;
      return { method: "jobs.get", params: params };
    }
    // 带参路径：GET /experience/<id> → experience.get（同上，id 从路径取）。
    // ⚠ 必须在 `GET /experience`（精确匹配）之后 —— 精确匹配先命中就走了，
    //   这条只接 `/experience/12` 这种；顺序写反不会出错（键名不同），
    //   但读起来容易误解，所以留这句。
    const expMatch = /^\/experience\/([^\/]+)$/.exec(only);
    if (expMatch && m === "GET") {
      let id = expMatch[1];
      try { id = decodeURIComponent(id); } catch (e) { /* 用原文 */ }
      return { method: "experience.get", params: { id: id } };
    }
    return null;
  },

  /** 页面的一条 `op:"http"` 请求 → `{ok, data|error}`。 */
  kbAdminHandleRequest: async function (browser, msg) {
    var self = ZoteroKB;
    if (msg.op !== "http") {
      return { ok: false, error: "宿主半不认识这个 op：" + String(msg.op) };
    }
    const args = msg.args || {};
    const route = self.kbAdminMapRoute(args.method, args.path, args.body);
    if (!route) {
      return { ok: false,
               error: "W1 还没有这条路径的映射：" + String(args.method || "GET")
                 + " " + String(args.path || "")
                 + "（映射表在 src/23-admin.js 的 KB_ADMIN_ROUTES）" };
    }
    try {
      const data = await self.kbAdminRpc(route.method, route.params);
      return { ok: true, data: data };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },

  // ================================================================ 页面窗口与桥

  /** 管理页面的地址（必须走 resource://，理由见 20-kbview.js 的 KB_RES_KEY）。 */
  kbAdminPageURL: function () {
    var self = ZoteroKB;
    const key = self.KB_RES_KEY || "zotero-kb";
    const rel = "content/admin.html";
    return "resource://" + key + "/" + rel;
  },

  /** 已经开着的管理窗口（Zotero 用 `viewerOriginalURI` 认，见 Zotero.openInViewer）。 */
  kbAdminFindWindow: function (url) {
    try {
      const e = Services.wm.getEnumerator("zotero:basicViewer");
      while (e.hasMoreElements()) {
        const w = e.getNext();
        try { if (w.viewerOriginalURI === url) return w; } catch (err) { /* 下一个 */ }
      }
    } catch (e2) { /* 拿不到窗口枚举就算了 */ }
    return null;
  },

  /**
   * 我们那个"壳窗口"的地址（**不是**管理页面的地址）。
   *
   * ⚠ 为什么不让 `Zotero.openInViewer` 直接加载管理页面 —— 第一版就是这么做的，也因此
   *   彻底失败。真机实测（2026-10-13，Zotero 10.0.6 / Gecko 140）：
   *     basicViewer 自带的那个 `<browser>` 带 **`maychangeremoteness="true"`**，
   *     于是 `resource://zotero-kb/content/admin.html` 被切到**独立进程**：
   *       `b.isRemoteBrowser = true`、`b.contentWindow = null`、`b.contentDocument = null`，
   *     而我们的整套桥（装 postMessage 钩子 / 读 `wrappedJSObject` / 回包）全都要求页面
   *     **在父进程里**。所以之前 `contentWindow` 只能读到导航前的 about:blank，
   *     钩子装在了那个空窗口上（`kbAdminProbe` 里 `url:"about:blank"` 就是这么来的）。
   *   对照：侧栏 `20-kbview.js` 自建的那个 `<browser>` **没有** maychangeremoteness，
   *   真机实测 `isRemoteBrowser=false`、`contentWindow` 正常 —— 同一台机器、同一个 scheme。
   *   因此：壳窗口只加载 about:blank，管理页面由**我们自己建的 browser** 加载
   *   （见 kbAdminMountFrame，里面绝不设 maychangeremoteness）。
   */
  kbAdminShellURI: function () { return "about:blank#zotero-kb-admin"; },

  /** 已经开着的壳窗口（按 `viewerOriginalURI` 认，与 Zotero 自己的幂等口径一致）。 */
  kbAdminFindShellWindow: function () {
    var self = ZoteroKB;
    const want = self.kbAdminShellURI();
    try {
      const e = Services.wm.getEnumerator("zotero:basicViewer");
      while (e.hasMoreElements()) {
        const w = e.getNext();
        try { if (String(w.viewerOriginalURI) === want) return w; } catch (err) { /* 下一个 */ }
      }
    } catch (e2) { /* 拿不到窗口枚举就算了 */ }
    return null;
  },

  /**
   * 在壳窗口里建**我们自己的** `<browser>` 并加载管理页面（幂等）。
   * 建的时候**绝不设 maychangeremoteness**（理由见 kbAdminShellURI）。返回那个 browser。
   */
  kbAdminMountFrame: function (win) {
    var self = ZoteroKB;
    if (!win) return null;
    let doc = null;
    try { doc = win.document; } catch (e) { doc = null; }
    if (!doc) return null;
    let frame = null;
    try { frame = doc.getElementById("zotero-kb-admin-frame"); } catch (e2) { frame = null; }
    if (frame && frame.getAttribute && frame.getAttribute("src")) return frame;
    if (!frame) {
      let host = null;
      try {
        host = doc.getElementById("appcontent") || doc.getElementById("browser")
          || doc.documentElement;
      } catch (e3) { host = null; }
      if (!host) return null;
      // 壳窗口自带那个 browser（里面是 about:blank#…）藏起来，别两页叠着
      try {
        const stock = doc.querySelector("browser");
        if (stock) {
          stock.setAttribute("hidden", "true");
          stock.setAttribute("collapsed", "true");
        }
      } catch (e4) { /* ignore */ }
      try {
        frame = doc.createXULElement
          ? doc.createXULElement("browser") : doc.createElement("browser");
      } catch (e5) { frame = null; }
      if (!frame) return null;
      frame.id = "zotero-kb-admin-frame";
      frame.setAttribute("type", "content");
      frame.setAttribute("remote", "false");
      // ⚠⚠ 这里**刻意没有** maychangeremoteness：设了它页面会跑进独立进程，
      //     宿主拿不到 contentWindow（真机实测 isRemoteBrowser=true / cw=null）。
      frame.setAttribute("disableglobalhistory", "true");
      frame.setAttribute("flex", "1");
      try {
        frame.style.setProperty("width", "100%");
        frame.style.setProperty("height", "100%");
        frame.style.setProperty("min-width", "0");
        frame.style.setProperty("min-height", "0");
      } catch (e6) { /* ignore */ }
      try { host.appendChild(frame); } catch (e7) { return null; }
      try { win.document.title = "知识库 · 管理"; } catch (e8) { /* ignore */ }
    }
    try {
      if (!frame.getAttribute("src")) frame.setAttribute("src", self.kbAdminPageURL());
    } catch (e9) { /* ignore */ }
    return frame;
  },

  /** 窗口里那个装页面的 `<browser>`（basicViewer 自带一个）。 */
  kbAdminBrowserOf: function (win) {
    try {
      const doc = win && win.document;
      if (!doc) return null;
      if (doc.querySelector) {
        const b = doc.querySelector("browser");
        if (b) return b;
      }
      const root = doc.documentElement;
      if (root && root.getElementsByTagName) {
        return root.getElementsByTagName("browser")[0] || null;
      }
    } catch (e) { /* ignore */ }
    return null;
  },

  /** 两个 window 是不是同一个（event.source 与目标窗口的比法，含 Xray 兜底）。 */
  kbAdminSameWindow: function (target, source) {
    if (!target || !source) return false;
    if (source === target) return true;
    try {
      if (source.wrappedJSObject && target.wrappedJSObject) {
        return source.wrappedJSObject === target.wrappedJSObject;
      }
    } catch (e) { /* Xray 挡了就算了 */ }
    return false;
  },

  /** 给页面发一条消息。**必须走 WindowProxy 上的内建 postMessage**（见下）。 */
  kbAdminPostTo: function (w, obj) {
    if (!w) return false;
    // ① WindowProxy 上的内建 postMessage —— 真机实测**只有这条路不抛 DataCloneError**。
    //    为什么：chrome 里的对象经 Xray 交给 content 时，用 wrappedJSObject 上那个
    //    `postMessage` 会报 "The object could not be cloned."（真机实测原话）；
    //    而 WindowProxy 上的这个是 WebIDL 方法，参数会被正常结构化克隆。
    try { w.postMessage(obj, "*"); return true; } catch (e) { /* 落兜底 */ }
    // ② 兜底：wrappedJSObject 上的（少数窗口形态下只有它可达）
    try {
      const p = w.wrappedJSObject;
      if (p && p !== w) { p.postMessage(obj, "*"); return true; }
    } catch (e2) { /* ignore */ }
    Zotero.debug("[zotero-kb] 给管理页面发消息失败（两种 postMessage 都不行）");
    return false;
  },

  /** 兼容旧签名：第一个参数可以是 window，也可以是 `<browser>`。 */
  kbAdminPost: function (browserOrWindow, obj) {
    let w = browserOrWindow;
    try {
      if (browserOrWindow && browserOrWindow.contentWindow) {
        w = browserOrWindow.contentWindow;
      }
    } catch (e) { /* 当 window 用 */ }
    return ZoteroKB.kbAdminPostTo(w, obj);
  },

  /**
   * 把页面的一条请求派发给引擎，并按**同一个 seq** 回包。
   * 三条通道（postMessage 钩子 / 页面窗口监听 / 父窗口监听）共用这一个出口。
   *
   * `target` = **执行 admin.html 的那个 window**（回包就发给它）。
   */
  kbAdminDispatch: function (target, d, channel) {
    var self = ZoteroKB;
    const seq = d.seq;
    const reply = function (res) {
      const full = { __kba: 1, seq: seq };
      if (res && res.ok) { full.ok = true; full.data = res.data; }
      else {
        full.ok = false;
        full.error = String((res && res.error) || "宿主半处理失败");
      }
      // 记下"这次是哪条通道、哪个 seq、哪个路径通的"—— 真机排查一眼就知道走了哪条路。
      try {
        self.writeStatusFile({
          kbAdminChannel: String(channel || "") + " seq=" + seq
            + " op=" + String((d.args && d.args.path) || ""),
        });
      } catch (e) { /* ignore */ }
      if (!self.kbAdminPostTo(target, full)) {
        Zotero.debug("[zotero-kb] 回不了管理页面（窗口没了？）");
      }
    };
    return Promise.resolve()
      .then(function () { return self.kbAdminHandleRequest(target, d); })
      .then(function (res) {
        reply(res || { ok: false, error: "宿主半没有返回结果" });
      }, function (e) {
        reply({ ok: false, error: String((e && e.message) || e) });
      });
  },

  /**
   * 桥的核心：**在页面自己的 window 上**装钩子/监听，并按 URL 认出"哪个 window 是
   * 管理页面"。
   *
   * ## 为什么必须装钩子（`parentIsSelf` 已由真机证实）
   *
   * `content/admin.js::call()` 用 `window.parent.postMessage(...)` 发请求；真机
   * `kbAdminProbe.parentIsSelf = true` —— 内容文档是**顶层**浏览上下文，
   * `window.parent === window`，于是请求会**先被页面自己的 message 监听吃到**：
   * 那个 seq 从 `pending` 里被删掉、因为请求里没有 `ok`，当场
   * `reject(new Error("宿主半返回失败"))`（用户看到的就是这一句，不是 30 秒超时）；
   * 宿主半后来那份真实回包永远被丢弃。
   *
   * 所以把 `{__kba:1, op}` 形状的**请求**直接交给宿主处理，并且**不再往下派发**
   * （页面自己的监听因此收不到它）；其余消息（回包 `ok/error`、progress `event`、
   * 页面里任何别的 postMessage）原样放行 —— 回包必须走原函数才能到页面监听。
   *
   * ## 上一版为什么失败（真机证据，别重犯）
   *
   * 上一版从 `Zotero.openInViewer()` 的窗口里 `document.querySelector("browser")`
   * 取 `contentWindow`，真机上拿到的是**导航前的 about:blank**
   * （`kbAdminProbe` = `{"parentIsSelf":true,"hasKBAdmin":false,"url":"about:blank"}`），
   * 钩子装在了那个空窗口上，页面真正的请求没人接（`kbAdminChannel` 一直空）。
   * 现在窗口一律**按 URL 认**（`kbAdminOnDocInserted` 在文档创建时 /  `kbAdminScanAdminWindows`
   * 在已加载时），不再依赖 browser 元素的层次。
   */
  /** 某个 window 现在的地址（读不到给空串，不抛）。 */
  kbAdminHrefOf: function (w) {
    try { return String((w && w.location && w.location.href) || ""); }
    catch (e) { return ""; }
  },

  /** 这个地址是不是我们的管理页面。 */
  kbAdminIsAdminHref: function (href) {
    var self = ZoteroKB;
    const want = self.kbAdminPageURL();
    return !!want && String(href || "").indexOf(want) >= 0;
  },

  /**
   * 在**指定 window** 的 `postMessage` 上装一道分派钩子（防自回环，见文件头的说明）。
   *
   * 与上一版的区别：参数从 `<browser>` 换成**window 本身** —— 上一版从
   * `browser.contentWindow` 取窗口，真机上取得的是**导航前的 about:blank**
   * （`kbAdminProbe` 实测 `url:"about:blank", hasKBAdmin:false`），钩子装到了错的
   * 窗口上，所以什么也没接住。现在窗口由 URL 认（见 kbAdminOnDocInserted /
   * kbAdminScanAdminWindows），装的一定是"正在执行 admin.html 的那个 window"。
   */
  kbAdminInstallHookOn: function (w) {
    var self = ZoteroKB;
    if (!w) return false;
    let page = w;
    try { if (w.wrappedJSObject) page = w.wrappedJSObject; } catch (e) { /* 用原对象 */ }
    if (!page) return false;
    let already = false;
    try { already = page.__kbAdminHooked === true; } catch (e2) { already = false; }
    if (already) return true;
    let orig = null;
    try { orig = page.postMessage; } catch (e3) { orig = null; }
    if (typeof orig !== "function") return false;
    const hook = function (data) {
      try {
        if (data && typeof data === "object" && data.__kba === 1
            && typeof data.seq === "number"
            && data.op !== undefined
            && data.ok === undefined && data.error === undefined
            && data.event === undefined) {
          // 页面 → 宿主的**请求**：自己处理，不再派发（否则会被页面自己的监听吃掉）。
          ZoteroKB.kbAdminDispatch(w, data, "hook");
          return;
        }
      } catch (e) { /* 判断失败就当普通消息放行 */ }
      return orig.apply(this, arguments);
    };
    try { page.postMessage = hook; }
    catch (e4) {
      Zotero.debug("[zotero-kb] 装管理页面 postMessage 钩子失败：" + e4);
      return false;
    }
    try { page.__kbAdminHooked = true; } catch (e5) { /* 标记丢了下次再装，无害 */ }
    try {
      self.writeStatusFile({
        kbAdminPageHook: "installed@" + self.kbAdminHrefOf(w).slice(0, 80),
      });
    } catch (e6) { /* ignore */ }
    return true;
  },

  /**
   * 对一个**确认是管理页面**的 window 做完整武装：钩子 + 两处 message 监听 + 诊断。
   *
   *   ① 钩子 —— 顶层上下文（`window.parent === window`）时请求会回环到页面自己，
   *      只有它能拦下（真机 `parentIsSelf:true` 已证实这是真实形态）。
   *   ② 页面 window 上的监听 —— 上面那条路的兜底。
   *   ③ **父窗口**上的监听 —— 若内容文档其实是嵌套上下文，请求会落在 `window.parent`
   *      （那个 chrome 窗口）上，那里也必须有人听。
   * 三条同时装，多装零成本（不是请求形状就忽略），换来"哪种层次都能通"。
   */
  kbAdminArmPageWindow: function (w, via) {
    var self = ZoteroKB;
    if (!w) return false;
    let page = w;
    try { if (w.wrappedJSObject) page = w.wrappedJSObject; } catch (e) { /* 用原对象 */ }

    // ① 钩子
    let hooked = false;
    try { hooked = self.kbAdminInstallHookOn(w); } catch (e1) { /* ignore */ }

    // ② 页面 window 监听
    try {
      let bound = false;
      try { bound = page && page.__kbAdminMsgBound === true; } catch (e2) { bound = false; }
      if (!bound) {
        const onMsg = function (event) {
          try { self.kbAdminOnPageMessage(w, event, via || "page"); }
          catch (e3) { Zotero.debug("[zotero-kb] 处理页面消息失败：" + e3); }
        };
        w.addEventListener("message", onMsg, true);
        try { if (page) page.__kbAdminMsgBound = true; } catch (e4) { /* ignore */ }
      }
    } catch (e5) { Zotero.debug("[zotero-kb] 挂页面 message 监听失败：" + e5); }

    // ③ 父窗口监听（嵌套上下文时请求落在那里）
    try {
      const p = w.parent;
      if (p && p !== w) {
        let pj = p;
        try { if (p.wrappedJSObject) pj = p.wrappedJSObject; } catch (e6) { /* ignore */ }
        let pbound = false;
        try { pbound = pj && pj.__kbAdminParentBound === true; } catch (e7) { pbound = false; }
        if (!pbound) {
          const onMsg2 = function (event) {
            try { self.kbAdminOnPageMessage(w, event, "parent"); }
            catch (e8) { Zotero.debug("[zotero-kb] 处理父窗口消息失败：" + e8); }
          };
          p.addEventListener("message", onMsg2, true);
          try { if (pj) pj.__kbAdminParentBound = true; } catch (e9) { /* ignore */ }
        }
      }
    } catch (e10) { /* 拿不到 parent 就算了 */ }

    // ④ 诊断：页面在哪、parent 是谁、是不是顶层 —— 只在内容变化时写
    let probe = null;
    try {
      probe = {
        via: String(via || ""),
        url: self.kbAdminHrefOf(w).slice(0, 80),
        parentIsSelf: (w.parent === w),
        parentHref: (w.parent && w.parent !== w)
          ? self.kbAdminHrefOf(w.parent).slice(0, 60) : "",
        hasKBAdmin: (function () {
          try { return !!w.KBAdmin; } catch (e11) { return "ERR"; }
        })(),
        ready: (function () {
          try { return String(w.document && w.document.readyState); } catch (e12) { return "ERR"; }
        })(),
      };
    } catch (e13) { probe = { via: String(via || ""), error: String(e13) }; }
    let text = "";
    try { text = JSON.stringify(probe); } catch (e14) { text = String(probe); }
    if (self._kbAdminProbeWrote !== text) {
      self._kbAdminProbeWrote = text;
      try { self.writeStatusFile({ kbAdminProbe: text }); } catch (e15) { /* ignore */ }
    }
    return hooked;
  },

  /**
   * 观察"文档被创建"与"内容全局被创建"，**用 URL 认出管理页面**并在它的脚本
   * **跑起来之前**把桥装上。
   *
   * 为什么换成这条路（上一版为什么失败，有真机证据）：
   *   上一版靠 `Zotero.openInViewer` 返回的窗口 + `document.querySelector("browser")`
   *   去拿窗口，真机上拿到的是**导航前的 about:blank**（`kbAdminProbe` =
   *   `{"parentIsSelf":true,"hasKBAdmin":false,"docReady":"complete","url":"about:blank"}`），
   *   所以钩子和监听全装在了那个空窗口上，页面真正的请求没人接（`kbAdminChannel` 一直空）。
   *   用 URL 认窗口就没有"是哪一个"的不确定性 —— 页面在哪，钩子就在哪。
   *
   * ⚠ `document-element-inserted` 的 subject 是 **document**；它在脚本执行**之前**
   *   触发，所以能赶在 `admin.js` 的 boot()（complete 后 30ms）之前装上钩子。
   *   另外补一个 `content-document-global-created`（subject 是 window）作保险。
   */
  kbAdminEnsureDocObserver: function () {
    var self = ZoteroKB;
    if (self.kbAdminDocObs) return true;
    try {
      const obs = {
        observe: function (subject, topic) {
          try {
            if (topic === "document-element-inserted") {
              const doc = subject;
              let href = "";
              try {
                href = String((doc && doc.location && doc.location.href)
                               || (doc && doc.documentURI) || "");
              } catch (e) { href = ""; }
              if (!ZoteroKB.kbAdminIsAdminHref(href)) return;
              let w = null;
              try { w = doc.defaultView; } catch (e2) { w = null; }
              if (w) ZoteroKB.kbAdminArmPageWindow(w, "doc");
              return;
            }
            if (topic === "content-document-global-created") {
              const w = subject;
              if (ZoteroKB.kbAdminIsAdminHref(ZoteroKB.kbAdminHrefOf(w))) {
                ZoteroKB.kbAdminArmPageWindow(w, "global");
              }
            }
          } catch (e3) { /* 观察者里绝不抛 */ }
        },
      };
      Services.obs.addObserver(obs, "document-element-inserted");
      Services.obs.addObserver(obs, "content-document-global-created");
      self.kbAdminDocObs = obs;
      Zotero.debug("[zotero-kb] 管理页面文档观察者已挂");
      return true;
    } catch (e) {
      Zotero.debug("[zotero-kb] 挂文档观察者失败：" + e);
      return false;
    }
  },

  /**
   * 扫一遍所有 `zotero:basicViewer` 窗口里的**每一个** `<browser>`，把其中"正在跑
   * 管理页面"的那个武装起来（观察者只对**之后**创建的文档有效，页面若已经加载好，
   * 只能靠这里补上）。
   *
   * 同时把"这个窗口里到底有哪些 browser、各自的 currentURI / browsingContext /
   * contentWindow 在哪"记进状态文件（`kbAdminBrowsers`）—— 上一轮就是缺这份现场，
   * 只能靠猜 `querySelector("browser")` 那一个。
   */
  kbAdminScanAdminWindows: function () {
    var self = ZoteroKB;
    let armed = 0;
    const seen = [];
    try {
      const e = Services.wm.getEnumerator("zotero:basicViewer");
      while (e.hasMoreElements()) {
        const win = e.getNext();
        const rec = { winURI: "", browsers: [] };
        try { rec.winURI = String(win.viewerOriginalURI || "").slice(0, 70); }
        catch (e2) { rec.winURI = "?"; }
        let browsers = [];
        try {
          browsers = Array.from(
            win.document.documentElement.getElementsByTagName("browser"));
        } catch (e3) { browsers = []; }
        try {
          const more = win.document.querySelectorAll
            ? win.document.querySelectorAll("browser") : [];
          for (const b of Array.from(more)) {
            if (browsers.indexOf(b) < 0) browsers.push(b);
          }
        } catch (e4) { /* ignore */ }
        for (const b of browsers) {
          let cw = null, curi = "", href = "", bcuri = "", isTop = null;
          try { cw = b.contentWindow; } catch (e5) { cw = null; }
          try { curi = b.currentURI ? String(b.currentURI.spec) : ""; } catch (e6) { curi = ""; }
          try { href = cw ? String(cw.location.href) : ""; } catch (e7) { href = ""; }
          try {
            const bc = b.browsingContext;
            bcuri = (bc && bc.currentURI) ? String(bc.currentURI.spec) : "";
            isTop = bc ? !!bc.isTopLevel : null;
          } catch (e8) { bcuri = "ERR"; }
          rec.browsers.push({
            id: (function () { try { return String(b.id || ""); } catch (e9) { return "?"; } })(),
            curi: curi.slice(0, 70), bc: String(bcuri).slice(0, 70),
            href: href.slice(0, 70), hasCw: !!cw, isTop: isTop,
          });
          if (cw && self.kbAdminIsAdminHref(href)) {
            try {
              self.kbAdminArmPageWindow(cw, "scan");
              armed++;
            } catch (e10) { Zotero.debug("[zotero-kb] 武装页面窗口失败：" + e10); }
          }
        }
        seen.push(rec);
      }
    } catch (e11) { /* 拿不到窗口枚举就算了 */ }
    // 一个都没武装上时留下现场（只在变化时写，避免 20ms 轮询刷盘）
    if (!armed && seen.length) {
      let text = "";
      try { text = JSON.stringify(seen); } catch (e12) { text = String(seen); }
      if (self._kbAdminBrowsersWrote !== text) {
        self._kbAdminBrowsersWrote = text;
        try { self.writeStatusFile({ kbAdminBrowsers: text }); } catch (e13) { /* ignore */ }
      }
    }
    return armed;
  },

  /**
   * 兼容旧名字：现在它是"扫所有 basicViewer 窗口、按 URL 认页面并武装它"。
   * 保留这个名字是因为 `kbAdminStartBridgeWatch` 与验证脚本仍在调它。
   */
  kbAdminAttachWindow: function (win) {
    return ZoteroKB.kbAdminScanAdminWindows();
  },

  /**
   * 反复补挂桥（**本波的关键修复**，与 `20-kbview.js` 的 400ms 补挂同一套路）。
   *
   * 为什么不能只挂一次：刚 openInViewer 时 `contentWindow` 还是 about:blank；页面一
   * 导航内层 window 就换了，挂上去的监听与钩子全部失效；而 XUL `<browser>` 不触发
   * load 事件，没有可靠的"页面好了"事件可依赖；偏偏页面 boot() 在 complete 后 30ms
   * 就发第一批请求。所以：**头 2 秒每 20ms 补一次**（赶 boot），之后每 250ms 补到
   * 约 12 秒（扛重载/晚到的导航），然后停（不留常驻定时器）。
   */
  kbAdminStartBridgeWatch: function (win) {
    var self = ZoteroKB;
    if (!win) return;
    if (win.__kbAdminWatch) return;          // 一个窗口只起一条链
    win.__kbAdminWatch = true;
    let ticks = 0;
    const tick = function () {
      ticks++;
      try {
        if (win.closed) return;
        // ① 壳窗口的文档还没好就先把我们的 browser 建出来（幂等）
        self.kbAdminMountFrame(win);
        // ② 扫所有 basicViewer 窗口，把"在跑管理页面的那个 window"武装起来
        self.kbAdminAttachWindow(win);
      } catch (e) { /* 下一拍再试 */ }
      if (ticks >= 140) return;
      const delay = ticks <= 100 ? 20 : 250; // 前 2 秒密，之后疏
      if (typeof setTimeout === "function") setTimeout(tick, delay);
    };
    if (typeof setTimeout === "function") setTimeout(tick, 0);
  },

  /**
   * 页面来的一条消息。只处理**请求形状**的（`op` 有值），回包/其它一律忽略 ——
   * 否则我们自己的回包会被再派发一次，变成无限回环。
   */
  kbAdminOnPageMessage: function (target, event, channel) {
    var self = ZoteroKB;
    let d = null;
    try { d = event && event.data; } catch (e) { d = null; }
    if (!d || typeof d !== "object" || d.__kba !== 1) return;
    if (d.op === undefined) return;
    // 来源校验：比对不上只**记日志**，不丢消息 —— 监听点本身只有这个页面的窗口（或它
    // 的父窗口）能说话；Xray 下同一性比不出来是常事，宁可多处理，也不要再出现
    // "请求被静默丢掉"这种事（这一轮就是被静默丢消息坑的）。
    if (!self.kbAdminSameWindow(target, event && event.source)) {
      Zotero.debug("[zotero-kb] 管理页面消息来源比对不上（监听点唯一，继续处理）");
      try { self.writeStatusFile({ kbAdminSrcCheck: "mismatch-but-handled" }); }
      catch (e2) { /* ignore */ }
    }
    self.kbAdminDispatch(target, d, channel || "msg");
  },

  /**
   * 打开管理页面（工具菜单的入口）。**幂等**：已经开着就聚焦它。
   *
   * 为什么用 `Zotero.openInViewer()` 而不是自己 openDialog 一个窗口：
   *   · 它就是 Zotero 自己那条"带 `<browser>` 的窗口"的路子（工具 → 插件用的也是它），
   *     `<browser type="content" remote="false">` 在父进程里加载，宿主拿得到
   *     `contentWindow` —— 这正是我们挂桥需要的（jar: 地址则根本不导航，见 20-kbview.js）；
   *   · 它自带幂等（按 `viewerOriginalURI` 找已开窗口并聚焦）；
   *   · 不用新建任何 chrome 文件（本波只许改三个文件）。
   */
  kbAdminOpen: function () {
    var self = ZoteroKB;
    const url = self.kbAdminPageURL();
    if (!url) {
      self.alertDialog("打不开管理页面", "页面地址没算出来（resource 包没注册？）");
      return null;
    }
    // ⚠ **先挂文档观察者**：管理页面的文档一被创建（脚本还没跑）就按 URL 认出来、
    //   当场把钩子装上（见 kbAdminEnsureDocObserver）。这是"页面在哪、钩子在哪"的
    //   确定性做法；下面那些"扫 browser / 定时补挂"都是它的兜底。
    try { self.kbAdminEnsureDocObserver(); }
    catch (e0) { Zotero.debug("[zotero-kb] 挂文档观察者失败：" + e0); }

    // 壳窗口已经开着 → 聚焦 + 补建 frame + 起观察链
    let win = self.kbAdminFindShellWindow();
    if (win) {
      try { win.focus(); } catch (e1) { /* ignore */ }
      try { self.kbAdminMountFrame(win); }
      catch (e2) { Zotero.debug("[zotero-kb] 补建管理页面 frame 失败：" + e2); }
      self.kbAdminStartBridgeWatch(win);
      return win;
    }
    if (typeof Zotero.openInViewer !== "function") {
      self.alertDialog("打不开管理页面",
        "这个 Zotero 版本没有 Zotero.openInViewer()，装不了带 <browser> 的窗口。\n"
        + "可以先用工具栏按钮打开旧的 Tk 面板。");
      return null;
    }
    const shellURI = self.kbAdminShellURI();
    win = null;
    try {
      // ⚠ 只让它加载 about:blank（**不能**直接加载管理页面，理由见 kbAdminShellURI：
      //   壳窗口自带的 browser 有 maychangeremoteness，会把页面切到独立进程）
      win = Zotero.openInViewer(shellURI, {
        onLoad: function () {
          try {
            self.kbAdminSizeWindow(win);
            self.kbAdminMountFrame(win);
            self.kbAdminStartBridgeWatch(win);
          } catch (e) { Zotero.debug("[zotero-kb] 壳窗口就绪后建 frame 失败：" + e); }
        },
      });
    } catch (e3) {
      self.alertDialog("打开管理页面失败", String((e3 && e3.message) || e3));
      return null;
    }
    // ⚠ 新窗口把默认尺寸调宽（用户 2026-10-13："默认宽度把主页列表挤成两行"）。
    //   放在"新窗口"这条路上：已经开着的窗口在上面就 return 了 —— 用户自己调过的
    //   尺寸不会被我们每次打开都推翻。
    try { self.kbAdminSizeWindow(win); } catch (e5) { /* ignore */ }
    // 壳窗口的文档可能还没解析完 → 立刻试一次，剩下的交给观察链
    try { self.kbAdminMountFrame(win); } catch (e4) { /* ignore */ }
    self.kbAdminStartBridgeWatch(win);
    return win;
  },

  /**
   * 管理窗口的**默认尺寸与最小尺寸**（2026-10-13 用户两轮要求："再宽一点"）。
   *
   * 为什么这么做（读 Zotero 10.0.6 的 `app/omni.ja` 查实，不是猜的）：
   *   · `Zotero.openInViewer(uri, opts)` **没有尺寸参数** —— 它内部就是
   *     `ww.openWindow(null, "chrome://zotero/content/standalone/basicViewer.xhtml",
   *     null, "chrome,dialog=yes,resizable,centerscreen,menubar,scrollbars", arg)`，
   *     尺寸完全由那份 XUL 与平台默认决定；
   *   · `basicViewer.xhtml` 的根元素只写了 `style="min-width:1000px; min-height:700px"`
   *     与 `persist="screenX screenY width height sizemode"`（用户调过的尺寸会被记住），
   *     所以**默认就是最小**：1000×700（实测页面 innerWidth=1000）。
   *     而结构页在 1000~1194px 这个宽度段里「能删吗」表头会被折成三行。
   * 做法（第二版，用户说第一版的 1300 还是"挤"）：
   *   ① 把根元素的 `min-width/min-height` 抬到 **1400×820** —— 用户再也拖不到更窄；
   *   ② `resizeTo()` 到**屏幕可视区的 90%**（`screen.availWidth/availHeight`，
   *      不超过屏幕），拿不到屏幕尺寸时兜底 1600×950。
   * 调用点只在**新建窗口**时（见 kbAdminOpen），不动用户自己调过的尺寸。
   */
  kbAdminSizeWindow: function (win) {
    if (!win || !win.document || !win.document.documentElement) return false;
    const MIN_W = 1400, MIN_H = 820;
    const FALLBACK_W = 1600, FALLBACK_H = 950;      // 取不到屏幕尺寸时的兜底
    try {
      const root = win.document.documentElement;
      root.style.setProperty("min-width", MIN_W + "px");
      root.style.setProperty("min-height", MIN_H + "px");
    } catch (e) { /* 设不上最小尺寸也不影响下面的 resizeTo */ }
    try {
      const scr = win.screen || {};
      const availW = Number(scr.availWidth) || 0;
      const availH = Number(scr.availHeight) || 0;
      // 先按可视区 90% 取，再夹在 [MIN, 可视区] 之间。屏幕比 MIN 还小时只能顶到
      // MIN（min-width 已经是 1400，窗口再小会自相矛盾），这种情况如实大一点。
      const wantW = Math.round((availW || FALLBACK_W) * 0.9);
      const wantH = Math.round((availH || FALLBACK_H) * 0.9);
      const w = Math.max(MIN_W, Math.min(wantW, availW || FALLBACK_W));
      const h = Math.max(MIN_H, Math.min(wantH, availH || FALLBACK_H));
      if (typeof win.resizeTo === "function") win.resizeTo(w, h);
      // 原来那 1000 宽是 centerscreen 放好的；变宽之后顺手放回屏幕中间
      if (typeof win.moveTo === "function" && availW && availH) {
        win.moveTo(Math.max(0, Math.round((availW - w) / 2)),
                   Math.max(0, Math.round((availH - h) / 2)));
      }
      return true;
    } catch (e2) {
      Zotero.debug("[zotero-kb] 调整管理窗口尺寸失败：" + e2);
      return false;
    }
  },

  /**
   * **给插件内部其它模块用的引擎调用面**（W3 线2.5 的入口）。
   *
   * 为什么要有它：面板走"页面 → postMessage → kbAdminHandleRequest"，但那需要
   * 一个打开的页面窗口。`src` 里其它模块（权重列、分类、元数据、侧栏 doc-edit、
   * 纲要菜单…）**不是**在页面里跑的，它们要的是"给一条 HTTP 语义的路径（或引擎
   * 方法名），把数据拿回来"。于是这里给两层：
   *
   *   · `kbAdminHostRoute(method, path, body)` —— 认 HTTP 语义（与
   *     `KB_ADMIN_ROUTES` 同一张表），返回引擎的 `data`；未知路径**明确报错**
   *     （不猜），错误信息里带上路径，方便"这条还没迁"一眼看出来。
   *   · `kbAdminHostRpc(engineMethod, params)` —— 直接调引擎方法（已经知道方法名时用）。
   *
   * 两者都**按需拉起引擎**（`kbAdminRpc` 内部会 `kbAdminStartEngine()`，所以
   * 面板没打开也照样能用；拿不到长驻管道时自动退化成"短进程 + 单行管道"）。
   * 返回 Promise；失败 reject 一个 message 人能看懂的 Error —— 调用方**必须**
   * 接住，别让异常冒到 UI 线程（本项目的纪律：降级可以，抛出去不行）。
   */
  kbAdminHostRpc: function (engineMethod, params) {
    var self = ZoteroKB;
    return self.kbAdminRpc(engineMethod, params);
  },

  kbAdminHostRoute: function (method, path, body) {
    var self = ZoteroKB;
    const route = self.kbAdminMapRoute(method, path, body);
    if (!route) {
      return Promise.reject(new Error(
        "这条路径还没有引擎映射：" + String(method || "GET") + " " + String(path || "")
        + "（要迁就加进 src/23-admin.js 的 KB_ADMIN_ROUTES，并在 online/engine.py 补方法）"));
    }
    return self.kbAdminRpc(route.method, route.params);
  },

  // ================================================================ 门面
  //
  // 计划文档里把宿主半写成 `KBAdminHost.ensureEngine()`。内部实现是一批
  // `ZoteroKB.kbAdminXxx` 成员（好被 check_plugin 的成员引用检查扫到、
  // 也好在桩测试里单独调），这里给一个薄门面把这个名字对上。

  KBAdminHost: null,

  kbAdminHost: function () {
    var self = ZoteroKB;
    if (!self.KBAdminHost) {
      self.KBAdminHost = {
        ensureEngine: function () { return ZoteroKB.kbAdminStartEngine(); },
        rpc: function (method, params) {
          return ZoteroKB.kbAdminRpc(method, params);
        },
        // 插件内部模块用的那两个（见上面的注释）：HTTP 语义 + 引擎方法名
        route: function (method, path, body) {
          return ZoteroKB.kbAdminHostRoute(method, path, body);
        },
        call: function (engineMethod, params) {
          return ZoteroKB.kbAdminHostRpc(engineMethod, params);
        },
        stop: function () { return ZoteroKB.kbAdminStop(); },
        open: function () { return ZoteroKB.kbAdminOpen(); },
        state: function () { return ZoteroKB.kbAdminEngineState(); },
      };
    }
    return self.KBAdminHost;
  },
});
