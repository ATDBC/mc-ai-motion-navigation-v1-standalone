"""Predeclared quality routes: evaluator-only ordinary leader controls."""
from copy import deepcopy
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import require_nonnegative_int
from mc2p.contracts.observation import Vec3V0

QUALITY_PHASES = (
    dict(id=0, kind='quality_straight', mode='normal', start_ns=5_000_000_000,
         duration_ns=35_000_000_000, waypoints=[[0., 12.]]),
    dict(id=1, kind='quality_stop_go', mode='normal', start_ns=45_000_000_000,
         duration_ns=35_000_000_000, waypoints=[[0., 18.], [0., 24.]]),
    dict(id=2, kind='quality_straight', mode='auto', start_ns=85_000_000_000,
         duration_ns=35_000_000_000, waypoints=[[0., 36.]]),
    dict(id=3, kind='quality_turn', mode='normal', start_ns=125_000_000_000,
         duration_ns=35_000_000_000, waypoints=[[0., 40.], [8., 40.]]),
)
MEASUREMENT_REVISION = 6  # Same metrics/windows/sensor; block-state V3 admission and fresh paired runs.
ANGULAR_WINDOW_NS = 10_000_000_000


def quality_clock_bindings(runtimes: tuple, measurement_clock_id: str) -> dict[str,str]:
    """Evaluator-only attestation of the configured Python clock, not JVM alignment.

    Backends use independent stream IDs even when sharing perf_counter_ns. Keep
    those formal IDs intact; reject injected clocks instead of assuming equality.
    """
    import time
    if len(runtimes) != 2 or not measurement_clock_id:
        raise ValueError('quality requires two clock sources')
    result = {}
    for runtime in runtimes:
        if runtime._clock is not time.perf_counter_ns or runtime._backend._clock_ns is not time.perf_counter_ns:
            raise ValueError('quality shared controller clock is not proven')
        if runtime.observation.controller_clock_id in result:
            raise ValueError('quality requires distinct per-backend controller IDs')
        result[runtime.observation.controller_clock_id] = measurement_clock_id
    return result


def quality_case_plan() -> dict:
    commands = []
    for phase in QUALITY_PHASES:
        start = phase['start_ns']
        commands.extend((dict(at_ns=start-2_000_000_000, kind='follow_stop'),
                         dict(at_ns=start-2_000_000_000, kind='follow_mode', mode=phase['mode']),
                         dict(at_ns=start, kind='follow_start'),
                         dict(at_ns=start+phase['duration_ns'], kind='follow_stop')))
    commands.append(dict(at_ns=164_500_000_000, kind='follow_stop'))
    return dict(schema_version='mc2p.playground-case-plan.v1', revision=1, case='active-quality',
                interval_ns=100_000_000, duration_ns=165_000_000_000,
                phases=deepcopy(list(QUALITY_PHASES)), owner_commands=commands,
                angular_window_ns=ANGULAR_WINDOW_NS,
                leader_separation_distance_blocks=None, actor_input='lawful_structured_only',
                leader_schedule='ordinary_client_controls_evaluator_only', measurement_revision=MEASUREMENT_REVISION)


def quality_leader_controls(phase: dict, own_position: Vec3V0, own_yaw: float,
                            origin: Vec3V0) -> tuple[MovementV1, LookV1]:
    """Optional waypoint_index is evaluator progress, not a change to the saved plan."""
    index = phase.get('waypoint_index', 0)
    require_nonnegative_int(index, 'quality waypoint index')
    if index >= len(phase['waypoints']):
        return MovementV1(), LookV1()
    x, z = phase['waypoints'][index]
    dx, dz = origin.x+x-own_position.x, origin.z+z-own_position.z
    if math.hypot(dx, dz) <= .2:
        return MovementV1(), LookV1()
    delta = (math.degrees(math.atan2(-dx, dz))-own_yaw+180)%360-180
    return MovementV1(forward=1 if abs(delta) <= 8 else 0), LookV1(max(-15, min(15, delta)), 0)


class QualityLeaderController:
    """Bounded progress through the fixed route using the leader's own observations."""
    def __init__(self, origin: Vec3V0):
        self.origin = origin
        self.phase_id = None
        self.waypoint_index = 0
        self.pause_until_ns = None
        self.last_time_ns = None

    def controls(self, phase: dict, position: Vec3V0, yaw: float,
                 now_ns: int) -> tuple[MovementV1, LookV1]:
        require_nonnegative_int(now_ns, 'quality leader time')
        if self.last_time_ns is not None and now_ns < self.last_time_ns:
            raise ValueError('quality leader time regressed')
        self.last_time_ns = now_ns
        if phase['id'] != self.phase_id:
            self.phase_id, self.waypoint_index, self.pause_until_ns = phase['id'], 0, None
        if self.pause_until_ns is not None:
            if now_ns < self.pause_until_ns:
                return MovementV1(), LookV1()
            self.pause_until_ns = None
        if self.waypoint_index < len(phase['waypoints']):
            x, z = phase['waypoints'][self.waypoint_index]
            if math.hypot(position.x-self.origin.x-x, position.z-self.origin.z-z) <= .2:
                self.waypoint_index += 1
                if phase['kind'] == 'quality_stop_go' and self.waypoint_index == 1:
                    self.pause_until_ns = now_ns+2_000_000_000
                    return MovementV1(), LookV1()
        return quality_leader_controls(dict(phase, waypoint_index=self.waypoint_index), position, yaw, self.origin)
