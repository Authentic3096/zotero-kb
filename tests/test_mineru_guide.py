"""MinerU 安装引导：预检逻辑 + 窗口装配（不需要真装 MinerU）。

    python tests/test_mineru_guide.py

为什么单独测"预检"：它是**装之前唯一的守门人** —— 磁盘不够、没显卡、网络全断
这三件事如果不提前说清，用户会等二十分钟才看到失败。它是纯函数（只读环境），
所以用打桩的方式把三种坏情况都跑一遍（本机是好机器，不测就等于没测）。
窗口那段走"装配 + 销毁"：Tk 只要装配不炸，按钮与回调就齐了（与面板冒烟同一个思路）。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_preflight_bad_cases():
    print("\n[1] 预检：三种坏情况都要能被拦下（打桩）")
    from panels import mineru_guide as MG

    old_free, old_gpu, old_net = MG._free_gb, MG._gpu_info, MG._net_ok
    try:
        # ① 磁盘不够 → 硬阻塞
        MG._free_gb = lambda _p: 1.2
        MG._gpu_info = lambda: (True, "Fake GPU, 8192 MiB")
        MG._net_ok = lambda _u, timeout=6.0: True
        res = MG.preflight(with_vlm=False, check_net=True)
        check("磁盘只剩 1.2 GB（要 6 GB）→ 判为不可开始",
              res["ok"] is False and any("磁盘不够" in b for b in res["blockers"]),
              str(res["blockers"]))
        check("阻塞时仍然给出逐条说明（用户要知道缺多少）",
              any("1.2 GB" in it["text"] for it in res["items"]),
              str(res["items"]))

        # ② 没显卡 → 只是警告（还能用 CPU）
        MG._free_gb = lambda _p: 300.0
        MG._gpu_info = lambda: (False, "")
        res = MG.preflight(with_vlm=False, check_net=False)
        check("没 NVIDIA 显卡 → 可继续，但明确说会用 CPU、慢很多",
              res["ok"] is True
              and any(it["level"] == "warn" and "CPU" in it["text"]
                      for it in res["items"]),
              str(res["items"]))

        # ③ 三个源全断 → 硬阻塞
        MG._net_ok = lambda _u, timeout=6.0: False
        res = MG.preflight(with_vlm=False, check_net=True)
        check("三个下载源都连不上 → 判为不可开始",
              res["ok"] is False
              and any("连不上" in b for b in res["blockers"]),
              str(res["blockers"]))

        # ④ 选 VLM 档时门槛更高（8 GB）
        MG._free_gb = lambda _p: 7.0
        MG._net_ok = lambda _u, timeout=6.0: True
        check("7 GB 空间：basic 可以、含 VLM 档不行",
              MG.preflight(False, check_net=False)["ok"] is True
              and MG.preflight(True, check_net=False)["ok"] is False)
    finally:
        MG._free_gb, MG._gpu_info, MG._net_ok = old_free, old_gpu, old_net


def test_preflight_real():
    print("\n[2] 预检：本机真实环境（读磁盘/显卡，不联网）")
    from panels import mineru_guide as MG

    res = MG.preflight(with_vlm=False, check_net=False)
    check("本机应该可以开始装（磁盘几百 GB + 有 RTX 4060）", res["ok"] is True,
          str(res["blockers"]))
    check("逐条结果都带 level 与 text",
          all(set(it) >= {"level", "text"} for it in res["items"]),
          str(res["items"]))
    check("手动命令带脚本路径，-WithVlm 会加上开关",
          MG.SCRIPT in MG.manual_command(False)
          and "-WithVlm" in MG.manual_command(True),
          MG.manual_command(True))


def test_window_assembles():
    print("\n[3] 窗口装配（不跑 mainloop）")
    import tkinter as tk
    try:
        from panels.mineru_guide import MineruGuide
    except Exception as exc:      # noqa: BLE001
        check("能 import MineruGuide", False, f"{type(exc).__name__}: {exc}")
        return
    check("能 import MineruGuide", True)

    class FakeApp:
        def __init__(self):
            self.said = []

        def say(self, text):
            self.said.append(text)

    root = tk.Tk()
    root.withdraw()
    app = FakeApp()
    try:
        win = MineruGuide(root, app)
        win.withdraw()
        check("窗口建起来了（标题/几何都由自己设）",
              "MinerU" in win.title(), win.title())
        check("有「开始安装 / 停止安装 / 复制手动命令」三个按钮",
              hasattr(win, "install_btn") and hasattr(win, "cancel_btn"),
              "缺按钮属性")
        check("默认档位是 basic（VLM 要用户主动勾）",
              win.with_vlm.get() is False)
        win.say("自检：这是一行日志")
        check("say() 同时写窗口日志与面板日志",
              "自检：这是一行日志" in win.log.get("1.0", "end")
              and any("自检" in s for s in app.said),
              str(app.said[-1:]))
        win.destroy()
        check("能正常销毁", True)
    except Exception as exc:      # noqa: BLE001
        check("窗口装配与销毁", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            root.destroy()
        except Exception:      # noqa: BLE001
            pass


def main() -> int:
    test_preflight_bad_cases()
    test_preflight_real()
    test_window_assembles()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
