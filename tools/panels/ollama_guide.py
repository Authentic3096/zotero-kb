"""ollama_guide.py —— Ollama 安装引导（**可选组件**，与 MinerU 那套同形）。

## 为什么要有它（用户 2026-10-05 提的）

「面板环境中的『ollama 程序』能不能也像 minerU 那样做成『ollama（可选）』然后也有
一样逻辑的图形化引导」。Ollama 是本地模型（分类建议 / 摘要 / 标签 / 抽经验）的后端；
不装也能用 API 模型，所以标成**可选**。

顺带解决了用户另一句话「当 ollama 没启动时点『启动本地服务』拉不起来」背后的真实
需求：**面板里没有把 Ollama 拉起来的入口**。所以这个窗口的流程里包含"装完就启动"，
运行环境页也加了「启动 Ollama」按钮。

## 流程（每一步都可取消）

1. 说明代价：安装包约 1.2 GB + 模型 2.5 GB（4B）→ 磁盘要有 ~6 GB；
2. 预检：磁盘 / 网络（ollama.com 与 registry.ollama.ai）/ 是否已装；
3. 下载 `OllamaSetup.exe`（带进度、可取消）；
4. **静默安装**（`/SILENT`，按用户装到 %LOCALAPPDATA%\\Programs\\Ollama，不需要管理员）；
   静默失败就退回"打开安装器，你点几下"；
5. 启动 Ollama 并等它响应；
6. `ollama pull <模型>`（默认 `qwen3:4b-instruct`，带进度、可取消）；
7. 复检 + 把 `ollama.exe` 路径写回运行环境配置。

⚠ 实测过的事实：Ollama 的官方安装器是**按用户安装**（不写系统目录、不要 UAC），
  装完 `ollama.exe` 在 `%LOCALAPPDATA%\\Programs\\Ollama\\`；托盘应用叫
  `ollama app.exe`（`ollama serve` 也行）。这些都在 `offline/schemas.resolve_ollama()`
  的候选里。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter import scrolledtext

from .common import ROOT, ts
from .mineru_guide import _free_gb, _net_ok   # 共用这两个小工具（同一套判据）

# 面板/服务里跑子进程的统一入口（stdin=DEVNULL，绕开 pythonw 的坏句柄）
import procrun as PR  # noqa: E402

SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_MODEL = "qwen3:4b-instruct"
NET_URLS = (
    ("ollama.com（安装包）", "https://ollama.com/"),
    ("registry.ollama.ai（模型）", "https://registry.ollama.ai/"),
)
# 装的临时位置：项目内的 .tools\（不污染别处；装完可删）
SETUP_DIR = os.path.join(ROOT, ".tools")
SETUP_EXE = os.path.join(SETUP_DIR, "OllamaSetup.exe")


def ollama_paths() -> dict:
    """当前 Ollama 状态：exe 路径 / 托盘应用 / API 通不通 / 有哪些模型。"""
    out = {"exe": "", "app": "", "api_up": False, "models": [], "err": ""}
    try:
        sys.path.insert(0, os.path.join(ROOT, "offline"))
        import judge as J
        import schemas as S
        out["exe"] = S.resolve_ollama() or ""
        if out["exe"]:
            cand = os.path.join(os.path.dirname(out["exe"]), "ollama app.exe")
            out["app"] = cand if os.path.exists(cand) else ""
        up, models = J.ollama_models()
        out["api_up"], out["models"] = bool(up), list(models or [])
    except Exception as exc:      # noqa: BLE001
        out["err"] = f"{type(exc).__name__}: {exc}"
    if not out["exe"]:
        # 退一步：拿不到 exe 也把 API 状态报出去（可能是别处装的）
        try:
            import judge as J
            up, models = J.ollama_models()
            out["api_up"], out["models"] = bool(up), list(models or [])
        except Exception:      # noqa: BLE001
            pass
    return out


def preflight(check_net: bool = True) -> dict:
    """装之前的体检（纯函数，单测直接调）。"""
    items: list[dict] = []
    blockers: list[str] = []
    need = 6.0
    free = _free_gb(ROOT)
    if free < 0:
        items.append({"level": "warn", "text": "读不到磁盘剩余空间（不影响继续）"})
    elif free < need:
        msg = f"磁盘不够：只剩 {free} GB，至少要 {need} GB（安装包 + 一个 4B 模型）"
        items.append({"level": "bad", "text": msg})
        blockers.append(msg)
    else:
        items.append({"level": "ok", "text": f"磁盘剩余 {free} GB（需要约 {need} GB）"})

    st = ollama_paths()
    installed = bool(st["exe"])
    if installed:
        items.append({"level": "ok", "text": "Ollama 已经装好了：" + st["exe"]})
    else:
        items.append({"level": "warn", "text": "本机还没装 Ollama —— 这个窗口会帮你装"})
    if st["api_up"]:
        items.append({"level": "ok",
                      "text": "Ollama 正在运行，本机模型：" +
                              ("、".join(st["models"]) if st["models"] else "（还没有模型）")})
    else:
        items.append({"level": "warn", "text": "Ollama 当前没有响应（装完会自动启动它）"})

    if check_net:
        bad = []
        for label, url in NET_URLS:
            if _net_ok(url):
                items.append({"level": "ok", "text": f"网络可达：{label}"})
            else:
                bad.append(label)
        if len(bad) == len(NET_URLS):
            msg = "两个下载源都连不上 —— 先把网络/代理弄好再装"
            items.append({"level": "bad", "text": msg})
            blockers.append(msg)
        elif bad:
            items.append({"level": "warn",
                          "text": "连不上：" + "、".join(bad) + "（可能仍然能装）"})
    return {"ok": not blockers, "installed": installed,
            "items": items, "blockers": blockers}


class OllamaGuide(tk.Toplevel):
    """图形化安装引导（下载 → 静默装 → 启动 → 拉模型）。"""

    def __init__(self, root: tk.Misc, app, on_done=None):
        super().__init__(root)
        self.app = app
        self.on_done = on_done
        self.proc: subprocess.Popen | None = None
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        self.title("Ollama 安装引导（可选组件）")
        self.geometry("880x640")
        self.transient(root)
        self._build()
        self.after(200, self.refresh_preflight)

    # ------------------------------------------------------------ 界面

    def _build(self):
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Ollama 安装引导", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            f, justify="left", wraplength=830, foreground="#555",
            text="Ollama 是「可选」的本地模型后端：装了它，分类建议 / 摘要 / 标签 / "
                 "抽经验都能在本机离线跑（不用 API Key、论文不外传）。\n"
                 "不装也能用 —— 在「模型接入」里把服务商改成 openai（DeepSeek 等）即可。\n\n"
                 "代价：安装包约 1.2 GB + 一个 4B 模型约 2.5 GB（磁盘要有 ~6 GB）；"
                 "装到 %LOCALAPPDATA%\\Programs\\Ollama（按用户安装，不需要管理员）。"
        ).pack(anchor="w", pady=(6, 8))

        box = ttk.LabelFrame(f, text=" 要拉的模型 ", padding=10)
        box.pack(fill="x")
        ttk.Label(box, text="模型名（默认这个：质量/速度平衡，中文可用）：").pack(
            side="left")
        ttk.Combobox(box, textvariable=self.model, width=28, values=(
            DEFAULT_MODEL, "qwen2.5:7b-instruct", "gemma3:4b", "llama3.1:8b",
        )).pack(side="left", padx=6)
        ttk.Label(box, foreground="#777", font=("", 8),
                  text="（小机器建议 4B；换别的名字也行，第一次拉会下载 GB 级文件）").pack(
            side="left")

        pf = ttk.LabelFrame(f, text=" 体检 ", padding=10)
        pf.pack(fill="x", pady=(8, 0))
        self.pf_text = scrolledtext.ScrolledText(pf, height=7, wrap="word",
                                                 font=("Consolas", 9))
        self.pf_text.pack(fill="x")
        self.pf_text.configure(state="disabled")

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 4))
        self.install_btn = ttk.Button(btns, text="开始安装（下载 + 安装 + 拉模型）",
                                      command=self.do_install)
        self.install_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="停止", command=self.do_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="只下载安装器（我手动装）",
                   command=self.do_download_only).pack(side="left", padx=6)
        ttk.Button(btns, text="重新体检", command=self.refresh_preflight).pack(
            side="left")
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")

        ttk.Label(f, text="日志", font=("", 9, "bold")).pack(anchor="w", pady=(8, 2))
        self.log = scrolledtext.ScrolledText(f, height=12, wrap="word",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    def _set_pf(self, lines):
        self.pf_text.configure(state="normal")
        self.pf_text.delete("1.0", "end")
        self.pf_text.insert("end", "\n".join(lines))
        self.pf_text.configure(state="disabled")

    def say(self, text: str):
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        try:
            self.app.say(f"[ollama] {text.rstrip()}")
        except Exception:      # noqa: BLE001
            pass

    def refresh_preflight(self):
        self._set_pf(["正在体检（几秒）…"])

        def work():
            res = preflight(True)
            lines = [("✓ " if it["level"] == "ok" else
                      ("! " if it["level"] == "warn" else "✗ ")) + it["text"]
                     for it in res["items"]]
            lines.append("")
            if res.get("installed"):
                lines.append("→ 已检测到本机装有 Ollama，无需重装"
                             "（「开始安装」已置灰）。只是要拉模型的话，"
                             "可以在命令行跑：ollama pull <模型名>。")
            else:
                lines.append("→ 可以开始安装。" if res["ok"]
                             else "→ 先解决上面 ✗ 的项。")
            self.after(0, lambda: self._apply(res, lines))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, res, lines):
        self._set_pf(lines)
        try:
            if res.get("installed"):
                # 已装 → 即使没有阻塞项也不给点（用户 2026-10-10 的规矩，
                # 与 MinerU 引导同一种灰）。拉模型可以走命令行或被别处复用。
                self.install_btn.configure(state="disabled")
            else:
                self.install_btn.configure(
                    state="normal" if res["ok"] else "disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 安装

    def _run(self, args, cwd=None) -> int:
        try:
            self.proc = PR.popen(
                args, cwd=cwd, merge_stderr=True,
                on_tier=lambda m: self.after(0, lambda mm=m: self.say(
                    f"[procrun] 子进程改用兜底档位：{mm}")))
            for line in self.proc.stdout:      # type: ignore[union-attr]
                self.after(0, lambda ln=line: self.say(ln.rstrip()))
            return self.proc.wait()
        except Exception as exc:      # noqa: BLE001
            self.after(0, lambda e=exc: self.say(f"[XX] {type(e).__name__}: {e}"))
            return -1

    def do_install(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("正在跑", "已经在装了。要停就点「停止」。")
            return
        st = ollama_paths()
        skip_dl = bool(st["exe"])
        if skip_dl:
            # 按钮此时本来就是灰的；这条护栏是给"别处误调"兜底
            messagebox.showinfo(
                "已经装好了",
                "本机已经装了 Ollama（" + st["exe"] + "），无需重装。\n\n"
                "只是要拉模型的话，可以在命令行跑："
                f"ollama pull {self.model.get()}")
            return
        if not messagebox.askyesno(
                "开始安装",
                ("已经装了 Ollama" if skip_dl else "会下载并安装 Ollama（约 1.2 GB）")
                + f"\n然后拉模型 {self.model.get()}（GB 级，第一次会慢）。\n"
                  "全程可点「停止」取消，已下载的部分会保留。\n\n继续？"):
            return
        self.install_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        def worker():
            os.makedirs(SETUP_DIR, exist_ok=True)
            # ① 下载
            if not skip_dl:
                if not self._download():
                    self.after(0, self._done)
                    return
                # ② 静默安装（Inno Setup 的 /SILENT；失败退回手动）
                self.after(0, lambda: self.say(f"[{ts()}] 静默安装：{SETUP_EXE} /SILENT"))
                code = self._run([SETUP_EXE, "/SILENT"])
                if code != 0:
                    self.after(0, lambda: self.say(
                        "[!!] 静默安装返回非 0 —— 会再试一次让安装器自己弹（你点几下）"))
                    self._run([SETUP_EXE])
            # ③ 启动
            st2 = ollama_paths()
            exe, app = st2["exe"], st2["app"]
            if not exe and not app:
                self.after(0, lambda: self.say(
                    "[XX] 装完了但没找到 ollama.exe —— 装到别处了？"
                    "在「运行环境」里把 Ollama 路径填上。"))
                self.after(0, self._done)
                return
            for cand in (app, exe):
                if not cand:
                    continue
                try:
                    self.after(0, lambda c=cand: self.say(f"[{ts()}] 启动 {c}"))
                    # 起了不管的后台进程：三个标准流都接 DEVNULL（procrun.spawn）
                    PR.spawn([cand, "serve"] if cand == exe else [cand],
                             cwd=os.path.dirname(cand))
                    break
                except Exception as exc:      # noqa: BLE001
                    self.after(0, lambda e=exc: self.say(f"[!!] 启动失败：{e}"))
            # ④ 等它就绪
            sys.path.insert(0, os.path.join(ROOT, "offline"))
            import judge as J
            ok = False
            for i in range(20):
                time.sleep(1.0)
                up, _models = J.ollama_models()
                if up:
                    ok = True
                    self.after(0, lambda s=i + 1: self.say(
                        f"[{ts()}] ✅ Ollama 已就绪（等了 {s} 秒）"))
                    break
            if not ok:
                self.after(0, lambda: self.say(
                    "[!!] 等了 20 秒 Ollama 还没响应。托盘里看得到图标吗？"
                    "也可以点「停止」后用命令行 `ollama serve` 起。"))
            # ⑤ 拉模型
            if exe:
                self.after(0, lambda: self.say(
                    f"[{ts()}] 拉模型 {self.model.get()}（第一次可能要几分钟）…"))
                self._run([exe, "pull", self.model.get()])
            # ⑥ 复检 + 写回路径
            st3 = ollama_paths()
            self.after(0, lambda: self.say(
                f"[探测] exe={st3['exe'] or '（没找到）'} API="
                f"{'通' if st3['api_up'] else '不通'} 模型={st3['models']}"))
            if st3["exe"]:
                try:
                    sys.path.insert(0, os.path.join(ROOT, "offline"))
                    import schemas as S
                    S.save_env_config({"ollama": st3["exe"]})
                    self.after(0, lambda: self.say("[配置] 已把 Ollama 路径写进运行环境"))
                except Exception as exc:      # noqa: BLE001
                    self.after(0, lambda e=exc: self.say(f"[配置] 写入失败：{e}"))
            self.after(0, self._done)

        threading.Thread(target=worker, daemon=True).start()

    def _download(self) -> bool:
        import urllib.request
        self.after(0, lambda: self.say(f"[{ts()}] 下载 {SETUP_URL}"))
        try:
            with urllib.request.urlopen(SETUP_URL, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                step = 0
                with open(SETUP_EXE, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        step += 1
                        if step % 20 == 0:      # 每 ~5 MB 报一次，别刷屏
                            pct = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
                            self.after(0, lambda p=pct: self.say(f"    已下载 {p}"))
        except Exception as exc:      # noqa: BLE001
            self.after(0, lambda e=exc: self.say(
                f"[XX] 下载失败：{type(e).__name__}: {e}\n"
                f"     可以手动下载 {SETUP_URL} 存到 {SETUP_EXE} 再点「开始安装」。"))
            return False
        size = os.path.getsize(SETUP_EXE) if os.path.exists(SETUP_EXE) else 0
        self.after(0, lambda: self.say(f"[{ts()}] 下载完成：{SETUP_EXE}（{size >> 20} MB）"))
        return size > 1024 * 1024

    def do_download_only(self):
        def worker():
            os.makedirs(SETUP_DIR, exist_ok=True)
            if self._download():
                self.after(0, lambda: self.say(
                    f"[{ts()}] 安装器在 {SETUP_EXE} —— 双击它按提示装（不需要管理员）。"))
        threading.Thread(target=worker, daemon=True).start()

    def _done(self):
        try:
            self.cancel_btn.configure(state="disabled")
            self.install_btn.configure(state="normal")
        except tk.TclError:
            pass
        self.refresh_preflight()
        if self.on_done:
            try:
                self.on_done()
            except Exception:      # noqa: BLE001
                pass

    def do_cancel(self):
        if not (self.proc and self.proc.poll() is None):
            self.say("当前没有在跑的进程")
            return
        pid = self.proc.pid
        self.say(f"[{ts()}] ⏹ 停止（杀进程树 {pid}）")
        if not PR.kill_tree(pid):
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
