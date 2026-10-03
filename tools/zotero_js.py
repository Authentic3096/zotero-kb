"""在 Zotero 里自动执行 JS —— 不用再手动复制粘贴到「运行 JavaScript」窗口。

    python tools/zotero_js.py status              # 插件自报状态（内建命令，最快）
    python tools/zotero_js.py weights             # 让插件刷新并回报权重缓存
    python tools/zotero_js.py run <file.js>       # 执行一个 JS 文件
    python tools/zotero_js.py eval "<js 代码>"     # 执行一段内联 JS
    python tools/zotero_js.py verify              # 执行 zotero-plugin/verify-plugin.js
    python tools/zotero_js.py diag                # 执行 zotero-plugin/diag-load.js

原理：本地服务（online/localserver.py）维护一个任务队列，Zotero 插件每 2 秒
轮询一次 `/task`，取到就在 Zotero 进程内执行，然后把结果 POST 回
`/task/result`。所以：

    Python 派任务 → 插件轮询领取 → 在 Zotero 里执行 → 结果回传 → Python 打印

好处是**零新依赖**（不用 websocket / CDP 调试端口）、复用已有的 token 认证，
而且拿到的是插件沙箱内的真实执行结果（比远程调试更贴近实际运行环境）。

前提：Zotero 开着、插件已启用且版本 ≥ 0.9.5（含任务轮询）、本地服务在跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

BASE = "http://127.0.0.1:8765"
TOKEN_PATH = os.path.join(S.KB_DIR, "service-token.txt")
PLUGIN_DIR = os.path.join(ROOT, "zotero-plugin")

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


def token() -> str:
    try:
        return open(TOKEN_PATH, encoding="utf-8").read().strip()
    except OSError:
        return ""


def api(method: str, path: str, body=None, timeout: float = 30.0):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    tok = token()
    if tok:
        req.add_header("X-KB-Token", tok)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def service_alive() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def dispatch(code: str, kind: str = "js", timeout: float = 90.0,
             poll: float = 1.5, quiet: bool = False):
    """派任务给插件并等结果。返回 (ok, result, error)。"""
    if not service_alive():
        print(f"{BAD}本地服务没在跑。先启动：scripts\\4-service.vbs")
        return False, None, "service down"
    try:
        resp = api("POST", "/task", {"kind": kind, "code": code,
                                     "timeout": timeout})
    except urllib.error.HTTPError as exc:
        print(f"{BAD}派任务失败：HTTP {exc.code} {exc.read()[:200]}")
        return False, None, "dispatch failed"
    task = resp.get("task") or {}
    tid = task.get("id")
    if not tid:
        print(f"{BAD}服务没返回任务 id：{resp}")
        return False, None, "no task id"
    if not quiet:
        print(f"  已派发任务 {tid}（{'内建命令' if kind == 'cmd' else 'JS'}，"
              f"等插件领取并执行…）")

    started = time.time()
    last_state = ""
    while time.time() - started < timeout:
        time.sleep(poll)
        try:
            t = api("GET", f"/task/{tid}")
        except Exception as exc:  # noqa: BLE001
            print(f"{WARN}查询任务状态失败：{exc}")
            continue
        st = t.get("state")
        if st != last_state and not quiet:
            elapsed = time.time() - started
            print(f"    状态：{st}（{elapsed:.1f}s）")
            last_state = st
        if st in ("done", "failed"):
            res = t.get("result")
            if isinstance(res, dict):
                inner_ok = True
                val = res.get("value")
                ms = res.get("ms")
            else:
                inner_ok, val, ms = True, res, None
            return (st == "done" and inner_ok), val, t.get("error", "")
    return False, None, f"超时（{timeout:.0f}s 内插件没完成）"


def print_result(ok: bool, result, error: str) -> int:
    print("")
    if isinstance(result, str):
        print(result)
    elif result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    if error:
        print(f"{BAD}错误：{error}")
    print("")
    print("=" * 66)
    print("任务成功" if ok else "任务失败")
    if not ok:
        print("排查提示：")
        print("  · 插件版本要 ≥ 0.9.5（含任务轮询）——跑 tools\\check_plugin.py 看")
        print("  · Zotero 要开着，且插件已启用")
        print("  · 装完新版本必须**完全重启 Zotero**（更新不会重跑 startup）")
    print("=" * 66)
    return 0 if ok else 1


def read_js(path_or_name: str) -> str:
    """给定的可能是路径，也可能只是文件名（默认到 zotero-plugin 下找）。"""
    if os.path.isabs(path_or_name) and os.path.exists(path_or_name):
        return open(path_or_name, encoding="utf-8").read()
    cand = os.path.join(PLUGIN_DIR, path_or_name)
    if os.path.exists(cand):
        return open(cand, encoding="utf-8").read()
    if os.path.exists(path_or_name):
        return open(path_or_name, encoding="utf-8").read()
    raise FileNotFoundError(f"找不到 JS 文件：{path_or_name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="在 Zotero 里自动执行 JS")
    ap.add_argument("action",
                    choices=["status", "weights", "health", "run", "eval",
                             "verify", "diag", "tasks", "wait"])
    ap.add_argument("arg", nargs="?", default="",
                    help="run/eval 时的文件路径或代码")
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    if not service_alive():
        print(f"{BAD}本地服务没在跑（{BASE}）。")
        print("     启动：双击 scripts\\4-service.vbs，或面板里的「启动服务」")
        return 1

    if args.action == "tasks":
        data = api("GET", "/task-list")
        tasks = data.get("tasks", [])
        print(f"任务队列：{len(tasks)} 条（不含 code 内容）")
        for t in tasks[-10:]:
            print(f"  {t['id']:6} {t['kind']:4} {t['state']:8} "
                  f"{t['created']}  {str(t.get('error') or '')[:50]}")
        return 0

    if args.action in ("status", "weights", "health"):
        cmd = {"status": "status", "weights": "refreshWeights",
               "health": "healthCheck"}[args.action]
        ok, res, err = dispatch(cmd, kind="cmd", timeout=args.timeout)
        return print_result(ok, res, err)

    if args.action == "wait":
        # 只等服务就绪（不派任务）
        print(f"{OK}本地服务在线：{BASE}")
        return 0

    if args.action == "eval":
        code = args.arg
        if not code:
            print(f"{BAD}eval 需要代码参数")
            return 1
    else:
        name = args.arg
        if not name:
            name = {"verify": "verify-plugin.js", "diag": "diag-load.js"}.get(
                args.action, "")
        if not name:
            print(f"{BAD}需要指定 JS 文件")
            return 1
        try:
            code = read_js(name)
        except FileNotFoundError as exc:
            print(f"{BAD}{exc}")
            return 1
        print(f"  已读取 {name}（{len(code)} 字符）")

    ok, res, err = dispatch(code, kind="js", timeout=args.timeout)
    return print_result(ok, res, err)


if __name__ == "__main__":
    sys.exit(main())
