"""Runtime bridge for one bounded external-motion recovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import IntentSourceV1, OrderedIntentV1, ordered_intent_id
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.external_motion import ExternalMotionEventV1
from mc2p.motion_nav.external_motion_recovery import (
    ExternalMotionRecoveryController,
    ExternalMotionRecoveryDecision,
    RecoveryDirective,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.point_goal import PointGoal
from mc2p.skills.point_goal_driver import PointGoalDriver
from mc2p.skills.point_goal_policy import PointGoalPolicy


STEP_LEASE_NS = 250_000_000
CLEANUP_NS = 1_000_000_000
OWNER_LEASE_NS = 3_000_000_000


@dataclass(frozen=True, slots=True)
class ExternalMotionRecoveryReport:
    state: str
    reason: str | None
    active_generation: int | None
    events: int
    elapsed_ticks: int
    stable_ticks: int
    complete: bool


class ExternalMotionRecoveryDriver:
    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        state: NavigationState,
        policy: PointGoalPolicy,
        *,
        task_deadline_ns: int,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        observation_request: ObservationRequestV3 | None = None,
        controller: ExternalMotionRecoveryController | None = None,
        evidence_scope_id: str = "external-motion-recovery",
    ) -> None:
        if (
            type(runtime) is not PlayerRuntimeV1
            or type(state) is not NavigationState
            or type(policy) is not PointGoalPolicy
        ):
            raise ContractViolation(
                "external recovery driver requires formal Runtime/state/policy"
            )
        require_nonnegative_int(task_deadline_ns, "external recovery task deadline")
        if runtime.state is not RuntimeStateV1.READY or task_deadline_ns <= clock_ns():
            raise ContractViolation("external recovery requires ready Runtime and future deadline")
        if type(runtime.observation) is not ObservationSnapshotV3:
            raise ContractViolation("external recovery requires V3 observation")
        self.runtime = runtime
        self.navigation_state = state
        self.policy = policy
        self.task_deadline_ns = task_deadline_ns
        self._clock = clock_ns
        if observation_request is not None and type(observation_request) is not ObservationRequestV3:
            raise ContractViolation("external recovery observation request is invalid")
        self._observation_request = observation_request or ObservationRequestV3()
        if controller is not None and type(controller) is not ExternalMotionRecoveryController:
            raise ContractViolation("external recovery controller is invalid")
        self.controller = controller or ExternalMotionRecoveryController()
        if type(evidence_scope_id) is not str or not evidence_scope_id:
            raise ContractViolation("external recovery evidence scope is invalid")
        self.evidence_scope_id = evidence_scope_id
        self.source: IntentSourceV1 | None = None
        self.hold_driver: PointGoalDriver | None = None
        self.sequence = 0
        self._event: ExternalMotionEventV1 | None = None
        self._last_decision: ExternalMotionRecoveryDecision | None = None
        self._state = "ready"
        self._reason: str | None = None

    @property
    def report(self) -> ExternalMotionRecoveryReport:
        decision = self._last_decision
        return ExternalMotionRecoveryReport(
            state=self._state,
            reason=self._reason,
            active_generation=self.controller.active_generation,
            events=self.controller.event_count,
            elapsed_ticks=0 if decision is None else decision.elapsed_ticks,
            stable_ticks=0 if decision is None else decision.stable_ticks,
            complete=self._state == "complete",
        )

    def start(self, event: ExternalMotionEventV1) -> None:
        if self._state != "ready":
            raise ContractViolation("external recovery driver already started")
        if event.episode_id != self.runtime.observation.episode_id:
            raise ContractViolation("external recovery event session mismatch")
        self.controller.start(event)
        self._event = event
        self.source = self.runtime.register_ordered_source("external-motion-recovery")
        self._state, self._reason = "running", "captured"
        self._record("started", event=event)

    def observe_event(self, event: ExternalMotionEventV1) -> bool:
        if self._state not in {"running", "braking", "returning_air"}:
            raise ContractViolation("external recovery driver has no active recovery")
        accepted = self.controller.observe_event(event)
        if accepted:
            self._event = event
            self._reason = "recovery_event_updated"
            self._record("event_updated", event=event)
        return accepted

    def tick(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("external recovery tick requires BehaviorProfileV0")
        if self._state in {"ready", "complete", "cancelled", "failed"}:
            raise ContractViolation("closed external recovery driver cannot tick")
        now_ns = self._clock()
        require_nonnegative_int(owner_deadline_ns, "external recovery owner deadline")
        if owner_deadline_ns <= now_ns:
            return self.cancel(profile, "owner_heartbeat_lost")
        if owner_deadline_ns > now_ns + OWNER_LEASE_NS:
            raise ContractViolation("external recovery owner lease is unbounded")
        if self.runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("external recovery Runtime is not ready")

        # Keep one backend step per public tick.  Releasing the point-goal
        # source in the same call as the second stable sample would skip an
        # observation for every parent that consumes only the returned result.
        if self._state == "releasing":
            if self.hold_driver is None:
                raise ContractViolation("releasing recovery has no point-goal owner")
            cleanup = self.hold_driver.stop(
                profile, "external_motion_recovery_complete"
            )
            self._state, self._reason = "complete", "stable_reanchored"
            self._record("completed")
            return cleanup
        if self._state == "returning_air":
            if self.hold_driver is None:
                raise ContractViolation("air handoff has no point-goal owner")
            cleanup = self.hold_driver.stop(
                profile, "external_motion_airborne_again"
            )
            self.hold_driver = None
            self.source = self.runtime.register_ordered_source(
                "external-motion-recovery"
            )
            self._state, self._reason = "running", "air_source_reacquired"
            self._record("air_source_reacquired")
            return cleanup

        decision = self.controller.decide(self.runtime.observation)
        self._last_decision = decision
        self._record("decision", decision=decision)
        if decision.directive is RecoveryDirective.EXHAUSTED:
            return self.cancel(profile, decision.reason)
        if decision.directive is RecoveryDirective.NEUTRAL_AIR:
            result = self._neutral_step(profile, owner_deadline_ns)
            post = self.controller.decide(result.observation)
            self._last_decision = post
            self._record("decision", decision=post)
            if post.directive is RecoveryDirective.START_GROUND_HOLD:
                self._start_hold()
            return result
        if decision.directive is RecoveryDirective.START_GROUND_HOLD:
            self._start_hold()
        if self.hold_driver is None:
            raise ContractViolation("ground recovery has no point-goal owner")

        result = self.hold_driver.tick(profile, owner_deadline_ns)
        if result is None:
            return None
        post = self.controller.decide(result.observation)
        self._last_decision = post
        self._record("decision", decision=post)
        if post.directive is RecoveryDirective.NEUTRAL_AIR:
            self._state, self._reason = "returning_air", post.reason
            return result
        if post.complete:
            self._state, self._reason = "releasing", post.reason
            return result
        self._state, self._reason = "braking", post.reason
        return result

    def cancel(self, profile: BehaviorProfileV0, reason: str) -> RuntimeStepResultV1:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("external recovery cancel requires BehaviorProfileV0")
        if not isinstance(reason, str) or not reason:
            raise ContractViolation("external recovery cancel requires reason")
        if self._state in {"ready", "complete", "cancelled", "failed"}:
            raise ContractViolation("external recovery driver is already closed")
        if self.hold_driver is not None:
            result = self.hold_driver.stop(profile, reason)
        else:
            result = self._release_air_source(profile, reason)
        self._state, self._reason = "cancelled", reason
        self._record("cancelled")
        return result

    def _record(
        self,
        stage: str,
        *,
        event: ExternalMotionEventV1 | None = None,
        decision: ExternalMotionRecoveryDecision | None = None,
    ) -> None:
        observation = self.runtime.observation
        own = observation.self_state.value
        hold_source = None
        if self.hold_driver is not None and self.hold_driver.source is not None:
            hold_source = self.hold_driver.source.source_id
        owners = tuple(source for source in (
            None if self.source is None else self.source.source_id,
            hold_source,
        ) if source is not None)
        self.runtime.record_task_event("external_motion_recovery", {
            "schema_version": "mc2p.external-motion-recovery-event.v1",
            "episode_id": observation.episode_id,
            "recovery_scope_id": self.evidence_scope_id,
            "stage": stage,
            "state": self._state,
            "reason": self._reason,
            "observation_sequence_id": observation.sequence_id,
            "movement_tick_id": None if own is None else own.movement_tick_id,
            "is_on_ground": None if own is None else own.is_on_ground,
            "velocity": None if own is None else trace_projection(own.velocity),
            "event": trace_projection(event),
            "decision": trace_projection(decision),
            "movement_owner_source_ids": owners,
        })

    def _start_hold(self) -> None:
        if self.hold_driver is not None:
            return
        if self.source is not None:
            self.runtime.cancel_source(self.source.source_id)
            self.runtime.unregister_ordered_source(self.source)
            self.source = None
        own = self.runtime.observation.self_state.value
        if own is None:
            raise ContractViolation("ground recovery requires current self state")
        now_ns = self._clock()
        self.hold_driver = PointGoalDriver(
            self.runtime,
            self.navigation_state,
            self.policy,
            clock_ns=self._clock,
            observation_request=self._observation_request,
        )
        self.hold_driver.start(
            PointGoal(
                goal_id=f"external-recovery/{self._event.event_id}",
                scope_id=self.navigation_state.scope_id,
                position=own.position,
                deadline_ns=self.task_deadline_ns,
                radius=.10,
                height_tolerance=.10,
                dwell_ns=300_000_000,
                stop_speed=.03,
            ),
            now_ns,
        )
        self._state, self._reason = "braking", "ground_hold_started"

    def _neutral_step(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1:
        if self.source is None:
            raise ContractViolation("airborne recovery has no input source")
        now_ns = self._clock()
        deadline_ns = min(owner_deadline_ns, self.task_deadline_ns, now_ns + STEP_LEASE_NS)
        if deadline_ns <= now_ns:
            raise ContractViolation("external recovery action window expired")
        self.runtime.cancel_source(self.source.source_id)
        self.sequence += 1
        intent_id = ordered_intent_id(self.source, self.sequence)
        intent = ActionIntentV1(
            intent_id=intent_id,
            source_id=self.source.source_id,
            episode_id=self.source.episode_id,
            observation_sequence_id=self.runtime.observation.sequence_id,
            priority=ActionPriorityV0.TASK,
            submitted_at_monotonic_ns=now_ns,
            expires_at_monotonic_ns=deadline_ns,
            movement=MovementV1(),
            valid_for_ticks=1,
        )
        self.runtime.submit_ordered_intent(OrderedIntentV1(self.source, self.sequence, intent))
        task = self._task("external_motion_airborne", deadline_ns)
        result = self.runtime.step(
            task,
            profile,
            deadline_ns,
            observation_request=self._observation_request,
        )
        self._require_confirmed(result, intent_id)
        return result

    def _release_air_source(
        self,
        profile: BehaviorProfileV0,
        reason: str,
    ) -> RuntimeStepResultV1:
        if self.source is None:
            raise ContractViolation("external recovery has no airborne source")
        self.runtime.cancel_source(self.source.source_id)
        now_ns = self._clock()
        deadline_ns = min(self.task_deadline_ns, now_ns + CLEANUP_NS)
        result = self.runtime.step(
            self._task("external_motion_release", deadline_ns, reason),
            profile,
            deadline_ns,
            observation_request=self._observation_request,
        )
        receipt = None if result.backend_result is None else result.backend_result.receipt
        if (
            result.observation is None
            or result.decision is None
            or result.report.failure is not None
            or receipt is None
            or receipt.status not in {"executed", "confirmed_local", "cancelled"}
        ):
            raise RuntimeError("unconfirmed external recovery release")
        self.runtime.unregister_ordered_source(self.source)
        self.source = None
        return result

    @staticmethod
    def _require_confirmed(result: RuntimeStepResultV1, intent_id: str) -> None:
        receipt = None if result.backend_result is None else result.backend_result.receipt
        if (
            result.observation is None
            or result.decision is None
            or result.report.failure is not None
            or receipt is None
            or receipt.status not in {"executed", "confirmed_local"}
            or dict(result.decision.selected_intents).get("movement") != intent_id
        ):
            raise RuntimeError("unconfirmed external recovery control")

    @staticmethod
    def _task(kind: str, deadline_ns: int, reason: str | None = None) -> TaskIntentV0:
        return TaskIntentV0(
            task_id="external-motion-recovery",
            task_type=kind,
            parameters_json=json.dumps(
                {"reason": reason}, sort_keys=True, separators=(",", ":")
            ),
            success_criteria=(SuccessCriterionV0(
                "external_motion_recovered",
                ComparisonOperatorV0.EQUAL,
                1,
                "boolean",
            ),),
            priority=100,
            deadline_monotonic_ns=deadline_ns,
            interruptible=True,
            max_risk=0.0,
        )
