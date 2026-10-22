"""D122-3 formal Runtime timing matrix on the calculator client."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.action_v1 import ActionSnapshotV1
from scripts.d121_handoff_execution_probe import ExecutionCase
from tests.sim.backend import CalculatorBackend, Perturbations


POLICIES = (
    "timely_l1", "late_l2", "never_delivered", "explicit_reject",
    "cancel",
)
L = 26
EXPECTED_TICK = L + 1


class D122TimingBackend(CalculatorBackend):
    """Inject successor transport timing without bypassing the v2 sampler."""

    def __init__(self, *args, transport_policy: str,
                 raise_never_timeout: bool = False,
                 explicit_reject_reason: str = "successor_explicitly_rejected",
                 **kwargs):
        if transport_policy not in POLICIES:
            raise ValueError("unknown D122 transport policy")
        if "perturbations" in kwargs:
            raise ValueError("D122 timing backend owns transport perturbations")
        late = frozenset({EXPECTED_TICK}) if transport_policy == "late_l2" else frozenset()
        super().__init__(*args, perturbations=Perturbations(late_ticks=late),
                         **kwargs)
        self.transport_policy = transport_policy
        self.raise_never_timeout = raise_never_timeout
        self.explicit_reject_reason = explicit_reject_reason
        self.successor_sequence = None
        self.transport_events: list[dict[str, object]] = []

    @staticmethod
    def _is_successor(action: ActionSnapshotV1) -> bool:
        window = action.first_effect_window
        return (window is not None
                and window.earliest_tick == EXPECTED_TICK
                and window.latest_tick == EXPECTED_TICK
                and action.movement.jump)

    def step(self, action, deadline, *, observation_request=None):
        if (self.transport_policy not in {"never_delivered", "explicit_reject"}
                or not self._is_successor(action)):
            result = super().step(
                action, deadline, observation_request=observation_request,
            )
            if self._is_successor(action):
                self.successor_sequence = action.request_sequence_id
            return result

        # The request used Runtime's sole backend exit but never reached the
        # client. Keep advancing the already-installed client arrangement;
        # do not manufacture intermediate Python observations or receipts.
        self.successor_sequence = action.request_sequence_id
        reject = self.transport_policy == "explicit_reject"
        self.transport_events.append({
            "kind": ("successor_explicitly_rejected" if reject
                     else "successor_lost_before_client"),
            "sequence": action.request_sequence_id,
            "source_tick": self.movement_tick,
            "attempted_request": {
                "request_sequence_id": action.request_sequence_id,
                "first_effect_window": asdict(action.first_effect_window),
                "injection_reason": (
                    self.explicit_reject_reason if reject
                    else "successor_transport_not_delivered"
                ),
            },
        })
        self.actions.append(action)
        self.sequence += 1
        start_tick = self.movement_tick
        self.clock[0] += 50_000_000
        if reject:
            self._client_timing.reject_admission(
                action, self.movement_tick + 1, self.clock[0],
                self.sequence - 1,
            )
        _, _, sample_state = self._sample_timed_command(None)
        while self._client_timing.default_parent is not None:
            self.clock[0] += 50_000_000
            _, _, sample_state = self._sample_timed_command(None)
        self.response_timing.append({
            "request_sequence_id": action.request_sequence_id,
            "waited_ticks": self.movement_tick - start_tick,
            "waited_ns": (self.movement_tick - start_tick) * 50_000_000,
        })
        if not reject and self.raise_never_timeout:
            raise TimeoutError("D122 injected successor transport loss")
        return self._step_response(
            action, observation_request, sample_state,
            status="rejected",
            reason=(self.explicit_reject_reason if reject
                    else "successor_transport_not_delivered"),
        )


def _factory(policy: str, *, raise_never_timeout: bool = False,
             explicit_reject_reason: str = "successor_explicitly_rejected"):
    def build(*args, **kwargs):
        return D122TimingBackend(
            *args, transport_policy=policy,
            raise_never_timeout=raise_never_timeout,
            explicit_reject_reason=explicit_reject_reason, **kwargs,
        )
    return build


def _event_rows(backend: D122TimingBackend):
    return [{**asdict(event), "status": event.status.value}
            for event in backend._client_timing.events]


def _profiled_tick(case):
    """Measure synchronous Python work apart from backend response holding."""
    deadline = case.clock[0] + case.step_window_ns
    started = time.perf_counter_ns()
    proposals = case.driver.prepare_proposals(deadline)
    submit_ns = time.perf_counter_ns() - started
    backend_before = case.runtime.backend_elapsed_ns_total
    started = time.perf_counter_ns()
    result = case.driver.advance_prepared(
        BehaviorProfileV0(), proposals=proposals,
    )
    control_wall_ns = time.perf_counter_ns() - started
    backend_ns = case.runtime.backend_elapsed_ns_total - backend_before
    return result, {
        "submit_compute_ns": submit_ns,
        "control_wall_ns": control_wall_ns,
        "backend_wait_ns": backend_ns,
        "control_compute_ns": max(0, control_wall_ns - backend_ns),
    }


def run_case(policy: str, *, direction_index: int = 0,
             max_frames: int = 80,
             raise_never_timeout: bool = False,
             explicit_reject_reason: str = "successor_explicitly_rejected",
             ) -> dict[str, object]:
    with patch(
        "scripts.d121_handoff_execution_probe.CalculatorBackend",
        _factory(
            policy, raise_never_timeout=raise_never_timeout,
            explicit_reject_reason=explicit_reject_reason,
        ),
    ):
        case = ExecutionCase(
            direction_index=direction_index,
            step_window_ns=1_000_000_000,
            action_timing_v2=True,
        )
    error = None
    frames = []
    timings = []
    runtime_failure = None
    transport_facts = []
    parent_action = successor_action = None
    cancel_requested_at_tick = None
    try:
        case.prepare_successor()
        for _ in range(max_frames):
            source_tick = case.backend.movement_tick
            if (policy == "cancel" and cancel_requested_at_tick is None
                    and source_tick == L):
                cancel_requested_at_tick = source_tick
                case.driver.release("d122-formal-cancel")
                if case.driver.source is None:
                    break
            try:
                result, timing = _profiled_tick(case)
                timings.append(timing)
                if result.transport_fact is not None:
                    transport_facts.append(asdict(result.transport_fact))
                if result.report.failure is not None:
                    runtime_failure = {
                        "code": result.report.failure.code.value,
                        "message": result.report.failure.message,
                        "retryable": result.report.failure.retryable,
                        "source": result.report.failure.source,
                    }
            except Exception as exception:
                error = {"type": type(exception).__name__, "message": str(exception)}
                try:
                    case.driver.release("d122-formal-transport-ended")
                except Exception as release_error:
                    error["release_error"] = {
                        "type": type(release_error).__name__,
                        "message": str(release_error),
                    }
                break
            action = result.decision.action
            if action.safe_default is not None:
                parent_action = action
            if D122TimingBackend._is_successor(action):
                successor_action = action
            frames.append({
                "source_tick": source_tick,
                "effect_tick": case.backend.movement_tick,
                "sequence": action.request_sequence_id,
                "first_effect_window": (
                    None if action.first_effect_window is None
                    else asdict(action.first_effect_window)
                ),
                "safe_default": (
                    None if action.safe_default is None
                    else asdict(action.safe_default)
                ),
                "selected_intents": list(result.decision.selected_intents),
                "owner": (None if case.session._supervisor.route is None
                          else case.session._supervisor.route.owner_id),
                "driver_state": case.driver.state.value,
                "failure": (None if result.report.failure is None
                            else result.report.failure.code.value),
            })
            if (result.report.failure is not None
                    and case.runtime.state.value != "ready"):
                case.driver.release("d122-formal-transport-failure")
                # Runtime has sealed the actor boundary. A second driver tick
                # would only produce "formal Runtime must be ready" and would
                # hide the typed failure that ended this trial.
                break
            if case.driver.state.value == "success":
                case.driver.release("d122-formal-probe-complete")
            if case.driver.source is None:
                break
        events = _event_rows(case.backend)
        if successor_action is None:
            successor_action = next((
                action for action in reversed(case.backend.actions)
                if D122TimingBackend._is_successor(action)
            ), None)
        successor_sequence = case.backend.successor_sequence
        successor_samples = [row for row in case.backend._sample_records
                             if row["request_sequence_id"] == successor_sequence]
        default_samples = [row for row in case.backend._sample_records
                           if row["state"] == "default_active"]
        jump_samples = [row for row in successor_samples if row["jump"]]
        expected_default = None if parent_action is None else parent_action.safe_default
        timely = policy == "timely_l1"
        safe_stop = (case.backend.state.on_ground and case.backend.damage_taken == 0.0
                     and abs(case.backend.state.velocity_blocks_per_tick[0]) <= .01
                     and abs(case.backend.state.velocity_blocks_per_tick[2]) <= .01)
        lifecycle_closed = (
            error is None and not case.driver.has_prepared_frame
            and (
                (policy == "never_delivered"
                 and case.driver.source is not None
                 and case.driver.state.value == "unresolved")
                or (policy != "never_delivered"
                    and case.driver.source is None
                    and case.driver.state.value in {
                        "success", "failed", "stopped", "cancelled",
                    })
            )
        )
        passed = (
            parent_action is not None
            and (successor_action is not None or policy == "cancel")
            and expected_default is not None
            and expected_default.valid_for_ticks == len(case.proof.fallback_tails[-1].inputs)
            and any(event["status"] == "fallback_armed" for event in events)
            and lifecycle_closed
            and (
                (
                    timely and len(jump_samples) == 1
                    and jump_samples[0]["movement_tick_id"] == EXPECTED_TICK
                )
                or (policy == "cancel" and not jump_samples and safe_stop)
                or (
                    policy not in {"timely_l1", "cancel"}
                    and not jump_samples
                    and len(default_samples) == expected_default.valid_for_ticks
                    and safe_stop
                )
            )
        )
        return {
            "schema": "mc2p.d122-formal-timing-case.v1",
            "policy": policy,
            "direction_index": direction_index,
            "passed": passed,
            "L": L,
            "expected_successor_tick": EXPECTED_TICK,
            "cancel_requested_at_tick": cancel_requested_at_tick,
            "parent_action": None if parent_action is None else asdict(parent_action),
            "successor_action": None if successor_action is None else asdict(successor_action),
            "timing_events": events,
            "successor_samples": successor_samples,
            "default_samples": default_samples,
            "response_timing": list(case.backend.response_timing),
            "synchronous_timings": timings,
            "final_body": {"position": list(case.backend.state.position),
                           "velocity": list(case.backend.state.velocity_blocks_per_tick),
                           "on_ground": case.backend.state.on_ground,
                           "damage": case.backend.damage_taken},
            "runtime_state": case.runtime.state.value,
            "final_observation_sequence_id": case.runtime.observation.sequence_id,
            "final_movement_tick_id": (
                case.runtime.observation.self_state.value.movement_tick_id
            ),
            "driver_state": case.driver.state.value,
            "goal_satisfied": case.driver.state.value == "success",
            "runtime_failure": runtime_failure,
            "source_released": case.driver.source is None,
            "prepared_frame": case.driver.has_prepared_frame,
            "ledger_records": [{
                "sequence": record.action.request_sequence_id,
                "status": record.status.value,
                "default_resolved_at_tick": record.default_resolved_at_tick,
                "stream_command": None if record.stream_command is None else {
                    "generation_id": record.stream_command.generation_id,
                    "command_index": record.stream_command.command_index,
                },
            } for record in case.runtime.input_ledger.snapshot()],
            "transport_events": list(case.backend.transport_events),
            "transport_facts": transport_facts,
            "error": error,
            "frames": frames,
        }
    finally:
        case.close()


def run_matrix() -> dict[str, object]:
    runs = [
        run_case(policy, direction_index=direction)
        for direction in range(4)
        for policy in POLICIES
    ]
    submit = sorted(
        timing["submit_compute_ns"] / 1_000_000
        for run in runs for timing in run["synchronous_timings"]
    )
    control = sorted(
        timing["control_compute_ns"] / 1_000_000
        for run in runs for timing in run["synchronous_timings"]
    )
    def percentile(values, q):
        return values[min(len(values) - 1, int((len(values) - 1) * q))]
    performance = {
        "sample_count": len(control),
        "submit_max_ms": max(submit),
        "control_p95_ms": percentile(control, .95),
        "control_p99_ms": percentile(control, .99),
        "control_max_ms": max(control),
        "response_wait_reported_separately": True,
    }
    performance["passed"] = (
        performance["submit_max_ms"] < 50.0
        and performance["control_p95_ms"] <= 8.0
        and performance["control_p99_ms"] <= 15.0
        and performance["control_max_ms"] < 30.0
    )
    return {
        "schema": "mc2p.d122-formal-timing-matrix.v1",
        "result": ("PASS" if all(run["passed"] for run in runs)
                   and performance["passed"] else "FAILED_STOPPED"),
        "performance": performance,
        "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("evidence is append-only")
    result = run_matrix()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"result": result["result"],
                      "runs": [(row["policy"], row["passed"]) for row in result["runs"]]}))


if __name__ == "__main__":
    main()
