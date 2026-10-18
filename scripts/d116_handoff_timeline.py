"""Read-only D116 attribution for the first formal two-gap handoff.

The production tree deliberately does not auto-prepare a strict successor yet.
This diagnostic restores only D115's withdrawn coordinator call with a method
patch, then records the unchanged Session/Supervisor/Executor ordering.  It
does not alter production state-machine code or claim that two-jump continuity
is available.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.prepared_motion import (
    PreparedQueryStatus, SnapshotBorrowResult, SnapshotBorrowStatus,
)
from mc2p.motion_nav.world_model import WorldKnowledge
from tests.sim.backend import Scene
from tests.sim.runner import InlineMotionWorker, Scenario, run


def _movement(value) -> dict[str, object]:
    return {
        "forward": value.forward,
        "strafe": value.strafe,
        "jump": value.jump,
        "sneak": value.sneak,
        "sprint": value.sprint,
    }


def _fact_payload(fact) -> tuple[object, ...]:
    visual = fact.visual_air_evidence
    return (
        fact.knowledge,
        fact.block,
        None if visual is None else (
            visual.observer_distance_blocks,
            visual.lower_region_visible,
        ),
    )


def _activity_summary(activity) -> dict[str, object] | None:
    if activity is None:
        return None
    return {
        "action_index": activity["action_index"],
        "observation_sequence_id": activity["observation_sequence_id"],
        "phase": activity["phase"],
    }


def _representative(*, max_ticks: int = 25) -> Scenario:
    solids = {
        (x, 63, z): "minecraft:grass_block"
        for x in range(-1, 2)
        for z in range(8)
        if z not in {2, 4}
    }
    return Scenario(
        "d116-formal-two-jump-attribution",
        Scene(solids, ((-2, 2), (60, 70), (-2, 9))),
        (.5, 64.0, .5),
        (.5, 64.0, 5.5),
        max_ticks=max_ticks,
    )


def _async_identity_parts(identity) -> tuple[object, ...]:
    """Retain every identity field while accepting the runner's dict view."""
    if isinstance(identity, dict):
        scope = identity["scope"]
        return (
            scope["world_session_id"], scope["task_id"], scope["generation"],
            identity["owner_instance_id"], identity["work_kind"],
            identity["subject_id"], identity["revision"],
        )
    return (
        identity.scope.world_session_id, identity.scope.task_id,
        identity.scope.generation, identity.owner_instance_id,
        identity.work_kind.value, identity.subject_id, identity.revision,
    )


def _prepared_lifecycle_summary(async_records, prepared_identity_token):
    records = [
        (tick, event) for tick, event in async_records
        if event.get("identity_token") == prepared_identity_token
    ]
    admission = next((item for item in records
        if item[1].get("record_type") == "admission"
        and item[1].get("disposition") == "applied"), None)
    events = [item for item in records
              if item[1].get("record_type") == "event"]
    apply_event = next((item for item in events
                        if item[1].get("operation") == "apply"), None)
    finish_event = next((item for item in events
                         if item[1].get("operation") == "finish"), None)
    return {
        "identity_token": prepared_identity_token,
        "operations": [event["operation"] for _tick, event in events],
        "admission_tick": None if admission is None else admission[0],
        "admission_accepted_monotonic_ns": (
            None if admission is None else admission[1]["accepted_monotonic_ns"]
        ),
        "apply_tick": None if apply_event is None else apply_event[0],
        "apply_monotonic_ns": (
            None if apply_event is None else apply_event[1]["monotonic_ns"]
        ),
        "finish_tick": None if finish_event is None else finish_event[0],
    }


class _Timeline:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, object]] = {}
        self.planned_actions: list[str] = []
        self.prepared_identity: tuple[object, ...] | None = None
        self._identity_tokens: dict[tuple[object, ...], str] = {}

    def identity_token(self, identity: tuple[object, ...]) -> str:
        token = self._identity_tokens.get(identity)
        if token is None:
            token = f"async-work-{len(self._identity_tokens) + 1}"
            self._identity_tokens[identity] = token
        return token

    def row(self, tick: int) -> dict[str, object]:
        row = self.rows.setdefault(tick, {"movement_tick": tick, "event_order": []})
        return row

    def event(self, tick: int, name: str, **values: object) -> None:
        row = self.row(tick)
        row["event_order"].append(name)
        row.update(values)


def run_representative_timeline(*, dependency_gate: str = "semantic",
                                max_ticks: int = 25,
                                snapshot_borrow_mode: str = "normal",
                                force_prepared_entry_mismatch: bool = False) -> dict[str, object]:
    """Run the formal chain with a current or explicitly replayed dependency gate."""
    if dependency_gate not in {"semantic", "legacy_full_cell_fact"}:
        raise ValueError("dependency_gate must be semantic or legacy_full_cell_fact")
    busy_cycles = {
        "normal": 0,
        "busy_1": 1,
        "busy_3": 3,
        "busy_5": 5,
        "always_busy": None,
        "prepared_always_busy": -1,
    }
    if snapshot_borrow_mode not in busy_cycles:
        raise ValueError(
            "snapshot_borrow_mode must be normal, busy_1, busy_3, busy_5, "
            "always_busy or prepared_always_busy"
        )
    if type(force_prepared_entry_mismatch) is not bool:
        raise ValueError("force_prepared_entry_mismatch must be boolean")
    timeline = _Timeline()
    original_decide = MotionRouteCoordinator.decide
    original_advance = ActionRouteExecutor._advance
    original_stage = ActionRouteExecutor.stage_prepared_action
    original_approve = ExecutionSupervisor.approve_action_handoff
    original_commit_activation = ExecutionSupervisor.commit_activation
    original_validate_routes = ExecutionSupervisor._validate_routes
    original_proposal = NavigationSession._proposal
    original_prepared_decision = VerifiedMotionExecutor.prepared_decision
    original_verified_decide = VerifiedMotionExecutor.decide
    original_record_application = VerifiedMotionExecutor.record_application
    original_record_missing = VerifiedMotionExecutor.record_missing_application
    original_entry_check = ActionRouteExecutor.current_verified_motion_can_start

    def coordinator_decide(self, frame, anchor, ledger, world, **kwargs):
        if not timeline.planned_actions:
            timeline.planned_actions = [
                type(action).__name__ for action in self.route.action_route.actions
            ]
        controller_before = self.executor._controller
        timeline.event(
            anchor.movement_tick_id,
            "coordinator_decide_enter",
            controller_state_at_coordinator_enter=(
                None if controller_before is None
                else str(getattr(controller_before, "state", None))
            ),
            coordinator_scope_matches=(
                kwargs.get("current_scope") == self.computation_scope
            ),
            coordinator_worker_alive=self.worker.is_alive(),
            executor_cancel_requested=self.executor._cancel_requested,
            executor_stop_cause=(
                None if self.executor._stop_cause is None
                else self.executor._stop_cause.value
            ),
        )
        decision = original_decide(self, frame, anchor, ledger, world, **kwargs)
        if self._prepared_fields is not None:
            timeline.prepared_identity = _async_identity_parts(
                self._prepared_fields.async_identity,
            )
        next_index = self.executor.action_index + 1
        if (
            self.executor.current_verified_action_started()
            and self.executor.active_verified_exit_state() is not None
            and next_index < len(self.route.action_route.actions)
            and self.generation_for(next_index) is None
            and self._work_identity is None
        ):
            self.prepare_upcoming_action(
                anchor,
                world,
                ledger_identity=ledger.ledger_identity,
            )
            timeline.event(
                anchor.movement_tick_id,
                "diagnostic_prepare_requested",
                prepared_action_index=next_index,
            )
        controller = self.executor._controller
        timeline.event(
            anchor.movement_tick_id,
            "coordinator_decide_exit",
            controller_state_after=(
                None if controller is None else str(getattr(controller, "state", None))
            ),
            action_index_after=self.executor.action_index,
            session_route_reason=decision.reason_code,
            landing_observed=frame.body.is_on_ground,
            observed_position=list(frame.body.position),
            observed_velocity_blocks_per_second=list(
                frame.body.velocity_blocks_per_second
            ),
            ledger_latest_tick=ledger.latest_movement_tick_id,
            ledger_is_current=(
                ledger.latest_movement_tick_id == anchor.movement_tick_id
            ),
            changed_cells=[list(position) for position in frame.changed_cells],
        )
        return decision

    def executor_advance(self, frame, started, **kwargs):
        controller = self._controller
        tick = frame.body.movement_tick_id
        timeline.event(
            tick,
            "executor_advance_enter",
            controller_state_before=(
                None if controller is None else str(getattr(controller, "state", None))
            ),
            executor_advance_called=True,
            action_index_before_advance=self.action_index,
            boundary_before_advance=self.pending_action_boundary_index,
        )
        decision = original_advance(self, frame, started, **kwargs)
        timeline.event(
            tick,
            "executor_advance_exit",
            action_index_after_advance=self.action_index,
            boundary_after_advance=self.pending_action_boundary_index,
            executor_route_reason=decision.reason_code,
        )
        return decision

    def stage_prepared(self, frame, anchor):
        proposal = original_stage(self, frame, anchor)
        handle = self._prepared_handle(self.action_index + 1)
        hot = None if handle is None else handle.hot_view
        current_facts = (
            ()
            if hot is None
            else tuple((position, frame.world.cell(position))
                       for position, _fact in hot.dependency_facts)
        )
        expected_facts = () if hot is None else hot.dependency_facts
        if (
            dependency_gate == "legacy_full_cell_fact"
            and proposal.entry_result is PreparedQueryStatus.READY_MATCH
            and expected_facts != current_facts
        ):
            proposal = replace(
                proposal,
                entry_result=PreparedQueryStatus.DEPENDENCY_CHANGED,
            )
            timeline.event(
                frame.body.movement_tick_id,
                "diagnostic_legacy_full_cell_fact_rejection",
            )
        if (force_prepared_entry_mismatch
                and proposal.boundary_reached
                and proposal.entry_result is PreparedQueryStatus.READY_MATCH):
            proposal = replace(
                proposal,
                entry_result=PreparedQueryStatus.ENTRY_MISMATCH,
            )
            timeline.event(
                frame.body.movement_tick_id,
                "diagnostic_forced_prepared_entry_mismatch",
            )
        current_versions = (
            None
            if hot is None or frame.world._owner is None
            else frame.world._owner.dependency_versions(
                tuple(position for position, _version in hot.dependency_versions)
            )
        )
        stamp_only = tuple(
            position
            for (position, expected), (actual_position, actual) in zip(
                expected_facts, current_facts
            )
            if position == actual_position
            and expected != actual
            and _fact_payload(expected) == _fact_payload(actual)
        )
        body_matches = bool(
            hot is not None
            and any(branch.trajectory[0] == anchor.physics_state
                    for branch in hot.branches)
        )
        timeline.event(
            frame.body.movement_tick_id,
            "prepared_action_staged",
            prepared_entry_status=proposal.entry_result.value,
            prepared_boundary_reached=proposal.boundary_reached,
            body_matches_prepared_entry=body_matches,
            requested_window_allows_next_tick=(
                False
                if hot is None
                else hot.requested_window.allows_start(anchor.movement_tick_id + 1)
            ),
            dependency_facts_equal=(expected_facts == current_facts),
            dependency_payloads_equal=(
                len(expected_facts) == len(current_facts)
                and all(
                    expected_position == current_position
                    and _fact_payload(expected) == _fact_payload(current)
                    for (expected_position, expected), (current_position, current)
                    in zip(expected_facts, current_facts)
                )
            ),
            dependency_versions_equal=(
                hot is not None and current_versions == hot.dependency_versions
            ),
            stamp_only_dependency_differences=len(stamp_only),
            sample_stamp_only_cells=[list(position) for position in stamp_only[:4]],
            prepared_window=(
                None
                if proposal.permitted_window is None
                else [
                    proposal.permitted_window.earliest_start_tick,
                    proposal.permitted_window.latest_start_tick,
                ]
            ),
        )
        return proposal

    def approve(self, control, proposal, frame, ledger):
        approval = original_approve(self, control, proposal, frame, ledger)
        timeline.event(
            frame.body.movement_tick_id,
            "supervisor_approval",
            supervisor_status=approval.status.value,
            supervisor_reason=(
                None if approval.reason is None else approval.reason.value
            ),
        )
        return approval

    def commit_activation(self, transaction, accepted, delivered):
        proposal = None if self._prepared_approval is None else self._prepared_approval.proposal
        tick = None if proposal is None else proposal.entry_state.movement_tick_id
        result = original_commit_activation(
            self, transaction, accepted, delivered,
        )
        control = self.route
        if tick is not None and control is not None:
            timeline.event(
                tick,
                "prepared_route_commit",
                action_index_after_prepared_commit=control.executor.action_index,
                tracker_action_index_after_prepared_commit=(
                    None if control.tracker is None
                    else control.tracker.identity.action_index
                ),
            )
        return result

    def validate_routes(self, frame):
        control = self.route
        executor = None if control is None else control.executor
        before = None if executor is None else executor._cancel_requested
        result = original_validate_routes(self, frame)
        timeline.event(
            frame.body.movement_tick_id,
            "routes_validated",
            route_cancel_before_validation=before,
            route_cancel_after_validation=(
                None if executor is None else executor._cancel_requested
            ),
            route_stop_cause_after_validation=(
                None if executor is None or executor._stop_cause is None
                else executor._stop_cause.value
            ),
            incumbent_validation=(
                None if result.incumbent is None
                else result.incumbent.disposition.value
            ),
            incumbent_validation_reason=(
                None if result.incumbent is None else result.incumbent.reason
            ),
        )
        return result

    def session_proposal(self, *args, **kwargs):
        frame = self._frame
        tick = None if frame is None else frame.body.movement_tick_id
        decision = kwargs.get("route_decision")
        if tick is not None:
            timeline.event(
                tick,
                "session_proposal_enter",
                session_route_reason=(
                    None if decision is None else decision.reason_code
                ),
                boundary_at_session_proposal=(
                    None
                    if self._executor is None
                    else self._executor.pending_action_boundary_index
                ),
            )
        proposal = original_proposal(self, *args, **kwargs)
        if tick is not None:
            timeline.event(tick, "session_proposal_exit")
        return proposal

    def prepared_decision(self, anchor):
        tracker = self.stream_tracker
        branches = self.remaining_start_branches
        branch = branches[0] if len(branches) == 1 else None
        index = self._command_index
        expected = (
            None
            if branch is None or index >= len(branch.trajectory)
            else branch.trajectory[index]
        )
        timeline.event(
            anchor.movement_tick_id,
            "prepared_controller_decide_enter",
            prepared_command_index=index,
            prepared_controller_state_before=self.state.value,
            prepared_expected_state_matches=(
                None if expected is None else expected == anchor.physics_state
            ),
            prepared_expected_position=(
                None if expected is None else list(expected.position)
            ),
            prepared_actual_position=list(anchor.physics_state.position),
            prepared_expected_velocity=(
                None if expected is None
                else list(expected.velocity_blocks_per_tick)
            ),
            prepared_actual_velocity=list(
                anchor.physics_state.velocity_blocks_per_tick
            ),
            prepared_expected_yaw=(None if expected is None else expected.yaw_radians),
            prepared_actual_yaw=anchor.physics_state.yaw_radians,
            prepared_expected_on_ground=(None if expected is None else expected.on_ground),
            prepared_actual_on_ground=anchor.physics_state.on_ground,
            prepared_expected_pose=(None if expected is None else expected.pose),
            prepared_actual_pose=anchor.physics_state.pose,
        )
        decision = original_prepared_decision(self, anchor)
        timeline.event(
            anchor.movement_tick_id,
            "prepared_controller_decide_exit",
            prepared_controller_state_after=self.state.value,
            prepared_controller_reason=decision.reason,
        )
        return decision

    def verified_decide(self, anchor, ledger, *, changed_cells=()):
        timeline.event(
            anchor.movement_tick_id,
            "verified_controller_decide_enter",
            verified_state_before_decide=self.state.value,
            verified_changed_cells=[list(position) for position in changed_cells],
        )
        decision = original_verified_decide(
            self, anchor, ledger, changed_cells=changed_cells,
        )
        timeline.event(
            anchor.movement_tick_id,
            "verified_controller_decide_exit",
            verified_state_after_decide=self.state.value,
            verified_decision_reason=decision.reason,
        )
        return decision

    def record_application(self, application, *, command_index):
        result = original_record_application(
            self, application, command_index=command_index,
        )
        timeline.event(
            application.effect_tick,
            "prepared_application_recorded",
            prepared_application_status=result.status.value,
            prepared_application_reason=(
                None if result.reason is None else result.reason.value
            ),
            prepared_application_command_index=command_index,
            prepared_controller_state_after_application=self.state.value,
        )
        return result

    def record_missing_application(self, *, command_index):
        result = original_record_missing(self, command_index=command_index)
        effect_tick = (
            None if self.stream_tracker is None
            else self.stream_tracker.expected_effect_tick(command_index)
        )
        if effect_tick is not None:
            timeline.event(
                effect_tick,
                "prepared_application_marked_missing",
                prepared_missing_status=result.status.value,
                prepared_missing_reason=(
                    None if result.reason is None else result.reason.value
                ),
                prepared_missing_command_index=command_index,
                prepared_controller_state_after_missing=self.state.value,
            )
        return result

    def current_entry_check(self, anchor):
        controller = self._controller
        result = original_entry_check(self, anchor)
        timeline.event(
            anchor.movement_tick_id,
            "current_verified_entry_checked",
            current_verified_entry_result=result,
            current_verified_has_started=(
                None if not isinstance(controller, VerifiedMotionExecutor)
                else controller.has_started()
            ),
            current_verified_command_index=(
                None if not isinstance(controller, VerifiedMotionExecutor)
                else controller._command_index
            ),
            current_verified_state=(
                None if not isinstance(controller, VerifiedMotionExecutor)
                else controller.state.value
            ),
        )
        return result

    def control_step(context):
        source_tick = context.backend.movement_tick
        result = context.driver.tick(
            BehaviorProfileV0(), context.clock[0] + 500_000_000
        )
        control = context.session._supervisor.route
        controller = None if control is None else control.executor._controller
        timeline.event(
            source_tick,
            "runtime_selected",
            runtime_selected_movement=_movement(result.decision.action.movement),
            runtime_selected_groups=[group for group, _intent_id
                                     in result.decision.selected_intents],
            runtime_effect_tick=context.backend.movement_tick,
            controller_state_after_runtime=(
                None if controller is None
                else str(getattr(controller, "state", None))
            ),
        )
        return ()

    snapshot_borrow_attempts = 0
    original_borrow = WorldKnowledge.try_borrow_snapshot_version
    def controlled_borrow(world, *args, **kwargs):
        nonlocal snapshot_borrow_attempts
        snapshot_borrow_attempts += 1
        limit = busy_cycles[snapshot_borrow_mode]
        if (limit is None
                or limit == -1 and snapshot_borrow_attempts > 1
                or limit >= 0 and snapshot_borrow_attempts <= limit):
            return SnapshotBorrowResult(SnapshotBorrowStatus.BUSY)
        return original_borrow(world, *args, **kwargs)

    snapshot_patch = (
        patch.object(WorldKnowledge, "try_borrow_snapshot_version", controlled_borrow)
        if snapshot_borrow_mode != "normal" else nullcontext()
    )
    with (
        patch.object(MotionRouteCoordinator, "decide", coordinator_decide),
        patch.object(ActionRouteExecutor, "_advance", executor_advance),
        patch.object(ActionRouteExecutor, "stage_prepared_action", stage_prepared),
        patch.object(ExecutionSupervisor, "approve_action_handoff", approve),
        patch.object(ExecutionSupervisor, "commit_activation", commit_activation),
        patch.object(ExecutionSupervisor, "_validate_routes", validate_routes),
        patch.object(NavigationSession, "_proposal", session_proposal),
        patch.object(VerifiedMotionExecutor, "prepared_decision", prepared_decision),
        patch.object(VerifiedMotionExecutor, "decide", verified_decide),
        patch.object(VerifiedMotionExecutor, "record_application", record_application),
        patch.object(VerifiedMotionExecutor, "record_missing_application", record_missing_application),
        patch.object(ActionRouteExecutor, "current_verified_motion_can_start", current_entry_check),
        snapshot_patch,
    ):
        result = run(
            _representative(max_ticks=max_ticks),
            motion_factory=InlineMotionWorker,
            control_step=control_step,
        )

    trace_by_tick = {row["movement_tick"]: row for row in result.trace}
    for tick, row in timeline.rows.items():
        trace = trace_by_tick.get(tick)
        if trace is not None:
            row["applied_movement_at_observation"] = trace["applied_movement"]
            row["session_state"] = trace["session_state"]
            row["session_reason_after_effect"] = trace["session_reason"]
            row["runtime_failure"] = trace["runtime_failure"]
            validation = trace["route_validation"]
            incumbent = None if validation is None else validation["incumbent"]
            row["route_validation"] = (
                None if validation is None else {
                    "observation_sequence_id": validation["observation_sequence_id"],
                    "incumbent": (
                        None if incumbent is None else {
                            "action_index": incumbent["identity"]["action_index"],
                            "disposition": incumbent["disposition"],
                            "reason": incumbent["reason"],
                        }
                    ),
                }
            )
            row["controller_ids"] = list(trace["controller_ids"])
            row["source_bound"] = trace["source_bound"]
            row["body_control_activities"] = [
                _activity_summary(activity)
                for activity in trace["body_control_activities"]
            ]
            row["submitted_body_activity"] = _activity_summary(
                trace["submitted_body_activity"]
            )
            row["applied_body_activity"] = _activity_summary(
                trace["applied_body_activity"]
            )
            active_owner_ids = {
                activity["owner_id"]
                for activity in trace["body_control_activities"]
            }
            applied_owner = (
                None if trace["applied_body_activity"] is None
                else trace["applied_body_activity"]["owner_id"]
            )
            row["active_and_applied_owner_match"] = (
                applied_owner is None or applied_owner in active_owner_ids
            )
            row["owner_gap"] = bool(
                any(trace["applied_movement"].values())
                and trace["applied_body_activity"] is None
            )
            normalized_async = []
            for event in trace["async_events"]:
                identity = event["identity"]
                identity_parts = _async_identity_parts(identity)
                item = {
                    "work_kind": identity["work_kind"],
                    "revision": identity["revision"],
                    "identity_token": timeline.identity_token(identity_parts),
                    "prepared_identity": identity_parts == timeline.prepared_identity,
                }
                if "operation" in event:
                    item.update(
                        record_type="event",
                        operation=event["operation"],
                        monotonic_ns=event["monotonic_ns"],
                    )
                else:
                    item.update(
                        record_type="admission",
                        disposition=event["disposition"],
                        accepted_monotonic_ns=event["accepted_monotonic_ns"],
                        deadline_monotonic_ns=event["deadline_monotonic_ns"],
                        identity_matched=event["identity_matched"],
                        facts_valid=event["facts_valid"],
                    )
                normalized_async.append(item)
            row["async_events"] = normalized_async
    ordered_rows = [timeline.rows[tick] for tick in sorted(timeline.rows)]
    boundary_row = next((row for row in ordered_rows
        if row.get("session_route_reason") == "prepared_action_boundary"
        and row.get("prepared_boundary_reached")), None)
    application_row = next((row for row in ordered_rows
        if row.get("prepared_application_status") == "applied"
        and row.get("applied_movement_at_observation", {}).get("jump")), None)
    boundary_tick = (None if boundary_row is None
                     else boundary_row["movement_tick"])
    application_tick = (None if application_row is None
                        else application_row["movement_tick"])
    second_jump_started = bool(
        application_row is not None
    )
    owner_gap_ticks = [
        tick for tick, row in timeline.rows.items() if row.get("owner_gap")
    ]
    async_violation_tick = next(
        (
            int(tick)
            for tick, code, _detail in result.violations
            if code == "I19"
        ),
        None,
    )
    async_records = [
        (row["movement_tick"], event)
        for row in ordered_rows
        for event in row.get("async_events", ())
    ]
    prepared_identity_token = (
        None if timeline.prepared_identity is None
        else timeline.identity_token(timeline.prepared_identity)
    )
    prepared_lifecycle = _prepared_lifecycle_summary(
        async_records, prepared_identity_token,
    )
    return {
        "schema": "mc2p.d116-handoff-timeline.v1",
        "decision": "D116",
        "phase": (
            "D116-0-red-replay"
            if dependency_gate == "legacy_full_cell_fact"
            else "D116-1"
        ),
        "dependency_gate": dependency_gate,
        "platform": "Windows",
        "production_behavior_changed": dependency_gate == "semantic",
        "evidence_provenance": (
            "explicit in-process replay of the legacy full-CellFact gate; "
            "the original uncommitted D116-0 file was overwritten"
            if dependency_gate == "legacy_full_cell_fact"
            else "current D116-1 production semantic dependency result"
        ),
        "diagnostic_scaffolding": (
            "D115's withdrawn local prepare_upcoming_action call is restored "
            "only by an in-process method patch"
        ),
        "formal_chain": [
            "PlayerRuntimeV1",
            "RuntimeNavigationDriver",
            "NavigationSession",
            "ExecutionSupervisor",
            "ActionRouteExecutor",
            "MotionRouteCoordinator",
            "CalculatorBackend",
        ],
        "planned_actions": timeline.planned_actions,
        "outcome": result.outcome,
        "reason": result.reason,
        "final_position": list(result.final_position),
        "damage": result.damage,
        "violations": result.violations,
        "second_jump_started": second_jump_started,
        "prepared_apply_lifecycle": prepared_lifecycle,
        "attribution": (
            "legacy_full_cell_fact_gate_rejected_stamp_refresh"
            if dependency_gate == "legacy_full_cell_fact"
            else "semantic_dependency_async_apply_route_tracker_and_numeric_state_"
                 "passed"
        ),
        "milestones": {
            "handoff_boundary_tick": boundary_tick,
            "second_jump_application_tick": application_tick,
            "boundary_ready_match": (
                boundary_row is not None
                and boundary_row.get("prepared_entry_status") == "ready_match"
            ),
            "supervisor_approved": (
                boundary_row is not None
                and boundary_row.get("supervisor_status") == "approved"
            ),
            "second_jump_applied": second_jump_started,
            "single_movement_winner_at_handoff": (
                boundary_row is not None
                and boundary_row.get("runtime_selected_groups", []).count("movement") == 1
            ),
            "owner_gap_ticks": owner_gap_ticks,
            "zero_damage": result.damage == 0.0,
        },
        "stop": {
            "route_tracker": None if dependency_gate == "legacy_full_cell_fact" else {
                "movement_tick": application_tick,
                "reason": (None if application_row is None else
                           application_row.get("incumbent_validation_reason")),
                "disposition": (None if application_row is None else
                                application_row.get("incumbent_validation")),
                "effect": "route_tracker_and_numeric_state_passed",
            },
            "async_monitor": {
                "movement_tick": async_violation_tick,
                "violation": "I19",
                "detail": "prepared admission had finish and admission but no lifecycle apply event",
                "relationship": (
                    "pre-existing in the reconstructed RED and not caused by D116-1"
                ),
            },
        },
        "timeline": ordered_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--dependency-gate",
        choices=("semantic", "legacy_full_cell_fact"),
        default="semantic",
    )
    args = parser.parse_args()
    result = run_representative_timeline(dependency_gate=args.dependency_gate)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
