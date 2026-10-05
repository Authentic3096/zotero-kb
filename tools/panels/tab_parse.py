"""tab_parse.py —— 「PDF 解析」页（MinerU 可选组件）。

## 这一页解决什么

MinerU 接进转换管道之后，"库里这批正文到底是 Zotero 缓存还是 MinerU 解析的、
哪几篇还没走、哪几篇失败了、占了多大地方"这些问题**必须看得见** ——
否则用户只能靠 `items.fulltext_src` 猜，而"重解析全库"是个几十分钟的操作，
看不见状态就没有信心去点。

## 界面

- 策略区：解析器下拉（zotero / flash / basic / standard / advanced）+
  三个动作（只补缺失 / 全库重解析 / 强制重解析）+ 「解析选中的这一篇」；
- 状态表：每篇一行 —— 文献 / 当前来源 / 档位 / 页数 / 图片 / 耗时 / 产物 / 时间；
- 汇总行：已用 MinerU x 篇、待解析 y 篇、失败 z 篇、产物占多少 MB；
- 底部输出格：命令实时输出（长任务在子进程里跑，界面不卡）。

⚠ MinerU 没装时这一页照样能打开（表是空的、汇总写"未检测到"），
  并在顶部给一个入口指向「知识库结构」页的安装引导 —— 不再弹错。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .common import ROOT, ts


class ParseTab:
    """「PDF 解析」页：MinerU 的状态、单篇解析与全库重解析。"""

    # ================================================================ 建界面

    def _build_parse_tab(self):
        f = self.tab_parse
        head = ttk.Frame(f, padding=(10, 8, 10, 0))
        head.pack(fill="x")
        ttk.Label(head, text="PDF 解析（MinerU 可选组件）",
                  font=(self.ui_font, 11, "bold")).pack(side="left")
        ttk.Button(head, text="刷新状态", command=self.refresh_parse).pack(
            side="right")
        b_guide = ttk.Button(head, text="安装引导", command=self.open_mineru_guide)
        b_guide.pack(side="right", padx=6)
        self._tip(b_guide, "没装 MinerU（或模型没下全）时用它；装了就不用管这一页以外的设置")

        ttk.Label(
            f, foreground="#666", font=(self.ui_font, 9), justify="left",
            wraplength=920,
            text="MinerU 是「可选」的 PDF 解析增强：装了它，正文更干净、"
                 "公式会变成 LaTeX（能进检索）、表格与扫描件更稳。"
                 "不装也能用 —— 正文走 Zotero 缓存 + PyMuPDF，与以前逐字节一致。\n"
                 "下面「解析器」选 MinerU 档位后，构建只会重解析那一批"
                 "（已解析且 PDF 没变的会按指纹跳过，几秒一篇；真解析约 5~35 秒/篇）。"
        ).pack(fill="x", padx=12, pady=(6, 4))

        # ---- 策略区
        box = ttk.LabelFrame(f, text=" 策略与动作 ", padding=10)
        box.pack(fill="x", padx=10)
        r1 = ttk.Frame(box)
        r1.pack(fill="x")
        ttk.Label(r1, text="解析器：").pack(side="left")
        self.parse_tier = tk.StringVar(value="basic")
        ttk.Combobox(r1, textvariable=self.parse_tier, width=12, state="readonly",
                     values=("zotero", "flash", "basic", "standard",
                             "advanced")).pack(side="left")
        self._tip_p = ttk.Label(r1, foreground="#888", font=(self.ui_font, 8),
                                text="（zotero = 原路；basic 已够好，VLM 档更慢）")
        self._tip_p.pack(side="left", padx=6)

        r2 = ttk.Frame(box)
        r2.pack(fill="x", pady=(6, 0))
        b1 = ttk.Button(r2, text="① 只补缺失的（推荐先点）",
                        command=lambda: self.do_parse_missing())
        b1.pack(side="left")
        self._tip(b1, "只解析还没有 MinerU 产物的那批，已解析的跳过")
        b2 = ttk.Button(r2, text="② 全库重解析", command=self.do_parse_all)
        b2.pack(side="left", padx=6)
        self._tip(b2, "把全库的正文都换成所选档位（PDF 没变的不重跑）")
        b3 = ttk.Button(r2, text="③ 解析选中的这一篇",
                        command=self.do_parse_selected)
        b3.pack(side="left")
        b4 = ttk.Button(r2, text="删掉选中这篇的产物",
                        command=self.do_clear_selected)
        b4.pack(side="left", padx=6)
        b5 = ttk.Button(r2, text="打开产物目录",
                        command=self.do_open_artifacts)
        b5.pack(side="left")

        # ---- 汇总
        self.parse_note = tk.StringVar(value="正在读状态…")
        ttk.Label(f, textvariable=self.parse_note, foreground="#333",
                  font=(self.ui_font, 9), justify="left",
                  wraplength=920).pack(fill="x", padx=12, pady=(8, 2))

        # ---- 状态表
        cols = ("key", "title", "src", "tier", "pages", "images", "secs", "state")
        wrap = ttk.Frame(f, padding=(10, 0, 10, 6))
        wrap.pack(fill="both", expand=True)
        self.parse_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                       height=12)
        for key, text, width in (
            ("key", "key", 90), ("title", "标题", 330),
            ("src", "正文来源（库）", 150), ("tier", "产物档位", 80),
            ("pages", "页数", 55), ("images", "图", 45),
            ("secs", "解析耗时", 75), ("state", "产物状态", 150),
        ):
            self.parse_tree.heading(key, text=text)
            self.parse_tree.column(key, width=width, anchor="w",
                                   stretch=(key in ("title", "state")))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.parse_tree.yview)
        self.parse_tree.configure(yscrollcommand=vs.set)
        self.parse_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.parse_tree.bind("<Double-1>", lambda _e: self.do_parse_selected())
        self._tip(self.parse_tree, "双击一行 = 解析这一篇；选中后可用上面的按钮")
        # 进这一页就自动读一次状态：不读的话表格是空的、提示停在"正在读状态…"，
        # 用户会以为坏了（本机截图核对时发现的）。
        self.root.after(400, self.refresh_parse)

    # ================================================================ 读状态

    def refresh_parse(self):
        """后台读一遍状态（DB 的 fulltext_src + 每篇的 meta.json）。"""
        self.parse_note.set("正在读状态…")

        def work():
            rows = []
            err = ""
            try:
                import sqlite3
                import schemas as S
                import mineru as M
                conn = sqlite3.connect(S.INDEX_DB)
                conn.row_factory = sqlite3.Row
                items = conn.execute(
                    "SELECT key, title, fulltext_src, fulltext_chars FROM items "
                    "ORDER BY title").fetchall()
                for it in items:
                    st = M.status_for(it["key"])
                    rows.append({
                        "key": it["key"], "title": it["title"] or "",
                        "src": it["fulltext_src"] or "(无全文)",
                        "tier": st["tier"], "pages": st["pages"],
                        "images": st["images"], "secs": st["seconds"],
                        "parsed": st["parsed"], "stale": st["stale"],
                        "at": st["at"], "err": st["last_error"],
                    })
                conn.close()
                root_dir = os.path.join(S.kb_dir(), M.ART_ROOT)
                size = 0
                for dp, _dn, fn in os.walk(root_dir):
                    for n in fn:
                        try:
                            size += os.path.getsize(os.path.join(dp, n))
                        except OSError:
                            pass
                info = M.probe()
                note = {
                    "mineru": M.summary_line(info),
                    "ok": bool(info.get("ok")),
                    "size_mb": round(size / 1048576, 1),
                    "ready": [t for t, v in (info.get("tier_ready") or {}).items()
                              if v],
                }
            except Exception as exc:      # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                rows, note = [], {}
            self.out_queue.put(("call", (self._apply_parse, (rows, note, err))))

        threading.Thread(target=work, daemon=True).start()

    def _apply_parse(self, payload):
        rows, note, err = payload
        try:
            self.parse_tree.delete(*self.parse_tree.get_children())
        except tk.TclError:
            return
        n_mineru = n_parsed = n_stale = 0
        for r in rows:
            if str(r["src"]).startswith("mineru-"):
                n_mineru += 1
            if r["parsed"]:
                n_parsed += 1
            if r["stale"]:
                n_stale += 1
            state = ("✓ 有产物" if r["parsed"] else
                     ("✗ 产物缺失" if str(r["src"]).startswith("mineru-") else "— 没解析"))
            if r["stale"] and r["parsed"]:
                state = "⚠ 指纹过期（PDF 或档位变了）"
            elif r["err"]:
                state = "✗ " + str(r["err"])[:40]
            self.parse_tree.insert("", "end", values=(
                r["key"], r["title"][:60], r["src"], r["tier"] or "-",
                r["pages"] or "-", r["images"] or "-",
                (f"{r['secs']}s" if r["secs"] else "-"), state))
        if err:
            self.parse_note.set(f"读状态出错：{err}")
            return
        ready = "、".join(note.get("ready") or []) or "（模型没下全）"
        self.parse_note.set(
            f"共 {len(rows)} 篇：其中正文来源已是 MinerU 的 {n_mineru} 篇，"
            f"有解析产物的 {n_parsed} 篇（指纹过期 {n_stale} 篇待重跑）。"
            f"产物占用 {note.get('size_mb', 0)} MB。\n"
            f"MinerU：{note.get('mineru', '')}；可用档位：{ready}")

    # ================================================================ 动作

    def _selected_key(self) -> str:
        sel = self.parse_tree.selection()
        if not sel:
            return ""
        vals = self.parse_tree.item(sel[0], "values")
        return str(vals[0]) if vals else ""

    def do_parse_missing(self):
        self.run(
            f"补全缺失：MinerU {self.parse_tier.get()} 档",
            [os.path.join(ROOT, "tools", "kb_admin.py"), "mineru", "missing",
             "--tier", self.parse_tier.get()],
            confirm=f"会把「还没有 MinerU 产物」的文献用 {self.parse_tier.get()} "
                    f"档解析一遍（几十分钟，可随时点「停止当前任务」）。\n"
                    f"已解析且 PDF 没变的会按指纹跳过。继续？")

    def do_parse_all(self):
        self.run(
            f"全库重解析：MinerU {self.parse_tier.get()} 档",
            [os.path.join(ROOT, "tools", "kb_admin.py"), "mineru", "reparse",
             "--tier", self.parse_tier.get()],
            confirm=f"会把「全库正文」换成 MinerU {self.parse_tier.get()} 档解析的结果"
                    f"（并重建切片与向量，几十分钟）。\n"
                    f"PDF 没变、档位没变的那批会按指纹跳过（几秒一篇）。\n"
                    f"建议先点「只补缺失的」。继续？")

    def do_parse_selected(self):
        key = self._selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "在上面的表里选一行（或双击一行）。")
            return
        self.run(f"解析这一篇：{key}（{self.parse_tier.get()} 档）",
                 [os.path.join(ROOT, "tools", "kb_admin.py"), "mineru", "parse",
                  "--key", key, "--tier", self.parse_tier.get(), "--rebuild"])

    def do_clear_selected(self):
        key = self._selected_key()
        if not key:
            messagebox.showinfo("先选一篇", "在上面的表里选一行。")
            return
        self.run(f"删掉 {key} 的解析产物",
                 [os.path.join(ROOT, "tools", "kb_admin.py"), "mineru", "clear",
                  "--key", key],
                 confirm=f"删掉 {key} 在 <知识库>\\mineru\\{key}\\ 下的产物。\n"
                         f"（下次构建会重新解析；切片与向量不受影响）继续？")

    def do_open_artifacts(self):
        key = self._selected_key()
        try:
            import schemas as S
            import mineru as M
            path = (M.artifact_dir(key) if key
                    else os.path.join(S.kb_dir(), M.ART_ROOT))
        except Exception as exc:      # noqa: BLE001
            self.say(f"[XX] 找不到产物目录：{exc}")
            return
        if not os.path.isdir(path):
            messagebox.showinfo("还没有产物", f"{path}\n\n这一篇还没解析过。")
            return
        try:
            os.startfile(path)          # noqa: S606  （Windows 打开资源管理器）
            self.say(f"[{ts()}] 已打开 {path}")
        except Exception as exc:      # noqa: BLE001
            self.say(f"[XX] 打不开 {path}：{exc}")
