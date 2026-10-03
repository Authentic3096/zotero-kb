"""端到端测试 Zotero → DSH 桥（文件信箱版）。

    python tools/zotero_bridge_e2e.py --list          # 只列对话（只读）
    python tools/zotero_bridge_e2e.py --send "文本"    # 新建对话并发送
    python tools/zotero_bridge_e2e.py --to <id> "文本" # 发到已有对话

原理：往 `~/.dsh/zotero-bridge/requests/` 投一个 json，DSH 侧插件轮询到后
调 sessionController 处理，把结果写到 `results/<id>.json`。本脚本等待并打印结果。

为什么不直接 HTTP：desktop profile 里没有 webServer 服务，而 DSH 的 `/api`
是"受信任+已认证"专用通道（本机外部程序一律 401）。见 ARCHITECTURE.md。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MAILBOX = os.path.join(os.path.expanduser("~"), ".dsh", "zotero-bridge")
REQ_DIR = os.path.join(MAILBOX, "requests")
RES_DIR = os.path.join(MAILBOX, "results")

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


def ensure_mailbox() -> bool:
    if not os.path.isdir(REQ_DIR):
        print(f"{BAD}信箱目录不存在：{REQ_DIR}")
        print("       说明 DSH 侧插件没装载。检查：")
        print("         ① dsh plugin --profile desktop list 里有 dsh-bundle-zotero-bridge 吗")
        print("         ② 重启 DSH 了吗（新增 bundle 需要重启）")
        return False
    return True


def submit(req: dict, timeout: float = 180.0, poll: float = 0.5):
    """投递请求并等结果。返回 (ok, result)。"""
    req_id = req.setdefault("id", uuid.uuid4().hex[:12])
    tmp = os.path.join(REQ_DIR, f"{req_id}.json.tmp")
    dst = os.path.join(REQ_DIR, f"{req_id}.json")
    # 原子写：先 .tmp 再 rename，避免插件读到半截文件
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(req, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    print(f"  已投递请求 {req_id}（kind={req.get('kind')}），等 DSH 侧处理…")

    res_file = os.path.join(RES_DIR, f"{req_id}.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(res_file):
            time.sleep(0.1)          # 让它写完
            try:
                data = json.load(open(res_file, encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.3)
                continue
            return bool(data.get("ok")), data
        time.sleep(poll)
    return False, {"error": f"超时（{timeout:.0f}s 内没拿到结果）"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列对话（只读）")
    ap.add_argument("--send", metavar="TEXT", help="新建对话并发送这段文本")
    ap.add_argument("--to", metavar="SESSION_ID", help="发到已有对话")
    ap.add_argument("text", nargs="?", help="配合 --to 使用的文本")
    ap.add_argument("--title", default="来自 Zotero", help="新对话标题")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--raw", action="store_true",
                    help="打印会话对象的原始结构（查字段名用）")
    args = ap.parse_args()

    print("=" * 66)
    print("Zotero → DSH 桥 · 端到端测试")
    print("=" * 66)
    print(f"  信箱：{MAILBOX}")
    if not ensure_mailbox():
        return 1

    print("\n[1] 列对话")
    ok, res = submit({"kind": "list-sessions"}, timeout=30.0)
    if not ok:
        print(f"{BAD}失败：{json.dumps(res, ensure_ascii=False)[:400]}")
        return 1
    rows = res.get("sessions") or []
    print(f"{OK}拿到 {len(rows)} 个对话")
    for s in rows[:10]:
        print(f"        {str(s.get('id'))[:24]:26} {str(s.get('title'))[:44]}")
    # 字段名可能和我猜的不一样（标题显示"(未命名)"时就是猜错了）。
    # 这里把原始结构打出来，便于一次改对。
    if args.raw and rows:
        print("\n  原始结构（第一条，完整）：")
        print("    " + json.dumps(rows[0], ensure_ascii=False, indent=2)[:900])
        print(f"  字段名：{sorted(rows[0].keys())}")

    if args.list and not args.send and not args.to:
        print("\n（--list：只读检查，结束）")
        return 0

    if args.send:
        print("\n[2] 新建对话并发送")
        ok2, res2 = submit({"kind": "create-and-send", "title": args.title,
                            "text": args.send}, timeout=args.timeout)
        if ok2:
            print(f"{OK}已新建对话 {res2.get('sessionId')} 并投递消息")
            print(f"        去 DSH 里打开这个对话看内容。")
            if res2.get("tries"):
                print(f"        create 尝试记录：{res2['tries']}")
        else:
            print(f"{BAD}失败：{json.dumps(res2, ensure_ascii=False)[:600]}")
            return 1

    if args.to:
        text = args.text or args.send or ""
        if not text:
            print(f"{BAD}--to 需要文本参数")
            return 1
        print(f"\n[2] 发到已有对话 {args.to}")
        ok3, res3 = submit({"kind": "send", "sessionId": args.to, "text": text},
                           timeout=args.timeout)
        if ok3:
            print(f"{OK}已投递")
        else:
            print(f"{BAD}失败：{json.dumps(res3, ensure_ascii=False)[:600]}")
            return 1

    print("\n" + "=" * 66)
    print("链路正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
