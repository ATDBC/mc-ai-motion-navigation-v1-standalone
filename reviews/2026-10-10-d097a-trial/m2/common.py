"""Shared helpers for the M2 trial: input (de)serialisation, frozen-matrix fixtures, world edits.

Read-only use of the project checkout (PYTHONPATH must contain it) and of ../entries.py.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.scenarios import RepresentativeFixture
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import CellFact, CellKnowledge, WorldView

HERE = Path(__file__).resolve().parent
SET_A_CACHE = HERE / "set_a_inputs.json"
SET_C_CACHE = HERE / "set_c_inputs.json"


def tick_to_list(command: TickInput) -> list:
    return [command.forward, command.strafe, command.jump, command.sneak, command.sprint,
            command.movement_yaw_radians]


def tick_from_list(row) -> TickInput:
    forward, strafe, jump, sneak, sprint, yaw = row
    return TickInput(float(forward), float(strafe), bool(jump), bool(sneak), bool(sprint), float(yaw))


def inputs_to_json(inputs) -> list:
    return [tick_to_list(command) for command in inputs]


def inputs_from_json(rows) -> tuple[TickInput, ...]:
    return tuple(tick_from_list(row) for row in rows)


def case_key(scenario_id: str, tier_id: str | None) -> str:
    return f"{scenario_id}:{tier_id or ''}"


def set_a_cases():
    return tuple(m0_probe.POSITIVE_CASES) + (("mixed_ground_jump_air", None),)


def load_set_a() -> dict[str, tuple[TickInput, ...]]:
    raw = json.loads(SET_A_CACHE.read_text(encoding="utf-8"))
    return {key: inputs_from_json(rows) for key, rows in raw["inputs"].items()}


def fixture_for(scenario_id: str, tier_id: str | None) -> RepresentativeFixture:
    return m0_probe._fixture_for(scenario_id, tier_id)


def fresh_world(world: PhysicsWorldView) -> PhysicsWorldView:
    """Same facts, empty shape cache (cold-cache timing)."""
    return PhysicsWorldView(world._world, world.ruleset)


def world_facts(world: PhysicsWorldView, bounds=((-9, 9), (-50, 7), (-14, 16))) -> dict:
    facts = {}
    for x in range(bounds[0][0], bounds[0][1] + 1):
        for y in range(bounds[1][0], bounds[1][1] + 1):
            for z in range(bounds[2][0], bounds[2][1] + 1):
                fact = world.cell((x, y, z))
                if fact.knowledge is not CellKnowledge.UNKNOWN:
                    facts[(x, y, z)] = fact
    return facts


def edited_world(world: PhysicsWorldView, edits: dict, bounds=((-9, 9), (-50, 7), (-14, 16)),
                 geometry_revision: int | None = None, session=None) -> PhysicsWorldView:
    """Copy of a detached world with cell edits; None removes the fact (=> UNKNOWN)."""
    facts = world_facts(world, bounds)
    for position, fact in edits.items():
        if fact is None:
            facts.pop(position, None)
        else:
            facts[position] = fact
    revision = world.geometry_revision if geometry_revision is None else geometry_revision
    return PhysicsWorldView(WorldView.detached(session or world.session, revision, 0, facts),
                            JAVA_1_21_RULESET)


def evidence(request, inputs):
    return _boundary_evidence(request, inputs)


def label(command: TickInput) -> str:
    if command.jump:
        return "SJ" if command.sprint else "J"
    if command.sprint:
        return "S"
    return "W" if command.forward or command.strafe else "N"
