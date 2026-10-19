"""Background sealing of Runtime-owned immutable section versions.

This module owns no world, queue, registry, worker or mutable shape cache.
Only the immutable SealedSnapshot crosses a process boundary.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import hashlib
import json

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.motion_activation import validate_frozen_value
from mc2p.motion_nav.physics_types import PhysicsRuleset
from mc2p.motion_nav.prepared_motion import (
    DependencyFacts, DependencyVersions, SnapshotIdentity, WorldSnapshotVersion, _snapshot_facts,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, BlockPos, CellFact, ObservationStamp, UNKNOWN_CELL,
    VisualAirEvidence, WorldSessionId, _position,
)


class DependencyStatus(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    HISTORY_INCOMPLETE = "history_incomplete"


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    status: DependencyStatus
    changed_positions: tuple[BlockPos, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not DependencyStatus or type(self.changed_positions) is not tuple:
            raise ContractViolation("dependency check requires immutable typed values")
        for position in self.changed_positions:
            _position(position)
        if self.changed_positions != tuple(sorted(set(self.changed_positions))):
            raise ContractViolation("changed dependencies must be sorted and unique")
        if (self.status is DependencyStatus.CHANGED) != bool(self.changed_positions):
            raise ContractViolation("only changed dependencies carry changed positions")


def snapshot_version_content(version: WorldSnapshotVersion) -> tuple[DependencyFacts, DependencyVersions]:
    """Expand and clip shared section storage only on the background path."""
    bounds = version.bounds
    def inside(position):
        x, y, z = position
        return (bounds.min_x <= x < bounds.max_x and bounds.min_y <= y < bounds.max_y
                and bounds.min_z <= z < bounds.max_z)
    facts = tuple(sorted((item for section in version.section_references
                          for item in section.facts if inside(item[0])), key=lambda item: item[0]))
    versions = tuple(sorted((item for section in version.section_references
                             for item in section.dependency_versions if inside(item[0])),
                            key=lambda item: item[0]))
    return facts, versions


def canonical_snapshot_bytes(identity: SnapshotIdentity, facts: DependencyFacts,
                             dependency_versions: DependencyVersions) -> bytes:
    """Stable UTF-8 bytes binding metadata, observations, geometry and materials."""
    metadata = asdict(identity)
    del metadata["digest"]
    return json.dumps({"schema": "mc2p.sealed-world-snapshot.v1", "identity": metadata,
                       "facts": [(position, asdict(fact)) for position, fact in facts],
                       "dependency_versions": dependency_versions},
                      sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SealedSnapshot:
    identity: SnapshotIdentity
    canonical_bytes: bytes
    facts: DependencyFacts
    dependency_versions: DependencyVersions

    def __post_init__(self) -> None:
        validate_sealed_snapshot(self)

    @property
    def session(self):
        from mc2p.motion_nav.world_model import WorldSessionId
        return WorldSessionId(self.identity.world_session_id)

    @property
    def geometry_revision(self) -> int:
        return self.identity.geometry_revision

    def cell(self, position: BlockPos) -> CellFact:
        _position(position)
        # No dictionary or cache belongs to the signed snapshot object.
        index = bisect_left(self.facts, position, key=lambda item: item[0])
        if index < len(self.facts) and self.facts[index][0] == position:
            return self.facts[index][1]
        return UNKNOWN_CELL

    def validate_sections(self, sections) -> None:
        # An immutable snapshot remains readable after the live world changes.
        return None


def validate_sealed_snapshot(snapshot: SealedSnapshot) -> None:
    """Revalidate restored transport objects before publication, outside locks."""
    if type(snapshot) is not SealedSnapshot:
        raise ContractViolation("snapshot validation requires the exact sealed type")
    validate_frozen_value(snapshot)
    snapshot.identity.__post_init__()
    _validate_native_fact_values(snapshot.facts, snapshot.identity.bounds)
    _snapshot_facts(snapshot.identity.world_session_id, snapshot.identity.bounds,
                    snapshot.facts, snapshot.dependency_versions)
    if (snapshot.canonical_bytes != canonical_snapshot_bytes(snapshot.identity, snapshot.facts,
                                                             snapshot.dependency_versions)
            or hashlib.sha256(snapshot.canonical_bytes).hexdigest() != snapshot.identity.digest):
        raise ContractViolation("sealed snapshot bytes, digest and facts describe different versions")


def _validate_native_fact_values(facts: DependencyFacts, bounds: Aabb) -> None:
    """Repeat native field types and constructors skipped by pickle restoration.

    Equality and canonical JSON cannot distinguish a StrEnum from its value.
    Check each native object directly; shared stamps/geometries need one check.
    """
    checked: set[int] = set()

    def check(value, native_type):
        if type(value) is not native_type:
            raise ContractViolation("snapshot fact contains a non-native contract type")
        identity = id(value)
        if identity in checked:
            return
        validate_frozen_value(value)
        if native_type is CellFact:
            if value.stamp is not None:
                check(value.stamp, ObservationStamp)
            if value.block is not None:
                check(value.block, BlockGeometry)
            if value.visual_air_evidence is not None:
                check(value.visual_air_evidence, VisualAirEvidence)
        elif native_type is ObservationStamp:
            check(value.session, WorldSessionId)
        elif native_type is BlockGeometry:
            for box in value.boxes:
                check(box, Aabb)
        elif native_type is VisualAirEvidence:
            check(value.stamp, ObservationStamp)
        native_type.__post_init__(value)
        checked.add(identity)

    check(bounds, Aabb)
    for _, fact in facts:
        check(fact, CellFact)


def seal_snapshot_version(version: WorldSnapshotVersion, ruleset: PhysicsRuleset) -> SealedSnapshot:
    if type(version) is not WorldSnapshotVersion or type(ruleset) is not PhysicsRuleset:
        raise ContractViolation("sealing requires a borrowed version and ruleset")
    if version.ruleset != ruleset:
        raise ContractViolation("snapshot sealing ruleset differs from borrowed version")
    validate_frozen_value(version)
    facts, versions = snapshot_version_content(version)
    identity = SnapshotIdentity(version.world_session_id, version.geometry_revision,
                                version.evidence_revision, version.local_revision, "0" * 64,
                                ruleset, version.bounds)
    data = canonical_snapshot_bytes(identity, facts, versions)
    identity = replace(identity, digest=hashlib.sha256(data).hexdigest())
    return SealedSnapshot(identity, data, facts, versions)
