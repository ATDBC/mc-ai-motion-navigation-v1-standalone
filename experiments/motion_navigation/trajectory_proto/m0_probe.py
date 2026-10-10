"""D097 M0 deterministic feasibility and cost probe.

This is measurement code.  It does not grant execution permission and does
not implement the incremental proof planned for M2.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from mc2p.motion_nav.physics_types import CalculationStatus, TickInput
from mc2p.motion_nav.world_model import Aabb

from .commitment import ScanStatus, scan_commitment
from .contracts import SearchBudget
from .physics import CountedPhysics, trajectory_digest
from .reference_search import _boundary_evidence, _goal_check, _potential_goal
from .scenarios import RepresentativeFixture, primitive_fixture


POSITIVE_CASES: tuple[tuple[str, str], ...] = (
    ("flat_walk", "A3"),
    ("jump_up_straight", "A3"),
    ("jump_gap_1", "A3"),
    *((f"gap_start_1_width_{width}", tier)
      for width in (1, 2) for tier in ("A3", "A5", "A15")),
    *((f"gap_start_4_width_{width}", tier)
      for width in (1, 2) for tier in ("A3", "A5", "A15")),
    ("gap_start_4_width_3", "A5"),
    ("gap_start_4_width_3", "A15"),
    ("flat_sprint", "A5"),
    ("turn_90", "A15"),
    ("jump_up_after_turn", "A15"),
    ("jump_gap_continue", "A3"),
    ("jump_up_after_turn_continue", "A15"),
)

NEGATIVE_CASES: tuple[tuple[str, str | None], ...] = (
    ("gap_start_4_width_3_a3", None),
    ("one_twelfth_support", None),
    ("resource_goal", None),
    ("unknown_landing", None),
    ("tiny_budget", None),
    ("collision_only", None),
)


class ProbeStatus(StrEnum):
    FOUND = "found"
    SEARCH_EXHAUSTED = "search_exhausted"
    NEEDS_INFORMATION = "needs_information"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class ProbeOptions:
    floor_prune: bool
    grounded_jump_only: bool


FORMAL_OPTIONS = ProbeOptions(floor_prune=False, grounded_jump_only=True)
FLOOR_DIAGNOSTIC_OPTIONS = ProbeOptions(floor_prune=True, grounded_jump_only=True)
UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)


@dataclass(frozen=True, slots=True)
class MixedActionFixture:
    fixture: RepresentativeFixture
    expected_inputs: tuple[TickInput, ...]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    scenario_id: str
    tier_id: str | None
    status: ProbeStatus
    reason: str
    inputs: tuple[TickInput, ...]
    input_hash: str
    physics_steps: int
    completed_candidates: int
    scan_count: int


@dataclass(frozen=True, slots=True)
class KnownInputCost:
    rollout_steps: int
    goal_steps: int
    full_scan_steps: int
    full_scan_tail_ticks: int
    estimated_per_tick_steps: int
    estimated_commit_steps: int
    rollout_ms: float
    goal_ms: float
    full_scan_ms: float


@dataclass(frozen=True, slots=True)
class DurationSummary:
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def summarize_durations(values: tuple[float, ...]) -> DurationSummary:
    """Nearest-rank percentiles; the caller owns the sample definition."""
    if not values:
        raise ValueError("duration summary requires at least one sample")
    ordered = sorted(values)

    def nearest_rank(percent: float) -> float:
        rank = max(1, math.ceil(percent * len(ordered)))
        return ordered[rank - 1]

    return DurationSummary(len(ordered), nearest_rank(.5), nearest_rank(.95),
                           nearest_rank(.99), ordered[-1])


def build_mixed_action_fixture() -> MixedActionFixture:
    """Freeze the reviewer counterexample that the bound G/J/A grid omitted."""
    base = primitive_fixture("gap_start_1_width_2", "A5")
    walk, _, stop, _, sprint_jump = base.request.input_tiers[-1].inputs
    inputs = (walk, sprint_jump) + (walk,) * 5 + (stop,) * 14
    z = 3.341460582123732
    goal = replace(base.request.goal, region=Aabb(.5 - 1.e-9, 1., z - 1.e-9,
                                                  .5 + 1.e-9, 1.05, z + 1.e-9))
    request = replace(base.request, request_id="m0-mixed-ground-jump-air",
                      goal=goal)
    fixture = replace(base, scenario_id="mixed_ground_jump_air", request=request,
                      coverage="D097 M0 mixed G/J/A counterexample.")
    return MixedActionFixture(fixture, inputs)


def _gait(command: TickInput) -> tuple[float, float, bool, bool, bool]:
    return command.forward, command.strafe, command.jump, command.sneak, command.sprint


def _moving(command: TickInput) -> bool:
    return command.forward != 0. or command.strafe != 0.


def _heading_error(state, command: TickInput, goal) -> float:
    dx = (goal.region.min_x + goal.region.max_x) / 2. - state.position[0]
    dz = (goal.region.min_z + goal.region.max_z) / 2. - state.position[2]
    wanted = math.atan2(-dx, dz)
    return abs(math.remainder(command.movement_yaw_radians - wanted, math.tau))


def _fixture_for(scenario_id: str, tier_id: str | None):
    if scenario_id == "mixed_ground_jump_air":
        return build_mixed_action_fixture().fixture
    return primitive_fixture(scenario_id, tier_id) if tier_id else primitive_fixture(scenario_id)


def run_case(scenario_id: str, tier_id: str | None, *, options: ProbeOptions) -> ProbeResult:
    """Run the target-steering parameter probe without a wall-clock decision."""
    fixture = _fixture_for(scenario_id, tier_id)
    original_request = fixture.request
    if original_request.goal.minimum_resources.values:
        return ProbeResult(scenario_id, tier_id, ProbeStatus.NEEDS_INFORMATION,
                           "unproven_resources", (), "", 0, 0, 0)
    if original_request.budget.max_physics_steps <= 1:
        return ProbeResult(scenario_id, tier_id, ProbeStatus.BUDGET_EXHAUSTED,
                           "tiny_budget_classification", (), "", 0, 0, 0)

    request = replace(original_request, budget=UNBOUNDED)
    counter = CountedPhysics(UNBOUNDED)
    stop = request.stop_input
    alphabet = request.supported_inputs
    floor = min(request.anchor_state.position[1], request.goal.region.min_y)
    gaits = tuple(dict.fromkeys(_gait(command) for command in alphabet))
    walk_gaits = tuple(gait for gait in gaits if not gait[2] and (gait[0] or gait[1]))
    jump_gaits = tuple(gait for gait in gaits if gait[2])
    unknown = False
    completions = scans = 0
    tried: set[tuple[TickInput, ...]] = set()

    def steer(states, gait):
        choices = tuple(command for command in alphabet if _gait(command) == gait)
        return min(choices, key=lambda command: (
            _heading_error(states[0], command, request.goal), alphabet.index(command)))

    def roll(states, command):
        nonlocal unknown
        next_states = []
        for state in states:
            result = counter.step(state, command, fixture.world)
            if result.status is CalculationStatus.NEEDS_WORLD:
                unknown = True
                return None
            if (result.status is not CalculationStatus.OK
                    or result.next_state.horizontal_collision
                    or (options.floor_prune
                        and result.next_state.position[1] < floor - 1.e-7)):
                return None
            next_states.append(result.next_state)
        return tuple(next_states)

    def verified(inputs, states):
        nonlocal unknown, scans
        checks = []
        for branch, state in zip(request.timing_branches, states):
            check, missing = _goal_check(request, branch, state, fixture.world, counter)
            if missing:
                unknown = True
                return False
            checks.append(check)
        if not all(check.accepted for check in checks):
            return False
        scans += 1
        scan = scan_commitment(request, inputs, fixture.world,
                               _boundary_evidence(request, inputs), counter=counter)
        if scan.status is ScanStatus.NEEDS_INFORMATION:
            unknown = True
        return scan.status is ScanStatus.VERIFIED_CANDIDATE

    def complete(states, inputs):
        nonlocal completions
        if inputs in tried:
            return None
        tried.add(inputs)
        while True:
            completions += 1
            if _potential_goal(request, states) and verified(inputs, states):
                return inputs
            speeds = tuple(20. * math.hypot(state.velocity_blocks_per_tick[0],
                                            state.velocity_blocks_per_tick[2])
                           for state in states)
            if (len(inputs) >= request.budget.max_trajectory_ticks
                    or (all(state.on_ground for state in states) and max(speeds) == 0.)
                    or max(speeds) + 1.e-12
                    < request.minimum_terminal_speed_blocks_per_second):
                return None
            states = roll(states, stop)
            if states is None:
                return None
            inputs += (stop,)

    def hold(states, inputs, gait, ticks):
        path = [(states, inputs)]
        for _ in range(ticks):
            if len(inputs) >= request.budget.max_trajectory_ticks:
                break
            command = steer(states, gait)
            states = roll(states, command)
            if states is None:
                break
            inputs += (command,)
            path.append((states, inputs))
        return tuple(path)

    entry = request.entry_states
    for command in request.input_prefix:
        entry = roll(entry, command)
        if entry is None:
            status = ProbeStatus.NEEDS_INFORMATION if unknown else ProbeStatus.SEARCH_EXHAUSTED
            return ProbeResult(scenario_id, tier_id, status, status.value, (), "",
                               counter.physics_steps, completions, scans)

    runs = tuple(hold(entry, request.input_prefix, gait, 20) for gait in walk_gaits)
    for path in runs:
        for states, inputs in path:
            found = complete(states, inputs)
            if found is not None:
                return ProbeResult(scenario_id, tier_id, ProbeStatus.FOUND,
                                   "verified_candidate", found, trajectory_digest(found),
                                   counter.physics_steps, completions, scans)

    for path in runs:
        for states, inputs in reversed(path):
            if (len(inputs) >= request.budget.max_trajectory_ticks
                    or (options.grounded_jump_only
                        and not all(state.on_ground for state in states))):
                continue
            for jump_gait in jump_gaits:
                lifted = roll(states, steer(states, jump_gait))
                if lifted is None:
                    continue
                jump_input = steer(states, jump_gait)
                for air_gait in walk_gaits:
                    for flying, flown in hold(lifted, inputs + (jump_input,), air_gait, 16):
                        found = complete(flying, flown)
                        if found is not None:
                            return ProbeResult(scenario_id, tier_id, ProbeStatus.FOUND,
                                               "verified_candidate", found,
                                               trajectory_digest(found), counter.physics_steps,
                                               completions, scans)

    status = ProbeStatus.NEEDS_INFORMATION if unknown else ProbeStatus.SEARCH_EXHAUSTED
    return ProbeResult(scenario_id, tier_id, status, status.value, (), "",
                       counter.physics_steps, completions, scans)


def measure_known_input(scenario_id: str, tier_id: str | None,
                        inputs: tuple[TickInput, ...], *,
                        clock_ns=time.perf_counter_ns) -> KnownInputCost:
    """Measure a known plan's full scan and derive step-only lazy estimates."""
    fixture = _fixture_for(scenario_id, tier_id)
    request = replace(fixture.request, budget=UNBOUNDED)
    counter = CountedPhysics(UNBOUNDED)
    finals = []
    rollout_started = clock_ns()
    for state in request.entry_states:
        for command in inputs:
            result = counter.step(state, command, fixture.world)
            if result.status is not CalculationStatus.OK:
                raise AssertionError("known input rollout must remain calculable")
            state = result.next_state
        finals.append(state)
    rollout_finished = clock_ns()
    rollout_steps = counter.physics_steps
    for branch, state in zip(request.timing_branches, finals):
        check, missing = _goal_check(request, branch, state, fixture.world, counter)
        if missing or check is None or not check.accepted:
            raise AssertionError("known input must satisfy every branch goal")
    goal_finished = clock_ns()
    goal_steps = counter.physics_steps - rollout_steps
    scan = scan_commitment(request, inputs, fixture.world,
                           _boundary_evidence(request, inputs), counter=counter)
    if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
        raise AssertionError(f"known input full scan failed: {scan.status}/{scan.reason}")
    scan_finished = clock_ns()
    branches = scan.proof.branches
    committed = {interval.first_committed_boundary
                 for branch in branches for interval in branch.risk_intervals}
    per_tick = max(
        (sum(len(branch.tails[index].inputs) for branch in branches)
         for index in range(len(branches[0].tails)) if index not in committed),
        default=0,
    )
    commit = max(
        (sum(len(branch.tails[interval.recovered_boundary].inputs) for branch in branches)
         for interval in branches[0].risk_intervals),
        default=0,
    )
    return KnownInputCost(rollout_steps, goal_steps,
                          counter.physics_steps - rollout_steps - goal_steps,
                          counter.tail_ticks, per_tick, commit,
                          (rollout_finished - rollout_started) / 1_000_000.,
                          (goal_finished - rollout_finished) / 1_000_000.,
                          (scan_finished - goal_finished) / 1_000_000.)


EXPECTED_NEGATIVE = {
    "gap_start_4_width_3_a3": ProbeStatus.SEARCH_EXHAUSTED,
    "one_twelfth_support": ProbeStatus.SEARCH_EXHAUSTED,
    "resource_goal": ProbeStatus.NEEDS_INFORMATION,
    "unknown_landing": ProbeStatus.NEEDS_INFORMATION,
    "tiny_budget": ProbeStatus.BUDGET_EXHAUSTED,
    "collision_only": ProbeStatus.SEARCH_EXHAUSTED,
}


def _duration_payload(values):
    samples = tuple(values)
    return {**asdict(summarize_durations(samples)), "samples_ms": list(samples)}


def _parse_case(value: str) -> tuple[str, str | None]:
    scenario, separator, tier = value.partition(":")
    if not scenario:
        raise argparse.ArgumentTypeError("case requires SCENARIO[:TIER]")
    return scenario, tier if separator and tier else None


def _default_cases():
    return POSITIVE_CASES + NEGATIVE_CASES + (("mixed_ground_jump_air", None),)


def _expected_status(scenario_id, tier_id):
    if (scenario_id, tier_id) in POSITIVE_CASES or scenario_id == "mixed_ground_jump_air":
        return ProbeStatus.FOUND
    return EXPECTED_NEGATIVE[scenario_id]


def _measure_mode(name, options, cases, warmups, rounds):
    rows = []
    for scenario_id, tier_id in cases:
        for _ in range(warmups):
            run_case(scenario_id, tier_id, options=options)
        durations, results, costs = [], [], []
        for _ in range(rounds):
            started = time.perf_counter_ns()
            result = run_case(scenario_id, tier_id, options=options)
            durations.append((time.perf_counter_ns() - started) / 1_000_000.)
            results.append(result)
            if result.status is ProbeStatus.FOUND:
                costs.append(measure_known_input(scenario_id, tier_id, result.inputs))
        signatures = {(result.status.value, result.reason, result.input_hash,
                       result.physics_steps, result.completed_candidates, result.scan_count)
                      for result in results}
        if len(signatures) != 1:
            raise RuntimeError(f"nondeterministic M0 result: {scenario_id}/{tier_id}")
        result = results[0]
        row = {
            "scenario_id": scenario_id,
            "tier_id": tier_id,
            "expected_status": _expected_status(scenario_id, tier_id).value,
            "status": result.status.value,
            "reason": result.reason,
            "input_hash": result.input_hash,
            "input_ticks": len(result.inputs),
            "physics_steps": result.physics_steps,
            "completed_candidates": result.completed_candidates,
            "scan_count": result.scan_count,
            "controller_ms": _duration_payload(durations),
            "known_input_cost": None,
        }
        if costs:
            step_fields = (
                "rollout_steps", "goal_steps", "full_scan_steps", "full_scan_tail_ticks",
                "estimated_per_tick_steps", "estimated_commit_steps",
            )
            fixed = {field: getattr(costs[0], field) for field in step_fields}
            if any(any(getattr(cost, field) != value for field, value in fixed.items())
                   for cost in costs[1:]):
                raise RuntimeError(f"nondeterministic known-input counts: {scenario_id}/{tier_id}")
            unit_ms = [cost.rollout_ms / cost.rollout_steps for cost in costs]
            row["known_input_cost"] = {
                **fixed,
                "rollout_ms": _duration_payload([cost.rollout_ms for cost in costs]),
                "goal_ms": _duration_payload([cost.goal_ms for cost in costs]),
                "full_scan_ms": _duration_payload([cost.full_scan_ms for cost in costs]),
                "estimated_per_tick_ms": _duration_payload([
                    cost.estimated_per_tick_steps * unit
                    for cost, unit in zip(costs, unit_ms)
                ]),
                "estimated_commit_ms": _duration_payload([
                    cost.estimated_commit_steps * unit
                    for cost, unit in zip(costs, unit_ms)
                ]),
                "estimate_basis": "lazy step count multiplied by that run's rollout ms/step; M2 not implemented",
            }
        rows.append(row)
    return {"name": name, "floor_prune": options.floor_prune,
            "grounded_jump_only": options.grounded_jump_only, "cases": rows}


def _hashseed_check():
    code = (
        "import json; "
        "from experiments.motion_navigation.trajectory_proto.m0_probe import "
        "FORMAL_OPTIONS,run_case; "
        "r=run_case('turn_90','A15',options=FORMAL_OPTIONS); "
        "print(json.dumps([r.status.value,r.input_hash,r.physics_steps,"
        "r.completed_candidates,r.scan_count]))"
    )
    rows = []
    for seed in ("1", "77", "991"):
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=Path.cwd(),
            env=dict(os.environ, PYTHONHASHSEED=seed), check=True,
            capture_output=True, text=True,
        )
        rows.append(json.loads(completed.stdout))
    return {"seeds": [1, 77, 991], "rows": rows, "identical": rows[1:] == rows[:-1]}


def _gate(formal, complete_scope, hashseed):
    if not complete_scope:
        return {"evaluated": False, "passed": None,
                "reason": "partial --case measurement cannot sign M0"}
    rows = formal["cases"]
    classifications = all(row["status"] == row["expected_status"] for row in rows)
    positives = [row for row in rows if row["expected_status"] == ProbeStatus.FOUND.value]
    negative_cost = [row for row in rows
                     if row["expected_status"] != ProbeStatus.FOUND.value
                     and row["scenario_id"] != "tiny_budget"]
    positive_max = max(row["controller_ms"]["max_ms"] for row in positives)
    negative_max = max(row["controller_ms"]["max_ms"] for row in negative_cost)
    rollout_samples = [sample for row in positives
                       for sample in row["known_input_cost"]["rollout_ms"]["samples_ms"]]
    incremental_samples = [sample for row in positives
                           for sample in row["known_input_cost"]
                           ["estimated_per_tick_ms"]["samples_ms"]]
    rollout_p95 = summarize_durations(tuple(rollout_samples)).p95_ms
    incremental_p95 = summarize_durations(tuple(incremental_samples)).p95_ms
    checks = {
        "classifications_match": classifications,
        "positive_max_ms_le_400": positive_max <= 400.,
        "meaningful_negative_max_ms_le_800": negative_max <= 800.,
        "estimated_incremental_p95_ms_le_10": incremental_p95 <= 10.,
        "rollout_p95_ms_le_15": rollout_p95 <= 15.,
        "hashseed_identical": hashseed["identical"],
    }
    return {
        "evaluated": True,
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "positive_max_ms": positive_max,
            "meaningful_negative_max_ms": negative_max,
            "estimated_incremental_p95_ms": incremental_p95,
            "rollout_p95_ms": rollout_p95,
        },
        "notes": [
            "tiny_budget is classification-only and excluded from no-solution cost",
            "incremental milliseconds are estimates; M2 has not implemented incremental proof",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--case", action="append", type=_parse_case, dest="cases")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.rounds < 1:
        parser.error("warmups must be nonnegative and rounds must be positive")
    cases = tuple(args.cases) if args.cases else _default_cases()
    complete_scope = cases == _default_cases()
    formal = _measure_mode("formal_no_floor", FORMAL_OPTIONS, cases,
                           args.warmups, args.rounds)
    floor = _measure_mode("diagnostic_floor", FLOOR_DIAGNOSTIC_OPTIONS, cases,
                          args.warmups, args.rounds)
    hashseed = _hashseed_check()
    payload = {
        "schema": "incremental-motion-m0-v1",
        "platform": {"system": platform.system(), "release": platform.release(),
                     "python": platform.python_version(), "executable": sys.executable},
        "measurement": {"warmups": args.warmups, "rounds": args.rounds,
                        "percentile": "nearest-rank", "complete_scope": complete_scope},
        "modes": {"formal": formal, "floor_diagnostic": floor},
        "hashseed": hashseed,
    }
    payload["gate"] = _gate(formal, complete_scope, hashseed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output), "gate": payload["gate"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
