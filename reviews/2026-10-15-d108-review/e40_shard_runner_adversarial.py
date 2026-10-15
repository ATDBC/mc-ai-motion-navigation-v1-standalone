"""E40：D108 分片器“终止整棵进程树”在 Linux 上的对抗场景。

在临时目录里造 6 个测试模块，各自用 3 秒上限单独交给项目分片器运行：
  grandchild_sleep  孙进程睡 30 s，模块自己卡住（D107 审查 E32 的反例）；
  ignore_sigterm    孙进程忽略 SIGTERM 后睡 30 s，模块卡住；
  setsid_child      孙进程 setsid 离开进程组，但仍是后代，模块卡住；
  double_fork       孙进程 setsid 后再 fork、立刻退出中间层，留下的守护进程被 init 收养，模块卡住；
  pass_leave_bg     模块正常通过，但留下一个睡 30 s 的后台孙进程；
  zero_tests        模块里没有测试。
每个被启动的进程把自己的 PID 写进临时文件；先核对应登记的 PID 都已写出（否则场景未成立），
分片器返回后再检查这些 PID 是否仍存活，并对照报告里的 status、cleanup.status 和 surviving。
另外记录 cleanup 状态与“报告 surviving 为空”是否自相矛盾。
只运行项目脚本，不改项目文件。运行：python e40_shard_runner_adversarial.py <检出目录>
"""
import json, os, pathlib, signal, subprocess, sys, tempfile, textwrap, time

SPAWN = '''
import os, subprocess, sys, time, unittest
PIDS = {pids!r}

def spawn(code):
    return subprocess.Popen([sys.executable, "-c", code])

def record(name):
    return f"import os; open({PIDS!r} + '/{name}', 'w').write(str(os.getpid()))"
'''

EXPECTED = {
    "grandchild_sleep": ["grandchild"], "ignore_sigterm": ["grandchild"], "setsid_child": ["grandchild"],
    "double_fork": ["daemon"], "pass_leave_bg": ["grandchild"], "zero_tests": [],
}

MODULES = {
    "grandchild_sleep": SPAWN + '''
class T(unittest.TestCase):
    def test(self):
        spawn(record("grandchild") + "; import time; time.sleep(30)")
        time.sleep(600)
''',
    "ignore_sigterm": SPAWN + '''
class T(unittest.TestCase):
    def test(self):
        spawn("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " + record("grandchild") + "; import time; time.sleep(30)")
        time.sleep(600)
''',
    "setsid_child": SPAWN + '''
class T(unittest.TestCase):
    def test(self):
        spawn("import os; os.setsid(); " + record("grandchild") + "; import time; time.sleep(30)")
        time.sleep(600)
''',
    "double_fork": SPAWN + '''
class T(unittest.TestCase):
    def test(self):
        code = ("import os, time\\n"
                "os.setsid()\\n"
                "if os.fork() == 0:\\n"
                "    " + record("daemon") + "\\n"
                "    time.sleep(30)\\n"
                "os._exit(0)\\n")
        spawn(code).wait()
        time.sleep(600)
''',
    "pass_leave_bg": SPAWN + '''
class T(unittest.TestCase):
    def test(self):
        spawn(record("grandchild") + "; import time; time.sleep(30)")
        time.sleep(0.5)
''',
    "zero_tests": '''
import unittest
''',
}


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = open(f"/proc/{pid}/stat").read().split()[2]
    except OSError:
        return False
    return state != "Z"


def main():
    tree = pathlib.Path(sys.argv[1]).resolve()
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}：每个场景模块上限 3 s")
    for name, body in MODULES.items():
        root = pathlib.Path(tempfile.mkdtemp(prefix="e40-"))
        pids = root / "pids"
        pids.mkdir()
        (root / "tests" / "fake").mkdir(parents=True)
        (root / "tests" / "fake" / f"test_{name}.py").write_text(
            textwrap.dedent(body.replace("{pids!r}", repr(str(pids)))), encoding="utf-8")
        report = root / "report.json"
        started = time.perf_counter()
        subprocess.run([sys.executable, str(tree / "scripts" / "run_motion_nav_shards.py"),
                        "--root", str(root), "--tests-directory", "tests/fake",
                        "--timeout-seconds", "3", "--report", str(report)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        elapsed = time.perf_counter() - started
        module = json.loads(report.read_text(encoding="utf-8"))["modules"][0]
        cleanup = module.get("cleanup", {})
        survivors = []
        recorded = sorted(item.name for item in pids.iterdir())
        if recorded != EXPECTED[name]:
            recorded = f"{recorded}，应为 {EXPECTED[name]}（场景未成立）"
        for item in pids.iterdir():
            pid = int(item.read_text())
            if alive(pid):
                survivors.append(f"{item.name}:{pid}")
                os.kill(pid, signal.SIGKILL)
        print(f"  {name:<17} 返回 {elapsed:5.1f} s；status={module['status']}；"
              f"cleanup={cleanup.get('status')}，报告 surviving={len(cleanup.get('surviving', []))}；"
              f"登记到的 PID {recorded or '无'}，实际仍存活={survivors or '无'}；{(module.get('diagnostic') or '')[:60]}")


if __name__ == "__main__":
    main()
