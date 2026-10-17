from dataclasses import asdict, replace
import unittest

from mc2p.contracts.common import ContractViolation, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.skills.follow_types import FollowRequest
from mc2p.skills.local_perception import TargetMemory, project_follow_view
from tests.follow_v3_fixtures import follow_snapshot, player_value, observed_block


def view(**kwargs):
    observation = follow_snapshot(**kwargs)
    return project_follow_view(observation, observation.received_at_monotonic_ns, "controller-test")


def request(**kwargs):
    values = dict(skill_id="follow-1", episode_id="episode-1", target_track_id="player-1",
                  started_at_ns=100_000_000, deadline_ns=120_100_000_000,
                  controller_clock_id="controller-test")
    values.update(kwargs)
    return FollowRequest(**values)


class FollowPerceptionTests(unittest.TestCase):
    def test_projection_is_restricted_and_converts_relative_coordinates(self):
        projected = view()
        self.assertTrue(projected.available)
        self.assertEqual(projected.entities[0].position, Vec3V0(1, 64, 1))
        self.assertEqual(set(asdict(projected.entities[0])), {"track_id", "entity_type", "position", "size"})
        for forbidden in ("inventory", "equipment", "display_name", "observation", "world_time_ticks"):
            self.assertNotIn(forbidden, asdict(projected))

    def test_negative_block_coordinates_are_exact_integers_without_origin_roundoff(self):
        projected = view(blocks=[observed_block((-2,63,-3))])
        self.assertEqual(projected.observed_blocks[0].position,(-2,63,-3))
        with self.assertRaises(ContractViolation): observed_block((-2.01,63,-3))

    def test_no_observed_blocks_does_not_produce_air_evidence(self):
        projected = view()
        self.assertEqual(projected.observed_blocks, ())

    def test_unavailable_groups_privilege_and_clocks_fail_closed(self):
        original = follow_snapshot()
        for name in ("self_state", "gui", "perception"):
            group = getattr(original, name)
            changes = {name: ObservationGroupV2.missing(100, group.source_kind, "unavailable")}
            if name == "self_state":
                changes.update({key:FieldValueV0.missing("unavailable") for key in
                    ("position","yaw_degrees","pitch_degrees","is_on_ground","is_dead","health_points","food_points")})
            changed = replace(original, **changes)
            with self.subTest(name=name):
                self.assertEqual(project_follow_view(changed, 100_000_000, "controller-test").reason, name + "_unavailable")
        for changed, clock, reason in (
            (replace(original, privileged_fields_present=("server.position",)), "controller-test", "privileged_observation"),
            (original, "other-clock", "controller_clock_mismatch"),
        ):
            self.assertEqual(project_follow_view(changed, 100_000_000, clock).reason, reason)
        with self.assertRaises(ContractViolation):
            project_follow_view({}, 100_000_000, "controller-test")

    def test_freshness_uses_controller_interval_not_world_time(self):
        snapshot = follow_snapshot()
        self.assertTrue(project_follow_view(snapshot, 600_000_000, "controller-test").available)
        self.assertEqual(project_follow_view(snapshot, 600_000_001, "controller-test").reason, "stale_observation")
        self.assertEqual(project_follow_view(snapshot, 99_999_999, "controller-test").reason, "future_observation")
        slow = replace(snapshot, request_started_at_monotonic_ns=0, received_at_monotonic_ns=600_000_000)
        self.assertEqual(project_follow_view(slow, 600_000_000, "controller-test").reason, "slow_observation_request")

    def test_binding_requires_current_visible_player_and_same_episode(self):
        for initial, req in ((view(entities=[]), request()),
                             (view(entities=[player_value(entity_type="minecraft:cow")]), request()),
                             (view(), request(episode_id="other-episode"))):
            with self.subTest(initial=initial.entities):
                with self.assertRaises(ContractViolation):
                    TargetMemory().bind(req, initial)

    def test_target_memory_does_not_extrapolate_hidden_velocity_or_refresh_on_absence(self):
        memory = TargetMemory()
        memory.bind(request(), view())
        state = memory.observe(view(sequence=1, received=1_100_000_000, entities=[], truncated=True), 1_100_000_000)
        self.assertEqual((state.status, state.position), ("remembered", Vec3V0(1, 64, 1)))
        state = memory.observe(view(sequence=2, received=2_100_000_000, entities=[]), 2_100_000_000)
        self.assertEqual((state.status, state.position), ("lost", None))
        # Loss is terminal for this binding, even if another object later reuses a label.
        self.assertEqual(memory.observe(view(sequence=3, received=2_200_000_000), 2_200_000_000).status, "lost")

    def test_same_sequence_cannot_renew_ttl(self):
        initial = view()
        memory = TargetMemory()
        memory.bind(request(), initial)
        self.assertEqual(memory.observe(initial, 1_000_000_000).status, "remembered")
        self.assertEqual(memory.observe(initial, 2_100_000_000).status, "lost")

    def test_same_object_reappearance_refreshes_memory_but_other_track_does_not(self):
        memory = TargetMemory()
        memory.bind(request(), view())
        memory.observe(view(sequence=1, received=1_000_000_000, entities=[]), 1_000_000_000)
        returned = memory.observe(view(sequence=2, received=1_900_000_000,
                                       entities=[player_value(relative=(1, 0, 2))]), 1_900_000_000)
        self.assertEqual((returned.status, returned.position), ("visible", Vec3V0(0, 64, 0)))
        other = memory.observe(view(sequence=3, received=2_200_000_000,
                                    entities=[player_value("player-2")]), 2_200_000_000)
        self.assertEqual((other.status, other.position), ("remembered", Vec3V0(0, 64, 0)))

    def test_rewritten_regressed_sequence_and_clock_change_invalidate_binding(self):
        for changed in (view(entities=[]),
                        replace(view(sequence=1), client_clock_id="new-jvm"),
                        replace(view(sequence=1), controller_clock_id="new-controller"),
                        view(sequence=1, episode="new-episode")):
            memory = TargetMemory()
            memory.bind(request(), view())
            self.assertEqual(memory.observe(changed, 100_000_000).status, "invalidated")
            self.assertIsNone(memory.observe(view(sequence=2), 100_000_000).position)
        memory = TargetMemory()
        memory.bind(request(), view(sequence=1))
        self.assertEqual(memory.observe(view(), 100_000_000).reason, "sequence_regression")

    def test_request_budget_and_types_are_explicit(self):
        for values in ({"deadline_ns": 120_100_000_001}, {"deadline_ns": 100_000_000},
                       {"started_at_ns": True}, {"target_track_id": ""}):
            with self.subTest(values=values), self.assertRaises(ContractViolation):
                request(**values)

    def test_distance_thresholds_are_explicit_ordered_bounded_configuration(self):
        configured = request(retreat_distance_blocks=1.2, hold_distance_blocks=2, approach_distance_blocks=3)
        self.assertEqual(configured.hold_distance_blocks, 2)
        for values in ({"retreat_distance_blocks": True}, {"hold_distance_blocks": 4},
                       {"approach_distance_blocks": 7}, {"retreat_distance_blocks": .5}):
            with self.subTest(values=values), self.assertRaises(ContractViolation): request(**values)


if __name__ == "__main__":
    unittest.main()
