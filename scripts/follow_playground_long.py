"""Constant-space audit of every confirmed long-session task step, not sampled frames."""


class LongTaskAudit:
    def __init__(self, owner: list):
        self.controls=[item for item in owner if item['command']['kind'] in {'follow_start','follow_stop'}]
        if [item['command']['kind'] for item in self.controls]!=['follow_start','follow_stop','follow_start','follow_stop']:
            raise ValueError('long case requires one uninterrupted authorization then one explicit restart')
        self.identities=[None,None]; self.sequences=[0,0]
        self.first_time=self.last_time=None
        self.reset_episode=None
        self.modes=set(); self.holding=self.waiting=0

    def reset(self, episode: str):
        if self.reset_episode is not None: raise ValueError('long case reset during same-client lifetime')
        self.reset_episode=episode

    def observe(self, context: dict, now_ns: int):
        index=next((i//2 for i in (0,2) if self.controls[i]['sent_at_ns']<=now_ns<self.controls[i+1]['sent_at_ns']),None)
        if index is None: raise ValueError('long task outside explicit owner authorization')
        start=self.controls[index*2]['status']
        identity=tuple(context[k] for k in ('task_id','attempt_id','episode_id','source_generation','source_scope_id'))
        if (any(v is None for v in identity) or identity[:2]!=(start['task_id'],start['attempt_id'])
                or identity[2]!=self.reset_episode):
            raise ValueError('long task identity not bound to owner and initial reset')
        previous=self.identities[index]
        if previous is not None and previous!=identity: raise ValueError('long task identity changed midstream')
        if index==1:
            first=self.identities[0]
            if (first is None or identity[0]==first[0] or identity[1]==first[1]
                    or identity[3]<=first[3] or identity[2]!=first[2] or identity[4]!=first[4]):
                raise ValueError('restart reused task/generation or rebuilt client scope')
        if context['intent_sequence']!=self.sequences[index]+1:
            raise ValueError('long accepted intent sequence reset/gap/replay')
        self.identities[index]=identity; self.sequences[index]+=1
        if index==0:
            if self.last_time is not None and now_ns<=self.last_time: raise ValueError('long task clock not increasing')
            if self.first_time is None: self.first_time=now_ns
            self.last_time=now_ns
            self.modes.add(context['requested_mode'])
            if len(self.modes)>5: raise ValueError('unexpected long gait')
            self.holding+=context['decision']['state']=='holding_distance'
            self.waiting+=context['decision']['state']=='waiting_target'

    def metrics(self):
        return dict(duration_ns=0 if self.first_time is None else self.last_time-self.first_time,
            accepted_steps=self.sequences[0],restart_steps=self.sequences[1],
            holding_steps=self.holding,waiting_steps=self.waiting,modes=sorted(self.modes))

    def checks(self):
        return dict(same_task_at_least_660_seconds=self.metrics()['duration_ns']>=660_000_000_000,
            more_than_4096_accepted_intents=self.sequences[0]>4096,
            same_client_explicit_restart=self.sequences[1]>0,
            all_modes_holding_and_waiting=self.modes=={'slow','normal','fast','max','auto'} and self.holding>0 and self.waiting>0)
