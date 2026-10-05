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
        # 可滚动页签的画布表（滚轮处理器靠它找"鼠标底下该滚哪个容器"）
        self._scroll_hosts: dict[str, tk.Canvas] = {}

        root.title("Zotero 文献知识库 · 管理面板")
        self._set_window_icon(root)
        # 字体要在建控件**之前**定好 —— 否则先建的控件用旧字体，后建的用新字体，
        # 同一页里两种字体混着，又回到"字距看着奇怪"那个问题上。
        self.ui_font, self.mono_font = apply_ui_fonts(root)
        # 分隔线（PanedWindow 的 sash）要**看得见、抓得住** —— vista 主题默认是
        # 5px 的浅灰细条，用户根本不知道那里能拖（原话："内部窗口没办法独立
        # 调节吗？"）。加粗到 8px 之后，它自己就是一道看得见的分界。
        try:
            ttk.Style(root).configure("Sash", sashthickness=8)
        except tk.TclError:
            pass
        # 初始尺寸按屏幕来，别用固定值 —— 屏幕小的时候要能装下，
        # 屏幕大的时候默认就给足空间（实测 2560x1440 下 1000x720 显得局促）。
        # ⚠ 高度原来硬顶 880：在 1440p 屏上白白浪费 150px，而"内容要滚才能看完 /
        #   日志只剩几行"的抱怨有一部分就是这么来的。改成按屏幕 0.72 走（上限 1080）。
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1240, max(900, int(sw * 0.46)))
        h = min(1080, max(620, int(sh * 0.72)))
        x = max(0, (sw - w) // 2)
        y = max(0, int((sh - h) * 0.35))
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(860, 580)

        self._build_header()
        # ⚠ 顺序有讲究：日志区先建好容器（并记住它），页签建完再把它 add 到
        #   分栏的**下面**那一格 —— ttk.PanedWindow 的显示顺序就是 add 的顺序。
        self._build_log()
        self._build_tabs()
        # 滚轮：全局绑一次，按鼠标位置决定滚哪个页签（见 _on_wheel 的说明）
        root.bind_all("<MouseWheel>", self._on_wheel)

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

        # ---- 核心按钮：最常用的，一直可见
        bar = ttk.Frame(self.root, padding=(12, 4, 12, 2))
        bar.pack(fill="x")
        # ⚠ 按钮名要"看一眼知道是干什么的"。
        #   用户反馈：「更新索引（增量）」这个名字本身就很奇怪 ——
        #   "增量"是给写代码的人看的词，用户只关心"我新加了文献，点它更新"。
        #
        # ⚠ 「打开知识库」放在这里（而不是只放在「知识库结构」页）：
        #   它解决的是"我想看某一篇的某一层"这个**日常**动作，就该在第一屏。
        #   与它并列的「文献管理器中查看」是另一种需求（要动 index.db、
        #   看 logs\ 时去目录）。
        # ⚠ 两个名字都是用户 2026-10-05 定的：前者**不要省略号**
        #   （弹出来的是列表，不是"还要再填参数"的对话框），后者原来叫
        #   "打开知识库目录"，用户觉得看不出是"在资源管理器里打开"。
        quick = [
            ("手动更新", self.do_convert_incremental,
             "新加了文献、或改了笔记/标注之后点这个（只处理变了的，很快）"),
            ("全部重建", self.do_convert_full,
             "重新解析所有文献、重算向量。一般不用，除非索引坏了或换了模型"),
            ("环境自检", self.do_check, "检查依赖、索引、向量是否正常"),
            ("备份", self.do_backup, "备份索引（含经验层和权重）"),
            ("打开知识库", self.open_kb_browser,
             "先列文献（看得见作者/年份/标题），选中一篇再选\"要读多深\"的那一层，"
             "双击就打开对应的 md —— 不用去目录里按 22X9PMR6 这种编号找"),
            ("文献管理器中查看", self.open_folder,
             "在文件管理器里打开知识库根目录（要动 index.db、看 logs\\ 时用它）"),
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


    def _bind_sash_cursor(self, paned):
        """鼠标移到分隔线上时把光标换成"上下箭头"。

        为什么值得做：ttk 的 sash **没有任何悬停反馈** —— 用户根本不知道那里
        可以拖（反馈原话："内部窗口没办法独立调节吗？…太不自然了"）。
        这不是 Python/Tk 的限制，只是 ttk 没帮我们把光标管起来，几行就能补上。

        判据：指针 y 落在 sashpos(±6px) 之内。ttk 的 sash 是控件自己画的、
        不是子控件，所以只能按坐标判断（这就是它"不自然"的根源）。
        """
        def on_motion(e):
            try:
                pos = paned.sashpos(0)
            except tk.TclError:
                return
            want = "sb_v_double_arrow" if abs(e.y - pos) <= 6 else ""
            try:
                if paned.cget("cursor") != want:
                    paned.configure(cursor=want)
            except tk.TclError:
                pass

        def on_leave(_e=None):
            try:
                paned.configure(cursor="")
            except tk.TclError:
                pass

        paned.bind("<Motion>", on_motion, add="+")
        paned.bind("<Leave>", on_leave, add="+")


    def _build_tabs(self):
        """建 Notebook 与页签，并把「内容 / 日志」两格 add 进去。

        布局（用户 2026-10-05 的三条反馈都落在这里）：
          · 上面一格 = 各页签。每个页签套一层**可滚动画布** —— 内容比窗口高
            时滚轮就能看完，不必把窗口拉长；
          · 下面一格 = 运行日志。它与上面之间有一条**可拖的分隔线**，
            日志想拉长就拉长（以前固定 8 行，看不全）；
          · 页签内部要"钉在下面"的东西（详情/输出）用 split 的**下面那格**。
        """
        nb = ttk.Notebook(self._paned)
        self.notebook = nb

        # ⚠ `_tab_panes` 的第二个返回值是"页签内部的下面那一格"（可拖），
        #   需要钉底的文本框（详情/输出）就放那里；不需要的页签给 None。
        self.tab_struct, _ = self._tab_panes(nb, "  知识库结构  ")
        self.tab_exp, self.exp_out = self._tab_panes(
            nb, "  经验库  ", split=" 选中那条的详情（拖分隔线调） ")
        self.tab_ai, self.ai_out = self._tab_panes(
            nb, "  分类建议  ", split=" 输出（拖分隔线调） ")
        self.tab_env, _ = self._tab_panes(nb, "  运行环境  ")
        # 「PDF 解析」紧跟「运行环境」：它俩是同一类东西（环境/解析器配置）
        self.tab_parse, _ = self._tab_panes(nb, "  PDF 解析  ")
        self.tab_quality, _ = self._tab_panes(nb, "  损坏查询  ")
        self.tab_meta, _ = self._tab_panes(nb, "  元数据  ")
        self.tab_adv, self.adv_out = self._tab_panes(
            nb, "  高级  ", split=" 输出（拖分隔线调） ")
        self.tab_prompts, self.prompt_out = self._tab_panes(
            nb, "  提示词  ", split=" 试跑输出（拖分隔线调） ")

        # 顺序即上下顺序：先内容，后日志（日志在最下面）
        # ⚠ 权重 3:2：日志初始就能看见十来行（用户嫌 5 行太少），
        #   拉分隔线还能任意改。
        self._paned.add(nb, weight=3)
        self._paned.add(self.log_frame, weight=2)

        self._build_struct_tab()
        self._build_experience_tab()
        self._build_ai_tab()
        self._build_quality_tab()
        self._build_meta_tab()

        self._build_env_tab()
        self._build_parse_tab()
        self._build_advanced_tab()
        self._build_prompts_tab()


    def _tab_panes(self, nb: ttk.Notebook, title: str, split: str = ""):
        """建一个页签，返回 `(内容容器, 底部容器)`。

        内容容器套在**可滚动的画布**里：内容比窗口高时用滚轮看（用户反馈
        "很多东西都要把面板拉长才能看到，应该改成滚轮能滑看"）。
        底部容器在 `split` 非空时才有 —— 页签内部再分成上下两格，
        **中间那条分隔线可以拖**，用来放"想拉长看"的文本框（运行日志、
        输出、详情）。`split` 的值就是那一格的标题。

        ⚠ 画布里**不要**用 `pack(side="bottom")` 钉底 —— 内层容器的高度是按
          内容请求算出来的，钉不住；要钉底就用 split 那一格。
        ⚠ 内层高度取 `max(画布高度, 内容请求高度)`：这样内容少的页签能占满
          整屏（页签里那些 `expand=True` 的表格/文本框照旧会被拉满），
          内容多的才出现滚动条。
        """
        host = ttk.Frame(nb)
        top: tk.Misc = host
        paned = None
        if split:
            paned = ttk.PanedWindow(host, orient="vertical")
            paned.pack(fill="both", expand=True)
            top = ttk.Frame(paned)
            paned.add(top, weight=3)
            self._bind_sash_cursor(paned)

        canvas = tk.Canvas(top, highlightthickness=0, borderwidth=0, height=340)
        try:
            bg = ttk.Style(self.root).lookup("TFrame", "background")
            if bg:
                canvas.configure(background=bg)
        except tk.TclError:
            pass
        vs = ttk.Scrollbar(top, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        state = {"busy": False, "bar": None, "h": None}
        # ⚠ 请求高度的上限：有"下面那一格"的页签要小一些 —— 否则
        #   画布(460) + 输出格(~180) 会让这个页签向 Notebook 请求 ~640px，
        #   而 Notebook 取的是**所有页签里最大的那个请求**，于是底部「运行日志」
        #   被挤到只剩 5 行（实测：日志只拿到 79px，用户报的"只能显示几行"
        #   有一半是这么来的）。实测数字见会话目录里的 probe_tabs_req.py。
        cap = 300 if split else 460

        def sync(_e=None):
            if state["busy"]:
                return
            state["busy"] = True
            try:
                w = max(canvas.winfo_width(), 1)
                h = max(canvas.winfo_height(), inner.winfo_reqheight())
                canvas.itemconfigure(win, width=w, height=h)
                canvas.configure(scrollregion=(0, 0, w, h))
                # 画布的**请求高度跟随内容**（140~cap）：内容少的页签不留一大块空白、
                # 把下面那格挤小；内容多的才出现滚动条。
                # ⚠ 只在值真的变了才 configure —— 否则会自己触发自己的 <Configure>。
                want = max(140, min(inner.winfo_reqheight(), cap))
                if state["h"] != want:
                    state["h"] = want
                    canvas.configure(height=want)
                # 装得下就把滚动条收起来（免得每个页签都挂一条无用的条）
                need = inner.winfo_reqheight() > canvas.winfo_height()
                if state["bar"] is not need:
                    state["bar"] = need
                    if need:
                        vs.pack(side="right", fill="y")
                    else:
                        vs.pack_forget()
            except tk.TclError:
                pass
            finally:
                state["busy"] = False

        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)
        nb.add(host, text=title)
        self._scroll_hosts[str(canvas)] = canvas

        bottom = None
        if paned is not None:
            bottom = ttk.LabelFrame(paned, text=split, padding=4)
            paned.add(bottom, weight=2)
        return inner, bottom


    def _on_wheel(self, event):
        """滚轮：滚动鼠标底下那个页签。

        为什么用**全局绑定**而不是逐个控件绑：Tk 在 Windows 上把
        `<MouseWheel>` 发给**焦点控件**，焦点常常不在画布上 —— 绑在画布上
        经常收不到事件，用户看到的就是"滚不动"。这里按鼠标位置自己找控件，
        再往上找最近的可滚动容器。

        ⚠ 鼠标在"本来就会滚"的控件上（Text / Treeview / Listbox）时**必须
          让给它**，否则画布与控件各滚一次，手感是"滚一格跳两格"。
        """
        if not self._scroll_hosts:
            return
        try:
            node = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:      # noqa: BLE001
            return
        while node is not None:
            try:
                cls = node.winfo_class()
            except Exception:      # noqa: BLE001
                cls = ""
            if cls in ("Text", "Listbox", "Treeview"):
                return                 # 它自己会滚
            canvas = self._scroll_hosts.get(str(node))
            if canvas is not None:
                n = max(1, abs(int(event.delta)) // 120)
                canvas.yview_scroll(-n if event.delta > 0 else n, "units")
                return
            node = getattr(node, "master", None)


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
        """运行日志：放进竖直分栏，**分隔线可以拖**。

        以前是 `side="bottom"` + 固定 `height=8` 的一条，只有八行可看 ——
        用户反馈"运行日志框只能显示几行，应该能拉长显示更多"。现在改成
        PanedWindow 的下面那一格，想看长日志把分隔线往下拖就行。

        ⚠ 这里只建容器、**不 add**：pane 的上下顺序由 add 的顺序决定，
          而内容（Notebook）要在 `_build_tabs` 里才建得出来 —— 所以两个
          add 都放在 `_build_tabs` 末尾，顺序是"先内容、后日志"。
        """
        self._paned = ttk.PanedWindow(self.root, orient="vertical")
        self._paned.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        self._bind_sash_cursor(self._paned)
        # 标题里写明"能拖"：分隔线虽然加粗了，但不说一句用户还是不会去试。
        self.log_frame = ttk.LabelFrame(
            self._paned, text=" 运行日志（拖上面的分隔线调高度） ", padding=4)
        self.log = scrolledtext.ScrolledText(self.log_frame, height=8,
                                             wrap="none",
                                             font=(self.mono_font, 9),
                                             background="#1b1b1b", foreground="#ddd")
        self.log.pack(fill="both", expand=True)


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
                elif kind == "call":
                    # 后台线程把结果交给**主线程**的 Tk 控件时用这个。
                    # 为什么必须走队列：Tk 的控件只能在主线程改，直接在线程里
                    # 改会随机崩/无反应（见 refresh_status 的同一句说明）。
                    # ⚠ 加这个分支之前，别处用 ("call", …) 发消息会**被静默
                    #   忽略**（队列泵只认下面那几种 kind，其余直接丢掉）——
                    #   表现是"点了按钮没反应"，没有任何报错。
                    fn, arg = payload
                    try:
                        fn(arg)
                    except Exception as exc:      # noqa: BLE001
                        self.say(f"[{ts()}] 回调出错：{type(exc).__name__}: {exc}")
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
        """把探测结果填进运行环境页（主线程调用）。

        ⚠ 输入框里只放**裸路径**（不带 "✓/✗" 前缀）：这三个框现在是可编辑、
          可保存的，带前缀会把前缀一起写进配置。存不存在由旁边的小字说明。
        """
        for key, var in self.env_vars.items():
            p = info.get(key) or ""
            ok = bool(p) and os.path.exists(p)
            var.set(p)
            if key in getattr(self, "env_ok", {}):
                self.env_ok[key].set("✓ 存在" if ok else
                                     ("✗ 找不到" if p else "（没填）"))
        hint = []
        if info.get("_server"):
            hint.append("这些值是正在运行的本机服务报的。")
        else:
            hint.append("本机服务没在跑，上面是面板自己探测的结果"
                        "（点「启动本地服务」可以拉起它）。")
        hint.append(f"知识库位置：{info.get('_kb') or self.kb_dir()}")
        # MinerU 是可选组件：只写"✓ 存在"没意义，要写它**能不能干活**
        # （版本/GPU/档位），否则用户没法判断该不该动它。
        if info.get("_mineru"):
            hint.append(f"MinerU：{info['_mineru']}")
        # 模型接入区那行小字要跟着环境一起刷新（它的「程序位置」用的是探测
        # 出来的 Ollama 路径，而那个探测是异步的 —— 不刷新就会显示成空的）。
        try:
            self.refresh_llm()
        except Exception:      # noqa: BLE001
            pass
        # ⚠ 这里**不重复**"要改就去上面那三个框"那一段 —— 页面上方已经写了一遍，
        #   同一句话说两遍反而像没写完（第一版就是，用户看界面很挑这种）。
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
