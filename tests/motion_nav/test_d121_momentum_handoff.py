"""D121 component proof tests; no worker handoff or production admission."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import math
from types import SimpleNamespace
import unittest
from unittest import mock

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.motion_chain import (
    FallbackCandidate, FallbackTerminalMode, FallbackYawMode,
    preserve_current_neutral_fallback, validate_fallback_candidates,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, MotionCommandTick, SolveStatus,
    _release_recovery_evidence,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.prepared_motion import (
    PreparedComputeStatus, PreparedMotionRequest, PreparedScanBudget, StreamGeneration,
)
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest, prepare_fixed_motion
from mc2p.motion_nav.world_model import UNKNOWN_CELL
from mc2p.motion_nav.world_snapshot import SealedSnapshot, canonical_snapshot_bytes, seal_snapshot_version
from scripts import d121_momentum_handoff_probe as probe
from scripts import d120_momentum_entry_calibration as calibration


def _snapshot_with_change(snapshot, position, fact):
    facts = dict(snapshot.facts)
    facts[position] = fact
    ordered = tuple(sorted(facts.items()))
    versions = dict(snapshot.dependency_versions)
    versions[position] = versions.get(position, 0) + 1
    ordered_versions = tuple(sorted(versions.items()))
    identity = replace(snapshot.identity,
        geometry_revision=snapshot.identity.geometry_revision + 1,
        local_revision=snapshot.identity.local_revision + 1,
        digest="0" * 64)
    data = canonical_snapshot_bytes(identity, ordered, ordered_versions)
    identity = replace(identity, digest=hashlib.sha256(data).hexdigest())
    return SealedSnapshot(identity, data, ordered, ordered_versions)


def _d120_fallback_request(*, controls=("sneak",), snapshot=None, remaining=0.0):
    fixture = calibration.build_fixture()
    entry = calibration.nominal_entry(fixture)
    if snapshot is None:
        version = fixture.context.world._world._owner.borrow_snapshot_version(
            PhysicsWorldBounds(-4, 4, 55, 70, -10, 12), ruleset=JAVA_1_21_RULESET)
        if version is None:
            raise AssertionError("D120 fixture snapshot is unavailable")
        snapshot = seal_snapshot_version(version, JAVA_1_21_RULESET)
    movement = MovementV1(sneak=True)
    main_input = TickInput(0., 0., False, True, False, entry.yaw_radians)
    calculated = step(entry, main_input, PhysicsWorldView.from_snapshot(snapshot), JAVA_1_21_RULESET)
    if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
        raise AssertionError("D120 one-tick fallback fixture no longer advances")
    window = CandidateExecutionWindow(entry.movement_tick_id + 1, entry.movement_tick_id + 1)
    suffix = ("d121-declared-fallback",)
    stream = StreamGeneration("d121-stream", "d121-owner", "d121-request", "d121-route", 0,
        (main_input,), (entry,), window, suffix,
        TickInput(0., 0., False, False, False, entry.yaw_radians))
    inputs = {
        "neutral": TickInput(0., 0., False, False, False, entry.yaw_radians),
        "sneak": TickInput(0., 0., False, True, False, entry.yaw_radians),
        "back": TickInput(-1., 0., False, False, False, entry.yaw_radians),
    }
    fallbacks = tuple(FallbackCandidate(name, inputs[name], 40,
        FallbackTerminalMode.SNEAK_EDGE_STABLE if name == "sneak"
        else FallbackTerminalMode.STANDARD_SUPPORT) for name in controls)
    request = PreparedMotionRequest(
        "d121-job", AsyncWorkIdentity(
            AsyncComputationScope(entry.session.value, "d121-task", 1),
            "d121-computation", AsyncWorkKind.MOTION_SOLVE, stream.request_id, 1),
        stream, 1, 1, 1, (MotionCommandTick(movement, entry.yaw_radians),),
        (main_input,), canonical_motion_digest((main_input,)), entry, calculated.next_state,
        window, window.earliest_start_tick, "d121-ledger", entry.session.value, (),
        snapshot.identity, "d121-lease", (), JAVA_1_21_RULESET,
        TaskDamageBudget(), remaining, PreparedScanBudget(65_536, 65_536, 1), suffix,
        fallbacks)
    return request, snapshot


class D121S0Tests(unittest.TestCase):
    def test_independent_exit_cannot_feed_frozen_successor(self):
        result = probe.independent_exit_fact()
        self.assertEqual(result["solver_status"], "solved")
        self.assertLess(result["exit_speed_blocks_per_tick"], .01)
        self.assertGreater(result["d120_entry_speed_blocks_per_tick"], .25)
        self.assertFalse(result["fixed_successor"]["passed"])
        self.assertTrue(result["exit_state"]["on_ground"])

    def test_nominal_suffixes_freeze_safe_and_unsafe_bodies(self):
        result = probe.nominal_facts()
        self.assertEqual(result["landed_tick"], 26)
        expected = {"landing-immediate": (True, 20), "fallback-sneak": (True, 8),
                    "fallback-neutral": (False, 1), "fallback-back": (False, 11)}
        for name, (safe, steps) in expected.items():
            with self.subTest(name=name):
                self.assertEqual(result["suffixes"][name]["passed"], safe)
                self.assertEqual(result["suffixes"][name]["steps"], steps)
        for name in ("fallback-neutral", "fallback-back"):
            self.assertEqual(result["suffixes"][name]["reason"], "middle_platform_support_lost")

    def test_unenumerated_midpoint_is_not_authorized_by_bounds(self):
        self.assertIs(probe.sample_status((0., 0., 0., 0., 0.)), probe.SampleStatus.ENUMERATED_ONLY)
        self.assertIs(probe.sample_status((.001, 0., 0., 0., 0.)), probe.SampleStatus.UNPROVEN_MIDPOINT)
        self.assertIs(probe.sample_status((.003, 0., 0., 0., 0.)), probe.SampleStatus.OUTSIDE_SAMPLES)

    def test_diagnostic_gate_rejects_two_branches_and_merged_input(self):
        jump = MovementV1(1, 0, True, False, True)
        sneak = MovementV1(sneak=True)
        self.assertEqual(probe.single_branch_input((jump,)), jump)
        self.assertEqual(probe.single_branch_input((sneak,)), sneak)
        for values in ((jump, sneak), (), (MovementV1(jump=True, sneak=True),)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                probe.single_branch_input(values)

    def test_missing_joint_authority_is_a_frozen_gap_not_a_fake_success(self):
        facts = probe.unimplemented_facts()
        self.assertEqual({f.kind for f in facts}, set(probe.GapKind))
        self.assertTrue(all(f.status == "unimplemented" for f in facts))
        with self.assertRaises(FrozenInstanceError):
            facts[0].status = "passed"

    def test_runtime_observes_l_then_applies_only_selected_input_at_l1(self):
        for branch in ("landing-immediate", "fallback-sneak"):
            with self.subTest(branch=branch):
                result = probe.runtime_timing(branch)
                self.assertEqual(result["landing_tick"], 26)
                self.assertEqual(result["decision_tick"], 26)
                self.assertEqual(result["first_application_tick"], 27)
                self.assertEqual(result["movement_winner_count"], 1)
                self.assertTrue(result["receipt_identity_match"])
                self.assertTrue(result["receipt_movement_match"])
                self.assertTrue(result["final_position_velocity_match"])
                self.assertTrue(result["final_supported"])
                self.assertEqual(result["damage"], 0.)

    def test_receipt_gate_rejects_wrong_identity_tick_or_movement(self):
        selected = SimpleNamespace(
            episode_id="episode",
            request_sequence_id=7,
            movement=MovementV1(1, 0, True, False, True),
        )
        receipt = SimpleNamespace(
            episode_id="episode",
            request_sequence_id=7,
            movement_tick_id=27,
            forward=1.0,
            strafe=0.0,
            jump=True,
            sneak=False,
            sprint=True,
        )
        self.assertEqual(
            probe.receipt_matches_selected_input(receipt, selected, 26),
            (True, True),
        )
        for field, value, expected in (
            ("episode_id", "other", (False, True)),
            ("request_sequence_id", 8, (False, True)),
            ("movement_tick_id", 28, (False, True)),
            ("jump", False, (True, False)),
            ("sneak", True, (True, False)),
        ):
            mutated = SimpleNamespace(**vars(receipt))
            setattr(mutated, field, value)
            with self.subTest(field=field):
                self.assertEqual(
                    probe.receipt_matches_selected_input(mutated, selected, 26),
                    expected,
                )

    def test_cost_counts_actual_physics_calls_and_does_not_claim_worker_margin(self):
        result = probe.replay_cost(repetitions=3)
        self.assertEqual(result["physics_steps_per_decision"], [28] * 3)
        self.assertEqual(len(result["elapsed_ms"]), 3)
        self.assertIsNone(result["background_ready_margin_ticks"])
        self.assertEqual(result["background_ready_status"], "joint_worker_not_implemented")


class D121S1Tests(unittest.TestCase):
    def test_declared_sneak_fallback_is_proved_with_complete_typed_evidence(self):
        request, snapshot = _d120_fallback_request()
        result = prepare_fixed_motion(request, snapshot)
        self.assertIs(result.status, PreparedComputeStatus.PREPARED)
        self.assertIsNotNone(result.bundle)
        for branch in result.bundle.proof.branches:
            for tail in branch.safety_tails:
                self.assertEqual(tail.fallback.candidate.candidate_id, "sneak")
                self.assertEqual(tail.fallback.candidate.tick_input.sneak, True)
                self.assertEqual(tail.fallback.first_effect_tick,
                                 tail.first_effect_tick + tail.fallback.input_start_index)
                self.assertEqual(tail.fallback.permitted_window.earliest_start_tick,
                                 tail.fallback.first_effect_tick)
                self.assertEqual(tail.fallback.permitted_window.latest_start_tick,
                                 tail.fallback.first_effect_tick)
                self.assertEqual(len(tail.trajectory), len(tail.inputs) + 1)
                self.assertEqual(tuple(pos for pos, _ in tail.dependency_facts),
                                 tuple(pos for pos, _ in tail.dependency_versions))
                self.assertLessEqual(tail.expected_damage_points, request.damage_remaining_points)
                self.assertIsNotNone(tail.fallback.release_witness_input)
                self.assertIsNotNone(tail.fallback.release_witness_state)
                self.assertEqual(tail.fallback.release_witness_state.position[::2],
                                 tail.trajectory[-1].position[::2])

    def test_risk_interval_uses_the_recovered_sneak_tail_and_release_witness(self):
        from tests.motion_nav.test_d102_prepared_proof import ProofTests
        ProofTests.setUpClass()
        request = ProofTests.request
        sneak = FallbackCandidate(
            "sneak",
            TickInput(0., 0., False, True, False, request.entry_state.yaw_radians),
            40,
            FallbackTerminalMode.SNEAK_EDGE_STABLE,
        )
        result = prepare_fixed_motion(
            replace(request, fallback_candidates=(sneak,)), ProofTests.snapshot)
        self.assertIs(result.status, PreparedComputeStatus.PREPARED)
        self.assertIsNotNone(result.bundle)
        composite = tuple(
            tail
            for branch in result.bundle.proof.branches
            for tail in branch.safety_tails
            if tail.fallback.input_start_index is not None
            and tail.fallback.input_start_index > 1
        )
        self.assertTrue(composite)
        proof_dependencies = set(result.bundle.proof.dependency_facts)
        for tail in composite:
            witness = tail.fallback.release_witness_state
            self.assertIsNotNone(witness)
            self.assertEqual(witness.movement_tick_id,
                             tail.trajectory[-1].movement_tick_id + 1)
            self.assertTrue(set(tail.dependency_facts).issubset(proof_dependencies))

    def test_missing_or_unsafe_declared_fallback_is_typed_rejected(self):
        request, snapshot = _d120_fallback_request()
        with self.assertRaises(ContractViolation):
            replace(request, fallback_candidates=())
        for controls in (("neutral",), ("back",)):
            with self.subTest(controls=controls):
                rejected, same = _d120_fallback_request(controls=controls, snapshot=snapshot)
                result = prepare_fixed_motion(rejected, same)
                self.assertIs(result.status, PreparedComputeStatus.BLOCKED)

    def test_candidate_local_failure_does_not_hide_a_later_safe_candidate(self):
        import mc2p.motion_nav.prepared_motion_proof as proof_module
        request, snapshot = _d120_fallback_request()
        unavailable = FallbackCandidate("unavailable",
            TickInput(0., 1., False, True, False, request.entry_state.yaw_radians), 40,
            FallbackTerminalMode.SNEAK_EDGE_STABLE)
        request = replace(request, fallback_candidates=(
            unavailable, request.fallback_candidates[0]))
        original = proof_module._stop_tail
        for status in (PreparedComputeStatus.MISSING_INFORMATION,
                       PreparedComputeStatus.UNSUPPORTED):
            def injected(*args, **kwargs):
                candidate = args[3]
                if candidate.candidate_id == "unavailable":
                    raise proof_module._ScanFailure(status, ((99, 99, 99),))
                return original(*args, **kwargs)
            with self.subTest(status=status), mock.patch.object(
                    proof_module, "_stop_tail", side_effect=injected):
                result = prepare_fixed_motion(request, snapshot)
                self.assertIs(result.status, PreparedComputeStatus.PREPARED)
                self.assertTrue(all(tail.fallback.candidate.candidate_id == "sneak"
                                    for branch in result.bundle.proof.branches
                                    for tail in branch.safety_tails))

    def test_fallback_identity_input_tick_and_window_mutations_are_rejected(self):
        request, snapshot = _d120_fallback_request()
        bundle = prepare_fixed_motion(request, snapshot).bundle
        self.assertIsNotNone(bundle)
        tail = bundle.proof.branches[0].safety_tails[0]
        neutral = FallbackCandidate("neutral", replace(tail.fallback.candidate.tick_input, sneak=False))
        for mutation in (
            lambda: replace(tail.fallback, candidate=neutral,
                            release_witness_input=None, release_witness_state=None),
            lambda: replace(tail.fallback, first_effect_tick=tail.fallback.first_effect_tick + 1,
                            permitted_window=CandidateExecutionWindow(
                                tail.fallback.first_effect_tick + 1,
                                tail.fallback.first_effect_tick + 1)),
            lambda: replace(tail.fallback, permitted_window=CandidateExecutionWindow(
                tail.fallback.first_effect_tick, tail.fallback.first_effect_tick + 1)),
            lambda: replace(tail.fallback, release_witness_input=None,
                            release_witness_state=None),
            lambda: replace(tail.fallback, release_witness_input=replace(
                tail.fallback.release_witness_input, sneak=True)),
            lambda: replace(tail.fallback, release_witness_state=replace(
                tail.fallback.release_witness_state, sneaking=True)),
            lambda: replace(tail.fallback, release_witness_state=replace(
                tail.fallback.release_witness_state,
                velocity_blocks_per_tick=(0.2,
                    tail.fallback.release_witness_state.velocity_blocks_per_tick[1], 0.0))),
            lambda: replace(tail.fallback, release_witness_state=replace(
                tail.fallback.release_witness_state,
                position=(tail.fallback.release_witness_state.position[0],
                          tail.fallback.release_witness_state.position[1] + 1.0,
                          tail.fallback.release_witness_state.position[2]))),
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ContractViolation):
                replace(tail, fallback=mutation())

    def test_unknown_or_removed_intermediate_support_cannot_keep_the_proof(self):
        request, snapshot = _d120_fallback_request()
        bundle = prepare_fixed_motion(request, snapshot).bundle
        self.assertIsNotNone(bundle)
        tail = bundle.proof.branches[0].safety_tails[0]
        support = next(pos for pos, fact in tail.dependency_facts if fact.block is not None)
        air = next(fact for _, fact in snapshot.facts if fact.block is None
                   and fact.knowledge.value != "unknown")
        for fact, expected in ((UNKNOWN_CELL, PreparedComputeStatus.MISSING_INFORMATION),
                               (air, PreparedComputeStatus.BLOCKED)):
            changed = _snapshot_with_change(snapshot, support, fact)
            changed_request = replace(request, snapshot_identity=changed.identity)
            with self.subTest(expected=expected):
                self.assertIs(prepare_fixed_motion(changed_request, changed).status, expected)

    def test_damage_and_hardcoded_neutral_mutations_do_not_survive_bundle_validation(self):
        request, snapshot = _d120_fallback_request()
        bundle = prepare_fixed_motion(request, snapshot).bundle
        self.assertIsNotNone(bundle)
        branch = bundle.proof.branches[0]
        tail = branch.safety_tails[0]
        with self.assertRaises(ContractViolation):
            replace(bundle, proof=replace(bundle.proof, branches=(
                replace(branch, safety_tails=(replace(tail, expected_damage_points=1.0),)
                        + branch.safety_tails[1:]),
                *bundle.proof.branches[1:])))
        neutral = FallbackCandidate("neutral", replace(tail.fallback.candidate.tick_input, sneak=False))
        with self.assertRaises(ContractViolation):
            replace(tail, fallback=replace(tail.fallback, candidate=neutral))
        foreign_tail = replace(tail, fallback=replace(tail.fallback, owner_id="other-owner"))
        with self.assertRaises(ContractViolation):
            replace(bundle.proof, branches=(
                replace(branch, safety_tails=(foreign_tail,) + branch.safety_tails[1:]),
                *bundle.proof.branches[1:]))
        renamed = replace(tail.fallback.candidate, candidate_id="renamed-sneak")
        renamed_tail = replace(tail, fallback=replace(tail.fallback, candidate=renamed))
        renamed_branch = replace(branch, safety_tails=(renamed_tail,) + branch.safety_tails[1:])
        renamed_proof = replace(bundle.proof,
                                branches=(renamed_branch, *bundle.proof.branches[1:]))
        with self.assertRaises(ContractViolation):
            replace(bundle, proof=renamed_proof)

    def test_legacy_requests_still_declare_and_prove_neutral(self):
        from tests.motion_nav.test_d102_prepared_proof import ProofTests
        ProofTests.setUpClass()
        result = prepare_fixed_motion(ProofTests.request, ProofTests.snapshot)
        self.assertIs(result.status, PreparedComputeStatus.PREPARED)
        self.assertEqual(result.bundle.request.fallback_candidates[0].candidate_id, "neutral")
        self.assertTrue(all(tail.fallback.candidate.candidate_id == "neutral"
                            for branch in result.bundle.proof.branches
                            for tail in branch.safety_tails))


class D121OnlineSolverFallbackTests(unittest.TestCase):
    @staticmethod
    def _request(entry, controls):
        inputs = {
            "neutral": TickInput(0., 0., False, False, False, entry.yaw_radians),
            "sneak": TickInput(0., 0., False, True, False, entry.yaw_radians),
            "back": TickInput(-1., 0., False, False, False, entry.yaw_radians),
        }
        fallbacks = tuple(FallbackCandidate(
            name, inputs[name], 40,
            FallbackTerminalMode.SNEAK_EDGE_STABLE if name == "sneak"
            else FallbackTerminalMode.STANDARD_SUPPORT,
        ) for name in controls)
        return GapSolveRequest(
            (0, 1), LandingRegion(-4., 4., -10., 12., entry.position[1]),
            CandidateExecutionWindow(entry.movement_tick_id + 1,
                                     entry.movement_tick_id + 1),
            max_ticks=20, recovery_horizon_ticks=40,
            fallback_candidates=fallbacks,
        )

    @staticmethod
    def _entry_world():
        fixture = calibration.build_fixture()
        entry = calibration.nominal_entry(fixture)
        return entry, fixture.context.world

    def test_online_release_uses_declared_sneak_and_keeps_complete_tail(self):
        entry, world = self._entry_world()
        evidence = _release_recovery_evidence(
            (entry,), world, self._request(entry, ("sneak",)))

        self.assertIsNone(evidence.status)
        self.assertEqual(len(evidence.fallback_tails), 1)
        tail = evidence.fallback_tails[0]
        self.assertEqual(tail.boundary_index, 0)
        self.assertEqual(tail.candidate.candidate_id, "sneak")
        self.assertIs(tail.candidate.yaw_mode, FallbackYawMode.EXACT)
        self.assertTrue(tail.candidate.tick_input.sneak)
        self.assertEqual(len(tail.trajectory), len(tail.inputs) + 1)
        self.assertTrue(all(state.on_ground for state in tail.trajectory))
        self.assertIsNotNone(tail.release_witness_input)
        self.assertIsNotNone(tail.release_witness_state)
        self.assertEqual(tail.release_witness_state.position[::2],
                         tail.trajectory[-1].position[::2])
        self.assertTrue(set(tail.dependencies).issubset(evidence.dependencies))

    def test_online_release_rejects_neutral_and_back_on_d120_edge(self):
        entry, world = self._entry_world()
        for control in ("neutral", "back"):
            with self.subTest(control=control):
                evidence = _release_recovery_evidence(
                    (entry,), world, self._request(entry, (control,)))
                self.assertIs(evidence.status,
                              SolveStatus.NO_SOLUTION_WITHIN_SEARCH)
                self.assertEqual(evidence.fallback_tails, ())

    def test_candidate_local_unavailable_result_does_not_hide_sneak(self):
        import mc2p.motion_nav.motion_solver as solver
        entry, world = self._entry_world()
        request = self._request(entry, ("back", "sneak"))
        original = solver._prove_fallback_candidate
        for status in (SolveStatus.NEEDS_WORLD, SolveStatus.UNSUPPORTED):
            def injected(*args, **kwargs):
                candidate = args[2]
                if candidate.candidate_id == "back":
                    return solver._FallbackAttempt(
                        None, status, ((99, 99, 99),), ("injected",))
                return original(*args, **kwargs)
            with self.subTest(status=status), mock.patch.object(
                    solver, "_prove_fallback_candidate", side_effect=injected):
                evidence = solver._release_recovery_evidence(
                    (entry,), world, request)
                self.assertIsNone(evidence.status)
                self.assertEqual(
                    evidence.fallback_tails[0].candidate.candidate_id, "sneak")

    def test_online_result_binds_primary_delayed_and_revalidated_fallbacks(self):
        from tests.motion_nav.test_b10_gap_solver import fixture
        from mc2p.motion_nav.motion_solver import (
            revalidate_gap_motion, solve_one_cell_gap,
        )
        anchor, world, target, _ = fixture()
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12))
        solved = solve_one_cell_gap(anchor, world, request)
        self.assertIs(solved.status, SolveStatus.SOLVED)
        proof = solved.proof
        self.assertEqual(len(proof.fallback_tails), len(proof.trajectory))
        self.assertTrue(all(tail.candidate.candidate_id == "neutral"
                            for tail in proof.fallback_tails))
        self.assertTrue(all(
            len(variant.fallback_tails) == len(variant.trajectory)
            and all(tail.candidate.candidate_id == "neutral"
                    for tail in variant.fallback_tails)
            for variant in proof.delayed_start_variants))

        refreshed = revalidate_gap_motion(
            proof, anchor, world, CandidateExecutionWindow(11, 12))
        self.assertIs(refreshed.status, SolveStatus.SOLVED)
        self.assertEqual(refreshed.proof.fallback_candidates,
                         proof.fallback_candidates)
        self.assertEqual(len(refreshed.proof.fallback_tails),
                         len(refreshed.proof.trajectory))

    def test_online_fallback_tail_rejects_detached_input_or_release_witness(self):
        entry, world = self._entry_world()
        evidence = _release_recovery_evidence(
            (entry,), world, self._request(entry, ("sneak",)))
        tail = evidence.fallback_tails[0]
        wrong = TickInput(0., 0., False, False, False, entry.yaw_radians)
        with self.assertRaises(ContractViolation):
            replace(tail, inputs=(wrong,) + tail.inputs[1:])
        with self.assertRaises(ContractViolation):
            replace(tail, release_witness_state=replace(
                tail.release_witness_state,
                position=(tail.release_witness_state.position[0] + .01,
                          tail.release_witness_state.position[1],
                          tail.release_witness_state.position[2])))
        witness = tail.release_witness_state
        for mutation in (
            replace(witness, position=(witness.position[0],
                                       witness.position[1] + .01,
                                       witness.position[2])),
            replace(witness, position=(witness.position[0],
                                       witness.position[1],
                                       witness.position[2] + .01)),
            replace(witness, sneaking=True),
            replace(witness, velocity_blocks_per_tick=(
                .01, witness.velocity_blocks_per_tick[1], 0.)),
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ContractViolation):
                replace(tail, release_witness_state=mutation)

    def test_online_result_rejects_missing_fallback_dependency_or_extra_damage(self):
        from tests.motion_nav.test_b10_gap_solver import fixture
        from mc2p.motion_nav.motion_solver import solve_one_cell_gap
        anchor, world, target, _ = fixture()
        solved = solve_one_cell_gap(anchor, world, GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12)))
        proof = solved.proof
        dependency = proof.fallback_tails[0].dependencies[0]
        with self.assertRaises(ContractViolation):
            replace(proof, world_dependencies=tuple(
                value for value in proof.world_dependencies if value != dependency))
        damaged = replace(
            proof.fallback_tails[0],
            expected_damage_points=proof.maximum_expected_damage_points + 1.0)
        with self.assertRaises(ContractViolation):
            replace(proof, fallback_tails=(damaged,) + proof.fallback_tails[1:])

    def test_online_sneak_fallback_reports_unknown_support(self):
        prepared_request, snapshot = _d120_fallback_request()
        entry = prepared_request.entry_state
        request = self._request(entry, ("sneak",))
        initial = _release_recovery_evidence(
            (entry,), PhysicsWorldView.from_snapshot(snapshot), request)
        support = next(
            position for position in initial.fallback_tails[0].dependencies
            if dict(snapshot.facts)[position].block is not None)
        changed = _snapshot_with_change(snapshot, support, UNKNOWN_CELL)

        evidence = _release_recovery_evidence(
            (entry,), PhysicsWorldView.from_snapshot(changed), request)

        self.assertIs(evidence.status, SolveStatus.NEEDS_WORLD)
        self.assertIn(support, evidence.missing_cells)

    def test_online_sneak_fallback_checks_the_boundary_support_before_moving(self):
        import mc2p.motion_nav.motion_solver as solver
        entry, world = self._entry_world()
        original = solver._fallback_support_fraction

        def unsupported_boundary(state, *args):
            if state.movement_tick_id == entry.movement_tick_id:
                return 0.0, None
            return original(state, *args)

        with mock.patch.object(
                solver, "_fallback_support_fraction",
                side_effect=unsupported_boundary):
            evidence = solver._release_recovery_evidence(
                (entry,), world, self._request(entry, ("sneak",)))

        self.assertIs(evidence.status,
                      SolveStatus.NO_SOLUTION_WITHIN_SEARCH)

    def test_primary_and_delayed_fallback_tails_cannot_move_between_boundaries(self):
        from tests.motion_nav.test_b10_gap_solver import fixture
        from mc2p.motion_nav.motion_solver import solve_one_cell_gap
        anchor, world, target, _ = fixture()
        proof = solve_one_cell_gap(anchor, world, GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12))).proof
        primary = proof.fallback_tails
        moved = (replace(primary[1], boundary_index=0),
                 replace(primary[0], boundary_index=1), *primary[2:])
        with self.assertRaises(ContractViolation):
            replace(proof, fallback_tails=moved)

        delayed = proof.delayed_start_variants[0]
        tails = delayed.fallback_tails
        moved = (replace(tails[1], boundary_index=0),
                 replace(tails[0], boundary_index=1), *tails[2:])
        with self.assertRaises(ContractViolation):
            replace(delayed, fallback_tails=moved)

    def test_legacy_neutral_preserves_each_turning_exit_state_yaw(self):
        from tests.motion_nav.test_b10_gap_solver import fixture
        from mc2p.motion_nav.motion_solver import solve_one_cell_gap
        anchor, world, target, _ = fixture()
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
            exit_direction=(1, 0), exit_motion_ticks=1)
        proof = solve_one_cell_gap(anchor, world, request).proof
        tail = proof.fallback_tails[-1]

        current = proof.trajectory[-1]
        maximum_fall = current.fall_distance_blocks
        settled = False
        for recovery_tick in range(2 * request.recovery_horizon_ticks):
            if recovery_tick >= request.recovery_horizon_ticks and not settled:
                break
            command = TickInput(0., 0., False, False, False,
                                current.yaw_radians)
            calculated = step(current, command, world, JAVA_1_21_RULESET)
            self.assertIs(calculated.status, CalculationStatus.OK)
            current = calculated.next_state
            maximum_fall = max(maximum_fall, current.fall_distance_blocks)
            speed = (current.velocity_blocks_per_tick[0] ** 2
                     + current.velocity_blocks_per_tick[2] ** 2) ** .5
            if current.on_ground and speed <= .01 + 1.e-9:
                settled = True
            if current.on_ground and speed <= 1.e-9:
                break

        self.assertEqual(tail.trajectory[-1], current)
        self.assertTrue(all(
            value.movement_yaw_radians == tail.trajectory[index].yaw_radians
            for index, value in enumerate(tail.inputs)))
        self.assertIs(tail.candidate.yaw_mode,
                      FallbackYawMode.PRESERVE_CURRENT)

    def test_prepared_legacy_neutral_preserves_changed_boundary_yaw_and_counts(self):
        from tests.motion_nav.test_d102_prepared_proof import ProofTests
        import mc2p.motion_nav.prepared_motion_proof as prepared
        ProofTests.setUpClass()
        request = replace(ProofTests.request, fallback_candidates=None)
        candidate = request.fallback_candidates[0]
        self.assertIs(candidate.yaw_mode, FallbackYawMode.PRESERVE_CURRENT)
        state = replace(request.entry_state, yaw_radians=math.pi / 2)
        world = PhysicsWorldView.from_snapshot(ProofTests.snapshot)
        counter = prepared._RequestCounter(request.scan_budget)

        tail = prepared._stop_tail(
            counter, (state,), (), candidate, world,
            state.position[1], request.damage_remaining_points)

        current = state
        inputs = []
        states = [state]
        for _ in range(candidate.maximum_ticks):
            command = TickInput(0., 0., False, False, False,
                                current.yaw_radians)
            calculated = step(current, command, world, JAVA_1_21_RULESET)
            self.assertIs(calculated.status, CalculationStatus.OK)
            inputs.append(command)
            current = calculated.next_state
            states.append(current)
            speed = (current.velocity_blocks_per_tick[0] ** 2
                     + current.velocity_blocks_per_tick[2] ** 2) ** .5
            if current.on_ground and speed <= 1.e-9:
                break

        self.assertTrue(tail.safe)
        self.assertEqual(tail.inputs, tuple(inputs))
        self.assertEqual(tail.states, tuple(states))
        self.assertEqual(counter.physics_steps, len(inputs))
        self.assertEqual(counter.tail_steps, len(inputs))

    def test_preserve_current_yaw_has_one_canonical_placeholder(self):
        for yaw in (.5, -0.0):
            with self.subTest(yaw=yaw), self.assertRaises(ContractViolation):
                FallbackCandidate(
                    "bad-placeholder", TickInput(0., 0., False, False, False, yaw),
                    yaw_mode=FallbackYawMode.PRESERVE_CURRENT)
        canonical = preserve_current_neutral_fallback()
        self.assertEqual(canonical.tick_input.movement_yaw_radians, 0.)
        with self.assertRaises(ContractViolation):
            validate_fallback_candidates((
                canonical, replace(canonical, candidate_id="same-runtime-control"),
            ))

    def test_standard_fallback_tail_requires_grounded_zero_speed_terminal(self):
        from tests.motion_nav.test_b10_gap_solver import fixture
        from mc2p.motion_nav.motion_solver import solve_one_cell_gap
        anchor, world, target, _ = fixture()
        proof = solve_one_cell_gap(anchor, world, GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12))).proof
        tail = proof.fallback_tails[-1]
        terminal = tail.trajectory[-1]
        mutations = (
            replace(terminal, on_ground=False),
            replace(terminal, velocity_blocks_per_tick=(
                .01, terminal.velocity_blocks_per_tick[1], 0.)),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ContractViolation):
                replace(tail, trajectory=tail.trajectory[:-1] + (mutation,))


if __name__ == "__main__":
    unittest.main()
