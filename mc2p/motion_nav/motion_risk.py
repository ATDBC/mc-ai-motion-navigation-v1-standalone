"""Task-scoped damage limits used by motion solving and admission."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_identifier


MOVEMENT_DAMAGE_BUDGET_RESOURCE = "movement_damage_budget_points"


def _finite_nonnegative(value: float, label: str) -> float:
    if (type(value) not in (int, float)
            or not math.isfinite(float(value))
            or float(value) < 0.0):
        raise ContractViolation(f"{label} must be finite and nonnegative")
    return float(value)


@dataclass(frozen=True, slots=True)
class TaskDamageBudget:
    """Damage one task authorizes a motion candidate to expect."""

    risk_policy_id: str = "no_expected_damage"
    maximum_expected_damage_points: float = 0.0

    def __post_init__(self) -> None:
        require_identifier(self.risk_policy_id, "damage risk policy id")
        object.__setattr__(
            self,
            "maximum_expected_damage_points",
            _finite_nonnegative(
                self.maximum_expected_damage_points,
                "maximum expected damage",
            ),
        )

    def allows(
        self,
        predicted_damage_points: float,
        *,
        health_points: float | None,
        absorption_points: float | None,
    ) -> bool:
        predicted = _finite_nonnegative(
            predicted_damage_points, "predicted damage",
        )
        if predicted > self.maximum_expected_damage_points:
            return False
        if predicted == 0.0:
            return True
        if health_points is None or absorption_points is None:
            return False
        health = _finite_nonnegative(health_points, "health points")
        absorption = _finite_nonnegative(absorption_points, "absorption points")
        return predicted < health + absorption


def conservative_plain_fall_damage_points(fall_distance_blocks: float) -> float:
    """Return the conservative vanilla bound for an ordinary block landing."""

    distance = _finite_nonnegative(fall_distance_blocks, "fall distance")
    return float(max(0, math.ceil(distance - 3.0)))
