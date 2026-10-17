"""Native offline fixture tooling; its map/player initializers never enter the skill."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import uuid

from scripts.control_probe_core import write_json_atomic
from scripts.fabric_deployment_sandbox import JAVA
from scripts.visibility_fixture_world import ROOT, _classpath, _hash, _no_links, _run

CASES = ("static", "moving", "obstacle", "occlusion", "hazard", "interrupt")
PLAYERS = ("MC2PFollower", "MC2PLeader")
SOURCES = ("scripts/java/FollowFixtureBuilder.java", "scripts/fixtures/visibility-level.snbt",
           "tests/java/VanillaObjectTestHost.java")


def offline_uuid(name: str) -> str:
    if name not in PLAYERS:
        raise ValueError("undeclared fixture player")
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:"+name).encode()).digest(), version=3))


def _validate(seed: int, scenario: str) -> None:
    if type(seed) is not int or seed not in (21001, 21002, 21003) or scenario not in CASES:
        raise ValueError("undeclared follow seed/scenario")


def _sources() -> dict:
    return {name: _hash(ROOT/name) for name in SOURCES}


def _files(world: Path) -> dict:
    _no_links(world)
    expected = {"level.dat", *("playerdata/"+offline_uuid(n)+".dat" for n in PLAYERS),
                *(f"region/r.{x}.{z}.mca" for x in (-1, 0) for z in (-1, 0))}
    result = {}
    for directory, directories, names in os.walk(world, followlinks=False):
        for name in directories:
            path = Path(directory)/name
            _no_links(path)
            if path.relative_to(world).as_posix() not in {"region", "playerdata"}:
                raise ValueError("unexpected fixture directory")
        for name in names:
            path = Path(directory)/name
            _no_links(path)
            if not path.is_file(): raise ValueError("nonregular fixture file")
            result[path.relative_to(world).as_posix()] = _hash(path)
    if set(result) != expected:
        raise ValueError("fixture file set differs")
    return result


def _native(mode: str, world: Path, seed: int, scenario: str, work: Path) -> None:
    classpath = _classpath()
    _run([str(JAVA.parent/"javac.exe"), "-encoding", "UTF-8", "-proc:none", "-cp", classpath,
          "-d", str(work), str(ROOT/SOURCES[0]), str(ROOT/SOURCES[2])], work, "compile")
    _run([str(JAVA), "-cp", str(work)+os.pathsep+classpath, "VanillaObjectTestHost", "FollowFixtureBuilder",
          mode, str(ROOT/SOURCES[1]), str(world), str(seed), scenario], work, mode)


def build_follow_fixture(target: Path, *, seed: int, scenario: str) -> dict:
    _validate(seed, scenario)
    target = Path(target).absolute()
    _no_links(target.parent)
    before = _sources()
    target.mkdir(exist_ok=False)
    classes = target/"classes"
    classes.mkdir()
    _native("build", target/"world", seed, scenario, classes)
    if before != _sources(): raise ValueError("fixture sources changed during generation")
    manifest = dict(schema_version="mc2p.follow-fixture.v1", seed=seed, scenario=scenario,
                    purpose="offline_test_initial_state_not_actor_capability", source_fingerprints=before,
                    players={name: offline_uuid(name) for name in PLAYERS}, files=_files(target/"world"))
    write_json_atomic(target/"fixture-manifest.json", manifest)
    return manifest


def verify_follow_fixture(fixture: Path) -> dict:
    fixture = Path(fixture).absolute()
    _no_links(fixture/"fixture-manifest.json")
    manifest = json.loads((fixture/"fixture-manifest.json").read_text("utf-8"))
    if type(manifest) is not dict or set(manifest) != {"schema_version", "seed", "scenario", "purpose", "source_fingerprints", "players", "files"}:
        raise ValueError("invalid fixture manifest")
    _validate(manifest["seed"], manifest["scenario"])
    before = _sources()
    if (manifest["schema_version"] != "mc2p.follow-fixture.v1" or manifest["purpose"] != "offline_test_initial_state_not_actor_capability"
            or manifest["source_fingerprints"] != before or manifest["players"] != {name: offline_uuid(name) for name in PLAYERS}
            or manifest["files"] != _files(fixture/"world")):
        raise ValueError("follow fixture provenance/content mismatch")
    # Always compile trusted repository verifier; never execute classes from the artifact being checked.
    with TemporaryDirectory(prefix="mc2p-follow-verifier-") as temporary:
        try:
            _native("verify", fixture/"world", manifest["seed"], manifest["scenario"], Path(temporary))
        except RuntimeError as error:
            raise ValueError("native follow fixture verification failed: " + str(error)) from error
    if before != _sources() or manifest["files"] != _files(fixture/"world"):
        raise ValueError("fixture changed during verification")
    return manifest


def install_follow_fixture(fixture: Path, target: Path) -> Path:
    target = Path(target).absolute()
    _no_links(target.parent)
    if target.exists(): raise FileExistsError("follow world must be new")
    manifest = verify_follow_fixture(fixture)
    target.mkdir(exist_ok=False)
    for relative in sorted(manifest["files"]):
        path = target/relative
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(Path(fixture)/"world"/relative, path)
    if _files(target) != manifest["files"]:
        raise ValueError("follow fixture changed during installation")
    return target
