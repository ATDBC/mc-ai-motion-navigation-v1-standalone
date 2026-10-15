"""E48：D109 的 Linux subreaper 对抗矩阵（项目测试 test_linux_subreaper_adversarial_matrix）逐子场景结果。

该测试在 Windows 上只核对场景名，D109 交付时没有在 Linux 上执行过。这里在 Linux 上重复运行，
按子场景统计失败及其断言消息，并在对照检出上做同样的事。对照只改一处（e48_settle_sigkill_proposal.diff）：
settle 阶段直接 SIGKILL 仍存活的已登记进程，不再给 SIGTERM 处理函数第二次机会。测试文件不改。
另在每轮前后各取一次进程快照，统计测试退出后仍存活的场景进程（python -c 且命令行含 time.sleep(30)
和 .pid，不属于并行运行的另一个分片器，也不是本脚本的祖先进程），记下命令行后强杀。
运行：python e48_subreaper_matrix_subtests.py <项目检出> <对照检出> [次数]
"""
import collections, pathlib, re, subprocess, sys

import psutil

TEST = ("tests.motion_nav.test_d107_shard_runner.MotionNavShardRunnerTests."
        "test_linux_subreaper_adversarial_matrix")


def scenario_processes():
    """Scenario sleepers that do not belong to another running shard runner."""
    others = set()
    for process in psutil.process_iter(["pid", "cmdline"]):
        command = " ".join(process.info["cmdline"] or ())
        if "run_motion_nav_shards.py" in command and "--report" in command and "_child" not in command:
            try:
                others.update(child.pid for child in process.children(recursive=True))
            except psutil.Error:
                pass
    others.update(ancestor.pid for ancestor in psutil.Process().parents())
    others.add(psutil.Process().pid)
    found = {}
    for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
        argv = process.info["cmdline"] or []
        command = " ".join(argv)
        if (argv and pathlib.Path(argv[0]).name.startswith("python") and "-c" in argv
                and "time.sleep(30)" in command and ".pid" in command
                and process.info["pid"] not in others):
            found[process.info["pid"]] = (process.info["create_time"], command)
    return found


def leftovers(before):
    return {pid: value for pid, value in scenario_processes().items() if before.get(pid) != value}


def run(tree, repeats):
    per_subtest, outcomes, leaked = collections.Counter(), collections.Counter(), 0
    messages, examples = {}, {}
    for _ in range(repeats):
        before = scenario_processes()
        result = subprocess.run([sys.executable, "-m", "unittest", TEST], cwd=tree,
                                capture_output=True, text=True, timeout=600)
        outcomes[result.stderr.strip().splitlines()[-1]] += 1
        blocks = re.split(r"^={50,}$", result.stderr, flags=re.M)[1:]
        for block in blocks:
            name = re.search(r"\(name='([^']+)'\)", block)
            assertion = [line for line in block.splitlines() if line.startswith("AssertionError")]
            if name:
                per_subtest[name.group(1)] += 1
                messages.setdefault(name.group(1), assertion[-1][:120] if assertion else "?")
        stray = leftovers(before)
        leaked += len(stray)
        for pid, (_created, command) in stray.items():
            examples.setdefault(command[:160], 0)
            examples[command[:160]] += 1
        for pid in stray:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
    return outcomes, per_subtest, messages, leaked, examples


def main():
    project, control = sys.argv[1], sys.argv[2]
    repeats = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    for label, tree in (("项目检出", project), ("对照检出（settle 阶段直接 SIGKILL）", control)):
        head = subprocess.run(["git", "-C", tree, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", tree, "diff", "--stat"], capture_output=True, text=True).stdout.strip()
        outcomes, per_subtest, messages, leaked, examples = run(tree, repeats)
        print(f"{label} {head}{'（工作区有对照改动）' if dirty else ''}：{repeats} 次 → {dict(outcomes)}；"
              f"测试结束后残留的场景进程 {leaked} 个（已强杀）")
        for name in ("fast_root_exit", "setsid", "double_fork", "ignore_sigterm_holds_output", "fork_during_cleanup"):
            count = per_subtest.get(name, 0)
            print(f"  {name:<28} 失败 {count}/{repeats}" + (f"；{messages[name]}" if count else ""))
        for command, count in examples.items():
            print(f"  残留进程命令行（{count} 次）：{command}")


if __name__ == "__main__":
    main()
