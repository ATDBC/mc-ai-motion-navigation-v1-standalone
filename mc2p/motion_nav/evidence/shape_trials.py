"""Offline geometry and scoring for B03 line-look and circle trials."""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

from mc2p.contracts.common import ContractViolation


Point3 = tuple[float, float, float]


def line_reference_yaw_degrees(progress_blocks: float) -> float:
    """Turn 45 degrees per three travelled blocks, then unwind once."""
    if type(progress_blocks) not in (int, float) or not math.isfinite(float(progress_blocks)):
        raise ContractViolation("line trial progress must be finite")
    progress = max(0.0, float(progress_blocks))
    if progress <= 24.0:
        return progress * 15.0
    if progress <= 48.0:
        return 360.0 - (progress - 24.0) * 15.0
    return 0.0


def circle_route_points(origin: Point3, radius: float, side: str,
                        *, spacing_blocks: float = .25) -> tuple[Point3, ...]:
    if (type(origin) is not tuple or len(origin) != 3
            or any(type(value) not in (int, float) or not math.isfinite(float(value))
                   for value in origin)):
        raise ContractViolation("circle origin must be a finite coordinate triple")
    if type(radius) not in (int, float) or not math.isfinite(float(radius)) or radius <= 0:
        raise ContractViolation("circle radius must be positive and finite")
    if side not in {"left", "right"}:
        raise ContractViolation("circle side must be left or right")
    if (type(spacing_blocks) not in (int, float) or not math.isfinite(float(spacing_blocks))
            or not 0 < spacing_blocks <= radius):
        raise ContractViolation("circle point spacing must be within the radius")
    x, y, z = (float(value) for value in origin)
    radius = float(radius)
    count = max(12, math.ceil(math.tau * radius / float(spacing_blocks)))
    center_x = x - radius if side == "left" else x + radius
    start_angle = 0.0 if side == "left" else math.pi
    direction = 1.0 if side == "left" else -1.0
    points = []
    for index in range(count + 1):
        angle = start_angle + direction * math.tau * index / count
        points.append((center_x + radius * math.cos(angle), y,
                       z + radius * math.sin(angle)))
    points[-1] = points[0]
    return tuple(points)


def _angle_error_degrees(actual: float, expected: float) -> float:
    return (actual - expected + 180.0) % 360.0 - 180.0


def _rms(values: Iterable[float]) -> float:
    values = tuple(values)
    return math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0


def line_metrics(samples: Sequence[Mapping], origin: Point3) -> dict[str, float]:
    if not samples:
        raise ContractViolation("line metrics require samples")
    lateral = [float(sample["position"][0]) - origin[0] for sample in samples]
    yaw_errors = []
    for sample in samples:
        progress = max(0.0, float(sample["position"][2]) - origin[2])
        yaw_errors.append(_angle_error_degrees(
            float(sample["yaw"]), line_reference_yaw_degrees(progress)))
    return {
        "lateral_rmse_blocks": _rms(lateral),
        "lateral_max_blocks": max(abs(value) for value in lateral),
        "final_lateral_error_blocks": abs(lateral[-1]),
        "yaw_error_rms_degrees": _rms(yaw_errors),
        "yaw_error_max_degrees": max(abs(value) for value in yaw_errors),
    }


def _solve_three(matrix: list[list[float]], values: list[float]) -> tuple[float, float, float] | None:
    augmented = [row[:] + [value] for row, value in zip(matrix, values)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [a - factor * b for a, b in zip(
                augmented[row], augmented[column]
            )]
    return tuple(augmented[row][3] for row in range(3))  # type: ignore[return-value]


def _fit_circle(points: Sequence[tuple[float, float]]) -> tuple[float, float, float] | None:
    if len(points) < 3:
        return None
    sx = sz = sxx = sxz = szz = sb = sxb = szb = 0.0
    for x, z in points:
        b = x * x + z * z
        sx += x; sz += z; sxx += x * x; sxz += x * z; szz += z * z
        sb += b; sxb += x * b; szb += z * b
    solved = _solve_three(
        [[2 * sxx, 2 * sxz, sx], [2 * sxz, 2 * szz, sz], [2 * sx, 2 * sz, len(points)]],
        [sxb, szb, sb],
    )
    if solved is None:
        return None
    center_x, center_z, constant = solved
    radius_squared = constant + center_x * center_x + center_z * center_z
    if radius_squared <= 0:
        return None
    return center_x, center_z, math.sqrt(radius_squared)


def circle_metrics(samples: Sequence[Mapping], origin: Point3,
                   radius: float, side: str) -> dict[str, float | None]:
    if not samples:
        raise ContractViolation("circle metrics require samples")
    if side not in {"left", "right"}:
        raise ContractViolation("circle side must be left or right")
    center_x = origin[0] - radius if side == "left" else origin[0] + radius
    center_z = origin[2]
    points = [(float(sample["position"][0]), float(sample["position"][2]))
              for sample in samples]
    radial = [math.hypot(x - center_x, z - center_z) - radius for x, z in points]
    closure = math.hypot(points[-1][0] - origin[0], points[-1][1] - origin[2])
    fit = _fit_circle(points)
    return {
        "radial_rmse_blocks": _rms(radial),
        "radial_max_blocks": max(abs(value) for value in radial),
        "closure_error_blocks": closure,
        "fitted_center_error_blocks": None if fit is None else math.hypot(
            fit[0] - center_x, fit[1] - center_z),
        "fitted_radius_blocks": None if fit is None else fit[2],
        "fitted_radius_error_blocks": None if fit is None else abs(fit[2] - radius),
    }
