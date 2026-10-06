"""Persist D058 route-revalidation and full F1-C preparation timings."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Callable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.route_admission import ActiveRouteTracker
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidation,
    ActiveRouteValidationDisposition,
    ActiveRouteValidationIdentity,
    ActiveRouteValidationReason,
    RouteValidationBudget,
    WalkValidationQueryKind,
)
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldQueryCache,
    WorldSessionId,
)
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.motion_nav.test_b07_step_transition import (
    frame as make_frame,
    profile as step_profile,
)
from tests.motion_nav.test_b07_surface_planning import (
    flat_surface_world,
    ordinary_profile,
)
from tests.motion_nav.test_d058_validation_plan import _admit, _candidate
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _ground_anchor
from tests.sim.known_world_following import (
    SCENARIOS as F1_SCENARIOS,
    run_manifest,
    run_scenario,
)


SCHEMA_VERSION = "mc2p.d058-route-revalidation-performance.v1"
GROUP_P95_LIMIT_MS = 1.0
PREPARE_P95_LIMIT_MS = 8.0
PREPARE_P99_LIMIT_MS = 15.0
PREPARE_MAXIMUM_LIMIT_MS = 30.0
DEFAULT_WARMUP = 100
DEFAULT_SAMPLES = 1000
DEFAULT_PREPARE_DISCARD = 100
DEFAULT_MINIMUM_PREPARE_SAMPLES = 1000
GC_DIAGNOSTIC_CAPACITY = 4096


@dataclass(frozen=True, slots=True)
class ExpectedValidation:
    identity: ActiveRouteValidationIdentity
    disposition: ActiveRouteValidationDisposition
    reason: ActiveRouteValidationReason
    queries_used: int


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    scope: str
    invoke: Callable[[], tuple[ActiveRouteValidation, ...]]
    expected: tuple[ExpectedValidation, ...]


def _nearest_rank(values: list[int], quantile: float) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(quantile * len(ordered)) - 1]


def _statistics(values: list[int]) -> dict:
    if not values:
        raise ValueError("performance statistics require samples")
    return {
        "samples": len(values),
        "p50_ns": _nearest_rank(values, .50),
        "p95_ns": _nearest_rank(values, .95),
        "p99_ns": _nearest_rank(values, .99),
        "maximum_ns": max(values),
        "p50_ms": _nearest_rank(values, .50) / 1_000_000,
        "p95_ms": _nearest_rank(values, .95) / 1_000_000,
        "p99_ms": _nearest_rank(values, .99) / 1_000_000,
        "maximum_ms": max(values) / 1_000_000,
    }


def _jsonable(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _expected(
    tracker: ActiveRouteTracker,
    *,
    disposition: ActiveRouteValidationDisposition,
    reason: ActiveRouteValidationReason,
    queries_used: int,
) -> ExpectedValidation:
    return ExpectedValidation(
        tracker.identity, disposition, reason, queries_used,
    )


def _assert_validation(
    case_name: str,
    actual: tuple[ActiveRouteValidation, ...],
    expected: tuple[ExpectedValidation, ...],
) -> None:
    if len(actual) != len(expected):
        raise AssertionError(
            f"{case_name}: validation count {len(actual)} != {len(expected)}"
        )
    for index, (result, frozen) in enumerate(zip(actual, expected)):
        if type(result) is not ActiveRouteValidation:
            raise AssertionError(
                f"{case_name}[{index}]: result is not typed validation"
            )
        observed = (
            result.disposition,
            result.reason,
            result.queries_used,
            result.identity,
        )
        wanted = (
            frozen.disposition,
            frozen.reason,
            frozen.queries_used,
            frozen.identity,
        )
        if observed != wanted:
            raise AssertionError(
                f"{case_name}[{index}]: {observed!r} != {wanted!r}"
            )


def _plain_route(request_id: str):
    world = flat_surface_world(5)
    request, candidate = _candidate(
        world, (1, 1), (3, 1), request_id=request_id,
    )
    route = _admit(
        world, request, candidate, candidate.path[0].position,
    )
    return world, candidate, route


def _complex_shape_route():
    session = WorldSessionId("d058-benchmark-complex-shape")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(
        session, 1, 1, "d058-benchmark-clock", 50_000_000,
    )
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-1, 6)
        for y in range(-1, 4)
        for z in range(-1, 4)
    ))
    slab = BlockGeometry(
        "minecraft:smooth_stone_slab", "boxes",
        (Aabb(0, 0, 0, 1, .5, 1),),
    )
    world.observe_blocks(stamp, {
        (x, 0, z): slab
        for x in range(5)
        for z in range(3)
    })
    request, candidate = _candidate(
        world, (1, 1), (2, 1),
        request_id="d058-benchmark-complex-shape",
    )
    route = _admit(
        world, request, candidate, candidate.path[0].position,
    )
    return world, route


def _tracker_case(
    name: str,
    scope: str,
    world: WorldKnowledge,
    route,
    changed: tuple[int, int, int],
    *,
    disposition: ActiveRouteValidationDisposition,
    reason: ActiveRouteValidationReason,
    queries_used: int,
    ground_profile=True,
) -> BenchmarkCase:
    tracker = ActiveRouteTracker(route)
    view = world.view()

    def invoke() -> tuple[ActiveRouteValidation, ...]:
        kwargs = {}
        if ground_profile:
            kwargs.update(
                ground_profile=ordinary_profile(),
                query_cache=WorldQueryCache(view),
                budget=RouteValidationBudget(4),
            )
        return (tracker.validate(view, (changed,), **kwargs),)

    return BenchmarkCase(
        name,
        scope,
        invoke,
        (_expected(
            tracker,
            disposition=disposition,
            reason=reason,
            queries_used=queries_used,
        ),),
    )


def _route_control(route, initial) -> RouteControl:
    executor = ActionRouteExecutor(
        ordinary_profile(), jump_profile(), step_profile(),
    )
    executor.start(route.action_route, initial)
    return RouteControl(route, executor)


def _supervisor_case(*, pending: bool) -> BenchmarkCase:
    world, candidate, route = _plain_route(
        "d058-benchmark-supervisor",
    )
    shared = next(
        item.position
        for item in route.validation_plan.dependency_provenance
        if len(item.owner_refs) == 2
    )
    initial = make_frame(world, 3, candidate.path[0].position)
    current = replace(initial, changed_cells=(shared,))
    ledger = InputApplicationLedger()
    anchor = _ground_anchor(initial)
    supervisor = ExecutionSupervisor()
    incumbent = _route_control(route, initial)
    if not supervisor.offer_route(incumbent, initial, ledger, anchor):
        raise AssertionError("benchmark incumbent route was refused")
    controls = [incumbent]
    if pending:
        pending_route = replace(
            route,
            route_id="d058-benchmark-pending",
            source_request_id="d058-benchmark-pending-request",
        )
        pending_control = _route_control(pending_route, initial)
        if not supervisor.offer_route(
            pending_control, initial, ledger, anchor,
        ):
            raise AssertionError("benchmark pending route was refused")
        controls.append(pending_control)

    def invoke() -> tuple[ActiveRouteValidation, ...]:
        result = supervisor._validate_routes(current)
        validations = (result.incumbent, result.pending)
        return tuple(item for item in validations if item is not None)

    expected = tuple(
        _expected(
            control.tracker,
            disposition=ActiveRouteValidationDisposition.CONTINUE,
            reason=ActiveRouteValidationReason.REVALIDATED,
            queries_used=2,
        )
        for control in controls
    )
    return BenchmarkCase(
        (
            "incumbent_pending_four_replays"
            if pending else "single_incumbent_two_replays"
        ),
        (
            "ExecutionSupervisor incumbent then pending under one four-query "
            "frame budget"
            if pending else
            "ExecutionSupervisor single incumbent with one shared dependency"
        ),
        invoke,
        expected,
    )


def _cases() -> tuple[BenchmarkCase, ...]:
    world, _, route = _plain_route("d058-benchmark-unaffected")
    unrelated = _tracker_case(
        "nonempty_no_intersection",
        "ActiveRouteTracker nonempty changed set with no dependency intersection",
        world,
        route,
        (99, 99, 99),
        disposition=ActiveRouteValidationDisposition.UNAFFECTED,
        reason=ActiveRouteValidationReason.NO_INTERSECTION,
        queries_used=0,
        ground_profile=False,
    )

    world, _, route = _plain_route("d058-benchmark-surface-edge")
    surface_recipe = next(
        recipe for recipe in route.validation_plan.recipes
        if recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE
    )
    surface_edge = _tracker_case(
        "surface_edge_one_replay",
        "ActiveRouteTracker exact SURFACE_EDGE replay with real geometry",
        world,
        route,
        surface_recipe.dependencies[0],
        disposition=ActiveRouteValidationDisposition.CONTINUE,
        reason=ActiveRouteValidationReason.REVALIDATED,
        queries_used=1,
    )

    world, candidate, _ = _plain_route("d058-benchmark-standable-base")
    request, candidate = _candidate(
        world, (1, 1), (3, 1),
        request_id="d058-benchmark-standable-connection",
    )
    start = candidate.path[0].position
    route = _admit(
        world, request, candidate,
        (start[0] + .451, start[1], start[2]),
    )
    initial = route.validation_plan.initial_connection
    if initial is None:
        raise AssertionError("benchmark standable connection was not admitted")
    initial_owner = route.validation_plan.owner(initial.owner_ref)
    standable_recipe = route.validation_plan.recipe(
        initial_owner.recipe_ref,
    )
    if (standable_recipe.query_kind
            is not WalkValidationQueryKind.STANDABLE_CONNECTION):
        raise AssertionError("benchmark did not build standable recipe")
    standable = _tracker_case(
        "standable_connection_one_replay",
        "ActiveRouteTracker exact STANDABLE_CONNECTION replay with real geometry",
        world,
        route,
        standable_recipe.dependencies[0],
        disposition=ActiveRouteValidationDisposition.CONTINUE,
        reason=ActiveRouteValidationReason.REVALIDATED,
        queries_used=1,
    )

    complex_world, complex_route = _complex_shape_route()
    complex_recipe = complex_route.validation_plan.recipes[0]
    if complex_recipe.query_kind is not WalkValidationQueryKind.SURFACE_EDGE:
        raise AssertionError("complex shape did not build a surface recipe")
    complex_shape = _tracker_case(
        "complex_shape_one_replay",
        "ActiveRouteTracker SURFACE_EDGE replay over slab AABB geometry",
        complex_world,
        complex_route,
        complex_recipe.dependencies[0],
        disposition=ActiveRouteValidationDisposition.CONTINUE,
        reason=ActiveRouteValidationReason.REVALIDATED,
        queries_used=1,
    )

    return (
        unrelated,
        surface_edge,
        standable,
        complex_shape,
        _supervisor_case(pending=False),
        _supervisor_case(pending=True),
    )


def _measure_case(
    case: BenchmarkCase,
    *,
    warmup: int,
    samples: int,
) -> dict:
    elapsed: list[int] = []
    for index in range(warmup + samples):
        started = time.perf_counter_ns()
        result = case.invoke()
        duration = time.perf_counter_ns() - started
        _assert_validation(case.name, result, case.expected)
        if index >= warmup:
            elapsed.append(duration)
    statistics = _statistics(elapsed)
    return {
        "scope": case.scope,
        "expected": [
            {
                "disposition": item.disposition.value,
                "reason": item.reason.value,
                "queries_used": item.queries_used,
                "identity": _jsonable(asdict(item.identity)),
            }
            for item in case.expected
        ],
        "raw_samples_ns": elapsed,
        "statistics": statistics,
        "gates": {
            "p95_ms_max": GROUP_P95_LIMIT_MS,
            "p95_passed": statistics["p95_ms"] <= GROUP_P95_LIMIT_MS,
        },
    }


def _measure_prepare(
    *,
    scenario_names: tuple[str, ...] | None,
    discard: int,
    minimum_samples: int,
    gc_diagnostic: bool = False,
) -> dict:
    import gc as scenario_gc

    elapsed: list[int] = []
    scenario_ranges: list[dict] = []
    original = RuntimeNavigationDriver.prepare_proposals
    gc_event = None
    gc_module = None

    if gc_diagnostic:
        gc_module = scenario_gc
        from array import array

        no_sample = (1 << 64) - 1
        current_sample = no_sample
        active_sample = no_sample
        active_generation = 0
        active_started_ns = 0
        event_count = 0
        overflowed = False
        event_samples = array("Q", [0]) * GC_DIAGNOSTIC_CAPACITY
        event_generations = bytearray(GC_DIAGNOSTIC_CAPACITY)
        event_starts = array("Q", [0]) * GC_DIAGNOSTIC_CAPACITY
        event_ends = array("Q", [0]) * GC_DIAGNOSTIC_CAPACITY
        event_collected = array("Q", [0]) * GC_DIAGNOSTIC_CAPACITY

        def gc_event(phase, info):
            nonlocal active_sample, active_generation, active_started_ns
            nonlocal event_count, overflowed
            if phase == "start":
                if current_sample == no_sample:
                    active_sample = no_sample
                    return
                active_sample = current_sample
                active_generation = info.get("generation", 0)
                active_started_ns = time.perf_counter_ns()
                return
            if phase != "stop" or active_sample == no_sample:
                return
            ended_ns = time.perf_counter_ns()
            if event_count >= GC_DIAGNOSTIC_CAPACITY:
                overflowed = True
            else:
                event_samples[event_count] = active_sample
                event_generations[event_count] = active_generation
                event_starts[event_count] = active_started_ns
                event_ends[event_count] = ended_ns
                event_collected[event_count] = info.get("collected", 0)
                event_count += 1
            active_sample = no_sample

        def measured(driver, *args, **kwargs):
            nonlocal current_sample
            current_sample = len(elapsed)
            started = time.perf_counter_ns()
            try:
                return original(driver, *args, **kwargs)
            finally:
                duration = time.perf_counter_ns() - started
                current_sample = no_sample
                elapsed.append(duration)
    else:
        def measured(driver, *args, **kwargs):
            started = time.perf_counter_ns()
            try:
                return original(driver, *args, **kwargs)
            finally:
                elapsed.append(time.perf_counter_ns() - started)

    selected_names = (
        tuple(scenario_names) if scenario_names is not None
        else tuple(scenario.name for scenario in F1_SCENARIOS)
    )
    run_scenario.cache_clear()
    initial_gc_collected = scenario_gc.collect(2)
    results = []
    try:
        if gc_diagnostic:
            assert gc_module is not None and gc_event is not None
            gc_module.callbacks.append(gc_event)
        with patch.object(
            RuntimeNavigationDriver, "prepare_proposals", measured,
        ):
            for scenario_ordinal, scenario_name in enumerate(selected_names):
                captured_start = len(elapsed)
                single = run_manifest((scenario_name,))
                results.extend(single["results"])
                captured_end = len(elapsed) - 1
                run_scenario.cache_clear()
                collected = scenario_gc.collect(2)
                scenario_ranges.append({
                    "scenario": scenario_name,
                    "scenario_ordinal": scenario_ordinal,
                    "captured_start_ordinal": captured_start,
                    "captured_end_ordinal": captured_end,
                    "captured_samples": captured_end - captured_start + 1,
                    "gc_collected_after": collected,
                })
    finally:
        if (gc_diagnostic and gc_module is not None
                and gc_event in gc_module.callbacks):
            gc_module.callbacks.remove(gc_event)
        run_scenario.cache_clear()
    failed_scenarios = [
        item["scenario"] for item in results if not item["passed"]
    ]
    report = {
        "scenario_order": list(selected_names),
        "results": results,
        "failed_scenarios": failed_scenarios,
        "f1_c_complete": not failed_scenarios,
    }
    if report["failed_scenarios"] or not report["f1_c_complete"]:
        raise AssertionError(
            "F1-C manifest failed during preparation benchmark: "
            f"{report['failed_scenarios']}"
        )
    retained = elapsed[discard:]
    if len(retained) < minimum_samples:
        raise RuntimeError(
            "F1-C preparation benchmark has too few retained samples: "
            f"{len(retained)} < {minimum_samples} "
            f"after discarding {discard} of {len(elapsed)}"
        )
    statistics = _statistics(retained)
    for item in scenario_ranges:
        captured_start = item["captured_start_ordinal"]
        captured_end = item["captured_end_ordinal"]
        retained_captured_start = max(captured_start, discard)
        if retained_captured_start > captured_end:
            item.update(
                retained_start_ordinal=None,
                retained_end_ordinal=None,
                retained_samples=0,
            )
        else:
            item.update(
                retained_start_ordinal=retained_captured_start - discard,
                retained_end_ordinal=captured_end - discard,
                retained_samples=captured_end - retained_captured_start + 1,
            )
    gates = {
        "p95_ms_max": PREPARE_P95_LIMIT_MS,
        "p99_ms_max": PREPARE_P99_LIMIT_MS,
        "maximum_ms_exclusive": PREPARE_MAXIMUM_LIMIT_MS,
        "p95_passed": statistics["p95_ms"] <= PREPARE_P95_LIMIT_MS,
        "p99_passed": statistics["p99_ms"] <= PREPARE_P99_LIMIT_MS,
        "maximum_passed": (
            statistics["maximum_ms"] < PREPARE_MAXIMUM_LIMIT_MS
        ),
    }
    result = {
        "scope": (
            "RuntimeNavigationDriver.prepare_proposals around the existing "
            "F1-C formal manifest; includes Session preparation and inline "
            "test workers, excludes Runtime control_frame, game/IPC, and "
            "trace serialization"
        ),
        "scenario_order": report["scenario_order"],
        "scenario_results": [
            {
                "scenario": item["scenario"],
                "passed": item["passed"],
                "observed_ticks": item["observed_ticks"],
            }
            for item in report["results"]
        ],
        "scenario_sample_ranges": scenario_ranges,
        "initial_gc_collected": initial_gc_collected,
        "captured_samples": len(elapsed),
        "discarded_prefix_samples": discard,
        "retained_samples": len(retained),
        "minimum_retained_samples": minimum_samples,
        "raw_samples_ns": retained,
        "statistics": statistics,
        "gates": gates,
    }
    if gc_diagnostic:
        result["gc_diagnostic"] = {
            "clock": "time.perf_counter_ns",
            "sample_ordinal_basis": "zero_based_captured_prepare_sample",
            "capacity": GC_DIAGNOSTIC_CAPACITY,
            "event_count": event_count,
            "overflowed": overflowed,
            "events": [
                {
                    "sample_ordinal": event_samples[index],
                    "generation": event_generations[index],
                    "start_ns": event_starts[index],
                    "end_ns": event_ends[index],
                    "duration_ns": event_ends[index] - event_starts[index],
                    "collected": event_collected[index],
                }
                for index in range(event_count)
            ],
        }
    return result

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT,
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _source_identity() -> dict:
    paths = (
        "scripts/benchmark_d058_route_revalidation.py",
        "mc2p/motion_nav/route_validation.py",
        "mc2p/motion_nav/route_admission.py",
        "mc2p/motion_nav/route_body_controller.py",
        "mc2p/motion_nav/execution_supervisor.py",
        "mc2p/skills/navigation_session_driver.py",
        "tests/sim/known_world_following.py",
    )
    digest = hashlib.sha256()
    files = []
    for relative in paths:
        content = (ROOT / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        files.append({
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    status = _git("status", "--porcelain", "--untracked-files=normal")
    return {
        "commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "git_status": status.splitlines(),
        "scope_sha256": digest.hexdigest(),
        "files": files,
    }


def run_benchmark(
    *,
    warmup: int = DEFAULT_WARMUP,
    samples: int = DEFAULT_SAMPLES,
    scenario_names: tuple[str, ...] | None = None,
    prepare_discard: int = DEFAULT_PREPARE_DISCARD,
    minimum_prepare_samples: int = DEFAULT_MINIMUM_PREPARE_SAMPLES,
    gc_diagnostic: bool = False,
) -> dict:
    groups = {
        case.name: _measure_case(
            case, warmup=warmup, samples=samples,
        )
        for case in _cases()
    }
    preparation = _measure_prepare(
        scenario_names=scenario_names,
        discard=prepare_discard,
        minimum_samples=minimum_prepare_samples,
        gc_diagnostic=gc_diagnostic,
    )
    group_gates_passed = all(
        item["gates"]["p95_passed"] for item in groups.values()
    )
    prepare_gates_passed = all(
        preparation["gates"][key]
        for key in ("p95_passed", "p99_passed", "maximum_passed")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": _source_identity(),
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "clock": "time.perf_counter_ns",
        },
        "configuration": {
            "route_group_warmup_iterations": warmup,
            "route_group_measured_iterations": samples,
            "prepare_discarded_prefix_samples": prepare_discard,
            "prepare_minimum_retained_samples": minimum_prepare_samples,
            "f1_scenarios": (
                list(scenario_names) if scenario_names is not None
                else [scenario.name for scenario in F1_SCENARIOS]
            ),
        },
        "route_revalidation_groups": groups,
        "complete_prepare": preparation,
        "gates": {
            "route_revalidation_p95_passed": group_gates_passed,
            "complete_prepare_passed": prepare_gates_passed,
            "all_passed": group_gates_passed and prepare_gates_passed,
        },
    }


def _command(arguments) -> str:
    command = [
        sys.executable,
        "-m",
        "scripts.benchmark_d058_route_revalidation",
        "--output",
        str(arguments.output),
    ]
    if arguments.warmup != DEFAULT_WARMUP:
        command.extend(("--warmup", str(arguments.warmup)))
    if arguments.samples != DEFAULT_SAMPLES:
        command.extend(("--samples", str(arguments.samples)))
    for scenario_name in arguments.f1_scenario or ():
        command.extend(("--f1-scenario", scenario_name))
    if arguments.prepare_discard != DEFAULT_PREPARE_DISCARD:
        command.extend(("--prepare-discard", str(arguments.prepare_discard)))
    if (arguments.minimum_prepare_samples
            != DEFAULT_MINIMUM_PREPARE_SAMPLES):
        command.extend((
            "--minimum-prepare-samples",
            str(arguments.minimum_prepare_samples),
        ))
    if arguments.gc_diagnostic:
        command.append("--gc-diagnostic")
    return subprocess.list2cmdline(command)


def _write_evidence(output: Path, payload: dict, command: str) -> None:
    output.mkdir(parents=True)
    performance = output / "performance.json"
    command_file = output / "COMMAND.txt"
    performance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    command_file.write_text(command + "\n", encoding="utf-8")
    sums = output / "SHA256SUMS"
    sums.write_text(
        "".join(
            f"{_sha256(path)}  {path.name}\n"
            for path in (command_file, performance)
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument(
        "--f1-scenario",
        action="append",
        choices=tuple(scenario.name for scenario in F1_SCENARIOS),
    )
    parser.add_argument(
        "--prepare-discard", type=int,
        default=DEFAULT_PREPARE_DISCARD,
    )
    parser.add_argument(
        "--minimum-prepare-samples", type=int,
        default=DEFAULT_MINIMUM_PREPARE_SAMPLES,
    )
    parser.add_argument("--gc-diagnostic", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.output.exists():
        parser.error(
            f"refusing to overwrite benchmark evidence: {arguments.output}"
        )
    if arguments.warmup < 0:
        parser.error("--warmup must be nonnegative")
    if arguments.samples < 1:
        parser.error("--samples must be positive")
    if arguments.prepare_discard < 0:
        parser.error("--prepare-discard must be nonnegative")
    if arguments.minimum_prepare_samples < 1:
        parser.error("--minimum-prepare-samples must be positive")
    payload = run_benchmark(
        warmup=arguments.warmup,
        samples=arguments.samples,
        scenario_names=(
            None if arguments.f1_scenario is None
            else tuple(arguments.f1_scenario)
        ),
        prepare_discard=arguments.prepare_discard,
        minimum_prepare_samples=arguments.minimum_prepare_samples,
        gc_diagnostic=arguments.gc_diagnostic,
    )
    _write_evidence(arguments.output, payload, _command(arguments))
    print(json.dumps({
        "output": str(arguments.output),
        "route_groups": len(payload["route_revalidation_groups"]),
        "prepare_samples": payload["complete_prepare"]["retained_samples"],
        "passed": payload["gates"]["all_passed"],
    }, sort_keys=True))
    return 0 if payload["gates"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
