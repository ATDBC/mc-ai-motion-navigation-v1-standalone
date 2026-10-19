"""Record the D121 preparation slice using the original worker and receiver.

The component movement clock stays at zero during preparation. No incumbent
is admitted, no action is started, and no physical fallback is applied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import pickle
import sys
import time
from multiprocessing.reduction import ForkingPickler

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.motion_solver import bind_fixed_successor
from mc2p.motion_nav.motion_worker import GapMotionSolveResult
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_bytes, canonical_bundle_bytes
from tests.motion_nav.test_d121_successor_binding import FixedPreparationTests, RecordingProcessWorker


def collect_preparation_facts():
    rows = []
    for direction in range(4):
        case = FixedPreparationTests()
        case.direction_index = direction
        case.worker_factory = RecordingProcessWorker
        try:
            case.setUp()
            case.addCleanup(case.worker.close)
            case.inbox.publish_diagnostic_movement_tick(0)
            bound = bind_fixed_successor(case.proof, case.request, case.spec, case.snapshot,
                                        damage_remaining_points=0.)
            current = GapMotionSolveResult("fixed-current", 1, bound, 0, case.identity)
            if (bound.proof.successor_binding.parent_dependency_versions != bound.proof.world_dependency_versions
                    or tuple(pos for pos, _ in bound.proof.world_dependency_versions) != bound.proof.world_dependencies):
                raise AssertionError("bound parent source versions must be exact and complete")
            started = time.perf_counter_ns()
            submitted = case.coordinator.prepare_fixed_successor(current, case.request, case.context,
                case.anchor, case.fixture.context.world, ledger_identity="fixed-ledger",
                expected_work_identity=case.identity)
            if submitted.status.value != "accepted" or not case.wait_ready(case):
                raise AssertionError("the actual worker did not publish a complete candidate")
            first_visible_ns = time.perf_counter_ns()
            job = case.worker.jobs[0]
            wire = case.worker.messages[0]
            bundle = pickle.loads(wire.payload)
            sizes = {"current_result_canonical": len(canonical_motion_bytes(current)),
                "current_result_pickle5": len(pickle.dumps(current, protocol=5)),
                "prepared_job_canonical": len(canonical_motion_bytes(job)),
                "prepared_job_pickle5": len(pickle.dumps(job, protocol=5)),
                "prepared_job_queue_sender": len(ForkingPickler.dumps(job)),
                "bundle_canonical": len(canonical_bundle_bytes(bundle)),
                "wire_protocol5_payload": len(wire.payload)}
            if any(size > 1048576 for size in sizes.values()):
                raise AssertionError(f"formal message exceeds the half-limit: {sizes}")
            applied = case.coordinator.apply_fixed_successor_preparation(
                movement_tick=0, current_scope=case.identity.scope)
            if not applied or case.eligibility().value != "ready_match":
                raise AssertionError("the complete candidate did not follow async apply")
            diagnostics = case.inbox.drain_receiver_diagnostics()
            rows.append({"direction_index": direction, "sizes_bytes": sizes,
                "actual_process_transport": True, "worker_jobs": len(case.worker.jobs),
                "worker_results": len(case.worker.messages),
                "actual_current_worker_submission": False,
                "current_result_boundary": "format-only; D120 fixed first proof wrapped by component",
                "movement_clock": "frozen deterministic component clock; not full Runtime frames",
                "worker_completed_tick": None,
                "worker_completion_boundary": "prepared wire has no worker completion tick; not inferred",
                "receiver_published_tick": 0, "first_control_visible_ready_tick": 0,
                "landing_tick": 26, "preparation_margin_component_ticks": 26,
                "wall_submit_to_visible_ms": (first_visible_ns - started) / 1e6,
                "receiver_diagnostics": diagnostics,
                "requested_window": [job.request.requested_window.earliest_start_tick,
                                     job.request.requested_window.latest_start_tick],
                "input_projection_version": job.request.stream.suffix_identity[0].input_projection_version,
                "parent_dependency_positions_exact": True,
                "parent_source_versions_exact": True,
                "main_dependency_count": len(job.request.inherited_dependencies),
                "full_proof_dependency_count": len(bundle.proof.dependency_versions),
                "dependency_basis": "same-source exact parent/main versions and complete registered proof/tail versions",
                "branches": [branch.start_branch.value for branch in bundle.proof.branches],
                "counts": {name: getattr(bundle.proof, name) for name in (
                    "nodes_scanned", "physics_steps", "stopping_tail_steps", "completed_candidates")},
                "feasibility_physics_steps": bound.proof.successor_binding.physics_steps,
                "parent_verification_physics_steps": bound.proof.successor_binding.parent_verification_steps,
                "async_operations": [event.operation for event in case.coordinator._work.events],
                "action_index": case.executor.action_index,
                "successor_controller_activated": case.executor.prepared_controller is not None,
                "fallback_physically_applied": False})
        finally:
            case.doCleanups()
    return {"schema": "mc2p.d121-task3-preparation.v1", "scope": "preparation only",
            "hard_limit_bytes": 2097152, "representative_limit_bytes": 1048576,
            "directions": rows,
            "historical_stop_sha256": hashlib.sha256((ROOT / (
                "evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json")).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to replace existing Task 3 evidence")
    result = collect_preparation_facts()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"directions": len(result["directions"]),
                      "maximum_message_bytes": max(v for row in result["directions"]
                          for v in row["sizes_bytes"].values())}))


if __name__ == "__main__":
    main()
