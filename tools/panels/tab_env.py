"""tab_env.py —— 「运行环境」页：服务、同步、插件侧载、诊断、升级

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (
    PY,
    ROOT,
    ts,
)

# 面板/服务里跑子进程的统一入口（stdin=DEVNULL，绕开 pythonw 的坏句柄）
import procrun as PR  # noqa: E402


def llm_sync_js(cfg: dict) -> str:
    """生成"把模型配置写进插件 pref"的那段 JS。

    抽成模块级纯函数是为了能单测（`tests/test_ollama_guide.py` 会把它交给
    `node --check` 验语法，并断言四个 pref 名都在）—— 插件里字段名写错在
    Zotero 里**没有任何报错**，只表现为"同步了但没生效"。

    ⚠ 别把这些花括号塞进 f-string：嵌套 dict 字面量会让 f-string 解析炸掉
      （本机实测 SyntaxError: f-string: expecting '}'）。先 json.dumps 出来。
    """
    vals_js = json.dumps({
        "provider": cfg.get("provider") or "ollama",
        "model": cfg.get("model") or "",
        "apiBaseUrl": cfg.get("base_url") or "",
        "apiKey": cfg.get("api_key") or "",
    }, ensure_ascii=False)
    return (
        'const P = Zotero.ZoteroKB.PREFS;\n'
        'const set = (k, v) => { try { Zotero.Prefs.set(k, v); }'
        ' catch (e) { return "ERR:" + e; } return "ok"; };\n'
        'const vals = ' + vals_js + ';\n'
        'const out = [set(P.provider, vals.provider), set(P.model, vals.model),\n'
        '             set(P.apiBaseUrl, vals.apiBaseUrl), set(P.apiKey, vals.apiKey)];\n'
        'return "插件 pref 已写入：" + JSON.stringify(out);\n'
    )


class EnvTab:
    """「运行环境」页：服务、同步、插件侧载、诊断、升级。"""


    def _build_env_tab(self):
        """运行环境页：项目目录 / Python / Ollama / MinerU 四个位置 + 检测报告。

        为什么值得单独一页：这四个位置**互相独立**，而面板"打不开"
        九成是因为其中一个不对。把它摆在明面上，用户自己就能看出来。

        ⚠ 2026-10-05（用户要求）：这几个位置**可以直接在面板里改**，而且
          写的是与 Zotero 插件设置**同一份配置**（知识库目录下
          `kb-location.json` 的 `env` 段，见 `schemas.save_env_config`；
          插件设置面板点「保存并检测」写的也是它）。
        ⚠ 但要诚实说清一处差别：插件设置页里那几个框显示的是**插件自己记的
          pref**，插件的 `projectRoot()` / `pythonExe()` 会**优先**用它。
          所以"插件设置里曾经手填过某一项"时，想让这里说了算，得先把插件设置里
          那一项清空再保存。界面上（下面的说明与 tooltip）如实写了这句。
        ⚠ MinerU 是**可选组件**（2026-10-05 起）：留空/找不到都不影响任何现有
          功能，只是 PDF 解析走原来的 Zotero 缓存 + PyMuPDF；装它走
          `scripts\\install-mineru.cmd` 或面板「知识库结构」页的安装引导。
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
            text="下面几个位置可以直接在这里改：填好（或点「浏览…」选）"
                 "再点「保存并重新检测」——写的是与 Zotero 插件设置同一份配置。"
                 "留空 = 交回下层：先看项目根目录的 .env，再自动探测"
                 "（例如 Python 留空就用项目里的 .venv）。\n"
                 "优先级：这里保存的值 > .env > 自动探测；两边都写过时，"
                 "每项后面会写明「被谁覆盖」（例如「.env 里也写了，被『运行时设置』"
                 "覆盖，未生效」）。\n"
                 "⚠ 如果你在 Zotero 插件设置页里手填过某一项，插件会优先用那一份 —— "
                 "想让这里说了算，请把插件设置里对应那一项清空再保存。"
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
            ("ollama", "Ollama（可选）", "file",
             "本地模型；不装也能用 API。装了它就能离线跑分类/摘要，"
             "面板里还有图形化安装引导"),
            ("mineru", "MinerU（可选）", "file",
             "mineru-kit.exe；装了 PDF 解析更准（公式成 LaTeX）。"
             "不装也能用；安装见「知识库结构」页的安装引导"),
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
        self._tip(b, "写进 kb-location.json（插件设置页点「保存并检测」写的也是同一份），"
                     "然后重新检测一遍")
        ttk.Button(btns, text="启动本地服务",
                   command=self.do_service_start).pack(side="left", padx=6)
        b_restart = ttk.Button(btns, text="重启本地服务",
                               command=self.do_service_restart)
        b_restart.pack(side="left")
        self._tip(b_restart,
                  "先杀掉残留的 localserver 进程再起 —— 「启动」没反应时点它")
        ttk.Button(btns, text="服务状态", command=self.do_service_status).pack(
            side="left", padx=6)
        b_oll = ttk.Button(btns, text="启动 Ollama", command=self.do_ollama_start)
        b_oll.pack(side="left")
        self._tip(b_oll, "本地模型服务（可选）：它没启动时，分类/摘要/试跑都会报"
                         "「本地模型服务不可用」")
        b_guide = ttk.Button(btns, text="Ollama 安装引导",
                             command=self.open_ollama_guide)
        b_guide.pack(side="left", padx=6)
        self._tip(b_guide, "没装 Ollama 时用它：下载 → 安装 → 启动 → 拉一个模型")
        # Zotero 设置面板那条入口留着（插件的右键功能读的是它自己的 pref），
        # 但面板里现在也能配模型，所以它降为"次要入口"。
        ttk.Button(btns, text="打开 Zotero 设置面板",
                   command=self.do_open_model_settings).pack(side="left")

        # 模型接入区：面板里直接配（写 kb/llm-config.json），见 _build_llm_section
        self._build_llm_section(f)


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
        for key in ("project_root", "python", "ollama", "mineru"):
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
        self.say(f"[{ts()}] 写的是与 Zotero 插件设置同一份配置；"
                 f"若插件设置页里手填过某一项，插件会优先用它"
                 f"（想让这里说了算就把那一项清空再保存）。")
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
            sources, envfile = {}, {}
            try:
                import schemas as S
                import settings as SETT
                fallback = {
                    "project_root": S.resolve_project_root(),
                    "python": S.resolve_python(),
                    "ollama": S.resolve_ollama(),
                    "mineru": S.resolve_mineru(),
                }
                # "这个值从哪来"（默认/自动探测/运行时设置/.env/环境变量）
                sources = S.resolve_sources()
                envfile = {"path": SETT.path(), "keys": SETT.keys()}
            except Exception:  # noqa: BLE001
                fallback = {"project_root": ROOT, "python": PY, "ollama": "",
                            "mineru": ""}
            out = {k: (info.get(k) or fallback.get(k) or "（没找到）")
                   for k in ("project_root", "python", "ollama", "mineru")}
            out["_server"] = bool(info.get("ok"))
            out["_kb"] = info.get("kb_dir") or self.kb_dir()
            out["_err"] = err
            # 来源：优先用**服务端**报的（值与来源出自同一进程，不会错位）；
            # 服务端旧版本没有这一项、或服务没在跑时，用面板自己解析的。
            out["_sources"] = info.get("sources") or sources
            out["_envfile"] = info.get("env_file") or envfile
            # MinerU 状态栏：光有"✓ 存在"没用，用户要知道**它到底能不能干活**
            # （版本、GPU、档位）。探测自身缓存 60 秒，且**没装就不探**。
            if out.get("mineru") and out["mineru"] != "（没找到）":
                try:
                    import mineru as MU
                    mu = MU.probe()
                    out["_mineru"] = MU.summary_line(mu)
                except Exception as exc:  # noqa: BLE001
                    out["_mineru"] = f"探测失败：{type(exc).__name__}: {exc}"
            else:
                out["_mineru"] = ("未检测到（可选组件，不装也能用）—— "
                                  "「知识库结构」页有安装引导")
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

    def _service_up(self, timeout: float = 4.0) -> dict:
        """问一次 /health。返回 {"up": bool, "info": dict, "err": str}。"""
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/health",
                                        timeout=timeout) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            return {"up": bool(info.get("ok", True)), "info": info, "err": ""}
        except Exception as exc:      # noqa: BLE001
            return {"up": False, "info": {}, "err": f"{type(exc).__name__}: {exc}"}

    def _localserver_pids(self) -> list[int]:
        """找出所有 `localserver.py` 进程的 pid。

        ⚠ 不能 `taskkill /IM pythonw.exe` —— 那会连别的插件的 python 一起杀。
          必须按**命令行**过滤，所以走一次 PowerShell 的 CIM 查询。
        """
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or "
              "Name='python.exe'\" | Where-Object { $_.CommandLine -like "
              "'*localserver.py*' } | Select-Object -ExpandProperty ProcessId")
        try:
            r = PR.run(["powershell", "-NoProfile", "-Command", ps],
                       merge_stderr=True, timeout=25)
            return [int(x) for x in (r.stdout or "").split() if x.strip().isdigit()]
        except Exception:      # noqa: BLE001
            return []

    def _service_start_and_wait(self, title: str, tries: int = 15):
        """起服务并**确认它真的起来了**（轮询 /health），失败就自诊断。

        用户 2026-10-05 的原话：「当 ollama 没启动时点『启动本地服务』拉不起来」。
        实测：脚本本身没问题（我手动跑 `wscript 4-service.vbs`，Ollama 没启动时
        服务照样起来并回 /health 200）。真正的问题是**点完没有任何反馈** ——
        原来只写一句"已请求启动"，起没起、为什么没起，用户完全不知道。
        所以这里：先看在不在线 → 起 → 每秒探一次、最多 15 秒 → 仍然不通就
        把"为什么"列出来（残留进程 / 端口被别人占 / 缺 python / 日志尾），
        并提示点「重启本地服务」。
        """
        vbs = os.path.join(ROOT, "scripts", "4-service.vbs")
        if not os.path.exists(vbs):
            messagebox.showerror("缺文件", f"找不到 {vbs}")
            return

        def work():
            first = self._service_up()
            if first["up"]:
                self.out_queue.put(("log", f"[{ts()}] 本地服务**已经在线**，不用重复启动。"))
                self.out_queue.put(("call", (self.refresh_env, None)))
                return
            try:
                PR.spawn(["wscript.exe", vbs])
                self.out_queue.put(("log", f"[{ts()}] ▶ {title}：已拉起后台进程，"
                                           f"正在等它响应…"))
            except Exception as exc:      # noqa: BLE001
                self.out_queue.put(("log", f"[XX] 拉起失败：{exc}"))
                return
            for i in range(tries):
                time.sleep(1.0)
                if self._service_up()["up"]:
                    self.out_queue.put(("log",
                        f"[{ts()}] ✅ 本地服务已在线（等了 {i + 1} 秒）—— "
                        f"插件设置里服务地址填 http://127.0.0.1:8765"))
                    self.out_queue.put(("call", (self.refresh_env, None)))
                    return
            # 15 秒还不通 → 自诊断
            lines = [f"[XX] {title}：等了 {tries} 秒还没响应，下面是排查线索："]
            pids = self._localserver_pids()
            if pids:
                lines.append(f"  · 有 {len(pids)} 个 localserver 进程活着"
                             f"（pid {pids}）—— 可能是**残留进程**："
                             f"启动脚本看到它就不再起新的，点「重启本地服务」可解决")
            else:
                lines.append("  · 没有 localserver 进程 —— 它起来后立刻退出了")
            lines.append(f"  · 最后一次 /health 的错误：{self._service_up()['err']}")
            logf = None
            try:
                logs = [p for p in
                        (os.path.join(self.kb_dir(), "logs", n)
                         for n in os.listdir(os.path.join(self.kb_dir(), "logs")))
                        if "localserver" in os.path.basename(p)]
                if logs:
                    logf = max(logs, key=os.path.getmtime)
            except Exception:      # noqa: BLE001
                logf = None
            if logf:
                try:
                    tail = io.open(logf, encoding="utf-8",
                                   errors="replace").read().splitlines()[-6:]
                    lines.append(f"  · 服务日志尾（{os.path.basename(logf)}）：")
                    lines += ["      " + t for t in tail]
                except Exception:      # noqa: BLE001
                    pass
            lines.append("  → 先点本页「重启本地服务」；还不行就看上面日志尾。")
            self.out_queue.put(("log", "\n".join(lines)))

        threading.Thread(target=work, daemon=True).start()

    def do_service_start(self):
        """启动本地服务（起完**确认**它在响应，不再是"已请求"就完事）。"""
        self._service_start_and_wait("启动本地服务")

    def do_service_restart(self):
        """重启：先杀掉残留的 localserver 进程，再起。"""
        pids = self._localserver_pids()
        if pids and not messagebox.askyesno(
                "重启本地服务",
                f"要杀掉这些 localserver 进程再重启：\n  {pids}\n\n"
                f"（只杀命令行里带 localserver.py 的，不动别的 python 进程）\n"
                f"Zotero 里刚派出去还没领的任务会丢，其余不受影响。继续？"):
            return
        for pid in pids:
            PR.kill_tree(pid)      # 失败也无所谓，下面照样尝试重启
        if pids:
            self.say(f"[{ts()}] 已停 {len(pids)} 个 localserver 进程，准备重启…")
            time.sleep(1.5)
        self._service_start_and_wait("重启本地服务")


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


    # ---------------------------------------------------------- 模型接入

    def _build_llm_section(self, f):
        """「模型接入」：在面板里就能配模型（原来只能去 Zotero 设置面板改）。

        用户 2026-10-05 的要求：「那个『打开模型设置（在 Zotero 中）』能不能也
        同时能在面板中设置。」能，而且这里写的是**服务端与面板共用的那一份**：
        知识库目录下的 `kb/llm-config.json`（`offline/judge.py` 每次调用都重读它，
        改完立刻生效）。面板的所有模型功能（分类建议 / 元数据兜底 / 提示词试跑 /
        经验草稿 / 摘要标签）都走这条。

        ⚠ 但 Zotero 插件**右键菜单**那几项读的是它自己的 pref（`07-classify.js`
          把 provider/model/base_url/api_key 一起发给服务端）。所以这里另给一个
          「同步到 Zotero 插件」按钮：通过任务队列让插件把这几项 pref 写成同一份值
          —— 这样两边不会各说各话（本项目的教训：两个写入口迟早漂移）。
        """
        box = ttk.LabelFrame(f, text=" 模型接入（面板与服务端共用；插件可一键同步） ",
                             padding=10)
        box.pack(fill="x", padx=10, pady=(10, 0))

        r1 = ttk.Frame(box)
        r1.pack(fill="x")
        ttk.Label(r1, text="服务商：").pack(side="left")
        self.llm_provider = tk.StringVar(value="ollama")
        ttk.Combobox(r1, textvariable=self.llm_provider, width=10, state="readonly",
                     values=("ollama", "openai")).pack(side="left")
        ttk.Label(r1, text="  模型：").pack(side="left")
        self.llm_model = tk.StringVar(value="")
        self.llm_model_box = ttk.Combobox(r1, textvariable=self.llm_model, width=28)
        self.llm_model_box.pack(side="left")
        b = ttk.Button(r1, text="刷新本地模型", command=self.do_load_ollama_models)
        b.pack(side="left", padx=6)
        self._tip(b, "从 Ollama 拉模型列表（没启动 Ollama 就先点上面的「启动 Ollama」）")

        r2 = ttk.Frame(box)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="API 地址：").pack(side="left")
        self.llm_base_url = tk.StringVar(value="")
        ttk.Entry(r2, textvariable=self.llm_base_url, width=30,
                  font=(self.mono_font, 9)).pack(side="left")
        ttk.Label(r2, text=" Key：").pack(side="left")
        self.llm_api_key = tk.StringVar(value="")
        self.llm_key_entry = ttk.Entry(r2, textvariable=self.llm_api_key, width=20,
                                       show="*", font=(self.mono_font, 9))
        self.llm_key_entry.pack(side="left")
        self.llm_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="显示", variable=self.llm_show_key,
                        command=lambda: self.llm_key_entry.configure(
                            show="" if self.llm_show_key.get() else "*")
                        ).pack(side="left", padx=2)
        # ⚠ 这三个输入框挤在一行是为了**纵向省一格**：这一页内容多，
        #   Notebook 的请求高度会顶到日志区（本机吃过"日志只剩 5 行"的亏），
        #   所以能并排就并排。
        ttk.Label(r2, text=" Ollama：").pack(side="left")
        self.llm_ollama_host = tk.StringVar(value="")
        ttk.Entry(r2, textvariable=self.llm_ollama_host, width=22,
                  font=(self.mono_font, 9)).pack(side="left")

        r4 = ttk.Frame(box)
        r4.pack(fill="x", pady=(6, 0))
        ttk.Button(r4, text="保存模型配置", command=self.do_save_llm).pack(
            side="left")
        b_test = ttk.Button(r4, text="测试连接", command=self.do_test_llm)
        b_test.pack(side="left", padx=6)
        self._tip(b_test, "真发一次最小请求（ollama 只查 /api/tags），把结果写进日志")
        b_sync = ttk.Button(r4, text="同步到 Zotero 插件",
                            command=self.do_sync_llm_to_plugin)
        b_sync.pack(side="left")
        self._tip(b_sync, "把这份配置写进插件的 pref —— 插件右键菜单那几项读的是它"
                          "（需要 Zotero 在运行）")

        self.llm_note = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.llm_note, foreground="#666",
                  justify="left", wraplength=900, font=(self.ui_font, 8)).pack(
            fill="x", pady=(6, 0))
        self.refresh_llm()

    def refresh_llm(self):
        """把 kb/llm-config.json 读进这一区，并顺手报 Ollama 三态。"""
        def work():
            out = {"cfg": {}, "ollama": {"up": False, "models": []}, "err": "",
                   "path": ""}
            try:
                import judge as J
                import schemas as S
                out["cfg"] = J.load_llm_config()
                out["path"] = S.resolve_ollama() or ""
                up, models = J.ollama_models()
                out["ollama"] = {"up": up, "models": models}
            except Exception as exc:      # noqa: BLE001
                out["err"] = f"{type(exc).__name__}: {exc}"
            self.out_queue.put(("call", (self._apply_llm, out)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_llm(self, out: dict):
        cfg = out.get("cfg") or {}
        self.llm_provider.set(str(cfg.get("provider") or "ollama"))
        self.llm_model.set(str(cfg.get("model") or ""))
        self.llm_base_url.set(str(cfg.get("base_url") or ""))
        self.llm_api_key.set(str(cfg.get("api_key") or ""))
        self.llm_ollama_host.set(str(cfg.get("ollama_host")
                                     or "http://127.0.0.1:11434"))
        oll = out.get("ollama") or {}
        models = list(oll.get("models") or [])
        try:
            self.llm_model_box.configure(values=models)
        except tk.TclError:
            pass
        # ⚠ Ollama 路径取**探测结果**（out["path"]），不取上面那个输入框：
        #   输入框是异步填的，构造这一区时还是空的 —— 本机截图里出现过
        #   "上面写着路径、下面说未找到"的自相矛盾。
        path = out.get("path") or "（没找到，可选）"
        if oll.get("up"):
            state = ("✅ 正在运行" + ("，本机模型：" + "、".join(models[:6])
                                    if models else
                                    "，但还没有模型（命令行跑 ollama pull "
                                    + "qwen3:4b-instruct）"))
        elif out.get("path"):
            state = "⚠ 装了但没启动 —— 点上面的「启动 Ollama」"
        else:
            state = ("✗ 没装（可选组件）。可以点「Ollama 安装引导」，"
                     "或把服务商改成 openai 用 API 模型")
        note = (f"Ollama：{state}\n"
                f"程序位置：{path}\n"
                "这一区保存到知识库目录下的 llm-config.json（面板与服务端所有模型功能"
                "都用它）；Zotero 插件右键菜单用的是插件自己的 pref → "
                "点「同步到 Zotero 插件」推过去。")
        if out.get("err"):
            note += f"\n读配置出错：{out['err']}"
        self.llm_note.set(note)

    def do_load_ollama_models(self):
        self.say(f"[{ts()}] 正在向 Ollama 要模型列表…")
        self.refresh_llm()

    def do_save_llm(self):
        """保存到 kb/llm-config.json（与 /llm-config 写的是同一份）。"""
        try:
            import judge as J
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("保存失败", f"导不进 judge：{exc}")
            return
        vals = {
            "provider": (self.llm_provider.get() or "").strip(),
            "model": (self.llm_model.get() or "").strip(),
            "base_url": (self.llm_base_url.get() or "").strip(),
            "api_key": (self.llm_api_key.get() or "").strip(),
            "ollama_host": (self.llm_ollama_host.get() or "").strip(),
        }
        if vals["provider"] == "openai" and not vals["base_url"]:
            J_guess = ""
            try:
                J_guess = J.guess_base_url_from_model(vals["model"])
            except Exception:      # noqa: BLE001
                J_guess = ""
            if J_guess:
                vals["base_url"] = J_guess
                self.llm_base_url.set(J_guess)
        try:
            path = J.save_llm_config(**vals)
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}")
            return
        self.say(f"[{ts()}] 模型配置已保存：{path}")
        for k, v in vals.items():
            if k == "api_key":
                v = (v[:6] + "…") if v else ""
            self.say(f"    {k} = {v or '（空）'}")
        self.refresh_llm()

    def do_test_llm(self):
        """测试连接：ollama 只查 /api/tags；openai 兼容走一次最小生成。"""
        def work():
            try:
                import judge as J
                cfg = J.load_llm_config()
                provider = (self.llm_provider.get() or cfg.get("provider")
                            or "ollama")
                model = (self.llm_model.get() or cfg.get("model") or "")
                if provider == "ollama":
                    up, models = J.ollama_models(
                        (self.llm_ollama_host.get() or "").strip())
                    if not up:
                        msg = ("❌ Ollama 没响应。检查：① 装了没（「Ollama 安装引导」）"
                               "② 启动了没（点「启动 Ollama」）")
                    elif not models:
                        msg = "✅ Ollama 通，但**一个模型都没有** —— 跑一次 `ollama pull qwen3:4b-instruct`"
                    else:
                        pick = J.pick_model(model, (self.llm_ollama_host.get() or "").strip())
                        msg = (f"✅ Ollama 通，模型 {len(models)} 个；"
                               f"当前会用它：**{pick}**"
                               + ("（你填的那个不在本机）"
                                  if model and model not in models else ""))
                else:
                    res = J.generate("只回两个字：可用", model=model,
                                     provider=provider, temperature=0.0,
                                     timeout=60.0)
                    msg = ("✅ API 可用：" + str(res.get("text") or "")[:40]
                           if res.get("ok") else
                           f"❌ API 不通：{res.get('error')}")
                self.out_queue.put(("log", f"[{ts()}] 测试连接：{msg}"))
                self.out_queue.put(("call", (lambda: self.llm_note.set(msg), None)))
            except Exception as exc:      # noqa: BLE001
                self.out_queue.put(("log", f"[{ts()}] 测试连接失败：{type(exc).__name__}: {exc}"))

        self.say(f"[{ts()}] 正在测试模型连接…")
        threading.Thread(target=work, daemon=True).start()

    def do_sync_llm_to_plugin(self):
        """把这份配置写进 Zotero 插件的 pref（走任务队列让插件自己写）。

        为什么绕一圈让插件写：插件读的是 `extensions.zotero.zotero-kb.*`，
        只有 Zotero 进程自己能改；而我们已经有任务队列这条通路（`tools/zotero_js.py`）。
        """
        try:
            import judge as J
            cfg = J.load_llm_config()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("同步失败", f"读配置失败：{exc}")
            return
        # ⚠ 别把这些花括号塞进 f-string：嵌套 dict 字面量会让 f-string 解析炸掉
        #   （本机实测 SyntaxError: f-string: expecting '}'）。先 json.dumps 出来。
        # （见模块顶部的 llm_sync_js —— 抽出去是为了单测能直接验这段 JS。）
        js = llm_sync_js(cfg)
        import tempfile
        tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                          encoding="utf-8", newline="\n")
        try:
            tmp.write(js)
            tmp.close()
            self.run("同步模型配置到 Zotero 插件",
                     [os.path.join(ROOT, "tools", "zotero_js.py"), "run", tmp.name])
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("同步失败", f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Ollama

    def do_ollama_start(self):
        """启动 Ollama（先试托盘应用 `ollama app.exe`，再退到 `ollama serve`）。"""
        try:
            import schemas as S
            exe = S.resolve_ollama()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("启动失败", f"解析 Ollama 路径出错：{exc}")
            return
        if not exe:
            messagebox.showinfo(
                "没找到 Ollama",
                "本机没装 Ollama（它是可选组件）。\n\n"
                "点本页的「Ollama 安装引导」可以图形化装一个；\n"
                "只想用 API 模型的话，下面「模型接入」里把服务商改成 openai 就行。")
            return
        app = os.path.join(os.path.dirname(exe), "ollama app.exe")

        def work():
            started = ""
            for cand, mode in ((app, "托盘应用"), (exe, "serve")):
                if not os.path.exists(cand):
                    continue
                try:
                    args = [cand, "serve"] if cand == exe else [cand]
                    PR.spawn(args, cwd=os.path.dirname(cand))
                    started = f"{mode}（{os.path.basename(cand)}）"
                    break
                except Exception as exc:      # noqa: BLE001
                    self.out_queue.put(("log", f"[XX] 启动 {cand} 失败：{exc}"))
            if not started:
                self.out_queue.put(("log", f"[XX] 没找到可启动的 Ollama 程序（{exe}）"))
                return
            self.out_queue.put(("log", f"[{ts()}] 已请求启动 Ollama：{started}，等它就绪…"))
            import judge as J
            for i in range(12):
                time.sleep(1.0)
                up, models = J.ollama_models()
                if up:
                    self.out_queue.put(("log",
                        f"[{ts()}] ✅ Ollama 已就绪（等了 {i + 1} 秒），本机模型："
                        + ("、".join(models) if models else "（还没有模型）")))
                    break
            else:
                self.out_queue.put(("log",
                    "[XX] 等了 12 秒 Ollama 还没响应。可以看托盘图标是否出现；"
                    "或者它可能装在别处 —— 在「运行环境」把 Ollama 路径填上再试。"))
            self.out_queue.put(("call", (self.refresh_llm, None)))

        self.say(f"[{ts()}] 正在启动 Ollama…")
        threading.Thread(target=work, daemon=True).start()

    def open_ollama_guide(self):
        """打开 Ollama 安装引导（与 MinerU 那个同一套形态）。"""
        from .ollama_guide import OllamaGuide
        win = OllamaGuide(self.root, self)
        win.focus_set()

    def do_open_model_settings(self):
        """打开 Zotero 的设置窗口，让用户去插件的设置面板里选模型。

        为什么不做在面板里选：**模型接入统一由 Zotero 插件的设置面板负责**。
        理由（用户明确要求）：插件是给别人用的，用户装完插件就能在自己熟悉的
        设置界面里配模型；面板是本机工具，不该成为"必须打开才知道去哪配"的地方。
        而且插件在调 /classify 时会把设置里的模型一起传过去，服务端按它走 ——
        所以配置源只有一个。
        """
        import schemas as S              # 本方法自己 import（本文件其它方法也都这么写）
        zotero = S.resolve_zotero()      # 自动探测；找不到时下面会提示写 .env
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
