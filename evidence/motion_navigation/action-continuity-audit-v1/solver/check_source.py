import json
from pathlib import Path

root=Path(__file__).resolve().parents[3]
demo=root/'artifacts/current-navigation-demo/20261002T130530628560Z-9880fb4f'
pos=[224.55,100.,2.549600454447249]
hits=[]
last=None
with (demo/'game/MC2PFollower/trace.jsonl').open('r',encoding='utf-8') as stream:
    for n,line in enumerate(stream,1):
        item=json.loads(line)
        p=item['payload']
        o=p.get('result',{}).get('observation') or p.get('backend_result',{}).get('observation')
        if not o:
            continue
        own=o['self_state']['value']
        v=own['position']
        q=[v['x'],v['y'],v['z']]
        t=own.get('movement_tick_id')
        if all(abs(a-b)<1e-10 for a,b in zip(q,pos)):
            hits.append(dict(line=n,record_type=item['record_type'],sequence=o['sequence_id'],
                             movement=t,world=o['world_time_ticks']['value'],
                             received_ns=o['received_at_monotonic_ns'],episode=o['episode_id']))
        last=dict(line=n,record_type=item['record_type'],sequence=o['sequence_id'],
                  movement=t,position=q,episode=o['episode_id'])
print(json.dumps(dict(hits=hits,last=last,lines=n),ensure_ascii=False,indent=2))
