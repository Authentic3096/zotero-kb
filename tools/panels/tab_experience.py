"""tab_experience.py —— 「经验库」页：用过什么方法、效果如何、权重

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .common import (
    CREATE_NO_WINDOW,
    ENV,
    PY,
    ROOT,
    ts,
)


class ExperienceTab:
    """「经验库」页：用过什么方法、效果如何、权重。"""


    def _build_experience_tab(self):
        """经验库：知识库"学到了什么"。**一个列表 + 一个详情**，不再有两套列表。

        ⚠ 原来的界面有两个"显示经验"的地方：体检用的 `Listbox`（能选中删除）和
          底部那个大文本框（列出全部的结果，**只能看不能选**）。用户反馈：
          「上下两个显示经验的窗口……列出全部不在上面的窗口没法选中并筛去，
          两个是不是冗余了」。现在合成一个 Treeview —— 查询 / 列出全部 /
          待确认 / 体检**共用它**，选中哪一条，下面详情就显示哪一条。
        """
        f = self.tab_exp
        bar = ttk.Frame(f, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="关键词：").pack(side="left")
        self.exp_query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.exp_query, width=28)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self.do_exp_list())
        for text, cmd, tip in (
            ("查询", self.do_exp_list, "按关键词找相关经验"),
            ("列出全部", self.do_exp_all, "库里所有经验（选中一条可在下面看全文、可删）"),
            ("经验体检", self.do_exp_checkup,
             "把「没有关联文献 / 像是工具链记录」的标出来（「同一个列表」里显示原因，不自动删）"),
            ("待确认清单", self.do_pending, "小模型从对话里抽出来、还没确认的"),
            ("修改/增添经验…", self.do_exp_edit, "手记一条，或改已有的一条（有本地模型可以口述）"),
            ("选择会话…", self.do_pick_sessions, "「手动选」要扫的对话（不再默认扫全部）"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=3)
            self._tip(b, tip)

        ttk.Label(
            f, foreground="#888", wraplength=980, justify="left",
            text="经验是知识库越用越准的唯一机制：用某篇的方法做过尝试后，"
                 "记录「有效 / 无效 / 部分有效」，下次检索会自动把验证过的文献排前。"
                 "本地模型用提示词限制只记录「与文献内容或研究方法有关」的尝试。"
        ).pack(fill="x", padx=12, pady=(0, 2))

        # ---- 列表（所有视图**共用这一个**）
        mid = ttk.Frame(f, padding=(10, 2, 10, 0))
        mid.pack(fill="both", expand=True)
        cols = ("id", "date", "outcome", "source", "items", "asked", "suspect")
        # ⚠ 高度 9 行而不是 11：列表下面还有一行操作按钮（改选中/删除/采纳/丢弃），
        #   给 11 行时默认窗口高度下那一行正好被挤出可视区（要不滚一下才能点到）。
        #   条目多了不影响 —— 列表自己带滚动条，按钮位置是固定的。
        self.exp_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                     height=9, selectmode="extended")
        for col, width, label, stretch in (
            ("id", 54, "id", False), ("date", 84, "日期", False),
            ("outcome", 66, "效果", False), ("source", 62, "来源", False),
            ("items", 44, "文献", False), ("asked", 420, "问题 / 摘要", True),
            ("suspect", 190, "体检", False),
        ):
            self.exp_tree.heading(col, text=label)
            self.exp_tree.column(col, width=width, stretch=stretch)
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.exp_tree.yview)
        self.exp_tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.exp_tree.pack(side="left", fill="both", expand=True)
        self.exp_tree.bind("<<TreeviewSelect>>", lambda _e: self._exp_show_picked())
        self.exp_rows = {}          # tree item id -> 行数据

        # ---- 操作行
        act = ttk.Frame(f, padding=(10, 4, 10, 2))
        act.pack(fill="x")
        self.exp_edit_btn = ttk.Button(act, text="改选中这条", command=self.do_exp_edit_selected)
        self.exp_edit_btn.pack(side="left")
        self.exp_del_btn = ttk.Button(act, text="删除选中（会回滚权重）",
                                      command=self.do_exp_delete)
        self.exp_del_btn.pack(side="left", padx=6)
        self.exp_approve_btn = ttk.Button(act, text="采纳入库（待确认）",
                                          command=self.do_exp_approve)
        self.exp_approve_btn.pack(side="left", padx=6)
        self.exp_reject_btn = ttk.Button(act, text="丢弃（待确认）",
                                         command=self.do_exp_reject)
        self.exp_reject_btn.pack(side="left", padx=6)
        # 编号说明：用户直接问过「删除了应该更新」/「编号应该自动更新、从 1 开始」
        # —— 这里是答案，写在界面上（实现见 offline/experience.py: renumber）
        ttk.Label(act, foreground="#888",
                  text="id 是库里的编号：删除后会自动重排，始终从 1 开始连续"
                 ).pack(side="left", padx=10)

        # ---- 详情（选中那一条的全文；列表负责导航，这里负责内容）
        # ⚠ 放在页签内部的下面那一格（分隔线可拖）：详情长短差很多，
        #   固定在 11 行时长的看不全、短的浪费空间。
        # ⚠ 高度取 8：这一格的**请求高度**会跟上面（列表+操作行）抢空间 ——
        #   给 11 行时上面那行操作按钮会被挤出可视区（实测：内容需要 364、
        #   画布只给 345，正好卡掉那一行）。想临时看长详情把分隔线往下拖即可。
        self.exp_text = scrolledtext.ScrolledText(self.exp_out, height=8,
                                                  wrap="word",
                                                  font=(self.mono_font, 10))
        self.exp_text.pack(fill="both", expand=True, padx=6, pady=4)

        self.exp_view = ("all", "")     # 记住当前视图，删完刷新的是**同一个**视图
        self._exp_load("all", "")


    # ================================================================ 经验库

    def do_exp_list(self):
        q = self.exp_query.get().strip()
        if not q:
            return self.do_exp_all()
        self._exp_load("query", q)


    def do_exp_all(self):
        self._exp_load("all", "")


    def do_pending(self):
        self._exp_load("pending", "")


    def _exp_load(self, mode: str, q: str):
        """把某个视图的数据读出来，填进**同一个**列表。

        ⚠ 读库在后台线程（`Searcher` 查询可能慢），但**填 Treeview 必须回主线程**
          （Tk 控件只能在主线程改）—— 所以走队列的 `call`。
        """
        self.exp_view = (mode, q)

        def work():
            try:
                import schemas as S
                rows = []
                if mode == "pending":
                    path = os.path.join(S.INBOX_DIR, "pending.jsonl")
                    if os.path.exists(path):
                        import json
                        for line in open(path, encoding="utf-8"):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                row = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if row.get("status") == "pending":
                                rows.append({"pending": True, **row})
                elif mode == "checkup":
                    rows = self._exp_checkup_rows()
                else:
                    from searcher import Searcher
                    s = Searcher()
                    try:
                        if mode == "query":
                            rows = [dict(r) for r in s.related_experience(q, limit=60)]
                        else:
                            rows = [dict(r) for r in s.read(
                                "SELECT * FROM experience ORDER BY id")]
                    finally:
                        s.close()
                self.out_queue.put(("call", (self._exp_fill, (mode, q, rows))))
            except Exception as exc:      # noqa: BLE001
                import traceback
                self.out_queue.put(("call", (
                    lambda _a: self.say(f"[{ts()}] 读取经验失败："
                                        f"{type(exc).__name__}: {exc}\n"
                                        + traceback.format_exc()[-400:]),
                    None)))

        threading.Thread(target=work, daemon=True, name="kb-exp-load").start()
        self.say(f"[{ts()}] 读取经验（{mode}）")


    def _exp_fill(self, payload):
        """在主线程里填列表与详情（队列 `call` 的回调）。"""
        mode, q, rows = payload
        import sys
        if os.path.join(ROOT, "offline") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "offline"))
        import experience as EXP

        tree = getattr(self, "exp_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self.exp_rows = {}
        label = {"effective": "有效", "ineffective": "无效",
                 "partial": "部分", "unknown": "未验证"}
        for r in rows:
            if r.get("pending"):
                iid = tree.insert("", "end", values=(
                    str(r.get("id"))[:16], str(r.get("when") or "")[:10],
                    label.get(str(r.get("outcome")), str(r.get("outcome"))),
                    "小模型", len(r.get("items") or []),
                    str(r.get("asked") or "")[:120],
                    str(r.get("suspect") or "")))
                self.exp_rows[iid] = r
                continue
            keys = EXP.loads(r.get("item_keys"), [])
            iid = tree.insert("", "end", values=(
                r.get("id"), str(r.get("created_at") or "")[:10],
                label.get(str(r.get("outcome")), str(r.get("outcome"))),
                str(r.get("source") or ""), len(keys),
                str(r.get("asked") or "")[:120],
                (r.get("_why") or "") if isinstance(r.get("_why"), str)
                else "；".join(r.get("_why") or [])))
            self.exp_rows[iid] = r
        total = len(rows)
        if mode == "checkup":
            self.say(f"[{ts()}] 经验体检：{total} 条可疑（共 {self._exp_total()} 条经验）。"
                     f"只列不自动删 —— 在「同一个列表」里勾选（可多选）再点「删除选中」。")
        else:
            self.say(f"[{ts()}] 经验（{mode}）：{total} 条")
        if rows:
            first = tree.get_children()[0]
            tree.selection_set(first)
            tree.focus(first)
        else:
            self.exp_text.delete("1.0", "end")
            self.exp_text.insert("1.0", {
                "pending": "待确认清单是空的。\n\n先点「选择会话…」挑要扫的对话，"
                           "扫完抽出来的条目会出现在这里。",
                "checkup": "体检没发现可疑条目 —— 库里没有「没有关联文献」或"
                           "「像是工具链记录」的经验。",
            }.get(mode, "这个视图里没有条目。"))


    def _exp_show_picked(self):
        """把选中的第一条的全文写进详情区（列表导航 + 详情内容，各司其职）。"""
        tree = getattr(self, "exp_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        row = self.exp_rows.get(sel[0]) if sel else None
        self.exp_text.delete("1.0", "end")
        if not row:
            return
        if row.get("pending"):
            text = (f"[待确认] {row.get('id')}　置信 {row.get('confidence', 0)}\n"
                    f"来源会话：{row.get('session')}　{row.get('when')}\n"
                    f"效果：{row.get('outcome')}\n\n"
                    f"问题：{row.get('asked')}\n\n方法：{row.get('method')}\n\n"
                    f"原因：{row.get('reason')}\n\n前提：{row.get('context')}\n\n"
                    f"依据：{row.get('evidence')}\n\n"
                    f"关联文献：{row.get('items') or row.get('item_refs')}\n"
                    + (f"\n⚠ {row['suspect']}\n" if row.get("suspect") else "")
                    + "\n（确认可用就点「采纳入库」；明显错就点「丢弃」）")
        else:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import experience as EXP
            keys = EXP.loads(row.get("item_keys"), [])
            tags = EXP.loads(row.get("tags"), [])
            text = (f"#{row.get('id')}　{row.get('created_at')}\n"
                    f"来源：{row.get('source')}　会话：{row.get('session') or '(无)'}\n"
                    f"效果：{row.get('outcome')}"
                    + (f"　（改过 {row.get('updated_at')}）" if row.get("updated_at") else "")
                    + f"\n\n问题：{row.get('asked')}\n\n方法：{row.get('method')}\n\n"
                    f"原因：{row.get('reason')}\n\n前提：{row.get('context')}\n\n"
                    f"依据：{row.get('evidence')}\n\n"
                    f"标签：{', '.join(tags)}\n关联文献：{', '.join(keys) or '（无）'}\n")
            if row.get("_why"):
                text += f"\n⚠ 体检认为可疑：{row['_why']}\n"
            if not keys:
                text += ("\n⚠ 这条没有关联文献：不会被检索加权，"
                         "也不会出现在任何一篇的档案里。\n")
        self.exp_text.insert("1.0", text)


    def _selected_exp_ids(self) -> list:
        tree = getattr(self, "exp_tree", None)
        if tree is None:
            return []
        out = []
        for iid in tree.selection():
            row = self.exp_rows.get(iid) or {}
            if row.get("pending"):
                continue
            if row.get("id") is not None:
                out.append(int(row["id"]))
        return out


    def _selected_pending_ids(self) -> list:
        tree = getattr(self, "exp_tree", None)
        if tree is None:
            return []
        out = []
        for iid in tree.selection():
            row = self.exp_rows.get(iid) or {}
            if row.get("pending") and row.get("id"):
                out.append(str(row["id"]))
        return out


    def do_exp_edit_selected(self):
        """改**选中的这一条**（没选就按关键词框里的数字来，再没有就新增）。"""
        ids = self._selected_exp_ids()
        if ids:
            from .exp_editor import ExperienceEditor
            editor = ExperienceEditor(self.root, self, exp_id=ids[0],
                                      on_done=lambda: self._exp_refresh())
            editor.focus_set()
            return
        self.do_exp_edit()


    def do_learn(self):
        self.run("从会话记录补经验（本地模型）",
                 [os.path.join(ROOT, "offline", "learn.py"), "scan"],
                 on_done=lambda: self._exp_refresh())


    # ================================================================ 编辑 / 体检 / 选会话

    def do_exp_edit(self):
        """弹「修改/增添经验」对话框。

        默认是**新增**；想改某一条就先在列表里选中它（点「改选中这条」）。
        """
        from .exp_editor import ExperienceEditor
        exp_id = 0
        raw = (getattr(self, "exp_query", None) and self.exp_query.get() or "").strip()
        if raw.isdigit():
            exp_id = int(raw)
        editor = ExperienceEditor(self.root, self, exp_id=exp_id,
                                  on_done=lambda: self._exp_refresh())
        editor.focus_set()


    def _exp_refresh(self):
        """按**当前视图**刷新（删完/改完看到的是同一批条目，而不是跳回全部）。"""
        mode, q = getattr(self, "exp_view", ("all", ""))
        self._exp_load(mode, q)


    # 体检的**判据在 offline/experience.py**（`checkup_reason` / `META_WORK_WORDS`）
    # —— 那是领域判断，面板与 CLI 共用一份，也才写得了测试（这里只负责取数）。

    def _exp_checkup_rows(self) -> list:
        """体检：给每条经验打一个"可疑原因"（**只标注，不自动删**）。"""
        import sys
        if os.path.join(ROOT, "offline") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "offline"))
        import experience as EXP
        import schemas as S
        conn = S.connect(S.INDEX_DB)
        out = []
        try:
            for r in conn.execute("SELECT * FROM experience ORDER BY id DESC"):
                row = dict(r)
                why = EXP.checkup_reason(row)
                if why:
                    row["_why"] = why
                    out.append(row)
        finally:
            conn.close()
        return out


    def do_exp_checkup(self):
        """体检 = 换一个**视图**（在同一个列表里显示可疑原因），不再另开一个窗口。"""
        self._exp_load("checkup", "")


    def _exp_total(self) -> int:
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import schemas as S
            conn = S.connect(S.INDEX_DB)
            try:
                return int(conn.execute("SELECT COUNT(*) n FROM experience")
                           .fetchone()["n"])
            finally:
                conn.close()
        except Exception:      # noqa: BLE001
            return 0


    def do_exp_delete(self):
        """删掉列表里勾选的条目：**同时回滚权重**（走 experience 层，同一事务）。

        ⚠ 只对"库里真实存在的经验"有效；待确认清单里的条目用「丢弃」。
        """
        ids = self._selected_exp_ids()
        if not ids:
            if self._selected_pending_ids():
                messagebox.showinfo("这是待确认清单",
                                    "待确认的条目还没入库，用「丢弃」或「采纳入库」。")
            else:
                messagebox.showinfo("没勾选", "先在列表里选中要删的条目（可多选，"
                                              "按住 Ctrl / Shift）。")
            return
        body = "\n".join(f"#{i}" for i in ids[:20])
        if not messagebox.askyesno(
                f"删掉这 {len(ids)} 条？",
                body + "\n\n删除会「同时把它给文献加过的权重减回去」"
                       "（不回滚的话会留下「没有经验却权重很高」的脏状态）。\n"
                       "⚠ 删完会把编号重排成从 1 开始的连续编号。"):
            return
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import experience as EXP
            import schemas as S
            w = EXP.ConnWriter(S.connect(S.INDEX_DB))
            # ⚠ 一次批量删：循环调用单条删除会边删边重排编号，删到别的行上
            gone = EXP.delete_experiences(w, ids)
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("删除失败", str(exc))
            return
        self.say(f"[{ts()}] 已删除 {len(gone)} 条经验（权重已回滚），"
                 f"列表已按当前视图刷新；编号已重排成 1..N 连续")
        self._exp_refresh()


    def do_exp_approve(self):
        """把待确认清单里勾选的条目**入库**（面板直接写，不用回 DSH 对话）。"""
        ids = self._selected_pending_ids()
        if not ids:
            messagebox.showinfo("没勾选", "先在列表里选中待确认的条目（来源列写着"
                                          "「小模型」的那些）。")
            return
        rows = [self.exp_rows[iid] for iid in self.exp_tree.selection()
                if (self.exp_rows.get(iid) or {}).get("pending")]
        body = "\n".join(f"· {str(r.get('asked'))[:70]}" for r in rows[:10])
        if not messagebox.askyesno("采纳并入库", body + "\n\n确认这些条目成立吗？"
                                   "入库会「计入关联文献的权重」。"):
            return
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import experience as EXP
            import schemas as S
            w = EXP.ConnWriter(S.connect(S.INDEX_DB))
            ok = 0
            for r in rows:
                try:
                    EXP.add_experience(
                        w, asked=str(r.get("asked") or ""),
                        outcome=str(r.get("outcome") or "unknown"),
                        method=str(r.get("method") or ""),
                        context=str(r.get("context") or ""),
                        reason=str(r.get("reason") or ""),
                        evidence=str(r.get("evidence") or ""),
                        item_keys=r.get("items") or [], source="llm",
                        session=str(r.get("session") or ""))
                    ok += 1
                except ValueError as exc:
                    self.say(f"[{ts()}] 跳过一条：{exc}")
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("入库失败", str(exc))
            return
        self._exp_mark(rows, approve=True)
        self.say(f"[{ts()}] 已采纳入库 {ok} 条（权重已按关联文献计入）")
        self._exp_refresh()


    def do_exp_reject(self):
        """丢弃待确认清单里勾选的条目（把它们标成 rejected，不再出现在清单里）。"""
        ids = self._selected_pending_ids()
        if not ids:
            messagebox.showinfo("没勾选", "先在列表里选中要丢弃的待确认条目。")
            return
        rows = [self.exp_rows[iid] for iid in self.exp_tree.selection()
                if (self.exp_rows.get(iid) or {}).get("pending")]
        if not messagebox.askyesno("丢弃", f"丢掉这 {len(rows)} 条待确认条目？"
                                          "（只是标记为已丢弃，不写经验库）"):
            return
        self._exp_mark(rows, approve=False)
        self.say(f"[{ts()}] 已丢弃 {len(rows)} 条待确认条目")
        self._exp_refresh()


    def _exp_mark(self, rows, approve: bool) -> None:
        """改 pending.jsonl 里的状态：直接改文件（learn.py 的 mark 就是干这个的）。

        ⚠ 为什么面板直接改而不是起子进程：一次可能要标好几条，
          而且这是个小 JSONL 文件、面板与 learn.py 不会同时写（用户点一次动一次）。
        """
        import json
        import sys
        if os.path.join(ROOT, "offline") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "offline"))
        import schemas as S
        path = os.path.join(S.INBOX_DIR, "pending.jsonl")
        if not os.path.exists(path):
            return
        want = {str(r.get("id")): ("approved" if approve else "rejected") for r in rows}
        lines = []
        for line in open(path, encoding="utf-8"):
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except json.JSONDecodeError:
                lines.append(s)
                continue
            if str(row.get("id")) in want:
                row["status"] = want[str(row.get("id"))]
            lines.append(json.dumps(row, ensure_ascii=False))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")


    def do_pick_sessions(self):
        """手动选会话 → 只扫勾中的（learn.py scan --session <id>）。"""
        from .exp_editor import SessionPicker

        def run_picked(sids):
            for sid in sids:
                self.run(f"补经验：只扫会话 {sid[:18]}",
                         [os.path.join(ROOT, "offline", "learn.py"), "scan",
                          "--session", sid],
                         on_done=lambda: self._exp_refresh())

        picker = SessionPicker(self.root, self, on_run=run_picked)
        picker.focus_set()


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
            "import experience as EXP\n"
            "from searcher import Searcher\n"
            "key = os.environ['KB_KEY']\n"
            "pin = os.environ['KB_PIN'] == '1'\n"
            "note = os.environ.get('KB_NOTE', '')\n"
            "s = Searcher()\n"
            "try:\n"
            "    if not s.get_item(key):\n"
            "        print(f'[XX] 知识库里没有 key={key} 的条目')\n"
            "        raise SystemExit(1)\n"
            "    # ⚠ 权重写入只有一份实现：offline/experience.py:set_weight\n"
            "    #   （这里原来内联了一段 upsert SQL —— 那是这份算术的第四份拷贝）\n"
            "    EXP.set_weight(s, key, pinned=pin,\n"
            "                   note=(note if note else None))\n"
            "    print(f'  {key} 现在权重 = {s.weight_of(key):.3f}'\n"
            "          f\"（{'重点' if pin else '非重点'}）\")\n"
            "finally:\n"
            "    s.close()\n"
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
