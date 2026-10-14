"""Run each ``tests/motion_nav/test_*.py`` module in a bounded process."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fnmatch
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import traceback
import unittest


SCHEMA_VERSION = "mc2p.motion-nav-shards.v1"
STATUSES = ("passed", "failed", "error", "timeout", "launch_error")


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
    else:
        status = "passed"
    return {
        "module": module,
        "status": status,
        "duration_seconds": duration,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "diagnostic": None,
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
    ]
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except OSError as error:
        item.update(
            status="launch_error",
            duration_seconds=time.perf_counter() - started,
            diagnostic=f"{type(error).__name__}: {error}",
        )
        return item
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        item.update(
            status="timeout",
            duration_seconds=time.perf_counter() - started,
            return_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            diagnostic=f"module exceeded {timeout_seconds:g} seconds",
        )
        return item
    item.update(
        duration_seconds=time.perf_counter() - started,
        return_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    try:
        child = json.loads(child_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        item.update(
            status="error",
            errors=1,
            diagnostic=f"child result unavailable: {type(error).__name__}: {error}",
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
    parser.add_argument("--timeout-seconds", type=_positive_seconds, default=300.0)
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
    args = parser.parse_args(argv)
    return _run_child(args.module, args.root, args.report)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_child":
        return _run_child_cli(arguments[1:])
    return _run_parent(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
