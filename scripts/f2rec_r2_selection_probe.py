"""Windows selector cost and exact F2-R preference comparison (test oracle only)."""
from __future__ import annotations
import argparse
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import platform
import time
from types import SimpleNamespace
from unittest.mock import patch

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_point_in_region
from mc2p.motion_nav.world_model import Aabb
from tests.sim.backend import CalculatorBackend
from tests.sim.f2r_cases import goal_for, materialized_manifest, scenario_for


def original_selector(frame, goal):
    """Frozen 686cc0a preference oracle; it does not authorize real inputs."""
    candidates, missing = [], set()
    for x in range(math.floor(goal.region.min_x), math.floor(math.nextafter(goal.region.max_x, -math.inf)) + 1):
        for z in range(math.floor(goal.region.min_z), math.floor(math.nextafter(goal.region.max_z, -math.inf)) + 1):
            result = query_support_surfaces(frame.world, x, z, goal.region.min_y, goal.region.max_y,
                                            collect_complete_missing=True)
            missing.update(result.missing_cells)
            for surface in result.surfaces:
                point = standable_point_in_region(frame.world, surface, goal.region)
                missing.update(point.missing_cells)
                if point.status is QueryStatus.FEASIBLE:
                    candidates.append((surface, point.position))
    if missing or not candidates:
        return None, tuple(sorted(missing))
    center = tuple((lo + hi) / 2 for lo, hi in zip(goal.region.as_tuple()[:3], goal.region.as_tuple()[3:]))
    return min(candidates, key=lambda item: math.dist(item[1], center))[0].node_id, ()


def statistics(values):
    values = sorted(values)
    return {"samples": len(values), "p50": values[(len(values)-1)//2],
            "p95": values[math.ceil(len(values)*.95)-1],
            "p99": values[math.ceil(len(values)*.99)-1], "maximum": values[-1]}



def revision_frames():
    """Measure revise + prepare on Runtime's formal bridge in clutter."""
    from mc2p.contracts.behavior import BehaviorProfileV0
    from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
    from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
    import tests.sim.runner as runner

    cases = [row for row in materialized_manifest()['clutter_scan']
             if row['target'] == 'follow' and row['scene_index'] == 0
             and row['sample'] in (0, 1, 3)]
    samples, outcomes, backend_samples, full_samples = [], [], [], []
    original_job = runner.planner_worker._execute_job
    original_prepare = RuntimeNavigationDriver.prepare_proposals
    for case in cases:
        scenario = replace(scenario_for(case), max_ticks=80)
        goal = replace(goal_for(scenario.goal, 'follow'), region=Aabb(*case['goal_box']))
        current = {'revision_ns': None, 'planner_ns': 0}
        def measured_job(*args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return original_job(*args, **kwargs)
            finally:
                current['planner_ns'] += time.perf_counter_ns() - start
        accepted = []
        def measured_prepare(driver, *args, **kwargs):
            current['planner_ns'] = 0
            start = time.perf_counter_ns()
            proposal = original_prepare(driver, *args, **kwargs)
            if current['revision_ns'] is not None:
                full = current.pop('revision_ns') + time.perf_counter_ns() - start
                samples.append((full - current['planner_ns'])/1e6)
                full_samples.append(full/1e6)
                backend_samples.append(current['planner_ns']/1e6)
                current['revision_ns'] = None
            return proposal
        def control(context):
            if context.tick <= 10:
                start = time.perf_counter_ns()
                accepted.append(context.driver.replace_goal('goal', context.tick + 1, goal, context.clock[0]))
                current['revision_ns'] = time.perf_counter_ns() - start
            elif context.tick == 11:
                context.driver.stop(BehaviorProfileV0(), 'probe_complete')
                return ()
            context.driver.tick(BehaviorProfileV0(), context.clock[0]+500_000_000)
            return ()
        with patch.object(runner, '_goal', lambda *_args, **_kwargs: goal), \
             patch.object(RuntimeNavigationDriver, 'prepare_proposals', measured_prepare), \
             patch.object(runner.planner_worker, '_execute_job', measured_job):
            result = runner.run(scenario, control_step=control,
                                reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH)
        outcomes.append({'id': case['id'], 'revisions_accepted': sum(accepted),
                         'revisions_requested': len(accepted), 'outcome': result.outcome,
                         'violations': result.violations, 'damage': result.damage})
    summary = statistics(samples)
    return {'cases': outcomes, 'production_revision_prepare_ms': summary,
            'synchronous_test_planner_ms': statistics(backend_samples),
            'full_revision_prepare_ms': statistics(full_samples),
            'gates': {'all_revisions_accepted': all(row['revisions_accepted']==10 for row in outcomes),
                      'zero_safety_events': all(not row['violations'] and not row['damage'] for row in outcomes),
                      'p95': summary['p95'] <= 8, 'p99': summary['p99'] <= 15, 'maximum': summary['maximum'] < 30},
            'timing_boundary': 'replace_goal + prepare_proposals, backend advancement and synchronous test planner job excluded; latter reported separately.'}


def collect():
    rows, follow_ns, changes = [], [], []
    manifest = materialized_manifest()
    for case in manifest['tasks'] + manifest['clutter_scan']:
        scene = scenario_for(case)
        backend = CalculatorBackend([0], scene.scene, scene.start, scene.yaw_degrees)
        frame = SimpleNamespace(world=backend.world._world)
        goal = replace(goal_for(scene.goal, case['target']), region=Aabb(*case['goal_box']))
        old = original_selector(frame, goal)
        started = time.perf_counter_ns()
        selected = NavigationSession._surface_for_goal(frame, goal)
        elapsed = time.perf_counter_ns() - started
        if case['target'] == 'follow' and 'posts' in case:
            follow_ns.append(elapsed)
        if old != selected:
            changes.append({'id': case['id'], 'old_node': None if old[0] is None else asdict(old[0]),
                            'selected_node': None if selected[0] is None else asdict(selected[0]),
                            'old_missing': old[1], 'selected_missing': selected[1]})
        rows.append({'id': case['id'], 'selection_equal': old == selected})
    return {'schema_version': 'mc2p.f2rec-r2-selection-probe.v1', 'platform': platform.platform(),
            'baseline_commit': '686cc0a78c379f2ef87fa3f6201e36fd2c98c49c',
            'comparison': {'tasks': len(rows), 'equal': sum(row['selection_equal'] for row in rows),
                           'changes': changes},
            'follow_clutter_selection_ms': statistics([ns/1e6 for ns in follow_ns]),
            'formal_clutter_revisions': revision_frames(),
            'timing_boundary': 'Selector only, excludes test-world creation; complete production frame uses D061.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--revisions-only', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    report = {'formal_clutter_revisions': revision_frames()} if args.revisions_only else collect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
