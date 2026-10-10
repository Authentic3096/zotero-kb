"""tab_outline.py —— 「分节纲要」页：全库纲要清单 + 生成/重做/补渲染/打开/删除

## 为什么要有这一页（用户 2026-10-10 的主诉求）

「分节纲要」原来是藏在「分类建议」页里一个单篇按钮（`do_ai_outline`）：
只能对**当前选中**的一篇动手，看不到"全库谁有纲要、谁没有、文件在不在"。
用户要的是像「分类建议」「元数据」那样的**管理入口** —— 一屏看清 94 篇的状态，
能批量补、能删、能打开。

## 判据以 meta 为准，不以文件为准（这是本轮修的一个显示错误）

纲要有两份落点：
  · `meta.ai_outline:<KEY>`  —— 结构化 JSON，**读端读的就是它**
    （`offline/kbviews.py::_render_outline`、MCP 资源、面板「打开知识库」）；
  · `views/<KEY>.outline.md` —— 给人读的渲染产物。

T0-8 那列当初按**文件**计数，于是本机 94 篇都有纲要（meta 94 行）却只显示 41 篇
（views 里只有 49 个 .outline.md）—— 46 篇明明有纲要却显示成 ✗。
本页和 `/col-status` 现在都按 meta 算，文件在不在另给一列。

## 与「分类建议」页那个单篇按钮的关系

`tab_ai.py` 的「生成纲要」按钮保留（选一篇 → digest.py <key>）；本页的
「生成 / 强制重做」走的是**同一个** `offline/digest.py`（不是第二份实现）。
"""

from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (
    ROOT,
    ts,
)


class OutlineTab:
    """「分节纲要」页：全库纲要清单 + 生成/强制/补渲染/打开/删除。"""

    def _build_outline_tab(self):
        f = self.tab_outline
        bar = ttk.Frame(f, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        for text, cmd, tip in (
            ("刷新列表", self.refresh_outline_list,
             "从索引里重新读纲要清单（很快，不调模型）"),
            ("生成", self.do_outline_build,
             "给选中的这篇生成纲要：已有的节按指纹复用，不会重复调模型"),
            ("强制重做", self.do_outline_rebuild,
             "忽略已有节，整篇重新问一遍模型（学位论文要几分钟）"),
            ("补渲染文件", self.do_outline_render_missing,
             "把有纲要、却没有 views 下 .outline.md 的条目补出来（复用分级视图渲染，不调模型）"),
            ("打开纲要", self.do_outline_open,
             "用默认程序打开人读的那份 .outline.md"),
            ("删除", self.do_outline_delete,
             "把这篇的纲要一起删掉（索引库 meta + views 文件），会二次确认"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._tip(b, tip)

        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=920,
            text=("分节纲要 = 全文与摘要之间的中间层：按章节给要点 + 页码范围。\n"
                  "「纲要状态」读的是索引库里的 meta（读端也读它）；"
                  "views 下的 .outline.md 只是给人读的渲染产物 —— "
                  "有纲要却没有文件不影响功能（点「补渲染文件」可补齐）。")
        ).pack(fill="x", padx=12, pady=(4, 8))

        self.outline_stats = tk.StringVar(value="还没读 —— 点「刷新列表」")
        ttk.Label(f, textvariable=self.outline_stats, justify="left",
                  font=(self.mono_font, 9)).pack(anchor="w", padx=12)

        mid = ttk.Frame(f, padding=(10, 6, 10, 4))
        mid.pack(fill="both", expand=True)
        cols = ("key", "title", "year", "state", "sections", "file")
        self.outline_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                         height=12)
        for c, txt, w, anc in (
            ("key", "条目", 84, "w"),
            ("title", "标题", 430, "w"),
            ("year", "年份", 56, "center"),
            ("state", "纲要状态", 120, "center"),
            ("sections", "节数", 56, "center"),
            ("file", "文件在不在", 110, "center"),
        ):
            self.outline_tree.heading(c, text=txt)
            self.outline_tree.column(c, width=w, anchor=anc)
        vs = ttk.Scrollbar(mid, orient="vertical",
                           command=self.outline_tree.yview)
        self.outline_tree.configure(yscrollcommand=vs.set)
        self.outline_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.outline_tree.bind("<Double-1>", lambda e: self.do_outline_open())

        self.outline_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.outline_note, foreground="#666",
                  font=(self.ui_font, 9), justify="left", wraplength=920
                  ).pack(fill="x", padx=12, pady=(4, 8))

        # 最近一次清单的原始行（按钮直接查它，不再重读）
        self.outline_rows: list[dict] = []
        self.refresh_outline_list()

    # ------------------------------------------------------------ 读清单

    def refresh_outline_list(self):
        """后台读纲要清单（items + meta + views 文件），结果回主线程填表。"""
        self.outline_note.set("正在读纲要清单…")

        def work():
            try:
                import digest as D
                import schemas as S

                counts: dict[str, int] = {}
                conn = S.connect(S.INDEX_DB)
                try:
                    for r in conn.execute("SELECT k, v FROM meta WHERE k LIKE ?",
                                          (D.META_PREFIX + "%",)):
                        k = str(r["k"])[len(D.META_PREFIX):]
                        try:
                            data = json.loads(r["v"] or "{}")
                        except Exception:      # noqa: BLE001
                            data = {}
                        counts[k] = (len(data.get("sections") or [])
                                     if isinstance(data, dict) else 0)
                    items = [(r["key"], r["title"] or "", r["year"])
                             for r in conn.execute(
                                 "SELECT key, title, year FROM items "
                                 "ORDER BY year DESC, key")]
                    files: set[str] = set()
                    try:
                        for fn in os.listdir(S.VIEWS_DIR):
                            if fn.endswith(".outline.md"):
                                files.add(fn[: -len(".outline.md")])
                    except OSError:
                        pass
                finally:
                    conn.close()

                rows: list[dict] = []
                seen: set[str] = set()
                for key, title, year in items:
                    n = int(counts.get(key, 0) or 0)
                    has_file = key in files
                    rows.append({"key": key, "title": title,
                                 "year": str(year or ""), "sections": n,
                                 "file": has_file,
                                 # 孤儿 = 有文件、meta 里却没纲要（删过 meta
                                 # 或留了旧文件）—— 报警，不静默删
                                 "orphan": bool(has_file and not n)})
                    seen.add(key)
                # 文件比 items 里还多出来的（条目已不在库）也列出来
                for key in sorted(files):
                    if key not in seen and not counts.get(key, 0):
                        rows.append({"key": key,
                                     "title": "（不在索引里的条目）",
                                     "year": "", "sections": 0, "file": True,
                                     "orphan": True})
                self.out_queue.put(("outline_rows", rows))
            except Exception as exc:      # noqa: BLE001
                self.out_queue.put(("log", f"[XX] 读纲要清单失败：{exc}"))

        threading.Thread(target=work, daemon=True,
                         name="kb-outline-list").start()

    def _apply_outline_rows(self, rows: list[dict]):
        """主线程：把清单填进表（Tk 控件只能主线程碰）。"""
        self.outline_rows = list(rows)
        try:
            self.outline_tree.delete(*self.outline_tree.get_children())
        except tk.TclError:
            return
        n_have = n_file = n_orph = 0
        for r in rows:
            n = int(r.get("sections") or 0)
            has_file = bool(r.get("file"))
            if n:
                n_have += 1
            if has_file:
                n_file += 1
            if r.get("orphan"):
                n_orph += 1
                state, filetxt = "孤儿（有文件无纲要）", "有文件"
            elif n:
                state, filetxt = "有", ("有" if has_file else "文件缺失")
            else:
                state, filetxt = "无", ("有文件（见孤儿）" if has_file else "—")
            try:
                self.outline_tree.insert("", "end", iid=r["key"], values=(
                    r["key"], str(r.get("title") or "")[:90],
                    r.get("year") or "", state, (n or ""), filetxt))
            except tk.TclError:
                pass
        self.outline_stats.set(
            f"清单 {len(rows)} 行 ｜ 有纲要 {n_have} 篇 ｜ 有渲染文件 {n_file} 份"
            + (f" ｜ ⚠ 孤儿 {n_orph} 个（有文件没纲要，见状态列）" if n_orph else ""))
        self.outline_note.set(
            "提示：「生成」只补没跑过的节（已有的直接复用）；"
            "「强制重做」会整篇重问模型；「补渲染文件」不调模型、秒级。")

    # ------------------------------------------------------------ 选中

    def _outline_selected(self) -> dict | None:
        sel = self.outline_tree.selection()
        if not sel:
            self.outline_note.set("先在列表里选一篇（双击也可以直接打开纲要）")
            return None
        key = self.outline_tree.item(sel[0], "values")[0]
        return next((r for r in self.outline_rows if r["key"] == key), None)

    # ------------------------------------------------------------ 生成

    def do_outline_build(self):
        self._outline_run(force=False)

    def do_outline_rebuild(self):
        if self._outline_selected() and not messagebox.askyesno(
                "强制重做",
                "会忽略已有纲要、整篇重新问一遍模型（一篇学位论文要几分钟，"
                "还要占着本地模型）。继续？"):
            return
        self._outline_run(force=True)

    def _outline_run(self, force: bool):
        row = self._outline_selected()
        if not row:
            return
        key = row["key"]
        args = [os.path.join(ROOT, "offline", "digest.py"), key]
        if force:
            args.append("--force")
        # ⚠ 与 tab_ai 的「生成纲要」是**同一个** offline/digest.py，不另写实现
        self.run(f"分节纲要{'强制重做' if force else '生成'} {key}", args,
                 on_done=self.refresh_outline_list)

    # ------------------------------------------------------------ 补渲染文件

    def do_outline_render_missing(self):
        missing = [r for r in self.outline_rows
                   if int(r.get("sections") or 0) > 0 and not r.get("file")]
        if not missing:
            messagebox.showinfo("不用补",
                                "所有有纲要的条目都已经有渲染文件了。")
            return
        if not messagebox.askyesno(
                "补渲染文件",
                f"有 {len(missing)} 篇有纲要、却没有 views 下的 .outline.md。\n\n"
                "会按索引库里的纲要重新渲染这些文件（复用分级视图的渲染，"
                "不调模型、秒级完成）。继续？"):
            return
        self.say(f"[{ts()}] 补渲染 {len(missing)} 篇的纲要文件…")

        def work():
            ok = 0
            bad: list[str] = []
            for i, r in enumerate(missing, 1):
                try:
                    # ⚠ 复用 kbviews 的渲染（别重写一份）：它就是「meta → 文件」
                    #   那条路，顺带把 tldr/权重等人读视图也补齐。
                    import kbviews as KV
                    KV.write_levels(r["key"])
                    ok += 1
                except Exception as exc:      # noqa: BLE001
                    bad.append(f"{r['key']}: {type(exc).__name__}: {exc}")
                if i % 10 == 0:
                    self.out_queue.put(("log", f"    补渲染 {i}/{len(missing)}"))
            self.out_queue.put(("log",
                f"[{ts()}] ✓ 补渲染完成：成功 {ok} / {len(missing)}"
                + (f"，失败 {len(bad)}：{bad[:3]}" if bad else "")))
            self.out_queue.put(
                ("call", (lambda _a=None: self.refresh_outline_list(), None)))

        threading.Thread(target=work, daemon=True,
                         name="kb-outline-render").start()

    # ------------------------------------------------------------ 打开

    def do_outline_open(self):
        row = self._outline_selected()
        if not row:
            return
        key = row["key"]
        import schemas as S
        path = os.path.join(S.VIEWS_DIR, f"{key}.outline.md")
        if not os.path.exists(path):
            if not int(row.get("sections") or 0):
                messagebox.showinfo("这篇还没有纲要",
                                    f"{key} 还没有分节纲要。先选中它点「生成」。")
                return
            if not messagebox.askyesno(
                    "还没有文件",
                    "这篇有纲要、但还没有人读的那份文件。\n"
                    "现在渲染出来并打开？"):
                return
            try:
                import kbviews as KV
                KV.write_levels(key)
            except Exception as exc:      # noqa: BLE001
                messagebox.showerror("渲染失败",
                                     f"{type(exc).__name__}: {exc}")
                return
            self.refresh_outline_list()
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            self.say(f"[{ts()}] 已打开：{path}")
        except Exception as exc:      # noqa: BLE001
            self.say(f"[XX] 打开失败：{exc}")

    # ------------------------------------------------------------ 删除

    def do_outline_delete(self):
        row = self._outline_selected()
        if not row:
            return
        key = row["key"]
        n = int(row.get("sections") or 0)
        if not messagebox.askyesno(
                "确认删除纲要",
                f"会删掉 {key} 的分节纲要：\n\n"
                f"  · 索引库 meta.ai_outline:{key}（{n} 节）\n"
                f"  · views\\{key}.outline.md 与它的 .html 孪生\n\n"
                "正文、切片、向量、经验都不动。删了要重新按节调模型才能回来。\n"
                "继续？"):
            return
        self.say(f"[{ts()}] 删除纲要 {key}…")

        def work():
            import schemas as S
            deleted: list[str] = []
            try:
                conn = S.connect(S.INDEX_DB)
                try:
                    cur = conn.execute("DELETE FROM meta WHERE k = ?",
                                       (f"ai_outline:{key}",))
                    conn.commit()
                    deleted.append(f"meta {cur.rowcount} 行")
                finally:
                    conn.close()
            except Exception as exc:      # noqa: BLE001
                self.out_queue.put(("log", f"[XX] 删 meta 失败：{exc}"))
            for suffix in (".outline.md", ".outline.html"):
                p = os.path.join(S.VIEWS_DIR, key + suffix)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        deleted.append(os.path.basename(p))
                except OSError as exc:
                    self.out_queue.put(("log", f"[!!] 删不掉 {p}：{exc}"))
            # 顺手把 papers/<KEY>.md 里那段「分节纲要」概览也撤掉（幂等）
            try:
                import digest as D
                D.sync_card(key)
            except Exception:      # noqa: BLE001
                pass
            self.out_queue.put(("log",
                f"[{ts()}] ✓ 已删除纲要 {key}："
                + ("、".join(deleted) or "（没找到东西）")))
            self.out_queue.put(
                ("call", (lambda _a=None: self.refresh_outline_list(), None)))

        threading.Thread(target=work, daemon=True,
                         name="kb-outline-del").start()
