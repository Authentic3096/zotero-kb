"""模拟 Zotero 加载偏好面板的解析流程，验证 settings.xhtml 能不能被解析。

为什么需要这个：Zotero 加载 pane 时不是直接把文件当 XML 文档解析，而是
**把文件内容当字符串嵌进一个 <div> 里**再解析（preferences.js 的
_parseXHTMLToFragment）。这个差别很要命 —— 文件头部有 `<?xml ...?>`
声明时，嵌进 <div> 内部就成了非法 XML，整个面板点进去是空白的。

本机就这么踩过一次（用户报"设置里点击文献知识库没有反应"），
所以把 Zotero 的逻辑原样复刻出来做回归检查。
"""
from __future__ import annotations

import io
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
P = os.path.join(ROOT, "zotero-plugin", "settings.xhtml")

# 与 Zotero preferences.js 的 _parseXHTMLToFragment 一致：
#   parseFromSafeString(`<div xmlns="http://www.w3.org/1999/xhtml"
#     xmlns:xul="http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul">
#     ${str}</div>`, "application/xml")
WRAPPER_OPEN = (
    '<div xmlns="http://www.w3.org/1999/xhtml"'
    ' xmlns:xul="http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul">'
)
WRAPPER_CLOSE = "</div>"


def strip_xml_comments(text: str) -> str:
    """去掉 XML 注释，避免把注释里举的反例当成真的处理指令。

    ⚠ 这个检查器第一版就栽在这里：settings.xhtml 的注释里**写了**
      `<?xml version="1.0"?>` 当反面教材，结果检查器报"有处理指令"，
      而实际上文件顶部是注释、声明并不存在。做静态检查一定要先排除注释。
    """
    out = []
    i = 0
    while True:
        j = text.find("<!--", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        k = text.find("-->", j + 4)
        if k < 0:
            break                     # 注释没闭合，剩下全当注释
        i = k + 3
    return "".join(out)


def main() -> int:
    raw = io.open(P, encoding="utf-8").read()
    src = strip_xml_comments(raw)
    print("=" * 72)
    print("settings.xhtml 加载回归检查（复刻 Zotero _parseXHTMLToFragment）")
    print("=" * 72)

    problems = []

    # 1) 不能有 XML 声明 —— 嵌进 <div> 后就是非法 XML
    stripped = src.lstrip()
    if stripped.startswith("<?xml"):
        problems.append("文件开头有 `<?xml ...?>` 声明 —— 嵌进 <div> 后会导致"
                        "XML 解析失败，面板点进去一片空白")
        print("  [XX] 有 XML 声明（这就是那个 bug）")
    else:
        print("  [OK] 没有 XML 声明（注释里的反例不算）")

    # 2) 其他 xml 处理指令（如 <?xml-stylesheet ...?>）也会出问题
    pi = []
    i = 0
    while True:
        i = src.find("<?", i)
        if i < 0:
            break
        j = src.find("?>", i)
        if j < 0:
            break
        pi.append(src[i:j + 2][:60])
        i = j + 2
    if pi:
        problems.append(f"发现 XML 处理指令 {pi} —— 同样会破坏解析")
        print(f"  [XX] 有处理指令：{pi}")
    else:
        print("  [OK] 没有其它 XML 处理指令")

    # 3) 按 Zotero 的方式真的解析一遍（用原文，注释在 XML 里是合法的）
    wrapped = WRAPPER_OPEN + raw + WRAPPER_CLOSE
    try:
        root = ET.fromstring(wrapped)
        n = len(list(root.iter()))
        print(f"  [OK] 嵌进 <div> 后能解析，共 {n} 个元素")
    except ET.ParseError as exc:
        problems.append(f"嵌进 <div> 后解析失败：{exc}")
        print(f"  [XX] 嵌进 <div> 后解析失败：{exc}")

    # 4) 根元素必须只有一个（Zotero 假设 fragment 有一个根）
    try:
        root = ET.fromstring(wrapped)
        if len(list(root)) != 1:
            problems.append(f"根下有 {len(list(root))} 个顶层元素，"
                            f"Zotero 期望 1 个")
            print(f"  [XX] 顶层元素 {len(list(root))} 个（应为 1）")
        else:
            only = list(root)[0]
            tag = only.tag.split("}")[-1]
            print(f"  [OK] 单一根元素 <{tag}>，id="
                  f"{only.get('id') or '(无)'}")
            if not only.get("id"):
                problems.append("根元素没有 id（settings.js 靠 id 找它）")
    except ET.ParseError:
        pass

    # 5) 面板里的 id 与 settings.js 找的一致
    js = io.open(os.path.join(ROOT, "zotero-plugin", "settings.js"),
                 encoding="utf-8").read()
    for probe in ("zotero-kb-settings", "zotero-kb-project-root",
                  "zotero-kb-python-exe", "zotero-kb-ollama-exe",
                  "zotero-kb-env-save-btn", "zotero-kb-env-report"):
        in_xhtml = f'id="{probe}"' in src
        in_js = probe in js
        if in_js and not in_xhtml:
            problems.append(f"settings.js 引用了 {probe}，但 xhtml 里没有这个 id")
            print(f"  [XX] {probe}：js 要用但 xhtml 里缺")
        elif in_xhtml and in_js:
            print(f"  [OK] {probe}：两边都有")

    print()
    print("=" * 72)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("通过：这个文件能被 Zotero 正常加载成设置面板。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
