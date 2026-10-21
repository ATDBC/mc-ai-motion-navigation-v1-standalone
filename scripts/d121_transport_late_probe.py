"""D121 Task 5: real transport sampling at L+1, L+2, or never.

This is a diagnostic component probe.  It changes only the calculator backend's
transport/application timing.  The navigation, prepared successor, Runtime,
ledger, receipt and physics paths are the same ones used by Task 4.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
import hashlib
import inspect
import json
import pickle
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc2p.backends.client_behavior_payload import encode_behavior_action
from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1
from mc2p.runtime.backend_v1 import PlayerBackendV1
from mc2p.motion_nav.world_model import CellFact
from scripts.d121_handoff_execution_probe import ExecutionCase
from tests.sim.backend import CalculatorBackend, Perturbations

POLICIES = ("timely_l1", "late_l2", "never_applied")
L = 26
EXPECTED_TICK = L + 1


class TransportDisposition(str, Enum):
    APPLIED_IN_WINDOW = "applied_in_window"
    APPLIED_OUTSIDE_WINDOW = "applied_outside_window"
    NOT_DELIVERED = "not_delivered"


class ExpiredApplicationPrevention(str, Enum):
    NOT_PREVENTED = "not_prevented"
    NOT_APPLICABLE = "not_applicable"

# Test comparison only. Production proof validation still checks every identity.
SEMANTIC_RUNTIME_FIELDS = frozenset({
    "async_identity", "job_token", "snapshot_lease_token", "ledger_id",
    "sealed_bytes", "canonical_bytes", "generation_id", "request_id", "local_revision",
})
SEMANTIC_VERSION_FIELDS = frozenset({
    "world_dependency_versions", "dependency_versions",
    "parent_dependency_versions", "inherited_dependencies",
})


def semantic_value(value):
    """Keep physical content, positions and timing; separate runtime bindings."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, CellFact):
        # Evidence stamps identify observations; knowledge and block geometry
        # are the facts consumed by physics, including UNKNOWN and known air.
        return {"knowledge": value.knowledge.value, "block": semantic_value(value.block)}
    if is_dataclass(value):
        result = {}
        for field in fields(value):
            name, item = field.name, getattr(value, field.name)
            if name in SEMANTIC_RUNTIME_FIELDS or name == "digest" or name.endswith("_digest"):
                continue
            result[name] = (
                [semantic_value(position) for position, _ in item]
                if name in SEMANTIC_VERSION_FIELDS else semantic_value(item)
            )
        return result
    if isinstance(value, (tuple, list)):
        return [semantic_value(item) for item in value]
    if isinstance(value, frozenset):
        return sorted((semantic_value(item) for item in value),
                      key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return {key: semantic_value(item) for key, item in value.items()}
    return value


def semantic_digest(value):
    content = json.dumps(semantic_value(value), sort_keys=True, ensure_ascii=True,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def semantic_controls(case, job):
    # This is the actual local worker result already admitted by the receiver,
    # not another solve or a fabricated READY. Decode only in test setup.
    bundle = pickle.loads(case.message.payload)
    components = {
        "route": case.route,
        "current_entry": case.proof.entry_state,
        "current_trajectory": case.proof.trajectory,
        "current_commands": (case.proof.commands, case.proof.tick_inputs),
        "current_proof_and_fallbacks": case.proof,
        "current_dependency_positions": case.proof.world_dependencies,
        "world_facts": job.snapshot.facts,
        "world_ruleset_and_bounds": (job.snapshot.identity.ruleset, job.snapshot.identity.bounds),
        "calculator_world": (case.backend.scene.volume, tuple(
            (position, case.backend.scene.geometry(block))
            for position, block in sorted(case.backend.scene.solids.items()))),
        "prepared_request": bundle.request,
        "prepared_successor_proof": bundle.proof,
        "prepared_successor_inputs": bundle.inputs,
    }
    binding = case.proof.successor_binding
    return {
        "component_sha256": {name: semantic_digest(value) for name, value in components.items()},
        "counts": {
            "route_actions": len(case.route.action_route.actions),
            "current_commands": len(case.proof.commands),
            "current_trajectory": len(case.proof.trajectory),
            "dependency_positions": len(case.proof.world_dependencies),
            "snapshot_facts": len(job.snapshot.facts),
            "prepared_inputs": len(bundle.inputs),
            "prepared_branches": len(bundle.proof.branches),
        },
        "runtime_identity": {
            "world_owner_snapshot_epoch": case.source._snapshot_epoch,
            "world_dependency_versions": case.proof.world_dependency_versions,
            "parent_main_digest": binding.parent_main_digest,
            "dependency_digest": binding.dependency_digest,
            "job_token": case.message.job_token,
            "payload_digest": case.message.payload_digest,
            "snapshot_digest": job.snapshot.identity.digest,
            "snapshot_local_revision": job.snapshot.identity.local_revision,
            "stream_generation_id": bundle.request.stream.generation_id,
            "stream_request_id": bundle.request.stream.request_id,
        },
    }


class TransportTimingBackend(CalculatorBackend):
    """Test-only FIFO transport timing; never fabricates receipt rejection."""

    def __init__(self, *args, transport_policy: str, **kwargs):
        if transport_policy not in POLICIES:
            raise ValueError("unknown transport policy")
        perturbations = kwargs.pop("perturbations", None)
        if perturbations is not None:
            raise ValueError("transport probe owns perturbations")
        # Delaying tick 27 queues the L successor for tick 28.  Delaying tick
        # 28 keeps the following Python frame behind it, matching a FIFO link:
        # the stale successor is the command sampled at L+2.
        late = frozenset({27, 28}) if transport_policy == "late_l2" else frozenset()
        super().__init__(*args, perturbations=Perturbations(late_ticks=late), **kwargs)
        self.transport_policy = transport_policy
        self.target_sequence: int | None = None
        self.target_action: ActionSnapshotV1 | None = None
        self.transport_events: list[dict[str, object]] = []
        self.sampling_events: list[dict[str, object]] = []

    def _sample_command(self, action):
        tick = self.movement_tick + 1
        before = {
            "movement_tick": tick,
            "received_action": None if action is None else asdict(action),
            "pending_before": [
                {"arrival_tick": arrival, "action": asdict(queued)}
                for arrival, queued in self._pending_actions
            ],
            "position_before": list(self.state.position),
            "velocity_before": list(self.state.velocity_blocks_per_tick),
            "on_ground_before": self.state.on_ground,
        }
        is_target = (
            action is not None and tick == EXPECTED_TICK
            and action.movement.jump and action.movement.forward == 1
        )
        if is_target:
            self.target_sequence = action.request_sequence_id
            self.target_action = action
            self.transport_events.append({
                "kind": "successor_submitted",
                "source_tick": L,
                "requested_effect_tick": EXPECTED_TICK,
                "sequence": action.request_sequence_id,
                "movement": asdict(action.movement),
            })
            if self.transport_policy == "never_applied":
                # The Runtime still used its sole real backend exit and the
                # backend received the real ActionSnapshot.  The transport
                # loses it before the client sampling point.  No receipt is
                # rewritten after the fact.
                self.submitted_commands.append((
                    action.request_sequence_id, action.movement, None,
                    action.deadline_monotonic_ns,
                ))
                self.transport_events.append({
                    "kind": "successor_lost_before_sampling",
                    "sequence": action.request_sequence_id,
                })
                action = None
        result = super()._sample_command(action)
        self.sampling_events.append({
            **before,
            "pending_after": [
                {"arrival_tick": arrival, "action": asdict(queued)}
                for arrival, queued in self._pending_actions
            ],
            "sample": dict(self._sample_records[-1]),
            "position_after": list(self.state.position),
            "velocity_after": list(self.state.velocity_blocks_per_tick),
            "on_ground_after": self.state.on_ground,
        })
        if self.target_sequence is not None:
            event = self.command_events[-1]
            if event["applied_request"] == self.target_sequence:
                self.transport_events.append({
                    "kind": "successor_physically_sampled",
                    "movement_tick": event["tick"],
                    "sequence": self.target_sequence,
                    "movement": asdict(self.applied_commands[-1][2]),
                })
        return result


def _factory(policy: str):
    def build(*args, **kwargs):
        return TransportTimingBackend(*args, transport_policy=policy, **kwargs)
    return build


def _failure_value(failure):
    if failure is None:
        return None
    return {
        "code": failure.code.value,
        "message": failure.message,
        "source": failure.source,
    }


def _ledger_value(record):
    if record is None:
        return None
    return {
        "status": record.status.value,
        "requested_first_tick": record.requested_first_tick,
        "latest_allowed_first_tick": record.latest_allowed_first_tick,
        "applied_ticks": list(record.applied_ticks),
        "resolved_at_tick": record.resolved_at_tick,
    }


def protocol_audit(action: ActionSnapshotV1 | None = None) -> dict[str, object]:
    snapshot_fields = [field.name for field in fields(ActionSnapshotV1)]
    intent_fields = [field.name for field in fields(ActionIntentV1)]
    result: dict[str, object] = {
        "action_intent_has_movement_tick_window": "movement_tick_window" in intent_fields,
        "action_snapshot_has_movement_tick_window": "movement_tick_window" in snapshot_fields,
        "action_snapshot_has_prepared_generation": any(
            "generation" in name for name in snapshot_fields
        ),
        "action_snapshot_fields": snapshot_fields,
        "backend_step_signature": str(inspect.signature(PlayerBackendV1.step)),
        "client_preapplication_exact_tick_guard": False,
        "client_sequence_guard_is_tick_window": False,
        "client_wall_deadline_is_tick_window": False,
        "client_cancel_is_self_expiry": False,
        "conclusion": (
            "the exact movement window remains Python-local; the current wire can "
            "report an outside-window application after sampling but cannot reject "
            "that application by exact movement tick before physics"
        ),
    }
    if action is not None:
        encoded = json.loads(encode_behavior_action(
            action,
            now_ns=max(0, action.deadline_monotonic_ns - 1),
            observation_received_at_ns=max(0, action.deadline_monotonic_ns - 1),
        ))
        result["wire_fields"] = sorted(encoded)
        result["wire_has_movement_tick_window"] = "movement_tick_window" in encoded
        result["wire_has_prepared_generation"] = any(
            "generation" in name for name in encoded
        )
        result["wire_cancel_request_sequence_id"] = encoded.get(
            "cancel_request_sequence_id"
        )
        result["wire_valid_for_ticks"] = encoded.get("valid_for_ticks")
    return result


def run_case(policy: str, *, direction_index: int = 0, max_frames: int = 60) -> dict[str, object]:
    if policy not in POLICIES:
        raise ValueError("unknown transport policy")
    with patch(
        "scripts.d121_handoff_execution_probe.CalculatorBackend",
        _factory(policy),
    ):
        case = ExecutionCase(direction_index=direction_index)
    rows: list[dict[str, object]] = []
    target_sequence = None
    target_window = None
    target_action = None
    terminal_failure = None
    try:
        job = case.prepare_successor()
        controls = semantic_controls(case, job)
        for _ in range(max_frames):
            source_tick = case.backend.movement_tick
            owner = (
                None if case.session._supervisor.route is None
                else case.session._supervisor.route.owner_id
            )
            try:
                result = case.tick()
            except Exception as error:
                terminal_failure = {
                    "code": type(error).__name__, "message": str(error),
                    "source": "probe_call",
                }
                break
            decision = result.decision
            receipt = None if result.backend_result is None else result.backend_result.receipt
            if source_tick == L:
                target_sequence = decision.action.request_sequence_id
                target_window = (
                    None if decision.movement_tick_window is None
                    else [decision.movement_tick_window.earliest_tick,
                          decision.movement_tick_window.latest_tick]
                )
                target_action = decision.action
            record = (
                None if target_sequence is None
                else case.runtime.input_ledger.record(target_sequence)
            )
            applications = [] if receipt is None else [
                {
                    "movement_tick": item.movement_tick_id,
                    "sequence": item.request_sequence_id,
                    "state": item.state,
                    "forward": item.forward,
                    "strafe": item.strafe,
                    "jump": item.jump,
                    "sneak": item.sneak,
                    "sprint": item.sprint,
                }
                for item in receipt.input_applications
            ]
            rows.append({
                "source_tick": source_tick,
                "effect_tick": case.backend.movement_tick,
                "decision_sequence": decision.action.request_sequence_id,
                "decision_window": (
                    None if decision.movement_tick_window is None
                    else [decision.movement_tick_window.earliest_tick,
                          decision.movement_tick_window.latest_tick]
                ),
                "decision_movement": asdict(decision.action.movement),
                "winner_count": sum(
                    group == "movement" for group, _ in decision.selected_intents
                ),
                "selected_intents": list(decision.selected_intents),
                "receipt_request_sequence": (
                    None if receipt is None else receipt.request_sequence_id
                ),
                "receipt_applications": applications,
                "target_ledger": _ledger_value(record),
                "position": list(case.backend.state.position),
                "velocity": list(case.backend.state.velocity_blocks_per_tick),
                "on_ground": case.backend.state.on_ground,
                "damage": case.backend.damage_taken,
                "owner": owner,
                "action_index": (
                    None if case.session._executor is None
                    else case.session._executor.action_index
                ),
                "driver_state": case.driver.state.value,
                "failure": _failure_value(result.report.failure),
            })
            if result.report.failure is not None:
                terminal_failure = _failure_value(result.report.failure)
                break
            if case.driver.state.value == "success":
                case.driver.release("d121-transport-probe-complete")
            if case.driver.source is None:
                break

        if target_sequence is None or target_action is None:
            raise AssertionError("formal chain never submitted the L successor")
        applications = [
            (tick, sequence, movement)
            for tick, sequence, movement in case.backend.applied_commands
            if sequence == target_sequence
        ]
        actual_tick = None if not applications else applications[0][0]
        actual_movement = None if not applications else asdict(applications[0][2])
        expired_jump = bool(
            applications and applications[0][0] > EXPECTED_TICK
            and applications[0][2].jump
        )
        final_record = case.runtime.input_ledger.record(target_sequence)
        disposition = (
            TransportDisposition.NOT_DELIVERED if actual_tick is None else
            TransportDisposition.APPLIED_IN_WINDOW if actual_tick == EXPECTED_TICK else
            TransportDisposition.APPLIED_OUTSIDE_WINDOW
        )
        prevention = (
            ExpiredApplicationPrevention.NOT_PREVENTED
            if disposition is TransportDisposition.APPLIED_OUTSIDE_WINDOW else
            ExpiredApplicationPrevention.NOT_APPLICABLE
        )
        if policy == "timely_l1":
            outcome = (
                "TIMELY_SUCCESS" if actual_tick == EXPECTED_TICK
                and case.backend.damage_taken == 0.0
                and terminal_failure is None and case.driver.state.value == "success"
                and case.driver.source is None else "TIMELY_CONTROL_FAILED"
            )
        elif policy == "late_l2":
            outcome = (
                "RED_EXPIRED_JUMP_PHYSICALLY_APPLIED"
                if expired_jump else "LATE_COMMAND_BLOCKED_BEFORE_PHYSICS"
            )
        else:
            outcome = (
                "LOST_SUCCESSOR_SAFELY_BOUNDED"
                if terminal_failure is None and case.driver.source is None
                and case.backend.state.on_ground and case.backend.damage_taken == 0.0
                else "RED_LOST_SUCCESSOR_BODY_UNRESOLVED"
            )
        return {
            "schema": "mc2p.d121-transport-late-case.v2",
            "policy": policy,
            "direction_index": direction_index,
            "L": L,
            "expected_effect_tick": EXPECTED_TICK,
            "target_sequence": target_sequence,
            "target_window": target_window,
            "target_action": asdict(target_action),
            "semantic_controls": controls,
            "L_movement_winner": next({
                "movement_type": type(target_action.movement).__qualname__,
                "sources": [source for group, source in row["selected_intents"]
                            if group == "movement"],
                "source_kinds": [source.split("/", 1)[0]
                                 for group, source in row["selected_intents"]
                                 if group == "movement"],
                "count": row["winner_count"],
            } for row in rows if row["source_tick"] == L),
            "actual_effect_tick": actual_tick,
            "actual_movement": actual_movement,
            "expired_jump_physically_applied": expired_jump,
            "transport_disposition": disposition.value,
            "expired_application_prevention": prevention.value,
            # Compatibility field: null means no expired-application rejection
            # was exercised. It is not evidence of a successful protocol guard.
            "protocol_prevented_expired_application": (
                False if prevention is ExpiredApplicationPrevention.NOT_PREVENTED else None
            ),
            "final_target_ledger": _ledger_value(final_record),
            "terminal_failure": terminal_failure,
            "final_body": {
                "position": list(case.backend.state.position),
                "velocity": list(case.backend.state.velocity_blocks_per_tick),
                "on_ground": case.backend.state.on_ground,
                "damage": case.backend.damage_taken,
            },
            "final_driver_state": case.driver.state.value,
            "source_released": case.driver.source is None,
            "transport_events": case.backend.transport_events,
            "backend_sampling_events": case.backend.sampling_events,
            "backend_submissions": [
                {"sequence": sequence, "movement": asdict(movement),
                 "arrival_tick": arrival, "deadline_monotonic_ns": deadline}
                for sequence, movement, arrival, deadline
                in case.backend.submitted_commands
            ],
            "protocol_audit": protocol_audit(target_action),
            "outcome": outcome,
            "frames": rows,
        }
    finally:
        case.close()


def run_matrix() -> dict[str, object]:
    runs = [run_case(policy) for policy in POLICIES]
    submitted = [
        (run["target_window"], run["target_action"])
        for run in runs
    ]
    same_submission = all(item == submitted[0] for item in submitted[1:])
    controls = [(run["semantic_controls"]["component_sha256"],
                 {key: value for key, value in run["L_movement_winner"].items() if key != "sources"})
                for run in runs]
    same_semantics = all(item == controls[0] for item in controls[1:])
    late = next(run for run in runs if run["policy"] == "late_l2")
    timely = next(run for run in runs if run["policy"] == "timely_l1")
    never = next(run for run in runs if run["policy"] == "never_applied")
    gate_passed = (
        same_submission and same_semantics and timely["outcome"] == "TIMELY_SUCCESS"
        and late["outcome"] == "LATE_COMMAND_BLOCKED_BEFORE_PHYSICS"
        and late["protocol_audit"]["client_preapplication_exact_tick_guard"]
        and late["terminal_failure"] is None and late["source_released"]
        and late["final_body"]["on_ground"] and late["final_body"]["damage"] == 0.0
        and never["outcome"] == "LOST_SUCCESSOR_SAFELY_BOUNDED"
    )
    return {
        "schema": "mc2p.d121-transport-late-matrix.v2",
        "result": "PASS" if gate_passed else "FAILED_STOPPED",
        "same_successor_action_and_window": same_submission,
        "same_control_semantics": same_semantics,
        "control_comparison_scope": (
            "route, entry/trajectory/commands, full proof/fallback content, dependency positions, "
            "world facts/geometry/rules, actual prepared request/proof/inputs and L winner; "
            "runtime identities, dependency versions, evidence stamps and derived digests remain separate"
        ),
        "stop_reason": (
            "current ActionSnapshot/wire has no exact movement-tick window; "
            "the L successor was physically sampled at L+2 before the Python "
            "ledger could classify it outside the [L+1,L+1] proof window"
            if late["expired_jump_physically_applied"] else
            None if gate_passed else "one or more transport safety gates remain unproved"
        ),
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
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(json.dumps({
        "result": result["result"],
        "runs": [
            {
                "policy": run["policy"], "outcome": run["outcome"],
                "expected": run["expected_effect_tick"],
                "actual": run["actual_effect_tick"],
            }
            for run in result["runs"]
        ],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
