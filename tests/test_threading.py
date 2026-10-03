# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""验收：索引库在多线程下可用（MCP 服务器就是这样调用的）。

为什么必须有这个测试：MCP 的每个 tools/call 可能在不同线程执行，而连接是启动时
在主线程建的。曾经因此报：
    ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread.
表现是"第一次调用成功、之后偶发失败"，而且只在服务器重启后才暴露 ——
手工单线程测试永远发现不了。所以这里**必须真的用线程池并发打**。
"""

import concurrent.futures as futures
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import kbquery  # noqa: E402
import schemas as S  # noqa: E402
from searcher import Searcher  # noqa: E402

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


print("[1] 主线程建连接，多线程并发用（复现原故障场景）")
s = Searcher()          # 连接在这里创建
QUERIES = kbquery.sample_queries(6) + ["the", "model", "data", "method"]

errors = []


def hit(query):
    """在线程里跑检索，异常收进 errors（线程池不抛的话会静默丢）。"""
    try:
        r = s.search(query, limit=3)
        return len(r["hits"])
    except Exception as exc:  # noqa: BLE001
        import traceback
        errors.append(f"{type(exc).__name__}: {exc}")
        errors.append(traceback.format_exc())   # 把堆栈也带上，便于定位
        return -1


with futures.ThreadPoolExecutor(max_workers=8) as pool:
    counts = list(pool.map(hit, QUERIES))

check("并发检索无异常", not errors, f"{errors[:2]}")
check("并发检索都返回了结果", all(c >= 0 for c in counts), f"{counts}")
if errors:
    print("\n  --- 首个异常堆栈 ---")
    stack = next((e for e in errors if "Traceback" in e), "")
    for line in stack.splitlines()[-16:]:
        print(f"  {line}")


print("\n[2] 并发调用混合操作（检索 + 经验 + 权重 + 统计）")
errors2 = []
tracebacks = []


def mixed(i):
    try:
        if i % 4 == 0:
            s.search(kbquery.sample_query() or "model", limit=2)
        elif i % 4 == 1:
            s.experience_for_item("7DFGIIQG")
        elif i % 4 == 2:
            s.related_experience("非线性最小二乘")
        else:
            s.stats()
        return True
    except Exception as exc:  # noqa: BLE001
        import traceback
        errors2.append(f"{type(exc).__name__}: {exc}")
        tracebacks.append(traceback.format_exc())
        return False


with futures.ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(mixed, range(40)))
check("40 次混合并发调用无异常", not errors2, f"{errors2[:2]}")
if tracebacks:
    print("\n  --- 首个异常的完整堆栈（定位用）---")
    for line in tracebacks[0].splitlines()[-14:]:
        print(f"  {line}")

print("\n[3] 并发写经验（MCP 里 kb_experience_add 可能被并发调用）")
errors3 = []


def add_exp(i):
    try:
        # 必须走 s.write（整段串行化）；直接 conn.execute + commit 会绕过锁，
        # 并发时报 InterfaceError / OperationalError 并静默丢写。
        s.write(
            "INSERT INTO experience(created_at, asked, outcome, item_keys, source, "
            "tags) VALUES(datetime('now'), ?, 'unknown', '[]', 'dsh', '[]')",
            (f"并发测试 {i}",),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        errors3.append(f"{type(exc).__name__}: {exc}")
        return False


with futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(add_exp, range(20)))
check("20 次并发写无异常", not errors3, f"{errors3[:2]}")
n = s.read_one(
    "SELECT COUNT(*) AS n FROM experience WHERE asked LIKE '并发测试%'"
)["n"]
check("20 条都写进去了", n == 20, f"实际 {n} 条")
s.write("DELETE FROM experience WHERE asked LIKE '并发测试%'")

print("\n[4] 连接仍是可用状态")
try:
    check("清理后仍能正常检索", len(s.search(kbquery.sample_query() or "model", limit=2)["hits"]) >= 0)
    check("连接类型是 LockedConnection",
          type(s.conn).__name__ == "LockedConnection", type(s.conn).__name__)
except Exception as exc:  # noqa: BLE001
    check("清理后仍能正常检索", False, f"{type(exc).__name__}: {exc}")

s.close()
print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
