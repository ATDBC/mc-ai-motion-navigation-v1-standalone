"""Experimental normal-ground route feedback; predictions never authorize motion."""
from __future__ import annotations

import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.skills.normal_direction_control import world_direction


# Ground-only local approximation, checked against development traces.  This is
# neither a new Minecraft physics implementation nor a safety envelope.
PARAMETERS = {
    'revision': 2,
    'acceleration_blocks_per_tick2': .098,
    'retained_velocity_fraction': .546,
    'horizon_ticks': 2,
    'maximum_sequences': 16,
    'position_weight': 1.,
    'stopping_offset_weight': 1.,
    'progress_weight': .002,
    'switch_weight': .00005,
}
_INPUTS = tuple(MovementV1(forward=f, strafe=s) for f, s in (
    (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)))


def _finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('feedback requires finite numeric state')
    return value


def _pair(value):
    if type(value) is not tuple or len(value) != 2:
        raise ValueError('feedback requires horizontal coordinate pairs')
    return tuple(_finite(item) for item in value)


def choose_feedback_movement(position, velocity, origin, travel_yaw, gaze_yaw,
                             previous=MovementV1(), *, gaze_step=0.,
                             remaining_movement_ticks=None) -> MovementV1:
    """Compare up to 16 two-tick sequences, execute only their first input.

    A declared final movement tick predicts neutral input on the second tick.
    Otherwise the second tick uses bounded continuation of the requested turn, not world
    truth. Position and velocity are lawful current self observations.  The
    route reference stays fixed. Every returned action still needs the caller's
    real guard/arbiter/lease; this function provides no motion authorization.
    """
    px, pz = _pair(position)
    vx, vz = _pair(velocity)
    ox, oz = _pair(origin)
    for value in (travel_yaw, gaze_yaw, gaze_step):
        _finite(value)
    if type(previous) is not MovementV1 or abs(gaze_step) > 3.:
        raise ValueError('invalid previous input or bounded gaze continuation')
    if remaining_movement_ticks is not None and (
            type(remaining_movement_ticks) is not int or remaining_movement_ticks < 1):
        raise ValueError('remaining movement ticks must be a positive integer or None')
    tx, tz = world_direction(travel_yaw, 1, 0)
    nx, nz = tz, -tx
    error = (px-ox)*nx + (pz-oz)*nz
    lateral = vx*nx + vz*nz
    acceleration = PARAMETERS['acceleration_blocks_per_tick2']
    drag = PARAMETERS['retained_velocity_fraction']

    def candidates(yaw):
        result = []
        for movement in _INPUTS:
            dx, dz = world_direction(yaw, movement.forward, movement.strafe)
            progress = dx*tx + dz*tz
            # No stationary/backward shortcut for passing a moving probe.
            if progress > 1e-9:
                result.append((movement, dx*nx + dz*nz, progress))
        return result

    first = candidates(gaze_yaw)
    second = ([(MovementV1(), 0., 0.)] if remaining_movement_ticks == 1
              else candidates(gaze_yaw + gaze_step))
    best = None
    for movement, side, progress in first:
        displacement = lateral + acceleration*side
        e1, v1 = error + displacement, displacement*drag
        for following, next_side, next_progress in second:
            displacement2 = v1 + acceleration*next_side
            e2, v2 = e1 + displacement2, displacement2*drag
            stopping_offset = e2 + v2/(1.-drag)
            score = (PARAMETERS['position_weight']*(e1*e1 + e2*e2)
                     + PARAMETERS['stopping_offset_weight']*stopping_offset**2
                     + PARAMETERS['progress_weight']*(2.-progress-next_progress)
                     + PARAMETERS['switch_weight']*(int(movement != previous)
                                                    + int(following != movement)))
            if best is None or score < best[0]:
                best = score, movement
    return best[1]
