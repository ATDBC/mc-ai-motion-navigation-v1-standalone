"""Declarative movement effects for the frozen Minecraft 1.21 block registry."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.environment_identity import FROZEN_ENVIRONMENT_ID
from mc2p.motion_nav.world_model import (
    BlockGeometry, BlockPos, CellKnowledge, WorldQueryCache, WorldView,
)


class TraitStatus(StrEnum):
    SUPPORTED = "supported"
    DEFERRED = "deferred"
    UNSUPPORTED = "unsupported"


class MotionEffect(StrEnum):
    BOUNCY = "bouncy"
    CLIMBABLE = "climbable"
    CONDITIONAL_SUPPORT = "conditional_support"
    FLUID = "fluid"
    HAZARD = "hazard"
    SLIPPERY = "slippery"
    SURFACE_SLOWING = "surface_slowing"
    VOLUME_SLOWING = "volume_slowing"
    WORLD_CHANGE_RISK = "world_change_risk"


@dataclass(frozen=True, slots=True)
class BlockMotionClassification:
    status: TraitStatus
    material_key: str
    ground_model_id: str | None
    effects: frozenset[MotionEffect]
    reason_code: str


@dataclass(frozen=True, slots=True)
class BlockMotionCatalog:
    environment_id: str
    minecraft_version: str
    registered_materials: frozenset[str]
    ground_models: tuple[tuple[str, frozenset[str]], ...]
    ground_shape_models: tuple[tuple[str, frozenset[str]], ...]
    effects_by_material: tuple[tuple[str, frozenset[MotionEffect]], ...]

    def __post_init__(self) -> None:
        require_identifier(self.environment_id, "block motion environment id")
        if self.environment_id != FROZEN_ENVIRONMENT_ID:
            raise ContractViolation("block motion catalog environment is not frozen")
        if self.minecraft_version != "1.21":
            raise ContractViolation("block motion catalog Minecraft version is unsupported")
        if not self.registered_materials:
            raise ContractViolation("block motion catalog requires a registry snapshot")
        for material in self.registered_materials:
            require_identifier(material, "registered block material")
        model_names = tuple(name for name, _ in self.ground_models)
        if model_names != tuple(sorted(set(model_names))):
            raise ContractViolation("ground model ids must be sorted and unique")
        ordinary_materials: set[str] = set()
        for model_id, materials in self.ground_models:
            require_identifier(model_id, "ground model id")
            if not materials or not materials.issubset(self.registered_materials):
                raise ContractViolation("ground model materials must exist in the frozen registry")
            if ordinary_materials.intersection(materials):
                raise ContractViolation("one material cannot use two ground models")
            ordinary_materials.update(materials)
        shape_model_names = tuple(name for name, _ in self.ground_shape_models)
        if shape_model_names != tuple(sorted(set(shape_model_names))):
            raise ContractViolation("shape ground model ids must be sorted and unique")
        known_models = set(model_names)
        for model_id, materials in self.ground_shape_models:
            require_identifier(model_id, "shape ground model id")
            if model_id not in known_models:
                raise ContractViolation("shape ground model must extend a declared ground model")
            if not materials or not materials.issubset(self.registered_materials):
                raise ContractViolation("shape ground materials must exist in the frozen registry")
            if ordinary_materials.intersection(materials):
                raise ContractViolation("one material cannot use two ground geometry families")
            ordinary_materials.update(materials)
        effect_materials = tuple(material for material, _ in self.effects_by_material)
        if effect_materials != tuple(sorted(set(effect_materials))):
            raise ContractViolation("effect materials must be sorted and unique")
        for material, effects in self.effects_by_material:
            if material not in self.registered_materials or not effects:
                raise ContractViolation("special effects require registered materials")
            if material in ordinary_materials:
                raise ContractViolation("ordinary material cannot carry deferred special effects")

    @classmethod
    def load(cls, traits_path: Path, registry_path: Path) -> BlockMotionCatalog:
        if not isinstance(traits_path, Path) or not isinstance(registry_path, Path):
            raise ContractViolation("block motion catalog paths must be Path values")
        try:
            traits = json.loads(traits_path.read_text("utf-8"))
            registry = json.loads(registry_path.read_text("utf-8"))
            if traits.get("schema_version") != "mc2p.block-motion-traits.v1":
                raise ContractViolation("unsupported block motion traits schema")
            if registry.get("schema_version") != "mc2p.vanilla-block-registry.v1":
                raise ContractViolation("unsupported vanilla block registry schema")
            materials = tuple(registry["materials"])
            if (registry["material_count"] != len(materials)
                    or materials != tuple(sorted(set(materials)))):
                raise ContractViolation("vanilla block registry must be sorted and complete")
            ground_models = tuple(sorted(
                (model_id, frozenset(model_materials))
                for model_id, model_materials in traits["ground_models"].items()
            ))
            ground_shape_models = tuple(sorted(
                (model_id, frozenset(model_materials))
                for model_id, model_materials
                in traits.get("ground_shape_models", {}).items()
            ))
            effects: dict[str, set[MotionEffect]] = {}
            for effect_name, effect_materials in traits["special_effects"].items():
                try:
                    effect = MotionEffect(effect_name)
                except ValueError as error:
                    raise ContractViolation("unknown block motion effect") from error
                for material in effect_materials:
                    effects.setdefault(material, set()).add(effect)
            return cls(
                environment_id=traits["environment_id"],
                minecraft_version=traits["minecraft_version"],
                registered_materials=frozenset(materials),
                ground_models=ground_models,
                ground_shape_models=ground_shape_models,
                effects_by_material=tuple(
                    (material, frozenset(material_effects))
                    for material, material_effects in sorted(effects.items())
                ),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ContractViolation("block motion catalog is invalid") from error

    def classify(self, geometry: BlockGeometry) -> BlockMotionClassification:
        if type(geometry) is not BlockGeometry:
            raise ContractViolation("block motion classification requires block geometry")
        material = geometry.material_key
        if material not in self.registered_materials:
            return BlockMotionClassification(
                TraitStatus.UNSUPPORTED, material, None, frozenset(),
                "material_not_in_frozen_registry",
            )
        for model_id, materials in self.ground_models:
            if material in materials:
                if geometry.collision_kind != "full_cube" or geometry.fluid:
                    return BlockMotionClassification(
                        TraitStatus.UNSUPPORTED, material, None, frozenset(),
                        "ordinary_geometry_mismatch",
                    )
                return BlockMotionClassification(
                    TraitStatus.SUPPORTED, material, model_id, frozenset(),
                    "ordinary_ground_supported",
                )
        for model_id, materials in self.ground_shape_models:
            if material in materials:
                if geometry.collision_kind not in {"boxes", "full_cube"} or geometry.fluid:
                    return BlockMotionClassification(
                        TraitStatus.UNSUPPORTED, material, None, frozenset(),
                        "ordinary_shape_geometry_mismatch",
                    )
                reason = ("ordinary_shape_full_cube_supported"
                          if geometry.collision_kind == "full_cube"
                          else "ordinary_shape_supported")
                return BlockMotionClassification(
                    TraitStatus.SUPPORTED, material, model_id, frozenset(), reason,
                )
        effects = dict(self.effects_by_material).get(material, frozenset())
        if effects:
            return BlockMotionClassification(
                TraitStatus.DEFERRED, material, None, effects,
                "special_behavior_deferred",
            )
        return BlockMotionClassification(
            TraitStatus.UNSUPPORTED, material, None, frozenset(),
            "motion_behavior_unclassified",
        )

    def materials_for_ground_model(self, model_id: str, *,
                                   include_shape_materials: bool = False) -> frozenset[str]:
        require_identifier(model_id, "ground model id")
        if type(include_shape_materials) is not bool:
            raise ContractViolation("shape material inclusion must be explicit")
        for candidate, materials in self.ground_models:
            if candidate == model_id:
                shape_materials = next((
                    candidate_materials
                    for candidate, candidate_materials in self.ground_shape_models
                    if candidate == model_id
                ), frozenset()) if include_shape_materials else frozenset()
                return materials | shape_materials
        raise ContractViolation("ground model is not present in the block motion catalog")


def unsupported_motion_cells(
    catalog: BlockMotionCatalog,
    world: WorldView,
    positions: tuple[BlockPos, ...],
    ground_model_id: str,
    *,
    query_cache: WorldQueryCache | None = None,
) -> tuple[BlockPos, ...]:
    """Return known blocks that the declared ordinary-motion model cannot enter or use."""
    if (type(catalog) is not BlockMotionCatalog or type(world) is not WorldView
            or type(positions) is not tuple):
        raise ContractViolation("motion cell check requires catalog, world and immutable cells")
    require_identifier(ground_model_id, "motion cell ground model id")
    if query_cache is not None and (
            type(query_cache) is not WorldQueryCache
            or query_cache.world is not world):
        raise ContractViolation("motion cell query cache belongs to another world view")
    rejected = []
    for position in sorted(set(positions)):
        fact = world.cell(position) if query_cache is None else query_cache.cell(position)
        if fact.knowledge is not CellKnowledge.BLOCK:
            continue
        assert fact.block is not None
        classification = catalog.classify(fact.block)
        if (classification.status is not TraitStatus.SUPPORTED
                or classification.ground_model_id != ground_model_id):
            rejected.append(position)
    return tuple(rejected)
