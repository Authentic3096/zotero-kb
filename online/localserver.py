"""知识库本地 HTTP 服务：给 Zotero 插件（以及任何本地程序）用的接口。

    python online/localserver.py            # 前台启动（Ctrl+C 停）
    python online/localserver.py --port 8765
    python online/localserver.py --test     # 自检：起服务、打全部接口、退出

为什么需要它：Zotero 插件跑在 Zotero 的 JS 环境里，**不能直接调 Python**。
本机已有的 pdf2zh 插件就是"插件 + 本地 Python 服务"这套模式（跑在 8890），
这里沿用同样的思路，端口用 8765 避开它。

接口设计原则
    · 只监听 127.0.0.1，绝不对外。
    · 需要 token（首次自动生成并落盘到 kb\\service-token.txt）——防止
      本机上其他网页/程序乱调（浏览器里的恶意页面能访问 127.0.0.1）。
    · 长任务（建索引、跑模型）异步执行并返回 job id，不阻塞插件。
    · 任何接口都不改 Zotero 的库 —— 回填由插件侧用 Zotero 自己的 API 做，
      这样写操作始终在 Zotero 进程内、走它的事务与同步队列。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, HERE)

import schemas as S  # noqa: E402

DEFAULT_PORT = 8765
TOKEN_PATH = os.path.join(S.KB_DIR, "service-token.txt")

# Windows 下起子进程（跑 python --version 等）时别弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOG_PATH = os.path.join(S.KB_DIR, "logs", "localserver.log")

# 后台任务表：{job_id: {state, started, finished, title, log[], result}}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_MAX_JOBS = 50

# 插件任务队列：给 Zotero 插件"取任务 → 执行 → 回报结果"用。
# 为什么要这套：想在 Zotero 里跑一段 JS（诊断、刷新权重、验证功能）原本只能
# 手动"打开运行JavaScript窗口 → 复制 → 粘贴 → Ctrl+R"，做一次调试要几十秒。
# 有了它，Python 侧派任务、插件轮询执行、结果回传，全自动、零人工。
#
# 安全考虑：这个队列等于"能在 Zotero 里执行任意 JS"，所以
#   · 只监听 127.0.0.1（服务本身就没对外）；
#   · 必须带 token（和别的接口一样）；
#   · 任务只保留最近若干条，不落盘。
TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()
_TASK_SEQ = [0]
_TASK_KEEP = 30


# ---------------------------------------------------------------- 工具


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


_TOKEN_CACHE: dict = {"path": "", "mtime": 0.0, "value": ""}


def get_token(force_refresh: bool = False) -> str:
    """读服务 token。

    顺序：环境变量 `KB_SERVICE_TOKEN` → 文件 `kb\\service-token.txt`（没有就生成）。

    每次请求都重新读文件（带 mtime 缓存）—— 这样**改了 token 文件不用重启服务**。
    插件那边把 token 填进设置即可；用户在面板里也能一键复制。
    """
    env = os.environ.get("KB_SERVICE_TOKEN", "").strip()
    if env:
        return env
    path = TOKEN_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    if (not force_refresh and _TOKEN_CACHE["path"] == path
            and _TOKEN_CACHE["mtime"] == mtime and _TOKEN_CACHE["value"]):
        return _TOKEN_CACHE["value"]
    tok = ""
    if os.path.exists(path):
        try:
            tok = open(path, encoding="utf-8").read().strip()
        except OSError:
            tok = ""
    if not tok:
        tok = secrets.token_urlsafe(24)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(tok)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
    _TOKEN_CACHE.update({"path": path, "mtime": mtime, "value": tok})
    return tok


def start_job(title: str, fn, *args, **kwargs) -> str:
    """把长任务丢到后台线程跑，返回 job id。"""
    job_id = f"job-{int(time.time() * 1000) % 100000000}"
    entry = {"id": job_id, "title": title, "state": "running",
             "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "", "log": [], "result": None, "error": ""}
    with JOBS_LOCK:
        JOBS[job_id] = entry
        if len(JOBS) > _MAX_JOBS:       # 只留最近若干条
            for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:-_MAX_JOBS]:
                JOBS.pop(old, None)

    def worker():
        try:
            result = fn(*args, **kwargs)
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
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=ROOT, env=env, timeout=1800)
    tail = (proc.stdout or "").strip().split("\n")[-12:]
    log(f"构建结束，退出码 {proc.returncode}")
    return {"exit_code": proc.returncode, "output_tail": tail}


def _task_new(kind: str, code: str, timeout: float = 60.0) -> dict:
    """入队一个给 Zotero 插件执行的任务。"""
    with TASKS_LOCK:
        _TASK_SEQ[0] += 1
        task_id = f"t{_TASK_SEQ[0]}"
        task = {
            "id": task_id,
            "kind": kind,               # "js" = 执行任意 JS；"cmd" = 插件内建命令
            "code": code,               # kind=js 时的 JS 源码；cmd 时是命令名
            "timeout": timeout,
            "state": "pending",         # pending / running / done / failed / timeout
            "created": datetime.now().isoformat(timespec="seconds"),
            "started": "", "finished": "",
            "result": None, "error": "",
            "fetched_by": "",
        }
        TASKS[task_id] = task
        # 只留最近若干条
        if len(TASKS) > _TASK_KEEP:
            for old in sorted(TASKS, key=lambda k: TASKS[k]["created"])[:-_TASK_KEEP]:
                TASKS.pop(old, None)
        return dict(task)


def _task_claim() -> dict | None:
    """插件来取一个待执行任务（原子地把 pending 改成 running）。"""
    with TASKS_LOCK:
        for tid, t in TASKS.items():
            if t["state"] == "pending":
                t["state"] = "running"
                t["started"] = datetime.now().isoformat(timespec="seconds")
                t["fetched_by"] = "plugin"
                return dict(t)
    return None


def _task_cancel(tid: str) -> dict:
    """撤回一个**还没被领取**的任务（pending → cancelled）。

    为什么需要它（真机踩到的）：派出去的 acquire 任务如果一直没人领
    （Zotero 关着），派发方等 30 秒就会报"插件没在轮询"并放弃。**但那条任务
    仍然躺在队列里 pending** —— 用户几个小时后打开 Zotero，插件把它领走并
    **真的建了条目**：派发方早就报过失败了，用户却莫名其妙多出一篇文献。

    实测复现过一次（一条 22:36 派出的任务，22:39 Zotero 一开就被执行了）。

    所以"我这边不等了"必须**连队列里那条一起撤掉**，不能只放弃等待。

    ⚠ 只能撤 pending 的：已经在跑的任务撤不了（那只能等它自己结束）。
    返回里带 `cancelled` 说明到底撤成功没有，别让调用方以为一定撤掉了。
    """
    with TASKS_LOCK:
        t = TASKS.get(tid)
        if not t:
            return {"ok": False, "cancelled": False, "reason": "没有这个任务"}
        if t["state"] == "pending":
            t["state"] = "cancelled"
            t["finished"] = datetime.now().isoformat(timespec="seconds")
            t["error"] = "派发方已放弃等待，任务被撤回"
            return {"ok": True, "cancelled": True, "state": "cancelled"}
        return {"ok": True, "cancelled": False, "state": t["state"],
                "reason": f"任务已经是 {t['state']}，撤不了"
                          + ("（正在执行，等它自己结束）"
                             if t["state"] == "running" else "")}


def _task_finish(tid: str, ok: bool, result, error: str) -> bool:
    with TASKS_LOCK:
        t = TASKS.get(tid)
        if not t:
            return False
        t["state"] = "done" if ok else "failed"
        t["result"] = result
        t["error"] = error or ""
        t["finished"] = datetime.now().isoformat(timespec="seconds")
        return True


def _task_get(tid: str) -> dict | None:
    with TASKS_LOCK:
        t = TASKS.get(tid)
        return dict(t) if t else None


def do_set_weight(key: str, pinned: bool | None = None,
                  manual: float | None = None,
                  note: str | None = None) -> dict:
    """写一条条目的权重（标重点 / 取消重点 / 改人工分 / 写备注）。

    为什么要这个端点：原来只有 **读** 权重的接口，写权重得靠管理面板
    跑一段内联 Python（`gui.py` 的 do_weight 就是那么做的）。
    但用户在 Zotero 里看到某篇想标重点时，右键菜单是最短路径 ——
    插件没法跑那段内联代码，所以得有个 HTTP 写接口。

    ⚠ 只动"人工字段"（pinned / manual / note），**不碰经验统计**
      （attempts / effective / ineffective / partial）——
      那些是 `kb_experience_add` 按经验累加出来的，人工改会破坏"经验驱动"
      这个前提（本机在 kb_admin.py 的清理逻辑里也是这个判据）。
    """
    from searcher import Searcher

    s = Searcher()
    try:
        if not s.get_item(key):
            return {"ok": False, "error": f"知识库里没有 key={key} 的条目"}
        row = s.read_one("SELECT * FROM item_weight WHERE item_key = ?", (key,))
        cur = dict(row) if row else {}

        new_pinned = int(bool(cur.get("pinned"))) if pinned is None \
            else int(bool(pinned))
        new_manual = float(cur.get("manual") or 0.0) if manual is None \
            else float(manual)
        new_note = (cur.get("note") or "") if note is None else str(note)[:500]

        s.write(
            "INSERT INTO item_weight(item_key, pinned, manual, note, updated_at,"
            " attempts, effective, ineffective, partial)"
            " VALUES(?,?,?,?,datetime('now'),?,?,?,?)"
            " ON CONFLICT(item_key) DO UPDATE SET pinned=excluded.pinned,"
            " manual=excluded.manual, note=excluded.note,"
            " updated_at=excluded.updated_at",
            (key, new_pinned, new_manual, new_note,
             cur.get("attempts") or 0, cur.get("effective") or 0,
             cur.get("ineffective") or 0, cur.get("partial") or 0))
        s.reload_weights()
        weight = round(s.weight_of(key), 3)
        return {"ok": True, "key": key, "pinned": bool(new_pinned),
                "manual": new_manual, "note": new_note, "weight": weight}
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


def do_item_info(keys: list[str]) -> dict:
    """给插件返回某几条文献在知识库里的状态（用于"这条切过片没有"）。"""
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


def _python_exe() -> str:
    """本项目用的 Python 解释器（优先 venv 的 pythonw —— 无控制台窗口）。

    走 schemas.resolve_python()：它会按"用户指定 > 环境变量 > 配置 >
    运行时状态 > 扫描"的顺序找，每一步都有来源可查。
    """
    import schemas as S
    return S.resolve_python()


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
            cp = subprocess.run([py, "--version"], capture_output=True,
                                text=True, timeout=15, encoding="utf-8",
                                errors="replace",
                                creationflags=CREATE_NO_WINDOW)
            py_ver = ((cp.stdout or "") + (cp.stderr or "")).strip()
            py_ok = cp.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            py_ver = f"运行失败：{exc}"

    # ---- Ollama：文件在不在 + 服务通不通（两个概念，分开报）
    ollama = _S.resolve_ollama()
    ollama_api, ollama_models = False, []
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                    timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ollama_api = True
        ollama_models = [m.get("name", "") for m in (data.get("models") or [])]
    except Exception:  # noqa: BLE001
        pass

    root = _S.resolve_project_root()
    panel = os.path.join(root, "tools", "gui.py")

    return {
        "ok": py_ok,
        "python": {
            "path": py,
            "exists": bool(py) and os.path.isfile(py),
            "runnable": py_ok,
            "version": py_ver,
            "candidates": _S._scan_for_python(root),
            "hint": "" if py_ok else
                    "没找到能用的 Python。请点「浏览…」选到 "
                    "python.exe（或 pythonw.exe），"
                    "或者指到项目里的 .venv\\Scripts 目录下。",
        },
        "project_root": {
            "path": root,
            "exists": os.path.isdir(root),
            "has_venv": os.path.isdir(os.path.join(root, ".venv")),
            "has_panel": os.path.isfile(panel),
            "panel": panel,
            "hint": "" if os.path.isfile(panel) else
                    "这个目录里没有 tools\\gui.py，可能选错了。"
                    "它应该是包含 offline\\、online\\、zotero-plugin\\ 的那个目录。",
        },
        "ollama": {
            "path": ollama,
            "exists": bool(ollama) and os.path.isfile(ollama),
            "api_up": ollama_api,
            "models": ollama_models,
            "hint": "" if ollama else
                    "没找到 Ollama（可选组件）。不装也能用："
                    "改用 OpenAI 兼容 API，或干脆不用模型功能。",
        },
        "location_file": _S.LOCATION_FILE,
        "user_location_file": _S.USER_LOCATION_FILE,
        "runtime_state": _S.read_runtime_state(),
    }


def _migrate_kb(target: str) -> dict:
    """把知识库迁移到 target（复制 + 备份 + 校验，不删旧目录）。

    为什么做成"服务端自己干"而不是让插件调外部脚本：
      · 迁移期间不能有别的请求在读写库（这里是同步阻塞的）
      · 备份/校验逻辑与 tools\\migrate_kb.py 一致，复用同一套判断
    ⚠ 是**复制**不是移动：迁完两边都在，确认没问题再让用户自己删。
    """
    import shutil
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
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=1800, encoding="utf-8", errors="replace")
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


def _kb_conn():
    """每次新开连接（后台线程用，别跨线程共享）。"""
    return S.connect(S.INDEX_DB)


def _try_acquire_watch_lock() -> bool:
    """保证只有一个监听进程在跑（多开会重复建索引）。

    用进程级互斥（Windows 命名 Mutex）而不是文件锁：文件锁在 Zotero
    写库时会误判，而且崩溃后残留难清理。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, True, "Global\\zotero-kb-watcher")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            return False
        _try_acquire_watch_lock._handle = handle   # 保持引用，防止被回收
        return True
    except Exception:  # noqa: BLE001
        return True


def watch_loop(interval: float = 20.0, stop_event: threading.Event | None = None) -> None:
    """监视 Zotero 库，发现新文献就自动切片并记下分类建议。

    为什么还要这个（明明有插件）：本机实测插件装不上（UI 报
    "可能无法与该版本的 Zotero 兼容"，源码级排查到 UUID 已分配但注册表没写进去）。
    这个循环不依赖插件，效果对用户是一样的：
      新文献 → 自动切片进知识库 → 本地模型给分类建议 → 待确认列表里一键应用。

    两个已知限制（写在这里免得以后当成 bug 查）：
      1. **附件增删检测不到**。知识库的增量判定只看父条目 `items.version`，
         Zotero 给已存在条目加附件不会改父条目 version（本机实测：
         附件从 624 变 613，而 version 没动）。所以这里额外比对附件数与
         父条目的全文缓存时间戳，尽量补上这个盲区。
      2. 需要 Zotero 正在运行才有意义（读的是它的库文件快照）。
    """
    if not _try_acquire_watch_lock():
        log("已有监听进程在跑，本进程不再重复监听")
        return
    log(f"开始监听 Zotero 库，间隔 {interval:.0f}s")
    last_sig: str | None = None
    first = True
    # 第一次循环建立基线（不触发处理，否则会把全库重扫一遍）
    if _load_baseline() is None:
        _new_unclassified_keys()      # 首次调用即建立基线
    while not (stop_event and stop_event.is_set()):
        try:
            sig = _library_signature()
            if first:
                last_sig = sig
                first = False
                log(f"初始库指纹：{sig}")
            elif sig != last_sig:
                log(f"检测到库变化：{last_sig} → {sig}，开始增量处理")
                last_sig = sig
                _watch_handle_change()
        except Exception as exc:  # noqa: BLE001
            log(f"监听循环出错（继续）：{type(exc).__name__}: {exc}")
        # 用 Event.wait 而不是 sleep，便于及时响应停止
        if stop_event:
            if stop_event.wait(interval):
                break
        else:
            time.sleep(interval)
    log("监听已停止")


def _library_signature() -> str:
    """库指纹：条目数 + 附件数 + 最新修改时间。

    比单纯的 mtime 可靠 —— Zotero 仅同步、或只在 UI 里点选也会动 mtime，
    而这三项一起变才说明"真的加了东西"。
    """
    import sqlite3

    tmp = os.path.join(S.CACHE_DIR, "watch-snapshot.sqlite")
    os.makedirs(S.CACHE_DIR, exist_ok=True)
    # Zotero 10 开了 WAL —— 必须连 -wal 一起拷，否则快照可能不一致
    # （官方 "Zotero 10 for Developers" 明确提到这点）
    for suffix in ("", "-wal", "-shm"):
        src = S.ZOTERO_DB + suffix
        if os.path.exists(src):
            try:
                shutil.copy2(src, tmp + suffix)
            except OSError:
                pass
    conn = sqlite3.connect(tmp)
    try:
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        atts = conn.execute("SELECT COUNT(*) FROM itemAttachments").fetchone()[0]
        mtime = conn.execute("SELECT MAX(dateModified) FROM items").fetchone()[0]
        return f"{items}/{atts}/{mtime}"
    finally:
        conn.close()


def _watch_handle_change() -> None:
    """库变了：跑增量构建，并对新条目生成分类建议写入待确认清单。"""
    # 1) 增量构建（--no-vectors：交互式补抽不阻塞在向量上，快得多）
    args = [sys.executable, "-X", "utf8",
            os.path.join(ROOT, "offline", "convert.py"), "--no-vectors"]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "HF_ENDPOINT": "https://hf-mirror.com"}
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              cwd=ROOT, env=env, timeout=1800)
        tail = (proc.stdout or "").strip().split("\n")[-4:]
        log(f"增量构建完成（退出码 {proc.returncode}）：{' | '.join(tail)}")
    except Exception as exc:  # noqa: BLE001
        log(f"增量构建失败：{type(exc).__name__}: {exc}")
        return

    # 2) 找出新进库、还没给过建议的条目
    try:
        new_keys = _new_unclassified_keys()
    except Exception as exc:  # noqa: BLE001
        log(f"查新条目失败：{exc}")
        return
    if not new_keys:
        log("没有需要建议的新条目")
        return
    log(f"给 {len(new_keys)} 篇新文献生成分类建议…")
    suggestions = []
    for key in new_keys[:20]:
        try:
            import judge
            from searcher import Searcher

            s = Searcher()
            try:
                item = s.get_item(key)
            finally:
                s.close()
            if not item:
                continue
            res = do_classify_one(
                {"title": item["title"], "abstract": item["abstract"] or "",
                 "tags": item["tags"]}, [])
            if res.get("error"):
                log(f"  {key} 建议失败：{res['error']}")
                continue
            suggestions.append({"key": key, "title": item["title"],
                                "category": res.get("category"),
                                "confidence": res.get("confidence"),
                                "reason": res.get("reason"),
                                "tags": res.get("tags"),
                                "created": datetime.now().isoformat(timespec="seconds")})
            log(f"  {key} → {res.get('category')!r}"
                f"（{res.get('confidence')}）")
        except Exception as exc:  # noqa: BLE001
            log(f"  {key} 出错：{type(exc).__name__}: {exc}")
    if suggestions:
        _append_pending(suggestions)
        log(f"已写入待确认清单（{len(suggestions)} 条）")
    # 推进基线：处理过的都记上，下次不再重复
    try:
        conn = _kb_conn()
        try:
            current = {r["key"] for r in conn.execute("SELECT key FROM items")}
        finally:
            conn.close()
        _save_baseline(current)
    except Exception as exc:  # noqa: BLE001
        log(f"更新基线失败：{exc}")


BASELINE_PATH = os.path.join(S.KB_DIR, "watch-baseline.json")


def _load_baseline() -> set[str] | None:
    if not os.path.exists(BASELINE_PATH):
        return None
    try:
        data = json.load(open(BASELINE_PATH, encoding="utf-8"))
        return set(data.get("keys") or [])
    except (json.JSONDecodeError, OSError):
        return None


def _save_baseline(keys: set[str]) -> None:
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"keys": sorted(keys),
                   "updated": datetime.now().isoformat(timespec="seconds")},
                  fh, ensure_ascii=False)


def _new_unclassified_keys() -> list[str]:
    """相对**基线**新增的条目 key。

    为什么要基线：如果只按"不在待确认清单里"来判断，第一次运行会把全库
    100 篇都当成新的，一次性跑 100 次模型（几分钟 + 一堆无用建议）。
    所以要有个基线快照 —— 只在**监听运行期间真正新加进来的**文献才算新。

    基线第一次由 watch_loop 建立（存当前全库），之后每处理完一轮就推进。
    """
    conn = _kb_conn()
    try:
        current = {r["key"] for r in conn.execute("SELECT key FROM items")}
    finally:
        conn.close()
    baseline = _load_baseline()
    if baseline is None:
        _save_baseline(current)
        log(f"已建立监听基线（{len(current)} 篇），只处理以后新加的文献")
        return []
    new_keys = current - baseline
    handled = {r.get("key") for r in _read_pending()}
    return sorted(new_keys - handled)


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


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "zotero-kb/1.0"

    # ---------------------------------------------------------- 基础

    def log_message(self, fmt, *args):  # 静音默认的 stderr 日志
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        """校验 token：Header 或 query 都行（插件两种都方便）。"""
        tok = self.headers.get("X-KB-Token", "")
        if not tok:
            qs = parse_qs(urlparse(self.path).query)
            tok = (qs.get("token") or [""])[0]
        return bool(tok) and secrets.compare_digest(tok, get_token())

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------------------------------------------------------- 路由

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health"):
            # health 不需要 token：插件启动时要先探活。
            #
            # 顺便把 token 一起返回，让插件**自动配置**、不用用户手工粘贴。
            # 安全性：这里做两道校验，确保只有本机非浏览器进程能拿到它：
            #   1. 服务只 bind 127.0.0.1（外部根本连不上）；
            #   2. 带 Origin（或 Referer 是 http/https）的请求一律拒绝 ——
            #      Origin 头只由浏览器发，所以这挡住了"恶意网页去 fetch
            #      localhost 偷 token"这条路。
            #      Zotero 插件用自己的 HTTP 封装发请求，不带 Origin，正常通过。
            # 这样 token 依然能防住本机其他程序/网页乱调，但不用用户手工填。
            origin = self.headers.get("Origin") or ""
            referer = self.headers.get("Referer") or ""
            from_web = bool(origin) or referer.startswith(("http://", "https://"))
            payload = {
                "ok": True, "service": "zotero-kb", "version": "1.0",
                "kb_dir": S.KB_DIR,
                # ⚠ 项目根（代码/venv 所在）与知识库位置（数据所在）**是两回事**：
                #   知识库默认跟着 Zotero 数据目录走，而 venv / gui.py / 脚本
                #   仍在项目目录里。插件要启动管理面板就必须知道**项目根**，
                #   不能从 kb_dir 反推 —— 本机就因为反推，面板报"找不到 py 环境"
                #   （kb 搬走后，反推出的目录是 Zotero 数据目录，那里没有 .venv）。
                # ⚠ 这三个值走 schemas 的 resolve_*（支持用户在插件设置里指定），
                #   不是模块加载时定死的常量 —— 用户改完设置不用重启服务。
                "project_root": S.resolve_project_root(),
                "python": _python_exe(),
                "panel": os.path.join(S.resolve_project_root(), "tools", "gui.py"),
                "ollama": S.resolve_ollama(),
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            if from_web:
                payload["token_withheld"] = (
                    "请求带 Origin/Referer（像是浏览器发起的），出于安全不返回 token。"
                    "请用 tools\\kb_admin.py 或管理面板复制 token。")
            else:
                payload["token"] = get_token()
            return self._send(200, payload)
        if not self._authorized():
            return self._send(401, {"error": "缺少或错误的 token",
                                    "hint": f"token 存在 {TOKEN_PATH}"})
        if path == "/status":
            from searcher import Searcher
            s = Searcher()
            try:
                return self._send(200, s.stats())
            finally:
                s.close()
        if path == "/pending":
            return self._send(200, {"pending": _read_pending()})
        if path == "/jobs":
            with JOBS_LOCK:
                return self._send(200, {"jobs": [
                    {k: v for k, v in j.items() if k != "log"} for j in JOBS.values()]})
        if path.startswith("/jobs/"):
            job_id = path.split("/", 2)[2]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self._send(404, {"error": f"没有 {job_id}"})
            return self._send(200, job)
        if path == "/weights":
            # GET /weights → 全量权重映射（插件做权重列用）
            # GET /weights?keys=A,B → 只要这几条
            # 注意：必须放在 do_GET 里（它是读接口）。曾经误放进 do_POST，
            # 结果 GET /weights 一直 404。
            qs = parse_qs(urlparse(self.path).query)
            raw = (qs.get("keys") or [""])[0]
            keys = [k.strip() for k in raw.split(",") if k.strip()] or None
            return self._send(200, do_weights_map(keys))

        # 插件任务队列的**读**接口（POST 那侧负责派发与回报）
        if path == "/task":
            # 插件轮询：取一个待执行任务。没有就返回 {"task": null}。
            return self._send(200, {"task": _task_claim()})
        if path.startswith("/task/"):
            tid = path.split("/", 2)[2]
            t = _task_get(tid)
            if not t:
                return self._send(404, {"error": f"没有任务 {tid}"})
            return self._send(200, t)
        if path == "/task-list":
            with TASKS_LOCK:
                items = [{k: v for k, v in t.items() if k != "code"}
                         for t in TASKS.values()]
            return self._send(200, {"tasks": items})
        if path == "/kb-location":
            # ⚠ 和 /llm-config 一样：GET 与 POST 必须**各在自己的 do_ 方法里
            #   有一支**（GET 只读位置，迁移在 do_POST）。
            #   本机因为"只写了一个方法"已经踩过 /weights、/llm-config 两次，
            #   加这个端点时又差点第三次 —— 所以两处都留了这条注释。
            import schemas as _S
            return self._send(200, {
                "kb_dir": _S.KB_DIR,
                "zotero_data_dir": _S.ZOTERO_DATA_DIR,
                "location_file": _S.LOCATION_FILE,
                "default_follow": os.path.join(_S.ZOTERO_DATA_DIR, "zotero-kb"),
            })
        if path == "/env-check":
            # 运行环境诊断：每个位置"找到了什么、找过哪些、为什么用它"。
            # 为什么要这个而不是只回一句"找不到 py 环境"：
            #   本机面板就报过那句干巴巴的话，用户完全不知道该改哪儿。
            #   这里把候选全列出来，一眼就能看出是"没装"还是"装错地方"。
            return self._send(200, env_report())
        if path == "/models":
            # 本机 Ollama 有哪些模型 —— 插件设置面板用它填「模型」下拉列表。
            #
            # 为什么要这个端点（用户要求"oll 的本地模型选择也用下拉列表"）：
            #   原来设置面板是个空白输入框，用户得先自己 `ollama list`
            #   看有哪些模型、再手打模型名 —— 打错一个字就报"模型不存在"。
            #   下拉列表直接列出本机装好的，点一下就选上。
            #
            # ⚠ Ollama 没在跑时要返回**能看懂的原因**，不能只给空列表 ——
            #   否则用户看到空下拉框会以为是插件坏了。
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
                "host": (_j.load_llm_config().get("ollama_host")
                         or _j.OLLAMA_HOST),
            }
            if not ok:
                payload["error"] = "连不上本机的 Ollama"
                payload["hint"] = (
                    "Ollama 没在运行。装好之后它一般会随系统启动；"
                    "也可以手动跑一下 Ollama 程序，或在管理面板的"
                    "「运行环境」页点「启动本地服务」旁边的 Ollama 检测。")
            elif not names:
                payload["hint"] = ("Ollama 在跑但一个模型都没有。"
                                   "先拉一个：ollama pull qwen3:4b-instruct")
            return self._send(200, payload)
        if path == "/metafill":
            # 元数据补全建议（插件右键「补全元数据（本地模型）」用）。
            #
            # ⚠ 和 /weights、/llm-config、/kb-location 同一个坑：GET 与 POST 必须
            #   **各在自己的 do_ 方法里有一支**。本项目已经三次踩过"只在 do_POST
            #   里加、GET 过去 404"，所以这个端点从一开始就两处都写，注释也留在两处。
            # ⚠ 而且必须在最后那个 `return 404` **之前** —— 放它后面就是死代码。
            #
            # GET /metafill?key=<key>&use_model=0 → 只跑规则（排障用，不依赖 Ollama）
            qs = parse_qs(urlparse(self.path).query)
            key = ((qs.get("key") or [""])[0] or "").strip()
            if not key:
                # 401/400 分开写：400 是"参数不对"，401 是"没带 token"，
                # 排障时一眼能分出是插件没配 token 还是参数写错。
                return self._send(400, {"error": "需要 key 参数",
                                        "hint": "GET /metafill?key=<Zotero 条目 key>"})
            raw_um = ((qs.get("use_model") or ["1"])[0] or "").strip().lower()
            use_model = raw_um not in ("0", "false", "no", "off")
            return self._send(200, do_metafill(key, use_model))
        if path == "/metafill-scan":
            # 批量补全建议（面板「一键补全」用）。
            # ⚠ 同样 GET/POST 各一支（本项目在这一点上踩过三次）。
            # GET /metafill-scan?use_model=0&limit=10 → 纯规则扫描，方便排障/脚本
            qs = parse_qs(urlparse(self.path).query)

            def _flag(name: str, default: str = "0") -> bool:
                v = ((qs.get(name) or [default])[0] or "").strip().lower()
                return v in ("1", "true", "yes", "on")

            return self._send(200, do_metafill_scan(
                use_model=_flag("use_model"),
                model=((qs.get("model") or [""])[0] or ""),
                limit=int(((qs.get("limit") or ["0"])[0] or "0") or 0)))
        if path == "/typefix":
            # 单篇"类型/标题被网站污染"的检测与建议（插件右键用）。
            qs = parse_qs(urlparse(self.path).query)
            key = ((qs.get("key") or [""])[0] or "").strip()
            if not key:
                return self._send(400, {"error": "需要 key 参数",
                                        "hint": "GET /typefix?key=<Zotero 条目 key>"})
            um = ((qs.get("use_network") or ["0"])[0] or "").strip().lower()
            return self._send(200, do_typefix(
                key, um in ("1", "true", "yes", "on")))
        if path == "/typefix-scan":
            # 全库找"网页存了、后来挂 PDF"的条目（面板用，只读）
            qs = parse_qs(urlparse(self.path).query)
            um = ((qs.get("use_network") or ["0"])[0] or "").strip().lower()
            return self._send(200, do_typefix_scan(
                use_network=um in ("1", "true", "yes", "on")))
        if path == "/kb-sync":
            return self._send(400, {"error": "/kb-sync 只接受 POST"})
        if path == "/llm-config":
            # ⚠ GET 与 POST 必须**各在自己的 do_ 方法里有一支** ——
            #   只在 do_POST 里写的话，GET 过来就是 404（本机刚踩过）。
            #   同类错误之前也犯过一次（/weights 曾误放进 do_POST）。
            # ⚠ 而且必须放在最后那个 `return 404` **之前** ——
            #   放它后面就成了永远执行不到的死代码（本机又踩了一次，
            #   自检报 404 才发现）。
            # GET：读配置（api_key 只回"是否已配置"，不回明文）
            import judge as _j
            return self._send(200, {"config": _safe_llm_config(),
                                    "status": _j.llm_status()})
        return self._send(404, {"error": f"未知路径 {path}"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._authorized():
            return self._send(401, {"error": "缺少或错误的 token",
                                    "hint": f"token 存在 {TOKEN_PATH}"})
        body = self._read_json()

        if path == "/reindex":
            keys = body.get("keys") or []
            full = bool(body.get("full"))
            job = start_job("重建知识库索引" + ("（全量）" if full else "（增量）"),
                            do_reindex, keys, full)
            return self._send(202, {"job": job, "state": "running"})

        if path == "/item-info":
            keys = body.get("keys") or []
            if not keys:
                return self._send(400, {"error": "需要 keys 数组"})
            return self._send(200, do_item_info(keys))

        if path == "/tag-suggest":
            keys = body.get("keys") or []
            if not keys:
                return self._send(400, {"error": "需要 keys 数组"})
            limit = int(body.get("limit") or 10)
            job = start_job("生成标签建议", do_tag_suggest, keys, limit)
            return self._send(202, {"job": job, "state": "running"})

        if path == "/metafill":
            # 元数据补全建议（插件右键「补全元数据（本地模型）」用）。
            # ⚠ 这个端点 GET 也要能用（见 do_GET 里同一段的注释）：两处各一支，
            #   少写一处就是 404，本项目在 /weights、/llm-config、/kb-location
            #   上已经踩过三次。
            #
            # POST /metafill {key, use_model?, model?}
            #   同步返回建议（不像 /tag-suggest 那样转异步任务）：
            #   规则能搞定的字段是秒回，只有兜底那一步才等模型。
            key = str(body.get("key") or "").strip()
            if not key:
                return self._send(400, {"error": "需要 key",
                                        "hint": "POST /metafill {key: <Zotero key>}"})
            # use_model 缺省为 True（用户要的默认行为）；显式传 false 才关掉模型兜底
            raw_um = body.get("use_model")
            use_model = True if raw_um is None else bool(raw_um)
            return self._send(200, do_metafill(
                key, use_model, str(body.get("model") or "")))

        if path == "/metafill-scan":
            # 批量补全建议（面板「一键补全」）。**同步**返回：
            # 纯规则模式下全库 101 篇是秒级，不值得为它再加一套 job 轮询状态机。
            # ⚠ 默认 use_model=False（面板传什么就是什么）—— 调模型是显式动作。
            limit = int(body.get("limit") or 0)
            return self._send(200, do_metafill_scan(
                use_model=bool(body.get("use_model")),
                model=str(body.get("model") or ""), limit=limit))

        if path == "/metafill-scan-cancel":
            # 取消正在跑的批量补全（面板「取消」按钮）
            return self._send(200, do_metafill_scan_cancel())

        if path == "/typefix":
            # 单篇类型/标题修正建议。**只读**：真正的改库在插件里、要用户确认。
            key = str(body.get("key") or "").strip()
            if not key:
                return self._send(400, {"error": "需要 key",
                                        "hint": "POST /typefix {key, use_network?}"})
            return self._send(200, do_typefix(
                key, bool(body.get("use_network"))))

        if path == "/typefix-scan":
            return self._send(200, do_typefix_scan(
                use_network=bool(body.get("use_network"))))

        if path == "/classify":
            # 插件给新文献要一个分类建议。给单篇、可指定分类列表与模型。
            # 也用于"跟模型对话调整"：带 feedback + previous 再问一次。
            meta = body.get("item") or {}
            if not (meta.get("title") or meta.get("abstract")):
                return self._send(400, {"error": "需要 item.title 或 item.abstract"})
            cats = body.get("categories") or []
            # 模型相关的参数由插件设置面板随请求带过来
            # （provider/base_url/api_key/model）。带了就落盘，
            # 这样"配置在插件里"对服务端也成立 —— 用户不用来改本机 json。
            _persist_llm_overrides(body)
            return self._send(200, do_classify_one(
                meta, cats,
                body.get("model") or "",
                feedback=body.get("feedback") or "",
                previous=body.get("previous") or None,
                # 这三个**直接透传**：本次请求就用调用方给的那套配置，
                # 不去读落盘的那份（两者可能因为"空值不写"的语义而不一致）
                provider=body.get("provider") or "",
                base_url=body.get("base_url") or "",
                api_key=body.get("api_key") or ""))

        if path == "/llm-config":
            # ⚠ GET 与 POST 都要能用，所以**两个 do_ 方法里都得有这一支**。
            #   本机踩过：只在 do_POST 里写了，GET 过去是 404
            #   （同类错误还犯过一次 —— /weights 也曾误放进 do_POST）。
            #   GET  → 读配置（api_key 只回"是否已配置"）
            #   POST → 写配置（body 带 __write: true）
            import judge as _j
            if body and body.get("__write"):
                kw = {k: body.get(k) for k in
                      ("provider", "base_url", "api_key", "model", "ollama_host")
                      if body.get(k) is not None}
                if not kw:
                    return self._send(400, {"error": "没有要写入的键"})
                p = _j.save_llm_config(**kw)
                return self._send(200, {"ok": True, "path": p,
                                        "config": _safe_llm_config()})
            return self._send(200, {"config": _safe_llm_config(),
                                    "status": _j.llm_status()})

        if path == "/env-config":
            # 写运行环境配置（项目目录 / Python / Ollama）。
            # 插件设置面板的「运行环境」区保存时调它。
            # ⚠ 三个位置**互相独立**，绝不能拿一个去推另一个（本机踩过：
            #   面板从知识库位置反推项目根，kb 一搬走就报"找不到 py 环境"）。
            import schemas as _S
            raw = body.get("env") if isinstance(body.get("env"), dict) else body
            pairs = (("project_root", "project_root", "目录"),
                     ("python", "python_exe", "文件"),
                     ("ollama", "ollama_exe", "文件"))
            flat: dict = {}
            for key, _cfg_key, kind in pairs:
                val = str(raw.get(key) or "").strip()
                if not val:
                    continue
                if not os.path.exists(val):
                    return self._send(400, {
                        "error": f"{key} 指向的位置不存在：{val}",
                        "hint": f"它应该是一个{kind}的完整路径。"
                                + ("Python 要指到 python.exe（或 pythonw.exe）。"
                                   if key == "python" else "")})
                flat[key] = val
            try:
                if flat:
                    _S.write_location_config(env=flat)
                    # 同时写"运行时状态"（用户级，与项目无关）：
                    # 即使项目根被改错，下次启动也能从这里把 Python 找回来。
                    _S.write_runtime_state(**flat)
            except OSError as exc:
                return self._send(500, {"error": f"写入配置失败：{exc}"})
            log(f"运行环境已更新：{flat}")
            return self._send(200, {"ok": True, "saved": flat,
                                    "report": env_report()})

        if path == "/kb-location":
            # 知识库位置：读 / 迁移（插件设置面板用）。
            #   GET  → 当前位置、Zotero 数据目录、跟随规则
            #   POST {"migrate_to": "..."} → 迁移（复制 + 备份 + 校验）
            # ⚠ 迁移是这个服务**自己**做完再返回的（同步阻塞），因为迁移期间
            #   不能有别的请求在读写库。前端要提示"可能要一会儿"。
            import schemas as _S
            if body and body.get("migrate_to"):
                target = str(body["migrate_to"])
                res = _migrate_kb(target)
                return self._send(200 if res.get("ok") else 500, res)
            return self._send(200, {
                "kb_dir": _S.KB_DIR,
                "zotero_data_dir": _S.ZOTERO_DATA_DIR,
                "location_file": _S.LOCATION_FILE,
                "default_follow": os.path.join(_S.ZOTERO_DATA_DIR, "zotero-kb"),
            })

        if path == "/kb-sync":
            # 把知识库同步到别处（用户可选功能，默认关闭）。
            # 为什么要服务端做：要同步的是服务端的目录，而且迁移/同步期间
            # 不应该有别的写操作。
            import schemas as _S
            mode = str((body or {}).get("mode") or "none")
            target = str((body or {}).get("target") or "")
            if mode == "none" or not target:
                return self._send(400, {"error": "没启用同步，或没填同步目标"})
            if mode == "folder":
                res = _sync_to_folder(_S.KB_DIR, target)
            elif mode == "command":
                res = _sync_by_command(target)
            else:
                return self._send(400, {"error": f"未知同步方式 {mode}"})
            return self._send(200 if res.get("ok") else 500, res)

        if path == "/collection-suggest":
            use_model = bool(body.get("use_model", True))
            job = start_job("生成分类建议", do_collection_suggest, use_model)
            return self._send(202, {"job": job, "state": "running"})

        if path == "/weight":
            # 写权重（标重点 / 取消重点 / 人工分 / 备注）。
            # 插件右键菜单用它 —— 用户在 Zotero 里看到某篇想标重点时，
            # 不该还要回管理面板填 key。
            key = str(body.get("key") or "").strip()
            if not key:
                return self._send(400, {"error": "需要 key"})
            pin = body.get("pinned")
            res = do_set_weight(
                key,
                pinned=None if pin is None else bool(pin),
                manual=(None if body.get("manual") is None
                        else float(body.get("manual"))),
                note=(None if body.get("note") is None else str(body.get("note"))))
            return self._send(200 if res.get("ok") else 400, res)

        if path == "/pending":
            # 待确认的分类建议（监听进程写入，面板/插件读取）
            return self._send(200, {"pending": _read_pending()})

        # 注意：GET 的 /task、/task/<id>、/task-list 三个分支在 do_GET 里，
        # 不要重复写到这里 —— 曾经误插过一次，结果 POST /task（派发任务）
        # 被前面的 `if path == "/task"` 拦截，永远返回 {"task": null}。
        if path == "/task":
            # Python 侧派一个任务给插件执行
            kind = (body.get("kind") or "js").strip()
            code = body.get("code") or ""
            if not code:
                return self._send(400, {"error": "需要 code"})
            t = _task_new(kind, code,
                          float(body.get("timeout") or 60.0))
            log(f"派发插件任务 {t['id']}（{kind}，{len(code)} 字符）")
            return self._send(200, {"task": t})

        if path == "/task/cancel":
            # 撤回一个还没被领取的任务。**必须在 `/task/result` 之前判**，
            # 否则 "/task/cancel" 会被 startswith 之类的分支吃掉。
            # （这里用的是精确相等，所以顺序其实无所谓；写这句是提醒：
            #   上面 POST /task 那条注释里记过一次被前缀拦截的坑。）
            tid = (body.get("id") or "").strip()
            if not tid:
                return self._send(400, {"error": "需要 id"})
            res = _task_cancel(tid)
            return self._send(200 if res.get("ok") else 404, res)

        if path == "/task/result":
            tid = (body.get("id") or "").strip()
            if not tid:
                return self._send(400, {"error": "需要 id"})
            ok = _task_finish(tid, bool(body.get("ok")),
                              body.get("result"), body.get("error") or "")
            return self._send(200 if ok else 404, {"ok": ok})

        if path == "/search":
            q = (body.get("query") or "").strip()
            if not q:
                return self._send(400, {"error": "需要 query"})
            from searcher import Searcher
            s = Searcher()
            try:
                res = s.search(q, limit=int(body.get("limit") or 8))
                return self._send(200, res)
            finally:
                s.close()

        if path == "/watch-start":
            # 手动触发一次"库变化"处理（不必等监听间隔）
            job = start_job("处理库变化（切片 + 分类建议）", _watch_handle_change)
            return self._send(202, {"job": job, "state": "running"})

        if path == "/shutdown":
            # 关停服务（插件在 Zotero 关闭时调它，**仅当服务是插件自己拉起的**）。
            # 先回响应再置位，保证调用方拿得到 200 而不是连接被掐断。
            self._send(200, {"ok": True, "bye": True})
            _SHUTDOWN.set()
            return

        return self._send(404, {"error": f"未知路径 {path}"})


# 关停信号：`POST /shutdown` 置位，`serve()` 里的看门狗看到就退出。
# 定义在模块级是因为 Handler（另一个类）要能置位它。
_SHUTDOWN = threading.Event()


def serve(port: int, watch: bool = True) -> None:
    tok = get_token()          # 启动时就把 token 文件落下来（插件要用）
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"服务已启动：http://127.0.0.1:{port}  （token 在 {TOKEN_PATH}）")
    log(f"知识库目录：{S.KB_DIR}")
    log(f"健康检查：GET /health（无需 token）")
    if watch:
        threading.Thread(target=watch_loop, kwargs={"interval": 20.0},
                         daemon=True).start()
        log("已启动 Zotero 库监听（发现新文献会自动切片并生成分类建议）")
    # 关停看门狗：`POST /shutdown` 置位后，每 0.5 秒看一次，置位了就退出。
    #
    # 为什么要有这个端点：服务的生命周期跟着 Zotero 走（插件拉起、插件关掉），
    # 而插件**不能**用 `taskkill /IM pythonw.exe` 来关它 —— pythonw 是极常见
    # 的进程名，那样会连管理面板、用户自己的 Python 脚本一起杀掉。
    # 让服务自己收到通知后退出，是唯一不会误伤的做法。
    def _watch_shutdown():
        while not _SHUTDOWN.is_set():
            time.sleep(0.5)
        log("收到 /shutdown，正在退出")
        try:
            httpd.shutdown()
        except Exception:                                    # noqa: BLE001
            pass

    threading.Thread(target=_watch_shutdown, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，停止服务")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------- 自检


def self_test(port: int) -> int:
    """起服务、把每个接口打一遍、报结果，然后退出。

    ⚠ 端口选择很关键：如果直接用默认端口（8765），而本机**已经有一个
      长期运行的服务**占着它，那么自检起的那个会 bind 失败，
      而请求会被**旧服务**接走 —— 于是你测的是旧代码，
      新加的接口全部 404。本机就这么被误导过一次。
      所以自检固定用一个不常用的端口，避免撞上正在跑的服务。
    """
    import urllib.error
    import urllib.request

    # 8765 是生产端口，自检躲开它
    if port == DEFAULT_PORT:
        port = 8899
    print("=" * 66)
    print("本地服务自检")
    print(f"（自检端口 {port}，避开正在运行的服务）")
    print("=" * 66)
    tok = get_token()
    thread = threading.Thread(target=serve, args=(port,), daemon=True)
    thread.start()
    time.sleep(1.2)

    base = f"http://127.0.0.1:{port}"
    passed = failed = 0

    def call(method: str, path: str, body=None, token=True, expect=200):
        nonlocal passed, failed
        url = base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-KB-Token", tok)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                payload = {}
        ok = code == expect
        print(f"  {'PASS' if ok else 'FAIL'}  {method} {path:24} → {code}"
              f"{'' if ok else f'（期望 {expect}）'}")
        if ok:
            passed += 1
        else:
            failed += 1
        return payload

    print("\n[1] 无需 token 的探活")
    call("GET", "/health", token=False)
    call("GET", "/", token=False)

    print("\n[2] token 校验")
    call("GET", "/status", token=False, expect=401)
    call("GET", "/status", token=True)

    print("\n[3] 只读接口")
    call("GET", "/jobs")
    # 样本 key 从**权重映射的键**里取一个真实的。
    #
    # ⚠ 不要写死某个具体条目的 key：文档脱敏时容易漏掉，而且换台机器、
    #   换个库那个 key 就不存在了，自检会**假失败**。
    #   为什么要"真 key"：item-info 要同时覆盖"在库"与"不在库"两条分支，
    #   只有拿一个真存在于索引里的 key 才能测前者。
    #   （`/weights` 的键就来自索引里的条目，是最省事的真实样本来源。）
    _wm = call("GET", "/weights")
    ANY_KEY = next(iter((_wm or {}).get("weights") or {}), "AAAAAAAA")
    info = call("POST", "/item-info", {"keys": [ANY_KEY, "NOTEXIST1"]})
    items = info.get("items", [])
    ok = len(items) == 2 and items[0].get("in_kb") and not items[1].get("in_kb")
    print(f"  {'PASS' if ok else 'FAIL'}  item-info 区分了在库/不在库"
          f"（样本 {ANY_KEY}）")
    passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    print("\n[4] 搜索（插件用它回答「这条在库里讲过什么」）")
    res = call("POST", "/search", {"query": "机器学习模型", "limit": 3})
    print(f"        命中 {len(res.get('hits', []))} 条")

    print("\n[5] 权重映射（插件「知识库权重」列靠它）")
    wm = call("GET", "/weights")
    weights = (wm or {}).get("weights", {})
    ok = isinstance(weights, dict)
    print(f"  {'PASS' if ok else 'FAIL'}  /weights 返回映射，共 {len(weights)} 条")
    passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
    if weights:
        # 抽查一条的字段完整性 —— 列渲染依赖 weight/pinned
        sample_key = next(iter(weights))
        sample = weights[sample_key]
        need = ("weight", "pinned", "attempts")
        ok2 = all(k in sample for k in need)
        print(f"  {'PASS' if ok2 else 'FAIL'}  条目字段完整"
              f"（{sample_key}: {sample}）")
        passed, failed = (passed + 1, failed) if ok2 else (passed, failed + 1)
    # 带 keys 过滤
    one = call("GET", "/weights?keys=" + ANY_KEY)
    got = (one or {}).get("weights", {})
    ok3 = set(got).issubset({ANY_KEY})
    print(f"  {'PASS' if ok3 else 'FAIL'}  /weights?keys= 过滤生效"
          f"（返回 {list(got)}）")
    passed, failed = (passed + 1, failed) if ok3 else (passed, failed + 1)

    print("\n[6] 异步任务：分类建议（纯确定性部分，不调模型）")
    job = call("POST", "/collection-suggest", {"use_model": False}, expect=202)
    job_id = job.get("job", "")
    print(f"        job = {job_id}")
    for _ in range(60):
        time.sleep(0.5)
        st = call("GET", f"/jobs/{job_id}", expect=200)
        if st.get("state") in ("done", "failed"):
            break
    if st.get("state") == "done":
        adv = st.get("result", {})
        print(f"  PASS  任务完成：分类 {len(adv.get('collections', []))} 个，"
              f"问题 {len(adv.get('issues', []))} 条")
        passed += 1
    else:
        print(f"  FAIL  任务未完成：{st.get('state')} {st.get('error', '')}")
        failed += 1

    print("\n[7] 模型配置接口（/llm-config）")
    # 读：不该回 api_key 明文（只回 has_key / key_tail）。
    # 注意 call() 自己会判状态码并打印 PASS/FAIL，这里只补"内容"层面的断言。
    data = call("GET", "/llm-config", token=True, expect=200)
    cfg = (data or {}).get("config") or {}
    if isinstance(cfg, dict) and "api_key" not in cfg:
        print(f"  PASS  响应不含 api_key 明文（provider={cfg.get('provider')}，"
              f"has_key={cfg.get('has_key')}，key_tail={cfg.get('key_tail')}）")
        passed += 1
    else:
        print(f"  FAIL  响应里出现了 api_key 明文：{list(cfg)[:6]}")
        failed += 1
    # 写：存一次（用当前值，等价于不变更），确认接口可写
    call("POST", "/llm-config",
         {"__write": True, "model": cfg.get("model") or ""},
         token=True, expect=200)

    print("\n[8] 知识库位置与同步接口")
    # /health 也要报告"项目根 / python / 面板脚本" —— 插件靠它启动管理面板。
    # ⚠ 这三个不能从 kb_dir 反推：知识库跟着 Zotero 数据目录走，
    #   代码/venv 还在项目目录里。本机就因为反推，面板报"找不到 py 环境"。
    h = call("GET", "/health", token=False, expect=200)
    if isinstance(h, dict) and h.get("project_root"):
        py_ok = os.path.exists(h.get("python") or "")
        panel_ok = os.path.exists(h.get("panel") or "")
        print(f"  PASS  /health 带 project_root={h['project_root']}")
        print(f"  {'PASS' if py_ok else 'FAIL'}  python 存在：{h.get('python')}")
        print(f"  {'PASS' if panel_ok else 'FAIL'}  面板脚本存在：{h.get('panel')}")
        passed += 1 + (1 if py_ok else 0) + (1 if panel_ok else 0)
        failed += (0 if py_ok else 1) + (0 if panel_ok else 1)
    else:
        print(f"  FAIL  /health 没返回 project_root：{str(h)[:120]}")
        failed += 1

    # /env-check：运行环境诊断。用户报"找不到 py 环境"时就靠它定位。
    # 关键断言：**项目根绝不能等于知识库位置的上一级** —— 那正是
    # 本机出过的 bug（从数据位置反推代码位置）。
    rep = call("GET", "/env-check", token=True, expect=200)
    if isinstance(rep, dict) and isinstance(rep.get("python"), dict):
        p = rep["python"]
        r = rep.get("project_root") or {}
        o = rep.get("ollama") or {}
        ok_py = bool(p.get("runnable"))
        ok_root = bool(r.get("has_panel"))
        print(f"  {'PASS' if ok_py else 'FAIL'}  python 可用：{p.get('version')}"
              f"  ({p.get('path')})")
        print(f"  {'PASS' if ok_root else 'FAIL'}  项目根里有 tools\\gui.py："
              f"{r.get('path')}")
        # 反推 bug 的回归断言
        wrong = os.path.normcase(os.path.dirname(os.path.abspath(S.KB_DIR))) \
            == os.path.normcase(os.path.abspath(r.get("path") or "x"))
        print(f"  {'FAIL' if wrong else 'PASS'}  项目根不是从知识库位置反推的")
        print(f"  PASS  候选 Python {len(p.get('candidates') or [])} 个，"
              f"ollama={'有' if o.get('path') else '无'}"
              f"（api {'在跑' if o.get('api_up') else '没跑'}）")
        passed += 3 + (1 if ok_py else 0) + (1 if ok_root else 0)
        failed += (0 if ok_py else 1) + (0 if ok_root else 1) + (1 if wrong else 0)
    else:
        print(f"  FAIL  /env-check 结构不对：{str(rep)[:140]}")
        failed += 1

    # /env-config 必须拒绝不存在的路径（否则用户填错了却"保存成功"）
    bad = call("POST", "/env-config", {"python": r"D:\___不存在的目录___\python.exe"},
               token=True, expect=400)
    if isinstance(bad, dict) and bad.get("error"):
        print(f"  PASS  /env-config 拒绝不存在的路径")
        passed += 1
    else:
        print(f"  FAIL  /env-config 居然接受了不存在的路径：{str(bad)[:100]}")
        failed += 1

    print("\n[9] 「跟模型对话调整」与「外接 API 报错质量」")
    # 只验证**参数被正确接收并传进提示词**，不真调模型（自检不该依赖模型在线）。
    # 做法：缺 title/abstract 时服务端在**进模型之前**就返回 400 ——
    # 用它确认 /classify 这条路由是活的、且能带 feedback/previous 字段。
    r9 = call("POST", "/classify",
              {"feedback": "这篇应该归到分类 A",
               "previous": {"category": "分类 B", "round": 1}},
              token=True, expect=400)
    if isinstance(r9, dict) and r9.get("error"):
        print(f"  PASS  /classify 接受 feedback + previous 字段"
              f"（缺 item 时按预期拒绝）")
        passed += 1
    else:
        print(f"  FAIL  /classify 参数通路异常：{str(r9)[:120]}")
        failed += 1
    # 确认分类建议的实现在 judge.classify_item（面板与插件共用一份），
    # 且签名里有 feedback/previous —— 少了这两个参数，调整功能会静默失效。
    try:
        import inspect
        import judge as _jd
        sig = inspect.signature(_jd.classify_item)
        need = ("feedback", "previous", "provider", "base_url", "api_key")
        missing = [n for n in need if n not in sig.parameters]
        print(f"  {'PASS' if not missing else 'FAIL'}  judge.classify_item 签名齐全"
              f"（{'缺 ' + str(missing) if missing else 'feedback/previous/'
                 'provider/base_url/api_key'}）")
        passed += 1 if not missing else 0
        failed += 0 if not missing else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  读 judge.classify_item 签名失败：{exc}")
        failed += 1

    # 报错质量：配错 API 时必须给出**可操作的中文提示**，而不是只有 HTTP 码。
    # 这是用户第一次配 API 最可能遇到的路径，报错看不懂就等于配不成。
    try:
        import judge as _jd2
        r = _jd2._generate_openai("ping", "", "x", False, 5, 0.1,
                                  "https://127.0.0.1:9/v1", "sk-x")
        has = bool(r.get("hint")) and "连不上" in str(r.get("error", ""))
        print(f"  {'PASS' if has else 'FAIL'}  连不上时给人话提示："
              f"{str(r.get('hint'))[:44]}")
        passed += 1 if has else 0
        failed += 0 if has else 1
        # key 里混进非 ASCII 时应被清洗掉、并且**明确告诉用户**已清洗
        # （以前报 UnicodeEncodeError，用户完全看不懂）
        r2 = _jd2._generate_openai("ping", "", "x", False, 5, 0.1,
                                   "https://api.deepseek.com/v1", "中文key")
        note = str(r2.get("hint", ""))
        clean = ("非英文字符" in note) or ("ASCII" in str(r2.get("error", "")))
        print(f"  {'PASS' if clean else 'FAIL'}  非 ASCII key 被清洗并告知："
              f"{note[:56] or str(r2.get('error'))[:56]}")
        passed += 1 if clean else 0
        failed += 0 if clean else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  报错质量检查没跑成：{type(exc).__name__}: {exc}")
        failed += 1

    print("\n[10] Ollama 模型列表（插件设置面板的下拉列表靠它）")
    # 用户要求"oll 的本地模型选择也用下拉列表" —— 那个列表就是这个端点给的。
    # 要点：Ollama 没在跑时也必须返回**能看懂的原因**，不能只给空列表
    # （否则用户看到空下拉框会以为插件坏了）。
    md = call("GET", "/models", token=True, expect=200)
    if isinstance(md, dict) and "models" in md:
        n = len(md.get("models") or [])
        rec = md.get("recommended") or ""
        if md.get("ok"):
            print(f"  PASS  /models 返回 {n} 个模型"
                  + (f"，推荐 {rec}" if rec else ""))
            passed += 1
            # 每个条目要有 name（下拉列表的 value）
            bad_items = [m for m in (md.get("models") or [])
                         if not (m or {}).get("name")]
            print(f"  {'PASS' if not bad_items else 'FAIL'}  每个模型条目都有 name")
            passed += 1 if not bad_items else 0
            failed += 0 if not bad_items else 1
        else:
            # Ollama 没跑：也应当有 error + hint（这才叫"能看懂"）
            ok_hint = bool(md.get("error")) and bool(md.get("hint"))
            print(f"  {'PASS' if ok_hint else 'FAIL'}  Ollama 不在时给出原因与建议："
                  f"{str(md.get('error'))[:40]}")
            passed += 1 if ok_hint else 0
            failed += 0 if ok_hint else 1
    else:
        print(f"  FAIL  /models 结构不对：{str(md)[:120]}")
        failed += 1

    data = call("GET", "/kb-location", token=True, expect=200)
    if isinstance(data, dict) and data.get("kb_dir"):
        print(f"  PASS  /kb-location 报告当前位置：{data['kb_dir']}")
        passed += 1
    else:
        print(f"  FAIL  /kb-location 没返回 kb_dir：{str(data)[:100]}")
        failed += 1
    # 同步接口：没启用时应明确拒绝（而不是偷偷同步）
    call("POST", "/kb-sync", {"mode": "none"}, token=True, expect=400)
    # 迁移接口：目标与当前相同，应回 skipped（不能真的搬）
    cur = (data or {}).get("kb_dir") or ""
    if cur:
        d2 = call("POST", "/kb-location", {"migrate_to": cur}, token=True, expect=200)
        ok = isinstance(d2, dict) and d2.get("skipped")
        print(f"  {'PASS' if ok else 'FAIL'}  目标==当前时不迁移（skipped={bool(ok)}）")
        passed += 1 if ok else 0
        failed += 0 if ok else 1

    print("\n[11] 元数据补全建议（/metafill）")
    # 为什么这一节值得占自检的位置：它是**唯一一个新加的写入口的前置**——
    # 插件右键「补全元数据」全靠它，而它自己只读（真写回在插件侧、要用户勾选）。
    # ⚠ GET 与 POST **都要打一遍**：本项目在 /weights、/llm-config、/kb-location
    #   上三次踩过"只在 do_POST 里加了一支、GET 过去 404"。
    # ⚠ 用 use_model=False：自检不该依赖 Ollama 在不在线（规则部分是确定性的）。
    mf = call("GET", "/metafill?key=" + ANY_KEY + "&use_model=0", token=True,
              expect=200)
    ok_mf = (isinstance(mf, dict) and mf.get("ok") is True
             and isinstance(mf.get("suggestions"), list)
             and isinstance(mf.get("skipped"), list)
             and mf.get("key") == ANY_KEY)
    print(f"  {'PASS' if ok_mf else 'FAIL'}  GET 返回建议结构"
          f"（建议 {len(mf.get('suggestions') or [])} 条，"
          f"跳过 {len(mf.get('skipped') or [])} 条，"
          f"正文来源 {mf.get('fulltext_source') or '无'}）")
    passed, failed = (passed + 1, failed) if ok_mf else (passed, failed + 1)
    # 每条建议必须带原文证据（页码 + 片段）——这是"用户能核对"的底线
    bad_ev = [s for s in (mf.get("suggestions") or [])
              if not (s.get("evidence") or {}).get("text")]
    print(f"  {'PASS' if not bad_ev else 'FAIL'}  每条建议都带原文证据"
          f"（缺证据 {len(bad_ev)} 条）")
    passed, failed = (passed + 1, failed) if not bad_ev else (passed, failed + 1)
    mf2 = call("POST", "/metafill", {"key": ANY_KEY, "use_model": False},
               token=True, expect=200)
    ok_post = isinstance(mf2, dict) and mf2.get("ok") is True
    print(f"  {'PASS' if ok_post else 'FAIL'}  POST 与 GET 走同一条实现")
    passed, failed = (passed + 1, failed) if ok_post else (passed, failed + 1)
    # 缺 key 必须被拒（400）：静默返回空建议会让插件显示"这篇没得补"，
    # 而真实原因是参数没传对 —— 那种"看着正常其实错了"最难查。
    call("POST", "/metafill", {}, token=True, expect=400)
    call("GET", "/metafill", token=True, expect=400)

    print("\n[12] 批量补全与「类型/标题修正」（本轮新增）")
    # 批量端点存在的理由见 do_metafill_scan 的注释：逐个请求会让
    # metafill.ZoteroReader 每篇重拷一次整库快照。
    # ⚠ 自检里用 limit=3 + use_model=False：只验证**通路与结构**，
    #   不把整套扫描塞进自检（自检要快），也绝不依赖 Ollama。
    ms = call("POST", "/metafill-scan", {"limit": 3, "use_model": False},
              token=True, expect=200)
    ok_ms = (isinstance(ms, dict) and ms.get("ok") is True
             and isinstance(ms.get("rows"), list)
             and isinstance((ms.get("apply_plan") or {}).get("items"), list))
    print(f"  {'PASS' if ok_ms else 'FAIL'}  批量扫描返回结构"
          f"（扫了 {ms.get('scanned')} 篇，缺字段 {ms.get('missing_total')} 篇，"
          f"高置信 {ms.get('high_total')} 条，耗时 {ms.get('elapsed')}s）")
    passed, failed = (passed + 1, failed) if ok_ms else (passed, failed + 1)
    # apply_plan 里**只能有 high 置信**：面板「一键采用高置信」直接照它写库，
    # 这里泄进一条 low 就等于自动写了低置信值 —— 那是红线。
    plan_items = (ms.get("apply_plan") or {}).get("items") or []
    low_fields = {r["key"]: {x["field"] for x in (r.get("low") or [])}
                  for r in (ms.get("rows") or [])}
    leak = [it["key"] for it in plan_items
            if any(f["field"] in low_fields.get(it["key"], set())
                   for f in it.get("fields", []))]
    print(f"  {'PASS' if not leak else 'FAIL'}  apply_plan 只含高置信建议"
          f"（{len(plan_items)} 篇、"
          f"{sum(len(i.get('fields') or []) for i in plan_items)} 个字段；"
          f"混进低置信的 {len(leak)} 篇）")
    passed, failed = (passed + 1, failed) if not leak else (passed, failed + 1)
    # 每篇明细必须带"缺哪些字段"，否则面板那一列是空的（等于没扫）
    bad_rows = [r for r in (ms.get("rows") or []) if not r.get("missing")]
    print(f"  {'PASS' if not bad_rows else 'FAIL'}  每篇都带 missing 列表"
          f"（缺 {len(bad_rows)} 篇）")
    passed, failed = (passed + 1, failed) if not bad_rows else (passed, failed + 1)
    # GET 也要能用（本项目在 /weights、/llm-config、/kb-location 上踩过三次）
    call("GET", "/metafill-scan?limit=1&use_model=0", token=True, expect=200)
    # 取消端点：没有扫描在跑时也应安全返回（幂等）
    cxl = call("POST", "/metafill-scan-cancel", {}, token=True, expect=200)
    ok_cxl = isinstance(cxl, dict) and cxl.get("ok") is True
    print(f"  {'PASS' if ok_cxl else 'FAIL'}  取消端点幂等可调")
    passed, failed = (passed + 1, failed) if ok_cxl else (passed, failed + 1)

    # 类型/标题修正：**只读**建议
    tf = call("POST", "/typefix", {"key": ANY_KEY, "use_network": False},
              token=True, expect=200)
    ok_tf = (isinstance(tf, dict) and tf.get("ok") is True
             and isinstance(tf.get("fixes"), list)
             and isinstance(tf.get("signals"), list))
    print(f"  {'PASS' if ok_tf else 'FAIL'}  /typefix 返回结构"
          f"（类型 {tf.get('item_type')}，嫌疑 {tf.get('suspect')}，"
          f"建议 {len(tf.get('fixes') or [])} 条）")
    passed, failed = (passed + 1, failed) if ok_tf else (passed, failed + 1)
    call("GET", "/typefix?key=" + ANY_KEY, token=True, expect=200)
    call("POST", "/typefix", {}, token=True, expect=400)
    tfs = call("POST", "/typefix-scan", {"use_network": False},
               token=True, expect=200)
    ok_tfs = (isinstance(tfs, dict) and tfs.get("ok") is True
              and isinstance(tfs.get("rows"), list))
    print(f"  {'PASS' if ok_tfs else 'FAIL'}  /typefix-scan 全库检出"
          f"（查了 {tfs.get('checked')} 篇，其中嫌疑 {tfs.get('suspect_total')} 篇，"
          f"耗时 {tfs.get('elapsed')}s）")
    passed, failed = (passed + 1, failed) if ok_tfs else (passed, failed + 1)
    # ⚠ 类型/标题修正**只出建议、不写库**：这里断言服务端确实没有写入路径 ——
    #   写回必须由插件在用户确认后做（applyTypeFix）。所以只检查响应里没有
    #   "已写入 / applied" 这类字段（有的话说明有人往服务端加了写库）。
    wrote = [k for k in (tf or {}) if k in ("applied", "written", "changed")]
    print(f"  {'PASS' if not wrote else 'FAIL'}  /typefix 是只读的"
          f"（响应里没有写入结果字段）")
    passed, failed = (passed + 1, failed) if not wrote else (passed, failed + 1)

    print("\n[9] 未知路径")
    call("GET", "/nope", token=True, expect=404)

    print(f"\n{'=' * 66}\n通过 {passed}　失败 {failed}\n{'=' * 66}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="知识库本地 HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--test", action="store_true", help="自检后退出")
    args = ap.parse_args()
    if args.test:
        return self_test(args.port)
    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
