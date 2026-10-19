"""Current Fabric runtime-stage evidence checks without legacy backend imports."""
from __future__ import annotations

import math


STAGES = (
    "start", "moving", "released", "open", "open_again", "closed",
    "closed_again", "reopened", "stale_rejected", "recovered", "hotbar",
    "no_repeat", "cancelled",
)


def evaluate_stages(stages: dict) -> list[dict]:
    checks = {"complete_ordered_stages": set(stages) == set(STAGES)}
    if not checks["complete_ordered_stages"]:
        return [{"name": name, "passed": passed} for name, passed in checks.items()]
    values = stages
    sequences = [values[name]["sequence"] for name in STAGES]
    checks.update({
        "complete_ordered_stages": all(
            a < b for a, b in zip(sequences, sequences[1:])
        ),
        "movement_has_real_effect": math.hypot(
            values["moving"]["x"] - values["start"]["x"],
            values["moving"]["z"] - values["start"]["z"],
        ) > .1,
        "look_delta_applied_once": abs(
            (values["moving"]["yaw"] - values["start"]["yaw"] + 180)
            % 360 - 180 - 12
        ) < 1e-5,
        "cancelled_camera_never_replays": all(
            abs(
                (values[name]["yaw"] - values["moving"]["yaw"] + 180)
                % 360 - 180
            ) < 1e-5
            for name in STAGES[2:]
        ),
        "source_cancel_releases_motion": values["released"]["speed"] < .01
        and values["released"]["speed"] < values["moving"]["speed"],
        "inventory_repeated_open_close": [
            values[name]["gui_open"]
            for name in ("open", "open_again", "closed", "closed_again", "reopened")
        ] == [True, True, False, False, True]
        and values["open"]["gui_session"] == values["open_again"]["gui_session"]
        and values["open"]["gui_session"] != values["reopened"]["gui_session"],
        "stale_request_rejected_then_recovered":
            values["stale_rejected"]["receipt_status"] == "rejected"
            and values["stale_rejected"]["reason"] == "stale_gui_session"
            and values["stale_rejected"]["status"] == "failed"
            and values["stale_rejected"]["gui_open"]
            and values["stale_rejected"]["gui_session"]
            == values["reopened"]["gui_session"]
            and not values["recovered"]["gui_open"]
            and values["recovered"]["status"] == "running",
        "hotbar_pending_is_not_task_success": values["hotbar"]["hotbar"] == 2
            and values["hotbar"]["receipt_status"] == "pending_confirmation"
            and values["hotbar"]["status"] == "running"
            and values["no_repeat"]["operation"] is None
            and values["no_repeat"]["hotbar"] == 2,
        "final_cancel_locally_accepted": values["cancelled"]["status"] == "cancelled"
            and values["cancelled"]["receipt_status"]
            in {"executed", "confirmed_local", "cancelled"}
            and values["cancelled"]["speed"] < .01,
    })
    return [
        {"name": name, "passed": bool(passed)}
        for name, passed in checks.items()
    ]
