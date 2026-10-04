"""经验层写入测试：**权重算术只有一份**，且与经验永远一致。

    python tests/test_experience_edit.py

盯四件事：
  ① **不许再出现第二份实现**：回滚权重的 SQL 原来在 `kb_admin.py` 里抄了两遍，
     "加分"那半在 `server.py` 里是第三种写法。三处漂移过一次，症状是
     "经验删了、权重还挂着"——**没有任何报错**，只是排序继续被幽灵分数影响。
     所以这里直接静态检查那几个文件里还有没有自己写 SQL。
  ② 增 / 改 / 删之后，"权重 = 经验的函数"这条恒等式成立（含换 outcome、
     换关联文献、只改文字三种情形）。
  ③ `pinned / manual / note`（人工分）不受经验增删影响 —— 它俩是两套东西。
  ④ MCP 工具的**错误文案与返回形状**没走样（那是对外契约，改了会让模型
     的用法习惯失效）。

全部在**临时库**上跑，不碰真实知识库。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
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


print("=" * 62)
print("经验层写入（offline/experience.py）")
print("=" * 62)

try:
    import experience as EXP
    import schemas as S
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  导不进 experience：{type(exc).__name__}: {exc}")
    sys.exit(0)

# ---------------------------------------------------------------- [1] 唯一实现
print("\n[1] 静态检查：加分与回滚各只允许有一份实现")
admin = open(os.path.join(ROOT, "tools", "kb_admin.py"), encoding="utf-8").read()
server = open(os.path.join(ROOT, "online", "server.py"), encoding="utf-8").read()
exp_src = open(os.path.join(ROOT, "offline", "experience.py"), encoding="utf-8").read()
check("kb_admin.py 里不再自己回滚经验权重",
      "MAX(0, effective" not in admin, "还有回滚 SQL")
check("kb_admin.py 用的是 experience 层", "EXP.delete_experience" in admin)
check("server.py 里不再自己 INSERT experience",
      "INSERT INTO experience" not in server)
check("server.py 里不再自己 INSERT item_weight",
      "INSERT INTO item_weight" not in server)
check("加分只此一处（effective = effective +）",
      exp_src.count("effective = effective +") == 1,
      str(exp_src.count("effective = effective +")))
check("回滚只此一处（MAX(0, effective -）",
      exp_src.count("MAX(0, effective -") == 1,
      str(exp_src.count("MAX(0, effective -")))
# 全仓扫一遍：这两句 SQL 片段不许出现在别的**生产代码**里
# （tests/ 不算：测试为了造数据自己写 SQL 是正常的，那不是"第二份实现"）
dupes = []
for sub in ("offline", "online", "tools"):
    for base, dirs, files in os.walk(os.path.join(ROOT, sub)):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(base, fn)
            if path == os.path.join(ROOT, "offline", "experience.py"):
                continue
            try:
                t = open(path, encoding="utf-8").read()
            except OSError:
                continue
            if "effective = effective +" in t or "MAX(0, effective -" in t:
                dupes.append(os.path.relpath(path, ROOT))
check("生产代码里没有第二个地方写这两句", not dupes, str(dupes))

# ---------------------------------------------------------------- 临时库
conn = S.init_db(os.path.join(tempfile.mkdtemp(prefix="kbexp_"), "t.db"))
w = EXP.ConnWriter(conn)


def wt(key):
    row = conn.execute("SELECT * FROM item_weight WHERE item_key=?", (key,)).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------- [2] 增改删
print("\n[2] 增 / 改 / 删 之后权重跟着经验走")
eid = EXP.add_experience(w, asked="自检：方法 A 有效吗", outcome="effective",
                         item_keys="AAAA1111", tags="自检,方法", source="user")
a = wt("AAAA1111")
check("新增 effective → attempts=1 effective=1",
      a.get("attempts") == 1 and a.get("effective") == 1, str(a))
check("没关联的文献不受影响", wt("BBBB2222") == {}, str(wt("BBBB2222")))

info = EXP.update_experience(w, eid, outcome="ineffective",
                             item_keys="AAAA1111,BBBB2222", reason="自检改写")
a, b = wt("AAAA1111"), wt("BBBB2222")
check("改成 ineffective：旧格子减掉、新格子加上",
      a.get("effective") == 0 and a.get("ineffective") == 1, str(a))
check("attempts 没有被改错（还是 1 次尝试）", a.get("attempts") == 1, str(a))
check("新关联的文献被计入", b.get("attempts") == 1 and b.get("ineffective") == 1,
      str(b))
check("改动摘要列出了变了的字段",
      {"outcome", "item_keys", "reason"} <= set(info["changed"]),
      str(sorted(info["changed"])))
row = EXP.get_experience(w, eid)
hist = EXP.loads(row["history"], [])
check("旧值进了 history", len(hist) == 1 and hist[0]["fields"]["outcome"] == "effective",
      str(hist)[:120])
check("updated_at 有值", bool(row.get("updated_at")), str(row.get("updated_at")))

# 只改文字：权重必须一动不动
before = (wt("AAAA1111"), wt("BBBB2222"))
EXP.update_experience(w, eid, asked="自检：换了个问题问法")
after = (wt("AAAA1111"), wt("BBBB2222"))
check("只改文字不动权重", before == after, f"{before} → {after}")

EXP.delete_experience(w, eid)
check("删除后权威重全部回零",
      wt("AAAA1111").get("attempts") == 0 and wt("BBBB2222").get("attempts") == 0,
      f"{wt('AAAA1111')} {wt('BBBB2222')}")
check("经验行真没了", EXP.get_experience(w, eid) is None)

# ---------------------------------------------------------------- [3] 校验
print("\n[3] 校验：文案是对外契约，别改字")
for bad, want in ((("x", "bogus"), "outcome 必须是"),
                  (("", "effective"), "asked 不能为空")):
    try:
        EXP.add_experience(w, asked=bad[0], outcome=bad[1])
        check(f"非法入参被拒（{bad}）", False, "没抛异常")
    except ValueError as exc:
        check(f"非法入参被拒（{bad}）", want in str(exc), str(exc))
try:
    EXP.add_experience(w, asked="x", outcome="effective", source="乱写")
    check("非法 source 被拒", False, "没抛异常")
except ValueError as exc:
    check("非法 source 被拒", "source 必须是" in str(exc), str(exc))

# ---------------------------------------------------------------- [4] 人工分
print("\n[4] pinned / manual / note 是另一套，不受经验增删影响")
EXP.set_weight(w, "CCCC3333", pinned=True, manual=1.5, note="自检重点")
EXP.add_experience(w, asked="自检：再记一条", outcome="partial",
                   item_keys="CCCC3333", tags="自检")
c = wt("CCCC3333")
check("经验计数加上了", c.get("attempts") == 1 and c.get("partial") == 1, str(c))
check("人工分还在", c.get("pinned") == 1 and c.get("manual") == 1.5
      and c.get("note") == "自检重点", str(c))
eid2 = None    # 下面按 asked 查 id，避免依赖具体自增值
row2 = conn.execute("SELECT id FROM experience WHERE asked='自检：再记一条'").fetchone()
EXP.delete_experience(w, row2["id"])
c = wt("CCCC3333")
check("删经验后人工分仍在", c.get("pinned") == 1 and c.get("manual") == 1.5, str(c))
check("删经验后计数回零", c.get("attempts") == 0 and c.get("partial") == 0, str(c))

# ---------------------------------------------------------------- [5] unknown
print("\n[5] outcome=unknown：只记「试过」，不判好坏")
EXP.add_experience(w, asked="自检：unknown", outcome="unknown",
                   item_keys="DDDD4444", tags="自检")
d = wt("DDDD4444")
check("attempts+1 但三格都是 0",
      d.get("attempts") == 1 and d.get("effective") == 0
      and d.get("ineffective") == 0 and d.get("partial") == 0, str(d))

# ---------------------------------------------------------------- [6] MCP 契约
print("\n[6] MCP 工具的对外形状（只走不碰库的早退分支）")
try:
    import server as SRV
    out = json.loads(SRV.kb_experience_add(asked="x", outcome="乱写"))
    check("非法 outcome 返回 error JSON", "error" in out and "outcome 必须是" in out["error"],
          str(out)[:80])
    out = json.loads(SRV.kb_experience_add(asked="", outcome="effective"))
    check("空 asked 返回 error JSON", "error" in out and "asked 不能为空" in out["error"],
          str(out)[:80])
    check("re_split 与 experience.split_list 结果一致（不许两份实现）",
          SRV.re_split("a, b；c  a") == EXP.split_list("a, b；c  a"),
          str(SRV.re_split("a, b；c  a")))
except ImportError as exc:  # noqa: BLE001
    print(f"  SKIP  导不进 server.py（{exc}）")
except Exception as exc:  # noqa: BLE001
    check(f"server.py 契约检查没抛异常：{type(exc).__name__}: {exc}", False)

# ---------------------------------------------------------------- [7] CLI
print("\n[7] CLI：add / edit 的参数面可用")
for sub in ("add", "edit"):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "kb_admin.py"),
                        sub, "--help"], capture_output=True, text=True)
    check(f"kb_admin.py {sub} --help 正常", r.returncode == 0, r.stderr[-120:])

# ---------------------------------------------------------------- [8] 原子性
print("\n[8] 改经验是原子的：中间步骤炸了，权重与行都不许变")


class BoomWriter(EXP.ConnWriter):
    """在第 fail_at 条语句上抛异常，用来验证事务真的回滚了。"""

    def __init__(self, conn, fail_at):
        super().__init__(conn)
        self.fail_at = fail_at

    def write_many(self, statements: list) -> int:
        n = 0
        try:
            for sql, params in statements:
                n += 1
                if n == self.fail_at:
                    raise RuntimeError("boom")
                self.conn.execute(sql, params)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return 0


eid3 = EXP.add_experience(w, asked="自检：原子性", outcome="effective",
                          item_keys="EEEE5555", tags="自检")
snap_w, snap_row = wt("EEEE5555"), dict(EXP.get_experience(w, eid3))
try:
    EXP.update_experience(BoomWriter(conn, 2), eid3, outcome="ineffective",
                          reason="这条不该生效")
    check("第 2 步炸掉时会抛异常", False, "没有抛")
except RuntimeError as exc:
    check("第 2 步炸掉时会抛异常", "boom" in str(exc), str(exc))
check("炸掉后权重没变（回滚生效）", wt("EEEE5555") == snap_w,
      f"{snap_w} → {wt('EEEE5555')}")
check("炸掉后经验行没变", dict(EXP.get_experience(w, eid3)) == snap_row,
      "行被改了")

# ---------------------------------------------------------------- [9] 迁移
print("\n[9] 打开旧库时自动补列（不然写入会撞 no such column）")
old_path = os.path.join(tempfile.mkdtemp(prefix="kbexp_old_"), "t.db")
S.init_db(old_path).close()
raw = __import__("sqlite3").connect(old_path)
for col in ("history", "updated_at"):
    raw.execute(f"ALTER TABLE experience DROP COLUMN {col}")
raw.commit()
raw.close()
got = {r[1] for r in S.connect(old_path).execute("PRAGMA table_info(experience)")}
check("connect() 就地把列补回来了", {"history", "updated_at"} <= got,
      str(sorted(got)))

conn.close()

print()
print("=" * 62)
print(f"通过 {PASS}，失败 {FAIL}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
