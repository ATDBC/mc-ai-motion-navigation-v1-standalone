"""Print the preparation failure that the corner landing turns into a task 'cancelled'."""
import runpy, sys
from mc2p.motion_nav import motion_coordination as mc

original = mc.MotionRouteCoordinator._accept_result


def accept(self, result, anchor, world, changed):
    ok = original(self, result, anchor, world, changed)
    if not ok and self.last_failure_reason:
        print("motion preparation failed:", self.last_failure_reason, "attempt", self.last_failure_attempt_id,
              "anchor pos", tuple(round(v, 3) for v in anchor.physics_state.position) if hasattr(anchor, "physics_state") else "")
    return ok


mc.MotionRouteCoordinator._accept_result = accept
sys.argv = ["trace_corner.py", sys.argv[1] if len(sys.argv) > 1 else "2"]
runpy.run_path(__file__.replace("corner_reason.py", "trace_corner.py"), run_name="__main__")
