"""校验 "知识库分级" 的清单在**两种语言里是同一份**。

    python tools/check_kb_levels.py

## 为什么需要它

分级清单（摘要与要点 / 完整档案 / 按页正文 / 图注与表格 / 权重与经验）有
两个消费方，各在一个语言里：

    offline/kbviews.py     LEVELS      —— Python 侧（生成视图、面板分级窗口）
    zotero-plugin/src/13-kbopen.js  KB_LEVELS  —— JS 侧（Zotero 右键二级菜单）

JS 里读不到 Python 的常量，只能复制一份。而"手工同步的约定最后一定会漂移"是
这个项目反复吃过的亏（同类检查见 check_api_presets.py：插件与服务端各存一份
服务商地址）。所以这里把"两边必须一致"变成一条会红的检查，接进 check_plugin.py，
并在 CI 里跟着跑。

具体盯三条：**id 集合、标签、相对路径模板**（外加顺序）。label 不一致 → 用户
在两处看到不同的名字；rel 不一致 → 某一边会指向不存在的文件。

## 返回码（调用方要区分）

    0 = 两侧一致
    1 = **真的不一致**（要红）
    2 = **这项跑不起来**（缺 node / 读不到源文件）—— 不能当成"通过"，
        也不能当成"不一致"（否则缺 node 的机器会莫名其妙红）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

JS = os.path.join(ROOT, "zotero-plugin", "src", "13-kbopen.js")


def py_levels() -> list[dict]:
    import kbviews as KV  # noqa: PLC0415
    return [{"id": lv["id"], "label": lv["label"], "rel": lv["rel"]}
            for lv in KV.LEVELS]


def js_levels() -> list[dict]:
    """用 node 把 KB_LEVELS 里的那三个字段抠出来。

    为什么借 node 而不是正则硬解：这个数组元素是**跨行的对象字面量**，
    正则一遇到换行/注释/引号变化就会漏项，而漏项会让检查变成"永远通过"
    （本项目出过"审计器某档从来没有人往里放东西"的事故）。
    真正执行那段 JS 最可靠：只构造数组、不调用任何插件函数。
    """
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if not (node and os.path.exists(node)):
        raise RuntimeError("node 不可用（无法解析 JS 侧的 KB_LEVELS）")
    code = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// 只取 KB_LEVELS: [ ... ], 这一段：从 "KB_LEVELS:" 起做括号配平
const at = src.indexOf('KB_LEVELS');
if (at < 0) { console.log('ERR 找不到 KB_LEVELS'); process.exit(2); }
const open = src.indexOf('[', at);
let depth = 0, end = -1;
for (let i = open; i < src.length; i++) {
  if (src[i] === '[') depth++;
  else if (src[i] === ']') { depth--; if (depth === 0) { end = i + 1; break; } }
}
if (end < 0) { console.log('ERR KB_LEVELS 的 [] 不配平'); process.exit(2); }
const arr = eval(src.slice(open, end));
console.log(JSON.stringify(arr.map(l => ({ id: l.id, label: l.label, rel: l.rel }))));
"""
    out = subprocess.run([node, "-e", code, JS], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=60)
    msg = (out.stdout or "").strip()
    if not msg.startswith("["):
        raise RuntimeError(f"解析 KB_LEVELS 失败：{msg or out.stderr}")
    return json.loads(msg)


def main() -> int:
    print("=" * 70)
    print("知识库分级清单：Python 与 JS 两侧是否一致")
    print("=" * 70)
    problems: list[str] = []

    try:
        py = py_levels()
    except Exception as exc:  # noqa: BLE001
        print(f"  [XX] 读不到 offline/kbviews.py 的 LEVELS：{exc}")
        return 2                      # 跑不起来 ≠ 不一致
    try:
        js = js_levels()
    except Exception as exc:  # noqa: BLE001
        print(f"  [XX] 读不到 {os.path.relpath(JS, ROOT)} 的 KB_LEVELS：{exc}")
        return 2                      # 跑不起来 ≠ 不一致

    print(f"  Python：{len(py)} 级  {'、'.join(l['label'] for l in py)}")
    print(f"  JS    ：{len(js)} 级  {'、'.join(l['label'] for l in js)}")

    py_by = {l["id"]: l for l in py}
    js_by = {l["id"]: l for l in js}

    only_py = sorted(set(py_by) - set(js_by))
    only_js = sorted(set(js_by) - set(py_by))
    if only_py or only_js:
        print(f"  [XX] id 集合不同：只有 Python 有 {only_py}；只有 JS 有 {only_js}")
        problems.append("分级 id 集合不一致")
    else:
        print(f"  [OK] id 集合一致（{'、'.join(py_by)}）")

    for lv_id in sorted(set(py_by) & set(js_by)):
        a, b = py_by[lv_id], js_by[lv_id]
        if a["label"] != b["label"]:
            print(f"  [XX] {lv_id} 的标签不同："
                  f"Python「{a['label']}」 vs JS「{b['label']}」")
            problems.append(f"{lv_id} 标签不一致")
        if a["rel"] != b["rel"]:
            print(f"  [XX] {lv_id} 的路径模板不同："
                  f"Python「{a['rel']}」 vs JS「{b['rel']}」")
            problems.append(f"{lv_id} 路径模板不一致")
    if not problems:
        print("  [OK] 每一级的标签与路径模板都逐字相同")

    # 顺序也要一致：右键菜单是按这个顺序列出来的，两边顺序不同会让人困惑
    if [l["id"] for l in py] != [l["id"] for l in js]:
        print(f"  [XX] 顺序不同：Python {[l['id'] for l in py]}"
              f" vs JS {[l['id'] for l in js]}")
        problems.append("分级顺序不一致")
    else:
        print("  [OK] 顺序一致（右键菜单与面板列出来的次序相同）")

    # 路径模板必须是"相对知识库目录"的、且不能用 ../
    for lv in py:
        rel = lv["rel"].replace("\\", "/")
        if rel.startswith("/") or ".." in rel or ":" in rel:
            print(f"  [XX] {lv['id']} 的 rel 不是安全的相对路径：{lv['rel']}")
            problems.append(f"{lv['id']} 的 rel 不安全")
        if "{key}" not in rel:
            print(f"  [XX] {lv['id']} 的 rel 里没有 {{key}}：{lv['rel']}")
            problems.append(f"{lv['id']} 的 rel 缺 {{key}}")
    if not problems:
        print("  [OK] 每个 rel 都是相对路径、且带 {key} 占位符")

    print("\n" + "=" * 70)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        print("\n  改的时候两边一起改：offline/kbviews.py 的 LEVELS 和")
        print("  zotero-plugin/src/13-kbopen.js 的 KB_LEVELS。")
        return 1
    print("两侧一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
