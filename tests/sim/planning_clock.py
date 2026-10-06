"""Test-only search clock; unrelated threads retain the host clock."""
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import count
from threading import RLock
from types import SimpleNamespace

from mc2p.motion_nav import known_map_planner


_clock_ticks = ContextVar("inline_planning_clock_ticks", default=None)
_installation_lock = RLock()


@contextmanager
def deterministic_planning_clock():
    """Advance one ns per query during one synchronous planning job only.

    The per-job counter retains a finite, monotonic search clock. Expansion
    limits still bound the search, while host scheduling cannot reject a route.
    The proxy changes only known_map_planner's binding, never stdlib time.
    Its thread context keeps concurrent production calls on the original clock;
    the lock serializes installation/restoration by concurrent inline jobs.
    """
    with _installation_lock:
        original_time = known_map_planner.time
        token = _clock_ticks.set(count())

        def perf_counter_ns():
            ticks = _clock_ticks.get()
            return (original_time.perf_counter_ns()
                    if ticks is None else next(ticks))

        known_map_planner.time = SimpleNamespace(perf_counter_ns=perf_counter_ns)
        try:
            yield
        finally:
            known_map_planner.time = original_time
            _clock_ticks.reset(token)
