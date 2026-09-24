"""Independent, bounded NB2 evaluator for a single normal-navigation run.

Required input entries (no substitutes): ``fixture/`` containing a complete fixture,
``run-manifest.json``, ``initial-state.json``, sealed segmented ``trajectory/``,
``terminal.json``, ``cleanup.json``, and ``permission-audit.json``.  The fixture is
natively reverified.  The trajectory has separate ``sample`` and ``execution`` rows:
sample identity/JVM interval is never conflated with controller request/receive time, while
requested/selected controls are kept apart from the action in a matching execution
receipt.  NB5 may strengthen that receipt against raw Runtime traces; NB2 never treats
an actor claim, predicted collision, or an unsealed/missing segment as run evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from dataclasses import asdict

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1, LookV1, MovementV1
from mc2p.backends.deployment_transport import ClientProcessIdentity
from mc2p.contracts.intent_source import IntentSourceV1, ordered_intent_id
from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from mc2p.skills.normal_navigation_types import NormalNavigationConfig
from mc2p.skills.navigation_strategy import GOAL_DIRECTED_EXPLORATION, JOINT_STRATEGIES, strategy_config
from scripts.normal_navigation_fixture import (
    _body_clear,
    _route_exists,
    _supported,
    case_plan,
    verify_normal_fixture,
)
from scripts.client_behavior_probe_support import sandbox_provenance
from scripts.fabric_deployment_launch import inspect_launch, verify_assets
from scripts.probe_fabric_deployment_observation import validate_connection_evidence
from scripts.visibility_fixture_world import _no_links


MAX_JSON_BYTES = 1024 * 1024
MAX_TRAJECTORY_RECORDS = 200_000
MAX_SAMPLE_GAP_NS = 500_000_000
ARRIVAL_HOLD_NS = 300_000_000
REQUIRED_ENTRIES = frozenset({
    "fixture", "source-archive", "runtime-trace", "run-manifest.json",
    "initial-state.json", "trajectory", "terminal.json", "cleanup.json",
    "permission-audit.json", "supervision.json", "owned",
})
MIN_SOURCE_FILES = 212
MAX_SOURCE_FILES = 512
SENSOR_CONTRACT = {
    "sensor_profile_revision": 3,
    "horizontal_fov_degrees": 120.0,
    "vertical_fov_degrees": 120.0,
    "ray_columns": 159,
    "ray_rows": 9,
    "max_block_distance": 16.0,
    "body_contact_tolerance_blocks": 0.05,
}
ACTION_KEYS = {"forward", "strafe", "sprint", "sneak", "jump"}
DRIVER_RELEASE_REASONS = frozenset({
    "normal_benchmark_complete", "point_goal_deadline",
    "owner_lease_insufficient", "action_lease_expired_before_dispatch",
    "owner_heartbeat_lost",
})
RECOVERY_RELEASE_REASONS=frozenset({'navigation_recovery_problem_deadline',
    'navigation_recovery_attempts_exhausted','navigation_recovery_task_deadline'})
SUMMARY_OUTCOMES = frozenset({
    "success", "timeout", "pit_entry", "algorithm_error", "running",
    "unavailable", "blocked",
})
SUMMARY_HISTORY_AGE_NS = {
    "H1": 200_000_000,
    "H2_2s": 2_000_000_000,
    "H2_10s": 10_000_000_000,
}
# R6 freezes independent refusal categories: 64 cell plus 64 segment entries.
MAX_PLANNING_CELL_REJECTIONS = 64
MAX_PLANNING_SEGMENT_REJECTIONS = 64


def trial_outcome(*, arrived: bool, pit_entry: bool, expired: bool, algorithm_error: bool) -> str:
    if any(type(value) is not bool for value in (arrived, pit_entry, expired, algorithm_error)):
        raise ValueError("trial outcome inputs must be booleans")
    if algorithm_error:
        return "algorithm_error"
    if pit_entry:
        return "pit_entry"
    if arrived:
        return "success"
    if expired:
        return "timeout"
    return "running"


def recovery_outcome(outcome,terminal,*,runtime_validated):
    """Keep the historical terminal failure field; distinguish a proved blockage."""
    if (runtime_validated and outcome=='algorithm_error' and terminal['driver_state']=='stopped'
            and terminal['driver_reason'] in RECOVERY_RELEASE_REASONS):return 'blocked'
    return outcome


def validate_recovery_stop(run,reason,planning,action):
    """Require the final neutral decision to justify the exact revision-5 limit."""
    from scripts.joint_planning_evidence import validate_execution_diagnostic
    config=run.get('joint_configuration',{})
    diag=planning.get('planning_diagnostic',{});execution=diag.get('execution',{})
    if (run.get('group')!=GOAL_DIRECTED_EXPLORATION or reason not in RECOVERY_RELEASE_REASONS
            or type(config.get('execution_recovery_revision')) is not int or config['execution_recovery_revision']!=1
            or type(config.get('terrain_history_revision')) is not int or config['terrain_history_revision']!=1
            or config.get('stage_goals_enabled') is not True or diag.get('joint_revision')!=5
            or planning.get('reason')!=reason or planning.get('state')!='blocked'
            or diag.get('selected_candidate_id') is not None or diag.get('proposal') is not None
            or diag.get('planned') is not False or execution.get('phase')!='blocked'
            or execution.get('deadline_reason')!=reason.removeprefix('navigation_recovery_')
            or action.get('movement')!=asdict(MovementV1()) or action.get('look')!=asdict(LookV1())
            or action.get('operation') is not None):
        raise ValueError('recovery release lacks its exact bound neutral exhaustion')
    started=execution.get('started_ns');elapsed=execution.get('task_elapsed_ns')
    if type(started) is not int or type(elapsed) is not int:
        raise ValueError('recovery release lacks execution timing')
    validate_execution_diagnostic(execution,started+elapsed)


def paired_summary(runs: tuple[dict, ...]) -> dict:
    """Describe explicitly-cohorted raw-evidence bundles without causal claims.

    Every input has exactly ``cohort``, ``source_tree_sha256``, ``evaluation``,
    ``run_manifest``, ``initial_state``, ``terminal`` and
    ``task_start_raw_observation``.  The last four fields may be ``None`` only
    for an engineering-invalid attempt whose raw evidence is unavailable.
    Complete bundles carry the unmodified JSON objects from the run directory;
    ``task_start_raw_observation`` is the bound raw Runtime observation from
    which this function derives the physical position, yaw/pitch, ground state
    and finite three-axis velocity.  A caller-supplied pose or scalar speed is
    intentionally not part of the schema.

    Absolute clock/session identifiers are validated against the initial-state
    projection but excluded from matching.  Pairs require exact cohort, source,
    case/seed/layout/goal, task-start pose, history treatment, backend, sensor
    and control profile.  Results are descriptive only: significance is always
    absent, and different groups yield observed deltas rather than a claimed
    counterfactual route.
    """
    if type(runs) is not tuple:
        raise ValueError("paired summary requires an immutable run tuple")
    normalized = [_paired_summary_run(run) for run in runs]
    cohort_names = sorted({item["cohort"] for item in normalized})
    for name in cohort_names:
        trees = {item["source_tree_sha256"] for item in normalized
                 if item["cohort"] == name}
        if len(trees) != 1:
            raise ValueError("declared paired cohort contains multiple source trees")
    cohorts = [_paired_cohort_summary(
        name, [item for item in normalized if item["cohort"] == name]
    ) for name in cohort_names]
    success_count = sum(item["engineering_status"] == "valid"
                        and item["algorithm_outcome"] == "success"
                        for item in normalized)
    success_rate = (None if not normalized or len(cohorts) != 1
                    else success_count / len(normalized))
    return {
        "schema_version": "mc2p.normal-navigation-paired-summary.v2",
        "sample_count": len(normalized),
        "verified_success_count": success_count,
        "success_rate": success_rate,
        "success_rate_reason": ("empty_data" if not normalized else
                                "multiple_cohorts_not_pooled"
                                if len(cohorts) != 1 else None),
        "cohorts": cohorts,
        "significance": None,
        "significance_reason": "descriptive_baseline_only",
    }


def _paired_summary_run(value: dict) -> dict:
    keys = {"cohort", "source_tree_sha256", "evaluation", "run_manifest",
            "initial_state", "terminal", "task_start_raw_observation"}
    _exact(value, keys, "paired summary run")
    cohort = _identifier(value["cohort"], "paired cohort")
    source_tree = value["source_tree_sha256"]
    if (type(source_tree) is not str or len(source_tree) != 64
            or re.fullmatch(r"[0-9a-f]{64}", source_tree) is None):
        raise ValueError("invalid paired source tree")
    evaluation = value["evaluation"]
    if type(evaluation) is not dict:
        raise ValueError("invalid paired evaluation")
    if evaluation.get("schema_version") != "mc2p.normal-navigation-evidence.v1":
        raise ValueError("paired evaluation schema differs")
    engineering = evaluation.get("engineering_status")
    outcome = evaluation.get("algorithm_outcome")
    directory = evaluation.get("directory")
    if (engineering not in {"valid", "invalid_run"}
            or outcome not in SUMMARY_OUTCOMES
            or not isinstance(directory, str) or not directory):
        raise ValueError("paired evaluation status is invalid")

    raw_values = (value["run_manifest"], value["initial_state"],
                  value["terminal"], value["task_start_raw_observation"])
    if any(item is None for item in raw_values):
        if engineering == "valid" or not all(item is None for item in raw_values):
            raise ValueError("paired raw evidence is partially unavailable")
        return {
            "cohort": cohort, "source_tree_sha256": source_tree,
            "engineering_status": engineering, "algorithm_outcome": outcome,
            "directory": directory, "group": None, "pair_key": None,
            "task_duration_ns": None, "episode_limit_ns": None,
            "censored_task_duration_ns": None,
            "pre_observation_duration_ns": None, "waiting_duration_ns": None,
            "preparation_duration_ns": None, "total_duration_ns": None,
            "scoring_unavailable_reason": "raw_evidence_unavailable",
        }

    run, initial, terminal, raw = raw_values
    if any(type(item) is not dict for item in raw_values):
        raise ValueError("invalid paired raw evidence")
    plan = run.get("case_plan")
    if type(plan) is not dict:
        raise ValueError("paired case plan is absent")
    case, seed = plan.get("case"), plan.get("seed")
    actor_task, budget = plan.get("actor_task"), plan.get("budget_ns")
    if (not isinstance(case, str) or type(seed) is not int
            or type(actor_task) is not dict
            or set(actor_task) != {"goal_id", "position"}
            or not isinstance(actor_task["goal_id"], str)
            or not actor_task["goal_id"]
            or _integer(budget, "paired episode budget", minimum=1) < 1):
        raise ValueError("invalid paired case/goal/budget")
    goal_position = _position(actor_task["position"], "paired goal position")
    layout = run.get("layout_sha256")
    if (type(layout) is not str or re.fullmatch(r"[0-9a-f]{64}", layout) is None):
        raise ValueError("invalid paired layout hash")
    history = run.get("history")
    backend = run.get("backend")
    group = run.get("group")
    control = run.get("control_profile")
    if (history not in {"H0", *SUMMARY_HISTORY_AGE_NS}
            or backend not in {"standalone", "craftground"}
            or group not in {"A", "B", "C", *JOINT_STRATEGIES}
            or control != ('normal_decoupled_v1' if group in JOINT_STRATEGIES else 'forward_stop_v1')):
        raise ValueError("invalid paired treatment/control dimensions")
    archive = run.get("source_archive")
    if type(archive) is not dict or archive.get("tree_sha256") != source_tree:
        raise ValueError("paired source cohort differs from run manifest")
    clock = run.get("controller_clock")
    if type(clock) is not dict:
        raise ValueError("paired controller clock is absent")
    started = _integer(clock.get("started_at_ns"), "paired task start", minimum=1)
    ended = _integer(terminal.get("ended_at_ns"), "paired task end", minimum=started)

    task_start = initial.get("task_start")
    treatment = initial.get("history_treatment")
    if type(task_start) is not dict or type(treatment) is not dict:
        raise ValueError("paired initial evidence is absent")
    if _raw_sample(raw) != _validate_sample(task_start):
        raise ValueError("paired task-start observation is not raw-bound")
    own = raw.get("self_state", {}).get("value")
    if type(own) is not dict:
        raise ValueError("paired raw task-start body is absent")
    velocity = own.get("velocity")
    if type(velocity) is not dict or set(velocity) != {"x", "y", "z"}:
        raise ValueError("paired raw task-start velocity is absent")
    body = (
        tuple(_position([own["position"][axis] for axis in ("x", "y", "z")],
                        "paired task-start position")),
        _number(own.get("yaw_degrees"), "paired task-start yaw"),
        _number(own.get("pitch_degrees"), "paired task-start pitch"),
        own.get("is_on_ground"),
        tuple(_number(velocity[axis], "paired task-start velocity")
              for axis in ("x", "y", "z")),
    )
    if type(body[3]) is not bool:
        raise ValueError("invalid paired task-start ground state")
    preparation = _paired_history_timing(treatment, history, started,
                                         task_start["received_at_ns"])
    sensor = run.get("sensor_contract")
    sensor_key = json.dumps(sensor, sort_keys=True, separators=(",", ":"),
                            allow_nan=False)
    history_key = (history, preparation["concerned_positions"])
    configuration=run.get('joint_configuration')
    legacy_stage=group in {'D','E'} and isinstance(configuration,dict) and configuration.get('stage_goals_enabled') is True
    full_configuration=configuration
    strategy_configuration={}
    # Compare whole strategies under common conditions. Preserve intrinsic
    # revisions as treatment metadata; unknown fields still prevent matching.
    if group==GOAL_DIRECTED_EXPLORATION:
        if not isinstance(configuration,dict) or configuration.get('stage_goals_enabled') is not True:
            raise ValueError('named exploration summary lacks stage binding')
        revisions={'terrain_history_revision':1,'execution_recovery_revision':1,
                   'execution_cost_revision':1,'motion_review_revision':2}
        for key,expected in revisions.items():
            if key in configuration and (type(configuration[key]) is not int or configuration[key]!=expected):
                raise ValueError('unsupported named exploration summary revision: '+key)
        if ('execution_recovery_revision' in configuration
                and configuration.get('terrain_history_revision')!=1):
            raise ValueError('execution recovery summary lacks terrain revision')
        if {'execution_cost_revision','motion_review_revision'} & set(configuration):
            if any(configuration.get(key)!=expected for key,expected in revisions.items()):
                raise ValueError('execution cost summary lacks complete revision binding')
        intrinsic={'stage_goals_enabled',*revisions}
        strategy_configuration={k:v for k,v in configuration.items() if k in intrinsic}
        configuration={k:v for k,v in configuration.items() if k not in intrinsic}
    pair_key = (
        source_tree, case, seed, layout,
        (actor_task["goal_id"], tuple(goal_position)), body, history_key,
        backend, control, sensor_key, budget,
        json.dumps(configuration,sort_keys=True,separators=(',',':')),
    )
    task_duration = ended - started
    if engineering == "valid" and outcome == "success":
        if task_duration > budget:
            raise ValueError("successful paired task exceeded episode budget")
        censored = task_duration
    else:
        censored = budget
    scoring_reason = None if engineering == "valid" else "engineering_invalid"
    total = preparation["preparation_duration_ns"] + task_duration
    censored_total = preparation["preparation_duration_ns"] + censored
    return {
        "cohort": cohort, "source_tree_sha256": source_tree,
        "engineering_status": engineering, "algorithm_outcome": outcome,
        "directory": directory, "group": group, "pair_key": pair_key,
        "legacy_stage_entry": legacy_stage,
        "joint_configuration": full_configuration,
        "strategy_configuration": strategy_configuration,
        "task_duration_ns": task_duration, "episode_limit_ns": budget,
        "censored_task_duration_ns": censored,
        "pre_observation_duration_ns": preparation["pre_observation_duration_ns"],
        "waiting_duration_ns": preparation["waiting_duration_ns"],
        "preparation_duration_ns": preparation["preparation_duration_ns"],
        "total_duration_ns": total,
        "censored_total_duration_ns": censored_total,
        "scoring_unavailable_reason": scoring_reason,
    }


def _paired_history_timing(treatment: dict, history: str, started: int,
                           task_start_received: int) -> dict:
    _exact(treatment, {"history", "preparation", "concerned_blocks"},
           "paired history treatment")
    if treatment["history"] != history:
        raise ValueError("paired history label differs")
    rows, concerned = treatment["preparation"], treatment["concerned_blocks"]
    if history == "H0":
        if rows != [] or concerned != []:
            raise ValueError("paired H0 has undeclared preparation")
        return {"pre_observation_duration_ns": 0, "waiting_duration_ns": 0,
                "preparation_duration_ns": 0, "concerned_positions": ()}
    if type(rows) is not list or not rows or type(concerned) is not list or not concerned:
        raise ValueError("paired history evidence is absent")
    previous_received = -1
    for sequence, row in enumerate(rows, 1):
        if type(row) is not dict:
            raise ValueError("invalid paired preparation row")
        submitted = _integer(row.get("submitted_at_ns"), "paired preparation submit")
        received = _integer(row.get("received_at_ns"), "paired preparation receive")
        if (row.get("sequence") != sequence or submitted > received
                or received < previous_received or received > started):
            raise ValueError("paired preparation chronology is invalid")
        previous_received = received
    last_received = rows[-1]["received_at_ns"]
    if last_received != task_start_received:
        raise ValueError("paired preparation does not end at task-start sample")
    target_age = SUMMARY_HISTORY_AGE_NS[history]
    positions = []
    original_requests = []
    original_receives = []
    for item in concerned:
        if type(item) is not dict:
            raise ValueError("invalid paired concerned history block")
        position = tuple(_position(item.get("position"), "paired concerned block"))
        original_request = _integer(item.get("original_request_start_ns"),
                                    "paired history original request")
        original_received = _integer(item.get("original_received_at_ns"),
                                     "paired history original receive")
        age = _integer(item.get("task_start_age_ns"), "paired history age")
        actual_minimum_age = task_start_received - original_request
        if (original_request > original_received or original_received > task_start_received
                or age < target_age or age < actual_minimum_age
                or age > started - original_request):
            raise ValueError("paired history age is future or falsely fresh")
        positions.append(position)
        original_requests.append(original_request)
        original_receives.append(original_received)
    first_request = min(original_requests)
    last_source_receive = max(original_receives)
    return {
        "pre_observation_duration_ns": last_source_receive - first_request,
        "waiting_duration_ns": last_received - last_source_receive,
        "preparation_duration_ns": last_received - first_request,
        "concerned_positions": tuple(sorted(positions)),
    }


def _mean_or_none(values: list[int | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(values) / len(values)


def _paired_cohort_summary(name: str, runs: list[dict]) -> dict:
    outcomes = {}
    for run in runs:
        outcome = run["algorithm_outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    success = sum(run["engineering_status"] == "valid"
                  and run["algorithm_outcome"] == "success" for run in runs)
    failures = [{"directory": run["directory"], "group": run["group"],
                 "algorithm_outcome": run["algorithm_outcome"]}
                for run in runs if run["algorithm_outcome"] != "success"]
    invalid = [{"directory": run["directory"],
                "algorithm_outcome": run["algorithm_outcome"]}
               for run in runs if run["engineering_status"] != "valid"]
    unavailable = [{"directory": run["directory"],
                    "reason": run["scoring_unavailable_reason"]}
                   for run in runs if run["scoring_unavailable_reason"] is not None]
    scoreable = [run for run in runs if run["scoring_unavailable_reason"] is None]
    attempt_fields = (
        "directory", "group", "engineering_status", "algorithm_outcome",
        "task_duration_ns", "episode_limit_ns", "censored_task_duration_ns",
        "pre_observation_duration_ns", "waiting_duration_ns",
        "preparation_duration_ns", "total_duration_ns",
        "censored_total_duration_ns", "scoring_unavailable_reason",
    )
    attempts = []
    for run in runs:
        attempt = {field: run.get(field) for field in attempt_fields}
        if run.get('joint_configuration') is not None:
            attempt['joint_configuration']=run['joint_configuration']
            attempt['strategy_configuration']=run['strategy_configuration']
        attempt["scoring_available"] = (
            run["scoring_unavailable_reason"] is None)
        attempts.append(attempt)
    by_key = {}
    pairs, unpaired = [], []
    for run in scoreable:
        if run.get('legacy_stage_entry'):
            unpaired.append(dict(directory=run['directory'],reason='legacy_stage_entry_not_strategy_comparison'))
            continue
        by_key.setdefault(run["pair_key"], []).append(run)
    for same_key in by_key.values():
        by_group = {}
        for run in same_key:
            by_group.setdefault(run["group"], []).append(run)
        if any(len(group_runs) != 1 for group_runs in by_group.values()):
            unpaired.extend({"directory": run["directory"],
                             "reason": "duplicate_group_for_exact_match"}
                            for run in same_key)
            continue
        groups = sorted(by_group)
        if len(groups) < 2:
            run = same_key[0]
            unpaired.append({"directory": run["directory"],
                             "reason": "no_exact_match"})
            continue
        for left_index, left_group in enumerate(groups[:-1]):
            for right_group in groups[left_index + 1:]:
                left, right = by_group[left_group][0], by_group[right_group][0]
                pairs.append({
                    "comparison": right_group + "-" + left_group,
                    "left_directory": left["directory"],
                    "right_directory": right["directory"],
                    "success_delta": int(right["algorithm_outcome"] == "success")
                                     - int(left["algorithm_outcome"] == "success"),
                    "censored_task_duration_delta_ns":
                        right["censored_task_duration_ns"] - left["censored_task_duration_ns"],
                    "pre_observation_duration_delta_ns":
                        right["pre_observation_duration_ns"] - left["pre_observation_duration_ns"],
                    "waiting_duration_delta_ns":
                        right["waiting_duration_ns"] - left["waiting_duration_ns"],
                    "preparation_duration_delta_ns":
                        right["preparation_duration_ns"] - left["preparation_duration_ns"],
                    "total_duration_delta_ns":
                        right["total_duration_ns"] - left["total_duration_ns"],
                    "censored_total_duration_delta_ns":
                        right["censored_total_duration_ns"]
                        - left["censored_total_duration_ns"],
                })
    paired_directories = {pair[key] for pair in pairs
                          for key in ("left_directory", "right_directory")}
    for run in scoreable:
        if (run["directory"] not in paired_directories
                and not any(item["directory"] == run["directory"]
                            for item in unpaired)):
            unpaired.append({"directory": run["directory"],
                             "reason": "no_exact_match"})
    unpaired.extend({"directory": run["directory"],
                     "reason": run["scoring_unavailable_reason"]}
                    for run in runs if run["scoring_unavailable_reason"] is not None)
    success_durations = [run["task_duration_ns"] for run in scoreable
                         if run["engineering_status"] == "valid"
                         and run["algorithm_outcome"] == "success"]
    return {
        "cohort": name,
        "source_tree_sha256": (runs[0]["source_tree_sha256"] if runs else None),
        "sample_count": len(runs), "verified_success_count": success,
        "success_rate": None if not runs else success / len(runs),
        "outcomes": outcomes, "raw_algorithm_outcomes": outcomes,
        "failures": failures,
        "attempts": attempts,
        "engineering_invalid": invalid,
        "scoring_unavailable": unavailable,
        "mean_censored_task_duration_ns": _mean_or_none(
            [run["censored_task_duration_ns"] for run in scoreable]),
        "successful_task_duration_mean_ns": _mean_or_none(success_durations),
        "mean_pre_observation_duration_ns": _mean_or_none(
            [run["pre_observation_duration_ns"] for run in scoreable]),
        "mean_waiting_duration_ns": _mean_or_none(
            [run["waiting_duration_ns"] for run in scoreable]),
        "mean_preparation_duration_ns": _mean_or_none(
            [run["preparation_duration_ns"] for run in scoreable]),
        "mean_total_duration_ns": _mean_or_none(
            [run["total_duration_ns"] for run in scoreable]),
        "mean_censored_total_duration_ns": _mean_or_none(
            [run["censored_total_duration_ns"] for run in scoreable]),
        "pair_count": len(pairs), "pairs": pairs, "unpaired": unpaired,
        "significance": None,
        "significance_reason": "descriptive_baseline_only",
    }


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(path: Path) -> dict:
    _no_links(path)
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"missing or oversized evidence file: {path.name}")
    value = json.loads(path.read_text("utf-8"), object_pairs_hook=_object,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON: " + value)))
    if type(value) is not dict:
        raise ValueError(f"evidence file is not an object: {path.name}")
    return value


def _exact(value: dict, keys: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"invalid {label} fields")


def _integer(value, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2 ** 63 - 1:
        raise ValueError(f"invalid {label}")
    return value


def _identifier(value, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def _number(value, label: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"invalid {label}")
    try:
        finite = math.isfinite(value)
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"invalid {label}") from error
    if not finite:
        raise ValueError(f"invalid {label}")
    return result


def _position(value, label: str) -> list[float]:
    if type(value) is not list or len(value) != 3:
        raise ValueError(f"invalid {label}")
    return [_number(item, label) for item in value]


def _action(value: dict, label: str) -> dict:
    _exact(value, ACTION_KEYS, label)
    if (type(value["forward"]) is not int or type(value["strafe"]) is not int
            or value["forward"] not in (-1, 0, 1) or value["strafe"] not in (-1, 0, 1)):
        raise ValueError(f"invalid {label} axes")
    if any(type(value[key]) is not bool for key in ("sprint", "sneak", "jump")):
        raise ValueError(f"invalid {label} flags")
    return value


def _ordered_source(value: dict, episode_id: str) -> IntentSourceV1:
    _exact(value, {"schema_version", "scope_id", "episode_id", "slot", "generation",
                   "source_id", "label"}, "ordered source")
    if value["schema_version"] != "mc2p.intent-source.v1":
        raise ValueError("ordered source schema differs")
    _identifier(value["label"], "ordered source label", maximum=80)
    source = IntentSourceV1(
        scope_id=value["scope_id"],
        episode_id=value["episode_id"],
        slot=value["slot"],
        generation=value["generation"],
        source_id=value["source_id"],
    )
    if source.episode_id != episode_id:
        raise ValueError("ordered source episode differs from actual initial sample")
    return source


def _normal(action: dict, *, profile: str = 'forward_stop_v1') -> bool:
    permitted = (action["forward"] in (0,1) and action["strafe"] == 0
                 if profile == 'forward_stop_v1' else
                 action["forward"] in (-1,0,1) and action["strafe"] in (-1,0,1)
                 if profile == 'normal_decoupled_v1' else False)
    return (permitted
            and not action["sprint"] and not action["sneak"] and not action["jump"])


def _neutral(action: dict) -> bool:
    return action == {"forward": 0, "strafe": 0, "sprint": False, "sneak": False, "jump": False}


def _pit_entry(layout: dict, position: list[float]) -> bool:
    x, foot_y, z = position
    if foot_y >= layout["standing_y"] - 0.5:
        return False
    low_x, high_x, low_z, high_z = x - 0.3, x + 0.3, z - 0.3, z + 0.3
    return any(low_x < cell["x"] + 1 and high_x > cell["x"]
               and low_z < cell["z"] + 1 and high_z > cell["z"]
               for cell in layout["pit_cells"])


def _arrival_sample(layout: dict, goal: list[float], sample: dict, action: dict | None) -> bool:
    position = sample["position"]
    return (math.hypot(position[0] - goal[0], position[2] - goal[2]) <= 0.75
            and abs(position[1] - goal[1]) <= 0.1
            and sample["on_ground"] is True
            and sample["horizontal_speed_blocks_per_tick"] <= 0.03
            and _supported(layout, position) and _body_clear(layout, position)
            and action is not None and _neutral(action))


def _section(result: dict, name: str, passed: bool, **details) -> None:
    result["evidence"][name] = {"passed": passed, **details}


def _source_tree_digest(fingerprints: dict[str, str]) -> str:
    encoded = json.dumps(fingerprints, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_source_archive(directory: Path, expected: dict[str, str], binding: dict) -> None:
    if (type(expected) is not dict or not MIN_SOURCE_FILES <= len(expected) <= MAX_SOURCE_FILES
            or any(type(name) is not str or not 1 <= len(name) <= 512
                   or type(digest) is not str
                   or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                   for name, digest in expected.items())):
        raise ValueError("source fingerprints are absent, truncated, or invalid")
    manifest = _json(directory / "manifest.json")
    _exact(manifest, {"schema_version", "file_count", "tree_sha256",
                      "source_fingerprints"}, "source archive manifest")
    digest = _source_tree_digest(expected)
    if (manifest != {"schema_version": "mc2p.normal-navigation-source-archive.v1",
                     "file_count": len(expected), "tree_sha256": digest,
                     "source_fingerprints": expected}
            or binding != {"schema_version": "mc2p.normal-navigation-source-archive.v1",
                           "file_count": len(expected), "tree_sha256": digest}):
        raise ValueError("source archive binding differs")
    found = {}
    files = directory / "files"
    _no_links(files)
    if not files.is_dir():
        raise ValueError("source archive files are missing")
    for path in files.rglob("*"):
        _no_links(path)
        if path.is_file():
            name = path.relative_to(files).as_posix()
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if found != expected:
        raise ValueError("source archive content differs")


def _movement(value: dict, label: str) -> dict:
    if type(value) is not dict:
        raise ValueError("invalid " + label)
    movement = value.get("movement", value)
    return _action({key: movement[key] for key in ACTION_KEYS}, label)


def horizontal_speed_blocks_per_tick(velocity_x: float,
                                     velocity_z: float) -> float:
    """Project the two horizontal velocity axes with one canonical rounding."""
    return math.hypot(velocity_x, velocity_z)


def evaluate_control_probe(directory: Path) -> dict:
    """J3 entry point kept beside the established normal-navigation evaluator."""
    from scripts.normal_control_probes import evaluate_control_probe as evaluate
    return evaluate(directory)


def _raw_sample(observation: dict) -> dict:
    own = observation["self_state"]["value"]
    position, velocity = own["position"], own["velocity"]
    client = observation["client_sample"]
    return {
        "episode_id": observation["episode_id"],
        "sequence_id": observation["sequence_id"],
        "request_sequence_id": observation["request_sequence_id"],
        "request_started_at_ns": observation["request_started_at_monotonic_ns"],
        "received_at_ns": observation["received_at_monotonic_ns"],
        "controller_clock_id": observation["controller_clock_id"],
        "client_sample": {"clock_id": client["clock_id"],
            "started_at_ns": client["started_at_monotonic_ns"],
            "completed_at_ns": client["completed_at_monotonic_ns"]},
        "position": [position["x"], position["y"], position["z"]],
        "on_ground": own["is_on_ground"],
        "horizontal_speed_blocks_per_tick": horizontal_speed_blocks_per_tick(
            velocity["x"], velocity["z"]),
    }


def _main_intent(envelope: dict, source: IntentSourceV1, sequence: int,
                 observation_sequence_id: int, controller_ns: int,
                 requested_action: dict) -> None:
    _exact(envelope, {"schema_version", "source", "sequence", "intent"},
           "raw ordered envelope")
    if (envelope["schema_version"] != "mc2p.ordered-intent.v1"
            or envelope["source"] != asdict(source)
            or envelope["sequence"] != sequence):
        raise ValueError("raw ordered envelope identity differs")
    intent = envelope["intent"]
    _exact(intent, {"schema_version", "intent_id", "source_id", "episode_id",
                    "observation_sequence_id", "priority",
                    "submitted_at_monotonic_ns", "expires_at_monotonic_ns",
                    "movement", "look", "operation", "valid_for_ticks",
                    "movement_requires_look"}, "raw ordered intent")
    if (intent["schema_version"] != "mc2p.action-intent.v1"
            or intent["intent_id"] != ordered_intent_id(source, sequence)
            or intent["source_id"] != source.source_id
            or intent["episode_id"] != source.episode_id
            or intent["observation_sequence_id"] != observation_sequence_id
            or intent["submitted_at_monotonic_ns"] != controller_ns
            or intent["operation"] is not None
            or intent["valid_for_ticks"] != 1
            or intent["movement_requires_look"] is not True
            or intent["priority"] != int(ActionPriorityV0.TASK)):
        raise ValueError("raw ordered intent binding differs")
    ActionIntentV1(
        intent_id=intent["intent_id"], source_id=intent["source_id"],
        episode_id=intent["episode_id"],
        observation_sequence_id=intent["observation_sequence_id"],
        priority=ActionPriorityV0(intent["priority"]),
        submitted_at_monotonic_ns=intent["submitted_at_monotonic_ns"],
        expires_at_monotonic_ns=intent["expires_at_monotonic_ns"],
        movement=MovementV1(**intent["movement"]),
        look=LookV1(**intent["look"]), operation=None,
        valid_for_ticks=intent["valid_for_ticks"],
        movement_requires_look=intent["movement_requires_look"],
    )


def _planning_diagnostic(value: dict, group: str, run=None) -> None:
    if group in JOINT_STRATEGIES:
        from scripts.joint_planning_evidence import validate_planning_diagnostic
        validate_planning_diagnostic(value,run)
        return
    keys = {"planned", "expansions", "cells_checked", "candidates",
            "cell_rejections", "segment_rejections", "elapsed_ns",
            "planning_budget_ns", "budget_exhausted", "summary_truncated"}
    _exact(value, keys, "raw planning diagnostic")
    if (type(value["planned"]) is not bool
            or type(value["budget_exhausted"]) is not bool
            or type(value["summary_truncated"]) is not bool):
        raise ValueError("raw planning diagnostic flags are invalid")
    expansions = _integer(value["expansions"], "raw planning expansions")
    cells = _integer(value["cells_checked"], "raw planning cells")
    elapsed = _integer(value["elapsed_ns"], "raw planning elapsed")
    budget = _integer(value["planning_budget_ns"], "raw planning budget", minimum=1)
    config = NormalNavigationConfig(group)
    candidates = value["candidates"]
    cell_rejections = value["cell_rejections"]
    segment_rejections = value["segment_rejections"]
    if (type(candidates) is not list or len(candidates) > config.max_candidates
            or type(cell_rejections) is not list
            or len(cell_rejections) > MAX_PLANNING_CELL_REJECTIONS
            or type(segment_rejections) is not list
            or len(segment_rejections) > MAX_PLANNING_SEGMENT_REJECTIONS
            or expansions > config.max_expansions
            or cells > config.max_terrain
            or budget != config.planning_budget_ns):
        raise ValueError("raw planning diagnostic exceeds bounds")
    for candidate in candidates:
        _exact(candidate, {"cell", "score", "age_cost", "turn_cost"},
               "raw planning candidate")
        cell = candidate["cell"]
        if (type(cell) is not list or len(cell) != 2
                or any(type(axis) is not int for axis in cell)
                or _number(candidate["age_cost"], "raw candidate age") < 0
                or _number(candidate["turn_cost"], "raw candidate turn") < 0):
            raise ValueError("raw planning candidate is invalid")
        _number(candidate["score"], "raw candidate score")
    for rejection in cell_rejections:
        _exact(rejection, {"cell", "reason"}, "raw cell rejection")
        if (type(rejection["cell"]) is not list or len(rejection["cell"]) != 2
                or any(type(axis) is not int for axis in rejection["cell"])
                or not isinstance(rejection["reason"], str)
                or not 1 <= len(rejection["reason"]) <= 80):
            raise ValueError("raw cell rejection is invalid")
    for rejection in segment_rejections:
        _exact(rejection, {"key", "reason"}, "raw segment rejection")
        if (not isinstance(rejection["key"], str)
                or not 1 <= len(rejection["key"]) <= 160
                or not isinstance(rejection["reason"], str)
                or not 1 <= len(rejection["reason"]) <= 80):
            raise ValueError("raw segment rejection is invalid")
    if not value["planned"] and (expansions != 0 or cells != 0 or candidates
                                 or cell_rejections or segment_rejections
                                 or elapsed != 0 or value["budget_exhausted"]
                                 or value["summary_truncated"]):
        raise ValueError("unplanned raw diagnostic retains an old plan")


def _main_planning_event(rows: list[dict], intent_index: int,
                         dispatch_index: int, run: dict,
                         source: IntentSourceV1, sequence: int,
                         observation_sequence: int, intent: dict,
                         attempt_id: str | None) -> str:
    events = [row["payload"] for row in rows[intent_index + 1:dispatch_index]
              if row["record_type"] == "playground_task"]
    if len(events) != 1:
        raise ValueError("raw main step planning event count differs")
    event = events[0]
    _exact(event, {"schema_version", "task_id", "attempt_id", "episode_id",
                   "source_generation", "intent_sequence",
                   "observation_sequence_id", "group", "state", "reason",
                   "movement", "look", "owner_deadline_ns",
                   "selected_waypoint", "planning_diagnostic"},
           "raw main planning event")
    found_attempt = _identifier(event["attempt_id"], "raw planning attempt")
    if attempt_id is not None and found_attempt != attempt_id:
        raise ValueError("raw planning attempt changed")
    if (event["schema_version"] != "mc2p.playground-task-step.v1"
            or event["task_id"] != "point-goal/" + run["case_plan"]["actor_task"]["goal_id"]
            or event["episode_id"] != source.episode_id
            or event["source_generation"] != source.generation
            or event["intent_sequence"] != sequence
            or event["observation_sequence_id"] != observation_sequence
            or event["group"] != run["group"]
            or not isinstance(event["state"], str) or not event["state"]
            or not isinstance(event["reason"], str) or not event["reason"]
            or _movement(event["movement"], "raw planning movement")
                != _movement(intent, "raw planning intent movement")
            or event["look"] != intent["look"]
            or _integer(event["owner_deadline_ns"],
                        "raw planning owner deadline", minimum=1) < 1):
        raise ValueError("raw main planning event binding differs")
    waypoint = event["selected_waypoint"]
    if waypoint is not None:
        _exact(waypoint, {"x", "y", "z"}, "raw selected waypoint")
        for axis in ("x", "y", "z"):
            _number(waypoint[axis], "raw selected waypoint")
    diagnostic = event["planning_diagnostic"]
    _planning_diagnostic(diagnostic, run["group"],run)
    if not diagnostic["planned"] and waypoint is not None:
        raise ValueError("unplanned raw diagnostic retains a selected waypoint")
    return found_attempt


def _neutral_snapshot(action: dict, label: str) -> ActionSnapshotV1:
    _exact(action, {"schema_version", "episode_id", "request_sequence_id",
                    "observation_sequence_id", "deadline_monotonic_ns",
                    "movement", "look", "operation", "valid_for_ticks",
                    "cancel_request_sequence_id"}, label)
    snapshot = ActionSnapshotV1(
        episode_id=action["episode_id"],
        request_sequence_id=action["request_sequence_id"],
        observation_sequence_id=action["observation_sequence_id"],
        deadline_monotonic_ns=action["deadline_monotonic_ns"],
        movement=MovementV1(**action["movement"]),
        look=LookV1(**action["look"]), operation=action["operation"],
        valid_for_ticks=action["valid_for_ticks"],
        cancel_request_sequence_id=action["cancel_request_sequence_id"],
    )
    if (snapshot.movement != MovementV1() or snapshot.look != LookV1()
            or snapshot.operation is not None or snapshot.valid_for_ticks != 1
            or snapshot.cancel_request_sequence_id is not None):
        raise ValueError(label + " is not neutral")
    return snapshot


def _driver_release(rows: list[dict], source: IntentSourceV1,
                    last_step_index: int, source_release_index: int,
                    run: dict, intent_count: int,
                    terminal: dict | None) -> None:
    release_rows = rows[last_step_index + 1:source_release_index]
    if [row["record_type"] for row in release_rows] != [
            "source_cancel", "dispatch", "step", "playground_control_release"]:
        raise ValueError("raw Driver release lifecycle differs")
    cancel, dispatch_row, step_row, event_row = release_rows
    if cancel["payload"] != {"source_id": source.source_id,
                              "removed_intent_ids": []}:
        raise ValueError("raw Driver source cancellation differs")
    dispatch_payload = dispatch_row["payload"]
    decision = dispatch_payload["decision"]
    _exact(decision, {"schema_version", "action", "candidate_intent_ids",
                      "selected_intents", "suppressed_intents"},
           "raw Driver release decision")
    action = _neutral_snapshot(decision["action"], "raw Driver release action")
    if (decision["schema_version"] != "mc2p.arbitration-decision.v1"
            or decision["candidate_intent_ids"] != []
            or decision["selected_intents"] != []
            or decision["suppressed_intents"] != []
            or dispatch_payload.get("task", {}).get("task_type")
                != "point_goal_release"
            or dispatch_payload["task"].get("task_id")
                != "point-goal/" + run["case_plan"]["actor_task"]["goal_id"]):
        raise ValueError("raw Driver release arbitration/task differs")
    step_payload = step_row["payload"]
    if step_payload.get("decision") != decision:
        raise ValueError("raw Driver release dispatch/step decision differs")
    backend = step_payload["backend_result"]
    _exact(backend, {"observation", "receipt", "reward", "terminated",
                     "truncated"}, "raw Driver release backend result")
    receipt = ClientBehaviorReceiptV2.from_mapping(backend["receipt"])
    observation = backend["observation"]
    previous_observation = rows[last_step_index]["payload"]["backend_result"]["observation"]
    if (receipt.status not in {"executed", "confirmed_local"}
            or receipt.reason != "neutral"
            or action.episode_id != previous_observation.get("episode_id")
            or action.observation_sequence_id
                != previous_observation.get("sequence_id")
            or action.episode_id != observation.get("episode_id")
            or action.request_sequence_id
                != observation.get("request_sequence_id")
            or receipt.episode_id != observation.get("episode_id")
            or receipt.generation_id != observation.get("sequence_id")
            or receipt.request_sequence_id
                != observation.get("request_sequence_id")):
        raise ValueError("raw Driver release receipt/observation differs")
    event = event_row["payload"]
    _exact(event, {"schema_version", "attempt_id", "episode_id",
                   "intent_sequence", "observation_sequence_id", "reason",
                   "source_generation", "task_id"},
           "raw Driver release event")
    if (event["schema_version"] != "mc2p.playground-control-release.v1"
            or not isinstance(event["attempt_id"], str) or not event["attempt_id"]
            or event["episode_id"] != source.episode_id
            or event["intent_sequence"] != intent_count
            or event["observation_sequence_id"] != observation.get("sequence_id")
            or event["reason"] not in DRIVER_RELEASE_REASONS | RECOVERY_RELEASE_REASONS
            or event["source_generation"] != source.generation
            or event["task_id"]
                != "point-goal/" + run["case_plan"]["actor_task"]["goal_id"]):
        raise ValueError("raw Driver release event binding differs")
    if event['reason'] in RECOVERY_RELEASE_REASONS:
        planning=next((row['payload'] for row in reversed(rows[:last_step_index])
            if row['record_type']=='playground_task'),None)
        if planning is None:raise ValueError('recovery release lacks its final planning event')
        validate_recovery_stop(run,event['reason'],planning,
            rows[last_step_index]['payload']['decision']['action'])
        envelope=next((row['payload']['envelope'] for row in reversed(rows[:last_step_index])
            if row['record_type']=='ordered_intent' and row['payload']['envelope']['source']['source_id']==source.source_id),None)
        execution=planning['planning_diagnostic']['execution']
        if (envelope is None or execution['started_ns']+execution['task_elapsed_ns']
                !=envelope['intent']['submitted_at_monotonic_ns']):
            raise ValueError('recovery deadline differs from actual final planning time')
    if terminal is not None:
        stopped_reasons = (DRIVER_RELEASE_REASONS | RECOVERY_RELEASE_REASONS) - {"normal_benchmark_complete"}
        if event["reason"] == "normal_benchmark_complete":
            reason_matches = not (
                terminal["driver_state"] == "stopped"
                or terminal["driver_reason"] in stopped_reasons)
        else:
            reason_matches = (terminal["driver_state"] == "stopped"
                              and terminal["driver_reason"] == event["reason"])
        failure_reason = event["reason"] in ({
            "action_lease_expired_before_dispatch", "owner_heartbeat_lost"} | RECOVERY_RELEASE_REASONS)
        boundary_reason = event["reason"] in {
            "point_goal_deadline", "owner_lease_insufficient"}
        if (not reason_matches
                or (failure_reason and (
                    terminal["algorithm_error"] is not True
                    or terminal["algorithm_failure"] != {
                        "type": "DriverStopped", "message": event["reason"]}))
                or (boundary_reason and (
                    terminal["algorithm_error"] is not False
                    or terminal["algorithm_failure"] is not None
                    or terminal["expired"] is not True))):
            raise ValueError("raw Driver release reason differs from terminal")


def _close_release(rows: list[dict], source_release_index: int) -> None:
    closes = [(index, row["payload"]) for index, row in enumerate(rows)
              if row["record_type"] == "close_release"]
    if len(closes) != 1:
        raise ValueError("raw Runtime final close release count differs")
    index, payload = closes[0]
    if index <= source_release_index or index != len(rows) - 1:
        raise ValueError("raw Runtime close release is not after source release")
    _exact(payload, {"action", "backend_result"}, "raw close release")
    snapshot = _neutral_snapshot(payload["action"], "raw close action")
    backend = payload["backend_result"]
    _exact(backend, {"observation", "receipt", "reward", "terminated",
                     "truncated"}, "raw close backend result")
    receipt = ClientBehaviorReceiptV2.from_mapping(backend["receipt"])
    observation = backend["observation"]
    previous_observation = next(
        (row["payload"]["backend_result"]["observation"]
         for row in reversed(rows[:index]) if row["record_type"] == "step"),
        None,
    )
    if (previous_observation is None
            or receipt.status not in {"executed", "confirmed_local"}
            or receipt.reason != "neutral"
            or receipt.episode_id != observation.get("episode_id")
            or receipt.generation_id != observation.get("sequence_id")
            or receipt.request_sequence_id
                != observation.get("request_sequence_id")
            or snapshot.episode_id != observation.get("episode_id")
            or snapshot.request_sequence_id
                != observation.get("request_sequence_id")
            or snapshot.observation_sequence_id
                != previous_observation.get("sequence_id")
            or snapshot.episode_id != previous_observation.get("episode_id")):
        raise ValueError("raw close receipt/observation binding differs")


def _history_runtime_evidence(rows: list[dict], history: dict,
                              task_start_sample: dict,
                              task_started_ns: int) -> None:
    if history["history"] == "H0":
        if history != {"history": "H0", "preparation": [],
                       "concerned_blocks": []}:
            raise ValueError("H0 history treatment differs")
        return
    registrations = [(index, row["payload"])
                     for index, row in enumerate(rows)
                     if row["record_type"] == "ordered_source_registered"
                     and row["payload"].get("label")
                         == "normal-history-preparation"]
    if len(registrations) != 1:
        raise ValueError("raw history preparation registration differs")
    registration_index, registration = registrations[0]
    preparation_source = registration["source"]
    _ordered_source({**preparation_source,
                     "label": "normal-history-preparation"},
                    task_start_sample["episode_id"])
    releases = [(index, row["payload"])
                for index, row in enumerate(rows)
                if row["record_type"] == "ordered_source_unregistered"
                and row["payload"].get("source") == preparation_source]
    if len(releases) != 1 or releases[0][0] <= registration_index:
        raise ValueError("raw history preparation release differs")
    release_index = releases[0][0]
    preparation = history["preparation"]
    if type(preparation) is not list or not preparation:
        raise ValueError("history preparation trace is absent")
    recorded_ordinals = []
    preparation_observations = []
    previous_ordinal = registration_index
    for expected_sequence, item in enumerate(preparation, 1):
        _exact(item, {"sequence", "runtime_trace_ordinal",
                      "observation_sequence_id", "submitted_at_ns",
                      "received_at_ns", "yaw_delta_degrees"},
               "history preparation row")
        ordinal = _integer(item["runtime_trace_ordinal"],
                           "history Runtime ordinal")
        if (item["sequence"] != expected_sequence
                or not previous_ordinal < ordinal < release_index
                or ordinal >= len(rows)
                or rows[ordinal]["record_type"] != "step"):
            raise ValueError("history preparation ordinal/order differs")
        intent_index = next((index for index in range(ordinal - 1,
                                  previous_ordinal, -1)
                             if rows[index]["record_type"] == "ordered_intent"),
                            None)
        dispatch_index = next((index for index in range(ordinal - 1,
                                    previous_ordinal, -1)
                               if rows[index]["record_type"] == "dispatch"),
                              None)
        if (intent_index is None or dispatch_index is None
                or not intent_index < dispatch_index < ordinal):
            raise ValueError("raw history action chain is incomplete")
        envelope = rows[intent_index]["payload"]["envelope"]
        intent = envelope["intent"]
        selected = rows[dispatch_index]["payload"]["decision"]["action"]
        expected_yaw = 180.0 if expected_sequence == 1 else 0.0
        if (envelope["source"] != preparation_source
                or envelope["sequence"] != expected_sequence
                or _integer(item["submitted_at_ns"],
                            "history submitted time")
                    != intent["submitted_at_monotonic_ns"]
                or _number(item["yaw_delta_degrees"],
                           "history yaw delta") != expected_yaw
                or intent["look"] != {"yaw_delta_degrees": expected_yaw,
                                      "pitch_delta_degrees": 0.0}
                or not _neutral(_movement(intent, "raw history requested action"))
                or not _neutral(_movement(selected, "raw history selected action"))):
            raise ValueError("raw history requested/selected action differs")
        backend = rows[ordinal]["payload"]["backend_result"]
        observation, receipt = backend["observation"], backend["receipt"]
        if (receipt["status"] not in {"executed", "confirmed_local"}
                or receipt["episode_id"] != observation["episode_id"]
                or receipt["generation_id"] != observation["sequence_id"]
                or receipt["request_sequence_id"]
                    != observation["request_sequence_id"]
                or observation["sequence_id"]
                    != item["observation_sequence_id"]
                or observation["received_at_monotonic_ns"]
                    != item["received_at_ns"]):
            raise ValueError("raw history receipt/observation differs")
        recorded_ordinals.append(ordinal)
        preparation_observations.append(observation)
        previous_ordinal = ordinal
    raw_step_ordinals = [index for index in range(registration_index + 1,
                                                  release_index)
                         if rows[index]["record_type"] == "step"]
    if raw_step_ordinals != recorded_ordinals:
        raise ValueError("history preparation omits raw Runtime steps")
    if _raw_sample(preparation_observations[-1]) != task_start_sample:
        raise ValueError("history preparation does not end at task-start sample")
    earlier_observations = []
    for row in rows[:registration_index]:
        if (row["record_type"] == "reset"
                and row["payload"]["result"].get("succeeded") is True):
            earlier_observations.append(row["payload"]["result"]["observation"])
        elif row["record_type"] == "step":
            earlier_observations.append(
                row["payload"]["backend_result"]["observation"])
    if not earlier_observations:
        raise ValueError("history preparation lacks its source observation")
    source_observation = earlier_observations[-1]
    source_request = source_observation["request_started_at_monotonic_ns"]
    source_received = source_observation["received_at_monotonic_ns"]
    source_blocks = source_observation["perception"]["value"]["blocks"]
    authorized = {tuple(block["position"]): block.get("sources", [])
                  for block in source_blocks}
    prepared_authorization = [
        {tuple(block["position"])
         for block in observation["perception"]["value"]["blocks"]}
        for observation in preparation_observations]
    concerned = history["concerned_blocks"]
    if type(concerned) is not list or not concerned:
        raise ValueError("concerned history blocks are absent")
    for item in concerned:
        position = tuple(_position(item["position"],
                                   "concerned history position"))
        age = _integer(item["task_start_age_ns"], "history age")
        if (item["original_request_start_ns"] != source_request
                or item["original_received_at_ns"] != source_received
                or not source_received - source_request <= age
                    <= task_started_ns - source_request
                or position not in authorized
                or ('surface_depth' if source_observation['perception']['value'].get('sensor_profile_revision',3)==4
                    else 'first_hit_ray') not in authorized[position]
                or "body_contact" in authorized[position]
                or any(position in positions
                       for positions in prepared_authorization)):
            raise ValueError("history stamp or authorization differs from raw Runtime")


def _runtime_evidence(root: Path, run: dict, source: IntentSourceV1,
                      links: list[dict], spawn_sample: dict,
                      task_start_sample: dict, history: dict,
                      terminal: dict | None = None) -> tuple[bool, str]:
    rows = list(iter_segmented_jsonl(root / "runtime-trace" / "trace"))
    diagnostics = list(iter_segmented_jsonl(root / "runtime-trace" / "diagnostics"))
    if not rows:
        raise ValueError("raw Runtime stream is empty")
    for row in rows:
        _exact(row, {"schema_version", "record_type", "payload"}, "Runtime row")
        if row["schema_version"] != "mc2p.trace-record.v0":
            raise ValueError("raw Runtime row schema differs")
    expected_source = {key: value for key, value in run["ordered_source"].items()
                       if key != "label"}
    registrations = [(index, row["payload"]) for index, row in enumerate(rows)
                     if row["record_type"] == "ordered_source_registered"]
    releases = [(index, row["payload"]) for index, row in enumerate(rows)
                if row["record_type"] == "ordered_source_unregistered"]
    matching_registrations = [(index, payload) for index, payload in registrations
                              if payload.get("source") == expected_source]
    matching_releases = [(index, payload) for index, payload in releases
                         if payload.get("source") == expected_source]
    allowed_labels = {run["ordered_source"]["label"]}
    if history["history"] != "H0":
        allowed_labels.add("normal-history-preparation")
    expected_source_count = 1 if history["history"] == "H0" else 2
    if (len(registrations) != expected_source_count
            or len(releases) != expected_source_count
            or len(matching_registrations) != 1
            or matching_registrations[0][1] != {
                "label": run["ordered_source"]["label"],
                "source": expected_source}
            or len(matching_releases) != 1
            or matching_releases[0][1] != {
                "source": expected_source, "removed_intent_ids": []}
            or any(payload.get("label") not in allowed_labels
                   for _, payload in registrations)
            or any(payload.get("source") not in
                   [registered.get("source") for _, registered in registrations]
                   for _, payload in releases)):
        raise ValueError("raw ordered-source registration/release differs")
    registration_index = matching_registrations[0][0]
    release_index = matching_releases[0][0]
    if registration_index >= release_index:
        raise ValueError("raw ordered-source lifecycle order differs")
    raw_observations = []
    for row in rows:
        if row["record_type"] == "reset" and row["payload"]["result"].get("succeeded") is True:
            raw_observations.append(row["payload"]["result"]["observation"])
        elif row["record_type"] == "step":
            raw_observations.append(row["payload"]["backend_result"]["observation"])
    projected_samples = [_raw_sample(observation) for observation in raw_observations]
    if (projected_samples.count(spawn_sample) != 1
            or projected_samples.count(task_start_sample) != 1):
        raise ValueError("spawn/task-start sample differs from raw Runtime observation")
    _history_runtime_evidence(rows, history, task_start_sample,
                              run["controller_clock"]["started_at_ns"])
    if not links:
        raise ValueError("trajectory supplies no main Runtime execution links")
    linked_ordinals = []
    planning_attempt_id = None
    previous_step_ordinal = registration_index
    previous_observation_sequence = task_start_sample["sequence_id"]
    for sequence, pair in enumerate(links, 1):
        link, recorded_sample = pair["execution"], pair["sample"]
        ordinal = link["confirmed_execution"]["runtime_trace_ordinal"]
        if (not previous_step_ordinal < ordinal < release_index
                or ordinal >= len(rows) or rows[ordinal]["record_type"] != "step"):
            raise ValueError("trajectory does not point to a raw Runtime step")
        step = rows[ordinal]["payload"]
        receipt = step["backend_result"]["receipt"]
        confirmed = link["confirmed_execution"]
        for key in ("status", "episode_id", "generation_id", "request_sequence_id"):
            if receipt[key] != confirmed[key]:
                raise ValueError("raw Runtime receipt differs from trajectory")
        dispatches = [index for index in range(previous_step_ordinal + 1, ordinal)
                      if rows[index]["record_type"] == "dispatch"]
        intents = [index for index in range(previous_step_ordinal + 1, ordinal)
                   if rows[index]["record_type"] == "ordered_intent"]
        if len(dispatches) != 1 or len(intents) != 1 or intents[0] > dispatches[0]:
            raise ValueError("raw Runtime action chain is incomplete")
        dispatch = rows[dispatches[0]]["payload"]["decision"]["action"]
        envelope = rows[intents[0]]["payload"]["envelope"]
        _main_intent(envelope, source, sequence,
                     previous_observation_sequence,
                     link["controller_ns"], link["requested_action"])
        planning_attempt_id = _main_planning_event(
            rows, intents[0], dispatches[0], run, source, sequence,
            previous_observation_sequence, envelope["intent"],
            planning_attempt_id,
        )
        if (envelope["source"] != expected_source
                or _movement(envelope["intent"], "raw requested action") != link["requested_action"]
                or _movement(dispatch, "raw selected action") != link["selected_action"]):
            raise ValueError("raw requested/selected action differs from trajectory")
        observation = step["backend_result"]["observation"]
        if (observation["episode_id"] != confirmed["episode_id"]
                or observation["sequence_id"] != confirmed["generation_id"]
                or observation["request_sequence_id"] != confirmed["request_sequence_id"]):
            raise ValueError("raw observation/receipt generation differs")
        if _raw_sample(observation) != recorded_sample:
            raise ValueError("trajectory sample values differ from raw Runtime observation")
        linked_ordinals.append(ordinal)
        previous_step_ordinal = ordinal
        previous_observation_sequence = observation["sequence_id"]
    _driver_release(rows, source, linked_ordinals[-1], release_index, run,
                    len(links), terminal)
    _close_release(rows, release_index)
    raw_main_intents = [index for index in range(registration_index + 1,
                                                 release_index)
                        if rows[index]["record_type"] == "ordered_intent"
                        and rows[index]["payload"]["envelope"].get("source")
                            == expected_source]
    if len(raw_main_intents) != len(links):
        raise ValueError("raw Runtime main intent count differs from trajectory")
    derived_step_ordinals = []
    for intent_index, next_intent_index in zip(
            raw_main_intents,
            raw_main_intents[1:] + [linked_ordinals[-1] + 1]):
        steps = [index for index in range(intent_index + 1, next_intent_index)
                 if rows[index]["record_type"] == "step"]
        if len(steps) != 1:
            raise ValueError("raw Runtime main intent has no unique step")
        derived_step_ordinals.append(steps[0])
    if derived_step_ordinals != linked_ordinals:
        raise ValueError("raw Runtime main steps differ from trajectory")
    sample_ordinals = [index for index, row in enumerate(rows)
                       if row["record_type"] in {"reset", "step", "close_release"}
                       and (row["record_type"] != "reset"
                            or row["payload"]["result"].get("succeeded") is True)]
    if len(diagnostics) != len(sample_ordinals):
        raise ValueError("backend diagnostics do not cover every Runtime sample")
    source_backend = "fabric" if run["backend"] == "standalone" else "craftground"
    previous_tick = -1
    for expected_ordinal, diagnostic in zip(sample_ordinals, diagnostics):
        _exact(diagnostic, {"schema_version", "runtime_trace_ordinal", "episode_id",
                            "observation_sequence_id", "source_backend", "diagnostics"},
               "Runtime diagnostic")
        values = diagnostic["diagnostics"]
        common_bad = (diagnostic["schema_version"] != "mc2p.normal-navigation-diagnostic.v1"
                or diagnostic["runtime_trace_ordinal"] != expected_ordinal
                or diagnostic["source_backend"] != source_backend
                or type(values) is not dict)
        if run["backend"] == "standalone":
            diagnostics_bad = (values.get("window_recorded") is not True
                    or values.get("window_visible_at_creation") is not False
                    or values.get("window_visible") is not False
                    or values.get("has_integrated_server") is not False
                    or any(values.get(key) != 0 for key in (
                        "framebuffer_capture_attempts", "image_encode_attempts",
                        "world_render_completions", "gui_render_completions"))
                    or type(values.get("client_tick")) is not int
                    or values["client_tick"] <= previous_tick)
            tick = values.get("client_tick", -1)
        else:
            diagnostics_bad = (values.get("observation_mode") != "structured_only"
                    or values.get("debug_frame_count") != 0
                    or values.get("debug_sink_failure_count") != 0
                    or type(values.get("observation_count")) is not int
                    or values["observation_count"] <= previous_tick)
            tick = values.get("observation_count", -1)
        if common_bad or diagnostics_bad:
            raise ValueError("backend diagnostic association or zero-image invariant differs")
        previous_tick = tick
    return True, "sealed Runtime registration/action/receipt/diagnostic chain matched"


def _process_identity(value: dict, label: str | None = None) -> dict:
    keys = {"pid", "create_time"} | ({"label"} if label is not None else set())
    _exact(value, keys, "host process identity")
    if label is not None and value["label"] != label:
        raise ValueError("host process label differs")
    ClientProcessIdentity(value["pid"], value["create_time"])
    return {"pid": value["pid"], "create_time": value["create_time"]}


def _same_host_launch_verification(encoded_launch: str) -> dict:
    return inspect_launch(json.loads(encoded_launch, object_pairs_hook=_object))


def _same_host_asset_verification() -> dict:
    return verify_assets()


def _expected_server_properties(*, seed: int, port: int) -> dict[str, str]:
    properties = {
        "server-ip": "127.0.0.1", "server-port": str(port),
        "level-seed": str(seed), "level-name": "world",
        "level-type": "minecraft:flat", "generate-structures": "false",
        "online-mode": "false", "enforce-secure-profile": "false",
        "enable-rcon": "false", "enable-query": "false",
        "enable-command-block": "false", "gamemode": "survival",
        "difficulty": "peaceful", "max-players": "2",
        "view-distance": "4", "simulation-distance": "4",
        "spawn-animals": "false", "spawn-monsters": "false",
        "motd": "MC2P isolated local deployment test",
    }
    properties["generator-settings"] = json.dumps({
        "biome": "minecraft:plains",
        "layers": [
            {"height": 1, "block": "minecraft:bedrock"},
            {"height": 2, "block": "minecraft:dirt"},
            {"height": 1, "block": "minecraft:grass_block"},
        ],
        "lakes": False, "features": False, "structure_overrides": [],
    }, separators=(",", ":"))
    return properties


def _unescape_java_property(value: str) -> str:
    result = []
    index = 0
    escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
            continue
        index += 1
        if index == len(value):
            raise ValueError("dangling retained server property escape")
        escaped = value[index]
        if escaped == "u":
            digits = value[index + 1:index + 5]
            if len(digits) != 4 or re.fullmatch(r"[0-9a-fA-F]{4}", digits) is None:
                raise ValueError("invalid retained server property unicode escape")
            result.append(chr(int(digits, 16)))
            index += 5
            continue
        result.append(escapes.get(escaped, escaped))
        index += 1
    return "".join(result)


def _logical_java_property_lines(text: str) -> list[str]:
    result = []
    pending = ""
    for physical in text.splitlines():
        line = physical.lstrip(" \t\f") if pending else physical
        combined = pending + line
        trailing_slashes = len(combined) - len(combined.rstrip("\\"))
        if trailing_slashes % 2:
            pending = combined[:-1]
        else:
            result.append(combined)
            pending = ""
    if pending:
        raise ValueError("unterminated retained server property continuation")
    return result


def _server_properties(path: Path) -> dict[str, str]:
    _no_links(path)
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("missing or oversized retained server properties")
    result = {}
    for raw_line in _logical_java_property_lines(path.read_text("utf-8")):
        line = raw_line.lstrip(" \t\f")
        if not line or line.startswith(("#", "!")):
            continue
        escaped = False
        separator = None
        whitespace_separator = False
        for index, character in enumerate(line):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character in "=:":
                separator = index
                break
            elif character in " \t\f":
                separator = index
                whitespace_separator = True
                break
        if separator is None:
            key_encoded, value_encoded = line, ""
        else:
            key_encoded = line[:separator]
            value_start = separator
            if whitespace_separator:
                while value_start < len(line) and line[value_start] in " \t\f":
                    value_start += 1
                if value_start < len(line) and line[value_start] in "=:":
                    value_start += 1
            else:
                value_start += 1
            while value_start < len(line) and line[value_start] in " \t\f":
                value_start += 1
            value_encoded = line[value_start:]
        key = _unescape_java_property(key_encoded)
        value = _unescape_java_property(value_encoded)
        if not key or key in result:
            raise ValueError("duplicate retained server property")
        result[key] = value
    return result


def _validate_host_provenance(root: Path, run: dict, fixture: dict,
                              supervision: dict) -> None:
    provenance = _json(root / "owned" / "host-provenance.json")
    ready = _json(root / "owned" / "ready.json")
    expected_top = ({"schema_version", "backend", "fixture", "launch", "server"}
                    if run["backend"] == "standalone" else
                    {"schema_version", "backend", "fixture", "craftground"})
    _exact(provenance, expected_top, "host provenance")
    _exact(ready, {"schema_version", "backend", "ports", "processes"},
           "host ready")
    if (provenance["schema_version"]
            != "mc2p.normal-navigation-host-provenance.v1"
            or provenance["backend"] != run["backend"]
            or provenance["fixture"] != fixture
            or ready["schema_version"]
                != "mc2p.normal-navigation-host-ready.v1"
            or ready["backend"] != run["backend"]
            or type(ready["ports"]) is not list
            or any(type(port) is not int or not 1 <= port <= 65535
                   for port in ready["ports"])
            or len(ready["ports"]) != len(set(ready["ports"]))
            or type(ready["processes"]) is not list):
        raise ValueError("host provenance/ready binding differs")
    if type(supervision["registered_processes"]) is not list:
        raise ValueError("parent supervision process identities differ")
    supervised_identities = [_process_identity(value)
                             for value in supervision["registered_processes"]]
    if len({value["pid"] for value in supervised_identities}) != len(
            supervised_identities):
        raise ValueError("parent supervision repeats a process PID")

    if run["backend"] == "standalone":
        if len(ready["ports"]) != 2 or len(ready["processes"]) != 2:
            raise ValueError("standalone host ready cardinality differs")
        server_port, client_port = ready["ports"]
        server = _process_identity(ready["processes"][0], "server")
        client = _process_identity(ready["processes"][1], "client")
        if (server == client
                or _json(root / "owned" / "server-identity.json") != server
                or _json(root / "owned" / "client" / "identity.json") != client
                or server not in supervised_identities
                or client not in supervised_identities):
            raise ValueError("standalone host process identity differs")
        launch = provenance["launch"]
        server_metadata = provenance["server"]
        _exact(launch, {"manifest_sha256", "verified", "assets"},
               "standalone launch provenance")
        _exact(launch["verified"], {"client_only", "jar_count",
                                    "project_directory_count",
                                    "remap_jar_count"}, "verified launch")
        _exact(launch["assets"], {"index_sha1", "metadata_sha256",
                                  "object_count"}, "verified assets")
        _exact(server_metadata, {"fixture_animals", "properties_sha256",
                                 "scope", "seed", "server_mods",
                                 "server_port", "server_sha256",
                                 "upstream_sha1", "world_name"},
               "standalone server provenance")
        launch_path = root / "owned" / "client-launch.json"
        launch_value = _json(launch_path)
        derived_launch = _same_host_launch_verification(json.dumps(
            launch_value, sort_keys=True, separators=(",", ":")))
        derived_assets = _same_host_asset_verification()
        server_directory = root / "owned" / "server"
        server_jar = server_directory / "server.jar"
        server_properties = server_directory / "server.properties"
        _no_links(server_jar)
        if not server_jar.is_file():
            raise ValueError("retained standalone server jar is missing")
        properties = _server_properties(server_properties)
        expected_properties = _expected_server_properties(
            seed=run["case_plan"]["seed"], port=server_port)
        expected_properties_payload = "".join(
            f"{key}={value}\n" for key, value in expected_properties.items()
        ).encode("utf-8")
        with server_jar.open("rb") as stream:
            server_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        with server_jar.open("rb") as stream:
            server_sha1 = hashlib.file_digest(stream, "sha1").hexdigest()
        if (launch["manifest_sha256"] != hashlib.sha256(
                    launch_path.read_bytes()).hexdigest()
                or launch["verified"] != derived_launch
                or launch["assets"] != derived_assets
                or server_metadata["seed"] != run["case_plan"]["seed"]
                or server_metadata["server_port"] != server_port
                or server_metadata["server_mods"] != []
                or server_metadata["fixture_animals"] is not False
                or server_metadata["world_name"] != "world"
                or server_metadata["scope"]
                    != "new isolated loopback vanilla test server; offline identity only"
                or server_metadata["properties_sha256"]
                    != hashlib.sha256(expected_properties_payload).hexdigest()
                or server_metadata["server_sha256"] != server_sha256
                or server_metadata["upstream_sha1"] != server_sha1
                or any(properties.get(key) != value
                       for key, value in expected_properties.items())):
            raise ValueError("standalone launch/server provenance differs")
        connection = _json(root / "owned" / "client" / "connection-proof.json")
        if (connection.get("server_identity") != server
                or connection.get("client_identity") != client
                or not validate_connection_evidence(
                    connection, server_port=server_port,
                    ipc_port=client_port)):
            raise ValueError("standalone connection evidence differs")
    else:
        if len(ready["ports"]) != 1 or len(ready["processes"]) != 1:
            raise ValueError("CraftGround host ready cardinality differs")
        process = _process_identity(ready["processes"][0],
                                    "craftground-client")
        if (_json(root / "owned" / "craftground-client-identity.json")
                != process or process not in supervised_identities):
            raise ValueError("CraftGround host process identity differs")
        craftground = provenance["craftground"]
        _exact(craftground, {"sandbox", "sandbox_manifest", "reused",
                             "installed_save"}, "CraftGround provenance")
        sandbox_value = craftground["sandbox"]
        if type(sandbox_value) is not dict or type(sandbox_value.get("sandbox")) is not str:
            raise ValueError("CraftGround sandbox provenance differs")
        sandbox = Path(sandbox_value["sandbox"]).absolute()
        owned_sandboxes = (root / "owned" / "sandboxes").resolve()
        try:
            sandbox.resolve().relative_to(owned_sandboxes)
            installed = Path(craftground["installed_save"]).resolve()
            installed.relative_to(sandbox.resolve() / "run" / "saves")
        except (ValueError, TypeError):
            raise ValueError("CraftGround sandbox/save escaped owned evidence")
        if (sandbox_provenance(sandbox) != sandbox_value
                or not installed.is_dir()
                or type(craftground["sandbox_manifest"]) is not dict
                or type(craftground["reused"]) is not bool):
            raise ValueError("CraftGround sandbox provenance does not reverify")


def evaluate_normal_run(directory: Path) -> dict:
    """Evaluate one sealed directory without converting absent evidence into failure evidence."""
    result = {
        "schema_version": "mc2p.normal-navigation-evidence.v1",
        "engineering_status": "invalid_run",
        "algorithm_outcome": "unavailable",
        "directory": str(Path(directory).absolute()),
        "evidence": {name: {"passed": False, "reason": "not evaluated"} for name in
                     ("initial_state", "normal", "source", "source_archive", "runtime",
                      "time", "cleanup", "permissions", "trajectory")},
        "errors": [],
    }
    arrived = pit = expired = algorithm_error = False
    terminal_available = False
    trajectory_available_for_outcome = False
    try:
        root = Path(directory).absolute()
        _no_links(root)
        if not root.is_dir():
            raise ValueError('run directory is missing')
        run = _json(root / 'run-manifest.json')
        joint = run.get('schema_version') == 'mc2p.normal-navigation-run.v2'
        expected_entries = REQUIRED_ENTRIES | {'surface','memory-observations.json','joint-feedback.json'} if joint else REQUIRED_ENTRIES
        if (joint and run.get('group')=='goal_directed_exploration'
            and run.get('joint_configuration',{}).get('terrain_history_revision')==1
            and run.get('case_plan',{}).get('case') in ('terrain_removed','terrain_wall_added','terrain_entity')):
            expected_entries=expected_entries | {'terrain-change.json'}
        if {path.name for path in root.iterdir()} != expected_entries:
            raise ValueError("run directory has missing or undeclared evidence entries")

        fixture = verify_normal_fixture(root / "fixture")
        run = _json(root / "run-manifest.json")
        initial = _json(root / "initial-state.json")
        terminal = _json(root / "terminal.json")
        cleanup = _json(root / "cleanup.json")
        permissions = _json(root / "permission-audit.json")

        run_keys = {"schema_version", "case_plan", "fixture_manifest_sha256", "observation_mode",
                     "sensor_contract", "control_profile", "backend", "group", "history",
                     "algorithm", "layout_sha256", "host_provenance_sha256",
                     "task_binding", "ordered_source",
                     "controller_clock", "source_archive", "source_fingerprints"}
        _exact(run,run_keys | {'joint_configuration'} if joint else run_keys,'run manifest')
        if run["schema_version"] not in {"mc2p.normal-navigation-run.v1","mc2p.normal-navigation-run.v2"}:
            raise ValueError("unsupported normal run schema")
        plan = run["case_plan"]
        if (plan != fixture["case_plan"] or plan != case_plan(plan["case"], plan["seed"])
                or not _route_exists(plan["layout"], plan["start"]["position"], plan["actor_task"]["position"])):
            raise ValueError("run plan differs from the reverified fixture")
        expected_fixture_hash = hashlib.sha256((root / "fixture/fixture-manifest.json").read_bytes()).hexdigest()
        if run["fixture_manifest_sha256"] != expected_fixture_hash:
            raise ValueError("run did not bind the exact fixture manifest")
        if joint:
            from scripts.joint_navigation_evidence import validate_trial_contract
            validate_trial_contract(run)
        elif (run["observation_mode"] != "structured_only" or run["sensor_contract"] != SENSOR_CONTRACT
                or run["control_profile"] != "forward_stop_v1"):
            raise ValueError("formal observation or control contract differs")
        expected_layout_hash = hashlib.sha256(json.dumps(
            plan["layout"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        if (run["backend"] not in {"standalone", "craftground"}
                or run["group"] not in ({'C',*JOINT_STRATEGIES} if joint else {'A','B','C'})
                or run["history"] not in {"H0", "H1", "H2_2s", "H2_10s"}
                or run["algorithm"] != asdict(strategy_config(run["group"]))
                or run["layout_sha256"] != expected_layout_hash
                or run["host_provenance_sha256"] != hashlib.sha256(
                    (root / "owned" / "host-provenance.json").read_bytes()).hexdigest()):
            raise ValueError("backend/history/algorithm/layout binding differs")

        _exact(run["task_binding"], {"goal_id", "position", "scope_id", "absolute_deadline_ns"}, "task binding")
        _exact(run["controller_clock"], {"clock_id", "source", "started_at_ns"}, "controller clock")
        binding = run["task_binding"]
        source_metadata = run["ordered_source"]
        clock = run["controller_clock"]
        started = _integer(clock["started_at_ns"], "controller start", minimum=1)
        deadline = _integer(binding["absolute_deadline_ns"], "absolute deadline", minimum=1)
        if ({key: binding[key] for key in ("goal_id", "position")} != plan["actor_task"]
                or not isinstance(binding["scope_id"], str) or not 1 <= len(binding["scope_id"]) <= 128
                or deadline != started + plan["budget_ns"]
                or clock["source"] != "time.perf_counter_ns"
                or not isinstance(clock["clock_id"], str) or not 1 <= len(clock["clock_id"]) <= 128):
            raise ValueError("task or time binding invalid")
        fingerprints = run["source_fingerprints"]
        try:
            _verify_source_archive(root / "source-archive", fingerprints, run["source_archive"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            _section(result, "source_archive", False, reason=str(error))
            raise
        _section(result, "source_archive", True, files=len(fingerprints),
                 tree_sha256=run["source_archive"]["tree_sha256"],
                 reason="complete frozen source archive rehashed without current-tree substitution")

        _exact(initial, {"schema_version", "spawn", "task_start", "memory_scope_id",
                         "history_treatment"}, "initial state")
        if initial["schema_version"] != "mc2p.normal-navigation-initial.v1":
            raise ValueError("invalid initial state schema")
        spawn = initial["spawn"]
        _exact(spawn, {"sample", "yaw_degrees", "pitch_degrees", "game_mode", "health",
                       "food_level", "status_effects", "equipment"}, "spawn state")
        spawn_sample = _validate_sample(spawn["sample"])
        initial_sample = _validate_sample(initial["task_start"])
        source = _ordered_source(source_metadata, initial_sample["episode_id"])
        expected_start = plan["start"]["position"]
        initial_position = spawn_sample["position"]
        history = initial["history_treatment"]
        _exact(history, {"history", "preparation", "concerned_blocks"}, "history treatment")
        history_ok = (history["history"] == run["history"]
                      and type(history["preparation"]) is list
                      and type(history["concerned_blocks"]) is list
                      and (run["history"] != "H0"
                           or history == {"history": "H0", "preparation": [],
                                          "concerned_blocks": []}))
        if run["history"] != "H0":
            target_age = {"H1": 200_000_000, "H2_2s": 2_000_000_000,
                          "H2_10s": 10_000_000_000}[run["history"]]
            history_ok = (history_ok and len(history["preparation"]) > 0
                          and len(history["concerned_blocks"]) > 0)
            for item in history["concerned_blocks"]:
                _exact(item, {"position", "original_request_start_ns",
                              "original_received_at_ns", "task_start_age_ns"},
                       "concerned history block")
                _position(item["position"], "concerned history position")
                history_ok = (history_ok
                              and _integer(item["original_request_start_ns"],
                                           "history original request") >= 0
                              and _integer(item["original_received_at_ns"],
                                           "history original receive")
                                  >= item["original_request_start_ns"]
                              and _integer(item["task_start_age_ns"], "history age")
                                  >= target_age)
        initial_ok = (all(abs(actual - expected) <= 0.001 for actual, expected in zip(initial_position, expected_start))
                      and spawn_sample["episode_id"] == initial_sample["episode_id"]
                      and spawn_sample["sequence_id"] == 0
                      and spawn_sample["request_sequence_id"] is None
                      and initial_sample["sequence_id"] > spawn_sample["sequence_id"]
                      and initial_sample["request_sequence_id"]
                          == initial_sample["sequence_id"] - 1
                      and initial_sample["client_sample"]["clock_id"]
                          == spawn_sample["client_sample"]["clock_id"]
                      and initial_sample["client_sample"]["started_at_ns"]
                          >= spawn_sample["client_sample"]["completed_at_ns"]
                      and initial_sample["controller_clock_id"] == clock["clock_id"]
                      and initial_sample["received_at_ns"] <= started
                      and started - initial_sample["received_at_ns"] <= MAX_SAMPLE_GAP_NS
                      and initial_sample["received_at_ns"] - initial_sample["request_started_at_ns"] <= MAX_SAMPLE_GAP_NS
                      and initial_sample["on_ground"] is True
                      and initial_sample["horizontal_speed_blocks_per_tick"] <= 0.03
                      and abs(_number(spawn["yaw_degrees"], "initial yaw") - plan["start"]["yaw_degrees"]) <= 0.001
                      and abs(_number(spawn["pitch_degrees"], "initial pitch") - plan["start"]["pitch_degrees"]) <= 0.001
                      and spawn["game_mode"] == "survival" and spawn["health"] == 20.0
                      and spawn["food_level"] == 20 and spawn["status_effects"] == []
                      and spawn["equipment"] == [] and initial["memory_scope_id"] == binding["scope_id"]
                      and history_ok and _supported(plan["layout"], initial_position)
                      and _body_clear(plan["layout"], initial_position))
        _section(result, "initial_state", initial_ok,
                 reason="matched frozen spawn and survival state" if initial_ok else "initial state differs")

        _exact(terminal, {"schema_version", "ended_at_ns", "expired", "algorithm_error",
                          "algorithm_failure", "arrival_claimed", "driver_state",
                          "driver_reason"}, "terminal")
        ended = _integer(terminal["ended_at_ns"], "terminal end", minimum=started)
        if (terminal["schema_version"] != "mc2p.normal-navigation-terminal.v1"
                or any(type(terminal[key]) is not bool for key in ("expired", "algorithm_error", "arrival_claimed"))):
            raise ValueError("invalid terminal values")
        if ((terminal["algorithm_failure"] is None) != (not terminal["algorithm_error"])
                or type(terminal["driver_state"]) is not str
                or not terminal["driver_state"]
                or type(terminal["driver_reason"]) is not str
                or not terminal["driver_reason"]):
            raise ValueError("invalid algorithm failure detail")
        if terminal["algorithm_failure"] is not None:
            _exact(terminal["algorithm_failure"], {"type", "message"}, "algorithm failure")
        expired, algorithm_error = terminal["expired"], terminal["algorithm_error"]
        terminal_available = True
        time_ok = expired == (ended >= deadline)

        records = sample_count = execution_count = confirmed_count = 0
        pending_execution = None
        sample_identity = (initial_sample["episode_id"], initial_sample["controller_clock_id"],
                           initial_sample["client_sample"]["clock_id"])
        previous_sequence = initial_sample["sequence_id"]
        previous_client_end = initial_sample["client_sample"]["completed_at_ns"]
        previous_request_start = initial_sample["request_started_at_ns"]
        previous_received = initial_sample["received_at_ns"]
        previous_execution_ns = started - 1
        previous_sample = None
        hold_client_end = hold_controller = None
        normal_all = source_all = True
        execution_links = []
        trajectory_error = None
        try:
            for record in iter_segmented_jsonl(root / "trajectory"):
                records += 1
                if records > MAX_TRAJECTORY_RECORDS:
                    raise ValueError("trajectory record capacity exceeded")
                _exact(record, {"schema_version", "record_type", "sample"}
                       if record.get("record_type") == "sample" else
                       {"schema_version", "record_type", "request_sequence_id", "controller_ns", "source_id",
                        "requested_action", "selected_action", "confirmed_execution"}, "trajectory record")
                if record["schema_version"] != "mc2p.normal-navigation-record.v1":
                    raise ValueError("trajectory record schema differs")
                if record["record_type"] == "execution":
                    if pending_execution is not None:
                        raise ValueError("execution lacks an immediately following post-action sample")
                    pending_execution = _validate_execution(record)
                    execution_count += 1
                    normal_all = (normal_all
                                  and _normal(pending_execution["requested_action"],profile=run['control_profile'])
                                  and _normal(pending_execution["selected_action"],profile=run['control_profile']))
                    if (pending_execution["source_id"] != source.source_id
                            or not started <= pending_execution["controller_ns"] <= ended
                            or pending_execution["controller_ns"] <= previous_execution_ns):
                        raise ValueError("execution source, order, or controller time differs")
                    previous_execution_ns = pending_execution["controller_ns"]
                    source_all = source_all and pending_execution["source_id"] == source.source_id
                    continue
                if record["record_type"] != "sample":
                    raise ValueError("unknown trajectory record type")
                if pending_execution is None:
                    raise ValueError("post-action sample lacks its preceding execution")
                sample = _validate_sample(record["sample"])
                execution_links.append({"execution": pending_execution,
                                        "sample": sample})
                sample_count += 1
                client = sample["client_sample"]
                receipt = pending_execution["confirmed_execution"]
                if ((sample["episode_id"], sample["controller_clock_id"], client["clock_id"]) != sample_identity
                        or sample["sequence_id"] != previous_sequence + 1
                        or sample["request_sequence_id"] != sample["sequence_id"] - 1
                        or sample["request_started_at_ns"] < previous_request_start
                        or sample["received_at_ns"] <= previous_received
                        or client["started_at_ns"] < previous_client_end
                        or client["started_at_ns"] - previous_client_end > MAX_SAMPLE_GAP_NS
                        or sample["request_started_at_ns"] - previous_received > MAX_SAMPLE_GAP_NS
                        or sample["received_at_ns"] - sample["request_started_at_ns"] > MAX_SAMPLE_GAP_NS
                        or not started <= sample["request_started_at_ns"] <= sample["received_at_ns"] <= ended):
                    raise ValueError("sample identity, recorded order, freshness, or time domains are invalid")
                if (receipt["episode_id"] != sample["episode_id"]
                         or receipt["generation_id"] != sample["sequence_id"]
                         or receipt["request_sequence_id"] != sample["request_sequence_id"]
                         or pending_execution["request_sequence_id"] != sample["request_sequence_id"]
                         or not previous_received <= pending_execution["controller_ns"]
                             <= sample["request_started_at_ns"] <= sample["received_at_ns"]):
                    raise ValueError("receipt does not match the actual post-action sample")
                action = receipt["action"] if receipt["status"] == "executed" else None
                if action is not None:
                    confirmed_count += 1
                    normal_all = normal_all and _normal(action,profile=run['control_profile'])
                pit = pit or _pit_entry(plan["layout"], sample["position"])
                qualifies = _arrival_sample(plan["layout"], plan["actor_task"]["position"], sample, action)
                continuous = (previous_sample is not None
                              and sample["sequence_id"] == previous_sample["sequence_id"] + 1
                              and client["started_at_ns"] - previous_sample["client_sample"]["completed_at_ns"]
                                  <= MAX_SAMPLE_GAP_NS
                              and sample["request_started_at_ns"] - previous_sample["received_at_ns"]
                                  <= MAX_SAMPLE_GAP_NS)
                if qualifies:
                    if hold_client_end is None or not continuous:
                        hold_client_end = client["completed_at_ns"]
                        hold_controller = sample["received_at_ns"]
                    elif (client["started_at_ns"] - hold_client_end >= ARRIVAL_HOLD_NS
                          and sample["received_at_ns"] - hold_controller >= ARRIVAL_HOLD_NS
                          and sample["received_at_ns"] < deadline):
                        arrived = True
                else:
                    hold_client_end = hold_controller = None
                previous_sample = sample
                previous_sequence = sample["sequence_id"]
                previous_client_end = client["completed_at_ns"]
                previous_request_start = sample["request_started_at_ns"]
                previous_received = sample["received_at_ns"]
                pending_execution = None
        except (OSError, ValueError, KeyError, TypeError) as error:
            trajectory_error = str(error)

        stream_ok = (trajectory_error is None and pending_execution is None
                     and sample_count > 0 and execution_count == sample_count)
        trajectory_available_for_outcome = stream_ok
        arrival_matches = stream_ok and terminal["arrival_claimed"] == arrived
        trajectory_ok = stream_ok and arrival_matches
        if stream_ok and not arrival_matches:
            trajectory_error = "arrival claim differs from derived hold"
        _section(result, "trajectory", trajectory_ok, records=records,
                 samples=sample_count, executions=execution_count, reason=trajectory_error or "sealed stream exhausted")

        normal_ok = stream_ok and confirmed_count > 0 and normal_all
        _section(result, "normal", normal_ok, confirmed_executions=confirmed_count,
                 reason="requested, selected and confirmed controls use "+run['control_profile']
                 if normal_ok else "requested, selected or confirmed control violated normal subset")
        source_ok = stream_ok and source_all

        runtime_ok = False
        runtime_reason = "raw Runtime evidence not evaluated"
        try:
            runtime_ok, runtime_reason = _runtime_evidence(
                root, run, source, execution_links, spawn_sample, initial_sample,
                history, terminal)
        except (OSError, ValueError, KeyError, TypeError) as error:
            runtime_reason = str(error)
        _section(result, "runtime", runtime_ok, reason=runtime_reason)
        trajectory_ok = trajectory_ok and runtime_ok
        if not runtime_ok and trajectory_error is None:
            trajectory_error = runtime_reason
        _section(result, "trajectory", trajectory_ok, records=records,
                 samples=sample_count, executions=execution_count,
                 reason=trajectory_error or "sealed stream exhausted and matched raw Runtime")

        _exact(cleanup, {"schema_version", "finished_at_ns", "ordered_source_released", "neutral_confirmed",
                         "worker_state", "live_processes", "listening_ports", "errors",
                         "source_tree_unchanged", "source_tree_sha256"}, "cleanup")
        cleanup_ok = (cleanup["schema_version"] == "mc2p.normal-navigation-cleanup.v1"
                      and _integer(cleanup["finished_at_ns"], "cleanup finish", minimum=ended) >= ended
                      and cleanup["ordered_source_released"] is True and cleanup["neutral_confirmed"] is True
                      and cleanup["worker_state"] == "closed" and cleanup["live_processes"] == 0
                      and cleanup["listening_ports"] == [] and cleanup["errors"] == []
                      and cleanup["source_tree_unchanged"] is True
                      and cleanup["source_tree_sha256"] == run["source_archive"]["tree_sha256"])
        supervision = _json(root / "supervision.json")
        _exact(supervision, {"schema_version", "return_code", "primary_failure", "cleanup_failures",
                             "process_stopped", "registered_processes"}, "parent supervision")
        _validate_host_provenance(root, run, fixture, supervision)
        host_cleanup = _json(root / "owned" / "host-cleanup.json")
        _exact(host_cleanup, {"schema_version", "backend", "live_processes",
                              "listening_ports", "cleanup_failures"}, "host cleanup")
        cleanup_ok = (cleanup_ok
                      and supervision["schema_version"] == "mc2p.bounded-process-result.v0"
                      and supervision["return_code"] == 0
                      and supervision["primary_failure"] is None
                      and supervision["cleanup_failures"] == []
                      and supervision["process_stopped"] is True
                      and host_cleanup == {"schema_version": "mc2p.normal-navigation-host-cleanup.v1",
                          "backend": run["backend"], "live_processes": 0,
                          "listening_ports": [], "cleanup_failures": []})
        _section(result, "cleanup", cleanup_ok, reason="source, neutral, worker, process and port cleanup verified"
                 if cleanup_ok else "cleanup evidence incomplete")
        source_ok = source_ok and cleanup["ordered_source_released"] is True
        _section(result, "source", source_ok, source_id=source.source_id,
                 reason="single ordered source matched execution and release" if source_ok else "source lifecycle mismatch")

        _exact(permissions, {"schema_version", "actor_task_keys", "actor_received_layout", "actor_received_start",
                             "privileged_state_entered_actor", "pov_generated", "runtime_teleport",
                             "actor_world_edit_capability", "errors"}, "permission audit")
        permission_ok = (permissions["schema_version"] == "mc2p.normal-navigation-permission-audit.v1"
                         and permissions["actor_task_keys"] == ["goal_id", "position"]
                         and all(permissions[key] is False for key in ("actor_received_layout", "actor_received_start",
                                 "privileged_state_entered_actor", "pov_generated", "runtime_teleport",
                                 "actor_world_edit_capability")) and permissions["errors"] == [])
        _section(result, "permissions", permission_ok,
                 reason="actor input and zero-POV/world-edit boundary recorded" if permission_ok else "permission audit failed")
        time_ok = time_ok and trajectory_ok
        _section(result, "time", time_ok, started_at_ns=started, absolute_deadline_ns=deadline,
                 ended_at_ns=ended, expired=expired,
                 reason="controller bounds and client sample time remain separate" if time_ok else "time evidence inconsistent")

        if terminal_available and (algorithm_error or trajectory_available_for_outcome):
            result["algorithm_outcome"] = trial_outcome(arrived=arrived, pit_entry=pit,
                                                         expired=expired, algorithm_error=algorithm_error)
            result['algorithm_outcome']=recovery_outcome(result['algorithm_outcome'],terminal,runtime_validated=runtime_ok)
        if all(section["passed"] for section in result["evidence"].values()):
            result["engineering_status"] = "valid"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        result["errors"].append(type(error).__name__ + ": " + str(error))
        if terminal_available and (algorithm_error or trajectory_available_for_outcome):
            result["algorithm_outcome"] = trial_outcome(arrived=arrived, pit_entry=pit,
                                                         expired=expired, algorithm_error=algorithm_error)
    return result


def _validate_sample(value: dict) -> dict:
    _exact(value, {"episode_id", "sequence_id", "request_sequence_id", "request_started_at_ns",
                   "received_at_ns", "controller_clock_id", "client_sample", "position", "on_ground",
                   "horizontal_speed_blocks_per_tick"}, "actual sample")
    _identifier(value["episode_id"], "sample episode")
    _identifier(value["controller_clock_id"], "sample controller clock")
    if type(value["on_ground"]) is not bool:
        raise ValueError("sample identity/state invalid")
    for key in ("sequence_id", "request_started_at_ns", "received_at_ns"):
        _integer(value[key], "sample " + key)
    if value["request_sequence_id"] is not None:
        _integer(value["request_sequence_id"], "sample request sequence")
    if value["received_at_ns"] < value["request_started_at_ns"]:
        raise ValueError("sample received before request start")
    client = value["client_sample"]
    _exact(client, {"clock_id", "started_at_ns", "completed_at_ns"}, "client sample interval")
    _identifier(client["clock_id"], "client sample clock")
    if client["clock_id"] == value["controller_clock_id"]:
        raise ValueError("sample clock identity invalid")
    _integer(client["started_at_ns"], "client sample start")
    _integer(client["completed_at_ns"], "client sample completion")
    if client["completed_at_ns"] < client["started_at_ns"]:
        raise ValueError("client sample completion precedes start")
    value = dict(value)
    value["position"] = _position(value["position"], "sample position")
    speed = _number(value["horizontal_speed_blocks_per_tick"], "sample horizontal speed")
    if speed < 0:
        raise ValueError("sample horizontal speed is negative")
    value["horizontal_speed_blocks_per_tick"] = speed
    return value


def _validate_execution(record: dict) -> dict:
    _integer(record["request_sequence_id"], "request sequence")
    _integer(record["controller_ns"], "execution controller time", minimum=1)
    if not isinstance(record["source_id"], str) or not record["source_id"]:
        raise ValueError("execution source missing")
    _action(record["requested_action"], "requested action")
    _action(record["selected_action"], "selected action")
    confirmed = record["confirmed_execution"]
    _exact(confirmed, {"status", "episode_id", "generation_id", "request_sequence_id", "action",
                       "runtime_trace_ordinal"}, "confirmed execution")
    if (confirmed["status"] not in ("executed", "confirmed_local", "rejected")
            or not isinstance(confirmed["episode_id"], str) or not confirmed["episode_id"]
            or _integer(confirmed["generation_id"], "receipt generation", minimum=1) < 1
            or confirmed["request_sequence_id"] != record["request_sequence_id"]
            or _integer(confirmed["runtime_trace_ordinal"], "runtime trace ordinal") < 0):
        raise ValueError("confirmed execution receipt mismatch")
    _action(confirmed["action"], "confirmed action")
    if confirmed["status"] in {"executed", "confirmed_local"} \
            and confirmed["action"] != record["selected_action"]:
        raise ValueError("selected action differs from confirmed execution")
    return record
