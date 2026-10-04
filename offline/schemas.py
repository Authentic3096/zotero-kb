"""知识库的共享数据结构与数据库表定义。

这是「离线部分」与「在线部分」之间唯一的接口定义：
    离线 convert.py 按这里的表结构与文件布局写，
    在线 server.py 按同样的约定读。
任何一边改结构，都必须同时改这个文件，并在 README 里记录迁移方式。

设计原则
    1. 条目的稳定标识是 Zotero 的 item key（8 位大写字母数字），不是自增 itemID。
       itemID 只在本库内部用于关联，出库一律用 key。
    2. 原文层与结论层只由离线写；经验层只由在线追加，永不覆盖。
    3. 页码用 `## p.N` 标记，方便在线侧按页切分返回。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 路径约定
#
# 这里有**四个互相独立**的位置，谁也不能从谁推算出来（推算是本机踩过的坑）：
#
#   KB_DIR      知识库（数据）  默认跟着 Zotero 数据目录走，为了能整包同步
#   KB_ROOT     项目目录（代码）  .venv / gui.py / 脚本 在哪儿
#   PYTHON_EXE  解释器           KB_ROOT\.venv 优先，但不一定在那儿
#   OLLAMA_EXE  本地模型程序     官方安装器装到 %LOCALAPPDATA%\Programs\Ollama
#
# 解析优先级统一是：环境变量 > 用户配置 > 自动探测 > 兜底。
# 用户配置读两个地方（先项目根、后用户级 —— 项目被挪走时还能靠用户级找回）：
#   1. <项目根>\kb-location.json      （老位置，兼容已有安装）
#   2. %APPDATA%\zotero-kb\location.json 或 ~\.config\zotero-kb\location.json
#
# ⚠ 曾经 `projectRoot()` 写成"kbDir 的上一级"。知识库一搬到 Zotero 数据目录，
#   就推成 D:\Application\ZoteroData\Zotero，那里没有 .venv，
#   面板于是报"找不到 py 环境"。结论：**不要从数据位置反推代码位置**。
# ⚠ 迁移目录**不会**破坏索引：实测索引里不存文件绝对路径
#   （papers/fulltext 是按 Zotero key 命名的，路径由 KB_DIR 推算）。
#   可用 `python tools\check_abs_paths.py` 复核。

KB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCATION_FILE = os.path.join(KB_ROOT, "kb-location.json")
USER_CONFIG_DIR = (
    os.path.join(os.environ["APPDATA"], "zotero-kb")
    if os.environ.get("APPDATA")
    else os.path.join(os.path.expanduser("~"), ".config", "zotero-kb")
)
USER_LOCATION_FILE = os.path.join(USER_CONFIG_DIR, "location.json")

# 解析结果缓存（进程内）——避免每次调用都去读盘、扫目录
_RESOLVED: dict[str, str] = {}

# Zotero 数据目录探测不到时的占位符。
# ⚠ 故意**留空**而不是写死本机路径：这个仓库要公开发布，
#   写死别人的目录既无用（在别人机器上不存在）又泄露信息。
#   探测不到时下游会给出"请设置 ZOTERO_DATA_DIR"的中文提示。
_ZOTERO_DATA_FALLBACK = ""


def _zotero_profile_dirs() -> list[str]:
    """列出可能的 Zotero profile 目录（用来找 prefs.js 里的 dataDir）。"""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    base = os.path.join(appdata, "Zotero", "Zotero", "Profiles")
    out = []
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "prefs.js")):
                out.append(p)
    except OSError:
        pass
    return out


def _read_zotero_data_dir() -> str:
    """从 Zotero 的 prefs.js 里读出数据目录（用户可能改过，不能写死）。"""
    import re
    for prof in _zotero_profile_dirs():
        try:
            with open(os.path.join(prof, "prefs.js"), encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        # user_pref("extensions.zotero.dataDir", "D:\\...\\Zotero");
        m = re.search(
            r'user_pref\(\s*"extensions\.zotero\.dataDir"\s*,\s*"((?:[^"\\]|\\.)*)"',
            text)
        if m:
            # prefs.js 里的反斜杠是转义的
            raw = m.group(1).replace("\\\\", "\\")
            if raw and os.path.isdir(raw):
                return raw
    return ""


def _read_location_config() -> dict:
    """读位置配置，**顺序敏感**：先项目根，后用户级。

    顺序不能反：项目根的配置是主要的（跟着仓库走），用户级的只是
    "项目被挪走了还能找回数据在哪"的后备。
    """
    for path in (LOCATION_FILE, USER_LOCATION_FILE):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data:
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def write_location_config(**kw: Any) -> str:
    """更新位置配置（合并写，不覆盖别的键）。

    先写项目根；那里不可写（只读安装、OneDrive 同步冲突等）就退到用户级。
    返回实际写入的文件路径。
    """
    cfg = dict(_read_location_config())
    for k, v in kw.items():
        if v is None:
            continue
        cfg[k] = v
    targets = [LOCATION_FILE, USER_LOCATION_FILE]
    last_err: Exception | None = None
    for path in targets:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            _RESOLVED.clear()
            return path
        except OSError as exc:
            last_err = exc
            continue
    raise OSError(f"位置配置写入失败（试过 {targets}）：{last_err}")


def _config_dir() -> str:
    """用户级配置目录（放运行时状态，例如上次探测到的 Ollama 路径）。"""
    return USER_CONFIG_DIR


def runtime_state_path() -> str:
    return os.path.join(USER_CONFIG_DIR, "runtime.json")


def read_runtime_state() -> dict:
    try:
        with open(runtime_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_runtime_state(**kw: Any) -> str:
    """写运行时状态。

    为什么和位置配置分开存：探测到的路径是**这台机器的状态**，不该提交到
    版本库；而 kb-location.json 是用户的**意图**，可能要跟着仓库走。
    """
    state = dict(read_runtime_state())
    state.update({k: v for k, v in kw.items() if v is not None})
    path = runtime_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    _RESOLVED.clear()
    return path


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def _scan_for_python(root: str | None = None) -> list[str]:
    """找解释器，按"最适合本项目"排序。

    顺序有讲究：**pythonw.exe 优先** —— 它不弹控制台窗口，起本地服务/
    管理面板时体验完全不同（用 python.exe 会闪一个黑框）。

    `root` 为 None 时返回"纯系统级"候选（PATH 上的），
    **不掺 KB_ROOT** —— 调用方要区分"项目内的"和"系统级的"：
    `resolve_python` 就是先用项目内的、再用系统级的。
    以前它无条件把 KB_ROOT 也算进去，导致"项目内扫描"永远能命中
    KB_ROOT 下的 venv —— 在"代码被复制到另一个目录"的场景里
    （沙箱测试就复现了）它会指向**原来那个**项目的解释器。
    """
    roots = [r for r in ([root] if root is not None else []) if r]
    if root is None:
        roots = []                       # 纯系统级
    out: list[str] = []
    seen: set[str] = set()
    for base in roots:
        for rel in (r".venv\Scripts\pythonw.exe", r".venv\Scripts\python.exe",
                    r".venv\bin\python", r"venv\Scripts\pythonw.exe",
                    r"venv\Scripts\python.exe", r"env\Scripts\pythonw.exe"):
            p = os.path.join(base, rel)
            k = os.path.normcase(p)
            if k not in seen and _is_executable(p):
                seen.add(k)
                out.append(p)
    # 系统级：PATH 上的 python / pythonw（用户自己装的）
    for name in ("pythonw.exe", "python.exe"):
        for d in (os.environ.get("PATH") or "").split(os.pathsep):
            d = d.strip('"').strip()
            if not d:
                continue
            p = os.path.join(d, name)
            k = os.path.normcase(p)
            if k not in seen and _is_executable(p):
                seen.add(k)
                out.append(p)
    return out


def _scan_for_ollama() -> list[str]:
    """找 Ollama 可执行文件。

    官方 Windows 安装器固定装到 `%LOCALAPPDATA%\\Programs\\Ollama`，
    所以这里先探那里；用户改过安装位置就靠 handle 配置或运行时状态兜。
    """
    out: list[str] = []
    seen: set[str] = set()
    cands: list[str] = []

    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        cands.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
        cands.append(os.path.join(local, "Ollama", "ollama.exe"))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env) or ""
        if base:
            cands.append(os.path.join(base, "Ollama", "ollama.exe"))
    cands.append(os.path.join(os.path.expanduser("~"), "AppData", "Local",
                              "Programs", "Ollama", "ollama.exe"))
    # PATH 上直接能找到（某些安装方式会加 PATH）
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        d = d.strip('"').strip()
        if d:
            cands.append(os.path.join(d, "ollama.exe"))
            cands.append(os.path.join(d, "ollama"))

    for p in cands:
        k = os.path.normcase(p)
        if k not in seen and _is_executable(p):
            seen.add(k)
            out.append(p)
    return out


def resolve_python(explicit: str = "") -> str:
    """决定用哪个 Python 解释器。

    优先级（每条都对应一个真实场景）：
      1. `explicit` / 环境变量 —— 调用方**明确要求**的，直接采信
      2. 配置里 `env.python` —— **用户在设置面板里显式选的位置**
         （比如"我就是把 venv 装在 D 盘别处"）→ 只要文件在就采信
      3. **本项目目录下**的 .venv —— 自动探测的默认答案
      4. 运行时状态里的 python —— 上次探测记下来的，**最可能陈旧**，
         所以排在自动探测之后
      5. 系统 PATH 上的 python
      6. 当前解释器

    ⚠ 为什么配置要分"env 段"和"运行时状态"两级，而不是一视同仁：
      · `env.python` 是**用户手动选的** —— 那是明确意图，该尊重；
      · `runtime.json` 是**程序自己写的缓存**（`write_runtime_state`），
        可能来自另一个项目目录。无条件信任它会导致"新目录里的代码
        去用旧目录的解释器" —— 本机在沙箱测试里就复现了：
        沙箱里跑，识别出的却是开发机的项目目录（而不是沙箱目录）。
    """
    cfg = _read_location_config()
    env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    state = read_runtime_state()
    root = resolve_project_root()

    # 1) 明确要求 / 环境变量
    for cand in (explicit, os.environ.get("ZOTERO_KB_PYTHON", "")):
        cand = (cand or "").strip()
        if cand and _is_executable(os.path.expanduser(cand)):
            return os.path.abspath(os.path.expanduser(cand))

    # 2) 用户在设置面板里显式选的。
    #    ⚠ 两种写法都要认，而且**同级**：
    #      · 扁平 `python_exe` —— 插件设置面板通过 /env-config 写的就是这个
    #        （`write_location_config(python_exe=...)`），是**主要格式**；
    #      · 嵌套 `env.python` —— 手写配置或旧版本留下的。
    #    第一版只把 env.python 放这一级、把扁平键降到第 4 级，
    #    结果"用户显式指定"被自动探测覆盖 —— 测试抓到了。
    for cand in (str(env_cfg.get("python") or ""),
                 str(cfg.get("python_exe") or "")):
        cand = (cand or "").strip()
        if cand and _is_executable(os.path.expanduser(cand)):
            return os.path.abspath(os.path.expanduser(cand))

    # 3) 本项目目录下的 .venv（自动探测；只含项目内的候选）
    project_cands = _scan_for_python(root)
    if project_cands:
        return project_cands[0]

    # 4) 运行时状态（程序自己写的缓存，最可能陈旧，排自动探测之后）
    for cand in (str(state.get("python") or ""),):
        cand = (cand or "").strip()
        if cand and _is_executable(os.path.expanduser(cand)):
            return os.path.abspath(os.path.expanduser(cand))

    # 5) 系统级候选
    for cand in _scan_for_python(None):
        if _is_executable(cand):
            return cand
    # ⚠ 这一行要 `import sys`。**它以前没有 import**，而且一直没暴露：
    #   本机 Windows 上第 1~3 步总能找到 .venv / 项目里的 Python，走不到这里；
    #   而在 CI（Linux）上必然走到 —— 模块级 `PYTHON_EXE = resolve_python()`
    #   于是直接 `NameError: name 'sys' is not defined`，连累所有 import schemas
    #   的检查（2026-10-05 发布 v1.0.0 时被插件自检逮到）。
    return sys.executable or "python"


def _looks_like_this_project(path: str) -> bool:
    """这个目录是不是"**当前正在运行的这个项目**"？

    判据不是"像不像一个项目"（那样任何一份拷贝都会通过），
    而是**这里的 offline/schemas.py 是不是正在跑的这个文件**
    —— 用 inode/同路径比对（`os.path.samefile`）。

    为什么必须这么严：
      · `kb-location.json` 和用户级 `location.json` 都可能记着**别人/别处**的
        项目根。只要那个目录存在（它当然存在，是另一个拷贝），
        "看着像项目"的判断就会通过 —— 于是新目录里的代码去用了
        旧目录的解释器。本机沙箱测试复现的正是这个：
        沙箱里跑，识别出的却是开发机的项目目录（而不是沙箱目录）。
      · 用 samefile 之后，"另一份拷贝"自然被判为不是本项目，
        而"项目就在这个目录里"这种正当情况仍然通过。
    """
    if not path or not os.path.isdir(path):
        return False
    if not all(os.path.isdir(os.path.join(path, d))
               for d in ("offline", "online")):
        return False
    me = os.path.abspath(__file__)
    there = os.path.join(path, "offline", "schemas.py")
    try:
        return os.path.samefile(me, there)
    except OSError:
        # 拿不到（权限/不存在）时退回路径比对
        return os.path.normcase(os.path.abspath(there)) == os.path.normcase(me)


def resolve_project_root(explicit: str = "") -> str:
    """决定项目目录（代码、.venv、脚本所在）。

    **绝不从知识库位置反推** —— 数据跟着 Zotero 走，代码留在项目目录，
    两者本来就没关系（见文件头的说明）。

    ⚠ 配置里的 project_root 要**验证是不是本项目**再用。
      踩过的坑：`kb-location.json` 是跟着仓库走的，如果它是从
      别人机器上复制过来的（或从一个项目目录复制到另一个），
      里面的 `project_root` 是**别人/别处**的路径 —— 无条件信任的话，
      新目录里的代码会去用旧目录的解释器，表现是"装好了却识别不出来"。
      本机在沙箱测试里就复现了：沙箱里跑，识别出的却是开发机的项目根。
    """
    cfg = _read_location_config()
    env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    state = read_runtime_state()

    for cand in (explicit,
                 os.environ.get("ZOTERO_KB_ROOT", ""),
                 str(env_cfg.get("project_root") or ""),
                 str(cfg.get("project_root") or ""),
                 str(state.get("project_root") or "")):
        cand = (cand or "").strip()
        if not cand:
            continue
        cand = os.path.abspath(os.path.expanduser(cand))
        if not os.path.isdir(cand):
            continue
        # explicit / 环境变量 / ZOTERO_KB_ROOT 是**本次调用方明确要求**的，
        # 直接采信；配置与运行时状态则是"可能已经过期"的，要验证。
        if cand in (os.path.abspath(os.path.expanduser(str(explicit or ""))),
                    os.environ.get("ZOTERO_KB_ROOT", "")):
            return cand
        if _looks_like_this_project(cand):
            return cand

    # 兜底 1：向上找带 markers 的目录（从本文件位置起 —— 本文件就在项目里）
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _ in range(4):
        if any(os.path.exists(os.path.join(here, m))
               for m in (".venv", "offline", "online", "zotero-plugin")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    # 兜底 2：知识库的上一级（老布局就是 <项目>\kb\，保留兼容）
    return os.path.dirname(os.path.abspath(KB_DIR.rstrip("\\/")))


def resolve_ollama(explicit: str = "") -> str:
    """决定 Ollama 可执行文件路径。找不到返回空串（它是可选组件）。"""
    cfg = _read_location_config()
    env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    state = read_runtime_state()

    for cand in (explicit,
                 os.environ.get("OLLAMA_EXE", ""),
                 str(env_cfg.get("ollama") or ""),
                 str(cfg.get("ollama_exe") or ""),
                 str(state.get("ollama") or "")):
        cand = (cand or "").strip()
        if cand and _is_executable(os.path.expanduser(cand)):
            return os.path.abspath(os.path.expanduser(cand))

    found = _scan_for_ollama()
    if found:
        # 记住它：下次就算 PATH 变了、环境变量没了也还能找到
        try:
            write_runtime_state(ollama=found[0])
        except OSError:
            pass
        return found[0]
    return ""


def figures_enabled(cli_flag: bool | None = None) -> bool:
    """要不要抽图注与表格（三级开关，默认开）。

    优先级：命令行显式指定 > 环境变量 `ZOTERO_KB_FIGURES` >
    `kb-location.json` 的 `figures` 字段 > 默认 True。

    为什么做成三级而不是一个布尔常量：抽图表要读 PDF，会给构建**多加
    约 60 秒**（101 篇实测）。命令行用于"这一次不要"，环境变量用于脚本，
    配置文件用于"我一直不要"（面板「运行环境」页写它）。
    """
    if cli_flag is not None:
        return bool(cli_flag)
    env = (os.environ.get("ZOTERO_KB_FIGURES") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        cfg = _read_location_config()
        v = cfg.get("figures")
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip():
            return v.strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        pass
    return True


def _resolve_kb_dir() -> str:
    """按优先级决定知识库目录。"""
    cfg = _read_location_config()

    # 1) 环境变量
    env = (os.environ.get("KB_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))

    # 2) 配置文件（可能是相对路径 —— 相对项目根解析）
    raw = str(cfg.get("kb_dir") or "").strip()
    if raw:
        raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            raw = os.path.join(KB_ROOT, raw)
        return os.path.abspath(raw)

    # 3) 跟随 Zotero 数据目录
    zdir = _resolve_zotero_data_dir()
    if zdir and cfg.get("follow_zotero", True) is not False:
        return os.path.join(zdir, "zotero-kb")

    # 4) 默认（老位置）
    return os.path.join(KB_ROOT, "kb")


def _resolve_zotero_data_dir() -> str:
    """Zotero 数据目录：环境变量 > 配置文件 > 从 Zotero prefs 探测 > 空。"""
    env = (os.environ.get("ZOTERO_DATA_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    cfg = _read_location_config()
    raw = str(cfg.get("zotero_data_dir") or "").strip()
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return _read_zotero_data_dir()


KB_DIR = _resolve_kb_dir()
PAPERS_DIR = os.path.join(KB_DIR, "papers")
FULLTEXT_DIR = os.path.join(KB_DIR, "fulltext")
# 分级视图（摘要与要点 / 图注与表格 / 权重与经验），由 offline/kbviews.py 生成。
# 和 papers/ fulltext/ 一样是**可重建的派生物**：删了跑一次构建就回来。
VIEWS_DIR = os.path.join(KB_DIR, "views")
INBOX_DIR = os.path.join(KB_DIR, "inbox")
CACHE_DIR = os.path.join(KB_DIR, ".cache")
INDEX_DB = os.path.join(KB_DIR, "index.db")
MANIFEST = os.path.join(KB_DIR, "MANIFEST.json")

# Zotero 侧
ZOTERO_DATA_DIR = _resolve_zotero_data_dir() or _ZOTERO_DATA_FALLBACK
ZOTERO_DB = os.path.join(ZOTERO_DATA_DIR, "zotero.sqlite")
ZOTERO_STORAGE = os.path.join(ZOTERO_DATA_DIR, "storage")

# 运行环境侧（代码在哪、用哪个解释器、Ollama 在哪）
# ⚠ 这三个都**不能用 KB_DIR 推算**，见文件头的说明。
PROJECT_ROOT = resolve_project_root()
PYTHON_EXE = resolve_python()
OLLAMA_EXE = resolve_ollama()

# DSH 侧（供 learn.py 扫会话记录）
DSH_SESSIONS = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")

# ---------------------------------------------------------------- 常量

# Zotero 会给"全文索引"生成一个标题为 Full Text PDF 的合成附件条目。
# 把它当文献展示会得到一堆同名条目，所以显式识别出来。
SYNTHETIC_TITLE = "Full Text PDF"

# 标注类型：Zotero 用数字编码，1 = 高亮，5 = 高亮里的"文本标注"（近似）
ANNOTATION_TYPES = {
    1: "highlight",
    2: "underline",
    3: "note",
    4: "image",
    5: "text",
    6: "ink",
}

# 展示优先级：这几个字段最关键
KEY_FIELDS = (
    "title",
    "abstractNote",
    "date",
    "publicationTitle",
    "DOI",
    "url",
    "language",
    "volume",
    "issue",
    "pages",
    "publisher",
    "institution",
    "university",
    "thesisType",
    "conferenceName",
    "bookTitle",
    "extra",
)

# 抓取"所有字段"时按这个顺序排列，其余字段追加在后
FIELD_ORDER = KEY_FIELDS


# ---------------------------------------------------------------- 记录结构


@dataclass
class Author:
    """一位作者。fieldMode=1 时姓名整体存在 lastName，中文名多为这种。"""

    first: str = ""
    last: str = ""
    creator_type: str = "author"
    order: int = 0

    @property
    def display(self) -> str:
        if self.last and not self.first:
            return self.last
        if self.first and not self.last:
            return self.first
        return f"{self.last} {self.first}".strip()

    @property
    def western_short(self) -> str:
        """参考文献里常见的姓+缩写形式，用于紧凑展示。"""
        if self.last and not self.first:
            return self.last
        parts = [p for p in self.first.replace(".", " ").split() if p]
        initials = "".join(p[0].upper() + "." for p in parts)
        return f"{self.last} {initials}".strip()


@dataclass
class Annotation:
    key: str
    type: str
    text: str
    comment: str
    color: str
    page: str


@dataclass
class Note:
    key: str
    html: str
    markdown: str
    image_refs: int = 0


@dataclass
class Attachment:
    key: str
    title: str
    content_type: str
    link_mode: int
    path: str          # 真实磁盘路径（已解析 storage: 前缀）
    exists: bool
    has_ft_cache: bool


@dataclass
class Item:
    """一篇文献（或网页、学位论文）的完整档案。"""

    item_id: int
    key: str
    item_type: str
    title: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    authors: list[Author] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)   # 分类名（不是 key）
    collection_keys: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    pdfs: list[Attachment] = field(default_factory=list)
    fulltext_pages: list[str] = field(default_factory=list)
    fulltext_source: str = ""          # "pymupdf" / "zotero-cache" / ""
    date_added: str = ""
    version: int = 0

    # ---------------------------------------------------------- 派生信息

    @property
    def year(self) -> str:
        """从 date 字段里抠出年份，抠不到就返回空。"""
        date = self.fields.get("date", "")
        for token in date.replace("/", "-").replace(".", "-").split("-"):
            token = token.strip()
            if len(token) == 4 and token.isdigit():
                return token
        for token in date.split():
            if len(token) == 4 and token.isdigit():
                return token
        return ""

    @property
    def abstract(self) -> str:
        return self.fields.get("abstractNote", "")

    @property
    def doi(self) -> str:
        return self.fields.get("DOI", "")

    @property
    def venue(self) -> str:
        for name in ("publicationTitle", "conferenceName", "bookTitle",
                     "university", "institution", "publisher"):
            value = self.fields.get(name)
            if value:
                return value
        return ""

    @property
    def first_author(self) -> str:
        for author in self.authors:
            if author.creator_type == "author":
                return author.display
        return self.authors[0].display if self.authors else ""

    @property
    def author_line(self) -> str:
        authors = [a for a in self.authors if a.creator_type == "author"] or self.authors
        return ", ".join(a.display for a in authors)

    @property
    def fulltext_chars(self) -> int:
        return sum(len(p) for p in self.fulltext_pages)

    @property
    def has_fulltext(self) -> bool:
        return bool(self.fulltext_pages)

    def paper_path(self) -> str:
        return os.path.join(PAPERS_DIR, f"{self.key}.md")

    def fulltext_path(self) -> str:
        return os.path.join(FULLTEXT_DIR, f"{self.key}.md")


# ---------------------------------------------------------------- 建表


SCHEMA_SQL = """
-- 条目的结论层：检索与展示都从这里出发
CREATE TABLE IF NOT EXISTS items (
    item_id       INTEGER PRIMARY KEY,
    key           TEXT UNIQUE NOT NULL,
    item_type     TEXT NOT NULL,
    title         TEXT,
    year          TEXT,
    first_author  TEXT,
    author_line   TEXT,
    venue         TEXT,
    doi           TEXT,
    abstract      TEXT,
    tags          TEXT,          -- JSON 数组
    collections   TEXT,          -- JSON 数组（分类名）
    n_pdfs        INTEGER DEFAULT 0,
    n_notes       INTEGER DEFAULT 0,
    n_annotations INTEGER DEFAULT 0,
    fulltext_chars INTEGER DEFAULT 0,
    fulltext_src  TEXT,          -- pymupdf / zotero-cache / ''
    date_added    TEXT,
    version       INTEGER,
    built_at      TEXT,
    -- 分类+标签的指纹。**必须单独存**：Zotero 改条目的分类或标签时
    -- 不会更新 items.version，只比 version 的话知识库察觉不到改动
    -- （实测：重整了 33 篇的分类归属，增量却报"0 条需要处理"）。
    collections_sig TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_year ON items(year);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type);

-- 切片：向量检索与片段定位的最小单位
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    INTEGER PRIMARY KEY,
    item_key    TEXT NOT NULL,
    page        INTEGER,          -- 起始页（1 基）
    seq         INTEGER,          -- 该条目内第几块
    text        TEXT NOT NULL,
    n_chars     INTEGER,
    FOREIGN KEY (item_key) REFERENCES items(key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_key);

-- 图注与表格（从 PDF 抽，见 offline/figures.py）
-- ⚠ 单独一张表而不是塞进 chunks：它们是**结构化**的（有编号、有行列），
--   混进正文切片会被切碎；而且表格 Markdown 往往很长，会挤掉正文。
CREATE TABLE IF NOT EXISTS figures (
    fig_id     INTEGER PRIMARY KEY,
    item_key   TEXT NOT NULL,
    page       INTEGER,
    kind       TEXT,          -- caption-image | caption-table | table
    label      TEXT,          -- 图1.1 / 表2.3 / Table 1
    text       TEXT,          -- 图注正文，或表格的 Markdown
    n_rows     INTEGER,
    n_cols     INTEGER,
    built_at   TEXT,
    FOREIGN KEY (item_key) REFERENCES items(key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_figures_item ON figures(item_key);
CREATE INDEX IF NOT EXISTS idx_figures_page ON figures(item_key, page);

-- 关键词层：FTS5。中文靠查询改写（逐字 AND）命中，见 online/query.py
-- 注意：这里只索引 text 一列，不声明 UNINDEXED 列 —— 带 UNINDEXED 的
-- 列会让 fts5 变成「内容表」形态，从而不支持 DELETE，增量重建会失败。
-- 关联方式是 rowid：写入时显式指定 rowid = chunks.chunk_id。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 向量层：float32 二进制块 + 维度，读出来直接喂 numpy
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id  INTEGER PRIMARY KEY,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL,
    model     TEXT NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
);

-- 经验层：只追加，永不覆盖
CREATE TABLE IF NOT EXISTS experience (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    asked      TEXT NOT NULL,     -- 当时的问题/目标
    context    TEXT,              -- 条件：频段、采样率、初值、数据集…
    item_keys  TEXT,              -- JSON 数组：这次涉及哪几篇
    method     TEXT,              -- 用了什么方法
    outcome    TEXT NOT NULL,     -- effective / ineffective / partial / unknown
    reason     TEXT,              -- 为什么有效或失败
    evidence   TEXT,              -- 指向本次尝试的产物路径或数字
    tags       TEXT,              -- JSON 数组
    source     TEXT NOT NULL,     -- dsh / llm / user
    session    TEXT               -- 来源会话（事后补时用）
);

CREATE INDEX IF NOT EXISTS idx_exp_created ON experience(created_at DESC);

-- 每篇的累积战力：检索加权用
CREATE TABLE IF NOT EXISTS item_weight (
    item_key    TEXT PRIMARY KEY,
    attempts    INTEGER DEFAULT 0,
    effective   INTEGER DEFAULT 0,
    ineffective INTEGER DEFAULT 0,
    partial     INTEGER DEFAULT 0,
    pinned      INTEGER DEFAULT 0,   -- 用户标了重点
    manual      REAL DEFAULT 0,      -- 人工加减分
    note        TEXT,                -- 为什么标重点
    updated_at  TEXT
);

-- 词元文档频率：某个词元（中文单字/英文词）在多少个切片里出现过。
-- 用来给检索做稀有度加权 —— 「偶」「极」这种字区分度极高，
-- 「磁」「方」这种字满库都是、几乎不含信息。没有这层加权，
-- 覆盖率打分会被常用字带偏（实测：查"机器学习模型"会把无关领域无损检测排到第 2）。
CREATE TABLE IF NOT EXISTS term_df (
    term  TEXT PRIMARY KEY,
    df    INTEGER NOT NULL
);

-- 构建元信息
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class LockedConnection(sqlite3.Connection):
    """允许跨线程使用的 SQLite 连接（线程检查关掉 + 操作锁）。

    为什么需要（踩过的坑，报错原文）：
        ProgrammingError: SQLite objects created in a thread can only be used in
        that same thread. The object was created in thread id 19388 and this is
        thread id 22912.

    MCP 服务器**不是单线程**的：DSH 每发一个 tools/call 就可能换一个线程执行我们的
    工具函数，而连接是启动时在主线程建的。表现是"第一次调用成功、之后偶发失败"。

    为什么只锁 `execute` 不够（第二轮的坑）：
        `execute()` 返回游标后就放锁的话，游标随后可能在别的线程正在执行查询时
        被 fetch —— 拿到 None，报
        `TypeError: 'NoneType' object is not subscriptable` /
        `IndexError: tuple index out of range`，且只在并发下偶发（约 1/8）。
        曾试过包一层"取完行才放锁"的游标代理，但 **Python 3.14 的
        sqlite3.Cursor 属性是只读的**（`AttributeError: ... is read-only`），
        包装根本不生效。

    最终做法：连接层只提供"允许跨线程"的能力（本类），
    真正的互斥交给调用方按**完整操作**加锁 —— 见 searcher.py 的 `_serialized`
    装饰器，它保证"执行 + 取行"整段不被打断。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 供调用方复用的操作锁（RLock：同一线程可重入，方便嵌套调用）
        self.dsh_lock = threading.RLock()


def connect(path: str = INDEX_DB) -> sqlite3.Connection:
    """打开索引库。

    WAL 模式供在线侧长期持有；连接做成 `LockedConnection`，
    因为 MCP 工具会在不同线程里被调用（见该类的说明）。
    """
    conn = sqlite3.connect(path, timeout=30.0, factory=LockedConnection,
                           check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def collections_sig(collection_keys, tags) -> str:
    """分类 + 标签的指纹。

    为什么需要它：Zotero 里改条目的**分类归属或标签**时不会更新
    `items.version`，只比 version 的增量判定会漏掉这类改动
    （本机实测：重整 33 篇的分类后，增量仍报"0 条需要处理"，
    知识库里的分类还停在旧值）。所以把这两项单独做指纹存下来一起比。

    用排序后拼接，保证顺序无关；分隔符用 \\x1f（不会出现在 key 里）。
    """
    a = sorted(str(x) for x in (collection_keys or []))
    b = sorted(str(x) for x in (tags or []))
    return "\x1f".join(a) + "\x1e" + "\x1f".join(b)


def add_figure_columns(conn) -> None:
    """给 items 补 figure/table 计数列（老库平滑升级）。

    ⚠ SQLite 没有 `ADD COLUMN IF NOT EXISTS`，所以先查 PRAGMA 再决定。
      直接 ALTER 会在第二次启动时报 "duplicate column name"。
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, typ in (("n_figures", "INTEGER"), ("n_tables", "INTEGER")):
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
            except Exception:  # noqa: BLE001
                pass
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补新列。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表**不会**加列，所以新字段
    必须在建表之后显式补（SQLite 支持 ADD COLUMN，成本极低）。
    """
    def columns(table: str) -> set[str]:
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    cols = columns("items")
    if "collections_sig" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN collections_sig TEXT")
    conn.commit()


def init_db(path: str = INDEX_DB) -> sqlite3.Connection:
    """建库建表。幂等，重复调用安全。"""
    for directory in (KB_DIR, PAPERS_DIR, FULLTEXT_DIR, VIEWS_DIR, INBOX_DIR,
                      CACHE_DIR):
        os.makedirs(directory, exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _migrate(conn)
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


# 可疑码位：藏文 / 天城文 / 阿拉伯 / 希伯来 / 孟加拉 / 泰文 —— 中英文学术
# PDF 里出现这些基本可以断定是字符映射错了。
#
# 本机实测（全库 111 篇扫描）：Zotero 的 `.zotero-ft-cache` 对
# 「非嵌入 Type0 + Identity-H / GBK-EUC-H」字体的映射会失败，产出
# 20%~41% 的这类字符（而同一份 PDF 用 PyMuPDF 提取是 0.0%、通顺中文）。
#
# ⚠ 放在 schemas 这个基础层，而不是 check_chunks：`zreader` 是更底层的
#   模块，让它反向 import 上层功能模块会造成依赖倒挂。
#   `check_chunks.SUSPECT_RANGES` 与本表同源，改一处要同步另一处。
SUSPECT_RANGES = [
    (0x0900, 0x097F),   # 天城文
    (0x0980, 0x09FF),   # 孟加拉
    (0x0F00, 0x0FFF),   # 藏文
    (0x0600, 0x06FF),   # 阿拉伯
    (0x0590, 0x05FF),   # 希伯来
    (0x0E00, 0x0E7F),   # 泰文
]

_CJK_RE = None
_LATIN_RE = None


def _cjk_re():
    global _CJK_RE
    if _CJK_RE is None:
        import re

        _CJK_RE = re.compile(r"[\u4e00-\u9fff]")
    return _CJK_RE


def _latin_re():
    global _LATIN_RE
    if _LATIN_RE is None:
        import re

        _LATIN_RE = re.compile(r"[A-Za-z]")
    return _LATIN_RE


def char_stats(text: str) -> dict:
    """字符构成统计（缓存体检用）。

    返回 total / cjk / latin / exotic 四个计数与三个占比。
    占比以【非空白字符】为分母 —— 页眉页脚与排版空白会稀释比例，
    用总长度做分母会让判据在不同排版间漂移。

    ⚠ latin_ratio 的作用是判断"这篇是不是外文文献"：
      实测教训 —— 不能用 item.title 判断语言，因为用户会给英文文献
      起中文标题（本机 5 篇 IEEE 论文的标题就是中文，正文全英文），
      那会让"CJK 偏低"判据把它们冤枉成坏缓存，
      而其中一篇的 PyMuPDF 只提到缓存 13% 的字 —— 换过去反而更糟。
      缓存自身的拉丁字母占比不会骗人。
    """
    t = "".join((text or "").split())
    total = len(t)
    if not total:
        return {"total": 0, "cjk": 0, "latin": 0, "exotic": 0,
                "exotic_ratio": 0.0, "cjk_ratio": 0.0, "latin_ratio": 0.0}
    cjk = len(_cjk_re().findall(t))
    latin = len(_latin_re().findall(t))
    exotic = sum(1 for c in t if any(a <= ord(c) <= b for a, b in SUSPECT_RANGES))
    return {
        "total": total, "cjk": cjk, "latin": latin, "exotic": exotic,
        "exotic_ratio": exotic / total, "cjk_ratio": cjk / total,
        "latin_ratio": latin / total,
    }


# ---------------------------------------------------------------- 解析健康度
#
# 判"这一篇的正文提取是不是失败了"。粒度是**文献级**，不是切片级 ——
# 本机实测数据支持这个选择（见会话目录 `_recon/dist_bad.py` / `probe_health.py`）：
#
#   · 实测现象是"字体编码坏掉"，那是**整份 PDF** 的事：坏文本整篇都坏
#     （`MBCD29B4` 全篇 `&DOFXODWLRQ DQG 6LPXODWLRQ`），好文本整篇都好
#   · 切片级判定（3681 次）在正常文本上误判约 60%，误判集中在
#     "公式矩阵 / 参考文献 / 封面 / 表格看着零碎"这类**正常学术文本形态**上
#     —— 拿"读不出完整句子"当坏的标准，本来就会把这些全打成坏
#   · 全库 97 篇有正文的文献里，客观判据下真正有问题的只有 1 篇
#
# 所以：判据只抓"整篇提取失败"，不碰"这一段看着零碎"。

# 常见英文短词。正常英文学术文本里它们占拉丁词的 20%~40%；
# 字符偏移的文本里接近 0%（`&DOFXODWLRQ DQG 6LPXODWLRQ RI 0DJQHWLF`
# 一个都不命中，位移还原后立刻回到 32.8%）。
COMMON_EN_WORDS = (
    "the", "and", "of", "to", "in", "is", "are", "was", "were", "for",
    "with", "that", "this", "as", "on", "by", "be", "an", "we", "it",
    "from", "at", "which", "can", "has", "have", "not", "but", "or",
    "its", "their", "our", "these", "than", "also", "such", "may",
)

# 位移还原的判据阈值（都来自实测，不是拍的）
SHIFT_MIN_WORDS = 200      # 拉丁词少于这个数，位移评分不可信（54 词的中文文献
                           # 位移后能"碰"到 25%，纯属巧合）
SHIFT_MIN_LATIN = 0.50     # 只对外文文献做 —— 中文文献 common_ratio 天然是 0
SHIFT_SUSPECT = 0.12       # 常见词率低于此值才值得去试位移
SHIFT_ACCEPT = 0.15        # 位移后要达到此值才算"还原成功"（正常英文 ≥20%）
SHIFT_MIN_GAIN = 0.10      # 而且至少比原来高这么多
SHIFT_SPAN = 40            # 试 ±40 的位移就够（实测那篇是 +29）

_EN_WORD_RE = None
_LATIN_WORD_RE = None


def _latin_word_re():
    global _LATIN_WORD_RE
    if _LATIN_WORD_RE is None:
        import re

        _LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
    return _LATIN_WORD_RE


def _en_word_re():
    global _EN_WORD_RE
    if _EN_WORD_RE is None:
        import re

        _EN_WORD_RE = re.compile(r"\b(" + "|".join(COMMON_EN_WORDS) + r")\b",
                                 re.I)
    return _EN_WORD_RE


def en_common_ratio(text: str) -> tuple[float, int]:
    """常见英文短词占拉丁词的比例，返回 (比例, 拉丁词数)。

    这是"这段英文明不明白"的客观代理量：正常英文学术文本 20%~40%，
    字符偏移的乱码 ~0%。**只对拉丁词足够多的文本有意义** ——
    中文文献里零星几个英文词会算出误导性的比例，调用方要先用
    `char_stats().latin_ratio` 挡一道。
    """
    t = text or ""
    words = _latin_word_re().findall(t)
    n = len(words)
    if not n:
        return 0.0, 0
    return len(_en_word_re().findall(t)) / n, n


def deshift(text: str, shift: int) -> str:
    """把文本里所有可打印 ASCII 字符整体位移 `shift` 位。

    实测的坏文本正是这个形态：源 PDF 的字体没有可用的 ToUnicode 映射，
    提取出来的每个字符都比真实字符小 29：

        &DOFXODWLRQ DQG 6LPXODWLRQ   --(+29)-->   Calculation and Simulation

    ⚠ 必须对**所有**可打印 ASCII 做位移，不能只对字母：上面那串里的
      `&`→`C`、`6`→`S`、`0`→`M`、`'`→`D`、`=`→`Z` 全不是字母。
      第一版只移字母，还原出来是 `&alculation and 6imulation` —— 看着"差不多"，
      其实判据就废了。移位后落到不可打印区间的字符保持原样。

    ⚠ **空格不动**（0x20 排除在位移范围外）：实测坏文本里空格还是空格
      （`&DOFXODWLRQ DQG`，不是 `&DOFXODWLRQ=DQG`），
      而 0x20 + 29 正好会变成 `=`。把空格一起移会把词边界毁掉。

    ⚠ 标点会还原得不准：实测那份 PDF 里逗号被提取成了句点（差 2 而不是 29），
      还原时 `.` 会变成 `K`。所以**还原后的文本是"词对了、个别标点错"** ——
      对它做检索和阅读都没问题（偏移原文是 100% 不可读），
      但不要指望它和原 PDF 逐字符一致。
    """
    if not shift:
        return text
    out = []
    for ch in text:
        o = ord(ch)
        if 0x21 <= o <= 0x7E:          # 排除空格：见上面的说明
            n = o + shift
            out.append(chr(n) if 0x21 <= n <= 0x7E else ch)
        else:
            out.append(ch)
    return "".join(out)


def find_shift(text: str, span: int = SHIFT_SPAN,
               sample: int = 6000) -> tuple[int, float, float]:
    """找让文本"最像正常英文"的那个位移。返回 (位移, 原比例, 位移后比例)。

    没找到就返回 `(0, 原比例, 原比例)`。判断标准见上面几个 SHIFT_* 常量 ——
    **调用方要用 `shift_fix()` 而不是直接看这里的返回值**，
    因为"分数最高"不等于"该还原"（正常文本在某个位移下也能碰巧涨一点）。
    """
    before, n = en_common_ratio(text)
    if n < SHIFT_MIN_WORDS:
        return 0, before, before
    best_sh, best_v = 0, before
    for sh in range(-span, span + 1):
        if sh == 0:
            continue
        v, _ = en_common_ratio(deshift(text[:sample], sh))
        if v > best_v:
            best_sh, best_v = sh, v
    return best_sh, before, best_v


def shift_fix(text: str) -> tuple[int, str]:
    """如果这一篇是字符偏移文本，给出还原方案。返回 (位移, 还原后文本)。

    不满足下面**全部**条件就返回 `(0, 原文本)`：

        · 是外文文献（latin_ratio ≥ 0.50，中文文献天然算不出常见词率）
        · 常见词率 < 12%（正常英文 ≥20%）
        · 存在某个位移使常见词率 ≥15% 且比原来高 ≥10 个百分点

    实测对照（这是卡阈值用的真实数据）：

        MBCD29B4   0.2% → +29 → 32.8%   ✓ 还原（真坏）
        正常英文   24%~36% 各种位移都涨不上去        ✗ 不动
        FC4LNFGN   2.4% → +31 → 12.8%   涨了但没到位  ✗ 不动
        中文文献   词数不够，直接跳过                 ✗ 不动
    """
    if char_stats(text)["latin_ratio"] < SHIFT_MIN_LATIN:
        return 0, text
    before, n = en_common_ratio(text)
    if n < SHIFT_MIN_WORDS or before >= SHIFT_SUSPECT:
        return 0, text
    sh, _b, after = find_shift(text)
    if sh and after >= SHIFT_ACCEPT and (after - before) >= SHIFT_MIN_GAIN:
        return sh, deshift(text, sh)
    return 0, text


# 权重公式的唯一实现处：离线展示与在线排序必须用同一套，避免两边漂移
PINNED_BONUS = 3.0
EFFECTIVE_BONUS = 2.0
INEFFECTIVE_BONUS = 0.5
PARTIAL_BONUS = 1.0

# 按发表年份给的"基础权重"（recency base），区间 0.95~1.05。
#
# 为什么需要：库里 101 篇文献只有 8 篇在 item_weight 里有行，其余 93 篇的
# 权重被硬编码成 1.0 —— 也就是说"相关性相同的一篇 2010 年论文和一篇 2025 年
# 论文"排出来完全一样。这里给所有文献一个按年份的初始值。
#
# 设计取舍：
#   · 用【绝对年龄】而不是"库内最新/最老"做归一化。后者每加一篇新文献都会
#     改变全库旧文献的权重，同一篇文献的排序会莫名变化 —— 不可复现。
#   · **基准是"当前年份"，每次检索实时算，不落库** —— 所以它是动态的：
#     跨年后同一篇文献的 base 会自己往下走，不需要重建索引。
#     （唯一的前提是进程活着时 reload 过 items；新入库的文献若在长驻的
#      MCP 进程里查不到年份，会退化成中性的 1.0。）
#   · 15 年及更早一律压到 RECENCY_LO。
#   · 幅度只有 ±5%，刻意做得比经验权重小一个数量级（有效一次 ≈ +2.39），
#     所以它只负责"同等相关时新的略靠前"，绝不会盖过实证结论。
#   · 缺年份 → 1.0（中性），不猜、不惩罚。
RECENCY_SPAN_YEARS = 15
RECENCY_LO = 0.95
RECENCY_HI = 1.05


def _this_year() -> int:
    import datetime

    return datetime.date.today().year


def recency_base(year: Any, now_year: int | None = None) -> float:
    """按发表年份算基础权重，落在 [RECENCY_LO, RECENCY_HI]。

    今年的文献拿 RECENCY_HI，RECENCY_SPAN_YEARS 年前及更早拿 RECENCY_LO，
    中间线性过渡。年份缺失或明显不合理时返回 1.0。

    now_year 可注入，便于测试与"冻结时间"复现历史排序。
    """
    try:
        y = int(str(year).strip()[:4])
    except (TypeError, ValueError):
        return 1.0
    if y < 1800 or y > 2200:          # 明显不是年份（空串、乱码、误填）
        return 1.0
    age = (now_year if now_year is not None else _this_year()) - y
    if age <= 0:                       # 今年或未来（在线首发/预印本）
        return RECENCY_HI
    if age >= RECENCY_SPAN_YEARS:
        return RECENCY_LO
    frac = age / RECENCY_SPAN_YEARS
    return round(RECENCY_HI + (RECENCY_LO - RECENCY_HI) * frac, 6)


def raw_weight(
    row: dict[str, Any] | sqlite3.Row | None, base: float = 1.0
) -> float:
    """把 item_weight 的一行换算成基础权重（未取对数）。

    base 是按发表年份算的初始权重；没有经验记录的文献 row 为 None，
    此时仍然返回 base（这正是"基础权重不再全为 1"的落点）。
    """
    if row is None:
        return base
    get = (lambda k, d=0: row[k] if row[k] is not None else d)  # noqa: E731
    return (
        base
        + PINNED_BONUS * get("pinned")
        + float(get("manual", 0.0))
        + EFFECTIVE_BONUS * get("effective")
        + INEFFECTIVE_BONUS * get("ineffective")
        + PARTIAL_BONUS * get("partial")
    )


def weight_multiplier(
    row: dict[str, Any] | sqlite3.Row | None, base: float = 1.0
) -> float:
    """取对数后的乘数：既让有价值的文献前移，又避免某一篇垄断结果。

    零经验的文献乘数≈base（0.95~1.05，只体现发表年份的新旧）；
    有效 1 次 → 1+ln(3)≈2.10，重点标记 → 1+ln(4)≈2.39，
    有效 3 次 → 1+ln(7)≈2.95 —— 增长是递减的，不会让一篇垄断结果。

    注意 base 是加在 log 里面的：年份的影响被压缩到 ±5% 左右，
    而"经验有效一次"是 +139%，两者相差一个数量级 —— 这是刻意的，
    年份只做同等相关时的微调，不能盖过"这篇试过、有用"的实证。
    """
    import math

    return 1.0 + math.log(raw_weight(row, base))


# ---------------------------------------------------------------- 可读引用名
#
# 为什么需要：Zotero 的 item key 是 8 位大写字母数字（如 22X9PMR6），
# 唯一但完全认不出是哪篇。用户看检索结果、看目录时都需要"人话"。
#
# ⚠ 为什么不干脆用可读名当**文件名**：库里已经有重名 ——
#     · 3 篇同名《X射线检测分析技术在文物保护修复中的应用》
#     · 2 篇同名 "Magnetic Dipole Moment Determination…"
#   用标题当文件名会互相覆盖，**仍然必须靠 key 区分**。
#   所以结论是：文件保持 key 命名（也保证迁移后路径可推算），
#   可读名只做**显示**（外加一份给人看的清单 INDEX.md）。

_FILE_BAD = None


def _file_bad_re():
    global _FILE_BAD
    if _FILE_BAD is None:
        import re
        # Windows 文件名禁止的字符 + 控制字符
        _FILE_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    return _FILE_BAD


def clean_name(text: str) -> str:
    """把任意标题清成能放进文件名/表格的样子（不会用于文件名，但表格也要干净）。"""
    import re
    s = _file_bad_re().sub("", (text or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .")


def short_title(title: str, limit: int = 40) -> str:
    """标题截断：去掉副标题、按词边界断，**不切断英文单词**。

    ⚠ 第一版直接 `title[:limit]`，结果英文标题被切成
      "Supercapattery Ene"、"A Rapid Linear Loc" 这种半个词，
      看着像乱码。英文必须退到最后一个空格。
    """
    t = clean_name(title)
    if not t:
        return "(无标题)"
    # 副标题分隔符：中文全角冒号/破折号、英文冒号（后面通常是最不重要的部分）
    for sep in ("：", ":", "——"):
        if sep in t:
            head = t.split(sep)[0].strip()
            if len(head) >= 6:
                t = head
                break
    if len(t) <= limit:
        return t
    cut = t[:limit]
    # 含拉丁字母就退到词边界
    if any("a" <= c.lower() <= "z" for c in cut):
        sp = cut.rfind(" ")
        if sp >= max(6, limit - 16):
            cut = cut[:sp]
    return cut.rstrip(" ,，、;；") + "…"


def short_author(name: str) -> str:
    """作者名缩短：中文原样；西文 "Yucheng Jiao" → "Jiao Y."。

    为什么不能直接用 last：
      ⚠ 本机实测，Zotero 里中文作者的拼音**存进 lastName 的可能是整个名字** ——
        "Du Changping"、"Liu Xin-Gen"、"Ma Dingyi" 这种。直接取 last 会得到
        "Changping D." / "Xin-Gen L."，**姓和名反了**，比不缩写还糟。
      所以这里的规则是：
        · 含中文 → 原样（中文名本来就短）
        · 第一个词 ≤ 3 个字母 → 它几乎肯定是姓（Li / Du / Ma / Xu / An），
          拼成 "Du Changping" → "Du C."
        · 否则认为顺序是 "First Last"，取最后一个是姓 → "Yucheng Jiao" → "Jiao Y."
        这样 "Anjana P. M"、"Chen Xuning"、"Sharifi Ghasem" 都能落到合理形态。
    """
    a = clean_name(name)
    if not a:
        return ""
    if any("\u4e00" <= c <= "\u9fff" for c in a):
        return a
    # 把 "P." 这类已缩写的尾巴单独处理："Anjana P. M" → parts=[Anjana, P., M]
    parts = [p for p in a.split() if p]
    if len(parts) < 2:
        return a
    first = parts[0]
    rest = parts[1:]
    # 第一个词很短 → 它是姓（中文拼音名的常见存法）
    if len(first.rstrip(".")) <= 3:
        family, given = first, rest
    else:
        family, given = rest[-1], parts[:-1]
    inits = "".join(p[0].upper() + "." for p in given if p)
    return f"{family} {inits}".strip()


def human_ref(title: str, first_author: str = "", year: Any = "",
              limit: int = 40) -> str:
    """生成"人话"引用名：`作者 年份 · 短标题`。

    这是**显示用**的，不唯一（有重名文献），所以界面上要跟 [KEY] 一起出现。
    """
    a = short_author(first_author)
    y = str(year or "").strip()
    head = f"{a} {y}".strip() if (a or y) else ""
    t = short_title(title, limit)
    return f"{head} · {t}" if head else t


def human_label(title: str, first_author: str = "", year: Any = "",
                key: str = "", limit: int = 40) -> str:
    """带 key 的完整标签：`作者 年份 · 短标题  [KEY]`。

    界面上显示这个 —— 一眼认得出是哪篇，又保留 key 可以精确指定。
    """
    ref = human_ref(title, first_author, year, limit)
    return f"{ref}  [{key}]" if key else ref
