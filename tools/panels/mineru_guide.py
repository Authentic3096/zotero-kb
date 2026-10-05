"""mineru_guide.py —— MinerU 安装引导（**可选组件**，用户要求做成图形化）。

## 它解决什么

MinerU 是"装了 PDF 解析明显更准（公式变 LaTeX、表格/扫描件更稳）、不装也
完全能用"的可选组件（见 `offline/mineru.py` 与 `scripts/install-mineru.ps1`）。
命令行装法对非程序员不友好，所以做一个窗口：说明代价 → 选档位 → 预检 →
后台装（实时日志、可取消）→ 自动复检并写回运行环境配置。

## 入口只有两个（用户定的规矩）

1. **插件首次启动**、且**没检测到** MinerU 时弹一次对话框（`src/19-mineruguide.js`），
   选「打开安装引导」就带 `--mineru-guide` 拉起面板并直接开这个窗口；
   选「以后再说」不再弹（记 pref）。
2. 面板「知识库结构」页头部那个按钮 —— **只在未检测到 MinerU 时才出现**
   （装了就收起来，见 `tab_struct.py`）。

## 为什么要预检

装 MinerU 要下 2~4 GB、还要 CUDA 版 torch（PyPI 的 Windows torch 是 CPU 版）。
失败最常见的三个原因就是**磁盘不够 / 没有 GPU / 网络不通**，所以先花几秒
把它们说清楚，再让用户决定要不要开始 —— 而不是装了二十分钟才报错。
预检逻辑抽成 `preflight()` 纯函数，单测直接调它（不开窗口）。

## 安装过程为什么要可取消

实测下载 + 安装要 5~20 分钟（取决于网速）。期间用户可能改主意，也可能
发现选错了档位。所以子进程要在**进程树**上杀（`taskkill /T /F`），
而不是只 terminate 父进程（否则 powershell 死了 uv 还在下）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter import scrolledtext

from .common import ROOT, CREATE_NO_WINDOW, ts

# 装在哪：与 scripts/install-mineru.ps1 里的约定一致
BASE = os.path.join(ROOT, ".mineru")
KIT = os.path.join(BASE, ".venv", "Scripts", "mineru-kit.exe")
SCRIPT = os.path.join(ROOT, "scripts", "install-mineru.ps1")
LOG = os.path.join(BASE, "install.log")

# 预检要打的三个地址（国内实际会用的那三个）
NET_URLS = (
    ("pypi 阿里云镜像", "https://mirrors.aliyun.com/pypi/simple/"),
    ("modelscope（模型）", "https://www.modelscope.cn/"),
    ("pytorch（CUDA 轮子）", "https://download.pytorch.org/whl/cu130/"),
)


def _free_gb(path: str) -> float:
    """某个路径所在盘的剩余空间（GB）。拿不到就返回 -1。

    ⚠ 用 `shutil.disk_usage` 而不是 `os.statvfs` —— 后者在 Windows 上不存在
      （第一版写成 statvfs，本机一跑就 AttributeError）。
    """
    import shutil
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0] or os.path.sep
        return round(shutil.disk_usage(drive).free / 1e9, 1)
    except Exception:      # noqa: BLE001
        return -1.0


def _has_uv() -> str:
    """项目里已经下好的 uv（install-mineru.ps1 没有它会自己下，所以只是提示）。"""
    p = os.path.join(ROOT, ".tools", "uv.exe")
    return p if os.path.exists(p) else ""


def _gpu_info() -> tuple[bool, str]:
    """有没有 NVIDIA 显卡（有 → 顺便报型号与显存）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW)
        line = (out.stdout or "").strip().splitlines()
        if out.returncode == 0 and line:
            return True, line[0].strip()
    except Exception:      # noqa: BLE001
        pass
    return False, ""


def _net_ok(url: str, timeout: float = 6.0) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:      # noqa: BLE001
        try:               # 有的站点不认 HEAD，退一步 GET
            with urllib.request.urlopen(url, timeout=timeout):
                return True
        except Exception:  # noqa: BLE001
            return False


def preflight(with_vlm: bool = False, check_net: bool = True) -> dict:
    """装之前的体检。**纯函数**（只读环境），单测直接调它。

    返回 `{"ok": bool, "items": [{"level": "ok|warn|bad", "text": str}],
    "blockers": [str]}`。`ok=False` 表示有硬阻塞（例如磁盘不够）——
    界面上这时把「开始安装」置灰。
    """
    items: list[dict] = []
    blockers: list[str] = []

    need = 8.0 if with_vlm else 6.0
    free = _free_gb(ROOT)
    if free < 0:
        items.append({"level": "warn", "text": "读不到磁盘剩余空间（不影响继续）"})
    elif free < need:
        msg = f"磁盘不够：只剩 {free} GB，至少要 {need} GB"
        items.append({"level": "bad", "text": msg})
        blockers.append(msg)
    else:
        items.append({"level": "ok",
                      "text": f"磁盘剩余 {free} GB（需要约 {need} GB）"})

    uv = _has_uv()
    items.append({"level": "ok" if uv else "warn",
                  "text": ("uv 已就绪：" + uv) if uv else
                          "还没下过 uv —— 安装脚本会自己下一个（约 17 MB）"})

    has_gpu, gpu = _gpu_info()
    if has_gpu:
        items.append({"level": "ok", "text": f"NVIDIA 显卡：{gpu}（会装 CUDA 版 torch）"})
    else:
        items.append({"level": "warn",
                      "text": "没检测到 NVIDIA 显卡 —— 仍可安装，但会用 ONNX/CPU 跑，"
                              "慢很多（一篇 5 页的论文可能要几分钟）"})

    if check_net:
        bad = []
        for label, url in NET_URLS:
            if _net_ok(url):
                items.append({"level": "ok", "text": f"网络可达：{label}"})
            else:
                bad.append(label)
        if len(bad) == len(NET_URLS):
            msg = "三个下载源都连不上 —— 先把网络/代理弄好再装"
            items.append({"level": "bad", "text": msg})
            blockers.append(msg)
        elif bad:
            items.append({"level": "warn",
                          "text": "连不上：" + "、".join(bad) + "（可能仍然能装，脚本里有备用镜像）"})

    return {"ok": not blockers, "items": items, "blockers": blockers}


def manual_command(with_vlm: bool = False) -> str:
    """给用户复制的手动命令（向导失败时的退路）。"""
    tail = " -WithVlm" if with_vlm else ""
    return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{SCRIPT}"{tail}'


class MineruGuide(tk.Toplevel):
    """安装引导窗口。

    `app` 是管理面板（用来 `say()` 写日志、`out_queue` 回主线程）。
    """

    def __init__(self, root: tk.Misc, app, on_done=None):
        super().__init__(root)
        self.app = app
        self.on_done = on_done
        self.proc: subprocess.Popen | None = None
        self.with_vlm = tk.BooleanVar(value=False)
        self.title("MinerU 安装引导（可选组件）")
        self.geometry("880x620")
        self.transient(root)
        self._build()
        self.after(200, self.refresh_preflight)

    # ------------------------------------------------------------ 界面

    def _build(self):
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="MinerU 安装引导", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            f, justify="left", wraplength=830, foreground="#555",
            text="MinerU 是「可选」组件：装了它，PDF 解析出来的正文更干净、"
                 "公式会变成 LaTeX（可以进检索与摘要）、表格与扫描件更稳。\n"
                 "不装也完全能用 —— 知识库会用 Zotero 自己的缓存文本 + PyMuPDF，"
                 "现有功能一个不少。\n\n"
                 "代价：装到项目目录下的 .mineru\\（basic 档约 5 GB，含 VLM 档约 9 GB），"
                 "首次安装 5~20 分钟（看网速），装 VLM 档解析会再慢 2~3 倍。"
        ).pack(anchor="w", pady=(6, 8))

        box = ttk.LabelFrame(f, text=" 档位 ", padding=10)
        box.pack(fill="x")
        ttk.Checkbutton(box, text="basic 档（必装，约 1 GB 模型）",
                        variable=tk.BooleanVar(value=True),
                        state="disabled").pack(anchor="w")
        cb = ttk.Checkbutton(
            box, variable=self.with_vlm,
            text="额外装 VLM 档（更强，但多 1.24 GB，解析慢 2~3 倍）",
            command=self.refresh_preflight)
        cb.pack(anchor="w", pady=(4, 0))
        ttk.Label(box, foreground="#777", font=("", 8),
                  text="实测（本机 RTX 4060）：basic 5 页 10~30 秒；VLM 5 页 25~37 秒，"
                       "公式与多行矩阵各有胜负。").pack(anchor="w", pady=(2, 0))

        pf = ttk.LabelFrame(f, text=" 体检 ", padding=10)
        pf.pack(fill="x", pady=(8, 0))
        self.pf_text = scrolledtext.ScrolledText(pf, height=7, wrap="word",
                                                 font=("Consolas", 9))
        self.pf_text.pack(fill="x")
        self.pf_text.configure(state="disabled")

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 4))
        self.install_btn = ttk.Button(btns, text="开始安装", command=self.do_install)
        self.install_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="停止安装", command=self.do_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="重新体检", command=self.refresh_preflight).pack(
            side="left")
        ttk.Button(btns, text="复制手动命令",
                   command=self.do_copy_command).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")

        ttk.Label(f, text="安装日志（也可以看面板底部的日志格）",
                  font=("", 9, "bold")).pack(anchor="w", pady=(8, 2))
        self.log = scrolledtext.ScrolledText(f, height=10, wrap="word",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    def _set_pf(self, lines: list[str]):
        self.pf_text.configure(state="normal")
        self.pf_text.delete("1.0", "end")
        self.pf_text.insert("end", "\n".join(lines))
        self.pf_text.configure(state="disabled")

    def say(self, text: str):
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        try:
            self.app.say(f"[mineru] {text.rstrip()}")
        except Exception:      # noqa: BLE001
            pass

    # ------------------------------------------------------------ 预检

    def refresh_preflight(self):
        self._set_pf(["正在体检（几秒）…"])
        with_vlm = bool(self.with_vlm.get())

        def work():
            res = preflight(with_vlm)
            lines = [("✓ " if it["level"] == "ok" else
                      ("! " if it["level"] == "warn" else "✗ ")) + it["text"]
                     for it in res["items"]]
            if res["ok"]:
                lines.append("")
                lines.append("→ 可以开始安装。")
            else:
                lines.append("")
                lines.append("→ 有阻塞项，先解决上面 ✗ 的那些。")
            self.after(0, lambda: self._apply_preflight(res, lines))

        threading.Thread(target=work, daemon=True).start()

    def _apply_preflight(self, res: dict, lines: list[str]):
        self._set_pf(lines)
        try:
            self.install_btn.configure(
                state="normal" if res["ok"] else "disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 安装

    def do_install(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("正在安装", "已经在跑了。要停就点「停止安装」。")
            return
        if not os.path.exists(SCRIPT):
            messagebox.showerror("找不到安装脚本", SCRIPT)
            return
        if not messagebox.askyesno(
                "开始安装",
                "会下载并安装 MinerU（basic 档约 5 GB"
                + ("，含 VLM 档约 9 GB" if self.with_vlm.get() else "")
                + "）。\n过程中可以点「停止安装」中止，已下载的部分会保留，"
                  "下次继续。\n\n现在开始吗？"):
            return
        os.makedirs(BASE, exist_ok=True)
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", SCRIPT]
        if self.with_vlm.get():
            args.append("-WithVlm")
        self.say(f"[{ts()}] ▶ 开始安装：" + " ".join(args))
        self.install_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        def worker():
            code = -1
            try:
                self.proc = subprocess.Popen(
                    args, cwd=ROOT, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", creationflags=CREATE_NO_WINDOW)
                for line in self.proc.stdout:      # type: ignore[union-attr]
                    self.after(0, lambda ln=line: self.say(ln))
                code = self.proc.wait()
            except Exception as exc:      # noqa: BLE001
                self.after(0, lambda e=exc: self.say(f"[XX] {type(e).__name__}: {e}"))
            self.after(0, lambda: self._install_done(code))

        threading.Thread(target=worker, daemon=True).start()

    def _install_done(self, code: int):
        self.cancel_btn.configure(state="disabled")
        self.install_btn.configure(state="normal")
        self.say(f"[{ts()}] ⏹ 安装进程结束，退出码 {code}"
                 + ("（0 = 成功；2 = 装好了但没有 CUDA，会用 CPU）"
                    if code in (0, 2) else "（失败，看上面的日志）"))
        # 装完立刻把探测结果写回运行环境（成功的话），并复检一次
        try:
            sys.path.insert(0, os.path.join(ROOT, "offline"))
            import mineru as MU
            import schemas as S
            MU.clear_cache()
            info = MU.probe(force=True)
            self.say("[探测] " + MU.summary_line(info))
            if info.get("ok") and info.get("exe"):
                try:
                    S.save_env_config({"mineru": info["exe"]})
                    self.say("[配置] 已把 MinerU 路径写进运行环境配置")
                except Exception as exc:      # noqa: BLE001
                    self.say(f"[配置] 写配置失败（可以在「运行环境」页手填）：{exc}")
            elif code in (0, 2):
                self.say("[探测] 退出码说成功，但探测还没看到 —— 点「重新体检」再看一眼")
        except Exception as exc:      # noqa: BLE001
            self.say(f"[探测] 失败：{type(exc).__name__}: {exc}")
        if self.on_done:
            try:
                self.on_done()
            except Exception:      # noqa: BLE001
                pass

    def do_cancel(self):
        if not (self.proc and self.proc.poll() is None):
            self.say("当前没有在跑的安装进程")
            return
        pid = self.proc.pid
        self.say(f"[{ts()}] ⏹ 请求停止（杀进程树 {pid}）")
        # ⚠ 必须连同**子进程树**一起杀：powershell 死了，它下面的 uv / pip / python
        #   还在下文件（实测父进程 terminate 之后下载仍在继续）。
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:      # noqa: BLE001
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def do_copy_command(self):
        cmd = manual_command(bool(self.with_vlm.get()))
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self.say("已复制到剪贴板：" + cmd)
