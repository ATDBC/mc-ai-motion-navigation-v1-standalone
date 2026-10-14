"""E31：Linux 上 MotionSolverWorker.close() 为什么报 `OSError: [Errno 9] Bad file descriptor`。

在进程内记录 multiprocessing 连接对象每次进入底层关闭时的对象、描述符和调用线程，然后按项目
测试的用法 `with MotionSolverWorker(max_pending=...)` 打开并关闭 worker（有子进程、无任务）。
如果同一个连接对象被两个线程同时关闭，两边都可能通过 `_handle is not None` 检查：后到的一方
要么关闭已经释放的描述符（EBADF），要么在描述符号已被复用时误关无关文件；记录本身拉长了
竞争窗口，也可能看到 `_handle` 已被置空的 TypeError。只调用项目函数，不改项目文件。
运行：PYTHONPATH=<检出> python e31_linux_worker_close.py
"""
import collections, os, subprocess, threading, traceback
from multiprocessing import connection

from mc2p.motion_nav.motion_worker import MotionSolverWorker

closed = []
original_close = connection.Connection._close


def logged_close(self):
    closed.append((id(self), self._handle, threading.current_thread().name,
                   [frame.name for frame in traceback.extract_stack(limit=7)[:-1]]))
    return original_close(self)


connection.Connection._close = logged_close


def unlogged_rate(attempts=50):
    """Same open/close without the logging wrapper, to report the natural failure rate."""
    connection.Connection._close = original_close
    outcomes = collections.Counter()
    for _ in range(attempts):
        try:
            with MotionSolverWorker(max_pending=1):
                pass
            outcomes["正常关闭"] += 1
        except OSError as error:
            outcomes[f"OSError errno={error.errno}"] += 1
    connection.Connection._close = logged_close
    return dict(outcomes)


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}：不加记录包装时 50 次打开/关闭的结果 {unlogged_rate()}")
    outcomes = collections.Counter()
    for attempt in range(10):
        closed.clear()
        try:
            with MotionSolverWorker(max_pending=1):
                pass
            outcomes["正常关闭"] += 1
        except (OSError, TypeError) as error:
            outcomes[f"{type(error).__name__}" + (f" errno={error.errno}" if isinstance(error, OSError) else "")] += 1
        counts = collections.Counter(owner for owner, _, _, _ in closed)
        repeated = {owner for owner, n in counts.items() if n > 1}
        if repeated:
            outcomes["同一连接对象进入底层关闭两次"] += 1
            if not outcomes.get("_printed"):
                outcomes["_printed"] = 1
                print(f"第 {attempt + 1} 次：同一连接对象被关闭两次：")
                for owner, fd, thread, stack in closed:
                    if owner in repeated:
                        print(f"  fd={fd} 线程={thread} 调用链={' > '.join(stack[-4:])}")
    outcomes.pop("_printed", None)
    print(f"检出 {head}（{os.name}）：10 次打开/关闭的结果 {dict(outcomes)}")


if __name__ == "__main__":
    main()
