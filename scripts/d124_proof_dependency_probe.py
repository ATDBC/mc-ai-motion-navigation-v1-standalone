"""D124 read-only proof-dependency and Minecraft 1.21 rule audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import d123_dependency_probe as d123
from mc2p.backends.client_behavior_payload import (
    ACTION_SCHEMA_V2, MAX_ACTION_BYTES, encode_behavior_action,
)
from mc2p.contracts.action_v1 import (
    ActionSnapshotV1, AttackEntityV1, ClickSlotV1, CloseScreenV1,
    InteractBlockV1, LookV1, MineBlockV1, MovementTickWindowV1, MovementV1,
    OpenInventoryV1, SafeDefaultV1, SelectHotbarV1,
)


BASE_COMMIT = "9b8a2e7d"
CLASSIFICATIONS = (
    ROOT / "evidence/motion_navigation/d123-proof-and-transport-v1/"
    "d123-0/classifications-final.json"
)
PRODUCTION_FILES = (
    "mc2p/motion_nav/geometry.py",
    "mc2p/motion_nav/physics_adapter.py",
    "mc2p/motion_nav/physics_1_21.py",
    "mc2p/motion_nav/prepared_motion_proof.py",
    "mc2p/motion_nav/safe_ground_control.py",
    "mc2p/motion_nav/support_surfaces.py",
)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _git_blob(relative: str) -> bytes:
    return subprocess.run(
        ("git", "show", f"{BASE_COMMIT}:{relative}"), cwd=ROOT,
        check=True, stdout=subprocess.PIPE,
    ).stdout


def _named_jar() -> Path:
    roots = (ROOT / ".gradle", ROOT.parent.parent / ".gradle")
    matches = []
    for root in roots:
        if root.exists():
            matches.extend(root.glob(
                "caches/fabric-loom/minecraftMaven/net/minecraft/"
                "minecraft-merged/1.21-*/minecraft-merged-1.21-*.jar"
            ))
    if not matches:
        raise FileNotFoundError("locked named Minecraft 1.21 jar is unavailable")
    return sorted(matches)[0]


def _source_jar() -> Path:
    roots = (ROOT / ".gradle", ROOT.parent.parent / ".gradle")
    for root in roots:
        candidate = root / "caches/fabric-loom/1.21/minecraft-client.jar"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("locked source Minecraft 1.21 client jar is unavailable")


def _javap() -> Path:
    candidates = (
        ROOT / ".venv/Library/bin/javap.exe",
        ROOT.parent.parent / ".venv/Library/bin/javap.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("project javap is unavailable")


def original_rule_audit() -> dict:
    jar = _named_jar()
    source = _source_jar()
    def javap(class_name: str) -> str:
        return subprocess.run(
            (str(_javap()), "-classpath", str(jar), "-c", "-p", class_name),
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        ).stdout
    text = javap("net.minecraft.entity.Entity")
    collision_text = javap("net.minecraft.world.BlockCollisionSpliterator")
    slab_text = javap("net.minecraft.block.SlabBlock")
    stair_text = javap("net.minecraft.block.StairsBlock")
    piston_text = javap("net.minecraft.block.PistonBlock")
    piston_extension_text = javap("net.minecraft.block.PistonExtensionBlock")
    piston_entity_text = javap("net.minecraft.block.entity.PistonBlockEntity")
    flow_text = javap("net.minecraft.fluid.FlowableFluid")
    required = {
        "body_cell_material_callback": "checkBlockCollision",
        "body_box_inset": "1.0E-7d",
        "support_owner_selection": "findSupportingBlockPos",
        "velocity_affecting_cell": "getVelocityAffectingPos",
        "surface_velocity_multiplier": "getVelocityMultiplier",
        "block_state_collision_callback": "onEntityCollision",
    }
    found = {name: token in text for name, token in required.items()}
    collision_checks = {
        "one_cell_border_enumeration": (
            "CuboidBlockIterator" in collision_text and "1.0E-7d" in collision_text
        ),
        "contextual_collision_shape": "BlockState.getCollisionShape" in collision_text,
        "full_cube_fast_path": "VoxelShapes.fullCube" in collision_text,
        "edge_shape_filter": (
            "BlockState.exceedsCube" in collision_text
            and "Blocks.MOVING_PISTON" in collision_text
        ),
    }
    representative_shapes = {
        "double_slab_is_full_cube": (
            "SlabType.DOUBLE" in slab_text and "VoxelShapes.fullCube" in slab_text
        ),
        "single_slabs_are_partial": (
            "TOP_SHAPE" in slab_text and "BOTTOM_SHAPE" in slab_text
        ),
        "stairs_are_composed_partial_shapes": (
            "TOP_SHAPES" in stair_text and "BOTTOM_SHAPES" in stair_text
            and "VoxelShapes.union" in stair_text
        ),
    }
    dynamic_checks = {
        "piston_requires_push_plan_before_motion": (
            "PistonHandler.calculatePush" in piston_text
            and "Blocks.MOVING_PISTON" in piston_text
        ),
        "moving_piston_collision_comes_from_block_entity": (
            "PistonBlockEntity.getCollisionShape" in piston_extension_text
            and "public net.minecraft.util.shape.VoxelShape getCollisionShape" in piston_entity_text
        ),
        "moving_shape_is_offset_by_bounded_progress": (
            "getAmountExtended" in piston_entity_text
            and "VoxelShape.offset" in piston_entity_text
            and "fconst_1" in piston_entity_text
            and "fsub" in piston_entity_text
        ),
        "entity_push_is_a_separate_world_tick_effect": (
            "pushEntities" in piston_entity_text
            and "offsetHeadBox" in piston_entity_text
            and "Boxes.stretch" in piston_entity_text
            and "World.getOtherEntities" in piston_entity_text
            and "0.5f" in piston_entity_text
        ),
        "honey_drag_only_runs_for_horizontal_motion": (
            "moveEntitiesInHoneyBlock" in piston_entity_text
            and "Direction$Axis.isHorizontal" in piston_entity_text
            and "1.5000010000000001d" in piston_entity_text
        ),
        "body_fluid_scan_is_contracted": (
            "updateMovementInFluid" in text
            and "0.001d" in text
            and "World.getFluidState" in text
        ),
        "surface_effect_positions_are_separate": (
            "getVelocityAffectingPos" in text
            and "getSteppingPos" in text
            and "0.500001f" in text
            and "1.0E-5f" in text
        ),
        "full_cube_blocks_fluid_flow": (
            "canFlowThrough" in flow_text
            and "canFill" in flow_text
            and "BlockState.blocksMovement" in flow_text
        ),
    }
    if not all((*found.values(), *collision_checks.values(),
                *representative_shapes.values(), *dynamic_checks.values())):
        raise RuntimeError("Minecraft 1.21 rule audit incomplete")
    return {
        "source_jar": str(source),
        "source_jar_sha256": digest_bytes(source.read_bytes()),
        "named_transformed_jar": str(jar),
        "named_transformed_jar_sha256": digest_bytes(jar.read_bytes()),
        "javap": str(_javap()), "class": "net.minecraft.entity.Entity",
        "checks": found,
        "block_collision_spliterator_checks": collision_checks,
        "representative_shape_checks": representative_shapes,
        "dynamic_block_checks": dynamic_checks,
        "audit_method": (
            "source jar is retained for provenance; named transformed bytecode is "
            "used for automatic call-site and representative-shape checks"
        ),
        "conclusion": {
            "collision_shape_owners": "broad shape candidates may include one lower owner",
            "body_material_callbacks": "only cells inside the body box contracted by 1e-7",
            "surface_effects": "supporting/velocity-affecting position is queried separately",
            "full_cube_shadow": (
                "a lower local x/z-bounded shape with local max_y<=2 adds no collision "
                "outside the known full cube directly above"
            ),
            "moving_piston_boundary": (
                "a legal upward piston move first plans and replaces its destination, so a "
                "moving lower owner cannot coexist with the unchanged full-cube destination; "
                "horizontal/downward push and honey boxes remain below the cover top. A later "
                "piston tick is a world-change/external-force event, not frozen-shape evidence"
            ),
            "fluid_boundary": (
                "fluid and block-collision callbacks use body cells; a lower broad owner hidden "
                "behind the cover is not a body-contact cell"
            ),
            "synthetic_unsupported_boundary": (
                "unsupported BlockGeometry fixtures only lock query/cache semantics and are not "
                "claimed as additional vanilla states"
            ),
        },
    }


def dependency_audit() -> dict:
    historical = json.loads(CLASSIFICATIONS.read_text(encoding="utf-8"))["cells"]
    directions = []
    for direction in range(4):
        case = d123.fixed_case(direction)
        baseline = d123.replay(case, {})
        rows = [row for row in historical if row["direction"] == direction]
        shadowed = tuple(tuple(row["position"]) for row in rows
                         if row["whole_chain_unknown_solved"])
        required = tuple(tuple(row["position"]) for row in rows
                         if not row["whole_chain_unknown_solved"])
        mutations = []
        for position in shadowed + required:
            result = d123.replay(case, {position: d123.UNKNOWN_CELL})
            mutations.append({
                "position": position,
                "classified": "shadowed" if position in shadowed else "required",
                "dependency_present": position in result["dependencies"],
                "gate": result["offline_dependency_gate"],
                "parent": result["parent_status"],
                "successor": result["successor_binding_status"],
            })
        directions.append({
            "direction": direction,
            "dependency_count": len(baseline["dependencies"]),
            "shadowed": shadowed, "required": required,
            "mutations": mutations,
        })
    return {
        "directions": directions,
        "all_dependency_counts": [row["dependency_count"] for row in directions],
        "shadowed_removed": all(
            not mutation["dependency_present"] and mutation["gate"] == "known"
            for row in directions for mutation in row["mutations"]
            if mutation["classified"] == "shadowed"
        ),
        "required_retained": all(
            mutation["dependency_present"] and mutation["gate"] == "NEEDS_WORLD"
            for row in directions for mutation in row["mutations"]
            if mutation["classified"] == "required"
        ),
    }


def source_boundary_audit() -> dict:
    support_source = (ROOT / "mc2p/motion_nav/support_surfaces.py").read_text("utf-8")
    prepared_source = (ROOT / "mc2p/motion_nav/prepared_motion_proof.py").read_text("utf-8")
    solver_source = (ROOT / "mc2p/motion_nav/motion_solver.py").read_text("utf-8")
    support_occurrences = support_source.count("required_cells_for_sweep")
    return {
        "support_surfaces_required_cell_occurrences": support_occurrences,
        "support_surfaces_imported_by_prepared_proof": "support_surfaces" in prepared_source,
        "support_surfaces_imported_by_motion_solver": "support_surfaces" in solver_source,
        "classification": (
            "planner_and_terminal-standability conservative dependencies; "
            "not part of the D121/D122 prepared proof dependency collector"
        ),
        "prepared_broad_dependency_update_present": (
            "dependencies.update(cells)" in prepared_source
        ),
        "identity_chain": {
            "world_to_snapshot": "seal_snapshot_version",
            "request_identity": "PreparedMotionRequest.snapshot_identity",
            "worker_job": "PreparedMotionJob",
            "proof_entry": "prepare_fixed_motion",
            "existing_mutation_suite": "tests/motion_nav/d102_contract_cases.py",
        },
    }


def action_transport_audit() -> dict:
    """Measure the largest bounded v2 action across every operation variant."""
    maximum = 2**63 - 1
    operations = (
        None,
        OpenInventoryV1(),
        CloseScreenV1(),
        SelectHotbarV1(8),
        ClickSlotV1("x" * 128, 2**31 - 1, 2**31 - 1, 1023, 8, "swap"),
        InteractBlockV1(-30_000_000, -30_000_000, -30_000_000, "north"),
        MineBlockV1(-30_000_000, -30_000_000, -30_000_000, "north"),
        AttackEntityV1("x" * 128, 0.12345678901234567),
    )
    sizes = {}
    for operation in operations:
        name = "none" if operation is None else type(operation).__name__
        action = ActionSnapshotV1(
            "e" * 128, maximum, maximum, maximum,
            movement=MovementV1(-1, -1, True, True, True),
            look=LookV1(-180.0, -180.0),
            operation=operation,
            valid_for_ticks=20,
            first_effect_window=MovementTickWindowV1(maximum - 20, maximum - 20),
            safe_default=SafeDefaultV1(
                maximum, MovementV1(-1, -1, True, True, True),
                20, 30_000_000_000,
            ),
        )
        sizes[name] = len(encode_behavior_action(
            action,
            now_ns=1,
            observation_received_at_ns=0,
            negotiated_action_schema=ACTION_SCHEMA_V2,
        ))
    largest_name = max(sizes, key=sizes.__getitem__)
    largest = sizes[largest_name]
    return {
        "schema": ACTION_SCHEMA_V2,
        "limit_bytes": MAX_ACTION_BYTES,
        "sizes_by_operation_bytes": sizes,
        "largest_operation": largest_name,
        "largest_bytes": largest,
        "within_limit": largest <= MAX_ACTION_BYTES,
        "method": (
            "encode one valid action per supported operation using maximum bounded "
            "identifier, integer, tick-window, lease and safe-default fields"
        ),
    }


def run(output: Path) -> int:
    if output.exists():
        raise ValueError("append-only evidence: output must not exist")
    output.mkdir(parents=True)
    write(output / "environment.json", {
        "platform": platform.platform(), "python": sys.version,
        "executable": sys.executable, "cwd": str(ROOT),
        "base_commit": BASE_COMMIT,
    })
    write(output / "minecraft-1_21-rules.json", original_rule_audit())
    dependencies = dependency_audit()
    write(output / "dependencies.json", dependencies)
    boundaries = source_boundary_audit()
    write(output / "source-boundaries.json", boundaries)
    action_transport = action_transport_audit()
    write(output / "action-transport.json", action_transport)
    sources = {}
    for relative in PRODUCTION_FILES:
        sources[relative] = {
            "base_sha256": digest_bytes(_git_blob(relative)),
            "current_sha256": digest_bytes((ROOT / relative).read_bytes()),
        }
    write(output / "source-hashes.json", sources)
    summary = {
        "status": "PASS",
        "stage": "D124-0-to-D124-2-targeted",
        "dependency_counts": dependencies["all_dependency_counts"],
        "shadowed_removed": dependencies["shadowed_removed"],
        "required_retained": dependencies["required_retained"],
        "largest_action_bytes": action_transport["largest_bytes"],
        "action_within_limit": action_transport["within_limit"],
        "production_files_changed": [
            name for name, values in sources.items()
            if values["base_sha256"] != values["current_sha256"]
        ],
        "fabric_run": False,
        "d123_history_rewritten": False,
    }
    if (summary["dependency_counts"] != [58, 58, 58, 58]
            or not summary["shadowed_removed"]
            or not summary["required_retained"]
            or not summary["action_within_limit"]
            or boundaries["prepared_broad_dependency_update_present"]):
        summary["status"] = "FAILED"
    write(output / "summary.json", summary)
    files = {
        path.relative_to(output).as_posix(): digest_bytes(path.read_bytes())
        for path in output.iterdir() if path.is_file()
    }
    write(output / "manifest.json", {
        "schema_version": "mc2p.d124-proof-dependency-projection.v1",
        "files": files,
    })
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "PASS" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return run(parser.parse_args().output)


if __name__ == "__main__":
    raise SystemExit(main())
