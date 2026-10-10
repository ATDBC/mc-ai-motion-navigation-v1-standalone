"""D096 R2 frozen primitive-search matrix and monotonic input layers."""
from dataclasses import replace
import json
import math
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, TailStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.contracts import SearchReason, SearchStatus
from experiments.motion_navigation.trajectory_proto.reference_search import (
    _boundary_evidence, _goal_check, reference_search,
)
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.primitive_search import primitive_prefixes
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET, primitive_fixture
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET


POSITIVE_MATRIX = (
    ("flat_walk", "A3"),
    ("jump_up_straight", "A3"),
    ("jump_gap_1", "A3"),
    ("gap_start_1_width_1", "A3"),
    ("gap_start_1_width_1", "A5"),
    ("gap_start_1_width_1", "A15"),
    ("gap_start_1_width_2", "A3"),
    ("gap_start_1_width_2", "A5"),
    ("gap_start_1_width_2", "A15"),
    ("gap_start_4_width_1", "A3"),
    ("gap_start_4_width_1", "A5"),
    ("gap_start_4_width_1", "A15"),
    ("gap_start_4_width_2", "A3"),
    ("gap_start_4_width_2", "A5"),
    ("gap_start_4_width_2", "A15"),
    ("gap_start_4_width_3", "A5"),
    ("gap_start_4_width_3", "A15"),
    ("flat_sprint", "A5"),
    ("turn_90", "A15"),
    ("jump_up_after_turn", "A15"),
    ("jump_gap_continue", "A3"),
    ("jump_up_after_turn_continue", "A15"),
)

EXPECTED_NEW_LAYER = {
    "gap_start_4_width_3": "A5",
    "flat_sprint": "A5",
    "turn_90": "A15",
    "jump_up_after_turn": "A15",
    "jump_up_after_turn_continue": "A15",
}


def furthest_stopped_z(fixture, tier):
    """Independent literal G/J/A/B characterization for the flat fixture."""
    best = float("-inf")
    gaits = tuple(command for command in tier.inputs
                  if not command.jump and (command.forward or command.strafe))
    for gait in gaits:
        jump = next((command for command in tier.inputs
                     if command.jump and command.forward == gait.forward
                     and command.strafe == gait.strafe and command.sprint == gait.sprint
                     and command.movement_yaw_radians == gait.movement_yaw_radians), None)
        for ground_ticks in range(21):
            prefixes = [(gait,) * ground_ticks]
            if jump is not None:
                prefixes.extend((gait,) * ground_ticks + (jump,) + (gait,) * air_ticks
                                for air_ticks in range(17))
            for inputs in prefixes:
                states = fixture.request.entry_states
                valid = True
                for command in inputs:
                    results = tuple(step(state, command, fixture.world, JAVA_1_21_RULESET)
                                    for state in states)
                    if any(result.status is not CalculationStatus.OK
                           or result.next_state.horizontal_collision for result in results):
                        valid = False
                        break
                    states = tuple(result.next_state for result in results)
                while valid and len(inputs) < 40 and (
                        not all(state.on_ground for state in states)
                        or any(math.hypot(state.velocity_blocks_per_tick[0],
                                          state.velocity_blocks_per_tick[2]) > 1.e-12
                               for state in states)):
                    results = tuple(step(state, fixture.request.stop_input, fixture.world,
                                         JAVA_1_21_RULESET) for state in states)
                    if any(result.status is not CalculationStatus.OK for result in results):
                        valid = False
                        break
                    states = tuple(result.next_state for result in results)
                    inputs += (fixture.request.stop_input,)
                if valid and all(state.on_ground for state in states) and all(
                        math.hypot(state.velocity_blocks_per_tick[0],
                                   state.velocity_blocks_per_tick[2]) <= 1.e-12
                        for state in states):
                    best = max(best, min(state.position[2] for state in states))
    return best


class TrajectoryProtoPrimitiveMatrixTests(unittest.TestCase):
    def test_primitive_identity_binds_tier_gait_turn_and_all_hold_ticks(self):
        fixture = primitive_fixture("turn_90", "A15")
        tier = fixture.request.input_tiers[-1]
        row = next(item for item in primitive_prefixes(tier)
                   if item.gait_index == 5 and item.ground_ticks == 3
                   and item.jumped and item.air_ticks == 4)
        self.assertEqual(row.tier_id, "A15")
        self.assertEqual(row.movement_yaw_radians, math.pi / 2.)
        self.assertEqual(row.brake_ticks, 0)
        braked = row.with_brake(fixture.request.stop_input, 2)
        self.assertEqual((braked.tier_id, braked.gait_index,
                          braked.movement_yaw_radians,
                          braked.ground_ticks, braked.jumped,
                          braked.air_ticks, braked.brake_ticks),
                         ("A15", 5, math.pi / 2., 3, True, 4, 2))
        self.assertEqual(braked.inputs[-2:], (fixture.request.stop_input,) * 2)

    def test_frozen_input_layers_contain_real_paired_motion_inputs(self):
        tiers = primitive_fixture("turn_90", "A15").request.input_tiers
        self.assertEqual(tuple(len(tier.inputs) for tier in tiers), (3, 5, 15))
        for command in tiers[-1].inputs[5:]:
            self.assertTrue(command.forward or command.strafe)
        for gait in (command for command in tiers[-1].inputs
                     if not command.jump and (command.forward or command.strafe)):
            self.assertTrue(any(
                jump.jump and jump.forward == gait.forward and jump.strafe == gait.strafe
                and jump.sprint == gait.sprint
                and jump.movement_yaw_radians == gait.movement_yaw_radians
                for jump in tiers[-1].inputs))

    def test_flat_sprint_target_has_margin_beyond_a3_and_inside_a5(self):
        fixture = primitive_fixture("flat_sprint", "A5")
        a3_maximum = furthest_stopped_z(fixture, fixture.request.input_tiers[0])
        a5_maximum = furthest_stopped_z(fixture, fixture.request.input_tiers[1])
        self.assertAlmostEqual(a3_maximum, 6.962757644806954)
        self.assertAlmostEqual(a5_maximum, 9.970127011683733)
        self.assertGreater(fixture.request.goal.region.min_z - a3_maximum, .5)
        self.assertGreater(a5_maximum - fixture.request.goal.region.max_z, 1.5)

        a3 = primitive_fixture("flat_sprint", "A3")
        exhausted = reference_search(a3.request, a3.world)
        self.assertIs(exhausted.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIs(exhausted.reason, SearchReason.SEARCH_EXHAUSTED)
        found = reference_search(fixture.request, fixture.world)
        self.assertIs(found.status, SearchStatus.FOUND)
        self.assertEqual(found.winning_tier, "A5")
        self.assertTrue(any(command.sprint for command in found.inputs))

    def test_frozen_positive_matrix_is_found_with_p0_count_budget(self):
        for scenario_id, tier_id in POSITIVE_MATRIX:
            with self.subTest(scenario=scenario_id, tier=tier_id):
                fixture = primitive_fixture(scenario_id, tier_id)
                self.assertEqual(fixture.request.budget, P0_BUDGET)
                outcome = reference_search(fixture.request, fixture.world)
                self.assertIs(outcome.status, SearchStatus.FOUND)
                self.assertIn(outcome.winning_tier,
                              tuple(tier.tier_id for tier in fixture.request.input_tiers))
                if scenario_id in EXPECTED_NEW_LAYER:
                    self.assertEqual(outcome.winning_tier, EXPECTED_NEW_LAYER[scenario_id])
                self.assertLessEqual(outcome.counts.nodes, P0_BUDGET.max_nodes)
                self.assertLessEqual(outcome.counts.physics_steps, P0_BUDGET.max_physics_steps)

    def test_start_four_width_three_a3_exhausts_grid_without_exhausting_budget(self):
        fixture = primitive_fixture("gap_start_4_width_3", "A3")
        outcome = reference_search(fixture.request, fixture.world)
        self.assertIs(outcome.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIs(outcome.reason, SearchReason.SEARCH_EXHAUSTED)
        self.assertLess(outcome.counts.nodes, P0_BUDGET.max_nodes)
        self.assertLess(outcome.counts.physics_steps, P0_BUDGET.max_physics_steps)

    def test_nonzero_continuation_exits_keep_a_separate_safe_stop_tail(self):
        for scenario_id, tier_id in (("jump_gap_continue", "A3"),
                                     ("jump_up_after_turn_continue", "A15")):
            with self.subTest(scenario=scenario_id):
                fixture = primitive_fixture(scenario_id, tier_id)
                outcome = reference_search(fixture.request, fixture.world)
                self.assertIs(outcome.status, SearchStatus.FOUND)
                for branch in outcome.proof.branches:
                    terminal = branch.states[-1]
                    speed = 20. * math.hypot(terminal.velocity_blocks_per_tick[0],
                                             terminal.velocity_blocks_per_tick[2])
                    self.assertGreaterEqual(
                        speed, fixture.request.minimum_terminal_speed_blocks_per_second)
                    self.assertGreater(speed, 0.)
                    tail = branch.tails[-1]
                    self.assertIs(tail.status, TailStatus.SAFE_STOP)
                    self.assertTrue(tail.inputs)
                    self.assertEqual(tail.states[-1].velocity_blocks_per_tick[0], 0.)
                    self.assertEqual(tail.states[-1].velocity_blocks_per_tick[2], 0.)
                    self.assertNotEqual(outcome.inputs[-len(tail.inputs):], tail.inputs)

    def test_zero_speed_and_mixed_stop_tail_mutations_cannot_satisfy_continuation(self):
        from experiments.motion_navigation.trajectory_proto import reference_search as source

        fixture = primitive_fixture("jump_gap_continue", "A3")

        def old_zero_speed_only(request, states):
            return all(state.on_ground
                       and 20. * math.hypot(state.velocity_blocks_per_tick[0],
                                           state.velocity_blocks_per_tick[2]) <= 1.e-12
                       for state in states)

        with patch.object(source, "_potential_goal", side_effect=old_zero_speed_only):
            forced_stop = reference_search(fixture.request, fixture.world)
        self.assertIsNot(forced_stop.status, SearchStatus.FOUND)

        found = reference_search(fixture.request, fixture.world)
        tail = found.proof.branches[0].tails[-1].inputs
        contaminated = found.inputs + tail
        counter = CountedPhysics(fixture.request.budget)
        scan = scan_commitment(fixture.request, contaminated, fixture.world,
                               _boundary_evidence(fixture.request, contaminated), counter=counter)
        self.assertIs(scan.status, ScanStatus.VERIFIED_CANDIDATE)
        checks = [_goal_check(fixture.request, branch.timing_branch, branch.states[-1],
                              fixture.world, counter)[0]
                  for branch in scan.proof.branches]
        self.assertFalse(any(check.accepted for check in checks))

    def test_six_negative_classes_remain_typed(self):
        cases = (
            ("gap_start_4_width_3_a3", SearchStatus.NO_TRAJECTORY_IN_BUDGET,
             SearchReason.SEARCH_EXHAUSTED),
            ("one_twelfth_support", SearchStatus.NO_TRAJECTORY_IN_BUDGET,
             SearchReason.SEARCH_EXHAUSTED),
            ("resource_goal", SearchStatus.NEEDS_INFORMATION,
             SearchReason.UNPROVEN_RESOURCES),
            ("unknown_landing", SearchStatus.NEEDS_INFORMATION,
             SearchReason.UNKNOWN_WORLD),
            ("tiny_budget", SearchStatus.NO_TRAJECTORY_IN_BUDGET,
             SearchReason.PHYSICS_STEP_BUDGET),
            ("collision_only", SearchStatus.NO_TRAJECTORY_IN_BUDGET,
             SearchReason.SEARCH_EXHAUSTED),
        )
        for scenario_id, status, reason in cases:
            with self.subTest(scenario=scenario_id):
                fixture = primitive_fixture(scenario_id)
                outcome = reference_search(fixture.request, fixture.world)
                self.assertIs(outcome.status, status)
                self.assertIs(outcome.reason, reason)
                self.assertIsNone(outcome.proof)

    def test_hashseed_does_not_change_result_proof_or_counts(self):
        script = (
            "import json; "
            "from experiments.motion_navigation.trajectory_proto.scenarios import primitive_fixture; "
            "from experiments.motion_navigation.trajectory_proto.reference_search import reference_search; "
            "f=primitive_fixture('jump_gap_continue','A3'); r=reference_search(f.request,f.world); "
            "print(json.dumps([r.result_hash,r.proof.trajectory_hash,"
            "r.counts.nodes,r.counts.physics_steps,r.completed_candidates,r.commitment_scans]))"
        )
        rows = []
        for seed in ("1", "77", "991"):
            environment = dict(os.environ, PYTHONHASHSEED=seed)
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=os.getcwd(), env=environment,
                capture_output=True, text=True, check=True,
            )
            rows.append(json.loads(completed.stdout))
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0], rows[2])


if __name__ == "__main__":
    unittest.main()
