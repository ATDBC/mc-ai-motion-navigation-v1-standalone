"""Isolated client-only human controls. Build without launching any game."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid
from zipfile import ZipFile, BadZipFile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}: sys.path.insert(0, str(ROOT))
PROJECT = ROOT / "deployment/fabric-demo-control"
SOURCE_ROOT = "deployment/fabric-demo-control/src/main/java"

def _locked_api_modules() -> dict[str, tuple[str, str]]:
    """Read exact nested module identities from the pinned 0.100.6+1.21 distribution."""
    path = ROOT/'.gradle/caches/modules-2/files-2.1/net.fabricmc.fabric-api/fabric-api/0.100.6+1.21/dd92d95977c960affefc6c29e3ee946ce7b4723a/fabric-api-0.100.6+1.21.jar'
    from scripts.visibility_fixture_world import _no_links
    _no_links(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != '6de57bcceae257d0db5f8f1d5b1a0575ab17fe94c86221c7af310cd26869f808':
        raise ValueError('pinned Fabric API distribution changed')
    result = {}
    def visit(data: bytes):
        with ZipFile(io.BytesIO(data)) as archive:
            descriptor = json.loads(archive.read('fabric.mod.json'))
            identity = (descriptor['version'], hashlib.sha256(data).hexdigest())
            if descriptor['id'] in result and result[descriptor['id']] != identity:
                raise ValueError('ambiguous locked Fabric API module')
            result[descriptor['id']] = identity
            for nested in descriptor.get('jars', []): visit(archive.read(nested['file']))
    visit(raw)
    # Locked API POM also declares this development module, absent from the distribution's nested jars.
    result['fabric-gametest-api-v1'] = ('2.0.2+6fc22b99d1', 'aff72f2768256c666718c9c65e080ab52c0974e76de15ac013b26095f71ab44b')
    return result


def _approved_api_path(name: str, descriptor: dict, digest: str, modules: dict) -> bool:
    identifier = descriptor.get('id')
    if identifier not in modules: return False
    version, _nested_hash = modules[identifier]
    if descriptor.get('version') != version: return False
    original = f'.gradle/caches/modules-2/files-2.1/net.fabricmc.fabric-api/{identifier}/{version}/'
    remapped = ('deployment/fabric-demo-control/.gradle/loom-cache/remapped_mods/'
                f'net_fabricmc_yarn_1_21_1_21_build_9_v2/net/fabricmc/fabric-api/{identifier}/{version}/'
                f'{identifier}-{version}.jar')
    # Standalone Maven jars and nested distribution jars can have different ZIP metadata.
    # Exact pinned coordinates authorize the module; each actual jar still needs the caller's SHA-256 proof.
    return (name == remapped or (name.startswith(original)
            and re.fullmatch(r'[0-9a-f]{1,40}/'+re.escape(f'{identifier}-{version}.jar'), name[len(original):]) is not None))


def inspect_control_classpath(entries: object, *, repository_root: Path = ROOT) -> int:
    """Verify actual JavaCompile files, including local file dependencies absent from coordinates."""
    if type(entries) is not list or not entries:
        raise ValueError("missing actual compile classpath")
    seen = set()
    api_modules = None
    allowed = (PurePosixPath(".gradle/caches"),
               PurePosixPath("deployment/fabric-demo-control/.gradle/loom-cache"))
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
                if any(name.startswith(("com/kyhsgeekcode/", "com/mc2p/")) for name in names):
                    raise ValueError("CraftGround classes in actual compile classpath")
                if "fabric.mod.json" in names:
                    descriptor = json.loads(archive.read("fabric.mod.json"))
                    locked_loader_library = (descriptor.get('id') == 'mixinextras'
                        and name == '.gradle/caches/modules-2/files-2.1/io.github.llamalad7/mixinextras-fabric/0.3.5/'
                                    '3b577be20ea942610b3045e4f0cd909fa415a9d3/mixinextras-fabric-0.3.5.jar'
                        and digest == '743bf47e4fa24642f843b9f85a5f1ba5125fb4b7e656e96b9c010a5043396047')
                    locked_loader = (descriptor.get('id') == 'fabricloader' and descriptor.get('version') == '0.15.11'
                        and name == '.gradle/caches/modules-2/files-2.1/net.fabricmc/fabric-loader/0.15.11/'
                                    '9c2be53d84ec5c4fb58bb5b8d9c8a629047c9ee9/fabric-loader-0.15.11.jar'
                        and digest == 'f58fca271b5b48dd4f8faad792b09002439e8444d50af670b8493de3b2d85f08')
                    if api_modules is None: api_modules = _locked_api_modules()
                    if not (locked_loader or locked_loader_library or _approved_api_path(name, descriptor, digest, api_modules)):
                        raise ValueError(f"unapproved mod in actual human classpath: {descriptor.get('id')} at {name}")
        except (OSError, BadZipFile) as error:
            raise ValueError("compile classpath file unavailable or invalid") from error
    return len(entries)

def inspect_control_mod(jar: Path) -> dict:
    with ZipFile(jar) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate human mod archive entries")
        descriptor = json.loads(archive.read("fabric.mod.json"))
        if (descriptor.get("id") != "mc2p-demo-control" or descriptor.get("environment") != "client"
                or descriptor.get("entrypoints") != {"client": ["com.mc2p.democontrol.DemoControlMod"]}
                or "mixins" in descriptor or "jars" in descriptor
                or descriptor.get("depends") != {"fabricloader": "0.15.11", "minecraft": "1.21",
                                                 "java": ">=21", "fabric-api": "0.100.6+1.21"}):
            raise ValueError("human mod descriptor is not isolated/locked")
        if any(name.endswith(".class") and not name.startswith("com/mc2p/democontrol/") for name in names):
            raise ValueError("foreign/actor code in human mod")
        provenance = json.loads(archive.read("mc2p-compiled-sources.json"))
        expected = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (ROOT/SOURCE_ROOT).rglob("*.java")}
        required = {Path(name).stem + ".class" for name in expected}
        if (provenance.get("schema_version") != "mc2p.demo-compiled-sources.v1"
                or provenance.get("source_roots") != [SOURCE_ROOT] or not expected
                or provenance.get("sources") != expected
                or not required <= {name.rsplit("/", 1)[-1] for name in names}):
            raise ValueError("human compiled sources drifted")
        count = inspect_control_classpath(provenance.get("classpath_files"))
        return {"client_only": True, "actor_sources_absent": True, "shared_sources": 0,
                "source_count": len(expected), "verified_classpath_files": count}


def build_control_mod(*, timeout_seconds: float = 240) -> Path:
    from scripts.probe_craftground_timing_parallel import run_bounded_process
    from scripts.control_probe_core import write_json_atomic
    from mc2p.runtime.trace import trace_projection
    gradle, = (ROOT/".gradle/wrapper/dists/gradle-8.8-bin").glob("*/gradle-8.8/bin/gradle.bat")
    environment = {k: v for k, v in os.environ.items()
                   if k.upper() not in {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}
                   and not k.upper().startswith(("MC2P_", "FABRIC_"))}
    environment.update(JAVA_HOME=str(ROOT/".venv/Library"), GRADLE_USER_HOME=str(ROOT/".gradle"))
    log = ROOT/"artifacts/fabric-demo-control-build"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid.uuid4().hex[:8])
    log.mkdir(parents=True, exist_ok=False)
    result = run_bounded_process([str(gradle), "--offline", "--no-daemon", "--console=plain", "build", "exportClientLaunch"],
        cwd=PROJECT, environment=environment, log_path=log/"build.log", timeout_seconds=timeout_seconds)
    write_json_atomic(log/"result.json", trace_projection(result))
    if result.return_code != 0 or result.primary_failure or result.cleanup_failures or not result.process_stopped:
        raise RuntimeError(f"human control offline build failed; retained evidence: {log}")
    jar = PROJECT/"build/libs/mc2p-demo-control-0.1.0.jar"
    try:
        evidence = inspect_control_mod(jar)
    except Exception as error:
        write_json_atomic(log/'validation.json', dict(status='failed', reason=str(error)))
        raise
    write_json_atomic(log/"artifact.json", {"jar": str(jar), "sha256": hashlib.sha256(jar.read_bytes()).hexdigest(), **evidence})
    return jar


if __name__ == "__main__":
    print(build_control_mod())
    print("DEMO_CONTROL_BUILD_OK")
