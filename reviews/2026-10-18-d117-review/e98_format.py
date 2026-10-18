"""E98 输出整理：把 e98_momentum_feasibility.py 的 JSON 行整理成一行一个地形的中文摘要。

“静止可过”指中间平台上存在任一静止起点能跨过（frontmost_feasible_start_z 不为空），
不只看后缘起跑。kN_x=成功的第一跳落点数/全部，括号内为目标平台最大重叠余量（格）。
--only-momentum 只列出静止不可过、带速度可过的地形。
用法：python e98_format.py [--only-momentum] <jsonl> [...]
"""
import json
import sys


def main():
    args = sys.argv[1:]
    only = "--only-momentum" in args
    for path in [a for a in args if a != "--only-momentum"]:
        for line in open(path, encoding="utf-8"):
            if not line.strip().startswith("{"):
                continue
            r = json.loads(line)
            st = r["standstill"]
            n = len(r["first_landings"])
            c = r["chain_successes_and_best_margin"]
            stand = st["frontmost_feasible_start_z"] is not None
            momentum = any(v[0] for v in c.values())
            if only and (stand or not momentum):
                continue
            ss = (f"是（后缘起跑 {st['back_edge_jump_tick']} tick；最靠前可行起点 z={st['frontmost_feasible_start_z']}）"
                  if stand else "否")
            chains = " ".join(f"{k}={v[0]}/{n}" + (f"({v[1]})" if v[1] is not None else "") for k, v in c.items())
            print(f"DY={r['DY']:+d} W1={r['W1']} P={r['P']} W2={r['W2']} | 静止可过:{ss} | {chains} | "
                  f"潜行刹车 {sorted(set(map(str, r['brake_ticks']['sneak'])))} 后退 {sorted(set(map(str, r['brake_ticks']['back'])))}")


if __name__ == "__main__":
    main()
