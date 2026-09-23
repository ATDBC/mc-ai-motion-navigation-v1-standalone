"""Offline calibration, comparison and frozen-evidence helpers.

Nothing in this package may own live navigation state or write player input.
Online motion code can be measured by these helpers, but must not depend on
them to make a control decision.
"""

LIFECYCLE = "evidence_only"

__all__ = ("LIFECYCLE",)
