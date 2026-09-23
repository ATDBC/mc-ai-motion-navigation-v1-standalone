"""Compatibility import for the evidence-only ground calibration helpers.

New code should import :mod:`mc2p.motion_nav.evidence.ground_calibration`.
"""

from mc2p.motion_nav.evidence.ground_calibration import (
    GroundMotionCalibration,
    GroundMotionSample,
    calibrate_ground_motion,
)

LIFECYCLE = "compatibility_import"

__all__ = (
    "GroundMotionCalibration",
    "GroundMotionSample",
    "calibrate_ground_motion",
)
