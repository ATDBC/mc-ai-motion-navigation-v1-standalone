"""One bounded background process for B04 global route search."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from queue import Empty, Full
import math
import multiprocessing
import time
from typing import Protocol, runtime_checkable

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


def _publish(queue, value) -> None:
    """Wait only in the worker process; never overwrite an accepted result."""
    while True:
        try:
            queue.put(value, timeout=.01)
            return
        except Full:
            continue


class PlanningSubmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    BUSY = "busy"

    def __bool__(self) -> bool:
        return self is PlanningSubmissionStatus.ACCEPTED


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
            work_identity=request.work_identity,
        )
    return RouteCandidate(
        request.sequence, request.request_id, request.goal_id,
        request.goal_revision, request.world_session, geometry_revision,
        request.start, request.goal, PlanningStatus.INTERNAL_ERROR,
        (), (), None, (), 0, goal_state=request.goal_state,
        reasons=(reason,),
        work_identity=request.work_identity,
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
        if delay_seconds:time.sleep(delay_seconds)
        candidate = _execute_job(job)
        _publish(results,candidate)


@runtime_checkable
class PlannerWorkerPort(Protocol):
    """Formal surface-planning interface used by process and inline workers."""

    def submit_surface_snapshot(
        self, snapshot: KnownMapSnapshot, ground_profile: GroundMotionProfile,
        step_profile: StepProfile, request: SurfacePlanningRequest,
        jump_profile: JumpUpProfile | None = None, *,
        air_profiles: tuple[AirMotionProfile, ...] = (),
        ground_mode_profile: GroundModeProfile | None = None,
    ) -> PlanningSubmissionStatus: ...
    def poll_available(self) -> tuple[RouteCandidate | SurfaceRouteCandidate, ...]: ...
    def poll_latest(self) -> RouteCandidate | SurfaceRouteCandidate | None: ...
    def close(self) -> None: ...
    def is_alive(self) -> bool: ...


class PlannerWorker:
    """Own a real process; control-side methods never wait for planning."""

    def __init__(self, *, debug_delay_seconds: float = 0.0) -> None:
        if (type(debug_delay_seconds) not in (int,float)
                or not math.isfinite(float(debug_delay_seconds))
                or not 0<=debug_delay_seconds<=2):
            raise ContractViolation("planner debug delay must be within 0..2 seconds")
        self._context=multiprocessing.get_context("spawn")
        self._requests=self._context.Queue(maxsize=2)
        self._results=self._context.Queue(maxsize=2)
        self._process=self._context.Process(
            target=_worker,args=(self._requests,self._results,float(debug_delay_seconds)),
            name="mc2p-route-planner",daemon=True,
        )
        self._process.start();self._closed=False
        self._submitted: list[_PlanningJob] = []
        self._diagnostic_deliveries = []

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def submit(self, graph: WalkGraph, request: PlanningRequest) -> PlanningSubmissionStatus:
        """Submit a frozen materialized graph for compatibility tests only."""
        if self._closed:raise ContractViolation("planner worker is closed")
        if type(graph) is not WalkGraph or type(request) is not PlanningRequest:
            raise ContractViolation("planner submission requires graph and request")
        return self._enqueue(_PlanningJob(graph,None,None,None,None,None,(),request))

    def submit_surface(self, graph: SurfaceGraph,
                       request: SurfacePlanningRequest) -> PlanningSubmissionStatus:
        """Submit a materialized surface graph for diagnostics only."""
        if self._closed:
            raise ContractViolation("planner worker is closed")
        if type(graph) is not SurfaceGraph or type(request) is not SurfacePlanningRequest:
            raise ContractViolation("surface planner submission requires graph and request")
        return self._enqueue(_PlanningJob(graph, None, None, None, None, None, (), request))

    def submit_snapshot(self, snapshot: KnownMapSnapshot,
                        profile: GroundMotionProfile,
                        request: PlanningRequest,
                        jump_profile: JumpUpProfile | None = None) -> PlanningSubmissionStatus:
        if self._closed:raise ContractViolation("planner worker is closed")
        if (type(snapshot) is not KnownMapSnapshot
                or type(profile) is not GroundMotionProfile
                or type(request) is not PlanningRequest):
            raise ContractViolation("snapshot submission requires snapshot, profile and request")
        if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
            raise ContractViolation("snapshot submission requires a JumpUp profile or None")
        return self._enqueue(_PlanningJob(None,snapshot,profile,None,None,jump_profile,(),request))

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
    ) -> PlanningSubmissionStatus:
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
        return self._enqueue(_PlanningJob(
            None, snapshot, ground_profile, ground_mode_profile,
            step_profile, jump_profile,
            air_profiles, request,
        ))

    def _enqueue(self, job: _PlanningJob) -> PlanningSubmissionStatus:
        if len(self._submitted) >= 2:
            return PlanningSubmissionStatus.BUSY
        try:
            self._requests.put_nowait(job)
        except Full:
            return PlanningSubmissionStatus.BUSY
        self._submitted.append(job)
        return PlanningSubmissionStatus.ACCEPTED

    @property
    def outstanding_identities(self):
        return tuple(job.request.work_identity for job in self._submitted)

    def poll_latest(self) -> RouteCandidate | SurfaceRouteCandidate | None:
        """Compatibility read for diagnostics; formal owners use poll_available."""
        if not self._diagnostic_deliveries:
            self._diagnostic_deliveries.extend(self.poll_available())
        if not self._diagnostic_deliveries:
            return None
        return self._diagnostic_deliveries.pop(0)

    def poll_available(self) -> tuple[RouteCandidate | SurfaceRouteCandidate, ...]:
        delivered = []
        for _ in range(2):
            try:candidate=self._results.get_nowait()
            except Empty:
                break
            delivered.append(candidate)
            matched = next((job for job in self._submitted
                if self._matches_submission(candidate, job)), None)
            if matched is not None:
                self._submitted.remove(matched)
        if not self._closed and not self._process.is_alive():
            for job in tuple(self._submitted)[:2 - len(delivered)]:
                delivered.append(_failure_candidate(job, "planner_worker_died"))
                self._submitted.remove(job)
        return tuple(delivered)

    @staticmethod
    def _matches_submission(candidate, job: _PlanningJob) -> bool:
        request = job.request
        if request.work_identity is not None:
            return candidate.work_identity == request.work_identity
        # Diagnostic jobs have no computation scope. Match their explicit
        # producer fields; never manufacture a generation for them.
        return (candidate.work_identity is None
                and (candidate.world_session, candidate.request_id,
                     candidate.request_sequence, candidate.goal_id, candidate.goal_revision)
                == (request.world_session, request.request_id,
                    request.sequence, request.goal_id, request.goal_revision))

    def terminate(self) -> None:
        if self._process.is_alive():self._process.terminate()

    def join(self, timeout: float | None = None) -> None:
        self._process.join(timeout)

    def close(self) -> None:
        if self._closed:return
        self._closed=True
        self._submitted.clear()
        self._diagnostic_deliveries.clear()
        if self._process.is_alive():
            _replace(self._requests,None)
            self._process.join(2)
        if self._process.is_alive():
            self._process.terminate();self._process.join(2)
        for queue in (self._requests,self._results):
            queue.close();queue.cancel_join_thread()

    def __enter__(self):return self

    def __exit__(self,*_):self.close()
