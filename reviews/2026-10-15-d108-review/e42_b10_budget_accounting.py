"""E42：D108-C-01 的预算账。只读公开证据和项目源码，不运行 Fabric。

1. 从 B10 正式命令和探针源码确定实际期限：命令未传 --timeout-seconds 时取默认值，
   任务期限 = 探针启动时刻 + (timeout - 25) s；
2. 从四批 trace 审计读停止 tick、控制帧墙钟中位和 receiver 交付 tick，推出阶段位置；
3. 按 20 Hz 锁步估算停止时已花在控制循环上的时间，以及控制循环之前的启动开销；
4. 列出历史上 B10 正式命令是否带 --timeout-seconds。
运行：python e42_b10_budget_accounting.py <检出目录>
"""
import json, pathlib, re, subprocess, sys


def main():
    tree = pathlib.Path(sys.argv[1]).resolve()
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    probe = (tree / "scripts" / "probe_fabric_deployment_observation.py").read_text(encoding="utf-8")
    default = float(re.search(r"_DEFAULT_PROBE_TIMEOUT_SECONDS = ([0-9.]+)", probe).group(1))
    margin = int(re.search(r"deadline = time\.perf_counter_ns\(\) \+ round\(\(timeout - (\d+)\) \* 1e9\)", probe).group(1))
    maximum = re.search(r"or args\.navigation_review20_probe\) else (\d+)", probe).group(1)
    print(f"检出 {head}")
    print(f"  探针默认 timeout {default:.0f} s；任务期限 = 启动时刻 + (timeout - {margin}) s；B10 允许上限 {maximum} s")
    evidence = tree / "evidence" / "motion_navigation" / "d108-snapshot-hardening-v1"
    runs = (("b10-fabric", "trace-audit.json"), ("b10-fabric-a5r", "trace-audit-a5r.json"),
            ("b10-fabric-a5r2", "trace-audit-a5r2.json"), ("b10-fabric-a5r2-final", "trace-audit-a5r2-final.json"))
    for directory, audit_name in runs:
        command = json.loads((evidence / directory / "index.json").read_text(encoding="utf-8"))["command"]
        timeout = default
        if "--timeout-seconds" in command:
            timeout = float(command[command.index("--timeout-seconds") + 1])
        budget = timeout - margin
        audit = json.loads((evidence / audit_name).read_text(encoding="utf-8"))["runs"][0]
        stop = audit["ftick"]["input_sampled_ticks"]["last"]
        wall = audit["control_frames"]["control_wall_ms"]["p50"] / 1000
        # The first A5 batch used the legacy field name for the same tick.
        deliveries = [row.get("first_control_consume_tick", row.get("first_decision_consume_tick"))
                      for row in audit.get("receiver_diagnostics", {}).get("deliveries", [])]
        timelines = [json.loads(line) for line in (evidence / directory / "timelines.jsonl").read_text(encoding="utf-8").splitlines()]
        summary = json.loads((evidence / directory / "summary.json").read_text(encoding="utf-8"))
        loop = stop * wall
        phase = (f"40 次 validation 锚点 {timelines[0]['anchor_movement_tick_id']}–{timelines[-1]['anchor_movement_tick_id']}；"
                 + (f"10 次默认协调链交付 {deliveries[0]}–{deliveries[-1]}；" if deliveries else "")
                 + f"停止 tick {stop}")
        print(f"  {directory:<22} 命令带 --timeout-seconds={'是' if '--timeout-seconds' in command else '否'}"
              f"（实际 {timeout:.0f} s，任务预算 {budget:.0f} s）")
        print(f"    {phase}")
        print(f"    控制帧墙钟中位 {wall * 1000:.1f} ms → 控制循环约 {loop:.0f} s；"
              f"推出循环前启动开销约 {budget - loop:.0f} s；"
              f"主失败 {summary['b10_primary_failure']['type']}: {summary['b10_primary_failure']['message']}")
    print("  阶段顺序（scripts/b10_gap_solver_runtime.py）：preflight → 40 validation → turn_preflight → "
          "40 turn_validation → 10 默认协调链 → R4（3 速度带 × 4 方向 × 5 次 = 60 次验证 + 24 个负例，每次先做最多 80 tick 的带速接近）")
    print("  历史 B10 正式命令：")
    for path in sorted((tree / "docs" / "motion_navigation" / "acceptance").glob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "--b10-gap-solver-probe" in line:
                match = re.search(r"--timeout-seconds\s+(\d+)", line)
                print(f"    {path.name}:{number}  --timeout-seconds {match.group(1) if match else '未传（默认）'}")
    for directory in sorted((tree / "evidence" / "motion_navigation").glob("*/**/index.json")):
        try:
            command = json.loads(directory.read_text(encoding="utf-8")).get("command")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(command, list) and "--b10-gap-solver-probe" in command:
            timeout = command[command.index("--timeout-seconds") + 1] if "--timeout-seconds" in command else "未传（默认）"
            summary = directory.parent / "summary.json"
            status = json.loads(summary.read_text(encoding="utf-8")).get("status") if summary.exists() else "无 summary"
            print(f"    {directory.parent.relative_to(tree)}  --timeout-seconds {timeout}；status={status}")


if __name__ == "__main__":
    main()
