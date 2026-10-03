"""一次渲染多组椭圆参数，拼成对比图挑最好的一组（调参用，不产出正式设计）。

    <捆绑 python> tools/tune_ellipse.py

为什么值得单独写一个：椭圆与矩形"交几次"是解析上很难一眼看准的事 ——
交 4 次以上书就被切成碎线、读不出是书了。与其在纸上推，不如几组参数
各渲染一张、拼起来用眼睛挑。每组约 3 秒（要过一次 PowerPoint 导出）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import make_icon_pptx as M                                     # noqa: E402
from PIL import Image                                          # noqa: E402

# (名字, cx, cy, rx, ry, rot, 节点角)
CANDS = [
    ("B 大而正", 48, 46, 44, 33, -28, [10, 70, 130, 190, 250, 310]),
    ("G 右下移", 53, 51, 43, 31, -28, [10, 70, 130, 190, 250, 310]),
    ("H 更右下", 56, 54, 42, 30, -30, [10, 70, 130, 190, 250, 310]),
    ("I 更大",   50, 49, 46, 35, -28, [10, 70, 130, 190, 250, 310]),
    ("J 略窄",   52, 47, 41, 30, -30, [10, 70, 130, 190, 250, 310]),
    ("K 低平",   50, 53, 45, 32, -22, [0, 60, 120, 180, 240, 300]),
]

TMP = os.path.join(M.HERE, "_tune")
os.makedirs(TMP, exist_ok=True)


def render(i, name, cx, cy, rx, ry, rot, node_t):
    M.ELLIPSE.update(cx=cx, cy=cy, rx=rx, ry=ry, rot=rot)
    M.NODE_T = [float(t) for t in node_t]
    prs, n_arc, n_line = M.build()
    pptx = os.path.join(TMP, f"c{i}.pptx")
    png = os.path.join(TMP, f"c{i}.png")
    prs.save(pptx)
    M.export_png(pptx, png, 300)
    return Image.open(png).convert("RGB"), n_arc, n_line


def main() -> int:
    tiles = []
    for i, (name, cx, cy, rx, ry, rot, nt) in enumerate(CANDS):
        im, n_arc, n_line = render(i, name, cx, cy, rx, ry, rot, nt)
        tiles.append((name, im, n_arc, n_line))
        print(f"  {name:8} 弧 {n_arc} 段 / 直段 {n_line} 条")

    cols, pad = 3, 16
    w, h = tiles[0][1].size
    rows = (len(tiles) + cols - 1) // cols
    comp = Image.new("RGB", (pad + cols * (w + pad), pad + rows * (h + pad)),
                     (246, 246, 248))
    for k, (_n, im, _a, _l) in enumerate(tiles):
        r, c = divmod(k, cols)
        comp.paste(im, (pad + c * (w + pad), pad + r * (h + pad)))
    out = os.path.join(os.path.dirname(M.HERE), "sessions", "..", "..")
    out = os.path.join(M.HERE, "_tune_compare.png")
    comp.save(out)
    print("对比图：", out)
    print("排列：", " / ".join(t[0] for t in tiles))
    return 0


if __name__ == "__main__":
    sys.exit(main())
