"""知识库统一管理面板（一个窗口管所有事）。

    python tools/gui.py

为什么做这个：原先要记四个入口（1-convert.cmd / 3-maintain.cmd /
kb_admin.py / judge.py），还得记参数。这里把它们收成一页：
状态一目了然，按钮点一下就干活，日志就在下面。

设计取舍
    · 用 Tkinter：知识库自己的 venv 就带（实测 tkinter 8.6），**零新增依赖**、
      不用装 PyQt/webview。界面朴素但够用。
    · 长任务（建索引、跑模型）一律放**子进程**，不 import 到 GUI 进程里 ——
      否则一次建索引就把界面卡死，而且把索引库连接、numpy、onnxruntime
      全拖进 GUI 进程，退出时容易留下半开连接。
    · 所有输出走队列回主线程渲染，避免 Tk 的跨线程崩溃。
    · 破坏性操作（全量重建、清空经验）都要二次确认。

界面的组织原则（2026-10 大修）
    · **按"你想干什么"分，不按"代码怎么分层"分**。旧版 4 个标签页、40 个按钮、
      6 类不相干的东西挤在一页，用户看着名字不知道是做什么的。
    · 第一屏只放**最常用的**：状态卡片 + 五个核心按钮。
    · 每个文件夹是干什么的 → 单独一页「知识库结构」讲清楚（用户明确要的）。
    · 冷门/危险/一次性的操作 → 收进「高级」页，功能一个不删，只是不挡路。
    · 路径一律来自 schemas 的解析（可被用户在插件设置里覆盖），
      **不再有 `ROOT + "\\kb"` 这种假设** —— 知识库默认不跟代码放一起了。
"""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# ⚠ 两个目录都要加：
#   offline\ —— schemas / convert / judge 等
#   online\  —— searcher / query（面板里查经验、列权重都要用）
# 本机踩过：只加了 offline，结果点「列出全部经验」报
#   ModuleNotFoundError: No module named 'searcher'
# 因为 searcher.py 在 online\ 下。凡是 import 这两个目录里的模块，
# 都得先确保这里都加上了。
for _sub in ("offline", "online"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
# 有些模块（如 searcher）内部按 "from schemas import ..." 这种平铺方式导入，
# 而 schemas 在 offline\ 下 —— 上面已经加了，这里不用额外处理。

VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable
ENV = {
    **os.environ,
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "HF_ENDPOINT": "https://hf-mirror.com",
    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
}
# Windows 下别弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------- 字体
#
# 为什么必须显式设字体（用户反馈"有的字看不清"、"字的间距看着很奇怪"）：
#   Tk 不指定字体时，不同控件会各用各的默认字体 —— Label 用 TkDefaultFont、
#   表格用 TkTextFont、等宽处用 TkFixedFont。混着来的后果是**同一个小标签
#   在雅黑/宋体/Arial 之间跳**，同一行里字号和字距都不一样，看着就"奇怪"。
#   更糟的是用 `("", 9, "bold")` 这种写法：空字体名 → 拿 TkDefaultFont
#   再强行加粗，而 TkDefaultFont 在中文 Windows 上不一定是雅黑，
#   加粗后中文字距会明显挤在一起（用户说的"应该是加粗的问题"）。
#
# 所以：先挑一个系统里**真的有**的中文字体，再把所有命名字体统一改掉。
UI_FAMILY = "TkDefaultFont"          # 兜底：保持 Tk 原样
UI_MONO = "TkFixedFont"


def pick_ui_font(root: tk.Misc) -> tuple[str, str]:
    """选一个存在的中文字体，返回 (界面字体, 等宽字体)。

    优先 微软雅黑 —— 它是 Windows 上小字号中文最清楚的（带 hinting），
    宋体在小字号下发虚、黑体字重过重。取不到就退回 Tk 默认值。
    """
    try:
        from tkinter import font as tkfont
        have = {f.lower() for f in tkfont.families(root)}
    except tk.TclError:
        return UI_FAMILY, UI_MONO
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                 "Noto Sans SC", "Source Han Sans SC", "SimHei", "SimSun"):
        if name.lower() in have:
            ui = name
            break
    else:
        ui = UI_FAMILY
    for name in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"):
        if name.lower() in have:
            mono = name
            break
    else:
        mono = UI_MONO
    return ui, mono


def apply_ui_fonts(root: tk.Tk) -> tuple[str, str]:
    """把所有 Tk 命名字体统一成选定的界面字体。返回 (ui, mono) 供后续用。

    ⚠ 只改**命名字体**（TkDefaultFont / TkTextFont / ...），不改字体大小 ——
      大小由下面的 `tk scaling` 统一缩放，两个一起改会把字号乘两次。
    """
    ui, mono = pick_ui_font(root)
    try:
        from tkinter import font as tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont", "TkTooltipFont", "TkIconFont"):
            try:
                tkfont.nametofont(name).configure(family=ui)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkFixedFont").configure(family=mono)
        except tk.TclError:
            pass
    except ImportError:
        pass
    # ttk 控件（Button/Label/Entry/Treeview）不受命名字体影响，
    # 得单独配 Style —— 本机实测：不配的话按钮还是老面板字体。
    try:
        style = ttk.Style(root)
        for cls in ("TButton", "TLabel", "TEntry", "TCheckbutton",
                    "TRadiobutton", "TLabelframe.Label", "TNotebook.Tab",
                    "TCombobox"):
            style.configure(cls, font=(ui, 10))
        style.configure("Treeview", font=(ui, 10), rowheight=24)
        style.configure("Treeview.Heading", font=(ui, 10, "bold"))
    except tk.TclError:
        pass
    return ui, mono


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ================================================================ 知识库目录里的文件说明
#
# 每一项：(名称, 说明, 计算方式, 能删吗)
#   计算方式：db = 只算主库单文件；dir = 递归数文件；file = 单文件；
#             glob = 一类文件合并成一行（多份、同样大，逐行列反而看不清）
#
# ⚠ 放在**模块级**而不是函数里：这样测试能直接断言
#   "目录里每个文件都有说明"（见 tests/test_gui_smoke.py）。
#   以前它藏在 refresh_struct 里，新出现的文件只能显示成
#   "（未在说明表里的文件，可能是新版本新增的）"—— 用户看到的就是这个。
#
# ⚠ 说明里**不要用 markdown 的 `**加粗**`** —— 这是 Tk 表格，
#   星号会原样显示出来（第一版就犯了，界面上出现了 "**唯一不能丢的文件**"）。
#   要强调就用中文书名号或括号。（插入前还会统一剥一遍，双保险。）
KB_FILE_SPEC = [
    ("index.db", "整个索引：条目、切片、全文检索表、向量、经验、权重。"
                 "（唯一不能丢的文件）", "db", "✗ 绝对不能删，先备份"),
    # WAL 边车文件：服务一跑就会出现，用户看到目录里多出两个陌生文件会问
    # "这俩是什么、能不能删"。用 glob 合并成一行（两个文件是一回事），
    # 体积也由这一行负责 —— index.db 那一行只算主库，免得「合计」算两遍。
    ("index.db-*", "SQLite 的 WAL 边车文件：-wal 是还没并回主库的新写入，"
                   "-shm 是它的共享内存索引。只在服务运行时出现，"
                   "停掉服务后一般会自己消失", "glob",
     "✗ 别单独删（跟 index.db 是一体的）"),
    ("INDEX.md", "文献清单（给人看的）：全部文献按年份分组的表格，"
                 "一眼认出哪篇是哪篇。AI 检索不读它", "file",
     "○ 可删，「更新索引」会自动重建"),
    ("papers\\", "每篇文献一份档案：元数据 + 摘要 + 笔记 + 标注 + 正文首段。"
                 "按 Zotero key 命名（如 22X9PMR6.md）", "dir",
     "○ 可删，重新索引会再生成"),
    ("fulltext\\", "每篇的正文，带「## p.N」页码锚点。"
                   "AI 按页读正文用的就是它", "dir",
     "○ 可删，重新索引会再生成"),
    ("inbox\\", "待确认的经验、分类建议、以及「扫到哪了」的进度记录",
     "dir", "△ 看看再删（可能有没确认的经验）"),
    ("logs\\", "运行日志：服务日志、面板报错日志", "dir", "✓ 随便删"),
    (".cache\\", "嵌入模型的临时缓存与下载内容，可重新下载", "dir",
     "✓ 可删（下次要重新下载模型）"),
    ("MANIFEST.json", "上次构建的统计与警告（哪篇没抓到全文之类）",
     "file", "✓ 可删，下次构建重写"),
    ("llm-config.json", "模型配置（provider / 模型名 / API Key）。"
                        "含密钥，别分享出去", "file", "△ 删了要重新配模型"),
    ("service-token.txt", "本机服务与 Zotero 插件之间的通行证"
                          "（自动生成，跟模型 key 无关）", "file",
     "△ 删了插件要重新握手"),
    ("bridge-token.txt", "知识库与 DSH 之间的通行证（同上，自动生成）",
     "file", "△ 同上"),
    ("zotero-api-key.txt", "连 Zotero 本地 API 用的授权码"
                           "（分类重整时申请的那次）", "file",
     "△ 删了要重新授权"),
    ("pending-suggestions.json", "分类建议的待确认队列", "file",
     "△ 删了建议就没了"),
    ("taxonomy-plan.json", "上次分类重整的干跑计划", "file", "✓ 可删"),
    ("watch-baseline.json", "「新文献监听」的比对基线", "file",
     "✓ 可删（下次重新建立基线）"),
    ("plugin-status.json", "Zotero 插件写出来的运行状态（排错用）",
     "file", "✓ 可删"),
    ("profile-backup\\", "Zotero profile 的配置备份（升级前检查时生成）",
     "dir", "✓ 确认升级没问题后可删"),
    ("backups\\", "索引备份（含经验层）。备份命令的输出", "dir",
     "△ 保留最近几个"),
    # 历史备份：各工具在动索引之前自动留的（改页眉过滤 / 体检 / 图注抽取…）。
    # ⚠ 合并成一行而不是逐个列：每份 40 MB 上下，五行一模一样的 43.8 MB
    #   反而看不出"它才是占地方的那个"。
    ("index.db.bak*", "索引的历史备份：各工具在改动索引前自动留的底"
                      "（改页眉过滤、解析体检、图注抽取…）。"
                      "确认新索引没问题后就能删旧的", "glob",
     "△ 占地方，确认没问题后删旧的"),
    ("settings-init.log", "Zotero 设置面板的初始化诊断日志"
                          "（面板打不开、配置存不进去时看它）", "file",
     "✓ 可删"),
]


class App:
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

    # ---------------------------------------------------------- 知识库结构

    def _build_struct_tab(self):
        """用户明确要的「知识库结构表」：每个文件夹是干什么的。

        为什么要单独一页：知识库点开是一堆目录（papers/fulltext/inbox/
        .cache/logs + 若干 json），光看名字猜不出用途；而且这些目录
        **有的能删、有的删了就丢经验**，不讲清用户不敢动。
        """
        f = self.tab_struct
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="知识库的位置与内容",
                  font=(self.ui_font, 11, "bold")).pack(side="left")
        ttk.Button(head, text="打开知识库目录", command=self.open_folder).pack(
            side="right")
        ttk.Button(head, text="刷新", command=self.refresh_struct).pack(
            side="right", padx=6)

        # 用 Treeview 做表：能对齐、能排序、能选中
        cols = ("name", "what", "count", "size", "safe")
        wrap = ttk.Frame(f, padding=(10, 6, 10, 4))
        wrap.pack(fill="both", expand=True)
        self.struct_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                        height=13)
        for key, text, width in (
            ("name", "文件 / 文件夹", 150),
            ("what", "存的是什么", 380),
            ("count", "内容量", 110),
            ("size", "占用", 80),
            ("safe", "能删吗", 190),
        ):
            self.struct_tree.heading(key, text=text)
            self.struct_tree.column(key, width=width,
                                    anchor="w" if key in ("name", "what", "safe")
                                    else "e")
        vs = ttk.Scrollbar(wrap, orient="vertical",
                           command=self.struct_tree.yview)
        self.struct_tree.configure(yscrollcommand=vs.set)
        self.struct_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.struct_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.struct_note, foreground="#666",
                  wraplength=900, justify="left").pack(
            fill="x", padx=12, pady=(0, 8))

        self.refresh_struct()

    def refresh_struct(self):
        """扫描知识库目录，填结构表。"""
        import time as _t

        def sizeof(path: str) -> int:
            total = 0
            for dirpath, _dirs, files in os.walk(path):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            return total

        def human(n: int) -> str:
            if n >= 1024 * 1024:
                return f"{n / 1024 / 1024:.1f} MB"
            if n >= 1024:
                return f"{n / 1024:.0f} KB"
            return f"{n} B"

        kb = self.kb_dir()
        SPEC = KB_FILE_SPEC

        try:
            self.struct_tree.delete(*self.struct_tree.get_children())
        except tk.TclError:
            return

        used = 0
        # Treeview 不渲染 markdown：`**加粗**` 会原样显示成带星号的怪样子。
        # 这个坑犯了两次（第一版是 index.db 的说明，第二版是 INDEX.md），
        # 所以改成**在插入前统一剥掉**，以后写说明时不用再记这条。
        def plain(s: str) -> str:
            return s.replace("**", "")

        for name, what, kind, safe in SPEC:
            what, safe = plain(what), plain(safe)
            p = os.path.join(kb, name.rstrip("\\/"))
            if kind == "dir":
                if not os.path.isdir(p):
                    continue
                n = sum(len(fs) for _dp, _dn, fs in os.walk(p))
                size = sizeof(p)
                count = f"{n} 个文件"
            elif kind == "db":
                if not os.path.exists(p):
                    continue
                # 只算主库：-wal / -shm 由下面 `index.db-*` 那一行单独列，
                # 这里再算一遍会让「合计」重复计（曾按"一起算"写过，改了行数才分开）。
                size = os.path.getsize(p)
                count = "见上面状态栏"
            elif kind == "glob":
                # 一类文件合并成一行（历史备份这种：多份、同样大、逐行列反而看不清）
                import glob as _glob
                hits = [x for x in _glob.glob(p) if os.path.isfile(x)]
                if not hits:
                    continue
                size = sum(os.path.getsize(x) for x in hits)
                count = f"{len(hits)} 个"
            else:
                if not os.path.isfile(p):
                    continue
                size = os.path.getsize(p)
                count = f"{human(size)}"
            used += size
            self.struct_tree.insert("", "end", values=(
                name, what, count, human(size), safe))

        # 不在清单里的文件也要显示 —— 否则用户看到目录里有东西、表里没有会疑惑
        try:
            # ⚠ glob 类条目（如 index.db.bak*）不是具体文件名，不能进 known 集合，
            #   否则它会"占着名字"却匹配不上任何文件，而那些文件又会在下面
            #   被当成"未在说明表里"**重复列一遍**。
            known = {n.rstrip("\\/").lower() for n, _w, _k, _s in SPEC
                     if _k != "glob"}
            patterns = [n.lower() for n, _w, k, _s in SPEC if k == "glob"]
            extras = [n for n in os.listdir(kb)
                      if n.lower() not in known
                      and not any(fnmatch.fnmatch(n.lower(), g) for g in patterns)]
            for n in sorted(extras):
                p = os.path.join(kb, n)
                if os.path.isdir(p):
                    size = sizeof(p)
                    cnt = f"{sum(len(fs) for _dp, _dn, fs in os.walk(p))} 个文件"
                else:
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        size = 0
                    cnt = human(size)
                used += size
                self.struct_tree.insert("", "end", values=(
                    n + ("\\" if os.path.isdir(p) else ""),
                    "（未在说明表里的文件，可能是新版本新增的）",
                    cnt, human(size), "? 先别删"))
        except OSError:
            pass

        self.struct_note.set(
            f"知识库根目录：{kb}    ·    合计 {human(used)}\n"
            "说明：papers/ 与 fulltext/ 是可重建的派生物，删了跑一次"
            "「手动更新」就回来；index.db 里的经验层和权重是攒出来的，"
            "重建索引不会恢复它们 —— 所以定期点「备份」。\n"
            "⚠ 上面三个 .txt（service-token / bridge-token / zotero-api-key）"
            "是本机程序之间互相认身份的通行证，全部自动生成、"
            "跟用哪个模型无关 —— 用 Ollama 不需要任何模型 key，"
            "这三个文件也一直会在。模型那边的 key（如果用 API）"
            "存在 llm-config.json 里。")

    # ---------------------------------------------------------- 运行环境


    # ================================================================ 切片质量页

    def _build_quality_tab(self):
        """切片质量页：查坏切片、修坏切片、看图注表格统计。

        为什么值得单独一页：PDF 提取失败**从界面上看不出来** ——
        检索命中了、片段也显示了，但内容是
        `HVHDUFK DQG 7HVW`（其实是 "Research and Test" 每个字母 -3）
        或 `ऍնླbັຩቔູ`（混进藏文码位）这种**整段乱码**。
        规则看不出来，得让模型判（实测单条 9/9 全对）。
        """
        f = self.tab_quality
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="解析健康",
                  font=(self.ui_font, 11, "bold")).pack(side="left")
        ttk.Button(head, text="刷新统计",
                   command=self.refresh_quality).pack(side="right")

        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=900,
            text=("检查 PDF 提取失败的切片（字符错乱 / 编码错乱 / 只剩符号）。"
                  "这类切片**能检索到但读不出东西**，还会污染向量。\n"
                  "检查用本机模型批量判定，全库约几分钟；"
                  "修复会绕开 Zotero 的文本缓存、直接用 PyMuPDF 重新提取。")
        ).pack(fill="x", padx=12, pady=(6, 8))

        # ---- 统计
        self.q_stats = tk.StringVar(value="正在读取…")
        box = ttk.LabelFrame(f, text=" 现状 ", padding=(10, 6))
        box.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(box, textvariable=self.q_stats, justify="left",
                  font=(self.mono_font, 9)).pack(anchor="w")

        # ---- 操作
        btns = ttk.Frame(f, padding=(10, 0))
        btns.pack(fill="x")
        self.q_check_btn = ttk.Button(
            btns, text="检查解析健康", command=self.do_quality_check)
        self.q_check_btn.pack(side="left")
        self.q_rule_btn = ttk.Button(
            btns, text="只跑规则（秒级）", command=self.do_quality_check_rule)
        self.q_rule_btn.pack(side="left", padx=6)
        self.q_fix_btn = ttk.Button(
            btns, text="重建不可信的", command=self.do_quality_repair)
        self.q_fix_btn.pack(side="left", padx=6)
        self.q_chain_btn = ttk.Button(
            btns, text="全库重建后体检",
            command=self.do_quality_rebuild_repair)
        self.q_chain_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="看某篇明细",
                   command=self.do_quality_detail).pack(side="left", padx=6)

        # ---- 图表提取开关
        #
        # 放这一页而不是设置面板：它和"切片质量"是同一件事的两面
        # （都在回答"这篇的正文可不可用"），摆一起用户才看得懂取舍。
        opt = ttk.LabelFrame(f, text=" 构建选项 ", padding=(10, 4))
        opt.pack(fill="x", padx=10, pady=(8, 0))
        self.q_figures_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt, text="构建时提取图注与表格（要读 PDF，约多 1 分钟）",
            variable=self.q_figures_var,
            command=self._save_figures_pref).pack(anchor="w")
        ttk.Label(
            opt, foreground="#888", font=(self.ui_font, 8), justify="left",
            text="关掉之后知识库里就没有「图1.1 神经网络模型」这类信息，"
                 "kb_figures 也就查不到东西。"
        ).pack(anchor="w", padx=(20, 0))

        # ---- 结果表
        mid = ttk.Frame(f, padding=(10, 8, 10, 0))
        mid.pack(fill="both", expand=True)
        cols = ("key", "n", "verdict", "title")
        self.q_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                   height=10)
        for c, txt, w in (("key", "条目", 90), ("n", "字数", 60),
                          ("verdict", "结论", 60), ("title", "标题", 500)):
            self.q_tree.heading(c, text=txt)
            self.q_tree.column(c, width=w,
                               anchor="w" if c not in ("n", "verdict")
                               else "center")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.q_tree.yview)
        self.q_tree.configure(yscrollcommand=vs.set)
        self.q_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.q_tree.bind("<Double-1>", lambda e: self.do_quality_detail())

        self._load_figures_pref()

        self.q_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.q_note, foreground="#666",
                  font=(self.ui_font, 9), justify="left", wraplength=900
                  ).pack(fill="x", padx=12, pady=(4, 8))

    def _quality_offline_dir(self):
        """离线模块目录（check_chunks / repair_chunks 都在那里）。

        ⚠ 统一走这一个方法，别在每处现算路径 ——
          第一版在 `refresh_quality` 里写了两行拼路径的 `sys.path.insert`，
          其中一行还是个空操作（拼出来的结果没用上）。集中一处最不容易错。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "offline")

    def refresh_quality(self):
        """读一次统计：切片/图表数 + 坏切片按篇聚合。"""
        offline = self._quality_offline_dir()

        def work():
            out = {"lines": [], "rows": [], "note": ""}
            try:
                sys.path.insert(0, offline)
                import sqlite3
                import schemas as S
                conn = sqlite3.connect(S.INDEX_DB)
                conn.row_factory = sqlite3.Row
                n_chunk = conn.execute(
                    "SELECT COUNT(*) FROM chunks").fetchone()[0]
                n_item = conn.execute(
                    "SELECT COUNT(*) FROM items").fetchone()[0]
                try:
                    n_fig = conn.execute(
                        "SELECT COUNT(*) FROM figures WHERE kind LIKE "
                        "'caption%'").fetchone()[0]
                    n_tab = conn.execute(
                        "SELECT COUNT(*) FROM figures WHERE kind='table'"
                    ).fetchone()[0]
                except Exception:
                    n_fig = n_tab = 0
                checked = bad = unsure = 0
                try:
                    checked = conn.execute(
                        "SELECT COUNT(*) FROM item_health").fetchone()[0]
                    bad = conn.execute(
                        "SELECT COUNT(*) FROM item_health WHERE "
                        "verdict='bad'").fetchone()[0]
                    unsure = conn.execute(
                        "SELECT COUNT(*) FROM item_health WHERE "
                        "verdict='unsure'").fetchone()[0]
                    rows = conn.execute(
                        "SELECT h.item_key k, h.verdict v, h.n_chars n, "
                        "COALESCE(i.title,'') t FROM item_health h "
                        "LEFT JOIN items i ON i.key=h.item_key "
                        "WHERE h.verdict <> 'ok' "
                        "ORDER BY h.verdict DESC, h.item_key LIMIT 200"
                    ).fetchall()
                    out["rows"] = [(r["k"], r["n"] or 0, r["v"], r["t"][:80])
                                   for r in rows]
                except Exception:
                    pass
                conn.close()
                out["lines"] = [
                    f"{n_item} 篇文献｜{n_chunk} 个切片｜"
                    f"图注 {n_fig}｜表格 {n_tab}",
                    (f"已体检 {checked} 篇：不可信 {bad} 篇"
                     + (f"，待人工确认 {unsure} 篇" if unsure else "")
                     if checked else
                     "还没体检过 —— 点「检查解析健康」跑一次"),
                ]
                if checked and not bad and not unsure:
                    out["note"] = "✓ 所有文献的正文提取都可信"
            except Exception as exc:  # noqa: BLE001
                out["lines"] = [f"读取失败：{exc}"]
            def apply():
                self.q_stats.set("\n".join(out["lines"]))
                for i in self.q_tree.get_children():
                    self.q_tree.delete(i)
                for k, n, v, t in out["rows"]:
                    self.q_tree.insert("", "end", values=(k, n, v, t))
                if out["note"]:
                    self.q_note.set(out["note"])
            self.root.after(0, apply)
        threading.Thread(target=work, daemon=True, name="kb-quality").start()

    def _quality_busy(self, busy, which="check"):
        def apply():
            st = "disabled" if busy else "normal"
            self.q_check_btn.configure(state=st)
            self.q_rule_btn.configure(state=st)
            self.q_fix_btn.configure(state=st)
            self.q_chain_btn.configure(state=st)
        self.root.after(0, apply)

    def _run_quality_check(self, use_model=True):
        offline = self._quality_offline_dir()
        self._quality_busy(True)

        def work():
            sys.path.insert(0, offline)
            try:
                import check_chunks as CC
            except Exception as exc:  # noqa: BLE001
                self.say(f"[切片检查] 载入失败：{exc}")
                self._quality_busy(False)
                return
            self.say(f"[解析体检] 开始"
                     f"（{'规则 + 本地模型' if use_model else '仅规则'}）…")

            def prog(done, total, note):
                if done % 20 == 0 or done == total:
                    self.say(f"[解析体检] {done}/{total}  {note[:60]}")
            r = CC.scan(use_model=use_model, progress=prog)
            self.say(f"[解析体检] 完成：{r['total']} 篇，"
                     f"不可信 {r['bad']} 篇，待人工确认 {r.get('unsure', 0)} 篇，"
                     f"耗时 {r['elapsed']} 秒")
            if r.get("legacy_dropped"):
                self.say(f"[解析体检] 已清理旧的 chunk_quality 表"
                         f"（{r['legacy_dropped']} 行误判数据）")
            if r.get("model_note"):
                self.say(f"[解析体检] ⚠ {r['model_note']}")
            self._quality_busy(False)
            self.refresh_quality()
        threading.Thread(target=work, daemon=True, name="kb-check").start()

    def do_quality_check(self):
        self._run_quality_check(True)

    def do_quality_check_rule(self):
        self._run_quality_check(False)

    def do_quality_repair(self):
        """修复坏切片。**先预览再动手** —— 让用户知道会改哪几篇。"""
        offline = self._quality_offline_dir()
        self._quality_busy(True)
        self.say("[切片修复] 先看哪些能修…")

        def work():
            sys.path.insert(0, offline)
            try:
                import repair_chunks as RC
            except Exception as exc:  # noqa: BLE001
                self.say(f"[切片修复] 载入失败：{exc}")
                self._quality_busy(False)
                return
            plan = RC.inspect()
            ok = [x for x in plan["items"] if x.get("fixable")]
            no = [x for x in plan["items"] if not x.get("fixable")]
            self.say(f"[逐篇重建] 共 {plan['total']} 篇提取不可信："
                     f"{len(ok)} 篇可重建，{len(no)} 篇不行")
            for x in no[:6]:
                self.say(f"           ✗ {x['key']} {x.get('why', '')[:52]}")
            if not ok:
                self.say("[逐篇重建] 没有可重建的（多是扫描件，需要 OCR）")
                self._quality_busy(False)
                return
            self.say(f"[逐篇重建] 开始重建 {len(ok)} 篇…")

            def prog(done, total, note):
                self.say(f"[逐篇重建] {done}/{total}  {note[:60]}")
            r = RC.repair(progress=prog)
            if not r.get("ok"):
                self.say(f"[逐篇重建] ✗ {r.get('error')}")
            else:
                self.say(f"[逐篇重建] 完成：成功 {r['fixed']} 篇，"
                         f"仍不可信 {r['failed']}，跳过 {r['skipped']}，"
                         f"耗时 {r['elapsed']} 秒")
                if r.get("bad_before") is not None:
                    self.say(f"[逐篇重建] 不可信文献 {r['bad_before']} → "
                             f"{r['bad_after']}")
                if r.get("note"):
                    self.say(f"[逐篇重建] {r['note']}")
                for d in r.get("details", [])[:12]:
                    mark = "✓" if d.get("ok") else "✗"
                    self.say(f"           {mark} {d['key']} "
                             f"{d.get('src', '')} {d.get('why', '')[:40]}")
            self._quality_busy(False)
            self.refresh_quality()
        threading.Thread(target=work, daemon=True, name="kb-repair").start()


    # ---------------------------------------------------------- 构建选项

    def _load_figures_pref(self):
        """读 kb-location.json 的 figures 字段（默认开）。"""
        try:
            offline = self._quality_offline_dir()
            sys.path.insert(0, offline)
            import schemas as S
            self.q_figures_var.set(S.figures_enabled())
        except Exception:  # noqa: BLE001
            self.q_figures_var.set(True)

    def _save_figures_pref(self):
        """写进 kb-location.json —— 和运行环境、知识库位置放在同一份配置里。

        ⚠ 用 `S.write_location_config(figures=...)` 而不是自己拼 JSON：
          那个函数知道位置优先级（项目根 > 用户级）与写回格式，
          手写容易写到另一份上去。
        """
        want = bool(self.q_figures_var.get())
        try:
            offline = self._quality_offline_dir()
            sys.path.insert(0, offline)
            import schemas as S
            p = S.write_location_config(figures=want)
            self.say(f"[构建选项] 图注与表格提取已{'开启' if want else '关闭'}"
                     f"（写入 {os.path.basename(p)}）")
        except Exception as exc:  # noqa: BLE001
            self.say(f"[构建选项] 保存失败：{exc}")

    def do_quality_rebuild_repair(self):
        """重建 → 再修复，一条龙。

        为什么必须这个顺序（本机踩过）：
          `convert.py` 的正文默认**优先读 Zotero 的 .zotero-ft-cache**，
          而缓存里恰恰是那些乱码。所以"先修复、后重建"会让修复成果
          被原样覆盖回去。做成一个按钮，用户不用记这条约束。
        """
        offline = self._quality_offline_dir()
        self._quality_busy(True)
        self.say("[重建+修复] 开始 —— 先全量重建，再修复坏切片")

        def work():
            sys.path.insert(0, offline)
            import subprocess
            py = sys.executable
            root = os.path.dirname(offline)
            # ---- 第一步：重建
            self.say("[重建+修复] ① 全量重建中…（约 2~3 分钟）")
            try:
                r = subprocess.run(
                    [py, "-X", "utf8", os.path.join(offline, "convert.py"),
                     "--full"],
                    cwd=root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=3600)
                tail = (r.stdout or "").strip().split("\n")[-6:]
                for ln in tail:
                    if ln.strip():
                        self.say(f"           {ln.strip()[:88]}")
                if r.returncode != 0:
                    self.say(f"[重建+修复] ① 失败（退出码 {r.returncode}）")
                    self.say(f"           {(r.stderr or '')[-200:]}")
                    self._quality_busy(False)
                    return
            except Exception as exc:  # noqa: BLE001
                self.say(f"[重建+修复] ① 出错：{exc}")
                self._quality_busy(False)
                return

            # ---- 第二步：逐篇重建（只重建体检判不可信的）
            self.say("[全库重建] ② 逐篇重建不可信的文献…")
            try:
                import repair_chunks as RC
            except Exception as exc:  # noqa: BLE001
                self.say(f"[全库重建] 载入 repair_chunks 失败：{exc}")
                self._quality_busy(False)
                return
            plan = RC.inspect()
            if not plan.get("fixable"):
                self.say("[全库重建] ② 没有可重建的（剩余的多是扫描件）")
            else:
                def prog(done, total, note):
                    self.say(f"           {done}/{total}  {note[:60]}")
                rr = RC.repair(progress=prog)
                if rr.get("ok"):
                    self.say(f"[全库重建] ② 完成：成功 {rr['fixed']} 篇，"
                             f"不可信 {rr.get('bad_before')} → "
                             f"{rr.get('bad_after')}")
                    if rr.get("note"):
                        self.say(f"[全库重建] {rr['note']}")
                else:
                    self.say(f"[全库重建] ② 失败：{rr.get('error')}")

            # ---- 收尾：重新查一遍，把结果刷新到界面
            self.say("[全库重建] 全部完成。")
            self._quality_busy(False)
            self.refresh_quality()
        threading.Thread(target=work, daemon=True,
                         name="kb-rebuild-repair").start()

    def do_quality_detail(self):
        """看选中那篇的体检明细（弹窗，可复制）。"""
        sel = self.q_tree.selection()
        if not sel:
            self.q_note.set("先在上面选一篇（双击也行）")
            return
        key = self.q_tree.item(sel[0], "values")[0]
        offline = self._quality_offline_dir()

        def work():
            sys.path.insert(0, offline)
            try:
                import repair_chunks as RC
                d = RC.detail(key)
            except Exception as exc:  # noqa: BLE001
                self._show_dialog("出错", f"{exc}")
                return
            if not d.get("ok"):
                self._show_dialog("出错", str(d.get("error")))
                return
            lines = [f"{key}  {d.get('title', '')[:60]}",
                     f"结论：{d.get('verdict')}（{d.get('checker')}）"
                     f"  {d.get('n_chars', 0)} 字"
                     f"  正文来源：{d.get('fulltext_src') or '无'}"
                     f" / {d.get('fulltext_chars', 0)} 字", ""]
            for x in (d.get("reasons") or []):
                lines.append(f"  · {x}")
            for i, s in enumerate(d.get("samples") or []):
                lines.append("")
                lines.append(f"--- 片段 {i + 1} ---")
                lines.append(s[:300])
            self._show_dialog(f"解析体检明细 · {key}", "\n".join(lines))
        threading.Thread(target=work, daemon=True, name="kb-detail").start()

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

    def _build_meta_tab(self):
        """元数据页：一键补全（只读扫描）+ 一键采用高置信 + 类型/标题污染检查。"""
        f = self.tab_meta
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="元数据补全", font=(self.ui_font, 11, "bold")).pack(
            side="left")
        # ⚠ 这一页**不放**「刷新统计」这类按钮：它属于「切片质量」页，
        #   点这里的结果会写到那一页去 —— 本项目在"运行环境页点服务状态、
        #   结果显示在高级页"上踩过一次，规矩是**状态输出的去处要和点击处一致**。

        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=920,
            text=("「一键补全」= 对全库缺字段的文献，从它们的 PDF 首页里找"
                  "日期 / DOI / 卷 / 期 / 页码 / 作者，列成一张表。\n"
                  "这一动作「只读」：不写 Zotero、不写索引，秒级完成（纯规则）。"
                  "结果表里「高置信」是能从固定格式里抽出来的（规则），"
                  "「低置信」是本地小模型给的（会编，要你自己核对）。")
        ).pack(fill="x", padx=12, pady=(6, 8))

        # ---- 操作
        btns = ttk.Frame(f, padding=(10, 0))
        btns.pack(fill="x")
        self.meta_scan_btn = ttk.Button(
            btns, text="一键补全（纯规则·秒级）", command=self.do_meta_scan)
        self.meta_scan_btn.pack(side="left")
        self.meta_model_btn = ttk.Button(
            btns, text="用本地模型补全（慢）", command=self.do_meta_scan_model)
        self.meta_model_btn.pack(side="left", padx=6)
        self.meta_cancel_btn = ttk.Button(
            btns, text="取消", command=self.do_meta_cancel)
        self.meta_cancel_btn.pack(side="left", padx=6)
        self.meta_cancel_btn.configure(state="disabled")   # 没在跑时点不了

        btns2 = ttk.Frame(f, padding=(10, 6, 10, 0))
        btns2.pack(fill="x")
        self.meta_typefix_btn = ttk.Button(
            btns2, text="检查类型/标题污染", command=self.do_typefix_scan)
        self.meta_typefix_btn.pack(side="left")
        ttk.Button(btns2, text="看某篇的建议明细",
                   command=self.do_meta_detail).pack(side="left", padx=6)

        # ⚠ 这一页**故意没有"写回 Zotero"的按钮** —— 这是用户拍板的形态：
        #   写库统一留在 Zotero 插件里（插件在 Zotero 进程内，能直接调
        #   `item.setField + saveTx`；面板是另一个 Python 进程，写不了库，
        #   绕任务队列那条路要新增任务类型、处理领取/回执/超时，成本高、
        #   容易做成半成品）。面板只做**只读**的事：扫描、列表、给清单。
        #
        # ⚠ 这段说明里**不能写 markdown 的 `**加粗**`** —— 这是 Tk 标签，
        #   星号会原样显示出来（知识库结构表那边犯过两次，别再犯）。
        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=920,
            text=("「一键采用」在 Zotero 里：多选若干篇文献 → 右键 →"
                  "「补全元数据（本地模型）」→ 插件会先把所有高置信建议"
                  "汇总成一份清单（写着「将写入 N 篇 / M 个字段」）让你确认，"
                  "确认后一次写完并逐篇报结果。\n"
                  "只选一篇时，插件仍然逐条弹确认框 —— 一篇时你要核对证据，"
                  "一次全采用反而不合适。")
        ).pack(fill="x", padx=12, pady=(6, 0))

        # ---- 统计
        self.meta_stats = tk.StringVar(value="还没扫过 —— 点「一键补全」跑一次")
        box = ttk.LabelFrame(f, text=" 扫描结果 ", padding=(10, 6))
        box.pack(fill="x", padx=10, pady=(8, 6))
        ttk.Label(box, textvariable=self.meta_stats, justify="left",
                  font=(self.mono_font, 9)).pack(anchor="w")

        # ---- 结果表
        mid = ttk.Frame(f, padding=(10, 4, 10, 0))
        mid.pack(fill="both", expand=True)
        cols = ("key", "type", "missing", "high", "low", "title")
        self.meta_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                      height=11)
        for c, txt, w in (("key", "条目", 80), ("type", "类型", 90),
                          ("missing", "缺哪些字段", 210),
                          ("high", "高置信", 55), ("low", "低置信", 55),
                          ("title", "标题", 380)):
            self.meta_tree.heading(c, text=txt)
            self.meta_tree.column(c, width=w,
                                  anchor="center" if c in ("high", "low")
                                  else "w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.meta_tree.yview)
        self.meta_tree.configure(yscrollcommand=vs.set)
        self.meta_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.meta_tree.bind("<Double-1>", lambda e: self.do_meta_detail())

        self.meta_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.meta_note, foreground="#666",
                  font=(self.ui_font, 9), justify="left", wraplength=920
                  ).pack(fill="x", padx=12, pady=(4, 8))

        # 最近一次扫描的原始结果，「一键采用高置信」直接用它（不再重扫）
        self.meta_scan_result: dict = {}

    def _meta_busy(self, busy: bool):
        """扫描期间把按钮置灰（照 q_* 那一套 busy 模式）。

        ⚠ Tk 的控件状态只能在主线程改，所以这里统一走 root.after(0, ...)。
        """
        def apply():
            st = "disabled" if busy else "normal"
            self.meta_scan_btn.configure(state=st)
            self.meta_model_btn.configure(state=st)
            self.meta_typefix_btn.configure(state=st)
            # 取消按钮反过来：忙的时候才能点
            self.meta_cancel_btn.configure(state="normal" if busy else "disabled")
        self.root.after(0, apply)

    def do_meta_scan(self):
        self._run_meta_scan(use_model=False)

    def do_meta_scan_model(self):
        """调模型的批量补全。

        ⚠ 单独一个按钮、而且要用户先点确认：73 篇 × 每篇几秒 = 十几分钟，
          用户必须知道自己在等什么（照面板里其它长任务的规矩，先问再做）。
        """
        if not messagebox.askyesno(
                "确认",
                "「用本地模型补全」会对每一篇缺字段的文献都调一次本地小模型，\n"
                "73 篇大约要十几分钟（纯规则那档只要几秒）。\n\n"
                "模型给的都算「低置信」，要你自己核对。继续？"):
            return
        self._run_meta_scan(use_model=True)

    def _run_meta_scan(self, use_model: bool):
        self._meta_busy(True)
        self.say(f"[元数据补全] 开始扫描"
                 f"（{'规则 + 本地模型' if use_model else '纯规则'}）…")
        self.meta_note.set("扫描中…（随时可以点「取消」，已扫出来的部分会保留）")

        def work():
            ok, payload, err = self._api(
                "POST", "/metafill-scan",
                {"use_model": bool(use_model)}, timeout=3600)
            if not ok:
                self.out_queue.put(("log", f"[元数据补全] ✗ 失败：{err}"))
                self.out_queue.put(("log",
                                    "[元数据补全] 提示：本地服务要开着"
                                    "（scripts\\4-service.vbs），"
                                    "而且服务必须是「新代码」启动的"
                                    "（改过服务端文件后要重启它）。"))
                self.meta_note.set(f"扫描失败：{err}")
                self._meta_busy(False)
                return
            self.meta_scan_result = payload

            def apply():
                rows = payload.get("rows") or []
                for i in self.meta_tree.get_children():
                    self.meta_tree.delete(i)
                for r in rows:
                    self.meta_tree.insert("", "end", values=(
                        r.get("key", ""), r.get("item_type", ""),
                        "/".join(r.get("missing") or []),
                        r.get("n_high", 0), r.get("n_low", 0),
                        str(r.get("title") or "")[:80]))
                plan = (payload.get("apply_plan") or {}).get("items") or []
                n_fields = sum(len(i.get("fields") or []) for i in plan)
                self.meta_stats.set(
                    f"全库 {payload.get('total')} 篇｜缺字段 "
                    f"{payload.get('missing_total')} 篇｜本次扫了 "
                    f"{payload.get('scanned')} 篇｜耗时 "
                    f"{payload.get('elapsed')} 秒"
                    + ("（⚠ 被取消，只有部分结果）" if payload.get("canceled")
                       else "")
                    + f"\n高置信建议 {payload.get('high_total')} 条"
                      f"（可自动写回的 {n_fields} 个字段，涉及 {len(plan)} 篇）"
                      f"｜低置信建议 {payload.get('low_total')} 条")
                exc = payload.get("excluded_high") or {}
                if exc:
                    # 有 high 置信建议但**没列进可写清单**的字段要说明白，
                    # 否则用户会问"明明有 25 条高置信，为什么只写 23 个字段"
                    why = "、".join(f"{k}（{v} 条）" for k, v in exc.items())
                    self.meta_note.set(
                        f"⚠ 另有 {why} 是「作者」类建议：按现有规矩"
                        f"不自动写作者（要逐条确认可以在 Zotero 里右键那篇"
                        f"走「逐条确认」）。")
                else:
                    self.meta_note.set("")
                self._meta_busy(False)
            self.root.after(0, apply)

            self.out_queue.put(("log",
                                f"[元数据补全] ✓ 完成：缺字段 "
                                f"{payload.get('missing_total')} 篇，"
                                f"高置信 {payload.get('high_total')} 条，"
                                f"低置信 {payload.get('low_total')} 条，"
                                f"耗时 {payload.get('elapsed')} 秒"
                                + ("（被取消）" if payload.get("canceled") else "")))
        threading.Thread(target=work, daemon=True, name="kb-metafill-scan").start()

    def do_meta_cancel(self):
        """请求取消批量扫描。

        为什么取消要发到服务端（而不是本地忽略结果）：
        扫描是服务端在跑，本地忽略结果的话那 73 篇它照样跑完、照样占着
        数据库快照 —— 用户以为停了，其实没停。服务端在每篇之间检查取消标志。
        """
        def work():
            ok, payload, err = self._api("POST", "/metafill-scan-cancel", {},
                                         timeout=30)
            self.out_queue.put(("log", f"[元数据补全] 取消请求："
                                       f"{'已发出' if ok else '失败 ' + err}"
                                       f"（会在当前这一篇算完后停下）"))
        threading.Thread(target=work, daemon=True, name="kb-metafill-cancel").start()

    def do_meta_detail(self):
        """看选中那篇的建议明细（每条带值和原文证据）。"""
        sel = self.meta_tree.selection()
        if not sel:
            self.meta_note.set("先在上面选一篇（双击也行）")
            return
        key = self.meta_tree.item(sel[0], "values")[0]
        row = next((r for r in (self.meta_scan_result.get("rows") or [])
                    if r.get("key") == key), None)
        if not row:
            self.meta_note.set("这篇不在上次扫描结果里，重新扫一次")
            return
        lines = [f"{key}　{row.get('title', '')[:70]}",
                 f"类型：{row.get('item_type', '')}　"
                 f"正文来源：{row.get('fulltext_source') or '无'}",
                 f"缺的字段：{'、'.join(row.get('missing') or []) or '（无）'}", ""]
        if row.get("high"):
            lines.append("== 高置信建议（规则抽取，自动写回只会写这些）==")
            for h in row["high"]:
                lines.append(f"  {h['field']} = {h['value']}")
                if h.get("evidence_text"):
                    lines.append(f"      证据：{h['evidence_text']}")
        if row.get("low"):
            lines.append("")
            lines.append("== 低置信建议（本地小模型给的，会编，必须自己核对）==")
            for h in row["low"]:
                lines.append(f"  {h['field']} = {h['value']}")
                if h.get("evidence_text"):
                    lines.append(f"      证据：{h['evidence_text']}")
        if row.get("skipped"):
            lines.append("")
            lines.append("== 没建议的字段（为什么）==")
            for s in row["skipped"]:
                lines.append(f"  {s.get('field')}：{s.get('why')}")
        if row.get("notes"):
            lines.append("")
            lines.append("== 说明 ==")
            for n in row["notes"]:
                lines.append(f"  · {n}")
        if row.get("error"):
            lines.append("")
            lines.append(f"⚠ 这一篇出错了：{row['error']}")
        self._show_dialog(f"元数据建议明细 · {key}", "\n".join(lines))

    def do_typefix_scan(self):
        """检查"类型/标题被网站污染"的条目（只读；改不改由用户在 Zotero 里定）。"""
        self._meta_busy(True)
        self.say("[类型修正] 正在找「网页类型却挂着 PDF」的条目…")

        def work():
            ok, payload, err = self._api("POST", "/typefix-scan",
                                         {"use_network": False}, timeout=600)
            if not ok:
                self.out_queue.put(("log", f"[类型修正] ✗ 失败：{err}"))
                self._meta_busy(False)
                return
            rows = payload.get("rows") or []
            self.out_queue.put(("log",
                f"[类型修正] ✓ 查了 {payload.get('checked')} 篇"
                f"（挂了 PDF 且类型不是期刊/会议/学位论文/图书的），"
                f"其中 {payload.get('suspect_total')} 篇像是"
                f"「先存网页、后来挂 PDF」，耗时 {payload.get('elapsed')} 秒"))
            lines = [
                "下面这些条目「类型不像正经文献、却挂着 PDF」——",
                "最常见的原因是：先用浏览器「保存网页」存下来，后来才把 PDF 拖上去。",
                "这样条目的「类型」和「标题」都还是网页的样子（标题尾部常带站点名）。",
                "",
                f"共查了 {payload.get('checked')} 篇，"
                f"其中 {payload.get('suspect_total')} 篇有嫌疑。",
                "",
            ]
            for r in rows:
                lines.append("=" * 62)
                lines.append(f"{r.get('key')}　类型：{r.get('item_type')}"
                             f"　PDF {r.get('n_pdfs')} 个"
                             f"　{'有DOI' if r.get('doi') else '没有DOI'}")
                lines.append(f"标题：{r.get('title')}")
                for s in (r.get("signals") or []):
                    lines.append(f"  · {s}")
                for fx in (r.get("fixes") or []):
                    if fx.get("kind") == "type":
                        lines.append(f"  → 建议类型：{fx.get('current')}"
                                     f" → {fx.get('value')}"
                                     f"　〔{fx.get('source')}·{fx.get('confidence')}〕")
                    else:
                        lines.append(f"  → 建议标题：{fx.get('value')}"
                                     f"　〔{fx.get('source')}·{fx.get('confidence')}〕")
                    if fx.get("rule"):
                        lines.append(f"     依据：{fx['rule']}")
                    ev = (fx.get("evidence") or {}).get("text")
                    if ev:
                        lines.append(f"     证据：{str(ev)[:160]}")
                if r.get("will_drop"):
                    lines.append(f"  ⚠ 改类型会清掉这些字段："
                                 f"{'、'.join(r['will_drop'])}")
                for n in (r.get("notes") or []):
                    lines.append(f"  · {n}")
                lines.append("")
            lines.append("=" * 62)
            lines.append("⚠ 面板「不会」替你改类型 —— 改类型是敏感操作，")
            lines.append("   请到 Zotero 里右键那一条 →「补全元数据（本地模型）」：")
            lines.append("   它会先弹一个确认框，逐条给你看"
                         "「现在是什么 → 要改成什么」和会丢哪些字段，")
            lines.append("   你点了确认才写，写完还会重新读一遍库向你复述结果。")
            self.out_queue.put(("dialog", ("类型/标题污染检查", "\n".join(lines))))
            self._meta_busy(False)
        threading.Thread(target=work, daemon=True, name="kb-typefix-scan").start()

    def _build_env_tab(self):
        """运行环境页：项目目录 / Python / Ollama 三个位置 + 检测报告。

        为什么值得单独一页：这三个位置**互相独立**，而面板"打不开"
        九成是因为其中一个不对。把它摆在明面上，用户自己就能看出来。
        """
        f = self.tab_env
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="运行环境", font=(self.ui_font, 11, "bold")).pack(side="left")
        ttk.Button(head, text="重新检测", command=self.refresh_env).pack(
            side="right")

        body = ttk.Frame(f, padding=(10, 6, 10, 4))
        body.pack(fill="x")

        self.env_vars = {}
        rows = [
            ("project_root", "项目目录", "代码和 .venv 所在（含 offline、online）"),
            ("python", "Python 解释器", "跑脚本用；优先 .venv\\Scripts\\pythonw.exe"),
            ("ollama", "Ollama 程序", "本地模型（可选，不装也能用 API）"),
        ]
        for i, (key, label, hint) in enumerate(rows):
            ttk.Label(body, text=label + "：").grid(row=i * 2, column=0,
                                                    sticky="w", pady=(6, 0))
            var = tk.StringVar(value="检测中…")
            self.env_vars[key] = var
            ttk.Label(body, textvariable=var, font=(self.mono_font, 9),
                      foreground="#06c", wraplength=780,
                      justify="left").grid(row=i * 2, column=1, sticky="w",
                                           padx=(6, 0), pady=(6, 0))
            ttk.Label(body, text=hint, foreground="#888",
                      font=(self.ui_font, 8)).grid(row=i * 2 + 1, column=1, sticky="w",
                                         padx=(6, 0))

        self.env_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.env_note, foreground="#666",
                  wraplength=900, justify="left").pack(
            fill="x", padx=12, pady=(10, 4))

        btns = ttk.Frame(f, padding=(10, 0, 10, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="改这些设置（在 Zotero 里）",
                   command=self.do_open_env_settings).pack(side="left")
        ttk.Button(btns, text="打开模型设置（在 Zotero 里）",
                   command=self.do_open_model_settings).pack(side="left",
                                                             padx=6)
        ttk.Button(btns, text="启动本地服务", command=self.do_service_start).pack(
            side="left", padx=6)
        ttk.Button(btns, text="服务状态", command=self.do_service_status).pack(
            side="left")

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

    # ---------------------------------------------------------- 高级

    def _build_advanced_tab(self):
        """冷门 / 危险 / 一次性的操作收在这里：功能不删，只是不挡路。

        为什么要收：旧版把「侧载安装 + 诊断 + 打包 xpi + 复制 token +
        分类重整五连击」和日常操作摆在同一个标签页里，用户看着一堆
        不知道是什么的按钮，反而找不到"更新索引"。

        ⚠ 用户第二次反馈："高级里的一些功能没用吧，比如分类重整，插件与本地服务。"
          这个判断是对的 —— 那些是**一次性**的（分类重整已经跑完，Zotero 也升级过了）
          或**只有开发者用**的（打包 xpi、侧载安装、任务桥接）。
          但直接删掉有风险：真要重装插件、或以后 Zotero 再升级时还得用。
          所以处理成：**日常的三组排在最前面，一次性/开发者的移到后面并标明**，
          让用户一眼知道"下面这些不用管"。
        """
        f = self.tab_adv
        note = ttk.Label(
            f, foreground="#666", wraplength=900, justify="left",
            font=(self.ui_font, 10),
            text="这里是一次性或排错用的功能，平时用不到。"
                 "下面「一次性 / 开发者」那几组现在可以不管 —— "
                 "分类重整已经做完了，Zotero 也已经升级过了，"
                 "打包/侧载只在重新安装插件时才需要。")
        note.pack(fill="x", padx=12, pady=(10, 6))

        # 先放日常会用到的；一次性/开发者的在后面（下面 groups 的顺序就是显示顺序）
        groups = [
            ("检索文献", "search"),
            ("手动调权重（重点标记）", "weight"),
            ("索引维护", [
                ("统计", self.do_stats, "各表行数"),
                ("列出当前分类", self.do_coll_list, "看 Zotero 里的分类"),
                ("分类分布统计", self.do_coll_stats, "每个类有多少篇"),
                ("重载技能到 DSH", self.do_sync_skill, "改了 SKILL.md 后同步"),
                ("清理测试残留", self.do_clean_test, "删掉自检留下的假经验"),
            ]),
            ("—— 以下是一次性 / 开发者功能，平时不用管 ——", "divider"),
            ("分类重整（改 Zotero 里的分类归属）· 已跑完", [
                ("1 探测写入能力", self.do_sync_check, "看能不能连上 Zotero"),
                ("2 申请授权", self.do_sync_authorize, "Zotero 会弹窗，要允许"),
                ("3 干跑（只看要动什么）", self.do_sync_plan, "不修改任何东西"),
                ("4 执行重整", self.do_sync_apply, "真的改分类。先干跑"),
                ("5 核对结果", self.do_sync_verify, "改完检查"),
            ]),
            ("插件安装 · 只在重装插件时需要", [
                ("打包插件 xpi", self.do_build_plugin, "改过插件代码后重新打包"),
                ("侧载状态", self.do_sideload_status, "绕开 UI 安装的备用方式"),
                ("侧载安装", self.do_sideload_install, "UI 装不上时用"),
                ("诊断安装问题", self.do_diagnose, "装不上时跑这个看原因"),
                ("卸载侧载", self.do_sideload_remove, "撤销侧载"),
            ]),
            ("本地服务 · 一般不用手动起", [
                ("启动本地服务", self.do_service_start, "Zotero 开着时它会自动拉起"),
                ("服务状态", self.do_service_status, "地址、token、各项路径"),
                ("复制服务 token", self.do_copy_token, "极少数情况要手工填"),
            ]),
            ("Zotero 升级 · 已升级完", [
                ("升级前检查", self.do_upgrade_check, "查兼容性"),
                ("备份数据", self.do_upgrade_backup, "备份 zotero.sqlite"),
                ("升级后核对", self.do_upgrade_verify, "确认数据没丢"),
            ]),
        ]
        for title, items in groups:
            if items == "divider":
                sep = ttk.Frame(f, padding=(12, 8, 12, 0))
                sep.pack(fill="x")
                ttk.Separator(sep, orient="horizontal").pack(fill="x")
                ttk.Label(f, text=title, foreground="#999",
                          font=(self.ui_font, 9)).pack(anchor="w", padx=14)
                continue
            box = ttk.LabelFrame(f, text=" " + title + " ", padding=(10, 6))
            box.pack(fill="x", padx=12, pady=4)
            if isinstance(items, str):
                self._build_adv_inputs(box, items)
                continue
            line = ttk.Frame(box)
            line.pack(fill="x")
            for text, cmd, tip in items:
                b = ttk.Button(line, text=text, command=cmd)
                b.pack(side="left", padx=(0, 6), pady=2)
                self._tip(b, tip)

        ttk.Label(f, text="输出", font=(self.ui_font, 9, "bold")).pack(
            anchor="w", padx=14, pady=(8, 0))
        self.adv_text = scrolledtext.ScrolledText(f, height=9, wrap="word",
                                                  font=(self.mono_font, 10))
        self.adv_text.pack(fill="both", expand=True, padx=12, pady=(2, 10))

    def _build_adv_inputs(self, box, kind: str):
        """高级页里需要输入框的两组：检索、手动调权重。"""
        if kind == "search":
            ttk.Label(box, text="关键词：").pack(side="left")
            self.q_var = tk.StringVar()
            qe = ttk.Entry(box, textvariable=self.q_var, width=38)
            qe.pack(side="left", padx=4)
            qe.bind("<Return>", lambda _e: self.do_search())
            b = ttk.Button(box, text="搜索", command=self.do_search)
            b.pack(side="left")
            self._tip(b, "结果里带可读的文献名和 key，便于复制")
            # ⚠ 用户反馈："高级里的检索文献也应该能下拉列表选取。"
            #   所以除了"搜关键词"，再给一个"从列表选"——在弹窗里能按列看、
            #   能排序，选中一篇直接填到下面的「文献 key」框，不用手抄。
            b2 = ttk.Button(box, text="从列表选…",
                            command=lambda: self.open_paper_picker(
                                on_pick=self._fill_weight_key))
            b2.pack(side="left", padx=6)
            self._tip(b2, "打开全部文献列表，选一篇直接填到下面的 key 框")
            return

        ttk.Label(box, text="文献 key：").pack(side="left")
        self.w_key = tk.StringVar()
        ttk.Entry(box, textvariable=self.w_key, width=14).pack(side="left",
                                                               padx=4)
        ttk.Label(box, text="备注：").pack(side="left")
        self.w_note = tk.StringVar()
        ttk.Entry(box, textvariable=self.w_note, width=30).pack(side="left",
                                                                padx=4)
        for text, cmd, tip in (
            ("标为重点", lambda: self.do_weight(True), "检索时权重提高"),
            ("取消重点", lambda: self.do_weight(False), ""),
            ("列出权重榜", self.do_weight_list, "看全部被加权过的文献"),
        ):
            b = ttk.Button(box, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self._tip(b, tip)

    def _fill_weight_key(self, key):
        """从选择器选中后，把 key 填进「手动调权重」的输入框。"""
        if hasattr(self, "w_key"):
            self.w_key.set(key)
        self.say(f"[{ts()}] 已填入 key：{key}（可直接标重点）")

    def _build_experience_tab(self):
        """经验库：知识库"学到了什么"。"""
        f = self.tab_exp
        bar = ttk.Frame(f, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="关键词：").pack(side="left")
        self.exp_query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.exp_query, width=32)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self.do_exp_list())
        for text, cmd, tip in (
            ("查询", self.do_exp_list, "按关键词找相关经验"),
            ("列出全部", self.do_exp_all, "看库里所有经验记录"),
            ("待确认清单", self.do_pending, "从对话里抽出来、还没确认的经验"),
            ("从会话记录补经验", self.do_learn, "扫 DSH 对话转录，找可补的经验"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=3)
            self._tip(b, tip)

        ttk.Label(
            f, foreground="#888", wraplength=900, justify="left",
            text="经验是知识库越用越准的唯一机制：用某篇的方法做过尝试后，"
                 "记录「有效 / 无效 / 部分有效」，下次检索会自动把验证过的文献排前。"
        ).pack(fill="x", padx=12, pady=(0, 4))

        self.exp_text = scrolledtext.ScrolledText(f, height=16, wrap="word",
                                                  font=(self.mono_font, 10))
        self.exp_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _build_ai_tab(self):
        """分类建议：调本地模型判断"这篇该归哪类"。

        与 Zotero 插件里的弹窗**同一份实现**（judge.classify_item），
        所以这里的结果和插件里点出来的一致。
        """
        f = self.tab_ai
        bar = ttk.Frame(f, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        for text, cmd, tip in (
            ("检查模型服务", self.do_ai_status, "看 Ollama/API 通不通、有哪些模型"),
            ("生成分类建议", lambda: self.do_taxonomy(True),
             "调模型给未分类的文献提建议"),
            ("只看分类分布", lambda: self.do_taxonomy(False), "不调模型，只统计"),
            ("处理待确认建议", self.do_pending, "看插件弹窗攒下的建议"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._tip(b, tip)

        bar2 = ttk.Frame(f, padding=(10, 0, 10, 4))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="选中的文献：").pack(side="left")
        # ⚠ 用户反馈："下拉列表太乱了，长长短短的标题和作者、分类等等，
        #   下拉列表可以单独做个弹窗出来，可以手动拉宽窄，包括每列的宽窄。"
        #   所以不再用 Combobox 把一整行塞进去，改成"只显示结果 + 按钮开表格"。
        self.paper_label = tk.StringVar(value="（还没选）")
        ttk.Label(bar2, textvariable=self.paper_label, foreground="#06c",
                  font=(self.ui_font, 10)).pack(side="left", padx=(2, 8))
        b = ttk.Button(bar2, text="选择文献…", command=self.open_paper_picker)
        b.pack(side="left")
        self._tip(b, "打开文献列表：列宽可拖、表头可排序、窗口可拉大")
        b2 = ttk.Button(bar2, text="刷新列表", command=self.refresh_paper_list)
        b2.pack(side="left", padx=6)
        self._tip(b2, "从知识库重新读文献列表")

        bar3 = ttk.Frame(f, padding=(10, 0, 10, 4))
        bar3.pack(fill="x")
        for text, cmd, tip in (
            ("生成要点", self.do_ai_summary, "给选中的这篇写摘要要点"),
            ("分类建议", self.do_taxonomy_one, "让模型判断该归哪类，可一键应用"),
            ("调整建议", self.do_taxonomy_talk,
             "跟模型对话：说一句哪里不对，让它重新判断"),
            ("应用建议", self.do_taxonomy_apply,
             "把模型建议的分类/标签写回 Zotero"),
        ):
            b = ttk.Button(bar3, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._tip(b, tip)

        ttk.Label(f, text="跟模型说一句（调整建议时用）",
                  font=(self.ui_font, 9, "bold")).pack(anchor="w", padx=14, pady=(6, 0))
        talk = ttk.Frame(f, padding=(10, 2, 10, 4))
        talk.pack(fill="x")
        self.talk_var = tk.StringVar()
        te = ttk.Entry(talk, textvariable=self.talk_var)
        te.pack(side="left", fill="x", expand=True)
        te.bind("<Return>", lambda _e: self.do_taxonomy_talk())
        ttk.Label(f, foreground="#888", justify="left", wraplength=900,
                  text="例如：「这篇应该归到分类 A」「这是设备类不是方法类」"
                       "「分类对，但标签换成 A、B」。点「调整建议」让模型按这句话重判。"
        ).pack(fill="x", padx=12, pady=(0, 4))

        # 上次的建议存这里，供「调整建议」「应用建议」用
        self.last_suggestion = None
        self.last_key = ""

        ttk.Label(
            f, foreground="#888", wraplength=900, justify="left",
            text="分类建议只在 Zotero 插件弹窗里由你确认后才生效 —— "
                 "这里生成的结果不会自动改你的 Zotero。"
                 "更快的入口：在 Zotero 里右键某篇 →「分类建议（本地模型）」。"
        ).pack(fill="x", padx=12, pady=(0, 4))

        self.ai_text = scrolledtext.ScrolledText(f, height=12, wrap="word",
                                                 font=(self.mono_font, 10))
        self.ai_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.refresh_paper_list()

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

    def do_open_env_settings(self):
        """提示怎么打开 Zotero 里本插件的设置面板。

        为什么不直接打开：Zotero 没有"用代码打开指定 prefpane"的公开 API
        （Zotero.PreferencePanes.register 只管注册，不提供 open），
        所以给出准确路径让用户自己点，比假装能打开好。
        """
        self.say(f"[{ts()}] 要改运行环境，请打开："
                 " Zotero → 编辑 → 设置 → 文献知识库 → 运行环境")
        messagebox.showinfo(
            "在 Zotero 里改",
            "请打开：\n\n    Zotero → 编辑 → 设置 → 文献知识库 → 运行环境\n\n"
            "那里可以浏览选择「项目目录」「Python 解释器」「Ollama 程序」，\n"
            "点「保存并检测」会立刻告诉你路径对不对。\n\n"
            "（改完回到本面板的「运行环境」页点「重新检测」）")

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

    # ================================================================ 快捷操作

    def do_convert_incremental(self):
        self.run("更新索引（增量）", [os.path.join(ROOT, "offline", "convert.py")],
                 on_done=self._after_build_report)

    def do_convert_full(self):
        self.run("全量重建", [os.path.join(ROOT, "offline", "convert.py"), "--full"],
                 confirm="全量重建会重新解析全部文献并重算向量，约 1-2 分钟。继续？",
                 on_done=self._after_build_report)

    def _after_build_report(self):
        """构建结束后读清单，有事就弹窗 —— 别让用户去日志里翻。

        为什么要弹窗（而不是只在日志里打几行）：
          · **没有 PDF 附件的条目被跳过了**。这不是出错，是那些条目本身缺东西，
            但用户过一阵会奇怪"我库里明明有这篇，怎么知识库搜不到"。
            弹一次说清楚，比让他猜好。
          · **字符偏移被自动还原**的篇目也值得报一声：那说明源 PDF 的字体
            编码是坏的（换源修不了），用户可能想知道哪些文献属于这种。
        没事发生时（绝大多数构建）**不弹窗**，避免每次构建都打扰。
        """
        try:
            import schemas as S
            with open(S.MANIFEST, encoding="utf-8") as fh:
                mf = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            self.say(f"[XX] 读构建清单失败：{type(exc).__name__}: {exc}")
            return
        skipped = ((mf.get("warnings") or {}).get("skipped_no_pdf")) or []
        deshifted = ((mf.get("fulltext") or {}).get("deshifted")) or []
        if not skipped and not deshifted:
            return

        lines = []
        if deshifted:
            lines.append(f"⚙ {len(deshifted)} 篇的正文原本是「字符整体偏移」的"
                         f"（源 PDF 字体编码坏，换源也修不了），已自动还原：")
            for x in deshifted[:8]:
                lines.append(f"    {x['key']}    {x.get('src', '')}")
            if len(deshifted) > 8:
                lines.append(f"    …还有 {len(deshifted) - 8} 篇")
            lines.append("")
        if skipped:
            lines.append(f"⚠ {len(skipped)} 条没有 PDF 附件，没有收进知识库：")
            for x in skipped[:12]:
                lines.append(f"    {x['key']}    {(x.get('title') or '')[:46]}")
            if len(skipped) > 12:
                lines.append(f"    …还有 {len(skipped) - 12} 条")
            lines.append("")
            lines.append("想让它们进知识库：在 Zotero 里给这些条目挂上 PDF，"
                         "再跑一次构建。")
        self._show_dialog("构建报告", "\n".join(lines))

    def do_check(self):
        self.run("环境自检", [os.path.join(ROOT, "offline", "maintain.py"), "check"])

    def do_stats(self):
        self.run("统计", [os.path.join(ROOT, "offline", "maintain.py"), "stats"])

    def do_backup(self):
        self.run("备份索引", [os.path.join(ROOT, "offline", "maintain.py"), "backup"])

    def do_clean_test(self):
        self.run("清理测试残留",
                 [os.path.join(ROOT, "tools", "kb_admin.py"), "clean", "--test-data"],
                 on_done=lambda: self.run(
                     "清理权重残留",
                     [os.path.join(ROOT, "tools", "kb_admin.py"), "clean",
                      "--orphan-weights"]))

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

    # ================================================================ 经验库

    def do_exp_list(self):
        q = self.exp_query.get().strip()
        if not q:
            return self.do_exp_all()
        self._exp_query_run("query", q)

    def do_exp_all(self):
        self._exp_query_run("all", "")

    def do_pending(self):
        self._exp_query_run("pending", "")

    def _exp_query_run(self, mode: str, q: str):
        def work():
            try:
                import schemas as S
                from searcher import Searcher

                s = Searcher()
                if mode == "pending":
                    path = os.path.join(S.INBOX_DIR, "pending.jsonl")
                    if not os.path.exists(path):
                        body = "待确认清单是空的。\n\n先点「从会话记录补经验」扫一遍会话。"
                    else:
                        import json
                        rows = []
                        for line in open(path, encoding="utf-8"):
                            line = line.strip()
                            if line:
                                try:
                                    rows.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                        pend = [r for r in rows if r.get("status") == "pending"]
                        parts = [f"待确认 {len(pend)} 条（共 {len(rows)} 行）\n"]
                        for r in pend:
                            parts.append(
                                f"[{r['id']}] [{r['outcome']}] 置信 "
                                f"{r.get('confidence', 0):.2f}\n"
                                f"  问题：{r['asked'][:110]}\n"
                                f"  方法：{(r.get('method') or '')[:110]}\n"
                                f"  原因：{(r.get('reason') or '')[:160]}\n")
                        body = "\n".join(parts)
                else:
                    if mode == "query":
                        rows = s.related_experience(q, limit=50)
                    else:
                        rows = [dict(r) for r in s.read(
                            "SELECT * FROM experience ORDER BY id")]
                    label = {"effective": "✅ 有效", "ineffective": "❌ 无效",
                             "partial": "◐ 部分", "unknown": "❓ 未验证"}
                    parts = [f"经验 {len(rows)} 条\n" + "=" * 70]
                    for r in rows:
                        parts.append(
                            f"#{r['id']} {label.get(r['outcome'], r['outcome'])}"
                            f"  {r['created_at'][:10]}\n"
                            f"  问题：{r['asked'][:130]}\n"
                            f"  方法：{(r.get('method') or '')[:130]}\n"
                            f"  原因：{(r.get('reason') or '')[:200]}\n"
                            f"  依据：{(r.get('evidence') or '(无)')[:160]}\n"
                            f"  文献：{r.get('item_keys') or '[]'}\n")
                    body = "\n".join(parts)
                s.close()
                self.out_queue.put(("show", (self.exp_text, body)))
            except Exception as exc:  # noqa: BLE001
                import traceback
                self.out_queue.put(("show", (self.exp_text,
                                             f"出错了：{type(exc).__name__}: {exc}\n\n"
                                             + traceback.format_exc())))

        threading.Thread(target=work, daemon=True).start()
        self.say(f"[{ts()}] 读取经验（{mode}）")

    def do_learn(self):
        self.run("从会话记录补经验（本地模型）",
                 [os.path.join(ROOT, "offline", "learn.py"), "scan"],
                 on_done=lambda: self._exp_query_run("pending", ""))

    # ================================================================ 权重 / 搜索

    def do_weight(self, pin: bool):
        key = self.w_key.get().strip()
        if not key:
            messagebox.showinfo("缺参数", "请先填文献 key（可在「搜索」结果里看到）。")
            return
        note = self.w_note.get().strip()
        # 参数走环境变量传，不在 -c 字符串里拼 —— 否则备注里的引号、
        # 反斜杠、中文都会变成转义地狱（这里踩过一次，海象运算符还会先在
        # f-string 求值时炸掉）。
        code = (
            "import os, sys\n"
            "sys.path[:0] = [os.environ['KB_OFFLINE'], os.environ['KB_ONLINE']]\n"
            "import schemas as S\n"
            "from searcher import Searcher\n"
            "key = os.environ['KB_KEY']\n"
            "pin = 1 if os.environ['KB_PIN'] == '1' else 0\n"
            "note = os.environ.get('KB_NOTE', '')\n"
            "s = Searcher()\n"
            "if not s.get_item(key):\n"
            "    print(f'[XX] 知识库里没有 key={key} 的条目')\n"
            "    raise SystemExit(1)\n"
            "row = s.read_one('SELECT * FROM item_weight WHERE item_key = ?', (key,))\n"
            "s.write(\n"
            "    'INSERT INTO item_weight(item_key, pinned, manual, note, updated_at,'\n"
            "    ' attempts, effective, ineffective, partial)'\n"
            "    ' VALUES(?,?,?,?,datetime(\\'now\\'),?,?,?,?)'\n"
            "    ' ON CONFLICT(item_key) DO UPDATE SET pinned=excluded.pinned,'\n"
            "    ' manual=excluded.manual, note=excluded.note,'\n"
            "    ' updated_at=excluded.updated_at',\n"
            "    (key, pin, row['manual'] if row else 0.0, note,\n"
            "     row['attempts'] if row else 0, row['effective'] if row else 0,\n"
            "     row['ineffective'] if row else 0, row['partial'] if row else 0))\n"
            "s.reload_weights()\n"
            "print(f'  {key} 现在权重 = {s.weight_of(key):.3f}'\n"
            "      f\"（{'重点' if pin else '非重点'}）\")\n"
            "s.close()\n"
        )
        env = {
            "KB_OFFLINE": os.path.join(ROOT, "offline"),
            "KB_ONLINE": os.path.join(ROOT, "online"),
            "KB_KEY": key,
            "KB_PIN": "1" if pin else "0",
            "KB_NOTE": note,
        }
        self.run_with_env(f"{'标为重点' if pin else '取消重点'} {key}",
                          ["-c", code], env, on_done=self.do_weight_list)

    def run_with_env(self, title: str, args: list[str], extra_env: dict,
                     on_done=None):
        """同 run()，但额外注入环境变量（用于传用户输入的参数）。"""
        if self.busy:
            messagebox.showinfo("忙", "还有任务在跑，等它结束或点「停止当前任务」。")
            return
        self.busy = True
        self.say(f"[{ts()}] ▶ {title}")

        def worker():
            try:
                self.proc = subprocess.Popen(
                    [PY, "-X", "utf8", *args],
                    cwd=ROOT, env={**ENV, **extra_env},
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

    def do_weight_list(self):
        def work():
            try:
                import schemas as S
                from searcher import Searcher

                s = Searcher()
                rows = s.read(
                    "SELECT w.*, i.title, i.year FROM item_weight w "
                    "LEFT JOIN items i ON i.key = w.item_key ORDER BY "
                    "(3*w.pinned + w.manual + 2*w.effective + 0.5*w.ineffective) DESC"
                )
                parts = [f"权重 {len(rows)} 行\n" + "=" * 70]
                for r in rows:
                    mult = S.weight_multiplier(r, S.recency_base(r["year"]))
                    star = " ★重点" if r["pinned"] else ""
                    parts.append(
                        f"{r['item_key']}  乘数 {mult:.2f}{star}\n"
                        f"  尝试 {r['attempts']}｜有效 {r['effective']}｜"
                        f"无效 {r['ineffective']}｜部分 {r['partial']}\n"
                        f"  {(r['title'] or '')[:70]}\n"
                        + (f"  备注：{r['note']}\n" if r["note"] else ""))
                s.close()
                self.out_queue.put(("show", (self.adv_text, "\n".join(parts))))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("show", (self.adv_text, f"出错了：{exc}")))

        threading.Thread(target=work, daemon=True).start()

    def do_search(self):
        q = self.q_var.get().strip()
        if not q:
            messagebox.showinfo("缺参数", "请输入检索词。")
            return

        def work():
            try:
                from searcher import Searcher

                s = Searcher()
                res = s.search(q, limit=12)
                parts = [f"检索「{q}」→ {len(res['hits'])} 条"
                         f"（关键词候选 {res['diagnostics']['fts_candidates']}，"
                         f"向量 {res['diagnostics']['vector_candidates']}）\n" + "=" * 70]
                for i, h in enumerate(res["hits"], 1):
                    parts.append(
                        f"{i}. [{h['key']}] {h['title'][:66]}\n"
                        f"   {h['year']}｜{h['first_author']}｜"
                        f"{'/'.join(h['collections'])}｜权重 {h['weight']}")
                if not res["hits"]:
                    parts.append("没有命中。试试换同义词（中英各一次）。")
                s.close()
                self.out_queue.put(("show", (self.adv_text, "\n".join(parts))))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("show", (self.adv_text, f"出错了：{exc}")))

        threading.Thread(target=work, daemon=True).start()

    # ================================================================ 本地模型

    def do_ai_status(self):
        self.run("检查模型服务",
                 [os.path.join(ROOT, "offline", "judge.py"), "status"])

    def do_ai_tag(self, limit: int, write: bool):
        args = [os.path.join(ROOT, "offline", "judge.py"), "tag",
                "--limit", str(limit)]
        if write:
            args.append("--write")
        self.run(f"打标签（{limit} 篇{'，写入' if write else '，试跑'}）", args)

    # ---------------------------------------------------------- 选文献

    def refresh_paper_list(self):
        """把库里的文献读进来（给弹窗选择器用）。

        读的是**结构化字段**（作者/年份/标题/分类/全文字数/key），
        不是拼好的一整行 —— 拼成一行的坏处已在 paper_picker 里说明。
        """
        def work():
            try:
                import schemas as S
                conn = S.connect(S.INDEX_DB)
                rows = conn.execute(
                    "SELECT key, title, year, first_author, collections, "
                    "fulltext_chars FROM items "
                    "ORDER BY year DESC, first_author").fetchall()
                conn.close()
                out = []
                for r in rows:
                    try:
                        cols = "、".join(json.loads(r["collections"] or "[]"))
                    except Exception:  # noqa: BLE001
                        cols = ""
                    out.append((r["key"], r["first_author"] or "",
                                str(r["year"] or ""), r["title"] or "",
                                cols, r["fulltext_chars"] or 0))
                self.out_queue.put(("papers", out))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("log", f"[XX] 读文献列表失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_paper_list(self, rows):
        """主线程：存下来（Tk 控件只能主线程碰）。"""
        self.paper_rows = rows
        n = len(rows)
        if not getattr(self, "last_key", ""):
            self.paper_label.set(f"（还没选，库里 {n} 篇）")
        self.ai_text.insert(
            "end", f"[{ts()}] 已载入 {n} 篇文献，点「选择文献…」挑一篇。\n")

    def open_paper_picker(self, on_pick=None):
        """打开独立的选择窗口（列宽可拖、表头可排序、窗口可拉大）。

        on_pick 不给时用默认的"_选中就设为当前文献"；给了就用调用方的
        （比如「手动调权重」那边要填到 key 输入框里）。
        """
        rows = getattr(self, "paper_rows", None)
        if rows is None:
            messagebox.showinfo("列表还没读好", "正在读文献列表，稍等一下再点。")
            return
        if not rows:
            messagebox.showinfo("库里还没有文献",
                                "索引里没有条目。先点「手动更新」。")
            return
        try:
            from paper_picker import PaperPicker
        except ImportError as exc:
            messagebox.showerror("缺文件", f"找不到 paper_picker.py：{exc}")
            return
        PaperPicker(self.root, rows, on_pick or self._on_paper_picked,
                    ui_font=self.ui_font, mono_font=self.mono_font,
                    title="选择文献（列宽可拖、表头可排序）")

    def _on_paper_picked(self, key):
        """弹窗里选中了一篇。"""
        self.last_key = key
        self.last_suggestion = None
        for r in getattr(self, "paper_rows", []):
            if r[0] == key:
                self.paper_label.set(
                    f"{r[1] or '（无作者）'} {r[2] or ''} · {r[3][:40]}  [{key}]")
                break
        self.ai_text.delete("1.0", "end")
        self.ai_text.insert("end", f"已选中：{key}\n\n")
        self.say(f"[{ts()}] 选中文献 {key}")

    def selected_key(self) -> str:
        """当前选中的 key（没有就返回空串）。"""
        return getattr(self, "last_key", "") or ""

    # ---------------------------------------------------------- 单篇操作

    def do_ai_summary(self):
        key = self.selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "请在上面的列表里选一篇文献"
                                          "（也可以直接把 key 粘进去）。")
            return
        self.run(f"生成要点 {key}",
                 [os.path.join(ROOT, "offline", "judge.py"), "summarize",
                  "--key", key])

    def do_taxonomy_one(self):
        """给选中的这篇生成分类建议（可选带一句调整意见）。

        走 `judge.py classify --key`，与插件弹窗**同一份实现**
        （`judge.classify_item`），所以口径一致。
        """
        key = self.selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "请在上面的列表里选一篇文献。")
            return
        args = [os.path.join(ROOT, "offline", "judge.py"), "classify",
                "--key", key, "--json"]
        fb = (self.talk_var.get() if hasattr(self, "talk_var") else "").strip()
        if fb:
            args += ["--feedback", fb]
        self.run(f"分类建议 {key}" + ("（已带调整意见）" if fb else ""), args,
                 on_done=self._remember_suggestion)

    def do_taxonomy_talk(self):
        """「调整建议」：把上一轮结论 + 用户那句话一起发回去重判。"""
        fb = (self.talk_var.get() if hasattr(self, "talk_var") else "").strip()
        if not fb:
            messagebox.showinfo(
                "先说一句",
                "请在下面的输入框里写一句你的判断，例如：\n\n"
                "    · 这篇应该归到「分类 A」\n"
                "    · 这是设备类，不是方法类\n\n"
                "模型会基于这句话重新判断（分类仍然只能从已有分类里选）。")
            return
        self.do_taxonomy_one()

    def _remember_suggestion(self):
        """读回最近一次建议（`--json` 会写到 kb 下的文件），供「应用」用。"""
        try:
            p = os.path.join(self.kb_dir(), "last-suggestion.json")
            with open(p, encoding="utf-8") as fh:
                self.last_suggestion = json.load(fh)
            s = self.last_suggestion
            self.say(f"[{ts()}] 建议：{s.get('category') or '（无）'}"
                     f"（置信 {s.get('confidence')}）")
        except Exception as exc:  # noqa: BLE001
            self.say(f"[!!] 没读回建议结果：{exc}")

    def do_taxonomy_apply(self):
        """把最近一次建议应用回 Zotero（分类 + 标签）。

        ⚠ 这是**真的改你的 Zotero**，而且会同步到 zotero.org / 坚果云，
          所以要二次确认。走 tools\\zotero_sync.py 的 apply-one 子命令
          （本机没授权过时它会明确告诉你去哪授权）。
        """
        s = self.last_suggestion
        if not s:
            messagebox.showinfo("还没有建议", "请先点「分类建议」生成一个。")
            return
        key = s.get("key") or self.selected_key()
        cat = s.get("category") or ""
        tags = s.get("tags") or []
        if not cat and not tags:
            messagebox.showinfo("没什么可应用", "这条建议里既没有分类也没有标签。")
            return
        if not messagebox.askyesno(
                "确认应用到 Zotero",
                f"会给这篇：\n\n"
                f"  分类 → {cat or '（不改）'}\n"
                f"  标签 → {'、'.join(tags) if tags else '（不改）'}\n\n"
                f"⚠ 这会真的修改你的 Zotero 库，并同步到 zotero.org / 坚果云。\n"
                f"继续？"):
            return
        args = [os.path.join(ROOT, "tools", "zotero_sync.py"), "apply-one",
                "--key", key, "--category", cat]
        if tags:
            args += ["--tags", ",".join(tags)]
        self.run(f"应用分类建议 {key}", args)

    def do_taxonomy(self, use_model: bool = True):
        """生成分类建议。

        ⚠ 这里的算法**不在面板里**，而是走 `judge.py classify` ——
          它与服务端 `/classify`（Zotero 插件弹窗用的那条）**共用
          `judge.classify_item` 同一份实现**。这样：
             · 面板和插件给出的建议口径完全一致（提示词只有一份）
             · 模型配置也统一（插件设置面板里选的那个模型）
          之前面板等的是一个从来没写过的 `offline/taxonomy.py`，
          所以点了只弹"还没做"。
        """
        args = [os.path.join(ROOT, "offline", "judge.py"), "classify",
                "--limit", "10"]
        if not use_model:
            args.append("--no-model")
        self.run("分类建议" + ("（本地模型）" if use_model else "（只看分布）"), args)

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

    # ================================================================ 分类 / 标签

    def do_coll_list(self):
        def work():
            try:
                import schemas as S
                from searcher import Searcher

                s = Searcher()
                cols = s.collections()
                parts = [f"当前分类 {len(cols)} 个\n" + "=" * 70]
                for c in sorted(cols, key=lambda x: -x["items"]):
                    parts.append(f"  {c['name']:34} {c['items']:>3} 篇")
                # 顺带指出可能的重复（中英同名）
                names = [c["name"] for c in cols]
                dup = [(a, b) for a in names for b in names
                       if a < b and (a.lower() in b.lower() or b.lower() in a.lower())]
                if dup:
                    parts.append("\n⚠ 疑似重复的中英分类：")
                    for a, b in dup[:8]:
                        parts.append(f"    「{a}」 ↔ 「{b}」")
                s.close()
                self.out_queue.put(("show", (self.adv_text, "\n".join(parts))))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("show", (self.adv_text, f"出错了：{exc}")))

        threading.Thread(target=work, daemon=True).start()

    def do_coll_stats(self):
        self.run("分类分布统计",
                 [os.path.join(ROOT, "offline", "maintain.py"), "stats"])

    def do_write_check(self):
        """检查 Zotero 写入能力。

        ⚠ 这里原本调用 `tools/zotero_write.py` —— 那个文件**从来不存在**，
          所以这个按钮点了必然报"找不到文件"。
          实际能干活的是 `zotero_sync.py check`（探测可达性 + 授权需求），
          与下面「分类重整」区的「探测写入能力」是同一件事，直接复用。
        """
        self._sync("check", "检查 Zotero 写入能力")

    def do_sync_skill(self):
        script = os.path.join(ROOT, "scripts", "sync-skill.ps1")
        self.run("重载技能到 DSH",
                 ["-c",
                  "import subprocess, sys\n"
                  "sys.exit(subprocess.call([\n"
                  "    'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',\n"
                  "    '-File', r'" + script + "']))",
                  ])


def console_check() -> int:
    """不开窗口，只在终端打印状态摘要。

    用途：脚本里检查、排障时确认（"GUI 打不开"时先跑这个，
    能区分是环境问题还是 Tk 问题）。返回 0=正常，1=有问题。
    """
    print("=" * 66)
    print("知识库管理面板 · 无界面自检")
    print("=" * 66)
    problems = []
    # ⚠ 这里印的是**两个不同的位置**，别混：
    #   项目目录 = 代码在哪（本文件所在）
    #   知识库   = 数据在哪（跟着 Zotero 数据目录走，可被用户改）
    #   老代码把 ROOT 当知识库印出来，迁移后会误导人以为库在项目里。
    kb = App.kb_dir()
    print(f"  项目目录：  {ROOT}")
    print(f"  知识库目录：{kb}")
    print(f"  Python：    {PY}")
    if PY != VENV_PY:
        problems.append(f"没找到项目 venv（{VENV_PY}），正在用系统 Python，"
                        f"可能缺少依赖")
        print("  [!!] 未使用项目 venv")
    else:
        print("  [OK] 使用项目 venv")
    if not os.path.isdir(kb):
        problems.append(f"知识库目录不存在：{kb}")
        print(f"  [!!] 知识库目录不存在：{kb}")
    try:
        import tkinter  # noqa: F401
        print(f"  [OK] tkinter 可用（Tk {tkinter.TkVersion}）")
    except ImportError as exc:
        problems.append(f"tkinter 不可用：{exc}")
        print(f"  [XX] tkinter 不可用：{exc}")

    for mod in ("mcp", "numpy", "fastembed"):
        try:
            __import__(mod)
            print(f"  [OK] {mod}")
        except ImportError:
            print(f"  [!!] {mod} 缺失")
            problems.append(f"{mod} 缺失")

    try:
        import schemas as S
        if not os.path.exists(S.INDEX_DB):
            problems.append("索引库不存在，先点「更新索引（增量）」")
            print(f"  [XX] 索引库不存在：{S.INDEX_DB}")
        else:
            conn = S.connect(S.INDEX_DB)
            n_items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
            n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            n_vec = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
            n_exp = conn.execute("SELECT COUNT(*) AS n FROM experience").fetchone()["n"]
            conn.close()
            print(f"  [OK] 索引：{n_items} 篇 / {n_chunks} 切片 / {n_vec} 向量 / "
                  f"{n_exp} 条经验")
            if n_chunks and not n_vec:
                problems.append("向量缺失（语义检索不可用）：点「全量重建」或"
                                "跑 convert.py 补向量")
                print("  [!!] 向量缺失")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"读索引失败：{type(exc).__name__}: {exc}")
        print(f"  [XX] 读索引失败：{exc}")

    print("-" * 66)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    else:
        print("一切正常，可以双击 scripts\\0-panel.vbs 打开面板。")
    print("=" * 66)
    return 1 if problems else 0


def _arg_value(flag: str) -> str:
    """取 `--flag value` 或 `--flag=value` 的值（没有则空串）。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return ""


def main() -> int:
    if "--check" in sys.argv:
        return console_check()
    root = tk.Tk()
    # ⚠ 不再调 `tk scaling` —— 它和显式字号是**乘在一起**生效的：
    #   scaling 1.25 + 雅黑 9 号 ≈ 11 号，中文小字会发虚（用户反馈"看不清"）。
    #   让 Tk 按系统 DPI 自己算。
    app = App(root)

    # --tab <名称> ：启动时直接切到某一页。
    # 用途：截图核对界面（Tk 的 Notebook 从外部没法可靠地切页，
    # 只能靠鼠标点坐标，窗口一改大小就失效），也方便以后做自动化 UI 检查。
    want = _arg_value("--tab")
    if want:
        names = {"结构": 0, "知识库结构": 0, "struct": 0,
                 "经验": 1, "经验库": 1, "exp": 1,
                 "分类": 2, "分类建议": 2, "ai": 2,
                 "环境": 3, "运行环境": 3, "env": 3,
                 "质量": 4, "切片质量": 4, "解析健康": 4, "quality": 4,
                         "高级": 5, "adv": 5}
        idx = names.get(want)
        if idx is not None:
            try:
                app.notebook.select(idx)
            except tk.TclError:
                pass

    app.say(f"[{ts()}] 面板就绪。")
    app.say(f"[{ts()}] 项目目录：{ROOT}")
    app.say(f"[{ts()}] 知识库：  {App.kb_dir()}")
    app.say(f"[{ts()}] 提示：长任务在后台跑，日志会实时出现在这里。")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
