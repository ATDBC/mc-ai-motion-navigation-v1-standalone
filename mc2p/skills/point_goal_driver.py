"""Independent ordered Runtime driver for one bounded point-goal task."""
from __future__ import annotations

import json
import time
from typing import Callable
import uuid

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import (
    ControlFrameEventV1, ControlFrameProposalV1, OrderedIntentV1,
    ordered_intent_id,
)
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.point_goal import GoalMonitor, PointGoal
from mc2p.skills.point_goal_policy import PointGoalPolicy


INTERVAL_NS = 50_000_000
STEP_LEASE_NS = 250_000_000
CLEANUP_NS = 1_000_000_000
OWNER_LEASE_NS = 3_000_000_000


class PointGoalDriver:
    def __init__(self, runtime: PlayerRuntimeV1, state: NavigationState,
                 policy: PointGoalPolicy,
                 clock_ns: Callable[[], int] = time.perf_counter_ns, *,
                 observation_request: ObservationRequestV3 | None = None) -> None:
        if (type(runtime) is not PlayerRuntimeV1 or type(state) is not NavigationState
                or type(policy) is not PointGoalPolicy):
            raise ContractViolation("point-goal driver requires formal Runtime/state/policy")
        if runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("point-goal Runtime must be ready")
        if type(runtime.observation) is not ObservationSnapshotV3:
            raise ContractViolation("point-goal driver requires Observation V3")
        if observation_request is not None and type(observation_request) is not ObservationRequestV3:
            raise ContractViolation("point-goal observation request is invalid")
        self.runtime, self.navigation_state, self.policy = runtime, state, policy
        self._clock = clock_ns
        self._observation_request = observation_request or ObservationRequestV3()
        self.clock_id = runtime.observation.controller_clock_id
        self.attempt_id = uuid.uuid4().hex
        self.source = None
        self.sequence = 0
        self.next_update_at_ns = 0
        self._owner_deadline_ns = 0
        self._goal: PointGoal | None = None
        self._task: TaskIntentV0 | None = None
        self._monitor: GoalMonitor | None = None
        self._last_goal_update_ns: int | None = None
        self.state, self.reason = "ready", None
        self.last_result: RuntimeStepResultV1 | None = None
        self.last_execution_confirmed = False

    @staticmethod
    def _make_task(goal: PointGoal) -> TaskIntentV0:
        parameters = json.dumps({
            "goal_id": goal.goal_id,
            "scope_id": goal.scope_id,
            "position": {"x": goal.position.x, "y": goal.position.y, "z": goal.position.z},
            "radius": goal.radius,
            "height_tolerance": goal.height_tolerance,
            "dwell_ns": goal.dwell_ns,
            "stop_speed": goal.stop_speed,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return TaskIntentV0(
            task_id="point-goal/"+goal.goal_id,
            task_type="point_goal",
            parameters_json=parameters,
            success_criteria=(SuccessCriterionV0(
                "point_goal_reached", ComparisonOperatorV0.EQUAL, 1, "boolean"
            ),),
            priority=100,
            deadline_monotonic_ns=goal.deadline_ns,
            interruptible=True,
            max_risk=0.,
        )

    def start(self, goal: PointGoal, now_ns: int) -> None:
        if type(goal) is not PointGoal:
            raise ContractViolation("point-goal start requires PointGoal")
        require_nonnegative_int(now_ns, "point-goal start time")
        if self._goal is not None or self.state != "ready":
            raise ContractViolation("point-goal driver already started")
        if goal.scope_id != self.navigation_state.scope_id:
            raise ContractViolation("point-goal start scope mismatch")
        if now_ns >= goal.deadline_ns:
            raise ContractViolation("point-goal start deadline expired")
        if self.runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("point-goal Runtime must be ready")
        observation = self.runtime.observation
        if type(observation) is not ObservationSnapshotV3:
            raise ContractViolation("point-goal start requires V3 observation")
        self.navigation_state.observe(observation, now_ns)
        self.navigation_state.bind_policy(self.policy)
        self.policy.verify_control_sources()
        self.source = self.runtime.register_ordered_source(
            "point-goal-"+self.policy.group.lower()
        )
        self._goal = goal
        self._task = self._make_task(goal)
        self._monitor = GoalMonitor(goal)
        self._last_goal_update_ns = now_ns
        self.state, self.reason = "running", None

    def replace_goal(self, goal: PointGoal, now_ns: int) -> None:
        """Rebind the destination while preserving this driver's source and memory."""
        if type(goal) is not PointGoal:
            raise ContractViolation("point-goal replacement requires PointGoal")
        require_nonnegative_int(now_ns, "point-goal replacement time")
        if (self._goal is None or self._task is None or self._monitor is None
                or self.source is None or self._last_goal_update_ns is None):
            raise ContractViolation("point-goal driver has not started")
        if self.state in {"success", "stopped", "failed", "cancelled"}:
            raise ContractViolation("terminal point-goal driver cannot replace its goal")
        if goal.scope_id != self.navigation_state.scope_id:
            raise ContractViolation("point-goal replacement scope mismatch")
        if now_ns < self._last_goal_update_ns:
            raise ContractViolation("point-goal replacement time regressed")
        if now_ns >= goal.deadline_ns:
            raise ContractViolation("point-goal replacement deadline expired")
        if self.runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("closed Runtime cannot replace point goal")
        self._goal = goal
        self._task = self._make_task(goal)
        self._monitor = GoalMonitor(goal)
        self._last_goal_update_ns = now_ns
        self.next_update_at_ns = min(self.next_update_at_ns, now_ns)
        self.state, self.reason = "running", "goal_replaced"

    def _view(self, now_ns: int):
        if self.runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("closed/sealed point-goal driver cannot resume")
        observation = self.runtime.observation
        if type(observation) is not ObservationSnapshotV3:
            raise ContractViolation("point-goal driver requires V3 observation")
        return project_playground_view(observation, now_ns, self.clock_id)

    def _event(self, record_type: str, schema: str, **values) -> None:
        self.runtime.record_task_event(
            record_type, self._event_payload(schema, **values),
        )

    def _event_payload(self, schema: str, **values) -> dict:
        assert self.source is not None and self._task is not None
        return dict(
            schema_version=schema,
            task_id=self._task.task_id,
            attempt_id=self.attempt_id,
            episode_id=self.source.episode_id,
            source_generation=self.source.generation,
            **values,
        )

    @staticmethod
    def _receipt(result: RuntimeStepResultV1):
        return None if result.backend_result is None else result.backend_result.receipt

    @staticmethod
    def _later_observation(before: ObservationSnapshotV3,
                           after: ObservationSnapshotV3,
                           now_ns: int) -> bool:
        return (
            after.sequence_id > before.sequence_id
            and (after.episode_id, after.controller_clock_id,
                 after.client_sample.clock_id, after.source_backend)
                == (before.episode_id, before.controller_clock_id,
                    before.client_sample.clock_id, before.source_backend)
            and after.request_started_at_monotonic_ns >= before.received_at_monotonic_ns
            and after.received_at_monotonic_ns >= before.received_at_monotonic_ns
            and after.received_at_monotonic_ns <= now_ns
            and after.client_sample.started_at_monotonic_ns
                >= before.client_sample.completed_at_monotonic_ns
        )

    def _fail(self, error: BaseException) -> None:
        self.state, self.reason = "failed", type(error).__name__
        self.last_execution_confirmed = False
        if self._monitor is not None:
            self._monitor.cancel()
        self.policy.clear()
        if self.runtime.state is RuntimeStateV1.READY:
            try:
                self.runtime.fail_closed("point-goal control or evidence uncertain")
            except BaseException as secondary:
                error.add_note("seal also failed: "+repr(secondary))

    def _validate_owner(self, now_ns: int, owner_deadline_ns: int) -> str | None:
        require_nonnegative_int(owner_deadline_ns, "point-goal owner deadline")
        if owner_deadline_ns < self._owner_deadline_ns:
            raise ContractViolation("owner lease regressed")
        if owner_deadline_ns > now_ns + OWNER_LEASE_NS:
            raise ContractViolation("owner lease is unbounded")
        if owner_deadline_ns <= now_ns:
            return "owner_heartbeat_lost"
        if owner_deadline_ns - now_ns < STEP_LEASE_NS:
            return "owner_lease_insufficient"
        return None

    def tick(self, profile: BehaviorProfileV0,
             owner_deadline_ns: int) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("point-goal tick requires BehaviorProfileV0")
        if self._goal is None or self._task is None or self._monitor is None or self.source is None:
            raise ContractViolation("point-goal driver has not started")
        if self.state in {"stopped", "failed", "cancelled"}:
            raise ContractViolation("closed/sealed point-goal driver cannot resume")
        now_ns = self._clock()
        stop_reason = self._validate_owner(now_ns, owner_deadline_ns)
        if stop_reason is not None:
            return self.stop(profile, stop_reason)
        if (self.state == 'blocked' and self.reason in {
                'navigation_recovery_problem_deadline', 'navigation_recovery_attempts_exhausted',
                'navigation_recovery_task_deadline', 'navigation_recovery_local_entries_exhausted'}):
            # The previous neutral frame retained the terminal planning and
            # feedback evidence. Retire only our source on this next cycle.
            return self.stop(profile, self.reason)
        if self._goal.deadline_ns - now_ns < STEP_LEASE_NS:
            return self.stop(profile, "point_goal_deadline")
        try:
            if owner_deadline_ns != self._owner_deadline_ns:
                self._event(
                    "task_lease_renewed", "mc2p.task-lease-renewed.v1",
                    previous_owner_deadline_ns=self._owner_deadline_ns,
                    owner_deadline_ns=owner_deadline_ns, renewed_at_ns=now_ns,
                )
                self._owner_deadline_ns = owner_deadline_ns
            if now_ns < self.next_update_at_ns:
                return None
            observation = self.runtime.observation
            assert type(observation) is ObservationSnapshotV3
            snapshot = self.navigation_state.observe(observation, now_ns)
            view = self._view(now_ns)
            decision = self.policy.decide(
                snapshot, view, now_ns, self._goal,
                belief=self.navigation_state.belief,
            )
            deadline_ns = min(owner_deadline_ns, self._goal.deadline_ns,
                              now_ns+STEP_LEASE_NS)
            self.runtime.cancel_source(self.source.source_id)
            # The planned proposal is tied to the exact observed pose, source,
            # memory generation and controls; renew none of them at dispatch.
            recheck_now_ns = self._clock()
            decision = self.policy.revalidate(
                decision, self.navigation_state.snapshot, view,
                recheck_now_ns, belief=self.navigation_state.belief,
            )
            deadline_ns = self.policy.submission_deadline(deadline_ns)
            next_sequence = self.sequence+1
            intent_id = ordered_intent_id(self.source, next_sequence)
            intent = ActionIntentV1(
                intent_id, self.source.source_id, self.source.episode_id,
                view.base.sequence_id, ActionPriorityV0.TASK, now_ns, deadline_ns,
                movement=decision.movement, look=decision.look, valid_for_ticks=1,
                movement_requires_look=True,
            )
            planning_event = ControlFrameEventV1(
                "playground_task", self._event_payload(
                "mc2p.playground-task-step.v1",
                intent_sequence=next_sequence,
                observation_sequence_id=view.base.sequence_id,
                group=self.policy.group,
                state=decision.state,
                reason=decision.reason,
                movement=decision.movement,
                look=decision.look,
                selected_waypoint=self.policy.selected_waypoint,
                planning_diagnostic=self.policy.planning_diagnostic,
                owner_deadline_ns=owner_deadline_ns,
                ),
            )
            self.next_update_at_ns = now_ns+INTERVAL_NS
            step_task = TaskIntentV0(
                self._task.task_id, self._task.task_type,
                self._task.parameters_json, self._task.success_criteria,
                self._task.priority, deadline_ns, self._task.interruptible,
                self._task.max_risk, self._task.forbidden_actions,
            )
            try:
                result = self.runtime.control_frame(
                    step_task, profile, deadline_ns,
                    proposals=(ControlFrameProposalV1(
                        (OrderedIntentV1(self.source, next_sequence, intent),),
                        self._observation_request,
                        (planning_event,),
                    ),),
                )
            except ContractViolation as error:
                current = self._clock()
                if (str(error) != "intent has stale episode/observation or invalid time"
                        or self.runtime.state is not RuntimeStateV1.READY
                        or self.runtime.observation.episode_id != intent.episode_id
                        or self.runtime.observation.sequence_id != intent.observation_sequence_id
                        or current < intent.expires_at_monotonic_ns):
                    raise
                reason = ("owner_heartbeat_lost" if current >= owner_deadline_ns
                          else "action_lease_expired_before_dispatch")
                return self.stop(profile, reason)
            self.sequence = next_sequence
            receipt = self._receipt(result)
            cancelled = receipt is not None and receipt.status == "cancelled"
            runtime_cancelled = (result.report.failure is not None
                                 and result.report.failure.code.value == "cancelled")
            if runtime_cancelled:
                cancelled = True
            if (result.observation is None or result.decision is None or receipt is None
                    or receipt.status not in {"executed", "confirmed_local", "cancelled"}):
                raise RuntimeError("unconfirmed point-goal control")
            if result.report.failure is not None and not runtime_cancelled:
                raise RuntimeError("failed point-goal control: "+result.report.failure.reason)
            feedback_now = self._clock()
            if not self._later_observation(observation, result.observation, feedback_now):
                raise RuntimeError("point-goal execution lacks later compatible observation")
            post_snapshot = self.navigation_state.observe(result.observation, feedback_now)
            post_view = project_playground_view(result.observation, feedback_now, self.clock_id)
            selected = dict(result.decision.selected_intents)
            executed = (not cancelled and selected.get("movement") == intent_id
                        and selected.get("look") == intent_id)
            self.policy.bind_feedback_snapshot(post_snapshot)
            self.policy.feedback(executed, post_view, feedback_now)
            self.last_execution_confirmed = executed
            final_movement = (result.decision.action.movement
                              if not cancelled else MovementV1())
            if not executed:
                self._monitor.interrupt_dwell()
            monitor_state = self._monitor.observe(post_view, feedback_now, final_movement)
            if not executed:
                self._monitor.interrupt_dwell()
            if cancelled:
                self.state, self.reason = "cancelled", "client_control_cancelled"
                self._monitor.cancel()
                self.policy.clear()
                if self.runtime.state is RuntimeStateV1.READY:
                    # A cancelled receipt is positive local release evidence;
                    # retire the source without another action or retry.
                    self.runtime.cancel_source(self.source.source_id)
                    self.runtime.unregister_ordered_source(self.source)
            elif not executed:
                self.state, self.reason = "preempted", "arbitration_selected_other_or_filtered"
            elif monitor_state == "success":
                self.state, self.reason = "success", "point_goal_reached"
            else:
                self.state, self.reason = decision.state, decision.reason
            self.last_result = result
            return result
        except BaseException as error:
            self._fail(error)
            raise

    def stop(self, profile: BehaviorProfileV0, reason: str) -> RuntimeStepResultV1:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("point-goal stop requires BehaviorProfileV0")
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("point-goal stop requires a reason")
        if self._goal is None or self._task is None or self._monitor is None or self.source is None:
            raise ContractViolation("point-goal driver has not started")
        if self.state in {"stopped", "failed", "cancelled"}:
            raise ContractViolation("point-goal driver is already closed")
        now_ns = self._clock()
        try:
            self.runtime.cancel_source(self.source.source_id)
            deadline_ns = now_ns+CLEANUP_NS
            cleanup = TaskIntentV0(
                self._task.task_id, "point_goal_release", self._task.parameters_json,
                self._task.success_criteria, self._task.priority, deadline_ns,
                self._task.interruptible, self._task.max_risk,
                self._task.forbidden_actions,
            )
            result = self.runtime.control_frame(
                cleanup, profile, deadline_ns,
                proposals=(ControlFrameProposalV1(
                    observation_request=self._observation_request,
                ),),
            )
            receipt = self._receipt(result)
            if (result.observation is None or result.decision is None
                    or result.report.failure is not None or receipt is None
                    or receipt.status not in {"executed", "confirmed_local", "cancelled"}):
                raise RuntimeError("unconfirmed point-goal release")
            if any(value.startswith(self.source.source_id+"/")
                   for _, value in result.decision.selected_intents):
                raise RuntimeError("old point-goal control remained selected")
            feedback_now = self._clock()
            self.navigation_state.observe(result.observation, feedback_now)
            self._event(
                "playground_control_release", "mc2p.playground-control-release.v1",
                reason=reason, observation_sequence_id=result.observation.sequence_id,
                intent_sequence=self.sequence,
            )
            self.runtime.unregister_ordered_source(self.source)
            self.policy.clear()
            self._monitor.cancel()
            self.last_execution_confirmed = False
            self.last_result = result
            self.next_update_at_ns = feedback_now+INTERVAL_NS
            self.state, self.reason = "stopped", reason
            return result
        except BaseException as error:
            self._fail(error)
            raise
