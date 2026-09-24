"""Pure C1-A assessment, candidate enumeration and deterministic selection."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math

from mc2p.contracts.common import (
    ContractViolation, FieldStatusV0, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import VisibleEntityV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode,
)
from mc2p.motion_nav.world_model import Aabb
from mc2p.skills.targeting import confirmed_entity_target


class FixedMeleePhase(StrEnum):
    APPROACHING = "approaching"
    ALIGNING = "aligning"
    WAITING_HURT_CLEAR = "waiting_hurt_clear"
    WAITING_COOLDOWN = "waiting_cooldown"
    READY_TO_ATTACK = "ready_to_attack"
    CONFIRMING_HIT = "confirming_hit"
    COMPLETE = "complete"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    FEASIBLE = "feasible"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


CANDIDATE_IDS = (
    "approach", "aim", "wait_hurt_clear", "wait_cooldown", "attack",
    "wait_hit", "complete", "fail_confirmation_timeout",
    "fail_missing_hurt_observation", "fail_invalid_attack_baseline",
    "fail_target_unavailable",
)
_ACTION_CANDIDATES = frozenset({
    "approach", "aim", "wait_hurt_clear", "wait_cooldown", "attack", "wait_hit", "complete",
})
MAX_COARSE_ATTACK_DISTANCE_BLOCKS = 3.0
COMBAT_GOAL_RADIUS_BLOCKS = .5
COMBAT_STAND_DISTANCE_BLOCKS = 2.2


@dataclass(frozen=True, slots=True)
class CombatTargetV1:
    task_id: str
    goal_id: str
    revision: int
    episode_id: str
    track_id: str
    schema_version: str = field(default="mc2p.combat-target.v1", init=False)

    def __post_init__(self) -> None:
        for name in ("task_id", "goal_id", "episode_id", "track_id"):
            require_identifier(getattr(self, name), name)
            if len(getattr(self, name)) > 128:
                raise ContractViolation(f"{name} exceeds 128 characters")
        require_nonnegative_int(self.revision, "target revision")


@dataclass(frozen=True, slots=True)
class MeleeAssessmentV1:
    decision_generation: int
    observation_sequence_id: int
    target_revision: int
    target_visible: bool
    horizontal_distance_blocks: float | None
    coarse_surface_distance_blocks: float | None
    target_hit_distance_blocks: float | None
    targeting_confirmed: bool
    attack_cooldown: float | None
    hurt_animation_ticks: int | None
    phase: FixedMeleePhase
    schema_version: str = field(default="mc2p.combat-assessment.v1", init=False)


@dataclass(frozen=True, slots=True)
class MeleeCandidateV1:
    candidate_id: str
    status: CandidateStatus
    reason: str
    schema_version: str = field(default="mc2p.combat-candidate.v1", init=False)


@dataclass(frozen=True, slots=True)
class FixedMeleeDecisionV1:
    generation: int
    phase: FixedMeleePhase
    assessment: MeleeAssessmentV1
    candidates: tuple[MeleeCandidateV1, ...]
    selected_candidate_id: str
    schema_version: str = field(default="mc2p.fixed-melee-decision.v1", init=False)


def stable_attack_position(
    self_position: Vec3V0,
    target_relative_position: Vec3V0,
    stand_distance_blocks: float = 2.5,
) -> Vec3V0:
    if type(self_position) is not Vec3V0 or type(target_relative_position) is not Vec3V0:
        raise ContractViolation("stable attack position requires typed vectors")
    if (type(stand_distance_blocks) not in (int, float) or isinstance(stand_distance_blocks, bool)
            or not math.isfinite(stand_distance_blocks) or stand_distance_blocks <= 0):
        raise ContractViolation("stable attack distance must be positive and finite")
    length = math.hypot(target_relative_position.x, target_relative_position.z)
    if length == 0:
        raise ContractViolation("stable attack direction has zero horizontal length")
    target_x = self_position.x + target_relative_position.x
    target_z = self_position.z + target_relative_position.z
    return Vec3V0(
        target_x - target_relative_position.x / length * stand_distance_blocks,
        self_position.y,
        target_z - target_relative_position.z / length * stand_distance_blocks,
    )


def combat_standoff_goal_state(
    self_position: Vec3V0,
    target_relative_position: Vec3V0,
) -> tuple[Vec3V0, GoalState]:
    """Return one stand point and a planner goal that stays inside melee reach."""
    stand = stable_attack_position(
        self_position, target_relative_position, COMBAT_STAND_DISTANCE_BLOCKS,
    )
    radius = COMBAT_GOAL_RADIUS_BLOCKS
    return stand, GoalState(
        Aabb(
            stand.x - radius, stand.y - .1, stand.z - radius,
            stand.x + radius, stand.y + .1, stand.z + radius,
        ),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _candidate_set(selected: str, reason: str) -> tuple[MeleeCandidateV1, ...]:
    if selected not in CANDIDATE_IDS:
        raise ContractViolation("unknown fixed melee candidate")
    result = []
    for candidate_id in CANDIDATE_IDS:
        if candidate_id == selected:
            status, candidate_reason = CandidateStatus.FEASIBLE, reason
        elif candidate_id in _ACTION_CANDIDATES:
            status, candidate_reason = CandidateStatus.BLOCKED, "current_preconditions_not_met"
        else:
            status, candidate_reason = CandidateStatus.NOT_APPLICABLE, "failure_condition_not_present"
        result.append(MeleeCandidateV1(candidate_id, status, candidate_reason))
    return tuple(result)


def _visible_target(observation: ObservationSnapshotV3, track_id: str) -> VisibleEntityV2 | None:
    if (observation.perception.status is not FieldStatusV0.VALID
            or observation.perception.value is None):
        return None
    return next((entity for entity in observation.perception.value.visible_entities
                 if entity.track_id == track_id), None)


def decide_fixed_melee(
    observation: ObservationSnapshotV3,
    *,
    target: CombatTargetV1,
    phase: FixedMeleePhase,
    generation: int,
    now_ns: int,
    controller_clock_id: str,
    attack_observation_sequence_id: int | None = None,
    pre_attack_hurt_animation_ticks: int | None = None,
    confirmation_deadline_ns: int | None = None,
) -> FixedMeleeDecisionV1:
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("fixed melee requires exact ObservationSnapshotV3")
    if type(target) is not CombatTargetV1 or type(phase) is not FixedMeleePhase:
        raise ContractViolation("fixed melee requires typed target and phase")
    require_nonnegative_int(generation, "decision generation")
    require_nonnegative_int(now_ns, "decision time")
    require_identifier(controller_clock_id, "decision controller clock")

    identity_valid = (
        observation.episode_id == target.episode_id
        and observation.controller_clock_id == controller_clock_id
        and not observation.privileged_fields_present
        and observation.received_at_monotonic_ns <= now_ns
        and 0 <= now_ns - observation.request_started_at_monotonic_ns <= 500_000_000
        and observation.self_state.status is FieldStatusV0.VALID
    )
    entity = _visible_target(observation, target.track_id) if identity_valid else None
    distance = None if entity is None else math.hypot(
        entity.relative_position.x, entity.relative_position.z,
    )
    surface_distance = None if entity is None else math.hypot(
        max(abs(entity.relative_position.x) - entity.bounding_box_size.x / 2, 0.0),
        max(abs(entity.relative_position.z) - entity.bounding_box_size.z / 2, 0.0),
    )
    aligned = identity_valid and confirmed_entity_target(
        observation, entity_ref=target.track_id, now_ns=now_ns,
        controller_clock_id=controller_clock_id,
    )
    target_hit_distance = (
        observation.targeting.value.distance_blocks if aligned else None
    )
    self_state = observation.self_state.value if identity_valid else None
    cooldown = None if self_state is None else self_state.attack_cooldown
    hurt = None if entity is None else entity.hurt_animation_ticks
    assessment = MeleeAssessmentV1(
        generation, observation.sequence_id, target.revision, entity is not None,
        distance, surface_distance, target_hit_distance, aligned, cooldown, hurt, phase,
    )
    reach_distance = target_hit_distance if aligned else surface_distance

    selected, reason = "fail_target_unavailable", "target_or_observation_unavailable"
    if entity is not None and phase is FixedMeleePhase.CONFIRMING_HIT:
        for value, name in (
            (attack_observation_sequence_id, "attack observation sequence"),
            (pre_attack_hurt_animation_ticks, "pre-attack hurt animation"),
            (confirmation_deadline_ns, "confirmation deadline"),
        ):
            require_nonnegative_int(value, name)
        if hurt is None:
            selected, reason = "fail_missing_hurt_observation", "hurt_animation_not_observed"
        elif (observation.sequence_id > attack_observation_sequence_id
                and hurt > pre_attack_hurt_animation_ticks
                and observation.received_at_monotonic_ns <= confirmation_deadline_ns):
            selected, reason = "complete", "new_hurt_animation_confirmed"
        elif now_ns >= confirmation_deadline_ns:
            selected, reason = "fail_confirmation_timeout", "confirmation_deadline_reached"
        else:
            selected, reason = "wait_hit", "awaiting_later_hurt_observation"
    elif entity is not None and phase is FixedMeleePhase.COMPLETE:
        selected, reason = "complete", "already_complete"
    elif (entity is not None and reach_distance is not None
          and reach_distance > MAX_COARSE_ATTACK_DISTANCE_BLOCKS):
        selected, reason = "approach", "outside_stable_attack_distance"
    elif entity is not None and not aligned:
        selected, reason = "aim", "exact_entity_not_under_crosshair"
    elif entity is not None and hurt is None:
        selected, reason = "fail_missing_hurt_observation", "hurt_animation_not_observed"
    elif entity is not None and cooldown is not None and cooldown < 1.0:
        selected, reason = "wait_cooldown", "attack_cooldown_not_full"
    elif entity is not None:
        selected, reason = "attack", "all_attack_preconditions_met"

    return FixedMeleeDecisionV1(
        generation, phase, assessment, _candidate_set(selected, reason), selected,
    )
