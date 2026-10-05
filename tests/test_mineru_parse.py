"""MinerU 接进转换管道的那一半：渲染 / 指纹 / 解压 / 回落 / 清理。

    python tests/test_mineru_parse.py

为什么这些必须单测（不能只靠"我手动跑过一次 5 页的论文"）：
  · `render_pages()` 决定了 KB 拿到的**逐页正文**长什么样 —— 页眉页脚丢没丢、
    公式有没有包成 $$、图注有没有留下，全是它说了算；
  · 指纹决定"要不要重跑"：算错了要么永远重跑（几十分钟白等），
    要么永远不重跑（换了 PDF 还用旧产物）；
  · 解压那段是**自己写的** zip 处理（防 zip-slip），喂坏包不能炸；
  · 回落必须是"按篇"的：一篇失败不能把整轮降级。
  · 真实解析要 5~35 秒/篇且依赖已装的 MinerU，所以这里用**假 runner** 造产物。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

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


# ---------------------------------------------------------------- 夹具

STRUCTURED = {
    "pages": [
        {"page_idx": 0, "blocks": [
            {"type": "header", "content": "第 6 期 2020 年 6 月"},
            {"type": "doc_title", "level": 1, "content": "基于遗传算法的卫星磁模型研究"},
            {"type": "text", "content": "徐超群<sup>1，2</sup>，易忠"},
            {"type": "equation", "content": "B_{px} = \\sum_{i=1}^{N} a_{xi} M_{xi}"},
            {"type": "page_footnote", "content": "收稿日期: 2018-04-23"},
        ]},
        {"page_idx": 1, "blocks": [
            {"type": "paragraph_title", "level": 1, "content": "2 计算方法"},
            {"type": "text", "content": "遗传算法以均方根误差为目标函数。"},
            {"type": "chart", "img_path": "images/page_2_chart_1.jpg",
             "img_caption": ["图 2 测点分布图"]},
            {"type": "page_number", "content": "1109"},
        ]},
    ],
    "metadata": {"document": {"page_count": 2}},
}


def make_zip(path: str, include_structured: bool = True,
             include_md: bool = True, evil: bool = False) -> str:
    """造一个"像 MinerU 输出的" zip。"""
    with zipfile.ZipFile(path, "w") as zf:
        if include_md:
            zf.writestr("markdown.md", "# 标题\n\n正文…\n\n![图](images/a.jpg)\n")
        if include_structured:
            zf.writestr("structured_content.json",
                        json.dumps(STRUCTURED, ensure_ascii=False))
        zf.writestr("middle_json.json", json.dumps({"pages": []}))
        zf.writestr("images/a.jpg", b"\xff\xd8\xff\xe0fake-jpeg")
        zf.writestr("images/page_2_chart_1.jpg", b"\xff\xd8\xff\xe0fake2")
        if evil:
            zf.writestr("../../../evil.txt", "should not be written")
    return path


def fake_runner_factory(tmpdir: str, *, make: bool = True, rc: int = 0,
                        structured=None):
    """假 runner：不真跑 MinerU，直接在 `-o` 指定的目录里造出 zip。

    ⚠ 必须**读出命令里的 `-o` 路径**再往里写 —— 真实调用也会把产物放那儿，
      假的放进别处就测不出"路径传对了没有"。
    """
    def runner(args, timeout=0.0, **kw):
        out = ""
        for i, a in enumerate(args):
            if a == "-o" and i + 1 < len(args):
                out = args[i + 1]
        if make and out:
            os.makedirs(out, exist_ok=True)
            make_zip(os.path.join(out, "result.zip"),
                     include_structured=structured is not False,
                     evil=bool(kw.get("evil")))
        return rc, "假的 MinerU 输出"
    return runner


# ---------------------------------------------------------------- 测试

def test_render_pages():
    print("\n[1] render_pages：逐页正文怎么渲染")
    import mineru as M

    pages = M.render_pages(STRUCTURED)
    check("页数对", len(pages) == 2, str(len(pages)))
    p1, p2 = pages
    check("页眉被丢掉（MinerU 自己标了 header）", "第 6 期" not in p1, p1[:60])
    check("页脚注也被丢掉", "收稿日期" not in p1, p1[:60])
    check("页码被丢掉", "1109" not in p2, p2[:60])
    check("标题渲染成一级标题", p1.startswith("# 基于遗传算法"), p1[:40])
    check("段落标题按 level 渲染成二级标题", "## 2 计算方法" in p2, p2[:60])
    check("公式包成 $$（能被检索命中）",
          "$$B_{px} = \\sum_{i=1}^{N} a_{xi} M_{xi}$$" in p1, p1[:200])
    check("正文里的 <sup> 原样保留（不 HTML 转义）",
          "<sup>1，2</sup>" in p1)
    check("图片渲染成 markdown 图片引用",
          "![图 2 测点分布图](images/page_2_chart_1.jpg)" in p2, p2[:200])
    check("图注文字留在正文里（能搜到）", "图 2 测点分布图" in p2)
    check("空块/未知类型不炸", M.render_pages({"pages": [{"page_idx": 0, "blocks": [
        {"type": "unknowntype"}, None, "字符串不是块"]}]}) == [""], "")
    check("没有 pages 键时返回空列表", M.render_pages({}) == [])


def test_fingerprint():
    print("\n[2] 指纹：PDF / 档位 / 工具版本 任一变化都要能反映出来")
    import mineru as M

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    try:
        pdf = os.path.join(tmp, "a.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        fp1 = M.fingerprint(pdf, "basic", "C:\\x\\mineru-kit.exe", "4.0.10")
        check("同一输入稳定（可复现）",
              fp1 == M.fingerprint(pdf, "basic", "C:\\x\\mineru-kit.exe", "4.0.10"))
        check("换档位 → 指纹变",
              fp1 != M.fingerprint(pdf, "standard", "C:\\x\\mineru-kit.exe", "4.0.10"))
        check("换工具版本 → 指纹变（升级后不会一直命中旧产物）",
              fp1 != M.fingerprint(pdf, "basic", "C:\\x\\mineru-kit.exe", "4.1.0"))
        check("换可执行文件 → 指纹变",
              fp1 != M.fingerprint(pdf, "basic", "D:\\y\\mineru-kit.exe", "4.0.10"))
        with open(pdf, "ab") as fh:
            fh.write(b"more")
        check("PDF 变了 → 指纹变",
              fp1 != M.fingerprint(pdf, "basic", "C:\\x\\mineru-kit.exe", "4.0.10"))
        check("PDF 不存在也不抛（返回一个指纹）",
              bool(M.fingerprint(os.path.join(tmp, "nope.pdf"), "basic", "x")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_parse_item():
    print("\n[3] parse_item：假 runner 造产物 → 落盘 / 命中指纹 / 失败不覆盖旧产物")
    import mineru as M

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    kb = os.path.join(tmp, "kb")
    os.makedirs(kb)
    pdf = os.path.join(tmp, "a.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF-1.4 fake")

    old_probe = M.probe
    try:
        # 让 probe 说"装了、basic 档就绪"（否则 parse_item 直接拒）
        M.probe = lambda *a, **k: {
            "ok": True, "exe": "C:\\x\\mineru-kit.exe", "version": "4.0.10",
            "tier_ready": {"basic": True, "standard": True},
        }
        res = M.parse_item("TESTKEY1", pdf, tier="basic", kb_dir=kb,
                           runner=fake_runner_factory(tmp))
        check("解析成功", res["ok"] is True, str(res.get("why")))
        check("页数 2", res["n_pages"] == 2, str(res.get("n_pages")))
        check("来源标记 mineru-basic", res["source"] == "mineru-basic")
        adir = os.path.join(kb, "mineru", "TESTKEY1")
        for name in ("markdown.md", "structured_content.json", "middle_json.json",
                     "pages.json", "meta.json"):
            check(f"产物里有 {name}", os.path.isfile(os.path.join(adir, name)), "")
        check("图片解出来了 2 张",
              len(os.listdir(os.path.join(adir, "images"))) == 2,
              str(os.listdir(os.path.join(adir, "images"))))
        check("markdown.md 里没有 base64 内联图",
              "base64" not in io.open(os.path.join(adir, "markdown.md"),
                                      encoding="utf-8").read())
        meta = M.read_meta("TESTKEY1", kb)
        check("meta 记了档位/页数/指纹",
              meta.get("tier") == "basic" and meta.get("n_pages") == 2
              and bool(meta.get("fingerprint")), json.dumps(meta)[:120])
        check("load_pages 能读回来（2 页）", len(M.load_pages("TESTKEY1", kb)) == 2)

        # 再跑 → 命中指纹，且**不调用 runner**（用计数器证明）
        calls = {"n": 0}

        def counting_runner(args, timeout=0.0, **kw):
            calls["n"] += 1
            return 0, ""
        res2 = M.parse_item("TESTKEY1", pdf, tier="basic", kb_dir=kb,
                            runner=counting_runner)
        check("第二次命中指纹（cached=True）", res2.get("cached") is True, str(res2))
        check("命中期**没有**再跑解析", calls["n"] == 0, str(calls))
        # force → 必须重跑
        res3 = M.parse_item("TESTKEY1", pdf, tier="basic", kb_dir=kb, force=True,
                            runner=fake_runner_factory(tmp))
        check("--force 时忽略指纹重新解析",
              res3["ok"] and not res3.get("cached"), str(res3.get("cached")))

        # 换档位：指纹不同 → 重跑，且产物档位更新
        res4 = M.parse_item("TESTKEY1", pdf, tier="standard", kb_dir=kb,
                            runner=fake_runner_factory(tmp))
        check("换档位会重跑并把 meta 档位改掉",
              res4["ok"] and M.read_meta("TESTKEY1", kb)["tier"] == "standard")

        # 失败路径：模型阶段不产出 zip → ok=False 且**旧产物仍可用**
        res5 = M.parse_item("TESTKEY1", pdf, tier="basic", kb_dir=kb,
                            runner=fake_runner_factory(tmp, make=False, rc=1))
        check("没产物时 ok=False 且给出原因",
              res5["ok"] is False and "zip" in (res5.get("why") or ""),
              str(res5.get("why")))
        meta5 = M.read_meta("TESTKEY1", kb)
        check("失败不清空旧产物（meta.ok 仍为 True、pages 还在）",
              meta5.get("ok") is True and meta5.get("last_error"),
              json.dumps(meta5, ensure_ascii=False)[:200])

        # 没装 MinerU
        M.probe = lambda *a, **k: {"ok": False, "why": "没装", "exe": ""}
        res6 = M.parse_item("TESTKEY1", pdf, tier="basic", kb_dir=kb)
        check("MinerU 不可用时返回 ok=False 与原因（不抛）",
              res6["ok"] is False and "不可用" in res6["why"], res6["why"])

        # 档位模型没下全
        M.probe = lambda *a, **k: {
            "ok": True, "exe": "C:\\x\\mineru-kit.exe", "version": "4.0.10",
            "tier_ready": {"basic": True, "standard": True, "advanced": False},
        }
        res7 = M.parse_item("TESTKEY1", pdf, tier="advanced", kb_dir=kb)
        check("档位模型没下全时明确说清（不是含糊失败）",
              res7["ok"] is False and "模型还没下全" in res7["why"], res7["why"])
    finally:
        M.probe = old_probe
        shutil.rmtree(tmp, ignore_errors=True)


def test_zip_safety():
    print("\n[4] 解压：坏包/怪名字不能炸，也不能写到目录外")
    import mineru as M

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    try:
        adir = os.path.join(tmp, "art")
        os.makedirs(adir)
        z = make_zip(os.path.join(tmp, "z.zip"), evil=True)
        res = M._extract_zip(z, adir)
        check("坏条目（../..）没被写到目录外",
              not os.path.exists(os.path.join(tmp, "evil.txt")), "")
        check("正常产物照常解出来",
              os.path.isfile(res["markdown"])
              and os.path.isfile(res["structured"]), json.dumps(res)[:150])
        # 只有 middle_json、没有 structured_content 的老格式 → 退回 middle
        z2 = os.path.join(tmp, "z2.zip")
        with zipfile.ZipFile(z2, "w") as zf:
            zf.writestr("middle_json.json", "{}")
        res2 = M._extract_zip(z2, os.path.join(tmp, "art2"))
        check("没有 structured_content.json 时退回 middle_json",
              res2["structured"].endswith("middle_json.json"),
              res2["structured"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_and_clear():
    print("\n[5] status_for / clear_artifacts")
    import mineru as M

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    kb = os.path.join(tmp, "kb")
    os.makedirs(kb)
    old_probe = M.probe
    try:
        M.probe = lambda *a, **k: {"ok": True, "exe": "C:\\x\\mineru-kit.exe",
                                   "version": "4.0.10",
                                   "tier_ready": {"basic": True}}
        pdf = os.path.join(tmp, "a.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF fake")
        st0 = M.status_for("K1", kb)
        check("没解析过时 parsed=False 且不抛", st0["parsed"] is False, str(st0))
        M.parse_item("K1", pdf, tier="basic", kb_dir=kb,
                     runner=fake_runner_factory(tmp))
        st1 = M.status_for("K1", kb)
        check("解析后 parsed=True / 页数对 / 不 stale",
              st1["parsed"] and st1["pages"] == 2 and not st1["stale"], str(st1))
        with open(pdf, "ab") as fh:      # PDF 变了 → 指纹过期
            fh.write(b"x")
        st2 = M.status_for("K1", kb)
        check("PDF 变了 → status 报 stale=True", st2["stale"] is True, str(st2))
        n = M.clear_artifacts("K1", kb)
        check("clear_cache 删掉一篇的产物",
              n == 1 and not os.path.isdir(os.path.join(kb, "mineru", "K1")), str(n))
        check("全删也能用（返回篇数）", M.clear_artifacts("", kb) == 0)
    finally:
        M.probe = old_probe
        shutil.rmtree(tmp, ignore_errors=True)


def test_fulltext_for_fallback():
    print("\n[6] fulltext_for：与 zreader 同形；失败返回空（回落交给调用方）")
    import mineru as M

    class Att:
        def __init__(self, path):
            self.path = path

    class It:
        def __init__(self, key, pdfs):
            self.key = key
            self.pdfs = pdfs

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    kb = os.path.join(tmp, "kb")
    os.makedirs(kb)
    pdf = os.path.join(tmp, "a.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    old_probe = M.probe
    try:
        M.probe = lambda *a, **k: {"ok": True, "exe": "C:\\x\\mineru-kit.exe",
                                   "version": "4.0.10",
                                   "tier_ready": {"basic": True}}
        pages, src = M.fulltext_for(It("K2", [Att(pdf)]), tier="basic", kb_dir=kb,
                                    runner=fake_runner_factory(tmp))
        check("成功时返回 (2 页, mineru-basic)",
              len(pages) == 2 and src == "mineru-basic", f"{len(pages)} {src}")
        pages2, src2 = M.fulltext_for(It("K2", [Att(pdf)]), tier="basic", kb_dir=kb)
        check("第二次带 +cached 后缀（来源可追溯）",
              src2 == "mineru-basic+cached", src2)
        pages3, src3 = M.fulltext_for(It("K3", []), tier="basic", kb_dir=kb)
        check("没有 PDF 附件 → ([], '')（调用方回落）",
              pages3 == [] and src3 == "", f"{pages3} {src3}")
        pages4, src4 = M.fulltext_for(It("K4", [Att(os.path.join(tmp, "nope.pdf"))]),
                                      tier="basic", kb_dir=kb)
        check("PDF 文件不在 → ([], '')", pages4 == [] and src4 == "")
    finally:
        M.probe = old_probe
        shutil.rmtree(tmp, ignore_errors=True)


def test_clear_does_not_touch_artifacts():
    """回归：清**探测缓存**的那个 `clear_cache()` 不许碰产物目录。

    2026-10-05 实测过的真实事故：产物清理函数最初也叫 `clear_cache()`，
    把探测那个覆盖了；于是 `test_mineru_probe.py` 里十几处"清探测缓存"的调用
    把**真实知识库**的 `<kb>/mineru/*` 全删了 —— 全库重建跑到第 6 篇，
    回头一看产物目录空了，而且没有任何报错。
    """
    print("\n[8] clear_cache 与 clear_artifacts 必须互不干扰")
    import mineru as M

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    kb = os.path.join(tmp, "kb")
    os.makedirs(kb)
    pdf = os.path.join(tmp, "a.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    old_probe = M.probe
    try:
        M.probe = lambda *a, **k: {"ok": True, "exe": "C:\\x\\mineru-kit.exe",
                                   "version": "4.0.10",
                                   "tier_ready": {"basic": True}}
        M.parse_item("KEEP1", pdf, tier="basic", kb_dir=kb,
                     runner=fake_runner_factory(tmp))
        adir = os.path.join(kb, "mineru", "KEEP1")
        check("先有产物", os.path.isfile(os.path.join(adir, "meta.json")))
        # ① 清探测缓存 → 产物必须还在
        M.clear_cache()
        check("clear_cache()（清探测缓存）不动产物",
              os.path.isfile(os.path.join(adir, "meta.json")), adir)
        # ② 陌生目录不能被当成产物删掉
        stray = os.path.join(kb, "mineru", "别人放的东西")
        os.makedirs(stray, exist_ok=True)
        with open(os.path.join(stray, "note.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        n = M.clear_artifacts("", kb)
        check("clear_artifacts 只删像产物的目录（陌生目录留着）",
              n == 1 and os.path.isdir(stray) and not os.path.isdir(adir), str(n))
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "meta.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        check("指名删一篇也要求像产物", M.clear_artifacts("KEEP1", kb) == 1)
    finally:
        M.probe = old_probe
        shutil.rmtree(tmp, ignore_errors=True)


def batch_runner(in_keys=None, *, make=True, rc=0):
    """假 runner：为输入目录里的每个 <KEY>.pdf 产出一个 <KEY>.zip。"""
    seen = {"called": 0, "inputs": [], "batches": 0}

    def runner(args, timeout=0.0, **kw):
        seen["called"] += 1
        indir = ""
        outdir = ""
        for i, a in enumerate(args):
            if a == "-o" and i + 1 < len(args):
                outdir = args[i + 1]
            elif i >= 2 and not a.startswith("-") and os.path.isdir(a) and not indir:
                indir = a
        os.makedirs(outdir, exist_ok=True)
        keys = []
        if os.path.isdir(indir):
            keys = [f[:-4] for f in os.listdir(indir) if f.lower().endswith(".pdf")]
        seen["inputs"].append(sorted(keys))
        seen["batches"] += 1
        if make:
            for k in keys:
                if in_keys is not None and k not in in_keys:
                    continue
                make_zip(os.path.join(outdir, k + ".zip"))
        return rc, "假批量输出"

    return runner, seen


def test_parse_many():
    print("\n[7] parse_many：一次加载模型、按 zip 名映射回 key、批量口径")
    import mineru as M

    tmp = tempfile.mkdtemp(prefix="kbmineru-")
    kb = os.path.join(tmp, "kb")
    os.makedirs(kb)
    pdfs = []
    for k in ("KAAA", "KBBB", "KCCC"):
        p = os.path.join(tmp, f"{k}.pdf")
        with open(p, "wb") as fh:
            fh.write(b"%PDF fake " + k.encode())
        pdfs.append((k, p))
    old_probe = M.probe
    try:
        M.probe = lambda *a, **k: {"ok": True, "exe": "C:\\x\\mineru-kit.exe",
                                   "version": "4.0.10",
                                   "tier_ready": {"basic": True}}
        runner, seen = batch_runner()
        res = M.parse_many(pdfs, tier="basic", kb_dir=kb, batch_size=2,
                           runner=runner)
        check("三篇全成功", res["n_ok"] == 3 and res["n_failed"] == 0,
              json.dumps({k: v for k, v in res.items() if k != "results"})[:200])
        check("分成两批调（batch_size=2）", seen["batches"] == 2, str(seen["batches"]))
        check("每批都真的把 PDF 硬链进了输入目录（名字是 key.pdf）",
              all(len(b) <= 2 and b for b in seen["inputs"]), str(seen["inputs"]))
        for k, _p in pdfs:
            adir = os.path.join(kb, "mineru", k)
            check(f"{k} 的产物齐（pages.json + meta.json）",
                  os.path.isfile(os.path.join(adir, "pages.json"))
                  and os.path.isfile(os.path.join(adir, "meta.json")))
            check(f"{k} 的页数是 2", len(M.load_pages(k, kb)) == 2)
        check("临时目录已清理（_batch_in / _batch_out 都不在）",
              not os.path.isdir(os.path.join(kb, "mineru", "_batch_in"))
              and not os.path.isdir(os.path.join(kb, "mineru", "_batch_out")))

        runner2, seen2 = batch_runner()
        res2 = M.parse_many(pdfs, tier="basic", kb_dir=kb, runner=runner2)
        check("再跑一遍全部命中指纹（n_cached=3、n_ok=0）",
              res2["n_cached"] == 3 and res2["n_ok"] == 0, str(res2["n_cached"]))
        check("命中期一次都没调解析", seen2["called"] == 0, str(seen2["called"]))

        runner3, _s3 = batch_runner()
        res3 = M.parse_many(pdfs, tier="basic", kb_dir=kb, force=True,
                            runner=runner3)
        check("force=True 时重跑", res3["n_ok"] == 3, str(res3["n_ok"]))

        # 一批什么都没产出 → 必须逐篇记失败原因（不能静默）
        runner4, _s4 = batch_runner(make=False, rc=1)
        res4 = M.parse_many(pdfs, tier="basic", kb_dir=kb, force=True,
                            runner=runner4)
        check("解析没产出时逐篇记失败并给原因",
              res4["n_failed"] == 3
              and all("zip" in w or "退出码" in w for _k, w in res4["failed"]),
              json.dumps(res4["failed"], ensure_ascii=False)[:160])

        # 没有 PDF 的条目也算失败（不能悄悄跳过）
        res5 = M.parse_many([("KDDD", os.path.join(tmp, "nope.pdf"))],
                            tier="basic", kb_dir=kb,
                            runner=batch_runner()[0])
        check("PDF 不存在 → 记失败并说明",
              res5["n_failed"] == 1 and "PDF 不存在" in res5["failed"][0][1],
              str(res5["failed"]))
    finally:
        M.probe = old_probe
        shutil.rmtree(tmp, ignore_errors=True)


def test_convert_wiring():
    print("\n[7] convert.py 的接线（参数 / 增量判据 / stats 键）")
    import convert as CV

    src = io.open(os.path.join(ROOT, "offline", "convert.py"),
                  encoding="utf-8").read()
    for frag, label in (
            ('"--parser"', "命令行有 --parser"),
            ('"--force-parse"', "命令行有 --force-parse"),
            ('want_parser != "zotero"', "循环里按解析器分支"),
            ('M.fulltext_for(', "调 mineru.fulltext_for"),
            ('source + "+fallback"', "失败按篇回落并留痕"),
            ('"fulltext_from_mineru"', "stats 里有 from_mineru"),
            ('"mineru_fell_back"', "MANIFEST 里有回落清单"),
            ('"parser": want_parser', "MANIFEST 记了解析器")):
        check(label, frag in src, "")
    ns = type("NS", (), {})()
    for k, v in dict(full=False, no_vectors=True, limit=0, item=[],
                     embed_model="", cache_check="", figures=False,
                     no_figures=False, parser="zotero", force_parse=False,
                     parse_timeout=0.0).items():
        setattr(ns, k, v)
    check("build 能被调用（签名没变：只吃 args）",
          callable(CV.build) and CV.build.__code__.co_argcount == 1, "")


def main() -> int:
    test_render_pages()
    test_fingerprint()
    test_parse_item()
    test_zip_safety()
    test_status_and_clear()
    test_fulltext_for_fallback()
    test_clear_does_not_touch_artifacts()
    test_parse_many()
    test_convert_wiring()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
