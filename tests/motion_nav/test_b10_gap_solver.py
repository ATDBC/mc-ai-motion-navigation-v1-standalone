from dataclasses import replace
import math
import pickle
import unittest

from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, MotionTickPhase, StateAnchor,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


SESSION = WorldSessionId("b10-gap-solver-test")


def fixture(direction=(0, 1), *, missing=(), ceiling=False,
            landing_ceiling=False, food=20, using_item=False):
    dx, dz = direction
    start_cell = (0, 63, 0)
    gap_cell = (dx, 63, dz)
    target_cell = (2 * dx, 63, 2 * dz)
    stamp = ObservationStamp(SESSION, 0, 0, "test", 0)
    knowledge = WorldKnowledge(SESSION)
    cells = tuple(
        (x, y, z)
        for x in range(-4, 5)
        for y in range(40, 71)
        for z in range(-4, 5)
        if (x, y, z) not in missing
    )
    knowledge.confirm_air(stamp, cells)
    blocks = {
        (x, 63, z): BlockGeometry.full_cube("minecraft:grass_block")
        for x in range(-4, 5)
        for z in range(-4, 5)
        if (x, 63, z) not in {gap_cell, target_cell}
        and (x, 63, z) not in missing
    }
    blocks[target_cell] = BlockGeometry.full_cube("minecraft:grass_block")
    if ceiling:
        blocks[(dx, 66, dz)] = BlockGeometry.full_cube("minecraft:stone")
    if landing_ceiling:
        blocks[(2 * dx, 65, 2 * dz)] = BlockGeometry.full_cube(
            "minecraft:stone"
        )
    knowledge.observe_blocks(stamp, blocks)
    yaw = math.atan2(-dx, dz)
    state = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        SESSION, 10, (.5, 64., .5), (0., -.0784000015258789, 0.),
        yaw, 0., "standing", .6, 1.8, True, False, True,
        False, False, 0, 0., .1, .6, .08, .42, food, 5., "survival",
        (), False, False, False, False, False, False,
        is_using_item=using_item,
    )
    anchor = StateAnchor(
        SESSION, 3, 10, MotionTickPhase.AFTER_MOVEMENT,
        None, None, JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema, "mc2p.input-projection.v1", state,
    )
    min_x = target_cell[0] + .3
    max_x = target_cell[0] + .7
    min_z = target_cell[2] + .3
    max_z = target_cell[2] + .7
    return (
        anchor,
        PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET),
        (min_x, max_x, min_z, max_z, 64.),
        gap_cell,
    )


class B10GapSolverTests(unittest.TestCase):
    @staticmethod
    def moving_anchor(anchor, direction, speed_blocks_per_second):
        dx, dz = direction
        return replace(
            anchor,
            physics_state=replace(
                anchor.physics_state,
                velocity_blocks_per_tick=(
                    dx * speed_blocks_per_second / 20.0,
                    anchor.physics_state.velocity_blocks_per_tick[1],
                    dz * speed_blocks_per_second / 20.0,
                ),
            ),
        )

    def test_gap_coordination_crops_the_live_world_before_worker_submission(self):
        from mc2p.motion_nav.motion_coordination import _gap_physics_snapshot
        anchor, world, target, _ = fixture()

        local = _gap_physics_snapshot(world, anchor, self.request(target))

        self.assertTrue(local.is_detached)
        self.assertLess(len(pickle.dumps(local)), len(pickle.dumps(world)))

    def test_bounded_physics_snapshot_is_detached_and_keeps_gap_solution(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, _ = fixture()
        local = world.snapshot(PhysicsWorldBounds(-2, 2, 61, 68, -2, 4))

        self.assertTrue(local.is_detached)
        self.assertLess(len(pickle.dumps(local)), len(pickle.dumps(world)))
        result = solve_one_cell_gap(anchor, local, self.request(target))
        self.assertIs(result.status, SolveStatus.SOLVED)

    def request(self, target, direction=(0, 1), *, candidates=12):
        from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion
        return GapSolveRequest(
            direction=direction,
            landing=LandingRegion(*target),
            execution_window=CandidateExecutionWindow(11, 12),
            max_candidates=candidates,
            max_ticks=20,
        )

    def test_searches_real_commands_and_keeps_one_immutable_proof(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, _ = fixture()
        result = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(result.status, SolveStatus.SOLVED)
        proof = result.proof
        self.assertEqual(proof.entry_state, anchor.physics_state)
        self.assertEqual(len(proof.commands), len(proof.tick_inputs))
        self.assertEqual(len(proof.trajectory), len(proof.commands) + 1)
        self.assertEqual(proof.trajectory[0], proof.entry_state)
        self.assertEqual(proof.trajectory[-1], proof.exit_state)
        self.assertEqual(
            proof.release_safe_command_indices,
            tuple(range(len(proof.commands))),
        )
        self.assertTrue(proof.commands[0].movement.jump)
        self.assertTrue(proof.commands[0].movement.sprint)
        self.assertTrue(proof.commands[0].movement.forward)
        self.assertTrue(proof.exit_state.on_ground)
        self.assertLessEqual(math.hypot(
            proof.exit_state.velocity_blocks_per_tick[0],
            proof.exit_state.velocity_blocks_per_tick[2],
        ), .01)
        self.assertEqual(proof.execution_window, CandidateExecutionWindow(11, 12))
        self.assertEqual(proof.ruleset_id, JAVA_1_21_RULESET.ruleset_id)
        self.assertEqual(proof.input_projection_version, "mc2p.input-projection.v1")
        self.assertGreater(len(proof.world_dependencies), 0)

    def test_four_cardinal_directions_use_the_same_solver(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        for direction in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            with self.subTest(direction=direction):
                anchor, world, target, _ = fixture(direction)
                result = solve_one_cell_gap(
                    anchor, world, self.request(target, direction),
                )
                self.assertIs(result.status, SolveStatus.SOLVED)

    def test_three_moving_entry_speed_bands_use_bounded_templates(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        samples = (0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0)
        for direction in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            for speed in samples:
                with self.subTest(direction=direction, speed=speed):
                    anchor, world, target, _ = fixture(direction)
                    anchor = self.moving_anchor(anchor, direction, speed)
                    result = solve_one_cell_gap(
                        anchor, world, self.request(target, direction),
                    )
                    self.assertIs(result.status, SolveStatus.SOLVED)
                    self.assertLessEqual(result.candidates_evaluated, 12)
                    self.assertEqual(
                        result.proof.release_safe_command_indices,
                        tuple(range(len(result.proof.commands))),
                    )

    def test_start_window_proves_the_neutral_tick_before_a_delayed_first_command(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        direction = (0, 1)
        anchor, world, target, _ = fixture(direction)
        anchor = self.moving_anchor(anchor, direction, 2.7)

        result = solve_one_cell_gap(
            anchor, world, self.request(target, direction),
        )

        self.assertIs(result.status, SolveStatus.SOLVED)
        immediate = result.proof.start_variant(11)
        delayed = result.proof.start_variant(12)
        self.assertIsNotNone(immediate)
        self.assertIsNotNone(delayed)
        self.assertEqual(immediate.entry_state, anchor.physics_state)
        self.assertEqual(delayed.entry_state.movement_tick_id, 11)
        self.assertLess(
            math.hypot(
                delayed.entry_state.velocity_blocks_per_tick[0],
                delayed.entry_state.velocity_blocks_per_tick[2],
            ),
            math.hypot(
                immediate.entry_state.velocity_blocks_per_tick[0],
                immediate.entry_state.velocity_blocks_per_tick[2],
            ),
        )
        self.assertTrue(delayed.exit_state.on_ground)
        self.assertTrue(result.proof.landing.contains(delayed.exit_state))
        self.assertEqual(
            delayed.release_safe_command_indices,
            tuple(range(len(result.proof.commands))),
        )
    def test_moving_entry_rejects_a_known_insufficient_landing_region(self):
        from mc2p.motion_nav.motion_solver import (
            GapSolveRequest, LandingRegion, SolveStatus, solve_one_cell_gap,
        )
        for direction in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            for speed in (0.75, 1.5, 2.5):
                with self.subTest(direction=direction, speed=speed):
                    anchor, world, target, _ = fixture(direction)
                    anchor = self.moving_anchor(anchor, direction, speed)
                    min_x, max_x, min_z, max_z, surface_y = target
                    landing = (
                        LandingRegion(
                            min_x, min_x + .01, min_z, max_z, surface_y,
                        )
                        if direction[0] == 0 else
                        LandingRegion(
                            min_x, max_x, min_z, min_z + .01, surface_y,
                        )
                    )
                    result = solve_one_cell_gap(
                        anchor,
                        world,
                        GapSolveRequest(
                            direction,
                            landing,
                            CandidateExecutionWindow(11, 12),
                            exit_direction=direction,
                            exit_motion_ticks=1,
                        ),
                    )

                    self.assertIs(
                        result.status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
                    )
                    self.assertEqual(
                        result.reasons, ("validated_candidates_failed",),
                    )

    def test_moving_entry_rejects_known_blocked_landing_clearance(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        for direction in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            for speed in (0.75, 1.5, 2.5):
                with self.subTest(direction=direction, speed=speed):
                    anchor, world, target, _ = fixture(
                        direction, landing_ceiling=True,
                    )
                    anchor = self.moving_anchor(anchor, direction, speed)
                    result = solve_one_cell_gap(
                        anchor, world, self.request(target, direction),
                    )

                    self.assertIs(
                        result.status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
                    )
                    self.assertEqual(
                        result.reasons, ("validated_candidates_failed",),
                    )

    def test_frozen_r4_matrix_covers_240_entries_and_their_interruptions(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        bands = (
            (0.5, 0.625, 0.75, 0.875, 1.0),
            (1.05, 1.25, 1.5, 1.75, 2.0),
            (2.05, 2.25, 2.5, 2.75, 3.0),
        )
        offsets = (-0.10, -0.05, 0.0, 0.05)
        completed = 0
        for direction in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            for band_index, speeds in enumerate(bands):
                category = 0
                for speed in speeds:
                    for offset in offsets:
                        with self.subTest(
                                direction=direction, band=band_index,
                                speed=speed, offset=offset):
                            anchor, world, target, _ = fixture(direction)
                            anchor = self.moving_anchor(anchor, direction, speed)
                            x, y, z = anchor.physics_state.position
                            dx, dz = direction
                            anchor = replace(
                                anchor,
                                physics_state=replace(
                                    anchor.physics_state,
                                    position=(
                                        x + dx * offset, y, z + dz * offset,
                                    ),
                                ),
                            )
                            result = solve_one_cell_gap(
                                anchor, world,
                                self.request(target, direction),
                            )
                            self.assertIs(result.status, SolveStatus.SOLVED)
                            self.assertLessEqual(result.candidates_evaluated, 12)
                            self.assertEqual(
                                result.proof.release_safe_command_indices,
                                tuple(range(len(result.proof.commands))),
                            )
                            # The proof checks every command boundary.  These
                            # five fixed samples are the frozen R4 reporting set.
                            count = len(result.proof.commands)
                            sampled = tuple(sorted({
                                0, count // 4, count // 2,
                                (3 * count) // 4, count - 1,
                            }))
                            self.assertTrue(set(sampled).issubset(
                                result.proof.release_safe_command_indices,
                            ))
                            category += 1
                            completed += 1
                self.assertEqual(category, 20)
        self.assertEqual(completed, 240)

    def test_moving_entry_velocity_must_point_toward_the_gap(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        direction = (0, 1)
        anchor, world, target, _ = fixture(direction)
        anchor = self.moving_anchor(anchor, (0, -1), 2.0)

        result = solve_one_cell_gap(
            anchor, world, self.request(target, direction),
        )

        self.assertIs(result.status, SolveStatus.NEEDS_STATE)
        self.assertIn("entry_velocity_direction", result.reasons)

    def test_entry_speed_above_the_frozen_range_is_not_authorized(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, _ = fixture()
        anchor = self.moving_anchor(anchor, (0, 1), 3.01)

        result = solve_one_cell_gap(anchor, world, self.request(target))

        self.assertIs(result.status, SolveStatus.NEEDS_STATE)
        self.assertIn("entry_speed_outside_trial", result.reasons)

    def test_release_recovery_does_not_call_moving_ground_state_safe(self):
        from mc2p.motion_nav.motion_solver import _release_recovery_evidence
        anchor, world, target, _ = fixture()
        fast_edge_state = replace(
            anchor.physics_state,
            position=(0.5, 64.0, 0.8),
            velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.3),
        )

        safe, _, _, status, _, _ = _release_recovery_evidence(
            (fast_edge_state, fast_edge_state),
            world,
            self.request(target),
        )

        self.assertIsNone(status)
        self.assertEqual(safe, ())

    def test_missing_world_and_unsupported_body_are_not_called_blocked(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, gap = fixture(missing=((0, 65, 1),))
        result = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(result.status, SolveStatus.NEEDS_WORLD)
        self.assertIn((0, 65, 1), result.missing_cells)

        anchor, world, target, _ = fixture(using_item=True)
        result = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(result.status, SolveStatus.UNSUPPORTED)
        self.assertIn("item_slowdown_not_supported", result.reasons)

    def test_known_low_ceiling_is_rejected_by_full_trajectory_validation(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, _ = fixture(ceiling=True)
        result = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(result.status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH)
        self.assertIn("validated_candidates_failed", result.reasons)

    def test_resource_and_search_budget_failures_stay_distinct(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, _ = fixture(food=6)
        hungry = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(hungry.status, SolveStatus.NEEDS_STATE)
        self.assertIn("sprint_eligibility", hungry.reasons)

        anchor, world, target, _ = fixture(ceiling=True)
        exhausted = solve_one_cell_gap(
            anchor, world, self.request(target, candidates=1),
        )
        self.assertIs(exhausted.status, SolveStatus.BUDGET_EXHAUSTED)
        self.assertEqual(exhausted.candidates_evaluated, 1)

    def test_full_trajectory_validator_rejects_lost_recovery_margin(self):
        from mc2p.motion_nav.motion_solver import (
            SolveStatus, validate_gap_trajectory, solve_one_cell_gap,
        )
        anchor, world, target, _ = fixture()
        solved = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(solved.status, SolveStatus.SOLVED)
        too_narrow = replace(
            self.request(target),
            landing=replace(self.request(target).landing,
                            min_z=target[2] + .19, max_z=target[2] + .2),
        )
        validation = validate_gap_trajectory(
            solved.proof.trajectory, solved.proof.step_events, too_narrow,
        )
        self.assertFalse(validation.accepted)
        self.assertIn("landing_recovery_margin", validation.reasons)

        collision_index = next(
            index for index, state in enumerate(solved.proof.trajectory[:-1])
            if state.velocity_blocks_per_tick[1] > 0.0
        )
        collided_events = tuple(
            events + (("vertical_collision",) if index == collision_index else ())
            for index, events in enumerate(solved.proof.step_events)
        )
        collision = validate_gap_trajectory(
            solved.proof.trajectory, collided_events, self.request(target),
        )
        self.assertFalse(collision.accepted)
        self.assertIn("ceiling_collision", collision.reasons)

    def test_solver_contract_is_available_from_public_motion_package(self):
        import mc2p.motion_nav as motion_nav
        from mc2p.motion_nav.motion_solver import (
            GapSolveRequest, solve_one_cell_gap,
        )
        self.assertIs(motion_nav.GapSolveRequest, GapSolveRequest)
        self.assertIs(motion_nav.solve_one_cell_gap, solve_one_cell_gap)

    def test_formal_policy_is_loaded_without_expanding_the_legacy_profile(self):
        import json
        from pathlib import Path
        from mc2p.motion_nav.motion_solver import (
            DEFAULT_GAP_SOLVER_POLICY, load_gap_solver_policy,
        )
        path = Path("config/motion-navigation/air-motions-b09-v1.json")

        policy = load_gap_solver_policy(path)
        document = json.loads(path.read_text("utf-8"))
        legacy = next(
            profile for profile in document["profiles"]
            if profile["mode"] == "jump_gap"
        )

        self.assertEqual(policy, DEFAULT_GAP_SOLVER_POLICY)
        self.assertEqual(policy.maximum_entry_speed_blocks_per_second, 3.0)
        self.assertEqual(len(policy.templates), 12)
        self.assertEqual(
            legacy["entry"]["maximum_horizontal_speed_blocks_per_second"],
            0.1,
        )

    def test_moving_exit_binds_the_first_tick_of_the_following_turn(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
        anchor, world, target, _ = fixture()
        request = replace(
            self.request(target),
            exit_direction=(1, 0),
            exit_motion_ticks=1,
        )

        result = solve_one_cell_gap(anchor, world, request)

        self.assertIs(result.status, SolveStatus.SOLVED)
        self.assertEqual(
            result.proof.commands[-1].required_movement_yaw_radians,
            -math.pi / 2,
        )
        self.assertEqual(result.proof.commands[-1].movement.forward, 1)
        self.assertGreater(result.proof.exit_state.velocity_blocks_per_tick[0], 0)
        self.assertTrue(result.proof.exit_state.on_ground)

    def test_stale_background_proof_can_be_revalidated_without_searching_again(self):
        from mc2p.motion_nav.motion_solver import (
            SolveStatus, revalidate_gap_motion, solve_one_cell_gap,
        )
        from mc2p.motion_nav.online_motion import CandidateExecutionWindow
        anchor, world, target, _ = fixture()
        solved = solve_one_cell_gap(anchor, world, self.request(target))
        self.assertIs(solved.status, SolveStatus.SOLVED)
        later = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 3,
            movement_tick_id=anchor.movement_tick_id + 3,
            physics_state=replace(
                anchor.physics_state,
                movement_tick_id=anchor.movement_tick_id + 3,
            ),
        )

        refreshed = revalidate_gap_motion(
            solved.proof, later, world,
            CandidateExecutionWindow(14, 15),
        )

        self.assertIs(refreshed.status, SolveStatus.SOLVED)
        self.assertEqual(refreshed.candidates_evaluated, 0)
        self.assertEqual(refreshed.proof.anchor_observation_sequence_id, 6)
        self.assertEqual(refreshed.proof.anchor_movement_tick_id, 13)
        self.assertEqual(refreshed.proof.entry_state, later.physics_state)
        self.assertEqual(
            refreshed.proof.execution_window,
            CandidateExecutionWindow(14, 15),
        )
        self.assertEqual(refreshed.proof.commands, solved.proof.commands)

    def test_revalidation_rejects_a_changed_entry_instead_of_reusing_old_exit(self):
        from mc2p.motion_nav.motion_solver import (
            SolveStatus, revalidate_gap_motion, solve_one_cell_gap,
        )
        from mc2p.motion_nav.online_motion import CandidateExecutionWindow
        anchor, world, target, _ = fixture()
        solved = solve_one_cell_gap(anchor, world, self.request(target))
        moved = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=anchor.movement_tick_id + 1,
            physics_state=replace(
                anchor.physics_state,
                movement_tick_id=anchor.movement_tick_id + 1,
                position=(.8, 64.0, .5),
            ),
        )

        refreshed = revalidate_gap_motion(
            solved.proof, moved, world,
            CandidateExecutionWindow(12, 13),
        )

        self.assertIs(refreshed.status, SolveStatus.NEEDS_STATE)
        self.assertIn("entry_state_outside_revalidation_envelope", refreshed.reasons)


if __name__ == "__main__":
    unittest.main()
