"""为管理页面建 10 个页签的**占位文件**（一页一文件，各自注册到 KBAdmin）。

为什么占位也要先生成：`admin.html` 里已经 `<script src="admin-pages/<id>.js">` 全列上了，
缺一个就是 404（页面会静默少一页）。占位文件让"壳先能跑、页面逐个填"这件事成立，
也让并行改不同页的人（agent）各占一个文件、互不撞车。

    & <venv>\\Scripts\\python.exe tools\\admin_pages_stub.py          # 只打印
    ...  --write                                                      # 真写（不覆盖已有内容）
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ → plugins/zotero-kb/ → zotero-plugin/content/admin-pages
# ⚠ 少写一层 dirname 会写到 tools/zotero-plugin/… 去（第一版就是）
OUT = os.path.join(os.path.dirname(HERE), "zotero-plugin", "content", "admin-pages")

# id, 菜单标题, 排序, 一句话职责（来自 Tk 面板各 tab）
PAGES = [
    ("structure", "知识库结构", 10, "库在哪、每个文件夹装什么、能删吗、备份/更新入口"),
    ("experience", "经验库", 20, "经验的增删改查与失效引用体检"),
    ("suggest", "分类建议", 30, "本地模型给出的分类/标签建议，采纳或丢弃"),
    ("env", "运行环境", 40, "每个路径/组件从哪来、是否可用、谁覆盖了谁"),
    ("parse", "PDF 解析", 50, "MinerU 解析状态与补跑"),
    ("quality", "损坏查询", 60, "损坏/异常的全文与公式清单"),
    ("meta", "元数据", 70, "批量补元数据与来源核对"),
    ("outline", "分节纲要", 80, "分节纲要的生成与现状"),
    ("advanced", "高级", 90, "低频与危险操作（全量重建、清理、对账）"),
    ("prompts", "提示词", 100, "本地模型提示词的查看与编辑"),
]

TEMPLATE = """/* ============================================================================
 * admin-pages/{id}.js —— 「{title}」页（{duty}）
 *
 * ⚠ 这是**占位**：先把文件占住（admin.html 里已按名字 <script src> 它，
 *   缺文件会 404、页面静默少一页），内容由负责这一页的人填。
 *
 * 实现约定（照抄 admin.js 顶部那三条 + 下面这段）：
 *   · 只画自己这一页，取数一律 `KBAdmin.get/post(...)`（**不要**直接 fetch 8765，
 *     服务端故意不给带 Origin 的请求发 token）；
 *   · 长任务用 `KBAdmin.runTask({{...}})`（提交 → 轮询 /jobs/<id> → 画进度）；
 *   · 破坏性操作：按钮用 `btn-danger` + `KBAdmin.dialog(...)` 二次确认；
 *   · 样式只用 admin.css 里的类（.card / .row / .btn 系列 / .grid / .note 系列 /
 *     .num / .mono …），不要再写颜色字面量 —— 那套令牌与 Tk 侧 theme.py 是同名的；
 *     ⚠ **块注释里绝对不要出现"星号紧接斜杠"那两个字符**（哪怕是在举例）——
 *       它会把块注释提前闭合，后面全成了裸代码，整份脚本语法错误、静默不注册
 *       （这个模板第一版就是这么坏的，而且还在注释里又写了一遍，坏了两次）。
 *   · 文案：说清"这是什么、点了会怎样"，不要解释性长句（用户要"干净"）。
 * ==========================================================================*/
(function () {{
  "use strict";
  KBAdmin.page({{
    id: "{id}",
    title: "{title}",
    order: {order},
    async render(host, ctx) {{
      host.appendChild(ctx.el("div", {{ class: "card" }}, [
        ctx.el("div", {{ class: "row" }}, [
          ctx.el("strong", {{ text: "{title}" }}),
          ctx.el("span", {{ class: "faint", text: "{duty}" }}),
        ]),
        ctx.el("div", {{ class: "sep" }}),
        ctx.el("div", {{ class: "note note-warn", text: "这一页还没实现（占位）。" }}),
      ]));
    }},
  }});
}})();
"""


def main() -> int:
    write = "--write" in sys.argv
    os.makedirs(OUT, exist_ok=True) if write else None
    print(f"目标目录：{OUT}")
    for pid, title, order, duty in PAGES:
        path = os.path.join(OUT, pid + ".js")
        exists = os.path.exists(path)
        body = TEMPLATE.format(id=pid, title=title, order=order, duty=duty)
        act = "已存在（跳过）" if exists else ("写出" if write else "将写出")
        print(f"  {act:14} {pid + '.js':18} {title}")
        if write and not exists:
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
    if not write:
        print("\n（只打印；加 --write 真写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
