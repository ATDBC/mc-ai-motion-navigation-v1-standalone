"""D092 RED checks for generic entry yaw and anticipated-work retirement."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment
from mc2p.motion_nav.async_work import AsyncComputationScope
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_solver import SolveResult, SolveStatus
from mc2p.motion_nav.motion_worker import GapMotionSolveResult, MotionSolverWorker
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.support_surfaces import HorizontalRegion, SupportSurface, SurfaceNodeId
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from tests.motion_nav.test_b10_gap_solver import fixture
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
import tests.motion_nav.test_b10_motion_candidate as b10_motion_candidate
from mc2p.motion_nav.movement_transition import MovementMode
from tests.sim.f2s_cases import materialized_manifest
from tests.sim.f2s_cases import scene_for
from tests.sim.f2r_cases import goal_for
from tests.sim.runner import Event, ObservedAsyncActivity, Scenario, run, _goal
from tests.test_player_runtime import _task
from mc2p.motion_nav.world_model import Aabb


class _HoldExternalLook:
    """Keep one real higher-priority view selected through the Runtime."""

    def __init__(self) -> None:
        self.source = None
        self.sequence = 0

    def __call__(self, context):
        driver = context.driver
        runtime = driver.runtime
        deadline = context.clock[0] + 500_000_000
        if self.source is None:
            self.source = runtime.register_ordered_source("handoff-combat-look")
        self.sequence += 1
        intent = ActionIntentV1(
            ordered_intent_id(self.source, self.sequence),
            self.source.source_id,
            self.source.episode_id,
            runtime.observation.sequence_id,
            ActionPriorityV0.SAFETY,
            context.clock[0],
            deadline,
            look=LookV1(15.0, 0.0),
        )
        envelope = OrderedIntentV1(self.source, self.sequence, intent)
        proposals = driver.prepare_proposals(
            deadline, conditioned_look=envelope,
        ) + (ControlFrameProposalV1((envelope,)),)
        result = runtime.control_frame(
            _task(deadline), BehaviorProfileV0(), deadline,
            proposals=proposals,
        )
        driver.adopt_result(result)
        return (intent.intent_id,)


class _StaleNegativeWorker:
    """Return one typed negative on the tick after every anticipated solve."""

    pid = None

    def __init__(self) -> None:
        self.pending = []
        self.submitted = 0
        self.maximum_pending = 0
        self.activity = []

    def is_alive(self) -> bool:
        return True

    def submit(self, job) -> bool:
        self.pending.append(job)
        self.submitted += 1
        self.maximum_pending = max(self.maximum_pending, len(self.pending))
        self.activity.append(ObservedAsyncActivity(job.work_identity, "submit"))
        return True

    def poll_available(self):
        available = tuple(
            GapMotionSolveResult(
                job.connection_id,
                job.candidate_revision,
                SolveResult(
                    SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
                    reasons=("injected_anticipated_negative",),
                ),
                1,
                job.work_identity,
            )
            for job in self.pending
        )
        self.activity.extend(
            ObservedAsyncActivity(job.work_identity, "poll")
            for job in self.pending
        )
        self.pending = []
        return available

    def close(self) -> None:
        self.pending = []


class ActionEntryHandoffRepairTests(unittest.TestCase):
    def test_published_regression_uses_frozen_v7_product_signatures(self):
        root = Path(__file__).resolve().parents[2]
        evidence = root / "evidence/motion_navigation/action-entry-handoff-v1"
        baseline = root / "evidence/motion_navigation/F2REC-recovery-v1/r4"
        summary = json.loads((evidence / "regression-summary.json").read_text("utf-8"))
        current_rows = [
            json.loads(line)
            for line in (evidence / "v7-current-index.jsonl").read_text("utf-8").splitlines()
        ]
        baseline_rows = [
            json.loads(line)
            for line in (baseline / "five-groups/product-index.jsonl").read_text("utf-8").splitlines()
        ]
        current = {row["id"]: row for row in current_rows}
        frozen = {row["id"]: row for row in baseline_rows}
        product = next(row for row in summary["sets"] if row["name"] == "v7-product")

        self.assertEqual(len(current), 2000)
        self.assertEqual(current.keys(), frozen.keys())
        self.assertEqual(
            {identifier: row["signature"] for identifier, row in current.items()},
            {identifier: row["signature"] for identifier, row in frozen.items()},
        )
        self.assertEqual(sum(row["success"] for row in current.values()), 1998)
        self.assertEqual(product["current_successes"], 1998)
        self.assertEqual(product["old_successes"], 1998)
        self.assertEqual(product["old_success_regressions"], [])
        self.assertEqual(product["common_signature_differences"], [])
        self.assertTrue(all(
            row["old_success_regressions"] == []
            and row["common_signature_differences"] == []
            for row in summary["sets"]
        ))

    def test_column_top_uses_formal_chain_to_turn_before_jump(self):
        case = next(
            case for case in materialized_manifest()["support_region_cases"]
            if case["id"] == "f2s/column_top/product/east/normal"
        )
        goal = replace(
            goal_for(tuple(case["goal"]), case["target"]),
            region=Aabb(*case["goal_box"]),
        )
        scenario = Scenario(
            case["id"] + "/entry-turn-proof",
            scene_for(case), tuple(case["start"]), tuple(case["goal"]),
            case["yaw_degrees"], max_ticks=case["max_ticks"],
        )
        entry_yaws = {}

        def control(context):
            active = context.session._active_route
            if active is not None:
                for index, action in enumerate(active.action_route.actions):
                    window = getattr(action, "entry_window", None)
                    if window is not None and window.required_yaw_radians is not None:
                        entry_yaws[index] = window.required_yaw_radians
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
            return ()

        import tests.sim.runner as runner
        with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
            result = run(scenario, control_step=control)

        jumps = [row for row in result.trace if row["applied_movement"]["jump"]]
        self.assertTrue(jumps, "the formal route never entered JumpUp")
        jump = jumps[0]
        required_yaw = entry_yaws[jump["action_index"]]
        preturned_walk = [
            row for row in result.trace
            if row["action_index"] == 0
            and not row["applied_movement"]["jump"]
            and (row["applied_movement"]["forward"]
                 or row["applied_movement"]["strafe"])
            and abs(math.atan2(
                math.sin(row["yaw_radians"] - required_yaw),
                math.cos(row["yaw_radians"] - required_yaw),
            )) <= math.radians(2.0) + 1.0e-9
        ]
        jump_error = abs(math.atan2(
            math.sin(jump["yaw_radians"] - required_yaw),
            math.cos(jump["yaw_radians"] - required_yaw),
        ))
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [])
        self.assertTrue(preturned_walk,
                        "the Walk owner never satisfied the successor yaw")
        self.assertLessEqual(jump_error, math.radians(2.0) + 1.0e-9)
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_walk_owns_required_yaw_while_still_moving_before_jump(self):
        from scripts.action_entry_handoff_evidence import frozen_cases, _scenario

        case = next(
            case for case in frozen_cases()
            if case["family"] == "turn_90"
            and case["direction"] == "south"
            and case["condition"] == "normal"
            and case["variant"] == 2
        )
        scenario, _ = _scenario(case)
        entry_yaws = {}

        def control(context):
            active = context.session._active_route
            if active is not None:
                for index, action in enumerate(active.action_route.actions):
                    window = getattr(action, "entry_window", None)
                    if window is not None and window.required_yaw_radians is not None:
                        entry_yaws[index] = window.required_yaw_radians
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
            return ()

        result = run(scenario, control_step=control)
        jumps = [row for row in result.trace if row["applied_movement"]["jump"]]
        self.assertTrue(jumps, "the formal route never entered JumpUp")
        jump = jumps[0]
        required_yaw = entry_yaws[jump["action_index"]]
        misaligned_walk = [
            row for row in result.trace
            if row["loop_tick"] < jump["loop_tick"]
            and row["action_kind"] == "WalkSegment"
            and abs(math.atan2(
                math.sin(row["yaw_radians"] - required_yaw),
                math.cos(row["yaw_radians"] - required_yaw),
            )) > math.radians(2.0) + 1.0e-9
        ]
        moving_walk = [
            row for row in result.trace
            if row["loop_tick"] < jump["loop_tick"]
            and row["action_kind"] == "WalkSegment"
            and (row["applied_movement"]["forward"]
                 or row["applied_movement"]["strafe"])
            and abs(math.atan2(
                math.sin(row["yaw_radians"] - required_yaw),
                math.cos(row["yaw_radians"] - required_yaw),
            )) <= math.radians(2.0) + 1.0e-9
        ]

        self.assertEqual(result.outcome, "success", result.reason)
        self.assertTrue(misaligned_walk,
                        "the RED route started already inside the yaw window")
        self.assertTrue(moving_walk,
                        "successor yaw was only repaired after Walk stopped")
        self.assertEqual(result.violations, [])

    def test_external_look_keeps_final_yaw_authority_and_bounds_entry_wait(self):
        case = next(
            case for case in materialized_manifest()["support_region_cases"]
            if case["id"] == "f2s/column_top/product/east/normal"
        )
        goal = replace(
            goal_for(tuple(case["goal"]), case["target"]),
            region=Aabb(*case["goal_box"]),
        )
        scenario = Scenario(
            case["id"] + "/combat-look",
            scene_for(case), tuple(case["start"]), tuple(case["goal"]),
            case["yaw_degrees"], max_ticks=case["max_ticks"],
        )
        external = _HoldExternalLook()

        import tests.sim.runner as runner
        with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
            result = run(scenario, control_step=external)

        self.assertNotEqual(result.outcome, "success")
        self.assertFalse(any(
            row["applied_movement"]["jump"] for row in result.trace
        ))
        first_yaws = [row["yaw_radians"] for row in result.trace[:4]]
        self.assertEqual(len(first_yaws), 4)
        self.assertTrue(all(
            abs((new - old) - .2617993877991494) <= 1.0e-9
            for old, new in zip(first_yaws, first_yaws[1:])
        ))
        self.assertGreater(sum(
            any(frame["applied_movement"].values()) for frame in result.trace
        ), 0)
        self.assertEqual(result.violations, [])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(all(row["runtime_failure"] is None for row in result.trace))

    def test_repeated_stale_anticipated_results_keep_one_job_and_end_bounded(self):
        case = next(
            case for case in materialized_manifest()["support_region_cases"]
            if case["id"] == "f2s/column_top/product/east/normal"
        )
        goal = replace(
            goal_for(tuple(case["goal"]), case["target"]),
            region=Aabb(*case["goal_box"]),
        )
        scenario = Scenario(
            case["id"] + "/stale-loop",
            scene_for(case), tuple(case["start"]), tuple(case["goal"]),
            case["yaw_degrees"], max_ticks=case["max_ticks"],
            expect="failed",
        )
        external = _HoldExternalLook()
        workers = []
        maximum_mailboxes = 0
        maximum_failures = 0

        def factory():
            worker = _StaleNegativeWorker()
            workers.append(worker)
            return worker

        def control(context):
            nonlocal maximum_mailboxes, maximum_failures
            selected = external(context)
            maximum_mailboxes = max(
                maximum_mailboxes,
                len(context.session.active_motion_mailboxes),
            )
            coordinator = context.session._supervisor.route
            if isinstance(coordinator, MotionRouteCoordinator):
                maximum_failures = max(
                    maximum_failures,
                    coordinator._local_attempts.failure_count,
                )
            return selected

        import tests.sim.runner as runner
        with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
            result = run(
                scenario,
                control_step=control,
                motion_factory=factory,
            )

        self.assertEqual(len(workers), 1)
        self.assertGreaterEqual(workers[0].submitted, 2,
                                "continuous stale path was not exercised")
        self.assertEqual(workers[0].maximum_pending, 1)
        self.assertLessEqual(maximum_mailboxes, 1)
        self.assertEqual(maximum_failures, 0,
                         "typed stale negatives spent the real failure budget")
        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertLess(result.ticks, scenario.max_ticks,
                        "the original no-progress bound did not end the task")
        self.assertEqual(result.violations, [])
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_goal_revision_and_cancel_during_entry_turn_retire_old_look(self):
        case = next(
            case for case in __import__(
                "scripts.action_entry_handoff_evidence",
                fromlist=["frozen_cases"],
            ).frozen_cases()
            if case["family"] == "turn_90"
            and case["direction"] == "south"
            and case["condition"] == "normal"
            and case["variant"] == 2
        )
        from scripts.action_entry_handoff_evidence import _scenario

        base, _ = _scenario(case)
        initial_yaw = base.yaw_degrees

        def turning(context):
            return (
                context.backend.state.on_ground
                and abs(math.degrees(context.backend.state.yaw_radians) - initial_yaw) >= 10.0
            )

        def revise(context):
            position = tuple(case["start"])
            goal = _goal(position, context.risk_policy_id)
            context.driver.replace_goal(
                "goal", 2, goal, context.clock[0],
                damage_budget=TaskDamageBudget(
                    context.risk_policy_id, context.damage_points,
                ),
            )
            context.goal_state = goal
            context.goal_position = position

        revised = run(replace(
            base,
            name=base.name + "/revise-during-turn",
            events=[Event(
                "revise_during_turn", turning, revise,
                kind="goal_revision", goal_revision=2,
            )],
        ))
        cancelled = run(replace(
            base,
            name=base.name + "/cancel-during-turn",
            events=[Event(
                "cancel_during_turn", turning,
                lambda context: context.driver.release("cancel_during_entry_turn"),
                kind="cancel",
            )],
            expect="cancelled",
        ))

        for result, expected in ((revised, "success"), (cancelled, "cancelled")):
            self.assertEqual(len(result.event_dispatches), 1,
                             "entry-turn interruption was not dispatched")
            interrupted_at = result.event_dispatches[0][1]
            after = [row for row in result.trace if row["loop_tick"] >= interrupted_at]
            self.assertTrue(after)
            self.assertFalse(any(row["applied_movement"]["jump"] for row in after),
                             "the retired entry look authorized the old jump")
            self.assertEqual(result.outcome, expected, result.reason)
            self.assertEqual(result.violations, [])
            self.assertFalse(result.trace[-1]["source_bound"])

    def test_fabric_plan_freezes_four_directions_and_two_input_conditions(self):
        from scripts.f2_ground_route_runtime import frozen_plan

        plan = frozen_plan(handoff=True)

        self.assertEqual(len(plan), 8)
        self.assertEqual(
            {(row["direction"], row["condition"]) for row in plan},
            {(direction, condition) for direction in range(4)
             for condition in ("normal", "late_first")},
        )
        self.assertTrue(all(row["family"] == "handoff_column_top" for row in plan))
        self.assertTrue(all(row["expected"] == "success" for row in plan))

    def test_formal_matrix_freezes_all_declared_positive_families(self):
        from scripts.action_entry_handoff_evidence import frozen_cases

        plan = frozen_cases()

        self.assertEqual(len(plan), 80)
        self.assertEqual(
            {row["family"] for row in plan},
            {"straight_wrong_yaw", "turn_90", "short_run_up", "column_top"},
        )
        self.assertEqual(
            {(row["direction"], row["condition"]) for row in plan},
            {(direction, condition)
             for direction in ("south", "east", "north", "west")
             for condition in ("normal", "first_late")},
        )

    def test_stale_anticipated_negative_retires_without_buying_real_failure(self):
        anchor, world, _, _ = fixture()
        first_id = SurfaceNodeId(0, 0, 64, 0)
        middle_id = SurfaceNodeId(0, 2, 64, 0)
        final_id = SurfaceNodeId(0, 4, 64, 0)
        first_surface = SupportSurface(
            first_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        middle_surface = SupportSurface(
            middle_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        final_surface = SupportSurface(
            final_id, (.5, 64.0, 4.5), HorizontalRegion(0, 4, 1, 5),
            1.0, ("minecraft:grass_block",), (),
        )
        first = JumpGapSegment(
            JumpGapEdge(first_id, middle_id, "test-jump-gap", .9, ()),
            first_surface, middle_surface, (),
        )
        second = JumpGapSegment(
            JumpGapEdge(middle_id, final_id, "test-second-jump-gap", .9, ()),
            middle_surface, final_surface, (),
        )
        route = ActiveRoute(
            "stale-anticipated", 1, "request", "goal", 1,
            anchor.session.value, None, 4.0, 0.0, (),
            ExecutableCorridor((first_id, middle_id, final_id), (), 4.0, final_id),
            ActionRoute("stale-anticipated", (first, second)),
            planning_generation=2,
        )
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        retry = RetryLedger("motion-task")
        scope = AsyncComputationScope(route.world_session, retry.task_id, 1)
        with MotionSolverWorker(max_pending=1) as worker:
            coordinator = MotionRouteCoordinator(
                route, executor, worker, retry_ledger=retry,
                computation_scope=scope,
            )
            coordinator.start(
                b10_motion_candidate.VerifiedMotionRouteIntegrationTests.frame(
                    world._world, anchor.physics_state, 1,
                )
            )
            coordinator._submit_action(1, anchor, world)
            identity = coordinator._work_identity
            connection = coordinator._pending_connection
            revision = coordinator._candidate_revision
            self.assertIsNotNone(identity)
            self.assertIsNotNone(connection)
            changed_anchor = replace(
                anchor,
                physics_state=replace(
                    anchor.physics_state,
                    pitch_radians=anchor.physics_state.pitch_radians + .1,
                ),
            )
            rejected = GapMotionSolveResult(
                connection, revision,
                SolveResult(
                    SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
                    reasons=("old_predicted_entry_rejected",),
                ),
                0,
                identity,
            )

            installed = coordinator._accept_result(
                rejected, changed_anchor, world, (),
                current_scope=scope,
            )

            self.assertFalse(installed)
            self.assertEqual(coordinator._local_attempts.failure_count, 0)
            self.assertFalse(executor._cancel_requested)
            self.assertIsNone(coordinator._work_identity)
            self.assertEqual(coordinator.last_failure_reason, "")


if __name__ == "__main__":
    unittest.main()
