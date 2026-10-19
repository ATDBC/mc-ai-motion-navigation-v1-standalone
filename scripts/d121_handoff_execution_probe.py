"""D121 component fixture: explicit legal route, real Runtime and receipts."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment
from mc2p.motion_nav.async_work import AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmitter, MotionCandidateContext, MotionCandidateStatus,
    VerifiedMotionCandidate, VerifiedMotionExecutor,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_solver import (
    SolveStatus, _replay_verified_commands, bind_fixed_successor,
)
from mc2p.motion_nav.motion_worker import GapMotionSolveResult, PreparedMotionJob, _execute_job
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.support_surfaces import SupportSurface, SurfaceNodeId, HorizontalRegion
from mc2p.motion_nav.world_snapshot import seal_snapshot_version
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.motion_nav.test_d102_receiver import QueuedWorker
from tests.motion_nav.test_d121_successor_binding import fixed_case
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import CONFIG, InlinePlannerWorker, seed_memory, _goal
from tests.test_player_runtime import _RecordingTrace


def _install_manual_route(session, route, frame, anchor, ledger):
    """Match the existing successful ground-direct caller's pending cleanup.

    The hand route is the explicit test boundary. This helper grants no proof,
    READY, supervisor approval, tracker advance or executable stream.
    """
    assert session._direct_goal_request_id == session._request.request_id
    assert session._direct_planning_permit is None  # initial request has no retry permit
    assert route.source_request_id == session._request.request_id
    assert route.planning_generation == session._request.sequence
    session._activate_planning_route(route, frame, anchor, ledger, planning_result=False)
    control = session._supervisor.route
    assert control is not None and control.route is route
    # Same postconditions as NavigationSession._activate_ground_direct_route.
    session._direct_goal_request_id = None
    session._direct_planning_permit = None


class ExecutionCase:
    def __init__(self, *, direction_index=0, worker=None):
        from scripts import d120_momentum_entry_calibration as calibration
        request, fixture, template, spec, old_snapshot = fixed_case(direction_index)
        solids = {pos: fact.block.material_key for pos, fact in old_snapshot.facts if fact.block is not None}
        self.clock = [100_000_000]
        self.backend = CalculatorBackend(self.clock, Scene(solids, ((-14,14),(60,70),(-14,14))),
            template.entry_state.position)
        self.backend.state = replace(template.entry_state, session=self.backend._truth_session)
        self.backend.movement_tick = 0
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, clock_ns=lambda:self.clock[0])
        assert self.runtime.reset(ResetRequestV0("reset", self.backend.episode, "d121", 1,
            self.clock[0] + 30_000_000_000)).succeeded
        seed_memory(self.runtime, self.backend.scene)
        self.worker = worker or QueuedWorker()
        self.session = NavigationSession("d121", NavigationSessionProfiles.load(CONFIG),
            planner_worker=InlinePlannerWorker(), motion_worker=self.worker, clock_ns=lambda:self.clock[0])
        self.session._motion_result_inbox.attach_worker(self.worker)
        self.driver = RuntimeNavigationDriver(self.runtime, self.session, clock_ns=lambda:self.clock[0])
        x, z = calibration.frozen._local_to_world(fixture.context.origin, direction_index, .5, 6.5)
        self.driver.start("fixed-goal", 1, _goal((x,64,z)), self.clock[0],
            planning_policy=GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND)
        frame = self.session._frame
        self.anchor = self.session.execution_anchor(self.runtime.observation, self.runtime.input_ledger)
        self.world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
        primary, failure = _replay_verified_commands(self.anchor.physics_state, template.commands,
            self.world, request, start_tick=1)
        assert failure is None, failure
        proof = replace(template, entry_state=self.anchor.physics_state, tick_inputs=primary.tick_inputs,
            trajectory=primary.trajectory, step_events=primary.step_events, exit_state=primary.exit_state,
            release_safe_command_indices=primary.release_safe_command_indices,
            world_dependencies=primary.world_dependencies, resource_incomplete_reasons=primary.resource_incomplete_reasons,
            fallback_tails=primary.fallback_tails)
        self.source = frame.world._owner
        snapshot = seal_snapshot_version(self.source.borrow_snapshot_version(
            PhysicsWorldBounds(-14,14,60,70,-14,14), ruleset=JAVA_1_21_RULESET),JAVA_1_21_RULESET)
        self.bound = bind_fixed_successor(proof, request, spec, snapshot, damage_remaining_points=0.)
        assert self.bound.status is SolveStatus.SOLVED, self.bound
        self.proof = self.bound.proof
        surfaces = []
        for local_z in (-6,1,6):
            x, z = calibration.frozen._local_to_world(fixture.context.origin, direction_index, .5, local_z+.5)
            cx, cz = int(x // 1), int(z // 1)
            surfaces.append(SupportSurface(SurfaceNodeId(cx,cz,64,0),(x,64,z),
                HorizontalRegion(cx,cz,cx+1,cz+1),1.,("minecraft:grass_block",),()))
        actions = tuple(JumpGapSegment(JumpGapEdge(a.node_id,b.node_id,"component-explicit-proof",.9,()),a,b,())
            for a,b in zip(surfaces,surfaces[1:]))
        self.route = ActiveRoute("fixed-route",1,self.session._request.request_id,"fixed-goal",1,
            frame.session.value,None,12.,0.,(),ExecutableCorridor(
                tuple(s.node_id for s in surfaces),(),12.,surfaces[-1].node_id),
            ActionRoute("fixed-route",actions),planning_generation=self.session._request.sequence)
        _install_manual_route(self.session, self.route, frame, self.anchor, self.runtime.input_ledger)
        self.coordinator = self.session._coordinator
        self.controller = VerifiedMotionExecutor()
        self.context = MotionCandidateContext(self.route.source_request_id,self.route.planning_generation,
            self.route.goal_id,1,self.route.route_id,1,0,1,TaskDamageBudget(),self.proof.resource_incomplete_reasons)
        arguments = asdict(self.context)
        arguments.pop("accepted_resource_incomplete_reasons")
        arguments["damage_budget"] = self.context.damage_budget
        arguments.update(intended_start_tick=1,changed_cells=())
        admitted = MotionCandidateAdmitter().admit_committed(VerifiedMotionCandidate(self.proof,self.context),
            self.anchor,executor=self.controller,**arguments)
        assert admitted.status is MotionCandidateStatus.ACCEPTED, admitted
        self.session._executor.install_verified_motion(admitted.candidate)
        self.identity = AsyncWorkIdentity(self.session.current_computation_scope,
            self.coordinator._owner_instance_id, AsyncWorkKind.MOTION_SOLVE,"component-current",1)
        self.request = request
        self.message = None

    def prepare_successor(self, *, deliver=True):
        submitted = self.coordinator.prepare_fixed_successor(
            GapMotionSolveResult("component-current",1,self.bound,0,self.identity),
            self.request,self.context,self.anchor,self.world,
            ledger_identity=self.runtime.input_ledger.ledger_identity,expected_work_identity=self.identity)
        from mc2p.motion_nav.prepared_motion import PreparedSubmitStatus
        assert submitted.status is PreparedSubmitStatus.ACCEPTED, submitted
        if not hasattr(self.worker, "put"):
            # Real IPC returns asynchronously while Runtime ticks keep advancing.
            return None
        assert self.worker.submitted.wait(10), (
            self.session._motion_result_inbox.prepared_compute_result(self.coordinator._prepared_fields.async_identity),
            self.session._motion_result_inbox.drain_receiver_diagnostics())
        assert self.session._motion_result_inbox.wait_for_receiver(timeout=10)
        job = next(job for job in self.worker.jobs if type(job) is PreparedMotionJob)
        self.message = _execute_job(job)
        if deliver:
            self.worker.put(self.message)
            assert self.session._motion_result_inbox.wait_for_receiver(timeout=10)
        return job

    def tick(self, *, dispatch_transform=None):
        if dispatch_transform is None:
            return self.driver.tick(BehaviorProfileV0(),self.clock[0]+500_000_000)
        proposals = self.driver.prepare_proposals(self.clock[0]+500_000_000)
        deadline = self.driver._prepared_deadline_ns
        dispatches = dispatch_transform(self.driver.prepared_streams)
        result = self.runtime.control_frame(self.driver._task(deadline), BehaviorProfileV0(),
            deadline, proposals=proposals, prepared_streams=dispatches)
        self.driver.adopt_result(result)
        return result

    def close(self):
        self.session.close()
        self.runtime.close()
