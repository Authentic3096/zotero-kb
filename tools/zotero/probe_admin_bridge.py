"""离线自检：宿主半（`zotero-plugin/src/23-admin.js`）的传输层能不能工作。

**不依赖 Zotero**：直接按 23-admin.js 的传输逻辑起 `online/engine.py`，用 NDJSON
跑一次 `ping` + 一次 `kb.structure`。三种传输各验一遍 ——

  1. **长驻管道**（主路径）：起一次进程，同一条 stdin/stdout 上连发两条请求
     （`ping`、`kb.structure`）—— 这才叫"会话式"，只发一条证明不了第二条还能通；
  2. **文件退回（契约形态）**：`engine.py --once --request <json> --response <json>`，
     宿主等退出码再读响应文件（见开发说明）；
  3. **短进程管道**（当下就能用的退化）：起进程 → 写一行 → 读一行 → 关 stdin。

为什么要单独有这个自检：真机验证要重启 Zotero，而"协议对不上（字段名/分帧/编码）"
在插件里只表现为"页面转圈"或"宿主半没回应"，看不出是哪一层。这里把同一套编解码
拿到进程外跑一遍，**协议本身先钉死**。

用法：
    & <项目>\\<venv>\\Scripts\\python.exe tools\\zotero\\probe_admin_bridge.py
    ... --method kb.status          # 换一个方法（默认 kb.structure）
    ... --timeout 60                # 单步超时（秒，默认 60）
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
ENGINE = os.path.join(ROOT, "online", "engine.py")

# 控制台符号兜底（同 tools/audit_release.py 的理由：GBK 编不出来就直接崩）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:      # noqa: BLE001
        pass


def pick_python() -> str:
    """宿主半挑解释器的口径（23-admin.js::kbAdminEngineExe：优先 python.exe）。"""
    cands = [
        os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe"),
        sys.executable,
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return sys.executable


def engine_env() -> dict:
    """引擎**必须**拿到 UTF-8（宿主半在 spawn 里设的就是这两个）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class LineReader:
    """把一条管道的行丢进队列（Windows 上没有可用的 select()，只能用线程）。"""

    def __init__(self, stream, q, tag):
        self.stream = stream
        self.q = q
        self.tag = tag
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "LineReader":
        self.thread.start()
        return self

    def _run(self):
        try:
            for line in iter(self.stream.readline, ""):
                self.q.put((self.tag, line))
        except Exception as exc:                     # noqa: BLE001
            self.q.put((self.tag, "\x00read-error: %r" % (exc,)))
        finally:
            self.q.put((self.tag, None))


def rpc_stream(proc, q, req, timeout, log_prefix="    "):
    """写一条请求、读到同 id 的响应（进度事件只打印，不返回）。"""
    proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tag, line = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            raise RuntimeError("引擎的 %s 结束了（没等到响应）" % tag)
        if line.startswith("\x00read-error"):
            raise RuntimeError("读 %s 失败：%s" % (tag, line[1:]))
        if tag == "err":
            print(log_prefix + "[engine] " + line.rstrip())
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(log_prefix + "[stdout 非 JSON] " + line.rstrip()[:200])
            continue
        if msg.get("event"):
            print(log_prefix + "[progress] " + str(msg.get("data"))[:200])
            continue
        if msg.get("id") == req["id"]:
            return msg
    raise TimeoutError("等 %s 的响应超时（%s 秒）" % (req["method"], timeout))


def spawn(args, cwd=ROOT):
    proc = subprocess.Popen(
        args, cwd=cwd, env=engine_env(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    q: "queue.Queue" = queue.Queue()
    LineReader(proc.stdout, q, "out").start()
    LineReader(proc.stderr, q, "err").start()
    return proc, q


def teardown(proc):
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()                 # 引擎是 "stdin EOF 就退出"
    except Exception:                          # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:                          # noqa: BLE001
        try:
            proc.kill()
        except Exception:                      # noqa: BLE001
            pass


def step_persistent(py, method, timeout):
    """① 主路径：长驻管道，同一条会话上连发两条请求。"""
    print("\n[1] 长驻管道（主路径，同一条会话连发两条请求）")
    proc, q = spawn([py, "-X", "utf8", ENGINE])
    try:
        ping = rpc_stream(proc, q, {"id": 1, "method": "ping", "params": {}},
                          timeout)
        if not ping.get("ok"):
            return False, "ping 失败：" + str(ping.get("error"))
        print("    ping -> " + json.dumps(ping.get("data"), ensure_ascii=False))
        req = {"id": 2, "method": method, "params": {}}
        resp = rpc_stream(proc, q, req, timeout)
        if not resp.get("ok"):
            return False, "%s 失败：%s" % (method, resp.get("error"))
        data = resp.get("data")
        summary = _summarize(method, data)
        print("    %s -> %s" % (method, summary))
        print("    ✓ 同一条管道上第二条请求也通了（会话式，不是一次性）")
        return True, summary
    except Exception as exc:                   # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        teardown(proc)


def step_once_file(py, method, timeout):
    """② 文件退回（契约形态）：--once --request/--response。"""
    print("\n[2] 文件退回（engine.py --once --request/--response）")
    req = {"id": 7, "method": method, "params": {}}
    with tempfile.TemporaryDirectory(prefix="kb-admin-probe-") as td:
        rq = os.path.join(td, "req.json")
        rp = os.path.join(td, "res.json")
        with open(rq, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(req, fh, ensure_ascii=False)
        cmd = [py, "-X", "utf8", ENGINE, "--once",
               "--request", rq, "--response", rp]
        try:
            r = subprocess.run(cmd, cwd=ROOT, env=engine_env(),
                               stdin=subprocess.DEVNULL,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout)
        except Exception as exc:               # noqa: BLE001
            return False, "起短进程失败：%s: %s" % (type(exc).__name__, exc)
        if not os.path.exists(rp):
            detail = ("引擎没有写响应文件（退出码 %s）—— 说明它还不认 "
                      "--once/--request/--response 这套参数" % r.returncode)
            err = (r.stderr or "").strip()
            if err:
                detail += "；引擎 stderr：" + err[-300:]
            return False, detail
        with open(rp, encoding="utf-8") as fh:
            resp = json.load(fh)
        if not resp.get("ok"):
            return False, "%s 失败：%s" % (method, resp.get("error"))
        summary = _summarize(method, resp.get("data"))
        print("    %s -> %s" % (method, summary))
        return True, summary


def step_once_pipe(py, method, timeout):
    """③ 短进程管道：一条请求一个进程（当下就能用的退化形态）。"""
    print("\n[3] 短进程管道（一次请求一个进程：写一行 → 读一行 → 关 stdin）")
    proc, q = spawn([py, "-X", "utf8", ENGINE])
    try:
        req = {"id": 9, "method": method, "params": {}}
        resp = rpc_stream(proc, q, req, timeout)
        if not resp.get("ok"):
            return False, "%s 失败：%s" % (method, resp.get("error"))
        summary = _summarize(method, resp.get("data"))
        print("    %s -> %s" % (method, summary))
        return True, summary
    except Exception as exc:                   # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        teardown(proc)


def _summarize(method, data):
    """把响应压成一行（形状对不上也要看得出来：这是"字段名对不对"的验收）。"""
    if method == "kb.structure" and isinstance(data, dict):
        rows = data.get("rows") or []
        return ("rows=%d, total=%s, kb_dir=%s"
                % (len(rows), data.get("total_size_text"),
                   data.get("kb_dir")))
    if method == "kb.status" and isinstance(data, dict):
        return ("items=%s chunks=%s vectors=%s fulltext_chars=%s"
                % (data.get("items"), data.get("chunks"),
                   data.get("vectors"), data.get("fulltext_chars")))
    if method == "kb.health" and isinstance(data, dict):
        return "kb_dir=%s service=%s" % (data.get("kb_dir"), data.get("service"))
    return json.dumps(data, ensure_ascii=False)[:300]


def main() -> int:
    method = "kb.structure"
    timeout = 60.0
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--method" and i + 1 < len(argv):
            method = argv[i + 1]
        elif a == "--timeout" and i + 1 < len(argv):
            timeout = float(argv[i + 1])

    print("=" * 72)
    print("宿主侧桥 / 引擎协议 离线自检")
    print("=" * 72)
    print("  项目根：%s" % ROOT)
    print("  引擎：  %s" % ENGINE)
    if not os.path.exists(ENGINE):
        print("  [XX] 找不到引擎 —— 引擎还没实现（那是另一个 agent 的活）")
        return 1
    py = pick_python()
    print("  解释器：%s" % py)
    print("  环境：  PYTHONIOENCODING=utf-8（引擎要求）")
    print("  方法：  %s（超时 %.0f 秒）" % (method, timeout))

    results = []
    ok1, d1 = step_persistent(py, method, timeout)
    results.append(("长驻管道（主路径）", ok1, d1))
    ok2, d2 = step_once_file(py, method, timeout)
    results.append(("文件退回 --once", ok2, d2))
    ok3, d3 = step_once_pipe(py, method, timeout)
    results.append(("短进程管道", ok3, d3))

    print("\n" + "=" * 72)
    print("小结")
    print("=" * 72)
    for name, ok, detail in results:
        print("  %-22s %s  %s" % (name, "✓" if ok else "✗", detail))
    # 判据：**主路径通了就算过**。文件退回是"契约形态还没实现"，
    # 退化有短进程管道兜着，所以它不红（只如实说明现状）。
    print()
    print("  依赖的引擎字段：kb.status → items/chunks/vectors/experience_rows/"
          "fulltext_chars/embed_model/built_at；")
    print("                  kb.structure → {kb_dir,total_size,total_size_text,"
          "generated_at,rows[]}")
    print("                  rows[] → name/what/count/size/size_text/deletable/note")
    if ok1:
        print("\n  [OK] 宿主侧的 RPC 编解码（NDJSON 分帧 + id 配对）在真引擎上跑通了。")
        return 0
    print("\n  [XX] 主路径（长驻管道）没通 —— 先看上面的引擎 stderr。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
