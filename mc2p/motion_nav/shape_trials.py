"""Compatibility import for evidence-only route-shape measurements.

New code should import :mod:`mc2p.motion_nav.evidence.shape_trials`.
"""

from mc2p.motion_nav.evidence.shape_trials import (
    circle_metrics,
    circle_route_points,
    line_metrics,
    line_reference_yaw_degrees,
)

LIFECYCLE = "compatibility_import"

__all__ = (
    "circle_metrics",
    "circle_route_points",
    "line_metrics",
    "line_reference_yaw_degrees",
)
