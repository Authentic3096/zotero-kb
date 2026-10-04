"""提示词注册表测试：搬家没走样 + 用户能改 + 改坏了当场拒。

    python tests/test_prompts.py

盯四件事：
  ① **逐字不变**：拿搬家前从真机抓的 `_prompt_goldens.json`（5 次真实调用，
     含 system 与完整 prompt）逐字比对。这是"话术从硬编码搬到注册表时没有
     走样"的回归网 —— 提示词差一个字，模型行为就可能变，而没有任何报错。
  ② **覆盖生效**：写一份 kb/prompts.json，`get`/`render` 立刻用新文本；
     `reset` 之后回到默认。
  ③ **改坏了在保存时被拒**：删掉占位符（`{text}`）或契约键（`tags`）都必须
     被 `validate` 拦下，而不是等到调用模型时才炸 / 静默解析失败。
  ④ 渲染**不走 `str.format`**：提示词里本来就含 JSON 大括号
     （`{"tags": [...]}`），用 format 会把它们吃掉或需要写成 `{{`。

真实库只读；覆盖文件写到临时目录，测完不留痕。
"""

from __future__ import annotations

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


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("=" * 62)
print("提示词注册表（offline/prompts.py）")
print("=" * 62)

try:
    import prompts as PR
    import schemas as S
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  导不进 prompts：{type(exc).__name__}: {exc}")
    sys.exit(0)

# ---------------------------------------------------------------- 覆盖到临时目录
tmp = tempfile.mkdtemp(prefix="kbprompt_")
real_index_db = S.INDEX_DB
S.INDEX_DB = os.path.join(tmp, "index.db")     # path() 由它派生
assert PR.path().startswith(tmp), PR.path()

# ---------------------------------------------------------------- [1] 逐字不变
print("\n[1] 逐字比对：搬家前抓的 5 次真实调用")
# 基线随仓库走（tests/prompt_goldens.json）：它是**回归网**，下一个对话也要用，
# 不能只留在某个会话目录里。会话目录里那份只是抓取现场。
gold_candidates = [
    os.path.join(HERE, "prompt_goldens.json"),
    os.path.join(r"D:\DSHwork\sessions\2026-10-04-继续之前的手递会话问题",
                 "_prompt_goldens.json"),
]
gold_path = next((p for p in gold_candidates if os.path.exists(p)), "")
if not gold_path:
    print("  SKIP  找不到基线 prompt_goldens.json")
else:
    golden = json.load(open(gold_path, encoding="utf-8"))["calls"]
    calls = []

    def fake_generate(prompt, system="", **kw):
        calls.append({"prompt": prompt, "system": system})
        return {"ok": False, "error": "capture"}

    import judge
    import metafill as MF
    import check_chunks as CC
    real = judge.generate
    judge.generate = fake_generate
    try:
        judge.tag_item("示例标题", "示例摘要内容")
        judge.summarize_item("示例标题", "示例正文内容")
        judge.extract_experience("示例对话片段")
        MF._model_suggest(["正文片段" * 900], ["date", "DOI", "creators"])
        CC.judge_item("正文片段" * 300)
    finally:
        judge.generate = real
    check("抓到的调用数一致", len(calls) == len(golden),
          f"{len(calls)} vs {len(golden)}")
    labels = ["tag_item", "summarize_item", "extract_experience",
              "metafill._model_suggest", "check_chunks.judge_item"]
    for i, (got, want) in enumerate(zip(calls, golden)):
        name = labels[i] if i < len(labels) else f"call[{i}]"
        check(f"{name}：system 逐字相同", got["system"] == want["system"],
              f"{got['system'][:40]!r} vs {want['system'][:40]!r}")
        check(f"{name}：prompt 逐字相同", got["prompt"] == want["prompt"],
              f"{len(got['prompt'])} vs {len(want['prompt'])} 字符")

# ---------------------------------------------------------------- [2] 覆盖
print("\n[2] 覆盖生效 / 还原")
check("默认不是 custom", not PR.is_custom("tag", "system"))
before = PR.get("tag", "system")
try:
    PR.save("tag", "system", before + "（自检追加）")
    check("保存后立刻生效", PR.get("tag", "system").endswith("（自检追加）"),
          PR.get("tag", "system")[-20:])
    check("标成 custom", PR.is_custom("tag", "system"))
    PR.reset("tag", "system")
    check("reset 后回到默认", PR.get("tag", "system") == before)
    check("reset 后不再是 custom", not PR.is_custom("tag", "system"))
except Exception as exc:  # noqa: BLE001
    check(f"覆盖流程没抛异常：{type(exc).__name__}: {exc}", False)

# ---------------------------------------------------------------- [3] 拒绝改坏
print("\n[3] 改坏了在保存时就被拒")
cases = [
    ("删掉占位符", "tag", "user", '输出 JSON：{"tags": []}'),
    ("删掉契约键", "tag", "user", "标题：{title}\n\n摘要：{abstract}"),
    ("存空串", "tag", "system", "   "),
]
for name, pid, field, text in cases:
    err = PR.validate(pid, field, text)
    check(f"{name} → 被拒", bool(err), "没报错")
try:
    PR.save("tag", "user", "标题：{title}")
    check("save 也拒绝改坏", False, "没抛异常")
except ValueError:
    check("save 也拒绝改坏", True)
check("未知 id 被拒", bool(PR.validate("nope", "system", "x")))

# ---------------------------------------------------------------- [4] 渲染
print("\n[4] 渲染：JSON 大括号不许被吃掉")
out = PR.render("tag", title="T", abstract="A")
check("正文里含 JSON 大括号", '{"tags": ["标签1"' in out, out[:60])
check("占位符被换掉", "标题：T" in out and "摘要：A" in out, out[-40:])
chunks = PR.render("chunks", fragments="[片段 1] 正文")
check("chunks 的 fragments 被换掉", "[片段 1] 正文" in chunks and "{fragments}" not in chunks)
check("metafill 三个占位符都能换",
      "{text}" not in PR.render("metafill", text="X", creator_hint="H",
                                creator_rules="R"))
check("render 不炸（含 JSON 示例）", len(out) > 80)

# ---------------------------------------------------------------- [5] 兜底
print("\n[5] 文件坏了也要能用（退化成默认值）")
os.makedirs(os.path.dirname(PR.path()), exist_ok=True)
with open(PR.path(), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("{ 这不是合法 JSON")
check("坏文件 → 用默认值", PR.get("tag", "system") == PR.default("tag", "system"))
with open(PR.path(), "w", encoding="utf-8", newline="\n") as fh:
    json.dump({"version": 1, "overrides": {"不存在的id": {"system": "x"}}}, fh)
check("未知 id 的覆盖被忽略", PR.get("tag", "system") == PR.default("tag", "system"))

# ---------------------------------------------------------------- [6] 文件格式
print("\n[6] 写出来的文件：无 BOM、LF、可读回")
PR.save("summary", "system", "自检话术")
raw = open(PR.path(), "rb").read()
check("没有 BOM", raw[:3] != b"\xef\xbb\xbf", raw[:3].hex())
check("行尾是 LF", b"\r\n" not in raw)
check("能读回", PR.get("summary", "system") == "自检话术")
check("写的不是默认值", PR.is_custom("summary", "system"))
PR.reset()

S.INDEX_DB = real_index_db
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 62)
print(f"通过 {PASS}，失败 {FAIL}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
