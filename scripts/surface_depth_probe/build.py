"""Build the pinned surface-depth native core and the retired diagnostic jar."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_motion_navigation_standalone import discover_java_tools


SOURCE = ROOT / "deployment/surface-depth-diagnostic"
BUILD = ROOT / "artifacts/surface-cache"
NATIVE_LIBRARY = BUILD / "native-build/Release/surface_cache.dll"


def build_native() -> Path:
    """Build the native visibility core used by the formal Fabric client."""
    if os.name != "nt":
        raise RuntimeError(
            "the surface-depth native build is only accepted on Windows in the current stage"
        )
    BUILD.mkdir(parents=True, exist_ok=True)
    cmake = Path("C:/Program Files/CMake/bin/cmake.exe")
    if not cmake.is_file():
        raise FileNotFoundError("the pinned CMake executable is unavailable")
    java_tools = discover_java_tools()
    java_home = java_tools.java.parent.parent
    environment = dict(
        os.environ,
        JAVA_HOME=str(java_home),
    )
    subprocess.run([
        str(cmake), "-S", str(SOURCE / "native"),
        "-B", str(BUILD / "native-build"),
        "-G", "Visual Studio 17 2022", "-A", "x64",
    ], check=True, env=environment)
    subprocess.run([
        str(cmake), "--build", str(BUILD / "native-build"),
        "--config", "Release",
    ], check=True, env=environment)
    if not NATIVE_LIBRARY.is_file():
        raise FileNotFoundError("surface-depth native build produced no library")
    # Exercise the exact package/class used by the formal Fabric provider. The
    # older default-package smoke below only preserves historical experiments.
    with tempfile.TemporaryDirectory(prefix="surface-jni-", dir=BUILD) as directory:
        classes = Path(directory)
        source = (ROOT / "mc2p/backends/runtime_overlays/mc121_surface/"
                  "com/mc2p/surface/CacheBridge.java")
        subprocess.run([
            str(java_tools.javac),
            "-d", str(classes), str(source),
        ], check=True, cwd=ROOT)
        subprocess.run([
            str(java_tools.java),
            "-cp", str(classes), "com.mc2p.surface.CacheBridge",
            str(NATIVE_LIBRARY),
        ], check=True, cwd=ROOT)
    return NATIVE_LIBRARY


def build_retired_diagnostic() -> Path:
    """Keep old experiments reproducible; this jar is not a formal actor input."""
    library = build_native()
    java_tools = discover_java_tools()
    launch = json.loads((
        ROOT / "deployment/fabric-observation-probe/build/launch/client-launch.json"
    ).read_text())
    classpath = os.pathsep.join(str(ROOT / item["path"])
                                for item in launch["classpath"])
    classes = Path(tempfile.mkdtemp(prefix="classes-", dir=BUILD))
    arguments = BUILD / "javac.args"
    arguments.write_text("\n".join(
        '"' + str(source).replace("\\", "/") + '"'
        for source in [
            "-proc:none", "-cp", classpath, "-d", classes,
            *sorted((SOURCE / "java").rglob("*.java")),
        ]
    ))
    subprocess.run([
        str(java_tools.javac),
        "@" + str(arguments),
    ], check=True, cwd=ROOT)
    target = BUILD / "surface-cache-diagnostic.jar"
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n\r\n")
        for path in classes.rglob("*.class"):
            archive.write(path, path.relative_to(classes).as_posix())
        archive.writestr("fabric.mod.json", json.dumps(dict(
            schemaVersion=1, id="mc2p_surface_cache_diagnostic", version="0.0.2",
            environment="client", entrypoints={"client": ["CachedSurface"]},
            mixins=["surface-cache.mixins.json"],
            depends={"fabricloader": ">=0.15.11", "minecraft": "1.21"},
        )))
        archive.writestr("surface-cache.mixins.json", json.dumps(dict(
            required=True, package="mc2p.surface.mixin",
            compatibilityLevel="JAVA_21", client=["ChunkStateMixin"],
            injectors={"defaultRequire": 1},
        )))
    subprocess.run([
        str(java_tools.java),
        "-cp", str(classes), "CacheBridge", str(library),
    ], check=True)
    return target


if __name__ == "__main__":
    print(build_retired_diagnostic())
