"""Fail-closed D102 evidence validation; no production control implementation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.motion_nav.d102_contract_cases import (
    RED_METHODS, canonical_bytes, digest, write_contract_evidence,
)

D102_STAGE_ORDER = ("G0", "G1", "G2", "G3", "F-TICK", "F-CONTINUITY", "G4")
MANIFEST_PATH = ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json"
EVENT_FIELDS = ("thread_events", "activation_events", "reference_events",
                "publication_events", "consumption_events")
SOURCE_FILES = (
    "scripts/verify_prepared_motion_d102.py",
    "tests/motion_nav/d102_contract_cases.py",
    "tests/motion_nav/test_d102_contract_red.py",
    "tests/motion_nav/test_d102_evidence_contract.py",
    "tests/motion_nav/test_b10_gap_solver.py",
    "mc2p/motion_nav/motion_worker.py", "mc2p/motion_nav/motion_solver.py",
    "mc2p/motion_nav/world_model.py", "mc2p/motion_nav/physics_adapter.py",
    "mc2p/motion_nav/physics_types.py", "mc2p/motion_nav/physics_1_21.py",
    "mc2p/motion_nav/physics_rollout.py", "mc2p/motion_nav/motion_risk.py",
    "mc2p/motion_nav/async_work.py", "mc2p/motion_nav/online_motion.py",
    "mc2p/contracts/action_v1.py", "mc2p/contracts/common.py",
    "docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md",
)
G1_SOURCE_FILES = SOURCE_FILES + (
    "mc2p/contracts/motion_activation.py", "mc2p/motion_nav/input_stream.py",
    "mc2p/motion_nav/prepared_motion.py", "mc2p/motion_nav/prepared_motion_validation.py",
    "mc2p/motion_nav/prepared_motion_proof.py", "mc2p/motion_nav/world_snapshot.py",
    "mc2p/motion_nav/action_route_executor.py", "mc2p/motion_nav/motion_candidate.py",
    "mc2p/motion_nav/route_body_controller.py", "mc2p/motion_nav/execution_supervisor.py",
    "mc2p/motion_nav/motion_coordination.py", "mc2p/motion_nav/navigation_session.py",
    "mc2p/skills/navigation_session_driver.py", "mc2p/runtime/player_runtime_v1.py",
    "tests/motion_nav/test_d102_runtime_activation.py", "tests/motion_nav/test_d102_action_handoff.py",
    "tests/motion_nav/test_d102_receiver.py",
)
G0_PREDECESSOR = ROOT / ".tmp/d102-formal-v1/red/20261012T0209255251309Z-53798e265e2e4b96955aa672fef74d0c/index.json"
G0_PREDECESSOR_SHA256 = "e643368dfaea5aa4f75e67d556feeabf20b22fe9cde0e4b9ec872ab295afa04c"
G0_SOURCE_COMMIT = "1ff77a98feff9f185a65c6cb358ea84512142ec7"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    if type(value) is not dict:
        raise ValueError("evidence document must be an object")
    return value


def _relative_file(base: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("artifact requires a relative path")
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("artifact missing or outside its unique run")
    return path


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _stream_generation(value: object) -> bool:
    if _integer(value):
        return True
    if type(value) is not str or len(value.encode("utf-8")) > 128:
        return False
    from mc2p.contracts.common import ContractViolation, require_identifier
    try:
        require_identifier(value, "stream generation")
    except ContractViolation:
        return False
    return True


def _same_generation(first, second) -> bool:
    return type(first) is type(second) and first == second


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _body_errors(value: object) -> list[str]:
    if type(value) is not dict:
        return ["body state is not an object"]
    errors = []
    for name in ("position", "velocity"):
        vector = value.get(name)
        if type(vector) is not list or len(vector) != 3 or not all(_number(part) for part in vector):
            errors.append("missing/invalid body " + name)
    if not _number(value.get("yaw")) or not _text(value.get("pose")):
        errors.append("missing body yaw/pose")
    if not _number(value.get("support_fraction")) or not 0 <= value["support_fraction"] <= 1:
        errors.append("invalid body support")
    if type(value.get("on_ground")) is not bool:
        errors.append("missing body ground fact")
    return errors


def _input_errors(inputs: object) -> list[str]:
    if type(inputs) is not list or not inputs:
        return ["empty/missing exact inputs"]
    errors = []
    for command in inputs:
        if type(command) is not dict or not _number(command.get("yaw")):
            errors.append("missing command/yaw")
            continue
        movement = command.get("movement", {})
        if (type(movement) is not dict or not _number(movement.get("forward"))
                or not -1 <= movement["forward"] <= 1
                or type(movement.get("jump")) is not bool):
            errors.append("missing movement projection")
    return errors


def _fact_errors(facts: object) -> list[str]:
    if type(facts) is not list or not facts:
        return ["empty snapshot/dependency facts"]
    errors, positions = [], set()
    for fact in facts:
        if type(fact) is not dict:
            errors.append("fact must be an object")
            continue
        position = fact.get("position")
        if type(position) is not list or len(position) != 3 or any(type(part) is not int for part in position):
            errors.append("invalid fact position")
            continue
        key = tuple(position)
        if key in positions:
            errors.append("duplicate fact position")
        positions.add(key)
        if not _integer(fact.get("version")) or fact.get("knowledge") not in ("UNKNOWN", "AIR", "BLOCK"):
            errors.append("missing fact version/knowledge")
        if fact.get("knowledge") == "BLOCK" and (not _text(fact.get("material")) or not fact.get("geometry")):
            errors.append("block fact lacks material/geometry")
        geometry = fact.get("geometry", [])
        if type(geometry) is not list or any(type(box) is not list or len(box) != 6 or not all(_number(part) for part in box) for box in geometry):
            errors.append("fact geometry is not canonical collision boxes")
        if fact.get("knowledge") == "UNKNOWN" and (fact.get("geometry") or fact.get("material")):
            errors.append("UNKNOWN was filled with known geometry")
    return errors


def _trajectory_errors(states: object, *, session: str, ruleset: dict) -> list[str]:
    if type(states) is not list or len(states) < 2:
        return ["missing complete state trajectory"]
    errors = []
    ticks = []
    for state in states:
        errors.extend(_body_errors(state))
        tick = state.get("movement_tick") if isinstance(state, dict) else None
        if not _integer(tick):
            errors.append("trajectory lacks actual movement tick")
        else:
            ticks.append(tick)
        physics = state.get("physics_state") if isinstance(state, dict) else None
        if type(physics) is not dict or not physics:
            errors.append("trajectory lacks the complete native PhysicsState")
        else:
            from mc2p.motion_nav.physics_types import PhysicsEffect, PhysicsState
            from mc2p.motion_nav.world_model import WorldSessionId
            try:
                native = dict(physics)
                native["session"] = WorldSessionId(native["session"]["value"])
                for name in ("position", "velocity_blocks_per_tick"):
                    native[name] = tuple(native[name])
                native["status_effects"] = tuple(PhysicsEffect(**effect) for effect in native["status_effects"])
                checked = PhysicsState(**native)
                if (checked.session.value != session or checked.ruleset_id != ruleset.get("ruleset_id")
                        or checked.state_schema != ruleset.get("state_schema")):
                    errors.append("trajectory state belongs to another session/ruleset/schema")
                pairs = ((checked.position, state.get("position")), (checked.velocity_blocks_per_tick, state.get("velocity")))
                if (any(list(first) != second for first, second in pairs)
                        or checked.yaw_radians != state.get("yaw") or checked.pose != state.get("pose")
                        or checked.on_ground != state.get("on_ground") or checked.movement_tick_id != tick):
                    errors.append("trajectory public body view differs from complete physics state")
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                errors.append("incomplete/invalid native physics state: " + str(error))
    if len(ticks) == len(states) and any(second != first + 1 for first, second in zip(ticks, ticks[1:])):
        errors.append("trajectory ticks are not consecutive")
    return errors


def _payload_errors(raw: dict, case_id: str, *, stage: str, frozen: dict) -> list[str]:
    payloads = [raw.get(name) for name in ("input", "snapshot", "proof")]
    if any(type(value) is not dict or not value for value in payloads):
        return ["input/snapshot/proof must be nonempty objects"]
    request, snapshot, proof = payloads
    errors = []
    if stage == "G0":
        method = case_id.rsplit(".", 1)[-1]
        if request.get("method") != method or not _text(request.get("request")) or not _text(request.get("work_identity")):
            errors.append("G0 input is not associated with its frozen case")
        if (not _text(snapshot.get("session")) or not _integer(snapshot.get("revision"))
                or not _text(snapshot.get("ruleset")) or not isinstance(snapshot.get("facts"), list)
                or not snapshot["facts"]):
            errors.append("G0 snapshot facts are absent")
        else:
            for fact in snapshot["facts"]:
                if (type(fact) is not list or len(fact) != 2 or type(fact[0]) is not list
                        or len(fact[0]) != 3 or any(type(part) is not int for part in fact[0])
                        or not _text(fact[1]) or not fact[1].startswith("CellFact(")):
                    errors.append("G0 snapshot contains no inspectable cell fact")
        if not _text(proof.get("value")) or not proof["value"].startswith("VerifiedMotionResult("):
            errors.append("G0 lacks its real solved proof representation")
        elif repr(snapshot.get("session")) not in proof["value"]:
            errors.append("G0 proof belongs to another snapshot session")
        if method == RED_METHODS[0]:
            if (not _text(request.get("expected_inputs")) or not _text(request.get("returned_inputs"))
                    or request["expected_inputs"] == request["returned_inputs"]
                    or raw.get("inputs_differ") is not True or not _text(proof.get("returned_value"))):
                errors.append("G0 parent input counterexample lost its independent inputs")
            elif ("commands=" + request["expected_inputs"] not in proof["value"]
                  or "commands=" + request["returned_inputs"] not in proof["returned_value"]):
                errors.append("G0 input declarations do not match their actual solved proofs")
        if method == RED_METHODS[1] and (not _text(raw.get("before_cell")) or not _text(raw.get("after_cell")) or raw["before_cell"] == raw["after_cell"]):
            errors.append("G0 immutable snapshot counterexample lost its actual mutation")
        return errors
    schemas = frozen["payload_schemas"]
    for name, value in zip(("request", "snapshot", "proof"), payloads):
        spec = schemas[name]
        if value.get("schema") != spec["schema"] or any(key not in value for key in spec["required"]):
            errors.append("missing canonical " + name + " schema fields")
    identity = request.get("identity", {})
    if type(identity) is not dict:
        return errors + ["missing request identity"]
    for name in schemas["identity_fields"]:
        valid = _stream_generation(identity.get(name)) if name == "stream_generation" else _text(identity.get(name))
        if not valid:
            errors.append("empty/invalid request identity: " + name)
    if proof.get("identity") != identity:
        errors.append("proof and parent request identities differ")
    if not _same_generation(proof.get("identity", {}).get("stream_generation"), identity.get("stream_generation")):
        errors.append("proof and parent stream generation values/types differ")
    generation = identity.get("stream_generation")
    for value, key in ((request, "native_request"), (proof, "native_proof")):
        if key in value:
            native = value[key]
            if (type(native) is not dict or type(native.get("stream")) is not dict
                    or not _same_generation(native["stream"].get("generation_id"), generation)):
                errors.append("native request/proof changes stream generation value/type")
    for key in ("native_registration", "native_proposal"):
        if key in raw:
            native = raw[key]
            if type(native) is not dict or not _same_generation(native.get("generation_id"), generation):
                errors.append("native handle/proposal changes stream generation value/type")
            elif key == "native_registration" and not _same_generation(
                    native.get("hot_view", {}).get("stream", {}).get("generation_id"), generation):
                errors.append("native handle stream changes generation value/type")
    scope = request.get("computation_scope", {})
    if (type(scope) is not dict or scope.get("world_session_id") != identity.get("session_identity")
            or not _text(scope.get("task_id")) or not _integer(scope.get("generation"))
            or not _text(request.get("owner_instance_id"))):
        errors.append("missing independent computation scope/owner")
    for name in ("computation_scope", "owner_instance_id", "request_budget"):
        if proof.get(name) != request.get(name):
            errors.append("proof and parent " + name + " differ")
    request_budget = request.get("request_budget", {})
    counts = proof.get("scan_counts", {})
    for name, limit in frozen["scan_budget"].items():
        value = request_budget.get(name) if isinstance(request_budget, dict) else None
        count = counts.get(name) if isinstance(counts, dict) else None
        if not _integer(value) or not 1 <= value <= limit or not _integer(count) or not 1 <= count <= value:
            errors.append("missing/exceeded proof work budget: " + name)
    errors.extend(_input_errors(request.get("expected_inputs")))
    if request.get("expected_inputs_sha256") != digest(request.get("expected_inputs")):
        errors.append("parent exact input digest mismatch")
    if proof.get("inputs") != request.get("expected_inputs") or proof.get("inputs_sha256") != request.get("expected_inputs_sha256"):
        errors.append("proof inputs do not match the independent parent")
    if not _integer(request.get("requested_start_tick")):
        errors.append("missing requested start tick")
    window = request.get("effect_window")
    if (type(window) is not list or len(window) != 2 or not all(_integer(tick) for tick in window)
            or window != [request.get("requested_start_tick"), request.get("requested_start_tick", -1) + 1]):
        errors.append("missing exact 0/1 start effect window")
    for name in ("requested_start_tick", "effect_window", "entry", "exit", "damage_budget"):
        if proof.get(name) != request.get(name):
            errors.append("proof and parent " + name + " differ")
    for name in ("entry", "exit"):
        errors.extend(_body_errors(request.get(name)))
    parent_body = request.get("entry")
    parent_entry = parent_body.get("physics_state") if type(parent_body) is dict else None
    if type(parent_entry) is not dict or not parent_entry:
        errors.append("parent request lacks the independent complete entry PhysicsState")
    for value, name in ((request, "native_request"), (proof, "native_proof")):
        native = value.get(name)
        if type(native) is dict:
            stream = native.get("stream", {})
            if type(stream) is not dict or stream.get("entry_identity") != [parent_entry]:
                errors.append("native request/proof stream entry differs from the independent parent PhysicsState")
            if name == "native_request" and native.get("entry_state") != parent_entry:
                errors.append("native request entry differs from the independent parent PhysicsState")
    budget = request.get("damage_budget", {})
    if type(budget) is not dict or not all(_number(budget.get(name)) and budget[name] >= 0 for name in ("max_damage", "remaining_damage")):
        errors.append("missing damage budget and remaining balance")
    elif budget["remaining_damage"] > budget["max_damage"]:
        errors.append("remaining damage exceeds task budget")
    world = request.get("world", {})
    if type(world) is not dict:
        errors.append("missing parent world binding")
        world = {}
    for name in ("world_session", "source_revision", "snapshot_revision", "ruleset"):
        if name.endswith("revision"):
            valid = _integer(snapshot.get(name))
        elif name == "ruleset":
            valid = type(snapshot.get(name)) is dict
        else:
            valid = _text(snapshot.get(name))
        if not valid or snapshot.get(name) != world.get(name):
            errors.append("snapshot origin mismatch: " + name)
    if identity.get("session_identity") != snapshot.get("world_session"):
        errors.append("request belongs to a different world session")
    coverage = snapshot.get("coverage")
    if (not _text(snapshot.get("lease_identity")) or type(coverage) is not list or len(coverage) != 6
            or any(type(part) is not int for part in coverage) or any(coverage[index] > coverage[index + 1] for index in (0, 2, 4))):
        errors.append("missing snapshot lease/coverage")
    if world.get("snapshot_sha256") != raw.get("snapshot_sha256") or proof.get("snapshot_sha256") != raw.get("snapshot_sha256"):
        errors.append("proof/request snapshot digest mismatch")
    if type(proof.get("ruleset")) is not dict or proof.get("ruleset") != world.get("ruleset") or proof.get("ruleset") != snapshot.get("ruleset"):
        errors.append("three-way ruleset mismatch")
    try:
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsRuleset
        if PhysicsRuleset(**snapshot.get("ruleset", {})) != JAVA_1_21_RULESET:
            errors.append("proof ruleset differs from the formal runtime ruleset")
    except (TypeError, ValueError) as error:
        errors.append("snapshot omits the complete ruleset: " + str(error))
    facts = snapshot.get("facts")
    errors.extend(_fact_errors(facts))
    if isinstance(facts, list) and type(coverage) is list and len(coverage) == 6 and all(type(part) is int for part in coverage):
        for fact in facts:
            position = fact.get("position") if isinstance(fact, dict) else None
            if type(position) is list and len(position) == 3 and all(type(part) is int for part in position):
                if any(not coverage[index * 2] <= coordinate <= coverage[index * 2 + 1] for index, coordinate in enumerate(position)):
                    errors.append("snapshot fact lies outside declared coverage")
    dependencies = proof.get("dependency_facts")
    errors.extend(_fact_errors(dependencies))
    errors.extend(_fact_errors(request.get("inherited_dependencies")))
    if isinstance(facts, list) and isinstance(dependencies, list):
        if any(fact not in facts or fact.get("knowledge") == "UNKNOWN" for fact in dependencies if isinstance(fact, dict)):
            errors.append("proof dependency not backed by observed snapshot facts")
        if any(fact not in dependencies for fact in request.get("inherited_dependencies", [])):
            errors.append("proof drops an inherited parent dependency")
    origin = {"session": identity.get("session_identity"), "ruleset": snapshot.get("ruleset", {})}
    errors.extend(_trajectory_errors(proof.get("trajectory"), **origin))
    branches = proof.get("branches")
    if type(branches) is not list or len(branches) != 2 or {branch.get("start_offset") for branch in branches if isinstance(branch, dict)} != {0, 1}:
        return errors + ["proof lacks both exact start branches"]
    for branch_index, branch in enumerate(branches):
        trajectory = branch.get("trajectory")
        errors.extend(_trajectory_errors(trajectory, **origin))
        tails = branch.get("safe_tails")
        if type(tails) is not list or not tails:
            errors.append("proof branch lacks complete bounded safe tails")
            continue
        count = len(trajectory) if isinstance(trajectory, list) else 0
        if type(branch.get("start_offset")) is not int or count < 2 or trajectory[0].get("movement_tick") != request["requested_start_tick"] - 1 + branch["start_offset"]:
            errors.append("proof branch uses a different absolute start tick")
        entry = branch.get("entry")
        if (type(entry) is not dict or not entry.get("physics_state")
                or count < 2 or entry != trajectory[0]):
            errors.append("proof branch first native state differs from its recorded entry")
        native = proof.get("native_proof")
        if type(native) is dict:
            native_branches = native.get("branches", [])
            if (branch_index >= len(native_branches) or not native_branches[branch_index].get("trajectory")
                    or entry.get("physics_state") != native_branches[branch_index]["trajectory"][0]):
                errors.append("branch entry differs from its recorded native complete PhysicsState")
        if branch.get("start_offset") == 0 and trajectory != proof.get("trajectory"):
            errors.append("main trajectory differs from its on-time branch")
        if branch.get("start_offset") == 0 and entry != request.get("entry"):
            errors.append("on-time branch entry differs from the independent parent complete PhysicsState")
        if {tail.get("boundary_index") for tail in tails if isinstance(tail, dict)} != set(range(count)):
            errors.append("proof has no tail for each declared boundary")
        if count > frozen["scan_budget"]["trajectory_ticks"] + 1:
            errors.append("proof trajectory exceeds frozen tick budget")
        for tail in tails:
            if type(tail) is not dict or not _text(tail.get("tail_id")) or tail.get("termination") not in ("SAFE_STOP", "SAFE_LANDING"):
                errors.append("tail lacks identity and safe termination")
                continue
            errors.extend(_input_errors(tail.get("inputs")))
            tail_trajectory = tail.get("trajectory")
            errors.extend(_trajectory_errors(tail_trajectory, **origin))
            boundary = tail.get("boundary_index")
            if type(boundary) is not int or not 0 <= boundary < count:
                errors.append("safe-tail boundary index is not a valid integer")
            elif (not isinstance(tail_trajectory, list) or not tail_trajectory
                    or tail_trajectory[0] != trajectory[boundary]):
                errors.append("safe-tail first complete state/tick differs from its branch boundary")
            if type(native) is dict and type(boundary) is int:
                native_tails = (native_branches[branch_index].get("safety_tails", [])
                                if branch_index < len(native_branches) else [])
                recorded = next((item for item in native_tails if item.get("boundary_index") == boundary), None)
                if (recorded is None or not recorded.get("trajectory") or not tail_trajectory
                        or recorded["trajectory"][0] != tail_trajectory[0].get("physics_state")):
                    errors.append("native safe-tail first complete state differs from its recorded branch boundary")
            if not isinstance(tail.get("dependency_facts"), list) or not tail["dependency_facts"] or any(fact not in dependencies for fact in tail["dependency_facts"]):
                errors.append("tail dependencies are not covered by proof")
            if isinstance(tail.get("trajectory"), list) and len(tail["trajectory"]) > frozen["scan_budget"]["tail_ticks_per_boundary"] + 1:
                errors.append("tail exceeds frozen tick budget")
    return errors


def _command_errors(raw: dict, row: dict, document: dict, *, stage: str, frozen: dict) -> list[str]:
    command = raw.get("command")
    if not _text(command) or command != row.get("command"):
        return ["missing actual case command"]
    argv = raw.get("command_argv")
    if argv is None and stage == "G0":
        # Read-only compatibility with the original G0 run's Windows command.
        argv = [word.strip('"') for word in shlex.split(command, posix=False)]
    elif argv != row.get("command_argv"):
        return ["case argv index association mismatch"]
    if type(argv) is not list or not argv or not all(_text(value) for value in argv):
        return ["missing checkable case argv"]
    if Path(argv[0]).resolve() != Path(document["environment"].get("executable", "")).resolve():
        return ["case command uses a different recorded Python executable"]
    if "command_argv" in raw and subprocess.list2cmdline(argv) != command:
        return ["command text and argv differ"]
    if stage == "G0":
        if argv[1:] != ["-m", "unittest", raw["case_id"], "-v"]:
            return ["actual unittest command belongs to another case"]
    elif stage == "G1":
        producer = frozen["g1_producer_prefix"] + raw["case_id"]
        if not _text(producer) or raw.get("producer_id") != producer or row.get("producer_id") != producer:
            return ["producer is not the frozen case callable"]
        if (len(argv) < 3 or (ROOT / argv[1]).resolve() != Path(__file__).resolve()
                or "--record-contracts" not in argv or "--verify-red" in argv or "unittest" in argv):
            return ["G1 command is not a direct contract producer run"]
    elif not _text(raw.get("producer_id")) or raw.get("producer_id") != row.get("producer_id"):
        return ["missing actual indexed producer identity"]
    if type(raw.get("command_exit_code")) is not int or type(row.get("command_exit_code")) is not int:
        return ["actual exit code must be an integer"]
    return []


def _record_command_errors(document: dict, *, stage: str) -> list[str]:
    command = document.get("command")
    if not _text(command):
        return ["missing overall evidence command"]
    argv = document.get("command_argv")
    if argv is None and stage == "G0":
        argv = [word.strip('"') for word in shlex.split(command, posix=False)]
    elif type(argv) is not list or subprocess.list2cmdline(argv) != command:
        return ["missing/mismatched overall command argv"]
    if stage not in ("G0", "G1"):
        return [] if type(argv) is list and argv and all(_text(value) for value in argv) else ["missing actual stage command argv"]
    if (len(argv) < 5 or not all(_text(value) for value in argv)
            or (ROOT / argv[1]).resolve() != Path(__file__).resolve()
            or "--record-contracts" not in argv or "--output" not in argv):
        return ["overall command is not the explicit contract evidence runner"]
    if (stage == "G0") != ("--verify-red" in argv):
        return ["overall command runs a different evidence stage"]
    return []


def _g0_event_errors(raw: dict, case_id: str) -> list[str]:
    errors = []
    method = case_id.rsplit(".", 1)[-1]
    threads = raw.get("thread_events", [])
    for event in threads:
        if type(event) is not dict or event.get("operation") != "deserialize" or not _integer(event.get("thread_id")) or not _text(event.get("thread_name")):
            errors.append("G0 lacks its actual deserialize thread")
    if method == RED_METHODS[2]:
        control = raw.get("control_thread_id")
        if not _integer(control) or not threads or sum(event.get("thread_id") == control for event in threads) != raw.get("actual"):
            errors.append("G0 control drain no longer binds the real control thread")
    if method in RED_METHODS[3:]:
        events = raw.get("reference_events", [])
        if (len(events) != 2 or [event.get("operation") for event in events] != ["legacy_result_available", "job_retired"]
                or not _text(events[0].get("identity")) or events[0]["identity"] != events[1].get("identity")):
            errors.append("G0 lacks matching result-available/job-retired facts")
        else:
            from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
            text = raw.get("input", {}).get("work_identity", "")
            values = {}
            for name in ("world_session_id", "task_id", "owner_instance_id", "subject_id"):
                match = re.search(name + "='([^']+)'", text)
                if match:
                    values[name] = match.group(1)
            generation = re.search(r"generation=(\d+)", text)
            revision = re.search(r"revision=(\d+)", text)
            if len(values) != 4 or not generation or not revision:
                errors.append("G0 work identity is not inspectable")
            else:
                identity = AsyncWorkIdentity(AsyncComputationScope(values["world_session_id"], values["task_id"], int(generation[1])),
                    values["owner_instance_id"], AsyncWorkKind.MOTION_SOLVE, values["subject_id"], int(revision[1]))
                if events[0]["identity"] != identity.key:
                    errors.append("G0 reference events belong to a different work identity")
        expected_use = "executing" if method == RED_METHODS[3] else "safe-tail"
        if raw.get("requested_reference_use") != expected_use or raw.get("legacy_candidate_object_still_exists") is not True:
            errors.append("G0 loses its retained candidate/reference-use distinction")
        if not threads:
            errors.append("G0 retirement result has no receive-thread evidence")
        if method == RED_METHODS[4] and not _integer(raw.get("overdue_tick")):
            errors.append("G0 tail probe lost its declared overdue tick")
    return errors


def _activation_errors(raw: dict) -> list[str]:
    errors = []
    events = raw.get("activation_events", [])
    required = ("proof_token", "generation", "body_owner", "ledger_identity",
                "session_identity", "first_control_sequence", "requested_start_tick",
                "effect_window", "submission_tick", "composite_command_identity")
    for event in events:
        if not isinstance(event, dict) or any(key not in event for key in required):
            errors.append("incomplete Runtime activation binding")
            continue
        if event.get("operation") != "activate" or event.get("producer") != "Runtime" or event.get("preallocated") is not False:
            errors.append("activation must come from final Runtime arbitration")
        if type(event["first_control_sequence"]) is not int or event["first_control_sequence"] < 0:
            errors.append("activation requires the actual sequence")
        for field in ("requested_start_tick", "submission_tick"):
            if type(event[field]) is not int or event[field] < 0:
                errors.append("activation requires a real nonnegative " + field)
        window = event["effect_window"]
        if (not isinstance(window, list) or len(window) != 2
                or any(type(tick) is not int or tick < 0 for tick in window)
                or window[0] > window[1]):
            errors.append("activation requires a bounded effect window")
        if "actual_effect_tick" in event:
            errors.append("send activation cannot invent actual application tick")
        for field in ("proof_token", "body_owner", "ledger_identity", "session_identity", "composite_command_identity"):
            if not _text(event[field]):
                errors.append("activation identity is empty: " + field)
        if not _stream_generation(event["generation"]) or not _integer(event.get("timestamp_ns")):
            errors.append("activation needs typed generation and event time")
        request = raw.get("input", {})
        identity = request.get("identity", {})
        registration = raw.get("registration", {})
        runtime_command = raw.get("runtime_command", {})
        expected = {"proof_token": registration.get("proof_token"), "generation": identity.get("stream_generation"),
                    "body_owner": identity.get("body_owner"), "ledger_identity": identity.get("ledger_identity"),
                    "session_identity": identity.get("session_identity"),
                    "requested_start_tick": request.get("requested_start_tick"), "effect_window": request.get("effect_window"),
                    "composite_command_identity": runtime_command.get("command_identity"),
                    "first_control_sequence": runtime_command.get("first_control_sequence"),
                    "submission_tick": runtime_command.get("submission_tick")}
        if any(event[name] != value for name, value in expected.items()):
            errors.append("activation does not bind the independent request/registration/Runtime command")
        if not _same_generation(event["generation"], identity.get("stream_generation")):
            errors.append("activation changes stream generation value/type")
        if "native_binding" in event and (type(event["native_binding"]) is not dict
                or not _same_generation(event["native_binding"].get("generation_id"), identity.get("stream_generation"))):
            errors.append("native activation binding changes stream generation value/type")
        inputs = request.get("expected_inputs", [])
        if not inputs or runtime_command.get("motion_projection") != inputs[0]:
            errors.append("actual composite motion projection differs from proved input")
    for receipt in raw.get("first_receipt_events", []):
        if type(receipt) is not dict or receipt.get("operation") != "first_applied" or len(events) != 1:
            errors.append("unbound first receipt")
            continue
        activation = events[0]
        for name in ("proof_token", "generation", "body_owner", "ledger_identity", "session_identity", "composite_command_identity", "first_control_sequence"):
            if receipt.get(name) != activation.get(name):
                errors.append("first receipt identity differs from Runtime activation")
        if not _same_generation(receipt.get("generation"), activation.get("generation")):
            errors.append("first receipt changes stream generation value/type")
        if "native_application" in receipt and (type(receipt["native_application"]) is not dict
                or not _same_generation(receipt["native_application"].get("generation_id"), activation.get("generation"))):
            errors.append("native first receipt changes stream generation value/type")
        if not _integer(receipt.get("timestamp_ns")) or not _integer(activation.get("timestamp_ns")) or receipt["timestamp_ns"] < activation["timestamp_ns"]:
            errors.append("first receipt precedes Runtime activation")
        start_tick = activation.get("requested_start_tick")
        tick = receipt.get("actual_effect_tick")
        offset = receipt.get("surviving_start_offset")
        if (not _integer(start_tick) or not _integer(tick) or type(offset) is not int
                or offset not in (0, 1) or tick != start_tick + offset
                or receipt.get("pruned_start_offset") != 1 - offset
                or receipt.get("actual_sequence") != activation.get("first_control_sequence")):
            errors.append("first receipt does not immediately select the unique 0/1 branch")
    if raw.get("requires_activation") is True and not events:
        errors.append("missing activation association")
    return errors


def _event_errors(raw: dict, case_id: str, *, stage: str) -> list[str]:
    errors = []
    if stage == "G2":
        from tests.sim.d102_prepared_motion import validate_probe
        errors.extend(validate_probe(raw, check_performance=True))
    if type(raw.get("published")) is not bool or type(raw.get("requires_activation")) is not bool:
        errors.append("missing explicit publication/activation declaration")
    for event in raw.get("thread_events", []):
        if not isinstance(event, dict) or not event.get("operation") or type(event.get("thread_id")) is not int or not event.get("thread_name"):
            errors.append("incomplete actual thread event")
    published = raw.get("publication_events", [])
    consumed = raw.get("consumption_events", [])
    refs = raw.get("reference_events", [])
    # Every event is checked even when a producer flips its status switches.
    if published or consumed or refs:
        if not published or not consumed or not refs:
            errors.append("missing actual publish/consume/reference association")
        for event in published + consumed + refs:
            if not isinstance(event, dict) or not event.get("proof_token") or not event.get("operation") or type(event.get("timestamp_ns")) is not int:
                errors.append("unbound or untimed registry event")
        tokens = {event.get("proof_token") for event in published if isinstance(event, dict)}
        if len(tokens) != 1 or any(event.get("proof_token") not in tokens for event in consumed + refs if isinstance(event, dict)):
            errors.append("publication/consumption/reference proof token mismatch")
        if raw.get("requires_activation") is True and any(event.get("proof_token") not in tokens for event in raw.get("activation_events", []) if isinstance(event, dict)):
            errors.append("Runtime activation references a different registered proof")
        if published and consumed and all(type(event.get("timestamp_ns")) is int for event in published + consumed):
            if min(event["timestamp_ns"] for event in consumed) < max(event["timestamp_ns"] for event in published):
                errors.append("control consumption precedes actual publication")
    if raw.get("published") != bool(published) or raw.get("requires_activation") != bool(raw.get("activation_events")):
        errors.append("self-reported switches contradict actual indexed events")
    if raw.get("execution_started") is True and raw.get("requires_activation") is not True:
        errors.append("started execution lacks an activation requirement")
    if stage not in ("G0", "G1") and raw.get("group") not in ("representative_negative", "fallback"):
        if raw.get("requires_activation") is not True:
            errors.append("formal execution case lacks Runtime activation association")
    return errors


def _g1_semantic_errors(raw: dict, case_id: str, frozen: dict) -> list[str]:
    spec = frozen["g1_case_semantics"][case_id]
    errors = []
    if raw.get("exception_type") or raw.get("traceback"):
        errors.append("direct PASS producer still carries a top-level failure")
    request = raw["input"]
    identity = request.get("identity", {})
    registration = raw.get("registration", {})
    if (type(registration) is not dict or not _text(registration.get("proof_token"))
            or registration.get("proof_token") == registration.get("job_token")
            or registration.get("job_token") != identity.get("job_token")):
        errors.append("missing separate proof registration/job identity")
        registration = {}
    token = registration.get("proof_token")
    fields = {"proof_token": token, "generation": identity.get("stream_generation"),
              "body_owner": identity.get("body_owner"), "ledger_identity": identity.get("ledger_identity"),
              "session_identity": identity.get("session_identity")}
    event_fields = (*EVENT_FIELDS[1:], "contract_events", "rejection_events", "first_receipt_events",
                    "handoff_events", "deviation_events", "race_events", "tail_events", "close_events",
                    "command_terminal_events", "supervisor_events")
    for field in event_fields:
        events = raw.get(field, [])
        if type(events) is not list:
            errors.append("event field is not a list: " + field)
            continue
        for event in events:
            if type(event) is not dict or not _text(event.get("operation")) or not _integer(event.get("timestamp_ns")):
                errors.append("empty/untyped actual event: " + field)
                continue
            if any(event.get(name) != value or value is None for name, value in fields.items()):
                errors.append("event is not associated with request/registered proof: " + field)
            if not _same_generation(event.get("generation"), fields["generation"]):
                errors.append("event changes stream generation value/type: " + field)
            if field in ("publication_events", "consumption_events") and any(event.get(name + "_sha256") != raw.get(name + "_sha256") for name in ("input", "snapshot", "proof")):
                errors.append("publication/consumption is not bound to the complete payloads")
    contract = raw.get("contract_events", [])
    operations = {event.get("operation") for event in contract if isinstance(event, dict)}
    if not set(spec["operations"]).issubset(operations):
        errors.append("missing frozen case-specific semantic operations")
    for event in contract:
        if type(event) is not dict:
            continue
        if (event.get("input_sha256") != raw.get("input_sha256")
                or event.get("snapshot_sha256") != raw.get("snapshot_sha256")
                or event.get("proof_sha256") != raw.get("proof_sha256")):
            errors.append("case semantic operation is not bound to complete payloads")
        facts = event.get("facts")
        if type(facts) is not dict or not facts:
            errors.append("semantic operation has no actual facts")
            continue
        operation = event.get("operation")
        if operation == "immutable_graph_checked" and (facts.get("mutation_attempts") != facts.get("mutations_blocked") or not _integer(facts.get("mutation_attempts")) or facts["mutation_attempts"] < 1):
            errors.append("immutability case has no blocked mutation")
        if operation in ("parent_inputs_retained", "returned_inputs_compared") and (facts.get("parent_digest") != request.get("expected_inputs_sha256") or facts.get("returned_digest") != raw["proof"].get("inputs_sha256")):
            errors.append("input case compares worker values without independent parent inputs")
        if operation == "ruleset_three_way_checked" and facts.get("rulesets") != [request.get("world", {}).get("ruleset"), raw["snapshot"].get("ruleset"), raw["proof"].get("ruleset")]:
            errors.append("missing three-way ruleset comparison")
        if operation == "full_proof_checked" and (facts.get("branches_checked") != 2 or facts.get("safe_tails_checked") != sum(len(branch.get("safe_tails", [])) for branch in raw["proof"].get("branches", []))):
            errors.append("full proof check omits branches/safe tails")
        if operation == "dependency_facts_checked" and facts.get("facts_checked") != raw["proof"].get("dependency_facts"):
            errors.append("dependency case does not check actual proof facts")
        if operation == "single_receiver_checked" and facts.get("consumer_count") != 1:
            errors.append("result has no unique actual receiver")
        if operation == "cancel_publish_atomicity_checked" and facts.get("publications_after_cancel") != 0:
            errors.append("cancel/publish atomicity failed or not measured")
        if operation in ("snapshot_pinned", "lease_reference_released", "dependency_change_checked"):
            if facts.get("lease_identity") != raw["snapshot"].get("lease_identity") or facts.get("snapshot_sha256") != raw.get("snapshot_sha256"):
                errors.append("snapshot lease operation belongs to another version")
            if operation == "lease_reference_released" and facts.get("pinned_readable") is not True:
                errors.append("lease release destroys still-referenced facts")
            if operation == "dependency_change_checked" and (facts.get("qualification_invalidated") is not True or facts.get("body_owner_retained") is not True):
                errors.append("dependency change lacks qualification/owner separation")
    if spec.get("publish") and (not raw.get("publication_events") or not raw.get("consumption_events")):
        errors.append("frozen case requires actual publication and control consumption")
    if spec.get("activate") and not raw.get("activation_events"):
        errors.append("frozen case requires post-arbitration Runtime activation")
    for flag, field in (("first_receipt", "first_receipt_events"), ("handoff", "handoff_events"),
                        ("deviation", "deviation_events"), ("race", "race_events"), ("tail", "tail_events"), ("close", "close_events")):
        if spec.get(flag) and not raw.get(field):
            errors.append("frozen case has no actual " + field)
    rejection_events = raw.get("rejection_events", [])
    reasons = []
    for event in rejection_events:
        if type(event) is not dict:
            continue
        reasons.append(event.get("reason"))
        if (event.get("reason") not in frozen["typed_reject_reasons"] or event.get("outcome") != "REJECTED"
                or event.get("unauthorized_body_steps") != 0 or type(event.get("unauthorized_body_steps")) is not int
                or event.get("handle_published") is not False or "expected" not in event or "observed" not in event
                or event.get("expected") is None or event.get("observed") == event.get("expected")):
            errors.append("negative event lacks typed rejection and independent zero advancement")
    if set(reasons) != set(raw.get("typed_rejections", [])) or not set(spec.get("reject_reasons", [])).issubset(reasons):
        errors.append("typed rejection summary is not backed by frozen negative events")
    references = raw.get("reference_events", [])
    terminals = {event.get("event_id"): event for event in raw.get("command_terminal_events", []) if isinstance(event, dict)}
    supervisors = {event.get("event_id"): event for event in raw.get("supervisor_events", []) if isinstance(event, dict)}
    if len(terminals) != len(raw.get("command_terminal_events", [])) or len(supervisors) != len(raw.get("supervisor_events", [])):
        errors.append("terminal/supervisor event IDs are not unique")
    for event in terminals.values():
        if (not _text(event.get("event_id")) or event.get("operation") != "command_terminal"
                or event.get("producer") != "Runtime" or not _text(event.get("action_id"))
                or not _integer(event.get("sequence")) or not _integer(event.get("terminal_tick"))
                or event.get("status") not in ("APPLIED", "CANCELLED", "NOT_APPLIED")):
            errors.append("missing actual Runtime command terminal fact")
    for event in supervisors.values():
        ids = event.get("command_terminal_event_ids", [])
        if (not _text(event.get("event_id")) or event.get("operation") != "body_responsibility_released"
                or event.get("producer") != "ExecutionSupervisor" or not _text(event.get("action_id"))
                or event.get("use") not in ("executing", "safe-tail") or event.get("safe_boundary") is not True
                or type(ids) is not list or not ids or any(item not in terminals for item in ids)):
            errors.append("supervisor release has no linked actual command terminals")
            continue
        if any(terminals[item].get("action_id") != event.get("action_id") or terminals[item].get("timestamp_ns", math.inf) > event.get("timestamp_ns", -1) for item in ids):
            errors.append("supervisor release precedes or mismatches command terminals")
    active = set()
    uses = set()
    releases = set()
    retired_with_body_refs = False
    last_reference_time = -1
    for event in references:
        if type(event) is not dict:
            continue
        use, operation = event.get("use"), event.get("operation")
        reference_time = event.get("timestamp_ns")
        if not _integer(reference_time) or reference_time < last_reference_time:
            errors.append("reference events are not in actual chronological order")
        else:
            last_reference_time = reference_time
        if (use not in frozen["payload_schemas"]["reference_uses"]
                or operation not in frozen["payload_schemas"]["reference_operations"]
                or event.get("user_id") != identity.get("body_owner")):
            errors.append("reference lacks valid use, operation or actual user")
            continue
        uses.add(use)
        if operation == "retain":
            active.add(use)
        elif operation == "transfer":
            if event.get("from_use") not in active:
                errors.append("reference transfer has no retained source")
            active.discard(event.get("from_use"))
            active.add(use)
        elif operation == "job_retired":
            if event.get("job_token") != identity.get("job_token"):
                errors.append("retired computation belongs to another job")
            retired_with_body_refs = bool(active & {"executing", "safe-tail"})
            if active & {"executing", "safe-tail"} and event.get("execution_refs_retained") is not True:
                errors.append("computation retirement loses execution references")
        elif operation == "release":
            if use not in active or any(event.get(name) is not True for name in ("command_terminals_complete", "responsibility_resolved", "supervisor_confirmed")):
                errors.append("execution/tail release has no terminal/responsibility/supervisor proof")
            supervisor = supervisors.get(event.get("supervisor_event_id"), {})
            terminal_ids = event.get("command_terminal_event_ids", [])
            if (not supervisor or supervisor.get("use") != use or supervisor.get("action_id") != identity.get("action_id")
                    or type(terminal_ids) is not list or not terminal_ids or any(item not in terminals for item in terminal_ids)
                    or terminal_ids != supervisor.get("command_terminal_event_ids")
                    or supervisor.get("timestamp_ns", math.inf) > event.get("timestamp_ns", -1)):
                errors.append("reference release lacks independent terminal/supervisor associations")
            releases.add(use)
            active.discard(use)
        elif operation == "qualification_revoked":
            if use != "candidate":
                errors.append("candidate cancellation revoked a body reference")
            active.discard(use)
        elif operation == "responsibility_retained" and use not in active:
            errors.append("responsibility claim has no retained proof reference")
    if not set(spec.get("uses", [])).issubset(uses):
        errors.append("frozen case omits candidate/executing/safe-tail uses")
    if spec.get("release") and (not {"executing", "safe-tail"}.issubset(releases) or not retired_with_body_refs):
        errors.append("lifecycle lacks separate computation retirement and final body releases")
    for event in raw.get("handoff_events", []):
        if (event.get("operation") != "action_handoff" or not _text(event.get("old_action_id"))
                or event.get("old_action_id") == event.get("new_action_id")
                or event.get("new_action_id") != identity.get("action_id")
                or event.get("old_body_owner") != identity.get("body_owner")
                or any(event.get(name) is not True for name in ("safe_boundary", "command_terminals_complete", "supervisor_confirmed"))):
            errors.append("same-owner handoff lacks formal boundary/release evidence")
        supervisor = supervisors.get(event.get("supervisor_event_id"), {})
        if (not supervisor or supervisor.get("action_id") != event.get("old_action_id")
                or supervisor.get("command_terminal_event_ids") != event.get("command_terminal_event_ids")
                or supervisor.get("timestamp_ns", math.inf) > event.get("timestamp_ns", -1)
                or any(event.get("timestamp_ns", math.inf) > activation.get("timestamp_ns", -1) for activation in raw.get("activation_events", []))):
            errors.append("new activation/handoff precedes formal old-action release")
    for event in raw.get("deviation_events", []):
        if (event.get("operation") != "midstream_deviation" or event.get("kind") not in ("MISSING", "LATE", "CONTENT")
                or not _integer(event.get("movement_tick")) or event["movement_tick"] <= request.get("requested_start_tick", 0) + 1
                or event.get("start_branch_reselected") is not False or event.get("body_owner_retained") is not True
                or event.get("outcome") != "BOUNDED_DEVIATION"):
            errors.append("midstream deviation was reclassified as a new start branch")
    races = raw.get("race_events", [])
    if spec.get("race") and {event.get("ordering") for event in races if isinstance(event, dict)} != {"cancel_before_publish", "publish_before_cancel"}:
        errors.append("both actual cancel/publication orders are not present")
    for event in races:
        cancel, publish = event.get("cancel_timestamp_ns"), event.get("publish_timestamp_ns")
        if not _integer(cancel) or not _integer(publish) or event.get("unauthorized_body_steps") != 0:
            errors.append("race event lacks actual ordering/zero advancement")
        elif event.get("ordering") == "cancel_before_publish":
            if not cancel < publish or event.get("handle_published") is not False or event.get("outcome") != "CANCELLED":
                errors.append("cancel-before-publish allowed a handle")
        elif event.get("ordering") == "publish_before_cancel" and (not publish < cancel or event.get("candidate_revoked") is not True or event.get("current_body_owner_retained") is not True):
            errors.append("publish-before-cancel lost future eligibility/body distinction")
    tail_ids = {tail.get("tail_id") for branch in raw["proof"].get("branches", []) for tail in branch.get("safe_tails", [])}
    for event in raw.get("tail_events", []):
        if (event.get("operation") != "safe_tail_selected" or event.get("tail_id") not in tail_ids
                or not _integer(event.get("deadline_tick")) or not _integer(event.get("observed_tick"))
                or event["observed_tick"] <= event["deadline_tick"] or event.get("second_action_authorizations") != 0
                or event.get("use") != "safe-tail" or event.get("dependency_facts") not in [tail.get("dependency_facts") for branch in raw["proof"].get("branches", []) for tail in branch.get("safe_tails", [])]):
            errors.append("overdue did not consume an already registered valid finite tail")
    for event in raw.get("close_events", []):
        if (event.get("operation") != "service_closed_unresolved" or event.get("outcome") != "BODY_RESPONSIBILITY_UNRESOLVED"
                or event.get("responsibility_resolved") is not False or event.get("supervisor_confirmed") is not False
                or event.get("command_terminals_complete") is not False
                or any(event.get(name) is not True for name in ("worker_exited", "receiver_exited", "proof_readable", "snapshot_lease_pinned"))
                or not _integer(event.get("worker_exit_code")) or "safe-tail" not in active or releases or supervisors):
            errors.append("unresolved close releases body facts or invents a safe terminal")
    return errors


def _matrix_errors(rows: list[dict], frozen: dict, *, stage: str) -> list[str]:
    widths, starts = (2, 3, 4), (0, 1)
    directions, backlogs = ("south", "west", "north", "east"), (0, 1)
    axes = {
        ("G2", "fast"): (("platform_width", "start_offset", "entry_offset", "direction", "backlog"),
                         (widths, starts, (-.05, 0, .05), directions, backlogs)),
        ("G2", "realtime"): (("platform_width", "start_offset", "backlog", "repetition"),
                             (widths, starts, backlogs, tuple(range(1, 6)))),
        ("G3", "formal_python"): (("platform_width", "start_offset", "direction"), (widths, starts, directions)),
        ("F-TICK", "tick_order"): (("direction", "repetition"), (directions, tuple(range(1, 11)))),
        ("F-CONTINUITY", "positive"): (("platform_width", "direction", "start_offset"), (widths, directions, starts)),
        ("F-CONTINUITY", "fallback"): (("direction",), (directions,)),
    }
    errors = []
    for (matrix_stage, group), (names, values) in axes.items():
        if matrix_stage != stage:
            continue
        actual = [tuple(row.get("axes", {}).get(name) for name in names)
                  for row in rows if row.get("group") == group]
        expected = set(itertools.product(*values))
        if len(actual) != len(expected) or set(actual) != expected:
            errors.append("missing/duplicate frozen matrix coordinates: " + group)
    if stage == "G2":
        negative_ids = {row.get("negative_case_id") for row in rows if row.get("group") == "representative_negative"}
        if negative_ids != set(frozen["representative_negative_case_ids"]):
            errors.append("representative negative families incomplete")
    return errors


def verify_stage_report(document: dict, *, stage: str) -> tuple[str, ...]:
    """Validate only the run's explicit index, not logs or directory scans.

    Loaded dictionaries must include `_index_path`. G0 validates RED evidence,
    and does not certify that the five missing contracts are implemented.
    """
    errors = []
    if stage not in D102_STAGE_ORDER:
        return ("unknown stage",)
    if type(document) is not dict:
        return ("report must be an object",)
    if stage == "G2" and document.get("schema") == "d102-g2-run-v1":
        from tests.sim.d102_prepared_motion import verify_g2_directory
        path = Path(document.get("_index_path", "")).resolve()
        return verify_g2_directory(path.parent.parent)
    try:
        frozen = _load(MANIFEST_PATH)
        if document.get("schema") != "d102-stage-index-v1" or document.get("stage") != stage:
            errors.append("schema or stage mismatch")
        if document.get("historical_d101_m0") != "FAILED" or document.get("historical_d098_p3") != "FAILED":
            errors.append("historical failures cannot be promoted")
        accepted_manifest_hashes = {file_hash(MANIFEST_PATH)}
        if stage == "G0":
            accepted_manifest_hashes.update(frozen.get("g0_legacy_manifest_sha256", []))
        if document.get("manifest_sha256") not in accepted_manifest_hashes:
            errors.append("frozen acceptance manifest mismatch")
        index_path = Path(document.get("_index_path", "")).resolve()
        if not index_path.is_file():
            return tuple(errors + ["missing explicit index path"])
        on_disk = _load(index_path)
        if not document.get("run_id") or document.get("run_id") != on_disk.get("run_id"):
            errors.append("unique run identity mismatch")
        base = index_path.parent
        environment = document.get("environment", {})
        if environment.get("system") != "Windows" or not environment.get("python"):
            errors.append("formal platform must be Windows with Python recorded")
        if not document.get("command") or document.get("record_mode") != "explicit_unique_run":
            errors.append("missing explicit command or run isolation")
        errors.extend(_record_command_errors(document, stage=stage))
        source = document.get("source", {})
        if stage == "G0":
            if file_hash(G0_PREDECESSOR) != G0_PREDECESSOR_SHA256:
                errors.append("signed historical G0 index bytes changed")
            original = _load(G0_PREDECESSOR)
            if (source.get("commit") != G0_SOURCE_COMMIT
                    or source.get("files") != original["source"]["files"]):
                errors.append("G0 signed source commit/files changed")
            expected_raw = {row["case_id"]: row["raw_sha256"] for row in original["cases"]}
            if any(row.get("raw_sha256") != expected_raw.get(row.get("case_id")) for row in document.get("cases", [])):
                errors.append("G0 signed raw evidence bytes changed")
        if not source.get("commit") or not source.get("files"):
            errors.append("missing source provenance")
        for name, expected in source.get("files", {}).items():
            archived = source.get("archives", {}).get(name)
            if file_hash(_relative_file(base, archived)) != expected:
                errors.append("source hash mismatch: " + name)
        required_sources = G1_SOURCE_FILES if stage == "G1" else SOURCE_FILES
        if not set(required_sources).issubset(source.get("files", {})):
            errors.append("incomplete probe source provenance")
        for historical in frozen["historical_evidence"]:
            historical_path = _relative_file(ROOT, historical["path"])
            if file_hash(historical_path) != historical["sha256"]:
                errors.append("historical evidence bytes changed: " + historical["path"])
            historical_document = _load(historical_path)
            if "continuous-motion-d101" in historical["path"] and historical_document.get("stages", {}).get("M0", {}).get("passed") is not False:
                errors.append("D101 M0 historical failure missing")
            if "incremental-motion-r98" in historical["path"] and historical_document.get("stage_status") != "failed_at_p3":
                errors.append("D098 P3 historical failure missing")
        if stage != "G0":
            previous = D102_STAGE_ORDER[D102_STAGE_ORDER.index(stage) - 1]
            predecessor = document.get("predecessor", {})
            predecessor_path = _relative_file(base, predecessor.get("path"))
            if file_hash(predecessor_path) != predecessor.get("sha256"):
                errors.append("predecessor hash mismatch")
            previous_document = _load(predecessor_path)
            previous_document["_index_path"] = str(predecessor_path)
            if verify_stage_report(previous_document, stage=previous):
                errors.append("previous stage has not passed evidence validation")
        rows = document.get("cases")
        if not isinstance(rows, list) or not rows:
            return tuple(errors + ["missing indexed raw cases"])
        ids = [row.get("case_id") for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or len(set(ids)) != len(ids):
            errors.append("case IDs must be complete and unique")
        expected_ids = frozen["g0_red_case_ids"] if stage == "G0" else frozen["g1_case_ids"] if stage == "G1" else None
        if expected_ids is not None and set(ids) != set(expected_ids):
            errors.append("frozen contract denominator mismatch")
        if document.get("case_count") != len(rows):
            errors.append("declared case count mismatch")
        typed_rejections = set()
        duration_samples = []
        for row in rows:
            label = row.get("case_id", "<missing>")
            try:
                raw_path = _relative_file(base, row.get("raw_path"))
                if file_hash(raw_path) != row.get("raw_sha256"):
                    raise ValueError("raw hash mismatch")
                raw = _load(raw_path)
                if raw.get("schema") != "d102-contract-raw-v1" or raw.get("case_id") != label or raw.get("run_id") != document.get("run_id"):
                    raise ValueError("raw schema, case or run association mismatch")
                payload_errors = _payload_errors(raw, label, stage=stage, frozen=frozen)
                errors.extend(label + ": " + error for error in payload_errors)
                for key in ("input", "snapshot", "proof"):
                    if key not in raw or raw.get(key + "_sha256") != digest(raw[key]) or row.get(key + "_sha256") != raw.get(key + "_sha256"):
                        errors.append(label + ": missing/mismatched " + key + " hash")
                for key in EVENT_FIELDS:
                    if not isinstance(raw.get(key), list) or row.get(key) != raw.get(key):
                        errors.append(label + ": missing/mismatched " + key)
                if (raw.get("command") != row.get("command")
                        or raw.get("record_command") != document.get("command")
                        or row.get("command_exit_code") != raw.get("command_exit_code")):
                    errors.append(label + ": command association mismatch")
                errors.extend(label + ": " + error for error in _command_errors(raw, row, document, stage=stage, frozen=frozen))
                if raw.get("unauthorized_body_steps") != 0:
                    errors.append(label + ": unauthorized body advancement")
                if stage not in ("G0", "G1") and row.get("group") != raw.get("group"):
                    errors.append(label + ": raw group association mismatch")
                if stage == "G0":
                    errors.extend(label + ": " + error for error in _g0_event_errors(raw, label))
                    if (row.get("status") != "RED" or raw.get("status") != "RED"
                            or raw.get("exception_type") != "AssertionError"
                            or raw.get("command_exit_code") != 1
                            or not raw.get("traceback") or not raw.get("failure_reason")
                            or raw.get("actual") == raw.get("expected")):
                        errors.append(label + ": missing real behavior RED")
                    if raw.get("activation_events") or raw.get("publication_events") or raw.get("consumption_events"):
                        errors.append(label + ": G0 cannot claim prepared execution")
                else:
                    if row.get("status") != "PASS" or raw.get("status") != "PASS" or raw.get("command_exit_code") != 0:
                        errors.append(label + ": direct contract did not pass")
                    if not raw.get("thread_events"):
                        errors.append(label + ": missing actual execution thread events")
                    typed_rejections.update(raw.get("typed_rejections", []))
                    errors.extend(label + ": " + error for error in _event_errors(raw, label, stage=stage))
                    if not payload_errors:
                        errors.extend(label + ": " + error for error in _activation_errors(raw))
                        if stage == "G1":
                            errors.extend(label + ": " + error for error in _g1_semantic_errors(raw, label, frozen))
                    durations = raw.get("decision_durations_ns", [])
                    if any(type(value) is not int or value < 0 for value in durations):
                        errors.append(label + ": invalid complete decision durations")
                    duration_samples.extend(durations)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                errors.append(label + ": " + str(error))
        if stage == "G1" and typed_rejections != set(frozen["typed_reject_reasons"]):
            errors.append("G1 typed rejection subevents incomplete or unknown")
        if stage not in ("G0", "G1"):
            counts = document.get("groups", {})
            expected_counts = frozen["stage_denominators"][stage]
            if set(counts) != set(expected_counts):
                errors.append("stage group matrix mismatch")
            for group, expected in expected_counts.items():
                indexed = sum(row.get("group") == group for row in rows)
                if counts.get(group) != expected or indexed != expected:
                    errors.append("frozen denominator mismatch: " + group)
            errors.extend(_matrix_errors(rows, frozen, stage=stage))
            high_water = document.get("resource_high_water", {})
            for name, bound in frozen["capacities"].items():
                if type(bound) is int:
                    value = high_water.get(name)
                    if type(value) is not int or not 0 <= value <= bound:
                        errors.append("missing/exceeded measured resource bound: " + name)
            if document.get("responsibility_reference_evictions") != 0:
                errors.append("responsibility references were evicted or not measured")
            if not duration_samples:
                errors.append("missing raw complete control decision timing")
            else:
                values = sorted(duration_samples)
                for percentile, bound in ((.95, 8_000_000), (.99, 15_000_000)):
                    if values[math.ceil(len(values) * percentile) - 1] > bound:
                        errors.append("complete control decision percentile exceeded")
                if max(values) >= 30_000_000:
                    errors.append("complete control decision maximum exceeded")
            if document.get("hot_path_forbidden_calls") != 0 or document.get("safety_violations") != 0:
                errors.append("hot path or safety gate failed/missing")
            if document.get("unresolved_body_responsibility") != 0:
                errors.append("unresolved body responsibility cannot count as pass")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        errors.append("missing/malformed evidence: " + str(error))
    return tuple(errors)


def record_contracts(output: Path, *, verify_red: bool) -> Path:
    """Only explicit unique output runs write persistent contract evidence."""
    if verify_red:
        raise ValueError("historical G0 RED evidence is read only; use --verify-red without --record-contracts")
    return _record_g1_contracts(output)


def _copy_g0_predecessor(output: Path) -> dict:
    """Copy only the signed index's explicit files, keeping their original bytes."""
    if file_hash(G0_PREDECESSOR) != G0_PREDECESSOR_SHA256:
        raise ValueError("signed G0 predecessor bytes changed")
    document = _load(G0_PREDECESSOR)
    document["_index_path"] = str(G0_PREDECESSOR)
    if verify_stage_report(document, stage="G0"):
        raise ValueError("signed G0 predecessor evidence failed validation")
    paths = {row["raw_path"] for row in document["cases"]}
    paths.update(document["source"]["archives"].values())
    destination = output / "predecessor/g0"
    for relative in ("index.json", *sorted(paths)):
        source = _relative_file(G0_PREDECESSOR.parent, relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(source.read_bytes())
    return {"path": "predecessor/g0/index.json", "sha256": G0_PREDECESSOR_SHA256}


def _record_g1_contracts(output: Path) -> Path:
    from tests.motion_nav import d102_contract_cases as producers
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = _load(MANIFEST_PATH)
    argv = [sys.executable, "scripts/verify_prepared_motion_d102.py", "--record-contracts", "--output", str(output)]
    command = subprocess.list2cmdline(argv)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex
    index = dict(schema="d102-stage-index-v1", stage="G1", run_id=run_id,
        created_utc=datetime.now(timezone.utc).isoformat(), record_mode="explicit_unique_run",
        command=command, command_argv=argv, manifest_sha256=file_hash(MANIFEST_PATH),
        historical_d101_m0="FAILED", historical_d098_p3="FAILED",
        environment=dict(system=platform.system(), platform=platform.platform(),
                         python=platform.python_version(), executable=sys.executable),
        source=dict(commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    working_tree_status=subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
                    measurement_uses_archived_bytes=True, files={}, archives={}),
        predecessor=_copy_g0_predecessor(output), case_count=len(frozen["g1_case_ids"]), cases=[])
    for name in G1_SOURCE_FILES:
        source = ROOT / name
        archive = output / "source" / name
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("xb") as stream:
            stream.write(source.read_bytes())
        index["source"]["files"][name] = file_hash(archive)
        index["source"]["archives"][name] = archive.relative_to(output).as_posix()
    for case_id in frozen["g1_case_ids"]:
        producer = getattr(producers, case_id)
        try:
            facts = producer()
        except Exception as error:
            # Retain failed runs as failed raw evidence, never fabricate PASS.
            facts = dict(status="ERROR", exception_type=type(error).__name__, traceback=traceback.format_exc(),
                         unauthorized_body_steps=0, **{field: [] for field in EVENT_FIELDS})
        facts.update(run_id=run_id, command=command, command_argv=argv, record_command=command,
                     producer_id=frozen["g1_producer_prefix"] + case_id,
                     command_exit_code=0 if facts["status"] == "PASS" else 1)
        raw_path = write_contract_evidence(case_id, facts, output=output / "raw")
        row = dict(case_id=case_id, raw_path=raw_path.relative_to(output).as_posix(), raw_sha256=file_hash(raw_path),
                   status=facts["status"], command=command, command_argv=argv, producer_id=facts["producer_id"],
                   command_exit_code=facts["command_exit_code"])
        for key in (*EVENT_FIELDS, "input_sha256", "snapshot_sha256", "proof_sha256"):
            row[key] = facts.get(key)
        index["cases"].append(row)
    path = output / "index.json"
    with path.open("xb") as stream:
        stream.write(canonical_bytes(index))
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=D102_STAGE_ORDER)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--verify-red", action="store_true")
    parser.add_argument("--record-contracts", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.record_contracts:
            if args.output is None or args.stage is not None or args.input is not None:
                raise ValueError("recording requires --output and excludes --stage/--input")
            path = record_contracts(args.output, verify_red=args.verify_red)
            stage = "G0" if args.verify_red else "G1"
        else:
            if args.verify_red and args.stage is None:
                args.stage = "G0"
                args.input = args.input or G0_PREDECESSOR
            if args.input is None or args.stage is None or args.output is not None:
                raise ValueError("verification requires --stage and --input")
            path, stage = args.input.resolve(), args.stage
        if stage == "G2" and path.is_dir():
            from tests.sim.d102_prepared_motion import verify_g2_directory
            errors = verify_g2_directory(path)
            if errors:
                print("FAIL: " + "\nFAIL: ".join(errors))
                return 1
            print("PASS evidence G2: four indexed Windows stages verified")
            return 0
        document = _load(path)
        document["_index_path"] = str(path)
        errors = verify_stage_report(document, stage=stage)
        if stage == "G0" and not args.verify_red:
            errors += ("G0 is RED evidence; explicit --verify-red required",)
        if errors:
            print("FAIL: " + "\nFAIL: ".join(errors))
            return 1
        print(f"PASS evidence {stage}: {document['case_count']} indexed cases; "
              + ("5 RED retained; stage implementation not passed" if stage == "G0" else "direct evidence verified"))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("FAIL: " + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
