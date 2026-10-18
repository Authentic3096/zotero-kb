# 快速开始（普通用户）

一页装好、一页升级。完整说明见 [INSTALL.md](INSTALL.md)。

> 需要：Windows 10/11、Zotero 7 或更新、能上网（只在安装/升级时）。
> 不需要：Python、git、API Key。

## 安装（5 步）

> 顺序：**先装插件 → 再装环境 → 在面板里建库**。装环境时不用跑 `1-convert.cmd`。

**1. 下载并解压**

到 [Releases](https://github.com/Authentic3096/zotero-kb/releases) 下载最新版的
`zotero-kb-<版本>-win64.zip`，解压到**路径里没有中文和空格**的目录，例如
`C:\zotero-kb`。

包里已经有运行代码、脚本、两份 Skill 和插件 xpi，**不用再下源码**。

**2. 先装 Zotero 插件**

Zotero → 工具 → 插件 → 右上角齿轮 → **Install Plugin From File…** →
选解压目录里的 `zotero-plugin\zotero-kb-<版本>.xpi` →
**完全退出 Zotero 再打开**（关窗口不算）。

重新打开后，工具栏会多一个图标；点它，或走
**工具 →「知识库管理…」**，能打开面板就说明插件好了。**这时还没有建库**。

**3. 装 Python 环境（只装前置依赖）**

```powershell
cd C:\zotero-kb
.\scripts\install-env.cmd
```

本机没 Python 也行，脚本会自己下载。它会建 `.venv`、装依赖、下嵌入模型；
**只往项目目录里写东西**，不改系统环境。**它不建库，也不用先跑 1-convert。**

**4. 首次建库（在面板里）**

Zotero → 工具 →「知识库管理…」→ 左侧 **「高级」** →
**「索引重建」→「全部重建」**。

等底部「运行日志」跑完，回到「知识库结构」页，能看到
**文献 / 切片 / 向量** 的数字就建好了。首次会给每篇算向量，耗时几分钟。

**5. 可选组件（不装也能用）**

管理面板 →「偏好设置」→「可选组件安装引导」：

- **Ollama**：分类建议、元数据补全、分节纲要、摘要与要点；
- **MinerU**：更干净的正文、公式 LaTeX、表格与扫描件；装完后在
  「PDF 解析 → 全库重解析」切换。

两个都不装时，检索、经验、权重、按页读正文照常可用。

> `PDF 解析 → 全库重解析` 是 **MinerU 可选解析**，不是首次建库的必需步骤；
> 首次建库用上面的 **高级 → 全部重建**。

> **挪动过目录？** 在 Zotero「知识库管理 → 运行环境」页把 **Python 解释器**
> 指到 `<新目录>\.venv\Scripts\python.exe`，点「保存并重新检测」。
> 路径不是写死的，不用改代码。知识库数据默认在 Zotero 数据目录下，不受影响。

### 可选：命令行建库 / 增量

主流程不要求命令行；想用脚本时：

```powershell
.\scripts\1-convert.cmd            # 增量
.\scripts\1-convert.cmd --full     # 全部重建（等价于面板「全部重建」）
```

首次仍推荐面板「高级 → 全部重建」，进度和失败原因都在运行日志里。

## 升级（3 步）

**1. 下载新便携包**：Release 里的 `zotero-kb-<新版本>-win64.zip`，解压到**新的**
文件夹（不要直接覆盖正在用的目录）。

**2. 运行新包里的更新脚本**，按提示输入旧目录：

```powershell
cd C:\zotero-kb-新版本
.\scripts\update.cmd
```

例如旧目录是 `C:\zotero-kb`，输入它并回车。脚本只覆盖程序文件
（`offline` / `online` / `tools` / `scripts` / `skills` 与文档），
**保留** `.venv`、知识库数据（`index.db` / `edits` / `trash`）和 Zotero 设置；
`requirements.txt` 变了才会重装依赖。

**3. 装新 xpi 并重启 Zotero**：用新包里的
`zotero-plugin\zotero-kb-<新版本>.xpi` 覆盖安装，然后完全退出 Zotero 再打开。

> 升级后打开管理面板 →「知识库结构」页确认数字正常；
> 新文献用面板「高级」页的「增量更新」即可。

## 接 AI（可选，两件事分开看）

### MCP 工具：让 AI 能调用检索

知识库自带一个 **stdio MCP 服务器**：`online\server.py`。
在你的 MCP 客户端里新增一个 stdio 服务器：

| 配置项 | 填什么 |
|---|---|
| 命令 | `<项目目录>\.venv\Scripts\python.exe` |
| 参数 | `-X utf8 <项目目录>\online\server.py` |
| 工作目录 | `<项目目录>`（建议填） |
| 环境变量 | `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` |

它提供 `kb_search` / `kb_item` / `kb_fulltext` / `kb_experience_add` /
`kb_weight_set` / `kb_reindex` / `kb_stats` 等工具。
不同客户端（DSH、Claude Desktop、其他支持 MCP 的 agent）的配置写法看各自文档；
本项目的 MCP 服务器本身与具体客户端无关。

### Skill：告诉 AI 什么时候用这些工具

Skill 不是工具，是一份使用说明。把对应目录复制到 agent 的 skill 目录：

```powershell
# 以 DSH 为例（其他 agent 看各自文档里的 skill / instructions 目录）
Copy-Item -Recurse -Force ".\skills\zotero-kb"     "$env:USERPROFILE\.dsh\skills\zotero-kb"
Copy-Item -Recurse -Force ".\skills\zotero-acquire" "$env:USERPROFILE\.dsh\skills\zotero-acquire"
```

| Skill | 作用 |
|---|---|
| `zotero-kb` | 怎么用知识库：先搜后读、按页取正文、用完记经验 |
| `zotero-acquire` | 怎么找新文献：搜 → 核 DOI → 列候选 → 交给 Zotero 抓 |

复制后**新开一个会话**才生效。改了 `SKILL.md` 要重新复制一次，或运行
`.\scripts\sync-skill.ps1`（它默认同步到 DSH 的用户级 skills 目录）。

## 出问题先看哪里

1. 管理面板底部的「运行日志」；
2. Zotero「知识库管理 → 运行环境」页，看项目目录 / Python / 知识库位置是否都对；
3. `<知识库>\plugin-status.json`（启动各步成败）。
4. 还不行再看 [INSTALL.md](INSTALL.md) 的「常见问题」与 `.env` 救急层。
