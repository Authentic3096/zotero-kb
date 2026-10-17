"""知识库业务实现库（**无端口、无服务**）—— 由引擎子进程与测试复用。

2026-10 起，Zotero 插件按需拉起 `online/engine.py`（stdio JSON-RPC），
所有 `do_* / start_* / _safe_*` 业务函数都在这里，供引擎与测试直接 import。
这里**不监听任何端口、不生成 token、不认识 HTTP** —— 老的 8765 服务已整体删除。
如果某段代码需要"服务在跑"，那它应该改成由引擎来调，而不是把服务加回来。
"""


from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, HERE)

import schemas as S  # noqa: E402
import procrun as PR  # noqa: E402 —— 服务是 pythonw 起的，子进程统一走它


# Windows 下起子进程（跑 python --version 等）时别弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOG_PATH = os.path.join(S.KB_DIR, "logs", "kbops.log")

# 后台任务表：{job_id: {state, started, finished, title, log[], result}}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_MAX_JOBS = 50



# ---------------------------------------------------------------- 工具


def log(msg: str) -> None:
    """打一行日志（stdout + 知识库目录下的 logs/kbops.log）。

    ⚠ **两个写口都必须自己接住异常**，因为它跑在调用方的线程里：
      服务是插件用 `Subprocess.call(..., stdout: "ignore")` 起的，而那个
      "ignore" 并不真的把孩子接到 NUL —— 孩子**继承父进程的句柄**，父进程
      （Zotero）那边的管道一旦失效，孩子这里的 stdout 就成了坏句柄：
      `print` 抛 `OSError: [Errno 22] Invalid argument`。
      本机实测踩到（2026-10-10）：Zotero 重启后新起的服务里**每一次 log()
      都抛**，于是 POST /task 连响应都发不出去（客户端看到连接被掐断）、
      生成纲要的任务 2 秒就报 `OSError: [Errno 22] Invalid argument` ——
      现象离真凶（"日志打不出去"）十万八千里。
      结论：**日志失败绝不允许影响业务**，所以这里静默降级（文件那份照写）。
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:      # noqa: BLE001  —— 坏句柄、管道断开都算，业务优先
        pass
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass



def start_job(title: str, fn, *args, progress: bool = False, **kwargs) -> str:
    """把长任务丢到后台线程跑，返回 job id。

    `progress=True` 时给被调用的 fn 额外塞一个 **关键字参数 `progress`**
    （一个 `lambda 一行文本 -> None`），它写进 job 的 `log` 里 ——
    插件轮询 `GET /jobs/<id>` 就能把"现在跑到第几节"显示出来。
    默认 False：既有调用方一个都不受影响（它们没有 `progress` 形参）。
    """
    job_id = f"job-{int(time.time() * 1000) % 100000000}"
    entry = {"id": job_id, "title": title, "state": "running",
             "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "", "log": [], "result": None, "error": ""}
    with JOBS_LOCK:
        JOBS[job_id] = entry
        if len(JOBS) > _MAX_JOBS:       # 只留最近若干条
            for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:-_MAX_JOBS]:
                JOBS.pop(old, None)

    def emit(msg) -> None:
        """记一行进度。**有上限** —— 一篇学位论文几十节，别让它无限长。"""
        try:
            entry["log"].append(str(msg)[:400])
            if len(entry["log"]) > 200:
                del entry["log"][:-200]
        except Exception:      # noqa: BLE001
            pass

    def worker():
        call = dict(kwargs)
        if progress:
            call["progress"] = emit
        try:
            result = fn(*args, **call)
            entry["result"] = result
            entry["state"] = "done"
        except Exception as exc:  # noqa: BLE001
            import traceback
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["log"].append(traceback.format_exc()[-2000:])
            entry["state"] = "failed"
        finally:
            entry["finished"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


# ---------------------------------------------------------------- 业务动作


def do_reindex(keys: list[str] | None, full: bool) -> dict:
    """跑知识库增量/全量构建。用子进程，避免长任务占住服务线程。"""
    args = [sys.executable, "-X", "utf8", os.path.join(ROOT, "offline", "convert.py")]
    if full:
        args.append("--full")
    else:
        args.append("--no-vectors")     # 交互式补抽不阻塞在向量上
    log("跑构建：" + " ".join(args[2:]))
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "HF_ENDPOINT": "https://hf-mirror.com"}
    proc = PR.run(args, merge_stderr=True, cwd=ROOT, env=env, timeout=1800,
                  on_tier=lambda m: log(f"[procrun] 构建子进程改用兜底档位：{m}"))
    tail = (proc.stdout or "").strip().split("\n")[-12:]
    log(f"构建结束，退出码 {proc.returncode}")
    return {"exit_code": proc.returncode, "output_tail": tail}


def do_doc_rebuild(key: str, progress=None) -> dict:
    """按**修订后的正文**把一篇重切重向量（POST /doc-edit 的后台 job）。

    为什么不复用 do_reindex：那条路给增量构建加了 `--no-vectors`（交互式补抽
    不想等向量），而用户的口径是「改完立即重切**重向量**」（设计方案 J.5）。
    这里显式只处理 `--item <key>` 这一篇、**带向量** —— 单篇几十个块，几秒钟。
    """
    args = [sys.executable, "-X", "utf8",
            os.path.join(ROOT, "offline", "convert.py"), "--item", str(key)]
    log("重建单篇（含向量）：" + " ".join(args[2:]))
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "HF_ENDPOINT": "https://hf-mirror.com"}
    proc = PR.run(args, merge_stderr=True, cwd=ROOT, env=env, timeout=1800,
                  on_tier=lambda m: log(f"[procrun] 重建子进程改用兜底档位：{m}"))
    tail = (proc.stdout or "").strip().split("\n")[-12:]
    log(f"单篇重建结束，退出码 {proc.returncode}")
    return {"exit_code": proc.returncode, "key": str(key), "output_tail": tail}


def do_doc_edit(body: dict) -> tuple:
    """正文修订（引擎 doc.get / doc.save 的业务实现）。返回 (状态码, 响应体)。

    ⚠ 这个 `(码, 体)` 是**历史形状**（引擎按 >=400 判失败）；不是 HTTP 状态码。

    {key, page: n, text}             -> 保存这一页的修订，并派一次单篇重建；
    {key, page: n, action:"restore"} -> 恢复这一页的机器稿（同样重建）；
    {key, action:"draft"}            -> 把整份**有效正文**写成 edits/<key>.draft.md。

    ⚠ 保存/恢复之后**必须**重切重向量，否则 chunks 还是旧文本 —— 用户要的就是
    「改完立即」（设计方案 J.5）。这里复用任务队列：start_job -> GET /jobs/<id>。
    """
    import edits as ED
    import mineru as M
    key = str(body.get("key") or "").strip()
    if not key:
        return 400, {"error": "需要 key",
                     "hint": "POST /doc-edit {key, page, text}"}
    action = str(body.get("action") or "save").strip().lower()
    machine = M.load_pages(key)
    if action == "draft":
        if not machine:
            return 400, {"error": "这一篇还没有逐页正文（没建过索引？）", "key": key}
        path = ED.write_draft(key, ED.apply(key, machine))
        return 200, {"ok": True, "key": key, "path": path, "pages": len(machine)}
    try:
        page = int(body.get("page"))
    except (TypeError, ValueError):
        return 400, {"error": "需要整数 page", "key": key}
    if not machine:
        return 400, {"error": "这一篇还没有逐页正文（没建过索引？）", "key": key}
    if page < 1 or page > len(machine):
        return 400, {"error": f"页码越界（1..{len(machine)}）", "key": key,
                     "page": page}
    if action == "restore":
        res = ED.restore_page(key, page)
    elif action in ("save", ""):
        if body.get("text") is None:
            return 400, {"error": "需要 text", "key": key, "page": page}
        res = ED.save_page(key, page, str(body.get("text") or ""), machine)
    else:
        return 400, {"error": f"不认识的 action：{action}", "key": key}
    job = start_job("重建正文 " + key, do_doc_rebuild, key)
    res.update({"ok": True, "job": job, "state": "running",
                "machine_pages": len(machine)})
    return 202, res


def do_status() -> dict:
    """状态卡的数字（GET /status 的业务实现）。

    抽成模块级函数是为了让 stdio 引擎 `online/engine.py` 的 `kb.status`
    **复用同一份**（2026-10-13，范围 B 第一波）。引擎在它的返回值上再补一个
    `fulltext_chars`（`SUM(items.fulltext_chars)`）—— 那是 W1 状态卡要的新数字，
    不塞进这里是为了不改动 HTTP 的 `/status` 响应（旧服务的行为一个字不许变）。
    """
    from searcher import Searcher

    s = Searcher()
    try:
        return s.stats()
    finally:
        s.close()


def do_jobs_list() -> dict:
    """GET /jobs 的响应体：长任务列表（**不含 log**）。

    与 HTTP 完全同形（`{"jobs": [...]}`），引擎的 `jobs.list` 直接返回它。
    不含 log 的理由同 HTTP：列表里每条的 log 可能上百行，列表页不需要。
    """
    with JOBS_LOCK:
        return {"jobs": [
            {k: v for k, v in j.items() if k != "log"} for j in JOBS.values()]}


def do_jobs_get(job_id: str) -> dict | None:
    """GET /jobs/<id> 的响应体：一条任务的全部字段（含 log）；没有则 None。

    ⚠ 返回**副本**（dict(job)）而不是表里的那个活字典：调用方可能在锁外
      序列化它，而 worker 线程同时在改 state/log —— 拷一份出去最稳。
      （HTTP 那边原来是锁外直接 `_send(job)`，序列化瞬间可能读到半个状态；
       改走这里之后顺带修好了，返回的 JSON 形状不变。）
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


# 「生成分节纲要」的停止标记。
#
# 为什么要有它：纲要要一节一节调本地模型（论文几十秒、学位论文几分钟），
# 用户在 Zotero 里点了以后总得能停下来。面板那条路的「停止当前任务」是它
# 自己 kill 子进程（tools/panels/base.py（已退役 2026-10-14）），而插件这条路是**服务端线程**里
# 跑的，杀不掉也没必要杀 —— 在**节与节之间**看一眼这个标记就够（真正耗时的
# 是模型调用，节边界就是安全点，见 digest.build 的 should_stop）。
#
# ⚠ 全局一个标记而不是每个 job 一个：本机是 4B 小模型 + 单 GPU，服务端同一
#   时刻只应该有一个纲要任务在跑（插件侧也是串行提交的）。每次 POST /digest
#   都会先 clear()，免得上一批的取消把这一批也带走。
_DIGEST_CANCEL = threading.Event()


def _digest_cancelled() -> bool:
    return _DIGEST_CANCEL.is_set()


def do_digest(key: str, force: bool = False, progress=None) -> dict:
    """给一篇生成/增量更新「分节纲要」（POST /digest 的后台 job）。

    为什么放服务端：MinerU 产物、向量库、模型、views/ 目录都在这边，
    插件只把一个 key 发过来（与 /reindex 同一套分工）。

    为什么不走子进程（do_reindex 那样）：digest.build() 的返回值本身
    就是一份结构化结果（ok / n_sections / cached / asked / why / cancelled），
    in-process 直接拿到，不用去解析 stdout —— 而 stdout 那条路还踩过
    「pythonw 下 stdout 是 None」的坑（见插件 14-menus.js 的 runPython）。

    返回的是 build() 的结果**去掉 outline**（整份纲要几十 KB，塞进 HTTP
    响应没意义；插件要的是"成了几节"），另外补两件事：
      · existed_before —— 这次跑之前 views/<key>.outline.md 在不在；
      · skipped —— 一节模型都没问（全都是节指纹复用），也就是
        「这篇本来就有纲要，这次等于跳过」。插件靠它汇总"跳过 M 篇"。
    """
    import digest as D
    key = (key or "").strip()
    vpath = os.path.join(S.VIEWS_DIR, f"{key}.outline.md")
    existed = os.path.isfile(vpath)

    def note(msg) -> None:
        if progress:
            progress(msg)
        log(f"[digest] {msg}")

    log(f"生成分节纲要 {key}（force={force}，"
        + ("已有纲要" if existed else "还没有纲要") + "）")
    r = D.build(key, force=force, log=note, should_stop=_digest_cancelled)
    r.pop("outline", None)
    r["existed_before"] = existed
    r["skipped"] = bool(r.get("ok")) and not r.get("asked")
    log(f"生成分节纲要 {key} 结束：ok={r.get('ok')} "
        f"{r.get('n_sections')} 节（新跑 {r.get('asked')}）"
        + ("，已取消" if r.get("cancelled") else ""))
    return r



# ---------------------------------------------------------------- 经验起草
#
# ⚠ 2026-10-05：这里原来有一整组「窗格聊天助手」端点（/chat、/chat-context、
# /para-plan、/para-check、/para-locate、/kb-propose、/kb-apply），给 Zotero
# 右侧内容窗格那个「本地模型」分区用。用户判断"没什么用而且 bug 多"，
# 要求连同窗格一起删掉 —— 那七个端点、offline/kbchat.py、offline/paras.py、
# 面板的逐段复核页、以及 para_check / para_override / fulltext_patch 三张表
# 都不再存在。**别照着旧文档把它们加回来。**
#
# 唯一活下来的是 /exp-draft（面板「经验库 → 新建经验」的草稿按钮），
# 实现搬到了 offline/experience.py::draft_experience。


def start_reindex_job(keys: list[str] | None, full: bool) -> str:
    """派一次建索引（POST /reindex 的业务实现），返回 job id。

    抽出来给 stdio 引擎的 `kb.reindex` 复用（2026-10-13，范围 B 第一波）——
    任务标题与参数顺序只有这一份，两处各自拼就会漂移。
    """
    return start_job("重建知识库索引" + ("（全量）" if full else "（增量）"),
                     do_reindex, keys, full)


def start_digest_job(key: str, force: bool = False) -> str:
    """派一次「生成分节纲要」（POST /digest 的业务实现），返回 job id。

    抽出来给 stdio 引擎的 `kb.digest` 复用。⚠ 这里包含**清停止标记**那一步：
      不清的话，用户上一次点了「停止」之后，这一次刚派出去的任务第一件事
      就是收工（HTTP 那边原来就在路由里 clear，搬过来语义一致）。
    """
    _DIGEST_CANCEL.clear()
    return start_job("生成分节纲要 " + key, do_digest, key,
                     force=force, progress=True)


def do_exp_draft(text: str, ref_id: int = 0, model: str = "") -> dict:
    """把用户的大白话整理成一条经验草稿（**不写库**）。"""
    import experience as EXP

    conn = S.connect(S.INDEX_DB)
    try:
        return EXP.draft_experience(text, ref_id=ref_id, model=model,
                                    conn=conn)
    finally:
        conn.close()


def do_set_weight(key: str, pinned: bool | None = None,
                  manual: float | None = None,
                  note: str | None = None) -> dict:
    """写一条条目的权重（标重点 / 取消重点 / 改人工分 / 写备注）。

    为什么要这个端点：原来只有 **读** 权重的接口，写权重得靠管理面板
    跑一段内联 Python（旧 Tk 面板的 do_weight 就是这么做的，面板已退役 2026-10-14）。
    但用户在 Zotero 里看到某篇想标重点时，右键菜单是最短路径 ——
    插件没法跑那段内联代码，所以得有个 HTTP 写接口。

    ⚠ 只动"人工字段"（pinned / manual / note），**不碰经验统计**
      （attempts / effective / ineffective / partial）——
      那些是 `kb_experience_add` 按经验累加出来的，人工改会破坏"经验驱动"
      这个前提（本机在 kb_admin.py 的清理逻辑里也是这个判据）。

    ⚠ 实现只有一份：`offline/experience.py:set_weight`。原来这里自己写了一遍
      upsert，`server.py` 的 kb_weight_set 又写了一遍 —— 两处对"空 note 算不算
      清空"的理解就不一样，迟早出事。
    """
    from searcher import Searcher

    import experience as EXP

    s = Searcher()
    try:
        if not s.get_item(key):
            return {"ok": False, "error": f"知识库里没有 key={key} 的条目"}
        info = EXP.set_weight(s, key, pinned=pinned, manual=manual, note=note)
        return {"ok": True, "key": key, "pinned": info["pinned"],
                "manual": info["manual"], "note": info["note"],
                "weight": round(s.weight_of(key), 3)}
    finally:
        s.close()


def do_weights_map(keys: list[str] | None = None) -> dict:
    """批量返回条目权重：{key: {weight, pinned, attempts, ...}}。

    为什么要"批量"这一个接口：Zotero 插件要在文献列表里显示权重列，
    而 ItemTree 的 `dataProvider` 是**同步**的、每行都会被调用 —— 不可能在里面
    发网络请求（几百行会打爆服务端）。所以插件启动时拉一次全量映射放进内存，
    dataProvider 只做字典查表。

    性能上也必须批量：原来 kb_item 是"取条目 + 单独查权重"两趟，
    100 条就是 200 次查询；这里一次 SELECT 出全部权重行再合并。

    ⚠ **必须以 `items` 为主表**（不是以 `item_weight` 为主表）。
      曾经写成 `FROM item_weight w LEFT JOIN items i` —— 那样只返回
      "在 item_weight 里有行"的那些条目（本机 19/97），于是 Zotero 列表里
      **只有那几行显示权重，其余全是空白**。而实际上每篇文献都有权重：
      没有经验、没标重点的，至少还有按发表年份算的基础权重（0.95~1.05）。
      用户看到空白的直接反应就是"权重没算"。
    """
    from searcher import Searcher

    s = Searcher()
    try:
        rows = s.read(
            "SELECT i.key AS item_key, i.year, i.title, "
            "       w.item_key AS w_key, w.pinned, w.manual, w.note, "
            "       w.attempts, w.effective, w.ineffective, w.partial, "
            "       w.updated_at "
            "FROM items i LEFT JOIN item_weight w ON w.item_key = i.key"
        )
        weights: dict[str, dict] = {}
        for r in rows:
            # 没有 item_weight 行 → 传 None，让 `weight_multiplier` 退回
            # "只有基础权重"的算法（它显式支持 row=None 这条路）。
            has_row = bool(r["w_key"])
            row = dict(r) if has_row else None
            weights[r["item_key"]] = {
                "weight": round(
                    S.weight_multiplier(row, S.recency_base(r["year"])), 3
                ),
                "pinned": bool(r["pinned"]) if has_row else False,
                "manual": (r["manual"] or 0.0) if has_row else 0.0,
                "note": (r["note"] or "") if has_row else "",
                "attempts": (r["attempts"] or 0) if has_row else 0,
                "effective": (r["effective"] or 0) if has_row else 0,
                "ineffective": (r["ineffective"] or 0) if has_row else 0,
                "partial": (r["partial"] or 0) if has_row else 0,
            }
        out = {
            "weights": weights,
            "count": len(weights),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if keys:
            out["weights"] = {k: weights[k] for k in keys if k in weights}
        return out
    finally:
        s.close()


# 「分节纲要」的元数据前缀。⚠ 与 offline/digest.py::META_PREFIX 是同一个值
# （那边是唯一写路径）—— 改了要两边一起改。
_OUTLINE_META_PREFIX = "ai_outline:"


def _outline_meta_counts(s) -> dict[str, int]:
    """一次取全量：`{key: 节数}`，来自 `meta.ai_outline:<key>` 的 JSON。

    为什么按 **meta** 而不是文件：读端（`offline/kbviews.py::_render_outline`、
    MCP 资源、面板「打开知识库 → 分节纲要」）读的全都是 meta，
    `views/<key>.outline.md` 只是渲染产物。

    ⚠ T0-8 那列当初按**文件**计数，于是本机 94 篇**全都有纲要**（meta 94 行、
    读端也读得到）却只显示 41 篇（views 里只有 49 个 .outline.md 文件）——
    46 篇明明有纲要却显示成 ✗（2026-10-10 面板修整时查实）。真相应以 meta 为准。
    """
    out: dict[str, int] = {}
    try:
        rows = s.read("SELECT k, v FROM meta WHERE k LIKE ?",
                      (_OUTLINE_META_PREFIX + "%",))
    except Exception as exc:      # noqa: BLE001
        log(f"/col-status 读纲要 meta 失败：{type(exc).__name__}: {exc}")
        return out
    for r in rows:
        try:
            k = str(r["k"])[len(_OUTLINE_META_PREFIX):]
        except Exception:      # noqa: BLE001
            continue
        try:
            data = json.loads(r["v"] or "{}")
        except Exception:      # noqa: BLE001
            data = {}
        out[k] = len(data.get("sections") or []) if isinstance(data, dict) else 0
    return out


def _outline_file_keys() -> set[str]:
    """`views/` 下**已经存在** `*.outline.md` 的 key 集合。"""
    out: set[str] = set()
    try:
        for fn in os.listdir(S.VIEWS_DIR):
            if fn.endswith(".outline.md"):
                out.add(fn[: -len(".outline.md")])
    except OSError:
        pass
    return out


def do_col_status() -> dict:
    """文献列表那两列（「已建知识库」「分节纲要」）要的数据。

    返回 `{"ok": true, "items": {key: {"in_kb": bool, "outline_sections": int,
    "outline_file": bool}}, "with_outline": int, "with_outline_file": int,
    "outline_orphans": [key, …]}`。

    三条语义（插件按它们渲染 ✓/✗ 与「N 节」）：
      · 映射里出现的 key 就是"在知识库里"（它们逐条来自 items 表）；
        插件那边的判据是"**完整**的映射里查不到 = 还没建库"；
      · outline_sections 数的是 `meta.ai_outline:<key>` 里的节数（= 读端看到的），
        没有就是 **0（真的没有）**，不是"没数到"；
      · outline_file 是"人读的那份渲染文件在不在"（views/<key>.outline.md），
        它只是派生物 —— 有 meta 没文件时读端仍能渲染出纲要。

    为什么一次给全量：ItemTree 的 dataProvider 是**同步**的、每行都会被调用
    （同 /weights 的理由）—— 逐行发请求会把本地服务打爆。
    """
    from searcher import Searcher

    try:
        s = Searcher()
    except Exception as exc:  # noqa: BLE001
        # 知识库还没建 / 索引文件坏了：回 ok=False + 空映射，让插件**留空**。
        # 不能回一份空映射却说 ok=True —— 那样插件会把"服务没数据"
        # 画成"每条都没建库"（两句完全不同的话）。
        log(f"/col-status 读不了索引：{type(exc).__name__}: {exc}")
        return {"ok": False, "items": {},
                "error": f"{type(exc).__name__}: {exc}"}
    try:
        counts = _outline_meta_counts(s)
        files = _outline_file_keys()
        items: dict[str, dict] = {}
        for r in s.read("SELECT key FROM items"):
            key = r["key"]
            n = int(counts.get(key, 0) or 0)
            items[key] = {"in_kb": True,
                          "outline_sections": n,
                          "outline_file": key in files}
        # 孤儿 = 有渲染文件、但 meta 里没有纲要（或 0 节）：多半是删过 meta、
        # 或那一篇已不在 items 里。**只报警、不静默删**（用户的文件用户说了算）。
        orphans = sorted(k for k in files
                         if k not in items or not int(counts.get(k, 0) or 0))
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "with_outline": sum(1 for v in items.values()
                                if v["outline_sections"]),
            "with_outline_file": sum(1 for v in items.values()
                                     if v["outline_file"]),
            "outline_orphans": orphans,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        s.close()


def do_item_info(keys: list[str]) -> dict:
    """给插件返回某几条文献在知识库里的状态（用于"这条切过片没有"）。

    除了"在不在库 / 切了多少片"，还一并给出 **first_author / year / collections /
    fulltext_chars / weight** —— 外部工具（如 DSH 那个按条目挑文献的插件）要靠这几列
    做一张可排序的选文献表，逐条查库既慢又要另开一条数据路径。

    加这两个字段（2026-10-13）是**纯增量**：`Searcher.get_item()` 就是
    `SELECT * FROM items`，`first_author` / `year` 本来就在表里（面板的
    `exp_editor.picker_rows()` 一直在用），只是原来没往外吐。老调用方无感。
    """
    from searcher import Searcher

    s = Searcher()
    try:
        out = []
        for key in keys[:200]:
            item = s.get_item(key)
            if not item:
                out.append({"key": key, "in_kb": False})
                continue
            n_chunks = s.read_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE item_key = ?", (key,))["n"]
            out.append({
                "key": key,
                "in_kb": True,
                "title": item["title"],
                "first_author": item.get("first_author") or "",
                "year": item.get("year"),
                "fulltext_chars": item["fulltext_chars"],
                "n_chunks": n_chunks,
                "n_annotations": item["n_annotations"],
                "n_notes": item["n_notes"],
                "weight": item["weight"],
                "tags": item["tags"],
                "collections": item["collections"],
            })
        return {"items": out}
    finally:
        s.close()


def do_tag_suggest(keys: list[str], limit: int) -> dict:
    """用本地模型给指定文献生成标签建议（**不写任何东西**，只出建议）。"""
    import judge

    from searcher import Searcher

    if not judge.ollama_available()[0]:
        return {"error": "本地模型服务不可用", "hint": "启动 Ollama 后重试"}
    model = judge.pick_model()
    s = Searcher()
    suggestions = []
    try:
        for key in keys[:limit]:
            item = s.get_item(key)
            if not item:
                suggestions.append({"key": key, "error": "不在知识库里"})
                continue
            # 已有标签一并给模型，让它尽量沿用而不是另造一套
            res = judge.tag_item(item["title"], item["abstract"] or "", model=model)
            if not res.get("ok"):
                suggestions.append({"key": key, "error": res.get("error", "失败")})
                continue
            suggestions.append({
                "key": key,
                "title": item["title"],
                "existing_tags": item["tags"],
                "suggested_tags": res["tags"],
                "topic": res.get("topic", ""),
                "model": res.get("model", ""),
            })
        return {"model": model, "suggestions": suggestions}
    finally:
        s.close()


def do_metafill(key: str, use_model: bool = True, model: str = "") -> dict:
    """从 PDF 首页给**一条**文献算"元数据补全建议"（`offline/metafill.py`）。

    和上面 `do_tag_suggest` 的区别（决定了两者形态不同，不是随手写的）：
      · 标签建议**必须**调模型，所以做成异步任务（`start_job`），插件轮询结果；
      · 元数据补全**规则优先**，纯规则时是秒回，只有规则没抽到字段时才调模型。
        所以这里做成**同步**接口，插件一次请求直接拿到建议，少一套轮询状态机。

    ⚠ 本函数**只读**：不写 Zotero 库、不写索引库。写回由插件侧在用户勾选确认后
      自己做（`item.setField` + `saveTx`）—— 服务端不提供任何写回路径，
      这样"服务端被别的程序调用"也永远改不了用户的数据。

    参数：
      key        必填。Zotero 条目 key。
      use_model  可选，默认 True。False = 只跑规则（自检与排障用它，不依赖 Ollama）。
      model      可选。透传给 judge 的模型名；空 = 用配置里的。
    """
    key = (key or "").strip()
    if not key:
        # 路由那层一般已经挡过，这里再挡一道：本函数被别处直接调用时也不会静默出错
        return {"ok": False, "key": "", "title": "", "error": "需要 key",
                "suggestions": [], "skipped": []}
    try:
        import metafill
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "key": key, "title": "",
                "error": f"加载 offline/metafill.py 失败：{type(exc).__name__}: {exc}",
                "suggestions": [], "skipped": []}
    try:
        return metafill.suggest(key, model=model or "", use_model=bool(use_model))
    except Exception as exc:  # noqa: BLE001
        # 不让异常冒到 HTTP 层变成 500 空响应：把原因原样带给插件，
        # 插件弹窗里能直接看到"是哪儿坏了"（本项目的规矩：失败要可见）。
        return {"ok": False, "key": key, "title": "",
                "error": f"{type(exc).__name__}: {exc}",
                "suggestions": [], "skipped": []}
    finally:
        # ⚠ 必须成对释放：`metafill.ZoteroReader` 在构造时会把 zotero.sqlite
        #   （含 -wal/-shm）**整库拷一份快照**再用（见 zreader 的类注释），
        #   而它是模块级懒加载的单例。长驻服务里不 close，就是每次请求都留一个
        #   打开着的 SQLite 连接 + 下次再重拷一份。
        #   放 finally 是为了把"成功 / 抛异常 / 提前 return"三条路径一起兜住。
        metafill.close()


# ---------------------------------------------------------------- 批量补全
#
# 为什么要有批量端点（而不是让面板对 73 篇逐个打 /metafill）：
#   ① 每一篇的 HTTP 往返本身要几十毫秒，73 篇就是一串串行等待；
#   ② 更要命的是 `metafill.ZoteroReader` 是**模块级单例**，但 `do_metafill`
#      在 finally 里 `metafill.close()` 把它关掉 —— 逐个请求时每篇都要
#      **重新把 zotero.sqlite 整库拷一份快照**（含 -wal/-shm），73 篇就是
#      73 次整库拷贝。本机实测这就是"扫一遍要几分钟"的真正原因。
#   ③ 批量走一次请求、一个 reader、一次 close，是唯一不浪费的形态。

# 取消标志：面板点「取消」时置位，扫描循环在**每篇之间**检查它。
# 为什么不做成"杀线程"：Python 里杀线程就没有干净的办法（只能整进程退），
# 而这个是只读扫描，合作式取消（检查标志后收尾返回）就够用，
# 还能把**已经扫出来的部分结果**带回去（用户往往就是扫到一半发现够了）。
_SCAN_CANCEL = threading.Event()
# 同一时间只允许一个批量在跑：两个并发扫描会各建一份快照、互相抢 IO，
# 而且取消标志只有一个，分不清取消的是谁。
_SCAN_BUSY = threading.Lock()


# 「一键采用高置信」**允许自动写**的字段。
#
# ⚠ 这份名单不是这里定的，是**插件 `applyMeta` 里的白名单的镜像**：
#   插件那边写死了 `["date","DOI","volume","issue","pages"]`，理由是用户
#   明确说过"作者留到后面看效果再说"，所以创作者（creators）与标题**永远
#   不自动写**。而 metafill 现在会给 creators 出 high 置信的建议
#   （规则从首页作者行抽的），如果不过滤就会变成"面板说会写、插件拒绝写"，
#   用户看到的是"成功率对不上"。
#   所以这里**先过滤掉**，并在响应里用 excluded_high 如实说明"有几条作者
#   建议没列进来、为什么" —— 宁可少写，也不要静默地写不进去。
BATCH_WRITABLE_FIELDS = ("date", "DOI", "volume", "issue", "pages")


def do_metafill_scan(use_model: bool = False, model: str = "",
                     limit: int = 0, fields: tuple | None = None) -> dict:
    """对全库**缺字段**的条目批量算补全建议（**只读**，不写任何库）。

    参数：
      use_model  默认 **False**（纯规则，秒级）。理由：73 篇 × 每篇几秒的模型
                 调用是十几分钟的量级，必须是用户显式点的"调模型补全"才做。
      limit      只扫前 N 篇（0 = 全部）。给"先试 10 篇看看"用。
      fields     只关心这几个字段（默认 metafill.FIELDS）。

    返回里除了逐篇明细，还给一份 `apply_plan`：**只含 high 置信**的建议，
    形态直接就是插件 `applyMetaBatch` 要的入参 —— 面板「一键采用高置信」
    把它原样交给插件即可，中间不再做一次"哪些算高置信"的判断（那种判断
    散在两处必然漂移）。
    """
    try:
        import metafill
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"加载 offline/metafill.py 失败："
                                      f"{type(exc).__name__}: {exc}",
                "rows": [], "apply_plan": {"items": []}}

    if not _SCAN_BUSY.acquire(blocking=False):
        return {"ok": False, "error": "已经有一个批量补全在跑，等它结束或先点「取消」",
                "busy": True, "rows": [], "apply_plan": {"items": []}}
    _SCAN_CANCEL.clear()
    started = time.time()
    try:
        want = tuple(fields) if fields else metafill.FIELDS
        # ⚠ 关键：**自己拿一次 reader**，然后整批复用它。
        #   `suggest_for_item` 内部也走 `_get_reader()`，拿到的就是这个单例，
        #   所以不会中途再拷贝快照（这正是这个端点的存在理由）。
        reader = metafill._get_reader()
        all_items = reader.load_items()
        rows: list[dict] = []
        per_field: dict[str, int] = {}
        scanned = canceled = 0
        model_used = 0
        for it in all_items:
            if _SCAN_CANCEL.is_set():
                canceled = 1
                break
            cur = metafill._current_values(it)
            lack = [f for f in want if not cur[f]]
            if not lack:
                continue
            if limit and scanned >= limit:
                break
            scanned += 1
            try:
                res = metafill.suggest_for_item(it, model=model or "",
                                                use_model=bool(use_model))
            except Exception as exc:  # noqa: BLE001
                # 单篇失败不能让整批断掉：记一条 error 行，继续下一篇
                rows.append({"key": it.key, "title": it.title,
                             "item_type": it.item_type, "missing": lack,
                             "n_high": 0, "n_low": 0, "high": [], "low": [],
                             "notes": [f"{type(exc).__name__}: {exc}"],
                             "error": str(exc)})
                continue
            high, low = [], []
            for s in (res.get("suggestions") or []):
                item = {"field": s.get("field", ""), "value": s.get("value", ""),
                        "source": s.get("source", ""),
                        "evidence_text": (s.get("evidence") or {}).get("text", "")}
                if s.get("confidence") == "high":
                    high.append(item)
                else:
                    low.append(item)
            if (res.get("model") or {}).get("used"):
                model_used += 1
            for f in lack:
                per_field[f] = per_field.get(f, 0) + 1
            rows.append({
                "key": it.key, "title": it.title, "item_type": it.item_type,
                "missing": lack, "n_high": len(high), "n_low": len(low),
                "high": high, "low": low,
                "notes": list(res.get("notes") or []),
                "fulltext_source": res.get("fulltext_source", ""),
                "skipped": list(res.get("skipped") or []),
            })
        # 缺得多的排前面（更值得先补），同数量按 key 稳定排序 —— 和
        # `metafill.missing_items` 的排序保持一致，面板两处看起来才是一回事
        rows.sort(key=lambda r: (-len(r["missing"]), r["key"]))
        plan_items = []
        excluded: dict[str, int] = {}
        for r in rows:
            fs = []
            for h in r["high"]:
                if not str(h.get("value") or "").strip():
                    continue
                if h["field"] not in BATCH_WRITABLE_FIELDS:
                    # 例如 creators：metafill 会给 high 置信的作者建议，
                    # 但"自动写作者"超出了既有红线（见 BATCH_WRITABLE_FIELDS
                    # 的注释）。不写，但要**数出来告诉用户**。
                    excluded[h["field"]] = excluded.get(h["field"], 0) + 1
                    continue
                fs.append({"field": h["field"], "value": h["value"]})
            if fs:
                plan_items.append({"key": r["key"], "title": r["title"],
                                   "fields": fs})
        return {
            "ok": True,
            "total": len(all_items),
            "missing_total": len(rows),
            "scanned": scanned,
            "canceled": bool(canceled),
            "elapsed": round(time.time() - started, 1),
            "model_requested": bool(use_model),
            "model_used_items": model_used,
            "per_field": per_field,
            "high_total": sum(r["n_high"] for r in rows),
            "low_total": sum(r["n_low"] for r in rows),
            "writable_fields": list(BATCH_WRITABLE_FIELDS),
            "excluded_high": excluded,
            "rows": rows,
            "apply_plan": {"items": plan_items},
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "rows": [], "apply_plan": {"items": []}}
    finally:
        # 和 do_metafill 同一个理由：单例连接必须成对释放，
        # 否则服务长驻下来会攒着一堆打开的 SQLite 连接 + 快照文件。
        try:
            metafill.close()
        except Exception:  # noqa: BLE001
            pass
        _SCAN_BUSY.release()


def do_metafill_scan_cancel() -> dict:
    """请求取消正在跑的批量补全（合作式：循环在下一篇之前看到标志就收尾）。"""
    _SCAN_CANCEL.set()
    return {"ok": True, "canceled": True,
            "hint": "扫描会在当前这一篇算完后停下，并返回已经算好的部分"}


# ---------------------------------------------------------------- 类型/标题修正
#
# 用户原话："先保存的网页后把pdf拉进来，会出现显示情况和其他文献不一致的
# 问题，比如文献类型，还有文献标题后面出现文献来源的名称"。
#
# 为什么会这样（诊断结论）：用 Zotero 连接器"保存网页"时条目是 `webpage`，
# 标题由网页 <title> 决定，于是带上 `| IEEE Xplore` 这类站点后缀；后来把 PDF
# 拖到这个条目上，**类型并不会自己变成期刊论文**，标题也没人清 —— 于是它
# 在列表里跟别的文献长得不一样。
#
# 这一节只做**只读的检测与建议**：真正改库在插件里、且必须用户逐条确认
# （改类型是敏感操作，见 `zotero-plugin/bootstrap.js` 的 applyTypeFix）。

# "正经文献"的类型。不在这个集合里、却又挂了 PDF 的，就是"网页存了、后来
# 挂上 PDF"的嫌疑对象（用户给的判据）。注意这是**必要条件**不是充分条件：
# report / patent 这类也常挂 PDF 而且完全正常，还要再看别的信号。
PAPER_TYPES = frozenset({"journalArticle", "conferencePaper", "thesis", "book"})

# 浏览器"保存网页"最常产生的类型。只有这几种才值得怀疑"它其实是一篇论文"——
# 一个 `report` 类型挂了 PDF 是正常的，不该去劝用户改它。
WEBISH_TYPES = frozenset({"webpage", "blogPost", "forumPost"})

# DOI 权威记录的类型 → Zotero 类型。
#
# ⚠ **这里必须同时收两套词汇，这是实测出来的**：
#   规范上 DOI 内容协商（Accept: application/vnd.citationstyles.csl+json）
#   应该返回 **CSL** 的类型词（`article-journal`），但本机实测 CrossRef
#   返回的是它**自己的**词表 —— `"type": "journal-article"`。
#   第一版只写了 CSL 那套，结果"有 DOI 的条目反而拿不到权威类型"
#   （代码里那句 `CSL_TYPE_MAP.get(...)` 拿到 None，静默跳过）。
#   所以两套都收：CSL 优先，CrossRef 兜底，谁也别说谁"不该出现"。
#
# **故意只列这几个**：拿不准的类型（`dataset` / `software` / `peer-review` /
# `component`）宁可不建议，让用户手工改 —— 改类型是敏感操作。
DOI_TYPE_MAP = {
    # —— CSL 词汇
    "article-journal": "journalArticle",
    "paper-conference": "conferencePaper",
    "book": "book",
    "chapter": "bookSection",
    "thesis": "thesis",
    "report": "report",
    "posted-content": "preprint",
    "manuscript": "manuscript",
    # —— CrossRef 自己的词汇（实测返回的就是这一套）
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "dissertation": "thesis",
    "other": "",          # 明确"无法判断"，留空 = 不映射
}

# 目标类型的中文名（弹窗里给用户看，不用他去学 Zotero 的类型枚举）
TYPE_CN = {
    "journalArticle": "期刊论文", "conferencePaper": "会议论文",
    "thesis": "学位论文", "book": "图书", "bookSection": "图书章节",
    "report": "报告", "preprint": "预印本", "manuscript": "手稿",
    "webpage": "网页",
}

# 标题尾部的"已知站点后缀"。
#
# ⚠ 这里**必须是白名单式的精确模式**，不能写成"凡是 ` | xxx` 就截掉"：
#   实测库里有中文标题本身带 `｜`（全角）的，也有真的用 ` - ` 分隔副标题的。
#   一刀切会把正常标题切坏，而"标题被切坏"比"留着站点名"严重得多
#   （用户原话："拿不准就不动，宁可让用户手工改"）。
#
# 每条 = (正则, 置信, 说明)。只在**尾部**匹配（`$`）。
TITLE_TAIL_RULES = (
    # IEEE Xplore 家族：浏览器保存 IEEE 页面时最常见的两种尾巴
    (r"\s*\|\s*IEEE\s+Journals?\s*&\s*Magazine\s*\|\s*IEEE\s+Xplore\s*$",
     "high", "IEEE Xplore 期刊页的站点后缀"),
    (r"\s*\|\s*IEEE\s+Conference\s+Publication\s*\|\s*IEEE\s+Xplore\s*$",
     "high", "IEEE Xplore 会议页的站点后缀"),
    (r"\s*\|\s*IEEE\s+Xplore\s*$", "high", "IEEE Xplore 站点后缀"),
    (r"\s*\|\s*IEEE\s+Access\s*$", "high", "IEEE Access 站点后缀"),
    # 其他常见学术站点的站点后缀（同样是精确写法，不是通配）
    (r"\s*[-–—|]\s*ScienceDirect\.com\s*$", "high", "ScienceDirect 站点后缀"),
    (r"\s*\|\s*SpringerLink\s*$", "high", "SpringerLink 站点后缀"),
    (r"\s*\|\s*Wiley\s+Online\s+Library\s*$", "high", "Wiley 站点后缀"),
    # 中文网站：只在尾部是**明确的站点名**且带"网/网站/首页/资讯"字样时才认，
    # 而且标 low（只提示、不预设勾选）。为什么留这一档：库里确实有
    # `..._央广网` 这种尾巴，但中文站点名与期刊名很难用规则分开。
    (r"\s*[-–—_|]\s*[\u4e00-\u9fffA-Za-z0-9]{2,12}(?:网|网站|官网|首页|资讯|新闻网)\s*$",
     "low", "像是中文站点名（需要你自己判断）"),
)


def _title_weight(s: str) -> int:
    """标题的"信息量"权重：**中文字符算 2，其余算 1**。

    为什么要加权而不是用 `len()`：12 个拉丁字符（`Data Analysis`）已经是个
    像样的标题，而 12 个汉字（`某某技术发展现状与展望`）信息量是它的两倍。
    用裸 `len()` 时阈值只能迁就短的英文，于是**中文标题全被误判成"剥完太短"**
    （本文件的自检抓到的：`某某技术发展现状与展望-某某新闻网` 剥完剩 11 字，
    被判<b>太短不动</b>，可它明明是个完整的中文标题）。
    """
    return sum(2 if "\u4e00" <= ch <= "\u9fff" else 1 for ch in (s or ""))


def _clean_title_by_rules(title: str) -> dict | None:
    """按白名单规则剥掉标题尾部的站点后缀。拿不准就返回 None（不动）。

    三条"保守"闸门，缺一条都可能把好标题改坏：
      ① 只匹配**白名单里的精确模式**，不做通配；
      ② 剥完剩下的部分不能太短（按 `_title_weight` 加权算，中文按 2 计）。
         阈值分两档：精确站点名（high）**8**，通配式（low）**12** ——
         精确匹配本身就是强证据，可以多信一点；通配那档本来就拿不准，
         门槛抬高一截，宁可不动。
         为什么不设一个更高的统一阈值：` | IEEE Journals & Magazine |
         IEEE Xplore` 有 40 个字符，短标题（`Data Study`）一比就"超过一半"，
         但那**必须**剥（自检里两条 IEEE 用例就是这么被误挡的）。
      ③ 剥掉的部分不能超过原标题的一半 —— ⚠ **只对 low 置信（通配式）的
         规则生效**。理由同上：精确到字的已知站点名，长度不该成为不剥的理由。
    """
    raw = str(title or "").strip()
    if not raw:
        return None
    for pat, conf, why in TITLE_TAIL_RULES:
        m = re.search(pat, raw)
        if not m:
            continue
        clean = raw[:m.start()].strip(" \t-–—|·")
        if _title_weight(clean) < (8 if conf == "high" else 12):
            continue                       # 闸门②
        if conf != "high" and (len(raw) - len(clean)) > len(raw) * 0.5:
            continue                       # 闸门③（只对通配式规则）
        if not clean:
            continue
        return {"value": clean, "confidence": conf, "rule": why,
                "removed": raw[m.start():].strip(),
                "evidence": {"page": 0, "text": f"标题尾部「{raw[m.start():].strip()}」"
                                                f"匹配到{why}"}}
    return None


# PDF 首页文本里的类型判据。分三族：会议 / 期刊 / 学位论文。
# 为什么要"分族再比大小"而不是"命中一个就算"：实测一篇会议论文的首页
# 也可能出现 `IEEE Transactions` 字样（参考文献里的），单看命中会把类型判反。
TYPE_TEXT_MARKERS = {
    "conferencePaper": (
        r"\bProceedings\s+of\b", r"\bInt(?:ernational)?\.?\s+Conf(?:erence)?\b",
        r"\bConference\s+on\b", r"\bSymposium\b", r"\bWorkshop\b",
        r"\bConf\.\s*\d{4}", r"会议论文集", r"会议论文",
    ),
    # ⚠ 期刊这一族里**故意没有 `pp. 12-34`**：会议论文也几乎都有页码，
    #   把它算作"期刊特征"会让会议论文同时命中两族 → 置信度掉到 low
    #   （自检里抓到过：一篇 `Proceedings of ... pp. 1-6` 被判成 low）。
    #   留下的这几个（Vol./Volume/No./Issue/ISSN/Journal/学报/期刊）都是
    #   期刊刊头特有或明显偏期刊的。
    "journalArticle": (
        r"\bVol\.?\s*\d+", r"\bVolume\s+\d+", r"\bNo\.?\s*\d+\s*,",
        r"\bIssue\s+\d+", r"\bISSN\b",
        r"\bJournal\b", r"学报", r"期刊",
    ),
    "thesis": (
        r"\bDissertation\b", r"\bThesis\b", r"\bPh\.?D\.?\b",
        r"学位论文", r"硕士学位论文", r"博士学位论文",
    ),
}


def _type_from_pdf_text(pages: list) -> dict | None:
    """从 PDF 首页文本判类型（返回 {value, confidence, markers, evidence}）。

    判据是"哪一族的**不同**命中最多"——单看某个词命中与否会被参考文献带偏。
    置信度：只有一族命中 且 该族命中 ≥ 2 个不同模式 → high；否则 low。
    一族都没命中 → None（

不动，让用户自己看）。
    """
    try:
        import metafill
    except Exception:  # noqa: BLE001
        return None
    text = metafill.head_text(pages, 2)
    if len(text.strip()) < 200:
        return None
    hits: dict[str, list] = {}
    for typ, pats in TYPE_TEXT_MARKERS.items():
        for pat in pats:
            m = re.search(pat, text, re.I)
            if m:
                hits.setdefault(typ, []).append((pat, m))
    if not hits:
        return None
    # 命中族数最多的那个；并列则按 会议 > 期刊 > 学位论文 的优先级
    order = ["conferencePaper", "journalArticle", "thesis"]
    best = max(order, key=lambda t: (len(hits.get(t, [])),
                                     -order.index(t)))
    if best not in hits:
        return None
    fams = [t for t in order if hits.get(t)]
    n = len(hits[best])
    conf = "high" if (len(fams) == 1 and n >= 2) else "low"
    pat, m = hits[best][0]
    ev = metafill.build_evidence(pages, m.start(), m.end())
    ev["markers"] = [p for p, _ in hits[best]]
    ev["families"] = fams
    return {"value": best, "confidence": conf,
            "rule": f"PDF 首页里命中 {n} 个「{TYPE_CN.get(best, best)}」特征词",
            "evidence": ev}


def _doi_lookup_csl(doi: str, timeout: float = 8.0) -> dict:
    """用 DOI 内容协商拿权威 CSL JSON（**会联网**）。

    为什么用内容协商（`Accept: application/vnd.citationstyles.csl+json`）
    而不是 CrossRef REST：它是 DOI 基金会的标准做法，一次请求就同时拿到
    `type`（权威类型）与 `title`（干净标题），而且对 DataCite 等其它注册
    机构同样有效。**这一点是查了 DOI 手册的，不是猜的。**

    ⚠ 隐私：这一步会把 DOI 发到 doi.org。所以**默认关闭**（调用方显式传
      use_network=True），并且只发 DOI、不发标题/作者。
    失败一律返回 {"ok": False, ...}，绝不抛 —— 联网不该让整个功能挂掉。
    """
    doi = str(doi or "").strip()
    if not doi:
        return {"ok": False, "error": "没有 DOI"}
    import urllib.error
    import urllib.parse
    import urllib.request
    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.citationstyles.csl+json",
        "User-Agent": "zotero-kb/1.0 (local metadata check)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"DOI 服务返回 HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "csl": data}


def _type_field_names(conn, type_name: str) -> set:
    """问 Zotero 的**快照库**："这个条目类型有哪些字段"。

    为什么查表而不是自己写一张 Python 常量表：那张表会跟 Zotero 版本漂移，
    而我们拷过来的快照里就有 Zotero 自己的 `itemTypeFields` 表 ——
    `Zotero.ItemFields.isValidForType()` 用的正是它（读了 `itemFields.js`
    的实现确认：它查的就是 `fieldsCombined.itemTypes`，来源是这张表）。
    所以这是**权威数据**，不是我猜的。
    """
    try:
        rows = conn.execute(
            "SELECT f.fieldName FROM itemTypeFields itf "
            "JOIN itemTypes t ON t.itemTypeID = itf.itemTypeID "
            "JOIN fields f ON f.fieldID = itf.fieldID "
            "WHERE t.typeName = ?", (type_name,)).fetchall()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


def _fields_not_in_type(item, target_type: str) -> list:
    """改类型会丢掉哪些字段（预览；与 Zotero 自己的 `getFieldsNotInType` 同判据）。

    ⚠ 这只是**预览**：真正动手时插件会调 `item.getFieldsNotInType(newTypeID)`，
      以 Zotero 现场算的为准（两边判断不一致时以 Zotero 为准，并在结果里
      如实报出来）。这里做预览是为了让用户在**点确认之前**就看到代价。
    """
    try:
        import metafill
        reader = metafill._get_reader()
        allowed = _type_field_names(reader.conn, target_type)
        if not allowed:
            return []
        cur = {k: str(v or "").strip()
               for k, v in (getattr(item, "fields", None) or {}).items()}
        return sorted(k for k, v in cur.items() if v and k not in allowed)
    except Exception:  # noqa: BLE001
        return []


def _plain_title(s: str) -> str:
    """标题归一化：**只用来比"到底变没变"，不用来当建议值**。

    为什么需要：CrossRef 的标题里带 HTML 实体与标签（`&lt;i&gt;`、`<sub>`），
    直接跟 Zotero 里的标题比会得出"不一样"——用户点开一看两个标题长得一样，
    等于白改一次。所以比较前先解实体、去标签、压空白、去掉结尾的句点。
    建议值仍然用**原文**，不归一化（归一化过的标题不该写进库里）。
    """
    import html as _html
    t = _html.unescape(str(s or ""))
    t = re.sub(r"<[^>]{1,40}>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.rstrip(".").strip().casefold()


def typefix_for_item(item, use_network: bool = False) -> dict:
    """给一个**已加载**的条目算"类型/标题被网站污染"的修正建议（**只读**）。

    返回：
      suspect   要不要在界面上提示（判据见下）
      signals   支持"suspect"的具体信号（给用户看，不黑箱）
      fixes     [{"kind":"type"|"title", "current","value","confidence",
                  "source","rule","evidence"}] —— 空数组 = 没什么可改的
      will_drop 改类型会**丢掉**的字段名（Zotero 自己的判据，见下）
      notes     给用户的说明

    suspect 的判据（必须同时成立）：
      ① 挂了 PDF；② 类型不在 PAPER_TYPES 里；③ 命中至少一个"它其实是论文"
         的额外信号（有 DOI / 标题带站点后缀 / PDF 首页有论文特征词）。
    只满足 ①② 时算"弱嫌疑"，signals 里说明，但不弹窗打扰 ——
    report/patent 之类挂了 PDF 完全正常。
    """
    key = str(getattr(item, "key", "") or "")
    title = str(getattr(item, "title", "") or "")
    itype = str(getattr(item, "item_type", "") or "")
    fields = getattr(item, "fields", None) or {}
    pdfs = list(getattr(item, "pdfs", None) or [])
    doi = str(fields.get("DOI") or "").strip()
    out = {"ok": True, "key": key, "title": title, "item_type": itype,
           "n_pdfs": len(pdfs), "doi": bool(doi),
           "suspect": False, "signals": [], "fixes": [],
           "will_drop": [], "notes": []}

    if not pdfs:
        out["notes"].append("这条没有 PDF 附件，不像是「网页存了、后来挂 PDF」，跳过")
        return out
    if itype in PAPER_TYPES:
        out["notes"].append(f"类型已经是「{TYPE_CN.get(itype, itype)}」，不用改")
        return out
    if itype not in WEBISH_TYPES:
        # report / patent / dataset… → 挂 PDF 是正常的，不当嫌疑
        out["notes"].append(
            f"类型是「{TYPE_CN.get(itype, itype)}」，这种类型挂 PDF 很正常，"
            f"不当作网页污染")
        return out

    out["signals"].append(f"类型是「{TYPE_CN.get(itype, itype)}」却挂了 "
                          f"{len(pdfs)} 个 PDF")

    # ---- 信号一：标题尾部的站点后缀
    tail = _clean_title_by_rules(title)

    # ---- 读 PDF 首页（只读地借用 metafill 的正文能力）
    text_hint = None
    try:
        import metafill
        pages, src = metafill._get_reader().fulltext_for(item, use_model=False)
        out["fulltext_source"] = src
        text_hint = _type_from_pdf_text(pages or [])
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"读 PDF 正文失败（不影响别的判断）："
                            f"{type(exc).__name__}: {exc}")

    # ---- 信号二：DOI 权威记录（只有显式要求时才联网）
    csl = None
    if doi and use_network:
        r = _doi_lookup_csl(doi)
        if r.get("ok"):
            csl = r["csl"]
            out["network"] = "已查 DOI 权威记录"
        else:
            out["notes"].append(f"查 DOI 权威记录失败（不影响其它判断）："
                                f"{r.get('error')}")
    elif doi:
        out["notes"].append("这条有 DOI。勾上「联网查 DOI」能拿到权威类型与标题")

    # ---- 组装类型建议（权威源 > PDF 首页 > 不动）
    #
    # ⚠ 这里是踩过坑的地方：第一版写成 `if csl: ... elif text_hint: ...`，
    #   于是"DOI 查到了、但它的类型词不在映射表里"时**连 PDF 判断也一起跳过**，
    #   结果是"联网之后反而没有类型建议"。现在改成：权威源能用就用，
    #   用不上（类型词不认识 / 映射到空）就**落回 PDF 首页判断**。
    type_fix = None
    ztype = ""
    if csl:
        raw_type = str(csl.get("type") or "").lower()
        ztype = DOI_TYPE_MAP.get(raw_type, "")
        if not ztype:
            out["notes"].append(
                f"DOI 权威记录的类型词是「{raw_type or '（空）'}」，"
                f"不在可安全映射的范围内，改用 PDF 首页判断")
    if csl and ztype:
        if ztype != itype:
            type_fix = {"kind": "type", "current": itype, "value": ztype,
                        "confidence": "high", "source": "DOI 权威记录",
                        "rule": f"DOI 注册机构记的类型是 "
                                f"{csl.get('type')}（权威）",
                        "evidence": {"page": 0,
                                     "text": "DOI 内容协商返回的权威记录"
                                             f"（container-title："
                                             f"{(csl.get('container-title') or '')[:60]}）"}}
        else:
            out["notes"].append("DOI 权威记录的类型与当前一致，类型不用改")
    elif text_hint and text_hint["value"] != itype:
        type_fix = {"kind": "type", "current": itype,
                    "value": text_hint["value"],
                    "confidence": text_hint["confidence"],
                    "source": "PDF 首页文本",
                    "rule": text_hint["rule"],
                    "evidence": text_hint["evidence"]}
    if type_fix:
        out["fixes"].append(type_fix)
        out["signals"].append(
            f"PDF 首页/DOI 显示它更像「{TYPE_CN.get(type_fix['value'], type_fix['value'])}」")

    # ---- 组装标题建议
    #
    # 优先级不是简单的"权威源一定更好"，这里踩过一个真实的坑：
    #   CrossRef 的标题**有可能是全小写的**（IEEE Access 那条实测就是
    #   `improving the quality of ...`）。第一版直接采纳，结果"能把站点后缀
    #   剥干净，却顺手把标题改成了小写"——比不改更糟。
    #   所以规则是：**权威标题只有在"确实带来了不同内容"时才用**；
    #   如果它与"本地剥掉站点后缀后的标题"只差大小写/标点，就用本地那个
    #   （保真），并说明原因。
    csl_title = ""
    if csl:
        raw_title = csl.get("title")
        if isinstance(raw_title, list):
            raw_title = raw_title[0] if raw_title else ""
        csl_title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    local_clean = tail["value"] if tail else ""
    title_fix = None
    if csl_title and _plain_title(csl_title) != _plain_title(title):
        if local_clean and _plain_title(csl_title) == _plain_title(local_clean) \
                and csl_title != local_clean:
            # 只差大小写/标点 → 用本地的写法（它才是原文的写法）
            title_fix = {"kind": "title", "current": title, "value": local_clean,
                         "confidence": "high", "source": "站点后缀规则",
                         "rule": tail["rule"] + "（DOI 权威记录的标题与它只差"
                                               "大小写，已保留你原来的写法）",
                         "evidence": tail["evidence"]}
            out["notes"].append(
                "DOI 权威记录里的标题与本地只差大小写/标点，"
                "已按你原来的写法保留（只去掉站点后缀）")
        else:
            title_fix = {"kind": "title", "current": title, "value": csl_title,
                         "confidence": "high", "source": "DOI 权威记录",
                         "rule": "DOI 权威记录里的标题（比网页 <title> 干净）",
                         "evidence": {"page": 0,
                                      "text": f"权威标题：{csl_title[:120]}"}}
            out["signals"].append("标题与 DOI 权威记录不一致")
    elif tail and tail["confidence"] == "high":
        title_fix = {"kind": "title", "current": title, "value": tail["value"],
                     "confidence": "high", "source": "站点后缀规则",
                     "rule": tail["rule"], "evidence": tail["evidence"]}
        out["signals"].append(f"标题尾部带着站点后缀「{tail['removed']}」")
    elif tail:
        # low 置信的尾巴只提示、不写成"建议值" —— 用户要自己判断
        out["notes"].append(
            f"标题尾部「{tail['removed']}」{tail['rule']}；"
            f"如果它确实不是标题的一部分，可以手工删掉（这里不替你决定）")
        out["signals"].append("标题尾部可能有站点名（拿不准）")
    if title_fix:
        out["fixes"].append(title_fix)

    if not type_fix and not out["fixes"]:
        # 弱嫌疑：类型可疑但没有任何"它其实是论文"的信号 → 不打扰用户
        if not (doi or tail or text_hint):
            out["notes"].append("除了「挂了 PDF」之外没有别的论文信号，"
                                "可能本来就是网页，不动它")
            return out

    # ---- 会丢掉哪些字段：查 Zotero 快照库里的 itemTypeFields（权威数据）
    if type_fix:
        out["will_drop"] = _fields_not_in_type(item, type_fix["value"])

    out["suspect"] = bool(out["fixes"]) or bool(out["signals"])
    if out["will_drop"]:
        out["notes"].append("改类型会丢掉这些字段（Zotero 不给新类型存）："
                            + "、".join(out["will_drop"]))
    out["notes"].append("⚠ 改条目类型是敏感操作：请在 Zotero 里右键这篇 →"
                        "「补全元数据（本地模型）」，在弹出的确认框里逐条看"
                        "「现在是什么 → 要改成什么」再决定")
    return out


def do_typefix(key: str, use_network: bool = False) -> dict:
    """按 key 找条目并算类型/标题修正建议（**只读**）。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "需要 key", "fixes": [], "suspect": False}
    try:
        import metafill
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"加载 offline/metafill.py 失败："
                                      f"{type(exc).__name__}: {exc}",
                "fixes": [], "suspect": False}
    try:
        reader = metafill._get_reader()
        for it in reader.load_items():
            if it.key == key:
                return typefix_for_item(it, use_network=use_network)
        return {"ok": False, "key": key, "error": f"Zotero 库里没有 key={key} 的条目",
                "fixes": [], "suspect": False}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "key": key, "error": f"{type(exc).__name__}: {exc}",
                "fixes": [], "suspect": False}
    finally:
        try:
            metafill.close()
        except Exception:  # noqa: BLE001
            pass


def do_typefix_scan(use_network: bool = False, limit: int = 0) -> dict:
    """全库找"类型/标题被网站污染"的条目（**只读**，面板用来列清单）。

    只处理"挂了 PDF 且类型不是正经文献类型"的少数条目，所以很快 ——
    全库 101 篇里符合条件的只有个位数，不需要像 metafill 批量那样做取消机制。
    """
    try:
        import metafill
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"加载 offline/metafill.py 失败："
                                      f"{type(exc).__name__}: {exc}", "rows": []}
    started = time.time()
    try:
        reader = metafill._get_reader()
        rows, n_suspect = [], 0
        for it in reader.load_items():
            pdfs = list(getattr(it, "pdfs", None) or [])
            itype = str(getattr(it, "item_type", "") or "")
            if not pdfs or itype in PAPER_TYPES:
                continue                     # 必要条件：挂了 PDF 且类型不正经
            r = typefix_for_item(it, use_network=use_network)
            r["n_pdfs"] = len(pdfs)
            rows.append(r)
            if r.get("suspect"):
                n_suspect += 1
            if limit and len(rows) >= limit:
                break
        # 有修正建议的排前面
        rows.sort(key=lambda r: (-len(r.get("fixes") or []), r.get("key", "")))
        return {"ok": True, "checked": len(rows), "suspect_total": n_suspect,
                "elapsed": round(time.time() - started, 1),
                "network": bool(use_network), "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "rows": []}
    finally:
        try:
            metafill.close()
        except Exception:  # noqa: BLE001
            pass


def _shared_item_pairs(mapping: dict, cols: list, min_ratio: float = 0.6) -> list[dict]:
    """找"收着大量相同文献"的分类对 —— 这是重复分类最硬的证据。

    实测：中英对照的两个分类（同一主题被分别命名）经常收的就是同一批文献。
    这比语义相似度可靠得多，而且**完全确定**（数集合交集就行）。
    """
    names = [c["name"] for c in cols if c["items"] > 0]
    keysets = {n: {i["key"] for i in mapping.get(n, [])} for n in names}
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ka, kb = keysets[a], keysets[b]
            shared = len(ka & kb)
            if not shared:
                continue
            ratio = shared / max(1, min(len(ka), len(kb)))
            if ratio >= min_ratio:
                out.append({
                    "kind": "重复分类（收着同一批文献）",
                    "a": a, "b": b,
                    "shared_items": shared,
                    "overlap_ratio": round(ratio, 2),
                    "suggest": f"两类有 {shared} 篇共同文献（占少的一方的 "
                               f"{ratio:.0%}），极可能是同一主题的双份命名，建议合并",
                })
    out.sort(key=lambda x: -x["overlap_ratio"])
    return out


def _model_judge_pairs(pairs: list[tuple], model: str) -> list[dict]:
    """让本地 LLM 判断这些分类对是不是同一主题（中英对照靠它）。

    为什么不用嵌入向量：本机的嵌入模型是 `bge-small-zh-v1.5`，实测它对
    **跨语言**几乎没有区分力 —— 「机器学习模型」↔「machine learning model」
    只有 0.518，而「强化学习」↔「reinforcement learning」只有 0.369，
    全都落在无关词的噪声区间（0.302）。同语言的无关词反而更高
    （「监督学习」↔「无监督学习」=0.619）。
    所以中英对照必须交给 LLM 判断 —— 它读得懂两边。

    一次把所有候选对交给模型（而不是逐对调用），省往返。
    """
    import judge

    listing = "\n".join(f"{i + 1}. 「{a}」 与 「{b}」" for i, (a, b) in enumerate(pairs))
    prompt = (
        "任务：判断文献库里的分类名配对，是否属于**重复分类**。\n\n"
        "【什么算重复】两者指**同一类文献**，只是语言不同或写法不同。\n"
        "  正例：「强化学习」与「Reinforcement Learning」→ 重复（中英对照）\n"
        "  正例：「分类器」与「Classifiers」→ 重复\n"
        "【什么不算重复】两者只是**相关**、有交叉，或一个是另一个的应用场景/子集。\n"
        "  反例：「生成模型」与「图像分类」→ 不重复"
        "（一个是模型类型，一个是任务类型；文献只是有交叉）\n"
        "  反例：「模型训练」与「模型推理」→ 不重复（训练与推理是两件事）\n"
        "  反例：「分类器」与「统计分析方法」→ 不重复"
        "（前者是具体组件，后者是方法大类）\n\n"
        "判断时只问一句：**把两个分类合并，会不会把不相干的文献混在一起？**\n"
        "会 → 不重复；不会且两者本就该是一类 → 重复。\n"
        "宁可判 false，也不要为了\"有关系\"就判 true。\n\n"
        f"{listing}\n\n"
        '输出 JSON：{"pairs": [{"n": 1, "duplicate": true, "reason": "简短理由"}]}'
    )
    res = judge.generate(prompt, model=model, timeout=300)
    if not res.get("ok"):
        return [{"kind": "模型判断失败", "a": "", "b": "",
                 "suggest": str(res.get("error"))[:200]}]
    ok, data = judge.parse_json(res["text"])
    if not ok or not isinstance(data, dict):
        return [{"kind": "模型输出无法解析", "a": "", "b": "",
                 "suggest": str(res.get("text"))[:200]}]
    out = []
    for entry in data.get("pairs") or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("n", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(pairs)):
            continue
        # 兼容模型可能写成 same_topic 的旧字段名
        verdict = entry.get("duplicate", entry.get("same_topic"))
        if verdict:
            a, b = pairs[idx]
            out.append({
                "kind": "重复分类（模型判断）",
                "a": a, "b": b,
                "reason": str(entry.get("reason", ""))[:160],
                "suggest": "模型认为两者是同一类文献的双份命名，建议合并"
                           "（保留收文献更多的那个）",
                "model": model,
            })
    return out


def _env_file_info() -> dict:
    """覆盖层 .env 的位置 + 当前生效的键名（面板「运行环境」页要显示）。"""
    try:
        import settings as SETT
        return {"path": SETT.path(), "keys": SETT.keys()}
    except Exception:        # noqa: BLE001 —— 读不到就当"没有覆盖层"
        return {"path": "", "keys": []}


def _python_exe() -> str:
    """本项目用的 Python 解释器（优先 venv 的 pythonw —— 无控制台窗口）。

    走 schemas.resolve_python()：它会按"用户指定 > 环境变量 > 配置 >
    运行时状态 > 扫描"的顺序找，每一步都有来源可查。
    """
    import schemas as S
    return S.resolve_python()


def _ollama_probe() -> dict:
    """Ollama 的三件事：**文件在不在 / API 通不通 / 有哪些模型**。

    ⚠ 这个形状是给插件用的：19-caps.js::probeLocalModel() 按
      {path, exists, api_up, models} 读 —— 它原来读的是 /health 的
      ollama 字段，而那个字段是**一个路径字符串**，于是 info.exists
      恒为 undefined：明明 Ollama 在跑、模型也齐，右键菜单却写
      「未连接：没找到 Ollama」（用户 2026-10-10 报的正是这个）。

    为什么不直接把 ollama 改成这个结构：面板「运行环境」页把 ollama 当
    **路径字符串**填进输入框、还会 os.path.exists 它 —— 改成结构会当场把它弄崩。
    所以保留字符串键 ollama，另加结构化的 ollama_info。
    """
    path = S.resolve_ollama()
    api_up, models = False, []
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                    timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        api_up = True
        models = [m.get("name", "") for m in (data.get("models") or [])]
    except Exception:  # noqa: BLE001
        pass
    return {
        "path": path,
        "exists": bool(path) and os.path.isfile(path),
        "api_up": api_up,
        "models": models,
        "hint": "" if path else
                "没找到 Ollama（可选组件）。不装也能用："
                "改用 OpenAI 兼容 API，或干脆不用模型功能。",
    }


def do_models() -> dict:
    """本机 Ollama 有哪些模型（引擎 `llm.models` 的响应体）。

    这段逻辑现在只有这一份：引擎 `llm.models` 直接返回它。
    （2026-10-13 从旧服务的路由里提成模块级函数；旧服务已删，别再写第二份。）
    """
    import judge as _j

    ok, names = _j.ollama_models()
    rec = _j.pick_model() if ok else ""
    out = []
    for n in names:
        tag = ""
        low = n.lower()
        if low == rec.lower():
            tag = "推荐（质量更好）"
        elif "1.7b" in low or ":0." in low or "1b" in low or "2b" in low:
            tag = "更轻量、更快"
        out.append({"name": n, "note": tag})
    payload = {
        "ok": ok,
        "models": out,
        "recommended": rec,
        "host": (_j.load_llm_config().get("ollama_host") or _j.OLLAMA_HOST),
    }
    if not ok:
        payload["error"] = "连不上本机的 Ollama"
        payload["hint"] = (
            "Ollama 没在运行。装好之后它一般会随系统启动；"
            "也可以从开始菜单 / 托盘图标启动 Ollama 程序，"
            "然后到面板「偏好设置 → 可选组件安装引导」点「重新检测」。")
    elif not names:
        payload["hint"] = ("Ollama 在跑但一个模型都没有。"
                           "先拉一个：ollama pull qwen3:4b-instruct")
    return payload


def env_report() -> dict:
    """运行环境诊断报告：当前位置 + 来源 + 所有候选 + 实测结果。

    这是给"插件设置面板"和排错用的。设计目标：用户看到报告就知道
    **该改哪一个框**，而不是只看到一句"找不到 Python 环境"。
    """
    import schemas as _S

    # ---- Python：真实跑一次 --version，确认它不是个空壳
    py = _S.resolve_python()
    py_ok, py_ver = False, ""
    if py and os.path.isfile(py):
        try:
            cp = PR.run([py, "--version"], merge_stderr=True, timeout=15)
            py_ver = ((cp.stdout or "") + (cp.stderr or "")).strip()
            py_ok = cp.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            py_ver = f"运行失败：{exc}"

    # ---- Ollama：文件在不在 + 服务通不通（两个概念，分开报）
    ollama_info = _ollama_probe()

    root = _S.resolve_project_root()
    # ⚠ W4（2026-10-14）：Tk 面板退役后 tools\gui.py 已删 —— 这个"项目根标记"换成
    #   仍然存在、语义等价的 **online\engine.py**（引擎宿主脚本）。键名 `panel` /
    #   `has_panel` 保留不动，免得动到插件设置页那几个消费者。
    panel = os.path.join(root, "online", "engine.py")

    sources = _S.resolve_sources()
    ollama_info["source"] = (sources.get("ollama") or {}).get("source", "")
    # 八个"位置"的可编辑行（面板「运行环境」页与插件设置页的只读展示共用一份）。
    # ⚠ 2026-10-16 加的：以前只有 4 项、而且只有 python/project_root/ollama 三个
    #   有顶层值；settings.js 的只读列表要用到它，engine 的 env.check 也改成
    #   直接读这一份（实现只有一处，见 schemas.env_source_items）。
    try:
        items = _S.env_source_items(sources)
    except Exception as exc:      # noqa: BLE001 —— 一项探测失败别让整份报告打不开
        log(f"生成运行环境清单失败：{exc}")
        items = []
    return {
        "ok": py_ok,
        "items": items,
        "sources": sources,
        "env_file": _env_file_info(),
        "python": {
            "path": py,
            "exists": bool(py) and os.path.isfile(py),
            "runnable": py_ok,
            "version": py_ver,
            "source": (sources.get("python") or {}).get("source", ""),
            "candidates": _S._scan_for_python(root),
            "hint": "" if py_ok else
                    "没找到能用的 Python。请点「浏览…」选到 "
                    "python.exe（或 pythonw.exe），"
                    "或者指到项目里的 .venv\\Scripts 目录下。",
        },
        "project_root": {
            "path": root,
            "exists": os.path.isdir(root),
            "source": (sources.get("project_root") or {}).get("source", ""),
            "has_venv": os.path.isdir(os.path.join(root, ".venv")),
            "has_panel": os.path.isfile(panel),   # 见上面：判据已换成 online\engine.py
            "panel": panel,
            "hint": "" if os.path.isfile(panel) else
                    "这个目录里没有 online\\engine.py，可能选错了。"
                    "它应该是包含 offline\\、online\\、zotero-plugin\\ 的那个目录。",
        },
        "ollama": ollama_info,
        "location_file": _S.LOCATION_FILE,
        "user_location_file": _S.USER_LOCATION_FILE,
        "runtime_state": _S.read_runtime_state(),
    }


def health_payload() -> dict:
    """引擎 `kb.health` 的响应体。

    2026-10-13 从旧服务的路由里提成模块级函数，现在只有这一份实现。
    返回的是**本机运行环境**（kb_dir / project_root / python / ollama…），
    不含任何密钥。
    """
    return {
        "ok": True, "service": "zotero-kb", "version": "1.0",
        "kb_dir": S.KB_DIR,
        # ⚠ 项目根（代码/venv 所在）与知识库位置（数据所在）**是两回事**：
        #   知识库默认跟着 Zotero 数据目录走，而 venv / 脚本
        #   仍在项目目录里。插件要启动管理面板就必须知道**项目根**，
        #   不能从 kb_dir 反推 —— 本机就因为反推，面板报"找不到 py 环境"
        #   （kb 搬走后，反推出的目录是 Zotero 数据目录，那里没有 .venv）。
        # ⚠ 这三个值走 schemas 的 resolve_*（支持用户在插件设置里指定），
        #   不是模块加载时定死的常量 —— 用户改完设置不用重启服务。
        "project_root": S.resolve_project_root(),
        "python": _python_exe(),
        "panel": os.path.join(S.resolve_project_root(), "online", "engine.py"),
        # ⚠ 两个键都要：
        #   · ollama 是**路径字符串** —— 面板「运行环境」页拿它填输入框
        #     （tools/panels/tab_env.py（已退役 2026-10-14）::refresh_env，还 os.path.exists 它）；
        #   · ollama_info 是**结构化状态** —— 插件 19-caps.js 按
        #     {exists, api_up, models} 判断本地模型可用不可用。
        #   2026-10-10 之前只有前者，于是插件 info.exists 恒 undefined，
        #   明明连上也显示「未连接」。
        "ollama": S.resolve_ollama(),
        "ollama_info": _ollama_probe(),
        # MinerU 是可选组件：这里只报**路径**（一次文件系统检查，快）。
        # 要"版本/后端/档位"这种要跑子进程的信息，走 /mineru-check。
        "mineru": S.resolve_mineru(),
        # 来源（"这个值从哪来"）：面板「运行环境」页紧接着每个值显示它。
        # 插件不用这一项（它只读 project_root / python / ollama），
        # 所以是纯增量、不影响任何现有前端。
        "sources": S.resolve_sources(),
        "env_file": _env_file_info(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


def _migrate_kb(target: str) -> dict:
    """把知识库迁移到 target（复制 + 备份 + 校验，不删旧目录）。

    为什么做成"服务端自己干"而不是让插件调外部脚本：
      · 迁移期间不能有别的请求在读写库（这里是同步阻塞的）
      · 备份/校验逻辑与 tools\\migrate_kb.py 一致，复用同一套判断
    ⚠ 是**复制**不是移动：迁完两边都在，确认没问题再让用户自己删。
    """
    import shutil
    import sqlite3
    import time as _t

    import schemas as S

    old = S.KB_DIR
    new = os.path.abspath(os.path.expanduser(target))
    if os.path.normcase(old) == os.path.normcase(new):
        return {"ok": True, "skipped": True, "message": "位置相同，不需要迁移",
                "kb_dir": old}
    if not os.path.exists(os.path.join(old, "index.db")):
        return {"ok": False, "error": f"{old} 里没有 index.db，不像知识库目录"}
    if os.path.exists(os.path.join(new, "index.db")):
        return {"ok": False,
                "error": f"目标 {new} 已经有 index.db 了。"
                         "换个目标，或先自己清理它（避免覆盖另一个库）。"}

    stamp = _t.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(os.path.dirname(old.rstrip("\\/")), f"_kb-backup-{stamp}")
    try:
        os.makedirs(backup, exist_ok=True)
        for name in os.listdir(old):
            src, dst = os.path.join(old, name), os.path.join(backup, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"备份失败，已中止：{exc}"}

    os.makedirs(new, exist_ok=True)
    copied = []
    for name in os.listdir(old):
        src, dst = os.path.join(old, name), os.path.join(new, name)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            copied.append(name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"复制 {name} 失败：{exc}",
                    "backup": backup}

    # 校验关键表行数
    def _counts(path: str) -> dict:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out = {}
            for label, sql in (("items", "SELECT COUNT(*) FROM items"),
                               ("chunks", "SELECT COUNT(*) FROM chunks"),
                               ("embeddings", "SELECT COUNT(*) FROM embeddings"),
                               ("experience", "SELECT COUNT(*) FROM experience")):
                try:
                    out[label] = c.execute(sql).fetchone()[0]
                except sqlite3.Error:
                    out[label] = -1
            return out
        finally:
            c.close()

    try:
        a, b = _counts(os.path.join(old, "index.db")), _counts(os.path.join(new, "index.db"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"校验失败：{exc}", "backup": backup}
    if a != b:
        return {"ok": False, "error": "数据不一致，**不要**删旧目录", "old": a, "new": b,
                "backup": backup}

    # 写 kb-location.json —— 这是"以后都用新位置"的开关
    try:
        import json as _json
        data = {}
        if os.path.exists(S.LOCATION_FILE):
            try:
                data = _json.load(open(S.LOCATION_FILE, encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                data = {}
        data["kb_dir"] = new
        os.makedirs(os.path.dirname(S.LOCATION_FILE), exist_ok=True)
        with open(S.LOCATION_FILE, "w", encoding="utf-8") as fh:
            _json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"数据已复制，但写 kb-location.json 失败：{exc}",
                "backup": backup, "new": new}

    return {"ok": True, "kb_dir": new, "old_dir": old, "backup": backup,
            "verified": a, "copied_count": len(copied),
            "message": "迁移完成。重启服务后生效（当前进程仍指向旧位置）。"}


def _sync_to_folder(src: str, dst: str) -> dict:
    """把知识库目录同步到一个目标目录（交给坚果云/OneDrive 客户端再同步）。

    ⚠ 用"复制 + 覆盖"而不是镜像删除：万一目标目录里还有别的东西，
      镜像（/MIR）会把它们删掉 —— 这种事不该由我们代劳。
    """
    import shutil

    dst = os.path.abspath(os.path.expanduser(dst))
    try:
        os.makedirs(dst, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"建目标目录失败：{exc}"}
    if os.path.normcase(dst) == os.path.normcase(src):
        return {"ok": False, "error": "目标和知识库是同一个目录"}

    n = skipped = 0
    bytes_copied = 0
    errors = []
    for dp, _dn, fn in os.walk(src):
        rel = os.path.relpath(dp, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rel}: {exc}")
            continue
        for f in fn:
            s, d = os.path.join(dp, f), os.path.join(out, f)
            try:
                # 大小和时间都一样就跳过（省时间；内容变了大小/时间通常会变）
                if os.path.exists(d):
                    ss, ds = os.stat(s), os.stat(d)
                    if ss.st_size == ds.st_size and int(ss.st_mtime) <= int(ds.st_mtime):
                        skipped += 1
                        continue
                shutil.copy2(s, d)
                n += 1
                bytes_copied += os.path.getsize(s)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}\\{f}: {exc}")
    return {"ok": not errors, "copied": n, "skipped": skipped,
            "mb": round(bytes_copied / 1048576, 1), "target": dst,
            "errors": errors[:10],
            "message": f"已复制 {n} 个文件（{round(bytes_copied / 1048576, 1)} MB），"
                       f"跳过未变的 {skipped} 个。"}


def _sync_by_command(cmd: str) -> dict:
    """执行一条同步命令（rclone / robocopy 等），返回输出。

    ⚠ 这是"用户自己填命令、我们照着跑"，所以只做最基础的防护：
      拒绝明显的危险模式（格式化、删除整盘）。真要防住得靠用户自己看清单。
    """
    import subprocess

    low = cmd.lower()
    for bad in ("format ", "rm -rf /", "del /f /s /q c:\\", "rd /s /q c:\\"):
        if bad in low:
            return {"ok": False, "error": f"命令里有危险片段，拒绝执行：{bad}"}
    try:
        # shell=True 是必要的（用户填的是完整命令行），所以上面做了粗过滤
        p = PR.run(cmd, shell=True, merge_stderr=True, timeout=1800)
        out = ((p.stdout or "") + (p.stderr or ""))[-2000:]
        return {"ok": p.returncode == 0, "returncode": p.returncode,
                "output": out,
                "message": "命令执行完成" if p.returncode == 0
                           else f"命令返回 {p.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令超过 30 分钟还没结束，已放弃等待"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _persist_llm_overrides(body: dict) -> None:
    """把请求里带的模型配置落盘（有才写，没有就不动）。

    为什么要落盘而不是只用一次：插件设置面板是配置的**唯一来源**，
    但服务端还会被别的入口调用（管理面板、命令行、定时任务）。
    把插件传来的配置存下来，那些入口就不用各自再配一遍 —— 配置只有一份。

    ⚠ 两种请求要区别对待（本机踩过其中一个）：

      · **零散覆盖**（body 里没有 provider）：只写传了的非空字段。
        避免"另一个入口顺手带了 model 就把 base_url 抹掉"。

      · **完整提交**（body 里有 provider）：三个字段（model / base_url /
        api_key）**整份覆盖，包括写空值**。
        为什么必须支持写空：用户在设置里**清空** key 或模型名是个明确动作
        （"我不用 API 了"），如果沿用"空值不写"，旧 key 会一直留着 ——
        表现为"我明明清空了，却还报旧 key 认证失败"。
        插件每次分类都会带上 provider，所以走的是这一支。
    """
    if not isinstance(body, dict):
        return
    try:
        import judge as _j
    except ImportError:
        return

    full = bool(str(body.get("provider") or "").strip())
    kw: dict = {}
    for k in ("provider", "base_url", "api_key", "model", "ollama_host"):
        if k not in body:
            continue
        v = body.get(k)
        if not isinstance(v, str):
            continue
        if full or v.strip():
            kw[k] = v.strip()          # 完整提交时允许空值（= 清除）
    if not kw:
        return
    # 与现有配置比对，没变化就不写文件（避免每次分类都动磁盘）
    try:
        cur = _j.load_llm_config()
        if all(cur.get(k) == v for k, v in kw.items()):
            return
        _j.save_llm_config(**kw)
    except Exception:  # noqa: BLE001
        pass          # 配置写失败不该让分类请求整体失败


def _safe_llm_config() -> dict:
    """回给调用方的配置：**不含 api_key 明文**，只说有没有配。"""
    try:
        import judge as _j
    except ImportError:
        return {}
    cfg = dict(_j.load_llm_config())
    key = cfg.pop("api_key", "")
    cfg["has_key"] = bool(key)
    # 只露尾部 4 位，便于确认"是不是我配的那把"
    cfg["key_tail"] = ("…" + key[-4:]) if len(key) >= 8 else ("已设置" if key else "")
    return cfg


def do_classify_one(item_meta: dict, categories: list[dict],
                    model: str = "", feedback: str = "",
                    previous: dict | None = None, provider: str = "",
                    base_url: str = "", api_key: str = "") -> dict:
    """给**一篇新文献**推荐分类与标签（插件弹窗用）。

    `feedback` / `previous` 支持"跟模型对话调整"：用户不满意上一轮建议时，
    把原建议 + 一句人话一起发回来重新判断。

    `provider` / `base_url` / `api_key` **直接透传给 judge**，不依赖"先落盘
    再读回"（那条路在"用户清空 key"和"落盘失败"两种情况下会用到旧配置，
    报的错和用户填的对不上）。

    ⚠ 算法已抽到 `judge.classify_item` —— 管理面板的 CLI 也调那一份。
       这里只做转发，**不要**再往这里写提示词逻辑（两份必然漂移）。
    """
    import judge

    return judge.classify_item(item_meta, categories, model,
                               feedback=feedback, previous=previous,
                               provider=provider, base_url=base_url,
                               api_key=api_key)



def _has_column(conn, table: str, column: str) -> bool:
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        return column in cols
    except Exception:  # noqa: BLE001
        return False


PENDING_PATH = os.path.join(S.KB_DIR, "pending-suggestions.json")


def _read_pending() -> list[dict]:
    if not os.path.exists(PENDING_PATH):
        return []
    try:
        return json.load(open(PENDING_PATH, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _append_pending(rows: list[dict]) -> None:
    data = _read_pending()
    seen = {r.get("key") for r in data}
    data.extend(r for r in rows if r.get("key") not in seen)
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    with open(PENDING_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def do_collection_suggest(use_model: bool) -> dict:
    """分析现有分类与文献分布，产出分类整理建议。

    三层证据，从硬到软：
      1. **名字包含**（确定性）：一个名字是另一个的子串 —— 中英对照常见。
      2. **文献重合**（确定性）：两类收着 ≥60% 相同的文献 —— 重复命名最硬的证据。
      3. **模型判断**（可选）：把剩下的候选对交给本地 LLM 判"是否同一主题"，
         因为嵌入向量对跨语言没有区分力（见 `_model_judge_pairs` 的说明）。
    另外总是报告：空分类、条目极少的分类、未归类文献。
    """
    from searcher import Searcher

    s = Searcher()
    try:
        cols = s.collections()          # [{name, items}]
        mapping: dict[str, list[dict]] = {}
        for row in s.read("SELECT key, title, year, collections FROM items"):
            for name in json.loads(row["collections"] or "[]"):
                mapping.setdefault(name, []).append(
                    {"key": row["key"], "title": row["title"], "year": row["year"]})

        report: dict = {"collections": [], "issues": []}
        for c in cols:
            items = mapping.get(c["name"], [])
            report["collections"].append({
                "name": c["name"],
                "count": c["items"],
                "sample": [i["title"][:60] for i in items[:6]],
            })

        # 1) 名字包含
        names = [c["name"] for c in cols]
        reported_pairs: set[frozenset] = set()
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                la, lb = a.lower(), b.lower()
                if la in lb or lb in la:
                    reported_pairs.add(frozenset((a, b)))
                    report["issues"].append({
                        "kind": "疑似重复分类（名字包含）", "a": a, "b": b,
                        "suggest": f"合并为一个，建议保留「{a if len(a) >= len(b) else b}」",
                    })

        # 2) 文献重合
        for issue in _shared_item_pairs(mapping, cols):
            pair = frozenset((issue["a"], issue["b"]))
            if pair in reported_pairs:
                continue
            reported_pairs.add(pair)
            report["issues"].append(issue)

        # 3) 模型判断剩余的候选对
        #    候选范围：**名字里含非中文**的（中英对照的主要嫌疑），
        #    或者名字较短容易泛指的。全量两两组合会太多，这里按需筛。
        total_pairs = len(names) * (len(names) - 1) // 2
        if use_model and total_pairs:
            import judge
            if not judge.ollama_available()[0]:
                report["model_note"] = "本地模型不可用，只给了确定性分析"
            else:
                candidates = []
                for i, a in enumerate(names):
                    for b in names[i + 1:]:
                        if frozenset((a, b)) in reported_pairs:
                            continue
                        # 至少一方不是纯中文 → 可能是跨语言命名
                        def has_ascii(x):
                            return any(ch.isascii() and ch.isalpha() for ch in x)
                        if has_ascii(a) or has_ascii(b):
                            candidates.append((a, b))
                if candidates:
                    model = judge.pick_model()
                    report["model"] = model
                    report["model_checked_pairs"] = len(candidates)
                    for issue in _model_judge_pairs(candidates, model):
                        if issue.get("a"):
                            reported_pairs.add(frozenset((issue["a"], issue["b"])))
                        report["issues"].append(issue)
                else:
                    report["model_note"] = "没有需要模型判断的候选对"

                # 模型层面的整体整理建议（分类结构）
                # ⚠ 别写成列表推导式：[{...} for c in report["collections"] ...]
                # 会遮蔽外面 `for c in cols` 的分类字典 c（踩过一次 KeyError: 'items'）
                brief_rows = []
                for col in report["collections"]:
                    brief_rows.append({"name": col["name"], "count": col["count"],
                                       "sample": col["sample"][:4]})
                brief = json.dumps(brief_rows, ensure_ascii=False)[:3500]
                prompt2 = (
                    "下面是一个文献库的分类现状（JSON）。请给出分类整理建议：\n"
                    "1) 建议的目标结构（可用一级/二级，用「父/子」表示）；\n"
                    "2) 每个建议的理由。\n"
                    "只依据给定信息，不要编造分类里没有的主题。\n"
                    '输出 JSON：{"structure": [{"path": "父/子", "count": 12, '
                    '"note": "..."}], "notes": "总体说明"}\n\n' + brief
                )
                res2 = judge.generate(prompt2, model=model, timeout=300)
                if res2.get("ok"):
                    ok2, data2 = judge.parse_json(res2["text"])
                    report["model_structure"] = (data2 if ok2
                                                 else {"raw": res2["text"][:2000]})

        # 4) 空分类 / 条目很少
        for c in cols:
            if c["items"] == 0:
                report["issues"].append({
                    "kind": "空分类", "a": c["name"],
                    "suggest": "删除，或把相关文献归进来"})
            elif c["items"] <= 2:
                report["issues"].append({
                    "kind": "条目很少", "a": c["name"], "count": c["items"],
                    "suggest": "考虑并入更宽的分类"})

        # 5) 未归类
        all_keys = {r["key"] for r in s.read("SELECT key FROM items")}
        classified: set[str] = set()
        for items in mapping.values():
            classified.update(i["key"] for i in items)
        unclassified = sorted(all_keys - classified)
        if unclassified:
            report["issues"].append({
                "kind": "未归类文献", "count": len(unclassified),
                "keys": unclassified[:20],
                "suggest": "用模型按主题分配，或手工归类"})
        report["unclassified_count"] = len(unclassified)
        return report
    finally:
        s.close()


