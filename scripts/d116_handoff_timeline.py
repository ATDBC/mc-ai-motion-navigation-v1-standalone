"""Read-only D116 attribution for the first formal two-gap handoff.

The production tree deliberately does not auto-prepare a strict successor yet.
This diagnostic restores only D115's withdrawn coordinator call with a method
patch, then records the unchanged Session/Supervisor/Executor ordering.  It
does not alter production state-machine code or claim that two-jump continuity
is available.
"""
from __future__ import annotations

import argparse
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
from mc2p.motion_nav.prepared_motion import PreparedQueryStatus
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


def _representative() -> Scenario:
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
        max_ticks=25,
    )


class _Timeline:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, object]] = {}
        self.planned_actions: list[str] = []

    def row(self, tick: int) -> dict[str, object]:
        row = self.rows.setdefault(tick, {"movement_tick": tick, "event_order": []})
        return row

    def event(self, tick: int, name: str, **values: object) -> None:
        row = self.row(tick)
        row["event_order"].append(name)
        row.update(values)


def run_representative_timeline(*, dependency_gate: str = "semantic") -> dict[str, object]:
    """Run the formal chain with a current or explicitly replayed dependency gate."""
    if dependency_gate not in {"semantic", "legacy_full_cell_fact"}:
        raise ValueError("dependency_gate must be semantic or legacy_full_cell_fact")
    timeline = _Timeline()
    original_decide = MotionRouteCoordinator.decide
    original_advance = ActionRouteExecutor._advance
    original_stage = ActionRouteExecutor.stage_prepared_action
    original_approve = ExecutionSupervisor.approve_action_handoff
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

    with (
        patch.object(MotionRouteCoordinator, "decide", coordinator_decide),
        patch.object(ActionRouteExecutor, "_advance", executor_advance),
        patch.object(ActionRouteExecutor, "stage_prepared_action", stage_prepared),
        patch.object(ExecutionSupervisor, "approve_action_handoff", approve),
        patch.object(ExecutionSupervisor, "_validate_routes", validate_routes),
        patch.object(NavigationSession, "_proposal", session_proposal),
        patch.object(VerifiedMotionExecutor, "prepared_decision", prepared_decision),
        patch.object(VerifiedMotionExecutor, "decide", verified_decide),
        patch.object(VerifiedMotionExecutor, "record_application", record_application),
        patch.object(VerifiedMotionExecutor, "record_missing_application", record_missing_application),
        patch.object(ActionRouteExecutor, "current_verified_motion_can_start", current_entry_check),
    ):
        result = run(
            _representative(),
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
                item = {
                    "work_kind": identity["work_kind"],
                    "revision": identity["revision"],
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
    tick20 = timeline.rows[20]
    tick21 = timeline.rows[21]
    second_jump_started = bool(
        tick21.get("applied_movement_at_observation", {}).get("jump")
        and tick21.get("prepared_application_status") == "applied"
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
        "attribution": (
            "legacy_full_cell_fact_gate_rejected_stamp_refresh"
            if dependency_gate == "legacy_full_cell_fact"
            else "semantic_dependency_handoff_passed_then_route_tracker_identity_"
                 "and_async_apply_invariants_blocked"
        ),
        "milestones": {
            "tick20_ready_match": tick20.get("prepared_entry_status") == "ready_match",
            "tick20_supervisor_approved": tick20.get("supervisor_status") == "approved",
            "tick21_second_jump_applied": bool(
                tick21.get("applied_movement_at_observation", {}).get("jump")
            ),
            "single_movement_winner_at_handoff": (
                tick20.get("runtime_selected_groups", []).count("movement") == 1
            ),
            "owner_gap_ticks": owner_gap_ticks,
            "zero_damage": result.damage == 0.0,
        },
        "stop": {
            "route_tracker": None if dependency_gate == "legacy_full_cell_fact" else {
                "movement_tick": 21,
                "reason": tick21.get("incumbent_validation_reason"),
                "disposition": tick21.get("incumbent_validation"),
                "effect": "dependency_changed_cancelled_new_prepared_controller",
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
        "timeline": [timeline.rows[tick] for tick in sorted(timeline.rows)],
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
