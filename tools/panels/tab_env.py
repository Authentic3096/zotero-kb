"""tab_env.py —— 「运行环境」页：服务、同步、插件侧载、诊断、升级

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (
    CREATE_NO_WINDOW,
    PY,
    ROOT,
    ts,
)


class EnvTab:
    """「运行环境」页：服务、同步、插件侧载、诊断、升级。"""


    def _build_env_tab(self):
        """运行环境页：项目目录 / Python / Ollama 三个位置 + 检测报告。

        为什么值得单独一页：这三个位置**互相独立**，而面板"打不开"
        九成是因为其中一个不对。把它摆在明面上，用户自己就能看出来。

        ⚠ 2026-10-05（用户要求）：这三个位置**可以直接在面板里改**，而且
          与 Zotero 插件设置**同步** —— 两边读写的是同一份配置
          （知识库目录下 `kb-location.json` 的 `env` 段，见
          `schemas.write_location_config`；插件设置面板走 `/env-config`
          写的也是它）。所以这里是可编辑输入框 + 「浏览…」+「保存并重新检测」。
        """
        f = self.tab_env
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="运行环境", font=(self.ui_font, 11, "bold")).pack(side="left")
        ttk.Button(head, text="重新检测", command=self.refresh_env).pack(
            side="right")

        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=920,
            text="下面三个位置可以直接在这里改：填好（或点「浏览…」选）"
                 "再点「保存并重新检测」。它和 Zotero 插件设置里的"
                 "「运行环境」是同一份配置 —— 在哪边改，两边都用新值。\n"
                 "留空 = 这一项交回自动探测（例如 Python 留空就用项目里的 .venv）。"
        ).pack(fill="x", padx=12, pady=(6, 4))

        body = ttk.Frame(f, padding=(10, 2, 10, 4))
        body.pack(fill="x")

        self.env_vars = {}
        self.env_ok = {}          # 每项后面的 "✓ 存在 / ✗ 找不到" 小字
        rows = [
            ("project_root", "项目目录", "dir",
             "代码和 .venv 所在（含 offline、online）"),
            ("python", "Python 解释器", "file",
             "跑脚本用；优先 .venv\\Scripts\\pythonw.exe"),
            ("ollama", "Ollama 程序", "file",
             "本地模型（可选，不装也能用 API）"),
        ]
        for i, (key, label, kind, hint) in enumerate(rows):
            ttk.Label(body, text=label + "：").grid(row=i * 2, column=0,
                                                    sticky="w", pady=(6, 0))
            var = tk.StringVar(value="")
            self.env_vars[key] = var
            ttk.Entry(body, textvariable=var, font=(self.mono_font, 9),
                      width=70).grid(row=i * 2, column=1, sticky="we",
                                     padx=(6, 0), pady=(6, 0))
            okv = tk.StringVar(value="")
            self.env_ok[key] = okv
            ttk.Label(body, textvariable=okv, foreground="#888").grid(
                row=i * 2, column=2, sticky="w", padx=(6, 0))
            ttk.Button(body, text="浏览…", width=7,
                       command=lambda k=key, kd=kind: self.do_browse_env(k, kd)
                       ).grid(row=i * 2, column=3, sticky="w", padx=(6, 0))
            ttk.Label(body, text=hint, foreground="#888",
                      font=(self.ui_font, 8)).grid(row=i * 2 + 1, column=1,
                                                   sticky="w", padx=(6, 0))
        body.columnconfigure(1, weight=1)

        self.env_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.env_note, foreground="#666",
                  wraplength=900, justify="left").pack(
            fill="x", padx=12, pady=(10, 4))

        btns = ttk.Frame(f, padding=(10, 0, 10, 8))
        btns.pack(fill="x")
        b = ttk.Button(btns, text="保存并重新检测", command=self.do_save_env)
        b.pack(side="left")
        self._tip(b, "写进 kb-location.json（与 Zotero 插件设置同一份配置），"
                     "然后重新检测一遍")
        ttk.Button(btns, text="打开模型设置（在 Zotero 里）",
                   command=self.do_open_model_settings).pack(side="left",
                                                             padx=6)
        ttk.Button(btns, text="启动本地服务", command=self.do_service_start).pack(
            side="left", padx=6)
        ttk.Button(btns, text="服务状态", command=self.do_service_status).pack(
            side="left")


    # ---------------------------------------------------------- 改这三个位置

    def do_browse_env(self, key: str, kind: str):
        """「浏览…」：目录用 askdirectory，文件用 askopenfilename。"""
        from tkinter import filedialog
        cur = (self.env_vars.get(key).get() if key in self.env_vars else "") or ""
        cur = cur.strip()
        kw = {"parent": self.root,
              "title": "选择" + ("目录" if kind == "dir" else "程序")}
        if cur:
            kw["initialdir"] = cur if os.path.isdir(cur) else os.path.dirname(cur)
        try:
            if kind == "dir":
                picked = filedialog.askdirectory(**kw)
            else:
                picked = filedialog.askopenfilename(
                    filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
                    **kw)
        except Exception as exc:      # noqa: BLE001
            self.say(f"[XX] 打开选择器失败：{exc}")
            return
        if not picked:
            return
        picked = os.path.normpath(picked)
        self.env_vars[key].set(picked)
        if key in self.env_ok:
            self.env_ok[key].set("✓ 存在" if os.path.exists(picked) else "✗ 找不到")
        # 顺手把另外两项的状态也重算一遍（用户可能刚补上另一个）
        for k, v in self.env_vars.items():
            p = (v.get() or "").strip()
            if k in self.env_ok and p and k != key:
                self.env_ok[k].set("✓ 存在" if os.path.exists(p) else "✗ 找不到")


    def do_save_env(self):
        """保存三个位置 —— 与插件设置 / `/env-config` 写的是**同一份配置**。

        ⚠ 真正的写入与校验在 `schemas.save_env_config`（唯一一份实现）：
          路径不存在就**不保存**（与 /env-config 一致），留空 = 交回自动探测。
        """
        try:
            import schemas as S
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("保存失败", f"导不进 schemas：{exc}")
            return
        given = {}
        for key in ("project_root", "python", "ollama"):
            if key in self.env_vars:
                given[key] = (self.env_vars[key].get() or "").strip()
        try:
            res = S.save_env_config(given)
        except ValueError as exc:
            messagebox.showerror(
                "没有保存", f"{exc}\n\n请点「浏览…」重新选；确实不想用这一项"
                            "就把框清空（留空 = 交回自动探测）。")
            return
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}")
            return
        saved = res.get("saved") or {}
        self.say(f"[{ts()}] 运行环境已保存到 {res.get('path')}："
                 + "、".join(f"{k}={v or '(自动探测)'}" for k, v in saved.items()))
        self.say(f"[{ts()}] Zotero 插件设置读的就是这一份配置 —— 两边已经同步。")
        self.refresh_env()


    def refresh_env(self):
        """从本机服务（或直接探测）取运行环境，填进这一页。"""
        def work():
            info = {}
            err = ""
            try:
                import urllib.request
                with urllib.request.urlopen(
                        "http://127.0.0.1:8765/health", timeout=4) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                info = {}
            # 服务不在线就自己探测（不依赖服务，页面照样有内容）
            try:
                import schemas as S
                fallback = {
                    "project_root": S.resolve_project_root(),
                    "python": S.resolve_python(),
                    "ollama": S.resolve_ollama(),
                }
            except Exception:  # noqa: BLE001
                fallback = {"project_root": ROOT, "python": PY, "ollama": ""}
            out = {k: (info.get(k) or fallback.get(k) or "（没找到）")
                   for k in ("project_root", "python", "ollama")}
            out["_server"] = bool(info.get("ok"))
            out["_kb"] = info.get("kb_dir") or self.kb_dir()
            out["_err"] = err
            self.out_queue.put(("env", out))
            # 排错用：探测失败时把原因写进日志（吞掉异常会让"服务没在跑"
            # 这句话看起来像结论，其实只是"请求失败了"，用户无从下手）
            if err:
                self.out_queue.put(("log", f"[!!] 连本机服务失败：{err}"))

        threading.Thread(target=work, daemon=True).start()


    # ---------------------------------------------------------- 分类重整

    def _sync(self, action: str, title: str):
        self.run(title, [os.path.join(ROOT, "tools", "zotero_sync.py"), action])


    def do_sync_check(self):
        self._sync("check", "探测 Zotero 写入能力")


    def do_sync_authorize(self):
        self._sync("authorize", "申请 Zotero 写入授权（留意 Zotero 弹窗）")


    def do_sync_plan(self):
        self._sync("plan", "分类重整 · 干跑")


    def do_sync_apply(self):
        self.run("分类重整 · 执行",
                 [os.path.join(ROOT, "tools", "zotero_sync.py"), "apply"],
                 confirm="会真的修改 Zotero 的分类归属（并同步到 zotero.org/坚果云）。\n"
                         "建议先点「干跑」看清楚要动哪些。继续？")


    def do_sync_verify(self):
        self._sync("verify", "分类重整 · 核对")


    # ---------------------------------------------------------- 本地服务

    def do_service_start(self):
        """后台起本地服务（用 VBS，无窗口）。"""
        vbs = os.path.join(ROOT, "scripts", "4-service.vbs")
        if not os.path.exists(vbs):
            messagebox.showerror("缺文件", f"找不到 {vbs}")
            return
        try:
            subprocess.Popen(["wscript.exe", vbs],
                             creationflags=CREATE_NO_WINDOW)
            self.say(f"[{ts()}] 已请求启动本地服务（后台）。"
                     f"2 秒后可点「服务状态」确认。")
        except Exception as exc:  # noqa: BLE001
            self.say(f"[XX] 启动失败：{exc}")


    def do_service_status(self):
        """看本地服务状态。

        ⚠ 用户反馈："运行环境中点击服务状态日志没显示结果。"
          根因：结果被写到「高级」页的输出框（`self.adv_text`），而用户是在
          「运行环境」页点的按钮 —— 输出跑到看不见的地方去了。
          这里改成**结果写到哪一页看得见就写到哪**：
            · 「运行环境」页有专门的探测区，状态本来就显示在那里（顺手刷新它）
            · 详细文本同时进底部日志（全局可见）
            · 还要弹一个窗口 —— 因为"复制 token"这种场景用户要选中文本
        """
        def work():
            import urllib.request
            url = "http://127.0.0.1:8765/health"
            try:
                with urllib.request.urlopen(url, timeout=6) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                body = ("本地服务：在线 ✅\n"
                        f"  地址：http://127.0.0.1:8765\n"
                        f"  服务：{info.get('service')} v{info.get('version')}\n"
                        f"  知识库：{info.get('kb_dir')}\n"
                        f"  项目目录：{info.get('project_root')}\n"
                        f"  Python：{info.get('python')}\n"
                        f"  Ollama：{info.get('ollama') or '（未找到，可选）'}\n\n"
                        "插件设置里填：\n"
                        f"  服务地址 = http://127.0.0.1:8765\n"
                        f"  token    = {self._read_token()}\n")
            except Exception as exc:  # noqa: BLE001
                body = ("本地服务：离线 ❌\n"
                        f"  {type(exc).__name__}: {exc}\n\n"
                        "启动方式：\n"
                        "  1) 双击 scripts\\4-service.vbs（后台无窗口）\n"
                        "  2) 或本页「启动本地服务」按钮\n"
                        "  3) 或命令行 python online\\localserver.py\n")
            self.out_queue.put(("log", body.rstrip()))
            self.out_queue.put(("dialog", ("本地服务状态", body)))
            # 顺手刷新这一页的探测结果 —— 用户就在这一页，状态该当场更新
            self.refresh_env()

            self.refresh_quality()

        threading.Thread(target=work, daemon=True).start()


    def do_copy_token(self):
        tok = self._read_token()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(tok)
            self.root.update()
            self.say(f"[{ts()}] token 已复制到剪贴板：{tok[:8]}…"
                     f"（粘到 Zotero 插件设置里）")
        except Exception as exc:  # noqa: BLE001
            self.say(f"[XX] 复制失败：{exc}；token 是：{tok}")


    def do_build_plugin(self):
        self.run("打包 Zotero 插件 xpi",
                 [os.path.join(ROOT, "tools", "check_plugin.py")])


    # ---------------------------------------------------------- 插件安装

    def do_sideload_status(self):
        self.run("插件侧载状态",
                 [os.path.join(ROOT, "tools", "sideload_plugin.py"), "status"])


    def do_sideload_install(self):
        self.run(
            "侧载安装插件（需先完全退出 Zotero）",
            [os.path.join(ROOT, "tools", "sideload_plugin.py"), "install"],
            confirm="侧载会：\n"
                    "  1. 备份 profile 的 prefs.js / extensions.json\n"
                    "  2. 在 profile\\extensions 放一个代理文件指向插件源码\n"
                    "  3. 删掉 prefs.js 里的 extensions.lastAppVersion/BuildId\n"
                    "     （让 Zotero 下次启动重扫扩展目录）\n\n"
                    "必须先把 Zotero 完全退出，否则不生效。\n继续？")


    def do_sideload_remove(self):
        self.run("卸载侧载",
                 [os.path.join(ROOT, "tools", "sideload_plugin.py"), "remove"],
                 confirm="这会移除侧载痕迹（需先退出 Zotero）。继续？")


    def do_diagnose(self):
        self.run("诊断插件安装问题",
                 [os.path.join(ROOT, "tools", "diagnose_plugin.py")])


    def do_upgrade_check(self):
        self.run("Zotero 升级前检查",
                 [os.path.join(ROOT, "tools", "zotero_upgrade.py"), "check"])


    def do_upgrade_backup(self):
        self.run("备份 Zotero 数据目录",
                 [os.path.join(ROOT, "tools", "zotero_upgrade.py"), "backup"])


    def do_upgrade_verify(self):
        self.run("Zotero 升级后核对",
                 [os.path.join(ROOT, "tools", "zotero_upgrade.py"), "verify"])


    def do_open_model_settings(self):
        """打开 Zotero 的设置窗口，让用户去插件的设置面板里选模型。

        为什么不做在面板里选：**模型接入统一由 Zotero 插件的设置面板负责**。
        理由（用户明确要求）：插件是给别人用的，用户装完插件就能在自己熟悉的
        设置界面里配模型；面板是本机工具，不该成为"必须打开才知道去哪配"的地方。
        而且插件在调 /classify 时会把设置里的模型一起传过去，服务端按它走 ——
        所以配置源只有一个。
        """
        zotero = r"D:\Application\Zotero\zotero.exe"
        if not os.path.exists(zotero):
            messagebox.showinfo(
                "找不到 Zotero",
                f"没找到 {zotero}\n\n请手动打开 Zotero → 编辑 → 设置 → 文献知识库，"
                "在「使用的本地模型」里填模型名（留空=自动挑）。")
            return
        try:
            # Zotero 支持用 -ZoteroPane 之类的参数，但"直接打开某个设置面板"
            # 没有官方入口。所以这里只把设置窗口打开，再告诉用户点哪里。
            os.startfile(zotero, "open", "-ZoteroPane")
            messagebox.showinfo(
                "模型设置在哪",
                "已尝试打开 Zotero。\n\n"
                "请到：编辑 → 设置 → 文献知识库 → 「使用的本地模型」\n"
                "填模型名（如 qwen3:4b-instruct），留空表示自动挑选。\n\n"
                "说明：这一项是插件与服务端共用的 —— 插件给文献分类时会把"
                "它一起发过去，服务端按它调用；面板也走同一条路径，所以两边一致。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo(
                "请手动打开",
                f"自动打开失败：{exc}\n\n"
                "请手动：编辑 → 设置 → 文献知识库 → 「使用的本地模型」")
