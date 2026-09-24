"""Durable bounded local demo lifecycle. Status never equates a PID with visible readiness."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid

import psutil

from scripts.control_probe_core import write_json_atomic
from scripts.fabric_deployment_launch import ROOT, _hash
from scripts.probe_craftground_timing_parallel import run_bounded_process
from scripts.visual_client_launch import visible_environment
from scripts.visibility_fixture_world import _no_links
from scripts.block_observation_v3_sources import V3_BODY_SOURCES, V3_ENTRY_SOURCES
from scripts.block_observation_v3_evidence import NAVIGATION_CONTRACT, require_navigation_contract

SESSIONS = ROOT/"artifacts/visual-demo"
ID_PATTERN = r"\d{8}T\d{12}Z-[0-9a-f]{8}"
STARTUP_SECONDS = 150
CLEANUP_SECONDS = 45
LEGACY_CORE_SOURCES = ("mc2p/skills/follow.py","mc2p/skills/follow_driver.py","mc2p/skills/local_navigation.py",
                "mc2p/skills/local_perception.py","mc2p/skills/follow_types.py","mc2p/runtime/arbiter_v1.py",
                "mc2p/contracts/action_v1.py")
CORE_SOURCES = tuple(dict.fromkeys(LEGACY_CORE_SOURCES + V3_BODY_SOURCES + V3_ENTRY_SOURCES + (
    'scripts/follow_runtime_host.py','scripts/follow_evidence.py','scripts/follow_scenarios.py',
    'scripts/visual_demo.py','scripts/visual_demo_host.py','scripts/visual_demo_session.py',
    'scripts/fabric_deployment_launch.py','scripts/fabric_deployment_sandbox.py',
    'scripts/block_observation_v3_evidence.py',
    'scripts/formal_observation_v3_evidence.py','scripts/smoke_test_player_runtime.py',
    'scripts/client_time_evidence.py','scripts/streaming_time_evidence.py',
    'scripts/active_perception_world_change.py','scripts/active_perception_world_change_evidence.py',
)))


def create_session(skill: str, mode: str, seed: int, duration_seconds: int = 300) -> Path:
    if (skill != "follow" or mode not in {"manual","scripted"} or type(seed) is not int
            or seed not in (21001,21002,21003) or type(duration_seconds) is not int or not 60 <= duration_seconds <= 300):
        raise ValueError("supported demo: follow, manual/scripted, seeds 21001..21003, 60..300 seconds")
    SESSIONS.mkdir(parents=True,exist_ok=True)
    _no_links(SESSIONS)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid.uuid4().hex[:8]
    directory = SESSIONS/session_id
    directory.mkdir(exist_ok=False)
    now = time.perf_counter_ns()
    manifest = dict(schema_version="mc2p.visual-session.v2",session_id=session_id,skill=skill,mode=mode,seed=seed,
        duration_seconds=duration_seconds,created_at_unix_ns=time.time_ns(),created_at_monotonic_ns=now,
        startup_deadline_ns=now+STARTUP_SECONDS*1_000_000_000,
        hard_deadline_ns=now+(STARTUP_SECONDS+duration_seconds+CLEANUP_SECONDS)*1_000_000_000,
        core_sources={p:_hash(ROOT/p) for p in CORE_SOURCES},ports=[25598,8144,8145],
        identity_scope="new local offline test identities; no external server/account authentication",**dict(NAVIGATION_CONTRACT))
    with (directory/"manifest.json").open("x",encoding="utf-8") as stream: json.dump(manifest,stream,indent=2)
    return directory


def session_path(session_id: str) -> Path:
    if type(session_id) is not str or re.fullmatch(ID_PATTERN,session_id) is None:
        raise ValueError("invalid visual session id")
    path = SESSIONS/session_id
    _no_links(path)
    if not path.is_dir(): raise ValueError("visual session directory is missing")
    return path


def read_json(path: Path) -> dict:
    _no_links(path)
    if path.stat().st_size > 2_000_000: raise ValueError("oversized visual session record")
    value = json.loads(path.read_text("utf-8"))
    if type(value) is not dict: raise ValueError("invalid visual session record")
    return value


def read_manifest(directory: Path) -> dict:
    path = session_path(directory.name)
    if directory.absolute() != path.absolute(): raise ValueError("foreign visual session path")
    manifest = read_json(path/"manifest.json")
    current=manifest.get('schema_version')=='mc2p.visual-session.v2'
    expected_sources=CORE_SOURCES if current else LEGACY_CORE_SOURCES
    if (manifest.get("schema_version") not in {'mc2p.visual-session.v1','mc2p.visual-session.v2'} or manifest.get("session_id") != path.name
            or manifest.get("skill") != "follow" or manifest.get("mode") not in {"manual","scripted"}
            or manifest.get("seed") not in (21001,21002,21003)
            or type(manifest.get("duration_seconds")) is not int or not 60 <= manifest["duration_seconds"] <= 300
            or manifest.get("ports") != [25598,8144,8145]
            or type(manifest.get("core_sources")) is not dict or set(manifest["core_sources"]) != set(expected_sources)
            or any(type(v) is not str or re.fullmatch("[0-9a-f]{64}",v) is None for v in manifest["core_sources"].values())
            or type(manifest.get("created_at_monotonic_ns")) is not int or manifest["created_at_monotonic_ns"] < 0
            or manifest.get("startup_deadline_ns") != manifest["created_at_monotonic_ns"]+STARTUP_SECONDS*1_000_000_000
            or type(manifest.get("hard_deadline_ns")) is not int
            or manifest["hard_deadline_ns"] != manifest["created_at_monotonic_ns"]+
                (STARTUP_SECONDS+manifest["duration_seconds"]+CLEANUP_SECONDS)*1_000_000_000):
        raise ValueError("visual manifest differs from bounded session contract")
    if current: require_navigation_contract(manifest)
    elif any(key in manifest for key,_ in NAVIGATION_CONTRACT):
        raise ValueError('historical visual manifest cannot claim current knowledge')
    return manifest


def require_current_session(manifest):
    if manifest['schema_version']!='mc2p.visual-session.v2':
        raise ValueError('historical visual manifest is read-only')
    require_navigation_contract(manifest)
    if manifest['core_sources']!={name:_hash(ROOT/name) for name in CORE_SOURCES}:
        raise ValueError('visual implementation fingerprints changed')


def identity_alive(value: dict) -> bool:
    try:
        return (type(value.get("pid")) is int and type(value.get("create_time")) in (int,float)
                and psutil.Process(value["pid"]).create_time() == value["create_time"])
    except (psutil.Error,ValueError,TypeError): return False


def read_status(session_id: str) -> dict:
    directory = session_path(session_id)
    manifest = read_manifest(directory)
    stopped = (directory/"stop-request.json").exists()
    if (directory/"result.json").exists():
        result = read_json(directory/"result.json")
    else:
        result = read_json(directory/"worker-status.json") if (directory/"worker-status.json").exists() else dict(state="starting")
        result["cleanup_passed"] = None
        identity = read_json(directory/"supervisor.json") if (directory/"supervisor.json").exists() else None
        if (identity is not None and not identity_alive(identity)) or time.perf_counter_ns() >= manifest["hard_deadline_ns"]:
            result.update(state="failed",primary_failure="supervisor_absent_or_deadline_expired")
    if (directory/"startup-failure.json").exists():
        startup=read_json(directory/"startup-failure.json")
        result.update(state="failed",startup_failure=startup,
                      primary_failure=result.get("primary_failure") or startup["primary_failure"])
    return dict(result,session_id=session_id,mode=manifest["mode"],stop_requested=stopped,
                duration_seconds=manifest["duration_seconds"],directory=str(directory))


def request_stop(session_id: str) -> dict:
    directory = session_path(session_id)
    manifest=read_manifest(directory)
    if manifest['schema_version']!='mc2p.visual-session.v2':
        raise ValueError('historical session is read-only')
    # A current session must remain stoppable after source changes.
    # Only request cooperation; never signal a possibly reused PID from a status record.
    try:
        with (directory/"stop-request.json").open("x",encoding="utf-8") as stream:
            json.dump(dict(session_id=session_id,requested_at_unix_ns=time.time_ns()),stream)
    except FileExistsError:
        _no_links(directory/"stop-request.json")
    return read_status(session_id)


def supervise_command(directory: Path, command: list[str]) -> int:
    manifest = read_manifest(directory)
    require_current_session(manifest)
    with (directory/"supervisor.json").open("x",encoding="utf-8") as stream:
        json.dump(dict(pid=os.getpid(),create_time=psutil.Process().create_time()),stream)
    remaining = (manifest["hard_deadline_ns"]-time.perf_counter_ns())/1e9
    if remaining <= 0: raise TimeoutError("visual session already expired")
    process = run_bounded_process(command,cwd=ROOT,environment=visible_environment(dict(os.environ)),
        log_path=directory/"worker-console.log",timeout_seconds=remaining)
    write_json_atomic(directory/"supervisor-result.json",asdict(process))
    parent_ok = (process.return_code == 0 and process.primary_failure is None
                 and not process.cleanup_failures and process.process_stopped)
    try:
        worker = read_json(directory/"worker-result.json")
    except (OSError,ValueError):
        worker = dict(state="failed",cleanup_passed=False,primary_failure="missing_worker_result",cleanup_failures=[])
    startup=read_json(directory/"startup-failure.json") if (directory/"startup-failure.json").exists() else None
    ok = parent_ok and startup is None and worker.get("state") == "closed" and worker.get("cleanup_passed") is True and not worker.get("primary_failure") and not worker.get("cleanup_failures")
    result = dict(worker,state="closed" if ok else "failed",cleanup_passed=parent_ok and worker.get("cleanup_passed") is True,
        supervisor=asdict(process),completed_at_unix_ns=time.time_ns(),**dict(NAVIGATION_CONTRACT))
    if not parent_ok: result["parent_failure"] = process.primary_failure or "worker_exit_or_cleanup_failed"
    if startup is not None:
        result.update(startup_failure=startup,primary_failure=result.get("primary_failure") or startup["primary_failure"])
    write_json_atomic(directory/"result.json",result)
    return 0 if ok else 1
