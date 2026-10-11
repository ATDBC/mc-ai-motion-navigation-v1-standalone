"""E6: the same P3 owner on D097-A template actions (frozen D sample), injecting at the commitment
boundary, the next boundary and the first airborne boundary.  Variants:
  V0  project P3 owner unchanged
  V1  E3 (re-anchor with the known timing, one branch) + E4 (compare with proven branch states)
  V2  V1 + re-plan with the frozen table controller from the observed state on every re-proof
Experiment only; monkeypatches this process."""
import collections, json, sys
from dataclasses import replace
import experiments.motion_navigation.trajectory_proto.closed_loop as CL
import experiments.motion_navigation.trajectory_proto.closed_loop_measurement as M
from experiments.motion_navigation.trajectory_proto.closed_loop import ActionPhase, FrozenInputPlan
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch
from experiments.motion_navigation.trajectory_proto.decision import TableControllerAdapter
from experiments.motion_navigation.trajectory_proto.motion_table import load_frozen_table
from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.measurement import _set_d
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import e3_e4_patches as P

TABLE = load_frozen_table()
variant = sys.argv[1]
flags = set(variant.split("+"))
from mc2p.motion_nav.geometry import query_support, required_cells_for_sweep
from mc2p.motion_nav.world_model import WorldView
def landing_support_only(request, state, world):
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    detached = WorldView.detached(world.session, world.geometry_revision, 0, {p: world.cell(p) for p in cells})
    support = query_support(state.body_box, detached)
    return not support.missing_cells and CL.support_is_safe(state, support)
if "G" in flags:
    CL._landing_recovery_proved = landing_support_only
cases, meta = {}, {}
for case in _set_d(18):
    set_id, key, request, world, inputs, bin_id, template = case
    if template == "turn":
        continue
    proof = prove_incrementally(request, inputs, world, _boundary_evidence(request, inputs), mode="strict")
    commits = [p for p in proof.permits if p.locked_count > 0]
    if not commits:
        continue
    c = commits[0].boundary
    states = proof.branches[0].states
    air = next(i for i in range(c, len(states)) if not states[i].on_ground)
    cases[key] = ("P3", key, request, world, inputs, None, None)
    meta[key] = (template, bin_id, c, air)
M._cases = lambda: cases

current = {}
orig_start = CL.RollingProofOwner.start.__func__
def start(cls, request, world, plan, **kw):
    owner = orig_start(cls, request, world, plan, **kw)
    owner._e6 = current["meta"]
    return owner
CL.RollingProofOwner.start = classmethod(start)
M.RollingProofOwner = CL.RollingProofOwner

def reprove_controller(self, observed, world, request):
    if self.consecutive_reproofs >= CL.MAX_CONSECUTIVE_REPROOFS:
        return False, 0, 0, "reproof_limit"
    rebound = self._reanchored_request(request, observed, world)
    template, bin_id = self._e6[:2]
    try:
        attempt = CL._proof_bundle(rebound, world, TableControllerAdapter(TABLE, template, bin_id), self.runahead_steps)
    except RuntimeError:
        self.consecutive_reproofs += 1
        return False, 0, 0, "controller_miss"
    self.consecutive_reproofs += 1
    if attempt.bundle is None:
        return False, attempt.total_ns, attempt.physics_steps, attempt.reason or "proof_unavailable"
    bundle = attempt.bundle
    self.bundle, self.external_request = bundle, request
    self.plan = FrozenInputPlan(bundle.inputs)
    self.consumed = 0
    self.ledger = CL.ExperimentalInputLedger(rebound.world_session, rebound.input_ledger_id)
    return True, bundle.total_ns, bundle.physics_steps, None

orig_reprove = CL.RollingProofOwner._reprove
def reprove_both(self, observed, world, request):
    saved = (self.consecutive_reproofs, self.plan)
    ok, ns, steps, reason = orig_reprove(self, observed, world, request)
    if ok or not observed.on_ground:
        return ok, ns, steps, reason
    self.consecutive_reproofs, self.plan = saved
    ok2, ns2, steps2, reason2 = reprove_controller(self, observed, world, request)
    return ok2, ns + ns2, steps + steps2, (reason2 if not ok2 else None)
if "E34" in flags:
    P.apply(CL)
if "CTRL" in flags:
    CL.RollingProofOwner._reprove = reprove_controller
if "BOTH" in flags:
    CL.RollingProofOwner._reprove = reprove_both
if "SAFEGOAL" in flags:
    P.apply_safe_goal(CL)

injections = ("position_0_03", "yaw_5deg", "late_application", "missing_application")
rows = []
for key, (template, bin_id, c, air) in meta.items():
    current["meta"] = (template, bin_id)
    for label, boundary in (("commit", c), ("commit+1", c + 1), ("airborne", air)):
        for injection in injections:
            M._phase_boundary = lambda phase, b=boundary: b
            spec = M.MatrixSpec(f"{key}/{label}/{injection}", key, "on_time", injection, ActionPhase.POST_COMMIT)
            r = M.run_scenario(spec)
            rows.append((key, label, injection, r["status"], r["reason"], r["damage_points"], r["recovery_proof_obtained"], max(r["decision_ms"])))
out = collections.Counter()
for key, label, injection, status, reason, dmg, rec, ms in rows:
    out[(label, injection, "fell" if dmg > 0 else status)] += 1
print(f"== {variant}: {len(meta)} D template actions with a commitment, {len(rows)} runs")
for (label, injection, status), n in sorted(out.items()):
    print(f"  {label:9s} {injection:20s} {status:32s} {n}")
reasons = collections.Counter(r[4] for r in rows if r[3] not in ("completed", "cancelled"))
print("  failure reasons:", dict(reasons))
print("  totals: completed", sum(r[3] == "completed" and r[5] == 0 for r in rows), "| fell (damage>0)", sum(r[5] > 0 for r in rows),
      "| unresolved", sum(r[3] == "body_responsibility_unresolved" for r in rows), "| bounded", sum(r[3] == "bounded_failure" for r in rows))
json.dump(rows, open(__import__("pathlib").Path(__file__).resolve().parent / f"output/e6_{variant}.json", "w"), indent=0)
