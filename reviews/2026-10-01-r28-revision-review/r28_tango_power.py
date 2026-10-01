"""Decisiveness of the revised R28 paired gate (acceptance 5.2, d66d729 -> 0701299).

Gate: the paired 95% Tango score interval for delta = candidate - baseline success rate must have
a lower bound >= -0.01.  For an equally good candidate (true delta = 0) with discordance d, how
often does the gate pass at n pairs?  Also prints the normal-approximation sample size
n = (1.96 + 0.84)^2 * d / 0.01^2 that the plan quotes (3,200 / 6,400 pairs for d = 4% / 8%).

Orientation: b = baseline success / candidate fail, c = baseline fail / candidate success,
theta = (b - c) / n = -delta.  lower(delta) >= -0.01  <=>  upper(theta) <= 0.01
<=> Tango score Z(theta = 0.01) <= -1.96 (Z is decreasing in theta).
"""
import math
import random

Z = 1.96
MARGIN = 0.01


def tango_z(b, c, n, theta):
    m = n - b - c
    big_b = b * (1 - theta) + c * (1 - 3 * theta) - 2 * m * theta
    p21 = (big_b + math.sqrt(big_b * big_b + 8 * n * c * theta * (1 - theta))) / (4 * n)
    var = n * (2 * p21 + theta - theta * theta)
    return (b - c - n * theta) / math.sqrt(var)


def tango_interval(b, c, n):
    """Full 95% interval for delta = (c - b) / n by bisection on theta."""
    def root(lo, hi, target):
        for _ in range(80):
            mid = (lo + hi) / 2
            if tango_z(b, c, n, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    theta_hat = (b - c) / n
    upper_theta = root(theta_hat, 1 - 1e-12, -Z)
    lower_theta = root(-1 + 1e-12, theta_hat, Z)
    return -upper_theta, -lower_theta


def passes(b, c, n):
    return tango_z(b, c, n, MARGIN) <= -Z


print("Sanity checks of the interval (delta = candidate - baseline)")
for b, c, n in ((0, 0, 200), (0, 0, 1500), (3, 1, 200), (1, 3, 200), (30, 30, 1500), (60, 60, 3200)):
    lo, hi = tango_interval(b, c, n)
    print(f"  b={b:<3} c={c:<3} n={n:<5} -> [{100 * lo:6.2f}, {100 * hi:6.2f}] pt  pass={passes(b, c, n)}")

print("\nNormal-approximation size for 80% decisiveness at margin 1 pt: n = 7.84 * d / 0.0001")
for d in (0.02, 0.04, 0.08, 0.12):
    print(f"  d={d:.2f}: ~{math.ceil((1.96 + 0.8416) ** 2 * d / MARGIN ** 2)} pairs")

print("\nSimulated share of passing comparisons for an equally good candidate (Tango interval)")
rng = random.Random(20261001)
TRIALS = 2000
for d in (0.02, 0.04, 0.08):
    cells = []
    for n in (1500, 3200, 6400):
        ok = 0
        for _ in range(TRIALS):
            discordant = sum(1 for _ in range(n) if rng.random() < d)
            b = sum(1 for _ in range(discordant) if rng.random() < 0.5)
            ok += passes(b, discordant - b, n)
        cells.append(f"n={n}: {100 * ok / TRIALS:5.1f}%")
    print(f"  d={d:.2f}: " + "   ".join(cells))

print("\nAll-steps view: chance that k behaviour-changing steps are all decisive at 80% each")
for k in (1, 2, 3, 4):
    print(f"  k={k}: {100 * 0.8 ** k:5.1f}%")

print("\nOption: pre-declared two-look design (Pocock boundary z = 2.178 at each look, overall alpha 2.5% one-sided)")
POCOCK = 2.178
rng = random.Random(20261002)
for d, n in ((0.04, 3200), (0.08, 6400)):
    first = both = 0
    for _ in range(TRIALS):
        disc1 = sum(1 for _ in range(n) if rng.random() < d)
        b1 = sum(1 for _ in range(disc1) if rng.random() < 0.5)
        if tango_z(b1, disc1 - b1, n, MARGIN) <= -POCOCK:
            first += 1
            continue
        disc2 = sum(1 for _ in range(n) if rng.random() < d)
        b2 = sum(1 for _ in range(disc2) if rng.random() < 0.5)
        if tango_z(b1 + b2, disc1 + disc2 - b1 - b2, 2 * n, MARGIN) <= -POCOCK:
            both += 1
    print(f"  d={d:.2f}, {n} pairs per look: decided at look 1 {100 * first / TRIALS:5.1f}%, "
          f"after look 2 {100 * (first + both) / TRIALS:5.1f}%")
