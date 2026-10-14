"""知识库本地引擎：父子进程 + stdin/stdout 上的 JSON-RPC 服务端。

    python online/engine.py          # 由宿主（Zotero 插件）用管道拉起

为什么要有它（范围 B 第一波，见开发说明）：用户的第一诉求是
**不要监听端口**（不想在本机开任何 TCP 监听）。原来的常驻 HTTP 服务
（online/localserver.py，127.0.0.1:8765）改成"宿主拉起子进程、用管道说话"：
**无端口、无 token、不常驻、不自启** —— Zotero 在跑时引擎是它的子进程、
随 Zotero 退出；Zotero 关着时引擎只活在外部启动器里。

协议（NDJSON：一行一条 JSON，强制 UTF-8）
    请求   {"id":1,"method":"kb.status","params":{...}}
    成功   {"id":1,"ok":true,"data":{...}}
    失败   {"id":1,"ok":false,"error":"人能看懂的错误"}
    进度   {"id":1,"event":"progress","data":{"percent":35,"message":"…"}}   （同一 id 可多次）
    长任务（kb.reindex / kb.digest / learn.scan / meta.fillScan）**派发即返回**：
    立刻回一条 ok，data 形如 {"job":{"id":"job-…"}, "id":"job-…", "state":"running"}，
    之后由调用方轮询 `jobs.get`（`GET /jobs/<id>`）拿 state / log / result。
    **不等它跑完**（一篇学位论文要几分钟，阻塞会把宿主的 180s 超时撞穿）。

⚠ 两条硬规矩（宿主半请照做）：
  1. **stdout 只许出现协议 JSON** —— 本进程把真正的 stdout 收进 `_STDOUT`，
     再把 `sys.stdout` 换成了 stderr：任何外来模块的 print / 日志都会落到
     stderr（宿主收进日志），绝不会污染协议流。日志一律走 stderr。
  2. **必须设 `PYTHONIOENCODING=utf-8`**（本文件自己也会 reconfigure）——
     Windows 下不设，中文会被 GBK 撕碎，宿主读到的是乱码参数。

⚠ 一条引擎自己的硬规矩（改本文件前先看 `_StdinLines` 那段）：
  **主循环不许用阻塞的 `sys.stdin.readline()` 读请求**。只要有一条线程挂在
  读 stdin 这个管道上，本进程里任何 DLL 初始化（numpy→OpenBLAS、onnxruntime）
  都会永久卡住、连它起的子进程都跑不起来 —— 这是 `kb.search` 卡死的真根因
  （2026-10-13 查明；复现脚本在该次会话目录的 `repro_engine_hang.py`）。

业务**一律不重写**：全部复用 online/localserver.py 里的既有实现
（health_payload / do_status / do_reindex / do_digest / do_jobs_list /
start_reindex_job / start_digest_job …）与 offline/*.py 里已有的实现
（experience / prompts / learn / digest / kbviews / metafill / typefix）。
localserver 那边只做了"把逻辑提成模块级函数"的搬运，**HTTP 行为一个字没改**
—— 切换之前旧服务照常能用，那是验证新通道的手段。

W2 批次 A（2026-10-13）新增的方法表（契约见开发说明）：
    experience.list / experience.get / experience.save / experience.delete /
    pending.mark / sessions.list / learn.scan /
    outline.list / outline.render / outline.delete /
    meta.fillScan / meta.fillCancel / meta.typeScan / env.check / env.save /
    llm.config / llm.save / llm.models / llm.test / service.restart /
    prompts.list / prompts.save / prompts.probe

W2 批次 B（2026-10-13）新增的方法表（契约见开发说明）：
    papers.list / ai.summarize / ai.classify / ai.apply / maintain.run /
    mineru.status / mineru.parse / mineru.clear /
    quality.stats / quality.scan / quality.repair / quality.detail /
    config.figures / config.figuresGet /
    kb.search / kb.weight / advanced.weightList / advanced.collections /
    advanced.zoteroSync / advanced.plugin / advanced.upgrade
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime

# ---------------------------------------------------------------- 流与编码
#
# ⚠ 顺序很重要：**先** reconfigure 真 stdout（要求它 UTF-8 + 只用 \n），
#   **再** 把它收进 _STDOUT、把 sys.stdout 指向 stderr。
#   这样即使某个被 import 的模块里有裸 print，也只会写到 stderr。
if sys.stdout is None or sys.stdin is None:
    # pythonw 下两个流都是 None —— 管道协议没法工作，早点说清楚
    sys.stderr.write("engine.py 需要可用的 stdin/stdout（不要用 pythonw 启动）\n")
    raise SystemExit(2)

sys.stdout.reconfigure(encoding="utf-8", newline="\n")
sys.stdin.reconfigure(encoding="utf-8")
_STDOUT = sys.stdout
sys.stdout = sys.stderr          # 从此任何 print 都进 stderr，不污染协议
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                # noqa: BLE001 —— 拿不到无所谓，日志宁缺勿扰
    pass

# ---------------------------------------------------------------- stdin 怎么读
#
# ⚠⚠ 主循环**绝对不能**用阻塞的 `sys.stdin.readline()` 等请求。本机实测
#     （2026-10-13；最小复现见会话目录
#     `sessions/2026-10-13-zotero-kb-交接执行/repro_engine_hang.py`）：
#     **只要有线程正阻塞在 ReadFile 上读 stdin 这个管道**，本进程里任何 DLL 的
#     初始化都会永久卡住，连它之后起的子进程也起不来 ——
#       · `import numpy`（要载 OpenBLAS）/ `import onnxruntime` 永不返回，
#         于是 `kb.search` 与 `experience.list?mode=query` 全卡死；
#       · `ctypes.WinDLL(libscipy_openblas*.dll)` 直接把那个 DLL 载进来也永不返回；
#       · `subprocess.Popen` 出来的子进程**一句代码都不执行**（python.exe 自己的
#         启动就要载 DLL；只要它继承了同一个 stdin 句柄就起不来）。
#     管道里一有数据、那个挂着的读一返回，卡住的动作**当场**接着跑完
#     （实测：第 10.128 秒往 stdin 写一行，卡了 10 秒的 numpy 立刻加载完）。
#     与 stdout 无关（把 stdout 换成文件照样卡）、与管道缓冲区满无关
#     （换一条**别的**匿名管道去阻塞读就不卡）、与被放进 job object 无关。
#
#     所以这里自己按"可读才读"取行：每 20ms 用 PeekNamedPipe 看一眼，
#     **有数据才 os.read**（绝不把线程停在 ReadFile 上），行缓冲自己拼。
#     stdin 不是 Windows 管道时（手敲命令行、Linux、CI）自动退回原来的阻塞
#     readline —— 那些场合没有这个毒。

_POLL_INTERVAL = 0.02      # 轮询间隔：请求延迟 ≤20ms；空闲时 50 次/秒的廉价探测
                           # （实测空闲净烧 ~0.3% 单核 —— 用一点空转换"绝不阻塞读"）
_READ_CHUNK = 65536


class _StdinLines:
    """从 stdin 取行：Windows 管道走 PeekNamedPipe 轮询，其余退回阻塞 readline。"""

    def __init__(self):
        self._mode = None          # None 未定 / "poll" / "block"
        self._buf = b""
        self._ctypes = None
        self._k32 = None
        self._handle = None
        self._avail = None
        self._fd = -1

    def _try_poll(self) -> bool:
        """探一下能不能走轮询路（不是 Windows 管道就返回 False，退回老路）。

        ⚠ 探不动就退回阻塞 readline：宁可保留老行为，也不能让引擎打不开。
        """
        if os.name != "nt":
            return False
        try:
            import ctypes
            import msvcrt
            fd = sys.stdin.fileno()
            handle = msvcrt.get_osfhandle(fd)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            avail = ctypes.c_ulong()
            if not k32.PeekNamedPipe(ctypes.c_void_p(handle), None, 0, None,
                                     ctypes.byref(avail), None):
                return False
            msvcrt.setmode(fd, os.O_BINARY)     # 别让 CRT 再做一次 CRLF 翻译
            self._ctypes = ctypes
            self._k32 = k32
            self._handle = ctypes.c_void_p(handle)
            self._fd = fd
            self._avail = avail
            return True
        except Exception:          # noqa: BLE001 —— 探测失败不许影响引擎启动
            return False

    def readline(self) -> str:
        """等价于 `sys.stdin.readline()`（含空串表示 EOF），但绝不阻塞在内核里。

        ⚠ 返回值必须**带上行尾的 \\n**：主循环用 `if not line` 判 EOF，
          所以"一个空行"（"\\n"）与"流结束"（""）必须分得开 —— 少了这个，
          宿主随手写一个空行就会把引擎关掉。
        """
        if self._mode is None:
            self._mode = "poll" if self._try_poll() else "block"
            log("stdin 读法：" + ("PeekNamedPipe 轮询（不阻塞在 ReadFile 上）"
                                  if self._mode == "poll" else "阻塞 readline"))
        if self._mode == "block":
            return sys.stdin.readline()
        while b"\n" not in self._buf:
            if not self._fill():
                line, self._buf = self._buf, b""
                return line.decode("utf-8", "replace")
        line, self._buf = self._buf.split(b"\n", 1)
        return (line + b"\n").decode("utf-8", "replace")

    def _fill(self) -> bool:
        """等到有数据（补进缓冲，返回 True）或 EOF（返回 False）。

        ⚠ 只有 PeekNamedPipe 说有数据时才 os.read —— 所以本线程永远不会
          停在 ReadFile 上，也就不会把整个进程锁住（见上面那段说明）。
        """
        ctypes = self._ctypes
        while True:
            ok = self._k32.PeekNamedPipe(self._handle, None, 0, None,
                                         ctypes.byref(self._avail), None)
            if not ok:                       # 写端关掉了 / 出错 —— 一律当 EOF
                return False
            n = int(self._avail.value)
            if n:
                chunk = os.read(self._fd, min(n, _READ_CHUNK))
                if not chunk:
                    return False
                self._buf += chunk
                return True
            time.sleep(_POLL_INTERVAL)


STDIN = _StdinLines()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)     # 仓库根（offline/ online/ tools/ 的上一层）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import localserver as LS         # noqa: E402 —— 业务实现唯一来源（它会自己把 offline/ 加进 sys.path）
import schemas as S              # noqa: E402 —— 时间戳 / 目录大小 / 人读大小只有它那一份实现

ENGINE_VERSION = "1.0"

_WRITE_LOCK = threading.Lock()   # 多个请求线程 + 进度流会并发写 stdout


# ---------------------------------------------------------------- 输出


def emit(obj: dict) -> None:
    """写一行协议 JSON 到真 stdout。**只允许在这里写 stdout。**"""
    line = json.dumps(obj, ensure_ascii=False)
    with _WRITE_LOCK:
        _STDOUT.write(line + "\n")
        _STDOUT.flush()


def log(msg: str) -> None:
    """日志一律走 stderr（宿主会把 stderr 收进日志）。"""
    try:
        sys.stderr.write(f"[engine {datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()
    except Exception:            # noqa: BLE001 —— 日志失败绝不允许影响业务
        pass


class RpcError(Exception):
    """方法参数不对 / 业务上就该拒绝 → 直接回 ok:false，不打印堆栈。"""


# ---------------------------------------------------------------- 方法实现
#
# 每个方法接收 params（dict），返回 data（dict），失败抛 RpcError。
# ⚠ 参数校验放这里、不放进业务函数：业务函数（do_reindex 等）一被调用就会
#   真派活（跑子进程 / 调模型），参数不对时必须**在派活之前**就挡住。


def _m_ping(params: dict) -> dict:
    """探活。宿主用它确认引擎起来了、拿到版本与 pid。"""
    return {"ok": True, "version": ENGINE_VERSION, "pid": os.getpid()}


def _m_health(params: dict) -> dict:
    """等价 GET /health（**不含 token** —— stdio 通道没有浏览器这回事）。"""
    return LS.health_payload()


def _m_status(params: dict) -> dict:
    """状态卡的数字。

    直接复用 HTTP /status 的那份实现（LS.do_status），只**追加**一个
    `fulltext_chars`（`SUM(items.fulltext_chars)`，W1 状态卡要的新数字）。
    为什么要另开一次连接：不把新字段塞进 do_status，是为了**不改 HTTP 的
    /status 响应**（旧服务行为一个字不许变）；这条 SELECT 只扫一列，代价可忽略。
    """
    from searcher import Searcher

    out = dict(LS.do_status())
    s = Searcher()
    try:
        row = s.read_one("SELECT COALESCE(SUM(fulltext_chars), 0) AS n FROM items")
        out["fulltext_chars"] = int((row["n"] if row else 0) or 0)
    finally:
        s.close()
    return out


def _human(n: int) -> str:
    """字节数 → 人读的大小。**实现只有一份：`schemas.human_size()`**（2026-10-15 合并）。

    ⚠ 原先这里的注释说「口径与面板一致（tab_struct.py，已退役）」—— 面板没了，
      口径落在 schemas 上：≥1 GB 现在显示 "2.00 GB" 而不是 "2048.0 MB"。
    """
    return S.human_size(n)


# 「能删吗」的短标签。**与 tools/panels/tab_struct.py（已退役 2026-10-14）::SAFE_SHORT 同一份语义**
# （按 KB_FILE_SPEC 里那个字符串的首符号判：✗ 绝不能删 / △ 看情况 / ○ ✓ 可删）。
_SAFE_SHORT = {"✗": "绝不删", "○": "可删", "✓": "可删",
               "△": "看情况", "?": "看情况"}


def _m_structure(params: dict) -> dict:
    """「知识库结构」页那张表。

    ⚠ 口径**照抄** tools/panels/tab_struct.py（已退役 2026-10-14）::refresh_struct()（那才是原实现），
      但清单本身来自 tools/panels/common.py（已退役 2026-10-14）::KB_FILE_SPEC —— **唯一事实来源**，
      新增目录时两张视图一起就有了（工作区规矩：新目录必须进 KB_FILE_SPEC）。
      不 import tab_struct 是因为它依赖 Tk（要真窗口才能跑）。

    每行字段（宿主半与页面照这个写）：
        name      文件 / 文件夹名（目录带结尾反斜杠；glob 条目是模式名）
        what      存放内容（**已剥掉 markdown 的 `**`** —— Tk 不渲染它，
                  页面是 HTML 能渲染，但保持与面板同形更省心）
        count     内容量（目录 "N 个文件" / db "见上面状态栏" / glob "N 个" /
                  单文件 "12.3 KB"）
        size      占用字节数（int）
        size_text 占用的人读写法（"12.3 MB"）
        deletable 短标签（绝不删 / 可删 / 看情况）
        note      「能删吗」的**长解释**（面板里是悬停提示的原文；空串表示没有）
    """
    try:
        # W4（2026-10-14）：这张清单原来在 tools/panels/common.py（Tk 面板的共用模块）。
        # Tk 面板退役后搬到了 offline/kb_manifest.py —— **删的是界面，不是这份唯一事实来源**。
        from kb_manifest import KB_FILE_SPEC
    except Exception as exc:     # noqa: BLE001
        raise RpcError(f"读不到知识库结构清单（offline/kb_manifest.py）："
                       f"{type(exc).__name__}: {exc}") from exc

    kb = LS.S.KB_DIR

    def sizeof(path: str) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return total

    def plain(s: str) -> str:
        # Treeview 不渲染 markdown，面板统一剥掉 `**`；保持一致（见 KB_FILE_SPEC 注释）
        return (s or "").replace("**", "")

    def short(safe: str) -> str:
        return _SAFE_SHORT.get((safe or "").strip()[:1], "看情况")

    rows: list[dict] = []
    used = 0
    for name, what, kind, safe in KB_FILE_SPEC:
        what, safe = plain(what), plain(safe)
        p = os.path.join(kb, name.rstrip("\\/"))
        if kind == "dir":
            if not os.path.isdir(p):
                continue
            n = sum(len(fs) for _dp, _dn, fs in os.walk(p))
            size = sizeof(p)
            count = f"{n} 个文件"
        elif kind == "db":
            if not os.path.exists(p):
                continue
            # 只算主库：-wal / -shm 由 `index.db-*` 那一行单独列，这里再算一遍
            # 会让「合计」重复计（面板注释里记过这个坑）
            size = os.path.getsize(p)
            count = "见上面状态栏"
        elif kind == "glob":
            # 一类文件合并成一行（历史备份这种：多份、同样大，逐行列反而看不清）
            import glob as _glob
            hits = [x for x in _glob.glob(p) if os.path.isfile(x)]
            if not hits:
                continue
            size = sum(os.path.getsize(x) for x in hits)
            count = f"{len(hits)} 个"
        else:
            if not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            count = _human(size)
        used += size
        rows.append({"name": name, "what": what, "count": count,
                     "size": size, "size_text": _human(size),
                     "deletable": short(safe), "note": safe})

    # 不在清单里的文件也要显示 —— 否则用户看到目录里有东西、表里没有会疑惑
    try:
        import fnmatch

        known = {n.rstrip("\\/").lower() for n, _w, k, _s in KB_FILE_SPEC
                 if k != "glob"}
        patterns = [n.lower() for n, _w, k, _s in KB_FILE_SPEC if k == "glob"]
        extras = [n for n in os.listdir(kb)
                  if n.lower() not in known
                  and not any(fnmatch.fnmatch(n.lower(), g) for g in patterns)]
        for n in sorted(extras):
            p = os.path.join(kb, n)
            if os.path.isdir(p):
                size = sizeof(p)
                cnt = f"{sum(len(fs) for _dp, _dn, fs in os.walk(p))} 个文件"
            else:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                cnt = _human(size)
            used += size
            rows.append({
                "name": n + ("\\" if os.path.isdir(p) else ""),
                "what": "（未在说明表里的文件，可能是新版本新增的）",
                "count": cnt, "size": size, "size_text": _human(size),
                "deletable": "看情况",
                "note": "不在说明表里（可能是新版本新增的）：先别删，"
                        "先确认它是干什么的。"})
    except OSError:
        pass

    return {"kb_dir": kb, "rows": rows,
            "total_size": used, "total_size_text": _human(used),
            "generated_at": datetime.now().isoformat(timespec="seconds")}


def _m_jobs_list(params: dict) -> dict:
    """长任务列表。**与 HTTP GET /jobs 同形**：{"jobs": [...]}（每条不含 log）。"""
    return LS.do_jobs_list()


def _m_jobs_get(params: dict) -> dict:
    """一条任务的详情。**与 HTTP GET /jobs/<id> 同形**（含 log）。缺 id / 没有都报错。"""
    job_id = str(params.get("id") or "").strip()
    if not job_id:
        raise RpcError("需要 id（POST /jobs/<id> 的那个 id）")
    job = LS.do_jobs_get(job_id)
    if job is None:
        raise RpcError(f"没有 {job_id}")
    return job


def _job_dispatch(job_id: str) -> dict:
    """长任务的**派发**回执：立刻返回 job id，**不等它跑完**。

    为什么必须"派发即返回"（2026-10-13 用户拍板）：一篇学位论文的纲要要几分钟，
    而宿主半的单条 RPC 超时只有 180s —— 阻塞式（W1 早期的形状）会让页面收到一个
    超时错误，可任务其实还在跑（用户以为卡死，还可能重复点一遍）。所以长任务一律
    先回 `{job:{id}}`，之后页面用 `admin.js::runTask` 轮询 `GET /jobs/<id>`。
    这也与契约里其它长任务（`learn.scan` / `meta.fillScan`）的形状一致。

    ⚠ `job` 与 `id` 给的是同一个值、两种用法：`job` 是 `{id}` 对象（契约形状，
      也是 `runTask` 优先读的那个）；`id` / `job_id` 是扁平写法，给只认字符串的
      调用方（引擎测试、脚本）留的。
    ⚠ **进度一律靠轮询 `job.log`，不做事件转发**：宿主半没有把 progress 事件转给
      页面（`src/23-admin.js::kbAdminForwardProgress` 只记日志），而 `job.log`
      本来就有每一行进度，页面轮询够用。**别把这里当成"漏了转发"去补。**
    """
    return {"job": {"id": job_id}, "id": job_id, "job_id": job_id,
            "state": "running"}


def _m_reindex(params: dict) -> dict:
    """建索引（长任务，**派发即返回**）。参数 {keys?: [...], full?: bool}。"""
    keys = params.get("keys") or []
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list):
        raise RpcError("keys 必须是数组")
    full = bool(params.get("full"))
    rid = _rid()
    job_id = LS.start_reindex_job(keys, full)
    emit({"id": rid, "event": "progress",
          "data": {"job": job_id, "percent": 0,
                   "message": f"已派发建索引任务 {job_id}"
                              + ("（全量）" if full else "（增量）")}})
    return _job_dispatch(job_id)


def _m_digest(params: dict) -> dict:
    """生成分节纲要（长任务，**派发即返回**）。参数 {key, force?}。"""
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    job_id = LS.start_digest_job(key, bool(params.get("force")))
    rid = _rid()
    emit({"id": rid, "event": "progress",
          "data": {"job": job_id, "percent": 0,
                   "message": f"已派发分节纲要任务 {job_id}（{key}）"}})
    return _job_dispatch(job_id)


# ================================================================ W2 批次 A
#
# 管理面板剩下的 5 页（经验库 / 分节纲要 / 元数据 / 运行环境 / 提示词）要的
# 引擎方法。契约见开发说明（**冻结**，页面与引擎都照它写）：
#   · 列表一律 {rows:[…]}（不裸数组）；
#   · 失败一律 ok:false + 中文原因（坏输入不许弄死引擎）；
#   · 长任务先回 {job:{id}}，之后 GET /jobs/<id> 轮询。
#
# ⚠ 业务**一律不重写**：能用 localserver 的模块级函数（do_* / _safe_llm_config /
#   env_report / _outline_*）就用它；offline/*.py 里已有的实现（experience /
#   prompts / learn / digest / kbviews / schemas）直接用。这里只做
#   **参数校验 + 形状翻译**两件事。


def _as_int(value, default: int = 0) -> int:
    """宽松取整数（query 串过来的一律是字符串，坏值退回默认，不抛）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _searcher():
    """开一个 Searcher（读**与写**都要走它）。

    ⚠ 引擎每个请求一个线程，所以**并发是真实存在的** —— 绝不能用
      `schemas.connect()` + `ConnWriter`（那是"只给单线程调用方"用的，
      并发下会静默丢写，见 experience.ConnWriter 的注释）。
      Searcher 带 `_lock`，是项目里唯一并发安全的写入口。
    """
    from searcher import Searcher
    return Searcher()


# ---------------------------------------------------------------- 经验库

def _keys_from_refs(refs) -> list:
    """把契约里的 `refs` 规整成 key 列表。

    `refs` 允许 `["A","B"]`（前端顺手传）或 `[{key,label}]`（契约形态、
    也是查询回来的形态）—— 两种都收，免得页面为了"改一条"要先剥一层壳。
    """
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    out: list = []
    for x in refs:
        k = x.get("key") if isinstance(x, dict) else x
        k = str(k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _exp_refs(value) -> list:
    """`item_keys`/`items` → 契约要的 `[{key,label}]`。

    ⚠ 显示口径只有一份：`offline/experience.py::item_refs`（作者 年份 短标题
      [KEY]，已删/已合并的标出来）。这里只把它的 `items` 翻成字段名，
      **不许**自己拼字符串。
    """
    import experience as EXP
    refs = EXP.item_refs(value)
    return [{"key": it.get("key", ""), "label": it.get("text", "")}
            for it in (refs.get("items") or [])]


def _exp_row(row: dict, *, pending: bool = False, checkup: str = "") -> dict:
    """一条经验 → 列表行的契约形状（all/query/checkup/pending 四种视图共用）。

    ⚠ 除了契约列出的 7 个字段，这里**顺带**给 `method/reason/session/tags/
      item_keys`：经验库页面拿到它们就能直接画详情，省掉一次
      `GET /experience/<id>`（页面文件头写了这是可选优化）。
      多给的字段对只认契约字段的调用方无害。
    """
    import experience as EXP
    if pending:
        # pending.jsonl 的一行：时间在 when、来源是本地小模型抽的
        date = str(row.get("when") or "")[:10]
        source = "小模型"
        summary = row.get("suspect") or row.get("reason") or ""
        value = row.get("items") or []
        tags: list = []
        session = row.get("session") or ""
    else:
        date = str(row.get("created_at") or "")[:10]
        source = row.get("source") or ""
        summary = row.get("reason") or row.get("method") or ""
        value = row.get("item_keys")
        tags = EXP.loads(row.get("tags"), [])
        session = row.get("session") or ""
    out = {"id": row.get("id"), "date": date,
           "effect": row.get("outcome") or "",
           "source": source, "refs": _exp_refs(value),
           "question": row.get("asked") or "", "summary": str(summary or ""),
           # ---- 页面画详情用的可选字段（见上面说明）
           "outcome": row.get("outcome") or "",
           "method": row.get("method") or "",
           "reason": row.get("reason") or "",
           "session": session, "tags": tags,
           "item_keys": EXP.keys_of(value)}
    if checkup:
        out["checkup"] = checkup
    return out


def _m_experience_list(params: dict) -> dict:
    """经验库列表。参数 `{mode:"all"|"query"|"checkup"|"pending", q?, limit?}`。

    `mode` 的口径照 Tk 面板 `tools/panels/tab_experience.py（已退役 2026-10-14）::_exp_load`：
      · all     按 id 升序全部；
      · query   走 `Searcher.related_experience`（关键词+语义），要 q；
      · checkup 只列**可疑**的（判据在 experience.checkup_reason，不许在这里另写），
                按 id 降序（近的在前，与面板一致）；
      · pending 读 `learn.py` 的 `inbox/pending.jsonl` 里 status=pending 的。
    """
    mode = str(params.get("mode") or "all").strip().lower()
    if mode not in ("all", "query", "checkup", "pending"):
        raise RpcError('mode 只能是 "all" / "query" / "checkup" / "pending"')
    q = str(params.get("q") or "").strip()
    limit = _as_int(params.get("limit"), 0)

    if mode == "pending":
        import learn as L
        rows = [r for r in L._read_pending() if r.get("status") == "pending"]
        out = [_exp_row(r, pending=True) for r in rows]
    else:
        import experience as EXP
        s = _searcher()
        try:
            if mode == "query":
                if not q:
                    raise RpcError("query 模式需要 q（关键词）")
                raw = [dict(r) for r in s.related_experience(q, limit=limit or 60)]
            elif mode == "checkup":
                raw = [dict(r) for r in
                       s.read("SELECT * FROM experience ORDER BY id DESC")]
            else:
                raw = [dict(r) for r in
                       s.read("SELECT * FROM experience ORDER BY id")]
        finally:
            s.close()
        out = []
        for r in raw:
            why = EXP.checkup_reason(r) if mode == "checkup" else []
            if mode == "checkup" and not why:
                continue
            out.append(_exp_row(r, checkup="；".join(why)))

    if limit > 0:
        out = out[:limit]
    return {"rows": out, "total": len(out), "mode": mode}


def _m_experience_get(params: dict) -> dict:
    """一条经验的详情。参数 `{id}`（路径 `/experience/<id>` 里取）。"""
    exp_id = _as_int(params.get("id"), 0)
    if exp_id <= 0:
        raise RpcError("需要 id（整数，正数）")
    import experience as EXP
    s = _searcher()
    try:
        row = EXP.get_experience(s, exp_id)
    finally:
        s.close()
    if not row:
        raise RpcError(f"没有 id={exp_id} 的经验")
    return {"id": exp_id, "date": str(row.get("created_at") or "")[:10],
            "effect": row.get("outcome") or "", "source": row.get("source") or "",
            "session": row.get("session") or "",
            "question": row.get("asked") or "", "method": row.get("method") or "",
            "reason": row.get("reason") or "",
            "refs": _exp_refs(row.get("item_keys")),
            "tags": EXP.loads(row.get("tags"), []),
            "item_keys": EXP.loads(row.get("item_keys"), []),
            # 详情位要用的另外两段（契约没列，但面板里是逐字展示的；
            # 多余字段不影响页面，缺了反而要再查一次库）
            "context": row.get("context") or "", "evidence": row.get("evidence") or ""}


def _m_experience_save(params: dict) -> dict:
    """新增 / 修改一条经验。参数 `{id?, question, method?, reason?, effect,
    source?, refs?[], tags?[]}` → `{id, created}`。

    写入只有一份实现（`experience.add_experience` / `update_experience`）：
    它们会**在同一个事务里**改行 + 回滚旧权重 + 应用新权重，不许在这里自己拼 SQL。
    """
    question = str(params.get("question") or "").strip()
    effect = str(params.get("effect") or "").strip().lower()
    if not question:
        raise RpcError("需要 question（这条经验在回答什么问题）")
    if not effect:
        raise RpcError("需要 effect（effective / ineffective / partial / unknown）")

    import experience as EXP
    exp_id = _as_int(params.get("id"), 0)
    # ⚠ 只有**传进来的**字段才改（update_experience 的语义：None = 不动）。
    #   不能无脑塞 method=""，那会把编辑时没展开的字段清空。
    fields: dict = {"asked": question, "outcome": effect}
    for name, key in (("method", "method"), ("reason", "reason"),
                      ("context", "context"), ("evidence", "evidence")):
        if key in params and params.get(key) is not None:
            fields[name] = str(params.get(key))
    if "refs" in params:
        fields["item_keys"] = _keys_from_refs(params.get("refs"))
    if "tags" in params:
        fields["tags"] = [str(t) for t in (params.get("tags") or [])]

    s = _searcher()
    try:
        if exp_id > 0:
            EXP.update_experience(s, exp_id, **fields)
            return {"id": exp_id, "created": False}
        source = str(params.get("source") or "user").strip() or "user"
        new_id = EXP.add_experience(s, **fields, source=source)
        return {"id": int(new_id), "created": True}
    except ValueError as exc:      # 领域校验（outcome/source 不合法…）→ 可控失败
        raise RpcError(str(exc)) from exc
    finally:
        s.close()


def _m_experience_delete(params: dict) -> dict:
    """删除若干条经验 → `{deleted, rolled_back_weights}`。

    `rolled_back_weights` = 这次被回滚权重的**不同文献**篇数（不是语句条数）——
    页面要显示的是"几篇文献的权重被扣回去了"。
    """
    ids = params.get("ids")
    if isinstance(ids, (str, int)):
        ids = [ids]
    if not isinstance(ids, (list, tuple)) or not ids:
        raise RpcError("需要 ids 数组（要删的经验编号）")
    want: list = []
    for i in ids:
        v = _as_int(i, 0)
        if v > 0 and v not in want:
            want.append(v)
    if not want:
        raise RpcError("ids 里没有有效的编号")

    import experience as EXP
    s = _searcher()
    try:
        touched: list = []
        for i in want:
            row = EXP.get_experience(s, i)
            if not row:
                continue
            for k in EXP.loads(row.get("item_keys"), []):
                if k and k not in touched:
                    touched.append(k)
        # ⚠ 必须**批量**删（delete_experiences）：逐条删会边删边重排编号，
        #   下一条要删的 id 已经指到别的行上了（面板注释里记过这个坑）。
        gone = EXP.delete_experiences(s, want)
    finally:
        s.close()
    return {"deleted": len(gone), "rolled_back_weights": len(touched)}


def _m_pending_mark(params: dict) -> dict:
    """标记待确认的经验草稿。参数 `{ids:[…], status:"adopted"|"dropped"}`。

    `adopted`（采纳）**不只是改个状态**：要把这条草稿真正写进经验库、并计入
    关联文献的权重 —— 入库动作与 Tk 面板 `do_exp_approve` 完全同路
    （`experience.add_experience`，它自己会在同一事务里写经验行 + 加权重）。
    页面不许写业务，所以这一半必须由引擎做。

    ⚠ 状态词有两套：契约给页面用 `adopted/dropped`，而 `pending.jsonl` 里
      存的是**既有词** `approved/rejected`（`offline/learn.py mark` 与 Tk 面板
      都写这两个词）。这里做一次翻译，不动文件里的词表 —— 否则同一份文件
      会出现四种状态词，`learn.py show` 一过滤就漏。
    """
    status = str(params.get("status") or "").strip().lower()
    if status not in ("adopted", "dropped"):
        raise RpcError('status 只能是 "adopted"（采纳）或 "dropped"（丢弃）')
    ids = params.get("ids")
    if isinstance(ids, (str, int)):
        ids = [ids]
    if not isinstance(ids, (list, tuple)) or not ids:
        raise RpcError("需要 ids 数组（待确认条目的编号）")
    want = {str(i) for i in ids if str(i).strip()}

    import learn as L
    rows = L._read_pending()
    hit = [r for r in rows
           if str(r.get("id")) in want and r.get("status") == "pending"]

    adopted = 0
    if status == "adopted" and hit:
        import experience as EXP
        s = _searcher()
        try:
            for r in hit:
                try:
                    EXP.add_experience(
                        s, asked=str(r.get("asked") or ""),
                        outcome=str(r.get("outcome") or "unknown"),
                        method=str(r.get("method") or ""),
                        context=str(r.get("context") or ""),
                        reason=str(r.get("reason") or ""),
                        evidence=str(r.get("evidence") or ""),
                        item_keys=r.get("items") or [], source="llm",
                        session=str(r.get("session") or ""))
                    adopted += 1
                except ValueError as exc:
                    # 单条不合格（例如小模型给的 outcome 不合法）→ 跳过它、
                    # 不弄死整批；它仍留在 pending 里等人工处理。
                    log(f"采纳待确认经验失败（跳过）：{exc}")
        finally:
            s.close()

    marked = 0
    for r in rows:
        if str(r.get("id")) in want and r.get("status") == "pending":
            r["status"] = "approved" if status == "adopted" else "rejected"
            marked += 1
    if marked:
        L._write_pending(rows)
    return {"marked": marked, "adopted": adopted}


# ---------------------------------------------------------------- 会话 / 从对话补经验

def _m_sessions_list(params: dict) -> dict:
    """DSH 会话清单（经验库页那个「选择会话…」弹窗）→ `{rows}`。

    口径照 Tk 面板 `tools/panels/exp_editor.py（已退役 2026-10-14）::SessionPicker._load`（那是原实现）：
      · 每个会话只取**最新**的那个 `session.v*.jsonl.zstd`；
      · 标题从 `~/.dsh/storages/session_projcache/sessions/<id>.json` 里读；
      · 「扫到第几条」来自 `learn.py` 的位置文件（`learn.load_positions`）。
    路径全都走 `schemas`（`DSH_SESSIONS`），不在这里写死 `~/.dsh`。
    """
    import glob
    import json as _json
    import schemas as S

    files = glob.glob(os.path.join(S.DSH_SESSIONS, "**",
                                  "session.v*.jsonl.zstd"), recursive=True)
    seen: dict = {}          # sid → (mtime, path, size)
    for path in files:
        sid = os.path.basename(os.path.dirname(path))
        try:
            mtime, size = os.path.getmtime(path), os.path.getsize(path)
        except OSError:
            continue
        cur = seen.get(sid)
        if cur is None or mtime > cur[0]:
            seen[sid] = (mtime, path, size)

    try:
        import learn as L
        scanned = L.load_positions()
    except Exception as exc:      # noqa: BLE001 —— 位置文件读不到就当"没扫过"
        log(f"读 learn 位置文件失败：{exc}")
        scanned = {}

    cache = os.path.join(os.path.expanduser("~"), ".dsh", "storages",
                         "session_projcache", "sessions")
    rows = []
    for sid, (mtime, path, size) in sorted(seen.items(), key=lambda x: -x[1][0]):
        title = ""
        try:
            with open(os.path.join(cache, f"{sid}.json"),
                      encoding="utf-8") as fh:
                rec = _json.load(fh)
            title = str(((rec.get("record") or {}).get("rows") or {})
                        .get("title", {}).get("val") or "")
        except Exception:      # noqa: BLE001 —— 没有缓存文件就退回用 id 当标题
            title = ""
        rows.append({"id": sid, "title": title,
                     "when": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                     "size_kb": size // 1024,
                     "scanned": sid in scanned,
                     "scanned_n": int(scanned.get(sid) or 0)})
    return {"rows": rows, "total": len(rows)}


def _run_learn_scan(sessions: list, progress=None) -> dict:
    """按会话跑 `offline/learn.py scan --session <sid>`（job 线程里执行）。

    为什么是**子进程**而不是 import 进来调：Tk 面板本来就是起
    `python offline/learn.py scan --session <sid>` 这个进程（见
    `tab_experience.py::do_pick_sessions`）。这里照做 —— 业务实现只有 learn.py
    一份，而且扫描会调很多次小模型，放子进程也不占住引擎线程。
    逐行把它的输出当进度喂给 job.log（页面的滚动文字就有东西看了）。
    """
    import subprocess

    py = sys.executable
    script = os.path.join(ROOT, "offline", "learn.py")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    flags = 0x08000000 if os.name == "nt" else 0       # 别弹黑窗口
    results = []
    for i, sid in enumerate(sessions, 1):
        if progress:
            progress(f"[{i}/{len(sessions)}] 扫描会话 {sid}…")
        try:
            cp = subprocess.Popen(
                [py, "-X", "utf8", script, "scan", "--session", sid],
                stdin=subprocess.DEVNULL,     # ⚠ 别让子进程继承 stdin 那条管道
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=ROOT, env=env, creationflags=flags,
                text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            results.append({"session": sid, "ok": False, "error": str(exc)})
            if progress:
                progress(f"[XX] 起不了扫描进程：{exc}")
            continue
        for line in (cp.stdout or []):
            line = line.rstrip()
            if line and progress:
                progress(line)
        rc = cp.wait()
        results.append({"session": sid, "ok": rc == 0, "returncode": rc})
    ok_n = sum(1 for r in results if r.get("ok"))
    if progress:
        progress(f"扫描结束：{ok_n}/{len(results)} 个会话成功")
    # 顺手报一下"现在还欠人确认多少条"，页面扫完能直接显示
    pending_n = 0
    try:
        import learn as L
        pending_n = sum(1 for r in L._read_pending()
                        if r.get("status") == "pending")
    except Exception:      # noqa: BLE001
        pass
    return {"ok": True, "sessions": results, "pending": pending_n}


def _m_learn_scan(params: dict) -> dict:
    """从勾选的 DSH 会话里抽经验（长任务）→ `{job:{id}}`。

    结果在 `GET /jobs/<id>` 的 `job.result` 里：`{ok, sessions:[{session,ok,…}],
    pending}`（`pending` = 扫完后待确认清单的条数）。
    """
    sessions = params.get("sessions")
    if isinstance(sessions, str):
        sessions = [sessions]
    if not isinstance(sessions, (list, tuple)) or not sessions:
        raise RpcError("需要 sessions 数组（要扫的会话 id）")
    picked = [str(s or "").strip() for s in sessions if str(s or "").strip()]
    if not picked:
        raise RpcError("sessions 里没有有效的会话 id")
    # ⚠ 会话 id 会拼进命令行参数：只放行"路径安全"的字符，别让参数注入
    bad = [s for s in picked
           if not all(c.isalnum() or c in "._:-" for c in s)]
    if bad:
        raise RpcError("会话 id 只允许字母数字与 . _ : - ：" + "、".join(bad[:3]))

    job_id = LS.start_job("从会话记录补经验", _run_learn_scan, picked, progress=True)
    rid = _rid()
    emit({"id": rid, "event": "progress",
          "data": {"job": job_id, "percent": None,
                   "message": f"已派发扫描任务 {job_id}（{len(picked)} 个会话）"}})
    return {"job": {"id": job_id}, "id": job_id, "job_id": job_id,
            "state": "running"}


# ---------------------------------------------------------------- 分节纲要

def _outline_rows() -> list:
    """全库纲要清单（items + meta.ai_outline + views 文件）的**原始行**。

    口径照 Tk 面板 `tab_outline.py::refresh_outline_list`（也照 localserver 里
    那套 `_outline_meta_counts` / `_outline_file_keys` 的判据）：
      · sections 按 **meta** 数（读端读的就是它），不是按 views 里的文件；
      · has_file  是人读的那份 `views/<key>.outline.md` 在不在；
      · orphan    = 有文件、meta 里却没有纲要（条目已删或留了旧文件）。
    字段名用页面的 `has_file`（契约），不是面板内部的 `file`。

    2026-10-13 加 `collections`（该文献所属的 Zotero 合集名数组）：分节纲要页要给
    每行显示分类、并按分类筛选。取数**复用 `items.collections`**（与 `papers.list`
    同一列、同一读法；`Searcher.collections` 就是把它聚合出来的），不另写业务。
    """
    import json as _json
    import schemas as S

    def _collections(raw) -> list:
        """items.collections 是 JSON 文本；脏值（None / 非数组）一律当空数组。"""
        try:
            v = _json.loads(raw or "[]")
        except (TypeError, ValueError):
            v = []
        return [str(c) for c in v] if isinstance(v, list) else []

    counts: dict = {}          # key → (节数, 生成时间)
    s = _searcher()
    try:
        for r in s.read("SELECT k, v FROM meta WHERE k LIKE ?",
                        (LS._OUTLINE_META_PREFIX + "%",)):
            k = str(r["k"])[len(LS._OUTLINE_META_PREFIX):]
            try:
                data = _json.loads(r["v"] or "{}")
            except (TypeError, ValueError):
                data = {}
            if isinstance(data, dict):
                counts[k] = (len(data.get("sections") or []), data.get("at") or "")
            else:
                counts[k] = (0, "")
        items = [(r["key"], r["title"] or "", r["year"], _collections(r["collections"]))
                 for r in s.read("SELECT key, title, year, collections FROM items "
                                 "ORDER BY year DESC, key")]
    finally:
        s.close()
    files = LS._outline_file_keys()

    rows: list = []
    seen: set = set()
    for key, title, year, cols in items:
        n, at = counts.get(key, (0, ""))
        has_file = key in files
        rows.append({"key": key, "title": title, "year": str(year or ""),
                     "sections": int(n or 0), "has_file": has_file,
                     "orphan": bool(has_file and not n), "built_at": at,
                     "collections": cols})
        seen.add(key)
    # 文件比 items 还多出来的（条目已不在索引里）也要列，别静默藏起来
    for key in sorted(files):
        if key not in seen and not counts.get(key, (0, ""))[0]:
            rows.append({"key": key, "title": "（不在索引里的条目）", "year": "",
                         "sections": 0, "has_file": True, "orphan": True,
                         "built_at": "", "collections": []})
    return rows


def _m_outline_list(params: dict) -> dict:
    """纲要清单。参数 `{filter:"all"|"missing"|"orphan", collection:"<分类名>"}`。

    `missing` = 有纲要、却缺渲染文件（页面的「只补缺失」处理的就是这批）；
    `orphan`  = 有渲染文件、索引库里却没有纲要。
    `collection` 空 = 全部分类；否则只留**属于该 Zotero 分类**的条目（精确匹配
    `items.collections` 里的一个名字）。两个条件是**正交**的：先按 filter 筛，
    再按 collection 筛，交集就是结果。
    """
    flt = str(params.get("filter") or "all").strip().lower()
    if flt not in ("all", "missing", "orphan"):
        raise RpcError('filter 只能是 "all" / "missing" / "orphan"')
    coll = str(params.get("collection") or "").strip()
    rows = _outline_rows()
    if flt == "missing":
        rows = [r for r in rows if r["sections"] and not r["has_file"]]
    elif flt == "orphan":
        rows = [r for r in rows if r["orphan"]]
    if coll:
        rows = [r for r in rows if coll in (r.get("collections") or [])]
    return {"rows": rows, "total": len(rows), "filter": flt, "collection": coll}


def _m_outline_render(params: dict) -> dict:
    """补齐 `views/<key>.outline.md`（不调模型）→ `{rendered, skipped}`。

    复用 `offline/kbviews.py::write_levels`（面板「补渲染文件」走的就是它，
    它是「meta → 文件」那条路）。**先自己判定"这篇有没有纲要"**：
    write_levels 对没有纲要的 key 会跳过 outline，但会写别的级别文件；
    对不存在的 key 更会写一堆"知识库里没有这篇"的视图文件 —— 那是噪音，
    所以不满足条件的一律进 `skipped`，一个文件都不碰。
    """
    keys = params.get("keys")
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)) or not keys:
        raise RpcError("需要 keys 数组（要补渲染的条目 key）")
    keys = [str(k or "").strip() for k in keys if str(k or "").strip()]

    import digest as D
    import kbviews as KV
    rendered = 0
    skipped: list = []
    s = _searcher()
    try:
        for key in keys:
            if not s.read_one("SELECT key FROM items WHERE key = ?", (key,)):
                skipped.append({"key": key, "reason": "索引库里没有这篇"})
                continue
            if not D.load_outline(key, s).get("sections"):
                skipped.append({"key": key, "reason": "这篇还没有纲要（先「生成」）"})
                continue
            try:
                KV.write_levels(key, s)
            except Exception as exc:      # noqa: BLE001 —— 单篇失败不影响别的
                skipped.append({"key": key, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            rendered += 1
    finally:
        s.close()
    return {"rendered": rendered, "skipped": skipped}


def _m_outline_delete(params: dict) -> dict:
    """删掉一篇的分节纲要 → `{deleted}`（索引库 meta + views 两份文件 + 档案概览）。

    与面板「删除」同一套动作：删 `meta.ai_outline:<key>`、删
    `views/<key>.outline.md` 与它的 `.html` 孪生、再让 `digest.sync_card`
    把 `papers/<KEY>.md` 里那段概览撤掉。**正文/切片/向量/经验都不动。**
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    import schemas as S
    hit = False
    s = _searcher()
    try:
        n = s.write("DELETE FROM meta WHERE k = ?", (LS._OUTLINE_META_PREFIX + key,))
        hit = bool(n and n > 0)
    finally:
        s.close()
    for suffix in (".outline.md", ".outline.html"):
        p = os.path.join(S.VIEWS_DIR, key + suffix)
        try:
            if os.path.exists(p):
                os.remove(p)
                hit = True
        except OSError as exc:
            log(f"删纲要文件失败 {p}：{exc}")      # 删不掉不算整体失败
    try:
        import digest as D
        D.sync_card(key)
    except Exception as exc:      # noqa: BLE001 —— 概览是派生物
        log(f"撤回 papers 里的纲要概览失败：{exc}")
    return {"deleted": hit}


# ---------------------------------------------------------------- 元数据（补全 / 类型）

def _meta_summary(res: dict) -> dict:
    """把 do_metafill_scan 的聚合字段收进 `summary`（契约要的一个键）。

    ⚠ 聚合字段**同时也留在顶层**：页面（admin-pages/meta.js）读的是顶层的
      `total/missing_total/scanned/elapsed/canceled`，契约写的是 `{rows, summary}`。
      两边都要成立，所以 summary 是"同一批字段的副本"，不是另一份口径。
    """
    keys = ("ok", "total", "missing_total", "scanned", "canceled", "elapsed",
            "model_requested", "model_used_items", "per_field", "high_total",
            "low_total", "writable_fields", "excluded_high", "busy", "error")
    return {k: res.get(k) for k in keys if k in res}


def _run_meta_fill(use_model: bool, model: str, limit: int, progress=None) -> dict:
    """job 线程里跑一次批量补全（日志里至少留下开头与结尾两行）。

    do_metafill_scan 自己不带 progress 形参，所以 per-篇 进度拿不到；
    这里只保证 job.log 有起止两行 —— 页面的滚动文字不至于一直是空的。
    """
    if progress:
        progress("开始扫描（只读，不写任何库）…")
    res = LS.do_metafill_scan(use_model=use_model, model=model, limit=limit)
    if progress:
        progress("完成：扫了 %s 篇，高置信 %s 条"
                 % (res.get("scanned"), res.get("high_total")))
    return res


def _m_meta_fill_scan(params: dict) -> dict:
    """元数据批量补全建议。参数 `{use_model?, limit?}`。

    · `use_model` 假（默认）→ **同步**返回 `{rows, summary}`：纯规则是全库秒级，
      不值得为它加一套 job 轮询（Tk 面板也是同步）。
    · `use_model` 真 → **长任务**：几十篇 × 每篇几秒 = 十几分钟，同步会把宿主的
      RPC 超时（3 分钟）撞穿。立刻回 `{job:{id}}`，结果结构仍是 `{rows, summary}`
      （页面 meta.js 的 submit 两种形状都接）。
    """
    use_model = bool(params.get("use_model"))
    model = str(params.get("model") or "")
    limit = _as_int(params.get("limit"), 0)

    if use_model:
        job_id = LS.start_job("批量补全元数据（本地模型）", _run_meta_fill,
                              use_model, model, limit, progress=True)
        rid = _rid()
        emit({"id": rid, "event": "progress",
              "data": {"job": job_id, "percent": None,
                       "message": f"已派发批量补全任务 {job_id}（本地模型）"}})
        return {"job": {"id": job_id}, "id": job_id, "job_id": job_id,
                "state": "running"}

    res = dict(LS.do_metafill_scan(use_model=False, model=model, limit=limit))
    if res.get("ok") is False:
        raise RpcError(str(res.get("error") or "批量补全失败"))
    res["summary"] = _meta_summary(res)
    return res


def _m_meta_fill_cancel(params: dict) -> dict:
    """取消正在跑的批量补全 → `{cancelled}`（合作式：下一篇之前停下）。"""
    res = LS.do_metafill_scan_cancel() or {}
    # ⚠ 拼写：HTTP 那边回的是 `canceled`（一个 l），契约要 `cancelled`（两个 l）
    return {"cancelled": bool(res.get("canceled")),
            "note": str(res.get("hint") or "")}


def _typefix_issue(r: dict) -> str:
    """类型/标题污染那一行「问题」列的文本（信号优先，其次说明）。"""
    sig = [str(x) for x in (r.get("signals") or []) if str(x).strip()]
    if sig:
        return "；".join(sig)
    notes = [str(x) for x in (r.get("notes") or []) if str(x).strip()]
    return notes[0] if notes else ""


def _typefix_suggest(r: dict) -> str:
    """「建议」列：把 fixes 翻成"标题：现在 → 改成"。"""
    parts = []
    for f in (r.get("fixes") or []):
        if not isinstance(f, dict):
            continue
        kind = "标题" if f.get("kind") == "title" else "类型"
        val = str(f.get("value") or "")
        if kind == "类型":
            val = LS.TYPE_CN.get(val, val)      # 类型值翻中文（与面板同一份表）
        cur = str(f.get("current") or "")
        parts.append(f"{kind}：{cur} → {val}")
    if r.get("will_drop"):
        parts.append("改类型会丢字段：" + "、".join(str(x) for x in r["will_drop"]))
    return "；".join(parts)


def _m_meta_type_scan(params: dict) -> dict:
    """全库找「网页存了、后来挂 PDF」的条目（只读）→ `{rows}`。

    契约的行形状是 `{key,title,issue,suggest}`；`do_typefix_scan` 给的是
    `signals/notes/fixes` —— 这里只翻译字段，**不改**里面的判据。
    """
    use_network = bool(params.get("use_network"))
    res = LS.do_typefix_scan(use_network=use_network)
    if res.get("ok") is False:
        raise RpcError(str(res.get("error") or "类型/标题检查失败"))
    rows = []
    for r in (res.get("rows") or []):
        rows.append({"key": r.get("key", ""), "title": r.get("title", ""),
                     "issue": _typefix_issue(r), "suggest": _typefix_suggest(r),
                     # 页面弹窗要用类型名做悬停说明，一并带上（多余字段无害）
                     "item_type": r.get("item_type", ""),
                     "suspect": bool(r.get("suspect"))})
    return {"ok": True, "rows": rows,
            "checked": res.get("checked", len(res.get("rows") or [])),
            "suspect_total": res.get("suspect_total", 0),
            "elapsed": res.get("elapsed"), "network": bool(use_network)}


# ---------------------------------------------------------------- 运行环境 / 模型

def _env_items(rep: dict) -> list:
    """四个位置 → `[{key,name,path,ok,source,optional?}]`（env 页的表格）。

    只列面板那四项（项目目录 / Python / Ollama / MinerU）—— 与
    `tools/panels/tab_env.py（已退役 2026-10-14）` 的四行一一对应；figures / kb_dir 这些在
    `sources` 里照旧可见，不塞进这张表（它们不是"位置"）。
    """
    import schemas as S
    src = rep.get("sources") or {}
    root = rep.get("project_root") or {}
    py = rep.get("python") or {}
    ol = rep.get("ollama") or {}
    mu_path = ""
    mu_ok = False
    try:
        mu_path = S.resolve_mineru()
        mu_ok = bool(mu_path) and os.path.exists(mu_path)
    except Exception as exc:      # noqa: BLE001 —— 探测失败不该让整页打不开
        log(f"探测 MinerU 失败：{exc}")
    rows = [
        {"key": "project_root", "path": root.get("path") or "",
         "ok": bool(root.get("exists")), "source": root.get("source") or ""},
        {"key": "python", "path": py.get("path") or "",
         "ok": bool(py.get("exists")), "source": py.get("source") or ""},
        # ⚠ 状态列的语义是"文件在不在"（页面写的就是"存在/找不到"），
        #   不是"服务通不通" —— 后者由 llm.models 真去问一次。
        {"key": "ollama", "path": ol.get("path") or "",
         "ok": bool(ol.get("exists")), "source": ol.get("source") or "",
         "optional": True},
        {"key": "mineru", "path": mu_path, "ok": mu_ok,
         "source": (src.get("mineru") or {}).get("source", ""), "optional": True},
    ]
    labels = getattr(S, "ENV_SOURCE_LABELS", {})
    for r in rows:
        r["name"] = labels.get(r["key"], r["key"])
    return rows


def _env_rejected(sources: dict) -> list:
    """把"高优先档填了但被否"摊平成一个数组（页面按 `{key,source,value,why}` 读）。"""
    out = []
    for key, one in (sources or {}).items():
        for b in ((one or {}).get("rejected") or []):
            if not isinstance(b, dict):
                continue
            out.append({"key": key, "source": b.get("source", ""),
                        "value": b.get("value", ""), "why": b.get("why", "")})
    return out


def _m_env_check(params: dict) -> dict:
    """运行环境诊断（四个位置 + 来源 + 被否掉的高优先档）。

    业务直接复用 `localserver.env_report()`（那是"该改哪一个框"的唯一实现）。
    """
    rep = LS.env_report()
    return {"ok": bool(rep.get("ok")), "items": _env_items(rep),
            "sources": rep.get("sources") or {},
            "rejected": _env_rejected(rep.get("sources") or {}),
            # 多给两句上下文（面板里点开就看得到），页面不用也无害
            "env_file": rep.get("env_file") or {},
            "location_file": rep.get("location_file") or ""}


def _m_env_save(params: dict) -> dict:
    """写运行环境配置 → `{saved, check}`。

    ⚠ 语义照 `schemas.save_env_config`：**非空值**要校验存在性（不合格报错、
      不写），**空串** = 这一项交回自动探测（清掉用户设置）。
      `figures` 不在那三个位置里，它是 `kb-location.json` 的一个开关，
      单独走 `write_location_config`（合并写，不会碰 env 段）。
    """
    import schemas as S
    given = {k: str(params.get(k) or "")
             for k in ("project_root", "python", "ollama", "mineru")
             if k in params}
    has_fig = "figures" in params
    fig = params.get("figures")
    if not given and not has_fig:
        raise RpcError("没有要写入的键（project_root / python / ollama / mineru / figures）")
    try:
        if given:
            S.save_env_config(given)
        if has_fig:
            # 字符串 "0"/"false" 也要按关处理（插件设置里可能传字符串）
            if isinstance(fig, str):
                val = fig.strip().lower() not in ("0", "false", "no", "off", "")
            else:
                val = bool(fig)
            S.write_location_config(figures=val)
    except ValueError as exc:
        raise RpcError(str(exc)) from exc
    except OSError as exc:
        raise RpcError(f"写入配置失败：{exc}") from exc
    # W3 线2.5（2026-10-14）：**把诊断原文一起回**（`report`）。
    # 为什么：老 HTTP `POST /env-config` 的消费方（插件设置面板 settings.js）
    # 保存后直接读 `res.report` 渲染报告；引擎原来只回 `{saved, check}`，
    # 缺这一项会让设置面板"保存后报告变空"（只能再补一次 env-check，
    # 而 W3 后期那条 HTTP 路会没）。
    # 多跑一次 `env_report()`：不在热路径上（用户点一次保存而已），
    # 换来不用把 `_m_env_check` 拆成"算一次、用两次"。
    return {"saved": True, "check": _m_env_check({}), "report": LS.env_report()}


def _m_llm_config(params: dict) -> dict:
    """当前模型配置（**不含 api_key 明文**）→ 契约要的扁平形状。

    复用 `localserver._safe_llm_config()`（它负责"只露 has_key / key_tail"），
    这里只把嵌套里的字段摊平。没配的给空串，页面不用判 undefined。

    2026-10-13 多给一个 `services`（`[{key, base_url}]`，来自
    `judge.KNOWN_BASE_URLS`）：页面要按**地址**认出"这是哪一家"、好把
    「外部 API（OpenAI 兼容）」与「自定义（自建服务）」分开（用户要求：别把
    显示名当模型名用）。地址的唯一事实来源还是 judge 那张表，页面不另存一份。
    """
    import judge as J

    cfg = LS._safe_llm_config() or {}
    services = [{"key": str(k), "base_url": str(v)}
                for k, v in sorted((J.KNOWN_BASE_URLS or {}).items())]
    return {"provider": cfg.get("provider") or "",
            "model": cfg.get("model") or "",
            "base_url": cfg.get("base_url") or "",
            "has_key": bool(cfg.get("has_key")),
            "key_tail": cfg.get("key_tail") or "",
            "ollama_host": cfg.get("ollama_host") or "",
            "services": services}


def _m_llm_save(params: dict) -> dict:
    """保存模型配置 → `{saved}`（只覆盖传进来的键）。

    ⚠ `api_key` **没传就不动**（页面表单留空 = 不改，别把已存的 Key 抹掉）；
      显式传空串 = 清除（那是明确动作）。写入只有一份实现：
      `offline/judge.py::save_llm_config`。

    ⚠ 2026-10-13 补的**白名单与形状校验**（用户接外部 API 时踩到的）：
      · `provider` 只认 ollama / openai（openai-compatible / api 这类别名会被
        **归一成 "openai" 再存**）。为什么要挡：`judge.generate()` 只认这两类，
        存进去一个 "deepseek" 时它会被**当成 ollama** 走本机 —— 用户以为切到了
        DeepSeek、实际在问本地模型，这种"静默走错后端"最难查。
      · `base_url` 非空时必须以 http(s):// 开头（把 `api.deepseek.com` 这种
        漏了协议的当场挡下，而不是等到调用时才报"连不上"）。
      校验只挡**明确错的**，不做"这个模型 id 存不存在"那种猜（引擎查不到）。
    """
    import judge as J
    kw = {k: params.get(k) for k in
          ("provider", "base_url", "api_key", "model", "ollama_host")
          if params.get(k) is not None}
    if not kw:
        raise RpcError("没有要写入的键（provider / model / base_url / api_key / ollama_host）")
    if kw.get("provider") is not None:
        p = str(kw["provider"]).strip().lower()
        if p in ("openai-compatible", "openai_compatible", "api"):
            p = "openai"
        if p not in ("ollama", "openai"):
            raise RpcError(
                f'provider 只能是 "ollama"（本机）或 "openai"（任何 OpenAI 兼容的外部 '
                f'API），收到的是 "{kw["provider"]}"')
        kw["provider"] = p
    if kw.get("base_url") is not None:
        b = str(kw["base_url"]).strip()
        if b and not (b.startswith("http://") or b.startswith("https://")):
            raise RpcError(f'base_url 要以 http:// 或 https:// 开头（收到 "{kw["base_url"]}"）')
        kw["base_url"] = b
    if kw.get("model") is not None:
        kw["model"] = str(kw["model"]).strip()
    J.save_llm_config(**kw)
    return {"saved": True}


def _m_llm_models(params: dict) -> dict:
    """本机 Ollama 有哪些模型（复用 localserver.do_models，HTTP 与引擎同一份）。"""
    return LS.do_models()


# ---------------------------------------------------------------- 模型目录（三层）
#
# 用户 2026-10-13 的困惑："想把大模型的 api 接上去，怎么选具体类型？v4pro 还是
# v4.1flash？" —— 根因是**没给目录**，让人去猜模型名。成熟软件（含 DSH）的做法都是
# 三层兜底：**动态探测 → 内置候选 → 手填**（设计依据见开发说明）。
#
# 内置候选表的**出处**（可核实，2026-10-13 用 tools/from-repo/asar_read.py 从
# 已装的 app.asar 里读出来的；不是凭记忆写的）：
#   dsh/node_modules/@earendil-works/pi-ai/dist/providers/data/deepseek.json
#     → openai-completions → deepseek-flash / deepseek-v4-pro
#       （name / contextWindow=1000000 / maxTokens=384000 逐字抄）
#   dsh/node_modules/@earendil-works/pi-ai/dist/providers/data/openai.json
#     → openai-responses → 取常用的几条（id/name/contextWindow/maxTokens 逐字抄）
#   同目录另有 41 个 provider 文件与 .manifest.json：
#     schemaVersion=3、generatedAt=2026-09-22T19:31:44.346Z、
#     structureHash=c6acf0a192036dd24f23414d3064d4ebbec194485398fee5b9def0b4d86a0095、
#     deepseek.json 的 sha256=549a7ddbdcfd4c59272f1ea9c7d6d9a70bbbfccad3df3a75fb16d6918ac54d0d
# ⚠ 只抄**那些文件里真有**的条目；拿不准就留空、让页面写"按服务商文档填"（宁空勿猜）。
# ⚠ 地址**不抄**在这张表里：唯一事实来源是 judge.KNOWN_BASE_URLS（与插件侧有
#   一致性检查），这里只存模型条目，免得两处地址将来漂移。
_BUILTIN_MODELS: dict = {
    "deepseek": [
        {"id": "deepseek-flash", "name": "DeepSeek V4.1 Flash",
         "context": 1000000, "maxtokens": 384000},
        {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro",
         "context": 1000000, "maxtokens": 384000},
    ],
    "openai": [
        {"id": "gpt-5.2", "name": "GPT-5.2", "context": 400000, "maxtokens": 128000},
        {"id": "gpt-5.2-mini", "name": "GPT-5.2 mini", "context": 400000,
         "maxtokens": 128000},
        {"id": "gpt-4o", "name": "GPT-4o", "context": 128000, "maxtokens": 16384},
        {"id": "gpt-4o-mini", "name": "GPT-4o mini", "context": 128000,
         "maxtokens": 16384},
        {"id": "o4-mini", "name": "o4-mini", "context": 200000, "maxtokens": 100000},
    ],
}

# 探测结果的进程内缓存：`(provider, base_url)` → 上一次的结果（含时间）。
# 为什么要缓存：**打开下拉不该打网络**（用户 2026-10-13 的要求）。页面只在
# 进页面 / 切服务商 / 点「刷新模型列表」时来问，`refresh=true` 绕开缓存。
_MODEL_CACHE: dict = {}
_MODEL_CACHE_TTL = 600.0          # 10 分钟


def _model_int(*vals):
    """从若干候选字段里取第一个正整数（拿不到返回 None —— **宁可空也别猜**）。"""
    for v in vals:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


def _context_of(it: dict):
    """上下文长度：各家字段名不一样，别名归一化（DSH 同一套口径）。"""
    limit = it.get("limit") if isinstance(it.get("limit"), dict) else {}
    return _model_int(it.get("contextWindow"), it.get("context_window"),
                      it.get("context_length"), it.get("max_input_tokens"),
                      limit.get("context"), it.get("context"))


def _maxtokens_of(it: dict):
    """最大输出：同上（拿不到就 None）。"""
    limit = it.get("limit") if isinstance(it.get("limit"), dict) else {}
    return _model_int(it.get("maxTokens"), it.get("max_tokens"),
                      it.get("max_output_tokens"), it.get("max_completion_tokens"),
                      limit.get("output"), it.get("maxOutputTokens"))


def _discover_ollama(host: str, timeout: float) -> dict:
    """本机 Ollama 的模型列表：**优先 `/api/tags`**（比 /v1/models 多给
    parameter_size / quantization_level）。读不到就回落到 judge 那份实现。"""
    import urllib.error
    import urllib.request

    import judge as J

    base = str(host or J.OLLAMA_HOST or "").rstrip("/")
    url = base + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024)          # 上限 4 MiB
        data = json.loads(raw.decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        ok, names = J.ollama_models(host)
        return {"ok": bool(ok), "status": 0,
                "models": [{"id": n, "name": "", "note": "",
                            "context": None, "maxtokens": None} for n in names],
                "error": "" if ok else f"连不上 {url}：{exc}"}
    rows = []
    for m in (data.get("models") or []):
        if not isinstance(m, dict):
            continue                                   # 坏条目跳过，不让整次失败
        name = str(m.get("name") or m.get("model") or "").strip()
        if not name:
            continue
        det = m.get("details") if isinstance(m.get("details"), dict) else {}
        bits = [str(det.get(k)) for k in ("parameter_size", "quantization_level")
                if det.get(k)]
        rows.append({"id": name, "name": str(det.get("family") or "").strip(),
                     "note": " · ".join(bits),
                     "size_bytes": int(m.get("size") or 0),
                     "context": None, "maxtokens": None})
    return {"ok": True, "status": 200, "models": rows, "error": ""}


def _discover_openai(base_url: str, api_key: str, timeout: float) -> dict:
    """`GET {base_url}/models`（OpenAI 兼容）→ `{ok, status, models, error}`。

    · **两种返回体都认**：`{data:[{id,name,…}]}` 与 `{models:{<id>:{…}}}`（后者
      拿对象键当 id）——DSH 的 `readListing` 就是这么做的；
    · 容量字段走别名归一化（见 `_context_of` / `_maxtokens_of`）；
    · **最多读 4 MiB**、单条坏数据跳过（不让一个脏条目把整次探测打掉）。
    """
    import urllib.error
    import urllib.request

    url = str(base_url or "").rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:                              # noqa: BLE001
            body = ""
        code = int(getattr(exc, "code", 0) or 0)
        return {"ok": False, "status": code, "models": [],
                "error": f"HTTP {code}: {body[:160]}" if body else f"HTTP {code}"}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "status": 0, "models": [],
                "error": f"连不上 {url}：{exc}"}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"ok": False, "status": status, "models": [],
                "error": f"返回体不是 JSON：{exc}"}
    items: list = []
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            items = [x for x in data["data"] if isinstance(x, dict)]
        elif isinstance(data.get("models"), dict):
            for mid, mv in data["models"].items():
                row = dict(mv) if isinstance(mv, dict) else {}
                row.setdefault("id", mid)              # 对象键就是 id
                items.append(row)
    rows = []
    for it in items:
        mid = str(it.get("id") or it.get("name") or "").strip()
        if not mid:
            continue
        rows.append({"id": mid, "name": str(it.get("name") or "").strip(),
                     "note": "", "context": _context_of(it),
                     "maxtokens": _maxtokens_of(it)})
    rows.sort(key=lambda r: r["id"])
    return {"ok": True, "status": status, "models": rows, "error": ""}


def _builtin_service(base_url: str, model: str) -> str:
    """认出内置候选表里的服务商 key（`deepseek` / `openai`…），认不出返回 ""。

    认法两种（都不新写规则，复用 judge 里已有的那张地址表）：
      · base_url 的 host 命中 `KNOWN_BASE_URLS` 里某家的 host；
      · 都不命中时用 `guess_base_url_from_model(model)` 的结果去对。
    """
    import judge as J

    known = J.KNOWN_BASE_URLS or {}
    low = str(base_url or "").lower()
    for key, url in known.items():
        host = str(url).split("//", 1)[-1].split("/", 1)[0].lower()
        if host and host in low:
            return key
    guessed = str(J.guess_base_url_from_model(str(model or "")) or "").rstrip("/")
    if guessed:
        for key, url in known.items():
            if str(url).rstrip("/") == guessed:
                return key
    return ""


def _m_llm_discover(params: dict) -> dict:
    """模型目录（三层兜底）→ `{ok, source, models:[…], …}`。

    参数：`{provider?, model?, base_url?, api_key?, ollama_host?, refresh?, timeout?}`。
    `source` 四档：`ollama`（本机 /api/tags）/ `remote`（外部 /models）/
    `builtin`（探测失败 → 内置候选，页面要写明"也可手填"）/ `none`（既没探到、
    也没有对应服务商的内置表 → 页面提示"按服务商文档填其模型 id"）。
    `refresh=true` 绕开进程内缓存（「刷新模型列表」按钮走这条路）。
    ⚠ 探测超时默认 12s（页面不该等宿主那 180s）；失败**不抛**，如实回 `ok:false`。
    """
    import judge as J

    cfg = J.load_llm_config()
    prov = str(params.get("provider") or cfg.get("provider") or "ollama").strip().lower()
    if prov in ("openai-compatible", "openai_compatible", "api"):
        prov = "openai"
    if prov not in ("ollama", "openai"):
        return {"ok": False, "provider": prov, "source": "none", "models": [],
                "error": f'provider 只能是 "ollama" 或 "openai"，收到 "{prov}"'}
    model = str(params.get("model") or cfg.get("model") or "").strip()
    base = str(params.get("base_url") or cfg.get("base_url") or "").strip()
    key = params.get("api_key")
    if key is None:
        key = cfg.get("api_key") or ""
    try:
        timeout = float(params.get("timeout") or 12.0)
    except (TypeError, ValueError):
        timeout = 12.0
    timeout = max(1.0, min(30.0, timeout))
    refresh = bool(params.get("refresh"))

    if prov == "ollama":
        base = str(params.get("ollama_host") or cfg.get("ollama_host") or J.OLLAMA_HOST)
    elif not base:
        base = J.resolve_base_url("openai", "", model)
        if not base:
            base = J.KNOWN_BASE_URLS.get(_builtin_service("", model) or "", "")

    ck = (prov, str(base or ""))
    now = time.time()
    hit = _MODEL_CACHE.get(ck)
    if hit and not refresh and (now - hit["at"]) < _MODEL_CACHE_TTL:
        out = dict(hit["data"])
        out["cached"] = True
        out["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hit["at"]))
        return out

    if prov == "ollama":
        res = _discover_ollama(base, timeout)
    else:
        res = _discover_openai(base, str(key or ""), timeout)

    source = "ollama" if prov == "ollama" else "remote"
    out = {"ok": bool(res.get("ok")), "provider": prov, "base_url": base,
           "source": source, "models": res.get("models") or [],
           "status": int(res.get("status") or 0), "error": str(res.get("error") or ""),
           "cached": False, "hint": ""}
    if not out["ok"] and prov == "openai":
        # 探测失败 → 内置候选（照抄 DSH 静态目录，见 _BUILTIN_MODELS 的注释）
        svc = _builtin_service(base, model)
        rows = [dict(r) for r in (_BUILTIN_MODELS.get(svc) or [])]
        for r in rows:
            r.setdefault("note", "内置候选")
        out["service"] = svc
        if rows:
            out["source"] = "builtin"
            out["models"] = rows
            out["hint"] = ("探测失败，以下为内置候选（照抄 DSH 的静态目录，可能滞后），"
                           "也可以直接手填模型名。")
        else:
            out["source"] = "none"
            out["hint"] = "探测失败，也没有这家的内置候选 —— 请按服务商文档填其模型 id。"
    elif not out["ok"]:
        out["hint"] = ("启动 Ollama 再刷新；没有模型就先 "
                       "`ollama pull qwen3:4b-instruct`")

    _MODEL_CACHE[ck] = {"at": now, "data": out}
    out = dict(out)
    out["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return out


def _m_llm_test(params: dict) -> dict:
    """「测试连接」→ 真发一次最小请求，把结果/错误**原样**带回来。

    口径照 Tk 面板 `tools/panels/tab_env.py（已退役 2026-10-14）::do_test`（同一件事不能在两个面板
    里各写一套）：
      · `provider=ollama` → 只查 `ollama list`（便宜，不发模型请求）；
      · `provider=openai` → 真调一次 `/chat/completions`（走 `judge.generate`），
        模型 id / 地址 / Key 有一个不对，就会把服务端或 urllib 那句话带回来。

    参数全可选（不传就用当前配置）：`{provider, model, base_url, api_key, timeout}`。
    ⚠ 失败**不抛**：回 `{ok:false, error, hint}` —— "api_key 不对或已过期 ——
      去服务商后台重新复制一个"这种原话比一行 RpcError 有用得多（页面直接显示）。
    """
    import judge as J

    cfg = J.load_llm_config()
    prov = str(params.get("provider") or cfg.get("provider") or "ollama").strip().lower()
    if prov in ("openai-compatible", "openai_compatible", "api"):
        prov = "openai"
    model = str(params.get("model") or cfg.get("model") or "").strip()
    base = str(params.get("base_url") or cfg.get("base_url") or "").strip()
    key = params.get("api_key")
    if key is None:
        key = cfg.get("api_key") or ""
    started = time.time()
    if prov not in ("ollama", "openai"):
        return {"ok": False, "provider": prov,
                "error": f'provider 只能是 "ollama" 或 "openai"，收到 "{prov}"'}

    if prov == "ollama":
        host = str(params.get("ollama_host") or cfg.get("ollama_host") or "")
        ok, names = J.ollama_models(host)
        out = {"ok": bool(ok), "provider": "ollama",
               "base_url": host or J.OLLAMA_HOST, "models": list(names),
               "model": model or (J.pick_model(host=host) if ok else ""),
               "ms": int((time.time() - started) * 1000)}
        if not ok:
            out["error"] = "连不上本机的 Ollama"
            out["hint"] = ("启动 Ollama 再试；没有模型就先 "
                           "`ollama pull qwen3:4b-instruct`")
        elif not names:
            out["ok"] = False
            out["error"] = "Ollama 在跑，但一个模型都没有"
            out["hint"] = "先拉一个：ollama pull qwen3:4b-instruct"
        elif model and model not in names:
            out["hint"] = (f'本机列表里没有 "{model}" —— 名字写错，'
                           f'或还没 `ollama pull {model}`')
        return out

    # ---- 外部 OpenAI 兼容 API
    if not model:
        return {"ok": False, "provider": "openai", "base_url": base,
                "error": "外部 API 必须填模型 id（不是产品名）",
                "hint": "例如 DeepSeek 官方（2026-10 文档）是 deepseek-flash / "
                        "deepseek-v4-pro；其它服务商照其文档填"}
    base2 = J.resolve_base_url("openai", base, model)
    try:
        wait = float(params.get("timeout") or 45.0)
    except (TypeError, ValueError):
        wait = 45.0
    res = J.generate("只回两个字：可用", model=model, provider="openai",
                     json_mode=False, timeout=wait, temperature=0.0,
                     base_url=base2, api_key=key)
    out = {"ok": bool(res.get("ok")), "provider": "openai", "base_url": base2,
           "model": res.get("model") or model,
           "backend": res.get("backend") or "openai",
           "ms": int((time.time() - started) * 1000)}
    if res.get("ok"):
        out["output"] = str(res.get("text") or "").strip()[:200]
    else:
        out["error"] = str(res.get("error") or "调用失败")
        # 页面要按状态码给"可操作的话"（401/403 → 查 Key；404 → 查模型名）。
        # judge 把码写在 error 串里（"HTTP 401：…"），这里抠出来当**结构化字段**。
        m = re.match(r"\s*HTTP\s+(\d{3})", out["error"])
        out["status"] = int(m.group(1)) if m else 0
        if res.get("hint"):
            out["hint"] = str(res["hint"])
    return out


def _m_service_restart(params: dict) -> dict:
    """「重启服务」**占位**：没有 8765 之后，引擎的生命周期归宿主半（插件）管。

    为什么如实回 `started:false` 而不是假装成功：引擎是 Zotero 的子进程、
    **不能自己重启自己**（重启自己 = 杀掉正在处理这次请求的进程）。真要做成
    "重开引擎"，那是宿主半的动作（关掉旧句柄 + 下次请求重新拉起），
    不是引擎能承诺的事。页面的原话照抄这一句，不装作重启了。
    """
    return {"started": False,
            "note": "HTTP 服务退场后由宿主半管理生命周期（引擎随 Zotero 起落，"
                    "不常驻、不自启）"}


# ---------------------------------------------------------------- 提示词

def _m_prompts_list(params: dict) -> dict:
    """提示词注册表 → `{rows}`（契约的 is_custom 是布尔，status() 给的是列表）。"""
    import prompts as PR
    rows = []
    for r in PR.status():
        rows.append({"id": r.get("id", ""), "title": r.get("title", ""),
                     "where": r.get("where", ""),
                     "placeholders": list(r.get("placeholders") or []),
                     "system": r.get("system", ""), "user": r.get("user", ""),
                     "is_custom": bool(r.get("custom"))})
    return {"rows": rows}


def _m_prompts_save(params: dict) -> dict:
    """保存 / 还原一条提示词 → `{saved, is_custom}`。

    · `{id, field:"system"|"user", text}` → 写覆盖（校验不过就 ok:false，
      校验在 `offline/prompts.py::validate`，不许在这里另写一套）；
    · `{id, action:"reset"}` → 删掉这一条的覆盖，回默认。
    """
    import prompts as PR
    pid = str(params.get("id") or "").strip()
    if not pid:
        raise RpcError("需要 id（哪一条提示词）")
    if str(params.get("action") or "").strip().lower() == "reset":
        PR.reset(pid)
        return {"saved": True, "is_custom": bool(PR.is_custom(pid))}
    field = str(params.get("field") or "").strip()
    if field not in PR.FIELDS:
        raise RpcError(f'field 只能是 {list(PR.FIELDS)}（或给 action="reset"）')
    text = params.get("text")
    try:
        PR.save(pid, field, "" if text is None else str(text))
    except ValueError as exc:
        raise RpcError(str(exc)) from exc
    return {"saved": True, "is_custom": bool(PR.is_custom(pid, field))}


def _m_prompts_probe(params: dict) -> dict:
    """拿固定样例试跑一条提示词 → `{ok, output, ms, error, …}`。

    ⚠ 这条**可能要几十秒**（真调模型）：不是失败，是慢。页面自己给"运行中"状态。
      引擎侧不转成 job —— 契约就是同步回 `{ok,output,ms}`。
    `text` 传了就试**没保存的那份草稿**（页面可以"先看效果再保存"），
    实现仍在 `prompts.probe`（那里唯一一份组装/解析逻辑）。
    """
    import prompts as PR
    pid = str(params.get("id") or "").strip()
    if not pid:
        raise RpcError("需要 id（哪一条提示词）")
    text = params.get("text")
    if text is not None and not isinstance(text, str):
        text = str(text)
    t0 = time.time()
    res = PR.probe(pid, user_text=text)
    return {"ok": bool(res.get("ok")), "output": res.get("output") or "",
            "ms": int((time.time() - t0) * 1000),
            "error": res.get("error") or "",
            # 额外给页面两条能用的信息（试跑最有用的是"JSON 解析过不过"）
            "want_json": bool(res.get("want_json")),
            "parsed": res.get("parsed") or ""}


# ================================================================ W2 批次 B
#
# 「分类建议 / PDF 解析 / 损坏查询 / 高级」四组页面要的方法（契约见开发说明，
# 冻结）。规矩与批次 A 一字不差：
#   · 列表一律 {rows:[…]}；
#   · 失败一律 ok:false + 中文原因（坏输入不许弄死引擎）；
#   · 长任务先回 {job:{id}}，之后 GET /jobs/<id> 轮询；
#   · **`output` 类返回是一份"原样文本"**（契约硬口径 1）—— 不解析成结构化
#     字段：解析必然随输出漂移，页面只把它放进等宽只读区。
#
# 业务同样**一律不重写**：能调 localserver 的模块级函数（do_set_weight /
# do_classify_one / start_job）就调；能调 offline/*.py 里的实现（mineru /
# check_chunks / repair_chunks / judge / schemas / searcher）就调；那些"在 Tk
# 面板里就是一个脚本"的动作（kb_admin.py / zotero_sync.py / maintain.py /
# sideload_plugin.py / zotero_upgrade.py / check_plugin.py）就**照样跑那个脚本**
# —— 只是改成在引擎自己的解释器里跑（见 `_run_script`）。
#
# ⚠ 为什么"跑脚本"用 runpy 而**不再**起子进程（2026-10-13）：
#   当时在本机的宿主上下文里实测**引擎起的子进程起不来** —— 探针让子进程第一件事
#   就写一个标记文件，8 秒后进程还在、标记却不存在（也就是一句 Python 代码都
#   没执行），而**同一条命令在命令行里 0.3 秒就跑完**。既然"再起一个 Python"
#   在那时不可靠，就让脚本在引擎自己的解释器里跑：`runpy.run_path(...,
#   run_name="__main__")` 走的正是 `python <脚本> <参数>` 那条 `__main__` 路径，
#   脚本代码一个字不改，输出也照样是一份原样文本（实测 907 字节，与子进程
#   逐字节同长）。
#   ⚠ 2026-10-13 晚补记：**那个根因已经查明并修掉了** —— 是主循环阻塞读 stdin
#     把整个进程锁住了（见文件开头「stdin 怎么读」），现在引擎起子进程是能的
#     （`learn.scan` 那条子进程实测跑完 rc=0）。runpy **保留**：它已经在跑、
#     与 `python xxx.py` 行为等价，还省掉每次起进程的开销，没有理由回退。
#   唯一的例外是 `scripts/sync-skill.ps1`（PowerShell，不是 Python），它只能走
#   子进程 —— 见 `_run_powershell` 的注释。


def _tool(name: str) -> str:
    """`<仓库根>/tools/<name>` 的绝对路径。"""
    return os.path.join(ROOT, "tools", name)


# 进程内跑脚本的串行锁（见 _run_script：它改的是全局的 stdout/stderr/argv）
_SCRIPT_LOCK = threading.Lock()


def _off(name: str) -> str:
    """`<仓库根>/offline/<name>` 的绝对路径。"""
    return os.path.join(ROOT, "offline", name)


class _CaptureIO:
    """把脚本的 stdout/stderr 收进内存的"假文件"。

    为什么不是 io.StringIO：长任务（mineru 批量解析）要**边跑边**把整行喂给
    `job.log`。这里在 write() 里按 `\\n` 切行、即时回调，页面轮询就有进度。
    只实现脚本真正会用到的那几个方法（write / flush / isatty / fileno）；
    `fileno()` 如实抛 OSError —— 让"以为能拿 fd"的代码当场失败，而不是静默
    写到别处去（比返回假 fd 安全）。
    """

    def __init__(self, on_line=None):
        self._parts: list = []
        self._pending = ""
        self._on_line = on_line

    def write(self, s) -> int:
        text = s if isinstance(s, str) else str(s)
        self._parts.append(text)
        if self._on_line and text:
            self._pending += text
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                if line.strip():
                    self._on_line(line.rstrip()[:400])
        return len(text)

    def flush(self) -> None:
        if self._on_line and self._pending.strip():
            self._on_line(self._pending.rstrip()[:400])
        self._pending = ""

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("这是一个被捕获的输出流，没有文件描述符")

    def text(self) -> str:
        self.flush()
        return "".join(self._parts)


def _run_script(script: str, argv=(), on_line=None) -> tuple:
    """在**本进程内**按 `python <script> <argv…>` 的方式跑一个既有脚本。

    返回 `(退出码, 原样文本)`（stdout 与 stderr 合并 —— 这些脚本的失败原因
    几乎都在 stderr，分开收页面就得看两处）。
    退出码取脚本 `sys.exit()` 的值；脚本抛异常时按 1 记，并把 traceback 写进
    文本尾部（页面在等宽区里就能看到为什么失败）。

    ⚠ 必须**串行**：进程内改的是全局的 `sys.stdout` / `sys.stderr` / `sys.argv`
      （redirect_stdout 与 runpy 的既定语义），两个请求同时进来会互相串台。
      引擎每个请求一个线程，所以这把锁是必需的，不是保险。
    """
    import contextlib
    import runpy

    if not os.path.isfile(script):
        raise RpcError(f"找不到脚本：{script}")

    with _SCRIPT_LOCK:
        cap = _CaptureIO(on_line)
        old_argv = sys.argv
        sys.argv = [script, *[str(a) for a in argv]]
        code = 0
        try:
            with contextlib.redirect_stdout(cap), \
                    contextlib.redirect_stderr(cap):
                runpy.run_path(script, run_name="__main__")
        except SystemExit as exc:
            val = exc.code
            code = 0 if val is None else (val if isinstance(val, int) else 1)
        except Exception as exc:      # noqa: BLE001 —— 脚本崩了也要有文本可看
            code = 1
            cap.write(f"\n[XX] 脚本抛异常：{type(exc).__name__}: {exc}\n")
            cap.write(traceback.format_exc()[-1500:])
        finally:
            sys.argv = old_argv
        return int(code), cap.text()


def _run_powershell(script: str, timeout: float = 110.0) -> tuple:
    """跑一个 PowerShell 脚本 → `(退出码, 原样文本)`。

    ⚠ 全项目只有 `scripts/sync-skill.ps1` 需要它（把 `skills\\` 复制到
      `~/.dsh/skills`）。它是**PowerShell 写的既有实现**，不重写成 Python
      （那是另造一份业务，两边迟早漂移），所以这里是唯一必须起子进程的动作。
    """
    import subprocess

    if not os.path.isfile(script):
        raise RpcError(f"找不到脚本：{script}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy",
                             "Bypass", "-File", script],
                            stdin=subprocess.DEVNULL,   # 同上：不继承请求管道
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=ROOT, env=env, creationflags=flags, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RpcError(f"PowerShell 脚本超过 {int(timeout)} 秒还没结束"
                       f"（已被强制结束）：{os.path.basename(script)}") from exc
    except OSError as exc:
        raise RpcError(f"起不了 PowerShell：{type(exc).__name__}: {exc}") from exc
    return cp.returncode, (cp.stdout or "")


def _dir_size(path: str) -> int:
    """目录占用字节数。**实现只有一份：`schemas.dir_size()`**（2026-10-15 合并）。"""
    return S.dir_size(path or "")


# ---------------------------------------------------------------- 分类建议

def _m_papers_list(params: dict) -> dict:
    """文献列表（分类建议页那个"选择文献"表）。参数 `{limit?}`。

    口径照 Tk 面板 `tools/panels/tab_ai.py（已退役 2026-10-14）::refresh_paper_list`：直接读
    `items` 的结构化字段，**不**拼成一整行字符串（拼成一行的坏处记在
    paper_picker 里：列宽不可拖、不能排序）。
    `in_kb` 恒为 true —— 这些行本来就逐条来自 `items`（"在知识库里"的判据与
    `do_item_info` 一致，页面两处用同一个字段名）。
    """
    limit = _as_int(params.get("limit"), 0)
    s = _searcher()
    try:
        raw = s.read("SELECT key, title, year, first_author, collections, "
                     "fulltext_chars FROM items "
                     "ORDER BY year DESC, first_author, key")
    finally:
        s.close()
    rows = []
    for r in raw:
        try:
            cols = json.loads(r["collections"] or "[]")
        except (TypeError, ValueError):
            cols = []
        rows.append({"key": r["key"], "title": r["title"] or "",
                     "year": str(r["year"] or ""),
                     "first_author": r["first_author"] or "",
                     "collections": [str(c) for c in cols],
                     "fulltext_chars": int(r["fulltext_chars"] or 0),
                     "in_kb": True})
    total = len(rows)
    if limit > 0:
        rows = rows[:limit]
    return {"rows": rows, "total": total}


def _m_ai_summarize(params: dict) -> dict:
    """给一篇生成要点（本地模型）→ `{key, summary, ms}`。

    与 Tk 面板「生成要点」走同一条路（`judge.summarize_item`；面板是
    `python offline/judge.py summarize --key`）。
    ⚠ **只读**：面板那一条也没带 `--write`（不写 `meta.ai_summary:<key>`），
      这里保持一致 —— 要落库得另说，别在"看一眼"的按钮里偷偷写。
    ⚠ 这一条**很慢**（真调模型，几十秒）：契约就让它同步返回，页面自己给
      "运行中"状态。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key（要生成要点的那一篇）")
    import schemas as S
    import judge as J

    s = _searcher()
    try:
        item = s.get_item(key)
    finally:
        s.close()
    if not item:
        raise RpcError(f"索引库里没有 key={key} 的条目")
    path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
    if not os.path.isfile(path):
        raise RpcError(f"《{item.get('title') or key}》没有全文文件"
                       f"（fulltext/{key}.md），无法摘要")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise RpcError(f"读全文失败：{exc}") from exc

    t0 = time.time()
    res = J.summarize_item(item.get("title") or "", text)
    ms = int((time.time() - t0) * 1000)
    if not res.get("ok"):
        raise RpcError(str(res.get("error") or "生成要点失败"))
    points = [str(p) for p in (res.get("points") or [])]
    return {"key": key, "summary": "\n".join(points), "ms": ms,
            "title": item.get("title") or "",
            # 额外给页面几段能用的（面板里是分行打印的；缺了还得再调一次模型）
            "points": points, "method": res.get("method") or "",
            "keywords": [str(k) for k in (res.get("keywords") or [])],
            "model": res.get("model") or "",
            "written": False}


def _m_ai_classify(params: dict) -> dict:
    """给一篇生成分类建议 → `{key, category, tags[], reason, raw?}`。

    **复用** `localserver.do_classify_one`（它转发到 `judge.classify_item`，
    与 Tk 面板 `judge.py classify`、插件弹窗是同一份提示词）。
    区别只在于 HTTP 的 `/classify` 收的是插件发来的 `item`（新文献还没入库），
    而这里收的是 **key** —— 所以由引擎从索引库把 title/abstract/tags 读出来，
    再交给同一个实现。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key（要分类的那一篇）")
    feedback = str(params.get("feedback") or "")
    previous = params.get("previous")
    if previous is not None and not isinstance(previous, dict):
        raise RpcError("previous 要是一段对象（上一轮的建议）")

    s = _searcher()
    try:
        item = s.get_item(key)
    finally:
        s.close()
    if not item:
        raise RpcError(f"索引库里没有 key={key} 的条目")
    meta = {"title": item.get("title") or "",
            "abstract": item.get("abstract") or "",
            "tags": item.get("tags") or []}

    res = LS.do_classify_one(meta, [], "", feedback=feedback,
                             previous=previous) or {}
    if res.get("error"):
        msg = str(res["error"])
        if res.get("hint"):
            msg += f"（{res['hint']}）"
        raise RpcError(msg)
    return {"key": key, "category": res.get("category") or "",
            "tags": [str(t) for t in (res.get("tags") or [])],
            "reason": res.get("reason") or "",
            # raw 只在"模型原话被归一过"时才有意义（面板会把它显示出来）
            "raw": res.get("raw_category") or "",
            "confidence": res.get("confidence"),
            "known_categories": res.get("known_categories") or [],
            "round": res.get("round"), "model": res.get("model") or ""}


def _m_ai_apply(params: dict) -> dict:
    """把分类建议写回 Zotero → `{ok, applied, output}`。

    **写用户的 Zotero 库**（并同步到 zotero.org），所以页面必须二次确认。
    与 Tk 面板「应用建议」同一条路：`tools/zotero_sync.py apply-one`。
    ⚠ 失败**不抛**而是回 `ok:false` + `output`：这里最有用的信息恰恰是那句
      "没授权 / 连不上 Zotero"，抛成一行 error 会把原文丢掉。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    category = str(params.get("category") or "").strip()
    tags = params.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    if not isinstance(tags, (list, tuple)):
        tags = []
    tags = [str(t).strip() for t in tags if str(t).strip()]
    if not category and not tags:
        raise RpcError("这条建议里既没有分类也没有标签，没什么可应用")

    argv = ["apply-one", "--key", key]
    if category:
        argv += ["--category", category]
    if tags:
        argv += ["--tags", ",".join(tags)]
    rc, out = _run_script(_tool("zotero_sync.py"), argv)
    return {"ok": rc == 0, "applied": rc == 0, "output": out,
            "returncode": rc, "key": key, "category": category, "tags": tags}


_MAINTAIN_TASKS = ("stats", "views", "collections", "judge-status", "sync-skill")


def _m_maintain_run(params: dict) -> dict:
    """维护动作 → `{task, ok, output}`；`output` 是原样文本。

    Tk 面板里这些是四个不同按钮（`maintain.py stats/views`、`judge.py status`、
    `scripts/sync-skill.ps1`），页面把它们收成一个下拉 + 「运行」。
    `collections`（2026-10-13 契约更正补的）**没有对应的脚本** ——
    `maintain.py stats` 打的是"前 8 个分类 + 一堆别的数"，页面要完整分类分布，
    所以直接问 `Searcher.collections()`；输出仍是同一形状的等宽文本，
    页面不用为它开分支。
    """
    task = str(params.get("task") or "").strip().lower()
    if task not in _MAINTAIN_TASKS:
        raise RpcError("task 只能是 " + " / ".join(_MAINTAIN_TASKS))

    if task == "collections":
        s = _searcher()
        try:
            cols = s.collections()
            n_items = int((s.read_one("SELECT COUNT(*) AS n FROM items")
                           or {"n": 0})["n"])
        finally:
            s.close()
        lines = [f"分类分布（{len(cols)} 个分类，共 {n_items} 篇文献）",
                 "=" * 48]
        for c in cols:
            lines.append(f"  {str(c['name']):34} {int(c['items']):>4} 篇")
        if not cols:
            lines.append("  （库里还没有分类）")
        return {"task": task, "ok": True, "output": "\n".join(lines)}

    if task == "sync-skill":
        rc, out = _run_powershell(os.path.join(ROOT, "scripts",
                                               "sync-skill.ps1"))
    elif task == "judge-status":
        rc, out = _run_script(_off("judge.py"), ["status"])
    else:
        rc, out = _run_script(_off("maintain.py"), [task])
    return {"task": task, "ok": rc == 0, "output": out, "returncode": rc}


# ---------------------------------------------------------------- PDF 解析

def _m_mineru_status(params: dict) -> dict:
    """MinerU 状态表 → `{probe, rows, total}`。

    口径照 Tk 面板 `tools/panels/tab_parse.py（已退役 2026-10-14）::refresh_parse`：
      · `probe` = `mineru.probe()`（**带缓存**，第一次可能几十秒）；
      · 每行 = items 的正文来源 + `mineru.status_for(key)` 的产物信息 +
        产物目录占用。
    契约要的 `figures` 就是 `status_for().images`（产物里的图片数），
    `elapsed` 是解析耗时秒数，`built_at` 是产物写入时间。
    """
    import mineru as M

    info = M.probe()
    s = _searcher()
    try:
        items = s.read("SELECT key, title, fulltext_src FROM items "
                       "ORDER BY title, key")
    finally:
        s.close()

    rows = []
    total_size = 0
    n_mineru = n_parsed = n_stale = 0
    for it in items:
        key = it["key"]
        st = M.status_for(key)
        size = _dir_size(st.get("dir") or "")
        total_size += size
        src = it["fulltext_src"] or ""
        if str(src).startswith("mineru-"):
            n_mineru += 1
        if st.get("parsed"):
            n_parsed += 1
        if st.get("stale"):
            n_stale += 1
        rows.append({
            "key": key, "title": it["title"] or "",
            "tier": st.get("tier") or "", "fulltext_src": src,
            "pages": int(st.get("pages") or 0),
            "figures": int(st.get("images") or 0),
            "elapsed": st.get("seconds") or 0,
            "artifacts_size": size, "built_at": st.get("at") or "",
            # 多给几个判断用的（面板的状态列就是按这三条拼的）
            "parsed": bool(st.get("parsed")), "stale": bool(st.get("stale")),
            "error": st.get("last_error") or "",
        })
    return {"probe": {"ok": bool(info.get("ok")), "exe": info.get("exe") or "",
                      "why": info.get("why") or "",
                      # 这三条是面板"可用档位"那行要的，顺手带上
                      "version": info.get("version") or "",
                      "tier_ready": info.get("tier_ready") or {},
                      "cached": bool(info.get("cached"))},
            "rows": rows, "total": len(rows),
            "summary": {"items": len(rows), "mineru_src": n_mineru,
                        "parsed": n_parsed, "stale": n_stale,
                        "artifacts_size": total_size},
            "artifacts_size": total_size}


def _run_mineru_parse(mode: str, key: str, parser: str, progress=None) -> dict:
    """job 线程里跑 `tools/kb_admin.py mineru …`（与 Tk 面板同一条命令）。

    逐行把脚本输出喂给 job.log —— 批量解析几十分钟，页面只能靠轮询
    `job.log` 看进度，攒到结束才给等于没有进度。
    """
    argv = (["mineru", "parse", "--key", key, "--tier", parser, "--rebuild"]
            if key else ["mineru", mode, "--tier", parser])
    if progress:
        progress("开始解析（输出如下）…")
    rc, _out = _run_script(_tool("kb_admin.py"), argv, on_line=progress)
    if progress:
        progress(f"解析结束，退出码 {rc}")
    return {"mode": mode, "key": key, "parser": parser,
            "ok": rc == 0, "returncode": rc}


def _m_mineru_parse(params: dict) -> dict:
    """解析 PDF（**长任务，派发即返回**）。

    参数 `{tier:"missing"|"reparse", key?}`（契约口径）：
      · 不给 key → 批量：`missing` 只补还没有产物的，`reparse` 全库换成所选档位；
      · 给 key  → 只解析这一篇（并顺手重建它的切片与向量）。
    ⚠ 契约里的 `tier` 说的是**动作**（missing/reparse），不是解析档位 ——
      解析档位另用可选的 `parser`（zotero/flash/basic/standard/advanced，
      默认 basic，与面板下拉的默认值一致）。
    """
    mode = str(params.get("tier") or "missing").strip().lower()
    if mode not in ("missing", "reparse"):
        raise RpcError('tier 只能是 "missing"（只补缺失）或 "reparse"（全库重解析）')
    key = str(params.get("key") or "").strip()
    parser = str(params.get("parser") or "basic").strip().lower()
    if parser not in ("zotero", "flash", "basic", "standard", "advanced"):
        raise RpcError("parser 只能是 zotero / flash / basic / standard / advanced")

    title = f"解析这一篇 {key}（{parser} 档）" if key else \
        (f"补全缺失的 PDF（{parser} 档）" if mode == "missing"
         else f"全库重解析 PDF（{parser} 档）")
    job_id = LS.start_job(title, _run_mineru_parse, mode, key, parser,
                          progress=True)
    rid = _rid()
    emit({"id": rid, "event": "progress",
          "data": {"job": job_id, "percent": None,
                   "message": f"已派发 PDF 解析任务 {job_id}（{title}）"}})
    return _job_dispatch(job_id)


def _m_mineru_clear(params: dict) -> dict:
    """删掉一篇的解析产物 → `{cleared, freed_bytes}`（**删本地数据**）。

    业务复用 `mineru.clear_artifacts`（它自带护栏：只删"看起来像产物的"目录
    —— 里面有 meta.json 或 pages.json）。切片与向量**不受影响**（与面板那句
    提示一致）。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key（要删哪一篇的产物）")
    import mineru as M

    adir = M.artifact_dir(key)
    before = _dir_size(adir)
    n = M.clear_artifacts(key)
    after = _dir_size(adir)
    return {"cleared": bool(n), "freed_bytes": max(0, before - after),
            "key": key, "path": adir,
            "note": "" if n else "这一篇没有解析产物（没解析过，或者已经删过）"}


# ---------------------------------------------------------------- 损坏查询

def _m_quality_stats(params: dict) -> dict:
    """损坏查询页的现状数字。

    口径照 Tk 面板 `tab_quality.py::refresh_quality`（切片/图表计数 +
    `item_health` 里非 ok 的条目）。
    ⚠ 契约的行字段 `n_bad` / `n_suspect` 是 **0/1 标记**，不是"坏切片条数"：
      `item_health` 是**文献级**一张表（`check_chunks` 明确不做切片级 —— 旧的
      切片级 `chunk_quality` 已被它删掉，那套判据本身不成立）。所以这里一行 =
      一篇非 ok 的文献，`n_bad` = 是否被判 bad，`n_suspect` = 是否待人工确认；
      要"几篇坏"就把它们求和。
    `item_health` 给的是聚合对象（checked/ok/bad/unsure/checked_at），**另外**
      给同名的扁平数字字段（`bad`/`unsure`/`checked`/`item_health_count`），
      页面两种读法都成立。
    """
    s = _searcher()
    try:
        chunks = int((s.read_one("SELECT COUNT(*) AS n FROM chunks")
                      or {"n": 0})["n"])
        try:
            figures = int((s.read_one("SELECT COUNT(*) AS n FROM figures")
                           or {"n": 0})["n"])
        except Exception as exc:      # noqa: BLE001 —— 老库可能没有这张表
            log(f"读 figures 失败（当成 0）：{exc}")
            figures = 0
        checked_at = ""
        try:
            grp = {str(r["verdict"]): int(r["n"])
                   for r in s.read("SELECT verdict, COUNT(*) AS n FROM "
                                   "item_health GROUP BY verdict")}
            checked_at = str((s.read_one(
                "SELECT MAX(checked_at) AS t FROM item_health") or {"t": ""})["t"]
                or "")
            raw = s.read(
                "SELECT h.item_key AS key, h.verdict AS verdict, "
                "h.reasons AS reasons, h.n_chars AS n_chars, "
                "h.checker AS checker, COALESCE(i.title, '') AS title "
                "FROM item_health h LEFT JOIN items i ON i.key = h.item_key "
                "WHERE h.verdict <> 'ok' "
                "ORDER BY h.verdict DESC, h.item_key LIMIT 200")
        except Exception as exc:      # noqa: BLE001 —— 还没跑过检查
            log(f"读 item_health 失败（当成没查过）：{exc}")
            grp, raw = {}, []
    finally:
        s.close()

    rows = []
    for r in raw:
        verdict = str(r["verdict"] or "")
        try:
            reasons = json.loads(r["reasons"] or "[]")
        except (TypeError, ValueError):
            reasons = []
        rows.append({"key": r["key"], "title": r["title"] or "",
                     "n_bad": 1 if verdict == "bad" else 0,
                     "n_suspect": 1 if verdict == "unsure" else 0,
                     # 多给几列（页面要用 title 悬停说明"为什么坏"）
                     "verdict": verdict, "reasons": reasons,
                     "n_chars": r["n_chars"], "checker": r["checker"]})
    health = {"checked": sum(grp.values()), "ok": grp.get("ok", 0),
              "bad": grp.get("bad", 0), "unsure": grp.get("unsure", 0),
              "checked_at": checked_at}
    return {"chunks": chunks, "figures": figures, "item_health": health,
            "item_health_count": health["checked"],
            "checked": health["checked"], "bad": health["bad"],
            "unsure": health["unsure"], "checked_at": checked_at,
            "rows": rows, "total": len(rows)}


def _run_quality_scan(use_model: bool, progress=None) -> dict:
    """job 线程里跑 `check_chunks.scan`（结果翻译成契约形状）。

    契约的 `job.result = {items:[{key,title,issue,detail}], summary}`：
    `issue` 取第一条判据（列表里那一格要短），`detail` 是全部判据连起来；
    `summary` 放聚合数字（页面顶部的汇总行用它）。
    """
    import check_chunks as CC

    if progress:
        progress("开始检查（%s）…" % ("规则 + 本地模型" if use_model else "仅规则"))

    def prog(done, total, note):
        # 每 20 篇记一行：97 篇逐行会把 job.log 挤满 200 行的上限，
        # 反而把开头"开始检查"那几行顶掉。
        if progress and (done % 20 == 0 or done == total):
            progress(f"{done}/{total}  {str(note)[:60]}")

    r = CC.scan(use_model=use_model, progress=prog)
    items = []
    for x in (r.get("items") or []):
        reasons = [str(y) for y in (x.get("reasons") or [])]
        items.append({"key": x.get("key", ""), "title": x.get("title", ""),
                      "issue": reasons[0] if reasons else "",
                      "detail": "；".join(reasons)})
    summary = {k: r.get(k) for k in
               ("total", "bad", "unsure", "healthy", "elapsed", "model_note",
                "legacy_dropped", "stale_dropped", "per_sec", "note")
               if k in r}
    if progress:
        progress(f"检查完成：{r.get('total')} 篇，"
                 f"不可信 {r.get('bad')} 篇，待确认 {r.get('unsure', 0)} 篇，"
                 f"耗时 {r.get('elapsed')} 秒")
    return {"items": items, "summary": summary}


def _m_quality_scan(params: dict) -> dict:
    """检查提取不可信的切片（**长任务，派发即返回**）。

    参数 `{use_model?}`：假 = 只跑规则（秒级），真 = 灰区再问一次本地模型
    （全库几分钟）。结果形状见 `_run_quality_scan`。
    """
    use_model = bool(params.get("use_model"))
    job_id = LS.start_job(
        "损坏查询（%s）" % ("规则 + 本地模型" if use_model else "仅规则"),
        _run_quality_scan, use_model, progress=True)
    rid = _rid()
    emit({"id": rid, "event": "progress",
          "data": {"job": job_id, "percent": None,
                   "message": f"已派发损坏查询任务 {job_id}"
                              f"（{'规则 + 模型' if use_model else '仅规则'}）"}})
    return _job_dispatch(job_id)


def _run_quality_repair(keys, progress=None) -> dict:
    """job 线程里逐篇重建不可信的文献（`repair_chunks.repair`）。

    结果**原样**返回 `repair_chunks.repair` 的字典（ok / fixed / failed /
    skipped / bad_before / bad_after / deshifted / details / skipped_detail /
    note）—— 契约没规定这一条的 result 形状，而这份字段面板一直在用，
    翻译一次就等于又多一份口径。
    """
    import repair_chunks as RC

    if progress:
        progress("开始逐篇重建…")

    def prog(done, total, note):
        if progress:
            progress(f"{done}/{total}  {str(note)[:60]}")

    r = RC.repair(keys or None, progress=prog)
    if progress:
        if r.get("ok"):
            progress(f"重建完成：成功 {r.get('fixed')} 篇，"
                     f"仍不可信 {r.get('failed')}，跳过 {r.get('skipped')}，"
                     f"耗时 {r.get('elapsed')} 秒")
        else:
            progress(f"[XX] 重建失败：{r.get('error')}")
    return r


def _m_quality_repair(params: dict) -> dict:
    """重建不可信的文献（**长任务，派发即返回**）。

    参数 `{keys?[]}`（空 = 所有判坏的）。
    """
    keys = params.get("keys")
    if isinstance(keys, str):
        keys = [keys]
    if keys is not None and not isinstance(keys, (list, tuple)):
        raise RpcError("keys 要是数组（要重建的条目 key；不给 = 修复全部判坏的）")
    picked = [str(k or "").strip() for k in (keys or []) if str(k or "").strip()]
    job_id = LS.start_job(
        f"重建不可信文献（{len(picked)} 篇）" if picked else "重建全部不可信文献",
        _run_quality_repair, picked, progress=True)
    rid = _rid()
    emit({"id": rid, "event": "progress",
          "data": {"job": job_id, "percent": None,
                   "message": f"已派发重建任务 {job_id}"
                              + (f"（{len(picked)} 篇）" if picked else "")}})
    return _job_dispatch(job_id)


def _chunk_pages(key: str) -> list:
    """这一篇的 `(页码, 正文)` 列表（给样本定位页码用，只读）。"""
    s = _searcher()
    try:
        return [(r["page"], r["text"] or "")
                for r in s.read("SELECT page, text FROM chunks "
                                "WHERE item_key = ? "
                                "ORDER BY COALESCE(seq, 0), chunk_id", (key,))]
    finally:
        s.close()


def _page_of_sample(sample: str, chunks: list):
    """在切片里找这个样本落在哪一页（找不到就 None）。

    ⚠ 只是**尽力而为**：`check_chunks` 的样本取自"整篇拼接文本"，可能与切片
      边界错开；匹配不上时如实给 null，不猜一个页码。
    """
    head = (sample or "").strip()[:40]
    if not head:
        return None
    for page, text in chunks:
        if head in (text or ""):
            return page
    return None


def _m_quality_detail(params: dict) -> dict:
    """看某篇的体检明细 → `{key, title, rows:[{page,kind,detail,snippet}]}`。

    业务复用 `repair_chunks.detail`（它转发到 `check_chunks.detail`，再补上
    fulltext_src / fulltext_chars）。
    `kind` 三档（页面按它给行加小标记）：`verdict` 结论、`reason` 判据、
    `sample` 正文样本；`page` 由样本在切片里的位置推出（推不出给 null）。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key（要看哪一篇的明细）")
    import repair_chunks as RC

    d = RC.detail(key) or {}
    if not d.get("ok"):
        raise RpcError(str(d.get("error") or f"查不到 {key} 的检查结果"
                                             "（先跑一次检查）"))
    reasons = [str(x) for x in (d.get("reasons") or [])]
    samples = [str(x) for x in (d.get("samples") or [])]
    chunks = _chunk_pages(key) if samples else []
    rows = [{"page": None, "kind": "verdict",
             "detail": f"结论 {d.get('verdict') or '?'}"
                       f"（{d.get('checker') or '?'}）· "
                       f"{d.get('n_chars') or 0} 字",
             "snippet": ""}]
    for x in reasons:
        rows.append({"page": None, "kind": "reason", "detail": x, "snippet": ""})
    for i, sample in enumerate(samples):
        rows.append({"page": _page_of_sample(sample, chunks),
                     "kind": "sample",
                     "detail": f"正文样本 {i + 1}", "snippet": sample[:400]})
    return {"key": key, "title": d.get("title") or "", "rows": rows,
            "verdict": d.get("verdict") or "", "n_chars": d.get("n_chars"),
            "checker": d.get("checker") or "",
            "fulltext_src": d.get("fulltext_src") or "",
            "fulltext_chars": d.get("fulltext_chars")}


def _figures_on() -> bool:
    """`kb-location.json` 里"构建时提取图注与表格"的当前值（默认开）。"""
    import schemas as S
    try:
        return bool(S.figures_enabled())
    except Exception as exc:          # noqa: BLE001 —— 读不到按默认（开）处理
        log(f"读 figures 开关失败（按开处理）：{exc}")
        return True


def _m_config_figures(params: dict) -> dict:
    """构建时要不要抽图注与表格 → `{saved, figures}`（写 kb-location.json）。

    复用 `schemas.write_location_config`（它知道位置优先级与写回格式，手写
    JSON 容易写到另一份文件上）—— 与 Tk 面板那个勾选框同一份实现。
    """
    if "on" not in params:
        raise RpcError("需要 on（true / false）")
    import schemas as S

    val = params.get("on")
    if isinstance(val, str):
        # 插件设置里可能传字符串 "0"/"false" —— 按关处理（与 env.save 一致）
        val = val.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        val = bool(val)
    try:
        path = S.write_location_config(figures=val)
    except (ValueError, OSError) as exc:
        raise RpcError(f"写入配置失败：{exc}") from exc
    return {"saved": True, "figures": val,
            "figures_enabled": _figures_on(),
            "path": path}


def _m_config_figures_get(params: dict) -> dict:
    """读那个开关的当前值 → `{figures: bool}`。

    为什么要有**读**端点（契约 2026-10-13 补）：页面那个勾选框必须显示**真值**
    —— 假勾一个状态是骗人。值来自 `schemas.figures_enabled()`（与构建侧读的
    是同一份 `kb-location.json`）。
    """
    return {"figures": _figures_on()}


# ---------------------------------------------------------------- 高级

def _m_kb_search(params: dict) -> dict:
    """检索知识库（高级页那个搜索框）→ `{rows:[{key,title,score,snippet}]}`。

    业务复用 `Searcher.search`（HTTP `/search` 与 MCP 的 kb_search 都是它）。
    这里只把 `hits` 摊成契约的行形状：`snippet` 取**第一条**片段，另外把页码 /
    作者 / 权重 / 可读引用名一并带上（页面画结果列表要用）。
    """
    q = str(params.get("query") or "").strip()
    if not q:
        raise RpcError("需要 query（检索词）")
    limit = _as_int(params.get("limit"), 8)
    if limit <= 0:
        limit = 8

    s = _searcher()
    try:
        res = s.search(q, limit=limit)
    finally:
        s.close()
    rows = []
    for h in (res.get("hits") or []):
        sn = ((h.get("snippets") or [{}]) or [{}])[0]
        rows.append({"key": h.get("key", ""), "title": h.get("title") or "",
                     "score": h.get("score"), "snippet": sn.get("text") or "",
                     "page": sn.get("page"),
                     "year": str(h.get("year") or ""),
                     "first_author": h.get("first_author") or "",
                     "weight": h.get("weight"),
                     "ref": h.get("ref") or h.get("label") or "",
                     "collections": h.get("collections") or []})
    return {"rows": rows, "total": len(rows), "query": q}


def _m_kb_weight(params: dict) -> dict:
    """写一条条目的权重（标重点 / 取消重点 / 人工分 / 备注）。

    **复用** `localserver.do_set_weight`（实现只有 `experience.set_weight`
    一份；它只动人工字段，不碰经验统计）。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    pinned = params.get("pinned")
    if pinned is not None and not isinstance(pinned, bool):
        pinned = str(pinned).strip().lower() not in ("0", "false", "no", "off", "")
    manual = params.get("manual")
    if manual is not None:
        try:
            manual = float(manual)
        except (TypeError, ValueError):
            raise RpcError("manual 要是数字（人工加权分）") from None
    note = params.get("note")
    res = LS.do_set_weight(key, pinned=pinned, manual=manual,
                           note=None if note is None else str(note))
    if not res.get("ok"):
        raise RpcError(str(res.get("error") or "写入权重失败"))
    return res


def _m_advanced_weight_list(params: dict) -> dict:
    """权重榜（被加权过的文献）→ `{rows:[{key,title,year,weight,pinned,note}]}`。

    口径照 Tk 面板 `tab_experience.py::do_weight_list`：按
    `3*pinned + manual + 2*effective + 0.5*ineffective` 降序（**只列
    `item_weight` 里有行的**，与面板一致 —— "每篇的权重"要看 `do_weights_map`）。
    """
    limit = _as_int(params.get("limit"), 0)
    import schemas as S

    s = _searcher()
    try:
        raw = s.read(
            "SELECT w.item_key AS key, w.pinned, w.manual, w.note, "
            "w.attempts, w.effective, w.ineffective, w.partial, "
            "COALESCE(i.title, '') AS title, i.year AS year "
            "FROM item_weight w LEFT JOIN items i ON i.key = w.item_key "
            "ORDER BY (3*w.pinned + w.manual + 2*w.effective "
            "+ 0.5*w.ineffective) DESC, w.item_key")
    finally:
        s.close()
    rows = []
    for r in raw:
        try:
            weight = S.weight_multiplier(dict(r), S.recency_base(r["year"]))
        except Exception as exc:      # noqa: BLE001 —— 年份坏不该让整表打不开
            log(f"算权重失败（用 1.0）：{exc}")
            weight = 1.0
        rows.append({"key": r["key"], "title": r["title"] or "",
                     "year": str(r["year"] or ""),
                     "weight": round(weight, 3),
                     "pinned": bool(r["pinned"]), "note": r["note"] or "",
                     "manual": r["manual"] or 0.0,
                     "attempts": r["attempts"] or 0,
                     "effective": r["effective"] or 0,
                     "ineffective": r["ineffective"] or 0,
                     "partial": r["partial"] or 0})
    total = len(rows)
    if limit > 0:
        rows = rows[:limit]
    return {"rows": rows, "total": total}


def _m_kb_item_info(params: dict) -> dict:
    """某几条文献在知识库里的状态 → 与 HTTP `POST /item-info` **同一份**（`{items:[…]}`）。

    每个条目给：`in_kb`（在不在库）、`title/first_author/year/collections/tags`、
    `fulltext_chars/n_chunks/n_notes/n_annotations/weight`；**不在库的只有
    `{key, in_kb:false}`**（调用方靠这一条判"这篇还没建过库"）。

    两个调用方（`06-watch.js` 监听新条目、`12-dsh.js` 发文献给 DSH）共用它 ——
    所以这里**只做搬运**（复用 `LS.do_item_info`），一行业务都不重写。
    参数 `keys`：数组或逗号串；空 → 报错（与 HTTP 的 400 一致，别静默回空表）。
    """
    keys = params.get("keys")
    if isinstance(keys, str):
        keys = [k for k in keys.split(",") if k.strip()]
    if not isinstance(keys, list) or not keys:
        raise RpcError("需要 keys 数组")
    return LS.do_item_info([str(k) for k in keys])


def _m_kb_col_status(params: dict) -> dict:
    """Zotero 文献列表那两列（「已建库」「分节纲要」）→ 与 HTTP `GET /col-status` 同一份。

    为什么必须**全量 + 一次**：ItemTree 的 dataProvider 是同步的、每行都会被调用，
    不可能逐行发请求。所以这里是 `{items:{key:{in_kb,outline_sections,outline_file}}}`。

    ⚠ **`outline_sections` 走 `meta.ai_outline:<key>`（读端口径），不是数
    `views\\*.outline.md` 文件个数**。本机实测：94 篇全有 meta（读端能看到纲要），
    而 views 里只有 50 个 `.outline.md` —— 按文件数会让 44 篇「明明有纲要」显示成 ✗。
    那条老 bug 的取证在 `LS._outline_meta_counts` 的注释里，别再犯。
    """
    return LS.do_col_status()


def _m_kb_weights_map(params: dict) -> dict:
    """全量权重映射 → 与 HTTP `GET /weights` **逐字段一致**（`{weights:{key:{…}}}`）。

    为什么不能拿 `advanced.weightList` 顶替它（历史踩过同一个坑）：那个的口径是
    「权重榜」—— **只列 `item_weight` 里有行的几篇**；而 Zotero 文献列表的权重列要对
    **每一篇**都给数（没有经验、没标重点的至少还有按发表年份算的基础权重）。
    老实现曾写成 `FROM item_weight w LEFT JOIN items i`，本机只有 19/97 篇有数、
    其余全空白，用户直接以为"权重没算"。

    参数 `keys`（可选，逗号串或数组）：只取这几篇 —— 与 `LS.do_weights_map(keys)`
    的能力对齐，免得下次要按 key 过滤时又开一条新方法。
    """
    keys = params.get("keys")
    if isinstance(keys, str):
        keys = [k for k in keys.split(",") if k.strip()]
    elif isinstance(keys, list):
        keys = [str(k) for k in keys if str(k).strip()]
    else:
        keys = None
    return LS.do_weights_map(keys)


def _m_classify_item(params: dict) -> dict:
    """给**一篇还没入库的新文献**要分类建议（内联元数据 + 调用方给的模型配置）。

    **与 `ai.classify` 的分工（别混，这是两个不同能力）**：
      · `ai.classify` 收 **key**，由引擎**从索引库读**元数据 —— 服务"库里已有的那一篇"。
      · **本方法**收**内联** `item:{title,abstract,tags}` —— 服务"**还没入库**的新文献"
        （插件监听新条目、DSH 批量导入正是这个场景；那时索引库里根本没有它）。

    参数、校验、模型覆盖落盘与回包**逐条对齐老 HTTP `POST /classify`**
    （都复用 `LS.do_classify_one` / `LS._persist_llm_overrides`），所以插件从
    HTTP 切过来行为不变。

    ⚠ **模型配置（model/provider/base_url/api_key）一律来自调用方**，绝不许改成
      读引擎自己的 LLM 配置 —— 那会悄悄换成用户在管理面板里选的那套模型，
      而这个弹窗里用户选的可能是另一个（本机就是两套）。
    """
    item = params.get("item")
    if not isinstance(item, dict):
        raise RpcError("需要 item（内联的条目元数据 {title,abstract,tags}）")
    if not (item.get("title") or item.get("abstract")):
        # 与 HTTP 的 400 同一句话（校验在**任何模型调用之前**，坏输入不烧额度）
        raise RpcError("需要 item.title 或 item.abstract")
    cats = params.get("categories")
    if cats is not None and not isinstance(cats, list):
        raise RpcError("categories 要是一段数组")
    previous = params.get("previous")
    if previous is not None and not isinstance(previous, dict):
        raise RpcError("previous 要是一段对象（上一轮的建议）")
    tags = item.get("tags")
    meta = {"title": str(item.get("title") or ""),
            "abstract": str(item.get("abstract") or ""),
            "tags": [str(t) for t in tags] if isinstance(tags, list) else []}
    # 与 HTTP 一致：把调用方带来的模型配置落盘（尽力而为，失败不影响分类）
    try:
        LS._persist_llm_overrides(params)
    except Exception:                                       # noqa: BLE001
        pass
    res = LS.do_classify_one(
        meta, cats or [], str(params.get("model") or ""),
        feedback=str(params.get("feedback") or ""), previous=previous,
        provider=str(params.get("provider") or ""),
        base_url=str(params.get("base_url") or ""),
        api_key=str(params.get("api_key") or "")) or {}
    if res.get("error"):
        msg = str(res["error"])
        if res.get("hint"):
            msg += f"（{res['hint']}）"
        raise RpcError(msg)
    return {"category": res.get("category") or "",
            "tags": [str(t) for t in (res.get("tags") or [])],
            "reason": res.get("reason") or "",
            "confidence": res.get("confidence"),
            # raw 只在"模型原话被归一过"时才有意义（插件弹窗会显示它）
            "raw": res.get("raw_category") or ""}


def _m_env_check_raw(params: dict) -> dict:
    """运行环境诊断的**原文**（= 老 HTTP `GET /env-check` 那个形状）。

    为什么必须单独有一个：`env.check` 是给**管理面板**重整形过的
    `{ok, items[], sources, rejected[], env_file, location_file}` —— **没有**
    `python` / `project_root` / `ollama` / `runtime_state` / `user_location_file`
    这些顶层键；而 Zotero 插件的设置面板（`settings.js::renderReport`）读的正是
    `r.python.runnable` 与 `r.project_root.has_panel`。
    两条通道服务两个不同的消费者，所以**形状不能合并**：面板用 `env.check`、
    插件用 `env.checkRaw`。
    """
    return LS.env_report()


def _m_kb_digest_cancel(params: dict) -> dict:
    """停掉正在跑的分节纲要（= HTTP `POST /digest/cancel`）。

    ⚠ 不是杀线程：只是置取消标记，`digest.build` 在**每节开始前**看一眼，
      所以正在跑的那一次模型调用会跑完再收工（半份纲要不会落盘）。
      这里必须调 `LS._DIGEST_CANCEL.set()` —— 与 HTTP 分支**同一个标记对象**
      （另建一个的后果：面板与插件各撤各的，谁都撤不动）。
    """
    LS._DIGEST_CANCEL.set()
    return {"ok": True, "cancelled": True,
            "hint": "当前这一篇会在这一节跑完后停下"}


def _m_doc_get(params: dict) -> dict:
    """某篇的**修订层只读状态**（= HTTP `GET /doc-edit?key=`）。

    插件渲染「按页正文」时用它给每页打标记（哪几页改过、哪几页基于旧解析）。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    import edits as ED
    import mineru as M
    return ED.meta(key, M.load_pages(key))


def _m_doc_save(params: dict) -> dict:
    """正文修订的写路径（= HTTP `POST /doc-edit`）：保存/恢复某一页，或写草稿。

    ⚠ 保存/恢复之后 `do_doc_edit` 会**派一次单篇重建**（重切重向量），返回里带
      `job` —— 调用方拿它轮询 `jobs.get`（用户要的就是"改完立即"）。
    ⚠ HTTP 那条返回的是 `(状态码, 响应体)`；这里把 `>=400` 转成 RpcError、其余原样回，
      所以插件从 HTTP 切过来，"成功/失败"的判断口径不变。
    """
    code, payload = LS.do_doc_edit(dict(params or {}))
    if int(code or 200) >= 400:
        body = payload if isinstance(payload, dict) else {}
        raise RpcError(str(body.get("error") or "改正文失败"))
    return payload if isinstance(payload, dict) else {"ok": True}


def _m_formula_target(params: dict) -> dict:
    """把"用户选中的区域/点击位置"定位到某页的某条 `$$…$$`（**不做识别**）。

    = HTTP `GET /formula-target`。`rects` 走的是**查询串**，所以到这儿是
    JSON **字符串**（与 HTTP 完全一致 —— 别指望它是数组）。
    """
    import formula as FM

    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key 参数")
    try:
        page0 = int(params.get("page") or 0)
    except (TypeError, ValueError):
        raise RpcError("page 必须是整数（0 基）") from None
    rect = None
    raw = str(params.get("rects") or "").strip()
    if raw:
        try:
            arr = json.loads(raw)
        except (TypeError, ValueError):
            raise RpcError("rects 不是合法 JSON") from None
        if isinstance(arr, list) and arr and isinstance(arr[0], (list, tuple)):
            rect = [float(v) for v in arr[0][:4]]
        elif isinstance(arr, list) and len(arr) >= 4:
            rect = [float(v) for v in arr[:4]]
        else:
            raise RpcError("rects 形状不对（要 [[x0,y0,x1,y1]]）")
    res = FM.find_formula(key, page0, rect)
    if not res.get("ok"):
        raise RpcError(str(res.get("why") or "定位失败"))
    matched = res.get("matched")

    def _brief(b):
        return ({k: b.get(k) for k in ("block_index", "eq_ordinal",
                                       "latex", "bbox", "type")} if b else None)

    return {"ok": True, "key": key, "page": page0, "page1": page0 + 1,
            "matched": _brief(matched),
            "candidates": [_brief(c) for c in (res.get("candidates") or [])],
            "blocks": [_brief(c) for c in (res.get("blocks") or [])],
            "why": res.get("why") or "",
            "page_text": res.get("page_text") or "",
            "preview": FM.preview_png_b64(key, page0, block=matched, rect=rect),
            "n_pages": res.get("n_pages"),
            "mathtype": FM.mathtype_state()}


def _m_formula_apply(params: dict) -> dict:
    """把新 LaTeX 写回该页第 ordinal 条 `$$…$$`（= HTTP `POST /formula-apply`）。

    写回沿用**唯一写路径**：`do_doc_edit` 会 save_page + 派一次单篇重建，
    返回里带 `job`（调用方轮询 `jobs.get`）。
    """
    import formula as FM

    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    try:
        page0 = int(params.get("page") or 0)
    except (TypeError, ValueError):
        raise RpcError("page 必须是整数（0 基）") from None
    latex = str(params.get("latex") or "").strip()
    if not latex:
        raise RpcError("需要 latex")
    info = FM.page_info(key, page0)
    if not info.get("ok"):
        raise RpcError(str(info.get("why") or "取不到这一页"))
    try:
        text = FM.replace_nth_formula(info["text"], int(params.get("ordinal") or 0),
                                      latex, str(params.get("old_latex") or ""))
    except (ValueError, TypeError) as exc:
        raise RpcError(str(exc)) from None
    code, payload = LS.do_doc_edit({"key": key, "page": page0 + 1, "text": text})
    if int(code or 200) >= 400:
        body = payload if isinstance(payload, dict) else {}
        raise RpcError(str(body.get("error") or "写回失败"))
    return payload if isinstance(payload, dict) else {"ok": True}


def _m_formula_mathtype(params: dict) -> dict:
    """把 LaTeX 放进剪贴板 + 启动 MathType（= HTTP `POST /formula-mathtype`）。

    这一件**只能在本机做**（插件沙箱没有稳定的写剪贴板/起进程能力）。

    ⚠ **`dry_run=true` 也会写剪贴板**（2026-10-14 实测并核对过 `offline/formula.py`：
      那句 docstring 写的是"只做检测与写剪贴板、**不启动** MathType"——它省的是"起进程"，
      **不是**省剪贴板）。所以自检/探针跑 dry_run 时会**覆盖用户当前剪贴板内容**。
      要做到彻底不碰剪贴板，得先改 `formula.start_mathtype`（本轮没改，只留档）。
    """
    import formula as FM

    latex = str(params.get("latex") or "").strip()
    if not latex:
        raise RpcError("需要 latex")
    return FM.start_mathtype(latex, dry_run=bool(params.get("dry_run")))


def _m_advanced_collections(params: dict) -> dict:
    """Zotero 分类清单（从条目的 collections 聚合）→ `{rows:[{name,n}]}`。

    复用 `Searcher.collections`（面板"列出当前分类"就是它）。契约字段叫 `n`，
    面板内部叫 `items` —— 两个都给，免得页面记两套名字。
    """
    s = _searcher()
    try:
        cols = s.collections()
    finally:
        s.close()
    rows = [{"name": str(c.get("name") or ""),
             "n": int(c.get("items") or 0),
             "items": int(c.get("items") or 0)} for c in cols]
    return {"rows": rows, "total": len(rows)}


# zotero_sync / plugin / zotero_upgrade 三组动作 → 实际要跑哪个脚本、什么参数。
# ⚠ 动作名**必须与契约完全一致**（页面按它们发；改一个字母就变成"没有映射"）。
def _plugin_argv(action: str) -> list:
    if action == "build":
        # 面板的「打包插件 xpi」跑的就是它（检查 + 打包一起做）
        return [_tool("check_plugin.py")]
    return [_tool("sideload_plugin.py"),
            {"sideload-status": "status",
             "sideload-install": "install",
             "sideload-remove": "remove"}[action]]


def _cli_action(params: dict, table: dict) -> dict:
    """`{action, ok, output}` 这一类方法的公共骨架（三个动作组都用它）。

    ⚠ 失败**不抛**：`ok` 放在 data 里（契约形状就是 `{action, ok, output}`），
      脚本那句人话原文比一行 error 有用得多。
    """
    action = str(params.get("action") or "").strip().lower()
    if action not in table:
        raise RpcError("action 只能是 " + " / ".join(table))
    script, *argv = table[action]
    rc, out = _run_script(script, argv)
    return {"action": action, "ok": rc == 0, "output": out, "returncode": rc}


def _m_mineru_check_raw(params: dict) -> dict:
    """MinerU 探测的**原文**（= HTTP `GET /mineru-check`）→ 给插件的「安装引导」用。

    为什么不能拿 `mineru.status` 顶替（2026-10-14 实测形状不同）：
      · `mineru.status` 回 `{probe, rows, total, summary, artifacts_size}`
        —— 那是给**管理面板**的列表/汇总形状；
      · 本方法回 `mineru.probe()` 原文：`{ok, exe, version, small_backend, vlm_engine,
        tiers, tier_ready, repos, model_dir, home, torch, cuda, cuda_ok, gpu, why,
        checked_at, elapsed, cached}` —— 插件「MinerU 安装引导」读的就是它。
    `force=1/true` 强制重探（安装引导刚装完要立刻看到结果；否则 `mineru.probe` 自带 60s 缓存）。
    """
    import mineru as _mu
    raw = str(params.get("force") or "").strip().lower()
    return _mu.probe(force=raw in ("1", "true", "yes", "on"))


def _m_meta_fill_one(params: dict) -> dict:
    """单篇的**元数据补全建议**（= HTTP `POST /metafill`，**同步**返回）。

    ⚠ 与 `meta.fillScan` 的分工：那个是**长任务**（派发即返回 job、整库扫一遍）；
      这个是**单篇同步**（规则能搞定的字段秒回，只有兜底那一步才等模型）——
      插件条目右键「补全元数据」要的就是"立刻给我这一条的建议"。
    `use_model` 缺省 **true**（用户默认行为），显式传 false 才关掉模型兜底。
    **只出建议、不写库**（真正的写回在插件里、要用户确认）。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    raw_um = params.get("use_model")
    use_model = True if raw_um is None else bool(raw_um)
    return LS.do_metafill(key, use_model, str(params.get("model") or ""))


def _m_meta_typefix_one(params: dict) -> dict:
    """单篇的类型/标题污染检测与修正建议（= HTTP `POST /typefix`，**同步**返回）。

    ⚠ 与 `meta.typeScan` 的分工：那个是长任务（整库），这个是单篇同步。
    **只读**：真正的改库在插件里、要用户确认。
    """
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("需要 key")
    return LS.do_typefix(key, bool(params.get("use_network")))


def _m_advanced_zotero_sync(params: dict) -> dict:
    """分类重整五连击（check/authorize/plan/apply/verify）→ `{action, ok, output}`。

    `apply` 会**真的改 Zotero 的分类归属**（页面二次确认），其余四步只读/
    申请授权。
    """
    return _cli_action(params, {a: [_tool("zotero_sync.py"), a] for a in
                                ("check", "authorize", "plan", "apply",
                                 "verify")})


def _m_advanced_plugin(params: dict) -> dict:
    """插件打包与侧载 → `{action, ok, output}`。

    `build` 会重打 xpi（开发用，几分钟）；`sideload-install/remove` 要 Zotero
    **完全退出**才生效 —— 页面对这三个都要二次确认（只读的 `sideload-status`
    不用），所以动作名必须与契约一字不差。
    """
    table = {}
    for a in ("build", "sideload-status", "sideload-install", "sideload-remove"):
        parts = _plugin_argv(a)
        table[a] = parts
    return _cli_action(params, table)


def _m_advanced_upgrade(params: dict) -> dict:
    """Zotero 升级前检查 / 备份 / 升级后核对 → `{action, ok, output}`。

    `backup` 会往备份目录写一大份 zotero.sqlite（几分钟）。
    """
    return _cli_action(params, {a: [_tool("zotero_upgrade.py"), a] for a in
                                ("check", "backup", "verify")})


# 当前请求的 id。ⓘ 让方法函数不必层层传 rid —— 引擎每个请求一个线程、
# 同一线程内没有并发，threading.local 足够（比给每个方法加形参更少噪音）。
_CURRENT_ID = threading.local()


def _rid():
    """当前请求的 id（threading.local 没有 .get，要用属性取）。"""
    return getattr(_CURRENT_ID, "value", None)

METHODS = {
    "ping": _m_ping,
    "kb.health": _m_health,
    "kb.status": _m_status,
    "kb.structure": _m_structure,
    "jobs.list": _m_jobs_list,
    "jobs.get": _m_jobs_get,
    "kb.reindex": _m_reindex,
    "kb.digest": _m_digest,
    # W3 线2.5（2026-10-14）：右键菜单的「停止生成纲要」＋ 侧栏正文修订 ＋ 公式纠错。
    "kb.digestCancel": _m_kb_digest_cancel,
    "doc.get": _m_doc_get,
    "doc.save": _m_doc_save,
    "formula.target": _m_formula_target,
    "formula.apply": _m_formula_apply,
    "formula.mathtype": _m_formula_mathtype,
    # ---- W2 批次 A（方法名与开发说明里的那张表一一对应）
    "experience.list": _m_experience_list,
    "experience.get": _m_experience_get,
    "experience.save": _m_experience_save,
    "experience.delete": _m_experience_delete,
    "pending.mark": _m_pending_mark,
    "sessions.list": _m_sessions_list,
    "learn.scan": _m_learn_scan,
    "outline.list": _m_outline_list,
    "outline.render": _m_outline_render,
    "outline.delete": _m_outline_delete,
    "meta.fillScan": _m_meta_fill_scan,
    "meta.fillCancel": _m_meta_fill_cancel,
    "meta.typeScan": _m_meta_type_scan,
    "env.check": _m_env_check,
    # W3 线2.5（2026-10-14）：诊断**原文**（带 python/project_root/runtime_state …），
    # 给 Zotero 插件设置面板用；管理面板仍用上面那个重整形过的 env.check。
    "env.checkRaw": _m_env_check_raw,
    "env.save": _m_env_save,
    "llm.config": _m_llm_config,
    "llm.save": _m_llm_save,
    "llm.models": _m_llm_models,
    "llm.test": _m_llm_test,
    "llm.discover": _m_llm_discover,
    "service.restart": _m_service_restart,
    "prompts.list": _m_prompts_list,
    "prompts.save": _m_prompts_save,
    "prompts.probe": _m_prompts_probe,
    # ---- W2 批次 B（方法名与开发说明里的那张表一一对应）
    "papers.list": _m_papers_list,
    "ai.summarize": _m_ai_summarize,
    "ai.classify": _m_ai_classify,
    # W3 线2.5（2026-10-14）：**内联元数据**的分类建议（给"还没入库的新文献"用）。
    # ⚠ 与 ai.classify 是两个能力：那个认 key、只服务已入库的条目。
    "classify.item": _m_classify_item,
    "ai.apply": _m_ai_apply,
    "maintain.run": _m_maintain_run,
    "mineru.status": _m_mineru_status,
    # W3 线2.5（2026-10-14）：安装引导要的**原文**（与 mineru.status 形状不同，见其 docstring）。
    "mineru.checkRaw": _m_mineru_check_raw,
    # 单篇**同步**的元数据补全 / 类型修正（与 meta.fillScan / meta.typeScan 的分工见 docstring）。
    "meta.fillOne": _m_meta_fill_one,
    "meta.typefixOne": _m_meta_typefix_one,
    "mineru.parse": _m_mineru_parse,
    "mineru.clear": _m_mineru_clear,
    "quality.stats": _m_quality_stats,
    "quality.scan": _m_quality_scan,
    "quality.repair": _m_quality_repair,
    "quality.detail": _m_quality_detail,
    "config.figures": _m_config_figures,
    "config.figuresGet": _m_config_figures_get,
    "kb.search": _m_kb_search,
    "kb.weight": _m_kb_weight,
    # W3 线2.5：Zotero 文献列表的权重列要的是**全量映射**（与 HTTP GET /weights 同口径）。
    # ⚠ 不要拿 advanced.weightList 顶替 —— 那个是"权重榜"，只列有经验/权重的几篇。
    "kb.weightsMap": _m_kb_weights_map,
    # W3 线2.5：文献列表那两列（已建库 / 纲要节数）。⚠ 节数口径 = meta（不是文件数）。
    "kb.colStatus": _m_kb_col_status,
    # W3 线2.5：某几条文献在库状态（监听新条目 / 发文献给 DSH 两处共用）。
    "kb.itemInfo": _m_kb_item_info,
    "advanced.weightList": _m_advanced_weight_list,
    "advanced.collections": _m_advanced_collections,
    "advanced.zoteroSync": _m_advanced_zotero_sync,
    "advanced.plugin": _m_advanced_plugin,
    "advanced.upgrade": _m_advanced_upgrade,
}


# ---------------------------------------------------------------- 分发


def _handle(rid, method: str, params: dict) -> None:
    """处理一条请求：跑方法 → 回一条 ok / ok:false。绝不把异常漏到主循环。"""
    _CURRENT_ID.value = rid
    fn = METHODS.get(method)
    if fn is None:
        emit({"id": rid, "ok": False,
              "error": f"未知方法 {method!r}（可用：{', '.join(sorted(METHODS))}）"})
        return
    try:
        data = fn(params)
    except RpcError as exc:
        emit({"id": rid, "ok": False, "error": str(exc)})
    except Exception as exc:     # noqa: BLE001 —— 任何业务异常都要变成可读的 ok:false
        log("方法 %s 抛异常：\n%s" % (method, traceback.format_exc()[-2000:]))
        emit({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    else:
        emit({"id": rid, "ok": True, "data": data})


def main() -> int:
    log(f"引擎启动 pid={os.getpid()}，知识库 {LS.S.KB_DIR}")
    # ⚠ 用 STDIN.readline() 而不是 `for line in sys.stdin`：后者的迭代器会**提前
    #   缓冲**（读进一大块再逐行吐），交互式请求会被卡住等下一批输入。
    # ⚠ 也**不能**是 `sys.stdin.readline()` —— 阻塞读会把整个进程锁住
    #   （见文件开头「stdin 怎么读」那段；STDIN 是 PeekNamedPipe 轮询版）。
    while True:
        line = STDIN.readline()
        if not line:             # EOF：宿主关掉了管道 → 引擎跟着退出
            log("stdin 关闭，引擎退出")
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 坏 JSON 行不能让引擎退出（协议的核心不变量）—— 回一条 ok:false，
            # id 给 null（这行本身就读不出 id）
            emit({"id": None, "ok": False, "error": f"请求不是合法 JSON：{exc}"})
            continue
        if not isinstance(req, dict):
            emit({"id": None, "ok": False, "error": "请求必须是 JSON 对象"})
            continue
        rid = req.get("id")
        method = str(req.get("method") or "")
        params = req.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            emit({"id": rid, "ok": False, "error": "params 必须是 JSON 对象"})
            continue
        if not method:
            emit({"id": rid, "ok": False, "error": "请求缺少 method"})
            continue
        # 一个请求一个线程：这样 kb.reindex 这种长任务在跑时，
        # jobs.get / ping 仍然能被应答（宿主可以一边看进度一边轮询）。
        threading.Thread(target=_handle, args=(rid, method, params),
                         daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
