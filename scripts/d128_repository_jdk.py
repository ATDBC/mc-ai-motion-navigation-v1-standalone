"""D128 binding for the repository-fixed Windows OpenJDK 21."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping

from scripts.export_motion_navigation_standalone import (
    ExportViolation, discover_java_tools,
)
from scripts.visibility_fixture_world import _no_links


ROOT = Path(__file__).resolve().parents[1]
ENV_PREFIX = "MC2P_D128_"
ENV_SCHEMA = ENV_PREFIX + "BINDING_SCHEMA"
ENV_JAVA = ENV_PREFIX + "JAVA"
ENV_JAVAC = ENV_PREFIX + "JAVAC"
ENV_CANONICAL_HOME = ENV_PREFIX + "CANONICAL_JAVA_HOME"
ENV_CANONICAL_JAVA = ENV_PREFIX + "CANONICAL_JAVA"
ENV_VENDOR = ENV_PREFIX + "JAVA_VENDOR"
ENV_VERSION = ENV_PREFIX + "JAVA_VERSION"
ENV_JAVA_SHA256 = ENV_PREFIX + "JAVA_SHA256"
ENV_JAVAC_SHA256 = ENV_PREFIX + "JAVAC_SHA256"
ENV_CANONICAL_JAVA_SHA256 = ENV_PREFIX + "CANONICAL_JAVA_SHA256"
ENV_IDENTITY_SHA256 = ENV_PREFIX + "IDENTITY_SHA256"


@dataclass(frozen=True, slots=True)
class RepositoryJavaBinding:
    java: Path
    javac: Path
    major_version: int
    canonical_java_home: Path
    canonical_java: Path
    vendor: str
    version: str
    java_sha256: str
    javac_sha256: str
    canonical_java_sha256: str

    def identity(self) -> dict[str, object]:
        return {
            "schema": "mc2p.d128-repository-jdk.v1",
            "major_version": self.major_version,
            "java": str(self.java),
            "javac": str(self.javac),
            "canonical_java_home": str(self.canonical_java_home),
            "canonical_java": str(self.canonical_java),
            "vendor": self.vendor,
            "version": self.version,
            "java_sha256": self.java_sha256,
            "javac_sha256": self.javac_sha256,
            "canonical_java_sha256": self.canonical_java_sha256,
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _properties(command: list[str]) -> dict[str, str]:
    completed = subprocess.run(
        command, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15,
    )
    if completed.returncode != 0:
        raise ExportViolation("repository OpenJDK 21 property query failed")
    properties = {}
    for line in (completed.stdout + completed.stderr).splitlines():
        match = re.fullmatch(r"\s*([a-zA-Z0-9_.]+)\s*=\s*(.*?)\s*", line)
        if match:
            properties[match.group(1)] = match.group(2)
    required = {"java.home", "java.vendor", "java.version"}
    if not required <= set(properties):
        raise ExportViolation("repository OpenJDK 21 properties are incomplete")
    return properties


def resolve_repository_jdk(*, repository_root: Path = ROOT) -> RepositoryJavaBinding:
    repository = Path(repository_root).resolve()
    java_home = (repository / ".venv/Library").absolute()
    try:
        _no_links(java_home)
        tools = discover_java_tools({"JAVA_HOME": str(java_home), "PATH": ""})
        expected_java = (java_home / "bin/java.exe").resolve()
        expected_javac = (java_home / "bin/javac.exe").resolve()
        if tools.java != expected_java or tools.javac != expected_javac:
            raise ExportViolation("repository OpenJDK 21 launcher identity differs")
        _no_links(tools.java)
        _no_links(tools.javac)
        java_properties = _properties([
            str(tools.java), "-XshowSettings:properties", "-version",
        ])
        javac_properties = _properties([
            str(tools.javac), "-J-XshowSettings:properties", "-version",
        ])
        if any(java_properties[name] != javac_properties[name]
               for name in ("java.home", "java.vendor", "java.version")):
            raise ExportViolation("repository java and javac canonical identity differs")
        canonical_home = Path(java_properties["java.home"]).resolve()
        if canonical_home != (java_home / "lib/jvm").resolve():
            raise ExportViolation("repository OpenJDK 21 canonical java.home differs")
        canonical_java = canonical_home / "bin/java.exe"
        _no_links(canonical_home)
        _no_links(canonical_java)
        if not canonical_java.is_file():
            raise ExportViolation("repository OpenJDK 21 canonical java is missing")
        major = re.match(r"(\d+)(?:\.|$)", java_properties["java.version"])
        if major is None or int(major.group(1)) != 21 or tools.major_version != 21:
            raise ExportViolation("repository OpenJDK major version differs")
        return RepositoryJavaBinding(
            java=tools.java,
            javac=tools.javac,
            major_version=21,
            canonical_java_home=canonical_home,
            canonical_java=canonical_java.resolve(),
            vendor=java_properties["java.vendor"],
            version=java_properties["java.version"],
            java_sha256=_sha256(tools.java),
            javac_sha256=_sha256(tools.javac),
            canonical_java_sha256=_sha256(canonical_java),
        )
    except (ExportViolation, FileNotFoundError, OSError, ValueError) as error:
        if isinstance(error, ExportViolation) and str(error).startswith("repository OpenJDK"):
            raise
        raise ExportViolation(
            f"repository OpenJDK 21 is unavailable: {java_home}"
        ) from error


def bound_environment(binding: RepositoryJavaBinding, *,
                      base_environment: Mapping[str, str] | None = None) -> dict[str, str]:
    values = dict(os.environ if base_environment is None else base_environment)
    for name in tuple(values):
        if name.startswith(ENV_PREFIX):
            values.pop(name)
    java_bin = binding.java.parent
    retained = []
    for raw in values.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        directory = Path(raw)
        if directory.resolve() == java_bin:
            continue
        if ((directory / "java.exe").is_file()
                or (directory / "javac.exe").is_file()):
            continue
        retained.append(raw)
    bound = {
        "JAVA_HOME": str(java_bin.parent),
        "PATH": os.pathsep.join((str(java_bin), *retained)),
        ENV_SCHEMA: "mc2p.d128-repository-jdk.v1",
        ENV_JAVA: str(binding.java),
        ENV_JAVAC: str(binding.javac),
        ENV_CANONICAL_HOME: str(binding.canonical_java_home),
        ENV_CANONICAL_JAVA: str(binding.canonical_java),
        ENV_VENDOR: binding.vendor,
        ENV_VERSION: binding.version,
        ENV_JAVA_SHA256: binding.java_sha256,
        ENV_JAVAC_SHA256: binding.javac_sha256,
        ENV_CANONICAL_JAVA_SHA256: binding.canonical_java_sha256,
    }
    identity_values = {
        name: value for name, value in bound.items()
        if name.startswith(ENV_PREFIX)
    }
    bound[ENV_IDENTITY_SHA256] = hashlib.sha256(json.dumps(
        identity_values, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    values.update(bound)
    return values


def validate_bound_environment(environment: Mapping[str, str], *,
                               repository_root: Path = ROOT) -> Path:
    repository = Path(repository_root).resolve()
    java_home = (repository / ".venv/Library").resolve()
    expected = {
        ENV_SCHEMA: "mc2p.d128-repository-jdk.v1",
        ENV_JAVA: str(java_home / "bin/java.exe"),
        ENV_JAVAC: str(java_home / "bin/javac.exe"),
        ENV_CANONICAL_HOME: str(java_home / "lib/jvm"),
        ENV_CANONICAL_JAVA: str(java_home / "lib/jvm/bin/java.exe"),
    }
    if any(environment.get(name) != value for name, value in expected.items()):
        raise ExportViolation("D128 repository JDK environment identity differs")
    path_entries = environment.get("PATH", "").split(os.pathsep)
    if (Path(environment.get("JAVA_HOME", "")).resolve() != java_home
            or not path_entries
            or Path(path_entries[0]).resolve() != java_home / "bin"):
        raise ExportViolation("D128 repository JDK process environment differs")
    paths = {
        ENV_JAVA: Path(expected[ENV_JAVA]),
        ENV_JAVAC: Path(expected[ENV_JAVAC]),
        ENV_CANONICAL_JAVA: Path(expected[ENV_CANONICAL_JAVA]),
    }
    for path in (java_home, java_home / "lib/jvm", *paths.values()):
        _no_links(path)
    hashes = {
        ENV_JAVA_SHA256: _sha256(paths[ENV_JAVA]),
        ENV_JAVAC_SHA256: _sha256(paths[ENV_JAVAC]),
        ENV_CANONICAL_JAVA_SHA256: _sha256(paths[ENV_CANONICAL_JAVA]),
    }
    if any(environment.get(name) != value for name, value in hashes.items()):
        raise ExportViolation("D128 repository JDK environment hash differs")
    if not environment.get(ENV_VENDOR) or not re.match(
            r"21(?:\.|$)", environment.get(ENV_VERSION, "")):
        raise ExportViolation("D128 repository JDK canonical properties differ")
    identity_values = {
        name: environment.get(name) for name in (
            ENV_SCHEMA, ENV_JAVA, ENV_JAVAC, ENV_CANONICAL_HOME,
            ENV_CANONICAL_JAVA, ENV_VENDOR, ENV_VERSION,
            ENV_JAVA_SHA256, ENV_JAVAC_SHA256, ENV_CANONICAL_JAVA_SHA256,
        )
    }
    identity_hash = hashlib.sha256(json.dumps(
        identity_values, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if environment.get(ENV_IDENTITY_SHA256) != identity_hash:
        raise ExportViolation("D128 repository JDK environment binding differs")
    return paths[ENV_CANONICAL_JAVA].resolve()
