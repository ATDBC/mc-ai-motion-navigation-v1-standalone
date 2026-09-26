"""Strict admission gate for newly recorded formal Observation V3 evidence."""
from __future__ import annotations

from collections.abc import Mapping
import re

from scripts.formal_observation_v3_trace import restore_formal_surface_observation_v3


_IMAGE_KEY_TOKENS = (
    "pov", "rgb", "image", "frame", "pixel", "texture", "screenshot",
)


def formal_observation_violations(value: object, *, path: str) -> list[str]:
    """Reject image-bearing and binary projections in formal evidence."""
    violations: list[str] = []
    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        if {"byte_length", "sha256"}.issubset(keys):
            violations.append(f"{path}: binary trace projection")
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold())
            if any(token in normalized for token in _IMAGE_KEY_TOKENS):
                violations.append(f"{path}.{key}: image-bearing key")
            violations.extend(
                formal_observation_violations(child, path=f"{path}.{key}"),
            )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            violations.extend(
                formal_observation_violations(child, path=f"{path}[{index}]"),
            )
    return violations


def validate_formal_observations_v3(observations: list[dict]) -> list[str]:
    """Return stable, path-qualified violations; never reinterpret V2 as V3."""
    if type(observations) is not list:
        return ["observations: expected list"]
    if not observations:
        return ["observations: empty formal evidence"]
    violations: list[str] = []
    for index, raw in enumerate(observations):
        path = f"observations[{index}]"
        if type(raw) is not dict:
            violations.append(f"{path}: expected object")
            continue
        if raw.get("schema_version") != "mc2p.observation.v3":
            violations.append(f"{path}.schema_version: expected mc2p.observation.v3")
        if raw.get("privileged_fields_present") != []:
            violations.append(f"{path}.privileged_fields_present: formal actor evidence must be empty")
        violations.extend(formal_observation_violations(raw, path=path))
        try:
            restored = restore_formal_surface_observation_v3(raw)
            if restored.schema_version != "mc2p.observation.v3":
                violations.append(f"{path}: restored snapshot is not V3")
        except (TypeError, ValueError) as error:
            violations.append(f"{path}: invalid formal V3 snapshot ({error})")
    return violations
