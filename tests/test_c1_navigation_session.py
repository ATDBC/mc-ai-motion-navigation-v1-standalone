"""C1 uses the formal navigation session without reviving legacy navigation."""
from __future__ import annotations

from pathlib import Path
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.navigation_session import NavigationSessionState
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteState,
)
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.test_fixed_melee_driver import MeleeBackend
from tests.test_player_runtime import _RecordingTrace
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.contracts.reset import ResetRequestV0
from tests.navigation_session_fixtures import FakeNavigationSession


class C1NavigationSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(
            self.backend, _RecordingTrace(), lambda: self.clock[0],
        )
        self.assertTrue(self.runtime.reset(ResetRequestV0(
            "reset", "episode-1", "test", 1, 2_000_000_000,
        )).succeeded)

    def tearDown(self):
        self.runtime.close()

    def test_bridge_submits_one_session_proposal_and_tracks_target(self):
        session = FakeNavigationSession()
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
            observation_request=ObservationRequestV3(
                "navigation_v1", entity_track_id="entity-zombie-1",
            ),
        )
        from mc2p.skills.moving_target import decide_moving_goal
        from mc2p.skills.engagement_memory import (
            TargetPositionFactV1, TargetPositionSource,
        )
        from mc2p.contracts.observation import Vec3V0
        goal = decide_moving_goal(
            TargetPositionFactV1(
                "entity-zombie-1", TargetPositionSource.VISION,
                Vec3V0(0, 0, 5), Vec3V0(0, 0, 0), 0, self.clock[0],
                20, 20, False,
            ),
            Vec3V0(.5, 64, .5), "scope", self.clock[0] + 2_000_000_000,
        ).goal_state

        driver.start("combat-goal", 1, goal, self.clock[0])
        before = self.backend.sequence
        result = driver.tick(BehaviorProfileV0(), self.clock[0] + 500_000_000)

        self.assertEqual(self.backend.sequence, before + 1)
        self.assertEqual(result.decision.action.movement, MovementV1(forward=1))
        self.assertEqual(self.backend.query_track, "entity-zombie-1")
        self.assertEqual(session.starts[0][:2], ("combat-goal", 1))

    def test_bridge_passes_current_anchor_and_runtime_owned_input_ledger(self):
        session = FakeNavigationSession()
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.world_model import Aabb
        goal = GoalState(
            Aabb(0, 64, 1, 1, 64.2, 2), GoalSupport.SOLID,
            frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
        )
        driver.start("combat-goal", 1, goal, self.clock[0])

        driver.tick(BehaviorProfileV0(), self.clock[0] + 500_000_000)

        self.assertEqual(len(session.execution_anchor_requests), 1)
        self.assertIs(session.execution_anchor_requests[0][1], self.runtime.input_ledger)
        self.assertIs(session.proposal_anchors[-1], session.execution_anchor_token)
        self.assertIs(session.proposal_ledgers[-1], self.runtime.input_ledger)

    def test_bridge_registers_selected_verified_command_with_runtime_sequence(self):
        session = FakeNavigationSession()
        session.movement = MovementV1(forward=1, jump=True, sprint=True)
        session.route_decision = ActionRouteDecision(
            ActionRouteState.RUNNING, session.movement, None,
            1, 0, "submit_verified_command", (), 0, True,
            0, 1, 2,
        )
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.world_model import Aabb
        goal = GoalState(
            Aabb(0, 64, 1, 1, 64.2, 2), GoalSupport.SOLID,
            frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
        )
        driver.start("combat-goal", 1, goal, self.clock[0])

        result = driver.tick(
            BehaviorProfileV0(), self.clock[0] + 500_000_000,
        )

        self.assertEqual(len(session.verified_submissions), 1)
        proposal, control_sequence = session.verified_submissions[0]
        self.assertIs(proposal.route_decision, session.route_decision)
        self.assertEqual(
            control_sequence, result.decision.action.request_sequence_id,
        )

    def test_bridge_ingests_each_runtime_observation_only_when_it_is_needed(self):
        session = FakeNavigationSession()
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.world_model import Aabb
        goal = GoalState(
            Aabb(0, 64, 1, 1, 64.2, 2), GoalSupport.SOLID,
            frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
        )
        driver.start("combat-goal", 1, goal, self.clock[0])
        frames_before_tick = tuple(session.frames)

        driver.tick(BehaviorProfileV0(), self.clock[0] + 500_000_000)

        self.assertEqual(
            tuple(session.frames[len(frames_before_tick):]),
            (frames_before_tick[-1],),
            "the returned observation is consumed at the start of the next public tick",
        )

    def test_bridge_merges_session_world_queries_with_entity_tracking(self):
        session = FakeNavigationSession()
        session.request_positions = ((0, 62, 0),)
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
            observation_request=ObservationRequestV3(
                "navigation_v1", entity_track_id="entity-zombie-1",
            ),
        )
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.world_model import Aabb
        goal = GoalState(
            Aabb(0, 64, 1, 1, 64.2, 2), GoalSupport.SOLID,
            frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
        )

        driver.start("combat-goal", 1, goal, self.clock[0])
        driver.tick(BehaviorProfileV0(), self.clock[0] + 500_000_000)

        self.assertEqual(self.backend.query_track, "entity-zombie-1")
        self.assertEqual(self.backend.query_air, ((0, 62, 0),))

    def test_same_goal_uses_a_new_revision_and_cancel_releases_owner(self):
        session = FakeNavigationSession()
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.world_model import Aabb
        goal = GoalState(
            Aabb(0, 64, 1, 1, 64.2, 2), GoalSupport.SOLID,
            frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
        )
        driver.start("combat-goal", 1, goal, self.clock[0])
        driver.replace_goal("combat-goal", 2, goal, self.clock[0])
        self.assertEqual(session.updates[0][:2], ("combat-goal", 2))

        driver.stop(BehaviorProfileV0(), "cancelled")
        self.assertEqual(session.state, NavigationSessionState.CANCELLED)
        self.assertIsNone(session.source)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 0)

    def test_stop_keeps_input_owner_until_session_finishes_safe_cancellation(self):
        session = FakeNavigationSession(cancel_steps=2)
        driver = RuntimeNavigationDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.world_model import Aabb
        goal = GoalState(
            Aabb(0, 64, 1, 1, 64.2, 2), GoalSupport.SOLID,
            frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
        )
        driver.start("combat-goal", 1, goal, self.clock[0])

        driver.stop(BehaviorProfileV0(), "cancelled")

        self.assertEqual(driver.state, "stopping")
        self.assertIsNotNone(driver.source)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)

        driver.tick(BehaviorProfileV0(), self.clock[0] + 500_000_000)

        self.assertEqual(driver.state, "cancelled")
        self.assertIsNone(driver.source)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 0)

    def test_formal_c1_modules_do_not_import_legacy_navigation(self):
        root = Path(__file__).resolve().parents[1]
        names = (
            "mc2p/skills/fixed_melee_driver.py",
            "mc2p/skills/moving_melee_driver.py",
            "mc2p/skills/external_motion_recovery_driver.py",
            "scripts/c1_fixed_melee_runtime.py",
            "scripts/c1_moving_melee_runtime.py",
            "scripts/c1_external_motion_runtime.py",
        )
        forbidden = (
            "skills.navigation_state", "skills.point_goal_driver",
            "skills.point_goal_policy",
        )
        for name in names:
            text = (root / name).read_text("utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{name} imports {token}")

    def test_b10_formal_coordinator_uses_the_same_navigation_session(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "scripts/b10_gap_solver_runtime.py").read_text("utf-8")

        self.assertIn("NavigationSession(", text)
        self.assertNotIn("MotionRouteCoordinator(", text)
        self.assertNotIn("ActiveRoute(", text)


if __name__ == "__main__":
    unittest.main()
