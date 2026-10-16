"""Runtime-owned D102 snapshot versions and bounded lease behavior."""
from dataclasses import FrozenInstanceError, replace
import gc
import hashlib
import pickle
import threading
import time
import unittest
import weakref
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.prepared_motion import SnapshotLeaseStatus
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellFact, CellKnowledge, ImmutableWorldSection,
    ObservationStamp, VisualAirEvidence, WorldKnowledge, WorldSessionId,
)


def _restore_snapshot_wire_fields(fields):
    """Model pickle's dataclass slot restoration, which skips construction."""
    from mc2p.motion_nav.world_snapshot import SealedSnapshot
    value = SealedSnapshot.__new__(SealedSnapshot)
    value.__setstate__(fields)
    return value


class SnapshotWirePayload:
    def __init__(self, fields):
        self.fields = fields

    def __reduce__(self):
        return _restore_snapshot_wire_fields, (self.fields,)


def _restore_native_wire_fields(kind, fields):
    value = kind.__new__(kind)
    value.__setstate__(fields)
    return value


class NativeValueWirePayload:
    def __init__(self, kind, fields):
        self.kind = kind
        self.fields = fields

    def __reduce__(self):
        return _restore_native_wire_fields, (self.kind, self.fields)


class SnapshotTests(unittest.TestCase):
    position = (0, 0, 0)
    bounds = PhysicsWorldBounds(0, 3, 0, 1, 0, 1)

    def setUp(self):
        self.world = WorldKnowledge(WorldSessionId("snapshot-world"))
        self.stone = BlockGeometry.full_cube("minecraft:stone")
        self.observe(1, {self.position: self.stone})

    def stamp(self, sequence):
        return ObservationStamp(self.world.session, sequence, sequence, "clock", sequence * 50_000_000)

    def observe(self, sequence, blocks):
        self.world.observe_blocks(self.stamp(sequence), blocks)

    def published(self, bounds=None):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        region = self.bounds if bounds is None else bounds
        version = self.world.borrow_snapshot_version(region, ruleset=JAVA_1_21_RULESET)
        self.assertIsNotNone(version)
        sealed = seal_snapshot_version(version, JAVA_1_21_RULESET)
        self.assertTrue(self.world.publish_sealed_snapshot(version, sealed))
        result = self.world.acquire_published_snapshot(region, ruleset=JAVA_1_21_RULESET)
        self.assertIs(result.status, SnapshotLeaseStatus.READY)
        return version, result.lease

    def test_old_lease_survives_new_observation(self):
        _, old_lease = self.published()
        old_fact = old_lease.cell(self.position)
        self.observe(2, {self.position: BlockGeometry.full_cube("minecraft:dirt")})
        self.assertEqual(old_lease.cell(self.position), old_fact)
        _, new_lease = self.published()
        self.assertNotEqual(old_lease.identity, new_lease.identity)
        self.assertTrue(self.world.release_snapshot(old_lease.lease_token))
        self.assertFalse(self.world.release_snapshot(old_lease.lease_token))
        self.assertEqual(new_lease.cell(self.position).block.material_key, "minecraft:dirt")

    def test_revision_digest_and_facts_are_one_version(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        self.observe(2, {self.position: BlockGeometry.full_cube("minecraft:dirt")})
        sealed = seal_snapshot_version(version, JAVA_1_21_RULESET)
        self.assertEqual(sealed.identity.geometry_revision, version.geometry_revision)
        self.assertEqual(sealed.identity.digest, hashlib.sha256(sealed.canonical_bytes).hexdigest())
        self.assertEqual(dict(sealed.facts)[self.position].block, self.stone)
        self.assertTrue(self.world.publish_sealed_snapshot(version, sealed))
        # Publication retains the original version; acquiring new work requires current facts.
        self.assertIs(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET).status,
                      SnapshotLeaseStatus.NOT_READY)
        with self.assertRaises(ContractViolation):
            replace(sealed, facts=((self.position, self.world.view().cell(self.position)),))

    def test_unknown_is_not_air(self):
        self.world.confirm_air(self.stamp(2), ((1, 0, 0),))
        _, lease = self.published()
        self.assertIs(lease.cell((2, 0, 0)).knowledge, CellKnowledge.UNKNOWN)
        self.assertIs(lease.cell((1, 0, 0)).knowledge, CellKnowledge.AIR)
        view = PhysicsWorldView.from_snapshot(lease.sealed_snapshot())
        self.assertEqual(view.shapes(((2, 0, 0),)).missing_cells, ((2, 0, 0),))

    def test_unrelated_cell_change_keeps_dependency_valid(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        _, lease = self.published()
        self.observe(2, {(3, 0, 0): self.stone})
        self.assertIs(self.world.dependency_changes(lease.dependency_versions).status,
                      DependencyStatus.UNCHANGED)

    def test_related_change_invalidates_without_reclaiming_lease(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        _, lease = self.published()
        self.world.confirm_air(self.stamp(2), (self.position,))
        check = self.world.dependency_changes(lease.dependency_versions)
        self.assertIs(check.status, DependencyStatus.CHANGED)
        self.assertEqual(check.changed_positions, (self.position,))
        self.assertEqual(lease.cell(self.position).block, self.stone)
        self.assertTrue(self.world.release_snapshot(lease.lease_token))

    def test_control_acquire_never_builds_or_serializes(self):
        import mc2p.motion_nav.world_snapshot as snapshots
        version, lease = self.published()
        control_counts = {"copy": 0, "serialize": 0}
        def forbidden_copy(*args, **kwargs):
            control_counts["copy"] += 1
            raise AssertionError("control attempted snapshot construction")
        def forbidden_serialize(*args, **kwargs):
            control_counts["serialize"] += 1
            raise AssertionError("control attempted serialization")
        with patch.object(snapshots, "seal_snapshot_version", forbidden_copy), \
                patch.object(snapshots, "canonical_snapshot_bytes", forbidden_serialize), \
                patch.object(snapshots.hashlib, "sha256", forbidden_serialize), \
                patch.object(self.world, "dependency_changes", forbidden_copy), \
                patch.object(self.world, "changes_since", forbidden_copy):
            borrowed = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
            self.assertIs(borrowed.section_references[0], version.section_references[0])
            for _ in range(3):
                acquired = self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET)
                self.assertIs(acquired.status, SnapshotLeaseStatus.READY)
                self.assertIs(acquired.lease.sealed_snapshot(), lease.sealed_snapshot())
        self.assertEqual(control_counts["copy"] + control_counts["serialize"], 0)

    def test_stamp_refresh_does_not_fake_geometry_change(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        version, old_lease = self.published()
        self.observe(2, {self.position: self.stone})
        self.assertIs(self.world.dependency_changes(old_lease.dependency_versions).status,
                      DependencyStatus.UNCHANGED)
        newer, new_lease = self.published()
        self.assertEqual(version.geometry_revision, newer.geometry_revision)
        self.assertGreater(newer.evidence_revision, version.evidence_revision)
        self.assertNotEqual(new_lease.identity.digest, old_lease.identity.digest)
        self.assertEqual(old_lease.cell(self.position).stamp.sequence_id, 1)

    def test_evidence_only_refresh_is_coalesced_until_snapshot_borrow(self):
        section_position = (0, 0, 0)
        self.assertIsNotNone(self.world.borrow_snapshot_version(
            self.bounds, ruleset=JAVA_1_21_RULESET,
        ))
        original = self.world._snapshot_sections[section_position]
        original_geometry_revision = self.world.view().geometry_revision
        original_evidence_revision = self.world.view().evidence_revision

        self.observe(2, {self.position: self.stone})

        live = self.world.view().cell(self.position)
        self.assertEqual(live.stamp.sequence_id, 2)
        self.assertEqual(self.world.view().geometry_revision, original_geometry_revision)
        self.assertGreater(self.world.view().evidence_revision, original_evidence_revision)
        self.assertIs(self.world._snapshot_sections[section_position], original)

        refreshed = self.world.borrow_snapshot_version(
            self.bounds, ruleset=JAVA_1_21_RULESET,
        )
        self.assertIsNotNone(refreshed)
        self.assertIsNot(refreshed.section_references[0], original)
        self.assertEqual(dict(refreshed.section_references[0].facts)[self.position].stamp.sequence_id, 2)
        self.assertEqual(dict(original.facts)[self.position].stamp.sequence_id, 1)

    def test_latest_visual_air_evidence_is_preserved_when_snapshot_is_borrowed(self):
        air = (1, 0, 0)
        first_stamp = self.stamp(2)
        first = VisualAirEvidence(first_stamp, 3.0, False)
        self.world.confirm_air(first_stamp, (air,), {air: first})
        section_position = (0, 0, 0)
        self.assertIsNotNone(self.world.borrow_snapshot_version(
            self.bounds, ruleset=JAVA_1_21_RULESET,
        ))
        original = self.world._snapshot_sections[section_position]

        newer_stamp = self.stamp(3)
        newer = VisualAirEvidence(newer_stamp, 1.0, True)
        self.world.confirm_air(newer_stamp, (air,), {air: newer})

        self.assertIs(self.world._snapshot_sections[section_position], original)
        self.assertEqual(self.world.view().cell(air).visual_air_evidence, newer)
        refreshed = self.world.borrow_snapshot_version(
            self.bounds, ruleset=JAVA_1_21_RULESET,
        )
        self.assertIsNotNone(refreshed)
        self.assertEqual(
            dict(refreshed.section_references[0].facts)[air].visual_air_evidence,
            newer,
        )

    def test_locally_validated_observation_does_not_repeat_deep_graph_validation(self):
        import mc2p.contracts.motion_activation as activation
        dirt = BlockGeometry.full_cube("minecraft:dirt")

        with patch.object(
            activation,
            "validate_frozen_value",
            side_effect=AssertionError("trusted observation repeated deep validation"),
        ):
            self.observe(2, {self.position: dirt})
            self.assertIsNotNone(self.world.borrow_snapshot_version(
                self.bounds, ruleset=JAVA_1_21_RULESET,
            ))

        self.assertEqual(
            self.world._snapshot_sections[(0, 0, 0)].facts[0][1].block,
            dirt,
        )

    def test_capacity_returns_not_ready_without_waiting(self):
        world = WorldKnowledge(WorldSessionId("capacity"), max_snapshot_leases=1)
        self.world = world
        self.observe(1, {self.position: self.stone})
        _, lease = self.published()
        full = world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET)
        self.assertIs(full.status, SnapshotLeaseStatus.CAPACITY)
        self.assertIs(world.acquire_published_snapshot(PhysicsWorldBounds(0, 100, 0, 1, 0, 1),
                                                      ruleset=JAVA_1_21_RULESET).status,
                      SnapshotLeaseStatus.NOT_READY)
        self.assertTrue(world.release_snapshot(lease.lease_token))
        self.assertIs(world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET).status,
                      SnapshotLeaseStatus.READY)

    def test_non_cube_materials_and_sealed_spawn_graph(self):
        slab = BlockGeometry("minecraft:stone_slab", "boxes", (Aabb(0, 0, 0, 1, .5, 1),))
        self.observe(2, {self.position: slab})
        _, lease = self.published()
        sealed = pickle.loads(pickle.dumps(lease.sealed_snapshot()))
        view = PhysicsWorldView.from_snapshot(sealed)
        self.assertTrue(view.is_detached)
        self.assertEqual(view.shapes((self.position,)).boxes, (Aabb(0, 0, 0, 1, .5, 1),))
        self.assertEqual(view.cell(self.position).block.material_key, "minecraft:stone_slab")
        with self.assertRaises(FrozenInstanceError):
            sealed.identity = None
        with self.assertRaises(TypeError):
            sealed.facts[0] = None

    def test_history_incomplete_and_session_change_are_not_unchanged(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        unknown = (3, 0, 0)
        original = self.world.dependency_versions((unknown,))
        for sequence in range(2, 4100):
            self.observe(sequence, {self.position: BlockGeometry.full_cube(
                "minecraft:stone" if sequence % 2 else "minecraft:dirt")})
        self.assertIs(self.world.dependency_changes(original).status, DependencyStatus.HISTORY_INCOMPLETE)
        other = WorldKnowledge(WorldSessionId("new-session"))
        self.assertIs(other.dependency_changes(original).status, DependencyStatus.CHANGED)

    def test_publication_content_and_ruleset_mismatch_are_rejected(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        version, lease = self.published()
        with self.assertRaises(ContractViolation):
            seal_snapshot_version(version, replace(JAVA_1_21_RULESET, ruleset_id="different"))
        other = WorldKnowledge(WorldSessionId("different-session"))
        self.assertFalse(other.publish_sealed_snapshot(version, lease.sealed_snapshot()))
        with self.assertRaises(ContractViolation):
            replace(lease.sealed_snapshot(), canonical_bytes=b"bad")

    def test_control_acquire_does_not_wait_for_busy_owner_or_publication(self):
        self.published()
        for lock in (self.world._snapshot_version_lock, self.world._snapshot_publication_lock):
            with self.subTest(lock=lock):
                held = threading.Event()
                finish = threading.Event()
                def hold():
                    with lock:
                        held.set()
                        finish.wait(2)
                thread = threading.Thread(target=hold)
                thread.start()
                self.assertTrue(held.wait(1))
                try:
                    started = time.perf_counter()
                    result = self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET)
                    self.assertIs(result.status, SnapshotLeaseStatus.NOT_READY)
                    self.assertLess(time.perf_counter() - started, .5)
                finally:
                    finish.set()
                    thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_storage_capacity_preserves_pinned_lease_and_bounds_rejection(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        self.world = WorldKnowledge(WorldSessionId("storage-capacity"), max_snapshot_versions=1,
                                    max_snapshot_cells=16)
        self.observe(1, {self.position: self.stone})
        _, old = self.published()
        self.observe(2, {self.position: BlockGeometry.full_cube("minecraft:dirt")})
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        sealed = seal_snapshot_version(version, JAVA_1_21_RULESET)
        self.assertFalse(self.world.publish_sealed_snapshot(version, sealed))
        self.assertEqual(old.cell(self.position).block, self.stone)
        self.assertIsNone(self.world.borrow_snapshot_version(PhysicsWorldBounds(0, 4, 0, 1, 0, 1),
                                                            ruleset=JAVA_1_21_RULESET))
        self.assertTrue(self.world.release_snapshot(old.lease_token))
        self.assertTrue(self.world.publish_sealed_snapshot(version, sealed))
        self.assertEqual(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET)
                         .lease.cell(self.position).block.material_key, "minecraft:dirt")

    def test_runtime_port_reuses_owner_and_old_session_port_can_release(self):
        from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
        from tests.observation_v3_fixtures import valid_snapshot_v3
        adapter = NavigationObservationAdapter()
        self.assertIsNone(adapter.snapshot_world_port)
        first = adapter.ingest(replace(valid_snapshot_v3(sequence=1), episode_id="one"))
        self.assertIs(adapter.snapshot_world_port, first.world._owner)
        self.world = adapter.snapshot_world_port
        _, lease = self.published()
        original_port = self.world
        second = adapter.ingest(replace(valid_snapshot_v3(sequence=0), episode_id="two"))
        self.assertIs(adapter.snapshot_world_port, second.world._owner)
        self.assertIsNot(adapter.snapshot_world_port, original_port)
        self.assertTrue(original_port.release_snapshot(lease.lease_token))
        self.assertEqual(lease.identity.world_session_id, first.session.value)

    def test_sealed_size_bound_rejects_without_reclaiming(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        self.world = WorldKnowledge(WorldSessionId("byte-capacity"), max_snapshot_bytes=1)
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        self.assertFalse(self.world.publish_sealed_snapshot(
            version, seal_snapshot_version(version, JAVA_1_21_RULESET)))
        self.assertIs(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET).status,
                      SnapshotLeaseStatus.NOT_READY)

    def test_legacy_explicit_physics_pickle_exports_only_detached_facts(self):
        live = PhysicsWorldView(self.world.view(), JAVA_1_21_RULESET)
        live.shapes((self.position,))
        self.assertTrue(live._shape_cache)
        restored = pickle.loads(pickle.dumps(live))
        self.assertTrue(restored.is_detached)
        self.assertIsNone(restored._world._owner)
        self.assertEqual(restored._shape_cache, {})
        self.assertIs(restored.cell((2, 0, 0)).knowledge, CellKnowledge.UNKNOWN)
        self.observe(2, {self.position: BlockGeometry.full_cube("minecraft:dirt")})
        self.assertEqual(restored.cell(self.position).block, self.stone)
        with self.assertRaises(ContractViolation):
            restored._world.set_protection((0., 0., 0.), ())

    def storage_state(self):
        return (dict(self.world._published_snapshots), set(self.world._ready_snapshot_revisions),
                dict(self.world._snapshot_leases), dict(self.world._snapshot_reference_counts),
                self.world._published_snapshot_bytes)

    def test_replace_section_cannot_forge_owner_snapshot_source(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        section = version.section_references[0]
        fake = ImmutableWorldSection(section.position,
                                     ((self.position, CellFact(CellKnowledge.AIR, self.stamp(1))),),
                                     section.dependency_versions)
        forged_version = replace(version, section_references=(fake,))
        forged = seal_snapshot_version(forged_version, JAVA_1_21_RULESET)
        before = self.storage_state()
        self.assertFalse(self.world.publish_sealed_snapshot(forged_version, forged))
        self.assertEqual(self.storage_state(), before)
        self.assertIs(self.world.view().cell(self.position).knowledge, CellKnowledge.BLOCK)

    def test_source_replacement_and_pickle_clone_cannot_mint_issuance(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        for forged in (replace(version, source_provenance=replace(version.source_provenance)),
                       replace(version, source_provenance=None), pickle.loads(pickle.dumps(version))):
            with self.subTest(forged=forged):
                sealed = seal_snapshot_version(forged, JAVA_1_21_RULESET)
                before = self.storage_state()
                self.assertFalse(self.world.publish_sealed_snapshot(forged, sealed))
                self.assertEqual(self.storage_state(), before)

    def test_source_weak_index_does_not_extend_borrowed_storage_lifetime(self):
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        token = version.source_provenance.token
        original = weakref.ref(version.source_provenance)
        self.assertIs(self.world._snapshot_sources[token], original())
        del version
        gc.collect()
        self.assertIsNone(original())
        self.assertNotIn(token, self.world._snapshot_sources)

    def test_pickle_byte_tamper_is_revalidated_before_publication(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        self.published()
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        original = seal_snapshot_version(version, JAVA_1_21_RULESET)
        raw = pickle.dumps(original)
        corrupted = pickle.loads(raw.replace(b"mc2p.sealed-world-snapshot.v1",
                                            b"mc2p.sealed-world-snapshot.v0"))
        self.assertNotEqual(hashlib.sha256(corrupted.canonical_bytes).hexdigest(),
                            corrupted.identity.digest)
        before = self.storage_state()
        self.assertFalse(self.world.publish_sealed_snapshot(version, corrupted))
        self.assertEqual(self.storage_state(), before)

    def test_pickle_mutable_bytes_graph_is_rejected_without_storage_changes(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        self.published()
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        original = seal_snapshot_version(version, JAVA_1_21_RULESET)
        restored = pickle.loads(pickle.dumps(SnapshotWirePayload([
            original.identity, bytearray(original.canonical_bytes), original.facts,
            original.dependency_versions])))
        self.assertIsInstance(restored.canonical_bytes, bytearray)
        before = self.storage_state()
        self.assertFalse(self.world.publish_sealed_snapshot(version, restored))
        self.assertEqual(self.storage_state(), before)

    def test_publication_revalidation_runs_outside_both_owner_locks(self):
        import mc2p.motion_nav.world_snapshot as snapshots
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        sealed = snapshots.seal_snapshot_version(version, JAVA_1_21_RULESET)
        validate = snapshots.validate_sealed_snapshot
        calls = []
        def check(value):
            self.assertFalse(self.world._snapshot_version_lock._is_owned())
            self.assertFalse(self.world._snapshot_publication_lock.locked())
            calls.append(value)
            return validate(value)
        with patch.object(snapshots, "validate_sealed_snapshot", check):
            self.assertTrue(self.world.publish_sealed_snapshot(version, sealed))
        self.assertEqual(calls, [sealed])

    def test_frozen_default_lease_cells_and_byte_capacities(self):
        _, first = self.published()
        for _ in range(7):
            self.assertIs(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET)
                          .status, SnapshotLeaseStatus.READY)
        self.assertIs(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET)
                      .status, SnapshotLeaseStatus.CAPACITY)
        self.assertIsNotNone(self.world.borrow_snapshot_version(PhysicsWorldBounds(0, 16383, 0, 0, 0, 0),
                                                                ruleset=JAVA_1_21_RULESET))
        self.assertIsNone(self.world.borrow_snapshot_version(PhysicsWorldBounds(0, 16384, 0, 0, 0, 0),
                                                            ruleset=JAVA_1_21_RULESET))
        self.assertLessEqual(self.world._max_snapshot_bytes, 16 * 1024 * 1024)
        self.assertEqual(first.cell(self.position).block, self.stone)

    def test_over_service_byte_budget_does_not_replace_existing_lease(self):
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        _, old = self.published()
        self.observe(2, {self.position: BlockGeometry.full_cube("m" * (16 * 1024 * 1024))})
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        large = seal_snapshot_version(version, JAVA_1_21_RULESET)
        self.assertGreater(len(large.canonical_bytes), 16 * 1024 * 1024)
        before = self.storage_state()
        self.assertFalse(self.world.publish_sealed_snapshot(version, large))
        self.assertEqual(self.storage_state(), before)
        self.assertEqual(old.cell(self.position).block, self.stone)

    def test_explicit_pickle_rejects_deleted_or_changed_original_section(self):
        for mode in ("invalidate", "evict", "change"):
            with self.subTest(mode=mode):
                self.world = WorldKnowledge(WorldSessionId(mode), max_known_cells=1,
                                            protection_radius_blocks=0)
                self.observe(1, {self.position: self.stone})
                live = PhysicsWorldView(self.world.view(), JAVA_1_21_RULESET)
                if mode == "invalidate":
                    self.world.invalidate(self.stamp(2), (self.position,))
                elif mode == "evict":
                    self.observe(2, {(32, 0, 0): self.stone})
                else:
                    self.observe(2, {self.position: BlockGeometry.full_cube("minecraft:dirt")})
                with self.assertRaisesRegex(ContractViolation, "section changed"):
                    pickle.dumps(live)

    def test_change_during_publish_preserves_old_version_without_new_ready(self):
        import mc2p.motion_nav.world_snapshot as snapshots
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        sealed = snapshots.seal_snapshot_version(version, JAVA_1_21_RULESET)
        content_ready = threading.Event()
        proceed = threading.Event()
        content = snapshots.snapshot_version_content
        result = []
        errors = []
        def pause(value):
            value_content = content(value)
            content_ready.set()
            if not proceed.wait(2):
                raise AssertionError("publication barrier timed out")
            return value_content
        def publish():
            try:
                result.append(self.world.publish_sealed_snapshot(version, sealed))
            except BaseException as error:
                errors.append(error)
        with patch.object(snapshots, "snapshot_version_content", pause):
            thread = threading.Thread(target=publish)
            thread.start()
            try:
                self.assertTrue(content_ready.wait(2))
                self.world.confirm_air(self.stamp(2), (self.position,))
            finally:
                proceed.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [True])
        self.assertEqual(dict(sealed.facts)[self.position].block, self.stone)
        self.assertIs(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET).status,
                      SnapshotLeaseStatus.NOT_READY)

    def test_pickle_block_string_is_rejected_despite_equal_source_and_canonical_bytes(self):
        import mc2p.motion_nav.world_snapshot as snapshots
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        sealed = snapshots.seal_snapshot_version(version, JAVA_1_21_RULESET)
        fact = sealed.cell(self.position)
        wire = SnapshotWirePayload([sealed.identity, sealed.canonical_bytes,
                                    ((self.position, NativeValueWirePayload(CellFact, [
                                        "block", fact.stamp, fact.block, None])),),
                                    sealed.dependency_versions])
        restored = pickle.loads(pickle.dumps(wire))
        self.assertIs(type(restored.cell(self.position).knowledge), str)
        self.assertEqual(restored.facts, sealed.facts)
        self.assertEqual(snapshots.canonical_snapshot_bytes(restored.identity, restored.facts,
                                                          restored.dependency_versions), sealed.canonical_bytes)
        before = self.storage_state()
        with self.assertRaises(ContractViolation):
            snapshots.validate_sealed_snapshot(restored)
        self.assertFalse(self.world.publish_sealed_snapshot(version, restored))
        self.assertEqual(self.storage_state(), before)
        self.assertIs(self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET).status,
                      SnapshotLeaseStatus.NOT_READY)

    def test_normal_pickle_block_keeps_one_collision_box(self):
        import mc2p.motion_nav.world_snapshot as snapshots
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        restored = pickle.loads(pickle.dumps(snapshots.seal_snapshot_version(version, JAVA_1_21_RULESET)))
        snapshots.validate_sealed_snapshot(restored)
        self.assertTrue(self.world.publish_sealed_snapshot(version, restored))
        lease = self.world.acquire_published_snapshot(self.bounds, ruleset=JAVA_1_21_RULESET).lease
        self.assertIs(lease.cell(self.position).knowledge, CellKnowledge.BLOCK)
        shapes = PhysicsWorldView.from_snapshot(lease.sealed_snapshot()).shapes((self.position,))
        self.assertEqual(shapes.boxes, (Aabb(0, 0, 0, 1, 1, 1),))
        self.assertEqual(shapes.missing_cells + shapes.unsupported_cells, ())

    def test_pickle_nested_native_types_and_constructor_ranges_are_rechecked(self):
        import mc2p.motion_nav.world_snapshot as snapshots
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=JAVA_1_21_RULESET)
        sealed = snapshots.seal_snapshot_version(version, JAVA_1_21_RULESET)
        stamp = sealed.cell(self.position).stamp
        def stamp_wire(sequence=1, session=None):
            return NativeValueWirePayload(ObservationStamp, [
                stamp.session if session is None else session, sequence, 1, "clock", 50_000_000])
        invalid_stamp = stamp_wire(-1)
        invalid_session = NativeValueWirePayload(WorldSessionId, ["bad session"])
        invalid_box = NativeValueWirePayload(Aabb, [0, 0, 0, 0, .5, 1])
        cases = (
            (CellKnowledge.BLOCK, invalid_stamp, self.stone, None),
            (CellKnowledge.BLOCK, stamp_wire(True), self.stone, None),
            (CellKnowledge.BLOCK, stamp_wire(session=invalid_session), self.stone, None),
            (CellKnowledge.BLOCK, stamp, NativeValueWirePayload(BlockGeometry, [
                "bad material", "full_cube", (), False]), None),
            (CellKnowledge.BLOCK, stamp, NativeValueWirePayload(BlockGeometry, [
                "minecraft:stone", "full_cube", (), 0]), None),
            (CellKnowledge.BLOCK, stamp, NativeValueWirePayload(BlockGeometry, [
                "minecraft:slab", "boxes", (invalid_box,), False]), None),
            (CellKnowledge.BLOCK, stamp, NativeValueWirePayload(BlockGeometry, [
                "minecraft:stone", "boxes", (), False]), None),
            (CellKnowledge.AIR, stamp, self.stone, None),
            (CellKnowledge.UNKNOWN, stamp, None, None),
            (CellKnowledge.AIR, stamp, None, NativeValueWirePayload(VisualAirEvidence, [stamp, -1., True])),
            (CellKnowledge.AIR, stamp, None, NativeValueWirePayload(VisualAirEvidence, [stamp, 1., 1])),
        )
        for fields in cases:
            with self.subTest(fields=fields):
                invalid_fact = pickle.loads(pickle.dumps(NativeValueWirePayload(CellFact, list(fields))))
                facts = ((self.position, invalid_fact),)
                data = snapshots.canonical_snapshot_bytes(sealed.identity, facts, sealed.dependency_versions)
                identity = replace(sealed.identity, digest=hashlib.sha256(data).hexdigest())
                restored = pickle.loads(pickle.dumps(SnapshotWirePayload([
                    identity, data, facts, sealed.dependency_versions])))
                with self.assertRaises(ContractViolation):
                    snapshots.validate_sealed_snapshot(restored)


if __name__ == "__main__":
    unittest.main()
