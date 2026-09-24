"""Coordinate local execution without granting new observation or motion rights."""
from dataclasses import replace
import math
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.navigation_controller import NavigationDecision
from mc2p.skills.navigation_joint_policy import JointPlanner, look_axes, _yaw
from mc2p.skills.normal_direction_control import world_direction
from mc2p.skills.normal_navigation_guard import check_control_proposal


class NavigationExecutionMixin:
    """Own execution facts independently of the speculative stage checkpoint."""

    def __init__(self, **kwargs):
        from mc2p.skills.navigation_execution import NavigationExecution
        self.execution = NavigationExecution()
        super().__init__(**kwargs)
        self._reset_execution()

    def _reset_execution(self):
        from mc2p.skills.navigation_execution import NavigationExecution
        self.execution = NavigationExecution()
        self._feedback_sequence = -1
        self._execution_now = 0
        self._execution_belief = None
        self._blocked_entries = {}
        self._entry_revision = 0
        self._repair_attempt = 0
        self._last_escalation_ns = None
        self.last_execution_feedback = {}
        self._progress_position = None
        self._recovery_distance = 0.
        self._safe_stops = []
        self._wait_signature = None
        self._last_route_signature = None
        self._budget_wait_signature = None
        self._budget_retry_at_ns = 0
        self._retreat_target = None
        self._entry_pose = None
        self._execution_waypoint = None
        self._entry_cost_history = {}
        self._cost_window = None
        self._cost_revision = 0
        self.stage.execution_coordinator = self
        self.stage.selector.connection_filter = self.connection_allowed
        self.stage.selector.edge_filter = self.edge_allowed
        self.stage.selector.entry_cost = self.entry_cost
        self.stage.selector.entry_cost_revision = self._cost_revision
        from mc2p.skills.navigation_terrain_review import TerrainReviewQueue
        self.terrain_review = TerrainReviewQueue(unified_motion=True)

    def clear(self):
        super().clear()
        self._reset_execution()

    def _stage_diagnostic(self):
        super()._stage_diagnostic()
        self._diagnostic['joint_revision'] = 5
        self._diagnostic['execution'] = self.execution.diagnostic(self._execution_now)

    def decide(self, snapshot, view, goal, now_ns, *, belief, memory_generation):
        binding = (memory_generation, snapshot.scope_id,
                   None if snapshot.latest is None else snapshot.latest.stamp.scope,
                   goal.goal_id, goal.position)
        if binding != self._stage_binding:
            self.clear()
            self._stage_binding = binding
        self._execution_now = now_ns
        self.execution.begin(now_ns, binding)
        self._execution_belief = belief
        self._entry_pose = view.base.own
        self._review_blocked_candidate = False
        self._budget_sensing = False
        escalated = False
        reason = self.execution.deadline_reason(now_ns)
        if reason == 'no_progress':
            # A failed entry is not a failed destination. Block this local
            # direction, then let the existing stage search find another entry.
            if self._last_escalation_ns is None or now_ns-self._last_escalation_ns >= 3_000_000_000:
                if self._execution_waypoint is not None and view.base.own is not None:
                    key = self.entry_key(view.base.own.position, self._execution_waypoint)
                    self._blocked_entries[key] = self.entry_dependencies(key)
                    self._entry_revision += 1
                self._repair_attempt += 1
                self._close_cost_window()
                self._last_escalation_ns = now_ns
                escalated = True
                self.execution.set_phase('replanning', now_ns, reason='local_entry_exhausted')
                self.stage.plan = None
                self.stage.tail = ()
                self._route_origin = self._route_endpoint = None
                self._wait_signature = None
            if len(self._blocked_entries) >= 12:
                self.execution.set_phase('blocked', now_ns, reason='local_entries_exhausted')
                reason = 'local_entries_exhausted'
            else:
                reason = None
        if reason is not None:
            self.execution.enforce_deadline(now_ns)
            self.selected = None
            self.candidates = self.needs = ()
            self._pending_need = None
            self._route_origin = self._route_endpoint = None
            self._review_checkpoint = self._review_belief = None
            self._review_evaluated = False
            self._review_path = ()
            self.stage.work = dict(mode='idle',cell_hits=0,cell_misses=0)
            self.begin_frame()
            return NavigationDecision(state='blocked', reason='navigation_recovery_'+reason)
        decision = super().decide(snapshot, view, goal, now_ns, belief=belief,
                                  memory_generation=memory_generation)
        if (escalated and decision.movement == MovementV1() and not self.needs
                and not self._diagnostic.get('budget_exhausted')):
            self._choose_retreat(snapshot, view, now_ns, belief)
        if (self.selected is not None and view.base.own is not None
                and self.selected.endpoint != view.base.own.position):
            self._execution_waypoint = self.selected.endpoint
        return decision

    def _choose_retreat(self, snapshot, view, now_ns, belief):
        from mc2p.skills.navigation_stage_goals import known_segment
        if view.base.own is None:
            return
        own = view.base.own.position
        candidates = list(reversed(self._safe_stops))
        candidates += [Vec3V0(math.floor(own.x)+.5+dx,own.y,math.floor(own.z)+.5+dz)
                       for dx,dz in ((1,0),(-1,0),(0,1),(0,-1))]
        for target in candidates[:12]:
            if not .35 <= math.hypot(target.x-own.x,target.z-own.z) <= 4:
                continue
            if not self.connection_allowed(own,target):
                continue
            if known_segment(snapshot,view,now_ns,own,target,belief,control_margin=True,
                             static_history=True).reason is None:
                self._retreat_target = target
                return

    def entry_key(self, start, end):
        sx, sz = math.floor(start.x), math.floor(start.z)
        dx, dz = end.x-start.x, end.z-start.z
        direction = ((1 if dx > 0 else -1), 0) if abs(dx) > abs(dz) else (0, 1 if dz > 0 else -1)
        # Only a real heading change can change this approach identity. The
        # global problem deadline and attempts survive heading/route churn.
        yaw = 0. if self._entry_pose is None else self._entry_pose.yaw
        heading = math.floor((yaw+22.5) / 45) % 8
        return sx, sz, direction[0], direction[1], heading

    def connection_allowed(self, start, end):
        return self.entry_key(start, end) not in self._blocked_entries

    def _problem(self, now_ns, reason, key):
        if self.execution.problem_started_ns is None:
            self._repair_attempt += 1
        self.execution.problem(now_ns, reason, key)

    def _make_needs(self,snapshot,view,goal,now_ns,routes,summary,belief):
        needs=super()._make_needs(snapshot,view,goal,now_ns,routes,summary,belief)
        refreshable={'missing_support','uncertain_history','missing_field','contradiction'}
        result=[]
        for need in needs:
            if need.need_id.startswith('terrain-review/'):
                result.append(need)
                continue
            x,y,z=need.block
            if self.stage.selector._trusted_occlusion((x,z),y,snapshot.terrain_index,belief,now_ns):
                continue
            reason=self.stage.selector.geometry._cell_reason((x,z),y,now_ns,snapshot.terrain_index,belief)
            if reason in refreshable:
                result.append(need)
        return tuple(result)

    def entry_cost(self, start, end):
        """Estimate repeated handling, in block-equivalent stage score units.

        Eight-direction controls can turn while moving, so no unconditional
        yaw charge is added. Only actual stationary handling intervals count;
        simultaneous turn/observation intervals are recorded once.
        """
        key = self.entry_key(start, end)
        if self._cost_window is not None and self._cost_window[0] == key:
            return 0.  # Continuing handling must not repay elapsed waiting.
        record = self._entry_cost_history.get(key)
        if record is None or record[1] != self.entry_dependencies(key):
            return 0.
        speed, _ = self._timing('normal_speed_blocks_per_second', 6.)
        return record[0] / 1e9 * speed

    def _close_cost_window(self):
        if self._cost_window is None:
            return
        key, elapsed, dependencies = self._cost_window
        self._cost_window = None
        if elapsed and (key in self._entry_cost_history or len(self._entry_cost_history) < 64):
            self._entry_cost_history[key] = (elapsed, dependencies)
            self._cost_revision += 1
            self.stage.selector.entry_cost_revision = self._cost_revision

    def entry_dependencies(self, key):
        cache = self.stage.selector.geometry.cache
        x,z,dx,dz,_ = key
        points = tuple(sorted((p,fact) for p,fact in cache.facts.items()
            if min(x,x+dx)-1<=p[0]<=max(x,x+dx)+1 and min(z,z+dz)-1<=p[2]<=max(z,z+dz)+1))
        entities = None if cache.entities is None else (cache.entities[0],tuple(
            (p,size) for p,size in cache.entities[1]
            if abs(p.x-(x+.5))<=2+size.x/2 and abs(p.z-(z+.5))<=2+size.z/2))
        return points,entities

    def edge_allowed(self, start, end):
        own = self._entry_pose
        if own is None or start != (math.floor(own.position.x), math.floor(own.position.z)):
            return True
        return self.connection_allowed(own.position, Vec3V0(end[0]+.5, own.position.y, end[1]+.5))

    def route_override(self, snapshot, view, now_ns, goal, belief, started, prepared):
        own = view.base.own
        cache = self.stage.selector.geometry.cache
        expired = [key for key,facts in self._blocked_entries.items()
                   if facts != self.entry_dependencies(key)]
        for key in expired:
            del self._blocked_entries[key]
        if expired:
            self._entry_revision += 1
        stale_costs = [key for key, (_, facts) in self._entry_cost_history.items()
                       if facts != self.entry_dependencies(key)]
        for key in stale_costs:
            del self._entry_cost_history[key]
        if stale_costs:
            self._cost_revision += 1
            self.stage.selector.entry_cost_revision = self._cost_revision
        signature = (own.position, own.velocity, cache.entities,
                     tuple(sorted(cache.facts.items())), self._entry_revision,
                     math.floor((own.yaw+22.5)/45)%8)
        self._last_route_signature = signature
        if self._budget_wait_signature == signature and now_ns < self._budget_retry_at_ns:
            self.stage.work = dict(mode='idle', cell_hits=cache.hits, cell_misses=cache.misses)
            if self.stage.plan is not None and self.stage.plan.information:
                self._budget_sensing = True
                self._wait_signature = None
                summary=dict(expansions=0,cells_checked=0,candidates=(),cell_rejections=(),
                             summary_truncated=False,budget_exhausted=False)
                # Keep sensing the unfinished stage while the next full search
                # waits. This carries no old translation or connector grant.
                self.stage.plan=replace(self.stage.plan,waypoint=own.position,path=(own.position,),
                    connector=None,summary=summary)
                self.stage.selector.diagnostic=dict(reason='budget_observe_stage',target=self.stage.plan.target,
                    path=(own.position,),scores=(),attempts=len(self.stage.selector.attempts))
                self.stage.work['mode']='reused'
                return (own.position,),summary
            return (), dict(expansions=0,cells_checked=0,candidates=(),cell_rejections=(),
                            summary_truncated=False,budget_exhausted=True)
        if self._retreat_target is not None:
            if math.hypot(own.position.x-self._retreat_target.x, own.position.z-self._retreat_target.z) < .25:
                self._retreat_target = None
                self.stage.plan = None
                self.stage.tail = ()
                self._wait_signature = None
            else:
                return self._retreat_route(snapshot, view, now_ns, belief, self._retreat_target)
        if self._wait_signature != signature or self.stage.scanning:
            return None
        # Geometry has already been refreshed above. This stores no old
        # movement permission, only the unchanged lack of an entry.
        self.stage.work = dict(mode='reused', cell_hits=cache.hits, cell_misses=cache.misses)
        summary = dict(expansions=0, cells_checked=0, candidates=(), cell_rejections=(),
                       summary_truncated=False, budget_exhausted=False)
        if self.stage.plan is not None:
            self.stage.plan = replace(self.stage.plan, waypoint=own.position, summary=summary, connector=None)
        return (own.position,), summary

    def _retreat_route(self, snapshot, view, now_ns, belief, target):
        from mc2p.skills.navigation_stage_goals import known_segment, StagePlan
        own = view.base.own.position
        report = known_segment(snapshot, view, now_ns, own, target, belief,
                               control_margin=True, static_history=True)
        if report.reason is not None:
            self._retreat_target = None
            return None
        summary = dict(expansions=0, cells_checked=0,
                       candidates=(((math.floor(target.x), math.floor(target.z)), 0., 0., 0.),),
                       cell_rejections=(), summary_truncated=False, budget_exhausted=False)
        self.stage.plan = StagePlan(target, target, (own, target), (), 'retreat', summary, report)
        self.stage.tail = (target,)
        self.stage.selector.diagnostic = dict(reason='retreat', target=target, path=(own,target),
                                               scores=(), attempts=len(self.stage.selector.attempts))
        self.stage.work = dict(mode='reused', cell_hits=0, cell_misses=0)
        return (target,), summary

    def _proposal(self, snapshot, view, waypoint, yaw, pitch, generation):
        self._candidate_waypoint = waypoint
        return super()._proposal(snapshot, view, waypoint, yaw, pitch, generation)

    def _guard_candidate(self, snapshot, view, now_ns, proposal, memory_generation, belief):
        # Consider alternatives inside this one gaze/entry, retaining the
        # original short controller and joint candidate scoring contracts.
        original = proposal
        moving = proposal.movement != MovementV1()
        options = [proposal]
        if moving:
            target = self._candidate_waypoint
            tx, tz = world_direction(_yaw(view.base.own.position, target), 1, 0)
            alternatives = []
            for f, s in ((1,0),(1,1),(0,1),(-1,1),(-1,0),(-1,-1),(0,-1),(1,-1)):
                movement = MovementV1(forward=f, strafe=s)
                if movement == proposal.movement or not self.capabilities.allows(movement, look_axes(proposal.look)):
                    continue
                dx, dz = world_direction(proposal.yaw+proposal.look.yaw_delta_degrees, f, s)
                progress = dx*tx+dz*tz
                if progress <= 0:
                    continue
                endpoint = Vec3V0(proposal.origin.x+.45*dx, proposal.origin.y, proposal.origin.z+.45*dz)
                alternatives.append((-progress, f, s, replace(proposal, movement=movement, endpoint=endpoint)))
            options += [row[-1] for row in sorted(alternatives)[:3]]
        first_report = None
        for candidate in options:
            if self.terrain_review.due_for(candidate, now_ns):
                self._review_blocked_candidate = True
                continue
            report = check_control_proposal(snapshot, view, now_ns, candidate, historical=True,
                                           memory_generation=memory_generation, belief=belief, static_history=True)
            if first_report is None:
                first_report = report
            if report.reason is None:
                return candidate, report
        if moving:
            proposal = replace(original, movement=MovementV1(), endpoint=original.origin)
            self._stage_releases.append(proposal)
        return JointPlanner._guard_candidate(self, snapshot, view, now_ns, proposal, memory_generation, belief)

    def revalidate(self, decision, snapshot, view, now_ns, *, belief, memory_generation):
        # The legacy static parent blocks every movement when any need is due.
        # Check only the dependencies of the actual selected control instead.
        checked = JointPlanner.revalidate(self, decision, snapshot, view, now_ns,
                                         belief=belief, memory_generation=memory_generation)
        if checked.movement != MovementV1() and self.selected is not None and self.terrain_review.due_for(self.selected.proposal, now_ns):
            self.selected = None
            self._diagnostic['submit_rejection'] = 'terrain_review_due'
            return NavigationDecision(state='blocked', reason='joint_recheck_terrain_review_due')
        return checked

    def _finish(self, *args, **kwargs):
        import copy
        facts = None
        if self._review_checkpoint is not None:
            facts = copy.copy(self.terrain_review)
            # Need records and their contexts are immutable; copy containers.
            facts.__dict__ = {key: value.copy() if isinstance(value,dict) else value
                              for key,value in self.terrain_review.__dict__.items()}
        checked_plan = self.stage.plan
        decision = super()._finish(*args, **kwargs)
        # Stage trials may roll back, completed execution measurements may not.
        self.stage.selector.entry_cost_revision = self._cost_revision
        if (decision.reason == 'no_admissible_candidate' and checked_plan is not None
                and self._entry_pose is not None):
            self.stage.plan = replace(checked_plan, waypoint=self._entry_pose.position, connector=None)
        if self._diagnostic.get('budget_exhausted') and facts is not None:
            self.terrain_review.preserve_facts_from(facts)
            self._diagnostic['terrain_review']['state_sha256'] = self.terrain_review.state_digest()
        now = self._execution_now
        if self._diagnostic.get('budget_exhausted'):
            self._problem(now, 'planning_budget_exhausted', 'planning')
            if now >= self._budget_retry_at_ns or self._budget_wait_signature != self._last_route_signature:
                self._budget_wait_signature = self._last_route_signature
                self._budget_retry_at_ns = now + 250_000_000
        elif self.stage.scanning:
            self._problem(now, 'initial_scan', 'scan')
            self.execution.set_phase('observing', now, reason='initial_scan')
        elif not self.stage.scanning and decision.reason != 'point_goal_region':
            moving = decision.movement != MovementV1()
            if not moving:
                observing = self.selected is not None and self.selected.need_id is not None
                turning = bool(decision.look.yaw_delta_degrees or decision.look.pitch_delta_degrees)
                reason = ('observation_required' if observing else
                          'terrain_review_due' if self._review_blocked_candidate else
                          'adjusting_entry' if turning else 'connection_unavailable')
                self._problem(now, reason, 'entry')
                phase = 'observing' if observing else 'adjusting' if turning else 'waiting'
                self.execution.set_phase(phase, now, reason=reason)
                if (not self._budget_sensing and (phase == 'waiting' or (
                        self.stage.plan is not None and self._entry_pose is not None
                        and self.stage.plan.waypoint == self._entry_pose.position))):
                    self._wait_signature = self._last_route_signature
                decision = replace(decision, reason=reason)
            elif self._retreat_target is not None:
                self.execution.set_phase('retreating', now, reason='safe_retreat')
            else:
                self._wait_signature = None
        self._stage_diagnostic()
        if self._cache is not None:
            self._cache['planning_summary'] = self.diagnostic
        return decision

    def feedback(self, selected, snapshot, view, now_ns):
        sequence = snapshot.latest.stamp.sequence_id
        if sequence <= self._feedback_sequence:
            return self.last_feedback
        previous = self.selected
        was_scanning = self.stage.scanning
        executed_attempt = self._repair_attempt
        executed_start = self._execution_now
        origin = self._route_origin
        reference = origin
        if reference is None and self._diagnostic.get('original_route') is not None:
            reference = self._diagnostic['original_route'][0]
        lateral = None
        if previous is not None and reference is not None and view.base.own is not None:
            dx, dz = world_direction(_yaw(reference, previous.endpoint), 1, 0)
            lateral = ((view.base.own.position.x-reference.x)*dz - (view.base.own.position.z-reference.z)*dx)
        # Preserve scan and information feedback but do not let a control error
        # or a lost arbitration write the legacy route-wide blacklist.
        self._route_origin = None
        feedback = super().feedback(selected, snapshot, view, now_ns)
        self._route_origin = origin
        feedback['lateral_error_blocks'] = lateral
        self._feedback_sequence = sequence
        self._execution_now = now_ns
        own = view.base.own
        moving = previous is not None and previous.proposal.movement != MovementV1()
        progress = False
        resolved = False
        if selected and previous is not None and own is not None:
            if not was_scanning and not moving and previous.endpoint != previous.proposal.origin:
                key = self.entry_key(previous.proposal.origin, previous.endpoint)
                dependencies = self.entry_dependencies(key)
                if self._cost_window is not None and (self._cost_window[0] != key
                        or self._cost_window[2] != dependencies):
                    self._close_cost_window()
                if self._cost_window is None:
                    self._cost_window = (key, 0, dependencies)
                if self._cost_window[0] == key:
                    self._cost_window = (key, self._cost_window[1] + max(0, now_ns-executed_start), dependencies)
            moved = math.hypot(own.position.x-previous.proposal.origin.x, own.position.z-previous.proposal.origin.z)
            before = math.hypot(previous.proposal.origin.x-previous.endpoint.x, previous.proposal.origin.z-previous.endpoint.z)
            after = math.hypot(own.position.x-previous.endpoint.x, own.position.z-previous.endpoint.z)
            turn = math.hypot((own.yaw-previous.proposal.yaw+180)%360-180,
                              own.pitch-previous.proposal.pitch)
            progress = (moving and before-after > .01) or (
                not moving and turn > .1) or feedback['acquired_ns'] is not None
            self.execution.observe_feedback(sequence, now_ns, selected,
                (own.position.x,own.position.y,own.position.z), progress, None,
                attempt_id=executed_attempt)
            if moving and lateral is not None and abs(lateral) > .10:
                self._recovery_distance = 0.
                self._problem(now_ns, 'tracking_deviation', previous.route_id)
                self.execution.set_phase('adjusting', now_ns, reason='tracking_deviation')
                # Every replacement still passes current connector and motion
                # checks. The true old-line deviation remains in this feedback.
                self._route_origin = self._route_endpoint = None
            elif moving and progress and moved > .01 and not own.horizontal_collision and self._retreat_target is None:
                if self.execution.problem_started_ns is not None:
                    self._recovery_distance += max(0., before-after)
                if self._recovery_distance >= .5:
                    self.execution.resolve(now_ns)
                    resolved = True
                    self._close_cost_window()
                    self._recovery_distance = 0.
            elif moving:
                self._recovery_distance = 0.
            if was_scanning and self.stage.scan_degrees >= 360.-1e-6:
                self.execution.resolve(now_ns)
                resolved = True
            if own.horizontal_collision:
                self._problem(now_ns, 'observed_collision', previous.route_id)
                self._route_origin = self._route_endpoint = None
            if not moving and math.hypot(own.velocity.x, own.velocity.z) < .01:
                from mc2p.skills.navigation_stage_goals import known_segment
                if self._execution_belief is not None and known_segment(snapshot, view, now_ns, own.position,
                        own.position, self._execution_belief, control_margin=True, static_history=True).reason is None:
                    if not self._safe_stops or math.hypot(own.position.x-self._safe_stops[-1].x,own.position.z-self._safe_stops[-1].z) > .5:
                        self._safe_stops = (self._safe_stops+[own.position])[-8:]
        else:
            self.execution.observe_feedback(sequence, now_ns, selected,
                None if own is None else (own.position.x,own.position.y,own.position.z), progress,
                None if selected else 'not_executed', attempt_id=executed_attempt)
        self.last_execution_feedback = dict(event_id=sequence, attempt_id=executed_attempt,
            progress=progress, resolved=resolved, state=self.execution.diagnostic(now_ns))
        self._last_feedback = dict(feedback)
        return feedback
