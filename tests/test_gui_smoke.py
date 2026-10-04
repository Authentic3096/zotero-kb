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
check("标签页 ≥ 8", len(tabs) >= 8, str(titles))
for want in ("知识库结构", "经验库", "分类建议", "运行环境", "损坏查询",
             "元数据", "高级", "提示词"):
    check(f"有「{want}」页", want in titles, str(titles))

# ---- 布局（用户 2026-10-05：「很多东西要拉长面板才看得到，应该能滚轮滑看」
#      「运行日志只能显示几行，应该能拉长」）
# 这两条都是**装配阶段**就能验的：每个页签要套一层可滚动画布，
# 日志与页签要在同一个竖直分栏里（分隔线可拖）。
check("每个页签都套了可滚动画布（滚轮能滑看）",
      len(getattr(app, "_scroll_hosts", {})) == len(tabs),
      f"画布 {len(getattr(app, '_scroll_hosts', {}))} 个 / 页签 {len(tabs)} 个")
check("页签与运行日志在同一个可拖分栏里",
      getattr(app, "_paned", None) is not None
      and len(app._paned.panes()) == 2,
      str(app._paned.panes() if getattr(app, "_paned", None) else None))
for name in ("exp_out", "ai_out", "adv_out", "prompt_out"):
    check(f"页签内有可拉的输出格（{name}）", getattr(app, name, None) is not None)

# 提示词页（本轮新增）：模型在按什么话术干活，用户得看得见、改得动。
# ⚠ 按**控件**与**回调**查，而不是查方法名 —— 方法在但控件没画出来，
#   用户一样用不上（这一条是本项目踩过多次的老毛病）。
check("提示词页有下拉框", getattr(app, "prompt_combo", None) is not None)
check("提示词页有 system 编辑框", getattr(app, "prompt_system", None) is not None)
check("提示词页有 user 编辑框", getattr(app, "prompt_user", None) is not None)
check("提示词页列出了全部提示词",
      len(getattr(app, "prompt_combo", None).cget("values") or ()) >= 9,
      str(getattr(app, "prompt_combo", None).cget("values")))
for fn in ("do_prompt_refresh", "do_prompt_load", "do_prompt_save",
           "do_prompt_reset", "do_prompt_probe"):
    check(f"提示词页有回调 {fn}", callable(getattr(app, fn, None)))

# 用户明确要的两页必须在：知识库结构表（各文件夹存什么）、运行环境（三个路径）
check("结构表控件存在", getattr(app, "struct_tree", None) is not None)
# 切片质量页：确保不是"加了个空页"（控件与动作都在）
# ⚠ 页名 2026-10-05 由「解析健康」改为「损坏查询」（用户要求）
check("损坏查询页有结果表", getattr(app, "q_tree", None) is not None)
check("损坏查询页有检查按钮", getattr(app, "q_check_btn", None) is not None)
check("损坏查询页有修复按钮", getattr(app, "q_fix_btn", None) is not None)
check("损坏查询页有「重建+修复」按钮",
      getattr(app, "q_chain_btn", None) is not None)
# 逐段检查的进度与正文修正（窗格那条链的产物）也要在面板里管得着：
# 改错了要能撤销、进度乱了要能清空。
check("损坏查询页有「逐段进度与正文修正」按钮",
      getattr(app, "q_para_btn", None) is not None)
check("有 do_para_review 回调", callable(getattr(app, "do_para_review", None)))
try:
    from panels.para_review import ParaReview  # noqa: F401
    check("逐段复核窗能 import", True)
except Exception as exc:      # noqa: BLE001
    check("逐段复核窗能 import", False, str(exc))
check("损坏查询页有图表开关", getattr(app, "q_figures_var", None) is not None)
check("运行环境三个路径都有输入框", len(getattr(app, "env_vars", {})) == 3,
      str(list(getattr(app, "env_vars", {}).keys())))
# 用户 2026-10-05：「运行环境的设置能就在面板改吗（与插件设置同步）」
# → 可以：两边读写同一份 kb-location.json。装配阶段能验的是"框和按钮画出来了"。
check("运行环境有保存回调（写同一份配置）",
      callable(getattr(app, "do_save_env", None)))
check("运行环境有「浏览…」回调", callable(getattr(app, "do_browse_env", None)))


# 「打开知识库」的两个入口必须在**看得见的地方**（用户 2026-10-05 提的需求：
# 知识库目录里是 22X9PMR6.md 这种编号，靠人自己去翻等于没解决）。
# 这条按**控件文案**查，而不是查方法名 —— 方法在但按钮没画出来，用户一样用不上。
def _button_texts(widget):
    """递归收集窗口里所有按钮的文案。"""
    out = []
    for child in widget.winfo_children():
        try:
            if child.winfo_class() == "TButton":
                out.append(str(child.cget("text")))
        except tk.TclError:
            pass
        out.extend(_button_texts(child))
    return out


_btns = _button_texts(root)
# ⚠ 名字是用户定的：「打开知识库」**不要省略号**（点出来的是列表，
#   不是"还要再填参数"的对话框）；"打开知识库目录"改成「文献管理器中查看」
#   （原名看不出是在资源管理器里打开）。
check("有「打开知识库」按钮（第一屏核心按钮里）",
      "打开知识库" in _btns, str(sorted(set(_btns))[:24]))
check("按钮名里没有「打开知识库…」（省略号已按用户要求去掉）",
      "打开知识库…" not in _btns)
check("有「文献管理器中查看」按钮", "文献管理器中查看" in _btns,
      str(sorted(set(_btns))[:24]))
check("有「补齐知识库分级文件」按钮（高级页）",
      any("补齐" in t for t in _btns), str(sorted(set(_btns))[:24]))
check("运行环境页有「保存并重新检测」按钮", "保存并重新检测" in _btns)
check("「打开知识库」有回调", callable(getattr(app, "open_kb_browser", None)))
check("「补齐分级文件」有回调", callable(getattr(app, "do_views", None)))

# 经验库页（本轮新增三个入口：编辑、体检、手动选会话）。
# 用户的诉求是"经验用户难以直接修改和添加" —— 所以按钮**必须画出来**，
# 光有方法名不算（本项目踩过"方法在、按钮没画"的坑）。
_exp_btns = [t for t in _btns if t in ("修改/增添经验…", "经验体检", "选择会话…")]
check("经验库页有「修改/增添经验…」按钮", "修改/增添经验…" in _btns, str(_exp_btns))
check("经验库页有「经验体检」按钮", "经验体检" in _btns, str(_exp_btns))
check("经验库页有「选择会话…」按钮", "选择会话…" in _btns, str(_exp_btns))
for fn in ("do_exp_edit", "do_exp_checkup", "do_exp_delete", "do_pick_sessions",
           "do_exp_edit_selected", "do_exp_approve", "do_exp_reject"):
    check(f"经验库页有回调 {fn}", callable(getattr(app, fn, None)))
# ⚠ 经验库页**只有一个列表**（查询/全部/待确认/体检共用），底部那个只作详情。
#   用户反馈过"上下两个显示经验的窗口…列出全部没法选中，两个是不是冗余了"，
#   所以这里按"有列表 + **没有**第二套列表控件"来断言。
check("经验库页有列表（Treeview）", getattr(app, "exp_tree", None) is not None)
check("经验库页**没有**第二套列表控件（旧的体检 Listbox）",
      getattr(app, "exp_suspect_list", None) is None)
check("详情区还在（列表导航 + 详情内容）",
      getattr(app, "exp_text", None) is not None)
check("列表所有视图共用（记住当前视图，删完刷新同一个）",
      isinstance(getattr(app, "exp_view", None), tuple), str(getattr(app, "exp_view", None)))
# 编辑器与选会话对话框要能 import（它们的 import 失败只在点按钮时才暴露，
# 那时候用户看到的是"点了没反应"）
try:
    from panels.exp_editor import ExperienceEditor, SessionPicker  # noqa: F401
    check("经验编辑器/会话选择器能 import", True)
except Exception as exc:      # noqa: BLE001
    check("经验编辑器/会话选择器能 import", False, str(exc))

# 「修改/增添经验…」→「选文献…」弹出来的列表**不许是空的**（用户报过：
# 列表打开默认是空的，别处都是"默认列全部文献"）。
# 根因是行形状与 PaperPicker.COLUMNS 不一致（字典 vs 6 元组）—— 那会让
# `_fill` 解包时抛错，而窗口已经建出来了：看着就是"空列表、无报错"。
# 所以这里对着 COLUMNS 核对形状（**不是**只看函数能不能调）。
try:
    from paper_picker import PaperPicker as _PP     # noqa: E402
    from panels.exp_editor import ExperienceEditor as _EE  # noqa: E402

    _rows = _EE.picker_rows()
    _shape_ok = all(len(r) == len(_PP.COLUMNS) for r in _rows)
    check("选文献弹窗的行形状与 PaperPicker.COLUMNS 一致",
          _shape_ok and len(_PP.COLUMNS) == 6,
          f"{len(_rows)} 行，示例 {(_rows[0] if _rows else None)}")
    check("选文献弹窗默认列出**全部**文献（不是空的）",
          bool(_rows), "库里一篇都没读到")
except Exception as exc:      # noqa: BLE001
    check("选文献弹窗的行形状与 PaperPicker.COLUMNS 一致", False,
          f"{type(exc).__name__}: {exc}")
# 队列泵要认 "call"：后台线程把结果交给主线程控件全靠它，
# 少了这个分支消息会被**静默丢掉**（表现是"点了没反应"）。
check("队列泵支持 call（跨线程回主线程）",
      '"call"' in open(os.path.join(ROOT, "tools", "panels", "base.py"),
                       encoding="utf-8").read())

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

print("\n[6c] 打开知识库（分级浏览）")
# 级别定义在 offline/kbviews.py 里 —— 面板与 Zotero 插件共用这一份事实来源
# （插件侧那份由 tools/check_kb_levels.py 盯着一致）。
try:
    import kbviews as _KV  # noqa: E402

    _ids = [lv["id"] for lv in _KV.LEVELS]
    check("级别定义齐备", len(_ids) >= 5, str(_ids))
    for _want in ("tldr", "card", "fulltext", "figures", "weight"):
        check(f"有「{_want}」级别", _want in _ids, str(_ids))
    # 每个级别的相对路径模板都要能用 key 填出绝对路径
    _k0 = ""
    try:
        _row = _S.connect(_S.INDEX_DB).execute(
            "SELECT key FROM items ORDER BY key LIMIT 1").fetchone()
        _k0 = _row["key"] if _row else ""
    except Exception:  # noqa: BLE001
        pass
    if _k0:
        _rows = _KV.level_rows(_k0)
        check("level_rows 返回每一层", len(_rows) == len(_KV.LEVELS))
        check("每一层都有绝对路径",
              all(os.path.isabs(r["path"]) for r in _rows),
              str([r["path"] for r in _rows]))
        check("路径都在知识库目录下",
              all(os.path.normcase(r["path"]).startswith(
                  os.path.normcase(_S.KB_DIR)) for r in _rows))
        # card / fulltext 是构建时就写好的现成文件，必须在
        _missing_files = [r["label"] for r in _rows
                          if r["id"] in ("card", "fulltext") and not r["exists"]]
        check("完整档案与按页正文两份文件都在", not _missing_files, _missing_files)
    else:
        print("  （库里没有条目，跳过逐层路径检查）")

    # 真装配一次那个窗口（装配阶段的错只有建出来才暴露）。
    # ⚠ 这一段在 `root.destroy()` **之后**，所以要另开一个 Tk 根 —— 用那个
    #   已经销毁的 root 会报 "application has been destroyed"（第一版就是）。
    try:
        from panels.browser import LevelPicker  # noqa: E402

        _proot = tk.Tk()
        _proot.withdraw()
        _pick = LevelPicker(_proot, _k0 or "NOSUCHKEY", "自检用",
                            ui_font=app.ui_font, mono_font=app.mono_font)
        _pick.withdraw()          # 立刻藏起来：测试不该弹窗
        _proot.update_idletasks()
        check("LevelPicker 能装配", True)
        check("LevelPicker 列出全部级别",
              len(_pick.rows) == len(_KV.LEVELS), str(len(_pick.rows)))
        _pick.destroy()
        _proot.destroy()
    except Exception as _exc:  # noqa: BLE001
        check("LevelPicker 能装配", False,
              f"{type(_exc).__name__}: {_exc}")
except Exception as _exc:  # noqa: BLE001
    check("能 import kbviews", False, f"{type(_exc).__name__}: {_exc}")

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
