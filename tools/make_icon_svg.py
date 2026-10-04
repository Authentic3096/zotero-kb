"""把图标设计（`tools/icon-design-finetune.pptx` 里那份几何）导出成**插件用的 SVG**。

    <捆绑 python> tools/make_icon_svg.py            # 写 zotero-plugin/icon.svg 等
    <捆绑 python> tools/make_icon_svg.py --dry      # 只打印，不写文件

## 为什么不让 PowerPoint 直接导 SVG

PowerPoint 的 `Shape.Export(..., ppShapeFormatSVG)` 能用，但：
它会带出一堆 Office 命名空间、`<a:>` 残留、以及按 pt/EMU 写的尺寸 ——
对一个要长期维护、还要放进仓库的图标来说太脏。
这里的形状就四种（圆角矩形 / 直线 / 圆 / 弧），**自己生成又短又干净**。

## 单一设计源

设计常量（书的线、弧、椭圆、节点）全都从 `make_icon_pptx` import ——
**改设计只改那一处**，pptx 和 SVG 一起变，不会漂移。
（`compute_ellipse_arcs()` 也是共用的，保证两种格式断口一致。）

## 两个文件

    zotero-plugin/icon.svg           96×96，插件管理器 / 设置面板用
    zotero-plugin/toolbar-icon.svg   16×16，工具栏 / 右键菜单用
                                     （用 context-fill 跟随 Zotero 主题）
"""

from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import make_icon_pptx as M                                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "zotero-plugin")

STROKE = M.LW * 12700.0 / M.UNIT      # pt → 设计单位（1pt = 12700 EMU）


def f(v: float) -> str:
    """数字格式化：去掉多余小数，SVG 才短。"""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def pt_of_angle(cx, cy, r, deg):
    a = math.radians(deg)
    return (cx + r * math.cos(a), cy + r * math.sin(a))


def arc_path_circle(cx, cy, r, a0, a1):
    """圆弧 → SVG path。SVG 的 y 轴向下、角度顺时针为正，和我们的约定一致。"""
    x0, y0 = pt_of_angle(cx, cy, r, a0)
    x1, y1 = pt_of_angle(cx, cy, r, a1)
    span = (a1 - a0) % 360
    large = 1 if span > 180 else 0
    return (f"M{f(x0)} {f(y0)}A{f(r)} {f(r)} 0 {large} 1 {f(x1)} {f(y1)}")


def arc_path_ellipse(a0, a1):
    """椭圆弧 → SVG path（用 A 命令的 rx ry x-axis-rotation 描述）。"""
    e = M.ELLIPSE
    x0, y0 = M.world_of_angle(a0)
    x1, y1 = M.world_of_angle(a1)
    span = (a1 - a0) % 360
    large = 1 if span > 180 else 0
    return (f"M{f(x0)} {f(y0)}A{f(e['rx'])} {f(e['ry'])} {f(e['rot'])} "
            f"{large} 1 {f(x1)} {f(y1)}")


def build_icon_svg() -> str:
    e = M.ELLIPSE
    bx, by, bw, bh, br = M.BG
    rx_corner = bw * br          # adj 是"占短边的比例"

    parts = []
    parts.append('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" '
                 'viewBox="0 0 96 96">')
    parts.append("  <defs>")
    parts.append('    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">')
    parts.append('      <stop offset="0" stop-color="#14356B"/>')
    parts.append('      <stop offset="1" stop-color="#2B7FA6"/>')
    parts.append("    </linearGradient>")
    parts.append("  </defs>")
    parts.append(f'  <rect x="{f(bx)}" y="{f(by)}" width="{f(bw)}" '
                 f'height="{f(bh)}" rx="{f(rx_corner)}" fill="url(#bg)"/>')

    parts.append(f'  <g fill="none" stroke="#fff" stroke-width="{f(STROKE)}" '
                 'stroke-linecap="round" stroke-linejoin="round">')
    lines, arcs = M.book_lines_and_arcs()
    for x1, y1, x2, y2, _n in lines:
        parts.append(f'    <path d="M{f(x1)} {f(y1)}L{f(x2)} {f(y2)}"/>')
    for cx, cy, r, rot, a0, a1, _n in arcs:
        # 圆上的弧：整体旋转等价于把角度加上 rot，直接用世界角画
        parts.append(f'    <path d="{arc_path_circle(cx, cy, r, a0 + rot, a1 + rot)}"/>')
    for a0, a1 in M.compute_ellipse_arcs():
        parts.append(f'    <path d="{arc_path_ellipse(a0, a1)}"/>')
    parts.append("  </g>")

    parts.append(f'  <g fill="none" stroke="#fff" stroke-width="{f(STROKE)}">')
    for cx, cy in M.NODES:
        parts.append(f'    <circle cx="{f(cx)}" cy="{f(cy)}" r="{f(M.NODE_R)}"/>')
    parts.append("  </g>")
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def build_small_svg(size: int, stroke: float, node_r: float, scale: float,
                    what: str) -> str:
    """小尺寸图标（16×16 工具栏 / 20×20 内容窗格侧栏）。

    ⚠ 参数化是必须的：Zotero 的内容窗格 `sidenav.icon` 要 20×20，工具栏要
      16×16。手写两版近似必然漂移 —— 本项目已经因为"工具栏图标是手写近似"
      被用户一眼看出形状对不上。所以两个尺寸都从同一份几何生成，只调
      线宽/节点半径/整体放大倍数这三个小尺寸参数。

    `what` 只用于注释文字，说明这是哪个尺寸。
    """
    lines, arcs = M.book_lines_and_arcs()

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        'viewBox="0 0 96 96">',
        "  <!-- 由 tools/make_icon_svg.py 从主图标**同一份几何**生成，",
        f"       只按 {what} 做了简化（线加粗、整体放大、节点改实心点）。",
        "       避让关系与主图标完全一致：书的边在椭圆穿过处断开、",
        "       椭圆在节点和封面左上角处断开。",
        "       颜色用 context-fill，跟随 Zotero 的浅色/深色主题。 -->",
        f'  <g transform="translate(48 48) scale({scale}) translate(-48 -48)"',
        f'     fill="none" stroke="context-fill" stroke-width="{f(stroke)}" '
        'stroke-linecap="round" stroke-linejoin="round">',
    ]
    for x1, y1, x2, y2, _n in lines:
        parts.append(f'    <path d="M{f(x1)} {f(y1)}L{f(x2)} {f(y2)}"/>')
    for cx, cy, r, rot, a0, a1, _n in arcs:
        parts.append(f'    <path d="{arc_path_circle(cx, cy, r, a0 + rot, a1 + rot)}"/>')
    # 椭圆逐段画，**不要合并** —— 合并就把断口抹掉了
    for a0, a1 in M.compute_ellipse_arcs():
        parts.append(f'    <path d="{arc_path_ellipse(a0, a1)}"/>')
    parts += [
        "  </g>",
        f"  <!-- 节点：{what} 下只能做实心点 -->",
        f'  <g transform="translate(48 48) scale({scale}) translate(-48 -48)" '
        'fill="context-fill">',
    ]
    for cx, cy in M.NODES:
        parts.append(f'    <circle cx="{f(cx)}" cy="{f(cy)}" r="{f(node_r)}"/>')
    parts += ["  </g>", "</svg>"]
    return "\n".join(parts) + "\n"


def build_toolbar_svg() -> str:
    """16×16 版：工具栏 / 右键菜单用。

    ⚠ 踩过的第二个坑：为了"干净"，我把所有椭圆弧**合并成一条连续 path**、
    书的边也不断开 —— 结果**避让效果全没了**，两个图标看着还是不像。
    现在原样保留：书的边在椭圆穿过处断开、椭圆在节点和书角处断开。

    16px 下的两处必要简化：线加粗（主图标 1.29 单位 ≈ 0.21px，看不见）、
    节点改成实心点（半径 3.4 的空心圆在 16px 下只有一个像素）。
    """
    return build_small_svg(16, stroke=5.6, node_r=4.4, scale=1.18, what="16px")


def build_sidenav_svg() -> str:
    """20×20 版：Zotero 内容窗格侧栏（`sidenav.icon`）用。

    为什么单独要一版：`registerSection` 的 `header.icon` 与 `sidenav.icon`
    是两个不同的槽位（16 与 20 像素），Zotero 不会帮我们缩放；用 16px 的图
    去填 20px 的槽位会显得小一圈、和旁边自带图标不齐。
    """
    return build_small_svg(20, stroke=5.0, node_r=4.8, scale=1.15, what="20px")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    icon = build_icon_svg()
    tb = build_toolbar_svg()
    sn = build_sidenav_svg()
    if args.dry:
        print(icon)
        print(tb)
        print(sn)
        return 0

    for name, text in (("icon.svg", icon), ("toolbar-icon.svg", tb),
                       ("sidenav-icon.svg", sn)):
        p = os.path.join(PLUGIN, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"  {name:20} {len(text.encode('utf-8')):>6} 字节")
    print("已写入 zotero-plugin/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
