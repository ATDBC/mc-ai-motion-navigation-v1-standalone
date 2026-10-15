"""E54b（沿用 D109 审查 E48b）：fork_during_cleanup 场景不经项目测试、直接交给分片器，看分片器返回后还剩什么。

项目测试在 finally 里会按 PID 文件强杀残留进程，因此看不到“分片器返回时是否仍有进程存活”。
本脚本复刻该场景：守护进程的 SIGTERM 处理函数会 fork 一个 setsid 子进程后退出，而 fork 出的子进程
继承同一个处理函数。分片器以 1 s 模块上限运行它，返回后读取报告，并检查最后写入 late.pid 的进程
是否还活着（随后强杀）。在 D110 和 D109 检出上各运行 R 次。
运行：python e54b_fork_during_cleanup.py <次数> <标签>=<检出> [<标签>=<检出> ...]
"""
import collections, json, pathlib, subprocess, sys, tempfile, time

import psutil


def run_once(tree):
    root = pathlib.Path(tempfile.mkdtemp(prefix="e48b-"))
    package = root / "tests" / "motion_nav"
    package.mkdir(parents=True)
    daemon, late = root / "daemon.pid", root / "late.pid"
    code = (f"import os,signal,time\nfrom pathlib import Path\n"
            f"Path(r'{daemon}').write_text(str(os.getpid()))\n"
            f"def stop(_signum,_frame):\n if os.fork()==0:\n  os.setsid()\n"
            f"  Path(r'{late}').write_text(str(os.getpid()))\n  time.sleep(30)\n os._exit(0)\n"
            f"signal.signal(signal.SIGTERM,stop)\ntime.sleep(30)\n")
    (package / "test_fork_during_cleanup.py").write_text(
        "import subprocess, sys, time, unittest\nclass Case(unittest.TestCase):\n def test_wait(self):\n"
        f"  subprocess.Popen([sys.executable,'-c',{code!r}])\n  time.sleep(30)\n", encoding="utf-8")
    report = root / "report.json"
    started = time.perf_counter()
    subprocess.run([sys.executable, str(pathlib.Path(tree) / "scripts" / "run_motion_nav_shards.py"),
                    "--root", str(root), "--report", str(report), "--timeout-seconds", "1"],
                   cwd=tree, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    elapsed = time.perf_counter() - started
    cleanup = json.loads(report.read_text(encoding="utf-8"))["modules"][0]["cleanup"]
    survivor = None
    if late.exists():
        pid = int(late.read_text())
        if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            survivor = pid
            psutil.Process(pid).kill()
    return elapsed, cleanup, survivor


def main():
    repeats = int(sys.argv[1])
    for label, tree in (item.split("=", 1) for item in sys.argv[2:]):
        statuses, survivors, registered, errors, durations = collections.Counter(), 0, [], collections.Counter(), []
        for _ in range(repeats):
            elapsed, cleanup, survivor = run_once(tree)
            durations.append(elapsed)
            statuses[cleanup["status"]] += 1
            survivors += survivor is not None
            registered.append(cleanup.get("registered_processes", 0))
            for error in cleanup.get("errors", []):
                errors[error] += 1
        print(f"{label}：{repeats} 次 → cleanup {dict(statuses)}；分片器返回后仍存活 {survivors} 次；"
              f"登记进程数 {min(registered)}–{max(registered)}；用时 {min(durations):.1f}–{max(durations):.1f} s")
        for error, count in errors.items():
            print(f"  错误（{count} 次）：{error}")


if __name__ == "__main__":
    main()
