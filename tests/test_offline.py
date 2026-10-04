# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""离线管道的纯逻辑单测：切片、HTML 转换、查询改写、权重公式。

    python tests/test_offline.py

不需要 Zotero 在线，也不需要向量模型 —— 这些函数都是纯的。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import schemas as S  # noqa: E402
import converter as C  # noqa: E402
from zreader import clean_text, html_to_markdown, split_pages  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------- clean_text


def test_clean_text():
    print("\n[clean_text] 文本清洗")
    check("去掉行尾连字符断行",
          clean_text("research-\ners are great") == "researchers are great",
          repr(clean_text("research-\ners are great")))
    check("去掉纯页码行",
          "12" not in clean_text("正文开始\n 12 \n正文结束").split("\n"),
          repr(clean_text("正文开始\n12\n正文结束")))
    check("压缩多余空行",
          "\n\n\n" not in clean_text("a\n\n\n\n\nb"))
    check("空输入返回空", clean_text("") == "")
    # 中文 PDF 常见「航 天 器」式字间空格：既影响可读性，也会打断"气候变化"这类
    # 连续短语的语义，所以要清掉；但英文单词间的空格必须保留
    check("去掉汉字之间的空格（含全角空格）",
          clean_text("磁\u00a0场 强 度") == "磁场强度",
          repr(clean_text("磁\u00a0场 强 度")))
    check("保留英文单词间的空格",
          clean_text("magnetic field dipole") == "magnetic field dipole",
          repr(clean_text("magnetic field dipole")))
    check("中英混排时只清中文侧",
          clean_text("磁 场 magnetic") == "磁场 magnetic",
          repr(clean_text("磁 场 magnetic")))


# ---------------------------------------------------------------- html_to_markdown


def test_html_to_markdown():
    print("\n[html_to_markdown] 笔记 HTML → Markdown")
    raw = ('<div class="zotero-note znv1"><div data-schema-version="9">'
           "<p><strong>摘要</strong>：气候变化归因</p>"
           '<p><img alt="" data-attachment-key="SI2DXAGA" width="1405"></p>'
           "<p><em>重点</em>与<code>x=1</code></p>"
           "<ul><li>第一条</li><li>第二条</li></ul>"
           '<p><a href="https://x.org">链接</a></p>'
           "</div></div>")
    md, n_img = html_to_markdown(raw)
    check("粗体转换", "**摘要**" in md, md[:120])
    check("斜体转换", "*重点*" in md)
    check("行内代码转换", "`x=1`" in md)
    check("列表转换", "- 第一条" in md and "- 第二条" in md)
    check("链接转换", "[链接](https://x.org)" in md)
    check("统计到 1 张内嵌截图", n_img == 1, str(n_img))
    check("截图只留说明不搬二进制",
          "内嵌截图" in md and "SI2DXAGA" in md)
    check("没有残留 HTML 标签", "<" not in md.replace("<", "", 0) or "<p>" not in md,
          md[:200])
    check("HTML 实体解码", html_to_markdown("<p>a &amp; b</p>")[0] == "a & b")
    check("空输入", html_to_markdown("") == ("", 0))


# ---------------------------------------------------------------- chunk_text


def test_chunk_text():
    print("\n[chunk_text] 切片")
    long_text = "\n\n".join(
        f"第{i}段。" + "气候变化归因方法研究。" * 20 for i in range(8)
    )
    chunks = C.chunk_text(long_text)
    check("长文本被切成多块", len(chunks) > 1, str(len(chunks)))
    check("每块不小于下限", all(len(c) >= C.CHUNK_MIN for c in chunks))
    check("多数块接近目标长度",
          sum(1 for c in chunks if len(c) <= C.CHUNK_TARGET * 1.6) == len(chunks),
          str([len(c) for c in chunks]))
    check("短文本不切", C.chunk_text("太短") == [])
    check("空文本不切", C.chunk_text("") == [])

    # 超长单段（没有空行）必须被按句子切开，不能产出巨块
    one_para = "这是一个很长的句子。" * 300
    chunks2 = C.chunk_text(one_para)
    check("超长单段也能切开", len(chunks2) > 1, str(len(chunks2)))
    check("超长单段的块不会远超目标",
          max(len(c) for c in chunks2) < C.CHUNK_TARGET * 2.5,
          str(max(len(c) for c in chunks2)))

    # 重叠：保证跨块不丢语义
    check("块之间带重叠",
          any(chunks[i][-40:] in chunks[i + 1] or chunks[i + 1][:40] in chunks[i]
              for i in range(len(chunks) - 1)) or len(chunks) == 1)


# ---------------------------------------------------------------- split_pages


def test_split_pages():
    print("\n[split_pages] 页码拆分")
    ft = "## p.1\n\n第一页内容\n\n## p.2\n\n第二页内容\n\n## p.3\n\n第三页"
    pages = split_pages(ft)
    check("拆出 3 页", len(pages) == 3, str(pages))
    check("页内容正确", pages[0] == "第一页内容" and pages[1] == "第二页内容",
          str(pages))
    check("无标记时返回空", split_pages("没有页码标记") == [])


# ---------------------------------------------------------------- 权重公式


def test_weight():
    print("\n[权重公式] 经验加权")
    none = None
    check("无经验时乘数为 1（不影响排序）",
          abs(S.weight_multiplier(none) - 1.0) < 1e-9,
          str(S.weight_multiplier(none)))

    row_eff = {"pinned": 0, "manual": 0, "effective": 1,
               "ineffective": 0, "partial": 0}
    row_ineff = {"pinned": 0, "manual": 0, "effective": 0,
                 "ineffective": 1, "partial": 0}
    row_pin = {"pinned": 1, "manual": 0, "effective": 0,
               "ineffective": 0, "partial": 0}
    check("有效 1 次 > 无经验", S.weight_multiplier(row_eff) > 1.0)
    check("重点标记 > 有效 1 次",
          S.weight_multiplier(row_pin) > S.weight_multiplier(row_eff),
          f"{S.weight_multiplier(row_pin):.2f} vs {S.weight_multiplier(row_eff):.2f}")
    check("失败也算正分（有价值），但小于有效",
          1.0 < S.weight_multiplier(row_ineff) < S.weight_multiplier(row_eff),
          f"{S.weight_multiplier(row_ineff):.2f}")

    row3 = {"pinned": 0, "manual": 0, "effective": 3,
            "ineffective": 0, "partial": 0}
    row1 = dict(row_eff)
    check("增长是递减的（抗垄断）",
          (S.weight_multiplier(row3) - 1) < 3 * (S.weight_multiplier(row1) - 1),
          f"3次={S.weight_multiplier(row3):.2f} 1次={S.weight_multiplier(row1):.2f}")
    check("权重上限温和（不会碾压其他结果）",
          S.weight_multiplier(row3) < 3.0,
          f"{S.weight_multiplier(row3):.2f}")


def test_recency_weight():
    """按发表年份的基础权重（recency base）。

    这一组的存在意义：库里 101 篇文献只有 8 篇在 item_weight 里有行，
    其余 93 篇权重原本被硬编码成 1.0 —— 新旧文献排出来一模一样。
    """
    print("\n[权重公式] 按发表年份的基础权重")
    NOW = 2026
    base = lambda y: S.recency_base(y, now_year=NOW)  # noqa: E731

    check("今年 = 上限", base(2026) == S.RECENCY_HI, str(base(2026)))
    check("未来年份也夹在上限", base(2030) == S.RECENCY_HI)
    check("跨度年数前 = 下限", base(2026 - S.RECENCY_SPAN_YEARS) == S.RECENCY_LO,
          str(base(2026 - S.RECENCY_SPAN_YEARS)))
    check("更老也夹在下限", base(1990) == S.RECENCY_LO)
    mid_y = round(2026 - S.RECENCY_SPAN_YEARS / 2)
    check("半程处 ≈ 1.0（跨度是奇数年，取近似）",
          abs(S.recency_base(mid_y, now_year=NOW) - 1.0) <= 0.01,
          f"{mid_y} → {S.recency_base(mid_y, now_year=NOW):.4f}")
    check("全部落在 0.95~1.05",
          all(S.RECENCY_LO <= base(y) <= S.RECENCY_HI for y in range(1950, 2040)))
    check("越新越大（单调）",
          all(base(y) <= base(y + 1)
              for y in range(2010, 2027)))

    check("缺年份 → 中性 1.0（不猜不罚）", S.recency_base("") == 1.0)
    check("None → 中性 1.0", S.recency_base(None) == 1.0)
    check("乱码 → 中性 1.0", S.recency_base("abc") == 1.0)
    # 注意：这里必须用一个"确实已到下限"的年份，否则改跨度常量时断言会悄悄失效
    oldest = 2026 - S.RECENCY_SPAN_YEARS
    check("字符串年份可解析", S.recency_base(str(oldest), now_year=NOW) == S.RECENCY_LO)
    check("带月份也能解析",
          S.recency_base(f"{oldest}-05-01", now_year=NOW) == S.RECENCY_LO)

    none = None
    check("无经验行也拿得到 base（核心修复）",
          abs(S.raw_weight(none, 1.05) - 1.05) < 1e-9,
          str(S.raw_weight(none, 1.05)))
    check("不传 base 时保持旧行为",
          abs(S.weight_multiplier(none) - 1.0) < 1e-9)
    check("base 加在 log 内 → 年份影响被压缩",
          abs(S.weight_multiplier(none, 1.05) - (1 + __import__("math").log(1.05))) < 1e-9)

    row_eff = {"pinned": 0, "manual": 0, "effective": 1,
               "ineffective": 0, "partial": 0}
    gap_year = S.weight_multiplier(none, S.RECENCY_HI) - \
        S.weight_multiplier(none, S.RECENCY_LO)
    gap_exp = S.weight_multiplier(row_eff, 1.0) - S.weight_multiplier(none, 1.0)
    check("年份影响必须远小于经验影响（≥10×）", gap_exp > gap_year * 10,
          f"年份 {gap_year:.4f} vs 经验 {gap_exp:.4f}")


def test_cache_verdict():
    """缓存准入判据（两级：规则 + 模型灰区）。

    这一组锁住的核心教训：**判据不看标题**。
    本机有 5 篇 IEEE 英文论文的 item.title 是中文（用户起的中文名或
    Zotero 译名），正文全英文。若用标题判断语言，它们会被冤枉成
    "中文文献但没中文"→坏缓存 → 改用 PyMuPDF，而其中一篇的 PyMuPDF
    只提到缓存的 13% 字符 —— 等于用坏文本换掉好文本。
    改用缓存自身的拉丁字母占比后，两簇分得很开（坏缓存 59%~76%，
    真英文文献 87%~91%），阈值 0.80 落在中间。
    """
    print("\n[缓存体检] 规则判据")
    from zreader import cache_verdict, CACHE_LATIN_FOREIGN

    check("阈值卡在两簇中间（0.76~0.87）",
          0.76 < CACHE_LATIN_FOREIGN < 0.87, f"{CACHE_LATIN_FOREIGN}")

    v, w = cache_verdict(["अनुच्छेद" * 40] * 3)
    check("异域脚本 → bad", v == "bad", w)

    latin = "Hanzhi Ma, Student Member, IEEE, Dept of Electronic Eng " * 12
    v, w = cache_verdict([latin] * 3)
    check("外文文献 → ok", v == "ok", w)

    v, w = cache_verdict(
        ["IEEE TRANSACTIONS ON INSTRUMENTATION AND MEASUREMENT " * 15] * 3,
        "面向复杂场景的目标检测方法研究")
    check("标题是中文也不影响（不看标题）", v == "ok", w)

    noise = "2017 1 67 , ( 266071) :2016-08-17 :mzzz222@126.com " * 12
    v, w = cache_verdict([noise] * 3)
    check("非外文却几乎无中文 → bad", v == "bad", w)

    v, w = cache_verdict([])
    check("空缓存 → bad", v == "bad", w)
    v, w = cache_verdict(["短"] * 3)
    check("过短 → bad", v == "bad", w)

    st = S.char_stats("中文中文abcdef")
    check("char_stats: CJK 计数", st["cjk"] == 4, str(st))
    check("char_stats: 拉丁计数", st["latin"] == 6, str(st))
    check("char_stats: 空白不计入分母", S.char_stats("中 文")["total"] == 2)
    check("char_stats: 空串不炸", S.char_stats("")["total"] == 0)


# ---------------------------------------------------------------- 解析健康度


def test_parsing_health():
    """文献级解析健康判据 —— 重点是字符偏移的**可还原验证**。

    这一组锁住的核心教训：**"这一段读不出完整句子"不是坏文本的判据**。
    上一版用「规则 + 4B 模型挑坏切片」，在正常文本上误判约 60%：
    公式矩阵、参考文献、封面、中文正文全被打成坏。抽 90 个上轮判**好**的
    切片重判，仍有 25% 被判坏 —— 判据本身不成立。

    改成文献级 + 客观判据后，全库 97 篇有正文的文献里只有 1 篇真有问题
    （`MBCD29B4`，源 PDF 字体编码坏，提取出来每个字符偏移 +29）。
    而且这个偏移是**可确定性还原**的：位移 +29 后常见词率从 0.2% 跳到 32.8%。
    """
    print("\n[解析健康] 字符偏移的检测与还原")
    en_common_ratio = S.en_common_ratio
    deshift = S.deshift
    find_shift = S.find_shift
    shift_fix = S.shift_fix
    SHIFT_MIN_LATIN = S.SHIFT_MIN_LATIN

    # 一段正常英文（正常英文学术文本的常见词率在 20%~40%）
    good = ("The magnetic field of the ship is measured in the experiment. "
            "This method can be used for the compensation of the field, "
            "and the results are in good agreement with the theory. ") * 12
    r, n = en_common_ratio(good)
    check("正常英文：常见词率高", r > 0.25, f"{r:.1%} / {n} 词")

    # 坏文本 = 好文本每个可打印 ASCII 字符 -29（实测那篇的偏移量）
    bad = deshift(good, -29)
    rb, _ = en_common_ratio(bad)
    check("偏移文本：常见词率几乎为 0", rb < 0.02, f"{rb:.1%}")
    check("偏移文本：还原前后字面不同", bad != good)

    # ⚠ 这条是本组的关键：必须对**所有**可打印 ASCII 位移，不能只对字母。
    #   实测坏文本里 `&`→C、`6`→S、`0`→M、`'`→D 全不是字母；
    #   第一版只移字母，还原出来是 `&alculation and 6imulation`。
    check("还原对非字母也生效（&→C）",
          deshift("&DOFXODWLRQ", 29) == "Calculation",
          repr(deshift("&DOFXODWLRQ", 29)))
    check("还原对数字也生效（6→S / 0→M）",
          deshift("6LPXODWLRQ", 29) == "Simulation",
          repr(deshift("6LPXODWLRQ", 29)))
    check("空格不参与位移（否则会变成 =）",
          deshift("&DOFXODWLRQ DQG", 29) == "Calculation and",
          repr(deshift("&DOFXODWLRQ DQG", 29)))
    check("位移 0 不动文本", deshift(good, 0) == good)
    check("非 ASCII 不受影响", deshift("中文abc", 29).startswith("中文"))
    check("移出可打印区的字符保持原样",
          deshift("xyz", 29) == "xyz", repr(deshift("xyz", 29)))

    sh, b, a = find_shift(bad)
    check("find_shift 找到还原位移 +29", sh == 29, f"sh={sh} {b:.1%}→{a:.1%}")

    s, fixed = shift_fix(bad)
    check("偏移文本被判为需要还原", s != 0, f"shift={s}")
    check("还原后接近原文", en_common_ratio(fixed)[0] > 0.25,
          f"{en_common_ratio(fixed)[0]:.1%}")

    s2, same = shift_fix(good)
    check("正常英文不动它", s2 == 0 and same == good, f"shift={s2}")

    cn = "本文研究了城市交通流量的短时预测方法，实验结果表明该方法有效。" * 20
    s3, same3 = shift_fix(cn)
    check("中文文本不动它（latin 占比不够）", s3 == 0, f"shift={s3}")

    # 词数不够时不该做位移搜索 —— 实测 54 个词的中文文献位移后能"碰"到 25%
    tiny = deshift("This is a short text about the field. " * 3, -29)
    s4, _ = shift_fix(tiny)
    check("拉丁词太少时不下结论", s4 == 0, f"shift={s4}")

    check("阈值只对外文文献生效", SHIFT_MIN_LATIN >= 0.5,
          f"{SHIFT_MIN_LATIN}")


# ---------------------------------------------------------------- 中文查询改写


def test_query_rewrite():
    print("\n[查询改写] 中文按字 AND（这是 FTS5 unicode61 下中文可检索的关键）")
    try:
        from query import cjk_rewrite, build_match  # noqa: PLC0415
    except ImportError as exc:
        check("query 模块可导入", False, str(exc))
        return

    check("两字词被拆成单字 AND", cjk_rewrite("偶极") == '"偶" AND "极"',
          cjk_rewrite("偶极"))
    check("英文词不拆且不加引号", cjk_rewrite("dipole") == "dipole",
          cjk_rewrite("dipole"))
    check("中英混合都处理",
          cjk_rewrite("磁场 dipole") == '"磁" AND "场" AND dipole',
          cjk_rewrite("磁场 dipole"))
    check("单字保持原样", cjk_rewrite("磁") == '"磁"', cjk_rewrite("磁"))
    check("空查询返回空", build_match("") == "")
    check("引号被转义（防 FTS 语法报错）",
          build_match('a"b') != "" and '""' not in build_match('a"b').replace('"a""b"', ""),
          build_match('a"b'))


def test_purge_orphans():
    """孤儿清理：Zotero 里删掉的条目，知识库必须跟着消失。

    为什么值得单独测：这条路径**原来根本不存在**（增量判断只看 items.version，
    条目都从 Zotero 没了就没得比），结果是删掉一篇文献之后 kb_search 还搜得到。
    现在补上了，就要把它钉住 —— 尤其是"不许误伤还活着的条目"
    和"不许碰用户自己的经验/权重"这两条。
    """
    print("\n[孤儿清理] Zotero 删掉的条目要从知识库彻底移除")
    import shutil
    import sqlite3
    import tempfile

    import convert as CV  # noqa: PLC0415

    LIVE, DEAD = "LIVEKEY1", "DEADKEY2"
    tmp = tempfile.mkdtemp(prefix="kb-orphan-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(S.SCHEMA_SQL)

    for k in (LIVE, DEAD):
        conn.execute("INSERT INTO items(key, item_type, title, version, built_at) "
                     "VALUES(?,?,?,?,?)", (k, "journalArticle", "某标题 " + k, 1, "now"))
        conn.execute("INSERT INTO chunks(chunk_id, item_key, page, seq, text) "
                     "VALUES(?,?,?,?,?)",
                     (1 if k == LIVE else 2, k, 1, 0, "某正文 " + k))
    conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(1,'某正文 ' + ?)", (LIVE,))
    conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(2,'某正文 ' + ?)", (DEAD,))
    conn.execute("INSERT INTO embeddings(chunk_id, dim, vector, model) "
                 "VALUES(2, 4, X'00000000', 'fake')")
    conn.execute("INSERT INTO figures(item_key, page, kind, text) VALUES(?,?,?,?)",
                 (DEAD, 1, "caption-image", "某图注"))
    conn.execute("INSERT INTO experience(created_at, asked, outcome, source) "
                 "VALUES('now','试过某篇的方法','effective','user')")
    conn.execute("INSERT INTO item_weight(item_key, pinned, note) VALUES(?,?,?)",
                 (DEAD, 1, "用户标过重点"))
    conn.commit()

    # ⚠ 分级视图（views/<key>.*.md）也要跟着清 —— 否则条目都删了、文件还在，
    #   右键「打开知识库」会打开一份指向已删文献的 md。
    # 这里把 VIEWS_DIR 指到临时目录：绝不能拿**真实知识库**试删除。
    import kbviews as KV  # noqa: PLC0415
    _orig_views = S.VIEWS_DIR
    tmp_views = os.path.join(tmp, "views")
    os.makedirs(tmp_views, exist_ok=True)
    for _name in (f"{LIVE}.tldr.md", f"{DEAD}.tldr.md", f"{DEAD}.weight.md"):
        with open(os.path.join(tmp_views, _name), "w", encoding="utf-8") as fh:
            fh.write("x")
    S.VIEWS_DIR = tmp_views
    try:
        dead = CV._purge_orphans(conn, {LIVE})
        conn.commit()
        left = sorted(os.listdir(tmp_views))
    finally:
        S.VIEWS_DIR = _orig_views

    check("只清掉那一条孤儿", dead == [DEAD], f"得 {dead}")
    check("孤儿的分级视图被清、活着的保留",
          left == [f"{LIVE}.tldr.md"], f"剩 {left}")
    rows = {r["key"] for r in conn.execute("SELECT key FROM items")}
    check("活着的条目还在", LIVE in rows, f"剩 {rows}")
    check("孤儿从 items 消失", DEAD not in rows, f"剩 {rows}")

    n = conn.execute("SELECT COUNT(*) c FROM chunks WHERE item_key=?",
                     (DEAD,)).fetchone()["c"]
    check("孤儿的切片被删", n == 0, f"还剩 {n}")
    n = conn.execute("SELECT COUNT(*) c FROM chunks_fts WHERE rowid=2").fetchone()["c"]
    check("孤儿的 FTS 索引被删（否则还能被搜到）", n == 0, f"还剩 {n}")
    n = conn.execute("SELECT COUNT(*) c FROM embeddings WHERE chunk_id=2").fetchone()["c"]
    check("孤儿的向量被删", n == 0, f"还剩 {n}")
    n = conn.execute("SELECT COUNT(*) c FROM figures WHERE item_key=?",
                     (DEAD,)).fetchone()["c"]
    check("孤儿的图注被删", n == 0, f"还剩 {n}")

    # 活着的条目一个字节都不能动
    n = conn.execute("SELECT COUNT(*) c FROM chunks WHERE item_key=?",
                     (LIVE,)).fetchone()["c"]
    check("活着的条目切片未被误删", n == 1, f"得 {n}")

    # ⚠ 用户自己记的数据不许动 —— 这是 _purge_item 文档里写死的边界
    n = conn.execute("SELECT COUNT(*) c FROM experience").fetchone()["c"]
    check("经验层不受影响（那是用户记的，不是构建产物）", n == 1, f"得 {n}")
    n = conn.execute("SELECT COUNT(*) c FROM item_weight WHERE item_key=?",
                     (DEAD,)).fetchone()["c"]
    check("已删文献的重点标记也保留（用户可能还要它）", n == 1, f"得 {n}")

    # 传空集 = 清掉全部：这个行为本身没错，但调用方必须自己保证
    # "读到空列表"不等于"库里真空了"（convert.py 里有一道 all_ids 保险）
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_clean_text()
    test_html_to_markdown()
    test_chunk_text()
    test_split_pages()
    test_weight()
    test_recency_weight()
    test_cache_verdict()
    test_parsing_health()
    test_query_rewrite()
    test_purge_orphans()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
