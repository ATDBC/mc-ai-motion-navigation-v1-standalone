"""Bounded replay of M4/M5 from formal actor evidence; no fixture coordinates."""
from collections import Counter
import math

from mc2p.runtime.trace import trace_projection
from mc2p.skills.follow_gaits import fixed_movement
from mc2p.skills.follow_playground import PlaygroundFollower
from mc2p.skills.perception_needs import PerceptionConfig
from mc2p.skills.target_belief import TargetBelief
from mc2p.skills.target_search import TargetSearch


def verified_final_hold(context,track_id,distance,movement):
    if context is None or not context.get('verified_navigation_execution'): return False
    decision=context['decision']
    return (decision['state']=='holding_distance' and decision['reason']=='distance_hysteresis'
            and decision['target']['track_id']==track_id and distance is not None and distance<=3.5
            and not any(movement.values()))


class LossEvidence:
    """One active loss, constant-space completed closed-loop count."""
    def __init__(self, window=None):
        self.window=window
        self.started=None
        self.travel=0.
        self.completed=0
        self.fresh_started=self.fresh_previous=None
        self.max_fresh_unseen_ns=0
        self.budget_waited=False
        self.max_budget_fresh_unseen_ns=0

    def observe(self,visible,now,*,fresh=True):
        if visible or not fresh:
            self.fresh_started=self.fresh_previous=None
        else:
            if self.fresh_previous is None or not 0<=now-self.fresh_previous<=250_000_000:
                self.fresh_started=now
            self.fresh_previous=now
            self.max_fresh_unseen_ns=max(self.max_fresh_unseen_ns,now-self.fresh_started)
        if visible:
            if (self.started is not None and self.travel>=.5
                    and (self.window is None or self.window[0]<=self.started<now<=self.window[1])):
                self.completed+=1
            self.started=None; self.travel=0.
            self.budget_waited=False
        elif self.started is None:
            self.started=now; self.travel=0.
            self.budget_waited=False
        self._budget_duration()

    def _budget_duration(self):
        if self.budget_waited and self.fresh_started is not None and self.fresh_previous is not None:
            start,end=self.fresh_started,self.fresh_previous
            if self.window is not None: start,end=max(start,self.window[0]),min(end,self.window[1])
            self.max_budget_fresh_unseen_ns=max(self.max_budget_fresh_unseen_ns,max(0,end-start))

    def record_budget_wait(self):
        if self.started is not None:
            self.budget_waited=True
            self._budget_duration()

    def moved(self,distance):
        if self.started is not None: self.travel+=distance


class SearchRequestAudit:
    def __init__(self, *, required=False, window=None):
        self.required=required
        self.loss=LossEvidence(window)
        self.identity=self.belief=self.mode=None
        self.search=TargetSearch()
        self.variant='m6_baseline'
        self.follower=None
        self._planning_elapsed=()
        self._planning_index=0
        self.counts=Counter()
        self.hidden=False
        self.displacement=0.

    def release(self):
        if self.follower is None:
            self.search.interrupt()
        else:
            self.follower.clear_navigation()

    def _planning_clock(self):
        if self._planning_index>=len(self._planning_elapsed):
            raise ValueError('active replay consumed an unrecorded planning clock sample')
        value=self._planning_elapsed[self._planning_index]
        self._planning_index+=1
        return value

    def observe(self,snapshot,view,context,floor):
        decision=context['decision']; now=context['decision_time_ns']
        context['verified_search']=False
        if decision.get('cognition_mode')!='m4_search_v1':
            if self.required: raise ValueError('formal task missing M4/M5 cognition mode')
            return
        identity=(context['task_id'],context['attempt_id'],context['episode_id'])
        target=decision['target']
        if identity!=self.identity:
            self.search.interrupt()
            self.variant=context.get('perception_variant','m6_baseline')
            if self.variant=='m6_baseline':
                self.search=TargetSearch()
                self.follower=None
            else:
                config=context.get('perception_config')
                if self.variant not in {'smooth_only','active_perception_v1'} or type(config) is not dict:
                    raise ValueError('search perception variant/config missing')
                try:
                    frozen=PerceptionConfig(**config)
                except (TypeError,ValueError) as error:
                    raise ValueError('search perception config invalid') from error
                self.follower=PlaygroundFollower(context['task_id'],view,decision['requested_mode'],
                    perception_variant=self.variant,perception_config=frozen)
                distance=context['active_perception']['auto_target_distance_blocks']
                if decision['requested_mode']=='auto':
                    self.follower.set_distance(distance)
                elif distance!=self.follower.auto_config.target_distance:
                    raise ValueError('fixed-mode active replay gained an unbound auto distance')
                self.follower.bind_navigation(snapshot,now)
                self.follower.perception._clock=self._planning_clock
                self.search=self.follower.search
            self.identity=identity; self.hidden=False
            self.loss.started=None; self.loss.travel=0.
            self.loss.fresh_started=self.loss.fresh_previous=None
            self.loss.budget_waited=False
            self.belief=TargetBelief(target['track_id'],snapshot,now)
            self.mode=decision['requested_mode']
        if self.mode!=decision['requested_mode']:
            if self.follower is None:
                self.search.interrupt()
            else:
                self.follower.set_mode(decision['requested_mode'])
            self.mode=decision['requested_mode']
        replay=None
        if self.follower is not None:
            distance=context['active_perception']['auto_target_distance_blocks']
            if distance!=self.follower.auto_config.target_distance:
                if self.follower.mode!='auto':
                    raise ValueError('fixed-mode active replay changed its auto distance')
                self.follower.set_distance(distance)
            self._planning_elapsed=tuple(context['active_perception']['planning_elapsed_ns'])
            self._planning_index=0
            self.follower.perception.begin_control_frame(deadline_ns=context.get('control_deadline_ns'))
            replay=self.follower.decide(view,now,navigation=snapshot)
            if (self._planning_index!=len(self._planning_elapsed)
                    or self.follower.perception.planning_elapsed_ns!=self._planning_elapsed):
                raise ValueError('active replay did not consume the exact planning elapsed sequence')
            if trace_projection(replay)!=decision:
                raise ValueError('active follow/search replay differs from formal evidence or shared gaze state')
            if (trace_projection(self.follower.perception.status())
                    !=trace_projection(context['active_perception']['perception'])):
                raise ValueError('active perception status differs from deterministic control replay')
        belief=self.belief.update(snapshot,now,purpose='route_check' if decision['reason']=='reacquire' else 'search')
        visible=belief.visible_entity is not None
        if (target['status']=='visible')!=visible or target['track_id']!=belief.track_id:
            raise ValueError('search target identity/visibility differs from legal evidence')
        self.loss.observe(visible,now,fresh=view.base.available and 0<=now-view.base.request_start_ns<=500_000_000)
        for key,size,limit in (('max_terrain',len(snapshot.terrain),512),('max_entities',len(snapshot.entities),64),
                ('max_history',len(belief.samples),8),('max_checks',len(belief.checks),16)):
            if size>limit: raise ValueError('cognition collection exceeded bound: '+key)
            self.counts[key]=max(self.counts[key],size)
        if visible:
            if self.hidden: self.counts['reacquisitions']+=1
            self.hidden=False
            if self.follower is None: self.search.clear()
        else:
            if target['position'] is not None or decision['distance'] is not None:
                raise ValueError('hidden search target exposed an exact current position')
            if not self.hidden: self.counts['losses']+=1
            self.hidden=True
        report=decision.get('search_report')
        if report is None:
            if (not visible and decision.get('local_retreat') is None and not context.get('verified_retreat_terminal')
                    and decision['reason'] not in {'stale_or_missing_observation','unsupported_motion_or_effects',
                        'gui_open','player_dead','awaiting_new_observation','control_interval'}):
                raise ValueError('hidden target omitted search report')
            if context.get('verified_retreat_terminal') or decision['reason'] in {'stale_or_missing_observation','unsupported_motion_or_effects','gui_open','player_dead'}:
                if self.follower is None: self.search.interrupt()
            return
        if visible: raise ValueError('visible target retained a search report')
        gait='normal' if self.mode=='auto' else self.mode
        if replay is None:
            expected=self.search.decide(snapshot,view,belief,now,fixed_movement(gait),floor)
            if (trace_projection(expected.report)!=report or any(trace_projection(getattr(expected,k))!=decision[k]
                    for k in ('movement','look','state','reason'))):
                raise ValueError('search replay differs from formal evidence, execution feedback or budgets')
        if self.search.completed_check is not None: self.belief.record_check(self.search.completed_check)
        context['verified_search']=True
        self.counts['requests']+=1
        self.counts['movement_requests']+=bool(decision['movement']['forward'])
        self.counts['waiting_requests']+=decision['state']=='waiting_target'
        self.counts['budget_waiting_requests']+=(decision['state']=='waiting_target' and decision['reason'].endswith('_budget_exhausted'))
        if decision['state']=='waiting_target' and decision['reason'].endswith('_budget_exhausted'):
            self.loss.record_budget_wait()
        self.counts['max_candidates']=max(self.counts['max_candidates'],self.search.candidates)
        self.counts['max_visited']=max(self.counts['max_visited'],len(self.search.visited))
        if self.search.candidates>16 or len(self.search.visited)>16:
            raise ValueError('search candidate storage exceeded bound')
        self.counts['max_history']=max(self.counts['max_history'],len(belief.samples))
        self.counts['max_checks']=max(self.counts['max_checks'],len(self.belief.view(now).checks))

    def feedback(self,selected,evidence,view,context,pre):
        if self.follower is None:
            self.search.feedback(selected,view,view.base.received_at_ns)
        else:
            self.follower.feedback(selected,view,view.base.received_at_ns,evidence=evidence)
        if (not selected and context.get('verified_search') and any(value!=context['accepted_intent_id']
                for value in context['selected'].values())
                and (self.loss.window is None or self.loss.window[0]<=context['decision_time_ns']<=self.loss.window[1])):
            self.counts['other_selected_search_requests']+=1
        if not selected or not context.get('verified_search'): return
        if context['decision']['movement']['forward']:
            self.counts['executed_movements']+=1
            own=view.base.own
            if own is not None:
                before=pre['self_state']['value']['position']
                distance=math.hypot(own.position.x-before['x'],own.position.z-before['z'])
                self.displacement+=distance
                self.loss.moved(distance)
        else:
            self.counts['selected_neutral_checks']+=1

    def metrics(self):
        return dict(self.counts,actual_search_displacement_blocks=self.displacement,
                    completed_moving_search_losses=self.loss.completed,max_fresh_unseen_ns=self.loss.max_fresh_unseen_ns,
                    max_budget_fresh_unseen_ns=self.loss.max_budget_fresh_unseen_ns)
