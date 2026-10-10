"""common.py —— 面板各页签共用的东西：路径、字体、知识库目录说明表。

入口是 tools/gui.py（`python tools/gui.py`），界面代码按页签拆在同目录下的
tab_*.py 里，本模块只放**所有页签都要用**的那几样：

    HERE / ROOT        项目根在哪（见下面那条注释，别用 __file__ 直接推）
    PY / VENV_PY       用哪个 Python 跑子进程
    ENV                子进程的环境变量（UTF-8 + 国内镜像）
    UI_FAMILY/MONO_FONT + pick_ui_font/apply_ui_fonts   字体
    ts()               日志前缀用的时间戳
    KB_FILE_SPEC       「知识库结构」页那张表的内容

⚠ 顺便在这里做 **offline\\ 与 online\\ 的 sys.path 引导**（原来在 gui.py 的
  脚本顶部）。放在包的入口处更可靠：它靠"脚本所在目录自动进 sys.path[0]"才
  成立，换个启动方式（服务端用 Subprocess 拉起、别的工具 import 它）就不一定。

设计取舍（沿用面板一直以来的口径）
    · 用 Tkinter：知识库自己的 venv 就带（实测 tkinter 8.6），**零新增依赖**、
      不用装 PyQt/webview。界面朴素但够用。
    · 长任务（建索引、跑模型）一律放**子进程**，不 import 到 GUI 进程里 ——
      否则一次建索引就把界面卡死，而且把索引库连接、numpy、onnxruntime
      全拖进 GUI 进程，退出时容易留下半开连接。
    · 所有输出走队列回主线程渲染，避免 Tk 的跨线程崩溃。
    · 破坏性操作（全量重建、清空经验）都要二次确认。
    · 路径一律来自 schemas 的解析（可被用户在插件设置里覆盖），
      **不再有 `ROOT + "\\kb"` 这种假设** —— 知识库默认不跟代码放一起了。
"""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

# ⚠ 这个文件在 tools/panels/ 下，所以 ROOT 要往上数**两层**才到项目根
_PKG = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(_PKG)          # 就是 tools/
ROOT = os.path.dirname(HERE)          # 项目根
# ⚠ 两个目录都要加：
#   offline\ —— schemas / convert / judge 等
#   online\  —— searcher / query（面板里查经验、列权重都要用）
# 本机踩过：只加了 offline，结果点「列出全部经验」报
#   ModuleNotFoundError: No module named 'searcher'
# 因为 searcher.py 在 online\ 下。凡是 import 这两个目录里的模块，
# 都得先确保这里都加上了。
for _sub in ("offline", "online"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
# 有些模块（如 searcher）内部按 "from schemas import ..." 这种平铺方式导入，
# 而 schemas 在 offline\ 下 —— 上面已经加了，这里不用额外处理。

VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable
ENV = {
    **os.environ,
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "HF_ENDPOINT": "https://hf-mirror.com",
    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
}
# Windows 下别弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------- 字体
#
# 为什么必须显式设字体（用户反馈"有的字看不清"、"字的间距看着很奇怪"）：
#   Tk 不指定字体时，不同控件会各用各的默认字体 —— Label 用 TkDefaultFont、
#   表格用 TkTextFont、等宽处用 TkFixedFont。混着来的后果是**同一个小标签
#   在雅黑/宋体/Arial 之间跳**，同一行里字号和字距都不一样，看着就"奇怪"。
#   更糟的是用 `("", 9, "bold")` 这种写法：空字体名 → 拿 TkDefaultFont
#   再强行加粗，而 TkDefaultFont 在中文 Windows 上不一定是雅黑，
#   加粗后中文字距会明显挤在一起（用户说的"应该是加粗的问题"）。
#
# 所以：先挑一个系统里**真的有**的中文字体，再把所有命名字体统一改掉。
UI_FAMILY = "TkDefaultFont"          # 兜底：保持 Tk 原样
UI_MONO = "TkFixedFont"


def pick_ui_font(root: tk.Misc) -> tuple[str, str]:
    """选一个存在的中文字体，返回 (界面字体, 等宽字体)。

    优先 微软雅黑 —— 它是 Windows 上小字号中文最清楚的（带 hinting），
    宋体在小字号下发虚、黑体字重过重。取不到就退回 Tk 默认值。
    """
    try:
        from tkinter import font as tkfont
        have = {f.lower() for f in tkfont.families(root)}
    except tk.TclError:
        return UI_FAMILY, UI_MONO
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                 "Noto Sans SC", "Source Han Sans SC", "SimHei", "SimSun"):
        if name.lower() in have:
            ui = name
            break
    else:
        ui = UI_FAMILY
    for name in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"):
        if name.lower() in have:
            mono = name
            break
    else:
        mono = UI_MONO
    return ui, mono


def apply_ui_fonts(root: tk.Tk) -> tuple[str, str]:
    """把所有 Tk 命名字体统一成选定的界面字体。返回 (ui, mono) 供后续用。

    ⚠ 只改**命名字体**（TkDefaultFont / TkTextFont / ...），不改字体大小 ——
      大小由下面的 `tk scaling` 统一缩放，两个一起改会把字号乘两次。
    """
    ui, mono = pick_ui_font(root)
    try:
        from tkinter import font as tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont", "TkTooltipFont", "TkIconFont"):
            try:
                tkfont.nametofont(name).configure(family=ui)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkFixedFont").configure(family=mono)
        except tk.TclError:
            pass
    except ImportError:
        pass
    # ttk 控件（Button/Label/Entry/Treeview）不受命名字体影响，
    # 得单独配 Style —— 本机实测：不配的话按钮还是老面板字体。
    try:
        style = ttk.Style(root)
        for cls in ("TButton", "TLabel", "TEntry", "TCheckbutton",
                    "TRadiobutton", "TLabelframe.Label", "TNotebook.Tab",
                    "TCombobox"):
            style.configure(cls, font=(ui, 10))
        style.configure("Treeview", font=(ui, 10), rowheight=24)
        style.configure("Treeview.Heading", font=(ui, 10, "bold"))
    except tk.TclError:
        pass
    return ui, mono


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ================================================================ 知识库目录里的文件说明
#
# 每一项：(名称, 说明, 计算方式, 能删吗)
#   计算方式：db = 只算主库单文件；dir = 递归数文件；file = 单文件；
#             glob = 一类文件合并成一行（多份、同样大，逐行列反而看不清）
#
# ⚠ 放在**模块级**而不是函数里：这样测试能直接断言
#   "目录里每个文件都有说明"（见 tests/test_gui_smoke.py）。
#   以前它藏在 refresh_struct 里，新出现的文件只能显示成
#   "（未在说明表里的文件，可能是新版本新增的）"—— 用户看到的就是这个。
#
# ⚠ 说明里**不要用 markdown 的 `**加粗**`** —— 这是 Tk 表格，
#   星号会原样显示出来（第一版就犯了，界面上出现了 "**唯一不能丢的文件**"）。
#   要强调就用中文书名号或括号。（插入前还会统一剥一遍，双保险。）
KB_FILE_SPEC = [
    ("index.db", "整个索引：条目、切片、全文检索表、向量、经验、权重。"
                 "（唯一不能丢的文件）", "db", "✗ 绝对不能删，先备份"),
    # WAL 边车文件：服务一跑就会出现，用户看到目录里多出两个陌生文件会问
    # "这俩是什么、能不能删"。用 glob 合并成一行（两个文件是一回事），
    # 体积也由这一行负责 —— index.db 那一行只算主库，免得「合计」算两遍。
    ("index.db-*", "SQLite 的 WAL 边车文件：-wal 是还没并回主库的新写入，"
                   "-shm 是它的共享内存索引。只在服务运行时出现，"
                   "停掉服务后一般会自己消失", "glob",
     "✗ 别单独删（跟 index.db 是一体的）"),
    ("INDEX.md", "文献清单（给人看的）：全部文献按年份分组的表格，"
                 "一眼认出哪篇是哪篇。AI 检索不读它", "file",
     "○ 可删，「更新索引」会自动重建"),
    ("papers\\", "每篇文献一份档案：元数据 + 摘要 + 笔记 + 标注 + 正文首段。"
                 "按 Zotero key 命名（如 22X9PMR6.md）", "dir",
     "○ 可删，重新索引会再生成"),
    ("fulltext\\", "每篇的正文，带「## p.N」页码锚点。"
                   "AI 按页读正文用的就是它", "dir",
     "○ 可删，重新索引会再生成"),
    ("views\\", "每篇的分级视图：摘要与要点 / 图注与表格 / 权重与经验"
                "（按 Zotero key 命名，如 22X9PMR6.tldr.md）。"
                "面板的「打开知识库」与 Zotero 右键菜单读的就是它", "dir",
     "○ 可删，跑一次「手动更新」或 offline\\maintain.py views 就回来"),
    # MinerU（**可选组件**）的解析产物：2026-10-05 接进转换管道后新增的目录。
    # 它是"重解析一次要几十分钟"的那类东西 —— 说清"能不能删"很重要：
    # 删了只是下次要重解析（正文/切片不受影响，它们已经写进 index.db 与 papers/）。
    ("mineru\\", "每篇的 MinerU 解析产物（markdown / 按页结构 / 公式与图表切图 / "
                 "指纹 meta.json）。只在装了可选组件 MinerU 时才存在；"
                 "面板「PDF 解析」页可删单篇或看状态", "dir",
     "△ 删了要重新解析（不影响已有正文与检索）"),
    # T0-1（两段式删除）新增的归档目录：Zotero 里被删的文献先搬到这里而不是当场
    # 销毁，用户从回收站还原时还能原样搬回去。它是"能不能删"最需要说清的一个 ——
    # 目录一直在变（归档进、彻底删出），所以写明白"什么时候它才真的没用"。
    ("trash\\", "Zotero 里已删除（进回收站）文献的归档：分级视图 / 档案 / 正文 / "
                "MinerU 产物，外加还原用的行快照。从 Zotero 回收站还原时会自动搬回去；"
                "在 Zotero 里彻底删除后，这里的内容才真正没用了", "dir",
     "△ 删了就还原不了（还原是自动的，不用手动搬回来）"),
    ("inbox\\", "待确认的经验、分类建议、以及「扫到哪了」的进度记录",
     "dir", "△ 看看再删（可能有没确认的经验）"),
    ("logs\\", "运行日志：服务日志、面板报错日志", "dir", "✓ 随便删"),
    (".cache\\", "嵌入模型的临时缓存与下载内容，可重新下载", "dir",
     "✓ 可删（下次要重新下载模型）"),
    ("MANIFEST.json", "上次构建的统计与警告（哪篇没抓到全文之类）",
     "file", "✓ 可删，下次构建重写"),
    ("llm-config.json", "模型配置（provider / 模型名 / API Key）。"
                        "含密钥，别分享出去", "file", "△ 删了要重新配模型"),
    ("service-token.txt", "本机服务与 Zotero 插件之间的通行证"
                          "（自动生成，跟模型 key 无关）", "file",
     "△ 删了插件要重新握手"),
    ("bridge-token.txt", "知识库与 DSH 之间的通行证（同上，自动生成）",
     "file", "△ 同上"),
    ("zotero-api-key.txt", "连 Zotero 本地 API 用的授权码"
                           "（分类重整时申请的那次）", "file",
     "△ 删了要重新授权"),
    ("pending-suggestions.json", "分类建议的待确认队列", "file",
     "△ 删了建议就没了"),
    ("taxonomy-plan.json", "上次分类重整的干跑计划", "file", "✓ 可删"),
    ("watch-baseline.json", "「新文献监听」的比对基线", "file",
     "✓ 可删（下次重新建立基线）"),
    ("plugin-status.json", "Zotero 插件写出来的运行状态（排错用）",
     "file", "✓ 可删"),
    ("profile-backup\\", "Zotero profile 的配置备份（升级前检查时生成）",
     "dir", "✓ 确认升级没问题后可删"),
    ("backups\\", "索引备份（含经验层）。备份命令的输出", "dir",
     "△ 保留最近几个"),
    # 历史备份：各工具在动索引之前自动留的（改页眉过滤 / 体检 / 图注抽取…）。
    # ⚠ 合并成一行而不是逐个列：每份 40 MB 上下，五行一模一样的 43.8 MB
    #   反而看不出"它才是占地方的那个"。
    ("index.db.bak*", "索引的历史备份：各工具在改动索引前自动留的底"
                      "（改页眉过滤、解析体检、图注抽取…）。"
                      "确认新索引没问题后就能删旧的", "glob",
     "△ 占地方，确认没问题后删旧的"),
    ("settings-init.log", "Zotero 设置面板的初始化诊断日志"
                          "（面板打不开、配置存不进去时看它）", "file",
     "✓ 可删"),
]
