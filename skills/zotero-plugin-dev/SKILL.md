---
name: zotero-plugin-dev
description: 开发 Zotero 7/8/9/10 桌面端插件（bootstrapped extension）。当需要写插件、加自定义列、监听条目事件、调 Zotero 内部 API、或排查"插件装不上/装上了没反应/没显示"时使用。含大量实测踩坑与可复现的诊断方法。
---

# Zotero 插件开发（实测经验版）

本文件里的每条结论都在 **Zotero 10.0.5 / Firefox 140** 上实测验证过，标注了
证据（日志、状态文件、心跳数据）。踩过的坑都写了"症状 → 根因 → 修法"。

参考实现在 `D:\DSHplugins\zotero-kb\zotero-plugin\`（一个可工作的插件：
自定义权重列 + 设置面板 + 新条目监听 + 任务轮询）。

---

## 0. 环境与安装路径（先记这几条，能省几小时）

| 事项 | 结论 |
|---|---|
| 装 profile | `%APPDATA%\Zotero\Zotero\Profiles\<随机>.default` |
| 扩展注册表 | 同目录 `extensions.json`（+ `addonStartup.json.lz4`，非标准 LZ4 帧） |
| 已装插件 | `extensions\<插件id>.xpi` |
| Zotero 自身代码 | `D:\Application\Zotero\app\omni.ja`（**Zotero 的应用代码在这里**，约 45MB） |
| Mozilla 平台代码 | `D:\Application\Zotero\omni.ja`（约 10MB） |
| Zotero 版本 | `zotero.exe` 的 `VersionInfo.ProductVersion`（最可靠；`platform.ini` 是 Mozilla 构建信息、`install.log` 是首次安装记录，都**不**反映当前版本） |

**读源码是最高效的调试手段**。用 Python 直接读 `app\omni.ja`（它就是个 zip）：

```python
import zipfile
with zipfile.ZipFile(r'D:\Application\Zotero\app\omni.ja') as z:
    src = z.read('chrome/content/zotero/xpcom/plugins.js').decode('utf-8','replace')
```

关键文件：
- `chrome/content/zotero/xpcom/plugins.js` —— 插件加载机制（`_loadScope` / `_callMethod`）
- `chrome/content/zotero/xpcom/pluginAPI/itemTreeManager.js` —— 自定义列的 API 定义
- `chrome/content/zotero/components/virtualized-table.js` —— **标准单元格渲染**
- `chrome/content/zotero/itemTreeColumns.js` —— 内置列的参数写法（照抄它）
- `modules/addons/XPIInstall.sys.mjs` —— manifest 解析、`targetApplications` 构建
- `modules/addons/XPIDatabase.sys.mjs` —— `isCompatibleWith` 兼容性判定

---

## 1. manifest.json

```json
{
  "manifest_version": 2,
  "name": "我的插件",
  "version": "1.0.0",
  "description": "...",
  "author": "...",
  "homepage_url": "https://example.com",
  "icons": { "48": "icon.svg", "96": "icon.svg" },
  "applications": {
    "zotero": {
      "id": "myplugin@example.com",
      "update_url": "https://example.com/updates.json",
      "strict_min_version": "6.999",
      "strict_max_version": "10.*"
    }
  }
}
```

### ⚠ 坑 1：`update_url` 缺失 → 整个插件**装不上**，且报错毫无线索

**症状**：UI 安装报 `无法安装插件"…"。它可能无法与该版本的 Zotero 兼容。`
用 `AddonManager.getInstallForFile()` 探测则得到：

```
install.error = -3        （ERROR_CORRUPT_FILE）
install.addon = undefined
```

**根因**：Zotero 10 上 `update_url` 实际是必需的。删掉它、或写成空字符串
（`"update_url": ""`）都会让**解析阶段**失败。

**证据**（用只差一个字段的变体包逐个实测）：

| 变体 | 差异 | 结果 |
|---|---|---|
| `update_url` + `strict_min/max` | — | `error=0` ✅ **能装** |
| 去掉 `update_url` | 只差这个 | `error=-3` ❌ |
| 只加 `homepage_url` | — | `error=-3` ❌ |
| 用 `browser_specific_settings` | 官方 schema 推荐的写法 | `error=-3` ❌ |
| 纯英文 name/description | — | `error=-3` ❌ |
| 去掉 icons | — | `error=-3` ❌ |

所以**别信"官方推荐 `browser_specific_settings`"**：Firefox 的 manifest schema 里
`applications` 确实标着 `"description": "...please use 'browser_specific_settings'",
"max_manifest_version": 2`，Zotero 源码注释也说 "as of MV3, only
browser_specific_settings is accepted" —— 但**实测 Zotero 10 上只有
`applications.zotero` 能用，`browser_specific_settings` 反而不行**。
文档说"推荐"不等于"必需"，也不等于"可用"。

### 其他 manifest 注意点

- `strict_min_version` **不允许含 `*`**（`XPIInstall.sys.mjs:495` 会直接抛错）
- `id` 用 `name@some.domain` 形式（如 `myplugin@example.com`）；`@local` 这类不规范
- `icons` 用 SVG 完全可行（与可用插件一致），PNG 也可以
- 版本范围**可选**：不写时 Zotero 用默认 `min="0"` / `max="*"`
  （`XPIDatabase.sys.mjs:572` 的 `app.minVersion || "0"`）

---

## 2. bootstrap.js —— 生命周期必须用**顶层函数声明**

Zotero 的 `plugins.js` 这样找生命周期方法：

```javascript
// _callMethod，第 239 行
func = scope[method] || Cu.evalInSandbox(`${method};`, scope);
if (!func) {
  Zotero.warn(`Plugin ${id} is missing bootstrap method '${method}'`);
  return;   // 只是 warn！不报错
}
```

`scope[method]` 找的是**沙箱全局属性**，所以必须写成顶层函数声明：

```javascript
// ✅ 对：顶层函数声明 → 直接成为沙箱全局属性
function startup({ id, version, rootURI } = {}) { ... }
function shutdown() { ... }
function onMainWindowLoad({ window } = {}) { ... }
function onMainWindowUnload({ window } = {}) { ... }
function install() {}
function uninstall() {}

// ❌ 错：只挂在对象上 —— scope.startup 是 undefined，
//    只能靠 Cu.evalInSandbox 兜底，多一层不确定性
var MyPlugin = { startup: function () { ... } };
```

### ⚠ 坑 2：`startup` 里的 `this` **不可靠**

**症状**：插件 `active=true`，但完全没反应（`Zotero.MyPlugin` 是 undefined）。

**根因**：Zotero 调 `startup()` 时 `this` 不保证指向你的对象。第一行
`this.id = id` 就抛错，而它发生在"挂载对象"之前，错误又被 catch 吞掉。

**修法**：用闭包引用，不用 `this`：

```javascript
function startup({ id, version, rootURI } = {}) {
  var self = MyPlugin;              // 闭包引用，不依赖 this
  try { Zotero.MyPlugin = self; } catch (e) {}   // 第一件事就挂载
  self.id = id; self.version = version; self.rootURI = rootURI;
  ...
}
```

**为什么必须挂到 `Zotero.` 上**：bootstrap.js 在 `Cu.Sandbox` 里执行，顶层的
`var MyPlugin` 只是沙箱全局的一个属性 —— **不会**自动变成 `Zotero.MyPlugin`。
而设置面板脚本、外部诊断脚本都要通过 `Zotero.MyPlugin` 访问它。

### ⚠ 坑 3：更新插件后 `startup` 不会重跑

**症状**：装了新版本、`extensions.json` 里版本号也变了，但插件行为还是旧的。

**根因**：Zotero 在**运行中**更新插件**不会**重新执行 `bootstrap.js` 的
`startup()` —— 代码只在加载时读一次。

**修法**：装完必须**完全退出 Zotero 再打开**（不是关窗口、不是刷新）。
诊断脚本里可以 `addon.disable()` → `addon.enable()` 强制重跑生命周期。

### 官方对职责划分的建议

> 通常地，在 `startup` 中初始化插件的本地化系统、设置、兼容性等，
> 在 `onMainWindowLoad` 中初始化与 Zotero UI 有关的组件，如菜单、侧边栏、自定义列等。

所以**自定义列建议在 `onMainWindowLoad` 里注册**（并在 `shutdown` /
`onMainWindowUnload` 里注销）。多窗口时这是必须的。

---

## 3. 插件沙箱里有什么（最容易踩的一类坑）

`plugins.js` 的 `_loadScope` 给插件作用域装了这些全局：

```javascript
Object.assign(scope, {
  Zotero, ChromeWorker, IOUtils, Localization, PathUtils, Services, Worker,
  XMLSerializer,
  setTimeout, clearTimeout, setInterval, clearInterval,      // ← 挂在 scope 上！
  requestIdleCallback, cancelIdleCallback
});
```

### ⚠ 坑 4：`Zotero.setTimeout` / `Zotero.setInterval` **不存在**

**症状**：`TypeError: Zotero.setInterval is not a function` → `startup` 中断
→ 插件表现为"装上了但什么都不做"。

**根因**：那些是**沙箱全局**函数，`Zotero` 命名空间里没有。

**修法**：

```javascript
setTimeout(fn, 1000);      // ✅
setInterval(fn, 1000);     // ✅（但见坑 5）
Zotero.setTimeout(fn, 1000);   // ❌ 不存在
```

**同类不存在的还有**：`Zotero.setTimeout`、`Zotero.clearInterval`、
`Zotero.Promise.delay`（不保证有）。要用延时自己包：

```javascript
await new Promise((r) => setTimeout(r, 1000));
```

### ⚠ 坑 5：`setInterval` 创建成功但**回调从不触发**

**症状**：`taskPolling=true`（定时器对象建出来了），但心跳计数一直 `0`。

**证据**：

```json
{"taskPolling": true, "tickCount": 0, "lastTickAt": ""}
```

**根因**：这个沙箱里 `setInterval` 不可靠；**链式递归的 `setTimeout`
也不触发第二次**（实测：单次 `setTimeout(fn, 50)` 能执行，
`timerDiag.setTimeoutSelfTest="ok"`，但递归调度后 `tickCount` 卡在 1）。

**修法**：用 `Services.tm` 的主线程定时器（`Services` 是沙箱保证提供的特权 API）：

```javascript
let qi;
try { qi = ChromeUtils.generateQI(["nsITimerCallback"]); }
catch (e) { qi = function () { return this; }; }

const timer = Services.tm.newTimer({
  observe: function () { fn(); },
  QueryInterface: qi,
}, ms);                      // ms 是毫秒
```

**别用** `Services.tm.idleDispatchToMainThread(fn, ms)` —— 它的第二个参数是
"最大空闲等待"，不是延迟，语义不对。

**让定时器可观测**（否则"轮询到底跑没跑"完全靠猜）：
每次 tick 记 `tickCount` / `lastTickAt`，并写进状态文件。

### 其他沙箱注意点

- **没有** `window` / `document`（那是主窗口作用域）。需要 DOM 时用
  `Zotero.getMainWindow().document`
- **不是 Node.js 环境**：不能用 Node API
- 访问更低权限作用域的对象会遇 **Xray vision**，需要 `window.wrappedJSObject`

---

## 4. 自定义列（Item Tree Column）

### 注册

```javascript
const key = Zotero.ItemTreeManager.registerColumn({
  dataKey: "myColumn",              // 全局唯一，会被加上 pluginID 前缀
  label: "我的列",
  pluginID: MyPlugin.id,
  enabledTreeIDs: ["main"],         // 只加到主列表；["*"] = 所有列表
  flex: 1,                          // ← 见坑 7
  minWidth: 70,
  sortable: true,
  zoteroPersist: ["width", "hidden", "sortDirection"],
  dataProvider: function (item) {
    return item.key;                // 必须是**同步**返回字符串
  },
});
// 注销：Zotero.ItemTreeManager.unregisterColumn(key)
```

### ⚠ 坑 6：`dataProvider` 是**同步**的，不能在里面发网络请求

列表每行都会调用它。做法：启动时把数据拉进内存缓存，`dataProvider` 只做字典查表。

```javascript
dataProvider: function (item) {
  const w = MyPlugin.cache[item.key];   // O(1) 查表
  return w ? String(w) : "";
}
```

另外必须**批量**拉数据（一次请求拿全量），不要在 dataProvider 里逐个查 ——
几百行会把服务打爆。

### ⚠ 坑 7：列宽用 `flex`，不要用固定 `width`

**症状**：列显示出来了，但"位置不对、不像正常加进去的列"。

**根因**：Zotero 内置列绝大多数用 `flex`：

```javascript
// itemTreeColumns.js 里的真实写法
{ dataKey: "title",        flex: 4 }     // 标题
{ dataKey: "firstCreator", flex: 1 }
{ dataKey: "date",         flex: 1 }
{ dataKey: "itemType",     width: "40" } // 只有极窄列才用固定值
```

**修法**：`flex: 1, minWidth: 70`。

### ⚠ 坑 8：自定义 `renderCell` 必须复刻标准单元格结构

**症状**：列的内容显示了，但样式/对齐不对（因为丢了 Zotero 的单元格 class）。

Zotero 的标准渲染（`components/virtualized-table.js:2186`）：

```javascript
function renderCell(index, data, column, dir = null) {
  column = column || { dataKey: "" };
  if (column.renderCell) return column.renderCell(index, data, column, dir);
  let span = document.createElement('span');
  span.className = `cell ${column.className}`;    // ← class 必须含 cell
  span.textContent = data;
  if (dir) span.dir = dir;
  return span;
}
```

要自定义样式（比如按值上色）时这样写：

```javascript
renderCell: function (index, data, column, isFirstColumn, doc) {
  const doc2 = doc || Zotero.getMainWindow().document;
  const span = doc2.createElement("span");
  // column.className 对自定义列可能是 undefined，要兜底
  span.className = "cell " + ((column && column.className) || "my-col");
  span.textContent = data == null ? "" : String(data);
  if (!data) return span;          // 空值也要返回元素
  span.style.color = "#1f6feb";
  span.style.fontWeight = "600";
  return span;
}
```

顺带一个事实：Zotero 的**内置列其实没有 `className` 字段**，所以标准渲染出来
是 `class="cell undefined"` —— 你的 `"cell my-col"` 结构上等价，不是问题。

### ⚠ 坑 9：`pause` 不是 API，`dataProvider` 里不要用 `this`

`dataProvider` / `renderCell` 是普通函数回调，用闭包变量（`self`）而不是 `this`。

### 参考：jasminum 的做法

本机 jasminum 插件的"知网引用数"列**连 `renderCell` 都不提供**，
只给 `dataKey` + `label` + `pluginID` + `dataProvider`，完全走 Zotero 默认渲染。
想"最像正常列"就照这个来；需要上色时才自己写 `renderCell`（并按坑 8 的写法）。

### ⚠ 坑 10：`onMainWindowLoad` 在插件**启动时不会被调用**

官方文档建议"与窗口相关的 UI（菜单、自定义列等）放 `onMainWindowLoad`"，
但本机实测：**Zotero 启动时主窗口已经存在，那个窗口不会走这个钩子**。

证据（用任务队列在 Zotero 内查证）：

```
windowHookCalled: 0      ← onMainWindowLoad 从未被调用
manualCall: "ok"         ← 但手动调用立刻成功，按钮/菜单都注册上了
toolbarButton: MISSING   ← 所以 UI 一直不出现
```

**修法**：`startup` 里也注册一次（给已存在的窗口），`onMainWindowLoad`
保留给"启动之后新开的窗口"。窗口还没就绪时重试几次：

```javascript
const applyUI = () => {
  const win = Zotero.getMainWindow && Zotero.getMainWindow();
  if (!win) return false;
  self.registerToolbarButton(win);
  self.registerItemMenu(win);
  return true;
};
if (!applyUI()) {
  let n = 0;
  const retry = () => { if (applyUI() || ++n >= 20) return; setTimeout(retry, 500); };
  setTimeout(retry, 500);
}
```

### ⚠ 坑 11：`popupshowing` 会冒泡 —— 自己的子菜单会被自己拆掉

给条目右键菜单加**二级菜单**时，常见写法是每次 `popupshowing` 先删旧建新。
但 `popupshowing` **会冒泡**：鼠标悬停展开你自己的子菜单时，子菜单的
`popupshowing` 冒泡到父级 `zotero-itemmenu`，触发你的监听器 →
把自己的 menu 删了重建 → **子菜单刚展开就消失**（现象："二级菜单点不开"）。

```javascript
popup.addEventListener("popupshowing", (event) => {
  if (event.target !== popup) return;   // ← 必须有这一句
  …
});
```

### ⚠ 坑 12：菜单插到哪 —— 别拿别的插件当锚点

想让自己的菜单落在"插件菜单组"里，**不要**用另一个插件的菜单 id 当锚点
（那个插件一卸载/改名，位置就漂了）。

本机从 `zoteroPane.js` 的 `buildItemContextMenu()` 读到的可靠判据：

- 内置项是**隐藏而非删除**的，所以顺序固定
  （证据：源码用**索引**访问它们 —— `menu.childNodes[m.reindexItem]`）
- 内置项的 `class` 里都带 `zotero-menuitem-*` 前缀
- 它的硬编码内置列表最后一项是 `reindexItem`（「重建条目索引」）

所以：**插到"最后一个可见内置项"之后**，就恰好落在插件菜单组的最上面，
且与装了哪些插件无关。

```javascript
const isBuiltin = (el) =>
  /(^|\s)zotero-menuitem-/.test(el.getAttribute("class") || "")
  || BUILTIN_IDS.includes(el.id)
  || BUILTIN_LABELS.includes(el.getAttribute("label") || "");
let lastBuiltin = null;
for (const c of popup.children) {
  if (isBuiltin(c) && !c.hidden && c.getAttribute("collapsed") !== "true") {
    lastBuiltin = c;
  }
}
lastBuiltin.after(menu);
```

**记得把自己加的分隔符也带 id 并在重建时一起清掉**，否则每弹一次菜单就多留几个。

### ⚠ 坑 13：`ProgressWindow` 会叠加，而且它没有关闭按钮

每次提示都 `new Zotero.ProgressWindow()` → 屏幕上会叠好几个窗口
（"整理路径…"+"已开始发送…"+"已发送"）。

`progressWindow.js` 的公开 API 只有四个：
`show / close / startCloseTimer / closeAll` ——
**没有**让调用方加按钮或改样式的入口。Zotero 自己的进度窗（导入文献那个）
也没有 X，它的设计就是"点击任意处关闭"（`closeOnClick`）。

**做法**：自己管一个单例，新的显示前先关旧的；结果提示不自动消失、
文案里写明"点击本窗口任意处关闭"，再给个 60 秒兜底计时器。

```javascript
closeProgress: function () {
  if (this._progressWin) this._progressWin.close();
  this._progressWin = null;
},
newProgress: function (headline, text) {
  this.closeProgress();                                   // ← 关键
  const pw = new Zotero.ProgressWindow({ closeOnClick: true });
  pw.changeHeadline(headline); pw.addDescription(text); pw.show();
  this._progressWin = pw;
  return pw;
}
```

---

## 5. 常用 Zotero API 的正确用法

### ⚠ 坑 14：`Zotero.Items.getAll()` 是 **async**

```javascript
// items.js:122   this.getAll = async function (libraryID, onlyTopLevel, includeDeleted, asIDs)
const items = await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, true, false);
```

忘了 `await` 会拿到 Promise，接着 `for...of` 报 `items is not iterable`、
`.find` 报 `is not a function`。

### ⚠ 坑 15：集合类 API 的返回值不一定是数组

```javascript
// ✅ 用 Array.from 包一层
const raw = Zotero.Collections.getByLibrary(libraryID, true);
const all = raw ? Array.from(raw) : [];
const col = all.find((c) => c && c.name === name) || null;
```

`|| []` 挡不住"它不是数组"的情况，`.find` 会直接抛错 —— 而这个错误会让
"把文献归入分类"这一步静默失败。

### 分类与标签的写入

```javascript
const col = new Zotero.Collection();
col.libraryID = Zotero.Libraries.userLibraryID;
col.name = "新分类";
await col.saveTx();

item.addToCollection(col.id);
item.addTag("标签", 0);
await item.saveTx();
```

### 提示窗

```javascript
const pw = new Zotero.ProgressWindow({ closeOnClick: true });
pw.changeHeadline("标题");
pw.addDescription("内容");
pw.show();
pw.startCloseTimer(6000);
```

图标路径要指向**包里真实存在**的文件（对照 manifest 的 `icons` 声明）；
写错文件名会让整个通知静默失效。

### 询问框（三按钮）

```javascript
const ps = Services.prompt;
const flags = ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING
            + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_IS_STRING
            + ps.BUTTON_POS_2 * ps.BUTTON_TITLE_IS_STRING;
const choice = ps.confirmEx(Zotero.getMainWindow(), "标题", "正文", flags,
                            "按钮1", "按钮2", "跳过", null, {});
// choice: 0 / 1 / 2
```

### 设置面板

```javascript
Zotero.PreferencePanes.register({
  pluginID: MyPlugin.id,
  id: "myplugin-prefpane",
  label: "我的插件",
  rawLabel: "我的插件",
  image: MyPlugin.rootURI + "icon.svg",
  src: MyPlugin.rootURI + "settings.xhtml",     // XHTML 片段（默认按 HTML 解析）
  scripts: [MyPlugin.rootURI + "settings.js"],  // 交互脚本，避免内联被 CSP 拦
});
```

**不要**去 `document.getElementById("zotero-prefpane-advanced")` 注入 DOM ——
Zotero 7+ 的设置窗口不是那个结构，而且 `addToWindow` 之类的名字**不是**
生命周期钩子、压根不会被调用。

---

## 6. 调试：让失败可见（本文件最重要的方法论）

### ⚠ 坑 16：Zotero 的报错又笼统又会丢

- UI 安装失败永远是同一句 `可能无法与该版本的 Zotero 兼容`
- 生命周期方法找不到只 `Zotero.warn`（不是 error）
- 调试日志默认不写文件，**重启后就没了**

**所以：自己往文件里写状态。** 这是本机唯一奏效的排查手段。

### 分步记录 + 失败落盘

```javascript
function startup(params) {
  var self = MyPlugin;
  try { Zotero.MyPlugin = self; } catch (e) {}
  const steps = [];
  const step = (name, fn) => {
    try { fn(); steps.push(name + "=ok"); }
    catch (e) { steps.push(name + "=FAIL(" + e + ")"); throw new Error(name + " 失败: " + e); }
  };
  try {
    step("registerPrefPane", () => self.registerPrefPane());
    step("registerColumn",   () => self.registerColumn());
    self.writeStatus({ ok: true, steps: steps.join(" ") });
  } catch (e) {
    // 失败也写文件 —— 否则现象只是"插件不工作"，无从下手
    self.writeStatus({ ok: false, steps: steps.join(" "), error: String(e),
                       stack: (e.stack || "").slice(0, 800) });
  }
}
```

状态文件就写到你能读到的路径（如 `D:\myplugin\status.json`）。
本机这条措施直接把"插件没反应"变成了
`startTaskPolling=FAIL(TypeError: Zotero.setInterval is not a function)` —— 一句话定位。

### 拿完整调试日志

用参数启动 Zotero，把调试输出落盘：

```powershell
& "D:\Application\Zotero\zotero.exe" -ZoteroDebugText -jsconsole > debug.txt 2>&1
```

然后在里面 grep 关键行。有用的行包括：

```
Calling bootstrap method 'startup' for plugin <id> version <v> with reason APP_STARTUP
Plugin <id> is missing bootstrap method 'startup'        ← scope 是空的
Add-on <id> is not compatible with application version. add-on minVersion: … maxVersion: …
```

### 在 Zotero 里跑 JS 的坑

`工具 → 开发者 → 运行 JavaScript` 窗口里的全局 `AddonManager`**不是**
`resource://gre/modules/AddonManager.sys.mjs` 导出的那个：

```javascript
// ❌ 这个 AddonManager 上没有 getInstallForFile
AddonManager.getInstallForFile(file);

// ✅ 显式导入，拿真正的那个
const { AddonManager } = ChromeUtils.importESModule(
  "resource://gre/modules/AddonManager.sys.mjs");
await AddonManager.getInstallForFile(file, "application/x-xpinstall");
```

### ⚠ 坑 17：脚本的 IIFE 包装会让返回值传不出来

如果脚本是 `(async () => { …; return text; })();` 这种结构，
用 `new AsyncFunction(code)` 执行时返回值永远是 `undefined`
（最后一句是表达式语句）—— 现象是"任务报告成功但结果为空"。

**修法**：去掉 IIFE 包装，让文件本身就是 AsyncFunction 的函数体，
顶层的 `return text` 才能返回。

### ⚠ 坑 18：`node --check` 会误报这类脚本

这些脚本是被 `new AsyncFunction("Zotero", "Services", "ChromeUtils", code)`
执行的，**顶层 `await` / `return` 都合法**。而 `node --check` 按 CommonJS
解析，会把它们报成 `await is only valid in async functions`。

正确校验方式：

```javascript
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('Zotero', 'Services', 'ChromeUtils', code);   // 只构造，不执行 → 校验语法
```

### ⚠ 坑 18b：上面这个校验方式**自己有个盲区**（本机栽过，代价是一次 UI 白屏）

把整个文件包成 AsyncFunction 会**掩盖**一类错：

```js
async function loadModels() { await fetch(...); }   // 合法

function kbInitPane(doc) {          // ← 不是 async
  ...
  await loadModels();               // ← 运行到这里才 SyntaxError
}
```

单看整个文件，`await` 在 async 函数里，**整体构造能通过**。
但 `kbInitPane` 自己在运行时遇到 `await` 就抛 `SyntaxError` ——
整块 UI **直接不初始化**，表现是「设置面板点进去空白 / 没反应」，
而静态检查全报"语法正确"。

**本机的实际后果**：`settings.js` 里给 `kbInitPane` 加了 `await` 却没加
`async`，用户看到的就是"设置里点击文献知识库没有反应"。

**怎么补这个盲区**（已实现在 `tools/check_js_syntax.py`）：
在整体构造之外，再逐个检查"非 async 的 `function` 声明里有没有裸 `await`"。
三个判定要点，缺一个就会误报或漏报：

1. 必须**先剪掉嵌套的 async 函数体**（`async function` 与 `async (...) => {...}`）
   再找 `await` —— 不剪的话会把内层的 await 误报成外层的（第一版就误报了）。
2. 剪的时候按**花括号配平**定位函数体，不能靠正则匹配到最近的 `}`。
3. 注释也要去掉，免得注释里提到 `await` 就报错。

> 教训：**"语法检查通过"不等于"代码能跑"** —— 检查工具本身也会有盲区，
> 尤其是它为了适配运行方式做了包装的时候。
> 写完检查工具，**必须拿一个故意写错的文件验证它真能抓到**。

### ⚠ 坑 24：调用了不存在的方法 —— `if (obj.method)` 这个"防护"会静默吞掉一切

**本机栽过两次，两次都是"改了代码但界面毫无变化、且没有任何报错"。**

```js
// ✗ 看起来是防御性写法，实际是"永远不执行"
try {
  if (Zotero.ItemTreeManager && Zotero.ItemTreeManager.refresh) {
    Zotero.ItemTreeManager.refresh();       // ← 这个方法根本不存在
  }
} catch (e) { /* ignore */ }
```

`Zotero.ItemTreeManager` 的真实成员（读 `xpcom/pluginAPI/itemTreeManager.js`）：

```
registerColumn / registerColumns / unregisterColumn / unregisterColumns /
refreshColumns / getCustomColumns / isCustomColumn / getCustomCellData
```

**没有 `refresh`**。所以那个 `if` 永远为假 → 列表从来没重画过 →
用户"标了重点但权重列的星号不出现"，看起来像数据没写进去，
实际是**界面没刷新**。而 `catch {}` 让这件事连个日志都没有。

**怎么找到的**：用户报"权重没变"，我先确认服务端写成功（`/weight` 返回
权重 2.386），再顺着"写完要刷新界面"这条线查插件的刷新调用，
最后去 `omni.ja` 列了 `ItemTreeManager` 的真实成员 —— 一眼就看到没有 `refresh`。

**正确做法**：

- **让列表重画**用虚拟化表格自己的入口（Zotero 内部就是这么干的，见 `itemTree.js`）：
  ```js
  const pane = Zotero.getActiveZoteroPane() || Zotero.getMainWindow().ZoteroPane;
  pane.itemsView.tree.invalidate();                 // 重画可见行，不重查库
  pane.itemsView.refreshAndMaintainSelection();     // 更彻底（async）
  Zotero.ItemTreeManager.refreshColumns();          // 列定义也刷一下
  ```
- **写代码时先确认 API 存在**：插件的 API 面全在 `omni.ja` 的
  `chrome/content/zotero/xpcom/pluginAPI/` 下，`unar`/`python zipfile` 都能读。
  花 30 秒列一下成员，比猜半小时划算。
- **别用 `if (obj.method)` 当"兼容性防护"** —— 它把"方法不存在"
  这件需要你知道的事，变成了"功能静默失效"。要么直接调用（让它抛），
  要么在 else 分支里 `Zotero.debug` 说清楚缺什么。

> 同一轮里还犯了第三次同类错：诊断脚本里用了
> `Zotero.PreferencePanes.getScope(id)` —— 那个对象只有
> `register / unregister / pluginPanes / builtInPanes`，**没有 getScope**。
> 教训一致：**写 API 调用前先去源码确认名字**。

### ⚠ 坑 26：`Zotero.Prefs` 会自动补前缀 —— 而面板绑定**不补**（两条通道规则相反）

**本机最贵的一个 bug**：所有 pref 键都写成完整前缀，导致插件**读不到自己的配置**。

```js
// 我写的（错）—— 以为要写完整键
Zotero.Prefs.set("extensions.zotero-kb.token", "abc");
// 实际写的键是：
//   extensions.zotero. + extensions.zotero-kb.token
//   = extensions.zotero.extensions.zotero-kb.token   ← 双前缀
```

**依据**（`omni.ja` 里两行代码）：

```js
// chrome/content/zotero/config.mjs
PREF_BRANCH = 'extensions.zotero.'

// chrome/content/zotero/xpcom/prefs.js
pref = global ? pref : ZOTERO_CONFIG.PREF_BRANCH + pref;   // get 和 set 都补
```

**而偏好面板的 `data-pref` / `preference` 属性是原样用的**，不补前缀。
对照本机能正常工作的插件即可印证 —— 它们的 `preference=` 写的都是完整键：

```
pdf2zh:    preference="extensions.zotero.pdf2zh.sourceLang"
jasminum:  preference="extensions.zotero.jasminum.translatorSource"
```

**所以正确分工是**：

| 位置 | 该写什么 | 最终键 |
|---|---|---|
| JS：`Zotero.Prefs.get/set` | **相对键** `myplugin.token` | `extensions.zotero.myplugin.token` |
| 面板：`data-pref` / `preference` | **完整键** `extensions.zotero.myplugin.token` | 同上 |
| `prefs.js` 默认值文件 | **完整键**（Zotero 按字面注册） | 同上 |

**本机的连锁后果**（一次解释掉四个看起来无关的现象）：

| 现象 | 真实链路 |
|---|---|
| 设置里模型下拉是空的 | `token` 读成空 → 需要认证的接口全 **401** |
| 权重列一直是空的 | 同一个 token 问题 → `/weights` 401 |
| 版本号显示「—」 | 另一个 pref 读成空 → 走了 catch 分支 |
| 点了按钮"弹窗说改了" | 写请求在**服务端**确实成功了（弹窗没说谎） |

**排查方法**：直接打开 profile 的 `prefs.js`，搜你的插件名。
**键名里前缀出现两次**就是这个问题。

**修的时候要连带做的两件事**：

1. **迁移已存的配置** —— 用户之前存的值都在错键上，不迁就得重配。
   写个一次性脚本搬过去（并备份 `prefs.js`）。**只在 Zotero 没运行时做**，
   否则它退出时会把内存里的 prefs 写回、覆盖你的修改。
2. **给"保存"按钮加读回确认** —— 键名写错时界面上看不出任何异常，
   存完立刻 `Zotero.Prefs.get` 读回、和输入框比对，不一致就报出来。

> 教训：**读第三方框架的 API 时，"自动加前缀"这类隐形约定必须去源码确认**。
> 而且"两条通道规则相反"这种事，光看文档看不出来 ——
> 要看**实际写进 `prefs.js` 的键名**。

### ⚠ 坑 27：面板出问题却看不到任何信息 —— 给它写落盘日志

Zotero 的设置面板跑在 `Cu.Sandbox` 里：

- `Zotero.debug` 的输出平时看不到（要开 `-ZoteroDebugText`）
- `try/catch` 一包，出错就彻底静默
- 用户能说的只有"没生效"，你只能猜

**做法**：在面板的初始化流程里插一个写文件的日志函数：

```js
function kbDiag(line) {
  try {
    const KB = Zotero.ZoteroKB;
    if (!KB || !KB.kbDir || !KB.kbDir()) return;
    const p = KB.kbDir() + "\\settings-init.log";
    const f = Zotero.File.pathToFile(p);
    const old = f.exists() ? Zotero.File.getContents(f) : "";
    Zotero.File.putContents(f, old + line + "\n");
  } catch (e) { /* ignore */ }
}
// 每次初始化先覆盖写一次头部，之后逐条 append
// → "日志只有前几行"本身就说明卡在哪一步
```

在**每个关键步骤**前后各打一条（进函数、拿到元素没、请求发出去、返回是什么、
结果几个）。用户打开一次设置面板，你读文件就知道答案，不用再来回猜。

> 本项目为此来回猜了**两轮**（用户报两次"模型出不来"）才想到加日志。
> 面板类 UI 的调试，**第一轮就该把日志埋好**。

### ⚠ 坑 30：配置里记的路径**不能无条件信任** —— 用 `os.path.samefile` 认"是不是本项目的"

**场景**：插件/服务会把"项目根目录""解释器路径"记进配置文件或运行时状态。
这些文件会**跟着项目走**（复制目录、打成压缩包发人、git clone），
于是新目录里的代码拿着**旧目录**的路径去用。

**症状**：明明在新目录里装好了环境，代码却去用旧目录的解释器；
或者"配置里改了但没用"。

**错误的第一版判据**：`os.path.isdir(cand)` + "像不像一个项目"
（有 `offline/`、`online/`、`.venv/`）。
问题：那个旧目录**确实存在**（它是另一份拷贝），所以判据通过 ——
拦不住。本机在沙箱测试里就复现了：把代码复制到临时目录跑，
识别出的仍是 `D:\DSHplugins\zotero-kb`。

**正确判据**：问"这个目录里的**同一个文件**是不是我正在跑的那个"。

```python
def _looks_like_this_project(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    if not all(os.path.isdir(os.path.join(path, d))
               for d in ("offline", "online")):
        return False
    me = os.path.abspath(__file__)                      # 正在跑的模块
    there = os.path.join(path, "offline", "schemas.py")
    try:
        return os.path.samefile(me, there)               # ← 关键
    except OSError:
        return os.path.normcase(os.path.abspath(there)) == os.path.normcase(me)
```

`os.path.samefile` 比路径比较靠谱：能处理软链接、`..`、盘符大小写。

**优先级怎么排**（这样既拦陈旧值，又不妨碍正当用法）：

| 级别 | 来源 | 要不要验证 |
|---|---|---|
| 1 | 调用方显式传参 / 环境变量 | ❌ 这是**本次明确要求** |
| 2 | 用户在设置面板里选的（配置文件里的显式字段） | ❌ 这是**用户意图** |
| 3 | 自动探测（项目目录下的 `.venv` 等） | — 天然是本项目的 |
| 4 | 运行时状态缓存（程序自己写的） | ✅ **最可能陈旧**，要验证 |

⚠ 注意第 2 级和第 4 级的区别：**用户手动选的该尊重，
程序自己缓存的要怀疑**。第一版把两者一视同仁地"验证"，结果把
"用户特意把 venv 装在别处"这种正当用法也挡掉了（测试抓到的）。

### ⚠ 坑 31：`.gitignore` 里的文件**也在工作目录里** —— 离线审计器会误报

审计"有没有写死本机路径"时，`bundle/cordis.patch.yml`、`kb-location.json`
这类文件会被报出来。但它们**本来就不该进版本库**（已经在 `.gitignore` 里），
里面的本机路径是**设计如此**（按本机生成）。

**做法**：审计器解析 `.gitignore`，对匹配到的文件改判为"生成物，不算问题"，
并单独列一行说明。否则每次审计都有一堆噪音，久了就没人看了。

同理，检查"脚本里不能有非 ASCII"时要**排除注释行**
（`REM xxx` 里的中文不参与执行）—— 第一版没排除，误报过一次。

### ⚠ 坑 29：设置面板的 document **不是**主窗口的 —— 用错了整个初始化都不跑

**本机为此白折腾了三轮**（改 pref 键名、改 createElement、加日志），
因为改的那些代码**从来没被执行过**。

```js
// ✗ 错：主窗口的 document 里没有你的面板
const w = Zotero.getMainWindow();
kbInitPane(w.document);
```

Zotero 把面板的 XHTML 插进的是**设置窗口**（`zotero:pref`）的 document ——
和主窗口是**两个不同的 document**。所以
`doc.getElementById("zotero-kb-settings")` 永远返回 null，
你的 `kbInitPane` 第二行就 `return`，**什么都没做**。

表现：设置面板看起来"部分正常"（XHTML 静态内容都在、复选框能勾），
但所有 JS 驱动的部分都不动 —— 下拉是空的、版本号是占位符。

**正确做法**：`settings.js` 是 Zotero 在**设置窗口的沙箱**里加载的
（`preferences.js` 里 `new Cu.Sandbox(window, {...})` +
`Services.scriptloader.loadSubScript(script, pane.scope)`），
所以**这个文件里的全局 `document` 就是设置窗口的 document**：

```js
function kbFindPaneDoc() {
  try {                                   // 1) 沙箱全局 document
    if (typeof document !== "undefined" && document
        && document.getElementById("your-pane-root")) {
      return { doc: document, how: "沙箱全局 document" };
    }
  } catch (e) {}
  try {                                   // 2) 枚举设置窗口兜底
    const en = Services.wm.getEnumerator("zotero:pref");
    while (en.hasMoreElements()) {
      const w = en.getNext();
      if (w && w.document
          && w.document.getElementById("your-pane-root")) {
        return { doc: w.document, how: "Services.wm" };
      }
    }
  } catch (e) {}
  return { doc: null, how: "没找到" };
}
// 面板可能还没插好 —— 重试几次（300ms 起、每次翻倍）
```

> **教训（比这个 bug 本身更重要）**：
> 用户报"某块 UI 完全没反应"时，**第一件事是确认"初始化代码到底跑了没"**，
> 而不是直接去改那块 UI 的逻辑。
> 本项目就是反过来做的 —— 看到"下拉是空的"就去改 option 的创建方式、
> 看到"版本号是横线"就去改版本号的读法，**两轮都在改从未执行的代码**。
> 判断"跑了没"只要一行日志：
> `kbDiag("kbInitPane: root=" + !!root)`。

### ⚠ 坑 28：`ItemTreeManager` 没有 `refresh()` —— 刷列表要用 `tree.invalidate()`

```js
// ✗ 这个方法不存在，而 if 判断会让它静默失效
if (Zotero.ItemTreeManager && Zotero.ItemTreeManager.refresh) {
  Zotero.ItemTreeManager.refresh();
}
```

`Zotero.ItemTreeManager` 的真实成员（`xpcom/pluginAPI/itemTreeManager.js`）：

```
registerColumn / registerColumns / unregisterColumn / unregisterColumns /
refreshColumns / getCustomColumns / isCustomColumn / getCustomCellData
```

**重画列表的正确入口是虚拟化表格自己**（Zotero 内部也这么干，见 `itemTree.js`）：

```js
const pane = Zotero.getActiveZoteroPane() || Zotero.getMainWindow().ZoteroPane;
pane.itemsView.tree.invalidate();                 // 重画可见行，不重查库
pane.itemsView.refreshAndMaintainSelection();     // 更彻底（async）
Zotero.ItemTreeManager.refreshColumns();          // 列定义也刷一下
```

⚠ 这个坑和坑 24 是同一个病根：**凭印象写 API 名字**。
**写之前花 30 秒去 `omni.ja` 列一下成员**，比猜半小时划算。

### ⚠ 坑 25：Zotero 运行中替换 profile 里的 xpi **不会生效**

调试时为了省事，直接
`Copy-Item 新包 → profile\extensions\<id>.xpi`。
**Zotero 开着的时候这么干是无效的** —— 它已经把插件读进内存了，
不会因为你换了磁盘上的文件就重新加载。

症状很有欺骗性：**一部分改动看起来生效了、另一部分没有**，
让人以为是代码 bug，其实是新旧代码混在一起跑。

**判断方法（放在诊断脚本最前面）**：把"Zotero 实际加载的版本"
（`AddonManager.getAddonByID(id).version`）和"磁盘上 xpi 的版本"
（读 zip 里的 manifest.json）并排打出来。不一致就是没重启。

```js
const a = await AddonManager.getAddonByID("your-plugin@id");
// 磁盘上的：
const { Subprocess } = ChromeUtils.importESModule(
  "resource://gre/modules/Subprocess.sys.mjs");
const p = await Subprocess.call({
  command: "C:\\Windows\\System32\\tar.exe",       // Win10+ 自带，能读 zip
  arguments: ["-xOf", xpiPath, "manifest.json"],
  stdout: "pipe", stderr: "ignore",
});
```

**结论**：`Zotero 没在运行时`才能直接替换 xpi（启动时会重新扫描）；
`Zotero 开着时`必须走 UI 安装 + **完全退出重启**（关窗口不算）。

### 自动化：任务队列（可选，但很好用）

想省掉"手动复制粘贴到运行 JavaScript 窗口"，可以让插件轮询一个本地服务：
Python 派任务 → 插件领取并在 Zotero 内执行 → 结果回传。

要点：
- 服务只 bind `127.0.0.1`，token 认证
- 插件侧用 `setInterval` 不可靠（坑 5），要用 `Services.tm`
- 执行用 `new AsyncFunction("Zotero","Services","ChromeUtils", task.code)`
- 安全：`/health` 里回 token 时要检查 `Origin` 头（浏览器才发），
  带 `Origin` 的请求不给 token

---

## 7. 打包与安装

```python
import zipfile, json
with zipfile.ZipFile("myplugin.xpi", "w", zipfile.ZIP_DEFLATED) as z:
    info = zipfile.ZipInfo("manifest.json", date_time=(2026,1,1,0,0,0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3                    # Unix（与可用插件一致）
    info.external_attr = 0o644 << 16
    z.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2))
    for f in ("bootstrap.js", "prefs.js", "icon.svg"):
        z.write(f"src/{f}", f)
```

- `manifest.json` 和 `bootstrap.js` 必须在 zip **根目录**（不要套一层文件夹）
- 条目属性对齐可用插件：`create_system=3`、`external_attr=0o644<<16`

**安装**：`工具 → 插件 → 齿轮 → Install Plugin From File…` 选 xpi。

> 侧载（往 profile 的 `extensions\` 放文件）在 Zotero 10 上**不可靠**：
> `XPIProvider.readAddons()` 会把**文件名等于插件 id** 的文件当作"指针文件"
> 读内容，内容不是有效目录路径就**直接删掉**；而放真 xpi 也常被清理。
> 还是走 UI 安装最稳。
>
> 例外：**Zotero 没在运行时**，直接替换
> `profile\extensions\<id>.xpi` 是可行的（启动时会重新扫描并更新
> `extensions.json`）—— 用户已经退出的情况下用它省事。Zotero 开着时无效。

### ⚠ 坑 22：`preferences.xhtml` 不能带 `<?xml?>` 声明 —— 面板点进去一片空白

**最难查的一类**：设置窗口左侧能看到你的面板项，点进去**一片空白、无报错**。

根因在 Zotero 的加载方式（`preferences.js` 的 `_parseXHTMLToFragment`）：

```js
parser.parseFromSafeString(
  `<div xmlns="http://www.w3.org/1999/xhtml" xmlns:xul="...">${文件内容}</div>`,
  "application/xml");
```

它把**文件内容当字符串嵌进一个 `<div>`** 再解析。而 XML 声明只允许出现在
文档**最开头**，嵌进 `<div>` 内部就是非法 XML → `parsererror` → 面板空白。

**定位方法（值得照抄）**：把本机能正常显示设置面板的插件全拆开对比
`preferences.xhtml` 的开头几行。实测 4 个正常插件**全部以 `<linkset>` 开头、
没有声明**；唯一差异就是这一行。

**修法**：删掉声明（注释里可以写"为什么不能加"）。
**回归**：把上面那段解析逻辑原样复刻成静态检查（把内容嵌进 `<div>` 再
`ET.fromstring`），并接进打包流程 —— 这种错误静态看不出，只能专门回归。

> ⚠ 写这个检查器时**必须先剔除 XML 注释**：注释里举反例
> （`<?xml version="1.0"?>` 当反面教材）会让检查器误报。

### ⚠ 坑 23：`PreferencePanes.register()` 是 async，不接返回值会静默失败

```js
Zotero.PreferencePanes.register({...});          // ← 注册失败你永远不知道
```

返回的是 Promise（成功 resolve 面板 id，失败 reject）。不接住的话，
"面板没出现"这件事**没有任何提示**。正确写法：

```js
const p = Zotero.PreferencePanes.register({
  pluginID: id, id: "my-prefpane", label: "…",
  src: rootURI + "prefs.xhtml", scripts: [rootURI + "prefs.js"],
});
if (p && p.then) {
  p.then((pid) => Zotero.debug("pane ok " + pid))
   .catch((e) => {
     const m = String(e && e.message || e);
     if (m.includes("already registered")) return;   // 重载时正常
     Zotero.logError(new Error("注册设置面板失败：" + m));
   });
}
```

另外：参数名是 **`label`，没有 `rawLabel`**（源码里
`rawLabel: options.label || await getName(...)`）。传 `rawLabel` 无效。

---

## 7.5 发布前检查清单（给别人用的插件）

这五条每一条都**真的踩过**，而且都是"自己机器上完全正常、别人装上就出问题"
那一类。发版前逐条过一遍。

### ① 包内不能有写死的绝对路径

```js
// ✗ 别人装上就指向不存在的目录
const sys32 = "C:\\Windows\\System32\\taskkill.exe";
const kb = "D:\\MyProject\\kb";
// ✓ 向系统/环境问，或用用户配置
Services.dirsvc.get("SysD", Components.interfaces.nsIFile).path
Services.dirsvc.get("LocalAppData", Components.interfaces.nsIFile).path
Services.env.get("ZOTERO_KB_ROOT")
Zotero.Prefs.get("extensions.myplugin.dataDir")
```

**自动化**：写个脚本扫打包产物（不是源码），并接进打包流程。
判定要区分"写死"和"合理动态取值"：

| 算写死 ✗ | 不算 ✓ |
|---|---|
| 开发机目录名、具体用户名 | `Services.dirsvc.get(...)`、`PathUtils` |
| 完整绝对路径（盘符+两级以上） | `%LOCALAPPDATA%` 之类环境变量 |
| | `C:\Users\Public`（系统常量，不是某个用户的目录） |
| | 注释里的举例 |

> 还要**扫源码里的注释与文档**：示例路径写了开发者自己的目录同样该改
> （比如 placeholder 写 `D:\Nutstore\...`，别人根本不知道那是什么）。

### ② 不透露隐私

- **API key / token 绝不进包**。配置文件（`llm-config.json`、
  `*-token.txt`、`*-key.txt`）要进 `.gitignore`。
- 服务端返回配置时**不回传 key 明文**：只回 `has_key` / `key_tail`
  （连自己写的管理面板也要这样，因为响应可能被抓包/贴出来）。
- 日志与截图里出现 token 时**打码**（`tok[:8] + "…"`）。
- 分享"排错用的状态文件"前先看一眼里面有没有 token
  （本机就发生过：`plugin-status.json` 里带着完整 token）。
- 用户目录路径（`C:\Users\<真名>\`）在文档和示例里一律用占位符。

### ③ 方便安装

| 原则 | 做法 |
|---|---|
| 用户不该装运行时 | 优先复用系统已有能力；非要装就提供**一条命令**的脚本 |
| 环境位置自动探测 | 按"用户指定 > 环境变量 > 配置文件 > 探测 > 兜底"的优先级链 |
| 探测不到要**说清找的是哪** | ✗"找不到 Python" ✓"找的是 `D:\...\python.exe`，不存在；请在设置里选" |
| 给"点选"而不是"手打路径" | `nsIFilePicker`：`fp.init(win.browsingContext, title, modeGetFolder)`<br>⚠ 第一个参数要 **BrowsingContext** 不是 window |
| 三个独立位置别互相推算 | 数据在哪、代码在哪、解释器在哪**是三个东西**；<br>从数据位置反推代码位置是本机踩过最久的坑 |

### ④ 给人看的要方便、美观

- **按钮名用用户的话，不用实现的话**：「更新索引（增量）」→「手动更新」。
- **每个按钮挂一句话说明**（Tk/Qt 没内置 tooltip 就自己写一个）。
- **字体要统一**：别混着 `("", 9)` 和 `("Consolas", 10)` ——
  空字体名 = 拿系统默认字体再改字号，中文字距会跟别处不一样，
  同屏两种字体一眼就看出"没做完"。统一成"界面字体 / 等宽字体"两个常量。
- **别用 markdown 语法填 Tk/Qt 控件**：`**加粗**` 会原样显示成星号
  （本项目犯了两次，第二次才改成统一剥掉）。
- **文本区域配可滚动**，别让内容被挤成一条线（pack/grid 的顺序有讲究）。
- **长列表别塞进下拉框**：长短不一的文本在 Combobox 里既对不齐也改不了宽度。
  用**独立弹窗 + 表格**（列宽可拖、表头可排序、窗口可拉大）。
- **状态类输出的去处要和点击处一致**：在 A 页点的按钮，结果写到 B 页
  等于"没反应"（本项目出过：运行环境页点「服务状态」，结果显示在高级页）。

### ⑤ 出错时要说清"我找的是什么、你该改哪里"

```js
// ✗ 用户看完不知道该动什么
this.notify("找不到 Python 环境");

// ✓ 三个信息：找的路径、判断依据、下一步动作
this.notify("找不到 Python 环境",
  "我找的是：" + py + "\n项目目录：" + root
  + "\n\n请在 设置 → 文献知识库 → 运行环境 里点「浏览…」选到 "
  + ".venv\\Scripts\\pythonw.exe。");
```

同理，**别把异常吞掉**：`catch (e) {}` 会让界面上显示一个**错误的结论**
（本项目出过：`NumberError` 被吞掉，界面显示"服务没在跑"，而服务其实好好地）。
至少要 `Zotero.debug` 一行，能到用户眼前的就写进可见区域。

---

## 8. 快速排查清单

遇到问题按这个顺序查：

1. **插件装不上** → manifest 有 `update_url` 吗？是有效 https URL 吗？（坑 1）
2. **装上了没反应** → 完全重启 Zotero 了吗？（坑 3）
3. **还是没反应** → 状态文件里 `startupSteps` 哪一步 FAIL？（第 6 节）
4. **`missing bootstrap method`** → `startup` 是**顶层函数声明**吗？（第 2 节）
5. **`Zotero.xxx is not a function`** → 是不是把沙箱全局当成了 Zotero 成员？（坑 4）
6. **定时器不触发** → 换 `Services.tm.newTimer`（坑 5）
7. **列不像正常列** → 宽度用 `flex`；`renderCell` 加 `cell` class（坑 7、8）
8. **列没数据** → `dataProvider` 是同步的，数据要先缓存（坑 6）；
   API 要 `await`（坑 10）
9. **token/认证类接口 401** → 探活接口（常不需要 token）通了不代表认证接口通
10. **设置里点自己的面板项一片空白** → `preferences.xhtml` 头部有
    `<?xml?>` 声明吗？（坑 22）
11. **面板根本不出现** → `register()` 的 Promise 接了吗？参数是不是写成了
    `rawLabel`？（坑 23）
12. 仍然无解 → 用 `-ZoteroDebugText` 抓完整日志，并去 `app\omni.ja` 读 Zotero 源码

**发版前**再走一遍第 7.5 节的五条（绝对路径 / 隐私 / 安装 / 美观 / 报错质量）。

---

## 9. 附：与 DSH 协作（跨进程集成）

当需求是"把 Zotero 的东西送进 DSH 对话"时，**必须拆成两个插件** ——
两边各自掌握对方拿不到的东西：

| 侧 | 只有它能做 | 它做不到 |
|---|---|---|
| Zotero 插件 | 读条目/PDF/标注、弹窗、写回 | 调 DSH 的 `sessionController`（进程内服务） |
| DSH 插件 | 调 `sessionController`、`agents` 等进程内服务 | 读 Zotero 库 |

### ⚠ 坑 19：desktop profile 里**没有** `webServer` 服务

最初设计是"DSH 插件注册一个 HTTP 路由给 Zotero 投递"，写成：

```javascript
export const inject = { optional: ['webServer'] };
```

结果插件**永远不激活**，DSH 启动时报：

```
dsh: warning: 1 entry did not activate
zotero-bridge (dsh-bundle-zotero-bridge): pending (waiting for service: optional)
```

结论：Inspect 的 Service 目录能查到 `webServer` 的类型定义（那是全平台目录），
但**这个组合里没有它的实例**。

### ⚠ 坑 20：DSH 的 `/api` 是"受信任 + 已认证"专用通道

想直接 HTTP 调 `/api` 也不行。`connection` 的类型定义写着：

```typescript
createSharedFetchHandler(channel: '/api'): ConnectionFetchHandler
// 描述：Fetch handler for trusted, authenticated requests.
```

实测：`/api` 所有路径（连首页 `/`）都 **401**；
`Authorization: Bearer <secret>` / 自定义头 / query 参数**都不通**
（secret 取自 `~/.dsh/.credentials.yaml` 的 `client-connection/browser-session`）。

### ✅ 可行做法：文件信箱 + 定时轮询

不依赖任何 DSH 服务，只用 Node 内置 `fs`：

```
~/.dsh/<plugin>/
  requests/<id>.json    ← 外部程序写（先写 .tmp 再 rename，保证原子）
  results/<id>.json     ← 插件写结果，外部程序读它拿反馈
  done/                 ← 处理完挪过来，便于排查
```

```javascript
export const inject = [];       // 关键：不声明服务依赖，就不会 pending

export function apply(ctx, config = {}) {
  const timer = setInterval(async () => {
    for (const n of fs.readdirSync(reqDir)) {
      if (!n.endsWith('.json')) continue;
      const req = JSON.parse(fs.readFileSync(path.join(reqDir, n), 'utf8'));
      const result = await handle(ctx, req);        // 里面调 sessionController
      writeJsonAtomic(path.join(resDir, req.id + '.json'), result);
      fs.renameSync(path.join(reqDir, n), path.join(doneDir, n));
    }
  }, 1500);
  ctx.on?.('dispose', () => clearInterval(timer));
}
```

**好处**：不依赖任何服务（不会 pending）、不占端口（无冲突/防火墙问题）、
请求与结果都是文件（可离线排查）。**代价**：1.5 秒轮询延迟，可以接受。

### DSH 侧插件的安装方式

和知识库 MCP 一样用 **bundle**：

```
D:\DSHplugins\<name>\bundle\
  package.json          { name, type: 'module', main: 'index.js',
                          dsh: { bundle: { patch: 'cordis.patch.yml' } } }
  cordis.patch.yml      - insert: [ { id, name, config } ]
  index.js              export const name / inject / apply
```

```powershell
dsh plugin --profile <profile> add "link:D:/DSHplugins/<name>/bundle"
```

`patchReload: live` 只重载**已加载**插件的配置；**新增**的 bundle 需要**重启 DSH**。

### 会话相关 API（DSH 进程内，从 Inspect 查到）

```javascript
ctx.get('sessionController')
  .list(request)      // 列对话
  .create(request)    // 新建对话
  .prompt(request, signal)   // ← 往指定对话发消息
  .fork / .rename / .cancel / .page
```

另有 `ctx.get('sessions')`（内存态 `create/get/list/fork`）、
`ctx.get('agents')`（`create/get/list/roots`）。

**参数形状不确定时的做法**：DSH 的包都打包在 `app.asar` 里，拿不到 `.d.ts`。
**别猜** —— 写个"依次试几种形状、把结果与报错一次打全"的探测脚本，
把失败项的 `error.message` 一起返回。

### ⚠ 坑 21：不要用启动类命令做"只读探查"

排查时我跑 `dsh rescue --from-default-profile web`，想"拿个临时 profile 做配置合成实验"。
结果它**直接启动了一个 web 实例**：抢占端口、自动打开浏览器；
更糟的是我随后把它当成"自己起的临时进程"给 kill 了 ——
**那其实是用户正在用的 DSH host**，导致 DSH 崩溃弹框。

**教训**：
- `rescue` / 任何启动类命令都有副作用，不能当探测器用
- 看到不认识的 PID，**先查清归属再动手**（`Get-CimInstance Win32_Process` 看完整命令行与父进程）
- 隔离实验用 `--dump-config` 这类纯读取入口

---

## 10. 一句话总结

**Zotero 插件的坑大多来自三处**：
① manifest 的隐形必需字段（`update_url`）；
② 沙箱与 `Zotero` 命名空间的边界（`setTimeout`/`Items.getAll`）；
③ 失败被静默吞掉（`warn` + 无日志 + `this` 陷阱）。

所以最有效的两个习惯：**读 Zotero 源码**（`app\omni.ja` 就是 zip），
以及**把每一步结果写进文件**（别指望 Zotero 的报错）。
