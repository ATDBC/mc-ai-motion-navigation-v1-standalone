"""Finite task-cost comparison; predictions never authorize a future movement."""
from __future__ import annotations

import math
import time

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.skills.gaze_controller import GazeController
from mc2p.skills.navigation_look import wrap
from mc2p.skills.navigation_motion import report_navigation_motion
from mc2p.skills.perception_confirmation import PerceptionConfirmation
from mc2p.skills.perception_needs import (
    AimConstraint, MotionProposal, PerceptionCandidate, PerceptionConfig,
    PerceptionDecision, PerceptionNeed, OWNERS,
)


def soft_cost(candidate, config):
    success,failure=candidate.outcomes
    def seconds(value):
        return config.unknown_seconds if value is None else value
    return (config.progress_weight*success.progress_debt
        + config.time_weight*seconds(success.elapsed_seconds)
        + config.unseen_weight*seconds(success.unseen_seconds)
        + config.turn_weight*candidate.turn_units
        + config.reversal_weight*candidate.reversals
        + config.stop_start_weight*candidate.stop_starts
        + config.failure_weight*seconds(failure.failure_seconds)
        + config.task_gaze_weight*candidate.task_gaze_debt)


def matching_motion_report(snapshot, view, proposal, report, now_ns):
    """Recompute the complete original guard, not its bounded evidence summary."""
    own=view.base.own
    if proposal is None or report is None or own is None or snapshot.latest is None:
        return False
    speed=math.hypot(own.velocity.x,own.velocity.z)
    jump=proposal.movement.jump or not own.on_ground
    horizon=max(4.5,14*speed) if jump else .45+2*speed
    if (proposal.scope_id!=snapshot.scope_id or proposal.based_on!=snapshot.latest.stamp
            or abs(wrap(proposal.yaw_degrees-own.yaw))>8
            or abs(proposal.horizon_blocks-horizon)>1e-6
            or proposal.movement.forward!=1 or proposal.movement.strafe!=0):
        return False
    expected=report_navigation_motion(snapshot,view,now_ns,proposal.floor,own.yaw,
        jump=jump,allowed_player_contact=proposal.allowed_player_contact)
    return (expected==report and report.reason is None and report.earliest_expiry_ns is not None
            and now_ns<=report.earliest_expiry_ns)


def executable_movement(candidate, look, snapshot, view, proposal, report, now_ns):
    if (candidate.motion!=proposal or proposal is None or look.yaw_delta_degrees!=0
            or abs(wrap(candidate.yaw_degrees-view.base.own.yaw))>8
            or not matching_motion_report(snapshot,view,proposal,report,now_ns)):
        return MovementV1()
    return proposal.movement


def _validate_needs(needs, config):
    if (type(needs) is not tuple or len(needs)>config.max_needs
            or any(type(need) is not PerceptionNeed for need in needs)):
        raise ContractViolation('invalid_or_excessive_perception_needs')
    if len({need.need_id for need in needs})!=len(needs):
        raise ContractViolation('duplicate_perception_need_ids')


def rank_candidates(candidates, snapshot, view, needs, proposal, report, now_ns, aim, config):
    _validate_needs(needs,config)
    if (type(candidates) is not tuple or len(candidates)>config.max_candidates
            or any(type(item) is not PerceptionCandidate for item in candidates)):
        raise ContractViolation('invalid_or_excessive_perception_candidates')
    if len({item.candidate_id for item in candidates})!=len(candidates):
        raise ContractViolation('duplicate_perception_candidate_ids')
    by_id={need.need_id:need for need in needs}
    ranked=[]
    # All candidates share this exact snapshot/time/proposal. Cache only within
    # this call; dispatch and the next observation still recompute the guard.
    movement_allowed=matching_motion_report(snapshot,view,proposal,report,now_ns)
    for candidate in candidates:
        if any(identifier not in by_id for identifier in candidate.need_ids):
            continue
        if not -90<=candidate.pitch_degrees<=90:
            continue
        if aim is not None and aim.active:
            if aim.need.need_id not in candidate.need_ids or candidate.motion is not None:
                continue
        if candidate.motion is not None and (candidate.motion!=proposal or not movement_allowed):
            continue
        addressed=[by_id[identifier] for identifier in candidate.need_ids]
        if any(now_ns>=need.deadline_ns for need in addressed):
            continue
        priority=max((need.priority for need in addressed),default=0)
        score=soft_cost(candidate,config)
        ranked.append(((-priority,score,candidate.candidate_id),candidate))
    return sorted(ranked,key=lambda item:item[0])


def _motion_identity(proposal):
    if proposal is None:
        return None
    # A fresh stamp alone is not a new task. Geometry/input changes are.
    return (proposal.scope_id,proposal.goal,proposal.floor,proposal.yaw_degrees,
            proposal.movement,proposal.horizon_blocks,proposal.allowed_player_contact)


def _need_identity(need):
    return (need.owner,need.scope_id,need.purpose,need.priority,need.point,
            need.condition,need.block,need.face,need.track_id)


class ActivePerceptionCoordinator:
    def __init__(self, variant, config, *, clock=time.perf_counter_ns):
        if variant not in {'smooth_only','active_perception_v1'} or type(config) is not PerceptionConfig:
            raise ContractViolation('invalid_perception_variant_or_config')
        self.variant,self.config,self._clock=variant,config,clock
        self.gaze=GazeController()
        self._pending={}
        self._owner=None
        self._scope=None
        self._motion=None
        self._no_progress=0
        self._status={}
        self._last_sequence=-1
        self._planning_elapsed_ns=()
        self._frame_deadline_ns=None
        self._frame_clock_start=None

    @property
    def planning_elapsed_ns(self):
        return self._planning_elapsed_ns

    def begin_control_frame(self, *, deadline_ns=None):
        """Bind the root's current action lease without renewing consumer budgets."""
        if deadline_ns is not None:
            require_nonnegative_int(deadline_ns,'perception frame deadline')
        self._frame_deadline_ns=deadline_ns
        self._planning_elapsed_ns=()
        self._frame_clock_start=None
        if deadline_ns is not None and self.variant=='active_perception_v1':
            self._frame_clock_start=self._clock()
            require_nonnegative_int(self._frame_clock_start,'planning frame clock')
            self._planning_elapsed_ns=(0,)
        self._status={'reason':'no_perception_request','no_progress':self._no_progress}

    def clear_control(self, reason, *, owner=None):
        if owner is not None and owner not in OWNERS:
            raise ContractViolation('invalid_perception_owner')
        if owner is not None and owner!=self._owner:
            return
        self.gaze.clear(reason)
        for confirmation,need in self._pending.values():
            confirmation.clear()
        self._pending.clear()
        self._owner=None
        self._motion=None
        # Diagnostic no-progress and outer M3/M5 budgets are not reset here.
        self._status={'reason':reason,'no_progress':self._no_progress}

    def status(self):
        return dict(self._status)

    def command_legacy(self, view, yaw, pitch, now_ns, *, owner):
        if self.variant!='smooth_only' or owner not in OWNERS:
            raise ContractViolation('legacy_gaze_requires_explicit_smooth_owner')
        self._owner=owner
        look=self.gaze.command(view,yaw,pitch,now_ns)
        self._status={'reason':'legacy_gaze','variant':self.variant,
            'config_revision':self.config.revision,'owner':owner,
            'requested_yaw_degrees':yaw,'requested_pitch_degrees':pitch,
            'boundary_events':self.gaze.boundary_events,'planning_ns':0,
            'candidate_count':0,'need_count':0,'no_progress':self._no_progress}
        return look

    def consume_observation_need(self, need, evidence, now_ns):
        """Let an attention consumer advance its task without commanding gaze twice.

        Movement confirmation remains exclusively inside decide with a newly
        bound full motion report. This method can only complete an observation.
        """
        if type(need) is not PerceptionNeed or need.condition=='motion_guard':
            raise ContractViolation('attention_consume_requires_nonmotion_need')
        pending=self._pending.get(need.need_id)
        if pending is None:
            return False
        confirmation,old=pending
        if (_need_identity(old)!=_need_identity(need) or need.based_on!=evidence.stamp
                or need.deadline_ns>old.deadline_ns):
            confirmation.clear(); del self._pending[need.need_id]
            return False
        result=confirmation.consume(evidence,now_ns)
        if result:
            del self._pending[need.need_id]
        return result

    def feedback(self, selected, evidence, now_ns):
        if type(selected) is not bool:
            raise ContractViolation('perception feedback must be boolean')
        for confirmation,need in self._pending.values():
            confirmation.feedback(selected,evidence,now_ns)
        if not selected:
            self.clear_control('not_selected')

    def _neutral(self, reason):
        self.clear_control(reason)
        return PerceptionDecision(MovementV1(),LookV1(),reason,None,(),(),None)

    def decide(self, snapshot, view, needs, proposal, report, now_ns, deadline_ns, *, aim=None):
        if self.variant!='active_perception_v1':
            raise ContractViolation('smooth_variant_has_no_joint_planner')
        require_nonnegative_int(now_ns,'perception now')
        require_nonnegative_int(deadline_ns,'perception deadline')
        if self._frame_deadline_ns is not None:
            deadline_ns=min(deadline_ns,self._frame_deadline_ns)
        _validate_needs(needs,self.config)
        if aim is not None and type(aim) is not AimConstraint:
            raise ContractViolation('invalid_perception_aim')
        start=self._clock()
        require_nonnegative_int(start,'planning clock')
        self._planning_elapsed_ns=(0,)
        offset=0
        if self._frame_clock_start is not None:
            offset=start-self._frame_clock_start
            require_nonnegative_int(offset,'planning frame offset')
            self._planning_elapsed_ns+=(offset,)
            start=self._frame_clock_start
        # The compute budget starts at coordinator entry. The action lease and
        # evidence age still include the preceding root/route work.
        budget=min(offset+self.config.planning_budget_ns,max(0,deadline_ns-now_ns))
        def elapsed_time():
            stamp=self._clock()
            require_nonnegative_int(stamp,'planning clock')
            elapsed=stamp-start
            if elapsed<self._planning_elapsed_ns[-1] or len(self._planning_elapsed_ns)>=8:
                raise ContractViolation('planning_clock_regressed_or_exceeded_sample_bound')
            self._planning_elapsed_ns+= (elapsed,)
            return elapsed
        def exhausted():
            return elapsed_time()>=budget
        if exhausted():
            return self._neutral('planning_budget_exhausted')
        latest,base=snapshot.latest,view.base
        if (snapshot.invalid_reason or latest is None or not latest.available or base.own is None
                or not base.available or now_ns<base.received_at_ns
                or not 0<=now_ns-base.request_start_ns<=500_000_000):
            return self._neutral('perception_observation_unavailable')
        stamp=latest.stamp
        if (stamp.scope!=(base.episode_id,base.controller_clock_id,base.client_clock_id)
                or stamp.sequence_id!=base.sequence_id or stamp.request_start_ns!=base.request_start_ns
                or stamp.received_at_ns!=base.received_at_ns or latest.pose is None
                or latest.pose.position!=base.own.position
                or latest.pose.yaw!=base.own.yaw or latest.pose.pitch!=base.own.pitch
                or stamp.client_sample.started_at_monotonic_ns!=base.client_sample_start_ns
                or stamp.client_sample.completed_at_monotonic_ns!=base.client_sample_end_ns
                or latest.pose.on_ground!=base.own.on_ground
                or latest.pose.horizontal_collision!=base.own.horizontal_collision
                or latest.pose.pose!=view.pose or latest.entities!=base.entities
                or latest.coverage is None
                or latest.coverage.entities_truncated!=base.entities_truncated
                or latest.blocks!=base.observed_blocks):
            return self._neutral('perception_observation_mismatch')
        scope=(snapshot.scope_id,stamp.scope,stamp.source_backend)
        if self._scope is not None and self._scope!=scope:
            self.clear_control('perception_scope_changed')
            self._scope=scope
            self._last_sequence=-1
            return self._neutral('perception_scope_changed')
        self._scope=scope
        if base.sequence_id<=self._last_sequence:
            return self._neutral('repeated_perception_sample')
        self._last_sequence=base.sequence_id
        if any(need.scope_id!=snapshot.scope_id or need.based_on!=stamp
               or now_ns>=need.deadline_ns for need in needs):
            return self._neutral('perception_need_mismatch_or_expired')
        if aim is not None and aim.active:
            expected=next((need for need in needs if need.need_id==aim.need.need_id),None)
            source=aim.need.based_on
            if (expected is None or _need_identity(expected)!=_need_identity(aim.need)
                    or expected.deadline_ns>aim.need.deadline_ns
                    or source.scope!=stamp.scope or source.source_backend!=stamp.source_backend
                    or source.sequence_id>stamp.sequence_id or source.received_at_ns>stamp.received_at_ns):
                return self._neutral('perception_aim_mismatch')
        if base.gui_open or base.own.dead:
            return self._neutral('perception_body_unavailable')
        identity=_motion_identity(proposal)
        if identity!=self._motion:
            for identifier,(confirmation,need) in tuple(self._pending.items()):
                if need.condition=='motion_guard':
                    confirmation.clear(); del self._pending[identifier]
        completed=[]
        had_pending=bool(self._pending)
        current_needs={need.need_id:need for need in needs}
        for identifier,(confirmation,need) in self._pending.items():
            current=current_needs.get(identifier)
            if current is None or _need_identity(current)!=_need_identity(need):
                confirmation.clear()
                continue
            bound=need.condition!='motion_guard' or matching_motion_report(snapshot,view,proposal,report,now_ns)
            if bound and confirmation.consume(latest,now_ns,motion_report=report):
                if identifier in {item.need_id for item in needs}:
                    completed.append(identifier)
        if completed:
            self._no_progress=0
        elif had_pending:
            self._no_progress=min(2**31-1,self._no_progress+1)
        self._pending.clear()
        from mc2p.skills.perception_candidates import generate_candidates
        candidates=generate_candidates(snapshot,view,needs,proposal,now_ns,config=self.config)
        ranked=rank_candidates(candidates,snapshot,view,needs,proposal,report,now_ns,aim,self.config)
        if exhausted():
            return self._neutral('planning_budget_exhausted')
        if not ranked:
            return self._neutral('no_lawful_perception_candidate')
        ordering,chosen=ranked[0]
        look=self.gaze.command(view,chosen.yaw_degrees,chosen.pitch_degrees,now_ns,
                               precise=bool(aim is not None and aim.active
                                            or any(need.condition in {'motion_guard','center_block_face','observed_block'} and need.need_id in chosen.need_ids
                                                   for need in needs)
                                            or chosen.kind in {'heading_track','heading_track_partial','joint_partial',
                                                               'route_track_partial'}))
        if exhausted():
            return self._neutral('planning_budget_exhausted')
        chosen_needs=[need for need in needs if need.need_id in chosen.need_ids and need.need_id not in completed]
        for need in chosen_needs:
            confirmation=PerceptionConfirmation()
            confirmation.request(need,chosen.yaw_degrees,chosen.pitch_degrees)
            self._pending[need.need_id]=(confirmation,need)
        self._owner=max(chosen_needs,key=lambda need:need.priority).owner if chosen_needs else None
        self._motion=identity
        elapsed=elapsed_time()
        if elapsed>=budget:
            return self._neutral('planning_budget_exhausted')
        # Planning consumed real time: recheck the same original evidence at
        # its actual dispatch age, never extend its deadline by computation.
        movement=executable_movement(chosen,look,snapshot,view,proposal,report,now_ns+elapsed)
        elapsed=elapsed_time()
        if elapsed>=budget:
            return self._neutral('planning_budget_exhausted')
        if (movement!=MovementV1() and (report.earliest_expiry_ns is None
                or now_ns+elapsed>report.earliest_expiry_ns)):
            movement=MovementV1()
        if now_ns+elapsed-base.request_start_ns>500_000_000:
            return self._neutral('perception_expired_during_planning')
        if any(now_ns+elapsed>=need.deadline_ns for need in needs if need.need_id in chosen.need_ids):
            return self._neutral('perception_need_expired_during_planning')
        self._status={'reason':'selected_task_fragment','variant':self.variant,
            'config_revision':self.config.revision,'candidate_id':chosen.candidate_id,
            'candidate_kind':chosen.kind,'candidate_count':len(candidates),'need_count':len(needs),
            'score':ordering[1],'no_progress':self._no_progress,
            'task_gaze_debt':chosen.task_gaze_debt,
            'requested_yaw_degrees':chosen.yaw_degrees,'requested_pitch_degrees':chosen.pitch_degrees,
            'motion_guard_reason':None if report is None else report.reason,
            'boundary_events':self.gaze.boundary_events,'planning_ns':elapsed-offset}
        return PerceptionDecision(movement,look,'selected_task_fragment',chosen.candidate_id,
                                  tuple(completed),tuple(self._pending),ordering[1])
