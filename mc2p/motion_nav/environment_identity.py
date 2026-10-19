"""Frozen runtime identity shared by motion profiles and acceptance evidence."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

from mc2p.contracts.common import ContractViolation, require_identifier


FROZEN_ENVIRONMENT_ID = "fabric-1_21-motion-v1"
FROZEN_ENVIRONMENT_SHA256 = "0c1e49c9fc4a5b46dca40d42aa09c292018cf7d973b8570138859d2831e9caed"
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]*$")


@dataclass(frozen=True, slots=True)
class MotionEnvironmentIdentity:
    environment_id: str
    minecraft_version: str
    fabric_loader_version: str
    fabric_api_version: str
    yarn_mappings: str
    java_major: int
    python_version: tuple[int, int]
    tick_seconds: float
    automatic_jump: bool
    observation_protocol: str
    action_protocol: str
    client_mod_id: str
    client_mod_version: str
    server_kind: str
    baseline_source_commit: str
    evidence_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        require_identifier(self.environment_id, "motion environment id")
        for value, name in (
            (self.minecraft_version, "Minecraft version"),
            (self.fabric_loader_version, "Fabric Loader version"),
            (self.fabric_api_version, "Fabric API version"),
            (self.yarn_mappings, "Yarn mappings"),
        ):
            if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
                raise ContractViolation(f"{name} must be a non-empty version identifier")
        if type(self.java_major) is not int or self.java_major < 1:
            raise ContractViolation("Java major version must be positive")
        if (type(self.python_version) is not tuple or len(self.python_version) != 2
                or any(type(part) is not int or part < 0 for part in self.python_version)):
            raise ContractViolation("Python version must be a major/minor pair")
        if (type(self.tick_seconds) not in (int, float)
                or not math.isfinite(float(self.tick_seconds)) or self.tick_seconds <= 0):
            raise ContractViolation("tick duration must be positive and finite")
        if type(self.automatic_jump) is not bool:
            raise ContractViolation("automatic jump setting must be explicit")
        for value, name in (
            (self.observation_protocol, "observation protocol"),
            (self.action_protocol, "action protocol"),
            (self.client_mod_id, "client mod id"),
            (self.client_mod_version, "client mod version"),
            (self.server_kind, "server kind"),
        ):
            require_identifier(value, name)
        if (not isinstance(self.baseline_source_commit, str)
                or not re.fullmatch(r"[0-9a-f]{40}", self.baseline_source_commit)):
            raise ContractViolation("baseline source commit must be a full Git object id")
        if (type(self.evidence_roles) is not tuple or not self.evidence_roles
                or self.evidence_roles != tuple(sorted(set(self.evidence_roles)))):
            raise ContractViolation("evidence roles must be sorted and unique")
        for role in self.evidence_roles:
            require_identifier(role, "evidence role")

    def require_profile_environment(self, environment_id: str) -> None:
        require_identifier(environment_id, "profile environment id")
        if environment_id != self.environment_id:
            raise ContractViolation("profile environment does not match frozen environment")


def load_frozen_environment(
    path: Path,
    *,
    expected_sha256: str = FROZEN_ENVIRONMENT_SHA256,
) -> MotionEnvironmentIdentity:
    if not isinstance(path, Path):
        raise ContractViolation("motion environment path must be a Path")
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)):
        raise ContractViolation("environment configuration hash must be lowercase SHA-256")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ContractViolation("environment configuration hash does not match frozen identity")
    try:
        document = json.loads(payload)
        if document.get("schema_version") != "mc2p.motion-environment.v1":
            raise ContractViolation("unsupported motion environment schema")
        stack = document["stack"]
        runtime = document["runtime"]
        deployment = document["deployment"]
        evidence = document["evidence_roles"]
        if not isinstance(evidence, dict) or any(
                not isinstance(detail, str) or not detail.strip()
                for detail in evidence.values()):
            raise ContractViolation("environment evidence roles require descriptions")
        return MotionEnvironmentIdentity(
            environment_id=document["environment_id"],
            minecraft_version=stack["minecraft_version"],
            fabric_loader_version=stack["fabric_loader_version"],
            fabric_api_version=stack["fabric_api_version"],
            yarn_mappings=stack["yarn_mappings"],
            java_major=stack["java_major"],
            python_version=(stack["python_major"], stack["python_minor"]),
            tick_seconds=runtime["tick_seconds"],
            automatic_jump=runtime["automatic_jump"],
            observation_protocol=deployment["observation_protocol"],
            action_protocol=deployment["action_protocol"],
            client_mod_id=deployment["client_mod_id"],
            client_mod_version=deployment["client_mod_version"],
            server_kind=deployment["server_kind"],
            baseline_source_commit=deployment["baseline_source_commit"],
            evidence_roles=tuple(sorted(evidence)),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ContractViolation("motion environment configuration is invalid") from error
