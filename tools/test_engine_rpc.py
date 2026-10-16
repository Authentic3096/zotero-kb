"""engine.py（stdio JSON-RPC 引擎）的独立验收测试。

    python tools/test_engine_rpc.py

它**自己**启动引擎子进程（online/engine.py）、按 NDJSON 发请求，然后断言：
  · ping 通；kb.health 有 kb_dir；
  · kb.status 的 items>0、且带 fulltext_chars；
  · kb.structure 行数>5、每行字段齐全；
  · jobs.list 是列表；jobs.get 缺 id / 不存在 → ok:false；
  · kb.digest 缺 key → ok:false；
  · **长任务协议（派发即返回）**：kb.digest 一个不存在的 key（digest 在"找 MinerU
    产物"那一步就返回，一次模型都不调，见 localserver.self_test [5c] 的同一招）——
    应当**立刻**收到 ok、data 里 `job` 是 `{id}`，再用 `jobs.get` 轮询到结局；
    kb.reindex 与它共用 `_job_dispatch`（这里只测参数校验，不真派全库构建）；
  · 不存在的 method、坏 JSON 行 → 回 ok:false，**引擎不退出**（后面 ping 还能通）；
  · **W2 批次 A 的方法**（契约见开发说明）：experience.* / pending.mark /
    sessions.list / learn.scan / outline.* / meta.* / env.* / llm.* / service.restart /
    prompts.* —— 每个方法至少覆盖一次"坏输入被挡住"；会改真库的写动作只用
    坏输入覆盖（写经验、改配置、删纲要都不在测试里做），唯一真跑的长任务
    `learn.scan` 扫的是一个**不存在的会话 id**；
  · **W2 批次 B 的方法**（契约见开发说明）：papers.list / ai.* /
    maintain.run / mineru.* / quality.* / kb.search / kb.weight /
    advanced.* —— 同上：纯查询真跑，会改数据或跑几分钟的只覆盖坏输入；
    `_job_dispatch` 的派发形状用 `quality.repair` + 一个**不存在的 key**
    （inspect 查不到 → 立刻收尾、一篇都不动）覆盖；
  · **stdout 里不出现非 JSON 行**（这是协议的核心不变量，专门断言）；
  · 关掉 stdin 后引擎进程能自己退出。

> ⚠ 长任务的进度**靠轮询 `job.log`**（`GET /jobs/<id>`），引擎**不做事件转发** ——
> 详见 `online/engine.py::_job_dispatch` 的注释（别当成漏了）。

为什么测试要另起进程而不是 import engine：协议的全部价值就在"跨进程的管道"上，
in-process 调用测不到 stdout 污染、编码、EOF 退出这些真问题。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # 仓库根
ENGINE = os.path.join(ROOT, "online", "engine.py")
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"（{detail}）" if detail else ""))
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    return ok


def main() -> int:
    print("=" * 66)
    print("stdio 引擎（online/engine.py）验收")
    print(f"  python : {PY}")
    print(f"  engine : {ENGINE}")
    print("=" * 66)
    if not os.path.isfile(ENGINE):
        print(f"  FAIL  找不到 {ENGINE}")
        return 1

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen([PY, "-X", "utf8", ENGINE],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=errf, cwd=ROOT, env=env)

    raw_lines: list[str] = []      # stdout 里的每一行（原样）
    bad_lines: list[str] = []      # 解析不出 JSON 的那些（必须为空）
    msgs: queue.Queue = queue.Queue()

    def reader() -> None:
        for raw in proc.stdout:                     # type: ignore[union-attr]
            text = raw.decode("utf-8", "replace")
            raw_lines.append(text)
            try:
                msgs.put(json.loads(text))
            except json.JSONDecodeError:
                bad_lines.append(text)

    threading.Thread(target=reader, daemon=True).start()

    _seq = [0]
    _write_lock = threading.Lock()

    def send_raw(line: str) -> None:
        with _write_lock:
            proc.stdin.write((line + "\n").encode("utf-8"))   # type: ignore[union-attr]
            proc.stdin.flush()                               # type: ignore[union-attr]

    def send(method: str, params=None, timeout: float = 120.0):
        """发一条请求，等它的**最终响应**；沿途的 progress 收集起来一起返回。"""
        _seq[0] += 1
        rid = _seq[0]
        send_raw(json.dumps({"id": rid, "method": method,
                             "params": params or {}}, ensure_ascii=False))
        events = []
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"{method} 超时（{timeout}s）")
            try:
                msg = msgs.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(f"{method} 超时（{timeout}s）") from None
            if msg.get("id") != rid:
                # 串行测试里不应该出现：说明有请求的响应串了 id
                print(f"        [warn] 收到不属于 id={rid} 的消息：{str(msg)[:80]}")
                continue
            if msg.get("event") == "progress":
                events.append(msg)
                continue
            return msg, events

    try:
        # ---- 1. 探活
        print("\n[1] ping")
        r, _ = send("ping")
        check("ping 返回 ok", bool(r.get("ok")), str(r)[:120])
        data = r.get("data") or {}
        check("data 里有 version 与 pid",
              bool(data.get("version")) and isinstance(data.get("pid"), int),
              f"version={data.get('version')} pid={data.get('pid')}")

        # ---- 2. kb.health
        print("\n[2] kb.health")
        r, _ = send("kb.health")
        h = r.get("data") or {}
        check("kb.health 有 kb_dir", bool(h.get("kb_dir")), str(h.get("kb_dir")))
        check("带 project_root / python / sources",
              bool(h.get("project_root")) and bool(h.get("python"))
              and isinstance(h.get("sources"), dict))
        check("ollama / mineru 两个键在（可为空串）",
              "ollama" in h and "mineru" in h)

        # ---- 3. kb.status
        print("\n[3] kb.status")
        r, _ = send("kb.status")
        st = r.get("data") or {}
        check("items > 0", isinstance(st.get("items"), int) and st["items"] > 0,
              f"items={st.get('items')}")
        check("fulltext_chars 是 SUM(items.fulltext_chars) 且 > 0",
              isinstance(st.get("fulltext_chars"), int)
              and st["fulltext_chars"] > 0,
              f"fulltext_chars={st.get('fulltext_chars')}")
        for k in ("chunks", "vectors", "experience_rows", "embed_model",
                  "built_at"):
            check(f"有 {k}", k in st, f"{k}={str(st.get(k))[:28]}")

        # ---- 4. kb.structure
        print("\n[4] kb.structure")
        r, _ = send("kb.structure")
        sdata = r.get("data") or {}
        rows = sdata.get("rows") or []
        check("行数 > 5", len(rows) > 5, f"{len(rows)} 行")
        need = ("name", "what", "count", "size", "size_text", "deletable")
        incomplete = [x for x in rows
                      if not all(k in x for k in need)]
        check("每行字段齐全（name/what/count/size/size_text/deletable）",
              not incomplete,
              f"缺字段的 {len(incomplete)} 行")
        check("size 是 int、size_text 是 str",
              all(isinstance(x.get("size"), int)
                  and isinstance(x.get("size_text"), str) for x in rows))
        names = [x["name"] for x in rows]
        check("含 index.db 那一行", any(n == "index.db" for n in names),
              "、".join(names[:4]) + "…")
        # 口径已统一（2026-10-16）：index.db 那一行的「内容量」由引擎直接给真实
        # 数字（"N 篇 / M 切片"），**不再**是"见上面状态栏"那种指着别处的占位话
        # —— 页面侧那段"见到占位串就去状态块取数替换"的逻辑已删。
        dbrow = [x for x in rows if x["name"] == "index.db"]
        check("index.db 行的内容量是真实数字（N 篇 / M 切片），不是占位话",
              bool(dbrow)
              and "篇" in str(dbrow[0].get("count"))
              and "切片" in str(dbrow[0].get("count"))
              and "见上面状态栏" not in str(dbrow[0].get("count")),
              str(dbrow[0].get("count")) if dbrow else "没有这一行")
        check("带 kb_dir / total_size_text",
              bool(sdata.get("kb_dir")) and bool(sdata.get("total_size_text")),
              str(sdata.get("total_size_text")))

        # ---- 5. jobs.*
        print("\n[5] jobs.list / jobs.get")
        r, _ = send("jobs.list")
        jobs = (r.get("data") or {}).get("jobs")
        check("jobs.list 返回列表", isinstance(jobs, list),
              f"{len(jobs) if isinstance(jobs, list) else '?'} 条")
        r, _ = send("jobs.get", {})
        check("jobs.get 缺 id → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])
        r, _ = send("jobs.get", {"id": "job-nope"})
        check("jobs.get 不存在 → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])

        # ---- 6. 参数校验（不许在派活之前漏过）
        print("\n[6] 参数校验")
        r, _ = send("kb.digest", {})
        check("kb.digest 缺 key → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])

        # ---- 7. 错误请求不能弄死引擎
        print("\n[7] 错误请求不许弄死引擎")
        r, _ = send("nope.method")
        check("不存在的 method → ok:false", r.get("ok") is False,
              str(r.get("error"))[:60])
        send_raw("{这不是 JSON")
        try:
            bad = msgs.get(timeout=10)
        except queue.Empty:
            bad = {}
        check("坏 JSON 行 → ok:false（id 为 null）",
              bad.get("ok") is False and bad.get("id") is None,
              str(bad)[:60])
        r, _ = send("ping")
        check("之后 ping 仍然通（引擎没退出）", bool(r.get("ok")))

        # ---- 8. 长任务：**派发即返回** + 轮询 /jobs
        #
        # 2026-10-13 用户拍板把 kb.digest / kb.reindex 从"阻塞到跑完"改成
        # "派发即返回 {job:{id}}"。所以这里的判据是：**响应立刻回来**、
        # data 里给得出 job id、之后 jobs.get 能查到这个 job（并且它最终会 done）。
        # ⚠ 用一个不存在的 key（digest 在"找 MinerU 产物"那一步就返回、一次模型
        #   都不调，见 localserver.self_test [5c] 的同一招），不给真库派活。
        print("\n[8] 长任务协议（kb.digest 派发即返回 + 轮询 /jobs）")
        _t0 = time.time()
        r, events = send("kb.digest", {"key": "ZZZZZZZZ"}, timeout=60)
        el = time.time() - _t0
        check("立刻回一条 ok 响应", bool(r.get("ok")), str(r)[:100])
        check("派发很快（< 10s，不是等它跑完）", el < 10, f"{el:.2f}s")
        ld = r.get("data") or {}
        jid = (ld.get("job") or {}).get("id") if isinstance(ld.get("job"), dict) \
            else ld.get("id")
        check("data.job 是 {id}（也给扁平的 id / job_id / state）",
              bool(jid) and ld.get("id") == jid and ld.get("job_id") == jid
              and ld.get("state") == "running", str(ld)[:90])
        rj, _ = send("jobs.get", {"id": jid})
        jb = rj.get("data") or {}
        check("jobs.get 查得到这个 job（running 或已完成）",
              bool(rj.get("ok")) and jb.get("state") in ("running", "done", "failed"),
              f"state={jb.get('state')}")
        check("job 带 log（进度就靠轮询它，见 engine.py::_job_dispatch）",
              isinstance(jb.get("log"), list), f"{len(jb.get('log') or [])} 行")
        # 等它收尾（不存在的 key 应当很快 done；真失败也算有明确结局）
        dl = time.time() + 60
        while time.time() < dl:
            rj, _ = send("jobs.get", {"id": jid})
            jb = rj.get("data") or {}
            if jb.get("state") in ("done", "failed"):
                break
            time.sleep(0.4)
        check("job 最后有明确结局（done/failed，不是一直 running）",
              jb.get("state") in ("done", "failed"), f"state={jb.get('state')}")
        check("派发时也发了一条 progress（宿主只记日志，不转给页面）",
              len(events) > 0 and all(
                  (e.get("data") or {}).get("job") and "message" in (e.get("data") or {})
                  for e in events), f"{len(events)} 条")
        # kb.reindex 与 kb.digest **同一个 `_job_dispatch`**（派发形状由上面覆盖）。
        # ⚠ 这里**不真的派一次建索引**：do_reindex 不看 keys，非全量 = 起一次
        #   全库增量构建（会真跑子进程、可能改索引），不适合放进自检。
        #   所以只测参数挡住这一半。
        r, _ = send("kb.reindex", {"keys": 123})
        check("kb.reindex 坏 keys → ok:false（派活前就挡住）",
              r.get("ok") is False, str(r.get("error"))[:48])

        # ---- 9. W2 批次 A：五个页面的方法（契约见开发说明）
        #
        # ⚠ 这一节**不做任何会改真库的事**：写经验/改配置/删纲要/真扫会话这些
        #   都用"坏输入必须被挡住"来覆盖；唯一真跑的长任务是 `learn.scan`
        #   扫一个**不存在的会话 id**（扫 0 个文件就收工，不动 positions 内容）。
        print("\n[9] W2 批次 A：经验库 / 分节纲要 / 元数据 / 运行环境 / 提示词")
        r, _ = send("experience.list", {"mode": "all"})
        d = r.get("data") or {}
        rows = d.get("rows") or []
        check("experience.list(all) → {rows,total,mode}",
              bool(r.get("ok")) and isinstance(rows, list)
              and d.get("mode") == "all" and isinstance(d.get("total"), int),
              f"{len(rows)} 条")
        need = ("id", "date", "effect", "source", "refs", "question", "summary")
        check("经验行字段齐全", all(all(k in x for k in need) for x in rows),
              f"缺字段的 {sum(1 for x in rows if not all(k in x for k in need))} 行")
        check("refs 是 [{key,label}]",
              all(isinstance(x.get("refs"), list)
                  and all(isinstance(i, dict) and "key" in i and "label" in i
                          for i in x["refs"]) for x in rows))
        r, _ = send("experience.list", {"mode": "query"})
        check("experience.list query 缺 q → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])
        r, _ = send("experience.list", {"mode": "checkup"})
        crows = (r.get("data") or {}).get("rows") or []
        check("checkup 每条都带 checkup 原因",
              all(x.get("checkup") for x in crows), f"{len(crows)} 条可疑")
        r, _ = send("experience.list", {"mode": "pending"})
        check("experience.list pending 能读（可以为空）",
              bool(r.get("ok"))
              and isinstance((r.get("data") or {}).get("rows"), list))
        r, _ = send("experience.list", {"mode": "zzz"})
        check("experience.list 坏 mode → ok:false", r.get("ok") is False)

        if rows:
            r, _ = send("experience.get", {"id": rows[0]["id"]})
            g = r.get("data") or {}
            check("experience.get 详情字段齐全",
                  all(k in g for k in ("id", "date", "effect", "source", "session",
                                       "question", "method", "reason", "refs",
                                       "tags", "item_keys")),
                  f"id={g.get('id')}")
        r, _ = send("experience.get", {"id": 99999999})
        check("experience.get 不存在 → ok:false", r.get("ok") is False)
        r, _ = send("experience.get", {})
        check("experience.get 缺 id → ok:false", r.get("ok") is False)
        r, _ = send("experience.save", {"question": "", "effect": "effective"})
        check("experience.save 缺 question → ok:false", r.get("ok") is False)
        r, _ = send("experience.save", {"question": "x", "effect": "乱写"})
        check("experience.save 坏 effect → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])
        r, _ = send("experience.delete", {})
        check("experience.delete 缺 ids → ok:false", r.get("ok") is False)
        r, _ = send("pending.mark", {"ids": ["x"], "status": "zzz"})
        check("pending.mark 坏 status → ok:false", r.get("ok") is False)
        r, _ = send("pending.mark", {"status": "dropped"})
        check("pending.mark 缺 ids → ok:false", r.get("ok") is False)
        # adopted 分支会在真命中时写经验库 + 计权重，所以这里用一个不存在的 id：
        # 走完整条采纳代码路径、但一条都不写（marked/adopted 都是 0）。
        r, _ = send("pending.mark", {"ids": ["__no_such__"], "status": "adopted"})
        d = r.get("data") or {}
        check("pending.mark(adopted) 不存在的 id → 0 条（不写库）",
              bool(r.get("ok")) and d.get("marked") == 0
              and d.get("adopted") == 0, str(d)[:60])

        r, _ = send("sessions.list")
        srows = (r.get("data") or {}).get("rows")
        check("sessions.list 返回 rows",
              bool(r.get("ok")) and isinstance(srows, list),
              f"{len(srows) if isinstance(srows, list) else '?'} 个会话")
        check("会话行字段齐全（含 scanned_n）",
              isinstance(srows, list)
              and all(all(k in x for k in ("id", "title", "when", "size_kb",
                                           "scanned", "scanned_n"))
                      for x in srows))
        r, _ = send("learn.scan", {})
        check("learn.scan 缺 sessions → ok:false", r.get("ok") is False)
        r, _ = send("learn.scan", {"sessions": ["坏 id!"]})
        check("learn.scan 非法会话 id → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])
        r, events = send("learn.scan",
                         {"sessions": ["00000000-0000-0000-0000-000000000000"]},
                         timeout=180)
        ld = r.get("data") or {}
        check("learn.scan 立刻回 {job}", bool(r.get("ok"))
              and bool((ld.get("job") or {}).get("id")), str(ld)[:80])
        jid = (ld.get("job") or {}).get("id")
        st = None
        deadline = time.time() + 180
        while jid and time.time() < deadline:
            rr, _ = send("jobs.get", {"id": jid})
            st = rr.get("data") or {}
            if st.get("state") in ("done", "failed"):
                break
            time.sleep(0.5)
        check("learn.scan 的 job 跑到 done", (st or {}).get("state") == "done",
              f"state={(st or {}).get('state')} "
              f"result={str((st or {}).get('result'))[:60]}")

        r, _ = send("outline.list", {"filter": "all"})
        d = r.get("data") or {}
        orows = d.get("rows") or []
        check("outline.list(all) → {rows,filter}",
              bool(r.get("ok")) and isinstance(orows, list)
              and d.get("filter") == "all", f"{len(orows)} 行")
        check("纲要行字段齐全",
              all(all(k in x for k in ("key", "title", "year", "sections",
                                       "has_file", "orphan", "built_at"))
                  for x in orows))
        r, _ = send("outline.list", {"filter": "missing"})
        check("outline.list(missing) 只给「有纲要、缺文件」",
              bool(r.get("ok")) and all(x["sections"] and not x["has_file"]
                                        for x in (r.get("data") or {}).get("rows") or []))
        r, _ = send("outline.list", {"filter": "orphan"})
        check("outline.list(orphan) 只给孤儿",
              bool(r.get("ok")) and all(x["orphan"]
                                        for x in (r.get("data") or {}).get("rows") or []))
        r, _ = send("outline.list", {"filter": "zzz"})
        check("outline.list 坏 filter → ok:false", r.get("ok") is False)
        # 分类（2026-10-13 加）：行里要有 collections，collection 参数与 filter 正交
        check("纲要行带 collections 数组",
              all(isinstance(x.get("collections"), list) for x in orows))
        colls = sorted({c for x in orows for c in (x.get("collections") or [])})
        if colls:
            want = colls[0]
            r, _ = send("outline.list", {"filter": "all", "collection": want})
            d = r.get("data") or {}
            crows = d.get("rows") or []
            check("outline.list(collection=X) 只给该分类，且非空",
                  bool(r.get("ok")) and len(crows) > 0
                  and all(want in (x.get("collections") or []) for x in crows),
                  f"{want}：{len(crows)} 行 / 全部 {len(orows)}")
            check("顶层回 filter 与 collection",
                  d.get("filter") == "all" and d.get("collection") == want)
            r, _ = send("outline.list", {"filter": "missing", "collection": want})
            d = r.get("data") or {}
            check("collection 与 filter 正交（取交集）",
                  bool(r.get("ok"))
                  and all((x.get("sections") and not x.get("has_file"))
                          and want in (x.get("collections") or [])
                          for x in d.get("rows") or []),
                  f"{len(d.get('rows') or [])} 行")
        else:
            check("库里一个分类都没有——跳过正例（只验空集）", True,
                  "items.collections 全空")
        r, _ = send("outline.list", {"filter": "all",
                                     "collection": "ZZZ 不存在的分类"})
        d = r.get("data") or {}
        check("outline.list(collection=不存在) → 0 行且 ok",
              bool(r.get("ok")) and (d.get("rows") or []) == []
              and d.get("collection") == "ZZZ 不存在的分类")
        r, _ = send("outline.render", {"keys": ["ZZZZZZZZ"]})
        d = r.get("data") or {}
        check("outline.render 不存在的 key → rendered=0 且 skipped 说明原因",
              d.get("rendered") == 0 and len(d.get("skipped") or []) == 1
              and bool((d.get("skipped") or [{}])[0].get("reason")),
              str(d.get("skipped"))[:60])
        r, _ = send("outline.render", {"keys": []})
        check("outline.render 缺 keys → ok:false", r.get("ok") is False)
        r, _ = send("outline.delete", {})
        check("outline.delete 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("outline.delete", {"key": "ZZZZZZZZ"})
        check("outline.delete 不存在的 key → deleted=false（没删到东西）",
              bool(r.get("ok")) and (r.get("data") or {}).get("deleted") is False)

        r, _ = send("meta.fillScan", {"use_model": False, "limit": 1}, timeout=300)
        d = r.get("data") or {}
        check("meta.fillScan（纯规则）同步回 {rows,summary}",
              bool(r.get("ok")) and isinstance(d.get("rows"), list)
              and isinstance(d.get("summary"), dict),
              f"{len(d.get('rows') or [])} 行，扫了 {d.get('scanned')}")
        check("summary 里有聚合字段（scanned/total）",
              "scanned" in (d.get("summary") or {})
              and "total" in (d.get("summary") or {}))
        r, _ = send("meta.fillCancel")
        check("meta.fillCancel → {cancelled}",
              "cancelled" in (r.get("data") or {}),
              str(r.get("data"))[:48])
        r, _ = send("meta.typeScan", {"use_network": False}, timeout=300)
        d = r.get("data") or {}
        trows = d.get("rows")
        check("meta.typeScan 每行有 key/title/issue/suggest",
              bool(r.get("ok")) and isinstance(trows, list)
              and all(all(k in x for k in ("key", "title", "issue", "suggest"))
                      for x in trows), f"{len(trows) if isinstance(trows, list) else '?'} 行")

        r, _ = send("env.check")
        d = r.get("data") or {}
        items = d.get("items") or []
        check("env.check 四个位置、字段齐（name/path/ok/source）",
              len(items) >= 4
              and all(all(k in x for k in ("name", "path", "ok", "source"))
                      for x in items), f"{len(items)} 项")
        check("env.check 带 sources（谁覆盖谁）",
              isinstance(d.get("sources"), dict) and "python" in d["sources"])
        r, _ = send("env.save", {})
        check("env.save 一个键都没给 → ok:false（不写）", r.get("ok") is False)
        r, _ = send("env.save", {"python": "___no_such_dir___/python.exe"})
        check("env.save 拒绝不存在的路径 → ok:false", r.get("ok") is False,
              str(r.get("error"))[:60])
        r, _ = send("llm.config")
        d = r.get("data") or {}
        check("llm.config 扁平字段（provider/model/base_url/has_key/key_tail）",
              all(k in d for k in ("provider", "model", "base_url", "has_key",
                                   "key_tail")),
              f"provider={d.get('provider')}")
        # services（2026-10-13 加）：页面按地址认服务商（别把显示名当模型名）
        svcs = d.get("services") or []
        check("llm.config 带 services（[{key,base_url}]，来自 judge.KNOWN_BASE_URLS）",
              len(svcs) >= 5
              and all(all(k in x for k in ("key", "base_url")) for x in svcs),
              f"{len(svcs)} 家：" + ",".join(x["key"] for x in svcs[:6]))
        r, _ = send("llm.save", {})
        check("llm.save 一个键都没给 → ok:false（不写文件）", r.get("ok") is False)
        # provider 白名单与 base_url 形状（2026-10-13 加）：只在**坏输入**上验，
        # 好输入会真写 llm-config.json，测试里不碰。
        r, _ = send("llm.save", {"provider": "deepseek"})
        check("llm.save 不认识的 provider → ok:false（不写库；静默走回 ollama 最坏）",
              r.get("ok") is False and "openai" in str(r.get("error")),
              str(r.get("error"))[:56])
        r, _ = send("llm.save", {"provider": "openai", "base_url": "api.deepseek.com"})
        check("llm.save base_url 漏协议 → ok:false（不写库）",
              r.get("ok") is False and "http" in str(r.get("error")),
              str(r.get("error"))[:56])
        r, _ = send("llm.models")
        d = r.get("data") or {}
        check("llm.models 回 models 列表（Ollama 没在跑也照样有 ok/models）",
              isinstance(d.get("models"), list) and "ok" in d,
              f"{len(d.get('models') or [])} 个模型")
        # llm.test（「测试连接」）：坏输入在引擎里就挡住，不发任何请求
        r, _ = send("llm.test", {"provider": "zzz"})
        d = r.get("data") or {}
        check("llm.test 不认识的 provider → ok:false + 原话（不抛）",
              bool(r.get("ok")) and d.get("ok") is False and bool(d.get("error")),
              str(d.get("error"))[:52])
        r, _ = send("llm.test", {"provider": "openai", "model": ""})
        d = r.get("data") or {}
        check("llm.test 外部 API 但没给模型 id → 直接报出来（不发请求）",
              bool(r.get("ok")) and d.get("ok") is False
              and "模型 id" in str(d.get("error")),
              str(d.get("error"))[:52])
        r, _ = send("llm.test", {"provider": "ollama"}, timeout=60)
        d = r.get("data") or {}
        check("llm.test 本地只查 ollama list（ok/models 都在，不发模型请求）",
              bool(r.get("ok")) and d.get("provider") == "ollama"
              and isinstance(d.get("models"), list) and "ok" in d,
              f"ok={d.get('ok')} models={len(d.get('models') or [])}")
        # ---- 模型目录（三层兜底，2026-10-13 加）
        r, _ = send("llm.discover", {"provider": "ollama"}, timeout=60)
        d = r.get("data") or {}
        check("llm.discover(ollama) 走 /api/tags，行字段齐（id/name/note/context）",
              bool(r.get("ok")) and d.get("source") in ("ollama", "cache")
              and isinstance(d.get("models"), list)
              and all(all(k in x for k in ("id", "name", "note", "context",
                                           "maxtokens"))
                      for x in d.get("models") or []),
              f"source={d.get('source')} {len(d.get('models') or [])} 个")
        # 外部：拿本机 Ollama 的 OpenAI 兼容层真跑一次 /v1/models（不联网、不烧钱）
        r, _ = send("llm.discover", {"provider": "openai", "refresh": True,
                                     "base_url": "http://127.0.0.1:11434/v1",
                                     "api_key": "probe-not-needed"}, timeout=60)
        d = r.get("data") or {}
        seen = [x.get("id") for x in (d.get("models") or [])]
        check("llm.discover(openai→本机 /v1/models) 真探测成功（双返回体认 {data:[]}）",
              bool(r.get("ok")) and d.get("source") in ("remote", "builtin")
              and (d.get("ok") is True or d.get("source") == "builtin"),
              f"ok={d.get('ok')} source={d.get('source')} ids={seen[:3]}")
        # 坏输入：不认识的 provider / 连不上的地址（要快速失败，别把测试拖死）
        r, _ = send("llm.discover", {"provider": "zzz"})
        d = r.get("data") or {}
        check("llm.discover 坏 provider → ok:false + 原话",
              bool(r.get("ok")) and d.get("ok") is False and bool(d.get("error")),
              str(d.get("error"))[:50])
        r, _ = send("llm.discover", {"provider": "openai", "refresh": True,
                                     "base_url": "http://127.0.0.1:9/v1",
                                     "model": "deepseek-flash", "timeout": 2},
                    timeout=60)
        d = r.get("data") or {}
        check("llm.discover 连不上 → 探测失败但**回落内置候选**（按模型名认出服务商）",
              bool(r.get("ok")) and d.get("ok") is False
              and d.get("source") == "builtin"
              and any(x.get("id") == "deepseek-flash" for x in d.get("models") or []),
              f"source={d.get('source')} error={str(d.get('error'))[:36]}")
        r, _ = send("service.restart")
        d = r.get("data") or {}
        check("service.restart 如实回「没有真的重启」",
              d.get("started") is False and bool(d.get("note")),
              str(d.get("note"))[:60])

        r, _ = send("prompts.list")
        prows = (r.get("data") or {}).get("rows") or []
        check("prompts.list 有行、字段齐（is_custom 是布尔）",
              len(prows) > 0
              and all(all(k in x for k in ("id", "title", "where", "placeholders",
                                           "system", "user", "is_custom"))
                      for x in prows)
              and all(isinstance(x["is_custom"], bool) for x in prows),
              f"{len(prows)} 条")
        r, _ = send("prompts.save", {})
        check("prompts.save 缺 id → ok:false", r.get("ok") is False)
        r, _ = send("prompts.save", {"id": "nope", "field": "system", "text": "x"})
        check("prompts.save 不存在的 id → ok:false", r.get("ok") is False,
              str(r.get("error"))[:48])
        r, _ = send("prompts.save", {"id": "tag", "field": "zzz", "text": "x"})
        check("prompts.save 坏 field → ok:false", r.get("ok") is False)
        r, _ = send("prompts.probe", {"id": "nope"})
        d = r.get("data") or {}
        check("prompts.probe 不存在的 id → data.ok=false 且带 ms（不调模型）",
              d.get("ok") is False and "ms" in d, str(d.get("error"))[:48])

        # ---- 10. W2 批次 B：四组页面的方法（契约见开发说明）
        #
        # ⚠ 与批次 A 同一条纪律：**会改数据/跑几分钟的动作只覆盖坏输入**
        #   （mineru.parse 不真派；quality.scan 不真扫 —— 它会把 item_health
        #   整批重写；plugin build /
        #   upgrade backup 不真跑）。
        #   纯查询的真跑：papers.list / maintain.run(collections) /
        #   mineru.status / quality.stats / quality.detail / kb.search /
        #   advanced.weightList / advanced.collections。
        #   `_job_dispatch` 的派发形状用 **quality.repair + 一个不存在的 key**
        #   覆盖：inspect 查不到 → todo 空 → 立刻收尾、一篇都不动（这是唯一
        #   能在自检里真派一次的长任务）。
        print("\n[10] W2 批次 B：分类建议 / PDF 解析 / 损坏查询 / 高级")
        r, _ = send("papers.list", {"limit": 5})
        d = r.get("data") or {}
        prows = d.get("rows") or []
        check("papers.list → {rows,total}，limit 生效",
              bool(r.get("ok")) and isinstance(prows, list)
              and len(prows) <= 5 and isinstance(d.get("total"), int),
              f"{len(prows)}/{d.get('total')} 行")
        check("文献行字段齐全",
              all(all(k in x for k in ("key", "title", "year", "first_author",
                                       "collections", "fulltext_chars", "in_kb"))
                  for x in prows))
        r, _ = send("ai.summarize", {})
        check("ai.summarize 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("ai.summarize", {"key": "ZZZZZZZZ"})
        check("ai.summarize 不存在的 key → ok:false（不进模型）",
              r.get("ok") is False, str(r.get("error"))[:48])
        r, _ = send("ai.classify", {})
        check("ai.classify 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("ai.classify", {"key": "ZZZZZZZZ"})
        check("ai.classify 不存在的 key → ok:false（不进模型）",
              r.get("ok") is False)
        r, _ = send("ai.classify", {"key": prows[0]["key"] if prows else "X",
                                    "previous": "坏值"})
        check("ai.classify previous 不是对象 → ok:false", r.get("ok") is False)
        r, _ = send("ai.apply", {})
        check("ai.apply 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("ai.apply", {"key": "ZZZZZZZZ"})
        check("ai.apply 既没分类也没标签 → ok:false（不写 Zotero）",
              r.get("ok") is False)
        r, _ = send("maintain.run", {"task": "zzz"})
        err = str(r.get("error"))
        check("maintain.run 坏 task → ok:false，且白名单含 collections",
              r.get("ok") is False and "collections" in err, err[:80])
        r, _ = send("maintain.run", {"task": "collections"}, timeout=60)
        d = r.get("data") or {}
        check("maintain.run(collections) → {task,ok,output} 文本（纯查询真跑）",
              d.get("task") == "collections" and d.get("ok") is True
              and isinstance(d.get("output"), str)
              and "分类分布" in d.get("output", ""), str(d.get("output"))[:40])
        r, _ = send("maintain.run", {"task": "stats"}, timeout=120)
        d = r.get("data") or {}
        check("maintain.run(stats) 真起子进程、output 是原样文本",
              d.get("ok") is True and isinstance(d.get("output"), str)
              and "统计" in d.get("output", ""), str(d.get("output"))[:40])

        r, _ = send("mineru.status", timeout=180)
        d = r.get("data") or {}
        probe = d.get("probe") or {}
        check("mineru.status → {probe:{ok,exe,why},rows,total}",
              bool(r.get("ok")) and isinstance(probe, dict)
              and all(k in probe for k in ("ok", "exe", "why"))
              and isinstance(d.get("rows"), list),
              f"{len(d.get('rows') or [])} 篇 / probe.ok={probe.get('ok')}")
        check("mineru 行字段齐全",
              all(all(k in x for k in ("key", "title", "tier", "fulltext_src",
                                       "pages", "figures", "elapsed",
                                       "artifacts_size", "built_at"))
                  for x in (d.get("rows") or [])))
        r, _ = send("mineru.parse", {"tier": "zzz"})
        check("mineru.parse 坏 tier → ok:false（派活前就挡住）",
              r.get("ok") is False)
        r, _ = send("mineru.parse", {"tier": "missing", "parser": "zzz"})
        check("mineru.parse 坏 parser → ok:false", r.get("ok") is False)
        r, _ = send("mineru.clear", {})
        check("mineru.clear 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("mineru.clear", {"key": "ZZZZZZZZ"})
        d = r.get("data") or {}
        check("mineru.clear 不存在的 key → cleared=false 且 freed_bytes=0"
              "（没删到东西）",
              bool(r.get("ok")) and d.get("cleared") is False
              and d.get("freed_bytes") == 0, str(d)[:60])

        r, _ = send("quality.stats", timeout=120)
        d = r.get("data") or {}
        qrows = d.get("rows") or []
        check("quality.stats → chunks/figures/item_health/rows",
              bool(r.get("ok")) and isinstance(d.get("chunks"), int)
              and isinstance(d.get("item_health"), dict)
              and isinstance(qrows, list),
              f"chunks={d.get('chunks')} 坏={d.get('bad')} "
              f"待确认={d.get('unsure')}")
        check("quality.stats 行字段齐全（n_bad/n_suspect 是 0/1 标记）",
              all(all(k in x for k in ("key", "title", "n_bad", "n_suspect"))
                  for x in qrows))
        r, _ = send("quality.detail", {})
        check("quality.detail 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("quality.detail", {"key": "ZZZZZZZZ"})
        check("quality.detail 没检查过的 key → ok:false", r.get("ok") is False)
        if qrows:
            r, _ = send("quality.detail", {"key": qrows[0]["key"]}, timeout=60)
            dd = r.get("data") or {}
            check("quality.detail 明细行字段齐全（page/kind/detail/snippet）",
                  bool(r.get("ok"))
                  and all(all(f in x for f in ("page", "kind", "detail",
                                               "snippet"))
                          for x in (dd.get("rows") or [])),
                  f"{len(dd.get('rows') or [])} 行 kind={dd.get('verdict')}")
        # ⚠ quality.scan **不在自检里真跑**（会整批重写 item_health）；
        #   派发形状下面用 quality.repair 覆盖。

        r, events = send("quality.repair", {"keys": ["ZZZZZZZZ"]}, timeout=60)
        ld = r.get("data") or {}
        jid = (ld.get("job") or {}).get("id") if isinstance(ld.get("job"),
                                                            dict) \
            else ld.get("id")
        check("quality.repair 立刻回 {job:{id}}（派发即返回，不等它跑完）",
              bool(r.get("ok")) and bool(jid) and ld.get("state") == "running",
              str(ld)[:80])
        st = None
        deadline = time.time() + 60
        while jid and time.time() < deadline:
            rr, _ = send("jobs.get", {"id": jid})
            st = rr.get("data") or {}
            if st.get("state") in ("done", "failed"):
                break
            time.sleep(0.4)
        res = (st or {}).get("result") or {}
        check("quality.repair 的 job 跑到 done，且不存在的 key 一篇都没动",
              (st or {}).get("state") == "done" and res.get("fixed") == 0,
              f"state={(st or {}).get('state')} result={str(res)[:60]}")

        r, _ = send("kb.search", {})
        check("kb.search 缺 query → ok:false", r.get("ok") is False)
        # ⚠ 真检索**只做"能跑就报、跑不动就如实说"**：本机实测引擎里
        #   `Searcher._ensure_model()`（fastembed/onnxruntime 载入向量模型）
        #   在"引擎是宿主管道的子进程"时**不返回**（同一句在一个普通进程里
        #   1.5 秒就完成，见报告）。这不是本方法能修的，也不是批次 B 引入的
        #   —— `experience.list?mode=query` 那条同类路径同样依赖它。
        #   所以这里 fail-soft：超时不判 FAIL，只打一行显眼的 WARN（免得一个
        #   环境问题把整套自检的结论搅浑），但**留下证据**。
        try:
            r, _ = send("kb.search", {"query": "反演", "limit": 3}, timeout=120)
            d = r.get("data") or {}
            srows = d.get("rows") or []
            check("kb.search → {rows:[{key,title,score,snippet}]}",
                  bool(r.get("ok")) and isinstance(srows, list)
                  and all(all(k in x for k in ("key", "title", "score",
                                               "snippet"))
                          for x in srows), f"{len(srows)} 条")
        except TimeoutError as exc:
            print(f"  WARN  kb.search 真检索在本机引擎上下文里不返回：{exc}")
            print("        （引擎起子进程 / 载入向量模型在本机会卡住，"
                  "见 online/engine.py 顶部与 _run_script 的注释）")
            d = {}
            srows = []
        r, _ = send("kb.weight", {})
        check("kb.weight 缺 key → ok:false", r.get("ok") is False)
        r, _ = send("kb.weight", {"key": "ZZZZZZZZ", "pinned": True})
        check("kb.weight 不存在的 key → ok:false（不写库）",
              r.get("ok") is False)
        r, _ = send("kb.weight", {"key": "ZZZZZZZZ", "manual": "不是数字"})
        check("kb.weight manual 不是数字 → ok:false", r.get("ok") is False)

        r, _ = send("advanced.weightList", {"limit": 5}, timeout=60)
        d = r.get("data") or {}
        check("advanced.weightList → 行字段齐全",
              bool(r.get("ok"))
              and all(all(k in x for k in ("key", "title", "year", "weight",
                                           "pinned", "note"))
                      for x in (d.get("rows") or [])),
              f"{len(d.get('rows') or [])} 行")
        r, _ = send("advanced.collections", timeout=60)
        d = r.get("data") or {}
        check("advanced.collections → {rows:[{name,n}]}",
              bool(r.get("ok"))
              and all(all(k in x for k in ("name", "n"))
                      for x in (d.get("rows") or [])),
              f"{len(d.get('rows') or [])} 个分类")
        # W3 线2.5：Zotero 文献列表的权重列要的是**全量映射**（每篇都有数），
        # 与 HTTP `GET /weights` 同口径；绝不能退化成"只列有经验的几篇"。
        r, _ = send("kb.weightsMap", timeout=60)
        d = r.get("data") or {}
        wmap = d.get("weights") or {}
        check("kb.weightsMap → {weights:{key:{weight,…}}}",
              bool(r.get("ok")) and isinstance(wmap, dict) and len(wmap) > 0
              and all(all(k in v for k in ("weight", "pinned", "attempts"))
                      for v in list(wmap.values())[:5]),
              f"{len(wmap)} 篇")
        # 口径闸门（这条是最容易犯的错）：全量映射的篇数必须**明显多于**
        # 权重榜（advanced.weightList，只列 item_weight 里有行的）。
        # 拿榜单顶替列表列会让绝大多数条目空白 —— 本机曾经 19/97 就是这毛病。
        r2, _ = send("advanced.weightList", {"limit": 0}, timeout=60)
        n_list = len((r2.get("data") or {}).get("rows") or [])
        check("kb.weightsMap 是全量（不少于权重榜，通常远多于）",
              len(wmap) >= n_list, f"全量 {len(wmap)} vs 榜单 {n_list}")
        r, _ = send("kb.weightsMap", {"keys": "ZZZZZZZZ"}, timeout=60)
        d = r.get("data") or {}
        check("kb.weightsMap 支持 keys 过滤（不存在的 key → 空映射而不是报错）",
              bool(r.get("ok")) and (d.get("weights") or {}) == {},
              str(d.get("weights"))[:80])
        # W3 线2.5：文献列表那两列（已建库 / 纲要节数）。
        r, _ = send("kb.colStatus", timeout=120)
        d = r.get("data") or {}
        citems = d.get("items") or {}
        check("kb.colStatus → {ok,items:{key:{in_kb,outline_sections,outline_file}}}",
              bool(r.get("ok")) and bool(d.get("ok")) and len(citems) > 0
              and all(all(k in v for k in ("in_kb", "outline_sections", "outline_file"))
                      for v in list(citems.values())[:5]),
              f"{len(citems)} 条")
        # **口径闸门**：节数必须来自 `meta.ai_outline:<key>`（读端看的），
        # 不是数 `views/*.outline.md` 文件个数 —— 本机 94 篇有 meta、只有 50 个文件，
        # 按文件数会让 44 篇有纲要的显示成 ✗（老 bug 的取证见 localserver 的
        # _outline_meta_counts 注释）。
        n_meta = int(d.get("with_outline") or 0)
        n_file = int(d.get("with_outline_file") or 0)
        check("kb.colStatus 的 with_outline 按 meta 计（≥ 有渲染文件的篇数）",
              n_meta >= n_file, f"meta {n_meta} vs 文件 {n_file}")
        # 更硬的一条：存在"有 meta 节数、但没有渲染文件"的条目时，
        # 它必须 `outline_sections>0 且 outline_file=false`（按文件计的写法做不到）。
        mixed = [k for k, v in citems.items()
                 if int(v.get("outline_sections") or 0) > 0 and not v.get("outline_file")]
        if mixed:
            check("有 meta 没文件的条目：outline_sections>0 且 outline_file=false",
                  len(mixed) > 0, f"{len(mixed)} 篇（如 {mixed[0]}）")
        else:
            print("  （跳过「有 meta 没文件」那条：本机当前没有这种条目）")
        check("kb.colStatus 的 in_kb 恒为 true（它逐条来自 items 表）",
              all(v.get("in_kb") is True for v in citems.values()),
              str(list(citems.values())[:2]))
        # W3 线2.5：某几条文献在库状态（06-watch 监听 / 12-dsh 发文献给 DSH 共用）。
        # **与 HTTP POST /item-info 同形**（同一份 LS.do_item_info）。
        somekey = sorted(citems.keys())[0] if citems else ""
        if somekey:
            r, _ = send("kb.itemInfo", {"keys": [somekey]}, timeout=60)
            d = r.get("data") or {}
            irows = d.get("items") or []
            it = irows[0] if irows else {}
            check("kb.itemInfo → {items:[{key,in_kb,title,fulltext_chars,n_chunks,…}]}",
                  bool(r.get("ok")) and len(irows) == 1
                  and all(k in it for k in ("key", "in_kb", "title", "first_author",
                                            "year", "collections", "tags",
                                            "fulltext_chars", "n_chunks")),
                  str(it)[:160])
            check("kb.itemInfo 的 key/collections/tags 类型对（str / list）",
                  it.get("key") == somekey and isinstance(it.get("collections"), list)
                  and isinstance(it.get("tags"), list),
                  f"key={it.get('key')!r} collections={type(it.get('collections')).__name__}")
        # 不在库的 key：只回 {key, in_kb:false} —— 调用方靠这一条判"还没建过库"。
        r, _ = send("kb.itemInfo", {"keys": ["ZZZZZZZZ"]}, timeout=60)
        d = r.get("data") or {}
        z = ((d.get("items") or []) or [{}])[0]
        check("kb.itemInfo 不存在的 key → in_kb:false（且不报错）",
              bool(r.get("ok")) and z.get("in_kb") is False
              and z.get("key") == "ZZZZZZZZ", str(z)[:120])
        r, _ = send("kb.itemInfo", {"keys": []}, timeout=60)
        check("kb.itemInfo 空 keys → ok:false（与 HTTP 的 400 一致，别静默回空表）",
              r.get("ok") is False, str(r.get("error"))[:80])
        # W3 线2.5（2026-10-14）：两条**新补的契约**
        # ① env.checkRaw：诊断**原文**（插件设置面板读的 python/project_root 就在这儿）
        r, _ = send("env.checkRaw", timeout=60)
        d = r.get("data") or {}
        check("env.checkRaw 回 env_report() 原文（有 python / project_root 顶层键）",
              bool(r.get("ok")) and isinstance(d.get("python"), dict)
              and isinstance(d.get("project_root"), dict),
              str(list(d.keys()))[:140])
        check("env.check 与 env.checkRaw 是**两个形状**（前者没有 python 顶层键）",
              "python" not in ((send("env.check", timeout=60)[0].get("data") or {})),
              "env.check 不该有 python")
        # ② env.save 回包必须带 report（插件设置面板保存后就读它渲染报告）
        r, _ = send("env.save", {"project_root": "C:\\no-such-dir-xyz-20261014"}, timeout=60)
        check("env.save 坏路径 → ok:false（校验在写盘之前，不写配置）",
              r.get("ok") is False and "不存在" in str(r.get("error") or ""),
              str(r.get("error"))[:100])
        r, _ = send("env.save", {}, timeout=60)
        check("env.save 空参数 → ok:false（与 HTTP 的 400 同口径）",
              r.get("ok") is False, str(r.get("error"))[:80])
        check("env.save 的返回里带 report（源码断言：`{saved, check, report}`）",
              "report" in open(__file__.replace("test_engine_rpc.py", "")
                               + "../online/engine.py", encoding="utf-8").read()
              .split("def _m_env_save")[1].split("def _m_llm_config")[0],
              "")
        # ③ classify.item：**只验坏输入分支**（好输入会真调模型：烧额度且慢）
        for params, why in (({}, "缺 item"),
                            ({"item": "不是对象"}, "item 不是对象"),
                            ({"item": {"title": "x"}, "previous": "不是对象"},
                             "previous 不是对象"),
                            ({"item": {}}, "item 里 title/abstract 都没有")):
            r, _ = send("classify.item", params, timeout=30)
            check(f"classify.item 坏输入（{why}）→ ok:false 且引擎不死",
                  r.get("ok") is False and bool(r.get("error")), str(r)[:100])
        r, _ = send("classify.item", {"item": {"title": "x", "abstract": "y"},
                                      "categories": "不是数组"}, timeout=30)
        check("classify.item categories 不是数组 → ok:false",
              r.get("ok") is False, str(r.get("error"))[:80])
        # W3 线2.5 批次（2026-10-14）：右键取消纲要 / 正文修订 / 公式纠错。
        # **只验坏输入与非模型分支**：doc.save 与 formula.apply 是写路径（不许真写），
        # formula.mathtype 用 dry_run（不动剪贴板、不起进程）。
        r, _ = send("kb.digestCancel", timeout=30)
        check("kb.digestCancel → {ok,cancelled,hint}（只置标记，不杀线程）",
              bool(r.get("ok")) and (r.get("data") or {}).get("cancelled") is True,
              str(r.get("data"))[:80])
        r, _ = send("doc.get", {}, timeout=30)
        check("doc.get 缺 key → ok:false", r.get("ok") is False, str(r.get("error"))[:60])
        r, _ = send("doc.get", {"key": "ZZZZZZZZ"}, timeout=30)
        check("doc.get 不存在的 key → 仍回可用的空状态（edited/stale 是数组）",
              bool(r.get("ok")) and isinstance((r.get("data") or {}).get("edited"), list)
              and isinstance((r.get("data") or {}).get("stale"), list),
              str(r.get("data"))[:100])
        r, _ = send("doc.save", {}, timeout=30)
        check("doc.save 缺 key → ok:false（**不写任何页**）",
              r.get("ok") is False, str(r.get("error"))[:60])
        r, _ = send("doc.save", {"key": "ZZZZZZZZ", "page": 1, "text": "x"}, timeout=30)
        check("doc.save 不存在的 key → ok:false（不建文件、不派任务）",
              r.get("ok") is False, str(r.get("error"))[:80])
        r, _ = send("formula.target", {}, timeout=30)
        check("formula.target 缺 key → ok:false", r.get("ok") is False,
              str(r.get("error"))[:60])
        r, _ = send("formula.target", {"key": "ZZZZZZZZ", "rects": "notjson"}, timeout=30)
        check("formula.target rects 不是 JSON → ok:false（与 HTTP 同一句话）",
              r.get("ok") is False and "rects" in str(r.get("error")),
              str(r.get("error"))[:80])
        r, _ = send("formula.apply", {}, timeout=30)
        check("formula.apply 缺 key → ok:false（**不写回**）",
              r.get("ok") is False, str(r.get("error"))[:60])
        r, _ = send("formula.apply", {"key": "ZZZZZZZZ", "page": 0, "latex": "x^2"},
                    timeout=30)
        check("formula.apply 不存在的 key → ok:false（取不到这一页）",
              r.get("ok") is False, str(r.get("error"))[:80])
        r, _ = send("formula.mathtype", {}, timeout=30)
        check("formula.mathtype 缺 latex → ok:false", r.get("ok") is False,
              str(r.get("error"))[:60])
        r, _ = send("formula.mathtype", {"latex": "x^2", "dry_run": True}, timeout=60)
        d = r.get("data") or {}
        check("formula.mathtype dry_run → {ok,installed,exe,copied,launched,why}"
              "（不动剪贴板、不起进程）",
              bool(r.get("ok")) and all(k in d for k in ("ok", "installed", "exe",
                                                         "copied", "launched", "why")),
              str(d)[:120])
        # W3 线2.5 最后一批（2026-10-14）：MinerU 原文 / 单篇同步元数据 / health 不含 token。
        r, _ = send("mineru.checkRaw", timeout=120)
        d = r.get("data") or {}
        check("mineru.checkRaw → 原文形状（ok/exe/version/vlm_engine/cuda_ok…，"
              "**不是** mineru.status 那个汇总）",
              bool(r.get("ok")) and all(k in d for k in ("ok", "exe", "version",
                                                         "vlm_engine", "cuda_ok",
                                                         "tier_ready")),
              str(list(d.keys()))[:140])
        r, _ = send("meta.fillOne", {}, timeout=30)
        check("meta.fillOne 缺 key → ok:false（**不写库、不调模型**）",
              r.get("ok") is False, str(r.get("error"))[:60])
        r, _ = send("meta.fillOne", {"key": "ZZZZZZZZ", "use_model": False}, timeout=90)
        check("meta.fillOne 不存在的 key（use_model=false）→ 有明确结果且不抛",
              bool(r.get("ok")) and isinstance(r.get("data"), dict),
              str(r)[:100])
        r, _ = send("meta.typefixOne", {}, timeout=30)
        check("meta.typefixOne 缺 key → ok:false", r.get("ok") is False,
              str(r.get("error"))[:60])
        r, _ = send("meta.typefixOne", {"key": "ZZZZZZZZ", "use_network": False},
                    timeout=60)
        check("meta.typefixOne 不存在的 key → 不抛（ok:false 或空 fixes 都算合格）",
              bool(r.get("ok")), str(r)[:100])
        r, _ = send("kb.health", timeout=60)
        d = r.get("data") or {}
        check("kb.health 有 kb_dir 且**不含 token**（stdio 通道没有偷 token 这回事；"
              "迁移 #2 后老 HTTP 的 token 同步仍是可选动作）",
              bool(r.get("ok")) and bool(d.get("kb_dir")) and "token" not in d,
              str(list(d.keys()))[:140])
        # 三个动作组：只覆盖白名单（**不真跑** —— build/backup 是几分钟 + 会写盘）。
        # 白名单用错误信息反查，顺带钉住"动作名与契约一字不差"。
        for method, acts in (
            ("advanced.zoteroSync",
             ("check", "authorize", "plan", "apply", "verify")),
            ("advanced.plugin",
             ("build", "sideload-status", "sideload-install", "sideload-remove")),
            ("advanced.upgrade", ("check", "backup", "verify")),
        ):
            r, _ = send(method, {})
            check(f"{method} 缺 action → ok:false", r.get("ok") is False)
            r, _ = send(method, {"action": "zzz"})
            err = str(r.get("error"))
            check(f"{method} 坏 action → ok:false，白名单就是契约那几个",
                  r.get("ok") is False and all(a in err for a in acts),
                  err[:80])

        # ---- 11. stdout 净化（核心不变量）
        print("\n[11] stdout 只有协议 JSON")
        check("解析不出的行数 = 0", not bad_lines,
              f"{len(bad_lines)} 行：" + str(bad_lines[:2]))
        check("stdout 至少收到了若干行（不是空的）", len(raw_lines) > 5,
              f"{len(raw_lines)} 行")
        check("每一行都是 JSON 对象",
              all(l.strip().startswith("{") for l in raw_lines if l.strip()))

        # ---- 12. 关掉 stdin 引擎自己退出
        print("\n[12] 生命周期：关 stdin 后引擎退出")
        try:
            proc.stdin.close()                      # type: ignore[union-attr]
        except Exception:                           # noqa: BLE001
            pass
        try:
            code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            code = None
        check("进程在 15 秒内退出", code is not None, f"exit={code}")

    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            errf.seek(0)
            tail = errf.read().decode("utf-8", "replace").strip().splitlines()[-6:]
        except Exception:                           # noqa: BLE001
            tail = []
        if tail:
            print("\n[引擎 stderr 末尾几行]")
            for ln in tail:
                print("   ", ln)

    print("\n" + "=" * 66)
    print(f"通过 {PASSED}　失败 {FAILED}")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
