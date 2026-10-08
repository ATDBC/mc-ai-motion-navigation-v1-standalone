"""Freeze and run F2-R geometry, clutter, and original-goal formal chains."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
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
PRODUCTION_SOURCES = (
    'mc2p/motion_nav/support_surfaces.py',
    'mc2p/motion_nav/ground_terminal_search.py',
    'mc2p/motion_nav/motion_worker.py',
    'mc2p/motion_nav/motion_coordination.py',
    'mc2p/motion_nav/fixed_route.py',
    'mc2p/motion_nav/action_route_executor.py',
    'mc2p/motion_nav/navigation_session.py',
    'mc2p/motion_nav/online_motion.py',
    'mc2p/motion_nav/safe_ground_control.py',
    'mc2p/runtime/player_runtime_v1.py',
    'mc2p/skills/navigation_session_driver.py',
)


def production_source_digests() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        for name in PRODUCTION_SOURCES
    }


def production_source_sha256() -> str:
    encoded = json.dumps(
        production_source_digests(), sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class _ObservedMotionWorker:
    """Evidence wrapper around the real process-isolated motion worker."""

    def __init__(self):
        from mc2p.motion_nav.motion_worker import MotionSolverWorker
        self._worker = MotionSolverWorker(max_pending=8)
        self.activity = []
        self.executor_pids: set[int] = set()

    @property
    def pid(self):
        return self._worker.pid

    @property
    def health(self):
        return self._worker.health

    def is_alive(self):
        return self._worker.is_alive()

    def submit(self, job):
        accepted = self._worker.submit(job)
        if accepted:
            from tests.sim.async_monitor import ObservedAsyncActivity
            self.activity.append(ObservedAsyncActivity(job.work_identity, 'submit'))
        return accepted

    def poll_available(self):
        values = self._worker.poll_available()
        if values:
            from tests.sim.async_monitor import ObservedAsyncActivity
            for result in values:
                self.activity.append(ObservedAsyncActivity(
                    result.work_identity, 'poll',
                ))
                executor_pid = getattr(result, 'executor_pid', None)
                if executor_pid is not None:
                    self.executor_pids.add(executor_pid)
        return values

    def cancel(self, identity, status):
        return self._worker.cancel(identity, status)

    def close(self):
        self._worker.close()


class _ObservedPlannerWorker:
    """Evidence wrapper around the real process-isolated planner worker."""

    def __init__(self):
        from mc2p.motion_nav.planner_worker import PlannerWorker
        self._worker = PlannerWorker()
        self.activity = []

    def is_alive(self):
        return self._worker.is_alive()

    def submit_surface_snapshot(self, *args, **kwargs):
        from mc2p.motion_nav.planner_worker import PlanningSubmissionStatus
        from tests.sim.async_monitor import ObservedAsyncActivity
        request = args[3]
        status = self._worker.submit_surface_snapshot(*args, **kwargs)
        if status is PlanningSubmissionStatus.ACCEPTED:
            self.activity.append(ObservedAsyncActivity(
                request.work_identity, 'submit',
            ))
        return status

    def poll_available(self):
        from tests.sim.async_monitor import ObservedAsyncActivity
        values = self._worker.poll_available()
        for result in values:
            self.activity.append(ObservedAsyncActivity(
                result.work_identity, 'poll',
            ))
        return values

    def poll_latest(self):
        values = self.poll_available()
        return None if not values else values[-1]

    def close(self):
        self._worker.close()


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
        motion_workers = []
        planner_workers = []
        def motion_factory():
            worker = _ObservedMotionWorker()
            motion_workers.append(worker)
            return worker
        def planner_factory():
            worker = _ObservedPlannerWorker()
            planner_workers.append(worker)
            return worker
        def paced_control(context):
            from mc2p.contracts.behavior import BehaviorProfileV0
            # Keep simulated observations at the game's 20 Hz wall cadence so
            # the real worker can deliver without blocking the control call.
            time.sleep(.05)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )
            return ()
        try:
            with terminal_controller_evidence() as evidence:
                result = runner.run(
                    scenario,
                    planner_factory=planner_factory,
                    motion_factory=motion_factory,
                    control_step=paced_control,
                )
        finally:
            for worker in (*planner_workers, *motion_workers):
                worker.close()
    completion = next((row for row in result.trace if row.get('session_state') == 'complete' and row.get('goal_satisfied')), None)
    fixed_route_frames = evidence['frames']
    frames = evidence['runtime_frames']
    segment_labels = sorted({
        label for frame in frames
        for label in frame.get('production_segments_exclusive_ms', {})
    })
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
        'control_process_pid': os.getpid(),
        'motion_worker_pids': [worker.pid for worker in motion_workers],
        'motion_executor_pids': sorted({
            pid for worker in motion_workers for pid in worker.executor_pids
        }),
        'completion_observation': completion, 'contracts': evidence['contracts'],
        'control_ms': timing_summary([f['control_ms'] for f in frames]),
        'control_samples_ms': [f['control_ms'] for f in frames],
        'control_ms_kind': 'mixed_interval',
        'production_prepare_ms': timing_summary([
            f['production_prepare_ms'] for f in frames
        ]),
        'production_prepare_samples_ms': [
            f['production_prepare_ms'] for f in frames
        ],
        'backend_step_ms': timing_summary([
            f['backend_step_ms'] for f in frames
        ]),
        'backend_step_samples_ms': [f['backend_step_ms'] for f in frames],
        'full_frame_ms': timing_summary([
            f['full_frame_ms'] for f in frames
        ]),
        'full_frame_samples_ms': [f['full_frame_ms'] for f in frames],
        'production_segment_samples_ms': {
            label: [
                frame.get('production_segments_exclusive_ms', {}).get(label, 0.0)
                for frame in frames
            ]
            for label in segment_labels
        },
        'production_segment_inclusive_samples_ms': {
            label: [
                frame.get('production_segments_inclusive_ms', {}).get(label, 0.0)
                for frame in frames
            ]
            for label in segment_labels
        },
        'production_unattributed_samples_ms': [
            f.get('production_unattributed_ms', 0.0) for f in frames
        ],
        'timing_boundary': 'direct_intervals_no_subtraction',
        'fixed_route_control_ms': timing_summary([
            f['control_ms'] for f in fixed_route_frames
        ]),
        'full_candidates_max': max((
            f['full_candidates'] for f in fixed_route_frames
        ), default=0),
        'physics_steps': sum(f['physics_steps'] for f in fixed_route_frames)}


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
    with (output/'runs.jsonl').open('w', encoding='utf-8') as stream:
        if mode in ('formal', 'clutter-formal'):
            # Each case owns real planner and motion child processes. Running
            # in this process avoids nesting them under a pool worker.
            iterator = map(execute, [(mode, c) for c in cases])
        else:
            pool = ProcessPoolExecutor(max_workers=workers)
            iterator = pool.map(execute, [(mode, c) for c in cases])
        try:
            for row in iterator:
                rows.append(row)
                stream.write(json.dumps(row, sort_keys=True)+'\n')
                if len(rows) % 100 == 0:
                    print(f'{mode}: {len(rows)}/{len(cases)}', flush=True)
        finally:
            if mode not in ('formal', 'clutter-formal'):
                pool.shutdown()
    summary = {'mode': mode, 'cases': len(rows), 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'source_worktree_clean': not subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip(),
        'production_sha256': production_source_sha256(),
        'production_files': production_source_digests(),
        'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        'python': sys.version, 'platform': platform.platform(), 'by_layer': {}}
    for family, target in sorted({(r['family'], r['target']) for r in rows}):
        selected = [r for r in rows if (r['family'], r['target']) == (family, target)]
        segment_labels = sorted({
            label for row in selected
            for label in row.get('production_segment_samples_ms', {})
        })
        summary['by_layer'][family+'/'+target] = {'cases': len(selected),
            'results': dict(Counter((r['outcome'] if mode in ('formal', 'clutter-formal') else r['region_status']) for r in selected)),
            'reasons': dict(Counter(r.get('reason') for r in selected if not r.get('success', True))),
            'safety_events': sum(bool(r.get('violations')) for r in selected),
            'verification_complete': sum(r.get('verification_complete', False) for r in selected),
            'control_ms': timing_summary([v for r in selected for v in r.get('control_samples_ms', [])]),
            'production_prepare_ms': timing_summary([
                v for r in selected
                for v in r.get('production_prepare_samples_ms', [])
            ]),
            'backend_step_ms': timing_summary([
                v for r in selected
                for v in r.get('backend_step_samples_ms', [])
            ]),
            'full_frame_ms': timing_summary([
                v for r in selected
                for v in r.get('full_frame_samples_ms', [])
            ]),
            'production_segments_exclusive_ms': {
                label: timing_summary([
                    value for row in selected
                    for value in row.get('production_segment_samples_ms', {}).get(label, [])
                ]) for label in segment_labels
            },
            'production_segments_inclusive_ms': {
                label: timing_summary([
                    value for row in selected
                    for value in row.get('production_segment_inclusive_samples_ms', {}).get(label, [])
                ]) for label in segment_labels
            },
            'production_unattributed_ms': timing_summary([
                value for row in selected
                for value in row.get('production_unattributed_samples_ms', [])
            ]),
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
