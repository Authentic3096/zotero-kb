"""量一下用户最终版里"椭圆弧端点"到"节点圆心"的**实际距离**（不靠角度换算）。

    <捆绑 python> tools/measure_final.py

为什么单独写：前面几次我都在角度上翻车 —— 参数角、真实角、局部角、世界角
混在一起算，结论一会儿一个样。这里干脆**只算点与点的直线距离**：
把用户画的每段弧按真实角采样成点，再直接量每个节点圆心到最近弧端的距离。
不涉及任何角度换算，就没有换算错的可能。
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 用户最终版里那个椭圆（从 pptx 读出来的）
CX, CY, RX, RY, ROT = 48.0, 40.6, 13.8, 37.6, 58.8
R = math.radians(ROT)

# 用户画的 5 段弧：局部真实角（度）
ARCS = [(58.20, 81.48), (91.39, 125.83), (249.38, 275.92),
        (284.11, 314.00), (335.04, 43.57)]

# 4 个节点圆心
NODES = [(78.5, 27.5), (63.8, 47.6), (41.0, 59.3), (16.8, 62.4)]
NODE_R = 3.4
HALF_LW = 0.65          # 线宽 3.8pt 在设计坐标里约 1.3，半宽 0.65


def world_of_angle(theta_deg: float):
    """真实角 → 椭圆上的世界坐标点（射线与椭圆求交）。"""
    a = math.radians(theta_deg)
    k = 1.0 / math.sqrt((math.cos(a) / RX) ** 2 + (math.sin(a) / RY) ** 2)
    u, v = k * math.cos(a), k * math.sin(a)
    return (CX + u * math.cos(R) - v * math.sin(R),
            CY + u * math.sin(R) + v * math.cos(R))


def arcs_to_points(step=0.5):
    """把每段弧采样成点列（世界坐标），用于找"离某点最近的弧端"。

    也顺便返回每段弧的**两个端点**单独一份 —— 断口只可能出现在端点处。
    """
    pts, ends = [], []
    for a0, a1 in ARCS:
        span = (a1 - a0) % 360
        n = max(2, int(span / step))
        seg = [world_of_angle((a0 + span * i / n) % 360) for i in range(n + 1)]
        pts += seg
        ends.append((seg[0], seg[-1]))
    return pts, ends


def main() -> int:
    pts, ends = arcs_to_points()
    all_ends = [p for pair in ends for p in pair]

    print(f"椭圆：圆心 ({CX}, {CY})  半轴 {RX} / {RY}  倾角 {ROT}°")
    xs = [world_of_angle(t)[0] for t in range(360)]
    ys = [world_of_angle(t)[1] for t in range(360)]
    print(f"      范围 x {min(xs):.1f}..{max(xs):.1f}   y {min(ys):.1f}..{max(ys):.1f}")
    print()
    print("节点 → 最近的**弧端**：")
    print("   节点圆心          最近弧端           端到圆心   净间隙(减半径和线宽)")
    worst = 0.0
    for q in NODES:
        best = min(all_ends, key=lambda p: math.hypot(p[0] - q[0], p[1] - q[1]))
        d = math.hypot(best[0] - q[0], best[1] - q[1])
        clear = d - NODE_R - HALF_LW
        worst = max(worst, abs(clear - 1.5))
        print(f"   ({q[0]:5.1f},{q[1]:5.1f})   ({best[0]:5.1f},{best[1]:5.1f})"
              f"      {d:5.2f}      {clear:+5.2f}")
    print()
    print(f"（净间隙 = 到圆心距离 − 节点半径 {NODE_R} − 半线宽 {HALF_LW}）")
    print(f"  净间隙为负 = 弧端戳进节点圆里；各处不一致 = 看着忽宽忽窄")
    print(f"  当前最大偏差 {worst:.2f}（以 1.5 为理想值）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
