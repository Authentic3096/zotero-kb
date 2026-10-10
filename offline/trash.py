"""两段式删除：**归档 / 还原 / 删除**（本模块是这三个动作的唯一实现）。

对应《图谱与跨文献联系-设计方案.md》附录 G.0 / G.3 / H.1 / H.2 / H.4。

    Zotero 侧                              知识库动作
    ① 进回收站（key 在 deletedItems 里）    archive()         产物移进 kb/trash/<key>/、
                                                            删库里的行、写墓碑 kind='trashed'
    ② 回收站里彻底删（两处都没了）          delete_archived() 清掉 kb/trash/<key>/、
                                                            墓碑改 kind='deleted'（磁盘这时才释放）
    ③ 从回收站还原（key 回到 items）        restore()         产物搬回原位、按 rows.json 复原行、
                                                            清墓碑
    ④ 条目还在、只是没有 PDF 了             discard_live()    直接清掉，**不归档、不留墓碑**

为什么 ④ 不能走 archive()：墓碑一旦写成 kind='trashed'，下一轮对账看到"这条 live
且有待还原的墓碑"就会把它搬回来 —— 用户删掉 PDF 的条目会每轮复活一次。④ 不是"被删的
文献"，它还在 Zotero 里，只是按既有约定（用户明确要求）不进知识库。

为什么归档要带 `rows.json`（H.2）：向量是唯一"重算贵"的东西（ONNX/CPU）。把
chunks/figures/embeddings 一并导出，还原就是一次 INSERT —— 不用重跑模型、不用重新解析
（MinerU 产物也在 trash 里，指纹命中缓存）。一条文献的 rows.json 通常几十 KB～几百 KB。

⚠ 但「还原就是一次 INSERT」有个前提：**那些 id 那时还得空着**（T0-11）。
  归档之后又添了几篇文献的话，`chunks.chunk_id` 完全可能已经被别人复用 —— 直插会
  静默覆盖别人的切片与全文索引。所以直插前先探冲突，探到就整条降级为重建：
  见 `_id_conflicts` / `restore()` 的 degraded 分支。

⚠ 全局纪律 7（唯一写路径）：**别的模块不许自己搬这些文件、也不许自己删这些行**，
  只能调本模块。convert.py 里原来的 `_purge_key` 就是按这条拆掉的。

⚠ 本模块**不碰** `experience` / `item_weight`：那是用户自己记的数据（用过什么方法、
  效果如何、标没标重点），删文献也不删 —— 它们指向已删文献时靠 `keys.resolve_key()`
  在界面上提示（那是 T0-2/T0-5 的事）。
"""

from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402

# 归档目录里的形状（H.2）：rows.json + tombstone.json + 与 kb 同名的子目录
ROWS_JSON = "rows.json"
# 探到 id 冲突时 rows.json 改名叫这个（见 _mark_snapshot_stale）：
# 名字一换，下次 restore() 就再也读不到它，不会拿同一份快照再覆盖一次别人。
ROWS_STALE_JSON = "rows.stale.json"
TOMBSTONE_JSON = "tombstone.json"
MINERU_SUB = "mineru"        # kb/mineru/<key>/ 在 trash 里就叫 mineru/
SNAPSHOT_VERSION = 1
# 运行日志：与 localserver.log / zotero-sync.log 同一个日志目录（<知识库>\logs\）
LOG_NAME = "trash.log"


def now_iso() -> str:
    """本地时区的 ISO 时间（与 items.built_at 同一口径，人读得懂）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 落点
#
# ⚠ 这些路径**每次调用都重新解析**，不缓存成模块常量：测试与面板会改
#   S.INDEX_DB / S.VIEWS_DIR（既有测试就是这么做的），缓存下来就会指到真实知识库上
#   —— 那意味着"跑一次测试把用户的库归档了"。schemas.kb_dir() 是唯一事实定义。


def _kb_root() -> str:
    return os.path.dirname(S.INDEX_DB)


def trash_root() -> str:
    return os.path.join(_kb_root(), "trash")


def trash_dir(key: str) -> str:
    return os.path.join(trash_root(), key)


def _kb_dirs() -> dict[str, str]:
    """归档要一起搬走的三个"每篇一份"目录：trash 里的子目录名 → kb 里的目录。"""
    return {"views": S.VIEWS_DIR, "papers": S.PAPERS_DIR, "fulltext": S.FULLTEXT_DIR}


def _mineru_dir(key: str) -> str:
    """该条的 MinerU 产物目录 `<kb>/mineru/<key>/`。

    落点归 mineru.py 管（它有 artifact_dir()），这里不自己拼 —— 否则"产物在哪"
    会有两份定义，早晚漂移。导入失败（可选组件被卸载）时退回同样的拼法。
    """
    try:
        import mineru as M       # noqa: PLC0415
        return M.artifact_dir(key, _kb_root())
    except Exception:            # noqa: BLE001
        return os.path.join(_kb_root(), MINERU_SUB, key)


# ---------------------------------------------------------------- 小工具


def _log(msg: str) -> None:
    """把一次降级/异常写进 `<知识库>/logs/trash.log`（**只写文件，不 print**）。

    为什么只写文件：这条链会被本地服务与面板拉起，它们按行读 stdout 当进度，
    低层模块往里插一行会把那份进度读花。转换期的可见性交给 convert.py 的
    `log()` —— 它在还原摘要里会把降级原因一并报出来（那是构建自己的日志通道）。

    ⚠ 路径**每次重新解析**，不缓存成常量：测试会改 S.INDEX_DB（见上面「落点」一节），
      缓存下来就会把日志写进用户真实知识库里。
    """
    try:
        path = os.path.join(_kb_root(), "logs", LOG_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass        # 日志写不进去不该让还原失败 —— 它是记录，不是功能


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:            # noqa: BLE001 —— 缺了/坏了都按"没有"处理
        return {}


def _write_json(path: str, data: dict) -> None:
    """先写临时文件再 os.replace：中途断电不会留下半截 rows.json（那份是还原的唯一依据）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _move_path(src: str, dst: str) -> bool:
    """把 src **移**到 dst（不是复制后留原件 —— 归档不该在知识库里留一份）。

    同卷走 os.replace（原子），跨卷/被占用退回 shutil.move。
    目标已存在（崩溃后重跑、重复归档）时先清掉目标：归档天然幂等。
    源与目标都是目录时逐项递归合并 —— 覆盖安装式的重跑不会丢东西。
    """
    if not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.isdir(src) and os.path.isdir(dst):
        for name in sorted(os.listdir(src)):
            _move_path(os.path.join(src, name), os.path.join(dst, name))
        try:
            os.rmdir(src)
        except OSError:
            pass
        return True
    if os.path.exists(dst):
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        else:
            try:
                os.remove(dst)
            except OSError:
                pass
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)
    return True


def _kb_key_files(key: str) -> list[tuple[str, str]]:
    """该条在知识库里现存的"每篇一份"文件：[(绝对路径, 子目录名)]。

    用 `<key>.*` 而不是 `<key>.*.md`：分级视图与卡片都会**成对**写一份
    `.md` 和一份带 MathML 的 `.html`（Zotero 右侧栏读后者）。只搬 `.md`
    会把 `.html` 留在原地，"原位置已清空"就不成立（旧 `kbviews.purge()` 只删
    `.md`，这个洞是本次顺手补上的）。
    key 是 8 位大写字母数字、不含 `.`，所以 `<key>.*` 不会误伤别的条目。
    """
    found: list[tuple[str, str]] = []
    for sub, directory in _kb_dirs().items():
        for path in sorted(glob.glob(os.path.join(directory, f"{key}.*"))):
            if os.path.isfile(path):
                found.append((path, sub))
    return found


def _trash_key_files(key: str) -> list[tuple[str, str]]:
    """归档目录里存着的那些文件：[(绝对路径, 子目录名)]。"""
    found: list[tuple[str, str]] = []
    base = trash_dir(key)
    for sub in _kb_dirs():
        for path in sorted(glob.glob(os.path.join(base, sub, "*"))):
            if os.path.isfile(path):
                found.append((path, sub))
    return found


# ---------------------------------------------------------------- 墓碑（item_tombstone）


def tombstone(conn, key: str) -> dict:
    """读一条墓碑（没有就 {}）。表还没补上时不抛 —— 老库第一次跑新版会经过这里。

    T0-13 起多了 title/first_author/year（"删前叫什么"）。老库那三列还没补上时
    退回只读原来那五列：**kind / deleted_at 是逻辑依赖**（阶段③ 靠 kind='trashed'
    判断"要不要还原"），名字只是显示 —— 不能因为缺三列把它们一起丢掉。
    """
    base = "key, deleted_at, kind, merged_into, note"
    for cols in (base + ", " + ", ".join(S.TOMBSTONE_NAME_COLS), base):
        try:
            row = conn.execute(
                f"SELECT {cols} FROM item_tombstone WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            continue
        return dict(row) if row is not None else {}
    return {}


def _item_name(conn, key: str, fallback: dict | None = None) -> dict:
    """抄下"删前叫什么"（T0-13）：title / first_author / year 三个字段。

    只有归档/彻底删除那**一下**手上有 items 行 —— 之后归档目录一清、库里一行不剩，
    名字就再也找不回来了，所以在这里抄一份进墓碑。

    ⚠ **绝不抛**：读不到（行已经不在、老库缺列、库坏了）就留空 —— 归档比留名重要，
      不能因为抄不到名字让整条归档失败。fallback 给归档快照里的 items 行兜底
      （"库里那行已经删了、快照里还有"的重跑路径）。
    ⚠ 按**列名**取、不按下标（T0-2 踩过：单列查询读 r[1] 抛 IndexError，而且它
      不是 sqlite3.Error，except 兜不住）；调用方塞裸 sqlite3 连接（元组行）也要能用。
    """
    out = {c: "" for c in S.TOMBSTONE_NAME_COLS}
    try:
        row = conn.execute("SELECT title, first_author, year FROM items WHERE key = ?",
                           (key,)).fetchone()
        if row is not None:
            try:
                vals = [row[c] for c in S.TOMBSTONE_NAME_COLS]
            except (TypeError, IndexError, KeyError):
                vals = list(tuple(row))
            for col, val in zip(S.TOMBSTONE_NAME_COLS, vals):
                out[col] = str(val or "").strip()
    except Exception:        # noqa: BLE001 —— 抄不到名字不是错误，是"留空"
        pass
    src = fallback if isinstance(fallback, dict) else {}
    for col in S.TOMBSTONE_NAME_COLS:
        if not out[col]:
            out[col] = str(src.get(col) or "").strip()
    return out


def kind_of(conn, key: str) -> str:
    return str(tombstone(conn, key).get("kind") or "")


def _set_tombstone(conn, key: str, kind: str, *, merged_into: str | None = None,
                   note: str = "", deleted_at: str = "",
                   name: dict | None = None) -> None:
    """写/更新墓碑。key 是主键，重复归档直接覆盖（幂等）。

    name 是"删前叫什么"（T0-13 的三列，由 _item_name() 抄来）。
    ⚠ 传进来的空值**不覆盖**已经记下的：阶段②（彻底删除）时 items 行早就没了，
      照写就会把阶段①归档时留的名抹掉 —— 而留住"删前叫什么"正是这条任务的目的。
    """
    old = tombstone(conn, key)
    fresh = {c: str((name or {}).get(c) or "").strip() for c in S.TOMBSTONE_NAME_COLS}
    keep = [fresh[c] or str(old.get(c) or "") for c in S.TOMBSTONE_NAME_COLS]
    params = (key, deleted_at or now_iso(), kind, merged_into, note, *keep)
    try:
        conn.execute(
            "INSERT INTO item_tombstone(key, deleted_at, kind, merged_into, note, "
            "title, first_author, year) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "deleted_at=excluded.deleted_at, kind=excluded.kind, "
            "merged_into=excluded.merged_into, note=excluded.note, "
            "title=excluded.title, first_author=excluded.first_author, "
            "year=excluded.year", params)
    except sqlite3.OperationalError:
        # 老库这三列还没补上（没走过 S.connect 那次迁移）：退回原来的五列写法 ——
        # 留名是锦上添花，**归档本身不能因此失败**。
        conn.execute(
            "INSERT INTO item_tombstone(key, deleted_at, kind, merged_into, note) "
            "VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "deleted_at=excluded.deleted_at, kind=excluded.kind, "
            "merged_into=excluded.merged_into, note=excluded.note", params[:5])
    conn.commit()


def _clear_tombstone(conn, key: str) -> None:
    try:
        conn.execute("DELETE FROM item_tombstone WHERE key = ?", (key,))
        conn.commit()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- 行快照 / 复原


def _rows_snapshot(conn, key: str, *, with_embeddings: bool = True) -> dict:
    """把该条在索引库里的行导成一份能**直插回去**的快照（H.2 的 rows.json）。"""
    item = conn.execute("SELECT * FROM items WHERE key = ?", (key,)).fetchone()
    snap: dict = {
        "version": SNAPSHOT_VERSION,
        "key": key,
        "archived_at": now_iso(),
        "items": dict(item) if item is not None else {},
        "chunks": [dict(r) for r in conn.execute(
            "SELECT chunk_id, item_key, page, seq, text, n_chars FROM chunks "
            "WHERE item_key = ? ORDER BY chunk_id", (key,))],
        "figures": [dict(r) for r in conn.execute(
            "SELECT fig_id, item_key, page, kind, label, text, n_rows, n_cols, built_at "
            "FROM figures WHERE item_key = ? ORDER BY fig_id", (key,))],
        "embeddings": [],
    }
    if with_embeddings:
        # 向量是唯一"重算贵"的东西，默认全存（H.2 的建议：先全存，简单）。
        # BLOB 进不了 JSON，用 base64 —— 代价是体积涨约 1/3，换"还原不用跑模型"值得。
        for r in conn.execute(
                "SELECT e.chunk_id, e.dim, e.vector, e.model FROM embeddings e "
                "JOIN chunks c ON c.chunk_id = e.chunk_id WHERE c.item_key = ? "
                "ORDER BY e.chunk_id", (key,)):
            snap["embeddings"].append({
                "chunk_id": r["chunk_id"], "dim": r["dim"], "model": r["model"],
                "vector_b64": base64.b64encode(r["vector"] or b"").decode("ascii"),
            })
    snap["counts"] = {"chunks": len(snap["chunks"]), "figures": len(snap["figures"]),
                      "embeddings": len(snap["embeddings"])}
    return snap


# ---------------------------------------------------------------- id 冲突探测
#
# 为什么必须探：这是**静默数据损坏**，不是理论风险。
#   `chunks.chunk_id` 是 INTEGER PRIMARY KEY，**没有 AUTOINCREMENT** —— SQLite 给新行
#   分配的是 max(rowid)+1。归档把一条的行删掉之后，如果它当初排在**表尾**，之后新增的
#   文献会**原样复用**那些 id（本机现库实测：chunks 4588 行，chunk_id 却排到 6 万多）。
#   这时按快照里的原 id 直插，会：
#     ① `INSERT OR REPLACE` 静默覆盖**别人的切片行**；
#     ② 紧跟的那句 `DELETE FROM chunks_fts WHERE rowid=?` 删掉**别人的全文索引行**
#        （那篇的关键词检索直接坏掉，而且不报错）；
#     ③ 随后的增量又把那些 id 收走 —— 别人那篇变成「有条目、没切片、没向量」，
#        而它的 `items.version` 没变，**增量永远不会去修它**，直到下一次全量重建。
#   用户问的正是这个场景：「还原可能发生在很久以后，中间还会加文献」。

_OWNER_CHUNK = "SELECT item_key FROM chunks WHERE chunk_id = ?"
_OWNER_FIGURE = "SELECT item_key FROM figures WHERE fig_id = ?"
# items 的列名是 key（不是 item_key），别名一下好让 _owner() 通用
_OWNER_ITEM = "SELECT key AS item_key FROM items WHERE item_id = ?"
# embeddings 自己不记 item_key，归属要从 chunks 反查
# （LEFT JOIN：没有对应切片时给 NULL，即「无主」）
_OWNER_EMB = ("SELECT c.item_key AS item_key FROM embeddings e "
              "LEFT JOIN chunks c ON c.chunk_id = e.chunk_id WHERE e.chunk_id = ?")
_HAS_FTS = "SELECT rowid FROM chunks_fts WHERE rowid = ?"


def _owner(conn, sql: str, arg) -> str:
    """某个 id 现在归谁（没有那一行、或查不到主人时返回空串）。

    ⚠ **按列名取值，不按下标**：单列查询写 `r[1]` 会抛 IndexError，而它不是
      sqlite3.Error，`except sqlite3.Error` 兜不住（T0-2 在旁边的模块踩过这一下）。
    """
    row = conn.execute(sql, (arg,)).fetchone()
    if row is None:
        return ""
    try:
        return str(row["item_key"] or "")
    except (IndexError, KeyError):     # 列名对不上 -> 按「查不到主人」处理
        return ""


def _exists(conn, sql: str, arg) -> bool:
    return conn.execute(sql, (arg,)).fetchone() is not None


def _id_conflicts(conn, key: str, snap: dict) -> list[str]:
    """探测快照里的 id 是否已被**别的条目**占着（**纯只读**，在任何写之前跑）。

    判定用 `item_key != key` —— **同一个 key 自己崩溃后重跑的残留不算冲突**
    （那种残留下面的「先按 key 删净」正好收拾干净）。

    五处都要探（中间两处挂在 chunk_id 上）：
      · chunks.chunk_id      —— 主判据，它自己带着 item_key
      · figures.fig_id       —— 同上
      · chunks_fts.rowid     —— 它不记归属，只能靠 chunks 反查
      · embeddings.chunk_id  —— 同上
      · items.item_id        —— Zotero 的数值 id，见下面的专段
    后两处的口径是「**证明得了是本条目的才不算冲突**」：chunks 里没有对应行时，
    那一行不是任何条目的派生行（无主残留），证不明归属 —— 按「不是我的」降级重建，
    而不是覆盖它。宁可整条慢一点。

    探测本身报错时**不吞异常**：异常往上抛到 restore()，它记 ok=False 并保留快照，
    一个字节都不写 —— 绝不在没探明的情况下直插。
    """
    out: list[str] = []
    for row in snap.get("chunks") or []:
        cid = row.get("chunk_id")
        if cid is None:
            continue
        owner = _owner(conn, _OWNER_CHUNK, cid)
        if owner and owner != key:
            out.append(f"chunk_id {cid}（现属于 {owner}）")
        elif not owner:
            if _exists(conn, _HAS_FTS, cid):
                out.append(f"chunks_fts.rowid {cid}（无主残留）")
            elif _exists(conn, _OWNER_EMB, cid):
                out.append(f"embeddings.chunk_id {cid}（无主残留）")
    for row in snap.get("figures") or []:
        fid = row.get("fig_id")
        if fid is None:
            continue
        owner = _owner(conn, _OWNER_FIGURE, fid)
        if owner and owner != key:
            out.append(f"fig_id {fid}（现属于 {owner}）")

    # items.item_id 也在这个名单里：它是 Zotero 的数值 id，而 zotero.sqlite 的
    # items 表同样是 INTEGER PRIMARY KEY、**没有 AUTOINCREMENT**（本机实测确认）
    # —— 最后一条条目被清掉之后，新条目会拿到它的 itemID。
    # 这一处被顶掉的表现不是覆盖，而是 INSERT 直接撞 UNIQUE：restore 整轮失败、
    # 条目永远回不来（改这一版之前连一条日志都不留，见 convert 那边的 else 分支）。
    iid = (snap.get("items") or {}).get("item_id")
    if iid is not None:
        owner = _owner(conn, _OWNER_ITEM, iid)
        if owner and owner != key:
            out.append(f"item_id {iid}（现属于 {owner}）")
    return out


def _conflict_note(conflicts: list[str]) -> str:
    """把冲突清单压成一句人读的话（清单本身可能上百条）。"""
    head = "、".join(conflicts[:3])
    more = f" 等 {len(conflicts)} 处" if len(conflicts) > 3 else ""
    return (f"检测到 id 已被复用（{head}{more}），已降级为重建："
            f"数据库行一概不复原，交给紧随其后的增量重抽")


def _mark_snapshot_stale(tdir: str, conflicts: list[str]) -> str:
    """把 rows.json 改名成 rows.stale.json 并写明原因（返回新文件名，没动返回空串）。

    为什么改名而不是留着：归档目录一般会在还原的最后被清掉，但**中途崩溃**时它会
    留下，下一轮对账看到 kind='trashed' 的墓碑就还会再调一次 restore() —— 那时如果
    rows.json 还在，同一份「会覆盖别人」的快照会被再喂一次。名字一换，
    `_read_json(ROWS_JSON)` 自然读不到，那条路就退化成「按新建重抽」。
    """
    src = os.path.join(tdir, ROWS_JSON)
    dst = os.path.join(tdir, ROWS_STALE_JSON)
    if not os.path.exists(src):
        return ""
    data = _read_json(src)
    if data:
        data["stale"] = {"at": now_iso(), "reason": "restore-id-conflict",
                         "conflicts": conflicts[:20]}
        _write_json(dst, data)
        try:
            os.remove(src)
        except OSError:
            pass
    else:
        # 快照读不动（坏了）：原样改名，别把证据写成空字典
        try:
            os.replace(src, dst)
        except OSError:
            return ""
    return ROWS_STALE_JSON


def _purge_derived_rows(conn, key: str) -> None:
    """删掉该条的**派生行**：切片 / 全文索引 / 向量 / 图注（**不动 items 行**）。

    与 `_delete_rows` 共用这一份 SQL（同一件事只允许一份实现）：归档要连 items 行
    一起删，而还原的「先删净再直插」只删派生行。**只删本条目的行**，别人的不碰。
    """
    conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                 "(SELECT chunk_id FROM chunks WHERE item_key = ?)", (key,))
    conn.execute("DELETE FROM embeddings WHERE chunk_id IN "
                 "(SELECT chunk_id FROM chunks WHERE item_key = ?)", (key,))
    conn.execute("DELETE FROM chunks WHERE item_key = ?", (key,))
    conn.execute("DELETE FROM figures WHERE item_key = ?", (key,))


def _restore_rows(conn, key: str, snap: dict,
                  version: int | None = None) -> dict:
    """按 rows.json 直插数据库行（阶段③）。**探到 id 冲突就整条不写**。

    顺序有讲究：先 items（chunks/figures 的外键指向它），再 chunks + chunks_fts。
    `chunks_fts` 是**独立**的 fts5 表（rowid = chunk_id，见 schemas.py 的注释），
    它没有外部内容表那套触发器，所以必须自己写一行 —— 漏了就变成「条目回来了、
    kb_search 却搜不到它」，而且不报错。

    返回里 `degraded=True` 表示**一个字节都没写**（探到 id 冲突），
    由 restore() 改成只搬文件 + 交给紧随其后的增量重建。
    """
    res = {"items": 0, "chunks": 0, "figures": 0, "embeddings": 0,
           "degraded": False, "conflicts": []}
    conflicts = _id_conflicts(conn, key, snap)
    if conflicts:
        # **整条放弃直插**：宁可整条慢一点，也绝不部分覆盖 ——
        # 部分覆盖的后果见上面那一节（别人那篇会变成增量永远修不到的空壳）。
        res["degraded"] = True
        res["conflicts"] = conflicts
        return res

    item = snap.get("items") or {}
    # Zotero 进出回收站都会给 version 加一；直插的是归档时的旧号，
    # 不改写的话紧接着的增量会认为「变了」，把刚插回来的切片全删了重抽。
    # 所以把**当前** version 写进去 —— 这是「秒级还原」真正成立的条件。
    if item and version is not None:
        item = dict(item)              # 别改快照本身
        item["version"] = version
    if item:
        cols = [c for c in item if c in _table_columns(conn, "items")]
        # 已经有这条（崩溃后重跑）就别动它：库里那份可能比快照新
        if cols and not conn.execute("SELECT 1 FROM items WHERE key = ?",
                                     (key,)).fetchone():
            ph = ",".join("?" * len(cols))
            conn.execute(f"INSERT INTO items({','.join(cols)}) VALUES({ph})",
                         [item[c] for c in cols])
            res["items"] = 1

    # 先按 key 删净该条的残留行，再**普通 INSERT**（不再用 OR REPLACE）：
    #   · `INSERT OR REPLACE` 只按主键判重 —— 同 key 的半截行（崩溃后重跑、上一轮
    #     插了一半）它既清不掉也补不全，结果是一条文献混着两份切片；
    #   · 先删净再插是幂等的：重跑一次的结果与第一次完全相同。
    #   · 普通 INSERT 在探过冲突之后是安全的：每个 id 要么空着、要么是本条目自己的
    #     残留（别人的行在上面那一关就已经整条劝退了）。
    _purge_derived_rows(conn, key)

    chunk_cols = ("chunk_id", "item_key", "page", "seq", "text", "n_chars")
    for row in snap.get("chunks") or []:
        conn.execute(f"INSERT INTO chunks({','.join(chunk_cols)}) "
                     f"VALUES({','.join('?' * len(chunk_cols))})",
                     [row.get(c) for c in chunk_cols])
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row.get("chunk_id"),))
        conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
                     (row.get("chunk_id"), row.get("text") or ""))
        res["chunks"] += 1

    fig_cols = ("fig_id", "item_key", "page", "kind", "label", "text",
                "n_rows", "n_cols", "built_at")
    for row in snap.get("figures") or []:
        conn.execute(f"INSERT INTO figures({','.join(fig_cols)}) "
                     f"VALUES({','.join('?' * len(fig_cols))})",
                     [row.get(c) for c in fig_cols])
        res["figures"] += 1

    for row in snap.get("embeddings") or []:
        try:
            blob = base64.b64decode(row.get("vector_b64") or "")
        except Exception:        # noqa: BLE001 —— 坏掉的那一条不挡整篇的还原
            continue
        conn.execute("INSERT INTO embeddings(chunk_id, dim, vector, model) "
                     "VALUES(?,?,?,?)",
                     (row.get("chunk_id"), row.get("dim"), blob, row.get("model")))
        res["embeddings"] += 1
    conn.commit()
    return res


def _delete_rows(conn, key: str) -> dict:
    """删掉该条在索引库里的全部行（切片 / FTS / 向量 / 图注 / 体检 / 条目行）。

    这里原来是 convert.py 的 `_purge_key()`，原样搬过来（**唯一实现**）。
    派生行那四条 SQL 与还原的 `_purge_derived_rows` **共用一份**（别在两边各写一遍）。
    """
    n = {
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks WHERE item_key = ?",
                               (key,)).fetchone()[0],
        "figures": conn.execute("SELECT COUNT(*) FROM figures WHERE item_key = ?",
                                (key,)).fetchone()[0],
    }
    _purge_derived_rows(conn, key)
    try:
        conn.execute("DELETE FROM item_health WHERE item_key = ?", (key,))
    except sqlite3.Error:
        pass                     # 表还没建过（没跑过体检）
    conn.execute("DELETE FROM items WHERE key = ?", (key,))
    conn.commit()
    return n


# ---------------------------------------------------------------- 目录盘点（面板/对账用）


def archived_keys() -> list[str]:
    """kb/trash/ 下现有的归档条目 key。"""
    try:
        return sorted(n for n in os.listdir(trash_root())
                      if os.path.isdir(os.path.join(trash_root(), n)))
    except OSError:
        return []


def archived_size(key: str) -> int:
    return _dir_size(trash_dir(key))


def trash_size() -> int:
    """整个 kb/trash/ 的体积（面板显示"可回收多少空间"用）。"""
    return _dir_size(trash_root()) if os.path.isdir(trash_root()) else 0


# ---------------------------------------------------------------- 三个动作


def archive(conn, key: str, note: str = "", *,
            with_embeddings: bool = True) -> dict:
    """阶段①：把一条文献归档进 `kb/trash/<key>/`，并从索引库里摘掉它。

    幂等：崩溃后重跑、连着跑两次索引都只会把同一件事再做一遍（文件已在 trash 里
    就跳过，rows.json 覆盖成最新的一份）。返回
    `{"key","dir","files","rows","ok","why"}`。
    """
    tdir = trash_dir(key)
    res: dict = {"key": key, "dir": tdir, "files": [], "rows": {}, "ok": True, "why": ""}
    try:
        os.makedirs(tdir, exist_ok=True)
        # 快照先落盘：万一在"搬文件"或"删行"之间崩了，下次跑还能靠它还原。
        #
        # ⚠ 但**不能无条件覆盖**：崩溃/重跑时库里那行可能已经删掉了，这时再导一次
        #   会得到一份空的快照，把上一轮那份好的盖掉 —— 还原就再也回不来了
        #   （本机实测：连跑两次归档，rows.json 变成空壳，还原后条目只剩个壳）。
        #   判据：已有快照里有 items 行、而现在库里没有 → 留着旧的那份。
        snap_path = os.path.join(tdir, ROWS_JSON)
        kept = _read_json(snap_path)
        still_in_db = conn.execute("SELECT 1 FROM items WHERE key = ?",
                                   (key,)).fetchone() is not None
        if kept.get("items") and not still_in_db:
            snap = kept
        else:
            snap = _rows_snapshot(conn, key, with_embeddings=with_embeddings)
            _write_json(snap_path, snap)
        res["rows"] = dict(snap.get("counts") or {})
        res["rows"]["items"] = 1 if snap.get("items") else 0

        for src, sub in _kb_key_files(key):
            dst = os.path.join(tdir, sub, os.path.basename(src))
            if _move_path(src, dst):
                res["files"].append(f"{sub}/{os.path.basename(src)}")
        # MinerU 产物：G.0 里点名的**盲区**（原来根本不清理，一篇几十 MB 永久堆积）
        mdir = _mineru_dir(key)
        if os.path.isdir(mdir) and _move_path(mdir, os.path.join(tdir, MINERU_SUB)):
            res["files"].append(f"{MINERU_SUB}/")

        _write_json(os.path.join(tdir, TOMBSTONE_JSON), {
            "key": key, "archived_at": snap["archived_at"], "kind": "trashed",
            "note": note, "kb_version": SNAPSHOT_VERSION,
        })
        # 墓碑留名（T0-13）：名字只有**这一下**手上还有（下一行就把 items 行删了）。
        # 崩溃后重跑时库里那行已经没了，还有归档快照里的 items 行兜底。
        name = _item_name(conn, key, fallback=snap.get("items"))
        _delete_rows(conn, key)
        _set_tombstone(conn, key, "trashed", note=note,
                       deleted_at=snap["archived_at"], name=name)
    except Exception as exc:     # noqa: BLE001 —— 一条坏掉不该让整轮构建失败
        res["ok"] = False
        res["why"] = f"{type(exc).__name__}: {exc}"
    return res


def restore(conn, key: str, version: int | None = None) -> dict:
    """阶段③：产物搬回原位、按 rows.json 复原数据库行、清墓碑。

    返回里的 `rows=False` 说明**数据库行没有复原**，两种原因：
      · rows.json 缺失/损坏 → `rebuilt=True`；
      · 快照里的 id 已被别的条目复用（T0-11）→ `degraded=True`，
        `conflicts` 列出前几条冲突、`note` 是给人看的一句话。
    两种情况下**文件都照搬回来了**，这条会在紧接着的增量里被当成**新条目重抽
    一遍**（H.4 说的退路：MinerU 产物还在，不必重新解析 PDF）。
    """
    tdir = trash_dir(key)
    res: dict = {"key": key, "files": [], "rows": False, "rebuilt": False,
                 "version_used": version,
                 "degraded": False, "conflicts": [], "note": "",
                 "ok": True, "why": ""}
    try:
        snap = _read_json(os.path.join(tdir, ROWS_JSON))
        for src, sub in _trash_key_files(key):
            dst = os.path.join(_kb_dirs()[sub], os.path.basename(src))
            if _move_path(src, dst):
                res["files"].append(f"{sub}/{os.path.basename(src)}")
        mdir = os.path.join(tdir, MINERU_SUB)
        if os.path.isdir(mdir) and _move_path(mdir, _mineru_dir(key)):
            res["files"].append(f"{MINERU_SUB}/")

        if snap:
            got = _restore_rows(conn, key, snap, version=version)
            res["rows"] = not got.get("degraded")
            if got.get("degraded"):
                # 直插会覆盖别人的行（见 _id_conflicts 那一节）—— 整条放弃，
                # 只把文件搬回来，剩下的交给紧随其后的增量重抽。
                res["degraded"] = True
                res["rebuilt"] = True          # 与"快照缺失"同一条退路
                res["conflicts"] = list(got.get("conflicts") or [])
                res["note"] = _conflict_note(res["conflicts"])
                stale = _mark_snapshot_stale(tdir, res["conflicts"])
                if stale:
                    res["note"] += f"（{ROWS_JSON} 已改名为 {stale}，下次不会再拿它直插）"
                _log(f"还原 {key} 降级重建：{res['note']}")
        else:
            res["rebuilt"] = True
        _clear_tombstone(conn, key)
        # 归档目录收拾干净（rows.json/tombstone.json 已无用处：数据库才是事实源）
        shutil.rmtree(tdir, ignore_errors=True)
    except Exception as exc:     # noqa: BLE001
        res["ok"] = False
        res["why"] = f"{type(exc).__name__}: {exc}"
    return res


def delete_archived(conn, key: str, note: str = "") -> dict:
    """阶段②：彻底删除 —— 清掉 `kb/trash/<key>/`，墓碑改成 `kind='deleted'`。

    也用来收"一次都没归档过就直接消失"的条目（两个索引周期之间被删了、还把回收站
    清空了）：那种情况下 kb 下还留着它的文件、库里还留着行，所以顺手一并清掉，
    否则磁盘上会留下一份谁也认不出的孤儿产物。

    墓碑**保留**（不删行）：审计与评估集判分要靠它区分"这条被删了"与"这条从没进过库"
    （G.6 第 2 条）。
    """
    tdir = trash_dir(key)
    res: dict = {"key": key, "freed": 0, "files": [], "rows": {}, "ok": True, "why": ""}
    try:
        old = tombstone(conn, key)
        archived_at = str(old.get("deleted_at") or "") if old.get("kind") == "trashed" else ""
        # 墓碑留名（T0-13）：必须在清归档目录**之前**取 —— items 行可能早就不在库里
        # 了（阶段① 删过一次），那时只剩归档快照里那份可翻；两处都没有就留空。
        # 名字拿不到不影响删除，_item_name() 不会抛。
        name = _item_name(conn, key,
                          fallback=(_read_json(os.path.join(tdir, ROWS_JSON))
                                    or {}).get("items"))
        res["freed"] = archived_size(key)
        if os.path.isdir(tdir):
            shutil.rmtree(tdir, ignore_errors=True)
        # 没归档过的残留：原位文件 + MinerU 产物
        for src, sub in _kb_key_files(key):
            try:
                os.remove(src)
                res["files"].append(f"{sub}/{os.path.basename(src)}")
            except OSError:
                pass
        mdir = _mineru_dir(key)
        if os.path.isdir(mdir):
            res["freed"] += _dir_size(mdir)
            shutil.rmtree(mdir, ignore_errors=True)
            res["files"].append(f"{MINERU_SUB}/")
        res["rows"] = _delete_rows(conn, key)
        # 归档时间进 note：trash 目录一删就查不到了，留个痕
        parts = []
        if archived_at:
            parts.append(f"归档于 {archived_at}")
        parts.append(f"{now_iso()} 彻底删除")
        if note:
            parts.append(note)
        _set_tombstone(conn, key, "deleted", note="；".join(parts), name=name)
    except Exception as exc:     # noqa: BLE001
        res["ok"] = False
        res["why"] = f"{type(exc).__name__}: {exc}"
    return res


def discard_live(conn, key: str) -> dict:
    """条目**还在 Zotero 里**、只是没有 PDF 附件了：直接清掉。

    不归档、**不留墓碑** —— 理由见模块头部的 ④：留了 kind='trashed'，下一轮对账
    就会把这条 live 的条目当成"从回收站还原"搬回来，于是"删掉 PDF 的条目"会每轮复活。
    """
    res: dict = {"key": key, "files": [], "rows": {}, "ok": True, "why": ""}
    try:
        for src, sub in _kb_key_files(key):
            try:
                os.remove(src)
                res["files"].append(f"{sub}/{os.path.basename(src)}")
            except OSError:
                pass
        mdir = _mineru_dir(key)
        if os.path.isdir(mdir):
            shutil.rmtree(mdir, ignore_errors=True)
            res["files"].append(f"{MINERU_SUB}/")
        res["rows"] = _delete_rows(conn, key)
    except Exception as exc:     # noqa: BLE001
        res["ok"] = False
        res["why"] = f"{type(exc).__name__}: {exc}"
    return res
