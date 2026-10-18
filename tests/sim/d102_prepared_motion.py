"""G2-only observations of the existing Task 8 owners and result service.

The inline port is a fast transport for the same _execute_job producer. Spawn
runs use the unmodified MotionSolverWorker. No alternative registry or receiver
is provided here. Files are written only by explicit callers with fresh output.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import pickle
import platform
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace
import uuid

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.motion_activation import ActivationStatus, PreparedStreamDispatch
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import TaskIntentV0, SuccessCriterionV0, ComparisonOperatorV0
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState, decision_input_window
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.body_control import HandoffDisposition, StopCause
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.motion_candidate import AdmittedMotionCandidate, MotionCandidateContext, VerifiedMotionCandidate
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion, solve_one_cell_gap
from mc2p.motion_nav.motion_worker import MotionResultInbox, MotionSolverWorker, PreparedMotionJob, _execute_job
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import CandidateExecutionWindow, project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.prepared_motion import (
    PreparedSubmitStatus,
    ProofUse, ProofConsumerIdentity, ProofActionExitEvidence, ProofSupervisorReleaseEvidence,
    ProofReleaseEvidence, ReferenceStatus, StreamGeneration,
)
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest
from mc2p.motion_nav.prepared_motion_proof import prepare_fixed_motion, seal_bundle
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.segment_entry import MotionContinuationRequirement, SegmentEntryWindow
from mc2p.motion_nav.support_surfaces import HorizontalRegion, SupportSurface, SurfaceNodeId
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp, WorldSessionId
from mc2p.motion_nav.world_snapshot import seal_snapshot_version
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from tests.motion_nav.d102_contract_cases import canonical_bytes, canonical_prepared_payloads, digest
from tests.motion_nav.test_b10_motion_candidate import VerifiedMotionRouteIntegrationTests
from tests.motion_nav.test_d102_action_handoff import ActionHandoffTests
from tests.motion_nav.test_d102_runtime_activation import ActivationTests, _Backend
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.test_player_runtime import _RecordingTrace


ROOT = Path(__file__).resolve().parents[2]
PHASES = ("observation", "handle", "comparison", "ledger_receipt", "budget", "handoff", "arbitration", "send")
FORBIDDEN_OPERATIONS = ("scan", "search", "proof_hash", "serialize", "deserialize", "snapshot",
                        "physics_replay", "immutable_graph", "blocking_wait", "long_lock_wait")
GROUPS = {"fast": "fast", "spawn-smoke": "spawn_smoke", "realtime": "realtime", "fallback": "fallback"}
PERIOD_NS = 50_000_000
G2_PATHS = {"fast": "fast", "spawn-smoke": "spawn", "realtime": "realtime", "fallback": "fallback"}
NEGATIVE_RESULTS = dict(identity="REQUEST", scope="GENERATION", inputs="INPUTS", ruleset="RULESET",
    unknown="MISSING_INFORMATION", entry="ENTRY_MISMATCH", dependency="DEPENDENCY",
    cancel_before_publish="CANCELLED", cancel_after_publish="STALE", late_delivery="OVERDUE",
    midstream_missing="DEVIATION", worker_hard_exit="WORKER_FAILED")


def _read(path):
    return json.loads(Path(path).read_text("utf-8"))


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fresh(output):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("OUTPUT_EXISTS: " + str(output))
    output.mkdir(parents=True, exist_ok=True)
    return output


def matrix_cases(stage, manifest):
    frozen = _read(manifest)
    axes = frozen["matrix_axes"]
    widths, starts = axes["platform_width_blocks"], axes["start_offset_ticks"]
    if stage == "fast":
        names = ("platform_width", "start_offset", "entry_offset", "direction", "backlog")
        values = (widths, starts, axes["entry_offset_blocks"], axes["directions"], axes["backlog_jobs"])
    elif stage == "realtime":
        names = ("platform_width", "start_offset", "backlog", "repetition")
        values = (widths, starts, axes["backlog_jobs"], range(1, axes["realtime_repetitions"] + 1))
    elif stage in ("spawn-smoke", "fallback"):
        names, values = ("platform_width", "start_offset"), (widths, starts)
    else:
        raise ValueError("unsupported G2 stage")
    rows = [dict(zip(names, coordinate)) for coordinate in itertools.product(*values)]
    if stage == "spawn-smoke":
        rows.append(dict(platform_width=2, start_offset=0, backlog=1))
    for row in rows:
        row.setdefault("direction", "south")
        row.setdefault("entry_offset", 0.)
        row.setdefault("backlog", 0)
    return tuple(rows)


def realtime_samples(rows):
    return [row["complete_decision_ns"] for row in rows
            if row.get("group") == "realtime" and row.get("warmup") is False]


def _quantiles(values):
    values = sorted(values)
    if not values:
        return dict(count=0, p95_ms=None, p99_ms=None, max_ms=None)
    return dict(count=len(values), p95_ms=values[math.ceil(.95 * len(values)) - 1] / 1e6,
                p99_ms=values[math.ceil(.99 * len(values)) - 1] / 1e6, max_ms=max(values) / 1e6)


class _InlinePort:
    """Execute the real frozen job synchronously on the sole inbox receiver."""
    def __init__(self):
        self.results, self.alive = [], True
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)
        self.results.append(_execute_job(job))
        return True

    def poll_available(self):
        values, self.results = tuple(self.results), []
        return values

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False


class _ControlMeter:
    """Measure either call provenance or elapsed time, never both formally."""
    MODES = frozenset({"legacy-combined", "call-count", "performance"})

    def __init__(self, measurement_mode="legacy-combined"):
        if measurement_mode not in self.MODES:
            raise ValueError("unknown G2 measurement mode: " + repr(measurement_mode))
        self.measurement_mode = measurement_mode
        self.profile_enabled = measurement_mode in ("legacy-combined", "call-count")
        self.timing_valid = measurement_mode in ("legacy-combined", "performance")
        self.calls = dict.fromkeys(FORBIDDEN_OPERATIONS, 0)
        self.fixture_calls = dict.fromkeys(FORBIDDEN_OPERATIONS, 0)
        self.in_fixture_backend = False
        self.samples = []
        self.active = False
        self.phase = None
        self.tick_events = []
        self.sent_sequences = []
        self.lock_started = {}

    def _profile(self, frame, event, arg):
        if not self.active:
            return
        name = frame.f_code.co_name
        module = frame.f_globals.get("__name__", "")
        category = None
        if event == "call":
            if name in ("scan_commitment", "prepare_fixed_motion", "_independent_scan", "scan_full"):
                category = "scan"
            elif name in ("solve_one_cell_gap", "solve_air_transition", "search", "bounded_search"):
                category = "search"
            elif name in ("canonical_motion_digest", "canonical_bundle_bytes", "canonical_motion_bytes"):
                category = "proof_hash"
            elif name in ("seal_snapshot_version", "canonical_snapshot_bytes", "borrow_snapshot_version", "try_borrow_snapshot_version", "snapshot") and module in ("mc2p.motion_nav.world_model", "mc2p.motion_nav.world_snapshot", "mc2p.motion_nav.physics_adapter"):
                category = "snapshot"
            elif name in ("revalidate_air_transition", "verified_ground_rollout", "_release_recovery_evidence", "rollout", "rollout_with_trace", "_rollout_air_template") or (name == "step" and module == "mc2p.motion_nav.physics_1_21"):
                category = "physics_replay"
            elif name == "require_immutable_graph":
                category = "immutable_graph"
            elif name in ("wait_for_receiver", "join") and module.startswith(("mc2p", "threading")):
                category = "blocking_wait"
            elif name in ("dumps", "dump") and module in ("json", "pickle"):
                category = "serialize"
            elif name in ("loads", "load") and module in ("json", "pickle"):
                category = "deserialize"
        elif event == "c_call":
            owner = getattr(arg, "__module__", "")
            called = getattr(arg, "__name__", "")
            if owner == "_pickle":
                category = "serialize" if called in ("dump", "dumps") else "deserialize" if called in ("load", "loads") else None
            elif owner in ("_hashlib", "hashlib") and "sha" in called:
                category = "proof_hash"
            if called == "acquire":
                self.lock_started[id(arg)] = time.perf_counter_ns()
        elif event in ("c_return", "c_exception") and getattr(arg, "__name__", "") == "acquire":
            started = self.lock_started.pop(id(arg), None)
            if started is not None and time.perf_counter_ns() - started >= 1_000_000:
                category = "long_lock_wait"
        if category is not None:
            counts = self.fixture_calls if self.in_fixture_backend else self.calls
            counts[category] += 1
            self.tick_events.append(dict(operation=category, function=module + "." + name,
                timestamp_ns=time.perf_counter_ns(), phase=self.phase,
                callers=self._callers(frame), provenance="fixture_backend" if self.in_fixture_backend else "control"))

    @staticmethod
    def _callers(frame):
        names = []
        while frame is not None and len(names) < 8:
            names.append(frame.f_globals.get("__name__", "") + "." + frame.f_code.co_name)
            frame = frame.f_back
        return names

    @contextmanager
    def tick(self, kind, movement_tick, *, due_ns=None):
        previous = sys.getprofile()
        if self.measurement_mode == "performance" and previous is not None:
            raise ValueError("performance measurement requires sys.setprofile to be disabled")
        self.started = time.perf_counter_ns() if self.timing_valid else None
        self.phase_started = self.started
        self.phase, self.phases = "observation", dict.fromkeys(PHASES, 0)
        self.phase_calls = dict.fromkeys(PHASES, 0)
        self.tick_events = []
        self.sent_sequences = []
        before = dict(self.calls)
        self.active = True
        if self.profile_enabled:
            sys.setprofile(self._profile)
        try:
            yield
        finally:
            self._switch(None)
            self.active = False
            if self.profile_enabled:
                sys.setprofile(previous)
            sample = dict(kind=kind, movement_tick=movement_tick,
                phase_calls=self.phase_calls, sent_sequences=list(self.sent_sequences))
            if self.profile_enabled:
                sample.update(forbidden_calls={k: self.calls[k] - before[k] for k in before},
                              forbidden_events=list(self.tick_events))
            if self.timing_valid:
                ended = time.perf_counter_ns()
                sample.update(started_ns=self.started, ended_ns=ended,
                    duration_ns=ended - self.started, phases_ns=self.phases, scheduled_ns=due_ns,
                    wake_lateness_ns=None if due_ns is None else max(0, self.started - due_ns),
                    deadline_miss=due_ns is not None and ended > due_ns + PERIOD_NS)
            self.samples.append(sample)

    def _switch(self, name):
        now = time.perf_counter_ns() if self.timing_valid else None
        if self.phase is not None and self.timing_valid:
            self.phases[self.phase] += now - self.phase_started
        self.phase, self.phase_started = name, now
        if name is not None:
            self.phase_calls[name] += 1

    @contextmanager
    def span(self, name):
        if not self.active:
            yield
            return
        previous = self.phase
        self._switch(name)
        try:
            yield
        finally:
            self._switch(previous)


class _Case(ActivationTests):
    """Thin isolated fixture; Runtime, coordinator, supervisor and inbox are real."""
    def __init__(self, axes, inbox, worker, *, realtime=False,
                 measurement_mode="legacy-combined"):
        super().__init__("runTest")
        self.axes, self.meter, self.inbox, self.worker = axes, _ControlMeter(measurement_mode), inbox, worker
        self.negative = axes.get("negative_case_id")
        self.clock, self.events = [100_000_000], []
        self.task = TaskIntentV0("g2-task", "navigate", "{}",
            (SuccessCriterionV0("done", ComparisonOperatorV0.EQUAL, 1., "bool"),), 1, 2_000_000_000, True, 0.)
        self.profile = BehaviorProfileV0()
        self.native_events = []
        self.f = SimpleNamespace(anchor=ActionHandoffTests.anchor, base=SimpleNamespace(ruleset=JAVA_1_21_RULESET))
        direction = {"south": (0, 1), "west": (-1, 0), "north": (0, -1), "east": (1, 0)}[axes["direction"]]
        self.direction = direction
        dx, dz = direction
        yaw = math.atan2(-dx, dz)
        offset = float(axes["entry_offset"])
        position = (.5 + dz * offset, 1., .5 - dx * offset)
        session = WorldSessionId("fixture:episode:clock")
        state = PhysicsState(JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema, session, 10,
            position, (dx * 2.35 / 20, -.0784000015258789, dz * 2.35 / 20), yaw, 0.,
            "standing", .6, 1.8, True, False, True, False, False, 0, 0., .1, .6, .08, .42,
            20, 5., "survival", (), False, False, False, False, False, False)
        self.f.current = self.f.anchor(state, 0)
        self.backend = _Backend(self.f, self.clock, self.events)
        self.runtime = PlayerRuntimeV1(self.backend, _RecordingTrace(), clock_ns=lambda: self.clock[0])
        self.runtime.reset(ResetRequestV0("reset", "episode", "d102-g2", 1, 2_000_000_000))
        world = self.runtime.snapshot_world_port
        width = axes["platform_width"]
        gap = 2 + width
        def rotate(x, z):
            return (int(dz * x + dx * z), int(-dx * x + dz * z))
        cells = []
        blocks = {}
        stone = BlockGeometry.full_cube("minecraft:stone")
        for x in range(-3, 4):
            for z in range(-3, gap + 7):
                rx, rz = rotate(x, z)
                for y in range(-4, 8):
                    cells.append((rx, y, rz))
                if z not in (1, gap):
                    blocks[(rx, 0, rz)] = stone
        stamp = ObservationStamp(world.session, 0, 10, "G2-observation", 0)
        world.confirm_air(stamp, tuple(p for p in cells if p not in blocks))
        world.observe_blocks(stamp, blocks)
        min_x, max_x = min(p[0] for p in cells), max(p[0] for p in cells)
        min_z, max_z = min(p[2] for p in cells), max(p[2] for p in cells)
        self.f.bounds = PhysicsWorldBounds(min_x, max_x, -4, 7, min_z, max_z)
        version = world.borrow_snapshot_version(self.f.bounds, ruleset=JAVA_1_21_RULESET)
        snapshot = seal_snapshot_version(version, JAVA_1_21_RULESET)
        if not world.publish_sealed_snapshot(version, snapshot):
            raise AssertionError("formal observed facts were not published")
        self.f.world, self.f.snapshot = world, snapshot
        # Physical terrain stays fixed when the observed knowledge loses a fact.
        # The worker still obtains its own lease from the Runtime world owner.
        self.f.physics_world = PhysicsWorldView.from_snapshot(snapshot)
        def frame(anchor):
            value = VerifiedMotionRouteIntegrationTests.frame(world.view(), anchor.physics_state,
                                                             anchor.observation_sequence_id)
            return replace(value, body=replace(value.body, movement_tick_id=anchor.movement_tick_id))
        self.f.frame = frame
        self.inbox.bind_world(world)
        self.f.inbox, self.f.worker = inbox, worker
        self.f.ledger = self.runtime.input_ledger
        self.f.ledger_id = self.f.ledger.ledger_identity
        def point(progress):
            return (.5 + dx * progress + dz * offset, 1., .5 + dz * progress - dx * offset)
        def landing(lo, hi):
            start, end = point(lo), point(hi)
            return LandingRegion(min(start[0], end[0]) - (.2 if dz else 0),
                max(start[0], end[0]) + (.2 if dz else 0),
                min(start[2], end[2]) - (.2 if dx else 0),
                max(start[2], end[2]) + (.2 if dx else 0), 1.)
        continuation = MotionContinuationRequirement(SegmentEntryWindow(point(gap - 1.4),
            (float(dx), float(dz)), -.05, .35, .2, 1. - 1e-7, 1. + 1e-7,
            .5, 3., math.radians(5), frozenset({"standing"}), frozenset({MovementMode.WALK}),
            yaw, math.radians(1), "g2-isolated-continuation"), MovementMode.WALK, "g2-middle-platform")
        first = solve_one_cell_gap(self.f.current, self.f.physics_world, GapSolveRequest(direction,
            landing(1.8, gap - 1.), CandidateExecutionWindow(11, 12), max_ticks=40,
            exit_direction=direction, exit_motion_ticks=1, continuation=continuation))
        if first.proof is None:
            raise AssertionError("first fixed SOLVE did not produce a proof: " + repr(first))
        first_entry = first.proof.exit_state
        if axes["start_offset"]:
            first_entry = first.proof.delayed_start_variants[0].exit_state
        self.second_selection = GapSolveRequest(direction, landing(gap + .8, gap + 1.2),
                CandidateExecutionWindow(first_entry.movement_tick_id + 1, first_entry.movement_tick_id + 2))
        second = solve_one_cell_gap(self.f.anchor(first_entry, 1), self.f.physics_world, self.second_selection)
        if second.proof is None:
            raise AssertionError("second fixed SOLVE did not produce a proof: " + repr(second))
        self.f.proofs = (first.proof, second.proof)
        surfaces = []
        for i, progress in enumerate((0, 2, gap + 1)):
            p = point(progress)
            cell_x, cell_z = math.floor(p[0]), math.floor(p[2])
            node = SurfaceNodeId(cell_x, cell_z, 1, 0)
            surfaces.append(SupportSurface(node, p, HorizontalRegion(cell_x, cell_z, cell_x + 1, cell_z + 1),
                                          1., ("minecraft:stone",), ()))
        route_id = "g2-" + uuid.uuid4().hex
        actions = tuple(JumpGapSegment(JumpGapEdge(a.node_id, b.node_id, "g2-isolated-gap", .9, ()),
                        a, b, ()) for a, b in zip(surfaces, surfaces[1:]))
        self.f.route = ActiveRoute(route_id, 1, "request-" + route_id, "goal-" + route_id, 1,
            world.session.value, None, float(gap + 1), 0., (),
            ExecutableCorridor(tuple(s.node_id for s in surfaces), (), float(gap + 1), surfaces[-1].node_id),
            ActionRoute(route_id, actions))
        self.f.executor = ActionRouteExecutor(ground_profile(), jump_profile())
        self.f.coordinator = MotionRouteCoordinator(self.f.route, self.f.executor, worker, result_inbox=inbox,
            retry_ledger=RetryLedger(route_id), computation_scope=AsyncComputationScope(world.session.value, route_id, 1))
        self.f.coordinator.start(self.f.frame(self.f.current))
        for index, proof in enumerate(self.f.proofs):
            context = MotionCandidateContext(self.f.route.source_request_id, 0, self.f.route.goal_id, 1,
                route_id, 1, index, index + 1, TaskDamageBudget(), proof.resource_incomplete_reasons)
            self.f.executor.install_verified_motion(AdmittedMotionCandidate(VerifiedMotionCandidate(proof, context),
                proof.anchor_observation_sequence_id, proof.anchor_movement_tick_id,
                proof.execution_window.earliest_start_tick))
        self.f.control = RouteControl(self.f.route, self.f.executor, self.f.coordinator)
        self.f.supervisor = ExecutionSupervisor()
        self.f.supervisor.offer_route(self.f.control, self.f.frame(self.f.current), self.f.ledger, self.f.current)
        self.handle, self.proposal = None, None
        self.realtime = realtime
        self.next_due = None
        self.request_started = None
        self.prepare_durations = []
        self.validation_durations = []
        self.transport_events = []
        self.motion_trace = []
        self.activation_events = []
        self.application_events = []
        self.release_events = []
        self.background_stages = []
        self.publish_entered = threading.Event()
        self.publish_continue = threading.Event()
        self.native_rejection = None
        self.verified_submissions = []
        self.stop_requested = False
        self.backlog_identity = None
        self.resource_high_water = dict.fromkeys(("unfinished_jobs_total", "request_fifo", "result_fifo",
            "registered_bundles", "snapshot_leases", "snapshot_cells", "sealed_request_bytes",
            "sealed_result_bytes", "deduplicated_total_bytes", "diagnostic_history"), 0)
        self._instrument()

    def _instrument(self):
        inbox, executor = self.inbox, self.f.executor
        self.saved = []
        def wrap(obj, name, factory):
            original = getattr(obj, name)
            self.saved.append((obj, name, original))
            setattr(obj, name, factory(original))
        def publish(original):
            def call(record, *args):
                started = time.perf_counter_ns()
                if self.negative in ("cancel_before_publish", "late_delivery") and record.action_preparation is not None:
                    self.publish_entered.set()
                    if not self.publish_continue.wait(10):
                        raise AssertionError("negative publication barrier was not released")
                result = original(record, *args)
                admission = inbox.prepared_admission(record.identity)
                if admission is not None and admission.handle is not None and record.request.stream.action_index == 1:
                    handle = admission.handle
                    self.native_events.append(dict(operation="publish", producer="MotionResultInbox",
                        timestamp_ns=time.perf_counter_ns(), proof_token=handle.proof_token,
                        thread_id=threading.get_ident(), thread_name=threading.current_thread().name))
                self.validation_durations.append(time.perf_counter_ns() - started)
                if self.negative and admission is not None and record.action_preparation is not None:
                    self.native_rejection = dict(admission=asdict(admission),
                        receiver_thread_id=threading.get_ident(), operation="admission", timestamp_ns=time.perf_counter_ns())
                self.resources()
                return result
            return call
        wrap(inbox, "_publish_prepared", publish)
        def install(original):
            def call(handle):
                result = original(handle)
                if not any(e["operation"] == "first_consume" and e["proof_token"] == handle.proof_token for e in self.native_events):
                    self.native_events.append(dict(operation="first_consume", producer="ActionRouteExecutor",
                        timestamp_ns=time.perf_counter_ns(), proof_token=handle.proof_token,
                        thread_id=threading.get_ident(), thread_name=threading.current_thread().name))
                return result
            return call
        wrap(executor, "install_prepared_motion", install)
        def backend(original):
            def call(*args, **kwargs):
                with self.meter.span("send"):
                    action = args[0]
                    if self.meter.active:
                        self.meter.sent_sequences.append(action.request_sequence_id)
                    before = self.backend.state
                    registered = next((record for record in self.inbox._registry.values()
                        if any(command.request_sequence_id == action.request_sequence_id for command in record.commands)), None)
                    supervisor_owner = None if self.f.supervisor.route is None else self.f.supervisor.route.owner_id
                    source = ("verified_motion" if any(e["sequence"] == action.request_sequence_id
                        for e in self.verified_submissions) else "prepared_motion" if registered is not None
                        else "original_owner_stop" if self.stop_requested and action.movement == MovementV1()
                        else "original_owner_terminal" if action.movement == MovementV1()
                            and getattr(getattr(self, "last_route_decision", None), "reason_code", None) == "action_route_complete"
                        else "verified_safe_hold" if getattr(self, "safe_hold", False) else None)
                    self.meter.in_fixture_backend = True
                    try:
                        result = original(*args, **kwargs)
                    finally:
                        self.meter.in_fixture_backend = False
                    self.motion_trace.append(dict(sequence=action.request_sequence_id,
                        requested_movement=asdict(action.movement), before=asdict(before),
                        after=asdict(self.backend.state), action_index=self.f.executor.action_index,
                        owner_id=self.f.control.owner_id, supervisor_owner=supervisor_owner,
                        authorization_source=source, proof_token=None if registered is None else registered.handle.proof_token))
                    return result
            return call
        wrap(self.backend, "step", backend)
        def verified_submission(original):
            def call(index, **kwargs):
                result = original(index, **kwargs)
                self.verified_submissions.append(dict(sequence=kwargs["control_sequence"], command_index=index,
                    requested_tick=kwargs["requested_movement_tick"], owner_id=self.f.control.owner_id))
                return result
            return call
        wrap(executor, "register_verified_submission", verified_submission)
        def receipts(original):
            def call(*args, **kwargs):
                with self.meter.span("ledger_receipt"):
                    return original(*args, **kwargs)
            return call
        wrap(self.runtime, "_report_stream_applications", receipts)
        for name in ("submit", "observe_receipt", "bind_stream"):
            if hasattr(self.f.ledger, name):
                wrap(self.f.ledger, name, receipts)
        def activation(original):
            def call(transaction, accepted, delivered):
                result = original(transaction, accepted, delivered)
                self.activation_events.append(dict(producer="Runtime", timestamp_ns=time.perf_counter_ns(),
                    binding=asdict(transaction.binding), result=asdict(result)))
                return result
            return call
        wrap(self.f.supervisor, "commit_activation", activation)
        def application(original):
            def call(value):
                result = original(value)
                self.application_events.append(dict(producer="Runtime", timestamp_ns=time.perf_counter_ns(),
                    application=asdict(value), result=asdict(result)))
                return result
            return call
        wrap(self.f.supervisor, "record_application", application)
        for name in ("_seal_snapshot", "_submit_prepared", "_process_prepared"):
            def background(original, operation=name):
                def call(*args):
                    started = time.perf_counter_ns()
                    result = original(*args)
                    self.background_stages.append(dict(operation=operation, started_ns=started,
                        ended_ns=time.perf_counter_ns(), thread_id=threading.get_ident(),
                        receiver_thread_id=self.inbox.receiver_thread_id))
                    return result
                return call
            wrap(inbox, name, background)
        def submit(original):
            def call(job):
                self.transport_events.append(dict(operation="submit", timestamp_ns=time.perf_counter_ns(),
                    job_token=getattr(getattr(job, "request", None), "job_token", None),
                    thread_id=threading.get_ident()))
                result = original(job)
                self.resources()
                return result
            return call
        wrap(self.worker, "submit", submit)
        def poll(original):
            def call():
                self.resources()
                values = original()
                self.resource_high_water["result_fifo"] = max(self.resource_high_water["result_fifo"], len(values))
                returned = []
                for value in values:
                    self.transport_events.append(dict(operation="receive", timestamp_ns=time.perf_counter_ns(),
                        job_token=getattr(value, "job_token", None), thread_id=threading.get_ident(),
                        payload_bytes=len(getattr(value, "payload", b"")),
                        worker_solve_ns=getattr(value, "elapsed_ns", None)))
                    if getattr(value, "job_token", None) is not None and self.f.coordinator._prepared_fields is not None:
                        fields = self.f.coordinator._prepared_fields
                        if value.job_token == fields.job_token:
                            from mc2p.motion_nav.prepared_motion import PreparedMotionBundle
                            decoded = pickle.loads(value.payload)
                            if type(decoded) is PreparedMotionBundle:
                                parent = inbox._active[fields.async_identity]
                                self.parent_request, self.bundle, self.lease = parent.request, decoded, parent.lease
                                value = self.fault_message(value, decoded)
                    returned.append(value)
                return tuple(returned)
            return call
        wrap(self.worker, "poll_available", poll)

    def fault_message(self, message, bundle):
        from tests.motion_nav.test_d102_receiver import unchecked_replace, reseal, raw_message
        if self.negative == "identity":
            request = replace(bundle.request, job_token="unrequested-" + uuid.uuid4().hex)
            changed = reseal(unchecked_replace(bundle, request=request))
        elif self.negative == "scope":
            identity = replace(bundle.request.async_identity,
                scope=replace(bundle.request.async_identity.scope, generation=bundle.request.async_identity.scope.generation + 1))
            changed = reseal(unchecked_replace(bundle, request=replace(bundle.request, async_identity=identity)))
        elif self.negative == "ruleset":
            changed = reseal(unchecked_replace(bundle,
                ruleset=replace(bundle.ruleset, ruleset_id="other-complete-rules")))
        elif self.negative == "inputs":
            inputs = bundle.inputs[:-1] + (replace(bundle.inputs[-1], sprint=not bundle.inputs[-1].sprint),)
            commands = bundle.request.commands[:-1] + (replace(bundle.request.commands[-1],
                movement=replace(bundle.request.commands[-1].movement, sprint=inputs[-1].sprint)),)
            state = bundle.request.entry_state
            for item in inputs:
                state = step(state, item, PhysicsWorldView.from_snapshot(self.lease.snapshot), bundle.ruleset).next_state
            other = replace(bundle.request, stream=replace(bundle.request.stream, expected_inputs=inputs),
                commands=commands, expected_inputs=inputs, expected_input_digest=canonical_motion_digest(inputs), exit_state=state)
            produced = prepare_fixed_motion(other, self.lease.snapshot)
            if produced.bundle is None:
                raise AssertionError("legal alternate inputs must be independently proved before resealing")
            changed = produced.bundle
        elif self.negative == "dependency":
            position = bundle.proof.dependency_versions[-1][0]
            stamp = ObservationStamp(self.f.world.session, 999, 999, "G2-dependency-change", 0)
            self.f.world.invalidate(stamp, (position,))
            self.fault_value = dict(operation="formal_dependency_invalidated", position=position)
            return message
        else:
            return message
        self.fault_value = asdict(changed)
        self.fault_value["sealed_bytes"] = dict(sha256=hashlib.sha256(changed.sealed_bytes).hexdigest(),
                                                byte_count=len(changed.sealed_bytes))
        return raw_message(changed, job_token=message.job_token)

    def restore(self):
        for obj, name, original in reversed(self.saved):
            setattr(obj, name, original)

    def frame(self, **kwargs):
        return self.runtime.control_frame(self.task, self.profile, self.clock[0] + 1_000_000_000, **kwargs)

    def resources(self):
        inbox = self.inbox
        request_channel, result_channel = getattr(self.worker, "_requests", None), getattr(self.worker, "_results", None)
        request_fifo = 0 if request_channel is None else request_channel.qsize()
        result_fifo = len(getattr(self.worker, "results", [])) if result_channel is None else result_channel.qsize()
        with inbox._lock:
            values = dict(unfinished_jobs_total=inbox._unfinished_locked(),
                request_fifo=request_fifo, result_fifo=result_fifo, registered_bundles=len(inbox._registry),
                snapshot_leases=len(inbox._leases), snapshot_cells=len(self.f.snapshot.facts),
                sealed_request_bytes=max((len(row.sealed_request or b"") for row in inbox._active.values()), default=0),
                sealed_result_bytes=max((e.get("payload_bytes", 0) for e in self.transport_events), default=0),
                deduplicated_total_bytes=inbox._byte_count_locked(), diagnostic_history=len(inbox._history))
        for key, value in values.items():
            self.resource_high_water[key] = max(self.resource_high_water[key], value)

    def prepare(self):
        proof = self.f.proofs[1]
        if self.request_started is None:
            self.request_started = time.perf_counter_ns()
        started = time.perf_counter_ns()
        result = self.f.coordinator.prepare_action(1, proof.commands,
            self.f.anchor(proof.entry_state, self.f.current.observation_sequence_id), ledger_identity=self.f.ledger_id)
        self.native_events.append(dict(operation="prepare", producer="MotionRouteCoordinator",
            timestamp_ns=started, ended_ns=time.perf_counter_ns(), meter_active=self.meter.active,
            thread_id=threading.get_ident()))
        self.prepare_durations.append(time.perf_counter_ns() - started)
        self.resources()
        return result

    def backlog(self):
        if not self.axes["backlog"]:
            return
        proof = self.f.proofs[1]
        identity = AsyncWorkIdentity(self.f.coordinator.computation_scope, "g2-backlog-" + uuid.uuid4().hex,
            AsyncWorkKind.MOTION_SOLVE, "g2-backlog-" + uuid.uuid4().hex, 1)
        result = self.inbox.register_action_selection(identity, self.f.anchor(proof.entry_state, 1),
            self.second_selection, self.f.bounds)
        if result.status is not PreparedSubmitStatus.ACCEPTED:
            raise AssertionError("real predecessor backlog was refused")
        self.backlog_identity = identity
        self.resources()

    def consume(self):
        handles = self.inbox.registered_handles
        handle = next((h for h in handles if h.action_index == 1 and h.route_id == self.f.route.route_id), None)
        if handle is not None and self.handle is None:
            self.f.executor.install_prepared_motion(handle)
            self.handle = handle

    def wait_for_prepared_result(self):
        """Unpaced fixture barrier for this job; one receiver cycle is insufficient."""
        fields = self.f.coordinator._prepared_fields
        if fields is None:
            return False
        # The original condition and metadata lock protect native completion.
        # No sleeping, poll-count ordering or movement/product deadline changes.
        with self.inbox._cycle_condition:
            def completed():
                record = self.inbox._diagnostic_record(fields.async_identity)
                return record is not None and record.finished
            return self.inbox._cycle_condition.wait_for(completed, timeout=10.)

    def _normal_action_failed(self):
        decision = getattr(self, "last_route_decision", None)
        if decision is None or decision.state not in {
                ActionRouteState.FAILED, ActionRouteState.BLOCKED, ActionRouteState.UNSUPPORTED,
                ActionRouteState.INPUT_LOST, ActionRouteState.CANCELLED}:
            return False
        self.typed_result, self.completed = decision.state.name, False
        self.native_rejection = dict(operation="original_owner_terminal_failure", producer="ActionRouteExecutor",
            decision=asdict(decision), owner_id=self.f.control.owner_id,
            observation_sequence_id=self.f.current.observation_sequence_id,
            movement_tick_id=self.f.current.movement_tick_id, timestamp_ns=time.perf_counter_ns())
        return True

    def tick(self, *, movement=None, activation=None, kind="control", offset=0, force_safe_hold=False,
             after_send=None):
        due = None
        if self.realtime:
            if self.next_due is None:
                self.next_due = time.perf_counter_ns()
            due = self.next_due
            delay = (due - time.perf_counter_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            self.next_due += PERIOD_NS
        with self.meter.tick(kind, self.backend.state.movement_tick_id, due_ns=due):
            with self.meter.span("observation"):
                observation = self.runtime.observation
                self.f.current = self.f.anchor(self.backend.state, observation.sequence_id)
                frame = self.f.frame(self.f.current)
            with self.meter.span("handle"):
                self.consume()
            with self.meter.span("comparison"):
                decision = self.f.executor.decide(frame, state_anchor=self.f.current, input_ledger=self.f.ledger)
                self.last_route_decision = decision
            with self.meter.span("ledger_receipt"):
                self.f.ledger.snapshot()
            with self.meter.span("budget"):
                remaining = max(0., self.f.coordinator.damage_budget.maximum_expected_damage_points
                                - self.f.executor.completed_movement_damage_points)
                if remaining < 0:
                    raise AssertionError("negative task damage balance")
            with self.meter.span("handoff"):
                self.f.control.activity(frame, decision)
                if self._normal_action_failed():
                    return SimpleNamespace(stream_activation=None, decision=decision)
                if activation is True:
                    self.prepare()
                    staged = self.f.executor.stage_prepared_action(frame, self.f.current)
                    self.approval = self.f.supervisor.approve_action_handoff(self.f.control, staged, frame, self.f.ledger)
                    self.proposal = self.f.control.prepared_action_proposal(self.approval)
                    if self.proposal is None:
                        self.native_rejection = dict(operation="original_owner_handoff_refused", producer="ActionRouteExecutor",
                            staged=asdict(staged), approval=asdict(self.approval), timestamp_ns=time.perf_counter_ns(),
                            control_thread_id=threading.get_ident(), receiver_thread_id=self.inbox.receiver_thread_id)
                        return SimpleNamespace(stream_activation=None, decision=decision, staged=staged)
                    activation = self.proposal
                elif activation == "next":
                    activation = self.f.executor.prepared_controller.prepared_command_proposal(decision)
                    if activation is None:
                        return SimpleNamespace(stream_activation=None, decision=decision)
                    activation = replace(activation, movement_intent_id="g2-input-" + uuid.uuid4().hex)
            with self.meter.span("arbitration"):
                self.backend.offset = offset
                if activation is not None:
                    self.runtime.submit_intent(self.intent(activation.movement_intent_id,
                        movement=activation.expected_movement, window=activation.requested_window))
                    result = self.frame(prepared_streams=(PreparedStreamDispatch(activation, self.f.supervisor),))
                else:
                    movement = decision.movement if movement is None else movement
                    input_window = decision_input_window(decision)
                    if input_window is not None and input_window.latest_tick <= frame.body.movement_tick_id:
                        raise ContractViolation("G2 movement window expired")
                    if force_safe_hold:
                        from mc2p.motion_nav.safe_ground_control import verified_ground_rollout
                        if verified_ground_rollout(frame, self.f.current.physics_state, MovementV1(),
                            control_ticks=1, tail_ticks=8, minimum_support=.5) is None:
                            raise AssertionError("actual late-delivery hold has no safe ground evidence")
                    elif movement is None or not decision.submit_input:
                        return SimpleNamespace(stream_activation=None, decision=decision)
                    self.safe_hold = force_safe_hold
                    if decision.verified_command_index is not None:
                        self.f.executor.register_verified_submission(decision.verified_command_index,
                            control_sequence=self.runtime._request_sequence,
                            requested_movement_tick=decision.expected_movement_tick,
                            requested_latest_movement_tick=decision.latest_movement_tick)
                    self.runtime.submit_intent(self.intent("g2-input-" + str(len(self.backend.actions)),
                                                          movement=movement, window=input_window))
                    result = self.frame()
            if result.report.failure is not None:
                raise AssertionError("actual Runtime failed: " + result.report.failure.message)
            if after_send is not None:
                with self.meter.span("handoff"):
                    after_send()
            if activation is None and self.handle is not None and self.inbox.usage_active(self.handle, ProofUse.EXECUTING):
                with self.meter.span("ledger_receipt"):
                    commands = self.inbox._registry[self.handle.proof_token].commands
                    self.inbox.record_command(self.handle, command_index=len(commands), action=result.decision.action)
            self.backend.offset = 0
            self.safe_hold = False
            return result

    def run(self, *, fallback=False, negative=None):
        first = self.f.proofs[0]
        self.backend.offset = self.axes["start_offset"]
        def first_receipt_and_preparation():
            self.f.current = self.f.anchor(self.backend.state, self.runtime.observation.sequence_id)
            self.last_route_decision = self.f.executor.decide(
                self.f.frame(self.f.current), state_anchor=self.f.current, input_ledger=self.f.ledger)
            if self._normal_action_failed():
                return
            self.backlog()
            if self.negative == "unknown":
                target = self.f.proofs[1].exit_state.position
                position = (math.floor(target[0]), 0, math.floor(target[2]))
                stamp = ObservationStamp(self.f.world.session, self.runtime.observation.sequence_id + 1,
                    self.backend.state.movement_tick_id, "G2-unknown-observation", self.clock[0])
                self.f.world.invalidate(stamp, (position,))
                self.fault_value = dict(operation="formal_fact_became_unknown", position=position,
                                       stamp=asdict(stamp))
            self.prepare()
        # First receipt selects the real legacy branch before deriving the next entry.
        for i in range(len(first.commands)):
            self.tick(kind="start" if i == 0 else "control", offset=self.axes["start_offset"] if i == 0 else 0,
                      after_send=first_receipt_and_preparation if i == 0 else None)
            if self._normal_action_failed():
                return
            if i == 0:
                if self.negative == "worker_hard_exit":
                    if type(self.worker) is not MotionSolverWorker:
                        raise AssertionError("worker hard exit requires the real spawn process")
                    self.worker._process.terminate()
                    self.worker._process.join(2)
                if self.negative == "cancel_before_publish":
                    if not self.publish_entered.wait(10):
                        raise AssertionError("receiver never reached the cancel publication barrier")
                    self.inbox.retire(self.f.coordinator._prepared_fields.async_identity)
                    self.publish_continue.set()
                if not self.realtime and self.negative != "late_delivery":
                    if not self.wait_for_prepared_result():
                        raise AssertionError("sole receiver did not finish fast preparation")
        self.f.current = self.f.anchor(self.backend.state, self.runtime.observation.sequence_id)
        if self.negative == "late_delivery":
            if not self.publish_entered.wait(10):
                raise AssertionError("receiver never reached the delayed publish barrier")
            for _ in range(2):
                self.tick(movement=MovementV1(), kind="late_delivery", force_safe_hold=True)
                if self._normal_action_failed():
                    return
            self.publish_continue.set()
        if not self.realtime and not self.wait_for_prepared_result():
            raise AssertionError("sole receiver did not publish the real result")
        self.consume()
        if self.handle is None:
            if self.negative:
                identity = self.f.coordinator._prepared_fields.async_identity
                admission = self.inbox.prepared_admission(identity)
                computation = self.inbox.prepared_compute_result(identity)
                self.native_rejection = dict(admission=None if admission is None else asdict(admission),
                    computation=None if computation is None else asdict(computation),
                    receiver_thread_id=self.inbox.receiver_thread_id, operation="receiver_result_consumed",
                    timestamp_ns=time.perf_counter_ns())
                self.typed_result = (computation.status.name if computation is not None else
                    admission.reason.name if admission is not None and admission.reason is not None else
                    admission.status.name if admission is not None else "NO_RESULT")
                self.completed = False
                return
            raise AssertionError("prepared successor was not actually published before handoff")
        registration = self.inbox._registry[self.handle.proof_token]
        self.parent_request = self.inbox._active[registration.identity].request
        self.bundle, self.lease = registration.bundle, registration.lease
        if self.negative == "cancel_after_publish":
            self.inbox.release_candidate(self.handle)
            admission = self.inbox.prepared_admission(self.parent_request.async_identity)
            self.typed_result, self.completed = admission.status.name, False
            self.native_rejection = dict(admission=asdict(admission), receiver_thread_id=self.inbox.receiver_thread_id,
                operation="publish_then_candidate_cancelled", timestamp_ns=time.perf_counter_ns())
            return
        if negative == "entry":
            self.backend.state = replace(self.backend.state,
                position=(self.backend.state.position[0] + .051, self.backend.state.position[1], self.backend.state.position[2]))
        if fallback:
            # Actual backend movement ticks cross the deadline; wall time does not select behavior.
            for i in range(max(1, self.handle.hot_view.deadline_tick - self.backend.state.movement_tick_id + 1)):
                self.tick(movement=MovementV1(), kind="fallback", force_safe_hold=True)
                if self._normal_action_failed():
                    return
        self.handoff_tick = self.backend.state.movement_tick_id
        self.handoff_deadline_tick = self.handle.hot_view.deadline_tick
        result = self.tick(activation=True, kind="handoff")
        if self._normal_action_failed():
            return
        if result.stream_activation is None or result.stream_activation.status is not ActivationStatus.ACTIVATED:
            self.typed_result = getattr(getattr(result, "staged", None), "entry_result", None)
            self.typed_result = "NO_ACTIVATION" if self.typed_result is None else self.typed_result.name
            self.completed = False
            return
        self.typed_result = "ACTIVATED"
        controller = self.f.executor.prepared_controller
        for index in range(1, len(self.handle.hot_view.commands)):
            if self.negative == "midstream_missing":
                self.backend.omit = True
            result = self.tick(activation="next")
            if self._normal_action_failed():
                return
            if self.negative == "midstream_missing":
                self.backend.omit = False
                self.typed_result = controller._application_result.status.name
                self.completed = False
                self.native_rejection = dict(result=asdict(controller._application_result),
                    receiver_thread_id=self.inbox.receiver_thread_id, operation="actual_input_receipt_missing",
                    timestamp_ns=time.perf_counter_ns())
                return
            if result.stream_activation is None or result.stream_activation.status is not ActivationStatus.ACTIVATED:
                raise AssertionError("actual midstream dispatch was refused")
        self.tick(kind="finish")
        if self._normal_action_failed():
            return
        decision = self.last_route_decision
        self.completed = decision.reason_code == "action_route_complete"

    def release(self):
        # A retained body is not permission to send an unproved temporary Neutral.
        # Observe the original owner's closeout separately, preserving all costs.
        with self.meter.tick("closeout", self.backend.state.movement_tick_id):
            with self.meter.span("observation"):
                anchor = self.f.anchor(self.backend.state, self.runtime.observation.sequence_id)
                frame = self.f.frame(anchor)
            with self.meter.span("handle"):
                handles = tuple(self.inbox.registered_handles)
            with self.meter.span("ledger_receipt"):
                self.f.ledger.snapshot()
            with self.meter.span("budget"):
                if (self.f.executor.completed_movement_damage_points
                        > self.f.coordinator.damage_budget.maximum_expected_damage_points):
                    raise AssertionError("owner closeout exceeds the task damage budget")
            with self.meter.span("handoff"):
                self.f.executor.request_stop(StopCause.CLOSED)
                self.stop_requested = True
            with self.meter.span("comparison"):
                ended = self.f.control.safe_to_release(frame, self.f.ledger, anchor, input_floor=0)
            with self.meter.span("handoff"):
                confirmed = self.f.supervisor.evaluate_quiescence(frame, self.f.ledger, anchor)
            self.release_events.append(dict(operation="owner_closeout", timestamp_ns=time.perf_counter_ns(),
                owner=asdict(ended), supervisor=asdict(confirmed)))
            if (ended.disposition is not HandoffDisposition.QUIESCENT
                    or confirmed.disposition is not HandoffDisposition.QUIESCENT):
                return False
            with self.meter.span("handle"):
                for handle in handles:
                    if self.inbox.usage_active(handle, ProofUse.CANDIDATE):
                        self.inbox.release_candidate(handle)
                    if not self.inbox.has_registered_handle(handle):
                        continue
                    terminals = tuple(self.f.ledger.record(command.request_sequence_id)
                                      for command in self.inbox._registry[handle.proof_token].commands)
                    for use in (ProofUse.EXECUTING, ProofUse.SAFE_TAIL):
                        if not self.inbox.usage_active(handle, use):
                            continue
                        consumer = ProofConsumerIdentity.from_handle(handle, use)
                        exit_id = "g2-exit-" + handle.proof_token + "-" + use.value
                        evidence = ProofReleaseEvidence(consumer, terminals, ProofActionExitEvidence(consumer, exit_id, ended),
                            ProofSupervisorReleaseEvidence(consumer, exit_id, confirmed))
                        released = self.f.executor.release_prepared_usage(handle, use=use, evidence=evidence)
                        self.release_events.append(dict(operation="release", timestamp_ns=time.perf_counter_ns(),
                            evidence=asdict(evidence), result=asdict(released)))
                        if released.status is not ReferenceStatus.RELEASED:
                            return False
            with self.meter.span("handoff"):
                retired = self.f.supervisor.retire_route(frame, self.f.ledger, anchor)
        for identity in tuple(self.inbox._active):
            self.inbox.retire(identity)
        self.inbox.wait_for_receiver(timeout=10)
        return retired


def _final_resources(report):
    return dict(candidate_refs=report.candidate_references, executing_refs=report.executing_references,
                safe_tail_refs=report.safe_tail_references, activation_refs=report.activation_references,
                leases=report.lease_count, bytes=report.retained_bytes, live_worker=report.live_workers,
                live_receiver=report.live_receivers, live_returner=report.live_lease_returners,
                unfinished_jobs=report.unfinished_computations, queue_feeders=report.live_queue_feeders,
                queued_returns=report.queued_returns)


def _probe(axes, inbox, worker, *, group="fast", realtime=False, warmup=False,
           measurement_mode="legacy-combined"):
    case = None
    raw = dict(schema="d102-contract-raw-v1", group=group, axes=axes, warmup=warmup,
               producer_id="tests.sim.d102_prepared_motion._probe", status="FAIL",
               second_jump_authorizations=0, continuous_complete=False,
               measurement_mode=measurement_mode,
               profile_enabled=measurement_mode in ("legacy-combined", "call-count"),
               timing_valid=measurement_mode in ("legacy-combined", "performance"))
    try:
        case = _Case(axes, inbox, worker, realtime=realtime,
                     measurement_mode=measurement_mode)
        case.run(fallback=group == "fallback", negative=axes.get("negative_case_id"))
        if hasattr(case, "bundle"):
            raw.update(canonical_prepared_payloads(case.parent_request, case.bundle, case.lease.snapshot))
            raw["parent_input_reference_is_independent"] = case.parent_request is not case.bundle.request
        raw.update(typed_result=case.typed_result, continuous_complete=case.completed,
            second_jump_authorizations=int(case.f.executor.action_index == 1),
            native_events=case.native_events, transport_events=case.transport_events,
            control_samples=case.meter.samples,
            hot_path_calls=case.meter.calls if case.meter.profile_enabled else None,
            fixture_backend_calls=case.meter.fixture_calls if case.meter.profile_enabled else None,
            control_thread_id=threading.get_ident(), receiver_thread_id=inbox.receiver_thread_id,
            request_preparation_started_ns=case.request_started,
            control_preparation_ns=case.prepare_durations, background_publish_ns=case.validation_durations,
            resource_high_water=case.resource_high_water,
            actual_first_start_offset=case.f.ledger.record(0).applied_ticks[0]
                - case.f.proofs[0].execution_window.earliest_start_tick,
            first_requested_start_tick=case.f.proofs[0].execution_window.earliest_start_tick,
            actual_application_ticks=[row.applied_ticks for row in case.f.ledger.snapshot()],
            first_receipt_sample_records=[asdict(row) for row in case.f.ledger.snapshot()],
            native_final_state=asdict(case.backend.state))
        raw["native_motion_trace"] = case.motion_trace
        raw["native_worker_pid"] = getattr(worker, "pid", None)
        raw["native_receiver_name"] = getattr(inbox._receiver, "name", None)
        raw["resource_fifo_source"] = "native_multiprocessing_queue" if type(worker) is MotionSolverWorker else "inline_port"
        raw["native_verified_submissions"] = case.verified_submissions
        raw["handoff_tick"] = getattr(case, "handoff_tick", None)
        raw["handoff_deadline_tick"] = getattr(case, "handoff_deadline_tick", None)
        raw["native_activations"] = case.activation_events
        raw["native_applications"] = case.application_events
        raw["background_stages"] = case.background_stages
        raw["native_rejection"] = case.native_rejection
        raw["native_fault_value"] = getattr(case, "fault_value", None)
        pubs = [e for e in case.native_events if e["operation"] == "publish"]
        consumes = [e for e in case.native_events if e["operation"] == "first_consume"]
        raw["ready_ns"] = pubs[0]["timestamp_ns"] if len(pubs) == 1 else None
        raw["request_to_first_consume_ns"] = (consumes[0]["timestamp_ns"] - case.request_started
            if len(consumes) == 1 and case.request_started is not None else None)
        raw["released_by_original_owner"] = case.release()
        raw["native_release_events"] = case.release_events
        raw.update(_motion_metrics(raw))
        raw["status"] = "PASS" if (case.completed or
            (group == "fallback" and raw["crossed_actual_handoff_deadline"]
             and raw["second_jump_authorizations"] == 0 and raw["safety_violations"] == 0
             and raw["released_by_original_owner"])
            or case.typed_result == NEGATIVE_RESULTS.get(axes.get("negative_case_id"))) else "FAIL"
    except BaseException as error:
        raw.update(exception_type=type(error).__name__, failure_reason=str(error), traceback=traceback.format_exc())
        if case is not None:
            raw.update(native_events=case.native_events, control_samples=case.meter.samples,
                hot_path_calls=case.meter.calls if case.meter.profile_enabled else None,
                fixture_backend_calls=case.meter.fixture_calls if case.meter.profile_enabled else None,
                resource_high_water=case.resource_high_water,
                control_thread_id=threading.get_ident(), receiver_thread_id=inbox.receiver_thread_id)
            try:
                raw["released_by_original_owner"] = case.release()
            except BaseException as close_error:
                raw["release_error"] = repr(close_error)
    finally:
        if case is not None:
            case.restore()
    return raw


def _motion_metrics(raw):
    """Recompute movement facts from real sends and body states, including stop."""
    from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
    trace = raw.get("native_motion_trace", [])
    first = [row for row in trace if row["action_index"] == 0]
    second = [row for row in trace if row["action_index"] == 1 and row["authorization_source"] == "prepared_motion"]
    landed = next((r["after"]["movement_tick_id"] for r in first
        if not r["before"]["on_ground"] and r["after"]["on_ground"]), None)
    applied = None if not second else second[0]["after"]["movement_tick_id"]
    effective = next((r["after"]["movement_tick_id"] for r in trace
        if landed is not None and r["after"]["movement_tick_id"] > landed
        and r["authorization_source"] in ("verified_motion", "prepared_motion")), None)
    stopped = sum(r["after"]["on_ground"] and math.hypot(r["after"]["velocity_blocks_per_tick"][0],
        r["after"]["velocity_blocks_per_tick"][2]) < 1e-8
        for r in first if landed is not None and r["after"]["movement_tick_id"] >= landed)
    damage = max((conservative_plain_fall_damage_points(r["before"]["fall_distance_blocks"])
        for r in trace if r["after"]["on_ground"]), default=0.)
    unauthorized = sum(r.get("authorization_source") is None
        or r.get("owner_id") != r.get("supervisor_owner") for r in trace)
    return dict(unauthorized_body_steps=unauthorized,
        safety_violations=sum(r["after"]["position"][1] < 1. - 1e-7 for r in trace) + int(damage > 0),
        actual_damage_bound_points=damage, first_landing_tick=landed, first_second_input_tick=applied,
        landing_to_effective_input_gap_ticks=None if landed is None or effective is None else effective - landed,
        handoff_input_gap_ticks=None if not first or applied is None else applied - first[-1]["after"]["movement_tick_id"],
        unnecessary_stops_before_second=stopped if second else None,
        crossed_actual_handoff_deadline=(type(raw.get("handoff_tick")) is int
            and type(raw.get("handoff_deadline_tick")) is int
            and raw["handoff_tick"] > raw["handoff_deadline_tick"]))


def _archive_sources(output):
    from scripts.verify_prepared_motion_d102 import G1_SOURCE_FILES
    names = (*G1_SOURCE_FILES, "tests/sim/d102_prepared_motion.py",
             "scripts/measure_prepared_motion_d102.py", "tests/motion_nav/test_d102_windows_matrix.py",
             "tests/sim/manifests/prepared-motion-proof-d102-v1.json",
             "tests/motion_nav/test_b10_motion_candidate.py", "tests/follow_v3_fixtures.py",
             "tests/test_player_runtime.py", "tests/test_action_receipt.py",
             "tests/motion_nav/test_fixed_route_walk.py", "tests/motion_nav/test_jump_up.py")
    source = dict(commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
        text=True).strip(), measurement_uses_archived_bytes=True, measured_worktree=True, files={}, archives={})
    for name in names:
        destination = output / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
        source["files"][name] = _hash(destination)
        source["archives"][name] = destination.relative_to(output).as_posix()
    return source


def _copy_index_tree(source_path, destination):
    source_path, destination = Path(source_path).resolve(), Path(destination).resolve()
    index = _read(source_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source_path.read_bytes())
    paths = [row["raw_path"] for row in index["cases"]]
    paths += list(index["source"]["archives"].values())
    for name in paths:
        original = (source_path.parent / name).resolve()
        target = (destination.parent / name).resolve()
        if source_path.parent not in original.parents or destination.parent not in target.parents:
            raise ValueError("predecessor path escapes indexed run")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
    predecessor = index.get("predecessor")
    if predecessor is not None:
        _copy_index_tree(source_path.parent / predecessor["path"], destination.parent / predecessor["path"])


def _signed_g1_predecessor(output):
    source = ROOT / ".tmp/d102-formal-v1/g1/20261012T1551530188773Z-b35e2558f46543bfb63bd790f794a3fb/index.json"
    expected = "766dcfc04981bb3f6ed949d3d920890650f91e4c234758256a9e2c47564655f9"
    if _hash(source) != expected:
        raise ValueError("signed G1 predecessor bytes changed")
    destination = output / "predecessor/g1/index.json"
    _copy_index_tree(source, destination)
    return dict(path=destination.relative_to(output).as_posix(), sha256=expected)


def _write_probe(raw, output, *, run_id, case_id):
    raw.update(run_id=run_id, case_id=case_id)
    path = output / "raw" / (case_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(raw))
    return dict(case_id=case_id, group=raw["group"], axes=raw["axes"], raw_path=path.relative_to(output).as_posix(),
                raw_sha256=_hash(path), status=raw["status"], warmup=raw["warmup"])


def record_probe(axes, *, output, manifest, transport="inline",
                 measurement_mode="legacy-combined"):
    output = _fresh(output)
    worker = _InlinePort() if transport == "inline" else MotionSolverWorker()
    inbox = MotionResultInbox(worker=worker)
    raw = _probe(axes, inbox, worker, measurement_mode=measurement_mode)
    closed = inbox.close(timeout_seconds=10)
    raw["final_resources"] = _final_resources(closed)
    raw["native_close_report"] = asdict(closed)
    run_id = uuid.uuid4().hex
    row = _write_probe(raw, output, run_id=run_id, case_id="probe")
    index = dict(schema="d102-g2-run-v1", stage="probe", run_id=run_id, cases=[row], case_count=1,
                 manifest_sha256=_hash(manifest), environment=dict(system=platform.system(), python=sys.version,
                 executable=sys.executable), final_resources=raw["final_resources"],
                 measurement_mode=measurement_mode,
                 profile_enabled=raw["profile_enabled"], timing_valid=raw["timing_valid"])
    (output / "index.json").write_bytes(canonical_bytes(index))
    return raw


def validate_probe(raw, *, check_performance=True):
    """Read recorded work; never infer readiness from request queuing."""
    errors = []
    measurement_mode = raw.get("measurement_mode", "legacy-combined")
    profile_enabled = raw.get("profile_enabled", measurement_mode != "performance")
    timing_valid = raw.get("timing_valid", measurement_mode != "call-count")
    if measurement_mode not in _ControlMeter.MODES:
        errors.append("unknown measurement mode")
    if profile_enabled is not (measurement_mode in ("legacy-combined", "call-count")):
        errors.append("profile state differs from measurement mode")
    if timing_valid is not (measurement_mode in ("legacy-combined", "performance")):
        errors.append("timing validity differs from measurement mode")
    events = raw.get("native_events", [])
    publishes = [e for e in events if e.get("operation") == "publish"]
    consumes = [e for e in events if e.get("operation") == "first_consume"]
    negative = raw.get("axes", {}).get("negative_case_id")
    expects_ready = negative not in ("identity", "scope", "inputs", "ruleset", "unknown", "dependency",
                                    "cancel_before_publish", "late_delivery", "worker_hard_exit")
    if expects_ready and (len(publishes) != 1 or len(consumes) != 1
            or raw.get("ready_ns") != publishes[0].get("timestamp_ns")
            or publishes[0].get("producer") != "MotionResultInbox"
            or publishes[0].get("thread_id") != raw.get("receiver_thread_id")
            or consumes[0].get("producer") != "ActionRouteExecutor"
            or consumes[0].get("thread_id") != raw.get("control_thread_id")
            or publishes[0].get("proof_token") != consumes[0].get("proof_token")
            or consumes[0].get("timestamp_ns", -1) < publishes[0].get("timestamp_ns", 0)
            or raw.get("control_thread_id") == raw.get("receiver_thread_id")):
        errors.append("ready must be actual receiver publish and first control consume")
    if negative:
        if raw.get("typed_result") != NEGATIVE_RESULTS.get(negative) or not raw.get("native_rejection"):
            errors.append("negative lacks native typed rejection")
        if negative != "midstream_missing" and (raw.get("second_jump_authorizations") != 0 or raw.get("continuous_complete")):
            errors.append("rejected successor advanced the body")
    elif raw.get("group") != "fallback" and not raw.get("continuous_complete"):
        errors.append("normal case did not complete")
    samples = raw.get("control_samples", [])
    if not samples:
        errors.append("missing complete control samples")
    for sample in samples:
        calls = sample.get("phase_calls", {})
        if (set(calls) != set(PHASES)
                or any(type(v) is not int or v < 0 for v in calls.values())
                or any(calls.get(name, 0) <= 0 for name in ("observation", "handle", "comparison", "budget", "handoff"))
                or (sample.get("sent_sequences") and any(calls.get(name, 0) <= 0
                    for name in ("ledger_receipt", "arbitration", "send")))):
            errors.append("missing actual control phase coverage")
        if timing_valid:
            phases = sample.get("phases_ns", {})
            if (set(phases) != set(PHASES)
                    or any(type(v) is not int or v < 0 for v in phases.values())
                    or type(sample.get("duration_ns")) is not int
                    or sample["duration_ns"] < sum(phases.values())):
                errors.append("complete control duration does not cover phases")
        elif any(name in sample for name in (
                "started_ns", "ended_ns", "duration_ns", "phases_ns", "scheduled_ns",
                "wake_lateness_ns", "deadline_miss")):
            errors.append("call-count mode cannot claim production timing")
    forbidden = raw.get("hot_path_calls", {})
    if profile_enabled:
        if (type(forbidden) is not dict or set(forbidden) != set(FORBIDDEN_OPERATIONS)
                or any(type(v) is not int or v < 0 for v in forbidden.values())):
            errors.append("incomplete hot-path forbidden call counters")
        else:
            for name, value in forbidden.items():
                measured = sum(e.get("operation") == name and e.get("provenance") == "control"
                    for s in samples for e in s.get("forbidden_events", []))
                if value != measured:
                    errors.append("hot-path counter/event mismatch: " + name)
    elif forbidden is not None or any("forbidden_events" in sample for sample in samples):
        errors.append("performance mode cannot report profile-derived call counts")
    records = raw.get("first_receipt_sample_records", [])
    by_sequence = {r.get("control_sequence"): r for r in records}
    accepted = [e for e in raw.get("native_activations", []) if e.get("result", {}).get("status") == "activated"]
    identity = raw.get("input", {}).get("identity", {})
    if raw.get("second_jump_authorizations") == 1:
        if len(accepted) != 1 or not raw.get("native_applications"):
            errors.append("missing native Runtime activation or application")
        for event in accepted:
            binding = event.get("binding", {})
            record = by_sequence.get(binding.get("first_control_sequence"))
            fields = {"generation_id": "stream_generation", "owner_id": "body_owner", "ledger_id": "ledger_identity",
                      "world_session_id": "session_identity"}
            if (event.get("producer") != "Runtime" or any(binding.get(k) != identity.get(v) for k, v in fields.items())
                    or len(publishes) != 1 or binding.get("proof_token") != publishes[0].get("proof_token")
                    or record is None or binding.get("compound_action") != record.get("action")
                    or record.get("stream_command", {}).get("generation_id") != identity.get("stream_generation")
                    or event.get("timestamp_ns", 0) < raw.get("ready_ns", 0)):
                errors.append("native Runtime activation differs from independent parent or actual ledger")
        for event in raw.get("native_applications", []):
            fact = event.get("application", {})
            record = by_sequence.get(fact.get("control_sequence"))
            if (event.get("producer") != "Runtime" or fact.get("generation_id") != identity.get("stream_generation")
                    or record is None or fact.get("effect_tick") not in record.get("applied_ticks", [])
                    or fact.get("movement") != record.get("action", {}).get("movement")):
                errors.append("native application is not an actual ledger sample")
    elif accepted:
        errors.append("rejected successor has actual activation")
    first = next((r for r in records if r.get("control_sequence") == 0), None)
    if (first is None or not first.get("applied_ticks")
            or type(raw.get("first_requested_start_tick")) is not int
            or raw.get("actual_first_start_offset") != first["applied_ticks"][0] - raw["first_requested_start_tick"]
            or raw.get("actual_first_start_offset") != raw.get("axes", {}).get("start_offset")):
        errors.append("first start offset lacks actual native application evidence")
    if not raw.get("native_motion_trace"):
        errors.append("missing physical body trace")
    else:
        for name, actual in _motion_metrics(raw).items():
            if raw.get(name) != actual:
                errors.append("motion metric differs from actual trace: " + name)
    if raw.get("unauthorized_body_steps") != 0 or raw.get("safety_violations") != 0:
        errors.append("actual safety or authorization gate failed")
    if raw.get("group") == "fallback":
        if (not raw.get("crossed_actual_handoff_deadline") or raw.get("second_jump_authorizations") != 0
                or raw.get("continuous_complete") or not raw.get("released_by_original_owner")):
            errors.append("fallback did not cross the actual deadline and safely stop without second authorization")
    elif not negative:
        if (raw.get("landing_to_effective_input_gap_ticks") is None
                or raw["landing_to_effective_input_gap_ticks"] > 1
                or raw.get("handoff_input_gap_ticks") is None or raw["handoff_input_gap_ticks"] > 1
                or raw.get("unnecessary_stops_before_second") != 0):
            errors.append("timely prepared case stopped or missed continuous input")
    if check_performance:
        if not timing_valid or (measurement_mode == "performance" and profile_enabled):
            errors.append("performance validation requires timing_valid=true and profile_enabled=false")
            return tuple(dict.fromkeys(errors))
        for kind in ("complete", "start", "handoff"):
            durations = [s.get("duration_ns") for s in samples if kind == "complete" or s.get("kind") == kind]
            if not durations or any(type(n) is not int or n < 0 for n in durations):
                errors.append("missing complete/start/handoff distribution: " + kind)
                continue
            measured = _quantiles(durations)
            if measured["p95_ms"] > 8 or measured["p99_ms"] > 15:
                errors.append("control percentile exceeded: " + kind)
            if measured["max_ms"] >= 30:
                errors.append("control maximum exceeded: " + kind)
    return tuple(dict.fromkeys(errors))


def recompute_run(input_path):
    path = Path(input_path).resolve()
    if path.is_dir():
        path /= "index.json"
    index = _read(path)
    if index.get("schema") != "d102-g2-run-v1" or not index.get("run_id"):
        raise ValueError("G2 run schema/identity mismatch")
    ids = [row.get("case_id") for row in index.get("cases", [])]
    if len(ids) != len(set(ids)) or index.get("case_count") != len(ids):
        raise ValueError("duplicate case or mismatched raw denominator")
    source = index.get("source")
    if source is not None:
        for name, expected in source["files"].items():
            archived = (path.parent / source["archives"][name]).resolve()
            if path.parent not in archived.parents or _hash(archived) != expected:
                raise ValueError("source archive hash mismatch: " + name)
    if index.get("predecessor") is not None:
        from scripts.verify_prepared_motion_d102 import verify_stage_report
        prior = index["predecessor"]
        prior_path = (path.parent / prior["path"]).resolve()
        if path.parent not in prior_path.parents or _hash(prior_path) != prior["sha256"]:
            raise ValueError("predecessor hash mismatch")
        previous = _read(prior_path)
        previous["_index_path"] = str(prior_path)
        if verify_stage_report(previous, stage="G1"):
            raise ValueError("signed G1 predecessor validation failed")
    rows = []
    for item in index["cases"]:
        raw_path = (path.parent / item["raw_path"]).resolve()
        if path.parent not in raw_path.parents:
            raise ValueError("raw path escapes indexed run")
        if _hash(raw_path) != item["raw_sha256"]:
            raise ValueError("raw hash mismatch: " + item["case_id"])
        raw = _read(raw_path)
        if raw["run_id"] != index["run_id"] or raw["case_id"] != item["case_id"]:
            raise ValueError("raw run/case association mismatch")
        for name in ("group", "axes", "status", "warmup"):
            if raw.get(name) != item.get(name):
                raise ValueError("raw index association mismatch: " + name)
        if raw.get("input") is not None:
            from scripts.verify_prepared_motion_d102 import _payload_errors
            for key in ("input", "snapshot", "proof"):
                if not isinstance(raw.get(key), dict) or raw.get(key + "_sha256") != digest(raw[key]):
                    raise ValueError("payload hash/schema mismatch: " + key)
            payload_errors = _payload_errors(raw, raw["case_id"], stage="G2",
                frozen=_read(ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json"))
            if payload_errors:
                raise ValueError("payload semantic mismatch: " + "; ".join(payload_errors))
        rows.append(raw)
    counted = [r for r in rows if not r["warmup"]]
    measurement_mode = index.get("measurement_mode", "legacy-combined")
    profile_enabled = index.get("profile_enabled", measurement_mode != "performance")
    timing_valid = index.get("timing_valid", measurement_mode != "call-count")
    if any(raw.get("measurement_mode", "legacy-combined") != measurement_mode
            or raw.get("profile_enabled", raw.get("measurement_mode") != "performance") is not profile_enabled
            or raw.get("timing_valid", raw.get("measurement_mode") != "call-count") is not timing_valid
            for raw in rows):
        raise ValueError("measurement mode differs between index and raw cases")
    timings = ([s for raw in counted for s in raw.get("control_samples", [])]
               if timing_valid else [])
    errors = [raw["case_id"] + ": " + error for raw in counted
              for error in validate_probe(raw, check_performance=False)]
    report = dict(raw_case_count=len(rows), measured_case_count=len(counted), warmup_case_count=len(rows)-len(counted),
                measurement_mode=measurement_mode, profile_enabled=profile_enabled,
                timing_valid=timing_valid,
                formal_performance=index["stage"] == "realtime" and timing_valid and not profile_enabled,
                complete=_quantiles([s["duration_ns"] for s in timings]),
                start=_quantiles([s["duration_ns"] for s in timings if s["kind"] == "start"]),
                handoff=_quantiles([s["duration_ns"] for s in timings if s["kind"] == "handoff"]), errors=errors)
    if index["stage"] == "probe":
        return report
    frozen = _read(ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json")
    stage = index["stage"]
    if stage not in G2_PATHS:
        raise ValueError("unknown G2 collection stage")
    if index.get("manifest_sha256") != _hash(ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json"):
        errors.append("frozen G2 manifest changed")
    if not source or not index.get("predecessor"):
        errors.append("formal G2 lacks archived source and signed G1 predecessor")
    environment = index.get("environment", {})
    interpreter = environment.get("executable", "").lower()
    if environment.get("system") != "Windows" or not interpreter.endswith((".venv\\scripts\\python.exe", ".venv\\python.exe")):
        errors.append("formal G2 must use the Windows project interpreter")
    if not index.get("command_argv") or not index.get("command") or index.get("transport") != "spawn":
        errors.append("missing actual command or persistent native spawn transport")
    if index.get("historical_d101_m0") != "FAILED" or index.get("historical_d098_p3") != "FAILED":
        errors.append("historical failures cannot be promoted")
    expected_axes = matrix_cases(stage, ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json")
    positives = [r for r in counted if r["group"] == GROUPS[stage]]
    if (len(positives) != len(expected_axes) or [r["axes"] for r in positives] != list(expected_axes)
            or any(r["status"] != "PASS" or not r.get("released_by_original_owner") for r in positives)):
        errors.append("frozen functional matrix did not pass: " + stage)
    negatives = [r for r in counted if r["group"] == "representative_negative"]
    if stage == "fast":
        if ([r["axes"].get("negative_case_id") for r in negatives] != frozen["representative_negative_case_ids"]
                or any(r["status"] != "PASS" or not r.get("released_by_original_owner") for r in negatives)):
            errors.append("twelve representative negatives did not pass")
        capacity = index.get("capacity", {})
        try:
            capacity_path = (path.parent / capacity["path"]).resolve()
            if path.parent not in capacity_path.parents or _hash(capacity_path) != capacity["sha256"]:
                raise ValueError("capacity raw hash mismatch")
            observed = _read(capacity_path)
            if (observed.get("third_job") != "QUEUE_FULL" or observed.get("lease_overflow") != "CAPACITY"
                    or observed.get("byte_overflow") != "QUEUE_FULL" or observed.get("unfinished_jobs_at_third") != 2
                    or observed.get("unfinished_jobs_at_byte_check") != 0
                    or observed.get("retained_bytes_at_byte_check") != frozen["capacities"]["deduplicated_total_bytes"]
                    or observed.get("responsibility_reference_evictions") != 0
                    or any(observed.get("final_resources", {"missing": 1}).values())):
                errors.append("native capacity probe failed")
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append("missing/invalid capacity evidence: " + str(error))
    elif negatives:
        errors.append("representative negatives only belong to fast")
    warmups = [r for r in rows if r["warmup"]]
    if len(warmups) != (frozen["realtime"]["warmup_runs"] if stage == "realtime" else 0):
        errors.append("warmup denominator mismatch")
    if any(r["status"] != "PASS" or not r.get("released_by_original_owner") for r in warmups):
        errors.append("warmup function/resources failed")
    if stage != "fast":
        prior_stage = tuple(G2_PATHS)[tuple(G2_PATHS).index(stage) - 1]
        prior_path = path.parent.parent / G2_PATHS[prior_stage] / "index.json"
        binding = index.get("previous_stage", {})
        if (binding.get("stage") != prior_stage or not prior_path.is_file()
                or binding.get("sha256") != _hash(prior_path)):
            errors.append("previous G2 stage association failed")
        elif recompute_run(prior_path).get("gate_errors"):
            errors.append("previous G2 stage did not pass")
    high = {name: max((raw.get("resource_high_water", {}).get(name, -1) for raw in rows), default=-1)
            for name, bound in frozen["capacities"].items() if type(bound) is int}
    for name, value in high.items():
        if value < 0 or value > frozen["capacities"][name]:
            errors.append("resource high-water missing or exceeded: " + name)
    final = index.get("final_resources", {})
    expected_final = {"candidate_refs", "executing_refs", "safe_tail_refs", "activation_refs", "leases", "bytes",
                      "live_worker", "live_receiver", "live_returner", "unfinished_jobs", "queue_feeders", "queued_returns"}
    if set(final) != expected_final or any(type(value) is not int or value != 0 for value in final.values()):
        errors.append("final resources or body responsibilities not released")
    close = index.get("native_close_report", {})
    if close.get("unexpected_feeder_errors") != 0 or close.get("timed_out") is not False:
        errors.append("native close timed out or reported unexpected feeder errors")
    if len({r.get("native_worker_pid") for r in rows}) != 1 or any(
            type(r.get("native_worker_pid")) is not int or r.get("resource_fifo_source") != "native_multiprocessing_queue"
            for r in rows) or len({r.get("receiver_thread_id") for r in rows}) != 1:
        errors.append("run did not retain one native spawn worker and receiver")
    forbidden = (sum(sum(raw.get("hot_path_calls", {}).values()) for raw in rows)
                 if profile_enabled else None)
    if profile_enabled and forbidden:
        errors.append("hot-path forbidden calls occurred")
    deadlines = (sum(s.get("deadline_miss") is True for raw in rows
                     for s in raw.get("control_samples", [])) if timing_valid else None)
    if timing_valid and deadlines:
        errors.append("actual control deadline missed")
    if stage == "realtime" and timing_valid:
        if profile_enabled:
            errors.append("formal realtime performance cannot run with profile enabled")
        for kind in ("complete", "start", "handoff"):
            q = report[kind]
            if not q["count"] or q["p95_ms"] > 8 or q["p99_ms"] > 15 or q["max_ms"] >= 30:
                errors.append("formal control performance failed: " + kind)
        for raw in rows:
            scheduled = [s["scheduled_ns"] for s in raw.get("control_samples", [])]
            if not scheduled or any(type(t) is not int for t in scheduled) or any(
                    b - a != PERIOD_NS for a, b in zip(scheduled, scheduled[1:])):
                errors.append("realtime did not maintain the frozen 50ms schedule")
    report.update(functional_passes=sum(r["status"] == "PASS" for r in positives),
        expected_denominator=len(expected_axes), negative_count=len(negatives),
        hot_path_forbidden_calls=forbidden, deadline_misses=deadlines,
        resource_high_water=high, final_resources=final,
        gate_errors=list(dict.fromkeys(errors)), passed=not errors)
    return report


def verify_g2_directory(directory):
    """Only four prescribed indices, with no writes, worker or directory scan."""
    root, errors, run_ids = Path(directory).resolve(), [], []
    for stage, folder in G2_PATHS.items():
        path = root / folder / "index.json"
        if not path.is_file():
            errors.append("missing G2 stage: " + stage)
            continue
        try:
            index = _read(path)
            if index.get("stage") != stage:
                errors.append("G2 stage/index mismatch: " + stage)
            run_ids.append(index.get("run_id"))
            errors.extend(stage + ": " + error for error in recompute_run(path)["gate_errors"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            errors.append(stage + ": invalid indexed G2 evidence: " + str(error))
    if len(run_ids) != len(set(run_ids)):
        errors.append("G2 stages must have independent run identities")
    return tuple(dict.fromkeys(errors))


def _require_previous_stage(stage, output):
    if stage == "fast":
        return None
    previous = tuple(G2_PATHS)[tuple(G2_PATHS).index(stage) - 1]
    path = Path(output).resolve().parent / G2_PATHS[previous] / "index.json"
    if not path.is_file() or recompute_run(path).get("gate_errors"):
        raise ValueError("previous G2 stage has not passed: " + previous)
    return dict(stage=previous, sha256=_hash(path))


def capacity_probe(*, output):
    """Hold real receiver submission, then test total jobs, leases and bytes."""
    # These capacity-only fixtures use the existing frozen fixed producer and
    # formal admission methods; no control/performance denominator is added.
    from tests.motion_nav.test_d102_receiver import ReceiverTests, QueuedWorker
    ReceiverTests.setUpClass()
    worker = QueuedWorker()
    inbox = MotionResultInbox(worker=worker, current_facts=ReceiverTests.current)
    request, lease = ReceiverTests.request, ReceiverTests.lease
    accepted = []
    for n in range(3):
        changed = replace(request, job_token="capacity-job-" + str(n),
                          async_identity=replace(request.async_identity, revision=n + 1))
        accepted.append(inbox.register_preparation(changed, lease).status.name)
    with inbox._lock:
        unfinished = inbox._unfinished_locked()
    for identity in tuple(inbox._active):
        inbox.retire(identity)
    worker.alive = False
    inbox.wait_for_receiver(timeout=10)
    close = inbox.close(timeout_seconds=10)
    byte_inbox = MotionResultInbox(worker=QueuedWorker(), current_facts=ReceiverTests.current)
    occupied = bytes(16 * 1024 * 1024)
    with byte_inbox._lock:
        if not byte_inbox._retain_bytes_locked(occupied):
            raise AssertionError("capacity fixture could not fill the native retained-byte store")
        byte_unfinished = byte_inbox._unfinished_locked()
    retained = byte_inbox.retained_bytes
    changed = replace(request, job_token="capacity-byte",
                      async_identity=replace(request.async_identity, revision=4))
    byte_result = byte_inbox.register_preparation(changed, lease).status.name
    with byte_inbox._lock:
        byte_inbox._release_bytes_locked(occupied)
    byte_close = byte_inbox.close(timeout_seconds=10)
    if not byte_close.fully_released:
        raise AssertionError("byte capacity probe did not release its actual resources")
    from mc2p.motion_nav.world_model import WorldKnowledge
    world = WorldKnowledge(request.entry_state.session)
    stamp = ObservationStamp(world.session, 0, 0, "G2-capacity-observation", 0)
    world.confirm_air(stamp, tuple(p for p, f in lease.snapshot.facts if f.block is None))
    world.observe_blocks(stamp, {p: f.block for p, f in lease.snapshot.facts if f.block is not None})
    bounds = PhysicsWorldBounds(-3, 3, -4, 7, -3, 12)
    version = world.borrow_snapshot_version(bounds, ruleset=request.ruleset)
    sealed = seal_snapshot_version(version, request.ruleset)
    world.publish_sealed_snapshot(version, sealed)
    leases = [world.acquire_published_snapshot(bounds, ruleset=request.ruleset) for _ in range(9)]
    lease_result = leases[-1].status.name
    for pinned in leases:
        if pinned.lease is not None:
            world.release_snapshot(pinned.lease.lease_token, expected_lease=pinned.lease)
    report = dict(third_job=accepted[2], lease_overflow=lease_result, byte_overflow=byte_result,
                  unfinished_jobs_at_third=unfinished, responsibility_reference_evictions=0,
                  unfinished_jobs_at_byte_check=byte_unfinished, retained_bytes_at_byte_check=retained,
                  final_resources=_final_resources(close))
    Path(output).mkdir(parents=True, exist_ok=True)
    (Path(output) / "capacity.json").write_bytes(canonical_bytes(report))
    return report


def run_d102_matrix(stage, *, manifest, output, command_argv=None,
                    measurement_mode="call-count"):
    if measurement_mode not in ("call-count", "performance"):
        raise ValueError("formal G2 recording requires call-count or performance mode")
    previous_stage = _require_previous_stage(stage, output)
    output = _fresh(output)
    run_id = uuid.uuid4().hex
    source = _archive_sources(output)
    predecessor = _signed_g1_predecessor(output)
    axes = matrix_cases(stage, manifest)
    capacity = None
    if stage == "fast":
        capacity_probe(output=output)
        capacity = dict(path="capacity.json", sha256=_hash(output / "capacity.json"))
    # Even unpaced formal fast uses the original persistent spawn transport.
    # Its final negative kills that same worker; no replacement worker is made.
    transport = "spawn"
    worker = MotionSolverWorker()
    inbox = MotionResultInbox(worker=worker)
    rows, raws = [], []
    if stage == "realtime":
        for i in range(10):
            raw = _probe(axes[i % len(axes)], inbox, worker, group="realtime", realtime=True,
                         warmup=True, measurement_mode=measurement_mode)
            raws.append(raw)
            rows.append(_write_probe(raw, output, run_id=run_id, case_id=f"warmup-{i+1:02}"))
            if raw["status"] != "PASS" or not raw.get("released_by_original_owner"):
                break
    if not raws or all(raw["status"] == "PASS" for raw in raws):
        for i, coordinate in enumerate(axes):
            raw = _probe(coordinate, inbox, worker, group=GROUPS[stage],
                         realtime=stage == "realtime", measurement_mode=measurement_mode)
            raws.append(raw)
            rows.append(_write_probe(raw, output, run_id=run_id, case_id=f"{GROUPS[stage]}-{i+1:03}"))
            # A failed owner close or unresolved body stops the stage immediately.
            if raw["status"] != "PASS" or not raw.get("released_by_original_owner") or validate_probe(raw, check_performance=False):
                break
    if stage == "fast" and len(raws) == len(axes) and all(
            raw["status"] == "PASS" and raw.get("released_by_original_owner") for raw in raws):
        for case_id in _read(manifest)["representative_negative_case_ids"]:
            coordinate = dict(axes[0], negative_case_id=case_id)
            raw = _probe(coordinate, inbox, worker, group="representative_negative",
                         measurement_mode=measurement_mode)
            raws.append(raw)
            rows.append(_write_probe(raw, output, run_id=run_id, case_id="negative-" + case_id))
            if raw["status"] != "PASS" or not raw.get("released_by_original_owner") or validate_probe(raw, check_performance=False):
                break
    close = inbox.close(timeout_seconds=10)
    final = _final_resources(close)
    argv = command_argv or [sys.executable, "scripts/measure_prepared_motion_d102.py", "--stage", stage,
            "--manifest", str(manifest), "--output", str(output),
            "--measurement-mode", measurement_mode]
    index = dict(schema="d102-g2-run-v1", stage=stage, run_id=run_id, utc=datetime.now(timezone.utc).isoformat(),
        case_count=len(rows), cases=rows, environment=dict(system=platform.system(), python=sys.version,
        executable=sys.executable), command_argv=argv, command=subprocess.list2cmdline(argv),
        manifest_sha256=_hash(manifest), transport=transport, final_resources=final, native_close_report=asdict(close),
        measurement_mode=measurement_mode,
        profile_enabled=measurement_mode == "call-count", timing_valid=measurement_mode == "performance",
        source=source, predecessor=predecessor, previous_stage=previous_stage, capacity=capacity,
        historical_d101_m0="FAILED", historical_d098_p3="FAILED")
    (output / "index.json").write_bytes(canonical_bytes(index))
    try:
        summary = recompute_run(output / "index.json")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        summary = dict(passed=False, gate_errors=["indexed raw validation failed: " + str(error)],
                       raw_case_count=len(raws), final_resources=final)
    (output / "summary.json").write_bytes(canonical_bytes(summary))
    return summary
