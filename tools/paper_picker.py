"""独立的文献选择弹窗：可以拉宽、列宽可拖、点表头排序。

    PaperPicker(parent, rows, on_pick)              → 单选，回调 on_pick(key)
    PaperPicker(parent, rows, on_pick, multi=True,
                selected_keys=[...])                → 多选，回调 on_pick([key, ...])

为什么单独做成一个窗口（用户要求）：
    "现在下拉列表太乱了，长长短短的标题和作者、分类等等，下拉列表可以单独
     做个弹窗出来，可以手动拉宽窄，包括每列的宽窄。"

原来的做法是把 `作者 年份 · 标题 [KEY]　［分类］` 拼成一整行塞进 Combobox ——
标题长短不一时那一行既读不出对齐关系、又没法调宽窄，长的被截断、短的留一堆空白。
拆成表格后：每列各有宽度且可拖动，点表头能排序，窗口本身也能拉大。

## 两种模式（多选是后加的，默认仍是单选）

**单选**（`multi=False`，默认）：`selectmode="browse"`，双击/回车确认，
回调 `on_pick(key)`。这条路径是本文件原来的全部行为，**一个字都没改** ——
`tab_ai`「选择文献…」与 `tab_struct`「打开分级视图」都走它。

**多选**（`multi=True`）：给"一条经验关联哪几篇文献"用（方案附录 F.5 第 2 条）。
    · `selectmode="none"`，勾选**自己管**，状态只存在 `self._sel` 这个 set 里；
    · 最左边多一列 `☑ / ☐`（Tk 的 Treeview 没有原生复选框，用字符 + tag 着色，
      不引第三方库）；
    · 传进来的 `selected_keys` 默认勾上，并且在**默认排序下排在最前**
      （点「选」表头可以切成"未选优先"，也是点过别的列之后切回来的入口）；
    · 点一行 = 切换勾选；双击或回车 = 确认（回调**完整集合**，空列表合法）；
      Esc / 「取消」= 关窗且**不回调**（不改动调用方的关联）。

## 为什么勾选状态必须存在 set 里

搜索与排序会 `tree.delete(*children)` 之后整表重建（`_fill`），行的视觉状态
随时被冲掉；从"行上画的是什么"反推勾选，一次过滤就全丢了。所以 set 是唯一实现，
`_fill` 只负责把 set 画出来。

为什么不放 gui.py 里：gui.py 已经 1600+ 行，再加一个窗口类会更难找东西。
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

# 勾选列的字符（Tk 的 Treeview 没有原生复选框）。⚠ 这一列**不进 COLUMNS**：
# COLUMNS 定义的是"每个数据行的形状"，多选只是多画一列，行的元组长度不变 ——
# 于是 picker_rows() 与 COLUMNS 的形状一致性断言照旧成立。
MARK_ON = "☑"
MARK_OFF = "☐"
SEL_COL = "sel"
SEL_WIDTH = 40

# 两次点击被认为"是双击"的间隔（与 Windows 默认的双击间隔一致）。
# ⚠ 必须自己判：Tk 只按**时间**把第二次按下提升成 <Double-1>，**不看点的是哪一行** ——
#   连点两行（勾两篇）时第二次也会变成 <Double-1>（实测：探针里连着 event_generate
#   两次 <Button-1>，第二次直接被提升，窗口当场被"确认"关掉了）。
DOUBLE_MS = 500

# 可排序列 → 行元组里的下标。
# ⚠ 点表头会走到 _sort_by()：查不到的列名必须**直接返回**，不能 KeyError ——
#   Tk 回调里抛异常不会弹窗（只进 stderr），表现是"点了没反应"。
SORT_INDEX = {"author": 1, "year": 2, "title": 3,
              "collections": 4, "fulltext": 5, "key": 0}


class PaperPicker(tk.Toplevel):
    """选文献。单选回调 on_pick(key)，多选回调 on_pick([key, ...])。

    参数
        parent  父窗口
        rows    [(key, author, year, title, collections, fulltext_chars)]
        on_pick 回调，收到选中的 key（多选：完整集合；取消则不调用）
        ui_font / mono_font 由调用方传进来，保持与主窗口字体一致
        multi   True = 多选模式（默认 False，即原来的单选，行为不变）
        selected_keys  多选模式的**预选集合**（单选模式忽略它）
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
                 title="选择文献", multi=False, selected_keys=None):
        super().__init__(parent)
        self.multi = bool(multi)
        self.on_pick = on_pick
        self.ui_font = ui_font
        self.mono_font = mono_font
        self._sort_col = None
        self._sort_desc = False
        self._rows = list(rows)
        # ---- 勾选状态：多选的**唯一实现**（与行的视觉状态无关）--------------
        want = [str(k) for k in (selected_keys or []) if k and str(k).strip()]
        self._sel = set(want) if self.multi else set()
        if self.multi:
            # 已选、但调用方给的行里没有它（库里的 key 已被删/被合并）→ 补占位行
            self._rows += self._extra_rows(want)
        self._by_key = {str(r[0]): r for r in self._rows}
        self._shown = list(self._rows)
        self._done = False                       # 确认过了：窗口正在关，别再处理点击
        self._last_click = (0.0, "")             # (monotonic*1000, 行 id)，判双击用

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
        # （多选下回车 = 确认：回调完整集合，可能是空的）
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
        # 多选：最左边插一列勾选标记（COLUMNS 本身不动，见文件头的说明）
        cols = ([SEL_COL] if self.multi else []) + [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(
            wrap, columns=cols, show="headings",
            selectmode="none" if self.multi else "browse")
        if self.multi:
            # 点「选」表头 = 按勾选排（再点一次 = 未选优先）。它也是**回到默认顺序**
            # 的唯一入口 —— 点过别的列之后，"已选优先"要靠它切回来。
            self.tree.heading(SEL_COL, text="选",
                              command=lambda: self._sort_by(SEL_COL))
            self.tree.column(SEL_COL, width=SEL_WIDTH, minwidth=34,
                             anchor="center", stretch=False)
            self.tree.tag_configure("picked", foreground="#146c2e")
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
        # 双击：单选 = 选中这一行并确认；多选 = 勾上这一行并确认（见 _on_double）
        self.tree.bind("<Double-1>", self._on_double)
        if self.multi:
            # 多选：点一行 = 切换勾选。单选那条路是 browse 模式自己在管选中行，
            # 这里**不绑** <Button-1>，免得动到单选的默认行为。
            self.tree.bind("<Button-1>", self._on_click)

        bottom = ttk.Frame(self, padding=(10, 2, 10, 10))
        bottom.pack(fill="x")
        self.count = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.count,
                  foreground="#666").pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="确定" if self.multi else "就用这篇",
                   command=self._confirm).pack(side="right", padx=6)
        # 面板文案不用 markdown 的 **（Tk 原样显示）
        ttk.Label(bottom, foreground="#888",
                  text=("　表头可点着排序，列宽可拖动，窗口可拉大；"
                        "点一行勾选/取消，双击或回车确认"
                        if self.multi else
                        "　表头可点着排序，列宽可拖动，窗口可拉大")).pack(
            side="right")

        # ⚠ 这里必须过一遍 _order()：多选时"已选优先"也是**打开时**就要生效的
        #   （第一版直接 _fill(self._rows)，结果已选的没排到最前 —— 探针逮到）。
        self._shown = self._order(self._rows)
        self._fill(self._shown)

    # ------------------------------------------------------------ 数据

    def _extra_rows(self, want):
        """给"已选但 rows 里没有"的 key 补一行占位（多选）。

        调用方（经验编辑器）传进来的已选集合取自库里那条经验的 item_keys，
        里面可能有**已经删除/合并掉**的文献 —— 它们本来就不在 rows 里。
        若确认时把它们悄悄丢掉：用户什么都没做，关联就少几篇；而权重是按
        keys 重算的（experience.weight_statements 逐 key 计分），会连着把给
        它们的分数一起扣掉。所以补一行占位：看得见、能取消勾选、不勾就不会
        出现在返回的集合里。
        """
        have = {str(r[0]) for r in self._rows}
        out, seen = [], set()
        for k in want:
            if k in have or k in seen:
                continue
            seen.add(k)
            out.append((k, "（不在列表）", "—",
                        "（这篇不在当前文献列表里：可能已删除或已合并）", "", 0))
        return out

    def _values(self, row):
        """一行要显示的单元格（多选时最前面多一个勾选标记）。"""
        key, author, year, title, cols, ft = row
        ftxt = (f"{ft // 1000}k" if ft and ft >= 1000
                else (str(ft) if ft else "—"))
        vals = (author or "（无作者）", year or "—", title or "(无标题)",
                cols or "（未归类）", ftxt, key)
        if self.multi:
            vals = (MARK_ON if self._picked(key) else MARK_OFF,) + vals
        return vals

    def _picked(self, key) -> bool:
        return str(key) in self._sel

    def _fill(self, rows):
        """整表重建（搜索/排序/重画都走这里）。勾选状态只从 self._sel 取。"""
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", iid=row[0], values=self._values(row),
                             tags=("picked",) if self._picked(row[0]) else ())
        self._update_count()
        if self.multi:
            # 多选：selectmode="none"，不设选中行（selection 在这里没有意义）
            return
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])

    def _update_count(self):
        """底部计数。单选保持原来的"N 篇"；多选要实时说出"已选 N 篇"。"""
        if not self.multi:
            self.count.set(f"{len(self._shown)} 篇")
            return
        text = f"已选 {len(self._sel)} 篇"
        text += (f"　列表 {len(self._shown)}/{len(self._rows)} 篇"
                 if len(self._shown) != len(self._rows)
                 else f"　列表 {len(self._rows)} 篇")
        self.count.set(text)

    def _filter(self):
        q = self.query.get().strip().lower()
        if not q:
            self._shown = list(self._rows)
        else:
            # 作者 / 标题 / 分类 / key 都能搜 —— 想按哪种找都行
            self._shown = [r for r in self._rows
                           if any(q in str(x).lower()
                                  for x in (r[1], r[2], r[3], r[4], r[0]))]
        self._shown = self._order(self._shown)
        self._fill(self._shown)

    def _clear(self):
        self.query.set("")
        self._filter()

    def _sort_by(self, col):
        # "选"不是一列数据（行元组里没有它），但它可以排：按勾选状态。
        if col not in SORT_INDEX and not (col == SEL_COL and self.multi):
            return
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        self._shown = self._order(self._shown)
        self._fill(self._shown)

    def _order(self, rows):
        """把 rows 排成要显示的顺序。

        默认排序（没点过表头）：多选时**已选优先** —— 这就是"已选的行排在最前"
        的唯一实现；list.sort 是稳定的，所以两组内部都保持传入顺序
        （= picker_rows() 的 ORDER BY year DESC, first_author）。
        点过表头：按那一列排 —— 用户明确要的是那个顺序，不要再插一层"已选优先"，
        否则年份列看着就是乱的。勾选状态与顺序无关，怎么排都不会丢。

        ⚠ 切换勾选**不重新排序**（见 _toggle）：勾一下就让行跳到最前，会让双击
          变得危险 —— 第一次点击把那一行挪走，第二次点击落到的就是**另一行**。
          所以"已选优先"只在整表重建时生效：打开、搜索、清除搜索、点表头。
        """
        out = list(rows)
        if self._sort_col == SEL_COL:      # 点了「选」表头：已选优先 / 未选优先
            out.sort(key=lambda r: 0 if self._picked(r[0]) else 1,
                     reverse=self._sort_desc)
            return out
        if self._sort_col:
            idx = SORT_INDEX[self._sort_col]

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

            out.sort(key=keyf, reverse=self._sort_desc)
            return out
        if self.multi and self._sel:
            out.sort(key=lambda r: 0 if self._picked(r[0]) else 1)
        return out

    # ------------------------------------------------------------ 多选的勾选

    def _on_click(self, event):
        """多选：点一行 = 切换勾选。"""
        if self._done:
            return
        row = self.tree.identify_row(event.y)
        if row:
            self._last_click = (time.monotonic() * 1000.0, row)
            self._toggle(row)

    def _toggle(self, key):
        """切换一篇的勾选，并把**这一行**重画（不动其它行、不动顺序）。"""
        key = str(key)
        if key in self._sel:
            self._sel.discard(key)
        else:
            self._sel.add(key)
        if self.tree.exists(key):
            self.tree.item(key, values=self._values(self._by_key[key]),
                           tags=("picked",) if self._picked(key) else ())
        self._update_count()

    def _picked_keys(self):
        """确认时要回调的**完整集合**：勾上的都在里面（空列表合法）。

        顺序 = 传进来的 rows 的顺序（**稳定**：不受用户此刻搜索/排到哪一列影响）。
        被过滤掉但勾着的那些也算 —— 过滤只是"看不见"，不是"取消勾选"。
        注：默认排序下，"已选优先"是稳定排序，所以已选那批的相对顺序与 rows 一致，
        两种口径得到的顺序相同；用户手动按某列排过之后才可能有差别，那时取 rows 序。
        """
        return [str(r[0]) for r in self._rows if self._picked(r[0])]

    # ------------------------------------------------------------ 确认

    def _on_double(self, event):
        """双击 = 确认。

        ⚠ Tk 把"双击间隔内的第二次按下"提升成 <Double-1> 时**只看时间、不看行** ——
          连点两行（想勾两篇）时第二次也会走到这里。所以多选下要自己判一次：
          **同一行 + 间隔够近**才算双击；否则就是"手快点了另一行"，按普通点击处理
          （否则连点两行会把窗口直接确认关掉，用户还没勾完）。
        真双击：先把这一行**勾上**再确认 —— 用户双击一行显然是"就要这篇"；
        这也与单选模式一致（那边双击未选中的行 = 选中并确认）。
        """
        if not self.multi:
            self._confirm()
            return
        if self._done:
            return
        row = self.tree.identify_row(event.y)
        last_t, last_row = self._last_click
        gap = time.monotonic() * 1000.0 - last_t
        if row and not (row == last_row and 0.0 <= gap <= DOUBLE_MS):
            self._last_click = (time.monotonic() * 1000.0, row)
            self._toggle(row)
            return
        if row and not self._picked(row):
            self._sel.add(str(row))
            self._update_count()
        self._confirm()

    def _confirm(self):
        self._done = True
        if self.multi:
            # 完整集合（含被取消的）；空列表也照回调 —— "没有关联文献"是合法状态
            keys = self._picked_keys()
            try:
                self.on_pick(keys)
            finally:
                self.destroy()
            return
        sel = self.tree.selection()
        if not sel:
            return
        key = sel[0]
        try:
            self.on_pick(key)
        finally:
            self.destroy()


def pick_paper(parent, rows, on_pick, **kw):
    """打开选择窗口（薄封装，方便调用方少 import 一个类名）。

    多选：pick_paper(parent, rows, on_pick, multi=True, selected_keys=[...])，
    这时 on_pick 收到的是 key 的列表。
    """
    return PaperPicker(parent, rows, on_pick, **kw)
