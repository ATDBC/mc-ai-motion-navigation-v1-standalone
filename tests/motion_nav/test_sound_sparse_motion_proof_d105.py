"""D105 T1-A sound sparse proof contract tests."""
from dataclasses import replace
import importlib.util
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto import commitment as legacy
from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.measurement import _set_a
from experiments.motion_navigation.trajectory_proto.physics import trajectory_digest
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_types import CalculationStatus
from tests.motion_nav.test_trajectory_proto_commitment import (
    GAP_INPUTS,
    JUMP,
    NEUTRAL,
    WALK,
    boundary_fact,
    ledger,
    scan_request,
    world,
)


SPARSE_EXISTS = importlib.util.find_spec(
    "experiments.motion_navigation.trajectory_proto.sound_sparse_proof") is not None


class SoundSparseModuleAvailabilityTests(unittest.TestCase):
    def test_d105_sound_sparse_proof_module_exists(self):
        self.assertTrue(
            SPARSE_EXISTS,
            "D105 T1-A implementation module sound_sparse_proof is missing",
        )


@unittest.skipUnless(SPARSE_EXISTS, "D105 T1-A implementation has not been written")
class SoundSparseMotionProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from experiments.motion_navigation.trajectory_proto import sound_sparse_proof

        cls.sparse = sound_sparse_proof

    def _flat_fixture(self, length=10):
        fixture = m0_probe._fixture_for("flat_walk", "A3")
        inputs = (fixture.request.stop_input,) * length
        return fixture.request, fixture.world, inputs, _boundary_evidence(fixture.request, inputs)

    @staticmethod
    def _risk_boundaries(result):
        return [[(risk.last_abandon_boundary, risk.first_committed_boundary,
                  risk.recovered_boundary) for risk in branch.risk_intervals]
                for branch in result.proof.branches]

    def test_hidden_single_boundary_risk_is_not_skipped(self):
        request, local, inputs, evidence = self._flat_fixture()
        original = legacy._tail

        def hidden_risk(*args, **kwargs):
            tail = original(*args, **kwargs)
            return replace(tail, status=legacy.TailStatus.UNSAFE) if args[1] == 5 else tail

        with patch.object(legacy, "_tail", side_effect=hidden_risk), patch.object(
                self.sparse, "_tail", side_effect=hidden_risk):
            old = legacy.scan_commitment(request, inputs, local, evidence)
            new = self.sparse.scan_sound_sparse_commitment(request, inputs, local, evidence)

        self.assertIs(old.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        self.assertIs(new.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(self._risk_boundaries(old), [[(4, 5, 6)], [(4, 5, 6)]])
        self.assertEqual(self._risk_boundaries(new), self._risk_boundaries(old))

    def test_open_risk_omits_only_airborne_hypothetical_tails(self):
        request = scan_request(GAP_INPUTS)
        evidence = ledger(request, GAP_INPUTS, inflight=(JUMP,))
        result = self.sparse.scan_sound_sparse_commitment(
            request, GAP_INPUTS, world(gap=True), evidence)

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        branch = result.proof.branches[0]
        omitted = [decision.boundary for decision in branch.boundary_decisions
                   if decision.source is self.sparse.BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED]
        expected = [boundary for boundary in range(
            branch.risk_intervals[0].first_committed_boundary,
            branch.risk_intervals[0].recovered_boundary,
        ) if not branch.states[boundary].on_ground]
        self.assertEqual(omitted, expected)
        self.assertTrue(omitted)
        for decision in branch.boundary_decisions:
            if decision.boundary in omitted:
                self.assertIsNone(decision.tail)
                self.assertIsNone(decision.tail_status)
                self.assertFalse(decision.support_queried)
            else:
                self.assertIsNotNone(decision.tail)

    def test_frozen_a_gap_omits_every_final_open_risk_air_boundary(self):
        case = next(case for case in _set_a(4) if case[1] == "jump_gap_1:A3")
        _, _, request, local, inputs, *_ = case
        result = self.sparse.scan_sound_sparse_commitment(
            request, inputs, local, _boundary_evidence(request, inputs))

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        checked = 0
        for branch in result.proof.branches:
            for decision in branch.boundary_decisions:
                if decision.risk_open_before and not branch.states[decision.boundary].on_ground:
                    checked += 1
                    self.assertIs(
                        decision.source,
                        self.sparse.BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED,
                    )
        self.assertGreater(checked, 0)

    def test_frozen_a_gap_ignores_boundary_six_hypothetical_tail_failure(self):
        case = next(case for case in _set_a(4) if case[1] == "jump_gap_1:A3")
        _, _, request, local, inputs, *_ = case
        original = legacy._tail

        def unsupported_at_six(*args, **kwargs):
            if args[1] == 6:
                raise legacy._Incomplete(
                    legacy.ScanStatus.CANDIDATE_REJECTED,
                    legacy.CandidateRejection.UNSUPPORTED_PHYSICS,
                )
            return original(*args, **kwargs)

        with patch.object(self.sparse, "_tail", side_effect=unsupported_at_six):
            result = self.sparse.scan_sound_sparse_commitment(
                request, inputs, local, _boundary_evidence(request, inputs))

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        for branch in result.proof.branches:
            decision = branch.boundary_decisions[6]
            self.assertIs(
                decision.source,
                self.sparse.BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED,
            )

    def test_validator_rejects_tail_attached_to_final_open_risk_air_boundary(self):
        case = next(case for case in _set_a(4) if case[1] == "jump_gap_1:A3")
        _, _, request, local, inputs, *_ = case
        evidence = _boundary_evidence(request, inputs)
        compact = self.sparse.scan_sound_sparse_commitment(
            request, inputs, local, evidence).proof
        full = legacy.scan_commitment(request, inputs, local, evidence).proof
        branch = compact.branches[0]
        decisions = list(branch.boundary_decisions)
        decisions[6] = replace(
            decisions[6],
            source=self.sparse.BoundaryDecisionSource.OPEN_RISK_TAIL_EVALUATED,
            tail=full.branches[0].tails[6],
            dependencies=full.branches[0].tails[6].dependencies,
        )
        mutated = replace(
            compact,
            branches=(replace(branch, boundary_decisions=tuple(decisions)),)
                     + compact.branches[1:],
            trajectory_hash="",
        )
        mutated = replace(mutated, trajectory_hash=trajectory_digest(mutated))

        with self.assertRaises(ContractViolation):
            self.sparse.validate_sound_sparse_proof(mutated)

    def test_omitted_air_tail_cannot_reject_or_create_dependencies(self):
        request = scan_request(GAP_INPUTS)
        evidence = ledger(request, GAP_INPUTS, inflight=(JUMP,))
        original = legacy._tail

        def unsupported_if_hypothetical(*args, **kwargs):
            if args[1] == 2:
                raise legacy._Incomplete(
                    legacy.ScanStatus.CANDIDATE_REJECTED,
                    legacy.CandidateRejection.UNSUPPORTED_PHYSICS,
                )
            return original(*args, **kwargs)

        with patch.object(self.sparse, "_tail", side_effect=unsupported_if_hypothetical):
            result = self.sparse.scan_sound_sparse_commitment(
                request, GAP_INPUTS, world(gap=True), evidence)

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        decision = result.proof.branches[0].boundary_decisions[2]
        self.assertIs(decision.source,
                      self.sparse.BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED)
        self.assertEqual(decision.dependencies, ())

    def test_grounded_recovery_runs_tail_before_support_query(self):
        request = scan_request(GAP_INPUTS)
        evidence = ledger(request, GAP_INPUTS, inflight=(JUMP,))
        events = []
        original_tail = legacy._tail
        original_support = legacy._safe_support

        def ordered_tail(*args, **kwargs):
            events.append(("tail", args[1]))
            return original_tail(*args, **kwargs)

        def ordered_support(state, *args, **kwargs):
            events.append(("support", state.movement_tick_id))
            return original_support(state, *args, **kwargs)

        with patch.object(self.sparse, "_tail", side_effect=ordered_tail), patch.object(
                self.sparse, "_safe_support", side_effect=ordered_support):
            result = self.sparse.scan_sound_sparse_commitment(
                request, GAP_INPUTS, world(gap=True), evidence)

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        branch = result.proof.branches[0]
        recovered = branch.risk_intervals[0].recovered_boundary
        decision = branch.boundary_decisions[recovered]
        self.assertTrue(decision.support_queried)
        tail_event = events.index(("tail", recovered))
        support_event = next(index for index, event in enumerate(events)
                             if index > tail_event and event[0] == "support")
        self.assertLess(tail_event, support_event)

    def test_two_distinct_risks_and_compact_executable_tails_are_preserved(self):
        inputs = ((WALK, JUMP) + (WALK,) * 10 + (NEUTRAL,) * 9
                  + (WALK,) * 4 + (JUMP,) + (WALK,) * 10 + (NEUTRAL,) * 4)
        request = scan_request(inputs)
        rows = list(ledger(request, inputs, inflight=(JUMP,))[0])
        rows[25] = boundary_fact(request, 25, (JUMP,))
        local = world(gap=True, changed={(0, 0, 4): world().cell((0, 2, 4))})

        old = legacy.scan_commitment(request, inputs, local, (tuple(rows),))
        new = self.sparse.scan_sound_sparse_commitment(request, inputs, local, (tuple(rows),))

        self.assertEqual(self._risk_boundaries(new), self._risk_boundaries(old))
        self.assertEqual(self._risk_boundaries(new), [[(0, 1, 13), (24, 25, 37)]])
        branch = new.proof.branches[0]
        retained = {tail.boundary: tail for tail in branch.safety_tails}
        self.assertEqual(set(retained), {0, 13, 24, 37, len(inputs)})
        for tail in retained.values():
            self.assertGreaterEqual(tail.maximum_ticks, 1)
            self.assertEqual(tail.maximum_ticks, len(tail.inputs))
            self.assertEqual(tail.start_state, branch.states[tail.boundary])
            self.assertEqual(tail.end_state.movement_tick_id,
                             tail.start_state.movement_tick_id + len(tail.inputs))

    def test_t1_a_does_not_publish_unbound_deviation_reproof(self):
        self.assertFalse(hasattr(self.sparse, "prove_deviation_tail"))

    def test_all_required_tails_finish_before_recovery_support_queries(self):
        request, local, inputs, evidence = self._flat_fixture()
        original_tail = legacy._tail

        def ordered_failures(*args, **kwargs):
            tail = original_tail(*args, **kwargs)
            if args[1] == 1:
                return replace(tail, status=legacy.TailStatus.UNSAFE)
            if args[1] == 3:
                raise legacy._Incomplete(
                    legacy.ScanStatus.CANDIDATE_REJECTED,
                    legacy.CandidateRejection.UNSUPPORTED_PHYSICS,
                )
            return tail

        with patch.object(self.sparse, "_tail", side_effect=ordered_failures), patch.object(
                self.sparse, "_safe_support", side_effect=legacy._Incomplete(
                    legacy.ScanStatus.NEEDS_INFORMATION,
                    legacy.SearchReason.UNKNOWN_WORLD,
                    ((999, 0, 999),),
                )):
            result = self.sparse.scan_sound_sparse_commitment(
                request, inputs, local, evidence)

        self.assertIs(result.status, legacy.ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(result.reason, legacy.CandidateRejection.UNSUPPORTED_PHYSICS)

    def test_risk_from_boundary_zero_does_not_fabricate_entry_safe_tail(self):
        inputs = (NEUTRAL,) * 10
        request = scan_request(inputs)
        local = world()
        evidence = ledger(request, inputs)
        original_tail = legacy._tail

        def unsafe_entry(*args, **kwargs):
            tail = original_tail(*args, **kwargs)
            return replace(tail, status=legacy.TailStatus.UNSAFE) if args[1] == 0 else tail

        with patch.object(self.sparse, "_tail", side_effect=unsafe_entry):
            result = self.sparse.scan_sound_sparse_commitment(
                request, inputs, local, evidence)

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        branch = result.proof.branches[0]
        self.assertEqual(
            [(risk.last_abandon_boundary, risk.first_committed_boundary,
              risk.recovered_boundary) for risk in branch.risk_intervals],
            [(None, 0, 1)],
        )
        self.assertNotIn(0, {tail.boundary for tail in branch.safety_tails})

    def test_required_main_tail_and_support_failures_keep_typed_results(self):
        request, local, inputs, evidence = self._flat_fixture()
        original_tail = legacy._tail

        with patch.object(self.sparse, "_tail", side_effect=legacy._Incomplete(
                legacy.ScanStatus.CANDIDATE_REJECTED,
                legacy.CandidateRejection.TAIL_NOT_SETTLED)):
            tail_result = self.sparse.scan_sound_sparse_commitment(
                request, inputs, local, evidence)
        self.assertIs(tail_result.status, legacy.ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(tail_result.reason, legacy.CandidateRejection.TAIL_NOT_SETTLED)

        missing_world = m0_probe._fixture_for("flat_walk", "A3").world
        calculated = replace(
            legacy.CountedPhysics(request.budget).step(
                request.entry_states[0], inputs[0], missing_world),
            status=CalculationStatus.UNSUPPORTED,
            next_state=None,
            unsupported_reasons=("fixture_unsupported",),
            resource_update=None,
        )
        with patch.object(self.sparse.CountedPhysics, "step", return_value=calculated):
            unsupported = self.sparse.scan_sound_sparse_commitment(
                request, inputs, local, evidence)
        self.assertIs(unsupported.status, legacy.ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(unsupported.reason, legacy.CandidateRejection.UNSUPPORTED_PHYSICS)

    def test_compact_proof_digest_detects_mutation(self):
        request, local, inputs, evidence = self._flat_fixture()
        proof = self.sparse.scan_sound_sparse_commitment(
            request, inputs, local, evidence).proof
        self.sparse.validate_sound_sparse_proof(proof)
        with self.assertRaises(ContractViolation):
            self.sparse.validate_sound_sparse_proof(replace(proof, trajectory_hash="0" * 64))

    def test_scanner_computes_the_complete_proof_digest_once(self):
        request, local, inputs, evidence = self._flat_fixture()
        original = self.sparse.trajectory_digest
        complete_proof_digests = 0

        def counted(value):
            nonlocal complete_proof_digests
            if type(value) is self.sparse.SoundSparseProof:
                complete_proof_digests += 1
            return original(value)

        with patch.object(self.sparse, "trajectory_digest", side_effect=counted):
            result = self.sparse.scan_sound_sparse_commitment(
                request, inputs, local, evidence)

        self.assertIs(result.status, legacy.ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(complete_proof_digests, 1)


if __name__ == "__main__":
    unittest.main()
