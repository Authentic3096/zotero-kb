"""插件静态检查：JS 语法 + 关键符号 + manifest 合法性，并打包 xpi。

    python tools/check_plugin.py

语法检查优先用 **node --check** —— 那是权威的解析器。
本机一开始想手写一个括号配平扫描器凑合，结果是**误报**：
它不会正确跳过模板串里的 `${...}` 插值（buildPrefsHTML 里大量使用），
于是把插值的大括号当成了未闭合。所以这里只在 node 缺失时才退回粗检，
并且明确标注"粗检可能有误报"。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
# 版本号从 manifest 读，避免两处各写一份、升级时忘改一处
def _manifest() -> dict:
    # ⚠ 必须用 utf-8-sig：这一句在**模块加载时**就会执行（下面的 VERSION），
    #   而 manifest 万一被写进 UTF-8 BOM，`encoding="utf-8"` 会当场抛
    #   "Unexpected UTF-8 BOM" —— 于是整个自检脚本**崩在 import 阶段**，
    #   什么都没查就退出，报错还看不出是 BOM 的问题。
    #   本机就是这么踩的（用 PowerShell 的 Set-Content -Encoding UTF8 改版本号）。
    #   这里读得下去，BOM 该不该存在由下面的正式检查去判、去报。
    return json.load(open(os.path.join(PLUGIN, "manifest.json"),
                          encoding="utf-8-sig"))


VERSION = _manifest().get("version", "0.0.0")

PAIRS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {v: k for k, v in PAIRS.items()}


def node_check(path: str) -> tuple[bool, str]:
    """用 node 做真语法检查。返回 (可用, 结果说明)。"""
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if not node or not os.path.exists(node):
        return False, "node 不可用"
    try:
        out = subprocess.run([node, "--check", path], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as exc:  # noqa: BLE001
        return False, f"调用 node 失败：{exc}"
    if out.returncode == 0:
        return True, "语法正确"
    return True, (out.stderr or out.stdout or "").strip()[:400]


def rough_balance(src: str) -> list[str]:
    """粗检（仅 node 不可用时用）。已知会对模板串插值误报，仅供参考。"""
    problems: list[str] = []
    stack: list[tuple[str, int]] = []
    in_str = None
    esc = False
    line = 1
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == "\n":
            line += 1
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'`":
            in_str = ch
            i += 1
            continue
        if ch == "/" and i + 1 < len(src) and src[i + 1] == "/":
            while i < len(src) and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(src) and src[i + 1] == "*":
            i += 2
            while i + 1 < len(src) and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        if ch in PAIRS:
            stack.append((ch, line))
        elif ch in CLOSERS:
            if not stack:
                problems.append(f"第 {line} 行多余的 {ch}")
                return problems
            op, ln = stack.pop()
            if PAIRS[op] != ch:
                problems.append(f"第 {line} 行 {ch} 与第 {ln} 行 {op} 不匹配")
                return problems
        i += 1
    for op, ln in stack[:5]:
        problems.append(f"第 {ln} 行 {op} 未闭合")
    return problems


def main() -> int:
    print("=" * 64)
    print("Zotero 插件静态检查与打包")
    print("=" * 64)
    problems: list[str] = []

    # ---------------------------------------------------------- 生成物同步
    # ⚠ 放在最前：xpi 里装的是 `bootstrap.js`，而它是 `src/*.js` 拼出来的
    #   生成物。它不同步的话，后面所有"代码检查"查的都是**另一份**东西 ——
    #   而真正装进去的旧代码完全没被查。本机吃过"改了却没生效"的亏，所以这里
    #   直接把它变成一条会红的检查。
    print("\n[生成物同步]")
    sys.path.insert(0, HERE)
    import build_bootstrap as BB

    sync_problem = ""
    if not os.path.exists(BB.OUT):
        sync_problem = "bootstrap.js 不存在"
    else:
        try:
            want = BB.compose()
            with open(BB.OUT, "rb") as fh:
                raw = fh.read()
            if raw[:3] == b"\xef\xbb\xbf":
                sync_problem = "bootstrap.js 带 UTF-8 BOM"
            elif raw.decode("utf-8") != want:
                sync_problem = "bootstrap.js 与 src/*.js 不同步"
        except Exception as exc:  # noqa: BLE001
            sync_problem = f"{type(exc).__name__}: {exc}"
    if sync_problem:
        print(f"  [XX] {sync_problem}")
        print("       修：python tools/build_bootstrap.py")
        print("       （源码是 zotero-plugin/src/*.js，那份才是要改的东西）")
        problems.append(sync_problem)
    else:
        print(f"  [OK] bootstrap.js == src/*.js 拼出来的内容"
              f"（{len(BB.SRC_ORDER)} 个源文件）")

    # ---------------------------------------------------------- manifest
    mpath = os.path.join(PLUGIN, "manifest.json")
    # ⚠ BOM 检查放在最前：带 UTF-8 BOM 的 manifest.json 会让 `json.load` 直接抛
    #   "Unexpected UTF-8 BOM"，而 Zotero 那边的表现可能又是一句没有线索的
    #   "可能无法与该版本的 Zotero 兼容"（本项目在 error=-3 上吃过这种亏）。
    #   本机就是被这个坑到的：用 PowerShell 的 `Set-Content -Encoding UTF8`
    #   改版本号，它默认写 BOM —— 所以这条必须自动查，不能靠人记得。
    with open(mpath, "rb") as fh:
        head = fh.read(3)
    if head == b"\xef\xbb\xbf":
        print("  [XX] manifest.json 带 UTF-8 BOM —— 必须先去掉"
              "（用编辑器另存为「UTF-8 无 BOM」，别用 PowerShell 的 "
              "Set-Content -Encoding UTF8）")
        problems.append("manifest.json 带 UTF-8 BOM")
    else:
        print("  [OK] manifest.json 无 BOM")
    try:
        m = json.load(open(mpath, encoding="utf-8"))
        print("  [OK] manifest.json 可解析")
    except Exception as exc:  # noqa: BLE001
        print(f"  [XX] manifest.json 读不了：{exc}")
        return 1
    # manifest 用 applications 还是 browser_specific_settings？
    # **实测结论（2026-10-02，Zotero 10.0.5）**：
    #   · `applications.zotero` + update_url + strict_min/max  → 解析成功 ✅
    #   · `browser_specific_settings.zotero`（官方 schema 推荐的写法）→ error=-3 ❌
    # 也就是说虽然 Firefox 的 manifest schema 把 applications 标为 deprecated、
    # 建议用 browser_specific_settings，但 **Zotero 10 上反而是 applications 能用**。
    # 所以这里只做提示，不算问题。
    app = ((m.get("applications") or {}).get("zotero")
           or (m.get("browser_specific_settings") or {}).get("zotero") or {})
    if "applications" in m:
        print("  [OK] 用 applications.zotero（本机 Zotero 10 实测可用）")
    elif "browser_specific_settings" in m:
        print("  [!!] 用的是 browser_specific_settings —— 官方 schema 推荐它，"
              "但本机实测在 Zotero 10 上解析失败(error=-3)，建议换回 applications")
        problems.append("browser_specific_settings 在本机 Zotero 10 上解析失败")
    for label, ok in [
        ("manifest_version == 2", m.get("manifest_version") == 2),
        ("有插件 id", bool(app.get("id"))),
        ("有 name 与 version", bool(m.get("name")) and bool(m.get("version"))),
    ]:
        print(f"  {'[OK]' if ok else '[XX]'} {label}")
        if not ok:
            problems.append(f"manifest: {label}")

    # 版本范围是**可选**的：不写时 Zotero 用默认 min="0" / max="*"
    # （见 XPIInstall.sys.mjs 里 addon.targetApplications 的构建，以及
    #  XPIDatabase.isCompatibleWith 的 `app.minVersion || "0"`）。
    # 本机踩过的坑：写 7.0 ~ 10.*、6.999 ~ 10.0.* 都被 UI 安装拒绝并报
    # "可能无法与该版本的 Zotero 兼容"，而真正原因无从得知；
    # 去掉版本范围是最宽松、也最不容易被拒的写法。
    lo = str(app.get("strict_min_version") or "")
    hi = str(app.get("strict_max_version") or "")
    if not lo and not hi:
        print("  [OK] 未声明版本范围 → Zotero 用默认 min=0 / max=*（最宽松）")
    else:
        print(f"  [--] 声明了版本范围：{lo or '(默认 0)'} ~ {hi or '(默认 *)'}")
        if "*" in lo:
            print("  [XX] strict_min_version 里不允许用 '*'（Zotero 会直接拒绝）")
            problems.append("strict_min_version 含 *")
    print(f"       id={app.get('id')}  v{m.get('version')}")
    # update_url 有两种坏法，实测结论不同，别混：
    #   · 空字符串 → Zotero 判定"可能不兼容"直接拒绝安装（实测踩过）
    #   · 整个字段不写 → **同样装不上**：只差这一个字段的变体包拿到
    #     `AddonManager.getInstallForFile()` 的 `error=-3 (ERROR_CORRUPT_FILE)`，
    #     而加上它（哪怕指向 example.com）就 `error=0`。见
    #     skills/zotero-plugin-dev/SKILL.md 的「坑 1」对照实验表，
    #     以及本机 7 个可用插件**全部**带 update_url 这一旁证。
    # 所以这里把"没写"也报成问题，而不是放行。
    if "update_url" in app:
        if str(app.get("update_url") or "").strip():
            print("  [OK] update_url 有值")
        else:
            print("  [XX] update_url 是空字符串 —— Zotero 会拒绝安装，"
                  "删掉这个字段或填真实 URL")
            problems.append("manifest: update_url 为空")
    else:
        print("  [XX] 没有 update_url 字段 —— 实测 Zotero 10 上会解析失败"
              "(error=-3)，插件装不上。填一个真实 URL 即可")
        problems.append("manifest: 缺 update_url")

    # ---------------------------------------------------------- JS 语法
    # ⚠ 用 tools/check_js_syntax.py 的 AsyncFunction 方式，**不要**用 node --check。
    # 这些脚本是被 new AsyncFunction(...) 执行的（整个文件当 async 函数体），
    # 所以顶层 await / return 都合法；而 node --check 按 CommonJS 解析会把它们
    # 报成语法错误 —— 本机就被这个误导过（以为脚本坏了，其实是检查方式不对）。
    sys.path.insert(0, HERE)
    import check_js_syntax
    bpath = os.path.join(PLUGIN, "bootstrap.js")
    src = open(bpath, encoding="utf-8").read()
    ok, detail = check_js_syntax.check(bpath)
    print(f"  {'[OK]' if ok else '[XX]'} bootstrap.js 语法（AsyncFunction 方式）：{detail}")
    if not ok:
        problems.append("bootstrap.js 有语法错")

    # 生命周期函数（Zotero 会直接调这几个）
    for fn in ("startup", "shutdown", "install", "uninstall"):
        if re.search(rf"^\s*{fn}:\s*function", src, re.M):
            print(f"  [OK] 生命周期 {fn}")
        else:
            print(f"  [XX] 缺生命周期 {fn}")
            problems.append(f"缺 {fn}")

    internal = ["handleNewItem", "askApply", "applySuggestion", "ensureCollection",
                "request", "healthCheck", "registerNotifier", "registerPrefPane",
                "writeStatusFile", "onNotify", "waitJob", "suggestFor", "buildMeta",
                "notify", "registerWeightColumn", "refreshWeights",
                "unregisterWeightColumn", "startTaskPolling", "stopTaskPolling",
                "runTask", "runBuiltinCommand",
                # 元数据补全（右键「补全元数据（本地模型）」那条链）：
                # 少任何一个都是"点了没反应"或"建议拿到了写不进去"，
                # 所以跟着前 21 个一起盯着（本机吃过"分支定义但从未调用"的亏）。
                "metaFillFor", "metaLine", "metaEmptyText",
                "askApplyMeta", "askOneMeta", "applyMeta",
                # 打开知识库（分级）—— 右键二级菜单那条链。少任何一个都是
                # "菜单里少一项"或"点了没反应"，所以一起盯着。
                "kbLevels", "kbLevelsMissing", "openKbPath", "openKbViaPanel",
                "openKbLevel", "openKbFolder",
                # 内容窗格「本地模型」分区（19-itempane.js）与阅读器选中入口
                # （20-reader.js）。少任何一个都是"按钮点了没反应"或"窗格空白"，
                # 而这类故障在 Zotero 里**没有任何报错** —— 所以名字一起盯着。
                "registerItemPane", "registerReaderEvents", "registerQuitGuard",
                "unregisterQuitGuard", "hasUnsavedChat", "chatOf", "panePaint",
                "paneRender", "paneSend", "paneInject", "paneLocate",
                "panePropose", "paneClear", "paraStart", "paraLoadPlan",
                "paraShowCurrent", "paraCheckOne", "paraPrefetch", "paraRender",
                "paraRenderButtons", "paraNext", "paraExit", "enterWriteMode",
                "paintWrite", "confirmWrite", "onReaderSelection",
                "readerItemKey", "readerLocate"]
    missing = [f for f in internal if f"{f}:" not in src]
    if missing:
        print(f"  [XX] 缺内部函数：{missing}")
        problems.extend(f"缺 {f}" for f in missing)
    else:
        print(f"  [OK] {len(internal)} 个内部函数齐备")

    # ---------------------------------------------------------- 首选项键对齐
    #
    # ⚠ 这条检查原来是坏的：它找的是 `"extensions.zotero-kb.xxx"` 这个**全长形式**，
    #   而代码里从来没用过那种写法（PREFS 映射表里是 `"zotero-kb.xxx"` 短形式）
    #   —— 于是它一直"扫到 0 个键、检查通过"。一个匹配不到东西的检查等于没有检查，
    #   所以下面顺带加一条：扫不到就报错。
    #   短形式与 l10n id 不会混：l10n id 是 `zotero-kb-btn-send`（连字符），
    #   首选项是 `zotero-kb.chatQuitWarn`（点号）。
    ppath = os.path.join(PLUGIN, "prefs.js")
    if os.path.exists(ppath):
        declared = {m.replace("extensions.zotero.", "")
                    for m in re.findall(r'pref\("([^"]+)"',
                                        open(ppath, encoding="utf-8").read())}
        used = set(re.findall(r'"(zotero-kb\.[a-zA-Z]+)"', src))
        if not used:
            print("  [XX] 没扫到任何首选项键 —— 这条检查本身失效了（曾经就是这样）")
            problems.append("首选项检查失效：扫不到键")
        undeclared = sorted(used - declared)
        if undeclared:
            print(f"  [XX] prefs.js 缺默认值：{undeclared}")
            problems.append(f"prefs.js 缺默认值：{undeclared}")
        else:
            print(f"  [OK] prefs.js 覆盖全部 {len(used)} 个键"
                  f"（声明 {len(declared)} 个）")
    else:
        print("  [!!] 没有 prefs.js")

    for name in ("icon.svg", "icon.png", "toolbar-icon.svg"):
        p = os.path.join(PLUGIN, name)
        if os.path.exists(p):
            print(f"  [OK] {name}（{os.path.getsize(p)} 字节）")

    # manifest 里声明的图标必须真的在包里（缺了会出现
    # "无法安装插件…它可能无法与该版本的 Zotero 兼容"这种看不出原因的报错）
    for size, rel in (m.get("icons") or {}).items():
        if os.path.exists(os.path.join(PLUGIN, rel)):
            print(f"  [OK] manifest 图标 {size} → {rel}")
        else:
            print(f"  [XX] manifest 声明图标 {rel}，但文件不存在")
            problems.append(f"manifest 图标缺失：{rel}")

    # 代码里用 rootURI 引用的资源也必须存在。
    # 曾经漏打 toolbar-icon.svg：代码引用了、包里没有，结果是
    # "工具栏按钮在，但图标空白/巨大"，报错一点线索都没有。
    # 正则只取到 .扩展名为止 —— 否则会把 url(...) 后面的 `)` 一起吞进来。
    bsrc = open(os.path.join(PLUGIN, "bootstrap.js"), encoding="utf-8").read()
    for rel in sorted(set(re.findall(
            r'rootURI\s*\+\s*"([^"]+\.(?:svg|png|js|xhtml|css|json|ftl))"', bsrc))):
        if os.path.exists(os.path.join(PLUGIN, rel)):
            print(f"  [OK] 代码引用的资源 {rel} 存在")
        else:
            print(f"  [XX] bootstrap.js 引用了 {rel}，但文件不存在")
            problems.append(f"缺少被引用的资源 {rel}")

    # ---------------------------------------------------------- 本地化（ftl）
    # 两个坑都是"没有任何报错"的那种：
    #   · `registerSection` 的 header/sidenav 里 `l10nID` 是必填项，
    #     指向的 id 必须在 ftl 里存在 —— 少一条就是分区标题空白；
    #   · `locale/` 是**目录**，而打包器原来是"只收根目录的文件"，
    #     会把整目录漏掉（包做好了、装上了、就是没有文案）。
    print("\n[本地化 ftl]")
    ftl_files = []
    loc_dir = os.path.join(PLUGIN, "locale")
    if os.path.isdir(loc_dir):
        for root, _dirs, files in os.walk(loc_dir):
            for fn in files:
                if fn.endswith(".ftl"):
                    ftl_files.append(os.path.join(root, fn))
    if not ftl_files:
        print("  [XX] 没有 locale/**/*.ftl —— registerSection 的 l10nID 会找不到文案")
        problems.append("缺少 locale/**/*.ftl")
    else:
        defined = {}          # id -> [文件]
        for path in ftl_files:
            rel = os.path.relpath(path, PLUGIN).replace(os.sep, "/")
            for line in open(path, encoding="utf-8"):
                m = re.match(r"^([A-Za-z0-9_-]+)\s*=", line)
                if m:
                    defined.setdefault(m.group(1), []).append(rel)
        print(f"  [OK] {len(ftl_files)} 个 ftl，共 {len(defined)} 条文案："
              + ", ".join(sorted(os.path.relpath(p, PLUGIN).replace(os.sep, '/')
                                 for p in ftl_files)))
        # 代码里"用了哪个 l10n id"。三种写法都要认 —— 只认 `l10nID:` 字面量的话，
        # 动态调用（`paneButton(doc, "id")` / `self.l10n(doc, el, "id")`）会漏掉，
        # 而它们恰恰是分区里绝大多数按钮的写法（本机实测漏了 7 个）。
        # ⚠ `[^,]+` 必须写成 `[^,\n]+`：不排除换行的话，正则会跨越注释行去匹配，
        #   把注释里的 `self.l10n()` 和几行之后的 `"placeholder"` 凑成一对
        #   —— 于是报出一个根本不存在的 l10n id（实测撞到过）。
        used = set(re.findall(r"""l10nID:\s*['"]([^'"]+)['"]""", bsrc))
        used |= set(re.findall(
            r"""\.l10n\(\s*[^,\n]+,\s*[^,\n]+,\s*['"]([^'"]+)['"]""", bsrc))
        used |= set(re.findall(
            r"""paneButton\(\s*[^,\n]+,\s*['"]([^'"]+)['"]""", bsrc))
        used |= set(re.findall(r"""l10nText\(\s*['"]([^'"]+)['"]""", bsrc))
        for key in sorted(used):
            if key in defined:
                print(f"  [OK] l10nID {key} 有定义"
                      f"（{', '.join(sorted(set(defined[key]))) }）")
            else:
                print(f"  [XX] bootstrap.js 用了 l10nID {key}，但 ftl 里没有 —— "
                      f"界面上会显示空白，且没有任何报错")
                problems.append(f"l10nID 未定义：{key}")
        if not used:
            print("  [--] 代码里还没用到 l10nID（新加分区时这条检查会兜住）")
        else:
            print(f"  [OK] 代码里用了 {len(used)} 个 l10nID，全部有定义")
        # 中英文必须成对：只有 zh-CN 时，英文用户会看到空白标题
        langs = {os.path.basename(os.path.dirname(p)) for p in ftl_files}
        if "en-US" not in langs:
            print(f"  [XX] 缺 en-US（Zotero 的兜底链是 精确 → 同语种 → en-US → "
                  f"第一个可用的；没有 en-US 的非中文用户会看到空白）")
            problems.append("locale 缺 en-US")
        else:
            print(f"  [OK] 语言覆盖：{', '.join(sorted(langs))}")
        # 各语言的 id 必须一致：少一条就是那条文案在某个语言下显示空白
        per_lang = {}
        for path in ftl_files:
            lang = os.path.basename(os.path.dirname(path))
            per_lang.setdefault(lang, set())
            for line in open(path, encoding="utf-8"):
                m = re.match(r"^([A-Za-z0-9_-]+)\s*=", line)
                if m:
                    per_lang[lang].add(m.group(1))
        if len(per_lang) > 1:
            base = sorted(per_lang)[0]
            for lang in sorted(per_lang)[1:]:
                miss = per_lang[base] - per_lang[lang]
                extra = per_lang[lang] - per_lang[base]
                if miss or extra:
                    print(f"  [XX] {lang} 与 {base} 的文案 id 不一致："
                          f"缺 {sorted(miss)[:5]}，多 {sorted(extra)[:5]}")
                    problems.append(f"ftl id 不一致：{lang} vs {base}")
                else:
                    print(f"  [OK] {lang} 与 {base} 的 {len(per_lang[lang])} 条 id 一一对应")

    # ---------------------------------------------------------- 打包
    # 用 pack_plugin.py 打包，不用本地 zipfile 默认参数 ——
    # 默认参数写出的 ZIP 是 MS-DOS 来源、权限 0666，与可用插件不一致，
    # 有可能被 Zotero 拒（详见 pack_plugin.py 顶部说明）。
    print("\n[打包 xpi]")
    sys.path.insert(0, HERE)
    import importlib

    import pack_plugin
    importlib.reload(pack_plugin)
    xpi_for_paths = None
    if sync_problem:
        # 不同步就**不打包**：打出来的包是旧代码，装上只会让人以为"改了没生效"，
        # 而且目录里那个新 xpi 会盖掉上一个能用的包（更难查）。
        print(f"  [XX] 跳过打包：bootstrap.js 与 src/*.js 不同步，"
              f"先跑 python tools/build_bootstrap.py")
    else:
        for old in os.listdir(PLUGIN):
            if old.startswith("zotero-kb-") and old.endswith(".xpi"):
                os.remove(os.path.join(PLUGIN, old))
        xpi = pack_plugin.pack(None)
        with zipfile.ZipFile(xpi) as z:
            names = z.namelist()
        print(f"  [OK] {xpi}（{os.path.getsize(xpi)} 字节）")
        print("       内容：" + ", ".join(names))
        for must in ("manifest.json", "bootstrap.js"):
            if must not in names:
                print(f"  [XX] 包里缺 {must}")
                problems.append(f"xpi 缺 {must}")
        if "src/00-core.js" in names:
            print("  [XX] 包里有 src/ —— 源码目录不该进 xpi（白胖、而且没用）")
            problems.append("xpi 里混进了 src/")
        # locale 是**目录**：打包器原来是"只收根目录的文件"，会把整目录漏掉。
        # 这条断言盯的就是那个漏法（症状：分区标题空白、无报错）。
        ftl_in_xpi = [n for n in names if n.startswith("locale/") and n.endswith(".ftl")]
        if ftl_in_xpi:
            print(f"  [OK] 包内本地化：{', '.join(sorted(ftl_in_xpi))}")
        else:
            print("  [XX] 包里没有 locale/**/*.ftl —— 内容窗格的分区标题会是空白")
            problems.append("xpi 缺 locale/**/*.ftl")
        xpi_for_paths = xpi

    # 打包后立刻查"包内有没有写死的开发机路径"。
    # 为什么接在这里：这个项目要公开发布，包内残留 D:\DSHplugins\...
    # 会让别人装上后指向不存在的目录（本机已经出过一次"面板找不到
    # Python 环境"，根因就是从知识库位置反推项目根）。接进打包流程
    # 才能保证不会漏 —— 靠人记得单独跑一个脚本是不可靠的。
    print("\n[包内绝对路径]")
    if not xpi_for_paths:
        print("  [--] 没有打包，跳过")
    else:
        try:
            sys.path.insert(0, HERE)
            import check_xpi_paths
            n_bad, bad = check_xpi_paths.check_xpi(xpi_for_paths)
            if n_bad:
                print(f"  [XX] {n_bad} 处写死的本机路径：")
                for b in bad:
                    print(f"       {b}")
                problems.append(f"xpi 里有 {n_bad} 处写死的本机路径")
            else:
                print("  [OK] 没有写死的本机路径")
                print("       （Services.dirsvc / %LOCALAPPDATA% / 用户配置"
                      "这类动态取值不算）")
        except Exception as exc:  # noqa: BLE001
            print(f"  [!!] 这项检查没跑成：{type(exc).__name__}: {exc}")

    # 设置面板能不能被 Zotero 加载 —— 用户报过"设置里点击文献知识库没有反应"，
    # 根因是 settings.xhtml 头部带了 <?xml?> 声明（Zotero 把内容嵌进 <div>
    # 再解析，声明在中间就是非法 XML）。这种错误静态看不出，只能专门回归。
    print("\n[设置面板可加载性]")
    try:
        sys.path.insert(0, HERE)
        import importlib
        import check_prefpane
        importlib.reload(check_prefpane)
        if check_prefpane.main() != 0:
            problems.append("settings.xhtml 不能被 Zotero 正常加载（见上）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] 这项检查没跑成：{type(exc).__name__}: {exc}")

    # 外接 API 的服务商地址在插件和服务端各存一份，不一致就会出现
    # "设置界面显示一个地址、实际请求打到另一个"这种最难查的问题。
    print("\n[外接 API 服务商地址一致性]")
    try:
        sys.path.insert(0, HERE)
        import importlib
        import check_api_presets
        importlib.reload(check_api_presets)
        if check_api_presets.main() != 0:
            problems.append("插件预设与服务端的服务商地址不一致（见上）")
    except Exception as exc:  # noqa: BLE001
        # ⚠ 检查自己崩了也要**报出来**，不能只打一行 [!!] 就放过。
        #   2026-10-05 发布 v1.0.0 时才发现：CI 上这项一直是崩的
        #   （根因是 offline/schemas.py 少 import sys），而它被静默忽略了 ——
        #   等于这条检查在 CI 上从来没真正跑过（就是"死档"）。
        print(f"  [XX] 这项检查自己崩了：{type(exc).__name__}: {exc}")
        problems.append(f"服务商地址检查跑不起来：{type(exc).__name__}: {exc}")

    # 分级清单在 Python（offline/kbviews.py）和 JS（src/13-kbopen.js）里各存
    # 一份 —— 不一致就会出现"面板里叫这个名字、右键菜单里叫那个名字"，或者
    # 某一边指向不存在的文件。这是本项目反复吃过的"两处各写一份"的亏。
    #
    # ⚠ 返回码有三档，别混：0=一致，1=**真的不一致**（要红），2=这项跑不起来
    #   （缺 node / 读不到源文件）。第二档不能当成"通过" —— 那正是"审计器某档
    #   从来没有人往里放东西"的成因；但也不能当成"不一致"，否则缺 node 的机器
    #   会莫名其妙红。
    print("\n[知识库分级清单一致性]")
    try:
        sys.path.insert(0, HERE)
        import importlib
        import check_kb_levels
        importlib.reload(check_kb_levels)
        rc = check_kb_levels.main()
        if rc == 1:
            problems.append("知识库分级清单在 Python 与 JS 两侧不一致（见上）")
        elif rc == 2:
            print("  [!!] 这项跑不起来（多半是缺 node）—— 没能校验，不算通过")
    except Exception as exc:  # noqa: BLE001
        # 检查自己崩了也要报出来：2026-10-05 发布 v1.0.0 时，CI 上就是这个
        # 位置暴露出 offline/schemas.py 少 import sys（此前它一直没被发现，
        # 因为上层把它当"检查没跑成"静默放过了）。
        print(f"  [XX] 这项检查自己崩了：{type(exc).__name__}: {exc}")
        problems.append(f"分级清单检查跑不起来：{type(exc).__name__}: {exc}")

    print("\n" + "=" * 64)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("检查通过。")
    print("安装：Zotero → 工具 → 插件 → 右上齿轮 → Install Plugin From File…")
    print(f"      选 {xpi}（本机实测：往 profile 丢 xpi 无效，必须走这个入口）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
