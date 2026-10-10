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

## id 是**连续编号**（删除后自动重排，从 1 开始）

用户看到库里的 id 是 2、3、…、10（1 被早期测试删过），问「编号应该自动更新、
从 1 开始」。删除后留空号对"历史引用"更友好，但界面上就是一串跳号的数字，
越删越乱 —— 所以**删完自动把 id 重排成 1..N**（`renumber()`，见那里的两段式
写法的说明）。库里没有任何地方按 id 引用经验（history 是行内 JSON、
item_weight 按 item_key、pending.jsonl 用的是会话号），所以重排是安全的。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# outcome 白名单（与 MCP 工具、learn.py 的 pending 一致）
OUTCOMES = ("effective", "ineffective", "partial", "unknown")
# source 白名单。dsh=DSH 对话里记的；llm=小模型抽取并经人确认的；
# user=用户手填。（原来的 zotero-chat 随"内容窗格聊天"一起删了，2026-10-05）
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


def delete_experience(s, exp_id: int, *, rollback: bool = True,
                      renumber_ids: bool = True) -> bool:
    """删一条经验；默认同时回滚它给权重加过的分（否则留下脏权重）。

    删完默认把 id 重排成 1..N（`renumber_ids=False` 可关掉 —— 批量删除时
    由 `delete_experiences` 统一重排一次，见那里的说明）。
    """
    old = get_experience(s, exp_id)
    if not old:
        return False
    stmts = []
    if rollback:
        stmts.extend(weight_statements(loads(old.get("item_keys"), []),
                                       old["outcome"], -1))
    stmts.append(("DELETE FROM experience WHERE id = ?", (int(exp_id),)))
    if renumber_ids:
        stmts.extend(_renumber_statements(s, skip=(int(exp_id),)))
    _write_many(s, stmts)
    if rollback and hasattr(s, "reload_weights"):
        s.reload_weights()
    return True


def delete_experiences(s, exp_ids, *, rollback: bool = True) -> list[int]:
    """一次删多条：**同一个事务**，删完只重排一次编号。返回真正删掉的 id。

    ⚠ 为什么不能循环调用 `delete_experience`：每删一条都会立刻重排编号，
      下一条要删的 id 可能已经指到**别的行**上了（面板的多选删除、
      `kb_admin.py clean --test-data` 都是批量删的，会删错）。
    """
    want = []
    for i in exp_ids:
        i = int(i)
        if i not in want:
            want.append(i)
    olds = []
    for i in want:
        row = get_experience(s, i)
        if row:
            olds.append(row)
    if not olds:
        return []
    gone = [int(o["id"]) for o in olds]
    stmts = []
    if rollback:
        for old in olds:
            stmts.extend(weight_statements(loads(old.get("item_keys"), []),
                                           old["outcome"], -1))
    marks = ",".join("?" * len(gone))
    stmts.append((f"DELETE FROM experience WHERE id IN ({marks})", tuple(gone)))
    stmts.extend(_renumber_statements(s, skip=gone))
    _write_many(s, stmts)
    if rollback and hasattr(s, "reload_weights"):
        s.reload_weights()
    return gone


# ---------------------------------------------------------------- id 连续编号


def all_ids(s) -> list[int]:
    """库里全部经验 id（升序）。拿不到读接口时返回空列表（不报错）。"""
    reader = getattr(s, "read", None) or getattr(s, "read_all", None)
    if reader is None:
        return []
    out: list[int] = []
    for row in (reader("SELECT id FROM experience ORDER BY id") or []):
        try:
            out.append(int(row["id"]))
        except (TypeError, IndexError, KeyError):
            out.append(int(row[0]))
    return out


def _renumber_statements(s, skip=()) -> list:
    """生成"把 id 压成 1..N"的语句表（已经连续时返回空表）。

    ⚠ 必须**两段式**：`id` 是主键，直接改名会当场撞车（想写 2，而 2 还在）。
      所以先把整表改成负数（落在目标区间之外），再逐个写成目标值。
      整个过程在**同一个事务**里，中途失败会整体回滚。
    """
    skip_set = {int(x) for x in skip}
    ids = [i for i in all_ids(s) if i not in skip_set]
    want = list(range(1, len(ids) + 1))
    if ids == want:
        return []
    stmts = [("UPDATE experience SET id = -id", ())]
    for old, new in zip(ids, want):
        stmts.append(("UPDATE experience SET id = ? WHERE id = ?", (new, -old)))
    return stmts


def renumber(s) -> dict:
    """把经验 id 重排成 1..N 连续（已经连续就什么都不做）。"""
    before = all_ids(s)
    stmts = _renumber_statements(s)
    if not stmts:
        return {"ok": True, "moved": 0, "before": before, "after": before}
    _write_many(s, stmts)
    return {"ok": True, "moved": len(before), "before": before,
            "after": list(range(1, len(before) + 1))}


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


# ---------------------------------------------------------------- 关联文献的显示
#
# 「一条经验关联的文献该怎么显示」的**唯一实现**（"唯一写路径"的同类要求）：
# 经验详情、权重列表、经验编辑器都调 item_refs()，谁都不许在自己那边 join(keys)。
#
# 为什么值得单独一层（不是面板里拼字符串的小事）：
#   · key 是 8 位大写字母数字（22X9PMR6），唯一但认不出是哪篇（F.5 第 1 条，用户痛点）；
#   · 更要紧的是**死引用**：文献被删（item_tombstone）或被合并（item_merge）之后，
#     面板原来照样把那个 key 印出来 —— 看着像"这条经验还挂着这篇"，其实早就不在
#     库里了（G.0 的「经验失效引用」盲区 / H.3 第 10 条）。
#
# 六种状态 = 四种判定（正常/已删除/已合并/库里没有）+ 两个异常态（成环/读不到库）。
# 判据一律来自 `keys.resolve_key()`，**不在这里重写合并链与墓碑的判断**：
#
#   REF_OK       正常      `作者 年份 · 短标题  [KEY]`（schemas.human_label 现成的）
#   REF_DELETED  已删除    `（已删除）  [KEY]`；名字有**两级回退**：先翻归档快照
#                          （`trash/<key>/rows.json`），翻不到再用墓碑里留的那一份
#                          （T0-13 的 title/first_author/year）——
#                          `kind='deleted'` 的归档目录早被清掉了，墓碑是最后的来源
#   REF_MERGED   已合并    **保留项**的可读名 + `（已合并到 保留项KEY）`；
#                          方括号里仍是**用户当初记下的那个 key**（可追溯，不改历史）
#   REF_MISSING  库里没有  `（无此条目：KEY）`（不静默显示成空）
#   REF_CYCLE    合并成环  `（合并映射成环：A → B → A）  [KEY]`（数据错误，必须说出来）
#   REF_ERROR    读不到库  `（读不到知识库：<原因>）  [KEY]`
#
# 批量取数：三条 `SELECT … WHERE key IN (…)`（① 输入的 key，② 合并链上的保留项 ——
# 少查这一下，已合并的引用就只能显示成"保留项也不在库里"，③ **墓碑留名**，
# 只对"已不在库里"的那几条查 —— 正常引用一个字都不多查），
# 之后每条只过一遍 resolve_key()（它读的是进程内缓存，不再查库）。
# **别在渲染循环里逐条查库** —— 面板是 Tk 主线程，查一次库就少一帧。
#
# 只读：本层**一个字都不写回** experience.item_keys（G.4 第 5 条：那是用户手记，
# 改写等于篡改历史）。失效引用只标注，清不清、改不改由用户自己判断。

REF_OK = "ok"
REF_DELETED = "deleted"
REF_MERGED = "merged"
REF_MISSING = "missing"
REF_CYCLE = "cycle"
REF_ERROR = "error"


def _ref_conn(obj):
    """把 None / 库路径 / sqlite 连接 / Searcher / ConnWriter 统一成 (连接, 是不是本函数开的)。

    为什么要认这么多种：调用点手里拿的东西不一样 —— 经验详情位什么都不拿（None）、
    权重列表拿的是 Searcher、经验编辑器拿的是 ConnWriter。让**显示层**去适配它们，
    而不是让每个面板各自准备连接。
    """
    import sqlite3

    import schemas as S

    if isinstance(obj, sqlite3.Connection):
        return obj, False
    inner = getattr(obj, "conn", None)       # Searcher / ConnWriter 都是包一层的
    if isinstance(inner, sqlite3.Connection):
        return inner, False
    path = obj if isinstance(obj, str) and obj else S.INDEX_DB
    # ⚠ sqlite3.connect() 对**不存在的路径**会当场建一个空库出来 —— 显示层不该有
    #   这种副作用，也免得把"知识库没了"显示成"这些 key 都不存在"。先查存在性。
    if path and not path.startswith(":") and not os.path.exists(path):
        raise FileNotFoundError(f"索引库不存在：{path}")
    return S.connect(path), True


def _ref_row(row, names) -> dict:
    """把一行（sqlite3.Row 或普通元组）按**钉死的列名**取成 dict。

    ⚠ 两种都要支持：连接是自己开的（schemas.connect 设了 row_factory=Row），
      但调用方可能塞一个裸 sqlite3.connect() 进来（测试里就有）。
      T0-2 踩过的坑是"SELECT 一列却读 r[1]"—— 这里按列名取，且 SELECT 列表
      就在调用处上面几行，不会漂。
    """
    try:
        return {n: row[n] for n in names}
    except (TypeError, IndexError, KeyError):
        return dict(zip(names, tuple(row)))


def _ref_rows(conn, keys: list) -> dict:
    """一次问库：`{key: {title, first_author, year}}`（查不到的 key 不在返回里）。

    分批是怕 key 太多撞上 SQLite 的变量上限（默认 999 个 ?）。读不到（老库缺表、
    库文件坏了）时返回 {} —— 上层会把它显示成"无此条目"，而不是抛进 Tk 回调里。
    """
    import sqlite3

    names = ("key", "title", "first_author", "year")
    out: dict = {}
    for i in range(0, len(keys), 400):
        part = keys[i:i + 400]
        sql = ("SELECT key, title, first_author, year FROM items "
               "WHERE key IN (%s)" % ",".join("?" * len(part)))
        try:
            rows = conn.execute(sql, tuple(part)).fetchall()
        except sqlite3.Error:
            return out
        for r in rows:
            got = _ref_row(r, names)
            out[str(got["key"])] = {"title": got["title"] or "",
                                    "first_author": got["first_author"] or "",
                                    "year": got["year"]}
    return out


def _ref_snapshot(key: str) -> dict:
    """墓碑那条**历史上**的可读名：归档快照 `trash/<key>/rows.json` 里的 items 行。

    kind='trashed' 的条目归档目录还在，能翻出标题/作者/年份；kind='deleted' 的
    归档目录已被"彻底删除"清掉，读不到就返回 {}（没有别的历史记录可翻）—— 那时
    界面上只剩 `（已删除）  [KEY]`，虽然认不出是哪篇，但**明确说了它没了**，
    比继续显示一个死 key 强。
    读文件只发生在**失效的那几条**上（正常引用不碰磁盘），而且每条只读一次。
    """
    try:
        import trash as TR
        with open(os.path.join(TR.trash_dir(key), TR.ROWS_JSON),
                  encoding="utf-8") as fh:
            snap = json.load(fh)
    except Exception:        # noqa: BLE001 —— 缺了/坏了/没装 trash 都按"没有"处理
        return {}
    it = (snap or {}).get("items") or {}
    if not isinstance(it, dict):
        return {}
    return {"title": it.get("title") or "",
            "first_author": it.get("first_author") or "",
            "year": it.get("year")}


def _ref_tombstones(conn, keys: list) -> dict:
    """墓碑里留的"删前叫什么"（T0-13）：`{key: {title, first_author, year}}`。

    为什么需要它：`kind='deleted'` 的条目归档目录已被"彻底删除"清掉，
    `_ref_snapshot()` 翻不到 —— 而归档那一刻抄进墓碑的这三个字段还在，
    是**唯一**还能认出是哪篇的地方（老墓碑三列是空的，那就继续"认不出"）。

    读不到（老库还没补这三列、表坏了）返回 {} —— 上层退回只显示"（已删除）"，
    绝不因此把整条引用报成错误。同样按列名取，兼容裸 sqlite3 连接的元组行。
    """
    import sqlite3

    import schemas as S

    names = ("key",) + tuple(S.TOMBSTONE_NAME_COLS)
    out: dict = {}
    for i in range(0, len(keys), 400):
        part = keys[i:i + 400]
        sql = ("SELECT %s FROM item_tombstone WHERE key IN (%s)"
               % (", ".join(names), ",".join("?" * len(part))))
        try:
            rows = conn.execute(sql, tuple(part)).fetchall()
        except sqlite3.Error:
            return out
        for r in rows:
            got = _ref_row(r, names)
            # 与 _ref_rows() 同一个形状（title/first_author 兜成空串，year 原样）
            out[str(got["key"])] = {"title": got["title"] or "",
                                    "first_author": got["first_author"] or "",
                                    "year": got["year"]}
    return out


def _ref_resolve(conn, key: str) -> tuple:
    """resolve_key() 的一层包装：把异常变成"状态"，而不是抛进 Tk 回调里。

    返回 (解析结果, 出错状态, 出错文案)；一切正常时后两项都是 ""。
    解析结果是 None = 命中墓碑（这条已不在库里）。
    """
    import keys as K

    try:
        return K.resolve_key(key, conn), "", ""
    except K.MergeCycle as exc:
        # 环是**数据错误**：静默取根会把一条错的映射当成权威，之后所有引用都跟着错
        return None, REF_CYCLE, f"（合并映射成环：{' → '.join(exc.cycle)}）"
    except Exception as exc:     # noqa: BLE001 —— 解析不了也别让详情/列表空白
        return None, REF_ERROR, f"（查不了这个 key：{type(exc).__name__}: {exc}）"


def _ref_one(key: str, res: tuple, rows: dict, snaps: dict, tombs: dict,
             limit: int) -> dict:
    """一条 key 的显示（内部用，状态见上面那张表）。

    res 是 _ref_resolve() 的结果；rows 里**已经含有合并链上的保留项**
    （item_refs 分趟批量补齐，所以这里不必、也不该再查库）。
    snaps 是归档快照里翻出来的历史名（懒读、按 key 缓存），tombs 是墓碑里留的名
    （T0-13）—— 后者已经批量查好，只有"已不在库里"的 key 才会用到。
    """
    import schemas as S

    resolved, err_status, err_text = res
    out = {"key": key, "status": REF_MISSING, "resolved": "",
           "title": "", "first_author": "", "year": "", "text": ""}
    if err_status:
        out["status"] = err_status
        out["text"] = f"{err_text}  [{key}]"
        return out
    live = rows.get(key)

    if resolved is not None and resolved != key:
        # 有合并映射：显示**保留项**的可读名（dc:replaces 链取根由 resolve_key 做）
        root = rows.get(resolved)
        out["status"] = REF_MERGED
        out["resolved"] = resolved
        if root:
            for f in ("title", "first_author", "year"):
                out[f] = root.get(f) or ""
            ref = S.human_ref(root["title"], root["first_author"], root["year"],
                              limit)
            out["text"] = f"{ref}  [{key}]（已合并到 {resolved}）"
        else:
            # 并过去的那条后来被彻底删了（resolve_key 的说明里点名过这种边界）
            out["text"] = f"（已合并到 {resolved}，但该条目也已不在库里）  [{key}]"
        return out

    if resolved is None:
        if live:
            # 墓碑说它已删、索引库里却还有它 —— 只可能是"刚还原、墓碑还没清"的
            # 中间态（trash.restore() 会清墓碑）。按**还在库里**显示，但把矛盾说出来。
            out["status"] = REF_OK
            out["resolved"] = key
            for f in ("title", "first_author", "year"):
                out[f] = live.get(f) or ""
            out["text"] = S.human_label(live["title"], live["first_author"],
                                        live["year"], key, limit) \
                + "（墓碑里记着已删除，但索引库里还在）"
            return out
        if key not in snaps:
            snaps[key] = _ref_snapshot(key)
        snap = snaps[key] or {}
        tomb = tombs.get(key) or {}
        out["status"] = REF_DELETED
        # 名字两级回退（T0-13）：归档快照（kind='trashed' 时归档目录还在）→
        # 墓碑里留的那一份（kind='deleted' 时归档目录已被清掉，只剩它）。
        # 两处都没有（老墓碑三列是空的）就回落到只显示"（已删除）"——
        # 缺哪一段都可能是 ""，所以逐字段取第一个非空，不会写出 None/nan。
        for f in ("title", "first_author", "year"):
            out[f] = snap.get(f) or tomb.get(f) or ""
        head = (S.human_ref(out["title"], out["first_author"], out["year"], limit)
                if out["title"] else "")
        out["text"] = f"{head}（已删除）  [{key}]" if head else f"（已删除）  [{key}]"
        return out

    if live:
        out["status"] = REF_OK
        out["resolved"] = key
        for f in ("title", "first_author", "year"):
            out[f] = live.get(f) or ""
        out["text"] = S.human_label(live["title"], live["first_author"],
                                    live["year"], key, limit)
        return out

    # 库里没有、也没有墓碑/映射：很久以前（T0-1 之前）被清掉的，或者根本是笔误。
    # 两者在数据上分不开，所以只报"库里没有"，不猜是哪种。
    out["status"] = REF_MISSING
    out["text"] = f"（无此条目：{key}）"
    return out


def item_refs(value, conn=None, *, limit: int = 40) -> dict:
    """把经验的 `item_keys`（或任意 key 列表）解析成**能给人看**的一组标签。

    返回的键（调用方直接用这些，别再自己拼 key 或另写判断）：

        items    `[{key, status, resolved, title, first_author, year, text}]`，顺序 = 输入顺序
        text     单行形态：`甲；乙`（对话框预览、日志这类窄处用）
        lines    每条的 text（要自己排版时用）
        block    多行形态，**自带换行**、每行以「· 」开头（空列表时是 ""）：
                 直接接在「关联文献：」后面就是一段列表 —— 详情位与编辑器用它
        total    key 条数（去重后）；注意 0 不是错误（这条经验就是没关联文献）
        live     **还在库里**的条数（已合并、且保留项在库里的也算）
        dead     已不在库里的条数（已删除 + 无此条目 + 并到一条已删文献）
        summary  全失效/部分失效时的一句话提示（正常时是 ""）——
                 只提示，**绝不自动清空** item_keys（那是用户手记）
        error     读不到知识库时的原因（正常时是 ""）

    `value` 容忍历史脏值：`None`、空串、库里那一列的 JSON 字符串、列表、重复 key、
    两头空白、非字符串 —— 全部交给 `keys_of()` 规整。

    `conn` 可以是 None（用默认库，函数自己开关连接）、库路径、sqlite 连接，
    或 Searcher / ConnWriter（见 `_ref_conn`）。
    """
    keys: list = []
    for raw in keys_of(value):
        k = str(raw or "").strip()
        if k and k not in keys:            # 去重但保持顺序（库里有重复的历史值）
            keys.append(k)
    out = {"items": [], "text": "", "lines": [], "block": "",
           "total": len(keys), "live": 0, "dead": 0, "summary": "", "error": ""}
    if not keys:
        return out
    try:
        conn_, own = _ref_conn(conn)
        try:
            # 两趟取数，**都是批量的**（别在渲染循环里逐条查库）：
            #   ① 解析每个 key（resolve_key 读的是进程内缓存，不查库）；
            #   ② 一次把"输入 key + 合并链上的保留项"全查出来 —— 少了这一下，
            #      已合并的引用就只能显示成"保留项也不在库里"（第一版实测踩到）。
            plan = {k: _ref_resolve(conn_, k) for k in keys}
            rows = _ref_rows(conn_, keys)
            extra = [r for (r, _s, _t) in plan.values() if r and r not in rows]
            if extra:
                rows.update(_ref_rows(conn_, extra))
            # ③ 墓碑留名（T0-13）：只有解析结果是 None（= 这条已不在库里）的 key 才要，
            #    正常引用一个字都不多查（面板是 Tk 主线程，少一次查询就少一帧）。
            tombs = _ref_tombstones(conn_, [k for k in keys if plan[k][0] is None])
            snaps: dict = {}
            out["items"] = [_ref_one(k, plan[k], rows, snaps, tombs, limit)
                            for k in keys]
        finally:
            if own:
                conn_.close()
    except Exception as exc:      # noqa: BLE001 —— 读不到库要报出来，不是显示成空
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["items"] = [{"key": k, "status": REF_ERROR, "resolved": "",
                         "title": "", "first_author": "", "year": "",
                         "text": f"（读不到知识库：{out['error']}）  [{k}]"}
                        for k in keys]

    out["lines"] = [it["text"] for it in out["items"]]
    out["text"] = "；".join(out["lines"])
    out["block"] = "".join("\n  · " + t for t in out["lines"])
    if out["error"]:
        out["summary"] = f"读不到知识库：{out['error']}"
        return out
    out["dead"] = sum(1 for it in out["items"]
                      if it["status"] in (REF_DELETED, REF_MISSING)
                      or (it["status"] == REF_MERGED and not it["title"]))
    out["live"] = len(out["items"]) - out["dead"]
    if out["dead"]:
        out["summary"] = (f"关联的 {out['total']} 篇都已不在库里 —— 只标注，不自动清空"
                          if not out["live"] else
                          f"关联的 {out['total']} 篇里有 {out['dead']} 篇已不在库里")
    return out


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

    def read_all(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchall()

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

    # ---- id 连续编号：删掉中间一条之后，编号要从 1 开始重新连续
    for i in range(3):
        add_experience(w, asked=f"自检：编号 {i}", outcome="unknown",
                       tags="自检")
    # 手工造一个空号（模拟老库：那时删除不重排）
    delete_experience(w, 2, renumber_ids=False)
    before = all_ids(w)
    res = renumber(w)
    after = all_ids(w)
    ok &= before == [1, 3] and after == [1, 2] and res["moved"] == 2
    print(f"  重排编号：{before} → {after}（moved={res['moved']}）")

    # 批量删：一次删两条，剩下的编号仍然连续
    # （循环调用 delete_experience 会边删边重排，第二条就删到别的行上了）
    for i in range(2):
        add_experience(w, asked=f"自检：批量 {i}", outcome="unknown",
                       tags="自检")
    ids_before = all_ids(w)                      # [1, 2, 3, 4]
    gone = delete_experiences(w, [2, 4])
    ok &= sorted(gone) == [2, 4] and all_ids(w) == [1, 2]
    print(f"  批量删 {sorted(gone)}（原 {ids_before}）后：{all_ids(w)}")

    delete_experiences(w, all_ids(w))
    ok &= all_ids(w) == []
    print(f"  清空后：{all_ids(w)}")
    print("  自检结果：", "通过" if ok else "**不通过**")
    conn.close()
    return 0 if ok else 1


# ---------------------------------------------------------------- 起草一条经验
#
# ⚠ 这个函数**原来在 offline/kbchat.py 里**（那是"Zotero 内容窗格聊天助手"的
#   模块）。2026-10-05 用户要求把窗格那条链整个删掉，于是它被搬到这里 ——
#   它跟聊天没关系，只是"把用户的大白话整理成一条经验草稿"，面板「经验库 →
#   新建经验」的草稿按钮（端点 `/exp-draft`）一直在用它。
#
# 为什么 import 写在函数里：`judge` / `prompts` / `learn` 都在 offline 下，
# 模块级 import 会绕出环（learn → experience 也成立）。懒加载最省事。


def draft_experience(text: str, ref_id: int = 0, model: str = "",
                     conn=None) -> dict:
    """把用户的大白话整理成一条经验草稿（**不落库**）。

    另外给出：相似的已有经验（用户点"其实是改这条"）、提到的文献候选
    （**显示标题**，因为关联错文献＝权重加到别的论文上）。
    """
    import judge
    import prompts as PR

    res = judge.generate(PR.render("draft", text=text),
                         system=PR.get("draft", "system"), model=model,
                         json_mode=True, temperature=0.1, timeout=240.0)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "模型调用失败"}
    good, data = judge.parse_json(res.get("text") or "")
    if not good or not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON",
                "raw": str(res.get("text"))[:300]}
    draft = {
        "asked": str(data.get("asked") or "")[:400],
        "outcome": str(data.get("outcome") or "unknown").strip().lower(),
        "method": str(data.get("method") or "")[:300],
        "context": str(data.get("context") or "")[:300],
        "reason": str(data.get("reason") or "")[:600],
        "evidence": str(data.get("evidence") or "")[:200],
        "tags": split_list(data.get("tags")),
        "item_keys": split_list(data.get("item_keys")),
        "similar_hint": str(data.get("similar_hint") or "")[:200],
    }
    if draft["outcome"] not in OUTCOMES:
        draft["outcome"] = "unknown"
    similar, items = [], []
    if conn is not None:
        try:
            for row in conn.execute(
                    "SELECT id, asked, outcome, item_keys FROM experience "
                    "ORDER BY id DESC LIMIT 80"):
                if str(row["asked"])[:12] and str(row["asked"])[:12] in text:
                    similar.append({"id": row["id"], "asked": str(row["asked"])[:80],
                                    "outcome": row["outcome"]})
        except Exception:      # noqa: BLE001
            pass
        try:
            import learn                      # 复用已有的"文本→文献 key"匹配
            items = learn.match_items(text, conn)
        except Exception:      # noqa: BLE001
            items = []
    titles = []
    if conn is not None and (draft["item_keys"] or items):
        keys = list(dict.fromkeys(draft["item_keys"] + items))
        for k in keys:
            row = None
            try:
                row = conn.execute("SELECT title FROM items WHERE key=?",
                                   (k,)).fetchone()
            except Exception:      # noqa: BLE001
                row = None
            titles.append({"key": k, "title": (row["title"] if row else "")[:80]})
        draft["item_keys"] = [t["key"] for t in titles]
    return {"ok": True, "draft": draft, "similar": similar[:5],
            "items": titles, "ref_id": int(ref_id or 0),
            "model": res.get("model") or "",
            "note": "这是草稿。确认后才写库（写库时会重算权重）。"}


if __name__ == "__main__":
    import sys
    sys.exit(main())
