"""F1-B thin target manager over the formal Runtime navigation driver."""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import ControlFrameProposalV1
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.contracts.observation_v3 import TrackedEntityStateV3
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.navigation_session import NavigationSessionState
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.known_world_follow_driver import (
    FOLLOW_HOLD_DISTANCE_BLOCKS,
    FOLLOW_MAX_REVISION_INTERVAL_NS,
    FOLLOW_REVISION_DISTANCE_BLOCKS,
    KnownWorldFollowDriver,
    KnownWorldFollowState,
    KnownWorldFollowStatus,
)
from mc2p.skills.follow_types import MOVEMENT_FRESHNESS_NS
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.follow_fixtures import player_value
from tests.follow_v3_fixtures import follow_snapshot
from tests.navigation_session_fixtures import FakeNavigationSession
from tests.test_fixed_melee_driver import MeleeBackend
from tests.test_player_runtime import _RecordingTrace, _task


TRACK = "player-target-1"
NOW = 100_000_000


def observation(
    sequence: int,
    relative=(4.0, 0.0, 0.0),
    *,
    received: int | None = None,
    episode: str = "episode-1",
    track_id: str = TRACK,
    entity_type: str = "minecraft:player",
    visible: bool = True,
    tracked: bool = True,
    tracked_track_id: str | None = None,
):
    entities = (
        [player_value(track_id, relative, entity_type=entity_type)]
        if visible else []
    )
    snapshot = follow_snapshot(
        sequence=sequence,
        received=NOW if received is None else received,
        position=(.5, 64.0, .5),
        entities=entities,
        episode=episode,
    )
    if not tracked:
        return snapshot
    return with_tracked_target(
        snapshot,
        track_id=track_id if tracked_track_id is None else tracked_track_id,
        entity_type=entity_type,
        relative=relative,
    )


def with_tracked_target(
    snapshot,
    *,
    dead: bool = False,
    entity_type="minecraft:player",
    track_id=TRACK,
    relative=(4.0, 0.0, 0.0),
):
    tracked = TrackedEntityStateV3(
        track_id, entity_type, Vec3V0(*relative), Vec3V0(0.0, 0.0, 0.0),
        0.0, 0.0, Vec3V0(.6, 1.8, .6), "standing", True, True, dead,
        0.0 if dead else 20.0, 20.0,
    )
    return replace(snapshot, tracked_entity=ObservationGroupV2.valid(
        snapshot.world_time_ticks.value,
        "client_registered_entity",
        tracked,
    ))


def report(*, terminal=False, satisfied=False):
    return SimpleNamespace(
        terminal=terminal,
        observed_goal_status=(
            ObservedGoalStatus.SATISFIED
            if satisfied else ObservedGoalStatus.NOT_SATISFIED
        ),
    )


class NavigationSpy(RuntimeNavigationDriver):
    """Public bridge surface only; there are no Session private fields to read."""

    def __init__(self, snapshot, *, satisfied=False):
        self.runtime = SimpleNamespace(observation=snapshot)
        self.session = SimpleNamespace(report=report(satisfied=satisfied))
        self.source = object()
        self._prepared_deadline_ns = None
        self.start_calls = []
        self.replace_calls = []
        self.release_calls = []
        self.replace_accepted = True
        self.release_accepted = True

    def start(self, *args, **kwargs):
        self.start_calls.append((args, kwargs))

    def replace_goal(self, *args, **kwargs):
        self.replace_calls.append((args, kwargs))
        return self.replace_accepted

    def release(self, reason):
        self.release_calls.append(reason)
        return self.release_accepted


class FollowBackend(MeleeBackend):
    def __init__(self, clock, *, distance=5.0):
        super().__init__(clock, distance=distance)
        self.track = TRACK

    def observation(self):
        snapshot = super().observation()
        perception = snapshot.perception.value
        assert perception is not None
        visible = tuple(
            replace(entity, entity_type="minecraft:player")
            for entity in perception.visible_entities
        )
        snapshot = replace(
            snapshot,
            perception=replace(
                snapshot.perception,
                value=replace(perception, visible_entities=visible),
            ),
        )
        tracked = snapshot.tracked_entity.value
        if tracked is not None:
            snapshot = replace(
                snapshot,
                tracked_entity=replace(
                    snapshot.tracked_entity,
                    value=replace(tracked, entity_type="minecraft:player"),
                ),
            )
        return snapshot


class KnownWorldFollowComponentTests(unittest.TestCase):
    def driver(self, snapshot=None, *, satisfied=False):
        navigation = NavigationSpy(
            observation(1) if snapshot is None else snapshot,
            satisfied=satisfied,
        )
        driver = KnownWorldFollowDriver(
            navigation,
            task_id="follow-task-1",
            goal_id="follow-goal-1",
            target_track_id=TRACK,
        )
        return navigation, driver

    def test_start_binds_only_the_declared_visible_player(self):
        snapshot = with_tracked_target(follow_snapshot(
            sequence=1,
            received=NOW,
            position=(.5, 64.0, .5),
            entities=[
                player_value(TRACK, (4.0, 0.0, 0.0)),
                player_value("player-other", (6.0, 0.0, 0.0)),
            ],
            episode="episode-1",
        ))
        navigation, driver = self.driver(snapshot)

        result = driver.start(NOW)

        self.assertEqual(result.status, KnownWorldFollowStatus.STARTED)
        self.assertEqual(result.state, KnownWorldFollowState.FOLLOWING)
        self.assertEqual((driver.task_id, driver.goal_id, driver.target_track_id),
                         ("follow-task-1", "follow-goal-1", TRACK))
        self.assertEqual(driver.revision, 1)
        self.assertEqual(len(navigation.start_calls), 1)
        args, options = navigation.start_calls[0]
        self.assertEqual(args[:2], ("follow-goal-1", 1))
        self.assertEqual(options["task_id"], "follow-task-1")
        self.assertIs(options["reach_policy"], GoalReachPolicy.KEEP_ACTIVE_ON_REACH)
        self.assertIs(
            options["planning_policy"],
            GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )
        goal = args[2]
        half_extent = FOLLOW_HOLD_DISTANCE_BLOCKS / 2 ** .5
        self.assertEqual(
            goal.region.as_tuple(),
            (4.5 - half_extent, 63.9, .5 - half_extent,
             4.5 + half_extent, 64.1, .5 + half_extent),
        )

    def test_revision_is_throttled_by_distance_and_time(self):
        navigation, driver = self.driver()
        driver.start(NOW)

        navigation.runtime.observation = observation(
            2, (4.2, 0.0, 0.0), received=NOW + 50_000_000,
        )
        throttled = driver.update(NOW + 50_000_000)
        duplicate = driver.update(NOW + 60_000_000)
        navigation.runtime.observation = observation(
            3,
            (4.0 + FOLLOW_REVISION_DISTANCE_BLOCKS, 0.0, 0.0),
            received=NOW + 100_000_000,
        )
        distance_revision = driver.update(NOW + 100_000_000)
        navigation.runtime.observation = observation(
            4,
            (4.0 + FOLLOW_REVISION_DISTANCE_BLOCKS + .1, 0.0, 0.0),
            received=NOW + FOLLOW_MAX_REVISION_INTERVAL_NS + 100_000_000,
        )
        interval_revision = driver.update(
            NOW + FOLLOW_MAX_REVISION_INTERVAL_NS + 100_000_000,
        )

        self.assertEqual(throttled.status, KnownWorldFollowStatus.THROTTLED)
        self.assertEqual(duplicate.status,
                         KnownWorldFollowStatus.DUPLICATE_OBSERVATION)
        self.assertEqual(distance_revision.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(interval_revision.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(driver.revision, 3)
        self.assertEqual(len(navigation.replace_calls), 2)

    def test_current_tracked_life_fact_is_required_to_start_or_revise(self):
        navigation, driver = self.driver(observation(1, tracked=False))

        missing_start = driver.start(NOW)

        self.assertEqual(
            missing_start.status,
            KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION,
        )
        self.assertFalse(navigation.start_calls)

        navigation.runtime.observation = observation(
            2, received=NOW + 50_000_000,
        )
        started = driver.start(NOW + 50_000_000)
        navigation.runtime.observation = observation(
            3, (5.0, 0.0, 0.0), received=NOW + 100_000_000,
            tracked=False,
        )
        missing_revision = driver.update(NOW + 100_000_000)

        self.assertEqual(started.status, KnownWorldFollowStatus.STARTED)
        self.assertEqual(
            missing_revision.status,
            KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION,
        )
        self.assertFalse(navigation.replace_calls)

        navigation.runtime.observation = observation(
            4, (5.0, 0.0, 0.0), received=NOW + 150_000_000,
        )
        revised = driver.update(NOW + 150_000_000)
        self.assertEqual(revised.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(len(navigation.replace_calls), 1)

    def test_satisfied_goal_leaving_hold_region_revises_immediately(self):
        navigation, driver = self.driver(
            observation(1, (2.49, 0.0, 0.0)), satisfied=True,
        )
        driver.start(NOW)
        navigation.runtime.observation = observation(
            2, (2.51, 0.0, 0.0), received=NOW + 50_000_000,
        )

        result = driver.update(NOW + 50_000_000)

        self.assertEqual(result.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(len(navigation.replace_calls), 1)

    def test_satisfied_goal_stays_revision_free_during_long_hold(self):
        navigation, driver = self.driver(
            observation(1, (2.0, 0.0, 0.0)), satisfied=True,
        )
        driver.start(NOW)

        for sequence, distance in ((2, 2.1), (3, 2.2), (4, 2.3)):
            now = NOW + (sequence - 1) * FOLLOW_MAX_REVISION_INTERVAL_NS
            navigation.runtime.observation = observation(
                sequence, (distance, 0.0, 0.0), received=now,
            )
            result = driver.update(now)
            self.assertEqual(
                result.status, KnownWorldFollowStatus.THROTTLED,
            )

        self.assertEqual(driver.revision, 1)
        self.assertFalse(navigation.replace_calls)

    def test_unsatisfied_goal_keeps_interval_revision_inside_hold(self):
        navigation, driver = self.driver(
            observation(1, (2.0, 0.0, 0.0)), satisfied=False,
        )
        driver.start(NOW)
        now = NOW + FOLLOW_MAX_REVISION_INTERVAL_NS
        navigation.runtime.observation = observation(
            2, (2.1, 0.0, 0.0), received=now,
        )

        result = driver.update(now)

        self.assertEqual(result.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(driver.revision, 2)
        self.assertEqual(len(navigation.replace_calls), 1)

    def test_missing_or_changed_track_never_submits_a_remembered_position(self):
        navigation, driver = self.driver()
        driver.start(NOW)
        navigation.runtime.observation = observation(
            2, (5.0, 0.0, 0.0), received=NOW + 50_000_000,
            track_id="player-other",
        )

        missing = driver.update(NOW + 50_000_000)

        self.assertEqual(missing.status,
                         KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION)
        self.assertEqual(missing.state,
                         KnownWorldFollowState.NEEDS_TARGET_OBSERVATION)
        self.assertFalse(navigation.replace_calls)

        navigation.runtime.observation = observation(
            3, (4.8, 0.0, 0.0), received=NOW + 100_000_000,
        )
        visible_again = driver.update(NOW + 100_000_000)
        self.assertEqual(visible_again.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(driver.revision, 2)

    def test_old_duplicate_and_rewritten_observations_are_typed(self):
        navigation, driver = self.driver(observation(2))
        driver.start(NOW)

        duplicate = driver.update(NOW + 1)
        navigation.runtime.observation = observation(1, received=NOW + 2)
        stale = driver.update(NOW + 2)
        navigation.runtime.observation = observation(
            2, (5.0, 0.0, 0.0), received=NOW,
        )
        rewritten = driver.update(NOW + 3)

        self.assertEqual(duplicate.status,
                         KnownWorldFollowStatus.DUPLICATE_OBSERVATION)
        self.assertEqual(stale.status, KnownWorldFollowStatus.STALE_OBSERVATION)
        self.assertEqual(rewritten.status,
                         KnownWorldFollowStatus.OBSERVATION_REWRITTEN)
        self.assertEqual(len(navigation.release_calls), 1)
        self.assertFalse(navigation.replace_calls)

    def test_duplicate_observation_must_still_be_fresh(self):
        navigation, driver = self.driver(observation(1))
        driver.start(NOW)

        expired = driver.update(NOW + MOVEMENT_FRESHNESS_NS + 1)

        self.assertEqual(
            expired.status,
            KnownWorldFollowStatus.OBSERVATION_UNAVAILABLE,
        )
        self.assertEqual(
            expired.state,
            KnownWorldFollowState.NEEDS_TARGET_OBSERVATION,
        )

    def test_expired_same_sequence_rewrite_still_ends_the_task(self):
        navigation, driver = self.driver(observation(1))
        driver.start(NOW)
        navigation.runtime.observation = observation(1, (5.0, 0.0, 0.0))

        rewritten = driver.update(NOW + MOVEMENT_FRESHNESS_NS + 1)

        self.assertEqual(
            rewritten.status,
            KnownWorldFollowStatus.OBSERVATION_REWRITTEN,
        )
        self.assertEqual(rewritten.state, KnownWorldFollowState.ENDED)
        self.assertEqual(len(navigation.release_calls), 1)

    def test_type_change_and_observed_death_end_the_bound_task(self):
        for invalid, expected in (
            (observation(2, entity_type="minecraft:zombie"),
             KnownWorldFollowStatus.TARGET_INVALID),
            (with_tracked_target(observation(2), dead=True),
             KnownWorldFollowStatus.TARGET_DEAD),
        ):
            with self.subTest(expected=expected):
                navigation, driver = self.driver()
                driver.start(NOW)
                navigation.runtime.observation = replace(
                    invalid, received_at_monotonic_ns=NOW + 50_000_000,
                    request_started_at_monotonic_ns=NOW + 49_000_000,
                )

                result = driver.update(NOW + 50_000_000)

                self.assertEqual(result.status, expected)
                self.assertEqual(result.state, KnownWorldFollowState.ENDED)
                self.assertEqual(len(navigation.release_calls), 1)
                self.assertFalse(navigation.replace_calls)

    def test_world_or_clock_change_ends_without_goal_revision(self):
        changed_world = observation(
            2, (5.0, 0.0, 0.0), received=NOW + 50_000_000,
            episode="episode-2",
        )
        changed_clock = replace(
            observation(2, received=NOW + 50_000_000),
            controller_clock_id="controller-other",
        )
        for changed in (changed_world, changed_clock):
            with self.subTest(changed=changed.episode_id):
                navigation, driver = self.driver()
                driver.start(NOW)
                navigation.runtime.observation = changed

                result = driver.update(NOW + 50_000_000)

                self.assertEqual(result.status,
                                 KnownWorldFollowStatus.SESSION_CHANGED)
                self.assertEqual(result.state, KnownWorldFollowState.ENDED)
                self.assertEqual(len(navigation.release_calls), 1)
                self.assertFalse(navigation.replace_calls)

    def test_terminal_navigation_and_cancel_return_typed_results(self):
        navigation, driver = self.driver()
        driver.start(NOW)
        navigation.session.report = report(terminal=True)
        ended = driver.update(NOW + 1)

        self.assertEqual(ended.status,
                         KnownWorldFollowStatus.NAVIGATION_ENDED)
        self.assertEqual(ended.state, KnownWorldFollowState.ENDED)

        navigation, driver = self.driver()
        driver.start(NOW)
        navigation.release_accepted = False
        cancelling = driver.cancel()
        later = driver.update(NOW + 1)
        self.assertEqual(cancelling.status,
                         KnownWorldFollowStatus.CANCEL_PENDING)
        self.assertEqual(cancelling.state, KnownWorldFollowState.STOPPING)
        self.assertEqual(later.status,
                         KnownWorldFollowStatus.CANCEL_PENDING)
        self.assertFalse(navigation.replace_calls)

    def test_prepared_cancel_waits_then_retries_release(self):
        navigation, driver = self.driver()
        driver.start(NOW)
        navigation._prepared_deadline_ns = NOW + 500_000_000

        pending = driver.cancel()

        self.assertEqual(pending.status,
                         KnownWorldFollowStatus.CANCEL_PENDING)
        self.assertEqual(pending.state, KnownWorldFollowState.STOPPING)
        self.assertFalse(navigation.release_calls)

        navigation._prepared_deadline_ns = None
        ended = driver.update(NOW + 1)
        self.assertEqual(ended.status, KnownWorldFollowStatus.CANCELLED)
        self.assertEqual(ended.state, KnownWorldFollowState.ENDED)
        self.assertEqual(len(navigation.release_calls), 1)

    def test_source_has_no_legacy_follower_or_session_private_access(self):
        source_path = (
            Path(__file__).resolve().parents[2]
            / "mc2p/skills/known_world_follow_driver.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("RuleFollower", source)
        self.assertNotIn("PlaygroundFollower", source)
        self.assertNotIn("navigation_controller", source)
        private_session_access = [
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "session"
        ]
        self.assertEqual(private_session_access, [])


class KnownWorldFollowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.backend = FollowBackend(self.clock)
        self.runtime = PlayerRuntimeV1(
            self.backend, _RecordingTrace(), lambda: self.clock[0],
        )
        self.assertTrue(self.runtime.reset(ResetRequestV0(
            "follow-reset", "episode-1", "test", 1, 2_000_000_000,
        )).succeeded)
        bootstrap_deadline = self.clock[0] + 500_000_000
        self.runtime.control_frame(
            _task(bootstrap_deadline), BehaviorProfileV0(), bootstrap_deadline,
            proposals=(ControlFrameProposalV1(
                observation_request=ObservationRequestV3(
                    "navigation_v1", entity_track_id=TRACK,
                ),
            ),),
        )
        self.bootstrap_actions = len(self.backend.actions)
        self.session = FakeNavigationSession()
        self.navigation = RuntimeNavigationDriver(
            self.runtime,
            self.session,
            clock_ns=lambda: self.clock[0],
            observation_request=ObservationRequestV3(
                "navigation_v1", entity_track_id=TRACK,
            ),
        )
        self.follow = KnownWorldFollowDriver(
            self.navigation,
            task_id="follow-runtime-task",
            goal_id="follow-runtime-goal",
            target_track_id=TRACK,
        )

    def tearDown(self):
        self.runtime.close()

    def test_real_runtime_bridge_starts_and_revises_same_task(self):
        started = self.follow.start(self.clock[0])
        self.backend.distance += FOLLOW_REVISION_DISTANCE_BLOCKS + .1
        self.navigation.tick(
            BehaviorProfileV0(), self.clock[0] + 500_000_000,
        )
        revised = self.follow.update(self.clock[0])

        self.assertEqual(started.status, KnownWorldFollowStatus.STARTED)
        self.assertEqual(revised.status, KnownWorldFollowStatus.REVISED)
        self.assertEqual(self.session.starts[0][:2],
                         ("follow-runtime-goal", 1))
        self.assertEqual(self.session.start_options[0]["task_id"],
                         "follow-runtime-task")
        self.assertIs(self.session.start_options[0]["reach_policy"],
                      GoalReachPolicy.KEEP_ACTIVE_ON_REACH)
        self.assertIs(
            self.session.start_options[0]["planning_policy"],
            GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )
        self.assertEqual(self.session.updates[0][:2],
                         ("follow-runtime-goal", 2))
        self.assertEqual(len(self.backend.actions), self.bootstrap_actions + 1)

    def test_airborne_cancel_keeps_navigation_owner_until_safe_tail_finishes(self):
        self.follow.start(self.clock[0])
        self.backend.own_ground = False
        self.session.handoff_ready = False
        self.navigation.tick(
            BehaviorProfileV0(), self.clock[0] + 500_000_000,
        )

        cancelling = self.follow.cancel()

        self.assertEqual(cancelling.status,
                         KnownWorldFollowStatus.CANCEL_PENDING)
        self.assertEqual(self.navigation.state, "stopping")
        self.assertIsNotNone(self.navigation.source)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)

        self.backend.own_ground = True
        self.session.handoff_ready = True
        self.navigation.tick(
            BehaviorProfileV0(), self.clock[0] + 500_000_000,
        )
        self.assertEqual(self.session.state, NavigationSessionState.CANCELLED)
        self.assertIsNone(self.navigation.source)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 0)
        ended = self.follow.update(self.clock[0])
        self.assertEqual(ended.status, KnownWorldFollowStatus.CANCELLED)
        self.assertEqual(ended.state, KnownWorldFollowState.ENDED)

    def test_prepared_cancel_discards_then_follow_retries_release(self):
        self.follow.start(self.clock[0])
        deadline = self.clock[0] + 500_000_000
        self.navigation.prepare_proposals(deadline)

        pending = self.follow.cancel()

        self.assertEqual(pending.status,
                         KnownWorldFollowStatus.CANCEL_PENDING)
        self.assertEqual(self.session.state, NavigationSessionState.EXECUTING)
        self.assertIsNotNone(self.navigation.source)

        self.navigation.discard_prepared()
        ended = self.follow.update(self.clock[0])
        self.assertEqual(ended.status, KnownWorldFollowStatus.CANCELLED)
        self.assertEqual(ended.state, KnownWorldFollowState.ENDED)
        self.assertIsNone(self.navigation.source)

    def test_prepared_terminal_adopts_then_follow_closes_pending_end(self):
        self.follow.start(self.clock[0])
        deadline = self.clock[0] + 500_000_000
        proposals = self.navigation.prepare_proposals(deadline)
        self.session.state = NavigationSessionState.FAILED
        self.session.reason = "formal_terminal"

        pending = self.follow.update(self.clock[0])

        self.assertEqual(pending.status,
                         KnownWorldFollowStatus.CANCEL_PENDING)
        self.assertEqual(pending.state, KnownWorldFollowState.STOPPING)
        result = self.runtime.control_frame(
            _task(deadline), BehaviorProfileV0(), deadline,
            proposals=proposals,
        )
        self.navigation.adopt_result(result)
        ended = self.follow.update(self.clock[0])
        self.assertEqual(ended.status,
                         KnownWorldFollowStatus.NAVIGATION_ENDED)
        self.assertEqual(ended.state, KnownWorldFollowState.ENDED)
        self.assertIsNone(self.navigation.source)


if __name__ == "__main__":
    unittest.main()
