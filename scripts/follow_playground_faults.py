"""Declared local test faults. Acceptance is separate from the preserved failed session."""
import math
FAULT_KINDS=('owner_heartbeat','control_disconnect','worker_stall')


def released_controls(action: dict) -> bool:
    return not any(action['movement'].values()) and not any(action['look'].values()) and action['operation'] is None


class FinalReleaseEvidence:
    def __init__(self, injected_at_ns: int, deadline_ns: int):
        self.injected_at_ns,self.deadline_ns=injected_at_ns,deadline_ns
        self.last_active_ns=self.last_neutral_ns=None

    def observe(self, started_ns: int, received_ns: int, neutral: bool):
        if started_ns<self.injected_at_ns: return
        if neutral:
            if received_ns<=self.deadline_ns: self.last_neutral_ns=started_ns
        else: self.last_active_ns=received_ns

    def passed(self) -> bool:
        return self.last_neutral_ns is not None and (self.last_active_ns is None or self.last_neutral_ns>self.last_active_ns)


def eligible_stop(when: str, movement: dict, motion: dict | str) -> bool:
    if type(motion) is not dict or movement['forward']!=1: return False
    if when=='ground_moving':
        velocity=motion.get('velocity',{})
        return (motion.get('on_ground') is True and movement['jump'] is False
                and math.hypot(velocity.get('x',0),velocity.get('z',0))>.01)
    if when=='airborne':
        return (motion.get('on_ground') is False and movement['jump'] is True
                and motion.get('velocity',{}).get('y',0)>.1)
    raise ValueError('undeclared stop trigger')


class StopEvidence:
    """Full formal-observation audit; inertia is allowed, new movement commands are not."""
    def __init__(self, owner: list, triggered: dict):
        self.owner=owner; self.triggered=triggered
        self.releases={key:0 for key in triggered}

    def observe(self, observation: dict, action: dict | None, motion: dict):
        if action is None: return
        start=observation['request_started_at_monotonic_ns']; end=observation['received_at_monotonic_ns']
        controls=[c for c in self.owner if c['command']['kind'] in {'follow_start','follow_stop'} and c['sent_at_ns']<=start]
        if not controls: return
        latest=controls[-1]
        neutral=released_controls(action)
        if latest['command']['kind']=='follow_stop' and latest['applied_at_ns']<=start and not neutral:
            raise ValueError('actual command resumed movement after applied stop')
        for command in controls:
            sequence=command['command']['sequence']
            if sequence not in self.triggered or not command['sent_at_ns']<=start<=end<=command['applied_at_ns']: continue
            wanted=self.triggered[sequence]
            if neutral and motion.get('on_ground') is (wanted=='ground_moving'):
                self.releases[sequence]+=1

    def releases_complete(self) -> bool:
        return bool(self.releases) and all(count>0 for count in self.releases.values())


def fault_contract(kind: str) -> dict:
    if kind not in FAULT_KINDS: raise ValueError('undeclared fault')
    return dict(schema_version='mc2p.playground-fault-plan.v1',kind=kind,
        earliest_ns=12_000_000_000,latest_ns=15_000_000_000,
        trigger='actual_follow_movement',stall_duration_ns=4_000_000_000 if kind=='worker_stall' else 0,
        release_deadline_ns=6_000_000_000,purpose='owned_local_failure_injection_not_actor_capability')


def fault_outer_completed(outer: dict) -> bool:
    return (outer.get('return_code')==1 and outer.get('primary_failure') is None
        and not outer.get('cleanup_failures') and outer.get('process_stopped') is True)


def expected_fault_outcome(kind: str, parent: dict, worker: dict) -> bool:
    """Classify only; caller must additionally prove injection, neutral observations and full evidence."""
    fault_contract(kind)
    if (parent.get('process_stopped') is not True or parent.get('cleanup_failures')
            or worker.get('cleanup',{}).get('status')!='passed' or worker.get('cleanup',{}).get('failures')):
        return False
    primary=parent.get('primary_failure'); failure=worker.get('primary_failure')
    if kind=='worker_stall':
        return primary=='worker_heartbeat_lost' and parent.get('return_code')==0 and failure is None
    # The tick loop and socket reader independently enforce the same owner lease.
    # A clean worker can report expiry before the socket reader times out. The parent
    # may see only its consequent EOF, but unrelated parent failures remain rejected.
    if kind=='owner_heartbeat' and parent.get('return_code')==0:
        return primary in (None,'EOFError: scripted control disconnected','owner_heartbeat_lost') and failure is None and worker.get('reason') in {'owner_heartbeat_lost','owner_lease_insufficient'}
    if kind=='control_disconnect' and parent.get('return_code')==0:
        return primary in (None,'EOFError: scripted control disconnected') and failure is None and worker.get('reason')=='control_disconnected'
    expected='control_TimeoutError' if kind=='owner_heartbeat' else 'control_EOFError'
    allowed=(None,'EOFError: scripted control disconnected','owner_heartbeat_lost') if kind=='owner_heartbeat' else (None,'EOFError: scripted control disconnected')
    return (parent.get('return_code')==1 and primary in allowed
        and failure==dict(type='RuntimeError',message=expected))
