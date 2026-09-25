"""Bounded pure state machine for recovering from confirmed external motion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.external_motion import ExternalMotionEventV1


class RecoveryDirective(StrEnum):
    NEUTRAL_AIR = "neutral_air"
    START_GROUND_HOLD = "start_ground_hold"
    CONTINUE_GROUND_HOLD = "continue_ground_hold"
    COMPLETE = "complete"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class ExternalMotionRecoveryConfig:
    maximum_recovery_ticks: int = 40
    maximum_events: int = 4
    stable_ticks: int = 2
    stopped_speed_blocks_per_tick: float = .03

    def __post_init__(self) -> None:
        for name, value in (
            ("maximum recovery ticks", self.maximum_recovery_ticks),
            ("maximum events", self.maximum_events),
            ("stable ticks", self.stable_ticks),
        ):
            require_nonnegative_int(value, name)
            if value == 0:
                raise ContractViolation(f"{name} must be positive")
        require_finite(
            self.stopped_speed_blocks_per_tick, "stopped speed threshold"
        )
        if self.stopped_speed_blocks_per_tick < 0:
            raise ContractViolation("stopped speed threshold must be nonnegative")


@dataclass(frozen=True, slots=True)
class ExternalMotionRecoveryDecision:
    directive: RecoveryDirective
    generation: int
    elapsed_ticks: int
    stable_ticks: int
    complete: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.directive) is not RecoveryDirective:
            raise ContractViolation("recovery directive must be typed")
        require_nonnegative_int(self.generation, "recovery generation")
        require_nonnegative_int(self.elapsed_ticks, "recovery elapsed ticks")
        require_nonnegative_int(self.stable_ticks, "recovery stable ticks")
        if type(self.complete) is not bool:
            raise ContractViolation("recovery complete must be bool")
        if not isinstance(self.reason, str) or not self.reason:
            raise ContractViolation("recovery reason must be non-empty")


class ExternalMotionRecoveryController:
    """Own one recovery at a time while retaining the task-wide event budget."""

    def __init__(self, config: ExternalMotionRecoveryConfig | None = None) -> None:
        self.config = config or ExternalMotionRecoveryConfig()
        if type(self.config) is not ExternalMotionRecoveryConfig:
            raise ContractViolation("external recovery config must be typed")
        self._event_count = 0
        self._episode_id: str | None = None
        self._first_tick: int | None = None
        self._generation = 0
        self._last_sequence: int | None = None
        self._last_tick: int | None = None
        self._stable_ticks = 0
        self._ground_hold_started = False
        self._active = False
        self._complete = False
        self._exhausted_reason: str | None = None
        self._task_limit_reason: str | None = None

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def active_generation(self) -> int | None:
        return self._generation if self._active else None

    @property
    def task_limit_reason(self) -> str | None:
        """A task-policy signal that does not release the current body owner."""
        return self._task_limit_reason

    def start(self, event: ExternalMotionEventV1) -> None:
        self._require_event(event)
        if self._active and not self._complete:
            raise ContractViolation("external recovery is already active")
        self._event_count += 1
        self._episode_id = event.episode_id
        self._first_tick = event.movement_tick_id
        self._generation = event.generation
        self._last_sequence = event.observation_sequence_id
        self._last_tick = event.movement_tick_id
        self._stable_ticks = 0
        self._ground_hold_started = False
        self._active = True
        self._complete = False
        self._exhausted_reason = None
        self._task_limit_reason = (
            "event_budget_exhausted"
            if self._event_count > self.config.maximum_events else None
        )

    def observe_event(self, event: ExternalMotionEventV1) -> bool:
        self._require_event(event)
        if not self._active or self._complete:
            raise ContractViolation("external recovery has no active process")
        if event.episode_id != self._episode_id:
            raise ContractViolation("external recovery event changed session")
        if event.generation == self._generation:
            return False
        if event.generation < self._generation:
            raise ContractViolation("external recovery event generation moved backward")
        if event.movement_tick_id < self._last_tick:
            raise ContractViolation("external recovery event movement tick moved backward")
        self._event_count += 1
        self._generation = event.generation
        self._last_sequence = max(self._last_sequence, event.observation_sequence_id)
        self._last_tick = event.movement_tick_id
        self._stable_ticks = 0
        if self._event_count > self.config.maximum_events:
            self._task_limit_reason = "event_budget_exhausted"
        return True

    def decide(self, snapshot: ObservationSnapshotV3) -> ExternalMotionRecoveryDecision:
        if type(snapshot) is not ObservationSnapshotV3:
            raise ContractViolation("external recovery requires V3 observation")
        if not self._active:
            raise ContractViolation("external recovery has no active process")
        if self._complete:
            raise ContractViolation("external recovery is already complete")
        if snapshot.episode_id != self._episode_id:
            raise ContractViolation("external recovery observation changed session")
        own = snapshot.self_state.value
        if own is None or own.movement_tick_id is None:
            raise ContractViolation("external recovery requires current movement tick")
        if snapshot.sequence_id < self._last_sequence:
            raise ContractViolation("external recovery observation sequence moved backward")
        if own.movement_tick_id < self._last_tick:
            raise ContractViolation("external recovery movement tick moved backward")

        new_movement_tick = own.movement_tick_id > self._last_tick
        self._last_sequence = max(self._last_sequence, snapshot.sequence_id)
        if new_movement_tick:
            self._last_tick = own.movement_tick_id
        elapsed = own.movement_tick_id - self._first_tick
        if self._exhausted_reason is None and elapsed > self.config.maximum_recovery_ticks:
            self._exhausted_reason = "recovery_tick_budget_exhausted"
        if self._exhausted_reason is not None:
            return self._decision(
                RecoveryDirective.EXHAUSTED, elapsed, False, self._exhausted_reason
            )

        if not own.is_on_ground:
            if new_movement_tick:
                self._stable_ticks = 0
                self._ground_hold_started = False
            return self._decision(
                RecoveryDirective.NEUTRAL_AIR, elapsed, False, "airborne_settling"
            )

        directive = (
            RecoveryDirective.CONTINUE_GROUND_HOLD
            if self._ground_hold_started
            else RecoveryDirective.START_GROUND_HOLD
        )
        self._ground_hold_started = True
        horizontal_speed = math.hypot(own.velocity.x, own.velocity.z)
        if new_movement_tick:
            if horizontal_speed <= self.config.stopped_speed_blocks_per_tick:
                self._stable_ticks += 1
            else:
                self._stable_ticks = 0
        if self._stable_ticks >= self.config.stable_ticks:
            self._complete = True
            return self._decision(
                RecoveryDirective.COMPLETE, elapsed, True, "stable_reanchored"
            )
        return self._decision(directive, elapsed, False, "ground_braking")

    def _decision(
        self,
        directive: RecoveryDirective,
        elapsed: int,
        complete: bool,
        reason: str,
    ) -> ExternalMotionRecoveryDecision:
        return ExternalMotionRecoveryDecision(
            directive=directive,
            generation=self._generation,
            elapsed_ticks=elapsed,
            stable_ticks=self._stable_ticks,
            complete=complete,
            reason=reason,
        )

    @staticmethod
    def _require_event(event: ExternalMotionEventV1) -> None:
        if type(event) is not ExternalMotionEventV1:
            raise ContractViolation("external recovery event must be typed")
