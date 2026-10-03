"""GUI 冒烟测试：验证窗口能装配、标签页齐备、按钮都在，但不进主循环。

    python tests/test_gui_smoke.py

为什么要这个测试：Tkinter 的错误大多发生在装配阶段（比如 `pack` 到不存在的
父容器、`StringVar` 用了错的 master、某个回调引用未定义的名字），
而这些错误只有真的把窗口建出来才会暴露。手工点一遍很费事，
所以这里自动建、自动检查、自动关。

注意：需要图形环境。无显示环境（纯 SSH）下会 skip 而不是失败。
"""

from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


try:
    import tkinter as tk
except ImportError as exc:
    print(f"  SKIP  tkinter 不可用：{exc}")
    sys.exit(0)

root = None
try:
    root = tk.Tk()
    root.withdraw()          # 不弹窗，只装配
except tk.TclError as exc:
    print(f"  SKIP  无图形环境：{exc}")
    sys.exit(0)

print("[1] 装配主窗口")
try:
    import gui  # noqa: E402

    app = gui.App(root)
    root.update_idletasks()
    check("App 装配成功", True)
except Exception as exc:  # noqa: BLE001
    check("App 装配成功", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    root.destroy()
    print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
    sys.exit(1)

print("\n[2] 关键部件")
check("状态栏变量可用", isinstance(app.status_var.get(), str), app.status_var.get()[:60])
check("日志区存在", app.log is not None)
for name in ("exp_text", "ai_text", "adv_text"):
    check(f"{name} 存在", getattr(app, name, None) is not None)

print("\n[3] 标签页齐备")
# 2026-10 面板大修后的分页：按"你想干什么"分，不按代码分层分。
# ⚠ 标签文本带空格缩进（"  经验库  "），断言用 in 而不是 ==。
tabs = app.notebook.tabs()
titles = [app.notebook.tab(t, "text").strip() for t in tabs]
# ⚠ 数量会随功能增加（2026-10 加了「切片质量」页 → 6 个，
#   本轮加了「元数据」页 → 7 个）。
#   所以断言写成"至少"这些页都在，而不是卡死总数 ——
#   否则每加一页都要改测试，久了就没人认真看这个测试了。
check("标签页 ≥ 7", len(tabs) >= 7, str(titles))
for want in ("知识库结构", "经验库", "分类建议", "运行环境", "解析健康",
             "元数据", "高级"):
    check(f"有「{want}」页", want in titles, str(titles))

# 用户明确要的两页必须在：知识库结构表（各文件夹存什么）、运行环境（三个路径）
check("结构表控件存在", getattr(app, "struct_tree", None) is not None)
# 切片质量页：确保不是"加了个空页"（控件与动作都在）
check("解析健康页有结果表", getattr(app, "q_tree", None) is not None)
check("解析健康页有检查按钮", getattr(app, "q_check_btn", None) is not None)
check("解析健康页有修复按钮", getattr(app, "q_fix_btn", None) is not None)
check("解析健康页有「重建+修复」按钮",
      getattr(app, "q_chain_btn", None) is not None)
check("解析健康页有图表开关", getattr(app, "q_figures_var", None) is not None)
check("运行环境三个路径都有显示", len(getattr(app, "env_vars", {})) == 3,
      str(list(getattr(app, "env_vars", {}).keys())))

# 元数据页（本轮新增，"一键补全"就在这里）
check("元数据页有结果表", getattr(app, "meta_tree", None) is not None)
check("元数据页有「一键补全」按钮",
      getattr(app, "meta_scan_btn", None) is not None)
check("元数据页有「用本地模型补全」按钮",
      getattr(app, "meta_model_btn", None) is not None)
check("元数据页有「取消」按钮", getattr(app, "meta_cancel_btn", None) is not None)
check("元数据页有「检查类型/标题污染」按钮",
      getattr(app, "meta_typefix_btn", None) is not None)
# ⚠ 取消按钮在没跑任务时必须是**灰的**，否则用户点了不知道发生了什么
check("「取消」按钮初始是灰的",
      str(app.meta_cancel_btn.cget("state")) == "disabled",
      str(app.meta_cancel_btn.cget("state")))
# ⚠ 反向断言：面板里**不许**再有"写回 Zotero"的按钮。
#   用户拍板：写库统一留在插件里（面板是另一个 Python 进程，写不了 Zotero 库）。
#   这条断言是防止以后有人"顺手"把写库加回面板。
_stray = [n for n in dir(app) if "apply" in n.lower()
          and "meta" in n.lower()]
check("面板里没有批量写库的按钮/方法（写库只在插件里）", not _stray, str(_stray))

print("\n[4] 按钮回调都可调用（只验证可调用，不真执行）")
callbacks = {
    "更新索引": app.do_convert_incremental,
    "全量重建": app.do_convert_full,
    "自检": app.do_check,
    "统计": app.do_stats,
    "备份": app.do_backup,
    "清残留": app.do_clean_test,
    "列经验": app.do_exp_all,
    "权重列表": app.do_weight_list,
    "搜索": app.do_search,
    "模型状态": app.do_ai_status,
    "分类列表": app.do_coll_list,
    "写入能力": app.do_write_check,
    "技能同步": app.do_sync_skill,
    "分类建议": app.do_taxonomy,
    "一键补全": app.do_meta_scan,
    "用本地模型补全": app.do_meta_scan_model,
    "取消批量扫描": app.do_meta_cancel,
    "检查类型/标题污染": app.do_typefix_scan,
    "看某篇建议明细": app.do_meta_detail,
}
for name, fn in callbacks.items():
    check(f"{name} 可调用", callable(fn))

print("\n[5] 日志与显示接口")
app.say("自检写入的一行")
check("say() 能写日志", "自检写入的一行" in app.log.get("1.0", "end"))
app.show(app.exp_text, "测试内容")
check("show() 能写内容区", app.exp_text.get("1.0", "end").strip() == "测试内容")

# 头像：面板顶部的署名图。丢了不该报错（_build_header 里是 try 包着的），
# 但**应该被发现** —— 它只是装饰，坏了最容易没人注意。
_avatar = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tools", "assets", "avatar.png")
check("头像资源存在（tools/assets/avatar.png）", os.path.exists(_avatar), _avatar)
if os.path.exists(_avatar):

    def _avatar_size(path):
        """(宽, 高)；读不出来返回 (0, 0)。优先 Pillow，退回 PNG 文件头。"""
        try:
            from PIL import Image                       # noqa: PLC0415
            with Image.open(path) as im:
                return im.size
        except Exception:                               # noqa: BLE001
            pass
        try:
            import struct                               # noqa: PLC0415
            with open(path, "rb") as f:
                head = f.read(24)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
        except Exception:                               # noqa: BLE001
            pass
        return (0, 0)

    _w, _h = _avatar_size(_avatar)
    check("头像是正方形且够 HiDPI（边长 >= 96）",
          _w == _h and _w >= 96, f"得 {_w}x{_h}")
check("头像加载器把它读出来了（_load_avatar）",
      app._avatar_img is not None or not os.path.exists(_avatar),
      "头像文件在，但 _load_avatar 没返回对象")

print("\n[6] 状态刷新路径（后台线程读库，等它回填）")
import time  # noqa: E402

app.refresh_status()
deadline = time.time() + 8.0
while time.time() < deadline:
    root.update()          # 让 Tk 处理事件（别用 after+sleep，那样不会推进）
    status = app.status_var.get()
    if status and "读取状态" not in status:
        break
    time.sleep(0.05)
status = app.status_var.get()
check("状态读到了内容", bool(status) and "读取状态" not in status, status[:80])
check("状态包含条目数", "篇" in status, status[:80])

root.destroy()

# ---------------------------------------------------------------- 7. 导入路径
# 回归测试：面板里点「列出全部经验」曾经报
#     ModuleNotFoundError: No module named 'searcher'
# 因为 gui.py 只把 offline\ 加进了 sys.path，而 searcher.py 在 online\ 下。
# 这类"漏加一个路径"以后很容易再犯，所以在这里钉住三件事：
#   ① online\ 在路径里   ② searcher 能导入   ③ 面板那条查询真能跑
print("\n[6b] 知识库目录说明表：每个文件都要有说明")
# 为什么值得断言：以前这张表藏在 refresh_struct 里，新出现的文件（备份、
# 诊断日志…）只会显示成「（未在说明表里的文件，可能是新版本新增的）」——
# 用户实机反馈过这个。现在表提到了模块级，就能在这里钉住。
import fnmatch  # noqa: E402

import schemas as _S  # noqa: E402

_known = {n.rstrip("\\/").lower()
          for n, _w, _k, _s in gui.KB_FILE_SPEC if _k != "glob"}
_patterns = [n.lower() for n, _w, _k, _s in gui.KB_FILE_SPEC if _k == "glob"]
try:
    _kb_entries = os.listdir(_S.KB_DIR)
except OSError as _e:
    _kb_entries = []
    print(f"  （跳过：读不到知识库目录 {_e}）")
if _kb_entries:
    _undesc = [n for n in _kb_entries
               if n.lower() not in _known
               and not any(fnmatch.fnmatch(n.lower(), g) for g in _patterns)]
    check("知识库目录里每个文件都有说明", not _undesc,
          f"没说明的：{_undesc}")
    # glob 条目（如 index.db.bak*）匹配到 0 个文件是**正常的** ——
    # 那些历史备份被清理掉之后就一个都不剩了。所以这里只在"匹配到了"时
    # 报个数，不匹配不算失败；真正要钉住的是上面那条"每个文件都有说明"。
    # ⚠ 原来这里写成"一个都没匹配上就是错"，结果清理掉备份后测试假失败。
    _gmatch = [n for n in _kb_entries
               if any(fnmatch.fnmatch(n.lower(), g) for g in _patterns)]
    if _patterns:
        print(f"  （glob 条目 {_patterns} 当前匹配到 {len(_gmatch)} 个文件）")
    # 说明文字里不该出现 markdown 星号（Tk 表格会原样显示）
    _starred = [n for n, w, _k, _s in gui.KB_FILE_SPEC
                if "**" in w or "**" in _s]
    check("说明里没有 markdown 星号（Tk 会原样显示）", not _starred, _starred)

print("\n[7] 导入路径与经验查询（防 ModuleNotFoundError 回归）")
online_dir = os.path.join(ROOT, "online")
check("online 目录在 sys.path 里",
      any(os.path.normcase(p) == os.path.normcase(online_dir) for p in sys.path),
      f"sys.path 里没有 {online_dir}")

try:
    from searcher import Searcher  # noqa: E402

    _s = Searcher()
    # 这正是面板「列出全部经验」执行的那条查询
    _rows = [dict(r) for r in _s.read("SELECT * FROM experience ORDER BY id")]
    check("能 import searcher 并查经验表", isinstance(_rows, list),
          f"返回 {type(_rows).__name__}")
    if _rows:
        # 面板模板里用到的字段必须都在（缺了渲染时会 KeyError）
        _need = ["id", "outcome", "created_at", "asked", "method",
                 "reason", "evidence", "item_keys"]
        _missing = [f for f in _need if f not in _rows[0]]
        check("经验行字段齐全（面板模板渲染用）", not _missing, f"缺 {_missing}")
    else:
        check("经验行字段齐全（面板模板渲染用）", True, "（表为空，跳过）")
    _s.close()
except Exception as exc:  # noqa: BLE001
    check("能 import searcher 并查经验表", False, f"{type(exc).__name__}: {exc}")

print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
