# W2 API 契约（管理页面 ↔ 引擎）

> 2026-10-13 冻结。**页面与引擎两边的 agent 都照这份写，不要各自发明字段。**
> 上游依据：`docs/ADMIN-PORT-PLAN.md` 的端点缺口清单 + 只读盘点报告。
> 协议细节（NDJSON / 进度事件 / 错误形状）见 `online/engine.py` 顶部注释与
> `content/admin.js` 顶部注释 —— 这里只列**方法名与数据形状**。

## 通用约定（两边都必须守）

- 页面只用 `KBAdmin.get(path, onProgress)` / `post(path, body, onProgress)`；
  路径是**HTTP 风格**（`/kb-status`、`/experience`、`/experience/12`…），
  宿主半 `src/23-admin.js` 里那张**映射表**把它翻成引擎方法名。
  → **新增方法时两边都要改**：引擎加 method，宿主映射表加一行。
- 长任务：`POST /xxx` 立刻返回 `{job: {id}}`（或第一条 `progress` 事件带 job id），
  之后 `GET /jobs/<id>` 轮询（`{job: {state, log, result, error}}`）。
  state: `running | done | failed`；`log` 是字符串数组。
- 列表一律返回 `{rows: [...]}`（不要裸数组，便于以后加分页字段）。
- 失败：`ok:false` + 中文 `error`。

## 批次 A（本轮做的 5 页）

> **「建议超时」列（2026-10-13 补）**：`KBAdmin.get/post(path, body, onProgress, timeoutMs)`
> 现在支持第 4 个参数（毫秒）。**留空/0 = 用页面默认（30s）**。
> 只有在**同步**调用里标了值的方法才需要传 —— 长任务（返回 `{job:{id}}` 那种）
> 的第一步是"派发"，秒回，**不要**给它们传大超时（真正慢的是之后 `/jobs/<id>` 轮询）。

| 页面 | 路径 | 方法 | 参数 | 返回 | 建议超时(ms) |
|---|---|---|---|---|---|
| 经验库 | `GET /experience` | `experience.list` | `{mode:"all"\|"query"\|"checkup"\|"pending", q?, limit?}` | `{rows:[{id,date,effect,source,refs:[{key,label}],question,summary,checkup?}], total, mode}` | `query` 模式 60000（首次要加载嵌入模型）；其余默认 |
| 经验库 | `GET /experience/<id>` | `experience.get` | — | `{id,date,effect,source,session,question,method,reason,refs:[{key,label}],tags,item_keys}` | 默认 |
| 经验库 | `POST /experience` | `experience.save` | `{id?,question,method?,reason?,effect,source?,refs?[],tags?[]}` | `{id,created:bool}` | 默认 |
| 经验库 | `POST /experience/delete` | `experience.delete` | `{ids:[…]}` | `{deleted:n, rolled_back_weights:m}` | 默认 |
| 经验库 | `POST /pending/mark` | `pending.mark` | `{ids:[…], status:"adopted"\|"dropped"}` | `{marked:n, adopted:k}` | 采纳时要写库+计权重 → 60000 |
| 经验库 | `GET /sessions` | `sessions.list` | — | `{rows:[{id,title,when,size_kb,scanned,scanned_n}], total}` | 默认（读会话目录，秒级） |
| 经验库 | `POST /learn` | `learn.scan` | `{sessions:[…]}` | 长任务 `{job:{id}}`（job.result = `{ok,sessions:[…],pending}`） | 派发默认；扫描本身在 job 里，几十秒到几分钟 |
| 分节纲要 | `GET /outline-list` | `outline.list` | `{filter:"all"\|"missing"\|"orphan", collection?:"<分类名>"}` | `{rows:[{key,title,year,sections,has_file,orphan,built_at,collections:[…]}], total, filter, collection}` | 默认（通常秒级） |
| 分节纲要 | `POST /outline/render` | `outline.render` | `{keys:[…]}` | `{rendered:n, skipped:[{key,reason}]}` | 120000（逐篇渲染，几十篇会慢） |
| 分节纲要 | `POST /outline/delete` | `outline.delete` | `{key}` | `{deleted:bool}` | 默认 |
| 分节纲要 | `POST /digest` | **复用** `kb.digest` | `{key, force?}` | 长任务 `{job:{id}}`（**派发即返回**；结果在 `GET /jobs/<id>` 的 `job.result`） | 派发默认（秒回） |
| 元数据 | `POST /metafill-scan` | `meta.fillScan` | `{use_model?,limit?}` | `use_model` 假 → `{rows,summary}`；真 → `{job:{id}}`（job.result 仍是 `{rows,summary}`） | 同步（假）120000；真则派发默认 |
| 元数据 | `POST /metafill-scan-cancel` | `meta.fillCancel` | — | `{cancelled:bool}` | 默认 |
| 元数据 | `POST /typefix-scan` | `meta.typeScan` | `{use_network?}` | `{rows:[{key,title,issue,suggest}]}` | 120000（`use_network` 真时逐条查 DOI） |
| 运行环境 | `GET /health` | **复用** `kb.health` | — | 见 engine.py | 默认 |
| 运行环境 | `GET /env-check` | `env.check` | — | `{items:[{name,path,ok,source}], sources, rejected}` | 30000（要真跑一次 `python --version`） |
| 运行环境 | `POST /env-config` | `env.save` | `{project_root?,python?,ollama?,mineru?,figures?}` | `{saved:true, check:{…}}` | 30000（写完要复检一遍环境） |
| 运行环境 | `GET /llm-config` | `llm.config` | — | `{provider,model,base_url,has_key,key_tail,ollama_host,services:[{key,base_url}]}` | 默认 |
| 运行环境 | `POST /llm-config` | `llm.save` | `{provider,model,base_url,api_key?}` | `{saved:true}` | 默认 |
| 运行环境 | `GET /models` | `llm.models` | — | `{ok,models:[{name,note}],recommended}` | 15000（Ollama 探测本身 3s 超时） |
| 运行环境 | `POST /llm-test` | `llm.test` | `{provider?,model?,base_url?,api_key?,timeout?}` | `{ok,provider,base_url,model,backend?,ms,output?,error?,status?,hint?,models?}`（**失败不抛**：`ok:false` + 原话 + 状态码） | **90000**（外部 API 真调一次生成；本地只查 `ollama list`） |
| 运行环境 | `POST /llm-discover` | `llm.discover` | `{provider?,model?,base_url?,api_key?,ollama_host?,refresh?,timeout?}` | `{ok,provider,base_url,source,models:[{id,name,note,context,maxtokens}],status,error,hint,at,cached}` | **30000**（引擎侧最多 12s；失败回落内置候选） |
| 运行环境 | `POST /service/restart` | `service.restart` | — | `{started:bool, note}`（服务自身不能被自己重启 → 走 `os.startfile`/wscript 重新拉起；**没有 8765 之后这条要重新设计**，本轮先返回 `{started:false, note:"HTTP 服务退场后由宿主半管理生命周期"}`） | 默认 |
| 提示词 | `GET /prompts` | `prompts.list` | — | `{rows:[{id,title,where,placeholders,system,user,is_custom}]}` | 默认 |
| 提示词 | `POST /prompts` | `prompts.save` | `{id, field:"system"\|"user", text}` / `{id, action:"reset"}` | `{saved:true, is_custom:bool}` | 默认 |
| 提示词 | `POST /prompts/probe` | `prompts.probe` | `{id, text?}` | `{ok, output, ms}`（进程内调模型，可能要几十秒 → 页面给"运行中"状态） | **300000**（模型侧超时是 240s，页面别用默认 30s） |

### 长任务：一律「派发即返回」（2026-10-13 用户拍板，取代旧的"阻塞到跑完"）

**`POST /digest`（`kb.digest`）与 `POST /reindex`（`kb.reindex`）都是派发即返回**：

- 请求一发出，引擎**立刻**回一条 `ok`，data 是
  `{"job":{"id":"job-…"}, "id":"job-…", "job_id":"job-…", "state":"running"}`；
  **不等任务跑完**，也不把进度当"同一 id 上的事件"往回灌。
- 进度与结果一律由调用方轮询 `GET /jobs/<id>`：`job.state`（running/done/failed）、
  `job.log`（每一行进度）、`job.result`（跑完后的结果）。
- 为什么必须这样：一篇学位论文的纲要要几分钟，而宿主半单条 RPC 的超时只有 180s
  —— 阻塞式会让页面收到超时错误、可任务其实还在跑（用户以为卡死，还可能重复点）。
  派发即返回 + 轮询 `/jobs` 正是把管理面板搬进网页的设计初衷。
- ⚠ **进度不做事件转发**：宿主半 `src/23-admin.js::kbAdminForwardProgress` 只把
  progress 记进日志，没有转给页面；页面靠轮询 `job.log` 就有进度文字。
  这是**有意为之**，别当成漏了（引擎侧同一句话写在 `_job_dispatch` 的注释里）。
- 引擎仍会顺手发一条"已派发…"的 `progress` 事件（与 `learn.scan` /
  `meta.fillScan` 一致），它只是给宿主日志留痕，页面不依赖它。

`learn.scan` / `meta.fillScan(use_model=true)` 同属这一类（派发即返回 + 轮询）。

### 批次 A 实际口径（以"页面已经在用的"为准，2026-10-13 引擎侧对齐）

这几处是契约初稿没写细、但页面已经按它们写的地方。**引擎按这里实现，页面不用改。**

1. `GET /experience` 的 `mode` 走**查询串**（`/experience?mode=all|query|checkup|pending&q=&limit=`），
   返回 `{mode,total,rows:[…]}`。列表行**额外**给了
   `method/reason/session/tags/item_keys`（页面画详情不必再发一次 `GET /experience/<id>`；
   标注为"可选优化"，不看它们也行）。
2. `GET /experience/<id>` 同时给 `effect` 与 `outcome`（两个字段名页面都认）。
3. `POST /experience` 的 `refs` 传的是 **key 字符串数组**（`["7QMNVEF3"]`）；
   **读回来**的 `refs` 是 `[{key,label}]` —— 一来一回形状不同，是有意的（写入只要 key）。
4. `POST /pending/mark` 的 `adopted` **不只是改状态**：引擎会先把这条草稿
   `experience.add_experience` 写进经验库**并计入关联文献权重**，再把
   `pending.jsonl` 里的状态改成 `approved`（`dropped` → `rejected`）。
   返回 `{marked, adopted}`（`adopted` 是真正入库成功的条数，可能小于 `marked`：
   小模型给的 `outcome` 不合法的那种会被跳过、留在清单里）。页面不许写业务。
5. `checkup` 允许字符串或数组；引擎目前给的是**字符串**（用「；」连接）。
6. `learn.scan` 的 job 结果形状是 `{ok, sessions:[{session,ok,returncode|error}], pending}`。
   它在 job 线程里逐会话起 `python offline/learn.py scan --session <sid>` 子进程
   （与 Tk 面板同一条路），stdout 逐行进 `job.log` 当进度。
7. `meta.fillScan` 的行**沿用 localserver 既有形状**（页面 `countOf` 两种都认）：
   `{key,title,item_type,missing:[…],n_high,n_low,high:[…],low:[…],notes:[…],
   fulltext_source,skipped:[…],error?}`；聚合字段同时留在**顶层**（`total/
   missing_total/scanned/elapsed/canceled/high_total/low_total/per_field/
   excluded_high/apply_plan/…`）**和** `summary` 里（`{rows,summary}` 契约不丢，
   页面读顶层也不丢）。`use_model=true` 时改成长任务 `{job:{id}}`，
   `job.result` 才是上面那份。
8. `meta.typeScan` 的行翻译成契约的 `{key,title,issue,suggest}`（`issue` = 原
   `signals`/`notes`，`suggest` = `fixes` 翻成"标题/类型：现在 → 改成"），
   另外带上 `item_type`/`suspect` 供悬停说明。
9. `outline.list`（2026-10-13 加，供"分节纲要"页按分类筛选/显示）：
   · 每行多一个 **`collections`**（该文献所属 Zotero 合集名数组，来自
     `items.collections`；没归类就是 `[]`；孤儿行也是 `[]`）；
   · 多一个**查询参数 `collection`**（空 = 全部分类）：只留 `collections` 里
     含该名字的条目（精确匹配）。它与 `filter` **正交**（先后各筛一遍，取交集），
     顶层会把它原样回给页面（`{…, filter, collection}`）。
   · 分类清单页面另取 `GET /collections`（`advanced.collections`），引擎不重复给。
10. `POST /llm-test`（2026-10-13 加，页面上的「测试连接」）：`provider=ollama` 只查
    `ollama list`（便宜）；`provider=openai` **真调一次** `/chat/completions`
    （`judge.generate`，json_mode=False，引擎侧只等 45s）。失败**不抛** —— 回
    `{ok:false, error, hint}`，把服务端原话给用户看（"api_key 不对或已过期…"）。
    ⚠ 旧版页面拿 `GET /models` 当"测试连接"：接了外部 API 的用户会看到"✓ 通了：
    本机 N 个模型"——一次外部请求都没发过（假阴性）。
11. `POST /llm-config`（`llm.save`）**加了两条白名单校验**（2026-10-13）：
    · `provider` 只认 `ollama` / `openai`（`openai-compatible` / `openai_compatible`
      / `api` 会**归一成 `openai` 再存**）；写别的当场 `ok:false` —— 因为
      `judge.generate()` 只认这两类，存一个 "deepseek" 进去会被**当成 ollama**
      走本机（静默走错后端）。
    · `base_url` 非空时必须以 `http://` 或 `https://` 开头（把漏协议的当场挡下）。
12. `POST /llm-discover`（2026-10-13 加，页面的「刷新模型列表」/进页面时用）：模型目录
    的**三层兜底**（设计依据见 `docs/MODEL-SELECTION.md`）——
    ① `provider=ollama` → 本机 `GET /api/tags`（比 `/v1/models` 多给
    `parameter_size`/`quantization_level`）；`provider=openai` →
    `GET {base_url}/models`（`Authorization: Bearer <key>`；**两种返回体都认**：
    `{data:[…]}` 与 `{models:{<id>:{…}}}`；容量字段别名归一化
    `contextWindow/context_window/context_length/max_input_tokens/limit.context`；
    **最多读 4 MiB**、单条坏数据跳过）。
    ② 探测失败 → **内置候选**（照抄 DSH 的静态目录，见 engine.py 的
    `_BUILTIN_MODELS`；按 base_url 的 host 或模型名认出是哪一家）。
    ③ 永远留手填（页面那一格是可输入的 combobox）。
    `source` 四档：`ollama` / `remote` / `builtin` / `none`（连内置表都没有 →
    页面写"按服务商文档填其模型 id"）。结果按 `(provider, base_url)` 在**引擎进程里
    缓存 10 分钟**（`refresh:true` 绕开）—— 打开下拉不许打网络。
    `llm.config` 另给 `services:[{key,base_url}]`（来自 `judge.KNOWN_BASE_URLS`），
    页面据此把「外部 API」与「自定义」分组、并显示认得的服务商。

批次 B（分类建议 / PDF 解析 / 损坏查询 / 高级）的方法表在开工前另开一节补，
**不要提前实现**（免得字段漂移）。

## 页面实现的硬约束

1. **只用 `admin.css` 的类**（`.card/.row/.grid/.btn*/.note-*/.num/.n/.u/.mono…`），
   不许写颜色字面量，不许写内联 `style="color:…"`。
2. **按钮三档**：主操作 `btn-primary`、破坏性 `btn-danger`（必须配
   `KBAdmin.dialog` 二次确认）、次要 `btn-subtle`。
3. **表格**：用 `ctx.table(cols, rows)`；数值列用 `ctx.el` 里那种"数字+单位"结构
   （`.n` / `.u`，见 `admin-pages/structure.js` 的 `cellNumUnit`）；
   长文本列用 `title` 悬停看全文、格子只留关键词。
4. **一页一个文件**，文件里不要 `import`（页面是顺序 `<script>` 加载的）。
5. 换行/缩进/注释风格照 `admin-pages/structure.js`（中文注释、说清"为什么"）。
6. 每页都要能在**预览桩**里看到（自己往 `preview-data.json` 加该路径的假数据）。

> **2026-10-13 收尾补白**（完整版见 [`ADMIN-PANEL.md`](ADMIN-PANEL.md) §7，套路见 [`DEV-PREVIEW-HARNESS.md`](DEV-PREVIEW-HARNESS.md)）：
> - **原生 `<select>` 在 Zotero 内嵌 `<browser>` 里弹层打不开**（用户实测）→ 选项 ≤4 个用**并排按钮组**、
>   长列表用**自绘下拉** `KBAdmin.select()`；**页面里不许再出现原生 `<select>`**。
> - **切页缓存**：页容器常驻 `#view`、切页只切 `hidden`（**不摘挂 DOM**）；渲染中的页用 promise 去重
>   （反复点同一页不重复取数）；失败留错误条但清 `done` 以便重试。
> - **按钮三档**的"次"是 `btn-subtle`（**浅边框**，默认就能看出是按钮）。
