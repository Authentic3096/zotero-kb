"""混合检索：关键词（FTS5）+ 向量（余弦）→ RRF 融合 → 经验权重加成。

为什么两路都要：
    关键词擅长专有名词（"卷积神经网络""T-L 模型"），向量擅长"换了个说法"
    （"怎么把高维特征压到低维" 命中 特征压缩方法）。学术库里两者互补，
    缺一路都会漏。

为什么加权放在融合之后：
    权重是"这篇文献对我的价值"的先验，不该干扰召回（该出现的必须出现），
    只该影响排序。所以先融合、再乘权重、最后截断。
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

import json
import os
import re
import sys
import threading

import schemas as S
import extrafill as XF
from query import (build_match, build_match_phrases, coverage_units, keywords,

                   match_terms)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 融合常数：RRF 的标准取值，60 让排名靠前的结果优势明显但不过分
RRF_K = 60
# 关键词路先取多少候选参与融合
CANDIDATES = 200
# 短语召回至少命中这么多条才算够用，否则退到单字宽召回
MIN_PHRASE_HITS = 5
# 两路在融合时的权重。关键词路是主证据（查询词真的出现在文里），
# 向量路补充语义召回。见 `_rrf` 的说明。
KEYWORD_ROUTE_WEIGHT = 0.65
VECTOR_ROUTE_WEIGHT = 0.35

# 正文提取不可信的文献，整篇降权到这个倍数。
#
# 为什么是"整篇乘一个系数"而不是给坏切片扣分：提取失败是**整篇**现象
# （源 PDF 字体编码坏 → 全篇字符偏移），一篇里不存在"这几个切片可信、
# 那几个不可信"。判定与理由见 `offline/check_chunks.py`。
#
# 为什么留 0.25 而不是清零：这类文献往往仍是**主题相关**的，用户可能
# 就是想找它（比如"我库里那篇讲 X 的"）。降到看不见会让人以为库里没有；
# 压到 1/4 则正常提问时它沉在后面，明确点名时还能找到。
UNTRUSTED_PENALTY = 0.25


def serialized(fn):
    """把一次完整的数据库操作串行化。

    为什么需要（两轮踩坑后的结论）：
      1. MCP 每个 tools/call 可能在不同线程执行，连接必须允许跨线程
         （schemas.LockedConnection 关掉了线程检查）。
      2. 但**只允许跨线程还不够** —— `conn.execute()` 返回游标后若被别的线程
         插进来执行查询，随后 fetch 会拿到 None，报
         `TypeError: 'NoneType' object is not subscriptable` /
         `IndexError: tuple index out of range`，且只在并发下偶发。
      3. 想用"取完行才放锁"的游标代理来修 —— 不行，Python 3.14 的
         sqlite3.Cursor 属性只读，包装不上去。

    所以锁的粒度必须落在"一个完整操作"上：持锁期间把 execute 与 fetch 都做完。
    锁用 RLock，因为 search 内部会调用 search_fts / search_vector /
    related_experience 等同样被装饰的方法（可重入才不会自锁）。

    代价：同一连接上的检索变成串行。这些都是毫秒级操作，而一次对话里
    不会有几十个并发检索，所以换来的正确性远比那点并发度值钱。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class KnowledgeBaseMissing(RuntimeError):
    """索引库还没建好（或表结构不完整）。

    单独定义这个异常类型，是为了让上层能把它翻译成人话 ——
    直接抛 sqlite3 的 "no such table: meta" 对使用者毫无帮助。
    """


class Searcher:
    """一次构造，长期复用。连接与权重缓存都常驻。"""

    def __init__(self, db_path: str = S.INDEX_DB, model_name: str | None = None,
                 cache_dir: str | None = None):
        self.db_path = db_path
        if not os.path.exists(db_path):
            raise KnowledgeBaseMissing(
                f"知识库索引不存在：{db_path}\n"
                f"先构建一次：{os.path.join(ROOT, 'scripts', '1-convert.cmd')}\n"
                f"（或命令行 python offline/convert.py）"
            )
        self.conn = None
        try:
            self.conn = S.connect(db_path)
            # 表结构检查：文件在但表不全（或根本不是数据库），同样是"没建好"
            self.conn.execute("SELECT COUNT(*) FROM items").fetchone()
        except Exception as exc:  # noqa: BLE001
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:  # noqa: BLE001
                    pass
            raise KnowledgeBaseMissing(
                f"索引文件无法使用（{type(exc).__name__}: {exc}）\n"
                f"文件：{db_path}\n"
                f"如果它是损坏的，删掉后重新构建：\n"
                f"  1) 删除 {db_path}\n"
                f"  2) 跑 {os.path.join(ROOT, 'scripts', '1-convert.cmd')}"
            ) from exc
        self.cache_dir = cache_dir or os.path.join(S.CACHE_DIR, "models")
        self._model = None
        self._model_name = model_name or S.get_meta(self.conn, "embed_model", "") \
            or "intfloat/multilingual-e5-small"
        # 一把可重入锁，覆盖"执行 + 取行"的完整操作（见 serialized 装饰器）。
        # RLock：search 会调用 search_fts / search_vector / related_experience，
        # 它们同样被装饰，可重入才不会自锁。
        self._lock = threading.RLock()
        # 专门保护"懒加载状态"的锁：与 _lock 分开，避免慢加载长时间占住检索用的锁
        self._state_lock = threading.Lock()
        # 序列化 fastembed 调用：ONNX 会话不保证可重入，并发喂输入会出难以复现的错
        self._embed_lock = threading.Lock()
        self._weights: dict[str, dict] = {}
        self._years: dict[str, object] = {}   # key → 发表年份，算 recency base 用
        self._vectors = None      # (chunk_ids ndarray, matrix ndarray)
        # 懒加载交接用的事件（见 _ensure_model / _ensure_vectors 的说明）
        self._model_ready = threading.Event()
        self._vectors_ready = threading.Event()
        self._vec_model_used = S.get_meta(self.conn, "embed_model", "")
        # 词元稀有度表：中文单字检索的质量几乎全押在这上面
        self._idf: dict[str, float] = {}
        self._known_terms: set[str] = set()
        self._n_chunks = 0
        self._idf_default = 0.0
        self._load_idf()
        self.reload_weights()

    # ------------------------------------------------------------ 元信息

    @property
    def has_vectors(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
        return bool(row and row["n"])

    @serialized
    def n_items(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]

    @serialized
    def stats(self) -> dict:
        c = self.conn
        return {
            "items": c.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"],
            "chunks": c.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"],
            "vectors": c.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"],
            "embed_model": S.get_meta(c, "embed_model", "(未建向量)"),
            "built_at": S.get_meta(c, "built_at", "(从未构建)"),
            "experience_rows": c.execute("SELECT COUNT(*) AS n FROM experience").fetchone()["n"],
            "weighted_items": c.execute("SELECT COUNT(*) AS n FROM item_weight").fetchone()["n"],
            "vector_ready": self.has_vectors,
        }

    # ------------------------------------------------------------ 读入口

    @serialized
    def read(self, sql: str, params: tuple = ()) -> list:
        """执行读查询并**在锁内把行取完**，返回行列表。

        为什么不直接暴露 `self.conn.execute(...)` 给调用方：
        游标一旦跨出锁的范围，就可能被其他线程插进来的查询作废，
        随后 fetch 拿到 None（实测报 `TypeError: 'NoneType' object is not
        subscriptable`）。所以统一在这里一次取完（列表很小，没有流式需求）。

        调用方拿到的是普通 list，可以随便 fetch/切片/迭代，不再碰连接。
        """
        return self.conn.execute(sql, params).fetchall()

    @serialized
    def read_one(self, sql: str, params: tuple = ()):
        """读一行，没有就返回 None。"""
        return self.conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------ 写入口

    @serialized
    def write(self, sql: str, params: tuple = (), many: bool = False) -> int:
        """执行写操作并提交，整段串行化。

        **所有写都必须走这里**，不要在外面直接 `self.conn.execute(...)` ——
        那样绕过 `_lock`，并发下会报
        `InterfaceError: bad parameter or other API misuse` /
        `OperationalError: cannot start a transaction within a transaction`，
        并且**静默丢写**（实测 20 条并发只写进 13-17 条）。
        MCP 里 kb_experience_add / kb_weight_set 都可能被并发调用。

        返回 rowcount（仅供参考，插入 id 用 lastrowid 拿不到就在这里返回行数）。
        """
        if many:
            cur = self.conn.executemany(sql, params)
        else:
            cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    @serialized
    def write_returning_id(self, sql: str, params: tuple = ()) -> int:
        """需要 INSERT 自增 id 时用这个（同样整段串行化）。"""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # ------------------------------------------------------------ 权重缓存

    @serialized
    def reload_weights(self) -> None:
        """把 item_weight 读进内存。经验写入后必须调用这个刷新。

        为什么不每次查库：检索是热路径，110 条的库虽小，但索引一多
        （几千切片）时每轮多一次查询不划算，缓存下来最省事。

        并发说明：整体替换 `self._weights` 是原子操作，读方拿到旧字典或新字典
        都能正常工作，所以这里不必与检索抢锁。
        """
        self._weights = {
            row["item_key"]: dict(row)
            for row in self.conn.execute("SELECT * FROM item_weight")
        }
        # 年份一并缓存：基础权重（recency base）按发表年份算，而库里
        # 绝大多数文献在 item_weight 里根本没有行 —— 只靠 _weights 会让
        # 它们全部退化成 1.0，正是"基础权重全为 1"的老问题。
        self._years: dict[str, object] = {
            row["key"]: row["year"]
            for row in self.conn.execute("SELECT key, year FROM items")
        }
        # 正文提取不可信的文献（`item_health.verdict='bad'`）—— 整篇降权。
        # 表不存在（没跑过体检）时当空集，不影响检索。
        try:
            self._untrusted: set[str] = {
                row["item_key"] for row in self.conn.execute(
                    "SELECT item_key FROM item_health WHERE verdict='bad'")
            }
        except Exception:                                     # noqa: BLE001
            self._untrusted = set()

    @serialized
    def weight_of(self, key: str) -> float:
        w = S.weight_multiplier(
            self._weights.get(key), S.recency_base(self._years.get(key))
        )
        if key in self._untrusted:
            w *= UNTRUSTED_PENALTY
        return w

    # ------------------------------------------------------------ 词元稀有度

    def _load_idf(self) -> None:
        """载入各词元的逆文档频率。罕见字权重高，通用字权重低。"""
        import math

        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] or 1
            self._n_chunks = total
            # 语料里根本没有的词元：FTS5 一定匹配不到，直接排除，省一次 OR 项
            self._known_terms: set[str] = set()
            self._idf: dict[str, float] = {}
            for row in self.conn.execute("SELECT term, df FROM term_df"):
                term, df = row["term"], max(row["df"], 1)
                self._known_terms.add(term)
                self._idf[term] = max(0.0, math.log(total / df))
            self._idf_default = math.log(total)

    def term_idf(self, term: str) -> float:
        return self._idf.get(term, self._idf_default)

    # ------------------------------------------------------------ 关键词路

    @serialized
    def search_fts(self, query: str, limit: int = 200) -> list[dict]:
        """关键词检索：**短语召回** + 加权覆盖率打分。

        召回用「中文连续段作为短语」而不是单字 OR：
        「机器学习模型」要求这些字在正文里**相邻**出现，
        只在真的讲这件事的地方命中 —— 单字 OR 会把"反对""演进""特征分布"
        这些只含单个字的段落一并捞进来（实测：把「无关领域无损检测」顶到第 2）。

        打分用**加权的子串覆盖率**：命中「反演」比命中孤立的「反」值钱得多
        （权重 = 子串长度²），所以真正连续写出这个词的块会排到最前。

        为什么不用 bm25 排序：bm25 在中文单字上区分度差，长文档还天然劣势。
        """
        # 先确认查询里至少有一个子串在语料中出现过，否则直接返回空
        units = coverage_units(query)
        if not units:
            return []
        if not any(gram in self._known_terms or len(gram) >= 2 for gram, _ in units):
            # 全部单字都不在语料里 —— 比发一次注定为空的查询诚实
            if not any(gram in self._known_terms for gram, _ in units):
                return []

        rows = self._recall(query, limit)
        if not rows:
            return []

        chunk_ids = [r["chunk_id"] for r in rows]
        ph = ",".join("?" * len(chunk_ids))
        texts = {
            row["chunk_id"]: row["text"]
            for row in self.conn.execute(
                f"SELECT chunk_id, text FROM chunks WHERE chunk_id IN ({ph})", chunk_ids
            )
        }

        total_weight = sum(weight for _, weight in units) or 1.0
        hits: list[dict] = []
        for row in rows:
            chunk_id = row["chunk_id"]
            body = texts.get(chunk_id, "")
            if not body:
                continue
            matched = [(gram, weight) for gram, weight in units if gram in body]
            if not matched:
                continue
            gained = sum(weight for _, weight in matched)
            # 只认长度 ≥2 的连续命中为"强证据"
            strong = sum(1 for gram, _ in matched if len(gram) >= 2)
            hits.append({
                "chunk_id": chunk_id,
                "covered": len(matched),
                "strong": strong,
                "coverage": gained / total_weight,   # 0-1，长匹配权重高
                "idf_coverage": gained / total_weight,
                "bm25": row["bm25"],
            })

        # 主排序：加权覆盖率（连续长匹配优先），再看强证据数，最后看 bm25
        hits.sort(key=lambda h: (-h["coverage"], -h["strong"], h["bm25"]))
        for rank, hit in enumerate(hits, start=1):
            hit["rank"] = rank
        return hits[:limit]

    def _recall(self, query: str, limit: int) -> list:
        """召回：先短语（相邻匹配），命中太少再退到单字宽召回。"""
        sql = """
            SELECT f.rowid AS chunk_id, bm25(chunks_fts) AS bm25
            FROM chunks_fts f
            WHERE chunks_fts MATCH ?
            ORDER BY bm25
            LIMIT ?
        """
        expr = build_match_phrases(query)
        if expr:
            try:
                rows = self.conn.execute(sql, (expr, limit * 3)).fetchall()
            except Exception:  # noqa: BLE001
                rows = []
            if len(rows) >= MIN_PHRASE_HITS:
                return rows

        # 退一步：单字 OR 宽召回（覆盖"换了个说法"的表述）
        wide = build_match(query, mode="or")
        if not wide:
            return rows if expr else []
        try:
            wide_rows = self.conn.execute(sql, (wide, limit * 6)).fetchall()
        except Exception:  # noqa: BLE001
            return rows if expr else []
        # 合并两层结果：短语层（更精）排在前面，宽召回补充在后面。
        # 顺序很重要 —— RRF 按排名给分，先出现的层拿到的名次更好。
        seen = {r["chunk_id"] for r in rows}
        merged = list(rows) + [r for r in wide_rows if r["chunk_id"] not in seen]
        return merged

    # ------------------------------------------------------------ 向量路

    def _ensure_model(self):
        """懒加载嵌入模型 —— 只有真的用到向量时才付这份启动成本。

        并发安全（这里踩了两轮坑）：
          1. 用 "if self._model is None" 当判据不行 —— 加载过程中它一直是 None，
             第二个线程会**重复加载**，两个 TextEmbedding 争抢同一个 ONNX 会话，
             报出 `'NoneType' object is not subscriptable` /
             `tuple index out of range` 这类难复现的错。
          2. 光靠状态标记也不行 —— 把状态设成"加载中"后，其余线程如果只是
             "再检查一次状态"就会掉进同一个加载分支（实测仍 2/5 概率出错）。
             必须让它们**等第一个线程加载完**。

        所以这里用 Event 做交接：拿到加载权的线程干活并 set 事件，
        其余线程 wait 后直接取结果。状态锁只保护状态，慢加载在锁外做。
        """
        mine = False
        with self._state_lock:
            state = self._model
            if state is True:            # 之前加载失败过，不再重试
                return False
            if state is not None and state is not False:
                return state             # 已就绪
            if state is False:           # 别人正在加载 —— 等它
                event = self._model_ready
            else:                        # 加载权归我
                self._model = False
                self._model_ready = threading.Event()
                event = self._model_ready
                mine = True

        if not mine:
            event.wait(timeout=300)
            with self._state_lock:
                state = self._model
                return state if state not in (False, True, None) else False

        try:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from fastembed import TextEmbedding
            model = TextEmbedding(model_name=self._model_name,
                                  cache_dir=self.cache_dir)
            with self._state_lock:
                self._model = model
        except Exception as exc:  # noqa: BLE001
            print(f"[searcher] 向量模型不可用：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            with self._state_lock:
                self._model = True
            model = False
        finally:
            event.set()
        return model

    def _ensure_vectors(self):
        """把全部向量读进内存一次（几千块，几 MB，很快）。

        与模型同样的 Event 交接：只让一个线程读 BLOB 建矩阵，
        其余等它完成 —— 否则会各建一份矩阵，白占内存还可能读到半截。
        """
        mine = False
        with self._state_lock:
            state = self._vectors
            if state is True:            # 查过了，没有向量
                return False
            if state is not None and state is not False:
                return state
            if state is False:
                event = self._vectors_ready
            else:
                self._vectors = False
                self._vectors_ready = threading.Event()
                event = self._vectors_ready
                mine = True

        if not mine:
            event.wait(timeout=300)
            with self._state_lock:
                state = self._vectors
                return state if state not in (False, True, None) else False

        import numpy as np

        try:
            rows = self.conn.execute(
                "SELECT chunk_id, dim, vector FROM embeddings"
            ).fetchall()
            if not rows:
                with self._state_lock:
                    self._vectors = True
                return False
            ids = np.array([r["chunk_id"] for r in rows], dtype="int64")
            dim = rows[0]["dim"]
            mat = np.frombuffer(b"".join(r["vector"] for r in rows),
                                dtype="float32").reshape(len(rows), dim)
            # 预先归一化：查询时只需一次点积就是余弦相似度
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            result = (ids, mat / norms)
        except Exception as exc:  # noqa: BLE001
            print(f"[searcher] 载入向量失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            with self._state_lock:
                self._vectors = True
            result = False
        else:
            with self._state_lock:
                self._vectors = result
        finally:
            event.set()
        return result

    @serialized
    def search_vector(self, query: str, limit: int = CANDIDATES) -> list[dict]:
        model = self._ensure_model()
        if not model:
            return []
        vecs = self._ensure_vectors()
        if not vecs:
            return []
        ids, mat = vecs
        import numpy as np

        try:
            with self._embed_lock:      # ONNX 会话不保证可重入，串起来喂
                q = np.asarray(list(model.query_embed([query]))[0], dtype="float32")
        except Exception as exc:  # noqa: BLE001
            print(f"[searcher] 查询向量失败：{exc}", file=sys.stderr)
            return []
        norm = float(np.linalg.norm(q)) or 1.0
        sims = mat @ (q / norm)
        top = np.argsort(-sims)[:limit]
        # similarity 必须非负：融合时它当质量分用，负数会被当成"负相关证据"
        return [{"chunk_id": int(ids[i]), "rank": rank + 1,
                 "similarity": float(max(0.0, sims[i])), "raw": float(sims[i])}
                for rank, i in enumerate(top)]

    # ------------------------------------------------------------ 融合

    def _rrf(self, *rankings: list[dict], quality_scale: float = 1.0) -> dict[int, float]:
        """按**证据强度**加权的 RRF 融合。

        纯 RRF（只数排名）会犯一个错：一篇文献命中 5 个弱相关块，
        能靠数量压过只命中 3 个强相关块的文献 —— 实测「机器学习模型」时
        「无关领域无损检测」（5 个沾边块）就是这样把
        「统计学习方法」（3 个强相关块）顶下去的。

        所以这里乘两个因子：
          1. 该路的**质量分** quality —— 关键词路用 IDF 加权覆盖率，向量路用余弦相似度。
          2. 一路里的**位置衰减** 1/(1+0.35*pos) —— 同一篇的第 4、5 个块
             只算增量证据，不该和第 2 个块一样值钱。

        质量分先做**组内归一化**（见 `_normalize_quality`），因为两路的
        原始尺度完全不同：向量余弦挤在 0.63-0.66（区分度 5%），
        关键词覆盖率却能差 6 倍。不归一化就会出现"低区分度的一路
        因为绝对值大而主导排序"。实测过：不归一化时「机器学习模型」
        的第一名会被向量路带偏成《基于统计学习的信号分离方法》。
        """
        normalized = self._normalize_quality(rankings)
        scores: dict[int, float] = {}
        for ranking, qualities in zip(rankings, normalized):
            for pos, entry in enumerate(ranking):
                quality = qualities.get(entry["chunk_id"], 0.0)
                decay = 1.0 / (1.0 + 0.35 * pos)
                scores[entry["chunk_id"]] = scores.get(entry["chunk_id"], 0.0) + \
                    quality_scale * quality * decay / (RRF_K + entry["rank"])
        return scores

    @staticmethod
    def _normalize_quality(rankings: list[list[dict]]) -> list[dict[int, float]]:
        """把每一路的质量分各自拉满到 0-1，消除两路尺度差异。

        组内 min-max：该路最好的一块 = 1.0，最差的一块 = 0.0。
        这样两路的"相对好坏"才有可比性，融合时比的是
        "这一块在它那一路里有多突出"，而不是两套没校准过的分数。
        """
        out: list[dict[int, float]] = []
        for ranking in rankings:
            if not ranking:
                out.append({})
                continue
            raw = {
                entry["chunk_id"]: float(
                    entry.get("idf_coverage", entry.get("similarity", 0.0))
                )
                for entry in ranking
            }
            lo = min(raw.values())
            hi = max(raw.values())
            span = hi - lo
            if span <= 1e-9:
                # 全路同分（向量路很常见）：都给 1.0，退化成纯 RRF
                out.append({cid: 1.0 for cid in raw})
            else:
                out.append({cid: (value - lo) / span for cid, value in raw.items()})
        return out

    def _chunk_rows(self, chunk_ids: list[int]) -> dict[int, dict]:
        if not chunk_ids:
            return {}
        ph = ",".join("?" * len(chunk_ids))
        out: dict[int, dict] = {}
        for row in self.conn.execute(
            f"""
            SELECT c.chunk_id, c.item_key, c.page, c.seq, c.text,
                   i.title, i.year, i.first_author, i.item_type, i.collections
            FROM chunks c JOIN items i ON i.key = c.item_key
            WHERE c.chunk_id IN ({ph})
            """,
            chunk_ids,
        ):
            out[row["chunk_id"]] = dict(row)
        return out

    # ------------------------------------------------------------ 结构化字段
    #
    # `item_extra`（知网写入的结构化字段：中图分类号 / 来源库 / 收录标签 /
    # 中文译名 / 学位专业…）由 `offline/extrafill.py` 建表并维护，
    # `offline/convert.py` 每次构建时写它。这里只**读**。
    #
    # 为什么读它要不厌其烦地容错（表不存在就返回空）：这张表是**后加的**，
    # 用户可能还没跑过新版构建；而检索是主链路 —— 附加信息读不到时，
    # 检索必须照常工作，绝不能因此报错或返回空结果。

    @serialized
    def extras_for_keys(self, keys: list[str]) -> dict[str, dict]:
        """批量取若干条目的结构化字段行（没有的条目不在返回里）。"""
        if not keys:
            return {}
        ph = ",".join("?" * len(keys))
        try:
            rows = self.conn.execute(
                f"SELECT * FROM item_extra WHERE item_key IN ({ph})", list(keys)
            ).fetchall()
        except Exception:  # noqa: BLE001 —— 表还没建过（没跑过新版构建）
            return {}
        return {r["item_key"]: dict(r) for r in rows}

    @serialized
    def extras_for_key(self, key: str) -> dict:
        """单条读结构化字段（表不存在 / 没这一行 → 空字典）。

        为什么不直接把这个查询写在工具层：`Searcher` 的连接有一把锁，
        跨线程的游标会互相作废（见 `serialized` 的说明）。所以**所有**读都要
        走这里带锁的方法，不要在 server.py 里拿 `s.conn` 自己 execute。
        """
        return self.extras_for_keys([key]).get(key, {})

    @serialized
    def _keys_matching_facets(self, facet: dict) -> tuple[set[str], str]:
        """按结构化字段筛出符合条件的 item_key；返回 `(key 集合, 错误说明)`。

        什么时候用：用户/模型明确说了"只要 EI 收录的""只要学位论文"
        "找分类号 TP391 的"——这些信息**只在 item_extra 里**，
        正文切片里根本没有（分类号、收录标签不会印在 PDF 正文里）。

        筛不了的时候返回**空集合 + 原因**，而不是"全部通过"：
        对方明确要求"只要学位论文"，而结构化字段读不到时把全部结果还给他，
        等于**假装**筛过了 —— 那比诚实的空结果更糟。
        """
        try:
            rows = self.conn.execute(
                "SELECT item_key, clc, source_kind, publication_tag FROM item_extra"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            return set(), (f"结构化字段表不可读（{type(exc).__name__}: {exc}）；"
                           f"先跑一次构建（scripts\\1-convert.cmd）或 "
                           f"python offline/extrafill.py --sync")
        kind = (facet.get("source_kind") or "").strip().lower()
        if kind:
            # 允许直接用中文说（"学位论文"）—— 人/模型更可能这么讲。
            # 别名表从 extrafill 反查，不在这里另抄一份（抄了就会漂移）。
            kind = {label.lower(): code
                    for code, label in XF.KIND_LABELS.items()}.get(kind, kind)
        # 收录标签是"逗号分隔的多个值"，用子串即可（`EI` 不该命中 `EIA`？
        # 实测本库的标签都是完整词，但为了稳，匹配按**列表成员**比，而不是裸子串）
        tag = (facet.get("publication_tag") or "").strip().lower()
        codes = [c.strip().upper()
                 for c in re.split(r"[;；,，\s]+", facet.get("clc") or "")
                 if c.strip()]
        out: set[str] = set()
        for r in rows:
            if kind and (r["source_kind"] or "").strip().lower() != kind:
                continue
            if tag:
                members = [m.strip().lower()
                           for m in XF.split_list(r["publication_tag"] or "", r"[,，]")]
                if tag not in members:
                    continue
            if codes:
                members = [m.upper() for m in XF.clc_list(r["clc"] or "")]
                # 分类号允许**前缀**匹配：中图分类号是层级码，`TP39` 应当能
                # 筛出 `TP391.9`（用户经常只记得大类）。
                if not all(any(m.startswith(c) for m in members) for c in codes):
                    continue
            out.add(r["item_key"])
        return out, ""

    @serialized
    def extra_stats(self) -> dict:
        """结构化字段的覆盖情况（`kb_stats` 用）：让"有没有这些数据"一眼可见。"""
        try:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COUNT(NULLIF(clc, '')) AS n_clc,
                       COUNT(NULLIF(title_translation, '')) AS n_zh,
                       COUNT(NULLIF(publication_tag, '')) AS n_tag,
                       COUNT(NULLIF(orig_container_title, '')) AS n_orig
                FROM item_extra
                """
            ).fetchone()
            kinds = {r["k"] or "(未知)": r["n"] for r in self.conn.execute(
                "SELECT COALESCE(NULLIF(source_kind, ''), '(未知)') AS k,"
                " COUNT(*) AS n FROM item_extra GROUP BY 1")}
        except Exception:  # noqa: BLE001
            return {"available": False,
                    "hint": "还没有结构化字段：跑一次构建，或 "
                            "python offline/extrafill.py --sync"}
        return {
            "available": True,
            "items": row["n"],
            "with_clc": row["n_clc"],
            "with_title_translation": row["n_zh"],
            "with_publication_tag": row["n_tag"],
            "with_orig_container_title": row["n_orig"],
            "by_source_kind": kinds,
            "how_to_query": "kb_search 可传 source_kind / publication_tag / clc 筛选",
        }

    @serialized
    def search(self, query: str, limit: int = 8, collections: list[str] | None = None,
               year_from: int | None = None, year_to: int | None = None,
               per_item_chunks: int = 3, max_scoring_chunks: int = 3,
               snippets_per_item: int = 2,
               source_kind: str = "", publication_tag: str = "",
               clc: str = "") -> dict:
        """主检索入口。返回 {query, hits, diagnostics}。

        一个关键取舍：**每个条目最多只用 3 个块参与打分**。
        否则块多的文献会凭空占便宜 —— 实测「机器学习模型」时，
        无关领域无损检测（命中 6 个弱相关块）会靠数量压过
        统计学习方法（命中 3 个强相关块）。相关的是"最好的一块有多好"，
        不是"有多少块沾边"。

        向量路仍保留其各自的排名；两路都命中同一条目只会加权一次。
        """
        query = (query or "").strip()
        diag = {"fts_candidates": 0, "vector_candidates": 0, "after_filter": 0,
                "vector_used": False}
        if not query:
            return {"query": query, "hits": [], "diagnostics": diag}

        fts = self.search_fts(query, limit=CANDIDATES)
        diag["fts_candidates"] = len(fts)
        vec = self.search_vector(query, limit=CANDIDATES)
        diag["vector_candidates"] = len(vec)
        diag["vector_used"] = bool(vec)

        # 两路的可信度不同，给不同权重：关键词路要求查询词真的出现在文里，
        # 精度高；向量路负责"换了个说法"的召回，但它在短查询上的相似度
        # 分布很挤（实测 0.63-0.66），单独靠它排序不稳。
        # 所以关键词路拿 0.65、向量路拿 0.35 —— 关键词命中永远是主证据。
        fused = self._rrf(fts, quality_scale=KEYWORD_ROUTE_WEIGHT)
        for chunk_id, score in self._rrf(
                vec, quality_scale=VECTOR_ROUTE_WEIGHT).items():
            fused[chunk_id] = fused.get(chunk_id, 0.0) + score
        if not fused:
            return {"query": query, "hits": [], "diagnostics": diag}

        rows = self._chunk_rows(list(fused.keys()))

        # 按条目聚合：块按融合分从高到低排，只让前 max_scoring_chunks 个块贡献分数
        by_item: dict[str, dict] = {}
        for chunk_id, rrf_score in fused.items():
            row = rows.get(chunk_id)
            if not row:
                continue
            key = row["item_key"]
            item = by_item.setdefault(key, {
                "key": key,
                "title": row["title"],
                "year": row["year"],
                "first_author": row["first_author"],
                "item_type": row["item_type"],
                "collections": json.loads(row["collections"] or "[]"),
                "score": 0.0,
                "snippets": [],
                "_chunks": [],
            })
            item["_chunks"].append((rrf_score, row))

        for item in by_item.values():
            item["_chunks"].sort(key=lambda pair: -pair[0])
            # 只累加最好的几块：块多不再等于更相关
            best = item["_chunks"][:max_scoring_chunks]
            item["score"] = sum(score for score, _ in best)
            # 诊断用：把参与打分的块排名留下来，便于核对排序是否合理
            item["_scored"] = [(round(s, 5), row["chunk_id"], row["page"]) for s, row in best]
            for _, row in item["_chunks"][:per_item_chunks]:
                item["snippets"].append({
                    "page": row["page"],
                    "text": self._snippet(row["text"], query),
                })

        # 过滤
        hits = []
        for item in by_item.values():
            if collections and not (set(collections) & set(item["collections"])):
                continue
            if year_from or year_to:
                try:
                    year = int(item["year"])
                except (TypeError, ValueError):
                    year = 0
                if year_from and year < year_from:
                    continue
                if year_to and year > year_to:
                    continue
            hits.append(item)
        diag["after_filter"] = len(hits)

        # ---- 结构化字段过滤（**只有显式传参时才生效**）
        #
        # 为什么做成"筛掉"而不是"加权"：加权动的是**排序**，而排序目前只有
        # 零星的人工核对（没有回归集），改坏了不会当场暴露；过滤只在明确说
        # "只要网络首发的/只要 EI 的"时候改变结果集，不传参数时连一次 SQL
        # 都不多跑 —— 对既有检索行为**零影响**（这是本次接线的最低风险形态）。
        facet = {k: v.strip() for k, v in (("source_kind", source_kind),
                                           ("publication_tag", publication_tag),
                                           ("clc", clc)) if (v or "").strip()}
        if facet:
            keep, err = self._keys_matching_facets(facet)
            diag["facet_filter"] = facet
            if err:
                diag["facet_error"] = err
                hits = []            # 见 _keys_matching_facets：宁空不假装
            else:
                diag["facet_matched_items"] = len(keep)
                hits = [h for h in hits if h["key"] in keep]

        # 经验权重：乘在融合分上，只影响排序
        for item in hits:
            item["weight"] = round(self.weight_of(item["key"]), 3)
            item["score"] = round(item["score"] * item["weight"], 6)

        hits.sort(key=lambda x: -x["score"])
        for item in hits:
            item.pop("_chunks", None)
            item["snippets"] = item["snippets"][:snippets_per_item]
            # 可读引用名：`作者 年份 · 短标题`。
            # 为什么给**每个**命中都带上：item key 是 8 位大写字母数字
            # （22X9PMR6），唯一但认不出是哪篇 —— 模型和用户在结果里
            # 都需要"人话"。注意它**不唯一**（库里有 3 篇同名），
            # 所以 key 必须一起返回，不能拿 label 当标识用。
            item["ref"] = S.human_ref(item["title"], item["first_author"],
                                      item["year"])
            item["label"] = S.human_label(item["title"], item["first_author"],
                                          item["year"], item["key"])

        # ---- 每条命中带上"结构化字段摘要"（**有才带**，一次批量查询）
        #
        # 为什么放在最后、只查返回的那几条：结构化字段是**附加**信息，
        # 只有真的要给出去的那几条才值得读；而且它在结果里的作用只是
        # "让人一眼看出这篇是不是我要的"（学位论文？EI 收录？有中文译名？）。
        # 口径交给 extrafill.describe —— 展示层各处（kb_item / tldr）共用一份，
        # 不在这里另拼一套。
        page = hits[:limit]
        extras = self.extras_for_keys([h["key"] for h in page])
        for item in page:
            row = extras.get(item["key"])
            if row:
                view = XF.describe(row, compact=True)
                if view:
                    item["structured"] = view
        return {"query": query, "hits": page, "diagnostics": diag,
                "scored_chunks": {h["key"]: h.pop("_scored", []) for h in page}}

    # ------------------------------------------------------------ 片段

    @staticmethod
    def _snippet(text: str, query: str, width: int = 220) -> str:
        """在块里定位关键词，截一段带上下文的文字。"""
        words = [k for k in keywords(query) if len(k) >= 1]
        best = -1
        for word in words:
            pos = text.find(word)
            if pos != -1 and (best == -1 or pos < best):
                best = pos
        if best == -1:
            snippet = text[:width]
        else:
            start = max(0, best - width // 3)
            snippet = text[start:start + width]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        return ("…" if best > width // 3 else "") + snippet + "…"

    # ------------------------------------------------------------ 经验

    @serialized
    def related_experience(self, query: str, item_keys: list[str] | None = None,
                           limit: int = 5) -> list[dict]:
        """找与本次查询或这些文献相关的经验。

        ⚠ 关键点：中文查询必须用**多字子串**去匹配，不能用单字。
        踩过的坑：这里原先只取长度 ≥2 的词元，而中文经 tokenize 后全是单字，
        过滤后 words 为空 → SQL 变成空条件 → **永远返回 0 条**，
        表现就是"刚记的经验查不到"。现在改用 coverage_units（2-4 字滑窗子串），
        既能命中"非线性最小二乘"这类短语，也保留"初值"这种两字词。

        匹配策略是"能捞到就不漏"：任一子串出现在 asked/method/tags/reason
        任一处即命中；涉及到的文献 key 也直接命中。经验总量本来就不大
        （几百条量级），宁多勿少，交给模型自己判断相关性。
        """
        from query import coverage_units

        # 只取长度 ≥2 的子串：单字命中面太宽，会把不相关经验全捞进来
        words = [gram for gram, _w in coverage_units(query) if len(gram) >= 2][:8]
        clauses = []
        params: list = []
        for word in words:
            like = f"%{word}%"
            clauses.append("(asked LIKE ? OR method LIKE ? OR tags LIKE ? OR reason LIKE ?)")
            params.extend([like, like, like, like])
        for key in (item_keys or [])[:10]:
            clauses.append("item_keys LIKE ?")
            params.append(f"%{key}%")
        if not clauses:
            return []
        sql = f"""
            SELECT id, created_at, asked, context, method, outcome, reason,
                   evidence, item_keys, tags, source
            FROM experience WHERE {" OR ".join(clauses)}
            ORDER BY created_at DESC LIMIT ?
        """
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    @serialized
    def experience_for_item(self, item_key: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            """
            SELECT id, created_at, asked, context, method, outcome, reason,
                   evidence, item_keys, tags, source
            FROM experience WHERE item_keys LIKE ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (f"%{item_key}%", limit),
        )]

    @serialized
    def get_item(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM items WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["tags"] = json.loads(item["tags"] or "[]")
        item["collections"] = json.loads(item["collections"] or "[]")
        item["weight"] = round(self.weight_of(key), 3)
        return item

    @serialized
    def collections(self) -> list[dict]:
        """分类清单：从条目的 collections 字段聚合（比读 Zotero 更省事）。"""
        counts: dict[str, int] = {}
        for row in self.conn.execute("SELECT collections FROM items"):
            for name in json.loads(row["collections"] or "[]"):
                counts[name] = counts.get(name, 0) + 1
        return [{"name": k, "items": v} for k, v in sorted(counts.items(),
                                                          key=lambda x: -x[1])]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
