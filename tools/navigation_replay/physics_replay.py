"""Read-only B09-R actual-versus-predicted replay data."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Iterable, Mapping

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState, TickInput,
)
from mc2p.motion_nav.physics_validation import (
    declared_fixture_validation_world, input_from_tick_evidence,
    ordinary_flat_validation_world, sequence_evidence_rows,
    state_from_tick_evidence,
)
from mc2p.runtime.segmented_trace import iter_segmented_jsonl


_MODES = {
    "b09-air-motion": (
        ("b09-air", "B09 空中转换", None, "declared-commands"),
    ),
    "b08-ground-modes": (
        ("b08-ground", "B08 地面移动", frozenset({"standing", "crouching"}),
         "ordinary-flat"),
        ("b08-crawl", "B08 低顶爬行", frozenset({"swimming"}),
         "low-ceiling-flat"),
    ),
}


def _run_id(directory: Path, mode: str) -> str:
    digest = hashlib.sha256(f"{directory.as_posix()}\0{mode}".encode()).hexdigest()[:16]
    return f"physics-{mode}-{digest}"


def discover(root: Path) -> list[dict]:
    """Find sealed B08/B09 physics evidence without opening its segments."""
    root = Path(root).resolve()
    base = root / "artifacts" / "fabric-deployment"
    found = []
    if not base.is_dir():
        return found
    for directory in sorted(base.iterdir(), key=lambda item: item.name, reverse=True):
        if not directory.is_dir() or directory.is_symlink():
            continue
        result_path = directory / "result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        scenario = result.get("scenario")
        if scenario not in _MODES:
            continue
        evidence = directory / "client-0" / "physics-tick-events"
        complete = (evidence / "complete.json").is_file()
        fixture = directory / "client-0" / "b09-fixture-commands.jsonl"
        for mode, title, poses, fixture_kind in _MODES[scenario]:
            available = complete and (fixture_kind != "declared-commands" or fixture.is_file())
            found.append({
                "id": _run_id(directory.relative_to(root), mode),
                "mode": mode,
                "title": title,
                "scenario": scenario,
                "status": result.get("status", "unknown"),
                "seed": result.get("seed"),
                "complete": available,
                "directory": directory.relative_to(root).as_posix(),
                "evidence": evidence.relative_to(root).as_posix(),
                "fixture_commands": (
                    fixture.relative_to(root).as_posix() if fixture.is_file() else None),
                "poses": sorted(poses) if poses else [],
                "fixture": fixture_kind,
            })
    return found


def _state_payload(state: PhysicsState) -> dict:
    return {
        "movement_tick_id": state.movement_tick_id,
        "position": list(state.position),
        "velocity": list(state.velocity_blocks_per_tick),
        "yaw_degrees": math.degrees(state.yaw_radians),
        "pitch_degrees": math.degrees(state.pitch_radians),
        "pose": state.pose,
        "body_width": state.body_width,
        "body_height": state.body_height,
        "on_ground": state.on_ground,
        "horizontal_collision": state.horizontal_collision,
        "vertical_collision": state.vertical_collision,
        "sprinting": state.sprinting,
        "sneaking": state.sneaking,
    }


def _actual_payload(value: Mapping, tick_id: int) -> dict:
    pose = str(value["pose"])
    return {
        "movement_tick_id": tick_id,
        "position": [float(value["position"][axis]) for axis in ("x", "y", "z")],
        "velocity": [float(value["velocity"][axis]) for axis in ("x", "y", "z")],
        "yaw_degrees": float(value["yaw"]),
        "pitch_degrees": float(value["pitch"]),
        "pose": pose,
        "body_width": .6,
        "body_height": {"standing": 1.8, "crouching": 1.5, "swimming": .6}[pose],
        "on_ground": bool(value["on_ground"]),
        "horizontal_collision": bool(value["horizontal_collision"]),
        "vertical_collision": bool(value["vertical_collision"]),
        "sprinting": bool(value["actual_sprinting"]),
        "sneaking": bool(value["actual_sneaking"]),
    }


def _input_payload(value: TickInput) -> dict:
    return {
        "forward": value.forward, "strafe": value.strafe,
        "jump": value.jump, "sneak": value.sneak, "sprint": value.sprint,
        "movement_yaw_degrees": math.degrees(value.movement_yaw_radians),
    }


def _error(actual: Mapping, predicted: Mapping) -> dict:
    position = [predicted["position"][i] - actual["position"][i] for i in range(3)]
    velocity = [predicted["velocity"][i] - actual["velocity"][i] for i in range(3)]
    return {
        "position_axes": position, "velocity_axes": velocity,
        "position_max": max(map(abs, position)),
        "velocity_max": max(map(abs, velocity)),
    }


def build_payload(rows: Iterable[Mapping], world, *, title: str, source: str,
                  world_payload: Mapping) -> dict:
    """Run open-loop B09-R prediction and retain enough detail for animation."""
    rows = tuple(rows)
    groups, breaks = sequence_evidence_rows(rows)
    break_by_index = {index: reasons for index, reasons in breaks}
    absolute_index = 0
    sequences = []
    position_max = velocity_max = 0.0
    incomplete = 0
    for sequence_index, group in enumerate(groups):
        reason = ("recording_started",) if sequence_index == 0 else break_by_index[absolute_index]
        current = state_from_tick_evidence(group[0], world, JAVA_1_21_RULESET)
        initial = _state_payload(current)
        samples = [{
            "t": 0.0, "tick": current.movement_tick_id,
            "actual": initial, "predicted": initial, "input": None,
            "actual_events": [], "predicted_events": [],
            "error": {"position_axes": [0., 0., 0.], "velocity_axes": [0., 0., 0.],
                      "position_max": 0., "velocity_max": 0.},
            "status": "ok",
        }]
        for offset, row in enumerate(group, 1):
            tick_input = input_from_tick_evidence(row)
            calculated = step(current, tick_input, world, JAVA_1_21_RULESET)
            actual = _actual_payload(row["post_state"], int(row["movement_tick_id"]))
            if calculated.status is CalculationStatus.OK and calculated.next_state is not None:
                current = calculated.next_state
                predicted = _state_payload(current)
                error = _error(actual, predicted)
                position_max = max(position_max, error["position_max"])
                velocity_max = max(velocity_max, error["velocity_max"])
                predicted_events = list(calculated.events)
                status = "ok"
                detail = []
            else:
                incomplete += 1
                predicted = _state_payload(current)
                error = _error(actual, predicted)
                predicted_events = []
                status = calculated.status.value
                detail = (list(calculated.invalid_reasons)
                          or list(calculated.unsupported_reasons)
                          or [str(cell) for cell in calculated.missing_cells])
            samples.append({
                "t": offset * JAVA_1_21_RULESET.tick_seconds,
                "tick": int(row["movement_tick_id"]),
                "actual": actual, "predicted": predicted,
                "input": _input_payload(tick_input),
                "actual_events": list(row.get("contact_events", ())),
                "predicted_events": predicted_events,
                "error": error, "status": status, "detail": detail,
            })
            if status != "ok":
                break
        sequences.append({
            "id": sequence_index, "title": f"连续段 {sequence_index + 1}",
            "tick_count": len(samples) - 1,
            "duration": (len(samples) - 1) * JAVA_1_21_RULESET.tick_seconds,
            "break_reasons": list(reason), "samples": samples,
        })
        absolute_index += len(group)
    return {
        "schema_version": "mc2p.physics-replay.v1",
        "title": title, "source": source,
        "ruleset_id": JAVA_1_21_RULESET.ruleset_id,
        "tick_seconds": JAVA_1_21_RULESET.tick_seconds,
        "world": dict(world_payload),
        "summary": {
            "tick_count": len(rows), "sequence_count": len(sequences),
            "incomplete_tick_count": incomplete,
            "position_error_max": position_max, "velocity_error_max": velocity_max,
        },
        "sequences": sequences,
    }


def _flat_world_payload(rows: tuple[Mapping, ...], low_ceiling: bool) -> dict:
    positions = [
        (float(state["position"]["x"]), float(state["position"]["y"]),
         float(state["position"]["z"]))
        for row in rows for state in (row["pre_state"], row["post_state"])
    ]
    floor_y = math.floor(positions[0][1] - .500001)
    return {
        "kind": "low-ceiling-flat" if low_ceiling else "ordinary-flat",
        "floor_y": floor_y,
        "ceiling_y": floor_y + 2 if low_ceiling else None,
        "blocks": [],
        "bounds": {
            "min_x": math.floor(min(p[0] for p in positions)) - 1,
            "max_x": math.floor(max(p[0] for p in positions)) + 1,
            "min_z": math.floor(min(p[2] for p in positions)) - 1,
            "max_z": math.floor(max(p[2] for p in positions)) + 1,
        },
    }


def _declared_world_payload(path: Path) -> dict:
    blocks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        command = json.loads(line)
        if command["material"] != "minecraft:air":
            blocks.extend({"position": item, "material": command["material"]}
                          for item in command["positions"])
    return {"kind": "declared-commands", "blocks": blocks}


class PhysicsReplayStore:
    def __init__(self, root: Path, cache: Path) -> None:
        self.root = Path(root).resolve()
        self.cache = Path(cache)
        self.runs: dict[str, dict] = {}

    def catalog(self, force: bool = False) -> list[dict]:
        # Discovery is cheap and prevents this secondary viewer from owning timers/threads.
        runs = discover(self.root)
        self.runs = {item["id"]: item for item in runs}
        return runs

    def get(self, run_id: str) -> dict | None:
        self.catalog()
        run = self.runs.get(run_id)
        if run is None:
            return None
        if not run["complete"]:
            return {"status": "error", "message": "物理证据尚未完整封存"}
        evidence = self.root / run["evidence"]
        fixture = self.root / run["fixture_commands"] if run["fixture_commands"] else None
        inputs = [
            evidence / "complete.json", evidence / "manifest.jsonl", Path(__file__),
            self.root / "mc2p/motion_nav/physics_1_21.py",
            self.root / "mc2p/motion_nav/physics_adapter.py",
            self.root / "mc2p/motion_nav/physics_types.py",
            self.root / "mc2p/motion_nav/physics_validation.py",
            self.root / "mc2p/motion_nav/geometry.py",
        ]
        if fixture is not None:
            inputs.append(fixture)
        digest = hashlib.sha256()
        for path in inputs:
            digest.update(path.read_bytes())
        output = self.cache / f"{run_id}-{digest.hexdigest()[:16]}.json"
        if output.exists():
            return json.loads(output.read_text(encoding="utf-8"))
        rows = tuple(iter_segmented_jsonl(evidence))
        poses = frozenset(run["poses"])
        if poses:
            rows = tuple(row for row in rows if row.get("pre_state", {}).get("pose") in poses)
        if run["fixture"] == "declared-commands":
            world = declared_fixture_validation_world(fixture, JAVA_1_21_RULESET)
            world_payload = _declared_world_payload(fixture)
        else:
            low_ceiling = run["fixture"] == "low-ceiling-flat"
            world = ordinary_flat_validation_world(
                rows, JAVA_1_21_RULESET, low_ceiling=low_ceiling)
            world_payload = _flat_world_payload(rows, low_ceiling)
        payload = build_payload(
            rows, world, title=run["title"], source=run["directory"],
            world_payload=world_payload,
        )
        self.cache.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".pending")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                             encoding="utf-8")
        temporary.replace(output)
        return payload
