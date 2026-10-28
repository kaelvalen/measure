"""Exact false-positive rate of `measure compare`, by dynamic programming. No simulation.

For identical code run in the same place, the coin flip that orders each pair makes the sign
of each pair's log ratio a fair coin, independent across pairs, whatever the machine's noise
does. The sign-test interval at a look with n pairs and rank k excludes 1 exactly when the
number S of pairs that favored B satisfies S <= k - 1 or S >= n - k + 1. So the chance that
any look of a compare excludes 1 depends only on the look schedule and the ranks, and a DP
over the distribution of S computes it exactly.

This is the probability of "the interval excludes 1" at min-effect 0. With a min-effect band,
IMPROVED or REGRESSED needs the interval to exclude more than 1, so this bounds the
false-positive rate at every --min-effect.

It does not cover bias tied to the A/B label (different directories, environments or
binaries): randomization cannot cancel that. SKILL.md's A/A calibration step measures it.
"""
import os
import random
import sys
import unittest
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "measure", "scripts"))

import measure  # noqa: E402


def exact_null_rate(looks, levels):
    """P(some look's interval excludes 1) for identical code."""
    dist = {0: Fraction(1)}  # number of B-favoring pairs -> probability, among unstopped paths
    n = 0
    stopped = Fraction(0)
    for target, level in zip(looks, levels):
        while n < target:
            nxt = {}
            for s, p in dist.items():
                nxt[s] = nxt.get(s, 0) + p / 2
                nxt[s + 1] = nxt.get(s + 1, 0) + p / 2
            dist, n = nxt, n + 1
        k = measure.median_ci_rank(n, level)
        if k == 0:
            continue
        for s in [s for s in dist if s <= k - 1 or s >= n - k + 1]:
            stopped += dist.pop(s)
    return float(stopped)


def exact_rate_for_ranks(looks, ranks):
    """exact_null_rate with explicit ranks, in exact arithmetic (checks measure's float DP)."""
    dist = {0: Fraction(1)}
    n = 0
    stopped = Fraction(0)
    for target, k in zip(looks, ranks):
        while n < target:
            nxt = {}
            for s, p in dist.items():
                nxt[s] = nxt.get(s, 0) + p / 2
                nxt[s + 1] = nxt.get(s + 1, 0) + p / 2
            dist, n = nxt, n + 1
        for s in [s for s in dist if k > 0 and (s <= k - 1 or s >= n - k + 1)]:
            stopped += dist.pop(s)
    return float(stopped)


def compare_rate(runs, max_runs, until_conclusive):
    """The rate of compare as it runs: its look schedule and its (calibrated) ranks."""
    looks = measure.look_schedule(runs, max_runs, until_conclusive)
    return exact_rate_for_ranks(looks, measure.look_ranks(looks))


class SequentialExactTests(unittest.TestCase):
    def test_default_schedule(self):
        # --runs 15 --max-runs 60 --until-conclusive: looks at 15, 25, 35, 45, 55, 60
        self.assertAlmostEqual(compare_rate(15, 60, True), 0.0376, places=4)

    def test_without_the_correction_it_would_double(self):
        looks = measure.look_schedule(15, 60, True)
        self.assertGreater(exact_null_rate(looks, [0.95] * len(looks)), 0.10)

    def test_single_look_never_exceeds_alpha(self):
        for n in range(2, 201):
            self.assertLessEqual(exact_null_rate([n], [0.95]), 0.05, n)

    def test_every_budget_stays_under_five_percent(self):
        # Without calibration of the last look, 193 of these 910 budgets exceeded 5%
        # (worst: --runs 8 --max-runs 58, 6.02%).
        for runs in range(6, 41):
            for max_runs in range(runs, 151, 5):
                rate = compare_rate(runs, max_runs, True)
                self.assertLessEqual(rate, 0.05, (runs, max_runs, rate))

    def test_default_schedule_needs_no_widening(self):
        looks = measure.look_schedule(15, 60, True)
        nominal = [measure.median_ci_rank(n, measure.look_level(i, len(looks))) for i, n in enumerate(looks)]
        self.assertEqual(measure.look_ranks(looks), nominal)

    def test_measure_dp_matches_exact_arithmetic(self):
        for runs, max_runs in [(15, 60), (8, 58), (6, 6), (30, 120)]:
            looks = measure.look_schedule(runs, max_runs, True)
            ranks = measure.look_ranks(looks)
            self.assertAlmostEqual(measure.null_exclusion_rate(looks, ranks),
                                   exact_rate_for_ranks(looks, ranks), places=12)

    def test_sign_count_matches_gain_ci(self):
        # The DP's exclusion rule is the one gain_ci implements.
        rng = random.Random(3)
        for _ in range(2000):
            n = rng.randint(6, 60)
            level = rng.choice([0.95, 0.99, 1 - 0.05 / 6])
            d = [rng.choice([-1, 1]) * rng.expovariate(20) for _ in range(n)]
            a = [2.0 ** x for x in d]
            b = [1.0] * n
            lo, hi = measure.gain_ci(a, b, False, level)
            s = sum(x > 0 for x in d)
            k = measure.median_ci_rank(n, level)
            self.assertEqual(lo > 1 or hi < 1, s <= k - 1 or s >= n - k + 1)


if __name__ == "__main__":
    unittest.main()
