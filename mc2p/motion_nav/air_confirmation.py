"""Compatibility import for the replaced B02 air-confirmation coordinator.

The live owner is :class:`NavigationObservationAdapter`, which creates bounded
air requests and ingests their positive results. New runtime code must not use
this historical coordinator directly.
"""

from mc2p.motion_nav.legacy.air_confirmation import (
    AirConfirmationBatch,
    AirConfirmationService,
    AirProbe,
    AirResolution,
)

LIFECYCLE = "compatibility_import"

__all__ = (
    "AirConfirmationBatch",
    "AirConfirmationService",
    "AirProbe",
    "AirResolution",
)
