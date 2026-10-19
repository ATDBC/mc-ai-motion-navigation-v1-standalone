"""Read-only launch/cache verification and bounded offline Loom recipe export."""
from __future__ import annotations

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

from scripts.build_fabric_deployment_probe import PROJECT, build_probe, inspect_classpath, inspect_probe
from scripts.visibility_fixture_world import _no_links

BUILD = "deployment/fabric-observation-probe/build"
LOOM = "deployment/fabric-observation-probe/.gradle/loom-cache"
DIRECTORIES = {BUILD + "/classes/java/main", BUILD + "/resources/main"}


def _hash(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def _verify_file(path: Path, evidence: dict) -> None:
    _no_links(path)
    if (type(evidence) is not dict or set(evidence) != {"size", "sha256"}
            or type(evidence["size"]) is not int or evidence["size"] < 0
            or path.stat().st_size != evidence["size"] or _hash(path) != evidence["sha256"]):
        raise ValueError("launch input file changed")


def inspect_launch(value: dict) -> dict:
    if (type(value) is not dict or set(value) != {"schema_version", "main_class", "jvm_args", "game_args", "environment", "classpath", "remap_classpath", "auxiliary"}
            or value["schema_version"] != "mc2p.deployment_launch.v1"
            or value["main_class"] != "net.fabricmc.devlaunchinjector.Main" or value["environment"] != {}):
        raise ValueError("invalid independent launch schema/main/environment")
    expected_jvm = {
        "-Dfabric.dli.config=" + str(PROJECT / ".gradle/loom-cache/launch.cfg"),
        "-Dfabric.dli.env=client", "-Dfabric.dli.main=net.fabricmc.loader.impl.launch.knot.KnotClient",
    }
    if type(value["jvm_args"]) is not list or len(value["jvm_args"]) != 3 or set(value["jvm_args"]) != expected_jvm:
        raise ValueError("unexpected launch JVM argument")
    if value["game_args"] != []:
        raise ValueError("unexpected implicit game arguments")
    if type(value["classpath"]) is not list or not value["classpath"]:
        raise ValueError("missing launch classpath")
    jars, directories = [], set()
    dev_jar = PROJECT / "build/devlibs/mc2p-deployment-probe-0.1.0-dev.jar"
    inspect_probe(PROJECT / "build/libs/mc2p-deployment-probe-0.1.0.jar")
    _no_links(dev_jar)
    inspect_probe(dev_jar)
    with ZipFile(dev_jar) as archive:
        for item in value["classpath"]:
            if type(item) is not dict:
                raise ValueError("invalid launch classpath entry")
            if item.get("kind") == "jar" and set(item) == {"kind", "path", "size", "sha256"}:
                jars.append({key: item[key] for key in ("path", "size", "sha256")})
            elif (item.get("kind") == "directory" and set(item) == {"kind", "path", "files"}
                    and item["path"] in DIRECTORIES and item["path"] not in directories):
                directories.add(item["path"])
                directory = ROOT / item["path"]
                _no_links(directory)
                members = {path.relative_to(directory).as_posix(): path for path in directory.rglob("*") if path.is_file()}
                if set(members) != set(item["files"]) or not members:
                    raise ValueError("runtime project directory changed")
                for name, path in members.items():
                    _verify_file(path, item["files"][name])
                    if path.read_bytes() != archive.read(name):
                        raise ValueError("runtime project output differs from compiled dev jar")
            else:
                raise ValueError("unapproved runtime classpath entry")
    if directories != DIRECTORIES:
        raise ValueError("missing project classes or resources")
    jar_count = inspect_classpath(jars)
    if (type(value["auxiliary"]) is not dict
            or set(value["auxiliary"]) != {LOOM + "/launch.cfg", LOOM + "/log4j.xml", LOOM + "/remapClasspath.txt"}):
        raise ValueError("missing Loom launch configuration")
    for name, evidence in value["auxiliary"].items():
        if name not in {LOOM + "/launch.cfg", LOOM + "/log4j.xml", LOOM + "/remapClasspath.txt"}:
            raise ValueError("unapproved launch auxiliary file")
        _verify_file(ROOT / name, evidence)
    expected_config = ["commonProperties", "\tfabric.development=true",
        "\tfabric.remapClasspathFile=" + str(ROOT / LOOM / "remapClasspath.txt"),
        "\tlog4j.configurationFile=" + str(ROOT / LOOM / "log4j.xml"), "\tlog4j2.formatMsgNoLookups=true",
        "clientArgs", "\t--assetIndex", "\t1.21-17", "\t--assetsDir", "\t" + str(ROOT / ".gradle/caches/fabric-loom/assets")]
    if (ROOT / LOOM / "launch.cfg").read_text("utf-8").splitlines() != expected_config:
        raise ValueError("implicit DLI launch settings differ from approved client settings")
    remap_count = inspect_classpath(value["remap_classpath"])
    actual_remap = (ROOT / LOOM / "remapClasspath.txt").read_text("utf-8").split(os.pathsep)
    if actual_remap != [str(ROOT / entry["path"]) for entry in value["remap_classpath"]]:
        raise ValueError("actual remap classpath differs from verified files")
    return dict(jar_count=jar_count, remap_jar_count=remap_count,
                project_directory_count=len(directories), client_only=True)


def verify_assets() -> dict:
    metadata_path = ROOT / ".gradle/caches/fabric-loom/1.21/minecraft-info.json"
    _no_links(metadata_path)
    metadata = json.loads(metadata_path.read_text("utf-8"))
    if metadata["id"] != "1.21" or metadata["assetIndex"]["sha1"] != "6ae5eb70fe411facbb5c6c66003475c07a251a76":
        raise ValueError("asset metadata is not the locked Minecraft release")
    assets = ROOT / ".gradle/caches/fabric-loom/assets"
    index = assets / "indexes/1.21-17.json"
    _no_links(index)
    if index.stat().st_size != metadata["assetIndex"]["size"] or _hash(index, "sha1") != metadata["assetIndex"]["sha1"]:
        raise ValueError("asset index failed upstream hash/size verification")
    objects = json.loads(index.read_text("utf-8"))["objects"]
    for entry in objects.values():
        digest, size = entry["hash"], entry["size"]
        if type(digest) is not str or re.fullmatch("[0-9a-f]{40}", digest) is None or type(size) is not int or size < 0:
            raise ValueError("invalid asset object metadata")
        path = assets / "objects" / digest[:2] / digest
        _no_links(path)
        if path.stat().st_size != size or _hash(path, "sha1") != digest:
            raise ValueError("asset object failed upstream hash/size verification")
    return dict(index_sha1=metadata["assetIndex"]["sha1"], object_count=len(objects), metadata_sha256=_hash(metadata_path))


def export_launch(*, timeout_seconds: float = 180) -> Path:
    from scripts.bounded_process import run_bounded_process
    from scripts.control_probe_core import write_json_atomic
    from mc2p.runtime.trace import trace_projection
    build_probe(timeout_seconds=timeout_seconds)
    gradle, = (ROOT / ".gradle/wrapper/dists/gradle-8.8-bin").glob("*/gradle-8.8/bin/gradle.bat")
    log_dir = ROOT / "artifacts/fabric-deployment-launch" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8])
    log_dir.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ, JAVA_HOME=str(ROOT / ".venv/Library"), GRADLE_USER_HOME=str(ROOT / ".gradle"))
    result = run_bounded_process([str(gradle), "--offline", "--no-daemon", "--console=plain", "exportClientLaunch"],
        cwd=PROJECT, environment=environment, log_path=log_dir / "export.log", timeout_seconds=timeout_seconds)
    write_json_atomic(log_dir / "result.json", trace_projection(result))
    if result.return_code != 0 or result.primary_failure or result.cleanup_failures or not result.process_stopped:
        raise RuntimeError(f"independent launch export failed: {log_dir}")
    path = PROJECT / "build/launch/client-launch.json"
    value = json.loads(path.read_text("utf-8"))
    write_json_atomic(log_dir / "launch.json", value)
    write_json_atomic(log_dir / "verified.json", {**inspect_launch(value), "assets": verify_assets()})
    return path


if __name__ == "__main__":
    print(export_launch())
    print("FABRIC_DEPLOYMENT_LAUNCH_READY")
