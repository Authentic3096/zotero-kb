# 开发与维护

> 给**改这个项目的人**看的：目录结构、当前进度、发版流程、排错、后续计划。
> 想装来用 → [`../INSTALL.md`](../INSTALL.md)；
> 想理解设计与用法 → [`DESIGN.md`](DESIGN.md)。

---
## 当前进度

> **这一节的用途**：让**新开的会话**知道做到哪了，不重复讨论已经定下的事。
> 详细的逐项方案、架构图、分阶段路线图与风险清单属**内部开发文档**，未随仓库发布。

**已完成**

| 项 | 说明 |
|---|---|
| **基础权重按发表年份** | 跨度 **15 年**、**动态**（不落库，每次检索实时算，跨年自动更新）、15 年前压到 `0.95`。此前 101 篇里有 **93 篇**权重被硬编码成 `1.0`。见 `offline/schemas.py` 的 `recency_base()`，以及 `raw_weight()` / `weight_multiplier()` 新增的 `base` 参数 |
| **搜文献 → 列候选表 → 用户挑 → 抓进 Zotero**（v0.24.0） | 见 [`DESIGN.md` 的「获取文献」一节](DESIGN.md#获取文献搜--列候选可点开看--勾选--抓进-zotero)。**抓取交给 Zotero 自己**（`Zotero.Translate.Search` + 自带的附件 resolver），所以**校园网/机构订阅的访问权限天然生效**，而且**不用下载再手工拖** |
| **打开知识库（分级）** | 一篇文献的六个层面（摘要与要点 / 分节纲要 / 完整档案 / 按页正文 / 图注与表格 / 权重与经验）变成**数据模型**（`offline/kbviews.py` 的 `LEVELS`），面板与 Zotero 右键共用同一份清单（`tools/check_kb_levels.py` 盯两侧一致）。解决的是"目录里是 `22X9PMR6.md`、人认不出是哪篇"。见 [`../ARCHITECTURE.md` 的 B7](../ARCHITECTURE.md) |
| **模块化重构** | 插件 4951 行单文件 → `zotero-plugin/src/*.js` 源文件 + 生成器（运行时仍是单文件，见下）；面板 2805 行单类 → `tools/panels/` 各页签模块。两处都用"逐字比对"证明了是纯搬迁 |
| **内容窗格「本地模型」+ 逐段检测** | 加过：右侧内容窗格一个分区（注入摘要级/全文级、逐段检测、定位、整理讨论）+ 逐段检查按段落指纹落库。用户当天判定"没什么用而且 bug 多"，要求**整条删除**（窗格、七个端点、kbchat/paras、面板复核页、三张表一起）—— 见 [`../ARCHITECTURE.md` 的 B10](../ARCHITECTURE.md) 与 `check_plugin.py` 的反向检查 |
| **经验层写入收成一份实现 + 提示词注册表** | `offline/experience.py`（增删改与权重算术只有一份）+ `offline/prompts.py` 注册表（可改、可试跑；现在 6 条）。见 [`../ARCHITECTURE.md` 的 B12–B13](../ARCHITECTURE.md) |
| **MinerU 接进建库流程 + 全库重解析 + 层级设计** | `--parser` 那套 + 面板「PDF 解析」页 + 批量解析（模型只加载一次）+ 全库 94/94 换成 MinerU；另写 `docs/知识库层级设计.md` 定五层 |
| **MinerU 可选组件：项目内安装 + 探测 + 图形化引导** | 独立 venv（`mineru[torch]` + CUDA 版 torch + llama.cpp 跑 VLM），装在项目内 `.mineru\`、删目录即卸载；`offline/mineru.py::probe()` 探测 + `/mineru-check`；插件首启弹一次引导 + 面板向导（预检磁盘/显卡/网络、实时日志、可取消）。**接进转换管道是下一轮**。见 [`../ARCHITECTURE.md` 的 B11](../ARCHITECTURE.md) |

**进行中**

| 项 | 说明 |
|---|---|
| PDF 解析路线对照实验 | BabelDOC IL vs 现有 PyMuPDF，3 类样本（双栏带公式 / 含表格 / 扫描件）；外加"改 IL 后能否渲染回 PDF"的验证 |

**已定方向、尚未实现**（细节见实施方案）

- ~~知识层在"摘要级"和"全文级"之间再加一级~~ → **分级视图已落地**。但那是按**产出物**分的五层；"同一篇里按**摘要深度**再分层
  （逐段 → 逐节 → 全篇）"还没做
- **多级摘要树** + **知识卡片连线图**（跨文献联系）
- 每条知识可**回溯原文**的来源审查
- 与 pdf2zh 的联动（复用其版面分析；"本地模型修复后回灌翻译"待验证）

---

## 发布与提交约定

### ① 打包前跑一遍综合审计

```powershell
.venv\Scripts\python.exe tools\audit_release.py
```

查三类：**隐私泄露**（API key / token / 用户名）、**写死的绝对路径**
（区分"必须可移植的文件"和"文档里的示例"）、**安装可用性**
（脚本存在性、镜像配置、打包排除列表）。发版前必须全过。

### ② 有些文件是"按本机生成"的，不能提交

| 文件 | 为什么不能提交 | 怎么生成 |
|---|---|---|
| `bundle/cordis.patch.yml` | DSH 要求 `command`/`args` 是**绝对路径** | `python tools\gen_bundle_config.py`（`install-env.cmd` 会自动跑） |
| `kb-location.json` | 记着本机的项目/解释器位置 | 插件设置面板写 |
| `kb/llm-config.json` | **含 API key** | 插件设置面板写 |
| `kb/*-token.txt` | 本机通行证 | 自动生成 |

`bundle/` 里提交的是 `cordis.patch.yml.tmpl`（带 `__PROJECT_PYTHON__`
占位符的模板）。

### ③ 三个位置互相独立，谁也别推谁

**知识库（数据）/ 项目目录（代码）/ Python / Ollama** 是四个独立的
位置，各按"用户指定 > 环境变量 > 配置 > 自动探测 > 兜底"解析。

⚠ 这里踩过两次同类坑，都写进了 `skills/zotero-plugin-dev/SKILL.md`：
· 从知识库位置反推项目根 → 知识库一搬家就报"找不到 Python 环境"；
· 无条件信任配置里的 `project_root` → 代码被复制到新目录后，
  仍然用**旧目录**的解释器（用 `os.path.samefile` 认项目身份才拦住）。

### ④ 发版：改个版本号、打个 tag，剩下的自动做
**版本号怎么定**（约定）：

| 改动 | 动哪一位 | 发布说明 |
|---|---|---|
| 小改动：改文案、改署名、调面板措辞、修个小 bug —— **不动功能** | **只动最后一位**（0.25.1 → 0.25.2） | 一句话就够，写在 tag 附注里 |
| 加功能、行为有变化、用户需要知道怎么用 | 动中间位（0.25.x → 0.26.0） | 写清"多了什么、怎么用" |
| **架构换代 / 里程碑**（代码结构大改，但配置与数据格式不变） | 动第一位（0.25.2 → 1.0.0，2026-10-05 那次就是） | 说清"为什么这次是大版本"，并明确**升级不需要额外动作**（否则用户会以为要迁移） |
| 破坏性变更（配置格式、数据迁移） | 动第一位 | 必须写清怎么迁移 |

小改动的说明**不必**编排成正式文档 —— tag 附注写一行，发布链会拿它当说明：

```bash
# 0. 改过插件源码（zotero-plugin/src/*.js）就先重新生成，否则打包会被拒绝
python tools/build_bootstrap.py
# 1. 改 zotero-plugin/manifest.json 里的 version（小改动只动最后一位）
# 2. 同步更新清单，和上一步**同一次提交**（否则 CI 的 --check 会红）
python tools/make_updates_json.py --tag v0.25.2
git commit -am "统一作者署名" && git push origin main
# 3. 打**带附注**的 tag 推上去 —— 附注就是发布说明
git tag -a v0.25.2 -m "统一作者署名为 DeepSeek and Authentic3096" && git push origin v0.25.2
```

第 2 步不能省。`updates.json` 本该在发版后才更新，但 CI 有一条「它与 manifest
版本必须一致」的守门员 —— 只改了版本号就推，CI 会红。两者一起提交最省事；
发布链里也保留了「发现不一致就自动改并提交回 main」作为兜底。

**还想改已经发出去的说明**：tag 附注只在**新建** Release 时被采用；Release 已存在时
工作流只覆盖附件、不动正文。所以正文要单独改一次：

```powershell
python tools\set_release_body.py --tag v0.25.2 --body 说明.md   # 改正文
python tools\set_release_body.py --tag v0.25.2 --show           # 只看当前正文开头
```

脚本用本机凭据管理器里的 `gh:github.com:<用户名>`（以前用 gh CLI 留下的通用凭据），
运行时读取、不落盘、不打印；取不到就退回 `GITHUB_TOKEN` 环境变量。不需要装 gh CLI。

`.github/workflows/release.yml` 会依次：**跑自检 → 打包 xpi → 审计 →
生成 updates.json → 发 Release（xpi 作附件）→ 把 updates.json 提交回 main**。

最后一步是关键：`updates.json` 落在 main 上，Zotero 靠它发现新版本。
**忘了更新它的后果是用户永远停在旧版、且没有任何报错** —— 所以由 tag 与 manifest
版本不一致就直接失败的守门员（`tools/make_updates_json.py`）来兜底。

发布中途失败可以直接重跑：Release 已存在时会覆盖附件，不会卡在"已存在"上报错。

> **发布说明怎么来的**：优先取 tag 的**附注**（`git tag -a -m "…"`）；
> 没写附注才回退到"两个 tag 之间的提交列表"。
> ⚠ 不用 `gh release create --generate-notes` —— 它只列合并的 PR 和贡献者，
> 本项目直接往 main 提交、没有 PR，实测生成的说明只剩一行 compare 链接。
> 事后想改说明：`gh release edit <tag> --notes-file <文件>`。

> ⚠ **一个不会告警的坑**：`.gitattributes` 只对**新检出**生效。如果你在加它之前就已经
> 有工作区，那些文件会一直保持旧行尾（本地 CRLF），而 `git status` **永远是干净的** ——
> 因为 `text=auto` 会在比较前把 CRLF 归一化掉，git 不会提醒你。
> 后果是**本地打包出来的 xpi 与 CI 构建的不一致**（实测差 2 KB）。
> 查与修：
> ```bash
> git ls-files --eol | grep -E '^i/lf\s+w/crlf.*eol=lf'   # 该是 LF 却是 CRLF
> # 把这些文件删掉再 git checkout -- . 重新落盘即可
> ```
>
> 另注：即使条目内容完全一致，**整包 sha 也可能不同** —— zip 容器的压缩字节
> 随 Python/zlib 版本变化。要判断"是不是同一个包"，应比对**条目内容**，不是整包哈希。

### ⑤ 提交信息怎么写

提交历史是**公开**的，它不是内部开发日志。按「别人需不需要知道」分两档：

**① 面向使用者的改动**（功能、行为、安装方式变了）→ 详细写，说清「改了什么、为什么」。
   例：`面板新增 WAL 边车文件说明；测试不再掩盖缺说明的文件`

**② 内部整理 / 自检加固 / 修既有缺陷**（使用者无感）→ **一句话中性描述就够**。
   不要写「哪里曾经漏了、错了、发出去过」这类复盘 —— 那等于公告自己的问题，
   对使用者毫无价值。
   例：`修复若干已知问题，并加强发布前自检`、`内部整理：统一文本编码并加入编码自检`

**判据：这条信息是给「用这个项目的人」看的，还是给我自己看的？**
给自己看的那部分，写在这些地方，**不要写进提交信息**：

| 写什么 | 写在哪 |
|---|---|
| 「为什么这么写」的代码决策 | 就地的代码注释（给未来的维护者） |
| 流程、约定、踩过的坑 | 本文件 `docs/DEVELOPMENT.md` |
| 本次发版对使用者的说明 | **tag 附注**（`release.yml` 用它当 Release notes） |
| 纯本机的排查过程、事故复盘 | 自己的工作日志，**不进仓库** |

> 一句话：**提交信息只留中性的一行；复盘留在旁边，不留在牌子上。**

## 目录结构

```
<项目目录>\
  README.md              落地页（对外：能做什么、怎么开始）
  INSTALL.md             从零装到能用 —— 使用者看这份
  ARCHITECTURE.md        架构总览 + 关键功能的实现方式
  docs\
    DESIGN.md            设计与用法详解（为什么这么做 / 各功能怎么用 / 边界）
    DEVELOPMENT.md       本文件 —— 给改这个项目的人
  requirements.txt       Python 依赖（**装环境就靠这个**，别手敲包名）
  kb-location.json       位置配置（可选，插件设置面板写的；含 kb_dir /
                         project_root / python_exe / ollama_exe）
  offline\               离线管道（跟 DSH 无关，手动跑）
    schemas.py           两边共用的数据结构、表定义、**路径解析链**、可读名
                         ← 唯一的接口约定，改结构必须改这里
    zreader.py           读 Zotero 库（只读快照）、HTML 笔记转 Markdown
    converter.py         切片、Markdown 渲染、向量计算
    convert.py           命令行入口：构建知识库（结束后自动重建 INDEX.md）
    maintain.py          环境自检 / 统计 / 备份 / 修 FTS
    judge.py             模型调用：打标签、摘要、抽经验、分类建议（多 provider）
    learn.py             从 DSH 会话记录补经验（产出待确认清单）
  online\                在线部分（DSH 拉起）
    query.py             中文查询改写（含为什么这么做）
    searcher.py          混合检索 + 权重加成 + 可读名（ref/label）
    server.py            MCP 服务器（工具 + 资源）
    localserver.py       本地 HTTP 服务（端口 8765）：插件接口 + 任务队列 + 库监听
    acquire.py           **获取文献**：DOI 归一化 + 派任务给插件 + 把结果讲成人话
                         （它**不下载任何东西**，抓取在 Zotero 进程里做，见上文）
  tools\
    gui.py               面板**入口**：组装 App + 命令行自检 + mainloop
                         （双击 scripts\0-panel.vbs 打开；`--check` 无界面自检）
    panels\              面板的界面代码，**一个页签一个模块**
      common.py          公共：路径常量、字体、sys.path 引导、知识库目录说明表
      base.py            窗口骨架与通用能力（装配、日志、跑子进程、状态刷新）
                          ★ `_tab_panes`（每页套可滚动画布 + 可拖的底部格）、
                            `_on_wheel`（滚轮滚"鼠标底下那一页"）、`_build_log`（日志可拖）
      tab_struct.py      「知识库结构」页（含「文献管理器中查看」入口）
      tab_experience.py  「经验库」页
      tab_ai.py          「分类建议」页（文献列表、检索、分类建议与写回）
      tab_quality.py     「损坏查询」页
      tab_meta.py        「元数据」页
      tab_env.py         「运行环境」页（服务、同步、侧载、诊断、升级）
      tab_advanced.py    「高级」页（全量重建、自检、备份、清理、补齐分级文件）
      browser.py         「打开知识库」的级别选择窗口
    paper_picker.py      选文献的弹窗（列宽可拖、表头可排序）
    build_bootstrap.py   ★ 把 zotero-plugin\src\*.js 拼成 bootstrap.js
                         （改完插件源码必须跑它，否则打包会被拒绝）
    check_kb_levels.py   ★ 盯"知识库分级清单"在 Python 与 JS 两侧一致
    make_index.py        生成给人看的文献清单 INDEX.md（Markdown 表格）
    kb_admin.py          经验层管理：查看 / 清理测试数据 / 导出导入 / 重排编号
    zotero_upgrade.py    Zotero 升级前检查 / 备份 / 升级后核对
    zotero_sync.py       通过 Zotero 10 本地 API 写回分类（分类重整用）
    zotero_js.py         在 Zotero 里自动执行 JS（经插件任务队列，免手工粘贴）
    check_acquire.py     **获取文献链路自检**：逐段验"知识库/服务/插件/DOI/
                         参数/干跑"，先失败的那一环就是问题所在（--live 才联网）
    check_plugin.py      插件静态检查 + 打包 xpi（**发版前跑这个**，
                         最后两步会自动调 check_xpi_paths、check_prefpane）
    check_xpi_paths.py   查 xpi 里有没有写死的开发机路径（发布前必查）。
                         它导出的 SENSITIVE 模式被 audit_release 的全仓库扫描复用
                         —— 模式只留一处定义，不要再各写一份
    audit_release.py     发布前综合审计：**全仓库**扫密钥 / 本机用户名 /
                         开发机目录名（模式见 check_xpi_paths.SENSITIVE），
                         外加「必须可移植的文件」与安装可用性。
                         ⚠ 2026-10-04 补的「开发机目录名」这条：此前它只作用于 xpi，
                         于是 skills/ 里写死的开发机路径从首个提交起就躺在公开仓库里、
                         没人发现。正文/代码里命中即**失败**；注释里讲这个坑用的例子
                         只提示（与 check_xpi_paths 同口径）。
                         ⚠ 写这段说明时也要注意：那几个词本身不能出现在非注释行 ——
                         本文件就被自己拦过一次（见 check_xpi_paths.SENSITIVE）
    check_prefpane.py    查 settings.xhtml 能不能被 Zotero 正常加载
                         （XML 声明会让面板点进去一片空白，见下）
    pack_plugin.py       打包器（ZIP 条目属性对齐可用插件）
    bump_version.py      改版本号（只改 manifest，其余地方自动读）
    check_js_syntax.py   插件脚本语法检查（按 AsyncFunction 函数体校验）
    audit_plugin_api.py  审计插件用到的 Zotero API 是否存在
    audit_panel.py       面板体检（每个按钮背后的命令/文件/参数是否真的可用）
    check_abs_paths.py   复核索引里没存绝对路径（迁移安全性）
    migrate_kb.py        迁移知识库位置（复制 + 备份 + 校验）
    check_plugin_prefs.py   对照插件 prefs 与服务 token
    test_task_bridge.py  任务桥接自检（模拟插件跑完整链路）
    asar_read.py         按需读 DSH 的 app.asar（查官方 API 用）
    add_path_bootstrap.py 给脚本批量插入自包含路径设置
    preview_icon.py      **图标验收**：用 Edge 无头模式把插件的 SVG 图标渲染成
                         16/32/48/96 四档 PNG（浅色/深色主题各一版），肉眼确认
                         "小尺寸下还认不认得出"。出图是临时产物，**不进 xpi**
    make_icon_pptx.py    **图标设计源**：把设计画成 PowerPoint 原生形状
                         （`icon-design.pptx`，用户可直接拖）。顶部那一段常量
                         就是全部几何；书的接缝由参数算出，保证精确相接
    make_icon_svg.py     由**同一份常量**生成 `zotero-plugin/icon.svg` 与
                         `toolbar-icon.svg`（两种格式不会漂移）
    make_panel_icon.py   把同一个图标渲染成**面板用的位图**（Tk 读不了 SVG）：
                         `assets/icon.png` 给 `iconphoto`、`assets/icon.ico`
                         给 `iconbitmap`（任务栏/Alt-Tab）。**面板图标与插件
                         图标是同一个设计**，改一处两边一起变
    scrub_domain_words.py **发布前脱敏检查**：扫真实研究领域词/个人路径残留，
                         `--check` 有残留则返回非零，可挂进发布流程
    measure_final.py     量"椭圆弧端到节点圆心"的实际距离（调断口时用）
    assets\              面板用的图片资源
      avatar.png         面板顶部的头像（160×160，圆角方形）
      icon.png / .ico    面板窗口图标（由插件图标渲染而来，见 make_panel_icon.py）
  其余                    调试与一次性排障脚本（**不进 xpi**，路径写死无妨）
  bundle\                接入 DSH 用的 bundle 包（package.json + cordis.patch.yml）
  zotero-plugin\         插件（源码 + 生成物 + README、settings 面板、诊断脚本）
    src\*.js             ★ **源码**：按功能分 20 个文件（00-core … 99-bootstrap）
                         每个文件用 `Object.assign(ZoteroKB, {…})` 挂成员，
                         所以单独也是合法 JS，能逐个做语法检查
    bootstrap.js         ★ **生成物**（由 src\ 拼出来，xpi 里装的是它）
                         ⚠ 别直接改这里 —— 会被下次生成覆盖；改了 src 要跑
                         `python tools\build_bootstrap.py`
    icon.svg             插件图标（96×96）：**立着的书 + 斜掠的椭圆轨道**
    toolbar-icon.svg     工具栏/右键菜单的小图标（16×16）：同一意象的简化版，
                         用 `context-fill` 跟随 Zotero 主题自动变色
  skills\
    zotero-kb\           知识库使用规则（给 DSH 的技能）
    zotero-acquire\      **找文献/补文献的规范**：搜 → 核 DOI → 列候选表（标题可点开）
                         → 勾选 → kb_acquire 交给 Zotero 抓；含边界与兜底顺序
    zotero-plugin-dev\   **Zotero 插件开发经验**（manifest/沙箱/自定义列/调试方法论）
  scripts\               双击用的批处理
    install-env.cmd      **一键装 Python 环境**（下 uv → 建 venv → 装依赖 → 拉模型）
    install-env.ps1      上面那个的实际逻辑
    0-panel.vbs          打开管理面板（推荐入口，无黑窗口）
    1-convert.cmd        构建/更新知识库
    3-maintain.cmd       环境自检（不带参数）/ 统计 / 备份
    4-service.vbs        后台启动本地服务
    _kbtools.vbs         **共享工具**：两个 VBS 用它现算知识库/日志位置
                         （不再写死 <项目>\kb\logs）
    5-sideload.cmd       侧载插件（Zotero 10 上不可靠，优先用 UI 安装）
    sync-skill.ps1       把 skills\ 下所有技能同步到 DSH 技能目录
  tests\                 单测与自检（18 个 test_*.py；
                         **逐个** `python tests\test_*.py` 跑，退出码 0 = 通过 ——
                         不要用 unittest discover，这些脚本是独立进程自检。
                         ⚠ 断言**条数不是固定值**：`test_matching.py` 抽样真实库，
                         项数在 13~18 间浮动，属正常。所以别把某个总数写进文档，
                         判据是"每个文件退出码 0 / 失败 0"。
                         2026-10-05 新增：`test_experience_edit.py`
                         （经验写入只有一份实现 + 原子性 + 旧库自动补列）、
                         `test_prompts.py`（话术搬家逐字比对 + 改坏必被拒 +
                         经验口径）、`test_mineru_probe.py`（假 runner 覆盖
                         就绪/缺失/超时/崩了/没装五种 `probe()` 结果）、
                         `test_mineru_guide.py`（安装引导的预检四种坏情况 +
                         窗口装配）、`test_mineru_parse.py`（**逐页渲染 / 指纹 /
                         解压防穿越 / 失败不覆盖旧产物 / 按篇回落**）、
                         `test_ollama_guide.py`（Ollama 引导预检 + 同步 JS 语法）、
                         `test_metafill_sources.py`（元数据补全的**双源**：归一化 /
                         两路合并 / 冲突取哪一路 / 渲染 / 模型裁决）、
                         `test_digest.py`（中间层：按标题切节 / 丢页眉 /
                         跳参考文献 / 超长节再切 / 节指纹增量 / 渲染 / 级别清单）。
                         ⚠ 解析相关的测试一律用**假 runner** 造 zip 产物 ——
                         真解析 15~36 秒/篇且依赖装好的 MinerU，不能进单测。
                         ⚠ `test_paras.py` / `test_chat_endpoints.py` **已删**
                         —— 窗格与逐段检查那条链整体删除。)
  .mineru\               MinerU（**可选组件**）的独立 venv + 模型；删目录即卸载
  zotero-plugin\src\20-kbview.js   右侧栏「知识库」分区（只读展示 kb/ 里的 md）
  docs\知识库层级设计.md  知识库五层（原始/正文/档案/视图/图片）的重排方案与迁移步骤
                         ⚠ 第七点五节是**中间层（分节纲要）**：全文与摘要之间的那一级
                         ⚠ 跑全库解析前先退出 Ollama（抢显存，实测差十倍）
                         （由 scripts\install-mineru.ps1 装，见 ARCHITECTURE B11）
  .venv\                 Python 依赖（见 requirements.txt）
```

### 知识库在哪（与代码分开）

知识库**默认不在项目目录里**，而是跟着 Zotero 数据目录：

```
<Zotero 数据目录>\zotero-kb\        ← 实测：<知识库>
  mineru\<key>\                        MinerU 解析产物（可选组件；面板「PDF 解析」页看状态）
  index.db             索引：条目 / 切片 / FTS / 向量 / 经验 / 权重
  INDEX.md             **给人看的**文献清单（Markdown 表格，带作者年份标题）
  papers\*.md          每篇一份档案（元数据+摘要+笔记+标注+正文首段），按 KEY 命名
  fulltext\*.md        每篇正文，带 `## p.N` 页码锚点
  views\*.md           每篇的**分级视图**：`<KEY>.tldr.md` / `.figures.md` /
                       `.weight.md`（面板「打开知识库」与 Zotero 右键菜单读它）
  inbox\               待确认的经验、以及"扫到哪了"的记录
  logs\                运行日志
  .cache\              嵌入模型缓存
  MANIFEST.json        上次构建的统计与警告
```

> `papers\` `fulltext\` `views\` 都是**可重建的派生物**：删掉跑一次
> 「手动更新」就回来。`views\` 单独补齐用 `python offline\maintain.py views`
> （面板「高级」页的「补齐知识库分级文件」就是它），不必为了几个小文件做全量重建。

这样重装/换机只要同步 Zotero 那一份就够。位置可以在插件设置里改，
解析优先级见 `offline/schemas.py` 的 `_resolve_kb_dir`（环境变量 `KB_DIR`
> 配置文件 > 跟随 Zotero > 老位置）。

> 在面板的**「知识库结构」页**能看到上面每个文件/目录的用途、大小、
> **以及能不能删** —— 不用回来翻文档。

---

---

## 实测规模

| 项 | 值 |
|---|---|
| 文献条目 | 100（Zotero 里排除附件/笔记/标注/回收站后的顶层条目） |
| 可检索片段 | 3706（正文 + 高亮标注 + 笔记） |
| 向量 | 3706 × 512 维（`BAAI/bge-small-zh-v1.5`） |
| 全文来源 | Zotero 自己的 `.zotero-ft-cache` 96 篇；其余 4 篇是网页类条目（本来没 PDF） |
| 词元表 | 12710 项（用于中文检索的稀有度加权） |
| 构建耗时 | 全量约 2.5 秒；向量首次约 74 秒（之后只补新增的） |

---

---

## 显示与交互的打磨

界面上的改动，理由大多是"这里看不清 / 这里不好理解"：

| 改动 | 为什么 |
|---|---|
| 「更新索引（增量）」→ **「手动更新」** | "增量"是给写代码的人看的词，使用者只关心"我新加了文献，点它更新" |
| 「全量重建」→ **「全部重建」** | 同上 |
| **文献选择改成独立弹窗** | 原来把 `作者 年份 · 标题 [KEY]　［分类］` 拼成一整行塞进下拉框：标题长短不一时既对不齐、又改不了宽度，长的被截断短的留空白。拆成表格后每列可拖宽、表头可排序、窗口可拉大 |
| **统一字体** | 原来到处写 `("", 9, "bold")`（空字体名 = 拿系统默认字体再改字号）。中文 Windows 上会跟别处的雅黑不一致，加粗后字距发挤、字看着糊 |
| **每个按钮挂悬停说明** | 光看名字不知道是做什么的 |
| **去掉了 `tk scaling 1.25`** | 它和显式字号是乘在一起生效的，配上雅黑 9~10 号会偏大且发虚 |
| 底部日志 | 长任务实时输出、可停止；不会卡界面 |

**右侧栏「知识库预览」分区**（Zotero 条目右侧栏）

| 改动 | 为什么 |
|---|---|
| 层面下拉，默认「分节纲要」 | 摘要太短、全文太长，读的时候最常要的是中间那一层 |
| **字号下拉**（小 / 标准 / 较大 / 大 / 特大） | 正文 12px 在右侧栏里偏小、读久了费力；只作用于本分区，不动 Zotero 的默认设置 |
| 工具条固定在顶部 | 滚动时层面与字号跟着滚走，想切一下还得先滚回顶上 |
| 正文限高 + 内部滚动 | 正文几万字，不限高会把 Zotero 自己的分区顶出屏幕 |
| 提示行显示「用了哪一级 / 多少字 / 公式是否已渲染」 | 看不出"现在读的是哪一层"，也不知道空白时是没生成还是读不到 |
| 分区标题「知识库预览」 | 标题原本是空的 —— FTL 条目写成"只有名字、值留空"，Fluent 没有内容可写 |
| 层面下拉按内容自适应宽度 | 固定宽度会把「摘要与要点」截成「摘要与要」 |
| 公式按数学排版显示 | 正文里的 `$$…$$` 在生成时转成 MathML，由 Zotero 自带的显示能力排版，不需要额外插件或字体 |

**右键菜单**

| 改动 | 为什么 |
|---|---|
| 能力不可用时，在下级菜单第一行写明「未连接：原因」 | 菜单项点了没反应，比写明原因更难排查 |
| 「分类建议」「补全元数据」收进「连接到本地模型」二级菜单 | 两项都是"要本地模型"的动作，平铺在顶层既占地方，也没有统一的落点写状态 |
| 「重建知识库条目（这一篇）」→「重建本条目知识库」 | 原名太长，且"这一篇"与上下文的"本条目"用词不一致 |
| 「标为重点」的悬停说明改成真实倍数（约 1.0 → 2.4 倍） | 原来写「权重 ×4」，与实现不符（实际是 `1 + ln(4)` ≈ 2.39） |

**管理面板**

| 改动 | 为什么 |
|---|---|
| 新增「PDF 解析」页 | 解析档位、每篇状态、只补缺失 / 全库重解析 / 删产物，原来散在命令行里 |
| 新增「生成纲要」按钮 | 中间层必须显式生成（按节调模型，一篇学位论文几分钟），入口得在界面上 |
| 知识库结构表补 `mineru\` 一行 | 新目录必须进"这是什么、占多大、能不能删"那张表，否则不敢动 |

排障时先跑 `python tools\gui.py --check`（不开窗口，只报环境与索引状态）。

---

## 排错

| 症状 | 先看这里 |
|---|---|
| DSH 里没有 `mcp__zotero-kb__*` 工具 | 重启 DSH；再跑 `3-maintain.cmd check` 看 [6] 段 |
| 检索结果明显不对 | `3-maintain.cmd check` → 若提示 FTS 与切片不同步，跑 `maintain.py reindex-fst` |
| 搜不到明明有的内容 | 换中英同义词各试一次；可能是 Zotero 里那篇没 PDF |
| 报"没有可读正文" | 该条目是网页类，或 PDF 没有文字层 |
| 小模型功能报错 | `judge.py status`；Ollama 没开不影响检索 |
| 向量检索失效 | `3-maintain.cmd stats` 看向量数；缺失就跑 `1-convert.cmd` |
| DSH 整个 profile 不见了 | 检查 `cordis.patch.yml` 有没有 BOM（首字节应是 `35 32 89`）|

---

---

## 后续可做

- **写回 Zotero**：等 Zotero 升到 10+ 后本地 API 开放写入（需一次授权弹窗），
  届时在工具里加一条 HTTP 分支即可，不动其他代码。另一条路是装第三方
  [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp) 插件
  （插件内部 JS 写入，不受只读限制），作为第二个 `dsh-mcp-client` 条目接入。
- **更强的嵌入模型**：`fastembed` 支持 `intfloat/multilingual-e5-large`（2.24GB）
  等；换模型后跑一次 `1-convert.cmd --full`（代码会自动清掉旧模型的向量重算）。
- **OCR**：给扫描件补文字层。

---