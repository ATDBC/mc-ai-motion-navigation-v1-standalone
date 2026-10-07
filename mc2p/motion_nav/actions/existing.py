"""Explicit M1 declarations for the existing ground, step, and jump actions."""
import math
from mc2p.motion_nav.action_requirements import ActionPreconditionResult, ActionPreconditionStatus, ActionPreconditionReason
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route import WalkSegment, StepSegment, JumpUpSegment, JumpGapSegment
from mc2p.motion_nav.air_motion import AirMotionController, AirMotionState
from mc2p.motion_nav.motion_solver import LandingRegion, MotionSolveKind
from mc2p.motion_nav.actions.contracts import ActionSpec, BodyCommitment, ActionControllerAdapter, ActionControllerResult, ActionRouteState, SolveGeometry, StopHold


def zero_damage(action): return 0.0
def no_damage_commit(action, frame, *, force=False): return False
def no_entry_observation(action, frame): return None
def no_destination_completion(action, frame): return False


def ready_precondition(*args, **kwargs):
    return ActionPreconditionResult(ActionPreconditionStatus.READY, ActionPreconditionReason.READY)


def surface_geometry(action, anchor, distance, maximum_ticks):
    sx, sy, sz = action.start_surface.position
    dx, dz = action.end_surface.position[0] - sx, action.end_surface.position[2] - sz
    direction = None
    if math.isclose(abs(dx), distance, abs_tol=1.0e-7) and math.isclose(dz, 0.0, abs_tol=1.0e-7):
        direction = (1 if dx > 0 else -1, 0)
    elif math.isclose(abs(dz), distance, abs_tol=1.0e-7) and math.isclose(dx, 0.0, abs_tol=1.0e-7):
        direction = (0, 1 if dz > 0 else -1)
    landing = None
    if anchor is not None:
        half = anchor.physics_state.body_width / 2.0
        region = action.end_surface.region
        if region.max_x - half > region.min_x + half and region.max_z - half > region.min_z + half:
            landing = LandingRegion(region.min_x + half, region.max_x - half,
                                    region.min_z + half, region.max_z - half,
                                    action.end_surface.position[1])
    return SolveGeometry(direction, landing, (sx, sy, sz), maximum_ticks)


def gap_geometry(action, anchor): return surface_geometry(action, anchor, 2.0, 40)


def jump_up_geometry(action, anchor):
    x, y, z = action.edge.end
    half = None if anchor is None else anchor.physics_state.body_width / 2.0
    landing = (None if half is None else LandingRegion(
        x + half, x + 1.0 - half, z + half, z + 1.0 - half, float(y)))
    sx, sy, sz = action.edge.start
    return SolveGeometry(action.edge.direction, landing, (sx + .5, float(sy), sz + .5), 40)


def air_controller(action, frame, air_profiles):
    profile = air_profiles.get(action.edge.profile_id)
    if profile is None:
        raise ContractViolation('air segment requires a calibrated profile')
    if action.transition is not None and action.transition.trajectory_profile_id != profile.profile_id:
        raise ContractViolation('air segment uses another trajectory profile')
    controller = AirMotionController(profile)
    controller.start(action.start_surface, action.end_surface, frame)
    return controller


def region_completed(frame, min_x, max_x, min_z, max_z, feet_y):
    if not frame.body.is_on_ground:
        return False
    x, y, z = frame.body.position
    if abs(y - feet_y) > .10:
        return False
    overlap_x = max(0.0, min(x + .30, max_x) - max(x - .30, min_x))
    overlap_z = max(0.0, min(z + .30, max_z) - max(z - .30, min_z))
    return overlap_x * overlap_z >= .01


def surface_completed(action, frame):
    region = action.end_surface.region
    return region_completed(frame, region.min_x, region.max_x,
                            region.min_z, region.max_z, action.end_surface.position[1])


def jump_up_completed(action, frame):
    x, y, z = action.edge.end
    return region_completed(frame, float(x), float(x + 1), float(z), float(z + 1), float(y))


def air_entry_limits(action, air_profiles):
    profile = air_profiles.get(action.edge.profile_id)
    if profile is None:
        raise ContractViolation('air segment requires a calibrated profile')
    return profile.entry_center_tolerance_blocks, profile.maximum_entry_speed_blocks_per_second


def interpret_air_result(decision):
    terminal = {
        AirMotionState.COMPLETE: ActionRouteState.COMPLETE,
        AirMotionState.BLOCKED: ActionRouteState.BLOCKED,
        AirMotionState.NEEDS_INFORMATION: ActionRouteState.NEEDS_INFORMATION,
        AirMotionState.UNSUPPORTED: ActionRouteState.UNSUPPORTED,
        AirMotionState.FAILED: ActionRouteState.FAILED,
        AirMotionState.CANCELLED: ActionRouteState.CANCELLED,
        AirMotionState.INPUT_LOST: ActionRouteState.INPUT_LOST,
    }
    return ActionControllerResult(terminal.get(decision.state), decision.movement,
        decision.input_lease_ticks, decision.reason_code, decision.missing_cells, decision.look)


def protect_air_stop(controller, frame):
    speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                       frame.body.velocity_blocks_per_second[2])
    if frame.body.is_on_ground and not controller._departure_observed:
        stopped = speed <= controller.profile.maximum_exit_speed_blocks_per_second
        controller.state = AirMotionState.CANCELLED if stopped else AirMotionState.CANCELLING
        return ActionControllerResult(
            ActionRouteState.CANCELLED if stopped else ActionRouteState.CANCELLING,
            MovementV1(), 1, 'cancelled_before_departure' if stopped else 'stopping_before_departure')
    return None


AIR_CONTROLLER_ADAPTER = ActionControllerAdapter(
    air_controller, air_entry_limits, interpret_air_result, protect_air_stop)


EXISTING_SPECS = (
    ActionSpec(segment_type=WalkSegment, body_commitment=BodyCommitment.GROUND,
               requires_verified_motion=False, expected_damage_points=zero_damage,
               stop_hold=StopHold(False, None, None), needs_background_solving=False,
               solve_kind=None, solve_geometry=None, controller_adapter=None, completed=no_destination_completion,
               entry_observation=no_entry_observation, precondition=ready_precondition,
               tracks_damage=False, damage_committed=no_damage_commit),
    ActionSpec(segment_type=StepSegment, body_commitment=BodyCommitment.TRANSITION,
               requires_verified_motion=False, expected_damage_points=zero_damage,
               stop_hold=StopHold(False, None, None), needs_background_solving=False,
               solve_kind=None, solve_geometry=None, controller_adapter=None, completed=no_destination_completion,
               entry_observation=no_entry_observation, precondition=ready_precondition,
               tracks_damage=False, damage_committed=no_damage_commit),
    ActionSpec(segment_type=JumpUpSegment, body_commitment=BodyCommitment.TRANSITION,
               requires_verified_motion=True, expected_damage_points=zero_damage,
               stop_hold=StopHold(False, None, None), needs_background_solving=True,
               solve_kind=MotionSolveKind.JUMP_UP, solve_geometry=jump_up_geometry,
               controller_adapter=None,
               completed=jump_up_completed, entry_observation=no_entry_observation,
               precondition=ready_precondition, tracks_damage=False, damage_committed=no_damage_commit),
    ActionSpec(segment_type=JumpGapSegment, body_commitment=BodyCommitment.TRANSITION,
               requires_verified_motion=True, expected_damage_points=zero_damage,
               stop_hold=StopHold(True, None, None), needs_background_solving=True,
               solve_kind=MotionSolveKind.JUMP_GAP, solve_geometry=gap_geometry,
               controller_adapter=AIR_CONTROLLER_ADAPTER,
               completed=surface_completed, entry_observation=no_entry_observation,
               precondition=ready_precondition, tracks_damage=False, damage_committed=no_damage_commit),
)
