"""Action facts only; no task identities, retries, or body ownership state."""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, TYPE_CHECKING

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.motion_solver import MotionSolveKind, LandingRegion
if TYPE_CHECKING:
    from mc2p.motion_nav.runtime_adapter import NavigationFrame
    from mc2p.motion_nav.action_preconditions import ActionPreconditionResult
    from mc2p.motion_nav.segment_entry import SegmentEntryWindow
    from mc2p.motion_nav.world_model import BlockPos


class BodyCommitment(StrEnum):
    GROUND = 'ground'
    TRANSITION = 'transition'


class ControllerFamily(StrEnum):
    GROUND = 'ground'
    JUMP_UP = 'jump_up'
    STEP = 'step'
    AIR = 'air'


@dataclass(frozen=True, slots=True)
class StopHold:
    same_frame_protection: bool
    information_movement: MovementV1 | None
    dependency_movement: MovementV1 | None


@dataclass(frozen=True, slots=True)
class EntryObservation:
    landing_cell: BlockPos
    dependencies: tuple[BlockPos, ...]
    entry_window: SegmentEntryWindow | None
    recheck_started_action: bool
    can_begin_acquisition: bool
    needs_acquisition_before_solve: bool


@dataclass(frozen=True, slots=True)
class SolveGeometry:
    direction: tuple[int, int] | None
    landing: LandingRegion | None
    start: tuple[float, float, float]
    maximum_ticks: int


@dataclass(frozen=True, slots=True)
class ActionSpec:
    segment_type: type
    body_commitment: BodyCommitment
    requires_verified_motion: bool
    expected_damage_points: Callable[[object], float]
    stop_hold: StopHold
    needs_background_solving: bool
    solve_kind: MotionSolveKind | None
    solve_geometry: Callable | None
    controller_family: ControllerFamily
    controller_factory: Callable | None
    completed: Callable[[object, NavigationFrame], bool]
    entry_observation: Callable
    precondition: Callable[..., ActionPreconditionResult]
    tracks_damage: bool
    damage_committed: Callable
