"""E10: the P3 runner stops simulating when a run ends BODY_RESPONSIBILITY_UNRESOLVED, so its damage
reads 0.  Continue the body for 40 ticks with (a) the last applied input held, (b) the stop input,
and report what would actually happen.  Project owner unchanged."""
import collections
import experiments.motion_navigation.trajectory_proto.closed_loop_measurement as M
from experiments.motion_navigation.trajectory_proto.closed_loop import _simulate_step
from experiments.motion_navigation.trajectory_proto.rollout_trace import damage_points

final = {}
orig_adv = M.advance_backend
def adv(state, authority, world):
    new = orig_adv(state, authority, world)
    final["state"], final["command"], final["world"] = new, authority.command, world
    final.setdefault("states", []).append(new)
    return new
M.advance_backend = adv
outcome = collections.Counter()
for spec in M.matrix_specs():
    final.clear()
    _, _, request, world, inputs, _, _ = M._cases()[spec.case_id]
    final["states"] = [request.anchor_state]
    row = M.run_scenario(spec)
    if row["status"] != "body_responsibility_unresolved":
        continue
    res = []
    for label, command in (("hold last input", final["command"]), ("stop input", request.stop_input)):
        states = list(final["states"])
        state = states[-1]
        left = False
        for _ in range(40):
            try:
                state = _simulate_step(state, command, final["world"])
            except Exception:
                left = True
                break
            states.append(state)
        dmg = damage_points(tuple(states))
        res.append(f"{label}: y={state.position[1]:.1f} damage={dmg:.0f}" + (" (fell out of the known box)" if left else ""))
        outcome[(label, "falls" if left or dmg > 0 or state.position[1] < .5 else "stays")] += 1
    print(f"{spec.scenario_id:36s} | " + " | ".join(res))
print(dict(outcome))
