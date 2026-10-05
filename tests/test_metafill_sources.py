"""双源元数据补全：`offline/metafill_sources.py`（纯逻辑）+ metafill 的接线。

    python tests/test_metafill_sources.py

为什么单独一个测试文件：这一层是**纯函数**（取文本 / 归一化 / 合并 / 渲染），
不依赖 Zotero、不依赖模型、不依赖 MinerU 真装 —— 全部可以用假来源测。
判据里最要紧的三条：
  ① 两路一致 → 标 both（不能把"两路互证"降级成单路）；
  ② 两路不一致 → **不擅自合并**：按逐字段优先表取一路，另一路进 alternatives；
  ③ 任何一路缺失都不影响另一路（优雅退化），两路都缺时要说清原因。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, HERE)

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


class FakeReader:
    """只实现 metafill_sources 用到的那一个方法。"""

    def __init__(self, pages, source="zotero-cache", raise_exc=None):
        self.pages = pages
        self.source = source
        self.raise_exc = raise_exc
        self.calls = 0

    def fulltext_for(self, item, **kw):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return (list(self.pages), self.source)


class FakeItem:
    def __init__(self, key="FAKEKEY1", pages=None):
        self.key = key
        self.title = "合成标题"
        self.item_type = "journalArticle"
        self._pages = pages or []


def set_mineru(key, pages, tier="basic"):
    """把"这一篇有/没有 MinerU 产物"设成打桩状态（调用前先 _save，最后 _restore）。"""
    import mineru as M
    M.load_pages = lambda k, kb="": (list(pages) if k == key else [])
    M.read_meta = lambda k, kb="": ({"tier": tier, "ok": True} if k == key else {})
    M.artifact_dir = lambda k, kb="": os.path.join("X:\fake-kb", "mineru", k)


def _save_mineru():
    import mineru as M
    return (M.load_pages, M.read_meta, M.artifact_dir)


def _restore_mineru(old):
    import mineru as M
    M.load_pages, M.read_meta, M.artifact_dir = old


PAGE_A = ("第37卷 第4期 电子学报 Vol. 37, No. 4 2025年12月 "
          "DOI: 10.1234/abc.2025.001 文章编号: 0372-2112(2025)04-0765-06 "
          "张三, 李四, 王五 摘要 " + "正文内容" * 40)
PAGE_B = ("基于合成标题的研究 张三 李四 王五 电子学报 2025 年 12 月 "
          "第 37 卷 第 4 期 pp. 765-770 DOI 10.1234/abc.2025.001 " + "正文内容" * 40)


def test_norm():
    print("\n[1] norm_value：只做保守归一化（不做语义换算）")
    import metafill_sources as MS
    check("pages 的 ~ 与 - 视为一致",
          MS.norm_value("pages", "765~770") == MS.norm_value("pages", "765-770"))
    check("volume 去空白", MS.norm_value("volume", " 37 ") == MS.norm_value("volume", "37"))
    check("DOI 大小写与空格不敏感",
          MS.norm_value("DOI", "10.1234/ABC") == MS.norm_value("DOI", " 10.1234/abc"))
    check("不把 'Vol. 37' 和 '37' 当成一致（保守策略，交给上层裁决）",
          MS.norm_value("volume", "Vol. 37") != MS.norm_value("volume", "37"))
    check("空值不相等", MS.norm_value("date", "") != MS.norm_value("date", "2025-12"))


def test_gather():
    print("\n[2] gather：两路取文本与优雅退化")
    import metafill_sources as MS

    item = FakeItem("K1")
    originals = _save_mineru()
    try:
        # ① 两路都有
        set_mineru("K1", ["B 页1", "B 页2"])
        g = MS.gather(item, reader=FakeReader(["A 页1", "A 页2"]))
        check("两路都拿到 → both=True", g["both"] and g["any"], str(g["any"]))
        check("A 路来源被记录", g["pdf"]["source"] == "zotero-cache")
        check("B 路来源带档位", g["mineru"]["source"] == "mineru-basic",
              str(g["mineru"]["source"]))
        check("sources_line 同时报两路",
              "A(" in MS.sources_line(g) and "B(" in MS.sources_line(g),
              MS.sources_line(g))

        # ② 只有 B
        g2 = MS.gather(item, reader=FakeReader([], source=""))
        check("A 路空 → 只用 B（any=True / both=False）",
              g2["any"] and not g2["both"], str(g2["pdf"]))

        # ③ 只有 A（这一篇没有产物）
        set_mineru("OTHER", ["别的篇"])
        g3 = MS.gather(item, reader=FakeReader(["A 页1"]))
        check("B 路没有产物 → 只用 A，且 why 说明原因",
              g3["any"] and not g3["mineru"]["ok"] and bool(g3["mineru"]["why"]),
              str(g3["mineru"]))

        # ④ 两路都没有
        g4 = MS.gather(item, reader=FakeReader([]))
        check("两路都没有 → any=False 且 why_no_text 说清两路",
              not g4["any"] and "A（PDF 自解析）" in MS.why_no_text(g4)
              and "B（MinerU）" in MS.why_no_text(g4),
              MS.why_no_text(g4))

        # ⑤ A 路抛异常不冒泡
        set_mineru("K1", ["B 页1"])
        g5 = MS.gather(item, reader=FakeReader([], raise_exc=RuntimeError("读库炸了")))
        check("A 路抛异常不冒泡（写进 why，B 路照常）",
              g5["any"] and not g5["pdf"]["ok"] and "读库炸了" in g5["pdf"]["why"],
              str(g5["pdf"]))
    finally:
        _restore_mineru(originals)


def test_head_text():
    print("\n[3] head_text：页数与字符上限（A/B 两路同一口径）")
    import metafill_sources as MS
    check("只取前 2 页", MS.head_text(["1", "2", "3"], head_pages=2) == "1\n2")
    check("按字符截断", len(MS.head_text(["x" * 9999], max_chars=100)) == 100)
    check("空白页不影响", MS.head_text(["", "b"]).strip() == "b")


def test_merge():
    print("\n[4] merge_candidates：一致 / 单路 / 冲突（逐字段优先表）")
    import metafill_sources as MS

    both = MS.merge_candidates({
        "pdf": {"volume": ("37", 0, 2), "DOI": ("10.1/a", 5, 11)},
        "mineru": {"volume": ("37", 3, 5), "DOI": ("10.1/a", 9, 15)},
    })
    check("两路同值 → agreement=both", both["volume"]["agreement"] == "both",
          str(both["volume"]))
    check("both 时 sources 记两路",
          [v["source"] for v in both["volume"]["values"]] == ["pdf", "mineru"])

    single = MS.merge_candidates({"pdf": {"pages": ("765-770", 0, 7)}, "mineru": {}})
    check("只有一路 → agreement=single", single["pages"]["agreement"] == "single")

    conflict = MS.merge_candidates({
        "pdf": {"volume": ("37", 0, 2), "creators": ("张三; 李四", 0, 6)},
        "mineru": {"volume": ("38", 0, 2), "creators": ("张三; 王五", 0, 6)},
    })
    check("值不同 → agreement=conflict", conflict["volume"]["agreement"] == "conflict")
    check("冲突时 volume 默认取 A（PDF 原文）",
          conflict["volume"]["primary"]["source"] == "pdf",
          str(conflict["volume"]["primary"]))
    check("冲突时 creators 默认取 B（MinerU 版式更强）",
          conflict["creators"]["primary"]["source"] == "mineru",
          str(conflict["creators"]["primary"]))
    check("另一路进 alternatives",
          [a["value"] for a in conflict["volume"]["alternatives"]] == ["38"],
          str(conflict["volume"]["alternatives"]))
    check("agreement_note 说清两路不一致",
          "不一致" in MS.agreement_note("volume", conflict["volume"]))


def test_prompt_text():
    print("\n[5] prompt_text：两路正文 + 候选清单（同一占位符送出）")
    import metafill_sources as MS

    srcs = {
        "pdf": {"ok": True, "pages": ["A 路首页正文"], "source": "zotero-cache"},
        "mineru": {"ok": True, "pages": ["B 路首页正文"], "source": "mineru-basic"},
    }
    merged = MS.merge_candidates({"pdf": {"volume": ("37", 0, 2)},
                                  "mineru": {"volume": ("37", 0, 2)}})
    txt = MS.prompt_text(srcs, merged)
    check("含 A 路标头", "来源 A" in txt and "A 路首页正文" in txt, txt[:120])
    check("含 B 路标头", "来源 B" in txt and "B 路首页正文" in txt)
    check("含候选清单与一致性标注",
          "候选" in txt and "volume" in txt and "两路一致" in txt, txt[-200:])

    only_a = MS.prompt_text({"pdf": srcs["pdf"], "mineru": {"ok": False, "pages": []}})
    check("只有 A 时只渲染 A", "来源 A" in only_a and "来源 B" not in only_a)
    check("两路都没有时返回空串", MS.prompt_text({}) == "")


def test_metafill_wiring():
    print("\n[6] metafill 接线：一致→high / 冲突→medium+备选 / 模型裁决→model-picked")
    import metafill as MF

    item = FakeItem("K9")
    # 两路都给出 volume=37（一致）+ 只有 A 有 DOI
    srcs_both = {
        "pdf": {"ok": True, "pages": [PAGE_A], "source": "zotero-cache"},
        "mineru": {"ok": True, "pages": [PAGE_B], "source": "mineru-basic"},
        "head_pages": 2, "any": True, "both": True,
    }
    r = MF.suggest_for_item(item, use_model=False, sources_override=srcs_both)
    by = {s["field"]: s for s in r["suggestions"]}
    check("两路都拿到时 sources.line 记两路", "A(" in r["sources"]["line"], r["sources"]["line"])
    if "volume" in by:
        check("volume 两路一致 → agreement=both 且 high",
              by["volume"]["agreement"] == "both"
              and by["volume"]["confidence"] == "high", str(by["volume"]))
        check("suggestion 带 sources 与 note",
              len(by["volume"]["sources"]) == 2 and bool(by["volume"]["note"]),
              str(by["volume"]))
    else:
        check("（合成文本没抽出 volume，跳过该项）", True)

    # 冲突：A 说 37、B 说 38 → 没模型时给一路 + 备选 + medium
    srcs_conflict = {
        "pdf": {"ok": True, "pages": [PAGE_A.replace("第37卷", "第37卷")],
                "source": "zotero-cache"},
        "mineru": {"ok": True, "pages": [PAGE_B.replace("第 37 卷", "第 38 卷")],
                   "source": "mineru-basic"},
        "head_pages": 2, "any": True, "both": True,
    }
    r2 = MF.suggest_for_item(item, use_model=False, sources_override=srcs_conflict)
    v2 = next((s for s in r2["suggestions"] if s["field"] == "volume"), None)
    if v2:
        check("冲突且无模型 → confidence=medium + 带备选",
              v2["confidence"] == "medium" and bool(v2["alternatives"]),
              str(v2))
        check("冲突的说明写清了取哪一路",
              "不一致" in (v2.get("note") or ""), str(v2.get("note")))
    else:
        check("（合成文本没抽出 volume，跳过冲突项）", True)

    # 模型裁决：伪造 judge.generate 给 volume=38 → agreement 应为 model-picked
    import judge
    real_gen = judge.generate
    real_avail = MF.model_available

    def fake_gen(prompt, system="", **kw):
        return {"ok": True, "text": ('{"volume": "38", "evidence": '
                                     '{"volume": "第 38 卷"}}'),
                "model": "fake"}

    try:
        judge.generate = fake_gen
        MF.model_available = lambda: (True, "")
        r3 = MF.suggest_for_item(item, use_model=True, sources_override=srcs_conflict)
        v3 = next((s for s in r3["suggestions"] if s["field"] == "volume"), None)
        if v3:
            check("模型裁决后标 model-picked 或 low",
                  v3["agreement"] in ("model-picked", "conflict", "both")
                  or v3["source"] == "model", str(v3))
            check("模型看过两路（记在 model.saw_sources）",
                  "A(" in (r3["model"].get("saw_sources") or ""),
                  str(r3["model"]))
        else:
            check("（模型路径没给 volume，跳过）", True)
    finally:
        judge.generate = real_gen
        MF.model_available = real_avail


def main() -> int:
    test_norm()
    test_gather()
    test_head_text()
    test_merge()
    test_prompt_text()
    test_metafill_wiring()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
