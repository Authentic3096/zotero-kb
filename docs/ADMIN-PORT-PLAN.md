# 管理面板搬进 Zotero + 去掉常驻 HTTP 服务（范围 B）

> 2026-10-13 用户拍板：**范围 B（整体大改）**。目标两条：
> ① 在 Zotero 里直接打开的管理面板**全面搬进 Zotero**（一份前端，两个入口）；
> ② **去掉常驻的 `127.0.0.1:8765` HTTP 服务** —— 用户的安全诉求是第一位（不想有监听端口），
>    占用第二；**浏览器入口可以放弃**。
>
> 这份文档是施工契约。改之前先读「不变量」与「风险」。

## 1. 目标形态

```
        一份前端：zotero-plugin/content/admin.html / admin.css / admin.js
                          │  只依赖一个薄分发层
                 ┌────────┴────────┐
        Zotero 内 shell           外部 shell（**可选安装**，范围 B 内可晚做）
   resource:// 页面              小启动器（WebView2，自己就是引擎）
   页面 ↔ 宿主半 postMessage      页面 ↔ 启动器（注入 API）
                 └────────┬────────┘
                    引擎进程（Python）
              stdio JSON-RPC（长度前缀分帧），**无端口、无 token**
```

- **无端口**：不监听任何 TCP；页面与服务之间只有"父子进程的 stdin/stdout"。
- **不常驻、不自启**（用户明确要求）：Zotero 在跑时引擎是它的子进程、随 Zotero 退出；
  Zotero 关着时引擎只活在外部启动器里。
- **一份前端两个入口**：页面代码只认 `KBAdmin.get/post(...)`；底下是 postMessage
  还是启动器注入的 API，由分发层决定。

## 2. 三条迁移线（范围 B = 三条都换）

> ⚠ **2026-10-13 更正（W3 施工时查实）**：下面这张表**不完整**。
> `8765` 的调用方除了这三条线，还有 **Zotero 插件自己**的 12 个模块（权重列、
> 分类、元数据、侧栏 doc-edit、纲要右键、公式、MinerU 引导…），以及 Tk 面板
> （W4 才退役）。所以「去掉常驻服务」的真正前置不是三条线，而是**下面 §10 那张
> 完整清单**搬完或退役。判据以 §10 为准，不再靠口头确认。

| 线 | 现在 | 换成 | 谁在用 |
|---|---|---|---|
| 面板 | HTTP | **stdio 引擎** | 管理面板 UI |
| DSH 取数 | HTTP（`/item-info`、`/weights`、`/col-status`） | 插件侧**直读 `index.db`**（DSH 自带 Node 24 有 `node:sqlite`），或经引擎 | `plugins/dsh-zotero-kb` |
| 任务通道 | HTTP（`POST /task` → 插件轮询 `GET /task`） | **文件信箱**（`zotero-bridge` 的 FLAG_DIR 先例：写请求文件 / 轮询 / 写回结果） | DSH → Zotero 派 JS |
| MCP `zotero-kb` | 直读 sqlite | 不变 | DSH 的检索工具 |
| **线 2.5（2026-10-13 新增）** | 插件内部 12 处 `self.request(...)` | **走宿主半的引擎桥**（`src\23-admin.js` 的 `kbAdminHostRpc` / `kbAdminHostRoute`），**不加端口、不监听** | Zotero 插件自身的 UI 功能 |


## 3. 分波计划

| 波 | 做什么 | 可验收的样子 | 状态 |
|---|---|---|---|
| W0 | 前端地基：`admin.html/css/js` + 10 个页签占位 + `theme.py` 主题层 + Tk 面板懒建 | Tk 面板启动 3179→1244ms；`admin.html` 引用完整 | ✅ 已完成 |
| **W1** | **引擎（stdio）+ 宿主桥 + Zotero 内打开入口 + 状态页** | **在 Zotero 里点菜单能打开管理页面，看到状态/知识库结构** | ✅ 已完成（2026-10-13；两个根因见 §4 与 §7） |
| W2 | 其余 9 页 + 对应引擎方法（并行） | 每个页面对应一份验收截图 | ✅ 已完成（2026-10-13；10 页全可用，引擎 `METHODS` 53 个） |
| W3 | 三条线切换：DSH 取数、任务通道→文件信箱；删 HTTP 服务 | `8765` 不再监听；任务通道用新通道派 JS 成功 | ✅ **已完成（2026-10-14）**：线1/线2/线2.5 全部搬完（§10.3 已迁 12 行）；**8765 已停**（用户拍板，执行记录与回退路径见 §10.5） |
| W3.5 | 插件内部 12 处 HTTP 调用 → 引擎桥（§10 那张表） | 逐个模块"改前/改后"真机证据 | ⏳ 未做（入口已就绪并真机验过，见 §10.4） |
| W4 | Tk 面板退役（迁完且验完再删） | 仓库少一套界面 | ✅ **已完成（2026-10-14）**：`tools\panels\*` / `tools\gui.py（已退役 2026-10-14）` / `scripts\0-panel.vbs` 已删（备份在 `kb\backup-20261015-0059-tk-panel\tk-panel.zip`）；插件三个入口（工具栏/右键/设置页）改成打开**新面板**；`verify_bootstrap` 149→**157**（旧断言改成"不再存在 Tk 入口"）<br>⚠ **退役时踩到两个真回归**（都已修 + 已加闸门）：① `engine.py` 依赖 `tools/panels/common.py::KB_FILE_SPEC` —— **那是数据不是 UI**（`kb.structure` 的唯一事实来源）→ 搬到 `offline/kb_manifest.py`（内容逐字照抄）；② `projectRoot()` 的四个探测档原来都用 **`tools\gui.py` 在不在**当判据，删掉它之后引擎起不来（真机现象 `Error: 找不到引擎或 Python`）→ 改成 `online\engine.py` + `offline\schemas.py`；`localserver` 的 `/health` 项目根标记同样换判据（键名保留）。→ `test_engine_rpc` 176/0、`localserver --test` 115/0 恢复全绿 |

> 落地后的现状、协议、10 页清单、页面实现约定、验收数字、待办，见 [`ADMIN-PANEL.md`](ADMIN-PANEL.md)。

## 4. 引擎与协议（W1 的实现约定）

- 引擎入口：`online/engine.py`（新）—— 复用 `online/localserver.py` 里的业务函数，
  **不要重写业务**。协议：**stdin/stdout 上的 JSON-RPC**，一行一条 JSON（NDJSON，
  首版够用；输出里若有换行必须 `json.dumps` 转义，天然安全）。
  - 请求：`{"id":1,"method":"kb.status","params":{...}}`
  - 响应：`{"id":1,"ok":true,"data":{...}}` / `{"id":1,"ok":false,"error":"..."}`
  - 事件（长任务进度）：`{"id":1,"event":"progress","data":{...}}`（同一 id 可多次）
- 方法名：沿用 HTTP 路径语义（`kb.status` / `kb.structure` / `jobs.get` / `experience.list` …），
  一一对应 `docs` 第 5 节的端点清单。
- 宿主半：`zotero-plugin/src/23-admin.js`（新）
  - ⚠ **原写在这里的"Zotero 的 `Subprocess` 只能拿退出码、stdout 是 `ignore`"已被证伪**
    （2026-10-13 直接读已装 Zotero 实现）：Zotero 10 / Gecko 140 的 `Subprocess` 根本**不处理
    `options.stdout`**（传 `stdout:"ignore"` 被静默忽略），子进程的 stdin/stdout 管道**永远都建**，
    返回值上 `proc.stdout` 是一个 `InputPipe`（`readString()` 流式解码）→ **stdio 引擎这条路成立**，
    不需要退化。正确读法见 `src\23-admin.js` 顶部注释。
  - 页面消息 `{__kba:1,seq,op,args}` → 引擎 RPC → 回 `{__kba:1,seq,ok,data|error}`
    （形状已定，见 `content/admin.js` 顶部注释）。
  - 打开入口：工具菜单 + 一个 `<browser>` 窗口（`resource://zotero-kb/content/admin.html`）。
- 前端：`content/admin.js` 的 `call(op,args)` **不需要改** —— 它本来就只跟宿主说话。

## 5. 端点缺口（W2 按页实现，来自只读盘点）

已覆盖、直接复用：`/reindex`、`/jobs/<id>`、`/digest`、`/search`、`/weight`、`/exp-draft`、
`/metafill*`、`/typefix*`、`/classify`、`/llm-config`、`/models`、`/env-check`、`/env-config`、
`/health`、`/status`、`/col-status`、`/item-info`、`/pending`。

必须新增（按页）：
- 状态/结构：`kb-status`（3 个数）、`kb-structure`、`reconcile`、`reconcile/apply`
- 经验库（**整页零覆盖**）：`experience.list|get|save|delete`、`pending/mark`、`learn`
- 分类建议：`papers`（列表）、`summarize`、`apply-suggestion`、`maintain`
- 损坏查询（**整页零覆盖**，且长任务要进度）：`quality-stats|scan|repair|detail`、`config/figures`
- 分节纲要：`outline-list`、`outline/render`、`outline/delete`
- 运行环境：`service/start|restart|status`、`ollama/start`、`llm-test`
- PDF 解析：`mineru-status`、`mineru/missing|reparse|parse|clear`（长任务）
- 高级：`weight-list`、`collections`、`zotero-sync`、`plugin/*`、`zotero-upgrade`
- 提示词（**整页零覆盖**）：`prompts`、`prompts/probe`

顺手删掉两个**死按钮**：`tab_env.py` 的 `tools/diagnose_plugin.py` 与 `tools/zotero_js.py`
（两个文件都不存在，照搬必失败）。

## 6. 不变量（不许破坏）

1. ~~**W3 切换之前，旧的 HTTP 服务与 Tk 面板必须仍然可用**~~（⚠ 2026-10-14 已切换：8765 已停，验证手段改为**文件信箱 + 引擎桥**；Tk 面板在 W4 之前失效，见 §10.5）
   原来的话：**W3 切换之前，旧的 HTTP 服务与 Tk 面板必须仍然可用** —— 它们是我验证新通道的手段
   （`tools/zotero/dispatch_js.py` 就是靠 `/task` 派 JS 进 Zotero）。
2. 插件图标（v3）、侧栏列（`content/kbview.*`）、DSH 插件（`dsh-zotero-kb`）**在各自被点名之前不动**。
3. 发布面纪律：`tools/audit_release.py` 全绿（不许出现开发机路径/用户名）、无 BOM 纪律、
   改 `src/*.js` 必须跑 `tools/build_bootstrap.py`。
4. 零新增依赖（Zotero 插件与 Python 引擎侧）；外部启动器的 WebView2 依赖是**可选项**，不影响不装的人。

## 7. 风险与坑

1. **~~Zotero `Subprocess` 拿不到 stdout~~（已证伪，见 §4）** —— stdio 引擎成立，已落地。
   W1 实际撞到的两个真风险是：**页面被切进远程进程**（basicViewer 的 `<browser>` 带
   `maychangeremoteness`，要自建 browser）与**页面里 `window.parent === window`**
   （`postMessage` 请求回环，要在页面 window 上装分派钩子）。取证级细节见
   `project\zotero-kb\接手.md` §5.17.5。
2. 长任务进度：现在只有 `/digest` 用 `progress=True`；W2 的 `quality-scan`、`mineru/reparse`
   必须带进度，否则页面会像死了一样。
   → **已定口径**（2026-10-13 用户拍板）：长任务一律**派发即返回** + 轮询 `jobs.get`，
   **进度不做事件转发**（页面轮询 `job.log` 就有文字）。这是有意为之，不是漏项。
3. Windows 管道/编码：引擎必须强制 UTF-8（`PYTHONIOENCODING=utf-8` + `sys.stdout.reconfigure`），
   否则中文输出会被 GBK 撕碎。
4. 子进程生命周期：Zotero 退出要杀掉引擎；引擎崩了页面要能重连并给出可读错误。
5. 文件信箱（W3）：并发写、孤儿文件、清理规则要想清（`zotero-bridge` 里有现成做法，照抄）。

## 8. 每一波怎么验收

- W1：Zotero 里点开管理页面 → 看到状态块与知识库结构表 → **截图存证**；
  同时确认 `8765` 没被用到（关掉服务也能打开页面）。
- W2：每页一份截图 + 与 Tk 面板同页对照（数据要对得上）。
- W3：`netstat` 确认无监听；用新任务通道派一次 JS 并拿到结果；DSH 插件列表刷新正常。
- W4：Tk 面板删掉后，`check_plugin.py` / `verify_bootstrap.js` / `audit_release.py` 全绿。

## 9. 落地回填（2026-10-13，W1 / W2 完成后）

实际形态与这份契约的差异，以 [`ADMIN-PANEL.md`](ADMIN-PANEL.md) 为准：

- **版本**：Zotero 10.0.6 / 插件 1.12.0（**未升版**）；本轮**未提交、未发布**。
  （⚠ 2026-10-15 补记：插件已升 **2.0.0** 并发布，见 `docs\RELEASE-2.0.0.md`。）
- **协议**：NDJSON 一条一条（首版没做长度前缀分帧）；传输两条 —— 长驻管道（首选）/
  短进程 + 单行管道（退化）。**"短进程 + 结果写文件"那条已删**（`engine.py` 从没实现过 `--once`，
  是永远失败的尝试，留着只会盖住真故障）。
- **方法数**：引擎 `METHODS` **53** 个（含 `ping`）。
- **引擎两个硬约束**（都已落地）：① stdout 只走 `_STDOUT`、`sys.stdout` 转 stderr；
  ② 主循环**不用阻塞 `readline`**，Windows 管道走 `PeekNamedPipe` 轮询 —— 否则整个进程的
  DLL 初始化与子进程会卡死（见 `接手.md` §5.17.6）。
- **面板 UI 约定**（切页缓存、按钮三档、原生 `<select>` 打不开的替代）见 `ADMIN-PANEL.md` §7。
- **验收实测**：`test_engine_rpc.py` 139/0、`localserver.py --test` 115/0、
  `verify_bootstrap.js` 149/0、`check_js_syntax.py` 48 文件 0 问题、`audit_release.py` 35/35。
- **开发回路**：`docs\DEV-PREVIEW-HARNESS.md`（真页面 + 假宿主半的预览桩套路）。

## 10. W3 的调用方清单（线3 的判据）与迁移映射

> 2026-10-13 施工时**只读盘点**得到。**这张表搬完（或退役）之前不许停 8765。**
> 盘点方法：在 `zotero-plugin\src\*.js`（不含生成物 `bootstrap.js`）里找所有
> `self.request(` / `this.request(` 调用点，逐个记下文件 / 函数 / 路径 / 功能。

### 10.1 线1：DSH 侧（`plugins\dsh-zotero-kb`）—— ✅ 已搬走

宿主半 `lib/index.js` 原来打三趟 `/weights` + `/item-info` + `/col-status`，
2026-10-13 改成 **`node:sqlite` readOnly 直读 `<知识库>\index.db`**（items 表 +
`meta.ai_outline:` + `views\*.outline.md`），**一个 HTTP 请求都不发**（测试里
全局 fetch 换成"调用即抛"当闸门）。客户端半 `lib/client.js` 本来就没渲染权重列，
所以直读版不再算 `weight`（要显示就走引擎的 `advanced.weightList`，别在 JS 里复刻）。

### 10.2 线2：任务通道 —— ✅ 已搬走（真机验过）

- 新通道：`<知识库>\mailbox\{requests,claimed,results}\`
  （Python 侧 `online/taskbox.py`；插件侧 `src/18-taskpoll.js` 的
  `pollTaskMailbox` / `reportTaskMailbox` / `mailboxCleanup`）。
- 领取 = **move 进 `claimed\`**（rename 原子 → 只可能被执行一次）；
  结果 = `results\<id>.json`（先写 `.tmp` 再 move）。
- **老 HTTP 队列暂时保留**：插件两条队列都消费（同一任务只会写进其中一处，不会双执行），
  关掉它的开关是 `zotero-kb.taskPollHttp`。派发方应急回退：`KB_TASK_VIA=http`。

### 10.3 线3 的调用方清单与进度（**2026-10-13 起逐条搬，搬完才允许停服务**）

**已迁 12 行（**除 Tk 面板与归档脚本外全部**）：序号 1、2、3、4、5、6、7、8、9、10、12、13** ——
方法都是"桥 vs 老 HTTP 对照"（含 2026-10-14 这批：`/digest/cancel`、`/doc-edit` 读写、`formula.*`）：
权重 94 篇 × 8 字段、列状态 94 篇 × 3 字段**逐字段一致**；`/reindex` 派活回执的
**两通道形状差异实测确认**（桥 `job` 是**对象** `{id}`、HTTP 是**字符串**，归一化后
两边都能被 `jobs.get` 查到）；`/item-info` 对同一批真实 key **逐字段比类型 + 比值**
全等（含不在库的 `{key,in_kb:false}`），迁移后共用的 `kbItemInfo()` 同时满足
两个调用方；`/weight` **只验了错误分支**（两通道都失败且口径一致），并在探针前后
各读一次权重映射证明**真实条目一个字节没改**（写成功路径按纪律未验）。
全部保留老 HTTP 兜底。**#1（`02-paths.js`）与 #3 的 `/classify` 已判"不迁、待扩契约"** ——
下一个会话**不要从 #1 开始**，先做下面那两条已批准的契约变更（见 §10.3 收工状态）。

### 10.3.0 收工状态（2026-10-13 13:31 就地收工，给下一个会话看）

| 项 | 状态 |
|---|---|
| 已迁 | **5 处（4 整 + 1 半）**：#5 `/reindex`+`/jobs/<id>`、#6 `/item-info`、#7 `/weights`、#8 `/col-status`、#3 的 `/weight`。全部**已迁 + 真机验过 + 保留老 HTTP 兜底** |
| 已判"不迁" | **#1 整条**（`02-paths.js` 两处契约不符）、**#3 的 `/classify`**。两处都在代码注释里写清原因与后续做法 |
| **下一个会话先做** | 上面那两条**已批准待做的契约变更**：① `classify.item`；② `env.save.report` + `env.checkRaw`。**成批做、一次补 `test_engine_rpc` 断言**；做完再迁 #1 与 #3 的剩余部分 |
| **线 3** | **一个字没动**：8765 在跑、`online/localserver.py` 的路由/行为未改（`--test` 115/0 保持）。判据见 §10.5 |
| 加载面 | profile 里仍是那条 **60 B 代理文件**（`extensions\zotero-kb@authentic3096.github.io`，无 `.xpi`）→ **下次 Zotero 重启会自然带上新 `bootstrap.js`**（#1 那轮只改注释、零行为改动，故未为它专门重启） |
| 门禁（收工时实测） | `test_engine_rpc` **149/0** · `localserver --test` **115/0** · `node verify_bootstrap.js` **149/0** · `check_js_syntax` **48 文件 0 问题** · `audit_release` **35/35** · `check_plugin` **检查通过** |
| DSH 侧 | **未重启**（仍需用户重启 DSH 才生效）；DSH 插件那部分见本文件 §2 与 `plugins\dsh-zotero-kb\README.md` |

> **通用检查项（2026-10-13 立）**：每迁一个模块，探针里必须把「桥回包 vs 老 HTTP 回包」的
> **字段类型逐个比一遍**（不只是比值）—— 已经抓到一次真差异（`job` 对象 vs 字符串），
> 这类"照抄老写法就静默失败"的坑只能靠类型比对发现；不一致就在代码里归一化并在报告点名。
> **同一条检查也适用于入参**（键名/扁平 vs 嵌套）：`/env-config` 就是入参形状不同
> （老 HTTP 嵌套 `{env:{…}}`、引擎扁平）—— 照抄老写法过桥**当场报"没有要写入的键"**。

> **已批准的两条契约变更 —— ✅ 2026-10-14 已做完（成批做、一次补测试，`test_engine_rpc` 145→159）**：
> 1. **`classify.item`**（路由 `POST /classify-item`）—— 收**内联**元数据 + 调用方给的模型配置，
>    复用 `LS.do_classify_one`；`07-classify.js` 已改**桥优先 → 退回老 HTTP**。
> 2. **`env.checkRaw`**（路由 `GET /env-check-raw`）+ **`env.save` 回包补 `report`** ——
>    `02-paths.js` 的 `kbPushEnvConfig`（**入参摊平**）/ `kbEnvReport` 已改**桥优先 → 退回老 HTTP**。
> 真机验收（`dispatch_js.py` 派进活 Zotero）：`env.checkRaw` 与老 HTTP `/env-check`
> **逐字段类型 + 值全等**（`valueEqual: true`）、设置面板要的 `python.runnable`/`project_root.has_panel` 都在；
> `env.save` 摊平后用坏值 → 报「指向的位置不存在」（**不再是**「没有要写入的键」），
> 且 `kb-location.json` SHA256 前后一致（**没写配置**）；`classify.item` 的坏输入两条通道
> 都在**调模型之前**就拒掉（不烧额度、引擎不死）。

> **⚠ 剪贴板副作用（留档，2026-10-14 实测）**：`formula.mathtype` 的 `dry_run=true` **也会写剪贴板**——`offline/formula.py::start_mathtype` 的 docstring 说的是“只做检测与写剪贴板、**不启动** MathType”，它省的是“起进程”而不是剪贴板（本轮探针与测试各跑一次 → 用户剪贴板被写过 `x^2`）。要彻底不碰剪贴板得先改 `formula.start_mathtype`；**本轮只留档、没改**。
>
> **下一批候选 + 本轮已核对的结论（2026-10-14）**：
> · **#12 `/mineru-check`**：**不能**直接用 `mineru.status` —— 实测两者**形状不同**：
>   `mineru.status` 回 `{probe,rows,total,summary,artifacts_size}`（给面板的列表/汇总），
>   而 HTTP `/mineru-check` 回的是 `{ok,exe,version,small_backend,vlm_engine,tiers,tier_ready,
>   repos,model_dir,home,torch,cuda,cuda_ok,gpu,why,checked_at,elapsed,cached}`（MinerU 安装引导要的原文）。
>   要迁得同 `env.checkRaw` 那样**再加一个"原文"方法**（`mineru.checkRaw`）。
> · **#10 单条 `/metafill`、`/typefix`**：空口不同 —— HTTP 那两条**同步**回丰富对象
>   （`/metafill` → `{ok,key,item_type,current,sources,fulltext_pages,model,notes,…}`；
>   `/typefix` → `{ok,key,item_type,title,doi,suspect,signals,fixes,n_pdfs,notes}`），
>   而引擎现成的 `meta.fillScan` / `meta.typeScan` 是**长任务、派发即返回**。
>   要迁得新增两个"同步单条"方法（复用原来的 `do_metafill` / `do_typefix` 实现）。
> · 两条都属"需新增"，建议与 #2 `/health` 一起做最后一批。

| # | 模块 | HTTP 路径 | 功能 | 引擎侧 | 状态 |
|---|---|---|---|---|---|
| 1 | `02-paths.js` | `POST /env-config`、`GET /env-check` | 设置面板「运行环境」保存 / 检测 | ✅ 引擎侧已补齐：`env.save`（收**扁平**键、回包带 `report`）、**新增 `env.checkRaw`**（回 `env_report()` 原文） | ✅ **已迁 + 真机验过**（2026-10-14）：桥优先 → 退回 HTTP；**入参摊平**（`Object.assign({}, env)`）；两条通道**逐字段类型+值全等** |
| 2 | `05-server.js` | `GET /health` | 服务握手（拿 kb_dir；token 只用来同步 pref） | ✅ `kb.health`（**不含 token**，实测确认） | ✅ **已迁 + 真机验过**（2026-10-14）：桥优先拿 kb_dir；老 HTTP `/health` **只用来同步 token**、**不再当 serverOk 判据**（服务没跑也不影响）；探针只读、token pref 一字未动 |
| 3 | `07-classify.js` | `POST /classify`、`POST /weight` | 分类建议 / 标重点 | ✅ `kb.weight`（复用 `LS.do_set_weight`）<br>✅ **新增 `classify.item`**（收**内联**元数据 + 调用方给的模型配置，复用 `LS.do_classify_one`） | ✅ **全部已迁 + 真机验过**（2026-10-14）：`/weight` 桥优先（错误分支两通道一致 + 前后读回**一个字节没改**）；`/classify` → `POST /classify-item` 桥优先，坏输入两通道都在**调模型之前**拒掉 |
| 4 | `14-menus.js` | `POST /digest`、`POST /digest/cancel`、`GET /jobs/<id>` | 右键「生成分节纲要」/「停止」+ 轮询 | ✅ `kb.digest` / **新增 `kb.digestCancel`** / `jobs.get` | ✅ **已迁 + 真机验过**（2026-10-14）：`kb.digestCancel` 回 `{ok,cancelled,hint}`（与 HTTP 置**同一个取消标记**）；两处 cancel 调用点都改了 |
| 5 | `06-watch.js` | `POST /reindex`、`GET /jobs/<id>` | 监听新条目→建库+轮询 | ✅ 零新增：`kb.reindex` / `jobs.get` | ✅ **已迁 + 真机验过**（2026-10-13）。⚠ 回执形状两通道不同：桥 `{job:{id}}`、HTTP `{job:"<id>"}`，**必须归一化**（`res.job.id \|\| res.job \|\| res.id`）。⚠ 该模块**剩下**的 `POST /item-info` 属序号 6、`healthCheck()` 属序号 2，都还走 HTTP |
| 6 | `06-watch.js`、`12-dsh.js` | `POST /item-info` | "这条建过库没有" + 作者/年份/分类 | ✅ **新增 `kb.itemInfo`**（复用 `LS.do_item_info`） | ✅ **已迁 + 真机验过**（2026-10-13，**两处调用点共用 `05-server.js::kbItemInfo`**；逐字段比类型+比值全等） |
| 7 | `16-weightcol.js` | `GET /weights` | 文献列表权重列（**全量映射**，每篇都有） | ✅ **新增 `kb.weightsMap`**（复用 `LS.do_weights_map`）。⚠ **不能**用现成的 `advanced.weightList` —— 它只列 `item_weight` 里有行的那几篇（口径是"权重榜"），拿它填列会让其余全空白（老实现踩过同一个坑；本机实测 全量 94 vs 榜单 12） | ✅ **已迁 + 真机验过**（2026-10-13） |
| 8 | `21-kbcols.js` | `GET /col-status` | 列表那两列（已建库 / 纲要节数） | ✅ **新增 `kb.colStatus`**（复用 `LS.do_col_status`）。⚠ `outline_sections` 必须走 `meta.ai_outline:`（读端口径），**不是数 views 文件个数** | ✅ **已迁 + 真机验过**（2026-10-13） |
| 9 | `20-kbview.js` | `GET/POST /doc-edit` | 右侧栏「知识库」正文编辑（含草稿、逐页） | ❌ **需新增 `doc.get` / `doc.save`**（复用 `LS.do_doc_edit*` 与 `offline/edits.py`） | ⏳ 未动（**写路径，排最后**） |
| 10 | `08-metafill.js`、`09-metabatch.js` | `POST /metafill`、`POST /typefix` | 元数据补全 / 类型修正（**单条同步**，右键菜单用） | ✅ **新增 `meta.fillOne` / `meta.typefixOne`**（复用 `LS.do_metafill` / `LS.do_typefix`） | ✅ **已迁 + 真机验过**（2026-10-14）：坏 key（`use_model:false`/不联网）两通道**逐字段类型+值全等**（`valueEqual:true`），不调模型、不写库 |
| 11 | `14-menus.js` | `POST /digest/cancel` | 取消正在跑的分节纲要 | ❌ **需新增 `kb.digestCancel`**（复用 `LS.do_digest_cancel`） | ⏳ 未动 |
| 12 | `19-mineruguide.js` | `GET /mineru-check` | MinerU 安装引导的检测 | ✅ **新增 `mineru.checkRaw`**（回 `mineru.probe()` 原文）—— 与 `mineru.status`（面板汇总）**不是同一个形状** | ✅ **已迁 + 真机验过**（2026-10-14）：与老 HTTP 原文 **18 个键集合与类型全等**（`valueEqual:false` 只是 `checked_at/elapsed/cached` 这类每次不同的字段） |
| 13 | `22-formula.js` | `GET /formula-target`、`POST /formula-apply`、`POST /formula-mathtype`、`GET /jobs/<id>` | 公式定位/写回/起 MathType（阅读器侧） | ✅ **新增 `formula.target` / `formula.apply` / `formula.mathtype`**（复用 `offline/formula.py`）；轮询改用共用 `kbJobGet` | ✅ **已迁 + 真机验过**（2026-10-14）：坏输入两侧同一句话；`mathtype dry_run` **字段与类型全等** |
| 14 | `18-taskpoll.js` | `GET /task`、`POST /task/result` | 任务通道 | ✅ **已送走**（线2；HTTP 那条现在只是过渡兜底） |
| 15 | ~~Tk 面板~~ | 全部 | 管理界面 | — | ✅ **W4 已退役**（2026-10-14）：代码与启动入口已删，插件三个入口改指新面板 |
| 16 | `tools\from-repo\*`（`zotero_js.py`、`test_task_bridge.py`） | `/task`、`/task-list` | 历史归档脚本 | 不迁（归档副本；`test_task_bridge.py` 是**老 HTTP 队列本身**的测试，服务不退役就不该删） |

**统计**：`src` 里（不含 05-server 这个通信层本身、不含已迁的 18-taskpoll）
**12 个功能模块、19 条去重后的 HTTP 路径**：**8 条引擎已有**（`/env-config` `/env-check`
`/health` `/classify` `/weight` `/digest` `/jobs/<id>` `/reindex`）、**11 条需新增**
（`/item-info` `/weights` `/col-status` `/doc-edit` `/metafill` `/typefix`
`/digest/cancel` `/formula?…` `/formula-apply` `/formula-mathtype`，外加 `/mineru-check`
需核对形状）。新增的每一个都能直接复用 `online/localserver.py` 里现成的 `do_*` ——
引擎已定的口径是"业务一行不重写"。

### 10.4 线2.5 的入口已就绪（2026-10-13 真机验过）

`src\23-admin.js` 早就有全套引擎桥（`kbAdminRpc` → 长驻管道 / 短进程退化），
本轮**只补了一个给插件内部用的门面**（原来只有页面的 postMessage 一条路）：

```js
// 插件内部（任何模块，都可以这样用；不需要打开管理面板窗口）
const h = await Zotero.ZoteroKB.kbAdminHostRoute("GET", "/health");     // HTTP 语义，走 KB_ADMIN_ROUTES
const s = await Zotero.ZoteroKB.kbAdminHostRpc("kb.status", {});        // 或直接给引擎方法名
// 门面别名：Zotero.ZoteroKB.kbAdminHost().route(path) / .call(method, params)
```

- **按需拉起**：`kbAdminRpc` 自己会 `kbAdminStartEngine()`（幂等），拿不到长驻管道
  就退化成"短进程 + 单行管道"；**面板没打开也能用**。
- **未知路径不猜**：回一条带路径的错，说清"要迁就加进 `KB_ADMIN_ROUTES` +
  在 `engine.py` 补方法"。
- 真机证据（`tools\zotero\dispatch_js.py` 派进活 Zotero）：
  `kbAdminStop()` 收掉 pid 27276 → 只用 `kbAdminHostRoute("GET","/health")` 取数 →
  引擎**自己重新起来**（pid 26776，`mode=pipe`，879ms）并返回
  `{kb_dir:"C:\\Users\\…\\Zotero\\zotero-kb", ok:true}`；
  `kbAdminHostRpc("kb.status",{})` → `{items:94, chunks:16426}`；
  `kbAdminHostRoute("GET","/no-such-path")` → 明确报错而不是静默成功。

### 10.4.1 ⚠ 改 `src/` 后"重启也不生效"的头号原因（2026-10-13 真踩，30 秒可判）

**加载面会在"60 B 代理文件" ↔ "几百 KB `.xpi`"之间被来回切换。** 判据：

| 看哪 | 代理文件（好） | 打包副本（坏） |
|---|---|---|
| `profile\extensions\` 下本插件那条 | **不带 `.xpi`**，约 **60 B**（内容是一行源码目录路径） | 带 `.xpi`，几百 KB |
| `extensions.json` 里那条的 `rootURI` | 指向源码目录 | `jar:file:///…xpi!/` |

坏的情况下**改 `src/*.js` + `build_bootstrap.py` 全都不影响运行中的插件**，现象是
"改了没反应"或"某个新路由报『还没有引擎映射』"——极易误判成代码写错。

**触发源（2026-10-14 查清，别再互相猜了）**：
- **`tools\sideload_plugin.py install --copy-xpi`** ← **就是它**：它把打包好的 xpi
  **拷进 profile**，于是代理文件被替换成打包副本，**之后改 `src/` 全部不生效**。
  （父 agent 每轮做"循环"时用它，图省事想让面板页改动一定生效 —— 实际是反效果。）
- `tools\check_plugin.py` / `pack_plugin.py` **只打包到仓库、不动 profile**（已实测）。

**正确流程**：
- 改完 `src/` 只想让**源码生效** → 用**代理文件**：`sideload_plugin.py install`
  **不带** `--copy-xpi`（或直接 `--cycle`）。代理文件指向源码目录，**重启即吃新代码**。
- **只有**要模拟"用户从 Zotero UI 装 xpi"时，才用 `--copy-xpi`；用完记得换回代理文件
  （换回方式同上那条不带 `--copy-xpi` 的命令）。
- 判"新代码到没到"最快的办法：`dispatch_js.py` 派
  `return JSON.stringify(Object.keys(Zotero.ZoteroKB.KB_ADMIN_ROUTES))`，看新路由在不在。


### 10.5 线3（停 8765）—— ✅ **2026-10-14 已执行完毕（用户明确"我接受风险"后动手）**

> 执行人不代做决定：**用户在 2026-10-14 明确点头**后才停的服务。下面是执行记录与证据。

**执行顺序与结果（全部有真机证据，取证文件在会话目录 `2026-10-13-zotero-kb-W3/line3-*.json`）**

| 步 | 做了什么 | 证据（要点） |
|---|---|---|
| ① | **停之前的六条走查**（基线） | `weights.cached=94`；`cols.ready=true, cached=94`；`kb.digest`/`kb.digestCancel` 路由在表里、`jobs.get` 可达；`doc.get` 与老 HTTP **逐字段类型+值全等**（`valueEqual:true`）；`mineru.checkRaw` **18 键全等**；面板第一页依赖的 `kb.structure`+`kb.status` 走桥可达（`items=94`） |
| ② | **堵住自动拉起**：发现自动拉起路径 = `03-processes.js::serverOnStartup`（Zotero 启动即拉服务）。加一个**只控这一件事**的开关 `pref zotero-kb.autoStartService`（prefs.js 默认仍 true=老行为），并在本机把它置 **false** | 踩了一个坑：`Zotero.Prefs.set` 会**自动补 `extensions.zotero.`**，第一次写成全名 → 落成**双前缀**无效键，重启后服务果然又起来了；改成**短名** `zotero-kb.autoStartService` 后 profile 里是 `user_pref("extensions.zotero.zotero-kb.autoStartService", false)`，状态文件里 `serverAutoStart=false` |
| ③ | **停服务**：走它自己的 `POST /shutdown`（带 token，**不是**强杀） | 响应 `{"ok":true,"bye":true}`；`Get-NetTCPConnection -LocalPort 8765 -State Listen` **空**、无 Established（netstat 里只剩 TIME_WAIT 残留）；`localserver.py` 进程**全部退出**；`/health` 连接被拒 |
| ④ | **停后 + 重启后各取一次证**（信箱派 JS，**没有 8765 也能在活 Zotero 里派活**） | 两次都 `state=done channel=mailbox`；`serverOk=true`（**不再被"服务没跑"带成 false**）；功能抽查：权重 94 / 两列 ready+94 / 面板 structure+status / 侧栏 doc-edit / 纲要列表 94 / 设置面板要的 `env.checkRaw`+`itemInfo` 全 OK；`httpStillUp=false` |
| ⑤ | **`netstat` 复查 + 重启 Zotero 复查** | 重启后 `8765 仍无监听`、`没有 localserver 进程`、状态文件 `serverStartedByMe=false, serverAutoStart=false` → **自动拉起确实被堵住了** |

**门禁（服务停着时的实测）**：`test_engine_rpc` **176/0**、`localserver --test` **115/0**（它自带进程内测试服务，不需要常驻 8765）、`check_js_syntax` **48/0**、`audit_release` **35/35**、`node verify_bootstrap.js` **149/0**。

**⚠ 代价（必须让用户知道）**：
- **Tk 面板（`scripts\0-panel.vbs`、工具栏按钮、条目右键「打开知识库管理面板」）从现在起失效** —— 它是独立 Python 进程，靠 8765 通信。这是 W4 之前的状态：要么按 W4 退役它，要么恢复服务。
- Zotero 内的管理面板（工具菜单「知识库管理…」）**不受影响**（它走 stdio 引擎）。

**④ 回退路径（2026-10-15 更新：Tk 代码已随 W4 删除，所以"恢复 Tk 面板"要连代码一起还原）**：
- **只想恢复 8765 服务**（插件与 DSH 侧本来就不依赖它，所以不回退也不影响功能）：
  ① 双击 `scripts\4-service.vbs`（或 `python online\localserver.py`）手动起 →
  ② 想让插件自动拉起就把 pref 改回来：`Zotero.Prefs.set("zotero-kb.autoStartService", true)`
  （**短名！别写全名**），或直接在 Zotero 里跑一次上面这句（工具→开发者→运行 JavaScript）。
- **想恢复 Tk 面板**：先 `Expand-Archive kb\backup-20261015-0059-tk-panel\tk-panel.zip`（含 `tools\gui.py`、
  `tools\panels\*`、`scripts\0-panel.vbs`），再起 8765；否则那些入口打不开。**没有必要就别做** ——
  功能已被 Zotero 内那个面板 + 引擎桥完整覆盖。
- ⚠ `scripts\install-autostart.ps1` / `autostart-service.vbs` 是**登录自启**入口，**本机没有安装**
  （注册表 Run 里没有、启动文件夹里也没有）——**以后不要点它**，否则"停服务"会被登录自启破坏。

## 11. 未核实 / 已知不一致

- **未在真机验过**：经验库编辑弹窗里再开一层 `<dialog>` 的嵌套用法；
  `<select>` 替代控件（并排按钮组 / 自绘下拉）在真机上的展开。
- **文档与代码不一致（已按代码更新）**：`ADMIN-PORT-PLAN.md` 原来写
  "Zotero `Subprocess` 只能拿退出码、stdout 是 `ignore`，stdio 引擎可能做不了" —— 已核实**前提是错的**：
  Zotero 10 / Gecko 140 的 `Subprocess` 根本不处理 `options.stdout`（传了被静默忽略），
  子进程 stdin/stdout 管道**永远都建**，返回值上 `proc.stdout` 是 `InputPipe`。
  `ADMIN-PORT-PLAN.md` §4/§7 已按此更正。
- 本轮**没有**跑 `tests\test_mineru_*.py`（会真拉起 MinerU 占满机器），所以 MinerU 相关的全量测试
  数字不在上面那张表里。
- ⚠ **2026-10-14 已知的"只验了一半"的地方**（别当成全验过）：
  · `07-classify.js` 的**成功路径没验**（`classify.item` 会真调模型 → 只验了坏输入；
    成功路径靠"复用 `LS.do_classify_one`"保证）。
  · `20-kbview.js` 的 `doc.save` **写成功路径没验**（只验坏输入，按纪律不写真实页）。
  · `02-paths.js` 的 `env.save` **写成功路径没验**（只验坏输入；`kb-location.json` SHA256 前后一致）。
  · `05-server.js` 的 `healthCheck` 迁移后**只做了只读对照**（没有真跑一次看 token pref 是否照旧刷新）。
  · `formula.mathtype` 的 `dry_run` **会写剪贴板**（见 §10.3 顶部留档）。

## 12. 相关文档

| 文档 | 讲什么 |
|---|---|
| [`ADMIN-PORT-PLAN.md`](ADMIN-PORT-PLAN.md) | 范围 B 的施工契约、迁移线、分波、不变量、风险、**§10 W3/W3.5 的进度与停服务判据** |
| [`DEV-PREVIEW-HARNESS.md`](DEV-PREVIEW-HARNESS.md) | **预览桩套路**（真页面 + 假宿主半）：什么场景用、怎么搭、四个坑、两种验收 |
| [`ADMIN-API-W2.md`](ADMIN-API-W2.md) | 批次 A 方法契约 + 实际口径 + 页面硬约束 |
| [`ADMIN-API-W2B.md`](ADMIN-API-W2B.md) | 批次 B 方法契约 + 实际口径 + 页面硬约束 |
| `project\zotero-kb\接手.md` §5.17.5 / §5.17.6 | 宿主桥与引擎卡死的取证级根因 |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 目录结构、构建与测试、发版流程、排错入口 |
