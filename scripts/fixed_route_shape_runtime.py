"""Fabric runtime for B03 view-independent line and closed-circle trials."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import time

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.evidence.shape_trials import (
    circle_metrics, circle_route_points, line_metrics, line_reference_yaw_degrees,
)
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import _motion_profile, receipt_confirms_input


_SOURCE = "b03-shape-trials"


def _yaw_delta(target: float, actual: float) -> float:
    return (target - actual + 180.0) % 360.0 - 180.0


def _route(route_id: str, points: tuple[tuple[float, float, float], ...]) -> FixedRoute:
    return FixedRoute(route_id, tuple(RoutePoint(*point) for point in points))


def _lengths(points: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, ...], float]:
    cumulative = [0.0]
    for first, second in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.hypot(second[0] - first[0], second[2] - first[2]))
    return tuple(cumulative), cumulative[-1]


def _tangent_yaw(points: tuple[tuple[float, float, float], ...], progress: float) -> float:
    cumulative, total = _lengths(points)
    progress = min(total, max(0.0, progress))
    index = 0
    while index + 1 < len(cumulative) - 1 and progress > cumulative[index + 1]:
        index += 1
    first, second = points[index], points[index + 1]
    return math.degrees(math.atan2(-(second[0] - first[0]), second[2] - first[2]))


def _bounds(reference: tuple[tuple[float, float, float], ...], samples: list[dict]) -> dict:
    xs = [point[0] for point in reference] + [sample["position"][0] for sample in samples]
    zs = [point[2] for point in reference] + [sample["position"][2] for sample in samples]
    return dict(min_x=math.floor(min(xs) - 1), max_x=math.ceil(max(xs) + 1),
                min_z=math.floor(min(zs) - 1), max_z=math.ceil(max(zs) + 1))


def run_fixed_route_shape_runtime(runtime, backend, episode: str, directory: Path,
                                  deadline_ns: int) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "b03-shape-trials", "fixed_route_shape_tracking", "{}",
        (SuccessCriterionV0("shape_trials_recorded", ComparisonOperatorV0.EQUAL, 1, "boolean"),),
        200, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    rows: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        row = dict(episode_id=episode, observation_sequence_id=runtime.observation.sequence_id,
                   diagnostics=backend.last_diagnostics)
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement: MovementV1 = MovementV1(), *, look: LookV1 | None = None,
             valid_for_ticks: int = 2, observation_request=None):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"shape-{counter}", _SOURCE, episode, runtime.observation.sequence_id,
            ActionPriorityV0.TASK, now, min(deadline_ns, now + 750_000_000),
            movement=movement, look=look, valid_for_ticks=valid_for_ticks,
        ))
        result = runtime.step(task, behavior, min(deadline_ns, now + 5_000_000_000),
                              observation_request=observation_request)
        if result.observation is None or result.backend_result is None or result.decision is None:
            raise RuntimeError(f"shape trial Fabric step failed: {result.report}")
        if result.report.status.value != "running":
            raise RuntimeError(f"shape trial ended unexpectedly: {result.report}")
        if result.decision.action.movement != movement:
            raise RuntimeError("shape trial movement did not pass through the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    def orient(yaw: float, pitch: float) -> None:
        actual_yaw = math.degrees(frame.body.yaw_radians)
        actual_pitch = math.degrees(frame.body.pitch_radians)
        step(look=LookV1(_yaw_delta(yaw, actual_yaw), pitch - actual_pitch), valid_for_ticks=1)

    def execute(route: FixedRoute, points: tuple[tuple[float, float, float], ...], *,
                gaze, record: bool, maximum_frames: int) -> dict:
        controller = FixedRouteController(_motion_profile())
        controller.start(route, frame)
        start_ns = frame.body.stamp.received_monotonic_ns
        samples: list[dict] = []
        decisions: list[dict] = []
        input_confirmed = True

        def sample(decision=None, expected_yaw=None, tracking_error=None) -> None:
            samples.append(dict(
                t=(frame.body.stamp.received_monotonic_ns - start_ns) / 1e9,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                yaw=math.degrees(frame.body.yaw_radians),
                pitch=math.degrees(frame.body.pitch_radians),
                expected_yaw=expected_yaw,
                tracking_error=tracking_error,
                sequence=frame.body.sequence_id,
            ))
            if decision is not None:
                decisions.append(dict(
                    t=samples[-1]["t"], movement=asdict(decision["movement"]),
                    look=asdict(decision["look"]), reason=decision["reason"],
                    state=decision["state"], progress_blocks=decision["progress"],
                    expected_yaw=expected_yaw, tracking_error=tracking_error,
                    target=None, planning_ms=decision["control_time_ns"] / 1e6,
                ))

        if record:
            sample()
        final_state = FixedRouteState.RUNNING
        final_reason = ""
        for _ in range(maximum_frames):
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            final_state, final_reason = decision.state, decision.reason
            if decision.state is FixedRouteState.SUCCEEDED:
                break
            if decision.state in {
                FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                FixedRouteState.UNSUPPORTED, FixedRouteState.INPUT_LOST,
            }:
                break
            desired_yaw, desired_pitch, maximum_yaw_step = gaze(decision.progress_blocks)
            actual_yaw = math.degrees(frame.body.yaw_radians)
            actual_pitch = math.degrees(frame.body.pitch_radians)
            yaw_change = max(-maximum_yaw_step, min(maximum_yaw_step,
                             _yaw_delta(desired_yaw, actual_yaw)))
            look = LookV1(yaw_change, desired_pitch - actual_pitch)
            request = None
            if decision.missing_cells:
                request, _ = adapter.air_request(decision.missing_cells)
            result = step(decision.movement, look=look,
                          valid_for_ticks=decision.input_lease_ticks,
                          observation_request=request)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
            if record:
                sample(dict(look=look, reason=decision.reason, state=decision.state.value,
                            progress=decision.progress_blocks,
                            control_time_ns=decision.control_time_ns,
                            movement=decision.movement), desired_yaw)
        else:
            final_state, final_reason = FixedRouteState.FAILED, "shape_trial_frame_limit"
        if record and (not samples or samples[-1]["sequence"] != frame.body.sequence_id):
            sample()
        return dict(state=final_state.value, reason=final_reason,
                    samples=samples, decisions=decisions)

    def survey(points: tuple[tuple[float, float, float], ...], name: str) -> None:
        route = _route(name, points)
        _, distance = _lengths(points)
        result = execute(route, points, record=False,
                         maximum_frames=max(300, math.ceil(distance / .05) + 240),
                         gaze=lambda progress: (_tangent_yaw(points, progress), 55.0, 15.0))
        if result["state"] != FixedRouteState.SUCCEEDED.value:
            raise RuntimeError(f"shape survey {name} failed: {result['state']} {result['reason']}")

    diagnostic()
    y = frame.body.position[1]
    origin = tuple(frame.body.position)
    line_end = (origin[0], y, origin[2] + 51.0)
    survey((origin, line_end), "shape-line-survey-out")
    survey((tuple(frame.body.position), origin), "shape-line-survey-back")
    origin = tuple(frame.body.position)
    orient(0.0, 0.0)
    line_points = (origin, (origin[0], y, origin[2] + 51.0))
    line_run = execute(
        _route("shape-line-look", line_points), line_points, record=True, maximum_frames=900,
        gaze=lambda progress: (line_reference_yaw_degrees(progress), 0.0, 4.5),
    )
    line_run["samples"] = [
        {**sample, "tracking_error": abs(sample["position"][0] - origin[0]),
         "expected_yaw": line_reference_yaw_degrees(
             max(0.0, sample["position"][2] - origin[2]))}
        for sample in line_run["samples"]
    ]
    line_run.update(
        case="shape_line_rotating_view", title="平视直行 · 顺逆时针各转一圈",
        kind="line", reference_path=[list(point) for point in line_points],
        start=list(origin), goal=list(line_points[-1]),
        metrics=line_metrics(line_run["samples"], origin),
    )

    circle_origin = tuple(frame.body.position)
    scenarios = [line_run]

    def recenter(name: str) -> None:
        current = tuple(frame.body.position)
        if math.hypot(current[0] - circle_origin[0], current[2] - circle_origin[2]) <= .08:
            return
        survey((current, circle_origin), name)

    for radius in (2.0, 4.0, 8.0):
        for side in ("left", "right"):
            recenter(f"shape-circle-r{int(radius)}-{side}-precenter")
            points = circle_route_points(circle_origin, radius, side)
            survey(points, f"shape-circle-r{int(radius)}-{side}-survey")
            recenter(f"shape-circle-r{int(radius)}-{side}-center")
            orient(0.0, 0.0)
            run = execute(
                _route(f"shape-circle-r{int(radius)}-{side}", points), points,
                record=True, maximum_frames=max(360, len(points) * 8),
                gaze=lambda progress: (0.0, 0.0, 0.0),
            )
            center_x = circle_origin[0] - radius if side == "left" else circle_origin[0] + radius
            run["samples"] = [
                {**sample, "tracking_error": abs(
                    math.hypot(sample["position"][0] - center_x,
                               sample["position"][2] - circle_origin[2]) - radius)}
                for sample in run["samples"]
            ]
            run.update(
                case=f"shape_circle_r{int(radius)}_{side}",
                title=f"固定视角 · {'左' if side == 'left' else '右'}圆 · 半径 {int(radius)} 格",
                kind="circle", radius_blocks=radius, side=side,
                reference_path=[list(point) for point in points],
                start=list(circle_origin), goal=list(circle_origin),
                metrics=circle_metrics(run["samples"], circle_origin, radius, side),
            )
            scenarios.append(run)

    for scenario in scenarios:
        scenario["duration"] = scenario["samples"][-1]["t"] if scenario["samples"] else 0.0
        scenario["layout"] = dict(
            ground_y=math.floor(y - 1), bounds=_bounds(
                tuple(tuple(point) for point in scenario["reference_path"]), scenario["samples"]),
            obstacles=[], pit_cells=[],
        )
        scenario["summary"] = dict(
            outcome="success" if scenario["state"] == FixedRouteState.SUCCEEDED.value else "boundary",
            engineering="valid", path_length=sum(
                math.hypot(second["position"][0] - first["position"][0],
                           second["position"][2] - first["position"][2])
                for first, second in zip(scenario["samples"], scenario["samples"][1:])),
        )
        scenario.update(
            schema_version="mc2p.motion-shape-replay.v1", memory=dict(
                available=False, imports_checked=0, imports_expected=0,
                error=None, retained_in_radius=False),
            knowledge=[], observations=[], entities=[], changes=[], warnings=[],
            navigation_volume=False, current_history_batches=False,
            map_note="理想轨迹只供回放评分，不提供给机器人。正式阶段保持平视。",
        )

    evidence = dict(schema_version="mc2p.b03-shape-trials.v1", scenarios=scenarios)
    write_json_atomic(directory / "b03-shape-trials.json", evidence)
    checks = [
        dict(name="b03_shape_all_seven_scenarios_recorded", passed=len(scenarios) == 7),
        dict(name="b03_shape_line_completed", passed=line_run["state"] == "succeeded"),
        dict(name="b03_shape_pitch_stays_level", passed=all(
            abs(sample["pitch"]) <= .01 for scenario in scenarios for sample in scenario["samples"])),
        dict(name="b03_shape_control_budget", passed=all(
            decision["planning_ms"] < 30 for scenario in scenarios
            for decision in scenario["decisions"])),
    ]
    return evidence, rows, checks
