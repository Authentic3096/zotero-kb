# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""验收：经验层的「字段契约」——各查询来源都要返回组装器所需的列。

为什么必须有这个测试
--------------------
`online/server.py` 的 `kb_experience_query` 组装器要读 11 个列
（见 `server.EXPERIENCE_ROW_FIELDS`），但**行来自三个不同来源**：

    item_key  -> searcher.experience_for_item()
    query     -> searcher.related_experience()
    其它      -> 裸 "SELECT * FROM experience"

前两个是**手写列名清单**，一旦漏列，字段就静默变成 None / []：
写入端完全正常（数据库里列有值），所以从 `kb_experience_add` 那头查不出问题，
只有走检索路径才暴露。2026-10-02 就真的漏过两次：

    evidence   漏列  -> 检索结果里"依据"恒为 null
    item_keys  漏列  -> 按 item_key 查时 items 恒为 []（组装器有默认值，连报错都没有）

所以这里**必须针对三个来源分别断言字段齐全**，而不是只测一条路径。

用临时库跑，不动真实索引库；结束时删库。
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import schemas as S          # noqa: E402
import converter as C        # noqa: E402
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


# ---------------------------------------------------------------- 临时库
tmpdir = tempfile.mkdtemp(prefix="kb_expfields_")
db_path = os.path.join(tmpdir, "index.db")

ITEM = "TESTKEY1"          # 临时条目，只存在于这个临时库里
conn = S.init_db(db_path)  # 建表（含 experience / item_weight / items）
conn.execute(
    "INSERT INTO items(key, item_type, title, year, first_author, tags,"
    " collections, date_added, built_at)"
    " VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    (ITEM, "journalArticle", "临时测试条目", 2026, "测试", "[]", "[]"),
)
# 一条写全字段的经验（模拟 kb_experience_add 写入的完整行）
conn.execute(
    "INSERT INTO experience(created_at, asked, context, item_keys, method,"
    " outcome, reason, evidence, tags, source, session)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
    ("2026-10-02T20:00:00", "字段契约自检问题", "临时库", json.dumps([ITEM]),
     "某种方法", "effective", "因为所以", "关键数字 1.23e-4",
     json.dumps(["自检"]), "dsh", "test"),
)
conn.commit()

print("[1] 契约常量本身")
import server as SV  # noqa: E402

FIELDS = set(SV.EXPERIENCE_ROW_FIELDS)
check("EXPERIENCE_ROW_FIELDS 非空", bool(FIELDS))
check("含曾漏掉的 evidence", "evidence" in FIELDS)
check("含曾漏掉的 item_keys", "item_keys" in FIELDS)
check("契约条目数 >= 11", len(FIELDS) >= 11, f"实际 {len(FIELDS)}")

print("\n[2] 三个行来源各自返回的键（对运行时数据断言，不看源码）")
s = Searcher(db_path)

sources = {
    "experience_for_item": s.experience_for_item(ITEM, limit=5),
    "related_experience": s.related_experience("字段契约自检", limit=5),
    "SELECT * (其它分支)": [dict(r) for r in s.read(
        "SELECT * FROM experience ORDER BY created_at DESC LIMIT 5")],
}

for label, rows in sources.items():
    check(f"{label}: 取到测试行", len(rows) == 1, f"实际 {len(rows)} 行")
    if not rows:
        continue
    keys = set(rows[0].keys())
    missing = sorted(FIELDS - keys)
    check(f"{label}: 字段齐全", not missing, f"缺 {missing}")
    # 逐字段非空核对：漏列时这里是 None / 空串
    check(f"{label}: evidence 有值", rows[0].get("evidence") == "关键数字 1.23e-4",
          repr(rows[0].get("evidence")))
    check(f"{label}: item_keys 有值", rows[0].get("item_keys") == json.dumps([ITEM]),
          repr(rows[0].get("item_keys")))

print("\n[3] 组装器能把这些行解析成 items（不是空数组）")
row = sources["experience_for_item"][0]
check("items 解析出文献 key", json.loads(row.get("item_keys") or "[]") == [ITEM])
check("tags 解析出标签", json.loads(row.get("tags") or "[]") == ["自检"])

print("\n[4] converter.render_experience 也不漏 evidence")
conn2 = S.connect(db_path)
out = C.render_experience(conn2, ITEM)
check("渲染结果含『依据』行", "依据：关键数字 1.23e-4" in out,
      out.splitlines()[-1] if out else "(空)")
check("渲染结果含『问题』行", "字段契约自检问题" in out)

print("\n[5] MCP 工具层端到端（真实走 kb_experience_query）")
# 让 server.kb() 指向临时库。两件事都要做：
#   ① 替换模块级的 Searcher 名字（kb() 里就是用它构造的）
#   ② 清掉 kb() 的缓存实例 —— 变量名是 `_searcher`（小写！）
#      曾写成 `_SEARCHER`，赋值只是新建了一个属性，缓存根本没清；
#      一旦 kb() 已被调用过，monkeypatch 就失效，这一段会连到真实库
#      却依然"通过"（假阳性）。下面的断言就是为了挡住这种情况。
#   （`server` 已在 [1] 段导入为 SV）
SV.Searcher = lambda *a, **k: Searcher(db_path)
SV._searcher = None
try:
    payload = json.loads(SV.kb_experience_query(item_key=ITEM))
    check("工具返回 count=1", payload.get("count") == 1, repr(payload)[:160])
    exp = (payload.get("experience") or [{}])[0]
    check("工具返回 evidence 非空", bool(exp.get("evidence")), repr(exp.get("evidence")))
    check("工具返回 items 非空", exp.get("items") == [ITEM], repr(exp.get("items")))
    # 关键：确认这一段真的连的是临时库，而不是真实索引库
    check("确实连的是临时库（evidence 是临时值）",
          exp.get("evidence") == "关键数字 1.23e-4", repr(exp.get("evidence")))
except Exception as exc:  # noqa: BLE001
    import traceback
    check("工具层端到端", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

# ---------------------------------------------------------------- 清理
try:
    s.close()
except Exception:  # noqa: BLE001
    pass
try:
    conn2.close()
except Exception:  # noqa: BLE001
    pass
conn.close()
shutil.rmtree(tmpdir, ignore_errors=True)
print(f"\n临时库已清理：{tmpdir}")

print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
