"""Process-tree identity and bounded cleanup without backend dependencies."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

import psutil


@dataclass(frozen=True, slots=True)
class ProcessIdentityV0:
    pid: int
    create_time: float

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("process pid must be a positive integer")
        if not math.isfinite(self.create_time) or self.create_time <= 0:
            raise ValueError("process create_time must be finite and positive")


class ProcessIdentityError(RuntimeError):
    """Raised before mutation when a PID no longer identifies the same process."""


@dataclass(frozen=True, slots=True)
class ProcessTreeSnapshotV0:
    root: ProcessIdentityV0
    identities: tuple[ProcessIdentityV0, ...]


@dataclass(frozen=True, slots=True)
class ProcessTreeCleanupV0:
    attempted: tuple[ProcessIdentityV0, ...]
    stopped: tuple[ProcessIdentityV0, ...]
    surviving: tuple[ProcessIdentityV0, ...]
    errors: tuple[str, ...]


def _identity(process: psutil.Process) -> ProcessIdentityV0:
    return ProcessIdentityV0(process.pid, process.create_time())


def _same_process(process: psutil.Process, identity: ProcessIdentityV0) -> bool:
    try:
        return (
            process.pid == identity.pid
            and abs(process.create_time() - identity.create_time) <= 0.01
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def capture_registered_tree(root: ProcessIdentityV0) -> ProcessTreeSnapshotV0:
    try:
        process = psutil.Process(root.pid)
        if not _same_process(process, root):
            raise ProcessIdentityError(
                f"pid {root.pid} creation time does not match the registered process"
            )
        descendants = process.children(recursive=True)
        identities = (_identity(process),) + tuple(
            sorted((_identity(item) for item in descendants), key=lambda item: item.pid)
        )
    except psutil.NoSuchProcess as error:
        raise ProcessIdentityError(
            f"registered root pid {root.pid} is not alive"
        ) from error
    except psutil.AccessDenied as error:
        raise ProcessIdentityError(
            f"cannot inspect registered root pid {root.pid}"
        ) from error
    return ProcessTreeSnapshotV0(root=root, identities=identities)


def terminate_registered_tree(
    root: ProcessIdentityV0,
    registered: Sequence[ProcessIdentityV0],
    *,
    grace_seconds: float = 5.0,
) -> ProcessTreeCleanupV0:
    if not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("grace_seconds must be finite and nonnegative")
    identities = tuple(registered)
    by_pid = {identity.pid: identity for identity in identities}
    if len(by_pid) != len(identities) or by_pid.get(root.pid) != root:
        raise ProcessIdentityError("registered tree does not contain the exact root identity")

    processes: dict[int, psutil.Process] = {}
    stopped: list[ProcessIdentityV0] = []
    for identity in identities:
        try:
            process = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            stopped.append(identity)
            continue
        if not _same_process(process, identity):
            if identity == root:
                raise ProcessIdentityError(
                    f"root pid {identity.pid} creation time changed before cleanup"
                )
            stopped.append(identity)
            continue
        processes[identity.pid] = process

    errors: list[str] = []
    ordered = [
        processes[identity.pid]
        for identity in identities
        if identity.pid != root.pid and identity.pid in processes
    ]
    if root.pid in processes:
        ordered.append(processes[root.pid])
    for process in ordered:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            if isinstance(error, psutil.AccessDenied):
                errors.append(f"terminate pid {process.pid}: access denied")
    gone, alive = psutil.wait_procs(ordered, timeout=grace_seconds)
    del gone
    for process in alive:
        identity = by_pid[process.pid]
        if not _same_process(process, identity):
            continue
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            if isinstance(error, psutil.AccessDenied):
                errors.append(f"kill pid {process.pid}: access denied")
    if alive:
        psutil.wait_procs(alive, timeout=grace_seconds)

    surviving: list[ProcessIdentityV0] = []
    already_stopped = {identity.pid for identity in stopped}
    for identity in identities:
        if identity.pid in already_stopped:
            continue
        try:
            process = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            stopped.append(identity)
            continue
        if _same_process(process, identity) and process.is_running():
            surviving.append(identity)
        else:
            stopped.append(identity)
    return ProcessTreeCleanupV0(
        attempted=identities,
        stopped=tuple(sorted(stopped, key=lambda item: item.pid)),
        surviving=tuple(sorted(surviving, key=lambda item: item.pid)),
        errors=tuple(errors),
    )
