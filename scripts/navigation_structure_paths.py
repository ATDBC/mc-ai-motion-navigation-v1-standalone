"""Read-only candidate path counts, in a separate run from behavior signing."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager, ExitStack
from functools import wraps
import importlib
import inspect
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.navigation_s0r_evidence import SETS, case_jobs, representative_jobs, execute_case, signature_schema
from scripts.navigation_structure_inventory import SPECS


def _resolve(target):
    module, *parts = target.split(".")
    owner = importlib.import_module("mc2p.motion_nav." + module)
    for part in parts[:-1]:
        owner = getattr(owner, part)
    return owner, parts[-1]


@contextmanager
def path_probe():
    counts = Counter()
    targets = sorted({target for spec in SPECS for target in spec[3].split("|")})
    with ExitStack() as stack:
        for target in targets:
            owner, name = _resolve(target)
            descriptor = inspect.getattr_static(owner, name)
            original = descriptor.__func__ if isinstance(descriptor, (classmethod, staticmethod)) else descriptor
            def make_wrapper(original, target):
                @wraps(original)
                def observed(*args, **kwargs):
                    counts[target] += 1
                    result = original(*args, **kwargs)
                    if target == "route_validation.replay_walk_validation_recipe":
                        counts[target + ":" + result[0].value] += 1
                    return result
                return observed
            wrapper = make_wrapper(original, target)
            replacement = type(descriptor)(wrapper) if isinstance(descriptor, (classmethod, staticmethod)) else wrapper
            stack.enter_context(patch.object(owner, name, replacement))
            if target == "route_validation.replay_walk_validation_recipe":
                # RouteAdmitter imports this function by value. Its actual
                # call-site binding must be patched, not only the definition.
                admission = importlib.import_module("mc2p.motion_nav.route_admission")
                stack.enter_context(patch.object(admission, name, wrapper))
            if target == "route_validation.query_surface_walk_edge":
                planner = importlib.import_module("mc2p.motion_nav.known_map_planner")
                stack.enter_context(patch.object(planner, "query_surface_walk_edge", wrapper))
                stack.enter_context(patch.object(planner, "_surface_walk_query", wrapper))
        yield counts


def _count_job(task):
    group, job = task
    with path_probe() as counts:
        if group == "product":
            from tests.sim.product_cases import product_scenario
            from tests.sim.motion_delivery import DeterministicMotionWorker
            from tests.sim.runner import run
            manifest, family, seed = job[2]
            scenario, _ = product_scenario(manifest, family, seed)
            delivery = DeterministicMotionWorker(manifest["motion_delivery_profile"])
            result = run(scenario, motion_factory=lambda: delivery, control_step=delivery.control_step)
            assert result.verification_complete and not result.violations
        else:
            result = execute_case(job, sign=False, signature_schema_version=signature_schema(group))
            assert result["passed"], job[0]
    return group, job[0], dict(counts)


def collect_paths(output, workers=4, representative=False):
    if output.exists():
        raise FileExistsError(output)
    started = time.perf_counter()
    tasks = [(group, job) for group in SETS
             for job in (representative_jobs(group) if representative else case_jobs(group))]
    totals = {group: Counter() for group in SETS}
    cases = {group: 0 for group in SETS}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for group, identifier, counts in pool.map(_count_job, tasks):
            totals[group].update(counts)
            cases[group] += 1
            if sum(cases.values()) % 100 == 0:
                print(f"path cases {sum(cases.values())}/{len(tasks)}", flush=True)
    candidates = {spec[0]: {group: sum(totals[group][target] for target in spec[3].split("|"))
                          for group in SETS} for spec in SPECS}
    all_targets = sorted({target for spec in SPECS for target in spec[3].split("|")})
    result = {"schema_version": "mc2p.navigation-structure-path-matrix.v1",
              "behavior_signatures_emitted": False, "timings_are_performance_evidence": False,
              "representative_only": representative, "cases_per_set": cases,
              "elapsed_seconds": time.perf_counter() - started,
              "candidates": candidates, "target_calls": {group: {**{target: counts[target] for target in all_targets},
                                                               **dict(counts)} for group, counts in totals.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--representative", action="store_true")
    args = parser.parse_args(argv)
    result = collect_paths(args.output, args.workers, args.representative)
    print(json.dumps({"cases": result["cases_per_set"], "candidates": len(result["candidates"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
