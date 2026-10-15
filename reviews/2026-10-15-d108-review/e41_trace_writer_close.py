"""E41：D108-C-03 的两个后果——owner 收尾无期限，以及非守护线程拖住解释器退出。

场景 1：sink.write 卡住（模拟磁盘或管道阻塞），写满队列后在另一个线程调用 close()，
        观察 2 s 后 close() 是否仍未返回。
场景 2：sink 卡住、写入一条记录后主线程直接结束（模拟 owner 的 finally 没走到 close，
        或 close 前就抛出），用子进程观察解释器能否在 3 s 内退出。
场景 3：对照组。sink 正常、写一条记录后不调用 close，观察解释器能否退出。
只导入项目的 BoundedAsyncTraceWriter，不改项目文件。运行：python e41_trace_writer_close.py <检出目录>
"""
import pathlib, subprocess, sys, textwrap, threading, time

CHILD = textwrap.dedent('''
    import sys, threading
    sys.path.insert(0, {tree!r})
    from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
    class Sink:
        def __init__(self, stuck): self.stuck = stuck; self.gate = threading.Event()
        def write(self, record_type, payload):
            if self.stuck: self.gate.wait()
        def close(self): pass
    writer = BoundedAsyncTraceWriter(Sink({stuck}), capacity=4)
    writer.write("probe", {{"n": 1}})
    print("main thread finished", flush=True)
''')


def main():
    tree = pathlib.Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(tree))
    from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}")

    gate = threading.Event()

    class StuckSink:
        def write(self, record_type, payload):
            gate.wait()

        def close(self):
            pass

    writer = BoundedAsyncTraceWriter(StuckSink(), capacity=4)
    for index in range(10):
        writer.write("probe", {"n": index})
    stats = writer.stats
    done = threading.Event()
    closer = threading.Thread(target=lambda: (writer.close(), done.set()), daemon=True)
    started = time.perf_counter()
    closer.start()
    closer.join(2.0)
    print(f"  场景 1：队列容量 4，接收 {stats.accepted_records}，丢弃 {stats.dropped_records}；"
          f"close() 调用 2.0 s 后已返回 = {done.is_set()}")
    gate.set()
    closer.join(5.0)
    print(f"          放开 sink 后 close() 返回 = {done.is_set()}，总耗时 {time.perf_counter() - started:.1f} s")

    for label, stuck in (("场景 2：sink 卡住、未调用 close", True), ("场景 3：sink 正常、未调用 close", False)):
        started = time.perf_counter()
        try:
            result = subprocess.run([sys.executable, "-c", CHILD.format(tree=str(tree), stuck=stuck)],
                                    capture_output=True, text=True, timeout=3)
            outcome = f"{time.perf_counter() - started:.1f} s 内退出，返回码 {result.returncode}"
        except subprocess.TimeoutExpired as error:
            printed = (error.stdout or b"").decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
            outcome = f"3 s 后仍未退出（主线程已打印：{printed.strip()!r}），由 subprocess 超时强杀"
        print(f"  {label}：解释器 {outcome}")


if __name__ == "__main__":
    main()
