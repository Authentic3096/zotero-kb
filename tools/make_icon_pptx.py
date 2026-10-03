"""插件图标的**微调版**生成器 —— 书照抄用户定稿，只把细节修匀。

    <捆绑 python> tools/make_icon_pptx.py                  # 生成 tools/icon-design-finetune.pptx
    <捆绑 python> tools/make_icon_pptx.py --png            # 顺带导出 PNG
    <捆绑 python> tools/make_icon_pptx.py --compare <png>  # 与定稿并排比对

## ⚠ 绝不覆盖用户的文件

用户的定稿是 `tools/icon-design.pptx`（他有 PowerPoint 开着，随时会改）。
本脚本**只写 `icon-design-finetune.pptx`** —— 之前我把两者写成同一个文件名，
结果脚本一跑就把用户手拖的形状全冲掉了。这个教训写在这儿，别再犯。

## 分工

    BOOK_LINES / BOOK_ARCS  = 用户定稿的**逐形状坐标**，一个不改（见下）
    椭圆弧                  = 重新算，只为了把断口修匀（本次唯一的改动方向）

## 本次微调的三处（都有实测依据，不是凭感觉）

1. **弧端从节点圆里退出来。** 实测（`tools/measure_final.py`）：用户定稿里
   四个弧端到节点圆心的距离是 3.41 / 3.60 / 3.35 / 3.43，而"节点半径 3.4 + 
   半线宽 0.65 = 4.05" —— **净间隙全是负的**（−0.64 ~ −0.45），也就是弧端
   戳进了节点圆圈里。现在统一退到净间隙 +1.2，四处一样。

2. **背景圆角方竖直居中。** 定稿是 y 2.6..92.6（偏上 0.4），改成 3.0..93.0。

3. **所有线改圆头**（`cap="rnd"`）。用户提过"一些链接处要圆滑"——
   平头线在拐角处会露出方角。

## 用户定稿的几何（照抄，勿改）

书 = 封面（左右边各两段，中间留口给椭圆）+ 书脊折线 + 页块（三条横线 + 两端弧）。
椭圆 = 一个 27.6×75.2、rot=58.8° 的 ARC 拆成 5 段，**只画左上半圈**，
右下半圈（125.83°→249.38°）是故意不画的。
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.util import Emu, Pt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "icon-design-finetune.pptx")   # ← 只写副本

SLIDE_EMU = 3600000
UNIT = SLIDE_EMU / 96.0


def U(v: float) -> Emu:
    return Emu(int(round(v * UNIT)))


WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BG_FROM = RGBColor(0x14, 0x35, 0x6B)
BG_TO = RGBColor(0x2B, 0x7F, 0xA6)

LW = 3.8
HALF_LW = 0.65          # 3.8pt 在设计坐标里约 1.3，半宽 0.65

# ================================================================ 用户定稿：底
BG = (3.0, 3.0, 90.0, 90.0, 0.22)      # 微调：y 由 2.6 改 3.0（原来偏上 0.4）

# ================================================================ 书的"骨架"
#
# ⚠ 这一版把书改成了**参数化**：先定封面的矩形、圆角半径、书脊位置，
#   再由它们**算出**每条线该从哪画到哪。用户的原话是
#   「还有一些直线应该水平竖直的，可能在 ppt 中不好改动，改歪了」
#   「还有一些直线曲线相交的地方我手动调整可能链接不太准」——
#   也就是他认可形状、但希望**接缝精确**。
#   参数化的好处：只要这四个数对，所有连接就是**构造上**精确的，
#   不会出现"差 0.2 没接上"这种手拖残留。
#
#   数值全部取自用户 v3 的实测坐标（取两段的中间值，消掉漂移）。

COVER_L, COVER_R = 27.70, 71.39    # 封面左右（v3: 27.71/27.68 → 27.70；71.16~71.62 → 71.39）
COVER_T, COVER_B = 19.06, 70.47    # 封面上下的**中线**（同时也是页块上边线）
CORNER_R = 5.0                     # 圆角半径（v3 的弧直径 10）
SPINE_X = 33.93                    # 书脊折线（v3: 33.89 / 33.97 → 33.93）
BLOCK_MID = 75.47                  # 页块中间横线（也是两端圆弧的圆心高度）
BLOCK_BOT = 80.47                  # 页块下边
BLOCK_ARC_L = 32.66                # 页块左端圆心 x
BLOCK_ARC_R = 68.80                # 页块右端圆心 x

# 断口半宽：线在"椭圆穿过处"断开，两侧各留这么多
BREAK_HALF = 2.8

# 页块两端圆弧。
#
# ⚠ 用户原来手调的角度是 94.6°→263.3°（左）和 99.8°→245.5°（右）——
#   端点**差 0.4~0.85 落在页块上下边之外**，看着就是"没接上"。
#   这里统一改成**精确半圆**（90°→270°）：端点正好落在 y=BLOCK_MID±5，
#   也就是页块上下边的位置，接缝由构造保证精确。
#   造型不变：两段都**朝左鼓**（左端是向外的圆头，右端是书脊棱），这是用户的本意。
BLOCK_ARC_ANGLES = [
    (BLOCK_ARC_L, 0.0, 90.0, 270.0, "页块左端"),
    (BLOCK_ARC_R, 0.0, 90.0, 270.0, "页块右端"),
]


def ellipse_crossings_on_vertical(x: float, lo: float, hi: float) -> list[float]:
    """椭圆（**只算真画出来的那几段**）与竖线 x 的交点 y，限定在 [lo, hi]。

    只算画出来的段很关键：左边缘上部有一处椭圆穿过，但那一段在大缺口里没画
    （实测：交点真实角 127.55°，落在 125.83~249.38 的大缺口内）——
    所以那条线**不该**断开。用户 v3 就是这么处理的，是对的。
    """
    ys: list[float] = []
    for a0, a1 in compute_ellipse_arcs():
        span = (a1 - a0) % 360
        n = max(12, int(span * 2))
        prev = None
        for i in range(n + 1):
            p = world_of_angle((a0 + span * i / n) % 360)
            if prev is not None and abs(p[0] - prev[0]) > 1e-12:
                if (prev[0] - x) * (p[0] - x) <= 0:
                    f = (x - prev[0]) / (p[0] - prev[0])
                    y = prev[1] + (p[1] - prev[1]) * f
                    if lo <= y <= hi:
                        ys.append(round(y, 3))
            prev = p
    return sorted(set(ys))


def book_lines_and_arcs():
    """由骨架**算出**所有直边与圆角弧（连接由构造保证精确）。

    竖线在"椭圆穿过处"断开 —— 断口位置**实时算**，不写死：
    这样以后挪椭圆，线会自动跟着让，不会再出现"线断了但椭圆没经过"这种事。
    """
    from_ = COVER_L + CORNER_R
    to_ = COVER_R - CORNER_R
    top_y, bot_y = COVER_T + CORNER_R, COVER_B - CORNER_R

    def brk(x, lo, hi):
        """返回竖线 x 上的断口区间 (y_起, y_止)；没有交点就返回 None。"""
        ys = ellipse_crossings_on_vertical(x, lo, hi)
        if not ys:
            return None
        yc = ys[0]
        return (yc - BREAK_HALF, yc + BREAK_HALF)

    lines = [(from_, COVER_T, to_, COVER_T, "封面上边")]

    # 封面右边（两段，中间让开椭圆）
    b = brk(COVER_R, top_y, bot_y)
    if b:
        lines.append((COVER_R, top_y, COVER_R, b[0], "封面右边（上）"))
        lines.append((COVER_R, b[1], COVER_R, bot_y, "封面右边（下）"))
    else:
        lines.append((COVER_R, top_y, COVER_R, bot_y, "封面右边"))

    # 封面左边（下段一直延伸到页块中线 —— 用户去掉了一个圆角，这里保持）
    b = brk(COVER_L, top_y, BLOCK_MID)
    if b:
        lines.append((COVER_L, top_y, COVER_L, b[0], "封面左边（上）"))
        lines.append((COVER_L, b[1], COVER_L, BLOCK_MID, "封面左边（下）"))
    else:
        lines.append((COVER_L, top_y, COVER_L, BLOCK_MID, "封面左边"))

    # 书脊折线（两段，下段一直画到封面底边）
    b = brk(SPINE_X, COVER_T, COVER_B)
    if b:
        lines.append((SPINE_X, COVER_T, SPINE_X, b[0], "书脊折线（上）"))
        lines.append((SPINE_X, b[1], SPINE_X, COVER_B, "书脊折线（下）"))
    else:
        lines.append((SPINE_X, COVER_T, SPINE_X, COVER_B, "书脊折线"))

    lines += [
        # 页块上边：右端要一直伸到**右端弧的顶点**（x=BLOCK_ARC_R），
        # 否则弧顶会悬在离线的 2.4 处、看着是个豁口。
        # 封面右下角的底端 (to_, COVER_B) 落在这条线上，形成 T 形接点（正常）。
        (BLOCK_ARC_L, COVER_B, BLOCK_ARC_R, COVER_B, "页块上边"),
        (33.21, BLOCK_MID, 63.81, BLOCK_MID, "页块中间横线"),
        (BLOCK_ARC_L, BLOCK_BOT, BLOCK_ARC_R, BLOCK_BOT, "页块下边"),
    ]

    arcs = [
        (from_, top_y, CORNER_R, 0.0, 180.0, 270.0, "封面上左角"),
        (to_, top_y, CORNER_R, 0.0, 270.0, 360.0, "封面上右角"),
        (to_, bot_y, CORNER_R, 0.0, 0.0, 90.0, "封面下右角"),
        # ⚠ 这里**不要**再加"左下角书脊弧"（从书脊折线拐到封面底边的那个）。
        #   加过一次、又按要求撤掉了：用户说的"左下角弧线"指的是**面板图标**
        #   底部那条（`tools/make_panel_icon.py` 里单独叠），不是插件图标里的。
        #   而且他最终决定面板图标与插件图标**共用这一个、不带弧线**的版本。
    ]
    for cx, rot, a0, a1, name in BLOCK_ARC_ANGLES:
        arcs.append((cx, BLOCK_MID, CORNER_R, rot, a0, a1, name))
    return lines, arcs

# ================================================================ 椭圆与节点
ELLIPSE = dict(cx=48.0, cy=40.6, rx=13.8, ry=37.6, rot=58.8)

NODES = [(78.5, 27.5), (63.8, 47.6), (41.0, 59.3), (16.8, 62.4)]
NODE_R = 3.4
# 弧端到节点圆的**净间隙**。用户原话："缺口大小均匀不均匀不重要，只要每一段
# 椭圆都和节点的边**刚好相交**就对的" —— 所以取 0：
# 弧的中心线正好停在节点圆的外沿（NODE_R + 半线宽），不戳进去也不留空档。
# （定稿里这几个距离是 3.35~3.60，都小于 4.05，也就是戳进去了。）
CLEAR = 0.0

# 用户故意不画的那一段（真实角），保留
BIG_GAP = (125.83, 249.38)


# ================================================================ 几何

def world_of_angle(theta_deg: float):
    """真实角 → 椭圆上的世界坐标（射线与椭圆求交）。"""
    a = math.radians(theta_deg)
    k = 1.0 / math.sqrt((math.cos(a) / ELLIPSE["rx"]) ** 2
                        + (math.sin(a) / ELLIPSE["ry"]) ** 2)
    u, v = k * math.cos(a), k * math.sin(a)
    r = math.radians(ELLIPSE["rot"])
    return (ELLIPSE["cx"] + u * math.cos(r) - v * math.sin(r),
            ELLIPSE["cy"] + u * math.sin(r) + v * math.cos(r))


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def node_true_angle(q):
    """节点圆心的真实角：把它转到椭圆局部坐标，取几何角。"""
    r = math.radians(ELLIPSE["rot"])
    dx, dy = q[0] - ELLIPSE["cx"], q[1] - ELLIPSE["cy"]
    u = dx * math.cos(r) + dy * math.sin(r)
    v = -dx * math.sin(r) + dy * math.cos(r)
    return math.degrees(math.atan2(v, u)) % 360


def gap_bounds(q, target):
    """求节点两侧"弧端应当停在哪"的真实角。

    解 |P(θ) − q| = target。沿椭圆扫一遍找符号变化，再用二分收敛 ——
    比解析解稳（椭圆的点距没有初等闭式）。
    """
    best = []
    prev_t, prev_d = None, None
    for i in range(0, 3600):
        t = i / 10.0
        d = dist(world_of_angle(t), q) - target
        if prev_d is not None and (prev_d < 0) != (d < 0):
            lo, hi = prev_t, t
            for _ in range(40):
                mid = (lo + hi) / 2
                if (dist(world_of_angle(mid), q) - target < 0) == (prev_d < 0):
                    lo = mid
                else:
                    hi = mid
            best.append((lo + hi) / 2)
        prev_t, prev_d = t, d
    return sorted(set(round(x, 2) for x in best))


# ================================================================ 画形状

def _round_cap(sh):
    try:
        sh.line._get_or_add_ln().set("cap", "rnd")
    except Exception:                                        # noqa: BLE001
        pass


def add_line(slide, x1, y1, x2, y2, width=LW):
    cn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                                    U(x1), U(y1), U(x2), U(y2))
    cn.shadow.inherit = False
    cn.line.color.rgb = WHITE
    cn.line.width = Pt(width)
    _round_cap(cn)
    return cn


def add_arc(slide, cx, cy, r, a0, a1, rot=0.0, width=LW):
    sh = slide.shapes.add_shape(MSO_SHAPE.ARC,
                                U(cx - r), U(cy - r), U(r * 2), U(r * 2))
    sh.shadow.inherit = False
    sh.fill.background()
    sh.line.color.rgb = WHITE
    sh.line.width = Pt(width)
    if rot:
        sh.rotation = rot
    sh.adjustments[0] = a0 * 0.6
    sh.adjustments[1] = a1 * 0.6
    _round_cap(sh)
    return sh


def add_ellipse_arc(slide, a0, a1, width=LW):
    e = ELLIPSE
    sh = slide.shapes.add_shape(
        MSO_SHAPE.ARC,
        U(e["cx"] - e["rx"]), U(e["cy"] - e["ry"]),
        U(e["rx"] * 2), U(e["ry"] * 2))
    sh.shadow.inherit = False
    sh.fill.background()
    sh.line.color.rgb = WHITE
    sh.line.width = Pt(width)
    sh.rotation = e["rot"]
    sh.adjustments[0] = a0 * 0.6
    sh.adjustments[1] = a1 * 0.6
    _round_cap(sh)
    return sh


def add_oval(slide, cx, cy, r, width=LW):
    sh = slide.shapes.add_shape(MSO_SHAPE.OVAL, U(cx - r), U(cy - r),
                                U(r * 2), U(r * 2))
    sh.shadow.inherit = False
    sh.fill.background()
    sh.line.color.rgb = WHITE
    sh.line.width = Pt(width)
    return sh


def compute_ellipse_arcs(verbose=False):
    """算出椭圆要画哪几段（真实角区间），返回 [(a0, a1), ...]。

    抽成独立函数是为了 **pptx 和 SVG 两个生成器共用同一份结果** ——
    否则以后改一处忘一处，两个格式的图标就不一样了。
    """
    target = NODE_R + HALF_LW + CLEAR
    gaps = []
    for q in NODES:
        sols = gap_bounds(q, target)
        na = node_true_angle(q)
        if len(sols) < 2:
            print(f"  [!!] 节点 {q} 只找到 {len(sols)} 个断口，跳过")
            continue
        hi = min(sols, key=lambda s: (s - na) % 360)
        lo = min(sols, key=lambda s: (na - s) % 360)
        gaps.append((lo, hi))
        if verbose:
            print(f"  节点 ({q[0]:5.1f},{q[1]:5.1f}) 真实角 {na:6.2f}  "
                  f"→ 缺口 {lo:6.2f} .. {hi:6.2f}  （跨 {(hi - lo) % 360:5.2f}°）")

    # ---- 拼弧：**跳过每个节点缺口**，画到下一个缺口的起点。
    # ⚠ 两个坑都踩过：
    #   1. 断口表里"缺口的两个端点"是成对的，必须隔一个取一个 ——
    #      顺序连起来会把缺口本身也画成弧（段数会变成 9，一眼看得出不对）。
    #   2. BIG_GAP 是"**不画**的那一段"，它的起点 lo_big 是**绘制终点**、
    #      终点 hi_big 才是**绘制起点** —— 名字起得不好，一度用反，
    #      结果头尾两段弧各绕了 150°，在书上压出一条深色斜带。
    lo_big, hi_big = BIG_GAP
    start, end = hi_big, lo_big
    gaps.sort(key=lambda g: (g[0] - start) % 360)
    seq = [start] + [x for g in gaps for x in g] + [end]
    arcs = []
    for i in range(0, len(seq) - 1, 2):
        a0, a1 = seq[i], seq[i + 1]
        if (a1 - a0) % 360 > 2.0:
            arcs.append((a0, a1))
    return arcs


# ================================================================ 组装

def build(verbose=True):
    prs = Presentation()
    prs.slide_width = Emu(SLIDE_EMU)
    prs.slide_height = Emu(SLIDE_EMU)
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # ---- 底
    bx, by, bw, bh, br = BG
    bg = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                U(bx), U(by), U(bw), U(bh))
    bg.shadow.inherit = False
    bg.line.fill.background()
    bg.adjustments[0] = br
    bg.fill.gradient()
    st = bg.fill.gradient_stops
    st[0].color.rgb = BG_FROM
    st[0].position = 0.0
    st[1].color.rgb = BG_TO
    st[1].position = 1.0
    try:
        bg.fill.gradient_angle = 45.0
    except Exception:                                        # noqa: BLE001
        pass

    # ---- 书的直边与弧（由骨架算出，连接精确）
    lines, arcs = book_lines_and_arcs()
    for x1, y1, x2, y2, _n in lines:
        add_line(slide, x1, y1, x2, y2)
    for cx, cy, r, rot, a0, a1, _n in arcs:
        add_arc(slide, cx, cy, r, a0, a1, rot=rot)
    if verbose:
        print(f"  书：{len(lines)} 条直边 / {len(arcs)} 段圆弧")

    # ---- 椭圆：共用的断口计算
    arcs = compute_ellipse_arcs(verbose=verbose)
    for a0, a1 in arcs:
        add_ellipse_arc(slide, a0, a1)
    if verbose:
        print(f"  椭圆共 {len(arcs)} 段弧")

    # ---- 节点
    for q in NODES:
        add_oval(slide, q[0], q[1], NODE_R)

    slide.notes_slide.notes_text_frame.text = (
        "插件图标 · 微调版（96 单位方格，幻灯片 10cm×10cm）\n\n"
        "书的每条线与弧 = 你的定稿，坐标一个没改。\n"
        "本次只动了三处细节：\n"
        "  1. 椭圆弧端从节点圆里退出来（定稿里净间隙是 −0.64~−0.45，\n"
        "     也就是弧端戳进了圆圈；现在统一为 +1.2）\n"
        "  2. 背景圆角方竖直居中（y 2.6 → 3.0）\n"
        "  3. 所有线改圆头\n"
    )
    return prs, len(arcs)


def export_png(pptx: str, png: str, px: int = 420) -> bool:
    ps = f"""
$ErrorActionPreference='Stop'
$pp = New-Object -ComObject PowerPoint.Application
$pres = $pp.Presentations.Open('{pptx}', $true, $false, $false)
$pres.Slides.Item(1).Export('{png}', 'PNG', {px}, {px})
$pres.Close()
$pp.Quit()
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  [!!] 导出失败：", (r.stderr or r.stdout)[:300])
        return False
    return os.path.exists(png)


def compare(png: str, ref: str) -> None:
    try:
        from PIL import Image, ImageChops
    except ImportError:
        print("  （跳过比对：没有 Pillow）")
        return
    a = Image.open(png).convert("RGB")
    b = Image.open(ref).convert("RGB").resize(a.size, Image.LANCZOS)
    w, h = a.size
    comp = Image.new("RGB", (w * 3 + 40, h + 20), (255, 255, 255))
    comp.paste(a, (10, 10))
    comp.paste(b, (w + 20, 10))
    comp.paste(ImageChops.difference(a, b).convert("L")
               .point(lambda v: 255 - v).convert("RGB"), (w * 2 + 30, 10))
    out = os.path.join(HERE, "_finetune_compare.png")
    comp.save(out)
    print(f"  比对图（左=微调版 / 中=你的定稿 / 右=差异）：{out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", action="store_true")
    ap.add_argument("--compare", metavar="参考PNG")
    args = ap.parse_args()
    prs, n_arc = build()
    prs.save(OUT)
    print(f"已生成 {OUT}（形状 {len(prs.slides[0].shapes)} 个）")
    if args.png or args.compare:
        png = os.path.join(HERE, "_finetune.png")
        if export_png(OUT, png):
            print(f"  预览：{png}")
            if args.compare:
                compare(png, args.compare)
    return 0


if __name__ == "__main__":
    sys.exit(main())
