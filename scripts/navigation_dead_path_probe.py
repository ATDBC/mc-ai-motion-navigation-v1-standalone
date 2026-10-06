"""Independently assert each frozen dead entry across formal cases before deletion."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.navigation_s0r_evidence import SETS, case_jobs, representative_jobs, execute_case, signature_schema

TARGETS = ('navigation_session.NavigationSession._admit_async_event',
           'navigation_session.NavigationSession._cell_fact_id',
           'motion_coordination.MotionRouteCoordinator._upcoming_air_index')
_dead_calls = 0


def asserted_entry(*args, **kwargs):
    global _dead_calls
    _dead_calls += 1
    raise AssertionError('M0 frozen dead path was called')


def _execute(task):
    global _dead_calls
    _dead_calls = 0
    target, group, job = task
    module, classname, name = target.split('.')
    owner = getattr(importlib.import_module('mc2p.motion_nav.' + module), classname)
    with patch.object(owner, name, asserted_entry):
        if group == 'product':
            from tests.sim.product_cases import product_scenario
            from tests.sim.motion_delivery import DeterministicMotionWorker
            from tests.sim.runner import run
            manifest, family, seed = job[2]
            scenario, _ = product_scenario(manifest, family, seed)
            worker = DeterministicMotionWorker(manifest['motion_delivery_profile'])
            result = run(scenario, motion_factory=lambda: worker, control_step=worker.control_step)
            assert result.verification_complete and not result.violations
            assert result.reason != 'AssertionError', result.reason
        else:
            result = execute_case(job, sign=False, signature_schema_version=signature_schema(group))
            assert result['passed'], job[0]
    assert _dead_calls == 0, (target, group, job[0], _dead_calls)
    return group


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=TARGETS, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--representative', action='store_true')
    args = parser.parse_args()
    tasks = [(args.target, group, job) for group in SETS
             for job in (representative_jobs(group) if args.representative else case_jobs(group))]
    counts = {group: 0 for group in SETS}
    with ProcessPoolExecutor(max_workers=4) as pool:
        for group in pool.map(_execute, tasks):
            counts[group] += 1
            if sum(counts.values()) % 250 == 0:
                print(sum(counts.values()), '/', len(tasks), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'target': args.target, 'mutation': 'asserted_entry',
                                      'call_count_checked': True,
                                      'representative_only': args.representative,
                                      'cases': counts, 'calls': 0}, indent=2)+'\n', encoding='utf-8')
    print(counts)


if __name__ == '__main__':
    main()
