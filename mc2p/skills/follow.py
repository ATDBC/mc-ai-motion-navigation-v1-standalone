"""Pure bounded rule follower. It has no backend, Runtime, world or leader handle."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_types import MAX_DECISIONS, MOVEMENT_FRESHNESS_NS, FollowRequest, FollowView, TargetState
from mc2p.skills.local_navigation import Cell, LocalBlockMemory, guard_motion, plan_local_route
from mc2p.skills.local_perception import TargetMemory


@dataclass(frozen=True, slots=True)
class FollowReport:
    skill_id: str
    target_track_id: str
    observation_sequence_id: int
    decision_number: int
    state: str
    reason: str
    terminal: bool
    target_status: str
    distance_blocks: float | None
    route: tuple[Cell, ...]
    recovery_count: int


@dataclass(frozen=True, slots=True)
class FollowDecision:
    movement: MovementV1
    look: LookV1
    report: FollowReport
    waypoint: Vec3V0 | None = None


def _yaw(dx: float, dz: float) -> float:
    return math.degrees(math.atan2(-dx, dz))


def _wrap(angle: float) -> float:
    return (angle + 180) % 360 - 180


def _look(view: FollowView, yaw: float, pitch: float = 32) -> LookV1:
    return LookV1(max(-30, min(30, _wrap(yaw-view.own.yaw))), max(-20, min(20, pitch-view.own.pitch)))


class RuleFollower:
    def __init__(self, request: FollowRequest, initial_view: FollowView) -> None:
        self.request = request
        self.source_id = "follow/" + request.skill_id
        self._target = TargetMemory()
        self._target.bind(request, initial_view)
        self._map = LocalBlockMemory()
        self._map.update(initial_view)
        self._terminal: tuple[str, str] | None = None
        self._decisions = 0
        self._last_sequence = -1
        self._approaching = False
        self._scan_count = 0
        self._scan_pending = False
        self._recoveries: list[tuple[Cell, Cell]] = []
        self._last_decision: FollowDecision | None = None
        self._progress_position = initial_view.own.position
        self._progress_time = request.started_at_ns
        self._last_target = TargetState("visible", None, initial_view.received_at_ns)
        self._waypoint: Vec3V0 | None = None

    def cancel(self, reason: str, *, state: str = "cancelled") -> None:
        if state not in {"cancelled", "timed_out", "failed"} or not isinstance(reason, str) or not reason:
            raise ContractViolation("invalid follow termination")
        if not self._terminal:
            self._terminal = (state, reason)
            self._map.clear()

    def _result(self, view: FollowView, state: str, reason: str, *, distance: float | None = None,
                movement: MovementV1 = MovementV1(), look: LookV1 = LookV1(),
                route: tuple[Cell, ...] = (), terminal: bool = False,
                waypoint: Vec3V0 | None = None) -> FollowDecision:
        if terminal:
            self._terminal = (state, reason)
            self._map.clear()
        result = FollowDecision(movement, look, FollowReport(self.request.skill_id, self.request.target_track_id,
            view.sequence_id, self._decisions, state, reason, terminal, self._last_target.status,
            distance, route, len(self._recoveries)), waypoint)
        self._last_decision = result
        return result

    def _scan(self, view: FollowView, target: Vec3V0, distance: float, reason: str) -> FollowDecision:
        if self._scan_count >= 16:
            return self._result(view, "blocked", "scan_budget_exhausted", distance=distance, terminal=True)
        angles = ((0, 45), (0, 60), (-35, 45), (35, 45), (0, 20), (0, 45), (-60, 35), (60, 35))
        offset, pitch = angles[self._scan_count % len(angles)]
        self._scan_count += 1
        self._scan_pending = True
        yaw = _yaw(target.x-view.own.position.x, target.z-view.own.position.z)
        return self._result(view, "searching", reason, distance=distance, look=_look(view, yaw+offset, pitch))

    def decide(self, view: FollowView, now_ns: int) -> FollowDecision:
        self._scan_pending = False
        self._decisions += 1
        if self._terminal:
            return self._result(view, *self._terminal, terminal=True)
        if now_ns >= self.request.deadline_ns:
            return self._result(view, "timed_out", "skill_deadline", terminal=True)
        if self._decisions > MAX_DECISIONS:
            return self._result(view, "timed_out", "decision_budget_exhausted", terminal=True)
        target = self._target.observe(view, now_ns)
        self._last_target = target
        if target.status == "invalidated":
            return self._result(view, "cancelled", target.reason, terminal=True)
        if target.status == "lost":
            return self._result(view, "target_lost", target.reason, terminal=True)
        if not view.available or view.own is None or not 0 <= now_ns-view.received_at_ns <= MOVEMENT_FRESHNESS_NS:
            return self._result(view, "cancelled", view.reason or "stale_observation", terminal=True)
        if view.gui_open or view.own.dead:
            return self._result(view, "cancelled", "gui_open" if view.gui_open else "player_dead", terminal=True)
        if view.sequence_id == self._last_sequence:
            return self._result(view, "searching", "awaiting_new_observation")
        self._last_sequence = view.sequence_id
        self._map.update(view)
        if view.own.unsupported_motion or not view.own.on_ground:
            return self._result(view, "blocked", "unsupported_motion_state", terminal=True)
        if target.position is None:
            return self._result(view, "cancelled", "target_observation_unavailable", terminal=True)
        position, goal = view.own.position, target.position
        distance = math.hypot(goal.x-position.x, goal.z-position.z)
        if abs(goal.y-position.y) > .1:
            return self._result(view, "blocked", "unsupported_target_height", distance=distance, terminal=True)
        target_yaw = _yaw(goal.x-position.x, goal.z-position.z)
        if distance < self.request.retreat_distance_blocks:
            self._waypoint = None
            if distance < .01:
                return self._result(view, "blocked", "unsafe_retreat", distance=distance)
            retreat = Vec3V0(position.x+(position.x-goal.x)/distance, position.y,
                            position.z+(position.z-goal.z)/distance)
            guard = guard_motion(self._map, view, retreat, now_ns)
            if not guard.allowed:
                return self._result(view, "blocked", "unsafe_retreat", distance=distance,
                                    look=_look(view, target_yaw))
            movement = MovementV1(forward=-1) if abs(_wrap(target_yaw-view.own.yaw)) <= 12 else MovementV1()
            return self._result(view, "following", "bounded_retreat", distance=distance,
                                movement=movement, look=_look(view, target_yaw))
        if distance > self.request.approach_distance_blocks:
            self._approaching = True
        elif distance <= self.request.hold_distance_blocks:
            self._approaching = False
        if not self._approaching:
            self._waypoint = None
            self._scan_count = 0
            return self._result(view, "holding_distance" if target.status == "visible" else "searching",
                                "distance_hysteresis", distance=distance, look=_look(view, target_yaw))
        route = plan_local_route(self._map, view, goal, now_ns, tuple(self._recoveries), stop_distance=self.request.hold_distance_blocks)
        if not route.cells:
            if route.reason == "search_budget_exhausted":
                return self._result(view, "blocked", route.reason, distance=distance, terminal=True)
            return self._scan(view, goal, distance, route.reason)
        cell = route.cells[1] if len(route.cells) > 1 else route.cells[0]
        waypoint = Vec3V0(cell[0]+.5, position.y, cell[1]+.5)
        previous = self._waypoint
        if (previous is not None and (math.floor(previous.x),math.floor(previous.z)) in route.cells[:2]
                and math.hypot(previous.x-position.x, previous.z-position.z) > .15):
            waypoint = previous
        self._waypoint = waypoint
        desired_yaw = _yaw(waypoint.x-position.x, waypoint.z-position.z)
        if abs(_wrap(desired_yaw-view.own.yaw)) > 12:
            return self._result(view, "following" if target.status == "visible" else "searching", "turn_to_route",
                                distance=distance, look=_look(view, desired_yaw), route=route.cells, waypoint=waypoint)
        guard = guard_motion(self._map, view, waypoint, now_ns)
        if not guard.allowed:
            return self._scan(view, waypoint, distance, guard.reason)
        self._scan_count = 0
        return self._result(view, "following" if target.status == "visible" else "searching", "bounded_step",
                            distance=distance, movement=MovementV1(forward=1), look=_look(view, desired_yaw),
                            route=route.cells, waypoint=waypoint)

    def feedback(self, executed: bool, view: FollowView, now_ns: int) -> None:
        if self._terminal or view.own is None or self._last_decision is None:
            return
        position = view.own.position
        if self._scan_pending:
            if not executed:
                self._scan_count -= 1
            self._scan_pending = False
        if not executed or self._last_decision.report.state == "holding_distance":
            # External preemption/intentional holding is not a stuck movement interval.
            self._progress_position, self._progress_time = position, now_ns
            return
        if self._last_decision.movement == MovementV1():
            # Our own scan/turn is part of an unresolved route, not evidence of progress.
            return
        if math.hypot(position.x-self._progress_position.x, position.z-self._progress_position.z) >= .08:
            self._progress_position, self._progress_time = position, now_ns
        elif now_ns-self._progress_time >= 2_000_000_000:
            route = self._last_decision.report.route
            if len(route) < 2 or len(self._recoveries) >= 3 or (route[0], route[1]) in self._recoveries:
                self._terminal = ("blocked", "recovery_budget_exhausted")
                self._map.clear()
            else:
                self._recoveries.append((route[0], route[1]))
                self._progress_position, self._progress_time = position, now_ns
