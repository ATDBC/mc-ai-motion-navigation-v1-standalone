"""E39：D108 登记为“待三方复核”的 Linux 动态生命周期矩阵。

对 MotionSolverWorker（spawn 子进程 + 两个 multiprocessing.Queue）在 Linux 上实际创建、关闭，
每个场景重复 N 次，记录：关闭时抛出的异常（类型与 errno）、close() 用时、MotionCloseReport 的
timed_out／存活 feeder／预期与非预期 feeder 错误计数，以及全部场景结束后的文件描述符、线程和
子进程增量（泄漏检查）。每个场景期间把本进程的标准错误（fd 2，spawn 子进程会继承）
重定向到临时文件，只统计其中出现的异常类型和次数，避免原始回溯混进输出。
加 --no-capture 时不重定向（fd 布局与项目自身运行一致；D107 的竞态表现会随 fd 编号变化）。场景：
  idle          子进程存活、空闲，直接关闭；
  killed        SIGKILL 子进程后关闭；
  killed_full   SIGKILL 子进程后把请求队列塞满（put_nowait 直到 Full），再关闭；
  killed_large  SIGKILL 子进程后放入一条 8 MiB 消息（feeder 会卡在写满的管道上），再关闭；
  crash_large   子进程存活时放入 8 MiB 非作业消息，子进程读取后因非作业对象异常退出，再关闭。
后两类直接调用内部 Queue 注入故障，只为制造“feeder 被大消息阻塞”和“worker 异常退出”，
不改项目文件。运行：PYTHONPATH=<检出> python e39_linux_worker_lifecycle.py [每场景次数]
"""
import collections, os, queue, re, signal, statistics, subprocess, sys, tempfile, threading, time

import psutil

from mc2p.motion_nav.motion_worker import MotionSolverWorker

LARGE = b"x" * (8 * 1024 * 1024)


def fd_count():
    return len(os.listdir("/proc/self/fd"))


def wait_dead(worker, timeout=5.0):
    deadline = time.monotonic() + timeout
    while worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)


class CapturedStderr:
    """Redirect fd 2 for one scenario and count the exception lines written to it."""

    def __init__(self, enabled):
        self.enabled = enabled
        self.counts = collections.Counter()

    def __enter__(self):
        if not self.enabled:
            return self
        sys.stderr.flush()
        self.file = tempfile.TemporaryFile()
        self.saved = os.dup(2)
        os.dup2(self.file.fileno(), 2)
        return self

    def __exit__(self, *exc):
        if not self.enabled:
            return False
        sys.stderr.flush()
        os.dup2(self.saved, 2)
        os.close(self.saved)
        self.file.seek(0)
        text = self.file.read().decode("utf-8", "replace")
        self.file.close()
        self.counts = collections.Counter(
            match.group(1).rsplit(".", 1)[-1]
            for match in re.finditer(r"^([\w.]+(?:Error|Exception|Violation)):", text, re.M)
        )
        return False


def prepare(worker, scenario):
    if scenario == "idle":
        return
    if scenario == "crash_large":
        worker._requests.put_nowait(LARGE)
        wait_dead(worker)
        return
    os.kill(worker.pid, signal.SIGKILL)
    wait_dead(worker)
    if scenario == "killed_full":
        for _ in range(16):
            try:
                worker._requests.put_nowait(("filler", _))
            except queue.Full:
                break
    elif scenario == "killed_large":
        worker._requests.put_nowait(LARGE)
        time.sleep(0.05)  # let the feeder start writing into the unread pipe


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    numbers = [value for value in sys.argv[1:] if value.isdigit()]
    repeats = int(numbers[0]) if numbers else 20
    capture = "--no-capture" not in sys.argv
    me = psutil.Process()
    fds0, threads0, children0 = fd_count(), threading.active_count(), len(me.children(recursive=True))
    print(f"检出 {head}：每场景 {repeats} 次（Linux，spawn）{'' if capture else '；不重定向标准错误'}")
    for scenario in ("idle", "killed", "killed_full", "killed_large", "crash_large"):
        outcomes = collections.Counter()
        durations, timed_out, live_feeders, expected, unexpected = [], 0, 0, 0, 0
        with CapturedStderr(capture) as stderr:
            for _ in range(repeats):
                worker = MotionSolverWorker(max_pending=2)
                prepare(worker, scenario)
                started = time.perf_counter()
                try:
                    report = worker.close(timeout_seconds=5.0)
                    outcomes["正常返回"] += 1
                    timed_out += bool(report.timed_out)
                    live_feeders += report.live_queue_feeders
                    expected += report.expected_close_feeder_errors
                    unexpected += report.unexpected_feeder_errors
                except Exception as error:
                    outcomes[f"{type(error).__name__} errno={getattr(error, 'errno', None)}"] += 1
                durations.append(time.perf_counter() - started)
            time.sleep(0.2)
        print(f"  {scenario:<13} {dict(outcomes)}；close 中位 {statistics.median(durations) * 1000:7.1f} ms，"
              f"最大 {max(durations) * 1000:7.1f} ms；timed_out {timed_out}，存活 feeder {live_feeders}，"
              f"预期 feeder 错误 {expected}，非预期 {unexpected}；标准错误中的异常 {(dict(stderr.counts) or '无') if capture else '未统计'}")
    time.sleep(0.5)
    print(f"  全部结束后：fd 增量 {fd_count() - fds0}，线程增量 {threading.active_count() - threads0}，"
          f"存活子进程增量 {len([c for c in me.children(recursive=True) if c.is_running() and c.status() != psutil.STATUS_ZOMBIE]) - children0}")


if __name__ == "__main__":
    main()
