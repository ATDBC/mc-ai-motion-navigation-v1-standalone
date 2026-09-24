"""Typed evidence that player motion was changed by a confirmed external source."""

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


class ExternalMotionSource(StrEnum):
    DAMAGE_KNOCKBACK = "damage_knockback"


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

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "external motion event id")
        require_identifier(self.episode_id, "external motion episode id")
        require_nonnegative_int(self.generation, "external motion generation")
        if self.generation == 0:
            raise ContractViolation("external motion generation must be positive")
        require_nonnegative_int(
            self.observation_sequence_id, "external motion observation sequence"
        )
        require_nonnegative_int(self.movement_tick_id, "external motion movement tick")
        if type(self.source) is not ExternalMotionSource:
            raise ContractViolation("external motion source must be typed")
        require_finite(self.health_delta_points, "external motion health delta")
        if self.health_delta_points >= 0:
            raise ContractViolation("damage knockback health delta must be negative")
        for value in (
            self.previous_position,
            self.position,
            self.previous_velocity,
            self.velocity,
        ):
            if type(value) is not Vec3V0:
                raise ContractViolation("external motion vectors must be Vec3V0")
        if type(self.was_on_ground) is not bool or type(self.is_on_ground) is not bool:
            raise ContractViolation("external motion ground facts must be bool")


@dataclass(frozen=True, slots=True)
class ExternalMotionDetection:
    event: ExternalMotionEventV1 | None
    reason: str

    def __post_init__(self) -> None:
        if self.event is not None and type(self.event) is not ExternalMotionEventV1:
            raise ContractViolation("external motion detection event must be typed")
        require_identifier(self.reason, "external motion detection reason")


class DamageKnockbackDetector:
    """Turn a confirmed self-damage transition into one deduplicated event."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._episode_id: str | None = None
        self._last_sequence_id: int | None = None
        self._baseline: ObservationSnapshotV3 | None = None
        self._pending_damage_baseline: ObservationSnapshotV3 | None = None
        self._generation = 0

    def observe(self, snapshot: ObservationSnapshotV3) -> ExternalMotionDetection:
        if type(snapshot) is not ObservationSnapshotV3:
            raise ContractViolation("damage knockback detector requires V3 observation")

        if self._episode_id is None:
            self._episode_id = snapshot.episode_id
            self._last_sequence_id = snapshot.sequence_id
            self._baseline = snapshot if self._has_motion_evidence(snapshot) else None
            return ExternalMotionDetection(
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
            return ExternalMotionDetection(None, "session_reanchored")

        if snapshot.sequence_id == self._last_sequence_id:
            return ExternalMotionDetection(None, "duplicate_observation")
        if snapshot.sequence_id < self._last_sequence_id:
            raise ContractViolation("external motion observation sequence moved backward")
        self._last_sequence_id = snapshot.sequence_id

        if not self._has_motion_evidence(snapshot):
            self._baseline = None
            self._pending_damage_baseline = None
            return ExternalMotionDetection(None, "motion_evidence_unavailable")
        if self._baseline is None:
            self._baseline = snapshot
            return ExternalMotionDetection(None, "baseline_established")

        previous = self._baseline
        previous_tick = previous.self_state.value.movement_tick_id
        current_tick = snapshot.self_state.value.movement_tick_id
        if current_tick < previous_tick:
            raise ContractViolation("external motion movement tick moved backward")
        if current_tick == previous_tick:
            return ExternalMotionDetection(None, "movement_tick_not_advanced")

        previous_own = previous.self_state.value
        current_own = snapshot.self_state.value
        pending = self._pending_damage_baseline
        if pending is not None:
            pending_own = pending.self_state.value
            if current_own.hurt_animation_ticks > 0:
                health_delta = current_own.health_points - pending_own.health_points
                self._baseline = snapshot
                if health_delta < 0:
                    self._pending_damage_baseline = None
                    return self._confirmed_event(pending, snapshot, health_delta)
                return ExternalMotionDetection(None, "no_health_loss")
            self._pending_damage_baseline = None

        self._baseline = snapshot
        if not (
            previous_own.hurt_animation_ticks == 0
            and current_own.hurt_animation_ticks > 0
        ):
            return ExternalMotionDetection(None, "no_damage_transition")

        health_delta = current_own.health_points - previous_own.health_points
        if health_delta >= 0:
            self._pending_damage_baseline = previous
            return ExternalMotionDetection(None, "no_health_loss")

        return self._confirmed_event(previous, snapshot, health_delta)

    def _confirmed_event(
        self,
        previous: ObservationSnapshotV3,
        snapshot: ObservationSnapshotV3,
        health_delta: float,
    ) -> ExternalMotionDetection:
        previous_own = previous.self_state.value
        current_own = snapshot.self_state.value
        current_tick = current_own.movement_tick_id

        self._generation += 1
        event = ExternalMotionEventV1(
            event_id=(
                f"damage/{snapshot.episode_id}/{current_tick}/{self._generation}"
            ),
            generation=self._generation,
            episode_id=snapshot.episode_id,
            observation_sequence_id=snapshot.sequence_id,
            movement_tick_id=current_tick,
            source=ExternalMotionSource.DAMAGE_KNOCKBACK,
            health_delta_points=health_delta,
            previous_position=previous_own.position,
            position=current_own.position,
            previous_velocity=previous_own.velocity,
            velocity=current_own.velocity,
            was_on_ground=previous_own.is_on_ground,
            is_on_ground=current_own.is_on_ground,
        )
        return ExternalMotionDetection(event, "damage_knockback_confirmed")

    @staticmethod
    def _has_motion_evidence(snapshot: ObservationSnapshotV3) -> bool:
        own = snapshot.self_state.value
        return (
            own is not None
            and own.hurt_animation_ticks is not None
            and own.movement_tick_id is not None
        )
