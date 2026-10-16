# Zotero 文献知识库插件

把新加入的文献**自动送进本地知识库**（切片 + 索引 + 向量），用**本地模型或外部
API** 推荐分类与标签、补全元数据、生成分节纲要，并在 Zotero 里提供「知识库」侧栏、
阅读器对照列、公式就地纠错与管理面板。

> 插件声明兼容 `7.0 ~ 11.*`；Zotero 9 / 10 都装得上。

- **装来用**：见仓库根目录的 [`../INSTALL.md`](../INSTALL.md)
- **架构与设计**：见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- **还能做什么 / 文档索引**：见 [`../README.md`](../README.md)

---

## 源码怎么组织（改代码前先看这段）

```
zotero-plugin/src/00-core.js … 99-bootstrap.js    ← 源码，改这里
        │  python tools/build_bootstrap.py
        ▼
zotero-plugin/bootstrap.js                        ← 生成物，xpi 里装的是它
```

Zotero 的 bootstrapped extension **只加载 xpi 根目录下的 `bootstrap.js` 一个
文件**（`manifest.json` 也必须在根目录），所以源码分成 26 个文件之后，运行时
仍然必须是一个文件。每个源文件用 `Object.assign(ZoteroKB, { … })` 把成员挂到
插件对象上 —— 好处是**每个文件单独也是合法 JS**，能逐个做语法检查。

| 源文件 | 装什么 |
|---|---|
| `00-core.js` | 插件对象与 `startup`（运行时状态字段） |
| `01-lifecycle.js` | `shutdown` / `install` / `uninstall` |
| `02-paths.js` | 四个位置的解析（项目目录 / Python / Ollama / 知识库） |
| `03-processes.js` | 按需拉起引擎子进程；旧 HTTP 通道的回退与起停 |
| `04-prefs.js` `05-server.js` | 首选项；与引擎/旧服务的通信封装 |
| `06-watch.js` `07-classify.js` | 新条目监听；分类建议 |
| `08-metafill.js` `09-metabatch.js` | 元数据补全；批量补全 / 类型标题修正 |
| `10-acquire.js` `11-notify.js` `12-dsh.js` | 获取文献（从 DSH 导入）；提示窗；发到 DSH |
| `13-kbopen.js` | **打开知识库（分级）** —— 级别清单与打开文件 |
| `14-menus.js` | 工具栏按钮与条目右键菜单 |
| `15-prefpane.js` `16-weightcol.js` | 设置面板；「知识库权重」列 |
| `17-windowhook.js` `18-taskpoll.js` | 窗口钩子；任务轮询 / 文件信箱 |
| `19-caps.js` `19-mineruguide.js` | 能力探测；可选组件（MinerU / Ollama）首启引导 |
| `20-kbview.js` | 「知识库」侧栏自建列 + 阅读器对照列（页面在 `content/kbview.*`） |
| `21-kbcols.js` | 「已建知识库」「分节纲要」两列 |
| `22-formula.js` | 公式就地纠错的右键菜单与转发 |
| `23-admin.js` | **管理面板宿主半**：窗口 / 桥 / 路由表 / 引擎生命周期（页面在 `content/admin*`） |
| `99-bootstrap.js` | Zotero 要调用的顶层生命周期函数 |

界面文案在 `locale/{zh-CN,en-US}/zotero-kb.ftl`（**两个语言都要有**）。
`registerSection` 的 `header` / `sidenav.l10nID` 是**必填**项，指向的 id 不存在时
表现为"分区标题空白、没有任何报错"，所以 `tools/check_plugin.py` 会静态检查
"`bootstrap.js` 里用到的每个 l10nID 都在 ftl 里有定义"，并断言 `locale/**` 真的进了 xpi。

插件里"字段名写错 / 名字写错"在 Zotero 里**没有任何报错**，只表现为"按钮点了没反应"。
`tools/verify_bootstrap.js`（Node 的 vm + 桩 `Zotero` 跑真机 `bootstrap.js`）
专门盯这类问题，已接进 `check_plugin.py`，不需要重启 Zotero。

## 改完必须重新生成

**改了 `src/*.js` 就要跑 `python tools/build_bootstrap.py`**，否则 xpi 里装的还是
旧代码（插件表现像没改，而 Zotero 的报错没有任何线索）。忘了也不会出事：
`tools/pack_plugin.py` 打包前会校验并**拒绝出包**，CI 里也有一条会红的检查。

改 `content/**` 的页面脚本要跑 `tools/check_js_syntax.py`；`content/vendor/` 里
**只放原样引入的第三方库**（markdown-it / KaTeX），不要把自己的代码放进去。

## 打包与安装

```
python tools\check_plugin.py     # 静态检查 + 打包，产物在 zotero-plugin\ 下
```

安装步骤（含"必须完全退出 Zotero 再打开"）见 [`../INSTALL.md`](../INSTALL.md)。
往 profile 的 `extensions\` 目录丢 xpi 是无效的，必须走 Zotero 的
「工具 → 插件 → 齿轮 → Install Plugin From File…」。
