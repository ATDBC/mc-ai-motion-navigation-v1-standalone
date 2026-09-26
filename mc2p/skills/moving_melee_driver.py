"""C1-B orchestration for pursuit, repeated strikes and explicit death."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.external_motion import (
    DamageKnockbackDetector, ExternalMotionEventV1, ExternalMotionSource,
)
from mc2p.motion_nav.navigation_session import ExternalMotionReentryStatus
from mc2p.motion_nav.external_motion_recovery import ExternalMotionRecoveryController
from mc2p.motion_nav.navigation_session import NavigationSessionPort
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.attack_evidence import (
    AttackAttemptOutcome,
    AttackRetryLedgerV1,
    AttackTaskOutcome,
    advance_attack_retry,
)
from mc2p.skills.combat_aim import combat_aim_angles
from mc2p.skills.engagement_memory import (
    EngagementEventKind, EngagementStateV1, TargetPositionFactV1,
    TargetPositionSource, advance_engagement, event_from_observation,
    resolve_target_position,
)
from mc2p.skills.fixed_melee import CombatTargetV1, MAX_COARSE_ATTACK_DISTANCE_BLOCKS
from mc2p.skills.external_motion_recovery_driver import ExternalMotionRecoveryDriver
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.gaze_controller import GazeController
from mc2p.skills.melee_strike_driver import (
    MeleeStrikeDriver, MeleeStrikeOutcome, TASK_LIMIT_NS,
)
from mc2p.skills.moving_melee import MovingMeleePhase, decide_moving_melee
from mc2p.skills.moving_target import MovingGoalDecisionV1, decide_moving_goal
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver


MAX_REAPPROACHES = 16
MAX_CONFIRMED_STRIKES = 16
MAX_UNCONFIRMED_STRIKES = 2
MAX_REACQUIRE_LOOKS = 20


class _ApproachHandoff(StrEnum):
    STRIKE = "strike"
    FAIL = "fail"
    RECOVER_CADENCE = "recover_cadence"
    CANCEL = "cancel"
    COMPLETE = "complete"
    NEEDS_TASK_DECISION = "needs_task_decision"


@dataclass(frozen=True, slots=True)
class MovingMeleeReportV2:
    state: str
    reason: str
    target_revision: int
    confirmed_hits: int
    attack_submissions: int
    reapproaches: int
    engagement_position_uses: int
    last_health_points: float | None
    external_motion_events: int
    external_recoveries_completed: int
    active_recovery_generation: int | None
    attack_retry: AttackRetryLedgerV1
    task_outcome: AttackTaskOutcome | None
    terminal: bool
    schema_version: str = "mc2p.moving-melee-report.v2"


class MovingMeleeDriver:
    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        navigation_session: NavigationSessionPort,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if (type(runtime) is not PlayerRuntimeV1
                or not isinstance(navigation_session, NavigationSessionPort)):
            raise ContractViolation("moving melee requires Runtime/navigation session")
        if runtime.state is not RuntimeStateV1.READY \
                or type(runtime.observation) is not ObservationSnapshotV3:
            raise ContractViolation("moving melee requires ready V3 Runtime")
        self.runtime = runtime
        self.navigation_session = navigation_session
        self._clock = clock_ns
        self._target: CombatTargetV1 | None = None
        self._deadline_ns = 0
        self._phase = MovingMeleePhase.ACQUIRE_VISIBLE_TARGET
        self._reason = "not_started"
        self._engagement: EngagementStateV1 | None = None
        self._fact: TargetPositionFactV1 | None = None
        self._moving_goal: MovingGoalDecisionV1 | None = None
        self._navigation_goal_revision = 0
        self._confirmed_hits = 0
        self._completed_attack_submissions = 0
        self._attack_attempt_sequence = 0
        self._attack_retry = AttackRetryLedgerV1()
        self._attack_task_outcome: AttackTaskOutcome | None = None
        self._reapproaches = 0
        self._engagement_position_uses = 0
        self._last_engagement_sequence: int | None = None
        self._last_health: float | None = None
        self._external_detector = DamageKnockbackDetector()
        self._external_recovery_control = ExternalMotionRecoveryController()
        self._external_motion_events = 0
        self._external_recoveries_completed = 0
        self._last_external_motion_observation: ObservationSnapshotV3 | None = None
        self._last_external_motion_detected_at_ns: int | None = None
        self._reacquire_source = None
        self._reacquire_sequence = 0
        self._reacquire_attempts = 0
        self._gaze = GazeController()
        self._pending_target: CombatTargetV1 | None = None
        self._pending_approach_handoff: _ApproachHandoff | None = None
        self._pending_approach_reason: str | None = None
        self.approach_driver: RuntimeNavigationDriver | None = None
        self.strike_driver: MeleeStrikeDriver | None = None
        self.recovery_driver: ExternalMotionRecoveryDriver | None = None

    @property
    def report(self) -> MovingMeleeReportV2:
        revision = 0 if self._target is None else self._target.revision
        active_attacks = 0 if self.strike_driver is None else self.strike_driver.report.attack_submissions
        active_recovery = (
            None if self.recovery_driver is None
            else self.recovery_driver.report.active_generation
        )
        return MovingMeleeReportV2(
            self._phase.value, self._reason, revision, self._confirmed_hits,
            self._completed_attack_submissions + active_attacks,
            self._reapproaches, self._engagement_position_uses, self._last_health,
            self._external_motion_events, self._external_recoveries_completed,
            active_recovery, self._attack_retry, self._attack_task_outcome,
            self._phase in {
                MovingMeleePhase.COMPLETE, MovingMeleePhase.FAILED,
                MovingMeleePhase.CANCELLED, MovingMeleePhase.NEEDS_TASK_DECISION,
            },
        )

    @property
    def last_external_motion_observation(self) -> ObservationSnapshotV3 | None:
        """The exact formal sample that produced the newest external event."""
        return self._last_external_motion_observation

    @property
    def last_external_motion_detected_at_ns(self) -> int | None:
        return self._last_external_motion_detected_at_ns

    def _observe(self, kind: EngagementEventKind = EngagementEventKind.OBSERVED) -> None:
        assert self._target is not None and self._engagement is not None
        observation = self.runtime.observation
        if type(observation) is not ObservationSnapshotV3:
            self._phase, self._reason = MovingMeleePhase.FAILED, "observation_unavailable"
            return
        event = None
        if (kind is EngagementEventKind.OBSERVED
                and observation.sequence_id == self._engagement.last_observation_sequence_id):
            pass
        else:
            event = event_from_observation(self._target, observation, kind=kind)
            self._engagement = advance_engagement(self._engagement, event)
        decision_time_ns = self._clock()
        self._fact = resolve_target_position(
            self._engagement, observation, self._target, decision_time_ns,
        )
        if event is not None:
            self.runtime.record_task_event("moving_engagement", {
                "schema_version": "mc2p.moving-engagement.v1",
                "episode_id": observation.episode_id,
                "task_id": self._target.task_id,
                "goal_id": self._target.goal_id,
                "target_revision": self._target.revision,
                "track_id": self._target.track_id,
                "decision_time_ns": decision_time_ns,
                "event": event,
                "state": self._engagement,
                "fact": self._fact,
            })
        if self._fact is not None:
            self._last_health = self._fact.health_points
            if (self._fact.source is TargetPositionSource.ENGAGEMENT
                    and self._fact.observation_sequence_id != self._last_engagement_sequence):
                self._engagement_position_uses += 1
                self._last_engagement_sequence = self._fact.observation_sequence_id

    @staticmethod
    def _within_attack_distance(fact: TargetPositionFactV1) -> bool:
        return math.hypot(fact.relative_position.x, fact.relative_position.z) \
            <= MAX_COARSE_ATTACK_DISTANCE_BLOCKS

    def _detect_external_motion(self, observation: ObservationSnapshotV3):
        assert self._target is not None
        residual = self.navigation_session.motion_residual(
            observation, self.runtime.input_ledger,
        )
        detection = self._external_detector.observe(observation, residual)
        self.runtime.record_task_event("external_motion_detection", {
            "schema_version": "mc2p.external-motion-detection-event.v1",
            "episode_id": observation.episode_id,
            "task_id": self._target.task_id,
            "goal_id": self._target.goal_id,
            "target_revision": self._target.revision,
            "observation_sequence_id": observation.sequence_id,
            "detection": detection,
        })
        return detection

    def _record_melee_decision(
        self,
        decision,
        input_phase: MovingMeleePhase,
        target_dead: bool,
        strike_outcome: MeleeStrikeOutcome | None,
    ) -> None:
        assert self._target is not None
        self.runtime.record_task_event("moving_melee_decision", {
            "schema_version": "mc2p.moving-melee-decision-event.v1",
            "episode_id": self._target.episode_id,
            "task_id": self._target.task_id,
            "goal_id": self._target.goal_id,
            "target_revision": self._target.revision,
            "track_id": self._target.track_id,
            "observation_sequence_id": self.runtime.observation.sequence_id,
            "input_phase": input_phase.value,
            "position_source": None if self._fact is None else self._fact.source.value,
            "within_attack_distance": (
                False if self._fact is None else self._within_attack_distance(self._fact)
            ),
            "target_dead": target_dead,
            "strike_outcome": None if strike_outcome is None else strike_outcome.value,
            "decision": decision,
        })

    def _record_goal_decision(
        self,
        decision: MovingGoalDecisionV1,
        self_position,
        previous: MovingGoalDecisionV1 | None,
        *,
        adopted: bool,
    ) -> None:
        assert self._target is not None and self._fact is not None
        self.runtime.record_task_event("moving_goal_decision", {
            "schema_version": "mc2p.moving-goal-decision-event.v1",
            "episode_id": self._target.episode_id,
            "task_id": self._target.task_id,
            "goal_id": self._target.goal_id,
            "target_revision": self._target.revision,
            "track_id": self._target.track_id,
            "observation_sequence_id": self.runtime.observation.sequence_id,
            "fact": self._fact,
            "self_position": self_position,
            "scope_id": self.navigation_session.report.session_id,
            "deadline_ns": self._deadline_ns,
            "previous": previous,
            "decision": decision,
            "adopted": adopted,
        })

    def _start_strike(self) -> None:
        assert self._target is not None
        if self._reacquire_source is not None:
            raise ContractViolation("reacquire look must be released before striking")
        self._attack_attempt_sequence += 1
        self.strike_driver = MeleeStrikeDriver(
            self.runtime, clock_ns=self._clock, task_deadline_ns=self._deadline_ns,
            attempt_sequence=self._attack_attempt_sequence,
        )
        self.strike_driver.start(self._target, self._clock())
        self._phase = MovingMeleePhase.STRIKING
        self._reason = self.strike_driver.report.reason

    def _refresh_between_actions(self, profile: BehaviorProfileV0,
                                 owner_deadline_ns: int) -> RuntimeStepResultV1:
        """Obtain one current target fact after a child action released control."""
        assert self._target is not None
        now = self._clock()
        deadline = min(owner_deadline_ns, self._deadline_ns, now + 500_000_000)
        task = TaskIntentV0(
            self._target.task_id, "moving_melee_refresh",
            json.dumps({"goal_id": self._target.goal_id,
                        "target_revision": self._target.revision,
                        "track_id": self._target.track_id},
                       sort_keys=True, separators=(",", ":")),
            (SuccessCriterionV0(
                "target_fact_refreshed", ComparisonOperatorV0.GREATER_THAN, 0, "frames",
            ),), 100, deadline, True, 0.5,
        )
        return self.runtime.control_frame(
            task, profile, deadline,
            proposals=(ControlFrameProposalV1(
                observation_request=ObservationRequestV3(
                    "navigation_v1", entity_track_id=self._target.track_id,
                ),
            ),),
        )

    def _release_reacquire(self) -> None:
        if self._reacquire_source is None:
            return
        if self.runtime.state is RuntimeStateV1.READY:
            self.runtime.cancel_source(self._reacquire_source.source_id)
            self.runtime.unregister_ordered_source(self._reacquire_source)
        self._reacquire_source = None
        self._gaze.clear("reacquire_released")

    def _reacquire_vision(self, profile: BehaviorProfileV0,
                          owner_deadline_ns: int) -> RuntimeStepResultV1:
        assert self._target is not None and self._fact is not None
        if self._reacquire_attempts >= MAX_REACQUIRE_LOOKS:
            self._release_reacquire()
            self._phase, self._reason = MovingMeleePhase.FAILED, "reacquire_vision_exhausted"
            return self._refresh_between_actions(profile, owner_deadline_ns)
        if self._reacquire_source is None:
            self._reacquire_source = self.runtime.register_ordered_source(
                "moving-melee-reacquire"
            )
            self._reacquire_sequence = 0
        observation = self.runtime.observation
        relative = self._fact.relative_position
        own = observation.self_state.value
        tracked = observation.tracked_entity.value
        if (own is None or own.eye_height_blocks is None or tracked is None
                or tracked.track_id != self._target.track_id):
            self._release_reacquire()
            self._phase = MovingMeleePhase.FAILED
            self._reason = "reacquire_geometry_unavailable"
            return self._refresh_between_actions(profile, owner_deadline_ns)
        yaw, pitch = combat_aim_angles(
            relative,
            tracked.bounding_box_size,
            own.eye_height_blocks,
        )
        now = self._clock()
        view = project_playground_view(observation, now, observation.controller_clock_id)
        look = self._gaze.command(view, yaw, pitch, now, precise=False)
        self._reacquire_sequence += 1
        intent_id = ordered_intent_id(self._reacquire_source, self._reacquire_sequence)
        deadline = min(owner_deadline_ns, self._deadline_ns, now + 250_000_000)
        envelope = OrderedIntentV1(
            self._reacquire_source, self._reacquire_sequence,
            ActionIntentV1(
                intent_id, self._reacquire_source.source_id, self._target.episode_id,
                observation.sequence_id, ActionPriorityV0.TASK, now, deadline, look=look,
            ),
        )
        result = self.runtime.control_frame(
            TaskIntentV0(
                self._target.task_id, "moving_melee_reacquire", "{}",
                (SuccessCriterionV0("target_visible", ComparisonOperatorV0.EQUAL,
                                    1, "boolean"),),
                100, deadline, True, 0.5,
            ), profile, deadline,
            proposals=(ControlFrameProposalV1(
                (envelope,),
                ObservationRequestV3(
                    "navigation_v1", entity_track_id=self._target.track_id,
                ),
            ),),
        )
        self._reacquire_attempts += 1
        self._observe()
        if self._fact is not None and self._fact.source is TargetPositionSource.VISION:
            self._release_reacquire()
            self._reacquire_attempts = 0
            self._phase, self._reason = MovingMeleePhase.STRIKE_READY, "target_reacquired"
        else:
            self._phase, self._reason = MovingMeleePhase.PURSUING, "turning_to_engagement_target"
        return result

    def _start_approach(self) -> None:
        assert self._target is not None and self._fact is not None
        if self.strike_driver is not None:
            raise ContractViolation("combat source must be released before pursuit")
        if self._reapproaches >= MAX_REAPPROACHES:
            self._phase, self._reason = MovingMeleePhase.FAILED, "reapproach_attempts_exhausted"
            return
        observation = self.runtime.observation
        assert type(observation) is ObservationSnapshotV3
        own = observation.self_state.value
        if own is None:
            self._phase, self._reason = MovingMeleePhase.FAILED, "self_state_unavailable"
            return
        decision = decide_moving_goal(
            self._fact, own.position, self.navigation_session.report.session_id,
            self._deadline_ns, previous=self._moving_goal,
        )
        self._record_goal_decision(
            decision, own.position, self._moving_goal, adopted=True,
        )
        self._moving_goal = decision
        self._navigation_goal_revision += 1
        self.approach_driver = RuntimeNavigationDriver(
            self.runtime, self.navigation_session, clock_ns=self._clock,
            observation_request=ObservationRequestV3(
                "navigation_v1", entity_track_id=self._target.track_id,
            ),
        )
        self.approach_driver.start(
            self._target.goal_id, self._navigation_goal_revision,
            decision.goal_state, self._clock(),
        )
        self._reapproaches += 1
        self._phase, self._reason = MovingMeleePhase.PURSUING, decision.reason

    def start(self, target: CombatTargetV1, now_ns: int) -> None:
        if type(target) is not CombatTargetV1:
            raise ContractViolation("moving melee start requires CombatTargetV1")
        require_nonnegative_int(now_ns, "moving melee start time")
        if self._target is not None:
            raise ContractViolation("moving melee driver already started")
        observation = self.runtime.observation
        if (type(observation) is not ObservationSnapshotV3
                or observation.episode_id != target.episode_id
                or now_ns < observation.received_at_monotonic_ns):
            raise ContractViolation("moving melee target/session mismatch")
        self._target = target
        self._deadline_ns = now_ns + TASK_LIMIT_NS
        self._engagement = EngagementStateV1.for_target(target)
        self._detect_external_motion(observation)
        self._observe()
        decision = decide_moving_melee(
            self._phase,
            position_source=None if self._fact is None else self._fact.source,
            within_attack_distance=(False if self._fact is None
                                    else self._within_attack_distance(self._fact)),
            target_dead=False,
        )
        self._record_melee_decision(decision, self._phase, False, None)
        if decision.phase is MovingMeleePhase.PURSUING:
            self._start_approach()
        elif decision.phase is MovingMeleePhase.STRIKE_READY:
            # The shared strike first obtains an identity-bound interaction
            # observation, so an unqueried target is not treated as absent here.
            self._start_strike()
        else:
            self._phase, self._reason = decision.phase, decision.reason

    def _activate_revised_target(self, target: CombatTargetV1) -> None:
        self._target = target
        self._engagement = EngagementStateV1.for_target(target)
        self._fact = None
        self._moving_goal = None
        self._observe()

    def replace_target(self, target: CombatTargetV1, now_ns: int) -> None:
        if type(target) is not CombatTargetV1:
            raise ContractViolation("moving melee replacement requires CombatTargetV1")
        require_nonnegative_int(now_ns, "moving melee replacement time")
        if self._target is None or self.report.terminal:
            raise ContractViolation("moving melee driver is not replaceable")
        if (target.task_id != self._target.task_id
                or target.goal_id != self._target.goal_id
                or target.episode_id != self._target.episode_id
                or target.track_id != self._target.track_id
                or target.revision <= self._target.revision):
            raise ContractViolation("moving melee replacement identity/revision mismatch")
        if now_ns < self.runtime.observation.received_at_monotonic_ns:
            raise ContractViolation("moving melee replacement time regressed")
        if self.recovery_driver is not None:
            # The body recovery is target-independent and must keep ownership.
            # Rebind only the combat facts used after recovery completes.
            self._activate_revised_target(target)
            self._phase = MovingMeleePhase.RECOVERING_EXTERNAL_MOTION
            self._reason = "target_revised_during_external_recovery"
            return
        if self.strike_driver is not None:
            submitted = self.strike_driver.report.attack_submitted
            self.strike_driver.replace_target(target, now_ns)
            if submitted:
                self._pending_target = target
                self._reason = "target_revised_after_submit"
                return
        self._activate_revised_target(target)
        self._phase, self._reason = (
            (MovingMeleePhase.PURSUING, "target_revised")
            if self.approach_driver is not None
            else (MovingMeleePhase.STRIKING, "target_revised")
        )

    def _release_approach(
        self,
        profile: BehaviorProfileV0,
        reason: str,
        *,
        advance_runtime: bool = True,
        handoff: _ApproachHandoff | None = None,
    ) -> RuntimeStepResultV1 | None:
        assert self.approach_driver is not None
        result = (
            self.approach_driver.stop(profile, reason)
            if advance_runtime
            else None
        )
        if not advance_runtime:
            self.approach_driver.release(reason)
        if self.approach_driver.source is None:
            self.approach_driver = None
        else:
            if handoff is None:
                raise ContractViolation(
                    "nonterminal navigation release requires a typed handoff"
                )
            self._pending_approach_handoff = handoff
            self._pending_approach_reason = reason
        self._observe()
        return result

    def _retire_failed_approach(
        self,
        profile: BehaviorProfileV0,
    ) -> bool:
        """Retire a terminal route before deciding whether pursuit can retry."""
        if (self.approach_driver is None
                or self.approach_driver.state not in {
                    "failed", "cancelled", "stopped", "blocked",
                }):
            return False
        reason = "approach/" + str(self.approach_driver.reason)
        if self.approach_driver.source is not None:
            self._release_approach(
                profile, reason, advance_runtime=False,
            )
        else:
            self.approach_driver = None
        if (self._fact is not None and self._engagement is not None
                and self._engagement.active
                and self._reapproaches < MAX_REAPPROACHES):
            self._reason = reason
            self._start_approach()
        else:
            self._phase, self._reason = MovingMeleePhase.FAILED, reason
        return True

    def _finish_approach_handoff(self) -> None:
        handoff = self._pending_approach_handoff
        reason = self._pending_approach_reason
        self._pending_approach_handoff = None
        self._pending_approach_reason = None
        if handoff is None:
            return
        if handoff is _ApproachHandoff.STRIKE:
            self._start_strike()
        elif handoff is _ApproachHandoff.FAIL:
            self._phase = MovingMeleePhase.FAILED
            self._reason = reason or "approach_handoff_failed"
        elif handoff is _ApproachHandoff.RECOVER_CADENCE:
            self._phase = MovingMeleePhase.RECOVERING_CADENCE
            self._reason = reason or "approach_handoff_complete"
        elif handoff is _ApproachHandoff.CANCEL:
            self._phase = MovingMeleePhase.CANCELLED
            self._reason = "task_cancelled"
        elif handoff is _ApproachHandoff.COMPLETE:
            self._phase = MovingMeleePhase.COMPLETE
            self._reason = reason or "target_dead"
        elif handoff is _ApproachHandoff.NEEDS_TASK_DECISION:
            self._phase = MovingMeleePhase.NEEDS_TASK_DECISION
            self._reason = reason or "task_strategy_required"

    def _adopt_strike_report(self) -> None:
        assert self.strike_driver is not None and self._target is not None
        report = self.strike_driver.report
        self._reason = report.reason
        if report.outcome is MeleeStrikeOutcome.NEEDS_APPROACH:
            self._completed_attack_submissions += report.attack_submissions
            self.strike_driver = None
            self._observe()
            if self._fact is None:
                self._phase, self._reason = MovingMeleePhase.FAILED, "target_unavailable"
            elif self.approach_driver is not None:
                # A composed strike may discover that the target has left
                # range while the existing navigation owner is still valid.
                # Reuse it; the normal pursuit tick will revise its goal from
                # the latest target fact without registering a second owner.
                self._phase = MovingMeleePhase.PURSUING
                self._reason = "target_left_attack_distance"
            else:
                self._start_approach()
            return
        if report.outcome is MeleeStrikeOutcome.IN_PROGRESS:
            self._phase = MovingMeleePhase.STRIKING
            return

        attempt = self.strike_driver.attempt_report
        attempt_outcome = attempt.outcome if attempt.terminal else None
        retry_outcome = None
        if attempt.terminal:
            self._attack_retry, retry_outcome = advance_attack_retry(
                self._attack_retry, attempt,
                gate_limit=MAX_UNCONFIRMED_STRIKES,
                input_limit=MAX_UNCONFIRMED_STRIKES,
                confirmation_limit=MAX_UNCONFIRMED_STRIKES,
            )
            if retry_outcome is not None:
                self._attack_task_outcome = retry_outcome
        self._completed_attack_submissions += report.attack_submissions
        self.strike_driver = None
        if report.outcome is MeleeStrikeOutcome.TARGET_DEAD:
            self._phase, self._reason = MovingMeleePhase.COMPLETE, "target_dead"
        elif report.outcome is MeleeStrikeOutcome.HIT_CONFIRMED:
            self._confirmed_hits += 1
            if self._confirmed_hits > MAX_CONFIRMED_STRIKES:
                self._phase, self._reason = MovingMeleePhase.FAILED, "strike_limit_exhausted"
                return
            self._observe(EngagementEventKind.CONFIRMED_HIT)
            decision = decide_moving_melee(
                MovingMeleePhase.STRIKING,
                position_source=None if self._fact is None else self._fact.source,
                within_attack_distance=(False if self._fact is None
                                        else self._within_attack_distance(self._fact)),
                target_dead=False,
                strike_outcome=MeleeStrikeOutcome.HIT_CONFIRMED,
            )
            self._record_melee_decision(
                decision, MovingMeleePhase.STRIKING, False,
                MeleeStrikeOutcome.HIT_CONFIRMED,
            )
            self._phase, self._reason = decision.phase, decision.reason
        elif (report.outcome is MeleeStrikeOutcome.RETRY_AFTER_TARGET_REVISION
              and self._pending_target is not None):
            target = self._pending_target
            self._pending_target = None
            self._activate_revised_target(target)
            self._phase, self._reason = MovingMeleePhase.RECOVERING_CADENCE, "target_revised"
        elif attempt_outcome in {
            AttackAttemptOutcome.DEFERRED_BY_ARBITRATION,
            AttackAttemptOutcome.GATE_REJECTED,
            AttackAttemptOutcome.INPUT_FAILED,
            AttackAttemptOutcome.CONFIRMATION_TIMEOUT,
        }:
            self._observe()
            tracked = self.runtime.observation.tracked_entity.value
            if (tracked is not None and tracked.track_id == self._target.track_id
                    and tracked.is_dead):
                self._phase, self._reason = MovingMeleePhase.COMPLETE, "target_dead"
            elif retry_outcome is not None:
                self._phase = MovingMeleePhase.NEEDS_TASK_DECISION
                self._reason = {
                    AttackTaskOutcome.GATE_RETRY_EXHAUSTED:
                        "attack_operation_rejected",
                    AttackTaskOutcome.INPUT_RETRY_EXHAUSTED:
                        "attack_input_failure",
                    AttackTaskOutcome.CONFIRMATION_RETRY_EXHAUSTED:
                        "hit_confirmation_uncertain",
                }[retry_outcome]
            elif (attempt_outcome is AttackAttemptOutcome.INPUT_FAILED
                  and self.runtime.state is not RuntimeStateV1.READY):
                self._phase, self._reason = MovingMeleePhase.FAILED, "runtime_failure"
            else:
                self._phase = MovingMeleePhase.RECOVERING_CADENCE
                self._reason = {
                    AttackAttemptOutcome.DEFERRED_BY_ARBITRATION:
                        "attack_deferred_by_arbitration",
                    AttackAttemptOutcome.GATE_REJECTED:
                        "attack_operation_rejected_retryable",
                    AttackAttemptOutcome.INPUT_FAILED:
                        "attack_input_failure_retryable",
                    AttackAttemptOutcome.CONFIRMATION_TIMEOUT:
                        "hit_unconfirmed_retryable",
                }[attempt_outcome]
        elif (report.outcome is MeleeStrikeOutcome.FAILED
              and report.reason in {
                  "target_or_observation_unavailable",
                  "hit_observation_unavailable",
              }):
            # A one-frame visibility gap invalidates this strike attempt, not
            # the engagement.  Return ownership to the moving combat layer so
            # it can use engagement memory to turn back toward the target.
            self._observe()
            if self._engagement.active:
                self._phase = MovingMeleePhase.PURSUING
                self._reason = "target_observation_gap"
            else:
                self._phase = MovingMeleePhase.FAILED
                self._reason = self._engagement.revocation_reason or report.reason
        elif report.outcome is MeleeStrikeOutcome.CANCELLED:
            self._phase, self._reason = MovingMeleePhase.CANCELLED, report.reason
        else:
            self._phase, self._reason = MovingMeleePhase.FAILED, report.reason

    def _retire_approach_after_strike_terminal(self) -> None:
        if self.approach_driver is None:
            return
        terminal_phase, terminal_reason = self._phase, self._reason
        handoff = {
            MovingMeleePhase.COMPLETE: _ApproachHandoff.COMPLETE,
            MovingMeleePhase.NEEDS_TASK_DECISION: _ApproachHandoff.NEEDS_TASK_DECISION,
            MovingMeleePhase.CANCELLED: _ApproachHandoff.CANCEL,
            MovingMeleePhase.FAILED: _ApproachHandoff.FAIL,
        }.get(terminal_phase)
        if handoff is None:
            return
        released = self.approach_driver.release(
            "combat_terminal/" + terminal_reason
        )
        if released:
            self.approach_driver = None
            return
        self._pending_approach_handoff = handoff
        self._pending_approach_reason = terminal_reason
        self._phase = MovingMeleePhase.CANCELLING
        self._reason = "finishing_navigation_after_combat_terminal"

    def _tick_strike(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        assert self.strike_driver is not None
        approach = self.approach_driver
        supplier = (
            None
            if approach is None
            else lambda: approach.prepare_proposals(owner_deadline_ns)
        )
        try:
            result = self.strike_driver.tick(
                profile,
                owner_deadline_ns,
                additional_proposal_supplier=supplier,
            )
        except BaseException:
            if approach is not None and approach.has_prepared_frame:
                approach.discard_prepared()
            raise
        if approach is not None and approach.has_prepared_frame:
            if result is None:
                approach.discard_prepared()
            else:
                approach.adopt_result(result)
                if approach.source is None:
                    self.approach_driver = None
                elif approach.state == "success":
                    approach.release("combat_standoff_reached")
                    self.approach_driver = None
        captured, capture_result = self._capture_external_motion(
            profile, owner_deadline_ns,
        )
        if captured:
            return capture_result if capture_result is not None else result
        if result is not None:
            self._observe()
        self._adopt_strike_report()
        self._retire_approach_after_strike_terminal()
        return result

    def _begin_external_recovery(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
        event: ExternalMotionEventV1,
        *,
        step_already_performed: bool = False,
    ) -> RuntimeStepResultV1 | None:
        assert self._target is not None
        old_movement_source_id = (
            None if self.approach_driver is None or self.approach_driver.source is None
            else self.approach_driver.source.source_id
        )
        retired_source_ids = tuple(source_id for source_id in (
            old_movement_source_id,
            None if self._reacquire_source is None else self._reacquire_source.source_id,
            None if self.strike_driver is None else self.strike_driver.control_source_id,
        ) if source_id is not None)
        self._release_reacquire()
        if self.strike_driver is not None:
            self.strike_driver.interrupt_for_external_motion(
                profile, event.source.value,
            )
        self.recovery_driver = ExternalMotionRecoveryDriver(
            self.runtime,
            task_deadline_ns=self._deadline_ns,
            clock_ns=self._clock,
            observation_request=ObservationRequestV3(
                "navigation_v1", entity_track_id=self._target.track_id,
            ),
            controller=self._external_recovery_control,
            evidence_scope_id=self._target.task_id,
        )
        self.recovery_driver.start(event)
        if self.approach_driver is not None:
            # The recovery source now owns landing/braking. Only after that
            # successor exists may navigation relinquish its body responsibility.
            self.approach_driver.transfer_to_successor(
                "external_motion_recovery_owns_body"
            )
            self.approach_driver = None
        self.runtime.record_task_event("external_motion_recovery", {
            "schema_version": "mc2p.external-motion-recovery-event.v1",
            "episode_id": self._target.episode_id,
            "recovery_scope_id": self._target.task_id,
            "stage": "parent_handoff",
            "observation_sequence_id": self.runtime.observation.sequence_id,
            "event_id": event.event_id,
            "old_movement_source_id": old_movement_source_id,
            "retired_source_ids": retired_source_ids,
            "old_front_invalidated": self.approach_driver is None,
            "recovery_source_id": self.recovery_driver.source.source_id,
        })
        self._external_motion_events += 1
        self._phase = MovingMeleePhase.RECOVERING_EXTERNAL_MOTION
        self._reason = (
            "damage_knockback_captured"
            if event.source is ExternalMotionSource.DAMAGE_KNOCKBACK
            else "external_motion_captured/" + event.source.value
        )
        if step_already_performed:
            return None
        return self._tick_external_recovery(profile, owner_deadline_ns)

    def _continue_navigation_after_external_motion(
        self, event: ExternalMotionEventV1,
    ) -> bool:
        if self.approach_driver is None or self.recovery_driver is not None:
            return False
        reentry = self.navigation_session.external_motion_reentry(
            self.runtime.observation,
        )
        if reentry.status is not ExternalMotionReentryStatus.CONTINUE_NAVIGATION:
            return False
        self._external_motion_events += 1
        self._phase = MovingMeleePhase.PURSUING
        self._reason = reentry.reason
        self.runtime.record_task_event("external_motion_recovery", {
            "schema_version": "mc2p.external-motion-recovery-event.v1",
            "episode_id": self._target.episode_id,
            "recovery_scope_id": self._target.task_id,
            "stage": "navigation_reanchored",
            "observation_sequence_id": self.runtime.observation.sequence_id,
            "movement_tick_id": self.runtime.observation.self_state.value.movement_tick_id,
            "event": event,
            "reason": reentry.reason,
        })
        return True

    def _capture_external_motion(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> tuple[bool, RuntimeStepResultV1 | None]:
        """Consume the newest formal observation before child state advances."""
        detection = self._detect_external_motion(self.runtime.observation)
        if detection.event is None:
            return False, None
        self._last_external_motion_observation = self.runtime.observation
        self._last_external_motion_detected_at_ns = self._clock()
        if self.recovery_driver is None:
            # The child just produced this observation. Preserve its target
            # facts before releasing that child, otherwise engagement history
            # skips the exact frame that carried the damage transition.
            self._observe()
            if self._continue_navigation_after_external_motion(detection.event):
                return True, None
            return True, self._begin_external_recovery(
                profile, owner_deadline_ns, detection.event,
                step_already_performed=True,
            )
        self.recovery_driver.observe_event(detection.event)
        self._external_motion_events += 1
        return True, None

    def _resume_after_external_recovery(self) -> None:
        assert self._target is not None and self._engagement is not None
        if self.strike_driver is not None:
            report = self.strike_driver.report
            if report.terminal:
                self._adopt_strike_report()
                if self._phase in {MovingMeleePhase.COMPLETE, MovingMeleePhase.FAILED}:
                    return
            elif report.outcome is MeleeStrikeOutcome.NEEDS_APPROACH:
                self._completed_attack_submissions += report.attack_submissions
                self.strike_driver = None
            else:
                self._phase, self._reason = MovingMeleePhase.STRIKING, report.reason
                return
        self._observe()
        tracked = self.runtime.observation.tracked_entity.value
        if tracked is not None and tracked.track_id == self._target.track_id and tracked.is_dead:
            self._phase, self._reason = MovingMeleePhase.COMPLETE, "target_dead"
            return
        decision = decide_moving_melee(
            MovingMeleePhase.RECOVERING_EXTERNAL_MOTION,
            position_source=None if self._fact is None else self._fact.source,
            within_attack_distance=(False if self._fact is None
                                    else self._within_attack_distance(self._fact)),
            target_dead=False,
        )
        self._record_melee_decision(
            decision, MovingMeleePhase.RECOVERING_EXTERNAL_MOTION, False, None,
        )
        if decision.phase is MovingMeleePhase.PURSUING:
            self._start_approach()
        elif decision.phase is MovingMeleePhase.STRIKE_READY:
            self._start_strike()
        else:
            self._phase, self._reason = decision.phase, decision.reason

    def _tick_external_recovery(
        self, profile: BehaviorProfileV0, owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        assert self.recovery_driver is not None
        result = self.recovery_driver.tick(profile, owner_deadline_ns)
        if (self.strike_driver is not None
                and self.strike_driver.report.attack_submitted
                and not self.strike_driver.report.terminal):
            self.strike_driver.observe_after_external_motion(self.runtime.observation)
        self._observe()
        recovery_report = self.recovery_driver.report
        if recovery_report.state == "cancelled":
            self._phase, self._reason = (
                MovingMeleePhase.FAILED,
                "external_motion/" + str(recovery_report.reason),
            )
            self.recovery_driver = None
        elif recovery_report.complete:
            self._external_recoveries_completed += 1
            self.runtime.record_task_event("external_motion_recovery", {
                "schema_version": "mc2p.external-motion-recovery-event.v1",
                "episode_id": self._target.episode_id,
                "recovery_scope_id": self._target.task_id,
                "stage": "parent_reanchored",
                "observation_sequence_id": self.runtime.observation.sequence_id,
                "movement_tick_id": self.runtime.observation.self_state.value.movement_tick_id,
                "completed_generation": recovery_report.active_generation,
            })
            self.recovery_driver = None
            if recovery_report.task_limit_reason is not None:
                self._phase = MovingMeleePhase.NEEDS_TASK_DECISION
                self._reason = "external_motion/" + recovery_report.task_limit_reason
            else:
                self._resume_after_external_recovery()
        else:
            self._phase = MovingMeleePhase.RECOVERING_EXTERNAL_MOTION
            self._reason = recovery_report.reason or "external_motion_recovery"
        return result

    def tick(self, profile: BehaviorProfileV0,
             owner_deadline_ns: int) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("moving melee tick requires BehaviorProfileV0")
        if self._target is None or self._engagement is None:
            raise ContractViolation("moving melee driver has not started")
        if self.report.terminal:
            raise ContractViolation("moving melee driver is terminal")
        require_nonnegative_int(owner_deadline_ns, "moving melee owner deadline")

        observation = self.runtime.observation
        assert type(observation) is ObservationSnapshotV3
        if observation.episode_id != self._target.episode_id:
            if self.recovery_driver is not None:
                self.runtime.fail_closed("world_session_changed_during_external_recovery")
                self.recovery_driver = None
            self._phase, self._reason = MovingMeleePhase.FAILED, "world_session_changed"
            return None
        detection = self._detect_external_motion(observation)
        if detection.event is not None:
            self._last_external_motion_observation = observation
            self._last_external_motion_detected_at_ns = self._clock()
            if self.recovery_driver is None:
                if self._continue_navigation_after_external_motion(
                    detection.event,
                ):
                    pass
                else:
                    return self._begin_external_recovery(
                        profile, owner_deadline_ns, detection.event,
                    )
            else:
                self.recovery_driver.observe_event(detection.event)
                self._external_motion_events += 1
        if self.recovery_driver is not None:
            return self._tick_external_recovery(profile, owner_deadline_ns)

        if self.strike_driver is not None and self.approach_driver is not None:
            return self._tick_strike(profile, owner_deadline_ns)

        if self.approach_driver is not None:
            if self._pending_approach_handoff is not None:
                result = self.approach_driver.tick(profile, owner_deadline_ns)
                if self.approach_driver.source is None:
                    self.approach_driver = None
                    self._finish_approach_handoff()
                return result
            self._observe()
            if self._retire_failed_approach(profile):
                return None
            if (self._engagement.active and self._fact is None
                    and self._engagement.awaiting_continuity_reanchor):
                result = self.approach_driver.tick(profile, owner_deadline_ns)
                captured, capture_result = self._capture_external_motion(
                    profile, owner_deadline_ns,
                )
                if captured:
                    return capture_result if capture_result is not None else result
                self._observe()
                self._phase = MovingMeleePhase.PURSUING
                self._reason = (
                    "target_observation_reanchored"
                    if self._fact is not None
                    else "target_observation_gap"
                )
                return result
            if not self._engagement.active or self._fact is None:
                failure_reason = self._engagement.revocation_reason or "target_unavailable"
                result = self._release_approach(
                    profile, failure_reason, handoff=_ApproachHandoff.FAIL,
                )
                if self.approach_driver is None:
                    self._phase, self._reason = MovingMeleePhase.FAILED, failure_reason
                return result
            if (self._fact.source.value == "vision"
                    and self._within_attack_distance(self._fact)):
                self._start_strike()
                return self._tick_strike(profile, owner_deadline_ns)
            observation = self.runtime.observation
            assert type(observation) is ObservationSnapshotV3
            own = observation.self_state.value
            if own is None:
                result = self._release_approach(
                    profile, "self_state_unavailable",
                    handoff=_ApproachHandoff.FAIL,
                )
                if self.approach_driver is None:
                    self._phase, self._reason = (
                        MovingMeleePhase.FAILED, "self_state_unavailable",
                    )
                return result
            # A completed route is no longer replaceable. Consume its success
            # before considering a moving-target goal refresh.
            if self.approach_driver.state == "success":
                result = self._release_approach(profile, "combat_standoff_reached")
                self._phase, self._reason = MovingMeleePhase.RECOVERING_CADENCE, \
                    "standoff_reached_refresh_required"
                return result
            update = decide_moving_goal(
                self._fact, own.position, self.navigation_session.report.session_id,
                self._deadline_ns, previous=self._moving_goal,
            )
            self._record_goal_decision(
                update, own.position, self._moving_goal, adopted=update.changed,
            )
            if update.changed:
                self._navigation_goal_revision += 1
                self.approach_driver.replace_goal(
                    self._target.goal_id, self._navigation_goal_revision,
                    update.goal_state, self._clock(),
                )
                self._moving_goal = update
            result = self.approach_driver.tick(profile, owner_deadline_ns)
            captured, capture_result = self._capture_external_motion(
                profile, owner_deadline_ns,
            )
            if captured:
                return capture_result if capture_result is not None else result
            self._observe()
            if self.approach_driver.state == "success":
                self._release_approach(
                    profile, "combat_standoff_reached", advance_runtime=False,
                )
                self._phase, self._reason = MovingMeleePhase.RECOVERING_CADENCE, \
                    "standoff_reached_refresh_required"
                return result
            self._retire_failed_approach(profile)
            return result

        if self.strike_driver is None:
            if self._reacquire_source is not None:
                if (self._fact is not None
                        and self._fact.source is TargetPositionSource.ENGAGEMENT
                        and not self._within_attack_distance(self._fact)):
                    self._release_reacquire()
                    self._reacquire_attempts = 0
                    self._start_approach()
                    if self._retire_failed_approach(profile):
                        return None
                    if self.approach_driver is not None:
                        return self.approach_driver.tick(
                            profile, owner_deadline_ns,
                        )
                    return None
                return self._reacquire_vision(profile, owner_deadline_ns)
            refresh_result = None
            if (self._engagement.last_observation_sequence_id
                    == self.runtime.observation.sequence_id):
                refresh_result = self._refresh_between_actions(profile, owner_deadline_ns)
                captured, capture_result = self._capture_external_motion(
                    profile, owner_deadline_ns,
                )
                if captured:
                    return capture_result if capture_result is not None else refresh_result
            self._observe()
            if (self._fact is None and self._engagement.active
                    and self._engagement.awaiting_continuity_reanchor):
                self._phase = MovingMeleePhase.PURSUING
                self._reason = "target_observation_gap"
                return refresh_result
            tracked = self.runtime.observation.tracked_entity.value
            if tracked is not None and tracked.track_id == self._target.track_id and tracked.is_dead:
                self._phase, self._reason = MovingMeleePhase.COMPLETE, "target_dead"
                return None
            decision = decide_moving_melee(
                self._phase,
                position_source=None if self._fact is None else self._fact.source,
                within_attack_distance=(False if self._fact is None
                                        else self._within_attack_distance(self._fact)),
                target_dead=False,
            )
            self._record_melee_decision(decision, self._phase, False, None)
            if decision.phase is MovingMeleePhase.PURSUING:
                if (self._fact is not None
                        and self._fact.source is TargetPositionSource.ENGAGEMENT
                        and self._within_attack_distance(self._fact)):
                    return self._reacquire_vision(profile, owner_deadline_ns)
                self._start_approach()
                return refresh_result
            if decision.phase is MovingMeleePhase.STRIKE_READY:
                self._start_strike()
            else:
                self._phase, self._reason = decision.phase, decision.reason
                return refresh_result

        return self._tick_strike(profile, owner_deadline_ns)

    def cancel(self, profile: BehaviorProfileV0,
               reason: str) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0 or type(reason) is not str or not reason.strip():
            raise ContractViolation("moving melee cancel requires profile and reason")
        if self._target is None or self.report.terminal:
            raise ContractViolation("moving melee driver cannot be cancelled")
        result = None
        if self.recovery_driver is not None:
            result = self.recovery_driver.cancel(profile, reason)
            self.recovery_driver = None
        elif self.approach_driver is not None:
            result = self._release_approach(
                profile, reason, handoff=_ApproachHandoff.CANCEL,
            )
        elif self.strike_driver is not None:
            result = self.strike_driver.cancel(profile, reason)
            self._completed_attack_submissions += self.strike_driver.report.attack_submissions
            self.strike_driver = None
        self._release_reacquire()
        if self._engagement is not None:
            self._observe(EngagementEventKind.CANCELLED)
        if self.approach_driver is not None:
            self._phase, self._reason = MovingMeleePhase.CANCELLING, \
                "finishing_navigation_cancellation"
        else:
            self._phase, self._reason = MovingMeleePhase.CANCELLED, "task_cancelled"
        return result
