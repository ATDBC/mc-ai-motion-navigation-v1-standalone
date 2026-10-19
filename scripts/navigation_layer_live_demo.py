"""Launch a visible human-controlled client and the anonymous navigation-layer live view."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fabric_deployment_launch import ROOT
from scripts.fabric_deployment_sandbox import JAVA, prepare_server


PROJECT = ROOT / "deployment/navigation-layer-viewer"


def local_gradle() -> Path:
    """Resolve the pinned local Gradle only when the live client is launched."""
    matches = tuple(
        (ROOT / ".gradle/wrapper/dists/gradle-8.8-bin").glob(
            "*/gradle-8.8/bin/gradle.bat"
        )
    )
    if len(matches) != 1:
        raise RuntimeError(
            "navigation-layer live launch requires the pinned local Gradle 8.8 cache"
        )
    return matches[0]


def require_free(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"service did not become ready: {url}")
        time.sleep(0.05)


def wait_server(log_path: Path, process: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError("local Minecraft server exited during startup")
        if log_path.exists() and "Done (" in log_path.read_text("utf-8", errors="replace"):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("local Minecraft server readiness timeout")
        time.sleep(0.1)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def force_creative_mode(properties_path: Path) -> None:
    """Make the isolated manual-inspection server enter creative mode on join."""
    lines = properties_path.read_text("utf-8").splitlines()
    values: dict[str, str] = {}
    order: list[str] = []
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in values:
            order.append(key)
        values[key] = value
    values["gamemode"] = "creative"
    values["force-gamemode"] = "true"
    if "force-gamemode" not in order:
        order.append("force-gamemode")
    properties_path.write_text(
        "".join(f"{key}={values[key]}\n" for key in order),
        encoding="utf-8",
    )


def launch(*, web_port: int, producer_port: int, server_port: int, seed: int,
           open_browser: bool = True) -> Path:
    gradle = local_gradle()
    for port in (web_port, producer_port, server_port):
        require_free(port)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = ROOT / "artifacts/navigation-layer-live" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    prepare_server(run_dir / "server", seed=seed, port=server_port)
    force_creative_mode(run_dir / "server/server.properties")
    client_dir = run_dir / "client"
    client_dir.mkdir()
    (client_dir / "options.txt").write_text(
        "pauseOnLostFocus:false\nrenderDistance:4\nsimulationDistance:5\nmaxFps:60\n"
        "enableVsync:false\ntutorialStep:none\njoinedFirstServer:true\nskipMultiplayerWarning:true\n"
        "soundCategory_master:0.2\nautoJump:false\nlang:zh_cn\nfov:0.3\n",
        encoding="utf-8",
    )
    clean_environment = {key: value for key, value in os.environ.items()
                         if key.upper() not in {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}}
    clean_environment.update(
        JAVA_HOME=str(ROOT / ".venv/Library"),
        GRADLE_USER_HOME=str(ROOT / ".gradle"),
        MC2P_NAV_VIEW_STREAM_PORT=str(producer_port),
        HTTP_PROXY="http://127.0.0.1:7897",
        HTTPS_PROXY="http://127.0.0.1:7897",
        ALL_PROXY="http://127.0.0.1:7897",
        NO_PROXY="localhost,127.0.0.1,::1,repo.huaweicloud.com",
    )
    handles = []
    server_log = (run_dir / "server.log").open("wb")
    relay_log = (run_dir / "relay.log").open("wb")
    client_log = (run_dir / "client.log").open("wb")
    try:
        server = subprocess.Popen(
            [str(JAVA), "-Xms256M", "-Xmx1G", "-jar", str(run_dir / "server/server.jar"), "nogui"],
            cwd=run_dir / "server", env=clean_environment, stdin=subprocess.PIPE,
            stdout=server_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        handles.append(server)
        wait_server(run_dir / "server.log", server)
        relay = subprocess.Popen(
            [sys.executable, "-B", "-m", "tools.navigation_layer_live.server",
             "--web-port", str(web_port), "--producer-port", str(producer_port)],
            cwd=ROOT, env=clean_environment, stdin=subprocess.DEVNULL,
            stdout=relay_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        handles.append(relay)
        wait_http(f"http://127.0.0.1:{web_port}/health", 10.0)
        minecraft_args = (
            f"--username MC2PNavigator --quickPlayMultiplayer 127.0.0.1:{server_port} "
            f"--gameDir {client_dir} --width 1100 --height 700"
        )
        client = subprocess.Popen(
            [str(gradle), "--offline", "--no-daemon", "--console=plain", "runClient", f"--args={minecraft_args}"],
            cwd=PROJECT, env=clean_environment, stdin=subprocess.DEVNULL,
            stdout=client_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        handles.append(client)
        status = {
            "schema": "mc2p.navigation-layer-live-session.v1",
            "run_id": run_id,
            "web_url": f"http://127.0.0.1:{web_port}/",
            "producer_port": producer_port,
            "server_port": server_port,
            "pids": {"server": server.pid, "relay": relay.pid, "client_launcher": client.pid},
            "control": "ordinary first-person Minecraft keyboard and mouse",
            "navigation_semantics": "identity-free and occlusion-independent inside the loaded camera frustum",
        }
        write_json(run_dir / "status.json", status)
        write_json(ROOT / "artifacts/navigation-layer-live/current.json", status | {"run_directory": str(run_dir)})
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if open_browser:
            webbrowser.open(status["web_url"])
        return_code = client.wait()
        write_json(run_dir / "finished.json", {"client_return_code": return_code})
        return run_dir
    finally:
        if "server" in locals() and server.poll() is None and server.stdin is not None:
            try:
                server.stdin.write(b"stop\n"); server.stdin.flush(); server.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                server.terminate()
        for process in reversed(handles):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill()
        server_log.close(); relay_log.close(); client_log.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-port", type=int, default=8770)
    parser.add_argument("--producer-port", type=int, default=8771)
    parser.add_argument("--server-port", type=int, default=25575)
    parser.add_argument("--seed", type=int, default=21001)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    launch(web_port=args.web_port, producer_port=args.producer_port, server_port=args.server_port,
           seed=args.seed, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
