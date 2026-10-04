"""tab_advanced.py —— 「高级」页：全量重建、自检、备份、清理

⚠ 这是从 tools/gui.py 拆出来的一个页签。方法体与拆分前**逐字相同**；
每个混入类只提供方法，状态都挂在同一个 App 实例上（self）。
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import scrolledtext, ttk

from .common import (
    ROOT,
    ts,
)


class AdvancedTab:
    """「高级」页：全量重建、自检、备份、清理。"""


    # ---------------------------------------------------------- 高级

    def _build_advanced_tab(self):
        """冷门 / 危险 / 一次性的操作收在这里：功能不删，只是不挡路。

        为什么要收：旧版把「侧载安装 + 诊断 + 打包 xpi + 复制 token +
        分类重整五连击」和日常操作摆在同一个标签页里，用户看着一堆
        不知道是什么的按钮，反而找不到"更新索引"。

        ⚠ 用户 2026-10-05 又提了两条：
          · 开头那段"这里是一次性或排错用的功能…"的说明**整段删掉**；
          · 开发者那组的分隔标题从"—— 以下是一次性 / 开发者功能，平时不用管 ——"
            改成一句话「开发者功能」（细节已经在每个按钮的悬浮提示里了）。
        """
        f = self.tab_adv

        # 先放日常会用到的；一次性/开发者的在后面（下面 groups 的顺序就是显示顺序）
        groups = [
            ("检索文献", "search"),
            ("手动调权重（重点标记）", "weight"),
            ("索引维护", [
                ("统计", self.do_stats, "各表行数"),
                ("补齐知识库分级文件", self.do_views,
                 "给每篇补「摘要与要点 / 图注与表格 / 权重与经验」——"
                 "面板的「打开知识库」和 Zotero 右键菜单读的就是它"),
                ("列出当前分类", self.do_coll_list, "看 Zotero 里的分类"),
                ("分类分布统计", self.do_coll_stats, "每个类有多少篇"),
                ("重载技能到 DSH", self.do_sync_skill, "改了 SKILL.md 后同步"),
                ("清理测试残留", self.do_clean_test, "删掉自检留下的假经验"),
            ]),
            ("开发者功能", "divider"),
            ("分类重整（改 Zotero 里的分类归属）· 已跑完", [
                ("1 探测写入能力", self.do_sync_check, "看能不能连上 Zotero"),
                ("2 申请授权", self.do_sync_authorize, "Zotero 会弹窗，要允许"),
                ("3 干跑（只看要动什么）", self.do_sync_plan, "不修改任何东西"),
                ("4 执行重整", self.do_sync_apply, "真的改分类。先干跑"),
                ("5 核对结果", self.do_sync_verify, "改完检查"),
            ]),
            ("插件安装 · 只在重装插件时需要", [
                ("打包插件 xpi", self.do_build_plugin, "改过插件代码后重新打包"),
                ("侧载状态", self.do_sideload_status, "绕开 UI 安装的备用方式"),
                ("侧载安装", self.do_sideload_install, "UI 装不上时用"),
                ("诊断安装问题", self.do_diagnose, "装不上时跑这个看原因"),
                ("卸载侧载", self.do_sideload_remove, "撤销侧载"),
            ]),
            ("本地服务 · 一般不用手动起", [
                ("启动本地服务", self.do_service_start, "Zotero 开着时它会自动拉起"),
                ("服务状态", self.do_service_status, "地址、token、各项路径"),
                ("复制服务 token", self.do_copy_token, "极少数情况要手工填"),
            ]),
            ("Zotero 升级 · 已升级完", [
                ("升级前检查", self.do_upgrade_check, "查兼容性"),
                ("备份数据", self.do_upgrade_backup, "备份 zotero.sqlite"),
                ("升级后核对", self.do_upgrade_verify, "确认数据没丢"),
            ]),
        ]
        for title, items in groups:
            if items == "divider":
                sep = ttk.Frame(f, padding=(12, 8, 12, 0))
                sep.pack(fill="x")
                ttk.Separator(sep, orient="horizontal").pack(fill="x")
                ttk.Label(f, text=title, foreground="#999",
                          font=(self.ui_font, 9)).pack(anchor="w", padx=14)
                continue
            box = ttk.LabelFrame(f, text=" " + title + " ", padding=(10, 6))
            box.pack(fill="x", padx=12, pady=4)
            if isinstance(items, str):
                self._build_adv_inputs(box, items)
                continue
            line = ttk.Frame(box)
            line.pack(fill="x")
            for text, cmd, tip in items:
                b = ttk.Button(line, text=text, command=cmd)
                b.pack(side="left", padx=(0, 6), pady=2)
                self._tip(b, tip)

        ttk.Label(f, text="输出", font=(self.ui_font, 9, "bold")).pack(
            anchor="w", padx=14, pady=(8, 4))
        # ⚠ 输出框放在**页签内部的下面那一格**（可拖分隔线）：用户反馈
        #   "点了按钮像没反应"—— 输出落在页面最底下、又只能显示几行，
        #   得把窗口拉长才看得见。现在它自己占一格、能拖大。
        self.adv_text = scrolledtext.ScrolledText(self.adv_out, height=9,
                                                  wrap="word",
                                                  font=(self.mono_font, 10))
        self.adv_text.pack(fill="both", expand=True, padx=6, pady=4)


    def _build_adv_inputs(self, box, kind: str):
        """高级页里需要输入框的两组：检索、手动调权重。"""
        if kind == "search":
            ttk.Label(box, text="关键词：").pack(side="left")
            self.q_var = tk.StringVar()
            qe = ttk.Entry(box, textvariable=self.q_var, width=38)
            qe.pack(side="left", padx=4)
            qe.bind("<Return>", lambda _e: self.do_search())
            b = ttk.Button(box, text="搜索", command=self.do_search)
            b.pack(side="left")
            self._tip(b, "结果里带可读的文献名和 key，便于复制")
            # ⚠ 用户反馈："高级里的检索文献也应该能下拉列表选取。"
            #   所以除了"搜关键词"，再给一个"从列表选"——在弹窗里能按列看、
            #   能排序，选中一篇直接填到下面的「文献 key」框，不用手抄。
            b2 = ttk.Button(box, text="从列表选…",
                            command=lambda: self.open_paper_picker(
                                on_pick=self._fill_weight_key))
            b2.pack(side="left", padx=6)
            self._tip(b2, "打开全部文献列表，选一篇直接填到下面的 key 框")
            return

        ttk.Label(box, text="文献 key：").pack(side="left")
        self.w_key = tk.StringVar()
        ttk.Entry(box, textvariable=self.w_key, width=14).pack(side="left",
                                                               padx=4)
        ttk.Label(box, text="备注：").pack(side="left")
        self.w_note = tk.StringVar()
        ttk.Entry(box, textvariable=self.w_note, width=30).pack(side="left",
                                                                padx=4)
        for text, cmd, tip in (
            ("标为重点", lambda: self.do_weight(True), "检索时权重提高"),
            ("取消重点", lambda: self.do_weight(False), ""),
            ("列出权重榜", self.do_weight_list, "看全部被加权过的文献"),
        ):
            b = ttk.Button(box, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self._tip(b, tip)


    def _fill_weight_key(self, key):
        """从选择器选中后，把 key 填进「手动调权重」的输入框。"""
        if hasattr(self, "w_key"):
            self.w_key.set(key)
        self.say(f"[{ts()}] 已填入 key：{key}（可直接标重点）")


    # ================================================================ 快捷操作

    def do_convert_incremental(self):
        self.run("更新索引（增量）", [os.path.join(ROOT, "offline", "convert.py")],
                 on_done=self._after_build_report)


    def do_convert_full(self):
        self.run("全量重建", [os.path.join(ROOT, "offline", "convert.py"), "--full"],
                 confirm="全量重建会重新解析全部文献并重算向量，约 1-2 分钟。继续？",
                 on_done=self._after_build_report)


    def _after_build_report(self):
        """构建结束后读清单，有事就弹窗 —— 别让用户去日志里翻。

        为什么要弹窗（而不是只在日志里打几行）：
          · **没有 PDF 附件的条目被跳过了**。这不是出错，是那些条目本身缺东西，
            但用户过一阵会奇怪"我库里明明有这篇，怎么知识库搜不到"。
            弹一次说清楚，比让他猜好。
          · **字符偏移被自动还原**的篇目也值得报一声：那说明源 PDF 的字体
            编码是坏的（换源修不了），用户可能想知道哪些文献属于这种。
        没事发生时（绝大多数构建）**不弹窗**，避免每次构建都打扰。
        """
        try:
            import schemas as S
            with open(S.MANIFEST, encoding="utf-8") as fh:
                mf = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            self.say(f"[XX] 读构建清单失败：{type(exc).__name__}: {exc}")
            return
        skipped = ((mf.get("warnings") or {}).get("skipped_no_pdf")) or []
        deshifted = ((mf.get("fulltext") or {}).get("deshifted")) or []
        if not skipped and not deshifted:
            return

        lines = []
        if deshifted:
            lines.append(f"⚙ {len(deshifted)} 篇的正文原本是「字符整体偏移」的"
                         f"（源 PDF 字体编码坏，换源也修不了），已自动还原：")
            for x in deshifted[:8]:
                lines.append(f"    {x['key']}    {x.get('src', '')}")
            if len(deshifted) > 8:
                lines.append(f"    …还有 {len(deshifted) - 8} 篇")
            lines.append("")
        if skipped:
            lines.append(f"⚠ {len(skipped)} 条没有 PDF 附件，没有收进知识库：")
            for x in skipped[:12]:
                lines.append(f"    {x['key']}    {(x.get('title') or '')[:46]}")
            if len(skipped) > 12:
                lines.append(f"    …还有 {len(skipped) - 12} 条")
            lines.append("")
            lines.append("想让它们进知识库：在 Zotero 里给这些条目挂上 PDF，"
                         "再跑一次构建。")
        self._show_dialog("构建报告", "\n".join(lines))


    def do_check(self):
        self.run("环境自检", [os.path.join(ROOT, "offline", "maintain.py"), "check"])


    def do_stats(self):
        self.run("统计", [os.path.join(ROOT, "offline", "maintain.py"), "stats"])


    def do_backup(self):
        self.run("备份索引", [os.path.join(ROOT, "offline", "maintain.py"), "backup"])

    def do_views(self):
        """补齐分级视图（views/*.md）。

        什么时候需要它：① 升级到带分级视图的版本时，库里已有的条目没有这些
        文件；② 只改了经验/权重（那不动正文，不会触发重建）。
        没有它就得为了几个小文件跑一次 2 分钟的全量重建。
        """
        self.run("补齐分级文件",
                 [os.path.join(ROOT, "offline", "maintain.py"), "views"])


    def do_clean_test(self):
        self.run("清理测试残留",
                 [os.path.join(ROOT, "tools", "kb_admin.py"), "clean", "--test-data"],
                 on_done=lambda: self.run(
                     "清理权重残留",
                     [os.path.join(ROOT, "tools", "kb_admin.py"), "clean",
                      "--orphan-weights"]))
