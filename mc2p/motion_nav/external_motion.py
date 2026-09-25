"""Separate damage facts from motion that the robot's own inputs cannot explain."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.common import (
    ContractViolation,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult,
    MotionResidualStatus,
)


class ExternalMotionSource(StrEnum):
    DAMAGE_KNOCKBACK = "damage_knockback"
    DAMAGE_WITH_UNVERIFIED_MOTION = "damage_with_unverified_motion"
    UNATTRIBUTED_EXTERNAL_MOTION = "unattributed_external_motion"


@dataclass(frozen=True, slots=True)
class DamageFactV1:
    fact_id: str
    generation: int
    episode_id: str
    observation_sequence_id: int
    movement_tick_id: int
    health_delta_points: float
    absorption_delta_points: float
    previous_position: Vec3V0
    position: Vec3V0
    previous_velocity: Vec3V0
    velocity: Vec3V0
    was_on_ground: bool
    is_on_ground: bool

    def __post_init__(self) -> None:
        require_identifier(self.fact_id, "damage fact id")
        require_identifier(self.episode_id, "damage fact episode id")
        require_nonnegative_int(self.generation, "damage fact generation")
        require_nonnegative_int(
            self.observation_sequence_id, "damage fact observation sequence",
        )
        require_nonnegative_int(self.movement_tick_id, "damage fact movement tick")
        require_finite(self.health_delta_points, "damage health delta")
        require_finite(self.absorption_delta_points, "damage absorption delta")
        if self.generation == 0 or (
            self.health_delta_points + self.absorption_delta_points
        ) >= 0:
            raise ContractViolation("damage fact requires a net health or absorption loss")
        for value in (
            self.previous_position, self.position,
            self.previous_velocity, self.velocity,
        ):
            if type(value) is not Vec3V0:
                raise ContractViolation("damage fact vectors must be Vec3V0")
        if type(self.was_on_ground) is not bool or type(self.is_on_ground) is not bool:
            raise ContractViolation("damage fact ground values must be bool")

    @property
    def total_damage_points(self) -> float:
        return -(self.health_delta_points + self.absorption_delta_points)


@dataclass(frozen=True, slots=True)
class DamageFactDetection:
    fact: DamageFactV1 | None
    reason: str

    def __post_init__(self) -> None:
        if self.fact is not None and type(self.fact) is not DamageFactV1:
            raise ContractViolation("damage detection fact must be typed")
        require_identifier(self.reason, "damage detection reason")


@dataclass(frozen=True, slots=True)
class ExternalMotionEventV1:
    event_id: str
    generation: int
    episode_id: str
    observation_sequence_id: int
    movement_tick_id: int
    source: ExternalMotionSource
    health_delta_points: float
    previous_position: Vec3V0
    position: Vec3V0
    previous_velocity: Vec3V0
    velocity: Vec3V0
    was_on_ground: bool
    is_on_ground: bool
    absorption_delta_points: float = 0.0
    position_residual_blocks: float | None = None
    velocity_residual_blocks_per_tick: float | None = None

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "external motion event id")
        require_identifier(self.episode_id, "external motion episode id")
        require_nonnegative_int(self.generation, "external motion generation")
        if self.generation == 0:
            raise ContractViolation("external motion generation must be positive")
        require_nonnegative_int(
            self.observation_sequence_id, "external motion observation sequence",
        )
        require_nonnegative_int(self.movement_tick_id, "external motion movement tick")
        if type(self.source) is not ExternalMotionSource:
            raise ContractViolation("external motion source must be typed")
        require_finite(self.health_delta_points, "external motion health delta")
        require_finite(self.absorption_delta_points, "external motion absorption delta")
        total_delta = self.health_delta_points + self.absorption_delta_points
        if self.source in {
            ExternalMotionSource.DAMAGE_KNOCKBACK,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        } and total_delta >= 0:
            raise ContractViolation("damage external motion requires a net damage fact")
        if (self.source is ExternalMotionSource.UNATTRIBUTED_EXTERNAL_MOTION
                and total_delta != 0):
            raise ContractViolation("unattributed external motion cannot claim damage")
        for value in (
            self.previous_position, self.position,
            self.previous_velocity, self.velocity,
        ):
            if type(value) is not Vec3V0:
                raise ContractViolation("external motion vectors must be Vec3V0")
        if type(self.was_on_ground) is not bool or type(self.is_on_ground) is not bool:
            raise ContractViolation("external motion ground facts must be bool")
        for value in (
            self.position_residual_blocks,
            self.velocity_residual_blocks_per_tick,
        ):
            if value is not None and (
                type(value) not in (int, float) or value < 0
            ):
                raise ContractViolation("external motion residual must be nonnegative")


@dataclass(frozen=True, slots=True)
class ExternalMotionDetection:
    event: ExternalMotionEventV1 | None
    reason: str
    damage_fact: DamageFactV1 | None = None
    motion_residual: MotionResidualResult | None = None

    def __post_init__(self) -> None:
        if self.event is not None and type(self.event) is not ExternalMotionEventV1:
            raise ContractViolation("external motion detection event must be typed")
        if self.damage_fact is not None and type(self.damage_fact) is not DamageFactV1:
            raise ContractViolation("external motion damage fact must be typed")
        if (self.motion_residual is not None
                and type(self.motion_residual) is not MotionResidualResult):
            raise ContractViolation("external motion residual must be typed")
        require_identifier(self.reason, "external motion detection reason")


class DamageFactDetector:
    """Deduplicate self-damage without claiming that damage moved the body."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._episode_id: str | None = None
        self._last_sequence_id: int | None = None
        self._baseline: ObservationSnapshotV3 | None = None
        self._pending_damage_baseline: ObservationSnapshotV3 | None = None
        self._generation = 0

    def observe(self, snapshot: ObservationSnapshotV3) -> DamageFactDetection:
        if type(snapshot) is not ObservationSnapshotV3:
            raise ContractViolation("damage fact detector requires V3 observation")
        if self._episode_id is None:
            self._episode_id = snapshot.episode_id
            self._last_sequence_id = snapshot.sequence_id
            self._baseline = snapshot if self._has_motion_evidence(snapshot) else None
            return DamageFactDetection(
                None,
                "baseline_established" if self._baseline is not None
                else "motion_evidence_unavailable",
            )
        if snapshot.episode_id != self._episode_id:
            self._episode_id = snapshot.episode_id
            self._last_sequence_id = snapshot.sequence_id
            self._baseline = snapshot if self._has_motion_evidence(snapshot) else None
            self._pending_damage_baseline = None
            self._generation = 0
            return DamageFactDetection(None, "session_reanchored")
        if snapshot.sequence_id == self._last_sequence_id:
            return DamageFactDetection(None, "duplicate_observation")
        if snapshot.sequence_id < self._last_sequence_id:
            raise ContractViolation("damage observation sequence moved backward")
        self._last_sequence_id = snapshot.sequence_id
        if not self._has_motion_evidence(snapshot):
            self._baseline = None
            self._pending_damage_baseline = None
            return DamageFactDetection(None, "motion_evidence_unavailable")
        if self._baseline is None:
            self._baseline = snapshot
            return DamageFactDetection(None, "baseline_established")

        previous = self._baseline
        previous_tick = previous.self_state.value.movement_tick_id
        current_tick = snapshot.self_state.value.movement_tick_id
        if current_tick < previous_tick:
            raise ContractViolation("damage movement tick moved backward")
        if current_tick == previous_tick:
            return DamageFactDetection(None, "movement_tick_not_advanced")

        current_own = snapshot.self_state.value
        pending = self._pending_damage_baseline
        if pending is not None:
            if current_own.hurt_animation_ticks > 0:
                health_delta, absorption_delta = self._damage_delta(pending, snapshot)
                self._baseline = snapshot
                if health_delta + absorption_delta < 0:
                    self._pending_damage_baseline = None
                    return self._confirmed_fact(
                        pending, snapshot, health_delta, absorption_delta,
                    )
                return DamageFactDetection(None, "damage_amount_pending")
            self._pending_damage_baseline = None

        previous_own = previous.self_state.value
        self._baseline = snapshot
        if not (
            previous_own.hurt_animation_ticks == 0
            and current_own.hurt_animation_ticks > 0
        ):
            return DamageFactDetection(None, "no_damage_transition")
        health_delta, absorption_delta = self._damage_delta(previous, snapshot)
        if health_delta + absorption_delta >= 0:
            self._pending_damage_baseline = previous
            return DamageFactDetection(None, "damage_amount_pending")
        return self._confirmed_fact(
            previous, snapshot, health_delta, absorption_delta,
        )

    @staticmethod
    def _damage_delta(
        previous: ObservationSnapshotV3, current: ObservationSnapshotV3,
    ) -> tuple[float, float]:
        before = previous.self_state.value
        after = current.self_state.value
        return (
            after.health_points - before.health_points,
            after.absorption_points - before.absorption_points,
        )

    def _confirmed_fact(
        self,
        previous: ObservationSnapshotV3,
        snapshot: ObservationSnapshotV3,
        health_delta: float,
        absorption_delta: float,
    ) -> DamageFactDetection:
        previous_own = previous.self_state.value
        current_own = snapshot.self_state.value
        current_tick = current_own.movement_tick_id
        self._generation += 1
        fact = DamageFactV1(
            f"damage/{snapshot.episode_id}/{current_tick}/{self._generation}",
            self._generation, snapshot.episode_id, snapshot.sequence_id,
            current_tick, health_delta, absorption_delta,
            previous_own.position, current_own.position,
            previous_own.velocity, current_own.velocity,
            previous_own.is_on_ground, current_own.is_on_ground,
        )
        return DamageFactDetection(fact, "damage_confirmed")

    @staticmethod
    def _has_motion_evidence(snapshot: ObservationSnapshotV3) -> bool:
        own = snapshot.self_state.value
        return (
            own is not None
            and own.hurt_animation_ticks is not None
            and own.movement_tick_id is not None
        )


class DamageKnockbackDetector:
    """Join independent damage and motion evidence into external-motion events."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._damage = DamageFactDetector()
        self._generation = 0
        self._previous_snapshot: ObservationSnapshotV3 | None = None
        self._pending_residual: MotionResidualResult | None = None
        self._pending_damage: DamageFactV1 | None = None

    def observe(
        self,
        snapshot: ObservationSnapshotV3,
        motion_residual: MotionResidualResult | None = None,
    ) -> ExternalMotionDetection:
        if (motion_residual is not None
                and type(motion_residual) is not MotionResidualResult):
            raise ContractViolation("external motion requires a typed residual")
        previous = self._previous_snapshot
        damage = self._damage.observe(snapshot)
        if damage.reason in {"session_reanchored", "motion_evidence_unavailable"}:
            self._generation = 0
            self._pending_residual = None
            self._pending_damage = None
        if damage.reason == "duplicate_observation":
            return ExternalMotionDetection(None, damage.reason)
        self._previous_snapshot = (
            snapshot if DamageFactDetector._has_motion_evidence(snapshot) else None
        )
        if damage.reason in {
            "baseline_established", "session_reanchored",
            "motion_evidence_unavailable", "movement_tick_not_advanced",
        }:
            return ExternalMotionDetection(None, damage.reason, damage.fact, motion_residual)

        usable_residual = motion_residual
        if damage.reason == "damage_amount_pending":
            if (motion_residual is not None
                    and motion_residual.status is MotionResidualStatus.DEVIATION):
                self._pending_residual = motion_residual
            return ExternalMotionDetection(
                None, "damage_amount_pending", None, motion_residual,
            )
        if damage.fact is not None and self._pending_residual is not None:
            usable_residual = self._pending_residual
            self._pending_residual = None
        elif self._pending_residual is not None:
            # A hurt-animation transition can precede the authoritative health
            # update by one observation.  If that transition clears without a
            # damage fact, its motion evidence must not be reused by a later,
            # unrelated damage event.
            self._pending_residual = None

        if damage.fact is not None:
            if self._residual_permanently_unavailable(usable_residual):
                self._pending_damage = None
                event = self._unverified_damage_event(damage.fact)
                return ExternalMotionDetection(
                    event, "damage_motion_unverified",
                    damage.fact, usable_residual,
                )
            if not self._residual_is_complete(usable_residual):
                self._pending_damage = damage.fact
                return ExternalMotionDetection(
                    None, "motion_residual_unavailable",
                    damage.fact, usable_residual,
                )
            self._pending_damage = None
        elif self._pending_damage is not None:
            pending = self._pending_damage
            if self._residual_permanently_unavailable(usable_residual):
                self._pending_damage = None
                event = self._unverified_damage_event(pending)
                return ExternalMotionDetection(
                    event, "damage_motion_unverified",
                    pending, usable_residual,
                )
            if self._residual_covers_damage(usable_residual, pending):
                self._pending_damage = None
                if usable_residual.status is MotionResidualStatus.DEVIATION:
                    event = self._event_from_damage(pending, usable_residual)
                    return ExternalMotionDetection(
                        event, "damage_knockback_confirmed",
                        pending, usable_residual,
                    )
                return ExternalMotionDetection(
                    None, "damage_without_motion_residual",
                    pending, usable_residual,
                )
            if self._residual_is_complete(usable_residual):
                # This replay starts after the damage tick.  It can still prove
                # unrelated external motion, but cannot attribute that motion
                # to the older damage fact.
                self._pending_damage = None
            else:
                current = snapshot.self_state.value
                if (current is not None and current.movement_tick_id is not None
                        and current.movement_tick_id - pending.movement_tick_id <= 8):
                    return ExternalMotionDetection(
                        None, "motion_residual_unavailable",
                        pending, usable_residual,
                    )
                if current is None or current.movement_tick_id is None:
                    return ExternalMotionDetection(
                        None, "motion_residual_unavailable",
                        pending, usable_residual,
                    )
                self._pending_damage = None
                event = self._unverified_damage_event(pending)
                return ExternalMotionDetection(
                    event, "damage_motion_unverified_timeout",
                    pending, usable_residual,
                )

        if usable_residual is None or usable_residual.status not in {
            MotionResidualStatus.MATCHED, MotionResidualStatus.DEVIATION,
        }:
            return ExternalMotionDetection(
                None, "motion_residual_unavailable", damage.fact, usable_residual,
            )
        if usable_residual.status is MotionResidualStatus.MATCHED:
            return ExternalMotionDetection(
                None,
                "damage_without_motion_residual" if damage.fact is not None
                else "motion_matches_prediction",
                damage.fact, usable_residual,
            )
        if damage.fact is not None:
            event = self._event_from_damage(damage.fact, usable_residual)
            return ExternalMotionDetection(
                event, "damage_knockback_confirmed", damage.fact, usable_residual,
            )
        if previous is None:
            return ExternalMotionDetection(
                None, "motion_baseline_unavailable", None, usable_residual,
            )
        event = self._unattributed_event(previous, snapshot, usable_residual)
        return ExternalMotionDetection(
            event, "unattributed_external_motion_confirmed", None, usable_residual,
        )

    @staticmethod
    def _residual_is_complete(
        residual: MotionResidualResult | None,
    ) -> bool:
        return residual is not None and residual.status in {
            MotionResidualStatus.MATCHED,
            MotionResidualStatus.DEVIATION,
        }

    @staticmethod
    def _residual_permanently_unavailable(
        residual: MotionResidualResult | None,
    ) -> bool:
        return residual is not None and residual.status in {
            MotionResidualStatus.UNSUPPORTED,
            MotionResidualStatus.INVALID_INPUT,
        }

    @classmethod
    def _residual_covers_damage(
        cls,
        residual: MotionResidualResult | None,
        damage: DamageFactV1,
    ) -> bool:
        return (
            cls._residual_is_complete(residual)
            and residual.anchor_tick < damage.movement_tick_id
            <= residual.observed_tick
        )

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def _event_from_damage(
        self, fact: DamageFactV1, residual: MotionResidualResult,
    ) -> ExternalMotionEventV1:
        generation = self._next_generation()
        return ExternalMotionEventV1(
            f"external/{fact.episode_id}/{fact.movement_tick_id}/{generation}",
            generation, fact.episode_id, fact.observation_sequence_id,
            fact.movement_tick_id, ExternalMotionSource.DAMAGE_KNOCKBACK,
            fact.health_delta_points, fact.previous_position, fact.position,
            fact.previous_velocity, fact.velocity,
            fact.was_on_ground, fact.is_on_ground,
            fact.absorption_delta_points,
            residual.position_error_blocks,
            residual.velocity_error_blocks_per_tick,
        )

    def _unverified_damage_event(
        self, fact: DamageFactV1,
    ) -> ExternalMotionEventV1:
        """Invalidate the old route without pretending knockback was measured."""
        generation = self._next_generation()
        return ExternalMotionEventV1(
            f"external/{fact.episode_id}/{fact.movement_tick_id}/{generation}",
            generation, fact.episode_id, fact.observation_sequence_id,
            fact.movement_tick_id,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
            fact.health_delta_points, fact.previous_position, fact.position,
            fact.previous_velocity, fact.velocity,
            fact.was_on_ground, fact.is_on_ground,
            fact.absorption_delta_points,
        )

    def _unattributed_event(
        self,
        previous: ObservationSnapshotV3,
        snapshot: ObservationSnapshotV3,
        residual: MotionResidualResult,
    ) -> ExternalMotionEventV1:
        generation = self._next_generation()
        before = previous.self_state.value
        after = snapshot.self_state.value
        return ExternalMotionEventV1(
            f"external/{snapshot.episode_id}/{after.movement_tick_id}/{generation}",
            generation, snapshot.episode_id, snapshot.sequence_id,
            after.movement_tick_id,
            ExternalMotionSource.UNATTRIBUTED_EXTERNAL_MOTION,
            0.0, before.position, after.position,
            before.velocity, after.velocity,
            before.is_on_ground, after.is_on_ground,
            0.0, residual.position_error_blocks,
            residual.velocity_error_blocks_per_tick,
        )
