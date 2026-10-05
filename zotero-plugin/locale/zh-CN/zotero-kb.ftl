# zotero-kb 插件的界面文案（简体中文）。
#
# Zotero 会自动读插件根目录下的 `locale/<语言>/*.ftl`（pluginAPI 里的
# `readDirectory(rootURI, 'locale', true)` → 注册成 `zotero-plugins:{locale}/`），
# 不需要在 JS 里做任何注册；`01-lifecycle.js` 的 `initLocale()` 再把它挂到
# 每个窗口的 document 上（这样 `document.l10n.setAttributes` 与同步的
# `l10nText()` 都能用）。
#
# ⚠ 2026-10-05：**这个文件现在是空的（一条文案都没有）**。
#   原来这里 40 多条全是"内容窗格里的本地模型分区"用的 —— 窗格、七个端点、
#   kbchat/paras、逐段复核页都按用户要求删掉了，那些 l10nID 没有再被引用。
#   保留这两个文件与上面那套挂载机制，是为了**下次加界面文案时不用重新踩坑**：
#     · `Zotero.ItemPaneManager.registerSection` 的 header/sidenav 的 l10nID
#       是必填，指向的 id 必须在这里存在，少一条的症状是"标题空白、无报错"；
#     · 有值的 message 会被 Fluent 写成 `textContent`，把元素子节点抹掉
#       （分区正文就是这样消失的）；要写属性就用下面这种形态：
#         some-id =
#             .label = 文案
#
# 现在插件里没有走 ftl 的界面文案（设置面板 `settings.xhtml` 用字面量、
# 右键菜单也是字面量）。加了新的就往这里加，并跑 tools/check_plugin.py。

# 右侧栏「知识库」分区（20-kbview.js）：**只读**展示 kb/ 里的 md。
#   ⚠ 这两条必须写成「只有名字、值留空」：registerSection 的 header/sidenav
#     l10nID 是必填，而 Zotero 会对分区调 translateFragment —— 给了值反而
#     会把节点内容抹掉（结果是标题空白 + body 拿不到，且不报错）。
# ⚠ 这两条是**属性形态**（只有 .label / .tooltiptext，值为空）：
#   Fluent 会把文案写进元素的 label / tooltiptext 属性，
#   而 collapsible-section 用 label 渲染成可见标题（值为空时标题也空）。
zotero-kb-kbview-header =
    .label = 知识库预览
zotero-kb-kbview-title = 知识库预览
zotero-kb-kbview-sidenav =
    .tooltiptext = 知识库预览（知识库的 md，公式已渲染）
