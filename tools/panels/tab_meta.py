"""tab_meta.py —— 「元数据」页：一键补全、类型/标题污染

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk


class MetaTab:
    """「元数据」页：一键补全、类型/标题污染。"""


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
