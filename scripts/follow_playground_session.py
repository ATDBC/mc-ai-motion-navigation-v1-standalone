"""Persistent user authorization and session lifecycle, separate from short action envelopes."""
from __future__ import annotations
from scripts.demo_control_protocol import DemoCommandV1
from dataclasses import asdict, replace
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
import psutil
from scripts.control_probe_core import write_json_atomic as _write_json_atomic
from scripts.fabric_deployment_launch import ROOT, _hash
from scripts.visual_demo_session import read_json as _read_json, identity_alive, ID_PATTERN
from scripts.visibility_fixture_world import _no_links
from scripts.active_perception_scenarios import MEASUREMENT_REVISION
from scripts.block_observation_v3_evidence import NAVIGATION_CONTRACT, require_navigation_contract
from scripts.block_observation_v3_sources import V3_BODY_SOURCES, V3_ENTRY_SOURCES
from scripts.timing_parallel_probe_core import (ProcessIdentityV0, ProcessIdentityError,
    capture_registered_tree, terminate_registered_tree)

SESSIONS = ROOT/'artifacts/follow-playground'
PRE_INPUT_CADENCE_CORE_SOURCES = ('scripts/follow_playground_session.py','scripts/follow_playground_host.py',
    'mc2p/contracts/observation_v2.py','mc2p/backends/client_observation_payload.py',
    'mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java',
    'scripts/follow_playground_fixture.py','scripts/java/FollowPlaygroundInitializer.java',
    'scripts/demo_control_server.py','scripts/demo_control_launch.py','mc2p/runtime/player_runtime_v1.py',
    'mc2p/runtime/segmented_trace.py','scripts/streaming_time_evidence.py','scripts/follow_playground_motion.py',
    'mc2p/skills/follow_playground_driver.py','mc2p/skills/follow_playground.py','mc2p/skills/follow_gaits.py',
    'mc2p/skills/follow_tracking.py','mc2p/skills/follow_playground_types.py',
    'mc2p/skills/local_navigation.py','mc2p/skills/follow_types.py',
    'mc2p/skills/navigation_evidence.py','mc2p/skills/navigation_memory.py',
    'mc2p/skills/terrain_spatial_index.py',
    'mc2p/skills/navigation_motion.py','mc2p/skills/navigation_look.py','mc2p/skills/navigation_controller.py',
    'mc2p/skills/motion_guard.py','mc2p/skills/perception_needs.py','scripts/active_perception_scenarios.py',
    'mc2p/skills/gaze_controller.py','mc2p/skills/perception_confirmation.py',
    'mc2p/skills/perception_candidates.py','mc2p/skills/active_perception.py','mc2p/skills/route_steering.py',
    'scripts/active_perception_metrics.py','scripts/active_perception_evidence.py','scripts/active_perception_diagnostics.py',
    'scripts/navigation_motion_evidence.py',
    'mc2p/skills/target_belief.py','mc2p/skills/target_attention.py','mc2p/skills/target_search.py',
    'scripts/navigation_scenarios.py',
    'scripts/target_search_evidence.py',
    'scripts/follow_playground_scenarios.py','scripts/follow_playground_evidence.py','scripts/probe_follow_playground.py',
    'scripts/control_probe_core.py','scripts/follow_playground_faults.py',
    'scripts/follow_playground_resources.py','scripts/follow_playground_long.py')
PRE_DENSE_TRACE_CORE_SOURCES = PRE_INPUT_CADENCE_CORE_SOURCES + (
    'mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java',
    'mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorHooks.java',
    'mc2p/backends/runtime_overlays/mc121_structured/BehaviorPlayerMixin.java',
    'deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/mixin/BehaviorPlayerMixin.java',
)
PRE_BLOCK_STATE_CORE_SOURCES = PRE_DENSE_TRACE_CORE_SOURCES + ('mc2p/runtime/trace.py',)
CORE_SOURCES = tuple(dict.fromkeys(PRE_BLOCK_STATE_CORE_SOURCES + V3_BODY_SOURCES + V3_ENTRY_SOURCES + (
    'scripts/follow_runtime_host.py','scripts/follow_scenarios.py','scripts/follow_evidence.py',
    'scripts/fabric_deployment_launch.py','scripts/fabric_deployment_sandbox.py',
    'scripts/block_observation_v3_evidence.py',
    'scripts/formal_observation_v3_evidence.py','scripts/smoke_test_player_runtime.py',
    'scripts/client_time_evidence.py','scripts/reference_input_evidence.py',
    'scripts/active_perception_world_change.py','scripts/active_perception_world_change_evidence.py',
)))


def write_json_atomic(path: Path, value: object) -> None:
    # A concurrent Windows status reader may briefly deny rename/delete sharing.
    # Only this opt-in session path retries (<=100ms); durable trace failures are unchanged.
    _write_json_atomic(path,value,replace_retry_seconds=.1)


def read_json(path: Path) -> dict:
    deadline=time.perf_counter()+.1
    while True:
        try: return _read_json(path)
        except PermissionError as error:
            winerror=getattr(error,'winerror',None)
            # Windows CRT open may retain EACCES without the underlying Win32 code.
            sharing=os.name=='nt' and (winerror in {5,32,33} or winerror is None and error.errno==13)
            remaining=deadline-time.perf_counter()
            if not sharing or remaining<=0: raise
            time.sleep(min(.005,remaining))


PERCEPTION_VARIANTS = ('m6_baseline', 'smooth_only', 'active_perception_v1')
DEFAULT_PERCEPTION_VARIANT = 'active_perception_v1'


def validate_perception_configuration(variant: str, config: dict | None = None) -> dict:
    if variant not in PERCEPTION_VARIANTS:
        raise ValueError('perception variant is unknown or not implemented: '+str(variant))
    if variant!='m6_baseline':
        from mc2p.skills.perception_needs import PerceptionConfig
        from mc2p.contracts.common import ContractViolation
        default=asdict(PerceptionConfig())
        if config is None:
            return default
        if type(config) is not dict or set(config)!=set(default):
            raise ValueError('incomplete perception configuration')
        try:
            return asdict(PerceptionConfig(**config))
        except (ContractViolation,TypeError) as error:
            raise ValueError('invalid perception configuration') from error
    if config is None:
        return {'revision': 1}
    if type(config) is not dict or set(config) != {'revision'} or type(config['revision']) is not int or config['revision'] != 1:
        raise ValueError('invalid perception configuration')
    return dict(config)


def _prepared_report(*, scene: str, fixture: dict, human_launch: dict) -> dict:
    perception_variant = DEFAULT_PERCEPTION_VARIANT
    return dict(
        state='prepared', scene=scene, fixture=fixture, human_launch=human_launch,
        perception_variant=perception_variant,
        perception_config=validate_perception_configuration(perception_variant),
        core_sources={name:_hash(ROOT/name) for name in CORE_SOURCES},
        visible_game_started=False,
        note='start creates a fresh owned world; rendering/input acceptance deferred',
        measurement_revision=MEASUREMENT_REVISION, **dict(NAVIGATION_CONTRACT))


def create_session(seed: int, *, interactive: bool, test_duration_seconds: int | None = None,
                   scene: str = 'flat', perception_variant: str = DEFAULT_PERCEPTION_VARIANT,
                   perception_config: dict | None = None) -> Path:
    config = validate_perception_configuration(perception_variant, perception_config)
    if type(seed) is not int or seed not in (21001,21002,21003) or type(interactive) is not bool:
        raise ValueError('invalid playground configuration')
    if test_duration_seconds is not None and (type(test_duration_seconds) is not int or not 1 <= test_duration_seconds <= 900):
        raise ValueError('explicit test duration must be 1..900 seconds')
    if not interactive and test_duration_seconds is None: raise ValueError('scripted tests require a finite duration')
    if scene not in ('flat','search'): raise ValueError('undeclared playground scene')
    SESSIONS.mkdir(parents=True,exist_ok=True); _no_links(SESSIONS)
    directory = SESSIONS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8])
    directory.mkdir(exist_ok=False)
    now = time.perf_counter_ns()
    write_json_atomic(directory/'manifest.json',dict(schema_version='mc2p.follow-playground-session.v3',
        session_id=directory.name,seed=seed,interactive=interactive,test_duration_seconds=test_duration_seconds,scene=scene,
        perception_variant=perception_variant,perception_config=config,measurement_revision=MEASUREMENT_REVISION,
        created_at_ns=now,startup_deadline_ns=now+150_000_000_000,ports=[25598,8144,8145],
        core_sources={name:_hash(ROOT/name) for name in CORE_SOURCES},**dict(NAVIGATION_CONTRACT)))
    return directory


def session_path(session_id: str) -> Path:
    if type(session_id) is not str or re.fullmatch(ID_PATTERN,session_id) is None: raise ValueError('invalid playground session id')
    path = SESSIONS/session_id; _no_links(path)
    if not path.is_dir(): raise ValueError('playground session missing')
    return path


def read_manifest(directory: Path) -> dict:
    path = session_path(directory.name)
    if path.absolute() != directory.absolute(): raise ValueError('foreign playground session')
    value = read_json(path/'manifest.json')
    historical = value.get('schema_version') == 'mc2p.follow-playground-session.v1'
    current = value.get('schema_version') == 'mc2p.follow-playground-session.v3'
    expected_keys = {'schema_version','session_id','seed','interactive','test_duration_seconds','created_at_ns',
                     'startup_deadline_ns','ports','core_sources','scene'}
    if historical:
        if 'scene' not in value:
            expected_keys.remove('scene')
    else:
        expected_keys.update(('perception_variant','perception_config','measurement_revision'))
    if current:
        expected_keys.update(key for key,_ in NAVIGATION_CONTRACT)
    if (set(value) != expected_keys
            or value['schema_version'] not in {'mc2p.follow-playground-session.v1','mc2p.follow-playground-session.v2',
                                              'mc2p.follow-playground-session.v3'}
            or value['session_id'] != path.name
            or type(value['seed']) is not int or value['seed'] not in (21001,21002,21003)
            or type(value['interactive']) is not bool or value['ports'] != [25598,8144,8145]
            or type(value['created_at_ns']) is not int or value['created_at_ns'] < 0
            or value['startup_deadline_ns'] != value['created_at_ns']+150_000_000_000
            or value.get('scene','flat') not in ('flat','search')
            or type(value['core_sources']) is not dict
            or not (set(value['core_sources']) in (set(CORE_SOURCES),set(PRE_BLOCK_STATE_CORE_SOURCES),set(PRE_DENSE_TRACE_CORE_SOURCES),set(PRE_INPUT_CADENCE_CORE_SOURCES)) or historical
                and {'scripts/follow_playground_session.py','scripts/follow_playground_host.py',
                     'mc2p/runtime/player_runtime_v1.py','mc2p/skills/follow_playground.py'}<=set(value['core_sources'])<=set(CORE_SOURCES))
            or any(type(v) is not str or re.fullmatch('[0-9a-f]{64}',v) is None for v in value['core_sources'].values())):
        raise ValueError('invalid playground manifest')
    duration = value['test_duration_seconds']
    if (duration is not None and (type(duration) is not int or not 1 <= duration <= 900)) or (duration is None and not value['interactive']):
        raise ValueError('invalid playground test duration')
    # Reading historical status is allowed; run_worker independently requires
    # the complete current fingerprint set before any host construction.
    value.setdefault('scene','flat')
    if historical:
        value.update(perception_variant='m6_baseline',perception_config={'revision':1},measurement_revision=0)
    else:
        if current:
            validate_perception_configuration(value['perception_variant'],value['perception_config'])
        else:
            # Historical configuration is data, never an executable current config.
            # Preserve older fields/revisions without adding current defaults.
            from mc2p.skills.perception_needs import PerceptionConfig
            config=value['perception_config']
            if (value['perception_variant'] not in PERCEPTION_VARIANTS or type(config) is not dict
                    or type(config.get('revision')) is not int or config['revision'] not in (1,2,3,4)
                    or not set(config)<=set(asdict(PerceptionConfig()))
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in config.values())):
                raise ValueError('invalid historical perception configuration')
        if type(value['measurement_revision']) is not int or value['measurement_revision'] not in (4,5,MEASUREMENT_REVISION):
            raise ValueError('invalid perception measurement revision')
    if current:
        require_navigation_contract(value)
        if value['measurement_revision']!=MEASUREMENT_REVISION or set(value['core_sources'])!=set(CORE_SOURCES):
            raise ValueError('current playground manifest has historical measurement or source members')
    elif value['measurement_revision']==MEASUREMENT_REVISION:
        raise ValueError('legacy manifest cannot claim block-state measurement')
    return value


def require_current_session(manifest: dict) -> None:
    if manifest['schema_version']!='mc2p.follow-playground-session.v3':
        raise ValueError('historical playground manifest is read-only')
    require_navigation_contract(manifest)
    if manifest['measurement_revision']!=MEASUREMENT_REVISION:
        raise ValueError('historical perception measurement is read-only')
    if manifest['core_sources']!={name:_hash(ROOT/name) for name in CORE_SOURCES}:
        raise ValueError('playground source fingerprints changed before execution')


def read_status(session_id: str) -> dict:
    directory = session_path(session_id); manifest = read_manifest(directory)
    if (directory/'result.json').exists(): result = read_json(directory/'result.json')
    else:
        result = (read_json(directory/'validating.json') if (directory/'validating.json').exists()
                  else read_json(directory/'worker-status.json') if (directory/'worker-status.json').exists()
                  else read_json(directory/'ready.json') if (directory/'ready.json').exists() else dict(state='starting'))
        identity = read_json(directory/'supervisor.json') if (directory/'supervisor.json').exists() else None
        if ((identity is not None and not identity_alive(identity))
                or (not (directory/'ready.json').exists() and time.perf_counter_ns() >= manifest['startup_deadline_ns'])):
            result.update(state='failed',primary_failure='supervisor_absent_or_startup_expired')
    if (directory/'startup-failure.json').exists():
        result.update(state='failed',startup_failure=read_json(directory/'startup-failure.json'))
    return dict(result,session_id=session_id,interactive=manifest['interactive'],directory=str(directory),
                stop_requested=(directory/'stop-request.json').exists())


def request_stop(session_id: str) -> dict:
    directory = session_path(session_id); manifest=read_manifest(directory)
    if manifest['schema_version']!='mc2p.follow-playground-session.v3':
        raise ValueError('historical session is read-only')
    # A current session must remain stoppable even if source files changed since launch.
    try:
        with (directory/'stop-request.json').open('x',encoding='utf-8') as stream:
            json.dump(dict(session_id=session_id,requested_at_ns=time.perf_counter_ns()),stream)
    except FileExistsError: _no_links(directory/'stop-request.json')
    return read_status(session_id)


def worker_liveness_failure(now_ns: int, last_progress_ns: int, beat: dict | None, heartbeat_seconds: float) -> str | None:
    if now_ns-last_progress_ns>=int(heartbeat_seconds*1e9): return 'worker_heartbeat_lost'
    # A cached future deadline does not prove the control reader stopped renewing it.
    # Only a worker report sampled after expiry can independently attest owner loss.
    if beat is not None and beat['sampled_at_ns']>=beat['owner_deadline_ns']: return 'owner_heartbeat_lost'
    return None


class _ScriptedOwner:
    """Parent-owned bounded control socket used only by explicitly selected headless test runs."""
    def __init__(self, endpoint: dict, token: str, events=None):
        from scripts.demo_control_protocol import decode_object
        self.decode = decode_object
        if (set(endpoint) != {'session_id','port'} or type(endpoint['port']) is not int
                or not 1 <= endpoint['port'] <= 65535): raise ValueError('invalid scripted owner endpoint')
        self.session_id = endpoint['session_id']; self.socket = socket.socket()
        try:
            self.socket.settimeout(.2); self.socket.connect(('127.0.0.1',endpoint['port']))
            self.send(dict(schema_version='mc2p.demo-handshake.v1',session_id=self.session_id,token=token))
        except BaseException: self.socket.close(); raise
        self.buffer = bytearray(); self.ready = False; self.sequence = 0; self.next_heartbeat = 0
        self.deadline = time.perf_counter_ns()+3_000_000_000
        self.pending = {}
        self.completed = deque(maxlen=64)
        self.events = events
        self.heartbeat_enabled = True

    def command(self, kind: str, **values) -> int:
        if not self.ready or len(self.pending)>=32: raise RuntimeError('scripted owner not ready or command queue full')
        self.sequence += 1
        command = DemoCommandV1(self.sequence,kind,**values)
        raw = dict(schema_version='mc2p.demo-command.v1',sequence=command.sequence,kind=kind,**values)
        self.send(raw)
        self.pending[command.sequence] = dict(accepted=False,deadline_ns=time.perf_counter_ns()+5_000_000_000)
        if self.events is not None: self.events.write(dict(kind='sent',command=raw,sent_at_ns=time.perf_counter_ns()))
        return command.sequence

    def send(self, value):
        raw = json.dumps(value,allow_nan=False,separators=(',',':')).encode()+b'\n'
        if len(raw)>8192: raise ValueError('scripted control frame too large')
        self.socket.sendall(raw)

    def tick(self, now):
        if not self.ready and now>=self.deadline: raise TimeoutError('scripted owner handshake')
        if any(now>=item['deadline_ns'] for item in self.pending.values()): raise TimeoutError('scripted command application timeout')
        if self.ready and self.heartbeat_enabled and now>=self.next_heartbeat:
            self.sequence += 1
            self.send(dict(schema_version='mc2p.demo-command.v1',sequence=self.sequence,kind='heartbeat'))
            self.next_heartbeat = now+1_000_000_000
        self.socket.settimeout(.01)
        try: raw = self.socket.recv(min(4096,8192-len(self.buffer)))
        except socket.timeout: return
        finally: self.socket.settimeout(.2)
        if not raw: raise EOFError('scripted control disconnected')
        self.buffer.extend(raw)
        while b'\n' in self.buffer:
            line,_,tail = self.buffer.partition(b'\n'); self.buffer = bytearray(tail)
            value = self.decode(bytes(line))
            if not self.ready:
                if value != dict(schema_version='mc2p.demo-ready.v1',session_id=self.session_id):
                    raise ValueError('unexpected scripted owner ready reply')
                self.ready = True
            else:
                if (set(value)!={'schema_version','session_id','sequence','phase','status'}
                        or value['schema_version']!='mc2p.demo-reply.v1' or value['session_id']!=self.session_id
                        or type(value['sequence']) is not int or value['sequence'] not in self.pending):
                    raise ValueError('foreign scripted command reply')
                item = self.pending[value['sequence']]
                if value['phase']=='accepted' and not item['accepted']: item['accepted']=True
                elif value['phase'] in {'applied','rejected','failed'} and item['accepted']:
                    del self.pending[value['sequence']]; self.completed.append(value)
                else: raise ValueError('scripted command reply ordering failed')
                if self.events is not None: self.events.write(dict(kind='reply',reply=value,received_at_ns=time.perf_counter_ns()))
        if len(self.buffer)>=8192: raise ValueError('scripted frame exceeds buffer')

    def close(self):
        try: self.socket.close()
        finally:
            if self.events is not None: self.events.close()


def supervise_command(directory: Path, command: list[str], *, heartbeat_seconds: float = 3,
                      cleanup_seconds: float = 45, connect_scripted_owner: bool = True) -> int:
    """No interactive wall-clock limit: progress and owner leases independently bound active work."""
    manifest = read_manifest(directory)
    require_current_session(manifest)
    scripted_plan = None
    if (directory/'case-plan.json').exists():
        from scripts.follow_playground_scenarios import validate_case_plan
        scripted_plan = read_json(directory/'case-plan.json')
        if manifest['interactive']:
            raise ValueError('test schedule must be current and headless')
        validate_case_plan(scripted_plan)
    scripted_index = 0
    owner_disconnected=False; fault_injected=False
    if not command or not 0 < heartbeat_seconds <= 3 or not 0 < cleanup_seconds <= 45: raise ValueError('invalid supervisor bounds')
    with (directory/'supervisor.json').open('x',encoding='utf-8') as stream:
        json.dump(dict(pid=os.getpid(),create_time=psutil.Process().create_time()),stream)
    environment = {k:v for k,v in os.environ.items() if not k.upper().startswith(('MC2P_','FABRIC_'))
                   and k.upper() not in {'JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS','CLASSPATH'}}
    token = secrets.token_hex(32)
    environment.update(MC2P_PLAYGROUND_CONTROL_TOKEN=token,MC2P_PLAYGROUND_OWNER_PID=str(os.getpid()),
                       MC2P_PLAYGROUND_OWNER_CREATE_TIME=str(psutil.Process().create_time()))
    process = root = owner = None; registered = {}; primary = None; cleanup_failures = []
    closing_deadline = None; ready_since = None; last_progress = time.perf_counter_ns(); last_sequence = 0
    with (directory/'worker-console.log').open('xb') as output:
        try:
            process = subprocess.Popen(command,cwd=ROOT,env=environment,stdin=subprocess.DEVNULL,
                stdout=output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            root = ProcessIdentityV0(process.pid,psutil.Process(process.pid).create_time()); registered[root.pid] = root
            while process.poll() is None:
                now = time.perf_counter_ns()
                try: snapshot = capture_registered_tree(root)
                except ProcessIdentityError:
                    if process.poll() is not None: break
                    raise
                for item in snapshot.identities:
                    if item.pid in registered and registered[item.pid] != item: raise ProcessIdentityError('playground PID reused')
                    if item.pid not in registered and len(registered)>=64: raise RuntimeError('owned process bound exceeded')
                    registered[item.pid] = item
                is_closing = (directory/'closing.json').exists() or (directory/'stop-request.json').exists()
                if is_closing and closing_deadline is None: closing_deadline = now+int(cleanup_seconds*1e9)
                if closing_deadline is None:
                    endpoint = directory/'runtime/control-endpoint.json'
                    if not manifest['interactive'] and connect_scripted_owner and endpoint.exists() and owner is None:
                        value = read_json(endpoint)
                        if value.get('session_id') != directory.name: raise ValueError('foreign scripted owner session')
                        events = None
                        if scripted_plan is not None:
                            from mc2p.runtime.segmented_trace import SegmentedJsonlWriter
                            events = SegmentedJsonlWriter(directory/'owner-events')
                        owner = _ScriptedOwner(value,token,events)
                    if owner is not None and not owner_disconnected:
                        try: owner.tick(now)
                        except EOFError:
                            closing=directory/'closing.json'
                            if (not closing.exists() or owner.pending
                                    or read_json(closing).get('reason') not in {'user_stop','test_deadline'}): raise
                            closing_deadline=time.perf_counter_ns()+int(cleanup_seconds*1e9)
                            continue  # Normal close may race the poll begun before closing.json existed.
                    if (directory/'ready.json').exists():
                        if ready_since is None:
                            ready_since = now; last_progress = now
                            if scripted_plan is not None:
                                write_json_atomic(directory/'case-start.json',dict(started_at_ns=now,case=scripted_plan['case']))
                        if scripted_plan is not None and owner is not None and owner.ready:
                            commands = scripted_plan['owner_commands']
                            while scripted_index<len(commands) and now-ready_since>=commands[scripted_index]['at_ns']:
                                item = commands[scripted_index]
                                if 'when' in item:
                                    if now-ready_since>item['latest_ns']+250_000_000: raise TimeoutError('stop trigger expired')
                                    if not (directory/f'command-trigger-{scripted_index:02}.json').exists(): break
                                owner.command(item['kind'],**{k:v for k,v in item.items() if k not in {'at_ns','kind','when','latest_ns'}})
                                scripted_index += 1
                            fault=scripted_plan.get('fault')
                            if (fault is not None and fault['kind']!='worker_stall' and not fault_injected
                                    and (directory/'fault-trigger.json').exists()):
                                proof=read_json(directory/'fault-trigger.json')
                                injected=time.perf_counter_ns()
                                if (proof['session_id']!=directory.name or proof['kind']!=fault['kind']
                                        or not 0<=injected-proof['sampled_at_ns']<=250_000_000):
                                    raise ValueError('foreign or stale fault trigger')
                                if owner.pending: raise RuntimeError('cannot inject fault with unconfirmed command')
                                if fault['kind']=='owner_heartbeat': owner.heartbeat_enabled=False
                                else: owner.socket.close(); owner_disconnected=True
                                write_json_atomic(directory/'fault-injection.json',dict(proof,injected_at_ns=injected))
                                fault_injected=True
                        beat=None
                        if (directory/'heartbeat.json').exists():
                            beat = read_json(directory/'heartbeat.json')
                            observed_now = time.perf_counter_ns()
                            if (beat.get('session_id') != directory.name or beat.get('worker_pid') != root.pid
                                    or beat.get('worker_create_time') != root.create_time or type(beat.get('sequence')) is not int
                                    or beat['sequence'] < last_sequence or type(beat.get('sampled_at_ns')) is not int
                                    or beat['sampled_at_ns']>observed_now or type(beat.get('owner_deadline_ns')) is not int
                                    or beat['owner_deadline_ns'] > observed_now+3_000_000_000): raise ValueError('invalid worker heartbeat')
                            if beat['sequence']>last_sequence:
                                last_sequence = beat['sequence']; last_progress = beat['sampled_at_ns']
                        primary=worker_liveness_failure(now,last_progress,beat,heartbeat_seconds)
                        duration = manifest['test_duration_seconds']
                        if duration is not None and now-ready_since>=duration*1_000_000_000:
                            request_stop(directory.name); closing_deadline = now+int(cleanup_seconds*1e9)
                    elif now>=manifest['startup_deadline_ns']: primary = 'startup_deadline'
                    if primary:
                        request_stop(directory.name); closing_deadline = now+int(cleanup_seconds*1e9)
                if closing_deadline is not None and now>=closing_deadline:
                    primary = primary or 'cleanup_deadline'; break
                time.sleep(.05)
        except BaseException as error:
            primary = primary or type(error).__name__+': '+str(error)
            request_stop(directory.name)
            stop_deadline = time.perf_counter_ns()+int(cleanup_seconds*1e9)
            while process is not None and process.poll() is None and time.perf_counter_ns()<stop_deadline:
                time.sleep(.05)
        finally:
            if owner is not None:
                try: owner.close()
                except BaseException as error: cleanup_failures.append('owner close: '+repr(error))
            if process is not None:
                if root is not None:
                    try:
                        if primary is None and any(identity_alive(asdict(item)) for item in registered.values()):
                            primary = 'owned_processes_required_forced_cleanup'
                        cleanup = terminate_registered_tree(root,tuple(registered.values()),grace_seconds=1)
                        cleanup_failures.extend(cleanup.errors)
                        if cleanup.surviving: cleanup_failures.append('owned processes survived')
                    except BaseException as error: cleanup_failures.append(str(error))
                elif process.poll() is None: process.terminate()  # Owned Popen HANDLE, no PID lookup.
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: cleanup_failures.append('worker did not stop')
    parent = dict(return_code=None if process is None else process.poll(),primary_failure=primary,
        cleanup_failures=cleanup_failures,process_stopped=process is None or process.poll() is not None,
        registered_processes=[asdict(i) for i in registered.values()])
    write_json_atomic(directory/'supervisor-result.json',parent)
    try: worker = read_json(directory/'worker-result.json')
    except (ValueError,OSError): worker = dict(primary_failure='missing_worker_result')
    parent_ok = parent['return_code']==0 and primary is None and not cleanup_failures and parent['process_stopped']
    if parent['process_stopped'] and not cleanup_failures and worker.get('cleanup',{}).get('status')=='passed' and worker.get('evidence',{}).get('status')=='pending':
        try:
            validated=_post_close_evidence(directory)
            previous=worker['evidence'].get('checks',[])
            names={c['name'] for c in validated.get('checks',[])}
            combined=[c for c in previous if c['name'] not in names]+validated.get('checks',[])
            worker=dict(worker,evidence=dict(validated,checks=combined,
                status='passed' if validated['status']=='passed' and all(c['passed'] for c in combined) else 'failed'))
        except BaseException as error:
            worker=dict(worker,evidence=dict(status='failed',error=repr(error),checks=worker['evidence'].get('checks',[])))
    reported_control_failure=worker.get('reason') if worker.get('reason') in {'owner_heartbeat_lost','owner_lease_insufficient','control_disconnected'} else None
    okay = (parent_ok and not reported_control_failure and not worker.get('primary_failure') and all(worker.get(k,{}).get('status')=='passed'
        for k in ('behavior','evidence','cleanup')) and not (directory/'startup-failure.json').exists())
    result = dict(worker,state='closed' if okay else 'failed',supervisor=parent,session_id=directory.name,
                  completed_at_ns=time.perf_counter_ns(),measurement_revision=MEASUREMENT_REVISION,
                  **dict(NAVIGATION_CONTRACT))
    if reported_control_failure: result['reported_control_failure']=reported_control_failure
    write_json_atomic(directory/'result.json',result)
    return 0 if okay else 1


def validate_closed_session(directory: Path) -> int:
    """Offline-only child: no host/client construction, actions, or live game dependencies."""
    from scripts.streaming_time_evidence import export_segmented_runtime_time_evidence
    manifest=read_manifest(directory); checks=[]; errors=[]; resources=None
    require_current_session(manifest)
    if (directory/'offline-evidence.json').exists(): raise FileExistsError('offline result already exists')
    try:
        if (directory/'case-plan.json').exists() and read_json(directory/'case-plan.json')['case']=='long-session':
            from scripts.follow_playground_resources import ResourceSampler
            resources=ResourceSampler(directory/'validator-rss','validator'); resources.start()
        for role in (('MC2PFollower',) if manifest['interactive'] else ('MC2PFollower','MC2PLeader')):
            source=directory/'runtime'/role
            result=export_segmented_runtime_time_evidence(source,source/'time-evidence')
            checks.append(dict(name=role+':time_attribution',passed=result['status']=='passed'))
    except BaseException as error: errors.append(repr(error))
    finally:
        if resources is not None:
            try: resources.close()
            except BaseException as error: errors.append(repr(error))
    passed=bool(checks) and all(c['passed'] for c in checks) and not errors
    write_json_atomic(directory/'offline-evidence.json',dict(schema_version='mc2p.playground-offline-evidence.v1',
        session_id=directory.name,status='passed' if passed else 'failed',checks=checks,errors=errors,
        measurement_revision=MEASUREMENT_REVISION,**dict(NAVIGATION_CONTRACT)))
    return 0 if passed else 1


def _post_close_evidence(directory: Path) -> dict:
    from scripts.probe_craftground_timing_parallel import run_bounded_process
    from mc2p.runtime.trace import trace_projection
    write_json_atomic(directory/'validating.json',dict(state='validating',games_closed=True,
        evidence_status='pending',started_at_ns=time.perf_counter_ns()))
    supervision=run_bounded_process([sys.executable,str(ROOT/'scripts/visual_demo.py'),
        'playground','_validate','--session-id',directory.name],cwd=ROOT,environment=dict(os.environ),
        log_path=directory/'offline-evidence-console.log',timeout_seconds=300)
    write_json_atomic(directory/'offline-evidence-supervisor.json',trace_projection(supervision))
    result=read_json(directory/'offline-evidence.json')
    require_navigation_contract(result)
    if result.get('measurement_revision')!=MEASUREMENT_REVISION:
        raise ValueError('foreign offline evidence measurement')
    if result['schema_version']!='mc2p.playground-offline-evidence.v1' or result['session_id']!=directory.name:
        raise ValueError('foreign offline evidence result')
    if (supervision.return_code!=0 or supervision.primary_failure or supervision.cleanup_failures or not supervision.process_stopped):
        result=dict(result,status='failed',supervisor=trace_projection(supervision))
    return result


class PlaygroundSession:
    def __init__(self, host, control, *, test_deadline_ns: int | None = None, clock_ns=time.perf_counter_ns,
                 perception_variant: str = 'm6_baseline', perception_config: dict | None = None):
        self.perception_config = validate_perception_configuration(perception_variant, perception_config)
        self.perception_variant = perception_variant
        if test_deadline_ns is not None and (type(test_deadline_ns) is not int or test_deadline_ns <= 0):
            raise ValueError('invalid explicit test deadline')
        self.host, self.control, self.test_deadline_ns = host, control, test_deadline_ns
        self.state, self.reason = 'idle', None
        self.mode, self.distance_blocks = 'auto', 3.0
        self.stop_requested = False
        self._close_result = None
        self._clock = clock_ns
        self.driver = self.task = None
        from mc2p.contracts.behavior import BehaviorProfileV0
        self.profile = BehaviorProfileV0()

    def status(self, now_ns: int) -> dict:
        import math
        from mc2p.skills.follow_tracking import project_playground_view
        decision = None if self.driver is None else self.driver.follower.last_decision
        target_age = None if decision is None or decision.target.last_seen_ns is None else max(0,now_ns-decision.target.last_seen_ns)
        motion = self.host.motion_status(now_ns) if hasattr(self.host,'motion_status') else dict(actual_motion='unknown')
        cognition=None if self.driver is None else self.driver.follower.cognition_status(now_ns)
        same_scope=False
        if self.driver is not None:
            observation=self.driver.runtime.observation
            belief=self.driver.follower.belief
            same_scope=belief is not None and belief.observation_scope==(
                observation.episode_id,observation.controller_clock_id,observation.client_sample.clock_id,observation.source_backend)
            cognition['observation_age_ns']=(now_ns-observation.request_started_at_monotonic_ns
                if same_scope and now_ns>=observation.request_started_at_monotonic_ns else None)
        current_distance=None
        if decision is not None and same_scope:
            observation=self.driver.runtime.observation
            view=project_playground_view(observation,now_ns,observation.controller_clock_id)
            if view.base.available and view.base.own is not None and 0<=now_ns-view.base.request_start_ns<=500_000_000:
                targets=[entity for entity in view.base.entities if entity.track_id==decision.target.track_id
                         and entity.entity_type=='minecraft:player']
                if len(targets)==1:
                    point,own=targets[0].position,view.base.own.position
                    current_distance=math.hypot(point.x-own.x,point.z-own.z)
                    cognition['target_observation_age_ns']=now_ns-view.base.request_start_ns
        return dict(state=self.state, requested_mode=self.mode,cognition=cognition,
                    selected_gait=None if decision is None or self.driver.reason=='controls_released' else decision.selected_gait,
                    auto_distance_blocks=self.distance_blocks, fixed_stop_blocks=2.5, fixed_restart_blocks=3.5,
                    fixed_retreat_blocks=1.5,reason=self.reason,**motion,
                    target_distance_blocks=current_distance,
                    last_target_distance_blocks=None if self.driver is None else self.driver.last_visible_distance,
                    target_age_ns=target_age,task_id=None if self.task is None else self.task.task_id,
                    attempt_id=None if self.driver is None else self.driver.attempt_id,sampled_at_ns=now_ns)

    def _start(self, now_ns: int) -> str | None:
        from mc2p.skills.follow_playground import PlaygroundFollower
        from mc2p.skills.follow_playground_driver import PlaygroundDriver
        from mc2p.skills.follow_tracking import project_playground_view
        from mc2p.contracts.common import ContractViolation
        from scripts.follow_scenarios import follow_task
        runtime = getattr(self.host,'follower',None)
        if runtime is None: return 'follow_runtime_unavailable'
        if self.control.owner_deadline_ns-now_ns<250_000_000: return 'owner_lease_insufficient'
        if self.driver is not None:
            if self.state not in {'blocked','waiting_target'}: return None
            self._stop_follow('explicit_retry')
        now_ns=self._clock()
        if self.control.owner_deadline_ns-now_ns<250_000_000: return 'owner_lease_insufficient'
        try:
            observation = runtime.observation
            view = project_playground_view(observation,now_ns,observation.controller_clock_id)
            task = replace(follow_task(min(now_ns+250_000_000,self.control.owner_deadline_ns)),
                           task_id='playground-'+uuid.uuid4().hex,task_type='playground_follow')
            validate_perception_configuration(self.perception_variant,self.perception_config)
            from mc2p.skills.perception_needs import PerceptionConfig
            follower = PlaygroundFollower(task.task_id,view,mode=self.mode,
                perception_variant=self.perception_variant,
                perception_config=None if self.perception_variant=='m6_baseline' else PerceptionConfig(**self.perception_config))
            if self.mode=='auto': follower.set_distance(self.distance_blocks)
        except ContractViolation: return 'no_fresh_unique_visible_target'
        self.task,self.driver = task,PlaygroundDriver(runtime,follower,self._clock)
        self.host.last_actor_result = self.driver.tick(self.task,self.profile,self.control.owner_deadline_ns)
        self.state,self.reason = self.driver.state,self.driver.reason
        if self.driver.state=='stopped':
            self.driver,self.task = None,None
            self.state = 'idle'
            return self.reason
        return None

    def _stop_follow(self, reason: str) -> None:
        if self.driver is not None:
            if self.driver.state!='stopped': self.host.last_actor_result = self.driver.stop(self.task,self.profile,reason)
            self.driver,self.task = None,None
        self.state,self.reason = 'idle',reason

    def handle(self, command: DemoCommandV1, now_ns: int) -> dict:
        phase, reason = 'applied', None
        if (self.state == 'closed' or not self.control.authenticated or now_ns >= self.control.owner_deadline_ns):
            phase, reason = 'rejected', 'owner_not_authorized'
        elif self.stop_requested and command.kind!='follow_status': phase,reason = 'rejected','session_stopping'
        elif command.kind == 'follow_start':
            reason = self._start(now_ns)
            if reason: phase = 'rejected'
        elif command.kind == 'follow_stop': self._stop_follow('player_stop')
        elif command.kind == 'follow_mode':
            if self.driver is not None and self.mode!=command.mode:
                self.host.last_actor_result = self.driver.release(self.task,self.profile,'mode_change')
                self.driver.follower.set_mode(command.mode)
                if command.mode=='auto': self.driver.follower.set_distance(self.distance_blocks)
                self.state,self.reason = self.driver.state,self.driver.reason
            self.mode = command.mode
        elif command.kind == 'follow_distance':
            if self.mode != 'auto': phase, reason = 'rejected', 'distance_requires_auto'
            else:
                if self.driver is not None: self.driver.follower.set_distance(command.distance_blocks)
                self.distance_blocks = command.distance_blocks
        elif command.kind == 'demo_stop': self.stop_requested = True
        elif command.kind != 'follow_status': phase, reason = 'rejected', 'unsupported_session_command'
        status = self.status(self._clock())
        if reason: status['reason'] = reason
        return dict(phase=phase, status=status)

    def handle_batch(self, commands: list[DemoCommandV1], now_ns: int) -> dict:
        if len(commands)>32: raise ValueError('command batch exceeds bounded queue')
        has_stop = any(c.kind in {'follow_stop','demo_stop'} for c in commands)
        replies = {}
        for command in sorted(commands,key=lambda c: c.kind not in {'follow_stop','demo_stop'}):
            if has_stop and command.kind=='follow_start':
                replies[command.sequence] = dict(phase='rejected',status=dict(self.status(self._clock()),reason='stop_preempted_command'))
            else: replies[command.sequence] = self.handle(command,self._clock())
        return replies

    def tick(self, now_ns: int) -> None:
        if self.state == 'closed': return
        if self.stop_requested: self.close('user_stop'); return
        if not self.control.authenticated: self.close('control_disconnected'); return
        if now_ns >= self.control.owner_deadline_ns: self.close('owner_heartbeat_lost'); return
        if self.control.owner_deadline_ns-now_ns<250_000_000: self.close('owner_lease_insufficient'); return
        if self.test_deadline_ns is not None and now_ns >= self.test_deadline_ns:
            self.close('test_deadline'); return
        if self.driver is None:
            self.host.idle_tick(min(now_ns+250_000_000, self.control.owner_deadline_ns))
        else:
            paired=hasattr(self.host,'paired_tick')
            if paired:
                result=self.host.paired_tick(lambda:self.driver.tick(self.task,self.profile,self.control.owner_deadline_ns),
                    min(self._clock()+250_000_000,self.control.owner_deadline_ns))
            else: result = self.driver.tick(self.task,self.profile,self.control.owner_deadline_ns)
            if result is not None: self.host.last_actor_result = result
            self.state,self.reason = self.driver.state,self.driver.reason
            if self.driver.state=='stopped':
                self.driver,self.task = None,None
                self.state = 'idle'
            if not paired and hasattr(self.host,'maintain_leader'):
                self.host.maintain_leader(min(self._clock()+250_000_000,self.control.owner_deadline_ns))

    def close(self, reason: str) -> dict:
        if self._close_result is not None: return self._close_result
        primary = None
        try:
            if self.driver is not None and self.driver.runtime.state.value=='ready': self._stop_follow(reason)
        except BaseException as error:
            primary = error
        finally:
            self.state,self.reason = 'closed',reason
            try: self.host.close()
            except BaseException as error:
                if primary is None: primary = error
                else: primary.add_note('host cleanup also failed: '+repr(error))
            self._close_result = dict(state='closed',reason=reason)
        if primary is not None:
            self._close_result['cleanup_failure'] = type(primary).__name__
            raise primary
        return self._close_result


def run_worker(directory: Path) -> int:
    from mc2p.backends.deployment_transport import ClientProcessIdentity
    from scripts.follow_playground_host import PlaygroundHost
    manifest = read_manifest(directory)
    require_current_session(manifest)
    host = session = case_runner = resources = None; primary = None; reason = 'user_stop'; heartbeat_sequence = 0; diagnostic_failures = []
    try:
        if not (directory/'stop-request.json').exists():
            owner_identity = None if manifest['interactive'] else ClientProcessIdentity(
                int(os.environ['MC2P_PLAYGROUND_OWNER_PID']),float(os.environ['MC2P_PLAYGROUND_OWNER_CREATE_TIME']))
            host = PlaygroundHost(directory/'runtime',manifest['seed'],interactive=manifest['interactive'],
                test_deadline_ns=None,owner_identity=owner_identity,control_token=os.environ['MC2P_PLAYGROUND_CONTROL_TOKEN'],
                stop_path=directory/'stop-request.json',session_id=directory.name,scene=manifest['scene'])
            host.start()
            ready = time.perf_counter_ns()
            if ready >= manifest['startup_deadline_ns']: raise TimeoutError('playground startup deadline')
            duration = manifest['test_duration_seconds']
            deadline = None if duration is None else ready+duration*1_000_000_000
            host.test_deadline_ns = deadline
            session = PlaygroundSession(host,host.control,test_deadline_ns=deadline,
                perception_variant=manifest['perception_variant'],perception_config=manifest['perception_config'])
            if (directory/'case-plan.json').exists():
                from scripts.follow_playground_scenarios import PlaygroundCaseRunner
                case_runner = PlaygroundCaseRunner(directory,session)
                if case_runner.plan['case']=='long-session':
                    from scripts.follow_playground_resources import ResourceSampler
                    resources=ResourceSampler(directory/'worker-rss','worker'); resources.start()
            write_json_atomic(directory/'ready.json',dict(state='idle',ready_at_ns=ready,test_deadline_ns=deadline,
                interactive=manifest['interactive'],live_visual_verification='pending_user_request' if manifest['interactive'] else 'not_requested'))
            last_heartbeat = 0
            while session.state != 'closed':
                if resources is not None: resources.check()
                host.check_stop()
                now = time.perf_counter_ns()
                if case_runner is not None: case_runner.before_tick(now)
                if now-last_heartbeat>=1_000_000_000:
                    heartbeat_sequence += 1; last_heartbeat = now
                    write_json_atomic(directory/'heartbeat.json',dict(session_id=directory.name,sequence=heartbeat_sequence,
                        worker_pid=os.getpid(),worker_create_time=psutil.Process().create_time(),sampled_at_ns=now,
                        owner_deadline_ns=host.control.owner_deadline_ns))
                    write_json_atomic(directory/'worker-status.json',session.status(now))
                commands = []
                for _ in range(32):
                    command = host.control.poll()
                    if command is None: break
                    commands.append(command)
                # Stop has precedence within the bounded received batch; no extra I/O in callbacks.
                for sequence,reply in session.handle_batch(commands,time.perf_counter_ns()).items():
                    host.control.reply(sequence,reply['phase'],reply['status'])
                if session.stop_requested or (deadline is not None and time.perf_counter_ns()>=deadline):
                    write_json_atomic(directory/'closing.json',dict(reason='user_stop' if session.stop_requested else 'test_deadline'))
                session.tick(time.perf_counter_ns())
                if case_runner is not None and session.state!='closed': case_runner.after_tick(time.perf_counter_ns())
                # One cadence interval from this iteration, no extra sleep after slow I/O or catch-up burst.
                remaining = now+50_000_000-time.perf_counter_ns()
                if remaining>0: time.sleep(remaining/1_000_000_000)
            reason = session.reason
    except InterruptedError as error: reason = str(error)
    except BaseException as error:
        import traceback
        traceback.print_exc()  # Preserve the failing boundary even for empty Future timeouts; no locals.
        primary = dict(type=type(error).__name__,message=str(error)); reason = 'worker_failure'
    finally:
        if resources is not None:
            try: resources.close()
            except BaseException as error:
                primary=primary or dict(type=type(error).__name__,message=str(error))
        if case_runner is not None:
            try: case_runner.close()
            except BaseException as error:
                primary = primary or dict(type=type(error).__name__,message=str(error))
        try: write_json_atomic(directory/'closing.json',dict(reason=reason))
        except BaseException as error:
            diagnostic_failures.append(dict(stage='closing_record',type=type(error).__name__,message=str(error)))
            primary = primary or diagnostic_failures[-1]
        if host is not None:
            try:
                if session is not None: session.close(reason)
                else: host.close()
            except BaseException as error:
                primary = primary or dict(type=type(error).__name__,message=str(error))
    failures = [] if host is None else host.cleanup_failures
    checks = [] if host is None else host.checks
    sources_after = {}
    try:
        sources_after = {name:_hash(ROOT/name) for name in CORE_SOURCES}
        if sources_after != manifest['core_sources']:
            raise ValueError('playground Python sources changed during worker execution')
    except (OSError,ValueError) as error:
        primary = primary or dict(type=type(error).__name__,message=str(error))
        checks.append(dict(name='python_sources_stable',passed=False))
    deferred={p.name+':time_attribution' for p in getattr(host,'deferred_time_directories',())}
    evidence_ok = not diagnostic_failures and all(c['passed'] or c['name'] in deferred for c in checks)
    result = dict(primary_failure=primary,reason=reason,diagnostic_failures=diagnostic_failures,
        core_sources_after=sources_after,
        behavior=dict(status='passed' if primary is None else 'failed',scope='persistent_session_control_only'),
        evidence=dict(status=('pending' if deferred else 'passed') if evidence_ok else 'failed',checks=checks),
        cleanup=dict(status='passed' if not failures else 'failed',failures=failures),
        measurement_revision=MEASUREMENT_REVISION,**dict(NAVIGATION_CONTRACT))
    write_json_atomic(directory/'worker-result.json',result)
    return 0 if primary is None and evidence_ok and not failures else 1


def start_session(seed: int, *, interactive: bool = True, test_duration_seconds: int | None = None, scene: str = 'flat') -> dict:
    directory = create_session(seed,interactive=interactive,test_duration_seconds=test_duration_seconds,scene=scene)
    manifest = read_manifest(directory)
    with (directory/'launcher-console.log').open('xb') as output:
        try:
            process = subprocess.Popen([str(ROOT/'.venv/pythonw.exe'),str(ROOT/'scripts/visual_demo.py'),'playground',
                '_supervise','--session-id',directory.name],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,
                stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            write_json_atomic(directory/'launcher.json',dict(pid=process.pid,create_time=psutil.Process(process.pid).create_time()))
        except BaseException as error:
            write_json_atomic(directory/'startup-failure.json',dict(primary_failure=type(error).__name__+': '+str(error)))
            request_stop(directory.name)
            return read_status(directory.name)
    while time.perf_counter_ns() < manifest['startup_deadline_ns']:
        if (directory/'result.json').exists() or (directory/'ready.json').exists(): return read_status(directory.name)
        if process.poll() is not None: break
        time.sleep(.1)
    write_json_atomic(directory/'startup-failure.json',dict(primary_failure='supervisor_exit_or_startup_timeout'))
    request_stop(directory.name)
    return read_status(directory.name)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Persistent opt-in flat follow playground')
    commands = parser.add_subparsers(dest='command',required=True)
    for name in ('prepare','start'):
        sub = commands.add_parser(name)
        sub.add_argument('--seed',type=int,choices=(21001,21002,21003),default=21001)
        sub.add_argument('--scene',choices=('flat','search'),default='flat')
        if name=='prepare': sub.add_argument('--output',type=Path,required=True)
    for name in ('status','stop','_supervise','_worker','_validate'):
        commands.add_parser(name).add_argument('--session-id',required=True)
    args = parser.parse_args(argv)
    if args.command == '_validate': return validate_closed_session(session_path(args.session_id))
    if args.command == '_worker': return run_worker(session_path(args.session_id))
    if args.command == '_supervise':
        return supervise_command(session_path(args.session_id),[str(ROOT/'.venv/pythonw.exe'),
            str(ROOT/'scripts/visual_demo.py'),'playground','_worker','--session-id',args.session_id])
    if args.command == 'prepare':
        from scripts.follow_playground_fixture import prepare_playground
        from scripts.demo_control_launch import prepare_human_launch, PROJECT
        directory = args.output.absolute()
        if '..' in directory.parts: raise ValueError('preparation parent escape')
        _no_links(directory.parent); directory.mkdir(exist_ok=False)
        fixture = prepare_playground(directory/'fixture',args.seed,scenario='playground_search' if args.scene=='search' else 'playground')
        prepared = prepare_human_launch(directory/'human',PROJECT/'build/launch/client-launch.json',server_port=25598,
            control_port=54321,token=secrets.token_hex(32),session_id='preparation-only',base_environment={})
        result = _prepared_report(
            scene=args.scene, fixture=fixture, human_launch=prepared['launch_evidence'])
        write_json_atomic(directory/'prepared.json',result)
    elif args.command == 'start': result = start_session(args.seed,scene=args.scene)
    elif args.command == 'status': result = read_status(args.session_id)
    else: result = request_stop(args.session_id)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return 1 if result.get('state')=='failed' else 0
