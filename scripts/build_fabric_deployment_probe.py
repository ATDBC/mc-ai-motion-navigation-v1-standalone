"""Build the independent client-only Fabric jar from pinned cached dependencies only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid
from zipfile import ZipFile, BadZipFile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

PROJECT = ROOT / "deployment/fabric-observation-probe"
SHARED_ROOTS = ("mc2p/backends/runtime_overlays/mc121_actions", "mc2p/backends/runtime_overlays/mc121_observation",
                "mc2p/backends/runtime_overlays/mc121_diagnostics")
ADAPTER_ROOT = "deployment/fabric-observation-probe/src/main/java"
MIXINS = {"BehaviorInputMixin", "BehaviorPlayerMixin", "BehaviorKeyboardMixin", "BehaviorMouseMixin",
          "WindowOffScreenMixin", "GameRendererMixin", "HandledScreenRenderMixin", "ScreenHandlerPropertiesMixin",
          "ScreenshotGuardMixin", "NativeImageGuardMixin", "ClientClockTickMixin", "ClientClockWorldMixin", "ClientClockPacketMixin"}


def inspect_classpath(entries: object, *, repository_root: Path = ROOT) -> int:
    """Verify actual JavaCompile files, including local file dependencies absent from coordinates."""
    if type(entries) is not list or not entries:
        raise ValueError("missing actual compile classpath")
    seen = set()
    allowed = (PurePosixPath(".gradle/caches"),
               PurePosixPath("deployment/fabric-observation-probe/.gradle/loom-cache"))
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("invalid compile classpath entry")
        name, size, digest = entry["path"], entry["size"], entry["sha256"]
        if (type(name) is not str or not name or "\\" in name or ":" in name
                or type(size) is not int or size <= 0 or type(digest) is not str
                or re.fullmatch("[0-9a-f]{64}", digest) is None):
            raise ValueError("invalid compile classpath path/hash/size")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name
                or relative.suffix != ".jar" or name.lower() in seen
                or "craftground" in name.lower()
                or not any(relative.is_relative_to(root) for root in allowed)):
            raise ValueError("compile classpath escaped approved cache roots or is duplicated")
        seen.add(name.lower())
        path = (repository_root / name).absolute()
        try:
            # Inspect unresolved ancestors too: resolving would hide a Windows junction.
            for component in (*reversed(path.parents), path):
                info = component.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("compile classpath contains a symlink/reparse point")
            if not path.is_file() or path.stat().st_size != size:
                raise ValueError("compile classpath file size drift")
            with path.open("rb") as stream:
                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_hash != digest:
                raise ValueError("compile classpath hash drift")
            with ZipFile(path) as archive:
                names = archive.namelist()
                manifests = [name for name in names if name.lower() == "meta-inf/manifest.mf"]
                if len(manifests) > 1:
                    raise ValueError("duplicate or ambiguous JAR manifests")
                if manifests:
                    manifest = archive.read(manifests[0]).decode("utf-8", errors="strict")
                    # JAR continuations start with one space. javac follows Class-Path
                    # implicitly, bypassing JavaCompile.classpath.files and its hashes.
                    unfolded = re.sub(r"(?:\r\n|\n|\r) ", "", manifest)
                    for line in unfolded.splitlines():
                        key, _, value = line.partition(":")
                        if key.lower() == "class-path" and value.strip():
                            raise ValueError("implicit manifest Class-Path is unsupported")
                if any(name.startswith("com/kyhsgeekcode/") for name in names):
                    raise ValueError("CraftGround classes in actual compile classpath")
                if "fabric.mod.json" in names:
                    descriptor = json.loads(archive.read("fabric.mod.json"))
                    if "craftground" in str(descriptor.get("id", "")).lower():
                        raise ValueError("CraftGround mod in actual compile classpath")
        except (OSError, BadZipFile) as error:
            raise ValueError("compile classpath file unavailable or invalid") from error
    return len(entries)


def inspect_probe(jar: Path) -> dict[str, object]:
    """Fail closed on artifact/source-root/classpath drift; no source string success markers."""
    with ZipFile(jar) as archive:
        names = set(archive.namelist())
        descriptor = json.loads(archive.read("fabric.mod.json"))
        client_only = (descriptor["environment"] == "client"
                       and descriptor["entrypoints"] == {"client": ["com.mc2p.deployment.DeploymentObservationProbe"]})
        provenance = json.loads(archive.read("mc2p-compiled-sources.json"))
        if provenance["schema_version"] != "mc2p.compiled_sources.v2":
            raise ValueError("invalid compiled source schema")
        verified_classpath_files = inspect_classpath(provenance.get("classpath_files"))
        expected_sources = {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in (*SHARED_ROOTS, ADAPTER_ROOT) for path in (ROOT / directory).rglob("*.java")}
        shared_sources_match = (provenance["sources"] == expected_sources
                                and set(provenance["source_roots"]) == {*SHARED_ROOTS, ADAPTER_ROOT})
        dependencies = provenance["classpath"]
        no_craftground = (bool(dependencies) and not any("craftground" in dep.lower() for dep in dependencies)
                         and not any(name.startswith("com/kyhsgeekcode/") for name in names))
        shared_names = {"com/mc2p/" + directory.rsplit("mc121_", 1)[1]
                        + "/" + path.stem + ".class"
                        for directory in SHARED_ROOTS for path in (ROOT / directory).glob("*.java")}
        shared_count = len(names & shared_names)
        mixins = json.loads(archive.read("mc2p-deployment.mixins.json"))
        required_mixins = (descriptor.get("mixins") == ["mc2p-deployment.mixins.json"]
                           and mixins.get("package") == "com.mc2p.deployment.mixin"
                           and mixins.get("refmap") == "mc2p-deployment.refmap.json"
                           and mixins["required"] is True and set(mixins["client"]) == MIXINS
                           and mixins["injectors"]["defaultRequire"] == 1
                           and "mc2p-deployment.refmap.json" in names
                           and all("com/mc2p/deployment/mixin/" + name + ".class" in names for name in MIXINS))
        evidence = dict(client_only=client_only, shared_sources_match=shared_sources_match,
                        no_craftground_dependency=no_craftground, shared_class_count=shared_count,
                        required_mixins_present=required_mixins,
                        verified_classpath_file_count=verified_classpath_files)
        if not all((client_only, shared_sources_match, no_craftground, required_mixins,
                    shared_names <= names, shared_count == len(shared_names))):
            raise ValueError(f"independent artifact provenance/manifest validation failed: {evidence}")
        return evidence


def build_probe(*, timeout_seconds: float = 180) -> Path:
    # Reuse the existing exact PID/create_time build supervisor, not its CraftGround launcher.
    from scripts.probe_craftground_timing_parallel import run_bounded_process
    from scripts.control_probe_core import write_json_atomic
    from mc2p.runtime.trace import trace_projection

    gradle, = (ROOT / ".gradle/wrapper/dists/gradle-8.8-bin").glob("*/gradle-8.8/bin/gradle.bat")
    environment = dict(os.environ, JAVA_HOME=str(ROOT / ".venv/Library"), GRADLE_USER_HOME=str(ROOT / ".gradle"))
    log_dir = ROOT / "artifacts/fabric-deployment-build" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8])
    log_dir.mkdir(parents=True, exist_ok=False)
    result = run_bounded_process(
        [str(gradle), "--offline", "--no-daemon", "--console=plain", "build"], cwd=PROJECT,
        environment=environment, log_path=log_dir / "build.log", timeout_seconds=timeout_seconds)
    write_json_atomic(log_dir / "result.json", trace_projection(result))
    if result.return_code != 0 or result.primary_failure or result.cleanup_failures or not result.process_stopped:
        raise RuntimeError(f"independent offline build failed; evidence: {log_dir}")
    jar = PROJECT / "build/libs/mc2p-deployment-probe-0.1.0.jar"
    evidence = inspect_probe(jar)
    write_json_atomic(log_dir / "artifact.json", {"jar": str(jar), "sha256": hashlib.sha256(jar.read_bytes()).hexdigest(), **evidence})
    return jar


if __name__ == "__main__":
    print(build_probe())
    print("FABRIC_DEPLOYMENT_BUILD_OK")
