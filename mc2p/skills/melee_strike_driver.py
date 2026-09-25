"""One visible-target melee strike with exact dispatch and hit confirmation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, AttackEntityV1, LookV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, FieldStatusV0, require_nonnegative_int
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, IntentSourceV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.contracts.observation_v2 import VisibleEntityV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.combat_aim import combat_aim_angles
from mc2p.skills.fixed_melee import (
    CombatTargetV1, FixedMeleeDecisionV1, FixedMeleePhase,
    MAX_COARSE_ATTACK_DISTANCE_BLOCKS, decide_fixed_melee,
)
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.navigation_look import ObservationGate


STEP_LEASE_NS = 250_000_000
TASK_LIMIT_NS = 30_000_000_000
CONFIRMATION_NS = 1_000_000_000
MAX_AIM_ATTEMPTS = 20


class MeleeStrikeOutcome(StrEnum):
    IN_PROGRESS = "in_progress"
    NEEDS_APPROACH = "needs_approach"
    HIT_CONFIRMED = "hit_confirmed"
    TARGET_DEAD = "target_dead"
    UNCONFIRMED = "unconfirmed"
    RETRYABLE_OPERATION_REJECTION = "retryable_operation_rejection"
    RETRY_AFTER_TARGET_REVISION = "retry_after_target_revision"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MeleeStrikeReportV1:
    state: str
    reason: str
    target_revision: int
    attack_submitted: bool
    hit_observed: bool
    attack_submissions: int
    outcome: MeleeStrikeOutcome
    terminal: bool
    schema_version: str = "mc2p.melee-strike-report.v1"


class MeleeStrikeDriver:
    def __init__(self, runtime: PlayerRuntimeV1, *,
                 clock_ns: Callable[[], int] = time.perf_counter_ns,
                 task_deadline_ns: int | None = None) -> None:
        if type(runtime) is not PlayerRuntimeV1:
            raise ContractViolation("melee strike driver requires formal Runtime")
        if runtime.state is not RuntimeStateV1.READY or type(runtime.observation) is not ObservationSnapshotV3:
            raise ContractViolation("melee strike driver requires ready V3 Runtime")
        if task_deadline_ns is not None:
            require_nonnegative_int(task_deadline_ns, "melee strike task deadline")
        self.runtime = runtime
        self._clock = clock_ns
        self._fixed_deadline_ns = task_deadline_ns
        self._target: CombatTargetV1 | None = None
        self._clock_id: str | None = None
        self._task: TaskIntentV0 | None = None
        self._task_deadline_ns = 0
        self._state, self._reason = "ready", "not_started"
        self._phase = FixedMeleePhase.ALIGNING
        self._generation = 0
        self._sequence = 0
        self._combat_source: IntentSourceV1 | None = None
        self._gate = ObservationGate()
        self._attack_submitted = False
        self._attack_submissions = 0
        self._attack_observation_sequence_id: int | None = None
        self._pre_attack_hurt: int | None = None
        self._pre_attack_health: float | None = None
        self._confirmation_deadline_ns: int | None = None
        self._hit_observed = False
        self._aim_attempts = 0
        self._cancel_after_submit = False
        self._target_revised_after_submit = False
        self._external_motion_observation_only = False

    @property
    def report(self) -> MeleeStrikeReportV1:
        revision = 0 if self._target is None else self._target.revision
        if self._state == "needs_approach":
            outcome = MeleeStrikeOutcome.NEEDS_APPROACH
        elif self._state == "complete" and self._reason == "hit_confirmed":
            outcome = MeleeStrikeOutcome.HIT_CONFIRMED
        elif self._state == "complete" and self._reason == "target_dead":
            outcome = MeleeStrikeOutcome.TARGET_DEAD
        elif self._state == "unconfirmed":
            outcome = MeleeStrikeOutcome.UNCONFIRMED
        elif self._state == "operation_rejected":
            outcome = MeleeStrikeOutcome.RETRYABLE_OPERATION_REJECTION
        elif self._state == "failed" and self._reason == "target_revised_after_submit":
            outcome = MeleeStrikeOutcome.RETRY_AFTER_TARGET_REVISION
        elif self._state == "failed":
            outcome = MeleeStrikeOutcome.FAILED
        elif self._state == "cancelled":
            outcome = MeleeStrikeOutcome.CANCELLED
        else:
            outcome = MeleeStrikeOutcome.IN_PROGRESS
        return MeleeStrikeReportV1(
            self._state, self._reason, revision, self._attack_submitted,
            self._hit_observed, self._attack_submissions, outcome,
            self._state in {
                "complete", "unconfirmed", "operation_rejected", "failed", "cancelled",
            },
        )

    @property
    def control_source_id(self) -> str | None:
        return None if self._combat_source is None else self._combat_source.source_id

    @staticmethod
    def _visible_target(observation: ObservationSnapshotV3,
                        track_id: str) -> VisibleEntityV2 | None:
        if (observation.perception.status is not FieldStatusV0.VALID
                or observation.perception.value is None):
            return None
        return next((entity for entity in observation.perception.value.visible_entities
                     if entity.track_id == track_id), None)

    @staticmethod
    def _explicitly_dead(observation: ObservationSnapshotV3, track_id: str) -> bool:
        entity = observation.tracked_entity.value
        return entity is not None and entity.track_id == track_id and entity.is_dead

    @staticmethod
    def _tracked_health(
        observation: ObservationSnapshotV3, track_id: str,
    ) -> float | None:
        entity = observation.tracked_entity.value
        return (
            entity.health_points
            if entity is not None and entity.track_id == track_id
            else None
        )

    def _make_task(self, target: CombatTargetV1) -> TaskIntentV0:
        return TaskIntentV0(
            target.task_id, "fixed_visible_melee",
            json.dumps({
                "goal_id": target.goal_id,
                "target_revision": target.revision,
                "track_id": target.track_id,
            }, sort_keys=True, separators=(",", ":")),
            (SuccessCriterionV0(
                "melee_hit_confirmed", ComparisonOperatorV0.EQUAL, 1, "boolean",
            ),),
            100, self._task_deadline_ns, True, 0.5,
        )

    def start(self, target: CombatTargetV1, now_ns: int) -> None:
        if type(target) is not CombatTargetV1:
            raise ContractViolation("melee strike start requires CombatTargetV1")
        require_nonnegative_int(now_ns, "melee strike start time")
        if self._target is not None or self._state != "ready":
            raise ContractViolation("melee strike driver already started")
        observation = self.runtime.observation
        if (type(observation) is not ObservationSnapshotV3
                or target.episode_id != observation.episode_id
                or now_ns < observation.received_at_monotonic_ns):
            raise ContractViolation("melee strike target/session mismatch")
        deadline = now_ns + TASK_LIMIT_NS if self._fixed_deadline_ns is None else self._fixed_deadline_ns
        if deadline <= now_ns:
            raise ContractViolation("melee strike task deadline expired")
        self._target = target
        self._clock_id = observation.controller_clock_id
        self._task_deadline_ns = deadline
        self._task = self._make_task(target)
        entity = self._visible_target(observation, target.track_id)
        distance = None if entity is None else math.hypot(
            entity.relative_position.x, entity.relative_position.z,
        )
        if distance is not None and distance > MAX_COARSE_ATTACK_DISTANCE_BLOCKS:
            self._state, self._reason = "needs_approach", "outside_stable_attack_distance"
        else:
            self._state, self._reason = "acquiring_interaction", "interaction_observation_required"

    def replace_target(self, target: CombatTargetV1, now_ns: int) -> None:
        if type(target) is not CombatTargetV1:
            raise ContractViolation("melee strike replacement requires CombatTargetV1")
        require_nonnegative_int(now_ns, "melee strike replacement time")
        if self._target is None or self.report.terminal:
            raise ContractViolation("melee strike driver is not replaceable")
        if (target.task_id != self._target.task_id or target.goal_id != self._target.goal_id
                or target.episode_id != self._target.episode_id
                or target.revision <= self._target.revision):
            raise ContractViolation("melee strike replacement identity/revision mismatch")
        if self._attack_submitted:
            self._target_revised_after_submit = True
            self._state, self._reason = "observing_after_submit", "target_revised_after_submit"
            return
        self._drop_combat_source()
        self._gate.clear()
        self._target = target
        self._task = self._make_task(target)
        self._phase = FixedMeleePhase.ALIGNING
        self._state, self._reason = "acquiring_interaction", "interaction_observation_required"

    def _ensure_combat_source(self) -> IntentSourceV1:
        if self._combat_source is None:
            self._combat_source = self.runtime.register_ordered_source("fixed-melee")
        return self._combat_source

    def _drop_combat_source(self) -> None:
        source = self._combat_source
        if source is None:
            return
        if self.runtime.state is RuntimeStateV1.READY:
            self.runtime.cancel_source(source.source_id)
            self.runtime.unregister_ordered_source(source)
        self._combat_source = None

    def _event(self, record_type: str, schema: str, **values) -> None:
        assert self._target is not None
        self.runtime.record_task_event(record_type, {
            "schema_version": schema,
            "episode_id": self._target.episode_id,
            "task_id": self._target.task_id,
            "goal_id": self._target.goal_id,
            "target_revision": self._target.revision,
            "track_id": self._target.track_id,
            "decision_generation": self._generation,
            **values,
        })

    def _record_decision(self, decision: FixedMeleeDecisionV1,
                         decision_time_ns: int) -> None:
        self._event("combat_assessment", "mc2p.combat-assessment.v1",
                    assessment=decision.assessment,
                    decision_time_ns=decision_time_ns,
                    controller_clock_id=self._clock_id,
                    attack_observation_sequence_id=self._attack_observation_sequence_id,
                    pre_attack_hurt_animation_ticks=self._pre_attack_hurt,
                    pre_attack_health_points=self._pre_attack_health,
                    confirmation_deadline_ns=self._confirmation_deadline_ns)
        self._event("combat_candidates", "mc2p.combat-candidates.v1",
                    candidates=decision.candidates)
        self._event("combat_selection", "mc2p.combat-selection.v1",
                    selected_candidate_id=decision.selected_candidate_id,
                    phase=decision.phase)

    def _runtime_step(
        self, profile: BehaviorProfileV0, owner_deadline_ns: int,
        envelope: OrderedIntentV1 | None = None,
        *,
        additional_proposals: tuple[ControlFrameProposalV1, ...] = (),
        additional_proposal_supplier: (
            Callable[[], tuple[ControlFrameProposalV1, ...]] | None
        ) = None,
    ) -> RuntimeStepResultV1:
        assert self._task is not None and self._target is not None
        if additional_proposal_supplier is not None:
            if not callable(additional_proposal_supplier) or additional_proposals:
                raise ContractViolation("control proposal supplier is invalid")
            additional_proposals = additional_proposal_supplier()
        if (type(additional_proposals) is not tuple
                or any(type(item) is not ControlFrameProposalV1
                       for item in additional_proposals)):
            raise ContractViolation("additional control proposals must be typed")
        now_ns = self._clock()
        require_nonnegative_int(owner_deadline_ns, "melee strike owner deadline")
        deadline = min(owner_deadline_ns, self._task_deadline_ns, now_ns + STEP_LEASE_NS)
        if deadline <= now_ns:
            raise ContractViolation("melee strike owner/task deadline expired")
        return self.runtime.control_frame(
            self._task, profile, deadline,
            proposals=additional_proposals + (ControlFrameProposalV1(
                () if envelope is None else (envelope,),
                ObservationRequestV3(
                "interaction_v1", entity_track_id=self._target.track_id,
                ),
            ),),
        )

    def _submit(self, *, look: LookV1 | None = None,
                operation: AttackEntityV1 | None = None) -> tuple[str, OrderedIntentV1]:
        source = self._ensure_combat_source()
        self.runtime.cancel_source(source.source_id)
        self._sequence += 1
        now_ns = self._clock()
        assert self._target is not None
        observation = self.runtime.observation
        assert type(observation) is ObservationSnapshotV3
        intent_id = ordered_intent_id(source, self._sequence)
        intent = ActionIntentV1(
            intent_id, source.source_id, self._target.episode_id,
            observation.sequence_id, ActionPriorityV0.TASK,
            now_ns, min(self._task_deadline_ns, now_ns + STEP_LEASE_NS),
            look=look, operation=operation,
        )
        return intent_id, OrderedIntentV1(source, self._sequence, intent)

    def _finish(self, state: str, reason: str) -> None:
        self._drop_combat_source()
        self._state, self._reason = state, reason

    def interrupt_for_external_motion(
        self, profile: BehaviorProfileV0, reason: str,
    ) -> str:
        if type(profile) is not BehaviorProfileV0 or not isinstance(reason, str) or not reason:
            raise ContractViolation("external motion interruption requires profile and reason")
        if self._target is None or self.report.terminal:
            raise ContractViolation("melee strike cannot be interrupted")
        self._drop_combat_source()
        self._gate.clear()
        self._external_motion_observation_only = self._attack_submitted
        if self._attack_submitted:
            self._state, self._reason = "observing_after_submit", reason
            return "observe_submitted_attack"
        self._state, self._reason = "needs_approach", "external_motion_before_submit"
        return "retry_after_recovery"

    def observe_after_external_motion(self, observation: ObservationSnapshotV3) -> None:
        if self.report.terminal:
            return
        if not self._external_motion_observation_only or not self._attack_submitted:
            raise ContractViolation("strike has no submitted attack to observe")
        if type(observation) is not ObservationSnapshotV3:
            raise ContractViolation("external motion confirmation requires V3 observation")
        assert self._target is not None and self._clock_id is not None
        if (observation.episode_id != self._target.episode_id
                or observation.controller_clock_id != self._clock_id):
            self._finish("failed", "world_session_changed")
            return
        if self._explicitly_dead(observation, self._target.track_id):
            self._finish("complete", "target_dead")
            return
        entity = self._visible_target(observation, self._target.track_id)
        if entity is None:
            if self._clock() >= self._confirmation_deadline_ns:
                self._finish("failed", "confirmation_deadline_reached")
            else:
                self._state, self._reason = (
                    "observing_after_submit", "target_temporarily_unavailable"
                )
            return
        self._generation += 1
        decision_time_ns = self._clock()
        decision = decide_fixed_melee(
            observation,
            target=self._target,
            phase=FixedMeleePhase.CONFIRMING_HIT,
            generation=self._generation,
            now_ns=decision_time_ns,
            controller_clock_id=self._clock_id,
            attack_observation_sequence_id=self._attack_observation_sequence_id,
            pre_attack_hurt_animation_ticks=self._pre_attack_hurt,
            pre_attack_health_points=self._pre_attack_health,
            confirmation_deadline_ns=self._confirmation_deadline_ns,
        )
        self._record_decision(decision, decision_time_ns)
        selected = decision.selected_candidate_id
        self._reason = next(
            item.reason for item in decision.candidates
            if item.candidate_id == selected
        )
        if selected == "complete":
            self._hit_observed = True
            self._finish("complete", "hit_confirmed")
        elif selected == "fail_confirmation_timeout":
            self._finish("unconfirmed", self._reason)
        elif selected.startswith("fail_"):
            self._finish("failed", self._reason)
        else:
            self._state = "observing_after_submit"

    def tick(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
        *,
        additional_proposals: tuple[ControlFrameProposalV1, ...] = (),
        additional_proposal_supplier: (
            Callable[[], tuple[ControlFrameProposalV1, ...]] | None
        ) = None,
    ) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("melee strike tick requires BehaviorProfileV0")
        if self._target is None or self._task is None or self._clock_id is None:
            raise ContractViolation("melee strike driver has not started")
        if self.report.terminal:
            raise ContractViolation("melee strike driver is terminal")
        observation = self.runtime.observation
        assert type(observation) is ObservationSnapshotV3
        if (observation.episode_id != self._target.episode_id
                or observation.controller_clock_id != self._clock_id):
            self._finish("failed", "world_session_changed")
            return None
        if self._explicitly_dead(observation, self._target.track_id):
            self._finish("complete", "target_dead")
            return None
        entity = self._visible_target(observation, self._target.track_id)
        distance = None if entity is None else math.hypot(
            entity.relative_position.x, entity.relative_position.z,
        )
        if (not self._attack_submitted and distance is not None
                and distance > MAX_COARSE_ATTACK_DISTANCE_BLOCKS):
            self._drop_combat_source()
            self._state, self._reason = "needs_approach", "outside_stable_attack_distance"
            return None
        if observation.field_profile != "interaction_v1":
            self._ensure_combat_source()
            result = self._runtime_step(
                profile, owner_deadline_ns,
                additional_proposals=additional_proposals,
                additional_proposal_supplier=additional_proposal_supplier,
            )
            self._state, self._reason = "aligning", "interaction_observation_acquired"
            return result

        self._generation += 1
        phase = FixedMeleePhase.CONFIRMING_HIT if self._attack_submitted else self._phase
        decision_time_ns = self._clock()
        decision = decide_fixed_melee(
            observation, target=self._target, phase=phase,
            generation=self._generation, now_ns=decision_time_ns,
            controller_clock_id=self._clock_id,
            attack_observation_sequence_id=self._attack_observation_sequence_id,
            pre_attack_hurt_animation_ticks=self._pre_attack_hurt,
            pre_attack_health_points=self._pre_attack_health,
            confirmation_deadline_ns=self._confirmation_deadline_ns,
        )
        self._record_decision(decision, decision_time_ns)
        selected = decision.selected_candidate_id
        self._reason = next(item.reason for item in decision.candidates
                            if item.candidate_id == selected)
        if selected == "complete":
            self._hit_observed = True
            if self._cancel_after_submit:
                self._finish("cancelled", "cancelled_after_submit")
            elif self._target_revised_after_submit:
                self._finish("failed", "target_revised_after_submit")
            else:
                self._finish("complete", "hit_confirmed")
            return None
        if selected == "fail_confirmation_timeout":
            self._finish(
                "cancelled" if self._cancel_after_submit else "unconfirmed",
                "cancelled_after_submit" if self._cancel_after_submit else self._reason,
            )
            return None
        if selected.startswith("fail_"):
            self._finish("cancelled" if self._cancel_after_submit else "failed",
                         "cancelled_after_submit" if self._cancel_after_submit else self._reason)
            return None

        intent_id = None
        envelope = None
        if selected == "aim":
            if self._aim_attempts >= MAX_AIM_ATTEMPTS:
                self._finish("failed", "aim_not_confirmed")
                return None
            self._aim_attempts += 1
            entity = self._visible_target(observation, self._target.track_id)
            assert entity is not None
            own = observation.self_state.value
            if own is None or own.eye_height_blocks is None:
                self._finish("failed", "eye_height_not_observed")
                return None
            yaw, pitch = combat_aim_angles(
                entity.relative_position,
                entity.bounding_box_size,
                own.eye_height_blocks,
            )
            view = project_playground_view(observation, self._clock(), self._clock_id)
            look = self._gate.request(view, yaw, pitch, self._clock(),
                                      yaw_tolerance=.5, pitch_tolerance=.5)
            intent_id, envelope = self._submit(look=look)
            self._phase = FixedMeleePhase.ALIGNING
        elif selected == "attack":
            entity = self._visible_target(observation, self._target.track_id)
            assert entity is not None
            self._attack_observation_sequence_id = observation.sequence_id
            self._pre_attack_hurt = entity.hurt_animation_ticks
            self._pre_attack_health = self._tracked_health(
                observation, self._target.track_id,
            )
            self._confirmation_deadline_ns = self._clock() + CONFIRMATION_NS
            intent_id, envelope = self._submit(
                operation=AttackEntityV1(self._target.track_id),
            )
        elif selected == "wait_hurt_clear":
            self._phase = FixedMeleePhase.WAITING_HURT_CLEAR
        elif selected == "wait_cooldown":
            self._phase = FixedMeleePhase.WAITING_COOLDOWN

        result = self._runtime_step(
            profile, owner_deadline_ns, envelope,
            additional_proposals=additional_proposals,
            additional_proposal_supplier=additional_proposal_supplier,
        )
        if result.backend_result is None and result.report.failure is not None:
            self._finish("failed", "runtime_failure")
            return result
        receipt = None if result.backend_result is None else result.backend_result.receipt
        selected_intents = {} if result.decision is None else dict(result.decision.selected_intents)
        if intent_id is not None:
            group = "operation" if selected == "attack" else "look"
            selected_by_arbiter = selected_intents.get(group) == intent_id
            self._event(
                "combat_skill", "mc2p.combat-skill.v1",
                candidate_id=selected, intent_id=intent_id,
                operation=AttackEntityV1(self._target.track_id) if selected == "attack" else None,
                selected_by_arbiter=selected_by_arbiter,
                receipt_status=None if receipt is None else receipt.status,
                receipt_reason=None if receipt is None else receipt.reason,
            )
            if not selected_by_arbiter:
                self._finish("failed", "arbitration_selected_other_or_filtered")
                return result
        if selected == "attack":
            self._attack_submissions += 1
            if receipt is not None and receipt.status == "operation_rejected":
                self._finish(
                    "operation_rejected",
                    "client_rejected/" + receipt.reason,
                )
                return result
            if receipt is None or receipt.status != "pending_confirmation":
                reason = "missing_receipt" if receipt is None else receipt.reason
                self._finish("failed", "client_rejected/" + reason)
                return result
            self._attack_submitted = True
            self._phase = FixedMeleePhase.CONFIRMING_HIT
            self._state, self._reason = "confirming_hit", "attack_dispatched"
        elif selected == "aim":
            self._state = "aligning"
            if result.observation is not None:
                post_view = project_playground_view(result.observation, self._clock(), self._clock_id)
                self._gate.feedback(selected_intents.get("look") == intent_id,
                                    post_view, self._clock())
        else:
            self._state = "waiting"
        return result

    def cancel(self, profile: BehaviorProfileV0,
               reason: str) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0 or type(reason) is not str or not reason.strip():
            raise ContractViolation("melee strike cancel requires profile and reason")
        if self._target is None or self.report.terminal:
            raise ContractViolation("melee strike driver cannot be cancelled")
        self._drop_combat_source()
        if not self._attack_submitted:
            result = self._runtime_step(profile, self._clock() + STEP_LEASE_NS)
            self._state, self._reason = "cancelled", "cancelled_before_submit"
            return result
        self._cancel_after_submit = True
        result = self._runtime_step(profile, self._clock() + STEP_LEASE_NS)
        observation = result.observation
        if type(observation) is ObservationSnapshotV3:
            entity = self._visible_target(observation, self._target.track_id)
            self._hit_observed = bool(entity is not None
                                      and entity.hurt_animation_ticks is not None
                                      and entity.hurt_animation_ticks > 0)
        self._state, self._reason = "cancelled", "cancelled_after_submit"
        return result
