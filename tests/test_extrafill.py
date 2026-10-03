# ⚠ 与 tests/ 下其它测试一致：**只用合成的 extra 文本**（编造的键值与文件名），
#   不把本机任何一篇文献的标题、译名、基金、刊名、分类号写进这个文件。
#   原因见 test_metafill.py 文件头：测试文件会被提交、被分享、被别的会话读到，
#   一旦写进真实值，它就成了"用户研究领域的清单"。
#
#   所以这里的分工是：
#     · 解析/归一化正确性 → 合成 extra（可复现、不涉密、换台机器也能跑）
#     · 端到端接通       → 只跑本机库，**只断言格式与不变量**，不写死任何真实值

"""extrafill 的自检：extra 解析、字段归一化、写库与清理。

分四层：
  [A] 解析：合成 extra 文本 → 键值、未解析行、重复键。
  [B] 归一化：CLC/dbcode/Status/影响因子 → 提纯列；不猜的必须真的不猜。
  [C] 写库：**临时库**上建表、写行、幂等、清空删行、外键级联删除。
  [D] 展示口径：describe / compact / fields_of（检索命中、kb_item、tldr 共用）。
  [E] 本机库冒烟：只读地跑一遍 --scan，只查格式与不变量。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import extrafill as X  # noqa: E402
import schemas as S    # noqa: E402

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


class _FakeItem:
    """只实现 extrafill 用到的那两个属性：key 与 fields。"""

    def __init__(self, key="FAKEKEY1", extra=""):
        self.key = key
        self.fields = {"extra": extra}


# 合成的 extra 块：形态照着真实库的来（键名是真的知网字段名，
# **值全是编的**）。行序刻意打乱，防止实现偷偷依赖"某个键必须排在某处"。
_SYNTH_EXTRA = "\n".join([
    "titleTranslation: 一种合成测试方法",
    "download: 987654",
    "album: 合成专辑甲;合成专辑乙",
    "CLC: A12;B3.4",
    "dbcode: CMFD",
    "dbname: CMFD202501",
    "filename: TEST202301001",
    "CIF: 1.234",
    "AIF: 0.567",
    "publicationTag: 北大核心, EI",
    "ISSN: 1234-5678",
    "Status: advance online publication",
    "foundation: 某某基金(123456)；",
    "major: 测试工程",
    "original-container-title: Journal of Synthetic Testing",
    "CNKICite: (",
])


# ---------------------------------------------------------------- [A] 解析

print("[A] extra 解析（合成文本，不碰 Zotero 也不碰数据库）")

_rec = X.parse_extra(_SYNTH_EXTRA, key="FAKEKEY1")
check("解析 / 键的数量对得上", len(_rec.fields) == 16, str(len(_rec.fields)))
check("解析 / 值原样保留（不 trim 内部的空格）",
      _rec.fields["publicationTag"] == "北大核心, EI",
      repr(_rec.fields.get("publicationTag")))
check("解析 / 带连字符的键名认得出来",
      _rec.fields.get("original-container-title") == "Journal of Synthetic Testing",
      repr(_rec.fields.get("original-container-title")))
check("解析 / 没有未解析行（合成块每行都是 键: 值）",
      _rec.unparsed == [], str(_rec.unparsed))
check("解析 / 没有重复键", _rec.duplicates == [], str(_rec.duplicates))

# 全角冒号：用户手改 extra 时很常见，不认的话整行会被当成"备注"丢掉
check("解析 / 全角冒号也认",
      X.parse_extra("CLC：A12").fields == {"CLC": "A12"},
      str(X.parse_extra("CLC：A12").fields))
# 值里带冒号（基金名、URL）时只能按**第一个**冒号切
check("解析 / 值里的冒号不会被当成新键",
      X.parse_extra("foundation: 某某计划: 一期").fields
      == {"foundation": "某某计划: 一期"},
      str(X.parse_extra("foundation: 某某计划: 一期").fields))
# 不是 键:值 的行**不能**并进上一个键的值 —— 合并错了会把一段备注变成某个键的值
_note = X.parse_extra("CLC: A12\n这是一句手写备注，不是字段\nB3.4")
check("解析 / 非结构化行进 unparsed 且不污染上一个键",
      _note.fields == {"CLC": "A12"}
      and _note.unparsed == ["这是一句手写备注，不是字段", "B3.4"],
      f"fields={_note.fields} unparsed={_note.unparsed}")
# 中文键名不解析（ASCII 键的限制），但必须原样留在 unparsed 里而不是消失
check("解析 / 中文键名不猜，原样留在 unparsed",
      X.parse_extra("备注：这篇先看第三章").fields == {}
      and X.parse_extra("备注：这篇先看第三章").unparsed == ["备注：这篇先看第三章"],
      str(X.parse_extra("备注：这篇先看第三章")))
# 重复键：保留第一个，并记下来（外部工具写重了要让人看见）
_dup = X.parse_extra("download: 1\ndownload: 99")
check("解析 / 重复键保留第一个且记进 duplicates",
      _dup.fields == {"download": "1"} and _dup.duplicates == ["download"],
      f"fields={_dup.fields} duplicates={_dup.duplicates}")
check("解析 / 空值与 None 都不炸",
      X.parse_extra("").is_empty and X.parse_extra(None).fields == {},
      str(X.parse_extra(None)))
check("解析 / 空行被跳过",
      X.parse_extra("CLC: A12\n\n\nCLC2: B3").fields == {"CLC": "A12", "CLC2": "B3"},
      str(X.parse_extra("CLC: A12\n\n\nCLC2: B3").fields))
# 没有空格的写法（`CLC:A12`）也要认 —— 实测全部带空格，但不能只赌这一种
check("解析 / 冒号后没有空格也认",
      X.parse_extra("CLC:A12").fields == {"CLC": "A12"},
      str(X.parse_extra("CLC:A12").fields))


# ---------------------------------------------------------------- [B] 归一化

print("\n[B] 归一化与派生（不猜：确认不了的必须留空）")

_row = X.normalize(_rec)
# `normalize` 只产出"值列"，另外 4 列（原文、键值 JSON、key、时间）由写库那步补。
_side_cols = {"extra_raw", "fields_json", "item_key", "parsed_at"}
_diff = (set(_row) | _side_cols) ^ set(X._COLUMNS)
check("归一化 / 返回的键与表列一一对应（防列名漂移）",
      not _diff, f"对不上的列：{sorted(_diff)}")
check("归一化 / CLC 原样保留（不重排、不改分隔符）", _row["clc"] == "A12;B3.4",
      repr(_row["clc"]))
check("归一化 / 中图分类号拆成列表（半角分号）",
      X.clc_list("A12;B3.4") == ["A12", "B3.4"], str(X.clc_list("A12;B3.4")))
check("归一化 / 中图分类号列表认全角分号与逗号",
      X.clc_list("A12；B3.4，C5") == ["A12", "B3.4", "C5"],
      str(X.clc_list("A12；B3.4，C5")))
check("归一化 / 收录标签按逗号拆（且不留空项）",
      X.split_list("北大核心, EI", r"[,，]") == ["北大核心", "EI"],
      str(X.split_list("北大核心, EI", r"[,，]")))
check("归一化 / 以分号结尾的列表不留空串成员",
      X.split_list("某某基金(123)；", r"[;；]") == ["某某基金(123)"],
      str(X.split_list("某某基金(123)；", r"[;；]")))

# dbcode → 粗粒度类型：**只认实测交叉验证过的代码**
check("派生 / dbcode 映射（期刊/学位论文/会议）",
      X.dbcode_kind("CJFQ") == "journal" and X.dbcode_kind("capj") == "journal"
      and X.dbcode_kind("CMFD") == "thesis" and X.dbcode_kind("CDFD") == "thesis"
      and X.dbcode_kind("CPFD") == "conference",
      f"{X.dbcode_kind('CJFQ')}/{X.dbcode_kind('CMFD')}/{X.dbcode_kind('CPFD')}")
# 没见过的代码一律未知 —— 这是"不猜"的验收点：猜错的值用户看不出来，空值他会来查
check("派生 / 没见过的 dbcode 不猜（留空，不硬套一个类型）",
      X.dbcode_kind("ZZZZ") == "" and X.dbcode_kind("") == "",
      repr(X.dbcode_kind("ZZZZ")))
check("派生 / 合成块里的 CMFD 推出 thesis", _row["source_kind"] == "thesis",
      repr(_row["source_kind"]))

check("派生 / Status 的 advance online 标成网络首发",
      X.is_advance_online("advance online publication") is True
      and X.is_advance_online("网络首发") is True
      and X.is_advance_online("") is False,
      f"{X.is_advance_online('advance online publication')}/{X.is_advance_online('')}")
check("派生 / is_advance_online 落到列里是 0/1",
      _row["is_advance_online"] == 1, repr(_row["is_advance_online"]))

check("派生 / 影响因子转成浮点",
      _row["cif"] == 1.234 and _row["aif"] == 0.567,
      f"{_row['cif']}/{_row['aif']}")
# **返回 None 而不是 0.0**：0.0 会冒充一个"真实的低分"，参与排序时把这篇压下去
check("派生 / 影响因子转不动时返回 None（不是 0.0）",
      X.to_float("不是数字") is None and X.to_float("") is None
      and X.to_float(None) is None,
      repr(X.to_float("不是数字")))

# search_text 只拼白名单里的键：把 download（纯数字）和 CNKICite（坏数据 `(`）
# 拼进去会给检索灌噪声 —— 一个 `(` 谁都匹配得上。
# 用**整体相等**来断言，而不是 `"12" in text` 这种：后者会被别的字段里的数字
# 误伤（ISSN `1234-5678` 里就有 `12`），本文件第一版正是这么写错的。
_WANT_TEXT = ("A12;B3.4 合成专辑甲;合成专辑乙 北大核心, EI 测试工程 "
              "一种合成测试方法 Journal of Synthetic Testing 1234-5678")
check("派生 / 检索串只由白名单键按固定顺序拼成",
      _row["search_text"] == _WANT_TEXT, repr(_row["search_text"]))
check("派生 / 检索串不含未解释的键（download 的计数、CNKICite 的坏值）",
      "987654" not in _row["search_text"] and "(" not in _row["search_text"],
      repr(_row["search_text"]))

# extra 为空 → 不产生行（而不是产生一行全空的）
check("归一化 / extra 为空的条目不产出行",
      X.row_for_item(_FakeItem(extra="")) is None
      and X.row_for_item(_FakeItem(extra="   \n  ")) is None)
check("归一化 / 有 extra 时行里带原文与全部键值",
      (lambda g: g is not None and g[0]["item_key"] == "FAKEKEY1"
       and "dbcode" in g[0]["fields_json"])(X.row_for_item(_FakeItem(extra=_SYNTH_EXTRA))))


# ---------------------------------------------------------------- [C] 写库

print("\n[C] 写库（临时库；绝不碰真正的 index.db）")

_tmpdir = tempfile.mkdtemp(prefix="extrafill-test-")
_tmpdb = os.path.join(_tmpdir, "index.db")
try:
    conn = S.init_db(_tmpdb)
    X.ensure_schema(conn)
    X.ensure_schema(conn)          # 幂等：跑两次不能报 duplicate table
    check("写库 / ensure_schema 幂等", True)

    _cols = {r[1] for r in conn.execute("PRAGMA table_info(item_extra)")}
    # 这条是**防漂移**的：KEY_SPECS 里声明要提纯的列，必须真的存在于表里。
    # 少了列的话 sync 会报错；多了列（改了表没改规格）则说明两边已经开始漂移。
    _want = {spec.column for spec in X.KEY_SPECS.values() if spec.promoted}
    check("写库 / KEY_SPECS 里每个提纯列都真的在表里",
          _want <= _cols, f"缺={_want - _cols}")

    # 外键要求 items 里先有这一行（这也正是"表挂在 items 上"的体现）
    conn.execute("INSERT INTO items(key, item_type, title) VALUES(?,?,?)",
                 ("FAKEKEY1", "journalArticle", "合成标题"))
    conn.commit()

    _stats = X.sync_items(conn, [_FakeItem(extra=_SYNTH_EXTRA),
                                 _FakeItem(key="EMPTYKEY", extra="")])
    check("写库 / 返回统计（写了 1 行、扫了 2 篇）",
          _stats["rows"] == 1 and _stats["items"] == 2, str(_stats))
    check("写库 / 键覆盖统计对得上（CLC 计入 1 次）",
          _stats["keys"].get("CLC") == 1, str(_stats["keys"]))

    _got = X.facets_for_key(conn, "FAKEKEY1")
    check("写库 / 读回来的提纯列正确",
          _got.get("clc") == "A12;B3.4" and _got.get("source_kind") == "thesis"
          and _got.get("is_advance_online") == 1,
          str({k: _got.get(k) for k in ("clc", "source_kind", "is_advance_online")}))
    check("写库 / 原样 JSON 里连未解释的键也在",
          "foundation" in (_got.get("fields_json") or ""),
          str(_got.get("fields_json"))[:80])

    # 幂等：再同步一次不能变成两行
    X.sync_items(conn, [_FakeItem(extra=_SYNTH_EXTRA)])
    _n = conn.execute("SELECT COUNT(*) FROM item_extra").fetchone()[0]
    check("写库 / 重复同步不产生重复行", _n == 1, f"共 {_n} 行")

    # ---- 单条入口 `sync_item`：convert.py 的主循环逐条接线用它
    #
    # 两个默认值是**为了接线安全**才这么定的，所以要有断言盯着：
    #   · commit=False —— 事务边界留给调用方（主循环每轮自己 commit，
    #     这里再提交一次会把"这一条还没写完的其它表"提前提交）；
    #   · ensure=True —— 独立调用（CLI/测试）时表要能自建。
    conn.execute("DELETE FROM item_extra")
    conn.commit()
    _s1 = X.sync_item(conn, _FakeItem(extra=_SYNTH_EXTRA))
    check("写库 / sync_item 单条入口写入 1 行",
          _s1["rows"] == 1 and _s1["items"] == 1, str(_s1))
    _other = S.connect(_tmpdb)
    _seen = _other.execute("SELECT COUNT(*) FROM item_extra").fetchone()[0]
    _other.close()
    check("写库 / sync_item 默认不提交（事务边界留给调用方）",
          _seen == 0, f"另一个连接看到了 {_seen} 行")
    conn.commit()

    # extra 被清空 → 必须**主动删行**：只 INSERT OR REPLACE 的话会留下陈旧行，
    # 检索侧会一直以为这篇有分类号
    _stats2 = X.sync_items(conn, [_FakeItem(extra="")])
    _n2 = conn.execute("SELECT COUNT(*) FROM item_extra").fetchone()[0]
    check("写库 / extra 变空时旧行被删掉",
          _n2 == 0 and _stats2["cleared"] == 1, f"剩 {_n2} 行，{_stats2}")

    # 外键级联：条目被 convert.py 的 _purge_item 删掉时，
    # item_extra 的行应当自动消失（schemas.connect 开了 foreign_keys=ON）。
    # 这条断言很重要 —— 它决定了"接线时要不要再改 convert.py 去清理"。
    X.sync_items(conn, [_FakeItem(extra=_SYNTH_EXTRA)])
    conn.execute("DELETE FROM items WHERE key = ?", ("FAKEKEY1",))
    conn.commit()
    _n3 = conn.execute("SELECT COUNT(*) FROM item_extra").fetchone()[0]
    check("写库 / 条目被删时结构化行随外键级联删除",
          _n3 == 0, f"剩 {_n3} 行")

    # 表还没建过时读不能炸（可能是"还没跑过 --sync"）
    _bare = S.init_db(os.path.join(_tmpdir, "bare.db"))
    check("写库 / 没跑过 sync 时 facets_for_key 返回空而不抛",
          X.facets_for_key(_bare, "NOPE") == {}, "抛异常了")
    _bare.close()
    conn.close()
finally:
    shutil.rmtree(_tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- [D] 展示口径

print("\n[D] 展示口径（describe）——检索命中 / kb_item / tldr 共用这一份")

_d_row = {
    "clc": "A12;B3.4", "source_kind": "thesis",
    "publication_tag": "北大核心, EI", "title_translation": "一种合成测试方法",
    "major": "测试工程", "orig_container_title": "Journal of Synthetic Testing",
    "album": "合成专辑甲;合成专辑乙", "issn": "1234-5678",
    "cif": 1.234, "aif": 0.567, "is_advance_online": 1,
    "extra_raw": "（原文）", "fields_json": "{}", "search_text": "（检索串）",
    "dbcode": "CMFD", "dbname": "CMFD202501", "filename": "TEST202301001",
}
_d_full = X.describe(_d_row)
_d_compact = X.describe(_d_row, compact=True)

check("展示 / 分类号拆成列表（不是一整串）",
      _d_full.get("中图分类号") == ["A12", "B3.4"], str(_d_full.get("中图分类号")))
check("展示 / 来源类型用 KIND_LABELS 的中文名",
      _d_full.get("来源类型") == "学位论文", str(_d_full.get("来源类型")))
check("展示 / 收录标签拆成列表",
      _d_full.get("收录标签") == ["北大核心", "EI"], str(_d_full.get("收录标签")))
# 影响因子是**期刊级**指标：标签上必须写明，否则会被当成本文指标比较
check("展示 / 影响因子的标签写明「期刊级」",
      any("期刊级" in k for k in _d_full)
      and _d_full.get("期刊复合影响因子（期刊级，不是本文指标）") == 1.234,
      str([k for k in _d_full if "影响因子" in k]))
check("展示 / 网络首发有单独说明",
      "网络首发" in str(_d_full.get("出版状态", "")), str(_d_full.get("出版状态")))

# compact 是给**检索结果**用的：一次 8 条，每条都塞十几个字段会把上下文吃光
check("展示 / compact 只留判断相关性要用的那几项",
      set(_d_compact) == {"中图分类号", "来源类型", "收录标签", "中文译名", "学位专业"},
      str(sorted(_d_compact)))
check("展示 / compact 不含影响因子与内部凭据",
      not any(("影响因子" in k or "刊名" in k or "专辑" in k) for k in _d_compact),
      str(sorted(_d_compact)))

check("展示 / 空行/空值不产出一堆空字段",
      X.describe({}) == {} and X.describe({"clc": "", "major": ""}) == {},
      str(X.describe({"clc": "", "major": ""})))
# 用户拍板过：download 只原样留存、不参与**任何**判断 ——
# 它和坏掉的 CNKICite、非结构化的 foundation 都不该混进展示口径
check("展示 / 只原样留存的键不进展示口径",
      not any(("下载" in k or "CNKI" in k or "基金" in k) for k in _d_full),
      str(sorted(_d_full)))

# 防漂移：DBCODE_KINDS 里出现一个新取值时，必须同时给它一个中文名
# （否则界面会直接显示英文码，用户看不懂）
check("展示 / KIND_LABELS 覆盖 DBCODE_KINDS 的每个取值",
      set(X.DBCODE_KINDS.values()) <= set(X.KIND_LABELS),
      str(set(X.DBCODE_KINDS.values()) - set(X.KIND_LABELS)))
check("展示 / fields_of 把 fields_json 还原成字典（坏 JSON 返回空而不抛）",
      X.fields_of({"fields_json": '{"CLC": "A12"}'}) == {"CLC": "A12"}
      and X.fields_of({"fields_json": "不是 JSON"}) == {},
      str(X.fields_of({"fields_json": "不是 JSON"})))


# ---------------------------------------------------------------- [E] 本机库冒烟

print("\n[E] 本机库冒烟（只读，只查格式与不变量）")
try:
    import zreader

    _reader = zreader.ZoteroReader()
    _items = _reader.load_items()
    _reader.close()
    _scan = X.scan_keys(_items)
except Exception as exc:  # noqa: BLE001
    _scan = None
    print(f"  （跳过：本机 Zotero 库不可用 —— {type(exc).__name__}: {exc}）")

if _scan:
    check("冒烟 / 扫到了条目", _scan["total_items"] > 0, str(_scan["total_items"]))
    check("冒烟 / 有 extra 非空的条目", _scan["extra_nonempty"] > 0,
          str(_scan["extra_nonempty"]))
    check("冒烟 / 没有解析不了的行（实测形态就是每行 键: 值）",
          sum(_scan["unparsed"].values()) == 0, str(_scan["unparsed"]))
    check("冒烟 / 没有含重复键的块",
          _scan["blocks_with_duplicate_keys"] == 0,
          str(_scan["blocks_with_duplicate_keys"]))
    # ⚠ 这条原来断言 `CLC 篇数 == dbcode 篇数`。它**太强了** —— 那只是当时的
    #   经验观察，不是不变量：真机遇到过（用户在 Zotero 里合并重复条目），
    #   合并会保留**主条目的 extra**，被合并那篇的 CLC 行就没了，
    #   于是变成 CLC 38 / dbcode 39。数据没问题，是断言把巧合当了规律。
    #
    #   现在断言的是**真正该成立**的那部分：
    #     · 两者都读到了（防"解析器整个失效、全是 0"这种回归）
    #     · 有 CLC 的必定也有 dbcode（知网条目两个一起写）
    #     · CLC 不会塌掉（允许合并造成的少量缺失，但不允许断崖式下跌）
    _cov = _scan["coverage"]
    _clc, _db = _cov.get("CLC", 0), _cov.get("dbcode", 0)
    check("冒烟 / CLC 与 dbcode 都读到了（防解析器整体失效）",
          _clc > 0 and _db > 0, f"CLC={_clc} dbcode={_db}")
    check("冒烟 / CLC 篇数不超过 dbcode（两个都是知网一起写的）",
          _clc <= _db, f"CLC={_clc} dbcode={_db}")
    check("冒烟 / CLC 覆盖率没有塌掉（合并允许少量缺失，不允许断崖）",
          _clc >= _db * 0.8, f"CLC={_clc} dbcode={_db}")
    # 未登记的键只提示不判失败：换一台机器、换一批文献时它可能真的多出一个键，
    # 那时该做的是"去确认含义再登记"，不是让测试变红拦住别人。
    if _scan["unexplained_keys"]:
        print(f"  ⚠ 出现了模块里没登记的键，需要人工确认含义后登记："
              f"{'、'.join(_scan['unexplained_keys'])}")
    else:
        print("  （本机库里出现的 extra 键都已在 KEY_SPECS 里登记）")
    print(f"  （本机：{_scan['total_items']} 篇，extra 非空 "
          f"{_scan['extra_nonempty']} 篇，CLC "
          f"{_scan['coverage'].get('CLC', 0)} 篇 —— 具体值不打印，"
          f"extra 里含用户研究内容）")

print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
