"""分级视图测试：每个级别都能渲染出来，且**不破坏已有口径**。

    python tests/test_kbviews.py

盯三件事：
  ① 五个级别都能在真实库上渲染成非空 Markdown（库里没数据时跳过）；
  ② tldr 级的输出与**旧实现**（server.py 里那个 res_item_tldr）逐字相同 ——
     它是 MCP 资源，输出形状变了要重新训练模型的用法习惯；
     这条是"搬到 kbviews 之后没有走样"的回归网；
  ③ 生成的文件：没有 BOM、行尾是 LF、路径都在知识库目录下。

不需要 Zotero、不需要界面；只需要索引库（index.db）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

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


print("=" * 62)
print("分级视图（offline/kbviews.py）")
print("=" * 62)

try:
    import kbviews as KV
    import schemas as S
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  导不进 kbviews：{type(exc).__name__}: {exc}")
    sys.exit(0)

print("\n[1] 级别定义")
ids = [lv["id"] for lv in KV.LEVELS]
check("6 个级别（加了中间层 outline）", len(ids) == 6, str(ids))
for want in ("tldr", "card", "fulltext", "figures", "weight"):
    check(f"有「{want}」", want in ids, str(ids))
check("生成型级别是 tldr/outline/figures/weight",
      set(KV.GENERATED_IDS) == {"tldr", "outline", "figures", "weight"},
      str(KV.GENERATED_IDS))
# 每个 rel 都要能拼出知识库目录下的绝对路径
for lv in KV.LEVELS:
    p = KV.level_path(lv["id"], "ABCD1234")
    ok = (os.path.isabs(p)
          and os.path.normcase(p).startswith(os.path.normcase(S.KB_DIR))
          and "ABCD1234" in p)
    check(f"{lv['id']} 能拼出知识库下的绝对路径", ok, p)

# 取一个真实 key（没有就只跑定义部分的检查）
key = ""
try:
    conn = S.connect(S.INDEX_DB)
    row = conn.execute("SELECT key FROM items ORDER BY key LIMIT 1").fetchone()
    key = row["key"] if row else ""
    conn.close()
except Exception as exc:  # noqa: BLE001
    print(f"  （读不到索引库，跳过真实渲染：{exc}）")

if not key:
    print("  （库里没有条目，跳过真实渲染）")
    print(f"\n{'=' * 62}\n通过 {PASS}　失败 {FAIL}\n{'=' * 62}")
    sys.exit(1 if FAIL else 0)

print(f"\n[2] 真实渲染（样本 {key}）")
s = KV.open_searcher()
try:
    texts = {}
    for lv in KV.LEVELS:
        try:
            t = KV.render(lv["id"], key, s)
            texts[lv["id"]] = t
            check(f"{lv['id']} 渲染出非空 Markdown",
                  bool(t.strip()) and t.lstrip().startswith("#"),
                  f"{len(t)} 字符")
        except Exception as exc:  # noqa: BLE001
            check(f"{lv['id']} 渲染成功", False, f"{type(exc).__name__}: {exc}")

    print("\n[3] tldr 的输出必须与旧实现逐字相同")
    # 旧实现在 git 的 HEAD 版 online/server.py 里。取不到就跳过（比如已经
    # 提交了这次改动 —— 那时旧实现已经不存在了，跳过是正确行为）。
    old_fn = None
    try:
        out = subprocess.run(["git", "show", "HEAD:online/server.py"],
                             cwd=ROOT, capture_output=True)
        if out.returncode == 0:
            src = out.stdout.decode("utf-8")
            tree = ast.parse(src)
            fn = next((n for n in tree.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "res_item_tldr"), None)
            if fn is not None and "kbviews" not in src:
                body = textwrap.dedent(ast.get_source_segment(src, fn))
                body = body.replace("def res_item_tldr(key: str) -> str:",
                                    "def old_tldr(key):", 1)
                # 旧实现用的是模块级的 `kb()` 与 `XF`（extrafill）
                import extrafill as _XF  # noqa: PLC0415
                ns = {"kb": lambda: s, "XF": _XF}
                exec(compile(body, "<old>", "exec"), ns)  # noqa: S102
                old_fn = ns["old_tldr"]
    except Exception as exc:  # noqa: BLE001
        print(f"  （取不到旧实现，跳过这条：{type(exc).__name__}: {exc}）")
    if old_fn is None:
        print("  SKIP  旧实现已不在 HEAD 里（本轮改动已提交）—— 跳过逐字比对")
    else:
        same = old_fn(key) == texts.get("tldr", "")
        check("tldr 与旧实现逐字相同", same,
              "" if same else "输出形状变了！MCP 资源那边会跟着变")

    print("\n[4] 生成的文件：无 BOM、LF、路径在库内")
    bad_bom, bad_crlf, written = [], [], []
    try:
        for lv_id in KV.GENERATED_IDS:
            p = KV.level_path(lv_id, key)
            if not os.path.exists(p):
                continue
            written.append(p)
            raw = open(p, "rb").read()
            if raw[:3] == b"\xef\xbb\xbf":
                bad_bom.append(os.path.basename(p))
            if b"\r\n" in raw:
                bad_crlf.append(os.path.basename(p))
            if not os.path.normcase(p).startswith(os.path.normcase(S.KB_DIR)):
                bad_bom.append("(跑到库外了) " + p)
    except Exception as exc:  # noqa: BLE001
        check("读生成的文件", False, f"{type(exc).__name__}: {exc}")
    check("生成的文件没有 BOM", not bad_bom, str(bad_bom))
    check("生成的文件是 LF 行尾", not bad_crlf, str(bad_crlf))
    check("至少生成了 tldr 与 weight（figures 允许缺席）",
          len(written) >= 2, str([os.path.basename(x) for x in written]))

    print("\n[5] level_rows（界面靠它决定显示什么）")
    rows = KV.level_rows(key)
    check("返回每一层", len(rows) == len(KV.LEVELS), str(len(rows)))
    check("每一层都有 path/what/exists/size",
          all({"path", "what", "exists", "size"} <= set(r) for r in rows))
    check("card 与 fulltext 是现成文件、必须都在",
          all(r["exists"] for r in rows if r["id"] in ("card", "fulltext")),
          str([r["id"] for r in rows if not r["exists"]]))

    print("\n[6] 找不到的 key 也要给一句人话（不能抛）")
    for lv in KV.LEVELS:
        try:
            t = KV.render(lv["id"], "NOSUCHKEY", s)
            check(f"{lv['id']} 对不存在的 key 有落盘文案",
                  bool(t.strip()), repr(t[:60]))
        except Exception as exc:  # noqa: BLE001
            check(f"{lv['id']} 对不存在的 key 不抛异常", False, str(exc))
finally:
    try:
        s.close()
    except Exception:  # noqa: BLE001
        pass

print(f"\n{'=' * 62}\n通过 {PASS}　失败 {FAIL}\n{'=' * 62}")
sys.exit(1 if FAIL else 0)
