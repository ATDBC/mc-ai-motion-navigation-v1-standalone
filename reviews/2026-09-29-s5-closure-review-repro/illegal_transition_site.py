# Run from the repository root of a daa5cc4 checkout: PYTHONPATH=. python -B <script> <scenario> <kind@tick> ...
"""Print where an illegal transition is raised before the formal driver converts it into an internal failure."""
import runpy, sys, traceback
from mc2p.motion_nav import navigation_lifecycle as nl

original = nl.NavigationLifecycle.transition


def transition(self, action, **kwargs):
    try:
        return original(self, action, **kwargs)
    except Exception as error:  # noqa: BLE001
        frames = traceback.extract_stack(limit=8)[:-1]
        print(f"illegal transition from {self.state.value} via {action.value}: {error} | "
              + " <- ".join(f"{f.name}:{f.lineno}" for f in reversed(frames)))
        raise


nl.NavigationLifecycle.transition = transition
sys.argv = ["trace_sequence.py"] + sys.argv[1:]
runpy.run_path(__file__.replace("illegal_transition_site.py", "trace_sequence.py"), run_name="__main__")
