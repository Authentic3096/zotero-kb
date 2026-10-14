# W2 批次 B API 契约（管理页面 ↔ 引擎）

> 2026-10-13 冻结。与 `ADMIN-API-W2.md` 同规矩（页面只用 `KBAdmin.get/post`；
> 路径 HTTP 风格；列表返 `{rows:[…]}`；长任务**派发即返回** + 轮询 `/jobs/<id>`；
> 失败 `ok:false` + 中文原因；进度靠轮询 `job.log`，**不做事件转发**）。
> 依据：只读盘点报告里"高级/分类建议/PDF 解析/损坏查询"的缺口清单。

## 方法表

### 分类建议（tab_ai）
| 路径 | 方法 | 参数 | 返回 | 超时 |
|---|---|---|---|---|
| `GET /papers` | `papers.list` | `{limit?}` | `{rows:[{key,title,year,first_author,collections[],fulltext_chars,in_kb}], total}` | 默认 |
| `POST /summarize` | `ai.summarize` | `{key}` | `{key, summary, ms}`（Tk 是 `judge.py summarize`） | 120s |
| `POST /classify` | **复用** `ai.classify` | `{key, feedback?, previous?}` | `{key, category, tags[], reason, raw?}` | 120s |
| `POST /apply-suggestion` | `ai.apply` | `{key, category, tags[]}` | `{ok, applied}`（**写 Zotero 条目**，页面必须二次确认） | 120s |
| `POST /maintain` | `maintain.run` | `{task:"stats"\|"views"\|"collections"\|"judge-status"\|"sync-skill"}`（`collections`= 分类分布统计，`Searcher.collections`；页面「只看分类分布」发它） | `{task, ok, output}`（`output` 是子进程输出文本） | 120s |

### PDF 解析（tab_parse）
| 路径 | 方法 | 参数 | 返回 | 超时 |
|---|---|---|---|---|
| `GET /mineru-status` | `mineru.status` | — | `{probe:{ok,exe,why}, rows:[{key,title,tier,fulltext_src,pages,figures,elapsed,artifacts_size,built_at}], total}` | 60s |
| `POST /mineru/parse` | `mineru.parse` | `{tier:"missing"\|"reparse", key?}` | 长任务 `{job:{id}}` | 派发默认 |
| `POST /mineru/clear` | `mineru.clear` | `{key}` | `{cleared:bool, freed_bytes}`（**删产物**，页面二次确认） | 120s |

### 损坏查询（tab_quality）
| 路径 | 方法 | 参数 | 返回 | 超时 |
|---|---|---|---|---|
| `GET /quality-stats` | `quality.stats` | — | `{chunks,figures,item_health,rows:[{key,title,n_bad,n_suspect}]}` | 60s |
| `POST /quality-scan` | `quality.scan` | `{use_model?}` | 长任务 `{job:{id}}`；`job.result = {items:[{key,title,issue,detail}], summary}` | 派发默认 |
| `POST /quality-repair` | `quality.repair` | `{keys?[]}` | 长任务 `{job:{id}}` | 派发默认 |
| `GET /quality-detail` | `quality.detail` | `?key=` | `{key, title, rows:[{page,kind,detail,snippet}]}` | 60s |
| `POST /config/figures` | `config.figures` | `{on:bool}` | `{saved:true, figures:bool}`（写 `kb-location.json`） | 默认 |

### 高级（tab_advanced）
| 路径 | 方法 | 参数 | 返回 | 超时 |
|---|---|---|---|---|
| `POST /search` | **复用** `kb.search` | `{query, limit?}` | `{rows:[{key,title,score,snippet}]}` | 60s |
| `POST /weight` | **复用** `kb.weight` | `{key, pinned?, manual?, note?}` | `{ok, weight}` | 默认 |
| `GET /weight-list` | `advanced.weightList` | `{limit?}` | `{rows:[{key,title,year,weight,pinned,note}]}` | 60s |
| `GET /collections` | `advanced.collections` | — | `{rows:[{name,n}]}` | 60s |
| `POST /zotero-sync` | `advanced.zoteroSync` | `{action:"check"\|"authorize"\|"plan"\|"apply"\|"verify"}` | `{action, ok, output}`（`apply` 写 Zotero，页面二次确认） | 180s |
| `POST /plugin` | `advanced.plugin` | `{action:"build"\|"sideload-status"\|"sideload-install"\|"sideload-remove"}` | `{action, ok, output}`（`build` 会重打包 xpi，属开发用） | 300s |
| `POST /zotero-upgrade` | `advanced.upgrade` | `{action:"check"\|"backup"\|"verify"}` | `{action, ok, output}` | 300s |

## 三条硬口径

1. **`output` 一律是"子进程的原样文本"**：这些动作在 Tk 面板里是 `python xxx.py` 的输出，
   页面把它放进一个**等宽只读区**（`.mono`），不要试图解析成结构化字段（解析必然漂移）。
2. **写 Zotero 的三条必须二次确认**：`ai.apply`、`advanced.zoteroSync(apply)`、
   `advanced.plugin(build/sideload-install/sideload-remove)`；**只读的 `sideload-status` 不确认**
   （看个状态还要点确认是折磨用户 —— 2026-10-13 页面作者按此实现，我认可）；`mineru.clear`、`quality-repair` 是**删/改本地数据**，同样确认。
3. **删掉两个死入口**：Tk 面板里 `tools/diagnose_plugin.py` 与 `tools/zotero_js.py`
   两个文件**都不存在**，页面不要做这两个按钮（盘点报告已核实）。

## 批次 B 实际口径（2026-10-13 引擎侧落地后补 — **页面按这里读**）

> 契约初稿里几处没写细、或者与库里的真实数据模型对不上的地方，以本节为准。
> 引擎实现见 `online/engine.py` 的「W2 批次 B」一节。

1. **`output` 是"原样文本"，但**它**不是一定由子进程产生**：这几条脚本动作
   （`maintain.run` / `advanced.zoteroSync` / `advanced.plugin` / `advanced.upgrade` /
   `ai.apply`）在引擎里是**在引擎自己的解释器里跑脚本**（`runpy`，与
   `python xxx.py` 同一条 `__main__` 路径），不是起子进程。对页面**没有任何影响**：
   `output` 照样是一份原样文本，`ok` 照样是退出码是否为 0。唯一的例外是
   `maintain.run(sync-skill)`（PowerShell 脚本，只能起子进程）。
   > ⚠ 2026-10-13 晚更正：当时写这条的理由（"引擎起的子进程在本机起不来"）**根因已查明并修掉** ——
   > 是引擎主循环用阻塞 `readline()` 挂在 stdin 管道上，把所有 DLL 初始化（numpy→OpenBLAS、
   > onnxruntime）和以后起的子进程全锁住了（见 `online/engine.py` 的 `_StdinLines`）。
   > **契约口径不变**：`runpy` 保留（行为等价、省一次起进程），`output` 仍是原样文本。
2. **`quality.stats` 的 `n_bad` / `n_suspect` 是 `0/1` 标记，不是条数**：库里的
   `item_health` 是**文献级**一张表（切片级判据被 `check_chunks` 明确废掉了）。
   所以 `rows` 一行 = **一篇非 ok 的文献**；要"几篇坏"就求和。
   `item_health` 给的是**对象** `{checked, ok, bad, unsure, checked_at}`，
   另外**同时**给同名的扁平数字 `checked` / `bad` / `unsure` / `item_health_count`
   （两种读法都成立，页面挑一种）。`rows` 还额外带 `verdict` / `reasons[]` /
   `n_chars` / `checker`（画"为什么坏"的悬停说明）。
3. **`quality.detail` 的 `rows` 是"问题项清单"**，`kind` 三档：
   `verdict`（结论行，永远第一行）、`reason`（判据）、`sample`（正文样本）。
   `page` 由样本在切片里的位置**尽力推出**（推不出给 `null`，不猜）；
   `snippet` 是截到 400 字的正文。顶层另有 `verdict/n_chars/checker/
   fulltext_src/fulltext_chars/reasons/samples`。
4. **`quality.scan` 的 `job.result = {items:[{key,title,issue,detail}], summary}`**：
   `issue` = 第一条判据（短），`detail` = 全部判据连起来；
   `summary` = 聚合数字（`total/bad/unsure/healthy/elapsed/model_note/
   legacy_dropped/stale_dropped/per_sec/note`，有才带）。
5. **`quality.repair` 的 `job.result` 就是 `repair_chunks.repair` 的原样字典**
   （`ok/fixed/failed/skipped/bad_before/bad_after/deshifted/elapsed/details/
   skipped_detail/note/log`）—— 契约没规定这一条，翻译一次等于又多一份口径。
6. **`mineru.status`** 的 `figures` 就是产物里的**图片数**（`status_for().images`），
   `elapsed` 是解析耗时**秒**，`built_at` 是产物写入时间；`probe` 额外带
   `version` / `tier_ready` / `cached`（页面画"可用档位"那行要用）。
   行里另给 `parsed` / `stale` / `error`（状态列的三条判据）。
7. **`mineru.parse`** 的 `tier` 是**动作**（`missing` / `reparse`），**解析档位**是
   另一个**可选**参数 `parser`（`zotero|flash|basic|standard|advanced`，默认
   `basic`）；给 `key` 时只解析这一篇（并顺手重建它的切片与向量）。
   `job.result = {mode, key, parser, ok, returncode}`；进度看 `job.log`。
8. **`mineru.clear`** 返回 `{cleared, freed_bytes, key, path, note}`：
   `freed_bytes` 是**这次实际释放的字节数**；`cleared=false` 时看 `note`。
9. **`ai.summarize` 是只读的**（与 Tk 面板「生成要点」一致：不写
   `meta.ai_summary:<key>`），返回 `{key, summary, ms, title, points[], method,
   keywords[], model, written:false}` —— `summary` 就是把 `points` 用换行连起来；
   要落库得另说。
10. **`ai.classify`** 收 `key`（不是插件 HTTP 那条的 `item`）：引擎自己从索引库
    读 `title/abstract/tags` 再交给同一份 `judge.classify_item`。返回
    `{key, category, tags[], reason, raw, confidence, known_categories[], round, model}`；
    模型不可用/输出解析不了 → `ok:false`（`hint` 会拼进 error）。
11. **`ai.apply`** 失败**不抛**：回 `{ok:false, applied:false, output, returncode}`
    —— 子进程/脚本说的那句"没授权/连不上 Zotero"要原样给用户看。
12. **`maintain.run`** 的 `collections` 没有对应脚本：直接由
    `Searcher.collections()` 生成等宽文本（`任务名 | 分类 | N 篇`）；
    `stats`/`views` 走 `offline/maintain.py`，`judge-status` 走
    `offline/judge.py status`，`sync-skill` 走 `scripts/sync-skill.ps1`。
13. **`papers.list`** 的 `in_kb` 恒为 `true`（这些行逐条来自 `items` 表）——
    页面拿它当"在知识库里"的统一判据即可。
14. **`kb.weight`** 的 `manual` 接受数字或数字串；返回
    `{ok, key, pinned, manual, note, weight}`（`weight` 是算完的乘数）。
15. **`advanced.weightList`** 只列 `item_weight` 里**有行**的条目（与 Tk 面板
    一致）；行里另给 `manual/attempts/effective/ineffective/partial`。
16. **`advanced.collections`** 的每行同时给 `n`（契约字段）与 `items`
    （面板内部字段），值相同。
17. **`config.figures`** 写 `kb-location.json`，返回
    `{saved, figures, figures_enabled, path}`；**读**用 `config.figuresGet`
    → `{figures: bool}`（页面那个勾选框必须显示真值）。
18. **`kb.search`** 的 `rows` 是 `Searcher.search` 的 `hits` 摊平：`snippet` 取
    **第一条**片段，另带 `page/year/first_author/weight/ref/collections`。
    ⚠ **本机已知问题（不是本方法的 bug）**：`Searcher.search` 里的
    向量层（fastembed/onnxruntime 载入模型）在"引擎是宿主管道的子进程"时
    **不返回**（同一句在普通进程里 1.5 秒完成）。这是引擎侧的环境问题，
    `experience.list?mode=query` 那条同类路径也一样；已写进自检的 WARN。
19. **`outline.list`（批次 A 的 `GET /outline-list`，2026-10-13 按用户要求加）**
    多了一块"按分类"的能力（供分节纲要页显示分类列 + 分类筛选）：
    · 行里多一个 **`collections`**（Zotero 合集名数组，来自 `items.collections`；
      未归类与孤儿行都是 `[]`）；
    · 多一个查询参数 **`collection`**（空 = 全部分类；与 `filter` 仍然可用，
      两者**正交**：先按 filter 筛、再按 collection 筛，取交集），顶层回
      `{rows, total, filter, collection}`；
    · 页面上的分类下拉自己取 `GET /collections`（`advanced.collections`），
      引擎不重复给一份清单。
    ⚠ 这条端点的**权威表格在 `ADMIN-API-W2.md`（批次 A）**，那里已同步；
    这里记一笔是因为 W2B 的"实际口径"是页面作者查字段的落脚点。

## 页面实现的硬约束（与批次 A 相同）
只用 `admin.css` 的类；按钮三档严重度（主=btn-primary、破坏=btn-danger+确认、次=btn-subtle）；
表格用 `ctx.table`；数值列用 `.n`/`.u` 结构；长文本关键词 + `title` 悬停；
一页一个文件、不许 `import`；注释中文、说清"为什么"；每页都要能在预览桩里看到。

> **2026-10-13 收尾补白**（完整版见 `ADMIN-PANEL.md` §7）：**原生 `<select>` 在 Zotero 内嵌
> `<browser>` 里弹层打不开**（用户实测）→ 选项 ≤4 个用并排按钮组、长列表用自绘下拉；
> **切页缓存**（容器常驻 `#view`、只切 `hidden`、promise 去重）；**次按钮**是 `btn-subtle` 浅边框。
