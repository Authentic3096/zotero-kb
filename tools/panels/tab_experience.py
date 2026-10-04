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
            ("修改/增添经验…", self.do_exp_edit, "手记一条，或改已有的一条（有本地模型可以口述）"),
            ("经验体检", self.do_exp_checkup, "找出与文献无关/没有关联文献的条目"),
            ("待确认清单", self.do_pending, "从对话里抽出来、还没确认的经验"),
            ("选择会话…", self.do_pick_sessions, "**手动选**要扫的对话（不再默认扫全部）"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=3)
            self._tip(b, tip)

        # 经验体检的结果：列出可疑条目，勾选后删掉。
        # 为什么要这一块：经验库是**检索加权**的输入，混进来的工具链记录会让
        # 排序变脏（用户原话："我现在经验库的最后两条就是无关的"）。
        chk = ttk.Frame(f, padding=(10, 0, 10, 4))
        chk.pack(fill="x")
        self.exp_check_frame = chk
        self.exp_suspect_list = tk.Listbox(chk, height=4, selectmode="extended",
                                           font=(self.mono_font, 10))
        self.exp_suspect_list.pack(fill="x")
        delb = ttk.Frame(chk)
        delb.pack(fill="x", pady=(2, 0))
        b = ttk.Button(delb, text="删掉选中的（会回滚权重）", command=self.do_exp_delete)
        b.pack(side="left")
        self._tip(b, "删除经验并把它给文献加过的权重减回去（同一个事务里）")
        self._suspect_ids = []

        ttk.Label(
            f, foreground="#888", wraplength=900, justify="left",
            text="经验是知识库越用越准的唯一机制：用某篇的方法做过尝试后，"
                 "记录「有效 / 无效 / 部分有效」，下次检索会自动把验证过的文献排前。"
                 "⚠ 只记**与文献内容或研究方法有关**的尝试；"
                 "工程/工具链/配置类的踩坑请写进工作档案，"
                 "它们进经验库会污染检索排序。"
        ).pack(fill="x", padx=12, pady=(0, 4))

        self.exp_text = scrolledtext.ScrolledText(f, height=16, wrap="word",
                                                  font=(self.mono_font, 10))
        self.exp_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))


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


    # ================================================================ 编辑 / 体检 / 选会话

    def do_exp_edit(self):
        """弹「修改/增添经验」对话框。

        默认是**新增**；想改某一条可以在对话框里看到已有条目并选中，
        也可以从「列出全部」的输出里看到 id 后在下一行填。
        """
        from .exp_editor import ExperienceEditor
        exp_id = 0
        raw = (getattr(self, "exp_query", None) and self.exp_query.get() or "").strip()
        if raw.isdigit():
            exp_id = int(raw)
        editor = ExperienceEditor(self.root, self, exp_id=exp_id,
                                  on_done=lambda: self.do_exp_all())
        editor.focus_set()


    def do_exp_checkup(self):
        """经验体检：把"大概率与文献无关"的条目挑出来（**只列，不自动删**）。

        判据（可核对，不是模型说了算）：
          · `item_keys` 是空的 → 这条经验没有任何文献引用它，**不会参与加权**，
            也不会出现在任何一篇的档案里；
          · 命中工具链关键词（pdf2zh / BabelDOC / 插件 / 打包 / venv …）→
            这类"关于知识库自己怎么搭"的记录属于工作档案，不属于经验库。
        """
        tips = ("pdf2zh", "babeldoc", "插件", "打包", "venv", "xpi", "service-token",
                "localserver", "manifest", "调试", "排错", "部署")
        rows = []
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import experience as EXP
            import schemas as S
            conn = S.connect(S.INDEX_DB)
            try:
                for r in conn.execute(
                        "SELECT id, asked, outcome, item_keys, method FROM experience "
                        "ORDER BY id DESC"):
                    keys = EXP.loads(r["item_keys"], [])
                    text = f"{r['asked']} {r['method'] or ''}".lower()
                    why = []
                    if not keys:
                        why.append("没有关联文献（不参与加权）")
                    hit = [t for t in tips if t in text]
                    if hit:
                        why.append("像是工具链/工程记录：" + ", ".join(hit[:3]))
                    if why:
                        rows.append((int(r["id"]), str(r["asked"]), str(r["outcome"]),
                                     why))
            finally:
                conn.close()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("体检失败", str(exc))
            return
        self.exp_suspect_list.delete(0, "end")
        self._suspect_ids = []
        for eid, asked, outcome, why in rows:
            self.exp_suspect_list.insert(
                "end", f"#{eid} [{outcome}] {asked[:60]}　←　" + "；".join(why))
            self._suspect_ids.append(eid)
        self.say(f"[{ts()}] 经验体检：{len(rows)} 条可疑（共 "
                 f"{self._exp_total()} 条经验）。**只列不自动删** —— 删之前请看清单。")


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
        """删掉体检里勾选的条目：**同时回滚权重**（走 experience 层，同一事务）。"""
        sel = list(self.exp_suspect_list.curselection())
        if not sel:
            messagebox.showinfo("没勾选", "先在清单里选中要删的条目（可多选）。")
            return
        ids = [self._suspect_ids[i] for i in sel if i < len(self._suspect_ids)]
        body = "\n".join(self.exp_suspect_list.get(i) for i in sel)
        if not messagebox.askyesno(
                f"删掉这 {len(ids)} 条？",
                body + "\n\n删除会**同时把它给文献加过的权重减回去**"
                       "（不回滚的话会留下「没有经验却权重很高」的脏状态）。"):
            return
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import experience as EXP
            import schemas as S
            w = EXP.ConnWriter(S.connect(S.INDEX_DB))
            gone = [eid for eid in ids if EXP.delete_experience(w, eid)]
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("删除失败", str(exc))
            return
        self.say(f"[{ts()}] 已删除 {len(gone)} 条经验（权重已回滚）")
        self.do_exp_checkup()


    def do_pick_sessions(self):
        """手动选会话 → 只扫勾中的（learn.py scan --session <id>）。"""
        from .exp_editor import SessionPicker

        def run_picked(sids):
            for sid in sids:
                self.run(f"补经验：只扫会话 {sid[:18]}",
                         [os.path.join(ROOT, "offline", "learn.py"), "scan",
                          "--session", sid],
                         on_done=lambda: self._exp_query_run("pending", ""))

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
