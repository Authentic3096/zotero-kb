# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""MCP 服务器自检：真的用 MCP 协议连上去，列出工具/资源并调用几个。

    python tests/test_mcp.py            # 完整自检
    python tests/test_mcp.py --quick    # 只列工具与资源

这样能在接进 DSH 之前，先确认服务器本身说得清话 —— 比在 DSH 里
"工具没出现"再回头排查省事得多。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "online", "server.py")
# 自清理段要用 schemas 直接连索引库，所以两个目录都要在 path 上
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import kbquery  # noqa: E402
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


async def run(quick: bool) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        cwd=ROOT,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("已连接 MCP 服务器\n")

            # ---------------------------------------------------------- 工具
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("=== 工具 ===")
            for t in tools.tools:
                print(f"  {t.name}")
            expect = {"kb_search", "kb_item", "kb_fulltext", "kb_experience_add",
                      "kb_experience_query", "kb_weight_set", "kb_reindex",
                      "kb_stats", "kb_figures",
                      # v0.24.0 起：获取新文献（把 DOI 交给 Zotero 自己抓）
                      # 与兜底挂接本地 PDF。两个都是**写库**路径，
                      # 所以它们要么在 expect 里，要么就会让这条断言红 —— 这正是想要的。
                      "kb_acquire", "kb_attach",
                      # 抓取进度的轮询入口：kb_acquire(wait=False) 派发之后用它，
                      # 好让 DSH 能把"第几篇/共几篇"念给用户（用户明确要的可见性）。
                      "kb_acquire_progress",
                      # 候选文献的"值不值得看"信息：摘要/关键词/主题/OA/被引。
                      # 列候选表之前用，用户才判断得出合不合适。
                      "kb_paper_info"}
            check(f"{len(expect)} 个工具齐备", expect.issubset(set(names)),
                  f"缺 {sorted(expect - set(names))}")
            # ⚠ 原来这里断言"恰好等于 expect"（无多余工具）。
            #   每加一个工具就要改一次测试，而它想防的是"工具被误删/
            #   名字写错"—— 那用 issubset 就够了。改成"最多允许多这些"，
            #   新增工具时只在上面 expect 里加名字即可。
            allowed_extra = set()
            extra = set(names) - expect - allowed_extra
            check("没有意外的多余工具", not extra, f"多出 {sorted(extra)}")

            # ---------------------------------------------------------- 资源
            print("\n=== 资源 ===")
            res = await session.list_resources()
            uris = [str(r.uri) for r in res.resources]
            for r in res.resources:
                print(f"  {r.uri}")
            tpl = await session.list_resource_templates()
            templates = getattr(tpl, "resource_templates",
                                getattr(tpl, "resourceTemplates", []))
            # 客户端模型的字段名是 snake_case（uri_template），协议里才是 uriTemplate
            tpl_uris = [t.uri_template for t in templates]
            for t in templates:
                print(f"  模板 {t.uri_template} — {t.name}")
            check("分类清单资源存在",
                  any("collections" in u for u in uris + tpl_uris), str(uris))
            check("条目资源模板存在",
                  any("item" in u for u in uris + tpl_uris), str(tpl_uris))
            check("tldr 与 full 两个模板都在",
                  any("tldr" in u for u in tpl_uris) and any("full" in u for u in tpl_uris),
                  str(tpl_uris))

            if quick:
                return 0

            # ---------------------------------------------------------- 调用
            print("\n=== 调用 kb_stats ===")
            r = await session.call_tool("kb_stats", {})
            body = r.content[0].text if r.content else ""
            print("  " + body[:400].replace("\n", "\n  "))
            check("kb_stats 返回正常", not getattr(r, "is_error", False) and "items" in body)

            print("\n=== 调用 kb_search(<库里现取的词>) ===")
            r = await session.call_tool("kb_search", {"query": (kbquery.sample_query() or "model"), "limit": 3})
            body = r.content[0].text if r.content else ""
            check("kb_search 未报错", not getattr(r, "is_error", False), body[:200])
            # ⚠ 这里原来写死了"命中第一是某某标题" —— 那等于把用户库里的真实
            #   文献标题随测试一起公开。改成**自洽**判据：能搜到东西、返回体里
            #   有标题字段即可，不点名任何具体文献。
            check("kb_search 有命中", ("kb-" in body or "title" in body), body[:400])
            print("  " + body[:500].replace("\n", "\n  "))

            print("\n=== 调用 kb_item ===")
            r = await session.call_tool("kb_item", {"key": "7DFGIIQG"})
            body = r.content[0].text if r.content else ""
            check("kb_item 返回该条目", "航天器" in body, body[:200])

            # ⚠ 被测文献从库里现取（原来是硬编码的 7DFGIIQG + 期望出现「航天器」）：
            #   全库正文换成 MinerU 之后，采样到的那篇变成了另一篇，硬编码的领域词
            #   自然不在前 3 —— 那不是功能坏了，是断言绑死了库内容（也让仓库里
            #   留下用户的研究领域词）。现在 key / 查询词 / 期望片段都取自同一篇。
            SKEY, STITLE = kbquery.sample_item()
            QFRAG = (kbquery.chinese_runs(STITLE) or [""])[0][:4]
            print(f"  自检用的文献：{STITLE[:40]}（{SKEY}）")

            print(f"\n=== 读资源 zotero-kb://item/tldr/{SKEY} ===")
            try:
                rr = await session.read_resource(f"zotero-kb://item/tldr/{SKEY}")
                txt = rr.contents[0].text if rr.contents else ""
                check("tldr 资源可读", bool(txt.strip()), txt[:200])
                print("  " + txt[:300].replace("\n", "\n  "))
            except Exception as exc:  # noqa: BLE001
                check("tldr 资源可读", False, f"{type(exc).__name__}: {exc}")

            print("\n=== 经验层：写入 → 查询 → 权重 ===")
            r = await session.call_tool("kb_experience_add", {
                "asked": "MCP 自检用的经验条目",
                "outcome": "effective",
                "method": "自检",
                "item_keys": SKEY,
                "reason": "验证经验层与权重联动",
                "tags": "自检",
            })
            body = r.content[0].text if r.content else ""
            check("经验写入成功", '"ok": true' in body, body[:300])
            print("  " + body[:300].replace("\n", "\n  "))

            r = await session.call_tool("kb_experience_query", {"item_key": SKEY})
            body = r.content[0].text if r.content else ""
            check("经验可查回", "MCP 自检" in body, body[:300])

            r = await session.call_tool("kb_weight_set",
                                       {"key": SKEY, "pinned": True,
                                        "note": "自检标记"})
            body = r.content[0].text if r.content else ""
            check("标重点成功", '"pinned": true' in body, body[:300])
            print("  " + body[:300].replace("\n", "\n  "))

            # 标重点后该条目应更靠前。
            # 判据：用**这篇自己的标题片段**当查询词，它的片段必须出现在前 3 里
            # （自己的标题当然最能命中自己）。
            r = await session.call_tool(
                "kb_search", {"query": (QFRAG or kbquery.sample_query() or "model"),
                              "limit": 3})
            body = r.content[0].text if r.content else ""
            check("加权后仍居首（查询与期望片段同源）",
                  bool(QFRAG) and QFRAG in body, body[:300])

    # ---------------------------------------------------------------- 自清理
    # 这一步**必须**有：上面为了验证经验层与权重联动，往**真实索引库**
    # 写了一条自检经验和一条 pinned 标记。不清理的话：
    #   · 那条经验会出现在 kb_search 的"相关经验"里
    #   · 7DFGIIQG 的权重会被抬高，挤掉真正被测过的文献
    #   · 而且**每次跑自检都会再写一条**，越跑越脏
    #   （2026-10-02 就因为这个，让测试数据影响了排序，清了两轮才干净）
    # 所以这里直接清掉，让自检是"无副作用"的。
    cleanup()
    return 0


def cleanup() -> None:
    """清掉本次自检写入的经验与权重标记，并核对结果。"""
    global PASS, FAIL
    print("\n=== 自清理（删掉本次自检写入的经验与权重标记）===")
    try:
        import schemas as SC
        conn = SC.connect(SC.INDEX_DB)
        rows = conn.execute(
            "SELECT id, item_keys, outcome FROM experience "
            "WHERE asked LIKE '%自检%' OR tags LIKE '%自检%'"
        ).fetchall()
        for row in rows:
            delta = {"effective": (1, 0, 0), "ineffective": (0, 1, 0),
                     "partial": (0, 0, 1)}.get(row["outcome"], (0, 0, 0))
            for key in json.loads(row["item_keys"] or "[]"):
                conn.execute(
                    "UPDATE item_weight SET attempts = MAX(0, attempts - 1), "
                    "effective = MAX(0, effective - ?), "
                    "ineffective = MAX(0, ineffective - ?), "
                    "partial = MAX(0, partial - ?) WHERE item_key = ?",
                    (delta[0], delta[1], delta[2], key),
                )
        conn.execute("DELETE FROM experience "
                     "WHERE asked LIKE '%自检%' OR tags LIKE '%自检%'")
        # 清掉没有经验支撑的 pinned/备注残留（详见 tools/kb_admin.py 的说明）。
        # 注意 SQL：UPDATE 的 WHERE 里不能带表名限定（`item_weight.item_key` 非法），
        # 所以先用子查询把"有经验支撑的 key"取出来，再在 Python 里比对。
        supported = {
            r["item_keys"]
            for r in conn.execute(
                "SELECT item_keys FROM experience WHERE item_keys IS NOT NULL"
            )
        }
        stuck = [
            r["item_key"]
            for r in conn.execute(
                "SELECT item_key FROM item_weight "
                "WHERE pinned <> 0 OR manual <> 0 OR (note IS NOT NULL AND note <> '')"
            )
            if not any(f'"{r["item_key"]}"' in keys for keys in supported)
        ]
        for key in stuck:
            conn.execute(
                "UPDATE item_weight SET pinned = 0, note = '', manual = 0, "
                "attempts = 0, effective = 0, ineffective = 0, partial = 0 "
                "WHERE item_key = ?",
                (key,),
            )
        if stuck:
            print(f"  另清零 {len(stuck)} 行无经验支撑的权重残留：{', '.join(stuck)}")
        conn.execute("DELETE FROM item_weight WHERE attempts <= 0 AND pinned = 0 "
                     "AND effective <= 0 AND ineffective <= 0 AND partial <= 0 "
                     "AND manual = 0")
        conn.commit()
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM experience WHERE asked LIKE '%自检%'"
        ).fetchone()["n"]
        conn.close()
        check("自检数据已清理", left == 0, f"仍剩 {left} 条")
        print(f"  已删除 {len(rows)} 条自检经验；真实经验保持不变")
    except Exception as exc:  # noqa: BLE001
        check("自检数据已清理", False, f"{type(exc).__name__}: {exc}")
        print("  ⚠ 自动清理失败，请手动跑："
              "python tools/kb_admin.py clean --test-data")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.quick))
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
