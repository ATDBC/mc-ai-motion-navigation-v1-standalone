"""Fixed-input preparation against the unchanged independent scanner."""
from dataclasses import asdict, fields, is_dataclass, replace
import hashlib
import importlib
import importlib.util
import json
import math
from pathlib import Path
import pickle
import time
import unittest
from unittest import mock

from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.commitment import (
    CandidateRejection, ScanStatus, TailStatus, scan_commitment,
)
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
from mc2p.motion_nav.motion_solver import MotionCommandTick
from mc2p.motion_nav.motion_worker import MotionJobOperation, MotionSolverWorker
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.prepared_motion import (
    PreparedComputeStatus, PreparedMotionRequest, PreparedScanBudget, SnapshotIdentity,
    StreamGeneration,
)
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, UNKNOWN_CELL
from mc2p.motion_nav.world_snapshot import SealedSnapshot, canonical_snapshot_bytes


def input_digest(inputs):
    """Independent canonical input bytes, not a worker-provided digest."""
    payload = json.dumps([asdict(command) for command in inputs], sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sealed_fixture(fixture, *, changes=None, omissions=()):
    facts = dict(fixture.world._world._snapshot_facts)
    facts.update(changes or {})
    for position in omissions:
        facts.pop(position, None)
    ordered = tuple(sorted(facts.items()))
    versions = tuple((position, 1) for position, _ in ordered)
    identity = SnapshotIdentity(fixture.world.session.value, fixture.world.geometry_revision,
                                0, 1, "0" * 64, JAVA_1_21_RULESET,
                                Aabb(-3, -50, -3, 4, 8, 13))
    data = canonical_snapshot_bytes(identity, ordered, versions)
    identity = replace(identity, digest=hashlib.sha256(data).hexdigest())
    return SealedSnapshot(identity, data, ordered, versions)


def fixed_request(fixture, inputs, reference, snapshot, *, token="job-fixed-gap"):
    entry, exit_state = fixture.request.anchor_state, reference.proof.branches[0].states[-1]
    window = CandidateExecutionWindow(entry.movement_tick_id + 1, entry.movement_tick_id + 2)
    suffix = ("known-stop-or-proved-landing",)
    stream = StreamGeneration("stream-fixed-gap", "body-fixed-gap", "request-fixed-gap",
                              "route-fixed-gap", 0, inputs, (entry,), window, suffix,
                              fixture.request.stop_input)
    commands = tuple(MotionCommandTick(MovementV1(
        forward=int(item.forward), strafe=int(item.strafe), jump=item.jump,
        sneak=item.sneak, sprint=item.sprint), item.movement_yaw_radians) for item in inputs)
    return PreparedMotionRequest(
        token, AsyncWorkIdentity(AsyncComputationScope(entry.session.value, "task-fixed-gap", 1),
                                "computation-fixed-gap", AsyncWorkKind.MOTION_SOLVE,
                                stream.request_id, 1),
        stream, 1, 1, 1, commands, inputs, input_digest(inputs), entry, exit_state, window,
        window.earliest_start_tick, "ledger-fixed-gap", entry.session.value, (),
        snapshot.identity, "lease-fixed-gap", (), JAVA_1_21_RULESET,
        fixture.request.task_damage_budget,
        fixture.request.task_damage_budget.maximum_expected_damage_points,
        PreparedScanBudget(65_536, 65_536, 1), suffix,
    )


class ProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = m0_probe._fixture_for("jump_gap_1", "A3")
        frozen = json.loads((Path(__file__).resolve().parents[2]
                             / "experiments/motion_navigation/trajectory_proto/data"
                             / "d097a-set-a-sample.json").read_text(encoding="utf-8"))
        cls.inputs = tuple(TickInput(*row) for row in frozen["inputs"]["jump_gap_1:A3"])
        cls.reference = scan_commitment(cls.fixture.request, cls.inputs, cls.fixture.world,
                                        _boundary_evidence(cls.fixture.request, cls.inputs))
        if cls.reference.status is not ScanStatus.VERIFIED_CANDIDATE:
            raise AssertionError("the frozen independent gap scanner must remain verified")
        cls.snapshot = sealed_fixture(cls.fixture)
        cls.request = fixed_request(cls.fixture, cls.inputs, cls.reference, cls.snapshot)

    def preparer(self):
        name = "mc2p.motion_nav.prepared_motion_proof"
        self.assertIsNotNone(importlib.util.find_spec(name),
                             "the production fixed-input proof entry is missing")
        module = importlib.import_module(name)
        self.assertTrue(callable(getattr(module, "prepare_fixed_motion", None)),
                        "the production fixed-input proof entry is missing")
        return module

    def prepare(self, request=None, snapshot=None):
        return self.preparer().prepare_fixed_motion(request or self.request,
                                                    snapshot or self.snapshot)

    def test_same_fixed_inputs_match_frozen_scanner(self):
        result = self.prepare()
        self.assertIs(result.status, PreparedComputeStatus.PREPARED)
        self.assertEqual(result.bundle.inputs, self.request.expected_inputs)
        self.assertEqual(result.bundle.proof.physics_steps, self.reference.counts.physics_steps)
        self.assertEqual(result.counts.physics_steps, self.reference.counts.physics_steps)
        self.assertEqual(result.counts.nodes_scanned, self.reference.counts.nodes)
        self.assertEqual(result.counts.stopping_tail_steps, self.reference.counts.tail_ticks)
        self.assertEqual(result.bundle.proof.stopping_tail_steps, self.reference.counts.tail_ticks)
        self.assertEqual(result.counts.completed_candidates, 1)
        self.assertLessEqual(result.counts.physics_steps, 65_536)
        self.assertEqual(result.bundle.proof.dependency_facts, self.reference.proof.dependency_facts)
        for actual, frozen in zip(result.bundle.proof.branches, self.reference.proof.branches):
            self.assertEqual(actual.trajectory, frozen.states)
            interval, = frozen.risk_intervals
            self.assertEqual(actual.commitment_index, interval.first_committed_boundary)
            self.assertEqual(actual.last_abandonable_index, interval.last_abandon_boundary)
        module = self.preparer()
        self.assertEqual(module.canonical_motion_digest(result.bundle.proof), result.bundle.proof_digest)
        self.assertEqual(module.canonical_bundle_bytes(result.bundle), result.bundle.sealed_bytes)

    def test_each_boundary_has_valid_tail_and_dependencies(self):
        result = self.prepare()
        self.assertIs(result.status, PreparedComputeStatus.PREPARED)
        world = PhysicsWorldView.from_snapshot(self.snapshot)
        versions = dict(self.snapshot.dependency_versions)
        for actual, frozen in zip(result.bundle.proof.branches, self.reference.proof.branches):
            self.assertEqual(tuple(tail.boundary_index for tail in actual.safety_tails),
                             tuple(range(len(self.inputs) + 1)))
            for tail, immediate in zip(actual.safety_tails, frozen.tails):
                with self.subTest(branch=actual.start_branch, boundary=tail.boundary_index):
                    self.assertEqual(tail.trajectory[0], actual.trajectory[tail.boundary_index])
                    if immediate.status is TailStatus.SAFE_STOP:
                        self.assertEqual((tail.inputs, tail.trajectory),
                                         (immediate.inputs, immediate.states))
                    else:
                        recovery = frozen.risk_intervals[0].recovered_boundary
                        self.assertEqual(tail.inputs[:recovery - tail.boundary_index],
                                         self.inputs[tail.boundary_index:recovery])
                    current, replay_dependencies = tail.trajectory[0], set()
                    peak, damage = 0., 0.
                    for command, expected in zip(tail.inputs, tail.trajectory[1:]):
                        calculated = step(current, command, world, JAVA_1_21_RULESET)
                        self.assertIs(calculated.status, CalculationStatus.OK)
                        replay_dependencies.update(calculated.dependencies)
                        self.assertEqual(calculated.next_state, expected)
                        current = calculated.next_state
                        peak = max(peak, current.fall_distance_blocks)
                        if current.on_ground:
                            damage += conservative_plain_fall_damage_points(peak)
                            peak = 0.
                    self.assertTrue(current.on_ground)
                    self.assertLessEqual(math.hypot(current.velocity_blocks_per_tick[0],
                                                    current.velocity_blocks_per_tick[2]), 1.e-9)
                    self.assertLessEqual(damage, tail.expected_damage_points)
                    self.assertLessEqual(len(tail.inputs), 80)
                    facts = dict(tail.dependency_facts)
                    self.assertTrue(replay_dependencies.issubset(facts))
                    self.assertEqual(tail.dependency_versions,
                                     tuple((position, versions[position]) for position in facts))

    def test_one_budget_covers_branches_and_tails(self):
        full = self.prepare()
        total = full.counts.physics_steps
        for limit in (len(self.inputs), total // 2, total - 1):
            with self.subTest(limit=limit):
                request = replace(self.request, scan_budget=PreparedScanBudget(65_536, limit, 1))
                result = self.prepare(request)
                self.assertIs(result.status, PreparedComputeStatus.BUDGET_EXHAUSTED)
                self.assertIsNone(result.bundle)
                self.assertEqual(result.counts.physics_steps, limit)
        limited = self.prepare(replace(self.request,
                               scan_budget=PreparedScanBudget(full.counts.nodes_scanned - 1, total, 1)))
        self.assertIs(limited.status, PreparedComputeStatus.BUDGET_EXHAUSTED)
        self.assertLessEqual(limited.counts.physics_steps, total)

    def test_unknown_or_unsupported_is_typed(self):
        origin = (0, 0, 0)
        old = self.fixture.world.cell(origin)
        cases = (
            (sealed_fixture(self.fixture, omissions=(origin,)), PreparedComputeStatus.MISSING_INFORMATION),
            (sealed_fixture(self.fixture, changes={origin: replace(old,
                block=BlockGeometry("minecraft:slime_block", "boxes", (Aabb(0, 0, 0, 1, .5, 1),)))}),
             PreparedComputeStatus.UNSUPPORTED),
        )
        for snapshot, status in cases:
            with self.subTest(status=status):
                request = replace(self.request, snapshot_identity=snapshot.identity)
                result = self.prepare(request, snapshot)
                self.assertIs(result.status, status)
                self.assertIsNone(result.bundle)
                self.assertGreater(result.counts.physics_steps, 0)
                self.assertEqual(bool(result.missing_cells), status is PreparedComputeStatus.MISSING_INFORMATION)
                frozen = scan_commitment(self.fixture.request, self.inputs,
                    PhysicsWorldView.from_snapshot(snapshot), _boundary_evidence(self.fixture.request, self.inputs))
                self.assertEqual((result.counts.nodes_scanned, result.counts.physics_steps,
                                  result.counts.stopping_tail_steps),
                                 (frozen.counts.nodes, frozen.counts.physics_steps, frozen.counts.tail_ticks))
                if status is PreparedComputeStatus.UNSUPPORTED:
                    self.assertIs(frozen.reason, CandidateRejection.UNSUPPORTED_PHYSICS)
                else:
                    self.assertIs(frozen.status, ScanStatus.NEEDS_INFORMATION)

    def test_inherited_dependency_versions_are_preserved_or_typed_rejected(self):
        inherited = ((-3, -50, -3), 1)
        accepted = self.prepare(replace(self.request, inherited_dependencies=(inherited,)))
        self.assertIs(accepted.status, PreparedComputeStatus.PREPARED)
        self.assertIn(inherited, accepted.bundle.proof.dependency_versions)
        self.assertEqual(accepted.counts.physics_steps, self.reference.counts.physics_steps)
        for dependency, status in ((((inherited[0], 2),), PreparedComputeStatus.BLOCKED),
                                   ((((100, 0, 0), 1),), PreparedComputeStatus.MISSING_INFORMATION)):
            with self.subTest(status=status):
                rejected = self.prepare(replace(self.request, inherited_dependencies=dependency))
                self.assertIs(rejected.status, status)
                self.assertIsNone(rejected.bundle)
                self.assertEqual(rejected.counts.physics_steps, 0)
        # A present semantic version does not turn an UNKNOWN fact into known
        # inherited evidence, even when that cell is off the actual trajectory.
        unknown_snapshot = sealed_fixture(self.fixture, changes={inherited[0]: UNKNOWN_CELL})
        unknown_request = replace(self.request, snapshot_identity=unknown_snapshot.identity,
                                  inherited_dependencies=(inherited,))
        with mock.patch("mc2p.motion_nav.prepared_motion_proof.step",
                        side_effect=AssertionError("UNKNOWN inheritance reached physics")):
            unknown = self.prepare(unknown_request, unknown_snapshot)
        self.assertIs(unknown.status, PreparedComputeStatus.MISSING_INFORMATION)
        self.assertEqual(unknown.missing_cells, (inherited[0],))
        self.assertEqual(unknown.counts.physics_steps, 0)

    def test_future_inputs_do_not_invent_sequences(self):
        result = self.prepare()
        def check(value):
            if is_dataclass(value):
                for field in fields(value):
                    self.assertNotIn(field.name, ("control_sequence", "first_control_sequence",
                                                 "first_candidate_control_sequence"))
                    check(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    check(item)
        check(result.bundle)

    def test_late_start_replays_explicit_non_neutral_hold(self):
        normal = self.prepare()
        hold = replace(self.inputs[0], forward=-1.)
        stream = replace(self.request.stream, generation_id="stream-hold-back",
                         late_start_hold_input=hold)
        result = self.prepare(replace(self.request, stream=stream))
        self.assertIs(result.status, PreparedComputeStatus.PREPARED)
        expected = step(self.request.entry_state, hold, PhysicsWorldView.from_snapshot(self.snapshot),
                        JAVA_1_21_RULESET)
        self.assertIs(expected.status, CalculationStatus.OK)
        self.assertEqual(result.bundle.proof.branches[1].trajectory[0], expected.next_state)
        self.assertNotEqual(result.bundle.proof.branches[1].trajectory,
                            normal.bundle.proof.branches[1].trajectory)
        self.assertNotEqual(result.bundle.proof_digest, normal.bundle.proof_digest)
        old = self.fixture.request
        reference_request = replace(old, entry_states=(old.anchor_state, expected.next_state),
            branch_preludes=((), (replace(old.branch_preludes[1][0], tick_input=hold),)))
        reference = scan_commitment(reference_request, self.inputs, self.fixture.world,
                                     _boundary_evidence(reference_request, self.inputs))
        self.assertIs(reference.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(result.bundle.proof.branches[1].trajectory, reference.proof.branches[1].states)
        self.assertEqual((result.counts.nodes_scanned, result.counts.physics_steps,
                          result.counts.stopping_tail_steps),
                         (reference.counts.nodes, reference.counts.physics_steps, reference.counts.tail_ticks))
        # One extra held Walk makes this fixed jump miss its zero-damage
        # allowance. The independent scanner rejects exactly the same input.
        forward = replace(stream, generation_id="stream-hold-forward", late_start_hold_input=self.inputs[0])
        blocked = self.prepare(replace(self.request, stream=forward))
        self.assertIs(blocked.status, PreparedComputeStatus.BLOCKED)
        walk_entry = step(old.anchor_state, self.inputs[0], self.fixture.world, JAVA_1_21_RULESET).next_state
        walk_request = replace(old, entry_states=(old.anchor_state, walk_entry),
            branch_preludes=((), (replace(old.branch_preludes[1][0], tick_input=self.inputs[0]),)))
        walk_reference = scan_commitment(walk_request, self.inputs, self.fixture.world,
                                          _boundary_evidence(walk_request, self.inputs))
        self.assertIs(walk_reference.reason, CandidateRejection.DAMAGE_ALLOWANCE)
        self.assertEqual(blocked.counts.physics_steps, walk_reference.counts.physics_steps)
        jumping_hold = replace(hold, jump=True)
        jumping = replace(stream, generation_id="stream-hold-jump", late_start_hold_input=jumping_hold)
        rejected = self.prepare(replace(self.request, stream=jumping))
        self.assertIs(rejected.status, PreparedComputeStatus.BLOCKED)
        self.assertIsNone(rejected.bundle)

    def test_wrong_or_other_legal_inputs_are_not_substituted(self):
        module = self.preparer()
        with mock.patch("mc2p.motion_nav.motion_solver.solve_one_cell_gap", side_effect=AssertionError("search")), \
             mock.patch("mc2p.motion_nav.motion_solver.solve_air_transition", side_effect=AssertionError("search")):
            self.assertIs(self.prepare().status, PreparedComputeStatus.PREPARED)
            broken = pickle.loads(pickle.dumps(self.request))
            object.__setattr__(broken, "expected_inputs", (self.fixture.request.stop_input,) + self.inputs[1:])
            with self.assertRaises(ContractViolation):
                module.prepare_fixed_motion(broken, self.snapshot)
            bad_digest = replace(self.request, expected_input_digest="0" * 64)
            with self.assertRaises(ContractViolation):
                module.prepare_fixed_motion(bad_digest, self.snapshot)
            original = self.prepare().bundle
            other_inputs = (self.fixture.request.stop_input,) * len(self.inputs)
            changed = replace(self.request.stream, expected_inputs=other_inputs)
            commands = tuple(MotionCommandTick(MovementV1(), 0.) for _ in other_inputs)
            other_request = replace(self.request, stream=changed, commands=commands,
                                    expected_inputs=other_inputs, expected_input_digest=input_digest(other_inputs))
            # A valid different input sequence must not be repaired into the original jump.
            other = self.prepare(other_request)
            self.assertIs(other.status, PreparedComputeStatus.BLOCKED)
            self.assertIsNone(other.bundle)
            self.assertEqual(original.inputs, self.inputs)

    def test_spawn_uses_transmitted_snapshot(self):
        module = self.preparer()
        self.assertTrue(hasattr(MotionJobOperation, "PREPARE"), "the existing worker lacks PREPARE")
        worker_module = importlib.import_module("mc2p.motion_nav.motion_worker")
        self.assertTrue(hasattr(worker_module, "PreparedMotionJob"), "the existing worker lacks fixed jobs")
        origin, old = (0, 0, 0), self.fixture.world.cell((0, 0, 0))
        dirt = sealed_fixture(self.fixture, changes={origin: replace(old,
                                                    block=BlockGeometry.full_cube("minecraft:dirt"))})
        unsupported = sealed_fixture(self.fixture, changes={origin: replace(old,
                            block=BlockGeometry.full_cube("minecraft:ice"))})
        requests = (replace(self.request, job_token="spawn-dirt", snapshot_identity=dirt.identity),
                    replace(self.request, job_token="spawn-ice", snapshot_identity=unsupported.identity))
        with mock.patch.object(m0_probe, "_fixture_for", side_effect=AssertionError("fixture read")) as worker_fixture_read_spy, \
             mock.patch.object(m0_probe, "run_case", side_effect=AssertionError("fixture search")), \
             MotionSolverWorker(max_pending=2) as worker:
            pid = worker.pid
            self.assertTrue(worker.is_alive())
            for request, snapshot in zip(requests, (dirt, unsupported)):
                self.assertTrue(worker.submit(worker_module.PreparedMotionJob(request, snapshot)))
            results = []
            deadline = time.perf_counter() + 10.
            while len(results) < 2 and time.perf_counter() < deadline:
                results.extend(worker.poll_available())
                if len(results) < 2:
                    time.sleep(.01)
            self.assertEqual(worker.pid, pid)
            self.assertTrue(worker.is_alive())
            self.assertEqual(worker_fixture_read_spy.call_count, 0)
        self.assertEqual(len(results), 2)
        by_token = {result.job_token: result for result in results}
        for message in results:
            self.assertEqual(hashlib.sha256(message.payload).hexdigest(), message.payload_digest)
        bundle = pickle.loads(by_token["spawn-dirt"].payload)
        self.assertEqual(bundle.snapshot_identity, dirt.identity)
        self.assertEqual(dict(bundle.proof.dependency_facts)[origin].block.material_key, "minecraft:dirt")
        failure = pickle.loads(by_token["spawn-ice"].payload)
        self.assertIs(failure.status, PreparedComputeStatus.UNSUPPORTED)
        self.assertNotEqual(by_token["spawn-dirt"].schema, by_token["spawn-ice"].schema)
        self.assertEqual(module.seal_bundle(bundle).schema, by_token["spawn-dirt"].schema)

    def test_spawn_returns_missing_information_at_128_byte_job_token_and_stays_alive(self):
        module = self.preparer()
        worker_module = importlib.import_module("mc2p.motion_nav.motion_worker")
        # Invalid requests are rejected at parent construction, before they can
        # enter the persistent worker or consume its bounded failure channel.
        for size in (129, module.MAXIMUM_MESSAGE_BYTES, module.MAXIMUM_MESSAGE_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(ContractViolation):
                worker_module.PreparedMotionJob(replace(self.request, job_token="j" * size,
                    inherited_dependencies=(((100, 0, 0), 1),)), self.snapshot)
        token = "j" * 128
        missing_request = replace(self.request, job_token=token,
                                  inherited_dependencies=(((100, 0, 0), 1),))
        with MotionSolverWorker(max_pending=2) as worker:
            pid = worker.pid
            self.assertTrue(worker.submit(worker_module.PreparedMotionJob(missing_request, self.snapshot)))
            messages = []
            deadline = time.perf_counter() + 10.
            while not messages and time.perf_counter() < deadline:
                messages.extend(worker.poll_available())
                if not messages:
                    time.sleep(.01)
            self.assertEqual(len(messages), 1)
            message = messages[0]
            self.assertEqual(message.job_token, token)
            self.assertEqual(message.schema, module.PREPARED_FAILURE_SCHEMA)
            self.assertEqual(hashlib.sha256(message.payload).hexdigest(), message.payload_digest)
            self.assertLessEqual(len(message.payload), module.MAXIMUM_MESSAGE_BYTES)
            failure = pickle.loads(message.payload)
            self.assertIs(failure.status, PreparedComputeStatus.MISSING_INFORMATION)
            self.assertEqual(failure.job_token, token)
            self.assertEqual(failure.missing_cells, ((100, 0, 0),))
            self.assertEqual(failure.counts.physics_steps, 0)
            self.assertEqual(worker.pid, pid)
            self.assertTrue(worker.is_alive())
            self.assertIsNone(worker._process.exitcode)
            following = replace(self.request, job_token="following-normal-job")
            self.assertTrue(worker.submit(worker_module.PreparedMotionJob(following, self.snapshot)))
            messages = []
            deadline = time.perf_counter() + 10.
            while not messages and time.perf_counter() < deadline:
                messages.extend(worker.poll_available())
                if not messages:
                    time.sleep(.01)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].job_token, following.job_token)
            self.assertEqual(messages[0].schema, module.PREPARED_BUNDLE_SCHEMA)
            self.assertEqual(pickle.loads(messages[0].payload).inputs, self.inputs)
            self.assertEqual(worker.pid, pid)
            self.assertTrue(worker.is_alive())
            self.assertIsNone(worker._process.exitcode)

    def test_oversized_bundle_returns_bounded_budget_failure_with_actual_work(self):
        module = self.preparer()
        worker_module = importlib.import_module("mc2p.motion_nav.motion_worker")
        suffix = ("large-suffix", b"x" * (module.MAXIMUM_MESSAGE_BYTES + 1))
        stream = replace(self.request.stream, generation_id="large-stream", suffix_identity=suffix)
        request = replace(self.request, job_token="large-job", stream=stream,
                          safety_tail_requirement=suffix)
        message = worker_module._execute_job(worker_module.PreparedMotionJob(request, self.snapshot))
        result = pickle.loads(message.payload)
        self.assertIs(result.status, PreparedComputeStatus.BUDGET_EXHAUSTED)
        self.assertIsNone(result.bundle)
        self.assertEqual(result.counts.physics_steps, self.reference.counts.physics_steps)
        self.assertEqual(result.counts.stopping_tail_steps, self.reference.counts.tail_ticks)
        self.assertLessEqual(len(message.payload), module.MAXIMUM_MESSAGE_BYTES)


if __name__ == "__main__":
    unittest.main()
