"""Common validation and explicit field-state primitives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
import re
from typing import Any, Generic, TypeVar


T = TypeVar("T")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class ContractViolation(ValueError):
    """Raised when a public V0 value violates its declared semantics."""


class FieldStatusV0(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FieldValueV0(Generic[T]):
    status: FieldStatusV0
    value: T | None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is FieldStatusV0.VALID:
            if self.value is None:
                raise ContractViolation("valid field must carry a value")
            if self.detail is not None:
                raise ContractViolation("valid field detail must be null")
            return
        if self.value is not None:
            raise ContractViolation("non-valid field must not carry a value")
        if self.detail is None or not self.detail.strip():
            raise ContractViolation("non-valid field requires non-empty detail")

    @classmethod
    def valid(cls, value: T) -> FieldValueV0[T]:
        return cls(FieldStatusV0.VALID, value)

    @classmethod
    def missing(cls, detail: str) -> FieldValueV0[T]:
        return cls(FieldStatusV0.MISSING, None, detail)

    @classmethod
    def unsupported(cls, detail: str) -> FieldValueV0[T]:
        return cls(FieldStatusV0.UNSUPPORTED, None, detail)

    @classmethod
    def stale(cls, detail: str) -> FieldValueV0[T]:
        return cls(FieldStatusV0.STALE, None, detail)

    @classmethod
    def invalid(cls, detail: str) -> FieldValueV0[T]:
        return cls(FieldStatusV0.INVALID, None, detail)


def require_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ContractViolation(f"{name} must be a non-empty identifier")


def require_field_name(value: str, name: str) -> None:
    if not isinstance(value, str) or not _FIELD_NAME_PATTERN.fullmatch(value):
        raise ContractViolation(f"{name} must contain field names only")


def require_nonnegative_int(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ContractViolation(f"{name} must be a nonnegative integer")


def require_finite(value: float, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")


def parse_json_object(
    text: str,
    name: str,
    *,
    forbidden_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ContractViolation(f"{name} must be a JSON object string")
    try:
        value = json.loads(
            text,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(constant)
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ContractViolation(
            f"{name} must contain finite valid JSON values"
        ) from error
    if not isinstance(value, dict):
        raise ContractViolation(f"{name} must be a JSON object")

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ContractViolation(f"{name} keys must be strings")
                if key.casefold() in forbidden_keys:
                    raise ContractViolation(f"{name} contains forbidden key: {key}")
                check(nested)
        elif isinstance(item, list):
            for nested in item:
                check(nested)
        elif type(item) is float and not math.isfinite(item):
            raise ContractViolation(f"{name} values must be finite")

    check(value)
    return value
