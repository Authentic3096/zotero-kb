"""tab_struct.py —— 「知识库结构」页：每个文件夹存什么、能删吗

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import fnmatch
import os
import tkinter as tk
from tkinter import ttk

from .common import (
    KB_FILE_SPEC,
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
        ttk.Button(head, text="打开知识库目录", command=self.open_folder).pack(
            side="right")
        ttk.Button(head, text="刷新", command=self.refresh_struct).pack(
            side="right", padx=6)

        # 用 Treeview 做表：能对齐、能排序、能选中
        cols = ("name", "what", "count", "size", "safe")
        wrap = ttk.Frame(f, padding=(10, 6, 10, 4))
        wrap.pack(fill="both", expand=True)
        self.struct_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                        height=13)
        for key, text, width in (
            ("name", "文件 / 文件夹", 150),
            ("what", "存的是什么", 380),
            ("count", "内容量", 110),
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
            "说明：papers/ 与 fulltext/ 是可重建的派生物，删了跑一次"
            "「手动更新」就回来；index.db 里的经验层和权重是攒出来的，"
            "重建索引不会恢复它们 —— 所以定期点「备份」。\n"
            "⚠ 上面三个 .txt（service-token / bridge-token / zotero-api-key）"
            "是本机程序之间互相认身份的通行证，全部自动生成、"
            "跟用哪个模型无关 —— 用 Ollama 不需要任何模型 key，"
            "这三个文件也一直会在。模型那边的 key（如果用 API）"
            "存在 llm-config.json 里。")
