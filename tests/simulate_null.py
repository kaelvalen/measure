"""Power of compare under synthetic noise, plus a null sanity check.

The false-positive rate itself is a theorem, computed exactly in tests/test_sequential_exact.py:
every noise model below depends only on the execution slot, not on the A/B label, so for
identical code the coin-flip order makes the sign test exact and the null rows here can only
re-confirm it. What this script adds is power: how often a real 5% gain is detected, and how
many pairs it takes, under noise models that break the textbook assumptions:

  iid         lognormal noise, cv about 3%
  drift       iid plus a 20% slowdown over the session (thermal, turbo budget)
  bursts      iid plus background-load episodes lasting a few runs (x1.1 to x1.5)
  heavy-tail  iid plus rare large outliers (Pareto)
  waves       iid plus a slow periodic load, period 10 to 40 runs
  loaded      all of the above, cv about 6%

For each model it reports, for the paired sign-test interval that compare uses (and, with
--unpaired, for the bootstrap it replaced: A and B resampled independently, ratio of medians):
  fp    IMPROVED or REGRESSED for identical code, full procedure (--until-conclusive,
        min-effect 2%)
  miss  a single 95% CI at 15 pairs excludes 1.0 (nominal 5%: the coverage check, no
        min-effect band to hide behind)
  power IMPROVED when B is truly 5% faster, and the mean number of pairs it took

--check exits 1 unless, for the paired analysis in every model, the exact one-sided 95%
upper bound on fp is at most 5% and the observed miss rate is at most 7.5%.

Usage: python3 tests/simulate_null.py [--sims 2000] [--check] [--unpaired [--resamples 1000]]
"""
import argparse
import math
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "measure", "scripts"))
import measure  # noqa: E402

RUNS, MAX_RUNS, MIN_EFFECT = 15, 60, 0.02
FP_MAX, MISS_MAX = 0.05, 0.075
MODELS = ["iid", "drift", "bursts", "heavy-tail", "waves", "loaded"]


def noise_sequence(model, n_runs, rng):
    """Multiplicative noise for runs 0..n_runs-1, in execution order."""
    sigma = 0.06 if model == "loaded" else 0.03
    out = [math.exp(rng.gauss(0.0, sigma)) for _ in range(n_runs)]
    if model in ("drift", "loaded"):
        out = [x * (1.0 + 0.2 * t / n_runs) for t, x in enumerate(out)]
    if model in ("bursts", "loaded"):
        on, level = False, 1.0
        for t in range(n_runs):
            if on and rng.random() < 0.25:
                on = False
            elif not on and rng.random() < 0.05:
                on, level = True, rng.uniform(1.1, 1.5)
            if on:
                out[t] *= level
    if model in ("heavy-tail", "loaded"):
        out = [x * (1.0 + 0.2 * rng.paretovariate(1.5)) if rng.random() < 0.03 else x for x in out]
    if model in ("waves", "loaded"):
        period, phase = rng.uniform(10, 40), rng.uniform(0, 2 * math.pi)
        out = [x * (1.0 + 0.1 * math.sin(2 * math.pi * t / period + phase)) for t, x in enumerate(out)]
    return out


def simulate_pairs(model, seed, effect=1.0):
    """MAX_RUNS pairs scheduled like compare does; B is `effect` times faster."""
    rng = random.Random(seed)
    noise = noise_sequence(model, 2 * MAX_RUNS, rng)
    a, b = [], []
    for p, order in enumerate(measure.make_schedule(MAX_RUNS, seed)):
        for k, side in enumerate(order):
            x = noise[2 * p + k]
            if side == "A":
                a.append(x)
            else:
                b.append(x / effect)
    return a, b


def unpaired_ci(a, b, seed, resamples, level=measure.CI_LEVEL):
    """The analysis compare used before: resample A and B independently, ratio of medians."""
    rng = random.Random(seed)
    gains = []
    for _ in range(resamples):
        ma = statistics.median(rng.choices(a, k=len(a)))
        mb = statistics.median(rng.choices(b, k=len(b)))
        gains.append(ma / mb)
    gains.sort()
    tail = (1.0 - level) / 2.0
    return gains[int(math.floor(tail * resamples))], gains[int(math.ceil((1.0 - tail) * resamples)) - 1]


def sequential(a, b, seed, resamples, paired):
    """compare --until-conclusive on prefixes of the simulated pairs: (verdict, pairs used)."""
    looks = measure.look_schedule(RUNS, MAX_RUNS, True)
    ranks = measure.look_ranks(looks)
    for i, n in enumerate(looks):
        if paired:
            verdict = measure.evaluate_look(a[:n], b[:n], False, MIN_EFFECT, ranks[i])[0]
        else:
            # Before: a 95% CI at every look.
            verdict = measure.decide(*unpaired_ci(a[:n], b[:n], seed, resamples), MIN_EFFECT)
        if verdict != measure.INCONCLUSIVE:
            return verdict, n
    return verdict, looks[-1]


def binom_upper(k, n, conf=0.95):
    """Exact (Clopper-Pearson) one-sided upper confidence bound for a binomial rate."""
    if k >= n:
        return 1.0

    def cdf(p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))

    lo, hi = k / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if cdf(mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return hi


def run_model(model, sims, resamples, paired):
    fp = miss = power = 0
    pairs_used = []
    for s in range(sims):
        seed = 7919 * s + 1
        a, b = simulate_pairs(model, seed)
        verdict, _ = sequential(a, b, seed, resamples, paired)
        fp += verdict in (measure.IMPROVED, measure.REGRESSED)
        ci = (measure.gain_ci(a[:RUNS], b[:RUNS], False) if paired
              else unpaired_ci(a[:RUNS], b[:RUNS], seed, resamples))
        miss += not (ci[0] <= 1.0 <= ci[1])
        a, b = simulate_pairs(model, seed + 1, effect=1.05)
        verdict, n = sequential(a, b, seed + 1, resamples, paired)
        power += verdict == measure.IMPROVED
        pairs_used.append(n)
    return {"fp": fp, "miss": miss, "power": power, "pairs": statistics.fmean(pairs_used)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sims", type=int, default=2000)
    ap.add_argument("--resamples", type=int, default=1000, help="for the unpaired bootstrap")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--unpaired", action="store_true", help="also run the old unpaired bootstrap (slow)")
    args = ap.parse_args()

    n = args.sims
    print("%d simulated compares per model and analysis, %d unpaired resamples, min-effect %d%%, "
          "%d..%d pairs" % (n, args.resamples, MIN_EFFECT * 100, RUNS, MAX_RUNS))
    print("%-11s %-9s %16s %8s %14s" % ("model", "analysis", "fp (95% upper)", "miss", "power @5%"))
    failed = []
    t0 = time.time()
    for model in args.models.split(","):
        for paired in ((True, False) if args.unpaired else (True,)):
            r = run_model(model, n, args.resamples, paired)
            upper = binom_upper(r["fp"], n)
            label = "paired" if paired else "unpaired"
            print("%-11s %-9s %6.1f%% (%5.1f%%) %7.1f%% %6.1f%% in %4.1f" % (
                model, label, 100.0 * r["fp"] / n, 100 * upper, 100.0 * r["miss"] / n,
                100.0 * r["power"] / n, r["pairs"]))
            if paired and (upper > FP_MAX or r["miss"] / n > MISS_MAX):
                failed.append(model)
        sys.stdout.flush()
    print("%.0fs" % (time.time() - t0))
    if args.check and failed:
        print("FAIL: paired analysis over the limits (fp upper bound %d%%, miss %.1f%%) for: %s"
              % (FP_MAX * 100, MISS_MAX * 100, ", ".join(failed)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
