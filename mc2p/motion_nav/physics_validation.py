"""Compatibility import for offline physics comparison helpers.

New code should import :mod:`mc2p.motion_nav.evidence.physics_validation`.
"""

from mc2p.motion_nav.evidence.physics_validation import (
    OpenLoopValidationReport,
    ValidationReport,
    compare_open_loop_rows,
    compare_tick_rows,
)

LIFECYCLE = "compatibility_import"

__all__ = (
    "OpenLoopValidationReport",
    "ValidationReport",
    "compare_open_loop_rows",
    "compare_tick_rows",
)
