"""Small, deterministic probes used to freeze the F2-R recovery baseline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
_HOTPATH_IDS = {
    "straight": "f2/offset/south/normal",
    "turn": "f2/centre_to_offset/south/normal",
    "wall_contact": "f2/tangent/south/normal",
}
_RUNTIME_TOOL_PATHS = (
    "scripts/benchmark_d061_long_session.py",
    "scripts/f2_ground_route_evidence.py",
    "scripts/f2rec_r0_baseline.py",
    "scripts/f2rec_r0_v9.py",
    "scripts/run_motion_navigation_checks.py",
    "tests/motion_nav/test_d061_long_session_benchmark.py",
    "tests/motion_nav/test_f2rec_r0_baseline.py",
    "tests/sim/f2s_cases.py",
    "tests/sim/known_world_following.py",
    "tests/sim/manifests/navigation-product-f2s-support-region-v9.json",
)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=ROOT, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def collect_tool_provenance() -> dict:
    """Record exactly which dirty-checkout tools produced the R0 evidence."""
    runtime_tools = {}
    for relative in _RUNTIME_TOOL_PATHS:
        payload = (ROOT / relative).read_bytes()
        runtime_tools[relative] = hashlib.sha256(payload).hexdigest()
    tracked = tuple(filter(None, _git("diff", "--name-only").splitlines()))
    untracked = tuple(filter(
        None, _git("ls-files", "--others", "--exclude-standard").splitlines(),
    ))
    worktrees = _git("worktree", "list", "--porcelain")
    return {
        "schema_version": "mc2p.f2rec-r0-tool-provenance.v1",
        "formal_platform": "windows",
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "branch": _git("branch", "--show-current"),
        "head_commit": _git("rev-parse", "HEAD"),
        "checkout_kind": "shared_primary_checkout",
        "worktree_listing_sha256": hashlib.sha256(
            worktrees.encode("utf-8"),
        ).hexdigest(),
        "tracked_dirty_paths": tracked,
        "untracked_paths": untracked,
        "runtime_tools": runtime_tools,
        "verification_policy": (
            "The full forward/reverse baseline belongs to this exact dirty "
            "tool set. After the final commit, rerun affected targeted checks "
            "and hash verification; do not relabel it as a new full run."
        ),
    }


def collect_ground_hotpaths(*, repeats: int = 5) -> dict:
    """Measure three frozen ordinary-ground routes without changing behavior."""
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError("hotpath repeats must be within 1..20")
    from scripts.f2_ground_route_evidence import run_route

    manifest = json.loads((
        ROOT / "tests/sim/manifests/navigation-product-f2-ground-route-v1.json"
    ).read_text("utf-8"))
    cases = {row["id"]: row for row in manifest["tasks"]}
    routes = {}
    for label, case_id in _HOTPATH_IDS.items():
        runs = [run_route(cases[case_id]) for _ in range(repeats)]
        routes[label] = {
            "case_id": case_id,
            "runs": repeats,
            "frame_samples": sum(row["frames"] for row in runs),
            "median_control_ms": statistics.median(
                row["control_ms"]["p50"] for row in runs
            ),
            "maximum_control_ms": max(
                row["control_ms"]["maximum"] for row in runs
            ),
            "safety_events": sum(bool(row["gate_violations"]) for row in runs),
            "outcomes": [row["outcome"] for row in runs],
            "behavior_sha256": [row["behavior_sha256"] for row in runs],
        }
    return {
        "schema_version": "mc2p.f2rec-r0-ground-hotpaths.v1",
        "routes": routes,
    }


def _formal_gap_runtime():
    """Start the same Runtime -> Session -> verified-gap chain used by tests."""
    from tests.motion_nav.test_runtime_navigation_verified_handoff import (
        RuntimeVerifiedMotionHandoffTests,
    )

    fixture = RuntimeVerifiedMotionHandoffTests(
        "test_runtime_bridge_keeps_landing_owner_when_anchor_disappears",
    )
    return fixture._running_gap()


def _probe_worker_death() -> dict:
    from mc2p.contracts.action_v1 import MovementV1
    from mc2p.contracts.behavior import BehaviorProfileV0
    from mc2p.motion_nav.navigation_session import NavigationSessionState

    clock, _backend, runtime, session, driver = _formal_gap_runtime()
    executor = session._executor
    worker = session._motion_worker
    try:
        alive_before = worker is not None and worker.is_alive()
        if worker is None:
            raise RuntimeError("formal verified motion did not create a worker")
        worker._process.terminate()
        worker._process.join(2.0)
        result = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )
        owner_retained = session._executor is executor and driver.source is not None
        neutral = (
            result.decision is not None
            and result.decision.action.movement == MovementV1()
        )
        bounded_safe = (
            result.report.failure is None
            and session.report.state in {
                NavigationSessionState.EXECUTING,
                NavigationSessionState.CANCELLING,
                NavigationSessionState.FAILED,
            }
        )
        return {
            "worker_alive_before": alive_before,
            "worker_alive_after_injection": worker.is_alive(),
            "airborne_owner_retained": owner_retained,
            "neutral_or_bounded_safe_result": neutral or bounded_safe,
            "session_state": session.report.state.value,
            "session_reason": session.report.reason,
            "runtime_state": runtime.state.value,
        }
    finally:
        session.close()
        runtime.close()


def _probe_retired_late_result() -> dict:
    """Deliver an old result through the real coordinator/inbox boundary."""
    from mc2p.motion_nav.action_route_executor import ActionRouteState
    from mc2p.motion_nav.async_work import (
        AsyncAdmissionDisposition, AsyncComputationScope,
    )
    from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
    from mc2p.motion_nav.motion_worker import MotionSolverWorker, _execute_job
    from mc2p.motion_nav.online_motion import InputApplicationLedger
    from mc2p.motion_nav.retry_ledger import RetryLedger
    from tests.motion_nav.test_b10_motion_candidate import (
        VerifiedMotionRouteIntegrationTests,
    )

    fixture = VerifiedMotionRouteIntegrationTests(
        "test_old_motion_revision_cannot_terminate_new_revision",
    )
    anchor, physics_world, active, executor = fixture.coordinator_fixture(
        "f2rec-stale-motion-revision",
    )
    clock = [1]
    worker = MotionSolverWorker(max_pending=1)
    jobs = []
    try:
        ledger = RetryLedger(active.goal_id)
        coordinator = MotionRouteCoordinator(
            active, executor, worker, retry_ledger=ledger,
            clock_ns=lambda: clock[0],
            computation_scope=AsyncComputationScope(
                active.world_session, ledger.task_id, 1,
            ),
        )
        current_frame = fixture.frame(
            physics_world._world, anchor.physics_state, 1,
        )
        coordinator.start(current_frame)
        with patch.object(worker, "is_alive", return_value=True), \
                patch.object(worker, "poll_available", return_value=()), \
                patch.object(
                    worker, "submit",
                    side_effect=lambda job: jobs.append(job) or True,
                ):
            coordinator.decide(
                current_frame, anchor, InputApplicationLedger(max_records=64),
                physics_world, changed_cells=(),
                current_scope=coordinator.computation_scope,
            )
        old_result = _execute_job(jobs[0])
        old_identity = jobs[0].work_identity
        clock[0] += 1_100_000_000
        with patch.object(worker, "is_alive", return_value=True), \
                patch.object(worker, "poll_available", return_value=(old_result,)), \
                patch.object(
                    worker, "submit",
                    side_effect=lambda job: jobs.append(job) or True,
                ):
            decision = coordinator.decide(
                fixture.frame(physics_world._world, anchor.physics_state, 2),
                anchor, InputApplicationLedger(max_records=64), physics_world,
                changed_cells=(), current_scope=coordinator.computation_scope,
            )
        admission = coordinator.last_admission
        return {
            "old_identity_retired": jobs[1].work_identity != old_identity,
            "late_result_disposition": admission.disposition.value,
            "late_result_identity_matched": admission.identity == old_identity,
            "retired_result_executable": not (
                admission.disposition is AsyncAdmissionDisposition.DISCARDED_LATE
                and decision.verified_command_index is None
                and executor.state is ActionRouteState.RUNNING
            ),
            "replacement_identity_active": (
                coordinator._work_identity == jobs[1].work_identity
            ),
        }
    finally:
        worker.close()


def _probe_cancel_backpressure() -> dict:
    from mc2p.motion_nav.action_route_executor import ActionRouteState
    from mc2p.motion_nav.async_work import AsyncComputationScope
    from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
    from mc2p.motion_nav.motion_worker import MotionSolverWorker
    from mc2p.motion_nav.online_motion import InputApplicationLedger
    from mc2p.motion_nav.retry_ledger import RetryLedger
    from tests.motion_nav.test_b10_motion_candidate import (
        VerifiedMotionRouteIntegrationTests,
    )

    fixture = VerifiedMotionRouteIntegrationTests(
        "test_motion_backpressure_uses_fixed_deadline_and_shared_retry_limit",
    )
    anchor, physics_world, active, executor = fixture.coordinator_fixture(
        "f2rec-bounded-motion-backpressure",
    )
    clock = [1]
    worker = MotionSolverWorker(max_pending=1)
    try:
        ledger = RetryLedger(active.goal_id)
        coordinator = MotionRouteCoordinator(
            active, executor, worker, retry_ledger=ledger,
            clock_ns=lambda: clock[0],
            computation_scope=AsyncComputationScope(
                active.world_session, ledger.task_id, 1,
            ),
        )
        coordinator.start(fixture.frame(
            physics_world._world, anchor.physics_state, 1,
        ))
        decision = None
        with patch.object(worker, "is_alive", return_value=True), \
                patch.object(worker, "poll_available", return_value=()), \
                patch.object(worker, "submit", return_value=False):
            for sequence in range(1, 5):
                decision = coordinator.decide(
                    fixture.frame(
                        physics_world._world, anchor.physics_state, sequence,
                    ),
                    anchor, InputApplicationLedger(max_records=64),
                    physics_world, changed_cells=(),
                    current_scope=coordinator.computation_scope,
                )
                clock[0] += 1_100_000_000
        return {
            "decision_count": 4,
            "bounded_terminal_reached": (
                decision is not None
                and decision.state is ActionRouteState.UNSUPPORTED
            ),
            "terminal_reason": None if decision is None else decision.reason_code,
            "verified_command_executed": (
                decision is not None and decision.verified_command_index is not None
            ),
            "local_failure_count": coordinator._local_attempts.failure_count,
            "task_recovery_starts": ledger.total_recovery_starts,
        }
    finally:
        worker.close()


def _probe_runtime_close() -> dict:
    from mc2p.contracts.action_v1 import MovementV1

    _clock, backend, runtime, session, _driver = _formal_gap_runtime()
    worker = session._motion_worker
    try:
        alive_before = worker is not None and worker.is_alive()
        stats_before = runtime.ordered_source_stats
        runtime.close()
        stats_after = runtime.ordered_source_stats
        neutral_release = bool(
            backend.actions and backend.actions[-1].movement == MovementV1()
        )
        return {
            "worker_alive_before": alive_before,
            "worker_alive_after_runtime_close": (
                worker is not None and worker.is_alive()
            ),
            "neutral_release_sent": neutral_release,
            "source_stats_before": stats_before,
            "source_stats_after": stats_after,
            "input_ownership_cleared": (
                stats_after["active_sources"] == 0
                and stats_after["active_intents"] == 0
            ),
            "runtime_state": runtime.state.value,
        }
    finally:
        session.close()
        runtime.close()


def worker_lifecycle_probe() -> dict:
    """Exercise four lifecycle boundaries through real production objects."""
    death = _probe_worker_death()
    backpressure = _probe_cancel_backpressure()
    stale = _probe_retired_late_result()
    runtime_close = _probe_runtime_close()
    death_passed = (
        death["airborne_owner_retained"]
        and death["neutral_or_bounded_safe_result"]
    )
    backpressure_passed = (
        backpressure["bounded_terminal_reached"]
        and not backpressure["verified_command_executed"]
    )
    stale_passed = not stale["retired_result_executable"]
    close_safety_passed = (
        runtime_close["neutral_release_sent"]
        and runtime_close["input_ownership_cleared"]
    )
    cases = {
        "ready_after_death": {
            "status": "PASS" if death_passed else "RED",
            "risk": "closed" if death_passed else "hard_lifecycle_risk",
            "evidence": "Killed the live solver during an airborne verified action.",
            "observed": death,
        },
        "cancel_backpressure": {
            "status": "PASS" if backpressure_passed else "RED",
            "risk": "closed" if backpressure_passed else "hard_lifecycle_risk",
            "evidence": "Held the production submit boundary under backpressure.",
            "observed": backpressure,
        },
        "pre_publish_invalidation": {
            "status": "PASS" if stale_passed else "RED",
            "risk": "closed" if stale_passed else "hard_lifecycle_risk",
            "evidence": "Delivered a completed old revision after replacement.",
            "observed": stale,
        },
        "runtime_close": {
            "status": "PASS" if close_safety_passed else "RED",
            "risk": (
                "lifecycle_debt"
                if close_safety_passed
                and runtime_close["worker_alive_after_runtime_close"]
                else "closed" if close_safety_passed
                else "hard_lifecycle_risk"
            ),
            "evidence": "Closed Runtime while its Session-owned worker was live.",
            "observed": runtime_close,
        },
    }
    return {
        "schema_version": "mc2p.f2rec-r0-worker-lifecycle.v1",
        "probe_kind": "dynamic_formal_path",
        "cases": cases,
        "current_hard_risk": any(
            result["status"] == "RED"
            and result["risk"] == "hard_lifecycle_risk"
            for result in cases.values()
        ),
    }
