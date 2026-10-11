"""E8: for the two remaining airborne failures, is there ANY proven recovery?  Offline feasibility only:
try 'hold one alphabet input for k ticks, then stop' from the observed state (SAFEGOAL recovery goal)."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.argv = [str(HERE / "e6.py"), "G+E34+BOTH+SAFEGOAL"]
captured = []
import experiments.motion_navigation.trajectory_proto.closed_loop as CL
exec(open(HERE / "e6.py").read().split("injections = (")[0])   # same setup and patches as E6
from experiments.motion_navigation.trajectory_proto.decision import FrozenInputAdapter
inner = CL.RollingProofOwner._reprove
def capture(self, observed, world, request):
    result = inner(self, observed, world, request)
    if not result[0]:
        captured.append((self, observed, world, request))
    return result
CL.RollingProofOwner._reprove = capture
key = "gap3/sprint-L-n/8"
template, bin_id, c, air = meta[key]
current["meta"] = (template, bin_id)
for injection in ("late_application", "missing_application"):
    captured.clear()
    M._phase_boundary = lambda phase, b=air: b
    row = M.run_scenario(M.MatrixSpec(f"{key}/air/{injection}", key, "on_time", injection, ActionPhase.POST_COMMIT))
    owner, observed, world, request = captured[-1]
    rebound = owner._reanchored_request(request, observed, world)
    alphabet, stop = rebound.supported_inputs, rebound.stop_input
    found = []
    for command in alphabet:
        for k in range(0, 17):
            inputs = ((command,) * k + (stop,) * 40)[:40]
            attempt = CL._proof_bundle(rebound, world, FrozenInputAdapter(inputs), 24)
            if attempt.bundle is not None:
                found.append((k, command))
                break
    print(injection, "| observed y=%.2f z=%.3f vy=%+.3f on_ground=%s" % (observed.position[1], observed.position[2], observed.velocity_blocks_per_tick[1], observed.on_ground),
          "| row:", row["status"], row["reason"], "| proven recoveries found:", len(found), found[:3])
