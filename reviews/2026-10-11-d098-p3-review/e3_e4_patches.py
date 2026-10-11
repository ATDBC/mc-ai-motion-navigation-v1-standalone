"""E3 + E4 as reusable patches for the P3 owner (experiment only)."""
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch

def apply(CL):
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
    CL.RollingProofOwner._reanchored_request = single_branch_reanchor
    CL.RollingProofOwner._consume_pending = consume_with_envelope


def apply_safe_goal(CL):
    """After commitment the recovery obligation is a proven safe stop, not the task goal."""
    from mc2p.motion_nav.world_model import Aabb
    reanchor = CL.RollingProofOwner._reanchored_request
    def safe_goal_reanchor(self, source, observed, world):
        request = reanchor(self, source, observed, world)
        if self.phase() is CL.ActionPhase.PRE_COMMIT:
            return request
        anywhere = Aabb(-60., -60., -60., 60., 60., 60.)
        return replace(request, goal=replace(request.goal, region=anywhere),
                       minimum_terminal_speed_blocks_per_second=0.)
    CL.RollingProofOwner._reanchored_request = safe_goal_reanchor
