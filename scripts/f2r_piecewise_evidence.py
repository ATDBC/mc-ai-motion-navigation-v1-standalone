"""Freeze and run F2-R geometry, clutter, and original-goal formal chains."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.f2_ground_route_evidence import digest, terminal_controller_evidence, timing_summary
from tests.sim.f2r_cases import materialized_manifest, goal_for, scenario_for
from tests.sim.backend import CalculatorBackend, Scene, Perturbations
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_point_in_region, standable_region_in_goal

MANIFEST = ROOT/'tests/sim/manifests/navigation-product-r28-v8.json'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', 'utf-8')


def freeze():
    # The versioned parameter file is the frozen source; never expand it on disk.
    manifest = materialized_manifest()
    return {'tasks': len(manifest['tasks']), 'clutter_scan': len(manifest['clutter_scan']),
            'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest()}


def geometry(case):
    scenario = scenario_for(case)
    backend = CalculatorBackend([0], scenario.scene, scenario.start, scenario.yaw_degrees)
    world = backend.world._world
    goal = Aabb(*case['goal_box'])
    surface = query_support_surfaces(world, math.floor(case['goal'][0]), math.floor(case['goal'][2]), 64., 64.).surfaces[0]
    point = standable_point_in_region(world, surface, goal)
    selected = standable_region_in_goal(world, surface, goal, connection_from=scenario.start)
    return {'id': case['id'], 'family': case['family'], 'target': case['target'], 'input_sha256': digest(case),
        'point_status': point.status.value, 'region_status': selected.status.value,
        'completion_region': None if selected.completion_region is None else asdict(selected.completion_region)}


def clutter(case):
    solids = {(x, 63, z): 'minecraft:stone' for x in range(-6, 7) for z in range(-6, 7)}
    solids.update({(x, y, z): 'minecraft:stone' for x, z in case['posts'] for y in (64, 65, 66)})
    world = CalculatorBackend([0], Scene(solids, ((-8, 8), (60, 68), (-8, 8))), (.5, 64., .5), 0.).world._world
    cx, cz = case['column']
    surface = query_support_surfaces(world, cx, cz, 64., 64.).surfaces[0]
    point = standable_point_in_region(world, surface, Aabb(*case['goal_box']))
    selected = standable_region_in_goal(world, surface, Aabb(*case['goal_box']), connection_from=(.5, 64., .5))
    return {'id': case['id'], 'family': case['family'], 'target': case['target'], 'input_sha256': digest(case),
        'point_status': point.status.value, 'region_status': selected.status.value,
        'lost': point.status.value == 'feasible' and selected.status.value != 'feasible'}


def formal(case):
    import tests.sim.runner as runner
    scenario = scenario_for(case)
    base = goal_for(scenario.goal, case['target'])
    frozen_goal = replace(base, region=Aabb(*case['goal_box']))
    with patch.object(runner, '_goal', lambda position, risk_policy_id='no_expected_damage': frozen_goal):
        if case.get('condition') == 'first_late':
            normal = runner.run(scenario)
            first = next((row['movement_tick'] for row in normal.trace
                if row['applied_movement']['forward'] or row['applied_movement']['strafe']), None)
            if first is not None:
                scenario = replace(scenario, perturbations=Perturbations(late_ticks=frozenset({first})))
        with terminal_controller_evidence() as evidence:
            result = runner.run(scenario)
    completion = next((row for row in result.trace if row.get('session_state') == 'complete' and row.get('goal_satisfied')), None)
    frames = evidence['frames']
    last = result.trace[-1] if result.trace else {}
    from scripts.navigation_coordination_metrics import trace_signatures
    return {'id': case['id'], 'family': case['family'], 'target': case['target'],
        'input_sha256': digest(case), 'outcome': result.outcome, 'reason': result.reason,
        **trace_signatures(result.trace),
        'success': result.outcome == 'success', 'violations': result.violations,
        'verification_complete': result.verification_complete, 'final_position': result.final_position,
        'source_released': last.get('source_bound') is False,
        'final_body': {k: last.get(k) for k in ('position', 'velocity', 'pose', 'sneaking',
            'on_ground', 'support_fraction', 'source_bound')},
        'damage': result.damage, 'applied_perturbations': result.applied_perturbations,
        'completion_observation': completion, 'contracts': evidence['contracts'],
        'control_ms': timing_summary([f['control_ms'] for f in frames]),
        'control_samples_ms': [f['control_ms'] for f in frames],
        'full_candidates_max': max((f['full_candidates'] for f in frames), default=0),
        'physics_steps': sum(f['physics_steps'] for f in frames)}


def execute(job):
    mode, case = job
    return {'geometry': geometry, 'clutter': clutter, 'formal': formal, 'clutter-formal': formal}[mode](case)


def collect(output, mode, workers=4, ids=None):
    if output.exists():
        raise FileExistsError(output)
    manifest = materialized_manifest()
    cases = manifest['clutter_scan' if mode.startswith('clutter') else 'tasks']
    if ids:
        cases = [c for c in cases if c['id'] in ids]
        if {c['id'] for c in cases} != set(ids):
            raise ValueError('unknown frozen ID')
    output.mkdir(parents=True)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        with (output/'runs.jsonl').open('w', encoding='utf-8') as stream:
            for row in pool.map(execute, [(mode, c) for c in cases]):
                rows.append(row)
                stream.write(json.dumps(row, sort_keys=True)+'\n')
                if len(rows) % 100 == 0:
                    print(f'{mode}: {len(rows)}/{len(cases)}', flush=True)
    summary = {'mode': mode, 'cases': len(rows), 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'source_worktree_clean': not subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip(),
        'production_sha256': hashlib.sha256((ROOT/'mc2p/motion_nav/support_surfaces.py').read_bytes()).hexdigest(),
        'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        'python': sys.version, 'platform': platform.platform(), 'by_layer': {}}
    for family, target in sorted({(r['family'], r['target']) for r in rows}):
        selected = [r for r in rows if (r['family'], r['target']) == (family, target)]
        summary['by_layer'][family+'/'+target] = {'cases': len(selected),
            'results': dict(Counter((r['outcome'] if mode in ('formal', 'clutter-formal') else r['region_status']) for r in selected)),
            'reasons': dict(Counter(r.get('reason') for r in selected if not r.get('success', True))),
            'safety_events': sum(bool(r.get('violations')) for r in selected),
            'verification_complete': sum(r.get('verification_complete', False) for r in selected),
            'control_ms': timing_summary([v for r in selected for v in r.get('control_samples_ms', [])]),
            'point_feasible': sum(r.get('point_status') == 'feasible' for r in selected),
            'lost': sum(r.get('lost', False) for r in selected),
            'control_p95_ms_max': max((r['control_ms']['p95'] for r in selected if r.get('control_ms') and r['control_ms'].get('p95') is not None), default=None)}
    write(output/'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('freeze', 'geometry', 'clutter', 'formal', 'clutter-formal'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--ids', nargs='+')
    args = parser.parse_args()
    print(json.dumps(freeze() if args.mode == 'freeze' else collect(args.output, args.mode, args.workers, args.ids), indent=2))


if __name__ == '__main__':
    main()
