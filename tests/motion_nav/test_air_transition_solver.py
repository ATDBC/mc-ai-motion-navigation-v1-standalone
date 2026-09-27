from dataclasses import replace
import math
from pathlib import Path
import unittest

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, MotionTickPhase, StateAnchor,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


SESSION = WorldSessionId("air-transition-solver-test")


def fixture(kind, direction=(0, 1), *, speed=1.5, missing=()):
    from mc2p.motion_nav.motion_solver import MotionSolveKind

    dx, dz = direction
    target_support_y = {
        MotionSolveKind.JUMP_GAP: 63,
        MotionSolveKind.JUMP_UP: 64,
        MotionSolveKind.CONTROLLED_DROP: 62,
    }[kind]
    target_distance = 2 if kind is MotionSolveKind.JUMP_GAP else 1
    target_cell = (target_distance * dx, target_support_y, target_distance * dz)
    gap_cell = (dx, 63, dz)
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
    blocks = {(0, 63, 0): BlockGeometry.full_cube("minecraft:grass_block")}
    blocks[target_cell] = BlockGeometry.full_cube("minecraft:grass_block")
    if kind is MotionSolveKind.JUMP_GAP:
        blocks.update({
            (x, 63, z): BlockGeometry.full_cube("minecraft:grass_block")
            for x in range(-4, 5)
            for z in range(-4, 5)
            if (x, 63, z) not in {gap_cell, target_cell}
            and (x, 63, z) not in missing
        })
    knowledge.observe_blocks(stamp, blocks)
    yaw = math.atan2(-dx, dz)
    state = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        SESSION, 10, (.5, 64., .5),
        (dx * speed / 20.0, -.0784000015258789, dz * speed / 20.0),
        yaw, 0., "standing", .6, 1.8, True, False, True,
        False, False, 0, 0., .1, .6, .08, .42, 20, 5., "survival",
        (), False, False, False, False, False, False,
    )
    anchor = StateAnchor(
        SESSION, 3, 10, MotionTickPhase.AFTER_MOVEMENT,
        None, None, JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema, "mc2p.input-projection.v1", state,
        health_points=20.0, absorption_points=0.0,
    )
    half = .3
    return anchor, PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET), (
        target_cell[0] + half,
        target_cell[0] + 1.0 - half,
        target_cell[2] + half,
        target_cell[2] + 1.0 - half,
        target_support_y + 1.0,
    )


class AirTransitionSolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mc2p.motion_nav.motion_solver import load_air_transition_solver_policies

        cls.policies = load_air_transition_solver_policies(Path(
            "config/motion-navigation/verified-height-transitions-v1.json"
        ))

    def request(self, kind, target, direction=(0, 1), *, budget=None):
        from mc2p.motion_nav.motion_solver import (
            AirTransitionSolveRequest, LandingRegion,
        )

        return AirTransitionSolveRequest(
            kind=kind,
            direction=direction,
            landing=LandingRegion(*target),
            execution_window=CandidateExecutionWindow(11, 12),
            damage_budget=budget or TaskDamageBudget(),
            max_candidates=64,
            max_ticks=40,
            policy=self.policies[kind],
        )

    def test_policy_document_declares_all_three_solve_kinds(self):
        from mc2p.motion_nav.motion_solver import MotionSolveKind

        self.assertEqual(set(self.policies), set(MotionSolveKind))
        for kind, policy in self.policies.items():
            self.assertIs(policy.kind, kind)
            self.assertLessEqual(len(policy.templates), 64)

    def test_jump_up_and_controlled_drop_share_the_verified_solver(self):
        from mc2p.motion_nav.motion_solver import (
            MotionSolveKind, SolveStatus, solve_air_transition,
        )

        for kind in (MotionSolveKind.JUMP_UP, MotionSolveKind.CONTROLLED_DROP):
            for direction in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                for speed in (.5, 1.5, 2.5):
                    with self.subTest(kind=kind, direction=direction, speed=speed):
                        anchor, world, target = fixture(
                            kind, direction, speed=speed,
                        )
                        result = solve_air_transition(
                            anchor, world,
                            self.request(kind, target, direction),
                        )
                        self.assertIs(result.status, SolveStatus.SOLVED)
                        self.assertIs(result.proof.kind, kind)
                        self.assertTrue(result.proof.landing.contains(
                            result.proof.exit_state
                        ))
                        self.assertEqual(
                            result.proof.release_safe_command_indices,
                            tuple(range(len(result.proof.commands))),
                        )
                        self.assertEqual(
                            result.proof.maximum_expected_damage_points, 0.0,
                        )

    def test_delayed_start_is_proved_for_each_new_kind(self):
        from mc2p.motion_nav.motion_solver import (
            MotionSolveKind, SolveStatus, solve_air_transition,
        )

        for kind in (MotionSolveKind.JUMP_UP, MotionSolveKind.CONTROLLED_DROP):
            anchor, world, target = fixture(kind, speed=2.0)
            result = solve_air_transition(anchor, world, self.request(kind, target))
            self.assertIs(result.status, SolveStatus.SOLVED)
            self.assertIsNotNone(result.proof.start_variant(11))
            self.assertIsNotNone(result.proof.start_variant(12))

    def test_wrong_entry_direction_and_unknown_clearance_are_rejected(self):
        from mc2p.motion_nav.motion_solver import (
            MotionSolveKind, SolveStatus, solve_air_transition,
        )

        kind = MotionSolveKind.JUMP_UP
        anchor, world, target = fixture(kind, speed=1.5)
        wrong = replace(
            anchor,
            physics_state=replace(
                anchor.physics_state,
                velocity_blocks_per_tick=(.075, 0.0, 0.0),
            ),
        )
        result = solve_air_transition(wrong, world, self.request(kind, target))
        self.assertIs(result.status, SolveStatus.NEEDS_STATE)
        self.assertIn("entry_velocity_direction", result.reasons)

        anchor, world, target = fixture(kind, missing=((0, 65, 0),))
        result = solve_air_transition(anchor, world, self.request(kind, target))
        self.assertIs(result.status, SolveStatus.NEEDS_WORLD)

    def test_damage_budget_is_part_of_the_request_even_for_zero_damage_actions(self):
        from mc2p.motion_nav.motion_solver import (
            MotionSolveKind, SolveStatus, solve_air_transition,
        )

        kind = MotionSolveKind.CONTROLLED_DROP
        anchor, world, target = fixture(kind, speed=1.5)
        budget = TaskDamageBudget("task-controlled-damage", 2.0)
        result = solve_air_transition(
            anchor, world, self.request(kind, target, budget=budget),
        )
        self.assertIs(result.status, SolveStatus.SOLVED)
        self.assertEqual(result.proof.damage_budget, budget)

    def active_route(self, kind):
        from mc2p.motion_nav.action_route import (
            ActionRoute, ControlledDropSegment, JumpUpSegment,
        )
        from mc2p.motion_nav.controlled_drop import ControlledDropEdge
        from mc2p.motion_nav.jump_up import JumpUpEdge
        from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
        from mc2p.motion_nav.support_surfaces import (
            HorizontalRegion, SupportSurface, SurfaceNodeId,
        )
        from mc2p.motion_nav.motion_solver import MotionSolveKind

        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_y = 65 if kind is MotionSolveKind.JUMP_UP else 63
        end_id = SurfaceNodeId(0, 1, end_y, 0)
        if kind is MotionSolveKind.JUMP_UP:
            action = JumpUpSegment(
                JumpUpEdge(
                    (0, 64, 0), (0, 65, 1),
                    "moving-jump-up", (0, 1), .8, (),
                ),
                (),
            )
        else:
            start_surface = SupportSurface(
                start_id, (.5, 64., .5), HorizontalRegion(0, 0, 1, 1),
                1.0, ("minecraft:grass_block",), (),
            )
            end_surface = SupportSurface(
                end_id, (.5, 63., 1.5), HorizontalRegion(0, 1, 1, 2),
                1.0, ("minecraft:grass_block",), (),
            )
            action = ControlledDropSegment(
                ControlledDropEdge(
                    start_id, end_id, "moving-controlled-drop", .8, (),
                ),
                start_surface, end_surface, (),
            )
        action_route = ActionRoute("moving-height-route", (action,))
        return ActiveRoute(
            "moving-height-route", 1, "request", "goal", 1,
            SESSION.value, None, 1.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 1.0, end_id),
            action_route, planning_generation=1,
        )

    def test_new_kinds_use_the_existing_worker_and_candidate_admission_chain(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        from mc2p.motion_nav.motion_coordination import (
            GapPreparationStatus, prepare_planned_air_transition,
        )
        from mc2p.motion_nav.motion_solver import MotionSolveKind, SolveStatus
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from mc2p.motion_nav.motion_worker import GapMotionSolveJob, _execute_job
        from tests.motion_nav.test_b10_motion_candidate import (
            VerifiedMotionRouteIntegrationTests, air_profile, ground_profile,
            jump_profile, step_profile,
        )
        from mc2p.motion_nav.movement_transition import MovementMode

        for kind in (MotionSolveKind.JUMP_UP, MotionSolveKind.CONTROLLED_DROP):
            with self.subTest(kind=kind):
                anchor, world, target = fixture(kind, speed=1.5)
                request = self.request(kind, target)
                worked = _execute_job(GapMotionSolveJob(
                    f"route/{kind.value}", 1, anchor, world, request,
                ))
                self.assertIs(worked.solve_result.status, SolveStatus.SOLVED)
                route = self.active_route(kind)
                prepared = prepare_planned_air_transition(
                    route, 0, anchor, world,
                    candidate_revision=1, intended_start_tick=11,
                    precomputed=worked.solve_result,
                    policies=self.policies,
                )
                self.assertIs(prepared.status, GapPreparationStatus.READY)
                self.assertIs(prepared.candidate.proof.kind, kind)
                executor = ActionRouteExecutor(
                    ground_profile(), jump_profile(), step_profile(),
                    air_profiles=(
                        air_profile(MovementMode.JUMP_GAP),
                        air_profile(MovementMode.CONTROLLED_DROP),
                    ),
                )
                frame = VerifiedMotionRouteIntegrationTests.frame(
                    world._world, anchor.physics_state, 1,
                )
                executor.start(
                    route.action_route, frame,
                    verified_motion=(prepared.candidate,),
                    require_verified_motion_actions=frozenset({0}),
                )
                decision = executor.decide(
                    frame, state_anchor=anchor,
                    input_ledger=InputApplicationLedger(max_records=64),
                )
                self.assertTrue(decision.submit_input)
                self.assertEqual(
                    decision.movement,
                    prepared.candidate.proof.commands[0].movement,
                )


if __name__ == "__main__":
    unittest.main()
