"""Continuous behavior profile contract."""

from __future__ import annotations

from dataclasses import dataclass, field

from mc2p.contracts.common import ContractViolation, require_finite


@dataclass(frozen=True, slots=True)
class BehaviorProfileV0:
    risk_tolerance: float = 0.5
    protectiveness: float = 0.5
    resource_frugality: float = 0.5
    initiative: float = 0.5
    persistence: float = 0.5
    player_proximity: float = 0.5
    speed_over_precision: float = 0.5
    schema_version: str = field(default="mc2p.behavior-profile.v0", init=False)

    def __post_init__(self) -> None:
        for name in (
            "risk_tolerance",
            "protectiveness",
            "resource_frugality",
            "initiative",
            "persistence",
            "player_proximity",
            "speed_over_precision",
        ):
            value = getattr(self, name)
            require_finite(value, name)
            if not 0.0 <= float(value) <= 1.0:
                raise ContractViolation(f"{name} must be within [0, 1]")

