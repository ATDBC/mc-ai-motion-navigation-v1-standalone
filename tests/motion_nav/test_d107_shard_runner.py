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
from unittest.mock import Mock, patch

import psutil

from scripts import run_motion_nav_shards as shard_runner
from scripts import process_tree
from scripts.process_tree import ProcessIdentityV0


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


def _wait_for_pid(path: Path, *, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(.01)
    raise TimeoutError(f"child pid was not published: {path}")


def _stop_pid(pid: int) -> None:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        for child in process.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        process.wait(timeout=3)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


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
    def test_linux_subreaper_requires_verified_prctl_and_empty_baseline(self):
        runner = ProcessIdentityV0(pid=123, create_time=456.0)

        with patch.object(shard_runner.sys, "platform", "linux"), patch.object(
            shard_runner, "_current_process_identity", return_value=runner,
        ), patch.object(
            shard_runner, "_direct_child_identities", return_value=(),
        ), patch.object(
            shard_runner, "_set_child_subreaper", return_value=None,
        ) as enable, patch.object(
            shard_runner, "_get_child_subreaper", return_value=True,
        ):
            guard = shard_runner._enable_linux_subreaper()

        self.assertEqual(guard.runner, runner)
        self.assertEqual(guard.baseline, ())
        enable.assert_called_once_with(True)

        existing = ProcessIdentityV0(pid=124, create_time=457.0)
        with patch.object(shard_runner.sys, "platform", "linux"), patch.object(
            shard_runner, "_current_process_identity", return_value=runner,
        ), patch.object(
            shard_runner, "_direct_child_identities", return_value=(existing,),
        ):
            with self.assertRaisesRegex(RuntimeError, "existing child"):
                shard_runner._enable_linux_subreaper()

        with patch.object(shard_runner.sys, "platform", "linux"), patch.object(
            shard_runner, "_current_process_identity", return_value=runner,
        ), patch.object(
            shard_runner, "_direct_child_identities", return_value=(),
        ), patch.object(
            shard_runner, "_set_child_subreaper", return_value=None,
        ), patch.object(
            shard_runner, "_get_child_subreaper", return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                shard_runner._enable_linux_subreaper()

    def test_linux_parent_fails_before_launch_when_subreaper_is_unavailable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_package(root, {
                "test_one.py": (
                    "import unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_ok(self): pass\n"
                ),
            })
            report = root / "report.json"
            with patch.object(shard_runner.sys, "platform", "linux"), patch.object(
                shard_runner,
                "_enable_linux_subreaper",
                side_effect=OSError("not supported"),
            ), patch.object(shard_runner, "_run_module") as run_module:
                return_code = shard_runner._run_parent([
                    "--root", str(root),
                    "--report", str(report),
                ])

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(return_code, 1)
            self.assertEqual(payload["state"], "complete")
            self.assertEqual(payload["posix_subreaper"], {"status": "unavailable"})
            self.assertIn("subreaper unavailable", payload["infrastructure_failure"])
            run_module.assert_not_called()

    def test_linux_subreaper_scope_is_exclusive(self):
        runner = shard_runner._current_process_identity()
        first_root = ProcessIdentityV0(pid=123, create_time=runner.create_time + 1.0)
        second_root = ProcessIdentityV0(pid=124, create_time=runner.create_time + 2.0)
        guard = shard_runner._LinuxSubreaperGuard(runner=runner, baseline=())

        with patch.object(
            shard_runner, "_direct_child_identities", return_value=(first_root,),
        ):
            scope = guard.begin(first_root)
            with self.assertRaisesRegex(RuntimeError, "already active"):
                guard.begin(second_root)
            scope.finish()
        with patch.object(
            shard_runner, "_direct_child_identities", return_value=(second_root,),
        ):
            second = guard.begin(second_root)
            second.finish()

    def test_subreaper_adoption_requires_stable_parent_identity_and_time_window(self):
        runner = shard_runner._current_process_identity()
        root = ProcessIdentityV0(pid=123, create_time=runner.create_time + 10.0)
        adopted = ProcessIdentityV0(pid=124, create_time=root.create_time + 1.0)
        too_old = ProcessIdentityV0(pid=125, create_time=root.create_time - 1.0)
        guard = shard_runner._LinuxSubreaperGuard(runner=runner, baseline=())
        with patch.object(
            shard_runner, "_direct_child_identities", return_value=(root,),
        ):
            scope = guard.begin(root)
        registered = {root.pid: root}
        diagnostics = []

        with patch.object(
            shard_runner, "_current_process_identity", return_value=runner,
        ), patch.object(
            shard_runner,
            "_direct_child_identities",
            return_value=(root, adopted, too_old),
        ), patch.object(
            shard_runner, "_stable_direct_child_identity", side_effect=lambda value, _pid: value,
        ):
            scope.capture_adopted(registered, diagnostics)

        self.assertEqual(registered, {root.pid: root, adopted.pid: adopted})
        self.assertEqual(scope.adopted, {adopted.pid: adopted})
        self.assertTrue(any("predates module root" in item for item in diagnostics))

    def test_subreaper_access_denied_and_pid_reuse_fail_closed(self):
        runner = shard_runner._current_process_identity()
        root = ProcessIdentityV0(pid=123, create_time=runner.create_time + 10.0)
        denied_hint = ProcessIdentityV0(pid=124, create_time=root.create_time + 1.0)
        reused_hint = ProcessIdentityV0(pid=125, create_time=root.create_time + 2.0)
        guard = shard_runner._LinuxSubreaperGuard(runner=runner, baseline=())
        with patch.object(
            shard_runner, "_direct_child_identities", return_value=(root,),
        ):
            scope = guard.begin(root)
        diagnostics = []
        registered = {root.pid: root}

        def stable(identity, _runner_pid):
            if identity.pid == denied_hint.pid:
                raise psutil.AccessDenied(identity.pid)
            if identity.pid == reused_hint.pid:
                return ProcessIdentityV0(identity.pid, identity.create_time + 10.0)
            return identity

        with patch.object(
            shard_runner, "_current_process_identity", return_value=runner,
        ), patch.object(
            shard_runner,
            "_direct_child_identities",
            return_value=(root, denied_hint, reused_hint),
        ), patch.object(
            shard_runner, "_stable_direct_child_identity", side_effect=stable,
        ):
            scope.capture_adopted(registered, diagnostics)

        self.assertEqual(registered, {root.pid: root})
        self.assertTrue(scope.ownership_failed)
        self.assertTrue(any("access denied" in item.lower() for item in diagnostics))
        self.assertTrue(any("identity changed" in item for item in diagnostics))

    def test_subreaper_never_authorizes_forged_or_baseline_pid(self):
        runner = shard_runner._current_process_identity()
        baseline = ProcessIdentityV0(pid=120, create_time=runner.create_time - 1.0)
        root = ProcessIdentityV0(pid=123, create_time=runner.create_time + 10.0)
        forged = ProcessIdentityV0(pid=999, create_time=root.create_time + 1.0)
        guard = shard_runner._LinuxSubreaperGuard(runner=runner, baseline=(baseline,))
        with patch.object(
            shard_runner,
            "_direct_child_identities",
            return_value=(baseline, root),
        ):
            scope = guard.begin(root)
        registered = {root.pid: root}

        with patch.object(
            shard_runner, "_current_process_identity", return_value=runner,
        ), patch.object(
            shard_runner,
            "_direct_child_identities",
            return_value=(baseline, root),
        ):
            scope.capture_adopted(registered, [])
        shard_runner._merge_child_registration(
            {"registered_tree": [{"pid": forged.pid, "create_time": forged.create_time}]},
            registered,
            [],
            authorized_identities=scope.adopted,
        )

        self.assertEqual(registered, {root.pid: root})
        self.assertNotIn(baseline.pid, scope.adopted)
        self.assertNotIn(forged.pid, registered)

    def test_subreaper_reaps_only_exact_adopted_children(self):
        runner = shard_runner._current_process_identity()
        root = ProcessIdentityV0(pid=123, create_time=runner.create_time + 10.0)
        adopted = ProcessIdentityV0(pid=124, create_time=root.create_time + 1.0)
        guard = shard_runner._LinuxSubreaperGuard(runner=runner, baseline=())
        with patch.object(
            shard_runner, "_direct_child_identities", return_value=(root,),
        ):
            scope = guard.begin(root)
        scope.adopted[adopted.pid] = adopted

        with patch.object(
            shard_runner, "_identity_is_alive", return_value=False,
        ), patch.object(
            shard_runner.os, "waitpid", return_value=(adopted.pid, 0), create=True,
        ) as waitpid:
            errors = scope.reap_stopped()

        self.assertEqual(errors, [])
        self.assertEqual(scope.reaped, {adopted.pid})
        waitpid.assert_called_once_with(adopted.pid, shard_runner._WAIT_NO_HANG)

    def test_settle_kills_exact_registered_descendants_without_another_sigterm(self):
        for newly_adopted in (False, True):
            with self.subTest(newly_adopted=newly_adopted):
                root = ProcessIdentityV0(123, 456.)
                child = ProcessIdentityV0(124, 457.)
                reused = ProcessIdentityV0(125, 458.)
                zombie = ProcessIdentityV0(126, 459.)
                exited = ProcessIdentityV0(127, 460.)
                identities = (root, child, reused, zombie, exited)
                processes = {}
                for identity in identities:
                    process = Mock()
                    process.create_time.return_value = identity.create_time
                    process.is_running.return_value = True
                    process.status.return_value = psutil.STATUS_RUNNING
                    processes[identity.pid] = process
                processes[reused.pid].create_time.return_value = 999.
                processes[zombie.pid].status.return_value = psutil.STATUS_ZOMBIE
                processes[exited.pid].is_running.return_value = False
                processes[child.pid].kill.side_effect = lambda: setattr(
                    processes[child.pid].is_running, "return_value", False,
                )
                registered = {item.pid: item for item in identities}
                scope = Mock()
                scope.adopted = {}
                scope.reaped = set()
                scope.ownership_failed = False
                scope.reap_stopped.return_value = []
                if newly_adopted:
                    del registered[child.pid]

                    def capture(registered, _diagnostics):
                        registered[child.pid] = child
                        scope.adopted[child.pid] = child
                        scope.reaped.add(child.pid)

                    scope.capture_adopted.side_effect = capture
                cleanup = {"errors": []}

                with patch.object(
                    shard_runner.psutil, "Process", side_effect=processes.__getitem__,
                ), patch.object(
                    shard_runner, "_identity_is_alive", return_value=False,
                ), patch.object(shard_runner.time, "sleep"), patch.object(
                    shard_runner, "_cleanup_process_tree", return_value={"errors": []},
                ) as terminate_tree:
                    result, observed = shard_runner._settle_linux_subreaper_scope(
                        scope, root, registered, cleanup, [], process=Mock(),
                    )

                processes[child.pid].kill.assert_called_once_with()
                for identity in (root, reused, zombie, exited):
                    processes[identity.pid].kill.assert_not_called()
                for process in processes.values():
                    process.terminate.assert_not_called()
                terminate_tree.assert_not_called()
                scope.finish.assert_called_once_with()
                self.assertEqual(result["status"], "passed")
                self.assertEqual(set(observed), set(identities))

    def test_settle_preserves_kill_access_denial_as_cleanup_failure(self):
        root = ProcessIdentityV0(123, 456.)
        child = ProcessIdentityV0(124, 457.)
        scope = Mock(adopted={}, reaped=set(), ownership_failed=False)
        scope.reap_stopped.return_value = []
        process = Mock()
        process.create_time.return_value = child.create_time
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_RUNNING
        process.kill.side_effect = psutil.AccessDenied(child.pid)

        with patch.object(
            shard_runner.psutil, "Process", return_value=process,
        ), patch.object(
            shard_runner, "_identity_is_alive", return_value=False,
        ), patch.object(shard_runner.time, "sleep"):
            result, _ = shard_runner._settle_linux_subreaper_scope(
                scope, root, {root.pid: root, child.pid: child},
                {"errors": []}, [], process=Mock(),
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("AccessDenied" in error for error in result["errors"]))
        process.terminate.assert_not_called()

    def test_zombie_is_stopped_after_exact_identity_match(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        zombie = Mock()
        zombie.pid = identity.pid
        zombie.create_time.return_value = identity.create_time
        zombie.is_running.return_value = True
        zombie.status.return_value = psutil.STATUS_ZOMBIE

        self.assertFalse(process_tree._is_live(zombie))
        with patch.object(shard_runner.psutil, "Process", return_value=zombie):
            self.assertFalse(shard_runner._identity_is_alive(identity))

    def test_liveness_access_denied_remains_conservatively_alive(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        inaccessible = Mock()
        inaccessible.pid = identity.pid
        inaccessible.create_time.return_value = identity.create_time
        inaccessible.is_running.return_value = True
        inaccessible.status.side_effect = psutil.AccessDenied(identity.pid)

        self.assertTrue(process_tree._is_live(inaccessible))
        with patch.object(
            shard_runner.psutil, "Process", return_value=inaccessible,
        ):
            self.assertTrue(shard_runner._identity_is_alive(identity))

        with patch.object(process_tree.psutil, "Process", return_value=inaccessible):
            cleanup = process_tree.terminate_registered_tree(
                identity, (identity,), grace_seconds=0,
            )
        self.assertEqual(cleanup.surviving, (identity,))

    def test_identity_inspection_denial_never_grants_cleanup_success(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        inaccessible = Mock()
        inaccessible.pid = identity.pid
        inaccessible.create_time.side_effect = psutil.AccessDenied(identity.pid)

        with patch.object(process_tree.psutil, "Process", return_value=inaccessible):
            cleanup = process_tree.terminate_registered_tree(
                identity, (identity,), grace_seconds=0,
            )

        self.assertEqual(cleanup.surviving, (identity,))
        self.assertTrue(any("access denied" in error for error in cleanup.errors))
        inaccessible.terminate.assert_not_called()

    def test_refresh_recomputes_cleanup_status_from_final_facts(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        cleanup = {
            "status": "failed",
            "stopped": [],
            "surviving": [{"pid": 123, "create_time": 456.0}],
            "errors": [],
        }

        for _ in range(30):
            cleanup["status"] = "failed"
            cleanup["surviving"] = [{"pid": 123, "create_time": 456.0}]
            with patch.object(shard_runner, "_identity_is_alive", return_value=False):
                shard_runner._refresh_cleanup_liveness(cleanup, (identity,))
            self.assertEqual(cleanup["status"], "passed")
            self.assertEqual(cleanup["surviving"], [])

    def test_refresh_preserves_real_survivor_or_error_as_failure(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        survivor = {
            "status": "passed", "stopped": [], "surviving": [], "errors": [],
        }
        with patch.object(shard_runner, "_identity_is_alive", return_value=True):
            shard_runner._refresh_cleanup_liveness(survivor, (identity,))
        self.assertEqual(survivor["status"], "failed")

        errored = {
            "status": "failed", "stopped": [], "surviving": [],
            "errors": ["access denied"],
        }
        with patch.object(shard_runner, "_identity_is_alive", return_value=False):
            shard_runner._refresh_cleanup_liveness(errored, (identity,))
        self.assertEqual(errored["status"], "failed")

    def test_reused_pid_is_not_treated_as_registered_process(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        reused = Mock()
        reused.pid = identity.pid
        reused.create_time.return_value = 789.0
        reused.is_running.return_value = True
        reused.status.return_value = psutil.STATUS_RUNNING

        with patch.object(shard_runner.psutil, "Process", return_value=reused):
            self.assertFalse(shard_runner._identity_is_alive(identity))

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

    def test_zero_discovered_tests_is_an_error(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_package(root, {
                "test_empty.py": "VALUE = 'there are no tests here'\n",
            })

            completed, payload = _invoke(root)

            self.assertEqual(completed.returncode, 1)
            result = payload["modules"][0]
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["tests_run"], 0)
            self.assertGreaterEqual(result["errors"], 1)
            self.assertIn("zero tests", result["diagnostic"])

    def test_timeout_kills_descendant_holding_output_pipe_with_bounded_cleanup(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "descendant.pid"
            helper = root / "hold_pipe.py"
            helper.write_text(
                "import os, signal, sys, time\n"
                "from pathlib import Path\n"
                "if os.name != 'nt': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
                "print('descendant-ready', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            _write_package(root, {
                "test_leaves_descendant.py": (
                    "import subprocess, sys, time, unittest\n"
                    "from pathlib import Path\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_spawn(self):\n"
                    "  root = Path(__file__).resolve().parents[2]\n"
                    "  subprocess.Popen([sys.executable, str(root / 'hold_pipe.py'), "
                    "str(root / 'descendant.pid')])\n"
                    "  time.sleep(30)\n"
                ),
            })
            report = root / "report.json"
            started = time.monotonic()
            runner = subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER),
                    "--root",
                    str(root),
                    "--report",
                    str(report),
                    "--timeout-seconds",
                    "3",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            descendant_pid = None
            try:
                descendant_pid = _wait_for_pid(pid_path)
                runner.wait(timeout=11)
                elapsed = time.monotonic() - started
                payload = _read_atomic_report(report)
                result = payload["modules"][0]
                self.assertEqual(runner.returncode, 1)
                self.assertEqual(result["status"], "timeout")
                self.assertLess(elapsed, 11)
                self.assertEqual(result["cleanup"]["status"], "passed")
                self.assertGreaterEqual(result["cleanup"]["registered_processes"], 2)
                self.assertEqual(result["cleanup"]["surviving"], [])
                self.assertFalse(psutil.pid_exists(descendant_pid))
            finally:
                if runner.poll() is None:
                    _stop_pid(runner.pid)
                if descendant_pid is not None:
                    _stop_pid(descendant_pid)

    def test_linux_subreaper_adversarial_matrix(self):
        scenarios = {
            "fast_root_exit": (
                ["daemon"],
                """
import os, subprocess, sys, time, unittest
from pathlib import Path
class Case(unittest.TestCase):
 def test_exit(self):
  root = Path(__file__).resolve().parents[2]
  subprocess.Popen([sys.executable, '-c',
   \"import os,time; from pathlib import Path; Path(r'%s').write_text(str(os.getpid())); time.sleep(30)\"])
  deadline=time.monotonic()+2
  while not Path(r'%s').exists() and time.monotonic()<deadline: time.sleep(.001)
  os._exit(7)
""",
            ),
            "setsid": (
                ["daemon"],
                """
import subprocess, sys, time, unittest
class Case(unittest.TestCase):
 def test_wait(self):
  subprocess.Popen([sys.executable, '-c',
   \"import os,signal,time; from pathlib import Path; os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(r'%s').write_text(str(os.getpid())); time.sleep(30)\"])
  time.sleep(30)
""",
            ),
            "double_fork": (
                ["daemon"],
                """
import subprocess, sys, time, unittest
class Case(unittest.TestCase):
 def test_wait(self):
  code=\"\"\"import os,time
os.setsid()
if os.fork()==0:
 from pathlib import Path
 Path(r'%s').write_text(str(os.getpid()))
 time.sleep(30)
os._exit(0)
\"\"\"
  subprocess.Popen([sys.executable,'-c',code]).wait()
  time.sleep(30)
""",
            ),
            "ignore_sigterm_holds_output": (
                ["daemon"],
                """
import subprocess, sys, time, unittest
class Case(unittest.TestCase):
 def test_wait(self):
  subprocess.Popen([sys.executable,'-c',
   \"import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(r'%s').write_text(str(os.getpid())); print('ready',flush=True); time.sleep(30)\"])
  time.sleep(30)
""",
            ),
            "fork_during_cleanup": (
                ["daemon", "late"],
                """
import subprocess, sys, time, unittest
class Case(unittest.TestCase):
 def test_wait(self):
  code=\"\"\"import os,signal,time
from pathlib import Path
Path(r'%s').write_text(str(os.getpid()))
def stop(_signum,_frame):
 pid=os.fork()
 if pid==0:
  os.setsid()
  time.sleep(30)
  os._exit(0)
 Path(r'%s').write_text(str(pid))
 os._exit(0)
signal.signal(signal.SIGTERM,stop)
time.sleep(30)
\"\"\"
  subprocess.Popen([sys.executable,'-c',code])
  time.sleep(30)
""",
            ),
        }
        if not sys.platform.startswith("linux"):
            self.assertEqual(
                set(scenarios),
                {
                    "fast_root_exit", "setsid", "double_fork",
                    "ignore_sigterm_holds_output", "fork_during_cleanup",
                },
            )
            return

        for name, (pid_names, template) in scenarios.items():
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                pid_paths = {
                    pid_name: root / f"{pid_name}.pid" for pid_name in pid_names
                }
                substitutions = []
                if name == "fast_root_exit":
                    substitutions = [pid_paths["daemon"], pid_paths["daemon"]]
                elif name == "fork_during_cleanup":
                    substitutions = [pid_paths["daemon"], pid_paths["late"]]
                else:
                    substitutions = [pid_paths["daemon"]]
                _write_package(root, {
                    f"test_{name}.py": template % tuple(map(str, substitutions)),
                })
                completed, payload = _invoke(
                    root, "--timeout-seconds", "1", timeout=15,
                )
                result = payload["modules"][0]
                try:
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(result["cleanup"]["status"], "passed")
                    self.assertEqual(result["cleanup"]["surviving"], [])
                    self.assertEqual(
                        result["cleanup"]["isolation"],
                        "linux_subreaper_posix_session",
                    )
                    self.assertTrue(result["cleanup"]["adopted_reaped"])
                    adopted_pids = {
                        item["pid"]
                        for item in result["cleanup"]["adopted_processes"]
                    }
                    attempted_pids = {
                        item["pid"] for item in result["cleanup"]["attempted"]
                    }
                    for pid_name, pid_path in pid_paths.items():
                        self.assertTrue(
                            pid_path.is_file(),
                            f"{name} did not establish {pid_name}",
                        )
                        self.assertFalse(
                            psutil.pid_exists(int(pid_path.read_text())),
                            f"{name} left {pid_name} alive or unreaped",
                        )
                        self.assertIn(
                            int(pid_path.read_text()),
                            attempted_pids | adopted_pids,
                            f"{name} did not register {pid_name} for cleanup",
                        )
                    if name == "fork_during_cleanup":
                        required_adopted = {"late"}
                    elif name in {"fast_root_exit", "double_fork"}:
                        required_adopted = {"daemon"}
                    else:
                        required_adopted = set()
                    for pid_name in required_adopted:
                        self.assertIn(
                            int(pid_paths[pid_name].read_text()),
                            adopted_pids,
                            f"{name} did not record {pid_name} as adopted",
                        )
                finally:
                    for pid_path in pid_paths.values():
                        if pid_path.is_file():
                            _stop_pid(int(pid_path.read_text()))

    def test_cleanup_exception_is_preserved_as_structured_failure(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)

        with patch.object(
            shard_runner,
            "terminate_registered_tree",
            side_effect=RuntimeError("cleanup exploded"),
        ):
            result = shard_runner._cleanup_process_tree(identity, (identity,))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["registered_processes"], 1)
        self.assertEqual(result["attempted"], [{"pid": 123, "create_time": 456.0}])
        self.assertEqual(result["surviving"], [])
        self.assertIn("RuntimeError: cleanup exploded", result["errors"])

    @unittest.skipUnless(os.name == "nt", "Windows Job Object contract")
    def test_fast_root_exit_cannot_escape_descendant_from_windows_job(self):
        for iteration in range(10):
            with self.subTest(iteration=iteration), TemporaryDirectory() as temporary:
                root = Path(temporary)
                pid_path = root / "descendant.pid"
                helper = root / "fast_descendant.py"
                helper.write_text(
                    "import os, sys, time\n"
                    "from pathlib import Path\n"
                    "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                _write_package(root, {
                    "test_fast_exit.py": (
                        "import os, subprocess, sys, time, unittest\n"
                        "from pathlib import Path\n"
                        "class Case(unittest.TestCase):\n"
                        " def test_exit(self):\n"
                        "  root = Path(__file__).resolve().parents[2]\n"
                        "  pid_file = root / 'descendant.pid'\n"
                        "  subprocess.Popen([sys.executable, str(root / 'fast_descendant.py'), "
                        "str(pid_file)])\n"
                        "  deadline = time.monotonic() + .2\n"
                        "  while not pid_file.exists() and time.monotonic() < deadline: time.sleep(.001)\n"
                        "  os._exit(7)\n"
                    ),
                })
                report = root / "report.json"
                runner = subprocess.run(
                    [
                        sys.executable,
                        str(RUNNER),
                        "--root",
                        str(root),
                        "--report",
                        str(report),
                        "--timeout-seconds",
                        "3",
                    ],
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=11,
                )
                descendant_pid = (
                    int(pid_path.read_text(encoding="utf-8"))
                    if pid_path.is_file()
                    else None
                )
                try:
                    self.assertEqual(runner.returncode, 1)
                    result = _read_atomic_report(report)["modules"][0]
                    self.assertEqual(result["cleanup"]["isolation"], "windows_job")
                    self.assertEqual(result["cleanup"]["status"], "passed")
                    self.assertEqual(result["cleanup"]["surviving"], [])
                    if descendant_pid is not None:
                        self.assertFalse(psutil.pid_exists(descendant_pid))
                finally:
                    if descendant_pid is not None:
                        _stop_pid(descendant_pid)

    def test_cleanup_exception_falls_back_to_owned_popen_handle(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=ROOT,
        )
        identity = ProcessIdentityV0(
            pid=process.pid,
            create_time=psutil.Process(process.pid).create_time(),
        )
        try:
            with patch.object(
                shard_runner,
                "terminate_registered_tree",
                side_effect=RuntimeError("cleanup exploded"),
            ):
                result = shard_runner._cleanup_process_tree(
                    identity,
                    (identity,),
                    process=process,
                )

            self.assertEqual(result["status"], "failed")
            self.assertIn("RuntimeError: cleanup exploded", result["errors"])
            self.assertEqual(result["surviving"], [])
            self.assertIn(
                {"pid": identity.pid, "create_time": identity.create_time},
                result["stopped"],
            )
            self.assertIsNotNone(process.returncode)
            self.assertFalse(psutil.pid_exists(identity.pid))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def test_conflicting_create_time_never_replaces_registered_pid(self):
        original = ProcessIdentityV0(pid=123, create_time=456.0)
        reused = ProcessIdentityV0(pid=123, create_time=789.0)
        registered = {123: original}
        diagnostics = []

        shard_runner._register_scanned_identity(reused, registered, diagnostics)

        self.assertEqual(registered, {123: original})
        self.assertIn("pid 123 identity changed", diagnostics[0])

    def test_child_registration_diagnostic_is_not_dropped(self):
        identity = ProcessIdentityV0(pid=123, create_time=456.0)
        registered = {123: identity}
        diagnostics = []

        shard_runner._merge_child_registration(
            {"registered_tree": [], "registration_diagnostic": "child scan failed"},
            registered,
            diagnostics,
            authorized_identities={},
        )

        self.assertIn("child scan failed", diagnostics)

    def test_child_registration_uses_parent_authority_and_never_replaces_conflict(self):
        parent = ProcessIdentityV0(pid=123, create_time=456.0)
        child_only = ProcessIdentityV0(pid=124, create_time=457.0)
        authorized = ProcessIdentityV0(pid=124, create_time=458.0)
        registered = {123: parent}
        diagnostics = []

        shard_runner._merge_child_registration(
            {
                "registered_tree": [
                    {"pid": child_only.pid, "create_time": child_only.create_time},
                    {"pid": parent.pid, "create_time": 999.0},
                ],
            },
            registered,
            diagnostics,
            authorized_identities={124: authorized},
        )

        self.assertEqual(registered[124], authorized)
        self.assertEqual(registered[123], parent)
        self.assertTrue(any("container-confirmed identity" in item for item in diagnostics))
        self.assertTrue(any("retained scan-confirmed identity" in item for item in diagnostics))

    def test_unconfirmed_child_report_cannot_grant_cleanup_authority(self):
        root_process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=ROOT,
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=ROOT,
        )
        root = ProcessIdentityV0(
            root_process.pid,
            psutil.Process(root_process.pid).create_time(),
        )
        unrelated_identity = ProcessIdentityV0(
            unrelated.pid,
            psutil.Process(unrelated.pid).create_time(),
        )
        registered = {root.pid: root}
        diagnostics = []
        try:
            job = type(
                "OwnedJob",
                (),
                {"process_ids": lambda self: (root.pid,)},
            )()
            authorized = shard_runner._container_authorized_identities(
                {
                    "registered_tree": [{
                        "pid": unrelated_identity.pid,
                        "create_time": unrelated_identity.create_time,
                    }],
                },
                root,
                job=job,
                diagnostics=diagnostics,
            )
            shard_runner._merge_child_registration(
                {
                    "registered_tree": [{
                        "pid": unrelated_identity.pid,
                        "create_time": unrelated_identity.create_time,
                    }],
                },
                registered,
                diagnostics,
                authorized_identities=authorized,
            )

            cleanup = shard_runner._cleanup_process_tree(
                root,
                tuple(registered.values()),
                process=root_process,
            )

            self.assertEqual(cleanup["status"], "passed")
            self.assertNotIn(unrelated_identity.pid, registered)
            self.assertTrue(psutil.Process(unrelated.pid).is_running())
            self.assertTrue(any("unconfirmed child process identity" in item for item in diagnostics))
        finally:
            _stop_pid(root_process.pid)
            _stop_pid(unrelated.pid)
            root_process.wait(timeout=3)
            unrelated.wait(timeout=3)

    def test_container_confirmation_uses_live_identity_not_child_create_time(self):
        reported = {"registered_tree": [{"pid": 124, "create_time": 999.0}]}
        job = type("Job", (), {"process_ids": lambda self: (124,)})()
        live_process = type("LiveProcess", (), {"create_time": lambda self: 457.0})()
        diagnostics = []

        with patch.object(shard_runner.psutil, "Process", return_value=live_process):
            authorized = shard_runner._container_authorized_identities(
                reported,
                ProcessIdentityV0(123, 456.0),
                job=job,
                diagnostics=diagnostics,
            )

        self.assertEqual(authorized, {124: ProcessIdentityV0(124, 457.0)})

    def test_job_membership_must_survive_live_identity_read(self):
        report = {"registered_tree": [{"pid": 124, "create_time": 999.0}]}
        job = type(
            "ChangingJob",
            (),
            {"process_ids": Mock(side_effect=((124,), ()))},
        )()
        live_process = type("LiveProcess", (), {"create_time": lambda self: 457.0})()

        with patch.object(shard_runner.psutil, "Process", return_value=live_process):
            authorized = shard_runner._container_authorized_identities(
                report,
                ProcessIdentityV0(123, 456.0),
                job=job,
                diagnostics=[],
            )

        self.assertEqual(authorized, {})

    def test_posix_child_hint_requires_live_membership_in_owned_process_group(self):
        report = {
            "registered_tree": [
                {"pid": 124, "create_time": 999.0},
                {"pid": 125, "create_time": 999.0},
            ],
        }

        class LiveProcess:
            def __init__(self, pid):
                self.pid = pid

            def create_time(self):
                return 450.0 + self.pid

        with patch.object(shard_runner.os, "name", "posix"), patch.object(
            shard_runner.os,
            "getpgid",
            side_effect=lambda pid: 123 if pid == 124 else 999,
            create=True,
        ), patch.object(shard_runner.psutil, "Process", side_effect=LiveProcess):
            authorized = shard_runner._container_authorized_identities(
                report,
                ProcessIdentityV0(123, 456.0),
                job=None,
                diagnostics=[],
            )

        self.assertEqual(authorized, {124: ProcessIdentityV0(124, 574.0)})

    def test_default_timeout_is_platform_specific_and_explicit_value_wins(self):
        self.assertEqual(shard_runner._default_module_timeout("nt"), 300.)
        self.assertEqual(shard_runner._default_module_timeout("posix"), 600.)
        parser = shard_runner._main_parser()
        self.assertEqual(
            parser.parse_args([]).timeout_seconds,
            shard_runner._default_module_timeout(os.name),
        )
        self.assertEqual(parser.parse_args(["--timeout-seconds", "17"]).timeout_seconds, 17.)

    def test_missing_cpython_popen_handle_fails_closed_before_job_assignment(self):
        class ProcessWithoutHandle:
            pass

        with self.assertRaisesRegex(RuntimeError, "CPython 3.11 Popen handle"):
            shard_runner._popen_windows_handle(ProcessWithoutHandle())


if __name__ == "__main__":
    unittest.main()
