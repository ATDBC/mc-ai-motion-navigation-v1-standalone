"""E68：D113 首个 10-tick 场景为什么在等待观察时超时——只读 D113 公开的原始 trace。

Runtime 的动作期限取 `min(本步期限, 胜出 intent 的 expires_at)`（`mc2p/runtime/arbiter_v1.py:237`）。
会话在 `awaiting_verified_motion` 时 `submit_input=False`，本帧不带新的移动 intent。生产驱动
`RuntimeNavigationDriver.prepare_proposals` 在这种帧之前会 `cancel_source` 撤销旧租约（见该文件注释），
B10 夹具的等待分支直接调用 `runtime.control_frame`，没有这一步，于是上一帧的 intent 被反复选中，
它约 250 ms 的过期时间成为后续每一步的动作期限。

本脚本按 trace 中的 step／dispatch／step_failure 记录：
  - 找出“连续多步选中同一个移动 intent”的段落，并按 ordered source 对应到协调器试次；
  - 给出每段最后一步“动作期限 − 派发时刻”的余量；
  - 给出失败那一步的余量，以及该步全部候选 intent 的剩余寿命（夹具自己的 0/1 源也在其中）。
运行：python e68_stale_intent_deadline.py <D113 formal-default/raw 目录或 targeted-5tick/raw 目录>
"""
import collections, gzip, json, pathlib, sys


def load(path: pathlib.Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def find(raw: pathlib.Path, name: str) -> pathlib.Path:
    for candidate in (raw / name, raw / f"{name}.gz"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(name)


def main():
    raw = pathlib.Path(sys.argv[1])
    trace = load(find(raw, "trace.jsonl"))
    trials = load(find(raw, "b10-coordinator-trials.jsonl"))
    source_trial = {trial["ordered_source_id"]: trial for trial in trials if trial.get("ordered_source_id")}
    expiry = {}
    for row in trace:
        if row["record_type"] == "ordered_intent":
            intent = row["payload"]["envelope"]["intent"]
            expiry[intent["intent_id"]] = intent["expires_at_monotonic_ns"]
    steps = []
    for row in trace:
        if row["record_type"] not in {"step", "step_failure"}:
            continue
        payload = row["payload"]
        action = payload["decision"]["action"]
        movement = [intent for group, intent in payload["decision"]["selected_intents"] if group == "movement"]
        dispatched = payload.get("dispatched_monotonic_ns")
        steps.append((row["record_type"], action["request_sequence_id"], movement[0] if movement else None,
                      action["deadline_monotonic_ns"], dispatched))
    runs = []
    for kind, sequence, intent, deadline, dispatched in steps:
        if runs and runs[-1]["intent"] == intent and intent is not None:
            runs[-1]["steps"].append((kind, sequence, deadline, dispatched))
        else:
            runs.append({"intent": intent, "steps": [(kind, sequence, deadline, dispatched)]})
    histogram = collections.Counter(len(run["steps"]) for run in runs if run["intent"])
    print(f"{raw.parent.name}/{raw.name}")
    print(f"  step 记录 {sum(kind == 'step' for kind, *_ in steps)}，step_failure {sum(kind == 'step_failure' for kind, *_ in steps)}")
    print(f"  连续选中同一移动 intent 的段长分布：{dict(sorted(histogram.items()))}")
    print("  段长 ≥ 2 的段（按试次）：")
    for run in runs:
        if run["intent"] is None or len(run["steps"]) < 2:
            continue
        source = run["intent"].rsplit("/", 1)[0]
        trial = source_trial.get(source)
        name = trial["name"] if trial else "（未写出试次终态）"
        delay = trial.get("worker_delay_ticks") if trial else None
        kind, sequence, deadline, dispatched = run["steps"][-1]
        slack = None if dispatched is None else (deadline - dispatched) / 1e6
        last = "step_failure（无派发后观察）" if kind == "step_failure" else f"最后一步余量 {slack:.4f} ms"
        print(f"    {name:<36} 延迟 {delay!s:>4}  intent …/{run['intent'].rsplit('/', 2)[-2]}/{run['intent'].rsplit('/', 1)[-1]}"
              f"  连续 {len(run['steps'])} 步  {last}")
    failure_rows = [row["payload"] for row in trace if row["record_type"] == "step_failure"]
    for payload in failure_rows:
        last_dispatch = max(step[4] for step in steps if step[0] == "step")
        print("  失败步的全部候选 intent（过期时刻 − 上一步派发）：")
        for intent in payload["decision"].get("candidate_intent_ids", ()):
            print(f"    {intent.rsplit('/', 3)[-3:]}  {(expiry[intent] - last_dispatch) / 1e6:8.2f} ms")
    failures = [step for step in steps if step[0] == "step_failure"]
    for kind, sequence, intent, deadline, _ in failures:
        previous = [step for step in steps if step[0] == "step" and step[2] == intent]
        if previous:
            gap = (deadline - previous[-1][4]) / 1e6
            print(f"  失败步 {sequence}：intent {intent.rsplit('/', 3)[-3:]}，期限距上一步派发 {gap:.4f} ms，"
                  f"按 50 ms 节拍下一步派发时只剩约 {gap - 50:.1f} ms")


if __name__ == "__main__":
    main()
