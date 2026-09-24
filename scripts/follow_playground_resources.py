"""Bounded read-only RSS sidecar. Never imported by a policy or a game adapter."""
from collections import deque
import math
import os
import threading
import time
import psutil
from mc2p.runtime.segmented_trace import SegmentedJsonlWriter, iter_segmented_jsonl


class ResourceSampler:
    def __init__(self, directory, role: str, *, interval_seconds: float = 1):
        if not 0<interval_seconds<=1 or not role: raise ValueError('invalid resource sampler')
        self.writer=SegmentedJsonlWriter(directory)
        self.role,self.interval=role,interval_seconds
        self.process=psutil.Process(os.getpid())
        self.identity=(self.process.pid,self.process.create_time())
        self.stop=threading.Event()
        self.error=None
        self.thread=None

    def start(self):
        if self.thread is not None: raise RuntimeError('sampler already started')
        self.thread=threading.Thread(target=self._run,name='playground-rss',daemon=True)
        self.thread.start()

    def _run(self):
        sequence=0
        try:
            while not self.stop.is_set():
                sequence+=1
                if sequence>2000: raise RuntimeError('bounded RSS sampler duration exceeded')
                if self.process.create_time()!=self.identity[1]: raise RuntimeError('RSS process identity changed')
                self.writer.write(dict(schema_version='mc2p.playground-rss.v1',sequence=sequence,sampled_at_ns=time.perf_counter_ns(),
                    pid=self.identity[0],create_time=self.identity[1],role=self.role,rss_bytes=self.process.memory_info().rss))
                self.stop.wait(self.interval)
        except BaseException as error: self.error=error

    def check(self):
        if self.error is not None: raise RuntimeError('resource sampling failed') from self.error

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive(): raise TimeoutError('resource sampler did not stop')
        try: self.check()
        finally: self.writer.close()


def summarize_resource_rows(rows):
    count=0; identity=None; first_time=last_time=None; peak=0; max_gap=0
    first=[]; tail=deque(maxlen=32)
    for row in rows:
        count+=1
        current=(row['pid'],row['create_time'],row['role'])
        if (row['schema_version']!='mc2p.playground-rss.v1' or type(row['pid']) is not int or row['pid']<=0
                or type(row['create_time']) not in (int,float) or not math.isfinite(row['create_time']) or row['create_time']<=0
                or type(row['role']) is not str or not row['role']
                or type(row['sequence']) is not int or row['sequence']!=count or type(row['sampled_at_ns']) is not int
                or row['sampled_at_ns']<0 or (last_time is not None and row['sampled_at_ns']<=last_time)
                or (identity is not None and current!=identity) or type(row['rss_bytes']) is not int or row['rss_bytes']<=0):
            raise ValueError('resource sample continuity/identity/value failed')
        identity=current
        if first_time is None: first_time=row['sampled_at_ns']
        if last_time is not None: max_gap=max(max_gap,row['sampled_at_ns']-last_time)
        last_time=row['sampled_at_ns']; peak=max(peak,row['rss_bytes'])
        if len(first)<32: first.append(row['rss_bytes'])
        tail.append(row['rss_bytes'])
    if count<2: raise ValueError('insufficient complete resource samples')
    return dict(sample_count=count,pid=identity[0],create_time=identity[1],role=identity[2],
        duration_ns=last_time-first_time,max_gap_ns=max_gap,peak_rss_bytes=peak,
        first_32_mean_bytes=sum(first)/len(first),last_32_mean_bytes=sum(tail)/len(tail))


def summarize_resources(directory):
    return summarize_resource_rows(iter_segmented_jsonl(directory))


def resource_identity_matches(summary: dict, role: str, registered: list) -> bool:
    return summary['role']==role and any(summary['pid']==item['pid'] and summary['create_time']==item['create_time']
        for item in registered)


def attach_evaluator_resources(evidence: dict, directory, identity: tuple) -> None:
    """Keep an early case failure even when evaluation was too short for two RSS points."""
    passed=False
    try:
        summary=summarize_resources(directory)
        passed=summary['peak_rss_bytes']<=512*1024**2 and resource_identity_matches(summary,'evaluator',
            [dict(pid=identity[0],create_time=identity[1])])
        evidence['metrics']['case_evaluator_rss']=summary
    except Exception as error:
        evidence['metrics']['case_evaluator_resource_failure']=type(error).__name__+': '+str(error)
    evidence['checks'].append(dict(name='streaming_case_evaluator_peak_bounded',passed=passed))
    evidence['passed']=evidence['passed'] and passed


def long_resource_checks(worker: dict, validator: dict) -> dict:
    """Predeclared Python working-set bounds, not a claim about Java/world allocation."""
    mib=1024**2
    return dict(worker_rss_one_hz_coverage=worker['duration_ns']>=660_000_000_000
        and worker['sample_count']>=660 and worker['max_gap_ns']<=1_250_000_000,
        worker_rss_peak_growth_bounded=worker['peak_rss_bytes']-worker['first_32_mean_bytes']<=128*mib,
        worker_rss_tail_growth_bounded=worker['last_32_mean_bytes']-worker['first_32_mean_bytes']<=64*mib,
        streaming_validator_peak_bounded=validator['peak_rss_bytes']<=512*mib)
