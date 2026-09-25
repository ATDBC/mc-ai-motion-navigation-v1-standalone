"""Independent human renderer alongside unchanged headless Runtime clients."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import time

import psutil

from mc2p.backends.deployment_transport import ClientProcessIdentity
from mc2p.contracts.behavior import BehaviorProfileV0
from scripts.control_probe_core import write_json_atomic
from scripts.fabric_deployment_launch import ROOT,_hash,inspect_launch,verify_assets
from scripts.fabric_deployment_sandbox import JAVA,prepare_server
from scripts.follow_fixture_world import build_follow_fixture,install_follow_fixture
from scripts.follow_runtime_host import FollowRuntimeHost
from scripts.follow_scenarios import _neutral,follow_task
from scripts.probe_fabric_deployment_observation import PROXY_ARGS,_live,_stop,_connection_proof
from scripts.visual_client_launch import prepare_visible_launch,visible_environment
from scripts.visual_viewer_fixture import prepare_viewer_player
from scripts.visual_windows import owned_windows,close_owned_windows


class DemoStopped(Exception):
    pass


def prepare_visual_server(target: Path,*,seed: int,port: int,mode: str) -> dict:
    if mode not in {"manual","scripted"}: raise ValueError("undeclared visual mode")
    result=prepare_server(target,seed=seed,port=port)
    if mode=="scripted":
        path=target/"server.properties"
        text=path.read_text("utf-8")
        if text.count("max-players=2\n")!=1: raise ValueError("unexpected fresh server player limit")
        path.write_text(text.replace("max-players=2\n","max-players=3\n"),encoding="utf-8")
        result["properties_sha256"]=_hash(path)
    result["visual_mode"]=mode
    return result


class VisualRuntimeHost(FollowRuntimeHost):
    def __init__(self,run_dir: Path,*,mode: str,stop_path: Path,**kwargs):
        if mode not in {"manual","scripted"}: raise ValueError("undeclared visual mode")
        super().__init__(run_dir,scenario="moving",**kwargs)
        self.mode,self.stop_path=mode,stop_path
        self.visible_process=self.visible_identity=self.visible_output=None
        self.visible_directory=self.run_dir/"visible-player"
        self.window_proof=None

    def check_stop(self):
        if self.stop_path.exists(): raise DemoStopped("user_stop")
        if self.visible_process is not None and self.visible_process.poll() is not None:
            raise DemoStopped("viewer_closed")

    def start(self):
        # Separate orchestration only: no modifications to the accepted follow host/actor/fixture recipe.
        if self._closed or self.run_dir.exists(): raise RuntimeError("visual host cannot restart")
        self.check_stop()
        launch=json.loads(self.launch_path.read_text("utf-8"))
        self.provenance=dict(launch=inspect_launch(launch),assets=verify_assets())
        if psutil.virtual_memory().available < (10 if self.mode=="scripted" else 7)*1024**3:
            raise RuntimeError("insufficient memory headroom for the explicitly selected visual roles")
        self.run_dir.mkdir(exist_ok=False)
        self.provenance["fixture"]=build_follow_fixture(self.run_dir/"fixture",seed=self.seed,scenario="moving")
        self.provenance["server"]=prepare_visual_server(self.run_dir/"server",seed=self.seed,port=self.ports[0],mode=self.mode)
        install_follow_fixture(self.run_dir/"fixture",self.run_dir/"server/world")
        if self.mode=="scripted":
            self.provenance["viewer"]=prepare_viewer_player(self.run_dir/"server/world",self.run_dir/"viewer-initializer",seed=self.seed)
        visible=prepare_visible_launch(self.visible_directory,server_port=self.ports[0],
            username="MC2PViewer" if self.mode=="scripted" else "MC2PLeader")
        write_json_atomic(self.run_dir/"provenance.json",self.provenance)
        self.server_log=(self.run_dir/"server-console.log").open("xb")
        self.check_stop()
        self.server=subprocess.Popen([str(JAVA),"-Xms256M","-Xmx1G",*PROXY_ARGS,"-jar",
            str(self.run_dir/"server/server.jar"),"nogui"],cwd=self.run_dir/"server",env=visible_environment(dict(os.environ)),
            stdin=subprocess.PIPE,stdout=self.server_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        self.server_identity=ClientProcessIdentity(self.server.pid,psutil.Process(self.server.pid).create_time())
        write_json_atomic(self.run_dir/"server-identity.json",asdict(self.server_identity))
        ready_deadline=min(self.deadline_ns,time.perf_counter_ns()+60_000_000_000)
        while "Done (" not in (self.run_dir/"server-console.log").read_text("utf-8",errors="replace"):
            self.check_stop(); _live(self.server_identity)
            if time.perf_counter_ns()>=ready_deadline: raise TimeoutError("visual server readiness deadline")
            time.sleep(.05)
        self.visible_output=(self.visible_directory/"console.log").open("xb")
        self.visible_process=subprocess.Popen([str(JAVA),"@"+str(self.visible_directory/"client.args")],
            cwd=self.visible_directory,env=visible["environment"],stdin=subprocess.DEVNULL,stdout=self.visible_output,
            stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        self.visible_identity=ClientProcessIdentity(self.visible_process.pid,psutil.Process(self.visible_process.pid).create_time())
        write_json_atomic(self.visible_directory/"identity.json",asdict(self.visible_identity))
        self._launch_client("MC2PFollower",self.ports[1],launch)
        if self.mode=="scripted": self._launch_client("MC2PLeader",self.ports[2],launch)
        for client in self.clients:
            self.check_stop(); self._connect_client(client)
        # While the independent viewer completes startup, keep already-ready robot leases neutral.
        ready_deadline=min(self.deadline_ns,time.perf_counter_ns()+45_000_000_000)
        while True:
            self.check_stop()
            windows=owned_windows(self.visible_identity)
            joined=visible["username"]+" joined the game" in (self.run_dir/"server-console.log").read_text("utf-8",errors="replace")
            if len(windows)==1 and joined:
                proof=_connection_proof(self.server_identity,self.visible_identity,None,self.ports[0])
                self.window_proof=dict(window=windows[0],connection=proof,joined=True,
                    renderer_verification="awaiting_human_visual_check",manual_input_verified=False)
                write_json_atomic(self.run_dir/"visible-ready.json",self.window_proof)
                break
            if time.perf_counter_ns()>=ready_deadline: raise TimeoutError("visible client window/join readiness deadline")
            for client in self.clients:
                _neutral(client.runtime,follow_task(ready_deadline),BehaviorProfileV0(),ready_deadline)
            time.sleep(.05)
        identities=[self.server_identity,self.visible_identity,*[c.identity for c in self.clients]]
        self.checks.append(dict(name="distinct_visual_role_processes",passed=len({i.pid for i in identities})==len(identities)))
        write_json_atomic(self.run_dir/"ready.json",dict(mode=self.mode,identities=[asdict(i) for i in identities],
            window=self.window_proof["window"],actor_roles=[c.name for c in self.clients]))

    def close(self):
        if self._closed: return
        try:
            if self.visible_process is not None:
                if self.visible_process.poll() is None: close_owned_windows(self.visible_identity)
                cleanup=_stop(self.visible_process,self.visible_identity)
                if self.run_dir.exists(): write_json_atomic(self.run_dir/"visible-cleanup.json",cleanup)
                if not cleanup["passed"] or not cleanup["graceful"]: self.cleanup_failures.append(dict(viewer=cleanup))
        except BaseException as error:
            self.cleanup_failures.append(dict(viewer_error=repr(error)))
        finally:
            if self.visible_output is not None: self.visible_output.close()
            super().close()
