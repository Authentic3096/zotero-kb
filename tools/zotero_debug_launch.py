"""用 -ZoteroDebugText 启动 Zotero，把调试输出落盘到文件。

    python tools/zotero_debug_launch.py            # 关掉现有 Zotero → 带调试启动
    python tools/zotero_debug_launch.py --no-kill  # 不自动关，只提示

为什么需要：Zotero 默认不写日志文件，而"插件 active=true 却什么都不做"这类问题
的真正原因只在调试输出里（比如 `Plugin X is missing bootstrap method 'startup'`
或 `loadSubScriptWithOptions` 的解析错误）。手动开「帮助 → Debug Output Logging」
要复制粘贴，很慢；带 `-ZoteroDebugText` 参数启动可以直接把输出重定向到文件，
之后 Python 侧随时能读。

输出文件：kb\\logs\\zotero-debug-live.txt
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

ZOTERO_EXE = r"D:\Application\Zotero\zotero.exe"
LOG_DIR = os.path.join(S.KB_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "zotero-debug-live.txt")
ERR_PATH = os.path.join(LOG_DIR, "zotero-debug-live.err.txt")


def running() -> int:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or "").lower().count("zotero.exe")
    except Exception:  # noqa: BLE001
        return 0


def kill_zotero() -> None:
    """温和关闭，必要时强制。"""
    try:
        subprocess.run(["taskkill", "/IM", "zotero.exe", "/T"],
                       capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(10):
        if running() == 0:
            return
        time.sleep(1)
    try:
        subprocess.run(["taskkill", "/F", "/IM", "zotero.exe", "/T"],
                       capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        pass


def read_log(filter_tail: int = 60000) -> int:
    """读调试日志，挑出插件相关的关键行。"""
    import re

    print("=" * 70)
    print("Zotero 调试日志 · 插件相关")
    print("=" * 70)
    if not os.path.exists(LOG_PATH):
        print(f"  日志不存在：{LOG_PATH}")
        print("  先跑：python tools\\zotero_debug_launch.py")
        return 1
    raw = open(LOG_PATH, encoding="utf-8", errors="replace").read()
    lines = raw.splitlines()
    print(f"  文件：{LOG_PATH}")
    print(f"  共 {len(lines)} 行，{len(raw)} 字符")
    if not lines:
        print("  （空 —— Zotero 可能没起来，或调试输出没写进来）")
        return 1

    pat = re.compile(
        r"zotero-kb|bootstrap method|missing bootstrap|"
        r"loadSubScript|Error running bootstrap|"
        r"Calling bootstrap method|Blocking plugin|"
        r"SyntaxError|ERROR|Error:", re.I)
    hits = [ln for ln in lines if pat.search(ln)]

    ours = [ln for ln in hits if "zotero-kb" in ln.lower()]
    print(f"\n★ 提到 zotero-kb 的 {len(ours)} 条：")
    for ln in ours[-40:]:
        print("   " + ln.strip()[:190])
    if not ours:
        print("   （没有 —— 说明 Zotero 根本没在日志里提到这个插件）")

    boot = [ln for ln in hits if re.search(
        r"bootstrap method|loadSubScript|missing bootstrap", ln, re.I)]
    print(f"\n★ 插件生命周期相关 {len(boot)} 条：")
    for ln in boot[-40:]:
        print("   " + ln.strip()[:190])
    if not boot:
        print("   （没有）")

    errs = [ln for ln in lines if re.search(
        r"SyntaxError|is not defined|is not a function", ln)]
    print(f"\n★ 语法/引用错误 {len(errs)} 条：")
    for ln in errs[-25:]:
        print("   " + ln.strip()[:190])
    if not errs:
        print("   （没有）")

    print("\n" + "=" * 70)
    print(f"完整日志：{LOG_PATH}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-kill", action="store_true",
                    help="不自动关闭现有 Zotero")
    ap.add_argument("--wait", type=float, default=25.0,
                    help="启动后等多少秒再回来读日志")
    ap.add_argument("--read", action="store_true",
                    help="只读已有日志，不启动")
    args = ap.parse_args()

    if args.read:
        return read_log()

    os.makedirs(LOG_DIR, exist_ok=True)
    n = running()
    print(f"  当前 Zotero 进程数：{n}")
    if n:
        if args.no_kill:
            print("  有 Zotero 在运行。要拿到干净的启动日志，请先完全退出它，"
                  "再重跑本命令。")
            return 1
        print("  正在关闭 Zotero（带调试参数启动需要重启）…")
        kill_zotero()
        print(f"  关闭后进程数：{running()}")

    # 清掉旧日志，避免把上一轮的内容当成这一轮的
    for p in (LOG_PATH, ERR_PATH):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    print("  带 -ZoteroDebugText 启动 Zotero…")
    with open(LOG_PATH, "wb") as out, open(ERR_PATH, "wb") as err:
        proc = subprocess.Popen(
            [ZOTERO_EXE, "-ZoteroDebugText", "-jsconsole"],
            stdout=out, stderr=err, cwd=os.path.dirname(ZOTERO_EXE),
            creationflags=0x00000008,   # DETACHED_PROCESS：不随本命令退出而结束
        )
    print(f"  PID={proc.pid}，等 {args.wait:.0f}s 让它起来…")
    time.sleep(args.wait)
    size = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0
    print(f"  日志：{LOG_PATH}（{size} 字节）")
    print("\n现在可以跑：")
    print("  python tools\\zotero_debug_launch.py --read      # 看日志里的插件相关行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
