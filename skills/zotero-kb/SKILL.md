---
name: zotero-kb
description: 检索和阅读本机 Zotero 文献知识库，并记录使用经验。当用户提到"我的文献""Zotero 里的论文""查一下我库里关于…""某篇文献的方法"时使用。
---

# Zotero 文献知识库

本机有一个 Zotero 文献知识库（含正文、高亮标注、我的笔记与使用经验），
通过 MCP 提供工具与资源。**不要用文件工具去翻 Zotero 数据目录**，
也不要用 Read 工具读 PDF —— 走下面的工具，它们快得多，而且带页码与经验权重。

> **位置不用记**：知识库目录、条目数量这些会随配置和导入变化。
> 需要时调 `kb_stats` 拿权威数字，其余工具返回的路径都是当前有效的。
> 本文件里出现的 `<知识库>` / `<项目目录>` 都是占位符。
> 如果 MCP 工具不可用（没接 DSH 的场景），先看
> `%APPDATA%\zotero-kb\location.json` 与 `runtime.json` 里的实际路径。

## 工具与资源

| 名称 | 用途 |
|---|---|
| `mcp__zotero-kb__kb_search` | 检索。混合关键词+语义，并按经验权重排序，同时返回相关历史经验 |
| `mcp__zotero-kb__kb_figures` | **检索图注与表格**（"哪篇里有画…的图"「谁做过…的对比表」用这个） |
| `mcp__zotero-kb__kb_item` | 一篇文献的完整档案（元数据、摘要、笔记、标注、经验、**图表清单**） |
| `mcp__zotero-kb__kb_fulltext` | 读正文，支持 `page_from`/`page_to` 分段 |
| `mcp__zotero-kb__kb_experience_add` | **记录一次尝试的结果**（有效/无效/部分/未验证） |
| `mcp__zotero-kb__kb_experience_query` | 查历史经验 |
| `mcp__zotero-kb__kb_weight_set` | 标重点、加减分 |
| `mcp__zotero-kb__kb_reindex` | 刚导入的新文献立刻补抽 |
| `mcp__zotero-kb__kb_stats` | 库状态 |
| 资源 `zotero-kb://item/tldr/{key}` | 摘要级要点（几百 token，用来判断要不要深读） |
| 资源 `zotero-kb://item/{key}` | 完整档案 Markdown |
| 资源 `zotero-kb://item/full/{key}` | 全文（篇幅大，慎用） |
| 资源 `zotero-kb://collections` | 分类清单 |

**同一份内容在磁盘上也是文件**（给了 key 就能直接用 read 工具读，
不必绕 MCP）—— 这在"用户要自己打开某一篇的某一层"时最方便，
面板的「打开知识库…」和 Zotero 的右键「打开知识库」读的就是同一批路径：

| 层面 | 文件（相对知识库目录） |
|---|---|
| 摘要与要点 | `views\<key>.tldr.md` |
| 完整档案 | `papers\<key>.md` |
| 按页正文 | `fulltext\<key>.md` |
| 图注与表格 | `views\<key>.figures.md`（这篇没有图表时不存在） |
| 权重与经验 | `views\<key>.weight.md` |

## 使用纪律

1. **先搜后读**：任何与文献相关的问题，第一步都是 `kb_search`。
   （想了解库里都有哪些主题，用 `zotero-kb://collections` 看分类，
   或 `kb_stats` 看规模 —— 不要凭这个文件里的举例去猜。）
2. **按需深读**：`kb_search` 结果里的 `snippets` 往往已经够回答问题。
   需要更多时先读 `zotero-kb://item/tldr/{key}`，**只有需要方法细节、公式、
   实验参数时才** `kb_fulltext`，并且尽量用 `page_from/page_to` 分段取。
   不要把整篇正文一次性拉进上下文。
2b. **图表用 `kb_figures` 找、用 `kb_fulltext` 看**。
   知识库**不识别图的内容**（那是视觉模型的事，本机没做），
   但**图注与表格是有的**：
   - 想找"哪篇里有…的图"→ `kb_figures(query="神经网络模型")`
   - 某个图具体画了什么 → 先 `kb_figures` 拿到页码，
     再 `kb_fulltext(key, page_from=那页, page_to=那页)`
     —— 图注与表格 Markdown 就渲染在那一页的**末尾**。
   表格是**已经转成 Markdown 的**，直接读得到数值，不用再解析 PDF。
3. **引用要落位**：回答里引用文献时给出标题、年份与 `key`；
   引用具体内容时带页码（片段里的 `page` 字段）。
4. **每次尝试都要留痕**（最重要的一条）。当满足下列任一条件时，
   调用 `kb_experience_add` 记录：
   - 用某篇文献的方法做了实验/计算，有了结果（不管成败）；
   - 明确判断某篇文献在某类问题上可用/不可用；
   - 发现某条路线走不通及其原因。

   ⚠ **只记与文献内容或研究方法有关的东西。**
   工程/工具链/配置/安装/打包/调试这类「关于知识库自己怎么搭」的踩坑，
   **写进工作档案（`project/<工作名>/README.md`）或技能文档，不要进经验库** ——
   经验库是**检索加权**的输入，混进去会让排序变脏。
   用户明确抱怨过这一点，原话：「让他提经验是文献相关的啊，
   我现在经验库的最后两条就是无关的」——那两条（改 BabelDOC 的 IL 回灌、
   文本源选型扫描）连一篇关联文献都没有，留在库里只会误导。
   判据很好用：**这条经验能不能关联到至少一篇文献？** 不能就别记。

   字段填法：
   - `asked`：当时要解决什么问题（写具体，以后靠它检索）
   - `outcome`：`effective` 有效 / `ineffective` 无效 / `partial` 部分有效 / `unknown` 未验证
   - `method`：具体用了什么方法
   - `item_keys`：涉及的文献 key（逗号分隔）
   - `reason`：**为什么**有效或失败（这是最有价值的部分）
   - `context`：前提条件（采样率、初值、数据集、硬件…）
   记录之后，下次 `kb_search` 会自动把相关经验一起返回，
   验证有效的文献排序也会上移 —— 这是知识库越用越准的机制。
5. **标重点**：用户说"这篇重要""以后优先看它"时，用 `kb_weight_set` 传 `pinned=true`。
6. **失败也是知识**：不要只记成功。`ineffective` 同样会加分（只是不如有效多），
   因为"这条路走不通"能避免以后重复踩坑。

## 边界

- 知识库是 Zotero 的**只读快照**。不要试图写回 Zotero，也不要直接改
  `zotero.sqlite`（Zotero 的本地 API 是只读的）。
- AI 产出的分析、总结、笔记写到 `<知识库>\notes\`（或会话目录），
  并在回答里说明落在哪了。`<知识库>` 用 `kb_stats` 或 `kb_item` 返回的
  路径，不要凭记忆写。
- 知识库不是实时同步的。如果用户说"我刚导入了一篇"，先 `kb_reindex`
  （带 `scope=changed`），再检索。
- 检索不到时的排查顺序：换同义词（中英各试一次）→ 去掉分类/年份限制 →
  `kb_stats` 确认库状态 → `kb_reindex` 补抽。
