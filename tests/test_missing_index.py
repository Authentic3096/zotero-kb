# ⚠ 这个测试**绑定本机知识库的实际内容**（查询词、期望命中的文献标题、
#   条目 key 都取自本机 Zotero 库）。换一个知识库跑，这些断言要按你自己的
#   库改 —— 例如把查询词换成你库里确实有的主题。
#
#   文档（README / INSTALL / ARCHITECTURE）里的示例是**脱敏过的通用示例**，
#   和这里不一致是故意的：文档给陌生人看，测试给本机跑。

"""验收：索引没建好时，返回的是不是人话。

直接 sqlite 的 "no such table: meta" 对模型和用户都没用 ——
期望这里给出「先跑一次构建」这样的可执行指引。
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import kbquery  # noqa: E402
import schemas as S  # noqa: E402

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


# 让在线侧以为索引在别处（不存在）
missing = os.path.join(tempfile.gettempdir(), "kb-nonexistent-index.db")
if os.path.exists(missing):
    os.remove(missing)
S.INDEX_DB = missing
S.FULLTEXT_DIR = os.path.join(tempfile.gettempdir(), "kb-nonexistent-fulltext")
S.PAPERS_DIR = os.path.join(tempfile.gettempdir(), "kb-nonexistent-papers")

import server  # noqa: E402  （必须在改了路径之后再导入）

print("[1] 索引文件不存在时调用 kb_stats")
out = server.kb_stats()
check("返回的不是堆栈/SQLite 原始错误",
      "no such table" not in out and "Traceback" not in out, out[:200])
check("明确说明知识库还没构建", "还没有构建" in out or "不存在" in out, out[:300])
check("给出了可执行的修复方式", "1-convert.cmd" in out, out[:400])
print("  " + out[:280].replace("\n", "\n  "))

print("\n[2] 索引文件不存在时调用 kb_search")
out = server.kb_search(kbquery.sample_query() or "model")
check("同样是可读的指引", "1-convert.cmd" in out and "Traceback" not in out, out[:200])

print("\n[3] 索引文件存在但结构不完整")
with open(missing, "w", encoding="utf-8") as fh:
    fh.write("not a database")
out = server.kb_stats()
check("损坏/不完整时也不抛堆栈", "Traceback" not in out and "error" in out.lower(),
      out[:200])
print("  " + out[:200].replace("\n", "\n  "))

print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
