# logscan demo: expected agent behavior

`logscan.py` summarizes about 120k synthetic log lines (`gen.py`, fixed seed) in roughly 0.5 to 1 s. It contains two real problems, a decoy and a trap. A Claude Code session with the measure skill should go through the five steps below. The verdict lines quoted here come from a scripted replay of these edits on a laptop (`turbo=on(!)`, Python 3.14); your numbers will differ, but the verdicts should not. `99.2% CI` marks a verdict reached at an interim look.

## Setup for a recording

Run the demo from a fresh copy, so the agent cannot read this file or the reference fix:

```
mkdir -p /tmp/logscan && cp gen.py logscan.py /tmp/logscan && cd /tmp/logscan
git init -q && git add -A && git commit -qm init
mkdir -p .measure/workloads && python3 gen.py -o .measure/workloads/data.log
claude "logscan.py is slow on .measure/workloads/data.log. Make it faster."
```

## 1. A/A calibration, profile, and decline the decoy

Before any edit, the baseline worktree against the identical working tree must read NO_EFFECT:

```
VERDICT NO_EFFECT  gain 0.997x [0.987, 1.014] 99.2% CI  median 592.9ms → 594.7ms  n=45+45 interleaved  cv 7.2%/6.7%
```

The first profile of the baseline (abridged):

```
  self%   incl%   max gain   frame
   38.9    50.5      2.02x      normalize (logscan.py:34)
   30.3    30.3      1.43x      dedupe (logscan.py:41)
   15.0    97.9     47.73x      scan (logscan.py:107)
    8.2    10.9      1.12x      _compile (re/__init__.py:330)
    1.7     1.7      1.02x  ✗   parse_ts (logscan.py:24)
    0.2     0.2      1.00x  ✗   render_report (logscan.py:50)
```

`render_report` is the decoy: hand-rolled top-15 selection with nested `while` loops and character-by-character padding. It looks like the worst code in the file, and it runs once. The agent must say it is behind the Amdahl gate (✗, max gain 1.00x) and leave it alone.

## 2. Fix `normalize`: IMPROVED, kept

Hypothesis: `Target normalize (38.9% self / 50.5% incl, max gain 2.02x): re.compile with re.IGNORECASE on every call; the cache lookup and flag handling (_compile, enum __get__) cost ~12%.`
Fix: compile the masks once at module level (see `fixed/logscan.py`).

```
VERDICT IMPROVED  gain 1.26x [1.24, 1.28] 99.2% CI  median 562.9ms → 446.9ms  n=15+15 interleaved  cv 1.7%/0.8%
```

## 3. Fix `dedupe`: IMPROVED, kept

Hypothesis: `Target dedupe (30.3% self / 30.3% incl, max gain 1.43x): "t not in seen" scans a list of 320 templates for each of 120k lines.`
Fix: `list(dict.fromkeys(templates))`.

```
VERDICT IMPROVED  gain 1.38x [1.36, 1.40] 99.2% CI  median 449.0ms → 326.2ms  n=15+15 interleaved  cv 1.8%/1.7%
```

## 4. Try `parse_ts`: NO_EFFECT, reverted

After the two fixes, the total has shrunk and `parse_ts` crosses the gate (from 1.7% ✗ to about 3.5% inclusive, max gain 1.04x). Its docstring even says "strptime is slow". The tempting change is to replace `datetime.strptime` with `datetime.fromisoformat` or manual slicing.

It cannot help. `parse_ts` already caches by timestamp and the log spans 3 seconds, so strptime runs 3 times. Its one-time initialization and the format compile are paid anyway by the report's `date:` line, which parses the first timestamp with the same format. The frame's share is per-call overhead (string concatenation, a length check, a dict lookup), not strptime, which does not even appear in the table. Three replays: `1.002x [0.985, 1.015]`, `1.004x [0.988, 1.016]`, `0.996x [0.983, 1.013]`.

```
VERDICT NO_EFFECT  gain 1.002x [0.985, 1.015] 99.2% CI  median 327.2ms → 326.9ms  n=35+35 interleaved  cv 2.3%/2.1%
```

On a very noisy machine it can end as a budget-exhausted `INCONCLUSIVE` instead. Either way the rule is the same: **revert**, and list `parse_ts` under rejected hypotheses.

## 5. Final report

The cumulative line comes from its own compare of the original baseline against the final tree, not from multiplying 1.26 × 1.38:

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

## Pass criteria

A session reproduces the demo when it:
1. runs the A/A calibration (NO_EFFECT), profiles before editing, and explicitly declines `render_report` because of the gate;
2. fixes `normalize` and gets IMPROVED;
3. fixes `dedupe` and gets IMPROVED (steps 2 and 3 may come in either order, as separate compares);
4. tries a strptime replacement in `parse_ts`, gets NO_EFFECT or budget-exhausted INCONCLUSIVE, and reverts it;
5. ends with the report, a cumulative compare and a diff flame graph.

An agent that skips step 4 because strptime is absent from the table, or that cuts `parse_ts`'s per-call overhead and *measures* a real gain, is behaving correctly too, but it is not the scripted demo. Record another take.

## Tuning knobs

If the shares drift on other hardware or Python versions: `gen.py -n` (line count: total runtime), the 20 × 16 subject/action lists (320 templates: `dedupe` share), the three `MASKS` (`normalize` share) and `--seconds` (distinct timestamps; keep it at 3: with 30, the strptime calls inside the hot loop made the rewrite a real 1% to 3% gain, see DECISIONS.md).
