"""E39b：E39 末尾的 fd 增量从何而来（D108 的 feeder 错误回调造成引用环）。

D108 把原来只在 Windows 上安装的 `_on_queue_feeder_error` 回调改为所有平台都安装。回调闭包引用
worker 本身，形成 worker → Queue → 回调 → worker 的引用环，worker 不再在最后一个引用消失时立即释放，
而要等循环垃圾回收；它持有的描述符也随之延后关闭。本脚本：
  1. 关闭自动 GC，建 worker、杀子进程、close()、删除引用，用 weakref 看对象是否仍存活；
  2. 每个 E39 场景各做 10 次，记录不做 GC 时和 gc.collect() 之后的 fd 增量。
只调用项目函数，不改项目文件。运行：PYTHONPATH=<检出> python e39b_worker_reference_cycle.py
"""
import gc, importlib.util, os, pathlib, signal, subprocess, sys, time, weakref

from mc2p.motion_nav.motion_worker import MotionSolverWorker

spec = importlib.util.spec_from_file_location(
    "e39", pathlib.Path(__file__).with_name("e39_linux_worker_lifecycle.py"))
e39 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e39)


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    gc.disable()
    worker = MotionSolverWorker(max_pending=2)
    os.kill(worker.pid, signal.SIGKILL)
    time.sleep(0.3)
    try:
        worker.close()
    except OSError as error:
        print(f"检出 {head}：预热 close() 抛出 {type(error).__name__} errno={error.errno}")
    ref = weakref.ref(worker)
    del worker
    alive_without_gc = ref() is not None
    gc.collect()
    print(f"检出 {head}：删除最后一个引用后 worker 仍存活（需循环 GC）= {alive_without_gc}；"
          f"gc.collect() 后仍存活 = {ref() is not None}")
    for scenario in ("idle", "killed", "killed_full", "killed_large", "crash_large"):
        before = e39.fd_count()
        for _ in range(10):
            worker = MotionSolverWorker(max_pending=2)
            e39.prepare(worker, scenario)
            try:
                worker.close(timeout_seconds=5.0)
            except Exception:
                pass
            del worker
        time.sleep(0.3)
        without_gc = e39.fd_count() - before
        gc.collect()
        time.sleep(0.1)
        print(f"  {scenario:<13} 10 次后 fd 增量：不做 GC {without_gc:+d}，gc.collect() 后 {e39.fd_count() - before:+d}")


if __name__ == "__main__":
    main()
