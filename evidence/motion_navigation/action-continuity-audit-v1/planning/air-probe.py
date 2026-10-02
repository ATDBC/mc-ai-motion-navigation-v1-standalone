"""Two small formal-path checks of existing air transition requests."""
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.motion_nav.test_action_continuity_formal import ActionContinuityFormalTests
from tests.sim.backend import Scene
from tests.sim.runner import Scenario

checks = []
for name, rise in [('jump-up', 1), ('controlled-drop', -1)]:
    solids = {(x, 63 + (rise if z >= 3 else 0), z): 'minecraft:grass_block'
        for x in range(-2, 3) for z in range(-2, 10)}
    case = Scenario(f'audit-{name}', Scene(solids, ((-2, 2), (59, 70), (-2, 9))),
        (.55, 64., .65), (.5, 64. + rise, 8.5), max_ticks=200)
    result, worker, metrics = ActionContinuityFormalTests()._run(case)
    requests = [asdict(j.request) for j in worker.jobs]
    proofs = []
    for delivered in worker.results:
        p = delivered.solve_result.proof
        if p is not None:
            proofs.append({'entry': p.entry_state.position, 'exit': p.exit_state.position,
                'exit_speed': 20 * math.hypot(p.exit_state.velocity_blocks_per_tick[0], p.exit_state.velocity_blocks_per_tick[2]),
                'command_count': len(p.commands),
                'commands': [asdict(c.movement) for c in p.commands]})
    check = {'case': name, 'outcome': result.outcome, 'reason': result.reason,
        'violations': result.violations, 'ticks': len(result.trace),
        'requests': requests, 'proofs': proofs,
        'boundary_trace': [{k: r[k] for k in ('movement_tick', 'position', 'velocity',
            'on_ground', 'applied_movement', 'session_reason')} for r in result.trace
            if 1.9 <= r['position'][2] <= 4.0]}
    checks.append(check)
    print(json.dumps({k: check[k] for k in ('case', 'outcome', 'reason', 'violations', 'ticks')}, ensure_ascii=False))
    if result.outcome != 'success' or result.violations:
        break
Path(__file__).with_name('air-probe-results.json').write_text(
    json.dumps(checks, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
