"""发布前综合审计：隐私泄露 + 写死路径 + 安装可用性。

    python tools/audit_release.py

为什么要有它：这个项目要公开发布，而"自己机器上完全正常、别人拿到就出问题"
（或反过来：把自己的 key/路径泄露出去）这类事，**靠人记得检查是不可靠的**。
本机已经栽过几次：设置面板里的示例路径写成开发机目录、
打包产物里残留 `C:\\Windows\\System32` 的硬编码、状态文件里带着明文 token。

检查三类：

  A. 隐私泄露
     · API key / token 的字面值（形如 sk-xxx、32 位随机串）
     · 用户目录（C:\\Users\\<真名>）
     · 会跟着仓库走的文件里是否混进了运行期密钥

  B. 写死的绝对路径
     · 代码里的盘符路径（排除"动态取值/系统常量/环境变量"这些正当写法）
     · 文档与示例里的开发机路径

  C. 安装可用性
     · requirements.txt 是否可解析、有没有列不该列的
     · 安装脚本引用的文件都在不在
     · 打包器是否会把该排除的排掉

用法：发版前跑一遍，退出码 0 才算过。
"""

from __future__ import annotations

import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 不扫的目录：依赖、数据、备份、会话产物
#
# ⚠ `.mineru` 是**可选组件 MinerU 的本地安装**（scripts/install-mineru.ps1 装的：
#   venv + 下下来的模型）。模型目录里有 tokenizer 的 vocab/merges 文件，
#   里面成片都是"看着像 token 的随机串"，扫它只会刷出一屏误报
#   （本机实测：几十条 token 误报全部来自 .mineru）。它跟 .venv 同类，跳过。
SKIP_DIRS = {".venv", "kb", ".git", "__pycache__", ".tools",
             ".mineru", "node_modules"}
# 迁移备份目录（migrate_kb.py 生成，名带时间戳）——按前缀忽略，别写死某一个
SKIP_PREFIXES = ("_kb-backup-",)
# 只扫这些后缀（文本类）
EXTS = {".py", ".js", ".mjs", ".xhtml", ".html", ".json", ".md", ".txt",
        ".cmd", ".vbs", ".ps1", ".yml", ".yaml", ".toml", ".cfg", ".ini"}

PASS = FAIL = 0
PROBLEMS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        PROBLEMS.append(f"{name}  {detail}")
        print(f"  FAIL  {name}  {detail}")


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".venv")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXTS:
                yield os.path.join(dirpath, fn)


def read(p: str) -> str:
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


# ---------------------------------------------------------------- A. 隐私

# 真实密钥的样子：sk- 开头 + 足够长；或 32 位 hex/base64 随机串
SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "sk- 开头的 API key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"), "Anthropic key"),
    (re.compile(r"\bAIza[A-Za-z0-9\-_]{30,}"), "Google API key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\."), "JWT"),
]

# 32 位随机串（本机 token 就是 32 字符）
RANDOM32 = re.compile(r"\b[A-Za-z0-9_\-]{32}\b")

HOMEDIR = re.compile(r"C:\\+Users\\+(?!Public|%|\.\.\.)[A-Za-z0-9_.\-]+", re.I)


def audit_privacy():
    print("\n[A] 隐私泄露")
    leaks: list[str] = []
    for p in iter_files():
        rel = os.path.relpath(p, ROOT)
        txt = read(p)
        for i, ln in enumerate(txt.split("\n"), 1):
            # 注释里举例说明的不算（例如 "sk-... 从服务商后台复制"）
            stripped = ln.strip()
            for pat, what in SECRET_PATTERNS:
                for m in pat.finditer(ln):
                    val = m.group(0)
                    # 明显的占位符放过
                    if re.fullmatch(r"sk-[xX0\-\*\.]+", val):
                        continue
                    if "EXAMPLE" in val.upper() or "PLACEHOLDER" in val.upper():
                        continue
                    leaks.append(f"{rel}:{i}  {what}  {val[:20]}…")
            for m in RANDOM32.finditer(ln):
                val = m.group(0)
                # 排除：文件路径片段、常见词、markdown 分隔线、我们自己的 id
                if any(k in val.lower() for k in
                       ("zotero", "http", "example", "placeholder",
                        "localhost", "template")):
                    continue
                if stripped.startswith(("//", "*", "#", "<!--")):
                    continue
                leaks.append(f"{rel}:{i}  32 位随机串（像 token）  {val[:16]}…")
            for m in HOMEDIR.finditer(ln):
                leaks.append(f"{rel}:{i}  用户目录  {m.group(0)}")
    if leaks:
        print(f"  发现 {len(leaks)} 处：")
        for x in leaks[:25]:
            print(f"    {x}")
        if len(leaks) > 25:
            print(f"    … 还有 {len(leaks) - 25} 处")
    check(f"扫描 {sum(1 for _ in iter_files())} 个文件，无密钥/用户目录泄露",
          not leaks, f"{len(leaks)} 处")

    # 运行期密钥文件必须被 .gitignore 排除
    gi = read(os.path.join(ROOT, ".gitignore"))
    must_ignore = ["llm-config.json", "service-token.txt", "bridge-token.txt",
                   "zotero-api-key.txt", "index.db", ".venv", "*.xpi"]
    missing = [x for x in must_ignore if x not in gi]
    check(f".gitignore 覆盖运行期密钥与产物（{len(must_ignore)} 项）",
          not missing, f"缺：{missing}")

    # 知识库目录里不该有能跟着仓库走的东西
    check("仓库内没有 llm-config.json（含 API key）",
          not os.path.exists(os.path.join(ROOT, "llm-config.json")))


# ---------------------------------------------------------------- B. 路径

# 正当的"动态取值/系统常量"，命中就不算写死
BENIGN_PATH = re.compile(
    r"(dirsvc|getenv|expanduser|expandvars|environ|%LOCALAPPDATA%|%APPDATA%|"
    r"%USERPROFILE%|%PROGRAMFILES%|C:\\\\Users\\\\Public|C:\\\\ProgramData|"
    r"C:\\\\Windows\\\\System32|pathToFile|PathUtils|os\.path\.dirname|"
    r"__file__|sys\.executable|示例|例如|如\s|placeholder|"
    r"占位|比如)")

ABS_WIN = re.compile(r"[A-Za-z]:\\\\?[^\"'\s,;)\]]{3,}")

# **会跟着仓库走、且别人拿到必须能用**的文件 —— 这里出现本机路径是真问题
MUST_BE_PORTABLE = {
    "bundle/cordis.patch.yml",
    "kb-location.json",
    "requirements.txt",
    ".gitignore",
}
# 文档/说明：示例路径是**故意**的（帮读者理解），只提示不报错
DOC_FILES = {"README.md", "ARCHITECTURE.md", "INSTALL.md"}

# 一次性排查脚本 / 开发工具：不进任何发布物，里面的本机路径不算问题。
#
# ⚠ 这里出现的路径是"当时为了排查某个具体现象"记下的，改成动态反而
#   不再还原现场。它们在打包器里也都被排除了（`pack_plugin.py` 的
#   EXCLUDE_SCRIPTS / tools 目录根本不进 xpi）。
DEV_SCRIPTS = {
    "zotero-plugin/verify-plugin.js",
    "zotero-plugin/diag-load.js",
    "zotero-plugin/diag-settings.js",
    "zotero-plugin/find-working-manifest.js",
    "zotero-plugin/install-in-zotero.js",
    "zotero-plugin/probe-variants.js",
    "zotero-plugin/probe-install-api.js",
}


def gitignored() -> set[str]:
    """粗略解析 .gitignore，返回被忽略的相对路径（小写、正斜杠）。

    为什么要它：有些文件**故意**带本机路径，但它们已在 .gitignore 里
    （是"按本机生成"的产物，不跟着仓库走）—— 报它们就是误报。
    本机第一版审计器就把 `bundle/cordis.patch.yml`、`kb-location.json`
    报成了问题，而它们本来就该是各人不同的。
    """
    out: set[str] = set()
    gi = read(os.path.join(ROOT, ".gitignore"))
    for ln in gi.split("\n"):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        s = s.replace("\\", "/").lstrip("/")
        if s.endswith("/"):
            out.add(s.rstrip("/"))
        else:
            out.add(s)
    return out


def is_generated(rel: str, ignored: set[str]) -> bool:
    rel_l = rel.replace("\\", "/").lower()
    for pat in ignored:
        p = pat.lower()
        if rel_l == p or rel_l.endswith("/" + p):
            return True
        # ⚠ 2026-10-04 修：目录型规则（.gitignore 里的 `xxx/`）必须按**前缀**匹配。
        #   之前只比对整路径与 basename，于是 `zotero-plugin/variants/` 这种
        #   目录规则对它**里面**的文件全都不生效 —— 那些"按本机生成"的产物
        #   会被当成"会跟着仓库走"而误报（variants.json 就是这么冒出来的）。
        if rel_l.startswith(p + "/"):
            return True
        if "/" not in p and os.path.basename(rel_l) == p:
            return True
        # 通配：*.xpi / kb/index.db*
        if p.startswith("*") and rel_l.endswith(p[1:]):
            return True
        if p.endswith("*") and rel_l.startswith(p[:-1]):
            return True
    return False


def audit_paths():
    print("\n[B] 写死的绝对路径")
    ignored = gitignored()
    high: list[str] = []      # 真问题
    low: list[str] = []       # 文档示例 / 注释 / 生成物
    gen: list[str] = []       # 已在 .gitignore 里的生成物（不算问题）

    for p in iter_files():
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        base = os.path.basename(rel)
        # 一次性排查脚本按用户要求不管（它们不进任何发布物）
        if rel in DEV_SCRIPTS:
            continue
        generated = is_generated(rel, ignored)
        for i, ln in enumerate(read(p).split("\n"), 1):
            if not ABS_WIN.search(ln):
                continue
            if BENIGN_PATH.search(ln):
                continue
            entry = f"{rel}:{i}  {ln.strip()[:88]}"
            if generated:
                gen.append(entry)
                continue
            # ⚠ 2026-10-04 修：这一档（真问题）此前**从来没有任何分支往里放**，
            #   于是下面 `check(..., not high)` 永远通过 —— 等于没查（死档）。
            #   现在把「别人拿到必须能用」的文件真正接上。
            if rel in MUST_BE_PORTABLE or base in MUST_BE_PORTABLE:
                high.append(entry)
                continue
            if ln.strip().startswith(("//", "*", "#", "<!--", "REM", "'", "::")):
                low.append(entry)       # 注释里的说明
                continue
            low.append(entry)           # 文档示例 / 待确认

    if high:
        print("  ❌ 必须可移植的文件里有本机路径（别人拿到用不了）：")
        for x in high:
            print(f"    {x}")
    if gen:
        print(f"  ✓ 已在 .gitignore 的生成物里 {len(gen)} 处"
              f"（故意带本机路径，不跟着仓库走）")
    if low:
        print(f"  ℹ 文档示例 / 注释 {len(low)} 处（供读者理解，不是问题）：")
        for x in low[:8]:
            print(f"    {x}")
        if len(low) > 8:
            print(f"    … 还有 {len(low) - 8} 处")

    check("必须可移植的文件（bundle 配置 / requirements / .gitignore）"
          "没有本机路径", not high, f"{len(high)} 处")

    # 用户名绝不能出现在任何地方（包括文档、诊断脚本）
    #
    # ⚠ 2026-10-05 修（CI 上必挂的坑，本机用"全新 clone"复现出来的）：
    #   这条原来是"裸词出现即算泄漏"。在 GitHub Actions 上 `expanduser("~")`
    #   是 `/home/runner` → 用户名 **runner**，而代码/文档里到处都有 `runner`
    #   这个词（MinerU 的 runner 参数、测试里的 runner…）→ 每次 CI 都红，
    #   报的却是"隐私泄漏"，看日志要绕半天。
    #   现在分两档：
    #     · 通用名（runner/build/ubuntu/root/…）或跑在 CI 里 → 只认**路径上下文**
    #       （`\Users\<名>`、`/home/<名>`、`/Users/<名>`）——真泄漏（机器路径）
    #       照样抓得住，普通单词不再误报；
    #     · 其它名字 → 保持原来的严格规则（裸词出现即泄漏）。
    #   降级时**打印说明**，不静默。
    user = os.path.basename(os.path.expanduser("~"))
    GENERIC_USERS = {"public", "user", "admin", "runner", "build", "ubuntu",
                     "root", "circleci", "vsts", "gcp", "vscode", "test",
                     "user1", "administrator"}
    # ⚠ 2026-10-08 补：仓库 owner 名是**公开信息**（就写在仓库地址里），而本机
    #   Windows 用户名恰好与它同名 → 严格模式会把每一处仓库链接
    #   （`github.com/<owner>/...`）都判成隐私泄漏。本机实测 14 处误报；
    #   CI 上 user=runner 命中通用名表所以是绿的，只有本机红、容易被当成真问题。
    #   把 owner 名一并降级后，`C:\Users\<名>\...` 这类真机器路径照样抓得住。
    REPO_OWNER = "Authentic3096"
    in_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    loose = (in_ci
             or (user or "").lower() in GENERIC_USERS
             or (user or "").lower() == REPO_OWNER.lower())
    hits: list[str] = []
    if user and user.lower() != "public":
        ctx = re.compile(r"(?:\\\\Users\\\\|/home/|/Users/)" + re.escape(user),
                         re.I)
        # ⚠ 跳过审计器自身：这条规则的**说明注释**里必须写出"什么形态算泄漏"
        #   （例如 目录/用户名 长什么样），不跳过就会自指误报
        #   （本机实测：注释里那行 `/home/runner` 被自己判成了泄漏）。
        #   同文件里"开发机目录名"那条用的是同一套做法。
        SELF = {"tools/audit_release.py", "tools/check_xpi_paths.py"}
        for p in iter_files():
            rel = os.path.relpath(p, ROOT)
            if rel.replace("\\", "/") in SELF:
                continue
            for i, ln in enumerate(read(p).split("\n"), 1):
                hit = bool(ctx.search(ln)) if loose else (user in ln)
                if hit:
                    hits.append(f"{rel}:{i}  {ln.strip()[:80]}")
    if loose:
        print(f"  ℹ 用户名「{user}」按通用名/CI 处理：只在路径上下文里算泄漏"
              f"（{'CI 环境' if in_ci else '命中通用名表'}）")
    check(f"全仓库没有本机用户名（{user}）", not hits,
          f"{len(hits)} 处：{hits[:2]}")

    # 开发机特有的**目录名**，和本机用户名是同一类东西：对别人毫无意义。
    # 出现在任何会随仓库发出去的地方（文档正文、代码、示例）都是泄漏。
    #
    # ⚠ 2026-10-04 补：此前这类名字只写在 `check_xpi_paths.py` 里、只对 xpi 生效，
    #   于是 `skills/` 里写死的 `D:\DSHplugins\...` 从首个提交起就在公开仓库里。
    #   现在共用同一套模式（import 过来），不再各写一份。
    #   注释里讲这个坑本身时会举例（与 check_xpi_paths.strip_code 同口径放过），
    #   但仍然打印出来供人过目 —— 不静默。
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import check_xpi_paths
        dev_dir = check_xpi_paths.SENSITIVE
    except Exception:  # noqa: BLE001
        dev_dir = re.compile(r"(DSHplugins|ZoteroData)", re.I)

    dev_leaks: list[str] = []
    dev_notes: list[str] = []
    # 审计器自身：模式定义与提示语里**必须**写出这些名字，跳过（同 check_xpi_paths）
    AUDIT_MACHINERY = {"tools/check_xpi_paths.py", "tools/audit_release.py"}
    for p in iter_files():
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel in AUDIT_MACHINERY:
            continue
        if is_generated(rel, ignored):
            continue          # 按本机生成、不跟着仓库走（如 kb-location.json）
        for i, ln in enumerate(read(p).split("\n"), 1):
            if not dev_dir.search(ln):
                continue
            entry = f"{rel}:{i}  {ln.strip()[:88]}"
            if ln.strip().startswith(("//", "*", "#", "<!--", "REM", "'", "::")):
                dev_notes.append(entry)
            else:
                dev_leaks.append(entry)
    if dev_notes:
        print(f"  ℹ 注释里提到开发机目录名 {len(dev_notes)} 处"
              f"（讲这个坑用的例子，不拦）：")
        for x in dev_notes[:6]:
            print(f"    {x}")
        if len(dev_notes) > 6:
            print(f"    … 还有 {len(dev_notes) - 6} 处")
    if dev_leaks:
        print(f"  ❌ {len(dev_leaks)} 处开发机目录名出现在正文/代码里：")
        for x in dev_leaks[:15]:
            print(f"    {x}")
    check("全仓库正文/代码里没有开发机目录名"
          "（DSHplugins / ZoteroData / X:\\Nutstore）", not dev_leaks,
          f"{len(dev_leaks)} 处：{dev_leaks[:2]}")

    # 打包产物单独查（用的是更严格的规则）
    print()
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import check_xpi_paths
        xpis = [f for f in os.listdir(os.path.join(ROOT, "zotero-plugin"))
                if f.endswith(".xpi")]
        if xpis:
            newest = max((os.path.join(ROOT, "zotero-plugin", f)
                          for f in xpis), key=os.path.getmtime)
            n, probs = check_xpi_paths.check_xpi(newest)
            check(f"xpi（{os.path.basename(newest)}）内无写死路径",
                  n == 0, "; ".join(probs[:3]))
        else:
            check("xpi 已打包", False, "没找到 .xpi")
    except Exception as exc:  # noqa: BLE001
        check("xpi 路径检查能跑", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- C. 安装

def audit_install():
    print("\n[C] 安装可用性")
    req = os.path.join(ROOT, "requirements.txt")
    check("requirements.txt 存在", os.path.exists(req))
    if os.path.exists(req):
        txt = read(req)
        pkgs = [ln.strip() for ln in txt.split("\n")
                if ln.strip() and not ln.strip().startswith("#")]
        check(f"requirements.txt 有 {len(pkgs)} 个依赖声明",
              len(pkgs) >= 3)
        # 不该出现的东西
        bad_pkgs = [p for p in pkgs
                    if re.search(r"lz4|cryptography", p, re.I)]
        check("不含已知未使用的包（lz4 / cryptography）",
              not bad_pkgs, f"{bad_pkgs}")

    for f in ("scripts/install-env.cmd", "scripts/install-env.ps1",
              "scripts/_kbtools.vbs", "scripts/0-panel.vbs",
              "scripts/1-convert.cmd", "scripts/4-service.vbs"):
        check(f"安装/运行脚本存在：{os.path.basename(f)}",
              os.path.exists(os.path.join(ROOT, f)))

    ps1 = read(os.path.join(ROOT, "scripts", "install-env.ps1"))
    check("install-env.ps1 不用 $ErrorActionPreference=Stop"
          "（外部程序写 stderr 会打断）",
          '$ErrorActionPreference = "Stop"' not in ps1)
    check("install-env.ps1 有 Invoke-Native 包装",
          "Invoke-Native" in ps1)
    check("install-env.ps1 用阿里云镜像（清华缺 mcp 包）",
          "mirrors.aliyun.com" in ps1)
    check("install-env.ps1 设了 HF_ENDPOINT（模型走国内镜像）",
          "hf-mirror.com" in ps1)

    cmd = read(os.path.join(ROOT, "scripts", "install-env.cmd"))
    check("install-env.cmd 用 -ExecutionPolicy Bypass"
          "（不改用户执行策略）",
          "ExecutionPolicy Bypass" in cmd)
    # ⚠ 判据要精细：**可执行部分**必须全 ASCII（cmd 在 GBK 代码页下
    #   解析非 ASCII 命令有风险），但 **REM 注释里的中文没关系** ——
    #   注释不参与执行。第一版检查没排除注释，误报过一次。
    exec_lines = [ln for ln in cmd.split("\n")
                  if ln.strip() and not ln.strip().upper().startswith("REM")]
    bad_lines = [ln for ln in exec_lines if not ln.isascii()]
    check("install-env.cmd 的可执行部分全 ASCII（注释里的中文不算）",
          not bad_lines, f"{bad_lines[:2]}")

    # 打包器排除列表
    packer = read(os.path.join(ROOT, "tools", "pack_plugin.py"))
    for script in ("diag-settings.js", "verify-plugin.js", "diag-load.js"):
        check(f"打包器排除 {script}", script in packer)


# ---------------------------------------------------------------- D. 编码卫生

# 允许保留 BOM 的例外（白名单，按相对路径）。
# 为什么会有例外：Windows PowerShell 5.1 读 **无 BOM** 的 .ps1 时会按 ANSI 解码，
# 里面成片的中文字符串会全部变乱码、甚至直接报语法错 ——
# 所以 **含中文的 .ps1 必须带 BOM**，不是脏。
# 2026-10-08：install-mineru / install-autostart / sync-skill / fetch-pdf 四个脚本
# 当初漏了 BOM，在 PS 5.1 下根本跑不起来（MinerU 安装就是因此失败），一并登记。
BOM_ALLOWED = {
    "scripts/install-env.ps1",
    "scripts/install-mineru.ps1",
    "scripts/install-autostart.ps1",
    "scripts/sync-skill.ps1",
    "skills/zotero-acquire/tools/fetch-pdf.ps1",
}


def audit_encoding():
    """查 UTF-8 BOM。

    为什么单列一类：BOM 是**隐形**的，肉眼 review 看不出来，而后果可以很重 ——
    2026-10-04 本机 `~/.dsh/storages/workspace.json` 被 PowerShell 写进 BOM，
    DSH 的 JSON 存储后端直接抛错，`workspace` 插件启动失败、整个工作区列表消失。
    同类事故还发生过一次（profile package.json 带 BOM → desktop 变「不可选」）。

    本机 shell 名义上是 pwsh、实际是 **Windows PowerShell 5.1**，
    它的 `Set-Content / Out-File -Encoding UTF8` **一定**写 BOM ——
    所以这条只能靠自动检查，不能靠人记得。
    """
    print("\n[D] 编码卫生（UTF-8 BOM）")
    bad, scanned = [], 0
    for p in iter_files():
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        try:
            with open(p, "rb") as fh:
                head = fh.read(3)
        except OSError:
            continue
        scanned += 1
        if head == b"\xef\xbb\xbf" and rel not in BOM_ALLOWED:
            bad.append(rel)
    check(f"扫描 {scanned} 个文本文件，没有意外的 UTF-8 BOM",
          not bad, "带 BOM：" + ", ".join(bad))
    if bad:
        print("        ⚠ 去掉 BOM 用 Python 写文件，别用 PowerShell：")
        print("          python -c \"p=r'<文件>'; s=open(p,encoding='utf-8-sig')"
              ".read(); open(p,'w',encoding='utf-8',newline='\\n').write(s)\"")
    for rel in sorted(BOM_ALLOWED):
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if os.path.exists(p):
            with open(p, "rb") as fh:
                ok = fh.read(3) == b"\xef\xbb\xbf"
            # 例外文件**反过来**要求有 BOM：没有的话中文会乱码
            check(f"{rel} 保留 BOM（PS 5.1 需要它才能读中文）", ok)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print("=" * 72)
    print("发布前综合审计：隐私 / 路径 / 安装 / 编码")
    print("=" * 72)
    print(f"  项目根：{ROOT}")
    audit_privacy()
    audit_paths()
    audit_install()
    audit_encoding()
    print()
    print("=" * 72)
    if PROBLEMS:
        print(f"通过 {PASS}　失败 {FAIL}")
        print("\n要处理的：")
        for x in PROBLEMS:
            print(f"  - {x}")
        return 1
    print(f"全部通过（{PASS} 项）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
