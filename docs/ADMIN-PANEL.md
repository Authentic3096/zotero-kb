# 管理面板（搬进 Zotero 的无端口版）

> 2026-10-13 落地。**这是范围 B「管理面板搬进 Zotero + 去掉常驻 HTTP 服务」的权威说明**：
> 面板长什么样、三层怎么分工、10 页有哪些、协议与契约在哪、页面怎么写、怎么不开 Zotero 改样式。
>
> 施工契约与分波计划见 [`ADMIN-PORT-PLAN.md`](ADMIN-PORT-PLAN.md)；
> 两个根因（宿主桥 / 引擎卡死）的取证级细节在 `project\zotero-kb\接手.md` §5.17.5、§5.17.6。
> 状态：**W1–W4 全部完成（2026-10-14）** —— 10 页搬完、三条迁移线切完、8765 已停、Tk 面板已退役。
> 现在**只有一个入口**（Zotero 工具 →「知识库管理…」）；8765 的回退件是 `scripts\4-service.vbs`
> （除非要复现旧 HTTP 通道，否则不要起它）。施工记录见 ADMIN-PORT-PLAN §10.5。

## 1. 入口

| 入口 | 打开的是什么 | 代码 |
|---|---|---|
| Zotero **工具 → 「知识库管理…」** | **新面板**：Zotero 自己的窗口，里面加载 `resource://zotero-kb/content/admin.html` | `src\14-menus.js::registerAdminMenu` → `src\23-admin.js::kbAdminOpen` |
| 主工具栏「打开文献知识库管理面板」按钮、条目右键「打开知识库管理面板」 | **新面板**（同上）—— W4（2026-10-14）起 Tk 面板已退役，`openPanel()` 改成调 `kbAdminOpen` | `src\14-menus.js::registerToolbarButton` / `openPanel()` |
| ~~`scripts\0-panel.vbs`~~ | **已删**（W4 2026-10-14）；管理面板现在只有 Zotero 内那个入口 | — |

**为什么新入口放工具菜单**：工具栏那个按钮的定义就是"把旧 Tk 面板拉起来"，
W3 之前必须不动它（ADMIN-PORT-PLAN 不变量 1）；新面板与「JSON API」之类并列即可。

## 2. 三层架构（无端口）

```
content/admin.html + admin.js + admin.css + admin-pages/*.js   ← 页面（10 页）
        │ postMessage {__kba:1,seq,op,args}（HTTP 语义的路径 + 方法）
zotero-plugin/src/23-admin.js                                  ← 宿主半（窗口 / 桥 / 路由 / 超时）
        │ NDJSON over stdin/stdout（{id,method,params} → {id,ok,data|error}）
online/engine.py                                              ← 引擎（Python 子进程）
```

- **无监听端口、无 token、无常驻服务**：页面 ↔ 宿主 ↔ 引擎全在父子进程的管道里。
  引擎由宿主按需拉起，**随 Zotero 退出**；Zotero 关着时它只活在（尚未做的）外部启动器里。
- 引擎入口 `online/engine.py`，业务**一行不重写** —— 复用 `online/localserver.py` 与
  `offline\*.py` 的既有实现；localserver 只做了"把逻辑提成模块级函数"的搬运，**HTTP 行为一个字没改**。
- 传输两条路径：① **长驻管道**（首选，`kbAdminStartEngine` 拉起一次，之后每请求一行 NDJSON）；
  ② **短进程 + 单行管道**（拿不到管道时退化）。原来夹在中间的"短进程 + 结果写文件"已删
  （`engine.py` 从没实现过 `--once`，是永远失败的尝试）。

### 协议

请求 `{"id":1,"method":"kb.status","params":{...}}`；成功 `{"id":1,"ok":true,"data":{…}}`；
失败 `{"id":1,"ok":false,"error":"人能看懂的中文"}`；进度 `{"id":1,"event":"progress","data":{…}}`（同一 id 可多次）。

**两条引擎硬规矩**（`online/engine.py` 顶部）：

1. **stdout 只许出现协议 JSON**：真 stdout 收进 `_STDOUT`，`sys.stdout` 被换成 stderr，
   任何外来 `print` 都落到 stderr（宿主收进日志），绝不污染协议流。
2. **必须 UTF-8**：宿主设 `PYTHONIOENCODING=utf-8`，引擎自己也 `reconfigure(encoding="utf-8", newline="\n")`。

## 3. 十个页面（按 `order`）

| order | id | 标题 | 文件 | 契约文档 |
|---|---|---|---|---|
| 10 | `structure` | 知识库结构 | `admin-pages\structure.js` | W1（`ADMIN-PORT-PLAN.md` §5） |
| 20 | `experience` | 经验库 | `admin-pages\experience.js` | `ADMIN-API-W2.md` 批次 A |
| 30 | `suggest` | 分类建议 | `admin-pages\suggest.js` | `ADMIN-API-W2B.md` |
| 40 | `env` | 运行环境 | `admin-pages\env.js` | 批次 A |
| 50 | `parse` | PDF 解析 | `admin-pages\parse.js` | 批次 B |
| 60 | `quality` | 损坏查询 | `admin-pages\quality.js` | 批次 B |
| 70 | `meta` | 元数据 | `admin-pages\meta.js` | 批次 A |
| 80 | `outline` | 分节纲要 | `admin-pages\outline.js` | 批次 A |
| 90 | `advanced` | 高级 | `admin-pages\advanced.js` | 批次 B |
| 100 | `prompts` | 提示词 | `admin-pages\prompts.js` | 批次 A |

页面在 `admin.html` 里按顺序 `<script>` 引入（加载顺序无所谓），各自调
`KBAdmin.page({id,title,order,render})` 注册，壳按 `order` 排菜单（`content\admin.js::boot`）。
**一页一个文件、文件里不许 `import`**。

## 4. 方法总数与契约

- 引擎 `online\engine.py::METHODS` 现有 **53** 个方法（含 `ping`；2026-10-13 本轮实测计数；
  `docs\ADMIN-PORT-PLAN.md` 早期文档与交接清单里写的"51"是改动前的旧值）。
- 契约文档：`docs\ADMIN-API-W2.md`（批次 A：经验库 / 分节纲要 / 元数据 / 运行环境 / 提示词）、
  `docs\ADMIN-API-W2B.md`（批次 B：分类建议 / PDF 解析 / 损坏查询 / 高级）。两份都带
  **「实际口径」小节** —— 契约初稿没写细、或与库里真实数据模型对不上的地方以那一节为准。
- **新增方法两边都要改**：引擎加 method，宿主 `src\23-admin.js::KB_ADMIN_ROUTES` 加一行。
  路由表只认"HTTP 语义"（路径 + 方法），`GET /jobs/<id>`、`GET /experience/<id>` 这类带参路径
  在 `kbAdminMapRoute` 里单独匹配；查询串统一抽成 params。
- **超时**：页面 `get/post` 第 4 参数只管页面自己的等待（默认 30s）；宿主侧默认 **180s**，
  慢方法用 `KB_ADMIN_TIMEOUTS` 单独放宽（目前 `prompts.probe` → 300s，它内部模型超时就是 240s）。

### 长任务口径（用户 2026-10-13 拍板）

`kb.reindex` / `kb.digest` / `learn.scan` / `meta.fillScan(use_model=true)` / `mineru.parse` /
`quality.scan` / `quality.repair` 一律 **「派发即返回」**：请求一发出立刻回
`{"job":{"id":"job-…"}, …}`，**不等跑完**；进度与结果由调用方轮询 `jobs.get`（`GET /jobs/<id>`）
拿 `job.state / job.log / job.result`。

⚠ **进度不做事件转发**（宿主 `kbAdminForwardProgress` 只记日志）：页面靠轮询 `job.log` 就有文字，
**这是有意为之，不是漏项**。

## 5. 宿主桥的两个根因（本轮最贵的教训）

> 取证级细节在 `project\zotero-kb\接手.md` §5.17.5；这里只留结论与判据。

1. **页面必须跑在同进程**：`Zotero.openInViewer(页面地址)` 是错的 —— basicViewer 自带的
   `<browser>` 带 `maychangeremoteness="true"`，页面被切进远程进程
   （实测 `isRemoteBrowser=true`、`contentWindow=null`），整套桥无从谈起。
   **正解**：`openInViewer("about:blank#zotero-kb-admin")` 开壳窗口，在它里面
   `kbAdminMountFrame` **自建** `<browser>`（不设该属性）加载页面。
   对照：`20-kbview.js` 侧栏自建的 `<browser>` 本来就没这属性，一直是同进程。
2. **这类页面里 `window.parent === window`**（实测 `parentIsSelf:true`）—— 页面用
   `window.parent.postMessage` 发请求会**先被页面自己的监听吃到**，当场 reject 成
   「宿主半返回失败」。**正解**：宿主在页面 window 的 `postMessage` 上装**分派钩子**
   （`{__kba:1,op}` 形状直接接走、不再派发），并用 `document-element-inserted` /
   `content-document-global-created` **按 URL 认出页面、在它脚本执行前武装**
   （XUL `<browser>` **不触发 `load`**）。
3. 回包**必须**用 WindowProxy 的 `postMessage`（用 `wrappedJSObject.postMessage` 会
   `DataCloneError`）；页面里的 expando（如 `window.KBAdmin`）在 Xray 上看不到，要读
   `cw.wrappedJSObject.KBAdmin`。
4. 诊断键写在**知识库目录**的 `plugin-status.json`（不是插件目录）：
   `kbAdminBrowsers` / `kbAdminPageHook` / `kbAdminProbe` / `kbAdminEngine` / `kbAdminChannel`。
   ⚠ `kbAdminBridge` 是**粘性旧值**，别用它判断本次是否成功。

## 6. 引擎侧的两个硬教训

> 详细取证见 `project\zotero-kb\接手.md` §5.17.6。

1. **主循环不许阻塞读 stdin**：用阻塞 `readline()` 挂在宿主给的管道上，会让整个进程里
   任何重型 DLL 初始化（`import numpy` → OpenBLAS、onnxruntime）与之后起的子进程**永久卡住**
   （同一条命令在命令行里 0.3~1.8s）。修法：`_StdinLines` 用 **`PeekNamedPipe` 轮询**读
   （`_POLL_INTERVAL=0.02`）、行缓冲自己拼；stdin 不是 Windows 管道时自动退回阻塞 readline；
   两处起子进程补 `stdin=subprocess.DEVNULL`。代价：空闲 CPU ≈0.26% 单核、单条 RPC 平均 20.4ms。
2. **stdout 纪律**：见本文 §2（协议 JSON 只走 `_STDOUT`）。

## 7. 页面实现约定

契约文档里已有「页面实现的硬约束」（`ADMIN-API-W2.md` §页面实现的硬约束、
`ADMIN-API-W2B.md` 末节），这里补**走查后总结的完整口径**：

1. **只用 `admin.css` 的类与令牌**（`.card/.row/.grid/.btn*/.note-*/.n/.u/.mono…`）；
   禁 `import`、禁内联 `style` 与颜色字面量。
2. **按钮三档**：主 = `btn-primary`（蓝实底）；破坏 = `btn-danger`（红框 + `KBAdmin.dialog` 二次确认）；
   次 = `btn-subtle`（**浅边框**，默认就能看出是按钮）。
3. **表格**用 `ctx.table(cols, rows)`；数值列用 `.n` / `.u`（数字定宽右对齐、单位左对齐）；
   长文本格只留关键词、全文进 `title` 悬停。
4. **切页缓存**（用户两次反馈后定的第二版）：所有渲染过的页容器**常驻 `#view`、切页只切
   `hidden`**，**不摘挂 DOM**（摘挂会强制重排，观感像"又读了一遍"）；渲染中的页用 promise 去重
   （**反复点同一页/切回切出不会重复取数**）；成功标 `done` 永久直出、页面自己的「刷新」才是更新入口；
   **失败留错误条但清 `done`/promise**，下次切回来重试；缓存随面板窗口关闭消失（无需持久化）。
   → `content\admin.js::ensureBox/show/pageState`。
5. ⚠ **原生 `<select>` 在 Zotero 内嵌 `<browser>` 里打不开**（用户在分节纲要 / 运行环境 / 高级页
   实测；`content\admin.js` 顶部记录了 2026-10-13 真机实测结论）。口径：**选项 ≤4 个用并排按钮组**
   （`.btn-group`，选中态 `aria-pressed` + `.is-on`），长列表（模型、分类）用**自绘下拉**
   `KBAdmin.select()`。**页面里不许再出现原生 `<select>`**。
   机制层面的定论（为什么弹层不显示）本轮**未取得**：真机探针（会话目录
   `probe_native_select.out.txt`）只能证明"控件命中/可见性/DOM 都正常"，合成事件按 Gecko 设计
   本就不开原生下拉，所以**不写机制结论**。
6. 每页都要能在**预览桩**里看到（自己往 `preview-data` 加该路径的假数据，见 §9）。
7. **操作行 sticky —— 只钉"作用于选中行"的那一行**。PDF 解析 / 经验库 / 分节纲要三页的列表卡用
   `.sticktab`（作用域，`--stick-h: 30px`）+ `.stickrow`（`position: sticky; top: -sp-l; z-index: 2`，
   固定高度 + `nowrap` + **实心背景** `var(--card)`）；表头 `th` 让出这一行的高度
   （`top: calc(-1*var(--sp-l) + var(--stick-h))`，`z-index: 1`）—— 两层各占一个 `top`、严丝合缝，
   滚到中段「生成 / 强制重做 / 删除」仍在手边。**页面级条件行（筛选 / 状态 / 分类 / 关键词）故意不钉**：
   它与选中哪一行无关，而用户真实窗口可视区只有 ~560px，两行都钉会白吃掉顶部
   （反例与理由写在 `outline.js` 第 178-181 行的注释里）。挂载点：`parse.js::selRow`、
   `experience.js::opsRow`、`outline.js::opsRow`。
8. **页作用域 CSS 类**（为什么：一页的列宽/对齐**不能改到别页的表格** —— 规则写在 `content/admin.css`，
   但作用域类由**页面自己在 render 里加**）：

   | 类 | 作用页 | 干什么 |
   |---|---|---|
   | `.numtab` | 知识库结构 | 数值列整列右对齐 + 首/末列 `nowrap`（把宽度让给长说明列） |
   | `.parsetab` | PDF 解析 | 表头与第 4–7 列 `nowrap` / 右对齐 |
   | `.outlinetab` | 分节纲要 | 只钉「节数」那一列（右对齐 + 5em 宽；该列可排序，表头留了箭头位） |
   | `.sticktab` + `.stickrow` | PDF 解析 / 经验库 / 分节纲要 | 上面第 7 条那套「操作行 + 表头」两层 sticky |
   | `.split` | 提示词 | 左列表 + 右编辑器两栏：`flex-wrap: nowrap`、两栏都能收缩（`min-width: 0`、左栏定宽 220px） |
   | `.btn-group` | 全站（选项 ≤4） | 并排按钮组（互斥单选，选中 `.is-on` + `aria-pressed`） |
   | `.sel*`（`.sel` `.sel-btn` `.sel-cap` `.sel-val` `.sel-caret` `.sel-pop` `.sel-opt` `.sel-input`） | 全站（长列表） | 自绘下拉 / 可输入 combobox（原生 `<select>` 的替代，见第 5 条） |

## 8. 页面行为口径（别在界面里说假话）

> 这一节是**读码读出来的确切语义**（2026-10-13 走查）。一条总纪律：
> **界面文案要让用户一眼知道"点下去会不会真跑、会不会覆盖、会不会降档"** —— 按钮文案随状态变，
> 长任务只报"完成"不够。

### 8.1 「解析这一篇 / 重新解析」（PDF 解析页）

- 引擎对**单篇**固定发 `tools/kb_admin.py mineru parse --key <K> --tier basic --rebuild`、**不带 `--force`**
  （`engine.py::_run_mineru_parse`）。所以页面传的 `tier:"reparse"` 在单篇路径上**是空转** ——
  契约里 `tier` 是**动作**（missing/reparse），**解析档位**另用 `parser`，默认 `basic`。
- 是否真跑由**指纹**决定：`fingerprint = sha1(绝对路径|大小|mtime|档位|exe|MinerU版本)[:16]`
  （`offline/mineru.py::fingerprint`；带版本是因为 MinerU 升级后同一份 PDF 的产物也会变）。
  - **同档位（basic）且 PDF 未改** → 命中指纹、**秒回、产物原样不动**；`--rebuild` 只顺手重建这一篇的
    切片与向量。
  - **PDF 改过 / 原本不是 basic 档** → **真重跑并覆盖产物**，且档位**被改成 basic**。
- ⚠ `mineru.status_for()` 的 `stale` 只按"**当前记录的档位**"算指纹，**不反映"档位要换成 basic"** ——
  所以列表里显示"没过期"的一篇，点重新解析也可能真重跑。
- 因此界面：未解析 → `解析这一篇`（不弹确认）；已解析 → `重新解析` + **二次确认**，确认框里把上面
  两种结果**都写出来**（`parse.js::doParseSelected`）。

### 8.2 「生成 / 强制重做」（分节纲要页）

- **按节增量**：节指纹 = `sha1(标题 + "|" + 节内正文[:2000])[:6]`（`offline/digest.py::_sec_fp`）。
  没变的节**直接复用、零模型调用、秒回**；只有变了/新增的节才补问。
- 判"真干没干活"看返回里的 **`asked`（新问几节）/ `cached`（按指纹复用几节）**；
  `existed_before` 只说明"跑之前文库里有纲要"，**不等于这次跑了模型**（`localserver.py::do_digest`）。
- **整篇重问要用「强制重做」**（`force:true`，`POST /digest {key, force:true}`）；否则一篇已有纲要的
  文献点「生成」很可能"秒回、一节都没问"。界面已把文案随状态变：有纲要 → `生成（已有则跳过）`，
  否则 `生成`（`outline.js`）。
- ⚠ **吞错教训（已修）**：`digest.build` 找不到 MinerU 产物时**正常返回** `{ok:false, why:"没有 MinerU
  产物…"}`，而 `start_job` 只要函数不抛就把 job 记成 `done`（`localserver.py::start_job`）——
  于是 **"job done 但 result.ok=false"** 被旧界面显示成"完成"，把原因吞掉。
- 现在 `outline.js::runDigest` 先判 `res.ok === false` → **红条 + 原样给出 `why`**；
  且区分 cancel / 新问 / 全命中指纹三种尾巴。

## 9. 开发回路（跨会话复用）

### 9.1 管理页面预览（不开 Zotero 改样式）

**这是一套通用开发套路（用户 2026-10-13 认可为范例），单独成文：
[`DEV-PREVIEW-HARNESS.md`](DEV-PREVIEW-HARNESS.md)** —— 什么场景用、怎么搭、四个坑、两种验收、
配套的并行工作法，都在那一份里。面板这一侧的具体脚本：

脚本在**工作文件夹**（仓库外面那一层 `project\zotero-kb\`）的 `tools\admin-preview\`：
`render_admin_preview.py`（截图，人眼验收）、`render_scroll_preview.py`（滚动/sticky + `--dump-dom`，机器验收）、
`admin-preview.html`（冒充宿主半的 iframe 壳）、`preview-data\*.json` + `preview-data.json`（假数据）。

```powershell
# <仓库> = 本仓库根；<工作文件夹> = project\zotero-kb\（不进仓库）
& "<仓库>\.venv\Scripts\python.exe" `
  "<工作文件夹>\tools\admin-preview\render_admin_preview.py" `
  --page outline --size 1100x900 --out preview-outline.png
```

它用 Edge 无头渲染**真的** `content\admin.html`（真 CSS、真脚本），iframe 冒充宿主半按路径回假数据
（键 = 请求路径、值 = 引擎返回的 `data`），所以「改一次 CSS → 截一张图」几秒一轮。
`--page` / 临时文件名 / 截图高度三个坑见 `DEV-PREVIEW-HARNESS.md` §4。

### 9.2 Zotero 会话自动化

`project\zotero-kb\tools\zotero\zotero_session.py`（`start` / `quit` / `status`）：

- **开**：用户口径 —— AI 可自行启动，**不必先问**。
- **关**：**必须正常退出、永不强杀**（`Stop-Process -Force` / `taskkill /F` 会丢未保存笔记与标签页状态、
  可能中断 `zotero.sqlite` 写入）。三级降级、逐级尝试并如实报告用了哪级与耗时：
  ① 经插件任务通道在 Zotero 内 `Services.startup.quit(eAttemptQuit)`（等价菜单「退出」）；
  ② 主窗口 `WM_CLOSE`（pywin32，等价点 ×）；③ `taskkill /IM zotero.exe`（**不带 `/F`**）。
  `--force` 才会在第③级用 `/F`，属应急，默认关。
- `tools\sideload_plugin.py --cycle` = 退出 → 侧载 → 启动。

### 9.3 改动后必跑

改 `zotero-plugin\src\*.js` 后**必须** `python tools\build_bootstrap.py` 重新生成 `bootstrap.js`
（三道闸门：CI 的 `--check`、`check_plugin` 第一项、`pack_plugin` 打包前拒绝）。

## 10. 验收数字（2026-10-13 本会话实测）

> ⚠ 这是**面板搬进 Zotero 那一波**的门禁快照。之后线 2.5 又给引擎加了方法，**最新门禁数字与
> 进度一律以 [`ADMIN-PORT-PLAN.md`](ADMIN-PORT-PLAN.md) §10.3 / §10.3.0 为唯一出处**，
> 这里不再跟着改（避免两处各记一份）。

| 检查 | 结果 |
|---|---|
| `tools\test_engine_rpc.py` | **139 通过 / 0**（交接清单里写的 124 是改动前旧值） |
| `online\localserver.py --test` | **115 / 0**（旧 HTTP 服务仍在，W3 才下线） |
| `node tools\verify_bootstrap.js` | **149 / 0** |
| `python tools\check_js_syntax.py` | **48 文件 0 问题** |
| `python tools\audit_release.py` | **35 / 35** |
| `python tools\check_plugin.py` | 通过 |

版本：Zotero **10.0.6**、插件 **1.12.0**（**未升版**；本轮未提交、未发布）。
（⚠ 2026-10-15 补记：插件版本已升 **2.0.0** 并发布，见 `docs\RELEASE-2.0.0.md`。）

`plugin-status.json` 实测：`kbAdminPageHook=installed@…admin.html`、`kbAdminEngine=pipe: pid=…`、
`kbAdminChannel=hook seq=… op=/models`、`kbAdminProbe.parentIsSelf=true` —— 桥与引擎都在跑。

## 11. 待办

### W3（`ADMIN-PORT-PLAN.md` §2「三条迁移线」，切换前旧 HTTP 通道必须保持可用）

- [x] **线1 已完成（2026-10-13）**：DSH 侧取数（`/item-info`、`/weights`、`/col-status`）
      改成**直读 `index.db`**（DSH 自带 Node 24 的 `node:sqlite`，readOnly）。
      测试 `plugins\dsh-zotero-kb\tests\host-routes.test.mjs` 换成真 index.db，
      **全局 fetch 被换成"调用即抛"**当闸门（44/0）。⚠ 改了宿主半，**要重启 DSH 才生效**。
- [x] **线2 已完成（2026-10-13，真机验过）**：任务通道（`POST /task` → 插件轮询
      `GET /task`）改成**文件信箱** `<知识库>\mailbox\{requests,claimed,results}`
      （`online/taskbox.py` + `src\18-taskpoll.js`）。派发方 `dispatch_js.py` /
      `acquire.py` / `zotero_session.py` 全走信箱；应急回退 `KB_TASK_VIA=http`。
      老 HTTP 队列暂时保留（插件两条都消费，关它用 `zotero-kb.taskPollHttp`）。
      真机证据：信箱派 JS → `state=done channel=mailbox`；`kind=acquire` 干跑 3.0s 通过；
      `KB_TASK_VIA=http` 老队列仍 `state=done`；`zotero_session.py quit` 第①级
      恢复可用（2.9s 正常退出，不强化杀）。
- [ ] **线2.5（新增，才是线3 的真前置）**：插件内部 **12 个模块**的 `self.request(...)`
      改成走宿主半的引擎桥（入口 `kbAdminHostRpc` / `kbAdminHostRoute` **已就绪并真机验过**）。
      逐个模块改、逐个真机验；缺的引擎方法照 `ADMIN-PORT-PLAN.md` §10.3 补，
      补了要扩 `tools\test_engine_rpc.py`。
- [ ] **线3**：`8765` 下线。**判据以 `ADMIN-PORT-PLAN.md` §10.5 为准** ——
      不是"三条线切完"，而是 §10.3 那张表（含插件自身 12 个模块）搬完或退役。
      `netstat` 确认无监听；用新任务通道派一次 JS 并拿到结果。

### W4

- [x] ~~Tk 面板（`tools\panels\*`、`tools\gui.py`）在功能对齐后退役~~ → **2026-10-14 完成（W4）**：
      三样（`tools\panels\*`、`tools\gui.py`、`scripts\0-panel.vbs`）已删，备份在
      `kb\backup-20261015-0059-tk-panel\tk-panel.zip`；现以**新面板**为准。删完
      `check_plugin.py` / `verify_bootstrap.js` / `audit_release.py` 要全绿

### 零散

- [ ] 检索**多查询**：与 MCP `kb_search` 对齐（一句话拆多个词分别检索再合并）
- [ ] 环境页**模型配置三层**：设计依据文档 `docs\MODEL-SELECTION.md` **由另一个 agent 负责**
      （`admin-pages\env.js` 与 `ADMIN-API-W2.md` 都已引用它；引用前先确认它已落盘）
- [ ] 高级页**翻页**（当前只有"每屏条数"）
- [ ] `service.restart`：HTTP 服务退场后这条要重新设计，本轮引擎侧返回占位
      `{started:false, note:"HTTP 服务退场后由宿主半管理生命周期"}`，UI 如实说明、不假装成功

## 12. 未核实 / 已知不一致

- **未在真机验过**：经验库编辑弹窗里再开一层 `<dialog>` 的嵌套用法；
  `<select>` 替代控件（并排按钮组 / 自绘下拉）在真机上的展开。
- **文档与代码不一致（已按代码更新）**：`ADMIN-PORT-PLAN.md` 原来写
  "Zotero `Subprocess` 只能拿退出码、stdout 是 `ignore`，stdio 引擎可能做不了" —— 已核实**前提是错的**：
  Zotero 10 / Gecko 140 的 `Subprocess` 根本不处理 `options.stdout`（传了被静默忽略），
  子进程 stdin/stdout 管道**永远都建**，返回值上 `proc.stdout` 是 `InputPipe`。
  `ADMIN-PORT-PLAN.md` §4/§7 已按此更正。
- 本轮**没有**跑 `tests\test_mineru_*.py`（会真拉起 MinerU 占满机器），所以 MinerU 相关的全量测试
  数字不在上面那张表里。

## 13. 相关文档

| 文档 | 讲什么 |
|---|---|
| [`ADMIN-PORT-PLAN.md`](ADMIN-PORT-PLAN.md) | 范围 B 的施工契约、三条迁移线、分波、不变量、风险 |
| [`DEV-PREVIEW-HARNESS.md`](DEV-PREVIEW-HARNESS.md) | **预览桩套路**（真页面 + 假宿主半）：什么场景用、怎么搭、四个坑、两种验收 |
| [`ADMIN-API-W2.md`](ADMIN-API-W2.md) | 批次 A 方法契约 + 实际口径 + 页面硬约束 |
| [`ADMIN-API-W2B.md`](ADMIN-API-W2B.md) | 批次 B 方法契约 + 实际口径 + 页面硬约束 |
| `project\zotero-kb\接手.md` §5.17.5 / §5.17.6 | 宿主桥与引擎卡死的取证级根因 |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 目录结构、构建与测试、发版流程、排错入口 |
