"""E1: what happens in late_application-commit_boundary.  E2: re-proof with NO deviation at each boundary."""
import experiments.motion_navigation.trajectory_proto.closed_loop as CL
import experiments.motion_navigation.trajectory_proto.closed_loop_measurement as M
from experiments.motion_navigation.trajectory_proto.closed_loop import ActionPhase

def lab(c):
    if c.forward == 0 and not c.jump: return "N"
    return ("SJ" if c.sprint else "J") if c.jump else ("S" if c.sprint else "W")

# ---- E1
captured = {}
orig_start = CL.RollingProofOwner.start.__func__
def start(cls, *a, **k):
    owner = orig_start(cls, *a, **k); captured["owner"] = owner; captured["trace"] = owner.bundle.trace
    return owner
M.RollingProofOwner.start = classmethod(start)
steps = []
orig_adv = M.advance_backend
def adv(state, authority, world):
    new = orig_adv(state, authority, world); steps.append((authority.command, authority.source.value, new)); return new
M.advance_backend = adv
spec = next(s for s in M.matrix_specs() if s.scenario_id == "late_application-commit_boundary")
row = M.run_scenario(spec)
proven = captured["trace"].branch_states[0]
print("E1", spec.scenario_id, "->", row["status"], "damage", row["damage_points"], "invalidations", row["invalidations"])
print("   decisions:", row["decisions"])
print("   k  applied(src)        actual z     y      ground | proven z(b0)  ground")
for k, (cmd, src, st) in enumerate(steps, start=1):
    p = proven[k] if k < len(proven) else None
    print(f"  {k:2d}  {lab(cmd):2s} ({src:15s}) {st.position[2]:7.3f} {st.position[1]:7.2f}  {int(st.on_ground)}    | "
          + (f"{p.position[2]:7.3f}  {int(p.on_ground)}" if p else ""))

# ---- E2: force the deviation path at boundary k without changing the state
M.advance_backend = orig_adv
M.RollingProofOwner.start = classmethod(orig_start)
results = {}
for k in range(0, 9):
    flag = {"armed": False}
    orig_inject, orig_cmp, orig_pb = M._inject, CL.compare_states, M._phase_boundary
    def inject(spec, owner, state, world, request):
        flag["armed"] = True
        return state, world, request, False
    def cmp(expected, observed):
        if flag["armed"]:
            flag["armed"] = False
            return CL.StateComparison(False, "forced", None, None)
        return orig_cmp(expected, observed)
    M._inject, CL.compare_states = inject, cmp
    M._phase_boundary = lambda phase, k=k: k
    spec = M.MatrixSpec(f"forced-{k}", "jump_gap_1:A3", "on_time", "position_0_01", ActionPhase.PRE_COMMIT)
    try:
        row = M.run_scenario(spec)
        results[k] = (row["injection_actual_phase"], row["status"], row["reason"], row["damage_points"])
    finally:
        M._inject, CL.compare_states, M._phase_boundary = orig_inject, orig_cmp, orig_pb
print("E2 re-proof with an exact observation (no deviation) at boundary k:")
for k, v in results.items():
    print(f"   k={k}: phase={v[0]:16s} status={v[1]:32s} reason={v[2]}  damage={v[3]}")
