"""D121 timely-input component matrix; every command uses the formal driver."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict, replace
import json
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
import pickle
import sys
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.d121_handoff_execution_probe import ExecutionCase
from mc2p.contracts.motion_activation import ActivationResult, ActivationStatus, ActivationRejectReason
from mc2p.contracts.action_v1 import MovementTickWindowV1
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.motion_worker import GapMotionSolveResult
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_bytes, canonical_bundle_bytes
from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
from mc2p.motion_nav.world_snapshot import DependencyStatus
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriverState
from tests.motion_nav.test_d121_successor_binding import RecordingProcessWorker
from tests.sim.runner import _goal


SCENARIOS = ("ready", "late2", "late5", "late10", "never", "worker_dead",
    "expired_dispatch", "old_generation", "successor_dependency", "stamp_only",
    "cancel15", "cancel23", "cancel24", "cancel25", "cancel26", "cancel29",
    "replace_goal", "prepare_activation", "accept_activation", "deliver_activation", "commit_activation")


def run_case(direction, scenario):
    process = scenario == "process_ready"
    case = ExecutionCase(direction_index=direction,
        worker=RecordingProcessWorker() if process else None)
    rows = []
    expected_success = scenario in {"ready", "stamp_only", "process_ready"}
    started = time.perf_counter_ns()
    first_ready = None
    source_released_tick = None
    try:
        job = case.prepare_successor(deliver=scenario not in {"late2", "late5", "late10", "never", "worker_dead", "cancel29"})
        if scenario == "worker_dead":
            case.worker.alive = False
        reject = ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        for _ in range(70):
            tick = case.backend.movement_tick
            if scenario.startswith("late") and tick == 26 + int(scenario[4:]):
                case.worker.put(case.message)
                assert case.session._motion_result_inbox.wait_for_receiver(timeout=10)
            if scenario.startswith("cancel") and tick == int(scenario[6:]):
                case.session.cancel("matrix-cancel")
            if scenario == "replace_goal" and tick == 24:
                assert case.driver.replace_goal("fixed-goal", 2, _goal(case.proof.entry_state.position), case.clock[0])
            if tick == 25 and scenario == "successor_dependency":
                parent = set(case.proof.world_dependencies)
                successor = set(case.proof.successor_binding.dependency_versions)
                positions = sorted(pos for pos, _version in successor if pos not in parent)
                assert positions
                position = next(pos for pos in positions if pos in case.backend.scene.solids)
                # Truth changes before the next backend observation. The normal
                # Runtime adapter publishes the new semantic dependency version.
                case.backend.perturbations.world_edits[tick + 1] = {position: "minecraft:stone"}
            if tick == 26 and scenario == "successor_dependency":
                assert case.source.dependency_changes(case.proof.world_dependency_versions).status is DependencyStatus.UNCHANGED
            before = len(case.backend.actions)
            owner_before = None if case.session._supervisor.route is None else case.session._supervisor.route.owner_id
            dispatch_transform = None
            if tick == 26 and scenario in {"expired_dispatch", "old_generation"}:
                def dispatch_transform(dispatches):
                    assert len(dispatches) == 1
                    mutation = ({"requested_window": MovementTickWindowV1(26, 26)}
                        if scenario == "expired_dispatch" else {"generation_id": "retired-generation"})
                    return (replace(dispatches[0], proposal=replace(dispatches[0].proposal, **mutation)),)
            before_ns = time.perf_counter_ns()
            rejection = patch.object(ExecutionSupervisor, scenario, return_value=reject) if tick == 26 and scenario.endswith("_activation") else nullcontext()
            with rejection:
                result = case.tick(dispatch_transform=dispatch_transform)
            elapsed = (time.perf_counter_ns() - before_ns) / 1e6
            assert result.report.failure is None, result.report.failure
            assert len(case.backend.actions) == before + 1
            assert case.backend.movement_tick == tick + 1
            winners = sum(group == "movement" for group, _ in result.decision.selected_intents)
            assert winners == 1 if tick <= 35 or expected_success else winners in (0, 1)
            ledger = case.runtime.input_ledger
            record = ledger.record(result.decision.action.request_sequence_id)
            assert record is not None and record.applied_ticks == (tick + 1,)
            assert record.control_sequence == result.decision.action.request_sequence_id
            if tick <= 35:
                assert owner_before == "route/fixed-route", (tick, owner_before)
            if tick == 26:
                assert result.decision.action.movement.jump == expected_success
                assert result.decision.action.movement.sneak != expected_success
                assert case.session._executor.action_index == int(expected_success)
                if not expected_success:
                    assert record.stream_command is None
                    assert case.controller._pending.control_sequence == record.control_sequence
            if (first_ready is None and case.session._executor is not None
                    and case.session._executor._prepared_handle(1) is not None):
                first_ready = tick
            activation = None if result.stream_activation is None else result.stream_activation.status.value
            rows.append({"source_tick": tick, "effect_tick": case.backend.movement_tick,
                "sequence": record.control_sequence, "ledger_id": ledger.ledger_identity,
                "owner": owner_before, "movement": asdict(result.decision.action.movement),
                "winner_count": sum(group == "movement" for group, _ in result.decision.selected_intents),
                "action_index": case.session._executor.action_index if case.session._executor is not None else None,
                "activation": activation, "tail_cursor": case.controller._tail_cursor,
                "phase": None if case.controller._committed_phase is None else case.controller._committed_phase.value,
                "elapsed_ms": elapsed, "driver": case.driver.state.value})
            if expected_success and case.driver.state is RuntimeNavigationDriverState.SUCCESS:
                case.driver.release("matrix-completed-route")
            if case.driver.source is None:
                source_released_tick = case.backend.movement_tick
                break
            if process:
                # A real 20 Hz caller advances movement while the same worker runs.
                time.sleep(max(0., .05 - (time.perf_counter_ns() - before_ns) / 1e9))
        assert source_released_tick is not None, (scenario, "source not released")
        assert case.backend.damage_taken == 0.
        assert case.backend.state.on_ground
        assert not case.session.active_motion_mailboxes
        handles_before_close = len(case.session._motion_result_inbox.registered_handles)
        assert handles_before_close == 0, handles_before_close
        if not expected_success:
            assert case.controller._committed_phase.value == "released"
            assert case.controller._tail_cursor == len(case.proof.fallback_tails[-1].inputs) + 1
        before = len(case.backend.actions)
        for _ in range(4):
            case.backend.free_tick()
        assert len(case.backend.actions) == before
        assert case.driver.source is None
        assert case.backend.damage_taken == 0.
        messages = getattr(case.worker, "messages", ())
        if process:
            job = next(item for item in case.worker.jobs if hasattr(item, "request"))
            wire = next(item for item in messages if hasattr(item, "payload"))
        else:
            wire = case.message
        bundle = pickle.loads(wire.payload)
        current = GapMotionSolveResult("component-current", 1, case.bound, 0, case.identity)
        sizes = {"current_result_canonical": len(canonical_motion_bytes(current)),
            "current_result_pickle5": len(pickle.dumps(current, protocol=5)),
            "prepared_job_canonical": len(canonical_motion_bytes(job)),
            "prepared_job_queue_sender": len(ForkingPickler.dumps(job)),
            "bundle_canonical": len(canonical_bundle_bytes(bundle)), "wire_protocol5_payload": len(wire.payload)}
        assert max(sizes.values()) <= 1048576, sizes
        return {"direction_index": direction, "scenario": scenario, "result": "PASS",
            "route_injection": "manual legal route plus TEST_ORACLE; no planner claim",
            "expected_success": expected_success, "actual_process_worker": process,
            "L": 26, "first_installed_visible_tick": first_ready,
            "source_released_tick": source_released_tick, "post_release_free_ticks": 4,
            "worker_submit_to_end_ms": (time.perf_counter_ns() - started) / 1e6,
            "damage": case.backend.damage_taken, "final_driver": case.driver.state.value,
            "current_proof": case.proof.successor_binding.parent_main_digest,
            "successor_binding": job.request.stream.suffix_identity[0].commands_digest,
            "registered_handles_after_source_release": handles_before_close,
            "sizes_bytes": sizes, "frames": rows}
    finally:
        case.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--directions", default="0,1,2,3")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    args = parser.parse_args()
    assert not args.output.exists(), "evidence is append-only"
    result = {"schema": "mc2p.d121-task4-execution.v1", "runs": []}
    try:
        for direction in map(int, args.directions.split(",")):
            for scenario in args.scenarios.split(","):
                row = run_case(direction, scenario)
                result["runs"].append(row)
                print(json.dumps({"direction": direction, "scenario": scenario, "result": row["result"]}), flush=True)
        result["result"] = "PASS"
    except BaseException:
        result["result"] = "FAILED"
        result["failed_case"] = {"direction_index": direction, "scenario": scenario}
        result["failure"] = traceback.format_exc()
        print(result["failure"], flush=True)
        raise
    finally:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
