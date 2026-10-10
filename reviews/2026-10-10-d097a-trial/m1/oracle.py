"""D097-A M1: exhaustive enumeration ("oracle") over the controller's full parameter grid for one entry.

Formal option: no floor/height pruning, jump only when every branch is grounded (the controller's
own rule). The enumeration calls controller.evaluate for every parameter class, so it cannot differ
from the controller. Prefix sharing:
  * the controller's Tree steps (and counts) every input prefix once, for both timing branches;
  * the trigger value (takeoff_d or brake_b) only matters through the ground tick it fires on, so
    grid values firing on the same tick form one class that is run once and expanded afterwards;
  * jump gait x air gait x air ticks share the ground path and the air prefix.
Mode "collect": every Params passing rollout + potential goal + per-branch goal check (no scans).
Mode "feasible": early exit at the first candidate accepted by verify() (goal check + full scan).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import controller as C
import entries as E
from controller import Params, Tree

_ALPHABET_INDEX = {command: i for i, command in enumerate(E.ALPHABET)}


@dataclass(slots=True)
class PointResult:
    passing: dict = field(default_factory=dict)      # Params -> (margin in grid steps from its class edge, goal slack, behaviour)
    feasible: bool | None = None                      # mode "feasible" only
    first: tuple | None = None                        # (Params, inputs) of the first verified candidate
    steps: int = 0
    scan_steps: int = 0
    classes: int = 0
    scans: int = 0
    over_cap: bool = False
    aborted: bool = False


def _trigger_groups(tree: Tree, ground_gait: str, jump: bool):
    """Group the trigger grid values by the ground tick they fire on; failures are dropped."""
    groups: dict = {}
    for value in (C.TAKEOFF_GRID if jump else C.BRAKE_GRID):
        probe = (Params(ground_gait, value, "WJ", "N", 0) if jump
                 else Params(ground_gait, None, brake_b=value))
        node, reason = C.ground_phase(tree, probe)
        if node is not None:
            groups.setdefault(len(node.inputs), []).append(value)
    return groups.values()


def _classes(tree: Tree):
    """Yield (members, representative) for every parameter class, in canonical order."""
    has_line = tree.geometry.feature_z is not None
    for ground_gait in C.GROUND_GAITS:
        if has_line:
            for group in _trigger_groups(tree, ground_gait, True):
                for jump_gait in C.JUMP_GAITS:
                    for air_gait in C.AIR_GAITS:
                        for air_ticks in C.AIR_TICKS:
                            members = [Params(ground_gait, d, jump_gait, air_gait, air_ticks) for d in group]
                            yield members, members[0]
        for group in _trigger_groups(tree, ground_gait, False):
            members = [Params(ground_gait, None, brake_b=b) for b in group]
            yield members, members[0]


def enumerate_point(request, world, geometry: C.Geometry, *, mode: str, cap: int = 40_000,
                    hard_limit_factor: int = 10) -> PointResult:
    """mode 'collect' or 'feasible'. The cap is recorded (over_cap); only 10x the cap aborts."""
    assert mode in ("collect", "feasible")
    tree = Tree(request, world, geometry)
    counter, result, seen = tree.counter, PointResult(), set()
    for members, representative in _classes(tree):
        if counter.physics_steps > cap * hard_limit_factor:
            result.aborted = True
            break
        result.classes += 1
        node, reason = C.evaluate(tree, representative)
        if node is None:
            continue
        if mode == "collect":
            slack, behaviour = C.goal_slack(tree.request, node), bytes(_ALPHABET_INDEX[c] for c in node.inputs)
            for position, member in enumerate(members):
                result.passing[member] = (min(position, len(members) - 1 - position), slack, behaviour)
        elif node.inputs not in seen:
            seen.add(node.inputs)
            before = counter.physics_steps
            accepted, why, scan = C.verify(request, world, node.inputs, counter)
            result.scan_steps += counter.physics_steps - before
            result.scans += 1
            if accepted:
                result.feasible, result.first = True, (representative, node.inputs)
                break
    else:
        if mode == "feasible":
            result.feasible = False
    result.steps = counter.physics_steps
    result.over_cap = result.steps > cap
    return result
