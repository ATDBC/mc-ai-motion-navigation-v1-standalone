"""Owned persistent playground roles; only explicit interactive start creates a human window."""
from __future__ import annotations
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

import psutil
from mc2p.backends.deployment_transport import ClientProcessIdentity
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.runtime.segmented_trace import SegmentedJsonlWriter, SegmentedTraceWriter
from scripts.control_probe_core import write_json_atomic
from scripts.demo_control_server import DemoControlServer
from scripts.demo_control_launch import prepare_human_launch, PROJECT as HUMAN_PROJECT
from scripts.fabric_deployment_launch import ROOT, inspect_launch, verify_assets, _hash
from scripts.fabric_deployment_sandbox import JAVA, prepare_server, client_environment
from scripts.follow_playground_fixture import prepare_playground, install_playground
from scripts.follow_runtime_host import FollowRuntimeHost
from scripts.follow_scenarios import follow_task, _neutral
from scripts.probe_fabric_deployment_observation import PROXY_ARGS, _live, _stop, _connection_proof
from scripts.streaming_time_evidence import export_segmented_runtime_time_evidence
from scripts.visibility_fixture_world import _no_links


class PlaygroundDiagnosticTrace:
    def __init__(self, directory: Path, backend):
        self.backend = backend
        self.trace = SegmentedTraceWriter(directory/'trace')
        try: self.diagnostics = SegmentedJsonlWriter(directory/'diagnostics')
        except BaseException:
            self.trace.close()
            raise

    def record_diagnostic(self, observation):
        self.diagnostics.write(dict(episode_id=observation.episode_id, observation_sequence_id=observation.sequence_id,
                                    diagnostics=self.backend.last_diagnostics))

    def write(self, record_type: str, payload: object):
        self.trace.write(record_type, payload)
        observation = None
        if record_type == 'reset' and payload['result'].succeeded: observation = payload['result'].observation
        elif record_type in {'step','close_release'}: observation = payload['backend_result'].observation
        if observation is not None: self.record_diagnostic(observation)

    def close(self):
        primary = None
        try: self.trace.close()
        except BaseException as error: primary = error
        try: self.diagnostics.close()
        except BaseException as error:
            if primary is None: primary = error
            else: primary.add_note('diagnostic close: '+repr(error))
        if primary is not None: raise primary


class PlaygroundHost(FollowRuntimeHost):
    def __init__(self, run_dir: Path, seed: int, *, interactive: bool, test_deadline_ns: int | None,
                 owner_identity: ClientProcessIdentity | None = None, control_token: str | None = None,
                 server_port: int = 25598, actor_port: int = 8144, leader_port: int = 8145,
                 stop_path: Path | None = None, launch_path: Path | None = None, session_id: str | None = None,
                 scene: str = 'flat'):
        if type(interactive) is not bool: raise ValueError('explicit interactive selection required')
        if scene not in ('flat','search'): raise ValueError('undeclared playground scene')
        self.fixture_scenario='playground_search' if scene=='search' else 'playground'
        if test_deadline_ns is not None and (type(test_deadline_ns) is not int or test_deadline_ns <= 0):
            raise ValueError('invalid test deadline')
        super().__init__(run_dir,seed,server_port,actor_port,leader_port, scenario='static', launch_path=launch_path,
                         deadline_ns=time.perf_counter_ns()+150_000_000_000)
        self.interactive, self.test_deadline_ns = interactive, test_deadline_ns
        self.owner_identity, self.control_token = owner_identity, control_token
        self.control_session_id = session_id or self.run_dir.name
        self.stop_path = stop_path
        self.control = None
        self.human_process = self.human_identity = self.human_output = None
        self.human_directory = self.run_dir/'human'
        self._motion_reader = None
        self.last_actor_result = None
        self.leader_controller = None
        self._leader_executor = None
        self.deferred_time_directories = []

    def _client_environment(self, token: str, port: int) -> dict:
        result = super()._client_environment(token,port)
        result['MC2P_TIME_SEGMENTED'] = '1'
        result['MC2P_MOVEMENT_DIAGNOSTICS'] = '1'
        return result

    def _create_trace(self, directory: Path, backend):
        return PlaygroundDiagnosticTrace(directory,backend)

    def _export_time_evidence(self, directory: Path) -> dict:
        self.deferred_time_directories.append(directory)
        return dict(status='pending')  # Parent validates only after worker and every owned game have exited.

    def check_stop(self):
        if self.stop_path is not None and self.stop_path.exists(): raise InterruptedError('user_stop')
        if self.human_process is not None and self.human_process.poll() is not None: raise InterruptedError('owner_exited')
        if self.control is not None and self.control.failure is not None: raise RuntimeError(self.control.failure)

    def idle_tick(self, deadline_ns: int):
        self.check_stop()
        def actor_step():
            result=_neutral(self.follower,follow_task(deadline_ns),BehaviorProfileV0(),deadline_ns)
            self.last_actor_result=result
            return result
        if not self.interactive and len(self.clients)>1: self.paired_tick(actor_step,deadline_ns)
        else: actor_step()

    def paired_tick(self, actor_step, deadline_ns: int):
        """Independent normal clients share wall time, not each other's actor observations."""
        if self.interactive: return actor_step()
        if self._leader_executor is None:
            self._leader_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='playground-leader')
        future=self._leader_executor.submit(self.maintain_leader,deadline_ns)
        primary=None; result=None
        try: result=actor_step()
        except BaseException as error: primary=error
        try: future.result(timeout=max(.001,(deadline_ns-time.perf_counter_ns())/1e9))
        except BaseException as error:
            if primary is None: primary=error
            else: primary.add_note('paired leader: '+repr(error))
        if primary is not None: raise primary
        return result

    def maintain_leader(self, deadline_ns: int):
        self.check_stop()
        if not self.interactive:
            if self.leader_controller is not None: self.leader_controller(self.leader,deadline_ns)
            else: _neutral(self.leader,follow_task(deadline_ns),BehaviorProfileV0(),deadline_ns)

    def motion_status(self, now_ns: int) -> dict:
        if not self.clients or self.follower.observation is None: return dict(actual_motion='unknown')
        if self._motion_reader is None:
            from scripts.follow_playground_motion import MovementStatusReader
            self._motion_reader = MovementStatusReader(self.clients[0].directory)
        return self._motion_reader.status(self.follower.observation,now_ns)

    def start(self):
        if self._closed or self.run_dir.exists(): raise RuntimeError('playground cannot restart')
        if not self.interactive and self.owner_identity is None:
            raise ValueError('headless playground requires an explicitly identified scripted control owner')
        _no_links(self.launch_path)
        launch = json.loads(self.launch_path.read_text('utf-8'))
        self.provenance = dict(launch=inspect_launch(launch),assets=verify_assets())
        if psutil.virtual_memory().available < 7*1024**3: raise RuntimeError('insufficient 7 GiB memory headroom')
        self.run_dir.mkdir(exist_ok=False)
        case_path=self.run_dir.parent/'case-plan.json'
        scenario=self.fixture_scenario
        if case_path.exists():
            from scripts.follow_playground_scenarios import validate_case_plan
            case=json.loads(case_path.read_text('utf-8'))
            if self.interactive: raise ValueError('invalid automated scene declaration')
            validate_case_plan(case)
            if case['case']=='tracking': scenario='playground_tracking'
            if case['case'] in {'search','search-missing'}: scenario=case['fixture_scenario']
        self.provenance['fixture'] = prepare_playground(self.run_dir/'fixture', self.seed,scenario=scenario)
        self.provenance['server'] = prepare_server(self.run_dir/'server',seed=self.seed,port=self.ports[0])
        properties = self.run_dir/'server/server.properties'
        contents = properties.read_text('utf-8')
        if any(line.startswith('force-gamemode=') for line in contents.splitlines()):
            raise ValueError('unexpected generated force-gamemode setting')
        with properties.open('a',encoding='utf-8') as stream: stream.write('force-gamemode=false\n')
        self.provenance['server']['properties_sha256'] = _hash(properties)
        install_playground(self.run_dir/'fixture',self.run_dir/'server/world')
        token = self.control_token or secrets.token_hex(32)
        identity = self.owner_identity
        self.control = DemoControlServer(self.control_session_id,token,
            None if self.interactive else identity.pid, None if self.interactive else identity.create_time)
        self.control.start()
        write_json_atomic(self.run_dir/'control-endpoint.json',dict(session_id=self.control_session_id,port=self.control.port))
        self.server_log = (self.run_dir/'server-console.log').open('xb')
        environment = client_environment(dict(os.environ),token='0'*64,server_port=self.ports[0],ipc_port=self.ports[1])
        environment = {k:v for k,v in environment.items() if not k.startswith('MC2P_')}
        self.server = subprocess.Popen([str(JAVA),'-Xms256M','-Xmx1G',*PROXY_ARGS,'-jar',
            str(self.run_dir/'server/server.jar'),'nogui'],cwd=self.run_dir/'server',env=environment,
            stdin=subprocess.PIPE,stdout=self.server_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        self.server_identity = ClientProcessIdentity(self.server.pid,psutil.Process(self.server.pid).create_time())
        write_json_atomic(self.run_dir/'server-identity.json',asdict(self.server_identity))
        ready_deadline = min(self.deadline_ns,time.perf_counter_ns()+60_000_000_000)
        # Startup log has a finite 60-second lifetime; later continuous logs are not accumulated here.
        while 'Done (' not in (self.run_dir/'server-console.log').read_text('utf-8',errors='replace'):
            self.check_stop(); _live(self.server_identity)
            if time.perf_counter_ns() >= ready_deadline: raise TimeoutError('playground server startup')
            time.sleep(.05)
        if self.interactive:
            human = prepare_human_launch(self.human_directory,HUMAN_PROJECT/'build/launch/client-launch.json',
                server_port=self.ports[0],control_port=self.control.port,token=token,session_id=self.control_session_id,
                base_environment=dict(os.environ))
            self.human_output = (self.human_directory/'console.log').open('xb')
            self.human_process = subprocess.Popen(human['command'],cwd=self.human_directory,env=human['environment'],
                stdin=subprocess.DEVNULL,stdout=self.human_output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            self.human_identity = ClientProcessIdentity(self.human_process.pid,psutil.Process(self.human_process.pid).create_time())
            self.control.bind_owner(self.human_identity.pid,self.human_identity.create_time)
            write_json_atomic(self.human_directory/'identity.json',asdict(self.human_identity))
        self.control_token = None
        self._launch_client('MC2PFollower',self.ports[1],launch)
        if not self.interactive: self._launch_client('MC2PLeader',self.ports[2],launch)
        for client in self.clients:
            self.check_stop(); self._connect_client(client)
        while not self.control.authenticated:
            self.check_stop()
            if time.perf_counter_ns() >= self.deadline_ns: raise TimeoutError('playground owner authentication')
            self.idle_tick(min(self.deadline_ns,time.perf_counter_ns()+250_000_000))
            time.sleep(.05)
        if self.interactive:
            from scripts.visual_windows import owned_windows
            while True:
                self.check_stop()
                windows = owned_windows(self.human_identity)
                joined = 'MC2PLeader joined the game' in (self.run_dir/'server-console.log').read_text('utf-8',errors='replace')
                if len(windows)==1 and joined:
                    proof = _connection_proof(self.server_identity,self.human_identity,None,self.ports[0])
                    write_json_atomic(self.run_dir/'human-ready.json',dict(window=windows[0],connection=proof,
                        render_and_manual_input='pending_human_confirmation'))
                    break
                if time.perf_counter_ns() >= self.deadline_ns: raise TimeoutError('human game readiness')
                self.idle_tick(min(self.deadline_ns,time.perf_counter_ns()+250_000_000)); time.sleep(.05)
        write_json_atomic(self.run_dir/'provenance.json',self.provenance)
        write_json_atomic(self.run_dir/'ready.json',dict(state='idle',interactive=self.interactive,
            control_port=self.control.port,owner_deadline_ns=self.control.owner_deadline_ns,
            identities=[asdict(c.identity) for c in self.clients],test_deadline_ns=self.test_deadline_ns))

    def close(self):
        if self._closed: return
        if self._leader_executor is not None:
            self._leader_executor.shutdown(wait=False,cancel_futures=True)
            self._leader_executor=None
        try:
            if self.human_process is not None:
                from scripts.visual_windows import close_owned_windows
                if self.human_process.poll() is None and self.human_identity is not None: close_owned_windows(self.human_identity)
                cleanup = _stop(self.human_process,self.human_identity)
                write_json_atomic(self.run_dir/'human-cleanup.json',cleanup)
                if not cleanup['passed'] or not cleanup['graceful']: self.cleanup_failures.append(dict(human=cleanup))
        except BaseException as error: self.cleanup_failures.append(dict(human_error=repr(error)))
        finally:
            try:
                if self.control is not None: self.control.close()
            except BaseException as error: self.cleanup_failures.append(dict(control_error=repr(error)))
            try:
                if self.human_output is not None: self.human_output.close()
            except BaseException as error: self.cleanup_failures.append(dict(human_log_error=repr(error)))
            finally: super().close()
