from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socketserver
import threading
import time
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
MAX_FRAME_BYTES = 1_000_000
TOP_LEVEL_KEYS = {
    "schema", "sequence", "world_tick", "sample_ns", "sample_duration_ns",
    "camera", "player", "range", "horizontal_fov", "vertical_fov", "complete",
    "queried_cells", "unknown_cells", "palette", "runs",
}
IDENTITY_KEYS = {"block_id", "block_name", "registry_id", "item_id", "state_id"}


class SnapshotContractError(ValueError):
    pass


def _reject_identity(value) -> None:
    if isinstance(value, dict):
        forbidden = IDENTITY_KEYS.intersection(value)
        if forbidden:
            raise SnapshotContractError(f"navigation snapshot leaked identity: {sorted(forbidden)}")
        for child in value.values():
            _reject_identity(child)
    elif isinstance(value, list):
        for child in value:
            _reject_identity(child)


def validate_snapshot(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise SnapshotContractError("invalid snapshot JSON") from error
    if not isinstance(value, dict) or set(value) != TOP_LEVEL_KEYS:
        raise SnapshotContractError("unexpected navigation snapshot fields")
    if value["schema"] != "mc2p.navigation-layer-live.v1":
        raise SnapshotContractError("unexpected navigation snapshot schema")
    if type(value["sequence"]) is not int or value["sequence"] < 0:
        raise SnapshotContractError("invalid sequence")
    if type(value["complete"]) is not bool:
        raise SnapshotContractError("invalid completeness")
    for key in ("queried_cells", "unknown_cells"):
        if type(value[key]) is not int or value[key] < 0:
            raise SnapshotContractError(f"invalid {key}")
    palette = value["palette"]
    if not isinstance(palette, list) or len(palette) > 4096:
        raise SnapshotContractError("invalid palette")
    for entry in palette:
        if not isinstance(entry, dict) or set(entry) != {"category", "collision", "fluid", "boxes"}:
            raise SnapshotContractError("invalid palette entry")
        if entry["category"] not in {"air", "passable_non_air", "occupied"}:
            raise SnapshotContractError("invalid navigation category")
        if entry["collision"] not in {"empty", "full_cube", "boxes"}:
            raise SnapshotContractError("invalid collision kind")
        if type(entry["fluid"]) is not bool or not isinstance(entry["boxes"], list):
            raise SnapshotContractError("invalid palette geometry")
    runs = value["runs"]
    if not isinstance(runs, list) or len(runs) > 25000:
        raise SnapshotContractError("invalid run collection")
    for run in runs:
        if (not isinstance(run, list) or len(run) != 5
                or any(type(part) is not int for part in run)
                or run[3] <= run[2] or not 0 <= run[4] < len(palette)):
            raise SnapshotContractError("invalid navigation run")
    _reject_identity(value)
    return value


class SnapshotHub:
    """Keep one latest frame; slow viewers never create a gameplay backlog."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._version = 0
        self._payload: str | None = None
        self._published_at = 0.0

    def publish(self, raw: str) -> int:
        value = validate_snapshot(raw)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._condition:
            self._version = value["sequence"]
            self._payload = payload
            self._published_at = time.monotonic()
            self._condition.notify_all()
            return self._version

    def wait_after(self, version: int, timeout: float | None = 10.0) -> tuple[int, str | None]:
        with self._condition:
            if self._payload is None or self._version <= version:
                self._condition.wait(timeout)
            return self._version, self._payload

    def status(self) -> dict:
        with self._condition:
            age_ms = None if self._payload is None else (time.monotonic() - self._published_at) * 1000.0
            return {"ready": self._payload is not None, "sequence": self._version, "age_ms": age_ms}


class _ProducerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            line = self.rfile.readline(MAX_FRAME_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                return
            try:
                self.server.hub.publish(line.decode("utf-8"))  # type: ignore[attr-defined]
            except (UnicodeDecodeError, SnapshotContractError):
                return


class ProducerServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: SnapshotHub):
        self.hub = hub
        super().__init__(address, _ProducerHandler)


class _WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/events":
            self._events()
            return
        if path == "/health":
            self._json(self.server.hub.status())  # type: ignore[attr-defined]
            return
        if path == "/snapshot":
            _, payload = self.server.hub.wait_after(-1, timeout=0)  # type: ignore[attr-defined]
            if payload is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            else:
                self._bytes(payload.encode("utf-8"), "application/json; charset=utf-8")
            return
        files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        name = files.get(path)
        if name is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if name.endswith(".html") else (
            "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
        self._bytes((ROOT / "web" / name).read_bytes(), content_type)

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        version = -1
        try:
            while True:
                next_version, payload = self.server.hub.wait_after(version, timeout=5.0)  # type: ignore[attr-defined]
                if payload is None or next_version <= version:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    version = next_version
                    self.wfile.write(f"id:{version}\ndata:{payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(self, value: dict) -> None:
        self._bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"), "application/json")

    def _bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: SnapshotHub):
        self.hub = hub
        super().__init__(address, _WebHandler)


def serve(web_port: int, producer_port: int) -> None:
    hub = SnapshotHub()
    producer = ProducerServer(("127.0.0.1", producer_port), hub)
    web = WebServer(("127.0.0.1", web_port), hub)
    thread = threading.Thread(target=producer.serve_forever, name="navigation-producer", daemon=True)
    thread.start()
    print(json.dumps({"web": f"http://127.0.0.1:{web_port}/", "producer_port": producer_port}), flush=True)
    try:
        web.serve_forever()
    finally:
        producer.shutdown()
        producer.server_close()
        web.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-port", type=int, default=8770)
    parser.add_argument("--producer-port", type=int, default=8771)
    args = parser.parse_args()
    serve(args.web_port, args.producer_port)


if __name__ == "__main__":
    main()
