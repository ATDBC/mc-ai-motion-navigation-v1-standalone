"""F2-SS ordinary WALK uses one complete formal candidate family."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.ground_candidate_verifier import (
    GroundCandidateFamilyStatus,
    GroundCandidateVerifier,
)
from mc2p.motion_nav.ground_tracking_policy import GroundTrackingPolicy
from mc2p.motion_nav.fixed_route import FixedRouteController
from mc2p.motion_nav.ground_traversal import verify_ground_traversal
from mc2p.motion_nav.world_model import WorldQueryCache
from scripts.f2_ground_route_evidence import _frame, run_route
from tests.motion_nav.test_f2_non_center_ground_route import case
from tests.motion_nav.test_ground_traversal import traversal_fixture
from tests.motion_nav.test_f2cv_ground_candidate_verifier import (
    _context,
    _fixture,
)


class SingleGroundCandidateSourceTests(unittest.TestCase):
    def _report(self):
        backend, frame, profile = _fixture()
        cache = WorldQueryCache(frame.world)
        context = _context(frame, backend.state, profile, cache)
        report = GroundCandidateVerifier().verify(
            frame,
            backend.state,
            context,
            tail_ticks=30,
            minimum_support=.15,
            profile=profile,
            query_cache=cache,
        )
        return backend, frame, profile, context, report

    def test_complete_family_exposes_both_endpoints_and_split_trajectories(self):
        _, _, _, _, report = self._report()

        self.assertIs(report.status, GroundCandidateFamilyStatus.COMPLETE)
        self.assertEqual(len(report.candidates), 18)
        for item in report.candidates:
            with self.subTest(
                forward=item.movement.forward,
                strafe=item.movement.strafe,
                ticks=item.control_ticks,
            ):
                self.assertIsNotNone(item.tracking_end)
                self.assertIsNotNone(item.stopped_end)
                self.assertEqual(item.verified_prefix[-1], item.tracking_end)
                self.assertEqual(item.verified_neutral_tail[0], item.tracking_end)
                self.assertEqual(item.verified_neutral_tail[-1], item.stopped_end)
                self.assertEqual(item.dependencies, item.result.dependencies)

    def test_policy_scores_complete_report_without_legacy_rollout_or_world_query(self):
        _, _, _, context, report = self._report()

        with (
            patch.object(
                GroundTrackingPolicy,
                "_rollout",
                side_effect=AssertionError("legacy rollout entered"),
            ),
            patch.object(
                GroundTrackingPolicy,
                "_evaluate",
                side_effect=AssertionError("legacy world query entered"),
            ),
        ):
            evaluated = GroundTrackingPolicy.verified_candidates(context, report)

        self.assertEqual(len(evaluated), 18)
        self.assertEqual(
            [(item.movement.forward, item.movement.strafe, item.control_ticks)
             for item in evaluated],
            [(item.movement.forward, item.movement.strafe, ticks)
             for item in report.candidates for ticks in (item.control_ticks,)],
        )

    def test_budget_exhausted_family_exposes_no_quality_candidates(self):
        backend, frame, profile = _fixture()
        cache = WorldQueryCache(frame.world)
        context = _context(frame, backend.state, profile, cache)
        ticks = iter((0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20))
        report = GroundCandidateVerifier(clock_ns=lambda: next(ticks, 20)).verify(
            frame,
            backend.state,
            context,
            tail_ticks=30,
            minimum_support=.15,
            profile=profile,
            query_cache=cache,
            deadline_ns=5,
        )

        self.assertIs(report.status, GroundCandidateFamilyStatus.BUDGET_EXHAUSTED)
        self.assertEqual(GroundTrackingPolicy.verified_candidates(context, report), ())

    def test_quality_changes_do_not_rewrite_world_safety(self):
        _, _, _, context, report = self._report()
        narrow = replace(
            context,
            limits=replace(context.limits, maximum_cross_track_blocks=.05),
        )

        GroundTrackingPolicy.verified_candidates(context, report)
        GroundTrackingPolicy.verified_candidates(narrow, report)

        self.assertTrue(report.complete)
        self.assertEqual(len(report.safe_candidates), sum(
            item.status.value == "safe" for item in report.candidates
        ))

    def test_formal_fixed_route_uses_one_verifier_and_no_legacy_candidate_path(self):
        calls = 0
        original = GroundCandidateVerifier.verify
        original_decide = FixedRouteController.decide
        per_frame = []

        def counted(verifier, *args, **kwargs):
            nonlocal calls
            calls += 1
            return original(verifier, *args, **kwargs)

        def captured(controller, *args, **kwargs):
            before = calls
            decision = original_decide(controller, *args, **kwargs)
            per_frame.append((decision.declared_candidates, calls-before))
            return decision

        with (
            patch.object(GroundCandidateVerifier, "verify", counted),
            patch.object(FixedRouteController, "decide", captured),
            patch.object(
                FixedRouteController,
                "_ranked_tracking_candidates",
                side_effect=AssertionError("legacy tracking candidates entered"),
            ),
            patch.object(
                GroundTrackingPolicy,
                "safe_tail",
                side_effect=AssertionError("legacy safe tail entered"),
            ),
        ):
            row = run_route(case("tangent"))

        self.assertTrue(row["success"], row)
        self.assertGreater(calls, 0)
        self.assertTrue(all(
            count == 1 for declared, count in per_frame if declared == 18
        ))
        self.assertTrue(all(count in (0, 1) for _, count in per_frame))

    def test_strict_height_traversal_does_not_enter_ordinary_candidate_family(self):
        state, route, world = traversal_fixture()
        _, _, profile = _fixture()
        plan = verify_ground_traversal(
            state, route, world, profile, maximum_ticks=80,
        ).plan
        self.assertIsNotNone(plan)
        controller = FixedRouteController(profile)
        actor = world._world
        controller.start(route, _frame(state, actor, 0), traversal_plan=plan)

        with patch.object(
            GroundCandidateVerifier,
            "verify",
            side_effect=AssertionError("strict action entered ordinary verifier"),
        ):
            decision = controller.decide(
                _frame(state, actor, 1), physics_state=state,
            )

        self.assertNotEqual(decision.reason, "no_safe_ground_candidate")


if __name__ == "__main__":
    unittest.main()
