"""E49：D109 D4 之后 BoundedAsyncTraceWriter 的关闭与退出（D108 审查 E41 的续篇）。

  场景 1：sink 卡住，close(timeout_seconds=1.0) 是否按期返回 INCOMPLETE；放开后再次 close 是否 SEALED；
  场景 2：sink 卡住、owner 漏调 close，解释器能否退出；
  场景 3：sink 正常（项目 JsonlTraceWriterV0）、owner 漏调 close，解释器能否退出，已接纳记录有多少真正落盘；
  场景 4：主线程持续写入约 6 KB 的记录，最后一条写完立即退出（不 close）。daemon worker 此时多半正在
          序列化或写文件，观察进程退出码和标准错误（解释器关闭时 daemon 线程持有缓冲写锁的已知风险）。
只调用项目类，不改项目文件。运行：python e49_trace_writer_exit.py <检出目录> [场景 4 重复次数]
"""
import collections, pathlib, subprocess, sys, tempfile, textwrap, threading, time

CHILD_STUCK = textwrap.dedent('''
    import sys, threading
    sys.path.insert(0, {tree!r})
    from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
    class Sink:
        def __init__(self): self.gate = threading.Event()
        def write(self, record_type, payload): self.gate.wait()
        def close(self): pass
    writer = BoundedAsyncTraceWriter(Sink(), capacity=4)
    writer.write("probe", {{"n": 1}})
    print("main thread finished", flush=True)
''')

CHILD_JSONL = textwrap.dedent('''
    import sys
    sys.path.insert(0, {tree!r})
    from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
    from mc2p.runtime.trace import JsonlTraceWriterV0
    writer = BoundedAsyncTraceWriter(JsonlTraceWriterV0({path!r}), capacity=16384)
    payload = {{"blocks": [{{"position": [k, 63, k], "block_id": "minecraft:grass_block",
                             "sources": ["surface_depth"]}} for k in range({blocks})]}}
    for index in range({records}):
        writer.write("step", {{"index": index, **payload}})
    print(writer.stats.accepted_records, flush=True)
''')


def run_child(code, timeout=10):
    started = time.perf_counter()
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip(), time.perf_counter() - started
    except subprocess.TimeoutExpired:
        return None, "", "", time.perf_counter() - started


def main():
    tree = pathlib.Path(sys.argv[1]).resolve()
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 20
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
    started = time.perf_counter()
    first = writer.close(timeout_seconds=1.0)
    elapsed = time.perf_counter() - started
    gate.set()
    second = writer.close(timeout_seconds=2.0)
    print(f"  场景 1：close(1.0) 用时 {elapsed:.2f} s → {first.status.value}，worker_alive={first.worker_alive}，"
          f"accepted={first.accepted}，dropped={first.dropped}，pending={first.indeterminate_or_pending}，"
          f"failure={first.cleanup_failure.stage if first.cleanup_failure else None}；"
          f"放开后再 close → {second.status.value}，written={second.written}，worker_alive={second.worker_alive}")

    code, out, err, elapsed = run_child(CHILD_STUCK.format(tree=str(tree)), timeout=5)
    print(f"  场景 2：sink 卡住、未 close：{'%.1f s 内退出，返回码 %s' % (elapsed, code) if code is not None else '5 s 后仍未退出'}")

    with tempfile.TemporaryDirectory(prefix="e49-") as folder:
        path = str(pathlib.Path(folder) / "trace.jsonl")
        code, out, err, elapsed = run_child(CHILD_JSONL.format(tree=str(tree), path=path, blocks=40, records=2000))
        written = sum(1 for _ in open(path, encoding="utf-8")) if pathlib.Path(path).exists() else 0
        print(f"  场景 3：sink 正常、未 close：{'%.1f s 内退出，返回码 %s' % (elapsed, code) if code is not None else '仍未退出'}；"
              f"已接纳 {out or '?'} 条，文件里 {written} 行（未 close 时缺少 trace_delivery_summary）")

    outcomes, errors = collections.Counter(), collections.Counter()
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="e49-") as folder:
            path = str(pathlib.Path(folder) / "trace.jsonl")
            code, out, err, elapsed = run_child(CHILD_JSONL.format(tree=str(tree), path=path, blocks=60, records=400))
            outcomes["超时" if code is None else f"返回码 {code}"] += 1
            if err:
                errors[err.splitlines()[0][:110]] += 1
    print(f"  场景 4：{repeats} 次主线程写完即退出 → {dict(outcomes)}；标准错误首行 {dict(errors) or '无'}")


if __name__ == "__main__":
    main()
