"""Bounded live diagnostic display only. Final evidence still requires full sealed-stream validation."""
from collections import OrderedDict
import math
from pathlib import Path
from mc2p.runtime.segmented_trace import _safe_path, _decode


class _LiveCursor:
    def __init__(self, directory: Path):
        self.directory = directory
        self.index = self.offset = 0

    def rows(self):
        for _ in range(2048):
            path = _safe_path(self.directory/f'segment-{self.index:08d}.jsonl')
            if not path.exists(): return
            if path.stat().st_size<self.offset: raise ValueError('live diagnostic segment truncated')
            with path.open('rb') as stream:
                stream.seek(self.offset); raw = stream.readline(8193)
            if len(raw)>8192: raise ValueError('live diagnostic row exceeds bound')
            if not raw:
                following = _safe_path(self.directory/f'segment-{self.index+1:08d}.jsonl')
                if not following.exists(): return
                self.index,self.offset = self.index+1,0
                continue
            if not raw.endswith(b'\n'): return  # Active writer may not yet have finished this row.
            self.offset += len(raw)
            yield _decode(raw)


class MovementStatusReader:
    def __init__(self, directory: Path):
        self._time = _LiveCursor(directory/'time-events')
        self._motion = _LiveCursor(directory/'movement-events')
        self._times,self._motions = OrderedDict(),OrderedDict()
        self._identity = self._session = self._failure = None
        self._event = self._time_event = 0

    @staticmethod
    def _remember(table: OrderedDict, key, value):
        table[key] = value
        while len(table)>32: table.popitem(last=False)

    def status(self, observation, now_ns: int) -> dict:
        unknown = dict(actual_motion='unknown',actual_motion_age_ns=None)
        if self._failure is not None: return dict(unknown,actual_motion_reason=self._failure)
        identity = (observation.episode_id,observation.controller_clock_id,observation.client_sample.clock_id)
        if self._identity is None: self._identity = identity
        if self._identity!=identity:
            self._failure = 'diagnostic_session_changed'
            return dict(unknown,actual_motion_reason=self._failure)
        try:
            for row in self._time.rows():
                if row.get('schema_version')!='mc2p.client-time-event.v1': raise ValueError('wrong time schema')
                if type(row['event_sequence']) is not int or row['event_sequence']!=self._time_event+1:
                    raise ValueError('time event sequence gap')
                self._time_event += 1
                if self._session is None: self._session = row['session_id']
                if row['session_id']!=self._session: raise ValueError('time session changed')
                if row['event']=='observation': self._remember(self._times,row['generation_id'],row)
            for row in self._motion.rows():
                if (row.get('schema_version')!='mc2p.client-movement-event.v2'
                        or type(row['event_sequence']) is not int or row['event_sequence']!=self._event+1):
                    raise ValueError('movement event gap or schema')
                if any(type(row[k]) is not int or row[k]<0 for k in ('generation_id','client_ticks','time_event_sequence','sampled_at_monotonic_ns')):
                    raise ValueError('invalid movement counter')
                self._event += 1
                self._remember(self._motions,row['generation_id'],row)
            row = self._motions.get(observation.sequence_id)
            event = self._times.get(observation.sequence_id)
            if row is None or event is None: return unknown
            if any(row[key]!=event[key] for key in ('session_id','world_id','client_ticks')) or row['time_event_sequence']!=event['event_sequence']:
                raise ValueError('movement time identity mismatch')
            sample = observation.client_sample
            if row['clock_id']!=sample.clock_id: raise ValueError('movement sample clock identity mismatch')
            if not sample.started_at_monotonic_ns-250_000_000 <= row['sampled_at_monotonic_ns'] <= sample.completed_at_monotonic_ns+250_000_000:
                raise ValueError('movement and formal sample clocks disagree')
            age = now_ns-observation.received_at_monotonic_ns
            if not 0<=age<=500_000_000 or row['available'] is not True: return unknown
            fields = ('actual_sprinting','actual_sneaking','on_ground')
            if any(type(row[key]) is not bool for key in fields) or type(row['pose']) is not str:
                raise ValueError('invalid actual motion flags')
            velocity = row['velocity']
            if set(velocity)!={'x','y','z'} or any(type(v) not in (int,float) or not math.isfinite(v) for v in velocity.values()):
                raise ValueError('invalid actual motion velocity')
            return dict(actual_motion={key:row[key] for key in (*fields,'pose','velocity')},actual_motion_age_ns=age)
        except (OSError,ValueError,KeyError,TypeError) as error:
            self._failure = type(error).__name__+': '+str(error)[:160]
            return dict(unknown,actual_motion_reason=self._failure)
