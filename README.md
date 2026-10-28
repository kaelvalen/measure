# measure

Your agent says it's 2x faster. Is it?

<!-- GIF: record one unstaged session of examples/python-logscan (see EXPECTED.md), save as docs/demo.gif, then add:
![measure demo: profile, decline the decoy, two kept fixes, one reverted no-op, final report](docs/demo.gif) -->

**Profile first.** The agent may only optimize a frame the profiler shows, and only if removing it entirely could matter.
**Prove it with a confidence interval.** Every change gets an interleaved A/B benchmark and a paired confidence interval.
**Revert noise.** A change whose effect is indistinguishable from noise is rolled back, not reported as a win.

## Install

```
claude plugin marketplace add kaelvalen/measure
claude plugin install measure@measure
```

Manual: copy `skills/measure/` to `~/.claude/skills/measure/`. The only runtime dependency is Python 3.9+, plus a profiler for your language (py-spy, perf, Node or Go).

## The rules

1. No "faster", "optimized" or "Nx" without a `measure compare` verdict, quoted verbatim.
2. No optimization target without a profile frame and its share.
3. Amdahl gate: if deleting the frame could not beat the minimum effect, leave it alone.
4. One change per measurement.
5. Tests pass and outputs match before timing counts.
6. NO_EFFECT, or INCONCLUSIVE after the run budget, means revert.
7. Regressions are reported as loudly as wins.
8. Release builds only.

## Example report

Real output from the demo in [`examples/python-logscan`](examples/python-logscan/EXPECTED.md) on a busy laptop. The verdict lines come from a scripted replay of the demo's edits, not yet from a recorded agent session:

```
## Performance report
Workload: python3 logscan.py .measure/workloads/data.log   Env: linux 7.3 · 16c · governor=performance · turbo=on(!) · load 2.9   Runs: 15+ per side, interleaved
A/A calibration: VERDICT NO_EFFECT  gain 0.997x [0.987, 1.014] 99.2% CI  median 592.9ms → 594.7ms  n=45+45 interleaved  cv 7.2%/6.7%
| # | Change | Target frame (share) | Verdict | Gain (95% CI) | Kept |
|---|--------|----------------------|---------|---------------|------|
| 1 | precompile normalize masks | normalize (50.5% incl) | IMPROVED | 1.26x [1.24, 1.28] | yes |
| 2 | dict-based dedupe | dedupe (30.3% incl) | IMPROVED | 1.38x [1.36, 1.40] | yes |
| 3 | fromisoformat in parse_ts | parse_ts (3.5% incl) | NO_EFFECT | 1.002x [0.985, 1.015] | no, reverted |
Cumulative: VERDICT IMPROVED  gain 1.76x [1.68, 1.79] 99.2% CI  median 578.7ms → 330.1ms  n=15+15 interleaved  cv 1.8%/3.2%
Rejected hypotheses: render_report — Amdahl gate (0.2%, max gain 1.00x); parse_ts — NO_EFFECT
Diff flamegraph: .measure/diff-final.html
```

The interesting rows are the ones another agent would get wrong: the ugliest function in the file (`render_report`) is 0.2% of runtime and was never touched, and the "strptime is slow" rewrite was measured, found to be noise, and reverted. The A/A line shows that running the two sides in different directories did not bias this machine. Note also that the cumulative gain (1.76x) is its own measurement, not 1.26 × 1.38.

## How it works

**Interleaving.** `measure compare` runs A and B in pairs, each pair in a coin-flip order, after a warmup of both. Thermal drift, turbo budgets, cache warmth and slow background load hit both sides equally instead of piling onto whichever ran second. The seed is recorded so the schedule can be replayed.

**Paired CI.** Each pair yields one ratio, A / B, oriented so that above 1 always means B is better; the gain is the median ratio. Drift and load shared by the two runs of a pair cancel in the ratio. The 95% interval is the sign-test interval for that median. Because each pair's order was a coin flip, it is exact for identical code run in the same place, whatever the noise does, with no independence assumption. measure compares the interval against a minimum effect (default 2%) to get IMPROVED, NO_EFFECT, REGRESSED or INCONCLUSIVE. `--until-conclusive` adds runs until the interval is decisive or the budget runs out; an early stop must clear a stricter interval, and the chance that identical code is ever called different stays at or below 5% for any budget (3.76% for the default, computed exactly in `tests/test_sequential_exact.py`).

**A/A calibration.** That guarantee cannot cover a bias tied to a side: A and B run in different directories, and path length alone can shift memory layout by a few percent. So the agent's first compare, before any edit, is the baseline against an identical copy of itself in the working tree. It must read NO_EFFECT; if it does not, the environment's own bias exceeds the minimum effect, and the agent reports it and raises the bar instead of reporting that bias as a win. Outputs are hashed on both sides, so a fast wrong answer is flagged.

**Amdahl gate.** `measure hotspots` lists self and inclusive share per frame (recursion counted once), and the best case gain 1 / (1 − share). Frames whose best case cannot beat the minimum effect are marked ✗, and the skill forbids touching them.

**Diff flame graph.** `measure diff` renders before and after flame graphs into one self-contained HTML file on the same x scale, scaled to median wall time, so a real speedup is visibly shorter. Frames are colored by the change of their stack path (blue shrank, red grew). The top changes are also printed for the agent, which cannot see HTML.

## Supported profilers

| Language | Profiler | Notes and limitations |
|---|---|---|
| Python | py-spy (`pip install py-spy`) | `--native` for C extensions. Containers need `--cap-add SYS_PTRACE`; macOS needs sudo. |
| C, C++, Rust, other native | perf | Linux only. Needs `perf_event_paranoid` ≤ 1 (or 2 for user space). Frame pointers or `--callgraph dwarf`. Not available on macOS in v1. |
| Node | `--cpu-prof` via `NODE_OPTIONS` | Node 12+. Child processes are profiled too; the largest profile is used. |
| Go | `go test -cpuprofile` + `pprof -traces` | Benchmarks in one package. Plain Go binaries go through perf. |
| JVM, anything else | `measure profile --from FILE` | Accepts collapsed stacks (async-profiler `-o collapsed`), `.cpuprofile`, `pprof -traces` and `perf script` text. |
| GPU / ML | none (v1) | Compare on the program's own throughput line with `--parse 'tok/s: ([\d.]+)' --higher-better`. |
| Memory | wait4 rusage | `compare --metric rss` gives a verdict on peak resident memory. |

Windows is not supported. Details and fixes: [`skills/measure/reference/profilers.md`](skills/measure/reference/profilers.md).

## FAQ

**Why not hyperfine?** hyperfine is a good benchmark runner, and you can use it as the workload command. But it has no profiler integration, no Amdahl gate, no output-equivalence check and no agent protocol. It answers "how long does this take", not "should this change be kept".

**Can I skip measuring?** Yes: tell the agent so. The skill still works, but every claim you did not measure is labeled "unmeasured" instead of "faster".

**My self-compare is not NO_EFFECT.** Run `measure doctor`. A busy or thermally throttled machine widens intervals; the min-effect band keeps that from turning into false wins, but it can leave verdicts INCONCLUSIVE. See [`reference/statistics.md`](skills/measure/reference/statistics.md).

## Contributing

Collapsers for new profile formats are the easiest PRs. A collapser is a pure function from profiler output to `{"frame;frame;leaf": weight}`, with a fixture and an exact-output test in `tests/`. Run the tests with `python3 -m unittest discover -s tests` (including the exact false-positive test), the power simulation with `python3 tests/simulate_null.py`, the A/A smoke test with `python3 tests/self_compare_smoke.py`, the end-to-end check with `tests/e2e.sh` (needs py-spy) and the trigger evals with `claude plugin eval .`.

License: MIT.
