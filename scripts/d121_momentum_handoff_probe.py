"""D121 S0 diagnostics only: no joint proof, worker or production admission.

Finite samples and the local exclusivity guard grant no execution authority.
Physics loops have fixed limits; elapsed time is reported, never used by control.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion, solve_one_cell_gap
from mc2p.motion_nav.online_motion import CandidateExecutionWindow, MotionTickPhase, StateAnchor
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from scripts import d120_momentum_entry_calibration as calibration
from tests.sim.backend import CalculatorBackend, Scene
from tests.test_player_runtime import _RecordingTrace


class GapKind(StrEnum):
    JOINT_PROOF = "joint_successor_and_fallback_proof"
    UNAVAILABLE_SUCCESSOR = "expired_changed_or_missing_successor_selection"
    OWNER_RETENTION = "fallback_owner_retained_until_safe_terminal"
    PRODUCTION_EXCLUSIVITY = "production_branch_exclusivity"


@dataclass(frozen=True)
class GapFact:
    kind: GapKind
    status: str = "unimplemented"


def unimplemented_facts():
    return tuple(GapFact(kind) for kind in GapKind)


class SampleStatus(StrEnum):
    ENUMERATED_ONLY = "enumerated_sample_not_execution_authority"
    UNPROVEN_MIDPOINT = "unproven_midpoint"
    OUTSIDE_SAMPLES = "outside_sample_bounds"


def sample_status(offsets):
    """Classify the frozen grid; even a listed point is no admission proof."""
    if len(offsets) != 5 or any(not math.isfinite(v) for v in offsets):
        raise ValueError("expected five finite D120 offsets")
    if any(v < axis[0] or v > axis[-1] for v, axis in zip(offsets, calibration.AXES)):
        return SampleStatus.OUTSIDE_SAMPLES
    if all(v in axis for v, axis in zip(offsets, calibration.AXES)):
        return SampleStatus.ENUMERATED_ONLY
    return SampleStatus.UNPROVEN_MIDPOINT


def single_branch_input(branches):
    """S0 harness guard, not a production proof or branch selector."""
    if len(branches) != 1 or type(branches[0]) is not MovementV1:
        raise ValueError("exactly one diagnostic branch must be selected")
    command = branches[0]
    if command.jump and command.sneak:
        raise ValueError("jump and sneak branches cannot be merged")
    return command


def receipt_matches_selected_input(receipt, selected, landing_tick):
    """Check the identity and all five movement fields actually consumed."""
    identity = (
        receipt.episode_id == selected.episode_id
        and receipt.request_sequence_id == selected.request_sequence_id
        and receipt.movement_tick_id == landing_tick + 1
    )
    movement = (
        float(receipt.forward) == float(selected.movement.forward)
        and float(receipt.strafe) == float(selected.movement.strafe)
        and receipt.jump is selected.movement.jump
        and receipt.sneak is selected.movement.sneak
        and receipt.sprint is selected.movement.sprint
    )
    return identity, movement


def exclusivity_fact():
    jump, sneak = MovementV1(1, 0, True, False, True), MovementV1(sneak=True)
    rejected = []
    for name, branches in (("two_selected_branches", (jump, sneak)),
                           ("merged_jump_and_sneak", (MovementV1(jump=True, sneak=True),))):
        try:
            single_branch_input(branches)
        except ValueError:
            rejected.append(name)
    return {"rejected": rejected, "scope": "S0_harness_guard_only"}


def initial_state(fixture):
    # Preserve D120's complete state schema, changing only the frozen reset fields.
    return replace(calibration.nominal_entry(fixture), movement_tick_id=0,
        position=fixture.trial.start_local,
        velocity_blocks_per_tick=(0., -.0784000015258789, 0.),
        sprinting=False, sneaking=False)


def independent_exit_fact():
    fixture = calibration.build_fixture()
    state = replace(initial_state(fixture), position=(.5, 64., -.5))
    anchor = StateAnchor(state.session, 0, 0, MotionTickPhase.AFTER_MOVEMENT,
        None, None, state.ruleset_id, state.state_schema, "mc2p.input-projection.v1", state)
    request = GapSolveRequest((0, 1), LandingRegion(-.7, 1.7, 1.3, 1.7, 64.),
                              CandidateExecutionWindow(1, 2))
    result = solve_one_cell_gap(anchor, fixture.context.world, request)
    if result.proof is None:
        raise ValueError(f"independent first jump baseline changed: {result.status} {result.reasons}")
    exit_state = result.proof.exit_state
    nominal = calibration.nominal_entry(fixture)
    return {"solver_status": result.status.value, "start_state": state.to_mapping(),
        "exit_state": exit_state.to_mapping(), "candidates_evaluated": result.candidates_evaluated,
        "exit_speed_blocks_per_tick": math.hypot(*exit_state.velocity_blocks_per_tick[::2]),
        "d120_entry_speed_blocks_per_tick": math.hypot(*nominal.velocity_blocks_per_tick[::2]),
        "fixed_successor": calibration.replay_suffix(exit_state, fixture, "landing-immediate"),
        "scope": "independent_rest_start_same_fixture_not_D120_runup"}


def nominal_facts():
    fixture = calibration.build_fixture()
    entry = calibration.nominal_entry(fixture)
    return {"landed_tick": entry.movement_tick_id, "entry": entry.to_mapping(),
        "fixture_sha256": fixture.sha256,
        "suffixes": {name: calibration.replay_suffix(entry, fixture, name) for name in (
            "landing-immediate", "fallback-sneak", "fallback-neutral", "fallback-back")}}


def replay_cost(repetitions=100):
    if type(repetitions) is not int or not 1 <= repetitions <= 1000:
        raise ValueError("replay repetitions must be within 1..1000")
    fixture = calibration.build_fixture()
    entry = calibration.nominal_entry(fixture)
    elapsed, counts = [], []
    original = calibration.physics_step
    calls = [0]
    def counted(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)
    with patch.object(calibration, "physics_step", side_effect=counted):
        for _ in range(repetitions):
            before = calls[0]
            start = time.perf_counter_ns()
            results = tuple(calibration.replay_suffix(entry, fixture, suffix)
                for suffix in ("landing-immediate", "fallback-sneak"))
            elapsed.append((time.perf_counter_ns()-start)/1e6)
            counts.append(calls[0]-before)
            if not all(r["passed"] for r in results) or counts[-1] != 28:
                raise ValueError("fixed replay behavior or physical work changed")
    ordered = sorted(elapsed)
    return {"repetitions": repetitions, "physics_steps_per_decision": counts,
        "successor_physics_steps": 20, "fallback_physics_steps": 8,
        "elapsed_ms": elapsed, "p95_ms": ordered[math.ceil(.95*repetitions)-1],
        "p99_ms": ordered[math.ceil(.99*repetitions)-1], "maximum_ms": max(elapsed),
        "measurement_scope": "two_fixed_D120_replays_with_trace_and_call_counter_no_search",
        "background_ready_margin_ticks": None,
        "background_ready_status": "joint_worker_not_implemented"}


def runtime_timing(branch):
    """Observe a real component landing, then submit one branch via Runtime."""
    if branch not in {"landing-immediate", "fallback-sneak"}:
        raise ValueError("unknown diagnostic branch")
    fixture = calibration.build_fixture()
    rows = calibration.frozen.trial_fixture_rows(fixture.trial, origin=(0, 0), feet_y=64)
    all_cells = [tuple(p) for row in rows for p in row["positions"]]
    volume = tuple((min(p[i] for p in all_cells), max(p[i] for p in all_cells)) for i in range(3))
    solids = {tuple(p): rows[1]["material"] for p in rows[1]["positions"]}
    clock = [100_000_000]
    backend = CalculatorBackend(clock, Scene(solids, volume), fixture.trial.start_local)
    backend.state = replace(initial_state(fixture), session=backend._truth_session)
    backend.movement_tick = 0
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), clock_ns=lambda: clock[0])
    deadline = clock[0]+120_000_000_000
    task = TaskIntentV0("d121-s0", "fixed_input_diagnostic", "{}",
        (SuccessCriterionV0("ticks", ComparisonOperatorV0.EQUAL, 1, "count"),),
        100, deadline, True, 0.)
    source = "d121-s0"
    adapter = NavigationObservationAdapter()
    counter = 0
    def submit(movement):
        nonlocal counter
        runtime.cancel_source(source)
        counter += 1
        now = clock[0]
        runtime.submit_intent(ActionIntentV1(f"d121-s0-{counter}", source, backend.episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, now+750_000_000, movement=single_branch_input((movement,)), valid_for_ticks=1))
        result = runtime.step(task, BehaviorProfileV0(), deadline)
        if result.observation is None or result.decision is None or result.backend_result is None:
            raise ValueError(f"formal Runtime diagnostic failed: {result.report}")
        if result.decision.action.movement != movement:
            raise ValueError("formal Runtime did not select the diagnostic input")
        return result
    try:
        if not runtime.reset(ResetRequestV0("d121-reset", backend.episode, "d121", 1, deadline)).succeeded:
            raise ValueError("component reset failed")
        for command in fixture.trial.commands[:26]:
            result = submit(command.movement())
        frame = adapter.ingest(result.observation)
        landing = frame.body.movement_tick_id
        nominal = calibration.nominal_entry(fixture)
        if (not frame.body.is_on_ground or landing != 26
                or any(abs(a-b)>1e-9 for a,b in zip(frame.body.position, nominal.position))):
            raise ValueError("Runtime did not observe the frozen landed post-state")
        replay = calibration.replay_suffix(nominal, fixture, branch)
        first = None
        for index in range(replay["steps"]):
            values = (calibration.SNEAK if branch == "fallback-sneak" else
                calibration.JUMP if index == 0 else calibration.RUN if index < 12 else calibration.NEUTRAL)
            # D120 lands on the target at the twelfth input; settle from L+13.
            result = submit(MovementV1(int(values[0]), int(values[1]), *values[2:]))
            if first is None:
                first = result
        receipt = first.backend_result.receipt.last_input_sample
        final = backend.state
        expected = replay["final_state"]
        final_position_velocity_match = (all(abs(a-b)<1e-9 for a,b in zip(final.position, expected["position"]))
            and all(abs(a-b)<1e-9 for a,b in zip(final.velocity_blocks_per_tick,
                                              expected["velocity_blocks_per_tick"])))
        selected = first.decision.action
        receipt_identity_match, receipt_movement_match = \
            receipt_matches_selected_input(receipt, selected, landing)
        return {"branch": branch, "landing_tick": landing, "decision_tick": landing,
            "first_application_tick": receipt.movement_tick_id,
            "request_sequence_id": selected.request_sequence_id,
            "receipt_identity_match": receipt_identity_match,
            "receipt_movement_match": receipt_movement_match,
            "movement_winner_count": sum(group == "movement" for group, _ in first.decision.selected_intents),
            "applied_input": {key: getattr(receipt, key) for key in ("forward", "strafe", "jump", "sneak", "sprint")},
            "final_position_velocity_match": final_position_velocity_match,
            "final_state": final.to_mapping(),
            "final_supported": calibration._supported(final, fixture,
                (1, 2) if branch == "fallback-sneak" else (6, 12)),
            "damage": backend.damage_taken,
            "scope": "direct_S0_submission_through_Runtime_not_S2_S3_prepared_handoff"}
    finally:
        runtime.cancel_source(source)
        runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--timing-repetitions", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.timing_repetitions <= 100:
        raise ValueError("timing repetitions must be within 1..100")
    if args.output.exists():
        raise ValueError("refuse to overwrite existing S0 evidence")
    result = {"schema_version": "mc2p.d121-s0-diagnostic.v1", "platform": platform.platform(),
        "python_version": platform.python_version(), "status": "diagnostic_only",
        "d120_manifest_sha256": calibration.build_manifest()["sha256"],
        "sound_envelope": "unverified", "independent_exit": independent_exit_fact(),
        "nominal": nominal_facts(), "gaps": [asdict(fact) for fact in unimplemented_facts()],
        "exclusivity": exclusivity_fact(),
        "midpoint": {"offsets": [.001, 0., 0., 0., 0.],
                     "status": sample_status((.001, 0., 0., 0., 0.))},
        "runtime": [{"repetition": repetition, **runtime_timing(branch)}
            for repetition in range(args.timing_repetitions)
            for branch in ("landing-immediate", "fallback-sneak")],
        "cost": replay_cost(args.repetitions)}
    result["checks"] = {
        "independent_exit_lacks_frozen_successor": not result["independent_exit"]["fixed_successor"]["passed"],
        "nominal_successor_and_sneak_safe": all(result["nominal"]["suffixes"][s]["passed"]
            for s in ("landing-immediate", "fallback-sneak")),
        "neutral_and_back_unsafe": all(not result["nominal"]["suffixes"][s]["passed"]
            for s in ("fallback-neutral", "fallback-back")),
        "unproven_midpoint": result["midpoint"]["status"] == SampleStatus.UNPROVEN_MIDPOINT,
        "exclusive_input_guard": len(result["exclusivity"]["rejected"]) == 2,
        "runtime_l1": all(r["landing_tick"] == r["decision_tick"] == 26
            and r["first_application_tick"] == 27 and r["movement_winner_count"] == 1
            and r["receipt_identity_match"] and r["receipt_movement_match"]
            and r["final_position_velocity_match"]
            and r["final_supported"] and r["damage"] == 0. for r in result["runtime"]),
        "fixed_replay_work": all(n == 28 for n in result["cost"]["physics_steps_per_decision"]),
    }
    result["diagnostic_checks_status"] = "passed" if all(result["checks"].values()) else "failed"
    result["source_sha256"] = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        for name in ("scripts/d121_momentum_handoff_probe.py",
                     "tests/motion_nav/test_d121_momentum_handoff.py",
                     "scripts/d120_momentum_entry_calibration.py")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as target:
        target.write(json.dumps(
            result, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )+"\n")
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
        "diagnostic_checks_status": result["diagnostic_checks_status"],
        "runtime_trials": len(result["runtime"]), "checks": result["checks"],
        "cost": {key: result["cost"][key] for key in ("p95_ms", "p99_ms", "maximum_ms")}}))
    return 0 if result["diagnostic_checks_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
