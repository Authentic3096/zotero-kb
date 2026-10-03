"""把 DSH 搜出来的文献**交给 Zotero 自己去抓**。

    python online/acquire.py --doi 10.xxxx/yyyy --doi 10.zzzz/wwww
    python online/acquire.py --dry-run --doi 10.xxxx/yyyy     # 只解析，不落库
    python online/acquire.py --list-file dois.txt

## 为什么是"派任务给插件"而不是在这里下载

这个模块**不下载任何东西**。它只做三件事：归一化 DOI、把清单派给 Zotero
插件、把结果讲成人话。抓取本身在 Zotero 进程里完成，理由（都实测过）：

1. **网络身份**：请求从 Zotero 进程发出，用的是用户机器**当下**的网络环境 ——
   校园网出口 IP、机构订阅、出版社会话全都天然生效。在 Python 侧下载就把
   这层身份丢了，所以 `skills/zotero-acquire` 的脚本下载只当**兜底**。
2. **`Zotero.Attachments.addAvailableFile` 是 Zotero 自带的"查找可用的 PDF"**
   （实测是 function），自带一整套 resolver：DOI 落地页 → 条目 url →
   开放获取源（Unpaywall）→ PMC → 用户自定义 resolver。
3. **元数据走 Zotero 的翻译器生态**（实测走 "DOI Content Negotiation"），
   比解析搜索结果网页可靠。

## 边界（与 skill 里那份保持一致，不要放宽）

这里用的是**用户已有的访问权限**，不是"绕过付费墙"。
改 URL 骗计费、影子图书馆、盗用凭证 —— 一律不做。抓不到就如实回报，
让用户自己在浏览器里用 Zotero 连接器抓。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

BASE = "http://127.0.0.1:8765"
TOKEN_PATH = os.path.join(S.KB_DIR, "service-token.txt")
UA_STR = "ZoteroAcquire/1.2 (DSH skill; plugin probe)"

# 一篇大概要多久：DOI 解析 ~2s + 找 PDF 视来源 3~20s。
# 给足余量，宁可比实际慢也不要在抓到一半时超时（超时了插件那边还在跑，
# 用户会看到"失败了但条目其实进库了"这种最难查的状态）。
PER_ITEM_SECONDS = 45.0
MIN_TIMEOUT = 120.0
MAX_TIMEOUT = 1800.0

# 一次最多几篇（与插件里的 ACQUIRE_MAX 对齐，两处都拦一道）
MAX_ITEMS = 20


# ---------------------------------------------------------------- DOI 归一化

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)


def normalize_doi(raw: str) -> str:
    """把用户/模型给的 DOI 写成插件认的形状。

    模型很爱给整条 URL（`https://doi.org/10.xxxx/yyyy`）或带尾标点，
    所以这里一起收掉。插件侧还会用 `Zotero.Utilities.cleanDOI` 再洗一遍 ——
    两处都做不是重复，是因为插件那边才是最终权威，这边只求"别把明显能救的丢掉"。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\s*(https?://(dx\.)?doi\.org/|doi:\s*)", "", s, flags=re.I)
    m = _DOI_RE.search(s)
    if not m:
        return ""
    return m.group(0).rstrip(".,;)]}'\"")


def normalize_items(items) -> tuple[list[dict], list[str]]:
    """输入可以是字符串列表，也可以是 {"doi":..,"title":..} 列表。

    返回 (可用的, 被丢掉的原文) —— 被丢掉的要能报出来，
    不能静默吞掉（用户会以为"我都给了怎么少了两篇"）。
    """
    good: list[dict] = []
    bad: list[str] = []
    seen: set[str] = set()
    for it in items or []:
        raw = it if isinstance(it, str) else (it or {}).get("doi", "")
        title = "" if isinstance(it, str) else str((it or {}).get("title") or "")
        doi = normalize_doi(raw)
        if not doi:
            bad.append(str(raw))
            continue
        low = doi.lower()
        if low in seen:          # 同一批里重复给的不重复派
            continue
        seen.add(low)
        good.append({"doi": doi, "title": title})
    return good, bad


# ---------------------------------------------------------------- 与本地服务通信


def _token() -> str:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _api(method: str, path: str, body=None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    tok = _token()
    if tok:
        req.add_header("X-KB-Token", tok)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def service_alive() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


PLUGIN_STATUS_PATH = os.path.join(S.KB_DIR, "plugin-status.json")

# 插件每 30 秒写一次状态文件（每 15 个 tick）。判断"插件在不在"时，
# 允许多旧：给 300 秒，是因为刚启动 Zotero 时文件可能还没写第一遍 ——
# 宁可放过去让它走超时路径（那条路有更具体的提示），也不要误判成"没开"。
PLUGIN_STATUS_MAX_AGE = 300.0

# 从哪个版本起插件才认得 `kind: "acquire"` / `kind: "attach"`。
#
# 为什么要卡这一道：插件是**侧载**的，改完 bootstrap.js 不重启 Zotero 就还是
# 旧代码。那时派 acquire 任务会掉进"执行任意 JS"的分支，把一个 JSON 字符串
# 当 JS 编译 → 报一个 `SyntaxError: unexpected token`。那个报错对用户毫无意义，
# 也看不出"你只是忘了重启 Zotero"。所以这里先比版本，给出能照做的提示。
MIN_PLUGIN_VERSION = (0, 24, 0)


def _parse_version(v: str) -> tuple:
    parts = []
    for x in re.split(r"[.\-+]", str(v or "")):
        m = re.match(r"^(\d+)", x)
        parts.append(int(m.group(1)) if m else 0)
    return tuple((parts + [0, 0, 0])[:3])


def _zotero_alive() -> bool:
    """Zotero 进程还在不在？打它的**连接器端口 23119**。

    为什么需要这个额外的探针：插件状态文件是**每 30 秒才写一次**的，所以
    "Zotero 刚关掉"这段时间里文件看起来还是新的（实测踩过：Zotero 关了 4 分钟，
    前置检查仍放行，任务在队列里干等 30 秒才报"插件没在轮询" —— 而真正的原因
    只是**Zotero 没开**）。23119 是 Zotero 自己起的连接器服务，**开没开一试便知**。

    跨平台、无需 token、只读。任何 HTTP 响应（哪怕 404）都说明进程活着；
    连不上才算没开。
    """
    try:
        req = urllib.request.Request("http://127.0.0.1:23119/connector/ping",
                                     headers={"User-Agent": UA_STR})
        with urllib.request.urlopen(req, timeout=4):
            return True
    except urllib.error.HTTPError:
        return True          # 有响应 = 活着（404 也算）
    except Exception:        # noqa: BLE001
        return False


def _plugin_online() -> tuple[bool, str]:
    """插件在不在轮询？

    为什么不直接派个探针任务去问：那会往任务队列里插一条，和随后的
    acquire 抢顺序；而且每次抓取前多等 1.5~3 秒不值当。
    状态文件是插件自己主动写的（`tickCount` 每 2 秒 +1），读它零成本。

    Returns:
        (是否在线, 说明)。说明是给排错用的，在线时为空。
    """
    try:
        age = time.time() - os.path.getmtime(PLUGIN_STATUS_PATH)
    except OSError:
        return False, f"插件从没写过状态文件（{PLUGIN_STATUS_PATH} 不存在）"
    if age > PLUGIN_STATUS_MAX_AGE:
        return False, f"插件的状态文件已经 {age / 60:.0f} 分钟没更新了"
    try:
        with open(PLUGIN_STATUS_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception as exc:  # noqa: BLE001
        return False, f"状态文件读不出来：{exc}"
    if not st.get("alive"):
        return False, "插件自己报 alive=false（启动没跑完，或已被卸载）"
    if not st.get("taskPolling"):
        return False, "插件的任务轮询是关的（prefs: zotero-kb.autoTaskPoll）"
    ver = str(st.get("pluginVersion") or "0")
    if _parse_version(ver) < MIN_PLUGIN_VERSION:
        want = ".".join(str(x) for x in MIN_PLUGIN_VERSION)
        return False, (f"插件是 {ver}，从 DSH 导入文献要 {want} 以上 —— "
                       "代码已经装好了，但 Zotero 里跑的还是旧版本")
    return True, ""


class AcquireError(RuntimeError):
    """带"怎么办"的错误 —— 给模型看的东西必须能照着做。"""

    def __init__(self, message: str, how_to_fix: list[str] | None = None):
        super().__init__(message)
        self.how_to_fix = how_to_fix or []


# ---------------------------------------------------------------- 异步作业
#
# 为什么要有它：MCP 工具是"一次调用阻塞到返回"的，而抓 N 篇要几十秒 ——
# 那段时间 **DSH 对话里一片空白**，用户分不清是在跑还是卡死了。
# 拆成"派发（立刻返回）+ 轮询（边走边报）"之后，DSH 才能把进度说出来。
#
# 作业状态就放在**本进程内存**里：MCP 进程本来就是长驻的，而且一次抓取的
# 生命周期只有几十秒，没必要落盘。进程重启 = 作业丢失，这是可接受的
# （那种情况下抓取本身也已经断了）。

_JOBS: dict[str, dict] = {}
_JOB_ORDER: list[str] = []
_JOBS_LOCK = threading.Lock()
_JOB_KEEP = 20


def _job_new(total: int, plan: list[dict], dry_run: bool) -> str:
    with _JOBS_LOCK:
        seq = len(_JOB_ORDER) + 1
        jid = f"aq{int(time.time()) % 100000}-{seq}"
        _JOBS[jid] = {
            "job_id": jid,
            "state": "pending",       # pending / running / done / failed
            "total": total,
            "index": 0,               # 已完成几篇
            "current_doi": "",
            "current_title": "",
            "stage": "",
            "dry_run": bool(dry_run),
            "results": [],
            "duplicates": [],
            "failed": [],
            "collection": None,
            "error": "",
            "started": time.time(),
            "updated": time.time(),
            "plan": plan,
        }
        _JOB_ORDER.append(jid)
        while len(_JOB_ORDER) > _JOB_KEEP:
            _JOBS.pop(_JOB_ORDER.pop(0), None)
        return jid


def _job_update(jid: str, **fields) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if not j:
            return
        j.update(fields)
        j["updated"] = time.time()
        # 一律放副本出去，免得调用方拿到正在被线程改的同一个 dict
        #
        # ⚠ 这里**不**做 `list(...)` 深拷贝：results 里的元素是插件回报的
        #   普通 dict，我们只追加、不原地改，所以浅拷贝足够。


def job_progress(jid: str = "") -> dict:
    """读一个作业的进度快照。`jid` 为空则取最近一个。

    Returns:
        dict；没有这个作业时返回 `{}`（调用方据此说"没这个作业"）。
    """
    with _JOBS_LOCK:
        if not jid:
            jid = _JOB_ORDER[-1] if _JOB_ORDER else ""
        j = _JOBS.get(jid)
        if not j:
            return {}
        snap = dict(j)
        snap["results"] = list(j.get("results") or [])
        snap["duplicates"] = list(j.get("duplicates") or [])
        snap["failed"] = list(j.get("failed") or [])
        snap["plan"] = list(j.get("plan") or [])
    snap["age_s"] = round(time.time() - snap.get("started", time.time()), 1)
    snap["summary"] = _job_summary(snap)
    return snap


def _job_summary(j: dict) -> str:
    """一句话进度，DSH 直接念给用户听。"""
    total = j.get("total") or 0
    idx = j.get("index") or 0
    st = j.get("state")
    if st == "pending":
        return f"已派发，等 Zotero 领取（0/{total}）"
    if st == "running":
        cur = j.get("current_doi") or ""
        tail = f"，正在处理 {cur}" if cur else ""
        return f"进行中 {idx}/{total}{tail}"
    if st == "done":
        added = sum(1 for r in (j.get("results") or [])
                    if r.get("status") == "added")
        bits = [f"完成：{added}/{total} 篇入库"]
        wp = sum(1 for r in (j.get("results") or [])
                 if (r.get("pdf") or {}).get("ok"))
        if wp:
            bits.append(f"{wp} 篇带 PDF")
        # ⚠ 失败和跳过**必须一起说出来**。只报"完成：0/1 篇入库"听着像成功了，
        #   而实际上那一篇可能压根没进去 —— 这句话是用户第一眼看到的东西。
        n_bad = len(j.get("failed") or [])
        n_dup = len(j.get("duplicates") or [])
        if n_bad:
            bits.append(f"**{n_bad} 篇没成功**")
        if n_dup:
            bits.append(f"{n_dup} 篇库里已有（跳过）")
        return "，".join(bits)
    return f"失败：{j.get('error') or '未知原因'}"


def _preflight(items) -> tuple[list[dict], list[str]]:
    """归一化 + 各项前置检查。返回 (可用条目, 被丢掉的原文)。

    抽出来是因为阻塞版和异步版要走**完全相同**的检查 —— 两处各写一遍
    迟早会漂移（一边挡住了超量、另一边没挡）。
    """
    good, dropped = normalize_items(items)
    if not good:
        raise AcquireError(
            "没有解析出任何合法的 DOI。",
            ["DOI 的形状是 10.xxxx/yyyy；也可以整条 https://doi.org/10.xxxx/yyyy 丢进来。",
             "如果这批文献压根没有 DOI（很多中文文献就是），"
             "本工具帮不上 —— 请在浏览器里用 Zotero 连接器抓。"])
    if len(good) > MAX_ITEMS:
        raise AcquireError(
            f"一次最多 {MAX_ITEMS} 篇，收到 {len(good)} 篇。",
            [f"请分成 {((len(good) - 1) // MAX_ITEMS) + 1} 批，"
             "或先请用户缩小范围。"])

    if not service_alive():
        raise AcquireError(
            "本机知识库服务没在跑（127.0.0.1:8765），任务派不出去。",
            ["双击 scripts\\4-service.vbs，或在管理面板里点「启动服务」。",
             "服务通常随 Zotero 自动启停；如果 Zotero 也没开，先开 Zotero。"])
    # ⚠ 先问"Zotero 开没开"，再问"插件轮询活没活" —— 这两个的**修法完全不同**，
    #   而插件状态文件有 30 秒的滞后，单看它会得出错误的结论（真机踩过）。
    if not _zotero_alive():
        raise AcquireError(
            "Zotero 没在运行（连接器端口 23119 没人应答）—— "
            "抓取必须在 Zotero 进程里做，它没开就没法抓。",
            ["先打开 Zotero，再让我重试。",
             "抓取用的是 Zotero 自己的翻译器与附件 resolver，"
             "所以整个流程都依赖它开着。"])
    alive, why = _plugin_online()
    if not alive:
        fix = ["确认 Zotero 已启动，且 zotero-kb 插件已启用（工具 → 插件）。"]
        if "要 0.24.0 以上" in why:
            fix = ["**完全退出 Zotero 再重新打开**（不是关窗口 —— 要真的退出进程）。",
                   "插件是 xpi 装的：改完 bootstrap.js 必须重打 xpi 再装，"
                   "光重启 Zotero 不会加载新代码。",
                   "装好后在 Zotero 里跑 工具 → 开发者 → 运行 JavaScript："
                   "`return Zotero.ZoteroKB.version` —— 应该显示 0.24.0 以上。"]
        else:
            fix.append("刚装完/更新过插件必须**完全重启 Zotero**。")
        fix.append(f"状态文件：{PLUGIN_STATUS_PATH}")
        raise AcquireError(
            f"Zotero 插件没法执行抓取任务（{why}）—— "
            "抓取必须在 Zotero 进程里做，它没准备好就没法抓。", fix)
    return good, dropped


def _dispatch_batch(good: list[dict], collection_id: int, find_pdf: bool,
                    dry_run: bool, timeout: float, progress, dropped: list[str],
                    on_task=None) -> dict:
    """把**一批**条目派给插件并等它做完。`good` 通常就是全部，也可能是 1 条。

    （异步作业按"每篇一个任务"派，为的是拿到逐篇的进度；见 acquire_async。）
    """
    per = PER_ITEM_SECONDS
    budget = timeout or max(MIN_TIMEOUT, min(MAX_TIMEOUT, per * len(good) + 60))
    payload = {
        "items": good,
        "collectionID": int(collection_id or 0),
        "findPdf": bool(find_pdf),
        "dryRun": bool(dry_run),
    }

    if progress:
        progress(f"派发 {len(good)} 篇给 Zotero 抓取（预算 {budget:.0f}s）…")

    try:
        resp = _api("POST", "/task",
                    {"kind": "acquire",
                     "code": json.dumps(payload, ensure_ascii=False),
                     "timeout": budget},
                    timeout=30)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise AcquireError(f"派任务失败：HTTP {exc.code} {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise AcquireError(f"派任务失败：{exc}") from exc

    task = (resp or {}).get("task") or {}
    tid = task.get("id")
    if not tid:
        raise AcquireError(f"服务没返回任务 id：{resp}")
    if on_task:
        on_task(tid)

    started = time.time()
    last = ""
    while time.time() - started < budget:
        time.sleep(1.5)
        try:
            t = _api("GET", f"/task/{tid}", timeout=15)
        except Exception:  # noqa: BLE001
            continue
        st = t.get("state") or ""
        if st != last:
            last = st
            if progress:
                progress(f"  [{time.time() - started:.0f}s] {st}")
        if st in ("done", "failed"):
            return _shape_result(t, good, dropped, dry_run, time.time() - started)
        if st == "pending" and time.time() - started > 30:
            _cancel_task(tid)
            # 中途 Zotero 被关掉是最常见的原因 —— 报出来，别让用户去猜
            gone = not _zotero_alive()
            raise AcquireError(
                ("Zotero 在中途被关掉了" if gone
                 else "任务派出去 30 秒了还没被领取 —— Zotero 插件大概没在轮询")
                + "。（已经顺手把这条任务从队列里撤掉了，不会事后偷偷执行。）",
                (["重新打开 Zotero，再让我抓一次。"] if gone else
                 ["确认 Zotero 开着、插件启用、且没把 `autoTaskPoll` 关掉。",
                  "在 Zotero 里跑一次 工具 → 开发者 → 运行 JavaScript："
                  "`return Zotero.ZoteroKB.tickCount` —— 数字在涨就说明轮询活着。"]))

    _cancel_task(tid)
    raise AcquireError(
        f"等插件完成超时（{budget:.0f}s）。"
        "⚠ 如果它还没被领取，我已经把队列里那条撤掉了；"
        "**但如果它已经在跑，撤不掉** —— 过几秒去 Zotero 里看看条目是不是"
        "已经进去了，不要直接重试，否则可能重复入库。")


def _cancel_task(tid: str) -> dict:
    """把队列里那条还没被领取的任务撤掉。

    为什么必须撤（真机踩到的）：派发方等超时后只是"自己不等了"，那条任务
    仍然 pending 躺在本地服务的队列里 —— 用户几小时后打开 Zotero，插件把
    它领走并**真的建了条目**。派发方早就报过失败，用户却莫名多出一篇文献。
    实测复现：22:36 派出、22:39 Zotero 一开就被执行。

    撤不掉也不隐瞒：在跑的确实停不下来，返回里会说清。
    """
    try:
        r = _api("POST", "/task/cancel", {"id": tid}, timeout=10)
        return r if isinstance(r, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def acquire(items, collection_id: int = 0, find_pdf: bool = True,
            dry_run: bool = False, timeout: float = 0.0,
            progress=None) -> dict:
    """把 DOI 清单交给 Zotero 插件抓取，**等它做完**再返回。

    条目少（1~3 篇）时用这个最省事。想边跑边看进度就用 `acquire_async`。

    Args:
        items: DOI 字符串列表，或 {"doi","title"} 字典列表。
        collection_id: 落到哪个分类（Zotero 的 collectionID）；0 = 用用户在
            Zotero 里**当前选中**的分类（与浏览器连接器的行为一致）。
        find_pdf: 是否顺带找可用的 PDF。
        dry_run: 只解析元数据、**不落库**。用于"先看看抓不抓得到"。
        timeout: 秒；0 = 按篇数自动算。
        progress: 可选回调 `fn(text)`，给 CLI 用。

    Returns:
        dict，含 ok / added / with_pdf / results / duplicates / failed
        以及一句给人看的 `summary`。
    """
    good, dropped = _preflight(items)
    return _dispatch_batch(good, collection_id, find_pdf, dry_run,
                           timeout, progress, dropped)


def acquire_async(items, collection_id: int = 0, find_pdf: bool = True,
                  dry_run: bool = False) -> dict:
    """派发一批抓取，**立刻返回**；进度用 `job_progress(job_id)` 查。

    与 `acquire()` 的区别只有"等不等"，检查项完全一样（共用 `_preflight`）。

    **每篇一个任务**：这样进度能细到"第 2/4 篇"，而不是笼统的"运行中"。
    代价是每篇多 1~2 秒的任务往返 —— 对这个场景（一次几篇）可以接受。

    Returns:
        `{job_id, total, dry_run, plan:[{doi,title}]}`；`plan` 让 DSH 能立刻
        把"要抓哪几篇"念给用户听，不用等结果。
    """
    good, dropped = _preflight(items)
    plan = [{"doi": g["doi"], "title": g.get("title") or ""} for g in good]
    jid = _job_new(len(good), plan, dry_run)
    if dropped:
        _job_update(jid, failed=[{"doi": d, "why": "bad_doi",
                                  "detail": "不是合法的 DOI"} for d in dropped])
    th = threading.Thread(
        target=_run_batch,
        args=(jid, good, dropped, collection_id, find_pdf, dry_run),
        name="kb-acquire-" + jid, daemon=True)
    th.start()
    return {"job_id": jid, "total": len(good), "dry_run": bool(dry_run),
            "plan": plan,
            "note": "已派发。用 kb_acquire_progress 查进度；"
                    "完成后记得 kb_reindex 让新文献可搜。"}


def _run_batch(jid: str, good: list[dict], dropped: list[str],
               collection_id: int, find_pdf: bool, dry_run: bool) -> None:
    """异步作业的线程体：逐篇派任务，每完成一篇就更新进度。"""
    agg_results: list[dict] = []
    agg_dup: list[dict] = []
    agg_failed: list[dict] = []
    collection = None
    try:
        _job_update(jid, state="running", stage="开始")
        for i, it in enumerate(good):
            _job_update(jid, index=i, stage="解析元数据并入库",
                        current_doi=it["doi"],
                        current_title=it.get("title") or "")
            try:
                r = _dispatch_batch([it], collection_id, find_pdf, dry_run,
                                    0.0, None, [])
            except AcquireError as exc:
                # 单篇失败不能把整批带停 —— 记下来继续下一篇
                agg_failed.append({"doi": it["doi"], "why": "error",
                                   "detail": str(exc)})
                _job_update(jid, index=i + 1, results=list(agg_results),
                            duplicates=list(agg_dup), failed=list(agg_failed))
                continue
            agg_results.extend(r.get("results") or [])
            agg_dup.extend(r.get("duplicates") or [])
            agg_failed.extend(r.get("failed") or [])
            collection = r.get("collection") or collection
            _job_update(jid, index=i + 1, results=list(agg_results),
                        duplicates=list(agg_dup), failed=list(agg_failed),
                        collection=collection)

        # 汇总（与阻塞版同一套字段，好让两边能共用 format_report）
        final = {
            "ok": True,
            "dry_run": bool(dry_run),
            "collection": collection,
            "results": agg_results,
            "duplicates": agg_dup,
            "failed": agg_failed,
            "dropped": dropped,
            "added": sum(1 for r in agg_results if r.get("status") == "added"),
            "with_pdf": sum(1 for r in agg_results
                            if (r.get("pdf") or {}).get("ok")),
            "elapsed_s": round(time.time() - (job_progress(jid).get("started")
                                              or time.time()), 1),
        }
        _job_update(jid, state="done", stage="", index=len(good),
                    current_doi="", current_title="",
                    **{k: v for k, v in final.items()
                       if k in ("ok", "collection", "results", "duplicates",
                                "failed", "added", "with_pdf", "elapsed_s")})
    except Exception as exc:  # noqa: BLE001
        _job_update(jid, state="failed", error=f"{type(exc).__name__}: {exc}")



def _shape_result(task: dict, good: list[dict], dropped: list[str],
                  dry_run: bool, elapsed: float) -> dict:
    """把插件回传的结果整理成稳定的形状（字段名变了就在这里收口）。"""
    raw = task.get("result")
    inner = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except Exception:  # noqa: BLE001
            inner = {"raw": inner}
    if not isinstance(inner, dict):
        inner = {}
    if task.get("state") == "failed" or not inner:
        msg = task.get("error") or (inner.get("error") if inner else "") or "插件执行失败"
        raise AcquireError(f"插件执行失败：{msg}")

    out = {
        "ok": bool(inner.get("ok")),
        "cancelled": bool(inner.get("cancelled")),
        "nothing_to_do": bool(inner.get("nothingToDo")),
        "dry_run": bool(dry_run),
        "collection": inner.get("collection"),
        "added": int(inner.get("added") or 0),
        "with_pdf": int(inner.get("withPdf") or 0),
        "results": inner.get("results") or [],
        "duplicates": inner.get("duplicates") or [],
        "failed": inner.get("failed") or [],
        "dropped": dropped,
        "elapsed_s": round(elapsed, 1),
        "task_id": task.get("id"),
        # 插件侧的附注（例如"这批跳过了逐篇自动分类"）。**必须带出去** ——
        # 用户看不到它就会以为"自动分类坏了"。
        "note": inner.get("note") or "",
    }
    out["summary"] = summarize(out)
    return out


# ---------------------------------------------------------------- 结果讲人话


def _why(why: str) -> str:
    return {
        "no_translator": "Zotero 没有能识别这个 DOI 的翻译器",
        "no_result": "翻译器认了，但没返回任何条目",
        "translate_error": "翻译时报错",
        "error": "出错",
        "bad_doi": "这压根不是合法的 DOI",
    }.get(why, why or "未知原因")


def summarize(r: dict) -> str:
    """一句到几句的中文总结。**不夸大**：没下到 PDF 就说没下到。"""
    if r.get("cancelled"):
        return "用户在 Zotero 弹的确认框里点了取消，本次没有写入任何东西。"
    if r.get("nothing_to_do"):
        return ("没有可入库的条目 —— 这些 DOI 要么库里已经有了、"
                "要么 Zotero 抓不到元数据，所以什么都没写。")
    if r.get("dry_run"):
        n = r.get("added") or 0
        s = f"干跑：{n} 篇能抓到权威元数据（**没有写入 Zotero**）。"
    else:
        s = f"已入库 {r.get('added', 0)} 篇"
        if r.get("with_pdf"):
            s += f"，其中 {r.get('with_pdf')} 篇带上了 PDF"
        s += "。"
    dup = len(r.get("duplicates") or [])
    bad = len(r.get("failed") or [])
    extra = []
    if dup:
        extra.append(f"{dup} 篇库里已有（跳过）")
    if bad:
        extra.append(f"{bad} 篇抓不到元数据（跳过）")
    if r.get("dropped"):
        extra.append(f"{len(r['dropped'])} 条不是合法 DOI（跳过）")
    if extra:
        s += "另有 " + "、".join(extra) + "。"
    return s


def format_report(r: dict) -> str:
    """给模型/用户看的 Markdown 报告。CLI 和 MCP 工具共用，两处口径一致。"""
    lines = [f"## 从 DSH 导入 Zotero", ""]
    lines.append(r.get("summary") or "")
    lines.append("")
    if r.get("note"):
        lines.append("> " + str(r["note"]))
        lines.append("")
    col = r.get("collection")
    if col:
        lines.append(f"目标分类：**{col.get('name')}**"
                     f"（collectionID={col.get('id')}）")
        lines.append("")
    elif r.get("added") or r.get("results"):
        # ⚠ 没有分类**必须说出来**。用户在 Zotero 里可能压根没选中分类，
        #   条目就落到文库根目录了 —— 不说的话他回头在分类里找不到，
        #   会以为没入库。（真机遇到过：选中项为空，条目进了根目录。）
        lines.append("> ⚠ 存入的是**我的文库根目录** —— 抓取时你在 Zotero 里"
                     "没有选中任何分类。想放进某个分类：先在 Zotero 里选中它，"
                     "再抓一次；或者现在把它们拖进去。")
        lines.append("")

    rows = [x for x in (r.get("results") or []) if x.get("status") == "added"]
    if r.get("dry_run"):
        # 干跑：把 Zotero 真正解析出来的元数据摆出来（这些才是"抓得到"的证据）
        dry = [x for x in (r.get("results") or []) if x.get("status") == "dry"]
        if dry:
            lines.append("Zotero 能抓到这些（**尚未写入**）：")
            lines.append("")
            lines.append("| 标题 | 作者/年份 | 出处 |")
            lines.append("|---|---|---|")
            for x in dry:
                lines.append(f"| {str(x.get('title') or '')[:70]} "
                             f"| {x.get('byline') or ''} | {x.get('venue') or ''} |")
            lines.append("")
    if rows:
        lines.append("| 标题 | 条目 key | PDF |")
        lines.append("|---|---|---|")
        for x in rows:
            pdf = x.get("pdf") or {}
            if pdf.get("ok"):
                mark = "✅ 已带"
                if pdf.get("attachmentTitle"):
                    mark += "（" + str(pdf["attachmentTitle"])[:30] + "）"
            elif pdf.get("error"):
                mark = "⚠ " + str(pdf["error"])[:60]
            elif pdf.get("tried"):
                # ⚠ 这一支原来落进了下面的 else，被写成"—（未尝试）"，
                #   可同一行又列着"试过：doi/url/oa/oa" —— 自相矛盾。
                #   Zotero 的 addFileFromURLs **一条路都没走通时可能既不抛异常
                #   也不给 noFileFound**（直接返回假值），所以判据要加上
                #   "tried 里有东西 = 确实试过了"。
                mark = "❌ 没找到可用的"
            elif pdf.get("noFileFound"):
                mark = "❌ 没找到可用的"
            else:
                mark = "—（未尝试）"
            if pdf.get("tried"):
                mark += "｜试过：" + "/".join(str(m) for m in pdf["tried"])
            lines.append(f"| {str(x.get('title') or '')[:70]} "
                         f"| `{x.get('itemKey') or ''}` | {mark} |")
        lines.append("")

    bad = r.get("failed") or []
    if bad:
        lines.append("**没抓到元数据的：**")
        for b in bad[:15]:
            extra = f"（{b.get('detail')[:60]}）" if b.get("detail") else ""
            lines.append(f"- `{b.get('doi')}` — {_why(b.get('why'))}{extra}")
        lines.append("")

    dup = r.get("duplicates") or []
    if dup:
        lines.append("**库里已有的（跳过，没重复入库）：**")
        for d in dup[:15]:
            lines.append(f"- `{d.get('doi')}`"
                         + (f" — {d.get('title')}" if d.get("title") else "")
                         + (f"（`{d.get('key')}`）" if d.get("key") else ""))
        lines.append("")

    if r.get("dropped"):
        lines.append("**不是合法 DOI 的：** "
                     + "、".join(f"`{x}`" for x in r["dropped"][:10]))
        lines.append("")

    if (r.get("added") and not r.get("dry_run")
            and any(not (x.get("pdf") or {}).get("ok")
                    for x in (r.get("results") or []))):
        lines.append("> 没带上 PDF 的，可以用浏览器连接器抓（在出版社页面点 Zotero "
                     "图标）—— 那条路会带上你浏览器的登录态，比进程内抓取能过的站点更多。")
        lines.append("")
    # 用时：阻塞版给 elapsed_s，异步作业给 age_s —— 两个都没有就整段省掉，
    # 不要印出 "_用时 Nones_" 这种（真出现过）。
    secs = r.get("elapsed_s")
    if secs is None:
        secs = r.get("age_s")
    tail = []
    if secs is not None:
        tail.append(f"用时 {secs}s")
    if r.get("task_id"):
        tail.append(f"任务 {r['task_id']}")
    if r.get("job_id"):
        tail.append(f"作业 {r['job_id']}")
    if tail:
        lines.append("_" + "；".join(tail) + "_")
    return "\n".join(lines)


def reindex_hint(keys: list[str]) -> str:
    """入库之后怎么让知识库能搜到 —— 这句话必须跟着结果一起给，否则用户会以为没成功。"""
    if not keys:
        return ""
    return ("\n**下一步（不然检索不到）**：新条目还不在知识库里，"
            "用 `kb_reindex` 逐篇补抽即可立刻可搜：\n"
            + "\n".join(f"- `kb_reindex(key=\"{k}\")`" for k in keys[:10]))


# ---------------------------------------------------------------- 兜底：挂本地 PDF


def attach_files(files, timeout: float = 240.0, progress=None) -> dict:
    """把本机已经下好、验过是 PDF 的文件挂到**刚建的**条目上。

    这一步补的是"兜底下载"的最后一环：DSH 侧用 `fetch-pdf.ps1` 下下来的文件
    如果不挂进 Zotero，用户还是得手工拖 —— 那等于没解决他最初的问题
    （"不是下载下来手动拉"）。能自动挂的就不让用户动手。

    Args:
        files: `[{"itemKey": "ABCD1234", "path": "D:\\\\...\\\\x.pdf", "title": ""}]`

    Returns:
        dict，含 ok / attached / results。
    """
    clean = []
    for f in files or []:
        key = str((f or {}).get("itemKey") or "").strip()
        path = str((f or {}).get("path") or "").strip()
        if not key or not path:
            continue
        if not os.path.isfile(path):
            raise AcquireError(f"文件不存在：{path}",
                               ["先确认下载真的成功了（fetch-pdf.ps1 的退出码为 0）。"])
        clean.append({"itemKey": key, "path": os.path.abspath(path),
                      "title": str((f or {}).get("title") or "")})
    if not clean:
        raise AcquireError("没有可挂的文件（每条都要 itemKey 和 path）。")

    if not service_alive():
        raise AcquireError("本机知识库服务没在跑（127.0.0.1:8765）。",
                           ["双击 scripts\\4-service.vbs。"])
    alive, why = _plugin_online()
    if not alive:
        raise AcquireError(f"Zotero 插件没法挂接文件（{why}）。",
                           ["完全退出 Zotero 再重新打开，再试一次。"])

    if progress:
        progress(f"把 {len(clean)} 个文件交给 Zotero 挂接…")
    try:
        resp = _api("POST", "/task",
                    {"kind": "attach",
                     "code": json.dumps({"files": clean}, ensure_ascii=False),
                     "timeout": timeout}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        raise AcquireError(f"派任务失败：{exc}") from exc
    tid = ((resp or {}).get("task") or {}).get("id")
    if not tid:
        raise AcquireError(f"服务没返回任务 id：{resp}")

    started = time.time()
    while time.time() - started < timeout:
        time.sleep(1.2)
        try:
            t = _api("GET", f"/task/{tid}", timeout=15)
        except Exception:  # noqa: BLE001
            continue
        if t.get("state") in ("done", "failed"):
            raw = t.get("result")
            inner = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except Exception:  # noqa: BLE001
                    inner = {}
            if not isinstance(inner, dict) or not inner:
                raise AcquireError(f"挂接失败：{t.get('error') or '插件没回结果'}")
            inner["elapsed_s"] = round(time.time() - started, 1)
            return inner
    raise AcquireError(f"等插件挂接超时（{timeout:.0f}s）。")


def format_attach_report(r: dict) -> str:
    rows = r.get("results") or []
    ok = [x for x in rows if x.get("status") == "attached"]
    bad = [x for x in rows if x.get("status") != "attached"]
    lines = [f"## 挂接本地 PDF", "",
             f"挂上 {len(ok)} 个，失败 {len(bad)} 个。", ""]
    if ok:
        lines.append("| 条目 key | 附件 key |")
        lines.append("|---|---|")
        for x in ok:
            lines.append(f"| `{x.get('itemKey')}` | `{x.get('attachmentKey')}` |")
        lines.append("")
    if bad:
        lines.append("**没挂上的：**")
        for x in bad:
            lines.append(f"- `{x.get('itemKey')}` ← `{x.get('path')}`：{x.get('error')}")
        lines.append("")
    lines.append("> 挂上附件后，知识库里那一条还是用旧内容 —— "
                 "要让 PDF 正文进检索，用 `kb_reindex(key=...)` 重抽一次。")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(description="把 DSH 搜到的文献交给 Zotero 抓取")
    ap.add_argument("--doi", action="append", default=[],
                    help="可重复；DOI 或 https://doi.org/... 均可")
    ap.add_argument("--list-file", default="",
                    help="每行一个 DOI 的文件（以 # 开头的行忽略）")
    ap.add_argument("--collection", type=int, default=0,
                    help="目标 collectionID；0=用 Zotero 当前选中的分类")
    ap.add_argument("--no-pdf", action="store_true", help="不找 PDF，只建条目")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析元数据，**不写库**")
    ap.add_argument("--timeout", type=float, default=0.0)
    args = ap.parse_args()

    items = list(args.doi)
    if args.list_file:
        with open(args.list_file, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    items.append(ln)
    if not items:
        ap.error("至少要给一个 --doi 或 --list-file")

    try:
        r = acquire(items, collection_id=args.collection,
                    find_pdf=not args.no_pdf, dry_run=args.dry_run,
                    timeout=args.timeout,
                    progress=lambda t: print("  " + t, flush=True))
    except AcquireError as exc:
        print(f"\n[XX] {exc}")
        for h in exc.how_to_fix:
            print(f"     · {h}")
        return 1

    print("")
    print(format_report(r))
    keys = [x.get("itemKey") for x in (r.get("results") or []) if x.get("itemKey")]
    if keys and not r.get("dry_run"):
        print(reindex_hint(keys))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
