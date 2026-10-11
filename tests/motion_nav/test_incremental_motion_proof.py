from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.commitment import (
    CandidateRejection,
    ScanStatus,
    scan_commitment,
)
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.physics import trajectory_digest
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.physics_types import CalculationStatus
from mc2p.motion_nav.world_model import CellFact, CellKnowledge, WorldView


class IncrementalMotionProofTests(unittest.TestCase):
    def setUp(self):
        mixed = m0_probe.build_mixed_action_fixture()
        self.fixture = mixed.fixture
        self.inputs = mixed.expected_inputs

    def test_incremental_result_matches_independent_scanner_signature(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally

        evidence = _boundary_evidence(self.fixture.request, self.inputs)
        reference = scan_commitment(self.fixture.request, self.inputs, self.fixture.world, evidence)
        actual = prove_incrementally(self.fixture.request, self.inputs, self.fixture.world, evidence)
        self.assertEqual((actual.status, actual.reason), (reference.status, reference.reason))
        self.assertEqual(
            [[(i.last_abandon_boundary, i.first_committed_boundary, i.recovered_boundary)
              for i in branch.risk_intervals] for branch in actual.branches],
            [[(i.last_abandon_boundary, i.first_committed_boundary, i.recovered_boundary)
              for i in branch.risk_intervals] for branch in reference.proof.branches],
        )

    def test_frozen_independent_reference_signatures_match(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally

        data = (Path(__file__).resolve().parents[2]
                / "experiments/motion_navigation/trajectory_proto/data")
        expected = json.loads((data / "d098-reference-signatures-v1.json").read_text(encoding="utf-8"))
        pre_p1 = json.loads((data / "d097a-pre-p1-equivalence-sample.json").read_text(encoding="utf-8"))
        pre_by_case = {row["id"]: row for row in pre_p1["rows"]}
        inputs_by_key = json.loads((data / "d097a-set-a-sample.json").read_text(encoding="utf-8"))["inputs"]
        for frozen in expected["rows"]:
            with self.subTest(frozen["case"]):
                scenario, tier = frozen["case"].split(":")
                fixture = m0_probe._fixture_for(scenario, tier or None)
                inputs = tuple(type(fixture.request.stop_input)(*row) for row in inputs_by_key[frozen["case"]])
                actual = prove_incrementally(fixture.request, inputs, fixture.world,
                                             _boundary_evidence(fixture.request, inputs))
                self.assertEqual(actual.status.value, frozen["status"])
                self.assertEqual(None if actual.reason is None else actual.reason.value, frozen["reason"])
                self.assertEqual(actual.counts.physics_steps, frozen["counts"]["physics_steps"])
                self.assertEqual(actual.counts.nodes, frozen["counts"]["nodes"])
                self.assertEqual(actual.counts.tail_ticks, frozen["counts"]["tail_ticks"])
                self.assertEqual([
                    [[interval.last_abandon_boundary, interval.first_committed_boundary,
                      interval.recovered_boundary, interval.last_abandon_tick,
                      interval.first_committed_tick, interval.commitment_effect_tick,
                      interval.recovered_tick] for interval in branch.risk_intervals]
                    for branch in actual.branches
                ], [branch["risk"] for branch in frozen["branches"]])
                self.assertEqual([trajectory_digest(branch.states) for branch in actual.branches],
                                 [branch["states_hash"] for branch in frozen["branches"]])
                self.assertEqual(actual.dependencies,
                                 frozenset(tuple(row[0]) for row in frozen["dependencies"]))
                earlier = pre_by_case[frozen["case"]]
                self.assertEqual(actual.counts.physics_steps, earlier["scan_steps"])
                self.assertEqual(actual.counts.nodes, earlier["scan_nodes"])
                self.assertEqual([
                    [[i.last_abandon_boundary, i.first_committed_boundary,
                      i.recovered_boundary] for i in branch.risk_intervals]
                    for branch in actual.branches
                ], earlier["intervals_scan"])

    def test_independent_reference_signatures_are_reproducible(self):
        from experiments.motion_navigation.trajectory_proto.reference_signature_generator import (
            OUTPUT,
            generate_payload,
            serialized_payload,
        )

        stored = json.loads(OUTPUT.read_text(encoding="utf-8"))
        self.assertEqual(stored, generate_payload())
        self.assertEqual(OUTPUT.read_text(encoding="utf-8"), serialized_payload())
        self.assertEqual(stored["provenance"]["baseline_commit"], "d01d378f")
        self.assertEqual(stored["provenance"]["scanner_path"],
                         "experiments/motion_navigation/trajectory_proto/commitment.py")

    def test_truncated_air_path_reports_unrecovered_risk_or_earlier_safety_rejection(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally

        fixture = m0_probe._fixture_for("gap_start_1_width_1", "A3")
        result = m0_probe.run_case("gap_start_1_width_1", "A3", options=m0_probe.FORMAL_OPTIONS)
        inputs = result.inputs[:12]
        proof = prove_incrementally(fixture.request, inputs, fixture.world,
                                    _boundary_evidence(fixture.request, inputs))
        self.assertNotEqual(proof.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertIn(proof.reason, {
            CandidateRejection.UNRECOVERED_RISK,
            CandidateRejection.FINAL_EXIT_NOT_GROUNDED,
            CandidateRejection.TAIL_NOT_SETTLED,
        })

    def test_trace_factory_and_reuse_validation_reject_tampering(self):
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            TraceReuseStatus,
            build_controller_trace,
            trace_payload_digest_for_test,
            validate_trace_for_reuse,
        )

        trace = build_controller_trace(self.fixture.request, self.inputs, self.fixture.world)
        valid = validate_trace_for_reuse(trace, self.fixture.request, self.inputs, self.fixture.world)
        self.assertEqual(valid.status, TraceReuseStatus.VALID)
        forged = replace(trace, request_id="forged-request")
        self.assertEqual(validate_trace_for_reuse(
            forged, self.fixture.request, self.inputs, self.fixture.world).status,
            TraceReuseStatus.REJECTED,
        )
        changed = (self.fixture.request.stop_input,) + self.inputs[1:]
        self.assertEqual(validate_trace_for_reuse(
            trace, self.fixture.request, changed, self.fixture.world).status,
            TraceReuseStatus.REJECTED,
        )
        ordinary_rehash = replace(forged, payload_hash=trace_payload_digest_for_test(forged))
        self.assertEqual(validate_trace_for_reuse(
            ordinary_rehash, self.fixture.request, self.inputs, self.fixture.world).status,
            TraceReuseStatus.REJECTED,
        )
        position, fact = next((position, fact) for position, fact in trace.dependencies
                              if fact.knowledge is CellKnowledge.BLOCK)
        facts = {pos: self.fixture.world.cell(pos) for pos, _ in trace.dependencies}
        facts[position] = CellFact(CellKnowledge.AIR, fact.stamp, None)
        changed_world = type(self.fixture.world)(
            WorldView.detached(self.fixture.world.session,
                               self.fixture.world.geometry_revision, 0, facts),
            self.fixture.world.ruleset,
        )
        self.assertEqual(validate_trace_for_reuse(
            trace, self.fixture.request, self.inputs, changed_world).status,
            TraceReuseStatus.REJECTED,
        )

    def test_valid_trace_reuses_main_rollout_without_changing_proof(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            TraceReuseStatus,
            build_controller_trace,
        )

        request, world = self.fixture.request, self.fixture.world
        evidence = _boundary_evidence(request, self.inputs)
        reference = prove_incrementally(request, self.inputs, world, evidence)
        counter = CountedPhysics(request.budget)
        trace = build_controller_trace(request, self.inputs, world, counter=counter)
        trace_steps = counter.physics_steps
        reused = prove_incrementally(request, self.inputs, world, evidence,
                                     counter=counter, rollout_trace=trace)
        self.assertIs(reused.trace_reuse.status, TraceReuseStatus.VALID)
        self.assertEqual(counter.physics_steps, reference.counts.physics_steps)
        self.assertEqual(reused.counts.physics_steps, reference.counts.physics_steps)
        self.assertGreater(trace_steps, 0)
        self.assertEqual(
            (reused.status, reused.reason, reused.dependencies),
            (reference.status, reference.reason, reference.dependencies),
        )
        self.assertEqual(
            [trajectory_digest(branch.states) for branch in reused.branches],
            [trajectory_digest(branch.states) for branch in reference.branches],
        )

    def test_rejected_trace_falls_back_to_full_rollout_explicitly(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            TraceReuseStatus,
            build_controller_trace,
        )

        request, world = self.fixture.request, self.fixture.world
        counter = CountedPhysics(request.budget)
        trace = build_controller_trace(request, self.inputs, world, counter=counter)
        forged = replace(trace, request_id="forged")
        result = prove_incrementally(
            request, self.inputs, world, _boundary_evidence(request, self.inputs),
            counter=counter, rollout_trace=forged,
        )
        self.assertIs(result.trace_reuse.status, TraceReuseStatus.REJECTED)
        self.assertEqual(result.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertGreater(result.counts.physics_steps,
                           prove_incrementally(request, self.inputs, world,
                                               _boundary_evidence(request, self.inputs)
                                               ).counts.physics_steps)

    def test_trace_binds_all_request_semantics_and_actual_world_identity(self):
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            TraceReuseStatus,
            build_controller_trace,
            validate_trace_for_reuse,
        )

        request, world = self.fixture.request, self.fixture.world
        trace = build_controller_trace(request, self.inputs, world)
        changed_requests = (
            replace(request, budget=SearchBudget(
                request.budget.max_nodes + 1,
                request.budget.max_physics_steps,
                request.budget.max_trajectory_ticks,
                request.budget.max_timing_branches,
            )),
            replace(request, input_prefix=(self.inputs[0],)),
            replace(request, route_guidance=((0.25, 1.0, 2.0),)),
            replace(request,
                    supported_inputs=request.supported_inputs[:3]
                                     + tuple(reversed(request.supported_inputs[3:])),
                    input_tiers=request.input_tiers[:-1] + (
                        replace(request.input_tiers[-1],
                                inputs=request.supported_inputs[:3]
                                       + tuple(reversed(request.supported_inputs[3:]))),)),
        )
        for changed in changed_requests:
            with self.subTest(changed=changed):
                self.assertIs(
                    validate_trace_for_reuse(trace, changed, self.inputs, world).status,
                    TraceReuseStatus.REJECTED,
                )

        speed_request = replace(
            request,
            goal=replace(request.goal, maximum_terminal_speed_blocks_per_second=1.),
            minimum_terminal_speed_blocks_per_second=.1,
        )
        speed_trace = build_controller_trace(speed_request, self.inputs, world)
        self.assertIs(validate_trace_for_reuse(
            speed_trace, replace(speed_request,
                                 minimum_terminal_speed_blocks_per_second=.2),
            self.inputs, world).status, TraceReuseStatus.REJECTED)

        other_session = replace(request.world_session, value="different-session")
        session_world = type(world)(WorldView.detached(other_session, world.geometry_revision, 0, {}),
                                    world.ruleset)
        revision_world = type(world)(WorldView.detached(world.session,
                                         world.geometry_revision + 1, 0, {}), world.ruleset)
        for changed_world in (session_world, revision_world):
            self.assertIs(validate_trace_for_reuse(
                trace, request, self.inputs, changed_world).status, TraceReuseStatus.REJECTED)

    def test_trace_factory_rejects_illegal_candidate_before_issuing(self):
        from experiments.motion_navigation.trajectory_proto.rollout_trace import build_controller_trace

        unsupported = replace(self.inputs[0], movement_yaw_radians=1.2345)
        with self.assertRaisesRegex(Exception, "shared prefix|supported inputs"):
            build_controller_trace(self.fixture.request, (unsupported,) + self.inputs[1:],
                                   self.fixture.world)
        prefixed = replace(self.fixture.request, input_prefix=(self.inputs[0],))
        with self.assertRaisesRegex(Exception, "shared prefix|supported inputs"):
            build_controller_trace(prefixed, (self.fixture.request.stop_input,) + self.inputs[1:],
                                   self.fixture.world)

    def test_damage_examples_are_explicit(self):
        from experiments.motion_navigation.trajectory_proto.rollout_trace import damage_points

        base = self.fixture.request.anchor_state
        zero = (base, replace(base, movement_tick_id=base.movement_tick_id + 1))
        def landed(distance, tick):
            return (replace(base, movement_tick_id=base.movement_tick_id + tick,
                            on_ground=False, fall_distance_blocks=distance),
                    replace(base, movement_tick_id=base.movement_tick_id + tick + 1,
                            on_ground=True, fall_distance_blocks=0.))

        self.assertEqual(damage_points(zero), 0.)
        self.assertEqual(damage_points(landed(5.2, 2)), 3.)
        self.assertEqual(damage_points(landed(7., 4)), 4.)
        self.assertEqual(damage_points((replace(base, on_ground=False,
                                                       fall_distance_blocks=8.),)), 5.)
        self.assertEqual(damage_points(landed(5.2, 6) + landed(7., 8)), 7.)

    def test_incremental_damage_matches_explicit_damage_examples(self):
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            DamagePrefix,
            advance_damage,
            damage_points,
        )

        base = self.fixture.request.anchor_state

        def state(distance, tick, *, on_ground):
            return replace(base, movement_tick_id=base.movement_tick_id + tick,
                           on_ground=on_ground, fall_distance_blocks=distance)

        examples = (
            (base, state(0., 1, on_ground=True)),
            (state(5.2, 2, on_ground=False), state(0., 3, on_ground=True)),
            (state(7., 4, on_ground=False), state(0., 5, on_ground=True)),
            (state(8., 6, on_ground=False),),
            (state(5.2, 7, on_ground=False), state(0., 8, on_ground=True),
             state(7., 9, on_ground=False), state(0., 10, on_ground=True)),
        )
        expected = (0., 3., 4., 5., 7.)
        for states, points in zip(examples, expected):
            with self.subTest(points=points):
                prefix = DamagePrefix(0., 0.)
                for item in states:
                    prefix = advance_damage(prefix, item)
                self.assertEqual(prefix.points, points)
                self.assertEqual(prefix.points, damage_points(states))

    def test_stopping_tail_updates_damage_once_per_new_state(self):
        from experiments.motion_navigation.trajectory_proto.commitment import ScanOptions
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            DamagePrefix,
            DamageWork,
            stopping_tail,
        )

        request, world = self.fixture.request, self.fixture.world
        counter = CountedPhysics(request.budget)
        work = DamageWork()
        tail = stopping_tail(
            counter, 0, request.anchor_state, DamagePrefix(0., 0.), (),
            request.stop_input, world, request.anchor_state.position[1],
            request.task_damage_budget.maximum_expected_damage_points,
            ScanOptions(), damage_work=work,
        )
        self.assertEqual(work.state_updates, len(tail.states) - 1)

    def test_incremental_proof_reports_one_damage_update_per_processed_state(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally

        result = prove_incrementally(
            self.fixture.request, self.inputs, self.fixture.world,
            _boundary_evidence(self.fixture.request, self.inputs),
        )
        expected = 0
        for index, branch in enumerate(result.branches):
            expected += len(self.fixture.request.branch_preludes[index]) + len(branch.states)
            expected += sum(len(tail.states) - 1 for tail in branch.tails.values())
        self.assertEqual(result.damage_state_updates, expected)
        self.assertGreater(result.legacy_damage_state_visits, result.damage_state_updates)

    def test_all_seven_faults_are_declared(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import FAULTS

        self.assertEqual(FAULTS, frozenset({
            "no_landing_proof", "unknown_as_free", "one_branch", "ignore_inflight",
            "accept_stale", "ignore_damage", "swap_locked_command",
        }))

    def test_unrecovered_risk_and_invalid_physics_are_typed(self):
        from experiments.motion_navigation.trajectory_proto.commitment import StopTail, TailStatus
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        from experiments.motion_navigation.trajectory_proto.rollout_trace import (
            RolloutFailure,
            calculated_step,
            risk_intervals,
        )

        state = self.fixture.request.anchor_state
        tail = StopTail(0, TailStatus.UNSAFE, (), (state,), (), 0., state.movement_tick_id)
        with self.assertRaises(RolloutFailure) as caught:
            risk_intervals((tail,), CountedPhysics(self.fixture.request.budget),
                           self.fixture.world, set())
        self.assertEqual(caught.exception.reason, CandidateRejection.UNRECOVERED_RISK)

        fake = type("Result", (), {
            "dependencies": (), "status": CalculationStatus.INVALID_INPUT,
            "missing_cells": (), "next_state": state,
        })()
        counter = CountedPhysics(self.fixture.request.budget)
        with mock.patch.object(counter, "step", return_value=fake):
            with self.assertRaises(RolloutFailure) as invalid:
                calculated_step(counter, state, self.inputs[0], self.fixture.world, set())
        self.assertEqual(invalid.exception.reason, CandidateRejection.INVALID_PHYSICS)

    def test_mismatched_counter_budget_is_rejected(self):
        from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
        from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics

        wrong = CountedPhysics(SearchBudget(8, 80, 40, 2))
        with self.assertRaisesRegex(Exception, "unchanged total budget"):
            prove_incrementally(self.fixture.request, self.inputs, self.fixture.world,
                                _boundary_evidence(self.fixture.request, self.inputs), counter=wrong)

    def test_rolling_session_computes_only_one_actionable_decision_at_a_time(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import (
            RollingProofStatus,
            start_rolling_proof,
        )
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        from experiments.motion_navigation.trajectory_proto.rollout_trace import build_controller_trace

        counter = CountedPhysics(self.fixture.request.budget)
        trace = build_controller_trace(self.fixture.request, self.inputs, self.fixture.world,
                                       counter=counter)
        session = start_rolling_proof(
            self.fixture.request,
            self.inputs,
            self.fixture.world,
            _boundary_evidence(self.fixture.request, self.inputs),
            counter=counter,
            rollout_trace=trace,
            runahead_steps=24,
        )
        self.assertEqual(session.actionable_decisions, ())
        first = session.advance()
        self.assertIs(first.status, RollingProofStatus.PERMITTED)
        self.assertEqual(len(session.actionable_decisions), 1)
        self.assertIs(first.decision, session.actionable_decisions[0])
        second = session.advance()
        self.assertEqual(len(session.actionable_decisions), 2)
        self.assertIs(second.decision, session.actionable_decisions[1])

    def test_each_fault_changes_a_protected_observation(self):
        from experiments.motion_navigation.trajectory_proto.incremental_proof import (
            ExecutionCheck,
            PermitStatus,
            check_execution,
            prove_incrementally,
            validate_permit,
        )

        evidence = _boundary_evidence(self.fixture.request, self.inputs)
        baseline = prove_incrementally(self.fixture.request, self.inputs, self.fixture.world, evidence)
        base_signature = (
            baseline.status, baseline.reason,
            [[(i.first_committed_boundary, i.recovered_boundary) for i in b.risk_intervals]
             for b in baseline.branches],
        )
        # Each mutation has a frozen witness.  They need not all alter the same candidate.
        witnesses = {
            "ignore_inflight": (self.fixture, self.inputs),
            "one_branch": (self.fixture, self.inputs),
        }
        gap = m0_probe._fixture_for("jump_gap_1", "A3")
        gap_inputs = m0_probe.run_case("jump_gap_1", "A3", options=m0_probe.FORMAL_OPTIONS).inputs
        witnesses["no_landing_proof"] = (gap, gap_inputs)
        for fault, (fixture, inputs) in witnesses.items():
            rows = _boundary_evidence(fixture.request, inputs)
            normal = prove_incrementally(fixture.request, inputs, fixture.world, rows)
            mutated = prove_incrementally(fixture.request, inputs, fixture.world, rows,
                                          faults={fault})
            signature = lambda value: (
                value.status, value.reason,
                [[(i.last_abandon_boundary, i.first_committed_boundary, i.recovered_boundary)
                  for i in b.risk_intervals] for b in value.branches],
                [[(tail.status, tail.inputs) for _, tail in sorted(b.tails.items())]
                 for b in value.branches],
            )
            self.assertNotEqual(signature(mutated), signature(normal), fault)

        unknown_fixture = m0_probe._fixture_for("unknown_landing", None)
        source = m0_probe.run_case("gap_start_1_width_1", "A3", options=m0_probe.FORMAL_OPTIONS).inputs
        normal_unknown = prove_incrementally(
            unknown_fixture.request, source, unknown_fixture.world,
            _boundary_evidence(unknown_fixture.request, source))
        fault_unknown = prove_incrementally(
            unknown_fixture.request, source, unknown_fixture.world,
            _boundary_evidence(unknown_fixture.request, source), faults={"unknown_as_free"})
        self.assertNotEqual((fault_unknown.status, fault_unknown.reason),
                            (normal_unknown.status, normal_unknown.reason))

        # A four-block drop costs damage.  Ignoring it must change rejection into permission.
        base = m0_probe._fixture_for("flat_walk", "A3")
        stone = base.world.cell((0, 0, 0))
        facts = {}
        for x in range(-3, 4):
            for y in range(-50, 8):
                for z in range(-3, 13):
                    fact = base.world.cell((x, y, z))
                    if fact.knowledge is not CellKnowledge.UNKNOWN:
                        facts[(x, y, z)] = fact
        for x in range(-3, 4):
            for z in range(3, 13):
                facts[(x, 0, z)] = CellFact(CellKnowledge.AIR, stone.stamp, None)
                facts[(x, -4, z)] = stone
        drop_world = type(base.world)(WorldView.detached(base.world.session,
                                      base.world.geometry_revision, 0, facts), base.world.ruleset)
        request = replace(base.request, request_id="fault-drop",
                          task_damage_budget=TaskDamageBudget("fault-test", 0.))
        walk = request.supported_inputs[0]
        drop_inputs = (walk,) * 16 + (request.stop_input,) * 24
        drop_evidence = _boundary_evidence(request, drop_inputs)
        normal_drop = prove_incrementally(request, drop_inputs, drop_world, drop_evidence)
        ignored_drop = prove_incrementally(request, drop_inputs, drop_world, drop_evidence,
                                           faults={"ignore_damage"})
        self.assertEqual(normal_drop.reason, CandidateRejection.DAMAGE_ALLOWANCE)
        self.assertNotEqual((ignored_drop.status, ignored_drop.reason),
                            (normal_drop.status, normal_drop.reason))

        permit = baseline.permits[0]
        revised = replace(self.fixture.request, anchor_id="changed-anchor")
        self.assertIs(validate_permit(permit, revised, self.fixture.world).status,
                      PermitStatus.STALE)
        self.assertIs(validate_permit(permit, revised, self.fixture.world,
                                      faults={"accept_stale"}).status, PermitStatus.VALID)
        different = self.fixture.request.stop_input
        if different == permit.commands[0]:
            different = self.inputs[0]
        self.assertIs(check_execution(permit, different), ExecutionCheck.COMMAND_MISMATCH)
        self.assertIs(check_execution(permit, different, faults={"swap_locked_command"}),
                      ExecutionCheck.ALLOWED)


if __name__ == "__main__":
    unittest.main()
