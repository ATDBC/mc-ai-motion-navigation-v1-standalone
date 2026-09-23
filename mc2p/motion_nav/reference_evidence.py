"""Compatibility import for sealed historical-evidence normalization.

New code should import :mod:`mc2p.motion_nav.evidence.reference_evidence`.
"""

from mc2p.motion_nav.evidence.reference_evidence import normalize_legacy_evidence

LIFECYCLE = "compatibility_import"

__all__ = ("normalize_legacy_evidence",)
