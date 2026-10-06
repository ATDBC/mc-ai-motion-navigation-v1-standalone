"""Sketch of the ActionSpec interface for M1 (review material, not project code).

    python -m py_compile action_spec_sketch.py

Every member answers one question that coordination or execution code currently answers with
`type(action) is ...` (categories A-H in design_metrics.py).  Existing project types are reused;
they are referenced by name only so this file stays importable outside the repository.

Rules the sketch encodes:
  * A spec is stateless and pure: it reads a segment and observations, never stores per-run state.
    Per-execution state stays in the controller instance it creates (one owner per state).
  * Coordination code may ask a spec questions; it may not branch on the segment class.
  * A member that does not apply to an action has a typed neutral default in ActionSpecBase,
    so adding an action only implements what is different about it.
  * M1 implements A-F for ControlledDrop and the classification members (B, C) that the
    gap+drop combined sites need for all five types.  G (proof/revalidation) arrives in M3.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class BodyCommitment(StrEnum):
    """B: what an interruption may do to the body during this action."""
    GROUNDED = "grounded"              # can stop in place (walk, step)
    ENTRY_ON_GROUND = "entry"          # still on support, departure not yet committed
    AIRBORNE_COMMITTED = "airborne"    # must keep landing responsibility until landing


class SolutionDisposition(StrEnum):
    """C: how a delivered background solution is consumed."""
    INSTALL = "install"
    RECOMPUTE = "recompute"
    WAIT_FOR_LANDING_THEN_REANCHOR = "wait_for_landing"   # the S0R-C-01 rule, owned by the action
    UNSOLVABLE = "unsolvable"


@dataclass(frozen=True, slots=True)
class StopHold:
    """B: the neutral input an action needs while waiting (e.g. sneak at a drop edge)."""
    sneak: bool = False
    look_required: bool = False


@runtime_checkable
class ActionSpec(Protocol):
    segment_type: type

    # F geometry and progress
    def start_surface(self, segment) -> "SupportSurface": ...
    def end_surface(self, segment) -> "SupportSurface": ...
    def route_length_blocks(self, segment) -> float: ...
    def progress_key(self, segment) -> tuple: ...

    # D preconditions and information (reuses ActionPreconditionResult / AcquisitionSpec)
    def precondition(self, segment, frame: "NavigationFrame") -> "ActionPreconditionResult": ...
    def observation_needs(self, segment, frame: "NavigationFrame") -> "ObservationRequestV3 | None": ...

    # E risk and damage
    def expected_damage_points(self, segment) -> float: ...
    def damage_committed(self, segment, frame: "NavigationFrame") -> bool: ...

    # B body commitment and stop safety
    def body_commitment(self, segment, frame: "NavigationFrame", started: bool) -> BodyCommitment: ...
    def requires_safe_handoff(self, segment) -> bool: ...
    def stop_hold(self, segment, frame: "NavigationFrame") -> StopHold: ...
    def requires_verified_motion(self, segment) -> bool: ...

    # C background motion solving
    def solve_kind(self, segment) -> "MotionSolveKind | None": ...
    def solve_request(self, segment, anchor: "StateAnchor", following) -> "MotionSolveRequest | None": ...
    def accept_solution(self, segment, result, anchor: "StateAnchor") -> SolutionDisposition: ...

    # A controller selection and execution
    def controller(self, segment, context: "ActionContext") -> "ActionController": ...
    def arrived(self, segment, frame: "NavigationFrame") -> bool: ...

    # G proof and revalidation (M3)
    def dependencies(self, segment) -> tuple["BlockPos", ...]: ...
    def revalidate(self, segment, world: "WorldView", changed_cells) -> "ActiveRouteValidation": ...


class ActionSpecBase:
    """Typed neutral defaults; a concrete spec overrides only what differs."""
    segment_type: type = object

    def expected_damage_points(self, segment) -> float:
        return 0.0

    def damage_committed(self, segment, frame) -> bool:
        return False

    def body_commitment(self, segment, frame, started: bool) -> BodyCommitment:
        return BodyCommitment.GROUNDED

    def requires_safe_handoff(self, segment) -> bool:
        return False

    def stop_hold(self, segment, frame) -> StopHold:
        return StopHold()

    def requires_verified_motion(self, segment) -> bool:
        return False

    def solve_kind(self, segment):
        return None

    def solve_request(self, segment, anchor, following):
        return None

    def observation_needs(self, segment, frame):
        return None


class ActionRegistry:
    """H: the only place that maps a segment class to its spec."""

    def __init__(self, specs: tuple[ActionSpec, ...]) -> None:
        self._by_type = {}
        for spec in specs:
            if spec.segment_type in self._by_type:
                raise ValueError(f"duplicate spec for {spec.segment_type.__name__}")
            self._by_type[spec.segment_type] = spec

    def spec(self, segment) -> ActionSpec:
        try:
            return self._by_type[type(segment)]
        except KeyError:
            raise TypeError(f"no ActionSpec registered for {type(segment).__name__}") from None

    def supports(self, segment) -> bool:
        return type(segment) in self._by_type


# What ControlledDropSpec takes over in M1 (21 baseline sites; see design_metrics-e108a32.json):
#   D  action_preconditions.check_action_precondition; Session.observation_request,
#      _current_action_precondition, _upcoming_action_precondition_index, _begin_action_acquisition
#      -> precondition() / observation_needs()
#   E  Session._route_expected_damage_points, _reserve_route_risk; executor._commit_drop_damage_if_started
#      -> expected_damage_points() / damage_committed()
#   B  executor.stop_protection; Session._needs_same_frame_stop_protection, _prepare_route_action (sneak);
#      MotionRouteCoordinator.decide (drop > 1 block crosses boundary)
#      -> body_commitment() / stop_hold() / requires_safe_handoff()
#   C  MotionRouteCoordinator._air_action_direction, _air_action_landing, _prepare_upcoming_from_applied_state
#      -> solve_request() / accept_solution()
#   A  executor._activate (x2), _landed_on_current_action_destination, decide (x2)
#      -> controller() / arrived()
#   H  ActionRoute.__post_init__ -> ActionRegistry.supports()
