import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / 'artifacts/current-navigation-demo/20261002T130530628560Z-9880fb4f'

for rel in ('navigation-frames.jsonl', 'game/MC2PFollower/trace.jsonl'):
    path = DEMO / rel
    print('FILE', rel, path.stat().st_size)
    with path.open('r', encoding='utf-8') as stream:
        first = json.loads(next(stream))
        print('KEYS', list(first))
        if rel == 'navigation-frames.jsonl':
            print('FIRST', json.dumps(first, ensure_ascii=False)[:500])
        else:
            obs = first['payload']['result']['observation']
            print('OBS_KEYS', list(obs))
            print('SELF', json.dumps(obs['self_state'], ensure_ascii=False)[:5000])
        found = 0
        for index, line in enumerate(stream, 2):
            item = json.loads(line)
            if rel == 'navigation-frames.jsonl' and 16520 <= item['tick'] <= 16542:
                print('MATCH', index, json.dumps(item, ensure_ascii=False))
                found += 1
                if found >= 23:
                    break
            elif rel != 'navigation-frames.jsonl':
                obs = item['payload'].get('backend_result', {}).get('observation')
                if obs:
                    print('STEP_OBS_KEYS', list(obs))
                    break

root = json.loads((ROOT / 'config/motion-navigation/verified-height-transitions-v1.json').read_text('utf-8'))
for kind, policy in root['air_transition_solvers'].items():
    templates = policy['templates']
    summary = {key: sorted({t[key] for t in templates}) for key in templates[0]}
    print('POLICY', kind, len(templates), policy['maximum_entry_speed_blocks_per_second'], summary)
