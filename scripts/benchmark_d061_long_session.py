"""Run the D061 long single-Session preparation and GC evidence."""
from __future__ import annotations

import argparse
from array import array
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
from mc2p.runtime.segmented_trace import (
    SegmentedTraceWriter,
    iter_segmented_jsonl,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from mc2p.skills.known_world_follow_driver import KnownWorldFollowDriver
from tests.sim.known_world_following import (
    FOLLOW_HOLD_DISTANCE_BLOCKS,
    MAX_PLANNING_SUBMISSIONS_PER_REVISION,
    MAX_REVISION_RESPONSE_P95_TICKS,
    MAX_STABLE_MEAN_LAG_BLOCKS,
    MAX_STABLE_P95_LAG_BLOCKS,
    FollowScenario,
    run_scenario_with_trace,
)


SCHEMA_VERSION = "mc2p.d061-long-session-performance.v5"
TRACE_CAPACITY = 2048
TRACE_PROJECTION_SAMPLE_CAPACITY = 4096
DEFAULT_WARMUP = 100
DEFAULT_MINIMUM_RETAINED = 4096
DEFAULT_MAXIMUM_RETAINED = 8192
DEFAULT_POST_GEN2_SAMPLES = 128
GC_EVENT_CAPACITY = 65536
P95_LIMIT_MS = 8.0
P99_LIMIT_MS = 15.0
MAXIMUM_LIMIT_MS = 30.0
CONTROL_PATH_MAXIMUM_LIMIT_MS = 50.0
CONTROL_PERIOD_NS = 50_000_000


@dataclass(frozen=True, slots=True)
class _FrameTimingSample:
    production_ns: int
    simulation_backend_ns: int
    full_harness_ns: int
    production_segments: tuple[tuple[str, int], ...]
    following_revision: bool


class _FrameTimingRecorder:
    """Directly time disjoint production/backend spans in one harness frame."""

    def __init__(self, clock_ns=time.perf_counter_ns) -> None:
        if not callable(clock_ns):
            raise TypeError("frame timing recorder requires a clock")
        self._clock = clock_ns
        self._frame_started: int | None = None
        self._production_started: int | None = None
        self._production_label: str | None = None
        self._backend_started: int | None = None
        self._segments: dict[str, int] = {}
        self._backend_ns = 0

    @property
    def frame_active(self) -> bool:
        return self._frame_started is not None

    def start_frame(self) -> None:
        if self.frame_active:
            raise RuntimeError("timing frame already active")
        self._frame_started = self._clock()
        self._production_started = None
        self._production_label = None
        self._backend_started = None
        self._segments = {}
        self._backend_ns = 0

    def _elapsed(self, started: int) -> int:
        elapsed = self._clock() - started
        if elapsed < 0:
            raise RuntimeError("timing clock moved backwards")
        return elapsed

    def _record_production_interval(self) -> None:
        if self._production_started is None or self._production_label is None:
            raise RuntimeError("production interval is not active")
        duration = self._elapsed(self._production_started)
        self._segments[self._production_label] = (
            self._segments.get(self._production_label, 0) + duration
        )
        self._production_started = None

    def start_production(self, label: str) -> None:
        if (not self.frame_active or type(label) is not str or not label
                or self._production_started is not None
                or self._backend_started is not None):
            raise RuntimeError("production timing boundary is invalid")
        self._production_label = label
        self._production_started = self._clock()

    def finish_production(self, label: str) -> None:
        if self._production_label != label or self._backend_started is not None:
            raise RuntimeError("production timing boundary does not match")
        self._record_production_interval()
        self._production_label = None

    def start_backend(self) -> None:
        if (self._production_started is None
                or self._production_label is None
                or self._backend_started is not None):
            raise RuntimeError("backend must interrupt one production span")
        self._record_production_interval()
        self._backend_started = self._clock()

    def finish_backend(self) -> None:
        if self._backend_started is None or self._production_label is None:
            raise RuntimeError("backend timing boundary does not match")
        self._backend_ns += self._elapsed(self._backend_started)
        self._backend_started = None
        self._production_started = self._clock()

    def finish_frame(self, *, following_revision: bool) -> _FrameTimingSample:
        if (not self.frame_active or self._production_started is not None
                or self._production_label is not None
                or self._backend_started is not None
                or type(following_revision) is not bool):
            raise RuntimeError("timing frame ended with an active segment")
        assert self._frame_started is not None
        full = self._elapsed(self._frame_started)
        segments = tuple(self._segments.items())
        production = sum(duration for _, duration in segments)
        if production + self._backend_ns > full:
            raise RuntimeError("timing segments overlap")
        sample = _FrameTimingSample(
            production,
            self._backend_ns,
            full,
            segments,
            following_revision,
        )
        self._frame_started = None
        return sample


def _nearest_rank(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _statistics(values: list[int]) -> dict:
    if not values:
        raise ValueError("long-session performance needs retained samples")
    return {
        "samples": len(values),
        "p50_ns": _nearest_rank(values, .50),
        "p95_ns": _nearest_rank(values, .95),
        "p99_ns": _nearest_rank(values, .99),
        "maximum_ns": max(values),
        "p50_ms": _nearest_rank(values, .50) / 1_000_000,
        "p95_ms": _nearest_rank(values, .95) / 1_000_000,
        "p99_ms": _nearest_rank(values, .99) / 1_000_000,
        "maximum_ms": max(values) / 1_000_000,
    }


def _timing_gates(
    *,
    production_ns: list[int],
    simulation_backend_ns: list[int],
    full_harness_ns: list[int],
    revision_ordinals: tuple[int, ...],
) -> dict[str, bool]:
    """Evaluate every frozen timing gate from raw, directly measured spans."""
    if not production_ns or not simulation_backend_ns or not full_harness_ns:
        raise ValueError("timing gates require all three measurement layers")
    if not (len(production_ns) == len(simulation_backend_ns)
            == len(full_harness_ns)):
        raise ValueError("timing layers must describe the same frames")
    if any(type(value) is not int or value < 0 for values in (
            production_ns, simulation_backend_ns, full_harness_ns)
            for value in values):
        raise ValueError("timing samples must be nonnegative integers")
    if (type(revision_ordinals) is not tuple
            or any(type(value) is not int or value < 0
                   or value >= len(production_ns)
                   for value in revision_ordinals)):
        raise ValueError("revision ordinals must select production frames")
    production = _statistics(production_ns)
    revisions = (
        _statistics([production_ns[index] for index in revision_ordinals])
        if revision_ordinals else None
    )
    return {
        "production_prepare_p95": production["p95_ms"] <= P95_LIMIT_MS,
        "production_prepare_p99": production["p99_ms"] <= P99_LIMIT_MS,
        "production_prepare_maximum": (
            production["maximum_ms"] < MAXIMUM_LIMIT_MS
        ),
        "following_revision_production_p95": (
            revisions is not None and revisions["p95_ms"] <= P95_LIMIT_MS
        ),
        "following_revision_production_p99": (
            revisions is not None and revisions["p99_ms"] <= P99_LIMIT_MS
        ),
        "following_revision_production_maximum": (
            revisions is not None
            and revisions["maximum_ms"] < MAXIMUM_LIMIT_MS
        ),
        "full_harness_maximum": (
            _statistics(full_harness_ns)["maximum_ms"]
            < CONTROL_PATH_MAXIMUM_LIMIT_MS
        ),
    }


def _control_period_outcome(duration_ns: int) -> tuple[int, bool]:
    if type(duration_ns) is not int or duration_ns < 0:
        raise ValueError("control duration must be a nonnegative integer")
    slack_ns = CONTROL_PERIOD_NS - duration_ns
    return slack_ns, slack_ns <= 0


def _gen2_coverage(
    *,
    completed_control_frames: int,
    warmup_control_frames: int,
    maximum_retained: int,
    post_gen2_frames: int,
    first_gen2_completed_frames_before_event: int | None,
    first_gen2_followup_start_control_ordinal: int | None,
) -> dict:
    retained_frames = max(
        0, completed_control_frames - warmup_control_frames,
    )
    first_retained_ordinal = None
    if (first_gen2_completed_frames_before_event is not None
            and first_gen2_completed_frames_before_event
                >= warmup_control_frames):
        first_retained_ordinal = (
            first_gen2_completed_frames_before_event
            - warmup_control_frames
        )
    observed = first_retained_ordinal is not None
    post_complete = (
        observed
        and first_gen2_followup_start_control_ordinal is not None
        and completed_control_frames
            - first_gen2_followup_start_control_ordinal
            >= post_gen2_frames
    )
    return {
        "sample_limit_reached": retained_frames >= maximum_retained,
        "retained_gen2_observed": observed,
        "post_gen2_complete": post_complete,
        "first_retained_gen2_ordinal": first_retained_ordinal,
        "passed": observed and post_complete,
    }


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT,
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_identity() -> dict:
    paths = (
        "scripts/benchmark_d061_long_session.py",
        "tests/sim/known_world_following.py",
        "mc2p/runtime/async_trace.py",
        "mc2p/runtime/segmented_trace.py",
        "mc2p/runtime/player_runtime_v1.py",
        "mc2p/runtime/arbiter_v1.py",
        "mc2p/skills/known_world_follow_driver.py",
        "mc2p/skills/navigation_session_driver.py",
        "mc2p/motion_nav/navigation_session.py",
        "mc2p/motion_nav/route_admission.py",
    )
    digest = hashlib.sha256()
    files = []
    for relative in paths:
        content = (ROOT / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        files.append({
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    status = _git("status", "--porcelain", "--untracked-files=normal")
    return {
        "commit": _git("rev-parse", "HEAD"),
        "hash_basis": "windows_worktree_bytes",
        "git_dirty": bool(status),
        "git_status": status.splitlines(),
        "scope_sha256": digest.hexdigest(),
        "files": files,
    }


def _scenario(*, test_mode: bool, cancel_tick: int) -> FollowScenario:
    if test_mode:
        return FollowScenario(
            "d061_long_session_test", 3.3,
            move_ticks=2, pause_ticks=2, resume_ticks=2,
            final_hold_ticks=0, cancel_tick=cancel_tick,
        )
    return FollowScenario(
        "d061_long_session", 3.3,
        move_ticks=80, pause_ticks=800, resume_ticks=80,
        final_hold_ticks=0, cancel_tick=cancel_tick,
    )


def _verify_trace(directory: Path) -> tuple[int, dict | None]:
    count = 0
    delivery = None
    for record in iter_segmented_jsonl(directory):
        count += 1
        if record.get("record_type") == "trace_delivery_summary":
            delivery = record.get("payload")
    return count, delivery


def _write_sums(output: Path) -> None:
    files = sorted(
        path for path in output.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(output).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
    )


def run_long_session(
    output: Path,
    *,
    warmup: int,
    minimum_retained: int,
    maximum_retained: int,
    post_gen2_samples: int,
    test_mode: bool,
    source: dict,
    command: str,
) -> dict:
    output.mkdir(parents=True)
    control_thread_id = threading.get_ident()
    trace = BoundedAsyncTraceWriter(
        SegmentedTraceWriter(output / "trace"),
        capacity=TRACE_CAPACITY,
    )
    elapsed: list[int] = []
    control_durations = array("Q")
    control_period_slack = array("q")
    production_durations = array("Q")
    simulation_backend_durations = array("Q")
    harness_durations = array("Q")
    following_revision_flags = bytearray()
    frame_production_segments: list[tuple[tuple[str, int], ...]] = []
    frame_timing = _FrameTimingRecorder()
    original = RuntimeNavigationDriver.prepare_proposals
    no_sample = (1 << 64) - 1
    current_sample = no_sample
    last_completed_sample = no_sample
    active_control = no_sample
    last_completed_control = no_sample
    control_started_ns = 0
    control_path_mismatch = False
    active_event_slot = -1
    event_count = 0
    overflowed = False
    start_stop_mismatch = False
    first_gen2_anywhere: int | None = None
    first_retained_production_gen2: int | None = None
    first_gen2_completed_frames_before_event: int | None = None
    first_gen2_followup_start_control_ordinal: int | None = None
    measurement_active = True
    event_active_samples = array("Q", [no_sample]) * GC_EVENT_CAPACITY
    event_last_completed = array("Q", [no_sample]) * GC_EVENT_CAPACITY
    event_active_controls = array("Q", [no_sample]) * GC_EVENT_CAPACITY
    event_last_completed_controls = (
        array("Q", [no_sample]) * GC_EVENT_CAPACITY
    )
    event_completed_controls_before = (
        array("Q", [0]) * GC_EVENT_CAPACITY
    )
    event_followup_start_controls = array("Q", [0]) * GC_EVENT_CAPACITY
    event_generations = bytearray(GC_EVENT_CAPACITY)
    event_measurement_phases = bytearray(GC_EVENT_CAPACITY)
    event_control_phases = bytearray(GC_EVENT_CAPACITY)
    event_threads = array("Q", [0]) * GC_EVENT_CAPACITY
    event_starts = array("Q", [0]) * GC_EVENT_CAPACITY
    event_ends = array("Q", [0]) * GC_EVENT_CAPACITY
    event_collected = array("Q", [0]) * GC_EVENT_CAPACITY
    identity_samples = 0
    identity_violations: list[dict] = []
    expected_session_object = None
    expected_runtime_object = None
    expected_session_id = None
    expected_goal_id = None
    expected_source_backend = None
    expected_episode_id = None
    expected_clock_id = None
    pacing_started_ns = None
    pacing_finished_ns = None
    next_tick_deadline_ns = None
    paced_ticks = 0
    pacing_overruns = 0
    maximum_pacing_lateness_ns = 0

    def gc_measurement_phase(completed_before: int) -> int:
        if active_control == no_sample and last_completed_control == no_sample:
            return 0
        return 1 if completed_before < warmup else 2

    def gc_event(phase, info):
        nonlocal active_event_slot, event_count, overflowed
        nonlocal start_stop_mismatch, first_gen2_anywhere
        nonlocal first_retained_production_gen2
        nonlocal first_gen2_completed_frames_before_event
        nonlocal first_gen2_followup_start_control_ordinal
        if phase == "start":
            if active_event_slot != -1:
                start_stop_mismatch = True
                return
            if not measurement_active:
                active_event_slot = -3
                return
            if event_count >= GC_EVENT_CAPACITY:
                overflowed = True
                active_event_slot = -2
                return
            active_event_slot = event_count
            event_active_samples[event_count] = current_sample
            event_last_completed[event_count] = last_completed_sample
            event_active_controls[event_count] = active_control
            event_last_completed_controls[event_count] = (
                last_completed_control
            )
            completed_before = (
                active_control if active_control != no_sample
                else 0 if last_completed_control == no_sample
                else last_completed_control + 1
            )
            followup_start = (
                active_control + 1 if active_control != no_sample
                else completed_before
            )
            event_completed_controls_before[event_count] = completed_before
            event_followup_start_controls[event_count] = followup_start
            event_generations[event_count] = info.get("generation", 0)
            event_measurement_phases[event_count] = (
                gc_measurement_phase(completed_before)
            )
            event_control_phases[event_count] = (
                1 if active_control != no_sample else 0
            )
            event_threads[event_count] = threading.get_ident()
            event_starts[event_count] = time.perf_counter_ns()
            return
        if phase != "stop":
            return
        if active_event_slot in {-3, -2}:
            active_event_slot = -1
            return
        if active_event_slot < 0:
            start_stop_mismatch = True
            return
        slot = active_event_slot
        event_ends[slot] = time.perf_counter_ns()
        event_collected[slot] = info.get("collected", 0)
        if event_generations[slot] == 2:
            if first_gen2_anywhere is None:
                first_gen2_anywhere = slot
            retained_phase = event_measurement_phases[slot] == 2
            production_or_trace = (
                event_active_controls[slot] != no_sample
                or event_threads[slot] != control_thread_id
            )
            if (retained_phase and production_or_trace
                    and first_retained_production_gen2 is None):
                first_retained_production_gen2 = slot
                first_gen2_completed_frames_before_event = (
                    event_completed_controls_before[slot]
                )
                first_gen2_followup_start_control_ordinal = (
                    event_followup_start_controls[slot]
                )
        event_count += 1
        active_event_slot = -1

    def check_identity(driver) -> None:
        nonlocal identity_samples, expected_session_object
        nonlocal expected_runtime_object, expected_session_id, expected_goal_id
        nonlocal expected_source_backend, expected_episode_id, expected_clock_id
        observation = driver.runtime.observation
        if expected_session_object is None:
            expected_session_object = id(driver.session)
            expected_runtime_object = id(driver.runtime)
            expected_session_id = driver.session.session_id
            expected_goal_id = driver.goal_id
            expected_source_backend = observation.source_backend
            expected_episode_id = observation.episode_id
            expected_clock_id = observation.client_sample.clock_id
        elif (
            id(driver.session) != expected_session_object
            or id(driver.runtime) != expected_runtime_object
            or driver.session.session_id != expected_session_id
            or driver.goal_id != expected_goal_id
            or observation.source_backend != expected_source_backend
            or observation.episode_id != expected_episode_id
            or observation.client_sample.clock_id != expected_clock_id
        ):
            if not identity_violations:
                identity_violations.append({
                    "captured_sample_ordinal": len(elapsed),
                    "session_id": driver.session.session_id,
                    "goal_id": driver.goal_id,
                    "source_backend": observation.source_backend,
                    "episode_id": observation.episode_id,
                    "clock_id": observation.client_sample.clock_id,
                })
        identity_samples += 1

    def measured(driver, *args, **kwargs):
        nonlocal current_sample, last_completed_sample
        if not measurement_active:
            return original(driver, *args, **kwargs)
        sample_ordinal = len(elapsed)
        current_sample = sample_ordinal
        frame_timing.start_production("navigation_prepare")
        started = time.perf_counter_ns()
        try:
            return original(driver, *args, **kwargs)
        finally:
            duration = time.perf_counter_ns() - started
            frame_timing.finish_production("navigation_prepare")
            current_sample = no_sample
            elapsed.append(duration)
            last_completed_sample = sample_ordinal
            check_identity(driver)

    def measured_follow_update(driver, *args, **kwargs):
        if not measurement_active or not frame_timing.frame_active:
            return original_follow_update(driver, *args, **kwargs)
        frame_timing.start_production("follow_update")
        try:
            return original_follow_update(driver, *args, **kwargs)
        finally:
            frame_timing.finish_production("follow_update")

    def measured_follow_cancel(driver, *args, **kwargs):
        if not measurement_active or not frame_timing.frame_active:
            return original_follow_cancel(driver, *args, **kwargs)
        frame_timing.start_production("follow_cancel")
        try:
            return original_follow_cancel(driver, *args, **kwargs)
        finally:
            frame_timing.finish_production("follow_cancel")

    def measured_control_frame(runtime, *args, **kwargs):
        if not measurement_active or not frame_timing.frame_active:
            return original_control_frame(runtime, *args, **kwargs)
        frame_timing.start_production("runtime_control_frame")
        try:
            return original_control_frame(runtime, *args, **kwargs)
        finally:
            frame_timing.finish_production("runtime_control_frame")

    def measured_backend_step(runtime, *args, **kwargs):
        if not measurement_active or not frame_timing.frame_active:
            return original_backend_step(runtime, *args, **kwargs)
        frame_timing.start_backend()
        try:
            return original_backend_step(runtime, *args, **kwargs)
        finally:
            frame_timing.finish_backend()

    def measured_adopt(driver, *args, **kwargs):
        if not measurement_active or not frame_timing.frame_active:
            return original_adopt(driver, *args, **kwargs)
        frame_timing.start_production("navigation_adopt")
        try:
            return original_adopt(driver, *args, **kwargs)
        finally:
            frame_timing.finish_production("navigation_adopt")

    def harness_frame_started(
        _tick: int,
        _backend_elapsed_ns_total: int,
    ) -> None:
        if not measurement_active:
            return
        frame_timing.start_frame()

    def harness_frame_finished(
        _tick: int,
        revised_this_tick: bool,
        _backend_elapsed_ns_total: int,
    ) -> None:
        if not frame_timing.frame_active:
            return
        sample = frame_timing.finish_frame(
            following_revision=revised_this_tick,
        )
        production_durations.append(sample.production_ns)
        simulation_backend_durations.append(sample.simulation_backend_ns)
        harness_durations.append(sample.full_harness_ns)
        following_revision_flags.append(1 if sample.following_revision else 0)
        frame_production_segments.append(sample.production_segments)

    def control_path_started(_tick: int) -> None:
        nonlocal active_control, control_started_ns, control_path_mismatch
        if not measurement_active:
            active_control = no_sample
            return
        if active_control != no_sample:
            control_path_mismatch = True
            return
        active_control = len(control_durations)
        control_started_ns = time.perf_counter_ns()

    def control_path_finished(
        _tick: int,
        _logical_deadline_ns: int | None,
        _completed_clock_ns: int,
    ) -> None:
        nonlocal active_control, last_completed_control
        if active_control == no_sample:
            return
        ended_ns = time.perf_counter_ns()
        control_ordinal = active_control
        active_control = no_sample
        last_completed_control = control_ordinal
        duration_ns = ended_ns - control_started_ns
        control_durations.append(duration_ns)
        control_period_slack.append(_control_period_outcome(duration_ns)[0])

    def cancel_when(_tick: int) -> bool:
        nonlocal measurement_active
        retained = max(0, len(control_durations) - warmup)
        if retained < minimum_retained:
            return False
        if first_retained_production_gen2 is None:
            finished = retained >= maximum_retained
        else:
            finished = (
                len(control_durations)
                    - first_gen2_followup_start_control_ordinal
                    >= post_gen2_samples
                or retained >= maximum_retained
            )
        if finished:
            measurement_active = False
        return finished

    def pace_tick(_tick: int) -> None:
        nonlocal pacing_started_ns, pacing_finished_ns
        nonlocal next_tick_deadline_ns, paced_ticks
        nonlocal pacing_overruns, maximum_pacing_lateness_ns
        if test_mode:
            time.sleep(0)
            paced_ticks += 1
            return
        now_ns = time.perf_counter_ns()
        if next_tick_deadline_ns is None:
            pacing_started_ns = now_ns
            next_tick_deadline_ns = now_ns
        next_tick_deadline_ns += CONTROL_PERIOD_NS
        remaining_ns = next_tick_deadline_ns - time.perf_counter_ns()
        if remaining_ns > 0:
            time.sleep(remaining_ns / 1_000_000_000)
        else:
            pacing_overruns += 1
            maximum_pacing_lateness_ns = max(
                maximum_pacing_lateness_ns, -remaining_ns,
            )
        paced_ticks += 1
        pacing_finished_ns = time.perf_counter_ns()

    cancel_tick = warmup + maximum_retained + 512
    scenario = _scenario(test_mode=test_mode, cancel_tick=cancel_tick)
    original_follow_update = KnownWorldFollowDriver.update
    original_follow_cancel = KnownWorldFollowDriver.cancel
    original_control_frame = PlayerRuntimeV1.control_frame
    original_backend_step = PlayerRuntimeV1._backend_step
    original_adopt = RuntimeNavigationDriver.adopt_result
    gc.callbacks.append(gc_event)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                RuntimeNavigationDriver, "prepare_proposals", measured,
            ))
            stack.enter_context(patch.object(
                KnownWorldFollowDriver, "update", measured_follow_update,
            ))
            stack.enter_context(patch.object(
                KnownWorldFollowDriver, "cancel", measured_follow_cancel,
            ))
            stack.enter_context(patch.object(
                PlayerRuntimeV1, "control_frame", measured_control_frame,
            ))
            stack.enter_context(patch.object(
                PlayerRuntimeV1, "_backend_step", measured_backend_step,
            ))
            stack.enter_context(patch.object(
                RuntimeNavigationDriver, "adopt_result", measured_adopt,
            ))
            behavior = run_scenario_with_trace(
                scenario, trace,
                cancel_when=cancel_when, after_tick=pace_tick,
                control_path_started=control_path_started,
                control_path_finished=control_path_finished,
                harness_frame_started=harness_frame_started,
                harness_frame_finished=harness_frame_finished,
            )
    finally:
        if gc_event in gc.callbacks:
            gc.callbacks.remove(gc_event)

    stats = trace.stats
    trace_records, delivery = _verify_trace(output / "trace")
    retained = elapsed[warmup:]
    statistics = _statistics(retained)
    retained_control_durations = list(control_durations[warmup:])
    retained_control_slack = list(control_period_slack[warmup:])
    retained_production = list(production_durations[warmup:])
    retained_simulation_backend = list(
        simulation_backend_durations[warmup:]
    )
    retained_harness = list(harness_durations[warmup:])
    retained_revision_flags = list(following_revision_flags[warmup:])
    revision_ordinals = tuple(
        index for index, value in enumerate(retained_revision_flags) if value
    )
    production_statistics = _statistics(retained_production)
    simulation_backend_statistics = _statistics(
        retained_simulation_backend
    )
    harness_statistics = _statistics(retained_harness)
    revision_production_statistics = (
        _statistics([retained_production[index]
                     for index in revision_ordinals])
        if revision_ordinals else None
    )
    timing_gates = _timing_gates(
        production_ns=retained_production,
        simulation_backend_ns=retained_simulation_backend,
        full_harness_ns=retained_harness,
        revision_ordinals=revision_ordinals,
    )
    captured_control_statistics = _statistics(list(control_durations))
    retained_control_statistics = _statistics(retained_control_durations)
    retained_deadline_misses = sum(
        slack_ns <= 0 for slack_ns in retained_control_slack
    )
    gen2_coverage = _gen2_coverage(
        completed_control_frames=len(control_durations),
        warmup_control_frames=warmup,
        maximum_retained=maximum_retained,
        post_gen2_frames=post_gen2_samples,
        first_gen2_completed_frames_before_event=(
            first_gen2_completed_frames_before_event
        ),
        first_gen2_followup_start_control_ordinal=(
            first_gen2_followup_start_control_ordinal
        ),
    )
    unmatched_start = active_event_slot != -1
    unmatched_control_path = active_control != no_sample
    unmatched_harness = frame_timing.frame_active
    gc_diagnostic_integrity = (
        not overflowed and not start_stop_mismatch and not unmatched_start
    )
    generation_counts = {
        str(generation): sum(
            event_generations[index] == generation
            for index in range(event_count)
        )
        for generation in range(3)
    }
    phase_names = ("before_samples", "warmup", "retained")
    control_phase_names = ("outside", "active")

    def event_mapping(index: int) -> dict:
        active_prepare = event_active_samples[index]
        last_completed = event_last_completed[index]
        active_control_ordinal = event_active_controls[index]
        last_completed_control_ordinal = event_last_completed_controls[index]
        origin = (
            "control_path" if active_control_ordinal != no_sample
            else "trace_thread"
            if event_threads[index] != control_thread_id
            else "outside_control_thread"
        )
        return {
            "event_ordinal": index,
            "generation": event_generations[index],
            "start_ns": event_starts[index],
            "end_ns": event_ends[index],
            "duration_ns": event_ends[index] - event_starts[index],
            "collected": event_collected[index],
            "active_prepare_ordinal": (
                None if active_prepare == no_sample else active_prepare
            ),
            "last_completed_prepare_ordinal": (
                None if last_completed == no_sample else last_completed
            ),
            "active_control_ordinal": (
                None if active_control_ordinal == no_sample
                else active_control_ordinal
            ),
            "last_completed_control_ordinal": (
                None if last_completed_control_ordinal == no_sample
                else last_completed_control_ordinal
            ),
            "completed_control_frames_before_event": (
                event_completed_controls_before[index]
            ),
            "followup_start_control_ordinal": (
                event_followup_start_controls[index]
            ),
            "measurement_phase": (
                phase_names[event_measurement_phases[index]]
            ),
            "control_path_phase": (
                control_phase_names[event_control_phases[index]]
            ),
            "origin": origin,
            "thread_id": event_threads[index],
        }

    first_gen2_mapping = (
        None if first_gen2_anywhere is None
        else event_mapping(first_gen2_anywhere)
    )
    first_retained_gen2_mapping = (
        None if first_retained_production_gen2 is None
        else event_mapping(first_retained_production_gen2)
    )
    formal_behavior_gates = {
        "timeline_completed": behavior["observed_ticks"] > 960,
        "stable_lag": (
            behavior["stable_excess_lag"]["mean_blocks"] is not None
            and behavior["stable_excess_lag"]["p95_blocks"] is not None
            and behavior["stable_excess_lag"]["mean_blocks"]
                <= MAX_STABLE_MEAN_LAG_BLOCKS
            and behavior["stable_excess_lag"]["p95_blocks"]
                <= MAX_STABLE_P95_LAG_BLOCKS
        ),
        "final_within_hold": (
            behavior["final_raw_distance_blocks"]
            <= FOLLOW_HOLD_DISTANCE_BLOCKS + 1.0e-9
        ),
        "planning_ratio": (
            behavior["planning_submissions_per_accepted_revision"]
            <= MAX_PLANNING_SUBMISSIONS_PER_REVISION
        ),
        "revision_response": (
            behavior["unanswered_revisions"] == 0
            and behavior["revision_response_p95_ticks"] is not None
            and behavior["revision_response_p95_ticks"]
                <= MAX_REVISION_RESPONSE_P95_TICKS
        ),
        "recoveries": behavior["task_recoveries"] == 0,
    }
    if test_mode:
        formal_behavior_gates = {
            "timeline_completed": behavior["observed_ticks"] > 6,
        }
    pacing_elapsed_ns = (
        None if pacing_started_ns is None or pacing_finished_ns is None
        else pacing_finished_ns - pacing_started_ns
    )
    effective_hz = (
        None if pacing_elapsed_ns in {None, 0}
        else paced_ticks * 1_000_000_000 / pacing_elapsed_ns
    )
    gates = {
        **timing_gates,
        # Compatibility names stay visible to older evidence readers.  They
        # point at the new directly measured layers rather than weakening a
        # gate or deriving one duration by subtraction.
        "prepare_p95": timing_gates["production_prepare_p95"],
        "prepare_p99": timing_gates["production_prepare_p99"],
        "prepare_maximum": timing_gates["production_prepare_maximum"],
        "control_path_maximum": timing_gates["full_harness_maximum"],
        "input_deadline_miss": retained_deadline_misses == 0,
        "minimum_deadline_slack": (
            bool(retained_control_slack)
            and min(retained_control_slack) > 0
        ),
        "prepare_control_alignment": (
            len(elapsed) == len(control_durations)
            and len(control_durations) == len(control_period_slack)
            and len(control_durations) == len(production_durations)
            and len(production_durations)
                == len(simulation_backend_durations)
            and len(simulation_backend_durations) == len(harness_durations)
            and len(harness_durations) == len(following_revision_flags)
            and len(harness_durations) == len(frame_production_segments)
        ),
        "control_path_integrity": (
            not control_path_mismatch and not unmatched_control_path
            and not unmatched_harness
        ),
        "sample_minimum": (
            len(retained_control_durations) >= minimum_retained
        ),
        "sample_maximum": (
            len(retained_control_durations) <= maximum_retained
        ),
        "gen2_coverage": gen2_coverage["passed"],
        "gc_diagnostic_integrity": gc_diagnostic_integrity,
        "behavior": all(formal_behavior_gates.values()),
        "safety": not behavior["safety_violations"],
        "identity": not identity_violations,
        "pacing": (
            test_mode
            or (effective_hz is not None and 19.5 <= effective_hz <= 20.5)
        ),
        "terminal": (
            behavior["terminal_session_state"] == "cancelled"
            and behavior["source_released"]
        ),
        "trace": (
            not stats.worker_failed
            and stats.dropped_records == 0
            and delivery is not None
            and trace_records == stats.written_records + 1
            and delivery["capacity"] == TRACE_CAPACITY
            and delivery["projection_timing_ns"]["sample_count"]
                <= TRACE_PROJECTION_SAMPLE_CAPACITY
        ),
    }
    captured_frame_samples = [
        {
            "captured_ordinal": index,
            "phase": "warmup" if index < warmup else "retained",
            "production_prepare_ns": production_durations[index],
            "simulation_backend_ns": simulation_backend_durations[index],
            "full_harness_ns": harness_durations[index],
            "following_revision": bool(following_revision_flags[index]),
            "production_segments_ns": [
                {"name": name, "duration_ns": duration}
                for name, duration in frame_production_segments[index]
            ],
        }
        for index in range(len(harness_durations))
    ]
    retained_frame_samples = [
        {
            **sample,
            "retained_ordinal": index,
        }
        for index, sample in enumerate(captured_frame_samples[warmup:])
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "wall_clock": "time.perf_counter_ns",
            "hash_basis": "windows_worktree_bytes",
            "control_thread_id": control_thread_id,
            "control_frequency_hz": 20,
            "control_period_ns": CONTROL_PERIOD_NS,
        },
        "configuration": {
            "test_mode": test_mode,
            "warmup_samples": warmup,
            "minimum_retained_samples": minimum_retained,
            "maximum_retained_samples": maximum_retained,
            "post_gen2_samples": post_gen2_samples,
            "control_path_maximum_ms": CONTROL_PATH_MAXIMUM_LIMIT_MS,
            "trace_capacity": TRACE_CAPACITY,
            "trace_projection_sample_capacity": (
                TRACE_PROJECTION_SAMPLE_CAPACITY
            ),
            "move_ticks": scenario.move_ticks,
            "pause_ticks": scenario.pause_ticks,
            "resume_ticks": scenario.resume_ticks,
        },
        "timing_boundaries": {
            "production_prepare_ms": {
                "measurement_boundary": (
                    "direct spans around KnownWorldFollowDriver update/cancel, "
                    "RuntimeNavigationDriver prepare/adopt, plus the "
                    "PlayerRuntimeV1.control_frame intervals before and after "
                    "its directly timed backend call"
                ),
                "includes": [
                    "following_target_revision",
                    "navigation_session_ingest_propose",
                    "runtime_arbitration_and_execution_window_checks",
                    "runtime_input_ledger_and_dispatch_trace",
                    "runtime_result_validation_observation_ingest_and_report",
                    "navigation_result_adoption",
                ],
                "excludes": [
                    "simulation_backend_step",
                    "simulator_diagnostics",
                    "test_metric_aggregation",
                    "pacing_sleep",
                ],
                "derived_by_subtraction": False,
            },
            "simulation_backend_ms": {
                "measurement_boundary": (
                    "direct PlayerRuntimeV1.backend_elapsed_ns_total delta "
                    "between harness frame callbacks"
                ),
                "includes": [
                    "simulated_visual_queries",
                    "simulated_world_and_physics_advance",
                ],
                "excludes": [
                    "navigation_prepare",
                    "runtime_arbitration",
                    "test_metric_aggregation",
                    "pacing_sleep",
                ],
                "derived_by_subtraction": False,
            },
            "full_harness_ms": {
                "measurement_boundary": (
                    "direct wall span from the start of one scenario frame "
                    "through navigation, backend, invariants, and per-frame "
                    "metric collection; ends before pacing"
                ),
                "includes": [
                    "production_prepare_ms",
                    "simulation_backend_ms",
                    "invariant_monitor",
                    "per_frame_evidence_and_metrics",
                ],
                "excludes": ["pacing_sleep"],
                "derived_by_subtraction": False,
            },
        },
        "timing_layers": {
            "production_prepare_ms": {
                "measurement_boundary": "timing_boundaries.production_prepare_ms",
                "samples": len(retained_production),
                "raw_samples_ns": retained_production,
                "statistics": production_statistics,
            },
            "simulation_backend_ms": {
                "measurement_boundary": "timing_boundaries.simulation_backend_ms",
                "samples": len(retained_simulation_backend),
                "raw_samples_ns": retained_simulation_backend,
                "statistics": simulation_backend_statistics,
                "gating": "reported_only",
            },
            "full_harness_ms": {
                "measurement_boundary": "timing_boundaries.full_harness_ms",
                "samples": len(retained_harness),
                "raw_samples_ns": retained_harness,
                "statistics": harness_statistics,
            },
            "following_revision_frames": {
                "retained_ordinals": list(revision_ordinals),
                "samples": len(revision_ordinals),
                "production_prepare_statistics": (
                    revision_production_statistics
                ),
            },
        },
        "frame_samples": captured_frame_samples,
        "retained_frame_samples": retained_frame_samples,
        "complete_prepare": {
            "captured_samples": len(elapsed),
            "warmup_samples": warmup,
            "retained_samples": len(retained),
            "raw_samples_ns": retained,
            "statistics": statistics,
            "gc_diagnostic": {
                "capacity": GC_EVENT_CAPACITY,
                "event_count": event_count,
                "overflowed": overflowed,
                "start_stop_mismatch": start_stop_mismatch,
                "unmatched_start": unmatched_start,
                "generation_counts": generation_counts,
                "first_gen2_anywhere": first_gen2_mapping,
                "first_retained_production_gen2": (
                    first_retained_gen2_mapping
                ),
                "events": [event_mapping(index)
                           for index in range(event_count)],
            },
        },
        "formal_control_path": {
            "warmup_frames": warmup,
            "captured": {
                "frames": len(control_durations),
                "statistics": captured_control_statistics,
            },
            "retained": {
                "frames": len(retained_control_durations),
                "raw_durations_ns": retained_control_durations,
                "raw_control_period_slack_ns": retained_control_slack,
                "statistics": retained_control_statistics,
                "deadline_miss_count": retained_deadline_misses,
                "minimum_slack_ns": (
                    min(retained_control_slack)
                    if retained_control_slack else None
                ),
            },
            "path_boundary": (
                "KnownWorldFollowDriver.update_if_due_plus_"
                "RuntimeNavigationDriver.tick_through_backend_acceptance"
            ),
            "excluded_from_timing": [
                "InvariantMonitor",
                "tick_evidence",
                "revision_response_frames",
                "metric_aggregation",
            ],
            "gen2_coverage": gen2_coverage,
        },
        "behavior": behavior,
        "behavior_gates": formal_behavior_gates,
        "identity": {
            "task_id": f"f1-task/{scenario.name}",
            "session_id": expected_session_id,
            "goal_id": expected_goal_id,
            "world_session": (
                f"{expected_source_backend}:{expected_episode_id}:"
                f"{expected_clock_id}"
            ),
            "samples_checked": identity_samples,
            "consistent": not identity_violations,
            "violations": identity_violations,
        },
        "pacing": {
            "period_ns": CONTROL_PERIOD_NS,
            "paced_ticks": paced_ticks,
            "elapsed_ns": pacing_elapsed_ns,
            "effective_hz": effective_hz,
            "overruns": pacing_overruns,
            "maximum_lateness_ns": maximum_pacing_lateness_ns,
        },
        "trace": {
            "capacity": stats.capacity,
            "accepted_records": stats.accepted_records,
            "written_records": stats.written_records,
            "dropped_records": stats.dropped_records,
            "peak_depth": stats.peak_depth,
            "worker_failed": stats.worker_failed,
            "verified_records": trace_records,
            "projection_sample_capacity": TRACE_PROJECTION_SAMPLE_CAPACITY,
            "delivery_summary": delivery,
        },
        "gates": gates,
        "passed": all(gates.values()),
    }
    (output / "performance.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output / "COMMAND.txt").write_text(command + "\n", encoding="utf-8")
    _write_sums(output)
    return payload


def _command(arguments) -> str:
    recorded = os.environ.get("MC2P_D061_RECORDED_COMMAND")
    if recorded is not None:
        if not recorded or "\n" in recorded or "\r" in recorded:
            raise ValueError("recorded D061 command must be one nonempty line")
        return recorded
    command = [
        sys.executable,
        "-m",
        "scripts.benchmark_d061_long_session",
        "--output",
        str(arguments.output),
    ]
    if arguments.test_mode:
        command.append("--test-mode")
    for option, value, default in (
        ("--warmup", arguments.warmup, DEFAULT_WARMUP),
        ("--minimum-retained", arguments.minimum_retained,
         DEFAULT_MINIMUM_RETAINED),
        ("--maximum-retained", arguments.maximum_retained,
         DEFAULT_MAXIMUM_RETAINED),
        ("--post-gen2-samples", arguments.post_gen2_samples,
         DEFAULT_POST_GEN2_SAMPLES),
    ):
        if value != default:
            command.extend((option, str(value)))
    return subprocess.list2cmdline(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument(
        "--minimum-retained", type=int,
        default=DEFAULT_MINIMUM_RETAINED,
    )
    parser.add_argument(
        "--maximum-retained", type=int,
        default=DEFAULT_MAXIMUM_RETAINED,
    )
    parser.add_argument(
        "--post-gen2-samples", type=int,
        default=DEFAULT_POST_GEN2_SAMPLES,
    )
    parser.add_argument("--test-mode", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.output.exists():
        parser.error(
            f"refusing to overwrite benchmark evidence: {arguments.output}"
        )
    if (arguments.warmup < 0 or arguments.minimum_retained < 1
            or arguments.maximum_retained < arguments.minimum_retained
            or arguments.post_gen2_samples < 1):
        parser.error("long-session sample bounds are invalid")
    if (not arguments.test_mode and (
            arguments.warmup != DEFAULT_WARMUP
            or arguments.minimum_retained != DEFAULT_MINIMUM_RETAINED
            or arguments.maximum_retained != DEFAULT_MAXIMUM_RETAINED
            or arguments.post_gen2_samples != DEFAULT_POST_GEN2_SAMPLES)):
        parser.error("formal long-session evidence uses frozen sample bounds")
    source = _source_identity()
    payload = run_long_session(
        arguments.output,
        warmup=arguments.warmup,
        minimum_retained=arguments.minimum_retained,
        maximum_retained=arguments.maximum_retained,
        post_gen2_samples=arguments.post_gen2_samples,
        test_mode=arguments.test_mode,
        source=source,
        command=_command(arguments),
    )
    print(json.dumps({
        "output": str(arguments.output),
        "retained_control_frames": (
            payload["formal_control_path"]["retained"]["frames"]
        ),
        "first_gen2_anywhere": (
            payload["complete_prepare"]["gc_diagnostic"]
            ["first_gen2_anywhere"]
        ),
        "first_retained_gen2_ordinal": (
            payload["formal_control_path"]["gen2_coverage"]
            ["first_retained_gen2_ordinal"]
        ),
        "passed": payload["passed"],
    }, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
