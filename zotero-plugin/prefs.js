/* 默认首选项。Zotero 加载 bootstrapped extension 时会读这个文件。 */
// ---- 模型配置：**不在这里**（2026-10-17 起）
// 唯一权威来源是 <知识库>\llm-config.json（provider / model / base_url / api_key /
// ollama_host），管理面板「运行环境」页编辑，插件与引擎都读它。
// ⚠ 老的 zotero-kb.model / provider / apiModel / apiKey / apiBaseUrl 五个 pref
//   已退役：启动时由 src/04-prefs.js::migrateLlmPrefs 搬进配置文件并清掉。
//   别把它们的默认值加回来 —— 两个地方都存必然漂移。

// 模型下拉是否只列"名字带上下文后缀"的派生模型（默认开；管理面板可改）。
// 没有后缀的模型看不出上下文窗口大小，容易挑错。这是**面板显示偏好**，
// 不影响已经保存的模型。
pref("extensions.zotero.zotero-kb.filterBareModels", true);
pref("extensions.zotero.zotero-kb.autoProcess", true);
pref("extensions.zotero.zotero-kb.categories", "");
pref("extensions.zotero.zotero-kb.createMissing", true);
// 是否允许插件轮询文件信箱领取"在 Zotero 里执行 JS"的任务。
// 这是给自动调试/自动验证用的能力；不想开就设为 false，核心功能不受影响。
pref("extensions.zotero.zotero-kb.autoTaskPoll", true);
// ---- 任务通道（W3 线2，2026-10-13）：**只有文件信箱**
// 信箱目录：留空 = <知识库目录>\mailbox。只有在"知识库目录偏偏拿不到"
// 时才需要手填一个别的可写目录。
// （老 HTTP 队列与 8765 的 pref 已于 2026-10 随服务代码一起删除。）
pref("extensions.zotero.zotero-kb.mailboxDir", "");
// 知识库目录位置：由插件从引擎 kb.health 自动记住（不用手填）。
pref("extensions.zotero.zotero-kb.kbDir", "");
// 知识库同步（可选，默认关闭）。
// ⚠ 不是用 Zotero 自带同步 —— 它的文件同步只覆盖有附件条目的文件，
//   知识库目录不在其中（详见 README）。这里走同步到目录或执行命令。
pref("extensions.zotero.zotero-kb.syncEnabled", false);
pref("extensions.zotero.zotero-kb.syncMode", "none");
pref("extensions.zotero.zotero-kb.syncTarget", "");

// ---- 从 DSH 导入文献（10-acquire.js）
// ⚠ 这两个原来只在 registerPrefs 里设了兜底默认值、**没写进 prefs.js** ——
//   修好 check_plugin 里那条"首选项键对齐"检查后立刻被抓出来（那条检查原来
//   找的是一个代码里根本不存在的键形式，一直"扫到 0 个键、通过"）。
pref("extensions.zotero.zotero-kb.acquireConfirm", false);
pref("extensions.zotero.zotero-kb.acquireAutoClassify", true);

// ---- MinerU（可选组件）的首次安装引导
// 「首次启动且没检测到 MinerU 时弹一次，之后不再打扰」——用户定的规矩：
// 取消后入口收进面板「知识库结构」页（那个按钮只在没装时显示）。
pref("extensions.zotero.zotero-kb.mineruGuideDone", false);
// 可选组件（MinerU + Ollama）的安装引导是否已经问过
pref("extensions.zotero.zotero-kb.optionalGuideDone", false);

// 右侧栏「知识库」分区显示哪一级（20-kbview.js）。默认「分节纲要」
// —— 那一层就是为"对着 PDF 读"设计的（每节带页码范围）。
pref("extensions.zotero.zotero-kb.kbviewLevel", "outline");

// 「知识库」那一列的字号（px，只作用于这一列）
pref("extensions.zotero.zotero-kb.kbviewFont", 13);

// ⚠ 宽度**没有**首选项：那一列的宽度就是原生条目窗格的宽度（用户拖
//   #zotero-items-splitter / #zotero-context-splitter 即可）——用户 2026-10-11
//   明确要「覆盖在原侧边栏上、同宽窄能拉动」，所以别再加「窄/标准/宽」档位。

// ---- 运行环境（三个位置互相独立，谁也不能由谁推算）
//
// 「项目目录」= 用户填的（权威来源，设置面板「运行环境」区）
// 「serverProjectRoot」= 服务端 /health 报的（兜底）
// ⚠ 两者**必须分开存**：老版本写同一个键，服务端一报就把用户填的覆盖了。
pref("extensions.zotero.zotero-kb.projectRoot", "");
pref("extensions.zotero.zotero-kb.serverProjectRoot", "");
// Python 解释器：留空 = 用 <项目目录>\.venv\Scripts\pythonw.exe
pref("extensions.zotero.zotero-kb.pythonExe", "");
// Ollama 程序：留空 = 探测 %LOCALAPPDATA%\Programs\Ollama（可选组件）
pref("extensions.zotero.zotero-kb.ollamaExe", "");
// 服务端报的路径（只读缓存，供排错与兜底）
pref("extensions.zotero.zotero-kb.serverKbDir", "");
pref("extensions.zotero.zotero-kb.serverPython", "");
pref("extensions.zotero.zotero-kb.serverOllama", "");
