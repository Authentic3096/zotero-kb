"""分节纲要（中间层）：切节 / 跳过参考文献 / 增量指纹 / 渲染。

    python tests/test_digest.py

为什么单测它：切节规则决定"纲要分几节、每节是什么"，它错了整层就没意义；
而真跑一次要调几十次模型（几十秒到几分钟），所以模型那一层用**假 generate**。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

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


def blk(typ, content="", **kw):
    d = {"type": typ, "content": content}
    d.update(kw)
    return d


def structured(pages):
    return {"pages": [{"page_idx": i, "blocks": b} for i, b in enumerate(pages)]}


DOC = structured([
    [blk("doc_title", "合成标题", level=1), blk("header", "页眉噪声"),
     blk("text", "作者甲，作者乙；摘要：本文研究了某方法。" * 3)],
    [blk("paragraph_title", "1 引言", level=1), blk("text", "引言正文。" * 60)],
    [blk("paragraph_title", "2 方法", level=1),
     blk("text", "方法正文。" * 200),
     blk("equation", "E = m c^2"),
     blk("chart", img_path="images/p3_chart_1.jpg", img_caption=["图 1 结构图"])],
    [blk("paragraph_title", "参考文献", level=1), blk("text", "文献条目。" * 100)],
])


def test_sectionize():
    print("\n[1] sectionize：按标题切节 / 丢页眉 / 页码范围 / 跳过参考文献")
    import digest as D
    secs = D.sectionize(DOC)
    titles = [s["title"] for s in secs]
    check("切出了 4 节", len(secs) == 4, str(titles))
    check("第一节是文档标题", titles[0] == "合成标题", str(titles))
    check("页眉噪声没进任何一节",
          all("页眉噪声" not in s["text"] for s in secs))
    check("页码范围正确（方法在第 3 页）",
          secs[2]["page_from"] == 3 and secs[2]["page_to"] == 3,
          f"{secs[2]['page_from']}-{secs[2]['page_to']}")
    check("公式进了正文且带 $$",
          "$$E = m c^2$$" in secs[2]["text"], secs[2]["text"][-60:])
    check("图片行与图注都在",
          "images/p3_chart_1.jpg" in secs[2]["text"]
          and "图 1 结构图" in secs[2]["text"])
    ref = [s for s in secs if s["title"] == "参考文献"][0]
    check("参考文献被标记为 skip（不调模型）", ref["skip"] is True)
    check("其它节没被误标 skip", secs[1]["skip"] is False)
    check("每节都有指纹与字数",
          all(s["fp"] and s["chars"] > 0 for s in secs))


def test_windowize():
    print("\n[2] 没有标题块时按窗口切（兜底）")
    import digest as D
    # 6 页、每页 ~1500 字 → 按 4000 字窗口切，应该出 2 节以上
    doc = structured([[blk("text", "正文。" * 500)] for _ in range(6)])
    secs = D.sectionize(doc)
    check("切出了多节（每节 ~4000 字窗口）", len(secs) >= 2, str(len(secs)))
    check("标题是「第 N 段」", secs[0]["title"].startswith("第"), secs[0]["title"])
    check("页码范围不越界",
          all(s["page_from"] <= s["page_to"] for s in secs))


def test_split_and_merge():
    print("\n[3] 过长节再切 / 过短节并入下一节 / 节数上限")
    import digest as D
    long_doc = structured([
        [blk("paragraph_title", "1 长节", level=1),
         # 一节 10000+ 字（超过 MAX_SECTION_CHARS=9000）→ 必须被再切
         blk("text", "段落一。\n\n" * 3000 + "尾。")],
    ])
    secs = D.sectionize(long_doc)
    check("超长节被切成（续 N）",
          len(secs) >= 2 and any("（续" in s["title"] for s in secs),
          str([s["title"] for s in secs]))

    # 规则：**带标题**的节一律保留（纲要的价值就是保住原文节结构）；
    # 只有"没有标题的碎片"才会并进相邻节。
    titled = structured([
        [blk("paragraph_title", "1 甲", level=1), blk("text", "短。" * 10)],
        [blk("paragraph_title", "2 乙", level=1), blk("text", "长。" * 400)],
    ])
    secs2 = D.sectionize(titled)
    check("带标题的极短节也保留（不吞结构）", len(secs2) == 2,
          str([s["title"] for s in secs2]))
    frag = structured([
        [blk("text", "开头的一小段。")],
        [blk("paragraph_title", "1 正文", level=1), blk("text", "正文。" * 200)],
    ])
    secs2b = D.sectionize(frag)
    check("开头没有标题的碎片并进了下一节", len(secs2b) == 1,
          str([s["title"] for s in secs2b]))

    many = structured([[blk("paragraph_title", f"{i} 节", level=1),
                        blk("text", f"第 {i} 节内容。" * 60)] for i in range(40)])
    secs3 = D.sectionize(many)
    check(f"节数被压到上限 {D.MAX_SECTIONS} 以内", len(secs3) <= D.MAX_SECTIONS,
          str(len(secs3)))


def test_fp_stable():
    print("\n[4] 节指纹：正文没变就稳定（增量重跑的依据）")
    import digest as D
    a = D.sectionize(DOC)[1]
    b = D.sectionize(DOC)[1]
    check("同一输入 → 同一指纹", a["fp"] == b["fp"], f"{a['fp']} vs {b['fp']}")
    changed = structured([
        [blk("doc_title", "合成标题", level=1), blk("text", "作者甲，作者乙；摘要：本文研究了某方法。" * 3)],
        [blk("paragraph_title", "1 引言", level=1), blk("text", "改过的引言。" * 60)],
        [blk("paragraph_title", "2 方法", level=1), blk("text", "方法正文。" * 200)],
        [blk("paragraph_title", "参考文献", level=1), blk("text", "文献条目。" * 100)],
    ])
    c = D.sectionize(changed)[1]
    check("正文改了 → 指纹变", a["fp"] != c["fp"], f"{a['fp']} vs {c['fp']}")


def test_build_with_fake_model():
    print("\n[5] build：调模型（假）/ 跳过参考文献 / 第二次复用缓存")
    import digest as D
    import judge

    tmp = tempfile.mkdtemp(prefix="kbdigest-")
    kb = os.path.join(tmp, "kb")
    os.makedirs(os.path.join(kb, "mineru", "FAKEKEY1"), exist_ok=True)
    with io.open(os.path.join(kb, "mineru", "FAKEKEY1",
                              "structured_content.json"), "w",
                 encoding="utf-8", newline="\n") as fh:
        json.dump(DOC, fh, ensure_ascii=False)

    calls = {"n": 0}
    real_gen = judge.generate

    def fake_gen(prompt, system="", **kw):
        calls["n"] += 1
        return {"ok": True, "model": "fake",
                "text": json.dumps({"summary": "这一节做了某事。",
                                    "points": ["要点一", "要点二"]},
                                   ensure_ascii=False)}

    # 落盘位置也要重定向到临时目录（别写真实库）
    real_meta = (D.load_outline, D.save_outline)
    store: dict = {}
    D.load_outline = lambda key, s=None: store.get(key, {})
    D.save_outline = lambda key, data, s=None: store.__setitem__(key, data)
    try:
        judge.generate = fake_gen
        r = D.build("FAKEKEY1", kb_dir=kb)
        check("生成成功且节数=4", r["ok"] and r["n_sections"] == 4, str(r["why"]))
        # 4 节里只有 2 节真的会问模型：正文开头那节只有 60 字（< 80 字门槛）、
        # 参考文献被跳过 —— 这两条省下的正是"纲要比全文便宜"的一部分。
        check("只问了 2 次模型（开头太短 + 参考文献跳过）", calls["n"] == 2,
              str(calls["n"]))
        check("跳到参考文献的节留了占位说明",
              any("不展开" in (s.get("summary") or "")
                  for s in r["outline"]["sections"]))
        first_calls = calls["n"]
        r2 = D.build("FAKEKEY1", kb_dir=kb)
        check("第二次没有再问模型（全部命中节指纹）",
              calls["n"] == first_calls and r2["asked"] == 0,
              f"asked={r2['asked']} cached={r2['cached']}")
        r3 = D.build("FAKEKEY1", kb_dir=kb, force=True)
        check("--force 时重跑", calls["n"] > first_calls, str(calls["n"]))
        # 渲染
        txt = D.render("FAKEKEY1", r["outline"])
        check("渲染出标题与节", "分节纲要" in txt and "## 1." in txt, txt[:80])
        check("渲染里带页码范围", "p." in txt)
        check("没有内容时 build 会如实报原因",
              D.build("NOPEKEY", kb_dir=kb)["ok"] is False)
    finally:
        judge.generate = real_gen
        D.load_outline, D.save_outline = real_meta
        shutil.rmtree(tmp, ignore_errors=True)


def test_levels():
    print("\n[6] 级别清单与渲染入口")
    import kbviews as KV
    ids = [lv["id"] for lv in KV.LEVELS]
    check("outline 是第 2 级（紧跟摘要）", ids[:2] == ["tldr", "outline"], str(ids))
    check("outline 在 GENERATED_IDS 里（会被写进 views/）",
          "outline" in KV.GENERATED_IDS)
    check("路径模板是 views/{key}.outline.md",
          KV.level("outline")["rel"] == "views/{key}.outline.md")
    class _FakeSearcher:
        def get_item(self, key):
            return {"title": "合成标题", "author_line": "", "year": "",
                    "venue": "", "collections": [], "tags": [],
                    "weight": 1.0, "fulltext_chars": 0, "n_annotations": 0,
                    "n_notes": 0, "abstract": ""}

        def read(self, *a, **k):
            return []

        def close(self):
            pass

    txt = KV.render("outline", "没有纲要的KEY", _FakeSearcher())
    check("条目在、但还没生成纲要时给友好提示（且不调模型）",
          "还没有分节纲要" in txt and "digest.py" in txt, txt[:90])

    # 插件侧的镜像也必须有它（check_kb_levels 之外的快速自检）
    js = io.open(os.path.join(ROOT, "zotero-plugin", "src", "13-kbopen.js"),
                 encoding="utf-8").read()
    check("插件侧 KB_LEVELS 也有 outline 且顺序一致",
          js.index('id: "outline"') > js.index('id: "tldr"')
          and js.index('id: "outline"') < js.index('id: "card"'))


def main() -> int:
    test_sectionize()
    test_windowize()
    test_split_and_merge()
    test_fp_stable()
    test_build_with_fake_model()
    test_levels()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
