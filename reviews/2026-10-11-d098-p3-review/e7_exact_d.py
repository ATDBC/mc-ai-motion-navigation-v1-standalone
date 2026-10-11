"""E7: exact execution (no injection) of every D sample action with a commitment, through the P3 owner.
Shows where the submission gate's landing check stands relative to the goal region."""
import sys
import experiments.motion_navigation.trajectory_proto.closed_loop as CL
import experiments.motion_navigation.trajectory_proto.closed_loop_measurement as M
from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.measurement import _set_d
from mc2p.motion_nav.geometry import query_support, required_cells_for_sweep
from mc2p.motion_nav.world_model import WorldView

fix = len(sys.argv) > 1 and sys.argv[1] == "support_only"
if fix:
    def landing_support_only(request, state, world):
        cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
        detached = WorldView.detached(world.session, world.geometry_revision, 0, {p: world.cell(p) for p in cells})
        support = query_support(state.body_box, detached)
        return not support.missing_cells and CL.support_is_safe(state, support)
    CL._landing_recovery_proved = landing_support_only
cases = {}
for case in _set_d(18):
    _, key, request, world, inputs, bin_id, template = case
    proof = prove_incrementally(request, inputs, world, _boundary_evidence(request, inputs), mode="strict")
    intervals = [(r.first_committed_boundary, r.recovered_boundary) for r in proof.branches[0].risk_intervals]
    cases[key] = ("P3", key, request, world, inputs, None, None)
    M._cases = lambda: cases
    row = M.run_scenario(M.MatrixSpec("exact", key, "on_time", "exact", None))
    g = request.goal.region
    land = proof.branches[0].states[intervals[0][1]].position if intervals else None
    print(f"{key:24s} interval={intervals} landing z={'%.2f' % land[2] if land else '-':>5s} goal z=[{g.min_z:.2f},{g.max_z:.2f}] "
          f"-> {row['status']:16s} {row['reason']}")
