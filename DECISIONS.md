# Decisions

Choices made where the spec was silent or turned out not to fit reality. Each entry: what, and why.

## Layout and manifests

- **Plugin at the marketplace root (`"source": "./"`).** `claude plugin validate .` passes with it (Claude Code 2.1.308), so the fallback move to `plugins/measure/` was not needed. The validator warns that there is no plugin icon; none was added.
- **Trigger evals live in `evals/`** as `claude plugin eval` cases (one prompt per directory, a deterministic `tool_used: Skill` grader with `min: 1` for must-trigger and `min: 0, max: 0` for must-not). Result on 2026-10-27: 11/11 cases, 3/3 runs each, $2.23. The eval sandbox loads only this plugin, so "a Skill was called" means this skill.
- **`tests/e2e.sh` and `tests/false_positive_rate.py`** implement spec 9.2 and the 9.4 false-positive criterion; `.github/workflows/ci.yml` runs them with the unit tests on Python 3.9, 3.12 and 3.13.

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

## Demo (examples/python-logscan)

- **Size.** 120k lines instead of about 200k, because pure-Python per-line work made 200k lines take about 1.6 s on a fast laptop. Measured shares on the authoring machine: normalize about 50% inclusive, dedupe 22 to 29%, render_report 0.2% (✗).
- **The `parse_ts` trap had to be engineered, and measured.** The first designs made the "replace strptime" fix a real 3 to 7% gain: strptime costs about 20 to 50 µs per call in situ (each call goes through `locale.getlocale`), and its one-time initialization is 6 to 20 ms. The final design caches per timestamp, uses only 30 distinct seconds (`gen.py --seconds 30`), and pays strptime's initialization once more for the report's `date:` line, so a rewrite only moves that cost. Interleaved scan-only timings of the fixed version against the strptime-free version: median 392 ms vs 393 ms. `parse_ts` itself stays above the gate after the two real fixes (3.5 to 6% from per-call overhead).
- **Step 4 may read INCONCLUSIVE instead of NO_EFFECT** on a noisy machine. EXPECTED.md accepts both, because the required action (revert) is the same.
- **No GIF yet.** The README references `docs/demo.gif`, to be recorded from EXPECTED.md.

## Not verified on the authoring machine

- **perf end to end**: `kernel.perf_event_paranoid=3` there. The perf collapser is tested against a fixture; the perf profile path needs a run on a machine where perf is allowed.
- **macOS**: untested.
- **The "4 of 5 fresh sessions" acceptance check** (spec 9.4) has not been run. It needs five recorded Claude Code sessions on the demo.
- **The 20-repetition false-positive rate** was not run locally: the machine was under heavy unrelated load (load average 7 to 13), which is exactly the condition the check should not be judged under. CI runs it. Single local self-compares gave NO_EFFECT (when load dropped) and INCONCLUSIVE with CIs up to [0.952, 0.998] (at load 10). Neither is a false positive, but that interval shows that bursty load can push a CI for identical code off 1.0.
