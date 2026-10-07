"""Action facts only; no task identities, retries, or body ownership state."""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, TYPE_CHECKING

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.motion_solver import MotionSolveKind, LandingRegion
if TYPE_CHECKING:
    from mc2p.motion_nav.runtime_adapter import NavigationFrame
    from mc2p.motion_nav.action_requirements import ActionPreconditionResult
    from mc2p.motion_nav.segment_entry import SegmentEntryWindow
    from mc2p.motion_nav.world_model import BlockPos


class BodyCommitment(StrEnum):
    GROUND = 'ground'
    TRANSITION = 'transition'


class ActionRouteState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED = "unsupported"
    INPUT_LOST = "input_lost"
    NEEDS_REPLAN = "needs_replan"


@dataclass(frozen=True, slots=True)
class ActionControllerResult:
    state: ActionRouteState | None
    movement: MovementV1
    input_lease_ticks: int
    reason: str
    missing_cells: tuple[BlockPos, ...] = ()
    look: object | None = None


@dataclass(frozen=True, slots=True)
class ActionControllerAdapter:
    create: Callable
    entry_limits: Callable
    interpret: Callable
    # Replace an unsubmitted departure without deciding the controller twice.
    stop_protection: Callable | None = None


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
    controller_adapter: ActionControllerAdapter | None
    completed: Callable[[object, NavigationFrame], bool]
    entry_observation: Callable
    precondition: Callable[..., ActionPreconditionResult]
    tracks_damage: bool
    damage_committed: Callable
