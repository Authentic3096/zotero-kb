"""任务桥接自检：模拟"插件"跑一遍完整链路，验证 zotero_js.py 能用。

    python tools/test_task_bridge.py

为什么要单独测：`tools/zotero_js.py` 依赖"Python 派任务 → 插件轮询领取 →
在 Zotero 里执行 → 结果回传"这条链路。链条上任何一环断了（路由写错、
状态没转、结果取不回），用户看到的就是"任务超时"，很难判断是插件没轮询
还是链路本身坏了。这里用一个**假的插件**（就是本脚本）把链路走通，
从而把"链路问题"和"插件问题"分开。

注意：需要一个真实的 Zotero 插件在轮询时才能跑端到端；这里不依赖它 ——
本脚本自己扮演插件角色去 `/task` 领任务、再把结果 POST 回去。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

BASE = "http://127.0.0.1:8765"
PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"   → {detail}" if detail else ""))


def token() -> str:
    try:
        return open(os.path.join(S.KB_DIR, "service-token.txt"),
                    encoding="utf-8").read().strip()
    except OSError:
        return ""


def api(method: str, path: str, body=None, timeout: float = 15.0):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method)
    req.add_header("Content-Type", "application/json")
    if token():
        req.add_header("X-KB-Token", token())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def main() -> int:
    print("=" * 66)
    print("任务桥接自检（模拟插件跑完整链路）")
    print("=" * 66)

    # 0) 服务在不在
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
            alive = r.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"  [XX] 本地服务没在跑：{exc}")
        print("       先启动：scripts\\4-service.vbs")
        return 1
    check("本地服务在线", alive)

    # 1) token 校验：不带 token 应该 401
    try:
        req = urllib.request.Request(BASE + "/task", method="GET")
        urllib.request.urlopen(req, timeout=8)
        check("无 token 被拒（401）", False, "居然通过了")
    except urllib.error.HTTPError as exc:
        check("无 token 被拒（401）", exc.code == 401, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        check("无 token 被拒（401）", False, str(exc))

    # 2) 空队列时插件应拿到 null
    got = api("GET", "/task")
    check("空队列返回 task=null", got.get("task") is None, str(got))

    # 3) 派一个任务
    payload = "return 1 + 2;"
    resp = api("POST", "/task", {"kind": "js", "code": payload})
    task = resp.get("task") or {}
    tid = task.get("id")
    check("POST /task 派发成功", bool(tid), json.dumps(resp)[:160])
    check("新任务状态是 pending", task.get("state") == "pending",
          str(task.get("state")))
    if not tid:
        print(f"\n通过 {PASS}　失败 {FAIL}")
        return 1

    # 4) 模拟插件领取
    claimed = (api("GET", "/task").get("task") or {})
    check("插件能领到任务", claimed.get("id") == tid,
          f"领到 {claimed.get('id')}")
    check("领取后状态变 running", claimed.get("state") == "running",
          str(claimed.get("state")))
    check("任务带回原始代码", claimed.get("code") == payload,
          repr(claimed.get("code")))

    # 5) 再领一次应该拿不到（同一个任务不会被重复派发）
    again = api("GET", "/task").get("task")
    check("同一任务不会被重复领取", again is None or again.get("id") != tid,
          str(again))

    # 6) 模拟插件回报结果
    ok = api("POST", "/task/result", {
        "id": tid, "ok": True,
        "result": {"value": "3", "ms": 12, "pluginVersion": "test"},
    })
    check("回报结果成功", ok.get("ok") is True, str(ok))

    # 7) Python 侧能取到结果
    t = api("GET", f"/task/{tid}")
    check("结果状态为 done", t.get("state") == "done", str(t.get("state")))
    check("结果值正确", isinstance(t.get("result"), dict)
          and t["result"].get("value") == "3", json.dumps(t.get("result"))[:120])

    # 8) 失败任务也要能记录
    resp2 = api("POST", "/task", {"kind": "js", "code": "boom"})
    tid2 = (resp2.get("task") or {}).get("id")
    api("GET", "/task")     # 领走
    api("POST", "/task/result", {"id": tid2, "ok": False,
                                 "error": "ReferenceError: boom is not defined"})
    t2 = api("GET", f"/task/{tid2}")
    check("失败任务状态为 failed", t2.get("state") == "failed",
          str(t2.get("state")))
    check("失败原因被保留", "boom" in (t2.get("error") or ""),
          str(t2.get("error"))[:80])

    # 9) 不存在的任务返回 404
    try:
        api("GET", "/task/t999999")
        check("未知任务返回 404", False, "居然返回 200")
    except urllib.error.HTTPError as exc:
        check("未知任务返回 404", exc.code == 404, f"HTTP {exc.code}")

    # 10) 任务列表接口
    lst = api("GET", "/task-list")
    check("/task-list 可用", isinstance(lst.get("tasks"), list),
          str(type(lst.get("tasks"))))
    check("列表不含 code 内容（避免泄露大段代码）",
          all("code" not in x for x in lst.get("tasks", [])))

    print(f"\n{'=' * 66}\n通过 {PASS}　失败 {FAIL}\n{'=' * 66}")
    if FAIL == 0:
        print("链路正常。接下来只要插件版本 ≥ 0.9.5 并已重启，")
        print("就能用 tools\\zotero_js.py status / verify / diag 自动执行了。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
