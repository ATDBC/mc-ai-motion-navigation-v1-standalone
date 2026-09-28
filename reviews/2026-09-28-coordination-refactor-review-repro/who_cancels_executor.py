"""Print who cancels the route executor in the corner-landing run."""
import traceback, runpy, sys
from mc2p.motion_nav import action_route_executor as are
from mc2p.motion_nav import motion_coordination as mrc

for cls in (are.ActionRouteExecutor,):
    original = cls.cancel

    def cancel(self, *args, _original=original, _cls=cls.__name__, **kwargs):
        frames = traceback.extract_stack(limit=7)[:-1]
        print(f"{_cls}.cancel:", " <- ".join(f"{f.filename.split('/')[-1]}:{f.name}:{f.lineno}" for f in reversed(frames)))
        return _original(self, *args, **kwargs)
    cls.cancel = cancel
sys.argv = ["trace_corner.py", "2"]
runpy.run_path(__file__.replace("who_cancels_executor.py", "trace_corner.py"), run_name="__main__")
