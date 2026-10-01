"""How decisive are the R28 paired success-rate gates at 200 / 400 seeds?

Acceptance section 5: perturbation groups may lose at most 1 percentage point (point estimate),
the paired 95% interval is reported, and a result whose interval cannot exclude a substantive
regression is "uncertain"; the sample may be extended once from 200 to 400 paired seeds.

This script assumes the candidate is exactly as good as the baseline and asks how often the
interval can still exclude a 1-point regression.  Paired Wald interval on the difference.
"""
import math
import random

Z = 1.96
MARGIN = 0.01


def wilson_lower(k, n):
    p = k / n
    centre = p + Z * Z / (2 * n)
    spread = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (centre - spread) / (1 + Z * Z / n)


def paired_interval(b, c, n):
    """b = baseline success / candidate fail, c = baseline fail / candidate success."""
    diff = (c - b) / n
    var = ((b + c) / n - diff * diff) / n
    half = Z * math.sqrt(max(var, 0.0))
    return diff - half, diff + half


def one_comparison(rng, n, discordance):
    b = c = 0
    for _ in range(n):
        u = rng.random()
        if u < discordance / 2:
            b += 1
        elif u < discordance:
            c += 1
    return b, c


print("Wilson 95% lower bound for the normal-input success rate (gate: >= 99%)")
for n in (200, 400):
    for miss in (0, 1, 2, 4):
        print(f"  n={n:<4} {n - miss}/{n}: point {100 * (n - miss) / n:6.2f}%  lower {100 * wilson_lower(n - miss, n):6.2f}%")

print("\nHalf-width of the paired 95% interval when candidate == baseline (expected discordance d)")
for d in (0.02, 0.04, 0.08):
    need = math.ceil(Z * Z * d / (MARGIN * MARGIN))
    print(f"  d={d:.2f}: n=200 -> +/-{100 * Z * math.sqrt(d / 200):.2f} pt, n=400 -> +/-{100 * Z * math.sqrt(d / 400):.2f} pt,"
          f" seeds needed for +/-1 pt ~ {need}")

print("\nSimulated: equal candidate, share of comparisons whose interval excludes a 1-point regression")
rng = random.Random(20261001)
for d in (0.02, 0.04, 0.08):
    decided_200 = decided_after_400 = point_fail = 0
    trials = 4000
    for _ in range(trials):
        b, c = one_comparison(rng, 200, d)
        if (b - c) / 200 > MARGIN:
            point_fail += 1
        if paired_interval(b, c, 200)[0] > -MARGIN:
            decided_200 += 1
            continue
        b2, c2 = one_comparison(rng, 200, d)
        if paired_interval(b + b2, c + c2, 400)[0] > -MARGIN:
            decided_after_400 += 1
    print(f"  d={d:.2f}: decided at 200 {100 * decided_200 / trials:5.1f}%, after extension to 400 "
          f"{100 * (decided_200 + decided_after_400) / trials:5.1f}%, point estimate already > 1 pt worse "
          f"{100 * point_fail / trials:5.1f}%")
