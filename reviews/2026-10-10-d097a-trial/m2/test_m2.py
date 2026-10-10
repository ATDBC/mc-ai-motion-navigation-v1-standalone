"""M2 tests: lazy proof vs the project's full commitment scanner, permits and executor gate.

Run:  PYTHONPATH=<checkout>:<reviews dir>:<reviews dir>/m2 python3 -m unittest test_m2 -v
Mutation: D097A_FAULT=<fault>[,<fault>] makes every lazy/permit/executor call below carry that
fault flag (see incremental.FAULTS); run_mutation.py runs the suite once per fault.
"""
from __future__ import annotations

from dataclasses import replace
import os
import unittest

from experiments.motion_navigation.trajectory_proto.commitment import (
    CandidateRejection, ScanStatus, TailStatus, scan_commitment,
)
from experiments.motion_navigation.trajectory_proto.contracts import SearchReason, TimingBranch
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from mc2p.motion_nav.world_model import Aabb, CellFact, CellKnowledge, WorldSessionId

import candidates as K
import common
import equiv
import incremental as I

FAULTS = I.check_faults(name for name in os.environ.get("D097A_FAULT", "").split(",") if name)
A = {}

# Hand-found candidate on the two-hole world (candidates.double_gap_fixture): the body lands on the
# middle platform and its next command is already a jump, so the second hole must stay inside the
# FIRST risk interval.  Found by the differential search in logs/witness_search.log.
DOUBLE_HOP_LABELS = "W J W W W W W W W W W W W SJ W W N N N N N N N N N N N N N N N N"


def setUpModule():
    A.update(common.load_set_a())


def fx(key):
    scenario_id, tier_id = key.split(":")
    return common.fixture_for(scenario_id, tier_id or None)


def both(request, inputs, world, evidence=None):
    evidence = _boundary_evidence(request, inputs) if evidence is None else evidence
    scan = scan_commitment(request, inputs, world, evidence)
    lazy = I.lazy_prove(request, inputs, world, evidence, faults=FAULTS)
    return scan, lazy


def interval_keys(branches):
    return [[equiv.interval_key(i) for i in b.risk_intervals] for b in branches]


class Equivalence(unittest.TestCase):
    KEYS = ("mixed_ground_jump_air:", "gap_start_1_width_2:A5", "gap_start_4_width_3:A15",
            "jump_gap_continue:A3", "jump_up_after_turn:A15", "flat_sprint:A5", "turn_90:A15",
            "jump_up_straight:A3")

    def test_frozen_matrix_subset_matches_scanner_exactly(self):
        for key in self.KEYS:
            with self.subTest(key):
                request, world = fx(key).request, fx(key).world
                scan, lazy = both(request, A[key], world)
                row = equiv.compare(scan, lazy)
                self.assertEqual(scan.status, ScanStatus.VERIFIED_CANDIDATE)
                self.assertTrue(row["status_match"], row)
                self.assertTrue(row["equivalent"], row)
                self.assertEqual(interval_keys(scan.proof.branches), interval_keys(lazy.branches))
                self.assertEqual([b.risk_intervals for b in scan.proof.branches],
                                 [b.risk_intervals for b in lazy.branches])

    def test_lazy_does_the_same_physics_work_as_the_scanner_on_verified_candidates(self):
        for key in ("mixed_ground_jump_air:", "gap_start_1_width_1:A3", "flat_walk:A3"):
            with self.subTest(key):
                scan, lazy = both(fx(key).request, A[key], fx(key).world)
                self.assertEqual(lazy.status, ScanStatus.VERIFIED_CANDIDATE)
                self.assertEqual((lazy.counts.physics_steps, lazy.counts.nodes),
                                 (scan.counts.physics_steps, scan.counts.nodes))
                self.assertEqual(lazy.dependencies,
                                 frozenset(p for p, _ in scan.proof.dependency_facts))

    def test_mixed_counterexample_has_one_commit_covering_both_branches(self):
        key = "mixed_ground_jump_air:"
        request, world = fx(key).request, fx(key).world
        scan, lazy = both(request, A[key], world)
        self.assertEqual(interval_keys(lazy.branches), [[(0, 1, 13)], [(0, 1, 13)]])
        kinds = [d.kind for d in lazy.decisions]
        self.assertEqual(kinds, ["plan", "tick", "commit"] + ["tick"] * 7 + ["final"])
        commit = lazy.decisions[2]
        self.assertEqual((commit.boundary, commit.end_boundary, commit.permit.locked_count), (1, 13, 12))
        # every command is permitted exactly once, in order
        sent = tuple(c for d in lazy.decisions if d.permit for c in d.permit.commands)
        self.assertEqual(sent, A[key])

    def test_in_flight_command_is_part_of_the_tail(self):
        key = "mixed_ground_jump_air:"
        request, world = fx(key).request, fx(key).world
        scan, lazy = both(request, A[key], world)
        for branch in lazy.branches:
            self.assertEqual(branch.tails[0].status, TailStatus.SAFE_STOP)
            self.assertEqual(branch.tails[1].status, TailStatus.UNSAFE)    # sprint-jump already in flight
            self.assertEqual(branch.tails[1].inputs[0], A[key][1])
        for scan_branch, branch in zip(scan.proof.branches, lazy.branches):
            self.assertEqual([t.status for t in scan_branch.tails],
                             [branch.tails[i].status for i in sorted(branch.tails)])


class Rejection(unittest.TestCase):
    """Candidates the full scanner rejects (or cannot classify): lazy must agree and never verify."""

    def check_same(self, request, inputs, world, expect_status=None, expect_reason=None, evidence=None):
        scan, lazy = both(request, inputs, world, evidence)
        self.assertNotEqual(scan.status, ScanStatus.VERIFIED_CANDIDATE, "precondition: scanner rejects")
        if expect_status is not None:
            self.assertEqual(scan.status, expect_status)
        if expect_reason is not None:
            self.assertEqual(scan.reason, expect_reason)
        self.assertNotEqual(lazy.status, ScanStatus.VERIFIED_CANDIDATE, "lazy verified a rejected candidate")
        self.assertEqual(lazy.status, scan.status)
        self.assertEqual(lazy.reason, scan.reason)
        return scan, lazy

    def test_truncated_in_the_air_and_jump_replaced_by_walk(self):
        key = "gap_start_1_width_1:A3"
        request, world = fx(key).request, fx(key).world
        inputs = A[key]
        self.check_same(request, inputs[:12], world, ScanStatus.CANDIDATE_REJECTED,
                        CandidateRejection.FINAL_EXIT_NOT_GROUNDED)
        walked = tuple(replace(c, jump=False) for c in inputs)
        self.check_same(request, walked, world, ScanStatus.CANDIDATE_REJECTED)

    def test_walk_straight_over_the_hole(self):
        key = "gap_start_4_width_2:A3"
        request, world = fx(key).request, fx(key).world
        self.check_same(request, (request.supported_inputs[0],) * 30, world,
                        ScanStatus.CANDIDATE_REJECTED)

    def test_unknown_landing_is_needs_information_not_free(self):
        fixture = common.fixture_for("unknown_landing", None)
        scan, lazy = self.check_same(fixture.request, A["gap_start_1_width_1:A3"], fixture.world,
                                     ScanStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD)
        self.assertTrue(scan.missing_cells)

    def test_unknown_cell_on_the_route_is_not_free_space(self):
        """An unobserved cell the body sweeps through must stop the proof, not read as air."""
        fixture = common.fixture_for("flat_walk", "A3")
        world = common.edited_world(fixture.world, {(0, 1, 1): None}, bounds=((-3, 3), (-50, 7), (-3, 12)))
        scan, lazy = self.check_same(fixture.request, A["flat_walk:A3"], world,
                                     ScanStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD)
        self.assertIn((0, 1, 1), scan.missing_cells)

    def test_one_twelfth_support_landing_is_rejected(self):
        fixture = common.fixture_for("one_twelfth_support", None)
        self.check_same(fixture.request, A["gap_start_1_width_1:A3"], fixture.world,
                        ScanStatus.CANDIDATE_REJECTED, CandidateRejection.TAIL_NOT_SETTLED)

    def test_failure_of_the_late_branch_alone_is_not_missed(self):
        """Jumping on the very first tick is fine for ON_TIME (interval opens at boundary 0) but the
        LATE branch already waited one tick: its boundary-0 tail must be safe (PRELUDE_UNSAFE)."""
        key = "gap_start_1_width_2:A5"
        request, world = fx(key).request, fx(key).world
        first_jump = next(c for c in request.supported_inputs if c.jump and c.sprint)
        inputs = (first_jump,) + A[key][1:]
        scan, lazy = self.check_same(request, inputs, world, ScanStatus.CANDIDATE_REJECTED,
                                     CandidateRejection.PRELUDE_UNSAFE)
        on_time_only = replace(request, entry_states=request.entry_states[:1],
                               timing_branches=(TimingBranch.ON_TIME,),
                               allowed_effect_ticks=request.allowed_effect_ticks[:1],
                               branch_preludes=((),))
        alone = scan_commitment(on_time_only, inputs, world, _boundary_evidence(on_time_only, inputs))
        self.assertEqual(alone.status, ScanStatus.VERIFIED_CANDIDATE,
                         "witness must pass ON_TIME alone, otherwise it does not test the second branch")

    def test_damage_allowance_zero_budget(self):
        cases = {c.id: c for c in K.drop_cands()}
        for name, expected in (("drop/drop_depth5_budget0", ScanStatus.CANDIDATE_REJECTED),
                               ("drop/drop_depth4_budget0", ScanStatus.CANDIDATE_REJECTED),
                               ("drop/drop_depth6_budget1", ScanStatus.CANDIDATE_REJECTED)):
            with self.subTest(name):
                c = cases[name]
                self.check_same(c.request, c.inputs, c.world, expected, CandidateRejection.DAMAGE_ALLOWANCE)

    def test_damage_within_budget_is_still_accepted(self):
        cases = {c.id: c for c in K.drop_cands()}
        for name in ("drop/drop_depth3_budget0", "drop/drop_depth4_budget1", "drop/drop_depth5_budget2"):
            with self.subTest(name):
                c = cases[name]
                scan, lazy = both(c.request, c.inputs, c.world)
                self.assertEqual(scan.status, ScanStatus.VERIFIED_CANDIDATE)
                self.assertEqual(lazy.status, ScanStatus.VERIFIED_CANDIDATE)

    def test_request_level_classes(self):
        for c in K.request_level_cases():
            if c.id.endswith(("physics_budget_300",)):
                continue
            with self.subTest(c.id):
                self.check_same(c.request, c.inputs, c.world, evidence=c.evidence)

    def test_landing_must_be_proved_before_the_interval_closes(self):
        if DOUBLE_HOP_LABELS is None:
            self.skipTest("witness not recorded")
        fixture = K.double_gap_fixture()
        inputs = K.parse_labels(DOUBLE_HOP_LABELS, fixture.request)
        scan, lazy = both(fixture.request, inputs, fixture.world)
        self.assertEqual(scan.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(lazy.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(interval_keys(scan.proof.branches), [[(0, 1, 25)], [(0, 1, 25)]],
                         "one interval spans both hops: the landing boundary has a jump in flight")
        self.assertEqual(interval_keys(lazy.branches), interval_keys(scan.proof.branches))
        self.assertTrue(equiv.compare(scan, lazy)["equivalent"])


class RunAhead(unittest.TestCase):
    """Fix F1 (addendum 2): mode "strict_runahead" must be strict mode with the work reordered."""
    KEYS = Equivalence.KEYS + ("gap_start_4_width_3:A15", "gap_start_1_width_1:A3")

    def three(self, request, inputs, world, evidence=None):
        evidence = _boundary_evidence(request, inputs) if evidence is None else evidence
        scan = scan_commitment(request, inputs, world, evidence)
        strict = I.lazy_prove(request, inputs, world, evidence, faults=FAULTS)
        ahead = I.lazy_prove(request, inputs, world, evidence, mode="strict_runahead", faults=FAULTS)
        return scan, strict, ahead

    @staticmethod
    def total(result):
        return sum(d.physics_steps for d in result.decisions)

    def test_verdict_and_intervals_equal_scanner_and_strict(self):
        for key in self.KEYS:
            with self.subTest(key):
                request, world = fx(key).request, fx(key).world
                scan, strict, ahead = self.three(request, A[key], world)
                row = equiv.compare(scan, ahead)
                self.assertTrue(row["equivalent"], row)
                self.assertEqual(interval_keys(ahead.branches), interval_keys(strict.branches))
                self.assertEqual(ahead.dependencies, strict.dependencies)

    def test_step_total_equals_strict_mode_exactly(self):
        for key in self.KEYS:
            with self.subTest(key):
                request, world = fx(key).request, fx(key).world
                scan, strict, ahead = self.three(request, A[key], world)
                self.assertEqual(self.total(ahead), self.total(strict))
                self.assertEqual(ahead.counts, strict.counts)
                self.assertEqual(self.total(ahead), scan.counts.physics_steps)

    def test_ticks_look_ahead_until_the_step_budget_and_the_commit_gets_cheaper(self):
        key = "gap_start_4_width_3:A15"
        request, world = fx(key).request, fx(key).world
        scan, strict, ahead = self.three(request, A[key], world)
        n = len(A[key])
        ticks = [d for d in ahead.decisions if d.kind == "tick"]
        self.assertTrue(all(d.physics_steps >= 60 for d in ticks if d.computed_to < n))
        self.assertTrue(any(d.computed_to > d.end_boundary for d in ticks), "ticks must compute later boundaries")
        commits = {m: next(d for d in r.decisions if d.kind == "commit") for m, r in
                   (("strict", strict), ("ahead", ahead))}
        self.assertLess(commits["ahead"].physics_steps, commits["strict"].physics_steps)
        # work is only moved between decisions: the plan stage is untouched
        self.assertEqual(ahead.decisions[0].physics_steps, strict.decisions[0].physics_steps)
        self.assertEqual([d.kind for d in ahead.decisions], [d.kind for d in strict.decisions])
        self.assertEqual([d.permit.commands for d in ahead.decisions if d.permit],
                         [d.permit.commands for d in strict.decisions if d.permit])

    def test_commit_without_enough_ticks_before_it_still_does_its_own_missing_tails(self):
        key = "mixed_ground_jump_air:"          # the jump is already the second command
        request, world = fx(key).request, fx(key).world
        scan, strict, ahead = self.three(request, A[key], world)
        commit = next(d for d in ahead.decisions if d.kind == "commit")
        self.assertEqual((commit.boundary, commit.end_boundary, commit.permit.locked_count), (1, 13, 12))
        self.assertGreater(commit.physics_steps, 0)
        self.assertEqual(interval_keys(ahead.branches), [[(0, 1, 13)], [(0, 1, 13)]])

    def test_error_in_a_look_ahead_tail_halts_earlier_with_the_same_verdict(self):
        fixture = common.fixture_for("one_twelfth_support", None)
        scan, strict, ahead = self.three(fixture.request, A["gap_start_1_width_1:A3"], fixture.world)
        self.assertEqual((scan.status, scan.reason),
                         (ScanStatus.CANDIDATE_REJECTED, CandidateRejection.TAIL_NOT_SETTLED))
        for result in (strict, ahead):
            self.assertEqual((result.status, result.reason), (scan.status, scan.reason))
        self.assertEqual(ahead.online_halt, strict.online_halt)          # same offending boundary
        self.assertEqual(ahead.online_halt[0], 5)
        self.assertLess(len(ahead.permits), len(strict.permits))          # ... detected before permitting 1,2
        self.assertEqual(self.total(ahead), self.total(strict))

    def test_rejections_match_scanner_in_runahead_mode(self):
        cases = []
        for scenario, source in (("unknown_landing", "gap_start_1_width_1:A3"),
                                 ("one_twelfth_support", "jump_gap_continue:A3"),
                                 ("gap_start_4_width_3_a3", "gap_start_4_width_2:A3")):
            f = common.fixture_for(scenario, None)
            cases.append((scenario, f.request, f.world, A[source]))
        key = "gap_start_1_width_1:A3"
        cases.append(("truncated", fx(key).request, fx(key).world, A[key][:12]))
        for c in K.drop_cands()[:12] + K.request_level_cases():
            cases.append((c.id, c.request, c.world, c.inputs, c.evidence))
        for name, request, world, inputs, *extra in cases:
            with self.subTest(name):
                scan, strict, ahead = self.three(request, inputs, world, *(extra or [None]))
                if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
                    self.assertNotEqual(ahead.status, ScanStatus.VERIFIED_CANDIDATE)
                self.assertEqual((ahead.status, ahead.reason), (scan.status, scan.reason))
                self.assertEqual(ahead.online_halt, strict.online_halt)
                self.assertEqual(self.total(ahead), self.total(strict))

    def test_runahead_steps_must_be_positive(self):
        key = "flat_walk:A3"
        request, world = fx(key).request, fx(key).world
        with self.assertRaises(ValueError):
            I.lazy_prove(request, A[key], world, _boundary_evidence(request, A[key]),
                         mode="strict_runahead", runahead_steps=0)


class Permits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = "mixed_ground_jump_air:"
        cls.request, cls.world = fx(key).request, fx(key).world
        cls.lazy = I.lazy_prove(cls.request, A[key], cls.world, _boundary_evidence(cls.request, A[key]))
        cls.tick = cls.lazy.decisions[1].permit
        cls.commit = cls.lazy.decisions[2].permit

    def validate(self, permit=None, request=None, world=None):
        return I.validate_permit(permit or self.commit, request or self.request, world or self.world,
                                 faults=FAULTS)

    def assertStale(self, check, reason):
        self.assertEqual((check.status, check.reason), (I.PermitStatus.STALE, reason))

    def test_unchanged_world_and_request_are_valid(self):
        for permit in (self.tick, self.commit):
            self.assertEqual(self.validate(permit).status, I.PermitStatus.VALID)

    def test_changed_anchor_id(self):
        self.assertStale(self.validate(request=replace(self.request, anchor_id="anchor-21")),
                         SearchReason.ANCHOR)

    def test_changed_entry_state(self):
        anchor, late = self.request.entry_states
        moved = replace(anchor, position=(anchor.position[0] + .01, *anchor.position[1:]))
        self.assertStale(self.validate(request=replace(self.request, entry_states=(moved, late))),
                         SearchReason.ANCHOR)

    def test_changed_goal_revision_and_goal(self):
        self.assertStale(self.validate(request=replace(self.request, goal_revision=2)),
                         SearchReason.GOAL_REVISION)
        goal = replace(self.request.goal, region=Aabb(.3, 1., 3.3, .7, 1.05, 3.4))
        self.assertStale(self.validate(request=replace(self.request, goal=goal)), SearchReason.GOAL_REVISION)

    def test_changed_input_ledger(self):
        self.assertStale(self.validate(request=replace(self.request, input_ledger_id="ledger-100")),
                         SearchReason.INPUT_LEDGER)
        self.assertStale(self.validate(request=replace(
            self.request, first_candidate_control_sequence=101)), SearchReason.INPUT_LEDGER)

    def test_changed_request_identity_and_world_session(self):
        self.assertStale(self.validate(request=replace(self.request, request_id="other-request")),
                         SearchReason.REQUEST_IDENTITY)
        other = common.edited_world(self.world, {}, session=WorldSessionId("some-other-session"))
        self.assertStale(self.validate(world=other), SearchReason.WORLD_DEPENDENCY)

    def block_dependency(self):
        return next(p for p, fact in self.commit.dependency_facts
                    if fact.knowledge is CellKnowledge.BLOCK and p[1] == 0 and p[2] >= 2)

    def test_changed_dependency_cell(self):
        position = self.block_dependency()
        stone = self.world.cell(position)
        changed = common.edited_world(self.world, {position: CellFact(CellKnowledge.AIR, stone.stamp, None)})
        self.assertStale(self.validate(world=changed), SearchReason.WORLD_DEPENDENCY)
        changed = common.edited_world(self.world, {(position[0], position[1] + 1, position[2]): stone})
        self.assertStale(self.validate(world=changed), SearchReason.WORLD_DEPENDENCY)

    def test_dependency_that_became_unknown_needs_information(self):
        position = self.block_dependency()
        unknown = common.edited_world(self.world, {position: None})
        check = self.validate(world=unknown)
        self.assertEqual((check.status, check.reason),
                         (I.PermitStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD))
        self.assertIn(position, check.missing_cells)

    def test_unrelated_cell_change_does_not_stale_the_permit(self):
        far = (8, 5, 12)
        self.assertNotIn(far, {p for p, _ in self.commit.dependency_facts})
        stone = self.world.cell((0, 0, 0))
        changed = common.edited_world(self.world, {far: stone})
        self.assertEqual(self.validate(world=changed).status, I.PermitStatus.VALID)


class ExecutorGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = "mixed_ground_jump_air:"
        request, world = fx(key).request, fx(key).world
        lazy = I.lazy_prove(request, A[key], world, _boundary_evidence(request, A[key]))
        cls.commit = lazy.decisions[2].permit
        cls.tick = lazy.decisions[1].permit
        cls.stop = request.stop_input
        cls.inputs = A[key]

    def check(self, permit, command, boundary=None):
        return I.check_execution(permit, command, boundary, faults=FAULTS)

    def test_locked_suffix_commands_are_allowed_only_unchanged(self):
        permit = self.commit
        self.assertEqual((permit.boundary, permit.locked_count, len(permit.commands)), (1, 12, 13))
        for offset in range(permit.locked_count):
            boundary = permit.boundary + offset
            self.assertEqual(self.check(permit, self.inputs[boundary], boundary), I.ExecutionCheck.ALLOWED)
            swapped = self.stop if self.inputs[boundary] != self.stop else self.inputs[0]
            self.assertEqual(self.check(permit, swapped, boundary), I.ExecutionCheck.COMMAND_MISMATCH,
                             f"locked command {boundary} was swapped")

    def test_tick_permit_covers_one_command(self):
        self.assertEqual(self.check(self.tick, self.inputs[0]), I.ExecutionCheck.ALLOWED)
        self.assertEqual(self.check(self.tick, self.inputs[1], 1), I.ExecutionCheck.OUT_OF_RANGE)
        self.assertEqual(self.check(self.tick, self.stop, 0), I.ExecutionCheck.COMMAND_MISMATCH)


class FaultFlags(unittest.TestCase):
    def test_unknown_fault_names_are_refused(self):
        with self.assertRaises(ValueError):
            I.check_faults({"no_such_fault"})

    def test_all_seven_faults_are_defined(self):
        self.assertEqual(I.FAULTS, {"no_landing_proof", "unknown_as_free", "one_branch", "ignore_inflight",
                                    "accept_stale", "ignore_damage", "swap_locked_command"})


if __name__ == "__main__":
    unittest.main()
