"""Replaced motion-navigation implementations kept for bounded compatibility.

New runtime code must not import this package. Each module states which
current owner replaced it and remains only until historical callers and
evidence have migrated.
"""

LIFECYCLE = "legacy_compatibility"

__all__ = ("LIFECYCLE",)
