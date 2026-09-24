"""Compare observed player motion with B09-R prediction from applied inputs.

This module does not infer a damage source.  It answers the narrower question:
given the last accepted state, the exact inputs consumed by Minecraft and the
known collision world, did the body move as the current physics model predicts?
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger,
    MotionTickPhase,
    StateAnchor,
)
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_adapter import build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus,
    JAVA_1_21_RULESET,
    PhysicsRuleset,
    PhysicsState,
    TickInput,
)
from mc2p.motion_nav.world_model import BlockPos
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.contracts.observation_v3 import ObservationSnapshotV3


class MotionResidualStatus(StrEnum):
    MATCHED = "matched"
    DEVIATION = "deviation"
    NEEDS_INPUT = "needs_input"
    NEEDS_WORLD = "needs_world"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True, slots=True)
class MotionResidualThresholds:
    """Provisional shadow-mode thresholds for ordinary Java 1.21 movement.

    They are deliberately explicit.  Fabric evidence may tighten or widen them,
    but callers cannot silently use a different definition of external motion.
    """

    position_blocks: float = 0.08
    velocity_blocks_per_tick: float = 0.08

    def __post_init__(self) -> None:
        for name in ("position_blocks", "velocity_blocks_per_tick"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)) \
                    or value < 0:
                raise ContractViolation("motion residual thresholds must be finite and nonnegative")


DEFAULT_MOTION_RESIDUAL_THRESHOLDS = MotionResidualThresholds()

_ORDINARY_PLAYER_ASSUMPTIONS = {
    "jumping_cooldown_ticks": 0,
    "movement_speed_attribute": 0.1,
    "step_height_blocks": 0.6,
    "gravity_attribute": 0.08,
    "jump_strength_attribute": 0.42,
}


@dataclass(frozen=True, slots=True)
class MotionResidualResult:
    status: MotionResidualStatus
    anchor_tick: int
    observed_tick: int
    predicted_state: PhysicsState | None = None
    position_error_blocks: float | None = None
    velocity_error_blocks_per_tick: float | None = None
    contact_mismatch: bool = False
    dependencies: tuple[BlockPos, ...] = ()
    missing_ticks: tuple[int, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not MotionResidualStatus:
            raise ContractViolation("motion residual status must be typed")
        if type(self.anchor_tick) is not int or self.anchor_tick < 0 \
                or type(self.observed_tick) is not int or self.observed_tick < 0:
            raise ContractViolation("motion residual ticks must be nonnegative integers")
        complete = self.status in {
            MotionResidualStatus.MATCHED, MotionResidualStatus.DEVIATION,
        }
        if complete != (type(self.predicted_state) is PhysicsState):
            raise ContractViolation("only a complete residual can carry predicted state")
        if complete != (
            self.position_error_blocks is not None
            and self.velocity_error_blocks_per_tick is not None
        ):
            raise ContractViolation("complete residual requires numeric errors")
        for value in (self.position_error_blocks, self.velocity_error_blocks_per_tick):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ContractViolation("motion residual errors must be finite and nonnegative")
        if self.status is MotionResidualStatus.NEEDS_INPUT and not self.missing_ticks:
            raise ContractViolation("missing input result requires missing ticks")
        if self.status is MotionResidualStatus.NEEDS_WORLD and not self.missing_cells:
            raise ContractViolation("missing world result requires missing cells")
        if self.status in {
            MotionResidualStatus.UNSUPPORTED, MotionResidualStatus.INVALID_INPUT,
        } and not self.reasons:
            raise ContractViolation("incomplete residual result requires reasons")

    @property
    def has_external_motion(self) -> bool:
        return self.status is MotionResidualStatus.DEVIATION


def _magnitude(values: tuple[float, float, float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in values))


def _difference(
    left: tuple[float, float, float], right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(float(a) - float(b) for a, b in zip(left, right))


def calculate_motion_residual(
    anchor: StateAnchor,
    observed_state: PhysicsState,
    ledger: InputApplicationLedger,
    world: PhysicsWorldView,
    *,
    ruleset: PhysicsRuleset = JAVA_1_21_RULESET,
    thresholds: MotionResidualThresholds = DEFAULT_MOTION_RESIDUAL_THRESHOLDS,
) -> MotionResidualResult:
    """Replay the consumed movement ticks and compare their final body state."""
    if (type(anchor) is not StateAnchor or type(observed_state) is not PhysicsState
            or type(ledger) is not InputApplicationLedger
            or type(world) is not PhysicsWorldView
            or type(ruleset) is not PhysicsRuleset
            or type(thresholds) is not MotionResidualThresholds):
        raise ContractViolation("motion residual requires typed inputs")
    start_tick = anchor.movement_tick_id
    end_tick = observed_state.movement_tick_id
    if (anchor.phase is not MotionTickPhase.AFTER_MOVEMENT
            or end_tick <= start_tick
            or anchor.session != observed_state.session
            or world.session != anchor.session
            or anchor.ruleset_id != ruleset.ruleset_id
            or observed_state.ruleset_id != ruleset.ruleset_id
            or anchor.state_schema != ruleset.state_schema
            or observed_state.state_schema != ruleset.state_schema):
        return MotionResidualResult(
            MotionResidualStatus.INVALID_INPUT, start_tick, end_tick,
            reasons=("anchor_or_identity_not_comparable",),
        )

    first_tick = start_tick + 1
    samples = ledger.samples_between(first_tick, end_tick)
    samples_by_tick = {sample.movement_tick_id: sample for sample in samples}
    missing_ticks = tuple(
        tick for tick in range(first_tick, end_tick + 1)
        if tick not in samples_by_tick
    )
    if missing_ticks:
        return MotionResidualResult(
            MotionResidualStatus.NEEDS_INPUT, start_tick, end_tick,
            missing_ticks=missing_ticks,
        )

    predicted = anchor.physics_state
    dependencies: set[BlockPos] = set()
    for tick in range(first_tick, end_tick + 1):
        sample = samples_by_tick[tick]
        yaw = predicted.yaw_radians
        if sample.request_sequence_id is not None:
            record = ledger.record(sample.request_sequence_id)
            if record is None or record.session != anchor.session or not record.applied_ticks:
                return MotionResidualResult(
                    MotionResidualStatus.NEEDS_INPUT, start_tick, end_tick,
                    missing_ticks=(tick,),
                )
            if tick == min(record.applied_ticks):
                yaw += math.radians(record.action.look.yaw_delta_degrees)
        tick_input = TickInput(
            float(sample.forward), float(sample.strafe), sample.jump,
            sample.sneak, sample.sprint, yaw,
        )
        result = step(predicted, tick_input, world, ruleset)
        dependencies.update(result.dependencies)
        if result.status is CalculationStatus.NEEDS_WORLD:
            return MotionResidualResult(
                MotionResidualStatus.NEEDS_WORLD, start_tick, end_tick,
                dependencies=tuple(sorted(dependencies)),
                missing_cells=result.missing_cells,
            )
        if result.status is CalculationStatus.UNSUPPORTED:
            return MotionResidualResult(
                MotionResidualStatus.UNSUPPORTED, start_tick, end_tick,
                dependencies=tuple(sorted(dependencies)),
                reasons=result.unsupported_reasons,
            )
        if result.status is not CalculationStatus.OK or result.next_state is None:
            return MotionResidualResult(
                MotionResidualStatus.INVALID_INPUT, start_tick, end_tick,
                dependencies=tuple(sorted(dependencies)),
                reasons=result.invalid_reasons or ("physics_step_failed",),
            )
        predicted = result.next_state

    position_error = _magnitude(_difference(observed_state.position, predicted.position))
    velocity_error = _magnitude(_difference(
        observed_state.velocity_blocks_per_tick,
        predicted.velocity_blocks_per_tick,
    ))
    contact_mismatch = (
        observed_state.on_ground != predicted.on_ground
        or observed_state.horizontal_collision != predicted.horizontal_collision
        or observed_state.vertical_collision != predicted.vertical_collision
    )
    deviated = (
        position_error > thresholds.position_blocks
        or velocity_error > thresholds.velocity_blocks_per_tick
        or contact_mismatch
    )
    return MotionResidualResult(
        MotionResidualStatus.DEVIATION if deviated else MotionResidualStatus.MATCHED,
        start_tick, end_tick, predicted, position_error, velocity_error,
        contact_mismatch, tuple(sorted(dependencies)),
    )


class MotionResidualTracker:
    """Maintain one observation anchor while keeping every comparison bounded."""

    def __init__(
        self,
        *,
        ruleset: PhysicsRuleset = JAVA_1_21_RULESET,
        thresholds: MotionResidualThresholds = DEFAULT_MOTION_RESIDUAL_THRESHOLDS,
        maximum_replay_ticks: int = 8,
    ) -> None:
        if type(ruleset) is not PhysicsRuleset \
                or type(thresholds) is not MotionResidualThresholds:
            raise ContractViolation("motion residual tracker requires typed policy")
        if type(maximum_replay_ticks) is not int or maximum_replay_ticks < 1:
            raise ContractViolation("motion residual replay bound must be positive")
        self._ruleset = ruleset
        self._thresholds = thresholds
        self._maximum_replay_ticks = maximum_replay_ticks
        self._anchor: StateAnchor | None = None
        self._last_sequence_id: int | None = None

    @property
    def anchor(self) -> StateAnchor | None:
        return self._anchor

    def reset(self) -> None:
        self._anchor = None
        self._last_sequence_id = None

    def observe(
        self,
        snapshot: ObservationSnapshotV3,
        frame: NavigationFrame,
        ledger: InputApplicationLedger,
    ) -> MotionResidualResult | None:
        if (type(snapshot) is not ObservationSnapshotV3
                or type(frame) is not NavigationFrame
                or type(ledger) is not InputApplicationLedger):
            raise ContractViolation("motion residual tracker requires formal observation data")
        own = snapshot.self_state.value
        if own is None or own.movement_tick_id is None:
            self.reset()
            return None
        build = build_physics_state(
            frame, self._ruleset, _ORDINARY_PLAYER_ASSUMPTIONS,
        )
        if build.state is None:
            self.reset()
            return None
        observed = replace(build.state, movement_tick_id=own.movement_tick_id)
        previous = self._anchor
        if (previous is None or previous.session != frame.session
                or self._last_sequence_id is None):
            self._set_anchor(snapshot, observed)
            return None
        if snapshot.sequence_id == self._last_sequence_id:
            return None
        if snapshot.sequence_id < self._last_sequence_id:
            raise ContractViolation("motion residual observation sequence regressed")
        if observed.movement_tick_id - previous.movement_tick_id \
                > self._maximum_replay_ticks:
            self._set_anchor(snapshot, observed)
            return MotionResidualResult(
                MotionResidualStatus.INVALID_INPUT,
                previous.movement_tick_id,
                observed.movement_tick_id,
                reasons=("residual_replay_window_exceeded",),
            )
        result = calculate_motion_residual(
            previous, observed, ledger,
            PhysicsWorldView(frame.world, self._ruleset),
            ruleset=self._ruleset, thresholds=self._thresholds,
        )
        self._last_sequence_id = snapshot.sequence_id
        if result.status in {
            MotionResidualStatus.NEEDS_INPUT,
            MotionResidualStatus.NEEDS_WORLD,
        }:
            # Receipts and explicit air queries can complete this same interval
            # on a later observation.  Keep the last proved state as the anchor
            # instead of silently forgetting the unexplained tick.
            return result
        # Public observation owns position, velocity, pose and contacts.  The
        # calculator carries forward the one hidden tick state we currently
        # model (jump cooldown) when its replay was complete.
        anchored = observed
        if result.predicted_state is not None:
            anchored = replace(
                observed,
                jumping_cooldown_ticks=(
                    result.predicted_state.jumping_cooldown_ticks
                ),
            )
        self._set_anchor(snapshot, anchored)
        return result

    def _set_anchor(
        self, snapshot: ObservationSnapshotV3, state: PhysicsState,
    ) -> None:
        self._anchor = StateAnchor(
            state.session, snapshot.sequence_id, state.movement_tick_id,
            MotionTickPhase.AFTER_MOVEMENT, None, None,
            self._ruleset.ruleset_id, self._ruleset.state_schema,
            "mc2p.input-projection.v1", state,
        )
        self._last_sequence_id = snapshot.sequence_id
