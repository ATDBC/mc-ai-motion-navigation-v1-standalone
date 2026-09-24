"""Build and reverify isolated N0/N1 saves; this is never an actor capability."""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory

from scripts.control_probe_core import write_json_atomic
from scripts.fabric_deployment_sandbox import JAVA
from scripts.follow_fixture_world import offline_uuid
from scripts.visibility_fixture_world import ROOT, _classpath, _hash, _no_links, _run


JOINT_CASES = frozenset({"terrain_removed", "terrain_wall_added", "terrain_entity", "side_information", "corner_prelook", "narrow_clearance",
                         "reposition_view", "irrelevant_side", "staggered_walls"})
CASES = frozenset({"empty", "entrance", "pillar", "pillars", "pit", "two_routes"}) | JOINT_CASES
SEEDS = frozenset({21001, 21002, 21003})
SOURCES = (
    "scripts/normal_navigation_fixture.py",
    "scripts/java/NormalNavigationInitializer.java",
    "scripts/fixtures/visibility-level.snbt",
    "tests/java/VanillaObjectTestHost.java",
)
PLAYER = "MC2PFollower"
PLAYER_FILE = f"playerdata/{offline_uuid(PLAYER)}.dat"
PLAYER_INITIALIZATION = "level_host_and_offline_uuid_canonical_v1"
WORLD_FILES = frozenset({"level.dat", "region/r.0.0.mca", PLAYER_FILE})
BASE_LAYERS = (
    ("minecraft:bedrock", -64),
    ("minecraft:dirt", -63),
    ("minecraft:dirt", -62),
    ("minecraft:grass_block", -61),
)


def _base_layers() -> list[dict]:
    return [{"block": block, "y": y} for block, y in BASE_LAYERS]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sources() -> dict[str, str]:
    return {name: _hash(ROOT / name) for name in SOURCES}


def _obstacles(points: list[tuple[int, int]]) -> list[dict]:
    return [
        {"block": "minecraft:stone", "x": x, "y": y, "z": z}
        for x, z in sorted(points)
        for y in range(-60, -57)
    ]


def _joint_case(case: str, seed: int) -> tuple[list[tuple[int, int]], list[float], list[float], dict]:
    """Frozen N1 geometry and evaluator-only landmarks; no sensor or policy inputs."""
    relevant, irrelevant, checks = [], [], []
    start, goal = [7.5, -60.0, 2.5], [7.5, -60.0, 12.5]

    def landmark(x: int, y: int, z: int, role: str) -> dict:
        return {"x": x, "y": y, "z": z, "role": role}

    def region(x0: float, x1: float, z0: float, z1: float) -> dict:
        return {"min_x": x0, "max_x": x1, "min_z": z0, "max_z": z1}

    if case in ('terrain_removed','terrain_wall_added','terrain_entity'):
        points=[];decision=region(6.,9.,6.,9.)
        intent='����������ڻ����˾���z=4��ı�ǰ���ѹ۲�λ�ã�actor�����Ŀ��������Ϸ��۲졣'
    elif case == "staggered_walls":
        # Exact user grid: x=0..15, z=0..14; the existing z=15 row is rear padding.
        points = sorted({(x,10) for x in range(3,12)} | {(x,7) for x in range(6,12)}
            | {(11,z) for z in range(2,7)} | {(x,4) for x in range(3,7)} | {(3,z) for z in (2,3)})
        start, goal = [7.5,-60.0,.5], [12.5,-60.0,13.5]
        relevant = [landmark(11,-59,2,"right_wall_lower_end"),
                    landmark(12,-61,2,"right_outer_support"),
                    landmark(12,-61,10,"goal_corridor_support")]
        decision = region(8.,13.,1.,5.)
        intent = "�û�ָ������ʯǽ���Ƚ��м��۷����������Ƴ�ǽ�¶˽����Ҳ�յء����κͷ�·���ֻ�����⣬actor����Ŀ�����ꡣ"
    elif case == "side_information":
        # Straight prefix is clear; the wall's side opening changes the detour choice.
        left, right, spur_end = {21001: (3, 10, 5), 21002: (2, 11, 6), 21003: (4, 9, 4)}[seed]
        points = [(x, 8) for x in range(left, right + 1)] + [(left, z) for z in range(3, spur_end + 1)]
        goal = [12.5, -60.0, 12.5]
        relevant = [landmark(right, -59, 8, "branch_wall_end"),
                    landmark(right + 1, -61, 8, "branch_support")]
        decision = region(5.0, 13.0, 6.0, 8.0)
        intent = "ǰ���̶�ƽ̹����ǽ���˿����У��Ҳ����������ӳ�ǽӰ����һ��ѡ·����ʷǰ׺���ɺϷ�Ԥ�۲�ȡ�á�"
    elif case == "corner_prelook":
        end, marker_x = {21001: (8, 11), 21002: (9, 12), 21003: (7, 10)}[seed]
        points = [(8, z) for z in range(end + 1)] + [(marker_x, end + 3)]
        start, goal = [5.5, -60.0, 2.5], [13.5, -60.0, 13.5]
        target = {"x": 9, "y": -61, "z": end + 2}
        relevant = [landmark(**target, role="post_corner_support")]
        checks = [{"from_position": start, "target_block": target, "expected_visible": False},
                  {"from_position": [7.5, -60.0, end + 1.5], "target_block": target, "expected_visible": True}]
        decision = region(7.0, 10.0, end + 1.0, end + 3.0)
        intent = "�س�ǽ����ǽ�˺�����ת����㿴����ǽ����棬�ӽ�ǽ��ʱ�ſ��ܺϷ�ȡ��ת���ͨ·��Ϣ��"
    elif case == "narrow_clearance":
        gap, depth = {21001: (7, 1), 21002: (8, 2), 21003: (6, 3)}[seed]
        points = [(x, z) for x in range(16) if x != gap for z in range(7, 7 + depth)]
        relevant = [landmark(gap, -61, 7, "mouth_support"),
                    landmark(gap - 1, -59, 7, "mouth_left_wall"),
                    landmark(gap + 1, -59, 7, "mouth_right_wall")]
        decision = region(gap, gap + 1.0, 6.0, 8.0 + depth)
        intent = "��ǽֻ��һ�����ͨͨ�ڣ�����ͬ�ߡ�0.6��������ͨ������������һ��ƫ0.25�����ǽ����Ҫͣ���������"
    elif case == "reposition_view":
        left, right, rear_z = {21001: (6, 9, 9), 21002: (5, 10, 10), 21003: (6, 10, 8)}[seed]
        points = [(x, 6) for x in range(left, right + 1)] + [(7, rear_z)]
        start = [7.5, -60.0, 3.5]
        target = {"x": 7, "y": -59, "z": rear_z}
        relevant = [landmark(**target, role="occluded_route_obstacle")]
        checks = [{"from_position": start, "target_block": target, "expected_visible": False},
                  {"from_position": [right + 1.5, -60.0, 7.5], "target_block": target, "expected_visible": True}]
        decision = region(left - 1.0, right + 2.0, 7.0, rear_z + 1.0)
        intent = "������ڵ�ǽ��ס���ϰ������ԭ��תͷ���ܿ�������ƽ���Ƶ�ǽ��󣬲ſ��ܿ������·���ϰ���"
    elif case == "irrelevant_side":
        # Similar side-wall complexity, but no block intersects the straight task corridor.
        end, tooth = {21001: (10, 5), 21002: (11, 7), 21003: (9, 6)}[seed]
        points = [(2, z) for z in range(3, end + 1)] + [(x, tooth) for x in (0, 1, 3)]
        irrelevant = [landmark(2, -59, end, "off_route_wall"),
                      landmark(3, -59, tooth, "off_route_tooth")]
        decision = region(6.0, 9.0, 6.0, 9.0)
        intent = "���յ�ֱ��ͨ·ƽ̹���������ิ��ʯǽ��ȫλ������ͨ·֮�⡣δ�����෽��Ϣ�������ڼ���޼�ֵͣ�������С�"
    else:
        raise ValueError("undeclared joint navigation case")
    diagnostics = {
        "schema_version": "mc2p.normal-navigation-evaluator-diagnostics.v1",
        "purpose": "evaluator_only_not_actor_input",
        "intent": intent,
        "relevant_blocks": relevant,
        "irrelevant_blocks": irrelevant,
        "decision_region": decision,
        # These rays test fixture geometry only. Actual acquisition must come from legal observations.
        "visibility_checks": checks,
    }
    return points, start, goal, diagnostics


def _joint_diagnostics(case: str, seed: int, layout: dict) -> dict:
    diagnostics = _joint_case(case, seed)[3]
    diagnostics["layout_content_sha256"] = hashlib.sha256(_canonical_json(layout).encode("utf-8")).hexdigest()
    return diagnostics


def _layout(case: str, seed: int) -> dict:
    obstacles: list[dict] = []
    pits: list[dict] = []
    if case in JOINT_CASES:
        obstacles = _obstacles(_joint_case(case, seed)[0])
    elif case == "entrance":
        front_end, corner_end = {21001: (10, 12), 21002: (11, 13), 21003: (9, 11)}[seed]
        obstacles = _obstacles([(x, 9) for x in range(2, front_end + 1)]
                               + [(2, z) for z in range(10, corner_end + 1)])
    elif case == "pillar":
        obstacles = _obstacles([{21001: (7, 7), 21002: (6, 8), 21003: (8, 6)}[seed]])
    elif case == "pillars":
        points = {
            21001: [(6, 5), (8, 8), (6, 10)],
            21002: [(5, 5), (8, 7), (7, 11)],
            21003: [(8, 5), (5, 8), (8, 10)],
        }[seed]
        obstacles = _obstacles(points)
    elif case == "pit":
        x0, z0 = {21001: (6, 6), 21002: (5, 7), 21003: (7, 5)}[seed]
        pits = [{"x": x, "z": z} for x in range(x0, x0 + 3) for z in range(z0, z0 + 3)]
    elif case == "two_routes":
        first, last = {21001: (4, 10), 21002: (3, 10), 21003: (4, 12)}[seed]
        obstacles = _obstacles([(x, 7) for x in range(first, last + 1)])
    return {
        "schema_version": "mc2p.normal-navigation-layout.v1",
        "bounds": {"min_x": 0, "max_x": 15, "min_z": 0, "max_z": 15},
        "standing_y": -60,
        "ground_y": -61,
        "base_layers": _base_layers(),
        "obstacles": obstacles,
        "pit_cells": pits,
    }


def case_plan(case: str, seed: int) -> dict:
    if not isinstance(case, str) or case not in CASES:
        raise ValueError("undeclared normal navigation case")
    if type(seed) is not int or seed not in SEEDS:
        raise ValueError("undeclared normal navigation seed")
    starts = {
        "empty": [1.5, -60.0, 1.5],
        "entrance": [1.924, -60.0, 8.297],
        "pillar": [7.5, -60.0, 2.5],
        "pillars": [7.5, -60.0, 2.5],
        "pit": [7.5, -60.0, 2.5],
        "two_routes": [6.5, -60.0, 2.5],
    }
    goals = {
        "empty": [1.5, -60.0, 12.5],
        "entrance": [1.5, -60.0, 13.5],
        "pillar": [7.5, -60.0, 12.5],
        "pillars": [7.5, -60.0, 12.5],
        "pit": [7.5, -60.0, 12.5],
        "two_routes": [8.5, -60.0, 12.5],
    }
    if case in JOINT_CASES:
        _, starts[case], goals[case], _ = _joint_case(case, seed)
    yaw = {21001: 0.0, 21002: 35.0, 21003: -45.0}[seed] if case == "empty" else 0.0
    plan = {
        "case": case,
        "seed": seed,
        "layout": _layout(case, seed),
        "start": {"position": starts[case], "yaw_degrees": yaw, "pitch_degrees": 0.0},
        "actor_task": {"goal_id": f"normal-{case}-{seed}", "position": goals[case]},
        "budget_ns": 30_000_000_000 if case == "empty" else 90_000_000_000,
    }
    if case in JOINT_CASES:
        plan["evaluator_diagnostics"] = _joint_diagnostics(case, seed, plan["layout"])
    _validate_plan(plan)
    return plan


def _body_clear(layout: dict, position: list[float]) -> bool:
    x, foot_y, z = position
    lower_x, upper_x = x - 0.3, x + 0.3
    lower_z, upper_z = z - 0.3, z + 0.3
    head_y = foot_y + 1.8
    return not any(
        lower_x < block["x"] + 1 and upper_x > block["x"]
        and foot_y < block["y"] + 1 and head_y > block["y"]
        and lower_z < block["z"] + 1 and upper_z > block["z"]
        for block in layout["obstacles"]
    )


def _supported(layout: dict, position: list[float]) -> bool:
    x, foot_y, z = position
    bounds = layout["bounds"]
    cell = (int(x // 1), int(z // 1))
    pits = {(entry["x"], entry["z"]) for entry in layout["pit_cells"]}
    return (foot_y == float(layout["standing_y"])
            and bounds["min_x"] <= cell[0] <= bounds["max_x"]
            and bounds["min_z"] <= cell[1] <= bounds["max_z"]
            and cell not in pits)


def _route_exists(layout: dict, start: list[float], goal: list[float]) -> bool:
    bounds = layout["bounds"]
    pits = {(entry["x"], entry["z"]) for entry in layout["pit_cells"]}
    obstacle_cells = {(entry["x"], entry["z"]) for entry in layout["obstacles"]
                      if -60 <= entry["y"] <= -59}

    def open_cell(cell: tuple[int, int]) -> bool:
        x, z = cell
        return (bounds["min_x"] <= x <= bounds["max_x"]
                and bounds["min_z"] <= z <= bounds["max_z"]
                and cell not in pits and cell not in obstacle_cells
                and _body_clear(layout, [x + 0.5, -60.0, z + 0.5]))

    origin, target = (int(start[0] // 1), int(start[2] // 1)), (int(goal[0] // 1), int(goal[2] // 1))
    if not open_cell(origin) or not open_cell(target):
        return False
    pending, visited = deque([origin]), {origin}
    while pending:
        current = pending.popleft()
        if current == target:
            return True
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            candidate = current[0] + dx, current[1] + dz
            if candidate not in visited and open_cell(candidate):
                visited.add(candidate)
                pending.append(candidate)
    return False


def _validate_plan(plan: dict) -> None:
    fields = {"case", "seed", "layout", "start", "actor_task", "budget_ns"}
    if type(plan) is dict and plan.get("case") in JOINT_CASES:
        fields.add("evaluator_diagnostics")
    if type(plan) is not dict or set(plan) != fields:
        raise ValueError("invalid normal navigation case plan fields")
    if plan["case"] not in CASES or type(plan["seed"]) is not int or plan["seed"] not in SEEDS:
        raise ValueError("invalid normal navigation case identity")
    if set(plan["start"]) != {"position", "yaw_degrees", "pitch_degrees"}:
        raise ValueError("invalid fixture-only start")
    if set(plan["actor_task"]) != {"goal_id", "position"}:
        raise ValueError("actor task leaked fixture control data")
    if plan["budget_ns"] not in (30_000_000_000, 90_000_000_000):
        raise ValueError("invalid navigation budget")
    layout = plan["layout"]
    if type(layout) is not dict or set(layout) != {
            "schema_version", "bounds", "standing_y", "ground_y", "base_layers", "obstacles", "pit_cells"}:
        raise ValueError("invalid layout fields")
    if (layout["schema_version"] != "mc2p.normal-navigation-layout.v1"
            or layout["bounds"] != {"min_x": 0, "max_x": 15, "min_z": 0, "max_z": 15}
            or layout["standing_y"] != -60 or layout["ground_y"] != -61
            or layout["base_layers"] != _base_layers()):
        raise ValueError("layout base plane differs from frozen contract")
    seen_obstacles: set[tuple[int, int, int]] = set()
    for block in layout["obstacles"]:
        if (type(block) is not dict or set(block) != {"block", "x", "y", "z"}
                or block["block"] != "minecraft:stone"
                or any(type(block[key]) is not int for key in ("x", "y", "z"))
                or not 0 <= block["x"] <= 15 or not 0 <= block["z"] <= 15
                or not -60 <= block["y"] <= -58):
            raise ValueError("invalid layout obstacle")
        coordinate = block["x"], block["y"], block["z"]
        if coordinate in seen_obstacles:
            raise ValueError("duplicate layout obstacle")
        seen_obstacles.add(coordinate)
    seen_pits: set[tuple[int, int]] = set()
    for cell in layout["pit_cells"]:
        if (type(cell) is not dict or set(cell) != {"x", "z"}
                or any(type(cell[key]) is not int for key in ("x", "z"))
                or not 0 <= cell["x"] <= 15 or not 0 <= cell["z"] <= 15):
            raise ValueError("invalid pit cell")
        coordinate = cell["x"], cell["z"]
        if coordinate in seen_pits:
            raise ValueError("duplicate pit cell")
        seen_pits.add(coordinate)
    start, goal = plan["start"]["position"], plan["actor_task"]["position"]
    for name, position in (("start", start), ("goal", goal)):
        if (type(position) is not list or len(position) != 3
                or any(type(value) not in (int, float) for value in position)
                or not _supported(layout, position) or not _body_clear(layout, position)):
            raise ValueError(f"{name} lacks frozen support or body clearance")
    if not _route_exists(layout, start, goal):
        raise ValueError("layout has no normal route")
    if plan["case"] in JOINT_CASES and plan["evaluator_diagnostics"] != _joint_diagnostics(
            plan["case"], plan["seed"], layout):
        raise ValueError("joint fixture evaluator diagnostic mismatch")


def _world_hashes(world: Path) -> dict[str, str]:
    _no_links(world)
    found: dict[str, str] = {}
    for directory, directories, files in os.walk(world, followlinks=False):
        parent = Path(directory)
        for name in directories:
            child = parent / name
            _no_links(child)
            if child.relative_to(world).as_posix() not in {"region", "playerdata"}:
                raise ValueError("undeclared fixture directory")
        for name in files:
            child = parent / name
            _no_links(child)
            relative = child.relative_to(world).as_posix()
            if relative not in WORLD_FILES or not stat.S_ISREG(child.stat().st_mode):
                raise ValueError("undeclared/nonregular fixture file")
            found[relative] = _hash(child)
    if set(found) != WORLD_FILES:
        raise ValueError("missing fixture world files")
    return found


def _native(mode: str, world: Path, layout_path: Path, plan: dict, work: Path) -> None:
    classpath = _classpath()
    source, host = ROOT / SOURCES[1], ROOT / SOURCES[3]
    _run([str(JAVA.parent / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8", "-proc:none",
          "-cp", classpath, "-d", str(work), str(source), str(host)], work, "compile")
    start, goal = plan["start"], plan["actor_task"]["position"]
    _run([str(JAVA), "-cp", str(work) + os.pathsep + classpath, "VanillaObjectTestHost",
          "NormalNavigationInitializer", mode, str(ROOT / SOURCES[2]), str(world), str(layout_path),
          plan["case"], str(plan["seed"]), *(str(value) for value in start["position"]),
          str(start["yaw_degrees"]), *(str(value) for value in goal)], work, mode)


def _manifest(world: Path, plan: dict, source_fingerprints: dict, layout_path: Path) -> dict:
    return {
        "schema_version": "mc2p.normal-navigation-fixture.v1",
        "minecraft_data_version": 3953,
        "purpose": "offline_test_initial_state_not_actor_capability",
        "player_initialization": PLAYER_INITIALIZATION,
        "case_plan": plan,
        "layout_sha256": _hash(layout_path),
        "source_fingerprints": source_fingerprints,
        "files": _world_hashes(world),
    }


def prepare_normal_fixture(output: Path, case: str, seed: int) -> dict:
    plan = case_plan(case, seed)
    output = Path(output).absolute()
    if ".." in output.parts:
        raise ValueError("normal fixture parent escape")
    _no_links(output.parent)
    before = _sources()
    output.mkdir(exist_ok=False)
    layout_path = output / "layout.json"
    write_json_atomic(layout_path, plan["layout"])
    classes = output / "classes"
    classes.mkdir()
    _native("build", output / "world", layout_path, plan, classes)
    if before != _sources():
        raise ValueError("normal fixture sources changed during generation")
    manifest = _manifest(output / "world", plan, before, layout_path)
    write_json_atomic(output / "fixture-manifest.json", manifest)
    return manifest


def verify_normal_fixture(path: Path) -> dict:
    path = Path(path).absolute()
    _no_links(path)
    manifest_path, layout_path = path / "fixture-manifest.json", path / "layout.json"
    _no_links(manifest_path)
    _no_links(layout_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if type(manifest) is not dict or set(manifest) != {
            "schema_version", "minecraft_data_version", "purpose", "player_initialization",
            "case_plan", "layout_sha256",
            "source_fingerprints", "files"}:
        raise ValueError("invalid normal fixture manifest fields")
    plan = manifest["case_plan"]
    _validate_plan(plan)
    expected_plan = case_plan(plan["case"], plan["seed"])
    layout = json.loads(layout_path.read_text("utf-8"))
    before = _sources()
    if (manifest["schema_version"] != "mc2p.normal-navigation-fixture.v1"
            or manifest["minecraft_data_version"] != 3953
            or manifest["purpose"] != "offline_test_initial_state_not_actor_capability"
            or manifest["player_initialization"] != PLAYER_INITIALIZATION
            or plan != expected_plan or layout != plan["layout"]
            or manifest["layout_sha256"] != _hash(layout_path)
            or manifest["source_fingerprints"] != before
            or manifest["files"] != _world_hashes(path / "world")):
        raise ValueError("normal fixture provenance/content mismatch")
    with TemporaryDirectory(prefix="mc2p-normal-verifier-") as temporary:
        try:
            _native("verify", path / "world", layout_path, plan, Path(temporary))
        except RuntimeError as error:
            raise ValueError("native normal fixture verification failed") from error
    if before != _sources() or manifest["files"] != _world_hashes(path / "world"):
        raise ValueError("normal fixture changed during verification")
    return manifest
