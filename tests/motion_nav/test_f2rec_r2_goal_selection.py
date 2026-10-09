"""R2 selects a support face by the same completion-region contract as admission."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.support_surfaces import StandableRegionResult, query_support_surfaces
from mc2p.motion_nav.world_model import Aabb
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.f2r_cases import goal_for, materialized_manifest


class RecoveryGoalSelectionTests(unittest.TestCase):
    def _fixture(self, goal_position=(.5, 64., .5), target="product"):
        scene = Scene({(x, 63, z): "minecraft:stone"
                       for x in range(-4, 5) for z in range(-4, 5)},
                      ((-5, 5), (60, 68), (-5, 5)))
        backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
        return SimpleNamespace(world=backend.world._world), goal_for(goal_position, target)

    def test_no_positive_completion_region_cannot_be_selected(self):
        frame, goal = self._fixture()
        with patch("mc2p.motion_nav.navigation_session.standable_region_in_goal",
                   return_value=StandableRegionResult(QueryStatus.BLOCKED), create=True) as query:
            node, missing = NavigationSession._surface_for_goal(frame, goal)
        self.assertIsNone(node)
        self.assertEqual(missing, ())
        self.assertGreater(query.call_count, 0)

    def test_old_preferred_face_keeps_priority_over_larger_completion_region(self):
        frame, goal = self._fixture((.57, 64., .53), "follow")
        node, missing = NavigationSession._surface_for_goal(frame, goal)
        self.assertEqual(node, query_support_surfaces(frame.world, 0, 0, 64., 64.).surfaces[0].node_id)
        self.assertEqual(missing, ())

    def test_follow_goal_queries_only_faces_that_can_change_the_old_choice(self):
        import mc2p.motion_nav.support_surfaces as surfaces
        frame, goal = self._fixture((.57, 64., .53), "follow")
        with patch("mc2p.motion_nav.navigation_session.standable_region_in_goal",
                   wraps=surfaces.standable_region_in_goal, create=True) as query:
            node, missing = NavigationSession._surface_for_goal(frame, goal)
        self.assertIsNotNone(node)
        self.assertEqual(missing, ())
        self.assertEqual(query.call_count, 1)
        self.assertIsNotNone(query.call_args.kwargs.get("query_cache"))

    def test_rejected_preferred_face_uses_next_existing_choice(self):
        import mc2p.motion_nav.support_surfaces as surfaces
        frame, goal = self._fixture((.57, 64., .53), "follow")
        original = query_support_surfaces(frame.world, 0, 0, 64., 64.).surfaces[0].node_id
        def blocked_preferred(world, surface, region, **kwargs):
            if surface.node_id == original:
                return StandableRegionResult(QueryStatus.BLOCKED)
            return surfaces.standable_region_in_goal(world, surface, region, **kwargs)
        with patch("mc2p.motion_nav.navigation_session.standable_region_in_goal",
                   side_effect=blocked_preferred, create=True) as query:
            node, missing = NavigationSession._surface_for_goal(frame, goal)
        self.assertIsNotNone(node)
        self.assertNotEqual(node, original)
        self.assertEqual(missing, ())
        self.assertGreater(query.call_count, 1)
        self.assertEqual(len({id(call.kwargs["query_cache"]) for call in query.call_args_list}), 1)

    def test_selection_does_not_read_wall_clock(self):
        frame, goal = self._fixture((.57, 64., .53), "follow")
        with patch("mc2p.motion_nav.navigation_session.time.perf_counter_ns",
                   side_effect=AssertionError("decision read wall clock")), \
             patch("mc2p.motion_nav.navigation_session.time.monotonic_ns",
                   side_effect=AssertionError("decision read wall clock")):
            node, missing = NavigationSession._surface_for_goal(frame, goal)
        self.assertIsNotNone(node)
        self.assertEqual(missing, ())

    def test_c04_formal_chain_preserves_existing_success(self):
        from scripts.f2r_piecewise_evidence import formal
        case = next(row for row in materialized_manifest()["clutter_scan"]
                    if row["id"] == "f2r/clutter/0.2/3/9/product")
        row = formal(case)
        self.assertEqual(row["outcome"], "success")
        self.assertFalse(row["violations"])
        self.assertEqual(row["damage"], 0)
        self.assertTrue(row["source_released"])
