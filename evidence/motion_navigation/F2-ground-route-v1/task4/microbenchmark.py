"""Serial original-goal terminal benchmark; measurements never choose inputs."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from scripts.f2_ground_route_evidence import terminal_controller_evidence, timing_summary, digest
from scripts.navigation_coordination_metrics import source_fingerprint, trace_signatures
from tests.sim.product_cases import PLAYER_CASES, player_layout
from tests.sim.runner import Scenario, run


def main(output):
    rows, frames = [], []
    for repetition in range(4):
        for family in PLAYER_CASES:
            scene, start, goal = player_layout(family)
            with terminal_controller_evidence() as actual:
                result = run(Scenario('terminal-benchmark-'+family, scene, start, goal))
            assert result.outcome == 'success', (family, result.reason)
            assert not result.violations and result.verification_complete
            if repetition:
                frames.extend(actual['frames'])
                rows.append({'family': family, 'repeat': repetition,
                    'signature': trace_signatures(result.trace),
                    'contracts': actual['contracts'], 'outcome': result.outcome})
    for family in PLAYER_CASES:
        assert len({digest(r['signature']) for r in rows if r['family']==family}) == 1
    slow = [f for f in frames if f['full_candidates']]
    result = {'warmup_cases': len(PLAYER_CASES), 'cases': len(rows), 'frames': len(frames),
        'all_ms': timing_summary([f['control_ms'] for f in frames]),
        'slow_ms': timing_summary([f['control_ms'] for f in slow]), 'slow_frames': len(slow),
        'maximum_candidates': max(f['full_candidates'] for f in frames),
        'physics_steps': sum(f['physics_steps'] for f in frames),
        'deterministic': True, 'runs': rows,
        'production': source_fingerprint(['mc2p/**/*.py','config/motion-navigation/*.json'])}
    assert result['all_ms']['p95'] <= 8 and result['slow_ms']['p95'] <= 8
    assert result['maximum_candidates'] <= 3
    Path(output).write_text(json.dumps(result, indent=2)+'\n', 'utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'runs','production'}},indent=2))


if __name__ == '__main__':
    main(sys.argv[1])
