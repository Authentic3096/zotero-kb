# 架构总览：一套可复现的「文献知识库 → 大模型可用」系统

> 这份文档回答一个问题：**我们到底在做什么？**
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
中间用**两个本地服务**把它们的边界缝起来，再用**经验权重**让系统越用越准。

```
        ┌──────────────────────── Zotero（文献的家）────────────────────────┐
        │  条目 / PDF / 标注 / 分类 / 阅读进度                                │
        └───────────┬─────────────────────────────────────┬─────────────────┘
                    │ 只读快照（含 WAL）                   │ 插件写回
                    ↓                                     ↑
   ┌────────────────────────────────────────────────────────────────────┐
   │            ① 知识库（Python，<项目目录>\）              │
   │                                                                    │
   │  离线管道 offline\        在线服务 online\         经验层 experience\ │
   │   ├ zreader 读快照        ├ searcher 混合检索      ├ 一次尝试=一条记录 │
   │   ├ converter 切片        ├ server.py MCP         ├ outcome 四态      │
   │   ├ 向量 bge-small        └ localserver.py HTTP   └ 权重→排序上浮     │
   │   └ 全文/元数据入库                                                │
   └───────────┬──────────────────────────┬─────────────────────────────┘
               │ MCP（stdio）             │ HTTP（:8765）
               ↓                          ↑
   ┌───────────────────────┐   ┌──────────┴──────────────────────────────┐
   │  ② DSH（推理与对话）    │   │  ③ Zotero 插件（Zotero 进程内）          │
   │                       │   │                                          │
   │  8 个 MCP 工具         │   │  · 新条目 → 自动切片 + 弹分类建议         │
   │  kb_search/item/       │   │  · 文献列表「知识库权重」列（带颜色）      │
   │  fulltext/experience/  │   │  · 设置面板 / 任务轮询                    │
   │  weight/reindex/stats  │   │  · 右键「发送到 DSH」← 经文件信箱         │
   │  4 个资源              │   └──────────────────────────────────────────┘
   └───────────────────────┘
```

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
2. **插件不能跑模型** —— 模型、PDF、向量都在 Python 侧，插件只发 HTTP。
3. **DSH 的进程内服务外部够不着** —— 所以"发到对话"必须由 DSH 插件做，
   外部（Zotero）只能通过中间层投递。

---

## 三、数据流（四条主链路）

### 链路 1：文献进库（离线管道）

```
Zotero 库 ──只读快照(含 -wal/-shm)──> zreader ──清洗/HTML转MD/按页切──> converter
                                                                        │
                          ┌─────────────────────────────────────────────┤
                          ↓                      ↓                      ↓
                  chunks + FTS5 索引        向量(bge-small-zh)      全文 Markdown
                          └──────────┬───────────┘                kb/papers/*.md
                                     ↓
                              kb/index.sqlite  ← 单一事实来源
```

**要点**：
- 快照**必须**连 `-wal`/`-shm` 一起拷（Zotero 10 起启用 WAL，只拷主库会读到过时数据）
- 增量判定比 **`version` + `collections_sig`**（分类/标签改动不动 `version`）
- 全量重建约 100 秒，增量 0.3 秒

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
① Zotero → 知识库（切片、分类建议）
   Zotero 插件 ──HTTP :8765──> localserver.py ──> convert.py / judge.py（Ollama）

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

### 链路 5：DSH 找新文献 → Zotero 抓进库（需求 9）

```
① DSH 侧（搜索与呈现，**都在对话里**）
   web_search 找候选 → Crossref/OpenAlex 核 DOI（标题对不上就扔）
   → kb_paper_info 补关键词/OA/被引
   → 在对话里列候选表（**标题是 doi.org 链接**，用户自己点开看原文）
   → ask_user_question(multi_select) 勾选

② 派发（勾完就抓，不再二次确认）
   kb_acquire(wait=False) ──HTTP :8765──> 任务队列（kind="acquire"）
                                          ↓ 插件每 2 秒轮询领取
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
  ⚠ 只出现一次的首页 DOI / 版权行抓不到 —— 这是已知取舍。
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

#### B2. 经验层：系统里唯一会自我改进的部分

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
配置优先级"调用时传参 > `kb/llm-config.json` > 环境变量 > 默认值"，
传参优先是为了让插件设置面板说了算（`judge.py:45-47`）。

| 用途 | 怎么做的 |
|---|---|
| **分类建议** | 提示词把已有分类连同**篇数与标题样例**一起给模型做 few-shot，要求"只能从已有分类里选**一个**、原样照抄"；模型改写分类名时做一次宽松匹配，**只接受唯一命中**，含糊就宁可空（`judge.py:695-761`）。支持"跟模型对话调整"，但可选范围不变 |
| **打标签** | 出 3-6 个中文标签 + 一句话主题，只出建议不写库（`judge.py:513-531`） |
| **分类去重** | 中英文分类是否同一个交给模型 —— 嵌入模型跨语言几乎没区分力（`「机器学习模型」↔"machine learning model"` 只有 0.518，落在无关词噪声区） |
| **元数据兜底** | 规则优先、纯规则时秒回；只有规则抽不到字段才调模型，所以做成同步接口（`localserver.py:431-441`） |

**输出解析**：统一先剥 ``` 代码块、再 `json.loads`，失败就抠最外层 `{}`
（`judge.py:464-490`）；仍失败返回结构化错误而不是抛异常。
`generate()` 会把 HTTP/网络错误翻译成可操作的 hint（key 过期、
base_url 是否要带 `/v1`、连不上谁）（`judge.py:379-404`）。

#### B5. MCP 服务器

`online/server.py`，**stdio** 传输（由 DSH 的 `dsh-mcp-client` 拉起），
`instructions` 里写死用法约定：先搜后读、用完记经验、不写 Zotero
（`server.py:53-62`）。启动只打印统计、不读索引库，`kb()` 懒加载（`server.py:1125`）。

13 个工具与 4 个资源见本文档第八节。

#### B6. 本地 HTTP 服务（插件 ↔ Python 的通道）

`online/localserver.py`，`ThreadingHTTPServer` 只 bind **127.0.0.1**，默认端口 **8765**。

**token 机制**：优先环境变量 `KB_SERVICE_TOKEN`，否则读 `kb\service-token.txt`，
文件不存在就生成并落盘；按文件 mtime 缓存，**换了 token 不必重启服务**
（`localserver.py:87-122`）。校验用 `secrets.compare_digest` 定时比较（`:2034-2040`）。

> **一处值得抄的安全设计**：`GET /` 与 `/health` 免 token，并顺手把 token 回给调用方
> （方便插件自动配置）；但请求若带 `Origin`/`Referer`（只有浏览器会发）就**拒发 token** ——
> 挡住"恶意网页 fetch localhost 偷 token"。Zotero 插件用自带 HTTP 封装、不带 Origin，
> 正常通过（`localserver.py:2055-2090`）。

**任务队列**是"能在 Zotero 里执行一段 JS"的通道：只监听本机、必须带 token、
任务只留最近 30 条且**不落盘**（`localserver.py:55-67`）。
`kind` 有 `js / cmd / acquire / attach`；领取是原子的 `pending → running`
并记 `fetched_by`（`:174-207`）。


服务还带**库变化监听**：每 20 秒看一次，发现新条目就切片并生成分类建议，
写进 `kb/pending-suggestions.json`（`localserver.py:1859-1879`）。
`POST /shutdown` 用事件位 + 看门狗退出，刻意不用 `taskkill pythonw.exe`
—— 那会连管理面板和用户脚本一起杀掉（`:2519-2558`）。

#### B7. Zotero 插件的几个关键机制

插件是 **bootstrapped extension**（Zotero 7~10）。
**源码在 `zotero-plugin/src/*.js`，按功能分 20 个文件**；`zotero-plugin/bootstrap.js`
是它们拼出来的**生成物**（`tools/build_bootstrap.py`，xpi 里装的就是它）——
原因见「五、目录与归属」里那一段。

**启动是"分步执行 + 逐步记录"**：`registerPrefs → registerPrefObserver →
registerPrefPane → registerNotifier → registerWeightColumn → startTaskPolling`，
每步成败进 `steps` 并写进状态文件 —— 因为 Zotero 的调试日志重启即失
（`src/00-core.js` 的 `startup`）。

> 工具栏按钮与右键菜单**在 `startup` 里也要注册一次**：实测 `onMainWindowLoad`
> 在 Zotero 启动时**从未被调用**（那时主窗口已存在），该钩子只对"插件启动后新开的窗口"
> 触发（`src/00-core.js` 的 `startup` 里那段重试）。

**「知识库权重」列**：`Zotero.ItemTreeManager.registerColumn`。关键约束是
ItemTree 的 `dataProvider` **同步且逐行调用**，不能在里头发网络请求
（几百行会打爆服务）—— 所以启动/刷新时拉一次 `GET /weights` 全量进 `weightsCache`，
provider 只做 O(1) 查表（`src/16-weightcol.js` 的 `registerWeightColumn`）。

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
每 2 个 tick（约 4 秒）刷一次权重缓存（原来 30 秒，用户在别处改权重后
要等半分钟才跟上、像"没生效"）；每 15 个 tick（约 30 秒）才写一次状态文件，
因为那是磁盘 IO（`src/18-taskpoll.js` 的 `runTask`）。状态文件在 `kb/plugin-status.json`，
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

- **面板**：第一屏的「打开知识库」→ 先弹文献列表（复用已有的
  `PaperPicker`：作者/年份/标题、可搜索可排序）→ 选中一篇再列六个级别
  （`tools/panels/browser.py` 的 `LevelPicker`）→ 双击用系统默认程序打开那个 md。
- **Zotero 右键**：第 ⑥ 组「打开知识库」二级菜单列同样的六个级别
  （`src/13-kbopen.js` 的 `KB_LEVELS` 是 Python 那份的**镜像**，
  由 `tools/check_kb_levels.py` 盯着 id/标签/路径模板三者一致）；
  缺文件的级别在文案里标「（还没生成）」而**不设 disabled**
  —— 实测 menupopup 里只要有 disabled 的 menuitem，整个子菜单就点不开。
- **MCP**：资源 `zotero-kb://item/tldr/{key}` 的渲染实现就是
  `kbviews.render("tldr", …)`（原来写在 `online/server.py`，搬过去避免两份实现漂移）。

**两个不显然的决定**：

1. **视图文件在构建时生成，不在"打开时"现生成**。Zotero 右键菜单是**同步**
   构建的（弹出来那一刻不能等网络、也不能等 Python 起进程），它只能靠
   "文件在不在"决定显示什么。所以 `offline/convert.py` 在一批条目**全部提交
   之后**统一生成；单独补齐用 `offline/maintain.py views`（面板「高级」页的
   「补齐知识库分级文件」就是它）。
2. **打开文件走四级降级链**（`src/13-kbopen.js` 的 `openKbPath`）：
   `Zotero.launchURL` → `外部协议服务` → `面板的 pythonw gui.py --open <路径>`
   （`os.startfile`，本机一直在用、确定可用）→ `Zotero.File...reveal()`
   （保底：在文件管理器里选中它）。为什么写成链式而不是挑一个：沙箱里这些
   入口的可用性随版本变，而链式能保证最坏情况也有确定可用的兜底。

`views/` 与 `papers/`、`fulltext/` 同待遇：**可重建的派生物**，删了跑一次
构建就回来；条目被删除时 `_purge_key` 会连它一起清掉。

#### B9. 管理面板

`tools/gui.py` 只是**入口**（组装 App + 命令行自检 + mainloop）；
界面代码按"一个页签一个模块"在 `tools/panels/` 下，公共的路径与字体在
`tools/panels/common.py`（`bootstrap.js` 与 `schemas.resolve_project_root()`
都拿 `tools/gui.py` 当"项目根在哪"的判据，所以它必须继续存在）。

**tkinter/ttk**，底部常驻日志区。八个页签（`tools/panels/base.py` 的
`_build_tabs`）：
**知识库结构**（逐项写清"存放内容、占多少、能不能删"）、**经验库**、
**分类建议**、**运行环境**（项目目录 / Python / Ollama 三个位置 + 检测报告 ——
面板打不开九成是这三者之一不对；这三项**面板里也能改**，写的是与插件设置
同一份 `kb-location.json`）、**损坏查询**、**元数据**、**高级**
（侧载安装、诊断、打包、复制 token、补齐分级文件这类功能集中在这里，
不挡日常操作）、**提示词**（见 B12）。

版面（用户 2026-10-05 要求）：每个页签都套一层**可滚动画布**（`_tab_panes`，
内容比窗口高时用滚轮看，不必拉长窗口）；页签内部要"钉在下面"的文本框
（输出/详情/试跑结果）与底部**运行日志**都是**可拖的分隔格**（`ttk.PanedWindow`，
`_scroll_hosts` + `_on_wheel` 负责"滚鼠标底下那一页"）。

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
写 `kb/papers/<key>.md` 与 `kb/fulltext/<key>.md`（页锚点 `## p.N` 由 convert 写）→
切片/向量/MANIFEST，来源记进 `items.fulltext_src`。所以换文本源**只换这一个调用**。

- 默认（`zotero`）：`offline/zreader.py` 的两条路（Zotero 自己的 `.zotero-ft-cache`
  与 PyMuPDF）+ 字符偏移还原 + 页眉过滤 + 灰区模型仲裁。**没装 MinerU 的用户
  走的就是这条**，与之前逐字节一致。
- 可选（MinerU）：`offline/mineru.py` 两件事 —— **探测**（`probe()`：装没装、
  版本、生效的后端与 VLM 引擎、模型档位齐不齐、GPU 可用不可用，结果缓存 60 秒；
  挂在只读端点 `/mineru-check` 上，给插件首启对话框、面板「运行环境」第 4 行、
  「知识库结构」页那个"只在没装时出现"的按钮）与**解析**
  （`parse_item()` / `fulltext_for()`，见下面 B11.1）。
  默认仍是 `zotero`，所以**没装 MinerU 的用户行为逐字节不变**。
- 安装：`scripts/install-mineru.ps1`（可选组件，装在项目内的 `.mineru\`，
  不写 PATH/注册表，删目录即卸载）+ 图形化引导
  `tools/panels/mineru_guide.py`（说明 → 档位 → 预检 → 后台装 → 复检并写回配置）。

  ⚠ 三条实测出来的坑（脚本与文档里都写了）：Windows 上 `mineru[full]` 装的是
  **LMDeploy**（vLLM 只有 Linux），而它在本机起不来
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
| **批量** | `mineru.parse_many(pairs, batch_size=20)`：把 PDF 以 `<KEY>.pdf` **硬链**到临时输入目录 → `parse <目录> -o <输出> --format zip` **跑一次** → 按 `<KEY>.zip` 映射回 key 落产物；日志走 `_run_stream`（实时，面板也看它） | ⚠ **每次 `mineru-kit parse` 都要重新加载模型**（实测约 2 分钟）：逐篇跑 93 篇 ≈ 3 小时；批量时模型只加载一次，实测 1.7 页/秒。`missing`/`reparse` 因此都是「先批量解析（写好产物与指纹），再让 convert 命中指纹、只做切片与向量」 |

⚠ 三个实测出来的坑（都写进代码注释了）：
① 全库解析前**先退出 Ollama** —— 抢同一块显存，同一篇 5 页论文模型阶段
104.8 秒 → 8.9 秒（0.048 → 0.223 page/s）；
② `convert.build(args)` **只吃一个参数**、且内部要读
`args.figures/no_figures/embed_model` —— 自己造 Namespace 时字段要抄全；
③ 那段"把配置同步进插件 pref"的 JS 不能塞进 f-string（嵌套 dict 花括号会让
f-string 解析炸掉），已抽成纯函数 `llm_sync_js()` 并交给 `node --check` 单测。

#### B14. 右侧栏「知识库」分区
用户：「zotero 的文献右侧栏是可以正确显示 md 格式的，能不能把知识库中的 md 在这里
显示，方便和原文对照？」→ `zotero-plugin/src/20-kbview.js`。

- **只读展示**：读 `kb/` 里现成的 md → `md2html()` → 写进分区 body。
  不发请求、不写数据；文件不在就显示"还没生成"的文案（**不在窗格里现场生成** ——
  纲要要按节调模型，一篇学位论文几分钟）。
- **级别下拉**：摘要与要点 / 分节纲要 / 完整档案 / 按页正文 / 图注与表格 / 权重与经验；
  选择记进 pref（默认**分节纲要**）；文件缺失时按 纲要 → 摘要 → 档案 回退并在
  提示行写明"已回退"。
- ⚠ **与已删除的「窗格本地模型对话」（19-itempane）的区别**（别把这次回退当成复辟）：
  那个是交互（提问→调模型→多轮讨论），用户判定没用且 bug 多，整条链删了；
  这个是只读。所以 `ItemPaneManager` 重新出现是有意的 ——
  `check_plugin.py` 反向清单里只留"对话窗格"那批函数名（`paneSend`/`chatOf`…）。
- ⚠ **l10n 必填且必须"只有名字、值留空"**：`header`/`sidenav` 的 `l10nID`
  若在 ftl 里带了值，Zotero 的 `translateFragment` 会把节点内容抹掉 ——
  结果是"分区标题空白 + `onRender` 拿到的 body 是 null"，**且不报错**（实测踩过）。
- `md2html()` 是纯函数（桩测试 8 项）：标题/列表/引用/粗斜体/行内代码/链接/
  `$$公式$$`（保留 LaTeX、等宽底纹）/图片降级成图注；**先转义 HTML 再套标记**。

#### B15. 能力探测的菜单状态行
第一版把「（未连接）/（检测中）」挂在**顶层标题**上，出现两个问题：
① 标题太长；② 明明子菜单已经列出对话列表，标题却写"检测中"（`caps.dsh` 还是 `null`，
因为探测是周期性的）。改法：
- 顶层标题**不带后缀**；状态放到**子菜单第一行**（`capStateLine()`，纯函数）：
  `● 已连接 DSH　12 个对话` / `○ 未连接：桥目录不存在` / `○ 还没检查过连接（点这一行重新检查）`；
- **用这次真拿到的结果回填**：`listDSHSessions()` 成功即 `caps.dsh = true`
  —— 不再出现"列出对话却写检测中"；`null` 时不写"检测中"，据实说"还没检查过"。

#### B16. 元数据补全：两路首页正文 + 模型综合
需求：「元数据补全能不能同时读首页和 MinerU 首页，然后本地模型综合一下再给出来。」

- **两路**：A = `zreader.fulltext_for()`（PDF 原文字面）；B = `<kb>/mineru/<KEY>/pages.json`
  （MinerU 逐页正文）。取文本/合并/渲染全在**新模块** `offline/metafill_sources.py`
  （单一职责，纯函数，`tests/test_metafill_sources.py` 覆盖 36 项）。
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
  ⚠ 缓存判据里带 `tried`：有些节本来就产出不了要点（正文过短/模型没给结果），
  只认"有 summary"的话这些节会**每次构建都被重问**（实测第二次仍有 1 节被重问）。
- **只读不生成**：`kbviews._render_outline` / MCP 资源 `zotero-kb://item/outline/<KEY>`
  只读现成产物；生成入口是面板按钮与 `python offline/digest.py <KEY>` ——
  一篇学位论文要几分钟，不能挂在"随手点一下"的读路径上。
- **不写进索引、不改排序**（用户明确"检索分层算了吧"）。

#### B18. 提示词注册表：话术只有一份，且用户改得动

`offline/prompts.py`（2026-10-05 起 6 条：tag/summary/extract/metafill/
draft/chunks —— chat/para/propose 随窗格删了）+ 覆盖文件 `kb/prompts.json`。要点：

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
- **`history` 留档**：`-- 经验层：只追加，永不覆盖` 对**自动流程**仍然成立，
  只对用户显式编辑开口子，且旧值 push 进 `history`。
- **新增两个写入口**：面板「修改/增添经验」（口述 + 表单两条路，表单任何时候都能用）
  与 CLI `kb_admin.py add/edit`。
- **id 连续编号**：删除后自动把 id 重排成 1..N（`renumber()`；两段式写 ——
  先把整表改成负数再写成目标值，否则主键会当场撞车）。批量删走
  `delete_experiences()` **一次事务 + 只重排一次**：循环调单条删除会边删边重排，
  第二条就删到别的行上了。库里没有任何地方按 id 引用经验（`history` 是行内 JSON、
  `item_weight` 按 item_key、`pending.jsonl` 用会话号），所以重排是安全的；
  老库留下的空号用 `kb_admin.py renumber` 修一次。
- **打开旧库自动补列**：迁移原来只在 `init_db`（构建时）跑，于是"加了新列还没重建库"
  的窗口里写入会撞 `no such column` —— 现在 `schemas.connect()` 顺手补一次（幂等）。

## 五、目录与归属

```
<项目目录的上级>\                       跨工作区可复用
├── zotero-kb\                       ★ 知识库本体
│   ├── offline\                     离线管道：读 Zotero、清洗、切片、向量
│   │   ├── zreader.py               只读快照（含 WAL）
│   │   ├── converter.py             清洗 / HTML→MD / 按页切块
│   │   ├── convert.py               CLI 构建入口（收尾时生成分级视图）
│   │   ├── kbviews.py              ★ 分级视图：LEVELS + 渲染 + 落盘（唯一事实来源）
│   │   ├── judge.py                 本地小模型（Ollama）调用
│   │   ├── learn.py                 从会话记录里挖经验
│   │   ├── maintain.py              check / stats / backup / views / reindex-fst / vacuum
│   │   └── schemas.py               数据契约 + 权重公式（唯一实现处）
│   ├── online\                      在线服务
│   │   ├── searcher.py              混合检索 + 经验查询（唯一读库入口）
│   │   ├── server.py                MCP 服务器（13 工具 + 4 资源）
│   │   ├── query.py                 中文分词 / 查询改写 / 覆盖率
│   │   └── localserver.py           给 Zotero 插件的 HTTP 服务（:8765）
│   ├── kb\                          数据（可迁移）
│   │   ├── index.sqlite             ★ 单一事实来源
│   │   ├── papers\*.md              每篇文献的 Markdown
│   │   ├── fulltext\                按页正文
│   │   ├── views\*.md               ★ 分级视图（摘要与要点 / 图注与表格 / 权重与经验）
│   │   ├── MANIFEST.json            构建清单
│   │   └── taxonomy-plan.json       分类重整方案
│   ├── tools\                       管理工具（面板、升级、同步、打包、诊断…）
│   │   ├── gui.py                   面板**入口**（组装 App + 命令行自检 + mainloop）
│   │   ├── panels\                  ★ 面板各页签（base / tab_* / browser / common）
│   │   ├── paper_picker.py          选文献的弹窗（列宽可拖、可排序）
│   │   ├── build_bootstrap.py       ★ 把 src\*.js 拼成 bootstrap.js
│   │   ├── check_kb_levels.py       ★ 盯分级清单在 Python 与 JS 两侧一致
│   │   └── audit_panel.py           面板体检（按钮背后真的有东西吗）
│   ├── zotero-plugin\               ★ Zotero 插件源码 + 诊断脚本
│   │   ├── src\*.js                 ★ 按功能拆的源码（20 个文件）
│   │   └── bootstrap.js             ★ 生成物（xpi 里装的是它，别直接改）
│   ├── tests\                       本机跑的测试（要真实知识库）
│   ├── bundle\                      接入 DSH 的 MCP bundle
│   ├── skills\                      给 DSH 的技能（使用规则 + 插件开发经验）
│   └── scripts\                     双击用的批处理 / VBS
│
└── zotero-bridge\                   ★ DSH 侧收件箱（独立插件）
    └── bundle\                      package.json + cordis.patch.yml + index.js

~\.dsh\                              平台管理
├── profiles\desktop\                插件与 profile 配置
├── skills\                          技能（从 <项目目录的上级>\...\skills 同步过来）
└── zotero-bridge\                   文件信箱（requests / results / done）
```

### 插件为什么要"源码多文件 + 生成单文件"

Zotero 的 bootstrapped extension **只加载 xpi 根目录下的 `bootstrap.js` 一个
文件**（`manifest.json` 也必须在根目录）。所以源码分成 20 个文件之后，运行时
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
| 1 | 装 Python 3.14 + 建 venv，装 `mcp pymupdf numpy fastembed zstandard` | `python tools\gui.py --check` |
| 2 | 装 Zotero（建议 10+），设置数据目录；装 Ollama 并拉 `qwen3:4b-instruct` | `tools\zotero_upgrade.py check` |
| 3 | 跑 `1-convert.cmd` 建库 | `3-maintain.cmd` 显示条目/切片/向量 |
| 4 | 用 bundle 接入 DSH：`dsh plugin --profile <p> add "link:<...>/bundle"` | DSH 里出现 `mcp__zotero-kb__*` 工具 |
| 5 | 装 Zotero 插件（**必须完全重启 Zotero**） | 文献列表出现「知识库权重」列 |
| 6 | 跑 `4-service.vbs` 起本地服务 | `http://127.0.0.1:8765/health` |
| 7 | 跑 `tools\test_task_bridge.py` 等测试 | 各测试退出码 0 |
| 8 | （可选）装 DSH 侧收件箱 bundle | 往信箱丢个 json，看 results 出结果 |

**易错点（本机都踩过）**：
- DSH 的 patch 层是**覆盖**语义，新增条目必须写在 `- insert:` 里
- 插件 manifest **必须**有 `update_url`（否则装不上，报错却只说"可能不兼容"）
- Zotero 插件更新后**必须完全重启 Zotero**（不会重跑 `startup`）
- 读 Zotero 库**必须**连 `-wal`/`-shm`

---

## 七、这个系统的边界（明确不做什么）

说清楚边界和说清楚能力一样重要：

| 不做 | 原因 |
|---|---|
| 不写 `zotero.sqlite` | Zotero 没开放写接口，直写会损坏库；写操作一律走 Zotero 自己的 API |
| 不自动下载 PDF | 插件只处理 Zotero 里**已有**的附件 |
| 不替你做分类决策 | 模型只给**建议**，要不要应用由你点 |
| 不让模型读整篇正文 | 先搜后读，按页取；否则上下文会爆 |
| 不做云端同步 | 全部本地（Zotero 自己的同步不受影响） |

---

## 八、当前状态（2026-10-05 实测）

> 数字会变，**要看最新值请跑 `3-maintain.cmd stats` 或在 AI 里调 `kb_stats`**；
> 这一节的作用是让读者对量级有个概念。

| 部件 | 状态 | 证据 |
|---|---|---|
| 知识库 | ✅ 95 条目 / 3657 切片 / 3657 向量 / 经验 11 条 / 分级视图 275 份 | `3-maintain.cmd` |
| MCP 接入 | ✅ **13 工具 + 4 资源** | AI 里 `kb_search` 等工具可直接调用 |
| 混合检索 | ✅ 中文短语召回 + 向量 + RRF + 权重上浮 | 关键词与语义两路实测均可用 |
| 分类重整 | ✅ 6 个顶层分类，中英重复已消除 | `zotero_sync.py verify` |
| Zotero 插件 | ✅ 自动切片 + 分类建议 + 权重列 + 抓取 + **打开知识库（分级）** | 插件验证 30/30；实测分类置信度 0.98 |
| 本地服务 | ✅ 80 项自检 | `localserver.py --test` |
| 测试 | ✅ 18 个测试文件全过（0 失败）+ 本地服务 80 项自检。⚠ 断言**条数不固定**：`test_matching.py` 抽样真实库，项数会浮动 | `tests\test_*.py` |
| DSH 侧收件箱 | ✅ 文件信箱版可用 | 往信箱丢 json → DSH 对话里出现 |
| **获取文献（需求 9）** | ✅ **真机多轮验收** | 见链路 5；本地模型分类打标签实测可用 |
| **打开知识库（分级）** | ✅ 面板与 Zotero 右键两处都能用（2026-10-05） | 见 B7；分级视图 275 份已生成 |
| **代码模块化** | ✅ 插件 20 个源文件（窗格那两个已删）/ 面板 10 个页签 + 2 个对话框模块 | 两处都用"逐字比对"证明是纯搬迁 |
| ~~内容窗格「本地模型」~~ | **已删除**（2026-10-05）：没用而且 bug 多。见 B10；`check_plugin.py` 有反向检查盯着它别回来 | — |
| ~~逐段检查（可中断可续跑）~~ | **已删除**（连三张表一起）。见 B10 | — |
| **提示词注册表** | ✅ 6 条话术可看可改可还原 + 试跑（chat/para/propose 随窗格删了） | 见 B12；`tab_prompts.py` |
| **经验库可编辑** | ✅ 面板「修改/增添经验」（口述 + 表单两条路）+ 体检 + 手动选会话 | 见 B13；`test_experience_edit.py` 35 项 |

**13 个 MCP 工具**：`kb_search` / `kb_item` / `kb_fulltext` / `kb_figures` /
`kb_experience_add` / `kb_experience_query` / `kb_weight_set` / `kb_reindex` /
`kb_stats` / `kb_paper_info`（候选文献的摘要/关键词/OA/被引）/
`kb_acquire`（把 DOI 交给 Zotero 抓）/ `kb_acquire_progress`（抓取进度）/
`kb_attach`（把本地 PDF 挂到条目上，兜底用）。

**4 个资源**：`zotero-kb://collections`（分类清单）、
`zotero-kb://item/tldr/{key}`（摘要级要点）、`zotero-kb://item/{key}`（完整档案）、
`zotero-kb://item/full/{key}`（带页码锚点的全文，按需读）。

---

## 九、下一步（按价值排序）

1. ~~**公式 → LaTeX**~~ —— **已实现**：全库正文改由 MinerU 解析后，公式就是 LaTeX
   （`$$…$$` 进正文，可检索；右侧栏预览再转成 MathML 显示）。见 B11、B12。
2. **同一篇里按"摘要深度"再分层** —— 现在的六个级别是按**产出物**分的
   （摘要 / 纲要 / 档案 / 正文 / 图表 / 权重）；长学位论文还需要"逐段 → 逐节 → 全篇"
   这种按**深度**的分级
3. **多级摘要树 + 知识卡片连线图** —— 跨文献的联系（哪几篇在讲同一件事、
   谁的结论被谁引用）
4. **每条知识可回溯原文的来源审查** —— 模型引用某条知识时能指到具体页码
5. **经验回流的"手动选会话"跑顺之后**，再考虑"会话结束时自动挖"
   （本轮已把默认行为从"扫全部"改成"手动勾选"，并把口径收紧到"只记与文献有关的"）

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
**知道哪条路不通，本身就是在省时间。**