"""E127：D124 剩下的南向 3 个必要格 (0,62,z)，z=0、2、5，能从哪里看到。

项目的空气确认规则（`SurfaceSensor` → native `framePoseAir`）是：在该格放一块假想方块，
若从当前眼睛能看到它的一部分（16 格内、未被不透明几何遮挡），就记为 visible_air。
本脚本只做必要条件的几何检查：从眼睛到假想方块表面任一采样点的线段，
是否穿过 D120／D122 夹具的不透明支撑方块（项目 `trial_fixture_rows` 声明的 support 行）。
每段连续平台合并为一个不透明盒子（相邻方块之间的接缝不透光）。
线段全部被挡，native 核心也不可能看到；线段未被挡只说明“可能看到”，
还要满足视锥、像素面积等条件，所以“可见”一栏是乐观上界。

站位：在起跑平台（local z<0）和中间平台（local z∈[1,2)）上所有合法站立中心
（身体 0.6 宽的碰撞箱与支撑方块有重叠），x、z 步长 0.2（另含平台边缘的 z=0.29、2.29）；眼高取站立 1.62 和潜行 1.27。
另单独报告 D122 的实际起跑点 (0.5, 64, -5.17)。
用法：python e127_lower_cell_sight_lines.py   （在检出根目录运行）
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from scripts import d118_momentum_calibration_runtime as frozen  # noqa: E402
from scripts import d120_momentum_entry_calibration as calibration  # noqa: E402

FEET_Y = 64
CELLS = [(0, 62, z) for z in (-6, -5, -4, -3, -2, -1, 0, 1, 2, 5, 6, 7)]
NECESSARY = {(0, 62, 0), (0, 62, 2), (0, 62, 5)}
EYES = (1.62, 1.27)
RANGE = 16.0


def support_blocks():
    trial = calibration.build_fixture(0).trial
    rows = {row["phase"]: row for row in frozen.trial_fixture_rows(trial, origin=(0, 0), feet_y=FEET_Y)}
    return [tuple(p) for p in rows["support"]["positions"]]


def occluder_boxes(blocks):
    """Merge each contiguous platform into one box: seams between opaque blocks are not see-through."""
    xs = sorted({b[0] for b in blocks})
    zs = sorted({b[2] for b in blocks})
    if any((x, FEET_Y - 1, z) not in set(blocks) for x in xs for z in zs if (xs[0], FEET_Y - 1, z) in set(blocks)):
        raise AssertionError("platform rows are expected to span the same x range")
    boxes, run = [], []
    for z in zs + [None]:
        if run and (z is None or z != run[-1] + 1):
            boxes.append(((xs[0], FEET_Y - 1, run[0]), (xs[-1] + 1, FEET_Y, run[-1] + 1)))
            run = []
        if z is not None:
            run.append(z)
    return boxes


def segment_hits_box(a, b, box_min, box_max):
    """Open-interior slab test: does segment a→b pass through the box interior?"""
    t0, t1 = 0.0, 1.0
    for axis in range(3):
        d = b[axis] - a[axis]
        lo, hi = box_min[axis], box_max[axis]
        if abs(d) < 1e-12:
            if not lo < a[axis] < hi:
                return False
            continue
        u0, u1 = (lo - a[axis]) / d, (hi - a[axis]) / d
        if u0 > u1:
            u0, u1 = u1, u0
        t0, t1 = max(t0, u0), min(t1, u1)
        if t1 - t0 <= 1e-9:
            return False
    return True


def cube_samples(cell, n=5):
    x0, y0, z0 = cell
    grid = [(i + .5) / n for i in range(n)]
    points = []
    for u in grid:
        for v in grid:
            points += [(x0, y0 + u, z0 + v), (x0 + 1, y0 + u, z0 + v),
                       (x0 + u, y0, z0 + v), (x0 + u, y0 + 1, z0 + v),
                       (x0 + u, y0 + v, z0), (x0 + u, y0 + v, z0 + 1)]
    return points


def visible(eye, cell, blocks):
    for point in cube_samples(cell):
        if math.dist(eye, point) > RANGE:
            continue
        lo = [min(eye[i], point[i]) for i in range(3)]
        hi = [max(eye[i], point[i]) for i in range(3)]
        if not any(segment_hits_box(eye, point, low, high) for low, high in blocks
                   if all(low[i] < hi[i] and high[i] > lo[i] for i in range(3))):
            return True
    return False


def standing_centers(blocks, z_low, z_high):
    tops = {(b[0], b[2]) for b in blocks if b[1] == FEET_Y - 1}
    centers = []
    for xi in range(-14, 29, 2):
        for z in sorted({round(z_low + 0.2 * i, 2) for i in range(int((z_high - z_low) / 0.2) + 1)} | {z_high}):
            x = xi / 10
            if any(bx < x + .3 and x - .3 < bx + 1 and bz < z + .3 and z - .3 < bz + 1 for bx, bz in tops):
                centers.append((x, z))
    return centers


def main():
    blocks = support_blocks()
    boxes = occluder_boxes(blocks)
    platforms = {"start_platform": (-12.0, 0.29), "middle_platform": (0.71, 2.29)}
    report = {"occluder_boxes": [list(map(list, box)) for box in boxes], "cells": {}, "actual_start_pose": {}}
    for cell in CELLS:
        entry = {"necessary_after_d124": cell in NECESSARY}
        for name, (z_low, z_high) in platforms.items():
            centers = [c for c in standing_centers(blocks, z_low, z_high) if z_low <= c[1] <= z_high]
            seen = [(x, z, h) for x, z in centers for h in EYES
                    if visible((x, FEET_Y + h, z), cell, boxes)]
            entry[name] = {
                "poses_checked": len(centers) * len(EYES),
                "poses_with_sight_line": len(seen),
                "z_range_with_sight_line": [min(s[1] for s in seen), max(s[1] for s in seen)] if seen else None,
            }
        report["cells"][str(cell)] = entry
        report["actual_start_pose"][str(cell)] = visible((0.5, FEET_Y + 1.62, -5.17), cell, boxes)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
