"""E3: re-anchor with the timing already known (one ON_TIME branch, no waiting prelude).
E4: compare every consumed observation with the proven branch states, not only with a one-step replay.
Experiment only: monkeypatches the P3 owner in this process; the project code is unchanged."""
import collections, sys
from dataclasses import replace
import experiments.motion_navigation.trajectory_proto.closed_loop as CL
import experiments.motion_navigation.trajectory_proto.closed_loop_measurement as M
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch

def single_branch_reanchor(self, source, observed, world):
    sequence = self.next_sequence
    return replace(
        source,
        request_id=f"{source.request_id}-e3-{observed.movement_tick_id}-{self.consecutive_reproofs + 1}",
        entry_states=(observed,), timing_branches=(TimingBranch.ON_TIME,),
        geometry_revision=world.geometry_revision,
        anchor_id=f"e3-anchor-{observed.movement_tick_id}-{self.consecutive_reproofs + 1}",
        input_ledger_id=f"e3-ledger-{self.consecutive_reproofs + 1}",
        allowed_effect_ticks=(observed.movement_tick_id + 1,),
        branch_preludes=((),), first_candidate_control_sequence=sequence, input_prefix=())

orig_consume = CL.RollingProofOwner._consume_pending
def consume_with_envelope(self, observed, world, request):
    result = orig_consume(self, observed, world, request)
    if result == ("consumed", False):
        proven = [states[self.consumed] for states in self.bundle.trace.branch_states
                  if self.consumed < len(states)]
        if proven and not any(CL.compare_states(p, observed).matches for p in proven):
            return "outside_proven_branches", True
    return result

variant = sys.argv[1]
flags = set(variant.split("+"))
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent)); import e3_e4_patches as P
from mc2p.motion_nav.geometry import query_support, required_cells_for_sweep
from mc2p.motion_nav.world_model import WorldView
def landing_support_only(request, state, world):
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    detached = WorldView.detached(world.session, world.geometry_revision, 0, {p: world.cell(p) for p in cells})
    support = query_support(state.body_box, detached)
    return not support.missing_cells and CL.support_is_safe(state, support)
if "G" in flags:
    CL._landing_recovery_proved = landing_support_only
if "E3" in flags:
    CL.RollingProofOwner._reanchored_request = single_branch_reanchor
if "E4" in flags:
    CL.RollingProofOwner._consume_pending = consume_with_envelope
if "SAFEGOAL" in flags:
    P.apply_safe_goal(CL)
rows = [M.run_scenario(spec) for spec in M.matrix_specs()]
by = collections.Counter((r["status"], r["reason"]) for r in rows)
print(f"== {variant}: {len(rows)} scenarios")
for r in rows:
    print(f'  {r["scenario"]:36s} {r["status"]:32s} {str(r["reason"]):36s} dmg={r["damage_points"]:4.1f} inval={r["invalidations"]} reproofs={r["reproofs"]} rec={r["recovery_proof_obtained"]}')
post = [r for r in rows if r["phase"] in ("commit_boundary", "post_commit") and r["old_suffix_invalidated"]]
print("  needing recovery:", len(post), "recovered:", sum(r["recovery_proof_obtained"] and r["status"] == "completed" for r in post),
      "| damage>0:", sum(r["damage_points"] > 0 for r in rows), "| unresolved:", sum(r["status"] == "body_responsibility_unresolved" for r in rows))
