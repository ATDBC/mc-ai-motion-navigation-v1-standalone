"""Bounded point-goal values. A supplied destination grants no map knowledge."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

from mc2p.contracts.common import (
    ContractViolation, require_finite, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.task import TaskIntentV0, SuccessCriterionV0, ComparisonOperatorV0
from mc2p.contracts.action_v1 import MovementV1
from mc2p.skills.follow_playground_types import PlaygroundView
from mc2p.skills.follow_types import MOVEMENT_FRESHNESS_NS
from mc2p.skills.block_geometry import world_boxes

MAX_SIGNED_INT64 = 2**63 - 1
MAX_PARAMETERS_BYTES = 8192
_REQUIRED = frozenset({'goal_id', 'scope_id', 'position'})
_OPTIONAL = frozenset({'radius', 'height_tolerance', 'dwell_ns', 'stop_speed'})
_SUCCESS = (SuccessCriterionV0('point_goal_reached', ComparisonOperatorV0.EQUAL, 1, 'boolean'),)


def _identity(value: str, name: str) -> None:
    require_identifier(value, name)
    if len(value) > 128:
        raise ContractViolation(name+' exceeds 128 characters')


def _time(value: int, name: str) -> None:
    require_nonnegative_int(value, name)
    if value > MAX_SIGNED_INT64:
        raise ContractViolation(name+' exceeds signed int64')


def _finite(value: float, name: str) -> None:
    try:
        require_finite(value, name)
    except OverflowError as error:
        raise ContractViolation(name+' exceeds finite numeric range') from error


def _position(value: Vec3V0) -> None:
    if type(value) is not Vec3V0:
        raise ContractViolation('point goal position requires Vec3V0')
    if any(abs(v) > 30_000_000 for v in (value.x, value.y, value.z)):
        raise ContractViolation('point goal position exceeds world bounds')


@dataclass(frozen=True, slots=True)
class PointGoal:
    goal_id: str
    scope_id: str
    position: Vec3V0
    deadline_ns: int
    radius: float = .75
    height_tolerance: float = .1
    dwell_ns: int = 300_000_000
    stop_speed: float = .03  # Blocks/client tick, not blocks/second.

    def __post_init__(self) -> None:
        _identity(self.goal_id, 'point goal id')
        _identity(self.scope_id, 'point goal scope')
        _position(self.position)
        _time(self.deadline_ns, 'point goal deadline')
        if self.deadline_ns == 0:
            raise ContractViolation('point goal deadline must be positive')
        for name, ceiling in (('radius', .75), ('height_tolerance', .1), ('stop_speed', .03)):
            value = getattr(self, name)
            _finite(value, name)
            if not 0 < value <= ceiling:
                raise ContractViolation(name+' cannot relax frozen arrival conditions')
        _time(self.dwell_ns, 'point goal dwell')
        if self.dwell_ns < 300_000_000:
            raise ContractViolation('point goal dwell must be at least 300ms')


def inside_goal(goal: PointGoal, position: Vec3V0) -> bool:
    """Geometry only: no assertion about obstacles, support or actual arrival."""
    if type(goal) is not PointGoal:
        raise ContractViolation('inside_goal requires PointGoal')
    _position(position)
    # Account only for coordinate representation/subtraction rounding, not a
    # gameplay-sized extension of the arrival region.
    horizontal_roundoff = 4 * max(math.ulp(v) for v in (
        position.x, position.z, goal.position.x, goal.position.z, goal.radius))
    vertical_roundoff = 4 * max(math.ulp(v) for v in (
        position.y, goal.position.y, goal.height_tolerance))
    return (math.hypot(position.x-goal.position.x, position.z-goal.position.z)
            <= goal.radius + horizontal_roundoff
            and abs(position.y-goal.position.y) <= goal.height_tolerance + vertical_roundoff)


def _unique_pairs(items: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in items:
        if key in value:
            raise ContractViolation('duplicate point goal parameter')
        value[key] = item
    return value


def _nonfinite(value: str) -> None:
    raise ContractViolation('nonfinite point goal JSON: '+value)


def parse_point_goal(task: TaskIntentV0, scope_id: str) -> PointGoal:
    if type(task) is not TaskIntentV0 or task.task_type != 'point_goal':
        raise ContractViolation('point goal requires an explicit point_goal task')
    _identity(scope_id, 'point goal scope')
    if task.success_criteria != _SUCCESS:
        raise ContractViolation('point goal requires point_goal_reached eq 1 boolean')
    if len(task.parameters_json.encode('utf-8')) > MAX_PARAMETERS_BYTES:
        raise ContractViolation('point goal parameters exceed bounded payload')
    try:
        value = json.loads(task.parameters_json, object_pairs_hook=_unique_pairs,
                           parse_constant=_nonfinite)
    except (ValueError, RecursionError) as error:
        raise ContractViolation('invalid point goal parameters: '+str(error)) from error
    if (type(value) is not dict or not _REQUIRED <= value.keys()
            or not value.keys() <= _REQUIRED | _OPTIONAL):
        raise ContractViolation('point goal parameters have missing or unknown fields')
    if value['scope_id'] != scope_id:
        raise ContractViolation('point goal scope mismatch')
    position = value['position']
    if type(position) is not dict or set(position) != {'x', 'y', 'z'}:
        raise ContractViolation('point goal position requires exactly x/y/z')
    for name, coordinate in position.items():
        _finite(coordinate, 'point goal '+name)
    return PointGoal(value['goal_id'], scope_id, Vec3V0(**position),
                     task.deadline_monotonic_ns,
                     **{key:value[key] for key in _OPTIONAL if key in value})


def _crosses_open_rectangle(start: Vec3V0, end: Vec3V0,
                            bounds: tuple[float, float, float, float]) -> bool:
    """Segment vs open XZ rectangle, including its endpoints but not tangency."""
    low_t, high_t = 0., 1.
    for origin, target, low, high in ((start.x,end.x,bounds[0],bounds[2]),
                                    (start.z,end.z,bounds[1],bounds[3])):
        delta = target-origin
        if abs(delta) < 1e-12:
            if not low < origin < high:
                return False
        else:
            first, last = sorted(((low-origin)/delta, (high-origin)/delta))
            low_t, high_t = max(low_t, first), min(high_t, last)
            if low_t >= high_t:
                return False
    return True


def _known_barrier(view: PlaygroundView, goal: PointGoal) -> bool:
    """Negative evidence only, not proof of unobserved clearance or reachability."""
    own = view.base.own
    assert own is not None
    for block in view.base.observed_blocks:
        # Unknown shape is neither clearance nor known negative evidence.
        # The navigation guard/evaluator must resolve relevant uncertainty;
        # an unrelated unsupported block must not globally veto arrival.
        if block.collision.kind == 'unsupported':
            continue
        for box in world_boxes(block):
            if (box.max_y <= min(own.position.y,goal.position.y)+1e-7
                    or box.min_y >= max(own.position.y,goal.position.y)+1.8-1e-7):
                continue
            # Swept standing body; do not count a nearby goal across a thin wall.
            bounds = (box.min_x-.3+1e-7, box.min_z-.3+1e-7,
                      box.max_x+.3-1e-7, box.max_z+.3-1e-7)
            if _crosses_open_rectangle(own.position, goal.position, bounds):
                return True
    return False


class GoalMonitor:
    """Confirm a bounded sequence of fresh, stationary, same-context samples.

    The caller binds this monitor to the goal's navigation scope and supplies
    confirmed final movement inputs, not a policy proposal. JVM sample times
    measure dwell; controller times measure deadlines/delivery. Neither is
    server truth. The driver guard and independent evaluator still check the
    route/arrival-region connectivity; sparse local data proves no full map.
    """

    def __init__(self, goal: PointGoal) -> None:
        if type(goal) is not PointGoal:
            raise ContractViolation('goal monitor requires PointGoal')
        self._goal = goal
        self._state = 'running'
        self._last_view: PlaygroundView | None = None
        self._last_now_ns: int | None = None
        self._dwell_client_ns: int | None = None
        self._dwell_controller_ns: int | None = None
        self._invalid_reason: str | None = None

    @property
    def goal(self) -> PointGoal:
        return self._goal

    @property
    def state(self) -> str:
        return self._state

    def cancel(self) -> str:
        if self._state == 'running':
            self._state = 'cancelled'
            self._clear_dwell()
        return self._state

    def interrupt_dwell(self) -> str:
        """Break only an in-progress arrival dwell without resetting evidence."""
        if self._state == 'running':
            self._clear_dwell()
        return self._state

    def _clear_dwell(self) -> None:
        self._dwell_client_ns = self._dwell_controller_ns = None

    def _invalid(self, reason: str) -> None:
        self._invalid_reason = reason
        self._state = 'invalidated'
        self._clear_dwell()
        raise ContractViolation(reason)

    def _ordered(self, view: PlaygroundView, now_ns: int) -> bool:
        base = view.base
        if self._last_now_ns is not None and now_ns < self._last_now_ns:
            self._invalid('goal_controller_time_regression')
        if not 0 <= base.request_start_ns <= base.received_at_ns <= now_ns:
            self._invalid('goal_future_or_invalid_observation')
        if not 0 <= base.client_sample_start_ns <= base.client_sample_end_ns:
            self._invalid('goal_invalid_sample_interval')
        self._last_now_ns = now_ns
        if self._last_view is None:
            return True
        old = self._last_view.base
        if (base.episode_id,base.controller_clock_id,base.client_clock_id) != (
                old.episode_id,old.controller_clock_id,old.client_clock_id):
            self._invalid('goal_session_or_clock_changed')
        if base.sequence_id < old.sequence_id:
            self._invalid('goal_sequence_regression')
        if base.sequence_id == old.sequence_id:
            timing = ('request_start_ns','received_at_ns','client_sample_start_ns','client_sample_end_ns')
            if (any(getattr(base,k) != getattr(old,k) for k in timing)
                    or (base.available and old.available and view != self._last_view)):
                self._invalid('goal_sequence_rewritten')
            return False
        if (base.request_start_ns < old.request_start_ns or base.received_at_ns < old.received_at_ns
                or base.client_sample_start_ns < old.client_sample_end_ns):
            self._invalid('goal_sample_time_regression')
        if (base.client_sample_start_ns-old.client_sample_end_ns > MOVEMENT_FRESHNESS_NS
                or base.request_start_ns-old.received_at_ns > MOVEMENT_FRESHNESS_NS):
            self._clear_dwell()
        return True

    def observe(self, view: PlaygroundView, now_ns: int, movement: MovementV1) -> str:
        _time(now_ns, 'goal monitor time')
        if type(view) is not PlaygroundView or type(movement) is not MovementV1:
            raise ContractViolation('goal monitor requires legal view and final MovementV1')
        if self._invalid_reason is not None:
            raise ContractViolation(self._invalid_reason)
        if self._state != 'running':
            return self._state
        fresh_sequence = self._ordered(view, now_ns)
        if now_ns >= self.goal.deadline_ns:
            self._clear_dwell()
            self._state = 'timeout'
            return self._state
        base, own = view.base, view.base.own
        qualifies = (base.available and own is not None and not base.gui_open
            and 0 <= now_ns-base.request_start_ns <= MOVEMENT_FRESHNESS_NS
            and own.on_ground and not own.dead and not own.horizontal_collision
            and not own.unsupported_motion and view.pose == 'standing'
            and view.game_mode == 'survival' and not view.status_effect_ids
            and movement == MovementV1()
            and math.hypot(own.velocity.x, own.velocity.z) <= self.goal.stop_speed
            and inside_goal(self.goal, own.position) and not _known_barrier(view, self.goal))
        if not qualifies:
            self._clear_dwell()
        if not fresh_sequence:
            return self._state
        self._last_view = view
        if qualifies:
            if self._dwell_client_ns is None:
                # Conservative bounds: first read may be as late as sample end.
                self._dwell_client_ns = base.client_sample_end_ns
                self._dwell_controller_ns = now_ns
            elif (base.client_sample_start_ns-self._dwell_client_ns >= self.goal.dwell_ns
                    and now_ns-self._dwell_controller_ns >= self.goal.dwell_ns):
                self._state = 'success'
        return self._state
