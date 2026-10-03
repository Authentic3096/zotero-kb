"""验证 Zotero↔DSH 桥的 DSH 侧端点是否可用。

    python tools/zotero_bridge_check.py
    python tools/zotero_bridge_check.py --send-test   # 真发一条测试消息到新对话

检查顺序：
  1. 端点探活（/zotero-bridge/health，不需要口令）
  2. 口令校验（不带口令应 401）
  3. 列对话（/sessions）
  4. 可选：新建对话并发一条测试消息（--send-test）

为什么要单独一个脚本：DSH 侧是插件，装载失败时**不会**有显眼报错
（尤其 patchReload 是 live 时）。先探活能立刻区分
"插件没装载" 和 "插件装载了但会话 API 用错"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BASE = os.environ.get("ZOTERO_BRIDGE_BASE", "http://127.0.0.1:19387")
PREFIX = "/zotero-bridge"
TOKEN_PATH = os.path.join(ROOT, "kb", "bridge-token.txt")

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


def token() -> str:
    try:
        return open(TOKEN_PATH, encoding="utf-8").read().strip()
    except OSError:
        return ""


def call(method: str, path: str, body=None, with_token: bool = True,
         timeout: float = 30.0):
    url = BASE + PREFIX + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if with_token and token():
        req.add_header("x-dsh-bridge-token", token())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    global BASE
    default_base = BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", action="store_true",
                    help="真发一条测试消息（会新建一个对话）")
    ap.add_argument("--base", default=default_base)
    args = ap.parse_args()
    BASE = args.base

    print("=" * 66)
    print("Zotero → DSH 桥 · DSH 侧端点检查")
    print("=" * 66)
    print(f"  端点：{BASE}{PREFIX}")
    print(f"  口令：{'已配置（%d 字符）' % len(token()) if token() else '未配置'}")

    print("\n[1] 探活（不需要口令）")
    code, payload = call("GET", "/health", with_token=False)
    if code == 200 and isinstance(payload, dict) and payload.get("ok"):
        print(f"{OK}端点在线：{payload.get('service')} v{payload.get('version')}")
        print(f"        可用路径：{payload.get('routes')}")
    elif code == 401:
        print(f"{BAD}HTTP 401 —— 这通常是 DSH 自身的会话认证拦住了，说明")
        print("        自注册路由**没有**绕过认证（或路由没注册成功）。")
        return 1
    elif code == 404:
        print(f"{BAD}HTTP 404 —— 端点不存在。插件没装载。")
        print("        排查：① bundle 装了吗（dsh plugin --profile desktop list）")
        print("              ② DSH 重启过吗（改 profile 配置后常需重启）")
        return 1
    else:
        print(f"{BAD}HTTP {code} → {str(payload)[:300]}")
        print("        DSH 没在跑？或者端口不是 19387。")
        return 1

    print("\n[2] 口令校验")
    code2, _ = call("GET", "/sessions", with_token=False)
    if code2 == 401:
        print(f"{OK}不带口令被拒（401）")
    else:
        print(f"{WARN}不带口令也能访问（HTTP {code2}）—— "
              f"检查 bundle config 里的 requireToken/token")
    code3, payload3 = call("GET", "/sessions")
    if code3 != 200:
        print(f"{BAD}带口令仍失败：HTTP {code3} → {str(payload3)[:300]}")
        return 1
    print(f"{OK}带口令通过")

    print("\n[3] 列对话")
    rows = (payload3 or {}).get("sessions") if isinstance(payload3, dict) else None
    if rows is None:
        print(f"{WARN}返回结构里没有 sessions 字段：{str(payload3)[:300]}")
    else:
        print(f"{OK}读到 {len(rows)} 个对话")
        for s in rows[:8]:
            print(f"        {s.get('id', '')[:20]:22} {str(s.get('title'))[:40]}")

    if args.send_test:
        print("\n[4] 真发一条测试消息（会新建对话）")
        code4, payload4 = call("POST", "/create-and-send", {
            "title": "Zotero 桥测试",
            "text": "这是一条来自 Zotero 桥的测试消息。看到它就说明链路通了。",
        }, timeout=120.0)
        if code4 == 200 and isinstance(payload4, dict) and payload4.get("ok"):
            print(f"{OK}已新建对话并发送：{payload4.get('sessionId')}")
            print("        去 DSH 里看那个对话。")
        else:
            print(f"{BAD}HTTP {code4} → {str(payload4)[:400]}")

    print("\n" + "=" * 66)
    print("端点正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
