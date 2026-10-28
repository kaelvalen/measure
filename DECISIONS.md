# Decisions

Choices made where the spec was silent or turned out not to fit reality. Each entry: what, and why.

## Layout and manifests

- **Plugin at the marketplace root (`"source": "./"`).** `claude plugin validate .` passes with it (Claude Code 2.1.308), so the fallback move to `plugins/measure/` was not needed. The validator warns that there is no plugin icon; none was added.
- **Trigger evals live in `evals/`** as `claude plugin eval` cases (one prompt per directory, a deterministic `tool_used: Skill` grader with `min: 1` for must-trigger and `min: 0, max: 0` for must-not). Result on 2026-10-27: 11/11 cases, 3/3 runs each, $2.23. The eval sandbox loads only this plugin, so "a Skill was called" means this skill.
- **`tests/e2e.sh`** implements spec 9.2. The decoy check accepts `render_report` either marked ✗ or absent: on a fast CI runner it got no samples at all (first CI run, 2026-10-28).
- **The 9.4 false-positive criterion is checked by simulation, not by 20 self-compares.** 0 false positives in 20 bounds the rate only at about 14% (rule of three), and a shared runner's noise changes daily. `tests/simulate_null.py` runs compare's look schedule and decision code on 2000 synthetic null compares per noise model (iid, drift, bursts, heavy tails, periodic load, all combined) in a few seconds and fails CI if the exact 95% upper bound on the false-positive rate exceeds 5% for any model. `tests/self_compare_smoke.py` (3 real self-compares) only shows that compare works end to end. The first CI run of the old 20-repetition test gave 0/20, but one of those intervals for identical code was [0.985, 0.997]: only the 2% band kept it from being a false REGRESSED.
- **`.github/workflows/ci.yml`** runs the unit tests on Python 3.9, 3.12 and 3.13, the e2e script, the simulation and the smoke test.

## Statistics (changed after review, 2026-10-28)

- **Paired analysis instead of two independent samples.** The spec's bootstrap resampled A and B independently, which throws away the pairing the interleaved design creates: drift shared by a pair entered both sides' variance, and bursty load broke the independence the bootstrap assumed. compare now computes one log ratio per pair, reports gain = exp(median), and builds the interval from those ratios.
- **Sign-test interval, not a bootstrap** (deviation from spec 4.3). A block bootstrap over pairs was tried first and measured: at 15 pairs its 95% interval missed 1.0 for identical code 7.2% to 8.3% of the time with block length 1, 8.3% to 11% with length 2 and up to 18% with length 5. Longer blocks made it worse, because the coin-flip order already makes the pair ratios uncorrelated. The same coin flip makes the sign of each pair's noise a fair, independent coin under the null, whatever the noise process, so the order-statistic (sign-test) interval for the median is exact: measured miss rate 2.0% to 4.7% across all noise models and n = 15, 30, 60, at a width 10% to 17% larger than the bootstrap's. It is deterministic and needs no resampling, so the 10,000-resample constant is gone.
- **Consequences.** Values must be positive (a ratio of a zero or negative parsed metric is an error). Below 6 pairs no 95% interval exists, so the verdict is INCONCLUSIVE and compare warns.
- **Interim looks are corrected.** `--until-conclusive` looks up to 6 times (15, 25, ..., 60 pairs). Without correction, intervals for identical code excluded 1 about 10% to 11.5% of the time in simulation. An early stop now requires a 1 − 0.05/K interval (99.2% for K = 6), and the last look uses 95%: 2.9% to 4% in simulation. At the default 2% band, the full procedure's false-positive rate is 0% to 0.15% across models. Cost: about 10 more pairs on average for a real 5% gain on a quiet machine, and lower power on a heavily loaded one (41% to 32% at 60 pairs in the "loaded" model). The verdict line prints the level it stopped at (`99.2% CI`), and the JSON records every look.

## measure.py

- **Command after `--`.** One token is used verbatim (so `-- 'a | b'` keeps shell syntax); several tokens are shell-quoted and joined (so `-- python3 -c 'print(1)'` survives).
- **`profile --cwd DIR`** (not in the spec). The workflow profiles the baseline worktree, which needs a working directory other than the current one.
- **Two baseline worktrees.** The spec's single `.measure/base` cannot serve both "loop against the new baseline" and "final compare of the original baseline". SKILL.md freezes `.measure/orig` once and moves `.measure/base` forward after each kept change.
- **`--metric rss` on bench and compare** (not in the spec). "Reduce memory usage of the loader" is in the must-trigger set, and a verdict needs a metric. Peak RSS from `wait4` was already collected, so a memory verdict cost one flag. Lower is better; mutually exclusive with `--parse`.
- **Workload exit status under profilers.** py-spy 0.4 can exit 1 ("No child process") after a successful recording, and perf's exit status is not the workload's. measure wraps the command in `sh` and writes the real exit status to `PREFIX.rc`.
- **py-spy flags.** `--function` is always passed so a function is one row in hotspots, not one row per line. The command always runs under `sh -c`; `--subprocesses` makes py-spy follow the Python children. The `process PID:"cmdline"` root frames get their PIDs stripped and the empty-cmdline `sh` frame dropped, so two profiles of the same command line up.
- **Line numbers in `diff`.** Frames are matched with the trailing `:LINE` removed (`normalize (logscan.py:34)` → `normalize (logscan.py)`). Without this, an edit above a function shifts its first line and the diff shows it as fully removed plus "new". The demo run hit exactly this. `hotspots` keeps line numbers, since they help locate code.
- **pprof.** ` (inline)` suffixes are stripped so a function is one row. `go tool pprof` runs with `CGO_ENABLED=0`; without it, machines with no C compiler fail to build the pprof tool (hit on the authoring machine).
- **Folded output** is sorted by stack, integer weights where possible. No header comments, so flamegraph.pl and other tools read it unchanged.
- **perf collapser** counts one per sample (period ignored), like `stackcollapse-perf.pl`, with the comm as the root frame.
- **cpuprofile.** A frame with an empty URL is labeled by its function name alone, so `(garbage collector)` and `(program)` stay readable. Negative `timeDeltas` are clamped to 0.
- **Hotspots table** prints the union of the top N by self and the top N by inclusive time, sorted by self, so it can have up to 2N rows.
- **Gain formatting.** Three decimals when the gain and both CI bounds are within 10% of 1, otherwise two. Near the 2% threshold, `1.01x [1.00, 1.02]` would hide the decision.
- **Budget note.** When `--until-conclusive` runs out of budget, the note `(run budget exhausted: treat as NO_EFFECT)` is appended to the verdict line itself, so quoting the line carries it.
- **`--strict-output`** aborts right after the first pair whose outputs differ, before spending the rest of the budget.
- **Timeouts** kill the whole process group (`start_new_session=True`) and count as a workload failure (exit 4).
- **doctor.** An unexposed governor or turbo state (VMs, containers, macOS) is reported as `ok` with a note rather than a warning, since nothing can be fixed. `ptrace_scope=3`, or `2` without `CAP_SYS_PTRACE`, is `fail`. A container without `CAP_SYS_PTRACE` is `warn`.
- **Diff zoom** only magnifies rectangles rendered at the initial scale; rectangles under 0.5 px at full width are skipped (spec) and do not reappear when zoomed.
- **Default `--min-effect` stays at 2%** (open decision in spec 11). reference/statistics.md explains when to pick 1% or 5%.
- **No Stop hook in v0.1** (open decision in spec 11). A plugin's hook runs in every session. One false positive on the word "faster" in a session that has nothing to do with performance is enough for someone to uninstall. A safe scope (only while `.measure/` exists, or only after the skill loaded) is its own design task, planned for v0.2.

## Demo (examples/python-logscan)

- **Size.** 120k lines instead of about 200k, because pure-Python per-line work made 200k lines take about 1.6 s on a fast laptop. Measured shares on the authoring machine: normalize about 50% inclusive, dedupe 22 to 29%, render_report 0.2% (✗).
- **The `parse_ts` trap had to be engineered, and measured.** The first designs made the "replace strptime" fix a real 3 to 7% gain: strptime costs about 20 to 50 µs per call in situ (each call goes through `locale.getlocale`), and its one-time initialization is 6 to 20 ms. The final design caches per timestamp, uses only 30 distinct seconds (`gen.py --seconds 30`), and pays strptime's initialization once more for the report's `date:` line, so a rewrite only moves that cost. Interleaved scan-only timings of the fixed version against the strptime-free version: median 392 ms vs 393 ms. `parse_ts` itself stays above the gate after the two real fixes (3.5 to 6% from per-call overhead).
- **Step 4 may read INCONCLUSIVE instead of NO_EFFECT** on a noisy machine. EXPECTED.md accepts both, because the required action (revert) is the same.
- **The trap is not a perfect no-op.** The earlier design check compared medians of scan-only timings (392 vs 393 ms). The paired analysis is more sensitive and resolves a real effect of the strptime rewrite of about 1% to 3% (replays on 2026-10-28: `1.028x [1.007, 1.036]` after 60 pairs, then `1.016x [1.008, 1.020]` and `1.013x [1.001, 1.031]` at 40 pairs). Its source is not located: strptime's per-format setup and the 30 cached calls account for under 0.5 ms of the 3 to 6 ms difference. Because it sits just under the 2% bar, a run can occasionally read IMPROVED. This has to be fixed, or the bar raised for the demo, before the 4-of-5-sessions check.
- **No GIF yet.** The image line in the README is commented out until `docs/demo.gif` exists, so the page shows no broken image. It should be recorded from an unstaged session, not a scripted replay.

## Not verified on the authoring machine

- **perf end to end**: `kernel.perf_event_paranoid=3` there. The perf collapser is tested against a fixture; the perf profile path needs a run on a machine where perf is allowed.
- **macOS**: untested.
- **The "4 of 5 fresh sessions" acceptance check** (spec 9.4) has not been run. It needs five recorded Claude Code sessions on the demo.
- **Real-machine false-positive rate.** Replaced by the simulation above. Before the paired analysis, local self-compares at load 10 gave intervals for identical code as far off as [0.952, 0.998]; that observation is what the paired analysis fixes.
