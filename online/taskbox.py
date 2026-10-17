# -*- coding: utf-8 -*-
"""任务信箱：**DSH → Zotero 插件的唯一派活通道**（W3 线2；2026-10 起唯一）。

原来的常驻 HTTP 服务（127.0.0.1:8765 的 `/task`）与其队列**已整体删除**，
这条通道现在是文件即消息：

    <知识库>\\mailbox\\
      requests\\<id>.json   ← 派发方写（先写 .tmp 再 rename，读者永远看到完整文件）
      claimed\\<id>.json    ← 插件**移动**过来 = 领取（rename 原子，所以只会被执行一次）
      results\\<id>.json    ← 插件写回结果，派发方读它拿 state/result/error

方向与 `zotero-bridge` 的 FLAG_DIR 那套一致（文件即消息、轮询领取），
只是这条是 DSH → Zotero。**没有端口、没有 token、没有常驻进程**：
Zotero 关着时任务就静静躺在 requests\\ 里。

三条语义（刻意与老的 HTTP 队列**逐条对齐**，调用方不必重新想）
------------------------------------------------------------
1. **状态机**：pending（请求文件在）→ running（被 move 进 claimed\\）→
   done / failed（results\\ 里有了）。`state()` 返回的字典形状与老
   `GET /task/<id>` 的响应几乎一样（id/kind/state/created/started/finished/
   result/error/fetched_by/timeout），所以 `acquire.py` 那种老代码改动很小。
2. **撤回只能撤"还没被领取"的**：`cancel()` 删掉 requests\\ 里的那条；
   已经进 claimed\\ 的撤不掉（正在跑），返回里如实说明 —— 这正是老队列
   `_task_cancel` 的口径，也是真机上踩过的"派发方早报失败、几小时后 Zotero
   一开却把条目建出来了"那个坑的修法。
3. **结果只在 results\\ 里取**：派发方读不到就继续等，等不到就自己决定放弃
   （放弃前务必 `cancel()`，否则 Zotero 一开照样执行）。

不引入新依赖：只用标准库（json / os / time / uuid）。写文件一律 UTF-8 无 BOM。
"""
from __future__ import annotations

import json
import os
import random
import time

DIR_NAME = "mailbox"
SUBDIRS = ("requests", "claimed", "results")

# 结果文件留多久（插件启动时顺手清）：7 天足够任何排查了。
KEEP_SECONDS = 7 * 24 * 3600


# ---------------------------------------------------------------- 目录解析


def default_kb_dir() -> str:
    """知识库目录：优先 `schemas.KB_DIR`（仓库里的唯一事实定义），否则按约定兜底。

    能 import 到 schemas 就用它 —— 它认得 `KB_DIR` 环境变量、repo 里的
    `kb-location.json`、Zotero 数据目录三档；本模块被独立脚本（不在 online/ 的
    sys.path 上）使用时 import 会失败，那就退回同一套约定的前两档。
    """
    env = str(os.environ.get("KB_DIR") or "").strip()
    if env:
        return env
    try:
        import schemas as _S          # noqa: PLC0415
        kb = str(getattr(_S, "KB_DIR", "") or "").strip()
        if kb:
            return kb
    except Exception:                 # noqa: BLE001
        pass
    return os.path.join(os.path.expanduser("~"), "Zotero", "zotero-kb")


def box_dir(kb_dir: str = "", create: bool = True) -> str:
    """信箱根目录 = `<知识库>\\mailbox`。create=True 时顺手建好三个子目录。"""
    root = os.path.join(str(kb_dir or default_kb_dir()).rstrip("\\/"), DIR_NAME)
    if create:
        for sub in SUBDIRS:
            os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def _sub(kb_dir: str, name: str, create: bool = True) -> str:
    return os.path.join(box_dir(kb_dir, create=create), name)


# ---------------------------------------------------------------- 读写助手


def _read_json(path: str):
    """读 JSON：读不到 / 半截 / 坏内容一律返回 None（调用方当下一个状态处理）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                 # noqa: BLE001
        return None


def _write_atomic(path: str, obj) -> None:
    """先写 .tmp 再 rename —— 读者永远看不到半截文件。

    ⚠ rename 在同一目录内是原子的；跨盘不原子，所以 .tmp 必须与目标同目录。
    json.dumps 用 ensure_ascii=False + utf-8：中文标题/DOI 进去是给人看的原文。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _new_id() -> str:
    """任务 id：时间前缀（可排序）+ 随机尾巴（并发也不撞）。

    为什么不用递增序号：老 HTTP 队列的 `t1/t2/...` 在**服务重启后从 1 开始**，
    日志里两条不同的任务会同名。时间戳+随机串没有这个问题。
    """
    return "m%d-%04x" % (int(time.time() * 1000), random.randint(0, 0xFFFF))


# ---------------------------------------------------------------- 派发 / 查询


def send(kind: str, code: str, timeout: float = 60.0, kb_dir: str = "") -> dict:
    """派一个任务给 Zotero 插件。返回任务字典（含 id）。

    kind 与老队列一致：`js`（执行任意 JS 源码）/ `cmd`（插件内建命令）/
    `acquire`（DOI 清单入库）/ `attach`（挂本地 PDF）。`code` 对
    acquire/attach 是 **JSON 字符串**（与老实现逐字一致，插件那边这么解）。
    """
    tid = _new_id()
    task = {
        "id": tid,
        "kind": str(kind or "js"),
        "code": code if code is not None else "",
        "timeout": float(timeout or 60.0),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "from": "dsh",
        "pid": os.getpid(),
    }
    _write_atomic(os.path.join(_sub(kb_dir, "requests"), tid + ".json"), task)
    return task


def state(tid: str, kb_dir: str = "") -> dict | None:
    """查一条任务的状态；没有这条任务时返回 None。

    返回的字典形状尽量贴近老 `GET /task/<id>`（多一个 `channel: "mailbox"` 便于排查）。
    """
    tid = str(tid or "").strip()
    if not tid:
        return None
    done = _read_json(os.path.join(_sub(kb_dir, "results"), tid + ".json"))
    if isinstance(done, dict):
        out = dict(done)
        out["state"] = str(out.get("state") or ("done" if out.get("ok") else "failed"))
        out["channel"] = "mailbox"
        return out
    req = (_read_json(os.path.join(_sub(kb_dir, "claimed"), tid + ".json"))
           or _read_json(os.path.join(_sub(kb_dir, "requests"), tid + ".json")))
    if not isinstance(req, dict):
        return None
    out = {k: v for k, v in req.items() if k != "code"}
    out["state"] = "running" if os.path.exists(
        os.path.join(_sub(kb_dir, "claimed"), tid + ".json")) else "pending"
    out["result"] = None
    out["error"] = ""
    out["fetched_by"] = "plugin" if out["state"] == "running" else ""
    out["channel"] = "mailbox"
    return out


def cancel(tid: str, kb_dir: str = "") -> dict:
    """撤回一条**还没被领取**的任务（等价于老 `POST /task/cancel`）。

    只删 requests\\ 里那条。已经进 claimed\\ 的撤不掉（正在跑）—— 如实回报，
    别让调用方以为一定撤掉了（老实现踩过：以为撤了、其实几小时后被建了条目）。
    """
    tid = str(tid or "").strip()
    req = os.path.join(_sub(kb_dir, "requests"), tid + ".json")
    # ⚠ **先看结果、再看 claimed**：一条任务可能既有 claimed 文件又有 results 文件
    #   （插件领取后跑完就是这形状）。反过来判会把"已经结束了"报成"正在跑"，
    #   调用方据此以为"撤不掉，可能还在建条目" —— 白紧张一场。
    if os.path.exists(os.path.join(_sub(kb_dir, "results"), tid + ".json")):
        return {"ok": False, "cancelled": False, "state": "finished",
                "reason": "任务已经结束，无需撤回"}
    if os.path.exists(os.path.join(_sub(kb_dir, "claimed"), tid + ".json")):
        return {"ok": False, "cancelled": False, "state": "running",
                "reason": "任务已经被插件领取，撤不掉（等它自己结束）"}
    try:
        os.remove(req)
    except FileNotFoundError:
        return {"ok": False, "cancelled": False, "state": "missing",
                "reason": "requests 里没有这条任务（可能刚被领取，或早就没了）"}
    except OSError as exc:
        return {"ok": False, "cancelled": False, "state": "pending",
                "reason": "删除请求文件失败：%s" % exc}
    return {"ok": True, "cancelled": True, "state": "cancelled"}


def pending(kb_dir: str = "") -> list[dict]:
    """还没被领取的任务（诊断用）。"""
    out = []
    try:
        names = sorted(os.listdir(_sub(kb_dir, "requests", create=False)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        item = _read_json(os.path.join(_sub(kb_dir, "requests"), name))
        if isinstance(item, dict):
            out.append({k: v for k, v in item.items() if k != "code"})
    return out


def wait(tid: str, timeout: float = 90.0, poll: float = 1.0, kb_dir: str = "",
         on_state=None) -> dict | None:
    """等到 done/failed 或超时。返回最后一次 `state()`（超时返回当前状态，不抛）。

    `on_state(state_dict)` 在**状态变化时**回调一次（给 CLI 打印进度用）。
    """
    deadline = time.time() + max(0.0, float(timeout or 0.0))
    last_state = ""
    st = state(tid, kb_dir)
    while True:
        if st is not None and st.get("state") != last_state:
            last_state = str(st.get("state"))
            if on_state:
                try:
                    on_state(st)
                except Exception:     # noqa: BLE001
                    pass
        if st is not None and st.get("state") in ("done", "failed"):
            return st
        if time.time() >= deadline:
            return st
        time.sleep(max(0.1, poll))
        st = state(tid, kb_dir)


def cleanup(kb_dir: str = "", keep_seconds: float = KEEP_SECONDS) -> int:
    """删掉过期的 results / claimed 残留，返回删了几个文件。

    为什么要有：results\\ 会随每次派活长一个文件，没人清就是慢性垃圾。
    **只清过期的**（默认 7 天），正在跑的任务与刚写的结果绝不动。
    """
    now = time.time()
    removed = 0
    for sub in ("results", "claimed"):
        d = _sub(kb_dir, sub, create=False)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            p = os.path.join(d, name)
            try:
                if now - os.path.getmtime(p) > keep_seconds:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- CLI（自检）


def _selftest() -> int:
    """不碰 Zotero 的自检：临时目录里跑一遍完整状态机。"""
    import tempfile

    ok = 0
    bad = 0

    def check(name, cond, detail=""):
        nonlocal ok, bad
        if cond:
            ok += 1
            print("  PASS  " + name)
        else:
            bad += 1
            print("  FAIL  " + name + "  " + str(detail))

    with tempfile.TemporaryDirectory(prefix="kb_taskbox_") as tmp:
        print("[1] 派发 → 状态机（pending → running → done）")
        t = send("js", "return 1", kb_dir=tmp, timeout=30)
        check("send 返回 id", bool(t.get("id")), t)
        check("刚派发 = pending", (state(t["id"], tmp) or {}).get("state") == "pending",
              state(t["id"], tmp))
        check("请求文件真的在 requests\\",
              os.path.exists(os.path.join(_sub(tmp, "requests"), t["id"] + ".json")))
        # 手工扮演插件：领取 = move 到 claimed\
        os.replace(os.path.join(_sub(tmp, "requests"), t["id"] + ".json"),
                   os.path.join(_sub(tmp, "claimed"), t["id"] + ".json"))
        st = state(t["id"], tmp) or {}
        check("被领取 = running 且 fetched_by=plugin",
              st.get("state") == "running" and st.get("fetched_by") == "plugin", st)
        check("running 时不回吐 code（面板/日志里看不到源码）", "code" not in st, st)
        # 插件写结果
        _write_atomic(os.path.join(_sub(tmp, "results"), t["id"] + ".json"),
                      {"id": t["id"], "state": "done", "ok": True,
                       "result": {"value": "42", "ms": 3}})
        st = state(t["id"], tmp) or {}
        check("结果写回 = done + result",
              st.get("state") == "done" and st.get("result", {}).get("value") == "42", st)

        print("[2] 撤回只能撤 pending")
        t2 = send("cmd", "status", kb_dir=tmp)
        r = cancel(t2["id"], tmp)
        check("pending 能撤", r.get("cancelled") is True, r)
        check("撤了以后 state 是 missing", state(t2["id"], tmp) is None,
              state(t2["id"], tmp))
        t3 = send("js", "return 2", kb_dir=tmp)
        os.replace(os.path.join(_sub(tmp, "requests"), t3["id"] + ".json"),
                   os.path.join(_sub(tmp, "claimed"), t3["id"] + ".json"))
        r = cancel(t3["id"], tmp)
        check("已领取的撤不掉，且如实说 running",
              r.get("cancelled") is False and r.get("state") == "running", r)

        print("[3] wait 的行为")
        t4 = send("js", "return 3", kb_dir=tmp)
        # 照真实顺序：插件先领取（move 进 claimed\）再写结果
        os.replace(os.path.join(_sub(tmp, "requests"), t4["id"] + ".json"),
                   os.path.join(_sub(tmp, "claimed"), t4["id"] + ".json"))
        _write_atomic(os.path.join(_sub(tmp, "results"), t4["id"] + ".json"),
                      {"id": t4["id"], "state": "failed", "ok": False, "error": "boom"})
        seen = []
        st = wait(t4["id"], timeout=5, poll=0.05, kb_dir=tmp,
                  on_state=lambda s: seen.append(s.get("state")))
        check("wait 直接看到 failed + error",
              st.get("state") == "failed" and st.get("error") == "boom", st)
        check("on_state 回调过", seen == ["failed"], seen)
        t5 = send("js", "return 4", kb_dir=tmp)
        st = wait(t5["id"], timeout=0.1, poll=0.02, kb_dir=tmp)
        check("超时不抛，如实回报 pending", st.get("state") == "pending", st)

        print("[4] 顺带诊断与清理")
        check("pending() 列出待领取的（只有 t5）",
              [x["id"] for x in pending(tmp)] == [t5["id"]], pending(tmp))
        old = os.path.join(_sub(tmp, "results"), "old.json")
        _write_atomic(old, {"id": "old"})
        os.utime(old, (time.time() - 30 * 24 * 3600,) * 2)
        fresh_result = os.path.join(_sub(tmp, "results"), t["id"] + ".json")
        n = cleanup(tmp, keep_seconds=7 * 24 * 3600)
        check("cleanup 只删过期结果", n == 1 and not os.path.exists(old), n)
        check("cleanup 不碰新鲜结果 / 正在跑的任务",
              os.path.exists(fresh_result)
              and os.path.exists(os.path.join(_sub(tmp, "claimed"), t3["id"] + ".json")),
              "fresh_result=%s t3=%s" % (os.path.exists(fresh_result),
                                         os.path.exists(os.path.join(
                                             _sub(tmp, "claimed"), t3["id"] + ".json"))))

    print("\n" + "=" * 56)
    print("taskbox 自检：通过 %d  失败 %d" % (ok, bad))
    print("=" * 56)
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
