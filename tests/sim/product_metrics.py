"""Read-only R28 metrics shared by all compared versions.

Only emitted planner submissions, observed body positions and body owners are
counted. Retry counters and reason strings deliberately have no consumers here.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping
from enum import StrEnum

EXTRACTOR_VERSION = "mc2p.navigation-product-metrics.v3"
TERMINAL = {"success", "failed", "cancelled", "stopped", "interaction_required"}


class RevisionResponseMode(StrEnum):
    MOVEMENT_ONLY = "movement_only"
    MOVEMENT_OR_MATCHING_SATISFACTION = "movement_or_matching_satisfaction"


def revision_responses(frames: Iterable[Mapping], *, start_tick: int,
                       start_position: tuple[float, float, float],
                       mode: RevisionResponseMode = (
                           RevisionResponseMode.MOVEMENT_ONLY
                       )) -> list[dict]:
    """Measure one current revision request against its submitted target.

    A newer accepted revision supersedes the pending one.  The default R28
    ruler accepts only later physical movement toward the submitted target.
    Callers may explicitly accept a continuous, matching formal satisfaction.
    """
    if type(mode) is not RevisionResponseMode:
        raise TypeError("revision response mode must be typed")
    current_revision = None
    pending = None
    responses = []
    last_tick, last_position = start_tick, start_position
    terminal = False
    for row in frames:
        tick, position = row.get("movement_tick"), row.get("position")
        if (type(tick) is not int or not isinstance(position, (list, tuple))
                or len(position) != 3
                or not all(math.isfinite(value) for value in position)):
            continue
        if tick <= last_tick:
            continue
        consecutive = tick == last_tick + 1
        if pending is not None and not consecutive:
            pending["continuous"] = False
        if current_revision is None:
            current_revision = row.get("goal_revision")
        for request in row.get("goal_revision_requests", ()):
            if pending is not None:
                responses.append({
                    "revision": pending["revision"],
                    "response_ticks": None,
                    "end": "superseded",
                })
            current_revision = request["revision"]
            target_position = row.get("goal_position")
            pending = {
                "revision": current_revision,
                "movement_tick": request["movement_tick"],
                "continuous": True,
                "target_position": (
                    tuple(target_position)
                    if isinstance(target_position, (list, tuple))
                    else target_position
                ),
            }
        distance = math.hypot(
            position[0] - last_position[0], position[2] - last_position[2],
        )
        applied = row.get("applied_movement", {})
        demand = (row.get("source_bound")
                  and not row.get("goal_satisfied", False)
                  and not terminal)
        terminal |= row.get("driver_state") in TERMINAL
        if (pending is not None and demand and consecutive and distance > .005
                and (applied.get("forward", 0) or applied.get("strafe", 0))):
            target = pending["target_position"]
            if (target is not None
                    and math.dist(position, target)
                    < math.dist(last_position, target)):
                responses.append({
                    "revision": pending["revision"],
                    "response_ticks": tick - pending["movement_tick"],
                    "end": "movement",
                })
                pending = None
        if (pending is not None
                and mode is (
                    RevisionResponseMode.MOVEMENT_OR_MATCHING_SATISFACTION
                )
                and pending["continuous"] and consecutive
                and tick > pending["movement_tick"]
                and row.get("goal_revision") == pending["revision"]
                and row.get("goal_satisfied") is True):
            responses.append({
                "revision": pending["revision"],
                "response_ticks": tick - pending["movement_tick"],
                "end": "satisfied",
            })
            pending = None
        last_tick, last_position = tick, position
    if pending is not None:
        responses.append({
            "revision": pending["revision"],
            "response_ticks": None,
            "end": "unanswered",
        })
    return responses


def window_stalls(frames: Iterable[Mapping], *, window_ticks: int = 10,
                  minimum_displacement: float = .1) -> list[dict]:
    """Count little net movement, including oscillation, on consecutive ticks.

    This is an external measurement, never a reason to alter robot control.
    Preparation is reported separately using observed owners and action types.
    """
    history = deque(maxlen=window_ticks + 1)
    intervals, active = [], None
    terminal, last_tick = False, None

    def finish():
        nonlocal active
        if active is not None and active["end_tick"] - active["start_tick"] >= 2:
            intervals.append(active)
        active = None

    for row in frames:
        tick, position = row.get("movement_tick"), row.get("position")
        if (type(tick) is not int or not isinstance(position, (list, tuple))
                or len(position) != 3 or not all(math.isfinite(v) for v in position)):
            history.clear()
            finish()
            continue
        if last_tick is not None and tick != last_tick + 1:
            history.clear()
            finish()
        last_tick = tick
        terminal |= row.get("driver_state") in TERMINAL
        demand = row.get("source_bound") and not row.get("goal_satisfied") and not terminal
        if not demand:
            history.clear()
            finish()
            continue
        history.append(position)
        activities = row.get("body_control_activities")
        phase = (activities[0]["phase"] if activities else None)
        stalled = len(history) == window_ticks + 1 and (
            math.dist(position, history[0]) if phase == "strict_execution" else
            math.hypot(position[0] - history[0][0], position[2] - history[0][2])
        ) < minimum_displacement
        owners = row.get("controller_ids", ())
        kind = row.get("action_kind")
        strict_action = kind in {"jump_gap", "controlled_drop", "jump_up", "step",
                                 "JumpGapSegment", "ControlledDropSegment", "JumpUpSegment", "StepSegment"}
        category = ({"tracking": "walking", "acquisition": "strict_preparation",
                     "strict_preparation": "strict_preparation", "strict_execution": "strict_execution",
                     "entry_recovery": "entry_recovery", "stopping": "stopping"}.get(phase)
                    or ("strict_execution" if strict_action and row.get("on_ground") is False
                    else "strict_preparation" if "landing_edge_probe" in owners or strict_action
                    else "planning_wait" if not owners and row.get("planning_work_owned")
                    else "walking" if kind in {"WalkSegment", "walk"}
                    else "unclassified"))
        if stalled:
            if active is None or active["category"] != category:
                finish()
                active = {"start_tick": tick, "end_tick": tick, "category": category}
            else:
                active["end_tick"] = tick
        else:
            finish()
    finish()
    return intervals


def extract_metrics(frames: Iterable[Mapping], *, start_tick: int,
                    start_position: tuple[float, float, float], outcome: str,
                    violations: Iterable = ()) -> dict:
    frames = list(frames)
    net_stalls = window_stalls(frames)
    gaps = set()
    last_tick, last_position = start_tick, start_position
    last_owner = None
    first_move = terminal_tick = release_tick = None
    pauses, pause_start, pause_last = [], None, None
    requests = switches = acquisitions = releases = 0
    revision = None

    def finish_pause():
        nonlocal pause_start, pause_last
        if pause_start is not None and pause_last - pause_start + 1 >= 3:
            pauses.append([pause_start, pause_last])
        pause_start = pause_last = None

    count = 0
    for row in frames:
        count += 1
        for field in ("movement_tick", "position", "driver_state", "source_bound",
                      "controller_ids", "route_id", "planning_submissions",
                      "goal_revision", "goal_revision_requests", "goal_position", "goal_satisfied", "applied_movement"):
            if field not in row:
                gaps.add(f"{field}_missing")
        tick, position = row.get("movement_tick"), row.get("position")
        if (type(tick) is not int or not isinstance(position, (list, tuple))
                or len(position) != 3 or not all(math.isfinite(v) for v in position)):
            gaps.add("invalid_body_sample")
            finish_pause()
            continue
        if tick <= last_tick:
            gaps.add("movement_tick_not_increasing")
            finish_pause()
            continue
        consecutive = tick == last_tick + 1
        if not consecutive:
            gaps.add("movement_tick_gap")
            finish_pause()
        distance = math.hypot(position[0] - last_position[0], position[2] - last_position[2])
        applied = row.get("applied_movement", {})
        demand = row.get("source_bound") and not row.get("goal_satisfied", False) and terminal_tick is None
        if terminal_tick is None and row.get("driver_state") in TERMINAL:
            terminal_tick = tick
        if revision is None:
            revision = row.get("goal_revision")
        for request in row.get("goal_revision_requests", ()):
            revision = request["revision"]
        if row.get("goal_revision") != revision:
            gaps.add("goal_revision_request_not_recorded_or_not_accepted")
        if demand and consecutive and distance > .005 and (
                applied.get("forward", 0) or applied.get("strafe", 0)):
            if first_move is None:
                first_move = tick
        if demand and consecutive and distance <= .005:
            if pause_start is None:
                pause_start = tick
            pause_last = tick
        else:
            finish_pause()
        requests += len(row.get("planning_submissions", ()))
        owners = tuple((owner, row.get("route_id") if owner == "route_executor" else None)
                       for owner in row.get("controller_ids", ()))
        if owners != last_owner:
            if owners:
                acquisitions += 1
                if last_owner:
                    switches += 1
            if last_owner:
                releases += 1
            last_owner = owners
        if terminal_tick is not None and release_tick is None and not row.get("source_bound") and not owners:
            release_tick = tick
        last_tick, last_position = tick, position
    finish_pause()
    responses = revision_responses(
        frames, start_tick=start_tick, start_position=start_position,
    )
    if not count:
        gaps.add("empty_trace")
    if terminal_tick is None:
        gaps.add("task_terminal_not_observed")
    if release_tick is None:
        gaps.add("body_release_not_observed")
    safety = list(violations)
    return {
        "schema_version": EXTRACTOR_VERSION,
        "outcome": outcome, "success": outcome == "success",
        "evidence_complete": not gaps, "coverage_gaps": sorted(gaps),
        "safety_events": safety,
        "planning_requests": requests, "controller_switches": switches,
        "controller_acquisitions": acquisitions, "controller_releases": releases,
        "zero_displacement_intervals": pauses,
        "zero_displacement_ticks": sum(end - begin + 1 for begin, end in pauses),
        "net_stall_intervals": net_stalls,
        "net_stall_ticks": {category: sum(
            item["end_tick"] - item["start_tick"] + 1
            for item in net_stalls if item["category"] == category
        ) for category in ("walking", "strict_preparation", "strict_execution", "planning_wait", "entry_recovery", "stopping", "unclassified")},
        "first_movement_ticks": None if first_move is None else first_move - start_tick,
        "arrival_ticks": release_tick - start_tick if outcome == "success" and release_tick is not None else None,
        "terminal_ticks": None if terminal_tick is None else terminal_tick - start_tick,
        "body_tail_ticks": None if release_tick is None or terminal_tick is None else release_tick - terminal_tick,
        "revision_responses": responses,
        "observed_ticks": last_tick - start_tick,
    }


def compare_metrics(baseline: Mapping, candidate: Mapping, *, tick_tolerance: int) -> dict:
    if baseline.get("schema_version") != candidate.get("schema_version") or not (
            baseline.get("evidence_complete") and candidate.get("evidence_complete")):
        return {"status": "insufficient_evidence", "differences": []}
    exact = ("outcome", "safety_events", "planning_requests", "controller_switches",
             "controller_acquisitions", "controller_releases", "body_tail_ticks")
    differences = [key for key in exact if baseline[key] != candidate[key]]
    if len(baseline["zero_displacement_intervals"]) != len(candidate["zero_displacement_intervals"]):
        differences.append("zero_displacement_interval_count")
    if any(abs(baseline["net_stall_ticks"].get(k,0)-candidate["net_stall_ticks"].get(k,0)) >
           (tick_tolerance if k == "walking" else 0)
           for k in baseline["net_stall_ticks"].keys() | candidate["net_stall_ticks"].keys()):
        differences.append("net_stall_ticks")
    for key in ("arrival_ticks", "terminal_ticks", "first_movement_ticks", "zero_displacement_ticks"):
        old, new = baseline[key], candidate[key]
        if (old is None) != (new is None) or old is not None and abs(new - old) > tick_tolerance:
            differences.append(key)
    old_responses, new_responses = baseline["revision_responses"], candidate["revision_responses"]
    if len(old_responses) != len(new_responses) or any(
        old["revision"] != new["revision"] or old["end"] != new["end"] or
        (old["response_ticks"] is None) != (new["response_ticks"] is None) or
        old["response_ticks"] is not None and abs(old["response_ticks"] - new["response_ticks"]) > tick_tolerance
        for old, new in zip(old_responses, new_responses)
    ):
        differences.append("revision_responses")
    return {"status": "different" if differences else "equivalent", "differences": differences}


def strict_trace(frames: Iterable[Mapping]) -> list[dict]:
    """Keep input windows, physical samples and release; normalize random IDs only."""
    route_ids = {}
    action_ids = {}
    result = []
    fields = ("movement_tick", "position", "velocity", "on_ground", "pose",
              "source_bound", "controller_ids", "applied_movement", "input_window")
    for row in frames:
        if any(key not in row for key in (*fields, "route_id", "risk_actions")):
            raise ValueError("strict trace lacks required input/body/risk evidence")
        value = {key: row[key] for key in fields}
        route = row["route_id"]
        value["route_id"] = None if route is None else route_ids.setdefault(route, len(route_ids))
        value["risk_actions"] = [dict(
            action, action_id=action_ids.setdefault(action["action_id"], len(action_ids)),
            # Control sequences remain real; no blanket UUID/tick rewriting.
        ) for action in row["risk_actions"]]
        result.append(value)
    return result


def tango_score(b: int, c: int, n: int, theta: float) -> float:
    """Score for theta = baseline success probability - candidate probability."""
    if any(type(v) is not int or v < 0 for v in (b, c, n)) or n < 1 or b + c > n or not -1 < theta < 1:
        raise ValueError("invalid paired counts or probability difference")
    m = n - b - c
    big_b = b * (1 - theta) + c * (1 - 3 * theta) - 2 * m * theta
    p21 = (big_b + math.sqrt(max(0, big_b * big_b + 8 * n * c * theta * (1 - theta)))) / (4 * n)
    variance = n * max(0, 2 * p21 + theta - theta * theta)
    numerator = b - c - n * theta
    if variance < 1e-20:
        return 0.0 if abs(numerator) < 1e-12 else math.copysign(math.inf, numerator)
    return numerator / math.sqrt(variance)


def tango_interval(b: int, c: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    tango_score(b, c, n, 0)
    if not math.isfinite(z) or z <= 0:
        raise ValueError("invalid score boundary")
    estimate = (b - c) / n

    def root(lo, hi, target):
        for _ in range(65):
            mid = (lo + hi) / 2
            if tango_score(b, c, n, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    return (-root(estimate, 1 - 1e-12, -z), -root(-1 + 1e-12, estimate, z))


def sequential_verdict(b: int, c: int, n: int, *, first_look: int, margin: float = .01) -> dict:
    if n not in (first_look, 2 * first_look):
        raise ValueError("only predeclared first and second looks may decide")
    lower, upper = tango_interval(b, c, n, z=2.178)
    status = "pass" if lower >= -margin else "regression" if upper < -margin else "inconclusive"
    return {"status": status, "interval": [lower, upper], "n": n,
            "may_append": n == first_look and status == "inconclusive"}
