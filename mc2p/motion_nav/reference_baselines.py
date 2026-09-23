"""Compatibility import for frozen-reference verification helpers.

New code should import :mod:`mc2p.motion_nav.evidence.reference_baselines`.
"""

from mc2p.motion_nav.evidence.reference_baselines import (
    ReferenceCatalog,
    ReferenceScene,
    ReferenceVersion,
    SnapshotVerification,
    load_reference_catalog,
    verify_scene_sources,
    verify_snapshot,
    verify_version_snapshot_trees,
)

LIFECYCLE = "compatibility_import"

__all__ = (
    "ReferenceCatalog",
    "ReferenceScene",
    "ReferenceVersion",
    "SnapshotVerification",
    "load_reference_catalog",
    "verify_scene_sources",
    "verify_snapshot",
    "verify_version_snapshot_trees",
)
