"""Frozen A*/Dijkstra comparison for mixed known support surfaces."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import random
import time

from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, SurfacePlanningRequest, SurfacePlanningStatus,
    astar_surface_plan, build_surface_graph, dijkstra_surface_reference,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile


MANIFEST = Path(
    "tests/sim/manifests/continuous-height-planning-matrix.json"
)


@dataclass(frozen=True, slots=True)
class PlanningMatrixResult:
    seed: int
    status: str
    reference_reachable: bool
    astar_cost_seconds: float | None
    dijkstra_cost_seconds: float | None
    expanded_nodes: int
    graph_nodes: int
    graph_edges: int
    elapsed_ms: float

    @property
    def matches_reference(self) -> bool:
        if self.reference_reachable:
            return (
                self.status == SurfacePlanningStatus.COMPLETE.value
                and self.astar_cost_seconds is not None
                and self.dijkstra_cost_seconds is not None
                and abs(self.astar_cost_seconds - self.dijkstra_cost_seconds)
                <= 1.0e-9
            )
        return self.status != SurfacePlanningStatus.COMPLETE.value


def load_planning_manifest() -> dict:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if document.get("schema_version") != (
            "mc2p.continuous-height-planning-matrix.v1"):
        raise ValueError("unsupported planning matrix manifest")
    if document["seed_end"] - document["seed_start"] + 1 != 1_000:
        raise ValueError("planning matrix must freeze one thousand seeds")
    return document


def _geometry(material: str) -> BlockGeometry:
    if material == "minecraft:stone":
        return BlockGeometry.full_cube(material)
    if material == "minecraft:smooth_stone_slab":
        return BlockGeometry(
            material, "boxes", (Aabb(0, 0, 0, 1, .5, 1),),
        )
    if material == "minecraft:dirt_path":
        return BlockGeometry(
            material, "boxes", (Aabb(0, 0, 0, 1, 15 / 16, 1),),
        )
    if material == "minecraft:oak_stairs":
        return BlockGeometry(
            material, "boxes", (
                Aabb(0, 0, 0, 1, .5, 1),
                Aabb(0, .5, .5, 1, 1, 1),
            ),
        )
    raise ValueError(f"unsupported planning matrix material: {material}")


def run_planning_case(seed: int) -> PlanningMatrixResult:
    document = load_planning_manifest()
    if not document["seed_start"] <= seed <= document["seed_end"]:
        raise ValueError("planning matrix seed is outside the frozen range")
    size = int(document["grid_size"])
    rng = random.Random(seed)
    session = WorldSessionId(f"continuous-height-plan-{seed}")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "planning-matrix", 50_000_000)
    blocks = {}
    materials = tuple(document["materials"])
    for x in range(size):
        for z in range(size):
            if ((x, z) not in {(0, 0), (size - 1, size - 1)}
                    and rng.random() < float(document["missing_probability"])):
                continue
            material = materials[rng.randrange(len(materials))]
            blocks[(x, 0, z)] = _geometry(material)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-1, size + 1)
        for y in range(-2, 4)
        for z in range(-1, size + 1)
    ))
    world.observe_blocks(stamp, blocks)
    ground = replace(
        ordinary_profile(), support_materials=frozenset(materials),
    )
    graph = build_surface_graph(
        world.view(), KnownMapBounds(0, size - 1, 0, 1, 0, size - 1, True),
        ground, step_profile(),
    )
    node_ids = {node.node_id for node in graph.nodes}
    starts = sorted(
        node for node in node_ids if node.column_x == 0 and node.column_z == 0
    )
    goals = sorted(
        node for node in node_ids
        if node.column_x == size - 1 and node.column_z == size - 1
    )
    if not starts or not goals:
        raise AssertionError("frozen planning endpoints must expose support")
    start, goal = starts[-1], goals[-1]
    request = SurfacePlanningRequest(
        seed, f"planning-matrix-{seed}", "planning-matrix-goal", 1,
        session.value, start, goal, maximum_planning_seconds=5.0,
    )
    started = time.perf_counter()
    candidate = astar_surface_plan(graph, request)
    elapsed_ms = (time.perf_counter() - started) * 1_000.0
    reference = dijkstra_surface_reference(graph, start, goal)
    return PlanningMatrixResult(
        seed, candidate.status.value, reference is not None,
        candidate.total_cost_seconds, reference, candidate.expanded_nodes,
        len(graph.nodes), len(graph.edges), elapsed_ms,
    )
