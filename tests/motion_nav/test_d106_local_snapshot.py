"""D106 T2-2-R: a small detached snapshot fails closed outside its facts."""
from dataclasses import fields, replace
import hashlib
import unittest

from mc2p.motion_nav import prepared_motion_validation as admission
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionStatus,
    PreparedComputeStatus,
    PreparedCurrentFacts,
    PreparedMotionHandle,
    PreparedMotionHotView,
    PreparedRejectReason,
)
from mc2p.motion_nav.prepared_motion_proof import prepare_fixed_motion, seal_bundle
from mc2p.motion_nav.world_model import Aabb, CellKnowledge, UNKNOWN_CELL
from mc2p.motion_nav.world_snapshot import SealedSnapshot, canonical_snapshot_bytes
from tests.motion_nav import test_d102_prepared_proof as proof_fixtures


def _inside(bounds: Aabb, position: tuple[int, int, int]) -> bool:
    x, y, z = position
    return (bounds.min_x <= x < bounds.max_x and bounds.min_y <= y < bounds.max_y
            and bounds.min_z <= z < bounds.max_z)


def _clip(snapshot: SealedSnapshot, bounds: Aabb, *, changes=None) -> SealedSnapshot:
    facts = dict(snapshot.facts)
    facts.update(changes or {})
    clipped_facts = tuple(sorted((position, fact) for position, fact in facts.items()
                                 if _inside(bounds, position)))
    clipped_versions = tuple((position, version)
                             for position, version in snapshot.dependency_versions
                             if _inside(bounds, position))
    identity = replace(snapshot.identity, bounds=bounds, digest="0" * 64)
    data = canonical_snapshot_bytes(identity, clipped_facts, clipped_versions)
    identity = replace(identity, digest=hashlib.sha256(data).hexdigest())
    return SealedSnapshot(identity, data, clipped_facts, clipped_versions)


class D106LocalSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        proof_fixtures.ProofTests.setUpClass()
        cls.full_snapshot = proof_fixtures.ProofTests.snapshot
        cls.full_request = proof_fixtures.ProofTests.request
        cls.full_result = prepare_fixed_motion(cls.full_request, cls.full_snapshot)
        if cls.full_result.status is not PreparedComputeStatus.PREPARED:
            raise AssertionError("the frozen D102 proof fixture must remain prepared")
        dependencies = tuple(position for position, _ in cls.full_result.bundle.proof.dependency_facts)
        cls.local_bounds = Aabb(
            min(position[0] for position in dependencies),
            min(position[1] for position in dependencies),
            min(position[2] for position in dependencies),
            max(position[0] for position in dependencies) + 1,
            max(position[1] for position in dependencies) + 1,
            max(position[2] for position in dependencies) + 1,
        )
        cls.local_snapshot = _clip(cls.full_snapshot, cls.local_bounds)
        cls.local_request = replace(cls.full_request, snapshot_identity=cls.local_snapshot.identity)
        cls.local_result = prepare_fixed_motion(cls.local_request, cls.local_snapshot)

    def test_trusted_projection_matches_complete_construction_field_by_field(self) -> None:
        bundle = self.full_result.bundle
        complete = PreparedMotionHotView.from_bundle(bundle)
        trusted = admission._trusted_hot_view(bundle)
        self.assertEqual(
            {field.name: getattr(trusted, field.name) for field in fields(PreparedMotionHotView)},
            {field.name: getattr(complete, field.name) for field in fields(PreparedMotionHotView)},
        )
        stream = bundle.request.stream
        complete_handle = PreparedMotionHandle(
            "complete-projection-token", stream.request_id, stream.generation_id,
            stream.owner_id, stream.route_id, stream.action_index, bundle.proof_digest,
            bundle.proof.dependency_versions, complete,
        )
        trusted_handle = admission._trusted_handle(bundle, bundle.request, trusted)
        self.assertEqual(
            {field.name: getattr(trusted_handle, field.name)
             for field in fields(PreparedMotionHandle) if field.name != "proof_token"},
            {field.name: getattr(complete_handle, field.name)
             for field in fields(PreparedMotionHandle) if field.name != "proof_token"},
        )

    def test_actual_dependency_bounds_match_the_complete_snapshot_result(self) -> None:
        self.assertIs(self.local_result.status, PreparedComputeStatus.PREPARED)
        self.assertLess(len(self.local_snapshot.facts), len(self.full_snapshot.facts))
        self.assertEqual(self.local_result.counts, self.full_result.counts)
        self.assertEqual(self.local_result.bundle.proof, self.full_result.bundle.proof)
        for position, _ in self.local_result.bundle.proof.dependency_facts:
            self.assertTrue(_inside(self.local_snapshot.identity.bounds, position))

    def test_known_air_and_block_beyond_local_bounds_become_missing_information(self) -> None:
        # z=0 contains both a known AIR fact and the launch support BLOCK. A
        # detached snapshot clipped to z>=1 must not inherit either fact from
        # the live/full world.
        bounds = replace(self.local_bounds, min_z=1)
        snapshot = _clip(self.full_snapshot, bounds)
        outside_air = (0, -1, 0)
        outside_block = (0, 0, 0)
        self.assertIs(dict(self.full_snapshot.facts)[outside_air].knowledge, CellKnowledge.AIR)
        self.assertIs(dict(self.full_snapshot.facts)[outside_block].knowledge, CellKnowledge.BLOCK)
        self.assertIs(snapshot.cell(outside_air), UNKNOWN_CELL)
        self.assertIs(snapshot.cell(outside_block), UNKNOWN_CELL)
        request = replace(self.full_request, snapshot_identity=snapshot.identity)
        result = prepare_fixed_motion(request, snapshot)
        self.assertIs(result.status, PreparedComputeStatus.MISSING_INFORMATION)
        self.assertIsNone(result.bundle)
        self.assertTrue(result.missing_cells)

    def test_unknown_dependency_inside_bounds_is_typed_missing_information(self) -> None:
        # This head-space fact is read by the main motion branch. Some facts
        # in the complete proof belong only to a tail that a changed world can
        # avoid, so choosing merely the first AIR dependency would not prove
        # the required boundary.
        position = (0, 0, 1)
        self.assertIs(dict(self.full_result.bundle.proof.dependency_facts)[position].knowledge,
                      CellKnowledge.AIR)
        snapshot = _clip(self.full_snapshot, self.local_bounds, changes={position: UNKNOWN_CELL})
        request = replace(self.full_request, snapshot_identity=snapshot.identity)
        result = prepare_fixed_motion(request, snapshot)
        self.assertIs(result.status, PreparedComputeStatus.MISSING_INFORMATION)
        self.assertIsNone(result.bundle)
        self.assertIn(position, result.missing_cells)

    def test_parent_rejects_a_local_proof_after_an_actual_dependency_changes(self) -> None:
        self.assertIs(self.local_result.status, PreparedComputeStatus.PREPARED)
        bundle = self.local_result.bundle
        request = self.local_request
        current = PreparedCurrentFacts(
            request.async_identity.scope, request.stream, request.stream.owner_id,
            request.world_session_id, request.ruleset, bundle.proof.dependency_versions,
            request.ledger_id, self.local_snapshot.identity, request.entry_state,
            request.damage_remaining_points, request.entry_state.movement_tick_id,
            request.requested_window,
        )
        accepted = admission.validate_prepared_message(
            seal_bundle(bundle), request, self.local_snapshot, current)
        self.assertIs(accepted.status, PreparedAdmissionStatus.REGISTERED)
        position, version = current.dependency_versions[0]
        changed = replace(current, dependency_versions=((position, version + 1),)
                          + current.dependency_versions[1:])
        rejected = admission.validate_prepared_message(
            seal_bundle(bundle), request, self.local_snapshot, changed)
        self.assertIs(rejected.reason, PreparedRejectReason.DEPENDENCY)
        self.assertIsNone(rejected.handle)


if __name__ == "__main__":
    unittest.main()
