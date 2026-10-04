"""exp_editor.py —— 「修改/增添经验」对话框 与 「选择会话」对话框。

## 为什么经验编辑要单开一个对话框，而且分两条路

用户的诉求：**「现在经验用户难以直接修改和添加」**，而且明确说了两条路 ——
「如果有本地模型就弹窗用户描述，然后本地模型整理匹配更新，如果没有本地模型
再让用户直接修改」。所以这个对话框里：

    · 上面是**大白话输入框** + 「让模型整理」按钮（有模型时最省事）
    · 下面是**结构化表单**（没有模型时、或模型整理得不满意时直接改）

**表单任何时候都能用** —— 模型只是"帮你整理"，不是必经路径。否则模型一抽风
就记不了经验了。

## 三条纪律（都在这个文件里落地）

1. **权重算术只有一份**：写入一律走 `offline/experience.py`（`add_experience` /
   `update_experience`），它们会把"回滚旧权重 → 改行 → 应用新权重"放进**同一个
   事务**。面板**不写任何 SQL**（原来 do_weight 里那一段内联 SQL 就是这么来的
   第四份实现，本轮也收掉了）。
2. **改动先给人看**：「修改」保存前显示**字段级 diff**（旧 → 新）并要确认；
   「新增」显示整条内容并要确认。
3. **关联文献必须看得见**：模型猜的 key 可能错，而**权重会加到错的文献上**。
   所以文献列表显示标题、可增可删。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .common import ROOT, ts


def _writer():
    """面板侧的经验写入器。

    面板是**单线程**的，用普通连接包一层即可（`experience.ConnWriter`）。
    MCP 服务端那边必须用 Searcher（带锁）—— 见 offline/experience.py 的说明。
    """
    import sys
    if os.path.join(ROOT, "offline") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "offline"))
    import experience as EXP
    import schemas as S
    return EXP, EXP.ConnWriter(S.connect(S.INDEX_DB))


class ExperienceEditor(tk.Toplevel):
    """改一条 / 加一条经验。`exp_id=0` 表示新增。"""

    FIELDS = (
        ("asked", "问题/目标（必填）", 2),
        ("method", "用了什么方法", 2),
        ("reason", "为什么有效/失败", 3),
        ("context", "前提条件", 2),
        ("evidence", "证据（路径或数字）", 2),
        ("tags", "标签（逗号分隔）", 1),
    )

    def __init__(self, master, app, exp_id: int = 0, on_done=None):
        super().__init__(master)
        self.app = app
        self.exp_id = int(exp_id or 0)
        self.on_done = on_done
        self.vars = {name: tk.StringVar() for name, _l, _h in self.FIELDS}
        self.outcome = tk.StringVar(value="unknown")
        self.keys = []                      # 关联文献 key（可增删）
        self.title("修改经验" if self.exp_id else "增添经验")
        self.geometry("820x680")
        self.transient(master)
        self._build()
        self._load()

    # ---------------------------------------------------------------- 界面

    def _build(self):
        pad = {"padx": 10, "pady": 3}

        box = ttk.LabelFrame(self, text="① 用大白话说（有本地模型时走这条）",
                             padding=8)
        box.pack(fill="x", **pad)
        self.plain = scrolledtext.ScrolledText(box, height=4, wrap="word",
                                               font=(self.app.mono_font, 10))
        self.plain.pack(fill="x")
        self.plain.insert("1.0", "例：我用位置-磁矩分离求解试了一篇的方法，"
                                 "无噪声下误差从 19.78% 降到几乎为 0，"
                                 "说明这类联合反演的困难主要在初值。")
        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(4, 0))
        b = ttk.Button(bar, text="让模型整理", command=self.do_draft)
        b.pack(side="left")
        self.app._tip(b, "把上面的描述交给本地模型整理成结构化的一条（不写库）")
        self.draft_status = ttk.Label(bar, foreground="#666", text="")
        self.draft_status.pack(side="left", padx=8)

        form = ttk.LabelFrame(self, text="② 结构化内容（模型填完你也可以改）",
                              padding=8)
        form.pack(fill="both", expand=True, **pad)
        row = ttk.Frame(form)
        row.pack(fill="x")
        ttk.Label(row, text="效果：").pack(side="left")
        self.outcome_combo = ttk.Combobox(row, textvariable=self.outcome,
                                          state="readonly", width=14,
                                          values=list(self._outcomes()))
        self.outcome_combo.pack(side="left")
        ttk.Label(row, text="　（effective 有效 / ineffective 无效 / "
                            "partial 部分有效 / unknown 未验证）",
                  foreground="#888").pack(side="left")
        for name, label, height in self.FIELDS:
            ttk.Label(form, text=label).pack(anchor="w", pady=(4, 0))
            if height <= 1:
                ttk.Entry(form, textvariable=self.vars[name]).pack(fill="x")
            else:
                w = tk.Text(form, height=height, wrap="word",
                            font=(self.app.mono_font, 10))
                w.pack(fill="x")
                self.vars[name + "__text"] = w      # 多行控件另存

        items = ttk.LabelFrame(form, text="③ 关联哪几篇文献（权重会记到它们身上）",
                               padding=6)
        items.pack(fill="x", pady=(6, 0))
        self.items_label = ttk.Label(items, text="（还没选）", foreground="#666",
                                     wraplength=740, justify="left")
        self.items_label.pack(anchor="w")
        ib = ttk.Frame(items)
        ib.pack(anchor="w", pady=(4, 0))
        ttk.Button(ib, text="选文献…", command=self.do_pick).pack(side="left")
        ttk.Button(ib, text="清空", command=self.do_clear_items).pack(side="left",
                                                                     padx=6)

        foot = ttk.Frame(self)
        foot.pack(fill="x", **pad)
        ttk.Button(foot, text="保存（会先给你看改动）",
                   command=self.do_save).pack(side="left")
        ttk.Button(foot, text="取消", command=self.destroy).pack(side="left", padx=8)
        self.hint = ttk.Label(foot, foreground="#666",
                              text="写入会**重算权重**（先回滚旧贡献、再按新内容计分）")
        self.hint.pack(side="left", padx=10)

    def _outcomes(self):
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import experience as EXP
            return EXP.OUTCOMES
        except Exception:      # noqa: BLE001
            return ("effective", "ineffective", "partial", "unknown")

    # ---------------------------------------------------------------- 读入

    def _load(self):
        if not self.exp_id:
            return
        try:
            EXP, w = _writer()
            row = EXP.get_experience(w, self.exp_id)
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("读不到这条经验", str(exc), parent=self)
            return
        if not row:
            messagebox.showwarning("没有这条", f"id={self.exp_id} 不存在", parent=self)
            return
        self.outcome.set(str(row.get("outcome") or "unknown"))
        for name, _l, height in self.FIELDS:
            val = str(row.get(name) or "")
            if height <= 1:
                self.vars[name].set(val)
            else:
                self.vars[name + "__text"].delete("1.0", "end")
                self.vars[name + "__text"].insert("1.0", val)
        self.keys = list(EXP.loads(row.get("item_keys"), []))
        self._paint_items()
        self.plain.delete("1.0", "end")
        self.plain.insert("1.0", "（修改已有的这一条：直接改下面的字段，"
                                 "或先在上面用大白话描述再点「让模型整理」）")
        self.title(f"修改经验 #{self.exp_id}")

    # ---------------------------------------------------------------- 动作

    def _collect(self) -> dict:
        data = {"outcome": self.outcome.get()}
        for name, _l, height in self.FIELDS:
            if height <= 1:
                data[name] = self.vars[name].get().strip()
            else:
                data[name] = self.vars[name + "__text"].get("1.0", "end").strip()
        data["item_keys"] = list(self.keys)
        return data

    def do_draft(self):
        """让本地模型把大白话整理成结构化的一条（**不写库**）。"""
        text = self.plain.get("1.0", "end").strip()
        if len(text) < 8:
            messagebox.showinfo("先写点东西", "在上面用大白话描述一下这条经验。",
                                parent=self)
            return
        self.draft_status.config(text="正在整理…（本地模型一次要几秒）")

        def work():
            try:
                import sys
                if os.path.join(ROOT, "offline") not in sys.path:
                    sys.path.insert(0, os.path.join(ROOT, "offline"))
                import kbchat
                import schemas as S
                conn = S.connect(S.INDEX_DB)
                try:
                    res = kbchat.draft_experience(text, conn=conn)
                finally:
                    conn.close()
            except Exception as exc:      # noqa: BLE001
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.app.out_queue.put(("call", (self._apply_draft, res)))

        threading.Thread(target=work, daemon=True).start()
        self.app.say(f"[{ts()}] 让模型整理这条经验")

    def _apply_draft(self, res):
        if not res or not res.get("ok"):
            self.draft_status.config(text="整理失败：" + str((res or {}).get("error")))
            return
        d = res.get("draft") or {}
        self.outcome.set(str(d.get("outcome") or "unknown"))
        for name, _l, height in self.FIELDS:
            if name == "tags":
                val = ", ".join(d.get("tags") or [])
            elif name == "asked":
                val = str(d.get("asked") or "")
            else:
                val = str(d.get(name) or "")
            if height <= 1:
                self.vars[name].set(val)
            else:
                self.vars[name + "__text"].delete("1.0", "end")
                self.vars[name + "__text"].insert("1.0", val)
        if d.get("item_keys"):
            self.keys = list(dict.fromkeys(list(self.keys) + list(d["item_keys"])))
        if res.get("items"):
            # ⚠ 用**标题**显示，不用 key：模型猜的 key 可能错，
            #   而关联错的文献会把权重加到别的论文上。
            self._titles = {i["key"]: i["title"] for i in res["items"]}
        self._paint_items()
        sim = res.get("similar") or []
        self.draft_status.config(
            text=("整理好了（草稿，**还没写库**）。"
                  + (f" 相似的已有经验：{', '.join('#' + str(s['id']) for s in sim)}"
                     if sim else "")))

    def _paint_items(self):
        if not self.keys:
            self.items_label.config(text="（还没选 —— 没有关联文献的经验不会被检索加权，"
                                         "也不会出现在任何一篇的档案里）")
            return
        titles = getattr(self, "_titles", {})
        parts = []
        for k in self.keys:
            parts.append(f"{k}（{titles.get(k, '')[:40]}）" if titles.get(k) else k)
        self.items_label.config(text="已选：" + "；".join(parts))

    def do_pick(self):
        """复用面板已有的 PaperPicker；**可以连着选几篇**。"""
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import schemas as S
            from paper_picker import PaperPicker
            conn = S.connect(S.INDEX_DB)
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT key, title, first_author, year FROM items "
                    "ORDER BY year DESC, key")]
            finally:
                conn.close()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("打不开文献列表", str(exc), parent=self)
            return

        def on_pick(key):
            if key and key not in self.keys:
                self.keys.append(key)
            self._paint_items()

        PaperPicker(self, rows, on_pick, ui_font=self.app.ui_font,
                    mono_font=self.app.mono_font,
                    title="选文献（再点一次「选文献…」可以继续加）")

    def do_clear_items(self):
        self.keys = []
        self._paint_items()

    def do_save(self):
        data = self._collect()
        try:
            EXP, w = _writer()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("读不到经验层", str(exc), parent=self)
            return
        err = EXP.validate(data.get("asked", ""), data.get("outcome", ""))
        if err:
            messagebox.showerror("还不能保存", err, parent=self)
            return
        tags = EXP.split_list(data.get("tags"))

        if not self.exp_id:
            # ---- 新增：把整条内容摆出来让用户确认
            body = (f"问题：{data['asked']}\n效果：{data['outcome']}\n"
                    f"方法：{data['method']}\n原因：{data['reason']}\n"
                    f"条件：{data['context']}\n证据：{data['evidence']}\n"
                    f"标签：{', '.join(tags)}\n"
                    f"关联文献：{', '.join(data['item_keys']) or '（无 —— 不会被加权）'}")
            if not messagebox.askyesno("确认新增这条经验？", body, parent=self):
                return
            try:
                eid = EXP.add_experience(
                    w, asked=data["asked"], outcome=data["outcome"],
                    method=data["method"], context=data["context"],
                    reason=data["reason"], evidence=data["evidence"],
                    tags=tags, item_keys=data["item_keys"], source="user")
            except Exception as exc:      # noqa: BLE001
                messagebox.showerror("写不进去", str(exc), parent=self)
                return
            self.app.say(f"[{ts()}] 已新增经验 #{eid}（权重已按关联文献重算）")
        else:
            # ---- 修改：**先给字段级 diff**（旧 → 新），再确认
            old = EXP.get_experience(w, self.exp_id)
            if not old:
                messagebox.showwarning("没有这条", "它可能已被删除", parent=self)
                return
            old_view = EXP.row_view(dict(old))
            new_view = {"asked": data["asked"], "outcome": data["outcome"],
                        "method": data["method"], "context": data["context"],
                        "reason": data["reason"], "evidence": data["evidence"],
                        "tags": tags, "item_keys": data["item_keys"]}
            diff = [f"{k}：\n  旧：{str(old_view.get(k))[:200]}\n"
                    f"  新：{str(new_view.get(k))[:200]}"
                    for k in new_view
                    if str(old_view.get(k)) != str(new_view.get(k))]
            body = ("\n\n".join(diff) if diff else "（内容没有变化）")
            if not messagebox.askyesno(
                    f"确认修改 #{self.exp_id}？（权重会按新旧内容重算）", body,
                    parent=self):
                return
            try:
                EXP.update_experience(w, self.exp_id, reason_suffix="面板编辑",
                                      **new_view)
            except Exception as exc:      # noqa: BLE001
                messagebox.showerror("改不了", str(exc), parent=self)
                return
            self.app.say(f"[{ts()}] 已修改经验 #{self.exp_id}"
                         f"（旧值留在 history 里，权重已重算）")
        if self.on_done:
            self.on_done()
        self.destroy()


class SessionPicker(tk.Toplevel):
    """挑 DSH 会话（**手动选**，不再默认扫全部）。

    用户的诉求：「本地模型从对话补经验，应该用户手动选对话」。
    每个会话显示：标题、日期、大小、**是否已经扫过**（读 learn.py 的位置文件）。
    """

    def __init__(self, master, app, on_run=None):
        super().__init__(master)
        self.app = app
        self.on_run = on_run
        self.rows = []
        self.vars = []
        self.title("选择会话（只扫你勾的）")
        self.geometry("860x560")
        self.transient(master)
        self._build()
        self._load()

    def _build(self):
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="勾选要扫的会话 —— 抽取只在这几个会话里做。",
                  foreground="#666").pack(side="left")
        b = ttk.Button(top, text="扫描勾选的会话", command=self.do_run)
        b.pack(side="right")
        self.status = ttk.Label(self, foreground="#666", text="")
        self.status.pack(fill="x", padx=12)
        self.listbox = ttk.Frame(self)
        self.listbox.pack(fill="both", expand=True, padx=10, pady=6)
        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=(0, 8))

    def _load(self):
        try:
            import sys
            if os.path.join(ROOT, "offline") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "offline"))
            import glob
            import json as _json
            import schemas as S
            sessions_root = S.DSH_SESSIONS
            files = glob.glob(os.path.join(sessions_root, "**",
                                           "session.v*.jsonl.zstd"), recursive=True)
            seen = {}
            for path in files:
                sid = os.path.basename(os.path.dirname(path))
                cur = seen.get(sid)
                if cur is None or os.path.getmtime(path) > cur[0]:
                    seen[sid] = (os.path.getmtime(path), path)
            # 扫过的位置（learn.py 的 positions 文件）
            scanned = {}
            try:
                from learn import load_positions
                scanned = load_positions()
            except Exception:      # noqa: BLE001
                scanned = {}
            cache = os.path.join(os.path.expanduser("~"), ".dsh", "storages",
                                 "session_projcache", "sessions")
            for sid, (mtime, path) in sorted(seen.items(), key=lambda x: -x[1][0]):
                title = ""
                try:
                    with open(os.path.join(cache, f"{sid}.json"),
                              encoding="utf-8") as fh:
                        rec = _json.load(fh)
                    title = str(((rec.get("record") or {}).get("rows") or {})
                                .get("title", {}).get("val") or "")
                except Exception:      # noqa: BLE001
                    title = ""
                import datetime
                when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                size = os.path.getsize(path) // 1024
                done = ("已扫到第 %s 条" % scanned.get(sid)) if sid in scanned else "没扫过"
                self.rows.append((sid, path))
                var = tk.BooleanVar(value=False)
                self.vars.append(var)
                cb = ttk.Checkbutton(
                    self.listbox,
                    text=f"{when}　{size:>5} KB　{done:<14}　"
                         f"{(title or sid)[:52]}",
                    variable=var)
                cb.pack(anchor="w")
            self.status.config(text=f"共 {len(self.rows)} 个会话"
                                    f"（默认一个都不勾 —— 扫全库是以前的行为，"
                                    f"它抽出来过与文献无关的工具链记录）")
        except Exception as exc:      # noqa: BLE001
            self.status.config(text=f"读不到会话列表：{type(exc).__name__}: {exc}")

    def do_run(self):
        picked = [self.rows[i][0] for i, v in enumerate(self.vars) if v.get()]
        if not picked:
            messagebox.showinfo("一个都没勾", "先勾上要扫的会话。", parent=self)
            return
        if self.on_run:
            self.on_run(picked)
        self.destroy()
