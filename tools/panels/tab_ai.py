"""tab_ai.py —— 「分类建议」页：论文列表、检索、分类建议与写回

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .common import (
    ROOT,
    ts,
)


class AiTab:
    """「分类建议」页：论文列表、检索、分类建议与写回。"""


    def _build_ai_tab(self):
        """分类建议：调本地模型判断"这篇该归哪类"。

        与 Zotero 插件里的弹窗**同一份实现**（judge.classify_item），
        所以这里的结果和插件里点出来的一致。
        """
        f = self.tab_ai
        bar = ttk.Frame(f, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        for text, cmd, tip in (
            ("检查模型服务", self.do_ai_status, "看 Ollama/API 通不通、有哪些模型"),
            ("生成分类建议", lambda: self.do_taxonomy(True),
             "调模型给未分类的文献提建议"),
            ("只看分类分布", lambda: self.do_taxonomy(False), "不调模型，只统计"),
            ("处理待确认建议", self.do_pending, "看插件弹窗攒下的建议"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._tip(b, tip)

        bar2 = ttk.Frame(f, padding=(10, 0, 10, 4))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="选中的文献：").pack(side="left")
        # ⚠ 用户反馈："下拉列表太乱了，长长短短的标题和作者、分类等等，
        #   下拉列表可以单独做个弹窗出来，可以手动拉宽窄，包括每列的宽窄。"
        #   所以不再用 Combobox 把一整行塞进去，改成"只显示结果 + 按钮开表格"。
        self.paper_label = tk.StringVar(value="（还没选）")
        ttk.Label(bar2, textvariable=self.paper_label, foreground="#06c",
                  font=(self.ui_font, 10)).pack(side="left", padx=(2, 8))
        b = ttk.Button(bar2, text="选择文献…", command=self.open_paper_picker)
        b.pack(side="left")
        self._tip(b, "打开文献列表：列宽可拖、表头可排序、窗口可拉大")
        b2 = ttk.Button(bar2, text="刷新列表", command=self.refresh_paper_list)
        b2.pack(side="left", padx=6)
        self._tip(b2, "从知识库重新读文献列表")

        bar3 = ttk.Frame(f, padding=(10, 0, 10, 4))
        bar3.pack(fill="x")
        for text, cmd, tip in (
            ("生成要点", self.do_ai_summary, "给选中的这篇写摘要要点（几百字）"),
            ("生成纲要", self.do_ai_outline,
             "中间层：按章节给要点 + 页码范围（比摘要详细、比全文短）"),
            ("分类建议", self.do_taxonomy_one, "让模型判断该归哪类，可一键应用"),
            ("调整建议", self.do_taxonomy_talk,
             "跟模型对话：说一句哪里不对，让它重新判断"),
            ("应用建议", self.do_taxonomy_apply,
             "把模型建议的分类/标签写回 Zotero"),
        ):
            b = ttk.Button(bar3, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._tip(b, tip)

        ttk.Label(f, text="跟模型说一句（调整建议时用）",
                  font=(self.ui_font, 9, "bold")).pack(anchor="w", padx=14, pady=(6, 0))
        talk = ttk.Frame(f, padding=(10, 2, 10, 4))
        talk.pack(fill="x")
        self.talk_var = tk.StringVar()
        te = ttk.Entry(talk, textvariable=self.talk_var)
        te.pack(side="left", fill="x", expand=True)
        te.bind("<Return>", lambda _e: self.do_taxonomy_talk())
        ttk.Label(f, foreground="#888", justify="left", wraplength=900,
                  text="例如：「这篇应该归到分类 A」「这是设备类不是方法类」"
                       "「分类对，但标签换成 A、B」。点「调整建议」让模型按这句话重判。"
        ).pack(fill="x", padx=12, pady=(0, 4))

        # 上次的建议存这里，供「调整建议」「应用建议」用
        self.last_suggestion = None
        self.last_key = ""

        ttk.Label(
            f, foreground="#888", wraplength=900, justify="left",
            text="分类建议只在 Zotero 插件弹窗里由你确认后才生效 —— "
                 "这里生成的结果不会自动改你的 Zotero。"
                 "更快的入口：在 Zotero 里右键某篇 →「分类建议（本地模型）」。"
        ).pack(fill="x", padx=12, pady=(0, 4))

        self.ai_text = scrolledtext.ScrolledText(self.ai_out, height=12,
                                                 wrap="word",
                                                 font=(self.mono_font, 10))
        self.ai_text.pack(fill="both", expand=True, padx=6, pady=4)

        self.refresh_paper_list()


    def do_search(self):
        q = self.q_var.get().strip()
        if not q:
            messagebox.showinfo("缺参数", "请输入检索词。")
            return

        def work():
            try:
                from searcher import Searcher

                s = Searcher()
                res = s.search(q, limit=12)
                parts = [f"检索「{q}」→ {len(res['hits'])} 条"
                         f"（关键词候选 {res['diagnostics']['fts_candidates']}，"
                         f"向量 {res['diagnostics']['vector_candidates']}）\n" + "=" * 70]
                for i, h in enumerate(res["hits"], 1):
                    parts.append(
                        f"{i}. [{h['key']}] {h['title'][:66]}\n"
                        f"   {h['year']}｜{h['first_author']}｜"
                        f"{'/'.join(h['collections'])}｜权重 {h['weight']}")
                if not res["hits"]:
                    parts.append("没有命中。试试换同义词（中英各一次）。")
                s.close()
                self.out_queue.put(("show", (self.adv_text, "\n".join(parts))))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("show", (self.adv_text, f"出错了：{exc}")))

        threading.Thread(target=work, daemon=True).start()


    # ================================================================ 本地模型

    def do_ai_status(self):
        self.run("检查模型服务",
                 [os.path.join(ROOT, "offline", "judge.py"), "status"])


    def do_ai_tag(self, limit: int, write: bool):
        args = [os.path.join(ROOT, "offline", "judge.py"), "tag",
                "--limit", str(limit)]
        if write:
            args.append("--write")
        self.run(f"打标签（{limit} 篇{'，写入' if write else '，试跑'}）", args)


    # ---------------------------------------------------------- 选文献

    def refresh_paper_list(self):
        """把库里的文献读进来（给弹窗选择器用）。

        读的是**结构化字段**（作者/年份/标题/分类/全文字数/key），
        不是拼好的一整行 —— 拼成一行的坏处已在 paper_picker 里说明。
        """
        def work():
            try:
                import schemas as S
                conn = S.connect(S.INDEX_DB)
                rows = conn.execute(
                    "SELECT key, title, year, first_author, collections, "
                    "fulltext_chars FROM items "
                    "ORDER BY year DESC, first_author").fetchall()
                conn.close()
                out = []
                for r in rows:
                    try:
                        cols = "、".join(json.loads(r["collections"] or "[]"))
                    except Exception:  # noqa: BLE001
                        cols = ""
                    out.append((r["key"], r["first_author"] or "",
                                str(r["year"] or ""), r["title"] or "",
                                cols, r["fulltext_chars"] or 0))
                self.out_queue.put(("papers", out))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("log", f"[XX] 读文献列表失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()


    def _apply_paper_list(self, rows):
        """主线程：存下来（Tk 控件只能主线程碰）。"""
        self.paper_rows = rows
        n = len(rows)
        if not getattr(self, "last_key", ""):
            self.paper_label.set(f"（还没选，库里 {n} 篇）")
        self.ai_text.insert(
            "end", f"[{ts()}] 已载入 {n} 篇文献，点「选择文献…」挑一篇。\n")


    def open_paper_picker(self, on_pick=None):
        """打开独立的选择窗口（列宽可拖、表头可排序、窗口可拉大）。

        on_pick 不给时用默认的"_选中就设为当前文献"；给了就用调用方的
        （比如「手动调权重」那边要填到 key 输入框里）。
        """
        rows = getattr(self, "paper_rows", None)
        if rows is None:
            messagebox.showinfo("列表还没读好", "正在读文献列表，稍等一下再点。")
            return
        if not rows:
            messagebox.showinfo("库里还没有文献",
                                "索引里没有条目。先点「手动更新」。")
            return
        try:
            from paper_picker import PaperPicker
        except ImportError as exc:
            messagebox.showerror("缺文件", f"找不到 paper_picker.py：{exc}")
            return
        PaperPicker(self.root, rows, on_pick or self._on_paper_picked,
                    ui_font=self.ui_font, mono_font=self.mono_font,
                    title="选择文献（列宽可拖、表头可排序）")


    def _on_paper_picked(self, key):
        """弹窗里选中了一篇。"""
        self.last_key = key
        self.last_suggestion = None
        for r in getattr(self, "paper_rows", []):
            if r[0] == key:
                self.paper_label.set(
                    f"{r[1] or '（无作者）'} {r[2] or ''} · {r[3][:40]}  [{key}]")
                break
        self.ai_text.delete("1.0", "end")
        self.ai_text.insert("end", f"已选中：{key}\n\n")
        self.say(f"[{ts()}] 选中文献 {key}")


    def selected_key(self) -> str:
        """当前选中的 key（没有就返回空串）。"""
        return getattr(self, "last_key", "") or ""


    # ---------------------------------------------------------- 单篇操作

    def do_ai_summary(self):
        key = self.selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "请在上面的列表里选一篇文献"
                                          "（也可以直接把 key 粘进去）。")
            return
        self.run(f"生成要点 {key}",
                 [os.path.join(ROOT, "offline", "judge.py"), "summarize",
                  "--key", key])


    def do_ai_outline(self):
        """给选中的这篇生成**分节纲要**（中间层）。

        为什么值得单列一个按钮：摘要太短、全文太长（用户 2026-10-05 的原话），
        纲要是"先看这一层、再决定读哪几节"的入口。它按节调模型，一篇学位论文
        要 1~2 分钟，所以走 `self.run`（子进程 + 实时日志），界面不卡。
        """
        key = self.selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "请在上面的列表里选一篇文献"
                                          "（也可以直接把 key 粘进去）。")
            return
        self.run(f"生成分节纲要 {key}",
                 [os.path.join(ROOT, "offline", "digest.py"), key])


    def do_taxonomy_one(self):
        """给选中的这篇生成分类建议（可选带一句调整意见）。

        走 `judge.py classify --key`，与插件弹窗**同一份实现**
        （`judge.classify_item`），所以口径一致。
        """
        key = self.selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "请在上面的列表里选一篇文献。")
            return
        args = [os.path.join(ROOT, "offline", "judge.py"), "classify",
                "--key", key, "--json"]
        fb = (self.talk_var.get() if hasattr(self, "talk_var") else "").strip()
        if fb:
            args += ["--feedback", fb]
        self.run(f"分类建议 {key}" + ("（已带调整意见）" if fb else ""), args,
                 on_done=self._remember_suggestion)


    def do_taxonomy_talk(self):
        """「调整建议」：把上一轮结论 + 用户那句话一起发回去重判。"""
        fb = (self.talk_var.get() if hasattr(self, "talk_var") else "").strip()
        if not fb:
            messagebox.showinfo(
                "先说一句",
                "请在下面的输入框里写一句你的判断，例如：\n\n"
                "    · 这篇应该归到「分类 A」\n"
                "    · 这是设备类，不是方法类\n\n"
                "模型会基于这句话重新判断（分类仍然只能从已有分类里选）。")
            return
        self.do_taxonomy_one()


    def _remember_suggestion(self):
        """读回最近一次建议（`--json` 会写到 kb 下的文件），供「应用」用。"""
        try:
            p = os.path.join(self.kb_dir(), "last-suggestion.json")
            with open(p, encoding="utf-8") as fh:
                self.last_suggestion = json.load(fh)
            s = self.last_suggestion
            self.say(f"[{ts()}] 建议：{s.get('category') or '（无）'}"
                     f"（置信 {s.get('confidence')}）")
        except Exception as exc:  # noqa: BLE001
            self.say(f"[!!] 没读回建议结果：{exc}")


    def do_taxonomy_apply(self):
        """把最近一次建议应用回 Zotero（分类 + 标签）。

        ⚠ 这是**真的改你的 Zotero**，而且会同步到 zotero.org / 坚果云，
          所以要二次确认。走 tools\\zotero_sync.py 的 apply-one 子命令
          （本机没授权过时它会明确告诉你去哪授权）。
        """
        s = self.last_suggestion
        if not s:
            messagebox.showinfo("还没有建议", "请先点「分类建议」生成一个。")
            return
        key = s.get("key") or self.selected_key()
        cat = s.get("category") or ""
        tags = s.get("tags") or []
        if not cat and not tags:
            messagebox.showinfo("没什么可应用", "这条建议里既没有分类也没有标签。")
            return
        if not messagebox.askyesno(
                "确认应用到 Zotero",
                f"会给这篇：\n\n"
                f"  分类 → {cat or '（不改）'}\n"
                f"  标签 → {'、'.join(tags) if tags else '（不改）'}\n\n"
                f"⚠ 这会真的修改你的 Zotero 库，并同步到 zotero.org / 坚果云。\n"
                f"继续？"):
            return
        args = [os.path.join(ROOT, "tools", "zotero_sync.py"), "apply-one",
                "--key", key, "--category", cat]
        if tags:
            args += ["--tags", ",".join(tags)]
        self.run(f"应用分类建议 {key}", args)


    def do_taxonomy(self, use_model: bool = True):
        """生成分类建议。

        ⚠ 这里的算法**不在面板里**，而是走 `judge.py classify` ——
          它与服务端 `/classify`（Zotero 插件弹窗用的那条）**共用
          `judge.classify_item` 同一份实现**。这样：
             · 面板和插件给出的建议口径完全一致（提示词只有一份）
             · 模型配置也统一（插件设置面板里选的那个模型）
          之前面板等的是一个从来没写过的 `offline/taxonomy.py`，
          所以点了只弹"还没做"。
        """
        args = [os.path.join(ROOT, "offline", "judge.py"), "classify",
                "--limit", "10"]
        if not use_model:
            args.append("--no-model")
        self.run("分类建议" + ("（本地模型）" if use_model else "（只看分布）"), args)


    # ================================================================ 分类 / 标签

    def do_coll_list(self):
        """列出 Zotero 里的分类（结果写到高级页的输出框）。

        ⚠ 用户报过"点了没反应"。查下来不是后端错（`Searcher.collections()`
          正常），而是**结果落在页面最底下的输出框里、又只显示几行** ——
          按钮在上面、结果在下面看不见。现在两件事一起做：
            ① 输出框挪到页签内部可拖的那一格（一定看得见）；
            ② 点下去先往**全局日志**写一行，反馈立刻可见。
          这条经验对所有"结果写去别处"的按钮都适用。
        """
        self.say(f"[{ts()}] 正在读 Zotero 分类…（结果写到本页下面的「输出」）")

        def work():
            try:
                import schemas as S
                from searcher import Searcher

                s = Searcher()
                cols = s.collections()
                parts = [f"当前分类 {len(cols)} 个\n" + "=" * 70]
                for c in sorted(cols, key=lambda x: -x["items"]):
                    parts.append(f"  {c['name']:34} {c['items']:>3} 篇")
                # 顺带指出可能的重复（中英同名）
                names = [c["name"] for c in cols]
                dup = [(a, b) for a in names for b in names
                       if a < b and (a.lower() in b.lower() or b.lower() in a.lower())]
                if dup:
                    parts.append("\n⚠ 疑似重复的中英分类：")
                    for a, b in dup[:8]:
                        parts.append(f"    「{a}」 ↔ 「{b}」")
                s.close()
                self.out_queue.put(("show", (self.adv_text, "\n".join(parts))))
                self.out_queue.put(
                    ("log", f"[{ts()}] 分类读完了：{len(cols)} 个"
                            "（详见本页下面的「输出」）"))
            except Exception as exc:  # noqa: BLE001
                msg = f"出错了：{type(exc).__name__}: {exc}"
                self.out_queue.put(("show", (self.adv_text, msg)))
                self.out_queue.put(("log", f"[{ts()}] 读分类失败：{msg}"))

        threading.Thread(target=work, daemon=True, name="kb-coll-list").start()


    def do_coll_stats(self):
        self.run("分类分布统计",
                 [os.path.join(ROOT, "offline", "maintain.py"), "stats"])


    def do_write_check(self):
        """检查 Zotero 写入能力。

        ⚠ 这里原本调用 `tools/zotero_write.py` —— 那个文件**从来不存在**，
          所以这个按钮点了必然报"找不到文件"。
          实际能干活的是 `zotero_sync.py check`（探测可达性 + 授权需求），
          与下面「分类重整」区的「探测写入能力」是同一件事，直接复用。
        """
        self._sync("check", "检查 Zotero 写入能力")


    def do_sync_skill(self):
        script = os.path.join(ROOT, "scripts", "sync-skill.ps1")
        self.run("重载技能到 DSH",
                 ["-c",
                  "import subprocess, sys\n"
                  "sys.exit(subprocess.call([\n"
                  "    'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',\n"
                  "    '-File', r'" + script + "']))",
                  ])
