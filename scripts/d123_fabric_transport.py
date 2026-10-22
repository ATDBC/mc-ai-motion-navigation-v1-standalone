"""D123-3 shared product trials, offline gate, and Fabric launcher."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.backends.client_behavior_payload import ACTION_SCHEMA_V2
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV4
from mc2p.contracts.action_v1 import MineBlockV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockGeometry
from scripts import d122_fabric_timing as d122
from scripts import d123_transport_preflight
from scripts import d129_invalid_harness
from scripts.control_probe_core import write_json_atomic
from scripts.d120_momentum_entry_calibration import build_fixture
from scripts.d128_repository_jdk import (
    bound_environment as repository_java_environment,
    resolve_repository_jdk as resolve_repository_java_tools,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


STATES = (
    "timely",
    "late_window_rejected",
    "remote_rejected_before_sampling",
)
_STATE_POLICIES = {
    "timely": "timely_l1",
    "late_window_rejected": "late_l2",
    "remote_rejected_before_sampling": "explicit_reject",
}
_DIRECTIONS = ("south", "east", "north", "west")
_RUN10_EVIDENCE = (
    ROOT / "evidence/motion_navigation/d123-proof-and-transport-v1/"
    "d128/smoke-run-10"
)


def repository_java_identity(java_tools) -> dict[str, object]:
    return java_tools.identity()


@dataclass(frozen=True, slots=True)
class D123FabricTrial:
    direction_index: int
    repetition: int
    state: str

    def __post_init__(self) -> None:
        if self.direction_index not in range(4):
            raise ValueError("D123 direction is invalid")
        if type(self.repetition) is not int or self.repetition < 0:
            raise ValueError("D123 repetition is invalid")
        if self.state not in STATES:
            raise ValueError("D123 transport state is invalid")

    @property
    def policy(self) -> str:
        return _STATE_POLICIES[self.state]

    @property
    def trial_id(self) -> str:
        return (
            f"d123-{_DIRECTIONS[self.direction_index]}-{self.repetition}-"
            f"{self.state}"
        )


def build_trial_plan(*, full_matrix: bool) -> tuple[D123FabricTrial, ...]:
    directions = range(4) if full_matrix else range(1)
    repetitions = range(3) if full_matrix else range(1)
    return tuple(
        D123FabricTrial(direction, repetition, state)
        for direction in directions
        for repetition in repetitions
        for state in STATES
    )


def _safe_body(value: Mapping) -> bool:
    velocity = value.get("velocity") if type(value) is dict else None
    return (
        type(value) is dict
        and value.get("on_ground") is True
        and type(velocity) in (list, tuple)
        and len(velocity) == 3
        and math.hypot(float(velocity[0]), float(velocity[2])) <= .01
    )


def _owner_gap(frames: Iterable[Mapping]) -> bool:
    terminal = {"success", "failed", "stopped", "cancelled"}
    return any(
        row.get("owner") is None and row.get("driver_state") not in terminal
        for row in frames
    )


def validate_trial_result(row: Mapping) -> tuple[str, ...]:
    """Validate the same product evidence for component and Fabric trials."""
    failures: list[str] = []
    state = row.get("state")
    landing = row.get("landing_tick")
    expected = landing + 1 if type(landing) is int else None
    window = row.get("successor_window")
    jumps = row.get("successor_jump_sample_ticks")
    defaults = row.get("default_sample_count")
    duration = row.get("default_duration_ticks")

    if row.get("action_schema") != ACTION_SCHEMA_V2:
        failures.append("action_schema_not_v2")
    if row.get("reset_succeeded") is not True:
        failures.append("reset_not_succeeded")
    if row.get("start_grounded_still") is not True:
        failures.append("start_not_grounded_still")
    if row.get("parent_default_installed_before_l") is not True:
        failures.append("parent_default_not_installed_before_l")
    if row.get("fallback_armed_tick") != landing:
        failures.append("fallback_not_armed_at_l")
    if (type(window) is not dict
            or window.get("earliest_tick") != expected
            or window.get("latest_tick") != expected):
        failures.append("successor_window_not_l_plus_1")
    if type(duration) is not int or duration <= 0:
        failures.append("default_duration_not_positive_int")
    if type(jumps) is not list:
        failures.append("successor_input_samples_missing")
    if row.get("owner_gap") is not False:
        failures.append("movement_owner_gap")
    if row.get("backend") == "calculator" and row.get("raw_case_passed") is not True:
        failures.append("raw_component_case_failed")
    if row.get("error") is not None:
        failures.append("unexpected_trial_error")
    if row.get("runtime_state") != "ready":
        failures.append("runtime_not_ready")
    if row.get("source_released") is not True:
        failures.append("source_not_released")
    if row.get("prepared_frame_clear") is not True:
        failures.append("prepared_frame_not_clear")
    if row.get("driver_terminal") is not True:
        failures.append("driver_not_terminal")
    if row.get("forced_frame_limit_release") is not False:
        failures.append("forced_frame_limit_release")
    if row.get("backend") == "fabric":
        close = row.get("resource_close")
        session_close = None if type(close) is not dict else close.get("session")
        worker_close = None if type(close) is not dict else close.get("worker")
        if (type(session_close) is not dict
                or any(session_close.get(field) != 0 for field in (
                    "live_workers", "live_receivers", "live_lease_returners",
                    "live_queue_feeders", "open_handles",
                    "unexpected_feeder_errors",
                ))
                or session_close.get("timed_out") is not False):
            failures.append("session_resources_not_closed")
        if (type(worker_close) is not dict
                or any(worker_close.get(field) != 0 for field in (
                    "live_workers", "live_queue_feeders",
                    "unexpected_feeder_errors",
                ))
                or worker_close.get("timed_out") is not False):
            failures.append("motion_worker_not_closed")

    health = row.get("health_evidence")
    if type(health) is not dict or type(health.get("decreased")) is not bool:
        failures.append("health_evidence_missing")
    elif health["decreased"]:
        failures.append("health_decreased")
    if not _safe_body(row.get("final_body", {})):
        failures.append("fallback_terminal_not_safe")

    if state == "timely":
        if row.get("runtime_failure") is not None:
            failures.append("timely_runtime_failure")
        if jumps != [expected] or defaults != 0:
            failures.append("timely_first_effect_differs")
        if row.get("driver_state") != "success" or row.get("goal_satisfied") is not True:
            failures.append("timely_not_success")
        if row.get("not_applied_proof") is not None:
            failures.append("timely_has_not_applied_proof")
    elif state in {"late_window_rejected", "remote_rejected_before_sampling"}:
        if jumps != []:
            failures.append("negative_has_successor_input")
        if defaults != duration:
            failures.append("default_duration_incomplete")
        exhausted = row.get("default_exhausted_event")
        final_generation = row.get("final_observation_sequence_id")
        successor_observation = row.get("successor_observation_sequence_id")
        final_tick = row.get("final_movement_tick_id")
        if (type(exhausted) is not dict
                or exhausted.get("status") != "default_exhausted"
                or exhausted.get("request_sequence_id")
                != row.get("parent_request_sequence_id")
                or type(final_generation) is not int
                or type(successor_observation) is not int
                or final_generation <= successor_observation
                or type(final_tick) is not int
                or final_tick < exhausted.get("movement_tick_id", final_tick + 1)):
            failures.append("post_default_observation_not_fresh")
        if (row.get("remote_receipt_request_sequence_id")
                != row.get("successor_request_sequence_id")
                or type(successor_observation) is not int
                or type(row.get("remote_receipt_generation_id")) is not int
                or row.get("remote_receipt_generation_id")
                != successor_observation + 1
                or final_generation < row.get("remote_receipt_generation_id")):
            failures.append("negative_receipt_observation_identity_differs")
        if row.get("driver_state") != "failed" or row.get("goal_satisfied") is not False:
            failures.append("negative_not_failed")
        if state == "late_window_rejected":
            if row.get("runtime_failure") is not None:
                failures.append("late_runtime_failure")
            event = row.get("window_missed_event")
            if (type(event) is not dict
                    or event.get("status") != "window_missed"
                    or event.get("request_sequence_id")
                    != row.get("successor_request_sequence_id")):
                failures.append("late_window_missed_identity_differs")
            if row.get("window_missed_tick") != expected + 1:
                failures.append("late_not_rejected_at_l_plus_2")
            if row.get("not_applied_proof") is not None:
                failures.append("late_has_remote_rejection_proof")
        else:
            if row.get("runtime_failure") != {
                    "code": "contract",
                    "message": "windowed_operation_unsupported",
                    "retryable": True,
                    "source": "client_behavior",
            }:
                failures.append("remote_rejection_runtime_failure_differs")
            proof = row.get("not_applied_proof")
            receipt = row.get("raw_successor_receipt")
            if type(proof) is not dict:
                failures.append("remote_rejection_proof_missing")
            else:
                if proof.get("kind") != "remote_rejected_before_sampling":
                    failures.append("remote_rejection_kind_differs")
                if proof.get("episode_id") != row.get("successor_episode_id"):
                    failures.append("remote_rejection_episode_differs")
                if proof.get("request_sequence_id") != row.get("successor_request_sequence_id"):
                    failures.append("remote_rejection_sequence_differs")
                if proof.get("generation_id") != row.get("successor_generation_id"):
                    failures.append("remote_rejection_generation_differs")
                if [proof.get("earliest_effect_tick"), proof.get("latest_effect_tick")] != [
                    window.get("earliest_tick") if type(window) is dict else None,
                    window.get("latest_tick") if type(window) is dict else None,
                ]:
                    failures.append("remote_rejection_window_differs")
                if proof.get("input_applications") != 0:
                    failures.append("remote_rejection_has_input_applications")
                if proof.get("reason") in {None, "window_missed"}:
                    failures.append("remote_rejection_reason_not_independent")
                if proof.get("reason") != "windowed_operation_unsupported":
                    failures.append("remote_rejection_reason_differs")
                if proof.get("channel_healthy") is not True:
                    failures.append("remote_rejection_channel_not_healthy")
            if row.get("remote_receipt_status") == "operation_rejected":
                failures.append("remote_rejection_not_full_request")
            if row.get("remote_receipt_status") != "rejected":
                failures.append("remote_rejection_receipt_differs")
            if row.get("remote_receipt_reason") != "windowed_operation_unsupported":
                failures.append("remote_receipt_reason_differs")
            if type(receipt) is not dict:
                failures.append("raw_successor_receipt_missing")
            else:
                try:
                    parsed_receipt = ClientBehaviorReceiptV4.from_mapping(receipt)
                except ContractViolation:
                    failures.append("raw_successor_receipt_invalid")
                else:
                    target_applications = [
                        item for item in parsed_receipt.input_applications
                        if item.episode_id == row.get("successor_episode_id")
                        and item.request_sequence_id
                        == row.get("successor_request_sequence_id")
                    ]
                    target_not_applied = [
                        item for item in parsed_receipt.timing_events
                        if item.status.value == "not_applied"
                        and item.episode_id == row.get("successor_episode_id")
                        and item.request_sequence_id
                        == row.get("successor_request_sequence_id")
                    ]
                    target_window_applied = [
                        item for item in parsed_receipt.timing_events
                        if item.status.value == "window_applied"
                        and item.episode_id == row.get("successor_episode_id")
                        and item.request_sequence_id
                        == row.get("successor_request_sequence_id")
                    ]
                    if (parsed_receipt.status != "rejected"
                            or parsed_receipt.reason
                            != "windowed_operation_unsupported"
                            or parsed_receipt.episode_id
                            != row.get("successor_episode_id")
                            or parsed_receipt.request_sequence_id
                            != row.get("successor_request_sequence_id")
                            or parsed_receipt.generation_id
                            != row.get("successor_generation_id")):
                        failures.append("raw_receipt_identity_differs")
                    if parsed_receipt.dropped_input_samples != 0:
                        failures.append("raw_receipt_dropped_inputs")
                    if target_applications:
                        failures.append("raw_receipt_has_target_application")
                    if len(target_not_applied) != 1:
                        failures.append("raw_receipt_not_applied_count_differs")
                    if target_window_applied:
                        failures.append("raw_receipt_has_window_applied")
                    if len(target_not_applied) == 1:
                        raw_tick = target_not_applied[0].movement_tick_id
                        if (type(proof) is not dict
                                or proof.get("causal_movement_tick") != raw_tick
                                or type(window) is not dict
                                or type(window.get("earliest_tick")) is not int
                                or type(window.get("latest_tick")) is not int
                                or not window["earliest_tick"] <= raw_tick <= window["latest_tick"]
                                or proof.get("input_applications")
                                != len(target_applications)):
                            failures.append("raw_receipt_causal_tick_differs")
            event = row.get("not_applied_event")
            if (type(event) is not dict
                    or event.get("status") != "not_applied"
                    or event.get("episode_id") != row.get("successor_episode_id")
                    or event.get("request_sequence_id")
                    != row.get("successor_request_sequence_id")):
                failures.append("not_applied_event_identity_differs")
            injections = [event for event in row.get("transport_events", ())
                          if event.get("kind")
                          == "remote_full_request_rejection_injected"]
            if row.get("backend") == "fabric":
                injection = injections[0] if len(injections) == 1 else None
                if (type(injection) is not dict
                        or injection.get("label")
                        != "d123_windowed_operation_denial"
                        or injection.get("expected_receipt_reason")
                        != "windowed_operation_unsupported"
                        or injection.get("changed_fields") != ["operation"]
                        or injection.get("action_identity")
                        != row.get("successor_expected_identity")
                        or type(injection.get("preserved_fields")) is not dict
                        or not all(injection["preserved_fields"].values())):
                    failures.append("remote_rejection_injection_identity_differs")
    else:
        failures.append("unknown_state")
    return tuple(failures)


def assemble_trial_result(trial: D123FabricTrial, *, backend: str,
                          reset_succeeded: bool, start_grounded_still: bool,
                          payload: Mapping) -> dict:
    row = {
        "schema_version": "mc2p.d123-shared-trial.v1",
        "backend": backend,
        "trial_id": trial.trial_id,
        "direction_index": trial.direction_index,
        "repetition": trial.repetition,
        "state": trial.state,
        "policy": trial.policy,
        "reset_succeeded": reset_succeeded,
        "start_grounded_still": start_grounded_still,
        **dict(payload),
    }
    failures = validate_trial_result(row)
    return {**row, "passed": not failures, "failures": list(failures)}


def _component_payload(trial: D123FabricTrial, raw: Mapping) -> dict:
    parent = raw.get("parent_action") or {}
    successor = raw.get("successor_action") or {}
    parent_sequence = parent.get("request_sequence_id")
    successor_sequence = successor.get("request_sequence_id")
    events = tuple(raw.get("timing_events", ()))
    armed = [event for event in events
             if event.get("status") == "fallback_armed"
             and event.get("request_sequence_id") == parent_sequence]
    missed = [event for event in events
              if event.get("status") == "window_missed"
              and event.get("request_sequence_id") == successor_sequence]
    not_applied = [event for event in events
                   if event.get("status") == "not_applied"
                   and event.get("request_sequence_id") == successor_sequence]
    exhausted = [event for event in events
                 if event.get("status") == "default_exhausted"
                 and event.get("request_sequence_id") == parent_sequence]
    facts = [fact for fact in raw.get("transport_facts", ())
             if fact.get("kind") == "remote_rejected_before_sampling"]
    proof = facts[0] if len(facts) == 1 else None
    defaults = list(raw.get("default_samples", ()))
    final_generation = raw.get("final_observation_sequence_id")
    response_generation = (
        successor.get("observation_sequence_id") + 1
        if type(successor.get("observation_sequence_id")) is int else None
    )
    remote_status = "rejected" if trial.state != "timely" else "executed"
    remote_reason = (
        "windowed_operation_unsupported" if proof is not None
        else "window_missed" if trial.state == "late_window_rejected"
        else "executed"
    )
    raw_receipt = {
        "schema_version": "mc2p.client_action_receipt.v4",
        "generation_id": response_generation,
        "execution_path": "client_behavior_v1",
        "episode_id": successor.get("episode_id"),
        "request_sequence_id": successor_sequence,
        "status": remote_status,
        "reason": remote_reason,
        "execution_thread": "component-simulator",
        "on_client_thread": True,
        "execution_phase": "client_tick_action_boundary",
        "world_tick": max(
            [0] + [event["movement_tick_id"] for event in events]
            + [sample["movement_tick_id"]
               for sample in raw.get("successor_samples", ())]
        ),
        "action_keyboard_callbacks": 0,
        "action_mouse_callbacks": 0,
        "handled_screen_render_attempts": 0,
        "handled_screen_render_completions": 0,
        "input_samples": max(
            [0] + [event["movement_tick_id"] for event in events]
            + [sample["movement_tick_id"]
               for sample in raw.get("successor_samples", ())]
        ),
        "leased_input_samples": len(raw.get("successor_samples", ())),
        "dropped_input_samples": 0,
        "oldest_retained_input_tick": 0,
        "input_applications": list(raw.get("successor_samples", ())),
        "timing_events": list(events),
    }
    return {
        "action_schema": ACTION_SCHEMA_V2,
        "landing_tick": raw.get("L"),
        "parent_default_installed_before_l": len(armed) == 1,
        "fallback_armed_tick": armed[0].get("movement_tick_id") if len(armed) == 1 else None,
        "parent_request_sequence_id": parent_sequence,
        "successor_episode_id": successor.get("episode_id"),
        "successor_request_sequence_id": successor_sequence,
        "successor_observation_sequence_id": successor.get("observation_sequence_id"),
        "successor_generation_id": response_generation,
        "successor_window": successor.get("first_effect_window"),
        "successor_jump_sample_ticks": [
            row["movement_tick_id"] for row in raw.get("successor_samples", ())
            if row.get("jump")
        ],
        "default_duration_ticks": (parent.get("safe_default") or {}).get("valid_for_ticks"),
        "default_sample_count": len(defaults),
        "window_missed_event": missed[0] if len(missed) == 1 else None,
        "window_missed_tick": missed[0].get("movement_tick_id") if len(missed) == 1 else None,
        "not_applied_event": not_applied[0] if len(not_applied) == 1 else None,
        "default_exhausted_event": exhausted[0] if len(exhausted) == 1 else None,
        "not_applied_proof": proof,
        "remote_receipt_status": (
            "rejected" if trial.state != "timely" else None
        ),
        "remote_receipt_reason": (
            "windowed_operation_unsupported" if proof is not None
            else "window_missed" if trial.state == "late_window_rejected"
            else None
        ),
        "remote_receipt_request_sequence_id": (
            None if trial.state == "timely" else successor_sequence
        ),
        "remote_receipt_generation_id": (
            None if trial.state == "timely" else response_generation
        ),
        "raw_successor_receipt": raw_receipt,
        "final_observation_sequence_id": final_generation,
        "final_movement_tick_id": raw.get("final_movement_tick_id"),
        "health_evidence": {
            "source": "calculator_post_default_state",
            "decreased": raw.get("final_body", {}).get("damage") != 0.0,
        },
        "final_body": raw.get("final_body"),
        "owner_gap": _owner_gap(raw.get("frames", ())),
        "raw_case_passed": raw.get("passed"),
        "source_released": raw.get("source_released"),
        "prepared_frame_clear": raw.get("prepared_frame") is False,
        "driver_terminal": raw.get("driver_state") in {
            "success", "failed", "stopped", "cancelled",
        },
        "runtime_state": raw.get("runtime_state"),
        "runtime_failure": raw.get("runtime_failure"),
        "driver_state": raw.get("driver_state"),
        "goal_satisfied": raw.get("goal_satisfied"),
        "error": raw.get("error"),
        "forced_frame_limit_release": False,
        "frames": list(raw.get("frames", ())),
        "transport_events": list(raw.get("transport_events", ())),
        "synchronous_timings": list(raw.get("synchronous_timings", ())),
    }


def component_trial(trial: D123FabricTrial) -> dict:
    from scripts.d122_formal_timing_probe import run_case
    raw = run_case(
        trial.policy,
        direction_index=trial.direction_index,
        explicit_reject_reason="windowed_operation_unsupported",
    )
    return assemble_trial_result(
        trial,
        backend="calculator",
        reset_succeeded=True,
        start_grounded_still=True,
        payload=_component_payload(trial, raw),
    )


def _performance(rows: Iterable[Mapping]) -> dict:
    timings = [timing for row in rows for timing in row.get("synchronous_timings", ())]
    submit = sorted(timing["submit_compute_ns"] / 1_000_000 for timing in timings)
    control = sorted(timing["control_compute_ns"] / 1_000_000 for timing in timings)

    def percentile(values, q):
        return values[min(len(values) - 1, int((len(values) - 1) * q))]

    result = {
        "sample_count": len(control),
        "submit_max_ms": max(submit),
        "control_p95_ms": percentile(control, .95),
        "control_p99_ms": percentile(control, .99),
        "control_max_ms": max(control),
        "response_wait_reported_separately": True,
    }
    result["passed"] = (
        result["submit_max_ms"] < 50.0
        and result["control_p95_ms"] <= 8.0
        and result["control_p99_ms"] <= 15.0
        and result["control_max_ms"] < 30.0
    )
    return result


@lru_cache(maxsize=2)
def component_preflight(*, full_matrix: bool) -> dict:
    rows = [component_trial(trial) for trial in build_trial_plan(full_matrix=full_matrix)]
    performance = _performance(rows)
    return {
        "schema_version": "mc2p.d123-component-product-preflight.v1",
        "mode": "matrix" if full_matrix else "smoke",
        "result": ("PASS" if all(row["passed"] for row in rows)
                   and performance["passed"] else "FAILED_STOPPED"),
        "performance": performance,
        "trials": rows,
    }


class D123FabricBehaviorBackend(d122.D122FabricBehaviorBackend):
    """Inject one deterministic remote full-request denial for D123 trials."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._d123_state = "timely"
        self.d123_injections: list[dict] = []

    def select_d122_policy(self, policy: str) -> None:
        state = next((state for state, mapped in _STATE_POLICIES.items()
                      if mapped == policy), None)
        if state is None:
            raise ValueError("D123 transport policy is invalid")
        self._d123_state = state
        super().select_d122_policy(
            "late_l2" if state == "late_window_rejected" else "timely_l1"
        )

    def step(self, action, deadline_monotonic_ns, *, observation_request=None):
        if (self._d123_state == "remote_rejected_before_sampling"
                and self._action_role(action) == "successor"):
            expected = self._d122_expected_successor
            if expected is None:
                raise RuntimeError("D123 successor injection has no prepared identity")
            injected = replace(
                action,
                operation=MineBlockV1(0, -64, 0, "up"),
            )
            preserved = {
                "episode_id": injected.episode_id == action.episode_id,
                "request_sequence_id": (
                    injected.request_sequence_id == action.request_sequence_id
                ),
                "observation_sequence_id": (
                    injected.observation_sequence_id == action.observation_sequence_id
                ),
                "first_effect_window": (
                    injected.first_effect_window == action.first_effect_window
                ),
                "movement": injected.movement == action.movement,
                "look": injected.look == action.look,
                "valid_for_ticks": injected.valid_for_ticks == action.valid_for_ticks,
                "safe_default": injected.safe_default == action.safe_default,
            }
            injection = {
                "kind": "remote_full_request_rejection_injected",
                "label": "d123_windowed_operation_denial",
                "episode_id": action.episode_id,
                "request_sequence_id": action.request_sequence_id,
                "observation_sequence_id": action.observation_sequence_id,
                "first_effect_window": asdict(action.first_effect_window),
                "prepared_generation_id": expected["prepared_generation_id"],
                "action_identity": self._action_identity(
                    action, prepared=expected,
                ),
                "expected_receipt_reason": "windowed_operation_unsupported",
                "injection_point": "before_remote_input_binding",
                "changed_fields": ["operation"],
                "preserved_fields": preserved,
            }
            result = super().step(
                injected, deadline_monotonic_ns,
                observation_request=observation_request,
            )
            injection["action_identity"] = dict(
                self.d122_steps[-1]["action_identity"],
            )
            self.d123_injections.append(injection)
            self.d122_transport_events.append(injection)
            return result
        return super().step(
            action, deadline_monotonic_ns,
            observation_request=observation_request,
        )


def _fabric_assembler(trial: D123FabricTrial, *, backend: str,
                      reset_succeeded: bool, start_grounded_still: bool,
                      payload: Mapping) -> dict:
    steps = tuple(payload.get("backend_steps", ()))
    identity = payload.get("successor_expected_identity")
    required_identity = {
        "proof_token", "prepared_generation_id", "owner_id", "ledger_id",
        "world_session_id", "movement_intent_id", "handoff_token",
        "episode_id", "request_sequence_id", "observation_sequence_id",
        "first_effect_window", "receipt_generation_id", "movement", "look",
        "valid_for_ticks", "safe_default",
    }
    if type(identity) is not dict or set(identity) != required_identity:
        raise ValueError("D123 Fabric successor identity is missing")
    candidates = [
        step for step in steps
        if step.get("action_role") == "successor"
        and step.get("action_identity") == identity
    ]
    request_matches = [
        step for step in steps
        if step.get("episode_id") == identity.get("episode_id")
        and step.get("request_sequence_id")
        == identity.get("request_sequence_id")
    ]
    if len(candidates) != 1 or request_matches != candidates:
        raise ValueError("D123 Fabric successor identity is not unique")
    successor = candidates[0]
    for field in (
        "episode_id", "request_sequence_id", "observation_sequence_id",
        "first_effect_window",
    ):
        if successor.get(field) != identity.get(field):
            raise ValueError("D123 Fabric successor fields differ from identity")
    receipt = None if successor is None else successor.get("receipt")
    if (type(receipt) is not dict
            or receipt.get("episode_id") != identity.get("episode_id")
            or receipt.get("request_sequence_id")
            != identity.get("request_sequence_id")
            or receipt.get("generation_id")
            != identity.get("receipt_generation_id")):
        raise ValueError("D123 Fabric successor receipt is bound to another request")
    injections = [
        event for event in payload.get("transport_events", ())
        if type(event) is dict
        and event.get("kind") == "remote_full_request_rejection_injected"
    ]
    if trial.state == "remote_rejected_before_sampling":
        if (len(injections) != 1
                or injections[0].get("action_identity") != identity):
            raise ValueError("D123 remote injection identity differs")
    elif injections:
        raise ValueError("D123 non-remote trial contains a remote injection")
    timing = tuple(() if receipt is None else receipt.get("timing_events", ()))
    successor_sequence = None if successor is None else successor.get("request_sequence_id")
    not_applied = [event for event in timing
                   if event.get("status") == "not_applied"
                   and event.get("request_sequence_id") == successor_sequence]
    parent_sequence = payload.get("parent_request_sequence_id")
    exhausted = [event for event in timing
                 if event.get("status") == "default_exhausted"
                 and event.get("request_sequence_id") == parent_sequence]
    facts = [frame.get("transport_fact") for frame in payload.get("frames", ())
             if type(frame.get("transport_fact")) is dict
             and frame["transport_fact"].get("kind")
             == "remote_rejected_before_sampling"]
    proof = facts[0] if len(facts) == 1 else None
    final_tick = payload.get("final_movement_tick_id")
    default_ticks = payload.get("default_sample_ticks", ())
    augmented = {
        **dict(payload),
        "state": trial.state,
        "successor_episode_id": (
            None if successor is None else successor.get("episode_id")
        ),
        "successor_observation_sequence_id": (
            None if successor is None else successor.get("observation_sequence_id")
        ),
        "successor_request_sequence_id": successor_sequence,
        "successor_expected_identity": identity,
        "successor_generation_id": None if receipt is None else receipt.get("generation_id"),
        "default_sample_count": len(default_ticks),
        "not_applied_event": not_applied[0] if len(not_applied) == 1 else None,
        "default_exhausted_event": exhausted[0] if len(exhausted) == 1 else None,
        "not_applied_proof": proof,
        "remote_receipt_status": None if receipt is None else receipt.get("status"),
        "remote_receipt_reason": None if receipt is None else receipt.get("reason"),
        "remote_receipt_request_sequence_id": (
            None if receipt is None else receipt.get("request_sequence_id")
        ),
        "remote_receipt_generation_id": (
            None if receipt is None else receipt.get("generation_id")
        ),
        "raw_successor_receipt": receipt,
        "owner_gap": _owner_gap(payload.get("frames", ())),
    }
    return assemble_trial_result(
        trial,
        backend=backend,
        reset_succeeded=reset_succeeded,
        start_grounded_still=start_grounded_still,
        payload=augmented,
    )


def run_fabric_runtime(runtime, backend: D123FabricBehaviorBackend, episode: str,
                       directory: Path, deadline_ns: int, fixture_writer,
                       player_teleporter, *, direction_index: int,
                       repetition: int):
    trials = tuple(
        D123FabricTrial(direction_index, repetition, state) for state in STATES
    )
    return d122.run_fabric_runtime(
        runtime, backend, episode, directory, deadline_ns,
        fixture_writer, player_teleporter,
        direction_index=direction_index,
        repetition=repetition,
        trials=trials,
        assembler=_fabric_assembler,
        stage_name="D123",
        summary_schema="mc2p.d123-fabric-runtime.v1",
        summary_filename="d123-runtime.json",
        check_prefix="d123_shared_trial_",
        expected_trial_count=3,
    )


def fabric_probe_command(*, server_port: int, ipc_port: int,
                         full_matrix: bool) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/probe_fabric_deployment_observation.py"),
        "--d123-transport-probe",
        "--time-diagnostics",
        "--physics-tick-diagnostics",
        "--ftick-diagnostics",
        "--server-port", str(server_port),
        "--ipc-port", str(ipc_port),
        "--timeout-seconds", "1200" if full_matrix else "600",
    ]
    if full_matrix:
        command.append("--d123-full-matrix")
    return command


def _known_map_preflight() -> list[dict]:
    rows = []
    for direction in range(4):
        fixture = build_fixture(direction)
        known = d122.known_fixture_map(fixture.trial, origin=(30, 40), feet_y=64)
        manifest = d122.fabric_dependency_manifest(
            direction, origin=(30, 40), feet_y=64,
        )
        adapter = NavigationObservationAdapter()
        adapter.ingest(valid_snapshot_v3(sequence=direction + 1))
        runtime = SimpleNamespace(navigation_observation_adapter=adapter)
        frame, report = d122.seed_runtime_known_fixture_map(runtime, known)
        gate = d122.inspect_fabric_dependency_knowledge(frame.world, manifest)
        rows.append({
            "direction_index": direction,
            "required_count": gate["required_count"],
            "known_count": gate["known_count"],
            "unknown_count": len(gate["unknown"]),
            "mismatch_count": len(gate["mismatched"]),
            "same_runtime_owner": (
                frame.world._owner is adapter.snapshot_world_port
            ),
            "source": report["source"],
            "rows_sha256": report["rows_sha256"],
            "facts_sha256": report["facts_sha256"],
        })
    return rows


def _known_map_mutations() -> dict:
    fixture = build_fixture(0)
    known = d122.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
    manifest = d122.fabric_dependency_manifest(0, origin=(0, 0), feet_y=64)
    position = next(
        position for position, expected in manifest["facts"].items()
        if expected["knowledge"] == "air"
    )

    def gate(value):
        adapter = NavigationObservationAdapter()
        adapter.ingest(valid_snapshot_v3(sequence=20))
        runtime = SimpleNamespace(navigation_observation_adapter=adapter)
        frame, _ = d122.seed_runtime_known_fixture_map(runtime, value)
        return d122.inspect_fabric_dependency_knowledge(frame.world, manifest)

    missing = gate(d122.without_known_fixture_position(known, position))
    mismatch = gate(d122.with_known_fixture_block(
        known, position, BlockGeometry.full_cube("minecraft:stone"),
    ))
    passed = missing["unknown"] == (position,) and len(mismatch["mismatched"]) == 1
    return {
        "status": "PASS" if passed else "FAIL",
        "missing_position": list(position),
        "missing_unknown_count": len(missing["unknown"]),
        "wrong_material_mismatch_count": len(mismatch["mismatched"]),
    }


def _environment_preflight(java_tools) -> dict:
    from scripts.build_fabric_deployment_probe import select_probe_gradle_home
    from scripts.visibility_fixture_world import _no_links

    java = java_tools.java
    javac = java_tools.javac
    gradle_home = select_probe_gradle_home(repository_root=ROOT)
    _no_links(gradle_home)
    completed = subprocess.run(
        [str(java), "-version"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    version = completed.stderr + completed.stdout
    passed = (
        completed.returncode == 0 and java.is_file() and javac.is_file()
        and "21" in version and (gradle_home / "caches").is_dir()
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "jdk_major": 21,
        "java": str(java.resolve()),
        "javac": str(javac.resolve()),
        "java_sha256": hashlib.sha256(java.read_bytes()).hexdigest(),
        "javac_sha256": hashlib.sha256(javac.read_bytes()).hexdigest(),
        "gradle_version": "8.8",
        "gradle_home": str(gradle_home.resolve()),
        "real_cache": (gradle_home / "caches").is_dir(),
        "reparse_point_free": True,
    }


def _lazy_import_preflight() -> dict:
    resolved = d122.resolve_fabric_runner_dependencies()
    required = {"RuntimeNavigationDriverState", "MotionSolverWorker", "d118_runtime"}
    passed = required <= set(resolved)
    return {
        "status": "PASS" if passed else "FAIL",
        "resolved_count": len(resolved),
        "required": sorted(required),
    }


def _standalone_preflight() -> dict:
    from scripts.export_motion_navigation_standalone import (
        collect_export_files, load_manifest,
    )
    manifest = load_manifest()
    files = collect_export_files(manifest)
    required = {
        "scripts/d122_fabric_timing.py",
        "scripts/d123_fabric_transport.py",
        "scripts/d123_transport_preflight.py",
        "scripts/d128_repository_jdk.py",
        "scripts/d129_invalid_harness.py",
        "scripts/probe_fabric_deployment_observation.py",
    }
    selected = {path.as_posix() for path in files}
    passed = required <= selected
    return {
        "status": "PASS" if passed else "FAIL",
        "manifest": manifest.path.relative_to(ROOT).as_posix(),
        "selected_file_count": len(selected),
        "required": sorted(required),
        "missing": sorted(required - selected),
    }


def _java_overlay_gate(java_tools, java_environment: Mapping[str, str]) -> dict:
    from scripts.build_fabric_deployment_probe import select_probe_gradle_home

    command = [
        sys.executable, "-m", "unittest",
        "tests.test_standalone_java_gates",
        "tests.test_d122_java_permission_bridge",
        "tests.test_d122_java_timing",
        "-v",
    ]
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
        env=dict(java_environment),
    )
    gradle_home = select_probe_gradle_home(repository_root=ROOT)
    gradle, = (gradle_home / "wrapper/dists/gradle-8.8-bin").glob(
        "*/gradle-8.8/bin/gradle.bat"
    )
    overlay_command = [
        str(gradle), "--offline", "--no-daemon", "--console=plain",
        "compileJava", "processResources",
    ]
    overlay = subprocess.run(
        overlay_command,
        cwd=ROOT / "deployment/fabric-observation-probe",
        env=dict(java_environment, GRADLE_USER_HOME=str(gradle_home)),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    passed = completed.returncode == 0 and overlay.returncode == 0
    return {
        "status": "PASS" if passed else "FAIL",
        "command": command,
        "return_code": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "overlay_command": overlay_command,
        "overlay_return_code": overlay.returncode,
        "overlay_stdout_sha256": hashlib.sha256(overlay.stdout.encode()).hexdigest(),
        "overlay_stderr_sha256": hashlib.sha256(overlay.stderr.encode()).hexdigest(),
    }


def offline_preflight(*, run_java: bool = True, java_tools=None,
                      java_environment: Mapping[str, str] | None = None) -> dict:
    """Run every gate that must pass before a Minecraft process may start."""
    tools = (resolve_repository_java_tools(repository_root=ROOT)
             if java_tools is None else java_tools)
    bound_environment = (repository_java_environment(tools)
                         if java_environment is None else dict(java_environment))
    environment = _environment_preflight(tools)
    lazy_imports = _lazy_import_preflight()
    standalone = _standalone_preflight()
    transport = d123_transport_preflight.generate()
    known_map = _known_map_preflight()
    known_map_mutations = _known_map_mutations()
    smoke = component_preflight(full_matrix=False)
    matrix = component_preflight(full_matrix=True)
    java = (_java_overlay_gate(tools, bound_environment)
            if run_java else {"status": "NOT_RUN_IN_UNIT_TEST"})
    semantics = d122._production_semantic_binding()
    invalid_harness = d129_invalid_harness.validate_invalid_harness_run(
        _RUN10_EVIDENCE,
    )
    passed = (
        d123_transport_preflight.validate(transport)
        and environment["status"] == "PASS"
        and lazy_imports["status"] == "PASS"
        and standalone["status"] == "PASS"
        and all(row["required_count"] == 58
                and row["known_count"] == 58
                and row["unknown_count"] == 0
                and row["mismatch_count"] == 0
                and row["same_runtime_owner"]
                and row["source"] == "test_oracle_fixture_map"
                for row in known_map)
        and known_map_mutations["status"] == "PASS"
        and smoke["result"] == "PASS"
        and matrix["result"] == "PASS"
        and invalid_harness["eligible_for_invalid_harness"] is True
        and (not run_java or java["status"] == "PASS")
    )
    return {
        "schema": "mc2p.d123-fabric-offline-preflight.v1",
        "status": "PASS" if passed else "FAILED_STOPPED",
        "fabric_started": False,
        "java_tool_identity": repository_java_identity(tools),
        "environment": environment,
        "lazy_imports": lazy_imports,
        "standalone": standalone,
        "production_semantics": semantics,
        "production_raw_hashes": current_production_raw_hashes(),
        "harness_hashes": current_harness_hashes(),
        "transport_preflight": {"passed": len(transport.chains), "total": 3},
        "known_map": known_map,
        "known_map_mutations": known_map_mutations,
        "d129_invalid_harness": invalid_harness,
        "component_smoke": {
            "passed": sum(row["passed"] for row in smoke["trials"]), "total": 3,
        },
        "component_matrix": {
            "passed": sum(row["passed"] for row in matrix["trials"]), "total": 36,
        },
        "component_performance": matrix["performance"],
        "java_overlay": java,
        "release_entry": {
            "script": "scripts/d123_fabric_transport.py",
            "launcher": "scripts/probe_fabric_deployment_observation.py",
            "smoke_flag": "--d123-transport-probe",
            "matrix_flag": "--d123-full-matrix",
            "separate_runs": True,
        },
    }


def validate_offline_preflight(payload: object, *, require_java: bool,
                               expected_java_identity: Mapping | None = None) -> bool:
    java_identity = (
        repository_java_identity(resolve_repository_java_tools(repository_root=ROOT))
        if expected_java_identity is None else dict(expected_java_identity)
    )
    if (type(payload) is not dict
            or payload.get("schema") != "mc2p.d123-fabric-offline-preflight.v1"
            or payload.get("status") != "PASS"
            or payload.get("fabric_started") is not False
            or payload.get("java_tool_identity")
            != java_identity
            or payload.get("production_semantics")
            != d122._production_semantic_binding()
            or payload.get("production_raw_hashes")
            != current_production_raw_hashes()
            or payload.get("harness_hashes") != current_harness_hashes()
            or payload.get("transport_preflight") != {"passed": 3, "total": 3}
            or payload.get("component_smoke") != {"passed": 3, "total": 3}
            or payload.get("component_matrix") != {"passed": 36, "total": 36}
            or payload.get("environment", {}).get("status") != "PASS"
            or payload.get("lazy_imports", {}).get("status") != "PASS"
            or payload.get("standalone", {}).get("status") != "PASS"
            or payload.get("known_map_mutations", {}).get("status") != "PASS"):
        return False
    if payload.get("d129_invalid_harness") != (
        d129_invalid_harness.validate_invalid_harness_run(_RUN10_EVIDENCE)
    ):
        return False
    if require_java and payload.get("java_overlay", {}).get("status") != "PASS":
        return False
    known = payload.get("known_map")
    if type(known) is not list or len(known) != 4:
        return False
    return all(
        row.get("direction_index") == direction
        and row.get("required_count") == 58
        and row.get("known_count") == 58
        and row.get("unknown_count") == 0
        and row.get("mismatch_count") == 0
        and row.get("same_runtime_owner") is True
        and row.get("source") == "test_oracle_fixture_map"
        for direction, row in enumerate(known)
    )


def preflight_reference(path: Path) -> dict:
    payload = json.loads(path.read_text("utf-8"))
    if not validate_offline_preflight(payload, require_java=True):
        raise ValueError("D123 Fabric offline preflight is not a current full PASS")
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "status": "PASS",
        "production_semantics": payload["production_semantics"],
    }


def current_harness_hashes() -> dict[str, str]:
    from scripts.probe_fabric_deployment_observation import frozen_deployment_sources
    return frozen_deployment_sources(
        b03_fixed_route_probe=False, d123_transport_probe=True,
    )


def current_harness_semantic_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256(
            (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for name in current_harness_hashes()
    }


def current_production_raw_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in d122._production_semantic_binding()["files"]
    }


def current_run_source_hashes() -> dict[str, str]:
    from scripts.probe_fabric_deployment_observation import frozen_deployment_sources
    result = frozen_deployment_sources(
        b03_fixed_route_probe=False, d123_transport_probe=True,
    )
    result.update(current_production_raw_hashes())
    return result


def validate_run_directory(run_dir: Path, *, mode: str) -> dict:
    if mode not in {"smoke", "matrix"}:
        raise ValueError("D123 run mode is invalid")
    result_path = Path(run_dir) / "result.json"
    payload = json.loads(result_path.read_text("utf-8"))
    expected_sessions = 1 if mode == "smoke" else 12
    expected_trials = 3 if mode == "smoke" else 36
    sessions = payload.get("sessions")
    if (payload.get("status") != "passed"
            or payload.get("scenario") != "d123-transport"
            or type(sessions) is not list
            or len(sessions) != expected_sessions):
        raise ValueError("D123 run result mode/status/session count differs")
    harness = current_harness_hashes()
    production = current_production_raw_hashes()
    expected_sources = current_run_source_hashes()
    before = payload.get("core_sources_before")
    after = payload.get("core_sources_after")
    if (type(before) is not dict or before != after
            or any(before.get(name) != digest
                   for name, digest in expected_sources.items())):
        raise ValueError("D123 run source hashes differ")
    trials = []
    expected_plan = build_trial_plan(full_matrix=mode == "matrix")
    expected_groups = [expected_plan[offset:offset + 3]
                       for offset in range(0, len(expected_plan), 3)]
    for session, expected_group in zip(sessions, expected_groups):
        summary = session.get("d123_transport") if type(session) is dict else None
        rows = summary.get("trials") if type(summary) is dict else None
        if (summary is None or summary.get("status") != "passed"
                or summary.get("trial_count") != 3
                or summary.get("direction_index")
                != expected_group[0].direction_index
                or summary.get("repetition") != expected_group[0].repetition
                or type(rows) is not list or len(rows) != 3):
            raise ValueError("D123 session trial summary differs")
        for row, expected in zip(rows, expected_group):
            if row.get("backend") != "fabric":
                raise ValueError("D123 formal result backend is not Fabric")
            identity = (
                row.get("trial_id"), row.get("policy"), row.get("state"),
                row.get("direction_index"), row.get("repetition"),
            )
            expected_identity = (
                expected.trial_id, expected.policy, expected.state,
                expected.direction_index, expected.repetition,
            )
            if identity != expected_identity:
                raise ValueError("D123 trial identity differs from frozen plan")
            dependency = row.get("dependency_precheck")
            known_map = row.get("known_map")
            if (type(dependency) is not dict
                    or dependency.get("required_count") != 58
                    or dependency.get("known_count") != 58
                    or dependency.get("unknown") not in ([], ())
                    or dependency.get("mismatched") not in ([], ())
                    or type(known_map) is not dict
                    or known_map.get("source") != "test_oracle_fixture_map"
                    or known_map.get("same_runtime_owner") is not True):
                raise ValueError("D123 trial known-map owner gate differs")
            if row.get("passed") is not True or validate_trial_result(row):
                raise ValueError("D123 trial did not pass the current validator")
        trials.extend(rows)
    if len(trials) != expected_trials:
        raise ValueError("D123 run trial count differs")
    return {
        "status": "PASS",
        "mode": mode,
        "session_count": len(sessions),
        "trial_count": len(trials),
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "harness_hashes": harness,
        "production_raw_hashes": production,
    }


def _run_directory(stdout: str) -> Path | None:
    prefix = "FABRIC_DEPLOYMENT_RUN_DIR="
    values = [line[len(prefix):].strip() for line in stdout.splitlines()
              if line.startswith(prefix)]
    return Path(values[-1]) if values else None


def smoke_reference(path: Path, *, expected_java_identity: Mapping | None = None) -> dict:
    payload = json.loads(path.read_text("utf-8"))
    validation = payload.get("run_validation")
    preflight = payload.get("preflight")
    source_run = payload.get("source_run")
    if (payload.get("schema") != "mc2p.d123-fabric-launch.v1"
            or payload.get("status") != "captured"
            or payload.get("mode") != "smoke"
            or type(validation) is not dict
            or validation.get("status") != "PASS"
            or validation.get("session_count") != 1
            or validation.get("trial_count") != 3
            or validation.get("harness_hashes") != current_harness_hashes()
            or validation.get("production_raw_hashes")
            != current_production_raw_hashes()
            or not validate_offline_preflight(
                preflight, require_java=True,
                expected_java_identity=expected_java_identity,
            )
            or type(source_run) is not str):
        raise ValueError("D123 matrix requires one current smoke 3/3 PASS")
    result_path = Path(source_run) / "result.json"
    if (not result_path.is_file()
            or hashlib.sha256(result_path.read_bytes()).hexdigest()
            != validation.get("result_sha256")):
        raise ValueError("D123 smoke source result is unavailable or changed")
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_run": str(Path(source_run).resolve()),
        "result_sha256": validation["result_sha256"],
        "harness_hashes": validation["harness_hashes"],
        "production_raw_hashes": validation["production_raw_hashes"],
        "java_tool_identity": preflight["java_tool_identity"],
        "trial_count": 3,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-preflight", type=Path)
    parser.add_argument("--launch", choices=("smoke", "matrix"))
    parser.add_argument("--smoke-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--server-port", type=int, default=25597)
    parser.add_argument("--ipc-port", type=int, default=8140)
    args = parser.parse_args(argv)
    java_tools = resolve_repository_java_tools(repository_root=ROOT)
    java_environment = repository_java_environment(java_tools)
    if args.offline_preflight is not None:
        if args.launch is not None or args.output is not None:
            parser.error("offline preflight cannot launch Fabric")
        if args.offline_preflight.exists():
            raise FileExistsError("D123 preflight evidence is append-only")
        result = offline_preflight(
            java_tools=java_tools, java_environment=java_environment,
        )
        args.offline_preflight.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(args.offline_preflight, result)
        print(json.dumps({
            "status": result["status"],
            "smoke": result["component_smoke"],
            "matrix": result["component_matrix"],
        }))
        return 0 if result["status"] == "PASS" else 2
    if args.launch is None or args.output is None:
        parser.error("Fabric launch requires --launch and --output")
    if args.output.exists():
        raise FileExistsError("D123 Fabric evidence is append-only")
    args.output.mkdir(parents=True, exist_ok=False)
    preflight = offline_preflight(
        java_tools=java_tools, java_environment=java_environment,
    )
    write_json_atomic(args.output / "offline-preflight.json", preflight)
    java_identity = repository_java_identity(java_tools)
    if not validate_offline_preflight(
            preflight, require_java=True,
            expected_java_identity=java_identity):
        summary = {
            "schema": "mc2p.d123-fabric-launch.v1",
            "status": "failed",
            "mode": args.launch,
            "command": None,
            "return_code": None,
            "source_run": None,
            "preflight": preflight,
            "smoke": None,
            "run_validation": None,
            "validation_error": {
                "type": "OfflinePreflightFailure",
                "message": "same-process D123 offline preflight did not pass",
            },
        }
        write_json_atomic(args.output / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 2
    full_matrix = args.launch == "matrix"
    smoke = None
    if full_matrix:
        if args.smoke_manifest is None:
            parser.error("matrix launch requires --smoke-manifest")
        smoke = smoke_reference(
            args.smoke_manifest, expected_java_identity=java_identity,
        )
        if (smoke["harness_hashes"] != preflight["harness_hashes"]
                or smoke["production_raw_hashes"]
                != preflight["production_raw_hashes"]
                or smoke["java_tool_identity"]
                != preflight["java_tool_identity"]):
            raise ValueError(
                "D123 smoke and matrix preflight bytes differ"
            )
    elif args.smoke_manifest is not None:
        parser.error("smoke launch does not accept --smoke-manifest")
    command = fabric_probe_command(
        server_port=args.server_port, ipc_port=args.ipc_port,
        full_matrix=full_matrix,
    )
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=java_environment,
    )
    stdout, stderr, return_code = (
        completed.stdout, completed.stderr, completed.returncode,
    )
    run_dir = _run_directory(stdout)
    validation = None
    validation_error = None
    if return_code == 0 and run_dir is not None:
        try:
            validation = validate_run_directory(run_dir, mode=args.launch)
            if (validation["harness_hashes"] != preflight["harness_hashes"]
                    or validation["production_raw_hashes"]
                    != preflight["production_raw_hashes"]):
                raise ValueError(
                    "D123 run bytes differ from same-process preflight"
                )
            if full_matrix and (
                    validation["harness_hashes"] != smoke["harness_hashes"]
                    or validation["production_raw_hashes"]
                    != smoke["production_raw_hashes"]):
                raise ValueError("D123 matrix bytes differ from smoke")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            validation_error = {"type": type(error).__name__, "message": str(error)}
    summary = {
        "schema": "mc2p.d123-fabric-launch.v1",
        "status": "captured" if validation is not None else "failed",
        "mode": args.launch,
        "command": command,
        "return_code": return_code,
        "source_run": None if run_dir is None else str(run_dir.resolve()),
        "preflight": preflight,
        "smoke": smoke,
        "run_validation": validation,
        "validation_error": validation_error,
    }
    write_json_atomic(args.output / "summary.json", summary)
    (args.output / "probe-stdout.txt").write_text(stdout, encoding="utf-8", newline="\n")
    (args.output / "probe-stderr.txt").write_text(stderr, encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
