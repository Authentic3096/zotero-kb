"""tab_struct.py —— 「知识库结构」页：每个文件夹存什么、能删吗

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import fnmatch
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (
    KB_FILE_SPEC,
    ROOT,
    ts,
)


class StructTab:
    """「知识库结构」页：每个文件夹存什么、能删吗。"""


    # ---------------------------------------------------------- 知识库结构

    def _build_struct_tab(self):
        """用户明确要的「知识库结构表」：每个文件夹是干什么的。

        为什么要单独一页：知识库点开是一堆目录（papers/fulltext/inbox/
        .cache/logs + 若干 json），光看名字猜不出用途；而且这些目录
        **有的能删、有的删了就丢经验**，不讲清用户不敢动。
        """
        f = self.tab_struct
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="知识库的位置与内容",
                  font=(self.ui_font, 11, "bold")).pack(side="left")
        # ⚠ 两个入口的分工（用户 2026-10-05 提的）：
        #   「打开知识库」= 从**文献**出发（先列文献，再列级别，再打开 md）。
        #     它在**顶部那一排核心按钮**里（panels/base.py 的 _build_header）——
        #     那是个日常动作，不该只有切到这一页才看得到，所以这里不重复放。
        #   「文献管理器中查看」= 从**文件**出发，留在这一页（跟这张结构表配套）。
        #     原名"打开知识库目录"，用户说看不出是在资源管理器里打开。
        ttk.Button(head, text="文献管理器中查看",
                   command=self.open_folder).pack(side="right")
        ttk.Button(head, text="刷新", command=self.refresh_struct).pack(
            side="right", padx=6)
        # 可选组件的安装引导：**只在没检测到那个组件时才出现**（用户定的规矩：
        # "如果检测到 minerU 就没这个按键"；Ollama 同理）。检测放后台线程，
        # 结果回主线程决定按钮要不要 pack —— 所以先建好、默认不显示。
        self.mineru_btn = ttk.Button(
            head, text="MinerU 安装引导", command=self.open_mineru_guide)
        self._mineru_btn_shown = False
        self.ollama_btn = ttk.Button(
            head, text="Ollama 安装引导", command=self.open_ollama_guide)
        self._ollama_btn_shown = False
        # ⚠ 用 `self.root.after` 而不是 `self.after`：App 是"混入类的组合"，
        #   本身不是 Tk 控件（面板冒烟测试里就是在没有真 root 的情况下装配的，
        #   写 self.after 会 AttributeError —— 本机测试逮到）。
        self.root.after(300, self._check_mineru_button)
        self.root.after(500, self._check_ollama_button)

        # 用 Treeview 做表：能对齐、能排序、能选中
        cols = ("name", "what", "count", "size", "safe")
        wrap = ttk.Frame(f, padding=(10, 6, 10, 4))
        wrap.pack(fill="both", expand=True)
        self.struct_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                        height=13)
        for key, text, width in (
            ("name", "文件 / 文件夹", 150),
            # ⚠ 「存放内容」这一列的文字最长（一句话讲清一个文件是什么），
            #   给窄了就得把窗口拉大才看得全 —— 用户原话"很多东西都要把面板
            #   拉长才能看到"。列宽可拖，这里给个更合适的起点。
            ("what", "存放内容", 520),
            ("count", "内容量", 100),
            ("size", "占用", 80),
            ("safe", "能删吗", 190),
        ):
            self.struct_tree.heading(key, text=text)
            self.struct_tree.column(key, width=width,
                                    anchor="w" if key in ("name", "what", "safe")
                                    else "e")
        vs = ttk.Scrollbar(wrap, orient="vertical",
                           command=self.struct_tree.yview)
        self.struct_tree.configure(yscrollcommand=vs.set)
        self.struct_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.struct_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.struct_note, foreground="#666",
                  wraplength=900, justify="left").pack(
            fill="x", padx=12, pady=(0, 8))

        self.refresh_struct()


    # ---------------------------------------------------------- MinerU 引导

    def _check_mineru_button(self):
        """后台探一次 MinerU，决定「MinerU 安装引导」按钮显不显示。

        用户规矩：**检测到就不显示**（装了就不该再被引导）。探测要跑子进程
        （约 1~4 秒，`offline/mineru.py` 有 60 秒缓存），所以放线程里，
        结果回主线程改界面（Tk 控件只能主线程碰）。
        `refresh_struct()` 之后也会再调一次（用户点「刷新」时重新判断）。
        """
        def work():
            ok = False
            try:
                import sys as _sys
                if os.path.join(ROOT, "offline") not in _sys.path:
                    _sys.path.insert(0, os.path.join(ROOT, "offline"))
                import mineru as MU
                ok = bool(MU.probe().get("ok"))
            except Exception:      # noqa: BLE001
                ok = False
            self.after(0, lambda: self._apply_mineru_button(ok))

        threading.Thread(target=work, daemon=True).start()

    def _apply_mineru_button(self, installed: bool):
        want = not installed
        if want == self._mineru_btn_shown:
            return
        try:
            if want:
                self.mineru_btn.pack(side="right", padx=6)
            else:
                self.mineru_btn.pack_forget()
        except tk.TclError:
            return
        self._mineru_btn_shown = want

    def open_mineru_guide(self):
        """打开安装引导窗口（与插件首启对话框进的是同一个）。"""
        from .mineru_guide import MineruGuide
        win = MineruGuide(self.root, self,
                          on_done=self._check_mineru_button)
        win.focus_set()

    # ---------------------------------------------------------- Ollama 引导

    def _check_ollama_button(self):
        """后台探一次 Ollama，决定「Ollama 安装引导」按钮显不显示。

        与 MinerU 同一套规矩：**检测到就不显示**。判据用
        `panels.ollama_guide.ollama_paths()`（找 exe + 问 API），它内部走
        `schemas.resolve_ollama()` 与 `judge.ollama_models()`，都是已有的判据。
        """
        def work():
            installed = False
            try:
                from .ollama_guide import ollama_paths
                st = ollama_paths()
                installed = bool(st.get("exe"))
            except Exception:      # noqa: BLE001
                installed = False
            self.after(0, lambda: self._apply_ollama_button(installed))

        threading.Thread(target=work, daemon=True).start()

    def _apply_ollama_button(self, installed: bool):
        want = not installed
        if want == self._ollama_btn_shown:
            return
        try:
            if want:
                self.ollama_btn.pack(side="right", padx=6)
            else:
                self.ollama_btn.pack_forget()
        except tk.TclError:
            return
        self._ollama_btn_shown = want

    def open_ollama_guide(self):
        """打开 Ollama 安装引导（下载 → 静默装 → 启动 → 拉模型）。"""
        from .ollama_guide import OllamaGuide
        win = OllamaGuide(self.root, self, on_done=self._check_ollama_button)
        win.focus_set()


    def refresh_struct(self):
        """扫描知识库目录，填结构表。"""
        import time as _t

        def sizeof(path: str) -> int:
            total = 0
            for dirpath, _dirs, files in os.walk(path):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            return total

        def human(n: int) -> str:
            if n >= 1024 * 1024:
                return f"{n / 1024 / 1024:.1f} MB"
            if n >= 1024:
                return f"{n / 1024:.0f} KB"
            return f"{n} B"

        kb = self.kb_dir()
        SPEC = KB_FILE_SPEC

        try:
            self.struct_tree.delete(*self.struct_tree.get_children())
        except tk.TclError:
            return

        used = 0
        # Treeview 不渲染 markdown：`**加粗**` 会原样显示成带星号的怪样子。
        # 这个坑犯了两次（第一版是 index.db 的说明，第二版是 INDEX.md），
        # 所以改成**在插入前统一剥掉**，以后写说明时不用再记这条。
        def plain(s: str) -> str:
            return s.replace("**", "")

        for name, what, kind, safe in SPEC:
            what, safe = plain(what), plain(safe)
            p = os.path.join(kb, name.rstrip("\\/"))
            if kind == "dir":
                if not os.path.isdir(p):
                    continue
                n = sum(len(fs) for _dp, _dn, fs in os.walk(p))
                size = sizeof(p)
                count = f"{n} 个文件"
            elif kind == "db":
                if not os.path.exists(p):
                    continue
                # 只算主库：-wal / -shm 由下面 `index.db-*` 那一行单独列，
                # 这里再算一遍会让「合计」重复计（曾按"一起算"写过，改了行数才分开）。
                size = os.path.getsize(p)
                count = "见上面状态栏"
            elif kind == "glob":
                # 一类文件合并成一行（历史备份这种：多份、同样大、逐行列反而看不清）
                import glob as _glob
                hits = [x for x in _glob.glob(p) if os.path.isfile(x)]
                if not hits:
                    continue
                size = sum(os.path.getsize(x) for x in hits)
                count = f"{len(hits)} 个"
            else:
                if not os.path.isfile(p):
                    continue
                size = os.path.getsize(p)
                count = f"{human(size)}"
            used += size
            self.struct_tree.insert("", "end", values=(
                name, what, count, human(size), safe))

        # 不在清单里的文件也要显示 —— 否则用户看到目录里有东西、表里没有会疑惑
        try:
            # ⚠ glob 类条目（如 index.db.bak*）不是具体文件名，不能进 known 集合，
            #   否则它会"占着名字"却匹配不上任何文件，而那些文件又会在下面
            #   被当成"未在说明表里"**重复列一遍**。
            known = {n.rstrip("\\/").lower() for n, _w, _k, _s in SPEC
                     if _k != "glob"}
            patterns = [n.lower() for n, _w, k, _s in SPEC if k == "glob"]
            extras = [n for n in os.listdir(kb)
                      if n.lower() not in known
                      and not any(fnmatch.fnmatch(n.lower(), g) for g in patterns)]
            for n in sorted(extras):
                p = os.path.join(kb, n)
                if os.path.isdir(p):
                    size = sizeof(p)
                    cnt = f"{sum(len(fs) for _dp, _dn, fs in os.walk(p))} 个文件"
                else:
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        size = 0
                    cnt = human(size)
                used += size
                self.struct_tree.insert("", "end", values=(
                    n + ("\\" if os.path.isdir(p) else ""),
                    "（未在说明表里的文件，可能是新版本新增的）",
                    cnt, human(size), "? 先别删"))
        except OSError:
            pass

        self.struct_note.set(
            f"知识库根目录：{kb}    ·    合计 {human(used)}\n"
            "说明：papers/、fulltext/、views/ 都是可重建的派生物，删了跑一次"
            "「手动更新」就回来；index.db 里的经验层和权重是攒出来的，"
            "重建索引不会恢复它们 —— 所以定期点「备份」。\n"
            "⚠ 上面三个 .txt（service-token / bridge-token / zotero-api-key）"
            "是本机程序之间互相认身份的通行证，全部自动生成、"
            "跟用哪个模型无关 —— 用 Ollama 不需要任何模型 key，"
            "这三个文件也一直会在。模型那边的 key（如果用 API）"
            "存在 llm-config.json 里。")

    # ---------------------------------------------------------- 打开知识库

    def open_kb_browser(self):
        """「打开知识库」：先选文献，再选级别，最后打开那个 md。

        为什么不直接打开目录（原来只有那一个入口）：目录里的文件名是 Zotero
        的 key（`22X9PMR6.md`），人认不出是哪篇，等于让用户自己去找。
        这里把"找"变成两次点选 —— 而且第一段直接复用已有的文献选择弹窗。
        """
        rows = getattr(self, "paper_rows", None)
        if not rows:
            messagebox.showinfo(
                "列表还没读好",
                "正在从索引里读文献列表（几秒），稍等一下再点。\n\n"
                "如果一直没读到，去「分类建议」页点一次「刷新列表」。")
            # 顺手替用户触发一次读列表，省得他还要自己找那个按钮
            try:
                self.refresh_paper_list()
            except Exception:  # noqa: BLE001
                pass
            return
        self.open_paper_picker(on_pick=self._open_kb_levels)

    def _open_kb_levels(self, key: str):
        """文献选择弹窗里选中了一篇 → 打开它的级别列表。"""
        title = key
        for r in getattr(self, "paper_rows", []):
            if r[0] == key:
                title = (f"{r[1] or '（无作者）'} {r[2] or ''} · "
                         f"{(r[3] or '')[:38]}")
                break
        try:
            from .browser import open_level_picker
        except ImportError as exc:
            messagebox.showerror("缺文件", f"找不到 panels/browser.py：{exc}")
            return
        open_level_picker(self.root, key, title,
                          on_log=lambda t: self.say(f"[{ts()}] {t}"),
                          ui_font=self.ui_font, mono_font=self.mono_font)
