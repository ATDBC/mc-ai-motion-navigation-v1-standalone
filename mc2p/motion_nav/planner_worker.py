"""One bounded background process for B04 global route search."""
from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full
import math
import multiprocessing
import time

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.known_map_planner import (
    KnownMapSnapshot, PlanningRequest, PlanningStatus, RouteCandidate, WalkGraph,
    plan_known_snapshot,
    SurfaceGraph, SurfacePlanningRequest, SurfacePlanningStatus,
    SurfaceRouteCandidate,
    plan_known_surface_snapshot,
)
from mc2p.motion_nav.planning_reference import astar_plan, astar_surface_plan
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_modes import GroundModeProfile
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.jump_up import JumpUpProfile
from mc2p.motion_nav.step_transition import StepProfile


@dataclass(frozen=True, slots=True)
class _PlanningJob:
    graph: WalkGraph | SurfaceGraph | None
    snapshot: KnownMapSnapshot | None
    profile: GroundMotionProfile | None
    ground_mode_profile: GroundModeProfile | None
    step_profile: StepProfile | None
    jump_profile: JumpUpProfile | None
    air_profiles: tuple[AirMotionProfile, ...]
    request: PlanningRequest | SurfacePlanningRequest

    def __post_init__(self) -> None:
        if (self.graph is None) == (self.snapshot is None):
            raise ContractViolation("planning job requires exactly one map source")
        if self.snapshot is not None and type(self.profile) is not GroundMotionProfile:
            raise ContractViolation("snapshot planning requires a motion profile")
        if ((type(self.graph) is SurfaceGraph) !=
                (type(self.request) is SurfacePlanningRequest)):
            if self.graph is not None:
                raise ContractViolation("surface graph requires a surface planning request")
        if self.snapshot is not None:
            surface = type(self.request) is SurfacePlanningRequest
            if surface != (type(self.step_profile) is StepProfile):
                raise ContractViolation(
                    "surface snapshot requires a surface request and Step profile"
                )
        if (type(self.air_profiles) is not tuple
                or any(type(profile) is not AirMotionProfile
                       for profile in self.air_profiles)):
            raise ContractViolation("planning job air profiles must be typed")
        if type(self.request) is not SurfacePlanningRequest and self.air_profiles:
            raise ContractViolation("air profiles require surface planning")


def _replace(queue, value) -> bool:
    try:
        queue.put_nowait(value);return True
    except Full:
        try:queue.get_nowait()
        except Empty:return False
        try:
            queue.put_nowait(value);return True
        except Full:return False


def _publish_latest(queue, value) -> None:
    """Reliably replace a stale result from the background process.

    ``multiprocessing.Queue`` can report ``Full`` before its feeder thread has
    made the old item readable.  The worker may wait for that hand-off; the
    control thread never calls this function.
    """
    while True:
        try:
            queue.put(value, timeout=.01)
            return
        except Full:
            try:
                queue.get(timeout=.01)
            except Empty:
                continue


def _failure_candidate(job: _PlanningJob, reason: str):
    request = job.request
    source = job.graph if job.graph is not None else job.snapshot
    geometry_revision = (
        source.geometry_revision if job.graph is not None
        else source.world.geometry_revision
    )
    if type(request) is SurfacePlanningRequest:
        return SurfaceRouteCandidate(
            request.sequence, request.request_id, request.goal_id,
            request.goal_revision, request.world_session, geometry_revision,
            request.start, request.goal, SurfacePlanningStatus.INTERNAL_ERROR,
            (), (), None, (), 0, goal_state=request.goal_state,
            initial_resources=request.initial_resources,
            minimum_resources=request.minimum_resources,
            reasons=(reason,),
        )
    return RouteCandidate(
        request.sequence, request.request_id, request.goal_id,
        request.goal_revision, request.world_session, geometry_revision,
        request.start, request.goal, PlanningStatus.INTERNAL_ERROR,
        (), (), None, (), 0, goal_state=request.goal_state,
        reasons=(reason,),
    )


def _execute_job(job: _PlanningJob):
    try:
        if type(job.graph) is SurfaceGraph:
            return astar_surface_plan(job.graph, job.request)
        if job.graph is not None:
            return astar_plan(job.graph, job.request)
        if type(job.request) is SurfacePlanningRequest:
            return plan_known_surface_snapshot(
                job.snapshot, job.profile, job.step_profile, job.request,
                job.jump_profile, air_profiles=job.air_profiles,
                ground_mode_profile=job.ground_mode_profile,
            )
        return plan_known_snapshot(
            job.snapshot, job.profile, job.request, job.jump_profile,
        )
    except Exception as error:
        return _failure_candidate(job, type(error).__name__)


def _worker(requests, results, delay_seconds: float) -> None:
    while True:
        job=requests.get()
        if job is None:return
        while True:
            try:latest=requests.get_nowait()
            except Empty:break
            if latest is None:return
            job=latest
        if delay_seconds:time.sleep(delay_seconds)
        candidate = _execute_job(job)
        _publish_latest(results,candidate)


class PlannerWorker:
    """Own a real process; control-side methods never wait for planning."""

    def __init__(self, *, debug_delay_seconds: float = 0.0) -> None:
        if (type(debug_delay_seconds) not in (int,float)
                or not math.isfinite(float(debug_delay_seconds))
                or not 0<=debug_delay_seconds<=2):
            raise ContractViolation("planner debug delay must be within 0..2 seconds")
        self._context=multiprocessing.get_context("spawn")
        self._requests=self._context.Queue(maxsize=1)
        self._results=self._context.Queue(maxsize=1)
        self._process=self._context.Process(
            target=_worker,args=(self._requests,self._results,float(debug_delay_seconds)),
            name="mc2p-route-planner",daemon=True,
        )
        self._process.start();self._closed=False
        self._pending:_PlanningJob|None=None
        self._last_submitted:_PlanningJob|None=None
        self._death_reported=False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def submit(self, graph: WalkGraph, request: PlanningRequest) -> bool:
        """Submit a frozen materialized graph for compatibility tests only."""
        if self._closed:raise ContractViolation("planner worker is closed")
        if type(graph) is not WalkGraph or type(request) is not PlanningRequest:
            raise ContractViolation("planner submission requires graph and request")
        self._pending=_PlanningJob(graph,None,None,None,None,None,(),request)
        self._flush_pending()
        return True

    def submit_surface(self, graph: SurfaceGraph,
                       request: SurfacePlanningRequest) -> bool:
        """Submit a materialized surface graph for diagnostics only."""
        if self._closed:
            raise ContractViolation("planner worker is closed")
        if type(graph) is not SurfaceGraph or type(request) is not SurfacePlanningRequest:
            raise ContractViolation("surface planner submission requires graph and request")
        self._pending = _PlanningJob(graph, None, None, None, None, None, (), request)
        self._flush_pending()
        return True

    def submit_snapshot(self, snapshot: KnownMapSnapshot,
                        profile: GroundMotionProfile,
                        request: PlanningRequest,
                        jump_profile: JumpUpProfile | None = None) -> bool:
        if self._closed:raise ContractViolation("planner worker is closed")
        if (type(snapshot) is not KnownMapSnapshot
                or type(profile) is not GroundMotionProfile
                or type(request) is not PlanningRequest):
            raise ContractViolation("snapshot submission requires snapshot, profile and request")
        if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
            raise ContractViolation("snapshot submission requires a JumpUp profile or None")
        self._pending=_PlanningJob(None,snapshot,profile,None,None,jump_profile,(),request)
        self._flush_pending()
        return True

    def submit_surface_snapshot(
        self,
        snapshot: KnownMapSnapshot,
        ground_profile: GroundMotionProfile,
        step_profile: StepProfile,
        request: SurfacePlanningRequest,
        jump_profile: JumpUpProfile | None = None,
        *,
        air_profiles: tuple[AirMotionProfile, ...] = (),
        ground_mode_profile: GroundModeProfile | None = None,
    ) -> bool:
        if self._closed:
            raise ContractViolation("planner worker is closed")
        if (type(snapshot) is not KnownMapSnapshot
                or type(ground_profile) is not GroundMotionProfile
                or type(step_profile) is not StepProfile
                or type(request) is not SurfacePlanningRequest):
            raise ContractViolation(
                "surface snapshot submission requires snapshot, profiles and request"
            )
        if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
            raise ContractViolation(
                "surface snapshot submission requires a JumpUp profile or None"
            )
        if (type(air_profiles) is not tuple
                or any(type(profile) is not AirMotionProfile for profile in air_profiles)):
            raise ContractViolation(
                "surface snapshot submission requires typed air profiles"
            )
        if (ground_mode_profile is not None
                and type(ground_mode_profile) is not GroundModeProfile):
            raise ContractViolation(
                "surface snapshot submission requires a typed ground mode profile"
            )
        self._pending = _PlanningJob(
            None, snapshot, ground_profile, ground_mode_profile,
            step_profile, jump_profile,
            air_profiles, request,
        )
        self._flush_pending()
        return True

    def _flush_pending(self) -> None:
        if self._pending is not None and _replace(self._requests,self._pending):
            self._last_submitted=self._pending
            self._death_reported=False
            self._pending=None

    def poll_latest(self) -> RouteCandidate | SurfaceRouteCandidate | None:
        self._flush_pending()
        latest=None
        while True:
            try:candidate=self._results.get_nowait()
            except Empty:
                self._flush_pending()
                if latest is not None:
                    if (self._last_submitted is not None
                            and latest.request_sequence >= self._last_submitted.request.sequence):
                        self._last_submitted=None
                    return latest
                if (not self._closed and not self._process.is_alive()
                        and self._last_submitted is not None
                        and not self._death_reported):
                    self._death_reported=True
                    failed=_failure_candidate(
                        self._last_submitted, "planner_worker_died",
                    )
                    self._last_submitted=None
                    return failed
                return None
            if latest is None or candidate.request_sequence>=latest.request_sequence:
                latest=candidate

    def terminate(self) -> None:
        if self._process.is_alive():self._process.terminate()

    def join(self, timeout: float | None = None) -> None:
        self._process.join(timeout)

    def close(self) -> None:
        if self._closed:return
        self._closed=True
        self._pending=None
        if self._process.is_alive():
            _replace(self._requests,None)
            self._process.join(2)
        if self._process.is_alive():
            self._process.terminate();self._process.join(2)
        for queue in (self._requests,self._results):
            queue.close();queue.cancel_join_thread()

    def __enter__(self):return self

    def __exit__(self,*_):self.close()
