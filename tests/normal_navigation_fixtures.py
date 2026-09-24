"""Synthetic legal V3 samples; no world truth or live game access."""
from dataclasses import replace

from tests.follow_v3_fixtures import follow_snapshot

NOW_NS = 1_000_000_000
CLIENT_CLOCK_OFFSET_NS = 9_000_000_000


def frame(sequence=0, now_ns=NOW_NS, position=(.5, 64, .5), yaw=0.,
          blocks=(), velocity=(0., 0., 0.)):
    observation = follow_snapshot(
        sequence=sequence, received=now_ns, position=position, yaw=yaw,
        entities=[], blocks=blocks,
        self_changes={'velocity': dict(zip(('x', 'y', 'z'), velocity)),
                      'is_on_ground': True, 'pose': 'standing',
                      'horizontal_collision': False})
    # Distinct clock domains, with explicit synthetic progression for dwell tests.
    return replace(observation, client_sample=replace(observation.client_sample,
        started_at_monotonic_ns=CLIENT_CLOCK_OFFSET_NS+now_ns-1_000_000,
        completed_at_monotonic_ns=CLIENT_CLOCK_OFFSET_NS+now_ns))
