# 知识库插件 2.0.0（架构换代）

> 这一版换的是架构：管理面板从"另开一个 Python/Tk 程序"改成**住在 Zotero 里**，
> 同时把常驻的本地 HTTP 服务（`127.0.0.1:8765`）整个下掉。业务代码没有重写，
> 变的是界面跑在哪、插件与知识库之间怎么通信。
>
> 安装：Zotero → 工具 → 插件 → 齿轮 → **Install Plugin From File…** → 选本页的
> `zotero-kb-2.0.0.xpi`。往 profile 的 `extensions\` 丢 xpi **无效**，必须走这个入口。

## 这一版换掉了什么

| | 2.0 之前 | 2.0 之后 |
|---|---|---|
| 管理面板 | 独立的 Python 程序（`tools\gui.py` + Tkinter，八个页签），从 Zotero 里"拉起来" | **Zotero 自己的窗口**（工具菜单 →「知识库管理…」），**10 页**：知识库结构 / 经验库 / 分类建议 / 分节纲要 / 运行环境 / PDF 解析 / 损坏查询 / 元数据 / 高级 / 提示词 |
| 面板与知识库之间 | 常驻 HTTP 服务 `127.0.0.1:8765`（带 token） | **插件按需拉起的子进程**，父子进程 stdin/stdout 上的 JSON-RPC（NDJSON）。**没有监听端口、没有 token、不常驻、不自启**，随 Zotero 退出 |
| 跨程序派活（如"让 Zotero 抓一篇文献"） | HTTP 队列 | **文件信箱** `<知识库>\mailbox\{requests,claimed,results}`（HTTP 那条留着、默认关，开关 `zotero-kb.taskPollHttp`） |
| DSH 侧取数 | HTTP `/item-info` `/weights` `/col-status` | **直读 `index.db`**（Node 24 的 `node:sqlite`，**readOnly**） |
| 界面 | Tkinter/ttk | 无框架的纯 HTML/CSS/JS（`resource://zotero-kb/content/admin.html`），令牌与旧面板同一套语义 |

## 面板 UI（这一版统一调整过）

- **吸顶**：页里那条操作行与表头 `position: sticky` 都贴在滚动容器顶边（`top` 要用
  `calc(-1 * var(--sp-l))` 抵消内容盒 padding，否则顶上会透出一条缝）。
- **表头排序**：能排的只有**有明确大小关系**的列（key / 标题 / 年份 / 得分 / 权重 / 重点）；
  **自由长文本不给排序**（"命中片段""备注"）。客户端分页的表，点表头先对**全量**排、回到第 1 页重画
  （只排当前页会看着像"排错了"）。
- **分类多选筛选**：经验库「选择关联文献」与分类建议页共用同一个共享零件
  `KBAdmin.multiSelect`，一次可选一个或几个分类。
- 切页不摘挂 DOM、只切 `hidden`，并用 promise 去重（避免"像又读了一遍"和重复取数）。
- Zotero 的 `<browser>` 里**原生 `<select>` 的弹层打不开**，所以页面里一律不用它：
  ≤4 个选项用并排按钮组，长列表用自绘下拉。

## 顺带的清理（不改变行为）

- **死代码**：一批"只服务过一次迁移/补丁/实验"的脚本、旧面板专用的截图工具、
  已删模块残留的 `.pyc` 与 `.pytest_cache`。
- **重复实现合并**：`now_iso`（原先 5 处各写一遍）、`dir_size`（3 处）、`human_size`（3 处）、
  `table_columns`（2 处）收进 `offline\schemas.py` 的公开小工具。**唯一的可观察差异**：
  索引文件大小在 ≥1 GB 时显示成 `2.00 GB`（原来 `2048.0 MB`），MB 统一保留一位小数。

## 已知限制

- **`node:sqlite` 直读是只读的**：DSH 侧只能读 `items` 等表的原始值。**`weight` 这类算出来的字段
  直读不出来**（那是引擎里算的），要显示必须走引擎的 `advanced.weightList`，**别在 JS 里复刻一遍**。
- **有几条"成功路径"没在真机上验过**（只验过出错路径，没在真库上写过真实数据）：
  分类建议 `classify.item`、阅读器侧栏 `doc.save`、运行环境 `env.save` 的**写入成功**分支；
  `healthCheck` 迁移后只做过只读对照。
- **有两处交互没在真机验过**：经验库编辑弹窗里再嵌一层 `<dialog>`；`<select>` 替代控件
  （并排按钮组 / 自绘下拉）的实际展开。
- 与旧 HTTP 通道相比，**少了"从别的程序随时 curl 一下"这条路**（这正是下掉 8765 的目的）。
  要临时用它就按下面的回退路径起回来。
- MinerU 相关的全量测试**没有在本版跑**（会真拉起 MinerU 占满机器）。

## 回退路径

1. **想把 8765 服务起回来**（插件与 DSH 侧本来就不依赖它，所以**不起也不影响功能**）：
   双击 `scripts\4-service.vbs`（或 `python online\localserver.py`）；
   想让插件自动拉起就再跑一次 `Zotero.Prefs.set("zotero-kb.autoStartService", true)`
   （**短名，别写全名**）。
2. 别点 `scripts\install-autostart.ps1` / `autostart-service.vbs` —— 那是**登录自启**入口，
   会把"服务已停"这个状态破坏掉。
3. 装回旧版插件：用 GitHub Release 上的 `zotero-kb-1.12.0.xpi`，
   在 Zotero 里直接覆盖安装即可；`updates.json` 指向的是最新版，回退后不会自动"升"回来。

## 没有变的地方

只读 Zotero、绝不写回 `zotero.sqlite`；正文修订层是唯一的正文写路径；
知识库目录里的 `edits\` 是用户资产、绝不能删；改 `zotero-plugin\src\*.js` 之后必须跑
`python tools\build_bootstrap.py`。

## 完整对比

<https://github.com/Authentic3096/zotero-kb/compare/v1.12.0...v2.0.0>
