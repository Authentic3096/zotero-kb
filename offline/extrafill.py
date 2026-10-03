"""把 Zotero 条目的 `extra` 字段里**已经存在的结构化数据**用起来。

    python offline/extrafill.py --scan          # 只读：盘点 extra 里到底有哪些键
    python offline/extrafill.py --sync          # 解析并写入知识库的 item_extra 表
    python offline/extrafill.py <KEY>           # 看某一篇解析出来什么
    python offline/extrafill.py --sync --json   # 结构化输出（供上层接线/排障）

## 为什么要有这个模块

另一个子代理实测发现：库里有 **39 篇**条目的 `extra` 里带着**知网（CNKI）
写入的结构化字段**（`CLC` 中图分类号、`dbcode` 来源库、`filename`、
`publicationTag` 收录标签…）。这些是**现成的、免费的、结构化的**数据，
比让模型去猜靠谱得多，但此前**完全没被用起来**。

用户原话："结构化字段能直接用就直接用。"

## 实测到的 extra 形态（全库 101 篇、extra 非空 76 篇）

    · 每行都是 `键: 值`，分隔符恒为**半角冒号 + 一个空格**（实测 417/417 行）；
    · **没有多行值**（417 个冒号行 = 417 个非空行，没有"续行"）；
    · **没有重复键**（76 个块里 0 个块出现同名键）；
    · 键名是 ASCII（`CLC` / `original-container-title` / `publicationTag` …），
      大小写混用；值可以是中文、英文、数字、分号列表。
    · 行数分布：1~3 行的多是纯手工备注，7~13 行的一看就是机器写入的
      （知网导出），所以解析要能容忍"有些行压根不是结构化字段"。

## 哪些键"能用"（只有确认过形态与含义的才处理）

判据是**可验证的**，不是"看着像"。

⚠ 下表里的值都是**合成示例** —— 形态照着实测来，值本身是编的。
`extra` 里的中文译名、刊名、基金就是用户的研究内容，不该出现在代码里。

| 键 | 篇数 | 形态 | 怎么确认的 | 处理 |
|----|------|------|------------|------|
| `CLC` | 39 | `A12;B3.4`（分号分隔的中图分类号） | 中图法分类号是公开的编码体系，值形态与其完全一致 | ✅ 分类信号 |
| `dbcode` | 39 | `CJFQ`/`CAPJ`/`CMFD`/`CDFD`/`CPFD` | **与 Zotero 的 item_type 交叉验证**：CJFQ/CAPJ→期刊、CMFD/CDFD→学位论文、CPFD→会议，39 篇无例外 | ✅ 来源库类型 |
| `dbname` | 38 | `CJFDLAST2020`、`CMFD202001` | 形态是"库前缀+期号" | ⚠ 只存原值当**来源凭据**；见下方陷阱 |
| `filename` | 38 | `TEST202301001` / `1200000000.nh` | 知网下载文件名，两种形态 | ✅ 回溯 |
| `album` | 38 | `合成专辑甲;合成专辑乙`（分号分隔） | 知网的学科专辑名，公开体系 | ✅ 分类信号 |
| `titleTranslation` | 29 | 中文题名 | 键名自解释，值是中文标题 | ✅ 中文检索用 |
| `original-container-title` | 28 | 英文刊名 | 键名自解释 | ✅ 补 venue |
| `CIF` / `AIF` | 25 | `1.234` 这种小数 | 形态是浮点；实测 25/25 满足 `CIF >= AIF`（与"复合影响因子 ≥ 综合影响因子"一致） | ⚠ **期刊级**指标，只作参考 |
| `publicationTag` | 24 | `北大核心, EI, CSCD` | 公开的收录体系名称，逗号分隔 | ✅ 刊物等级信号 |
| `ISSN` | 5 | `1234-5678` | 标准号形态 | ✅ 期刊标识 |
| `major` | 9 | 学科名 | 学位论文的专业，键名自解释 | ✅ 分类信号 |
| `Status` | 4 | 恒为 `advance online publication` | 值唯一且语义明确 | ✅ 网络首发标记 |
| `download` | 38 | 整数 | 形态确认是整数，但**"下载次数"这个含义无法从数据确认** | ⚠ 只存原值，不解释 |
| `foundation` | 18 | 长自由文本（基金名+编号） | 无固定结构 | ⚠ 只存原值，不做结构化 |
| `CNKICite` | 20 | **坏数据**：20 条里 15 条的值就是一个 `(` | 明显被写坏了（原意可能是 `(6)`） | ❌ 不处理，且要在扫描里报出来 |

### 陷阱：`dbname` 里的年份**不是**发表年份

`CJFDLAST2023` 里的 `2023` 是**知网那个库版本的年份**（"LAST" = 最后一期），
不是这篇文献的发表年。拿它当 `year` 用会给文献算错基础权重
（`schemas.recency_base`），而且错得很隐蔽。所以本模块**不解析** dbname 的年份。

## 落到知识库的形式：新建 `item_extra` 表（不是给 items 加列）

三个理由，按重要性排：

1. **`items` 的写入点不在这里**。它的 INSERT 在 `offline/convert.py` 的
   `_upsert_item` 里；本模块不碰那个文件。若改成"给 items 加列"，
   convert.py 不写那一列，结果就是**永远为 NULL 的列** —— 比不加更糟
   （读的人会以为"这些文献没有结构化字段"）。
2. **稀疏**。101 篇里 76 篇有 extra、只有 39 篇有知网结构化字段。
   塞进主表会让 items 多出一堆大部分为空的行宽，而检索主链路的查询
   每次都要跨过它们。
3. **可整表重算**。结构化字段是"解析产物"，将来发现某个键解析错了，
   `DROP TABLE item_extra` 重跑即可，`items` / `chunks` / 向量一律不受影响。
   —— 项目里已有同样的先例：`check_chunks.py` 自建 `item_health` 表。

表里**同时**放两份东西，这是刻意的：

    · `extra_raw` + `fields_json` —— 原文与**全部**键值（包括没解释的
      `download` / `foundation` / 坏掉的 `CNKICite`）。将来发现某个键有用，
      直接重解析就行，不必回去读 Zotero；
    · 提纯列（`clc` / `publication_tag` / `source_kind` …）—— 让检索侧能直接
      SQL 过滤与加权，不必在 Python 里再解析一遍。

另外存一列 `search_text`：把分类号 / 专辑 / 收录标签 / 专业 / 中文译名 /
原刊名拼成一个字符串。为什么要存派生值：在线侧要按它给召回加权时，
现成的一列 `LIKE` 或拼接比每次现算便宜，而且**口径统一**（在哪拼、拼什么，
只有这一处说了算）。

## 已知边界

- 只读 Zotero（走 `zreader`，它本来就只读快照），只写知识库的 `item_extra`。
- `extra` 被清空 / 条目被删除时旧行要消失：条目删除由
  `FOREIGN KEY ... ON DELETE CASCADE` 兜住（`schemas.connect` 开了
  `PRAGMA foreign_keys=ON`）；extra 被清空由本模块的 `sync_items` 主动 DELETE。
- 值里出现的真实标题/作者等**只进数据库，不进代码**：本文件的示例全是合成的。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402

# ---------------------------------------------------------------- 键 → 含义

# 键名的书写形态。实测 417 行**全部**符合这个形态（ASCII 键 + 半角冒号 + 空格）。
#
# 为什么把键限制成 ASCII：`extra` 里也可能有用户手写的中文备注
# （比如 `备注：这篇要先看第三章`）。那种行**不是**结构化字段，
# 硬解析会把备注变成一个莫名其妙的键。所以不认识的行进 `unparsed` 原样留着，
# 而不是猜。
RE_EXTRA_LINE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_ \-/]{0,30}?)\s*[:：]\s*(.*)$")

# 值里的分号既可能是半角 `;` 也可能是全角 `；`（实测 `album` 用半角、
# `foundation` 用全角）。分隔列表时两种都要认。
RE_LIST_SPLIT = re.compile(r"[;；]")


@dataclass(frozen=True)
class KeySpec:
    """一个 extra 键的处理规格。

    `promoted=True` 表示"含义已经确认、值得提纯成列"；
    `promoted=False` 表示"只原样留存、不解释" —— 后者不是"没用"，
    而是**确认不了含义就不装作确认了**（用户明确要求：拿不准的不要猜）。
    """

    label: str          # 中文名，给界面/日志用
    promoted: bool      # 是否提纯成独立列
    column: str = ""    # promoted=True 时的列名
    kind: str = "text"  # text | list | number | flag
    split: str = ""     # list 型的分隔符（正则）
    why: str = ""       # 为什么这么判 —— 写清"怎么确认的"


KEY_SPECS: dict[str, KeySpec] = {
    "CLC": KeySpec(
        label="中图分类号", promoted=True, column="clc", kind="list",
        split=r"[;；]",
        why="中图法分类号是公开编码体系，值形态（字母+数字，可带小数点）与之一致"),
    "dbcode": KeySpec(
        label="知网来源库代码", promoted=True, column="dbcode",
        why="与 Zotero item_type 交叉验证过：期刊/学位论文/会议三类 39 篇无例外"),
    "dbname": KeySpec(
        label="知网来源库+期号", promoted=True, column="dbname",
        why="形态固定（库前缀+期号）；⚠ 里面的年份是库版本年份，不是发表年份"),
    "filename": KeySpec(
        label="知网下载文件名", promoted=True, column="filename",
        why="两种形态（字母+数字、纯数字.nh），用于回溯原始下载件"),
    "album": KeySpec(
        label="知网学科专辑", promoted=True, column="album", kind="list",
        split=r"[;；]",
        why="知网的专辑分类是公开体系，值是分号分隔的专辑名"),
    "publicationTag": KeySpec(
        label="收录标签", promoted=True, column="publication_tag", kind="list",
        split=r"[,，]",
        why="北大核心/CSCD/EI/JST 这类收录体系名是公开的，逗号分隔"),
    "ISSN": KeySpec(
        label="期刊 ISSN", promoted=True, column="issn",
        why="标准号形态（四位-四位）"),
    "major": KeySpec(
        label="学位专业", promoted=True, column="major",
        why="键名自解释，出现在学位论文上"),
    "Status": KeySpec(
        label="出版状态", promoted=True, column="status",
        why="实测取值唯一（网络首发标记），语义明确"),
    "original-container-title": KeySpec(
        label="原文刊名", promoted=True, column="orig_container_title",
        why="键名自解释，值是英文刊名"),
    "titleTranslation": KeySpec(
        label="中文译名", promoted=True, column="title_translation",
        why="键名自解释，值是中文标题；英文文献靠它能被中文查询命中"),
    "CIF": KeySpec(
        label="期刊复合影响因子", promoted=True, column="cif", kind="number",
        why="浮点形态；实测 25/25 满足 CIF>=AIF，与知网两个口径的影响因子一致。"
            "⚠ 这是**期刊级**指标，同一刊所有文章相同，不能当文献级质量信号"),
    "AIF": KeySpec(
        label="期刊综合影响因子", promoted=True, column="aif", kind="number",
        why="同上"),
    # ---- 以下只原样留存，不提纯（含义没能确认，不猜）
    "download": KeySpec(
        label="下载计数（含义未确认）", promoted=False,
        why="值形态确定是整数，但'下载次数'这个含义无法从本库数据确认 —— "
            "不做任何基于它的判断，只留在 fields_json 里"),
    "foundation": KeySpec(
        label="基金项目（自由文本）", promoted=False,
        why="长自由文本、无固定结构，结构化不了；原样留存供将来做全文检索"),
    "CNKICite": KeySpec(
        label="知网引用信息（数据已损坏）", promoted=False,
        why="实测 20 条里 15 条的值就是一个 '(' —— 写入时被截断了。"
            "不处理，并在 --scan 里报出来（源数据的问题，不该由解析器假装没看见）"),
}


# 知网来源库代码 → 粗粒度文献类型。**只写实测见过的代码**。
#
# 为什么不把"未见过"的代码也猜一个：这张表是"确认过的对应关系"，
# 不是"知网代码大全"。没见过的代码一律 source_kind='' （未知），
# 原代码照旧存在 `dbcode` 列里 —— 用户看到空值会来查，看到猜错的值不会。
#
# ⚠ 也被刻意压到"期刊/学位论文/会议"三档：知网代码的常见读法里
#   CMFD/CDFD 还分硕士/博士，但本库只有 9 篇学位论文，
#   **分不出**硕士博士这一层（交叉验证只能确认"是学位论文"）。
#   所以不写更细的档 —— 写了就是猜。
DBCODE_KINDS = {
    "CJFQ": "journal",        # 期刊（实测 26 篇，item_type 全是 journalArticle）
    "CAPJ": "journal",        # 期刊（实测 3 篇，同上）
    "CMFD": "thesis",         # 学位论文（实测 8 篇，item_type 全是 thesis）
    "CDFD": "thesis",         # 学位论文（实测 1 篇，同上）
    "CPFD": "conference",     # 会议论文（实测 1 篇，item_type=conferencePaper）
}

# `source_kind` 的中文名。放在**本模块**而不是界面层：它的取值集合由上面的
# DBCODE_KINDS 决定，中文名跟它放在一起才不会漂移 —— "模块里多了一个取值、
# 界面那边还停在老映射"是这类映射表最常见的坏法。
KIND_LABELS = {
    "journal": "期刊论文",
    "thesis": "学位论文",
    "conference": "会议论文",
}

# "网络首发"的判据。和 `metafill` 的 DATE_KIND_ONLINE_FIRST 是**同一件事的
# 两个数据源**：那边从 PDF 正文的日期标签看出来，这边从 extra 的结构化字段看出来。
# 两边都指向同一篇时，可以更确信"这篇只有网络首发、没有正式卷期"。
ADVANCE_ONLINE_MARKERS = (
    "advance online", "online first", "published online", "available online",
    "网络首发", "在线发表", "优先出版", "预出版",
)


# ---------------------------------------------------------------- 解析


@dataclass
class ExtraRecord:
    """一篇文献 `extra` 的解析结果（纯数据，不碰数据库）。"""

    key: str = ""
    raw: str = ""                              # extra 原文
    fields: dict[str, str] = field(default_factory=dict)   # 全部键值（原样）
    unparsed: list[str] = field(default_factory=list)      # 不是 KEY:value 的行
    duplicates: list[str] = field(default_factory=list)    # 重复出现的键

    @property
    def is_empty(self) -> bool:
        return not self.raw.strip()


def parse_extra(text: str, key: str = "") -> ExtraRecord:
    """把 `extra` 原文解析成 {键: 值} + 没解析出来的行。

    三条实测形态决定了实现（见模块头）：
      · 分隔符恒为半角冒号+空格 → 但全角冒号也认（用户手改过几篇）；
      · 没有多行值 → 所以**不做续行合并**。不匹配的行进 `unparsed`，
        不并进上一个键 —— 合并错了会把一段备注变成某个键的值，比不解析更糟；
      · 没有重复键 → 真出现重复时**保留第一个**并记进 `duplicates`。
        为什么留第一个而不是最后一个：`extra` 是**外部工具写进去的**，
        重复说明写入方出了异常，此时第一个通常是原始值、后面那个可能是重试残留。
        不管选哪个，都要让用户看得见（`duplicates` 会出现在 CLI 输出里）。
    """
    rec = ExtraRecord(key=key, raw=text or "")
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = RE_EXTRA_LINE.match(line)
        if not m:
            rec.unparsed.append(line)
            continue
        name, value = m.group(1).strip(), m.group(2).strip()
        if name in rec.fields:
            rec.duplicates.append(name)
            continue
        rec.fields[name] = value
    return rec


def split_list(value: str, pattern: str = r"[;；]") -> list[str]:
    """把 `a;b` / `a；b` 这种值拆成列表，去掉空项与空白。

    为什么去空项：实测有值以分号结尾（`国家自然科学基金(123)；`），
    不去空项会得到一个空字符串成员，后面按它检索就会"什么都匹配上"。
    """
    if not value:
        return []
    return [p.strip() for p in re.split(pattern, value) if p.strip()]


def to_float(value: str) -> float | None:
    """转浮点；转不动返回 None（**不返回 0.0** —— 0 会冒充一个真实的低分）。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def dbcode_kind(dbcode: str) -> str:
    """知网代码 → 粗粒度类型；没见过的代码返回空串（未知，不猜）。"""
    return DBCODE_KINDS.get((dbcode or "").strip().upper(), "")


def is_advance_online(status: str) -> bool:
    """出版状态是不是"网络首发"（只有它时，这篇没有正式卷期页码）。"""
    low = (status or "").strip().lower()
    return any(mark in low for mark in ADVANCE_ONLINE_MARKERS)


def build_search_text(fields: dict[str, str]) -> str:
    """把"能当检索信号"的字段拼成一段文本（存进 `search_text` 列）。

    为什么只挑这几个、而不是把整个 extra 拼进去：`extra` 里还有 `download`
    这种纯数字和 `CNKICite` 这种坏数据，拼进去会往检索里灌噪声
    （一个 `(` 谁都匹配得上）。口径固定的白名单比"全都塞"可控。
    """
    parts: list[str] = []
    for name in ("CLC", "album", "publicationTag", "major",
                 "titleTranslation", "original-container-title", "ISSN"):
        val = (fields.get(name) or "").strip()
        if val:
            parts.append(val)
    return " ".join(parts)


def normalize(rec: ExtraRecord) -> dict:
    """把解析结果压成"要写进库的那一行"（纯函数，方便测试与复用）。

    返回的键与 `item_extra` 的列一一对应（`extra_raw` / `fields_json` 除外，
    那两个由 `row_for_item` 补 —— 它们需要 JSON 序列化，不属于"归一化"）。
    """
    f = rec.fields
    status = (f.get("Status") or "").strip()
    dbcode = (f.get("dbcode") or "").strip()
    return {
        "n_keys": len(f),
        "clc": (f.get("CLC") or "").strip(),
        "dbcode": dbcode,
        "dbname": (f.get("dbname") or "").strip(),
        "album": (f.get("album") or "").strip(),
        "publication_tag": (f.get("publicationTag") or "").strip(),
        "issn": (f.get("ISSN") or "").strip(),
        "major": (f.get("major") or "").strip(),
        "status": status,
        "filename": (f.get("filename") or "").strip(),
        "orig_container_title": (f.get("original-container-title") or "").strip(),
        "title_translation": (f.get("titleTranslation") or "").strip(),
        "cif": to_float(f.get("CIF") or ""),
        "aif": to_float(f.get("AIF") or ""),
        # 两个派生列：派生逻辑放在这里，是为了让"从原始值到结论"只有一条路径
        # （检索侧读到的 source_kind 一定和本模块的映射表一致）。
        "source_kind": dbcode_kind(dbcode),
        "is_advance_online": 1 if is_advance_online(status) else 0,
        "search_text": build_search_text(f),
    }


def row_for_item(item) -> tuple[dict, ExtraRecord] | None:
    """从一个已加载的 Item 生成待写库的行；extra 为空时返回 None。

    ⚠ 从 `item.fields["extra"]` 读，**不从索引库读** ——
    `items` 表里根本没有 extra 列（这也正是本模块存在的原因）。
    """
    raw = str((getattr(item, "fields", None) or {}).get("extra") or "")
    if not raw.strip():
        return None
    rec = parse_extra(raw, key=str(getattr(item, "key", "") or ""))
    row = normalize(rec)
    row["item_key"] = rec.key
    row["extra_raw"] = rec.raw
    row["fields_json"] = json.dumps(rec.fields, ensure_ascii=False)
    return row, rec


# ---------------------------------------------------------------- 写库
#
# DDL 显式写出来（不是从 KEY_SPECS 生成）：表结构是要被人读、被人迁移的，
# 生成出来的 DDL 一眼看不出有哪些列。代价是"表结构与 KEY_SPECS 可能漂移"，
# 所以 tests/test_extrafill.py 里有一条断言：每个 promoted 的 column
# 都必须真的存在于表里。

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS item_extra (
    item_key            TEXT PRIMARY KEY,
    extra_raw           TEXT,     -- extra 原文，一行不丢
    fields_json         TEXT,     -- 解析出的全部键值（JSON；含未解释的键）
    n_keys              INTEGER,  -- 解析出多少个键
    clc                 TEXT,     -- 中图分类号（分号分隔原样）
    dbcode              TEXT,     -- 知网来源库代码
    dbname              TEXT,     -- 知网来源库+期号（⚠ 年份不是发表年份）
    album               TEXT,     -- 知网学科专辑
    publication_tag     TEXT,     -- 收录标签（北大核心/EI/CSCD…）
    issn                TEXT,
    major               TEXT,     -- 学位论文专业
    status              TEXT,     -- 出版状态（advance online publication）
    filename            TEXT,     -- 知网下载文件名（回溯用）
    orig_container_title TEXT,    -- 原文刊名
    title_translation   TEXT,     -- 中文译名
    cif                 REAL,     -- 期刊复合影响因子（期刊级！）
    aif                 REAL,     -- 期刊综合影响因子（期刊级！）
    source_kind         TEXT,     -- 由 dbcode 推出的粗粒度类型：journal/thesis/conference
    is_advance_online   INTEGER,  -- 是否网络首发（0/1）
    search_text         TEXT,     -- 供检索侧加权的拼接串（口径见 build_search_text）
    parsed_at           TEXT,
    FOREIGN KEY (item_key) REFERENCES items(key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_item_extra_clc ON item_extra(clc);
CREATE INDEX IF NOT EXISTS idx_item_extra_dbcode ON item_extra(dbcode);
CREATE INDEX IF NOT EXISTS idx_item_extra_kind ON item_extra(source_kind);
"""

# 写库用的列顺序（与 SCHEMA_SQL 对齐；INSERT 语句由它拼出来）
_COLUMNS = (
    "item_key", "extra_raw", "fields_json", "n_keys", "clc", "dbcode", "dbname",
    "album", "publication_tag", "issn", "major", "status", "filename",
    "orig_container_title", "title_translation", "cif", "aif", "source_kind",
    "is_advance_online", "search_text", "parsed_at",
)


def ensure_schema(conn) -> None:
    """建表建索引。幂等，重复调用安全。

    为什么由本模块自己建、不写进 `schemas.SCHEMA_SQL`：
    本模块**必须能被单独调用**（convert.py 不在本次改动范围），
    如果表要等 `init_db` 才建，独立调用就会 "no such table"。
    项目里 `item_health` 也是这个模式（`check_chunks.py` 自建自用）。
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def sync_item(conn, item, commit: bool = False, ensure: bool = True) -> dict:
    """把**一条** Item 的 extra 结构化后写进 `item_extra`（返回统计）。

    为什么要这个"单条入口"、而不是让调用方自己写 `sync_items(conn, [item])`：
      · `convert.py` 的主循环手里本来就是"一条 item"，包一层列表只是噪音；
      · 更实际的是**表结构的建立时机**：逐条接线时表在第一篇就建好了，
        每条都重跑一遍 DDL（`executescript` 一次 + 三个 CREATE INDEX）
        是纯浪费。所以"这条要不要 ensure_schema"的决定收在这里：
        单条入口默认**不**建表，由调用方在循环外调一次 `ensure_schema`。

    `commit=False` 是给 convert.py 用的：它整个主循环按条 commit
    （`conn.commit()` 在每条末尾），这里再 commit 一次会把"这一条还没写完的
    其它表（figures/chunks）"提前提交，虽然不出错，但事务边界就乱了。
    """
    return sync_items(conn, [item], commit=commit, ensure=ensure)


def sync_items(conn, items, commit: bool = True, ensure: bool = True) -> dict:
    """把一批 Item 的 extra 结构化后写进 `item_extra`（返回统计）。

    先删后写、逐条覆盖 —— 和 convert.py 写 figures 用同一套模式。为什么：
    `extra` 是**用户/外部工具会改**的字段，可能被清空或删掉某个键；
    只做 INSERT OR REPLACE 的话，"extra 被清空"的条目会留下一条陈旧的行，
    而检索侧看到它就会一直以为这篇有分类号。所以 `extra` 为空时**主动删行**。

    `ensure=True`（默认）时先建表建索引 —— 独立调用（CLI / 测试）需要它；
    convert.py 逐条调用时传 False，由它在循环外建一次。
    """
    if ensure:
        ensure_schema(conn)
    import datetime

    parsed_at = datetime.datetime.now().astimezone().replace(
        microsecond=0).isoformat()
    stats = {"items": 0, "rows": 0, "cleared": 0, "unparsed": 0,
             "duplicates": 0, "keys": Counter()}
    placeholders = ",".join("?" * len(_COLUMNS))
    updates = ",".join(f"{c}=excluded.{c}" for c in _COLUMNS[1:])
    sql = (f"INSERT INTO item_extra({','.join(_COLUMNS)}) "
           f"VALUES({placeholders}) "
           f"ON CONFLICT(item_key) DO UPDATE SET {updates}")

    for item in items or []:
        key = str(getattr(item, "key", "") or "")
        if not key:
            continue
        stats["items"] += 1
        got = row_for_item(item)
        if got is None:
            cur = conn.execute("DELETE FROM item_extra WHERE item_key = ?", (key,))
            stats["cleared"] += cur.rowcount or 0
            continue
        row, rec = got
        row["parsed_at"] = parsed_at
        conn.execute(sql, tuple(row.get(c) for c in _COLUMNS))
        stats["rows"] += 1
        stats["unparsed"] += len(rec.unparsed)
        stats["duplicates"] += len(rec.duplicates)
        for name in rec.fields:
            stats["keys"][name] += 1
    if commit:
        conn.commit()
    stats["keys"] = dict(stats["keys"])
    return stats


def sync_all(conn=None, reader=None, commit: bool = True) -> dict:
    """扫全库并把 extra 结构化写库（CLI 的 `--sync` 走这条）。"""
    close_reader = False
    if reader is None:
        import zreader

        reader = zreader.ZoteroReader()
        close_reader = True
    close_conn = False
    if conn is None:
        conn = S.init_db()
        close_conn = True
    try:
        return sync_items(conn, reader.load_items(), commit=commit)
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if close_reader:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 读库（给检索侧用）


def facets_for_key(conn, key: str) -> dict:
    """读一篇的结构化字段（检索侧/插件要展示时用）；没有就返回空 dict。"""
    try:
        row = conn.execute("SELECT * FROM item_extra WHERE item_key = ?",
                           (key,)).fetchone()
    except Exception:  # noqa: BLE001  —— 表还没建过（没跑过 --sync）
        return {}
    return dict(row) if row else {}


def clc_list(clc: str) -> list[str]:
    """中图分类号 → 列表。检索侧要按"某一个分类号"筛时用它。"""
    return split_list(clc, r"[;；,，]")


def describe(row, compact: bool = False) -> dict:
    """把 `item_extra` 的一行变成"给人/给模型看"的字典（**空值一律不出现**）。

    为什么展示口径放在本模块、而不是让每个调用方自己拼：
      · 同一个字段在 `kb_item`、tldr 资源、检索结果里必须**一个说法**；
        三处各拼一遍，迟早出现"这边写「中图分类号」、那边写 `CLC`"；
      · 哪些该露出、哪些只是内部凭据（`dbname` / `filename` / `extra_raw`，
        它们是回溯用的原始值，不是给人读的），只有本模块最清楚。
      所以列的中文名一律从 `KEY_SPECS` 取（`_label`），**不在这里另抄一份**。

    `compact=True` 是给**检索结果**用的：只留"能帮人判断这篇是不是我要的"
    那几个（分类号 / 来源类型 / 收录标签 / 中文译名 / 专业），不带影响因子
    与内部凭据 —— 一次 8 条结果，每条都塞十几个字段会把上下文吃光。

    ⚠ `download` / `CNKICite` / `foundation` 这些"只原样留存"的键**不在这里出现**，
    要看得去 `kb_item` 的原始键值（`fields_json`）：用户拍板过
    `download` 只原样保存、不参与任何判断，就不该混进展示口径里当信号用。
    """
    if not row:
        return {}
    out: dict = {}

    def _put(name: str, key: str, value) -> None:
        """按 KEY_SPECS 里的中文名放一个值 —— 取不到就跳过（不显示空值）。"""
        if value in (None, "", [], {}):
            return
        spec = KEY_SPECS.get(key)
        out[spec.label if spec else name] = value

    _put("中图分类号", "CLC", clc_list((row.get("clc") or "").strip()))
    # `来源类型` / `出版状态` 是**派生列**（source_kind / is_advance_online），
    # 不是 extra 里的键，所以没有 KEY_SPECS 条目，中文名写在这里。
    kind = (row.get("source_kind") or "").strip()
    if kind:
        out["来源类型"] = KIND_LABELS.get(kind, kind)
    _put("收录标签", "publicationTag",
         split_list((row.get("publication_tag") or "").strip(), r"[,，]"))
    _put("中文译名", "titleTranslation",
         (row.get("title_translation") or "").strip())
    _put("学位专业", "major", (row.get("major") or "").strip())
    if compact:
        return out
    # ---- 以下是"展开看"才需要的东西（kb_item / tldr 用）
    _put("原文刊名", "original-container-title",
         (row.get("orig_container_title") or "").strip())
    _put("知网学科专辑", "album",
         split_list((row.get("album") or "").strip(), r"[;；]"))
    _put("期刊 ISSN", "ISSN", (row.get("issn") or "").strip())
    if row.get("is_advance_online"):
        out["出版状态"] = "网络首发（可能还没有正式卷期页码）"
    # 影响因子是**期刊级**指标（同一刊所有文章相同），所以标签上显式加"期刊级"：
    # 不加说明的话，拿它比较两篇文献的质量是错的（模块头已说明）。
    for key, value in (("CIF", row.get("cif")), ("AIF", row.get("aif"))):
        if value is not None:
            out[KEY_SPECS[key].label + "（期刊级，不是本文指标）"] = value
    return out


def fields_of(row) -> dict:
    """`fields_json` → 原始键值字典（extra 里**全部**键，含未解释的那些）。

    给 `kb_item` 用：结构化列是"提纯后的结论"，这里是"原文照录"，
    两者都要有 —— 将来发现某个键有用时，不必回去翻 Zotero。
    解析失败返回空字典（坏 JSON 不该让档案读不出来）。
    """
    try:
        got = json.loads((row or {}).get("fields_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def scan_keys(items) -> dict:
    """只读盘点：extra 里实际有哪些键、形态如何（`--scan` 走这条）。

    这个函数本身就是"调查"的可复现版本 —— 结论写在模块头的表里，
    数字由它随时重算，避免文档和实现对不上。
    """
    total = 0
    nonempty = 0
    keys: Counter = Counter()
    vals: dict[str, list[str]] = {}
    unparsed: Counter = Counter()
    dup_blocks = 0
    sep = Counter()
    line_counts = Counter()
    samples: list[list[str]] = []
    for item in items or []:
        total += 1
        raw = str((getattr(item, "fields", None) or {}).get("extra") or "")
        if not raw.strip():
            continue
        nonempty += 1
        rec = parse_extra(raw, key=str(getattr(item, "key", "") or ""))
        if rec.duplicates:
            dup_blocks += 1
        lines = [l for l in raw.splitlines() if l.strip()]
        line_counts[len(lines)] += 1
        for l in lines:
            if ":" in l:
                sep[l.split(":", 1)[1][:1] or "<行尾>"] += 1
        for name, value in rec.fields.items():
            keys[name] += 1
            vals.setdefault(name, [])
            if len(vals[name]) < 8 and value not in vals[name]:
                vals[name].append(value)
        for l in rec.unparsed:
            unparsed[l] += 1
        if len(samples) < 5:
            samples.append([l.split(":", 1)[0].strip() for l in lines if ":" in l])
    return {
        "total_items": total, "extra_nonempty": nonempty,
        "keys": dict(keys.most_common()),
        "samples": samples,
        # 值样本只给"形态"（长度 + 是否含中文/数字），**不回显真实内容** ——
        # extra 里的中文译名之类就是用户的研究内容，扫描输出不该把它带到终端日志里。
        "value_shapes": {name: [f"len={len(v)}" + ("/含中文" if re.search(r"[\u4e00-\u9fff]", v) else "")
                                 + ("/纯数字" if v.isdigit() else "")
                                 for v in vs]
                         for name, vs in vals.items()},
        "unparsed": dict(unparsed), "blocks_with_duplicate_keys": dup_blocks,
        "separator_after_colon": dict(sep),
        "line_counts": dict(sorted(line_counts.items())),
        "unexplained_keys": sorted(k for k in keys if k not in KEY_SPECS),
        "kept_only_keys": sorted(k for k, v in KEY_SPECS.items()
                                 if not v.promoted and k in keys),
        "coverage": {k: keys.get(k, 0) for k in KEY_SPECS},
    }


# ---------------------------------------------------------------- CLI


def cmd_scan(as_json: bool) -> int:
    import zreader

    reader = zreader.ZoteroReader()
    try:
        data = scan_keys(reader.load_items())
    finally:
        reader.close()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("=" * 66)
    print(f"全库 {data['total_items']} 篇，extra 非空 {data['extra_nonempty']} 篇")
    print("=" * 66)
    print("\nextra 里实际出现的键（按篇数）：")
    for name, cnt in data["keys"].items():
        spec = KEY_SPECS.get(name)
        mark = ("✅ 提纯" if spec and spec.promoted else
                ("⚠ 只留存" if spec else "？ 未登记"))
        label = spec.label if spec else "（模块里没登记这个键）"
        print(f"  {name:26} x{cnt:<4} {mark}  {label}")
    print(f"\n提纯不了的键：{'、'.join(data['kept_only_keys']) or '无'}")
    if data["unexplained_keys"]:
        print(f"⚠ 模块里没登记的键（需要人工确认含义）："
              f"{'、'.join(data['unexplained_keys'])}")
    print(f"\n不是 `键: 值` 形态的行：{sum(data['unparsed'].values())} 行")
    for line, cnt in list(data["unparsed"].items())[:5]:
        print(f"  x{cnt} {line[:60]!r}")
    print(f"含重复键的块：{data['blocks_with_duplicate_keys']} 个")
    print(f"冒号后首字符分布：{data['separator_after_colon']}")
    print(f"每块行数分布：{data['line_counts']}")
    print("\n各键的值形态（只给长度与构成，不回显真实内容）：")
    for name, shapes in data["value_shapes"].items():
        print(f"  {name:26} {shapes[:4]}")
    print("\n写入知识库：python offline/extrafill.py --sync")
    return 0


def cmd_show(key: str, as_json: bool) -> int:
    import zreader

    reader = zreader.ZoteroReader()
    try:
        hit = None
        for it in reader.load_items():
            if it.key == key:
                hit = it
                break
    finally:
        reader.close()
    if hit is None:
        print(f"[XX] Zotero 库里没有 key={key} 的条目")
        return 1
    got = row_for_item(hit)
    if got is None:
        print(f"{key} 的 extra 是空的，没有结构化字段可用")
        return 0
    row, rec = got
    if as_json:
        print(json.dumps({"row": row, "unparsed": rec.unparsed,
                          "duplicates": rec.duplicates},
                         ensure_ascii=False, indent=2))
        return 0
    print("=" * 66)
    print(f"{key}　extra 里解析出 {len(rec.fields)} 个键")
    print("=" * 66)
    for name, value in rec.fields.items():
        spec = KEY_SPECS.get(name)
        tag = (f"[{spec.label}]" if spec and spec.promoted else
               (f"[{spec.label}·只留存]" if spec else "[未登记]"))
        # 值可能很长（基金项目那种），截断显示
        shown = value if len(value) <= 60 else value[:57] + "…"
        print(f"  {name:26} {tag:24} {shown}")
    if rec.unparsed:
        print("\n没解析成 `键: 值` 的行（原样留着，不猜）：")
        for line in rec.unparsed:
            print(f"  {line[:70]!r}")
    if rec.duplicates:
        print(f"\n⚠ 重复出现的键（保留了第一个）：{'、'.join(rec.duplicates)}")
    print(f"\n派生：来源类型={row['source_kind'] or '未知'}　"
          f"网络首发={'是' if row['is_advance_online'] else '否'}")
    print(f"\n中图分类号：{clc_list(row['clc']) or '（无）'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 extra 里的结构化字段（知网写入的 CLC/dbcode/… ）用起来")
    ap.add_argument("key", nargs="?", default="", help="Zotero 条目 key")
    ap.add_argument("--scan", action="store_true", help="只读盘点 extra 里的键")
    ap.add_argument("--sync", action="store_true",
                    help="解析并写入知识库的 item_extra 表")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="输出结构化 JSON")
    args = ap.parse_args()

    if args.scan:
        return cmd_scan(args.as_json)
    if args.sync:
        stats = sync_all()
        if args.as_json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        print("=" * 66)
        print("结构化字段已写入知识库的 item_extra 表")
        print("=" * 66)
        print(f"  扫描条目：{stats['items']} 篇")
        print(f"  写入/更新：{stats['rows']} 行")
        print(f"  清空删除：{stats['cleared']} 行（extra 变空了的条目）")
        print(f"  未解析行：{stats['unparsed']}　重复键：{stats['duplicates']}")
        print(f"  键覆盖：{stats['keys']}")
        return 0
    if not args.key:
        ap.print_help()
        return 2
    return cmd_show(args.key, args.as_json)


if __name__ == "__main__":
    sys.exit(main())
