"""Small formal-path quality comparison; simulation is not Fabric evidence."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from tests.motion_nav import test_action_continuity_formal as formal
from tests.sim.runner import Scenario, run, lane
from tests.sim.scenarios import columns


def metrics(result):
    rows = result.trace
    jump = next((i for i, row in enumerate(rows) if row['applied_movement']['jump']), None)
    landing = None if jump is None else next((i for i in range(jump + 1, len(rows))
                                             if rows[i]['on_ground']), None)
    aerial = [] if landing is None else rows[jump:landing]
    tail = next((row for row in rows if row['action_index'] == 1
                 and row.get('action_kind') == 'WalkSegment'), None)
    lowest_ground_y = min(row['position'][1] for row in rows if row['on_ground'])
    ground_tail_pause = [row['movement_tick'] for row in rows
                        if jump is None and row['on_ground']
                        and abs(row['position'][1] - lowest_ground_y) < 1.e-7
                        and row.get('action_kind') == 'WalkSegment' and row['action_index'] == 0
                        and not any(row['applied_movement'].values())]
    return dict(outcome=result.outcome, reason=result.reason, ticks=result.ticks,
        violations=result.violations,
        airborne_reverse_ticks=[row['movement_tick'] for row in aerial
                                if row['applied_movement']['forward'] < 0],
        airborne_neutral_ticks=[row['movement_tick'] for row in aerial
                                if not any(row['applied_movement'].values())],
        ground_tail_preparation_neutral_ticks=ground_tail_pause,
        landing_position=None if landing is None else rows[landing]['position'],
        landing_motion_speed_bps=None if landing is None else
            20 * math.hypot(rows[landing]['velocity'][0], rows[landing]['velocity'][2]),
        landing_sample_displacement_speed_bps=None if landing is None else
            20 * math.hypot(rows[landing]['position'][0] - rows[landing-1]['position'][0],
                            rows[landing]['position'][2] - rows[landing-1]['position'][2])
               / (rows[landing]['movement_tick'] - rows[landing-1]['movement_tick']),
        first_tail_motion_speed_bps=None if tail is None else
            20 * math.hypot(tail['velocity'][0], tail['velocity'][2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be new')
    paths = tuple(ROOT.glob('mc2p/motion_nav/*.py'))
    sources = {str(path.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in paths}
    trials = []
    for turns in range(4):
        result, _, _ = formal.ActionContinuityFormalTests()._run(formal._gap_case(turns))
        trials.append(dict(id=f'gap-{turns}', metrics=metrics(result), trace=result.trace))
        if result.outcome != 'success' or result.violations:
            break
    if len(trials) == 4 and all(row['metrics']['outcome'] == 'success' for row in trials):
        case = Scenario('height-to-non-centered-tail',
            lane(columns([68, 68, 67, 66, 65, 64, 64]), width=3),
            (.5, 68., .5), (.68, 64., 6.82))
        result = run(case)
        trials.append(dict(id='height-to-tail', metrics=metrics(result), trace=result.trace))
    payload = dict(scope='same five formal simulation tasks, no Fabric claim', sources=sources, trials=trials)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([dict(id=t['id'], **t['metrics']) for t in trials], ensure_ascii=False))
    return 0 if len(trials) == 5 and all(t['metrics']['outcome'] == 'success'
                                        and not t['metrics']['violations'] for t in trials) else 1


if __name__ == '__main__':
    raise SystemExit(main())
