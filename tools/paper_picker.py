"""独立的文献选择弹窗：可以拉宽、列宽可拖、点表头排序。

    PaperPicker(parent, rows, on_pick)      → 打开选择窗口

为什么单独做成一个窗口（用户要求）：
    "现在下拉列表太乱了，长长短短的标题和作者、分类等等，下拉列表可以单独
     做个弹窗出来，可以手动拉宽窄，包括每列的宽窄。"

原来的做法是把 `作者 年份 · 标题 [KEY]　［分类］` 拼成一整行塞进 Combobox ——
标题长短不一时那一行既读不出对齐关系、又没法调宽窄，长的被截断、短的留一堆空白。
拆成表格后：每列各有宽度且可拖动，点表头能排序，窗口本身也能拉大。

为什么不放 gui.py 里：gui.py 已经 1600+ 行，再加一个窗口类会更难找东西。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class PaperPicker(tk.Toplevel):
    """选一篇文献。选中后调用 on_pick(key)，然后自己关闭。

    参数
        parent  父窗口
        rows    [(key, author, year, title, collections, fulltext_chars)]
        on_pick 回调，收到选中的 key（取消则不调用）
        ui_font / mono_font 由调用方传进来，保持与主窗口字体一致
    """

    COLUMNS = [
        # (列 id, 表头, 宽度, 最小宽, 是否右对齐)
        #
        # ⚠ 宽度分配是权衡过的：**标题是最关键的信息**，要占大头；
        #   作者/年份/分类/全文/KEY 都是辅助定位用的，别喧宾夺主。
        #   第一版标题只给 380 而分类给 170，结果英文长标题一进来就被截断
        #   （"Supercapattery: Energy storage devices combining functions…"），
        #   而这恰恰是用户要选的那一篇 —— 看不出是哪篇就失去意义了。
        ("author", "作者", 96, 56, False),
        ("year", "年份", 52, 44, True),
        ("title", "标题", 520, 200, False),      # stretch=True，窗口拉宽时它先长
        ("collections", "分类", 130, 70, False),
        ("fulltext", "全文", 62, 46, True),
        ("key", "KEY", 88, 70, False),
    ]

    def __init__(self, parent, rows, on_pick,
                 ui_font="TkDefaultFont", mono_font="TkFixedFont",
                 title="选择文献"):
        super().__init__(parent)
        self.on_pick = on_pick
        self.ui_font = ui_font
        self.mono_font = mono_font
        self._sort_col = None
        self._sort_desc = False
        self._rows = list(rows)
        self._shown = list(rows)

        self.title(title)
        # 可调整大小 —— 用户明确要"可以手动拉宽窄"。
        # 默认给宽一点：标题列 520 + 其余 ≈ 960，所以 1020 起步才不用横向挤。
        self.geometry("1020x540")
        self.minsize(700, 320)
        self.transient(parent)
        # 居中到父窗口
        try:
            parent.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - 1020) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - 540) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        # 置顶：从主面板点开的窗口如果被主窗口挡住，用户会以为"没反应"
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass

        self._build()
        # Esc 关掉、回车选中 —— 键盘可达
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._confirm())
        self.tree.focus_set()

    # ------------------------------------------------------------ 界面

    def _build(self):
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="搜索：").pack(side="left")
        self.query = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.query, font=(self.ui_font, 10))
        e.pack(side="left", fill="x", expand=True, padx=(4, 8))
        e.bind("<KeyRelease>", lambda _e: self._filter())
        e.focus_set()
        ttk.Button(top, text="清除", command=self._clear).pack(side="left")

        wrap = ttk.Frame(self, padding=(10, 0, 10, 4))
        wrap.pack(fill="both", expand=True)
        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="browse")
        for cid, label, width, minw, right in self.COLUMNS:
            self.tree.heading(cid, text=label,
                              command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=width, minwidth=minw,
                             anchor="e" if right else "w",
                             stretch=(cid == "title"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        # 双击直接选中
        self.tree.bind("<Double-1>", lambda _e: self._confirm())

        bottom = ttk.Frame(self, padding=(10, 2, 10, 10))
        bottom.pack(fill="x")
        self.count = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.count,
                  foreground="#666").pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="就用这篇", command=self._confirm).pack(
            side="right", padx=6)
        ttk.Label(bottom, foreground="#888",
                  text="　表头可点着排序，列宽可拖动，窗口可拉大").pack(
            side="right")

        self._fill(self._rows)

    # ------------------------------------------------------------ 数据

    def _fill(self, rows):
        self.tree.delete(*self.tree.get_children())
        for key, author, year, title, cols, ft in rows:
            ftxt = (f"{ft // 1000}k" if ft and ft >= 1000
                    else (str(ft) if ft else "—"))
            self.tree.insert("", "end", iid=key, values=(
                author or "（无作者）", year or "—", title or "(无标题)",
                cols or "（未归类）", ftxt, key))
        self.count.set(f"{len(rows)} 篇")
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])

    def _filter(self):
        q = self.query.get().strip().lower()
        if not q:
            self._shown = list(self._rows)
        else:
            # 作者 / 标题 / 分类 / key 都能搜 —— 想按哪种找都行
            self._shown = [r for r in self._rows
                           if any(q in str(x).lower()
                                  for x in (r[1], r[2], r[3], r[4], r[0]))]
        if self._sort_col:
            self._apply_sort()
        self._fill(self._shown)

    def _clear(self):
        self.query.set("")
        self._filter()

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        self._apply_sort()
        self._fill(self._shown)

    def _apply_sort(self):
        idx = {"author": 1, "year": 2, "title": 3,
               "collections": 4, "fulltext": 5, "key": 0}[self._sort_col]

        def keyf(r):
            v = r[idx]
            if self._sort_col == "fulltext":
                return (v or 0)
            if self._sort_col == "year":
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
            return str(v or "")

        self._shown.sort(key=keyf, reverse=self._sort_desc)

    # ------------------------------------------------------------ 确认

    def _confirm(self):
        sel = self.tree.selection()
        if not sel:
            return
        key = sel[0]
        try:
            self.on_pick(key)
        finally:
            self.destroy()


def pick_paper(parent, rows, on_pick, **kw):
    """打开选择窗口（薄封装，方便调用方少 import 一个类名）。"""
    return PaperPicker(parent, rows, on_pick, **kw)
