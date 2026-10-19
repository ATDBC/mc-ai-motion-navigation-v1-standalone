"""Compare actual coordination entry paths of the historical drop-late seeds."""
import inspect
import json
from pathlib import Path
import sys
from contextlib import ExitStack
from functools import wraps
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from tests.motion_nav.test_motion_baseline_recovery import MotionBaselineRecoveryTests
from tests.sim.runner import run


def probe(callback):
    paths = set()
    arcs, previous = set(), {}
    def trace(frame, event, arg):
        if frame.f_code.co_filename.replace('\\', '/').endswith('/motion_coordination.py'):
            key = id(frame)
            if event == 'line':
                arcs.add((frame.f_code.co_name, previous.get(key, 0), frame.f_lineno))
                previous[key] = frame.f_lineno
            elif event == 'return':
                previous.pop(key, None)
            return trace
        return None
    with ExitStack() as stack:
        for name, function in vars(MotionRouteCoordinator).items():
            if not inspect.isfunction(function):
                continue
            def wrapper(*args, _name=name, _function=function, **kwargs):
                paths.add(_name)
                return _function(*args, **kwargs)
            stack.enter_context(patch.object(MotionRouteCoordinator, name, wraps(function)(wrapper)))
        sys.settrace(trace)
        try:
            outcome = callback()
        finally:
            sys.settrace(None)
    return sorted(paths), sorted(arcs), outcome


def main():
    test = MotionBaselineRecoveryTests()
    reports = {}
    for seed in (15, 69, 119, 163):
        paths, arcs, result = probe(lambda: run(test.scenario(seed)))
        reports[str(seed)] = {'paths': paths, 'arcs': arcs, 'outcome': result.outcome,
                             'reason': result.reason, 'violations': result.violations}
    paths, arcs, _ = probe(test.test_changed_heading_during_background_revalidation_can_realign)
    current = set(paths) | set(reports['163']['paths'])
    current_arcs = set(arcs) | set(tuple(x) for x in reports['163']['arcs'])
    for seed in ('15', '69', '119'):
        reports[seed]['additional_paths'] = sorted(set(reports[seed]['paths']) - current)
        reports[seed]['additional_arcs'] = sorted(set(tuple(x) for x in reports[seed]['arcs']) - current_arcs)
    reports['condition_triggered'] = {'paths': paths, 'arcs': arcs}
    target = ROOT / 'evidence/motion_navigation/redesign-m0/historical-seeds.json'
    target.write_text(json.dumps(reports, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: {key: value for key, value in v.items() if key not in ('arcs', 'paths')}
                      for k, v in reports.items()}, indent=2))


if __name__ == '__main__':
    main()
