"""Structured task intent contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mc2p.contracts.common import (
    ContractViolation,
    parse_json_object,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)


class ComparisonOperatorV0(StrEnum):
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    EQUAL = "eq"
    GREATER_THAN_OR_EQUAL = "gte"
    GREATER_THAN = "gt"


@dataclass(frozen=True, slots=True)
class SuccessCriterionV0:
    metric: str
    operator: ComparisonOperatorV0
    target_value: float
    unit: str

    def __post_init__(self) -> None:
        require_identifier(self.metric, "criterion metric")
        require_identifier(self.unit, "criterion unit")
        if not isinstance(self.operator, ComparisonOperatorV0):
            raise ContractViolation("criterion operator is invalid")
        require_finite(self.target_value, "criterion target_value")


@dataclass(frozen=True, slots=True)
class TaskIntentV0:
    task_id: str
    task_type: str
    parameters_json: str
    success_criteria: tuple[SuccessCriterionV0, ...]
    priority: int
    deadline_monotonic_ns: int
    interruptible: bool
    max_risk: float
    forbidden_actions: tuple[str, ...] = ()
    schema_version: str = field(default="mc2p.task-intent.v0", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.task_id, "task_id")
        require_identifier(self.task_type, "task_type")
        parse_json_object(self.parameters_json, "task parameters")
        if not self.success_criteria:
            raise ContractViolation("task requires success criteria")
        if not all(
            isinstance(item, SuccessCriterionV0)
            for item in self.success_criteria
        ):
            raise ContractViolation("task success criteria are invalid")
        require_nonnegative_int(self.priority, "task priority")
        require_nonnegative_int(
            self.deadline_monotonic_ns,
            "task deadline_monotonic_ns",
        )
        if self.deadline_monotonic_ns == 0:
            raise ContractViolation("task deadline must be positive")
        if type(self.interruptible) is not bool:
            raise ContractViolation("task interruptible must be bool")
        require_finite(self.max_risk, "max_risk")
        if not 0.0 <= float(self.max_risk) <= 1.0:
            raise ContractViolation("max_risk must be within [0, 1]")
        for action in self.forbidden_actions:
            require_identifier(action, "forbidden action")
        if len(set(self.forbidden_actions)) != len(self.forbidden_actions):
            raise ContractViolation("forbidden actions must be unique")
