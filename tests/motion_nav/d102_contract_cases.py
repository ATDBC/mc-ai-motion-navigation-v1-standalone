"""Direct D102 contract probes against existing formal ports; no production shim.

These probes intentionally describe missing D102 behavior. A retained Python
candidate is not evidence of a registered execution reference. G0 records that
distinction without claiming the legacy execution path is unsafe.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import pickle
import threading
import unittest
from unittest.mock import patch
from dataclasses import asdict

from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion, SolveStatus
from mc2p.motion_nav.motion_worker import GapMotionSolveJob, MotionResultInbox, _execute_job
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.world_model import UNKNOWN_CELL
from tests.motion_nav.test_b10_gap_solver import fixture


RED_METHODS = (
    "test_parent_inputs_are_independent", "test_snapshot_graph_is_immutable",
    "test_control_drain_is_not_receiver", "test_job_retirement_preserves_execution_refs",
    "test_overdue_keeps_registered_tail",
)
RED_CASE_IDS = tuple("tests.motion_nav.test_d102_contract_red.ContractRedTests." + name
                     for name in RED_METHODS)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_contract_evidence(case_id: str, facts: dict, *, output: Path) -> Path:
    """Caller chooses an isolated directory; never overwrite a raw case."""
    output.mkdir(parents=True, exist_ok=True)
    path = output / (case_id.rsplit(".", 1)[-1] + ".json")
    with path.open("xb") as stream:
        stream.write(canonical_bytes({"schema": "d102-contract-raw-v1",
                                     "case_id": case_id, **facts}))
    return path


def canonical_prepared_payloads(request, bundle, snapshot):
    """Represent actual native values without inventing states, branches or work."""
    from mc2p.motion_nav.geometry import query_support
    from mc2p.motion_nav.world_model import WorldView
    view = WorldView.from_sealed_snapshot(snapshot)
    versions = dict(snapshot.dependency_versions)
    def body(state):
        support = query_support(state.body_box, view)
        return dict(position=list(state.position), velocity=list(state.velocity_blocks_per_tick),
            yaw=state.yaw_radians, pose=state.pose, on_ground=state.on_ground,
            support_fraction=support.support_fraction, movement_tick=state.movement_tick_id,
            physics_state=asdict(state))
    def fact(position, value):
        block = value.block
        boxes = ([] if block is None else [[0., 0., 0., 1., 1., 1.]]
                 if block.collision_kind == "full_cube" else [list(box.as_tuple()) for box in block.boxes])
        return dict(position=list(position), version=versions[position], knowledge=value.knowledge.name,
                    material=None if block is None else block.material_key, geometry=boxes)
    def inputs(items):
        return [dict(movement=dict(forward=item.forward, strafe=item.strafe, jump=item.jump,
            sneak=item.sneak, sprint=item.sprint), yaw=item.movement_yaw_radians) for item in items]
    bounds = snapshot.identity.bounds
    snap = dict(schema="d102-snapshot-v1", world_session=snapshot.identity.world_session_id,
        source_revision=snapshot.identity.geometry_revision, snapshot_revision=snapshot.identity.local_revision,
        ruleset=asdict(snapshot.identity.ruleset), lease_identity=request.snapshot_lease_token,
        coverage=[int(bounds.min_x), int(bounds.max_x - 1), int(bounds.min_y), int(bounds.max_y - 1),
                  int(bounds.min_z), int(bounds.max_z - 1)],
        facts=[fact(position, value) for position, value in snapshot.facts])
    identity = dict(request_id=request.request_id, job_token=request.job_token,
        stream_generation=request.stream.generation_id, body_owner=request.stream.owner_id,
        route_id=request.stream.route_id, action_id=f"{request.stream.route_id}/{request.stream.action_index}",
        ledger_identity=request.ledger_id, session_identity=request.world_session_id)
    branches = []
    for branch in bundle.proof.branches:
        tails = []
        for tail in branch.safety_tails:
            tails.append(dict(tail_id=f"{branch.start_branch.value}/{tail.boundary_index}",
                boundary_index=tail.boundary_index, inputs=inputs(tail.inputs),
                trajectory=[body(state) for state in tail.trajectory],
                dependency_facts=[fact(position, value) for position, value in tail.dependency_facts],
                termination="SAFE_STOP" if tail.trajectory[0].on_ground else "SAFE_LANDING"))
        branches.append(dict(start_offset=int(branch.start_branch.value == "late_one"),
            entry=body(branch.trajectory[0]), first_effect_tick=branch.first_effect_tick,
            inputs=inputs(branch.inputs), trajectory=[body(state) for state in branch.trajectory], safe_tails=tails))
    budget = dict(physics_steps=request.scan_budget.maximum_physics_steps, trajectory_ticks=40,
                  start_branches=2, tail_ticks_per_boundary=80)
    counts = dict(physics_steps=bundle.proof.physics_steps,
        trajectory_ticks=len(bundle.inputs), start_branches=len(bundle.proof.branches),
        tail_ticks_per_boundary=max(len(tail.inputs) for branch in bundle.proof.branches for tail in branch.safety_tails))
    world = {name: snap[name] for name in ("world_session", "source_revision", "snapshot_revision", "ruleset")}
    world["snapshot_sha256"] = digest(snap)
    expected = inputs(request.expected_inputs)
    inp = dict(schema="d102-prepared-request-v1", identity=identity,
        computation_scope=asdict(request.async_identity.scope), owner_instance_id=request.async_identity.owner_instance_id,
        request_budget=budget, expected_inputs=expected, expected_inputs_sha256=digest(expected),
        requested_start_tick=request.requested_window.earliest_start_tick,
        effect_window=[request.requested_window.earliest_start_tick, request.requested_window.latest_start_tick],
        entry=body(request.entry_state), exit=body(request.exit_state),
        late_start_hold_input=inputs((request.stream.late_start_hold_input,))[0],
        damage_budget=dict(max_damage=request.damage_budget.maximum_expected_damage_points,
                           remaining_damage=request.damage_remaining_points), world=world,
        inherited_dependencies=[fact(position, snapshot.cell(position)) for position, _ in request.inherited_dependencies],
        native_request=asdict(request), native_input_digest=request.expected_input_digest)
    proof = dict(schema="d102-motion-proof-v1", identity=identity,
        computation_scope=inp["computation_scope"], owner_instance_id=inp["owner_instance_id"],
        request_budget=budget, scan_counts=counts, inputs=inputs(bundle.inputs), inputs_sha256=digest(inputs(bundle.inputs)),
        ruleset=asdict(bundle.ruleset), snapshot_sha256=digest(snap),
        requested_start_tick=inp["requested_start_tick"], effect_window=inp["effect_window"],
        entry=inp["entry"], exit=inp["exit"], damage_budget=inp["damage_budget"],
        dependency_facts=[fact(position, value) for position, value in bundle.proof.dependency_facts],
        trajectory=branches[0]["trajectory"], branches=branches,
        native_proof=asdict(bundle.proof), native_proof_digest=bundle.proof_digest,
        native_bundle_sha256=hashlib.sha256(bundle.sealed_bytes).hexdigest())
    return json.loads(canonical_bytes({"input": inp, "snapshot": snap, "proof": proof,
            "input_sha256": digest(inp), "snapshot_sha256": digest(snap), "proof_sha256": digest(proof)}))


@lru_cache(maxsize=2)
def _solved(direction=(0, 1)):
    anchor, world, target, _ = fixture(direction)
    identity = AsyncWorkIdentity(AsyncComputationScope(anchor.session.value, "d102-contract", 1),
                                 "d102-compute-owner", AsyncWorkKind.MOTION_SOLVE,
                                 "d102-route/action-0", 1)
    request = GapSolveRequest(direction, LandingRegion(*target), CandidateExecutionWindow(11, 12))
    result = _execute_job(GapMotionSolveJob(identity.subject_id, 1, anchor, world, request, identity))
    if result.solve_result.status is not SolveStatus.SOLVED:
        raise AssertionError("D102 probe requires a real solved legacy proof")
    return anchor, world, request, result


class RecordedPort:
    """A controlled worker port with real pickle receive work, no process leaks."""
    def __init__(self, result):
        self.payload = pickle.dumps(result)
        self.events = []

    def submit(self, job):
        return True

    def poll_available(self):
        if self.payload is None:
            return ()
        self.events.append({"operation": "deserialize", "thread_id": threading.get_ident(),
                            "thread_name": threading.current_thread().name})
        payload, self.payload = self.payload, None
        return (pickle.loads(payload),)

    def close(self):
        self.payload = None

    def is_alive(self):
        return True


def probe_contract(method: str) -> dict:
    if method not in RED_METHODS:
        raise ValueError("unknown frozen RED contract")
    anchor, world, request, result = _solved()
    identity = result.work_identity
    inbox = MotionResultInbox(max_results=2)
    inbox.register(identity)
    port = RecordedPort(result)
    snapshot = world.snapshot(PhysicsWorldBounds(-2, 2, 61, 68, -2, 4))
    facts = {
        "input": {"method": method, "request": repr(request), "work_identity": repr(identity)},
        "snapshot": {"session": world.session.value,
                     "revision": world.geometry_revision,
                     "ruleset": repr(world.ruleset),
                     "facts": [[list(position), repr(fact)] for position, fact in
                               sorted(snapshot._world._snapshot_facts.items())]},
        "proof": {"value": repr(result.solve_result.proof)},
        "thread_events": [], "activation_events": [], "reference_events": [],
        "publication_events": [], "consumption_events": [],
        "unauthorized_body_steps": 0,
        "legacy_port": "mc2p.motion_nav.motion_worker.MotionResultInbox",
    }
    if method == RED_METHODS[0]:
        # Two actual legal solver results: the existing receive boundary only
        # retains identity, not the parent's independent exact expected inputs.
        from dataclasses import replace
        _, _, _, alternate = _solved((1, 0))
        port = RecordedPort(replace(alternate, work_identity=identity))
        expected = result.solve_result.proof.commands
        inbox.drain_once(port, 1)
        received = inbox.peek(identity)
        facts["expected_inputs"] = repr(expected)
        facts["returned_inputs"] = repr(received.solve_result.proof.commands)
        facts["input"].update(expected_inputs=facts["expected_inputs"],
                              returned_inputs=facts["returned_inputs"])
        facts["proof"]["returned_value"] = repr(received.solve_result.proof)
        facts["inputs_differ"] = expected != received.solve_result.proof.commands
        facts["actual"] = received is not None
        facts["expected"] = False
        facts["failure_reason"] = "legacy inbox admits alternate inputs without parent input binding"
    elif method == RED_METHODS[1]:
        position = (0, 63, 0)
        before = snapshot._world.cell(position)
        blocked = False
        try:
            snapshot._world._snapshot_facts[position] = UNKNOWN_CELL
        except (TypeError, AttributeError):
            blocked = True
        facts["before_cell"] = repr(before)
        facts["after_cell"] = repr(snapshot._world.cell(position))
        facts["actual"] = blocked
        facts["expected"] = True
        facts["failure_reason"] = "detached frozen WorldView contains a mutable facts dictionary"
    else:
        inbox.drain_once(port, 1)
        facts["thread_events"] = port.events
        if method == RED_METHODS[2]:
            control_thread = threading.get_ident()
            facts["control_thread_id"] = control_thread
            facts["actual"] = sum(event["thread_id"] == control_thread for event in port.events)
            facts["expected"] = 0
            facts["failure_reason"] = "control drain synchronously receives and deserializes worker payload"
        else:
            retained_candidate = inbox.peek(identity)
            facts["reference_events"].append({"operation": "legacy_result_available",
                                                "identity": identity.key})
            inbox.retire(identity)
            facts["reference_events"].append({"operation": "job_retired", "identity": identity.key})
            facts["legacy_candidate_object_still_exists"] = retained_candidate is not None
            facts["actual"] = inbox.peek(identity) is not None
            facts["expected"] = True
            if method == RED_METHODS[3]:
                facts["requested_reference_use"] = "executing"
                facts["failure_reason"] = "job-only inbox has no separately registered execution reference after retirement"
            else:
                facts["requested_reference_use"] = "safe-tail"
                facts["overdue_tick"] = request.execution_window.latest_start_tick + 1
                facts["failure_reason"] = "overdue preparation retirement cannot query a registered safe-tail in job-only inbox"
    for key in ("input", "snapshot", "proof"):
        facts[key + "_sha256"] = digest(facts[key])
    return facts


def assert_contract(test: unittest.TestCase, facts: dict) -> None:
    test.assertEqual(facts["unauthorized_body_steps"], 0)
    if "inputs_differ" in facts:
        test.assertTrue(facts["inputs_differ"], "fixture did not change the inputs")
    test.assertEqual(facts["actual"], facts["expected"], facts["failure_reason"])


@lru_cache(maxsize=1)
def _activation_fixture_type():
    from tests.motion_nav.test_d102_runtime_activation import ActivationTests
    ActivationTests.setUpClass()
    return ActivationTests


def _observed_spawn_worker(test):
    """Borrow the formal process port; the test observes, never injects, results."""
    from mc2p.motion_nav.motion_worker import MotionSolverWorker
    worker = MotionSolverWorker()
    test.addCleanup(worker.close)
    worker.jobs, worker.consumer_threads = [], set()
    received, condition = set(), threading.Condition()
    submit, poll = worker.submit, worker.poll_available
    def key(result):
        return getattr(result, "job_token", None) or result.work_identity
    def observed_submit(job):
        admitted = submit(job)
        if admitted:
            worker.jobs.append(job)
        return admitted
    def observed_poll():
        worker.consumer_threads.add(threading.get_ident())
        values = poll()
        with condition:
            received.update(key(value) for value in values)
            condition.notify_all()
        return values
    def await_native_result(expected):
        with condition:
            test.assertTrue(condition.wait_for(lambda: key(expected) in received, timeout=10),
                            "formal spawn worker did not return its real result")
    worker.submit, worker.poll_available, worker.put = observed_submit, observed_poll, await_native_result
    return worker


def run_prepared_contract(case_id: str) -> dict:
    """Observe real existing owners; event facts are captured at their actual calls."""
    from contextlib import ExitStack
    from dataclasses import replace
    import time
    from unittest.mock import patch
    from mc2p.contracts.action_v1 import MovementV1, MovementTickWindowV1
    from mc2p.contracts.motion_activation import ActivationStatus, FirstStreamApplication, StreamApplicationStatus
    from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor
    from mc2p.motion_nav.prepared_motion import (
        PreparedRejectReason, ProofUse, ProofConsumerIdentity, ProofActionExitEvidence,
        ProofSupervisorReleaseEvidence, ProofReleaseEvidence, ReferenceStatus,
    )
    from mc2p.motion_nav.prepared_motion_validation import validate_prepared_message
    from mc2p.motion_nav.prepared_motion_proof import seal_bundle, prepare_fixed_motion, canonical_motion_digest
    from tests.motion_nav import test_d102_receiver as mutations
    frozen = json.loads((Path(__file__).resolve().parents[1] / "sim/manifests/prepared-motion-proof-d102-v1.json").read_text("utf-8"))
    if case_id not in frozen["g1_case_ids"]:
        raise ValueError("unknown frozen G1 producer")
    spec = frozen["g1_case_semantics"][case_id]
    test = _activation_fixture_type()("runTest")
    native_events = []
    # The ordinary retention fixture has a standalone Task 6 reference with
    # sequence 1000. G1 starts with the actual legacy controller/Runtime inputs
    # and does not create that unsent diagnostic reference.
    with patch.object(test.fixture_type, "prepared_incumbent", return_value=None):
        if spec.get("close"):
            native_worker = _observed_spawn_worker(test)
            with patch.object(mutations, "QueuedWorker", return_value=native_worker):
                test.setUp()
        else:
            test.setUp()
    try:
        with ExitStack() as stack:
            inbox, executor, supervisor = test.f.inbox, test.f.executor, test.f.supervisor
            publish = inbox._publish_prepared
            def published(record, *args):
                result = publish(record, *args)
                admission = inbox.prepared_admission(record.identity)
                if record.request.stream.action_index == 1 and admission is not None and admission.handle is not None:
                    native_events.append(("publish", time.perf_counter_ns(), admission.handle))
                return result
            stack.enter_context(patch.object(inbox, "_publish_prepared", side_effect=published))
            install = executor.install_prepared_motion
            def consumed(handle):
                result = install(handle)
                native_events.append(("consume", time.perf_counter_ns(), handle))
                return result
            stack.enter_context(patch.object(executor, "install_prepared_motion", side_effect=consumed))
            commit = supervisor.commit_activation
            def activated(transaction, accepted, delivered):
                result = commit(transaction, accepted, delivered)
                if result.status is ActivationStatus.ACTIVATED:
                    native_events.append(("activate", time.perf_counter_ns(), transaction.binding))
                return result
            stack.enter_context(patch.object(supervisor, "commit_activation", side_effect=activated))
            applied = supervisor.record_application
            def first_application(application):
                result = applied(application)
                if result.status is StreamApplicationStatus.APPLIED:
                    native_events.append(("application", time.perf_counter_ns(), (application, result)))
                return result
            stack.enter_context(patch.object(supervisor, "record_application", side_effect=first_application))
            test.arrive()
            handle = test.handle
            registration = inbox._registry[handle.proof_token]
            bundle, lease = registration.bundle, registration.lease
            parent = inbox._active[registration.identity]
            request = parent.request
            test.assertIsNot(request, bundle.request)
            test.assertEqual(request, bundle.request)
            test.assertEqual(parent.expected_inputs, bundle.inputs)
            test.assertEqual(parent.expected_digest, request.expected_input_digest)
            raw = canonical_prepared_payloads(request, bundle, lease.snapshot)
            identity = raw["input"]["identity"]
            fields = dict(proof_token=handle.proof_token, generation=handle.generation_id, body_owner=handle.owner_id,
                          ledger_identity=request.ledger_id, session_identity=request.world_session_id)
            hashes = {key: raw[key] for key in ("input_sha256", "snapshot_sha256", "proof_sha256")}
            def event(operation, timestamp=None, **extra):
                return dict(fields, operation=operation, timestamp_ns=time.perf_counter_ns() if timestamp is None else timestamp, **extra)
            raw.update(registration=dict(proof_token=handle.proof_token, job_token=request.job_token),
                native_registration=asdict(handle), native_proposal=asdict(test.proposal), unauthorized_body_steps=0,
                parent_input_reference_is_independent=request is not bundle.request,
                publication_events=[], consumption_events=[], activation_events=[], reference_events=[],
                thread_events=[], first_receipt_events=[], rejection_events=[], contract_events=[],
                handoff_events=[], deviation_events=[], race_events=[], tail_events=[], close_events=[],
                command_terminal_events=[], supervisor_events=[], typed_rejections=[])
            for operation, timestamp, value in native_events:
                if operation in ("publish", "consume") and value is handle:
                    raw["publication_events" if operation == "publish" else "consumption_events"].append(
                        event(operation, timestamp, **hashes))
            publication_time = raw["publication_events"][0]["timestamp_ns"]
            raw["reference_events"].append(event("retain", publication_time, use="candidate", user_id=handle.owner_id))
            for thread_id in test.f.worker.consumer_threads:
                raw["thread_events"].append(dict(operation="receive_validate_publish", thread_id=thread_id,
                    thread_name="mc2p-motion-receiver", timestamp_ns=publication_time))
            test.assertEqual(len(test.f.worker.consumer_threads), 1)
            current = inbox._current_facts
            baseline = seal_bundle(bundle)

            def reject(reason):
                changed, expected, observed = baseline, "authorized", "changed"
                changed_current = current
                if reason == "ACTIVATION":
                    before = len(test.backend.actions), executor.action_index
                    result = test.dispatch(proposal=replace(test.proposal, generation_id="rejected-generation",
                                                           movement_intent_id="rejected-activation"))
                    test.assertIsNot(result.stream_activation.status, ActivationStatus.ACTIVATED)
                    test.assertEqual((len(test.backend.actions), executor.action_index), before)
                    outcome = result.stream_activation
                    observed = outcome.reason.value
                else:
                    if reason == "MESSAGE":
                        changed = mutations.raw_message(bundle, payload_digest="0" * 64)
                    elif reason == "MUTABLE_GRAPH":
                        changed = mutations.raw_message(mutations.unchecked_replace(bundle, inputs=list(bundle.inputs)))
                    elif reason == "PROOF":
                        changed = mutations.raw_message(mutations.unchecked_replace(bundle, proof_digest="0" * 64))
                    elif reason == "RULESET":
                        changed = mutations.raw_message(mutations.reseal(mutations.unchecked_replace(bundle,
                            ruleset=replace(bundle.ruleset, ruleset_id="other-rules"))))
                    elif reason == "DEPENDENCY":
                        versions = current.dependency_versions
                        affected = bundle.proof.dependency_versions[0][0]
                        changed_current = replace(current, dependency_versions=tuple(
                            (position, version + int(position == affected)) for position, version in versions))
                    elif reason == "INPUTS":
                        from mc2p.motion_nav.motion_solver import MotionCommandTick
                        inputs = request.expected_inputs[:-1] + (replace(request.expected_inputs[-1], sprint=not request.expected_inputs[-1].sprint),)
                        commands = tuple(MotionCommandTick(MovementV1(forward=int(item.forward), strafe=int(item.strafe),
                            jump=item.jump, sneak=item.sneak, sprint=item.sprint), item.movement_yaw_radians) for item in inputs)
                        other = replace(request, stream=replace(request.stream, expected_inputs=inputs),
                            commands=commands, expected_inputs=inputs, expected_input_digest=canonical_motion_digest(inputs))
                        produced = prepare_fixed_motion(other, lease.snapshot)
                        test.assertIsNotNone(produced.bundle)
                        changed = seal_bundle(produced.bundle)
                    else:
                        changes = {
                            "REQUEST": dict(job_token="another-job"),
                            "GENERATION": dict(stream=replace(request.stream, generation_id="another-stream")),
                            "OWNER": dict(stream=replace(request.stream, owner_id="another-owner")),
                            "ROUTE_ACTION": dict(stream=replace(request.stream, route_id="another-route")),
                            "LEDGER": dict(ledger_id="another-ledger"),
                            "WORLD_SNAPSHOT": dict(snapshot_identity=replace(request.snapshot_identity, local_revision=request.snapshot_identity.local_revision + 1)),
                        }[reason]
                        other = mutations.unchecked_replace(request, **changes)
                        changed = mutations.raw_message(mutations.reseal(mutations.unchecked_replace(bundle, request=other)),
                                                        job_token=request.job_token)
                    outcome = validate_prepared_message(changed, request, lease.snapshot, changed_current)
                    test.assertIs(outcome.reason, PreparedRejectReason[reason])
                    test.assertIsNone(outcome.handle)
                    observed = outcome.reason.value
                raw["rejection_events"].append(event("reject", reason=reason, outcome="REJECTED",
                    native_outcome=asdict(outcome), unauthorized_body_steps=0, handle_published=False,
                    expected=expected, observed=observed))
                raw["typed_rejections"].append(reason)
            for reason in spec.get("reject_reasons", []):
                reject(reason)

            approval_time = time.perf_counter_ns()
            old_terminals = tuple(record for record in test.f.ledger.snapshot() if record.applied_ticks)
            if spec.get("handoff"):
                terminal_ids = []
                for record in old_terminals:
                    terminal_id = f"old-terminal/{record.control_sequence}"
                    terminal_ids.append(terminal_id)
                    raw["command_terminal_events"].append(event("command_terminal", producer="Runtime",
                        event_id=terminal_id, action_id=f"{handle.route_id}/0", sequence=record.control_sequence,
                        status="APPLIED", terminal_tick=record.resolved_at_tick, native_record=asdict(record)))
                approved = test.approval
                test.assertEqual(approved.status.value, "approved")
                supervisor_id = "approved-old-action/" + approved.approval_token
                raw["supervisor_events"].append(event("body_responsibility_released", producer="ExecutionSupervisor",
                    event_id=supervisor_id, action_id=f"{handle.route_id}/0", use="executing", safe_boundary=True,
                    command_terminal_event_ids=terminal_ids, native_approval=asdict(approved)))
                raw["handoff_events"].append(event("action_handoff", old_action_id=f"{handle.route_id}/0",
                    new_action_id=identity["action_id"], old_body_owner=handle.owner_id, safe_boundary=True,
                    command_terminals_complete=True, supervisor_confirmed=True, supervisor_event_id=supervisor_id,
                    command_terminal_event_ids=terminal_ids))
            if spec.get("activate"):
                if spec.get("first_receipt"):
                    test.backend.offset = 1
                result = test.dispatch()
                test.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
                binding = supervisor.active_activation
                activation_time = next(ts for op, ts, obj in native_events if op == "activate")
                command_id = digest(asdict(binding.compound_action))
                from mc2p.motion_nav.online_motion import project_movement_command
                import math
                actual_input = project_movement_command(request.entry_state, binding.compound_action.movement,
                    movement_yaw_radians=request.entry_state.yaw_radians + math.radians(binding.compound_action.look.yaw_delta_degrees)).tick_input
                projection = dict(movement=dict(forward=actual_input.forward, strafe=actual_input.strafe,
                    jump=actual_input.jump, sneak=actual_input.sneak, sprint=actual_input.sprint), yaw=actual_input.movement_yaw_radians)
                raw["runtime_command"] = dict(command_identity=command_id, first_control_sequence=binding.first_control_sequence,
                    submission_tick=binding.submitted_movement_tick, motion_projection=projection,
                    native_action=asdict(binding.compound_action))
                raw["activation_events"].append(event("activate", activation_time, producer="Runtime", preallocated=False,
                    first_control_sequence=binding.first_control_sequence, requested_start_tick=raw["input"]["requested_start_tick"],
                    effect_window=raw["input"]["effect_window"], submission_tick=binding.submitted_movement_tick,
                    composite_command_identity=command_id, native_binding=asdict(binding)))
                raw["reference_events"].extend([event("transfer", activation_time, from_use="candidate", use="executing", user_id=handle.owner_id),
                    event("retain", activation_time, use="safe-tail", user_id=handle.owner_id)])
                first_time, application, applied_result = next((ts, obj[0], obj[1]) for op, ts, obj in native_events if op == "application")
                offset = int(applied_result.start_branch.value == "late_one")
                raw["first_receipt_events"].append(event("first_applied", first_time, first_control_sequence=binding.first_control_sequence,
                    actual_sequence=application.control_sequence, composite_command_identity=command_id,
                    actual_effect_tick=application.effect_tick, surviving_start_offset=offset, pruned_start_offset=1-offset,
                    native_application=asdict(application)))
                inbox.retire(request.async_identity)
                test.assertTrue(inbox.usage_active(handle, ProofUse.EXECUTING))
                raw["reference_events"].append(event("job_retired", use="safe-tail", user_id=handle.owner_id,
                    job_token=request.job_token, execution_refs_retained=True))

            operation_facts = dict(parent_digest=raw["input"]["expected_inputs_sha256"], returned_digest=raw["proof"]["inputs_sha256"],
                rulesets=[raw["input"]["world"]["ruleset"], raw["snapshot"]["ruleset"], raw["proof"]["ruleset"]],
                branches_checked=len(bundle.proof.branches), safe_tails_checked=sum(len(b.safety_tails) for b in bundle.proof.branches),
                facts_checked=raw["proof"]["dependency_facts"], consumer_count=len(test.f.worker.consumer_threads),
                lease_identity=request.snapshot_lease_token, snapshot_sha256=raw["snapshot_sha256"],
                native_registered_digest=handle.registered_digest)
            if case_id == "contract_value_immutability":
                blocked = 0
                try:
                    bundle.inputs = ()
                except (AttributeError, TypeError):
                    blocked += 1
                test.assertEqual(blocked, 1)
                operation_facts.update(mutation_attempts=1, mutations_blocked=blocked)
            if case_id == "snapshot_version_lease":
                pinned = lease.snapshot
                test.assertTrue(inbox.release_candidate(handle))
                test.assertEqual(pinned.cell(pinned.facts[0][0]), pinned.facts[0][1])
                position = handle.dependency_versions[0][0]
                stamp = test.f.frame(test.f.current).body.stamp
                test.f.world.confirm_air(stamp, (position,))
                test.assertIsNone(test.f.control.prepared_action_proposal(test.approval))
                operation_facts.update(pinned_readable=True, qualification_invalidated=not inbox.has_registered_handle(handle),
                    body_owner_retained=test.f.control.owner_id == handle.owner_id)
                raw["reference_events"].append(event("qualification_revoked", use="candidate", user_id=handle.owner_id))
            if spec.get("deviation"):
                controller = executor.prepared_controller
                command = handle.hot_view.commands[1]
                receipt = FirstStreamApplication(handle.generation_id, 73, controller.stream_tracker.expected_effect_tick(1)+1,
                                                command.movement, command.required_movement_yaw_radians)
                deviated = controller.record_application(receipt, command_index=1)
                test.assertIs(deviated.status, StreamApplicationStatus.DEVIATION)
                raw["deviation_events"].append(event("midstream_deviation", kind="LATE", movement_tick=receipt.effect_tick,
                    start_branch_reselected=False, body_owner_retained=supervisor.route is test.f.control,
                    outcome="BOUNDED_DEVIATION", native_result=asdict(deviated), native_application=asdict(receipt)))
            if spec.get("race") or case_id == "receiver_single_consumer_atomic_publish":
                _record_cancel_orders(test, request, lease, raw, event)
                operation_facts["publications_after_cancel"] = sum(item["handle_published"] for item in raw["race_events"]
                    if item["ordering"] == "cancel_before_publish")
            if spec.get("tail"):
                controller = executor.prepared_controller
                command = handle.hot_view.commands[1]
                proposal = replace(test.proposal, movement_intent_id="tail-boundary-input",
                    expected_movement=command.movement, requested_window=MovementTickWindowV1(28, 28))
                test.dispatch(proposal=proposal)
                tail = next(tail for tail in controller.remaining_start_branches[0].safety_tails
                            if tail.trajectory[0] == test.backend.state)
                test.assertTrue(inbox.usage_active(handle, ProofUse.SAFE_TAIL))
                raw_tail = next(value for branch in raw["proof"]["branches"] for value in branch["safe_tails"]
                               if value["tail_id"] == f"{tail.start_branch.value}/{tail.boundary_index}")
                raw["tail_events"].append(event("safe_tail_selected", tail_id=raw_tail["tail_id"], use="safe-tail",
                    deadline_tick=request.deadline_tick, observed_tick=test.backend.state.movement_tick_id,
                    second_action_authorizations=0, dependency_facts=raw_tail["dependency_facts"], native_tail=asdict(tail)))
            if spec.get("release"):
                _record_complete_release(test, handle, raw, event)
            if spec.get("close"):
                # This is the original process port, inbox and receiver used
                # for the preparation above. Its exit code is observed here.
                test.assertTrue(inbox.wait_for_receiver(timeout=10))
                report = inbox.close(timeout_seconds=.5)
                test.assertTrue(inbox.has_registered_handle(handle))
                test.assertTrue(inbox.usage_active(handle, ProofUse.SAFE_TAIL))
                test.assertEqual(report.live_receivers+report.live_workers, 0)
                raw["reference_events"].append(event("responsibility_retained", use="safe-tail", user_id=handle.owner_id))
                raw["close_events"].append(event("service_closed_unresolved", outcome="BODY_RESPONSIBILITY_UNRESOLVED",
                    responsibility_resolved=False, supervisor_confirmed=False, command_terminals_complete=False,
                    worker_exited=report.live_workers == 0, receiver_exited=report.live_receivers == 0,
                    proof_readable=inbox.has_registered_handle(handle), snapshot_lease_pinned=report.lease_count > 0,
                    worker_exit_code=report.worker_exit_code,
                    native_close_report=asdict(report), native_worker_exit_code=report.worker_exit_code))
            for operation in spec["operations"]:
                raw["contract_events"].append(event(operation, facts=operation_facts, **hashes))
            raw["registry_command_sequences"] = [action.request_sequence_id
                for registered in inbox._registry.values() for action in registered.commands]
            raw["actual_ledger_sequences"] = [record.control_sequence for record in test.f.ledger.snapshot()]
            test.assertTrue(set(raw["registry_command_sequences"]).issubset(raw["actual_ledger_sequences"]))
            raw.update(published=bool(raw["publication_events"]), requires_activation=bool(raw["activation_events"]),
                execution_started=bool(raw["activation_events"]), status="PASS", exception_type=None, traceback="",
                actual=True, expected=True, failure_reason="current prepared motion contracts are not complete")
            return json.loads(canonical_bytes(raw))
    finally:
        test.doCleanups()


def _record_cancel_orders(test, request, lease, raw, event):
    """Pause the actual original receiver at its real atomic publish boundary."""
    from dataclasses import replace
    import time
    from unittest.mock import patch
    from mc2p.motion_nav.motion_worker import _execute_job, PreparedMotionJob
    from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
    from mc2p.motion_nav.prepared_motion_proof import prepare_fixed_motion, seal_bundle
    from mc2p.motion_nav.prepared_motion import PreparedAdmissionStatus
    inbox, worker = test.f.inbox, test.f.worker
    bounds = lease.snapshot.identity.bounds
    region = PhysicsWorldBounds(int(bounds.min_x), int(bounds.max_x-1), int(bounds.min_y),
                                int(bounds.max_y-1), int(bounds.min_z), int(bounds.max_z-1))
    raw["native_cancel_trials"] = []
    for index, ordering in enumerate(("cancel_before_publish", "publish_before_cancel"), 1):
        native_lease = test.f.world.acquire_published_snapshot(region, ruleset=request.ruleset).lease
        test.assertIsNotNone(native_lease)
        trial = replace(request, job_token="g1-cancel-" + ordering,
            async_identity=replace(request.async_identity, revision=request.async_identity.revision+index),
            snapshot_lease_token=native_lease.lease_token)
        produced = prepare_fixed_motion(trial, native_lease.snapshot)
        test.assertIsNotNone(produced.bundle)
        submitted = inbox.register_preparation(trial, native_lease)
        test.assertEqual(submitted.status.value, "accepted", repr(submitted))
        test.assertTrue(inbox.wait_for_receiver(timeout=10))
        original = inbox._publish_prepared
        reached, resume = threading.Event(), threading.Event()
        publish_time = []
        publish_errors = []
        def barrier(record, *args):
            if record.identity == trial.async_identity:
                reached.set()
                test.assertTrue(resume.wait(10))
                publish_time.append(time.perf_counter_ns())
            try:
                return original(record, *args)
            except Exception:
                import traceback
                publish_errors.append(traceback.format_exc())
                raise
        with patch.object(inbox, "_publish_prepared", side_effect=barrier):
            worker.put(seal_bundle(produced.bundle))
            inbox.wake_receiver()
            test.assertTrue(reached.wait(10))
            if ordering == "cancel_before_publish":
                inbox.retire(trial.async_identity)
                cancel_time = time.perf_counter_ns()
            resume.set()
            test.assertTrue(inbox.wait_for_receiver(timeout=10))
            admission = inbox.prepared_admission(trial.async_identity)
            test.assertEqual(publish_errors, [])
            if ordering == "cancel_before_publish":
                test.assertIs(admission.status, PreparedAdmissionStatus.CANCELLED, repr(admission))
                test.assertIsNone(admission.handle)
                raw["race_events"].append(event("cancel_publish_race", ordering=ordering,
                    cancel_timestamp_ns=cancel_time, publish_timestamp_ns=publish_time[0], publication_was_attempt=True,
                    handle_published=False, outcome="CANCELLED", unauthorized_body_steps=0,
                    trial_job_token=trial.job_token))
            else:
                test.assertIs(admission.status, PreparedAdmissionStatus.REGISTERED)
                inbox.retire(trial.async_identity)
                test.assertTrue(inbox.release_candidate(admission.handle))
                cancel_time = time.perf_counter_ns()
                test.assertFalse(inbox.has_registered_handle(admission.handle))
                raw["race_events"].append(event("cancel_publish_race", ordering=ordering,
                    cancel_timestamp_ns=cancel_time, publish_timestamp_ns=publish_time[0], candidate_revoked=True,
                    current_body_owner_retained=test.f.supervisor.route is test.f.control, unauthorized_body_steps=0,
                    trial_job_token=trial.job_token))
            raw["native_cancel_trials"].append(dict(ordering=ordering, job_token=trial.job_token,
                payloads=canonical_prepared_payloads(trial, produced.bundle, native_lease.snapshot),
                native_admission=asdict(admission)))


def _record_complete_release(test, handle, raw, event):
    from dataclasses import replace
    from mc2p.contracts.action_v1 import MovementV1, MovementTickWindowV1
    from mc2p.contracts.motion_activation import ActivationStatus
    from mc2p.motion_nav.body_control import HandoffDisposition
    from mc2p.motion_nav.prepared_motion import (
        ProofUse, ProofConsumerIdentity, ProofActionExitEvidence, ProofSupervisorReleaseEvidence,
        ProofReleaseEvidence, ReferenceStatus,
    )
    controller, inbox = test.f.executor.prepared_controller, test.f.inbox
    for index, command in enumerate(handle.hot_view.commands[1:], 1):
        tick = controller.stream_tracker.expected_effect_tick(index)
        proposal = replace(test.proposal, movement_intent_id=f"g1-body-input/{index}",
            expected_movement=command.movement, expected_yaw_radians=command.required_movement_yaw_radians,
            requested_window=MovementTickWindowV1(tick, tick))
        result = test.dispatch(proposal=proposal)
        test.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
    anchor = test.f.anchor(test.backend.state, test.runtime.observation.sequence_id)
    exit_decision = test.f.executor.decide(test.f.frame(anchor), state_anchor=anchor,
                                          input_ledger=test.f.ledger)
    test.assertEqual(exit_decision.reason_code, "action_route_complete")
    # One existing ordinary neutral frame resolves the tiny observed residual.
    # The actual command is also retained by the same body's proof use.
    test.runtime.submit_intent(test.intent("g1-release-residual", movement=MovementV1()))
    result = test.frame()
    commands = inbox._registry[handle.proof_token].commands
    appended = inbox.record_command(handle, command_index=len(commands), action=result.decision.action)
    test.assertIs(appended.status, ReferenceStatus.RETAINED)
    anchor = test.f.anchor(test.backend.state, test.runtime.observation.sequence_id)
    frame = test.f.frame(anchor)
    action_exit = test.f.control.safe_to_release(frame, test.f.ledger, anchor, input_floor=0)
    commands = inbox._registry[handle.proof_token].commands
    terminals = tuple(test.f.ledger.record(action.request_sequence_id) for action in commands)
    original_release = test.f.executor.release_prepared_usage
    observed_releases = {}
    def observe_release(handle, *, use, evidence):
        result = original_release(handle, use=use, evidence=evidence)
        if result.status is ReferenceStatus.RELEASED:
            observed_releases[use] = result
        return result
    with patch.object(test.f.executor, "release_prepared_usage", observe_release):
        confirmed = test.f.supervisor.evaluate_quiescence(frame, test.f.ledger, anchor)
    test.assertIs(action_exit.disposition, HandoffDisposition.QUIESCENT)
    test.assertEqual(confirmed, action_exit)
    terminal_ids = []
    for record in terminals:
        terminal_id = f"terminal/{record.control_sequence}"
        terminal_ids.append(terminal_id)
        raw["command_terminal_events"].append(event("command_terminal", producer="Runtime", event_id=terminal_id,
            action_id=raw["input"]["identity"]["action_id"], sequence=record.control_sequence, status="APPLIED",
            terminal_tick=record.resolved_at_tick, native_record=asdict(record)))
    for use in (ProofUse.EXECUTING, ProofUse.SAFE_TAIL):
        consumer = ProofConsumerIdentity.from_handle(handle, use)
        exit_id = f"exit/{handle.proof_token}/{use.value}"
        evidence = ProofReleaseEvidence(consumer, terminals, ProofActionExitEvidence(consumer, exit_id, action_exit),
            ProofSupervisorReleaseEvidence(consumer, exit_id, confirmed))
        supervisor_id = f"supervisor/{use.value}"
        raw["supervisor_events"].append(event("body_responsibility_released", producer="ExecutionSupervisor",
            event_id=supervisor_id, action_id=raw["input"]["identity"]["action_id"], use=use.value.replace("_", "-"),
            command_terminal_event_ids=terminal_ids, safe_boundary=True, native_handoff=asdict(confirmed)))
        released = observed_releases[use]
        test.assertIs(released.status, ReferenceStatus.RELEASED)
        raw["reference_events"].append(event("release", use=use.value.replace("_", "-"), user_id=handle.owner_id,
            command_terminals_complete=True, responsibility_resolved=True, supervisor_confirmed=True,
            command_terminal_event_ids=terminal_ids, supervisor_event_id=supervisor_id, native_result=asdict(released)))


def contract_value_immutability(): return run_prepared_contract("contract_value_immutability")
def snapshot_version_lease(): return run_prepared_contract("snapshot_version_lease")
def fixed_input_binding(): return run_prepared_contract("fixed_input_binding")
def proof_ruleset_dependency_validation(): return run_prepared_contract("proof_ruleset_dependency_validation")
def receiver_single_consumer_atomic_publish(): return run_prepared_contract("receiver_single_consumer_atomic_publish")
def candidate_execution_tail_reference_lifecycle(): return run_prepared_contract("candidate_execution_tail_reference_lifecycle")
def same_owner_action_handoff(): return run_prepared_contract("same_owner_action_handoff")
def runtime_post_arbitration_activation(): return run_prepared_contract("runtime_post_arbitration_activation")
def first_receipt_branch_pruning(): return run_prepared_contract("first_receipt_branch_pruning")
def midstream_deviation(): return run_prepared_contract("midstream_deviation")
def cancel_publish_race(): return run_prepared_contract("cancel_publish_race")
def overdue_safe_tail(): return run_prepared_contract("overdue_safe_tail")
def close_with_unresolved_responsibility(): return run_prepared_contract("close_with_unresolved_responsibility")


def probe_current_contract(method):
    producer = {
        RED_METHODS[0]: fixed_input_binding, RED_METHODS[1]: contract_value_immutability,
        RED_METHODS[2]: receiver_single_consumer_atomic_publish,
        RED_METHODS[3]: candidate_execution_tail_reference_lifecycle, RED_METHODS[4]: overdue_safe_tail,
    }.get(method)
    if producer is None:
        raise ValueError("unknown current contract")
    return producer()
