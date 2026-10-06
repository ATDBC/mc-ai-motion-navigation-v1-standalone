"""Do the unit tests enter the targets that no S0-R behaviour set enters?  (e108a32)

    python -B unit_suite_entry_count.py        (repository root; runs tests/motion_nav once)

Counts calls in the test process only (work done inside spawned planner workers is not counted,
which does not affect these in-process targets).  Read-only apart from the counters.
"""
import collections
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
TARGETS = [
    ("mc2p.motion_nav.execution_supervisor", "ExecutionSupervisor", "_advance_incumbent_prefix"),
    ("mc2p.motion_nav.execution_supervisor", "ExecutionSupervisor", "continue_rejected_candidate"),
    ("mc2p.motion_nav.navigation_session", "NavigationSession", "_wait_for_active_terminal"),
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit"),
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit_local_direct"),
]
COUNTS = collections.Counter()
CALLERS = collections.defaultdict(set)


def install():
    import importlib
    import inspect
    for module_name, owner_name, name in TARGETS:
        owner = getattr(importlib.import_module(module_name), owner_name)
        original = getattr(owner, name)

        def wrapper(*args, __original=original, __key=f"{owner_name}.{name}", **kwargs):
            COUNTS[__key] += 1
            for frame in inspect.stack()[1:]:
                if frame.filename.endswith(".py") and "/tests/motion_nav/test_" in frame.filename.replace("\\", "/"):
                    CALLERS[__key].add(os.path.basename(frame.filename))
                    break
            return __original(*args, **kwargs)
        setattr(owner, name, wrapper)


if __name__ == "__main__":
    install()
    suite = unittest.defaultTestLoader.discover("tests/motion_nav", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
    print(f"tests run {result.testsRun}, failures {len(result.failures)}, errors {len(result.errors)}")
    for _, owner_name, name in TARGETS:
        key = f"{owner_name}.{name}"
        print(f"  {key:<50} calls={COUNTS[key]:>6}  test modules={sorted(CALLERS[key])[:6]}")
