"""Post-run metrics. Privileged two-client positions are evaluator-only, not actor inputs."""
from __future__ import annotations

import math
from pathlib import Path

from scripts.client_time_evidence import _read_jsonl, export_time_evidence
from scripts.control_probe_core import write_json_atomic
from scripts.smoke_test_player_runtime import _formal_observation_violations
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3


def evaluate_follow(records: list[dict], phases: dict, leader_evidence: list[dict]) -> list[dict]:
    del leader_evidence  # Caller already joins evaluator-only leader samples into scheduled rows.
    try:
        case, duration, interval = phases["case"], phases["duration_ns"], phases["interval_ns"]
        if case not in {"static", "moving", "obstacle", "hazard"}:
            return [dict(name="implemented_case", passed=False, reason="case_not_yet_verified")]
        if type(duration) is not int or type(interval) is not int or not 0 < interval <= 200_000_000 or duration < 10_000_000_000:
            raise ValueError("invalid predeclared sampling phase")
        count = math.ceil(duration/interval)
        slots = [r["slot"] for r in records]
        if any(type(s) is not int or not 0 <= s < count for s in slots) or len(set(slots)) != len(slots) or slots != sorted(slots):
            raise ValueError("duplicate/out-of-order/out-of-phase sample")
        rows = {row["slot"]: row for row in records}
        valid = {}
        for slot, row in rows.items():
            distance = row["distance"]
            if distance is not None and (type(distance) not in (int, float) or not math.isfinite(distance) or distance < 0):
                raise ValueError("invalid observed distance")
            valid[slot] = row["target_visible"] is True and distance is not None
        checks = dict(scheduled_sample_coverage=len(rows)/count >= .9,
                      actual_initial_sample=0 in rows,
                      no_collision=all(row["collision"] is False for row in rows.values()))
        if not rows: raise ValueError("no measured samples")
        first, last = records[0], records[-1]
        moved = lambda key: math.dist(first[key], last[key]) >= .5
        if case in {"static", "obstacle"}:
            checks["initial_distance_6_to_8"] = valid.get(0, False) and 6 <= first["distance"] <= 8
            checks["actual_follower_approach"] = moved("follower")
            hold = 0
            reached = False
            for slot in range(count):
                row = rows.get(slot)
                if valid.get(slot, False) and 2 <= row["distance"] <= 4:
                    hold += 1
                    if hold*interval >= 2_000_000_000 and (slot-hold+1)*interval <= 15_000_000_000:
                        reached = True
                else:
                    hold = 0
            checks["approach_then_contiguous_two_second_hold"] = reached
            if case == "obstacle":
                barrier = phases["barrier"]
                bx, z_min, z_max = barrier["x"], barrier["z_min"], barrier["z_max"]
                checks["actual_lateral_detour_past_front"] = (max(abs(r["follower"][0]-first["follower"][0]) for r in records) >= .85
                    and last["follower"][2] >= z_min+1.25 and valid[slots[-1]] and 1.5 <= last["distance"] <= 4)
                checks["body_did_not_cross_known_barrier"] = all(
                    not (z_min-.35 < r["follower"][2] < z_max+.35 and bx-.35 < r["follower"][0] < bx+1.35)
                    for r in records)
                checks["walked_same_height_not_jumped_over"] = all(abs(r["follower"][1]-first["follower"][1]) <= .1 for r in records)
        elif case == "hazard":
            checks["close_target_requires_considering_retreat"] = valid.get(0,False) and .8 <= first["distance"] < 1.5
            checks["no_blind_backward_motion"] = all(math.dist(first["follower"],row["follower"]) <= .1 for row in records)
            checks["explicit_unsafe_retreat_refusal"] = all(row["state"] == "blocked" and row["reason"] == "unsafe_retreat"
                                                         for row in records if row["slot"] != 0)
        else:
            checks["both_players_actually_moved"] = moved("follower") and moved("leader")
            checks["at_least_90_percent_all_scheduled_slots_visible_in_band"] = sum(
                valid.get(slot, False) and 1.5 <= rows[slot]["distance"] <= 5 for slot in range(count))/count >= .9
            checks["no_close_collision_distance"] = all(row["distance"] is None or row["distance"] >= .8 for row in rows.values())
        return [dict(name=name, passed=bool(value)) for name, value in checks.items()]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        return [dict(name="well_formed_follow_evidence", passed=False, detail=str(error))]


def evaluate_follow_runtime(records: list[dict], diagnostics: list[dict], *, server_port: int) -> list[dict]:
    try:
        resets = [r["payload"]["result"]["observation"] for r in records if r["record_type"] == "reset"]
        steps = [r["payload"] for r in records if r["record_type"] == "step"]
        closes = [r["payload"] for r in records if r["record_type"] == "close_release"]
        observations = resets + [s["backend_result"]["observation"] for s in (*steps, *closes)]
        checks = dict(
            complete_v3=len(resets) == 1 and bool(steps) and not validate_formal_observations_v3(observations)
                and [o["sequence_id"] for o in observations] == list(range(len(observations))),
            formal_zero_image=not _formal_observation_violations(records, path="trace"),
            associated_direct_actions=all(s["decision"]["action"]["schema_version"] == "mc2p.action-snapshot.v1"
                and s["decision"]["action"]["request_sequence_id"] == i == s["backend_result"]["receipt"]["request_sequence_id"]
                and s["backend_result"]["receipt"]["generation_id"] == i+1 == observations[i+1]["sequence_id"]
                and s["backend_result"]["receipt"]["episode_id"] == observations[i+1]["episode_id"]
                and s["backend_result"]["receipt"]["status"] in {"executed", "confirmed_local", "cancelled"}
                and s["backend_result"]["receipt"]["on_client_thread"] is True
                and s["backend_result"]["receipt"]["action_keyboard_callbacks"] == s["backend_result"]["receipt"]["action_mouse_callbacks"] == 0
                for i,s in enumerate(steps)),
            zero_image_hidden_diagnostics=len(diagnostics) == len(observations) and all(
                row["observation_sequence_id"] == i and row["episode_id"] == observations[i]["episode_id"]
                and row["diagnostics"]["remote_address"] == f"127.0.0.1:{server_port}"
                and row["diagnostics"]["has_integrated_server"] is False
                and row["diagnostics"]["window_recorded"] is True
                and row["diagnostics"]["window_visible"] is row["diagnostics"]["window_visible_at_creation"] is False
                and all(type(row["diagnostics"][k]) is int and row["diagnostics"][k] == 0 for k in
                        ("framebuffer_capture_attempts", "image_encode_attempts", "world_render_completions", "gui_render_completions"))
                for i,row in enumerate(diagnostics)),
            raw_client_samples_monotonic=all(a["client_sample"]["clock_id"] == b["client_sample"]["clock_id"]
                and a["client_sample"]["completed_at_monotonic_ns"] <= b["client_sample"]["started_at_monotonic_ns"]
                for a,b in zip(observations, observations[1:])),
        )
        return [dict(name=name, passed=bool(value)) for name,value in checks.items()]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        return [dict(name="well_formed_follow_runtime", passed=False, detail=str(error))]


def export_follow_time_evidence(directory: Path) -> dict:
    """Include the real close_release sample without fabricating a Runtime step."""
    try:
        records = _read_jsonl(directory/"trace.jsonl")
        rows = _read_jsonl(directory/"diagnostics.jsonl")
        observations = [r["payload"]["result"]["observation"] if r["record_type"] == "reset"
                        else r["payload"]["backend_result"]["observation"] for r in records
                        if r["record_type"] in {"reset", "step", "close_release"}]
        result = export_time_evidence(directory/"mc2p-client-time.jsonl", 0, directory, observations=observations)
        if result["status"] == "passed":
            if (len(rows) != len(observations) or len(result["intervals"]) != len(observations)-1
                    or not all(o["source_backend"] == "fabric" and o["sequence_id"] == i
                               and row["observation_sequence_id"] == i and row["episode_id"] == o["episode_id"]
                               for i, (o,row) in enumerate(zip(observations,rows)))):
                raise ValueError("follow timing does not cover every reset/step/close sample")
            if not all(interval["client_tick_calls"] == b["diagnostics"]["client_tick"]-a["diagnostics"]["client_tick"]
                       for interval,a,b in zip(result["intervals"],rows,rows[1:])):
                raise ValueError("follow timing differs from actual client counters")
    except (OSError, KeyError, IndexError, TypeError, ValueError) as error:
        result = dict(schema_version="mc2p.client-time-evidence.v1", status="failed", errors=[str(error)])
    write_json_atomic(directory/"time-report.json", result)
    return result
