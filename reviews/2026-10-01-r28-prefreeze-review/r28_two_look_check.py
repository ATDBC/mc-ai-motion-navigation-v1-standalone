"""Check the two-look (Pocock 2.178) Tango gate now frozen in R28 acceptance 5.2 (e4308d9).

For each discordance d and first-look size n (second look adds n new pairs only if the first
look is undecided), estimate:
  * decisiveness when the candidate is exactly as good as the baseline (true delta = 0);
  * false pass rate when the candidate is exactly 1 point worse (true delta = -0.01),
    which the plan quotes as about 2.2%-2.5%;
  * false pass rate at 1.5 points worse.
Counts are drawn with numpy multinomials; the Tango statistic is the same closed form as in
reviews/2026-10-01-r28-revision-review/r28_tango_power.py.
"""
import numpy as np

BOUND = 2.178
MARGIN = 0.01
TRIALS = 200_000
rng = np.random.default_rng(20261001)


def tango_z(b, c, n, theta=MARGIN):
    m = n - b - c
    big_b = b * (1 - theta) + c * (1 - 3 * theta) - 2 * m * theta
    p21 = (big_b + np.sqrt(big_b * big_b + 8 * n * c * theta * (1 - theta))) / (4 * n)
    return (b - c - n * theta) / np.sqrt(n * (2 * p21 + theta - theta * theta))


def run(d, n, delta):
    p_b, p_c = (d - delta) / 2, (d + delta) / 2      # delta = (c - b) / n in expectation
    probs = [p_b, p_c, 1 - p_b - p_c]
    first = rng.multinomial(n, probs, size=TRIALS)
    second = rng.multinomial(n, probs, size=TRIALS)
    z1 = tango_z(first[:, 0], first[:, 1], n)
    pass1 = z1 <= -BOUND
    # "regression" at a look: the interval lies wholly below -0.01; score test of theta=0.01 the other way
    b2, c2 = first[:, 0] + second[:, 0], first[:, 1] + second[:, 1]
    z2 = tango_z(b2, c2, 2 * n)
    pass2 = ~pass1 & (z2 <= -BOUND)
    return pass1.mean(), (pass1 | pass2).mean()


print(f"{'d':>5} {'n first':>8} | {'delta=0: look1':>14} {'cumulative':>10} | "
      f"{'delta=-1pt: false pass':>22} | {'delta=-1.5pt: false pass':>24}")
for d, n in ((0.02, 1500), (0.04, 3200), (0.08, 6400), (0.04, 1500), (0.08, 3200)):
    ok1, ok = run(d, n, 0.0)
    _, fp = run(d, n, -0.01)
    _, fp15 = run(d, n, -0.015)
    print(f"{d:>5.2f} {n:>8} | {100 * ok1:>13.1f}% {100 * ok:>9.1f}% | {100 * fp:>21.2f}% | {100 * fp15:>23.2f}%")
print(f"\nTrials per cell: {TRIALS}; Monte Carlo standard error of a 2.5% rate ~ "
      f"{100 * (0.025 * 0.975 / TRIALS) ** 0.5:.2f} pt")
