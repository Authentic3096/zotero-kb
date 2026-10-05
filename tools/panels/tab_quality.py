"""tab_quality.py —— 「损坏查询」页：坏切片、图注表格

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。

⚠ 页名原叫「解析健康」，用户 2026-10-05 要求改成「损坏查询」
（"解析健康"是术语，用户一眼看不出这页是**查损坏的正文**的）。
改了页名之后，页内的标题、按钮、日志前缀都跟着改成同一套说法 ——
一个页面里两种叫法，用户会以为是两件事。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk


class QualityTab:
    """「损坏查询」页：坏切片、图注表格。"""


    # ---------------------------------------------------------- 运行环境


    # ================================================================ 损坏查询页

    def _build_quality_tab(self):
        """损坏查询页：查坏切片、修坏切片、看图注表格统计。

        为什么值得单独一页：PDF 提取失败**从界面上看不出来** ——
        检索命中了、片段也显示了，但内容是
        `HVHDUFK DQG 7HVW`（其实是 "Research and Test" 每个字母 -3）
        或 `ऍնླbັຩቔູ`（混进藏文码位）这种**整段乱码**。
        规则看不出来，得让模型判（实测单条 9/9 全对）。
        """
        f = self.tab_quality
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="损坏查询",
                  font=(self.ui_font, 11, "bold")).pack(side="left")
        ttk.Button(head, text="刷新统计",
                   command=self.refresh_quality).pack(side="right")

        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=900,
            text=("查 PDF 提取失败的切片（字符错乱 / 编码错乱 / 只剩符号）。"
                  "这类切片「能检索到但读不出东西」，还会污染向量。\n"
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
            btns, text="检查损坏切片", command=self.do_quality_check)
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
        # ⚠ 这里原来还有一个「逐段进度与正文修正」（para_review.py）。
        #   2026-10-05 用户要求把"内容窗格里的本地模型对话"整条链删掉：
        #   窗格、七个端点、kbchat/paras、以及 para_check / para_override /
        #   fulltext_patch 三张表一起没了 —— 逐段检测的**入口与产物都没有了**，
        #   所以这一页不再需要那个按钮。

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
                    (f"已检查 {checked} 篇：不可信 {bad} 篇"
                     + (f"，待人工确认 {unsure} 篇" if unsure else "")
                     if checked else
                     "还没检查过 —— 点「检查损坏切片」跑一次"),
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
            self.say(f"[损坏查询] 开始"
                     f"（{'规则 + 本地模型' if use_model else '仅规则'}）…")

            def prog(done, total, note):
                if done % 20 == 0 or done == total:
                    self.say(f"[损坏查询] {done}/{total}  {note[:60]}")
            r = CC.scan(use_model=use_model, progress=prog)
            self.say(f"[损坏查询] 完成：{r['total']} 篇，"
                     f"不可信 {r['bad']} 篇，待人工确认 {r.get('unsure', 0)} 篇，"
                     f"耗时 {r['elapsed']} 秒")
            if r.get("legacy_dropped"):
                self.say(f"[损坏查询] 已清理旧的 chunk_quality 表"
                         f"（{r['legacy_dropped']} 行误判数据）")
            if r.get("model_note"):
                self.say(f"[损坏查询] ⚠ {r['model_note']}")
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
            self._show_dialog(f"损坏查询明细 · {key}", "\n".join(lines))
        threading.Thread(target=work, daemon=True, name="kb-detail").start()
