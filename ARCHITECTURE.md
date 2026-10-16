# 架构总览：一套可复现的「文献知识库 → 大模型可用」系统

> 这份文档说明这个项目在做什么、各块怎么分工、关键实现为什么这么做。
> 答案：把"读过的文献 + 试过的方法效果"变成**大模型能随时调用**的能力
> （本项目里接的是 DSH，走标准 MCP，其它支持 MCP 的客户端同理），
> 并让这件事**可复现、可迁移、可长期用下去**。

---


> **本文件里的占位符**（照着替换成你自己的路径即可）：
>
> | 占位符 | 含义 | 怎么查 |
> |---|---|---|
> | `<项目目录>` | 你解压这个项目的目录 | 你自己放的地方 |
> | `<Zotero 数据目录>` | Zotero 存数据的地方 | Zotero → 编辑 → 设置 → 高级 → 文件和文件夹 |
> | `<知识库>` | 知识库目录，默认是 `<Zotero 数据目录>\zotero-kb` | 设置 →「文献知识库」→「知识库位置」 |
> | `<KEY>` | 某篇文献在 Zotero 里的 8 位条目 key | 右键条目 → 显示条目 key（或看 `INDEX.md`） |

## 一、一句话架构

**三块各自的强项拼成一个闭环**：Zotero 管文献、Python 管重活、DSH 管推理与对话，
中间用**两个按需拉起的 stdio 进程**（MCP 服务器与无端口引擎）加一个**文件信箱**
把它们缝起来，再用**经验权重**让系统越用越准。

```
Zotero（文献的家：条目 / PDF / 标注 / 分类 / 阅读进度）
   │  只读快照（含 WAL 与 -shm）                       ↑ 插件写回
   ↓                                                   │
Python 知识库（<项目目录>\）
   ├ offline\    离线管道：读快照 → 清洗 → 切片 → 向量 → index.db
   ├ online\     在线检索：searcher（关键词 + 向量 + 经验权重）
   │             server.py（MCP / stdio）  engine.py（无端口引擎 / stdio）
   └ experience\ 经验层：每次尝试记一条，权重回灌到检索排序
   │                                    ↑
   │ MCP（stdio）                       │ stdio JSON-RPC（父子进程管道）
   ↓                                    │
DSH（推理与对话）                  Zotero 插件（Zotero 进程内）
   · 13 个 MCP 工具 + 4 个资源        · 新条目自动切片 + 分类建议
                                      · 权重列 / 两个列表列 / 任务轮询
                                      · 管理面板（Zotero 内的一个窗口）
                                      · 右键「发送到 DSH」→ 文件信箱
```

> **不监听任何端口**：管理面板是 Zotero 自己的一个窗口，页面与宿主半、宿主半与
> Python 引擎之间走进程内的消息与父子进程管道，**没有常驻服务、没有 token**，
> 引擎随 Zotero 起落。

---

## 二、为什么要分成三块（各自的不可替代性）

这不是为了"架构好看"，每一块都有**别处做不到**的事：

| 块 | 只有它能做 | 它做不了 |
|---|---|---|
| **Zotero** | 存 PDF、管理条目与标注、给用户熟悉的阅读界面 | 跑 Python 生态（PDF 解析/向量/模型） |
| **Python 知识库** | PyMuPDF 抽全文、fastembed 算向量、SQLite 全文+向量混合检索、调 Ollama | 拿到 Zotero 的实时条目事件；在 DSH 里说话 |
| **DSH** | 大模型推理、多轮对话、调工具、写代码 | 直接读 Zotero 库；常驻后台做重活 |
| **Zotero 插件** | 在 Zotero 进程内读写条目、加列、弹窗 | 跑重活；调 DSH 的进程内服务 |
| **DSH 插件** | 调 DSH 的 `sessionController` 等进程内服务 | 读 Zotero 库 |

**关键约束（决定了所有设计）**：

1. **MCP 是 DSH 读知识库的唯一正道** —— 不让模型去翻 sqlite。
2. **插件不跑重活** —— 模型、PDF、向量都在 Python 侧，插件只按需拉起引擎
   （stdio）或投递任务；插件自己只做 Zotero 进程内的事（读条目、加列、弹窗）。
3. **DSH 的进程内服务外部够不着** —— 所以"发到对话"必须由 DSH 插件做，
   外部（Zotero）只能通过中间层投递。

---

## 三、数据流（五条主链路）

### 链路 1：文献进库（离线管道）

```
Zotero 库 ──只读快照(含 -wal/-shm)──> zreader ──清洗/HTML转MD/按页切──> mineru/basic
                                                                        │ 逐页 markdown
                          ┌─────────────────────────────────────────────┤
                          ↓                                             ↓
                  edits.py 按页覆盖（人工修订层）  ←── 插件侧边栏「改这一页」
                          │  有效正文 = 修订 ⊕ 机器稿
                          ↓
                  chunks：父块(节/章,level=0) + 子块(300 字,level=1) + 全篇(depth=2)
                  （切片前过 L2 清洗 clean_for_index；见 A10/A11）
                          │
                          ↓
                  chunks_fts(FTS5) + 向量(bge-small-zh)   全文/档案 Markdown
                          └──────────┬───────────┘        <知识库>\fulltext|papers\*.md
                                     ↓
                              <知识库>\index.db  ← 单一事实来源
```

**要点**：
- 快照**必须**连 `-wal`/`-shm` 一起拷（Zotero 10 起启用 WAL，只拷主库会读到过时数据）
- 增量判定比 **`version` + `collections_sig`**（分类/标签改动不动 `version`）
- 全量重建约 2~3 分钟（含父子切块与全库向量），增量 0.3 秒
- **图片/表格进正文、图注只出现一次**：MinerU 可用时正文/图注/表格**全部**取自它，
  不再叠加 PyMuPDF 的那一份（同一张图的图注曾经出现两次）

### 链路 2：DSH 读知识库（MCP）

```
DSH 模型 ──调工具──> mcp__zotero-kb__kb_search
                        │
                        ↓
                  online/server.py（MCP stdio）
                        │
                        ↓
                  online/searcher.py
                        │
        ┌───────────────┴───────────────┐
        ↓                               ↓
   关键词路（FTS5 + 覆盖率）        向量路（cosine）
   权重 0.65                        权重 0.35
        └───────────────┬───────────────┘
                        ↓
              RRF 融合（各路先 min-max 归一化）
                        ↓
              经验权重上浮：最终分 = 融合分 × (1 + ln(权重))
                        ↓
                 返回带页码的片段 + 相关经验
```

**为什么这么设计**（都是实测踩出来的）：
- 中文没有空格，FTS5 的 unicode61 分词不行 → 用**短语召回 + 加权子串覆盖**（权重 = 长度²）
- 两路分数尺度不齐（向量只有 0.63~0.66 的 5% 波动，关键词有 6 倍差距）→ **各路先归一化**再 RRF
- 经验权重乘 `(1 + ln(w))` 而不是直接乘 → 避免一篇"被试过很多次"的文献垄断排序

### 链路 3：经验回流（越用越准）

这是整个系统**唯一会自我改进**的部分：

```
DSH 里试了某篇文献的方法
        │
        ↓ kb_experience_add
   experience 表新增一条
   （outcome: effective / ineffective / partial / unknown）
        │
        ↓ 权重公式
   权重 = 1 + 3×pinned + manual + 2×effective + 1×partial + 0.5×ineffective
        │
        ↓
   下次 kb_search 该文献上浮
        │
        ↓
   DSH 更容易再读到它 → 继续积累经验
```

**注意 `ineffective` 也给正权重**（0.5）—— "试过但不行"本身是有价值的信息，
不该被埋没。这是有意的设计。

### 链路 4：Zotero 与 DSH 之间的两条通道

```
① Zotero → 知识库（切片、分类建议、面板取数）
   Zotero 插件 ──stdio──> online/engine.py ──> convert.py / judge.py（Ollama）
   （插件不直接跑 Python：它按需拉起引擎子进程，用父子进程的管道说话）

② Zotero → DSH 对话（发送文献）
   Zotero 插件 ──写文件──> ~/.dsh/zotero-bridge/requests/*.json
                                    │
                                    ↓ DSH 插件轮询（1.5s）
                            sessionController.prompt()
                                    │
                                    ↓
                          文本出现在指定 DSH 对话里
```

**为什么 ② 用文件信箱而不是 HTTP**（这段弯路值得记）：
最初设计是"DSH 插件注册 HTTP 路由"，结果插件一直卡在
`pending (waiting for service: optional)` —— 因为 **desktop profile 里没有
`webServer` 服务**。而走 `/api` 也不行：`connection` 的类型定义明确写着
它服务的是 *"trusted, authenticated requests"*（本机外部程序一律 401）。

文件信箱的三个好处：**不依赖任何 DSH 服务**（只用 Node 内置 `fs`）、
**不占端口**（无冲突、无防火墙问题）、**可离线排查**（请求和结果都是文件）。

### 链路 5：DSH 找新文献 → Zotero 抓进库

```
① DSH 侧（搜索与呈现，**都在对话里**）
   web_search 找候选 → Crossref/OpenAlex 核 DOI（标题对不上就扔）
   → kb_paper_info 补关键词/OA/被引
   → 在对话里列候选表（**标题是 doi.org 链接**，用户自己点开看原文）
   → ask_user_question(multi_select) 勾选

② 派发（勾完就抓，不再二次确认）
   kb_acquire(wait=False) ──写文件──> <知识库>\mailbox\requests\
                                          ↓ 插件轮询信箱领取（旧 HTTP 队列是兜底）
   DSH 侧轮询 kb_acquire_progress ←── 作业进度（每篇一个任务，粒度到"第 2/4 篇"）

③ Zotero 进程内（**关键：网络身份与 resolver 都在这里**）
   Translate.Search（DOI Content Negotiation）→ 建条目
   → 自带 resolver 找 PDF（doi 落地页 → url → Unpaywall → PMC → 自定义）
   → 本地模型分类 + 打标签（一批一个汇总框）

④ 收尾
   Zotero 把结果回传 → DSH 呈现 → kb_reindex 补抽（新文献立即可搜）
```

**为什么抓取必须放在 Zotero 进程里**（这条决定了整个形态）：

1. **网络身份** —— 请求从 Zotero 发出，**校园网出口 IP、学校订阅、出版社会话全都天然生效**。
   在 Python 侧下载就把这层身份丢了（所以 `fetch-pdf.ps1` 只当兜底）。
2. **resolver 更全** —— `Zotero.Attachments.addAvailableFile` 是 Zotero **自带**的
   "查找可用的 PDF"，一整套解析器，自己写爬虫不可能更全。
3. **元数据更准** —— 走 Zotero 的翻译器生态，比解析搜索结果网页可靠。

**为什么派发与执行要拆开**：MCP 工具是"一次调用阻塞到返回"的，抓一篇要十几秒、
三篇要一分钟 —— 不拆的话那段时间对话里**一片空白**，用户分不清是在跑还是卡死了。


---

## 四、关键功能的实现方式

这一节回答的是「**具体是怎么做的**」。每条结论后面括号里是源码依据
（`文件:行号`），可以对着代码读。行号会随版本漂移，找不到时按函数名搜。

### A. 离线管道：Zotero → 知识库

入口是 `convert.build()`：建表 → 读 Zotero → 逐条写 Markdown → 切片 → 建索引
→ 算向量 → 写 MANIFEST（`offline/convert.py:41`）。

#### A1. 正文从哪来：Zotero 全文缓存优先，PyMuPDF 兜底

**读的是快照，不是直连**：先把 `zotero.sqlite` 连 `-wal`/`-shm` 一起拷到
`<知识库>/.cache/zotero-snapshot.sqlite`，之后全程只读副本
（`offline/zreader.py:53-77`）。

> **为什么必须带 `-wal`**：Zotero 10 起用 WAL 模式，新写入先落 `-wal`，
> **只拷主库会读到过时数据** —— 实测本地 API 已经改好 33 篇分类、reader 仍读到旧值
> （`offline/zreader.py:40-51`）。也试过 `sqlite3.Connection.backup()`，
> 但 Zotero 开着时会被锁死 5 分钟以上，所以退回文件级拷贝（`zreader.py:47-48`）。

正文候选**只认 PDF 附件**；网页剪藏的 HTML 缓存刻意不要（正文里混满网页模板文字，
`offline/zreader.py:368-377`）。没有 PDF 的条目整条不收，收尾会列清单
（`offline/convert.py:188-202`）。

两条路的优先级（`fulltext_for`，`offline/zreader.py:320`）：

| 步骤 | 做什么 | 依据 |
|---|---|---|
| ① 缓存 | 读 PDF 同目录的 `.zotero-ft-cache`，UTF-8、按 `\f` 分页；先过长度关（任一行 >100 字符） | `zreader.py:388-400` |
| ② 体检 | 过 `cache_verdict` 判质量（见 A5）；判 `ok` 且非 deep 模式 → **直接用** | `zreader.py:346-354` |
| ③ 兜底 | PyMuPDF 逐页 `page.get_text()`（进口名兼容 `pymupdf`/`fitz`） | `zreader.py:411-421` |
| ④ 仲裁 | 两条都有时，先问本地小模型"哪个更像论文正文"；模型说 `same` 或没结论时按规则裁：`ok` 留着，`bad`/`unsure` 从严走 PyMuPDF | `zreader.py:424-442` |

两条路**共用同一个出口** `_finish` —— 页眉过滤与偏移还原对两条都生效，
避免"换个来源就换一种文本"（`offline/zreader.py:361-366`）。
来源会记成 `zotero-cache` / `pymupdf` / `zotero-cache+fix+29` 这类形式，
所以下游判断必须用 `startswith`（`offline/convert.py:211-226`）。

#### A2. 清洗：页眉页脚、断词、HTML

- **PDF 文本**（`clean_text`）：归一换行与空白、去掉 CJK 字间空格、接回
  `research-\ners` 式断词、丢掉纯页码行与 ≤2 字符的符号行、压缩连续空行
  （`offline/zreader.py:549-568`）。CJK 去空格的判据是"空格两侧都是 CJK 或中文标点"，
  循环替换以处理被逐字拆开的标题；英文空格保留（`zreader.py:525-546`）。
- **页眉页脚**（`drop_running_headers`）：在**按页文本的行位置**上做 —— 缓存里没有
  坐标，所以用"每页前 2 行 / 后 2 行"近似边缘带（`zreader.py:751-754`）。
  归一化 = 数字全换成 `#` + 压空白 + 转小写（`zreader.py:741-743`）。
  三条判据**同时**满足才删：落在边缘带内、归一化后在 ≥35% 的页上重复、长度 ≤300 字符；
  页数 <3 或单页 <4 行直接跳过（`zreader.py:746-794`）。
  只出现一次的首页 DOI / 版权行抓不到 —— 这是已知取舍。
- **HTML→Markdown**：只用于 Zotero 笔记（`html_to_markdown`）。
  `data-attachment-key` 的内嵌截图换成一行占位说明并计数，其余 `<img>` 删除；
  `a→[文本](href)`、`h1-6→#`、`strong/em/code`、`li→-`、`br/p`，
  认不得的标签一律剥掉（`zreader.py:808-864`）。
- **图注与表格**（`offline/figures.py`）：**全程不调模型**（视觉模型实测输出垃圾，
  `figures.py:1-34`）。图注靠 `图/圖/Fig./Figure + 编号` 正则、表格靠
  `find_tables()` 转 Markdown，并过滤行列数过小、填充率 <0.3 的误检
  （`figures.py:40-53, 201-237`）。两条关键判据防"吞正文"：
  先判首行是否自足（≥22 字或以 `])）】` 收尾），再只在明确像续行时才接折行
  （`figures.py:95-157`）。结果写进 `figures` 表，**并按页渲染进
  `fulltext/<key>.md` 的该页末尾** —— 这样 AI 读那一页时直接看见"这里有个图叫什么"
  （`offline/converter.py:248-298`）。

#### A3. 切片：按段落切，中文句号那个坑用零宽断言解

参数：目标 1000 字符、重叠 150、最小 120（`offline/converter.py:41-43`）。
按**段落**（空行分隔）切，超长段落先按句子拆，再贪心堆积；
跨块时把上一块尾部 150 字符接到新块开头（`converter.py:79-121`）。

```python
parts = re.split(r"(?<=[。！？；.!?;])\s*", para)
```

> 注释里写着为什么：若写成 `(?<=[。！？])\s+`，**整个中文段落会被当成一句**、
> 退化成几千字符的巨块（`converter.py:53-62`）。中文标点后不补空格、英文后补一个。

页码**不在切片文本里**，而是 `chunks.page` 列 + `seq`（`offline/schemas.py:734-742`）；
只有全文文件里才有 `## p.N` 锚点，供在线侧按页返回（`converter.py:289-292`）。

#### A4. 向量化

`fastembed` 的 `TextEmbedding`，默认模型 `BAAI/bge-small-zh-v1.5`
（`offline/converter.py:355-376`）。**维度在源码里没有常量**，是写入时从
`vectors.shape[1]` 取的（`convert.py:397`）—— 实测 `512`。
模型缓存在 `<知识库>/.cache/models`，默认把 `HF_ENDPOINT` 指向国内镜像
（`converter.py:368`）。只补**缺向量**的切片（`LEFT JOIN ... WHERE e.chunk_id IS NULL`，
`convert.py:378-384`）；向量是增强项，算不出来只提示、不阻塞关键词检索
（`converter.py:357-375`）。

#### A5. 文本质量：两级准入 + 本地模型只做"二选一"

**规则层**（分母是**非空白字符**，`offline/schemas.py:989-1015`）：

| 常量 | 值 | 含义 |
|---|---|---|
| `CACHE_BAD_EXOTIC` | 0.10 | 异域码位（藏文/天城文/阿拉伯等）>10% → 确定坏 |
| `CACHE_BAD_CJK` | 0.005 | 非外文却 CJK <0.5% → 确定坏 |
| `CACHE_LATIN_FOREIGN` | 0.80 | 拉丁字母 >80% → 判为外文文献，不再用 CJK 判 |
| `CACHE_OK_EXOTIC` / `CACHE_OK_CJK` | 0.01 / 0.05 | 达标才算 ok |

判定顺序：`total<100` → bad；异域 >10% → bad；拉丁 >80% → ok；CJK <0.5% → bad；
异域 >1% 或 CJK <5% → **unsure**；否则 ok（`offline/zreader.py:634-659`）。
0.80 这个阈值是实测卡出来的：坏缓存 latin 59%~76%、真英文 87%~91%，取两簇中间
（`zreader.py:585-588`）。**不用 `item.title` 判语言** —— 本机有 5 篇 IEEE 论文
标题是中文（`zreader.py:637-643`）。

**本地模型只在两处参与，且都是"二选一"而不是打分**：

1. **入库取舍**：把两条结果各取前 1200 字符并排，问"哪个更适合作为论文正文"，
   只输出 `{"better": "A"|"B"|"same"}`（`zreader.py:662-707`）。
   为什么要对比而不是单边打分：单边问"这段是不是坏切片"实测 **60% 判坏**，
   抽查却发现都是正常正文（`zreader.py:667-671`）。
2. **入库后审计**：对 `unsure` 的文献判"整篇提取是否失败"，整篇均匀取 4 段 ×500 字符；
   prompt 里明确列出"公式/参考文献/表格/封面都不算失败"，拿不准判没失败
   （`offline/check_chunks.py:65-69, 115-145`）。模型调用失败时**保持 unsure
   并记录原因**，不假装查过（`check_chunks.py:260-274`）。

另有一条独立的**字符偏移还原**通道（`shift_fix`）：需三条同时成立才动手
（拉丁占比 ≥0.50、常见词率 <12%、存在某位移使常见词率提升 ≥10 个百分点），
还原后来源标记加 `+fix<位移>`（`schemas.py:1043-1049`；`zreader.py:467-474`）。

#### A6. 元数据补全（`offline/metafill.py`）

- **材料**：只读 PDF **前两页**并拼成一整段（刊头常跨页，逐页匹配会漏）
  （`metafill.py:167-169, 191-197`）。字段范围固定为
  `date / DOI / volume / issue / pages / creators`，刻意不碰标题与摘要（`metafill.py:165`）。
- **红线是"只填空、不覆盖"**：已有值的字段不进建议，而是进 `skipped` 并写明原因
  （`metafill.py:1552-1570`）。每条建议必须带**页码 + 原文片段**作为证据
  （`metafill.py:211-250`）。
- **置信度不是打分，是来源标签**：规则抽到标 `high`，模型抽到一律标 `low` ——
  因为小模型会编（实测把参考文献里的 DOI 当成这篇的），用户必须一眼看出哪个要复核
  （`metafill.py:37-40, 1639, 1685`）。模型给的证据还会被回正文核对，
  定位不到就标 `verified=False`（`metafill.py:1347-1376`）。
- **离线侧不写回任何东西**（模块红线，`metafill.py:21-22`），只由本地服务的
  `/metafill` 端点暴露建议；真正落库在 Zotero 插件里 —— 写前**再查一次**当前值仍为空，
  再 `setField` / `setCreators` + `saveTx()`
  （`zotero-plugin/src/08-metafill.js` 的 `applyMeta`）。

#### A7. 增量还是全量

判据是**两项都比**：`items.version` 与"分类/标签指纹" `collections_sig`，
任一不同就重建（`offline/convert.py:98-131`）。

> **为什么必须加第二项**：Zotero 改**分类或标签不更新 `items.version`** ——
> 实测重整 33 篇分类后，增量仍报"0 条需要处理"，库里的分类还停在旧值
> （`offline/schemas.py:724-727, 879-882`）。

配套的几个刻意设计：`--item <KEY>` 强制重建并**绕过**增量（元数据没动但正文提取坏了时，
增量会误判"没变化"，`convert.py:133-144`）；孤儿清理放在增量判断**之前**
（已删条目不在读取结果里，增量永远看不到它），且有"一条都没读到就不清理"的防误删保险
（`convert.py:80-96`）；`term_df` 每次从**全量** chunks 重算，否则增量跑一次会把
词元稀有度算成"只剩这一批"（`convert.py:364-369`）。

#### A8. 正文修订层（人工改过的正文优先，且永不覆盖 MinerU 原版）

用户在 Zotero 侧边栏「知识库预览 → 按页正文」里对着原文 PDF 发现解析错的地方，
点「改这一页」就地改。写入磁盘是 `<kb>/edits/<key>.md`（**只存被改过的页**，与 fulltext
同形的 `## p.N` 分节）+ 同名 `.json` 边车（每页记一个 `base` = 该页**机器稿**的 sha1）。

```
structured_content.json（原版，只读） ──> pages.json（机器稿）
                                              ⊕ edits/<key>.md（人工修订）
                                              ↓
                          有效正文 = edits.apply(key, pages)
                          ├─ fulltext/<key>.md   ├─ chunks / FTS / 向量
                          └─ items.fulltext_chars
```

- **唯一读写路径**是 `offline/edits.py`（`apply/save_page/restore_page/meta/save_draft`）；
  接缝在 `convert.py` 的「正文来源」之后、`item.fulltext_pages = pages` 之前一行 ——
  下游三处（fulltext、切片、字符数）一起生效，检索与视图都不用知道有修订层。
- 机器稿变了（重解析/换 PDF）而 `base` 不符 → **仍然用用户版本**，界面标「基于旧解析」
  （静默丢掉用户的修正比提示冲突糟得多）。
- 外部编辑器走 `.draft.md` 整份导出，存回时**逐页 diff**，只有真变了的页进覆盖
  （否则重解析后整篇被冻在旧稿上）。
- 服务端入口：`POST /doc-edit`（保存/恢复/导出草稿）→ `start_job` 跑
  `convert.py --item <KEY>`（带向量）→ 插件轮询 `/jobs/<id>` 后重绘。

#### A9. 父子切块（small-to-big）

`chunks` 自引用两层（不给 `parent_id` 声明外键：还原/批量插入的顺序会坑）：

| 层 | 内容 | 用途 |
|---|---|---|
| `level=0` 父块 | 节 / 章的**整节全文**（≤3000 字，超了按段落拆「（续 N）」） | 只作「命中子块时返回上下文」的载体；**不进 FTS、不建向量** |
| `level=1` 子块 | 节内 300 字、重叠 60，文本前缀挂 `section_path` | **FTS 与向量都建在这一层** |

- 分节（`converter.sectionize_pages`）吃**有效正文**（不是 structured_content），层级只从
  标题**编号**推（`第N章`/`1`/`1.1`/`1.1.1`）—— MinerU 的 `paragraph_title.level` 实测
  **全是 2**，照它推会把「0 引言」与「1.1 …」判成同级。
- `section_kind`：`body|review|refs|toc|appendix|ack`。目录/参考文献/致谢/附录**存库但不进
  FTS/向量**（开关 `ZOTERO_KB_INDEX_NONBODY=1`）；综述章（`review`）检索侧小幅加权
  （`ZOTERO_KB_REVIEW_BOOST`，默认 +10%）。
- 检索侧（`searcher`）：同一个 `parent_id` 只留**最佳子块**（避免一节里多块重复占分，
  开关 `ZOTERO_KB_PARENT_DEDUPE=0`）；命中子块时把父块正文截断 1500 字随结果返回
  （`context` 字段，`kb_search` 会带给模型；开关 `ZOTERO_KB_PARENT_CONTEXT=0`）。
- 归档/还原（`trash.py`）的快照带齐新列并按 `level` 排序：**父块先插**，否则
  `parent_id` 会悬空（还原后检索照样跑、只是上下文没了，属静默错误）。
- **这套参数是复测过的**（题集 v4 = 回归 9 题 + 切块专项 24 题，脚本不随仓库发布）：
  在**纯向量路**上子块参数对旧参数（1000 字/页）全胜（hit@3 +2、MRR +0.063、平均排名 −0.49）；
  在**关键词主导的融合路**上旧参数的覆盖率略好 —— 这是"关键词路偏长块"的真实损失。
  试过的补偿档 **K500**（子块 500 字）与 **K300+PFTS**（父块也进 FTS、命中只给该篇加分）
  **都没有采用**：前者 D 组纯向量路略差，后者把回归组 hit@1 从 6/9 打到 5/9（越线）。
  实验开关（**调试面，默认行为不变**）：`ZOTERO_KB_CHILD_TARGET` / `ZOTERO_KB_PFTS`（构建期）、
  `ZOTERO_KB_ROUTE` / `ZOTERO_KB_W_KW` / `ZOTERO_KB_W_VEC` / `ZOTERO_KB_PARENT_CONTEXT` /
  `ZOTERO_KB_PARENT_DEDUPE` / `ZOTERO_KB_REVIEW_BOOST` / `ZOTERO_KB_INDEX_NONBODY`（检索期）。
- 分节的正则要认「带空格的中文标题」：`参 考 文 献` 匹配不上 `参考文献` 会让参考文献被当正文索引
  （曾实测 9 篇中招）。

#### A10. 索引层 L2 清洗

`fulltext/<key>.md` 是 **L1**（给人看、给 AI 读，**保留引文上标与图表**）；切片与向量另吃一层
**L2 = `converter.clean_for_index(L1)`** —— 纯函数、**不写入磁盘**（L1 是契约、L2 是推导，用户改修订层时
两边自动一致）。`collect_chunks` 只对 **fulltext** 支清洗，**标注与笔记一个字都不动**。

删三类：①**引文型** `<sup>`（前一个字符不是拉丁字母/数字/`>`、且内容是纯数字/数字列表/方括号含全角）；
②正文 `[n]` 编号（含全角；先给 `$$…$$` 公式与规则①保留下来的 `<sup>` 上占位符再处理）；
③页首期刊版权块那类**独立行**（`http://` / `E-mail` / `Tel` / `ISSN` / `收稿日期` /
`中图分类号` / `DOI:` …）。**明确不动**：`<sub>`（513 个全是数学下标）、单位与数学上标
（拉丁 308 / 数字 36 / `>` 后 71）、公式、`## p.N`。收益如实记：标签只占字符 1.23%，
**剥上标是小收益**，真正的大头是版权块。消融（题集 v4，与 A9 同一套）：回归组 hit@1 与 MRR 不变。

#### A11. 摘要深度分层（`chunks.depth`）

`chunks.depth` 与 `level` **正交**（`level` 是"父/子"结构，`depth` 是"抽得多深"）：

| depth | 来源 | 说明 |
|---|---|---|
| 0 | 现有子块（level=1） | 逐段 |
| 1 | 现有父块（level=0） | 逐节 |
| 2 | **全篇概要块**（新） | 文本复用 `meta ai_summary:<key>`（即 `views/<key>.tldr.md` 的产物），没有就退回 `abstract`；都没有就**不建** |

全篇块是 level=1、可索引、进 FTS 与向量。检索侧开关 **`ZOTERO_KB_DEPTH`（默认开）**：
关掉时（=0）在**候选阶段**（FTS 的 SQL `LIMIT` 之前、向量 `argsort` 之前）就把 `depth>=2` 排除，
结果与"没有全篇层"**逐字一致**（消融复现用）；老库（没有 `depth` 列）不加任何过滤。

**为什么默认开**：用一套**全新留出集**（30 题，与 v4 的 9+24 题不重题）实测 ——
总体 hit@1 16/30→18/30、MRR 0.684→0.764、必命中召回 0.882→0.971；topic 组与 detail 组**两组都升**；
配对 6 好 0 坏、符号检验 p=0.031。加上 v4 回归组 hit@1 不降、MRR 0.731→0.759，结论是"只增不减"。

### B. 在线检索与交互

#### B1. 混合检索：两路怎么算分、怎么融合

**关键词路**（`online/searcher.py:305`）。中文没有空格，FTS5 的 `unicode61` 会把
连续中文切成**单字** token；于是把中文连续段当**短语**召回
（`build_match_phrases` 把「机器学习模型」变成 `"机器学习模型"`，
`online/query.py:136-148`）—— 短语查询恰好等价于"这些字相邻出现"。
不用单字 OR，是因为它会把只含一个字的段落全捞进来（`searcher.py:308-312`）；
短语命中不足 `MIN_PHRASE_HITS=5` 时才退到单字 OR 宽召回（`searcher.py:52, 368-398`）。

打分不是 bm25，而是**加权重叠覆盖率**：查询切成 1~4 字滑窗子串，
权重取子串长度的平方（「反演」4 分 vs 孤立的「反」1 分），
命中权重之和 ÷ 总权重 = `coverage`（`online/query.py:151-183`、`searcher.py:340-360`）。
排序键 `(-coverage, -strong, bm25)`，bm25 只做末位 tie-break
（中文单字区分度差、长文天然吃亏，`searcher.py:316`）。

**向量路**（`searcher.py:513`）。`fastembed`，模型名**优先读索引里的
`meta.embed_model`**，读不到才退回默认值（`searcher.py:140-141`）——
所以换了嵌入模型重建索引后，在线侧会自动跟着换。向量在**读入时**就按行归一化，
查询时一次点积即余弦（`searcher.py:492-499, 530-531`）；相似度取 `max(0, sim)`，
因为它后面当质量分用，负数会被当成"负相关证据"（`searcher.py:533-535`）。
模型与向量矩阵都懒加载，并用 `threading.Event` 做单线程交接
（ONNX 会话不可重入，并发喂输入会出难复现的错，`searcher.py:402-511`）。

**为什么必须归一化**：两路原始尺度不可比 —— 向量余弦实测挤在 0.63~0.66
（区分度只有 5%），而关键词覆盖率能差 6 倍。不归一化时"低区分度但绝对值大"的
一路会主导排序（实测查询「机器学习模型」的第一名被向量路带偏，
`searcher.py:553-557`）。做法是**组内 min-max**，整路同分时全给 1.0、
退化成纯 RRF（`_normalize_quality`，`searcher.py:569-596`）。

**融合**是加权 RRF（`RRF_K=60`），另乘**同路位置衰减**
`1/(1+0.35·pos)` —— 同一篇的第 4、5 个块只算增量证据（`searcher.py:540-567`）。
两路权重：关键词 0.65 / 向量 0.35（`searcher.py:55-56`），
因为关键词命中意味着"查询词真的出现在文里"，是主证据。

**聚合 → 过滤 → 权重**：块先按融合分聚合到**条目**，且**只有最好的 3 块计分**
（否则块多的文献凭空占便宜，`searcher.py:740-744, 793-797`）；再按分类 / 年份 /
知网结构化字段过滤（不传参时一次 SQL 都不多跑，`searcher.py:806-840`）；
最后乘经验权重。顺序是有意的：**权重只该影响排序，不该干扰召回**
（`searcher.py:8-10`）。

#### B1.1 结构化字段：展示 + 可选筛选，不动排序

Zotero 条目的 `extra` 里有一部分是**知网写进去的结构化字段**（中图分类号、
来源类型、收录标签、中文译名、学位专业…），由 `offline/extrafill.py` 解析成
知识库的 `item_extra` 表（构建时维护，条目删除靠外键级联），在三处用起来：

| 用在哪 | 形态 |
|---|---|
| 检索命中 | 每条命中带 `structured`（中图分类号 / 来源类型 / 收录标签 / 中文译名 / 学位专业）—— 英文标题的文献常常有中文译名，据此能确认"是不是用户说的那篇" |
| 筛选 | `kb_search` 可传 `source_kind`（journal / thesis / conference，也认中文）、`publication_tag`（EI / 北大核心 / CSCD…）、`clc`（分类号，支持 `TP39` 这样的大类前缀） |
| 展示 | `kb_item` 给提纯后的 `structured` + 原样照录的 `extra_fields`；tldr 资源多一行"结构化字段"；`kb_stats` 报覆盖率 |

**为什么不拿它给召回加权**（刻意的取舍）：加权动的是排序，而排序目前
只有零星的人工核对、没有回归集 —— 改坏了不会当场暴露。筛选只在明确说
"只要 EI 的""只要学位论文的"时改变结果集，不传参数时连一次 SQL 都不多跑，
对既有检索行为**零影响**。

只有 `extra` 里有这些字段的条目才筛得到，筛不到**不等于**不符合 ——
空结果时工具会明说这一点。`download`（下载计数，含义无法确认）只原样留存，
**不参与任何判断**。

#### B2. 经验层：系统里唯一会自我改进的部分

知识库里的信息分三层，同一个库：

| 层 | 内容 | 谁写 |
|---|---|---|
| 原文层 | PDF 正文、高亮标注、笔记 | 离线管道（只读 Zotero） |
| 结论层 | 元数据、摘要、分类、纲要 | 离线管道 + 本地模型 |
| **经验层** | 试过哪几篇、方法是什么、效果如何、失败原因、重点标记 | 使用者当场追写（`kb_experience_add`）+ 小模型事后从会话里补 |

`experience` 表的字段：`asked`（问题）、`context`（前提条件）、`item_keys`、
`method`、`outcome`、`reason`、`evidence`、`tags`、`source`、`session`、
`created_at` —— **只追加，永不覆盖**（`offline/schemas.py:783-796`）。

`outcome` 四种取值 `effective / ineffective / partial / unknown`，
MCP 工具层做集合校验，不合法或 `asked` 为空直接拒收（`online/server.py:550-555`）。

> **权重不是按 outcome 直接给分**，而是写经验时顺手 upsert 每篇的累计战绩
> （`attempts / effective / ineffective / partial`，`server.py:979-1002`），
> 检索侧再由计数经公式算出乘数：

```
raw    = base + 3.0*pinned + manual + 2.0*effective + 0.5*ineffective + 1.0*partial
weight = 1 + ln(raw)
```

`base` 是按发表年份的时新性：今年 1.05、15 年前及更早 0.95、缺年份 1.0
（`schemas.py:1196-1227`）。它加在 **log 里面**，所以"年份新"只值 ±5%，
而"有效一次"≈ +139% —— 刻意差一个数量级（`schemas.py:1256-1262`）。
`unknown` 只累加 `attempts`，不给任何奖励分。
`ineffective` 也给正分（0.5）：**"试过但不行"本身是有价值的信息**。

正文提取被判 `bad` 的条目，整篇再乘 0.25（留 0.25 而不是清零，是为了
"明确点名时还找得到"）（`searcher.py:58-67, 262-279`）。

**从会话记录里挖经验**（`offline/learn.py`）：读 DSH 的会话转录
`~/.dsh/sessions/**/session.v*.jsonl.zstd`，解压后逐行 JSON，
按"用户提问开头、聚合到下一个提问之前"切片段（`learn.py:82-163`）。
两道粗筛：先看有没有结果类信号词（有效/失败/收敛/误差…），
再看这段是否真的在"用文献做事"（调过 `kb_*` 工具，或文本里出现文献 Zotero 等词）
—— 第二道是后补的，因为不加它连"我是怎么搭这个知识库的"工程对话也会被抽成经验
（`learn.py:38-61, 186-200`）。抽出的结果**只进待确认区** `inbox/pending.jsonl`，
**绝不允许直接写经验表**，要通过 `learn.py mark --approve` 才入库（`learn.py:1-14`）。

#### B3. 权重的算法（精确公式）

权重是「这篇文献对我的价值」的**先验**，只在**融合之后**乘上去，不参与召回
（该出现的还是会出现，只是排序前后不同）。实现在 `offline/schemas.py`，
检索侧调用在 `online/searcher.py` 的 `weight_of()`。

**第一步：按发表年份取基础分**（`recency_base(year)`，范围 0.95 ~ 1.05）

| 情况 | 取值 |
|---|---|
| 今年或更晚（在线首发、预印本） | 1.05 |
| 15 年及更早 | 0.95 |
| 中间的年份 | 线性过渡：`1.05 + (0.95 − 1.05) × 年龄 / 15` |
| 年份缺失或明显不合理（< 1800 / > 2200） | 1.00（中性，不猜也不罚） |

基准是「当前年份」，**每次检索实时算、不落库** —— 跨年后同一篇的分数会自己往下走，
不需要重建索引。幅度只有 ±5%，刻意比经验权重小一个数量级。

**第二步：加上标记与经验**（`raw_weight`）

```
raw = 年份基础分
    + 3.0 × 重点标记（pinned，0 或 1）
    + 人工加减分（manual，在面板里填的）
    + 2.0 × 经验「有效」次数
    + 0.5 × 经验「无效」次数
    + 1.0 × 经验「部分有效」次数
```

没有经验记录的文献，`raw` 就等于年份基础分。

**第三步：取对数当乘数**（`weight_multiplier = 1 + ln(raw)`）

| 情况 | 乘数 |
|---|---|
| 无任何经验（只按年份） | 0.95 ~ 1.05 |
| 标为重点 | `1 + ln(4)` ≈ 2.39 |
| 经验有效 1 次 | `1 + ln(3)` ≈ 2.10 |
| 经验有效 3 次 | `1 + ln(7)` ≈ 2.95 |

取对数的用意是**增长递减**：不加 log 的话"有效 3 次"就是 4 倍，再多几次会垄断结果。
年份分是加在 log **里面**的，所以年份的影响被压到 ±5%，而"试过、有用"是 +139% ——
这是刻意的：年份只做同等相关时的微调，不盖过实证结论。

**第四步：乘到融合分上**（`online/searcher.py`）

```
最终分 = RRF 融合分（关键词 0.65 / 向量 0.35，RRF_K = 60，含质量分与位置衰减）
        × weight_multiplier
        × （正文提取不可信时 0.25，整篇降权）
```

- 融合里关键词路是主证据（查询词真的出现在文里），向量路补「换个说法」的召回；
  同一篇里第 4、5 个块按 `1/(1 + 0.35 × 位置)` 衰减 —— 后面的块只算增量证据。
- 「正文提取不可信」= 该篇 PDF 的字符偏移不可靠（字体编码坏等），整篇乘 0.25；
  面板「损坏查询」页能看到这些篇。
- 权重入口：Zotero 右键「标为重点」、面板「高级 → 手动调权重」；
  经验由 `kb_experience_add` 或面板「经验库」写入，每次尝试记一条，历史不覆盖。

#### B4. 本地小模型用在哪（只出建议，不写库）

后端两类（`offline/judge.py:36-63`）：本机 Ollama，或任何 **OpenAI 兼容**的
`/chat/completions`（deepseek / openai / dashscope / moonshot / zhipu / groq…）。
配置优先级"调用时传参 > `<知识库>\llm-config.json` > 环境变量 > 默认值"，
传参优先是为了让设置面板说了算（`judge.py:45-47`）。

**换后端不难，因为主流服务用的是同一套协议** —— 区别只是
**服务地址 + 模型名 + Key** 三样，代码一行不用改：

| 后端 | 适合 | 需要 |
|---|---|---|
| **Ollama**（默认） | 完全离线、不花钱、数据不出本机 | 装 Ollama + `ollama pull qwen3:4b-instruct` |
| **外接 API** | 不想本地部署；或机器跑不动；想用更强的模型 | 一个 API Key（可能还要充值） |

地址**不填也能推断**（填了模型名就够；这张表在两处有副本，
`offline/judge.py` 的 `KNOWN_BASE_URLS` 与 `zotero-plugin/settings.js` 的
`KB_API_PRESETS`，`tools/check_api_presets.py` 盯着两边一致）：

```
deepseek-chat      → https://api.deepseek.com/v1
qwen-max           → https://dashscope.aliyuncs.com/compatible-mode/v1
gpt-4o-mini        → https://api.openai.com/v1
moonshot-v1-8k     → https://api.moonshot.cn/v1
glm-4              → https://open.bigmodel.cn/api/paas/v4
llama-3.3-70b      → https://api.groq.com/openai/v1
```

自建的（vLLM / LM Studio / one-api）手填地址，注意有些服务要带 `/v1`。
**不支持** Anthropic Claude、Google Gemini 的**原生**协议（不是 OpenAI 格式），
要用得先过一个转换网关（one-api / LiteLLM）；OpenRouter 上有这两家，走它就能间接用到。

| 用途 | 怎么做的 |
|---|---|
| **分类建议** | 提示词把已有分类连同**篇数与标题样例**一起给模型做 few-shot，要求"只能从已有分类里选**一个**、原样照抄"；模型改写分类名时做一次宽松匹配，**只接受唯一命中**，含糊就宁可空（`judge.py:695-761`）。支持"跟模型对话调整"，但可选范围不变 |
| **打标签** | 出 3-6 个中文标签 + 一句话主题，只出建议不写库（`judge.py:513-531`） |
| **写摘要与要点** | 2~3 句概括 + 方法名 + 关键词，只出建议不写库 |
| **分类去重** | 中英文分类是否同一个交给模型 —— 嵌入模型跨语言几乎没区分力（`「机器学习模型」↔"machine learning model"` 只有 0.518，落在无关词噪声区） |
| **分节纲要** | 逐节写 2~4 句 + 3~6 条关键点（`offline/digest.py`，见 B17） |
| **元数据兜底** | 规则优先、纯规则时秒回；只有规则抽不到字段才调模型，所以做成同步接口（`localserver.py:431-441`） |
| **从会话里抽经验** | 先关键词粗筛，再让模型抽成结构化条目，**只进待确认区**（见 B2） |

**绝不交给小模型**：判断某个方法有没有效果（需要真实实验反馈，小模型会一本正经
地编）、决定该用哪篇文献（科研判断，留给主模型和使用者）。

**输出解析**：统一先剥 ``` 代码块、再 `json.loads`，失败就抠最外层 `{}`
（`judge.py:464-490`）；仍失败返回结构化错误而不是抛异常。
`generate()` 会把 HTTP/网络错误翻译成可操作的 hint（key 过期、
base_url 是否要带 `/v1`、连不上谁）（`judge.py:379-404`）。

#### B5. MCP 服务器

`online/server.py`，**stdio** 传输（由 DSH 的 `dsh-mcp-client` 拉起），
`instructions` 里写死用法约定：先搜后读、用完记经验、不写 Zotero
（`server.py:53-62`）。启动只打印统计、不读索引库，`kb()` 懒加载（`server.py:1125`）。

13 个工具与 4 个资源见本文档第八节。

#### B6. 本地 HTTP 服务（插件 ↔ Python 的旧通道）

`online/localserver.py`，`ThreadingHTTPServer` 只 bind **127.0.0.1**，默认端口 **8765**。

> **这个常驻服务已停用**：管理面板走 `online/engine.py`（stdio JSON-RPC）、
> 任务通道走 `<知识库>\mailbox\` 文件信箱。但是**代码一个字没删** ——
> `localserver.py` 里的业务实现仍是引擎与 MCP 的复用来源，
> 而 `localserver.py --test`（115 项）自带**进程内**测试服务、不需要 8765 在监听。
> 需要时可以用 `scripts\4-service.vbs` 把它起回来。

**token 机制**：优先环境变量 `KB_SERVICE_TOKEN`，否则读 `<知识库>\service-token.txt`，
文件不存在就生成并写入磁盘；按文件 mtime 缓存，**换了 token 不必重启服务**
（`localserver.py:87-122`）。校验用 `secrets.compare_digest` 定时比较（`:2034-2040`）。

> **一处值得抄的安全设计**：`GET /` 与 `/health` 免 token，并顺手把 token 回给调用方
> （方便插件自动配置）；但请求若带 `Origin`/`Referer`（只有浏览器会发）就**拒发 token** ——
> 挡住"恶意网页 fetch localhost 偷 token"。Zotero 插件用自带 HTTP 封装、不带 Origin，
> 正常通过（`localserver.py:2055-2090`）。

**任务队列**是"能在 Zotero 里执行一段 JS"的通道：只监听本机、必须带 token、
任务只留最近 30 条且**不写入磁盘**（`localserver.py:55-67`）。
`kind` 有 `js / cmd / acquire / attach`；领取是原子的 `pending → running`
并记 `fetched_by`（`:174-207`）。


服务还带**库变化监听**：每 20 秒看一次，发现新条目就切片并生成分类建议，
写进 `<知识库>\pending-suggestions.json`（`localserver.py:1859-1879`）。
`POST /shutdown` 用事件位 + 看门狗退出，刻意不用 `taskkill pythonw.exe`
—— 那会连管理面板和用户脚本一起杀掉（`:2519-2558`）。

**对照页（浏览器里那个兜底页）已经没有了** —— `online/docpage.py` / `online/docpage.html`
与那一组 GET 端点（`/doc`、`/doc-text`、`/doc-raw`、`/doc-page.png`、`/kb-image`）
都已删除，请求它们返回 404。对照改由阅读器右侧那一列承担
（与条目窗格列**共用** `content/kbview.html`，见 §B14）。

**`/doc-edit` 一族（GET 元信息 + POST 保存/恢复）仍要保留**：它是**正文修订层**的
入口，阅读器列 / 条目窗格列的「改这一页」都走它（唯一写路径是 `offline/edits.py`，
保存后轮询 `/jobs/<id>`）—— 删了就废掉整个编辑功能。当前调用走**引擎桥**
（`doc.get` / `doc.save`，与这两个端点同一份实现），老 HTTP 只是兜底。

#### B7. Zotero 插件的几个关键机制

插件是 **bootstrapped extension**（Zotero 7~10）。
**源码在 `zotero-plugin/src/*.js`，按功能分 26 个文件**；`zotero-plugin/bootstrap.js`
是它们拼出来的**生成物**（`tools/build_bootstrap.py`，xpi 里装的就是它）——
原因见「五、目录与归属」里那一段。

**启动是"分步执行 + 逐步记录"**：`registerPrefs → registerPrefObserver →
registerPrefPane → registerNotifier → registerWeightColumn → registerKbColumns →
startTaskPolling`，
每步成败进 `steps` 并写进状态文件 —— 因为 Zotero 的调试日志重启即失
（`src/00-core.js` 的 `startup`）。

> 工具栏按钮与右键菜单**在 `startup` 里也要注册一次**：实测 `onMainWindowLoad`
> 在 Zotero 启动时**从未被调用**（那时主窗口已存在），该钩子只对"插件启动后新开的窗口"
> 触发（`src/00-core.js` 的 `startup` 里那段重试）。

**「知识库权重」列**：`Zotero.ItemTreeManager.registerColumn`。关键约束是
ItemTree 的 `dataProvider` **同步且逐行调用**，不能在里头发请求
（几百行会打爆服务）—— 所以启动/刷新时一次取回全量权重进 `weightsCache`，
provider 只做 O(1) 查表（`src/16-weightcol.js` 的 `registerWeightColumn`）。

**「已建知识库」「分节纲要」两列**：同一套做法 —— 数据一次全量取回进
`colStatusCache`（provider 仍然只查表，见上面那条约束）。
两条取值规则写在 `src/21-kbcols.js` 里：
① 一份**完整**的映射里查不到某个 key 才算"没建库"（拿不到服务时整列留空，
不把"我不知道"画成"这篇没建库"）；② 纲要节数按 `views/<key>.outline.md` 的
`## ` 行数算，补零成三位再交给 Zotero —— 它给自定义列排序用的是
`_sortCollation` 比较字符串（实测这一版 collation 认数字，补零是兜底：
哪天换回普通字典序，"13" 就会排到 "4" 前面），显示时由 `renderCell` 去掉前导零。

> 新列**必须**在列定义里写 `defaultIn: ["*"]`，否则会出现"注册成功、
> `_columns` 里也有、但列表上看不见"——因为只要任何一列带 `defaultIn`
> （内置的 title / firstCreator / hasAttachment 都带），`_getColumns()`
> 就把没声明它的新列算成 `hidden=true`，用户只能自己去右键"列"菜单里勾
> （漏了这句就会注册成功却看不见）。

**右键菜单六组**（每次 `popupshowing` 重建，并判断 `event.target !== popup`
以免子菜单冒泡把自己的菜单拆掉，`src/14-menus.js` 的 `registerItemMenu`）：
① 发送到 DSH（新建/已有对话）② 分类建议 ③ 标为重点/取消
④ 补全元数据 ⑤ 重建这一篇的知识库条目（专治"正文提取坏了但版本号没变、
增量会跳过"）⑥ **打开知识库（分级）**—— 二级菜单列这一篇的六个层面
（摘要与要点 / 分节纲要 / 完整档案 / 按页正文 / 图注与表格 / 权重与经验），点了直接打开
那个 md，见下面 B7。

**"发送到 DSH"走文件信箱，不走 HTTP**：往 `~/.dsh/zotero-bridge/` 投 JSON，
先写 `.tmp` 再原子改名，然后每 500ms 探 `results/<id>.json`
（`src/12-dsh.js` 的 `bridgeRequest`）。为什么不用 HTTP 写在注释里：DSH 的 desktop profile
**没有 `webServer` 服务**，走 `/api` 也不行 —— 那是"受信任 + 已认证"专用通道，
外部程序一律 401（`src/12-dsh.js` 的 `bridgeDir` 那一段）。
载荷**只发"解析后的存放路径"，不发正文** —— 正文已在 `papers/<key>.md` 与
`fulltext/<key>.md`，AI 有读文件的工具，把正文塞进消息只是白烧 token
（`src/12-dsh.js` 的 `sendToDSH`）。

**任务轮询用 `Services.tm` 定时器 + setTimeout 递归，不用 `setInterval`**：
实测 `setInterval` 创建成功但**回调从不触发**（状态文件里 `taskPolling=true`
而 `tickCount=0`），链式 setTimeout 也不可靠，所以主路径换成 nsITimer，
哪条路生效记进状态文件的 `delayBy`（`src/18-taskpoll.js` 的 `startTaskPolling`）。
每 2 个 tick（约 4 秒）刷一次权重缓存（别处改了权重最多 4 秒就可见，
不然用户会以为"没生效"）；每 15 个 tick（约 30 秒）才写一次状态文件，
因为那是磁盘 IO（`src/18-taskpoll.js` 的 `runTask`）。状态文件在 `<知识库>\plugin-status.json`，
**`tickCount` 在涨就说明轮询活着**。

#### B8. 打开知识库（分级）：把"找文件"变成两次点选

**问题**：知识库目录里是 `papers/22X9PMR6.md`、`views/7X23G52Y.tldr.md` ——
文件名是 Zotero 的条目 key，**人认不出是哪篇**。想打开某一篇的某一层，
只能先记 key、再去目录里翻；而目录根下最显眼的是 `index.db` 和 `INDEX.md`，
所以用户的原话是"打开文件夹后仍然是只有索引，看不见文献的名"。

**做法**：给"一篇文献有几层可看"建立数据模型，三个消费方共用一份定义：

| 级别 | 文件 | 内容 |
|---|---|---|
| 摘要与要点 | `views/<key>.tldr.md` | 元数据 + 结构化字段 + 摘要 + 笔记要点 + 使用经验 |
| 完整档案 | `papers/<key>.md` | 元数据 + 摘要 + 每页首段 + 笔记与标注 |
| 按页正文 | `fulltext/<key>.md` | 带 `## p.N` 锚点的正文 |
| 图注与表格 | `views/<key>.figures.md` | 散在各页末尾的图注与表格汇总成一份 |
| 权重与经验 | `views/<key>.weight.md` | 检索权重、重点标记、人工分、这篇的全部经验 |

定义在 `offline/kbviews.py` 的 `LEVELS`（**唯一事实来源**）：

- **管理面板**：点「打开知识库」→ 先弹文献列表（作者/年份/标题、可搜索可排序）→
  选中一篇再列六个级别 → 双击用系统默认程序打开那个 md（`content/admin-pages/` 里的实现）。
- **Zotero 右键**：第 ⑥ 组「打开知识库」二级菜单列同样的六个级别
  （`src/13-kbopen.js` 的 `KB_LEVELS` 是 Python 那份的**镜像**，
  由 `tools/check_kb_levels.py` 盯着 id/标签/路径模板三者一致）；
  缺文件的级别在文案里标「（还没生成）」而**不设 disabled**
  —— 实测 menupopup 里只要有 disabled 的 menuitem，整个子菜单就点不开。
- **MCP**：资源 `zotero-kb://item/tldr/{key}` 的渲染实现就是
  `kbviews.render("tldr", …)`（渲染实现只有这一份，避免两块漂移）。

**两个不显然的决定**：

1. **视图文件在构建时生成，不在"打开时"现生成**。Zotero 右键菜单是**同步**
   构建的（弹出来那一刻不能等网络、也不能等 Python 起进程），它只能靠
   "文件在不在"决定显示什么。所以 `offline/convert.py` 在一批条目**全部提交
   之后**统一生成；单独补齐用 `offline/maintain.py views`（面板「高级」页维护动作里的
   「补齐分级文件」就是它）。
2. **打开文件走三级降级链**（`src/13-kbopen.js` 的 `openKbPath`）：
   `Zotero.launchURL` → `外部协议服务` → `Zotero.File...reveal()`
   （保底：在文件管理器里选中它）。为什么写成链式而不是挑一个：沙箱里这些
   入口的可用性随版本变，而链式能保证最坏情况也有确定可用的兜底。

`views/` 与 `papers/`、`fulltext/` 同待遇：**可重建的派生物**，删了跑一次
构建就回来；条目被删除时 `_purge_key` 会连它一起清掉。

#### B9. 管理面板

**管理面板**是 Zotero 内的一个窗口（工具菜单 →「知识库管理…」；主工具栏按钮与条目右键
也指向同一个面板）。三层分工：

- **页面**：`zotero-plugin/content/admin.html` + `admin.js` + `admin.css` +
  `content/admin-pages/*.js`（一页一个文件、按 `order` 排成 10 页）；
- **宿主半**：`zotero-plugin/src/23-admin.js`（开窗 / 桥 / 路由表 / 超时 / 引擎生命周期）；
- **引擎**：`online/engine.py`（stdio JSON-RPC，**无端口、无 token、不常驻**）。

管理面板的 10 页按 `order` 排成：**知识库结构 · 经验库 · 分类建议 · 运行环境 ·
PDF 解析 · 损坏查询 · 元数据 · 分节纲要 · 高级 · 提示词**。页面只画自己那一页、
数据一律经宿主半取（页面**不直接**向本机发请求）；一页一文件，改一页不动别人。

日常维护集中在「高级」页：**索引重建**（`POST /reindex`，增量 / 全量两条）与
**维护动作**（`POST /maintain`，task = `stats` / `check` / `backup` / `reconcile` /
`views` / `sync-skill`）。重建是长任务 —— 派发即返回 `{job:{id}}`、页面轮询
`/jobs/<id>`，进度打到底部日志；危险动作（全部重建、分类重整、插件动作）都要二次确认。

两条容易踩的约束（细节见代码注释）：页面必须与宿主**同进程**（不要用
`openInViewer(页面地址)`，那会把页面切进远程进程），且这类窗口里 `window.parent === window`
—— 所以宿主**自建 `<browser>`** 并在页面的 `postMessage` 上**装一道分派钩子**。

> `KB_FILE_SPEC`（「知识库结构」页那张"是什么 / 占多大 / 能不能删"的清单）住在
> `offline/kb_manifest.py`；"项目根在哪"的判据是 `online/engine.py` + `offline/schemas.py`
> （见 `zotero-plugin/src/02-paths.js::_looksLikeProjectRoot`）。

#### B10. DSH 侧接入：bundle 与模板

`bundle/` 是一个 DSH bundle，往 loader 里 `insert` 一条 `dsh-mcp-client` 配置。
三条设计理由都写在模板注释里（`bundle/cordis.patch.yml.tmpl:4-33`）：

1. **新增条目必须写在 `- insert:` 下面** —— 直接写 `- id: xxx` 是"按 id 覆盖已有条目"
   的语法，会报 `entry not found`；profile 自己的 patch 同样是覆盖语义。
2. **`command`/`args` 必须是绝对路径** —— DSH 从 profile 解析该文件，相对路径找不到解释器。
3. 所以仓库里放的是带 `__PROJECT_PYTHON__` 占位符的 **`.tmpl`**，安装时由
   `tools/gen_bundle_config.py` 按本机实际路径生成 `cordis.patch.yml`
   （该文件在 `.gitignore` 里）—— 别人克隆下来也能用，也不泄露开发机的目录结构。

`serverName: zotero-kb` 决定了模型看到的工具名前缀 `mcp__zotero-kb__kb_search`；
**改名等于改名所有工具**，会让历史会话与权限规则失效，所以定死不动。


---

#### B11. 能力探测与"菜单不写死"
右键菜单里跟外部能力有关的那几项（发到 DSH、本地模型相关），**不该在没有那个能力时
装作可用**：菜单会先探测，拿不到就在下一级菜单第一行写明「未连接：原因」，
而不是让菜单项点了没反应。实现拆成两块：

- **`zotero-plugin/src/19-caps.js`**（新）：只负责"探测 + 缓存 + 文案"。
  `caps.dsh` / `caps.localModel` 三态（`true` / `false` / `null` 未知）；
  DSH 的探测 = 桥目录在不在 + 投一个 `list-sessions` 等 3 秒（超时后**删掉自己写的那个
  请求文件**、并冷却 5 分钟，避免 DSH 没开时每 30 秒往桥目录里堆垃圾）；
  本地模型 = 复用 `/health` 报的 `ollama` 状态 + 插件 pref（选了 API 就看 key）。
  周期刷新用 `setTimeout` 链（沙箱里 `setInterval` 可能不触发）。
- **`14-menus.js`**：只读缓存。`capLabel(kind)`（纯函数，桩测试覆盖）给出
  「发送到 DSH（未连接）」/「连接到本地模型（检测中）」这类标题 + 一句"怎么修"的提示。
  「分类建议」「补全元数据」合并成二级菜单「连接到本地模型」；「重建本条目知识库」改名。

#### B12. 正文从哪来：Zotero 缓存 / PyMuPDF / MinerU（可选解析器）

正文抽取那一步的接缝在 `offline/convert.py` 的主循环里：
`pages, source = reader.fulltext_for(item, **ft_kw)` → `item.fulltext_pages` →
写 `<知识库>\papers\<key>.md` 与 `<知识库>\fulltext\<key>.md`（页锚点 `## p.N` 由 convert 写）→
切片/向量/MANIFEST，来源记进 `items.fulltext_src`。所以换文本源**只换这一个调用**。

- 默认（`zotero`）：`offline/zreader.py` 的两条路（Zotero 自己的 `.zotero-ft-cache`
  与 PyMuPDF）+ 字符偏移还原 + 页眉过滤 + 灰区模型仲裁。**没装 MinerU 的用户
  走的就是这条**，与之前逐字节一致。
- 可选（MinerU）：`offline/mineru.py` 两件事 —— **探测**（`probe()`：装没装、
  版本、生效的后端与 VLM 引擎、模型档位齐不齐、GPU 可用不可用，结果缓存 60 秒；
  给插件首启对话框与面板「运行环境」页的 MinerU 那一行用）与**解析**
  （`parse_item()` / `fulltext_for()`，见下面 B13）。
  默认仍是 `zotero`，所以**没装 MinerU 的用户行为逐字节不变**。
- 安装：`scripts/install-mineru.ps1`（可选组件，装在项目内的 `.mineru\`，
  不写 PATH/注册表，删目录即卸载）。装完在面板「运行环境」页能看它认没认到，
  「PDF 解析」页负责解析与查看状态。

  三条实测出来的坑（脚本与文档里都写了）：Windows 上 `mineru[full]` 装的是
  **LMDeploy**（vLLM 只有 Linux），而它在这类机器上起不来
  （`ValueError: high is out of bounds for int32`）→ 所以装 `mineru[torch]`，
  VLM 档走 **llama.cpp + Q8 GGUF**；**PyPI 的 Windows torch 是 CPU 版**，得从
  `download.pytorch.org` 换 CUDA 轮子；模型走 modelscope（hf-mirror 的 API 403）。

#### B13. MinerU 怎么接进建库流程
接缝只有一处：`convert.py` 主循环里那个 `pages, source = reader.fulltext_for(...)`。
改成"按 `--parser` 选源"：

    --parser {zotero,basic,standard,flash,advanced}     默认 zotero
    --force-parse                                        忽略指纹全重解析
    --parse-timeout N                                    单篇超时（默认 3600 秒）

| 环节 | 做法 | 为什么 |
|---|---|---|
| 产物格式 | `mineru-kit parse … --format zip` | 默认的 markdown 输出**图片 base64 内联**（实测一篇 5 页论文 621 KB，14 张图最长 88228 字符）且**没有页码信息**；zip 里是 md（图片相对路径，同篇 20 KB）+ `structured_content.json`（按页）+ images/ |
| 逐页正文 | 从 `structured_content.json` 的 `pages[].page_idx` + `blocks[]` 渲染 | KB 的页锚点 `## p.N`、`kb_fulltext(page_from=…)`、检索结果带页码全靠它。**页眉/页脚/页码块直接丢掉** —— MinerU 自己标了类型，比自己写正则可靠 |
| 公式 | `equation` 块渲染成 `$$…$$` | 能被检索命中（这是装 MinerU 最大的收益） |
| 图注 | `img_caption`（**是列表**）压成一行、留在正文里 | 图注是能搜到的信息；直接 str() 会渲染出 `['图 2 …']` 那种带方括号的怪样子 |
| 落点 | `<kb>/mineru/<KEY>/`：markdown.md / structured_content.json / middle_json.json / images/ / pages.json / meta.json | 不进 index.db（不为它加表）：状态从 `items.fulltext_src` + meta.json + 目录在不在读 |
| 要不要重跑 | 指纹 = 绝对路径 + 大小 + mtime_ns + 档位 + exe + **工具版本** | 少一项就会出现"换了 PDF 还用旧产物"或"永远重跑"。带上工具版本是因为升级 MinerU 后同一份 PDF 的产物也会变 |
| 增量判据 | `items.fulltext_src` 不以 `mineru-<档位>` 开头 → 也要重跑 | 不判这条的话"把解析器换成 basic"会**什么都不做**（version/分类都没变，增量逻辑认为没变化）—— 用户会以为命令没生效 |
| 失败 | **按篇回落**到 Zotero 缓存 / PyMuPDF，`source` 加 `+fallback` 后缀、MANIFEST 里列出来 | 95 篇里混着扫描件/加密/损坏文件，任何一篇失败都不该把整轮降级 |
| 面板 | 新页「PDF 解析」：档位下拉 + 只补缺失 / 全库重解析 / 解析选中这篇 / 删产物 / 打开产物目录 + 状态表（来源 / 档位 / 页数 / 图 / 耗时 / 产物状态）+ 汇总（多少篇已是 MinerU、产物占多少 MB） | "重解析全库"是几十分钟的操作，看不见状态没人敢点 |
| CLI | `tools/kb_admin.py mineru {status,parse,clear,missing,reparse}` | 面板按钮调的就是它；`missing`/`reparse` 内部**包一层 `convert.build(ns)`**，不写第二份正文抽取 |
| **批量** | `mineru.parse_many(pairs, batch_size=20)`：把 PDF 以 `<KEY>.pdf` **硬链**到临时输入目录 → `parse <目录> -o <输出> --format zip` **跑一次** → 按 `<KEY>.zip` 映射回 key 落产物；日志走 `_run_stream`（实时，面板也看它） | **每次 `mineru-kit parse` 都要重新加载模型**（实测约 2 分钟）：逐篇跑 93 篇 ≈ 3 小时；批量时模型只加载一次，实测 1.7 页/秒。`missing`/`reparse` 因此都是「先批量解析（写好产物与指纹），再让 convert 命中指纹、只做切片与向量」 |

三个实测出来的坑（都写进代码注释了）：
① 全库解析前**先退出 Ollama** —— 抢同一块显存，同一篇 5 页论文模型阶段
104.8 秒 → 8.9 秒（0.048 → 0.223 page/s）；
② `convert.build(args)` **只吃一个参数**、且内部要读
`args.figures/no_figures/embed_model` —— 自己造 Namespace 时字段要抄全；
③ 那段"把配置同步进插件 pref"的 JS 不能塞进 f-string（嵌套 dict 花括号会让
f-string 解析炸掉），已抽成纯函数 `llm_sync_js()` 并交给 `node --check` 单测。

#### B14. 右侧栏「知识库」：**自建一列**，独占条目窗格
用户先说「把知识库里的 md 显示在右侧栏，方便和原文对照」，看过 `leike0813/zotero-agents`
的 ACP Chat 后又决定「仿他的做，要能独占一页」→ `zotero-plugin/src/20-kbview.js`
+ `zotero-plugin/content/kbview.{html,css,js}`。

- **为什么不能再用 `ItemPaneManager.registerSection`**：Zotero 的 itemPane 是一列纵向堆叠，
  rail 只做 `scrollToPane` / pin —— 注册进去的分区**永远只能堆在分区列最下面**，做不到独占。
  所以改成自己往 `#zotero-item-pane` 里插一列（`insertBefore(container, #zotero-view-item-sidenav)`），
  打开时把原生的 `#zotero-item-pane-content` 临时 `hidden`，视觉上独占。
- **rail 按钮 append 到 `#zotero-view-item-sidenav`**（天然就在最下面），`class="btn"`
  继承 Zotero 自己的 rail 样式。**绝不能给它 `data-pane`** —— `ItemPaneSidenav#render()`
  会按"已注册的分区"隐藏 `.btn[data-pane]` 的**父元素**，而父元素就是 rail 本身。
- **页面跑在 XUL `<browser type="content">` 里，地址是 `resource://zotero-kb/content/kbview.html`**：
  插件在 startup 注册自己的 `resource://` 包（`KB_RES_KEY`）。直接加载 `rootURI`（`jar:`）
  的两难、以及 `maychangeremoteness` 让它变远程进程的坑，源码注释里有记录 ——
  Zotero 自己的 reader 也是 `createXULElement('browser')` + `resource://`。
- **分工**：**宿主取数据**（IOUtils / kbDir / `kbLevels()` / `doc-edit` / `kbviewFixImgs`），
  **页面只渲染**（普通 HTML/CSS/JS，终于能正常写 CSS：`overflow-wrap:anywhere`、
  `max-width:100%`、MathML、宽表格横向滚动）。跨 principal 只传 **JSON 字符串**，
  经 `contentWindow.wrappedJSObject.kbvReceive` 与注入的 `__kbvHostPost`。
- **图片走 `resource://zotero-kb-files/`**（→ 知识库目录），**不能走 `file://`**
  （resource:// 页面加载 file:// 图片会被安全策略拦掉，静默空白）。
- **宽度归原生 splitter**（用户要"同宽窄能拉动"）：列只是 `flex:1` 撑满原生条目窗格，
  本插件**不改窗格 width**；`hidden`/`collapsed`/splitter state 关闭时逐项还原。
- **条目切换**自己盯（自建列没有 `onItemChange` 回调）：打开期间每 400ms 比一次
  `kbviewCurrentItem()`（阅读器页签取附件→**父条目**，与 Zotero 的 item-details 一致）。
- **取不到内部 id 就降级**（换 Zotero 版本）：退回 `registerSection` 只读模式并写状态文件，
  **不让面板整个消失**。
- 「改这一页 / 恢复原版 / 导出草稿」仍是原逻辑，只换了传输层；`md2html()` 与
  `kbviewSplitMd()` 仍是纯函数（桩测试盯着）。

**阅读器对照列的三处新增**（随 **v1.11.0** 发布；不做独立窗口、不做 `/doc-pdf`）：

- ① **阅读器那一列**（`kbviewRoots(win, "reader")` → `#zotero-context-pane` /
  `-inner` / `-sidenav`）：与条目窗格那一列**共用同一个页面**（`content/kbview.html`），
  只是宿主落点不同；打开时把 `#zotero-context-pane-inner` 临时 `hidden`。
  **左 PDF 右正文、同一个窗口**，零 PDF 渲染成本；关闭时逐项还原。
- ② **一键对照**：`Zotero.Reader.registerEventListener("renderToolbar", …)` 往阅读器工具栏
  塞一个「对照」按钮。这个事件是**一次性**的（React 那个 `CustomSections` 只在 mount 时发一次），
  而 Zotero 会**在启动时恢复会话里的阅读器** —— 那些阅读器早于本插件注册监听，于是永远收不到事件。
  所以另有一条**直接注入**（`kbviewInjectToolbarButton`，往 `.custom-sections` 里 append）
  兜底，并在 Notifier 的 `tab` select/add 时补一次。
- ③ **停在正在读的那一页（只在打开时定位一次）**：打开阅读器那一列时取当前页
  （`kbviewReaderPage` / `kbviewReaderInfo`）作**初始页**，页面滚过去并高亮。
  当前页优先取 `reader._state.primaryViewState.pageIndex`（实时），读不到才退回
  `items` 表的「最后阅读页」—— 后者会留着"上次退出时的页码"，先问它会拿到陈旧的页。
  **翻页不跟随**：跟随要做成周期性的重渲染（Notifier 的 `pageChange` + 轮询重推），
  代价是明显的渲染负担，而手动翻找并不麻烦 —— 所以只在打开时定位一次。
  函数 `kbviewFanoutPage`（一次性把当前页推给已打开的列）保留着，
  条目窗格那一列与将来的「跳到这一页」可以直接调它。

**正文与公式在页面里渲染**：用 vendored 的
`content/vendor/{markdown-it,katex}`（markdown-it 14.1.0 / KaTeX 0.16.11，都是 MIT，
含 LICENSE 与 20 个 woff2 字体），**不读服务端的 `.html` 孪生**（那份只在降级模式用）。
理由：latex2mathml 转不了 `\begin{aligned}`（会转成一串 `&`）；而逐块编辑要**实时预览**，
服务端 HTML 里拿不回来"改了一半"的正文。

**界面的几条约定**：页面工具条上**不放「对照页」按钮**（阅读器那一列已经在同一个窗口里
对着 PDF 看了，再跳浏览器多一步；浏览器兜底页与它那五个端点也已经没有了）；
编辑框**跟着内容自动长高**并跟预览等高；分页线是**实线**；页工具条字号比正文略大；
正文与预览里的图**限高**（150 / 110）—— 放图只为确认"对应原文哪张图"；MinerU 带出来的
`<sub>`/`<sup>` 用**无属性白名单**精确回填（不开 `html:true`，见 `allowInlineTags`）。

**按块编辑**：宿主 `kbviewSplitBlocks()` 把一页 md 切成块
（空行分段 / 独立公式一块 / 表格整块 / 图片行单独一块）并**原样保留块间分隔符**
（`kbviewJoinBlocks(parts) === 原文`，桩测试钉住），页面只负责画；
保存时页面把每块文本交回，由宿主拼回整页再走**既有** `POST /doc-edit`
（写路径仍然只有一条）。

#### B15. 能力探测的菜单状态行

「（未连接）/（检测中）」这类状态**不挂在顶层标题上**，而是放在**子菜单第一行**
（`capStateLine()`，纯函数）：`● 已连接 DSH　12 个对话` /
`○ 未连接：桥目录不存在` / `○ 还没检查过连接（点这一行重新检查）`。
两个理由：顶层标题会太长；而且探测是周期性的，挂在标题上会出现"子菜单已经列出对话
列表、标题却写检测中"的矛盾。改法是用**这次真拿到的结果**回填：
`listDSHSessions()` 成功即 `caps.dsh = true`；`null` 时不写"检测中"，
据实说"还没检查过"。

#### B16. 元数据补全：两路首页正文 + 模型综合

- **两路**：A = `zreader.fulltext_for()`（PDF 原文字面）；B = `<kb>/mineru/<KEY>/pages.json`
  （MinerU 逐页正文）。取文本/合并/渲染全在 `offline/metafill_sources.py`
  （单一职责，纯函数，本机测试覆盖）。
- **合并纪律**：两路归一化后同值 → `agreement="both"`（更可信）；只有一路 → `"single"`；
  两路不同 → `"conflict"`，**不擅自合并**，按**逐字段**优先表取一路
  （DOI/卷/期/页/日期信 A 的字面，作者信 B 的版面切分），另一路进 `alternatives`。
- **模型综合**：模型看到的是"两路正文 + 规则候选清单"（通过同一个 `{text}` 占位符送进去
  —— 这样用户**已保存的旧提示词覆盖**照样能用）；它给的证据在**任一路**里能找到就算核对通过
  （`_verify_evidence(pages, ev, pages_extra=…)`）。
- **输出契约向后兼容**：`suggestions[]` 的六个老键都在，新增
  `sources / agreement / alternatives / note`；插件弹窗会把"两路一致 / 不一致 + 另一路的值"
  显示出来（老服务端不给这些键时整块跳过）。
- 退化路径都写进 `notes`：只有 A、只有 B、两路都没有、模型不可用、某一路首页过短。

#### B17. 中间层：分节纲要
需求：「全文太长，现有的摘要还太短，怎么加中间的一层级」。答案是
**分节纲要**：`offline/digest.py`（新模块）按 MinerU 的标题块切节 → 每节让模型写
2~4 句 + 3~6 条关键点 → 落 `meta.ai_outline:<KEY>`（结构化）+ `views/<KEY>.outline.md`
（人读）。实测一篇 5 页论文：8 节、2600 字、33 秒（全文 12639 字、摘要几百字）。

- **切节规则**：带标题的节一律保留（保住原文结构）；没有标题的碎片才并进相邻节；
  参考文献/致谢/附录**跳过**（占位"（略）"，不调模型）；超长节按段落再切；
  节数上限 24（超了合并相邻节）。每节带 `p.起–止` 页码 → 细看就按页取原文。
- **按节指纹缓存**（`sha1(标题+正文[:2000])[:6]`）→ 改一章只重跑那一章；
  缓存判据里带 `tried`：有些节本来就产出不了要点（正文过短/模型没给结果），
  只认"有 summary"的话这些节会**每次构建都被重问**（实测第二次仍有 1 节被重问）。
- **只读不生成**：`kbviews._render_outline` / MCP 资源 `zotero-kb://item/outline/<KEY>`
  只读现成产物；生成入口是 **Zotero 条目右键菜单（`POST /digest` → 后台 job，
  `digest.build(should_stop=…)` 可中断）**、面板按钮与 `python offline/digest.py <KEY>`
  —— 一篇学位论文要几分钟，不能挂在"随手点一下"的读路径上。
- **批量入口在插件侧**：条目右键 →「生成分节纲要」逐篇**串行**
  提交（单 GPU 不能并发打 Ollama）、失败不中断整批、已有纲要的直接跳过，结束时汇总
  「已生成 / 跳过（已有纲要）/ 失败」并立刻刷一次两列；进度与每篇结果落在
  `plugin-status.json` 的 `digestBusy` / `lastDigest` 里。
- **不写进索引、不改排序**（用户明确"检索分层算了吧"）。

#### B18. 提示词注册表：话术只有一份，且用户改得动

`offline/prompts.py`（6 条：tag/summary/extract/metafill/
draft/chunks —— chat/para/propose 随窗格功能一起去掉了）+ 覆盖文件
`<知识库>\prompts.json`。要点：

- **渲染用 `{占位符}` 替换，不用 `str.format`**：这些提示词里本来就含 JSON 示例
  （`{"tags": [...]}`），用 format 就得写成 `{{`，用户看到的默认文本是一堆双括号。
- **保存前校验**：占位符还在、JSON 契约键还在、非空 —— 改坏了在保存时被拒，
  而不是等调用时炸或静默解析失败。
- **试跑**：拿固定样例跑一次，把实际送出去的提示词、模型输出、JSON 解析结果
  一起摆出来（提示词改坏了最典型的症状是"模型开始胡说"）。
- **搬家没走样**：`tests/prompt_goldens.json` 是搬家**前**从真机抓的 5 次真实调用，
  逐字比对（含 metafill 的 `{{` 与 chunks 的双括号这两个容易走样的细节）。

#### B19. 经验层的写入只有一份实现

`offline/experience.py`：`add / update / delete / set_weight` + **唯一**的权重算术
（`weight_statements`）。要点：

- **"权重 = 经验的函数"**：任何改经验的动作都是"先回滚旧贡献 → 改行 → 应用新贡献"，
  而且**三步在同一个事务里**（分三次写的话中间失败会留下"权重回滚了、行没改"的
  不一致状态，且没有任何报错 —— 实测撞到过）。
- **`history` 只是追加记录**：`-- 经验层：只追加，永不覆盖` 对**自动流程**仍然成立，
  只对用户显式编辑开口子，且旧值 push 进 `history`。
- **新增两个写入口**：面板「修改/增添经验」（口述 + 表单两条路，表单任何时候都能用）
  与 CLI `kb_admin.py add/edit`。面板里「选文献…」走 `PaperPicker` 的**多选模式**
  （`multi=True` + `selected_keys`：已选排最前且默认勾选、点一行勾/取消、双击或回车确认，
  回调的是**完整集合**、空列表合法）；保存仍走 `add_experience/update_experience`，
  面板不自己写库（回归在 `tests/test_picker_multi.py`）。
- **id 连续编号**：删除后自动把 id 重排成 1..N（`renumber()`；两段式写 ——
  先把整表改成负数再写成目标值，否则主键会当场撞车）。批量删走
  `delete_experiences()` **一次事务 + 只重排一次**：循环调单条删除会边删边重排，
  第二条就删到别的行上了。库里没有任何地方按 id 引用经验（`history` 是行内 JSON、
  `item_weight` 按 item_key、`pending.jsonl` 用会话号），所以重排是安全的；
  老库留下的空号用 `kb_admin.py renumber` 修一次。
- **打开旧库自动补列**：只靠 `init_db`（构建时）迁移的话，"加了新列还没重建库"
  的窗口里写入会撞 `no such column` —— 所以 `schemas.connect()` 也顺手补一次（幂等）。

#### B20. 条目生命周期：归档 / 还原 / 彻底删

Zotero 里的"删除"其实分两段：先进回收站（可还原），再彻底删。知识库跟着分两段走 ——
**不是"删了就没"**：

| Zotero 侧 | 知识库侧 | 产物 |
|---|---|---|
| 移进回收站 | **归档** | 这一篇的 `views/` `papers/` `fulltext/`（含 `.html` 孪生文件）+ `mineru/<key>/` + `rows.json`（切片 / 图注 / 向量的快照）搬进 `<知识库>\trash\<key>\` |
| 从回收站还原 | **装回** | 按 `rows.json` **直插回库**（秒级，不重跑模型），文件搬回原位 |
| 回收站里彻底删 | **清理** | 删掉归档目录；`item_tombstone` 留一行（key + 标题 / 作者 / 年份），界面据此把失效引用显示成「（已删除） 标题」 |

三个动作的**唯一实现**是 `offline/trash.py`。`convert.py::_reconcile_deletions()` 在每轮构建
**之前**做一次对账，四种判断：该归档 / 该删 / 该还原 / 归档目录还在但条目两处都没了。

两个不显眼但必须的细节：

- **还原时写"当前 version"**：Zotero 进出回收站都会给条目 `version` +1。直插归档时的旧号，
  紧接着的增量就会把它当成"变过的"整篇重抽（实测 1.7 秒/篇）；写当前号才是真正的秒级还原。
- **直插前探 id 冲突**：快照里的 `chunk_id` / 图注 id / **`items.item_id`** 都可能已被别的条目复用。
  五处探测到冲突就**整条放弃直插**，只搬文件、交给增量重抽 —— 宁可慢一次，也不覆盖别人的行。

#### B21. 合并映射：`resolve_key()` 与"被并掉的条目"

你在 Zotero 里「合并重复条目」时，被并掉那条会在保留项上留下 `dc:replaces` 关系
（**方向：保留项持有关系、object 指向被并掉的那条** → 映射是「被并项 → 保留项」，反了会静默少行）。
知识库把它读进 `item_merge`，此后所有"按 key 找东西"的地方都走 `offline/keys.py::resolve_key()`：

- 输入 key 有映射 → 顺链取到**根**（链上任何一环都不看墓碑）；
- 输入 key 没有映射 → 才轮到它自己的墓碑；
- 成环（A→B→A）直接报错，不静默。

`offline/experience.py::item_refs()` 是"key 怎么显示"的**唯一实现**：正常显示
「作者 年份 · 短标题 [KEY]」，被删 / 被并 / 不在库里各有各的写法。

> **还没做**：relink（把图谱 / 引文表里指向被并项的行走改指保留项）与权重合并。
> 它们要等分类树那批把 `kg_*` 表建出来；而且现在库里没有数据挂在被并 key 上，**暂时不痛**。

#### B22. 路径从哪来：`.env` 覆盖层与"来源可见"

所有路径类配置都走 `offline/schemas.py` 的 `resolve_*()`，优先级**只有一份定义**（`LAYER_ORDER`）：

```
调用方指定 > 进程环境变量 > 运行时设置（面板写的 kb-location.json）> .env > 自动探测 > 默认
```

- **面板设置赢过 `.env`**：早期是反的，结果"面板里改完却不生效"（`.env` 里一个旧值一直赢）。
  现在 `.env` 退回"面板起不来时的救急层"—— 那个场景天然成立，因为那时运行时设置本来就是空的。
- **`.env` 只放路径与参数，不放密钥**（密钥仍在 `<知识库>\llm-config.json` 这类运行时文件里）；
  模板是仓库里的 `.env.example`。
- **每一档都会记下来源**：面板「运行环境」页逐项显示（含"被谁盖住了"和"填的值不存在"），
  所以"改了不生效"能一眼看出是谁在起作用。

#### B23. 公式就地纠错：右键 → 定位 → 编辑 → 回写

**问题**：MinerU 解析公式会出错（多行矩阵尤甚），而公式在正文里是 **LaTeX 文本** ——
看着像原文，其实已经错了，肉眼对不核对。全库 1314 个块公式里有 3 个是**源 LaTeX 本身
坏了**（环境不配对 / 公式被截断），任何渲染器都救不回来。原来只有「改这一页」和按块编辑
能改，但得先在正文里找到那一条。

**做法**：在 PDF 上**右键公式本身**，直接改那一条。

| 环节 | 实现 |
|---|---|
| 触发 | 注册 `createViewContextMenu`（右键页面，`params.position` = **点击处在页面上的位置**）与 `createAnnotationContextMenu`（右键已有标注，`params.ids`）—— 两个都注册，所以**右键公式不必先画标注** |
| 定位 | `GET /formula-target?key&page&rects` → `offline/formula.py`：左下原点的 PDF 点换算成左上原点归一化 rect，再匹配该页的 equation 块 |
| 预览 | 优先用 MinerU 自己那块裁剪图（`image_source`），没有才按 rect 从 PDF 裁 |
| 编辑 | 阅读器那一列弹出模态框：原文裁剪图 + 可编辑 LaTeX + **KaTeX 实时预览**；一页多条公式可下拉挑 |
| MathType | `POST /formula-mathtype`：**服务端**把 LaTeX 放进剪贴板并启动 MathType（插件沙箱里没有稳定的起进程/写剪贴板能力）。**未装 MathType 时按钮只置灰、不隐藏** |
| 回写 | `POST /formula-apply` → `replace_nth_formula()` 换掉该页第 n 条 `$$…$$`（新 LaTeX 没写 `\\tag` 时沿用旧编号）→ 交给**既有唯一写路径** `POST /doc-edit`（修订层 + 重切重向量） |

**为什么不做本地公式识别（OCR）模型**：做过一轮选型（393 条矩阵公式做基准，脚本与结果
留在项目仓库外的选型工作目录（不进仓库）：

| 候选 | 结论 |
|---|---|
| pix2tex（MIT） | 与 MinerU 的矩阵环境重叠 17%、结构自洽 27%（MinerU 85%），明显更差 |
| PP-FormulaNet_plus-M（PaddleOCR，Apache-2.0） | ⚪ 结构自洽 86% vs MinerU 85%、**救回 6 / 退化 4** —— 属"打平"而非"明显更强"；而且**偏导符号 ∂ 两个模型都认错** |

按预登记判据（"没有候选明显强于 MinerU 自身输出就停手"）**不落地**，两个 spike 环境
都已卸载。结论：**识别错只能靠人改，那就把"改"做到最省事**（右键 + 用户本来就会的编辑器）。

**坐标换算（实测确定）**：MinerU 的 equation 块 **bbox 是归一化、左上原点**；Zotero 的
`position`/标注 `rects` 是 **PDF 用户空间点、左下原点**（pageIndex 0 基）。
换算 `ny = (2*my0 + mh - y)/mh`。不翻 Y 会裁到别的行、画面看着还"正常"
（正文也是文字），只有比对原文才露馅。

**边界**：不改 Zotero 数据库、不改 MinerU 产物 —— 新 LaTeX 只进**正文修订层**
（与「改这一页」同一条路），重跑解析后修订仍在。

#### B24. 获取文献：搜 → 列候选 → 勾选 → 抓进 Zotero

这是一个**对话内的连贯动作**，不是一个后台任务：

```
你：帮我找几篇「某主题」的文献
Agent：搜到 3 篇候选（**点标题能打开原文页**），逐条带作者/年份/出处/关键词/开放获取状态
你：在界面里勾选要哪几篇
Agent：勾完直接抓 → 每隔几秒报一次"进行中 1/3，正在处理 10.xxxx/yyyy"
      → 完成：N 篇入库、M 篇带 PDF → 补抽知识库（新文献立刻能被 kb_search 搜到）
```

也可以直接调工具：

```python
kb_paper_info(dois="10.xxxx/yyyy, 10.xxxx/zzzz")      # 先看值不值得看
kb_acquire(dois="10.xxxx/yyyy")                        # 1~2 篇：一次调用拿结果
kb_acquire(dois="...", wait=False) → job_id            # 3 篇以上：派发 + 轮询进度
kb_acquire_progress(job_id="aq...")                    # 进度 / 最终报告
kb_acquire(dois="...", dry_run=True)                   # 只解析元数据，一个字节都不写库
```

| 参数 | 说明 |
|---|---|
| `dois` | 勾中的 DOI，逗号/换行分隔；整条 `https://doi.org/...` 也认 |
| `wait` | `False` = 立刻返回 job_id（**3 篇以上、或想报进度时用**）；默认 `True` 阻塞到抓完 |
| `collection_id` | 目标分类；**默认 0 = 存到你在 Zotero 里当前选中的分类**（与浏览器连接器一致） |
| `find_pdf` | 是否顺带找 PDF（默认是） |
| `dry_run` | 只解析元数据、**不写库** |
| `reindex` | 入库后自动补抽知识库（默认是；**只对 `wait=True` 生效**） |

**插件侧的行为**：默认**不弹确认框**（搜索、列候选、挑选都在对话里做完了，
再弹一个 Zotero 模态框会把连贯的对话中断成两个界面；想要"写入磁盘前再看一眼"
就在插件设置「从 DSH 导入文献」里把勾打开）；落库后走一遍本地模型的分类建议，
**一个汇总框一次问完**（全部应用 / 逐篇确认… / 跳过）；只**新建**条目，
**绝不改**你已有的条目（唯一例外是会跳过库里已有的 DOI）。

**边界（这条线不许挪）**：

| | 做不做 | 为什么 |
|---|---|---|
| **用你已有的访问权限**（从你自己的机器发请求；校园网 IP、学校订的刊、你自己浏览器的登录态） | **可以，这正是这个功能的价值** | 等同于你自己在浏览器里点"下载" |
| **绕过付费墙**（改 URL 骗计费、影子图书馆、盗用凭证、伪造订阅身份） | **绝不做** | 版权与合规 |

线划在**"这个请求是不是以你自己的合法身份发出的"**上。

**抓不到 PDF 时**按顺序退，不要跳：① **浏览器连接器**（在出版社页面点 Zotero
图标，带登录态，能过的站点最多；中文文献基本只有这条路）；② 已知直链用
`skills\zotero-acquire\tools\fetch-pdf.ps1` 下载（**会验 `%PDF` 文件头**），
再用 `kb_attach(item_key=..., path=...)` 挂到条目上；③ 都不行就如实说没找到。

#### B25. 知识库位置、迁移与同步

默认位置：**`<Zotero 数据目录>\zotero-kb\`**。Zotero 数据目录是从它的
prefs.js 读出来的，所以你改过也没关系。

在 Zotero 设置 →「文献知识库」→「知识库位置」里能看当前在哪、也能
**迁移到 Zotero 数据目录**。迁移做四件事：**先备份 → 复制（不是移动，旧目录保留）
→ 校验**（条目 / 切片 / 向量 / 经验数必须一致）**→ 写 `kb-location.json`**；
校验不过会中止，不会让你丢数据。命令行等价操作是
`python tools\migrate_kb.py`（干跑）与 `--apply`（真迁移）。

为什么迁移是安全的：**索引里不存文件绝对路径**（`papers/`、`fulltext/` 都按
Zotero key 命名，路径由知识库目录推算出来）。

**把知识库同步到别的机器**：Zotero 自带的同步做不到（它的文件同步只覆盖
"数据库里有附件条目的文件"）。两个能用的替代方案（默认关闭，在设置里配）：
**同步到指定目录**（交给坚果云 / OneDrive 客户端去同步那个文件夹），或者
**用命令同步**（rclone / robocopy，输出会显示出来）。两者都是"复制 + 覆盖"，
不做镜像删除 —— 万一目标目录里有别的东西，`robocopy /MIR` 会把它删掉，
那不该由插件代劳。

**为什么不自动装 Python**：Zotero 插件**能**起外部进程（`Subprocess.sys.mjs` /
`nsIProcess`），也能下载解压。但代价是要自己处理约 300 MB 的下载、断点续传、
失败回滚 —— 为了省用户"选一次文件夹"不值得。所以选了"显式配置 + 自动探测"，
改起来直观、出错能说清原因。

## 五、目录与归属

**代码（仓库）与数据（知识库）是两处**，谁也不能由谁推算 —— 这是踩过的坑
（老代码让「项目目录」= 「知识库位置的上一级」，知识库一搬到 Zotero 数据目录，
就算出 `<Zotero 数据目录>`，那里没有 `.venv`）。

```
<项目目录>\                          代码（git clone / 解压到这里）
├── offline\                         离线管道：读 Zotero、清洗、切片、向量
│   ├── zreader.py                   只读快照（含 WAL）
│   ├── converter.py                 清洗 / HTML→MD / 按页切块
│   ├── convert.py                   CLI 构建入口（收尾时生成分级视图）
│   ├── kbviews.py                   分级视图：LEVELS + 渲染 + 写文件（唯一事实来源）
│   ├── kb_manifest.py               知识库目录/文件清单 KB_FILE_SPEC（唯一事实来源）
│   ├── edits.py                     正文修订层（唯一读写路径）
│   ├── trash.py                     归档 / 还原 / 彻底删（唯一写路径）
│   ├── digest.py                    分节纲要（调模型）
│   ├── formula.py                   公式就地纠错的坐标换算与回写
│   ├── judge.py / prompts.py        本地模型调用 / 提示词注册表
│   ├── learn.py                     从会话记录里挖经验
│   ├── maintain.py                  check / stats / backup / views / reindex-fst / vacuum
│   └── schemas.py                   数据契约 + 权重公式（唯一实现处）
├── online\                          在线检索与服务
│   ├── searcher.py                  混合检索 + 经验查询（唯一读库入口）
│   ├── server.py                    MCP 服务器（13 工具 + 4 资源）
│   ├── engine.py                    无端口引擎（管理面板用；stdio JSON-RPC）
│   ├── taskbox.py                   文件信箱任务通道（<知识库>\mailbox\）
│   ├── query.py                     中文分词 / 查询改写 / 覆盖率
│   ├── acquire.py / papermeta.py    把 DOI 交给 Zotero 抓 / 给 DOI 补元数据
│   └── localserver.py               旧 HTTP 服务（:8765；已停用，回退件 scripts\4-service.vbs）
├── tools\                           管理工具（自检、升级、打包、诊断…）
│   ├── test_engine_rpc.py           无端口引擎的 RPC 自检
│   ├── build_bootstrap.py           把 src\*.js 拼成 bootstrap.js
│   └── check_kb_levels.py           盯分级清单在 Python 与 JS 两侧一致
├── zotero-plugin\                   Zotero 插件源码 + 诊断脚本
│   ├── src\*.js                     按功能拆的源码（26 个文件）
│   ├── content\kbview.{html,css,js}     侧栏页面（条目窗格列 + 阅读器对照列共用）
│   ├── content\admin.{html,js,css} + admin-pages\*.js   管理面板页面（一页一个文件，共 10 页）
│   ├── content\vendor\              原样引入的第三方库（markdown-it / KaTeX，含 LICENSE 与字体）
│   └── bootstrap.js                 生成物（xpi 里装的是它，别直接改）
├── bundle\                          接入 DSH 的 MCP bundle
├── skills\                          给 DSH 的技能（使用规则 + 插件开发经验）
├── docs\                            版本发布说明
├── scripts\                         双击用的批处理 / VBS
└── .github\workflows\               CI 与自动发版

<Zotero 数据目录>\zotero-kb\          知识库（数据；可整包迁移/同步）
├── index.db                         单一事实来源（索引 + 经验 + 权重）
├── INDEX.md / MANIFEST.json         给人看的文献清单 / 构建报告
├── papers\  fulltext\  views\        档案 / 按页正文 / 分级视图（派生物）
├── edits\                           你亲手改过的正文
├── mineru\                          MinerU 解析产物（可选组件）
├── trash\                           已删除文献的归档
└── inbox\  logs\  .cache\           待确认经验 / 日志 / 模型缓存

~\.dsh\                               平台管理
├── profiles\desktop\                插件与 profile 配置
├── skills\                          技能（从 <项目目录>\skills 复制过来）
└── zotero-bridge\                   文件信箱（requests / results / done）
```

> 目录里每一项"是什么 / 占多大 / 能不能删"的**唯一事实来源**是
> `offline/kb_manifest.py::KB_FILE_SPEC`（管理面板「知识库结构」页读它）；
> 面向使用者的版本在 [`INSTALL.md`](INSTALL.md) 末尾。

### 插件为什么要"源码多文件 + 生成单文件"

Zotero 的 bootstrapped extension **只加载 xpi 根目录下的 `bootstrap.js` 一个
文件**（`manifest.json` 也必须在根目录）。所以源码分成 26 个文件之后，运行时
仍然必须是一个文件：

```
zotero-plugin/src/00-core.js … 99-bootstrap.js
        │  python tools/build_bootstrap.py
        ▼
zotero-plugin/bootstrap.js     ← xpi 里装的是它
```

每个源文件用 `Object.assign(ZoteroKB, { … })` 把成员挂到插件对象上，所以
**每个文件单独也是合法 JS**，能逐个做语法检查（`tools/check_js_syntax.py`）。

为什么不用运行时 `Services.scriptloader.loadSubScript` 真多文件加载：
① 往 xpi 里加子目录是新的未知（本项目在"装不上、报错又没有线索"上吃过很多次）；
② 沙箱里 `loadSubScript` 的目标 scope 没实测过，属于在最容易黑箱失败的地方
引入新失败模式。拼接法让**运行时与拆分前完全一样**。

代价是多了一条"改源码要重新生成"的规矩 —— 这条规矩由三道闸门保证：
CI 里的 `build_bootstrap.py --check`、`check_plugin.py` 的第一项、
以及 `pack_plugin.py` 打包前的硬性拒绝（**不同步就不出包**）。

---

## 六、可复现性：换一台机器怎么搭

这套系统的价值一半在"能复现"。按顺序做：

| 步 | 做什么 | 验证方式 |
|---|---|---|
| 1 | 装 Python 3.12+ 并建 `.venv`（或直接跑 `scripts\install-env.cmd`，它会用自带的 `uv` 下一份解释器） | `python offline\maintain.py check` |
| 2 | 装 Zotero（建议 10+）并设置数据目录；（可选）装 Ollama 并拉 `qwen3:4b-instruct` | `tools\zotero_upgrade.py check` |
| 3 | 跑 `scripts\1-convert.cmd` 建库 | `scripts\3-maintain.cmd stats` 显示条目/切片/向量 |
| 4 | 用 bundle 接入 DSH：`dsh plugin --profile <p> add "link:<...>/bundle"` | DSH 里出现 `mcp__zotero-kb__*` 工具 |
| 5 | 装 Zotero 插件（**必须完全重启 Zotero**） | 文献列表出现「知识库权重」列 |
| 6 | 跑 `python tools\test_engine_rpc.py`（引擎 RPC 自检） | 退出码 0 |
| 7 | （可选）装 DSH 侧收件箱 bundle | 往信箱丢个 json，看 results 出结果 |
| 8 | **（只在要复现旧 HTTP 通道时）** 跑 `scripts\4-service.vbs` 起本地服务 | `http://127.0.0.1:8765/health` |

**常见错误**：
- DSH 的 patch 层是**覆盖**语义，新增条目必须写在 `- insert:` 里
- 插件 manifest **必须**有 `update_url`（否则装不上，报错却只说"可能不兼容"）
- Zotero 插件更新后**必须完全重启 Zotero**（不会重跑 `startup`）
- 读 Zotero 库**必须**连 `-wal`/`-shm`

---

## 七、边界与已知限制

说清楚边界和说清楚能力一样重要：

| 不做 | 原因 |
|---|---|
| 不写 `zotero.sqlite` | 直写要自己处理 `synced` / `version` / 同步队列，写错会污染并同步到 zotero.org；写操作一律走 Zotero 自己的本地 API（Zotero 10 起支持） |
| 不自动下载 PDF（在 Python 侧） | 抓取一律交给 Zotero 进程：才带得上校园网出口 IP、机构订阅与浏览器登录态（`skills\zotero-acquire` 里的直链下载只当兜底） |
| 不替你做分类决策 | 模型只给**建议**，要不要应用由你点 |
| 不让模型读整篇正文 | 先搜后读，按页取；否则上下文会爆 |
| 不做云端同步 | 全部本地（Zotero 自己的同步不受影响） |
| 不做 OCR | 本机样本 PDF 全部有文字层，所以没接 OCR。扫描件会被明确标成没有正文，不会静默返回空 |
| 不搬运笔记里的内嵌截图 | 那类图只留一行占位说明（图在 Zotero 里看） |
| 知识库不是实时同步 | Zotero 里改了内容要跑一次构建（`1-convert.cmd`）或调 `kb_reindex` |
| 不识别图片内容 | 视觉模型对学术图表实测不好用；知识库给的是**图注与表格** |

写好回 Zotero 的三类低风险东西只有：**分类归属、标签、回收站状态**（删除 / 还原）。
AI 的产出（摘要、纲要、建议）只落在知识库目录或会话目录里，不写回文献本身。

---

## 八、能力清单与自证

> 具体数字（条目 / 切片 / 向量 / 经验条数）会随库变化，**不要抄这里**：
> 面板「知识库结构」页、`scripts\3-maintain.cmd stats` 或 AI 里的 `kb_stats`
> 都是现成的权威来源。这一节只说"哪些部件是通的、拿什么自证"。

| 部件 | 状态 | 怎么自证 |
|---|---|---|
| 离线管道 | 读 Zotero 只读快照 → 切片 → 向量 → index.db | `offline\maintain.py check` |
| 正文来源 | Zotero 缓存 / PyMuPDF / MinerU 三种，按篇决定 | 面板「PDF 解析」页的「正文来源」列 / `items.fulltext_src` |
| 正文修订层 | 按页覆盖 + 改完立即重切重向量 + 「基于旧解析」提示 + 草稿导入导出 | 侧栏「改这一页」→ 保存后 fulltext 与 chunks 变；重跑全量后修订仍在 |
| 父子切块 | 父块（节/章）不进 FTS/向量；子块带 `section_path`；命中子块返回父块上下文 | `maintain.py check`（FTS 与应索引子块一一对应）+ 归档→还原 `parent_id` 无悬空 |
| 索引层 L2 清洗 | 切片/向量只吃清洗后的正文，L1 不改 | `tests\test_l2_clean.py`（测试不随仓库发布） |
| 摘要深度分层 | `chunks.depth`（逐段 / 逐节 / 全篇概要）；全篇概要块**默认参与检索** | `online\searcher.py` 的 `ZOTERO_KB_DEPTH` 开关 |
| 混合检索 | 中文短语召回 + 向量 + RRF + 权重上浮 + small-to-big 上下文 | `online\searcher.py --test` 或面板「高级」页检索 |
| MCP 接入 | **13 工具 + 4 资源** | AI 里 `kb_search` 等可直接调用 |
| Zotero 插件 | 自动入库 + 分类建议 + 元数据补全 + 生成纲要 + 列表两列 | 右键菜单与列表列 |
| 「知识库」侧栏 | 自建一列独占条目窗格（rail 最下面一个图标 → 开/关） | 打开条目右侧栏看 |
| 阅读器对照列 | 左 PDF 右正文、同一个窗口；工具栏「对照」按钮；停在正在读的那一页（翻页**不**跟随）；KaTeX 排版；按块编辑 | 打开 PDF 点「对照」 |
| 公式就地纠错 | 右键公式 → 编辑区 → 应用（只改这一条并重切重向量） | 打开 PDF 右键一块公式 |
| 管理面板 | Zotero 内一个窗口，10 页；与引擎之间**无端口、无 token、不常驻** | 工具菜单 →「知识库管理…」逐页走 |
| 条目生命周期 | 归档 / 还原 / 彻底删 + 对账巡检（父子结构一并快照/还原） | `offline\maintain.py reconcile` |
| 旧 HTTP 通道 | 已停用；`--test` 自带**进程内**服务，不需要 8765 在监听 | `python online\localserver.py --test` |
| DSH 侧接入 | 文件信箱版可用 | 往信箱丢 json → DSH 对话里出现 |
| 提示词注册表 | 话术可看可改可还原 | 面板「提示词」页 |

**13 个 MCP 工具**：`kb_search` / `kb_item` / `kb_fulltext` / `kb_figures` /
`kb_experience_add` / `kb_experience_query` / `kb_weight_set` / `kb_reindex` /
`kb_stats` / `kb_paper_info`（候选文献的摘要/关键词/OA/被引）/
`kb_acquire`（把 DOI 交给 Zotero 抓）/ `kb_acquire_progress`（抓取进度）/
`kb_attach`（把本地 PDF 挂到条目上，兜底用）。

**4 个资源**：`zotero-kb://collections`（分类清单）、
`zotero-kb://item/tldr/{key}`（摘要级要点）、`zotero-kb://item/{key}`（完整档案）、
`zotero-kb://item/full/{key}`（带页码锚点的全文，按需读）。

## 九、开发与维护

### 改代码前的两条硬规矩

1. **改了 `zotero-plugin/src/*.js`，必须跑 `python tools\build_bootstrap.py`**
   重新生成 `bootstrap.js`。xpi 里装的是这个生成物 —— 忘了生成，插件表现就像
   "代码没改"，而 Zotero 不会给你任何线索。三道闸门保证这条规矩：
   CI 里的 `build_bootstrap.py --check`、`check_plugin.py` 的第一项、
   以及 `pack_plugin.py` 打包前的硬性拒绝。
2. **`zotero-plugin/content/vendor/` 里只能放原样引入的第三方库**
   （现在是 markdown-it 与 KaTeX，各自带 LICENSE 与字体）。
   审计器对 `content/vendor/` 前缀只保留"密钥 / 用户目录 / 开发机痕迹"那几条检查、
   跳过压缩 JS 必然误报的启发式 —— 一旦把本项目的代码放进去，就等于绕过了那套检查。
   升级第三方库时整目录替换，**不要手工改 min 文件里的字符**。

### 自检链（发版前全跑一遍）

| 命令 | 查什么 |
|---|---|
| `python tools\audit_release.py` | 发布面综合审计：隐私泄露（密钥 / 用户名）、写死的绝对路径、安装可用性、编码卫生（UTF-8 BOM）、`.env` 覆盖层 |
| `python tools\check_plugin.py` | 插件静态检查 + 打包 xpi（成员引用、pref 默认值、l10nID、打包排除列表…） |
| `python tools\build_bootstrap.py --check` | `src\*.js` 与生成物 `bootstrap.js` 是否同步 |
| `python tools\check_js_syntax.py` | 每个 `src\*.js` 单独做语法检查 |
| `node tools\verify_bootstrap.js` | 用桩 `Zotero` 跑真机 `bootstrap.js`，盯"字段名写错但没报错"这类问题 |
| `node tools\verify_kbview_md.js` | 页面里的 markdown 管线（vendored markdown-it + KaTeX + 本项目规则）真跑一遍，判据是**渲染质量**而不是"没抛异常" |
| `python tools\check_kb_levels.py` | 分级清单在 Python（`offline\kbviews.py`）与 JS（`src\13-kbopen.js`）两侧一致 |
| `python tools\check_api_presets.py` | 服务商地址表在 `judge.py` 与 `settings.js` 两处一致 |
| `python tools\make_updates_json.py --check` | `updates.json` 与 manifest 版本一致 |
| `python online\localserver.py --test` | 旧 HTTP 服务端点自检（自带进程内服务，不需要 8765 在监听） |

`tests\` 下的测试在本机跑（需要真实知识库），不随仓库发布。

### 发版流程

```bash
# 0. 改过插件源码（zotero-plugin/src/*.js）就先重新生成，否则打包会被拒绝
python tools/build_bootstrap.py
# 1. 改 zotero-plugin/manifest.json 里的 version
# 2. 同步更新清单，和上一步**同一次提交**（否则 CI 的 --check 会红）
python tools/make_updates_json.py --tag vX.Y.Z
git commit -am "<中性的一行说明>" && git push origin main
# 3. 打**带附注**的 tag 推上去 —— 附注就是发布说明
git tag -a vX.Y.Z -m "<这一版对使用者的说明>" && git push origin vX.Y.Z
```

`.github/workflows/release.yml` 会依次：**跑自检 → 打包 xpi → 审计 →
生成 `updates.json` → 发 Release（xpi 作附件）→ 把 `updates.json` 提交回 main**。
最后一步是关键：`updates.json` 落在 main 上，Zotero 靠它发现新版本；
忘了更新它的后果是**用户永远停在旧版、且没有任何报错**，所以有一条
"tag 与 manifest 版本必须一致"的守门员兜底。发布中途失败可以直接重跑
（Release 已存在时会覆盖附件）。

**版本号怎么定**：只改文案 / 修小 bug → 只动最后一位，说明一行就够；
加功能、行为有变化 → 动中间位，写清"多了什么、怎么用"；架构换代 → 动第一位，
并明确**升级不需要额外动作**；破坏性变更 → 动第一位，必须写清怎么迁移。

**提交信息是公开的**，按"别人需不需要知道"分两档：面向使用者的改动详细写
"改了什么、为什么"；内部整理 / 自检加固 / 修既有缺陷**一句话中性描述就够**
（写"哪里曾经漏了、错了"等于公告自己的问题，对使用者毫无价值）。
复盘写在代码注释或本机工作档案里，不写进提交信息。

### 两类要特别小心的改动

- **`offline/edits.py` 是正文修订层的唯一读写路径**，`<知识库>\edits\` 是
  **用户资产**（删了就等于放弃用户改过的正文）。任何"清理知识库"的改动都必须跳过它。
- **切块参数**（`converter.sectionize_pages` / `collect_chunks`）一改就要
  **全量重建 + 重跑检索评估**，同时检查 `trash.py` 的快照/还原列与
  `maintain.py` 的 FTS 一致性判据（这两处漏了是**静默**的）。实验开关：
  `ZOTERO_KB_CHILD_TARGET` / `ZOTERO_KB_PFTS`（构建期）。

### 后续可做

- **更强的嵌入模型**：`fastembed` 支持 `intfloat/multilingual-e5-large` 等；
  换模型后跑一次 `1-convert.cmd --full`（代码会自动清掉旧模型的向量重算）。
- **OCR**：给扫描件补文字层（现在扫描件的文字来自 Zotero 缓存里已有的 OCR 结果）。
- **图谱与跨文献联系**：分类树 → 树距离进检索 → 实体 / 关系抽取。
- **摘要与图注字段的独立修订**：现在修订层只有"正文按页"。
- **经验闭环 2.0**：时间衰减 + 关系类型（supports / contradicts / extends）。
- **跨行公式**：页面里的块级公式规则已支持跨行 `$$…$$` / `\[…\]`，
  改这条规则时补一个跨行的桩测试。

---

## 附：这套系统真正的"复利"在哪

单看每一块都不稀奇：切片、向量检索、RAG、插件，都是常规做法。
**真正让它产生复利的是链路 3**（经验回流）：

- 一般的文献库只解决"我存过什么"
- 这套系统还回答"**我试过什么、哪条路走不通、为什么**"
- 而后者才是做研究时最费时间、最容易重复踩坑的部分

所以系统里最该被认真维护的不是代码，而是 `experience` 表 ——
每做一次尝试就记一条，检索时它就会把"验证过有效"的文献往上推。
这也是为什么权重公式里 `ineffective` 也有正分：
知道哪条路不通，本身就是在省时间。