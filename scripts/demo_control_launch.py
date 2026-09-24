"""Verify the separate human launch recipe; never import actor outputs into its classpath."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import sys
from zipfile import ZipFile
ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}: sys.path.insert(0, str(ROOT))
from scripts.build_fabric_demo_control import PROJECT, build_control_mod, inspect_control_classpath, inspect_control_mod
from scripts.fabric_deployment_launch import _verify_file, verify_assets
from scripts.visibility_fixture_world import _no_links
from scripts.fabric_deployment_sandbox import JAVA, write_argument_file
from scripts.follow_fixture_world import offline_uuid
BUILD = "deployment/fabric-demo-control/build"
LOOM = "deployment/fabric-demo-control/.gradle/loom-cache"
DIRECTORIES = {BUILD + "/classes/java/main", BUILD + "/resources/main"}

def _inspect_launch(value: dict) -> dict:
    if (type(value) is not dict or set(value) != {"schema_version", "main_class", "jvm_args", "game_args", "environment", "classpath", "remap_classpath", "auxiliary"}
            or value["schema_version"] != "mc2p.demo-control-launch.v1"
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
    dev_jar = PROJECT / "build/devlibs/mc2p-demo-control-0.1.0-dev.jar"
    inspect_control_mod(PROJECT / "build/libs/mc2p-demo-control-0.1.0.jar")
    _no_links(dev_jar)
    inspect_control_mod(dev_jar)
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
    jar_count = inspect_control_classpath(jars)
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
    remap_count = inspect_control_classpath(value["remap_classpath"])
    actual_remap = (ROOT / LOOM / "remapClasspath.txt").read_text("utf-8").split(os.pathsep)
    if actual_remap != [str(ROOT / entry["path"]) for entry in value["remap_classpath"]]:
        raise ValueError("actual remap classpath differs from verified files")
    return dict(jar_count=jar_count, remap_jar_count=remap_count,
                project_directory_count=len(directories), client_only=True)

def inspect_control_launch(path: Path) -> dict:
    _no_links(path.absolute())
    return _inspect_launch(json.loads(path.read_text("utf-8")))


def human_environment(base: dict[str, str], *, port: int, token: str, session_id: str) -> dict[str, str]:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid human control endpoint")
    if type(token) is not str or re.fullmatch("[0-9a-f]{64}", token) is None:
        raise ValueError("invalid human control credential")
    if type(session_id) is not str or re.fullmatch("[A-Za-z0-9_-]{1,128}", session_id) is None:
        raise ValueError("invalid human session identity")
    blocked = {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}
    result = {k: v for k, v in base.items() if k.upper() not in blocked and not k.upper().startswith(("MC2P_", "FABRIC_"))}
    result.update(MC2P_DEMO_CONTROL_PORT=str(port), MC2P_DEMO_CONTROL_TOKEN=token, MC2P_DEMO_SESSION_ID=session_id,
        HTTP_PROXY="http://127.0.0.1:7897", HTTPS_PROXY="http://127.0.0.1:7897", ALL_PROXY="http://127.0.0.1:7897",
        NO_PROXY="localhost,127.0.0.1,::1,repo.huaweicloud.com")
    return result


def prepare_human_launch(directory: Path, launch_path: Path, *, server_port: int, control_port: int,
                         token: str, session_id: str, base_environment: dict[str, str]) -> dict:
    """Prepare only; the explicit interactive supervisor owns any later visible process launch."""
    if type(server_port) is not int or not 1 <= server_port <= 65535 or server_port == control_port:
        raise ValueError('invalid human server endpoint')
    directory = Path(directory).absolute()
    if '..' in directory.parts: raise ValueError('human directory cannot traverse')
    _no_links(directory.parent)
    if directory.exists(): raise FileExistsError('human game directory must be new')
    _no_links(Path(launch_path).absolute())
    launch = json.loads(Path(launch_path).read_text('utf-8'))
    evidence = _inspect_launch(launch)
    verify_assets()
    environment = human_environment(base_environment, port=control_port, token=token, session_id=session_id)
    name = 'MC2PLeader'
    arguments = ['-Xms256M', '-Xmx2G', '-Djava.net.preferIPv4Stack=true', *launch['jvm_args'],
        '-cp', os.pathsep.join(str(ROOT/item['path']) for item in launch['classpath']), launch['main_class'],
        '--username', name, '--uuid', offline_uuid(name), '--accessToken', '0', '--version', '1.21',
        '--gameDir', str(directory), '--quickPlayMultiplayer', f'127.0.0.1:{server_port}']
    directory.mkdir(exist_ok=False)
    with (directory/'options.txt').open('x', encoding='utf-8') as stream:
        stream.write('pauseOnLostFocus:false\nrenderDistance:8\nsimulationDistance:5\nmaxFps:60\n'
                     'enableVsync:false\ntutorialStep:none\njoinedFirstServer:true\nskipMultiplayerWarning:true\n'
                     'autoJump:false\n')
    write_argument_file(directory/'client.args', arguments)
    return dict(command=[str(JAVA), '@'+str(directory/'client.args')], environment=environment,
                directory=directory, launch_evidence=evidence)


if __name__ == "__main__":
    build_control_mod()
    path = PROJECT/"build/launch/client-launch.json"
    print(json.dumps({"launch": inspect_control_launch(path), "assets": verify_assets()}))
    print("DEMO_CONTROL_LAUNCH_READY")
