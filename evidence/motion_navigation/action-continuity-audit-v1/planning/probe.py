"""Read-only audit probes; writes only the planning audit directory."""
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.motion_nav.test_action_continuity_formal import ActionContinuityFormalTests, _gap_case
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.action_route import JumpGapSegment, WalkSegment
from mc2p.motion_nav.motion_coordination import _planned_gap_request
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.geometry import query_support, sweep


captured = {}

def control(context):
    context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
    active = context.driver.session.active_route
    if active is not None and any(type(a) is JumpGapSegment for a in active.action_route.actions):
        captured.setdefault('route', active)
    return ()


result, worker, metrics = ActionContinuityFormalTests()._run(_gap_case(), control_step=control)
active = captured['route']
gap_index = next(i for i, a in enumerate(active.action_route.actions) if type(a) is JumpGapSegment)
action = active.action_route.actions[gap_index]
job = worker.jobs[0]
window = CandidateExecutionWindow(job.anchor.movement_tick_id + 1, job.anchor.movement_tick_id + 2)
original_request = _planned_gap_request(active, gap_index, job.anchor, window)[0]
following = active.action_route.actions[gap_index + 1]
assert type(following) is WalkSegment
assert following.transition is not None
slower_transition = replace(following.transition,
    entry=replace(following.transition.entry, maximum_speed_blocks_per_second=.1))
slower_actions = tuple(replace(a, transition=slower_transition) if i == gap_index + 1 else a
    for i, a in enumerate(active.action_route.actions))
slower_active = replace(active, action_route=replace(active.action_route, actions=slower_actions))
slower_request = _planned_gap_request(slower_active, gap_index, job.anchor, window)[0]

landing = original_request.landing
probe_state = replace(job.anchor.physics_state,
    position=(action.end_surface.position[0], action.end_surface.position[1], action.end_surface.region.max_z + .05),
    on_ground=True)
support = query_support(probe_state.body_box, job.world._world)
clearance = sweep(probe_state.body_box, (0., 0., 0.), job.world._world)
rows = result.trace
jump_i = next(i for i, r in enumerate(rows) if r['applied_movement']['jump'])
land_i = next(i for i in range(jump_i + 1, len(rows)) if rows[i]['on_ground'])
proofs = [r.solve_result.proof for r in worker.results if r.solve_result.proof is not None]

data = {
    'formal_gap': {'outcome': result.outcome, 'reason': result.reason,
        'violations': result.violations, 'metrics': asdict(metrics),
        'timeline': [{k: r[k] for k in ('movement_tick', 'position', 'velocity', 'on_ground',
            'applied_movement', 'session_reason')} for r in rows[jump_i-1:land_i+3]],
        'request_landing': asdict(landing),
        'proofs': [{'entry': p.entry_state.position, 'exit': p.exit_state.position,
            'exit_speed': 20 * math.hypot(p.exit_state.velocity_blocks_per_tick[0], p.exit_state.velocity_blocks_per_tick[2]),
            'commands': [asdict(c.movement) for c in p.commands]} for p in proofs]},
    'next_entry_information_probe': {
        'original_next_max_speed': following.transition.entry.maximum_speed_blocks_per_second,
        'modified_next_max_speed': slower_transition.entry.maximum_speed_blocks_per_second,
        'requests_equal': original_request == slower_request,
        'kind': 'typed request construction only; modified contract was not executed'},
    'continuous_landing_support_probe': {
        'position': probe_state.position, 'support_status': support.status.value,
        'support_fraction': support.support_fraction, 'clearance_status': clearance.status.value,
        'accepted_by_old_landing_region': landing.contains(probe_state),
        'kind': 'geometry query only; no trajectory or motion authorization'},
}
out = Path(__file__).with_name('probe-results.json')
out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps({k: v for k, v in data.items() if k != 'formal_gap'}, ensure_ascii=False))
print(json.dumps({'formal_gap': data['formal_gap']['outcome'], 'metrics': asdict(metrics),
    'jump_speed': 20 * math.hypot(rows[jump_i-1]['velocity'][0], rows[jump_i-1]['velocity'][2]),
    'landing_speed': 20 * math.hypot(rows[land_i]['velocity'][0], rows[land_i]['velocity'][2]),
    'output': str(out)}, ensure_ascii=False))
