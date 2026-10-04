"""审计插件源码的 API 用法：找出沙箱里不存在的东西。

    python tools/audit_plugin_api.py

审计对象是 `zotero-plugin/src/*.js`（**源码**），不是 `bootstrap.js`。
原因：bootstrap.js 是 src 拼出来的生成物，它的行号会随着源文件增删而整体漂移
—— 报 `bootstrap.js:1234` 你还得再去 src 里找那一行在哪。报
`src/08-metafill.js:123` 才是可直接下手的位置。
（内容等价性由 tools/build_bootstrap.py --check 保证，这里不需要重复扫。）

为什么需要：Zotero 的插件沙箱是个**独立的 JS 作用域**，它只装了这些全局：
    Zotero, ChromeWorker, IOUtils, Localization, PathUtils, Services, Worker,
    XMLSerializer, setTimeout, clearTimeout, setInterval, clearInterval,
    requestIdleCallback, cancelIdleCallback
（见 Zotero 源码 chrome/content/zotero/xpcom/plugins.js 的 _loadScope）

而 `Zotero` 命名空间里有的东西远少于"想当然"：
    · `Zotero.setTimeout` / `Zotero.setInterval` / `Zotero.clearInterval` —— **不存在**
      （它们是沙箱全局，不是 Zotero 的成员）
    · `Zotero.Promise.delay` —— 同样不保证存在
本机就是因为 `Zotero.setInterval` 报 TypeError，导致 startup 中断、
插件表现为"装上了但什么都不做"，排查了很久。

这个脚本静态扫出来，防止再犯。
"""

from __future__ import annotations

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "zotero-plugin", "src")


def sources() -> list[str]:
    """按 build_bootstrap 的清单顺序返回源文件；清单不在就退回文件名排序。"""
    try:
        sys.path.insert(0, HERE)
        import build_bootstrap as BB
        names = [os.path.join(SRC_DIR, n + ".js") for n in BB.SRC_ORDER]
        return [p for p in names if os.path.exists(p)]
    except Exception:  # noqa: BLE001
        return sorted(glob.glob(os.path.join(SRC_DIR, "*.js")))


# 沙箱全局里有哪些（来自 plugins.js 的 Object.assign(scope, {...})）
SANDBOX_GLOBALS = {
    "Zotero", "ChromeWorker", "IOUtils", "Localization", "PathUtils", "Services",
    "Worker", "XMLSerializer", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "requestIdleCallback", "cancelIdleCallback",
    "atob", "btoa", "Blob", "crypto", "CSS", "ChromeUtils", "DOMParser",
    "fetch", "File", "FileReader", "TextDecoder", "TextEncoder", "URL",
    "URLSearchParams", "XMLHttpRequest", "console", "Promise", "Object", "Array",
    "JSON", "Date", "Math", "String", "Number", "Boolean", "Map", "Set", "RegExp",
    "Error", "TypeError", "Symbol", "WeakMap", "Intl", "Infinity", "NaN",
}

# 确认存在的 Zotero 命名空间成员（Zotero 官方 API）
ZOTERO_KNOWN = {
    "Prefs", "Notifier", "Items", "Collections", "Collection", "Item",
    "ItemTreeManager", "PreferencePanes", "ProgressWindow", "HTTP", "File",
    "Libraries", "debug", "logError", "warn", "getMainWindow", "version",
    "Plugins", "Utilities", "Date", "sync", "DB", "Search", "FullText",
    "ItemTypes", "ItemFields", "Creators", "Tags", "Groups", "Feeds",
    "SavedSearches", "Attachments", "Annotations", "Notes", "Translators",
    "Styles", "Citations", "QuickCopy", "isMac", "isWin", "isLinux",
    "localeCompare", "platformMajorVersion", "Schema", "DB", "Promise",
    "ZoteroKB", "File", "BetterBibTeX", "RecognizeDocument", "PDF",
    # ---- v0.24.0 获取文献链路用到、且**本机 10.0.5 真机实测过**的成员。
    #      加进来是因为它们原先不在表里，每次审计都报一次"需人工确认"，
    #      而人工确认的结论就是"确实存在"（见 _task4 的探针记录）：
    #        Zotero.Translate = function，Translate.Search = function
    #        Zotero.getActiveZoteroPane = function
    "Translate", "getActiveZoteroPane",
}

# 明确**不存在**的 Zotero 成员（本机踩过或与沙箱全局重名）
ZOTERO_FORBIDDEN = {
    "setTimeout": "这是沙箱全局函数，用 setTimeout(...) 而不是 Zotero.setTimeout(...)",
    "setInterval": "这是沙箱全局函数，用 setInterval(...) 而不是 Zotero.setInterval(...)",
    "clearInterval": "这是沙箱全局函数，用 clearInterval(...)",
    "clearTimeout": "这是沙箱全局函数，用 clearTimeout(...)",
    "fetch": "这是沙箱全局函数，用 fetch(...)",
}


def main() -> int:
    print("=" * 68)
    print("插件 API 用法审计")
    print("=" * 68)
    files = sources()
    if not files:
        print(f"  [XX] 在 {SRC_DIR} 下没找到源码（bootstrap.js 现在只是生成物）")
        return 1
    print(f"  审计源码：{len(files)} 个文件"
          f"（{', '.join(os.path.basename(f) for f in files[:3])} …）")

    problems: list[str] = []

    # 1) 扫 Zotero.<成员>
    print("\n[1] Zotero 命名空间成员")
    uses: dict[str, list[str]] = {}
    bad_this: list[str] = []
    n_interval = n_timeout = 0
    for path in files:
        short = os.path.relpath(path, os.path.dirname(SRC_DIR)).replace("\\", "/")
        for i, ln in enumerate(open(path, encoding="utf-8").read().splitlines()):
            code = ln.split("//")[0]          # 去掉行内注释
            if code.strip().startswith("*"):
                continue
            for m in re.finditer(r"\bZotero\.([A-Za-z_][A-Za-z0-9_]*)", code):
                uses.setdefault(m.group(1), []).append(f"{short}:{i + 1}")
            # 2) 非箭头函数里的 this（回调里最容易错）
            if re.search(r"function\s*\(", code) and "this." in code:
                bad_this.append(f"{short}:{i + 1}  {code.strip()[:96]}")
        src = open(path, encoding="utf-8").read()
        n_interval += len(re.findall(r"(?<![.\w])setInterval\s*\(", src))
        n_timeout += len(re.findall(r"(?<![.\w])setTimeout\s*\(", src))

    for member in sorted(ZOTERO_FORBIDDEN):
        if member in uses:
            print(f"  [XX] Zotero.{member}  → {uses[member][:4]}  "
                  f"（{ZOTERO_FORBIDDEN[member]}）")
            problems.append(f"Zotero.{member} 不存在")
    unknown = [m for m in uses if m not in ZOTERO_KNOWN and m not in ZOTERO_FORBIDDEN]
    if unknown:
        print(f"  [!!] 不在已知列表里的成员（需人工确认）：{sorted(unknown)}")
        for m in sorted(unknown):
            print(f"       {uses[m][:5]}  Zotero.{m}")
    else:
        print("  [OK] 没有明显不存在的成员")
    print(f"       共用到 {len(uses)} 个成员：{', '.join(sorted(uses))}")

    # 2) 回调里的 this
    print("\n[2] 回调里的 this 引用")
    if bad_this:
        print(f"  [!!] {len(bad_this)} 处「function(...) 里用 this」——"
              f"Zotero 回调/定时器里 this 不可靠：")
        for line in bad_this:
            print(f"       {line}")
            problems.append(f"{line.split()[0]} 回调里用了 this")
    else:
        print("  [OK] 没有在非箭头回调里用 this")

    # 3) 定时器：Zotero 沙箱里 setInterval 的坑
    print("\n[3] 定时器用法")
    print(f"       setTimeout  直接调用 {n_timeout} 次")
    print(f"       setInterval 直接调用 {n_interval} 次")
    if n_interval:
        print("  [!!] 用了 setInterval —— Zotero 插件沙箱里它**可能创建成功但从不触发**。")
        print("       本机实测（心跳数据为证）：taskPolling=true（对象建出来了）")
        print("       但 tickCount=0（回调一次都没跑），导致任务队列永远 pending。")
        print("       建议改用 setTimeout 递归：")
        print("         const schedule = () => { ...; self.timer = setTimeout(run, 2000); };")
        problems.append("用了不可靠的 setInterval")

    print("\n" + "=" * 68)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("审计通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
