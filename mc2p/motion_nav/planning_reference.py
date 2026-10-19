"""Materialized graph planners retained for regression and diagnostics only.

Live planning uses ``plan_known_snapshot`` or
``plan_known_surface_snapshot``. This module gives old graph construction,
A* and independent Dijkstra checks an explicit reference-only home without
breaking their frozen evidence.
"""

from mc2p.motion_nav.known_map_planner import (
    astar_plan,
    astar_surface_plan,
    build_surface_graph,
    build_walk_graph,
    dijkstra_reference,
    dijkstra_surface_reference,
)

LIFECYCLE = "reference_only"

__all__ = (
    "LIFECYCLE",
    "astar_plan",
    "astar_surface_plan",
    "build_surface_graph",
    "build_walk_graph",
    "dijkstra_reference",
    "dijkstra_surface_reference",
)
