"""Pure phase selection for one bounded moving-target melee task."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mc2p.contracts.common import ContractViolation
from mc2p.skills.engagement_memory import TargetPositionSource


class MovingMeleePhase(StrEnum):
    ACQUIRE_VISIBLE_TARGET = "acquire_visible_target"
    PURSUING = "pursuing"
    STRIKE_READY = "strike_ready"
    STRIKING = "striking"
    RECOVERING_CADENCE = "recovering_cadence"
    RECOVERING_EXTERNAL_MOTION = "recovering_external_motion"
    CONFIRMING_DEATH = "confirming_death"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MovingMeleeDecisionV1:
    phase: MovingMeleePhase
    reason: str
    terminal: bool
    schema_version: str = field(default="mc2p.moving-melee-decision.v1", init=False)


def decide_moving_melee(
    phase: MovingMeleePhase,
    *,
    position_source: TargetPositionSource | None,
    within_attack_distance: bool,
    target_dead: bool,
    strike_reason: str | None = None,
) -> MovingMeleeDecisionV1:
    if type(phase) is not MovingMeleePhase:
        raise ContractViolation("moving melee requires a typed phase")
    if (position_source is not None and type(position_source) is not TargetPositionSource
            or type(within_attack_distance) is not bool or type(target_dead) is not bool
            or strike_reason is not None and type(strike_reason) is not str):
        raise ContractViolation("moving melee facts are invalid")
    if phase is MovingMeleePhase.COMPLETE:
        return MovingMeleeDecisionV1(phase, "target_dead", True)
    if phase is MovingMeleePhase.FAILED:
        return MovingMeleeDecisionV1(phase, strike_reason or "task_failed", True)
    if phase is MovingMeleePhase.CANCELLED:
        return MovingMeleeDecisionV1(phase, strike_reason or "task_cancelled", True)
    if target_dead:
        return MovingMeleeDecisionV1(MovingMeleePhase.COMPLETE, "target_dead", True)
    if strike_reason == "hit_confirmed":
        return MovingMeleeDecisionV1(
            MovingMeleePhase.RECOVERING_CADENCE, "target_hit_but_alive", False,
        )
    if position_source is None:
        return MovingMeleeDecisionV1(
            MovingMeleePhase.FAILED, "target_unavailable", True,
        )
    if not within_attack_distance:
        return MovingMeleeDecisionV1(
            MovingMeleePhase.PURSUING, "outside_stable_attack_distance", False,
        )
    if position_source is not TargetPositionSource.VISION:
        return MovingMeleeDecisionV1(
            MovingMeleePhase.PURSUING, "direct_vision_required", False,
        )
    return MovingMeleeDecisionV1(
        MovingMeleePhase.STRIKE_READY, "visible_target_in_attack_distance", False,
    )
