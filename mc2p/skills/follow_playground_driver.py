"""One persistent ordered source, bounded steps, owner-authorized leases and confirmed release."""
from dataclasses import asdict, replace
import time
from typing import Callable
import uuid
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1, LookV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.follow_playground import PlaygroundFollower
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.navigation_memory import NavigationMemory
from mc2p.skills.navigation_evidence import project_navigation_evidence

INTERVAL_NS = 50_000_000
STEP_LEASE_NS = 250_000_000
CLEANUP_NS = 1_000_000_000
OWNER_LEASE_NS = 3_000_000_000


class PlaygroundDriver:
    def __init__(self, runtime: PlayerRuntimeV1, follower: PlaygroundFollower,
                 clock_ns: Callable[[],int] = time.perf_counter_ns) -> None:
        if type(runtime) is not PlayerRuntimeV1 or type(follower) is not PlaygroundFollower:
            raise ContractViolation('playground driver requires formal Runtime and follower')
        if runtime.state is not RuntimeStateV1.READY: raise ContractViolation('playground Runtime must be ready')
        self.runtime,self.follower,self._clock = runtime,follower,clock_ns
        self.clock_id = runtime.observation.controller_clock_id
        self.attempt_id = uuid.uuid4().hex
        self.navigation_memory = NavigationMemory(self.attempt_id)
        now=self._clock()
        initial=self.navigation_memory.observe(runtime.observation,now_ns=now,
            controller_clock_id=self.clock_id,scope_id=self.attempt_id)
        follower.bind_navigation(initial,now)
        self.source = runtime.register_ordered_source('playground-follow')
        self.sequence = 0
        self.next_update_at_ns = 0
        self._owner_deadline = 0
        self._task = None
        self.state,self.reason = 'ready',None
        self.last_result = None
        self.last_visible_distance = None

    def _view(self, now: int):
        if self.runtime.state is not RuntimeStateV1.READY or self.state in {'stopped','failed'}:
            raise ContractViolation('closed/sealed playground driver cannot resume')
        return project_playground_view(self.runtime.observation,now,self.clock_id)

    def _validate_task(self, task: TaskIntentV0) -> None:
        if type(task) is not TaskIntentV0 or task.task_id!=self.follower.task_id:
            raise ContractViolation('playground authorization belongs to another task')
        identity = replace(task,deadline_monotonic_ns=1)
        if self._task is not None and identity!=self._task:
            raise ContractViolation('persistent task authorization changed')
        self._task = identity

    def _event(self, kind: str, schema: str, **values) -> None:
        self.runtime.record_task_event(kind,dict(schema_version=schema,task_id=self.follower.task_id,
            attempt_id=self.attempt_id,episode_id=self.source.episode_id,source_generation=self.source.generation,**values))

    def _accepted(self, result: RuntimeStepResultV1) -> None:
        receipt = None if result.backend_result is None else result.backend_result.receipt
        if (result.observation is None or result.report.failure is not None or receipt is None
                or receipt.status not in {'executed','confirmed_local','cancelled'}):
            raise RuntimeError('unconfirmed playground control: '+str(result.report.failure or getattr(receipt,'status',None)))

    def _fail(self, error: BaseException) -> None:
        self.state,self.reason = 'failed',type(error).__name__
        self.follower.cancel('runtime_control_failed')
        if self.runtime.state is RuntimeStateV1.READY:
            try: self.runtime.fail_closed('playground control or evidence uncertain')
            except BaseException as secondary: error.add_note('seal also failed: '+repr(secondary))

    def tick(self, task: TaskIntentV0, profile: BehaviorProfileV0, owner_deadline_ns: int) -> RuntimeStepResultV1 | None:
        now = self._clock()
        if self.follower.perception is not None:
            require_nonnegative_int(owner_deadline_ns,'owner deadline')
            self.follower.perception.begin_control_frame(
                deadline_ns=min(owner_deadline_ns,now+STEP_LEASE_NS))
        view = self._view(now)
        self._validate_task(task)
        require_nonnegative_int(owner_deadline_ns,'owner deadline')
        if owner_deadline_ns<=now: return self.stop(task,profile,'owner_heartbeat_lost')
        if owner_deadline_ns>now+OWNER_LEASE_NS or owner_deadline_ns<self._owner_deadline:
            raise ContractViolation('owner lease is unbounded or regressed')
        try:
            if owner_deadline_ns!=self._owner_deadline:
                self._event('task_lease_renewed','mc2p.task-lease-renewed.v1',
                    previous_owner_deadline_ns=self._owner_deadline,owner_deadline_ns=owner_deadline_ns,renewed_at_ns=now)
                self._owner_deadline = owner_deadline_ns
            if now<self.next_update_at_ns: return None
            navigation=self.navigation_memory.observe(self.runtime.observation,now_ns=now,
                controller_clock_id=self.clock_id,scope_id=self.attempt_id)
            decision = self.follower.decide(view,now,navigation=navigation)
            if decision.target.status=='visible' and decision.distance is not None:
                self.last_visible_distance = decision.distance
            if decision.state=='cancelled': return self.stop(task,profile,decision.reason)
            deadline = min(owner_deadline_ns,now+STEP_LEASE_NS)
            envelope_task = replace(task,deadline_monotonic_ns=deadline)
            self.runtime.cancel_source(self.source.source_id)
            next_sequence=self.sequence+1
            intent_id = ordered_intent_id(self.source,next_sequence)
            intent = ActionIntentV1(intent_id,self.source.source_id,self.source.episode_id,
                view.base.sequence_id,ActionPriorityV0.TASK,now,deadline,movement=decision.movement,
                look=decision.look,valid_for_ticks=1,movement_requires_look=True)
            try:
                self.runtime.submit_ordered_intent(OrderedIntentV1(self.source,next_sequence,intent))
            except ContractViolation as error:
                current=self._clock()
                if (str(error)!='intent has stale episode/observation or invalid time'
                        or self.runtime.state is not RuntimeStateV1.READY
                        or self.runtime.observation.episode_id!=intent.episode_id
                        or self.runtime.observation.sequence_id!=intent.observation_sequence_id
                        or current<intent.expires_at_monotonic_ns): raise
                # Runtime validation rejected this envelope before any acceptance or I/O.
                # Never resend it or consume an accepted sequence; cancel via a fresh cleanup lease.
                return self.stop(task,profile,'owner_heartbeat_lost' if current>=owner_deadline_ns
                    else 'action_lease_expired_before_dispatch')
            self.sequence=next_sequence
            self._event('playground_task','mc2p.playground-task-step.v1',intent_sequence=self.sequence,
                observation_sequence_id=view.base.sequence_id,state=decision.state,requested_mode=decision.requested_mode,
                decision=decision,owner_deadline_ns=owner_deadline_ns)
            if self.follower.perception is not None:
                self._event('active_perception','mc2p.active-perception-step.v1',intent_sequence=self.sequence,
                    observation_sequence_id=view.base.sequence_id,variant=self.follower.perception_variant,
                    config=asdict(self.follower.perception.config),perception=self.follower.perception.status(),
                    planning_elapsed_ns=self.follower.perception.planning_elapsed_ns,
                    auto_target_distance_blocks=self.follower.auto_config.target_distance)
            self.next_update_at_ns = now+INTERVAL_NS
            result = self.runtime.step(envelope_task,profile,deadline,
                                       observation_request=ObservationRequestV3())
            self._accepted(result)
            selected = dict(result.decision.selected_intents)
            cancelled = result.backend_result.receipt.status=='cancelled'
            executed = (not cancelled and selected.get('movement')==intent_id and selected.get('look')==intent_id)
            self.state,self.reason = (decision.state,decision.reason) if executed else ('preempted','arbitration_selected_other_or_filtered')
            if cancelled: self.state,self.reason='preempted','client_control_cancelled'
            if self.follower.perception is None:
                self.follower.feedback(executed,self._view(self._clock()),self._clock())
            else:
                feedback_now=self._clock()
                evidence=project_navigation_evidence(result.observation,now_ns=feedback_now,
                    controller_clock_id=self.clock_id)
                self.follower.feedback(executed,self._view(feedback_now),feedback_now,evidence=evidence)
            self.last_result = result
            return result
        except BaseException as error:
            self._fail(error)
            raise

    def release(self, task: TaskIntentV0, profile: BehaviorProfileV0, reason: str) -> RuntimeStepResultV1:
        now = self._clock()
        self._view(now); self._validate_task(task)
        try:
            self.runtime.cancel_source(self.source.source_id)
            self.follower.clear_navigation()
            cleanup = replace(task,task_type='playground_release',deadline_monotonic_ns=now+CLEANUP_NS)
            result = self.runtime.step(cleanup,profile,cleanup.deadline_monotonic_ns,
                                       observation_request=ObservationRequestV3())
            self._accepted(result)
            if any(value.startswith(self.source.source_id+'/') for _,value in result.decision.selected_intents):
                raise RuntimeError('old playground control remained selected')
            self._event('playground_control_release','mc2p.playground-control-release.v1',reason=reason,
                observation_sequence_id=result.observation.sequence_id,intent_sequence=self.sequence)
            self.last_result = result
            self.next_update_at_ns = self._clock()+INTERVAL_NS
            self.state,self.reason = 'holding_distance','controls_released'
            return result
        except BaseException as error:
            self._fail(error)
            raise

    def stop(self, task: TaskIntentV0, profile: BehaviorProfileV0, reason: str) -> RuntimeStepResultV1:
        result = self.release(task,profile,reason)
        try:
            self.runtime.unregister_ordered_source(self.source)
            self.follower.cancel(reason)
            self.navigation_memory.reset('stopped/'+self.attempt_id)
            self.state,self.reason = 'stopped',reason
            return result
        except BaseException as error:
            self._fail(error)
            raise
