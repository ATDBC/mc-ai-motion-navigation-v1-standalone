"""Opt-in local game delivery: start/status/stop. Never invoked by normal headless probes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import psutil

ROOT=Path(__file__).resolve().parents[1]
if __package__ in {None,""}: sys.path.insert(0,str(ROOT))

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.runtime.player_runtime_v1 import RuntimeStateV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.follow import RuleFollower
from mc2p.skills.follow_driver import FollowDriver
from mc2p.skills.follow_types import FollowRequest
from mc2p.skills.local_perception import project_follow_view
from scripts.control_probe_core import append_jsonl,write_json_atomic
from scripts.fabric_deployment_launch import _hash
from scripts.follow_fixture_world import build_follow_fixture,install_follow_fixture
from scripts.follow_evidence import evaluate_follow,evaluate_follow_runtime
from scripts.follow_scenarios import run_follow_scene,follow_task,_neutral
from scripts.probe_fabric_deployment_observation import port_free
from scripts.visual_client_launch import visible_environment,prepare_visible_launch
from scripts.visual_viewer_fixture import prepare_viewer_player
from scripts.visibility_fixture_world import _no_links
from scripts.visual_demo_host import VisualRuntimeHost,DemoStopped
from scripts.visual_demo_session import (create_session,read_manifest,read_status,request_stop,session_path,
    supervise_command,identity_alive,read_json)


def prepare_scene(directory: Path,seed: int=21001) -> dict:
    directory=Path(directory).absolute()
    if ".." in directory.parts: raise ValueError("prepared scene parent escape")
    _no_links(directory.parent)
    if directory.exists(): raise FileExistsError("prepared scene must be new")
    if type(seed) is not int or seed not in (21001,21002,21003): raise ValueError("undeclared scene seed")
    directory.mkdir(exist_ok=False)
    fixture=build_follow_fixture(directory/"fixture",seed=seed,scenario="moving")
    install_follow_fixture(directory/"fixture",directory/"world")
    viewer=prepare_viewer_player(directory/"world",directory/"viewer-initializer",seed=seed)
    launch=prepare_visible_launch(directory/"visible-player",server_port=25598,username="MC2PViewer")
    result=dict(state="prepared",seed=seed,directory=str(directory),fixture=fixture,viewer=viewer,
        client_sha1=launch["client_sha1"],visible_game_started=False,
        sequence=["approach","follow_move_and_turn","hold_distance","neutral_wait"],
        use="verified scene exemplar; start creates a fresh session from the same fixed recipe",
        live_render_and_input_verification="deferred until user asks to watch")
    write_json_atomic(directory/"prepared.json",result)
    return result


def _status(directory: Path,phase: str,**details):
    write_json_atomic(directory/"worker-status.json",dict(state="running",phase=phase,cleanup_passed=None,
        updated_at_unix_ns=time.time_ns(),**details))


def _idle(host,directory: Path,until_ns: int,phase: str):
    _status(directory,phase,window=host.window_proof["window"],deadline_ns=host.deadline_ns)
    task,profile=follow_task(host.deadline_ns),BehaviorProfileV0()
    while time.perf_counter_ns()<until_ns:
        host.check_stop()
        for client in host.clients:
            deadline=time.perf_counter_ns()+1_000_000_000
            if deadline>=host.deadline_ns:
                return  # Leave room for the separate close/release phase.
            _neutral(client.runtime,task,profile,deadline)
        time.sleep(.1)


def _scripted(host,directory: Path) -> list[dict]:
    _status(directory,"script_running",window=host.window_proof["window"],deadline_ns=host.deadline_ns)
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix="visual-scene") as executor:
        scene=executor.submit(run_follow_scene,host)
        interrupted=None
        try:
            while not scene.done():
                host.check_stop()
                if time.perf_counter_ns()>=host.deadline_ns: raise DemoStopped("session_deadline")
                time.sleep(.05)
            rows,phase=scene.result()
            checks=evaluate_follow(rows,phase,[])
            write_json_atomic(directory/"effect-checks.json",checks)
            if not checks or any(not c["passed"] for c in checks):
                raise RuntimeError("scripted visual follow checks failed: "+str(checks))
            return checks
        except BaseException as error:
            interrupted=error
            # Whole-session shutdown, not a per-skill pause: normal Runtime cancellation still owns dispatch.
            for client in host.clients:
                if client.runtime.state is RuntimeStateV1.READY:
                    try: client.runtime.cancel("visual_session_shutdown")
                    except BaseException as cancel_error: error.add_note(repr(cancel_error))
            raise
        finally:
            if interrupted is not None:
                try: scene.result(timeout=5)
                except BaseException as scene_error: interrupted.add_note("scene shutdown: "+repr(scene_error))


def _manual(host,directory: Path):
    task,profile=follow_task(host.deadline_ns),BehaviorProfileV0()
    binding_end=min(host.deadline_ns,time.perf_counter_ns()+10_000_000_000)
    while time.perf_counter_ns()<binding_end:
        host.check_stop()
        _neutral(host.follower,task,profile,binding_end)
        obs=host.follower.observation
        now=time.perf_counter_ns()
        view=project_follow_view(obs,now,obs.controller_clock_id)
        players=[p for p in view.entities if p.entity_type=="minecraft:player"]
        if view.available and len(players)==1: break
        time.sleep(.05)
    else: raise RuntimeError("manual target must be one currently legally visible player")
    request=FollowRequest("manual-"+directory.name,view.episode_id,players[0].track_id,now,
        min(now+120_000_000_000,host.deadline_ns),view.controller_clock_id)
    driver=FollowDriver(host.follower,RuleFollower(request,view))
    write_json_atomic(directory/"manual-request.json",trace_projection(request))
    _status(directory,"manual_follow",manual_input_verified=False,window=host.window_proof["window"])
    try:
        while time.perf_counter_ns()<request.deadline_ns-250_000_000:
            host.check_stop()
            step=driver.tick(task,profile,time.perf_counter_ns()+1_000_000_000)
            if step is not None:
                append_jsonl(directory/"manual-follow.jsonl",trace_projection(step.report))
                if step.report.terminal: return
            time.sleep(.05)
    finally:
        if host.follower.state is RuntimeStateV1.READY:
            driver.stop(task,profile,time.perf_counter_ns()+1_000_000_000,"manual_session_skill_end")


def run_worker(directory: Path) -> int:
    manifest=read_manifest(directory)
    from scripts.block_observation_v3_evidence import NAVIGATION_CONTRACT
    from scripts.visual_demo_session import require_current_session
    require_current_session(manifest)
    host=None
    failure=None
    stop_reason="session_deadline"
    effect_checks=[]
    try:
        if (directory/"stop-request.json").exists(): raise DemoStopped("user_stop")
        if manifest["core_sources"]!={p:_hash(ROOT/p) for p in manifest["core_sources"]}:
            raise RuntimeError("follow implementation changed since session creation")
        if any(not port_free(p) for p in manifest["ports"]): raise RuntimeError("visual session port occupied")
        if port_free(7897): raise RuntimeError("Mihomo 7897 unavailable; no direct-network fallback")
        host=VisualRuntimeHost(directory/"session",mode=manifest["mode"],stop_path=directory/"stop-request.json",
            seed=manifest["seed"],server_port=manifest["ports"][0],follower_port=manifest["ports"][1],
            leader_port=manifest["ports"][2],deadline_ns=manifest["startup_deadline_ns"])
        with host:
            ready=time.perf_counter_ns()
            if ready>=manifest["startup_deadline_ns"]: raise TimeoutError("visual startup deadline expired")
            host.deadline_ns=min(ready+manifest["duration_seconds"]*1_000_000_000,manifest["hard_deadline_ns"]-30_000_000_000)
            write_json_atomic(directory/"ready.json",dict(ready_at_ns=ready,deadline_ns=host.deadline_ns,
                deadline_at_unix_ns=time.time_ns()+host.deadline_ns-ready,mode=manifest["mode"],window=host.window_proof,
                display_status="window_joined_pending_human_render_check",manual_input_verified=False))
            _idle(host,directory,min(host.deadline_ns,ready+15_000_000_000),"ready_waiting")
            if manifest["mode"]=="scripted": effect_checks=_scripted(host,directory)
            else: _manual(host,directory)
            _idle(host,directory,host.deadline_ns,"skill_finished_neutral_wait")
    except DemoStopped as error:
        stop_reason=str(error)
    except BaseException as error:
        failure=dict(type=type(error).__name__,message=str(error))
        (directory/"failure.txt").write_text(traceback.format_exc(),encoding="utf-8")
    finally:
        if host is not None: host.close()
    checks=list(host.checks) if host else []
    if host is not None:
        for client in host.clients:
            try:
                records=[json.loads(line) for line in (client.directory/"trace.jsonl").read_text("utf-8").splitlines()]
                diagnostics=[json.loads(line) for line in (client.directory/"diagnostics.jsonl").read_text("utf-8").splitlines()]
                checks.extend(dict(c,name=client.name+":"+c["name"]) for c in evaluate_follow_runtime(records,diagnostics,server_port=host.ports[0]))
            except (OSError,ValueError) as error:
                checks.append(dict(name=client.name+":evidence_present",passed=False,detail=str(error)))
    cleanup_failures=host.cleanup_failures if host else []
    ports_free=all(port_free(p) for p in manifest["ports"])
    cleanup_ok=not cleanup_failures and ports_free
    if failure is None and any(not c["passed"] for c in checks): failure=dict(type="VisualEvidenceFailure",message=str(checks))
    result=dict(state="closed" if failure is None and cleanup_ok else "failed",stop_reason=stop_reason,
        primary_failure=failure,cleanup_passed=cleanup_ok,cleanup_failures=cleanup_failures,checks=checks,
        effect_checks=effect_checks,ports_free=ports_free,manual_input_verified=False,
        renderer_verification="human_confirmation_required; no automatic screenshot",**dict(NAVIGATION_CONTRACT))
    write_json_atomic(directory/"worker-result.json",result)
    return 0 if result["state"]=="closed" else 1


def start_session(skill,mode,seed,duration_seconds) -> dict:
    directory=create_session(skill,mode,seed,duration_seconds)
    manifest=read_manifest(directory)
    output=(directory/"launcher-console.log").open("xb")
    try:
        process=subprocess.Popen(["D:/Miniforge3/Scripts/conda.exe","run","--prefix",str(ROOT/".venv"),
            "--no-capture-output","python",str(Path(__file__).resolve()),"_supervise","--session-id",directory.name],
            cwd=ROOT,env=visible_environment(dict(os.environ)),stdin=subprocess.DEVNULL,stdout=output,
            stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as error:
        write_json_atomic(directory/"result.json",dict(state="failed",cleanup_passed=True,
            primary_failure=dict(type=type(error).__name__,message=str(error)),cleanup_failures=[]))
        return read_status(directory.name)
    finally:
        output.close()
    try:
        write_json_atomic(directory/"launcher.json",dict(pid=process.pid,create_time=psutil.Process(process.pid).create_time()))
    except BaseException:
        # Its independent hard-bounded supervisor still owns all children; request cooperative shutdown.
        request_stop(directory.name)
        raise
    # The independent supervisor writes its own exact identity and owns worker+JVM cleanup.
    while time.perf_counter_ns()<manifest["startup_deadline_ns"]:
        if (directory/"result.json").exists(): return read_status(directory.name)
        if (directory/"ready.json").exists():
            result=read_status(directory.name)
            if result["state"]!="failed":
                return dict(result,state="running",readiness=read_json(directory/"ready.json"))
        if process.poll() is not None:
            write_json_atomic(directory/"startup-failure.json",dict(primary_failure="supervisor_launcher_exited_before_ready"))
            request_stop(directory.name)
            return read_status(directory.name)
        time.sleep(.1)
    write_json_atomic(directory/"startup-failure.json",dict(primary_failure="startup_deadline; stop requested"))
    request_stop(directory.name)
    return read_status(directory.name)


def main(argv=None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    if actual_argv and actual_argv[0] == 'playground':
        from scripts.follow_playground_session import main as playground_main
        return playground_main(actual_argv[1:])
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser('playground',help='persistent flat playground: prepare/start/status/stop')
    start=commands.add_parser("start")
    start.add_argument("--skill",choices=("follow",),default="follow")
    start.add_argument("--mode",choices=("manual","scripted"),default="scripted")
    start.add_argument("--seed",type=int,choices=(21001,21002,21003),default=21001)
    start.add_argument("--duration-seconds",type=int,default=300)
    prepare=commands.add_parser("prepare",help="create a verified scene exemplar without opening a game")
    prepare.add_argument("--output",type=Path,required=True)
    prepare.add_argument("--seed",type=int,choices=(21001,21002,21003),default=21001)
    for name in ("status","stop","_supervise","_worker"):
        sub=commands.add_parser(name)
        sub.add_argument("--session-id",required=True)
    args=parser.parse_args(argv)
    if args.command=="prepare":
        print(json.dumps(prepare_scene(args.output,args.seed),ensure_ascii=False,indent=2),flush=True)
        return 0
    if args.command=="_worker": return run_worker(session_path(args.session_id))
    if args.command=="_supervise":
        # pythonw avoids creating a helper console beneath the hidden supervisor.
        return supervise_command(session_path(args.session_id),[str(ROOT/".venv/pythonw.exe"),
            str(Path(__file__).resolve()),"_worker","--session-id",args.session_id])
    if args.command=="start": result=start_session(args.skill,args.mode,args.seed,args.duration_seconds)
    elif args.command=="stop": result=request_stop(args.session_id)
    else: result=read_status(args.session_id)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return 1 if result["state"]=="failed" else 0


if __name__=="__main__": raise SystemExit(main())
