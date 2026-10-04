"""经验层的**唯一写入实现**：新增 / 修改 / 删除 / 权重算术，都在这一份里。

## 为什么要有这个模块

同一段"回滚一条经验对权重的贡献"的 SQL，原来在
`tools/kb_admin.py:cmd_clean` 里**抄了两遍**（删单条一遍、清测试数据一遍），
而"加分"那一半在 `online/server.py:_bump_weights`，是第三种写法。三处一旦
漂移，就会出现"经验说无效、权重还挂着 +1"这种**没有任何报错**的脏状态。

更要紧的是：权重必须是**经验的函数**。所以任何改经验的动作（用户手改、
删一条、清测试数据）都必须"先回滚旧的、再应用新的"，而这两步只允许有一份
实现 —— 就是本模块的 `apply_weight_delta`。

## 为什么接 `Searcher` 而不是裸 sqlite 连接

`Searcher.write()` 的注释写得很清楚：**所有写都必须走它**，因为 MCP 不是
单线程的（DSH 每次 tools/call 可能换线程），绕过 `_lock` 会出现
`cannot start a transaction within a transaction` 并**静默丢写**（实测 20 条
并发只写进 13~17 条）。所以本模块只认一个"能 write / write_returning_id /
read_one 的对象"，MCP 侧传 `Searcher`，CLI 侧（单线程）传一个小适配器。

## `history` 与"只追加，永不覆盖"

`schemas` 里 experience 表写着 `-- 经验层：只追加，永不覆盖`。这条对**自动
流程**（learn.py 抽取、MCP 记经验）依然成立；本模块只在**用户显式编辑**时
改行，而且改之前把旧值 push 进 `history` 列（JSON 数组），所以历史仍可追溯。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

# outcome 白名单（与 MCP 工具、learn.py 的 pending 一致）
OUTCOMES = ("effective", "ineffective", "partial", "unknown")
# source 白名单。dsh=DSH 对话里记的；llm=小模型抽取并经人确认的；
# user=用户手填；zotero-chat=Zotero 窗格里那个本地模型聊天框记的。
SOURCES = ("dsh", "llm", "user", "zotero-chat")

# 可编辑字段（顺序即界面/CLI 里的展示顺序）
EDITABLE = ("asked", "outcome", "method", "context", "reason", "evidence",
            "tags", "item_keys")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def split_list(value) -> list[str]:
    """把"逗号/空格/分号分隔的字符串"或已有列表，规整成去重的字符串列表。

    （原 `server.re_split` 的逻辑；那边现在直接引用这一份，避免两份实现。）
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(v) for v in value]
    else:
        raw = (str(value).replace(",", " ").replace("，", " ")
               .replace(";", " ").replace("；", " ").split())
    out: list[str] = []
    for piece in raw:
        piece = piece.strip()
        if piece and piece not in out:
            out.append(piece)
    return out


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(text, default):
    if not text:
        return default
    try:
        got = json.loads(text)
    except (TypeError, ValueError):
        return default
    return got if isinstance(got, type(default)) else default


def validate(asked: str, outcome: str) -> str:
    """返回错误信息（空串=通过）。错误文案与 MCP 工具原来的一致，别改字。"""
    if outcome not in OUTCOMES:
        return f"outcome 必须是 {sorted(OUTCOMES)} 之一，收到 {outcome!r}"
    if not (asked or "").strip():
        return "asked 不能为空 —— 没有问题的经验以后检索不到。"
    return ""


# ---------------------------------------------------------------- 权重算术


def weight_triple(outcome: str) -> tuple[int, int, int]:
    """一次尝试落进 (effective, ineffective, partial) 的哪一格。"""
    return (1 if outcome == "effective" else 0,
            1 if outcome == "ineffective" else 0,
            1 if outcome == "partial" else 0)


def weight_statements(keys, outcome: str, sign: int = 1) -> list:
    """构造"把一次尝试计入（+1）/ 回滚（-1）文献战绩"的 SQL 语句表。

    ⚠ 这是**唯一**允许改 attempts/effective/ineffective/partial 的地方。
      `pinned / manual / note` 是人工分，与经验无关，这里一概不碰。
      返回语句表（而不是直接执行）是为了让"改经验"那三步能放进同一个事务。
    """
    if not keys or not outcome:
        return []
    eff, ineff, part = weight_triple(outcome)
    if eff + ineff + part == 0:          # outcome=unknown：只记尝试次数
        eff = ineff = part = 0
    out: list = []
    for key in keys:
        if sign >= 0:
            out.append((
                """
                INSERT INTO item_weight(item_key, attempts, effective, ineffective,
                                        partial, pinned, manual, updated_at)
                VALUES(?, 1, ?, ?, ?, 0, 0, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    attempts = attempts + 1,
                    effective = effective + ?,
                    ineffective = ineffective + ?,
                    partial = partial + ?,
                    updated_at = excluded.updated_at
                """,
                (key, eff, ineff, part, now_iso(), eff, ineff, part),
            ))
        else:
            out.append((
                """
                UPDATE item_weight SET
                    attempts = MAX(0, attempts - 1),
                    effective = MAX(0, effective - ?),
                    ineffective = MAX(0, ineffective - ?),
                    partial = MAX(0, partial - ?),
                    updated_at = ?
                WHERE item_key = ?
                """,
                (eff, ineff, part, now_iso(), key),
            ))
    return out


def apply_weight_delta(s, keys, outcome: str, sign: int = 1) -> None:
    """执行一次加/回滚（单独调用时用这个；多步改动请用 `write_many` 拼事务）。"""
    stmts = weight_statements(keys, outcome, sign)
    if not stmts:
        return
    _write_many(s, stmts)
    if hasattr(s, "reload_weights"):
        s.reload_weights()


def _write_many(s, statements: list):
    """优先用对象的 `write_many`（原子）；没有就逐条写（老适配器兜底）。

    返回 `(rowcount, rows)`（rows 来自带 RETURNING 的语句，见 add_experience）。
    """
    if hasattr(s, "write_many"):
        return s.write_many(statements)
    rowcount = 0
    for sql, params in statements:
        rowcount = s.write(sql, params)
    return rowcount, []


# ---------------------------------------------------------------- 增删改查


def get_experience(s, exp_id: int) -> dict | None:
    row = s.read_one("SELECT * FROM experience WHERE id = ?", (int(exp_id),))
    return dict(row) if row else None


def add_experience(s, *, asked: str, outcome: str, method: str = "",
                   item_keys=(), context: str = "", reason: str = "",
                   evidence: str = "", tags=(), source: str = "user",
                   session: str = "") -> int:
    """追加一条经验，并把它计入相关文献的权重。返回新 id。

    校验不过时抛 ValueError（文案与 MCP 工具原来的一致）。
    """
    outcome = (outcome or "").strip().lower()
    err = validate(asked, outcome)
    if err:
        raise ValueError(err)
    source = (source or "user").strip()
    if source not in SOURCES:
        raise ValueError(f"source 必须是 {sorted(SOURCES)} 之一，收到 {source!r}")
    keys = split_list(item_keys)
    tag_list = split_list(tags)
    # ⚠ INSERT 放**最后**并带 `RETURNING id`：同一批里插 item_weight 会把
    #   `last_insert_rowid()` 顶掉（实测踩过：拿到的是权重行的 id，
    #   于是刚写的经验查不到）。RETURNING 需要 SQLite ≥ 3.35。
    insert = (
        """
        INSERT INTO experience(created_at, asked, context, item_keys, method,
                               outcome, reason, evidence, tags, source, session)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        RETURNING id
        """,
        (now_iso(), asked.strip(), context, dumps(keys), method, outcome,
         reason, evidence, dumps(tag_list), source, session),
    )
    # 插入与"计入权重"放一个事务里：不许出现"经验写了、权重没加"
    _rowcount, rows = _write_many(s, weight_statements(keys, outcome, +1) + [insert])
    exp_id = 0
    if rows:
        exp_id = int(rows[0]["id"] if "id" in rows[0].keys() else rows[0][0])
    if not exp_id:
        # 兜底（老 SQLite 不支持 RETURNING 时）
        row = s.read_one("SELECT max(id) AS id FROM experience")
        exp_id = int(row["id"] or 0) if row else 0
    if hasattr(s, "reload_weights"):
        s.reload_weights()
    return int(exp_id)


def update_experience(s, exp_id: int, *, reason_suffix: str = "", **fields) -> dict:
    """改一条经验：先回滚旧权重 → 改行（旧值进 history）→ 应用新权重。

    只有真正传进来的字段会被改（`None` = 不动）。返回改动摘要，便于界面
    展示"改了什么"。
    """
    old = get_experience(s, exp_id)
    if not old:
        raise ValueError(f"没有 id={exp_id} 的经验")

    merged = {}
    for name in EDITABLE:
        if name in fields and fields[name] is not None:
            merged[name] = fields[name]
    if not merged:
        return {"ok": True, "changed": {}, "note": "没有要改的字段"}

    new_outcome = (merged.get("outcome") or old["outcome"]).strip().lower()
    new_asked = merged.get("asked", old["asked"])
    err = validate(new_asked, new_outcome)
    if err:
        raise ValueError(err)

    old_keys = loads(old.get("item_keys"), [])
    new_keys = split_list(merged["item_keys"]) if "item_keys" in merged else old_keys

    # 改动前后对照（只列真正变了的字段）
    before = {"asked": old["asked"], "outcome": old["outcome"],
              "method": old.get("method") or "", "context": old.get("context") or "",
              "reason": old.get("reason") or "", "evidence": old.get("evidence") or "",
              "tags": loads(old.get("tags"), []), "item_keys": old_keys}
    after = dict(before)
    for name in merged:
        if name == "outcome":
            after["outcome"] = new_outcome
        elif name in ("tags", "item_keys"):
            after[name] = split_list(merged[name])
        else:
            after[name] = merged[name]
    changed = {k: {"from": before[k], "to": after[k]}
               for k in before if before[k] != after[k]}

    # 旧值留档（"只追加，永不覆盖"对自动流程仍然成立；用户显式编辑则留痕）
    history = loads(old.get("history"), [])
    history.append({"at": now_iso(), "fields": before,
                    "why": reason_suffix or "用户/模型编辑"})

    # 三步必须在**同一个事务**里：回滚旧权重 → 改行 → 应用新权重。
    # 分开写的话，中间失败会留下"权重已回滚、经验行没改"的不一致状态，
    # 而且没有任何地方会报错（本机实测踩到：真库还缺 history 列）。
    stmts = weight_statements(old_keys, old["outcome"], -1)
    stmts.append((
        """
        UPDATE experience SET asked=?, outcome=?, method=?, context=?, reason=?,
                              evidence=?, tags=?, item_keys=?, history=?, updated_at=?
        WHERE id=?
        """,
        (after["asked"], after["outcome"], after["method"], after["context"],
         after["reason"], after["evidence"], dumps(after["tags"]),
         dumps(after["item_keys"]), dumps(history), now_iso(), int(exp_id)),
    ))
    stmts.extend(weight_statements(new_keys, new_outcome, +1))
    _write_many(s, stmts)
    if hasattr(s, "reload_weights"):
        s.reload_weights()
    return {"ok": True, "id": int(exp_id), "changed": changed,
            "outcome": new_outcome, "item_keys": new_keys}


def delete_experience(s, exp_id: int, *, rollback: bool = True) -> bool:
    """删一条经验；默认同时回滚它给权重加过的分（否则留下脏权重）。"""
    old = get_experience(s, exp_id)
    if not old:
        return False
    if rollback:
        apply_weight_delta(s, loads(old.get("item_keys"), []), old["outcome"], -1)
    s.write("DELETE FROM experience WHERE id = ?", (int(exp_id),))
    if rollback and hasattr(s, "reload_weights"):
        s.reload_weights()
    return True


def set_weight(s, key: str, pinned=None, manual=None, note=None) -> dict:
    """标重点 / 加减分 / 写备注（人工分，与经验计数互不干扰）。

    `None` = 这一项不动；`note=""` = **清空**备注（面板要能清）。
    """
    row = s.read_one("SELECT * FROM item_weight WHERE item_key=?", (key,))
    pinned_val = row["pinned"] if row else 0
    manual_val = row["manual"] if row else 0.0
    note_val = (row["note"] if row else "") or ""
    if pinned is not None:
        pinned_val = 1 if pinned else 0
    if manual is not None:
        manual_val = float(manual)
    if note is not None:
        note_val = str(note)[:500]
    s.write(
        """
        INSERT INTO item_weight(item_key, pinned, manual, note, updated_at, attempts,
                                effective, ineffective, partial)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(item_key) DO UPDATE SET
            pinned=excluded.pinned, manual=excluded.manual, note=excluded.note,
            updated_at=excluded.updated_at
        """,
        (key, pinned_val, manual_val, note_val, now_iso(),
         row["attempts"] if row else 0, row["effective"] if row else 0,
         row["ineffective"] if row else 0, row["partial"] if row else 0),
    )
    if hasattr(s, "reload_weights"):
        s.reload_weights()
    return {"ok": True, "key": key, "pinned": bool(pinned_val),
            "manual": manual_val, "note": note_val}


def row_view(row: dict) -> dict:
    """把库里的一行经验整理成界面/JSON 友好的形状（JSON 列拆成列表）。"""
    out = dict(row)
    out["item_keys"] = loads(row.get("item_keys"), [])
    out["tags"] = loads(row.get("tags"), [])
    out["history_count"] = len(loads(row.get("history"), []))
    out.pop("history", None)
    return out


# ---------------------------------------------------------------- 经验体检
#
# 判据放在**这一层**（而不是面板里）的原因：它是领域判断，面板/CLI/以后别的
# 入口都该用同一份。放 UI 里就只能靠人眼看界面验，写不了测试。
#
# ⚠ 这些词是"元工作"（关于知识库自己怎么搭）的强特征词。用户反馈过
#   「体检只筛选出 #11，#12 没筛选出来」—— #12 讲的是"文本源选型/ft-cache 缓存"，
#   正是这一类，所以这一组词要够宽。宁可在界面上多标几条（用户扫一眼就能判断），
#   也不要漏 —— 漏了的后果是脏数据留在**检索加权**的输入里。
META_WORK_WORDS = (
    "文本源", "ft-cache", "全文缓存", "缓存", "切片", "索引", "索引库", "嵌入",
    "bm25", "rrf", "页眉", "页脚", "字符偏移", "位移", "解析器", "版面",
    "pymupdf", "pdf2zh", "babeldoc", "localserver", "service-token",
    "manifest", "xpi", "venv", "pip", "插件", "打包", "侧载", "面板",
    "知识库自己", "zotero-kb", "kb_search", "kb_item", "kb_fulltext",
    "kb_experience", "kb_weight", "kb_reindex", "kb_stats", "mcp",
    "提示词", "prompt", "分级视图", "知识库的", "本库", "全库",
)


def keys_of(value) -> list:
    """把"库里的 item_keys 列"或"调用方给的字符串/列表"统一成 key 列表。

    ⚠ 库里存的是 **JSON 字符串**（`dumps(keys)` 写进去的）：`'[]'` / `'["A","B"]'`。
      用 `split_list` 去切它会把 `'[]'` 当成**一个 key**（按逗号切，切不开），
      于是"这条经验没有关联文献"这类判据**永远不成立** —— 实测被测试当场抓到
      （经验体检里那条"没有关联文献"从来没触发过）。
      所以：先按 JSON 解，解不出来再退回按分隔符切（面板表单是逗号分隔的）。
    """
    if isinstance(value, (list, tuple, set)):
        return split_list(value)
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        got = loads(text, [])
        if isinstance(got, list):
            return split_list([str(x) for x in got])
    return split_list(text)


def checkup_reason(row: dict) -> list:
    """经验体检：给一条经验列出"为什么可疑"（空列表 = 没意见）。

    **只标注，不自动删** —— 判据是启发式，最终由人决定（用户明确要自己删）。
    两条判据：

      · `item_keys` 空 → 这条经验没有任何文献引用它，**不参与检索加权**，
        也不会出现在任何一篇的档案里（用户库里那两条无关经验之一就是这种）；
      · `asked`/`method`/`reason` 命中**元工作特征词**（缓存/切片/索引/插件/
        打包…）→ "关于知识库自己怎么搭"的记录，属于工作档案。
        用户实测反馈过："体检只筛选出 #11，#12 没筛选出来" —— #12 正是这一类
        （讲"文本源选型 / ft-cache 缓存"），所以这一组词要够宽。
    """
    keys = keys_of(row.get("item_keys"))
    text = " ".join(str(row.get(k) or "") for k in ("asked", "method", "reason"))
    low = text.lower()
    why = []
    if not keys:
        why.append("没有关联文献（不参与加权）")
    hit = [w for w in META_WORK_WORDS if w.lower() in low]
    if hit:
        why.append("像是工具链/工程记录：" + ", ".join(hit[:4]))
    return why


class ConnWriter:
    """把普通 sqlite 连接包成上面这些函数要的写接口。

    ⚠ 只给**单线程**调用方用（CLI 工具、离线管道）。MCP 服务端必须传
      `Searcher` —— 它带 `_lock`，是并发下唯一安全的写入口（见模块头说明：
      绕过它会 `cannot start a transaction within a transaction` 并静默丢写）。
    """

    def __init__(self, conn):
        self.conn = conn

    def write(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    def write_returning_id(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def write_many(self, statements: list) -> tuple:
        """同一事务里跑多条（与 Searcher.write_many 同语义）。

        返回 `(rowcount, rows)`；带 `RETURNING` 的语句把结果行放进 rows。
        """
        rowcount = 0
        rows: list = []
        try:
            for sql, params in statements:
                cur = self.conn.execute(sql, params)
                rowcount = cur.rowcount
                if cur.description:
                    rows = cur.fetchall()
            self.conn.commit()
        except Exception:
            self.conn.rollback()      # 不回滚的话前几条会留在未提交事务里
            raise
        return rowcount, rows

    def last_insert_id(self) -> int:
        row = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()
        return int(row[0] if row else 0)

    def read_one(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchone()

    # 离线侧没有权重缓存要刷新，但保留同名方法让调用方不必分支
    def reload_weights(self) -> None:
        return None


# ---------------------------------------------------------------- 自检


def main(argv: list[str] | None = None) -> int:
    """`python offline/experience.py selftest`：用临时库验一遍权重算术。"""
    import argparse
    import os
    import sys
    import tempfile

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import schemas as S

    ap = argparse.ArgumentParser(description="经验层写入自检")
    ap.add_argument("cmd", nargs="?", default="selftest")
    args = ap.parse_args(argv)
    if args.cmd != "selftest":
        print("只有一个子命令：selftest")
        return 1

    conn = S.init_db(os.path.join(tempfile.mkdtemp(prefix="kbe_exp_"), "t.db"))
    w = ConnWriter(conn)
    ok = True

    eid = add_experience(w, asked="自检经验", outcome="effective",
                         item_keys="AAAA1111", tags="自检,测试", source="user")
    wt = conn.execute("SELECT * FROM item_weight WHERE item_key='AAAA1111'").fetchone()
    ok &= wt["attempts"] == 1 and wt["effective"] == 1
    print(f"  加分后：attempts={wt['attempts']} effective={wt['effective']}")

    update_experience(w, eid, outcome="ineffective", item_keys="AAAA1111,BBBB2222")
    a = conn.execute("SELECT * FROM item_weight WHERE item_key='AAAA1111'").fetchone()
    b = conn.execute("SELECT * FROM item_weight WHERE item_key='BBBB2222'").fetchone()
    ok &= a["effective"] == 0 and a["ineffective"] == 1
    ok &= b["attempts"] == 1 and b["ineffective"] == 1
    print(f"  改成 ineffective 并换文献后：AAAA effective={a['effective']} "
          f"ineffective={a['ineffective']}；BBBB attempts={b['attempts']}")
    row = get_experience(w, eid)
    ok &= row["history"] not in (None, "", "[]")
    print(f"  旧值进了 history：{len(loads(row['history'], []))} 条")

    delete_experience(w, eid)
    a2 = conn.execute("SELECT * FROM item_weight WHERE item_key='AAAA1111'").fetchone()
    b2 = conn.execute("SELECT * FROM item_weight WHERE item_key='BBBB2222'").fetchone()
    ok &= a2["attempts"] == 0 and a2["ineffective"] == 0
    ok &= b2["attempts"] == 0 and b2["ineffective"] == 0
    print(f"  删除并回滚后：AAAA attempts={a2['attempts']}；"
          f"BBBB attempts={b2['attempts']}")
    print("  自检结果：", "通过" if ok else "**不通过**")
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
