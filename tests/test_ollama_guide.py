"""Ollama 安装引导：预检 + 状态探测 + 窗口装配（不需要真装 Ollama）。

    python tests/test_ollama_guide.py

与 `test_mineru_guide.py` 同一个思路：预检是"装之前唯一的守门人"，而本机
（好机器、有网）不会自然走到坏分支，所以**打桩把坏情况跑一遍**；窗口那段只验
装配与销毁（Tk 装配不炸，按钮与回调就齐了）。
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


def test_preflight():
    print("\n[1] 预检：坏情况要能被拦下（打桩）")
    from panels import ollama_guide as OG

    old_free, old_net, old_paths = OG._free_gb, OG._net_ok, OG.ollama_paths
    try:
        # ① 磁盘不够
        OG._free_gb = lambda _p: 2.0
        OG._net_ok = lambda _u, timeout=6.0: True
        OG.ollama_paths = lambda: {"exe": "", "app": "", "api_up": False,
                                   "models": [], "err": ""}
        res = OG.preflight(check_net=True)
        check("磁盘只剩 2 GB（要 6 GB）→ 判为不可开始",
              res["ok"] is False and any("磁盘不够" in b for b in res["blockers"]),
              str(res["blockers"]))

        # ② 网络全断
        OG._free_gb = lambda _p: 300.0
        OG._net_ok = lambda _u, timeout=6.0: False
        res = OG.preflight(check_net=True)
        check("两个下载源都连不上 → 判为不可开始",
              res["ok"] is False
              and any("连不上" in b for b in res["blockers"]),
              str(res["blockers"]))

        # ③ 已装好 + 在跑 → 只是提示（不阻塞；用户想改用 API 也行）
        OG._net_ok = lambda _u, timeout=6.0: True
        OG.ollama_paths = lambda: {"exe": "C:\\x\\ollama.exe", "app": "",
                                   "api_up": True, "models": ["qwen3:4b-instruct"],
                                   "err": ""}
        res = OG.preflight(check_net=False)
        check("已经装好且在跑 → 可以继续（不阻塞），并报出模型",
              res["ok"] is True
              and any("已经装好了" in it["text"] for it in res["items"])
              and any("qwen3" in it["text"] for it in res["items"]),
              str(res["items"]))
    finally:
        OG._free_gb, OG._net_ok, OG.ollama_paths = old_free, old_net, old_paths


def test_state():
    print("\n[2] 状态探测：结构齐、不抛（本机没装也应如实报）")
    from panels.ollama_guide import ollama_paths

    st = ollama_paths()
    check("返回五个键", set(st) >= {"exe", "app", "api_up", "models", "err"},
          str(sorted(st)))
    check("api_up / models 类型正确",
          isinstance(st["api_up"], bool) and isinstance(st["models"], list),
          str(st))
    check("没装时 exe 为空串（不是 None / 不是猜的路径）",
          st["exe"] == "" or os.path.isfile(st["exe"]), str(st["exe"]))
    print(f"        （本机实际：exe={st['exe']!r} api_up={st['api_up']} "
          f"models={st['models']}）")


def test_window():
    print("\n[3] 窗口装配（不跑 mainloop）")
    import tkinter as tk
    try:
        from panels.ollama_guide import OllamaGuide
    except Exception as exc:      # noqa: BLE001
        check("能 import OllamaGuide", False, f"{type(exc).__name__}: {exc}")
        return
    check("能 import OllamaGuide", True)

    class FakeApp:
        def __init__(self):
            self.said = []

        def say(self, text):
            self.said.append(text)

    root = tk.Tk()
    root.withdraw()
    app = FakeApp()
    try:
        win = OllamaGuide(root, app)
        win.withdraw()
        check("窗口建起来了（标题含 Ollama）", "Ollama" in win.title(), win.title())
        check("有「开始安装 / 停止」两个按钮",
              hasattr(win, "install_btn") and hasattr(win, "cancel_btn"))
        check("默认要拉的模型是 4B 那档（小机器也能跑）",
              "4b" in win.model.get().lower(), win.model.get())
        win.say("自检：一行日志")
        check("say() 同时写窗口日志与面板日志",
              "自检：一行日志" in win.log.get("1.0", "end")
              and any("自检" in s for s in app.said))
        win.destroy()
        check("能正常销毁", True)
    except Exception as exc:      # noqa: BLE001
        check("窗口装配与销毁", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            root.destroy()
        except Exception:      # noqa: BLE001
            pass


def test_llm_sync_js():
    """面板「同步到 Zotero 插件」生成的那段 JS 要语法正确、四个 pref 都在。

    为什么单测它：插件里字段名写错在 Zotero 里**没有任何报错**，只表现为
    "同步了但没生效"。这段 JS 现在同时被 GUI 与任务队列消费，所以先验语法。
    """
    print("\n[4] 「同步到 Zotero 插件」那段 JS")
    import subprocess
    import tempfile
    from panels.tab_env import llm_sync_js

    js = llm_sync_js({"provider": "openai", "model": "deepseek-chat",
                      "base_url": "https://api.deepseek.com/v1",
                      "api_key": "sk-test-1234"})
    check("四个 pref 名都在（provider/model/apiBaseUrl/apiKey）",
          all(k in js for k in ("P.provider", "P.model", "P.apiBaseUrl",
                                "P.apiKey")), js[:120])
    check("带上了用户填的值", "deepseek-chat" in js and "sk-test-1234" in js)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        check("node --check 通过（语法正确）", r.returncode == 0,
              (r.stderr or "")[:200])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main() -> int:
    test_preflight()
    test_state()
    test_window()
    test_llm_sync_js()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
