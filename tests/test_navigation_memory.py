from dataclasses import FrozenInstanceError, fields, replace
from itertools import product
import unittest
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.skills.navigation_memory import MemoryLimits, NavigationMemory, TerrainHistory
from mc2p.skills.navigation_state import NavigationState
from tests.follow_v3_fixtures import follow_snapshot, observed_block, player_value

NOW = 100_000_000


class NavigationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = NavigationMemory("world-1")

    def observe(self, obs, now=None, memory=None, scope="world-1"):
        return (memory or self.memory).observe(
            obs,
            now_ns=obs.received_at_monotonic_ns if now is None else now,
            controller_clock_id="controller-test",
            scope_id=scope,
        )

    def view(self, now=NOW, memory=None):
        return (memory or self.memory).snapshot(
            now_ns=now, controller_clock_id="controller-test"
        )

    def test_contact_blocks_are_known_without_view_and_do_not_refresh_after_leaving(self):
        positions = ((0, 63, 0), (1, 63, 0), (0, 63, 1), (1, 63, 1))
        names = (
            "minecraft:stone",
            "minecraft:white_wool",
            "minecraft:glass",
            "minecraft:dirt",
        )
        blocks = tuple(
            observed_block(position, name, sources=("body_contact",))
            for position, name in zip(positions, names)
        )
        for yaw, pitch in ((0., 0.), (180., -80.)):
            memory = NavigationMemory("world-1")
            first = self.observe(
                follow_snapshot(
                    position=(1., 64., 1.), yaw=yaw, pitch=pitch,
                    entities=[], blocks=blocks,
                ),
                memory=memory,
            )
            self.assertEqual(
                {record.block.position: record.block.block_id for record in first.terrain},
                dict(zip(positions, names)),
            )
            self.assertTrue(
                all(record.block.sources == ("body_contact",) for record in first.terrain)
            )

            later = self.observe(
                follow_snapshot(
                    sequence=1, received=NOW + 600_000_000,
                    position=(3., 64., 1.), entities=[], blocks=(),
                ),
                memory=memory,
            )
            self.assertEqual(
                {record.block.position for record in later.terrain}, set(positions)
            )
            self.assertTrue(
                all(record.last_seen == first.latest.stamp for record in later.terrain)
            )

    def test_repeated_same_snapshot_does_not_refresh_history(self):
        obs = follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        first = self.observe(obs)
        later = self.observe(obs, NOW + 2_000_000_000)

        self.assertEqual(later.terrain, first.terrain)
        self.assertEqual(later.entities, first.entities)
        self.assertEqual(later.recent_entities, ())
        self.assertEqual(later.entities[0].last_seen.client_sample.completed_at_monotonic_ns, 9010)
        self.assertEqual(later.terrain[0].last_seen.received_at_ns, NOW)

        expired = self.view(NOW + 60_000_000_000)
        self.assertEqual(expired.terrain, ())
        self.assertEqual(expired.entities, ())

    def test_terrain_history_contains_only_whole_block_and_last_seen(self):
        view = self.observe(
            follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        )

        self.assertEqual(
            tuple(field.name for field in fields(TerrainHistory)), ("block", "last_seen")
        )
        record = view.terrain[0]
        self.assertFalse(hasattr(record, "evidence"))
        self.assertFalse(hasattr(record, "top_evidence"))
        self.assertFalse(hasattr(record, "top_stamp"))
        self.assertFalse(hasattr(record, "top_revocation"))
        self.assertFalse(hasattr(record, "ray_state"))

    def test_absolute_blocks_and_entities_keep_their_original_observer_frame(self):
        self.observe(
            follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        )
        second = follow_snapshot(
            sequence=1, received=NOW + 10, position=(1, 64, -2), entities=[]
        )
        view = self.observe(second)

        self.assertEqual(view.terrain[0].block.position, (0, 63, 0))
        self.assertEqual(view.entities[0].entity.position.x, 1)
        self.assertEqual(view.entities[0].last_seen.sequence_id, 0)
        self.assertEqual(view.entities[0].observer_pose.position.x, -1)

    def test_missing_perception_retains_history_without_negative_claim(self):
        self.observe(
            follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        )
        second = follow_snapshot(sequence=1, received=NOW + 10)
        missing = ObservationGroupV2.missing(
            second.world_time_ticks.value,
            "client_perception_filtered",
            "not_available",
        )
        view = self.observe(replace(second, perception=missing))

        self.assertEqual(len(view.terrain), 1)
        self.assertEqual(len(view.entities), 1)
        self.assertFalse(view.latest.available)
        self.assertIsNone(view.latest.coverage)
        self.assertEqual(view.recent_entities, ())

    def test_truncation_and_new_track_do_not_rebind_or_refresh_old_entity(self):
        self.observe(follow_snapshot())
        obs = follow_snapshot(
            sequence=1, received=NOW + 10,
            entities=[player_value("player-2")], truncated=True,
        )
        view = self.observe(obs)

        self.assertEqual(
            [record.entity.track_id for record in view.entities],
            ["player-1", "player-2"],
        )
        self.assertEqual([record.last_seen.sequence_id for record in view.entities], [0, 1])
        self.assertEqual([entity.track_id for entity in view.recent_entities], ["player-2"])
        self.assertTrue(view.latest.coverage.entities_truncated)

    def test_new_legal_block_sample_replaces_whole_block_and_refreshes_once(self):
        old = observed_block((0, 63, 0))
        self.observe(follow_snapshot(blocks=(old,)))
        changed = observed_block(
            (0, 63, 0), "minecraft:dirt", sources=("body_contact", "first_hit_ray")
        )
        view = self.observe(
            follow_snapshot(sequence=1, received=NOW + 10, blocks=(changed,))
        )

        self.assertEqual(view.terrain[0].block, changed)
        self.assertEqual(view.terrain[0].last_seen.sequence_id, 1)
        self.assertEqual(
            view.terrain[0].block.sources, ("body_contact", "first_hit_ray")
        )

    def test_unobserved_wall_is_unknown_until_new_legal_block_sample(self):
        floor = observed_block((0, 63, 0))
        first = self.observe(follow_snapshot(blocks=(floor,)))
        absent = self.observe(
            follow_snapshot(sequence=1, received=NOW + 10, entities=[], blocks=())
        )

        self.assertEqual(absent.terrain, first.terrain)
        self.assertNotIn((0, 64, 0), {record.block.position for record in absent.terrain})

        wall = observed_block((0, 64, 0))
        seen = self.observe(
            follow_snapshot(sequence=2, received=NOW + 20, entities=[], blocks=(wall,))
        )
        self.assertEqual(
            {record.block.position for record in seen.terrain}, {(0, 63, 0), (0, 64, 0)}
        )
        floor_record = next(
            record for record in seen.terrain if record.block.position == (0, 63, 0)
        )
        self.assertEqual(floor_record.last_seen.sequence_id, 0)

    def test_single_contact_adds_only_that_block_not_a_hidden_wall(self):
        contact = observed_block(
            (0, 64, -2), sources=("body_contact",)
        )
        view = self.observe(
            follow_snapshot(
                blocks=(contact,), self_changes={"horizontal_collision": True}
            )
        )

        self.assertEqual(len(view.terrain), 1)
        self.assertEqual(view.terrain[0].block.position, (0, 64, -2))
        self.assertTrue(view.latest.pose.horizontal_collision)

    def test_privileged_input_cannot_seed_history_and_requires_reset(self):
        self.observe(follow_snapshot())
        privileged = replace(
            follow_snapshot(sequence=1, received=NOW + 10),
            privileged_fields_present=("hidden_entities",),
        )
        with self.assertRaises(ContractViolation):
            self.observe(privileged)

        view = self.view(NOW + 10)
        self.assertEqual(view.entities, ())
        self.assertIsNone(view.latest)
        self.assertIsNotNone(view.invalid_reason)

    def test_sequence_rewrite_and_regression_clear_and_latch_until_reset(self):
        changed_values = (
            follow_snapshot(sequence=1, received=NOW + 20),
            follow_snapshot(sequence=0),
        )
        for changed in changed_values:
            with self.subTest(sequence=changed.sequence_id):
                memory = NavigationMemory("world-1")
                self.observe(
                    follow_snapshot(sequence=1, received=NOW + 10), memory=memory
                )
                with self.assertRaises(ContractViolation):
                    self.observe(changed, now=NOW + 20, memory=memory)
                self.assertEqual(self.view(NOW + 20, memory).entities, ())
                with self.assertRaises(ContractViolation):
                    self.observe(
                        follow_snapshot(sequence=2, received=NOW + 30), memory=memory
                    )

    def test_time_regression_and_wrong_controller_clock_fail_closed(self):
        self.observe(follow_snapshot())
        with self.assertRaises(ContractViolation):
            self.view(NOW - 1)
        self.assertEqual(self.view().entities, ())

        memory = NavigationMemory("world-1")
        self.observe(follow_snapshot(), memory=memory)
        with self.assertRaises(ContractViolation):
            memory.snapshot(now_ns=NOW, controller_clock_id="wrong")
        self.assertEqual(self.view(memory=memory).entities, ())

    def test_jvm_sample_regression_is_checked_within_same_clock(self):
        self.observe(follow_snapshot())
        second = follow_snapshot(sequence=1, received=NOW + 10)
        second = replace(
            second,
            client_sample=replace(
                second.client_sample,
                started_at_monotonic_ns=1,
                completed_at_monotonic_ns=2,
            ),
        )
        with self.assertRaises(ContractViolation):
            self.observe(second)
        self.assertEqual(self.view(NOW + 10).terrain, ())

    def test_episode_or_jvm_clock_change_requires_reset_and_old_scope_is_rejected(self):
        for field in ("episode", "clock"):
            memory = NavigationMemory("world-1")
            self.observe(follow_snapshot(), memory=memory)
            changed = follow_snapshot(
                sequence=1,
                received=NOW + 10,
                episode="episode-2" if field == "episode" else "episode-1",
            )
            if field == "clock":
                changed = replace(
                    changed,
                    client_sample=replace(changed.client_sample, clock_id="new-jvm"),
                )
            with self.assertRaises(ContractViolation):
                self.observe(changed, memory=memory)
            self.assertEqual(self.view(NOW + 10, memory).entities, ())

            memory.reset("world-2")
            expected = self.observe(changed, memory=memory, scope="world-2")
            with self.assertRaises(ContractViolation):
                self.observe(follow_snapshot(), memory=memory)
            self.assertEqual(self.view(NOW + 10, memory), expected)

    def test_unexplained_position_jump_invalidates_instead_of_fusing_maps(self):
        self.observe(follow_snapshot())
        with self.assertRaises(ContractViolation):
            self.observe(
                follow_snapshot(
                    sequence=1, received=NOW + 10, position=(20, 64, -2)
                )
            )
        self.assertEqual(self.view(NOW + 10).entities, ())

    def test_delayed_data_is_history_not_recent_and_retention_uses_original_request(self):
        limits = MemoryLimits(
            terrain_retention_ns=1_000_000_000,
            entity_retention_ns=1_000_000_000,
        )
        memory = NavigationMemory("world-1", limits)
        view = self.observe(
            follow_snapshot(blocks=(observed_block((0, 63, 0)),)),
            now=NOW + 600_000_000,
            memory=memory,
        )

        self.assertEqual(len(view.terrain), 1)
        self.assertEqual(len(view.entities), 1)
        self.assertEqual(view.recent_entities, ())
        self.assertEqual(self.view(1_099_000_000, memory).terrain, ())
        self.assertEqual(self.view(1_099_000_000, memory).entities, ())

    def test_capacity_eviction_is_deterministic_and_long_sequences_stay_bounded(self):
        limits = MemoryLimits(max_terrain=2, max_entities=2)
        memories = [NavigationMemory("world-1", limits), NavigationMemory("world-1", limits)]
        for i in range(160):
            obs = follow_snapshot(
                sequence=i,
                received=NOW + i * 1000,
                blocks=(observed_block((i % 10, 63, i // 10)),),
                entities=[player_value(f"player-{i:03d}")],
            )
            views = [self.observe(obs, memory=memory) for memory in memories]
            self.assertEqual(views[0], views[1])
            self.assertLessEqual(len(views[0].terrain), 2)
            self.assertLessEqual(len(views[0].entities), 2)

        self.assertEqual(
            [record.entity.track_id for record in views[0].entities],
            ["player-158", "player-159"],
        )
        self.assertEqual(
            [record.block.position for record in views[0].terrain],
            [(8, 63, 15), (9, 63, 15)],
        )

    def test_default_sphere_retains_all_513_observed_blocks(self):
        positions = tuple(sorted(product(range(-8, 9), range(56, 73), range(-10, 7))))[:513]
        blocks = tuple(observed_block(position) for position in positions)
        memories = [NavigationMemory("world-1"), NavigationMemory("world-1")]
        views = [
            self.observe(follow_snapshot(blocks=blocks, entities=[]), memory=memory)
            for memory in memories
        ]

        self.assertEqual(views[0], views[1])
        self.assertEqual(len(views[0].terrain), 513)

    def test_radius_eviction_and_immutable_snapshots_do_not_change_old_results(self):
        memory = NavigationMemory("world-1", MemoryLimits(radius_blocks=4))
        old = self.observe(
            follow_snapshot(blocks=(observed_block((-1, 63, -2)),)), memory=memory
        )
        new = self.observe(
            follow_snapshot(
                sequence=1, received=NOW + 10,
                position=(5, 64, -2), entities=[], blocks=(),
            ),
            memory=memory,
        )

        self.assertEqual(new.terrain, ())
        self.assertEqual(len(old.terrain), 1)
        with self.assertRaises(FrozenInstanceError):
            old.terrain[0].block = observed_block((9, 9, 9))

    def test_expiry_drops_latest_payload_but_keeps_immutable_replay_guard(self):
        obs = follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        self.observe(obs)
        view = self.view(16_000_000_000)
        self.assertIsNone(view.latest)
        self.assertEqual(view.entities, ())
        self.assertEqual(len(view.terrain), 1)

        duplicate = self.observe(obs, 16_000_000_001)
        self.assertIsNone(duplicate.latest)
        self.assertEqual(duplicate.terrain[0].last_seen.sequence_id, 0)

        rewritten = follow_snapshot(
            received=NOW + 10, blocks=(observed_block((0, 63, 0)),)
        )
        with self.assertRaises(ContractViolation):
            self.observe(rewritten, 16_000_000_002)

    def test_same_immutable_observation_is_not_projected_twice(self):
        obs = follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        from mc2p.skills.navigation_memory import project_navigation_evidence
        with patch(
            'mc2p.skills.navigation_memory.project_navigation_evidence',
            wraps=project_navigation_evidence,
        ) as project:
            first = self.observe(obs)
            duplicate = self.observe(obs, NOW + 1)

        self.assertEqual(project.call_count, 1)
        self.assertEqual(duplicate.terrain, first.terrain)

    def test_state_does_not_reimport_beliefs_for_same_immutable_observation(self):
        state = NavigationState("world-1")
        obs = follow_snapshot(blocks=(observed_block((0, 63, 0)),))
        with patch.object(state.belief, "observe", wraps=state.belief.observe) as observe:
            first = state.observe(obs, NOW)
            duplicate = state.observe(obs, NOW + 1)

        self.assertEqual(observe.call_count, 1)
        self.assertEqual(duplicate.terrain, first.terrain)
        self.assertIs(state.snapshot, duplicate)

    def test_invalid_configuration_cannot_disable_bounds(self):
        changes_values = (
            {"max_terrain": 0},
            {"max_entities": True},
            {"radius_blocks": float("inf")},
            {"terrain_retention_ns": -1},
            {"freshness_ns": 0},
        )
        for changes in changes_values:
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                MemoryLimits(**changes)


if __name__ == "__main__":
    unittest.main()
