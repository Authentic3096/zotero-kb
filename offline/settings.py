"""`.env` 用户覆盖层：让用户自己填"跑不通的路径"（**不含密钥**）。

为什么要有它
    Windows 上设"进程环境变量"要开对话框、还要重启进程 —— 而最常需要修的
    恰恰是"python.exe 找不到"，修它的面板本身也要先找到 python.exe（鸡生蛋）。
    所以给路径/参数类再加一层写在文件里的覆盖：把 `.env` 放在仓库根
    （与 `kb-location.json` 同级），改完重启进程即可。

取值优先级（定死，全项目一份定义 —— 在 `schemas.py` 的 `LAYER_ORDER` 与
`_resolve_layered()` 里实现，本模块只提供两档取值函数）
    调用方指定 > 进程环境变量 > 运行时设置（面板写的 `kb-location.json` /
    用户级 `location.json`）> `.env` > 自动探测（含 `runtime.json` 这个程序
    自己写的缓存）> 内置默认

    ⚠ 2026-10-10 调整：**运行时设置（面板/用户级）排在 `.env` 之前**。
      原来的顺序是 `.env` 赢过面板，后果是"面板里改完却不生效" —— 一个陈旧的
      `.env` 键会一直盖住面板设置。面板是用户最后操作的地方，应该赢；而面板
      起不来时运行时设置通常为空/无效 → 自然回落 `.env`，救急场景仍成立。
      外层强制仍用**进程环境变量**表达（那一档没动）。
    ⚠ 本模块只负责 `.env` 这一档的解析与取值；上面那份完整顺序在
      `offline/schemas.py` 里实现（只此一份）。
    ⚠ 面板保存仍然写 `kb-location.json`（写入路径不变）—— `.env` 只是覆盖层，
      不由程序改写，由用户手写。

解析规则（自己写，不引 python-dotenv；就是下面那三十行）
    · KEY=VALUE，一行一条；# 开头整行注释，行尾" # ..."也算注释（引号里的 # 不算）
    · 值可带单/双引号（成对才剥），也可以不带；两侧空白剥掉、值里的空格保留
    · 兼容 UTF-8 BOM、CRLF/LF、%VAR%（Windows）与 $VAR 展开、~ 展开
    · 文件不存在 → 静默返回空（那不是错误，绝大多数用户根本不需要它）

边界（重要）：密钥不进 `.env`。token / API key 仍然走 `kb/llm-config.json`、
    `kb/service-token.txt` 这些运行时文件（它们是程序生成、已进 `.gitignore`
    的，不该拿来手填）。
"""

from __future__ import annotations

import os

# .env 与 kb-location.json 同级 —— 都在仓库根（本文件在 offline 下，往上两层）
KB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(KB_ROOT, ".env")

_cache = None
_cache_stamp = None


def path():
    """这个 .env 文件的完整路径（面板/日志显示用）。"""
    return ENV_FILE


def expand(value):
    """展开 %VAR% / $VAR 与 ~（路径里的空格原样保留）。"""
    return os.path.expanduser(os.path.expandvars(str(value)))


def _strip_comment(line):
    """去掉行尾注释：引号外的第一个 # 之后全部丢掉（引号内的 # 保留）。"""
    out = []
    quote = ""
    for ch in line:
        if ch in "\"'":
            quote = "" if quote == ch else (quote or ch)
        if ch == "#" and not quote:
            break
        out.append(ch)
    return "".join(out)


def _unquote(value):
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def load(force=False):
    """解析 .env 成 {键: 值}（进程内缓存；文件 mtime 变了自动重读）。

    文件不存在时返回空字典 —— 不抛异常、不记错误（见模块头说明）。
    """
    global _cache, _cache_stamp
    try:
        stamp = os.path.getmtime(ENV_FILE)
    except OSError:
        _cache, _cache_stamp = {}, None
        return _cache
    if _cache is not None and _cache_stamp == stamp and not force:
        return _cache
    data = {}
    try:
        # utf-8-sig 同时吃下"带 BOM"与"不带 BOM"两种存法
        with open(ENV_FILE, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    for line_raw in text.splitlines():         # splitlines 同时处理 CRLF / LF
        line = _strip_comment(line_raw).strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):          # 容忍 shell 风格（从别处抄来的）
            key = key[len("export "):].strip()
        if not key:
            continue
        data[key] = expand(_unquote(value))
    _cache, _cache_stamp = data, stamp
    return _cache


def raw(key):
    """只看 .env 这一层（不掺进程环境变量）；没有返回空串。"""
    return str(load().get(key) or "").strip()


def env_value(key):
    """只看**进程环境变量**这一档（外层强制）；没有返回空串。

    这是完整优先级里的第 2 档（第 1 档"调用方指定"由调用方自己处理）。
    """
    v = str(os.environ.get(key) or "").strip()
    return expand(v) if v else ""


def dotenv_value(key):
    """只看 **`.env`** 这一档（用户手写文件）；没有返回空串。

    它在完整优先级里排在"运行时设置（面板）"**之后** —— 取数时不能只看它，
    顺序在 `offline/schemas.py` 的 `_resolve_layered()` 里。
    """
    v = str(load().get(key) or "").strip()
    return expand(v) if v else ""


def get(key, default=""):
    """只取**最外层两档**：进程环境变量 > `.env` > default。

    ⚠ 这**不是**完整优先级 —— 它跳过了"运行时设置（面板）"那一档。只给
      那些没有面板设置来源的参数用（如 `OLLAMA_HOST` / `KB_LOCAL_MODEL`，
      它们只可能来自环境变量、`.env` 或内置默认）。路径类配置一律走
      `schemas.resolve_*`（那里按完整顺序取）。
    """
    for val in (os.environ.get(key), load().get(key)):
        v = str(val or "").strip()
        if v:
            return v
    return default


def source(key):
    """这个键的覆盖层来源：环境变量 / .env / 空串（没被覆盖）。"""
    if str(os.environ.get(key) or "").strip():
        return "环境变量"
    return ".env" if raw(key) else ""


def keys():
    """.env 里出现过的键名（不含值）—— 启动日志与审计用。"""
    return sorted(load())
