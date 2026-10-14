"""Run each ``tests/motion_nav/test_*.py`` module in a bounded process."""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from datetime import datetime, timezone
import fnmatch
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import traceback
import unittest

import psutil

try:
    from scripts.process_tree import (
        ProcessIdentityError,
        ProcessIdentityV0,
        capture_registered_tree,
        terminate_registered_tree,
    )
except ModuleNotFoundError:  # Direct ``python scripts/run_motion_nav_shards.py``.
    from process_tree import (  # type: ignore[no-redef]
        ProcessIdentityError,
        ProcessIdentityV0,
        capture_registered_tree,
        terminate_registered_tree,
    )


SCHEMA_VERSION = "mc2p.motion-nav-shards.v1"
STATUSES = ("passed", "failed", "error", "timeout", "launch_error")
PROCESS_POLL_SECONDS = 0.025
TREE_TERMINATE_GRACE_SECONDS = 1.0
ROOT_WAIT_SECONDS = 1.0
START_GATE_SECONDS = 5.0


def _default_module_timeout(platform_name: str) -> float:
    return 300.0 if platform_name == "nt" else 600.0


DEFAULT_MODULE_TIMEOUT_SECONDS = _default_module_timeout(os.name)


if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", _BasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _JobProcessIdList(ctypes.Structure):
        _fields_ = [
            ("number_of_assigned_processes", wintypes.DWORD),
            ("number_of_process_ids_in_list", wintypes.DWORD),
            ("process_id_list", ctypes.c_size_t * 1024),
        ]


class _WindowsJob:
    def __init__(self, handle: int) -> None:
        self._handle = handle
        self.closed = False

    @classmethod
    def assign(cls, process: subprocess.Popen[str]) -> "_WindowsJob":
        if os.name != "nt":
            raise RuntimeError("Windows Job Object requested on a non-Windows platform")
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        job = cls(int(handle))
        try:
            limits = _ExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not _kernel32.SetInformationJobObject(
                wintypes.HANDLE(job._handle),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise OSError(
                    ctypes.get_last_error(),
                    "SetInformationJobObject failed",
                )
            process_handle = wintypes.HANDLE(_popen_windows_handle(process))
            if not _kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job._handle), process_handle
            ):
                raise OSError(
                    ctypes.get_last_error(),
                    "AssignProcessToJobObject failed; nested job is unavailable",
                )
            return job
        except BaseException:
            job.close()
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not _kernel32.CloseHandle(wintypes.HANDLE(self._handle)):
            raise OSError(ctypes.get_last_error(), "CloseHandle(job) failed")

    def process_ids(self) -> tuple[int, ...]:
        if self.closed:
            return ()
        process_ids = _JobProcessIdList()
        returned = wintypes.DWORD()
        if not _kernel32.QueryInformationJobObject(
            wintypes.HANDLE(self._handle),
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(process_ids),
            ctypes.sizeof(process_ids),
            ctypes.byref(returned),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "QueryInformationJobObject failed",
            )
        count = min(int(process_ids.number_of_process_ids_in_list), 1024)
        return tuple(int(process_ids.process_id_list[index]) for index in range(count))


def _popen_windows_handle(process: subprocess.Popen[str]) -> int:
    """Return the CPython 3.11 Windows handle or fail before assignment.

    The formal Windows runner is fixed to CPython 3.11. ``Popen._handle`` is
    private, so another interpreter must not silently fall back to PID-based
    assignment: without the owned handle the Job Object containment guarantee
    would be lost.
    """
    handle = getattr(process, "_handle", None)
    if handle is None:
        raise RuntimeError("CPython 3.11 Popen handle is unavailable")
    return int(handle)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.perf_counter() + 0.2
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                retryable = os.name == "nt" and getattr(
                    error, "winerror", None,
                ) in (5, 32, 33)
                remaining = deadline - time.perf_counter()
                if not retryable or remaining <= 0:
                    raise
                time.sleep(min(0.005, remaining))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    return ".".join(relative.parts)


def _discover_modules(
    root: Path,
    tests_directory: Path,
    includes: tuple[str, ...],
    filters: tuple[re.Pattern[str], ...],
    *,
    reverse: bool,
) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(tests_directory.glob("test_*.py"), key=lambda item: item.name):
        module = _module_name(root, path)
        relative = path.relative_to(root).as_posix()
        names = (path.name, path.stem, module, relative)
        if includes and not any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in includes
            for name in names
        ):
            continue
        if filters and not any(pattern.search(module) for pattern in filters):
            continue
        found.append((module, path))
    if reverse:
        found.reverse()
    return found


def _child_payload(module: str, result: unittest.TestResult, duration: float) -> dict:
    if result.errors:
        status = "error"
    elif result.failures or result.unexpectedSuccesses:
        status = "failed"
    elif result.testsRun == 0:
        status = "error"
    else:
        status = "passed"
    return {
        "module": module,
        "status": status,
        "duration_seconds": duration,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors) + int(result.testsRun == 0),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "diagnostic": "zero tests were discovered" if result.testsRun == 0 else None,
    }


def _run_child(module: str, root: Path, report: Path) -> int:
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    started = time.perf_counter()
    stream = io.StringIO()
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(module)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        payload = _child_payload(module, result, time.perf_counter() - started)
    except BaseException as error:
        payload = {
            "module": module,
            "status": "error",
            "duration_seconds": time.perf_counter() - started,
            "tests_run": 0,
            "failures": 0,
            "errors": 1,
            "skipped": 0,
            "expected_failures": 0,
            "unexpected_successes": 0,
            "diagnostic": f"{type(error).__name__}: {error}\n{traceback.format_exc()}",
        }
    try:
        own = psutil.Process(os.getpid())
        root_identity = ProcessIdentityV0(own.pid, own.create_time())
        registered_tree = capture_registered_tree(root_identity).identities
        payload["registered_tree"] = [
            {"pid": identity.pid, "create_time": identity.create_time}
            for identity in registered_tree
        ]
    except (psutil.Error, ProcessIdentityError, ValueError) as error:
        payload["registered_tree"] = []
        payload["registration_diagnostic"] = (
            f"{type(error).__name__}: {error}"
        )
    output = stream.getvalue()
    if output:
        sys.stdout.write(output)
        sys.stdout.flush()
    _write_json_atomic(report, payload)
    return 0 if payload["status"] == "passed" else 1


def _empty_result(module: str, path: Path) -> dict:
    return {
        "module": module,
        "file": path.as_posix(),
        "status": "error",
        "duration_seconds": 0.0,
        "return_code": None,
        "tests_run": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "expected_failures": 0,
        "unexpected_successes": 0,
        "stdout": "",
        "stderr": "",
        "diagnostic": None,
        "cleanup": {
            "status": "not_started",
            "isolation": "windows_job" if os.name == "nt" else "posix_session",
            "registered_processes": 0,
            "attempted": [],
            "stopped": [],
            "surviving": [],
            "errors": [],
        },
    }


def _identity_payload(identity: ProcessIdentityV0) -> dict[str, int | float]:
    return {"pid": identity.pid, "create_time": identity.create_time}


def _read_output(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"[output unavailable: {type(error).__name__}: {error}]"


def _capture_into(
    root_identity: ProcessIdentityV0,
    registered: dict[int, ProcessIdentityV0],
    diagnostics: list[str],
) -> None:
    try:
        snapshot = capture_registered_tree(root_identity)
    except ProcessIdentityError as error:
        if not registered:
            diagnostics.append(str(error))
        return
    for identity in snapshot.identities:
        _register_scanned_identity(identity, registered, diagnostics)


def _register_scanned_identity(
    identity: ProcessIdentityV0,
    registered: dict[int, ProcessIdentityV0],
    diagnostics: list[str],
) -> None:
    current = registered.get(identity.pid)
    if current is None:
        registered[identity.pid] = identity
        return
    if abs(current.create_time - identity.create_time) <= 0.01:
        return
    diagnostics.append(
        f"pid {identity.pid} identity changed from "
        f"{current.create_time} to {identity.create_time}; retained registered identity"
    )


def _merge_child_registration(
    child: dict,
    registered: dict[int, ProcessIdentityV0],
    diagnostics: list[str],
    *,
    authorized_identities: dict[int, ProcessIdentityV0],
) -> None:
    child_diagnostic = child.get("registration_diagnostic")
    if child_diagnostic:
        diagnostics.append(str(child_diagnostic))
    for raw in child.get("registered_tree", ()):
        try:
            identity = ProcessIdentityV0(
                pid=int(raw["pid"]),
                create_time=float(raw["create_time"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            diagnostics.append(
                f"invalid child process identity: {type(error).__name__}: {error}"
            )
            continue
        current = registered.get(identity.pid)
        if current is not None:
            if abs(current.create_time - identity.create_time) > 0.01:
                diagnostics.append(
                    f"pid {identity.pid} identity changed in child report; "
                    "retained scan-confirmed identity"
                )
            continue
        authorized = authorized_identities.get(identity.pid)
        if authorized is None:
            diagnostics.append(
                f"ignored unconfirmed child process identity pid {identity.pid}"
            )
            continue
        registered[identity.pid] = authorized
        if abs(authorized.create_time - identity.create_time) > 0.01:
            diagnostics.append(
                f"pid {identity.pid} child create_time differed; "
                "used container-confirmed identity"
            )


def _container_authorized_identities(
    child: dict | None,
    root_identity: ProcessIdentityV0,
    *,
    job: _WindowsJob | None,
    diagnostics: list[str],
) -> dict[int, ProcessIdentityV0]:
    """Read live identities from the parent-owned Job or POSIX process group.

    Child JSON supplies candidate PIDs on POSIX, never ownership or creation
    times. Windows obtains the whole membership list from the Job Object.
    """
    if job is not None:
        try:
            candidate_pids = set(job.process_ids())
        except OSError as error:
            diagnostics.append(f"query Windows job: {type(error).__name__}: {error}")
            return {}
    elif os.name != "nt":
        candidate_pids = set()
        for raw in () if child is None else child.get("registered_tree", ()):
            try:
                pid = int(raw["pid"])
            except (KeyError, TypeError, ValueError):
                continue  # _merge_child_registration records the malformed entry.
            try:
                if os.getpgid(pid) == root_identity.pid:
                    candidate_pids.add(pid)
            except (OSError, ProcessLookupError):
                continue
    else:
        return {}

    authorized: dict[int, ProcessIdentityV0] = {}
    for pid in candidate_pids:
        try:
            process = psutil.Process(pid)
            identity = ProcessIdentityV0(pid=pid, create_time=process.create_time())
            if job is None:
                # Recheck the group after reading create_time so PID reuse
                # cannot turn a stale child hint into cleanup authority.
                if os.getpgid(pid) != root_identity.pid:
                    continue
                if abs(psutil.Process(pid).create_time() - identity.create_time) > 0.01:
                    continue
        except (psutil.Error, OSError, ProcessLookupError, ValueError):
            continue
        authorized[pid] = identity
    if job is not None:
        try:
            confirmed_pids = set(job.process_ids())
        except OSError as error:
            diagnostics.append(
                f"recheck Windows job: {type(error).__name__}: {error}"
            )
            return {}
        authorized = {
            pid: identity
            for pid, identity in authorized.items()
            if pid in confirmed_pids
        }
    return authorized


def _identity_is_alive(identity: ProcessIdentityV0) -> bool:
    try:
        process = psutil.Process(identity.pid)
        return (
            abs(process.create_time() - identity.create_time) <= 0.01
            and process.is_running()
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _bounded_root_fallback(process: subprocess.Popen[str]) -> list[str]:
    errors: list[str] = []
    if process.poll() is not None:
        return errors
    try:
        process.terminate()
    except OSError as error:
        errors.append(f"fallback terminate root: {type(error).__name__}: {error}")
    try:
        process.wait(timeout=ROOT_WAIT_SECONDS)
        return errors
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError as error:
        errors.append(f"fallback kill root: {type(error).__name__}: {error}")
    try:
        process.wait(timeout=ROOT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        errors.append("fallback root remained alive after bounded kill")
    return errors


def _refresh_cleanup_liveness(
    cleanup: dict,
    registered: tuple[ProcessIdentityV0, ...],
    *,
    wait_seconds: float = 0.0,
) -> None:
    deadline = time.perf_counter() + wait_seconds
    while True:
        surviving = [item for item in registered if _identity_is_alive(item)]
        if not surviving or time.perf_counter() >= deadline:
            break
        time.sleep(.01)
    stopped = [item for item in registered if item not in surviving]
    cleanup["stopped"] = [_identity_payload(item) for item in stopped]
    cleanup["surviving"] = [_identity_payload(item) for item in surviving]
    if surviving:
        cleanup["status"] = "failed"


def _close_posix_session(process_group_id: int) -> list[str]:
    if os.name == "nt":
        return []
    errors: list[str] = []
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return errors
    except OSError as error:
        errors.append(f"terminate POSIX process group: {type(error).__name__}: {error}")
        return errors
    deadline = time.perf_counter() + TREE_TERMINATE_GRACE_SECONDS
    while time.perf_counter() < deadline:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return errors
        time.sleep(.01)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return errors
    except OSError as error:
        errors.append(f"kill POSIX process group: {type(error).__name__}: {error}")
        return errors
    deadline = time.perf_counter() + TREE_TERMINATE_GRACE_SECONDS
    while time.perf_counter() < deadline:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return errors
        time.sleep(.01)
    errors.append("POSIX process group remained alive after bounded kill")
    return errors


def _cleanup_process_tree(
    root_identity: ProcessIdentityV0,
    registered: tuple[ProcessIdentityV0, ...],
    *,
    process: subprocess.Popen[str] | None = None,
) -> dict:
    errors: list[str] = []
    try:
        cleanup = terminate_registered_tree(
            root_identity,
            registered,
            grace_seconds=TREE_TERMINATE_GRACE_SECONDS,
        )
    except Exception as error:
        errors.append(f"{type(error).__name__}: {error}")
        if process is not None:
            errors.extend(_bounded_root_fallback(process))
        stopped = [item for item in registered if not _identity_is_alive(item)]
        surviving = [item for item in registered if _identity_is_alive(item)]
        return {
            "status": "failed",
            "registered_processes": len(registered),
            "attempted": [_identity_payload(item) for item in registered],
            "stopped": [_identity_payload(item) for item in stopped],
            "surviving": [_identity_payload(item) for item in surviving],
            "errors": errors,
        }
    errors.extend(cleanup.errors)
    return {
        "status": "failed" if cleanup.surviving or errors else "passed",
        "registered_processes": len(registered),
        "attempted": [_identity_payload(item) for item in cleanup.attempted],
        "stopped": [_identity_payload(item) for item in cleanup.stopped],
        "surviving": [_identity_payload(item) for item in cleanup.surviving],
        "errors": errors,
    }


def _run_module(
    python: str,
    script: Path,
    root: Path,
    module: str,
    path: Path,
    timeout_seconds: float,
    child_report: Path,
) -> dict:
    item = _empty_result(module, path)
    start_gate = child_report.with_suffix(".start-gate")
    command = [
        python,
        str(script),
        "_child",
        "--root",
        str(root),
        "--module",
        module,
        "--report",
        str(child_report),
        "--start-gate",
        str(start_gate),
    ]
    started = time.perf_counter()
    stdout_path = child_report.with_suffix(".stdout.txt")
    stderr_path = child_report.with_suffix(".stderr.txt")
    try:
        with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_file, \
                stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                errors="replace",
                start_new_session=os.name != "nt",
            )
    except OSError as error:
        item.update(
            status="launch_error",
            duration_seconds=time.perf_counter() - started,
            diagnostic=f"{type(error).__name__}: {error}",
        )
        return item
    job: _WindowsJob | None = None
    if os.name == "nt":
        try:
            job = _WindowsJob.assign(process)
        except (OSError, RuntimeError) as error:
            cleanup_errors = _bounded_root_fallback(process)
            item.update(
                status="launch_error",
                duration_seconds=time.perf_counter() - started,
                return_code=process.returncode,
                stdout=_read_output(stdout_path),
                stderr=_read_output(stderr_path),
                diagnostic=f"Windows Job Object unavailable: {type(error).__name__}: {error}",
            )
            item["cleanup"].update(
                status="failed",
                errors=[f"{type(error).__name__}: {error}", *cleanup_errors],
            )
            return item
    try:
        start_gate.write_text("start\n", encoding="ascii")
    except OSError as error:
        cleanup_errors = _bounded_root_fallback(process)
        if job is not None:
            try:
                job.close()
            except OSError as close_error:
                cleanup_errors.append(
                    f"close Windows job: {type(close_error).__name__}: {close_error}"
                )
        item.update(
            status="launch_error",
            duration_seconds=time.perf_counter() - started,
            return_code=process.returncode,
            stdout=_read_output(stdout_path),
            stderr=_read_output(stderr_path),
            diagnostic=f"start gate unavailable: {type(error).__name__}: {error}",
        )
        item["cleanup"].update(status="failed", errors=cleanup_errors)
        return item
    registration_diagnostics: list[str] = []
    registered: dict[int, ProcessIdentityV0] = {}
    try:
        root_process = psutil.Process(process.pid)
        root_identity = ProcessIdentityV0(process.pid, root_process.create_time())
        registered[root_identity.pid] = root_identity
    except (psutil.Error, ValueError) as error:
        cleanup_errors: list[str] = []
        try:
            process.kill()
            process.wait(timeout=ROOT_WAIT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as cleanup_error:
            cleanup_errors.append(
                f"root identity fallback: {type(cleanup_error).__name__}: {cleanup_error}"
            )
        if job is not None:
            try:
                job.close()
            except OSError as cleanup_error:
                cleanup_errors.append(
                    f"close Windows job: {type(cleanup_error).__name__}: {cleanup_error}"
                )
        item.update(
            status="launch_error",
            duration_seconds=time.perf_counter() - started,
            return_code=process.returncode,
            stdout=_read_output(stdout_path),
            stderr=_read_output(stderr_path),
            diagnostic=f"process identity unavailable: {type(error).__name__}: {error}",
        )
        item["cleanup"].update(
            status="failed",
            errors=[f"{type(error).__name__}: {error}", *cleanup_errors],
        )
        return item

    deadline = started + timeout_seconds
    timed_out = False
    while process.poll() is None:
        _capture_into(root_identity, registered, registration_diagnostics)
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            timed_out = True
            break
        time.sleep(min(PROCESS_POLL_SECONDS, remaining))
    _capture_into(root_identity, registered, registration_diagnostics)
    child: dict | None = None
    try:
        child = json.loads(child_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    authorized = _container_authorized_identities(
        child,
        root_identity,
        job=job,
        diagnostics=registration_diagnostics,
    )
    if child is not None:
        _merge_child_registration(
            child,
            registered,
            registration_diagnostics,
            authorized_identities=authorized,
        )
    for process_identity in authorized.values():
        _register_scanned_identity(
            process_identity, registered, registration_diagnostics
        )
    identities = tuple(sorted(registered.values(), key=lambda identity: identity.pid))
    cleanup = _cleanup_process_tree(root_identity, identities, process=process)
    cleanup["isolation"] = "windows_job" if os.name == "nt" else "posix_session"
    if job is not None:
        try:
            job.close()
            cleanup["job_closed"] = True
        except OSError as error:
            cleanup["job_closed"] = False
            cleanup["errors"].append(
                f"close Windows job: {type(error).__name__}: {error}"
            )
        _refresh_cleanup_liveness(
            cleanup,
            identities,
            wait_seconds=ROOT_WAIT_SECONDS,
        )
    elif os.name != "nt":
        cleanup["errors"].extend(_close_posix_session(root_identity.pid))
        cleanup["session_closed"] = not cleanup["errors"]
        _refresh_cleanup_liveness(
            cleanup,
            identities,
            wait_seconds=ROOT_WAIT_SECONDS,
        )
    cleanup["errors"] = list(dict.fromkeys(
        [*registration_diagnostics, *cleanup["errors"]]
    ))
    if cleanup["errors"]:
        cleanup["status"] = "failed"
    item["cleanup"] = cleanup
    try:
        process.wait(timeout=ROOT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        cleanup["status"] = "failed"
        cleanup["errors"].append("root process did not exit after bounded tree cleanup")
    stdout = _read_output(stdout_path)
    stderr = _read_output(stderr_path)
    if timed_out:
        diagnostic = f"module exceeded {timeout_seconds:g} seconds"
        if cleanup["status"] != "passed":
            diagnostic += "; process-tree cleanup failed"
        item.update(
            status="timeout",
            duration_seconds=time.perf_counter() - started,
            return_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            diagnostic=diagnostic,
        )
        return item
    item.update(
        duration_seconds=time.perf_counter() - started,
        return_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if child is None:
        try:
            child = json.loads(child_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            item.update(
                status="error",
                errors=1,
                diagnostic=f"child result unavailable: {type(error).__name__}: {error}",
            )
            return item
    if child is None:
        item.update(
            status="error",
            errors=1,
            diagnostic="child result unavailable",
        )
        return item
    for name in (
        "status",
        "tests_run",
        "failures",
        "errors",
        "skipped",
        "expected_failures",
        "unexpected_successes",
        "diagnostic",
    ):
        item[name] = child[name]
    if item["status"] == "passed" and process.returncode != 0:
        item.update(
            status="error",
            errors=max(1, item["errors"]),
            diagnostic=f"passing child exited with code {process.returncode}",
        )
    if item["status"] == "passed" and item["tests_run"] == 0:
        item.update(
            status="error",
            errors=max(1, item["errors"]),
            diagnostic="zero tests were discovered",
        )
    if item["status"] == "passed" and cleanup["status"] != "passed":
        item.update(
            status="error",
            errors=max(1, item["errors"]),
            diagnostic="process-tree cleanup failed",
        )
    return item


def _summary(items: list[dict]) -> dict[str, int]:
    counts = Counter(item["status"] for item in items)
    return {status: counts[status] for status in STATUSES}


def _main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--tests-directory",
        type=Path,
        default=Path("tests/motion_nav"),
        help="directory relative to --root (default: tests/motion_nav)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".tmp/motion-nav-shards.json"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_seconds,
        default=DEFAULT_MODULE_TIMEOUT_SECONDS,
        help=("per-module deadline in seconds "
              f"(default: {DEFAULT_MODULE_TIMEOUT_SECONDS:g} on this platform)"),
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="include modules matching any repeatable file/module glob",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="REGEX",
        help="include modules matching any repeatable regular expression",
    )
    parser.add_argument("--reverse", action="store_true")
    return parser


def _run_parent(argv: list[str]) -> int:
    parser = _main_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    tests_directory = args.tests_directory
    if not tests_directory.is_absolute():
        tests_directory = root / tests_directory
    tests_directory = tests_directory.resolve()
    report = args.report
    if not report.is_absolute():
        report = root / report
    report = report.resolve()
    if not tests_directory.is_dir():
        parser.error(f"test directory does not exist: {tests_directory}")
    try:
        filters = tuple(re.compile(value) for value in args.filter)
    except re.error as error:
        parser.error(f"invalid --filter expression: {error}")
    modules = _discover_modules(
        root,
        tests_directory,
        tuple(args.include),
        filters,
        reverse=args.reverse,
    )
    if not modules:
        parser.error("no test modules matched the selection")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "state": "running",
        "started_at": _utc_now(),
        "finished_at": None,
        "root": str(root),
        "tests_directory": str(tests_directory),
        "python": args.python,
        "python_version": sys.version,
        "platform": platform.platform(),
        "order": "reverse" if args.reverse else "forward",
        "timeout_seconds": args.timeout_seconds,
        "includes": list(args.include),
        "filters": list(args.filter),
        "selected_modules": len(modules),
        "completed_modules": 0,
        "current_module": None,
        "summary": _summary([]),
        "modules": [],
    }
    _write_json_atomic(report, payload)
    script = Path(__file__).resolve()
    with TemporaryDirectory(prefix="motion-nav-shards-") as temporary:
        temporary_root = Path(temporary)
        for index, (module, path) in enumerate(modules, start=1):
            payload["current_module"] = module
            _write_json_atomic(report, payload)
            print(f"[{index}/{len(modules)}] START {module}", flush=True)
            result = _run_module(
                args.python,
                script,
                root,
                module,
                path.relative_to(root),
                args.timeout_seconds,
                temporary_root / f"{index:04d}.json",
            )
            payload["modules"].append(result)
            payload["completed_modules"] = index
            payload["summary"] = _summary(payload["modules"])
            print(
                f"[{index}/{len(modules)}] END   {module} {result['status']} "
                f"{result['duration_seconds']:.3f}s",
                flush=True,
            )
            _write_json_atomic(report, payload)
    payload["state"] = "complete"
    payload["current_module"] = None
    payload["finished_at"] = _utc_now()
    _write_json_atomic(report, payload)
    return 0 if payload["summary"]["passed"] == len(modules) else 1


def _run_child_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--start-gate", type=Path, required=True)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + START_GATE_SECONDS
    while not args.start_gate.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("parent did not release the bounded start gate")
        time.sleep(.005)
    return _run_child(args.module, args.root, args.report)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_child":
        return _run_child_cli(arguments[1:])
    return _run_parent(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
