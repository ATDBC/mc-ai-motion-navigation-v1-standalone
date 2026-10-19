"""Behavioral P0 safety scans over explicit inputs, without a searcher."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto.commitment import (
    BoundaryInputs, CandidateRejection, ScanOptions, ScanStatus, StopTail, TailStatus,
    scan_commitment, validate_commitment,
)
from experiments.motion_navigation.trajectory_proto.contracts import (
    InputTier, SearchBudget, SearchReason, TimingBranch,
)
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import (
    BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldSessionId, WorldView,
)
from tests.motion_nav.test_trajectory_proto_contracts import application, request, physics_state


WALK = TickInput(1., 0., False, False, False, 0.)
JUMP = replace(WALK, jump=True)
NEUTRAL = replace(WALK, forward=0.)


def world(*, gap=False, up=False, missing=(), changed=None, revision=3):
    stamp = ObservationStamp(physics_state().session, 0, 0, "p0-component", 0)
    facts = {}
    for x in range(-3, 4):
        for y in range(-5, 7):
            for z in range(-3, 13):
                position = (x, y, z)
                if position in missing:
                    continue
                solid = y == 0 and not (gap and z == 1)
                solid |= up and y == 1 and z >= 1
                facts[position] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR,
                    stamp, BlockGeometry.full_cube("minecraft:stone") if solid else None)
    if changed:
        facts.update(changed)
    return PhysicsWorldView(WorldView.detached(stamp.session, revision, 0, facts),
                            JAVA_1_21_RULESET)


def scan_request(inputs, *, state=None, two=False, budget=None, prelude=NEUTRAL, local=None):
    state = state or replace(physics_state(), velocity_blocks_per_tick=(0., -.0784, .1))
    late = step(state, prelude, local or world(), JAVA_1_21_RULESET).next_state if two else None
    if two and late is None:
        raise AssertionError("fixture must derive its late entry from a complete prelude step")
    return replace(request(), entry_states=(state, late) if two else (state,),
                   timing_branches=tuple(TimingBranch) if two else (TimingBranch.ON_TIME,),
                   allowed_effect_ticks=tuple(state.movement_tick_id + 1 + index
                                              for index in range(2 if two else 1)), input_prefix=(),
                   supported_inputs=(WALK, JUMP, NEUTRAL),
                   input_tiers=(InputTier("A3", (WALK, JUMP, NEUTRAL)),),
                   branch_preludes=((), (application(prelude, state.movement_tick_id + 1, 99),))
                                    if two else ((),),
                   budget=budget or SearchBudget(200, 5000, 40, 2))


def boundary_fact(req, index, commands, *, branch=0):
    absolute = req.entry_states[branch].movement_tick_id + index
    apps = (None if commands is None else tuple(
        application(command, absolute + offset + 1, req.first_candidate_control_sequence + index + offset)
        for offset, command in enumerate(commands)))
    return BoundaryInputs(index, commands, absolute, apps)


def ledger(req, inputs, *, inflight=(), absent=None):
    return tuple(tuple(boundary_fact(req, index, None if index == absent else
                       inflight if index == 1 else (), branch=branch)
                       for index in range(len(inputs) + 1))
                 for branch in range(len(req.entry_states)))


def falling_world(*, two_landings):
    """Known terraces with surfaces 9 -> 5 -> 1; no artificial fall counter."""
    stamp = ObservationStamp(physics_state().session, 0, 0, "p0-damage", 0)
    facts = {}
    for x in range(-3, 4):
        for y in range(-5, 14):
            for z in range(-3, 21):
                surface_y = 9 if z < 1 else 5 if not two_landings or z < 4 else 1
                solid = y == surface_y - 1
                facts[(x, y, z)] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR,
                    stamp, BlockGeometry.full_cube("minecraft:stone") if solid else None)
    return PhysicsWorldView(WorldView.detached(stamp.session, 3, 0, facts), JAVA_1_21_RULESET)


def falling_scan(*, allowance, two_landings):
    inputs = (WALK,) * 40
    state = replace(physics_state(), position=(.5, 9., .5),
                    velocity_blocks_per_tick=(0., -.0784, .1))
    req = replace(scan_request(inputs, state=state),
                  task_damage_budget=TaskDamageBudget("bounded", allowance))
    return scan_commitment(req, inputs, falling_world(two_landings=two_landings),
                           ledger(req, inputs))


GAP_INPUTS = (WALK, JUMP) + (WALK,) * 10 + (NEUTRAL,) * 21


class TrajectoryProtoCommitmentTests(unittest.TestCase):
    def test_real_neutral_delay_changes_nonzero_speed_and_first_effect_tick(self):
        req = scan_request(GAP_INPUTS, two=True)
        result = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                 ledger(req, GAP_INPUTS, inflight=(JUMP,)))
        expected_entry = step(req.entry_states[0], NEUTRAL, world(gap=True), JAVA_1_21_RULESET).next_state
        late = result.proof.branches[1]
        self.assertEqual(late.states[0], expected_entry)
        self.assertEqual(late.states[1].movement_tick_id, 12)
        self.assertAlmostEqual(late.states[0].position[2], .6)
        self.assertAlmostEqual(late.states[0].velocity_blocks_per_tick[2], .0546)
        self.assertAlmostEqual(late.states[1].position[2], .7526000090740741)
        self.assertAlmostEqual(late.states[1].velocity_blocks_per_tick[2], .08331960495444446)
        for branch in result.proof.branches:
            risk = branch.risk_intervals[0]
            self.assertEqual(risk.first_committed_tick,
                             branch.states[risk.first_committed_boundary].movement_tick_id)
            self.assertEqual(risk.commitment_effect_tick, risk.first_committed_tick + 1)
            self.assertEqual(risk.recovered_tick,
                             branch.states[risk.recovered_boundary].movement_tick_id)
            self.assertEqual(branch.tails[1].inputs[0], JUMP)
            self.assertEqual(branch.tails[1].absolute_tick, branch.states[1].movement_tick_id)

    def test_static_delay_still_advances_absolute_tick(self):
        state = replace(physics_state(), velocity_blocks_per_tick=(0., -.0784, 0.))
        inputs = (NEUTRAL,) * 4
        req = scan_request(inputs, state=state, two=True)
        result = scan_commitment(req, inputs, world(), ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
        for branch, effect in zip(result.proof.branches, (11, 12)):
            self.assertEqual(branch.effect_tick, effect)
            self.assertEqual(branch.states[0].movement_tick_id + 1, effect)
            self.assertEqual(branch.prelude_states[0], req.anchor_state)
            for index, state in enumerate(branch.states[1:]):
                self.assertEqual(state.movement_tick_id, effect + index)
        self.assertEqual(result.proof.branches[1].states[0].position, req.anchor_state.position)

    def test_held_walk_and_neutral_wait_have_different_real_entries(self):
        scans = []
        for prelude in (NEUTRAL, WALK):
            req = scan_request(GAP_INPUTS, two=True, prelude=prelude)
            result = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                     ledger(req, GAP_INPUTS, inflight=(JUMP,)))
            self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
            expected = step(req.anchor_state, prelude, world(gap=True), JAVA_1_21_RULESET).next_state
            self.assertEqual(result.proof.branches[1].states[0], expected)
            scans.append(result)
        self.assertNotEqual(scans[0].proof.trajectory_hash, scans[1].proof.trajectory_hash)
        self.assertNotEqual(scans[0].proof.branches[1].states[0], scans[1].proof.branches[1].states[0])
        held = scans[1].proof.branches[1].states[0]
        self.assertAlmostEqual(held.position[2], .6980000090740741)
        self.assertAlmostEqual(held.velocity_blocks_per_tick[2], .10810800495444446)

    def test_missing_prelude_never_defaults_to_neutral(self):
        req = scan_request(GAP_INPUTS, two=True)
        req = replace(req, branch_preludes=((), None))
        result = scan_commitment(req, GAP_INPUTS, world(gap=True), ledger(req, GAP_INPUTS))
        self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.MISSING_INPUT_APPLICATION)
        self.assertIsNone(result.proof)

    def test_declared_late_state_must_match_entire_real_prelude_state(self):
        req = scan_request(GAP_INPUTS, two=True)
        for change in ({"position": (.5, 1., .52)}, {"jumping_cooldown_ticks": 1},
                       {"velocity_blocks_per_tick": (0., -.0784, .1)}):
            forged = replace(req, entry_states=(req.entry_states[0], replace(req.entry_states[1], **change)))
            result = scan_commitment(forged, GAP_INPUTS, world(gap=True), ledger(forged, GAP_INPUTS))
            self.assertIs(result.status, ScanStatus.CANDIDATE_REJECTED)
            self.assertIs(result.reason, CandidateRejection.PRELUDE_ENTRY_MISMATCH)
            self.assertIsNone(result.proof)

    def test_any_real_wait_branch_failure_prevents_proof(self):
        inputs = (JUMP,) + (WALK,) * 10 + (NEUTRAL,) * 20
        state = replace(physics_state(), position=(.5, 1., 1.19),
                        velocity_blocks_per_tick=(0., -.0784, .1))
        local = world(gap=True)
        on_time = scan_request(inputs, state=state)
        accepted = scan_commitment(on_time, inputs, local, ledger(on_time, inputs))
        self.assertIs(accepted.status, ScanStatus.VERIFIED_CANDIDATE)
        req = scan_request(inputs, state=state, two=True, prelude=WALK, local=local)
        # Physics' prior vertical contact flag survives this tick, but the
        # actual end footprint is entirely beyond the known supporting block.
        self.assertTrue(req.entry_states[1].on_ground)
        self.assertGreater(req.entry_states[1].position[2] - .3, 1.)
        result = scan_commitment(req, inputs, local, ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(result.reason, CandidateRejection.PRELUDE_UNSAFE)
        self.assertIsNone(result.proof)

    def test_stale_ground_contact_without_support_is_not_a_safe_stop(self):
        inputs = (JUMP,) + (WALK,) * 10 + (NEUTRAL,) * 20
        state = replace(physics_state(), position=(.5, 1., 1.),
                        velocity_blocks_per_tick=(0., -.0784, .1))
        local = world(gap=True)
        nominal = scan_request(inputs, state=state)
        nominal_result = scan_commitment(nominal, inputs, local, ledger(nominal, inputs))
        self.assertIs(nominal_result.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(nominal_result.reason, CandidateRejection.TAIL_NOT_SETTLED)
        self.assertIsNone(nominal_result.proof)
        req = scan_request(inputs, state=state, two=True, prelude=WALK, local=local)
        late = req.entry_states[1]
        self.assertTrue(late.on_ground)
        self.assertFalse(late.horizontal_collision)
        self.assertEqual(late.position[1], state.position[1])
        self.assertAlmostEqual(late.position[2], 1.1980000090740741)
        # An already committed candidate entry remains usable for single-branch
        # scans; only creating it through this unmodelled waiting interval fails.
        late_only = scan_request(inputs, state=late)
        late_result = scan_commitment(late_only, inputs, local, ledger(late_only, inputs))
        self.assertIs(late_result.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertIs(late_result.proof.branches[0].tails[0].status, TailStatus.UNSAFE)
        result = scan_commitment(req, inputs, local, ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(result.reason, CandidateRejection.TAIL_NOT_SETTLED)
        self.assertIsNone(result.proof)

    def test_wait_rejects_height_change_or_horizontal_collision(self):
        inputs = (NEUTRAL,) * 20
        req = scan_request(inputs, two=True, prelude=JUMP)
        result = scan_commitment(req, inputs, world(), ledger(req, inputs))
        self.assertIs(result.reason, CandidateRejection.PRELUDE_UNSAFE)
        stone = world().cell((0, 0, 0))
        local = world(changed={(0, 1, 1): stone})
        state = replace(physics_state(), position=(.5, 1., .69),
                        velocity_blocks_per_tick=(0., -.0784, .1))
        req = scan_request(inputs, state=state, two=True, prelude=WALK, local=local)
        self.assertTrue(req.entry_states[1].horizontal_collision)
        result = scan_commitment(req, inputs, local, ledger(req, inputs))
        self.assertIs(result.reason, CandidateRejection.PRELUDE_UNSAFE)
        self.assertIsNone(result.proof)

    def test_unknown_is_detected_in_prelude_step_with_its_actual_input(self):
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        inputs = (WALK,) * 4 + (NEUTRAL,) * 12
        state = replace(physics_state(), position=(.65, 1., .5),
                        velocity_blocks_per_tick=(0., -.0784, .1))
        strafe = replace(NEUTRAL, strafe=1.)
        req = scan_request(inputs, state=state, two=True, prelude=strafe)
        missing = world(missing=((1, 1, 0),))
        nominal = scan_request(inputs, state=state)
        self.assertIs(scan_commitment(nominal, inputs, missing, ledger(nominal, inputs)).status,
                      ScanStatus.VERIFIED_CANDIDATE)
        original = CountedPhysics.step
        calls = []
        def observed(counter, state, command, world, **kwargs):
            calls.append((state.movement_tick_id, command))
            return original(counter, state, command, world, **kwargs)
        with patch.object(CountedPhysics, "step", observed):
            result = scan_commitment(req, inputs, missing, ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.UNKNOWN_WORLD)
        self.assertEqual(calls[-1], (10, strafe))
        self.assertIn((1, 1, 0), result.missing_cells)

    def test_prelude_steps_dependencies_and_total_duration_are_bound(self):
        inputs = (NEUTRAL,) * 40
        req = scan_request(inputs, two=True)
        result = scan_commitment(req, inputs, world(), ledger(req, inputs))
        self.assertEqual([branch.total_ticks for branch in result.proof.branches], [40, 41])
        self.assertEqual(len(result.proof.inputs), req.budget.max_trajectory_ticks)
        expected_steps = sum(len(inputs) + len(branch.prelude_states) - 1
                             + sum(len(tail.inputs) for tail in branch.tails)
                             for branch in result.proof.branches)
        self.assertEqual(result.counts.physics_steps, expected_steps)
        self.assertIn((0, 0, 0), result.proof.branches[1].prelude_dependencies)
        changed = world(revision=4, changed={(0, 0, 0): CellFact(CellKnowledge.UNKNOWN)})
        checked = validate_commitment(result.proof, req, changed,
                                      result.proof.boundary_inputs, boundary=1)
        self.assertIs(checked.reason, SearchReason.WORLD_DEPENDENCY)
        for limit, status in ((expected_steps, ScanStatus.VERIFIED_CANDIDATE),
                              (expected_steps - 1, ScanStatus.NO_TRAJECTORY_IN_BUDGET)):
            bounded = replace(req, budget=replace(req.budget, max_physics_steps=limit))
            repeated = scan_commitment(bounded, inputs, world(), ledger(bounded, inputs))
            self.assertIs(repeated.status, status)
            self.assertLessEqual(repeated.counts.physics_steps, limit)
            if repeated.proof is None:
                self.assertIs(repeated.reason, SearchReason.PHYSICS_STEP_BUDGET)
        self.assertEqual(result, scan_commitment(req, inputs, world(), ledger(req, inputs)))

    def test_absolute_application_schedule_and_prelude_identity_expire(self):
        req = scan_request(GAP_INPUTS, two=True)
        proof = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
        late_row = proof.boundary_inputs[1][1]
        self.assertEqual(late_row.absolute_tick, 12)
        self.assertEqual(late_row.applications[0].effect_tick, 13)
        for changed in (replace(late_row, absolute_tick=13),
                        replace(late_row, applications=(replace(late_row.applications[0], effect_tick=14),)),
                        replace(late_row, applications=(replace(late_row.applications[0], control_sequence=999),))):
            facts = [list(rows) for rows in proof.boundary_inputs]
            facts[1][1] = changed
            checked = validate_commitment(proof, req, world(gap=True),
                                          tuple(tuple(rows) for rows in facts), boundary=1)
            self.assertIs(checked.reason, SearchReason.INPUT_LEDGER)
        prelude = replace(req.branch_preludes[1][0], evidence_id="different-receipt")
        for changed in (replace(req, branch_preludes=((), (prelude,))),
                        replace(req, first_candidate_control_sequence=101)):
            checked = validate_commitment(proof, changed, world(gap=True),
                                          proof.boundary_inputs, boundary=1)
            self.assertIs(checked.reason, SearchReason.INPUT_LEDGER)
        missing = replace(req, branch_preludes=((), None))
        checked = validate_commitment(proof, missing, world(gap=True), proof.boundary_inputs, boundary=1)
        self.assertIs(checked.reason, SearchReason.MISSING_INPUT_APPLICATION)

    def test_known_clock_change_is_stale_even_with_missing_application_values(self):
        req = scan_request(GAP_INPUTS, two=True)
        proof = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
        facts = [list(rows) for rows in proof.boundary_inputs]
        facts[1][1] = replace(facts[1][1], absolute_tick=13, applications=None)
        result = validate_commitment(proof, req, world(gap=True),
                                     tuple(tuple(rows) for rows in facts), boundary=1)
        self.assertIs(result.status, ScanStatus.STALE)
        self.assertIs(result.reason, SearchReason.INPUT_LEDGER)

    def test_missing_prelude_does_not_mask_known_current_schedule_changes(self):
        req = scan_request(GAP_INPUTS, two=True)
        proof = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
        missing = replace(req, branch_preludes=((), None))
        for branch in (0, 1):
            original = proof.boundary_inputs[branch][1]
            changed_rows = (
                replace(original, absolute_tick=original.absolute_tick + 1),
                replace(original, irrevocable_inputs=(NEUTRAL,)),
                replace(original, applications=(replace(original.applications[0], control_sequence=999),)),
                replace(original, applications=(replace(original.applications[0],
                                                         effect_tick=original.applications[0].effect_tick + 1),)),
            )
            for changed in changed_rows:
                with self.subTest(branch=branch, changed=changed):
                    facts = [list(rows) for rows in proof.boundary_inputs]
                    facts[branch][1] = changed
                    checked = validate_commitment(proof, missing, world(gap=True),
                                                  tuple(tuple(rows) for rows in facts), boundary=1)
                    self.assertIs(checked.status, ScanStatus.STALE)
                    self.assertIs(checked.reason, SearchReason.INPUT_LEDGER)
                    self.assertIsNone(checked.proof)
        unchanged = validate_commitment(proof, missing, world(gap=True), proof.boundary_inputs, boundary=1)
        self.assertIs(unchanged.reason, SearchReason.MISSING_INPUT_APPLICATION)

    def gap_scan(self, inflight=(JUMP,)):
        req = scan_request(GAP_INPUTS)
        return scan_commitment(req, GAP_INPUTS, world(gap=True),
                               ledger(req, GAP_INPUTS, inflight=inflight))

    def test_commitment_can_precede_physical_takeoff(self):
        result = self.gap_scan()
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE, result)
        branch = result.proof.branches[0]
        risk = branch.risk_intervals[0]
        self.assertEqual(risk.last_abandon_boundary, 0)
        self.assertEqual(risk.first_committed_boundary, 1)
        self.assertTrue(branch.states[1].on_ground)
        self.assertFalse(branch.states[2].on_ground)

    def test_landing_closes_but_preserves_previous_risk_interval(self):
        branch = self.gap_scan().proof.branches[0]
        self.assertEqual(len(branch.risk_intervals), 1)
        risk = branch.risk_intervals[0]
        self.assertGreater(risk.recovered_boundary, risk.first_committed_boundary)
        self.assertTrue(branch.states[risk.recovered_boundary].on_ground)
        self.assertIs(branch.tails[-1].status, TailStatus.SAFE_STOP)
        self.assertEqual(risk.first_committed_boundary, 1)

    def test_inflight_jump_is_consumed_before_neutral_tail(self):
        with_jump = self.gap_scan().proof.branches[0].tails[1]
        without_jump = self.gap_scan(inflight=()).proof.branches[0].tails[1]
        self.assertIs(with_jump.status, TailStatus.UNSAFE)
        self.assertIs(without_jump.status, TailStatus.SAFE_STOP)
        self.assertEqual(with_jump.inputs[0], JUMP)
        self.assertFalse(with_jump.states[1].on_ground)

    def test_bound_dependency_changes_block_old_commitment(self):
        result = self.gap_scan()
        proof = result.proof
        req = proof.request
        known_air = world().cell((2, 2, 2))
        for replacement in (known_air, CellFact(CellKnowledge.UNKNOWN)):
            changed = world(gap=True, changed={(0, 0, 0): replacement}, revision=4)
            checked = validate_commitment(proof, req, changed,
                                          proof.boundary_inputs, boundary=1)
            self.assertIs(checked.status, ScanStatus.STALE)
            self.assertIs(checked.reason, SearchReason.WORLD_DEPENDENCY)
        # An unbound distant cell does not invalidate a bound proof.
        unrelated = world(gap=True, changed={(99, 0, 99): known_air}, revision=4)
        checked = validate_commitment(proof, req, unrelated,
                                      proof.boundary_inputs, boundary=1)
        self.assertIs(checked.status, ScanStatus.VERIFIED_CANDIDATE)

    def test_unknown_candidate_and_tail_have_information_classification(self):
        for missing in (((0, 0, 0),), ((0, 2, 1),)):
            req = scan_request(GAP_INPUTS)
            result = scan_commitment(req, GAP_INPUTS, world(gap=True, missing=missing),
                                     ledger(req, GAP_INPUTS, inflight=(JUMP,)))
            self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
            self.assertIs(result.reason, SearchReason.UNKNOWN_WORLD)
            self.assertTrue(result.missing_cells)

    def test_tail_only_unknown_is_not_hidden_by_complete_candidate(self):
        req = scan_request(GAP_INPUTS)
        local = world(gap=True, missing=((0, 4, 0),))
        current = req.entry_states[0]
        for command in GAP_INPUTS:
            calculated = step(current, command, local, JAVA_1_21_RULESET)
            self.assertIsNotNone(calculated.next_state)
            current = calculated.next_state
        result = scan_commitment(req, GAP_INPUTS, local,
                                 ledger(req, GAP_INPUTS, inflight=(JUMP,)))
        self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.UNKNOWN_WORLD)
        self.assertIn((0, 4, 0), result.missing_cells)

    def test_missing_input_application_is_never_neutral(self):
        req = scan_request(GAP_INPUTS)
        result = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                 ledger(req, GAP_INPUTS, absent=1))
        self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.MISSING_INPUT_APPLICATION)
        self.assertIsNone(result.proof)

    def test_missing_input_application_before_submission_is_information(self):
        proof = self.gap_scan().proof
        result = validate_commitment(proof, proof.request, world(gap=True),
                                     ledger(proof.request, GAP_INPUTS, absent=1), boundary=1)
        self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.MISSING_INPUT_APPLICATION)

    def test_submission_uses_current_boundary_only_for_each_branch(self):
        req = scan_request(GAP_INPUTS, two=True)
        proof = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
        for unrelated in (0, 20, len(GAP_INPUTS)):
            facts = ledger(req, GAP_INPUTS, inflight=(JUMP,), absent=unrelated)
            result = validate_commitment(proof, req, world(gap=True), facts, boundary=1)
            self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
            self.assertIs(result.proof, proof)
            self.assertEqual(result.counts.physics_steps, 0)
        # Checking the nominal branch alone is insufficient.
        late = list(proof.boundary_inputs[1])
        late[1] = boundary_fact(req, 1, None, branch=1)
        result = validate_commitment(proof, req, world(gap=True),
                                     (proof.boundary_inputs[0], tuple(late)), boundary=1)
        self.assertIs(result.status, ScanStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.MISSING_INPUT_APPLICATION)

    def test_stale_identity_precedes_current_missing_application(self):
        proof = self.gap_scan().proof
        req = proof.request
        missing = ledger(req, GAP_INPUTS, absent=1)
        different_world = PhysicsWorldView(
            WorldView.detached(WorldSessionId("other-world"), 3, 0, {}), JAVA_1_21_RULESET)
        changed_support = world(gap=True, revision=4,
                                changed={(0, 0, 0): CellFact(CellKnowledge.UNKNOWN)})
        cases = (
            (replace(req, request_id="new-request"), world(gap=True), SearchReason.REQUEST_IDENTITY),
            (req, different_world, SearchReason.WORLD_DEPENDENCY),
            (replace(req, goal_revision=5), world(gap=True), SearchReason.GOAL_REVISION),
            (replace(req, anchor_id="new-anchor"), world(gap=True), SearchReason.ANCHOR),
            (replace(req, input_ledger_id="new-ledger"), world(gap=True), SearchReason.INPUT_LEDGER),
            (req, changed_support, SearchReason.WORLD_DEPENDENCY),
        )
        for changed_req, changed_world, reason in cases:
            with self.subTest(reason=reason, world=changed_world.session):
                result = validate_commitment(proof, changed_req, changed_world, missing, boundary=1)
                self.assertIs(result.status, ScanStatus.STALE)
                self.assertIs(result.reason, reason)
                self.assertIsNone(result.proof)

    def test_current_input_disagreement_is_typed_stale_without_rescan(self):
        req = scan_request(GAP_INPUTS, two=True)
        proof = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
        for branch_index in (0, 1):
            for commands in ((), (NEUTRAL,)):
                rows = list(proof.boundary_inputs[branch_index])
                rows[1] = boundary_fact(req, 1, commands, branch=branch_index)
                facts = list(proof.boundary_inputs)
                facts[branch_index] = tuple(rows)
                result = validate_commitment(proof, req, world(gap=True), tuple(facts), boundary=1)
                self.assertIs(result.status, ScanStatus.STALE)
                self.assertIs(result.reason, SearchReason.INPUT_LEDGER)
                self.assertEqual(result.counts.physics_steps, 0)
                self.assertIsNone(result.proof)

    def test_deterministic_hash_intervals_and_counted_budget_limits(self):
        first = self.gap_scan()
        self.assertEqual(first, self.gap_scan())
        self.assertEqual(first.proof.trajectory_hash, self.gap_scan().proof.trajectory_hash)
        cases = (
            (SearchBudget(1, 5000, 40, 2), ScanOptions(), SearchReason.NODE_BUDGET),
            (SearchBudget(200, 1, 40, 2), ScanOptions(), SearchReason.PHYSICS_STEP_BUDGET),
            (SearchBudget(200, 5000, 1, 2), ScanOptions(), SearchReason.TRAJECTORY_TICK_BUDGET),
        )
        for budget, options, reason in cases:
            req = scan_request(GAP_INPUTS, budget=budget)
            result = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                     ledger(req, GAP_INPUTS), options=options)
            self.assertIs(result.status, ScanStatus.NO_TRAJECTORY_IN_BUDGET)
            self.assertIs(result.reason, reason)
            self.assertLessEqual(result.counts.nodes, budget.max_nodes)
            self.assertLessEqual(result.counts.physics_steps, budget.max_physics_steps)

    def test_tail_horizon_only_rejects_the_current_candidate(self):
        inputs = (WALK,) * 4
        req = scan_request(inputs)
        result = scan_commitment(req, inputs, world(), ledger(req, inputs),
                                 options=ScanOptions(max_tail_ticks=1))
        self.assertIs(result.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(result.reason, CandidateRejection.TAIL_NOT_SETTLED)
        self.assertIsNone(result.proof)

    def test_tail_uses_the_requests_declared_stop_input(self):
        inputs = (WALK,) * 4
        declared_stop = TickInput(0., 0., False, False, False, 1.)
        supported = (WALK, JUMP, NEUTRAL, declared_stop)
        req = replace(scan_request(inputs), stop_input=declared_stop,
                      supported_inputs=supported,
                      input_tiers=(InputTier("A3", supported),))
        result = scan_commitment(req, inputs, world(), ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(result.proof.branches[0].tails[0].inputs[0], declared_stop)

    def test_stop_tail_damage_uses_remaining_task_allowance(self):
        from experiments.motion_navigation.trajectory_proto import commitment

        inputs = (NEUTRAL,)

        def one_point_tail(counter, boundary, prefix, commands, stop_input, local,
                           minimum_y, maximum_damage, options):
            status = TailStatus.SAFE_STOP if maximum_damage >= 1. else TailStatus.UNSAFE
            return StopTail(boundary, status, (), (prefix[-1],), (), 1.,
                            prefix[-1].movement_tick_id)

        with patch.object(commitment, "_tail", side_effect=one_point_tail):
            within = scan_request(inputs)
            within = replace(within, task_damage_budget=TaskDamageBudget("bounded", 1.))
            accepted = scan_commitment(within, inputs, world(), ledger(within, inputs))
            self.assertIs(accepted.status, ScanStatus.VERIFIED_CANDIDATE)
            self.assertEqual(accepted.proof.branches[0].damage_points, 0.)
            self.assertEqual({tail.damage_points for tail in accepted.proof.branches[0].tails}, {1.})

            over = replace(within, task_damage_budget=TaskDamageBudget("bounded", .5))
            rejected = scan_commitment(over, inputs, world(), ledger(over, inputs))
            self.assertIs(rejected.status, ScanStatus.CANDIDATE_REJECTED)
            self.assertIs(rejected.reason, CandidateRejection.FINAL_STOP_UNSAFE)
            self.assertIsNone(rejected.proof)

    def test_two_branches_replay_one_candidate_and_require_both(self):
        req = scan_request(GAP_INPUTS, two=True)
        result = scan_commitment(req, GAP_INPUTS, world(gap=True),
                                 ledger(req, GAP_INPUTS, inflight=(JUMP,)))
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
        self.assertEqual(len(result.proof.branches), 2)
        self.assertEqual(result.proof.inputs, GAP_INPUTS)
        self.assertNotEqual(result.proof.branches[0].states, result.proof.branches[1].states)
        self.assertEqual([branch.risk_intervals[0].first_committed_boundary
                          for branch in result.proof.branches], [1, 1])
        limited = replace(req, budget=replace(req.budget, max_timing_branches=1))
        result = scan_commitment(limited, GAP_INPUTS, world(gap=True), ledger(req, GAP_INPUTS))
        self.assertIs(result.reason, SearchReason.TIMING_BRANCH_BUDGET)
        with self.assertRaises(ContractViolation):
            scan_commitment(req, (GAP_INPUTS, GAP_INPUTS), world(gap=True), ledger(req, GAP_INPUTS))

    def test_flat_brake_simulates_until_zero_not_just_stable(self):
        inputs = (WALK,) * 4 + (NEUTRAL,) * 12
        req = scan_request(inputs)
        result = scan_commitment(req, inputs, world(), ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
        tail = result.proof.branches[0].tails[0]
        self.assertIs(tail.status, TailStatus.SAFE_STOP)
        self.assertEqual(tail.states[-1].velocity_blocks_per_tick[2], 0.)
        self.assertGreater(len(tail.inputs), 1)
        self.assertEqual(result.proof.branches[0].risk_intervals, ())

    def test_two_distinct_risks_keep_their_own_boundaries(self):
        inputs = ((WALK, JUMP) + (WALK,) * 10 + (NEUTRAL,) * 9
                  + (WALK,) * 4 + (JUMP,) + (WALK,) * 10 + (NEUTRAL,) * 4)
        req = scan_request(inputs)
        rows = list(ledger(req, inputs, inflight=(JUMP,))[0])
        rows[25] = boundary_fact(req, 25, (JUMP,))
        local = world(gap=True, changed={(0, 0, 4): world().cell((0, 2, 4))})
        result = scan_commitment(req, inputs, local, (tuple(rows),))
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
        risks = result.proof.branches[0].risk_intervals
        self.assertEqual([(risk.last_abandon_boundary, risk.first_committed_boundary,
                           risk.recovered_boundary) for risk in risks], [(0, 1, 13), (24, 25, 37)])

    def test_request_and_ledger_changes_do_not_reuse_proof(self):
        proof = self.gap_scan().proof
        for changed, reason in (
                (replace(proof.request, request_id="request-new"), SearchReason.REQUEST_IDENTITY),
                (replace(proof.request, goal_revision=5), SearchReason.GOAL_REVISION),
                (replace(proof.request, anchor_id="new-anchor"), SearchReason.ANCHOR),
                (replace(proof.request, input_ledger_id="new-ledger"), SearchReason.INPUT_LEDGER)):
            result = validate_commitment(proof, changed, world(gap=True),
                                         proof.boundary_inputs, boundary=1)
            self.assertIs(result.status, ScanStatus.STALE)
            self.assertIs(result.reason, reason)
        damaged = replace(proof, trajectory_hash="0" * 64)
        result = validate_commitment(damaged, proof.request, world(gap=True),
                                     proof.boundary_inputs, boundary=1)
        self.assertIs(result.status, ScanStatus.STALE)

    def test_jump_up_and_landing_sliding_use_real_physics(self):
        inputs = (JUMP,) + (WALK,) * 10 + (NEUTRAL,) * 20
        req = scan_request(inputs)
        result = scan_commitment(req, inputs, world(up=True), ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.VERIFIED_CANDIDATE)
        branch = result.proof.branches[0]
        self.assertEqual(branch.states[-1].position[1], 2.)
        first_landing = next(index for index, state in enumerate(branch.states[1:], 1)
                             if state.on_ground)
        tail = branch.tails[first_landing]
        self.assertGreater(len(tail.inputs), 1)
        self.assertEqual(tail.states[-1].velocity_blocks_per_tick[2], 0.)

    def test_unstable_final_exit_is_candidate_rejection_never_blocked(self):
        inputs = (NEUTRAL,)
        state = replace(physics_state(), position=(.5, 1., 1.5),
                        velocity_blocks_per_tick=(0., -.0784, 0.))
        req = scan_request(inputs, state=state)
        result = scan_commitment(req, inputs, world(gap=True), ledger(req, inputs))
        self.assertIs(result.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIsNone(result.proof)

    def test_nonzero_landing_damage_requires_available_allowance(self):
        rejected = falling_scan(allowance=0., two_landings=False)
        self.assertIs(rejected.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(rejected.reason, CandidateRejection.DAMAGE_ALLOWANCE)
        accepted = falling_scan(allowance=1., two_landings=False)
        self.assertIs(accepted.status, ScanStatus.VERIFIED_CANDIDATE)
        branch = accepted.proof.branches[0]
        self.assertEqual(branch.damage_points, 1.)
        landings = [(index, branch.states[index - 1].fall_distance_blocks)
                    for index, state in enumerate(branch.states[1:], 1)
                    if state.on_ground and not branch.states[index - 1].on_ground]
        self.assertEqual(len(landings), 1)
        self.assertEqual(landings[0][0], 14)
        self.assertAlmostEqual(landings[0][1], 3.3462703824114044)
        self.assertEqual(branch.states[14].fall_distance_blocks, 0.)

    def test_two_landings_spend_cumulative_allowance(self):
        for allowance in (0., 1.):
            rejected = falling_scan(allowance=allowance, two_landings=True)
            self.assertIs(rejected.status, ScanStatus.CANDIDATE_REJECTED)
            self.assertIs(rejected.reason, CandidateRejection.DAMAGE_ALLOWANCE)
        accepted = falling_scan(allowance=2., two_landings=True)
        self.assertIs(accepted.status, ScanStatus.VERIFIED_CANDIDATE)
        branch = accepted.proof.branches[0]
        self.assertEqual(branch.damage_points, 2.)
        landings = [index for index, state in enumerate(branch.states[1:], 1)
                    if state.on_ground and not branch.states[index - 1].on_ground]
        self.assertEqual(landings, [14, 30])
        for index in landings:
            self.assertAlmostEqual(branch.states[index - 1].fall_distance_blocks, 3.3462703824114044)
            self.assertEqual(branch.states[index].fall_distance_blocks, 0.)

    def test_stop_tail_keeps_damage_already_spent_in_candidate_prefix(self):
        branch = falling_scan(allowance=2., two_landings=True).proof.branches[0]
        first_landing_tail = branch.tails[14]
        self.assertIs(first_landing_tail.status, TailStatus.SAFE_STOP)
        self.assertTrue(all(state.on_ground for state in first_landing_tail.states))
        self.assertEqual(first_landing_tail.damage_points, 1.)
        second_fall_tail = branch.tails[25]
        self.assertFalse(second_fall_tail.states[0].on_ground)
        self.assertIs(second_fall_tail.status, TailStatus.SAFE_STOP)
        self.assertEqual(second_fall_tail.damage_points, 2.)
        self.assertEqual(branch.tails[-1].damage_points, 2.)


if __name__ == "__main__":
    unittest.main()
