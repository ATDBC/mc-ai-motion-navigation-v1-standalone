"""E105：中间平台多长时，静止起步也能跨过 4 格间隙（平地）。

复用 E98 的地形和计算器（同目录 e98_momentum_feasibility.py，只调用项目的 step()）。
W1=1、W2=4、DY=0，中间平台长度 P=1..6：报告后缘静止起跑能否跨过、需要先跑几 tick、
以及最靠前的可行静止起点。用来给 D118 的“短平台”下定义，并给出不需要接续的对照地形。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, standstill  # noqa: E402


def main():
    for p in range(1, 7):
        result = standstill(Course(1, p, 4, 0))
        print(f"P={p}（中间平台 z∈{result['platform_z']}）：后缘静止起跑的起跳 tick={result['back_edge_jump_tick']}，"
              f"最靠前的可行静止起点 z={result['frontmost_feasible_start_z']}")


if __name__ == "__main__":
    main()
