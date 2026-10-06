"""Owned two-client local functional session, not a training capacity experiment."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

import psutil

from mc2p.backends.deployment_transport import ClientProcessIdentity, DeploymentTransport
from mc2p.backends.fabric_behavior import FabricBehaviorBackendV1
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import JsonlTraceWriterV0
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fabric_deployment_launch import ROOT, inspect_launch, verify_assets
from scripts.fabric_deployment_sandbox import JAVA, client_environment, prepare_server, write_argument_file
from scripts.follow_fixture_world import CASES, PLAYERS, build_follow_fixture, install_follow_fixture, offline_uuid
from scripts.follow_evidence import export_follow_time_evidence
from scripts.probe_fabric_deployment_observation import (
    PROXY_ARGS, _close_client, _connection_proof, _live, _stop, port_free, validate_connection_evidence,
)
from scripts.visibility_fixture_world import _no_links


class DiagnosticTrace:
    """Record the adapter sidecar alongside each formal trace sample, never into policy input."""
    def __init__(self, directory: Path, backend: FabricBehaviorBackendV1):
        self.directory, self.backend = directory, backend
        self.trace = JsonlTraceWriterV0(directory/"trace.jsonl")

    def write(self, record_type: str, payload: object) -> None:
        self.trace.write(record_type, payload)
        observation = None
        if record_type == "reset" and payload["result"].succeeded:
            observation = payload["result"].observation
        elif record_type in {"step", "close_release"}:
            observation = payload["backend_result"].observation
        if observation is not None:
            append_jsonl(self.directory/"diagnostics.jsonl", dict(episode_id=observation.episode_id,
                observation_sequence_id=observation.sequence_id, diagnostics=self.backend.last_diagnostics))

    def close(self) -> None:
        self.trace.close()


@dataclass
class OwnedClient:
    name: str
    directory: Path
    port: int
    transport: DeploymentTransport | None = None
    process: subprocess.Popen | None = None
    identity: ClientProcessIdentity | None = None
    backend: FabricBehaviorBackendV1 | None = None
    runtime: PlayerRuntimeV1 | None = None
    output: object = None


class FollowRuntimeHost:
    def __init__(self, run_dir: Path, seed: int = 21001, server_port: int = 25597,
                 follower_port: int = 8141, leader_port: int = 8142, *, scenario: str = "static",
                 client_names: tuple[str, str] = PLAYERS, launch_path: Path | None = None,
                 deadline_ns: int | None = None):
        path = Path(run_dir)
        if ".." in path.parts:
            raise ValueError("follow run cannot escape through parent segments")
        self.run_dir = path.absolute()
        _no_links(self.run_dir.parent)
        if self.run_dir.exists(): raise FileExistsError("follow session directory must be new")
        if type(seed) is not int or seed not in (21001, 21002, 21003) or scenario not in CASES:
            raise ValueError("undeclared follow scene")
        ports = (server_port, follower_port, leader_port)
        if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 3:
            raise ValueError("follow ports must be distinct valid loopback ports")
        if type(client_names) is not tuple or client_names != PLAYERS or len(set(client_names)) != 2:
            raise ValueError("follow roles need distinct declared normal identities")
        if any(not port_free(p) for p in ports):
            raise RuntimeError("follow port is occupied; existing owners are not modified")
        self.seed, self.scenario, self.ports = seed, scenario, ports
        self.launch_path = launch_path or ROOT/"deployment/fabric-observation-probe/build/launch/client-launch.json"
        self.deadline_ns = deadline_ns or time.perf_counter_ns()+240_000_000_000
        self.clients: list[OwnedClient] = []
        self.server = self.server_identity = self.server_log = None
        self.cleanup_failures: list[dict] = []
        self.checks: list[dict] = []
        self.provenance = {}
        self._closed = False

    @property
    def follower(self) -> PlayerRuntimeV1:
        return self.clients[0].runtime

    @property
    def leader(self) -> PlayerRuntimeV1:
        return self.clients[1].runtime

    def __enter__(self) -> FollowRuntimeHost:
        try:
            self.start()
            return self
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note("follow host cleanup failed: " + repr(cleanup_error))
            raise

    def start(self) -> None:
        if self._closed or self.run_dir.exists(): raise RuntimeError("follow host cannot restart")
        _no_links(self.launch_path.absolute())
        launch = json.loads(self.launch_path.read_text("utf-8"))
        self.provenance = dict(launch=inspect_launch(launch), assets=verify_assets())
        if psutil.virtual_memory().available < 7*1024**3:
            raise RuntimeError("two-player functional scene requires 7 GiB free memory headroom")
        self.run_dir.mkdir(exist_ok=False)
        self.provenance["fixture"] = build_follow_fixture(self.run_dir/"fixture", seed=self.seed, scenario=self.scenario)
        self.provenance["server"] = prepare_server(self.run_dir/"server", seed=self.seed, port=self.ports[0])
        install_follow_fixture(self.run_dir/"fixture", self.run_dir/"server/world")
        write_json_atomic(self.run_dir/"provenance.json", self.provenance)
        self.server_log = (self.run_dir/"server-console.log").open("xb")
        environment = client_environment(dict(os.environ), token="0"*64, server_port=self.ports[0], ipc_port=self.ports[1])
        environment = {k:v for k,v in environment.items() if not k.startswith("MC2P_")}
        command = [str(JAVA), "-Xms256M", "-Xmx1G", *PROXY_ARGS, "-jar", str(self.run_dir/"server/server.jar"), "nogui"]
        write_json_atomic(self.run_dir/"server-command.json", command)
        self.server = subprocess.Popen(command, cwd=self.run_dir/"server", env=environment, stdin=subprocess.PIPE,
            stdout=self.server_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        self.server_identity = ClientProcessIdentity(self.server.pid, psutil.Process(self.server.pid).create_time())
        write_json_atomic(self.run_dir/"server-identity.json", asdict(self.server_identity))
        ready_deadline = min(self.deadline_ns, time.perf_counter_ns()+60_000_000_000)
        while 'Done (' not in (self.run_dir/"server-console.log").read_text("utf-8", errors="replace"):
            _live(self.server_identity)
            if time.perf_counter_ns() >= ready_deadline: raise TimeoutError("follow server readiness deadline")
            time.sleep(.05)
        for name, port in zip(PLAYERS, self.ports[1:]):
            self._launch_client(name, port, launch)
        # Both terrain screens load before either established session is left idle
        # waiting through another JVM's 30s vanilla screen readiness fallback.
        for client in self.clients:
            self._connect_client(client)
        self.checks.append(dict(name="simultaneous_distinct_players", passed=len({c.identity.pid for c in self.clients}) == 2
                                and len({offline_uuid(c.name) for c in self.clients}) == 2))
        for client in self.clients:
            _live(client.identity)
        write_json_atomic(self.run_dir/"ready.json", dict(state="ready", clients=[dict(name=c.name, **asdict(c.identity)) for c in self.clients]))

    def _launch_client(self, name: str, port: int, launch: dict) -> None:
        directory = self.run_dir/name
        directory.mkdir(exist_ok=False)
        owned = OwnedClient(name, directory, port)
        self.clients.append(owned)  # Register partial lifecycle before any launch/transport can fail.
        (directory/"options.txt").write_text("pauseOnLostFocus:false\nrenderDistance:2\nsimulationDistance:5\n"
            "maxFps:60\nenableVsync:false\ntutorialStep:none\njoinedFirstServer:true\nskipMultiplayerWarning:true\n"
            "soundCategory_master:0.0\nautoJump:false\n", encoding="utf-8")
        token = secrets.token_hex(32)
        environment = self._client_environment(token, port)
        arguments = ["-Xms256M", "-Xmx2G", *PROXY_ARGS, "-Djava.net.preferIPv4Stack=true", *launch["jvm_args"],
            "-cp", os.pathsep.join(str(ROOT/item["path"]) for item in launch["classpath"]), launch["main_class"],
            "--username", name, "--uuid", offline_uuid(name), "--accessToken", "0", "--version", "1.21",
            "--gameDir", str(directory), "--quickPlayMultiplayer", f"127.0.0.1:{self.ports[0]}"]
        write_argument_file(directory/"client.args", arguments)
        owned.output = (directory/"console.log").open("xb")
        owned.transport = DeploymentTransport(port=port)
        owned.process = subprocess.Popen([str(JAVA), "@"+str(directory/"client.args")], cwd=directory, env=environment,
            stdin=subprocess.DEVNULL, stdout=owned.output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        owned.identity = ClientProcessIdentity(owned.process.pid, psutil.Process(owned.process.pid).create_time())
        write_json_atomic(directory/"identity.json", asdict(owned.identity))
        owned.backend = FabricBehaviorBackendV1(transport=owned.transport, client_identity=owned.identity, token=token,
                                                server_port=self.ports[0],observation_schema_version='mc2p.client_observation.v3')
        owned.runtime = PlayerRuntimeV1(owned.backend, self._create_trace(directory, owned.backend))

    def _client_environment(self, token: str, port: int) -> dict:
        return client_environment(dict(os.environ), token=token, server_port=self.ports[0], ipc_port=port, time_diagnostics=True)

    def _create_trace(self, directory: Path, backend):
        return DiagnosticTrace(directory, backend)

    def _export_time_evidence(self, directory: Path) -> dict:
        return export_follow_time_evidence(directory)

    def _connect_client(self, owned: OwnedClient) -> None:
        name, directory, port = owned.name, owned.directory, owned.port
        reset = owned.runtime.reset(ResetRequestV0("reset-"+name, f"follow-{self.seed}-{name}", "remote-session", 0,
            min(self.deadline_ns, time.perf_counter_ns()+90_000_000_000)))
        if not reset.succeeded: raise RuntimeError("follow client reset failed: " + str(reset.failure))
        proof = _connection_proof(self.server_identity, owned.identity, owned.backend.peer_identity_proof, self.ports[0])
        write_json_atomic(directory/"connection-proof.json", proof)
        if not validate_connection_evidence(proof, server_port=self.ports[0], ipc_port=port):
            raise RuntimeError("follow connection identity proof failed")
        print(f"FOLLOW_CLIENT_READY={name}", flush=True)

    def close(self) -> None:
        if self._closed: return
        self._closed = True
        for client in reversed(self.clients):
            try:
                if client.transport is not None:
                    cleanup, failures = _close_client(client.runtime, client.backend, client.transport, client.process, client.identity)
                    self.cleanup_failures.extend(dict(client=client.name, detail=v) for v in failures)
                    if cleanup is not None:
                        write_json_atomic(client.directory/"cleanup.json", cleanup)
                        if not cleanup["passed"] or not cleanup["graceful"]:
                            self.cleanup_failures.append(dict(client=client.name, cleanup=cleanup))
            except BaseException as error:
                self.cleanup_failures.append(dict(client=client.name, error=repr(error)))
            finally:
                try:
                    if client.output is not None: client.output.close()
                except BaseException as error:
                    self.cleanup_failures.append(dict(client=client.name,log_close_error=repr(error)))
        if self.server is not None:
            try:
                cleanup = _stop(self.server, self.server_identity, server=True)
                write_json_atomic(self.run_dir/"server-cleanup.json", cleanup)
                if not cleanup["passed"] or not cleanup["graceful"]:
                    self.cleanup_failures.append(dict(server=cleanup))
            except BaseException as error:
                self.cleanup_failures.append(dict(server_error=repr(error)))
        try:
            if self.server_log is not None: self.server_log.close()
        except BaseException as error:
            self.cleanup_failures.append(dict(server_log_close_error=repr(error)))
        self.checks.append(dict(name="owned_ports_released", passed=all(port_free(p) for p in self.ports)))
        # Closing one client must not leave another game alive while a long trace is validated.
        for client in reversed(self.clients):
            if client.runtime is not None:
                try:
                    timing = self._export_time_evidence(client.directory)
                    self.checks.append(dict(name=client.name+":time_attribution", passed=timing["status"] == "passed"))
                except BaseException as error:
                    self.cleanup_failures.append(dict(client=client.name,evidence_error=repr(error)))
        if self.run_dir.exists():
            write_json_atomic(self.run_dir/"host-cleanup.json", dict(cleanup_failures=self.cleanup_failures, checks=self.checks))

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
