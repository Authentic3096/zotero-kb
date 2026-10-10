"""读 Zotero 库（只读）。

只做一件事：把 Zotero 的 SQLite 结构翻译成 schemas.py 里的数据类型。
不写任何文件、不碰原库 —— 构造时先做快照副本，后续全部读副本。

除了条目元数据，本模块还负责读**生命周期信号**（都是 Zotero 自己写下的、不用猜）：
  · `trashed_keys` —— 进了回收站（`deletedItems`）的 key，供两段式删除区分
    "归档"与"彻底删"（offline/trash.py）；
  · `merged_keys()` —— 合并留下的 `dc:replaces` 关系，供 `offline/keys.py` 落成
    知识库里的权威映射（本机实测 6 条）。
"""

from __future__ import annotations
# --- 让本模块可以独立运行（不依赖调用者先配好 sys.path）---
# 本项目模块平铺在 offline/ 与 online/ 下，用 `from schemas import ...` 这种
# 平铺方式互相导入，因此要求对应目录在 sys.path 里。调用者通常配好了，但
# **直接运行本文件**时没有"调用者"，就会 ModuleNotFoundError。
# 本机踩过同类问题：gui.py 只加了 offline/，面板点「列出全部经验」报
#   ModuleNotFoundError: No module named 'searcher'（它在 online/ 下）。
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.dirname(_HERE)
for _sub in ("offline", "online"):
    _p = _os.path.join(_ROOT, _sub)
    if _os.path.isdir(_p) and _p not in _sys.path:
        _sys.path.append(_p)
# 清掉临时名，别污染本模块的命名空间
del _os, _sys, _HERE, _ROOT, _sub, _p
# --- 路径设置结束 ---

import html
import os
import re
import shutil
import sqlite3

import schemas as S



class ZoteroReader:
    """只读 Zotero 库。构造时先做快照，后续全部读副本。

    ⚠ **快照必须把 `-wal` / `-shm` 一起拷，不能只拷 zotero.sqlite。**
    Zotero 10 起启用了 WAL 模式（官方 "Zotero 10 for Developers" 专门警告过），
    新写入先落在 `zotero.sqlite-wal` 里，主库文件可能长时间不更新。
    只 `shutil.copy2(zotero.sqlite)` 会读到一个**过时的**库 ——
    本机实测踩坑：本地 API 已经把 33 篇文献的分类改好了，而 reader 仍旧
    读到旧分类，于是"重建了全库但分类没变"，查了半天才发现问题在这儿。

    也试过用 `sqlite3.Connection.backup()`（理论上更干净），但它在 Zotero
    正开着的时候会被库锁**卡死**（实测挂住 5 分钟以上），所以改用文件级拷贝。
    拷贝期间 Zotero 若正好在写，理论上可能取到不一致的瞬时状态；
    对"建索引"这种只读用途可以接受 —— 下一轮增量会自愈。
    """

    def __init__(self, source_db: str = S.ZOTERO_DB, cache_dir: str = S.CACHE_DIR):
        os.makedirs(cache_dir, exist_ok=True)
        self.snapshot = os.path.join(cache_dir, "zotero-snapshot.sqlite")
        if not os.path.exists(source_db):
            raise FileNotFoundError(f"找不到 Zotero 库：{source_db}")

        # 旧快照（含它的 -wal/-shm）先清掉：残留的 -wal 会让 SQLite 把
        # 上一次的日志当成新库的一部分，恢复出乱七八糟的内容。
        for suffix in ("", "-wal", "-shm", "-journal"):
            stale = self.snapshot + suffix
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        # 主库 + WAL + 共享内存一起拷（顺序：先 -wal 再主库，尽量贴近一致性）
        for suffix in ("-wal", "-shm", ""):
            src = source_db + suffix
            if os.path.exists(src):
                try:
                    shutil.copy2(src, self.snapshot + suffix)
                except OSError:
                    pass

        self.conn = sqlite3.connect(self.snapshot)
        self.conn.row_factory = sqlite3.Row
        self.storage = S.ZOTERO_STORAGE
        # 已删除的条目不进知识库
        self.deleted = {r[0] for r in self.conn.execute("SELECT itemID FROM deletedItems")}
        # 回收站里的条目 **key**（本次新加，不改上面那条"排除"语义）。
        #
        # 为什么要单独暴露：Zotero 侧"进回收站"和"在回收站里彻底删"是两件事
        # ——前者 key 还在 items 表里、只是多了 deletedItems 一行；后者两处都没了。
        # 知识库要按这两种情况分别做"归档"与"删除"（方案附录 H.1），所以除了
        # itemID 还必须拿到 key。回收站里也可能躺着附件/笔记，它们的 key 一并返回，
        # 对账那边只关心"这条 key 在不在知识库里"，多余的无害。
        self.trashed_keys = self._keys_for_ids(self.deleted)
        # 上一次 `fulltext_for` 关于缓存的一句话结论（"异域脚本 12.3%" 之类）。
        # 正文是从哪来的只看得到 fulltext_src，看不出**为什么**没选另一条路；
        # 排障时要能回答"这篇为什么走了 PyMuPDF"，所以留这一格。
        self._last_cache_note = ""
        # 上一次缓存体检的结论（ok / bad / unsure / ""=没有缓存可用）
        self._last_cache_verdict = ""

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ 条目集合

    def top_level_items(self) -> list[int]:
        """真正"文献"级别的条目：排除附件、笔记、标注、已删除。"""
        rows = self.conn.execute(
            """
            SELECT i.itemID
            FROM items i
            WHERE i.itemID NOT IN (SELECT itemID FROM itemAttachments)
              AND i.itemID NOT IN (SELECT itemID FROM itemNotes)
              AND i.itemID NOT IN (SELECT itemID FROM itemAnnotations)
            ORDER BY i.itemID
            """
        ).fetchall()
        return [r[0] for r in rows if r[0] not in self.deleted]

    # ------------------------------------------------------------ 合并关系

    def merged_keys(self, predicate: str = "dc:replaces") -> tuple[dict[str, str], int]:
        """Zotero 的合并关系：`被合并掉的 key → 保留项 key`。

        Zotero 做「Merge Items」时在**保留项**上写一条 `itemRelations` 行
        （本机实测：6 条，谓词全是 `dc:replaces`）。读它就能拿到精确的
        `old → new` 映射 —— 不用猜、不花钱（方案附录 G.1）。

        ⚠ **谓词按名字取，不写死 predicateID**：`relationPredicates` 现在确实
          只有一条（ID 挂在 1 上），但那是 Zotero 的实现细节；一旦库里出现第二种
          关系，"取 ID=1"就会开始返回错的东西，而且不报错。
        ⚠ object 是 **URI 不是 key**：`http://zotero.org/users/<uid>/items/<KEY>`。
          路径倒数第二段必须是 `items`（群组库是 `/groups/<id>/items/<KEY>`），
          尾段才是 key —— 直接 `rsplit("/")` 会把别的形态的 URI 也当成映射收进来。
        ⚠ **形态意外的行跳过、不抛**：本方法是"补齐知识库"的一环，Zotero 里出现
          没见过的关系形态不该让整轮构建挂掉。所以返回 `(映射, 跳过条数)`，
          由调用方决定要不要报出来。

        只读：不写 Zotero 库，也不碰知识库（落库是 `offline/keys.py` 的事）。
        """
        out: dict[str, str] = {}
        try:
            rows = self.conn.execute(
                """
                SELECT i.key AS holder, r.object AS object
                FROM itemRelations r
                JOIN items i ON i.itemID = r.itemID
                JOIN relationPredicates p ON p.predicateID = r.predicateID
                WHERE p.predicate = ?
                """,
                (predicate,),
            ).fetchall()
        except sqlite3.Error:
            # 老库/精简库里没有这两张表：当"没有合并"处理，别让调用方炸
            return {}, 0
        skipped = 0
        for row in rows:
            # ⚠ 方向别搞反（本机实测踩到）：**保留项持有关系、object 指向被并掉的那条**。
            #   所以 holder 才是 new_key、URI 尾段才是 old_key。反过来的话映射表会
            #   写成 PALT487S → N38Y49NF，于是"合并后的项"被解析到一个已不存在的
            #   key 上 —— 而且因为 old_key 是主键，两条关系还会撞成一行（6 条只剩 4 行）。
            old_key = self._item_key_from_uri(row["object"])
            new_key = str(row["holder"] or "")
            if not old_key or not new_key:
                skipped += 1
                continue
            out[old_key] = new_key
        return out, skipped

    @staticmethod
    def _item_key_from_uri(uri: str) -> str:
        """从 `…/items/<KEY>` URI 里取 key；形态对不上返回 ""（由调用方跳过）。"""
        parts = [p for p in str(uri or "").split("/") if p]
        if len(parts) >= 2 and parts[-2] == "items":
            return parts[-1]
        return ""

    def _keys_for_ids(self, item_ids: set[int]) -> set[str]:
        """itemID → key（只查 Zotero 的 items 表，不碰知识库）。"""
        if not item_ids:
            return set()
        ids = sorted(item_ids)
        ph = ",".join("?" * len(ids))
        return {r["key"] for r in self.conn.execute(
            f"SELECT key FROM items WHERE itemID IN ({ph})", ids)}

    def load_items(self, item_ids: list[int] | None = None) -> list[S.Item]:
        ids = self.top_level_items() if item_ids is None else [
            i for i in item_ids if i not in self.deleted
        ]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        fields = self._fields_for(ids, ph)
        authors = self._authors_for(ids, ph)
        coll_names, coll_keys = self._collections_for(ids, ph)
        tags = self._tags_for(ids, ph)
        attachments = self._attachments_for(ids, ph)
        notes = self._notes_for(ids, ph)
        annotations = self._annotations_for(ids, ph)
        versions = self._versions_for(ids, ph)
        types = {
            r["itemID"]: r["typeName"]
            for r in self.conn.execute(
                f"""SELECT i.itemID, t.typeName FROM items i
                    JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
                    WHERE i.itemID IN ({ph})""",
                ids,
            )
        }

        items: list[S.Item] = []
        for iid in ids:
            date_added, version, key = versions.get(iid, ("", 0, str(iid)))
            items.append(
                S.Item(
                    item_id=iid,
                    key=key,
                    item_type=types.get(iid, "unknown"),
                    title=fields[iid].get("title", ""),
                    fields=fields[iid],
                    authors=authors[iid],
                    collections=coll_names[iid],
                    collection_keys=coll_keys[iid],
                    tags=tags[iid],
                    notes=notes[iid],
                    annotations=annotations[iid],
                    attachments=attachments[iid],
                    pdfs=[a for a in attachments[iid] if a.content_type == "application/pdf"],
                    date_added=date_added,
                    version=version,
                )
            )
        return items

    # ------------------------------------------------------------ 各字段查询

    def _fields_for(self, ids: list[int], ph: str) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {i: {} for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT d.itemID, f.fieldName, v.value
            FROM itemData d
            JOIN itemDataValues v ON v.valueID = d.valueID
            JOIN fields f ON f.fieldID = d.fieldID
            WHERE d.itemID IN ({ph})
            """,
            ids,
        ):
            out[row["itemID"]][row["fieldName"]] = row["value"] or ""
        return out

    def _authors_for(self, ids: list[int], ph: str) -> dict[int, list[S.Author]]:
        out: dict[int, list[S.Author]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT ic.itemID, ic.orderIndex, ct.creatorType,
                   cr.firstName, cr.lastName, cr.fieldMode
            FROM itemCreators ic
            JOIN creators cr ON cr.creatorID = ic.creatorID
            JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
            WHERE ic.itemID IN ({ph})
            ORDER BY ic.itemID, ic.orderIndex
            """,
            ids,
        ):
            # fieldMode=1：姓名整体存在 lastName（中文名、机构名多为这种）
            if row["fieldMode"]:
                first, last = "", (row["lastName"] or row["firstName"] or "")
            else:
                first, last = row["firstName"] or "", row["lastName"] or ""
            out[row["itemID"]].append(
                S.Author(first=first, last=last,
                         creator_type=row["creatorType"], order=row["orderIndex"])
            )
        return out

    def _collections_for(self, ids: list[int], ph: str):
        names: dict[int, list[str]] = {i: [] for i in ids}
        keys: dict[int, list[str]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT ci.itemID, c.collectionName, c.key
            FROM collectionItems ci
            JOIN collections c ON c.collectionID = ci.collectionID
            WHERE ci.itemID IN ({ph})
            ORDER BY c.collectionName
            """,
            ids,
        ):
            names[row["itemID"]].append(row["collectionName"])
            keys[row["itemID"]].append(row["key"])
        return names, keys

    def _tags_for(self, ids: list[int], ph: str) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT it.itemID, t.name FROM itemTags it
            JOIN tags t ON t.tagID = it.tagID
            WHERE it.itemID IN ({ph}) ORDER BY t.name
            """,
            ids,
        ):
            out[row["itemID"]].append(row["name"])
        return out

    def _versions_for(self, ids: list[int], ph: str) -> dict[int, tuple]:
        out: dict[int, tuple] = {}
        for row in self.conn.execute(
            f"SELECT itemID, dateAdded, version, key FROM items WHERE itemID IN ({ph})", ids
        ):
            out[row["itemID"]] = (row["dateAdded"] or "", row["version"], row["key"])
        return out

    def _attachments_for(self, ids: list[int], ph: str) -> dict[int, list[S.Attachment]]:
        out: dict[int, list[S.Attachment]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT ia.parentItemID, ia.contentType, ia.linkMode, ia.path, i.key,
                   (SELECT v.value FROM itemData d
                    JOIN itemDataValues v ON v.valueID = d.valueID
                    JOIN fields f ON f.fieldID = d.fieldID
                    WHERE d.itemID = ia.itemID AND f.fieldName = 'title') AS title
            FROM itemAttachments ia
            JOIN items i ON i.itemID = ia.itemID
            WHERE ia.parentItemID IN ({ph})
            ORDER BY ia.parentItemID, ia.itemID
            """,
            ids,
        ):
            path = self._resolve_attachment_path(row["key"], row["path"])
            att = S.Attachment(
                key=row["key"],
                title=row["title"] or "",
                content_type=row["contentType"] or "",
                link_mode=row["linkMode"] if row["linkMode"] is not None else -1,
                path=path,
                exists=bool(path) and os.path.exists(path),
                has_ft_cache=bool(path) and os.path.exists(
                    os.path.join(os.path.dirname(path), ".zotero-ft-cache")
                ),
            )
            out.setdefault(row["parentItemID"], []).append(att)
        return out

    def _resolve_attachment_path(self, att_key: str, raw_path: str | None) -> str:
        """Zotero 的 path 字段形如 'storage:文件名.pdf'，也可能是绝对路径。"""
        if not raw_path:
            return ""
        if raw_path.startswith("storage:"):
            return os.path.join(self.storage, att_key, raw_path[len("storage:"):])
        if raw_path.startswith("attachments:"):
            return os.path.join(S.ZOTERO_DATA_DIR, "attachments", raw_path[len("attachments:"):])
        return raw_path

    def _notes_for(self, ids: list[int], ph: str) -> dict[int, list[S.Note]]:
        out: dict[int, list[S.Note]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT n.parentItemID, n.note, i.key
            FROM itemNotes n JOIN items i ON i.itemID = n.itemID
            WHERE n.parentItemID IN ({ph})
            ORDER BY n.parentItemID, n.itemID
            """,
            ids,
        ):
            md, n_img = html_to_markdown(row["note"] or "")
            out.setdefault(row["parentItemID"], []).append(
                S.Note(key=row["key"], html=row["note"] or "", markdown=md, image_refs=n_img)
            )
        return out

    def _annotations_for(self, ids: list[int], ph: str) -> dict[int, list[S.Annotation]]:
        out: dict[int, list[S.Annotation]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"""
            SELECT a.parentItemID, a.type, a.text, a.comment, a.color, a.pageLabel, i.key
            FROM itemAnnotations a JOIN items i ON i.itemID = a.itemID
            WHERE a.parentItemID IN ({ph})
            ORDER BY a.parentItemID, CAST(a.pageLabel AS INTEGER), a.itemID
            """,
            ids,
        ):
            out.setdefault(row["parentItemID"], []).append(
                S.Annotation(
                    key=row["key"],
                    type=S.ANNOTATION_TYPES.get(row["type"], f"type{row['type']}"),
                    text=(row["text"] or "").strip(),
                    comment=(row["comment"] or "").strip(),
                    color=row["color"] or "",
                    page=row["pageLabel"] or "",
                )
            )
        return out

    # ------------------------------------------------------------ 全文

    def fulltext_for(self, item: S.Item,
                     force_pymupdf: bool = False,
                     denoise: bool | None = None,
                     use_model: bool | None = None,
                     model: str = "",
                     arbitrate: bool | None = None) -> tuple[list[str], str]:
        """取一篇文献的正文页列表。

        默认优先 Zotero 自己的 `.zotero-ft-cache`（快、零解析成本），
        不可用时才用 PyMuPDF 逐页提取。返回 (pages, 来源标记)。

        ⚠ `force_pymupdf=True`：**跳过 Zotero 缓存，强制自己解析 PDF**。
          为什么需要：Zotero 的缓存不总是可信 —— 本机实测有一批文献
          缓存里是**码位错乱**的文本（`ଽಸิေ\\nՈ๙Ո৯၎...` 这种），
          而同一份 PDF 用 PyMuPDF 提取完全正常（`第34卷第1期 中国电机
          工程学报`）。坏缓存会污染检索与向量，所以"修复"时必须能绕开它。
          这个模式下**不做仲裁**：调用方（repair_chunks / 逐篇重建）
          要的就是纯 PyMuPDF 文本。

        ⚠ `denoise`：页眉页脚过滤（默认开）。None=按环境变量/默认决定，
          True/False 显式覆盖。见 `drop_running_headers()` 的说明。

        ⚠ `arbitrate`：是否"两条路都跑 + 模型仲裁取舍"。None=按模式
          （见 `cache_model_mode`：auto 只在灰区仲裁，deep 每篇都仲裁）。

        ## 决策表（cache_verdict 的结论 × 模式）

            verdict    off            auto                deep
            ok         用缓存          用缓存              跑两条路，模型选
            unsure     换 PyMuPDF     跑两条路，模型选     跑两条路，模型选
            bad        换 PyMuPDF     换 PyMuPDF          跑两条路，模型选

        为什么 bad 在 deep 下也要仲裁：规则说缓存坏，但 PyMuPDF 未必更好 ——
        遇到**扫描件**时 PyMuPDF 提不出字，而 Zotero 缓存里可能有 OCR 结果，
        这时换过去反而把仅有的文本丢了。模型对比能挡住这种情况。
        """
        do_filter = headers_filter_enabled(denoise)
        mode = cache_model_mode(use_model)
        want_model = mode != "off"
        deep = (mode == "deep") if arbitrate is None else bool(arbitrate)

        def _finish(pages: list[str], src: str) -> tuple[list[str], str]:
            """两条路径共用出口 —— 过滤必须对 cache 和 pymupdf 都生效，
            否则同一篇文献换个来源就会得到不同的文本。"""
            if do_filter and pages:
                pages, _ = drop_running_headers(pages)
            return self._deshift_fix(pages, src)

        # ---- 候选附件：**只认 PDF**
        #
        # ⚠ 网页剪藏（HTML 附件）刻意不处理。曾经加过一版"HTML 附件的
        #   Zotero 缓存也能用"的兜底，实测后按用户意见去掉了：
        #     · 正文里混着大量网页模板文字（"新闻政策招投标项目技术…
        #       收藏点赞分享订阅投稿"），进检索是噪声
        #     · 这类条目多是行业新闻页，不是要精读的文献
        #   构建时对"没有 PDF 附件的条目"直接跳过，并在收尾时列出清单
        #   提示用户（见 convert.py 的 skipped_no_pdf）。
        cands: list[S.Attachment] = list(item.pdfs)

        # ---- 第一路：Zotero 缓存
        cache_pages: list[str] = []
        cache_tag = "zotero-cache"
        verdict, why = "", ""
        self._last_cache_verdict, self._last_cache_note = "", ""
        if not force_pymupdf:
            for att in cands:
                if not att.path:
                    continue
                cache = os.path.join(os.path.dirname(att.path),
                                     ".zotero-ft-cache")
                if not os.path.exists(cache):
                    continue
                try:
                    with open(cache, "r", encoding="utf-8",
                              errors="replace") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                pages = self._pages_from_cache(raw)
                if not any(len(p) > 100 for p in pages):
                    continue          # 缓存基本是空的，当它不存在
                verdict, why = cache_verdict(pages, item.title)
                cache_pages = pages
                self._last_cache_verdict = verdict
                self._last_cache_note = why
                if verdict == "ok" and not deep:
                    return _finish(pages, cache_tag)
                break                 # 只看第一个有缓存的附件

        # ---- 第二路：PyMuPDF（只在需要时跑 —— 它是这里最慢的一步）
        pm_pages: list[str] = []
        need_pm = (not cache_pages) or force_pymupdf or deep \
            or verdict in ("bad", "unsure")
        if need_pm:
            for pdf in item.pdfs:
                if not (pdf.exists and pdf.path.lower().endswith(".pdf")):
                    continue
                got = self._pages_from_pymupdf(pdf.path)
                # 同样要过长度关：PyMuPDF 也可能只提到零星几个字
                if got and any(len(p) > 100 for p in got):
                    pm_pages = got
                    break

        # ---- 取舍
        if cache_pages and pm_pages:
            pick = (arbitrate_by_model(cache_pages, pm_pages, model)
                    if want_model else "")
            if pick == "cache":
                return _finish(cache_pages, cache_tag)
            if pick == "pymupdf":
                return _finish(pm_pages, "pymupdf")
            # 模型说 same 或没给出结论 → 按规则裁：
            # 缓存规则判定 ok（只可能出现在 deep 模式）就留着，别白换一次；
            # bad / unsure 一律从严走 PyMuPDF。
            if verdict == "ok":
                return _finish(cache_pages, cache_tag)
            return _finish(pm_pages, "pymupdf")
        if pm_pages:
            return _finish(pm_pages, "pymupdf")
        if cache_pages:
            # 兜底：有坏文本总比完全没有强（fulltext_src 能看出走了哪条）
            return _finish(cache_pages, cache_tag)
        return [], ""

    @staticmethod
    def _deshift_fix(pages: list[str], src: str) -> tuple[list[str], str]:
        """字符整体偏移的正文自动还原；没偏就原样返回。

        ## 为什么需要（本机实测）

        有一类 PDF 的字体没有可用的 ToUnicode 映射，提取出来的**每个字符
        都比真字符小 29**：

            &DOFXODWLRQ DQG 6LPXODWLRQ RI 0DJQHWLF 'LSROH )LHOGV
            还原（+29）后 →
            Calculation and Simulation of Magnetic Dipole Fields

        ⚠ 换源**修不了**它：本机那篇的 Zotero 缓存和 PyMuPDF **都是**偏移文本
          （PyMuPDF 还更糟，用 \\x03 当空格）。唯一的办法是位移还原。

        ⚠ 分页要保住：位移量从**整篇**算（单页词数可能不够，
          `find_shift` 有 200 词的门槛），但位移要**逐页施加** ——
          对拼接后的整篇做还原再 split 会把页内换行也当成分页符。

        入口 `S.shift_fix` 只在"是外文文献 + 常见词率极低 + 存在某个位移能
        把它救回正常水平"三条同时成立时才给位移，所以正常文本不会被误改
        （全库 97 篇实测：只命中 1 篇，就是真坏的那篇）。
        还原后的来源标记加 `+fix<位移>` 后缀，`fulltext_src` 里能看出来。
        """
        if not pages or not deshift_enabled():
            return pages, src
        sh, _ = S.shift_fix("\n".join(pages))
        if not sh:
            return pages, src
        return [S.deshift(p, sh) for p in pages], f"{src}+fix{sh:+d}"

    @staticmethod
    def _pages_from_cache(raw: str) -> list[str]:
        """Zotero 缓存用 \\f 分隔页；没有分隔符就整体当一页。"""
        if "\f" in raw:
            return [clean_text(p) for p in raw.split("\f")]
        return [clean_text(raw)]

    @staticmethod
    def _pages_from_pymupdf(path: str) -> list[str]:
        try:
            import pymupdf
        except ImportError:
            try:
                import fitz as pymupdf  # 兼容 PyMuPDF 旧导入名
            except ImportError:
                return []
        pages: list[str] = []
        try:
            doc = pymupdf.open(path)
        except Exception:  # noqa: BLE001
            return []
        try:
            for page in doc:
                pages.append(clean_text(page.get_text()))
        except Exception:  # noqa: BLE001
            pass
        finally:
            doc.close()
        return pages

    def collection_tree(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT c.collectionID, c.key, c.collectionName, c.parentCollectionID,
                       (SELECT COUNT(*) FROM collectionItems ci
                        WHERE ci.collectionID = c.collectionID) AS n
                FROM collections c ORDER BY c.collectionName
                """
            )
        ]


# ---------------------------------------------------------------- 文本清洗

_PAGE_MARK = re.compile(r"^## p\.(\d+)\s*$", re.MULTILINE)


def _strip_cjk_spacing(text: str) -> str:
    """去掉中日韩字符之间的分隔空格。

    中文 PDF 的 .zotero-ft-cache 常见「叶 建 成 等 : 航 天 器」这种把每个汉字
    用空格隔开的排版。它不影响 FTS5（单字照样成 token），但：
      · 片段读起来很别扭；
      · 连续中文短语被空格打断，"神经网络"这种整体匹配无从谈起；
      · 送进向量模型会明显拉低语义质量。
    所以只要空格两边都是 CJK 字符（或 CJK 与中文标点），就把空格删掉。
    英文字母之间的空格**必须保留**，否则会把英文粘成一坨。
    """
    if " " not in text:
        return text
    cjk = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    punct = "，。、；：？！“”‘’（）《》【】…—～·「」『』"
    # 反复替换，处理 "航 天 器" 这种连续多个空格间隔
    pattern = re.compile(f"([{cjk}{punct}]) +([{cjk}{punct}])")
    prev = None
    while prev != text:
        prev = text
        text = pattern.sub(r"\1\2", text)
    return text


def clean_text(text: str) -> str:
    """清 PDF 提取里的常见噪音：多余空白、孤立短行、连字符断行、汉字间空格。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = _strip_cjk_spacing(text)
    # 行尾连字符断行：research-\n     ers -> researchers
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        # 丢掉纯页码与孤立符号行（PDF 页眉页脚残留）
        if re.fullmatch(r"\d{1,4}", stripped):
            continue
        if len(stripped) <= 2 and not stripped.isalnum():
            continue
        lines.append(stripped)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- 缓存体检
#
# 为什么需要：全库 111 篇扫描发现 10 篇（9%）的 Zotero 缓存是坏文本，
# 而 `fulltext_for` 原来的准入判据只有 `any(len(p) > 100)` —— 只查长度不查质量，
# 于是坏文本被无条件采信入库。
#
# 两级判定（用户 2026-10-03 要求"判据严格些，不确定的交给本地模型"）：
#   规则能确定的 → 直接判 ok / bad
#   规则判不了的灰区 → 调本地模型对比两段样本
CACHE_BAD_EXOTIC = 0.10    # 异域脚本 >10%   → 确定坏（实测 12%~41%）
CACHE_OK_EXOTIC = 0.01     # 异域脚本 <1%    → 这一项没问题
CACHE_BAD_CJK = 0.005      # 非外文却 CJK <0.5% → 确定坏（实测 0%~0.2%）
CACHE_OK_CJK = 0.05        # CJK >5%         → 中文正常
CACHE_LATIN_FOREIGN = 0.80  # 拉丁字母 >80% → 外文文献，CJK 少属正常
# ⚠ 这个阈值是实测卡出来的，不是拍的：
#     · 6 篇"中文全丢"的坏缓存：latin 59%~76%（正文没了，只剩刊名/邮箱/编号）
#     · 5 篇真英文文献：        latin 87%~91%
#   取 0.80 正好落在两簇中间。定得太低（如 0.5）会把坏缓存误放行。


# 环境变量取值 → 模式（写一处，两个入口共用，避免将来两处漂移）
_CACHE_MODEL_DEEP = ("deep", "always", "2")
_CACHE_MODEL_OFF = ("0", "false", "no", "off")


def cache_model_mode(cli_flag: bool | None = None) -> str:
    """缓存校验用不用本地模型：返回 `"off"` / `"auto"` / `"deep"`。

        off   只用规则，不调模型（最快，秒级）
        auto  只在规则灰区（unsure）调模型仲裁 —— 默认
        deep  每篇都跑两条路（缓存 + PyMuPDF）再让模型选（最慢，最准）

    优先级：显式参数 > 环境变量 `ZOTERO_KB_CACHE_MODEL` > 默认 auto。
    环境变量取值：`0/false/no/off` → off；`deep/always/2` → deep；其余 → auto。
    """
    v = (os.environ.get("ZOTERO_KB_CACHE_MODEL") or "").strip().lower()
    if cli_flag is False:
        return "off"
    if v in _CACHE_MODEL_OFF:
        return "off"
    if v in _CACHE_MODEL_DEEP:
        return "deep"
    return "auto"


def cache_model_enabled(cli_flag: bool | None = None) -> bool:
    """灰区是否调本地模型（`cache_model_mode` 的布尔形式，向后兼容）。

    默认开：灰区本来就少（全库 111 篇里明确的坏缓存才 10 篇），
    调用量小；而判错的代价是整篇文献入库文本不可用，比多花几秒贵得多。
    """
    return cache_model_mode(cli_flag) != "off"


def cache_verdict(pages: list[str], title: str = "") -> tuple[str, str]:
    """缓存体检。返回 ("ok" | "bad" | "unsure", 原因)。

    unsure 是灰区 —— 规则不敢下结论的，交给模型（见 arbitrate_by_model）。
    宁可多送几个进灰区，也不要规则误判：误判成 ok 会让坏文本入库，
    误判成 bad 会让好文本白跑一遍 PyMuPDF（只是慢一点，不出错）。
    """
    text = "".join(pages or [])
    st = S.char_stats(text)
    if st["total"] < 100:
        return "bad", f"有效字符仅 {st['total']}"

    # ⚠ 不再用标题判断"是不是中文文献"。实测教训：本机有 5 篇 IEEE 英文论文
    #   的 item.title 是中文（用户起的中文名或 Zotero 译名），正文全英文 ——
    #   用标题判断会把它们冤枉成坏缓存。其中 B9FDAHMP 的 PyMuPDF 只提到
    #   缓存的 13% 字符，换过去等于用坏文本换好文本。
    #   改用【缓存自身的拉丁字母占比】：外文文献字母多，真丢字的中文文献
    #   只剩数字标点（如 `'2017 1 67 , ( 266071) :2016-08-17'`）。
    #   title 参数保留只为兼容调用方，不再参与判断。
    ex, cj, la = st["exotic_ratio"], st["cjk_ratio"], st["latin_ratio"]

    if ex > CACHE_BAD_EXOTIC:
        return "bad", f"异域脚本 {ex:.1%}（码位错乱特征）"

    if la > CACHE_LATIN_FOREIGN:
        return "ok", ""      # 外文文献，没中文是应该的

    if cj < CACHE_BAD_CJK:
        return "bad", f"非外文却 CJK 仅 {cj:.2%}（字体映射失败）"

    if ex > CACHE_OK_EXOTIC:
        return "unsure", f"异域脚本 {ex:.1%} 落在灰区"
    if cj < CACHE_OK_CJK:
        return "unsure", f"CJK {cj:.1%} 偏低，可能是坏缓存"
    return "ok", ""


def arbitrate_by_model(cache_pages: list[str], pm_pages: list[str],
                       model: str = "", sample: int = 1200) -> str:
    """让本地模型在两种提取结果之间选一个更好的。

    返回 "cache" / "pymupdf" / ""（判断不了）。

    为什么用【对比】而不是给单个结果打分：实测发现让模型独立判断
    "这段是不是坏切片"很不稳（本机一次全库检查里 60% 被判坏，而抽查
    发现那些段落其实是正常正文）。两段并排比较要容易得多 —— 模型只需
    回答"哪个更像正常论文"，不需要自己定一个绝对标准。

    任何异常都返回 ""，由调用方按保守策略决定（见 fulltext_for）。
    """
    c = "\n".join(cache_pages or [])[:sample]
    p = "\n".join(pm_pages or [])[:sample]
    if not c and not p:
        return ""
    if not c:
        return "pymupdf"
    if not p:
        return "cache"
    prompt = (
        "下面是同一篇论文的两种正文提取结果（A 来自 Zotero 文本缓存，"
        "B 来自直接解析 PDF）。请判断哪一段更适合作为论文正文。\n"
        "判断标准：内容是否完整、有没有大面积乱码或字符错乱、"
        "有没有缺字、该有中文的地方是否真的有中文。\n\n"
        f"【A】\n{c}\n\n【B】\n{p}\n\n"
        '只输出 JSON：{"better": "A" 或 "B" 或 "same", "reason": "一句话理由"}'
    )
    try:
        import judge as J

        r = J.generate(prompt, model=model, json_mode=True, temperature=0.0)
        if not isinstance(r, dict) or not r.get("ok"):
            return ""
        ok, data = J.parse_json(r.get("text") or "")
        if not ok or not isinstance(data, dict):
            return ""
        b = str(data.get("better") or "").strip().upper()
        if b == "A":
            return "cache"
        if b == "B":
            return "pymupdf"
        return ""      # same 或无法解析 → 交给调用方
    except Exception:  # noqa: BLE001
        return ""


def headers_filter_enabled(cli_flag: bool | None = None) -> bool:
    """页眉页脚过滤开关。优先级：显式参数 > 环境变量 > 默认开。

    默认开是因为它**只删每页重复出现的行**，误判空间很小（50 篇分层抽样
    实测误杀 0），而去掉页眉对检索是结构性改善：页眉出现在每一页，
    等于**每一个切片**都带着它。
    """
    if cli_flag is not None:
        return cli_flag
    v = (os.environ.get("ZOTERO_KB_HEADERS") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def deshift_enabled(cli_flag: bool | None = None) -> bool:
    """字符偏移自动还原的开关。优先级：显式参数 > 环境变量 > 默认开。

    默认开：它只在**三条判据同时成立**时才动手（外文文献 + 常见词率 <12%
    + 存在某个位移能把常见词率救到 ≥15% 且提升 ≥10 个百分点），
    全库 97 篇实测只命中 1 篇、就是真坏的那篇，误改风险极低；
    而收益是从"整篇 100% 不可读"变成"可正常检索阅读"。

    真要关就设 `ZOTERO_KB_DESHIFT=0`（比如怀疑某篇被误还原时）。
    """
    if cli_flag is not None:
        return cli_flag
    v = (os.environ.get("ZOTERO_KB_DESHIFT") or "").strip().lower()
    return v not in ("0", "false", "no", "off")


def _norm_edge(line: str) -> str:
    """页眉页脚行归一化：抹数字（页码/卷期每页不同）+ 压空白 + 小写。"""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", line or "")).strip().lower()


def drop_running_headers(pages: list[str], *, head: int = 2, tail: int = 2,
                         min_frac: float = 0.35, max_len: int = 300,
                         min_pages: int = 3) -> tuple[list[str], dict]:
    """去掉每页重复出现的页眉页脚行，返回 (过滤后的页列表, 统计)。

    为什么在【按页文本】上做而不是用 PDF 坐标：
      知识库默认读 Zotero 的 `.zotero-ft-cache`，那里**没有坐标**。
      按页文本的"行位置"是坐标的近似 —— 页首 K 行 ≈ 顶部边缘带，
      页尾 K 行 ≈ 底部边缘带。这样 cache 与 PyMuPDF 两条路径都受益。

    判据（只删同时满足的）：
      · 出现在页首 head 行或页尾 tail 行内
      · 归一化后在 ≥min_frac 的页上重复
      · 归一化后 ≤max_len 字符（长文本必然是正文，保护它）

    ⚠ 抓不到"只出现一次"的首页版权/DOI 行 —— 那类污染只有 1 次，
      危害远小于每页重复 40 次的页眉。
    """
    from collections import Counter

    n = len(pages)
    if n < min_pages:
        return pages, {"dropped": 0, "reason": "页数不足，跳过"}

    cnt: Counter = Counter()
    for pg in pages:
        lines = [l.strip() for l in (pg or "").split("\n") if l.strip()]
        if len(lines) < 4:          # 太短的页整页都算"边缘"，跳过以免误伤
            continue
        for l in lines[:head] + lines[-tail:]:
            k = _norm_edge(l)
            if k and len(k) <= max_len:
                cnt[k] += 1

    need = max(2, round(n * min_frac))
    bad = {k for k, c in cnt.items() if c >= need}
    if not bad:
        return pages, {"dropped": 0, "patterns": 0}

    out, dropped = [], 0
    for pg in pages:
        keep = []
        for l in (pg or "").split("\n"):
            if l.strip() and _norm_edge(l) in bad:
                dropped += 1
                continue
            keep.append(l)
        out.append("\n".join(keep))
    return out, {"dropped": dropped, "patterns": len(bad)}


def split_pages(fulltext: str) -> list[str]:
    """把带 `## p.N` 标记的全文拆成页列表（供按页返回）。"""
    if not fulltext:
        return []
    parts = _PAGE_MARK.split(fulltext)
    pages = []
    for i in range(1, len(parts), 2):
        pages.append(parts[i + 1].strip() if i + 1 < len(parts) else "")
    return pages


def html_to_markdown(raw: str) -> tuple[str, int]:
    """Zotero 笔记是 HTML，转成 Markdown。

    返回 (markdown, 内嵌图片引用数)。只做保守转换：认得的标签转，
    认不得的剥掉标签保留文字。内嵌截图（data-attachment-key）不搬运二进制，
    只留一行说明 —— 图片本来就在 Zotero 里看。
    """
    if not raw:
        return "", 0
    n_images = len(re.findall(r"data-attachment-key", raw))

    text = raw
    # 图片：先把内嵌截图换成占位说明，再清掉其余 img
    text = re.sub(
        r"<img[^>]*data-attachment-key=\"([^\"]+)\"[^>]*>",
        lambda m: f"\n> [内嵌截图，附件 key={m.group(1)}；在 Zotero 中查看]\n",
        text,
    )
    text = re.sub(r"<img[^>]*>", "", text)
    # 链接
    text = re.sub(
        r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        lambda m: f"[{m.group(2)}]({m.group(1)})",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # 标题
    for level in range(1, 7):
        text = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m, lv=level: "\n" + "#" * lv + " " + m.group(1).strip() + "\n",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # 强调与代码
    text = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"*\2*", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text,
                  flags=re.DOTALL | re.IGNORECASE)
    # 列表
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?(ul|ol)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # 段落与换行
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    # 其余标签一律剥掉，实体解码
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    # 笔记里也常见"航 天 器"这种字间空格（多为从 PDF 复制粘贴带入），同样清理
    text = _strip_cjk_spacing(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), n_images
