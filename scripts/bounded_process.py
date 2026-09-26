"""Neutral process-tree supervision shared by current Fabric tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Mapping, Sequence
import uuid

import psutil

from scripts.process_tree import (
    ProcessIdentityError,
    ProcessIdentityV0,
    capture_registered_tree,
    terminate_registered_tree,
)


_SAFE_COMPONENT = re.compile(r"[^a-z0-9-]+")


def make_run_id(*, now: datetime | None = None, unique: str | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    timestamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    component = _SAFE_COMPONENT.sub(
        "-", (unique or uuid.uuid4().hex[:8]).casefold(),
    ).strip("-")
    if not component:
        raise ValueError("run id unique component is empty after sanitization")
    return f"{timestamp}-{component}"


def _process_identity(pid: int) -> ProcessIdentityV0:
    process = psutil.Process(pid)
    return ProcessIdentityV0(pid, process.create_time())


def _merge_registered_identities(
    root: ProcessIdentityV0,
    registered: dict[int, ProcessIdentityV0],
    current: Sequence[ProcessIdentityV0],
) -> None:
    by_pid = {identity.pid: identity for identity in current}
    if len(by_pid) != len(current):
        raise ProcessIdentityError("current process tree contains duplicate pids")
    if by_pid.get(root.pid) != root:
        raise ProcessIdentityError("registered worker root identity changed")
    for identity in current:
        registered[identity.pid] = identity


@dataclass(frozen=True, slots=True)
class BoundedProcessResultV0:
    return_code: int | None
    primary_failure: str | None
    cleanup_failures: tuple[str, ...]
    process_stopped: bool
    registered_processes: tuple[ProcessIdentityV0, ...]
    schema_version: str = field(
        default="mc2p.bounded-process-result.v0", init=False,
    )


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> BoundedProcessResultV0:
    """Run one process tree with exact identity tracking and bounded cleanup."""
    if not command:
        raise ValueError("bounded process command must not be empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("bounded process timeout must be finite and positive")
    target_log = Path(log_path).resolve()
    target_log.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[str] | None = None
    root: ProcessIdentityV0 | None = None
    registered: dict[int, ProcessIdentityV0] = {}
    primary_failure: str | None = None
    cleanup_failures: list[str] = []
    return_code: int | None = None
    with target_log.open("x", encoding="utf-8", newline="\n") as log_stream:
        try:
            process = subprocess.Popen(
                list(command), cwd=Path(cwd).resolve(), env=dict(environment),
                stdout=log_stream, stderr=subprocess.STDOUT, text=True,
            )
            root = _process_identity(process.pid)
            registered[root.pid] = root
            deadline = monotonic() + timeout_seconds
            while process.poll() is None:
                try:
                    snapshot = capture_registered_tree(root)
                except ProcessIdentityError:
                    snapshot = None
                if snapshot is not None:
                    _merge_registered_identities(
                        root, registered, snapshot.identities,
                    )
                if monotonic() >= deadline:
                    primary_failure = f"process exceeded {timeout_seconds} seconds"
                    break
                sleep(0.1)
            return_code = process.poll()
        except BaseException as error:
            primary_failure = f"{type(error).__name__}: {error}"
        finally:
            if process is not None and root is not None:
                try:
                    snapshot = capture_registered_tree(root)
                except ProcessIdentityError:
                    snapshot = None
                if snapshot is not None:
                    try:
                        _merge_registered_identities(
                            root, registered, snapshot.identities,
                        )
                    except ProcessIdentityError as error:
                        cleanup_failures.append(str(error))
                try:
                    cleanup = terminate_registered_tree(
                        root, tuple(registered.values()), grace_seconds=5.0,
                    )
                except ProcessIdentityError as error:
                    cleanup_failures.append(str(error))
                else:
                    cleanup_failures.extend(cleanup.errors)
                    if cleanup.surviving:
                        cleanup_failures.append(
                            "registered build processes survived cleanup",
                        )
                if process.poll() is None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        cleanup_failures.append(
                            "build root did not exit after exact cleanup",
                        )
                return_code = process.poll()
            elif process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except BaseException as error:
                    cleanup_failures.append(
                        "unregistered build root cleanup: "
                        f"{type(error).__name__}: {error}",
                    )
                return_code = process.poll()
    return BoundedProcessResultV0(
        return_code=return_code,
        primary_failure=primary_failure,
        cleanup_failures=tuple(dict.fromkeys(cleanup_failures)),
        process_stopped=process is None or process.poll() is not None,
        registered_processes=tuple(
            sorted(registered.values(), key=lambda item: item.pid),
        ),
    )
