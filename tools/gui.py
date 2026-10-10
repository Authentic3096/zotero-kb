"""知识库统一管理面板（一个窗口管所有事）。

    python tools/gui.py

为什么做这个：原先要记四个入口（1-convert.cmd / 3-maintain.cmd /
kb_admin.py / judge.py），还得记参数。这里把它们收成一页：
状态一目了然，按钮点一下就干活，日志就在下面。

设计取舍
    · 用 Tkinter：知识库自己的 venv 就带（实测 tkinter 8.6），**零新增依赖**、
      不用装 PyQt/webview。界面朴素但够用。
    · 长任务（建索引、跑模型）一律放**子进程**，不 import 到 GUI 进程里 ——
      否则一次建索引就把界面卡死，而且把索引库连接、numpy、onnxruntime
      全拖进 GUI 进程，退出时容易留下半开连接。
    · 所有输出走队列回主线程渲染，避免 Tk 的跨线程崩溃。
    · 破坏性操作（全量重建、清空经验）都要二次确认。

界面的组织原则（2026-10 大修）
    · **按"你想干什么"分，不按"代码怎么分层"分**。旧版 4 个标签页、40 个按钮、
      6 类不相干的东西挤在一页，用户看着名字不知道是做什么的。
    · 第一屏只放**最常用的**：状态卡片 + 五个核心按钮。
    · 每个文件夹是干什么的 → 单独一页「知识库结构」讲清楚（用户明确要的）。
    · 冷门/危险/一次性的操作 → 收进「高级」页，功能一个不删，只是不挡路。
    · 路径一律来自 schemas 的解析（可被用户在插件设置里覆盖），
      **不再有 `ROOT + "\\kb"` 这种假设** —— 知识库默认不跟代码放一起了。

文件怎么分的（2026-10 拆分）
    这个文件原来 2805 行、一个 App 类 108 个方法；现在只剩**入口**：
    组装 App、命令行自检、mainloop。真正的界面代码按「一个页签一个模块」
    放在 panels/ 下（struct / experience / ai / quality / meta / env / advanced），
    公共的路径与字体在 panels/common.py（offline/ 与 online/ 的 sys.path
    引导也在那儿做一次，别的入口将来也自动生效）。
    改某一页就只看那一个文件 —— 比在 2805 行里找三个地方快得多。

    ⚠ 这个文件必须继续存在：插件和服务端都靠 tools/gui.py 认「项目根在哪」。
"""

from __future__ import annotations

import os
import sys
import tkinter as tk

# 再导出：老引用（tests/test_gui_smoke.py 的 gui.KB_FILE_SPEC 等）不能断
from panels.common import (  # noqa: F401
    HERE,
    ROOT,
    PY,
    VENV_PY,
    KB_FILE_SPEC,
    UI_FAMILY,
    UI_MONO,
    ts,
)
from panels.base import AppBase
from panels.tab_struct import StructTab
from panels.tab_experience import ExperienceTab
from panels.tab_ai import AiTab
from panels.tab_quality import QualityTab
from panels.tab_meta import MetaTab
from panels.tab_outline import OutlineTab
from panels.tab_env import EnvTab
from panels.tab_parse import ParseTab
from panels.tab_advanced import AdvancedTab
from panels.tab_prompts import PromptsTab


class App(AppBase, StructTab, ExperienceTab, AiTab, QualityTab,
          MetaTab, OutlineTab, EnvTab, ParseTab, AdvancedTab, PromptsTab):
    """管理面板主窗口。

    方法按页签分在 panels/ 下的各个 mixin 里；这里只把它们拼起来 ——
    所以「改某一页」只需要打开那一个文件。
    """

def console_check() -> int:
    """不开窗口，只在终端打印状态摘要。

    用途：脚本里检查、排障时确认（"GUI 打不开"时先跑这个，
    能区分是环境问题还是 Tk 问题）。返回 0=正常，1=有问题。
    """
    print("=" * 66)
    print("知识库管理面板 · 无界面自检")
    print("=" * 66)
    problems = []
    # ⚠ 这里印的是**两个不同的位置**，别混：
    #   项目目录 = 代码在哪（本文件所在）
    #   知识库   = 数据在哪（跟着 Zotero 数据目录走，可被用户改）
    #   老代码把 ROOT 当知识库印出来，迁移后会误导人以为库在项目里。
    kb = App.kb_dir()
    print(f"  项目目录：  {ROOT}")
    print(f"  知识库目录：{kb}")
    print(f"  Python：    {PY}")
    if PY != VENV_PY:
        problems.append(f"没找到项目 venv（{VENV_PY}），正在用系统 Python，"
                        f"可能缺少依赖")
        print("  [!!] 未使用项目 venv")
    else:
        print("  [OK] 使用项目 venv")
    if not os.path.isdir(kb):
        problems.append(f"知识库目录不存在：{kb}")
        print(f"  [!!] 知识库目录不存在：{kb}")
    try:
        import tkinter  # noqa: F401
        print(f"  [OK] tkinter 可用（Tk {tkinter.TkVersion}）")
    except ImportError as exc:
        problems.append(f"tkinter 不可用：{exc}")
        print(f"  [XX] tkinter 不可用：{exc}")

    for mod in ("mcp", "numpy", "fastembed"):
        try:
            __import__(mod)
            print(f"  [OK] {mod}")
        except ImportError:
            print(f"  [!!] {mod} 缺失")
            problems.append(f"{mod} 缺失")

    try:
        import schemas as S
        if not os.path.exists(S.INDEX_DB):
            problems.append("索引库不存在，先点「更新索引（增量）」")
            print(f"  [XX] 索引库不存在：{S.INDEX_DB}")
        else:
            conn = S.connect(S.INDEX_DB)
            n_items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
            n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            n_vec = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
            n_exp = conn.execute("SELECT COUNT(*) AS n FROM experience").fetchone()["n"]
            conn.close()
            print(f"  [OK] 索引：{n_items} 篇 / {n_chunks} 切片 / {n_vec} 向量 / "
                  f"{n_exp} 条经验")
            if n_chunks and not n_vec:
                problems.append("向量缺失（语义检索不可用）：点「全量重建」或"
                                "跑 convert.py 补向量")
                print("  [!!] 向量缺失")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"读索引失败：{type(exc).__name__}: {exc}")
        print(f"  [XX] 读索引失败：{exc}")

    # 分级视图（面板「打开知识库」与 Zotero 右键菜单读的就是它）。
    # 它是派生物：缺了功能会退化成"打不开这一层"，所以要在这里看得见。
    try:
        import kbviews as KV
        print(f"  [OK] 知识库分级：{'、'.join(lv['label'] for lv in KV.LEVELS)}")
        if os.path.isdir(S.VIEWS_DIR):
            n = len([f for f in os.listdir(S.VIEWS_DIR) if f.endswith(".md")])
            print(f"       分级视图文件 {n} 份（{S.VIEWS_DIR}）")
            if n == 0:
                problems.append("一份分级视图都没有 —— 跑一次"
                                "「更新索引（增量）」，或 offline\\maintain.py views")
                print("  [!!] 分级视图是空的")
        else:
            problems.append(f"分级视图目录不存在：{S.VIEWS_DIR}")
            print(f"  [!!] 分级视图目录不存在：{S.VIEWS_DIR}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] 读不到分级视图（{type(exc).__name__}: {exc}）")

    print("-" * 66)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    else:
        print("一切正常，可以双击 scripts\\0-panel.vbs 打开面板。")
    print("=" * 66)
    return 1 if problems else 0


def _arg_value(flag: str) -> str:
    """取 `--flag value` 或 `--flag=value` 的值（没有则空串）。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return ""


def main() -> int:
    if "--check" in sys.argv:
        return console_check()

    # --open <路径>：用系统默认程序打开一个文件，然后立刻退出（不开窗口）。
    #
    # 用途：Zotero 插件的右键「打开知识库」需要一个"用默认程序打开这个 md"的
    # 能力，而插件沙箱里没有可靠的接口（试过 launchURL / 外部协议服务，
    # 都可能因版本而异）。这里 `os.startfile` 是本机一直在用、确定可用的那条，
    # 于是插件把它当作兜底：起一个短命的 pythonw 进程，打开文件就退出。
    target = _arg_value("--open")
    if target:
        try:
            if not hasattr(os, "startfile"):       # 非 Windows：给个明确的出路
                print("[XX] --open 只在 Windows 上实现（os.startfile）")
                return 1
            os.startfile(target)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            print(f"[XX] 打不开 {target}：{type(exc).__name__}: {exc}")
            return 1
        return 0

    root = tk.Tk()
    # ⚠ 不再调 `tk scaling` —— 它和显式字号是**乘在一起**生效的：
    #   scaling 1.25 + 雅黑 9 号 ≈ 11 号，中文小字会发虚（用户反馈"看不清"）。
    #   让 Tk 按系统 DPI 自己算。
    app = App(root)

    # --tab <名称> ：启动时直接切到某一页。
    # 用途：截图核对界面（Tk 的 Notebook 从外部没法可靠地切页，
    # 只能靠鼠标点坐标，窗口一改大小就失效），也方便以后做自动化 UI 检查。
    #
    # ⚠ 按**页签名**去找下标，不再写死数字。原来那份写死的表已经过期了：
    #   它把「高级」写成 5，而高级其实已经是第 7 页（中间加了「元数据」），
    #   于是 `--tab 高级` 会切到「元数据」。凡是"页数变了要记得改这里"的
    #   约定最后都会过期，所以改成从 notebook 自己读。
    want = _arg_value("--tab")
    if want:
        alias = {"struct": "知识库结构", "exp": "经验库", "ai": "分类建议",
                 "env": "运行环境", "parse": "PDF 解析",
                 "quality": "损坏查询", "meta": "元数据",
                 "outline": "分节纲要",
                 "adv": "高级", "prompts": "提示词"}
        title = alias.get(want, want)
        titles = [app.notebook.tab(t, "text").strip()
                  for t in app.notebook.tabs()]
        if title in titles:
            try:
                app.notebook.select(titles.index(title))
            except tk.TclError:
                pass
        else:
            print(f"[!!] --tab {want}：没有这个页签。可用：{'、'.join(titles)}")

    # --mineru-guide ：直接打开「MinerU 安装引导」窗口。
    #
    # 谁在用：Zotero 插件首次启动检测到**没装** MinerU 时弹的那个对话框，
    # 选「打开安装引导」就会带这个参数拉起面板 —— 用户不用自己去翻菜单。
    # 面板里也有入口：「知识库结构」页那个只在未安装时出现的按钮。
    # ⚠ 这是个**开关**（没有值），所以不能用 `_arg_value` 判"非空"。
    if "--mineru-guide" in sys.argv:
        try:
            from panels.mineru_guide import MineruGuide
            MineruGuide(root, app, on_done=getattr(app, "_check_mineru_button",
                                                   None))
        except Exception as exc:      # noqa: BLE001
            print(f"[XX] 打不开 MinerU 安装引导：{type(exc).__name__}: {exc}")

    # --ollama-guide ：同上，打开 Ollama 安装引导（可选组件，2026-10-05 加）
    if "--ollama-guide" in sys.argv:
        try:
            from panels.ollama_guide import OllamaGuide
            OllamaGuide(root, app, on_done=getattr(app, "_check_ollama_button",
                                                   None))
        except Exception as exc:      # noqa: BLE001
            print(f"[XX] 打不开 Ollama 安装引导：{type(exc).__name__}: {exc}")

    app.say(f"[{ts()}] 面板就绪。")
    app.say(f"[{ts()}] 项目目录：{ROOT}")
    app.say(f"[{ts()}] 知识库：  {App.kb_dir()}")
    # 运行环境与**来源**：开机打一次（用户要求）。改了 .env / 环境变量却"没生效"
    # 时，第一眼就该能在日志里看到每个值从哪来。
    try:
        import schemas as S
        for line in S.env_report_lines():
            app.say(f"[{ts()}] {line}")
    except Exception as exc:      # noqa: BLE001 —— 环境报告不该挡面板启动
        app.say(f"[{ts()}] [!!] 运行环境报告生成失败：{type(exc).__name__}: {exc}")
    app.say(f"[{ts()}] 提示：长任务在后台跑，日志会实时出现在这里。")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
