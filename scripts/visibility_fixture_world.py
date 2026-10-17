"""Offline test save generation and verified fresh installation; never an actor capability."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory

from scripts.control_probe_core import write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("scripts/java/VisibilityFixtureBuilder.java", "scripts/fixtures/visibility-level.snbt",
           "tests/java/VanillaObjectTestHost.java")
WORLD_FILES = frozenset({"level.dat", "region/r.0.0.mca", "region/r.-1.0.mca",
                         "entities/r.0.0.mca", "entities/r.-1.0.mca"})


def _identity(seed: int, level_name: str) -> None:
    if type(seed) is not int or not -(2 ** 63) <= seed < 2 ** 63:
        raise ValueError("fixture seed must be a signed 64-bit integer")
    if not isinstance(level_name, str) or not re.fullmatch(r"mc2p-visibility-[a-z0-9-]{1,80}", level_name):
        raise ValueError("invalid visibility fixture identity")


def _no_links(path: Path) -> None:
    # Do not resolve first: that would erase evidence of an ancestor junction.
    absolute = path.absolute()
    for component in (*reversed(absolute.parents), absolute):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"fixture path must not contain a symlink/reparse point: {component}")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sources() -> dict[str, str]:
    return {name: _hash(ROOT / name) for name in SOURCES}


def _classpath() -> str:
    metadata = json.loads((ROOT / ".gradle/caches/fabric-loom/1.21/minecraft-info.json").read_text("utf-8"))
    if metadata["id"] != "1.21":
        raise ValueError("only the locked Minecraft 1.21 toolchain is supported")
    named = ROOT / ".gradle/caches/fabric-loom/minecraftMaven/net/minecraft/minecraft-merged/1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2/minecraft-merged-1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2.jar"
    if not named.is_file():
        raise FileNotFoundError(named)
    jars = [named]
    coordinates = [entry["name"].split(":") for entry in metadata["libraries"]]
    coordinates += [["net.fabricmc", "fabric-loader", "0.15.11"], ["org.ow2.asm", "asm", "9.6"]]
    for parts in coordinates:
        if len(parts) != 3 or parts[0] == "ca.weblite":
            continue
        group, name, version = parts
        matches = list((ROOT / ".gradle/caches/modules-2/files-2.1" / group / name / version).glob(f"*/{name}-{version}.jar"))
        if len(matches) != 1:
            raise FileNotFoundError(f"locked Java library missing or ambiguous: {parts}")
        jars.append(matches[0])
    return os.pathsep.join(map(str, jars))


def _run(command: list[str], directory: Path, label: str) -> None:
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=45)
    (directory / f"{label}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (directory / f"{label}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"native fixture {label} failed ({result.returncode}): {result.stdout}\n{result.stderr}")


def _world_hashes(world: Path) -> dict[str, str]:
    _no_links(world)
    found: dict[str, str] = {}
    for directory, directories, files in os.walk(world, followlinks=False):
        parent = Path(directory)
        for name in directories:
            child = parent / name
            _no_links(child)
            if child.relative_to(world).as_posix() not in {"region", "entities"}:
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


def _verify_saved_identity(level: Path, seed: int, level_name: str) -> None:
    # Never execute fixture/classes: artifacts are data, not a trusted tool distribution.
    before = _sources()
    classpath = _classpath()
    java = ROOT / ".venv/Library/bin"
    with TemporaryDirectory(prefix="mc2p-fixture-verifier-") as directory:
        work = Path(directory)
        _run([str(java / "javac.exe"), "-encoding", "UTF-8", "-proc:none", "-cp", classpath,
              "-d", str(work), str(ROOT / SOURCES[0]), str(ROOT / SOURCES[2])], work, "compile")
        try:
            _run([str(java / "java.exe"), "-cp", str(work) + os.pathsep + classpath,
                  "VanillaObjectTestHost", "VisibilityFixtureBuilder", "--verify-level", str(level), str(seed), level_name], work, "verify")
        except RuntimeError as error:
            raise ValueError(f"fixture saved identity verification failed: {error}") from error
    if before != _sources():
        raise ValueError("fixture source changed during identity verification")


def build_visibility_fixture(destination: Path, *, seed: int, level_name: str) -> dict:
    _identity(seed, level_name)
    destination = Path(destination).absolute()
    _no_links(destination.parent)
    before = _sources()
    classpath = _classpath()
    destination.mkdir()  # Never overwrite/reuse any existing target, including interrupted builds.
    compiled = destination / "classes"
    compiled.mkdir()
    java = ROOT / ".venv/Library/bin"
    _run([str(java / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8", "-proc:none",
          "-cp", classpath, "-d", str(compiled), str(ROOT / SOURCES[0]), str(ROOT / SOURCES[2])], destination, "compile")
    _run([str(java / "java.exe"), "-cp", str(compiled) + os.pathsep + classpath,
          "VanillaObjectTestHost", "VisibilityFixtureBuilder", str(ROOT / SOURCES[1]),
          str(destination / "world"), str(seed), level_name], destination, "build")
    if before != _sources():
        raise ValueError("fixture source changed during generation")
    manifest = {"schema_version": "mc2p.visibility-fixture.v1", "seed": seed, "level_name": level_name,
                "minecraft_data_version": 3953, "purpose": "test_initial_state_not_actor_capability",
                "source_fingerprints": before, "files": _world_hashes(destination / "world")}
    write_json_atomic(destination / "fixture-manifest.json", manifest)
    return manifest


def install_visibility_fixture(fixture: Path, saves: Path) -> Path:
    fixture, saves = Path(fixture).absolute(), Path(saves).absolute()
    _no_links(fixture)
    _no_links(saves)
    manifest_path = fixture / "fixture-manifest.json"
    _no_links(manifest_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "seed", "level_name", "minecraft_data_version", "purpose", "source_fingerprints", "files"}:
        raise ValueError("invalid fixture manifest fields")
    _identity(manifest["seed"], manifest["level_name"])
    if (manifest["schema_version"] != "mc2p.visibility-fixture.v1" or manifest["minecraft_data_version"] != 3953
            or manifest["purpose"] != "test_initial_state_not_actor_capability"
            or manifest["source_fingerprints"] != _sources()
            or manifest["files"] != _world_hashes(fixture / "world")):
        raise ValueError("fixture provenance/content mismatch")
    _verify_saved_identity(fixture / "world/level.dat", manifest["seed"], manifest["level_name"])
    target = saves / manifest["level_name"]
    target.mkdir()  # Reject an old save before any copy, never merge into user or test worlds.
    for relative in sorted(manifest["files"]):
        output = target / relative
        output.parent.mkdir(exist_ok=True)
        shutil.copyfile(fixture / "world" / relative, output)
    if _world_hashes(target) != manifest["files"]:
        raise ValueError("fixture changed during installation; incomplete target is not reusable")
    return target
