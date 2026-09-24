"""Cache-verified ordinary vanilla client; never loads project actor classes or mixins."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

from scripts.fabric_deployment_launch import ROOT, _hash, verify_assets
from scripts.fabric_deployment_sandbox import JAVA, _port, write_argument_file
from scripts.probe_fabric_deployment_observation import PROXY_ARGS
from scripts.visibility_fixture_world import _no_links

METADATA_SHA1 = "f4a5d9917728e89bcdeb63cba3094d4536a82214"
CLIENT_SHA1 = "0e9a07b9bb3390602f977073aa12884a4ce12431"
CLIENT_SIZE = 26836080


def visible_environment(base: dict[str,str]) -> dict[str,str]:
    blocked = {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}
    result = {k:v for k,v in base.items() if k.upper() not in blocked
              and not k.upper().startswith(("MC2P_", "FABRIC_", "CRAFTGROUND_", "LOG4J"))}
    result.update(JAVA_HOME=str(ROOT/".venv/Library"), HTTP_PROXY="http://127.0.0.1:7897",
        HTTPS_PROXY="http://127.0.0.1:7897", ALL_PROXY="http://127.0.0.1:7897",
        NO_PROXY="localhost,127.0.0.1,::1,repo.huaweicloud.com")
    return result


def library_allowed(entry: dict) -> bool:
    if type(entry) is not dict or type(entry.get("name")) is not str:
        raise ValueError("invalid vanilla library entry")
    rules = entry.get("rules")
    allowed = rules is None
    if rules is not None:
        if type(rules) is not list: raise ValueError("invalid vanilla library rules")
        for rule in rules:
            if (type(rule) is not dict or set(rule) != {"action","os"}
                    or rule["action"] not in {"allow","disallow"} or type(rule["os"]) is not dict
                    or set(rule["os"]) != {"name"} or rule["os"]["name"] not in {"windows","linux","osx"}):
                raise ValueError("unsupported vanilla library rule; no guessed platform fallback")
            if rule["os"]["name"] == "windows": allowed = rule["action"] == "allow"
    # Locked x64 Windows only. Modern LWJGL jars contain their own architecture directories.
    return allowed and not entry["name"].endswith((":natives-windows-x86", ":natives-windows-arm64"))


def verify_file(path: Path, sha1: str, size: int) -> None:
    _no_links(path)
    if (type(sha1) is not str or re.fullmatch("[0-9a-f]{40}",sha1) is None or type(size) is not int
            or size < 0 or not path.is_file() or path.stat().st_size != size or _hash(path,"sha1") != sha1):
        raise ValueError("visible client source hash/size mismatch: "+str(path))


def _libraries(metadata: dict) -> list[dict]:
    selected = []
    for entry in metadata["libraries"]:
        if not library_allowed(entry): continue
        parts = entry["name"].split(":")
        if len(parts) not in {3,4} or any(re.fullmatch(r"[A-Za-z0-9_.+\-]+",p) is None for p in parts):
            raise ValueError("invalid vanilla library coordinate")
        artifact = entry["downloads"]["artifact"]
        path = PurePosixPath(artifact["path"])
        filename = "-".join(parts[1:])+".jar"
        if path != PurePosixPath(parts[0].replace(".","/"),parts[1],parts[2],filename):
            raise ValueError("vanilla artifact path differs from coordinate")
        folder = ROOT/".gradle/caches/modules-2/files-2.1"/parts[0]/parts[1]/parts[2]
        _no_links(folder)
        candidates = list(folder.glob("*/"+filename))
        if len(candidates) != 1: raise FileNotFoundError("missing/ambiguous verified vanilla library: "+entry["name"])
        source = candidates[0]
        verify_file(source,artifact["sha1"],artifact["size"])
        selected.append(dict(name=entry["name"],path=str(source),sha1=artifact["sha1"],
                             size=artifact["size"],verified=True))
    if len(selected) != 54: raise ValueError("locked Windows x64 vanilla classpath changed")
    return selected


def prepare_visible_launch(target: Path, *, server_port: int, username: str) -> dict:
    _port(server_port)
    if type(username) is not str or re.fullmatch("[A-Za-z0-9_]{1,16}",username) is None:
        raise ValueError("invalid local visible identity")
    target = Path(target)
    if ".." in target.parts: raise ValueError("visible game directory cannot escape through parent segments")
    target = target.absolute()
    _no_links(target.parent)
    if target.exists(): raise FileExistsError("visible game directory must be new")
    if os.name != "nt" or os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64").upper() != "AMD64":
        raise ValueError("only the locked Windows x64 runtime is supported")
    metadata_path = ROOT/".gradle/caches/fabric-loom/1.21/minecraft-info.json"
    _no_links(metadata_path)
    if _hash(metadata_path,"sha1") != METADATA_SHA1: raise ValueError("locked vanilla metadata hash mismatch")
    metadata = json.loads(metadata_path.read_text("utf-8"))
    if metadata["id"] != "1.21" or metadata["mainClass"] != "net.minecraft.client.main.Main":
        raise ValueError("wrong vanilla client version/main")
    client = ROOT/".gradle/caches/fabric-loom/1.21/minecraft-client.jar"
    verify_file(client,CLIENT_SHA1,CLIENT_SIZE)
    libraries = _libraries(metadata)
    assets = verify_assets()
    _no_links(JAVA)
    target.mkdir(exist_ok=False)
    native_dir = target/"natives"
    native_dir.mkdir()
    arguments = ["-Xms256M","-Xmx2G",*PROXY_ARGS,"-Djava.net.preferIPv4Stack=true",
        *["-D"+key+"="+str(native_dir) for key in ("java.library.path","jna.tmpdir",
            "org.lwjgl.system.SharedLibraryExtractPath","io.netty.native.workdir")],
        "-cp",os.pathsep.join([str(client)]+[p["path"] for p in libraries]),metadata["mainClass"],
        "--username",username,"--uuid",str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:"+username).encode("utf-8")).digest(),version=3)),
        "--accessToken","0","--userType","legacy",
        "--version","1.21","--versionType","release","--gameDir",str(target),
        "--assetsDir",str(ROOT/".gradle/caches/fabric-loom/assets"),"--assetIndex","1.21-17",
        "--width","1100","--height","700","--quickPlayMultiplayer",f"127.0.0.1:{server_port}"]
    write_argument_file(target/"client.args",arguments)
    (target/"options.txt").write_text("pauseOnLostFocus:false\nrenderDistance:4\nsimulationDistance:5\n"
        "maxFps:60\nenableVsync:false\ntutorialStep:none\njoinedFirstServer:true\nskipMultiplayerWarning:true\n"
        "soundCategory_master:0.2\nautoJump:false\nlang:zh_cn\nfov:0.3\n",encoding="utf-8")
    result = dict(schema_version="mc2p.visible-launch.v1",main_class=metadata["mainClass"],arguments=arguments,
        java=str(JAVA),metadata_sha1=METADATA_SHA1,client_sha1=CLIENT_SHA1,libraries=libraries,assets=assets,
        actor_mods=[],username=username,server_port=server_port)
    # Do not persist the user's inherited environment/secrets in the manifest.
    (target/"launch.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    result["environment"] = visible_environment(dict(os.environ))
    return result
