# UI strings for the zotero-kb plugin (English).
#
# Zotero reads `<plugin root>/locale/<locale>/*.ftl` automatically (see
# `readDirectory(rootURI, 'locale', true)` in pluginAPI) and registers them as
# `zotero-plugins:{locale}/`. No JS registration is needed.
#
# ⚠ Keep the ids in sync with locale/zh-CN/zotero-kb.ftl — the section's
#   `header.l10nID` / `sidenav.l10nID` are required fields, and a missing id
#   shows up as a blank pane title with no error anywhere.
#   tools/check_plugin.py fails the build when a l10nID used in bootstrap.js
#   is not defined here.

# ---- Item pane section
# ⚠ 分区标题定义成**只有属性、没有值**（.label）—— 与 Zotero 自己的 section-* 一致。
#   有值的 message 会让 Fluent 执行 textContent = 文案，把分区模板里的 <div data-type="body"> 抹掉，
#   正文就永远是空白（2026-10-05 实测，见 src/19-itempane.js 的 setupSectionHeader）。
zotero-kb-pane-header =
    .label = Local model
# 上面那条的价值只有 .label，程序化取不到；我们自己要用的文案单列一条：
zotero-kb-pane-title = Local model
zotero-kb-pane-sidenav =
    .tooltiptext = Local model (this paper)
# Attribute-only on purpose: this id lands on an icon control, and a
# message with a value would be written into its textContent.


# ---- Context injection
zotero-kb-btn-inject-tldr = Summary
zotero-kb-btn-inject-tldr-tip = Put this paper's summary-level view into the context
zotero-kb-btn-inject-full = Full text
zotero-kb-btn-inject-full-tip = Put the full text into the context; if it exceeds the budget, pages are sampled evenly and the injected size is reported
zotero-kb-btn-para = Para check
zotero-kb-btn-para-tip = Check the extracted text paragraph by paragraph (interruptible; progress is kept)
zotero-kb-btn-para-next = Check next paragraph
zotero-kb-btn-para-exit = Exit paragraph check
zotero-kb-btn-para-recheck = Re-check this paragraph
zotero-kb-para-include-fixed = Also include paragraphs already fixed
zotero-kb-para-resume = Resume last session
zotero-kb-para-from-here = Start from this paragraph
zotero-kb-para-restart = Start over
zotero-kb-para-stale = Full text was rebuilt; { $n } paragraph(s) need re-checking

# ---- Input and status
zotero-kb-btn-send = Send
zotero-kb-btn-locate = Locate
zotero-kb-btn-propose = Propose
zotero-kb-btn-clear =
    .tooltiptext = Clear chat
# ↑ 只有属性、没有值：这条 id 会被 Zotero 设在**图标控件**上，有值的 message
#   会被 Fluent 写成控件的 textContent —— 图标栏里就会竖着排一列字（2026-10-05 实测）。
zotero-kb-placeholder-ask = Ask about this paper…
zotero-kb-placeholder-locate = Paste text selected in the PDF to locate its paragraph
zotero-kb-status-inject = Injected { $chars } / { $full } characters
zotero-kb-status-inject-sampled = Injected { $chars } / { $full } characters (sampled per page)
zotero-kb-status-para = Checked { $checked } / { $total } paragraphs ({ $when })
zotero-kb-status-thinking = Thinking… ({ $secs }s)
zotero-kb-signals = Objective signals
zotero-kb-model-reason = Model reason

# ---- Write mode (every change needs confirmation; the second dialog has NO "don't ask again")
zotero-kb-write-title = This conversation proposes knowledge-base changes
zotero-kb-write-note = Nothing is written before you confirm. You will be asked a second time after that.
zotero-kb-btn-confirm = Confirm
zotero-kb-btn-cancel = Not now
zotero-kb-confirm2-title = Confirm again
zotero-kb-confirm2-body = These changes will be written to the knowledge base (experience / weights / full-text fixes) and affect search ranking immediately. Write them?

# ---- Quit reminder (with a "don't ask again" checkbox)
zotero-kb-quit-title = Before quitting
zotero-kb-quit-body = The chat in this pane is not saved and will be cleared when Zotero quits. Check progress and confirmed fixes are kept.
zotero-kb-quit-dontask = Don't ask again

# ---- Unavailable states (always say what to do next)
zotero-kb-no-service = Local service is not running: start it from the management panel
zotero-kb-no-kb = This item is not in the knowledge base yet: build it from the management panel
zotero-kb-no-model = Local model unavailable: start Ollama, or switch to an OpenAI-compatible service in the plugin settings
zotero-kb-no-paras = No full text for this item yet: build the index or refresh the views
zotero-kb-locate-ambiguous = Several paragraphs match; please pick one (not guessing)
zotero-kb-locate-none = No matching paragraph found
