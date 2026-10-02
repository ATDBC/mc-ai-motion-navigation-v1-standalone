"""Quality checks use actual applied inputs on the formal navigation path."""
import unittest
from dataclasses import replace
import math
import pickle

from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, SolveStatus, solve_one_cell_gap,
    revalidate_air_transition, _release_recovery_evidence,
)
from mc2p.motion_nav.segment_entry import MotionContinuationRequirement, SegmentEntryWindow
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav import test_b10_gap_solver as gap_fixtures

from tests.motion_nav import test_action_continuity_formal as formal
from tests.sim.runner import Scenario, run, lane
from tests.sim.scenarios import columns


class MotionContinuationQualityTests(unittest.TestCase):
    def test_wide_known_platform_does_not_brake_back_to_old_landing_cell(self):
        runner = formal.ActionContinuityFormalTests()
        result, worker, metrics = runner._run(formal._gap_case())
        runner._assert_complete(result, metrics)
        first = next(i for i, row in enumerate(result.trace)
                     if row["applied_movement"]["jump"])
        landing = next(i for i in range(first + 1, len(result.trace))
                       if result.trace[i]["on_ground"])
        reverse = [row["movement_tick"] for row in result.trace[first:landing]
                   if row["applied_movement"]["forward"] < 0]
        self.assertEqual(reverse, [], "the next ordinary walk accepts a wider safe exit")
        self.assertGreater(sum(row["applied_movement"]["forward"] > 0
                               for row in result.trace[first:landing]), 3,
                           "collinear search points must not still force aerial coasting")
        runner._assert_executed_preparation(result, worker)

    def test_the_same_successor_is_reached_in_all_four_directions(self):
        for turns in (1, 2, 3):
            with self.subTest(turns=turns):
                runner = formal.ActionContinuityFormalTests()
                result, worker, metrics = runner._run(formal._gap_case(turns))
                runner._assert_complete(result, metrics)
                jump = next(i for i, row in enumerate(result.trace)
                            if row["applied_movement"]["jump"])
                landing = next(i for i in range(jump + 1, len(result.trace))
                               if result.trace[i]["on_ground"])
                self.assertFalse(any(row["applied_movement"]["forward"] < 0
                                     for row in result.trace[jump:landing]))
                self.assertTrue(worker.results[-1].solve_result.proof.continuation)

    def test_proved_descent_does_not_stop_before_its_non_centered_ground_tail(self):
        case = Scenario("d053-height-to-goal-tail",
            lane(columns([68, 68, 67, 66, 65, 64, 64]), width=3),
            (.5, 68., .5), (.68, 64., 6.82))
        result = run(case)
        self.assertEqual(result.outcome, "success", (result.reason, result.trace[-8:]))
        self.assertEqual(result.violations, [])
        tail = next(row for row in result.trace if row["action_index"] == 1)
        self.assertGreater(20 * math.hypot(tail["velocity"][0], tail["velocity"][2]), .5)
        self.assertLess(result.ticks, 64)


def continuation(*, speed=4.4, distance=1.8):
    return MotionContinuationRequirement(SegmentEntryWindow(
        (.5, 64., 2.5), (0., 1.), -.2, distance, .2,
        64. - 1.e-7, 64. + 1.e-7, 0., speed, math.radians(5),
        frozenset({"standing"}), frozenset({MovementMode.WALK}),
        None, None, "test-successor-window",
    ), MovementMode.WALK, "following-walk")


class MotionContinuationContractTests(unittest.TestCase):
    def request(self, target, requirement):
        return GapSolveRequest((0, 1), LandingRegion(*target),
            CandidateExecutionWindow(11, 12), exit_direction=(0, 1),
            exit_motion_ticks=1, continuation=requirement)

    def test_record_replay_and_delayed_start_keep_the_same_exit_contract(self):
        anchor, world, target, _ = gap_fixtures.fixture()
        anchor = gap_fixtures.B10GapSolverTests.moving_anchor(anchor, (0, 1), 2.35)
        request = self.request(target, continuation())
        result = solve_one_cell_gap(anchor, world, request)
        self.assertIs(result.status, SolveStatus.SOLVED, result)
        proof = pickle.loads(pickle.dumps(result.proof))
        self.assertEqual(proof.continuation, request.continuation)
        self.assertTrue(proof.continuation.accepts(proof.exit_state))
        self.assertTrue(all(proof.continuation.accepts(v.exit_state)
                            for v in proof.delayed_start_variants))
        replay = revalidate_air_transition(proof, anchor, world, request.execution_window)
        self.assertIs(replay.status, SolveStatus.SOLVED, replay)
        self.assertEqual(replay.proof.continuation, request.continuation)
        self.assertEqual(replay.proof.commands, proof.commands)

    def test_a_slower_successor_changes_what_counts_as_a_valid_exit(self):
        anchor, world, target, _ = gap_fixtures.fixture()
        anchor = gap_fixtures.B10GapSolverTests.moving_anchor(anchor, (0, 1), 2.35)
        fast = solve_one_cell_gap(anchor, world, self.request(target, continuation()))
        self.assertIs(fast.status, SolveStatus.SOLVED, fast)
        slow = continuation(speed=.1)
        self.assertFalse(slow.accepts(fast.proof.exit_state))
        limited = solve_one_cell_gap(anchor, world, self.request(target, slow))
        self.assertIs(limited.status, SolveStatus.SOLVED, limited)
        self.assertNotEqual(limited.proof, fast.proof)
        if limited.proof is not None:
            self.assertTrue(slow.accepts(limited.proof.exit_state))

    def test_short_platform_does_not_grant_space_beyond_its_successor(self):
        anchor, world, target, _ = gap_fixtures.fixture()
        anchor = gap_fixtures.B10GapSolverTests.moving_anchor(anchor, (0, 1), 2.35)
        request = self.request(target, continuation(distance=.2))
        result = solve_one_cell_gap(anchor, world, request)
        self.assertIs(result.status, SolveStatus.SOLVED, result)
        self.assertTrue(request.continuation.accepts(result.proof.exit_state))
        self.assertLessEqual(result.proof.exit_state.position[2], 2.7 + 1.e-7)
        self.assertTrue(any(c.movement.forward < 0 for c in result.proof.commands))

    def test_final_support_flag_without_known_floor_is_not_a_safe_stop(self):
        anchor, world, target, gap = gap_fixtures.fixture()
        unsupported = replace(anchor.physics_state,
            position=(gap[0] + .5, 64., gap[2] + .5))
        safe, _, _, status, _, reasons = _release_recovery_evidence(
            (anchor.physics_state, unsupported), world,
            self.request(target, continuation()),
        )
        self.assertIs(status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH)
        self.assertIn("final_exit_stop_tail_not_safe", reasons)

    def test_zero_damage_request_cannot_recover_on_a_lower_floor(self):
        anchor, world, target, _ = gap_fixtures.fixture()
        # Actual known floor is y=64; a fabricated grounded flag above it must
        # be stepped and checked, rather than immediately labelled safe.
        state = replace(anchor.physics_state, position=(.5, 68., 2.5))
        safe, _, _, status, _, _ = _release_recovery_evidence(
            (state, state), world, replace(self.request(target, continuation()),
                                          landing=LandingRegion(.3, .7, 2.3, 2.7, 68.)),
        )
        self.assertIs(status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH)
        self.assertEqual(safe, ())

    def test_low_velocity_edge_contact_is_not_a_complete_safe_stop(self):
        anchor, world, target, _ = gap_fixtures.fixture()
        # Remove the broad platform from this fixture: one known cell remains
        # beneath an already overhanging body and the following cells are air.
        from mc2p.motion_nav.world_model import WorldKnowledge, ObservationStamp, BlockGeometry
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
        knowledge = WorldKnowledge(anchor.session)
        stamp = ObservationStamp(anchor.session, 0, 0, "edge-stop-test", 0)
        knowledge.confirm_air(stamp, tuple((x, y, z) for x in range(-2, 3)
            for y in range(55, 71) for z in range(-2, 8)))
        knowledge.observe_blocks(stamp, {(0, 63, 2): BlockGeometry.full_cube("minecraft:grass_block")})
        world = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)
        state = replace(anchor.physics_state, position=(.5, 64., 3.28),
                        velocity_blocks_per_tick=(0., -.0784, .015))
        safe, _, _, status, _, _ = _release_recovery_evidence((state, state), world,
            self.request(target, continuation()))
        self.assertIs(status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH)
        self.assertEqual(safe, ())

    def test_interrupted_landing_uses_current_support_not_the_normal_exit_box(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        from tests.motion_nav.test_continuous_height_execution import frame_from_state
        from mc2p.motion_nav.world_model import WorldKnowledge, ObservationStamp, BlockGeometry
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
        anchor, world, _, _ = gap_fixtures.fixture()
        state = replace(anchor.physics_state, position=(.5, 64., 1.815),
                        velocity_blocks_per_tick=(0., -.0784, .007))
        requirement = continuation()
        requirement = replace(requirement, recovery_entry_window=replace(requirement.entry_window,
            minimum_longitudinal_offset_blocks=-.71, maximum_lateral_offset_blocks=.71))
        self.assertFalse(requirement.accepts(state))
        self.assertTrue(requirement.accepts_recovery(state))
        frame = frame_from_state(state, world, 0)
        self.assertTrue(ActionRouteExecutor._landed_on_current_action_destination(None, frame, requirement))
        empty = WorldKnowledge(anchor.session)
        stamp = ObservationStamp(anchor.session, 0, 0, "missing-support-test", 0)
        empty.confirm_air(stamp, tuple((x, y, z) for x in range(-2, 3)
            for y in range(60, 68) for z in range(-2, 8)))
        empty_world = PhysicsWorldView(empty.view(), JAVA_1_21_RULESET)
        frame = frame_from_state(state, empty_world, 0)
        self.assertFalse(ActionRouteExecutor._landed_on_current_action_destination(None, frame, requirement))


if __name__ == "__main__":
    unittest.main()
