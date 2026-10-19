"""E116：D121-S1 之后，一格跨隙的单动作求解和证明封装各花多少时间、多少字节。

只调用项目函数和项目夹具：tests.motion_nav.test_d102_action_handoff.ActionHandoffTests 的世界和
起点，项目的 solve_one_cell_gap、GapSolveRequest、LandingRegion，以及 canonical_motion_bytes／
canonical_motion_digest 和 pickle。同一脚本分别在 dce230f 与 3889d10 检出中运行。
每项重复 N 次，报告中位数与最大值（ms），以及证明的规范字节、pickle 字节和尾段状态数。
用法：python e116_solve_cost.py [N]
"""
import json
import pickle
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion, solve_one_cell_gap  # noqa: E402
from mc2p.motion_nav.online_motion import CandidateExecutionWindow  # noqa: E402
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_bytes, canonical_motion_digest  # noqa: E402
from tests.motion_nav.test_d102_action_handoff import ActionHandoffTests  # noqa: E402


def timed(function, repeats):
    samples, value = [], None
    for _ in range(repeats):
        started = time.perf_counter()
        value = function()
        samples.append((time.perf_counter() - started) * 1000.0)
    return value, {"median_ms": round(statistics.median(samples), 2), "max_ms": round(max(samples), 2)}


def main():
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    ActionHandoffTests.setUpClass()
    state = ActionHandoffTests.base.entry_state
    anchor = ActionHandoffTests.anchor(state, 1)
    request = GapSolveRequest((0, 1), LandingRegion(.3, .7, 2.3, 2.7, 1.),
                              CandidateExecutionWindow(state.movement_tick_id + 1, state.movement_tick_id + 2))
    solved, solve_cost = timed(lambda: solve_one_cell_gap(anchor, ActionHandoffTests.physics_world, request), repeats)
    proof = solved.proof
    _, canonical_cost = timed(lambda: canonical_motion_bytes(proof), repeats)
    _, digest_cost = timed(lambda: canonical_motion_digest(proof), repeats)
    _, pickle_cost = timed(lambda: pickle.dumps(proof, protocol=5), repeats)
    tails = getattr(proof, "fallback_tails", ())
    variant_tails = sum(len(getattr(v, "fallback_tails", ())) for v in proof.delayed_start_variants)
    print(json.dumps({
        "repeats": repeats,
        "solve_one_cell_gap": solve_cost,
        "canonical_motion_bytes": canonical_cost,
        "canonical_motion_digest": digest_cost,
        "pickle_dumps": pickle_cost,
        "proof_canonical_bytes": len(canonical_motion_bytes(proof)),
        "proof_pickle_bytes": len(pickle.dumps(proof, protocol=5)),
        "main_trajectory_states": len(proof.trajectory),
        "fallback_tails_main": len(tails),
        "fallback_tails_delayed_variants": variant_tails,
        "fallback_tail_states_main": sum(len(t.trajectory) for t in tails),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
