"""base.py —— 窗口骨架与通用能力：装配、日志、跑子进程、状态刷新

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .common import (
    CREATE_NO_WINDOW,
    ENV,
    PY,
    ROOT,
    apply_ui_fonts,
    ts,
)


class AppBase:
    """窗口骨架与通用能力（所有页签都用它）。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.out_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.proc: subprocess.Popen | None = None

        root.title("Zotero 文献知识库 · 管理面板")
        self._set_window_icon(root)
        # 字体要在建控件**之前**定好 —— 否则先建的控件用旧字体，后建的用新字体，
        # 同一页里两种字体混着，又回到"字距看着奇怪"那个问题上。
        self.ui_font, self.mono_font = apply_ui_fonts(root)
        # 初始尺寸按屏幕来，别用固定值 —— 屏幕小的时候要能装下，
        # 屏幕大的时候默认就给足空间（实测 2560x1440 下 1000x720 显得局促）。
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1180, max(900, int(sw * 0.46)))
        h = min(880, max(620, int(sh * 0.62)))
        x = max(0, (sw - w) // 2)
        y = max(0, int((sh - h) * 0.35))
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(860, 580)

        self._build_header()
        # ⚠ 顺序有讲究：日志用 side="bottom" 钉在底部，必须**先** pack 它，
        #   否则 Notebook（expand=True）会先占满整个区域，日志被挤成一条线。
        self._build_log()
        self._build_tabs()

        self.root.after(120, self._drain_queue)
        self.refresh_status()
        self.root.after(400, self.refresh_env)


    # ================================================================ 路径

    @staticmethod
    def kb_dir() -> str:
        """知识库目录（数据在哪）。

        ⚠ 不能用 `ROOT + "\\kb"`：知识库默认跟着 Zotero 数据目录走，
          用户还能在 Zotero 插件设置里改到别处。唯一权威来源是
          schemas 的解析链（环境变量 > 配置 > 跟随 Zotero > 老位置）。
        """
        try:
            import schemas as S
            return S.KB_DIR
        except Exception:  # noqa: BLE001
            return os.path.join(ROOT, "kb")


    # ================================================================ 布局

    def _set_window_icon(self, root: tk.Tk):
        """把窗口图标设成**插件那枚图标**（`tools/assets/icon.png` / `.ico`）。

        两条路都走一遍：
          · `iconbitmap`（.ico）—— Windows 上最稳，任务栏和 Alt-Tab 也吃这个；
          · `iconphoto`（.png）—— 部分主题下 iconbitmap 不生效，这个兜底。

        ⚠ 整段包在 try 里：图标是装饰，**缺了也绝不能让面板起不来**。
          生成脚本：`tools/make_panel_icon.py`（由 icon.svg 渲染而来）。
        """
        ico = os.path.join(ROOT, "tools", "assets", "icon.ico")
        png = os.path.join(ROOT, "tools", "assets", "icon.png")
        if os.path.exists(ico):
            try:
                root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        if os.path.exists(png):
            try:
                # 必须留引用，否则被 GC 掉、图标又变回 Tk 的羽毛
                self._icon_img = tk.PhotoImage(file=png)
                root.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass


    def _build_header(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 2))
        top.pack(fill="x")

        # ---- 头像 + 署名（在最左，和标题一行）
        #
        # 头像用的是用户自己那张"猫 + 书 + DeepSeek 鲸鱼吊坠"，和这个项目的意象
        # 正好对上。**图片加载失败绝不能把面板带崩** —— 它只是装饰，
        # 所以整段包在 try 里，拿不到就静默跳过（面板照常可用）。
        self._avatar_img = None          # 必须留引用，否则会被 GC 掉、图片消失
        try:
            img = self._load_avatar(56)
            if img is not None:
                self._avatar_img = img
                lbl = ttk.Label(top, image=img)
                lbl.pack(side="left", padx=(0, 10))
        except Exception:                                     # noqa: BLE001
            pass

        # 标题与署名竖排一列，紧跟在头像右边
        title_col = ttk.Frame(top)
        title_col.pack(side="left")
        row1 = ttk.Frame(title_col)
        row1.pack(anchor="w")
        ttk.Label(row1, text="Zotero 文献知识库",
                  font=(self.ui_font, 15, "bold")).pack(side="left")
        ttk.Label(row1, text="管理面板", foreground="#888").pack(
            side="left", padx=(8, 0))
        ttk.Label(title_col, text="by DeepSeek and Authentic3096",
                  foreground="#999", font=(self.ui_font, 8)).pack(
            anchor="w", pady=(1, 0))

        ttk.Button(top, text="刷新", command=self.refresh_all).pack(side="right")
        ttk.Button(top, text="停止当前任务", command=self.stop_task).pack(
            side="right", padx=6)

        # ---- 状态卡片：一进来就看见"库在哪、有多少东西"
        card = ttk.LabelFrame(self.root, text=" 状态 ", padding=(12, 8))
        card.pack(fill="x", padx=12, pady=(6, 4))

        self.kb_path_var = tk.StringVar(value="读取中…")
        ttk.Label(card, text="知识库位置：").grid(row=0, column=0, sticky="w")
        ttk.Label(card, textvariable=self.kb_path_var,
                  font=(self.mono_font, 9), foreground="#06c").grid(
            row=0, column=1, sticky="w", padx=(4, 10))

        self.status_var = tk.StringVar(value="正在读取状态…")
        ttk.Label(card, text="内容：").grid(row=1, column=0, sticky="w",
                                            pady=(4, 0))
        ttk.Label(card, textvariable=self.status_var).grid(
            row=1, column=1, sticky="w", padx=(4, 10), pady=(4, 0))

        # ---- 核心按钮：最常用的五个，一直可见
        bar = ttk.Frame(self.root, padding=(12, 4, 12, 2))
        bar.pack(fill="x")
        # ⚠ 按钮名要"看一眼知道是干什么的"。
        #   用户反馈：「更新索引（增量）」这个名字本身就很奇怪 ——
        #   "增量"是给写代码的人看的词，用户只关心"我新加了文献，点它更新"。
        quick = [
            ("手动更新", self.do_convert_incremental,
             "新加了文献、或改了笔记/标注之后点这个（只处理变了的，很快）"),
            ("全部重建", self.do_convert_full,
             "重新解析所有文献、重算向量。一般不用，除非索引坏了或换了模型"),
            ("环境自检", self.do_check, "检查依赖、索引、向量是否正常"),
            ("备份", self.do_backup, "备份索引（含经验层和权重）"),
            ("打开知识库目录", self.open_folder, "在文件管理器里打开"),
        ]
        for text, cmd, tip in quick:
            b = ttk.Button(bar, text=text, command=cmd, width=16)
            b.pack(side="left", padx=(0, 6))
            self._tip(b, tip)


    def _load_avatar(self, px: int):
        """把 `tools/assets/avatar.png` 读成能贴到 Tk 上的图片，缩放到 px。

        两条路，按可靠性从高到低：
          1. **Pillow** —— 缩放质量好（它本来是 `fastembed` 的传递依赖，
             现在 gui.py 也直接用，所以 requirements.txt 里显式声明了）；
          2. **Tk 自带的 PhotoImage**（Tk 8.6 原生支持 PNG）—— 不缩放，原样用。

        两条都不行就返回 None，调用方静默跳过。**不要为了显示一张装饰图
        去硬依赖某个包**：面板是排障入口，它自己不能成为新的故障点。
        """
        path = os.path.join(ROOT, "tools", "assets", "avatar.png")
        if not os.path.exists(path):
            return None
        try:
            from PIL import Image, ImageTk                   # noqa: PLC0415
            im = Image.open(path).convert("RGBA")
            im = im.resize((px, px), Image.LANCZOS)
            return ImageTk.PhotoImage(im)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            return tk.PhotoImage(file=path)                   # 不缩放，原样用
        except Exception:                                     # noqa: BLE001
            return None


    def _tip(self, widget, text: str):
        """给控件挂一个悬停提示（Tk 没有内置 tooltip，这里做个轻量的）。

        为什么值得做：用户说"有些功能只看名字不知道是做什么的"，
        除了改名字，最直接的就是让鼠标停上去能看一句话解释。

        ⚠ 字体必须跟界面一致（用户反馈"那个更新索引一排按钮的悬浮提示，
          字的间距看的也很奇怪"）。第一版这里写的是 `font=("", 9)` ——
          空字体名 = 拿 TkDefaultFont 再改字号，而 TkDefaultFont 在中文
          Windows 上不一定是雅黑，结果提示框里的中文字距跟按钮上的不一样，
          并排一看就很怪。现在统一用 self.ui_font。
        """
        tip = {"win": None}

        def show(_e=None):
            if tip["win"] or not text:
                return
            try:
                x = widget.winfo_rootx() + 12
                y = widget.winfo_rooty() + widget.winfo_height() + 4
                w = tk.Toplevel(widget)
                w.wm_overrideredirect(True)
                w.wm_geometry(f"+{x}+{y}")
                tk.Label(w, text=text, background="#ffffe0",
                         relief="solid", borderwidth=1,
                         font=(self.ui_font, 9), padx=8, pady=4,
                         justify="left").pack()
                tip["win"] = w
            except tk.TclError:
                tip["win"] = None

        def hide(_e=None):
            if tip["win"]:
                try:
                    tip["win"].destroy()
                except tk.TclError:
                    pass
                tip["win"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)
        widget.bind("<ButtonPress>", hide)


    def _build_tabs(self):
        # Notebook 占满剩余空间（日志已经 side="bottom" 钉住了，
        # pack 的顺序决定了谁先占位：日志先 pack(side=bottom) 就稳在底部）
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=(6, 0))

        self.tab_struct = ttk.Frame(nb)
        self.tab_exp = ttk.Frame(nb)
        self.tab_ai = ttk.Frame(nb)
        self.tab_env = ttk.Frame(nb)
        self.tab_quality = ttk.Frame(nb)
        self.tab_meta = ttk.Frame(nb)
        self.tab_adv = ttk.Frame(nb)
        nb.add(self.tab_struct, text="  知识库结构  ")
        nb.add(self.tab_exp, text="  经验库  ")
        nb.add(self.tab_ai, text="  分类建议  ")
        nb.add(self.tab_env, text="  运行环境  ")
        nb.add(self.tab_quality, text="  解析健康  ")
        nb.add(self.tab_meta, text="  元数据  ")
        nb.add(self.tab_adv, text="  高级  ")
        self.notebook = nb

        self._build_struct_tab()
        self._build_experience_tab()
        self._build_ai_tab()
        self._build_quality_tab()
        self._build_meta_tab()

        self._build_env_tab()
        self._build_advanced_tab()


    # ================================================================ 元数据页
    #
    # 为什么单独开一页（而不是塞进「切片质量」）：
    #   那一页回答的是"PDF 提取出来的正文可不可信"，这一页回答的是
    #   "Zotero 里的条目信息全不全、类型对不对" —— 两件事的判据、风险、
    #   处置方式都不一样。用户的原话是"在面板里加个一键补全和一键采用高置信"，
    #   他要的是一个**能找到、能一眼看懂**的地方，混在切片质量里等于藏起来。

    def _api(self, method: str, path: str, body=None, timeout: float = 180.0):
        """调本地服务（带 token）。返回 (ok, payload, error)。

        ⚠ token 在**知识库目录**下（知识库跟着 Zotero 数据目录走），
          不是项目目录 —— 面板里其它地方踩过这个。
        """
        import urllib.error
        import urllib.request
        url = "http://127.0.0.1:8765" + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        tok = self._read_token()
        if tok and not tok.startswith("("):
            req.add_header("X-KB-Token", tok)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, json.loads(resp.read().decode("utf-8")), ""
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:  # noqa: BLE001
                pass
            return False, {}, f"HTTP {exc.code} {detail}"
        except Exception as exc:  # noqa: BLE001
            return False, {}, f"{type(exc).__name__}: {exc}"


    def _show_dialog(self, title, body):
        """弹一个可复制的文本框（token、状态这类要能选中的内容）。

        为什么不用 messagebox：它**不能选中文字**，而"复制 token"
        就是要点在这里的。所以自己搭个 Toplevel + 只读 Text。
        """
        try:
            win = tk.Toplevel(self.root)
            win.title(title)
            win.geometry("640x340")
            win.transient(self.root)
            txt = tk.Text(win, wrap="word", font=(self.mono_font, 10))
            txt.pack(fill="both", expand=True, padx=8, pady=(8, 4))
            txt.insert("1.0", body)
            txt.configure(state="normal")
            bar = ttk.Frame(win, padding=(8, 0, 8, 8))
            bar.pack(fill="x")
            ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
            ttk.Button(
                bar, text="复制全部",
                command=lambda: (self.root.clipboard_clear(),
                                 self.root.clipboard_append(body),
                                 self.say(f"[{ts()}] 已复制到剪贴板"))
            ).pack(side="right", padx=6)
            txt.focus_set()
            win.bind("<Escape>", lambda _e: win.destroy())
        except tk.TclError as exc:
            self.say(f"[XX] 弹窗失败：{exc}")


    @staticmethod
    def _read_token() -> str:
        # ⚠ token 在**知识库目录**下，不是项目目录（知识库默认跟着 Zotero 走）
        p = os.path.join(App.kb_dir(), "service-token.txt")
        try:
            return open(p, encoding="utf-8").read().strip() or "(还没有，先启动服务)"
        except OSError:
            return "(还没有，先启动服务)"


    def _build_log(self):
        # 日志区固定高度、不参与拉伸 —— 否则内容区（结构表/经验/模型）会被挤扁。
        # ⚠ 反过来也不行：Notebook 用 fill="both"+expand=True 时，如果日志
        #   也用 expand，窗口不够高时两边互相抢空间，日志会被压成一条黑边
        #   （第一版就是这样，截图里只剩一条线）。
        #   所以：Notebook 独占剩余空间，日志用固定 height 钉在底部。
        frame = ttk.LabelFrame(self.root, text=" 运行日志 ", padding=4)
        frame.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
        self.log = scrolledtext.ScrolledText(frame, height=8, wrap="none",
                                             font=(self.mono_font, 9),
                                             background="#1b1b1b", foreground="#ddd")
        self.log.pack(fill="x")


    # ================================================================ 基础设施

    def say(self, text: str):
        """往日志区追加一行（只会被主线程调用）。"""
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")


    def show(self, widget: scrolledtext.ScrolledText, text: str):
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.see("1.0")


    def run(self, title: str, args: list[str], on_done=None, confirm: str = ""):
        """在子进程里跑一条命令，输出实时进日志。"""
        if self.busy:
            messagebox.showinfo("忙", "还有任务在跑，等它结束或点「停止当前任务」。")
            return
        if confirm and not messagebox.askyesno("确认", confirm):
            return
        self.busy = True
        self.say(f"[{ts()}] ▶ {title}")

        def worker():
            try:
                self.proc = subprocess.Popen(
                    [PY, "-X", "utf8", *args],
                    cwd=ROOT, env=ENV,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                for line in self.proc.stdout:  # type: ignore[union-attr]
                    self.out_queue.put(("log", line.rstrip()))
                code = self.proc.wait()
                self.out_queue.put(("done", (title, code, on_done)))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("log", f"[XX] {type(exc).__name__}: {exc}"))
                self.out_queue.put(("done", (title, -1, on_done)))

        threading.Thread(target=worker, daemon=True).start()


    def stop_task(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.say(f"[{ts()}] ⏹ 已请求停止")
        else:
            self.say(f"[{ts()}] 当前没有在跑的任务")


    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.out_queue.get_nowait()
                if kind == "log":
                    self.say(payload)
                elif kind == "status":
                    # Tkinter 变量只在主线程改（见 refresh_status 的说明）
                    self.status_var.set(payload)
                elif kind == "show":
                    widget, text = payload
                    self.show(widget, text)
                elif kind == "env":
                    self._apply_env(payload)
                elif kind == "papers":
                    self._apply_paper_list(payload)
                elif kind == "dialog":
                    title, body = payload
                    self._show_dialog(title, body)
                elif kind == "done":
                    title, code, callback = payload
                    flag = "✓" if code == 0 else f"✗ 退出码 {code}"
                    self.say(f"[{ts()}] {flag} {title} 结束")
                    self.busy = False
                    self.proc = None
                    if callback:
                        try:
                            callback()
                        except Exception as exc:  # noqa: BLE001
                            self.say(f"[XX] 回调失败：{exc}")
                    self.refresh_status()
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)


    # ================================================================ 运行环境

    def _apply_env(self, info: dict):
        """把探测结果填进运行环境页（主线程调用）。"""
        for key, var in self.env_vars.items():
            p = info.get(key) or ""
            ok = bool(p) and os.path.exists(p)
            var.set(("✓ " if ok else "✗ ") + (p or "（没找到）"))
        hint = []
        if info.get("_server"):
            hint.append("这些值是正在运行的本机服务报的。")
        else:
            hint.append("本机服务没在跑，上面是面板自己探测的结果"
                        "（点「启动本地服务」可以拉起它）。")
        hint.append(f"知识库位置：{info.get('_kb') or self.kb_dir()}")
        hint.append("要改这些位置：Zotero → 编辑 → 设置 → 文献知识库 → 运行环境，"
                    "点「浏览…」选目录/文件；改完回这里点「重新检测」。")
        self.env_note.set("\n".join(hint))


    # ================================================================ 状态

    def refresh_all(self):
        """刷新所有会变的显示：状态、结构表、运行环境。"""
        self.refresh_status()
        try:
            self.refresh_struct()
        except Exception as exc:  # noqa: BLE001
            self.say(f"[XX] 结构表刷新失败：{exc}")
        self.refresh_env()


    def refresh_status(self):
        """读库统计并更新状态栏。

        ⚠ 必须"后台线程读、主线程写"：Tkinter 的变量（StringVar）
        只能在主线程操作，在工作线程里 `status_var.set()` 可能静默不生效
        或直接抛 Tcl 错误（这里踩过一次：状态栏永远停在"正在读取状态…"）。
        所以线程只负责把文本算出来丢进队列，真正的 set 在 _drain_queue 里做。
        """
        self.status_var.set("正在读取状态…")
        # 知识库位置也在这里给 —— 它是"数据在哪"，与运行环境页的
        # "代码在哪"是两件事，所以不放那一页。
        self.kb_path_var.set(self.kb_dir())

        def work():
            try:
                import schemas as S

                conn = S.connect(S.INDEX_DB)
                n_items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
                n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
                n_vec = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
                n_exp = conn.execute("SELECT COUNT(*) AS n FROM experience").fetchone()["n"]
                n_test = conn.execute(
                    "SELECT COUNT(*) AS n FROM experience "
                    "WHERE asked LIKE '%自检%' OR asked LIKE '%验收%'"
                ).fetchone()["n"]
                ft = conn.execute(
                    "SELECT SUM(fulltext_chars) AS s FROM items").fetchone()["s"] or 0
                model = S.get_meta(conn, "embed_model", "(无)")
                conn.close()
                # ⚠ 这里**不再显示"建于 …"**（用户问"这个是什么时间，
                #   如果不必要就去掉"）。那个值是 `meta.built_at` =
                #   **上次跑构建的墙上时间**，两个原因让它不值得占位置：
                #     · 增量构建跑一次就刷新它，哪怕一条都没变 ——
                #       所以它**不能**用来看"索引是不是过时了"；
                #     · 用户真正想知道的是"我要不要点更新"，
                #       而不是"上次什么时候点的"。
                #   想看构建时间/耗时/规模，去 MANIFEST.json（排错用），
                #   面板状态栏只留"库里有什么"。
                text = (f"{n_items} 篇 ｜ 切片 {n_chunks} ｜ 向量 {n_vec} ｜ "
                        f"经验 {n_exp} ｜ 正文 {ft // 10000} 万字 ｜ 模型 {model}")
                if n_test:
                    text += f"  ⚠ 测试残留 {n_test} 条"
                if n_chunks and not n_vec:
                    text += "  ⚠ 向量缺失"
                self.out_queue.put(("status", text))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("status",
                                    f"状态读取失败：{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()


    def open_folder(self):
        """在文件管理器里打开**知识库目录**（不是项目目录）。

        ⚠ 老代码写的是 os.path.join(ROOT, "kb") —— 那是知识库还在项目里
          时代的假设。现在知识库默认跟着 Zotero 数据目录走，必须问
          schemas 要真实位置。
        """
        kb = self.kb_dir()
        if not os.path.isdir(kb):
            messagebox.showerror(
                "目录不存在",
                f"知识库目录不存在：\n\n{kb}\n\n"
                "可能还没建过库（点「更新索引」），或位置配置指错了"
                "（在 Zotero 插件的「知识库位置」里改）。")
            return
        try:
            os.startfile(kb)  # type: ignore[attr-defined]
            self.say(f"[{ts()}] 已打开：{kb}")
        except Exception as exc:  # noqa: BLE001
            self.say(f"[XX] 打开目录失败：{exc}")
