"""把插件图标（SVG）渲染成 PNG —— **只为了肉眼验收，不参与打包**。

    python tools/preview_icon.py            # 出 icon + toolbar 的多尺寸对比图
    python tools/preview_icon.py --open     # 出完直接用系统看图器打开

## 为什么需要它

图标是 `zotero-plugin/icon.svg` 与 `toolbar-icon.svg`。
**Zotero 渲染 SVG 的结果我看不到** —— 而"发出去的东西必须是我亲眼确认过的"。
所以这里借 Edge 的无头模式（`--headless --screenshot`）把 SVG 渲染成 PNG，
再用多尺寸拼图检查：**16 / 32 / 48 / 96 四个尺寸下还认不认得出**。

## 两个必须处理的细节

1. **`context-fill` Edge 不认识** —— 那是 Zotero/Firefox 专有的值，
   浏览器里会当成无效颜色（渲染成全黑或透明）。所以渲染前先把它替换成
   一个具体颜色，并**分别出浅色/深色两版**，模拟 Zotero 的两种主题。
2. **透明背景** —— 加 `--default-background-color=00000000`，
   再自己铺一层棋盘/纯色底，才看得出图标本身有没有露出方角。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def render_svg(edge: str, svg_text: str, px: int, bg: str = "transparent",
               out: str = "") -> str:
    """用 Edge 无头模式把一段 SVG 渲染成 px×px 的 PNG，返回路径。

    ⚠ `--window-size` 只是缩小**窗口**，不会缩放 SVG —— 不把 svg 标签上的
    width/height 一起改掉的话，小尺寸拿到的只是**左上角一块**（第一版就这样，
    16/32/48px 出的都是"裁切图"）。
    """
    tmp = tempfile.mkdtemp(prefix="kb-icon-")
    html_p = os.path.join(tmp, "a.html")
    png_p = out or os.path.join(tmp, "a.png")
    # 把 svg 根标签的 width/height 改成目标尺寸（viewBox 不动 → 等比缩放）
    svg_scaled = re.sub(
        r'(<svg\b[^>]*?)\swidth="[^"]*"\s+height="[^"]*"',
        rf'\1 width="{px}" height="{px}"', svg_text, count=1)
    if f'width="{px}"' not in svg_scaled:
        svg_scaled = re.sub(r"<svg\b", f'<svg width="{px}" height="{px}"',
                            svg_scaled, count=1)
    body = re.sub(r"<\?xml[^>]*\?>", "", svg_scaled)
    with open(html_p, "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'>"
                f"<body style='margin:0;background:{bg}'>{body}</body>")
    cmd = [edge, "--headless", "--disable-gpu", "--hide-scrollbars",
           f"--screenshot={png_p}", f"--window-size={px},{px}",
           "--default-background-color=00000000",
           "file:///" + html_p.replace("\\", "/")]
    subprocess.run(cmd, capture_output=True, timeout=90)
    return png_p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="出图后用系统看图器打开")
    args = ap.parse_args()

    edge = find_edge()
    if not edge:
        print("[XX] 没找到 Edge —— 它是唯一能渲染 SVG 的家伙，装一个再来")
        return 1

    def load(name: str) -> str:
        with open(os.path.join(PLUGIN, name), encoding="utf-8") as f:
            return f.read()

    icon = load("icon.svg")
    toolbar = load("toolbar-icon.svg")

    # context-fill 在浏览器里无效 → 换成具体颜色，分别模拟浅色/深色主题
    def subst(svg: str, color: str) -> str:
        return svg.replace("context-fill", color)

    print("=== 主图标（插件管理器用）===")
    sizes = [16, 32, 48, 96]
    tiles = []
    for s in sizes:
        p = render_svg(edge, icon, s)
        tiles.append((s, Image.open(p).convert("RGBA")))
        print(f"  {s}px 渲染完成")

    pad, gap = 16, 18
    W = pad * 2 + sum(s for s, _ in tiles) + gap * (len(tiles) - 1)
    H = pad * 2 + max(s for s, _ in tiles) + 20
    comp = Image.new("RGB", (W, H), (244, 244, 246))
    x = pad
    for s, im in tiles:
        comp.paste(im, (x, pad + (max(ss for ss, _ in tiles) - s) // 2), im)
        x += s + gap
    out_main = os.path.join(PLUGIN, "_icon_preview.png")
    comp.save(out_main)
    print(f"  → {out_main}")

    print()
    print("=== 工具栏图标（16×16，模拟 Zotero 两种主题）===")
    # ⚠ 按行拼，别用一个自增的 x 跨行累计 —— 第一版那样写，
    #   换到第二行时 x 没有复位，深色主题那排被贴到画面外面去了。
    themes = (("浅色主题", "#3a3a3a", "#f4f4f6"),
              ("深色主题", "#e8e8e8", "#2b2b2e"))
    tb_sizes = (16, 32, 48)
    cell_w, cell_h, pad = 64, 64, 16
    W2 = pad * 2 + cell_w * len(tb_sizes)
    H2 = pad * 2 + cell_h * len(themes)
    comp2 = Image.new("RGB", (W2, H2), (250, 250, 252))
    for r, (label, color, bg) in enumerate(themes):
        # 整行铺主题底色，才看得出"跟随主题"到底跟成什么样
        row_y = pad + r * cell_h
        row = Image.new("RGB", (W2 - pad * 2, cell_h), bg)
        comp2.paste(row, (pad, row_y))
        for c, s in enumerate(tb_sizes):
            p = render_svg(edge, subst(toolbar, color), s, bg=bg)
            im = Image.open(p).convert("RGBA")
            x = pad + c * cell_w + (cell_w - s) // 2
            y = row_y + (cell_h - s) // 2
            comp2.paste(im, (x, y), im)
            print(f"  {label} {s}px 渲染完成")
    out_tb = os.path.join(PLUGIN, "_toolbar_preview.png")
    comp2.save(out_tb)
    print(f"  → {out_tb}  （每行一个主题，从左到右 16 / 32 / 48px）")
    print()
    print("提示：这两张 _*_preview.png 只是验收图，**不要提交、也不进 xpi**。")

    if args.open:
        for p in (out_main, out_tb):
            os.startfile(p)  # noqa: S606
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
