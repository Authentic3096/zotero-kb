/* 默认首选项。Zotero 加载 bootstrapped extension 时会读这个文件。 */
pref("extensions.zotero.zotero-kb.server", "http://127.0.0.1:8765");
pref("extensions.zotero.zotero-kb.token", "");
pref("extensions.zotero.zotero-kb.model", "");
pref("extensions.zotero.zotero-kb.autoProcess", true);
pref("extensions.zotero.zotero-kb.categories", "");
pref("extensions.zotero.zotero-kb.createMissing", true);
// 是否允许插件轮询本地服务领取"在 Zotero 里执行 JS"的任务。
// 这是给自动调试/自动验证用的能力（走本地 token 认证、只监听 127.0.0.1）；
// 不想开就设为 false，插件的核心功能（切片、分类建议）不受影响。
pref("extensions.zotero.zotero-kb.autoTaskPoll", true);
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

// ---- 内容窗格「本地模型」分区
// 退出 Zotero 前提醒"这个窗格里的对话不会被保存"（对话框里带「下次不再提示」）。
// 默认开：按需求对话**确实不落盘**，不提醒的话用户会以为聊过的东西还在。
pref("extensions.zotero.zotero-kb.chatQuitWarn", true);
// 聊天的上下文窗口（Ollama num_ctx）。默认 16384 才装得下"注入全文级"
// （8192 大约只够一篇 6 页论文的一半）。
pref("extensions.zotero.zotero-kb.chatNumCtx", 16384);

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
