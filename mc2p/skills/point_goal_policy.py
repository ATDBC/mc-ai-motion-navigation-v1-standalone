"""Frozen A/B/C point-goal policies for the forward/stop normal baseline."""
from __future__ import annotations

import heapq
import math
import time

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import AabbV3
from mc2p.skills.active_perception import ActivePerceptionCoordinator
from mc2p.skills.block_geometry import overlaps
from mc2p.skills.follow_playground_types import PlaygroundView
from mc2p.skills.local_navigation import BODY_MARGIN, prepare_block_collision
from mc2p.skills.navigation_belief import BeliefStore
from mc2p.skills.navigation_controller import NavigationController, NavigationDecision
from mc2p.skills.navigation_look import ObservationGate, wrap
from mc2p.skills.navigation_memory import MemorySnapshot, TerrainHistory
from mc2p.skills.navigation_recovery import RecoveryLedger
from mc2p.skills.normal_navigation_guard import check_normal_segment, _support_reason
from mc2p.skills.normal_navigation_types import NormalNavigationConfig
from mc2p.skills.navigation_strategy import GOAL_DIRECTED_EXPLORATION, JOINT_STRATEGIES, strategy_config
from mc2p.skills.perception_needs import PerceptionConfig
from mc2p.skills.point_goal import PointGoal, inside_goal


MAX_DIAGNOSTIC_CELL_REJECTIONS = 64
MAX_DIAGNOSTIC_SEGMENT_REJECTIONS = 64
MAX_DIAGNOSTIC_REASON_CHARS = 80
MAX_DIAGNOSTIC_SEGMENT_KEY_CHARS = 160


class PointGoalPolicy:
    """Common bounded B/C route selection plus the genuine old A baseline."""

    def __init__(self, group: str, *, control_capabilities=None,
                 prelook_enabled: bool = True, force_route_id: str | None = None,
                 stage_goals_enabled: bool = False) -> None:
        self.config = strategy_config(group)
        self.group = group
        self.controller = NavigationController(
            perception=ActivePerceptionCoordinator(
                "active_perception_v1", PerceptionConfig()
            )
        ) if group == "A" else None
        self._gate = ObservationGate()
        self._ledger = RecoveryLedger()
        self._scope: tuple[str, str, str, str] | None = None
        self._last_sequence = -1
        self._selected_waypoint: Vec3V0 | None = None
        self._feedback_snapshot: MemorySnapshot | None = None
        self._shared_cache: dict[str, object] | None = None
        self._planning_summary: dict[str, object] = {}
        self._navigation_state = None
        self.joint_planner = None
        if group in JOINT_STRATEGIES:
            from mc2p.skills.navigation_joint_policy import JointPlanner
            if type(stage_goals_enabled) is not bool:
                raise ContractViolation('stage goals switch must be boolean')
            if group == GOAL_DIRECTED_EXPLORATION:
                from mc2p.skills.navigation_stage_policy import GoalDirectedExplorationPlanner
                self.joint_planner = GoalDirectedExplorationPlanner(
                    control_capabilities=control_capabilities,
                    prelook_enabled=prelook_enabled, force_route_id=force_route_id)
                return
            if stage_goals_enabled:
                from mc2p.skills.navigation_stage_policy import StageJointPlanner
                JointPlanner = StageJointPlanner
            self.joint_planner = JointPlanner(group,
                control_capabilities=control_capabilities,
                prelook_enabled=prelook_enabled, force_route_id=force_route_id)

    @property
    def planning_summary(self) -> dict[str, object]:
        if self.joint_planner is not None:
            return self.joint_planner.diagnostic
        return dict(self._planning_summary)

    @property
    def planning_diagnostic(self) -> dict[str, object]:
        """Bounded current-frame projection; recovery revisions stay internal.

        Candidates use the configured 16-entry cap. Cell and segment refusals
        have independent 64-entry caps (128 combined), and any entry or string
        cut sets ``summary_truncated``.
        """
        if self.joint_planner is not None:
            return self.joint_planner.diagnostic
        summary = self._planning_summary
        if not summary:
            return dict(
                planned=False, expansions=0, cells_checked=0,
                candidates=(), cell_rejections=(), segment_rejections=(),
                elapsed_ns=0, planning_budget_ns=self.config.planning_budget_ns,
                budget_exhausted=False, summary_truncated=False,
            )
        raw_candidates = tuple(summary.get("candidates", ()))
        candidates = tuple(dict(
            cell=tuple(cell), score=float(score), age_cost=float(age_cost),
            turn_cost=float(turn_cost),
        ) for cell, score, age_cost, turn_cost
            in raw_candidates[:self.config.max_candidates])
        raw_cells = tuple(summary.get("cell_rejections", ()))
        cell_rejections = tuple(dict(
            cell=tuple(cell), reason=str(reason)[:MAX_DIAGNOSTIC_REASON_CHARS])
            for cell, reason in raw_cells[:MAX_DIAGNOSTIC_CELL_REJECTIONS])
        raw_segments = tuple(summary.get("segment_rejections", ()))
        segment_rejections = tuple(dict(
            key=str(item[0])[:MAX_DIAGNOSTIC_SEGMENT_KEY_CHARS],
            reason=str(item[1])[:MAX_DIAGNOSTIC_REASON_CHARS])
            for item in raw_segments[:MAX_DIAGNOSTIC_SEGMENT_REJECTIONS])
        cell_string_truncated = any(
            len(str(item[1])) > MAX_DIAGNOSTIC_REASON_CHARS
            for item in raw_cells[:MAX_DIAGNOSTIC_CELL_REJECTIONS])
        segment_string_truncated = any(
            len(str(item[0])) > MAX_DIAGNOSTIC_SEGMENT_KEY_CHARS
            or len(str(item[1])) > MAX_DIAGNOSTIC_REASON_CHARS
            for item in raw_segments[:MAX_DIAGNOSTIC_SEGMENT_REJECTIONS])
        return dict(
            planned=True,
            expansions=int(summary.get("expansions", 0)),
            cells_checked=int(summary.get("cells_checked", 0)),
            candidates=candidates,
            cell_rejections=cell_rejections,
            segment_rejections=segment_rejections,
            elapsed_ns=int(summary.get("elapsed_ns", 0)),
            planning_budget_ns=int(summary.get(
                "planning_budget_ns", self.config.planning_budget_ns)),
            budget_exhausted=bool(summary.get("budget_exhausted", False)),
            summary_truncated=(bool(summary.get("summary_truncated", False))
                               or len(raw_candidates) > self.config.max_candidates
                               or len(raw_cells) > MAX_DIAGNOSTIC_CELL_REJECTIONS
                               or len(raw_segments) > MAX_DIAGNOSTIC_SEGMENT_REJECTIONS
                               or cell_string_truncated
                               or segment_string_truncated),
        )

    def bind_state(self, state) -> None:
        from mc2p.skills.navigation_state import NavigationState
        if type(state) is not NavigationState:
            raise ContractViolation("point policy requires NavigationState")
        self._ledger = state.recovery_ledger(self.group)
        self._shared_cache = state.policy_cache(self.group)
        self._navigation_state = state
        if self.joint_planner is not None:
            self.joint_planner.bind_state(state)

    @property
    def selected_waypoint(self) -> Vec3V0 | None:
        return self._selected_waypoint

    @property
    def recovery(self):
        return self._ledger.snapshot()

    def clear(self) -> None:
        if self.controller is not None:
            self.controller.clear()
        self._gate.clear()
        self._scope = None
        self._last_sequence = -1
        self._selected_waypoint = None
        self._feedback_snapshot = None
        self._planning_summary = {}
        if self.joint_planner is not None:
            self.joint_planner.clear()

    def bind_feedback_snapshot(self, snapshot: MemorySnapshot) -> None:
        if type(snapshot) is not MemorySnapshot:
            raise ContractViolation("point policy feedback requires MemorySnapshot")
        self._feedback_snapshot = snapshot

    def feedback(self, selected: bool, view: PlaygroundView, now_ns: int) -> None:
        if type(selected) is not bool or type(view) is not PlaygroundView:
            raise ContractViolation("point policy feedback requires selected bool and view")
        require_nonnegative_int(now_ns, "point policy feedback time")
        if self.joint_planner is not None:
            if self._feedback_snapshot is None:
                raise ContractViolation('joint feedback requires later evidence')
            self.joint_planner.feedback(selected, self._feedback_snapshot, view, now_ns)
        elif self.controller is not None:
            snapshot = self._feedback_snapshot
            if snapshot is None or snapshot.latest is None:
                raise ContractViolation("active point policy requires post-observation evidence")
            self.controller.perception.feedback(selected, snapshot.latest, now_ns)
            self.controller.feedback(selected, view, now_ns)
        else:
            self._gate.feedback(selected, view, now_ns)
        self._feedback_snapshot = None

    def verify_control_sources(self) -> None:
        if self.joint_planner is not None:
            verify = getattr(self.joint_planner.capabilities, 'verify_current_sources', None)
            if callable(verify):
                verify()

    def revalidate(self, decision, snapshot, view, now_ns, *, belief):
        if self.joint_planner is None:
            return decision
        if self._navigation_state is None:
            raise ContractViolation('D/E requires a bound NavigationState')
        return self.joint_planner.revalidate(decision,snapshot,view,now_ns,
            belief=belief,memory_generation=self._navigation_state.memory_generation)

    def submission_deadline(self, default_deadline_ns: int) -> int:
        require_nonnegative_int(default_deadline_ns, 'point submission deadline')
        if self.joint_planner is None or self.joint_planner.selected is None:
            return default_deadline_ns
        return min(default_deadline_ns,self.joint_planner.selected.valid_until_ns)

    @staticmethod
    def _cell_body(cell: tuple[int, int], floor: int) -> AabbV3:
        x, z = cell[0] + .5, cell[1] + .5
        return AabbV3(x-BODY_MARGIN, floor+1, z-BODY_MARGIN,
                      x+BODY_MARGIN, floor+2.8, z+BODY_MARGIN)

    @staticmethod
    def _terrain(snapshot: MemorySnapshot) -> dict[tuple[int, int, int], TerrainHistory]:
        return snapshot.terrain_index

    def _cell_reason(self, cell, floor, now_ns, terrain, belief, *, support_reason=None) -> str | None:
        grid = (cell[0], floor, cell[1])
        record = terrain.get(grid)
        marker = belief.get(grid)
        reason = (_support_reason if support_reason is None else support_reason)(
            record, now_ns, historical=self.group == "C",
            contradicted=False if marker is None else marker.contradicted,
            terrain=terrain, belief=belief, block=grid, view=self._current_view,
        )
        if reason is not None:
            return reason
        body = self._cell_body(cell, floor)
        for other, boxes, _ in terrain.collision_candidates(body):
            if boxes is None:
                bx, by, bz = other.block.position
                if overlaps(body, AabbV3(bx, by, bz, bx+1, by+1, bz+1)):
                    marker = belief.get(other.block.position)
                    return ("control_unavailable" if marker is None or marker.history != other
                            else "missing_field")
            elif any(overlaps(body, box) for box in boxes):
                marker = belief.get(other.block.position)
                if marker is None or marker.history != other:
                    return "control_unavailable"
                return "contradiction" if marker.contradicted else "known_obstacle"
        for entity in self._current_view.base.entities:
            entity_box = AabbV3(
                entity.position.x-entity.size.x/2, entity.position.y,
                entity.position.z-entity.size.z/2,
                entity.position.x+entity.size.x/2,
                entity.position.y+entity.size.y,
                entity.position.z+entity.size.z/2,
            )
            if overlaps(body, entity_box):
                return "known_obstacle"
        return None

    def _cell_open(self, cell, floor, now_ns, terrain, belief) -> bool:
        return self._cell_reason(cell, floor, now_ns, terrain, belief) is None

    def _route(self, snapshot, view, now_ns, goal, belief, started_ns):
        own = view.base.own
        assert own is not None
        floor = round(own.position.y) - 1
        terrain = self._terrain(snapshot)
        self._current_view = view
        start = (math.floor(own.position.x), math.floor(own.position.z))
        target = (math.floor(goal.position.x), math.floor(goal.position.z))
        budget_ns = self.config.planning_budget_ns
        candidates_checked = 0
        rejected: list[tuple[tuple[int, int], str]] = []
        traversable = set()
        reasons = {}
        radius = self.config.planning_radius_blocks
        radius_squared = radius * radius
        nearby = {
            (position[0], position[2])
            for position in terrain.positions_in_grid(
                (math.floor(own.position.x-radius-1), floor,
                 math.floor(own.position.z-radius-1)),
                (math.ceil(own.position.x+radius+1), floor,
                 math.ceil(own.position.z+radius+1)),
            )
            if ((position[0]+.5-own.position.x) ** 2
                + (position[2]+.5-own.position.z) ** 2) <= radius_squared
        }

        def cell_reason(cell):
            nonlocal candidates_checked
            if cell in reasons:
                return reasons[cell]
            if time.perf_counter_ns()-started_ns >= budget_ns:
                return "planning_budget_exhausted"
            candidates_checked += 1
            reason = self._cell_reason(cell, floor, now_ns, terrain, belief)
            if reason is None and cell not in nearby:
                reason = "outside_known_planning_area"
            reasons[cell] = reason
            if reason is None:
                traversable.add(cell)
            else:
                rejected.append((cell, reason))
            return reason

        immediate = [start, (start[0]+1, start[1]), (start[0], start[1]+1),
                     (start[0]-1, start[1]), (start[0], start[1]-1)]
        for cell in (target, *immediate):
            cell_reason(cell)
        if time.perf_counter_ns()-started_ns >= budget_ns:
            return (), dict(expansions=0, cells_checked=candidates_checked,
                candidates=(), cell_rejections=tuple(rejected[:64]),
                summary_truncated=len(rejected) > 64, budget_exhausted=True)
        queue = []
        distances: dict[tuple[int, int], int] = {}
        if target in traversable:
            distances[target] = 0
            heapq.heappush(queue, (
                abs(target[0]-start[0]) + abs(target[1]-start[1]),
                0,
                target,
            ))
        expansions = 0
        settled = set()
        required = {cell for cell in immediate if cell in traversable}
        while queue and expansions < self.config.max_expansions:
            if time.perf_counter_ns()-started_ns >= budget_ns:
                break
            _, cost, cell = heapq.heappop(queue)
            if cell in settled or cost != distances.get(cell):
                continue
            settled.add(cell)
            expansions += 1
            if required.issubset(settled):
                break
            for neighbor in ((cell[0]+1, cell[1]), (cell[0], cell[1]+1),
                             (cell[0]-1, cell[1]), (cell[0], cell[1]-1)):
                if cell_reason(neighbor) is not None:
                    continue
                next_cost = cost + 1
                if next_cost < distances.get(neighbor, 1 << 30):
                    distances[neighbor] = next_cost
                    heuristic = abs(neighbor[0]-start[0]) + abs(neighbor[1]-start[1])
                    heapq.heappush(
                        queue, (next_cost + heuristic, next_cost, neighbor),
                    )
        scored = []
        for cell in immediate:
            if cell not in traversable:
                reason = cell_reason(cell)
                if reason is not None and (cell, reason) not in rejected:
                    rejected.append((cell, reason))
                continue
            point = Vec3V0(cell[0]+.5, own.position.y, cell[1]+.5)
            connector = math.hypot(point.x-own.position.x, point.z-own.position.z)
            if connector < .01 and cell == start:
                continue
            desired_yaw = math.degrees(math.atan2(-(point.x-own.position.x),
                                                   point.z-own.position.z))
            path_cost = distances.get(cell, 100+math.hypot(
                point.x-goal.position.x, point.z-goal.position.z))
            record = terrain[(cell[0], floor, cell[1])]
            age_cost = max(0, now_ns-record.last_seen.request_start_ns)/1_000_000_000*.05
            turn_cost = abs(wrap(desired_yaw-own.yaw))/180*.25
            score = float(path_cost)+connector+age_cost+turn_cost
            scored.append((score, cell, point, age_cost, turn_cost))
        scored.sort(key=lambda item: (item[0], item[1]))
        exhausted = time.perf_counter_ns()-started_ns >= budget_ns
        return tuple(item[2] for item in scored[:self.config.max_candidates]), dict(
            expansions=expansions, cells_checked=candidates_checked,
            candidates=tuple((item[1], item[0], item[3], item[4]) for item in scored[:self.config.max_candidates]),
            cell_rejections=tuple(rejected[:64]), summary_truncated=len(rejected) > 64,
            budget_exhausted=exhausted,
        )

    @staticmethod
    def _revision(report, snapshot, belief) -> str:
        terrain = snapshot.terrain_index
        values = []
        for gap in report.gaps:
            record = terrain.get(gap.block)
            marker = None if gap.block is None else belief.get(gap.block)
            geometry = None if record is None else (
                record.block.collision.kind, record.block.collision.boxes,
                record.block.fluid_id,
            )
            values.append((gap.block, gap.reason, geometry,
                           False if marker is None else marker.contradicted))
        return repr(tuple(sorted(values, key=repr)))

    def _budget_expired(self, started_ns: int) -> bool:
        return time.perf_counter_ns()-started_ns >= self.config.planning_budget_ns

    def _finish_summary(self, started_ns, route, rejections) -> bool:
        elapsed = time.perf_counter_ns()-started_ns
        self._planning_summary = dict(route)
        self._planning_summary.update(
            elapsed_ns=elapsed,
            planning_budget_ns=self.config.planning_budget_ns,
            budget_exhausted=bool(route.get("budget_exhausted"))
                or elapsed >= self.config.planning_budget_ns,
            segment_rejections=tuple(rejections[:64]),
            summary_truncated=bool(route.get("summary_truncated")) or len(rejections) > 64,
        )
        if self._shared_cache is not None:
            self._shared_cache["planning_summary"] = dict(self._planning_summary)
        return bool(self._planning_summary["budget_exhausted"])

    def _replace_summary_rejections(self, route, rejections) -> None:
        self._planning_summary.update(
            segment_rejections=tuple(rejections[:64]),
            summary_truncated=bool(route.get("summary_truncated"))
                or len(rejections) > 64,
        )
        if self._shared_cache is not None:
            self._shared_cache["planning_summary"] = dict(self._planning_summary)

    def _budget_blocked(self, started_ns, route, rejections=()):
        exhausted_route = dict(route)
        exhausted_route["budget_exhausted"] = True
        self._selected_waypoint = None
        self._finish_summary(started_ns, exhausted_route, rejections)
        return NavigationDecision(state="blocked", reason="planning_budget_exhausted")

    def _observe_recovery(self, snapshot, view, now_ns, goal, reason, route, started_ns):
        own = view.base.own
        assert own is not None
        key = f"observe/{math.floor(own.position.x)},{math.floor(own.position.z)}/{reason}"
        # Stable decision identity: actual geometry/admission states, never the
        # sequence number or a refreshed-but-equivalent evidence timestamp.
        revision = repr((reason, route.get("cell_rejections", ())))
        observe_summary = ((key, reason, "observe"),)
        if self._finish_summary(started_ns, route, observe_summary):
            return self._budget_blocked(started_ns, route, observe_summary)
        if not self._ledger.allow(key, revision, now_ns):
            self._replace_summary_rejections(
                route, ((key, reason, "wait"),)
            )
            return NavigationDecision(state="blocked", reason="bounded_recovery_wait")
        self._ledger.fail(key, revision, now_ns)
        yaw = math.degrees(math.atan2(-(goal.position.x-own.position.x),
                                       goal.position.z-own.position.z))
        look = self._gate.request(view, yaw, 45., now_ns)
        state = "searching" if look != LookV1() else "waiting_observation"
        return NavigationDecision(look=look, state=state,
                                  reason="observe_"+reason)

    def _bc_decide(self, snapshot, view, now_ns, goal, belief, started_ns):
        base, own = view.base, view.base.own
        if (snapshot.invalid_reason is not None or snapshot.latest is None
                or not snapshot.latest.available or not base.available or own is None
                or not 0 <= now_ns-base.request_start_ns <= self.config.freshness_ns):
            return NavigationDecision(reason="navigation_observation_unavailable")
        if abs(own.position.y-round(own.position.y)) > .01 or abs(goal.position.y-own.position.y) > .1:
            return NavigationDecision(state="blocked", reason="unsupported_motion")
        if inside_goal(goal, own.position):
            return NavigationDecision(state="holding_distance", reason="point_goal_region")
        candidates, route = self._route(snapshot, view, now_ns, goal, belief, started_ns)
        if route["budget_exhausted"] or self._budget_expired(started_ns):
            return self._budget_blocked(started_ns, route)
        if not candidates:
            categories = {reason for _, reason in route["cell_rejections"]}
            for recoverable in ("missing_support", "uncertain_history"):
                if recoverable in categories:
                    return self._observe_recovery(snapshot, view, now_ns, goal,
                                                  recoverable, route, started_ns)
            self._finish_summary(started_ns, route, ())
            reason = next(iter(sorted(categories)), "no_admissible_candidate")
            return NavigationDecision(state="blocked", reason=reason)

        rejected = 0
        rejection_summary = []
        for waypoint in candidates:
            if self._budget_expired(started_ns):
                return self._budget_blocked(started_ns, route, rejection_summary)
            desired_report = check_normal_segment(
                snapshot, view, now_ns, waypoint,
                historical=self.group == "C", belief=belief,
            )
            if self._budget_expired(started_ns):
                return self._budget_blocked(started_ns, route, rejection_summary)
            if desired_report.reason is not None:
                key = (f"{own.position.x:.6f},{own.position.z:.6f}->"
                       f"{waypoint.x:.6f},{waypoint.z:.6f}")
                revision = self._revision(desired_report, snapshot, belief)
                if self._ledger.allow(key, revision, now_ns):
                    self._ledger.fail(key, revision, now_ns)
                rejection_summary.append((key, desired_report.reason, revision))
                rejected += 1
                continue
            yaw = math.degrees(math.atan2(-(waypoint.x-own.position.x),
                                           waypoint.z-own.position.z))
            self._selected_waypoint = waypoint
            angle = wrap(yaw-own.yaw)
            if abs(angle) > 8:
                reason = "alternate_connection_turn" if rejected else "turn_to_connection"
                if self._finish_summary(started_ns, route, rejection_summary):
                    return self._budget_blocked(started_ns, route, rejection_summary)
                look = self._gate.request(view, yaw, own.pitch, now_ns)
                return NavigationDecision(look=look, state="searching", reason=reason)
            # Forward execution follows actual current yaw.  Desired connector
            # geometry cannot replace the segment the client will really take.
            length = math.hypot(waypoint.x-own.position.x, waypoint.z-own.position.z)
            radians = math.radians(own.yaw)
            actual_end = Vec3V0(
                own.position.x-math.sin(radians)*length, own.position.y,
                own.position.z+math.cos(radians)*length,
            )
            actual_report = check_normal_segment(
                snapshot, view, now_ns, actual_end,
                historical=self.group == "C", belief=belief,
            )
            if self._budget_expired(started_ns):
                return self._budget_blocked(started_ns, route, rejection_summary)
            if actual_report.reason is not None:
                rejection_summary.append(("actual_heading", actual_report.reason,
                    self._revision(actual_report, snapshot, belief)))
                rejected += 1
                continue
            reason = "alternate_bounded_step" if rejected else "bounded_step"
            if self._finish_summary(started_ns, route, rejection_summary):
                return self._budget_blocked(started_ns, route, rejection_summary)
            return NavigationDecision(MovementV1(forward=1), LookV1(),
                                      "following", reason)
        self._selected_waypoint = None
        if self._finish_summary(started_ns, route, rejection_summary):
            return self._budget_blocked(started_ns, route, rejection_summary)
        return NavigationDecision(state="blocked", reason="bounded_recovery_wait")

    def decide(self, snapshot: MemorySnapshot, view: PlaygroundView, now_ns: int,
               goal: PointGoal, *, belief: BeliefStore) -> NavigationDecision:
        if type(snapshot) is not MemorySnapshot or type(view) is not PlaygroundView:
            raise ContractViolation("point policy requires formal snapshot and view")
        if type(goal) is not PointGoal or type(belief) is not BeliefStore:
            raise ContractViolation("point policy requires PointGoal and BeliefStore")
        require_nonnegative_int(now_ns, "point policy decision time")
        if goal.scope_id != snapshot.scope_id or belief.scope_id != snapshot.scope_id:
            raise ContractViolation("point navigation scope mismatch")
        self._planning_summary = {}
        self._selected_waypoint = None
        if self.joint_planner is not None:
            self.joint_planner.begin_frame()
        if now_ns >= goal.deadline_ns:
            return NavigationDecision(state="blocked", reason="point_goal_deadline")
        base = view.base
        scope = (snapshot.scope_id, base.episode_id,
                 base.controller_clock_id, base.client_clock_id)
        if self._scope is not None and scope != self._scope:
            return NavigationDecision(state="blocked", reason="navigation_scope_changed")
        self._scope = scope
        if base.sequence_id <= self._last_sequence:
            return NavigationDecision(reason="awaiting_new_observation")
        self._last_sequence = base.sequence_id
        started_ns = time.perf_counter_ns()

        if self.joint_planner is not None:
            if self._navigation_state is None:
                raise ContractViolation('D/E requires a bound NavigationState')
            result = self.joint_planner.decide(snapshot,view,goal,now_ns,
                belief=belief,memory_generation=self._navigation_state.memory_generation)
            selected = self.joint_planner.selected
            self._selected_waypoint = None if selected is None else selected.endpoint
            return result

        if self.controller is not None:
            self.controller.perception.begin_control_frame(
                deadline_ns=min(goal.deadline_ns, now_ns+self.config.step_lease_ns)
            )
            own = base.own
            floor = None if own is None or abs(own.position.y-round(own.position.y)) > .01 \
                else round(own.position.y)-1
            return self.controller.decide(
                snapshot, view, now_ns, goal.position, MovementV1(forward=1), floor,
                stop_distance=0, target_track_id=None,
            )
        return self._bc_decide(snapshot, view, now_ns, goal, belief, started_ns)
