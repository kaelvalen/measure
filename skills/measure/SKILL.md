---
name: measure
description: Use for ANY performance work — making code faster, reducing latency, memory or allocations, speeding up a build, test suite, query or inference loop, investigating a slowdown or regression, or answering "is this faster?". Enforces profile-first optimization, interleaved A/B benchmarking with confidence intervals, and automatic revert of changes within noise. Use it even when the user only says "optimize this" or "this is slow".
---

# measure: profile first, prove it or revert it

Performance work runs on evidence. Profile before you change anything, prove every change with an interleaved A/B benchmark, and revert what the benchmark cannot tell apart from noise.

The tool is a single stdlib-only Python script, `scripts/measure.py` in this skill's directory. Run it as
`python3 <skill-dir>/scripts/measure.py <subcommand>`, where `<skill-dir>` is the base directory shown when this skill loaded. Below, `measure` stands for that full invocation. Everything after `--` is the workload, run through `sh -c`. Quote the whole workload as one argument when it uses pipes or redirects.

Exit codes: 0 ok or IMPROVED, 1 error, 2 NO_EFFECT or INCONCLUSIVE, 3 REGRESSED, 4 the workload failed, 5 outputs differ (with `--strict-output`).

## Iron rules

These rules are non-negotiable unless the user explicitly overrides one. If they do, label every affected claim "unmeasured".

1. **No claim without a verdict.** Never write "faster", "slower", "more efficient", "optimized" or "Nx" in any message, commit or PR text unless a `measure compare` verdict backs it. Quote the `VERDICT` line verbatim. Write "unmeasured" next to anything else.
2. **No target without a profile.** Every optimization hypothesis names a frame from `measure hotspots` output and its share of runtime.
3. **Amdahl gate.** If removing the target frame entirely could not exceed the minimum effect (the `max gain` column; gated frames are marked ✗), do not touch it. Say so and move on to the next frame.
4. **One change per measurement.** Never bundle two optimizations into one compare.
5. **Correctness first.** Tests pass and outputs are equivalent (compare prints `outputs identical`) before any timing counts.
6. **Noise means revert.** `NO_EFFECT` means revert. `INCONCLUSIVE` after the run budget means revert. Keep the change only if the user wants it for non-performance reasons, and then say it is kept for those reasons.
7. **Report regressions.** Report a `REGRESSED` verdict as prominently as an improvement.
8. **Release builds only.** Benchmark optimized builds: `cargo build --release`, `-O2`, `NODE_ENV=production`, no debugger, no coverage.

## Workflow

### 0. Doctor
Run `measure doctor` and read the warnings. Fix what you can (close noisy processes, plug in the laptop). Note the rest, plus its `env:` line, for the final report. If py-spy or perf is missing, show the user the printed install command. Do not guess around a missing profiler.

### 1. Workload
Find or create a reproducible command that exercises the slow path.
- Use a fixed seed and fixed input. Pass inputs by **absolute path** so both sides of a compare read the same file.
- Make each run take at least 200 ms. Below that, process startup dominates. Use a bigger input, or have the program print its own metric and use `--parse REGEX` (add `--higher-better` for throughput).
- Memory work: compare with `--metric rss` (peak resident MiB) instead of time.
- Store generated workloads and inputs in `.measure/workloads/`.
- If you cannot build a workload without guessing the user's real usage, ask the user once.

### 2. Freeze the baseline
```
SNAP=$(git stash create)
git worktree add --detach .measure/orig "${SNAP:-HEAD}"
git worktree add --detach .measure/base "${SNAP:-HEAD}"
```
`git stash create` captures uncommitted tracked changes without touching the working tree. Untracked files are not captured; copy the ones the workload needs into both worktrees. Build the baseline in `.measure/base` the same way you build the working tree. `.measure/orig` stays frozen for the final cumulative compare. `.measure/base` moves forward after each kept change.

### 3. Profile the baseline
```
measure profile --cwd .measure/base -o .measure/prof-base-N -- <cmd>
```
This writes `.measure/prof-base-N.folded` and prints hotspots: `self%`, `incl%`, `max gain`, and ✗ for gated frames. Re-print them with `measure hotspots FILE.folded`. Language detection is automatic; force it with `--lang python|native|node|go`. Use `--native` for Python C extensions (numpy, torch). Collapse an existing profile with `--from FILE`.

### 4. Hypothesis
Write exactly one line before touching code:
`Target <frame> (<self%> self / <incl%> incl, max gain <x>): <mechanism>`
The mechanism must explain where the frame's time goes. If the expensive-looking call inside the frame is itself small or ✗ in the table, it is the wrong mechanism.

### 5. Change
Make one change in the working tree. Run the tests and build.

### 6. Compare
```
measure compare --a-cwd .measure/base --b-cwd . --until-conclusive -o .measure/cmp-N.json -- <cmd>
```
This warms up both sides, then runs A/B pairs in randomized order. It prints a 95% bootstrap CI of the gain (above 1 always means B is better), the output check and one `VERDICT` line. That line is the only thing you may quote. Add `--ignore-line REGEX` for output lines that legitimately vary (timestamps). Use `--strict-output` to abort on any difference.

### 7. Decide
| Verdict | Action |
|---|---|
| `IMPROVED` | Keep. Quote the verdict line. |
| `NO_EFFECT` | Revert: copy the changed files back from `.measure/base`. Record "NO_EFFECT" under rejected hypotheses. |
| `INCONCLUSIVE` (budget exhausted) | Treat as NO_EFFECT and revert. If `doctor` showed noise you can remove, fix it and re-run once. |
| `REGRESSED` | Revert. Report it prominently. |
| `WARNING: outputs differ` | Not an optimization. Fix the behavior change or revert, then compare again. |

### 8. Diff (kept changes only)
```
measure profile -o .measure/prof-cand-N -- <cmd>
measure diff .measure/prof-base-N.folded .measure/prof-cand-N.folded --compare .measure/cmp-N.json -o .measure/diff-N.html
```
Both flame graphs share one x scale, scaled to median wall time. Blue frames shrank, red grew. The top-15 changes are printed for you; the HTML is for the user.

### 9. Loop
Move the baseline forward to include the kept change:
```
git worktree remove --force .measure/base
SNAP=$(git stash create); git worktree add --detach .measure/base "${SNAP:-HEAD}"
```
Then go back to step 3 with N+1. Stop when every remaining hot frame is ✗ or the user's goal is met. Frames that were ✗ earlier can cross the gate once other work shrinks the total, so re-read the table each round.

### 10. Cumulative compare and clean up
Compare the original baseline against the final state. Never multiply per-step gains.
```
measure compare --a-cwd .measure/orig --b-cwd . --until-conclusive -o .measure/cmp-final.json -- <cmd>
git worktree remove --force .measure/base; git worktree remove --force .measure/orig
```
Suggest adding `.measure/` to `.gitignore`. Do not edit `.gitignore` without asking.

## Final report

Every performance task ends with this report:

```
## Performance report
Workload: <cmd>   Env: <doctor env line>   Runs: <n per side, interleaved>
| # | Change | Target frame (share) | Verdict | Gain (95% CI) | Kept |
|---|--------|----------------------|---------|---------------|------|
Cumulative: <first baseline → final>, verdict line from a final compare of the original baseline vs HEAD
Rejected hypotheses: <frame — reason (Amdahl gate / NO_EFFECT / REGRESSED)>
Diff flamegraph: .measure/diff-N.html
```

Fill the Gain column from verdict lines only. The cumulative line is the verdict line of the step 10 compare. Decoys you declined at the Amdahl gate belong under "Rejected hypotheses", with their share.

## When NOT to use this workflow

- Pure readability refactors where the user stated performance does not matter.
- Algorithmic-complexity questions answered by reasoning ("what is the big-O of this?").

Even then, Iron rule 1 still applies to any claim you make.

## Pointers

- `reference/profilers.md`: permission errors, install commands, per-language flags (py-spy, perf, Go, Node, JVM, GPU/ML workloads).
- `reference/statistics.md`: read it when a verdict is surprising (interleaving, bootstrap, min-effect, why gains don't multiply, common traps).
