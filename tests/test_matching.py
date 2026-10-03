"""验证 learn.py 的文献匹配：用真实库中标题做正向与反向测试。

反向测试同样重要 —— 早期版本把「Zotero 数据目录结构」这类技术名词
当成了文献标题，所以要确认现在**不会**误匹配。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import kbquery  # noqa: E402
import schemas as S  # noqa: E402
import learn  # noqa: E402

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


conn = S.connect(S.INDEX_DB)

# ---------------------------------------------------------------- 正向
print("[正向] 从库里取真实标题，造一句「对话里提到这篇」的话")
rows = conn.execute(
    "SELECT key, title FROM items WHERE length(title) > 14 ORDER BY RANDOM() LIMIT 3"
).fetchall()
for row in rows:
    title = row["title"]
    import re
    chunk = re.findall(r"[\u4e00-\u9fff]{8,}", title)
    if not chunk:
        continue
    probe = chunk[0]
    # 用「」而不是《》：真实对话里提文献很少带书名号，
    # 带符号的文本反而不能证明匹配逻辑有效（符号不会出现在探针里，
    # 于是字符串搜索必然失败，属于测试本身写得不对）。
    text = f"用户：我按「{title}」里的方法试了一下，效果不错。"
    hits = learn.match_items(text, conn)
    check(f"{row['key']} 能匹配到", row["key"] in hits, f"hits={hits} probe={probe!r}")

# 退一步：只提标题里的一截（更接近真实对话的说法）
print("\n[正向] 只提标题里的一截长片段")
for row in rows[:2]:
    import re
    chunks = re.findall(r"[\u4e00-\u9fff]{8,}", row["title"])
    if not chunks:
        continue
    probe = chunks[0][:12]
    text = f"助手：参考{probe}那篇的做法，参数这样取。"
    hits = learn.match_items(text, conn)
    check(f"{row['key']} 用片段 {probe!r} 能匹配", row["key"] in hits, f"hits={hits}")

# ---------------------------------------------------------------- 反向
print("\n[反向] 技术名词不该被当成文献标题")
negatives = [
    "用户：我们把 Zotero 数据目录结构理了一下。",
    "助手：cordis.patch.yml 热加载机制需要确认。",
    "用户：FTS5 trigram 分词限制是个坑。",
    "助手：Python 3.14 环境与依赖都装好了。",
    "用户：SQLite 文件锁机制要注意。",
]
for text in negatives:
    hits = learn.match_items(text, conn)
    check(f"不误匹配：{text[:26]}…", hits == [], f"误匹配到 {hits}")

# ---------------------------------------------------------------- 领域词门槛
# ---------------------------------------------------------------- 主题词表
print("\n[主题词] 自动学出的主题词表")
domain = learn.library_domain()
check("主题词表非空", len(domain) > 20, f"{len(domain)} 个")
# 不点名具体领域词（那会暴露研究方向）：改为按 library_domain() 的**定义**验证 ——
# 主题词表学的是"**在多条标题里都出现**的实词"，所以拿真实标题里
# 满足"出现在 ≥2 条标题"的中文片段来核对覆盖率。
#
# ⚠ 取样要够多：只取前 20 条时可能一条中文片段都不共享（测试会假失败），
#   取全库才反映真实情况。
_titles = kbquery.sample_titles(400)
_shared: list[str] = []
for _t in _titles:
    for _r in kbquery.chinese_runs(_t):
        if len(_r) >= 2 and sum(1 for _x in _titles if _r in _x) >= 2:
            _shared.append(_r)
_shared = sorted(set(_shared))
check("标题里确实存在跨多条共享的片段（主题词的来源）",
      bool(_shared), f"共 {len(_titles)} 条标题，{len(_shared)} 个共享片段")
if _shared:
    _cov = [_f for _f in _shared if any(_f in g or g in _f for g in domain)]
    check("主题词表覆盖了这些共享片段",
          len(_cov) >= max(1, len(_shared) // 2),
          f"覆盖 {len(_cov)}/{len(_shared)}")
# 反向：技术名词不该被算成主题词（它们不会出现在多个标题里）
for unexpected in ("目录", "配置", "服务"):
    check(f"技术名词「{unexpected}」不在主题词里",
          not any(unexpected in g for g in domain), "被误学成主题词")

print("\n[门槛] 标题必须含主题词")
row = conn.execute(
    "SELECT key, title FROM items WHERE length(title) > 8 ORDER BY key LIMIT 1"
).fetchone()
if row:
    hits = learn.match_items(f"用户：{row['title']} 这篇的方法可行。", conn)
    check("真实标题可匹配", row["key"] in hits, f"hits={hits}")

conn.close()
print(f"\n{'=' * 56}\n通过 {PASS}　失败 {FAIL}\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
