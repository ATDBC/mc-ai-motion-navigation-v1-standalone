"""Evaluator-only normal block replacement; never an input to the follower.

An ordinary creative leader first places stone, then mines and replaces that
same known obstacle with cobblestone while the follower looks away. This tests
block-history identity/timing, not newly built wall collision avoidance.
"""
from __future__ import annotations

from dataclasses import replace
import math
import time

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import (
    ActionIntentV1, InteractBlockV1, MineBlockV1, SelectHotbarV1, MovementV1, LookV1,
)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import require_nonnegative_int,require_finite
from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.gaze_controller import GazeController
from mc2p.skills.perception_needs import _block
from scripts.follow_scenarios import follow_task
from scripts.mining_runtime_core import current_block_target, INTERACTION

NS=1_000_000_000


class WorldChangeHistory:
    """Three bounded diagnostic copies. Never observe(), prune(), or inject state."""
    def __init__(self,support,clock_bindings,*,initial_yaw=0.):
        _block(support,'history support')
        require_finite(initial_yaw,'world change initial yaw')
        self.initial_yaw=initial_yaw
        self.support=support
        self.changed=(support[0],support[1]+1,support[2])
        self.clock_bindings=dict(clock_bindings)
        self.markers=[]

    def sample(self,driver,observation,now_ns,elapsed_ns):
        from mc2p.runtime.trace import trace_projection
        if len(self.markers)==3:return
        index=len(self.markers)
        earliest=(13*NS,25*NS,30*NS)[index]
        latest=(15*NS,30*NS,38*NS)[index]
        if elapsed_ns<earliest:return
        if elapsed_ns>=latest:raise ValueError('world-change history milestone missed its declared window')
        if driver is None:raise ValueError('world-change history requires the actual live driver')
        memory=driver.navigation_memory
        # TerrainHistory and its contents are immutable; copying does not renew
        # a timestamp or make a cached neighbor newly observable.
        history=memory._terrain.get(self.changed)
        expected='minecraft:cobblestone' if index==2 else 'minecraft:stone'
        if history is None or history.block.block_id!=expected:
            if index==2:return  # Wait for the normal Driver observation path.
            raise ValueError('world-change actual history missing expected old stone')
        if index==2 and (history.last_seen.request_start_ns<now_ns-elapsed_ns+30*NS
                or not 0<=now_ns-history.last_seen.request_start_ns<=500_000_000):
            return  # A cached update before returning the head is not reobservation.
        if index==2 and abs((observation.yaw_degrees.value-self.initial_yaw+180)%360-180)>.001:
            return  # Record the settled declared return pose, not a mid-turn sample.
        value=trace_projection(history)
        if index==1 and value!=self.markers[0]['history']:
            raise ValueError('world-change hidden history changed before reobservation')
        self.markers.append(dict(kind=('hidden_before','hidden_after','reobserved')[index],
            sampled_at_ns=now_ns,observation_sequence_id=observation.sequence_id,
            episode_id=observation.episode_id,controller_clock_id=observation.controller_clock_id,
            scope_id=memory._scope_id,task_id=driver.follower.task_id,attempt_id=driver.attempt_id,
            history=value))

    def report(self):
        from copy import deepcopy
        return dict(schema_version='mc2p.world-change-history.v1',support=list(self.support),
            changed=list(self.changed),clock_source='time.perf_counter_ns',
            clock_bindings=dict(self.clock_bindings),markers=deepcopy(self.markers))


def world_change_plan():
    return dict(schema_version='mc2p.playground-case-plan.v1',revision=1,case='active-world-change',
        interval_ns=100_000_000,duration_ns=40*NS,
        phases=[dict(id=0,kind='static',mode='normal',start_ns=2*NS,duration_ns=37*NS)],
        owner_commands=[dict(at_ns=2*NS,kind='follow_mode',mode='normal'),
                        dict(at_ns=2*NS,kind='follow_start'),dict(at_ns=39_500_000_000,kind='follow_stop')],
        leader_separation_distance_blocks=None,actor_input='lawful_structured_only',
        leader_schedule='ordinary_client_controls_evaluator_only',
        test_look_hold=dict(away_start_ns=10*NS,return_start_ns=30*NS,yaw_offset_degrees=120),
        hidden_before_ns=13*NS,change_start_ns=15*NS,hidden_after_ns=25*NS,
        reobserve_deadline_ns=38*NS)


class WorldChangeLeader:
    def __init__(self,support):
        _block(support,'world change support')
        if support is None:raise ValueError('world change requires a bounded support goal')
        self.support=support
        self.changed=(support[0],support[1]+1,support[2])
        _block(self.changed,'world change replacement')
        self.gaze=GazeController()
        self.stage='initial'
        self._seen=-1
        self._count=0
        self._last_proposal=None
        self.sequence=0

    def controls(self,observation,now_ns,elapsed_ns):
        require_nonnegative_int(elapsed_ns,'world change elapsed')
        if observation.sequence_id<=self._seen:return LookV1(),None
        self._seen=observation.sequence_id
        if now_ns-observation.request_started_at_monotonic_ns>500_000_000:
            self.gaze.clear('stale_sample_refresh')
            self._last_proposal=None
            return LookV1(),None  # One neutral Runtime step obtains a new sample.
        view=project_playground_view(observation,now_ns,observation.controller_clock_id)
        own=view.base.own
        body=observation.self_state.value
        # This is the predeclared creative test leader, not a change to the
        # survival follower's supported-motion gate. Camera control is shared.
        if (not view.base.available or own is None or own.dead or body is None
                or body.game_mode!='creative' or body.pose!='standing' or body.is_flying
                or body.is_swimming or body.is_submerged_in_water or body.is_fall_flying
                or body.is_climbing or body.is_burning
                or view.base.gui_open or not 0<=now_ns-view.base.request_start_ns<=500_000_000):
            raise RuntimeError('world-change leader needs a fresh available body')
        if observation.inventory.value is None:raise RuntimeError('world-change leader inventory missing')
        dx,dz=self.support[0]+.5-own.position.x,self.support[2]+.5-own.position.z
        yaw=math.degrees(math.atan2(-dx,dz))
        pitch=math.degrees(math.atan2(own.position.y+1.62-(self.support[1]+1),math.hypot(dx,dz)))
        look=self.gaze.command(view,yaw,pitch,now_ns,precise=True)
        current=current_block_target(observation,now_ns=now_ns)
        position=None if current is None else current[0].block_position
        name=None if current is None else current[1].block_id
        operation=None
        if self.stage in {'await_stone','await_cobble'}:
            expected='minecraft:stone' if self.stage=='await_stone' else 'minecraft:cobblestone'
            self._count=self._count+1 if position==self.changed and name==expected else 0
            if self._count>=3:
                self.stage='stone_ready' if expected=='minecraft:stone' else 'changed'
                self._count=0
        if self.stage=='await_removal' and position==self.support and name=='minecraft:grass_block':
            self.stage='refill'
        if look==LookV1():
            inventory=observation.inventory.value
            if self.stage in {'initial','refill'}:
                wanted='minecraft:stone' if self.stage=='initial' else 'minecraft:cobblestone'
                slot=next((i for i,item in enumerate(inventory.main[:9]) if not item.empty and item.item_id==wanted),None)
                if slot is None:raise RuntimeError('world-change requires existing building items')
                if inventory.selected_hotbar_slot!=slot:operation=SelectHotbarV1(slot)
                elif position==self.support and name=='minecraft:grass_block' and current[0].face=='up':
                    operation=InteractBlockV1(*self.support,'up')
            elif self.stage=='stone_ready' and elapsed_ns>=15*NS:
                if position==self.changed and name=='minecraft:stone':
                    operation=MineBlockV1(*self.changed,current[0].face)
        self._last_proposal=(observation.sequence_id,operation)
        return look,operation

    def accepted(self,operation,before_sequence):
        if operation is None or self._last_proposal!=(before_sequence,operation):
            raise RuntimeError('world-change acceptance is not the current proposal')
        self._last_proposal=None
        if type(operation) is MineBlockV1:self.stage='await_removal'
        elif type(operation) is InteractBlockV1:
            self.stage='await_stone' if self.stage=='initial' else 'await_cobble'

    def tick(self,runtime,source,deadline_ns,elapsed_ns):
        now=time.perf_counter_ns()
        observation=runtime.observation
        look,operation=self.controls(observation,now,elapsed_ns)
        self.sequence+=1
        if self.sequence>900:raise TimeoutError('world-change leader exceeded bounded case ticks')
        identifier=ordered_intent_id(source,self.sequence)
        runtime.cancel_source(source.source_id)
        runtime.submit_ordered_intent(OrderedIntentV1(source,self.sequence,ActionIntentV1(identifier,
            source.source_id,observation.episode_id,observation.sequence_id,ActionPriorityV0.PLAYER,
            now,min(deadline_ns,now+250_000_000),movement=MovementV1(),look=look,
            operation=operation,valid_for_ticks=1,movement_requires_look=True)))
        result=runtime.step(replace(follow_task(deadline_ns),task_id='world-change-leader',
            task_type='test_leader'),BehaviorProfileV0(),deadline_ns,observation_request=INTERACTION)
        receipt=None if result.backend_result is None else result.backend_result.receipt
        selected={} if result.decision is None else dict(result.decision.selected_intents)
        action=None if result.decision is None else result.decision.action
        statuses={'executed','confirmed_local'} if operation is None else {'pending_confirmation'}
        if (result.observation is None or result.report.failure is not None or receipt is None
                or receipt.status not in statuses or action is None or action.look!=look
                or action.operation!=operation or action.movement!=MovementV1()
                or selected.get('movement')!=identifier or selected.get('look')!=identifier
                or operation is not None and selected.get('operation')!=identifier):
            raise RuntimeError('world-change leader control not confirmed')
        if operation is not None:self.accepted(operation,observation.sequence_id)
        return result
