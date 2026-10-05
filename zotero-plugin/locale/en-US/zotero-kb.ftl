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
