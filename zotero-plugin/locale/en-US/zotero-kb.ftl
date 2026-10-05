# UI strings for the zotero-kb plugin (English).
#
# Zotero reads `<plugin root>/locale/<locale>/*.ftl` automatically (see
# `readDirectory(rootURI, 'locale', true)` in pluginAPI) and registers them as
# `zotero-plugins:{locale}/`; `initLocale()` in `01-lifecycle.js` then attaches
# the file to every window document (so `document.l10n.setAttributes` and the
# synchronous `l10nText()` both work). No JS registration is needed.
#
# ⚠ 2026-10-05: **this file is empty now (no messages)**.
#   All ~40 strings here belonged to the item-pane "local model" section, which
#   was removed on request (the pane, its seven endpoints, kbchat/paras and the
#   paragraph-review panel page are gone), so no l10nID references them anymore.
#   The two files and the loading machinery are kept so the next UI string does
#   not have to rediscover two traps:
#     · `registerSection` requires header/sidenav l10nID, and a missing id
#       shows up as a blank title with no error;
#     · a message *with a value* makes Fluent assign `textContent` and wipe the
#       element's children (that is how the section body disappeared) — use the
#       attribute form instead:
#         some-id =
#             .label = text
#
# Right now nothing in the plugin goes through ftl (settings.xhtml and the
# context menus use literals). Add new strings here and run
# tools/check_plugin.py.

# 右侧栏「知识库」分区（20-kbview.js）：**只读**展示 kb/ 里的 md。
#   ⚠ 这两条必须写成「只有名字、值留空」：registerSection 的 header/sidenav
#     l10nID 是必填，而 Zotero 会对分区调 translateFragment —— 给了值反而
#     会把节点内容抹掉（结果是标题空白 + body 拿不到，且不报错）。
# ⚠ 这两条是**属性形态**（只有 .label / .tooltiptext，值为空）：
#   Fluent 会把文案写进元素的 label / tooltiptext 属性，
#   而 collapsible-section 用 label 渲染成可见标题（值为空时标题也空）。
zotero-kb-kbview-header =
    .label = Knowledge Base Preview
zotero-kb-kbview-title = Knowledge Base Preview
zotero-kb-kbview-sidenav =
    .tooltiptext = Knowledge Base preview (KB markdown with rendered formulas)
