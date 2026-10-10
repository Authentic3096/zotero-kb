"""browser.py —— 「打开知识库」：文献列表 → 级别 → 打开那个 md。

## 为什么要有它

知识库根目录点开是一堆 `22X9PMR6.md` —— **文件名是 Zotero 的 key，人认不出
是哪篇**；想打开某一篇的某一层，只能先记 key 再去目录里翻。所以：
先弹一份**文献清单**（作者/年份/标题都在），选中一篇再列它的**级别**
（见 offline/kbviews.py 的 LEVELS），双击就打开对应的 md。

## 两段式的分工

第一段（选文献）直接复用 `paper_picker.PaperPicker` —— 它已经能满足
"列宽可拖、表头可排序、窗口可拉大、能搜索"，不再另写一个。
第二段（选级别）就是本文件里的 `LevelPicker`：级别只有 5 个，
重点是让人看清"每一层里到底是什么"，所以列宽给"里面有什么"最多。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from .common import ROOT  # noqa: F401 —— 顺便完成 offline\ 的 sys.path 引导
import procrun as PR  # noqa: E402


def level_rows(key: str) -> list[dict]:
    """问 offline/kbviews.py 要这篇的级别清单。

    为什么不当成 import 期依赖：它是唯一事实来源，拿不到时界面要给一句
    人话，而不是让面板整个起不来。
    """
    try:
        import kbviews as KV
    except ImportError as exc:  # pragma: no cover - 只会在装坏时发生
        raise RuntimeError(
            f"读不到 offline/kbviews.py（{exc}）。"
            f"面板的 sys.path 里应该有 offline/ —— 见 panels/common.py。"
        ) from exc
    return KV.level_rows(key)


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B" if n else "—"


class LevelPicker(tk.Toplevel):
    """选一篇文献的某个级别，然后打开那个 md。

    参数
        parent    父窗口
        key       Zotero 条目 key
        title     这篇的简述（窗口标题用，让人确认选对了）
        on_log    可选回调：写一行到面板日志
        ui_font / mono_font  与主面板保持一致的字体
    """

    COLUMNS = [
        # (列 id, 表头, 宽, 最小宽, 右对齐)
        # ⚠ 宽度是权衡过的：这一屏最关键的信息是"每一层里是什么"，
        #   所以「里面有什么」占大头；级别名短，够读就行。
        ("label", "级别", 118, 90, False),
        ("what", "里面有什么", 520, 220, False),
        ("size", "大小", 72, 56, True),
        ("state", "状态", 96, 72, False),
    ]

    def __init__(self, parent, key, title, on_log=None,
                 ui_font="TkDefaultFont", mono_font="TkFixedFont"):
        super().__init__(parent)
        self.key = key
        self.title_text = title or key
        self.on_log = on_log
        self.ui_font = ui_font
        self.mono_font = mono_font
        self.rows: list[dict] = []

        self.title(f"打开知识库 · {self.title_text}  [{key}]")
        self.geometry("920x420")
        self.minsize(640, 280)
        self.transient(parent)
        try:
            parent.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - 920) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - 420) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        # 置顶：从主面板点开的窗口被主窗口挡住时，用户会以为"没反应"
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass

        self._build()
        self.reload()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._open_selected())
        self.tree.focus_set()

    # ------------------------------------------------------------ 界面

    def _build(self):
        info = ttk.Frame(self, padding=(12, 10, 12, 2))
        info.pack(fill="x")
        ttk.Label(info, textvariable=tk.StringVar(value=self.title_text),
                  font=(self.ui_font, 10, "bold")).pack(side="left")
        ttk.Label(info, foreground="#888",
                  text="　同一篇按「要读多深」分了几层，双击就打开对应文件").pack(
            side="left")

        wrap = ttk.Frame(self, padding=(12, 4, 12, 4))
        wrap.pack(fill="both", expand=True)
        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="browse")
        for cid, label, width, minw, right in self.COLUMNS:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, minwidth=minw,
                             anchor="e" if right else "w",
                             stretch=(cid == "what"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._open_selected())

        bottom = ttk.Frame(self, padding=(12, 2, 12, 10))
        bottom.pack(fill="x")
        self.status = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.status, foreground="#666").pack(
            side="left")
        ttk.Button(bottom, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="打开", command=self._open_selected).pack(
            side="right", padx=6)
        ttk.Button(bottom, text="重新生成", command=self.regen).pack(
            side="right", padx=6)
        ttk.Button(bottom, text="在文件管理器里显示",
                   command=self._reveal_selected).pack(side="right", padx=6)

    # ------------------------------------------------------------ 数据

    def reload(self):
        try:
            self.rows = level_rows(self.key)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读不到知识库分级", str(exc), parent=self)
            self.rows = []
        self.tree.delete(*self.tree.get_children())
        for r in self.rows:
            state = "已有" if r["exists"] else "还没生成"
            self.tree.insert("", "end", iid=r["id"],
                             values=(r["label"], r["what"],
                                     _human_size(r["size"]), state))
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])
        n_ok = sum(1 for r in self.rows if r["exists"])
        self.status.set(f"{n_ok}/{len(self.rows)} 层已生成"
                        f"　（文件：{os.path.dirname(self.rows[0]['path'])}）"
                        if self.rows else "没有级别可显示")

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return next((r for r in self.rows if r["id"] == sel[0]), None)

    # ------------------------------------------------------------ 动作

    def _open_selected(self):
        row = self._selected()
        if not row:
            return
        path = row["path"]
        if not os.path.exists(path):
            # 还没生成就先自己生成一次，再打开 —— 让"打开"永远是能用的动作，
            # 不要逼用户先去别处点一个"补齐"按钮（那正是原来说"很费劲"的地方）。
            if not self.regen(quiet=True):
                return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "打不开这个文件",
                f"{path}\n\n{type(exc).__name__}: {exc}\n\n"
                f"（这类 md 用系统默认的 Markdown 阅读器打开；"
                f"没装的话可以先点「在文件管理器里显示」）",
                parent=self)
            return
        self._log(f"打开知识库「{row['label']}」：{path}")

    def _reveal_selected(self):
        row = self._selected()
        if not row:
            return
        path = row["path"]
        if not os.path.exists(path):
            messagebox.showinfo("还没有这份文件",
                                f"「{row['label']}」还没生成。\n"
                                f"先点「重新生成」。", parent=self)
            return
        try:
            # ⚠ explorer 的 /select 要求路径**紧跟**在 `/select,` 后面，
            #   所以这里拆成两个 argv（Python 会为含空格的那个自动加引号），
            #   这是本机实测能选中文件的形式。
            PR.spawn(["explorer", "/select,", os.path.normpath(path)])
        except Exception:  # noqa: BLE001
            # 退一步：至少把所在目录打开（用户自己一眼就能看到那个文件）
            try:
                os.startfile(os.path.dirname(path))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("打不开文件管理器", str(exc), parent=self)

    def regen(self, quiet: bool = False) -> bool:
        """重新生成这篇的生成型级别（tldr / figures / weight）。"""
        try:
            import kbviews as KV
        except ImportError as exc:
            messagebox.showerror("读不到 kbviews", str(exc), parent=self)
            return False
        try:
            self.configure(cursor="watch")
            self.update_idletasks()
            written = KV.write_levels(self.key)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("重新生成失败",
                                 f"{type(exc).__name__}: {exc}", parent=self)
            return False
        finally:
            try:
                self.configure(cursor="")
            except tk.TclError:
                pass
        self.reload()
        self._log(f"重新生成分级视图：{len(written)} 份（{self.key}）")
        if not quiet:
            self.status.set(f"已重新生成 {len(written)} 份")
        return True

    def _log(self, text: str):
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass


def open_level_picker(parent, key, title, **kw) -> LevelPicker:
    """薄封装：调用方少 import 一个类名。"""
    return LevelPicker(parent, key, title, **kw)
