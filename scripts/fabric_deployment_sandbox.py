"""Fresh local test-run preparation; no actor world-management or arbitrary command API."""
from __future__ import annotations

import hashlib
import json
import locale
import os
from pathlib import Path
import re
import shutil

from scripts.fabric_deployment_launch import ROOT, _hash
from scripts.visibility_fixture_world import _no_links

JAVA = ROOT / ".venv/Library/lib/jvm/bin/java.exe"
SERVER_SHA1 = "450698d1863ab5180c25d7c804ef0fe6369dd1ba"


def _port(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("invalid local test port")


def prepare_server(target: Path, *, seed: int, port: int, world_name: str = "world",
                   fixture_animals: bool=False, c1_fixture=None) -> dict:
    _port(port)
    if type(seed) is not int or not -(2 ** 63) <= seed < 2 ** 63:
        raise ValueError("invalid test world seed")
    if (type(world_name) is not str
            or re.fullmatch(r"world|mc2p-visibility-[a-z0-9-]{1,80}", world_name) is None):
        raise ValueError("unapproved local test world name")
    if type(fixture_animals) is not bool or (fixture_animals and not world_name.startswith('mc2p-visibility-')):
        raise ValueError('authored animals require an explicit visibility fixture')
    fixture_evidence = []
    if c1_fixture is not None:
        from scripts.build_fabric_c1_fixture import (
            C1FixtureArtifact, inspect_fixture, inspect_server_launch,
        )
        if type(c1_fixture) is not C1FixtureArtifact:
            raise ValueError("invalid C1 fixture artifact")
        if (not c1_fixture.jar.is_file()
                or _hash(c1_fixture.jar) != c1_fixture.sha256):
            raise ValueError("C1 fixture artifact hash changed")
        inspect_fixture(c1_fixture.jar)
        inspect_server_launch(c1_fixture.launch)
    target = target.absolute()
    _no_links(target.parent)
    if target.exists():
        raise FileExistsError("server run directory must be new")
    source = ROOT / ".gradle/caches/fabric-loom/1.21/minecraft-server.jar"
    _no_links(source)
    if source.stat().st_size != 51623779 or _hash(source, "sha1") != SERVER_SHA1:
        raise ValueError("cached server jar does not match locked upstream release")
    properties = dict(**{"server-ip": "127.0.0.1", "server-port": str(port), "level-seed": str(seed),
        "level-name": world_name, "level-type": "minecraft:flat", "generate-structures": "false",
        "online-mode": "false", "enforce-secure-profile": "false", "enable-rcon": "false",
        "enable-query": "false", "enable-command-block": "false", "gamemode": "survival",
        "difficulty": "peaceful", "max-players": "2", "view-distance": "4", "simulation-distance": "4",
        "spawn-animals": "false", "spawn-monsters": "false", "motd": "MC2P isolated local deployment test"})
    # Vanilla discards even saved animals when false. The visibility save separately disables natural mob spawning.
    if fixture_animals: properties['spawn-animals']='true'
    if c1_fixture is not None:
        properties["difficulty"] = "normal"
    properties["generator-settings"] = json.dumps(dict(biome="minecraft:plains", layers=[
        {"height": 1, "block": "minecraft:bedrock"}, {"height": 2, "block": "minecraft:dirt"},
        {"height": 1, "block": "minecraft:grass_block"}], lakes=False, features=False, structure_overrides=[]), separators=(",", ":"))
    target.mkdir(exist_ok=False)
    shutil.copyfile(source, target / "server.jar")
    if _hash(target / "server.jar", "sha1") != SERVER_SHA1:
        raise ValueError("server jar changed during fresh copy")
    with (target / "server.properties").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in properties.items()))
    with (target / "eula.txt").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("eula=true\n")
    if c1_fixture is not None:
        fixture_dir = target / "fixture-artifacts"
        fixture_dir.mkdir()
        copied = fixture_dir / c1_fixture.jar.name
        shutil.copyfile(c1_fixture.jar, copied)
        if _hash(copied) != c1_fixture.sha256:
            raise ValueError("C1 fixture changed during fresh copy")
        fixture_evidence.append({"name": copied.name, "sha256": c1_fixture.sha256})
    return dict(upstream_sha1=SERVER_SHA1, server_sha256=_hash(target / "server.jar"),
                properties_sha256=_hash(target / "server.properties"), seed=seed, server_port=port, world_name=world_name,
                server_mods=fixture_evidence, fixture_animals=fixture_animals,
                server_launch=(c1_fixture.launch if c1_fixture is not None else None),
                scope=("new isolated loopback Fabric C1 fixture server; offline identity only"
                       if c1_fixture is not None else
                       "new isolated loopback vanilla test server; offline identity only"))


def write_argument_file(target: Path, arguments: list[str]) -> None:
    _no_links(target.absolute().parent)
    if (type(arguments) is not list or not arguments
            or any(type(arg) is not str or any(char in arg for char in "\0\r\n") for arg in arguments)):
        raise ValueError("invalid Java argument vector")
    # The native Windows launcher reads argfiles in its ANSI code page, not
    # Java's file.encoding (UTF-8 on JDK 21). Encode strictly before creating a file.
    payload = "".join('"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"\n' for arg in arguments)
    encoded = payload.encode(locale.getencoding(), errors="strict")
    with target.open("xb") as stream:
        stream.write(encoded)


def client_environment(base: dict[str, str], *, token: str, server_port: int, ipc_port: int,
                       time_diagnostics: bool = False, block_parity_diagnostics: bool = False,
                       physics_tick_diagnostics: bool = False) -> dict[str, str]:
    _port(server_port)
    _port(ipc_port)
    if type(token) is not str or re.fullmatch("[0-9a-f]{64}", token) is None:
        raise ValueError("invalid session credential")
    if type(time_diagnostics) is not bool:
        raise ValueError("time diagnostics selection must be boolean")
    if type(block_parity_diagnostics) is not bool:
        raise ValueError('block parity diagnostics selection must be boolean')
    if type(physics_tick_diagnostics) is not bool:
        raise ValueError('physics tick diagnostics selection must be boolean')
    if physics_tick_diagnostics and not time_diagnostics:
        raise ValueError('physics tick diagnostics require time diagnostics')
    blocked = {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}
    result = {key: value for key, value in base.items()
              if key.upper() not in blocked and not key.upper().startswith(("FABRIC_", "MC2P_"))}
    result.update(MC2P_SESSION_TOKEN=token, MC2P_SERVER_PORT=str(server_port), MC2P_IPC_PORT=str(ipc_port),
        HTTP_PROXY="http://127.0.0.1:7897", HTTPS_PROXY="http://127.0.0.1:7897", ALL_PROXY="http://127.0.0.1:7897",
        NO_PROXY="localhost,127.0.0.1,::1,repo.huaweicloud.com")
    if time_diagnostics:
        result["MC2P_TIME_DIAGNOSTICS"] = "1"
    if block_parity_diagnostics:
        result['MC2P_BLOCK_PARITY_DIAGNOSTICS']='1'
    if physics_tick_diagnostics:
        result['MC2P_PHYSICS_TICK_DIAGNOSTICS']='1'
    return result
