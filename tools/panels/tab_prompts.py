"""tab_prompts.py —— 「提示词」页：所有喂给模型的固定话术，能看、能改、能还原。

## 为什么要有这一页

改之前这些话术**硬编码**在 5 个文件里（judge.py 的 TAG/SUMMARY/EXTRACT_SYSTEM、
metafill.py 的 MODEL_SYSTEM/MODEL_PROMPT、check_chunks.py 的 ITEM_PROMPT…），
用户看不到也改不了。而"经验抽得不对"这种问题恰恰只能靠改一句话解决 ——
用户的原话：「现在就有提示词吗，如果有应该做成用户可查看和修改」。

## 三个按钮为什么是这三个

   保存        写进 kb/prompts.json（**保存前校验**：占位符还在、JSON 契约键还在）
   恢复默认    删掉覆盖，回到代码里的默认值
   试跑        拿一条固定样例跑一次，显示模型输出与 JSON 解析结果

「试跑」是这一页最有用的按钮：提示词改坏了最典型的症状是"模型开始胡说"，
而那要等到真实任务里才发现。固定样例让改动**当场可见**。

⚠ `offline/prompts.py` 是**同一个进程里 import 的**（不是起子进程）：
   面板本来就跑在项目 venv 里、offline 已经在 sys.path 上（见 panels/common.py），
   而"试跑"要调模型（可能几秒），所以放后台线程 + out_queue 回传。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .common import ROOT, ts


class PromptsTab:
    """「提示词」页：查看 / 编辑 / 还原 / 试跑。"""


    def _build_prompts_tab(self):
        """提示词注册表：5 分钟能看懂模型在按什么话术干活。"""
        f = self.tab_prompts or self.tab_exp   # 没建过就借用经验页（不该发生）
        bar = ttk.Frame(f, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="提示词：").pack(side="left")
        self.prompt_choice = tk.StringVar()
        self.prompt_combo = ttk.Combobox(bar, textvariable=self.prompt_choice,
                                        state="readonly", width=34)
        self.prompt_combo.pack(side="left", padx=4)
        self.prompt_combo.bind("<<ComboboxSelected>>",
                               lambda _e: self.do_prompt_load())
        for text, cmd, tip in (
            ("保存", self.do_prompt_save, "写入 kb/prompts.json（保存前会校验占位符与 JSON 契约键）"),
            ("恢复默认", self.do_prompt_reset, "删掉这一条的改动，回到代码里的默认值"),
            ("试跑", self.do_prompt_probe, "拿固定样例跑一次，看模型怎么回、JSON 解析过不过"),
        ):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=3)
            self._tip(b, tip)

        self.prompt_where = ttk.Label(f, foreground="#666", wraplength=980,
                                      justify="left", text="")
        self.prompt_where.pack(fill="x", padx=12, pady=(0, 4))

        box = ttk.Frame(f, padding=(10, 0, 10, 6))
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="system（角色与纪律）").pack(anchor="w")
        self.prompt_system = scrolledtext.ScrolledText(box, height=6, wrap="word",
                                                       font=(self.mono_font, 10))
        self.prompt_system.pack(fill="both", expand=False, pady=(0, 6))
        ttk.Label(box, text="user（真正的任务；{占位符} 会由代码填内容）").pack(anchor="w")
        self.prompt_user = scrolledtext.ScrolledText(box, height=14, wrap="word",
                                                     font=(self.mono_font, 10))
        self.prompt_user.pack(fill="both", expand=True, pady=(0, 6))

        # 试跑输出放在页签内部的下面那一格（分隔线可拖）—— 模型输出有时很长
        self._prompt_out = scrolledtext.ScrolledText(self.prompt_out, height=8,
                                                     wrap="word",
                                                     font=(self.mono_font, 10))
        self._prompt_out.pack(fill="both", expand=True, padx=6, pady=4)
        self.do_prompt_refresh()


    # ================================================================ 提示词

    def _prompts_mod(self):
        """拿到 offline/prompts.py（id 列表与改动状态都从它读）。"""
        import prompts as PR      # noqa: PLC0415 —— 面板里 import 同进程模块
        return PR


    def do_prompt_refresh(self):
        """刷新下拉框里的条目与"改过没有"的标记。"""
        try:
            PR = self._prompts_mod()
        except Exception as exc:      # noqa: BLE001
            self.say(f"[{ts()}] 读不到 prompts.py：{exc}")
            return
        items = []
        for row in PR.status():
            mark = ("✎" + ",".join(row["custom"])) if row["custom"] else ""
            items.append(f"{row['id']}｜{row['title']}{('  ' + mark) if mark else ''}")
        self._prompt_items = items
        self._prompt_rows = PR.status()
        self.prompt_combo["values"] = items
        if items and not self.prompt_choice.get():
            self.prompt_combo.current(0)
            self.do_prompt_load()


    def do_prompt_load(self):
        """把选中的那条读进编辑框。"""
        try:
            PR = self._prompts_mod()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("读不到提示词", str(exc))
            return
        idx = self.prompt_combo.current()
        rows = getattr(self, "_prompt_rows", [])
        if idx < 0 or idx >= len(rows):
            return
        row = rows[idx]
        self._prompt_id = row["id"]
        self.prompt_where.config(
            text=f"用在哪：{row['where']}　｜　占位符："
                 f"{'、'.join('{' + p + '}' for p in row['placeholders']) or '（无）'}"
                 f"　｜　JSON 契约键：{', '.join(row['keys']) or '（无）'}"
                 f"　｜　{'已改过：' + ','.join(row['custom']) if row['custom'] else '当前是默认值'}")
        for widget, text in ((self.prompt_system, row["system"]),
                             (self.prompt_user, row["user"])):
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
        self._prompt_out.delete("1.0", "end")
        self._prompt_out.insert("1.0", "（点「试跑」看模型怎么回这一条）")


    def do_prompt_save(self):
        """保存改动。**校验不过就拒绝保存** —— 原因见 prompts.validate 的文档。"""
        pid = getattr(self, "_prompt_id", "")
        if not pid:
            messagebox.showinfo("先选一条", "先在左上角的下拉框里选一条提示词。")
            return
        try:
            PR = self._prompts_mod()
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("读不到提示词", str(exc))
            return
        plan = [("system", self.prompt_system.get("1.0", "end").strip()),
                ("user", self.prompt_user.get("1.0", "end").rstrip())]
        saved = []
        for field, text in plan:
            err = PR.validate(pid, field, text)
            if err:
                messagebox.showerror("这一条不能保存", f"{pid}.{field}：{err}")
                return
            if text != PR.default(pid, field) or PR.is_custom(pid, field):
                try:
                    PR.save(pid, field, text)
                    saved.append(field)
                except ValueError as exc:
                    messagebox.showerror("保存失败", str(exc))
                    return
        # 用户把某一项改回默认值时，把覆盖删掉（别留一条和默认一样的覆盖）
        for field, text in plan:
            if text == PR.default(pid, field) and PR.is_custom(pid, field):
                PR.reset(pid, field)
                saved.append(field + "（改回默认，已删掉覆盖）")
        self.say(f"[{ts()}] 提示词 {pid} 已保存：{', '.join(saved) or '内容没变'}"
                 f"（改完立即生效，不用重启服务）")
        self.do_prompt_refresh()


    def do_prompt_reset(self):
        pid = getattr(self, "_prompt_id", "")
        if not pid:
            return
        if not messagebox.askyesno("恢复默认", f"把 {pid} 恢复成代码里的默认话术？"):
            return
        try:
            PR = self._prompts_mod()
            PR.reset(pid)
        except Exception as exc:      # noqa: BLE001
            messagebox.showerror("恢复失败", str(exc))
            return
        self.say(f"[{ts()}] {pid} 已恢复默认")
        self.do_prompt_refresh()
        self.do_prompt_load()


    def do_prompt_probe(self):
        """试跑：拿固定样例问一次模型，把提示词全文、输出、JSON 解析结果都摆出来。"""
        pid = getattr(self, "_prompt_id", "")
        if not pid:
            return
        self._prompt_out.delete("1.0", "end")
        self._prompt_out.insert("1.0", f"正在调模型（{pid}）…（本地模型一次要几秒到几十秒）")
        self.say(f"[{ts()}] 试跑提示词 {pid}")

        def work():
            try:
                import sys
                if os.path.join(ROOT, "offline") not in sys.path:
                    sys.path.insert(0, os.path.join(ROOT, "offline"))
                import prompts as PR
                res = PR.probe(pid)
            except Exception as exc:      # noqa: BLE001
                import traceback
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "prompt": "", "output": "",
                       "trace": traceback.format_exc()}
            parts = []
            if res.get("error"):
                parts.append("⚠ " + str(res["error"]))
            parts.append("── 实际送出去的提示词（样例已填进去）──\n"
                         + (res.get("prompt") or "(空)"))
            parts.append("\n── 模型输出 ──\n" + (res.get("output") or "(空)"))
            if res.get("want_json"):
                parts.append("\n── JSON 解析 ──\n"
                             + (res.get("parsed") or "解析失败（这一条在真实任务里会被判失败）"))
            if res.get("trace"):
                parts.append("\n── 调用栈 ──\n" + str(res["trace"])[-800:])
            self.out_queue.put(("show", (self._prompt_out, "\n".join(parts))))

        threading.Thread(target=work, daemon=True).start()
