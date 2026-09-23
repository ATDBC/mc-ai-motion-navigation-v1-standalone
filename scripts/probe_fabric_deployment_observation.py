"""Supervised local vanilla server + independent Fabric client Runtime/reconnect probe."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

import psutil
from mc2p.backends.deployment_transport import ClientProcessIdentity, DeploymentTransport
from mc2p.backends.fabric_behavior import FabricBehaviorBackendV1
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1, LookV1, OpenInventoryV1, CloseScreenV1, ClickSlotV1, SelectHotbarV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import OBSERVATION_V3, ObservationRequestV3
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import TaskIntentV0, SuccessCriterionV0, ComparisonOperatorV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import JsonlTraceWriterV0, trace_projection
from mc2p.skills.perception_needs import PerceptionConfig
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.client_time_evidence import export_runtime_time_evidence
from scripts.fabric_deployment_launch import export_launch, inspect_launch, verify_assets, _hash
from scripts.fabric_deployment_sandbox import JAVA, SERVER_SHA1, prepare_server, write_argument_file, client_environment
from scripts.fabric_container_scenario import run_container_scenario, evaluate_container, evaluate_container_reconnect
from scripts.probe_craftground_timing_parallel import run_bounded_process, make_run_id
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3
from scripts.block_parity_evidence import parity_file_check,formal_samples_from_records,frozen_probe_sources
from scripts.smoke_test_player_runtime import _formal_observation_violations
from scripts.smoke_test_player_runtime_v1 import STAGES as RUNTIME_STAGES, evaluate_stages
from scripts.timing_parallel_probe_core import ProcessIdentityV0, terminate_registered_tree
from scripts.visibility_fixture_world import _no_links, build_visibility_fixture, install_visibility_fixture

PROXY_ARGS = ["-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=7897", "-Dhttps.proxyHost=127.0.0.1",
              "-Dhttps.proxyPort=7897", "-Dhttp.nonProxyHosts=localhost|127.*|[::1]|repo.huaweicloud.com"]
B03_SOURCES = (
    "mc2p/motion_nav/fixed_route.py",
    "mc2p/motion_nav/geometry.py",
    "mc2p/motion_nav/ground_motion.py",
    "mc2p/motion_nav/runtime_adapter.py",
    "scripts/fixed_route_runtime_core.py",
    "config/motion-navigation/ordinary-ground-v1.json",
)
B03_SHAPE_SOURCES = (
    "mc2p/motion_nav/shape_trials.py",
    "scripts/fixed_route_shape_runtime.py",
)
B04_SOURCES = (
    "mc2p/motion_nav/known_map_planner.py",
    "mc2p/motion_nav/planner_worker.py",
    "mc2p/motion_nav/route_admission.py",
    "scripts/known_map_navigation_runtime.py",
)
B05_CALIBRATION_SOURCES = (
    "scripts/jump_up_calibration_runtime.py",
    "mc2p/motion_nav/jump_up.py",
    "config/motion-navigation/jump-up-v1.json",
)
B05_ROUTE_SOURCES = (
    "mc2p/motion_nav/action_route.py",
    "mc2p/motion_nav/action_route_executor.py",
    "scripts/jump_up_navigation_runtime.py",
    "scripts/jump_up_acceptance_runtime.py",
)
B06_MATERIAL_SOURCES = (
    "scripts/b06_ordinary_material_runtime.py",
    "mc2p/motion_nav/block_motion_traits.py",
    "mc2p/motion_nav/environment_identity.py",
    "mc2p/motion_nav/movement_transition.py",
    "config/motion-navigation/environment-v1.json",
    "config/motion-navigation/block-motion-traits-v1.json",
    "config/motion-navigation/vanilla-block-registry-1_21.json",
    "config/motion-navigation/ordinary-ground-b06-v1.json",
    "config/motion-navigation/jump-up-b06-v1.json",
)
B07_STEP_SOURCES = (
    "scripts/step_transition_runtime.py",
    "mc2p/motion_nav/support_surfaces.py",
    "mc2p/motion_nav/step_transition.py",
    "config/motion-navigation/step-b07-v1.json",
)
B08_GROUND_MODE_SOURCES = (
    "scripts/ground_modes_runtime.py",
    "mc2p/motion_nav/ground_modes.py",
    "config/motion-navigation/ground-modes-b08-v1.json",
)
B09_AIR_MOTION_SOURCES = (
    "scripts/air_motion_runtime.py",
    "mc2p/motion_nav/air_motion.py",
    "mc2p/motion_nav/jump_gap.py",
    "mc2p/motion_nav/controlled_drop.py",
    "config/motion-navigation/air-motions-b09-v1.json",
)
B10_GAP_SOLVER_SOURCES = (
    "scripts/b10_gap_solver_runtime.py",
    "mc2p/motion_nav/motion_candidate.py",
    "mc2p/motion_nav/motion_solver.py",
    "mc2p/motion_nav/online_motion.py",
    "mc2p/motion_nav/physics_adapter.py",
    "mc2p/motion_nav/physics_1_21.py",
)


def frozen_deployment_sources(*, b03_fixed_route_probe: bool,
                              b03_shape_probe: bool = False,
                              b04_known_map_probe: bool = False,
                              b05_jump_calibration_probe: bool = False,
                              b05_jump_route_probe: bool = False,
                              b05_jump_acceptance_probe: bool = False,
                              b06_ordinary_material_probe: bool = False,
                              b07_step_probe: bool = False,
                              b08_ground_modes_probe: bool = False,
                              b09_air_motion_probe: bool = False,
                              b10_gap_solver_probe: bool = False) -> dict[str, str]:
    sources = frozen_probe_sources()
    if b03_fixed_route_probe or b03_shape_probe:
        sources.update({name: _hash(ROOT / name) for name in B03_SOURCES})
    if b03_shape_probe:
        sources.update({name: _hash(ROOT / name) for name in B03_SHAPE_SOURCES})
    if b04_known_map_probe:
        sources.update({name: _hash(ROOT / name) for name in (*B03_SOURCES, *B04_SOURCES)})
    if b05_jump_calibration_probe:
        sources.update({name: _hash(ROOT / name) for name in (*B03_SOURCES, *B05_CALIBRATION_SOURCES)})
    if b05_jump_route_probe or b05_jump_acceptance_probe:
        sources.update({name: _hash(ROOT / name) for name in (
            *B03_SOURCES, *B04_SOURCES, *B05_CALIBRATION_SOURCES, *B05_ROUTE_SOURCES,
        )})
    if b06_ordinary_material_probe:
        sources.update({name: _hash(ROOT / name) for name in (
            *B03_SOURCES, *B05_CALIBRATION_SOURCES, *B05_ROUTE_SOURCES,
            *B06_MATERIAL_SOURCES,
        )})
    if b07_step_probe:
        sources.update({name: _hash(ROOT / name) for name in (
            *B03_SOURCES, *B06_MATERIAL_SOURCES, *B07_STEP_SOURCES,
        )})
    if b08_ground_modes_probe:
        sources.update({name: _hash(ROOT / name) for name in (
            *B03_SOURCES, *B06_MATERIAL_SOURCES, *B08_GROUND_MODE_SOURCES,
        )})
    if b09_air_motion_probe:
        sources.update({name: _hash(ROOT / name) for name in (
            *B03_SOURCES, *B04_SOURCES, *B05_CALIBRATION_SOURCES,
            *B05_ROUTE_SOURCES, *B06_MATERIAL_SOURCES, *B07_STEP_SOURCES,
            *B09_AIR_MOTION_SOURCES,
        )})
    if b10_gap_solver_probe:
        sources.update({name: _hash(ROOT / name) for name in (
            *B03_SOURCES, *B09_AIR_MOTION_SOURCES, *B10_GAP_SOLVER_SOURCES,
        )})
    return sources


def create_deployment_backend(transport, identity, token: str, server_port: int):
    """Freeze the formal standalone probe on V3 at its construction boundary."""
    return FabricBehaviorBackendV1(transport=transport, client_identity=identity, token=token,
                                   server_port=server_port, observation_schema_version=OBSERVATION_V3)


def port_free(port: int) -> bool:
    with socket.socket() as handle:
        try:
            handle.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def validate_connection_evidence(value: dict, *, server_port: int, ipc_port: int) -> bool:
    try:
        server, client, peer = value["server_identity"], value["client_identity"], value["peer_proof"]
        for identity in (server, client):
            ClientProcessIdentity(**identity)
        outgoing, incoming = value["client_game_connection"], value["server_game_connection"]
        return (server["pid"] != client["pid"] and client["pid"] == peer["pid"]
            and client["create_time"] == peer["create_time"]
            and list(peer["controller_address"]) == ["127.0.0.1", ipc_port]
            and peer["client_address"][0] == "127.0.0.1"
            and list(value["server_listener"]) == list(outgoing["remote"]) == ["127.0.0.1", server_port]
            and outgoing["local"][0] == "127.0.0.1"
            and incoming["local"] == outgoing["remote"] and incoming["remote"] == outgoing["local"])
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def evaluate_trace(records: list[dict], rows: list[dict], *, server_port: int,
                   expected_steps: int = 28, require_gui_attempts: bool = True) -> list[dict]:
    if type(expected_steps) is not int or not 1 <= expected_steps <= 8000:
        raise ValueError("expected Runtime step count must be within 1..8000")
    try:
        reset = [r["payload"]["result"] for r in records if r["record_type"] == "reset"]
        steps = [r["payload"] for r in records if r["record_type"] == "step"]
        obs = [r["observation"] for r in reset] + [r["backend_result"]["observation"] for r in steps]
        receipts = [behavior_receipt_from_mapping(r["backend_result"]["receipt"]) for r in steps]
        dispatch = [r["payload"]["decision"] for r in records if r["record_type"] == "dispatch"]
        order = [r["record_type"] for r in records if r["record_type"] in {"reset", "dispatch", "step"}]
        checks = dict(
            one_reset_28_runtime_steps=len(reset) == 1 and len(steps) == len(dispatch) == expected_steps
                and dispatch == [r["decision"] for r in steps] and order == ["reset"] + ["dispatch", "step"] * expected_steps,
            exact_continuous_v3=len(obs) == expected_steps + 1 and not validate_formal_observations_v3(obs)
                and [o["sequence_id"] for o in obs] == list(range(expected_steps + 1)) and len({o["episode_id"] for o in obs}) == 1
                and all(o["source_backend"] == "fabric" and o["is_dead"]["value"] is False for o in obs),
            separate_monotonic_client_samples=len({o["client_sample"]["clock_id"] for o in obs}) == 1
                and all(a["client_sample"]["completed_at_monotonic_ns"] <= b["client_sample"]["started_at_monotonic_ns"] for a, b in zip(obs, obs[1:])),
            exact_action_receipt_association=len(receipts) == expected_steps and all(
                p["decision"]["action"]["schema_version"] == "mc2p.action-snapshot.v1"
                and p["decision"]["action"]["request_sequence_id"] == i == receipts[i].request_sequence_id
                and p["decision"]["action"]["observation_sequence_id"] == i
                and p["decision"]["action"]["episode_id"] == receipts[i].episode_id == obs[i + 1]["episode_id"]
                and obs[i + 1]["request_sequence_id"] == i and receipts[i].generation_id == i + 1
                and receipts[i].world_tick == obs[i + 1]["world_time_ticks"]["value"]
                and receipts[i].on_client_thread and receipts[i].status != "idle"
                and receipts[i].action_keyboard_callbacks == receipts[i].action_mouse_callbacks == receipts[i].handled_screen_render_completions == 0
                for i, p in enumerate(steps)),
            normal_input_ticks_continue=bool(receipts) and receipts[0].input_samples >= 1
                and all(a.input_samples < b.input_samples for a, b in zip(receipts, receipts[1:])),
            no_image_fields_or_binary_projection=not _formal_observation_violations(records, path="trace"),
            continuous_zero_image_diagnostics=len(rows) == len(obs) == expected_steps + 1 and all(
                row["episode_id"] == obs[i]["episode_id"] and row["observation_sequence_id"] == i
                and row["diagnostics"]["schema_version"] == "mc2p.deployment_diagnostics.v1"
                and row["diagnostics"]["remote_address"] == f"127.0.0.1:{server_port}"
                and row["diagnostics"]["has_integrated_server"] is False
                and row["diagnostics"]["window_recorded"] is True
                and row["diagnostics"]["window_visible"] is row["diagnostics"]["window_visible_at_creation"] is False
                and all(type(row["diagnostics"][key]) is int and row["diagnostics"][key] >= 0
                    for key in ("client_tick", "world_render_attempts", "gui_render_attempts"))
                and all(type(row["diagnostics"][key]) is int and row["diagnostics"][key] == 0 for key in
                    ("world_render_completions", "gui_render_completions", "framebuffer_capture_attempts", "image_encode_attempts"))
                for i, row in enumerate(rows))
                and all(a["diagnostics"]["client_tick"] < b["diagnostics"]["client_tick"]
                    and a["diagnostics"]["world_render_attempts"] < b["diagnostics"]["world_render_attempts"]
                    and a["diagnostics"]["gui_render_attempts"] <= b["diagnostics"]["gui_render_attempts"]
                    for a, b in zip(rows, rows[1:]))
                and (not require_gui_attempts
                     or rows[-1]["diagnostics"]["gui_render_attempts"]
                        > rows[0]["diagnostics"]["gui_render_attempts"]),
        )
        if expected_steps != 28:
            checks[f"one_reset_{expected_steps}_runtime_steps"] = checks.pop("one_reset_28_runtime_steps")
        return [{"name": name, "passed": bool(value)} for name, value in checks.items()]
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return [{"name": "well_formed_deployment_trace", "passed": False, "detail": str(error)}]


def finalize_result(worker: dict, supervision, ports_free: bool) -> dict:
    result = dict(worker)
    okay = (worker.get("status") == "passed" and worker.get("primary_failure") is None
        and worker.get("cleanup_failures") == [] and bool(worker.get("checks"))
        and all(c.get("passed") is True for c in worker["checks"])
        and supervision.return_code == 0 and supervision.primary_failure is None
        and not supervision.cleanup_failures and supervision.process_stopped and ports_free)
    result.update(status="passed" if okay else "failed", parent_supervision=trace_projection(supervision), ports_free=ports_free)
    if not okay:
        result["primary_failure"] = worker.get("primary_failure") or "parent_or_worker_evidence_failed"
    result["cleanup_failures"] = list(worker.get("cleanup_failures", [])) + list(supervision.cleanup_failures)
    return result


def _live(identity: ClientProcessIdentity) -> psutil.Process:
    process = psutil.Process(identity.pid)
    if process.create_time() != identity.create_time or not process.is_running():
        raise RuntimeError("registered JVM identity changed")
    return process


def _connection_proof(server, client, peer, server_port):
    outgoing = [item for item in _live(client).net_connections(kind="tcp4")
                if tuple(item.raddr) == ("127.0.0.1", server_port) and item.status == psutil.CONN_ESTABLISHED]
    server_connections = _live(server).net_connections(kind="tcp4")
    listeners = [item for item in server_connections if tuple(item.laddr) == ("127.0.0.1", server_port) and item.status == psutil.CONN_LISTEN]
    if len(outgoing) != 1 or len(listeners) != 1:
        raise RuntimeError("independent server/client OS connection evidence is missing")
    connection = outgoing[0]
    incoming = [item for item in server_connections if tuple(item.laddr) == tuple(connection.raddr)
                and tuple(item.raddr) == tuple(connection.laddr) and item.status == psutil.CONN_ESTABLISHED]
    if len(incoming) != 1:
        raise RuntimeError("server has no matching client connection")
    return dict(server_identity=asdict(server), client_identity=asdict(client), peer_proof=peer,
        server_listener=list(listeners[0].laddr),
        client_game_connection=dict(local=list(connection.laddr), remote=list(connection.raddr)),
        server_game_connection=dict(local=list(incoming[0].laddr), remote=list(incoming[0].raddr)))


def _stop(process, identity, *, server: bool = False) -> dict:
    graceful = False
    errors = []
    cleanup = None
    if identity is None:
        errors.append(dict(stage="identity_registration", type="MissingProcessIdentity"))
        try:
            # Windows Popen owns a native process HANDLE, not a looked-up PID.
            # This closes the narrow Popen -> psutil registration failure window.
            if os.name != "nt":
                raise RuntimeError("owned-handle cleanup requires Windows")
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        except BaseException as error:
            errors.append(dict(stage="owned_handle_termination", type=type(error).__name__))
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BaseException as error:
                    errors.append(dict(stage="stdin_close", type=type(error).__name__))
        return dict(graceful=False, return_code=process.poll(), cleanup=None, errors=errors, passed=False)
    try:
        if process.poll() is None:
            _live(identity)
            if server:
                process.stdin.write(b"stop\n")
                process.stdin.flush()
            try:
                process.wait(timeout=10)
                graceful = True
            except subprocess.TimeoutExpired:
                pass
        else:
            graceful = True
    except BaseException as error:
        errors.append(dict(stage="graceful_stop", type=type(error).__name__))
    registered = ProcessIdentityV0(identity.pid, identity.create_time)
    try:
        cleanup = terminate_registered_tree(registered, (registered,), grace_seconds=2)
    except BaseException as error:
        errors.append(dict(stage="registered_termination", type=type(error).__name__))
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except BaseException as error:
                errors.append(dict(stage="stdin_close", type=type(error).__name__))
    return dict(graceful=graceful, return_code=process.poll(), cleanup=trace_projection(cleanup),
                errors=errors, passed=not errors and cleanup is not None
                and not cleanup.surviving and not cleanup.errors and process.poll() == 0)


def _close_client(runtime, backend, transport, process, identity) -> tuple[dict | None, list]:
    failures, cleanup = [], None
    if runtime is not None:
        try:
            runtime.close()
        except BaseException as error:
            failures.append(dict(stage="runtime_close", type=type(error).__name__))
        try:
            failures.extend(trace_projection(runtime.cleanup_failures))
        except BaseException as error:
            failures.append(dict(stage="runtime_cleanup_report", type=type(error).__name__))
    if backend is not None:
        try:
            backend.close()
        except BaseException as error:
            failures.append(dict(stage="backend_close", type=type(error).__name__))
    try:
        transport.close()
    except BaseException as error:
        failures.append(dict(stage="transport_close", type=type(error).__name__))
    if process is not None:
        try:
            cleanup = _stop(process, identity)
        except BaseException as error:
            failures.append(dict(stage="client_stop", type=type(error).__name__))
    return cleanup, failures


def _runtime_trace(directory: Path, backend, *, capture_close_diagnostics: bool):
    if capture_close_diagnostics:
        from scripts.fabric_visibility_scenario import CloseDiagnosticsTrace
        return CloseDiagnosticsTrace(directory, backend)
    return JsonlTraceWriterV0(directory / "trace.jsonl")


def _scenario(runtime, backend, episode, directory, deadline, previous_gui=None, *, b02_air_probe=False):
    task = TaskIntentV0("deployment-probe", "control-gui-probe", "{}",
        (SuccessCriterionV0("horizontal_displacement", ComparisonOperatorV0.GREATER_THAN, .1, "blocks"),),
        200, deadline, True, 0.0)
    profile, stages, rows = BehaviorProfileV0(), {}, []
    last, counter = None, 0
    def diagnostic():
        obs = runtime.observation
        row = dict(episode_id=episode, observation_sequence_id=obs.sequence_id, diagnostics=backend.last_diagnostics)
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)
    def submit(source, **kwargs):
        nonlocal counter
        counter += 1
        runtime.submit_intent(ActionIntentV1(f"intent-{counter}", source, episode, runtime.observation.sequence_id,
            ActionPriorityV0.TASK, time.perf_counter_ns(), deadline, **kwargs))
    def step(operation=None, expected="running", observation_request=None):
        nonlocal last
        if operation is not None:
            submit("gui", operation=operation)
        last = runtime.step(task, profile, min(deadline, time.perf_counter_ns() + 5_000_000_000),
                            observation_request=observation_request)
        if last.observation is None or last.report.status.value != expected:
            raise RuntimeError(f"unexpected independent Runtime result: {last.report}")
        diagnostic()
    def mark(label):
        obs = runtime.observation
        p, velocity, gui = obs.position.value, obs.self_state.value.velocity, obs.gui.value
        receipt = None if last is None else last.backend_result.receipt
        operation = None if last is None else last.decision.action.operation
        stages[label] = dict(sequence=obs.sequence_id, x=p.x, z=p.z, yaw=obs.yaw_degrees.value,
            speed=math.hypot(velocity.x, velocity.z), gui_open=gui.open, gui_session=gui.gui_session_id,
            hotbar=obs.inventory.value.selected_hotbar_slot, status=None if last is None else last.report.status.value,
            receipt_status=None if receipt is None else receipt.status, reason=None if receipt is None else receipt.reason,
            operation=None if operation is None else operation.kind)
        append_jsonl(directory / "stages.jsonl", dict(label=label, state=stages[label]))
        print(f"FABRIC_RUNTIME_STAGE={episode}:{label}", flush=True)
    diagnostic()
    mark("start")
    submit("movement", movement=MovementV1(forward=1))
    submit("camera", look=LookV1(12, 0))
    air_evidence = None
    for index in range(11):
        if index == 0 and b02_air_probe:
            own = runtime.observation.self_state.value
            base_x, base_y, base_z = math.floor(own.position.x), math.floor(own.position.y), math.floor(own.position.z)
            expected_air = tuple(sorted((base_x + x, base_y + 1, base_z + z)
                                        for x in range(-4, 4) for z in range(-8, 8)))
            requested_solid = (base_x, base_y - 1, base_z)
            request = ObservationRequestV3("navigation_v1", tuple(sorted((*expected_air, requested_solid))))
            baseline_ns = (runtime.observation.client_sample.completed_at_monotonic_ns
                           - runtime.observation.client_sample.started_at_monotonic_ns)
            step(observation_request=request)
            observed = runtime.observation.perception.value.blocks
            positives = tuple(sorted(block.position for block in observed if "air_query" in block.sources))
            query_ns = (runtime.observation.client_sample.completed_at_monotonic_ns
                        - runtime.observation.client_sample.started_at_monotonic_ns)
            air_evidence = dict(requested_count=len(request.air_positions), expected_air_count=len(expected_air),
                confirmed_air_count=len(positives), positive_positions_match=positives == expected_air,
                non_air_not_confirmed=requested_solid not in positives, baseline_collection_ns=baseline_ns,
                query_collection_ns=query_ns, within_one_tick_budget=query_ns <= 50_000_000)
            write_json_atomic(directory / "b02-air-query.json", air_evidence)
        else:
            step()
    submit("camera", look=LookV1(90, 0))
    runtime.cancel_source("camera")
    step(); mark("moving")
    runtime.cancel_source("movement")
    for _ in range(6): step()
    mark("released")
    if b02_air_probe:
        for label, movement in (
            ("strafe", MovementV1(strafe=1)),
            ("diagonal", MovementV1(forward=1, strafe=1)),
        ):
            submit("movement", movement=movement)
            for _ in range(8):
                step()
            mark(label + "_moving")
            runtime.cancel_source("movement")
            for _ in range(6):
                step()
            mark(label + "_released")
    step(OpenInventoryV1()); mark("open")
    old_gui = runtime.observation.gui.value
    step(OpenInventoryV1()); mark("open_again")
    step(CloseScreenV1()); mark("closed")
    step(CloseScreenV1()); mark("closed_again")
    step(OpenInventoryV1()); mark("reopened")
    stale = old_gui if previous_gui is None else previous_gui
    step(ClickSlotV1(stale.gui_session_id, stale.sync_id, stale.revision, 0, 0, "pickup"), "failed"); mark("stale_rejected")
    step(CloseScreenV1()); mark("recovered")
    step(SelectHotbarV1(2)); mark("hotbar")
    step(); mark("no_repeat")
    runtime.cancel("player_stop")
    step(expected="cancelled"); mark("cancelled")
    if b02_air_probe:
        return stages, rows, old_gui, air_evidence
    return stages, rows, old_gui


def prepare_scenario(run_dir: Path, *, seed: int, port: int, container_probe: bool,visibility_probe: bool=False) -> dict:
    if type(container_probe) is not bool or type(visibility_probe) is not bool or (container_probe and visibility_probe):
        raise ValueError('probe scene selection must be exclusive booleans')
    world_name = f"mc2p-visibility-deployment-{seed}" if container_probe or visibility_probe else "world"
    result = {"server": prepare_server(run_dir / "server", seed=seed, port=port, world_name=world_name,
                                       fixture_animals=visibility_probe)}
    if container_probe or visibility_probe:
        result["fixture"] = build_visibility_fixture(run_dir / "fixture", seed=seed, level_name=world_name)
        installed = install_visibility_fixture(run_dir / "fixture", run_dir / "server")
        if installed != (run_dir / "server" / world_name).absolute():
            raise RuntimeError("installed fixture does not match the declared server world")
    return result


def collect_time_evidence(directory: Path, *, enabled: bool) -> tuple[dict, dict | None]:
    """Keep diagnostic failures separate from an already propagating Runtime failure."""
    name = "complete_time_event_attribution" if enabled else "no_time_sidecar_when_disabled"
    try:
        if enabled:
            passed = export_runtime_time_evidence(directory)["status"] == "passed"
        else:
            passed = not any((directory / item).exists()
                for item in ("mc2p-client-time.jsonl", "time-events.jsonl", "time-report.json"))
        return dict(name=name, passed=passed), None
    except BaseException as error:
        # This is called from the owner finally; even report-write failures must
        # leave the original exception intact while making the probe fail.
        return dict(name=name, passed=False), dict(stage="time_evidence_export",
            type=type(error).__name__, message=str(error))


def run_worker(run_dir: Path, launch: dict, seed: int, server_port: int, ipc_port: int, timeout: float,
               container_probe: bool = False, time_diagnostics: bool = False, mining_probe: bool = False,
               visibility_probe: bool = False, block_parity: bool = False, b02_air_probe: bool = False,
               b03_fixed_route_probe: bool = False, b03_shape_probe: bool = False,
               b04_known_map_probe: bool = False,
               b05_jump_calibration_probe: bool = False,
               b05_jump_route_probe: bool = False,
               b05_jump_acceptance_probe: bool = False,
               b06_ordinary_material_probe: bool = False,
               b07_step_probe: bool = False,
               b08_ground_modes_probe: bool = False,
               b09_air_motion_probe: bool = False,
               b10_gap_solver_probe: bool = False,
               physics_tick_diagnostics: bool = False) -> int:
    if sum((container_probe,mining_probe,visibility_probe,b02_air_probe,
            b03_fixed_route_probe,b03_shape_probe,b04_known_map_probe,
            b05_jump_calibration_probe,b05_jump_route_probe,
            b05_jump_acceptance_probe,b06_ordinary_material_probe,
            b07_step_probe,b08_ground_modes_probe,b09_air_motion_probe,
            b10_gap_solver_probe))>1:
        raise ValueError('probe scenarios are mutually exclusive')
    if block_parity and not visibility_probe: raise ValueError('block parity requires visibility scenario')
    source_before = frozen_deployment_sources(
        b03_fixed_route_probe=b03_fixed_route_probe,
        b03_shape_probe=b03_shape_probe,
        b04_known_map_probe=b04_known_map_probe,
        b05_jump_calibration_probe=b05_jump_calibration_probe,
        b05_jump_route_probe=b05_jump_route_probe,
        b05_jump_acceptance_probe=b05_jump_acceptance_probe,
        b06_ordinary_material_probe=b06_ordinary_material_probe,
        b07_step_probe=b07_step_probe,
        b08_ground_modes_probe=b08_ground_modes_probe,
        b09_air_motion_probe=b09_air_motion_probe,
        b10_gap_solver_probe=b10_gap_solver_probe,
    )
    deadline = time.perf_counter_ns() + round((timeout - 25) * 1e9)
    failure, cleanup_failures, checks, sessions = None, [], [], []
    diagnostic_failures = []
    session_records = []
    server = server_identity = None
    server_log = None
    provenance = {}
    try:
        provenance["launch"] = inspect_launch(launch)
        provenance["assets"] = verify_assets()
        provenance.update(prepare_scenario(run_dir, seed=seed, port=server_port, container_probe=container_probe,
                                          visibility_probe=visibility_probe))
        server_log = (run_dir / "server-console.log").open("xb")
        server_env = client_environment(dict(os.environ), token="0" * 64, server_port=server_port, ipc_port=ipc_port)
        for key in tuple(server_env):
            if key.startswith("MC2P_"): del server_env[key]
        server_command = [str(JAVA), "-Xms256M", "-Xmx1G", *PROXY_ARGS, "-jar", str(run_dir / "server/server.jar"), "nogui"]
        write_json_atomic(run_dir / "server-command.json", server_command)
        server = subprocess.Popen(server_command, cwd=run_dir / "server", env=server_env, stdin=subprocess.PIPE,
            stdout=server_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        server_identity = ClientProcessIdentity(server.pid, psutil.Process(server.pid).create_time())
        write_json_atomic(run_dir / "server-identity.json", asdict(server_identity))
        ready_deadline = min(deadline, time.perf_counter_ns() + 60_000_000_000)
        while True:
            _live(server_identity)
            if 'Done (' in (run_dir / "server-console.log").read_text("utf-8", errors="replace"):
                break
            if time.perf_counter_ns() >= ready_deadline:
                raise TimeoutError("independent server startup deadline exceeded")
            time.sleep(.05)
        previous_gui = None
        for number in range(1 if (b03_shape_probe or b04_known_map_probe
                                  or b05_jump_calibration_probe
                                  or b05_jump_route_probe
                                  or b05_jump_acceptance_probe
                                  or b06_ordinary_material_probe
                                  or b07_step_probe or b08_ground_modes_probe
                                  or b09_air_motion_probe
                                  or b10_gap_solver_probe) else 2):
            directory = run_dir / f"client-{number}"
            directory.mkdir(exist_ok=False)
            options = "pauseOnLostFocus:false\nrenderDistance:2\nsimulationDistance:5\nmaxFps:60\nenableVsync:false\ntutorialStep:none\njoinedFirstServer:true\nskipMultiplayerWarning:true\nsoundCategory_master:0.0\n"
            if (b05_jump_calibration_probe or b05_jump_route_probe
                    or b05_jump_acceptance_probe or b06_ordinary_material_probe
                    or b07_step_probe or b08_ground_modes_probe
                    or b09_air_motion_probe or b10_gap_solver_probe):
                options += "autoJump:false\n"
            (directory / "options.txt").write_text(options, encoding="utf-8")
            token = secrets.token_hex(32)
            environment = client_environment(dict(os.environ), token=token, server_port=server_port, ipc_port=ipc_port,
                time_diagnostics=time_diagnostics,block_parity_diagnostics=block_parity,
                physics_tick_diagnostics=physics_tick_diagnostics)
            player_uuid = str(uuid.UUID(bytes=hashlib.md5(b"OfflinePlayer:MC2PProbe").digest(), version=3))
            arguments = ["-Xms256M", "-Xmx2G", *PROXY_ARGS, "-Djava.net.preferIPv4Stack=true", *launch["jvm_args"],
                "-cp", os.pathsep.join(str(ROOT / item["path"]) for item in launch["classpath"]), launch["main_class"],
                "--username", "MC2PProbe", "--uuid", player_uuid, "--accessToken", "0", "--version", "1.21",
                "--gameDir", str(directory), "--quickPlayMultiplayer", f"127.0.0.1:{server_port}"]
            write_argument_file(directory / "client.args", arguments)
            process = identity = backend = runtime = None
            client_preclosed = False
            with (directory / "console.log").open("xb") as output:
                transport = DeploymentTransport(port=ipc_port)
                try:
                    process = subprocess.Popen([str(JAVA), "@" + str(directory / "client.args")], cwd=directory, env=environment,
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                    identity = ClientProcessIdentity(process.pid, psutil.Process(process.pid).create_time())
                    write_json_atomic(directory / "identity.json", asdict(identity))
                    backend = create_deployment_backend(transport, identity, token, server_port)
                    trace = _runtime_trace(
                        directory,
                        backend,
                        capture_close_diagnostics=visibility_probe or time_diagnostics,
                    )
                    runtime = PlayerRuntimeV1(backend,trace)
                    episode = f"fabric-{seed}-{number}"
                    reset = runtime.reset(ResetRequestV0(f"reset-{number}", episode, "remote-session", 0,
                        min(deadline, time.perf_counter_ns() + 90_000_000_000)))
                    if not reset.succeeded:
                        raise RuntimeError(str(reset.failure))
                    proof = _connection_proof(server_identity, identity, backend.peer_identity_proof, server_port)
                    write_json_atomic(directory / "connection-proof.json", proof)
                    if not validate_connection_evidence(proof, server_port=server_port, ipc_port=ipc_port):
                        raise RuntimeError("independent OS connection proof failed")
                    initial = runtime.observation
                    air_evidence = None
                    if visibility_probe and number==0:
                        from scripts.fabric_visibility_scenario import run_visibility_runtime
                        from scripts.probe_observation_v2_visibility import evaluate_visibility_stages
                        stages,rows,episode_checks=run_visibility_runtime(runtime,backend,episode,directory,deadline)
                        episode_checks+=evaluate_visibility_stages(stages)
                    elif mining_probe:
                        from scripts.mining_runtime_core import run_mining_scenario, run_mining_reconnect, evaluate_mining
                        rows, stages, episode_checks = [], {}, []
                        def mining_diagnostic():
                            row = dict(episode_id=episode, observation_sequence_id=runtime.observation.sequence_id,
                                       diagnostics=backend.last_diagnostics)
                            rows.append(row)
                            append_jsonl(directory / "diagnostics.jsonl", row)
                        if number == 0:
                            stages = run_mining_scenario(runtime, directory, deadline, on_observation=mining_diagnostic)
                        else:
                            episode_checks = run_mining_reconnect(runtime, directory, deadline, sessions[0]["final"],
                                                                  on_observation=mining_diagnostic)
                    elif b03_fixed_route_probe:
                        from scripts.fixed_route_runtime_core import run_fixed_route_runtime
                        def write_b03_fixture(positions, material):
                            if material not in {
                                "minecraft:stone", "minecraft:air", "minecraft:grass_block",
                            }:
                                raise ValueError("undeclared B03 fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B03 fixture server command channel is unavailable")
                            commands = [
                                f"setblock {x} {y} {z} {material} replace"
                                for x, y, z in positions
                            ]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b03-fixture-commands.jsonl", dict(
                                material=material, positions=[list(position) for position in positions],
                            ))
                        stages, rows, episode_checks = run_fixed_route_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b03_fixture,
                        )
                    elif b03_shape_probe:
                        from scripts.fixed_route_shape_runtime import run_fixed_route_shape_runtime
                        stages, rows, episode_checks = run_fixed_route_shape_runtime(
                            runtime, backend, episode, directory, deadline,
                        )
                    elif b04_known_map_probe:
                        from scripts.known_map_navigation_runtime import run_known_map_navigation_runtime
                        def write_b04_fixture(positions, material):
                            if material not in {
                                "minecraft:stone", "minecraft:air", "minecraft:grass_block",
                            }:
                                raise ValueError("undeclared B04 fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B04 fixture server command channel is unavailable")
                            commands = [
                                f"setblock {x} {y} {z} {material} replace"
                                for x, y, z in positions
                            ]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b04-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        stages, rows, episode_checks = run_known_map_navigation_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b04_fixture,
                        )
                    elif b05_jump_calibration_probe:
                        from scripts.jump_up_calibration_runtime import run_jump_up_calibration_runtime
                        def write_b05_fixture(positions, material):
                            if material not in {"minecraft:air", "minecraft:grass_block"}:
                                raise ValueError("undeclared B05 fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B05 fixture server command channel is unavailable")
                            commands = [
                                f"setblock {x} {y} {z} {material} replace"
                                for x, y, z in positions
                            ]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b05-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        stages, rows, episode_checks = run_jump_up_calibration_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b05_fixture,
                        )
                    elif b05_jump_route_probe:
                        from scripts.jump_up_navigation_runtime import run_jump_up_navigation_runtime
                        def write_b05_route_fixture(positions, material):
                            if material not in {"minecraft:air", "minecraft:grass_block"}:
                                raise ValueError("undeclared B05 route fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B05 route fixture server command channel is unavailable")
                            commands = [
                                f"setblock {x} {y} {z} {material} replace"
                                for x, y, z in positions
                            ]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b05-route-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        def teleport_b05_route_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B05 route server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b05-route-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_jump_up_navigation_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b05_route_fixture,
                            player_teleporter=teleport_b05_route_player,
                        )
                    elif b05_jump_acceptance_probe:
                        from scripts.jump_up_acceptance_runtime import run_jump_up_acceptance_runtime
                        def write_b05_acceptance_fixture(positions, material):
                            if material not in {"minecraft:air", "minecraft:grass_block",
                                                "minecraft:stone"}:
                                raise ValueError("undeclared B05 acceptance fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B05 acceptance server command channel is unavailable")
                            commands = [f"setblock {x} {y} {z} {material} replace"
                                        for x, y, z in positions]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b05-acceptance-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        def teleport_b05_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B05 acceptance server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b05-acceptance-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_jump_up_acceptance_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b05_acceptance_fixture,
                            player_teleporter=teleport_b05_player,
                        )
                    elif b06_ordinary_material_probe:
                        from scripts.b06_ordinary_material_runtime import run_b06_ordinary_material_runtime
                        allowed_materials = {
                            "minecraft:air", "minecraft:dirt", "minecraft:glass",
                            "minecraft:grass_block", "minecraft:oak_planks", "minecraft:stone",
                        }
                        def write_b06_fixture(positions, material):
                            if material not in allowed_materials:
                                raise ValueError("undeclared B06 material fixture")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B06 fixture server command channel is unavailable")
                            commands = [f"setblock {x} {y} {z} {material} replace"
                                        for x, y, z in positions]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b06-material-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        def teleport_b06_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B06 fixture server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b06-material-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_b06_ordinary_material_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b06_fixture,
                            player_teleporter=teleport_b06_player,
                        )
                    elif b07_step_probe:
                        from scripts.step_transition_runtime import run_step_transition_runtime
                        allowed_materials = {
                            "minecraft:air", "minecraft:grass_block",
                            "minecraft:smooth_stone_slab[type=bottom]",
                            "minecraft:smooth_stone_slab[type=top]",
                            "minecraft:oak_stairs[facing=south,half=bottom,shape=straight,waterlogged=false]",
                            "minecraft:oak_stairs[facing=south,half=bottom,shape=inner_left,waterlogged=false]",
                            "minecraft:white_carpet", "minecraft:snow[layers=2]",
                            "minecraft:snow[layers=4]", "minecraft:snow[layers=5]",
                            "minecraft:snow[layers=6]",
                            "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]",
                            "minecraft:dirt_path", "minecraft:oak_fence",
                            "minecraft:cobblestone_wall", "minecraft:iron_bars",
                            "minecraft:oak_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]",
                            "minecraft:oak_trapdoor[facing=north,half=bottom,open=true,powered=false,waterlogged=false]",
                            "minecraft:farmland[moisture=0]",
                        }
                        def write_b07_fixture(positions, material):
                            if material not in allowed_materials:
                                raise ValueError("undeclared B07 Step fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B07 fixture server command channel is unavailable")
                            commands = [f"setblock {x} {y} {z} {material} replace"
                                        for x, y, z in positions]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b07-step-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        def teleport_b07_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B07 fixture server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b07-step-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_step_transition_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b07_fixture,
                            player_teleporter=teleport_b07_player,
                        )
                    elif b08_ground_modes_probe:
                        from scripts.ground_modes_runtime import run_ground_modes_runtime
                        allowed_materials = {"minecraft:air", "minecraft:grass_block"}
                        def write_b08_fixture(positions, material):
                            if material not in allowed_materials:
                                raise ValueError("undeclared B08 ground-mode fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B08 fixture server command channel is unavailable")
                            commands = [f"setblock {x} {y} {z} {material} replace"
                                        for x, y, z in positions]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b08-fixture-commands.jsonl", dict(
                                material=material, positions=[list(position) for position in positions],
                            ))
                        def teleport_b08_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B08 fixture server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b08-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_ground_modes_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b08_fixture,
                            player_teleporter=teleport_b08_player,
                        )
                    elif b09_air_motion_probe:
                        from scripts.air_motion_runtime import run_air_motion_runtime
                        allowed_materials = {"minecraft:air", "minecraft:grass_block"}
                        def write_b09_fixture(positions, material):
                            if material not in allowed_materials:
                                raise ValueError("undeclared B09 air-motion fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B09 fixture server command channel is unavailable")
                            commands = [f"setblock {x} {y} {z} {material} replace"
                                        for x, y, z in positions]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b09-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        def teleport_b09_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B09 fixture server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b09-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_air_motion_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b09_fixture,
                            player_teleporter=teleport_b09_player,
                        )
                    elif b10_gap_solver_probe:
                        from scripts.b10_gap_solver_runtime import run_b10_gap_solver_runtime
                        allowed_materials = {"minecraft:air", "minecraft:grass_block"}
                        def write_b10_fixture(positions, material):
                            if material not in allowed_materials:
                                raise ValueError("undeclared B10 gap-solver fixture material")
                            if server is None or server.stdin is None:
                                raise RuntimeError("B10 fixture server command channel is unavailable")
                            commands = [f"setblock {x} {y} {z} {material} replace"
                                        for x, y, z in positions]
                            server.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b10-fixture-commands.jsonl", dict(
                                material=material,
                                positions=[list(position) for position in positions],
                            ))
                        def teleport_b10_player(x, y, z, yaw, pitch):
                            if server is None or server.stdin is None:
                                raise RuntimeError("B10 fixture server command channel is unavailable")
                            command = f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} {yaw:.6f} {pitch:.6f}"
                            server.stdin.write((command + "\n").encode("utf-8"))
                            server.stdin.flush()
                            append_jsonl(directory / "b10-teleports.jsonl", dict(
                                x=x, y=y, z=z, yaw=yaw, pitch=pitch,
                            ))
                        stages, rows, episode_checks = run_b10_gap_solver_runtime(
                            runtime, backend, episode, directory, deadline,
                            fixture_writer=write_b10_fixture,
                            player_teleporter=teleport_b10_player,
                        )
                    else:
                        scenario = run_container_scenario if container_probe else _scenario
                        if b02_air_probe:
                            stages, rows, previous_gui, air_evidence = _scenario(
                                runtime, backend, episode, directory, deadline, previous_gui,
                                b02_air_probe=True)
                        else:
                            stages, rows, previous_gui = scenario(runtime, backend, episode, directory, deadline, previous_gui)
                            air_evidence = None
                    final = runtime.observation
                    if (b03_fixed_route_probe or b03_shape_probe
                            or b04_known_map_probe or b05_jump_calibration_probe
                            or b05_jump_route_probe or b05_jump_acceptance_probe
                            or b06_ordinary_material_probe or b07_step_probe
                            or b08_ground_modes_probe or b09_air_motion_probe
                            or b10_gap_solver_probe):
                        # Motion scenarios can produce large offline reports. Close
                        # the live control session before parsing and serializing
                        # them, otherwise the fixed client input-sample ledger can
                        # fill while no Runtime step is consuming receipts.
                        cleanup, close_failures = _close_client(
                            runtime, backend, transport, process, identity,
                        )
                        client_preclosed = True
                        cleanup_failures.extend(close_failures)
                        if cleanup is not None:
                            write_json_atomic(directory / "cleanup.json", cleanup)
                            if not cleanup["passed"] or not cleanup["graceful"]:
                                cleanup_failures.append(dict(client=number, **cleanup))
                        runtime = backend = process = None
                    records = [json.loads(line) for line in (directory / "trace.jsonl").read_text("utf-8").splitlines()]
                    session_records.append(records)
                    if visibility_probe and number==0:
                        pass  # Shared visibility stages already evaluated above.
                    elif mining_probe:
                        if number == 0: episode_checks = evaluate_mining(records, stages)
                    elif (b03_fixed_route_probe or b03_shape_probe or b04_known_map_probe
                          or b05_jump_calibration_probe or b05_jump_route_probe
                          or b05_jump_acceptance_probe or b06_ordinary_material_probe
                          or b07_step_probe or b08_ground_modes_probe
                          or b09_air_motion_probe or b10_gap_solver_probe):
                        pass
                    else:
                        episode_checks = (evaluate_container(stages, records) if container_probe else evaluate_stages(
                            {name: stages[name] for name in RUNTIME_STAGES if name in stages}
                        ))
                        if b02_air_probe:
                            episode_checks.append({"name": "b02_positive_only_bounded_air_query", "passed":
                                air_evidence is not None and air_evidence["positive_positions_match"]
                                and air_evidence["non_air_not_confirmed"]
                                and air_evidence["within_one_tick_budget"]})
                    episode_checks += evaluate_trace(records, rows, server_port=server_port,
                        expected_steps=final.sequence_id if (container_probe or mining_probe or visibility_probe
                                                             or b02_air_probe or b03_fixed_route_probe
                                                             or b03_shape_probe or b04_known_map_probe
                                                             or b05_jump_calibration_probe
                                                             or b05_jump_route_probe
                                                             or b05_jump_acceptance_probe
                                                             or b06_ordinary_material_probe
                                                             or b07_step_probe
                                                             or b08_ground_modes_probe
                                                             or b09_air_motion_probe
                                                             or b10_gap_solver_probe) else 28,
                        require_gui_attempts=not (
                            b03_fixed_route_probe or b03_shape_probe or b04_known_map_probe
                            or b05_jump_calibration_probe or b05_jump_route_probe
                            or b05_jump_acceptance_probe or b06_ordinary_material_probe
                            or b07_step_probe or b08_ground_modes_probe
                            or b09_air_motion_probe or b10_gap_solver_probe))
                    checks.extend({**check, "name": f"client-{number}:" + check["name"]} for check in episode_checks)
                    sessions.append(dict(identity=asdict(identity), episode=episode, initial=trace_projection(initial),
                        final=trace_projection(final), stages=stages, proof=proof,
                        b02_air_query=air_evidence,
                        b03_fixed_route=stages if b03_fixed_route_probe else None,
                        b03_shape=stages if b03_shape_probe else None,
                        b04_known_map=stages if b04_known_map_probe else None,
                        b05_jump_calibration=stages if b05_jump_calibration_probe else None,
                        b05_jump_route=stages if b05_jump_route_probe else None,
                        b05_jump_acceptance=stages if b05_jump_acceptance_probe else None,
                        b06_ordinary_material=stages if b06_ordinary_material_probe else None,
                        b07_step=stages if b07_step_probe else None))
                    if b08_ground_modes_probe:
                        sessions[-1]["b08_ground_modes"] = stages
                    if b09_air_motion_probe:
                        sessions[-1]["b09_air_motion"] = stages
                    if b10_gap_solver_probe:
                        sessions[-1]["b10_gap_solver"] = stages
                    write_json_atomic(directory / "episode.json", sessions[-1])
                finally:
                    if not client_preclosed:
                        cleanup, close_failures = _close_client(
                            runtime, backend, transport, process, identity,
                        )
                        cleanup_failures.extend(close_failures)
                        if cleanup is not None:
                            write_json_atomic(directory / "cleanup.json", cleanup)
                            if not cleanup["passed"] or not cleanup["graceful"]:
                                cleanup_failures.append(dict(client=number, **cleanup))
                    time_check, diagnostic_failure = collect_time_evidence(directory, enabled=time_diagnostics)
                    checks.append({**time_check, "name": f"client-{number}:" + time_check["name"]})
                    if diagnostic_failure is not None:
                        diagnostic_failures.append(dict(client=number, **diagnostic_failure))
                    try:
                        closed_records=[json.loads(line) for line in (directory/'trace.jsonl').read_text('utf-8').splitlines()]
                        parity_check=parity_file_check(directory/'mc2p-block-parity.jsonl',
                            formal_samples_from_records(closed_records),enabled=block_parity,
                            require_profile_cycle=visibility_probe and number==0)
                    except (OSError,ValueError,KeyError,TypeError) as error:
                        parity_check=dict(name='block_parity_evidence',passed=False,error=str(error))
                    checks.append(dict(parity_check,name=f'client-{number}:'+parity_check['name']))
            _live(server_identity)
            if not port_free(ipc_port):
                raise RuntimeError("old client IPC port survived close")
        first = sessions[0]
        if b03_shape_probe:
            scenarios = first["stages"].get("scenarios", [])
            checks += [
                dict(name="b03_shape_session_has_seven_replays", passed=len(scenarios) == 7),
                dict(name="b03_shape_session_has_line_and_six_circles", passed=
                     {scenario.get("kind") for scenario in scenarios} == {"line", "circle"}
                     and sum(scenario.get("kind") == "circle" for scenario in scenarios) == 6),
            ]
        elif not (b04_known_map_probe or b05_jump_calibration_probe
                  or b05_jump_route_probe or b05_jump_acceptance_probe
                  or b06_ordinary_material_probe or b07_step_probe
                  or b08_ground_modes_probe or b09_air_motion_probe
                  or b10_gap_solver_probe):
            first, second = sessions
            if container_probe:
                checks += evaluate_container_reconnect(first["stages"], session_records[0], second["stages"], session_records[1])
        if b03_fixed_route_probe:
            all_trials = first["stages"]["trials"] + second["stages"]["trials"]
            scenario_counts = {
                scenario: sum(trial["scenario"] == scenario for trial in all_trials)
                for scenario in {trial["scenario"] for trial in all_trials}
            }
            checks += [
                dict(name="b03_each_critical_scenario_has_ten_real_runs",
                     passed=len(all_trials) == 50 and len(scenario_counts) == 5
                     and set(scenario_counts.values()) == {10}),
                dict(name="b03_nonzero_pitch_routes_executed",
                     passed=any(abs(trial["pitch_degrees"]) >= 15 for trial in all_trials)),
            ]
        if not (b03_shape_probe or b04_known_map_probe or b05_jump_calibration_probe
                or b05_jump_route_probe or b05_jump_acceptance_probe
                or b06_ordinary_material_probe or b07_step_probe
                or b08_ground_modes_probe or b09_air_motion_probe
                or b10_gap_solver_probe):
            start, end = second["initial"], first["final"]
            checks += [dict(name="new_jvm_episode_and_client_clock", passed=first["identity"] != second["identity"]
                and first["episode"] != second["episode"] and end["client_sample"]["clock_id"] != start["client_sample"]["clock_id"]),
                dict(name="same_server_preserves_normal_player_state", passed=start["inventory"]["value"]["selected_hotbar_slot"] == end["inventory"]["value"]["selected_hotbar_slot"]
                    and math.dist([start["position"]["value"][key] for key in ("x", "y", "z")],
                                  [end["position"]["value"][key] for key in ("x", "y", "z")]) < .1)]
        checks.append(dict(name="source_cache_and_launch_unchanged", passed=
            inspect_launch(launch) == provenance["launch"]
            and _hash(run_dir / "server/server.jar", "sha1") == SERVER_SHA1))
    except BaseException as error:
        failure = dict(type=type(error).__name__, message=str(error))
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if server is not None:
            try:
                cleanup = _stop(server, server_identity, server=True)
                write_json_atomic(run_dir / "server-cleanup.json", cleanup)
                if not cleanup["passed"] or not cleanup["graceful"] or cleanup["return_code"] != 0:
                    cleanup_failures.append(dict(server=cleanup))
            except BaseException as error:
                cleanup_failures.append(dict(type=type(error).__name__, message=str(error)))
        if server_log is not None:
            server_log.close()
    source_after = frozen_deployment_sources(
        b03_fixed_route_probe=b03_fixed_route_probe,
        b03_shape_probe=b03_shape_probe,
        b04_known_map_probe=b04_known_map_probe,
        b05_jump_calibration_probe=b05_jump_calibration_probe,
        b05_jump_route_probe=b05_jump_route_probe,
        b05_jump_acceptance_probe=b05_jump_acceptance_probe,
        b06_ordinary_material_probe=b06_ordinary_material_probe,
        b07_step_probe=b07_step_probe,
        b08_ground_modes_probe=b08_ground_modes_probe,
        b09_air_motion_probe=b09_air_motion_probe,
        b10_gap_solver_probe=b10_gap_solver_probe,
    )
    checks.append(dict(name='v3_python_and_java_sources_unchanged',passed=source_before==source_after))
    if failure is None and (not checks or not all(check["passed"] for check in checks)):
        failure = dict(type="DeploymentEvidenceFailure", message=str([c["name"] for c in checks if not c["passed"]]))
    result = dict(schema_version="mc2p.fabric-deployment-probe.v2", observation_schema_version="mc2p.observation.v3",
        knowledge_model="block_state_v1", default_field_profile="navigation_v1",
        field_profiles=["navigation_v1", "interaction_v1"] if (mining_probe or container_probe or visibility_probe) else ["navigation_v1"],
        seed=seed, provenance=provenance,
        scenario="visibility" if visibility_probe else "mining" if mining_probe else "container" if container_probe
            else "b02-air-query" if b02_air_probe else "b03-fixed-route" if b03_fixed_route_probe
            else "b03-shape-tracking" if b03_shape_probe
            else "b04-known-map" if b04_known_map_probe
            else "b05-jump-calibration" if b05_jump_calibration_probe
            else "b05-jump-route" if b05_jump_route_probe
            else "b05-jump-acceptance" if b05_jump_acceptance_probe
            else "b06-ordinary-material" if b06_ordinary_material_probe
            else "b07-step" if b07_step_probe
            else "b08-ground-modes" if b08_ground_modes_probe
            else "b09-air-motion" if b09_air_motion_probe
            else "b10-gap-solver" if b10_gap_solver_probe else "runtime-controls-gui",
        block_parity=block_parity,
        core_sources_before=source_before,core_sources_after=source_after,
        perception_variant='active_perception_v1' if mining_probe else None,
        perception_config=asdict(PerceptionConfig()) if mining_probe else None,
        time_diagnostics=time_diagnostics,
        physics_tick_diagnostics=physics_tick_diagnostics,
        status="passed" if failure is None and not cleanup_failures else "failed", checks=checks,
        primary_failure=failure, cleanup_failures=cleanup_failures, diagnostic_failures=diagnostic_failures, sessions=sessions,
        limits=["local vanilla server/direct client interface slice only", "no complete timing, visibility or training acceptance"])
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, choices=(21001, 21002, 21003), default=21001)
    parser.add_argument("--server-port", type=int, default=25597)
    parser.add_argument("--ipc-port", type=int, default=8140)
    parser.add_argument("--timeout-seconds", type=float, default=240)
    parser.add_argument("--launch-json", type=Path)
    parser.add_argument("--container-probe", action="store_true", help="verify nonempty normal chest transfer and reconnect")
    parser.add_argument("--mining-probe", action="store_true", help="verify shared Runtime mining, pickup, placement and persisted server state")
    parser.add_argument("--time-diagnostics", action="store_true", help="record read-only tick/time-packet attribution outside actor payloads")
    parser.add_argument("--physics-tick-diagnostics", action="store_true",
                        help="join pre-state, sampled input and post-state for each actor movement tick")
    parser.add_argument('--visibility-probe',action='store_true',help='run the shared original-speed visibility scene')
    parser.add_argument('--block-parity',action='store_true',help='test-only same-tick first-hit comparison, not a speed benchmark')
    parser.add_argument('--b02-air-probe',action='store_true',help='measure bounded positive-only air confirmation')
    parser.add_argument('--b03-fixed-route-probe',action='store_true',help='run B03 fixed-route walking, cancellation and lease checks')
    parser.add_argument('--b03-shape-probe',action='store_true',
                        help='run B03 rotating-view line and fixed-view circle trials')
    parser.add_argument('--b04-known-map-probe',action='store_true',
                        help='run B04 known-map background planning and execution')
    parser.add_argument('--b05-jump-calibration-probe',action='store_true',
                        help='calibrate one observed low-speed adjacent one-block JumpUp')
    parser.add_argument('--b05-jump-route-probe',action='store_true',
                        help='plan and execute one Fabric Walk-JumpUp-Walk route')
    parser.add_argument('--b05-jump-acceptance-probe',action='store_true',
                        help='run 100 repeated Fabric JumpUp acceptance trials')
    parser.add_argument('--b06-ordinary-material-probe',action='store_true',
                        help='validate ordinary-material Walk and JumpUp profile reuse')
    parser.add_argument('--b07-step-probe',action='store_true',
                        help='calibrate observed half-block StepUp and StepDown')
    parser.add_argument('--b08-ground-modes-probe',action='store_true',
                        help='calibrate and validate Sprint and Crouch fixed routes')
    parser.add_argument('--b09-air-motion-probe',action='store_true',
                        help='calibrate and validate gap jumps and controlled drops')
    parser.add_argument('--b10-gap-solver-probe', action='store_true',
                        help='solve and validate one-cell gaps in a controlled session')
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sum((args.container_probe,args.mining_probe,args.visibility_probe,args.b02_air_probe,
            args.b03_fixed_route_probe,args.b03_shape_probe,args.b04_known_map_probe,
            args.b05_jump_calibration_probe,args.b05_jump_route_probe,
            args.b05_jump_acceptance_probe,args.b06_ordinary_material_probe,
            args.b07_step_probe,args.b08_ground_modes_probe,
            args.b09_air_motion_probe,args.b10_gap_solver_probe))>1:
        parser.error('probe scenarios are mutually exclusive')
    if args.block_parity and not args.visibility_probe: parser.error('block parity requires visibility scenario')
    if args.physics_tick_diagnostics and not args.time_diagnostics:
        parser.error('physics tick diagnostics require --time-diagnostics')
    if (not math.isfinite(args.timeout_seconds) or not 120 <= args.timeout_seconds <= 600
            or not 1 <= args.server_port <= 65535 or not 1 <= args.ipc_port <= 65535 or args.server_port == args.ipc_port):
        parser.error("invalid bounded probe configuration")
    if args.worker:
        if args.launch_json is None or args.run_dir is None:
            parser.error("worker requires prepared inputs")
        _no_links(args.run_dir.absolute())
        return run_worker(args.run_dir.absolute(), json.loads(args.launch_json.read_text("utf-8")), args.seed,
                          args.server_port, args.ipc_port, args.timeout_seconds, args.container_probe, args.time_diagnostics, args.mining_probe,
                          args.visibility_probe,args.block_parity,args.b02_air_probe,
                          args.b03_fixed_route_probe,args.b03_shape_probe,
                          args.b04_known_map_probe,args.b05_jump_calibration_probe,
                          args.b05_jump_route_probe,args.b05_jump_acceptance_probe,
                          args.b06_ordinary_material_probe,args.b07_step_probe,
                          args.b08_ground_modes_probe,args.b09_air_motion_probe,
                          args.b10_gap_solver_probe,
                          args.physics_tick_diagnostics)

    if not port_free(args.server_port) or not port_free(args.ipc_port):
        parser.error("a local test port is occupied")
    if psutil.virtual_memory().available < 6 * 1024 ** 3 or shutil.disk_usage(ROOT).free < 10 * 1024 ** 3:
        parser.error("insufficient local memory or disk headroom")
    with socket.create_connection(("127.0.0.1", 7897), timeout=2):
        pass
    launch_path = args.launch_json or export_launch()
    _no_links(launch_path.absolute())
    launch = json.loads(launch_path.read_text("utf-8"))
    inspect_launch(launch)
    root = ROOT / "artifacts/fabric-deployment"
    _no_links(root.parent)
    root.mkdir(exist_ok=True)
    _no_links(root)
    run_dir = root / make_run_id()
    run_dir.mkdir(exist_ok=False)
    write_json_atomic(run_dir / "launch.json", launch)
    print(f"FABRIC_DEPLOYMENT_RUN_DIR={run_dir}", flush=True)
    command = [sys.executable, str(Path(__file__).absolute()), "--worker", "--run-dir", str(run_dir),
        "--launch-json", str(run_dir / "launch.json"), "--seed", str(args.seed), "--server-port", str(args.server_port),
        "--ipc-port", str(args.ipc_port), "--timeout-seconds", str(args.timeout_seconds)]
    if args.container_probe:
        command.append("--container-probe")
    if args.mining_probe:
        command.append("--mining-probe")
    if args.time_diagnostics:
        command.append("--time-diagnostics")
    if args.physics_tick_diagnostics:
        command.append("--physics-tick-diagnostics")
    if args.visibility_probe: command.append('--visibility-probe')
    if args.block_parity: command.append('--block-parity')
    if args.b02_air_probe: command.append('--b02-air-probe')
    if args.b03_fixed_route_probe: command.append('--b03-fixed-route-probe')
    if args.b03_shape_probe: command.append('--b03-shape-probe')
    if args.b04_known_map_probe: command.append('--b04-known-map-probe')
    if args.b05_jump_calibration_probe: command.append('--b05-jump-calibration-probe')
    if args.b05_jump_route_probe: command.append('--b05-jump-route-probe')
    if args.b05_jump_acceptance_probe: command.append('--b05-jump-acceptance-probe')
    if args.b06_ordinary_material_probe: command.append('--b06-ordinary-material-probe')
    if args.b07_step_probe: command.append('--b07-step-probe')
    if args.b08_ground_modes_probe: command.append('--b08-ground-modes-probe')
    if args.b09_air_motion_probe: command.append('--b09-air-motion-probe')
    if args.b10_gap_solver_probe: command.append('--b10-gap-solver-probe')
    supervision = run_bounded_process(command, cwd=ROOT, environment=dict(os.environ), log_path=run_dir / "worker.log",
                                      timeout_seconds=args.timeout_seconds)
    write_json_atomic(run_dir / "supervision.json", trace_projection(supervision))
    result_path = run_dir / "result.json"
    worker = json.loads(result_path.read_text("utf-8")) if result_path.is_file() else dict(status="failed", primary_failure="worker_result_missing", checks=[], cleanup_failures=[])
    write_json_atomic(run_dir / "worker-result.json", worker)
    result = finalize_result(worker, supervision, port_free(args.server_port) and port_free(args.ipc_port))
    write_json_atomic(result_path, result)
    if result["status"] == "passed":
        print("FABRIC_DEPLOYMENT_MINING_OK" if args.mining_probe else "FABRIC_DEPLOYMENT_CONTAINER_OK" if args.container_probe else "FABRIC_DEPLOYMENT_RUNTIME_OK")
        return 0
    print("FABRIC_DEPLOYMENT_MINING_FAILED" if args.mining_probe else "FABRIC_DEPLOYMENT_CONTAINER_FAILED" if args.container_probe else "FABRIC_DEPLOYMENT_RUNTIME_FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
