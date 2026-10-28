# Statistics: how to read a verdict

## Why the median, not the mean

Run times are not bell-shaped. They have a hard floor (the work itself) and a long right tail: a context switch, a page-cache miss, a garbage collection, a burst from another process. One 3-second outlier among fifteen 500 ms runs drags the mean up by more than 150 ms. It moves the median by at most one rank. measure reports the mean, standard deviation and coefficient of variation (cv = sd / mean) so you can see the noise, but the gain and its confidence interval are built on the median of per-pair ratios.

A cv above 5% is a warning: the environment or the workload is noisy. Run `measure doctor`, remove what it flags, or lengthen the workload.

## Why interleaving matters

Machines drift. CPUs heat up and clock down, turbo budgets run out, caches warm, a backup job starts halfway through. With a sequential schedule (AAAAA then BBBBB), any drift becomes part of the difference:

```
time →        cool CPU, 3.0 GHz              warm CPU, 2.7 GHz
sequential:   A A A A A A A A                B B B B B B B B
              A median 400 ms                B median 444 ms (same code: +11% "regression")
```

measure runs pairs instead. Each pair runs A and B back to back in an order chosen by a coin flip (a randomized block design). Both sides see the same conditions at almost the same moment, so slow drift cancels. The random order also cancels "whoever runs second gets a warm cache". The seed is recorded in the JSON, so the exact schedule can be replayed.

Interleaving does not cancel noise that changes faster than one pair, such as a burst that starts between the two runs. That noise widens the CI instead, which is the honest outcome.

## How the CI is computed

Each pair gives one ratio: a / b for times and memory, b / a with `--higher-better`, so above 1 always means B was better in that pair. The gain *g* is the median of the per-pair ratios (taken on logs). Pairing matters: what both runs of a pair shared (the clock speed at that moment, slow drift, a long burst of load) cancels in the ratio instead of adding variance to both sides.

The interval is the sign-test interval for that median: sort the n ratios and take the k-th smallest and the k-th largest, with k from the Binomial(n, ½) distribution. For 15 pairs that is the 4th and the 12th, which cover 96.5%. There is no resampling and no assumption about the noise. For identical code run in the same place, the coin flip that orders each pair makes its ratio equally likely to land above or below 1, independently of every other pair, whatever the machine is doing. So an interval for identical code excludes 1 with probability at most 5%, even under drift, bursts and outliers. Below 6 pairs no 95% interval exists and the verdict stays INCONCLUSIVE.

The guarantee stops at bias tied to a side. A and B run in different directories: path length and environment size shift stack and heap layout, and caches or builds can differ by directory. That bias is the same in every pair, so the coin flip cannot cancel it and the interval reports it as a real effect. This is what the A/A calibration in SKILL.md measures: the baseline against an identical working tree, before any edit. If it is not NO_EFFECT, raise `--min-effect` above the farther bound of its interval, or remove the cause.

`tests/simulate_null.py` shows power on synthetic noise (drift, bursts, heavy tails, periodic load). On that noise, the unpaired bootstrap measure used before detected a real 5% gain within 60 pairs 81% of the time under drift (paired: 97%) and 10% of the time under heavy load (paired: 32%), because drift and load land in both sides' spread instead of cancelling. On a quiet machine the two are close.

It **means**: given the runs you collected, gains outside this interval are poorly supported. A narrow interval is a precise estimate; a wide one says "collect more runs or reduce noise".

It **does not mean**:
- There is a 95% chance the true gain lies in this exact interval. The CI is a property of the procedure, not of one result.
- The change helps in production. It holds for this workload, input, build and machine only.
- The ratio holds at other load levels. If the change interacts with load (more threads, more memory bandwidth), its gain depends on how busy the machine was. `doctor` tells you when to distrust the machine.

## Why `--min-effect` exists

Statistical significance asks "is there any difference?". With enough runs, almost everything is "significant": a 0.3% change from code alignment can produce a CI that excludes 1. Practical significance asks "is the difference big enough to matter?". `--min-effect m` (default 0.02, so 2%) defines that threshold:

| Verdict | Condition |
|---|---|
| IMPROVED | CI low > 1 + m |
| REGRESSED | CI high < 1 / (1 + m) |
| NO_EFFECT | the whole CI lies inside [1 / (1 + m), 1 + m] |
| INCONCLUSIVE | anything else: the CI straddles a boundary |

NO_EFFECT is a positive finding: "whatever this change does, it is smaller than m". INCONCLUSIVE means the data cannot tell yet. `--until-conclusive` adds batches of 10 pairs up to `--max-runs`. If the budget runs out, the change has not earned its place: treat it as NO_EFFECT and revert.

Every look at the data is another chance to stop on noise. With 6 looks (15, 25, ..., 60 pairs) at 95% each, the exact chance that identical code is ever called different is 10.53%. So an early stop must clear a stricter interval, 1 − 0.05 / looks (99.2% for 6 looks), and only the last look uses 95%: exactly 3.76%. For other budgets compare computes its own exact rate when it starts and, if it would exceed 5%, widens the last look's interval until it does not (`tests/test_sequential_exact.py` checks every budget up to 150 pairs). The verdict line prints the level it stopped at: `99.2% CI` means an interim look.

Pick m for the workload: 2% suits macro workloads (a build, a request, a batch job). Microbenchmarks with very low noise can use 1%; noisy end-to-end systems may need 5%. Choose m **before** looking at the result.

## Why per-step gains don't multiply

Three kept changes with 1.30x, 1.20x and 1.10x do not make 1.72x:
- **Overlap.** Changes interact. A cache added in step 2 can make step 1's faster parser irrelevant.
- **Amdahl.** Each gain is relative to the total *at that step*. As the hot frames shrink, the untouched remainder dominates.
- **Error compounds.** Each factor carries its own CI; the product's uncertainty is much wider than any single CI, and nobody computes it.

Always run a final compare of the original baseline against the final state (`.measure/orig` vs `.`) and report that verdict line.

## Amdahl gate

If a frame accounts for share *p* of inclusive time, deleting it entirely gives at most 1 / (1 − p). A frame at 1.5% caps out at 1.015x, below a 2% min-effect, so no change to it can produce an IMPROVED verdict. That is why `hotspots` marks such frames ✗. Shares move as you optimize: after the big frames shrink, a frame that was ✗ can cross the gate. Re-read the table every round.

## Common traps

- **Warm vs cold caches.** The first run reads files from disk; later runs hit the page cache. Warmup runs (`--warmup`, default 2 per side) absorb this. If production is cold-cache, drop caches inside the workload deliberately rather than by accident.
- **First-run JIT and lazy initialization.** JVM, V8, PyPy and torch.compile get faster over the first iterations; so do lazy imports and one-time setup such as regex compilation or locale tables. Either warm up past it or make sure both sides pay it equally. Moving a one-time cost from one place to another is not a speedup.
- **Turbo and frequency scaling.** Turbo raises clocks until the chip is hot, then drops them, so run order and duration change the result. Prefer the `performance` governor and turbo off for A/B work (`doctor` prints the commands). Interleaving limits the damage when you cannot change them.
- **Laptops on battery.** Power management throttles aggressively and unpredictably. Plug in.
- **Background load.** Browsers, indexers, builds and other benchmarks steal cycles in bursts. `doctor` warns above 0.3 load per core.
- **Different inputs or paths.** Both sides must read the same input. Use one absolute path, not a copy per worktree.
- **Output changes.** A faster wrong answer is not an optimization. Check that compare printed `outputs identical`; use `--ignore-line` only for lines that legitimately vary.
- **Too-short runs.** Below about 200 ms, process startup and scheduler noise dominate. Lengthen the workload or `--parse` an internal timer.
