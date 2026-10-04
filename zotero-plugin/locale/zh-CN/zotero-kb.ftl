# zotero-kb 插件的界面文案（简体中文）。
#
# Zotero 会自动读插件根目录下的 `locale/<语言>/*.ftl`（pluginAPI 里的
# `readDirectory(rootURI, 'locale', true)` → 注册成 `zotero-plugins:{locale}/`），
# 不需要在 JS 里做任何注册。
#
# ⚠ 为什么这件事不能省：`Zotero.ItemPaneManager.registerSection` 的
#   `header` / `sidenav` 里 `l10nID` 是**必填**字段，而且它指向的 id 必须
#   在这个文件里存在 —— 少一条的症状是"内容窗格里分区标题空白"，没有任何报错。
#   tools/check_plugin.py 有一条检查专门盯它（bootstrap.js 里出现的每个
#   l10nID 都必须在这里定义）。
#
# 用法（JS 侧）：
#   Zotero.ItemPaneManager.registerSection({ ... header: { l10nID: 'zotero-kb-pane-header', ...} });
#   document.l10n.setAttributes(el, 'zotero-kb-status-inject', { chars: 1234 });

# ---- 内容窗格分区
zotero-kb-pane-header = 本地模型
zotero-kb-pane-sidenav = 本地模型（读这篇文献）

# ---- 按钮：三种上下文注入
zotero-kb-btn-inject-tldr = 注入摘要级
zotero-kb-btn-inject-tldr-tip = 把这篇的摘要级视图放进上下文（几百字，够问主题与方法）
zotero-kb-btn-inject-full = 注入全文级
zotero-kb-btn-inject-full-tip = 把全文放进上下文；超预算时按页均匀取样，注入量会如实显示

# ---- 按钮：逐段检测
zotero-kb-btn-para = 全文级段落检测
zotero-kb-btn-para-tip = 逐段核对正文提取质量（可随时中断，进度会保留）
zotero-kb-btn-para-next = 继续下一段检测
zotero-kb-btn-para-exit = 退出逐段检测
zotero-kb-btn-para-recheck = 重新检查这一段
zotero-kb-para-include-fixed = 连已修复的段也一起看
zotero-kb-para-resume = 继续上次
zotero-kb-para-from-here = 从这一段开始
zotero-kb-para-restart = 从头重来
zotero-kb-para-stale = 正文重建过，{ $n } 段需重查

# ---- 输入与状态
zotero-kb-btn-send = 发送
zotero-kb-btn-locate = 定位
zotero-kb-btn-propose = 整理这次讨论
zotero-kb-btn-clear = 清空对话
zotero-kb-placeholder-ask = 就这篇文献提问…
zotero-kb-placeholder-locate = 把 PDF 里选中的文字粘在这里，定位到对应段落
zotero-kb-status-inject = 已注入 { $chars } / { $full } 字符
zotero-kb-status-inject-sampled = 已注入 { $chars } / { $full } 字符（按页取样）
zotero-kb-status-para = 已查 { $checked } / { $total } 段（{ $when }）
zotero-kb-status-thinking = 正在想…（已 { $secs } 秒）
zotero-kb-signals = 客观信号
zotero-kb-model-reason = 模型理由

# ---- 写入模式（每个改动都要用户确认；二次确认里**没有**「下次不再提示」）
zotero-kb-write-title = 这段对话触发了知识库改动
zotero-kb-write-note = 确认前不会写入任何东西。确认后还会再问你一次。
zotero-kb-btn-confirm = 确认
zotero-kb-btn-cancel = 先不写
zotero-kb-confirm2-title = 再确认一次
zotero-kb-confirm2-body = 以下改动会写进知识库（经验 / 权重 / 正文修正），并立即影响检索排序。确认写入吗？

# ---- 退出提醒（可勾"下次不再提示"）
zotero-kb-quit-title = 退出前提醒
zotero-kb-quit-body = 这个窗格里的对话不会被保存，退出 Zotero 就清空了。检测进度与已确认的修正会保留。
zotero-kb-quit-dontask = 下次不再提示

# ---- 各种"用不了"的状态（都要说清下一步做什么）
zotero-kb-no-service = 本地服务没启动：先在管理面板里启动它
zotero-kb-no-kb = 这篇还没进知识库：先在管理面板建库
zotero-kb-no-model = 本地模型不可用：启动 Ollama，或在插件设置里换成 OpenAI 兼容服务
zotero-kb-no-paras = 这篇还没有全文：先在管理面板建库或补视图
zotero-kb-locate-ambiguous = 有几段都像，请点一个（不替你猜）
zotero-kb-locate-none = 没找到对得上的段落
