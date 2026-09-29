# Run from the repository root of a 340cac6 checkout: PYTHONPATH=. python -B <script> <period>
"""List the ContractViolations that the formal driver swallows (handle_internal_contract_failure) during goal flapping."""
import runpy, sys, traceback
from mc2p.motion_nav.navigation_session import NavigationSession

original = NavigationSession.propose


def propose(self, *args, **kwargs):
    try:
        return original(self, *args, **kwargs)
    except Exception as error:  # noqa: BLE001
        frames = traceback.extract_tb(error.__traceback__)[-4:]
        print(f"propose raised at observation {self._frame.body.sequence_id if self._frame else None}: "
              f"{type(error).__name__}: {error} | " + " <- ".join(f"{f.name}:{f.lineno}" for f in reversed(frames)))
        raise


NavigationSession.propose = propose
period = sys.argv[1] if len(sys.argv) > 1 else "5"
sys.argv = ["goal_flapping.py", period]
runpy.run_path(__file__.replace("hidden_contract_faults.py", "goal_flapping.py"), run_name="__main__")
