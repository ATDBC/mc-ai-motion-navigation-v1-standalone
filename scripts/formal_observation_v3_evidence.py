"""Strict admission gate for newly recorded formal Observation V3 evidence."""
from __future__ import annotations

from scripts.navigation_motion_evidence import restore_snapshot
from scripts.smoke_test_player_runtime import _formal_observation_violations


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
        violations.extend(_formal_observation_violations(raw, path=path))
        try:
            restored = restore_snapshot(raw)
            if restored.schema_version != "mc2p.observation.v3":
                violations.append(f"{path}: restored snapshot is not V3")
        except (TypeError, ValueError) as error:
            violations.append(f"{path}: invalid formal V3 snapshot ({error})")
    return violations
