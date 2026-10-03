"""把**工具栏图标**（不是那个蓝底的插件图标）做成管理面板能用的位图。

    <捆绑 python> tools/make_panel_icon.py

## 为什么用工具栏那套、而不是插件图标

面板窗口的标题栏是**深色**（跟随系统强调色），把蓝底白线的插件图标缩到
16px 上去，细白线糊在蓝底里、整体又偏暗 —— 用户的原话是"太糊了"。
工具栏那套是**纯线条、线更粗、图形更大**，缩到 16px 反而清楚。

## 为什么把颜色写死成白

工具栏 SVG 里用的是 `context-fill`（Zotero 按主题给值），**Edge 不认**，
所以渲染时必须换成一个具体颜色。这里写死**浅色**（`#FFFFFF`）——
面板标题栏是深色的，白线才看得见。

⚠ 换浅色系统主题后标题栏会变亮，白线就看不见了。真到那时候把
   `PANEL_COLOR` 改成深色（例如 `#1F2937`）重跑一次即可。

## 两个文件都给（Tk 的两种设图标方式各要一种）

    tools/assets/icon.png    256×256，`root.iconphoto()` 用（窗口左上角）
    tools/assets/icon.ico    多尺寸 ico，`root.iconbitmap()` 用（任务栏/Alt-Tab）

`iconphoto` 在部分 Windows 主题下不生效，`iconbitmap` 更稳；两个都设最保险
（面板代码里就是两个都试）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import preview_icon as P                                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")

PANEL_COLOR = "#FFFFFF"          # 深色标题栏 → 白线
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> int:
    edge = P.find_edge()
    if not edge:
        print("[XX] 没找到 Edge —— 它负责渲染 SVG，装一个再来")
        return 1

    import make_icon_svg as S                                  # noqa: PLC0415

    os.makedirs(ASSETS, exist_ok=True)
    # 用工具栏那套几何（线条、无底、图形更大），只把 context-fill 换成白色
    svg = S.build_toolbar_svg().replace("context-fill", PANEL_COLOR)

    png = os.path.join(ASSETS, "icon.png")
    P.render_svg(edge, svg, 256, out=png)
    print(f"  icon.png  {os.path.getsize(png):>7} 字节  256×256（白线条，无底）")

    try:
        from PIL import Image                                  # noqa: PLC0415
    except ImportError:
        print("  （没有 Pillow，跳过 ico —— 面板会退化成只用 png）")
        return 0

    frames = []
    for s in ICO_SIZES:
        p = os.path.join(ASSETS, f"_i{s}.png")
        P.render_svg(edge, svg, s, out=p)
        frames.append(Image.open(p).convert("RGBA"))
        os.remove(p)
    ico = os.path.join(ASSETS, "icon.ico")
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  icon.ico  {os.path.getsize(ico):>7} 字节  含 {len(ICO_SIZES)} 种尺寸")
    return 0


if __name__ == "__main__":
    sys.exit(main())
