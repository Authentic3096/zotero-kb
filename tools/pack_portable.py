"""打一个给普通用户用的便携运行包（Windows）。

    python tools/pack_portable.py
    python tools/pack_portable.py --out dist
    python tools/pack_portable.py --no-xpi-build   # xpi 必须已存在

产物：dist/zotero-kb-<版本>-win64.zip

包里只放**运行必需**的东西：
    scripts/            安装、构建、更新、技能同步脚本
    offline/            离线管道（读 Zotero、切片、向量、经验……）
    online/             无端口引擎、MCP 服务器、检索（插件按需拉起）
    tools/make_index.py 构建收尾生成 INDEX.md（可选，失败不影响索引）
    requirements.txt
    skills/             两份配套 Skill
    README.md / INSTALL.md / QUICKSTART.md / VERSION / LICENSE
    zotero-plugin/zotero-kb-<版本>.xpi

为什么不只放 offline/：Zotero 插件用 `online/engine.py` 与 `offline/schemas.py`
判断"项目根"，并从 `online/engine.py` 拉起 Python 引擎；MCP 也在
`online/server.py`。只带 offline/ 会得到一个"能建库但面板和检索都用不了"的包。

刻意排除：.git / .venv / .tools / .mineru / kb / tests / docs / video /
node_modules / __pycache__ / 任意本机数据与配置。

打包是**可重复**的：条目按路径排序、时间戳固定，同样的输入得到同样的 zip。
"""

from __future__ import annotations

import ast
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
DIST = os.path.join(ROOT, "dist")
VERSION_FILE = os.path.join(ROOT, "VERSION")
MANIFEST = os.path.join(PLUGIN, "manifest.json")

TOP_PREFIX = "zotero-kb-{version}-win64"

SCRIPT_FILES = (
    "install-env.cmd",
    "install-env.ps1",
    "1-convert.cmd",
    "sync-skill.ps1",
    "update.cmd",
    "update.ps1",
)

ROOT_FILES = (
    "requirements.txt",
    "README.md",
    "INSTALL.md",
    "QUICKSTART.md",
    "VERSION",
    "LICENSE",
    # README 顶部链接了这一份；只多收这一个小文件，其余 docs/ 仍不进包。
    "docs/RELEASE-2.0.7.md",
)

SKILL_DIRS = ("skills/zotero-kb", "skills/zotero-acquire")

TEXT_EXTS = {".py", ".md", ".txt", ".cmd", ".ps1", ".json", ".yml", ".yaml"}
ALLOWED_EXTS = TEXT_EXTS | {".xpi", ".svg", ".png", ".ico", ".ftl"}

ALLOWED_THIRD_PARTY = {
    "fitz", "pymupdf", "numpy", "fastembed", "latex2mathml", "mcp",
    "zstandard", "PIL", "onnxruntime", "huggingface_hub", "tokenizers",
}

# 真实开发机痕迹 / 用户目录：绝不允许出现在便携包里。
# 注意：文档里允许出现 C:\Users\<你> 这种**占位符**，所以用户目录按
# "后面跟着真实用户名"来匹配，而不是见到 C:\Users\ 就报。
DEV_PATH_RE = re.compile(
    r"(?i)C:[\\/]+Users[\\/]+(?!<|%|\.\.\.|Public)[A-Za-z0-9_.\-]+")
DEV_OTHER_RE = re.compile(
    r"(?i)(?:[CD]:[\\/]DSHwork|C:[\\/]Python314|/home/[A-Za-z0-9_.\-]+)")

FORBIDDEN_NAMES = {
    ".env", "kb-location.json", "llm-config.json", "index.db",
    "zotero.sqlite", "plugin-status.json",
}
FORBIDDEN_DIRS = {
    ".git", ".venv", ".tools", ".mineru", "kb", "tests", "docs",
    "video", "node_modules", "__pycache__", "dist", "data",
}
FORBIDDEN_EXTS = {
    ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
    ".pdf", ".zst", ".jsonl", ".log", ".zip",
}


def say(msg: str = "") -> None:
    print(msg)


def die(msg: str) -> "NoReturn":
    print(f"[XX] {msg}")
    raise SystemExit(1)


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def read_version() -> str:
    if not os.path.exists(VERSION_FILE):
        die("缺 VERSION 文件（内容应等于 manifest.json 的 version）")
    with open(VERSION_FILE, encoding="utf-8") as fh:
        v = fh.read().strip()
    m = str(read_json(MANIFEST).get("version") or "").strip()
    if not v:
        die("VERSION 是空的")
    if v != m:
        die(f"VERSION（{v}）与 manifest.json（{m}）不一致")
    return v


def ensure_xpi(version: str, allow_build: bool = True) -> str:
    path = os.path.join(PLUGIN, f"zotero-kb-{version}.xpi")
    if os.path.exists(path):
        return path
    if not allow_build:
        die(f"找不到 {os.path.relpath(path, ROOT)} —— 先跑 python tools/check_plugin.py")

    say(f"  没找到 xpi，先跑 tools/check_plugin.py 构建 {os.path.basename(path)} …")
    rc = subprocess.run([sys.executable, os.path.join(HERE, "check_plugin.py")],
                        cwd=ROOT).returncode
    if rc != 0:
        die("check_plugin.py 失败，未生成 xpi")
    if not os.path.exists(path):
        die("check_plugin.py 跑完了，但仍没有生成 " + os.path.basename(path))
    return path


def collect_files(version: str) -> list[tuple[str, str]]:
    """返回 [(zip 内相对路径, 本机源路径)]。"""
    items: list[tuple[str, str]] = []

    def add(rel: str, src: str) -> None:
        rel = rel.replace(os.sep, "/")
        if not os.path.isfile(src):
            die(f"缺文件：{os.path.relpath(src, ROOT)}")
        items.append((rel, src))

    for name in SCRIPT_FILES:
        add(f"scripts/{name}", os.path.join(ROOT, "scripts", name))

    for sub in ("offline", "online"):
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            die(f"缺目录：{sub}")
        for fn in sorted(os.listdir(base)):
            if not fn.endswith(".py"):
                continue
            src = os.path.join(base, fn)
            add(f"{sub}/{fn}", src)

    add("tools/make_index.py", os.path.join(ROOT, "tools", "make_index.py"))

    for name in ROOT_FILES:
        add(name, os.path.join(ROOT, name))

    for rel_dir in SKILL_DIRS:
        base = os.path.join(ROOT, rel_dir.replace("/", os.sep))
        if not os.path.isdir(base):
            die(f"缺技能目录：{rel_dir}")
        found = 0
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for fn in sorted(files):
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, ROOT).replace(os.sep, "/")
                add(rel, src)
                found += 1
        if not found:
            die(f"技能目录是空的：{rel_dir}")

    xpi = ensure_xpi(version)
    add(f"zotero-plugin/{os.path.basename(xpi)}", xpi)

    return sorted(items, key=lambda x: x[0])


def module_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for sub in ("offline", "online", "tools"):
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            continue
        for fn in os.listdir(base):
            if fn.endswith(".py"):
                out[fn[:-3]] = os.path.join(base, fn)
    return out


def check_import_closure(selected: set[str]) -> None:
    """确认入口模块 import 的本地模块都在包里。

    只做"本地模块有没有漏"这一件事；第三方依赖由 requirements.txt 负责。
    """
    mapping = module_map()
    entries = ("convert", "engine", "server", "make_index")
    seen: set[str] = set()
    stack = [m for m in entries if m in mapping]
    unknown: set[str] = set()

    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = mapping[mod]
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except SyntaxError as exc:
            die(f"模块语法错误：{os.path.relpath(path, ROOT)}: {exc}")

        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif (isinstance(node, ast.ImportFrom) and node.level == 0
                  and node.module):
                names = [node.module.split(".")[0]]
            for name in names:
                if name in mapping:
                    stack.append(name)
                elif (name not in sys.stdlib_module_names
                      and name not in ALLOWED_THIRD_PARTY):
                    unknown.add(name)

    not_included = sorted(m for m in seen if m not in selected)
    if not_included:
        die("运行时模块没有被打进包：" + ", ".join(not_included))
    if unknown:
        say("  [!!] 发现未登记的第三方 import（确认 requirements.txt 有）："
            + ", ".join(sorted(unknown)))
    say(f"  [OK] import 闭包：{len(seen)} 个本地模块都在包里")


# docs/ 整体不进包，只允许 README 直接链接的这一份发布说明。
ALLOWED_DOC_FILES = {"docs/release-2.0.7.md"}


def check_paths(allowed_xpi: str) -> None:
    forbidden_dirs = {d.lower() for d in FORBIDDEN_DIRS}
    for rel, src in FILES:
        low = rel.lower()
        parts = low.split("/")
        bad = [] if low in ALLOWED_DOC_FILES else [
            p for p in parts[:-1] if p in forbidden_dirs]
        if bad:
            die(f"包里混进了不该有的目录：{rel}")
        if parts[-1] in FORBIDDEN_NAMES:
            die(f"包里混进了本机数据/配置：{rel}")
        ext = os.path.splitext(parts[-1])[1].lower()
        if ext in FORBIDDEN_EXTS:
            die(f"包里混进了不该有的文件类型：{rel}")
        if rel != f"zotero-plugin/{os.path.basename(allowed_xpi)}" and ext == ".xpi":
            die(f"发现多余的 xpi：{rel}")
        if os.path.isabs(rel) or ".." in parts:
            die(f"非法条目路径：{rel}")
        if ext not in ALLOWED_EXTS and parts[-1] not in {"license", "version"}:
            die(f"不在允许清单里的文件类型：{rel}")


def scan_text(rel: str, data: bytes) -> None:
    low = rel.lower()
    ext = os.path.splitext(low)[1]
    if ext not in TEXT_EXTS:
        return
    try:
        txt = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        die(f"文本文件不是合法 UTF-8：{rel}")
    for pat in (DEV_PATH_RE, DEV_OTHER_RE):
        m = pat.search(txt)
        if m:
            die(f"{rel} 含开发机路径/用户目录痕迹：{m.group(0)}")


def build_zip(items: list[tuple[str, str]], version: str, out_path: str) -> None:
    top = TOP_PREFIX.format(version=version)
    date_time = (2026, 1, 1, 0, 0, 0)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, src in items:
            arc = f"{top}/{rel}"
            with open(src, "rb") as fh:
                data = fh.read()
            scan_text(rel, data)
            info = zipfile.ZipInfo(arc, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.flag_bits |= 0x800
            info.external_attr = 0o644 << 16
            z.writestr(info, data)

    with zipfile.ZipFile(out_path) as z:
        bad = z.testzip()
        if bad:
            die(f"zip 自检失败：{bad}")
        names = z.namelist()
        if len(names) != len(items):
            die(f"zip 条目数不对：{len(names)} != {len(items)}")
        for arc in names:
            if not arc.startswith(top + "/"):
                die(f"条目不在顶层目录下：{arc}")
        xpi_arc = f"{top}/zotero-plugin/zotero-kb-{version}.xpi"
        if xpi_arc not in names:
            die("zip 里没有 xpi")
        with z.open(xpi_arc) as fh:
            xdata = fh.read()
        try:
            import io
            with zipfile.ZipFile(io.BytesIO(xdata)) as xz:
                xm = json.loads(xz.read("manifest.json").decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            die(f"xpi 读不了：{type(exc).__name__}: {exc}")
        if str(xm.get("version")) != version:
            die(f"xpi 版本（{xm.get('version')}）与包版本（{version}）不一致")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包普通用户便携运行包")
    ap.add_argument("--out", default=DIST, help="输出目录（默认 dist/）")
    ap.add_argument("--no-xpi-build", action="store_true",
                    help="xpi 必须已经构建，缺了就失败")
    args = ap.parse_args()

    global FILES
    version = read_version()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"zotero-kb-{version}-win64.zip")

    say("=" * 68)
    say(f"便携运行包 v{version}")
    say("=" * 68)

    xpi = ensure_xpi(version, allow_build=not args.no_xpi_build)
    items = collect_files(version)
    FILES = items
    check_paths(xpi)
    check_import_closure({os.path.splitext(os.path.basename(src))[0]
                          for rel, src in items
                          if rel.startswith(("offline/", "online/", "tools/"))})

    say("")
    say("清单：")
    total = 0
    last_group = ""
    for rel, src in items:
        group = rel.split("/")[0]
        if group != last_group:
            say(f"  [{group}]")
            last_group = group
        size = os.path.getsize(src)
        total += size
        say(f"    {rel:<48} {size:>9,} B")

    build_zip(items, version, out_path)
    zsize = os.path.getsize(out_path)
    digest = hashlib.sha256(open(out_path, "rb").read()).hexdigest()
    say("")
    say(f"  [OK] 生成：{os.path.relpath(out_path, ROOT)}")
    say(f"       文件数 {len(items)}　未压缩 {total:,} B　zip {zsize:,} B")
    say(f"       sha256 {digest}")
    say(f"       解压后目录：{TOP_PREFIX.format(version=version)}")
    return 0


FILES: list[tuple[str, str]] = []


if __name__ == "__main__":
    sys.exit(main())
