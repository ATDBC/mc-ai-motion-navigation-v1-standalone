"""Finite D/E route and gaze selection over lawful terrain and one shared body."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import math
import time

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.navigation_controller import NavigationDecision
from mc2p.skills.navigation_joint_cost import joint_score, union_duration_ns
from mc2p.skills.navigation_look import wrap
from mc2p.skills.navigation_recovery import RecoveryLedger
from mc2p.skills.normal_direction_control import bounded_look, world_direction
from mc2p.skills.normal_navigation_guard import check_control_proposal, check_normal_segment
from mc2p.skills.normal_navigation_types import ControlProposal, JointCandidate, NormalNavigationConfig
from mc2p.skills.normal_control_capabilities import ControlCapabilities
from mc2p.skills.normal_yaw_feedback import choose_feedback_movement
from mc2p.skills.point_goal import inside_goal


@dataclass(frozen=True, slots=True)
class ObservationNeed:
    need_id: str
    block: tuple[int, int, int]
    reason: str
    priority: int
    required_by_ns: int
    evidence_after_ns: int


def look_axes(look: LookV1) -> str:
    yaw, pitch = bool(look.yaw_delta_degrees), bool(look.pitch_delta_degrees)
    return 'yaw_pitch' if yaw and pitch else 'yaw' if yaw else 'pitch' if pitch else 'fixed'


def _yaw(origin, endpoint):
    return math.degrees(math.atan2(-(endpoint.x-origin.x), endpoint.z-origin.z))


def _route_id(endpoint):
    if endpoint.x % 1 == .5 and endpoint.z % 1 == .5 and endpoint.y % 1 == 0:
        return f'cell/{math.floor(endpoint.x)}/{math.floor(endpoint.z)}'
    # Noncenter observation positions can share a cell with a regular route.
    # Hex preserves each finite coordinate exactly without frame-dependent IDs.
    coordinates = (float(value).hex().replace('+', '') if value else '0x0.0p0'
                   for value in (endpoint.x, endpoint.y, endpoint.z))
    return 'point/' + '/'.join(coordinates)


def _ray_hits(start, end, box) -> bool:
    """Segment/AABB intersection of retained geometry; unknown stays unknown."""
    lo, hi = 0., 1.
    for a, b, low, high in zip(start, end,
            (box.min_x, box.min_y, box.min_z), (box.max_x, box.max_y, box.max_z)):
        delta = b-a
        if abs(delta) < 1e-10:
            if a < low or a > high:
                return False
        else:
            first, last = sorted(((low-a)/delta, (high-a)/delta))
            lo, hi = max(lo, first), min(hi, last)
            if hi < lo:
                return False
    return hi > 1e-5 and lo < 1.-1e-5


class JointPlanner:
    stage_mode = False
    static_history = False

    def _connector(self, snapshot, view, now_ns, waypoint, belief):
        return check_normal_segment(snapshot,view,now_ns,waypoint,historical=True,belief=belief)

    def _gaze_candidates(self, snapshot, view, waypoint, gazes):
        return gazes

    def _guard_candidate(self,snapshot,view,now_ns,proposal,memory_generation,belief):
        report=check_control_proposal(snapshot,view,now_ns,proposal,historical=True,
            memory_generation=memory_generation,belief=belief,static_history=self.static_history)
        return proposal,report

    def __init__(self, group: str, *, control_capabilities=None,
                 prelook_enabled: bool = True, force_route_id: str | None = None):
        if group not in {'D', 'E'}:
            raise ContractViolation('joint planner group must be D or E')
        if type(control_capabilities) is not ControlCapabilities:
            raise ContractViolation('D/E requires validated control capabilities')
        if type(prelook_enabled) is not bool:
            raise ContractViolation('prelook switch must be boolean')
        if force_route_id is not None and (type(force_route_id) is not str or not force_route_id):
            raise ContractViolation('forced route id must be a nonempty string')
        self.group, self.capabilities = group, control_capabilities
        self.config = NormalNavigationConfig(group)
        self.prelook_enabled, self.force_route_id = prelook_enabled, force_route_id
        # Reuse only the C geometry/search helper. Evidence and recovery belong
        # to the caller's NavigationState, never to a second controller/runtime.
        from mc2p.skills.point_goal_policy import PointGoalPolicy
        self._routes = PointGoalPolicy('C')
        self._ledger = RecoveryLedger()
        self._cache = None
        self._route_origin = None
        self._route_endpoint = None
        self._route_goal = None
        self._previous_movement = MovementV1()
        self._pending_need = None
        self._pending_selection_ns = None
        self._last_feedback = {}
        self._failed_routes = {}
        self._force_completed_ns = None
        self.selected = None
        self.candidates = ()
        self.needs = ()
        self._diagnostic = {}

    @property
    def diagnostic(self):
        snapshot = copy.deepcopy(self._diagnostic)
        if self._cache is not None:
            # Publish the same detached snapshot that the caller records.  The
            # cache is diagnostic-only; making a second deep copy here used to
            # duplicate a large candidate tree on every 20 Hz control frame.
            self._cache['planning_summary'] = snapshot
        return snapshot

    @property
    def last_feedback(self):
        return dict(self._last_feedback)

    def begin_frame(self):
        self._diagnostic = dict(planned=False,expansions=0,cells_checked=0,
            candidates=(),cell_rejections=(),segment_rejections=(),elapsed_ns=0,
            planning_budget_ns=self.config.planning_budget_ns,budget_exhausted=False,
            summary_truncated=False,joint_revision=1,joint_candidates=(),needs=(),
            selected_candidate_id=None,route_id=None,proposal=None,acquired_ns=None,
            feedback=self.last_feedback,prelook_enabled=self.prelook_enabled,
            force_route_id=self.force_route_id,force_route_completed=self._force_completed_ns is not None,
            force_route_completed_ns=self._force_completed_ns)

    def bind_state(self, state):
        self._ledger = state.recovery_ledger(self.group)
        self._cache = state.policy_cache(self.group)

    def clear(self):
        self.selected = None
        self.candidates = ()
        self.needs = ()
        self._pending_need = None
        self._pending_selection_ns = None
        self._route_origin = self._route_endpoint = None
        self._route_goal = None
        self._previous_movement = MovementV1()
        self._diagnostic = {}
        self._last_feedback = {}
        self._failed_routes = {}

    def _timing(self, name, fallback):
        values = dict(getattr(self.capabilities, 'measurements', ()))
        value = values.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            return fallback, True
        return float(value), False

    def _occluded(self, snapshot, position, block):
        target = (block[0]+.5, block[1]+1.001, block[2]+.5)
        eye = (position.x, position.y+1.62, position.z)
        # Geometry predicts only known obstruction. Its absence is not proof
        # that the ray succeeds; acquisition always needs an actual later stamp.
        from mc2p.contracts.observation_v3 import AabbV3
        bounds = AabbV3(*(min(a,b)-1e-6 for a,b in zip(eye,target)),
                        *(max(a,b)+1e-6 for a,b in zip(eye,target)))
        for record, boxes, potential in snapshot.terrain_index.collision_candidates(bounds):
            if record.block.position == block:
                continue
            if any(_ray_hits(eye, target, box) for box in potential):
                return True
        return False

    def _make_needs(self, snapshot, view, goal, now_ns, routes, route_summary, belief):
        own = view.base.own
        candidates = {}
        goal_yaw = _yaw(own.position,goal.position)
        gx,gz = world_direction(goal_yaw,1,0)
        # Bounded task corridor queries name unknown cells without inventing
        # their contents. This includes a useful branch just beyond a corner.
        for distance in (1.,2.,3.):
            block = (math.floor(own.position.x+gx*distance),round(own.position.y)-1,
                     math.floor(own.position.z+gz*distance))
            if snapshot.terrain_index.get(block) is None:
                candidates[block] = 'missing_support'
        # Only gaps that can change the next goal-directed route are useful.
        for cell, reason in route_summary.get('cell_rejections', ()):
            distance = math.hypot(cell[0]+.5-own.position.x, cell[1]+.5-own.position.z)
            point = Vec3V0(cell[0]+.5, own.position.y, cell[1]+.5)
            if reason not in {'missing_support','uncertain_history','missing_field','contradiction'}:
                continue
            if distance > 2.5 or math.hypot(point.x-goal.position.x, point.z-goal.position.z) > math.hypot(own.position.x-goal.position.x, own.position.z-goal.position.z)+.1:
                continue
            block = (cell[0], round(own.position.y)-1, cell[1])
            candidates[block] = reason
        # A future prefix may need refresh even when this tick is safe. Use
        # the original evidence age plus a conservative unmeasured one second.
        for point in routes[:4]:
            yaw = _yaw(own.position, point)
            dx, dz = world_direction(yaw, 1, 0)
            block = (math.floor(point.x+dx), round(own.position.y)-1, math.floor(point.z+dz))
            record = snapshot.terrain_index.get(block)
            marker = belief.get(block)
            if record is None:
                candidates.setdefault(block, 'missing_support')
            elif marker is not None and marker.contradicted:
                candidates.setdefault(block, 'contradiction')
            elif now_ns-record.last_seen.request_start_ns+1_000_000_000 > 60_000_000_000:
                candidates.setdefault(block, 'uncertain_history')
        ordered = sorted(candidates.items(), key=lambda row: (
            math.hypot(row[0][0]+.5-goal.position.x,row[0][2]+.5-goal.position.z), row[0]))[:8]
        return tuple(ObservationNeed(
            'terrain/'+'/'.join(map(str, block)), block, reason,
            0 if reason in {'missing_support','missing_field','contradiction'} else 1,
            min(goal.deadline_ns, now_ns+1_000_000_000),
            now_ns if snapshot.terrain_index.get(block) is None else
                snapshot.terrain_index[block].last_seen.request_start_ns,
        ) for block, reason in ordered)

    def select(self, candidates, now_ns):
        if type(candidates) is not tuple or len(candidates) > 16:
            raise ContractViolation('joint candidates must be tuple with at most sixteen entries')
        valid = tuple(c for c in candidates if c.valid_until_ns > now_ns)
        if self.force_route_id is not None and self._force_completed_ns is None:
            valid = tuple(c for c in valid if c.route_id == self.force_route_id)
        if not valid:
            return None
        # With no admissible movement, an unresolved necessary gap cannot be
        # avoided forever by scoring a cheap stationary hold.
        if not any(c.proposal.movement != MovementV1() for c in valid):
            sensing = tuple(c for c in valid if c.need_id is not None)
            if sensing:
                valid = sensing
        if self.group == 'D':
            route_id = min(valid, key=lambda c: (c.route_score, c.route_id)).route_id
            previous = None if self.selected is None else next(
                (c for c in valid if c.candidate_id == self.selected.candidate_id),None)
            best_route = min(c.route_score for c in valid if c.route_id == route_id)
            if previous is not None and previous.route_score-best_route < .10:
                route_id = previous.route_id
            best = min((c for c in valid if c.route_id == route_id), key=lambda c:
                (c.need_priority, c.required_by_ns if c.required_by_ns is not None else 2**63,
                 c.need_id or '~', c.gaze_debt, c.candidate_id))
            return best
        else:
            best = min(valid, key=lambda c: (joint_score(c), c.candidate_id))
        previous = None if self.selected is None else next(
            (c for c in valid if c.candidate_id == self.selected.candidate_id), None)
        # Only a newly checked candidate can be retained; an old proposal itself
        # is never reused. Necessary deadlines override preference immediately.
        earlier_need = (previous is not None and best.need_priority == previous.need_priority
                        and best.required_by_ns is not None
                        and (previous.required_by_ns is None or best.required_by_ns < previous.required_by_ns))
        if previous is not None and previous.need_priority <= best.need_priority and not earlier_need:
            if joint_score(previous)-joint_score(best) < .10:
                best = previous
        return best

    def _proposal(self, snapshot, view, waypoint, yaw, pitch, generation):
        own = view.base.own
        look = bounded_look(own.yaw, own.pitch, yaw, pitch, self.config.interval_ns)
        if look_axes(look) == 'yaw_pitch' and not any(
                self.capabilities.allows(MovementV1(forward=f,strafe=s),'yaw_pitch')
                for f in (-1,0,1) for s in (-1,0,1)):
            # Axis evidence is not evidence for their composition. Continue
            # toward the same task gaze using a measured single-axis control.
            if abs(wrap(yaw-own.yaw)) > 3.:
                look = bounded_look(own.yaw,own.pitch,yaw,own.pitch,self.config.interval_ns)
            else:
                look = bounded_look(own.yaw,own.pitch,own.yaw,pitch,self.config.interval_ns)
        moving = math.hypot(waypoint.x-own.position.x, waypoint.z-own.position.z) > .08
        movement = MovementV1()
        if moving:
            travel_yaw = _yaw(own.position, waypoint)
            origin = own.position
            if self._route_endpoint == waypoint and self._route_origin is not None:
                origin = self._route_origin
                travel_yaw = _yaw(origin, waypoint)
            remaining = math.hypot(waypoint.x-own.position.x, waypoint.z-own.position.z)
            movement = choose_feedback_movement(
                (own.position.x, own.position.z), (own.velocity.x, own.velocity.z),
                (origin.x, origin.z), travel_yaw, own.yaw+look.yaw_delta_degrees,
                self._previous_movement, gaze_step=look.yaw_delta_degrees,
                remaining_movement_ticks=1 if remaining <= .35 else None)
            if not self.capabilities.allows(movement, look_axes(look)):
                movement = MovementV1()
        if not self.capabilities.allows(movement, look_axes(look)):
            return None
        dx, dz = world_direction(own.yaw+look.yaw_delta_degrees, movement.forward, movement.strafe)
        endpoint = Vec3V0(own.position.x+.45*dx, own.position.y, own.position.z+.45*dz)
        return ControlProposal(snapshot.scope_id, generation, snapshot.latest.stamp,
            movement, look, own.position, own.velocity, own.yaw, own.pitch, endpoint)

    def _finish(self, started, summary, rejections, decision, selected=None):
        elapsed = time.perf_counter_ns()-started
        exhausted = elapsed >= self.config.planning_budget_ns or bool(summary.get('budget_exhausted'))
        if exhausted:
            selected = None
            decision = NavigationDecision(state='blocked', reason='planning_budget_exhausted')
        self.selected = selected
        if selected is not None:
            if self._route_endpoint != selected.endpoint:
                self._route_origin = selected.proposal.origin
                self._route_endpoint = selected.endpoint
            self._pending_need = next((n for n in self.needs if n.need_id == selected.need_id), None)
            self._pending_selection_ns = selected.proposal.based_on.received_at_ns
        self._diagnostic = dict(
            planned=True, expansions=summary.get('expansions', 0),
            cells_checked=summary.get('cells_checked',0), candidates=summary.get('candidates',()),
            cell_rejections=tuple(summary.get('cell_rejections',()))[:64],
            segment_rejections=tuple(rejections)[:64], elapsed_ns=elapsed,
            planning_budget_ns=self.config.planning_budget_ns, budget_exhausted=exhausted,
            summary_truncated=bool(summary.get('summary_truncated')) or len(rejections)>64,
            joint_revision=1, route_count=len({c.route_id for c in self.candidates}),
            joint_candidates=tuple(dict(
                candidate_id=c.candidate_id, route_id=c.route_id, endpoint=c.endpoint,
                proposal=c.proposal, gaze_kind=c.gaze_kind, need_id=c.need_id,
                required_by_ns=c.required_by_ns, expected_acquired_ns=c.expected_acquired_ns,
                need_priority=c.need_priority,
                intervals=c.intervals, union_duration_ns=union_duration_ns(c.intervals),
                progress_debt_seconds=c.progress_debt_seconds, recovery_seconds=c.recovery_seconds,
                uncertainty_penalty=c.uncertainty_penalty, gaze_debt=c.gaze_debt,
                score=joint_score(c), route_score=c.route_score,
                valid_until_ns=c.valid_until_ns, missing_measurements=c.missing_measurements,
            ) for c in self.candidates),
            needs=tuple(asdict(n) for n in self.needs),
            selected_candidate_id=None if selected is None else selected.candidate_id,
            route_id=None if selected is None else selected.route_id,
            proposal=None if selected is None else selected.proposal,
            original_route=None if selected is None else (self._route_origin, selected.endpoint),
            need_id=None if selected is None else selected.need_id,
            required_by_ns=None if selected is None else selected.required_by_ns,
            expected_acquired_ns=None if selected is None else selected.expected_acquired_ns,
            acquired_ns=None, prelook_enabled=self.prelook_enabled,
            force_route_id=self.force_route_id, feedback=dict(self._last_feedback),
            force_route_completed=self._force_completed_ns is not None,
            force_route_completed_ns=self._force_completed_ns,
        )
        return decision

    def _gazes_and_need(self,snapshot,view,waypoint,travel_yaw,goal):
        own=view.base.own
        useful = [n for n in self.needs if not self._occluded(snapshot, waypoint, n.block)]
        need = min(useful,key=lambda n:(n.priority,n.required_by_ns,n.need_id)) if useful else None
        gazes = [('hold',own.yaw,own.pitch,None),('front',travel_yaw,45.,None)]
        if need is not None and (self.prelook_enabled or waypoint == own.position):
            target = Vec3V0(need.block[0]+.5,need.block[1]+1.001,need.block[2]+.5)
            pitch = math.degrees(math.atan2(own.position.y+1.62-target.y,
                max(.001,math.hypot(target.x-own.position.x,target.z-own.position.z))))
            if self._occluded(snapshot,own.position,need.block):
                # The route endpoint is a lawful, clear predicted vantage;
                # finish the known prefix before charging/attempting sensing.
                gazes.append(('reposition',travel_yaw,own.pitch,need))
            else:
                gazes.append(('need',_yaw(own.position,target),pitch,need))
        # A task gaze is valuable only when it differs from the route and
        # points toward a relevant gap; merely exposing more blocks earns zero.
        if (self.prelook_enabled and need is not None and not self._occluded(snapshot,own.position,need.block)
                and abs(wrap(_yaw(own.position,goal.position)-travel_yaw)) > 5):
            gazes.append(('task',_yaw(own.position,goal.position),45.,need))
        gazes = self._gaze_candidates(snapshot,view,waypoint,gazes)
        return gazes,need

    def decide(self, snapshot, view, goal, now_ns, *, belief, memory_generation):
        require_nonnegative_int(now_ns, 'joint decision time')
        started = time.perf_counter_ns()
        own = view.base.own
        previous = self.selected
        self.candidates = ()
        self.needs = ()
        if (snapshot.latest is None or not snapshot.latest.available or own is None
                or not view.base.available or now_ns >= goal.deadline_ns
                or not 0 <= now_ns-view.base.request_start_ns <= self.config.freshness_ns):
            return self._finish(started, {}, (), NavigationDecision(reason='navigation_observation_unavailable'))
        if inside_goal(goal, own.position):
            return self._finish(started, {}, (), NavigationDecision(state='holding_distance', reason='point_goal_region'))
        reuse_route = (
            not self.stage_mode
            and previous is not None
            and self._route_goal == goal
            and _route_id(previous.endpoint).startswith('cell/')
            and math.hypot(previous.endpoint.x-own.position.x,
                           previous.endpoint.z-own.position.z) > .15
        )
        if reuse_route:
            cell = (math.floor(previous.endpoint.x), math.floor(previous.endpoint.z))
            routes = (previous.endpoint,)
            summary = dict(
                expansions=0, cells_checked=0,
                candidates=((cell, previous.route_score, 0., 0.),),
                cell_rejections=(), summary_truncated=False,
                budget_exhausted=False,
            )
        else:
            routes, summary = self._routes._route(
                snapshot, view, now_ns, goal, belief, started,
            )
            routes = routes[:4]
            self._route_goal = goal
        if summary['budget_exhausted']:
            return self._finish(started, summary, (), NavigationDecision())
        # Include the still-useful old route without trusting its old admission.
        if not self.stage_mode and previous is not None and previous.endpoint not in routes and math.hypot(
                previous.endpoint.x-own.position.x, previous.endpoint.z-own.position.z) > .15:
            routes = (previous.endpoint,)+routes[:3]
        self.needs = self._make_needs(snapshot, view, goal, now_ns, routes, summary, belief)
        # A nearby observed side connector may lead to a useful vantage one
        # cell farther out. Bound this search to the existing four directions,
        # inspect real support/geometry, and replace at most one route.
        if not self.stage_mode and self.needs and routes:
            need = self.needs[0]
            if self._occluded(snapshot,own.position,need.block) and not any(
                    not self._occluded(snapshot,point,need.block) for point in routes):
                for point in routes:
                    if time.perf_counter_ns()-started >= self.config.planning_budget_ns:
                        break
                    dx,dz = world_direction(_yaw(own.position,point),1,0)
                    vantage = Vec3V0(point.x+dx,own.position.y,point.z+dz)
                    if self._occluded(snapshot,vantage,need.block):
                        continue
                    report = check_normal_segment(snapshot,view,now_ns,vantage,
                        historical=True,belief=belief)
                    if report.reason is None:
                        routes = routes[:3]+(vantage,)
                        break
        if not routes:
            routes = (own.position,)
        routes = tuple(dict.fromkeys(routes))
        scores = {tuple(cell): score for cell,score,_,_ in summary.get('candidates',())}
        built, rejections = [], []
        guard_cache = {}
        for waypoint in routes:
            if time.perf_counter_ns()-started >= self.config.planning_budget_ns:
                break
            route_id = _route_id(waypoint)
            failed_revision = self._failed_routes.get(route_id)
            if failed_revision is not None:
                if not self._ledger.allow(route_id,failed_revision,now_ns):
                    rejections.append(dict(key=route_id,reason='bounded_recovery_wait'))
                    continue
            route_score = max(0., scores.get((math.floor(waypoint.x),math.floor(waypoint.z)),
                math.hypot(waypoint.x-goal.position.x,waypoint.z-goal.position.z)))
            connector = self._connector(snapshot,view,now_ns,waypoint,belief)
            if connector.reason is not None and waypoint != own.position:
                rejections.append(dict(key=route_id,reason=connector.reason))
                continue
            travel_yaw = _yaw(own.position, waypoint)
            gazes,need = self._gazes_and_need(snapshot,view,waypoint,travel_yaw,goal)
            for kind,yaw,pitch,gaze_need in gazes[:4]:
                if time.perf_counter_ns()-started >= self.config.planning_budget_ns:
                    break
                proposal = self._proposal(snapshot,view,waypoint,yaw,pitch,memory_generation)
                if proposal is None:
                    rejections.append(dict(key=route_id+'/'+kind,reason='control_unavailable'))
                    continue
                cache_key = (proposal.movement,proposal.look,proposal.endpoint)
                guarded = guard_cache.get(cache_key)
                if guarded is None:
                    guarded = self._guard_candidate(snapshot,view,now_ns,proposal,memory_generation,belief)
                    guard_cache[cache_key] = guarded
                proposal,report=guarded
                # A stationary look is a safe release request even if missing
                # support prevents proving the inertia envelope. Never authorize
                # a moving proposal on that exception.
                if report.reason is not None and proposal.movement != MovementV1():
                    rejections.append(dict(key=route_id+'/'+kind,reason=report.reason))
                    continue
                missing = []
                speed, missing_speed = self._timing('normal_speed_blocks_per_second',1.)
                look_rate, missing_look = self._timing('look_rate_degrees_per_second',60.)
                delivery, missing_delivery = self._timing('sample_delivery_ns',1_000_000_000)
                if missing_speed: missing.append('normal_speed_blocks_per_second')
                if missing_look: missing.append('look_rate_degrees_per_second')
                if missing_delivery: missing.append('sample_delivery_ns')
                move_ns = (1_000_000_000 if missing_speed else int(1e9*math.hypot(
                    waypoint.x-own.position.x,waypoint.z-own.position.z)/speed))
                angle = math.hypot(wrap(yaw-own.yaw),pitch-own.pitch)
                look_ns = (1_000_000_000 if missing_look and angle else int(1e9*angle/look_rate))
                observing = gaze_need is not None
                future_turn_ns = 0
                if kind == 'reposition':
                    target = Vec3V0(gaze_need.block[0]+.5,gaze_need.block[1]+1.001,gaze_need.block[2]+.5)
                    future_pitch = math.degrees(math.atan2(waypoint.y+1.62-target.y,
                        max(.001,math.hypot(target.x-waypoint.x,target.z-waypoint.z))))
                    future_angle = abs(wrap(_yaw(waypoint,target)-yaw))+abs(future_pitch-pitch)
                    future_turn_ns = 1_000_000_000 if missing_look else int(future_angle/look_rate*1e9)
                observe_start = max(move_ns,look_ns)+future_turn_ns if kind == 'reposition' else look_ns
                observe_end = observe_start+(int(delivery) if observing else 0)
                # Moving and turning share time. Delivery follows the turn,
                # and no future prefix receives permission from this forecast.
                intervals = ((0,move_ns),(0,look_ns))
                if kind == 'reposition':
                    intervals += ((max(move_ns,look_ns),observe_start),)
                if observing:
                    intervals += ((observe_start,observe_end),)
                age = 0.
                for position in snapshot.terrain_index.positions_in_grid(
                        (math.floor(proposal.endpoint.x-1.5), round(own.position.y)-1, math.floor(proposal.endpoint.z-1.5)),
                        (math.ceil(proposal.endpoint.x+1.5), round(own.position.y)-1, math.ceil(proposal.endpoint.z+1.5))):
                    gap_record = snapshot.terrain_index[position]
                    bx,by,bz = gap_record.block.position
                    if by == round(own.position.y)-1 and abs(bx+.5-proposal.endpoint.x)<1.5 and abs(bz+.5-proposal.endpoint.z)<1.5:
                        age = max(age,min(1.,max(0,now_ns-gap_record.last_seen.request_start_ns)/60e9))
                blocked_motion = proposal.movement == MovementV1() and waypoint != own.position
                debt = route_score/speed + (1. if blocked_motion else 0.)
                # Missing relevant information has task cost until selected
                # sensing could arrive; no reward for irrelevant sideways views.
                gaze_debt = (1. if need is not None and not observing else 0.)
                if observing:
                    gaze_debt = min(1.,max(0,now_ns+observe_end-gaze_need.required_by_ns)/1e9)
                candidate = JointCandidate(route_id+'/'+kind,waypoint,proposal,
                    None if gaze_need is None else gaze_need.need_id,
                    None if gaze_need is None else gaze_need.required_by_ns,
                    intervals,debt,0.,age,gaze_debt,
                    min(goal.deadline_ns,now_ns+self.config.step_lease_ns,
                        snapshot.latest.stamp.request_start_ns+self.config.freshness_ns),
                    route_id,route_score,kind,9 if gaze_need is None else gaze_need.priority,
                    None if gaze_need is None else now_ns+observe_end,tuple(missing))
                built.append(candidate)
        self.candidates = tuple(built[:16])
        chosen = self.select(self.candidates,now_ns)
        if chosen is None:
            reason = 'requested_route_unavailable' if self.force_route_id else 'no_admissible_candidate'
            return self._finish(started,summary,rejections,NavigationDecision(state='blocked',reason=reason))
        moving = chosen.proposal.movement != MovementV1()
        reason = 'joint_bounded_step' if moving else 'joint_observe' if chosen.need_id else 'control_limited_turn'
        return self._finish(started,summary,rejections,NavigationDecision(
            chosen.proposal.movement,chosen.proposal.look,'following' if moving else 'searching',reason),chosen)

    def revalidate(self, decision, snapshot, view, now_ns, *, belief, memory_generation):
        selected = self.selected
        if selected is None or not self._diagnostic.get('planned'):
            return decision
        report = check_control_proposal(snapshot,view,now_ns,selected.proposal,
            historical=True,memory_generation=memory_generation,belief=belief,static_history=self.static_history)
        reason = report.reason
        if now_ns >= selected.valid_until_ns:
            reason = 'proposal_expired'
        if decision.movement != selected.proposal.movement or decision.look != selected.proposal.look:
            reason = 'proposal_mismatch'
        if not self.capabilities.allows(selected.proposal.movement,look_axes(selected.proposal.look)):
            reason = 'control_unavailable'
        if reason is not None and (decision.movement != MovementV1()
                or reason in {'proposal_expired','proposal_mismatch','control_unavailable'}):
            self._diagnostic['submit_rejection'] = reason
            self.selected = None
            return NavigationDecision(state='blocked',reason='joint_recheck_'+reason)
        return decision

    def feedback(self, selected, snapshot, view, now_ns):
        proposal = None if self.selected is None else self.selected.proposal
        own = view.base.own
        feedback = dict(executed=selected, received_at_ns=now_ns,
                        lateral_error_blocks=None, acquired_ns=None, information_lead_ns=None,
                        need_id=None if self._pending_need is None else self._pending_need.need_id,
                        required_by_ns=None if self._pending_need is None else self._pending_need.required_by_ns,
                        candidate_id=None if self.selected is None else self.selected.candidate_id,
                        route_id=None if self.selected is None else self.selected.route_id)
        if proposal is not None and own is not None and self._route_origin is not None:
            route_yaw = _yaw(self._route_origin,self.selected.endpoint)
            dx,dz = world_direction(route_yaw,1,0)
            lateral = ((own.position.x-self._route_origin.x)*dz
                       -(own.position.z-self._route_origin.z)*dx)
            feedback['lateral_error_blocks'] = lateral
            self._previous_movement = proposal.movement if selected else MovementV1()
            if (selected and self.force_route_id == self.selected.route_id
                    and self._force_completed_ns is None and math.hypot(
                        own.position.x-self.selected.endpoint.x,
                        own.position.z-self.selected.endpoint.z) <= .15):
                self._force_completed_ns = now_ns
                feedback['force_route_completed_ns'] = now_ns
            if not selected or abs(lateral) > .10 or own.horizontal_collision:
                key = self.selected.route_id
                revision = repr((self.selected.endpoint,own.horizontal_collision,round(lateral,2)))
                if self._ledger.allow(key,revision,now_ns):
                    self._ledger.fail(key,revision,now_ns)
                self._failed_routes[key] = revision
                self.selected = None
                self._route_origin = self._route_endpoint = None
                feedback['failure_reason'] = 'control_feedback_rejected'
        need = self._pending_need
        if need is not None:
            record = snapshot.terrain_index.get(need.block)
            if record is not None and record.last_seen.request_start_ns > need.evidence_after_ns:
                # receipt and required_by share controller clock; the JVM sample
                # interval is retained separately and never subtracted from them.
                acquired = record.last_seen.received_at_ns
                feedback['acquired_ns'] = acquired
                feedback['information_lead_ns'] = need.required_by_ns-acquired
                feedback['client_sample'] = record.last_seen.client_sample
                self._pending_need = None
        self._last_feedback = feedback
        return dict(feedback)
