"""B08 ground-mode facts, inputs and independently identified dynamics."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import MotionEnvironmentIdentity
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.runtime_adapter import BodyState


_GROUND_MODES = frozenset({
    MovementMode.WALK, MovementMode.SPRINT,
    MovementMode.CROUCH, MovementMode.CRAWL,
})


class ModeReadiness(StrEnum):
    READY = "ready"
    PENDING = "pending"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    INVALID_ENTRY = "invalid_entry"
    GROUND_STATE_LOST = "ground_state_lost"


@dataclass(frozen=True, slots=True)
class GroundModeProfile:
    mode: MovementMode
    motion: GroundMotionProfile
    poses: frozenset[str]
    request_sprint: bool
    request_sneak: bool
    confirmation_ticks: int
    minimum_food_points: int

    def __post_init__(self) -> None:
        if self.mode not in _GROUND_MODES:
            raise ContractViolation("ground mode profile has a non-ground mode")
        if type(self.motion) is not GroundMotionProfile:
            raise ContractViolation("ground mode requires a motion profile")
        if type(self.poses) is not frozenset or not self.poses:
            raise ContractViolation("ground mode requires immutable poses")
        for pose in self.poses:
            require_identifier(pose, "ground mode pose")
        if type(self.request_sprint) is not bool or type(self.request_sneak) is not bool:
            raise ContractViolation("ground mode inputs must be boolean")
        if self.request_sprint and self.request_sneak:
            raise ContractViolation("ground mode cannot request sprint and sneak together")
        if type(self.confirmation_ticks) is not int or self.confirmation_ticks < 0:
            raise ContractViolation("ground mode confirmation ticks must be nonnegative")
        if type(self.minimum_food_points) is not int or not 0 <= self.minimum_food_points <= 20:
            raise ContractViolation("ground mode food threshold must be within 0..20")


@dataclass(frozen=True, slots=True)
class GroundModeProfiles:
    profile_set_id: str
    environment_id: str
    modes: Mapping[MovementMode, GroundModeProfile]
    validation_status: str

    def __post_init__(self) -> None:
        require_identifier(self.profile_set_id, "ground mode profile set id")
        require_identifier(self.environment_id, "ground mode environment id")
        require_identifier(self.validation_status, "ground mode validation status")
        if set(self.modes) != _GROUND_MODES:
            raise ContractViolation("ground mode profile set must contain exactly four B08 modes")
        if any(type(value) is not GroundModeProfile or value.mode is not key
               for key, value in self.modes.items()):
            raise ContractViolation("ground mode profile set contains an invalid profile")
        object.__setattr__(self, "modes", MappingProxyType(dict(self.modes)))

    def require(self, mode: MovementMode) -> GroundModeProfile:
        if type(mode) is not MovementMode or mode not in self.modes:
            raise ContractViolation("requested movement mode has no B08 ground profile")
        return self.modes[mode]


def load_ground_mode_profiles(
    path: Path,
    *,
    environment: MotionEnvironmentIdentity,
    catalog: BlockMotionCatalog,
) -> GroundModeProfiles:
    if (not isinstance(path, Path) or type(environment) is not MotionEnvironmentIdentity
            or type(catalog) is not BlockMotionCatalog):
        raise ContractViolation("ground mode profiles require path, environment and catalog")
    try:
        document = json.loads(path.read_text("utf-8"))
        if document.get("schema_version") != "mc2p.ground-modes.v1":
            raise ContractViolation("unsupported ground mode profile schema")
        scope = document["scope"]
        environment.require_profile_environment(scope["environment_id"])
        if scope["minecraft_version"] != environment.minecraft_version:
            raise ContractViolation("ground mode Minecraft version does not match environment")
        if abs(float(scope["tick_seconds"]) - environment.tick_seconds) > 1.0e-12:
            raise ContractViolation("ground mode tick duration does not match environment")
        materials = catalog.materials_for_ground_model(scope["ground_model_id"],
                                                       include_shape_materials=True)
        modes: dict[MovementMode, GroundModeProfile] = {}
        for text, value in document["modes"].items():
            mode = MovementMode(text)
            motion = value["motion"]
            profile = GroundMotionProfile(
                tick_seconds=scope["tick_seconds"],
                acceleration_blocks_per_second2=motion["acceleration_blocks_per_second2"],
                velocity_retention_per_tick=motion["velocity_retention_per_tick"],
                maximum_speed_blocks_per_second=motion["maximum_speed_blocks_per_second"],
                support_materials=materials,
                profile_id=value["profile_id"],
                environment_id=scope["environment_id"],
                ground_model_id=scope["ground_model_id"],
                motion_catalog=catalog,
            )
            inputs = value["input"]
            modes[mode] = GroundModeProfile(
                mode, motion=profile, poses=frozenset(value["poses"]),
                request_sprint=inputs["sprint"], request_sneak=inputs["sneak"],
                confirmation_ticks=value["confirmation_ticks"],
                minimum_food_points=value["minimum_food_points"],
            )
        return GroundModeProfiles(
            document["profile_set_id"], scope["environment_id"], modes,
            document["validation"]["status"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ContractViolation("ground mode profile document is invalid") from error


def observed_ground_mode(body: BodyState) -> MovementMode | None:
    if type(body) is not BodyState:
        raise ContractViolation("observed ground mode requires a body state")
    if body.pose == "swimming" and not body.is_submerged_in_water:
        return MovementMode.CRAWL
    if body.pose == "crouching" and body.is_sneaking:
        return MovementMode.CROUCH
    if body.pose == "standing" and body.is_sprinting:
        return MovementMode.SPRINT
    if body.pose == "standing" and not body.is_sneaking:
        return MovementMode.WALK
    return None


def evaluate_ground_mode(profile: GroundModeProfile, body: BodyState) -> ModeReadiness:
    if type(profile) is not GroundModeProfile or type(body) is not BodyState:
        raise ContractViolation("ground mode evaluation requires profile and body")
    if not body.is_on_ground:
        return ModeReadiness.GROUND_STATE_LOST
    if (profile.mode is MovementMode.SPRINT and body.game_mode in {"survival", "adventure"}
            and body.food_points < profile.minimum_food_points):
        return ModeReadiness.RESOURCE_UNAVAILABLE
    observed = observed_ground_mode(body)
    if observed is profile.mode and body.pose in profile.poses:
        return ModeReadiness.READY
    if profile.mode is MovementMode.CRAWL:
        return ModeReadiness.INVALID_ENTRY
    if profile.mode is MovementMode.SPRINT:
        return (ModeReadiness.PENDING if body.pose == "standing" and not body.is_sneaking
                else ModeReadiness.INVALID_ENTRY)
    if profile.mode is MovementMode.CROUCH:
        return (ModeReadiness.PENDING if body.pose == "standing" and not body.is_sprinting
                else ModeReadiness.INVALID_ENTRY)
    if profile.mode is MovementMode.WALK and observed in {
            MovementMode.SPRINT, MovementMode.CROUCH, MovementMode.CRAWL}:
        return ModeReadiness.PENDING
    return ModeReadiness.INVALID_ENTRY


def movement_for_ground_mode(
    profile: GroundModeProfile,
    movement: MovementV1,
) -> MovementV1:
    if type(profile) is not GroundModeProfile or type(movement) is not MovementV1:
        raise ContractViolation("ground mode input requires profile and movement")
    return MovementV1(
        movement.forward, movement.strafe, movement.jump,
        profile.request_sneak, profile.request_sprint,
    )
