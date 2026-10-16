"""D108 A1: observation invalidates now; snapshot sections materialize by borrow range."""
from __future__ import annotations

import threading
import time
import unittest
import math
import statistics
import sys
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.prepared_motion import SnapshotLeaseStatus
from mc2p.motion_nav.world_model import (
    BlockGeometry,
    ImmutableWorldSection,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from mc2p.motion_nav.world_snapshot import DependencyStatus, seal_snapshot_version


class SnapshotMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = WorldKnowledge(WorldSessionId("d108-snapshot"))
        self.stone = BlockGeometry.full_cube("minecraft:stone")
        self.dirt = BlockGeometry.full_cube("minecraft:dirt")
        self.sequence = 0

    def stamp(self) -> ObservationStamp:
        self.sequence += 1
        return ObservationStamp(
            self.world.session,
            self.sequence,
            self.sequence,
            "d108-clock",
            self.sequence * 50_000_000,
        )

    @staticmethod
    def bounds(section_x: int) -> PhysicsWorldBounds:
        minimum = section_x * 16
        return PhysicsWorldBounds(minimum, minimum + 15, 0, 0, 0, 0)

    def observe(self, positions, block=None) -> None:
        geometry = self.stone if block is None else block
        self.world.observe_blocks(self.stamp(), {position: geometry for position in positions})

    def borrow(self, section_x: int):
        value = self.world.borrow_snapshot_version(
            self.bounds(section_x), ruleset=JAVA_1_21_RULESET,
        )
        self.assertIsNotNone(value)
        return value

    def test_semantic_observation_invalidates_without_materializing(self) -> None:
        position = (0, 0, 0)
        self.observe((position,))
        old = self.borrow(0)
        old_section = old.section_references[0]
        old_geometry = self.world.view().geometry_revision
        old_versions = old_section.dependency_versions
        old_live_view = self.world.view()
        sealed = seal_snapshot_version(old, JAVA_1_21_RULESET)
        self.assertTrue(self.world.publish_sealed_snapshot(old, sealed))
        self.assertIs(
            self.world.acquire_published_snapshot(
                self.bounds(0), ruleset=JAVA_1_21_RULESET,
            ).status,
            SnapshotLeaseStatus.READY,
        )

        with patch.object(
            ImmutableWorldSection,
            "_from_validated_observation",
            side_effect=AssertionError("observation materialized an immutable section"),
        ):
            self.observe((position,), self.dirt)

        self.assertEqual(self.world.view().cell(position).block, self.dirt)
        self.assertGreater(self.world.view().geometry_revision, old_geometry)
        self.assertNotIn((0, 0, 0), self.world._snapshot_sections)
        self.assertIn((0, 0, 0), self.world._snapshot_dirty_sections)
        self.assertNotIn((0, 0, 0), self.world._snapshot_dirty_positions)
        self.assertEqual(dict(old_section.facts)[position].block, self.stone)
        self.assertEqual(old_section.dependency_versions, old_versions)
        self.assertIs(
            self.world.dependency_changes(old_section.dependency_versions).status,
            DependencyStatus.CHANGED,
        )
        self.assertIs(
            self.world.acquire_published_snapshot(
                self.bounds(0), ruleset=JAVA_1_21_RULESET,
            ).status,
            SnapshotLeaseStatus.NOT_READY,
        )
        with self.assertRaisesRegex(ContractViolation, "expired"):
            old_live_view.cell(position)

    def test_borrow_materializes_only_dirty_sections_intersecting_bounds(self) -> None:
        positions = tuple((section * 16, 0, 0) for section in range(4))
        self.observe(positions)
        for section in range(4):
            self.borrow(section)
        originals = dict(self.world._snapshot_sections)

        self.observe(positions, self.dirt)
        self.assertEqual(self.world._snapshot_sections, {})
        self.assertEqual(self.world._snapshot_dirty_sections, set(range_sections(4)))

        original_builder = ImmutableWorldSection._from_validated_observation
        built = []

        def record(section, facts, versions):
            built.append(section)
            return original_builder(section, facts, versions)

        with patch.object(
            ImmutableWorldSection, "_from_validated_observation", side_effect=record,
        ):
            borrowed = self.borrow(0)

        self.assertEqual(built, [(0, 0, 0)])
        self.assertEqual(tuple(section.position for section in borrowed.section_references),
                         ((0, 0, 0),))
        self.assertEqual(self.world._snapshot_dirty_sections, set(range_sections(1, 4)))
        for section, old in originals.items():
            self.assertEqual(dict(old.facts)[(section[0] * 16, 0, 0)].block, self.stone)

    def test_evidence_refresh_reuses_unchanged_immutable_pairs_and_versions(self) -> None:
        positions = ((0, 0, 0), (1, 0, 0))
        self.observe(positions)
        before = self.borrow(0).section_references[0]
        self.observe((positions[0],))
        after = self.borrow(0).section_references[0]
        before_pairs = dict(before.facts)
        self.assertEqual(before_pairs[positions[1]], dict(after.facts)[positions[1]])
        self.assertIs(after.facts[1], before.facts[1])
        self.assertIsNot(after.facts[0], before.facts[0])
        self.assertIs(after.dependency_versions, before.dependency_versions)

    def test_unrelated_dirty_section_cannot_delay_or_block_local_borrow(self) -> None:
        positions = ((0, 0, 0), (16, 0, 0))
        self.observe(positions)
        self.borrow(0)
        self.borrow(1)
        self.observe(positions, self.dirt)

        original_builder = ImmutableWorldSection._from_validated_observation
        unrelated_started = threading.Event()

        def reject_unrelated(section, facts, versions):
            if section == (1, 0, 0):
                unrelated_started.set()
                time.sleep(.25)
                raise AssertionError("borrow materialized an unrelated dirty section")
            return original_builder(section, facts, versions)

        with patch.object(
            ImmutableWorldSection, "_from_validated_observation",
            side_effect=reject_unrelated,
        ):
            self.borrow(0)

        self.assertFalse(unrelated_started.is_set())
        self.assertIn((1, 0, 0), self.world._snapshot_dirty_sections)

    def test_one_section_borrow_has_constant_observable_work(self) -> None:
        diagnostics = []
        for section_count in (4, 16, 32):
            world = WorldKnowledge(WorldSessionId(f"d109-work-{section_count}"))
            positions = tuple((section * 16, 0, 0)
                              for section in range(section_count))
            stamp = ObservationStamp(
                world.session, 1, 1, "d109-work-clock", 1,
            )
            world.observe_blocks(stamp, {
                position: self.stone for position in positions
            })
            old_borrows = tuple(
                world.borrow_snapshot_version(
                    self.bounds(section), ruleset=JAVA_1_21_RULESET,
                )
                for section in range(section_count)
            )
            world.observe_blocks(ObservationStamp(
                world.session, 2, 2, "d109-work-clock", 2,
            ), {position: self.dirt for position in positions})

            borrowed = world.borrow_snapshot_version(
                self.bounds(0), ruleset=JAVA_1_21_RULESET,
            )
            self.assertIsNotNone(borrowed)
            report = world.snapshot_materialization_diagnostics
            diagnostics.append(report)

            self.assertEqual(report.requested_sections, 1)
            self.assertEqual(report.dirty_sections_before, section_count)
            self.assertEqual(report.selected_sections, 1)
            self.assertEqual(report.materialized_sections, 1)
            self.assertEqual(report.committed_sections, 1)
            self.assertEqual(report.dirty_sections_after, section_count - 1)
            self.assertEqual(
                dict(borrowed.section_references[0].facts)[positions[0]].block,
                self.dirt,
            )
            for section, old in enumerate(old_borrows):
                self.assertEqual(
                    dict(old.section_references[0].facts)[positions[section]].block,
                    self.stone,
                )
            self.assertLessEqual(
                len(world._snapshot_sections), world.section_count,
            )
            self.assertLessEqual(
                len(world._snapshot_dirty_sections), world.section_count,
            )

        self.assertEqual(
            {(row.requested_sections, row.selected_sections,
              row.materialized_sections, row.committed_sections)
             for row in diagnostics},
            {(1, 1, 1, 1)},
        )

    def test_gate_detects_missing_semantic_cache_retirement_mutation(self) -> None:
        position = (0, 0, 0)
        self.observe((position,))
        self.borrow(0)
        with patch.object(
            WorldKnowledge, "_retire_changed_snapshot_sections", return_value=None,
        ):
            self.observe((position,), self.dirt)
        with self.assertRaises(AssertionError):
            self.assertNotIn((0, 0, 0), self.world._snapshot_sections)

    def test_gate_detects_global_materialization_mutation(self) -> None:
        positions = ((0, 0, 0), (16, 0, 0))
        self.observe(positions)
        self.borrow(0)
        self.borrow(1)
        self.observe(positions, self.dirt)
        original_commit = self.world._commit_snapshot_sections
        original_builder = ImmutableWorldSection._from_validated_observation
        built = []

        def global_commit(_sections):
            return original_commit(set(self.world._snapshot_dirty_sections))

        def record(section, facts, versions):
            built.append(section)
            return original_builder(section, facts, versions)

        with patch.object(
            self.world, "_commit_snapshot_sections", side_effect=global_commit,
        ), patch.object(
            ImmutableWorldSection, "_from_validated_observation", side_effect=record,
        ):
            self.borrow(0)
        with self.assertRaises(AssertionError):
            self.assertEqual(built, [(0, 0, 0)])
        with self.assertRaises(AssertionError):
            self.assertEqual(
                self.world.snapshot_materialization_diagnostics.selected_sections,
                1,
            )

    def test_materialization_rejects_an_implicit_global_scope(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "explicit section set"):
            self.world._commit_snapshot_sections(None)

    def test_empty_and_evicted_sections_leave_no_cache_or_dirty_marker(self) -> None:
        position = (0, 0, 0)
        self.observe((position,))
        self.borrow(0)
        self.world.invalidate(self.stamp(), (position,))
        self.assertNotIn((0, 0, 0), self.world._snapshot_sections)
        self.assertNotIn((0, 0, 0), self.world._snapshot_dirty_sections)

        world = WorldKnowledge(
            WorldSessionId("d108-eviction"), max_known_cells=1,
            protection_radius_blocks=0,
        )
        first = ObservationStamp(world.session, 1, 1, "clock", 1)
        second = ObservationStamp(world.session, 2, 2, "clock", 2)
        world.observe_blocks(first, {(0, 0, 0): self.stone})
        self.assertIsNotNone(world.borrow_snapshot_version(
            self.bounds(0), ruleset=JAVA_1_21_RULESET,
        ))
        world.observe_blocks(second, {(32, 0, 0): self.stone})
        self.assertNotIn((0, 0, 0), world._snapshot_sections)
        self.assertNotIn((0, 0, 0), world._snapshot_dirty_sections)
        self.assertEqual(world._snapshot_dirty_sections, {(2, 0, 0)})
        self.assertLessEqual(len(world._snapshot_sections), world.section_count)
        self.assertLessEqual(len(world._snapshot_dirty_sections), world.section_count)

    def test_long_running_observe_borrow_invalidate_and_evict_stays_bounded(self) -> None:
        world = WorldKnowledge(
            WorldSessionId("d108-long-running"), max_known_cells=16,
            protection_radius_blocks=0,
        )
        sequence = 0

        def next_stamp():
            nonlocal sequence
            sequence += 1
            return ObservationStamp(
                world.session, sequence, sequence, "long-running-clock", sequence,
            )

        for index in range(512):
            section_x = index % 48
            position = (section_x * 16, 0, 0)
            world.observe_blocks(next_stamp(), {position: self.stone})
            borrowed = world.borrow_snapshot_version(
                self.bounds(section_x), ruleset=JAVA_1_21_RULESET,
            )
            self.assertIsNotNone(borrowed)
            if index % 3 == 0:
                world.observe_blocks(next_stamp(), {position: self.stone})
            if index % 5 == 0:
                world.invalidate(next_stamp(), (position,))
            live_sections = set(world._sections)
            self.assertTrue(set(world._snapshot_sections).issubset(live_sections))
            self.assertTrue(world._snapshot_dirty_sections.issubset(live_sections))
            self.assertTrue(set(world._snapshot_dirty_positions).issubset(live_sections))
            self.assertLessEqual(len(world._snapshot_sections), world.section_count)
            self.assertLessEqual(len(world._snapshot_dirty_sections), world.section_count)
            self.assertLessEqual(
                sum(len(values) for values in world._snapshot_dirty_positions.values()),
                world.known_cell_count,
            )
            self.assertLessEqual(world.known_cell_count, 16)
            self.assertLessEqual(world.tombstone_count, 16)

    def test_concurrent_observation_sees_coherent_old_then_new_versions(self) -> None:
        position = (0, 0, 0)
        self.observe((position,))
        self.borrow(0)
        self.observe((position,), self.dirt)
        original_builder = ImmutableWorldSection._from_validated_observation
        builder_entered = threading.Event()
        release_builder = threading.Event()
        borrowed = []
        errors = []

        def pause(section, facts, versions):
            builder_entered.set()
            if not release_builder.wait(2):
                raise AssertionError("snapshot builder release timed out")
            return original_builder(section, facts, versions)

        def borrow_in_background():
            try:
                borrowed.append(self.borrow(0))
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)

        with patch.object(
            ImmutableWorldSection, "_from_validated_observation", side_effect=pause,
        ):
            thread = threading.Thread(target=borrow_in_background)
            thread.start()
            self.assertTrue(builder_entered.wait(2))
            observation_done = threading.Event()

            def observe_after_borrow():
                try:
                    self.observe((position,), self.stone)
                except BaseException as error:  # pragma: no cover - reported below
                    errors.append(error)
                finally:
                    observation_done.set()

            observer = threading.Thread(target=observe_after_borrow)
            observer.start()
            self.assertFalse(observation_done.wait(.02))
            release_builder.set()
            thread.join(2)
            observer.join(2)

        self.assertFalse(thread.is_alive())
        self.assertFalse(observer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(dict(borrowed[0].section_references[0].facts)[position].block,
                         self.dirt)
        self.assertEqual(self.world.view().cell(position).block, self.stone)
        self.assertNotIn((0, 0, 0), self.world._snapshot_sections)
        self.assertIn((0, 0, 0), self.world._snapshot_dirty_sections)

    @unittest.skipUnless(sys.platform == "win32", "formal performance gate is Windows-only")
    def test_windows_benchmark_preserves_raw_distribution_and_deadline(self) -> None:
        from scripts.benchmark_d108_snapshot_materialization import (
            _passes_deadline, run_probe,
        )
        result = run_probe(samples=20)
        self.assertEqual(result["samples_per_case"], 20)
        self.assertTrue(result["passed"])
        self.assertEqual(
            [row["sections"] for row in result["semantic_changes"]],
            [1, 2, 4, 8],
        )
        for row in result["semantic_changes"]:
            self.assertEqual(len(row["raw_samples_ms"]), 20)
            self.assert_raw_summary(row)
        self.assertEqual(
            [row["unrelated_dirty_sections"] for row in result["unrelated_dirty"]],
            [4, 16, 32],
        )
        for row in result["unrelated_dirty"]:
            self.assertEqual(row["samples"], 20)
            self.assertEqual(len(row["raw_samples_ms"]), 20)
            self.assertIn("owner_lock_wait_ms", row)
            self.assertIn("python_control_unavailable_ms", row)
            self.assertTrue(row["range_lifecycle_passed"])
            self.assert_raw_summary(row)
        self.assertEqual(result["b10_bounds"]["bounds"], [-2, 2, 61, 68, -2, 4])
        self.assertEqual(result["b10_bounds"]["samples"], 20)
        self.assertEqual(len(result["b10_bounds"]["raw_samples_ms"]), 20)
        self.assert_raw_summary(result["b10_bounds"])
        self.assertIn("owner_lock_wait_ms", result["b10_bounds"])
        self.assertIn("python_control_unavailable_ms", result["b10_bounds"])

        self.assertTrue(_passes_deadline({
            "p95_ms": 8.0, "p99_ms": 15.0, "max_ms": 29.999,
        }))
        for values in (
            {"p95_ms": 8.001, "p99_ms": 15.0, "max_ms": 29.0},
            {"p95_ms": 8.0, "p99_ms": 15.001, "max_ms": 29.0},
            {"p95_ms": 8.0, "p99_ms": 15.0, "max_ms": 30.0},
        ):
            with self.subTest(values=values):
                self.assertFalse(_passes_deadline(values))

    def assert_raw_summary(self, row) -> None:
        samples = row["raw_samples_ms"]
        ordered = sorted(samples)
        self.assertEqual(row["samples"], len(samples))
        self.assertEqual(row["p50_ms"], statistics.median(samples))
        self.assertEqual(row["p95_ms"], ordered[math.ceil(len(samples) * .95) - 1])
        self.assertEqual(row["p99_ms"], ordered[math.ceil(len(samples) * .99) - 1])
        self.assertEqual(row["max_ms"], max(samples))


def range_sections(start: int, stop: int | None = None):
    if stop is None:
        start, stop = 0, start
    return ((section, 0, 0) for section in range(start, stop))


if __name__ == "__main__":
    unittest.main()
