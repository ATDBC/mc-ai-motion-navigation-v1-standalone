"""Compatibility import for the pre-refactor adapter module name."""

from mc2p.motion_nav.observed_block_adapter import apply_observed_blocks


apply_visible_blocks = apply_observed_blocks


__all__ = ("apply_visible_blocks",)
