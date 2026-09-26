"""Frozen B12-A scene list and evidence verdicts.

This module deliberately owns only the acceptance envelope.  The combat
drivers still produce the commands and raw trace; this layer freezes which
trials must run, where their evidence comes from, and checks the typed online
result against offline replay.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

from scripts.control_probe_core import write_json_atomic


B12A_NEGATIVE_CASES = (
    "gate_rejected",
    "arbitration_deferred",
    "input_failed",
    "confirmation_timeout",
    "health_decline_only",
    "unattributed_death",
    "target_revision_after_submit",
    "observation_interrupted",
    "world_session_change",
    "gate_budget_exhausted",
    "input_budget_exhausted",
    "confirmation_budget_exhausted",
)

# These cases depend on Minecraft observations or the real client gate.  The
# remaining cases inject control-chain failures; running a JVM would not make
# those injected facts more representative.
B12A_FABRIC_NEGATIVE_CASES = (
    "gate_rejected",
    "confirmation_timeout",
    "health_decline_only",
    "unattributed_death",
    "target_revision_after_submit",
)
B12A_RUNTIME_INJECTION_CASES = tuple(
    item for item in B12A_NEGATIVE_CASES
    if item not in B12A_FABRIC_NEGATIVE_CASES
)

_NEGATIVE_EXPECTATIONS = {
    "gate_rejected": ("gate_rejected", None, "complete", 1),
    "arbitration_deferred": ("deferred_by_arbitration", None, "complete", 1),
    "input_failed": ("input_failed", None, "complete", 1),
    "confirmation_timeout": ("confirmation_timeout", None, "complete", 1),
    "health_decline_only": ("confirmation_timeout", None, "complete", 1),
    "unattributed_death": ("target_dead_unattributed", None, "complete", 1),
    "target_revision_after_submit": ("target_revised", None, "complete", 1),
    "observation_interrupted": ("observation_interrupted", None, "complete", 1),
    "world_session_change": ("cancelled", None, "cancelled", 1),
    "gate_budget_exhausted": (
        "gate_rejected", "gate_retry_exhausted", "needs_task_decision", 2,
    ),
    "input_budget_exhausted": (
        "input_failed", "input_retry_exhausted", "needs_task_decision", 2,
    ),
    "confirmation_budget_exhausted": (
        "confirmation_timeout", "confirmation_retry_exhausted",
        "needs_task_decision", 2,
    ),
}


def b12a_trial_plan(world_seed: int) -> tuple[dict, ...]:
    """Return the complete run list before any result is known."""
    if type(world_seed) is not int:
        raise ValueError("B12-A world seed must be an integer")
    rows: list[dict] = []
    ordinal = 0
    directions = ("north", "east", "south", "west")
    moving_ai_seeds = (31001, 31002, 31003, 31004, 31005)
    for scenario in ("fixed_visible", "moving_visible"):
        for repeat in range(1, 21):
            ordinal += 1
            direction = directions[(repeat - 1) // 5]
            direction_repeat = (repeat - 1) % 5 + 1
            rows.append({
                "trial_id": f"positive-{scenario.replace('_', '-')}-{repeat:02d}",
                "classification": "positive",
                "scenario": scenario,
                "repeat": repeat,
                "direction": direction,
                "direction_repeat": direction_repeat,
                "world_seed": world_seed,
                "scenario_seed": world_seed * 1000 + ordinal,
                "ai_seed": (
                    moving_ai_seeds[direction_repeat - 1]
                    if scenario == "moving_visible" else None
                ),
                "injection": None,
                "evidence_source": "fabric",
            })
    for injection in B12A_NEGATIVE_CASES:
        expected, task_outcome, terminal_state, attempts = _NEGATIVE_EXPECTATIONS[injection]
        for repeat in range(1, 3):
            ordinal += 1
            rows.append({
                "trial_id": f"negative-{injection.replace('_', '-')}-{repeat:02d}",
                "classification": "negative",
                "scenario": "fixed_visible",
                "repeat": repeat,
                "world_seed": world_seed,
                "scenario_seed": world_seed * 1000 + ordinal,
                "injection": injection,
                "evidence_source": (
                    "fabric" if injection in B12A_FABRIC_NEGATIVE_CASES
                    else "deterministic_runtime"
                ),
                "expected_attempt_outcome": expected,
                "expected_attempt_count": attempts,
                "expected_task_outcome": task_outcome,
                "expected_terminal_state": terminal_state,
            })
    return tuple(rows)


def write_b12a_manifest(
    directory: Path,
    *,
    world_seed: int,
    code_hashes: dict[str, str],
) -> Path:
    if (type(code_hashes) is not dict
            or any(type(key) is not str or type(value) is not str
                   for key, value in code_hashes.items())):
        raise ValueError("B12-A code hashes must be a string mapping")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "b12a-attack-evidence-manifest.json"
    write_json_atomic(path, {
        "schema_version": "mc2p.b12a-manifest.v1",
        "world_seed": world_seed,
        "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(b12a_trial_plan(world_seed)),
    })
    return path


def _string_list(evidence: Mapping, field: str) -> tuple[str, ...] | None:
    value = evidence.get(field)
    if (not isinstance(value, list)
            or any(type(item) is not str for item in value)):
        return None
    return tuple(value)


def evaluate_b12a_trial(trial: Mapping, evidence: Mapping) -> tuple[str, ...]:
    """Return stable failure codes; an empty tuple means the trial passed."""
    failures: list[str] = []
    if evidence.get("fixture_valid") is not True:
        return ("fixture_invalid",)
    if evidence.get("runtime_state") != "ready":
        failures.append("runtime_not_ready")

    online = _string_list(evidence, "online_outcomes")
    replayed = _string_list(evidence, "replayed_outcomes")
    if online is None or replayed is None:
        failures.append("attempt_outcomes_missing")
        online = () if online is None else online
        replayed = () if replayed is None else replayed
    elif online != replayed:
        failures.append("online_replay_mismatch")

    classification = trial.get("classification")
    if classification == "positive":
        required_hits = 1 if trial.get("scenario") == "fixed_visible" else 2
        confirmed_hits = sum(
            outcome in {"command_correlated_hit", "source_confirmed_hit"}
            for outcome in online
        )
        if confirmed_hits < required_hits:
            failures.append("missing_confirmed_hit")
        if evidence.get("engagement_grants", 0) < required_hits:
            failures.append("missing_correlated_engagement_grant")
        if evidence.get("task_outcome") is not None:
            failures.append("unexpected_task_outcome")
        if evidence.get("terminal_state") != "complete":
            failures.append("positive_task_not_complete")
        return tuple(failures)

    if classification != "negative":
        return tuple(failures + ["unknown_trial_classification"])
    injection = trial.get("injection")
    if injection not in _NEGATIVE_EXPECTATIONS:
        return tuple(failures + ["unknown_negative_injection"])
    expected, expected_task, expected_terminal, expected_count = _NEGATIVE_EXPECTATIONS[injection]
    if online != (expected,) * expected_count:
        failures.append("unexpected_attempt_outcome")
    if evidence.get("task_outcome") != expected_task:
        failures.append("unexpected_task_outcome")
    if evidence.get("terminal_state") != expected_terminal:
        failures.append("unexpected_terminal_state")
    if evidence.get("engagement_grants", 0) != 0:
        failures.append("unexpected_engagement_grant")
    if (injection == "health_decline_only"
            and evidence.get("unattributed_damage_facts", 0) < 1):
        failures.append("unattributed_damage_fact_missing")
    return tuple(failures)


def summarize_b12a_trials(rows: Iterable[Mapping]) -> dict:
    """Keep valid reachable failures in the denominator; isolate bad fixtures."""
    rows = tuple(rows)
    valid = tuple(row for row in rows if row.get("fixture_valid") is True)
    positives = tuple(row for row in valid if row.get("classification") == "positive")
    negatives = tuple(row for row in valid if row.get("classification") == "negative")
    outcomes = Counter(
        outcome
        for row in valid
        for outcome in row.get("online_outcomes", ())
        if type(outcome) is str
    )
    negative_groups = Counter(
        row.get("injection") for row in negatives if type(row.get("injection")) is str
    )
    return {
        "schema_version": "mc2p.b12a-summary.v1",
        "planned_trials": len(rows),
        "valid_trials": len(valid),
        "fixture_invalid": len(rows) - len(valid),
        "positive_passed": sum(row.get("passed") is True for row in positives),
        "positive_total": len(positives),
        "positive_timeouts": sum(row.get("terminal_state") == "timeout"
                                 for row in positives),
        "negative_passed": sum(row.get("passed") is True for row in negatives),
        "negative_total": len(negatives),
        "attempt_outcomes": dict(sorted(outcomes.items())),
        "negative_groups": dict(sorted(negative_groups.items())),
    }
