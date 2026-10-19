"""Build and verify the server-only C1 acceptance fixture from pinned caches."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
PROJECT = ROOT / "deployment/fabric-c1-fixture-server"
SOURCE_ROOT = PROJECT / "src/main/java"


@dataclass(frozen=True, slots=True)
class C1FixtureArtifact:
    jar: Path
    sha256: str
    source_hashes: tuple[tuple[str, str], ...]
    launch: dict[str, object]


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_fixture(jar: Path) -> dict[str, object]:
    with ZipFile(jar) as archive:
        names = set(archive.namelist())
        descriptor = json.loads(archive.read("fabric.mod.json"))
        provenance = json.loads(archive.read("mc2p-c1-fixture-sources.json"))
    server_only = (
        descriptor.get("environment") == "server"
        and descriptor.get("entrypoints") == {"main": ["com.mc2p.fixture.C1FixtureServer"]}
        and "com/mc2p/fixture/C1FixtureServer.class" in names
    )
    expected = {
        path.relative_to(ROOT).as_posix(): _hash(path)
        for path in SOURCE_ROOT.rglob("*.java")
    }
    sources_match = (
        provenance.get("schema_version") == "mc2p.c1-fixture-sources.v1"
        and provenance.get("sources") == expected
    )
    classpath = provenance.get("classpath_files")
    if type(classpath) is not list or not classpath:
        raise ValueError("C1 fixture compile classpath evidence missing")
    for item in classpath:
        if (type(item) is not dict or set(item) != {"path", "size", "sha256"}
                or type(item["path"]) is not str or ".." in Path(item["path"]).parts
                or type(item["size"]) is not int or item["size"] <= 0
                or re.fullmatch("[0-9a-f]{64}", item["sha256"]) is None):
            raise ValueError("invalid C1 fixture classpath evidence")
        path = ROOT / item["path"]
        if not path.is_file() or path.stat().st_size != item["size"] or _hash(path) != item["sha256"]:
            raise ValueError("C1 fixture classpath drift")
    if not server_only or not sources_match:
        raise ValueError("C1 fixture artifact does not match server sources")
    return {
        "server_only": server_only, "sources_match": sources_match,
        "verified_classpath_file_count": len(classpath),
    }


def inspect_server_launch(value: object) -> dict[str, object]:
    if (type(value) is not dict
            or set(value) != {"schema_version", "main_class", "jvm_args", "game_args",
                              "environment", "classpath"}
            or value["schema_version"] != "mc2p.c1-fixture-launch.v1"
            or value["main_class"] != "net.fabricmc.devlaunchinjector.Main"
            or value["environment"] != {} or value["game_args"] != ["nogui"]):
        raise ValueError("invalid C1 server launch recipe")
    arguments = value["jvm_args"]
    if (type(arguments) is not list
            or "-Dfabric.dli.env=server" not in arguments
            or "-Dfabric.dli.main=net.fabricmc.loader.impl.launch.knot.KnotServer" not in arguments):
        raise ValueError("C1 launch is not a Fabric server")
    entries = value["classpath"]
    if type(entries) is not list or not entries:
        raise ValueError("C1 server launch classpath missing")
    fixture_output = False
    for item in entries:
        if type(item) is not dict or item.get("kind") not in {"jar", "directory"}:
            raise ValueError("invalid C1 server launch classpath entry")
        path = ROOT / item["path"]
        if item["kind"] == "jar":
            if (set(item) != {"kind", "path", "size", "sha256"}
                    or not path.is_file() or path.stat().st_size != item["size"]
                    or _hash(path) != item["sha256"]):
                raise ValueError("C1 server launch jar drift")
        else:
            if set(item) != {"kind", "path", "files"} or not path.is_dir():
                raise ValueError("C1 server launch directory drift")
            members = {member.relative_to(path).as_posix(): member
                       for member in path.rglob("*") if member.is_file()}
            if set(members) != set(item["files"]):
                raise ValueError("C1 server launch directory members changed")
            for name, member in members.items():
                evidence = item["files"][name]
                if member.stat().st_size != evidence["size"] or _hash(member) != evidence["sha256"]:
                    raise ValueError("C1 server launch directory file drift")
            fixture_output = fixture_output or item["path"] in {
                "deployment/fabric-c1-fixture-server/build/classes/java/main",
                "deployment/fabric-c1-fixture-server/build/resources/main",
            }
    if not fixture_output:
        raise ValueError("C1 fixture output absent from server launch")
    return {"environment": "server", "classpath_entry_count": len(entries)}


def build_fixture(*, timeout_seconds: float = 180) -> C1FixtureArtifact:
    from scripts.bounded_process import run_bounded_process
    from scripts.control_probe_core import write_json_atomic
    from mc2p.runtime.trace import trace_projection

    gradle, = (ROOT / ".gradle/wrapper/dists/gradle-8.8-bin").glob("*/gradle-8.8/bin/gradle.bat")
    environment = dict(os.environ, JAVA_HOME=str(ROOT / ".venv/Library"),
                       GRADLE_USER_HOME=str(ROOT / ".gradle"))
    log_dir = ROOT / "artifacts/fabric-c1-fixture-build" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    )
    log_dir.mkdir(parents=True, exist_ok=False)
    result = run_bounded_process(
        [str(gradle), "--offline", "--no-daemon", "--console=plain", "build", "exportServerLaunch"],
        cwd=PROJECT, environment=environment, log_path=log_dir / "build.log",
        timeout_seconds=timeout_seconds,
    )
    write_json_atomic(log_dir / "result.json", trace_projection(result))
    if result.return_code != 0 or result.primary_failure or result.cleanup_failures or not result.process_stopped:
        raise RuntimeError(f"C1 fixture offline build failed; evidence: {log_dir}")
    jar = PROJECT / "build/libs/mc2p-c1-fixture-server-0.1.0.jar"
    evidence = inspect_fixture(jar)
    launch = json.loads((PROJECT / "build/launch/server-launch.json").read_text("utf-8"))
    launch_evidence = inspect_server_launch(launch)
    artifact = C1FixtureArtifact(
        jar.absolute(), _hash(jar), tuple(sorted(
            (path.relative_to(ROOT).as_posix(), _hash(path))
            for path in SOURCE_ROOT.rglob("*.java")
        )), launch,
    )
    write_json_atomic(log_dir / "artifact.json", {
        "jar": str(artifact.jar), "sha256": artifact.sha256,
        "source_hashes": dict(artifact.source_hashes), **evidence, "launch": launch_evidence,
    })
    return artifact


if __name__ == "__main__":
    print(build_fixture().jar)
    print("FABRIC_C1_FIXTURE_BUILD_OK")
