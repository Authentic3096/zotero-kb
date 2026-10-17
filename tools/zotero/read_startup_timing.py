"""把插件写下的**启动耗时**念出来（读 `plugin-status.json` 里那份 startupTiming）。

    python tools/zotero/read_startup_timing.py

2026-10-13 加的仪表：用户报「启动时插件加载有点慢」，而 Zotero 自己的调试日志
重启就没了、又只能在 GUI 里看 —— 所以把每一步的毫秒数写进状态文件，这里念出来。

判读要点：
  · `startupSyncMs` 是**同步阻塞** Zotero 启动的那部分（越小越好，> 300ms 就该看明细）
  · `kbviewMount` / `kbviewPageLoadMs` 是自建列那个 <browser>：建元素 + 页面加载
    （markdown-it 121KB + katex 269KB + 21 个 woff2 字体）
  · `healthCheck` / `refreshWeights` / `refreshKbCols` 是**异步**的，不阻塞启动，
    但决定"多久之后列里才有内容"
"""
from __future__ import annotations

import json
import os
import sys

KB = os.path.join(os.path.expanduser("~"), "Zotero", "zotero-kb")
STATUS = os.path.join(KB, "plugin-status.json")

# 哪些是同步阻塞启动的（按 startup() 里 step() 的顺序）
SYNC_KEYS = [
    "initLocale", "registerKbResource", "registerPrefs", "registerPrefObserver",
    "registerPrefPane", "registerNotifier", "registerWeightColumn",
    "registerKbColumns", "startTaskPolling", "scheduleOptionalGuides",
    "scheduleCapsRefresh", "scheduleKbViewColumn", "scheduleWindowUI",
    "kbviewMount", "registerToolbarButton", "registerItemMenu",
]
ASYNC_KEYS = ["healthCheck", "refreshWeights", "refreshKbCols"]


def main() -> int:
    if not os.path.exists(STATUS):
        print(f"[XX] 没有状态文件：{STATUS}")
        return 1
    with open(STATUS, encoding="utf-8") as fh:
        st = json.load(fh)

    raw = st.get("startupTiming") or ""
    if not raw:
        print("[!!] 这份状态文件里没有 startupTiming —— 装的是加仪表**之前**的版本。")
        print("     装上带仪表的那版后重启一次 Zotero 就会有了。")
        return 1
    try:
        timing = json.loads(raw)
    except Exception as exc:                      # noqa: BLE001
        print(f"[XX] startupTiming 不是合法 JSON：{exc}")
        return 1

    print(f"状态文件：{STATUS}")
    print(f"写入时间：{st.get('wroteAt', '?')}   版本：{st.get('pluginVersion', '?')}")
    print(f"启动结果：startupOk={st.get('startupOk')}  "
          f"steps={str(st.get('startupSteps'))[:80]}…")
    print()

    print("同步阻塞启动的部分（Zotero 要等它做完）")
    print("  步骤                                     ms")
    sync_total = 0.0
    for k in SYNC_KEYS:
        if k in timing:
            ms = float(timing[k])
            sync_total += ms
            flag = "  ← 慢" if ms >= 60 else ""
            print(f"  {k:38} {ms:8.1f}{flag}")
    print(f"  {'（各步相加）':38} {sync_total:8.1f}")
    for k in ("startupSyncMs",):
        if k in timing:
            print(f"  {'startupSyncMs（含 step 之外的开销）':38} {float(timing[k]):8.1f}")
    print()

    print("异步部分（不阻塞启动，决定「多久之后列里有内容」）")
    for k in ASYNC_KEYS:
        if k in timing:
            print(f"  {k:38} {float(timing[k]):8.1f}")
    for k in ("startupAsyncMs",):
        if k in timing:
            print(f"  {'startupAsyncMs（从 startup 到写完状态）':38} {float(timing[k]):8.1f}")
    print()

    page = {k: st[k] for k in st if k.startswith("kbviewPage")}
    if page:
        print("侧栏那份页面（kbview.html + vendor + 字体）")
        for k, v in page.items():
            print(f"  {k:38} {v}")
        print()

    # 一眼结论
    worst = sorted(((float(v), k) for k, v in timing.items() if k != "startupSyncMs"),
                   reverse=True)[:4]
    print("最慢的四项：" + "、".join(f"{k} {v:.0f}ms" for v, k in worst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
