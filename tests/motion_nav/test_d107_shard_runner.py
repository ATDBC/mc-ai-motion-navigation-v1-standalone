from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_motion_nav_shards.py"


def _write_package(root: Path, modules: dict[str, str]) -> None:
    tests = root / "tests"
    motion_nav = tests / "motion_nav"
    motion_nav.mkdir(parents=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (motion_nav / "__init__.py").write_text("", encoding="utf-8")
    for name, body in modules.items():
        (motion_nav / name).write_text(body, encoding="utf-8")


def _invoke(root: Path, *arguments: str, timeout: float = 20.0):
    report = root / "report.json"
    command = [
        sys.executable,
        str(RUNNER),
        "--root",
        str(root),
        "--report",
        str(report),
        *arguments,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    return completed, payload


def _try_read_atomic_report(path: Path) -> dict | None:
    """Read an atomically replaced report across Windows sharing races.

    ``os.replace`` guarantees that a successful open sees either the old or
    the new complete file.  Windows may nevertheless reject the open briefly
    while the replace operation owns the directory entry.  Report only that
    sharing race as unavailable; malformed JSON must still fail immediately.
    """

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as error:
        retryable = os.name == "nt" and (
            getattr(error, "winerror", None) in (5, 32, 33)
            or error.errno in (errno.EACCES, errno.EBUSY, errno.EPERM)
        )
        if not retryable:
            raise
        return None


def _read_atomic_report(path: Path, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        payload = _try_read_atomic_report(path)
        if payload is not None:
            return payload
        if time.monotonic() >= deadline:
            raise TimeoutError(f"atomic report remained unavailable: {path}")
        time.sleep(.005)


class MotionNavShardRunnerTests(unittest.TestCase):
    def test_each_module_runs_in_its_own_process_and_has_a_typed_result(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_package(root, {
                "test_a_pass.py": (
                    "import os, unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_pass(self): self.assertNotEqual(os.getpid(), %d)\n"
                ) % os.getpid(),
                "test_b_failure.py": (
                    "import unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_failure(self): self.assertEqual(1, 2)\n"
                ),
                "test_c_error.py": (
                    "import unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_error(self): raise RuntimeError('boom')\n"
                ),
                "test_d_timeout.py": (
                    "import time, unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_timeout(self): time.sleep(5)\n"
                ),
            })

            completed, payload = _invoke(root, "--timeout-seconds", "0.25")

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(payload["state"], "complete")
            self.assertEqual(payload["order"], "forward")
            self.assertEqual(payload["summary"], {
                "passed": 1,
                "failed": 1,
                "error": 1,
                "timeout": 1,
                "launch_error": 0,
            })
            self.assertEqual(
                [item["module"] for item in payload["modules"]],
                [
                    "tests.motion_nav.test_a_pass",
                    "tests.motion_nav.test_b_failure",
                    "tests.motion_nav.test_c_error",
                    "tests.motion_nav.test_d_timeout",
                ],
            )
            self.assertEqual(
                [item["status"] for item in payload["modules"]],
                ["passed", "failed", "error", "timeout"],
            )
            self.assertIn("[1/4] START tests.motion_nav.test_a_pass", completed.stdout)
            self.assertIn("[4/4] END   tests.motion_nav.test_d_timeout timeout", completed.stdout)

    def test_include_filter_and_reverse_select_a_stable_module_order(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            passing = (
                "import unittest\n"
                "class Case(unittest.TestCase):\n"
                " def test_ok(self): pass\n"
            )
            _write_package(root, {
                "test_alpha_fast.py": passing,
                "test_alpha_slow.py": passing,
                "test_beta_fast.py": passing,
            })

            completed, payload = _invoke(
                root,
                "--include",
                "test_*fast.py",
                "--filter",
                "alpha|beta",
                "--reverse",
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(payload["order"], "reverse")
            self.assertEqual(
                [item["module"] for item in payload["modules"]],
                [
                    "tests.motion_nav.test_beta_fast",
                    "tests.motion_nav.test_alpha_fast",
                ],
            )
            self.assertEqual(payload["summary"]["passed"], 2)

    def test_missing_python_is_reported_as_launch_error(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_package(root, {
                "test_one.py": (
                    "import unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_ok(self): pass\n"
                ),
            })

            completed, payload = _invoke(
                root,
                "--python",
                str(root / "missing-python"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(payload["summary"]["launch_error"], 1)
            result = payload["modules"][0]
            self.assertEqual(result["status"], "launch_error")
            self.assertIn("FileNotFoundError", result["diagnostic"])

    def test_progress_report_is_always_complete_json(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_package(root, {
                "test_a_wait.py": (
                    "import time, unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_wait(self): time.sleep(.35)\n"
                ),
                "test_b_pass.py": (
                    "import unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_ok(self): pass\n"
                ),
            })
            report = root / "progress.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER),
                    "--root",
                    str(root),
                    "--report",
                    str(report),
                    "--timeout-seconds",
                    "2",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            reads = 0
            observed_running = False
            deadline = time.monotonic() + 10
            stdout = ""
            stderr = ""
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    if report.exists():
                        payload = _try_read_atomic_report(report)
                        if payload is not None:
                            reads += 1
                            observed_running = (
                                observed_running or payload["state"] == "running"
                            )
                    time.sleep(.005)
                stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    tail_stdout, tail_stderr = process.communicate()
                    stdout += tail_stdout
                    stderr += tail_stderr

            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertGreater(reads, 1)
            self.assertTrue(observed_running)
            self.assertEqual(_read_atomic_report(report)["state"], "complete")
            self.assertFalse(any(root.glob("progress.json.*.tmp")))


if __name__ == "__main__":
    unittest.main()
