/* 默认首选项。Zotero 加载 bootstrapped extension 时会读这个文件。 */
pref("extensions.zotero.zotero-kb.model", "");
pref("extensions.zotero.zotero-kb.autoProcess", true);
pref("extensions.zotero.zotero-kb.categories", "");
pref("extensions.zotero.zotero-kb.createMissing", true);
// 是否允许插件轮询本地服务领取"在 Zotero 里执行 JS"的任务。
// 这是给自动调试/自动验证用的能力（走本地 token 认证、只监听 127.0.0.1）；
// 不想开就设为 false，插件的核心功能（切片、分类建议）不受影响。
pref("extensions.zotero.zotero-kb.autoTaskPoll", true);
// ---- 任务通道（W3 线2，2026-10-13）：文件信箱 + 老 HTTP 队列
// 信箱目录：留空 = <知识库目录>\mailbox（正路）。只有在"知识库目录偏偏拿不到"
// 时才需要手填一个别的可写目录。
pref("extensions.zotero.zotero-kb.mailboxDir", "");
// 是否还去消费**老的 HTTP 队列**（GET /task + POST /task/result）。
// 默认 true：过渡期两条队列都消费（同一个任务只会写进其中一处，不会双执行）。
// 等确认没有任何调用方还在用老通道，把它设成 false，插件就彻底不碰 8765 了。
pref("extensions.zotero.zotero-kb.taskPollHttp", true);
// W3 线3（2026-10-14）：是否**自动拉起**本机服务 8765（Zotero 启动时）。
// 默认 true = 老行为。用户拍板停服务后把它设为 false —— 否则 Zotero 一重启
// 服务又被拉起来，"停服务"就是假的。**只影响"自动拉起"这一个动作**：
// 插件此时已全部走引擎桥/文件信箱，不依赖该服务。手动入口仍是
// scripts\4-service.vbs；想恢复自动拉起就把它设回 true（或手动跑那个脚本）。
pref("extensions.zotero.zotero-kb.autoStartService", true);
// ---- 模型来源（支持 Ollama 与 OpenAI 兼容 API 两种后端）
// provider: ollama（默认，本机）| openai（任何 OpenAI 兼容服务）
pref("extensions.zotero.zotero-kb.provider", "ollama");
// OpenAI 兼容后端用这几个（provider=openai 时生效）
pref("extensions.zotero.zotero-kb.apiModel", "");
pref("extensions.zotero.zotero-kb.apiKey", "");
pref("extensions.zotero.zotero-kb.apiBaseUrl", "");
// 知识库目录位置：由插件从服务端 /health 自动记住（不用手填）。
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
