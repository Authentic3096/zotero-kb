"""para_review.py —— 「逐段检查进度 / 正文修正」复核对话框。

## 为什么值得单开一个窗

窗格里那个「全文级段落检测」是**可以中断、可以续跑**的（用户明确要求），
于是会产生两类需要"事后看和管"的数据：

    para_check     哪几段查过、结论是什么（进度）
    fulltext_patch 确认过的正文修正（改了什么、锚点是什么）

没有这一页，用户就只能"相信它"：改错了没法撤、进度乱了没法清。

## 两个动作都是**可逆、可核对**的

    · 修正 →「撤销」= 把 status 设成 rejected（不删行，留着记录）
    · 进度 →「清空这一篇」= 删掉 para_check 行，下次从头查
      （**只删进度，不动 fulltext_patch** —— 那是用户确认过的劳动成果）
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from .common import ROOT, ts


def _db():
    import sys
    if os.path.join(ROOT, "offline") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "offline"))
    import schemas as S
    return S.connect(S.INDEX_DB)


class ParaReview(tk.Toplevel):
    """某一篇的逐段进度与正文修正。"""

    def __init__(self, master, app, key: str, on_done=None):
        super().__init__(master)
        self.app = app
        self.key = key
        self.on_done = on_done
        self.patch_ids = []
        self.title(f"逐段进度与正文修正 · {key}")
        self.geometry("900x520")
        self.transient(master)
        self._build()
        self.reload()

    def _build(self):
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        self.summary = ttk.Label(top, text="正在读取…", justify="left",
                                 wraplength=860)
        self.summary.pack(side="left")
        ttk.Button(self, text="清空这一篇的检查进度（下次从头查）",
                   command=self.do_clear_progress).pack(anchor="w", padx=10)

        mid = ttk.Frame(self, padding=(10, 6, 10, 0))
        mid.pack(fill="both", expand=True)
        cols = ("id", "kind", "page", "status", "anchor")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        for col, width, label in (("id", 50, "id"), ("kind", 90, "类型"),
                                  ("page", 50, "页"), ("status", 80, "状态"),
                                  ("anchor", 600, "被替换/定位的原文片段")):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=(col == "anchor"))
        self.tree.pack(fill="both", expand=True)

        bar = ttk.Frame(self, padding=(10, 6, 10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="撤销选中的修正", command=self.do_reject).pack(side="left")
        ttk.Button(bar, text="恢复选中的修正", command=self.do_restore).pack(side="left",
                                                                           padx=6)
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")

    # ---------------------------------------------------------------- 读

    def reload(self):
        self.tree.delete(*self.tree.get_children())
        self.patch_ids = []
        try:
            conn = _db()
        except Exception as exc:      # noqa: BLE001
            self.summary.config(text=f"读不到库：{exc}")
            return
        try:
            try:
                rows = list(conn.execute(
                    "SELECT status, COUNT(*) n FROM para_check WHERE item_key=? "
                    "GROUP BY status", (self.key,)))
            except Exception:      # noqa: BLE001
                rows = []
            if rows:
                cnt = {str(r["status"]): int(r["n"]) for r in rows}
                total = sum(cnt.values())
                self.summary.config(
                    text=f"逐段进度：共 {total} 段有记录"
                         + "　".join(f"{k}={v}" for k, v in sorted(cnt.items()))
                         + "\n（stale = 正文重建后指纹对不上，需要重查；"
                           "fixed = 这一段有确认过的修正）")
            else:
                self.summary.config(text="逐段进度：这一篇还没有检查记录"
                                        "（在 Zotero 右侧「本地模型」窗格里"
                                        "点「全文级段落检测」开始）")
            try:
                for r in conn.execute(
                        "SELECT patch_id, kind, page, status, anchor, source,"
                        " note FROM fulltext_patch WHERE item_key=? "
                        "ORDER BY patch_id DESC", (self.key,)):
                    anchor = str(r["anchor"] or "")
                    self.tree.insert("", "end", values=(
                        r["patch_id"], r["kind"], r["page"] or "", r["status"],
                        anchor[:200]))
                    self.patch_ids.append(int(r["patch_id"]))
            except Exception:      # noqa: BLE001
                pass
        finally:
            conn.close()

    # ---------------------------------------------------------------- 写

    def _selected_patch_ids(self):
        out = []
        for item in self.tree.selection():
            vals = self.tree.item(item, "values")
            if vals:
                try:
                    out.append(int(vals[0]))
                except (TypeError, ValueError):
                    pass
        return out

    def do_reject(self):
        """撤销 = 把 status 设成 rejected（**不删行**，留着记录）。"""
        ids = self._selected_patch_ids()
        if not ids:
            messagebox.showinfo("没选", "先在上面选中要撤销的修正。", parent=self)
            return
        if not messagebox.askyesno("撤销修正",
                                   f"把选中的 {len(ids)} 条修正标成「已撤销」？\n"
                                   "下次重建正文时就不会应用它们了（记录仍保留）。",
                                   parent=self):
            return
        self._set_status(ids, "rejected")

    def do_restore(self):
        ids = self._selected_patch_ids()
        if not ids:
            messagebox.showinfo("没选", "先选中要恢复的修正。", parent=self)
            return
        self._set_status(ids, "applied")

    def _set_status(self, ids, status):
        try:
            conn = _db()
            try:
                for pid in ids:
                    conn.execute("UPDATE fulltext_patch SET status=? WHERE patch_id=?",
                                 (status, pid))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("改不了", str(exc), parent=self)
            return
        self.app.say(f"[{ts()}] {self.key}：{len(ids)} 条修正 → {status}"
                     f"（重建正文时生效）")
        self.reload()

    def do_clear_progress(self):
        if not messagebox.askyesno(
                "清空检查进度",
                "删掉这一篇的逐段检查进度（para_check）？\n"
                "「正文修正不会被删」—— 那是你确认过的成果。\n"
                "清空后下次会从第一段重新查。", parent=self):
            return
        try:
            conn = _db()
            try:
                conn.execute("DELETE FROM para_check WHERE item_key=?", (self.key,))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("清不掉", str(exc), parent=self)
            return
        self.app.say(f"[{ts()}] 已清空 {self.key} 的逐段检查进度"
                     f"（正文修正保留）")
        self.reload()
