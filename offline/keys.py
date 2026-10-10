"""合并映射与统一解析入口：**resolve_key()**（本模块是这条规则的唯一实现）。

    python offline/keys.py sync     # 读 Zotero 的 dc:replaces，落进 kb 的 item_merge
    python offline/keys.py show     # 只读：打印映射表 + 若干解析示例
    python offline/keys.py sync --json

对应《图谱与跨文献联系-设计方案.md》附录 G.1 / G.2 / G.4 / H.3。

为什么需要它：Zotero 做「Merge Items」时会把重复项并进保留项，被合并的那条 key
从此不再出现在 Zotero 里 —— 可是**知识库里到处都有它的历史引用**：用户手记的
experience.item_keys、图谱的节点与证据、评估集的 ground truth（G.6 第 2 条点名说过：
不过这一层解析，被合并的题目会在判分时被误判成"检索漏了"）。所以：**不重写历史**
（experience 的 JSON 一个字节都不动），只在读取时把 key 解析过去。

    resolve_key(key) -> key | None
      ① 命中 item_merge      → 递归取根（A→B→C 得 C；**检测到环就抛 MergeCycle**）
      ② 命中 item_tombstone  → None（"已不在"：trashed 与 deleted 都算）
      ③ 否则                 → 原样返回

    ⚠ 优先级是 **合并映射 > 墓碑**（G.4 与任务清单都钉死了这条）：被合并掉的重复项
      通常还躺在 Zotero 回收站里，所以它**同时**有一条 kind='trashed' 的墓碑。
      先查墓碑的话，PALT487S ← N38Y49NF 这种引用会被解析成"已删除"，历史引用全断。

      ⚠ 这条优先级的边界要说清（别把它理解成"整条链都免疫墓碑"）：
        · 输入 key 有映射 → 走链取根，**只认映射**，不看墓碑（链上任何一环都不看）；
        · 输入 key 没有映射 → 才轮到它自己的墓碑。
        所以"并到一条后来被彻底删掉的文献"会解析成那个已删的 key（而不是 None）。
        这是有意的：解析层只回答"这条历史引用现在指谁"，"指到的人还在不在"是下一问
        （调用方拿结果去 items 里查就看得出来）。若在这里把它抹成 None，就再也分不清
        "从没进过库"与"被并到一条已删的文献"了。

只增不改：item_merge 是审计与撤销的依据（G.4 第 7 步），所以 sync 走 INSERT OR
IGNORE —— 重复 sync 既不新增行、也不刷新 seen_at（否则"这条什么时候被合并的"会每次
都被改成当下，历史就没了）。

⚠ 全局纪律 7（唯一写路径）：item_merge 这张表**只有本模块写**，别的模块只能调
  resolve_key() / merge_map() 读。要找"某条被并到哪去了"就调它，别自己拼 SQL。

本模块**不做**的事（各有归属，别顺手加进来）：
  · 不合并 item_weight（相加会抬高排序权重，要用户确认 —— G.4 第 4 步）；
  · 不 relink 图谱数据（kg_* 表还没建，见 T1-1）；
  · 不写 item_tombstone.merged_into：**合并事实只存 item_merge 一处**（G.2 建表时
    预留了那列，但它保持空）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S               # noqa: E402
import trash as TR                # noqa: E402 —— 墓碑读取走它（本模块不重复那句 SQL）
from zreader import ZoteroReader  # noqa: E402 —— 读 Zotero 的唯一姿势（快照副本）

SOURCE = "zotero-dc-replaces"
PREDICATE = "dc:replaces"

# 8 位 Zotero key，只用得上大写字母与数字（Zotero 自己的字母表里没有小写）
KEY_RE = re.compile(r"^[A-Z0-9]{8}$")

# 进程内缓存（分析见 _cache_for）。只活在**进程内存**里，不落盘 ——
# 落盘的话，用户在 Zotero 里刚合并完，这边还在读上一次的快照。
_CACHE = {}


# ---------------------------------------------------------------- 小工具


def _is_conn(obj) -> bool:
    return isinstance(obj, sqlite3.Connection)


def _open_conn(conn=None):
    """把 conn=None 补成一条连接。返回 (连接, 是不是本函数开的)。

    resolve_key(key) 不带 conn 时**每次都要开一次连接**（面板/MCP 就是那么调的）：
    所以那条路上有 _CACHE 顶着，重复调用不会反复读表；但连接本身是每次新开的，
    调用方不该在"每篇文献解析一次"的循环里依赖它省开销。
    """
    if _is_conn(conn):
        return conn, False
    path = conn if isinstance(conn, str) and conn else S.INDEX_DB
    return S.connect(path), True


def _cache_for(conn) -> dict:
    """同一份索引库的进程内缓存：{merges, tombstones, mtime}。

    拆成两个字段（而不是"解析结果"一个 dict）是有意的：resolve_key 的答案同时取决于
    "有没有映射"和"有没有墓碑"，任何一边变了答案就可能变；分别缓存两张小表、每次现算，
    就不用担心"缓存了一个后来才变成 None 的答案"。

    ⚠ 失效判据是**索引库文件的 mtime**：构建/归档/还原都会写库、mtime 必然变，所以
      缓存不会跨过一次改动。代价是"写库后同一秒内再读"理论上可能读到旧值 —— 这个窗口
      对本模块的用途（读取时解析历史引用）无害，真改了库的那个进程自己调 sync 时会顺手
      清掉（见 sync_merges）。
    ⚠ 只对"磁盘上的库"生效；:memory: 这类路径没有变化检测，一律不缓存。
    """
    path = ""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                path = str(row[2] or "")
    except sqlite3.Error:
        path = ""
    if not path or path.startswith(":"):
        return _load_cache(conn)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return _load_cache(conn)
    cached = _CACHE.get(path)
    if cached is not None and cached.get("mtime") == stamp:
        return cached
    fresh = _load_cache(conn)
    fresh["mtime"] = stamp
    _CACHE[path] = fresh
    return fresh


def _load_cache(conn) -> dict:
    return {
        "merges": _load_table(conn, "SELECT old_key, new_key FROM item_merge"),
        "tombstones": _load_table(conn, "SELECT key FROM item_tombstone"),
    }


def _load_table(conn, sql: str) -> dict:
    """读一张小表成 {key: 值}；只有一列时值是 ""。

    ⚠ 别写成固定的 r[0] / r[1]（本机实测踩到）：墓碑那张表只 select 一列，r[1] 会抛
      IndexError —— 而 IndexError **不是 sqlite3.Error**，兜不住、直接崩在 show 上。
    表还没建出来时返回 {} 而不是抛：老库第一次用新版会经过这里（S.connect() 每次都补
    一次迁移，但补迁失败时不该连"解析个 key"都做不了）。
    """
    try:
        return {str(r[0]): (str(r[1]) if len(r) > 1 else "")
                for r in conn.execute(sql) if r and r[0]}
    except sqlite3.Error:
        return {}


def _cycle_in_order(chain):
    """从解析路径里截出环上的人，按"走到的顺序"左到右 —— A→B→C→A 得 [A, B, C]。"""
    last = chain[-1]
    return chain[chain.index(last):-1]


def cache_clear(path: str = "") -> None:
    """清掉进程内缓存（:memory: 库只有靠它；也给测试与"刚写完库"的调用方用）。"""
    if path:
        _CACHE.pop(path, None)
    else:
        _CACHE.clear()


# ---------------------------------------------------------------- 读映射


def merge_map(conn=None) -> dict:
    """当前知识库的 old_key → new_key 映射（含运行时的缓存）。"""
    c, own = _open_conn(conn)
    try:
        return dict(_cache_for(c)["merges"])
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- 解析


class MergeCycle(RuntimeError):
    """合并映射成环（A→B→A）。Zotero 侧正常不会出现，出现了必须**报出来**：
    静默取根会把一条错误的映射当成权威，之后所有引用都跟着错，且没人知道。"""

    def __init__(self, cycle):
        self.cycle = [str(x) for x in cycle]
        self.key = self.cycle[0] if self.cycle else ""
        super().__init__("item_merge 出现环：" + " → ".join(self.cycle + [self.key]))


def _resolve(key: str, merges: dict, tombstones: dict):
    """核心解析（纯函数，不碰数据库 —— 单测可以直接喂字典）。"""
    chain = [key]
    seen = {key}
    cur = key
    while True:
        nxt = merges.get(cur)
        if nxt is None:
            break
        if nxt in seen:                       # 环：报错，不静默
            raise MergeCycle(_cycle_in_order(chain + [nxt]))
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    if cur != key:
        # ⚠ 这里**不再查墓碑**：链上的中间节点（甚至根）本来就可能躺在回收站里 ——
        #   那正是 Zotero 合并的常态（被并掉的项进回收站）。按墓碑判会让"并到一条
        #   已被归档的文献"解析成"已删除"，历史引用同样全断。根若真的被删了，该由
        #   调用方按"解析结果不在库里"处理，而不是在这里把引用抹成 None。
        return cur
    # 输入 key 自己没有映射：合并规则没命中，这时才轮到墓碑（优先级就是这么定的）
    return None if key in tombstones else key


def resolve_key(key: str, conn=None):
    """把任意历史 key 解析成**当前有效**的 key；"已不在"返回 None。

    · 有合并映射 → 递归取根（A→B→C 得 C；成环抛 MergeCycle，不静默）；
    · 没有映射、但有墓碑（trashed / deleted）→ None；
    · 都没有 → 原样返回。

    conn 可以是 S.connect() 出来的连接、索引库路径、或什么都不给（用默认库）。
    """
    k = str(key or "").strip()
    if not k:
        return None
    c, own = _open_conn(conn)
    try:
        cache = _cache_for(c)
        return _resolve(k, cache["merges"], cache["tombstones"])
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- 落库（只增不改）


def sync_merges(conn=None, reader=None) -> dict:
    """把 Zotero 的 dc:replaces 落进 item_merge。返回一份统计。

    · **只增不改**：已存在的 old_key 原样留着（seen_at 保持首次的值）—— INSERT OR
      IGNORE 一句就够，不必先查再插；
    · applied_at 一律留 NULL：那要等 relink 真做完才写（G.4 第 2 步，后续任务）；
    · source 固定 zotero-dc-replaces：将来若还有别的来源（比如用户手工补一条），靠它区分。

    返回 {seen, inserted, existing, skipped, live, total}：
    · seen 是**去重后** Zotero 侧的关系条数；
    · skipped 是 URI 形态没认出来的行数（见 ZoteroReader.merged_keys）；
    · live = Zotero 那边现有映射数 - 本地总数，**负数说明本地记着一条 Zotero 已经不认
      的映射**（只增不改的正常情况下不会发生，但它是唯一能看出这种偏差的口径）。
    """
    c, own = _open_conn(conn)
    own_reader = False
    try:
        if reader is None:
            reader = ZoteroReader()   # 快照读库（Zotero 在跑时直连会 database is locked）
            own_reader = True
        pairs, skipped = reader.merged_keys(PREDICATE)
        # 去重：一个 old_key 只允许一条映射（表主键就是它）。Zotero 正常不会给出同一个
        # old 的两条关系；真出现了就按最后读到的那条算，并把差额报出来。
        uniq = dict(pairs)
        have = {str(r[0]) for r in c.execute("SELECT old_key FROM item_merge")}
        now = TR.now_iso()
        added = 0
        for old_key, new_key in uniq.items():
            if old_key in have:
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO item_merge(old_key, new_key, seen_at, applied_at, "
                "source) VALUES(?,?,?,NULL,?)",
                (old_key, new_key, now, SOURCE))
            added += max(cur.rowcount, 0)
        c.commit()
        if added:
            cache_clear()             # 本进程刚写了映射表，别让下一句读还吃旧缓存
        total = int(c.execute("SELECT COUNT(*) FROM item_merge").fetchone()[0])
        return {"seen": len(uniq), "inserted": added,
                "existing": max(len(uniq) - added, 0),
                "skipped": int(skipped) + (len(pairs) - len(uniq)),
                "live": len(uniq) - total,
                "total": total}
    except sqlite3.Error as exc:
        return {"seen": 0, "inserted": 0, "existing": 0, "skipped": 0, "live": 0,
                "total": 0, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if own_reader:
            reader.close()
        if own:
            c.close()


# ---------------------------------------------------------------- 台账（审计/面板用）


def merge_rows(conn=None) -> list:
    """整张映射表，按 seen_at、old_key 排（show 与面板共用一份读法）。"""
    c, own = _open_conn(conn)
    try:
        try:
            rows = c.execute(
                "SELECT old_key, new_key, seen_at, applied_at, source FROM item_merge "
                "ORDER BY seen_at, old_key").fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


def pending_count(conn=None) -> int:
    """applied_at 还是空的映射条数（= 知识库还没迁的活，后续任务处理）。"""
    c, own = _open_conn(conn)
    try:
        try:
            return int(c.execute("SELECT COUNT(*) FROM item_merge "
                                 "WHERE applied_at IS NULL").fetchone()[0])
        except sqlite3.Error:
            return 0
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- 命令行


def _cmd_sync(as_json: bool = False) -> int:
    rep = sync_merges()
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 1 if "error" in rep else 0
    if "error" in rep:
        print("  同步失败：%s" % rep["error"])
        return 1
    print("  从 Zotero 读到 %d 条 %s" % (rep["seen"], PREDICATE))
    print("  item_merge：新增 %d 条、已存在 %d 条（只增不改，seen_at 保持首次值）"
          % (rep["inserted"], rep["existing"]))
    if rep["skipped"]:
        print("  [!!] 有 %d 条关系的 URI 形态没认出来，已跳过" % rep["skipped"])
    if rep["live"] < 0:
        print("  [!!] 本地比 Zotero 多 %d 条 —— 可能是历史遗留，值得看一眼"
              % (-rep["live"]))
    print("  待处理（applied_at 为空）：%d 条 —— relink 与权重合并属后续任务，本次不动"
          % pending_count())
    return 0


def _cmd_show(conn=None) -> int:
    """只读：把映射表与若干解析示例打出来（不发任何写请求）。"""
    c, own = _open_conn(conn)
    try:
        rows = merge_rows(c)
        print("item_merge（%d 行）" % len(rows))
        if rows:
            print("  %-8s  %-8s  %-25s  %s"
                  % ("old_key", "new_key", "seen_at", "applied_at"))
            for r in rows:
                print("  %-8s  %-8s  %-25s  %s"
                      % (r["old_key"], r["new_key"], r["seen_at"],
                         r["applied_at"] or "（待处理）"))
        else:
            print("  （空 —— 先跑 python offline/keys.py sync）")
        cache = _cache_for(c)
        tomb = cache["tombstones"]
        print("item_tombstone（%d 行）：%s"
              % (len(tomb), "、".join("%s[%s]" % (k, TR.kind_of(c, k))
                                      for k in sorted(tomb)) or "（空）"))

        # 解析示例：**全从数据库里现取**（写死样本的话，换了库就会打印过期结论）
        picks = []
        for r in rows[:2]:
            picks.append((r["old_key"], "被合并 → 根 %s" % r["new_key"]))
            picks.append((r["new_key"], "保留项自己（还在库里就是原样返回）"))
        picks.append(("ZZZZZZZZ", "库里没有的 8 位 key → 原样返回"))
        for k in sorted(tomb)[:2]:
            picks.append((k, "只有墓碑、没有映射 → None（已不在）"))
        print("resolve_key 示例")
        seen = set()
        for k, why in picks:
            if k in seen:
                continue
            seen.add(k)
            try:
                got = resolve_key(k, c)
                shown = "None" if got is None else (got if got != k else "%s（原样）" % got)
            except MergeCycle as exc:
                shown = "[!!] %s" % exc
            print("  resolve_key('%s') → %s      # %s" % (k, shown, why))
        return 0
    finally:
        if own:
            c.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="合并映射（item_merge）与 resolve_key")
    parser.add_argument("action", choices=["sync", "show"], nargs="?",
                        default="show", help="sync 写映射；show 只读打印")
    parser.add_argument("--json", action="store_true", help="sync 用：输出 JSON")
    args = parser.parse_args(argv)
    if args.action == "sync":
        return _cmd_sync(args.json)
    return _cmd_show()


if __name__ == "__main__":
    sys.exit(main())
